"""Measure retrieval quality across the three strategies.

The plan asked for an expert-written query set. This one is not that: it is
derived from the corpus, so every expected answer is verifiable against the
database rather than remembered. That makes it honest about *ranking* — whether
the right cave comes back near the top — but it cannot judge whether the corpus
answers a caver's real questions. Those two are different things, and this only
measures the first. A hand-written set from someone who knows the caves would
still be worth having.

Queries are grouped by what they exercise, because the strategies differ by
kind and an aggregate number hides that: keyword search should win on names,
vectors on descriptions of a thing whose name the reader does not know.
"""

from __future__ import annotations

import sqlite3
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from matienzo import config, search


@dataclass(frozen=True, slots=True)
class Query:
    text: str
    expect: tuple[int, ...]
    """Site numbers that count as correct. Any one of them scores a hit."""
    kind: str
    note: str = ""


@dataclass(slots=True)
class Result:
    strategy: str
    hits_at_1: int = 0
    hits_at_5: int = 0
    hits_at_10: int = 0
    total: int = 0
    misses: list[str] = field(default_factory=list)

    def recall(self, at: int) -> float:
        hits = {1: self.hits_at_1, 5: self.hits_at_5, 10: self.hits_at_10}[at]
        return hits / self.total if self.total else 0.0


def load_queries(path: Path | None = None) -> list[Query]:
    source = path or config.DATA_DIR / "eval" / "queries.toml"
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    return [
        Query(
            text=entry["query"],
            expect=tuple(entry["expect"]),
            kind=entry.get("kind", "general"),
            note=entry.get("note", ""),
        )
        for entry in data.get("query", ())
    ]


def strategies() -> dict[str, Callable[[sqlite3.Connection, str, int], list[int]]]:
    """The three retrieval paths, each reduced to a ranked list of site numbers."""

    def keyword(connection: sqlite3.Connection, query: str, limit: int) -> list[int]:
        return _sites(search.search_passages(connection, query, limit=limit * 6), limit)

    def vector(connection: sqlite3.Connection, query: str, limit: int) -> list[int]:
        return _sites(search.search_vectors(connection, query, limit=limit * 6), limit)

    def hybrid(connection: sqlite3.Connection, query: str, limit: int) -> list[int]:
        return _sites(search.search_hybrid(connection, query, limit=limit * 6), limit)

    return {"keyword": keyword, "vector": vector, "hybrid": hybrid}


def _sites(passages: list[search.Passage], limit: int) -> list[int]:
    """Collapse ranked passages to ranked distinct sites, preserving order."""
    seen: list[int] = []
    for passage in passages:
        if passage.site_number not in seen:
            seen.append(passage.site_number)
        if len(seen) >= limit:
            break
    return seen


def evaluate(
    connection: sqlite3.Connection, queries: list[Query], *, at: int = 10
) -> dict[str, Result]:
    results = {name: Result(strategy=name) for name in strategies()}

    for query in queries:
        for name, run in strategies().items():
            ranked = run(connection, query.text, at)
            result = results[name]
            result.total += 1
            expected = set(query.expect)
            if ranked[:1] and expected & set(ranked[:1]):
                result.hits_at_1 += 1
            if expected & set(ranked[:5]):
                result.hits_at_5 += 1
            if expected & set(ranked[:at]):
                result.hits_at_10 += 1
            else:
                result.misses.append(query.text)

    return results


def by_kind(connection: sqlite3.Connection, queries: list[Query]) -> dict[str, dict[str, Result]]:
    """Scores split by query kind, which is where the strategies differ."""
    kinds = sorted({q.kind for q in queries})
    return {kind: evaluate(connection, [q for q in queries if q.kind == kind]) for kind in kinds}
