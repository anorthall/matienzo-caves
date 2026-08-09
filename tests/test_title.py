"""Heading parsing, with emphasis on the nested-parenthesis alias scan."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from matienzo import config
from matienzo.anomaly import SECTIONS, AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import read_document
from matienzo.models import AliasKind, Title
from matienzo.parse.segment import segment
from matienzo.parse.title import infer_site_type, parse_title, sort_key, split_aliases


def _title(path: Path) -> tuple[Title | None, AnomalyRecorder]:
    recorder = AnomalyRecorder()
    confidence = ConfidenceScorer(SECTIONS)
    segments = segment(read_document(path, recorder=recorder), recorder)
    assert segments.title is not None
    return parse_title(segments.title.text, segments.site_number, recorder, confidence), recorder


@pytest.fixture
def title(fixture_path: Callable[[int], Path]) -> Callable[[int], Title]:
    def _run(site: int) -> Title:
        parsed, _ = _title(fixture_path(site))
        assert parsed is not None
        return parsed

    return _run


class TestSplitAliases:
    """The depth counter, tested directly — `\\(([^)]*)\\)` gets 61 pages wrong."""

    def test_nested_parentheses_stay_whole(self) -> None:
        name, aliases, balanced = split_aliases("Torca (3424 (French: SCD))")
        assert (name, aliases, balanced) == ("Torca", ["3424 (French: SCD)"], True)

    def test_multiple_top_level_groups(self) -> None:
        name, aliases, _ = split_aliases("Comellantes, Cueva del (Comediante) (Comellante)")
        assert (name, aliases) == ("Comellantes, Cueva del", ["Comediante", "Comellante"])

    def test_no_aliases(self) -> None:
        assert split_aliases("Burro, Sima del") == ("Burro, Sima del", [], True)

    def test_unclosed_bracket_keeps_its_content_and_reports_imbalance(self) -> None:
        name, aliases, balanced = split_aliases("shaft (unclosed")
        assert (name, aliases, balanced) == ("shaft", ["unclosed"], False)

    def test_stray_closing_bracket_is_dropped_not_fatal(self) -> None:
        name, aliases, balanced = split_aliases("shaft) stray")
        assert (name, aliases, balanced) == ("shaft stray", [], False)


class TestRealHeadings:
    def test_archetype(self, title: Callable[[int], Title]) -> None:
        parsed = title(1)
        assert parsed.name == "Burro, Sima del"
        assert parsed.separator == "colon"
        assert parsed.aliases == []

    def test_space_separator(self, title: Callable[[int], Title]) -> None:
        parsed = title(7)
        assert parsed.name == "West Ozana Pots"
        assert parsed.separator == "space"

    def test_space_separator_is_flagged(self, fixture_path: Callable[[int], Path]) -> None:
        _, recorder = _title(fixture_path(7))
        assert recorder.has(AnomalyCode.TITLE_SPACE_SEPARATOR)

    def test_french_catalogue_reference(self, title: Callable[[int], Title]) -> None:
        alias = title(2).aliases[0]
        assert alias.text == "3424 (French: SCD)"
        assert alias.kind is AliasKind.FRENCH_REF
        assert alias.site_number is None, "a French club number is not a site number"

    def test_two_spanish_alternates(self, title: Callable[[int], Title]) -> None:
        parsed = title(105)
        assert parsed.name == "Riaño, Cueva de"
        assert [a.text for a in parsed.aliases] == ["Riaño 1, Cueva de", "Reñada, Cueva de la"]
        assert {a.kind for a in parsed.aliases} == {AliasKind.SPANISH_ALT}

    def test_entrance_qualifier_and_system_name(self, title: Callable[[int], Title]) -> None:
        kinds = [a.kind for a in title(5).aliases]
        assert kinds == [AliasKind.ENTRANCE_QUALIFIER, AliasKind.SYSTEM_NAME]

    def test_reallocated_placeholder_name_is_flagged(
        self, fixture_path: Callable[[int], Path]
    ) -> None:
        parsed, recorder = _title(fixture_path(249))
        assert parsed is not None
        assert parsed.name == "-"
        assert recorder.has(AnomalyCode.TITLE_NAME_EMPTY)

    def test_site_number_comes_from_the_filename(self, title: Callable[[int], Title]) -> None:
        assert title(1930).site_number == 1930


class TestSortKey:
    """Spanish headings are written head-noun-last, which sorts uselessly."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Burro, Sima del", "sima del burro"),
            ("Riaño, Cueva de", "cueva de riano"),
            ("Coverón, Cueva del", "cueva del coveron"),
            ("West Ozana Pots", "west ozana pots"),
            ("shaft", "shaft"),
        ],
    )
    def test_deinverts_and_unaccents(self, name: str, expected: str) -> None:
        assert sort_key(name) == expected


class TestSiteType:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("shaft", "shaft"),
            ("Shaft", "shaft"),
            ("shafts - 2", "shafts"),
            ("A dig", "dig"),
            ("resurgence & sink", "resurgence & sink"),
            ("Burro, Sima del", None),
        ],
    )
    def test_generic_descriptors_only(self, name: str, expected: str | None) -> None:
        assert infer_site_type(name) == expected


@pytest.mark.slow
class TestWholeCorpus:
    def test_every_heading_parses(self) -> None:
        failed = []
        for path in config.iter_page_paths():
            parsed, _ = _title(path)
            if parsed is None:
                failed.append(path.stem)
        assert failed == []

    def test_only_the_known_upstream_typo_disagrees_with_its_filename(self) -> None:
        """`5255.htm` is headed `5254: cave (2955 (French: SCD))` — a copy-paste
        slip upstream; its `<title>` says `site 5255`. The filename wins.

        Pinned as an exact set so a *new* mismatch fails rather than hiding
        behind this one.
        """
        mismatched = [
            path.stem
            for path in config.iter_page_paths()
            if _title(path)[1].has(AnomalyCode.TITLE_NUMBER_MISMATCH)
        ]
        assert mismatched == ["5255"]

    def test_no_unbalanced_parentheses(self) -> None:
        unbalanced = []
        for path in config.iter_page_paths():
            _, recorder = _title(path)
            if recorder.has(AnomalyCode.UNBALANCED_PARENS):
                unbalanced.append(path.stem)
        assert unbalanced == []
