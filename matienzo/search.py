"""The one search API.

Both the CLI and the MCP server call this module and nothing else. Keeping a
single query surface is what stops the two answering the same question
differently — a failure that is invisible until someone compares them.

Results come at two granularities because callers genuinely want different
things: `search_passages` returns the paragraphs that matched, for reading or
for feeding an answer; `search_sites` aggregates those into "which cave is this
about". A site's score is its best passage plus a discounted contribution from
the rest, so a cave mentioned strongly once outranks one mentioned weakly five
times.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

#: How much a site's supporting passages count beyond its best one.
SUPPORTING_WEIGHT = 0.3

#: FTS5 operators that would otherwise make a plain user query a syntax error.
FTS_SPECIAL_RE = re.compile(r'["*():^-]')


@dataclass(slots=True)
class Passage:
    chunk_id: int
    site_number: int
    site_name: str | None
    area: str | None
    kind: str
    section_heading: str | None
    text: str
    score: float
    snippet: str = ""
    """Matched terms are wrapped in «guillemets» rather than [brackets]: the
    snippet is data, and square brackets are console-markup syntax in the CLI —
    a cave named `[shaft]` would silently vanish from the output."""


@dataclass(slots=True)
class SiteHit:
    site_number: int
    name: str | None
    area: str | None
    site_type: str | None
    length_m: float | None
    depth_m: float | None
    score: float
    best_passage: str = ""
    passage_count: int = 0


@dataclass(slots=True)
class Filters:
    """Constraints applied *inside* each retriever, never after fusion.

    Filtering a fused top-k would routinely return nothing: a narrow filter over
    a candidate set chosen without it usually removes every candidate.
    """

    area: str | None = None
    site_type: str | None = None
    min_length_m: float | None = None
    max_length_m: float | None = None
    min_depth_m: float | None = None
    has_survey: bool | None = None
    kinds: tuple[str, ...] = field(default_factory=tuple)

    def sql(self) -> tuple[str, list[object]]:
        """A `WHERE` fragment constraining `site` (aliased `s`) and `chunk` (`c`)."""
        clauses: list[str] = []
        params: list[object] = []

        if self.area:
            clauses.append(
                "s.area_id IN (SELECT area_id FROM area WHERE name LIKE ? OR slug LIKE ?)"
            )
            params += [f"%{self.area}%", f"%{self.area.lower()}%"]
        if self.site_type:
            clauses.append("s.site_type = ?")
            params.append(self.site_type)
        if self.min_length_m is not None:
            clauses.append("s.length_m >= ?")
            params.append(self.min_length_m)
        if self.max_length_m is not None:
            clauses.append("s.length_m <= ?")
            params.append(self.max_length_m)
        if self.min_depth_m is not None:
            clauses.append("s.depth_m >= ?")
            params.append(self.min_depth_m)
        if self.has_survey:
            clauses.append(
                "EXISTS (SELECT 1 FROM resource_link r WHERE r.site_number = s.site_number"
                " AND r.bucket LIKE 'survey%')"
            )
        if self.kinds:
            clauses.append(f"c.kind IN ({','.join('?' * len(self.kinds))})")
            params += list(self.kinds)

        return (" AND " + " AND ".join(clauses) if clauses else ""), params


def escape_query(query: str) -> str:
    """Make a user's words safe for FTS5's query grammar.

    Every term is quoted rather than escaped: users type hyphens, apostrophes
    and brackets in cave names constantly, and each one is an operator to FTS5.
    """
    terms = [t for t in FTS_SPECIAL_RE.sub(" ", query).split() if t]
    return " ".join(f'"{t}"' for t in terms)


def search_passages(
    connection: sqlite3.Connection,
    query: str,
    *,
    filters: Filters | None = None,
    limit: int = 20,
) -> list[Passage]:
    """Rank individual passages by BM25."""
    match = escape_query(query)
    if not match:
        return []

    constraint, params = (filters or Filters()).sql()
    rows = connection.execute(
        f"""
        SELECT c.chunk_id, c.site_number, c.kind, c.section_heading, c.text,
               s.name AS site_name, a.name AS area,
               bm25(chunk_fts) AS rank,
               snippet(chunk_fts, 0, '«', '»', '…', 18) AS snippet
        FROM chunk_fts
        JOIN chunk c ON c.chunk_id = chunk_fts.rowid
        JOIN site  s ON s.site_number = c.site_number
        LEFT JOIN area a ON a.area_id = s.area_id
        WHERE chunk_fts MATCH ?{constraint}
        ORDER BY rank
        LIMIT ?
        """,
        (match, *params, limit),
    ).fetchall()

    return [
        Passage(
            chunk_id=row["chunk_id"],
            site_number=row["site_number"],
            site_name=row["site_name"],
            area=row["area"],
            kind=row["kind"],
            section_heading=row["section_heading"],
            text=row["text"],
            # bm25() returns increasingly negative scores for better matches.
            score=-float(row["rank"]),
            snippet=row["snippet"],
        )
        for row in rows
    ]


def search_sites(
    connection: sqlite3.Connection,
    query: str,
    *,
    filters: Filters | None = None,
    limit: int = 20,
    candidate_pool: int = 200,
) -> list[SiteHit]:
    """Rank sites by aggregating their passage scores.

    The pool is deliberately much larger than the limit: a site's rank depends
    on all of its matching passages, so truncating before aggregation would
    order them by whichever passage happened to surface first.
    """
    passages = search_passages(connection, query, filters=filters, limit=candidate_pool)
    if not passages:
        return _name_only_matches(connection, query, filters, limit)

    grouped: dict[int, list[Passage]] = {}
    for passage in passages:
        grouped.setdefault(passage.site_number, []).append(passage)

    hits: list[SiteHit] = []
    for site_number, found in grouped.items():
        found.sort(key=lambda p: p.score, reverse=True)
        score = found[0].score + SUPPORTING_WEIGHT * sum(p.score for p in found[1:])
        row = connection.execute(
            "SELECT s.name, s.site_type, s.length_m, s.depth_m, a.name AS area"
            " FROM site s LEFT JOIN area a ON a.area_id = s.area_id"
            " WHERE s.site_number = ?",
            (site_number,),
        ).fetchone()
        hits.append(
            SiteHit(
                site_number=site_number,
                name=row["name"] if row else None,
                area=row["area"] if row else None,
                site_type=row["site_type"] if row else None,
                length_m=row["length_m"] if row else None,
                depth_m=row["depth_m"] if row else None,
                score=score,
                best_passage=found[0].snippet or found[0].text[:200],
                passage_count=len(found),
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def _name_only_matches(
    connection: sqlite3.Connection,
    query: str,
    filters: Filters | None,
    limit: int,
) -> list[SiteHit]:
    """Fall back to a substring match over names.

    Catches the partial and misspelled names the word tokeniser cannot reach —
    `hoyuc`, `azpili` — which is a common way people search for a cave.
    """
    constraint, params = (filters or Filters()).sql()
    constraint = constraint.replace(" AND c.kind IN", " AND 1=1 -- c.kind IN")
    rows = connection.execute(
        f"""
        SELECT s.site_number, s.name, s.site_type, s.length_m, s.depth_m,
               a.name AS area
        FROM site s
        LEFT JOIN area a ON a.area_id = s.area_id
        WHERE (s.name_sort LIKE ? OR s.name LIKE ?
               OR EXISTS (SELECT 1 FROM site_alias al
                          WHERE al.site_number = s.site_number AND al.text_sort LIKE ?))
              {constraint}
        ORDER BY s.length_m DESC NULLS LAST
        LIMIT ?
        """,
        (f"%{query.lower()}%", f"%{query}%", f"%{query.lower()}%", *params, limit),
    ).fetchall()

    return [
        SiteHit(
            site_number=row["site_number"],
            name=row["name"],
            area=row["area"],
            site_type=row["site_type"],
            length_m=row["length_m"],
            depth_m=row["depth_m"],
            score=0.0,
            best_passage="(name match)",
        )
        for row in rows
    ]


def nearby(
    connection: sqlite3.Connection, site_number: int, radius_m: float = 500.0, limit: int = 20
) -> list[SiteHit]:
    """Sites within a radius, by UTM distance.

    Distance is computed on the UTM grid rather than in degrees: eastings and
    northings are already metres, so this is exact and needs no projection.
    """
    origin = connection.execute(
        "SELECT easting, northing FROM site WHERE site_number = ?", (site_number,)
    ).fetchone()
    if origin is None or origin["easting"] is None:
        return []

    rows = connection.execute(
        """
        SELECT s.site_number, s.name, s.site_type, s.length_m, s.depth_m, a.name AS area,
               ((s.easting - ?) * (s.easting - ?) +
                (s.northing - ?) * (s.northing - ?)) AS d2
        FROM site s LEFT JOIN area a ON a.area_id = s.area_id
        WHERE s.easting IS NOT NULL AND s.site_number <> ? AND d2 <= ?
        ORDER BY d2 LIMIT ?
        """,
        (
            origin["easting"],
            origin["easting"],
            origin["northing"],
            origin["northing"],
            site_number,
            radius_m * radius_m,
            limit,
        ),
    ).fetchall()

    return [
        SiteHit(
            site_number=row["site_number"],
            name=row["name"],
            area=row["area"],
            site_type=row["site_type"],
            length_m=row["length_m"],
            depth_m=row["depth_m"],
            score=float(row["d2"]) ** 0.5,
            best_passage=f"{float(row['d2']) ** 0.5:.0f} m away",
        )
        for row in rows
    ]


def neighbourhood(
    connection: sqlite3.Connection, site_number: int, depth: int = 1
) -> dict[int, list[int]]:
    """The cross-reference neighbourhood around a site, breadth-first.

    Edges are followed in both directions: "what refers to this cave" is as much
    a part of its context as "what it refers to".
    """
    seen = {site_number}
    frontier = [site_number]
    graph: dict[int, list[int]] = {}

    for _ in range(max(1, depth)):
        next_frontier: list[int] = []
        for current in frontier:
            rows = connection.execute(
                "SELECT DISTINCT to_site AS other FROM xref WHERE from_site = ?"
                " UNION SELECT DISTINCT from_site FROM xref WHERE to_site = ?",
                (current, current),
            ).fetchall()
            neighbours = [int(r["other"]) for r in rows]
            graph[current] = neighbours
            for neighbour in neighbours:
                if neighbour not in seen:
                    seen.add(neighbour)
                    next_frontier.append(neighbour)
        frontier = next_frontier
        if not frontier:
            break

    return graph
