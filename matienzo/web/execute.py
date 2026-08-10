"""Running corpus tools from an async server.

Every tool body is synchronous SQLite, and some of them reach ONNX inference to
embed a query. Calling one directly from the event loop stalls every concurrent
stream on the process — including their keepalive comments, so the visible
symptom is not "slow" but "everyone's connection dropped". So everything goes
through a worker thread.

Two limits rather than one, because the two costs are different. The general
thread limit stops a burst of questions from opening more SQLite connections
than the box wants; the embedding limit is narrower and stricter, because ONNX
releases the GIL and will genuinely use every core it is given. Unbounded
parallelism there makes tail latency worse than a short queue does.

The connection is opened *inside* the worker thread and closed there too.
`sqlite3.Connection` objects are bound to the thread that created them by
default, and a thread pool hands out arbitrary threads, so anything longer-lived
than one call would need thread-affinity bookkeeping to buy back a few
microseconds.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from starlette.concurrency import run_in_threadpool

from matienzo import tools
from matienzo.db.connect import connect
from matienzo.web.settings import Settings


@dataclass(frozen=True, slots=True)
class ToolRun:
    """One completed tool call, as both the model and the browser need it."""

    name: str
    tool_use_id: str
    payload: Any
    site_numbers: tuple[int, ...]
    is_error: bool
    elapsed_ms: int

    def summary(self) -> str:
        """A short human phrase for the browser. The model gets the payload; the
        UI gets this, because a tool result rendered in full is a wall of JSON."""
        if self.is_error:
            return "failed"
        if isinstance(self.payload, list):
            return f"{len(self.payload)} result{'s' if len(self.payload) != 1 else ''}"
        if isinstance(self.payload, dict):
            if "error" in self.payload:
                return str(self.payload["error"])[:120]
            if "row_count" in self.payload:
                return f"{self.payload['row_count']} rows"
            if "name" in self.payload:
                return str(self.payload["name"])
        return "done"


class Executor:
    """Runs tools against the read-only corpus, under the two limits above."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._embedding_limiter = anyio.CapacityLimiter(settings.embedding_limit)

    async def run(self, name: str, arguments: dict[str, Any], *, tool_use_id: str) -> ToolRun:
        """Execute one tool, converting any failure into a result rather than an
        exception.

        A tool that raises is something the model can recover from — it can try
        different arguments, or a different tool — but only if it is told. An
        exception that escapes here would end the whole answer instead.
        """
        spec = tools.BY_NAME.get(name)
        started = time.monotonic()
        if spec is None:
            return ToolRun(
                name=name,
                tool_use_id=tool_use_id,
                payload={"error": f"no tool named {name!r}"},
                site_numbers=(),
                is_error=True,
                elapsed_ms=0,
            )

        try:
            if spec.needs_embeddings:
                async with self._embedding_limiter:
                    outcome = await run_in_threadpool(self._call, spec, arguments)
            else:
                outcome = await run_in_threadpool(self._call, spec, arguments)
        except Exception as error:
            return ToolRun(
                name=name,
                tool_use_id=tool_use_id,
                payload={"error": f"{type(error).__name__}: {error}"},
                site_numbers=(),
                is_error=True,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )

        return ToolRun(
            name=name,
            tool_use_id=tool_use_id,
            payload=outcome.payload,
            site_numbers=outcome.site_numbers,
            is_error=False,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    def _call(self, spec: tools.ToolSpec, arguments: dict[str, Any]) -> tools.Outcome:
        connection = self.open()
        try:
            return tools.call(spec, connection, arguments)
        finally:
            connection.close()

    def open(self) -> sqlite3.Connection:
        return connect(self._settings.db_path, read_only=True)

    async def read[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run an arbitrary read against the corpus on a worker thread.

        The search endpoints need this too — they are the same synchronous
        SQLite as the tools, just without a model asking for them.
        """

        def call() -> T:
            connection = self.open()
            try:
                return fn(connection, *args, **kwargs)
            finally:
                connection.close()

        return await run_in_threadpool(call)
