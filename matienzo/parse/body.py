"""Parse the free-text description.

This is the least regular part of the page and the place where a mistake is
hardest to see, because a wrong answer still reads like a cave description.

The single most dangerous rule is section-heading detection. Long pages break
into named sections — `Entrance Series`, `Downstream Riaño`, `Hydrology` — but
there are no heading tags anywhere in the corpus. A heading is a short bold or
underlined run standing alone in its own block. The trap is that the same `<b>`
is also used for mid-sentence emphasis of dates:

    Extension found in the boulders on the <b>2024 summer explorations, 11/8/24</b>.

Testing for bold alone turns that into a section title and everything after it
into a section that does not exist. So a heading must occupy its *whole* block,
be short, and not end in sentence punctuation. `1930` underlines its headings
rather than bolding them, so `<U>` counts too.

Cross-references come from two places and both are needed: hyperlinks to
`NNNN.htm`, and plain-text mentions like `see 1470`. The page's own number must
be excluded first — otherwise every page cites itself.
"""

from __future__ import annotations

import datetime as dt
import re

from matienzo.anomaly import AnomalyCode, AnomalyRecorder, ConfidenceScorer
from matienzo.decode import collapse_whitespace
from matienzo.htmlutil import iter_anchors, split_block_html, strip_tags
from matienzo.models import (
    BatObservation,
    Block,
    BlockKind,
    Body,
    BodyTable,
    CrossRef,
    DateMention,
    PersonMention,
    Season,
    Section,
)
from matienzo.parse.links import target_site

#: Markup with no textual content that must go before anything else looks at
#: the fragment. Comments can wrap real markup, so they are removed first.
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
SCRIPT_RE = re.compile(r"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
STYLE_RE = re.compile(r"<style\b.*?</style\s*>", re.IGNORECASE | re.DOTALL)

TABLE_RE = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)(?=<tr\b|</table)", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)(?=<t[dh]\b|</tr|</table)", re.IGNORECASE | re.DOTALL)

#: A block that is nothing but a bold or underlined run. The anchors are what
#: make this a heading test rather than an emphasis test.
HEADING_RE = re.compile(
    r"^\s*(?:<(?:b|u|strong|big)\b[^>]*>\s*)+(?P<text>.*?)\s*(?:</(?:b|u|strong|big)\s*>\s*)+$",
    re.IGNORECASE | re.DOTALL,
)

#: Editorial caveats about the page itself, written in red. Metadata, not
#: description of the cave.
EDITORIAL_RE = re.compile(r"<font\b[^>]*color\s*=\s*[\"']?#?ff0000", re.IGNORECASE)

#: Section names that recur often enough to be worth normalising.
CANONICAL_SECTIONS: dict[str, str] = {
    "cave description": "description",
    "description": "description",
    "passage description": "description",
    "introduction": "introduction",
    "hydrology": "hydrology",
    "exploration history": "exploration_history",
    "history": "exploration_history",
    "bat information": "bat_information",
    "science": "science",
    "diving": "diving",
    "archaeology": "archaeology",
    "tackle requirements": "tackle",
    "conservation and restoration": "conservation",
}

#: A heading must be short and must not read like a sentence.
HEADING_MAX_CHARS = 60
HEADING_MAX_WORDS = 8
SENTENCE_END_RE = re.compile(r"[.!?,;:]\s*$")

BAT_KEYVALUE_RE = re.compile(r"^\s*([A-Z][A-Za-z /()'-]{2,40})\s*:\s*(.+)$")

#: A plain-text reference to another site. Requires a leading cue word so that
#: bare four-digit numbers — years, altitudes, survey batch codes — are not
#: swept up as cross-references.
PLAINTEXT_REF_RE = re.compile(
    r"\b(?:site|sites|shaft|shafts|cave|caves|dig|digs|hole|holes|cueva|torca|sima|see)\s+"
    r"(?:no\.?\s*)?\(?(\d{4})\)?",
    re.IGNORECASE,
)

SEASON_RE = re.compile(
    r"\b(Easter|Whit|summer|autumn|spring|winter|Christmas|Xmas|New Year)\b\s*"
    r"((?:19|20)\d{2})?",
    re.IGNORECASE,
)
MONTH_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
DMY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)?\d{2})\b")
BATCH_RE = re.compile(r"\b(\d{4}[-_]\d{2}[-_]\d{2})\b")
#: A bare year, bounded to 1900–2030.
#:
#: Site numbers and years are the same shape, and the cue word that marks a site
#: number is often too far away to help: `other sites which also take water:
#: 2035, 2036 and 2037`. Bounding the range means anything above 2030 is read as
#: a site number rather than a date, which is right for a corpus maintained in
#: the 2020s. Residual ambiguity remains for numbers *inside* the range — a
#: reference to site 1995 with no cue word will be read as a year — and that is
#: not resolvable from the text alone.
BARE_YEAR_RE = re.compile(r"\b(19\d{2}|20[0-2]\d|2030)\b")

