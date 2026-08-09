"""Footer fields, citations, and link classification."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from matienzo import config
from matienzo.anomaly import SECTIONS, AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import read_document
from matienzo.models import CitationKind, Footer, LinkBucket
from matienzo.parse.citations import citation_key, parse_citations, split_units
from matienzo.parse.footer import parse_footer
from matienzo.parse.links import classify, target_site
from matienzo.parse.segment import segment


def _footer(path: Path) -> tuple[Footer, AnomalyRecorder]:
    recorder = AnomalyRecorder()
    segments = segment(read_document(path, recorder=recorder), recorder)
    assert segments.footer is not None
    return parse_footer(segments.footer.text, recorder, ConfidenceScorer(SECTIONS)), recorder


@pytest.fixture
def footer(fixture_path: Callable[[int], Path]) -> Callable[[int], Footer]:
    def _run(site: int) -> Footer:
        return _footer(fixture_path(site))[0]

    return _run


class TestSplitUnits:
    """`;` separates citations *and* terminates HTML entities."""

    def test_entities_are_not_mistaken_for_separators(self) -> None:
        raw = "Fern&aacute;ndez Guti&eacute;rrez et al, 1966; Corrin J S, 1981"
        assert split_units(raw) == [
            "Fern&aacute;ndez Guti&eacute;rrez et al, 1966",
            " Corrin J S, 1981",
        ]

    def test_naive_split_would_produce_four_fragments(self) -> None:
        """Pins the bug this function exists to prevent."""
        raw = "Fern&aacute;ndez Guti&eacute;rrez et al, 1966; Corrin J S, 1981"
        assert len(raw.split(";")) == 4
        assert len(split_units(raw)) == 2

    def test_numeric_entities_too(self) -> None:
        assert split_units("a&#176;b; c") == ["a&#176;b", " c"]

    def test_anchors_survive_the_split(self) -> None:
        parts = split_units('<a href="x.pdf">One</a>; <a href="y.pdf">Two</a>')
        assert all("href" in p for p in parts)


class TestCitations:
    def test_archetype(self, footer: Callable[[int], Footer]) -> None:
        first = footer(1).citations[0]
        assert first.author_raw == "Fernández Gutiérrez et al"
        assert first.year == 1966
        assert first.links[0].bucket is LinkBucket.SCANNED_PUB

    def test_nested_survey_link_is_an_attachment_not_a_citation(
        self, footer: Callable[[int], Footer]
    ) -> None:
        """`<A>Fernández …, 1966</A> (<A>survey</A>)` is one reference with a
        scan attached, not a bibliography entry called "survey"."""
        first = footer(1).citations[0]
        assert [a.text for a in first.attachments] == ["survey"]
        assert all(c.author_raw != "survey" for c in footer(1).citations)

    def test_citation_split_across_two_anchors_stays_one_citation(self) -> None:
        """234+ pages write `anon., 2005b (<a>Easter</a> &amp; <a>summer</a>)`.
        Extracting per-anchor invents two entries named Easter and summer."""
        html = (
            'anon., 2005b (<a href="../logbook/logbook-pdfs/2005e-complete-log.pdf">'
            'Easter</a> &amp; <a href="../logbook/logbook-pdfs/2005s-complete-log.pdf">'
            "summer</a>)"
        )
        recorder = AnomalyRecorder()
        (citation,) = parse_citations(html, recorder)
        assert citation.author_raw == "anon."
        assert citation.year == 2005
        assert citation.disambiguator == "b"
        assert citation.qualifier == "Easter & summer"
        assert len(citation.links) == 2
        assert recorder.has(AnomalyCode.CITATION_SPLIT_ANCHORS)

    def test_anonymous_logbook_entry(self) -> None:
        recorder = AnomalyRecorder()
        (citation,) = parse_citations("anon., 2009a (Easter logbook)", recorder)
        assert citation.kind is CitationKind.LOGBOOK
        assert citation.qualifier_is_known

    def test_uncertain_year(self) -> None:
        recorder = AnomalyRecorder()
        (citation,) = parse_citations("Asociación Espeleológica Conquense Lobetum, 1995?", recorder)
        assert citation.year == 1995
        assert citation.kind is not CitationKind.UNPARSED

    def test_parenthesised_year(self) -> None:
        recorder = AnomalyRecorder()
        (citation,) = parse_citations("Chandler, I (2009) pers. comm.", recorder)
        assert citation.year == 2009

    @pytest.mark.parametrize(
        "text",
        ["material in file", "card", "pers comm", "none", "pers comm (email 13/5/02)"],
    )
    def test_provenance_notes_are_not_treated_as_bibliography(self, text: str) -> None:
        recorder = AnomalyRecorder()
        (citation,) = parse_citations(text, recorder)
        assert citation.kind is CitationKind.PROVENANCE
        assert citation.author_raw is None

    def test_unrecognised_qualifier_is_kept_and_flagged(self) -> None:
        recorder = AnomalyRecorder()
        (citation,) = parse_citations("Smith P, 2001 (a brand new descriptor)", recorder)
        assert citation.qualifier == "a brand new descriptor"
        assert not citation.qualifier_is_known
        assert recorder.has(AnomalyCode.CITATION_QUALIFIER_UNKNOWN)


class TestCitationKey:
    """De-duplication identity."""

    def test_qualifier_is_part_of_the_key(self) -> None:
        """`anon., 2005b` with two different qualifiers is two different
        logbooks; merging on author+year alone collapses them."""
        recorder = AnomalyRecorder()
        (easter,) = parse_citations("anon., 2005b (Easter logbook)", recorder)
        (summer,) = parse_citations("anon., 2005b (summer logbook)", recorder)
        assert citation_key(easter) != citation_key(summer)

    def test_same_citation_on_two_pages_shares_a_key(self) -> None:
        recorder = AnomalyRecorder()
        (a,) = parse_citations('<a href="x.pdf">anon., 2009a (Easter logbook)</a>', recorder)
        (b,) = parse_citations("anon., 2009a (Easter logbook)", recorder)
        assert citation_key(a) == citation_key(b)


class TestFooterFields:
    def test_reference_is_always_first(self, footer: Callable[[int], Footer]) -> None:
        assert footer(1).fields[0].label == "references"

    def test_colon_inside_the_bold_tag(self, footer: Callable[[int], Footer]) -> None:
        """`<B>Entrance pictures :</B>` — requiring `</B>\\s*:` misses 175 pages."""
        labels = {f.label for f in footer(52).fields}
        assert "entrance_pictures" in labels

    def test_empty_fields_are_marked_empty(self, footer: Callable[[int], Footer]) -> None:
        line_survey = [f for f in footer(1).fields if f.label == "line_survey"]
        assert line_survey and line_survey[0].is_empty

    def test_nbsp_only_value_counts_as_empty(self, footer: Callable[[int], Footer]) -> None:
        for field in footer(1).fields:
            if field.text.strip() in {"", "\xa0"}:
                assert field.is_empty

    def test_links_are_bucketed(self, footer: Callable[[int], Footer]) -> None:
        survex = [f for f in footer(1).fields if f.label == "survex_file"]
        assert survex and survex[0].links
        assert survex[0].links[0].bucket is LinkBucket.SURVEY_3D

    def test_unknown_label_is_kept_and_flagged(self) -> None:
        """A handful of pages use the label slot as an ad-hoc dated heading
        (`<B>2026 Easter</B>:`). Those are kept under a slug of their own rather
        than dropped, and flagged so the review queue can see them."""
        recorder = AnomalyRecorder()
        footer = parse_footer(
            '<B>Reference</B>: none<BR> <B>2026 Easter</B>: <a href="x.jpg">yes</a>',
            recorder,
            ConfidenceScorer(SECTIONS),
        )
        odd = [f for f in footer.fields if not f.is_known_label]
        assert [f.label_raw for f in odd] == ["2026 Easter"]
        assert odd[0].label == "2026_easter"
        assert odd[0].links
        assert recorder.has(AnomalyCode.FOOTER_LABEL_UNKNOWN)


class TestLinkClassification:
    @pytest.mark.parametrize(
        ("href", "bucket"),
        [
            ("../logbook/logbook-pdfs/2009e-complete-log.pdf", LinkBucket.LOGBOOK_PDF),
            ("../history/scanned-pubs/pdfs/a-lower.pdf", LinkBucket.SCANNED_PUB),
            ("../entpics/0001.htm", LinkBucket.ENTRANCE_PICS),
            ("../ugpics/1930.htm", LinkBucket.UNDERGROUND_PICS),
            ("../surveys/0001-RCN.3d", LinkBucket.SURVEY_3D),
            ("../surveys/0001-1964.jpg", LinkBucket.SURVEY_IMAGE),
            ("../surveys/0105-current.pdf", LinkBucket.SURVEY_PDF),
            ("https://youtu.be/IUOvWOI9MN4", LinkBucket.VIDEO_EXTERNAL),
            ("../videos/1930/x.mpg", LinkBucket.VIDEO_FILE),
            ("0107.htm", LinkBucket.SITE_PAGE),
            ("#Refs", LinkBucket.ANCHOR),
            ("../style-sheet.css", LinkBucket.BOILERPLATE),
            ("../logbook.php", LinkBucket.BOILERPLATE),
            ("https://es.wikipedia.org/wiki/x", LinkBucket.EXTERNAL),
        ],
    )
    def test_buckets(self, href: str, bucket: LinkBucket) -> None:
        assert classify(href) is bucket

    @pytest.mark.parametrize(
        ("href", "site", "is_range"),
        [
            ("0107.htm", 107, False),
            ("../entpics/1930.htm", 1930, False),
            ("../entpics/1930-2011e.htm", 1930, True),
            ("../entpics/4271-jc.htm", 4271, False),
            ("../style-sheet.css", None, False),
        ],
    )
    def test_target_site_extraction(self, href: str, site: int | None, is_range: bool) -> None:
        assert target_site(href) == (site, is_range)


@pytest.fixture(scope="module")
def footers() -> list[tuple[int, Footer, AnomalyRecorder]]:
    out = []
    for path in config.iter_page_paths():
        recorder = AnomalyRecorder()
        segments = segment(read_document(path, recorder=recorder), recorder)
        if segments.footer is None:
            continue
        parsed = parse_footer(segments.footer.text, recorder, ConfidenceScorer(SECTIONS))
        out.append((segments.site_number, parsed, recorder))
    return out


@pytest.mark.slow
class TestWholeCorpus:
    def test_every_non_stub_page_has_a_footer(
        self, footers: list[tuple[int, Footer, AnomalyRecorder]]
    ) -> None:
        assert len(footers) == 5555

    def test_every_footer_carries_a_reference_field(
        self, footers: list[tuple[int, Footer, AnomalyRecorder]]
    ) -> None:
        missing = [n for n, f, _ in footers if not any(x.label == "references" for x in f.fields)]
        assert missing == []

    def test_citation_yield(self, footers: list[tuple[int, Footer, AnomalyRecorder]]) -> None:
        total = sum(len(f.citations) for _, f, _ in footers)
        distinct = {citation_key(c) for _, f, _ in footers for c in f.citations}
        assert total == 14661
        assert len(distinct) == 752

    def test_almost_everything_parses(
        self, footers: list[tuple[int, Footer, AnomalyRecorder]]
    ) -> None:
        total = sum(len(f.citations) for _, f, _ in footers)
        unparsed = sum(
            1
            for _, f, _ in footers
            for c in f.citations
            if c.kind is CitationKind.UNPARSED and c.raw.strip()
        )
        assert unparsed / total < 0.005

    def test_no_citation_text_contains_a_shredded_entity(
        self, footers: list[tuple[int, Footer, AnomalyRecorder]]
    ) -> None:
        """The signature of splitting before unescaping is a *truncated entity
        name* — `Fern&aacute` — not a bare `&`, which is legitimate in
        `Easter & summer` and in `pers comm (emails 21/5/02 & 10/6/02)`.

        `citation.raw` is already unescaped, so a surviving `&name` fragment
        can only mean the semicolon was eaten as a separator.
        """
        fragment = re.compile(
            r"&(?:aacute|eacute|iacute|oacute|uacute|ntilde|amp|quot|nbsp|deg)\b(?!;)",
            re.IGNORECASE,
        )
        shredded = [
            (n, c.raw) for n, f, _ in footers for c in f.citations if fragment.search(c.raw)
        ]
        assert shredded == []

    def test_no_phantom_links_from_the_logbook_javascript(
        self, footers: list[tuple[int, Footer, AnomalyRecorder]]
    ) -> None:
        for site_number, footer, _ in footers:
            for field in footer.fields:
                for link in field.links:
                    assert "getElementsByTagName" not in link.href, site_number
                    assert "window.location" not in link.href, site_number
