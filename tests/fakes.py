"""A scripted stand-in for the Anthropic client.

The agent loop takes its client as an argument for exactly this reason. Testing
an agent by calling the real API is slow, costs money, and — worst of the three —
is non-deterministic, so the cases actually worth covering here (a refusal, a
paused turn, two tools called at once, a tool that raises) either cannot be
provoked on demand or cannot be provoked at all.

The fake implements only what the loop reads, which is a useful check in itself:
if the loop starts depending on something else, this stops compiling rather than
silently drifting away from what it claims to test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


def text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


def thinking_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="thinking_delta", thinking=text),
    )


def usage(
    input_tokens: int = 1000,
    output_tokens: int = 100,
    cache_read: int = 0,
    cache_write: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def text_block(text: str) -> Any:
    """A text block as the API actually returns one.

    Deliberately the SDK's own `ParsedTextBlock` rather than a tidy dict. Real
    response blocks carry `parsed_output`, an output-only field the API rejects
    with a 400 if it is sent back — and a fake that hands back clean dicts
    cannot catch that. It did not: the loop shipped replaying the field, and the
    first two-turn conversation against the real API failed.
    """
    from anthropic.types import ParsedTextBlock

    return ParsedTextBlock(type="text", text=text)


@dataclass
class Turn:
    """One scripted model response."""

    events: Sequence[Any] = field(default_factory=tuple)
    content: Sequence[Any] = field(default_factory=tuple)
    stop_reason: str = "end_turn"
    usage: SimpleNamespace = field(default_factory=usage)


def say(text: str, *, chunks: Sequence[str] | None = None) -> Turn:
    """A turn that just answers."""
    pieces = list(chunks) if chunks is not None else [text]
    return Turn(
        events=[text_delta(piece) for piece in pieces],
        content=[text_block(text)],
        stop_reason="end_turn",
    )


def call_tools(*calls: tuple[str, dict[str, Any]], preamble: str = "") -> Turn:
    """A turn that asks for one or more tools."""
    blocks: list[Any] = []
    if preamble:
        blocks.append(text_block(preamble))
    blocks += [
        {"type": "tool_use", "id": f"toolu_{index}", "name": name, "input": arguments}
        for index, (name, arguments) in enumerate(calls)
    ]
    return Turn(
        events=[text_delta(preamble)] if preamble else [],
        content=blocks,
        stop_reason="tool_use",
    )


def refuse() -> Turn:
    """A pre-output refusal: `stop_reason` set and nothing in `content`.

    The case that breaks any loop reading `content[0]` before checking why the
    turn stopped.
    """
    return Turn(events=(), content=(), stop_reason="refusal")


def pause() -> Turn:
    return Turn(
        events=[text_delta("working")],
        content=[text_block("working")],
        stop_reason="pause_turn",
    )


class _Stream:
    def __init__(self, turn: Turn) -> None:
        self._turn = turn

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def events() -> AsyncIterator[Any]:
            for event in self._turn.events:
                yield event

        return events()

    async def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            content=list(self._turn.content),
            stop_reason=self._turn.stop_reason,
            usage=self._turn.usage,
        )


class FakeAnthropic:
    """Replays a script of turns, recording the kwargs it was called with."""

    def __init__(self, turns: Sequence[Turn], *, fail_with: Exception | None = None) -> None:
        self._turns: Iterator[Turn] = iter(turns)
        self._fail_with = fail_with
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs: Any) -> _Stream:
        if self._fail_with is not None:
            raise self._fail_with
        self.calls.append(kwargs)
        try:
            return _Stream(next(self._turns))
        except StopIteration:  # pragma: no cover - a script that ran short is a bug
            raise AssertionError("the agent asked for more turns than the script had") from None

    @property
    def messages_sent(self) -> list[list[dict[str, Any]]]:
        """The `messages` array as it stood on each request."""
        return [call["messages"] for call in self.calls]

    async def close(self) -> None:
        return None
