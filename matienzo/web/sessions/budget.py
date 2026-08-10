"""Rate limiting and the spend cap.

Both live in the database rather than in process memory, for the same reason: a
daily cap that resets when the process restarts is not a cap, and a restart is
precisely what happens under the load you were trying to cap.

The spend cap is a reserve-then-reconcile protocol, not a check-then-spend one.
Checking `spent < cap` before opening a stream lets every concurrent request
pass the same check and then all spend — and because usage is unknown until a
turn finishes, that check-then-act window is tens of seconds wide rather than
milliseconds. So each iteration pessimistically reserves what it could possibly
cost, then reconciles against the real usage afterwards. The residual overshoot
is bounded by how many streams may run at once, which is a number the operator
sets rather than a surprise.

Time is passed in rather than read here. Refill and day-rollover are the two
things most worth testing and both are untestable if the clock is implicit.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from matienzo.web.settings import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    Settings,
)

#: What a refused request is told to wait when its bucket is empty but its
#: refill rate would take longer than anyone will sit still for.
MAX_RETRY_AFTER_SECONDS: Final = 300


@dataclass(frozen=True, slots=True)
class Allowance:
    allowed: bool
    retry_after_seconds: int = 0


@dataclass(frozen=True, slots=True)
class Reservation:
    """A claim on the day's budget, held until the turn it covers finishes."""

    day: str
    micros: int
    granted: bool


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def hash_ip(address: str, salt: str) -> str:
    """Salted so the table is not a list of who visited.

    The raw address is never written anywhere; this is the only form that
    reaches the database.
    """
    return hashlib.sha256(f"{salt}{address}".encode()).hexdigest()


def client_address(forwarded_for: str | None, peer: str | None, hops: int) -> str:
    """The client address, read by index from the right of `X-Forwarded-For`.

    Each proxy *appends*, so the rightmost entries are the ones our own
    infrastructure wrote and the leftmost is whatever the client chose to claim.
    Taking `split(",")[0]` — the obvious reading, and the common bug — lets
    anyone forge a fresh identity per request and walk straight past the limit.
    """
    if hops <= 0 or not forwarded_for:
        return peer or "unknown"
    entries = [entry.strip() for entry in forwarded_for.split(",") if entry.strip()]
    if not entries:
        return peer or "unknown"
    return entries[-min(hops, len(entries))]


def check_rate(
    connection: sqlite3.Connection,
    ip_hash: str,
    *,
    settings: Settings,
    now: float,
    cost: float = 1.0,
) -> Allowance:
    """Consume `cost` tokens from an IP's bucket, refilling it lazily first."""
    rate_per_second = settings.rate_per_min / 60.0
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT tokens, updated_at FROM rate_bucket WHERE ip_hash = ?", (ip_hash,)
        ).fetchone()

        if row is None:
            tokens = settings.rate_burst
        else:
            elapsed = max(0.0, now - float(row["updated_at"]))
            tokens = min(settings.rate_burst, float(row["tokens"]) + elapsed * rate_per_second)

        if tokens < cost:
            connection.execute(
                "INSERT INTO rate_bucket (ip_hash, tokens, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (ip_hash) DO UPDATE SET tokens = excluded.tokens,"
                " updated_at = excluded.updated_at",
                (ip_hash, tokens, now),
            )
            connection.execute("COMMIT")
            wait = (cost - tokens) / rate_per_second if rate_per_second else MAX_RETRY_AFTER_SECONDS
            return Allowance(allowed=False, retry_after_seconds=_retry_after(wait))

        connection.execute(
            "INSERT INTO rate_bucket (ip_hash, tokens, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (ip_hash) DO UPDATE SET tokens = excluded.tokens,"
            " updated_at = excluded.updated_at",
            (ip_hash, tokens - cost, now),
        )
        connection.execute("COMMIT")
        return Allowance(allowed=True)
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _retry_after(seconds: float) -> int:
    return max(1, min(MAX_RETRY_AFTER_SECONDS, int(seconds) + 1))


