"""Shared request plumbing.

The rate-limit check lives here rather than in middleware because it has to
happen *before* the response starts. A refusal delivered as an SSE `error` event
inside a 200 response is invisible to every HTTP-level tool between us and the
visitor — a load balancer, a monitoring probe, a client library's retry logic
all see success. So the limiter runs as a dependency and raises a real 429 with
a real `Retry-After`.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, Response

from matienzo.web.execute import Executor
from matienzo.web.sessions import budget, store
from matienzo.web.settings import Settings

#: The cookie a single-conversation portal used to hold. Still read, never
#: written: it is what `store.adopt` needs to hand a conversation started before
#: the thread list existed to the visitor token that replaces it.
SESSION_COOKIE = "matienzo_session"

#: The cookie holding the opaque visitor token. One per browser, and the thing a
#: thread list is scoped to. Conversation ids travel in the request instead,
#: because a cookie can only name one conversation at a time.
VISITOR_COOKIE = "matienzo_visitor"

#: What one request of each kind costs against an address's bucket. A search is
#: cheap for us, so it should not consume a whole question's worth of allowance.
CHAT_COST = 1.0
SEARCH_COST = 0.2


def settings_of(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def executor_of(request: Request) -> Executor:
    return cast(Executor, request.app.state.executor)


@contextmanager
def open_sessions(request: Request) -> Iterator[sqlite3.Connection]:
    """A sessions connection scoped to one unit of work.

    Per-request rather than one for the process, for two reasons that both bite.
    A `sqlite3.Connection` is bound to the thread that created it, and requests
    do not all run on the thread the lifespan ran on. And the spend ledger uses
    explicit `BEGIN IMMEDIATE`, which two threads sharing a connection would
    interleave into each other's transactions. Separate connections over WAL
    give the isolation the protocol assumes, and opening one costs microseconds.
    """
    connection = store.connect(request.app.state.sessions_path)
    try:
        yield connection
    finally:
        connection.close()


def sessions_of(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency form of `open_sessions`."""
    with open_sessions(request) as connection:
        yield connection


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is asking, as far as the portal is willing to know."""

    ip_hash: str
    session_id: str | None


def caller_of(request: Request) -> Caller:
    settings = settings_of(request)
    address = budget.client_address(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
        settings.trusted_proxy_hops,
    )
    return Caller(
        ip_hash=budget.hash_ip(address, settings.ip_salt),
        session_id=request.cookies.get(SESSION_COOKIE),
    )


@dataclass(frozen=True, slots=True)
class Visitor:
    """Whose thread list this is."""

    id: str
    minted: bool
    """Whether the token was invented for this request and still has to be sent
    back. Kept as a flag rather than issuing the cookie here because a dependency
    does not hold the response — see `issue`."""


def visitor_of(request: Request) -> Visitor:
    """Read the visitor token, inventing one if the browser has none.

    Deliberately does no database work: a token names a visitor whether or not
    any row mentions it yet, and the first conversation is what creates the first
    row. A visitor who only ever reads an empty thread list writes nothing.
    """
    existing = request.cookies.get(VISITOR_COOKIE)
    if existing:
        return Visitor(id=existing, minted=False)
    return Visitor(id=store.new_visitor_id(), minted=True)


def issue(response: Response, visitor: Visitor, *, settings: Settings) -> None:
    """Send a freshly minted visitor token back, if there is one to send."""
    if not visitor.minted:
        return
    response.set_cookie(
        VISITOR_COOKIE,
        visitor.id,
        max_age=settings.session_ttl_days * 86_400,
        httponly=True,
        samesite="lax",
        secure=True,
    )


def _limit(request: Request, caller: Caller, cost: float) -> None:
    settings = settings_of(request)
    with open_sessions(request) as connection:
        allowance = budget.check_rate(
            connection,
            caller.ip_hash,
            settings=settings,
            now=time.time(),
            cost=cost,
        )
    if not allowance.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment.",
            headers={"Retry-After": str(allowance.retry_after_seconds)},
        )


def rate_limited_chat(request: Request, caller: Annotated[Caller, Depends(caller_of)]) -> Caller:
    _limit(request, caller, CHAT_COST)
    return caller


def rate_limited_search(request: Request, caller: Annotated[Caller, Depends(caller_of)]) -> Caller:
    _limit(request, caller, SEARCH_COST)
    return caller
