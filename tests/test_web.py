"""The portal.

Most of these assert on the *shape* of a response stream rather than on its
wording. The event schema is a contract with the SPA — exactly one `start`,
exactly one terminal event, every `tool_result` answering a `tool_use` the
client actually saw — and those are the properties a client depends on and
therefore the ones a change can break silently.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs `uv sync --extra web`")

from fastapi.testclient import TestClient

from matienzo import tools as tool_registry
from matienzo.db import load as db_load
from matienzo.db.connect import fresh_database
from matienzo.web.app import create_app
from matienzo.web.provenance import Ledger, MarkerScanner
from matienzo.web.sessions import store
from matienzo.web.settings import Settings
from tests import fakes

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real corpus, small enough to build once per run.

    It has no embeddings, so every search here goes down the keyword-only
    fallback — which means the fallback is exercised on every test run rather
    than only on a machine where `matienzo embed` was never run.
    """
    path = tmp_path_factory.mktemp("web") / "corpus.db"
    with fresh_database(path) as connection:
        db_load.build(connection, sorted(FIXTURES.glob("[0-9][0-9][0-9][0-9].htm")))
    return path


def build_settings(fixture_db: Path, tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "db_path": fixture_db,
        "sessions_db_path": tmp_path / "sessions.db",
        "ip_salt": "test-salt",
        "keepalive_seconds": 0.05,
        "daily_cap_micros": 10_000_000,
    }
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def client(fixture_db: Path, tmp_path: Path) -> Iterator[TestClient]:
    """A portal with no API key: the permanent search-only configuration."""
    app = create_app(build_settings(fixture_db, tmp_path))
    # https, because the session cookie is `Secure` and httpx correctly refuses
    # to send one back over plain http — testing on http would quietly exercise
    # a cookie-less portal.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def agent_client(
    fixture_db: Path, tmp_path: Path, turns: list[fakes.Turn], **overrides: Any
) -> tuple[TestClient, fakes.FakeAnthropic]:
    """A portal wired to a scripted model."""
    settings = build_settings(fixture_db, tmp_path, api_key="test-key", **overrides)
    fake = fakes.FakeAnthropic(turns)
    app = create_app(settings)

    original = app.router.lifespan_context

    def patched(app_: Any) -> Any:
        context = original(app_)

        class Wrapper:
            async def __aenter__(self) -> None:
                await context.__aenter__()
                app_.state.client = fake

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                app_.state.client = None
                await context.__aexit__(exc_type, exc, traceback)

        return Wrapper()

    app.router.lifespan_context = patched
    return TestClient(app, base_url="https://testserver"), fake


def sse_events(response: Any) -> list[dict[str, Any]]:
    """Parse an SSE body into `(name, data)` dicts, ignoring heartbeats.

    Folding the `event:` line into the payload mirrors what `frontend/src/api.ts`
    does, and the two have to keep agreeing. An earlier version of the browser
    client switched on an `event` key it assumed was *in* the JSON — which it
    never is — and every test here passed anyway, because this helper was quietly
    supplying the field the client was missing.
    """
    events: list[dict[str, Any]] = []
    name: str | None = None
    for line in response.iter_lines():
        line = line.rstrip("\n") if isinstance(line, str) else line.decode().rstrip("\n")
        if not line or line.startswith(":"):
            continue
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: ") and name is not None:
            events.append({"event": name, **json.loads(line.removeprefix("data: "))})
            name = None
    return events


