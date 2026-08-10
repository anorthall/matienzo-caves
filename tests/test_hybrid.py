"""Embeddings, hybrid fusion and the retrieval evaluation.

The tests that need vectors are skipped when the `embed` extra is not
installed or the database has not been embedded — but they skip loudly rather
than passing vacuously, because a hybrid search that quietly stopped being
hybrid looks exactly like one that works.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from matienzo import config, evaluate, search
from matienzo.db.connect import connect

pytest.importorskip("sqlite_vec", reason="needs `uv sync --extra embed`")


@pytest.fixture(scope="module")
def db() -> sqlite3.Connection:
    if not config.DB_PATH.exists():
        pytest.skip("no built database; run `matienzo build`")
    connection = connect(config.DB_PATH, read_only=True)
    from matienzo import embed

    if not embed.is_available(connection):
        pytest.skip("database has no embeddings; run `matienzo embed`")
    return connection


class TestQueryGrammar:
    def test_terms_are_joined_with_or_not_and(self) -> None:
        """FTS5 reads adjacent terms as AND. Juxtaposition made every
        multi-word descriptive query return nothing at all."""
        assert search.escape_query("goat shelter") == '"goat" OR "shelter"'

    def test_a_long_natural_language_query_still_matches(self, db: sqlite3.Connection) -> None:
        found = search.search_passages(db, "a cave used as a shelter for goats", limit=5)
        assert found, "an AND-joined query would return nothing here"

    def test_special_characters_are_stripped(self) -> None:
        assert search.escape_query("Papá Noel (2)") == '"Papá" OR "Noel" OR "2"'


class TestVectorSearch:
    def test_returns_ranked_passages(self, db: sqlite3.Connection) -> None:
        found = search.search_vectors(db, "bats roosting in a cave", limit=10)
        assert found
        assert [p.score for p in found] == sorted((p.score for p in found), reverse=True)

    def test_finds_a_paraphrase_keyword_search_cannot(self, db: sqlite3.Connection) -> None:
        """The reason vectors are here at all: wording the corpus never uses."""
        found = search.search_vectors(db, "a hole plugged by an old rubber wheel", limit=20)
        assert 5005 in [p.site_number for p in found], "Tractor Tyre Shaft"

    def test_filters_are_applied(self, db: sqlite3.Connection) -> None:
        found = search.search_vectors(
            db, "deep shaft", filters=search.Filters(area="Vega"), limit=20
        )
        assert found
        assert all(p.area and "Vega" in p.area for p in found)


class TestHybrid:
    def test_fuses_both_retrievers(self, db: sqlite3.Connection) -> None:
        found = search.search_hybrid(db, "optical brightener", limit=10)
        assert found
        assert [p.score for p in found] == sorted((p.score for p in found), reverse=True)

    def test_a_distinctive_paraphrase_lands_first(self, db: sqlite3.Connection) -> None:
        hits = search.search_sites(db, "hole blocked by a discarded tractor tyre", hybrid=True)
        assert hits[0].site_number == 5005

    def test_an_exact_name_still_wins(self, db: sqlite3.Connection) -> None:
        """Fusion must not dilute what keyword search is best at."""
        hits = search.search_sites(db, "Fuente Aguanaz", hybrid=True, limit=5)
        assert 713 in [h.site_number for h in hits]

    def test_weights_are_parameters_not_constants(self, db: sqlite3.Connection) -> None:
        """Exposed so a larger eval set can revisit them; equal by default
        because a sweep found the differences to be noise."""
        assert search.KEYWORD_WEIGHT == search.VECTOR_WEIGHT == 1.0
        skewed = search.search_hybrid(db, "shaft", limit=5, keyword_weight=0.1)
        assert skewed

    def test_falls_back_when_there_are_no_vectors(self, tmp_path: Path) -> None:
        """A database with chunks but no embeddings must still search."""
        from matienzo.db import load as db_load
        from matienzo.db.connect import fresh_database

        pages = sorted((config.REPO_ROOT / "tests" / "fixtures").glob("[0-9]*.htm"))[:8]
        path = tmp_path / "novec.db"
        with fresh_database(path) as connection:
            db_load.build(connection, pages)

        connection = connect(path, read_only=True)
        try:
            assert search.search_hybrid(connection, "shaft", limit=5)
        finally:
            connection.close()


class TestEvaluationSet:
    def test_every_expected_site_exists(self, db: sqlite3.Connection) -> None:
        """Ground truth that points at a missing site would score as a
        permanent, unexplained miss."""
        known = {row["site_number"] for row in db.execute("SELECT site_number FROM site")}
        for query in evaluate.load_queries():
            missing = set(query.expect) - known
            assert not missing, f"{query.text!r} expects unknown sites {missing}"

    def test_queries_are_grouped_by_kind(self) -> None:
        kinds = {q.kind for q in evaluate.load_queries()}
        assert kinds == {"name", "description", "metadata"}

    def test_the_set_is_big_enough_to_mean_something(self) -> None:
        assert len(evaluate.load_queries()) >= 25

    @pytest.mark.slow
    def test_hybrid_is_not_worse_than_both_single_strategies(self, db: sqlite3.Connection) -> None:
        """The Phase 6 checkpoint. Compared at every cut-off: hybrid can trail
        on recall@1 while clearly winning at recall@10, and judging on one
        number would call that a failure."""
        results = evaluate.evaluate(db, evaluate.load_queries())
        hybrid = results["hybrid"]
        singles = (results["keyword"], results["vector"])

        losses = [
            cut for cut in (1, 5, 10) if hybrid.recall(cut) < max(s.recall(cut) for s in singles)
        ]
        assert losses != [1, 5, 10], "hybrid loses everywhere; suspect the chunking"

    @pytest.mark.slow
    def test_name_queries_are_answered_reliably(self, db: sqlite3.Connection) -> None:
        """Looking a cave up by name is the commonest thing anyone does here,
        and it should essentially always work."""
        names = [q for q in evaluate.load_queries() if q.kind == "name"]
        results = evaluate.evaluate(db, names)
        assert results["hybrid"].recall(5) >= 0.9
