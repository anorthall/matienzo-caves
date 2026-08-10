"""One site, in full.

Backs the panel a citation opens into, and doubles as a plain public endpoint.
It is `tools.get_site` verbatim — the same record the model sees — so the page a
visitor reads and the evidence the answer was built from cannot disagree.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path

from matienzo import config, tools
from matienzo.web.execute import Executor
from matienzo.web.routes.deps import executor_of, rate_limited_search

router = APIRouter(prefix="/api")


@router.get("/site/{site_number}", dependencies=[Depends(rate_limited_search)])
async def get_site(
    executor: Annotated[Executor, Depends(executor_of)],
    site_number: Annotated[int, Path(ge=config.SITE_NUMBER_MIN, le=config.SITE_NUMBER_MAX)],
    include_description: bool = True,
) -> dict[str, Any]:
    record = await executor.read(
        tools.get_site, site_number=site_number, include_description=include_description
    )
    if "error" in record:
        raise HTTPException(status_code=404, detail=record["error"])
    return record
