"""Parse the `NNNN: Name (alias) (alias)` heading.

Two things here are less trivial than they look.

**Parentheses nest.** `Torca de … (3424 (French: SCD))` and
`Comellantes, Cueva del (Comediante, Cueva del) (Comellante, Cueva del)` both
occur, up to four groups deep. The obvious `\\(([^)]*)\\)` splits 61 headings
wrongly — it stops at the inner `)` and leaves a dangling `)` behind. A depth
counter is the only thing that gets this right.

**Spanish names are inverted.** The corpus writes `Burro, Sima del` rather than
`Sima del Burro`, so a plain lowercase of the name sorts and searches badly.
`name_sort` de-inverts and strips accents, which is what makes `sima del burro`
and `riano` find the right page.
"""

from __future__ import annotations

import re
import unicodedata

from matienzo.anomaly import AnomalyCode, AnomalyRecorder, ConfidenceScorer, Severity
from matienzo.decode import unescape
from matienzo.htmlutil import strip_tags
from matienzo.models import Alias, AliasKind, Title

#: `0001: Burro, Sima del` — but ten pages use a space instead of the colon,
#: e.g. `0007 West Ozana Pots`.
HEADING_RE = re.compile(r"^\s*(?P<number>\d{1,4})\s*(?P<sep>:|\s)\s*(?P<rest>.*)$", re.DOTALL)

#: Generic descriptors that are a site *type* rather than a proper name. Ordered
#: longest-first so `resurgence & sink` wins over `sink`.
SITE_TYPES: tuple[str, ...] = (
    "resurgence & sink",
    "shaft, cave",
    "cave/dig",
    "rock shelter",
    "depression",
    "resurgence",
    "collapse",
    "shelter",
    "shafts",
    "caves",
    "sinks",
    "holes",
    "rifts",
    "digs",
    "shaft",
    "cave",
    "rift",
    "sink",
    "hole",
    "dig",
)

#: `(3424 (French: SCD))` and similar club catalogue references.
FRENCH_REF_RE = re.compile(r"\bFrench\s*:", re.IGNORECASE)
ENTRANCE_QUALIFIER_RE = re.compile(
    r"\b(top|bottom|upper|lower|middle|north|south|east|west|new|old)\b.*\bentrances?\b"
    r"|\bentrances?\b",
    re.IGNORECASE,
)
SYSTEM_RE = re.compile(r"\bSistema\b|\bSystem\b", re.IGNORECASE)
BARE_NUMBER_RE = re.compile(r"^\d{1,4}$")
SITE_REF_RE = re.compile(r"\b(\d{4})\b")


def parse_title(
    raw_html: str,
    site_number: int,
    recorder: AnomalyRecorder,
    confidence: ConfidenceScorer,
) -> Title | None:
    """Turn the heading markup into a `Title`.

    `site_number` comes from the filename and always wins: it matches the
    heading on all 5,557 pages, but the filename is the thing we can trust
    unconditionally.
    """
    text = strip_tags(raw_html)
    match = HEADING_RE.match(text)
    if match is None:
        recorder.add(
            AnomalyCode.TITLE_NOT_FOUND,
            f"heading {text!r} is not `NNNN: name`",
            severity=Severity.ERROR,
            field_path="title",
        )
        confidence.deduct("title", 0.6, "heading does not match NNNN: name")
        return None

    if int(match.group("number")) != site_number:
        recorder.add(
            AnomalyCode.TITLE_NUMBER_MISMATCH,
            f"heading says {match.group('number')} but the file is {site_number:04d}",
            severity=Severity.ERROR,
            field_path="title",
        )
        confidence.deduct("title", 0.6, "heading number disagrees with the filename")

    separator = "colon" if match.group("sep") == ":" else "space"
    if separator == "space":
        recorder.add(
            AnomalyCode.TITLE_SPACE_SEPARATOR,
            "heading separates the number and name with a space, not a colon",
            field_path="title",
        )

    name, alias_texts, balanced = split_aliases(match.group("rest"))
    if not balanced:
        recorder.add(
            AnomalyCode.UNBALANCED_PARENS,
            f"unbalanced parentheses in {match.group('rest')!r}",
            field_path="title",
        )
        confidence.deduct("title", 0.2, "unbalanced parentheses in the heading")

    # `0249: -` uses a hyphen as the placeholder for a reallocated number.
    if not name or name == "-":
        recorder.add(
            AnomalyCode.TITLE_NAME_EMPTY,
            f"heading has a number but no name ({name!r})",
            field_path="title",
        )
        confidence.deduct("title", 0.4, "heading has no name")

    aliases = [classify_alias(t, recorder) for t in alias_texts]

    return Title(
        raw=unescape(strip_tags(raw_html)),
        site_number=site_number,
        name=name,
        name_sort=sort_key(name),
        site_type=infer_site_type(name),
        aliases=aliases,
        separator=separator,
    )


