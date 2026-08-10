"""Search without synthesis.

This endpoint is also the portal's degraded mode, and that is the reason it is
exposed permanently rather than only reached when something has gone wrong. A
fallback path that is only exercised on the day the budget runs out is a
fallback path nobody has ever seen work. This one is a public API, it is what
the portal serves with no API key configured, and it runs on every test.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from matienzo import embed, links, search
from matienzo.web.execute import Executor
from matienzo.web.routes.deps import executor_of, rate_limited_search

router = APIRouter(prefix="/api")

#: Sites returned by a default search. Enough to choose from, few enough to read.
DEFAULT_LIMIT = 10


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    area: str | None = None
    site_type: str | None = None
    min_length_m: float | None = None
    min_depth_m: float | None = None
    has_survey: bool = False
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=50)


class SiteResult(BaseModel):
    site_number: int
    name: str | None
    area: str | None
    site_type: str | None
    length_m: float | None
    depth_m: float | None
    excerpt: str
    url: str


class SearchResponse(BaseModel):
    query: str
    hybrid: bool
    """Whether semantic ranking actually ran. Reported rather than assumed: a
    database without vectors still answers, just less well, and a caller has no
    other way to tell that happened."""
    results: list[SiteResult]


@router.post("/search", dependencies=[Depends(rate_limited_search)])
async def post_search(
    request: SearchRequest, executor: Annotated[Executor, Depends(executor_of)]
) -> SearchResponse:
    filters = search.Filters(
        area=request.area,
        site_type=request.site_type,
        min_length_m=request.min_length_m,
        min_depth_m=request.min_depth_m,
        has_survey=request.has_survey or None,
    )

    def run(connection: Any) -> tuple[bool, list[search.SiteHit]]:
        hybrid = embed.is_available(connection)
        hits = search.search_sites(
            connection, request.query, filters=filters, limit=request.limit, hybrid=hybrid
        )
        return hybrid, hits

    hybrid, hits = await executor.read(run)
    return SearchResponse(
        query=request.query,
        hybrid=hybrid,
        results=[
            SiteResult(
                site_number=hit.site_number,
                name=hit.name,
                area=hit.area,
                site_type=hit.site_type,
                length_m=hit.length_m,
                depth_m=hit.depth_m,
                excerpt=" ".join(hit.best_passage.split())[:300],
                url=links.site_url(hit.site_number),
            )
            for hit in hits
        ],
    )
