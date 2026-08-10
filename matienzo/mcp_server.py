"""MCP server exposing the corpus to an agent.

An adapter and nothing more. The tools themselves live in `matienzo.tools`,
shared with the web portal, so that the two cannot advertise the same tool
differently — see that module's docstring for why description drift is the
failure worth designing against.

What stays here is the shape the MCP SDK needs. It derives each tool's JSON
Schema from the wrapper's signature, so the signatures have to be written out
even though `matienzo.tools` also carries a hand-written schema for the
Anthropic API. That duplication is real but bounded and *checkable*:
`tests/test_tools.py` asserts the two agree on every parameter name, and the
descriptions — the only thing steering an agent's choice — have a single source.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from matienzo import __version__, config, tools
from matienzo.db.connect import connect

# Re-exported: they are the `sql` guard's public surface and the tests that
# prove a write cannot be smuggled past it import them from here.
CORPUS_PRIMER = tools.CORPUS_PRIMER
FORBIDDEN_RE = tools.FORBIDDEN_RE
READ_ONLY_RE = tools.READ_ONLY_RE
SQL_ROW_LIMIT = tools.SQL_ROW_LIMIT

#: Kept as the historical name; the MCP handshake calls it `instructions`.
INSTRUCTIONS = tools.CORPUS_PRIMER


def _connect() -> sqlite3.Connection:
    if not config.DB_PATH.exists():
        raise RuntimeError(f"No database at {config.DB_PATH}. Run `matienzo build` first.")
    return connect(config.DB_PATH, read_only=True)


def _run(name: str, **arguments: Any) -> Any:
    """Open a connection, run one tool, close it.

    Per-call rather than pooled: opening a local SQLite file costs tens of
    microseconds, and a connection outliving the call would have to answer for
    thread affinity that a stdio server has no reason to take on.
    """
    connection = _connect()
    try:
        return tools.BY_NAME[name].handler(connection, **arguments)
    finally:
        connection.close()


def build_server() -> Any:
    """Construct the MCP server. Imported lazily so the core install works
    without the `mcp` extra."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="matienzo",
        version=__version__,
        instructions=tools.CORPUS_PRIMER,
    )

    def register(fn: Any) -> None:
        server.add_tool(fn, name=fn.__name__, description=tools.BY_NAME[fn.__name__].description)

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
        return _run(
            "search_sites",
            query=query,
            area=area,
            site_type=site_type,
            min_length_m=min_length_m,
            min_depth_m=min_depth_m,
            has_survey=has_survey,
            hybrid=hybrid,
            limit=limit,
        )

    def search_passages(
        query: str,
        site_number: int | None = None,
        hybrid: bool = True,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return _run(
            "search_passages",
            query=query,
            site_number=site_number,
            hybrid=hybrid,
            limit=limit,
        )

    def get_site(site_number: int, include_description: bool = True) -> dict[str, Any]:
        return _run("get_site", site_number=site_number, include_description=include_description)

    def nearby_sites(
        site_number: int, radius_m: float = 500.0, limit: int = 20
    ) -> list[dict[str, Any]]:
        return _run("nearby_sites", site_number=site_number, radius_m=radius_m, limit=limit)

    def site_graph(site_number: int, depth: int = 1) -> dict[str, Any]:
        return _run("site_graph", site_number=site_number, depth=depth)

    def find_by_citation(
        author: str | None = None, year: int | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        return _run("find_by_citation", author=author, year=year, limit=limit)

    def corpus_stats() -> dict[str, int]:
        return _run("corpus_stats")

    def sql(query: str) -> dict[str, Any]:
        return _run("sql", query=query)

    for fn in (
        search_sites,
        search_passages,
        get_site,
        nearby_sites,
        site_graph,
        find_by_citation,
        corpus_stats,
        sql,
    ):
        register(fn)

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