def day_of(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def cost_micros(usage: Usage, settings: Settings) -> int:
    """What one API call cost, in micro-dollars.

    Cache reads and writes are priced off the input rate rather than counted as
    input tokens, because they are not billed at the same multiple — and on this
    workload they are most of the input, since the tool schemas and corpus primer
    are cached on every single request.
    """
    input_price, output_price = settings.price_micros_per_mtok
    total = (
        usage.input_tokens * input_price
        + usage.output_tokens * output_price
        + usage.cache_read_tokens * input_price * CACHE_READ_MULTIPLIER
        + usage.cache_write_tokens * input_price * CACHE_WRITE_MULTIPLIER
    )
    return round(total / 1_000_000)


def estimate_micros(settings: Settings) -> int:
    """The pessimistic reservation for one iteration: a full `max_tokens` of
    output. Deliberately an over-estimate — under-reserving is what lets the cap
    be crossed, and the excess is returned moments later by `reconcile`."""
    _, output_price = settings.price_micros_per_mtok
    return round(settings.max_tokens * output_price / 1_000_000)


def reserve(
    connection: sqlite3.Connection, *, settings: Settings, now: datetime, micros: int | None = None
) -> Reservation:
    """Claim budget for one iteration, or refuse."""
    day = day_of(now)
    amount = estimate_micros(settings) if micros is None else micros

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT OR IGNORE INTO budget_day (day) VALUES (?)", (day,)
        )
        row = connection.execute(
            "SELECT reserved_micros, spent_micros FROM budget_day WHERE day = ?", (day,)
        ).fetchone()
        committed = int(row["reserved_micros"]) + int(row["spent_micros"])

        if committed + amount > settings.daily_cap_micros:
            connection.execute("COMMIT")
            return Reservation(day=day, micros=0, granted=False)

        connection.execute(
            "UPDATE budget_day SET reserved_micros = reserved_micros + ?,"
            " request_count = request_count + 1 WHERE day = ?",
            (amount, day),
        )
        connection.execute("COMMIT")
        return Reservation(day=day, micros=amount, granted=True)
    except Exception:
        connection.execute("ROLLBACK")
        raise


def reconcile(
    connection: sqlite3.Connection,
    reservation: Reservation,
    *,
    settings: Settings,
    usage: Usage,
    now: datetime,
    session_id: str | None = None,
    turn_id: int | None = None,
    iteration: int = 0,
    stop_reason: str | None = None,
) -> int:
    """Release a reservation, record what was actually spent, return the cost."""
    actual = cost_micros(usage, settings)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE budget_day SET reserved_micros = max(0, reserved_micros - ?),"
            " spent_micros = spent_micros + ? WHERE day = ?",
            (reservation.micros, actual, reservation.day),
        )
        connection.execute(
            "INSERT INTO usage_event (at, day, session_id, turn_id, model, iteration,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
            " cost_micros, stop_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now.isoformat(timespec="seconds"),
                reservation.day,
                session_id,
                turn_id,
                settings.model,
                iteration,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                actual,
                stop_reason,
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return actual


def release(connection: sqlite3.Connection, reservation: Reservation) -> None:
    """Give back an unused reservation.

    Called from the streaming generator's `finally`, which is the case that
    matters: a visitor who closes the tab mid-answer would otherwise hold a slice
    of the day's budget until midnight.
    """
    if not reservation.granted or reservation.micros == 0:
        return
    connection.execute(
        "UPDATE budget_day SET reserved_micros = max(0, reserved_micros - ?) WHERE day = ?",
        (reservation.micros, reservation.day),
    )


def snapshot(
    connection: sqlite3.Connection, *, settings: Settings, now: datetime
) -> dict[str, int]:
    """The day's ledger, for `/healthz` and the `usage` event."""
    day = day_of(now)
    row = connection.execute(
        "SELECT reserved_micros, spent_micros, request_count FROM budget_day WHERE day = ?",
        (day,),
    ).fetchone()
    reserved = int(row["reserved_micros"]) if row else 0
    spent = int(row["spent_micros"]) if row else 0
    cap = settings.daily_cap_micros
    return {
        "cap_micros": cap,
        "reserved_micros": reserved,
        "spent_micros": spent,
        "remaining_micros": max(0, cap - reserved - spent),
        "remaining_pct": round(100 * max(0, cap - reserved - spent) / cap) if cap else 0,
        "request_count": int(row["request_count"]) if row else 0,
    }
