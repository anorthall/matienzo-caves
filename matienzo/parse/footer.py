"""Parse the footer's resource fields.

The footer is a run of `<B>Label</B>: value` pairs. It looks like the most
regular part of the page and has two traps that cost about 290 pages between
them if you write the obvious regex:

**The colon is sometimes inside the bold tag.** `<B>Entrance picture </B>:` and
`<B>Entrance pictures :</B>` both occur. Requiring `</B>\\s*:` misses 175 pages.

**Opening and closing tag case need not match.** `<B>Reference</b>` and
`<b>Reference</B>` both occur, so a case-sensitive pattern misses 115 more.

`Reference` is the only field present on every non-stub page and is always
first; everything else is optional and most are usually empty. Fields are read
by label rather than by position, because `Video` floats through four positions
and the long tail of one-off labels shifts everything after it.
"""

from __future__ import annotations

import re

from matienzo.anomaly import AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.htmlutil import iter_anchors, strip_tags
from matienzo.models import CitationKind, Footer, FooterField
from matienzo.parse.citations import parse_citations
from matienzo.parse.links import make_link

#: `<B>Label</B>:` tolerating either colon placement and mismatched tag case.
FIELD_LABEL_RE = re.compile(
    r"<b\b[^>]*>\s*(?P<label>[^<]{1,60}?)\s*:?\s*</b\s*>\s*:?",
    re.IGNORECASE,
)

#: Canonical slugs for the labels that recur. Singular/plural and case are
#: folded; anything outside this map is kept verbatim and flagged.
KNOWN_LABELS: dict[str, str] = {
    "reference": "references",
    "references": "references",
    "entrance picture": "entrance_pictures",
    "entrance pictures": "entrance_pictures",
    "entrance picture(s)": "entrance_pictures",
    "entrance pictures(s)": "entrance_pictures",
    "underground picture": "underground_pictures",
    "underground pictures": "underground_pictures",
    "underground picture(s)": "underground_pictures",
    "video": "video",
    "video(s)": "video",
    "videos": "video",
    "detailed survey": "detailed_survey",
    "detailed surveys": "detailed_survey",
    "line survey": "line_survey",
    "on area survey": "on_area_survey",
    "on area map": "on_area_survey",
    "survex file": "survex_file",
    "passage direction rose diagram": "rose_diagram",
    "hydrology": "hydrology",
    "archaeology": "archaeology",
    "archaeological remains": "archaeology",
    "sketch survey": "sketch_survey",
    "survey": "survey",
    "surveys": "survey",
    "survex files": "survex_file",
    "entrance video": "entrance_video",
    "entrance videos": "entrance_video",
    "underground video": "underground_video",
    "underground videos": "underground_video",
    "miscellaneous": "miscellaneous",
    "misc": "miscellaneous",
    "misc. pic": "miscellaneous",
    "other pictures": "other_pictures",
    "pictures": "other_pictures",
    "entrance and other pictures": "other_pictures",
    "logbook": "logbook",
    "log book": "logbook",
    "visual topo": "visual_topo",
}

#: A value that is only a non-breaking space means "empty", not "contains a
#: space" — six pages in the corpus, and they would otherwise read as populated.
EMPTY_VALUE_RE = re.compile(r"^(?:\s| )*$")

REFERENCE_LABEL_RE = re.compile(r"^references?$", re.IGNORECASE)


def parse_footer(raw_html: str, recorder: AnomalyRecorder, confidence: ConfidenceScorer) -> Footer:
    """Turn the footer markup into a `Footer`."""
    footer = Footer(raw=strip_tags(raw_html))
    labels = list(FIELD_LABEL_RE.finditer(raw_html))
    unknown_labels: list[str] = []

    if not labels:
        recorder.add(
            AnomalyCode.FOOTER_REFERENCE_MISSING,
            "footer has no <B>Label</B> fields",
            field_path="footer",
        )
        confidence.deduct("footer", 0.7, "no footer fields")
        return footer

    for order, match in enumerate(labels):
        label_raw = strip_tags(match.group("label"))
        end = labels[order + 1].start() if order + 1 < len(labels) else len(raw_html)
        value_html = raw_html[match.end() : end]

        known_slug = KNOWN_LABELS.get(_normalise(label_raw))
        is_known = known_slug is not None
        slug = known_slug if known_slug is not None else _slugify(label_raw)
        if known_slug is None:
            unknown_labels.append(label_raw)
            recorder.add(
                AnomalyCode.FOOTER_LABEL_UNKNOWN,
                f"unrecognised footer label {label_raw!r}",
                field_path="footer.fields",
            )

        value_text = strip_tags(value_html)
        field = FooterField(
            label_raw=label_raw,
            label=slug,
            is_known_label=is_known,
            order=order,
            text=value_text,
            is_empty=bool(EMPTY_VALUE_RE.match(value_text)),
            links=[
                make_link(a.href, a.text, unquoted=a.unquoted) for a in iter_anchors(value_html)
            ],
        )
        footer.fields.append(field)

        if REFERENCE_LABEL_RE.match(label_raw.strip().rstrip(":")):
            footer.reference_label = label_raw.strip().rstrip(":")
            footer.citations = parse_citations(value_html, recorder)

    if not any(f.label == "references" for f in footer.fields):
        recorder.add(
            AnomalyCode.FOOTER_REFERENCE_MISSING,
            "footer has fields but no Reference(s) label",
            field_path="footer",
        )
        confidence.deduct("footer", 0.7, "no Reference field")

    # Both of these are per-occurrence problems on a page that may have very
    # many occurrences, so the total is capped. Uncapped, `0733` — Vallina, whose
    # footer groups photo links under year headings like `2026 Easter`, and whose
    # 114 citations all parse — scored 0.00 and looked like the worst page in the
    # corpus. Confidence is meant to rank pages for review; a measure that a
    # healthy page can bottom out on cannot do that.
    _deduct_capped(confidence, len(unknown_labels), 0.03, 0.15, "unrecognised footer labels")
    unparsed = sum(1 for c in footer.citations if c.kind is CitationKind.UNPARSED)
    _deduct_capped(confidence, unparsed, 0.05, 0.25, "unparsed citations")

    return footer


def _deduct_capped(
    confidence: ConfidenceScorer, count: int, each: float, cap: float, reason: str
) -> None:
    if count:
        confidence.deduct("footer", min(count * each, cap), f"{count} {reason}")


def _normalise(label: str) -> str:
    """Fold a label to its lookup key: lowercase, single-spaced, no trailing colon."""
    return re.sub(r"\s+", " ", label.strip().rstrip(":").strip()).lower()


def _slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalise(label)).strip("_") or "unnamed"
