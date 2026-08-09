"""Bytes on disk → trustworthy Python text.

The corpus declares no charset on ~99% of pages, and the few that *do* declare
one declare `iso-8859-1` while containing UTF-8. So the declaration is ignored
entirely and the bytes are trusted instead:

    strict UTF-8  →  CP1252  →  latin-1

Measured over the full 5,557-file corpus: 5,507 files are pure ASCII, 32 are
genuine UTF-8 (`Ria\\xc3\\xb1o` = `Riaño`), and 18 need the CP1252 fallback. The
latin-1 tier is never reached today and exists only so that a future re-scrape
cannot crash the build on a single stray byte.

CP1252 rather than latin-1 matters: four of those 18 carry `\\x91`/`\\x92`
(curly quotes), which are undefined in ISO-8859-1 and would decode to C1 control
characters. Going the other way is worse — decoding the whole corpus as latin-1
would turn `Riaño` into `RiaÃ±o` in all 32 UTF-8 files.

There is no mojibake in the corpus as scraped, so there is no repair pass — only
detection, because the upstream site is live and a future re-scrape could
introduce some.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from matienzo.anomaly import AnomalyCode, AnomalyRecorder, Severity


class Encoding(StrEnum):
    UTF8 = "utf-8"
    CP1252 = "cp1252"
    LATIN1 = "latin-1"
    """Last resort. Unused by the corpus as scraped — see `decode_bytes`."""


#: Sequences that appear when UTF-8 bytes have been decoded as latin-1/CP1252
#: somewhere upstream. `Ã±`, `â€™`, `Â ` and friends.
MOJIBAKE_RE = re.compile(r"Ã[\x80-\xbf]|â€[\x80-\xbf\x99\x9c\x9d]?|Â[\xa0-\xbf]")

#: An `&word;` or `&#123;` that `html.unescape` left behind — i.e. one that is
#: not a recognised HTML5 entity. Genuine `&` in URLs won't match (no `;`).
RESIDUAL_ENTITY_RE = re.compile(r"&(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")

#: Entity-looking text with no terminating semicolon, e.g. `20th November&nbsp`
#: in 1162.htm. `html.unescape` resolves many of these per the HTML5 spec, so
#: this is used for *reporting* rather than for correction.
UNTERMINATED_ENTITY_RE = re.compile(r"&(nbsp|amp|quot|lt|gt|[a-z]{2,10})(?![a-zA-Z0-9;])")

#: C0 controls that carry no meaning in this corpus. Tab and newline are kept.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class DecodedDocument:
    """A source page decoded to text, with the provenance needed to reproduce it.

    `text` is the *markup*, not plain text: entities are deliberately left
    unresolved so that segmentation and HTML parsing see the document the way the
    author wrote it. Resolving `&lt;` early would invent tags that were never
    there. Use `unescape()` on extracted text instead.
    """

    site_number: int
    path: Path
    text: str
    encoding: Encoding
    content_sha256: str
    byte_size: int

    @property
    def used_fallback(self) -> bool:
        """True when the bytes were not valid UTF-8."""
        return self.encoding is not Encoding.UTF8


def decode_bytes(raw: bytes) -> tuple[str, Encoding]:
    """Decode source bytes: strict UTF-8, then CP1252, then latin-1.

    The latin-1 tier exists because CP1252 is *not* total — 0x81, 0x8D, 0x8F,
    0x90 and 0x9D are undefined and raise. No page in the corpus as scraped
    reaches it, but the upstream site is live, and a decoder that can crash on
    one stray byte is a decoder that will eventually take the whole build down.
    latin-1 maps all 256 byte values, so this function cannot fail.
    """
    try:
        return raw.decode("utf-8"), Encoding.UTF8
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), Encoding.CP1252
    except UnicodeDecodeError:
        return raw.decode("latin-1"), Encoding.LATIN1


def normalise_source(text: str) -> str:
    """Canonicalise decoded markup: CRLF → LF, drop C0 controls, NFC.

    NFC matters for the Spanish place names: `Riaño` written as `n` + combining
    tilde and `Riaño` written with the precomposed `ñ` are different strings to
    SQLite's FTS tokeniser and to `==`, and both forms reach us via the two
    different decode paths.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_CHARS_RE.sub("", text)
    return unicodedata.normalize("NFC", text)


