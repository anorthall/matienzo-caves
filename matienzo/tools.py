"""The agent-facing projection of the corpus.

Every tool here is a thin wrapper over `matienzo.search` or
`matienzo.db.queries` — the same rule the MCP server was written under, moved
one layer down now that there are two adapters instead of one. No query logic
lives in this file.

It exists as a shared module rather than as tools defined inside each adapter
because of what would drift if it did not. Two copies of `search_sites` would go
on returning the same rows; what would diverge is the *descriptions*, and the
descriptions are the only thing steering a model's choice of tool. That failure
is invisible — both adapters keep working, they just start giving an agent
different advice — which makes it worse than the query drift the MCP server's
docstring already warns about.

Handlers take their connection as an argument rather than opening one. That is
what makes them individually importable and testable: the previous versions
closed over `config.DB_PATH`, so nothing could exercise a tool without a fully
built database sitting in the repo root.

Schemas are hand-written literals rather than derived from type hints. The
Anthropic API wants a JSON Schema dict, `strict` mode wants explicit `required`
and `additionalProperties: false`, and there are eight of them; owning a schema
generator to avoid writing eight literals is the worse trade.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from matienzo import embed, links, search
from matienzo.db import queries

CORPUS_PRIMER: Final = """\
The Matienzo Caves corpus: 5,557 cave and shaft descriptions from the Matienzo
depression in Cantabria, Spain, scraped from matienzocaves.org.uk.

Sites are identified by a four-digit number. Search ignores accents, so `riano`
finds `Riaño`. Names are written Spanish-style with the head noun last —
`Burro, Sima del` — so search by either word.

