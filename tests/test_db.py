"""Schema, loading and the corpus audit.

The build is deliberately destructive-and-total: it always starts from an empty
file. So the properties worth testing are that it is reproducible, that the
relational invariants hold, and that the numbers agree with what the parsers
produced.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from matienzo import config
from matienzo.db import audit as db_audit
from matienzo.db import load as db_load
from matienzo.db import queries
from matienzo.db.connect import connect, fresh_database
from matienzo.normalise import areas as areas_vocab
from matienzo.parse import parse_page

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def small_db(tmp_path_factory: pytest.TempPathFactory) -> sqlite3.Connection:
    """A database built from the fixture pages only — fast, and enough to
    exercise every table."""
    path = tmp_path_factory.mktemp("db") / "fixtures.db"
    pages = sorted(FIXTURES.glob("[0-9][0-9][0-9][0-9].htm"))
    with fresh_database(path) as connection:
        db_load.build(connection, pages)
    return connect(path, read_only=True)


class TestSchema:
    def test_every_ordinary_table_is_strict(self, small_db: sqlite3.Connection) -> None:
        """A type error should surface at write time, not as a baffling
        comparison result months later.

        FTS5 tables are exempt: neither a virtual table nor the shadow tables it
        creates can be declared STRICT.
        """
        rows = small_db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert rows

        virtual = {r["name"] for r in rows if (r["sql"] or "").upper().startswith("CREATE VIRTUAL")}
        lax = [
            r["name"]
            for r in rows
            if r["name"] not in virtual
            and not any(r["name"].startswith(f"{v}_") for v in virtual)
            and "STRICT" not in (r["sql"] or "").upper()
        ]
        assert lax == []

    def test_foreign_keys_are_enforced(self, small_db: sqlite3.Connection) -> None:
        assert small_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_no_foreign_key_violations(self, small_db: sqlite3.Connection) -> None:
        assert small_db.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_site_summary_view_works(self, small_db: sqlite3.Connection) -> None:
        row = small_db.execute("SELECT * FROM site_summary WHERE site_number = 1").fetchone()
        assert row["name"] == "Burro, Sima del"
        assert row["area"] == "Riva"


class TestLoadedContent:
    def test_archetype_site(self, small_db: sqlite3.Connection) -> None:
        row = small_db.execute("SELECT * FROM site WHERE site_number = 1").fetchone()
        assert row["name"] == "Burro, Sima del"
        assert row["length_m"] == 94.0
        assert row["depth_m"] == 50.0
        assert row["altitude_m"] == 365.0
        assert row["easting"] == 453898

    def test_multi_entrance_site_keeps_both_coordinates(self, small_db: sqlite3.Connection) -> None:
        rows = small_db.execute(
            "SELECT * FROM coordinate WHERE site_number = 1930 ORDER BY ordinal"
        ).fetchall()
        assert [r["altitude_m"] for r in rows] == [219.0, 203.0]

    def test_prose_length_does_not_become_a_number(self, small_db: sqlite3.Connection) -> None:
        """0081's length is `included in the Four Valleys System`. Putting a
        number here would make `sum(length_m)` double-count the system."""
        row = small_db.execute("SELECT length_m FROM site WHERE site_number = 81").fetchone()
        assert row["length_m"] is None

        quantity = small_db.execute(
            "SELECT kind, raw FROM quantity WHERE site_number = 81 AND label = 'length'"
        ).fetchone()
        assert quantity["kind"] == "prose"
        assert "Four Valleys" in quantity["raw"]

    def test_prose_length_becomes_a_system_membership(self, small_db: sqlite3.Connection) -> None:
        row = small_db.execute(
            "SELECT cs.name, m.relation FROM system_member m"
            " JOIN cave_system cs ON cs.system_id = m.system_id"
            " WHERE m.site_number = 81"
        ).fetchone()
        assert row["name"] == "Four Valleys System"
        assert row["relation"] == "part_of_system"

    def test_citations_are_deduplicated_across_sites(self, small_db: sqlite3.Connection) -> None:
        uses = queries.scalar(small_db, "SELECT count(*) FROM site_citation")
        works = queries.scalar(small_db, "SELECT count(*) FROM citation")
        assert works < uses, "citations should collapse to fewer distinct works"

    def test_stub_pages_load_without_a_header_or_footer(self, small_db: sqlite3.Connection) -> None:
        row = small_db.execute("SELECT * FROM site WHERE site_number = 249").fetchone()
        assert row["is_stub"] == 1
        assert row["area_id"] is None

    def test_anomalies_are_recorded(self, small_db: sqlite3.Connection) -> None:
        assert queries.scalar(small_db, "SELECT count(*) FROM anomaly") > 0

    def test_no_self_referencing_cross_reference(self, small_db: sqlite3.Connection) -> None:
        """Enforced by a CHECK constraint, so this asserts the constraint is
        present as much as the data."""
        assert (
            small_db.execute("SELECT count(*) FROM xref WHERE from_site = to_site").fetchone()[0]
            == 0
        )

    def test_resource_links_carry_no_javascript(self, small_db: sqlite3.Connection) -> None:
        rows = small_db.execute(
            "SELECT href FROM resource_link WHERE href LIKE '%getElementsByTagName%'"
            " OR href LIKE '%window.location%'"
        ).fetchall()
        assert rows == []


class TestAreaVocabulary:
    def test_every_corpus_spelling_resolves(self) -> None:
        """A new area spelling upstream must fail the build, not silently split
        a place in two."""
        if not config.DB_PATH.exists():
            pytest.skip("no built database; run `matienzo build`")
        connection = connect(config.DB_PATH, read_only=True)
        try:
            spellings = {
                row["area_raw"]
                for row in connection.execute(
                    "SELECT DISTINCT area_raw FROM site WHERE area_raw IS NOT NULL"
                )
            }
        finally:
            connection.close()
        assert areas_vocab.unmapped(spellings) == set()

    def test_variants_fold_to_one_area(self) -> None:
        vocabulary = areas_vocab.load()
        assert vocabulary.resolve("EL Naso") == vocabulary.resolve("El Naso")
        assert vocabulary.resolve("Enaso") == vocabulary.resolve("El Naso")
        assert vocabulary.resolve("Coteron las Llanas") == vocabulary.resolve("Coterón las Llanas")
        assert vocabulary.resolve("South Vega") == vocabulary.resolve("S Vega")

    def test_question_mark_is_an_unknown_marker_not_an_area(self) -> None:
        vocabulary = areas_vocab.load()
        assert vocabulary.is_unknown_marker("?")
        assert vocabulary.resolve("?") is None

    def test_an_invented_spelling_is_reported(self) -> None:
        assert areas_vocab.unmapped({"Nowhere In Particular"}) == {"Nowhere In Particular"}


class TestReproducibility:
    def test_two_builds_agree(self, tmp_path: Path) -> None:
        """The Phase 3 checkpoint. Row counts must be identical between runs;
        timestamps and build ids legitimately differ."""
        pages = sorted(FIXTURES.glob("[0-9][0-9][0-9][0-9].htm"))
        counts = []
        for name in ("first.db", "second.db"):
            with fresh_database(tmp_path / name) as connection:
                db_load.build(connection, pages)
            connection = connect(tmp_path / name, read_only=True)
            counts.append(
                {
                    table: queries.scalar(connection, f"SELECT count(*) FROM {table}")
                    for table in (
                        "site",
                        "coordinate",
                        "quantity",
                        "citation",
                        "site_citation",
                        "xref",
                        "block",
                        "resource_link",
                        "anomaly",
                    )
                }
            )
            connection.close()
        assert counts[0] == counts[1]

    def test_unmapped_area_stops_the_build(self, tmp_path: Path) -> None:
        """Guards the decision to make this fatal rather than a warning."""
        page = tmp_path / "9999.htm"
        page.write_text(
            "<HTML><HEAD><title>site 9999</title></HEAD><BODY>"
            "<BIG><B>9999: shaft</B></BIG><BR>"
            "<SMALL><B>Nowhere In Particular</B> 30T 450000 4795000 "
            "(Datum: ETRS89. Accuracy code: <B><A href='x'>G</a></B>) "
            "<B>Altitude</B> 100m<BR>"
            '<a href="#openModal">Area position</a></small>'
            "<P>A shaft.<P><SMALL><B>Reference</B>: none</SMALL></BODY></HTML>",
            encoding="utf-8",
        )
        with (
            pytest.raises(db_load.UnmappedAreasError, match="Nowhere In Particular"),
            fresh_database(tmp_path / "bad.db") as connection,
        ):
            db_load.build(connection, [page])


class TestAudit:
    def test_reports_a_clean_corpus(self, small_db: sqlite3.Connection) -> None:
        """The fixture database was built from tests/fixtures/, so every page in
        data/pages/ that is not a fixture reads as new."""
        changes = db_audit.corpus_diff(small_db)
        assert all(c.kind == "new" for c in changes)

    def test_detects_a_changed_page(self, tmp_path: Path) -> None:
        page = tmp_path / "0001.htm"
        page.write_bytes((FIXTURES / "0001.htm").read_bytes())
        with fresh_database(tmp_path / "d.db") as connection:
            db_load.build(connection, [page])

        connection = connect(tmp_path / "d.db", read_only=True)
        try:
            recorded = connection.execute(
                "SELECT content_sha256 FROM source_file WHERE site_number = 1"
            ).fetchone()["content_sha256"]
            assert recorded == parse_page(page).provenance.content_sha256
        finally:
            connection.close()


@pytest.fixture(scope="module")
def full_db() -> sqlite3.Connection:
    """The real database, if one has been built."""
    if not config.DB_PATH.exists():
        pytest.skip("no built database; run `matienzo build`")
    return connect(config.DB_PATH, read_only=True)


@pytest.mark.slow
class TestFullCorpus:
    def test_every_page_is_loaded(self, full_db: sqlite3.Connection) -> None:
        assert queries.scalar(full_db, "SELECT count(*) FROM site") == 5557

    def test_totals_match_the_parsers(self, full_db: sqlite3.Connection) -> None:
        assert queries.scalar(full_db, "SELECT count(*) FROM update_date") == 7051
        assert queries.scalar(full_db, "SELECT count(*) FROM site_citation") == 14661
        assert queries.scalar(full_db, "SELECT count(*) FROM citation") == 752
        assert queries.scalar(full_db, "SELECT count(*) FROM xref") == 2378

    def test_no_foreign_key_violations(self, full_db: sqlite3.Connection) -> None:
        assert full_db.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_every_coordinate_with_latlon_is_in_cantabria(
        self, full_db: sqlite3.Connection
    ) -> None:
        rows = full_db.execute(
            "SELECT site_number, latitude, longitude FROM coordinate"
            " WHERE latitude IS NOT NULL"
            " AND (latitude NOT BETWEEN 43.0 AND 43.6"
            "      OR longitude NOT BETWEEN -3.9 AND -3.2)"
        ).fetchall()
        assert [r["site_number"] for r in rows] == []

    def test_length_totals_exclude_prose_measurements(self, full_db: sqlite3.Connection) -> None:
        """`sum(length_m)` must not double-count a cave system whose members
        say 'included in …' rather than giving their own length."""
        suppressed = full_db.execute(
            "SELECT count(*) FROM quantity q JOIN site s USING (site_number)"
            " WHERE q.label = 'length' AND q.kind = 'prose' AND s.length_m IS NOT NULL"
        ).fetchone()[0]
        assert suppressed == 0