SEASON_ALIASES: dict[str, Season] = {
    "easter": Season.EASTER,
    "whit": Season.WHIT,
    "summer": Season.SUMMER,
    "autumn": Season.AUTUMN,
    "spring": Season.SPRING,
    "winter": Season.WINTER,
    "christmas": Season.CHRISTMAS,
    "xmas": Season.CHRISTMAS,
    "new year": Season.NEW_YEAR,
}

#: Person extraction is evidence-gated. Naive capitalised-bigram matching is
#: about 60% wrong on this corpus — it returns `Cueva Hoyuca`, `Four Valleys`
#: and `Bronze Age` as people. These patterns require an explicit cue.
PERSON_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\bby\s+(?P<name>[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){1,2})"
            r"(?:\s+and\s+(?P<second>[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){1,2}))?"
        ),
        "by_pattern",
    ),
    (
        re.compile(
            r"\b(?P<name>[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){1,2})"
            r"(?:\s+and\s+(?P<second>[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){1,2}))?"
            r"\s+(?:dived|surveyed|explored|descended|pushed|discovered|bolted)\b"
        ),
        "attribution",
    ),
)

#: Names the person patterns catch that are not people. Caving clubs are the
#: awkward ones — `explored by La Cambera` is grammatically identical to
#: `explored by Guy Simonnot`, and `La Cambera` is the Club de Montaña y
#: Espeleología La Cambera.
NOT_PEOPLE: frozenset[str] = frozenset(
    {
        # Clubs and organisations
        "la cambera",
        "el naso",
        "matienzo caves",
        "matienzo caving",
        "speleo club",
        "manchester university",
        "kendal caving",
        "cantabria government",
        # Caves, passages and places
        "cueva hoyuca",
        "fuente aguanaz",
        "four valleys",
        "south vega",
        "north vega",
        "red pike",
        "bronze age",
        "birthday passage",
        "lake bassenthwaite",
        "cave diving",
        "sump index",
        "the team",
        "the entrance",
    }
)

#: A trailing word that shows the match is a group rather than a person.
GROUP_SUFFIX_RE = re.compile(
    r"\b(?:group|club|team|society|expedition|members|cavers)\b", re.IGNORECASE
)

MINIMAL_BODY_CHARS = 70


def parse_body(
    raw_html: str,
    site_number: int,
    recorder: AnomalyRecorder,
    confidence: ConfidenceScorer,
) -> Body:
    """Turn the description markup into a `Body`."""
    cleaned = STYLE_RE.sub(" ", SCRIPT_RE.sub(" ", COMMENT_RE.sub(" ", raw_html)))

    tables = _parse_tables(cleaned)
    without_tables = TABLE_RE.sub(" ", cleaned)

    blocks, sections, editorial = _parse_blocks(without_tables)
    text = "\n\n".join(b.text for b in blocks if b.kind is not BlockKind.EDITORIAL_NOTE)

    body = Body(
        raw_html=raw_html,
        text=text,
        char_count=len(text),
        is_minimal=len(text) < MINIMAL_BODY_CHARS,
        blocks=blocks,
        sections=sections,
        tables=tables,
        editorial_notes=editorial,
        bat_observations=_parse_bat_information(blocks, sections),
        cross_refs=_parse_cross_refs(cleaned, text, site_number, recorder),
        dates=_parse_dates(text),
        people=_parse_people(text),
    )

    if not text:
        recorder.add(
            AnomalyCode.BODY_EMPTY,
            "description is empty",
            field_path="body",
        )
        confidence.deduct("body", 0.2, "empty description")
    return body


