"""The agentic loop.

Written by hand rather than with the SDK's tool runner, for four reasons that
all point the same way. Every turn's content blocks must be persisted exactly as
they arrived, and the runner keeps its own copy of the message array without
exposing it — so the documented workaround is to mirror the history yourself,
which is the loop. The spend cap has to be enforced between iterations, which is
precisely the seam the runner owns. `pause_turn` is not auto-resumed, and its
failure mode is a silently truncated answer rather than an error. And it is a
beta surface in a project whose core install has five dependencies.

The loop is about a hundred lines. The interesting parts are the ones that are
not obvious: results from parallel tool calls go back in a *single* user message
(splitting them teaches the model to stop making parallel calls), `stop_reason`
is checked before `content` is read (a refusal has no content to read), and the
budget reservation is released in a `finally` (a visitor who closes the tab would
otherwise hold a slice of the day's budget until midnight).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Any, Final

import anyio

from matienzo import tools
from matienzo.web import prompt, sse
from matienzo.web.execute import Executor, ToolRun
from matienzo.web.provenance import Ledger
from matienzo.web.sessions import budget
from matienzo.web.sessions.store import Source
from matienzo.web.settings import Settings

#: Built once, in the registry's fixed order. Rebuilding this per request — or
#: reordering it — changes the bytes ahead of the whole prompt and invalidates
#: the cache on every request.
ANTHROPIC_TOOLS: Final[list[dict[str, Any]]] = [
    {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
    for spec in tools.REGISTRY
]

SYSTEM: Final[list[dict[str, Any]]] = [
    {
        "type": "text",
        "text": prompt.SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }
]

#: How many times a paused turn may be resumed before we give up. Server-side
#: tools are not in use so this should never fire; it is here because the
#: alternative to bounding it is an unbounded loop on a public endpoint.
MAX_PAUSE_RESUMES: Final = 3


@dataclass(slots=True)
class Answer:
    """What the loop produced, for the caller to persist."""

    turns: list[dict[str, Any]] = field(default_factory=list)
    """Every turn the exchange produced, in order and with its role.

    In order, and *not* flattened, because the alternation is load-bearing: an
    assistant turn holding a `tool_use` must be followed by a user turn holding
    the matching `tool_result`, or the next request is rejected. Storing the
    assistant blocks alone would persist a conversation the API will not accept
    back — which is exactly what the first version of this did.
    """

    sources: list[Source] = field(default_factory=list)
    text: str = ""
    stop_reason: str = "end_turn"
    cost_micros: int = 0
    truncated: bool = False


async def run_agent(
    client: Any,
    *,
    messages: list[dict[str, Any]],
    executor: Executor,
    sessions: sqlite3.Connection,
    settings: Settings,
    stream: sse.Stream,
    ledger: Ledger,
    answer: Answer,
    session_id: str,
) -> AsyncIterator[sse.Event]:
    """Drive the conversation, yielding events as they happen.

    `client` is a parameter rather than a module global so the whole loop can be
    exercised against a scripted fake — the alternative is testing an agent by
    calling a paid API, which is neither fast nor deterministic.
    """
    from matienzo.web.provenance import MarkerScanner

    corpus = executor.open()
    resumes = 0
    try:
        for iteration in range(settings.max_tool_iterations):
            reservation = budget.reserve(sessions, settings=settings, now=datetime.now(UTC))
            if not reservation.granted:
                yield stream.notice(
                    "daily_cap",
                    "The daily budget for answers has been reached. "
                    "Search still works, and the budget resets at midnight UTC.",
                )
                answer.stop_reason = "daily_cap"
                return

            scanner = MarkerScanner()
            blocks: list[dict[str, Any]] = []
            usage = budget.Usage()
            stop_reason = "end_turn"

            try:
                async with client.beta.messages.stream(
                    model=settings.model,
                    max_tokens=settings.max_tokens,
                    system=SYSTEM,
                    tools=ANTHROPIC_TOOLS,
                    output_config={"effort": settings.effort},
                    thinking={"type": "adaptive", "display": "summarized"},
                    messages=sanitise(messages),
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as response:
                    async for event in response:
                        for produced in _translate(event, stream, scanner):
                            yield produced

                    final = await response.get_final_message()
            except Exception as error:
                budget.release(sessions, reservation)
                yield stream.error("upstream", f"{type(error).__name__}: {error}")
                answer.stop_reason = "error"
                return

            if tail := scanner.flush():
                yield stream.delta(tail)

            usage = _usage_of(final)
            stop_reason = getattr(final, "stop_reason", "end_turn") or "end_turn"
            cost = budget.reconcile(
                sessions,
                reservation,
                settings=settings,
                usage=usage,
                now=datetime.now(UTC),
                session_id=session_id,
                iteration=iteration,
                stop_reason=stop_reason,
            )
            answer.cost_micros += cost
            answer.text += scanner.text

            # Checked before `final.content` is touched: a refusal caught before
            # any output has an empty content list, so reading content[0] here
            # is the difference between a message and a crash.
            if stop_reason == "refusal":
                yield stream.error(
                    "refusal",
                    "I can't answer that one. Try rephrasing, or ask about the caves directly.",
                )
                answer.stop_reason = "refusal"
                return

            blocks = [_as_dict(block) for block in final.content]
            assistant_turn = {"role": "assistant", "content": blocks}
            answer.turns.append(assistant_turn)
            messages.append(assistant_turn)

            if stop_reason == "pause_turn":
                resumes += 1
                if resumes > MAX_PAUSE_RESUMES:
                    yield stream.error("upstream", "The answer kept pausing; giving up.")
                    answer.stop_reason = "pause_turn"
                    return
                continue

            if stop_reason == "max_tokens":
                answer.truncated = True
                answer.stop_reason = stop_reason
                break

            if stop_reason != "tool_use":
                answer.stop_reason = stop_reason
                break

            calls = [block for block in blocks if block.get("type") == "tool_use"]
            for call in calls:
                yield stream.event(
                    "tool_use",
                    id=call.get("id"),
                    name=call.get("name"),
                    args=call.get("input") or {},
                )

            # Indexed rather than appended: the tasks finish in whatever order
            # their queries do, and the results have to go back in the order the
            # model asked for them.
            slots: list[ToolRun | None] = [None] * len(calls)

            async with anyio.create_task_group() as group:
                for index, call in enumerate(calls):
                    group.start_soon(_run_one, executor, slots, index, call)

            results: list[ToolRun] = [run for run in slots if run is not None]

            for run in results:
                for source in ledger.record(corpus, run.site_numbers, tool=run.name):
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
                yield stream.event(
                    "tool_result",
                    id=run.tool_use_id,
                    name=run.name,
                    ok=not run.is_error,
                    ms=run.elapsed_ms,
                    summary=run.summary(),
                    site_numbers=list(run.site_numbers),
                )

            # One user message carrying every result. Splitting them across
            # messages silently teaches the model to stop calling tools in
            # parallel, which costs a round trip per tool from then on.
            tool_results = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": run.tool_use_id,
                        "content": _render(run.payload),
                        "is_error": run.is_error,
                    }
                    for run in results
                ],
            }
            messages.append(tool_results)
            answer.turns.append(tool_results)
        else:
            answer.stop_reason = "iteration_limit"
            yield stream.error(
                "iteration_limit",
                "I ran out of search steps before finishing. Try a narrower question.",
            )
            return
    finally:
        corpus.close()


async def _run_one(
    executor: Executor, slots: list[ToolRun | None], index: int, call: dict[str, Any]
) -> None:
    """One tool call, writing into its reserved slot.

    A module-level function rather than a closure over the loop body: the tasks
    all finish before the next iteration, but a closure over a loop variable is
    a shape worth not writing even when this instance of it is safe.
    """
    slots[index] = await executor.run(
        str(call.get("name")),
        dict(call.get("input") or {}),
        tool_use_id=str(call.get("id")),
    )


def _translate(event: Any, stream: sse.Stream, scanner: Any) -> list[sse.Event]:
    """Map one SDK stream event onto zero or more of ours."""
    kind = getattr(event, "type", None)

    if kind == "content_block_delta":
        delta = getattr(event, "delta", None)
        delta_type = getattr(delta, "type", None)
        if delta_type == "text_delta":
            if released := scanner.feed(getattr(delta, "text", "")):
                return [stream.delta(released)]
            return []
        if delta_type == "thinking_delta" and (text := getattr(delta, "thinking", "")):
            return [stream.event("thinking", text=text)]
    return []


def _usage_of(final: Any) -> budget.Usage:
    usage = getattr(final, "usage", None)
    if usage is None:
        return budget.Usage()
    return budget.Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


@cache
def _input_fields() -> dict[str, frozenset[str]]:
    """Which keys each block type is allowed to carry *on the way in*.

    Response blocks and request blocks are not the same shape, and the
    difference is not cosmetic. A `text` block comes back as the SDK's
    `ParsedTextBlock`, which carries `parsed_output` — a field that exists only
    on output and that the API rejects with a 400 if you send it back.

    Derived from the SDK's own `*BlockParam` TypedDicts rather than from a list
    of fields to strip. Those types *are* the input schema, so this keeps
    working when the SDK adds an output-only field we have never heard of —
    which is exactly how this was found.
    """
    from anthropic.types import (
        RedactedThinkingBlockParam,
        TextBlockParam,
        ThinkingBlockParam,
        ToolResultBlockParam,
        ToolUseBlockParam,
    )

    return {
        "text": frozenset(TextBlockParam.__annotations__),
        "tool_use": frozenset(ToolUseBlockParam.__annotations__),
        "tool_result": frozenset(ToolResultBlockParam.__annotations__),
        "thinking": frozenset(ThinkingBlockParam.__annotations__),
        "redacted_thinking": frozenset(RedactedThinkingBlockParam.__annotations__),
    }


def sanitise(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every message, with each block trimmed to what the API accepts on input.

    Applied here, at the boundary, rather than only where blocks are created —
    because blocks reach this point from two directions and only one of them is
    ours. The loop's own blocks come from `_as_dict`; the rest are replayed out
    of `sessions.db`, where they were written by whatever version of this code
    was running at the time. A store that keeps blocks verbatim, which is the
    right call for replay fidelity, is also a store that will hand back
    yesterday's mistakes.

    That is not hypothetical: rows written before `_as_dict` learned to strip
    `parsed_output` sat in the database and kept failing every later request in
    those conversations, long after the serialisation itself was fixed.
    """
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            cleaned.append({**message, "content": [_as_dict(block) for block in content]})
        else:
            cleaned.append(message)
    return cleaned


def _as_dict(block: Any) -> dict[str, Any]:
    """One content block as a plain dict the API will accept back.

    Two things happen here. The block is serialised through the SDK rather than
    field-by-field, because a thinking block is rejected if it comes back
    modified and "modified" includes dropping a field we did not know to keep.
    Then output-only fields are removed, because sending one back is a 400 —
    see `_input_fields`.

    Block types we do not recognise pass through untouched. Guessing at the
    shape of something new is worse than forwarding it: the API will say if it
    is wrong, whereas a silent trim would not.
    """
    raw: dict[str, Any]
    if isinstance(block, dict):
        raw = dict(block)
    else:
        for method in ("model_dump", "to_dict", "dict"):
            if callable(dump := getattr(block, method, None)):
                raw = dict(dump())
                break
        else:
            raw = {
                "type": getattr(block, "type", "text"),
                "text": getattr(block, "text", ""),
            }

    allowed = _input_fields().get(str(raw.get("type")))
    if allowed is None:
        return raw
    # `None` is dropped as well as unknown keys: the SDK fills absent optional
    # fields with null, and `citations: null` is noise on every text block we
    # ever replay.
    return {key: value for key, value in raw.items() if key in allowed and value is not None}


def _render(payload: Any) -> str:
    import json

    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)
