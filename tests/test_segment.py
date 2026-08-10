"""Segmentation is where a subtly wrong boundary produces plausible-looking
output instead of an error, so these tests assert on the specific pages that
broke each earlier attempt."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from matienzo import config
from matienzo.anomaly import AnomalyCode, AnomalyRecorder, Severity
from matienzo.decode import read_document
from matienzo.parse.segment import Segments, segment


def _segment(path: Path) -> tuple[Segments, AnomalyRecorder]:
    recorder = AnomalyRecorder()
    return segment(read_document(path, recorder=recorder), recorder), recorder


@pytest.fixture
def seg(fixture_path: Callable[[int], Path]) -> Callable[[int], Segments]:
    def _run(site: int) -> Segments:
        return _segment(fixture_path(site))[0]

    return _run


class TestTitle:
    def test_archetype(self, seg: Callable[[int], Segments]) -> None:
        title = seg(1).title
        assert title is not None
        assert "0001: Burro, Sima del" in title.text

    @pytest.mark.parametrize("site", [505, 1775, 1955])
    def test_swapped_tag_nesting_is_still_found(
        self, fixture_path: Callable[[int], Path], site: int
    ) -> None:
        """`<B><BIG>` instead of `<BIG><B>`. Three pages; the backreference in
        TITLE_RE keeps the pairing honest either way."""
        segments, recorder = _segment(fixture_path(site))
        assert segments.title is not None
        assert str(site) in segments.title.text
        assert recorder.has(AnomalyCode.TITLE_TAG_ORDER_SWAPPED)

    def test_swapped_nesting_is_texture_not_a_warning(
        self, fixture_path: Callable[[int], Path]
    ) -> None:
        _, recorder = _segment(fixture_path(1775))
        codes = {a.code: a.severity for a in recorder.items}
        assert codes[AnomalyCode.TITLE_TAG_ORDER_SWAPPED] is Severity.INFO


class TestHeaderBoundary:
    """Each of these pages defeated a simpler rule."""

    @pytest.mark.parametrize("site", [2410, 4950, 61, 151, 417, 917, 1452])
    def test_header_split_across_two_small_blocks(
        self, seg: Callable[[int], Segments], site: int
    ) -> None:
        """Stopping at the first `</SMALL>` truncates these and leaks the rest
        of the info line into the description."""
        segments = seg(site)
        assert segments.header is not None
        assert "Logbook search" in segments.header.text
        assert "Logbook search" not in segments.body.text

    def test_boilerplate_outside_any_small_block(self, seg: Callable[[int], Segments]) -> None:
        """0363 closes `</SMALL>` before the anchors, leaving them bare."""
        segments = seg(363)
        assert segments.header is not None
        assert "Area position" in segments.header.text
        assert "Area position" not in segments.body.text

    def test_stray_text_between_small_blocks(self, seg: Callable[[int], Segments]) -> None:
        """0420 has a literal `]` between `</small>` and the next `<SMALL>`, so
        a whitespace-only bridging rule fails on it."""
        segments = seg(420)
        assert segments.header is not None
        assert "Logbook search" in segments.header.text
        assert "Length" in segments.header.text

    def test_multi_entrance_header_keeps_both_coordinates(
        self, seg: Callable[[int], Segments]
    ) -> None:
        segments = seg(1930)
        assert segments.header is not None
        assert "Top entrance 30T 448666 4797809" in segments.header.text
        assert "Bottom entrance #5451" in segments.header.text
        assert "Vertical Range" in segments.header.text

    def test_duplicate_closing_small_is_reported(self, fixture_path: Callable[[int], Path]) -> None:
        _, recorder = _segment(fixture_path(1))
        assert recorder.has(AnomalyCode.STRAY_SMALL_IN_HEADER)


class TestFooterBoundary:
    def test_footer_is_located_before_the_header(self, seg: Callable[[int], Segments]) -> None:
        """5528 has no info line, so its first `<SMALL>` *is* the footer. A
        header-first rule eats it and then reports the page as footerless."""
        segments = seg(5528)
        assert segments.header is None
        assert segments.footer is not None
        assert "Survex file" in segments.footer.text

    def test_nested_small_in_the_body_does_not_end_it(self, seg: Callable[[int], Segments]) -> None:
        """178 pages nest `<SMALL>` inside the description for logbook quotes.
        The body ends at `<B>Referen…`, never at the next `<SMALL>`."""
        segments = seg(2366)
        assert segments.footer is not None
        assert segments.footer.text.lstrip().lower().startswith(("<small", "<p"))
        assert "Referen" in segments.footer.text[:200]

    def test_footer_excludes_the_trailing_modal(self, seg: Callable[[int], Segments]) -> None:
        segments = seg(1)
        assert segments.footer is not None
        assert "openModal" not in segments.footer.text
        assert segments.modal is not None
        assert "tinymaps/0001.png" in segments.modal.text

    @pytest.mark.parametrize("site", [28, 841, 2081, 2366, 4732])
    def test_text_to_speech_widget_is_excluded_and_flagged(
        self, fixture_path: Callable[[int], Path], site: int
    ) -> None:
        """Five pages append a TTS widget with a second `<BIG>` and a `<script>`.
        Scanning to end-of-file picks it up as a second cave description."""
        segments, recorder = _segment(fixture_path(site))
        assert "Experimental text-to-speech" not in segments.body.text
        assert segments.footer is None or "reader.js" not in segments.footer.text
        assert recorder.has(AnomalyCode.TTS_WRAPPER_PRESENT)


class TestUpdatedLine:
    def test_multi_date_line_is_captured_whole(self, seg: Callable[[int], Segments]) -> None:
        segments = seg(105)
        assert segments.updated is not None
        assert "19th February" in segments.updated.text
        assert "2026" in segments.updated.text

    def test_absent_updated_line_is_normal(self, fixture_path: Callable[[int], Path]) -> None:
        """Roughly half the corpus has none; it deducts no confidence."""
        segments, recorder = _segment(fixture_path(1775))
        assert segments.updated is None
        assert not recorder.has(AnomalyCode.UPDATED_DATE_UNPARSED)

    def test_updated_line_is_not_left_in_the_body(self, seg: Callable[[int], Segments]) -> None:
        assert "Updated" not in seg(1).body.text


class TestStubPages:
    """Three pages are reserved or reallocated numbers rather than real sites.
    They are legitimate records, not parse failures."""

    @pytest.mark.parametrize("site", [249, 4540])
    def test_stub_pages_have_neither_header_nor_footer(
        self, seg: Callable[[int], Segments], site: int
    ) -> None:
        assert seg(site).is_stub

    def test_reallocated_page_keeps_its_note_in_the_body(
        self, seg: Callable[[int], Segments]
    ) -> None:
        segments = seg(249)
        assert segments.title is not None
        assert segments.title.text.strip() == "0249: -"
        assert "To be re-allocated" in segments.body.text

    def test_reserved_page_carries_its_note_in_the_title(
        self, seg: Callable[[int], Segments]
    ) -> None:
        """4540's only content is `4540: reserved` — the body is genuinely empty,
        so an extractor must not treat an empty body as a failure."""
        segments = seg(4540)
        assert segments.title is not None
        assert segments.title.text.strip() == "4540: reserved"
        assert not segments.body.text.strip(" <BR>\n\t")
        assert segments.flags.body is False

    def test_a_page_with_a_footer_is_not_a_stub(self, seg: Callable[[int], Segments]) -> None:
        assert not seg(5528).is_stub


class TestMalformedDocuments:
    def test_missing_body_close_tag(self, fixture_path: Callable[[int], Path]) -> None:
        segments, recorder = _segment(fixture_path(1111))
        assert recorder.has(AnomalyCode.MISSING_BODY_CLOSE)
        assert segments.footer is not None

    def test_unterminated_attribute_does_not_break_segmentation(
        self, seg: Callable[[int], Segments]
    ) -> None:
        """0048's header carries `href="…q=Reñada, Cubío de la+Vega target="_blank"`
        with no closing quote before `target`."""
        segments = seg(48)
        assert segments.header is not None
        assert segments.footer is not None
        assert segments.body.text.strip()


@pytest.fixture(scope="module")
def all_segments() -> list[tuple[Segments, AnomalyRecorder]]:
    """Every page, segmented once and shared across the corpus assertions."""
    return [_segment(p) for p in config.iter_page_paths()]


@pytest.mark.slow
class TestWholeCorpus:
    """The Phase 1 checkpoint, asserted rather than eyeballed."""

    def test_every_page_yields_a_title(
        self, all_segments: list[tuple[Segments, AnomalyRecorder]]
    ) -> None:
        assert [s.site_number for s, _ in all_segments if s.title is None] == []

    def test_only_the_known_stubs_lack_a_header(
        self, all_segments: list[tuple[Segments, AnomalyRecorder]]
    ) -> None:
        assert [s.site_number for s, _ in all_segments if s.header is None] == [249, 4540, 5528]

    def test_only_the_known_stubs_lack_a_footer(
        self, all_segments: list[tuple[Segments, AnomalyRecorder]]
    ) -> None:
        assert [s.site_number for s, _ in all_segments if s.footer is None] == [249, 4540]

    def test_no_page_leaks_header_or_footer_into_its_body(
        self, all_segments: list[tuple[Segments, AnomalyRecorder]]
    ) -> None:
        leaked = [
            s.site_number
            for s, r in all_segments
            if r.has(AnomalyCode.BODY_LEAKED_HEADER) or r.has(AnomalyCode.BODY_LEAKED_FOOTER)
        ]
        assert leaked == []

    def test_no_error_severity_anomalies(
        self, all_segments: list[tuple[Segments, AnomalyRecorder]]
    ) -> None:
        errors = [
            (s.site_number, a.code, a.detail)
            for s, r in all_segments
            for a in r.items
            if a.severity is Severity.ERROR
        ]
        assert errors == []

    def test_segments_are_ordered_and_non_overlapping(
        self, all_segments: list[tuple[Segments, AnomalyRecorder]]
    ) -> None:
        for segments, _ in all_segments:
            spans = [
                (name, getattr(segments, name))
                for name in ("title", "header", "updated", "body", "footer", "modal")
            ]
            present = [(name, s) for name, s in spans if s is not None]
            for (_, earlier), (name, later) in itertools.pairwise(present):
                assert earlier.end <= later.start, f"{segments.site_number:04d} {name} overlaps"

    def test_updated_line_count_matches_the_survey(
        self, all_segments: list[tuple[Segments, AnomalyRecorder]]
    ) -> None:
        """Guards against the class of bug where the header's trailing-tag rule
        silently eats the `<I>` that opens the update history."""
        assert sum(1 for s, _ in all_segments if s.updated is not None) == 2615
