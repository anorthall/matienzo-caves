"""Split a source page into its structural segments.

Every page in the corpus follows one template::

    <BIG><B>NNNN: Name (aliases)</B></BIG><BR>          <- title
    <SMALL><B>Area</B> 30T E N (Datum…) <B>Altitude</B> …
      … <a>Area position</a> : <a>Site entrance in context</a>
      : <a>Logbook search</a></small>                   <- header
    <P><I>Updated 5th November 2003; …</I><BR>          <- updated (optional)
      …free text…                                       <- body
    <SMALL><B>Reference</B>: …</SMALL>                  <- footer
    <div id="openModal">…</div>                         <- modal (boilerplate)

Getting these five boundaries right is the whole of Phase 1, and it is the
highest-risk step in the project: everything downstream inherits its mistakes,
and a boundary that is subtly wrong produces plausible-looking output rather
than an error. So the rules below were each derived by measuring the whole
corpus, and the ones that look over-engineered are the ones that had to be.

Three rules that are *not* obvious, each of which cost a wrong first attempt:

**Find the footer before the header.** Three stub pages (`0249`, `4540`, `5528`)
have no header info line at all, and on `5528` the first `<SMALL>` in the
document *is* the reference footer. Taking "first `<SMALL>` after the title" as
the header swallows it and then reports the page as footerless.

**The header ends at the boilerplate anchor run, not at `</SMALL>`.** Seven
pages split the info line across two `<SMALL>` blocks (`2410`, `4950`, `0061`,
`0151`, `0417`, `0917`, `1452`) and `0363` leaves the anchors outside any
`<SMALL>` at all, so closing-tag-based rules truncate the header and leak
`Accuracy code` / `Logbook search` into the description. Every non-stub page ends
its header with the `Area position` / `Site entrance in context` / `Logbook
search` anchor group, so that run is the reliable terminator.

**Never cut the body at "the next `<SMALL>`".** 178 pages nest `<SMALL>` inside
the description for logbook quotations and bat-observation blocks. The body ends
where `<B>Referen…` begins, and nowhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from matienzo.anomaly import AnomalyCode, AnomalyRecorder, Severity
from matienzo.decode import DecodedDocument
from matienzo.models import SegmentFlags

#: The site heading. 5,555 pages nest it `<BIG><B>`; `0505` and `1775` swap the
#: two. The backreference keeps the pairing honest either way.
TITLE_RE = re.compile(
    r"<(BIG|B)\b[^>]*>\s*<(B|BIG)\b[^>]*>(?P<inner>.*?)</\2\s*>\s*</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

SMALL_OPEN_RE = re.compile(r"<SMALL\b[^>]*>", re.IGNORECASE)
SMALL_CLOSE_RE = re.compile(r"</SMALL\s*>", re.IGNORECASE)

#: Start of the reference footer. `Reference` and `References` both occur, the
#: wrapper is `<SMALL>` or `<P>`, and 23 pages put an `<a name="Refs">` anchor in
#: between. Tag case is mixed throughout, hence IGNORECASE everywhere.
FOOTER_RE = re.compile(
    r"<(?:SMALL|P)\b[^>]*>\s*(?:<a\s+name=[^>]*>\s*)?\s*<B\b[^>]*>\s*Referen",
    re.IGNORECASE,
)

#: Fallback for a footer whose `<B>Referen…` is not wrapped. Unused by the corpus
#: as scraped, but the upstream pages are hand-edited and this costs nothing.
FOOTER_BARE_RE = re.compile(r"<B\b[^>]*>\s*Referen", re.IGNORECASE)

#: The boilerplate anchor group that terminates every non-stub header.
HEADER_TAIL_RE = re.compile(
    r"(?:Logbook\s+search|Site\s+entrance\s+in\s+context|Area\s+position)\s*</a\s*>",
    re.IGNORECASE,
)

#: Tags that may trail the header info line: its own `</small>`, any stray
#: closers left over from the hand-written markup, and line breaks.
#:
#: Strictly *closing* tags plus `<br>`. It must never consume an opening tag —
#: an earlier version allowed `<p>` and `<i>`, which ate the `<I>` of the
#: following `<I>Updated …</I>` line and dropped the update history on 2,495
#: pages while still looking plausible.
HEADER_TRAIL_RE = re.compile(
    r"(?:\s|</(?:small|b|i|em|p|font|a)\b[^>]*>|<br\s*/?>)*",
    re.IGNORECASE,
)

#: The `Updated …` line. Half the corpus has none, which is normal rather than an
#: anomaly. Tag is `<I>`, `<i>`, `<em>` or `<EM>`.
UPDATED_OPEN_RE = re.compile(r"<(?P<tag>I|EM)\b[^>]*>\s*Updated\b", re.IGNORECASE)

MODAL_RE = re.compile(r"<div\b[^>]*\bid\s*=\s*[\"']?openModal", re.IGNORECASE)
BODY_CLOSE_RE = re.compile(r"</BODY\s*>", re.IGNORECASE)
BODY_OPEN_RE = re.compile(r"<BODY\b", re.IGNORECASE)

#: Markers of the experimental text-to-speech widget appended to five pages. Its
#: `<div data-speak>` wrappers open and close mid-sentence *inside* the
#: description, so those pages must be read through a DOM parser, not regex.
TTS_RE = re.compile(r"data-speak|TEXT TO SPEECH|reader\.js", re.IGNORECASE)
SCRIPT_RE = re.compile(r"<script\b", re.IGNORECASE)

#: Header content that must never appear in the body. Used as a self-check.
HEADER_LEAK_MARKERS = ("Accuracy code", "Logbook search", "Area position")


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open character range into the decoded document, with its text."""

    start: int
    end: int
    text: str

    def __len__(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Segments:
    """The structural decomposition of one page.

    Raw markup, not parsed values — turning these into fields is Phase 2. Kept as
    a plain dataclass because it is an internal intermediate that is never
    serialised; only `flags` reaches the database.
    """

    site_number: int
    title: Span | None
    header: Span | None
    updated: Span | None
    body: Span
    footer: Span | None
    modal: Span | None
    flags: SegmentFlags

    @property
    def is_stub(self) -> bool:
        """A page with no header and no footer — reserved or reallocated numbers.

        Three exist: `0249` ("To be re-allocated"), `4540` ("reserved") and
        `5528`. They are legitimate records, not parse failures.
        """
        return self.header is None and self.footer is None


def segment(doc: DecodedDocument, recorder: AnomalyRecorder) -> Segments:
    """Split a decoded page into title / header / updated / body / footer / modal."""
    text = doc.text
    flags = SegmentFlags()

    title = _find_title(text, recorder)
    flags.title = title is not None
    search_from = title.end if title else 0

    footer, modal = _find_footer_and_modal(text, search_from, recorder)
    flags.footer = footer is not None
    flags.modal = modal is not None

    # The body can never extend past the footer; without one it stops at the
    # modal, and without that at `</BODY>` or end of file.
    body_limit = footer.start if footer else (modal.start if modal else _document_end(text))

    header = _find_header(text, search_from, body_limit, recorder)
    flags.header = header is not None
    body_from = header.end if header else search_from

    updated = _find_updated(text, body_from, body_limit, recorder)
    flags.updated = updated is not None
    if updated is not None:
        body_from = updated.end

    body_from = min(body_from, body_limit)
    body = Span(body_from, body_limit, text[body_from:body_limit])
    flags.body = bool(_visible_text(body.text))

    _check_for_leaks(body, recorder)
    _note_document_oddities(text, recorder)

    return Segments(
        site_number=doc.site_number,
        title=title,
        header=header,
        updated=updated,
        body=body,
        footer=footer,
        modal=modal,
        flags=flags,
    )


def _find_title(text: str, recorder: AnomalyRecorder) -> Span | None:
    match = TITLE_RE.search(text)
    if match is None:
        recorder.add(
            AnomalyCode.TITLE_NOT_FOUND,
            "no <BIG><B>…</B></BIG> heading",
            severity=Severity.ERROR,
            field_path="title",
        )
        return None

    if match.group(1).upper() == "B":
        recorder.add(
            AnomalyCode.TITLE_TAG_ORDER_SWAPPED,
            "heading nests <B><BIG> rather than <BIG><B>",
            field_path="title",
        )
    return Span(match.start(), match.end(), match.group("inner"))


def _find_footer_and_modal(
    text: str, search_from: int, recorder: AnomalyRecorder
) -> tuple[Span | None, Span | None]:
    """Locate the reference footer and the trailing map-modal boilerplate.

    Done before the header because on `5528` the document's first `<SMALL>` is
    the footer, and a header search that doesn't know where the footer starts
    will consume it.
    """
    match = FOOTER_RE.search(text, search_from)
    if match is None:
        match = FOOTER_BARE_RE.search(text, search_from)
        if match is not None:
            recorder.add(
                AnomalyCode.FOOTER_LABEL_UNKNOWN,
                "reference footer is not wrapped in <SMALL> or <P>",
                field_path="footer",
            )

    modal_match = MODAL_RE.search(text, match.end() if match else search_from)
    modal_end = _document_end(text, from_index=modal_match.start()) if modal_match else None
    modal = (
        Span(modal_match.start(), modal_end, text[modal_match.start() : modal_end])
        if modal_match and modal_end is not None
        else None
    )

    if match is None:
        # Expected on the three stub pages; anything else means the page changed.
        recorder.add(
            AnomalyCode.FOOTER_BOUNDARY_NOT_FOUND,
            "no <B>Reference…</B> block",
            severity=Severity.WARN,
            field_path="footer",
        )
        return None, modal

    # The footer runs to the modal if there is one, else to `</BODY>`. Cutting
    # here is also what excludes the text-to-speech widget appended to five pages.
    footer_end = modal.start if modal else _document_end(text, from_index=match.end())
    return Span(match.start(), footer_end, text[match.start() : footer_end]), modal


def _find_header(
    text: str, search_from: int, body_limit: int, recorder: AnomalyRecorder
) -> Span | None:
    """Locate the `<SMALL>` info line carrying area, coordinates and measurements.

    Starts at the first `<SMALL>` after the title and ends after the boilerplate
    anchor run — see the module docstring for why the closing tag is not enough.
    """
    open_match = SMALL_OPEN_RE.search(text, search_from, body_limit)
    if open_match is None:
        recorder.add(
            AnomalyCode.NO_HEADER_SMALL,
            "no <SMALL> info line between the heading and the footer",
            severity=Severity.WARN,
            field_path="header",
        )
        return None

    # The *last* anchor in the boilerplate run terminates the header.
    tail = None
    for anchor in HEADER_TAIL_RE.finditer(text, open_match.end(), body_limit):
        tail = anchor

    if tail is not None:
        end = tail.end()
    else:
        close = SMALL_CLOSE_RE.search(text, open_match.end(), body_limit)
        end = close.end() if close else open_match.end()
        recorder.add(
            AnomalyCode.HEADER_BOUNDARY_NOT_FOUND,
            "header has no Area position / Logbook search anchors; fell back to the first </SMALL>",
            field_path="header",
        )

    trailing = HEADER_TRAIL_RE.match(text, end)
    if trailing is not None and trailing.end() > end:
        # The first `</SMALL>` here is the info line's own closing tag. A second
        # one is the hand-written duplicate that 19 pages carry.
        closers = SMALL_CLOSE_RE.findall(text, end, trailing.end())
        if len(closers) > 1:
            recorder.add(
                AnomalyCode.STRAY_SMALL_IN_HEADER,
                f"{len(closers)} </SMALL> tags close the header info line",
                field_path="header",
            )
        end = trailing.end()

    end = min(end, body_limit)
    return Span(open_match.start(), end, text[open_match.start() : end])


def _find_updated(
    text: str, search_from: int, body_limit: int, recorder: AnomalyRecorder
) -> Span | None:
    """Locate the italic `Updated …` line.

    Absent from roughly half the corpus, which is normal and deducts no
    confidence. Content is left unparsed here; Phase 2 turns it into dates.
    """
    match = UPDATED_OPEN_RE.search(text, search_from, body_limit)
    if match is None:
        return None

    tag = match.group("tag")
    end = _find_close(text, match.end(), body_limit, tag)
    if end is None:
        recorder.add(
            AnomalyCode.UPDATED_MARKUP_NESTED,
            f"<{tag}>Updated…</{tag}> is never closed",
            field_path="updated",
        )
        return None

    inner = text[match.end() : end - len(f"</{tag}>")]
    if re.search(rf"<{tag}\b", inner, re.IGNORECASE):
        recorder.add(
            AnomalyCode.UPDATED_MARKUP_NESTED,
            "Updated line contains nested markup",
            field_path="updated",
            excerpt=inner,
        )
    return Span(match.start(), end, text[match.start() : end])


def _find_close(text: str, start: int, limit: int, tag: str) -> int | None:
    """End offset of the `</tag>` that closes an already-open `<tag>`.

    Tracks nesting depth so that a `<i>` inside an `<I>Updated …</I>` line — 31
    pages carry links and emphasis in there — doesn't end the element early.
    """
    pattern = re.compile(rf"<(/?){tag}\b[^>]*>", re.IGNORECASE)
    depth = 1
    for match in pattern.finditer(text, start, limit):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            return match.end()
    return None


def _document_end(text: str, from_index: int = 0) -> int:
    """Offset of `</BODY>`, or end of text when the page never closes it.

    `1111.htm` has no `</BODY>` at all.
    """
    match = BODY_CLOSE_RE.search(text, from_index)
    return match.start() if match else len(text)


def _visible_text(html: str) -> str:
    """Crude tag strip, for presence checks only.

    Deliberately not the real text extractor — that lives in Phase 2 and goes
    through a DOM parser. This exists so `flags.body` can distinguish "empty"
    from "markup but no words" without pulling in lxml.
    """
    return " ".join(re.sub(r"<[^>]*>", " ", html).split())


def _check_for_leaks(body: Span, recorder: AnomalyRecorder) -> None:
    """Assert the body contains neither header nor footer content.

    This is the segmenter checking its own work. A boundary that is slightly
    wrong yields text that still reads like a cave description, so without this
    the failure mode is silent corruption rather than a visible error.
    """
    for marker in HEADER_LEAK_MARKERS:
        if marker in body.text:
            recorder.add(
                AnomalyCode.BODY_LEAKED_HEADER,
                f"header boilerplate {marker!r} appears in the description",
                severity=Severity.ERROR,
                field_path="body",
                char_offset=body.start + body.text.index(marker),
            )
    if FOOTER_BARE_RE.search(body.text) or "openModal" in body.text:
        recorder.add(
            AnomalyCode.BODY_LEAKED_FOOTER,
            "footer or modal boilerplate appears in the description",
            severity=Severity.ERROR,
            field_path="body",
        )


def _note_document_oddities(text: str, recorder: AnomalyRecorder) -> None:
    """Record page-level malformedness worth knowing about downstream."""
    if not BODY_OPEN_RE.search(text):
        recorder.add(AnomalyCode.NO_BODY_TAG, "document has no <BODY> tag")
    if not BODY_CLOSE_RE.search(text):
        recorder.add(AnomalyCode.MISSING_BODY_CLOSE, "document has no </BODY> tag")
    if TTS_RE.search(text):
        recorder.add(
            AnomalyCode.TTS_WRAPPER_PRESENT,
            "text-to-speech widget present; its <div data-speak> wrappers open and "
            "close mid-sentence, so this page must be read via the DOM",
        )
    if SCRIPT_RE.search(text):
        recorder.add(AnomalyCode.SCRIPT_TAG_PRESENT, "document contains a <script> block")
