"""Conversation storage and replay.

The awkward part of this module is `history`, and it is worth saying why up
front. Replaying a conversation into the Anthropic messages array is not
"select the last N rows": the array has structural rules the API enforces with a
400, and the obvious implementations break all of them. It must begin on a user
turn. A `tool_use` block must be followed by its `tool_result`, so a truncation
that lands between them is invalid. Thinking blocks may be replayed only to the
model that produced them, and only unmodified.

So truncation happens at question boundaries — user turns that are pure text —
rather than at a turn count. That is a coarser cut than counting tokens, and it
is the right one here: a portal question is short, and the cached prefix carries
the cost that actually matters.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

SCHEMA_PATH: Final = Path(__file__).parent / "schema.sql"

#: How many turns of history to replay. Deliberately small: this is a public
#: question-and-answer portal, not a long-running agent session, and every extra
#: turn is tool results paid for on every subsequent request.
DEFAULT_MAX_TURNS: Final = 6

#: Longest opening question kept as a thread title.
TITLE_CHARS: Final = 60


@dataclass(frozen=True, slots=True)
class Source:
    """A site a tool actually returned.

    Recorded per turn so that rendering an old conversation does not mean
    re-running its searches — and so that a citation can be checked against what
    the corpus returned rather than against what the model said it returned.
    """

    site_number: int
    name: str | None
    area: str | None
    url: str
    excerpt: str
    first_seen_tool: str


def connect(path: Path) -> sqlite3.Connection:
    """Open the sessions database, creating it if absent.

    Unlike the corpus database there is no rebuild-from-source option here, so
    the schema is written with `IF NOT EXISTS` and applied on every open.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return connection


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_session_id() -> str:
    """An opaque 128-bit token. Not a guessable integer: it is the only thing
    standing between one visitor's conversation and another's."""
    return secrets.token_urlsafe(16)


def ensure_session(
    connection: sqlite3.Connection, session_id: str | None, *, ip_hash: str | None = None
) -> str:
    """Return a live session id, creating or touching the row behind it.

    An unknown id is treated as a new session rather than an error. Cookies
    outlive the retention window, so "your session expired" is a routine event
    and not one worth showing anybody.
    """
    now = _now()
    if session_id:
        row = connection.execute(
            "SELECT session_id FROM session WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE session SET last_seen_at = ? WHERE session_id = ?", (now, session_id)
            )
            return session_id

    session_id = new_session_id()
    connection.execute(
        "INSERT INTO session (session_id, created_at, last_seen_at, ip_hash)"
        " VALUES (?, ?, ?, ?)",
        (session_id, now, now, ip_hash),
    )
    return session_id


def append_turn(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    role: str,
    blocks: Sequence[dict[str, Any]],
    model: str | None = None,
    stop_reason: str | None = None,
    cost_micros: int = 0,
    sources: Iterable[Source] = (),
) -> int:
    """Persist one turn and its blocks, returning the turn id."""
    ordinal = (
        connection.execute(
            "SELECT coalesce(max(ordinal), -1) + 1 AS next FROM turn WHERE session_id = ?",
            (session_id,),
        ).fetchone()["next"]
        or 0
    )
    cursor = connection.execute(
        "INSERT INTO turn (session_id, ordinal, role, created_at, model, stop_reason,"
        " cost_micros) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, ordinal, role, _now(), model, stop_reason, cost_micros),
    )
    turn_id = int(cursor.lastrowid or 0)

    connection.executemany(
        "INSERT INTO block (turn_id, ordinal, type, json) VALUES (?, ?, ?, ?)",
        [
            (turn_id, index, str(block.get("type", "text")), json.dumps(block))
            for index, block in enumerate(blocks)
        ],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO source (turn_id, site_number, name, area, url, excerpt,"
        " first_seen_tool) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (turn_id, s.site_number, s.name, s.area, s.url, s.excerpt, s.first_seen_tool)
            for s in sources
        ],
    )

    if ordinal == 0 and role == "user":
        connection.execute(
            "UPDATE session SET title = ? WHERE session_id = ? AND title IS NULL",
            (_title(blocks), session_id),
        )
    return turn_id


def _title(blocks: Sequence[dict[str, Any]]) -> str | None:
    for block in blocks:
        if block.get("type") == "text" and isinstance(text := block.get("text"), str):
            collapsed = " ".join(text.split())
            return collapsed[:TITLE_CHARS] or None
    return None


def history(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    model: str,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> list[dict[str, Any]]:
    """Rebuild the Anthropic messages array for a session.

    Thinking blocks are replayed only when the turn that produced them ran on
    the same model. The API rejects a *modified* thinking block, not one that is
    absent, so dropping them across a model change is the safe direction — and
    keeping them would be the unsafe one.
    """
    turns = connection.execute(
        "SELECT turn_id, role, model FROM turn WHERE session_id = ? ORDER BY ordinal",
        (session_id,),
    ).fetchall()
    if not turns:
        return []

    blocks_by_turn = {
        turn["turn_id"]: [
            json.loads(row["json"])
            for row in connection.execute(
                "SELECT json FROM block WHERE turn_id = ? ORDER BY ordinal",
                (turn["turn_id"],),
            )
        ]
        for turn in turns
    }

    start = _truncation_point(turns, blocks_by_turn, max_turns)
    messages: list[dict[str, Any]] = []
    for turn in turns[start:]:
        blocks = blocks_by_turn[turn["turn_id"]]
        if turn["model"] != model:
            blocks = [b for b in blocks if b.get("type") != "thinking"]
        if blocks:
            messages.append({"role": turn["role"], "content": blocks})
    return messages


def _truncation_point(
    turns: Sequence[sqlite3.Row],
    blocks_by_turn: dict[int, list[dict[str, Any]]],
    max_turns: int,
) -> int:
    """The earliest index we may start from without producing an invalid array.

    Candidates are user turns made only of text — real question boundaries. A
    user turn carrying `tool_result` blocks is the second half of a tool call
    and starting there orphans the `tool_use` that asked for it.
    """
    boundaries = [
        index
        for index, turn in enumerate(turns)
        if turn["role"] == "user"
        and all(b.get("type") == "text" for b in blocks_by_turn[turn["turn_id"]])
    ]
    if not boundaries:
        return len(turns)

    wanted = max(0, len(turns) - max_turns)
    eligible = [index for index in boundaries if index >= wanted]
    return eligible[0] if eligible else boundaries[-1]


def sources_for_turn(connection: sqlite3.Connection, turn_id: int) -> list[Source]:
    return [
        Source(
            site_number=row["site_number"],
            name=row["name"],
            area=row["area"],
            url=row["url"],
            excerpt=row["excerpt"],
            first_seen_tool=row["first_seen_tool"],
        )
        for row in connection.execute(
            "SELECT site_number, name, area, url, excerpt, first_seen_tool FROM source"
            " WHERE turn_id = ? ORDER BY source_id",
            (turn_id,),
        )
    ]


def prune(connection: sqlite3.Connection, *, ttl_days: int, now: datetime | None = None) -> int:
    """Delete sessions past the retention window, returning how many went.

    Retention is stated in the interface, so it has to be enforced somewhere;
    this is that somewhere. Cascades take the turns, blocks and sources with it.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=ttl_days)).isoformat(timespec="seconds")
    cursor = connection.execute("DELETE FROM session WHERE last_seen_at < ?", (cutoff,))
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return cursor.rowcount
