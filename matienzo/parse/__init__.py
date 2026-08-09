"""Pure parsing. No database, no network, no filesystem beyond reading a page.

`parse_document` is a pure function of its input bytes plus `PARSER_VERSION` and
the vocabulary files. That purity is what makes golden-file tests meaningful and
what lets `matienzo build` be idempotent — and it is why corrections live in
`data/overrides/` and are applied to the parsed object afterwards, rather than
being special cases threaded into the parsers.
"""

from __future__ import annotations

from pathlib import Path

from matienzo import config
from matienzo.anomaly import SECTIONS, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import DecodedDocument, read_document
from matienzo.models import ParsedSite, Provenance
from matienzo.parse.body import parse_body
from matienzo.parse.footer import parse_footer
from matienzo.parse.header import parse_header
from matienzo.parse.segment import Segments, segment
from matienzo.parse.title import parse_title
from matienzo.parse.updated import parse_updated

__all__ = ["parse_document", "parse_page", "parse_site"]


def parse_page(path: Path) -> ParsedSite:
    """Read and parse one source page."""
    recorder = AnomalyRecorder()
    return parse_document(read_document(path, recorder=recorder), recorder)


def parse_site(site_number: int) -> ParsedSite:
    """Parse a page by site number."""
    return parse_page(config.page_path(site_number))


def parse_document(doc: DecodedDocument, recorder: AnomalyRecorder) -> ParsedSite:
    """Parse a decoded page into a complete record.

    Every section is optional: three pages are reserved or reallocated numbers
    with no header and no footer, and roughly half carry no update history.
    Missing sections are recorded in the provenance rather than raised, so that
    a stub page produces a valid record instead of an exception.
    """
    confidence = ConfidenceScorer(SECTIONS)
    segments = segment(doc, recorder)

    title = (
        parse_title(segments.title.text, doc.site_number, recorder, confidence)
        if segments.title is not None
        else None
    )
    header = (
        parse_header(segments.header.text, recorder, confidence)
        if segments.header is not None
        else None
    )
    updated = (
        parse_updated(segments.updated.text, recorder, confidence)
        if segments.updated is not None
        else []
    )
    footer = (
        parse_footer(segments.footer.text, recorder, confidence)
        if segments.footer is not None
        else None
    )
    body = parse_body(segments.body.text, doc.site_number, recorder, confidence)

    _score_missing_sections(segments, confidence)

    return ParsedSite(
        site_number=doc.site_number,
        title=title,
        header=header,
        updated=updated,
        body=body,
        footer=footer,
        provenance=Provenance(
            site_number=doc.site_number,
            source_path=str(doc.path.relative_to(config.REPO_ROOT))
            if doc.path.is_absolute() and doc.path.is_relative_to(config.REPO_ROOT)
            else doc.path.name,
            content_sha256=doc.content_sha256,
            byte_size=doc.byte_size,
            parser_version=config.PARSER_VERSION,
            encoding_used=doc.encoding.value,
            segments_found=segments.flags,
            confidence=confidence.as_dict(),
            confidence_reasons=confidence.reasons(),
            anomalies=recorder.items,
        ),
    )


def _score_missing_sections(segments: Segments, confidence: ConfidenceScorer) -> None:
    """Deduct confidence for sections that should have been there.

    A missing update line deducts nothing — 2,942 pages have none, and treating
    that as a defect would bury the real problems under half the corpus. Stub
    pages likewise deduct nothing for their missing header and footer: a
    reallocated number is a valid record, not a broken one.
    """
    if not segments.flags.title:
        confidence.deduct("title", 0.6, "no heading found")
    if not segments.flags.header and not segments.is_stub:
        confidence.deduct("header", 0.5, "no info line")
    if not segments.flags.footer and not segments.is_stub:
        confidence.deduct("footer", 0.7, "no reference footer")
