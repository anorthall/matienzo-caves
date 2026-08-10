"""The shared tool layer.

Two things are worth testing here and they are different in kind. The handlers
are ordinary functions over a connection, so they can be exercised against the
fixture corpus directly — something the previous closure-based tools could not
be. The registry is a contract between two adapters, so what matters is that
neither can quietly drift away from it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from matienzo import links, tools
from matienzo.db import load as db_load
from matienzo.db.connect import connect, fresh_database

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> sqlite3.Connection:
    path = tmp_path_factory.mktemp("tools") / "tools.db"
    pages = sorted(FIXTURES.glob("[0-9][0-9][0-9][0-9].htm"))
    with fresh_database(path) as connection:
        db_load.build(connection, pages)
    return connect(path, read_only=True)


class TestRegistry:
    def test_every_tool_is_described(self) -> None:
        """Descriptions are the only thing telling an agent which tool to reach
        for, so an undocumented one is effectively invisible."""
        for spec in tools.REGISTRY:
            assert len(spec.description) > 40, spec.name

    def test_names_are_unique(self) -> None:
        assert len(tools.BY_NAME) == len(tools.REGISTRY)

    def test_schemas_are_strict(self) -> None:
        """`additionalProperties: false` plus an explicit `required` is what the
        Anthropic API's strict mode needs; a schema missing either validates
        loosely and lets an invented parameter through."""
        for spec in tools.REGISTRY:
            assert spec.input_schema["additionalProperties"] is False, spec.name
            assert "required" in spec.input_schema, spec.name

    def test_required_parameters_exist(self) -> None:
        for spec in tools.REGISTRY:
            properties = spec.input_schema["properties"]
            assert set(spec.input_schema["required"]) <= set(properties), spec.name

    def test_the_registry_order_is_fixed(self) -> None:
        """`REGISTRY` is a tuple because the Anthropic request renders `tools`
        before `system`, so a reordering would invalidate the prompt cache on
        every request — a silent tripling of the bill, not an error."""
        assert isinstance(tools.REGISTRY, tuple)


class TestHandlers:
    def test_get_site_assembles_the_record(self, corpus: sqlite3.Connection) -> None:
        record = tools.get_site(corpus, site_number=1930, include_description=False)
        assert record["name"] == "Cobadal, Sumidero de"
        assert record["url"] == "https://www.matienzocaves.org.uk/descrip/1930.htm"

    def test_get_site_reports_a_missing_number_rather_than_failing(
        self, corpus: sqlite3.Connection
    ) -> None:
        assert "error" in tools.get_site(corpus, site_number=9999)

    def test_search_passages_returns_evidence(self, corpus: sqlite3.Connection) -> None:
        found = tools.search_passages(corpus, query="entrance shaft", limit=5)
        assert found
        assert all(p["url"].endswith(".htm") for p in found)

    def test_unknown_arguments_are_dropped_rather_than_raising(
        self, corpus: sqlite3.Connection
    ) -> None:
        """A model that invents a parameter should get an answer to the question
        it could express, not a TypeError it has no way to interpret."""
        outcome = tools.call(
            tools.BY_NAME["get_site"],
            corpus,
            {"site_number": 1930, "include_description": False, "colour": "blue"},
        )
        assert outcome.payload["name"] == "Cobadal, Sumidero de"


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
    def test_writes_are_refused(self, corpus: sqlite3.Connection, query: str) -> None:
        assert "error" in tools.sql(corpus, query=query)

    def test_a_write_smuggled_after_a_select_is_refused(self, corpus: sqlite3.Connection) -> None:
        assert "error" in tools.sql(corpus, query="SELECT 1; DROP TABLE site")

    def test_reads_are_allowed(self, corpus: sqlite3.Connection) -> None:
        result = tools.sql(corpus, query="SELECT count(*) AS n FROM site")
        assert result["rows"][0]["n"] > 0

    def test_a_runaway_query_is_cancelled_rather_than_running_to_completion(
        self, corpus: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row limit caps what comes back, not what the query costs. A
        cross join returns one row after doing unbounded work, so without a
        deadline a single request can pin a CPU on a public endpoint."""
        monkeypatch.setattr(tools, "SQL_TIMEOUT_SECONDS", 0.05)
        result = tools.sql(
            corpus,
            query=(
                "SELECT count(*) AS n FROM resource_link a, resource_link b,"
                " resource_link c, resource_link d"
            ),
        )
        assert "error" in result
        assert "interrupt" in result["error"].lower()


class TestSiteNumbers:
    """Provenance is derived from payloads rather than from the model's account
    of them — that derivation is what makes a rendered citation trustworthy."""

    def test_a_list_of_hits(self) -> None:
        payload = [{"site_number": 1930}, {"site_number": 733}]
        assert tools.site_numbers(payload) == (1930, 733)

    def test_a_nested_graph(self) -> None:
        payload = {"centre": 107, "edges": {"107": [{"site_number": 108}]}}
        assert tools.site_numbers(payload) == (107, 108)

    def test_duplicates_collapse_in_first_seen_order(self) -> None:
        payload = [{"site_number": 5}, {"site_number": 3}, {"site_number": 5}]
        assert tools.site_numbers(payload) == (5, 3)

    def test_a_payload_with_no_sites(self) -> None:
        assert tools.site_numbers({"rows": [{"n": 5557}]}) == ()


class TestLinks:
    def test_site_numbers_are_zero_padded(self) -> None:
        """An unpadded number 404s — the padding is not cosmetic."""
        assert links.site_url(81) == "https://www.matienzocaves.org.uk/descrip/0081.htm"

    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            ("1930.htm", "https://www.matienzocaves.org.uk/descrip/1930.htm"),
            ("../surveys/x.pdf", "https://www.matienzocaves.org.uk/surveys/x.pdf"),
            ("/photos/y.jpg", "https://www.matienzocaves.org.uk/photos/y.jpg"),
            ("http://elsewhere.example/z", "http://elsewhere.example/z"),
        ],
    )
    def test_relative_hrefs_resolve_against_the_descrip_directory(
        self, href: str, expected: str
    ) -> None:
        assert links.resource_url(href) == expected

    def test_the_scraper_and_the_link_helper_agree(self) -> None:
        """`scripts/download_htm.py` is a standalone PEP-723 script and cannot
        import `matienzo`, so the base URL is written twice. This is the only
        way they can silently diverge."""
        scraper = Path(__file__).parent.parent / "scripts" / "download_htm.py"
        assert links.SITE_BASE in scraper.read_text(encoding="utf-8")
