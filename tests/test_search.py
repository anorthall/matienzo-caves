"""Chunking and full-text search."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from matienzo import config, search
from matienzo.chunk import MAX_CHARS, TARGET_CHARS, chunk_site
from matienzo.db import load as db_load
from matienzo.db.connect import connect, fresh_database
from matienzo.parse import parse_page

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> sqlite3.Connection:
    path = tmp_path_factory.mktemp("search") / "search.db"
    pages = sorted(FIXTURES.glob("[0-9][0-9][0-9][0-9].htm"))
    with fresh_database(path) as connection:
        db_load.build(connection, pages)
    return connect(path, read_only=True)


class TestChunking:
    def test_the_first_chunk_is_always_a_card(self) -> None:
        chunks = chunk_site(parse_page(FIXTURES / "0001.htm"))
        assert chunks[0].kind == "card"
        assert chunks[0].ordinal == 0

    def test_the_card_states_facts_the_prose_never_mentions(self) -> None:
        """This is what makes "shafts in Riva over 50 m deep" retrievable: the
        area and the measurements live in header fields, not in the text."""
        card = chunk_site(parse_page(FIXTURES / "0001.htm"))[0]
        assert "Riva" in card.text
        assert "94m" in card.text
        assert "50m" in card.text

    def test_the_card_records_system_membership(self) -> None:
        card = chunk_site(parse_page(FIXTURES / "0081.htm"))[0]
        assert "Four Valleys System" in card.text

    def test_every_chunk_carries_its_context(self) -> None:
        """A paragraph deep in a long description is otherwise an anonymous
        passage about mud and boulders."""
        for chunk in chunk_site(parse_page(FIXTURES / "1930.htm")):
            assert chunk.text.startswith("Site 1930")

    def test_section_headings_reach_the_chunk_prefix(self) -> None:
        chunks = chunk_site(parse_page(FIXTURES / "1930.htm"))
        headings = {c.section_heading for c in chunks if c.section_heading}
        assert "Wessex Inlet" in headings
        wessex = next(c for c in chunks if c.section_heading == "Wessex Inlet")
        assert "Wessex Inlet" in wessex.text.splitlines()[0]

    def test_sections_are_hard_boundaries(self) -> None:
        """`Wessex Inlet` must never share a chunk with `Passage of Vom`.

        Checked by block range: every prose chunk must sit wholly inside one
        section, so no two sections can claim the same block.
        """
        record = parse_page(FIXTURES / "1930.htm")
        owner: dict[int, str | None] = {}
        for chunk in chunk_site(record):
            if chunk.block_first is None or chunk.block_last is None:
                continue
            for block in range(chunk.block_first, chunk.block_last + 1):
                assert owner.setdefault(block, chunk.section_heading) == chunk.section_heading

    def test_a_one_line_site_becomes_exactly_one_prose_chunk(self) -> None:
        """1,220 sites are a single sentence. Padding or merging them would turn
        precise matches into noise."""
        chunks = chunk_site(parse_page(FIXTURES / "0015.htm"))
        prose = [c for c in chunks if c.kind == "prose"]
        assert len(prose) == 1

    def test_chunks_stay_within_the_hard_limit(self) -> None:
        for name in ("0733.htm", "2889.htm", "0107.htm"):
            for chunk in chunk_site(parse_page(FIXTURES / name)):
                body = chunk.text.split("\n\n", 1)[-1]
                assert len(body) <= MAX_CHARS * 1.5, (name, chunk.kind, len(body))

    def test_long_pages_produce_many_chunks(self) -> None:
        chunks = chunk_site(parse_page(FIXTURES / "0733.htm"))
        assert len(chunks) > 20

    def test_a_stub_still_produces_a_card(self) -> None:
        chunks = chunk_site(parse_page(FIXTURES / "4540.htm"))
        assert len(chunks) >= 1
        assert chunks[0].kind == "card"

    def test_content_hash_is_stable(self) -> None:
        first = chunk_site(parse_page(FIXTURES / "0001.htm"))[0]
        second = chunk_site(parse_page(FIXTURES / "0001.htm"))[0]
        assert first.content_sha256 == second.content_sha256

    def test_target_is_below_the_hard_limit(self) -> None:
        assert TARGET_CHARS < MAX_CHARS


class TestQueryEscaping:
    @pytest.mark.parametrize(
        "query",
        ["Papá Noel", "shaft - 2", 'a "quoted" term', "Cueva (del) Risco", "NOT AND OR"],
    )
    def test_user_input_never_becomes_a_syntax_error(
        self, indexed: sqlite3.Connection, query: str
    ) -> None:
        """Cave names are full of characters FTS5 treats as operators."""
        search.search_passages(indexed, query, limit=3)

    def test_an_empty_query_returns_nothing(self, indexed: sqlite3.Connection) -> None:
        assert search.search_passages(indexed, "   ") == []


class TestSearch:
    def test_finds_a_site_by_name(self, indexed: sqlite3.Connection) -> None:
        hits = search.search_sites(indexed, "Burro", limit=5)
        assert 1 in [h.site_number for h in hits]

    def test_accents_are_ignored(self, indexed: sqlite3.Connection) -> None:
        """The most common way a search of this corpus fails."""
        hits = search.search_sites(indexed, "riano", limit=10)
        assert 105 in [h.site_number for h in hits]

    def test_accented_spelling_works_too(self, indexed: sqlite3.Connection) -> None:
        hits = search.search_sites(indexed, "Riaño", limit=10)
        assert 105 in [h.site_number for h in hits]

    def test_unaccented_author_name(self, indexed: sqlite3.Connection) -> None:
        passages = search.search_passages(indexed, "fernandez", limit=5)
        assert passages

    def test_passages_carry_their_site_and_section(self, indexed: sqlite3.Connection) -> None:
        passages = search.search_passages(indexed, "entrance", limit=5)
        assert passages
        assert all(p.site_number > 0 for p in passages)

    def test_snippets_use_guillemets_not_brackets(self, indexed: sqlite3.Connection) -> None:
        """Square brackets are console-markup syntax; a bracketed snippet
        silently disappears from CLI output."""
        passages = search.search_passages(indexed, "shaft", limit=5)
        matched = [p for p in passages if "«" in p.snippet]
        assert matched
        assert all("[" not in p.snippet for p in passages)

    def test_a_site_scores_above_its_own_passages(self, indexed: sqlite3.Connection) -> None:
        """Supporting passages add to a site's score, so a cave mentioned
        strongly once outranks one mentioned weakly five times."""
        hits = search.search_sites(indexed, "shaft", limit=10)
        assert hits == sorted(hits, key=lambda h: h.score, reverse=True)


class TestFilters:
    def test_area_filter(self, indexed: sqlite3.Connection) -> None:
        hits = search.search_sites(indexed, "cave", filters=search.Filters(area="Riaño"), limit=20)
        assert hits
        assert all(h.area == "Riaño" for h in hits)

    def test_depth_filter(self, indexed: sqlite3.Connection) -> None:
        hits = search.search_sites(
            indexed, "shaft", filters=search.Filters(min_depth_m=40), limit=20
        )
        assert all(h.depth_m is None or h.depth_m >= 40 for h in hits)

    def test_filters_are_pushed_into_the_query(self, indexed: sqlite3.Connection) -> None:
        """Applied after fusion, a narrow filter over a top-k chosen without it
        usually removes every candidate."""
        wide = search.search_sites(indexed, "cave", limit=50)
        narrow = search.search_sites(
            indexed, "cave", filters=search.Filters(area="Riaño"), limit=50
        )
        assert narrow, "a filtered search must still return its matches"
        assert len(narrow) <= len(wide)


class TestGeography:
    def test_nearby_returns_sites_in_distance_order(self, indexed: sqlite3.Connection) -> None:
        hits = search.nearby(indexed, 105, radius_m=100_000, limit=10)
        assert hits
        assert [h.score for h in hits] == sorted(h.score for h in hits)

    def test_a_site_is_not_its_own_neighbour(self, indexed: sqlite3.Connection) -> None:
        assert 105 not in [h.site_number for h in search.nearby(indexed, 105, radius_m=100_000)]

    def test_a_site_without_coordinates_has_no_neighbours(
        self, indexed: sqlite3.Connection
    ) -> None:
        assert search.nearby(indexed, 249) == []


class TestGraph:
    def test_neighbourhood_follows_edges_both_ways(self, indexed: sqlite3.Connection) -> None:
        graph = search.neighbourhood(indexed, 105, depth=1)
        assert graph[105]

    def test_depth_two_reaches_further(self, indexed: sqlite3.Connection) -> None:
        shallow = search.neighbourhood(indexed, 105, depth=1)
        deep = search.neighbourhood(indexed, 105, depth=2)
        assert len(deep) >= len(shallow)


@pytest.fixture(scope="module")
def db() -> sqlite3.Connection:
    if not config.DB_PATH.exists():
        pytest.skip("no built database; run `matienzo build`")
    return connect(config.DB_PATH, read_only=True)


@pytest.mark.slow
class TestFullCorpusSearch:
    def test_every_site_has_at_least_one_chunk(self, db: sqlite3.Connection) -> None:
        missing = db.execute(
            "SELECT count(*) FROM site s WHERE NOT EXISTS"
            " (SELECT 1 FROM chunk c WHERE c.site_number = s.site_number)"
        ).fetchone()[0]
        assert missing == 0

    def test_chunk_count_is_reasonable(self, db: sqlite3.Connection) -> None:
        total = db.execute("SELECT count(*) FROM chunk").fetchone()[0]
        assert 10_000 < total < 60_000

    def test_the_fts_index_matches_the_chunk_table(self, db: sqlite3.Connection) -> None:
        chunks = db.execute("SELECT count(*) FROM chunk").fetchone()[0]
        indexed = db.execute("SELECT count(*) FROM chunk_fts").fetchone()[0]
        assert chunks == indexed

    def test_a_known_fact_is_findable(self, db: sqlite3.Connection) -> None:
        """Fuente Aguanaz is linked to Duck Pond Sink by a dye trace."""
        hits = search.search_sites(db, "optical brightener", limit=10)
        assert 713 in [h.site_number for h in hits]