def read_document(
    path: Path,
    site_number: int | None = None,
    recorder: AnomalyRecorder | None = None,
) -> DecodedDocument:
    """Read and decode one source page.

    The site number comes from the *filename*, not from the page content: the
    number in the `<BIG><B>NNNN: …</B></BIG>` heading matches the filename in all
    5,557 files, but the filename is the thing we can trust unconditionally.
    """
    raw = path.read_bytes()
    if site_number is None:
        site_number = int(path.stem)

    text, encoding = decode_bytes(raw)
    text = normalise_source(text)

    if recorder is not None:
        _report_encoding_anomalies(text, encoding, recorder)
        if not raw.strip():
            recorder.add(
                AnomalyCode.EMPTY_DOCUMENT,
                f"{path.name} is empty or whitespace-only",
                severity=Severity.ERROR,
            )

    return DecodedDocument(
        site_number=site_number,
        path=path,
        text=text,
        encoding=encoding,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )


def _report_encoding_anomalies(text: str, encoding: Encoding, recorder: AnomalyRecorder) -> None:
    if encoding is Encoding.CP1252:
        recorder.add(AnomalyCode.CP1252_FALLBACK, "not valid UTF-8; decoded as CP1252")
    elif encoding is Encoding.LATIN1:
        # Reaching here means bytes that are undefined even in CP1252. Unseen in
        # the corpus so far, so treat it as something a human must look at.
        recorder.add(
            AnomalyCode.CP1252_FALLBACK,
            "not valid UTF-8 or CP1252; decoded as latin-1",
            severity=Severity.ERROR,
        )

    if match := MOJIBAKE_RE.search(text):
        recorder.add(
            AnomalyCode.MOJIBAKE_SUSPECTED,
            "text looks like UTF-8 that was decoded as latin-1 upstream",
            severity=Severity.ERROR,
            excerpt=text[max(0, match.start() - 40) : match.end() + 40],
            char_offset=match.start(),
        )

    if "�" in text:
        recorder.add(
            AnomalyCode.REPLACEMENT_CHAR,
            "U+FFFD present — a lossy decode happened before we saw the file",
            severity=Severity.ERROR,
            char_offset=text.index("�"),
        )


def unescape(text: str, recorder: AnomalyRecorder | None = None) -> str:
    """Resolve HTML entities in *extracted text* and re-normalise to NFC.

    Call this on text pulled out of the DOM, never on whole markup.

    Order is load-bearing for citations: the footer's reference list is
    semicolon-separated, and `&aacute;` also ends in a semicolon. Splitting
    before unescaping shreds `Fernández` into `Fern` + `aacute` + `ndez`, which
    is how you end up with 751 "distinct" citations instead of 710.
    """
    resolved = unicodedata.normalize("NFC", html.unescape(text))

    if recorder is not None:
        if match := RESIDUAL_ENTITY_RE.search(resolved):
            recorder.add(
                AnomalyCode.UNKNOWN_ENTITY,
                f"unrecognised entity {match.group(0)!r} survived unescaping",
                excerpt=resolved[max(0, match.start() - 40) : match.end() + 40],
                char_offset=match.start(),
            )
        if match := UNTERMINATED_ENTITY_RE.search(text):
            recorder.add(
                AnomalyCode.UNTERMINATED_ENTITY,
                f"entity {match.group(0)!r} has no terminating semicolon",
                excerpt=text[max(0, match.start() - 40) : match.end() + 40],
                char_offset=match.start(),
            )

    return resolved


def collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces and trim.

    Source lines are hard-wrapped at ~72 characters by a legacy editor, so bare
    newlines inside a paragraph are an artefact of the tooling and carry no
    meaning. Paragraph structure comes from `<P>`/`<BR>`, never from `\\n`.
    """
    return " ".join(text.split())