def ask(
    client: TestClient, question: str, *, conversation_id: str | None = None
) -> list[dict[str, Any]]:
    """One question. Without a conversation id it starts a new conversation —
    continuity travels in the body now, not in a cookie, so a test that means to
    ask a follow-up has to say which conversation it is following up on."""
    body: dict[str, Any] = {"question": question}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    with client.stream("POST", "/api/chat", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers.get("x-accel-buffering") == "no", (
            "nginx buffers the whole response without this, and 'streaming' "
            "silently becomes one blob at the end"
        )
        return sse_events(response)


def conversation_id(events: list[dict[str, Any]]) -> str:
    """The conversation an exchange belonged to, as the SPA learns it."""
    return str(next(e for e in events if e["event"] == "start")["session_id"])


class TestHealth:
    def test_health_reports_the_degraded_mode_explicitly(self, client: TestClient) -> None:
        """A portal serving keyword-only results looks exactly like a healthy one
        from outside unless it says so."""
        body = client.get("/healthz").json()
        assert body["corpus"] is True
        assert body["mode"] == "search_only"
        assert "embeddings" in body

    def test_health_reports_the_budget(self, client: TestClient) -> None:
        assert client.get("/healthz").json()["budget"]["remaining_pct"] == 100

    def test_health_survives_being_asked_by_several_callers_at_once(
        self, client: TestClient
    ) -> None:
        """`sessions_of` is a sync generator dependency, so FastAPI runs its
        setup, the endpoint body and its teardown on three different threadpool
        threads. A connection opened with sqlite3's default thread check raises
        the moment it crosses one of those boundaries.

        One request at a time hides it, because an idle pool hands back the
        thread it just used. This asserts on the case the SPA actually produces:
        it asks for health and for the thread list from the same effect.
        """
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = [pool.submit(client.get, "/healthz") for _ in range(40)]
            codes = {response.result().status_code for response in responses}
        assert codes == {200}


class TestSearch:
    def test_search_returns_ranked_sites_with_links(self, client: TestClient) -> None:
        body = client.post("/api/search", json={"query": "entrance shaft"}).json()
        assert body["results"]
        prefix = "https://www.matienzocaves.org.uk/"
        assert all(r["url"].startswith(prefix) for r in body["results"])

    def test_whether_semantic_ranking_ran_is_reported_not_assumed(self, client: TestClient) -> None:
        assert client.post("/api/search", json={"query": "shaft"}).json()["hybrid"] is False

    def test_an_empty_query_is_rejected(self, client: TestClient) -> None:
        assert client.post("/api/search", json={"query": ""}).status_code == 422

    def test_a_site_page_is_the_same_record_the_model_sees(self, client: TestClient) -> None:
        body = client.get("/api/site/1930").json()
        assert body["name"] == "Cobadal, Sumidero de"
        assert body["url"].endswith("/1930.htm")

    def test_an_unknown_site_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/site/5000").status_code == 404

    def test_an_out_of_range_site_is_rejected_before_the_query(self, client: TestClient) -> None:
        assert client.get("/api/site/99999").status_code == 422


class TestSearchOnlyChat:
    def test_no_api_key_degrades_rather_than_failing(self, client: TestClient) -> None:
        events = ask(client, "which caves flood?")
        assert events[0]["event"] == "start"
        assert events[0]["mode"] == "search_only"
        assert events[-1]["event"] == "done"

    def test_sources_are_emitted_before_the_answer_text(self, client: TestClient) -> None:
        """The panel should fill while the reader is still reading."""
        events = ask(client, "entrance shaft")
        names = [e["event"] for e in events]
        assert "source" in names
        assert names.index("source") < names.index("delta")

    def test_the_visitor_cookie_is_set_once_and_then_reused(self, client: TestClient) -> None:
        client.post("/api/chat", json={"question": "first"})
        first = client.cookies.get("matienzo_visitor")
        assert first
        client.post("/api/chat", json={"question": "second"})
        assert client.cookies.get("matienzo_visitor") == first

    def test_two_questions_without_an_id_are_two_conversations(self, client: TestClient) -> None:
        """The cookie used to make every question a follow-up. It names the
        visitor now, not the conversation, so a thread list can hold more
        than one."""
        assert conversation_id(ask(client, "first")) != conversation_id(ask(client, "second"))

    def test_a_conversation_is_continued_by_id(self, client: TestClient) -> None:
        first = conversation_id(ask(client, "first"))
        assert conversation_id(ask(client, "second", conversation_id=first)) == first

    def test_another_visitors_conversation_is_not_joined(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """The id arrives in the request body, where anybody can put anything.
        Continuing on it without an ownership check would append one visitor's
        question to another's history — and replay it back to them."""
        app = create_app(build_settings(fixture_db, tmp_path))
        with TestClient(app, base_url="https://testserver") as owner:
            theirs = conversation_id(ask(owner, "a private question"))
        with TestClient(app, base_url="https://testserver") as stranger:
            got = conversation_id(ask(stranger, "let me in", conversation_id=theirs))
        assert got != theirs


class TestStreamInvariants:
    """The properties the SPA relies on. Each one is a way a client breaks."""

    def test_exactly_one_start_and_one_terminal_event(self, client: TestClient) -> None:
        events = ask(client, "shaft")
        names = [e["event"] for e in events]
        assert names.count("start") == 1
        assert sum(names.count(n) for n in ("done", "error")) == 1

    def test_the_terminal_event_is_last(self, client: TestClient) -> None:
        events = ask(client, "shaft")
        assert events[-1]["event"] in {"done", "error"}

    def test_sequence_numbers_are_strictly_increasing(self, client: TestClient) -> None:
        """So a client can tell 'nothing yet' from 'I missed something'."""
        seqs = [e["seq"] for e in ask(client, "shaft")]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)


class TestConversations:
    """The thread list. Every assertion here is either "this visitor sees their
    own conversations" or "this visitor sees nothing of anybody else's"."""

    def test_a_new_browser_gets_a_token_and_an_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/conversations")
        assert response.status_code == 200
        assert response.json() == []
        assert client.cookies.get("matienzo_visitor")

    def test_reading_the_list_alone_creates_no_conversation(self, client: TestClient) -> None:
        """A token names a visitor whether or not a row mentions it. Writing one
        per page load would fill the database with empty threads."""
        client.get("/api/conversations")
        assert client.get("/api/conversations").json() == []

    def test_a_question_appears_in_the_list_under_its_own_words(self, client: TestClient) -> None:
        started = conversation_id(ask(client, "Which caves in Cobadal take water?"))
        listed = client.get("/api/conversations").json()
        assert [c["id"] for c in listed] == [started]
        assert listed[0]["title"] == "Which caves in Cobadal take water?"

    def test_conversations_are_listed_newest_first(self, client: TestClient) -> None:
        first = conversation_id(ask(client, "older"))
        second = conversation_id(ask(client, "newer"))
        assert [c["id"] for c in client.get("/api/conversations").json()] == [second, first]

    def test_a_transcript_replays_the_question_the_answer_and_its_sources(
        self, client: TestClient
    ) -> None:
        """What a reader saw, rebuilt from storage — without re-running the
        searches that produced it."""
        events = ask(client, "shaft")
        streamed = "".join(e["text"] for e in events if e["event"] == "delta")
        sites = [e["site_number"] for e in events if e["event"] == "source"]

        body = client.get(f"/api/conversations/{conversation_id(events)}").json()
        assert len(body["exchanges"]) == 1
        exchange = body["exchanges"][0]
        assert exchange["question"] == "shaft"
        assert exchange["answer"] == streamed
        assert [s["site_number"] for s in exchange["sources"]] == sites

    def test_a_transcript_keeps_a_tool_call_inside_the_exchange_that_made_it(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """The user turn carrying a `tool_result` is the second half of a tool
        call, not a new question. Reading it as one splits a single exchange into
        two, the second with a wall of JSON where its question should be."""
        client, _ = agent_client(
            fixture_db,
            tmp_path,
            [fakes.call_tools(("corpus_stats", {})), fakes.say("5,557 sites.")],
        )
        with client:
            asked = conversation_id(ask(client, "how many?"))
            body = client.get(f"/api/conversations/{asked}").json()

        assert len(body["exchanges"]) == 1, "the tool_result turn was read as a question"
        exchange = body["exchanges"][0]
        assert exchange["question"] == "how many?"
        assert "5,557 sites." in exchange["answer"]
        assert [(t["name"], t["ok"]) for t in exchange["tools"]] == [("corpus_stats", True)]

    def test_another_visitors_conversation_is_a_404(self, fixture_db: Path, tmp_path: Path) -> None:
        app = create_app(build_settings(fixture_db, tmp_path))
        with TestClient(app, base_url="https://testserver") as owner:
            theirs = conversation_id(ask(owner, "a private question"))

            with TestClient(app, base_url="https://testserver") as stranger:
                assert stranger.get(f"/api/conversations/{theirs}").status_code == 404
                assert stranger.delete(f"/api/conversations/{theirs}").status_code == 404

            assert owner.get(f"/api/conversations/{theirs}").status_code == 200, (
                "the refused delete went through anyway"
            )

    def test_deleting_removes_it_from_the_list_and_from_storage(self, client: TestClient) -> None:
        doomed = conversation_id(ask(client, "forget this"))
        assert client.delete(f"/api/conversations/{doomed}").status_code == 204
        assert client.get("/api/conversations").json() == []
        assert client.get(f"/api/conversations/{doomed}").status_code == 404

    def test_a_conversation_from_before_the_thread_list_is_adopted(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """Everyone mid-conversation on the deploy that added thread lists holds
        the old per-conversation cookie and no visitor token. Their history is
        still in the database; without adoption it is no longer reachable, which
        reads as the portal having thrown it away."""
        app = create_app(build_settings(fixture_db, tmp_path))
        with TestClient(app, base_url="https://testserver") as client:
            sessions = store.connect(tmp_path / "sessions.db")
            try:
                legacy = store.ensure_session(sessions, None)
                store.append_turn(
                    sessions, legacy, role="user", blocks=[{"type": "text", "text": "before"}]
                )
            finally:
                sessions.close()

            client.cookies.set("matienzo_session", legacy)
            listed = client.get("/api/conversations").json()

        assert [c["id"] for c in listed] == [legacy]

    def test_somebody_elses_old_cookie_claims_nothing(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """Adoption is guarded on the conversation being unowned. Without that,
        presenting a stolen cookie once would transfer the conversation."""
        app = create_app(build_settings(fixture_db, tmp_path))
        with TestClient(app, base_url="https://testserver") as owner:
            theirs = conversation_id(ask(owner, "a private question"))

            with TestClient(app, base_url="https://testserver") as thief:
                thief.cookies.set("matienzo_session", theirs)
                assert thief.get("/api/conversations").json() == []

            assert [c["id"] for c in owner.get("/api/conversations").json()] == [theirs]


class TestRateLimit:
    def test_the_limit_is_a_real_429_not_an_event(self, fixture_db: Path, tmp_path: Path) -> None:
        """Delivered as an SSE error inside a 200, every HTTP-level tool between
        us and the visitor sees success."""
        app = create_app(build_settings(fixture_db, tmp_path, rate_per_min=0.001, rate_burst=1.0))
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/api/chat", json={"question": "one"}).status_code == 200
            refused = client.post("/api/chat", json={"question": "two"})
            assert refused.status_code == 429
            assert int(refused.headers["retry-after"]) >= 1


class TestAgent:
    def test_a_plain_answer_streams_and_finishes(self, fixture_db: Path, tmp_path: Path) -> None:
        client, _ = agent_client(fixture_db, tmp_path, [fakes.say("Cobadal floods in winter.")])
        with client:
            events = ask(client, "does Cobadal flood?")
        assert events[0]["mode"] == "agent"
        assert "".join(e["text"] for e in events if e["event"] == "delta") == (
            "Cobadal floods in winter."
        )
        assert events[-1]["event"] == "done"

    def test_a_tool_call_runs_and_reports_back(self, fixture_db: Path, tmp_path: Path) -> None:
        client, _ = agent_client(
            fixture_db,
            tmp_path,
            [
                fakes.call_tools(("get_site", {"site_number": 1930})),
                fakes.say("It is the Sumidero de Cobadal [[site:1930]]."),
            ],
        )
        with client:
            events = ask(client, "what is 1930?")

        names = [e["event"] for e in events]
        assert "tool_use" in names and "tool_result" in names
        result = next(e for e in events if e["event"] == "tool_result")
        assert result["ok"] is True
        assert 1930 in result["site_numbers"]

    def test_a_question_is_sent_to_the_model_exactly_once(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """The route persists the question before opening the stream, so the
        history already contains it. Appending it again as well sent every
        question twice — allowed by the API, which merges consecutive user
        turns, so it cost tokens silently rather than erroring."""
        client, fake = agent_client(fixture_db, tmp_path, [fakes.say("Once.")])
        with client:
            ask(client, "a distinctive question")

        sent = json.dumps(fake.messages_sent[0])
        assert sent.count("a distinctive question") == 1

    def test_every_tool_result_answers_a_tool_use_the_client_saw(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        client, _ = agent_client(
            fixture_db,
            tmp_path,
            [
                fakes.call_tools(("get_site", {"site_number": 1930}), ("corpus_stats", {})),
                fakes.say("Done."),
            ],
        )
        with client:
            events = ask(client, "two things")
        asked = {e["id"] for e in events if e["event"] == "tool_use"}
        answered = {e["id"] for e in events if e["event"] == "tool_result"}
        assert answered <= asked and answered

    def test_parallel_results_go_back_in_a_single_user_message(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """Splitting them silently teaches the model to stop calling tools in
        parallel, which costs a round trip per tool from then on."""
        client, fake = agent_client(
            fixture_db,
            tmp_path,
            [
                fakes.call_tools(("get_site", {"site_number": 1930}), ("corpus_stats", {})),
                fakes.say("Done."),
            ],
        )
        with client:
            ask(client, "two things")

        second_request = fake.messages_sent[1]
        tool_result_turns = [
            m
            for m in second_request
            if m["role"] == "user" and any(b.get("type") == "tool_result" for b in m["content"])
        ]
        assert len(tool_result_turns) == 1
        assert len(tool_result_turns[0]["content"]) == 2

    def test_output_only_fields_are_never_replayed(self, fixture_db: Path, tmp_path: Path) -> None:
        """Response blocks and request blocks are not the same shape. A text
        block comes back as `ParsedTextBlock`, carrying `parsed_output`, which
        the API rejects with a 400 on the way back in.

        It takes two iterations to fire, because the second request is the first
        one that replays an assistant turn — which is why a suite full of
        single-turn scripts missed it entirely.
        """
        client, fake = agent_client(
            fixture_db,
            tmp_path,
            [
                fakes.call_tools(("corpus_stats", {}), preamble="Let me count."),
                fakes.say("There are 5,557 sites."),
            ],
        )
        with client:
            ask(client, "how many sites?")

        assert len(fake.calls) == 2, "the second request is the one that replays"
        replayed = json.dumps(fake.messages_sent[1])
        assert "parsed_output" not in replayed

    def test_replayed_blocks_keep_the_fields_the_api_needs(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """The other half of the same fix: stripping must not take `id` off a
        `tool_use` block, or the `tool_result` answering it is orphaned."""
        client, fake = agent_client(
            fixture_db,
            tmp_path,
            [fakes.call_tools(("corpus_stats", {})), fakes.say("Done.")],
        )
        with client:
            ask(client, "?")

        assistant = next(
            m
            for m in fake.messages_sent[1]
            if m["role"] == "assistant" and any(b.get("type") == "tool_use" for b in m["content"])
        )
        tool_use = next(b for b in assistant["content"] if b["type"] == "tool_use")
        assert {"type", "id", "name", "input"} <= set(tool_use)

    def test_a_poisoned_row_from_an_older_version_is_replayed_clean(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """The store keeps blocks verbatim, which is right for replay fidelity
        and also means it hands back whatever past versions of this code wrote.

        Rows containing `parsed_output` were written before `_as_dict` learned to
        strip it, and they kept failing every later request in those
        conversations long after the serialisation itself was fixed. Sanitising
        at the API boundary is what makes the old rows harmless.
        """
        client, fake = agent_client(
            fixture_db, tmp_path, [fakes.say("First answer."), fakes.say("Second answer.")]
        )
        with client:
            # A real first exchange, so the conversation is the one the portal
            # itself opened rather than one a test invented.
            session_id = conversation_id(ask(client, "first question"))

            sessions = store.connect(tmp_path / "sessions.db")
            try:
                # Re-poison it exactly as the pre-fix code did.
                sessions.execute(
                    "UPDATE block SET json = ? WHERE type = 'text' AND json LIKE '%First answer%'",
                    (json.dumps({"type": "text", "text": "First answer.", "parsed_output": None}),),
                )
            finally:
                sessions.close()

            ask(client, "second question", conversation_id=session_id)

        replayed = json.dumps(fake.messages_sent[1])
        assert "First answer" in replayed, "the earlier turn was not replayed at all"
        assert "parsed_output" not in replayed
        assert session_id

    def test_a_tool_exchange_survives_a_round_trip_through_storage(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """An assistant turn holding a `tool_use` must be followed by a user turn
        holding the matching `tool_result`. Storing the assistant blocks alone
        persists a conversation the API rejects on the next question."""
        client, _ = agent_client(
            fixture_db,
            tmp_path,
            [fakes.call_tools(("corpus_stats", {})), fakes.say("5,557 sites.")],
        )
        with client:
            ask(client, "how many?")

        sessions = store.connect(tmp_path / "sessions.db")
        try:
            session_id = sessions.execute("SELECT session_id FROM session").fetchone()[0]
            replayed = store.history(sessions, session_id, model="claude-opus-5")
        finally:
            sessions.close()

        asked = {b["id"] for m in replayed for b in m["content"] if b.get("type") == "tool_use"}
        answered = {
            b["tool_use_id"]
            for m in replayed
            for b in m["content"]
            if b.get("type") == "tool_result"
        }
        assert asked, "the tool_use block was not persisted at all"
        assert asked == answered, f"orphaned tool_use: asked {asked}, answered {answered}"

    def test_a_failing_tool_is_reported_and_the_loop_continues(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        client, _ = agent_client(
            fixture_db,
            tmp_path,
            [
                fakes.call_tools(("no_such_tool", {})),
                fakes.say("I could not look that up."),
            ],
        )
        with client:
            events = ask(client, "?")
        result = next(e for e in events if e["event"] == "tool_result")
        assert result["ok"] is False
        assert events[-1]["event"] == "done"

    def test_a_refusal_never_reads_the_empty_content_list(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        client, _ = agent_client(fixture_db, tmp_path, [fakes.refuse()])
        with client:
            events = ask(client, "something disallowed")
        assert events[-1]["event"] == "error"
        assert events[-1]["code"] == "refusal"

    def test_a_paused_turn_is_resumed_rather_than_truncating_the_answer(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        client, fake = agent_client(fixture_db, tmp_path, [fakes.pause(), fakes.say("Finished.")])
        with client:
            events = ask(client, "?")
        assert len(fake.calls) == 2
        assert events[-1]["event"] == "done"

    def test_running_out_of_steps_is_an_error_not_a_silent_stop(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        client, _ = agent_client(
            fixture_db,
            tmp_path,
            [fakes.call_tools(("corpus_stats", {})) for _ in range(3)],
            max_tool_iterations=3,
        )
        with client:
            events = ask(client, "loop forever")
        assert events[-1]["event"] == "error"
        assert events[-1]["code"] == "iteration_limit"

    def test_an_upstream_failure_becomes_an_error_event(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        settings = build_settings(fixture_db, tmp_path, api_key="k")
        app = create_app(settings)
        fake = fakes.FakeAnthropic([], fail_with=RuntimeError("connection reset"))
        with TestClient(app, base_url="https://testserver") as client:
            app.state.client = fake
            events = ask(client, "?")
        assert events[-1]["event"] == "error"
        assert events[-1]["code"] == "upstream"


class TestBudgetInteraction:
    def test_an_exhausted_budget_degrades_to_search_rather_than_erroring(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """The text already streamed is still a real answer, so this ends with
        `done`, not `error`."""
        client, _ = agent_client(fixture_db, tmp_path, [fakes.say("unused")], daily_cap_micros=1)
        with client:
            events = ask(client, "anything")
        names = [e["event"] for e in events]
        assert "notice" in names
        assert next(e for e in events if e["event"] == "notice")["code"] == "daily_cap"
        assert events[-1]["event"] == "done"

    def test_spend_is_recorded_against_the_day(self, fixture_db: Path, tmp_path: Path) -> None:
        client, _ = agent_client(fixture_db, tmp_path, [fakes.say("Hello.")])
        with client:
            ask(client, "hi")
        connection = sqlite3.connect(tmp_path / "sessions.db")
        connection.row_factory = sqlite3.Row
        spent = connection.execute("SELECT spent_micros FROM budget_day").fetchone()
        assert spent["spent_micros"] > 0


class TestPromptCaching:
    def test_the_cached_prefix_is_byte_identical_across_requests(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        """Anything varying in `tools` or `system` invalidates the cache on every
        request — not an error, just roughly triple the bill."""
        client, fake = agent_client(fixture_db, tmp_path, [fakes.say("one"), fakes.say("two")])
        with client:
            ask(client, "first")
            ask(client, "second")

        assert fake.calls[0]["system"] == fake.calls[1]["system"]
        assert fake.calls[0]["tools"] == fake.calls[1]["tools"]
        assert fake.calls[0]["system"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_the_tool_order_is_stable(self, fixture_db: Path, tmp_path: Path) -> None:
        client, fake = agent_client(fixture_db, tmp_path, [fakes.say("one")])
        with client:
            ask(client, "first")
        names = [tool["name"] for tool in fake.calls[0]["tools"]]
        assert names == [spec.name for spec in tool_registry.REGISTRY]


class TestMarkerScanner:
    def test_a_marker_split_across_deltas_is_reassembled(self) -> None:
        """The model emits `…tunnel [[si` then `te:1930]] which…`, and no regex
        on the client can reliably put those back together."""
        scanner = MarkerScanner()
        released = scanner.feed("a tunnel [[si") + scanner.feed("te:1930]] which floods")
        released += scanner.flush()
        assert released == "a tunnel [[site:1930]] which floods"
        assert scanner.text == released

    def test_nothing_is_lost_when_a_delta_ends_on_a_bracket(self) -> None:
        scanner = MarkerScanner()
        out = scanner.feed("see [") + scanner.feed("[site:81]]") + scanner.flush()
        assert out == "see [[site:81]]"

    def test_plain_text_is_released_immediately(self) -> None:
        scanner = MarkerScanner()
        assert scanner.feed("no markers here") == "no markers here"

    def test_a_lone_bracket_is_eventually_released(self) -> None:
        scanner = MarkerScanner()
        out = scanner.feed("a [ b") + scanner.flush()
        assert out == "a [ b"


class TestLedger:
    def test_an_uncited_marker_is_reported_as_unverified(self, fixture_db: Path) -> None:
        """A hallucinated citation must not become a live link to a 404 on
        somebody else's website."""
        from matienzo.db.connect import connect

        connection = connect(fixture_db, read_only=True)
        try:
            ledger = Ledger()
            ledger.record(connection, (1930,), tool="get_site")
            report = ledger.report("real [[site:1930]] and invented [[site:9999]]")
        finally:
            connection.close()
        assert report.cited == (1930,)
        assert report.unverified == (9999,)

    def test_a_site_is_announced_once_however_many_tools_return_it(self, fixture_db: Path) -> None:
        from matienzo.db.connect import connect

        connection = connect(fixture_db, read_only=True)
        try:
            ledger = Ledger()
            first = ledger.record(connection, (1930, 81), tool="search_sites")
            second = ledger.record(connection, (1930,), tool="get_site")
        finally:
            connection.close()
        assert len(first) == 2
        assert second == []
