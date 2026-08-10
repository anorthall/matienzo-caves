"""The MCP server.

It is an adapter, so most of what is worth testing is that it *stays* one: the
tools are present, they return what the search layer returns, and the read-only
`sql` guard cannot be talked into a write.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from matienzo import config

pytest.importorskip("mcp", reason="needs `uv sync --extra mcp`")

from matienzo.mcp_server import FORBIDDEN_RE, READ_ONLY_RE, build_server

EXPECTED_TOOLS = {
    "search_sites",
    "search_passages",
    "get_site",
    "nearby_sites",
    "site_graph",
    "find_by_citation",
    "corpus_stats",
    "sql",
}


@pytest.fixture(scope="module")
def server() -> Any:
    return build_server()


def call(server: Any, name: str, **arguments: Any) -> Any:
    """Invoke a tool and decode its payload.

    The SDK returns one content block per element of a list return value, so a
    caller that reads `content[0]` and expects the whole list gets only the
    first item — which is exactly how a working search looked empty during
    development.
    """
    result = asyncio.run(server.call_tool(name, arguments))
    assert not result.is_error, getattr(result.content[0], "text", result)
    blocks = [json.loads(block.text) for block in result.content]
    return blocks if len(blocks) != 1 else blocks[0]


def tools(server: Any) -> list[Any]:
    return asyncio.run(server.list_tools())


class TestToolSurface:
    def test_the_expected_tools_are_registered(self, server: Any) -> None:
        assert {tool.name for tool in tools(server)} == EXPECTED_TOOLS

    def test_every_tool_is_described(self, server: Any) -> None:
        """The descriptions are the only thing telling an agent which tool to
        reach for, so an undocumented one is effectively invisible."""
        for tool in tools(server):
            assert tool.description and len(tool.description) > 40, tool.name


class TestSqlGuard:
    @pytest.mark.parametrize(
        "query",
        [
            "DROP TABLE site",
            "DELETE FROM site",
            "UPDATE site SET name = 'x'",
            "INSERT INTO site VALUES (1)",
            "PRAGMA writable_schema = 1",
            "ATTACH DATABASE '/tmp/x.db' AS x",
            "VACUUM",
        ],
    )
    def test_writes_are_refused(self, query: str) -> None:
        assert not READ_ONLY_RE.match(query) or FORBIDDEN_RE.search(query)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT count(*) FROM site",
            "  select name from site limit 1",
            "WITH x AS (SELECT 1) SELECT * FROM x",
        ],
    )
    def test_reads_are_allowed(self, query: str) -> None:
        assert READ_ONLY_RE.match(query) and not FORBIDDEN_RE.search(query)

    def test_a_write_smuggled_after_a_select_is_refused(self) -> None:
        """`SELECT 1; DROP TABLE site` starts with SELECT, so the prefix check
        alone would pass it."""
        query = "SELECT 1; DROP TABLE site"
        assert FORBIDDEN_RE.search(query)


@pytest.mark.skipif(not config.DB_PATH.exists(), reason="run `matienzo build`")
class TestAgainstTheDatabase:
    def test_sql_returns_rows(self, server: Any) -> None:
        result = call(server, "sql", query="SELECT count(*) AS n FROM site")
        assert result["rows"][0]["n"] == 5557

    def test_sql_refuses_a_write(self, server: Any) -> None:
        result = call(server, "sql", query="DROP TABLE site")
        assert "error" in result

    def test_sql_truncates_rather_than_flooding(self, server: Any) -> None:
        result = call(server, "sql", query="SELECT site_number FROM site")
        assert result["truncated"] is True
        assert result["row_count"] == 200

    def test_get_site_assembles_the_whole_record(self, server: Any) -> None:
        record = call(server, "get_site", site_number=1930, include_description=False)
        assert record["name"] == "Cobadal, Sumidero de"
        assert record["area"] == "Cobadal"
        assert len(record["coordinates"]) == 2, "a two-entrance site"
        assert "Wessex Inlet" in record["sections"]
        assert record["citations"]

    def test_get_site_reports_a_missing_number_rather_than_failing(self, server: Any) -> None:
        assert "error" in call(server, "get_site", site_number=9999)

    def test_system_membership_is_surfaced(self, server: Any) -> None:
        """The payoff from parsing prose measurements — it would be absent from
        a schema that stored length as a nullable float."""
        record = call(server, "get_site", site_number=81, include_description=False)
        assert record["systems"][0]["name"] == "Four Valleys System"

    def test_search_sites_ranks_a_distinctive_paraphrase(self, server: Any) -> None:
        """Needs a fully embedded database — a paraphrase is exactly what
        keyword search alone cannot resolve. Skips rather than failing when the
        vectors are absent or partial, so a missing `matienzo embed` reads as
        "not measured" instead of "retrieval is broken"."""
        from matienzo.db.connect import connect

        connection = connect(config.DB_PATH, read_only=True)
        try:
            embedded = connection.execute("SELECT count(*) FROM chunk_vec").fetchone()[0]
            chunks = connection.execute("SELECT count(*) FROM chunk").fetchone()[0]
        except Exception:
            pytest.skip("no vector table; run `matienzo embed`")
        finally:
            connection.close()
        if embedded < chunks:
            pytest.skip(f"only {embedded:,}/{chunks:,} chunks embedded")

        hits = call(server, "search_sites", query="hole blocked by a tractor tyre", limit=3)
        assert hits[0]["site_number"] == 5005

    def test_nearby_sites_are_ordered_by_distance(self, server: Any) -> None:
        hits = call(server, "nearby_sites", site_number=105, radius_m=400)
        distances = [h["distance_m"] for h in hits]
        assert distances == sorted(distances)

    def test_site_graph_follows_edges(self, server: Any) -> None:
        graph = call(server, "site_graph", site_number=107, depth=1)
        assert graph["centre"] == 107
        assert graph["edges"]["107"]

    def test_corpus_stats_reports_totals(self, server: Any) -> None:
        stats = call(server, "corpus_stats")
        assert stats["sites"] == 5557

    def test_find_by_citation(self, server: Any) -> None:
        found = call(server, "find_by_citation", author="Corrin", limit=5)
        assert found
        assert all("Corrin" in row["author_raw"] for row in found)