def _parse_blocks(html: str) -> tuple[list[Block], list[Section], list[str]]:
    """Split the description into blocks and identify the section headings."""
    blocks: list[Block] = []
    sections: list[Section] = []
    editorial: list[str] = []
    current_section: int | None = None

    for fragment in split_block_html(html):
        text = strip_tags(fragment)
        if not text:
            continue

        if EDITORIAL_RE.search(fragment):
            editorial.append(text)
            blocks.append(Block(index=len(blocks), kind=BlockKind.EDITORIAL_NOTE, text=text))
            continue

        heading = _as_heading(fragment, text)
        if heading is not None:
            current_section = len(sections)
            sections.append(
                Section(
                    index=current_section,
                    heading=heading,
                    heading_kind=_heading_kind(fragment),
                    canonical=CANONICAL_SECTIONS.get(heading.lower().rstrip(":")),
                    block_first=len(blocks),
                    block_last=len(blocks),
                )
            )
            blocks.append(
                Block(
                    index=len(blocks),
                    kind=BlockKind.SECTION_HEADING,
                    text=text,
                    section_index=current_section,
                )
            )
            continue

        blocks.append(
            Block(
                index=len(blocks),
                kind=BlockKind.PARAGRAPH,
                text=text,
                section_index=current_section,
            )
        )
        if current_section is not None:
            sections[current_section].block_last = len(blocks) - 1

    return blocks, sections, editorial


def _as_heading(fragment: str, text: str) -> str | None:
    """The heading text if this block is a section title, else None.

    All four conditions are load-bearing. The block must be *wholly* wrapped in
    emphasis (not merely contain some), because mid-sentence bold dates are
    common; and it must be short and unpunctuated, because a whole emphasised
    sentence is a quotation, not a heading.
    """
    match = HEADING_RE.match(fragment.strip())
    if match is None:
        return None
    if len(text) > HEADING_MAX_CHARS or len(text.split()) > HEADING_MAX_WORDS:
        return None
    if SENTENCE_END_RE.search(text) and not text.rstrip().endswith(":"):
        return None
    if not re.search(r"[A-Za-z]", text):
        return None
    return text.rstrip(":").strip()


def _heading_kind(fragment: str) -> str:
    lowered = fragment.lower()
    if "<u" in lowered:
        return "underline"
    if "<big" in lowered:
        return "big"
    return "bold"


def _parse_tables(html: str) -> list[BodyTable]:
    """Extract tables as structured rows.

    The survey-batch registers are the only place in the corpus where a batch
    id, a date and a list of surveyors sit together, so they are worth keeping
    as data rather than flattening into prose.
    """
    tables: list[BodyTable] = []
    for index, match in enumerate(TABLE_RE.finditer(html)):
        rows = [
            [strip_tags(cell) for cell in CELL_RE.findall(row)]
            for row in ROW_RE.findall(match.group(0))
        ]
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            continue

        headers: list[str] = []
        if _looks_like_header(rows[0]):
            headers, rows = rows[0], rows[1:]

        tables.append(
            BodyTable(
                index=index,
                headers=headers,
                rows=rows,
                looks_like="survey_batches" if _is_survey_batch_table(headers) else "unknown",
            )
        )
    return tables


def _looks_like_header(row: list[str]) -> bool:
    """A header row is short labels, none of which is a bare number."""
    filled = [cell for cell in row if cell]
    if not filled:
        return False
    return all(len(cell) < 40 and not cell.replace(".", "").isdigit() for cell in filled)


def _is_survey_batch_table(headers: list[str]) -> bool:
    lowered = {h.lower() for h in headers}
    return any("batch" in h for h in lowered) and any(
        key in h for h in lowered for key in ("surveyor", "date", "name")
    )


def _parse_bat_information(blocks: list[Block], sections: list[Section]) -> list[BatObservation]:
    """Read the `Bat information` key/value block found on 13 pages."""
    observations: list[BatObservation] = []
    for section in sections:
        if section.canonical != "bat_information":
            continue
        fields: dict[str, str] = {}
        for block in blocks[section.block_first + 1 : section.block_last + 1]:
            for line in re.split(r"(?<=[a-z0-9)])\s(?=[A-Z][a-z]+\s*:)", block.text):
                if match := BAT_KEYVALUE_RE.match(line.strip()):
                    fields[match.group(1).strip()] = match.group(2).strip()
        if fields:
            date_raw = fields.get("Date")
            observations.append(
                BatObservation(
                    date=_parse_dmy(date_raw) if date_raw else None,
                    date_raw=date_raw,
                    fields=fields,
                )
            )
    return observations


#: Two-digit years pivot here. Exploration in Matienzo began in the 1960s and
#: the corpus runs to the present, so `9/01/72` is 1972 while `9/4/23` is 2023.
#: A blanket `+2000` dates the earliest expeditions to the 2070s.
TWO_DIGIT_YEAR_PIVOT = 50


