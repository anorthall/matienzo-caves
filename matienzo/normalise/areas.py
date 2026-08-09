"""Resolve area names to canonical places.

The corpus spells roughly 80 places 89 different ways. Most of the difference is
case and accents (`Coteron` / `Coterón`, `EL Naso`), which fold away
mechanically. The rest are listed explicitly in `data/vocab/areas.toml` because
they need judgement — `Enaso` run together, `San Ant8nio` with an 8 typed for an
o, `Las Garma` where Spanish grammar says `La Garma`.

An unrecognised spelling is an error rather than a new area. Left to itself, a
fresh variant would quietly split a place in two and every count that mentions
it would be wrong; the build stopping is the cheaper outcome.
"""

from __future__ import annotations

import tomllib
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from matienzo import config


@dataclass(frozen=True, slots=True)
class Area:
    name: str
    slug: str


@dataclass(frozen=True, slots=True)
class AreaVocabulary:
    """Canonical areas and every spelling that maps to one."""

    areas: tuple[Area, ...]
    by_folded: dict[str, Area]
    unknown_markers: frozenset[str]

    def resolve(self, raw: str | None) -> Area | None:
        """The canonical area for a raw spelling, or None if it is unknown.

        Returns None both for a genuinely absent area and for the literal `?`
        two pages use; the caller distinguishes them via `is_unknown_marker`.
        """
        if raw is None:
            return None
        cleaned = " ".join(raw.split())
        if cleaned in self.unknown_markers:
            return None
        return self.by_folded.get(fold(cleaned))

    def is_unknown_marker(self, raw: str | None) -> bool:
        return raw is not None and " ".join(raw.split()) in self.unknown_markers


def fold(text: str) -> str:
    """Lowercase, unaccent and collapse whitespace, for variant matching."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


@cache
def load(path: Path | None = None) -> AreaVocabulary:
    """Read `data/vocab/areas.toml`. Cached; the file is read once per process."""
    source = path or config.VOCAB_DIR / "areas.toml"
    data = tomllib.loads(source.read_text(encoding="utf-8"))

    areas: list[Area] = []
    by_folded: dict[str, Area] = {}
    for name, entry in data.get("canonical", {}).items():
        area = Area(name=name, slug=entry["slug"])
        areas.append(area)
        for spelling in (name, *entry.get("variants", ())):
            by_folded[fold(spelling)] = area

    return AreaVocabulary(
        areas=tuple(sorted(areas, key=lambda a: a.name)),
        by_folded=by_folded,
        unknown_markers=frozenset(data.get("unknown", {}).get("markers", ())),
    )


def unmapped(raw_areas: set[str], vocabulary: AreaVocabulary | None = None) -> set[str]:
    """Spellings in the corpus that resolve to no canonical area.

    Asserted to be empty by the corpus tests: that turns "a new area spelling
    appeared upstream" from invisible data drift into a red build.
    """
    vocab = vocabulary or load()
    return {
        raw
        for raw in raw_areas
        if raw and not vocab.is_unknown_marker(raw) and vocab.resolve(raw) is None
    }
