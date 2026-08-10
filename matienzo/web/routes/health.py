"""Liveness, and an honest account of what is degraded.

The portal has three states, not two: answering, searching-but-not-answering,
and broken. Collapsing the middle one into either neighbour is what makes a
silent degradation possible — a portal serving keyword-only results because
fastembed failed to load looks exactly like a healthy one from the outside.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from matienzo import __version__
from matienzo.web.routes.deps import sessions_of, settings_of
from matienzo.web.sessions import budget
from matienzo.web.settings import Settings

router = APIRouter()


@router.get("/healthz")
def healthz(
    request: Request,
    settings: Annotated[Settings, Depends(settings_of)],
    sessions: Annotated[sqlite3.Connection, Depends(sessions_of)],
) -> dict[str, Any]:
    corpus_ready = bool(getattr(request.app.state, "corpus_ready", False))
    embeddings_ready = bool(getattr(request.app.state, "embeddings_ready", False))
    return {
        "version": __version__,
        "ok": corpus_ready,
        "corpus": corpus_ready,
        "embeddings": embeddings_ready,
        "mode": "agent" if settings.can_answer else "search_only",
        "model": settings.model if settings.can_answer else None,
        "budget": budget.snapshot(sessions, settings=settings, now=datetime.now(UTC)),
    }
