"""The conversation endpoint.

Two producers behind one event schema. The agent path runs the loop; the
search-only path runs a search and says plainly that it did. The SPA gets the
same events either way and branches on one field, which is why the degraded mode
is a mode rather than an error page.

The generator is wrapped so that the turn is persisted and the budget released
even when the visitor closes the tab mid-answer. That case is not exotic — it is
what happens every time somebody reads enough of the answer and moves on — and
skipping it leaks reserved budget until midnight.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from matienzo import embed, search
from matienzo.web import agent as agent_module
from matienzo.web import prompt, sse
from matienzo.web.execute import Executor
from matienzo.web.provenance import Ledger
from matienzo.web.routes.deps import (
    SESSION_COOKIE,
    Caller,
    executor_of,
    open_sessions,
    rate_limited_chat,
    settings_of,
)
from matienzo.web.sessions import budget, store
from matienzo.web.settings import Settings

router = APIRouter(prefix="/api")

#: Sites offered when the portal cannot write an answer.
FALLBACK_RESULTS = 8


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@router.post("/chat")
async def post_chat(
    body: ChatRequest,
    request: Request,
    caller: Annotated[Caller, Depends(rate_limited_chat)],
    settings: Annotated[Settings, Depends(settings_of)],
    executor: Annotated[Executor, Depends(executor_of)],
) -> StreamingResponse:
    with open_sessions(request) as sessions:
        session_id = store.ensure_session(sessions, caller.session_id, ip_hash=caller.ip_hash)
        turn_id = store.append_turn(
            sessions,
            session_id,
            role="user",
            blocks=[{"type": "text", "text": body.question}],
        )

    generator = _respond(
        question=body.question,
        session_id=session_id,
        turn_id=turn_id,
        sessions_path=request.app.state.sessions_path,
        settings=settings,
        executor=executor,
        client=getattr(request.app.state, "client", None),
        limiter=getattr(request.app.state, "stream_limiter", None),
    )

    response = StreamingResponse(
        _with_keepalive(generator, settings.keepalive_seconds),
        media_type="text/event-stream",
        headers=sse.STREAM_HEADERS,
    )
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=settings.session_ttl_days * 86_400,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return response


async def _respond(
    *,
    question: str,
    session_id: str,
    turn_id: int,
    sessions_path: Any,
    settings: Settings,
    executor: Executor,
    client: Any,
    limiter: Any,
) -> AsyncIterator[sse.Event]:
    stream = sse.Stream()
    ledger = Ledger()
    answer = agent_module.Answer()

    # Opened here rather than passed in: this generator outlives the request
    # handler, and the connection has to belong to the task that iterates it.
    sessions = store.connect(sessions_path)

    can_answer = client is not None and settings.can_answer
    mode = "agent" if can_answer else "search_only"

    yield stream.start(
        session_id=session_id,
        turn_id=turn_id,
        mode=mode,
        model=settings.model if can_answer else None,
    )

    try:
        if not can_answer:
            async for event in _search_only(
                question, executor=executor, stream=stream, ledger=ledger, answer=answer
            ):
                yield event
            return

        # The question is already in the history: the route persisted it before
        # opening the stream, so that a visitor who leaves mid-answer still has
        # their question recorded. Appending it here as well sent every question
        # to the model twice.
        messages = store.history(sessions, session_id, model=settings.model)

        async with limiter:
            with anyio.move_on_after(settings.request_timeout_seconds) as scope:
                async for event in agent_module.run_agent(
                    client,
                    messages=messages,
                    executor=executor,
                    sessions=sessions,
                    settings=settings,
                    stream=stream,
                    ledger=ledger,
                    answer=answer,
                    session_id=session_id,
                ):
                    yield event
            if scope.cancelled_caught:
                answer.stop_reason = "timeout"
                yield stream.error("timeout", "That took too long. Try a narrower question.")
                return

        if answer.stop_reason == "daily_cap":
            async for event in _search_only(
                question, executor=executor, stream=stream, ledger=ledger, answer=answer
            ):
                yield event
            return

        if answer.stop_reason in {"refusal", "iteration_limit", "error"}:
            return

        report = ledger.report(answer.text)
        yield stream.event(
            "citations", cited=list(report.cited), unverified=list(report.unverified)
        )
        yield stream.event(
            "usage",
            cost_usd=round(answer.cost_micros / 1_000_000, 6),
            **budget.snapshot(sessions, settings=settings, now=datetime.now(UTC)),
        )
        yield stream.done(stop_reason=answer.stop_reason, truncated=answer.truncated)
    finally:
        try:
            _persist(sessions, session_id, settings=settings, answer=answer)
        finally:
            sessions.close()


async def _search_only(
    question: str,
    *,
    executor: Executor,
    stream: sse.Stream,
    ledger: Ledger,
    answer: agent_module.Answer,
) -> AsyncIterator[sse.Event]:
    """The degraded producer: real results, no synthesis, and it says so."""

    def run(connection: Any) -> list[search.SiteHit]:
        return search.search_sites(
            connection,
            question,
            limit=FALLBACK_RESULTS,
            hybrid=embed.is_available(connection),
        )

    hits = await executor.read(run)
    corpus = executor.open()
    try:
        for source in ledger.record(
            corpus, tuple(hit.site_number for hit in hits), tool="search_sites"
        ):
            answer.sources.append(source)
            yield stream.event(
                "source",
                site_number=source.site_number,
                name=source.name,
                area=source.area,
                url=source.url,
                excerpt=source.excerpt,
                first_seen_tool=source.first_seen_tool,
            )
    finally:
        corpus.close()

    answer.text = prompt.SEARCH_ONLY_ANSWER
    answer.stop_reason = "search_only"
    answer.turns = [
        {"role": "assistant", "content": [{"type": "text", "text": prompt.SEARCH_ONLY_ANSWER}]}
    ]
    yield stream.delta(prompt.SEARCH_ONLY_ANSWER)
    yield stream.event("citations", cited=[], unverified=[])
    yield stream.done(stop_reason="search_only")


def _persist(
    sessions: Any, session_id: str, *, settings: Settings, answer: agent_module.Answer
) -> None:
    """Write every turn the exchange produced, in order.

    Runs from the streaming generator's `finally`, because closing the tab
    part-way through an answer is a normal way to leave rather than an error.

    The alternation is the point. An assistant turn holding a `tool_use` has to
    be followed by a user turn holding the matching `tool_result`, or the *next*
    question in this session replays a conversation the API rejects outright. An
    earlier version flattened all the assistant blocks into a single turn and
    dropped the tool results altogether, which persisted precisely that.
    """
    if not answer.turns:
        return

    last = len(answer.turns) - 1
    for index, turn in enumerate(answer.turns):
        role = str(turn["role"])
        store.append_turn(
            sessions,
            session_id,
            role=role,
            blocks=turn["content"],
            # The model is recorded on assistant turns only; replay uses it to
            # decide whether thinking blocks may be sent back.
            model=settings.model if role == "assistant" else None,
            # Cost and outcome describe the exchange, so they go on its last turn
            # rather than being repeated on every one.
            stop_reason=answer.stop_reason if index == last else None,
            cost_micros=answer.cost_micros if index == last else 0,
            sources=answer.sources if index == last else (),
        )


async def _with_keepalive(events: AsyncIterator[sse.Event], interval: float) -> AsyncIterator[str]:
    """Encode events, emitting an SSE comment whenever the stream goes quiet.

    A multi-hop answer can spend twenty seconds inside tool calls with nothing to
    say. Without a heartbeat that silence is indistinguishable from a dead
    connection, and the reverse proxy's read timeout eventually agrees.
    """
    send, receive = anyio.create_memory_object_stream[sse.Event | None](max_buffer_size=64)

    async def pump() -> None:
        try:
            async for event in events:
                await send.send(event)
        finally:
            await send.send(None)
            send.close()

    async with anyio.create_task_group() as group:
        group.start_soon(pump)
        async with receive:
            while True:
                with anyio.move_on_after(interval) as scope:
                    event = await receive.receive()
                if scope.cancelled_caught:
                    yield sse.keepalive()
                    continue
                if event is None:
                    break
                yield event.encode()


__all__ = ["router"]
