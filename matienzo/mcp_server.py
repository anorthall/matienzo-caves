"""MCP server exposing the corpus to an agent.

An adapter and nothing more: every tool here is a thin wrapper over
`matienzo.search` or `matienzo.db.queries`. No query logic lives in this file,
deliberately — the moment the MCP server starts answering questions its own way,
it and the CLI begin to disagree, and nobody notices until they are compared.

The tool set is small on purpose. Rather than pre-building a tool for every
question someone might ask, it covers the shapes an agent genuinely cannot
assemble from SQL — ranked search, geography, the link graph — and then offers a
guarded read-only `sql` tool for everything else. The schema is rich and
well-documented; letting the agent write its own aggregate beats guessing which
twelve tools it will want.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from matienzo import __version__, config, embed, search
from matienzo.db import queries
from matienzo.db.connect import connect

INSTRUCTIONS = """\
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
SQL_ROW_LIMIT = 200

#: Only a single read. `PRAGMA` is excluded too: some pragmas write.
READ_ONLY_RE = re.compile(r"^\s*(?:WITH|SELECT)\b", re.IGNORECASE)
FORBIDDEN_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM)\b",
    re.IGNORECASE,
)


def _connect() -> sqlite3.Connection:
    if not config.DB_PATH.exists():
        raise RuntimeError(f"No database at {config.DB_PATH}. Run `matienzo build` first.")
    return connect(config.DB_PATH, read_only=True)


def _rows(cursor: sqlite3.Cursor | list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor]


def build_server() -> Any:
    """Construct the MCP server. Imported lazily so the core install works
    without the `mcp` extra."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="matienzo",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @server.tool()
    def search_sites(
        query: str,
        area: str | None = None,
        site_type: str | None = None,
        min_length_m: float | None = None,
        min_depth_m: float | None = None,
        has_survey: bool = False,
        hybrid: bool = True,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Find caves by description or name, ranked.

        Use this to answer "which cave is this?". `hybrid` fuses keyword and
        semantic ranking and is on by default; it falls back to keyword-only if
        the database has no embeddings.
        """
        connection = _connect()
        try:
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
                }
                for h in hits
            ]
        finally:
            connection.close()

    @server.tool()
    def search_passages(
        query: str,
        site_number: int | None = None,
        hybrid: bool = True,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find the specific paragraphs that discuss something.

        Use this when you need the evidence rather than the cave — quoting a
        description, or checking what a page actually says.
        """
        connection = _connect()
        try:
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
                }
                for p in found[:limit]
            ]
        finally:
            connection.close()

    @server.tool()
    def get_site(site_number: int, include_description: bool = True) -> dict[str, Any]:
        """Everything recorded about one site: measurements, position, aliases,
        update history, references and cross-references."""
        connection = _connect()
        try:
            row = connection.execute(
                "SELECT * FROM site_summary WHERE site_number = ?", (site_number,)
            ).fetchone()
            if row is None:
                return {"error": f"site {site_number:04d} is not in the corpus"}

            record: dict[str, Any] = dict(row)
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
                    "SELECT label, raw, kind, value_m, modifier FROM quantity"
                    " WHERE site_number = ?",
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
        finally:
            connection.close()

    @server.tool()
    def nearby_sites(
        site_number: int, radius_m: float = 500.0, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Sites within a radius, nearest first. Distances are exact — the
        stored coordinates are UTM metres."""
        connection = _connect()
        try:
            return [
                {
                    "site_number": h.site_number,
                    "name": h.name,
                    "area": h.area,
                    "distance_m": round(h.score),
                    "length_m": h.length_m,
                    "depth_m": h.depth_m,
                }
                for h in search.nearby(connection, site_number, radius_m=radius_m, limit=limit)
            ]
        finally:
            connection.close()

    @server.tool()
    def site_graph(site_number: int, depth: int = 1) -> dict[str, Any]:
        """The cross-reference neighbourhood around a site.

        Edges run both ways: what a page refers to, and what refers to it.
        Useful for tracing how caves in a system connect.
        """
        connection = _connect()
        try:
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
        finally:
            connection.close()

    @server.tool()
    def find_by_citation(
        author: str | None = None, year: int | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Find works in the bibliography and the sites that cite them."""
        connection = _connect()
        try:
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
                    f"SELECT c.raw, c.author_raw, c.year, c.kind, c.site_count"
                    f" FROM citation c {where}"
                    f" ORDER BY c.site_count DESC LIMIT ?",
                    (*params, limit),
                )
            )
        finally:
            connection.close()

    @server.tool()
    def corpus_stats() -> dict[str, int]:
        """Counts across the whole corpus — useful for sanity-checking an
        aggregate before trusting it."""
        connection = _connect()
        try:
            return {
                label.strip(): queries.scalar(connection, sql)
                for label, sql in queries.STATS.items()
            }
        finally:
            connection.close()

    @server.tool()
    def sql(query: str) -> dict[str, Any]:
        """Run a read-only SELECT against the database.

        The schema is documented in `matienzo/db/schema.sql`. Key tables: site,
        area, coordinate, quantity, update_date, block, section, xref,
        cave_system, system_member, citation, site_citation, resource_link,
        person, anomaly.

        Only a single SELECT or WITH statement is allowed, and results are
        truncated. Prefer this over guessing which purpose-built tool exists.
        """
        if not READ_ONLY_RE.match(query) or FORBIDDEN_RE.search(query):
            return {"error": "only a single read-only SELECT or WITH query is allowed"}
        if ";" in query.rstrip().rstrip(";"):
            return {"error": "only one statement is allowed"}

        connection = _connect()
        try:
            cursor = connection.execute(query)
            rows = [dict(row) for row in cursor.fetchmany(SQL_ROW_LIMIT + 1)]
        except sqlite3.Error as error:
            return {"error": str(error)}
        finally:
            connection.close()

        truncated = len(rows) > SQL_ROW_LIMIT
        return {
            "rows": rows[:SQL_ROW_LIMIT],
            "row_count": len(rows[:SQL_ROW_LIMIT]),
            "truncated": truncated,
        }

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
