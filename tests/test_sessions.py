"""Session storage, rate limiting and the spend ledger.

The interesting cases here are all ones that only appear under conditions a
naive test never reaches: a truncation that lands inside a tool call, a cap
checked by several requests at once, a visitor who closes the tab. Each test
below is one of those.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from matienzo.web.sessions import budget, store
from matienzo.web.settings import Settings

NOON = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return store.connect(tmp_path / "sessions.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        sessions_db_path=tmp_path / "sessions.db",
        api_key="test-key",
        ip_salt="test-salt",
        daily_cap_micros=1_000_000,
        max_tokens=8192,
        rate_per_min=6.0,
        rate_burst=4.0,
    )


def text(value: str) -> dict[str, object]:
    return {"type": "text", "text": value}


class TestSessions:
    def test_an_unknown_id_starts_a_new_session_rather_than_failing(
        self, db: sqlite3.Connection
    ) -> None:
        """Cookies outlive the retention window, so this is routine."""
        created = store.ensure_session(db, "does-not-exist")
        assert created != "does-not-exist"

    def test_a_known_id_is_reused(self, db: sqlite3.Connection) -> None:
        first = store.ensure_session(db, None)
        assert store.ensure_session(db, first) == first

    def test_the_first_question_becomes_the_title(self, db: sqlite3.Connection) -> None:
        session_id = store.ensure_session(db, None)
        store.append_turn(db, session_id, role="user", blocks=[text("Which caves flood?")])
        row = db.execute(
            "SELECT title FROM session WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row["title"] == "Which caves flood?"

    def test_blocks_round_trip_verbatim(self, db: sqlite3.Connection) -> None:
        """The whole argument for storing the JSON rather than normalising it."""
        session_id = store.ensure_session(db, None)
        blocks = [
            {"type": "text", "text": "Looking that up."},
            {"type": "tool_use", "id": "toolu_1", "name": "search_sites", "input": {"query": "x"}},
        ]
        store.append_turn(db, session_id, role="assistant", blocks=blocks, model="claude-opus-5")
        replayed = store.history(db, session_id, model="claude-opus-5")
        assert replayed == [] or replayed[-1]["content"] == blocks


class TestHistory:
    def test_replay_starts_on_a_user_turn(self, db: sqlite3.Connection) -> None:
        """An assistant turn first is a 400, and naive truncation produces one."""
        session_id = store.ensure_session(db, None)
        for index in range(8):
            store.append_turn(db, session_id, role="user", blocks=[text(f"q{index}")])
            store.append_turn(
                db,
                session_id,
                role="assistant",
                blocks=[text(f"a{index}")],
                model="claude-opus-5",
            )
        messages = store.history(db, session_id, model="claude-opus-5", max_turns=5)
        assert messages[0]["role"] == "user"

    def test_a_tool_call_is_never_split_across_the_boundary(
        self, db: sqlite3.Connection
    ) -> None:
        """A user turn made of tool_result blocks is the second half of a tool
        call. Starting there orphans the tool_use that asked for it."""
        session_id = store.ensure_session(db, None)
        store.append_turn(db, session_id, role="user", blocks=[text("q0")])
        store.append_turn(
            db,
            session_id,
            role="assistant",
            blocks=[{"type": "tool_use", "id": "toolu_1", "name": "sql", "input": {}}],
            model="claude-opus-5",
        )
        store.append_turn(
            db,
            session_id,
            role="user",
            blocks=[{"type": "tool_result", "tool_use_id": "toolu_1", "content": "[]"}],
        )
        store.append_turn(
            db, session_id, role="assistant", blocks=[text("a0")], model="claude-opus-5"
        )

        messages = store.history(db, session_id, model="claude-opus-5", max_turns=2)
        assert messages[0]["content"][0]["type"] == "text", (
            "truncation landed on the tool_result half of a tool call"
        )

    def test_thinking_blocks_are_dropped_across_a_model_change(
        self, db: sqlite3.Connection
    ) -> None:
        """The API rejects a *modified* thinking block, not an absent one, so
        dropping is the safe direction and keeping is the unsafe one."""
        session_id = store.ensure_session(db, None)
        store.append_turn(db, session_id, role="user", blocks=[text("q")])
        store.append_turn(
            db,
            session_id,
            role="assistant",
            blocks=[{"type": "thinking", "thinking": "..."}, text("a")],
            model="claude-opus-5",
        )
        messages = store.history(db, session_id, model="claude-sonnet-5")
        types = [b["type"] for m in messages for b in m["content"]]
        assert "thinking" not in types

    def test_thinking_blocks_survive_on_the_same_model(self, db: sqlite3.Connection) -> None:
        session_id = store.ensure_session(db, None)
        store.append_turn(db, session_id, role="user", blocks=[text("q")])
        store.append_turn(
            db,
            session_id,
            role="assistant",
            blocks=[{"type": "thinking", "thinking": "..."}, text("a")],
            model="claude-opus-5",
        )
        messages = store.history(db, session_id, model="claude-opus-5")
        types = [b["type"] for m in messages for b in m["content"]]
        assert "thinking" in types

    def test_an_empty_session_replays_as_nothing(self, db: sqlite3.Connection) -> None:
        assert store.history(db, store.ensure_session(db, None), model="claude-opus-5") == []


class TestRetention:
    def test_old_sessions_are_pruned_with_their_turns(self, db: sqlite3.Connection) -> None:
        session_id = store.ensure_session(db, None)
        store.append_turn(db, session_id, role="user", blocks=[text("q")])
        db.execute(
            "UPDATE session SET last_seen_at = ? WHERE session_id = ?",
            ((NOON - timedelta(days=30)).isoformat(timespec="seconds"), session_id),
        )
        assert store.prune(db, ttl_days=14, now=NOON) == 1
        assert db.execute("SELECT count(*) AS n FROM turn").fetchone()["n"] == 0

    def test_recent_sessions_survive(self, db: sqlite3.Connection) -> None:
        store.ensure_session(db, None)
        assert store.prune(db, ttl_days=14, now=NOON) == 0


class TestClientAddress:
    def test_the_rightmost_hop_is_ours(self) -> None:
        """Each proxy appends, so the left of the list is whatever the client
        claimed. Reading index 0 lets anyone forge an identity per request."""
        assert budget.client_address("1.2.3.4, 5.6.7.8", "10.0.0.1", hops=1) == "5.6.7.8"

    def test_a_forged_prefix_is_ignored(self) -> None:
        forged = "203.0.113.9, 203.0.113.9, 198.51.100.7"
        assert budget.client_address(forged, "10.0.0.1", hops=1) == "198.51.100.7"

    def test_without_a_proxy_the_peer_is_used(self) -> None:
        assert budget.client_address("1.2.3.4", "10.0.0.1", hops=0) == "10.0.0.1"

    def test_a_missing_header_falls_back_to_the_peer(self) -> None:
        assert budget.client_address(None, "10.0.0.1", hops=1) == "10.0.0.1"


class TestRateLimit:
    def test_the_burst_is_exhausted_then_refused(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        ip = budget.hash_ip("1.2.3.4", settings.ip_salt)
        for _ in range(4):
            assert budget.check_rate(db, ip, settings=settings, now=1000.0).allowed
        refused = budget.check_rate(db, ip, settings=settings, now=1000.0)
        assert not refused.allowed
        assert refused.retry_after_seconds >= 1

    def test_the_bucket_refills_with_time(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        ip = budget.hash_ip("1.2.3.4", settings.ip_salt)
        for _ in range(4):
            budget.check_rate(db, ip, settings=settings, now=1000.0)
        assert not budget.check_rate(db, ip, settings=settings, now=1000.0).allowed
        assert budget.check_rate(db, ip, settings=settings, now=1020.0).allowed

    def test_addresses_are_limited_independently(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        first = budget.hash_ip("1.2.3.4", settings.ip_salt)
        second = budget.hash_ip("5.6.7.8", settings.ip_salt)
        for _ in range(4):
            budget.check_rate(db, first, settings=settings, now=1000.0)
        assert budget.check_rate(db, second, settings=settings, now=1000.0).allowed

    def test_the_raw_address_is_never_stored(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        budget.check_rate(
            db, budget.hash_ip("1.2.3.4", settings.ip_salt), settings=settings, now=1000.0
        )
        stored = [row["ip_hash"] for row in db.execute("SELECT ip_hash FROM rate_bucket")]
        assert "1.2.3.4" not in stored


class TestSpendCap:
    def test_concurrent_reservations_cannot_all_pass_the_same_check(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """The failure the reserve/reconcile protocol exists to prevent: with a
        plain `spent < cap` check every one of these would be granted, because
        none of them has spent anything yet."""
        estimate = budget.estimate_micros(settings)
        affordable = settings.daily_cap_micros // estimate

        granted = [
            budget.reserve(db, settings=settings, now=NOON).granted
            for _ in range(affordable + 3)
        ]
        assert sum(granted) == affordable

    def test_reconciling_returns_the_unused_reservation(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        reservation = budget.reserve(db, settings=settings, now=NOON)
        budget.reconcile(
            db,
            reservation,
            settings=settings,
            usage=budget.Usage(input_tokens=100, output_tokens=50),
            now=NOON,
        )
        state = budget.snapshot(db, settings=settings, now=NOON)
        assert state["reserved_micros"] == 0
        assert 0 < state["spent_micros"] < reservation.micros

    def test_an_abandoned_stream_releases_its_reservation(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        """A visitor closing the tab would otherwise hold budget until midnight."""
        reservation = budget.reserve(db, settings=settings, now=NOON)
        budget.release(db, reservation)
        assert budget.snapshot(db, settings=settings, now=NOON)["reserved_micros"] == 0

    def test_the_cap_rolls_over_at_midnight_utc(
        self, db: sqlite3.Connection, settings: Settings
    ) -> None:
        while budget.reserve(db, settings=settings, now=NOON).granted:
            pass
        assert budget.reserve(db, settings=settings, now=NOON + timedelta(days=1)).granted

    def test_cache_reads_are_priced_below_fresh_input(self, settings: Settings) -> None:
        """They are most of the input on this workload — the tool schemas and
        corpus primer are cached on every request — so pricing them as fresh
        input would overstate spend several-fold."""
        fresh = budget.cost_micros(budget.Usage(input_tokens=1_000_000), settings)
        cached = budget.cost_micros(budget.Usage(cache_read_tokens=1_000_000), settings)
        assert cached == pytest.approx(fresh * 0.1, rel=0.01)

    def test_a_zero_cap_refuses_everything(self, db: sqlite3.Connection, tmp_path: Path) -> None:
        settings = Settings(api_key="k", ip_salt="s", daily_cap_micros=0)
        assert not budget.reserve(db, settings=settings, now=NOON).granted


class TestSettings:
    def test_the_placeholder_salt_is_refused_when_answering(self) -> None:
        """Booting with it would hash every visitor's address with a value
        printed in the repository."""
        with pytest.raises(ValueError, match="IP_SALT"):
            Settings(api_key="k")

    def test_no_api_key_means_search_only_rather_than_a_failure(self) -> None:
        settings = Settings()
        assert not settings.can_answer

    def test_an_unpriced_model_is_refused(self) -> None:
        """Without a price the spend cap silently stops being enforceable."""
        with pytest.raises(ValueError, match="no price"):
            Settings(api_key="k", ip_salt="s", model="claude-imaginary-9")

    def test_the_daily_cap_is_read_as_dollars(self) -> None:
        settings = Settings.from_env(
            {"ANTHROPIC_API_KEY": "k", "MATIENZO_IP_SALT": "s", "MATIENZO_DAILY_CAP_USD": "2.50"}
        )
        assert settings.daily_cap_micros == 2_500_000

    def test_an_empty_environment_variable_is_treated_as_unset(self) -> None:
        assert Settings.from_env({"ANTHROPIC_API_KEY": "  "}).api_key is None
