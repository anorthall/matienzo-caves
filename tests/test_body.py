"""Description parsing.

Most of the weight is on section detection. There are no heading tags anywhere
in the corpus, so a heading is inferred from a short emphasised run standing
alone — and the same `<b>` is used for mid-sentence dates. Getting that wrong
invents sections and mis-assigns every paragraph after them, while still
producing output that reads perfectly well.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest

from matienzo import config
from matienzo.anomaly import SECTIONS, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import read_document
from matienzo.models import BlockKind, Body
from matienzo.parse.body import parse_body
from matienzo.parse.segment import segment


def _body(path: Path, site_number: int | None = None) -> Body:
    recorder = AnomalyRecorder()
    segments = segment(read_document(path, recorder=recorder), recorder)
    return parse_body(
        segments.body.text,
        site_number if site_number is not None else segments.site_number,
        recorder,
        ConfidenceScorer(SECTIONS),
    )


@pytest.fixture
def body(fixture_path: Callable[[int], Path]) -> Callable[[int], Body]:
    def _run(site: int) -> Body:
        return _body(fixture_path(site))

    return _run


def parse_fragment(html: str, site_number: int = 1) -> Body:
    return parse_body(html, site_number, AnomalyRecorder(), ConfidenceScorer(SECTIONS))


class TestSectionHeadings:
    def test_bold_heading_alone_in_its_block(self) -> None:
        body = parse_fragment("<P><B>Hydrology</B><BR>Water sinks here.")
        assert [s.heading for s in body.sections] == ["Hydrology"]
        assert body.sections[0].canonical == "hydrology"

    def test_underlined_headings(self, body: Callable[[int], Body]) -> None:
        """1930 underlines its section titles rather than bolding them."""
        sections = body(1930).sections
        assert [s.heading for s in sections][:3] == [
            "Entrance Series",
            "Loop Pitch",
            "Main streamway down to Eye-glasses Passage",
        ]
        assert {s.heading_kind for s in sections} == {"underline"}

    def test_mid_sentence_bold_date_is_not_a_heading(self) -> None:
        """The trap. Bold marks emphasis as often as it marks a title."""
        body = parse_fragment(
            "<P>Extension found in the boulders on the "
            "<b>2024 summer explorations, 11/8/24</b>. This led on."
        )
        assert body.sections == []

    def test_real_page_with_both_kinds_of_bold(self, body: Callable[[int], Body]) -> None:
        """0081 carries 13 bold date runs and 2 genuine headings."""
        assert [s.heading for s in body(81).sections] == ["Introduction", "Cave description"]

    def test_a_long_emphasised_sentence_is_not_a_heading(self) -> None:
        body = parse_fragment(
            "<P><i>It keeps going and has a small dog leg in the shaft which "
            "requires an additional bolt placement.</i>"
        )
        assert body.sections == []

    def test_paragraphs_are_assigned_to_their_section(self) -> None:
        body = parse_fragment("<P>Intro text.<P><B>Hydrology</B><BR>Water sinks.<P>More water.")
        paragraphs = [b for b in body.blocks if b.kind is BlockKind.PARAGRAPH]
        assert paragraphs[0].section_index is None
        assert [p.section_index for p in paragraphs[1:]] == [0, 0]


class TestBlocks:
    def test_paragraphs_split_on_p_and_br(self) -> None:
        body = parse_fragment("<P>One.<BR>Two.<P>Three.")
        assert [b.text for b in body.blocks] == ["One.", "Two.", "Three."]

    def test_hard_wrapped_lines_are_rejoined(self) -> None:
        """Source lines wrap at ~72 characters; those newlines are an artefact."""
        body = parse_fragment("<P>A shaft with a\nwindow into a\nsecond shaft.")
        assert body.text == "A shaft with a window into a second shaft."

    def test_nbsp_indent_starts_a_paragraph(self) -> None:
        """281 pages use `&nbsp;&nbsp;&nbsp;` as their paragraph marker."""
        body = parse_fragment("<P>&nbsp;&nbsp;&nbsp;First.&nbsp;&nbsp;&nbsp;Second.")
        assert [b.text for b in body.blocks] == ["First.", "Second."]

    def test_data_speak_divs_do_not_split_sentences(self) -> None:
        """Five pages wrap paragraphs in a text-to-speech div that opens and
        closes mid-sentence."""
        body = parse_fragment(
            '<P><div data-speak="true">A rift which is choked '
            '</div><div data-speak="true">with farm debris.</div>'
        )
        assert body.text == "A rift which is choked with farm debris."

    def test_editorial_note_is_separated_from_the_description(self) -> None:
        """Red text is a caveat about the page, not a description of the cave."""
        body = parse_fragment(
            '<P><FONT COLOR="#ff0000">Incomplete description.</FONT><P>The cave goes.'
        )
        assert body.editorial_notes == ["Incomplete description."]
        assert body.text == "The cave goes."


class TestCrossReferences:
    def test_hyperlink_to_another_site(self) -> None:
        body = parse_fragment('<P>Connects to <A HREF="0107.htm">Cueva Hoyuca</A>.')
        assert [(r.target_site, r.kind) for r in body.cross_refs] == [(107, "hyperlink")]

    def test_plaintext_reference(self) -> None:
        body = parse_fragment("<P>Water resurges at site 1470 downstream.")
        assert [(r.target_site, r.kind) for r in body.cross_refs] == [(1470, "plaintext")]

    def test_self_reference_is_dropped(self) -> None:
        """Every page's own number appears in its heading and title; letting
        those through makes the link graph useless."""
        body = parse_fragment('<P>See <A HREF="0105.htm">here</A>.', site_number=105)
        assert body.cross_refs == []

    def test_bare_four_digit_numbers_are_not_references(self) -> None:
        """Years, altitudes and survey batch codes are all four digits."""
        body = parse_fragment("<P>In 1987 the passage at 2500m was surveyed.")
        assert body.cross_refs == []

    def test_a_site_is_only_listed_once(self) -> None:
        body = parse_fragment(
            '<P>See <A HREF="0107.htm">Hoyuca</A> and <A HREF="0107.htm">again</A>.'
        )
        assert len(body.cross_refs) == 1


class TestDates:
    def test_season_and_year(self) -> None:
        body = parse_fragment("<P>At Easter 2009 a re-exploration took place.")
        (mention,) = [d for d in body.dates if d.kind == "season_year"]
        assert (mention.season, mention.year) == ("Easter", 2009)

    def test_month_and_year(self) -> None:
        body = parse_fragment("<P>Explored in December 2022.")
        (mention,) = [d for d in body.dates if d.kind == "month_year"]
        assert (mention.month, mention.year) == (12, 2022)

    def test_numeric_date(self) -> None:
        body = parse_fragment("<P>Surveyed 9/4/2023 by the team.")
        (mention,) = [d for d in body.dates if d.kind == "dmy"]
        assert (mention.day, mention.month, mention.year) == (9, 4, 2023)

    def test_survey_batch_code(self) -> None:
        body = parse_fragment("<P>See batch 0025-11-01 for the data.")
        assert [d.batch_code for d in body.dates if d.kind == "survey_batch"] == ["0025-11-01"]

    def test_a_year_inside_a_season_is_not_also_counted_alone(self) -> None:
        body = parse_fragment("<P>At Easter 2009 nothing was found.")
        assert [d.kind for d in body.dates] == ["season_year"]


class TestPeople:
    def test_by_pattern(self) -> None:
        body = parse_fragment("<P>The shaft was descended by Guy Simonnot.")
        assert [p.name_raw for p in body.people] == ["Guy Simonnot"]

    def test_two_names_joined_by_and(self) -> None:
        body = parse_fragment("<P>John Taylor and Dan Hibberts dived the sump.")
        assert {p.name_raw for p in body.people} == {"John Taylor", "Dan Hibberts"}

    def test_place_names_are_not_people(self) -> None:
        """Naive capitalised-bigram extraction is ~60% wrong on this corpus."""
        body = parse_fragment("<P>A route into Cueva Hoyuca and Fuente Aguanaz exists.")
        assert body.people == []

    def test_a_caving_club_is_not_a_person(self) -> None:
        """`explored by La Cambera` parses identically to `explored by Guy
        Simonnot`, but La Cambera is a club."""
        body = parse_fragment("<P>The shaft was explored by La Cambera in 2021.")
        assert body.people == []

    def test_a_named_group_is_not_a_person(self) -> None:
        body = parse_fragment("<P>Dug by La Cambera group who documented it.")
        assert body.people == []


class TestTables:
    def test_survey_batch_table_is_kept_as_rows(self, body: Callable[[int], Body]) -> None:
        """The only place in the corpus where a batch id, a date and a list of
        surveyors sit together."""
        tables = body(3234).tables
        assert tables
        assert any(t.rows for t in tables)


class TestBatInformation:
    def test_key_values_are_read(self, body: Callable[[int], Body]) -> None:
        observations = body(12).bat_observations
        assert observations
        assert observations[0].fields


class TestMinimalRecords:
    def test_one_sentence_body_is_marked_minimal_not_failed(self) -> None:
        body = parse_fragment("<P>Small shelter.")
        assert body.is_minimal
        assert body.text == "Small shelter."

    def test_a_long_body_is_not_minimal(self, body: Callable[[int], Body]) -> None:
        assert not body(1930).is_minimal


@pytest.mark.slow
class TestWholeCorpus:
    def test_no_page_has_more_sections_than_blocks(self) -> None:
        for path in config.iter_page_paths():
            body = _body(path)
            assert len(body.sections) <= len(body.blocks), path.stem

    def test_no_cross_reference_points_at_its_own_page(self) -> None:
        for path in config.iter_page_paths():
            body = _body(path)
            assert all(r.target_site != int(path.stem) for r in body.cross_refs), path.stem

    def test_body_text_never_contains_markup(self) -> None:
        for path in config.iter_page_paths():
            body = _body(path)
            assert "<" not in body.text or ">" not in body.text, path.stem

    def test_section_headings_are_short(self) -> None:
        """A long 'heading' means the block-wholly-emphasised rule has leaked."""
        for path in config.iter_page_paths():
            for section in _body(path).sections:
                assert len(section.heading) <= 60, (path.stem, section.heading)

    def test_extracted_dates_are_plausible(self) -> None:
        for path in config.iter_page_paths():
            for mention in _body(path).dates:
                if mention.year is not None:
                    assert 1900 <= mention.year <= 2030, (path.stem, mention.raw)
                if mention.month is not None:
                    assert 1 <= mention.month <= 12, (path.stem, mention.raw)

    def test_bat_observation_dates_parse(self) -> None:
        seen = 0
        for path in config.iter_page_paths():
            for observation in _body(path).bat_observations:
                seen += 1
                if observation.date is not None:
                    assert isinstance(observation.date, dt.date)
        assert seen >= 10