def split_aliases(text: str) -> tuple[str, list[str], bool]:
    """Split `Name (alias) (alias)` into the name and its parenthesised aliases.

    Scans with a depth counter so that a nested `(3424 (French: SCD))` is
    returned whole rather than truncated at the inner bracket. Returns the name,
    the top-level alias contents, and whether the brackets balanced.
    """
    name_parts: list[str] = []
    aliases: list[str] = []
    depth = 0
    current: list[str] = []
    balanced = True

    for char in text:
        if char == "(":
            if depth == 0:
                current = []
            else:
                current.append(char)
            depth += 1
        elif char == ")":
            if depth == 0:
                balanced = False  # a stray `)` with no opener
                continue
            depth -= 1
            if depth == 0:
                alias = "".join(current).strip()
                if alias:
                    aliases.append(alias)
            else:
                current.append(char)
        elif depth:
            current.append(char)
        else:
            name_parts.append(char)

    if depth:
        # An unclosed `(` — keep what we gathered rather than discarding it.
        balanced = False
        alias = "".join(current).strip()
        if alias:
            aliases.append(alias)

    return " ".join("".join(name_parts).split()), aliases, balanced


def classify_alias(text: str, recorder: AnomalyRecorder) -> Alias:
    """Label an alias by what it actually is.

    Order matters: a French catalogue reference is also a bare-ish number, and a
    system name may mention an entrance.
    """
    if FRENCH_REF_RE.search(text):
        kind = AliasKind.FRENCH_REF
    elif BARE_NUMBER_RE.match(text):
        kind = AliasKind.BARE_NUMBER
    elif SYSTEM_RE.search(text):
        kind = AliasKind.SYSTEM_NAME
    elif ENTRANCE_QUALIFIER_RE.search(text):
        kind = AliasKind.ENTRANCE_QUALIFIER
    elif re.search(r"[A-Za-z]", text):
        kind = AliasKind.SPANISH_ALT
    else:
        kind = AliasKind.UNKNOWN
        recorder.add(
            AnomalyCode.ALIAS_KIND_UNKNOWN,
            f"cannot classify alias {text!r}",
            field_path="title.aliases",
        )

    site_ref = SITE_REF_RE.search(text) if kind is not AliasKind.FRENCH_REF else None
    return Alias(
        text=text,
        kind=kind,
        site_number=int(site_ref.group(1)) if site_ref else None,
    )


def infer_site_type(name: str) -> str | None:
    """The generic descriptor a heading uses in place of a proper name.

    Most sites are `shaft`, `cave` or `dig` rather than something named, and the
    count suffix in `shafts - 2` is part of the descriptor, not the name.
    """
    stripped = re.sub(r"\s*-\s*\d+\s*$", "", name).strip().lower()
    if stripped in SITE_TYPES:
        return stripped
    for candidate in SITE_TYPES:
        if re.fullmatch(rf"(?:a |an |the )?{re.escape(candidate)}", stripped):
            return candidate
    return None


def sort_key(name: str) -> str:
    """De-invert and unaccent a name for sorting and fuzzy lookup.

    `Burro, Sima del` → `sima del burro`; `Riaño, Cueva de` → `cueva de riano`.
    The corpus writes Spanish names head-noun-last, which sorts uselessly and
    means a user typing the name as they'd say it never matches.
    """
    head, _, tail = name.partition(",")
    reordered = f"{tail.strip()} {head.strip()}".strip() if tail.strip() else name
    folded = unicodedata.normalize("NFD", reordered.lower())
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())