def _parse_dmy(text: str) -> dt.date | None:
    match = DMY_RE.search(text)
    if match is None:
        return None
    day, month, year = (int(g) for g in match.groups())
    if year < 100:
        year += 2000 if year < TWO_DIGIT_YEAR_PIVOT else 1900
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _parse_cross_refs(
    html: str, text: str, site_number: int, recorder: AnomalyRecorder
) -> list[CrossRef]:
    """Every reference from this description to another site.

    Hyperlinks and plain-text mentions both count. Self-references are dropped:
    a page that cites its own number is describing itself, and letting those
    through makes the link graph useless.
    """
    refs: list[CrossRef] = []
    seen: set[tuple[int, str]] = set()

    for anchor in iter_anchors(html):
        target, _ = target_site(anchor.href)
        if target is None or target == site_number:
            if target == site_number:
                recorder.add(
                    AnomalyCode.XREF_SELF_REFERENCE,
                    f"link to its own page: {anchor.href!r}",
                    field_path="body.cross_refs",
                )
            continue
        key = (target, "hyperlink")
        if key in seen:
            continue
        seen.add(key)
        fragment = anchor.href.partition("#")[2] or None
        refs.append(
            CrossRef(
                target_site=target,
                kind="hyperlink",
                href=anchor.href,
                fragment=fragment,
                link_text=anchor.text,
                context=anchor.text,
                from_zone="body",
            )
        )

    for match in PLAINTEXT_REF_RE.finditer(text):
        target = int(match.group(1))
        if target == site_number or (target, "hyperlink") in seen:
            continue
        key = (target, "plaintext")
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            CrossRef(
                target_site=target,
                kind="plaintext",
                context=collapse_whitespace(text[max(0, match.start() - 60) : match.end() + 60]),
                from_zone="body",
            )
        )

    return refs


def _parse_dates(text: str) -> list[DateMention]:
    """Dated events mentioned in the description.

    The season vocabulary is closed, which is what makes this tractable; the
    prepositions in front of it are not, so only the date token is matched.
    """
    mentions: list[DateMention] = []

    # Site numbers between 1900 and 2030 are indistinguishable from years —
    # `confusion between this site and site 2073` is not a date. Claiming the
    # cross-reference spans first means the bare-year pass cannot take them.
    claimed: list[tuple[int, int]] = [m.span() for m in PLAINTEXT_REF_RE.finditer(text)]

    def overlaps(start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in claimed)

    for match in BATCH_RE.finditer(text):
        claimed.append(match.span())
        mentions.append(
            DateMention(raw=match.group(0), kind="survey_batch", batch_code=match.group(1))
        )

    for match in DMY_RE.finditer(text):
        claimed.append(match.span())
        parsed = _parse_dmy(match.group(0))
        mentions.append(
            DateMention(
                raw=match.group(0),
                kind="dmy",
                year=parsed.year if parsed else None,
                month=parsed.month if parsed else None,
                day=parsed.day if parsed else None,
            )
        )

    for match in SEASON_RE.finditer(text):
        if overlaps(*match.span()):
            continue
        claimed.append(match.span())
        year = int(match.group(2)) if match.group(2) else None
        mentions.append(
            DateMention(
                raw=collapse_whitespace(match.group(0)),
                kind="season_year" if year else "season",
                year=year,
                season=SEASON_ALIASES[match.group(1).lower()],
            )
        )

    for match in MONTH_YEAR_RE.finditer(text):
        if overlaps(*match.span()):
            continue
        claimed.append(match.span())
        mentions.append(
            DateMention(
                raw=match.group(0),
                kind="month_year",
                year=int(match.group(2)),
                month=_month_number(match.group(1)),
            )
        )

    for match in BARE_YEAR_RE.finditer(text):
        if overlaps(*match.span()):
            continue
        claimed.append(match.span())
        mentions.append(DateMention(raw=match.group(0), kind="year", year=int(match.group(1))))

    return mentions


def _month_number(name: str) -> int:
    months = [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
    return months.index(name.lower()) + 1


def _parse_people(text: str) -> list[PersonMention]:
    """Named explorers, extracted only where the sentence says so.

    Recall is deliberately low. A `person` table trustworthy at 40% recall is
    more useful than one at 80% that is 60% place names.
    """
    people: list[PersonMention] = []
    seen: set[str] = set()

    for pattern, evidence in PERSON_PATTERNS:
        for match in pattern.finditer(text):
            for group in ("name", "second"):
                name = match.groupdict().get(group)
                if not name:
                    continue
                name = collapse_whitespace(name)
                if name.lower() in NOT_PEOPLE or name.lower() in seen:
                    continue
                # `by the Fresnedo group` names a party, not a person.
                if GROUP_SUFFIX_RE.search(text[match.end() : match.end() + 12]):
                    continue
                seen.add(name.lower())
                people.append(PersonMention(name_raw=name, evidence=evidence, confidence=0.8))
    return people
