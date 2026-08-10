"""The wire format between the portal and the browser.

Server-Sent Events framing, but delivered as the body of a POST and read with
`fetch` rather than with `EventSource`. That combination is deliberate:
`EventSource` cannot POST, so the question would have to travel in the query
string and end up in every proxy access log along the way. WebSockets would buy
bidirectionality nothing here needs, at the cost of an HTTP upgrade through the
reverse proxy. Keeping SSE's framing gives named event types and comment
heartbeats, which is what makes a stalled stream visible and a test assertable.

The event vocabulary is a contract with the SPA, so it lives here rather than
being spelled inline at each `yield`. `seq` is monotonic per response so a
client can tell "nothing has happened yet" from "I missed something".
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final, Literal

#: Response headers that keep a stream a stream. Every one of these is load
#: bearing behind a reverse proxy: without `X-Accel-Buffering` nginx buffers the
#: whole response and "streaming" silently becomes one blob at the end, and
#: without `no-transform` some CDNs re-chunk it into the same failure.
STREAM_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

EventName = Literal[
    "start",
    "thinking",
    "tool_use",
    "tool_result",
    "source",
    "delta",
    "citations",
    "usage",
    "notice",
    "done",
    "error",
]

#: Terminal events. Exactly one of these ends every response; the SPA uses that
#: to decide when to re-enable its composer, so emitting neither hangs the UI.
TERMINAL: Final[frozenset[str]] = frozenset({"done", "error"})

#: Error codes the SPA is expected to recognise. `daily_cap` is deliberately not
#: here — running out of budget mid-answer is delivered as a `notice` followed by
#: a normal `done`, because the text already streamed is still a real answer.
ErrorCode = Literal[
    "rate_limited", "refusal", "iteration_limit", "timeout", "upstream", "internal"
]


@dataclass(frozen=True, slots=True)
class Event:
    name: EventName
    data: dict[str, Any]

    def encode(self) -> str:
        return f"event: {self.name}\ndata: {json.dumps(self.data, separators=(',', ':'))}\n\n"


class Stream:
    """Numbers events as they are produced.

    A counter rather than a plain generator because `seq` has to be assigned at
    emit time: several producers contribute to one response (the agent loop, the
    provenance ledger, the budget), and a client that sees a gap should be able
    to tell that it did.
    """

    def __init__(self) -> None:
        self._seq = 0

    def event(self, name: EventName, /, **data: Any) -> Event:
        """Positional-only, because several payloads carry their own `name` —
        a tool has one, and so does a site."""
        event = Event(name=name, data={"seq": self._seq, **data})
        self._seq += 1
        return event

    def start(self, *, session_id: str, turn_id: int, mode: str, model: str | None) -> Event:
        return self.event(
            "start", session_id=session_id, turn_id=turn_id, mode=mode, model=model
        )

    def delta(self, text: str) -> Event:
        return self.event("delta", text=text)

    def notice(self, code: str, message: str) -> Event:
        return self.event("notice", code=code, message=message)

    def error(self, code: ErrorCode, message: str, **extra: Any) -> Event:
        return self.event("error", code=code, message=message, **extra)

    def done(self, *, stop_reason: str, truncated: bool = False) -> Event:
        return self.event("done", stop_reason=stop_reason, truncated=truncated)


def keepalive() -> str:
    """An SSE comment. Ignored by every client, and the thing that stops an idle
    proxy from closing a connection during a slow tool call."""
    return ": keepalive\n\n"


async def encode(events: AsyncIterator[Event]) -> AsyncIterator[str]:
    async for event in events:
        yield event.encode()
