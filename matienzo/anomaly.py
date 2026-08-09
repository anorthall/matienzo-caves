"""Anomalies are data, not exceptions.

Parsing a corpus of hand-written legacy HTML never raises for corpus weirdness.
It records what went wrong, degrades the value, and carries on. The recorded
anomalies drive the review queue and the per-section confidence scores.

`AnomalyCode` is deliberately *closed*. An open string vocabulary makes the
review queue un-triageable within a week — you lose the ability to ask "show me
every instance of this problem" because nobody spells it the same way twice.
Adding a code is a one-line change; the constraint is the point.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel


class Severity(StrEnum):
    """How badly a value was damaged.

    The distinction drives the review queue: only `WARN` and above are queued.
    """

    INFO = "info"
    """Known corpus texture, fully handled. Recorded for statistics, never queued."""

    WARN = "warn"
    """A value was degraded — parsed, but with something lost or guessed."""

    ERROR = "error"
    """A whole section failed to parse. Should be zero after the review queue is worked."""


class AnomalyCode(StrEnum):
    # --- structural -------------------------------------------------------
    NO_BODY_TAG = "no_body_tag"
    NO_HEADER_SMALL = "no_header_small"
    HEADER_BOUNDARY_NOT_FOUND = "header_boundary_not_found"
    FOOTER_BOUNDARY_NOT_FOUND = "footer_boundary_not_found"
    SECOND_BIG_TAG = "second_big_tag"
    TTS_WRAPPER_PRESENT = "tts_wrapper_present"
    SCRIPT_TAG_PRESENT = "script_tag_present"
    UNCLOSED_TAG_RECOVERED = "unclosed_tag_recovered"
    UNTERMINATED_ATTRIBUTE = "unterminated_attribute"
    DUPLICATE_CLOSE_TAG = "duplicate_close_tag"
    STRAY_SMALL_IN_HEADER = "stray_small_in_header"
    MISSING_BODY_CLOSE = "missing_body_close"
    EMPTY_DOCUMENT = "empty_document"

    # --- encoding ---------------------------------------------------------
    CP1252_FALLBACK = "cp1252_fallback"
    MOJIBAKE_SUSPECTED = "mojibake_suspected"
    UNKNOWN_ENTITY = "unknown_entity"
    UNTERMINATED_ENTITY = "unterminated_entity"
    CONTROL_CHAR = "control_char"
    REPLACEMENT_CHAR = "replacement_char"

    # --- title ------------------------------------------------------------
    TITLE_NOT_FOUND = "title_not_found"
    TITLE_TAG_ORDER_SWAPPED = "title_tag_order_swapped"
    TITLE_NUMBER_MISMATCH = "title_number_mismatch"
    TITLE_SPACE_SEPARATOR = "title_space_separator"
    TITLE_NAME_EMPTY = "title_name_empty"
    UNBALANCED_PARENS = "unbalanced_parens"
    ALIAS_KIND_UNKNOWN = "alias_kind_unknown"

    # --- header -----------------------------------------------------------
    AREA_MISSING = "area_missing"
    AREA_UNKNOWN_VARIANT = "area_unknown_variant"
    COORD_MISSING = "coord_missing"
    COORD_PLACEHOLDER = "coord_placeholder"
    COORD_LEGACY_VN = "coord_legacy_vn"
    COORD_OUT_OF_RANGE = "coord_out_of_range"
    COORD_MULTIPLE = "coord_multiple"
    MEASURE_PROSE = "measure_prose"
    MEASURE_UNPARSED = "measure_unparsed"
    MEASURE_DUPLICATE_LABEL = "measure_duplicate_label"
    MEASURE_LABEL_UNKNOWN = "measure_label_unknown"

    # --- updated ----------------------------------------------------------
    UPDATED_DATE_UNPARSED = "updated_date_unparsed"
    UPDATED_MARKUP_NESTED = "updated_markup_nested"

    # --- footer / citations ----------------------------------------------
    FOOTER_REFERENCE_MISSING = "footer_reference_missing"
    FOOTER_LABEL_UNKNOWN = "footer_label_unknown"
    CITATION_UNPARSED = "citation_unparsed"
    CITATION_QUALIFIER_UNKNOWN = "citation_qualifier_unknown"
    CITATION_SPLIT_ANCHORS = "citation_split_anchors"
    AUTHOR_UNKNOWN_VARIANT = "author_unknown_variant"
    LINK_UNQUOTED = "link_unquoted"
    LINK_BUCKET_UNKNOWN = "link_bucket_unknown"
    LINK_PHANTOM_JS = "link_phantom_js"

    # --- body -------------------------------------------------------------
    BODY_EMPTY = "body_empty"
    BODY_LEAKED_FOOTER = "body_leaked_footer"
    BODY_LEAKED_HEADER = "body_leaked_header"
    SECTION_HEADING_AMBIGUOUS = "section_heading_ambiguous"
    TABLE_NO_HEADERS = "table_no_headers"

    # --- graph / derived --------------------------------------------------
    XREF_TARGET_MISSING = "xref_target_missing"
    XREF_SELF_REFERENCE = "xref_self_reference"
    SYSTEM_NAME_UNKNOWN = "system_name_unknown"


#: Codes that describe known, fully-handled corpus texture. They are recorded so
#: the statistics stay honest, but they never reach the review queue.
KNOWN_TEXTURE: frozenset[AnomalyCode] = frozenset(
    {
        AnomalyCode.CP1252_FALLBACK,
        AnomalyCode.COORD_MISSING,
        AnomalyCode.COORD_PLACEHOLDER,
        AnomalyCode.COORD_LEGACY_VN,
        AnomalyCode.MEASURE_PROSE,
        AnomalyCode.TITLE_SPACE_SEPARATOR,
        AnomalyCode.TITLE_TAG_ORDER_SWAPPED,
        # A third of the corpus (1,813 pages) opens with `<p>` and never emits
        # `<BODY>`. Segmentation anchors on `<BIG>`, so this costs us nothing.
        AnomalyCode.NO_BODY_TAG,
    }
)


class Anomaly(BaseModel):
    """One recorded defect, tied to the place in the source that caused it."""

    code: AnomalyCode
    severity: Severity
    detail: str
    field_path: str | None = None
    """Dotted path into `ParsedSite`, e.g. `header.quantities[1]`."""
    excerpt: str | None = None
    """Up to 200 characters of the offending source."""
    char_offset: int | None = None
    """Offset into the decoded document."""

    model_config = {"frozen": True}


class AnomalyRecorder:
    """Collects anomalies during a parse.

    One recorder per document. Passed down through the parse functions rather
    than returned up, so a deeply nested helper can report a problem without
    every intermediate signature growing an error channel.
    """

    def __init__(self) -> None:
        self._items: list[Anomaly] = []

    def add(
        self,
        code: AnomalyCode,
        detail: str,
        *,
        severity: Severity | None = None,
        field_path: str | None = None,
        excerpt: str | None = None,
        char_offset: int | None = None,
    ) -> None:
        """Record an anomaly. `severity` defaults to INFO for known corpus texture."""
        if severity is None:
            severity = Severity.INFO if code in KNOWN_TEXTURE else Severity.WARN
        self._items.append(
            Anomaly(
                code=code,
                severity=severity,
                detail=detail,
                field_path=field_path,
                excerpt=_clip(excerpt),
                char_offset=char_offset,
            )
        )

    @property
    def items(self) -> list[Anomaly]:
        return list(self._items)

    def codes(self) -> set[AnomalyCode]:
        return {a.code for a in self._items}

    def has(self, code: AnomalyCode) -> bool:
        return any(a.code is code for a in self._items)

    def count(self, severity: Severity) -> int:
        return sum(1 for a in self._items if a.severity is severity)

    def max_severity(self) -> Severity | None:
        order = (Severity.INFO, Severity.WARN, Severity.ERROR)
        present = [a.severity for a in self._items]
        return max(present, key=order.index) if present else None

    def __len__(self) -> int:
        return len(self._items)


def _clip(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


class ConfidenceScorer:
    """Per-section confidence, computed by deduction from 1.0.

    Not a single number for the document: a page can have a perfect header and a
    ruined footer, and collapsing that to one score loses exactly the
    information the review queue needs to route the work.
    """

    def __init__(self, sections: Sequence[str]) -> None:
        self._scores = dict.fromkeys(sections, 1.0)
        self._reasons: dict[str, list[str]] = {s: [] for s in sections}

    def deduct(self, section: str, amount: float, reason: str) -> None:
        """Dock `amount` from a section's score. The reason is kept so the
        review queue can say *why* a page scored badly, not just that it did."""
        if section not in self._scores:
            raise KeyError(f"unknown section {section!r}")
        self._scores[section] = max(0.0, self._scores[section] - amount)
        self._reasons[section].append(f"-{amount:g} {reason}")

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self._scores.items()}

    def reasons(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._reasons.items() if v}

    def minimum(self) -> float:
        return min(self._scores.values(), default=1.0)


SECTIONS: tuple[str, ...] = ("title", "header", "updated", "body", "footer")