Measurements are not always numbers: about 60 sites record a length like
`included in the Four Valleys System` instead, meaning their passage is counted
under the system rather than separately. Those sites have a NULL `length_m` and
a row in `system_member`. Any total length should filter on
`quantity.kind = 'numeric'` to avoid double-counting a system.
"""

#: Rows a `sql` result is truncated to. Enough to answer a question, small
#: enough that a careless `SELECT *` cannot flood the context.
SQL_ROW_LIMIT: Final = 200

#: Wall-clock ceiling for one `sql` call. The row limit caps what comes back,
#: not what the query costs: `SELECT count(*) FROM resource_link a, resource_link b`
#: returns one row after cross-joining 38,501² of them. A progress handler is the
#: only mechanism SQLite offers to interrupt a statement already running.
SQL_TIMEOUT_SECONDS: Final = 2.0

#: How often the progress handler runs, in VM instructions. Frequent enough that
#: the deadline is honoured promptly, rare enough not to matter to normal queries.
SQL_PROGRESS_INTERVAL: Final = 10_000

#: Only a single read. `PRAGMA` is excluded too: some pragmas write.
READ_ONLY_RE: Final = re.compile(r"^\s*(?:WITH|SELECT)\b", re.IGNORECASE)
FORBIDDEN_RE: Final = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool, as both adapters see it.

    `description` is carried here rather than left as each wrapper's docstring
    so that the MCP server and the HTTP agent cannot advertise the same tool
    differently — `tests/test_tools.py` asserts they do not.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    needs_embeddings: bool = False
    """Whether a call can reach the embedding model. The web layer queues these
    behind a small capacity limiter; ONNX inference wants CPU, and unbounded
    parallelism makes tail latency worse than a queue does."""


def _rows(cursor: sqlite3.Cursor | list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor]


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def search_sites(
    connection: sqlite3.Connection,
    *,
    query: str,
    area: str | None = None,
    site_type: str | None = None,
    min_length_m: float | None = None,
    min_depth_m: float | None = None,
    has_survey: bool = False,
    hybrid: bool = True,
    limit: int = 15,
) -> list[dict[str, Any]]:
    filters = search.Filters(
        area=area,
        site_type=site_type,
        min_length_m=min_length_m,
        min_depth_m=min_depth_m,
        has_survey=has_survey or None,
    )
    hits = search.search_sites(
        connection,
        query,
        filters=filters,
        limit=limit,
        hybrid=hybrid and embed.is_available(connection),
    )
    return [
        {
            "site_number": h.site_number,
            "name": h.name,
            "area": h.area,
            "type": h.site_type,
            "length_m": h.length_m,
            "depth_m": h.depth_m,
            "excerpt": " ".join(h.best_passage.split())[:300],
            "url": links.site_url(h.site_number),
        }
        for h in hits
    ]


def search_passages(
    connection: sqlite3.Connection,
    *,
    query: str,
    site_number: int | None = None,
    hybrid: bool = True,
    limit: int = 10,
) -> list[dict[str, Any]]:
    use_hybrid = hybrid and embed.is_available(connection)
    found = (
        search.search_hybrid(connection, query, limit=limit * 3)
        if use_hybrid
        else search.search_passages(connection, query, limit=limit * 3)
    )
    if site_number is not None:
        found = [p for p in found if p.site_number == site_number]
    return [
        {
            "site_number": p.site_number,
            "site_name": p.site_name,
            "section": p.section_heading,
            "kind": p.kind,
            "text": p.text,
            "url": links.site_url(p.site_number),
        }
        for p in found[:limit]
    ]


def get_site(
    connection: sqlite3.Connection,
    *,
    site_number: int,
    include_description: bool = True,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM site_summary WHERE site_number = ?", (site_number,)
    ).fetchone()
    if row is None:
        return {"error": f"site {site_number:04d} is not in the corpus"}

    record: dict[str, Any] = dict(row)
    record["url"] = links.site_url(site_number)
    record["aliases"] = _rows(
        connection.execute(
            "SELECT text, kind FROM site_alias WHERE site_number = ? ORDER BY ordinal",
            (site_number,),
        )
    )
    record["coordinates"] = _rows(
        connection.execute(
            "SELECT label, easting, northing, latitude, longitude, altitude_m,"
            " accuracy_code FROM coordinate WHERE site_number = ? ORDER BY ordinal",
            (site_number,),
        )
    )
    record["measurements"] = _rows(
        connection.execute(
            "SELECT label, raw, kind, value_m, modifier FROM quantity WHERE site_number = ?",
            (site_number,),
        )
    )
    record["systems"] = _rows(
        connection.execute(
            "SELECT cs.name, m.relation FROM system_member m"
            " JOIN cave_system cs USING (system_id) WHERE m.site_number = ?",
            (site_number,),
        )
    )
    record["sections"] = [
        r["heading"]
        for r in connection.execute(
            "SELECT heading FROM section WHERE site_number = ? ORDER BY ordinal",
            (site_number,),
        )
    ]
    record["citations"] = [
        r["raw"]
        for r in connection.execute(
            "SELECT c.raw FROM site_citation sc JOIN citation c USING (citation_id)"
            " WHERE sc.site_number = ? ORDER BY sc.ordinal",
            (site_number,),
        )
    ]
    record["resources"] = [
        {
            "label": r["text"],
            "bucket": r["bucket"],
            "url": links.resource_url(r["href"]),
        }
        for r in connection.execute(
            "SELECT DISTINCT href, text, bucket FROM resource_link"
            " WHERE site_number = ? AND bucket NOT IN ('site_page', 'other')"
            " ORDER BY bucket, href",
            (site_number,),
        )
    ]
    record["refers_to"] = [
        r["to_site"]
        for r in connection.execute(
            "SELECT DISTINCT to_site FROM xref WHERE from_site = ? ORDER BY to_site",
            (site_number,),
        )
    ]
    record["referred_to_by"] = [
        r["from_site"]
        for r in connection.execute(
            "SELECT DISTINCT from_site FROM xref WHERE to_site = ? ORDER BY from_site",
            (site_number,),
        )
    ]
    if include_description:
        record["description"] = connection.execute(
            "SELECT body_text FROM site WHERE site_number = ?", (site_number,)
        ).fetchone()["body_text"]
    return record


def nearby_sites(
    connection: sqlite3.Connection,
    *,
    site_number: int,
    radius_m: float = 500.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return [
        {
            "site_number": h.site_number,
            "name": h.name,
            "area": h.area,
            "distance_m": round(h.score),
            "length_m": h.length_m,
            "depth_m": h.depth_m,
            "url": links.site_url(h.site_number),
        }
        for h in search.nearby(connection, site_number, radius_m=radius_m, limit=limit)
    ]


def site_graph(
    connection: sqlite3.Connection, *, site_number: int, depth: int = 1
) -> dict[str, Any]:
    graph = search.neighbourhood(connection, site_number, depth=depth)
    names = {
        r["site_number"]: r["name"]
        for r in connection.execute(
            "SELECT site_number, name FROM site WHERE site_number IN"
            f" ({','.join('?' * len(graph))})",
            tuple(graph),
        )
    }
    return {
        "centre": site_number,
        "edges": {
            str(source): [{"site_number": t, "name": names.get(t)} for t in targets]
            for source, targets in graph.items()
        },
    }


def find_by_citation(
    connection: sqlite3.Connection,
    *,
    author: str | None = None,
    year: int | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[str | int] = []
    if author:
        clauses.append("c.author_raw LIKE ?")
        params.append(f"%{author}%")
    if year:
        clauses.append("c.year = ?")
        params.append(year)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return _rows(
        connection.execute(
            f"SELECT c.raw, c.author_raw, c.year, c.kind, c.site_count FROM citation c {where}"
            f" ORDER BY c.site_count DESC LIMIT ?",
            (*params, limit),
        )
    )


def corpus_stats(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        label.strip(): queries.scalar(connection, sql_text)
        for label, sql_text in queries.STATS.items()
    }


def _deadline(seconds: float) -> Callable[[], int]:
    """A progress callback that aborts once `seconds` have elapsed.

    Returning non-zero makes SQLite raise `OperationalError: interrupted`, which
    the caller already reports as a tool error — so a runaway query costs one
    failed tool call rather than a pinned CPU.
    """
    end = time.monotonic() + seconds
    return lambda: 1 if time.monotonic() > end else 0


def sql(connection: sqlite3.Connection, *, query: str) -> dict[str, Any]:
    if not READ_ONLY_RE.match(query) or FORBIDDEN_RE.search(query):
        return {"error": "only a single read-only SELECT or WITH query is allowed"}
    if ";" in query.rstrip().rstrip(";"):
        return {"error": "only one statement is allowed"}

    connection.set_progress_handler(_deadline(SQL_TIMEOUT_SECONDS), SQL_PROGRESS_INTERVAL)
    try:
        cursor = connection.execute(query)
        rows = [dict(row) for row in cursor.fetchmany(SQL_ROW_LIMIT + 1)]
    except sqlite3.Error as error:
        return {"error": str(error)}
    finally:
        connection.set_progress_handler(None, 0)

    truncated = len(rows) > SQL_ROW_LIMIT
    return {
        "rows": rows[:SQL_ROW_LIMIT],
        "row_count": len(rows[:SQL_ROW_LIMIT]),
        "truncated": truncated,
    }


REGISTRY: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="search_sites",
        description=(
            "Find caves by description or name, ranked.\n\n"
            'Use this to answer "which cave is this?". `hybrid` fuses keyword and '
            "semantic ranking and is on by default; it falls back to keyword-only if "
            "the database has no embeddings."
        ),
        input_schema=_schema(
            {
                "query": {"type": "string", "description": "What to look for."},
                "area": {
                    "type": ["string", "null"],
                    "description": "Restrict to an area, matched loosely (e.g. `riano`).",
                },
                "site_type": {
                    "type": ["string", "null"],
                    "description": "Restrict to a site type, e.g. `cave`, `shaft`, `dig`.",
                },
                "min_length_m": {"type": ["number", "null"]},
                "min_depth_m": {"type": ["number", "null"]},
                "has_survey": {
                    "type": "boolean",
                    "description": "Only sites with a survey attached.",
                },
                "hybrid": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["query"],
        ),
        handler=search_sites,
        needs_embeddings=True,
    ),
    ToolSpec(
        name="search_passages",
        description=(
            "Find the specific paragraphs that discuss something.\n\n"
            "Use this when you need the evidence rather than the cave — quoting a "
            "description, or checking what a page actually says."
        ),
        input_schema=_schema(
            {
                "query": {"type": "string", "description": "What to look for."},
                "site_number": {
                    "type": ["integer", "null"],
                    "description": "Restrict to one site's passages.",
                },
                "hybrid": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            ["query"],
        ),
        handler=search_passages,
        needs_embeddings=True,
    ),
    ToolSpec(
        name="get_site",
        description=(
            "Everything recorded about one site: measurements, position, aliases, "
            "update history, references, cross-references, and links to its surveys "
            "and photographs."
        ),
        input_schema=_schema(
            {
                "site_number": {"type": "integer", "minimum": 1, "maximum": 5557},
                "include_description": {
                    "type": "boolean",
                    "description": "Include the full prose description. Long; omit when"
                    " you only need the metadata.",
                },
            },
            ["site_number"],
        ),
        handler=get_site,
    ),
    ToolSpec(
        name="nearby_sites",
        description=(
            "Sites within a radius, nearest first. Distances are exact — the stored "
            "coordinates are UTM metres, so no projection is involved."
        ),
        input_schema=_schema(
            {
                "site_number": {"type": "integer", "minimum": 1, "maximum": 5557},
                "radius_m": {"type": "number", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["site_number"],
        ),
        handler=nearby_sites,
    ),
    ToolSpec(
        name="site_graph",
        description=(
            "The cross-reference neighbourhood around a site.\n\n"
            "Edges run both ways: what a page refers to, and what refers to it. "
            "Useful for tracing how caves in a system connect."
        ),
        input_schema=_schema(
            {
                "site_number": {"type": "integer", "minimum": 1, "maximum": 5557},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            ["site_number"],
        ),
        handler=site_graph,
    ),
    ToolSpec(
        name="find_by_citation",
        description="Find works in the bibliography and the number of sites that cite them.",
        input_schema=_schema(
            {
                "author": {"type": ["string", "null"]},
                "year": {"type": ["integer", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        ),
        handler=find_by_citation,
    ),
    ToolSpec(
        name="corpus_stats",
        description=(
            "Counts across the whole corpus — useful for sanity-checking an aggregate "
            "before trusting it."
        ),
        input_schema=_schema({}),
        handler=corpus_stats,
    ),
    ToolSpec(
        name="sql",
        description=(
            "Run a read-only SELECT against the database.\n\n"
            "The schema is documented in `matienzo/db/schema.sql`. Key tables: site, "
            "area, coordinate, quantity, update_date, block, section, xref, "
            "cave_system, system_member, citation, site_citation, resource_link, "
            "person, anomaly.\n\n"
            "Only a single SELECT or WITH statement is allowed, results are truncated, "
            "and a slow query is cancelled. Prefer this over guessing which "
            "purpose-built tool exists."
        ),
        input_schema=_schema(
            {"query": {"type": "string", "description": "A single SELECT or WITH statement."}},
            ["query"],
        ),
        handler=sql,
    ),
)

BY_NAME: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in REGISTRY}


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a tool call produced, plus the provenance the caller needs.

    `site_numbers` is derived from the payload rather than from the model's
    account of it, which is what makes a rendered citation trustworthy: the web
    layer will only link a site the corpus actually returned.
    """

    payload: Any
    site_numbers: tuple[int, ...] = field(default_factory=tuple)
    is_error: bool = False


def call(spec: ToolSpec, connection: sqlite3.Connection, arguments: dict[str, Any]) -> Outcome:
    """Run one tool and collect the sites it touched.

    Unknown arguments are dropped rather than raising. A model that invents a
    parameter should get an answer to the question it could express, not a
    `TypeError` it has no way to interpret.
    """
    accepted = {
        key: value
        for key, value in arguments.items()
        if key in spec.input_schema.get("properties", {})
    }
    payload = spec.handler(connection, **accepted)
    return Outcome(payload=payload, site_numbers=site_numbers(payload))


def site_numbers(payload: Any) -> tuple[int, ...]:
    """Every site number a payload mentions, in first-seen order.

    Walks the structure rather than reading a fixed field: the eight tools return
    six different shapes, and a provenance ledger that only understood two of them
    would silently under-cite the other four.
    """
    found: dict[int, None] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("site_number", "centre", "to_site", "from_site") and isinstance(
                    value, int
                ):
                    found.setdefault(value, None)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return tuple(found)
