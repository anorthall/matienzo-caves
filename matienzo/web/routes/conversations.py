"""The thread list, and reading a conversation back.

Three endpoints around one idea: the browser holds a visitor token, the server
holds the conversations, and a conversation id is a capability the visitor
either has or does not. Nothing here trusts an id on its own — every read and
every delete is scoped by `visitor_id` in the WHERE clause rather than checked
and then acted on, so there is no window between the two.

Reading is separate from `/api/chat` on purpose. Replaying a stored conversation
through the streaming endpoint would mean re-running its searches to rebuild the
sources panel, at the cost of a paid model call to render text the database
already has.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel

from matienzo.web.routes.deps import (
    SESSION_COOKIE,
    Visitor,
    issue,
    open_sessions,
    rate_limited_search,
    settings_of,
    visitor_of,
)
from matienzo.web.sessions import store
from matienzo.web.settings import Settings

router = APIRouter(prefix="/api", dependencies=[Depends(rate_limited_search)])


class ConversationSummary(BaseModel):
    id: str
    title: str | None
    created_at: str
    updated_at: str


class TranscriptTool(BaseModel):
    id: str
    name: str
    args: dict[str, Any]
    ok: bool | None = None


class TranscriptSource(BaseModel):
    site_number: int
    name: str | None
    area: str | None
    url: str
    excerpt: str


class TranscriptExchange(BaseModel):
    question: str
    answer: str
    tools: list[TranscriptTool]
    sources: list[TranscriptSource]


class Conversation(BaseModel):
    id: str
    title: str | None
    exchanges: list[TranscriptExchange]


@router.get("/conversations")
def list_conversations(
    request: Request,
    response: Response,
    visitor: Annotated[Visitor, Depends(visitor_of)],
    settings: Annotated[Settings, Depends(settings_of)],
) -> list[ConversationSummary]:
    """Every conversation this browser may open, newest first.

    Also where a visitor token is issued, because this is the first call the SPA
    makes. A browser arriving with the old single-conversation cookie has that
    conversation adopted into the new token here, so a deploy does not look like
    the portal forgot everything.
    """
    with open_sessions(request) as sessions:
        if visitor.minted:
            store.adopt(sessions, request.cookies.get(SESSION_COOKIE), visitor.id)
        threads = store.threads(sessions, visitor.id)

    issue(response, visitor, settings=settings)
    return [
        ConversationSummary(
            id=thread.session_id,
            title=thread.title,
            created_at=thread.created_at,
            updated_at=thread.last_seen_at,
        )
        for thread in threads
    ]


@router.get("/conversations/{conversation_id}")
def get_conversation(
    request: Request,
    conversation_id: Annotated[str, Path(min_length=1, max_length=64)],
    visitor: Annotated[Visitor, Depends(visitor_of)],
) -> Conversation:
    with open_sessions(request) as sessions:
        if not store.owns(sessions, conversation_id, visitor.id):
            # A 404 rather than a 403: whether an id exists is not this
            # visitor's business either way.
            raise HTTPException(status_code=404, detail="No such conversation.")
        row = sessions.execute(
            "SELECT title FROM session WHERE session_id = ?", (conversation_id,)
        ).fetchone()
        exchanges = store.transcript(sessions, conversation_id)

    return Conversation(
        id=conversation_id,
        title=row["title"],
        exchanges=[
            TranscriptExchange(
                question=exchange.question,
                answer=exchange.answer,
                tools=[
                    TranscriptTool(id=call.id, name=call.name, args=call.args, ok=call.ok)
                    for call in exchange.tools
                ],
                sources=[
                    TranscriptSource(
                        site_number=source.site_number,
                        name=source.name,
                        area=source.area,
                        url=source.url,
                        excerpt=source.excerpt,
                    )
                    for source in exchange.sources
                ],
            )
            for exchange in exchanges
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    request: Request,
    conversation_id: Annotated[str, Path(min_length=1, max_length=64)],
    visitor: Annotated[Visitor, Depends(visitor_of)],
) -> Response:
    with open_sessions(request) as sessions:
        if not store.delete_session(sessions, conversation_id, visitor.id):
            raise HTTPException(status_code=404, detail="No such conversation.")
    return Response(status_code=204)


__all__ = ["router"]
