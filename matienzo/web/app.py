"""The portal application.

A third adapter over `matienzo.search`, alongside the CLI and the MCP server,
and held to the same rule: no query logic lives here.

Two things happen at startup that are easy to leave out and expensive to leave
out. The embedding model is warmed, because fastembed loads ~130 MB of ONNX
lazily and otherwise the first visitor pays for it on a worker thread. And the
thread limit is lowered from anyio's default of 40, which is a sensible number
for waiting on sockets and a bad one for forty simultaneous ONNX inferences on a
small virtual machine.

Neither the corpus nor the model being unavailable is fatal. The portal has a
useful degraded mode — search without synthesis — and booting into it beats
refusing to boot, as long as `/healthz` says so plainly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from matienzo import __version__
from matienzo.web.execute import Executor
from matienzo.web.routes import chat, health, search, site
from matienzo.web.sessions import store
from matienzo.web.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.threadpool_limit

    app.state.executor = Executor(settings)
    # The schema is applied once here; connections themselves are per-request
    # (see routes/deps.open_sessions for why).
    app.state.sessions_path = settings.sessions_db_path
    store.connect(settings.sessions_db_path).close()
    app.state.embeddings_ready = await _warm_embeddings()
    app.state.corpus_ready = await _check_corpus(app.state.executor)
    app.state.client = _anthropic_client(settings)
    app.state.stream_limiter = anyio.Semaphore(settings.max_concurrent_streams)

    try:
        yield
    finally:
        if app.state.client is not None:
            await app.state.client.close()


async def _warm_embeddings() -> bool:
    """Load the embedding model now rather than on someone's first question.

    Returns whether it worked instead of raising: a portal serving keyword-only
    results is degraded, not broken, and the honest place to say so is `/healthz`.
    """
    try:
        from matienzo import embed

        await run_in_threadpool(embed.embed_query, "warmup")
    except Exception:
        return False
    return True


async def _check_corpus(executor: Executor) -> bool:
    """Prove the corpus opens read-only before accepting traffic.

    This is the check that catches a WAL database shipped without a checkpoint:
    a read-only connection cannot create the `-shm` file it would need, and the
    resulting error says nothing about WAL. Failing at boot is much cheaper than
    discovering it per request.
    """
    try:
        await executor.read(lambda c: c.execute("SELECT count(*) FROM site").fetchone())
    except sqlite3.Error:
        return False
    return True


def _anthropic_client(settings: Settings) -> Any:
    """One client for the process. It owns a connection pool, so building one
    per request would quietly defeat the pool it exists to provide."""
    if not settings.can_answer:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.api_key)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Takes its settings as an argument so tests can construct a portal pointed at
    a fixture corpus and a temporary sessions database without touching the
    process environment.
    """
    settings = settings or Settings.from_env()

    app = FastAPI(
        title="Matienzo Caves",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["content-type"],
        )

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(site.router)
    app.include_router(chat.router)

    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=8000,
        # One worker. SQLite reads scale fine inside a process, and the spend
        # ledger lives in the database rather than in memory precisely so that
        # raising this later does not silently multiply the daily cap.
        workers=1,
    )


if __name__ == "__main__":
    main()
