"""Every Pydantic model the pipeline produces.

One module rather than a package: the models are mutually referential, and
splitting them buys nothing but circular imports.

Three rules shape everything here:

1. **Every model carries the source text it came from.** A `raw` field beside
   each parsed group costs a few megabytes across the corpus and is the
   difference between a database that is an archive and one that is a lossy
   summary.
2. **Never model "sometimes a number, sometimes prose" as `float | str`.** That
   pushes the ambiguity onto every consumer. Model it as a record that states
   how it should be read — see `Quantity`.
3. **Anomalies are data.** Parsing degrades a value and records why; it does not
   raise.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field

from matienzo.anomaly import Anomaly

type SiteNumber = int


class SegmentFlags(BaseModel):
    """Which structural segments were located in a source page.

    Recorded per file so `matienzo audit` can show, at a glance, whether a parse
    failure is "this page is a stub" or "the segmenter broke".
    """

    title: bool = False
    header: bool = False
    updated: bool = False
    body: bool = False
    footer: bool = False
    modal: bool = False

    def missing(self) -> list[str]:
        return [name for name, found in self.model_dump().items() if not found]


# --------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------


class QuantityKind(StrEnum):
    NUMERIC = "numeric"
    RANGE = "range"
    COMPOUND = "compound"
    PROSE = "prose"
    UNKNOWN = "unknown"


class Modifier(StrEnum):
    EXACT = "exact"
    CIRCA = "circa"
    """`c100m` — an estimate."""
    AT_LEAST = "at_least"
    """`290m+` — surveyed so far, expected to grow."""
    AT_MOST = "at_most"
    """`up to 5m` / `<8m` — an upper bound rather than a measurement."""
    UNCERTAIN = "uncertain"
    """`5m ?` / `?m` — the recorder was unsure."""
    RELATIVE = "relative"
    """`-5m` / `+11` — a vertical range signed against the entrance."""


class ProseRelation(StrEnum):
    """What a non-numeric Length or Depth is actually saying.

    These values are not noise: `Length: included in the Four Valleys System`
    records cave-system membership, which is real data that a `float | None`
    column would silently discard.
    """

    INCLUDED_IN = "included_in"
    PART_OF_SYSTEM = "part_of_system"
    ADDED_TO = "added_to"
    SEE_OTHER = "see_other"
    TRAVERSE_OF = "traverse_of"
    UNPARSED = "unparsed"


class ProseQuantity(BaseModel):
    """A structured reading of a prose measurement. Nothing is discarded."""

    relation: ProseRelation = ProseRelation.UNPARSED
    target_sites: list[SiteNumber] = Field(default_factory=list)
    system_name: str | None = None
    extra_value_m: float | None = None
    """`(870m added to Risco)` — a number that belongs to *another* site."""


class Quantity(BaseModel):
    """One measurement slot from the header info line.

    `raw` is always populated; everything else may not be. `kind` tells the
    consumer which fields are meaningful, so nobody has to guess whether a
    missing `value_m` means "zero", "unknown" or "see the prose".
    """

    label: str
    """Normalised: length | depth | altitude | vertical_range | height."""
    label_raw: str
    raw: str
    kind: QuantityKind
    value_m: float | None = None
    min_m: float | None = None
    max_m: float | None = None
    modifier: Modifier = Modifier.EXACT
    unit_stated: bool = True
    parts: list[float] = Field(default_factory=list)
    """`5 & 5m` → [5.0, 5.0]. `value_m` carries their sum."""
    prose: ProseQuantity | None = None


# --------------------------------------------------------------------------
# Title
# --------------------------------------------------------------------------


class AliasKind(StrEnum):
    SPANISH_ALT = "spanish_alt"
    FRENCH_REF = "french_ref"
    """`(3424 (French: SCD))` — a French club's catalogue number."""
    ENTRANCE_QUALIFIER = "entrance_qualifier"
    """`(top entrance)`."""
    BARE_NUMBER = "bare_number"
    SYSTEM_NAME = "system_name"
    UNKNOWN = "unknown"


class Alias(BaseModel):
    text: str
    kind: AliasKind = AliasKind.UNKNOWN
    site_number: SiteNumber | None = None
    """Set when the alias is a reference to another site."""


class Title(BaseModel):
    raw: str
    site_number: SiteNumber
    name: str
    """As written, inverted Spanish form preserved: `Burro, Sima del`."""
    name_sort: str
    """De-inverted, unaccented, lowercased: `sima del burro`."""
    site_type: str | None = None
    """`shaft`, `cave`, `dig`… taken from the name when it is generic."""
    aliases: list[Alias] = Field(default_factory=list)
    separator: str = "colon"


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------


class CoordSystem(StrEnum):
    UTM = "utm"
    VN_GRID = "vn_grid"
    """Pre-ETRS89 Spanish grid reference, `VN########`."""
    PLACEHOLDER = "placeholder"
    """`30T 04- 47-`, `30T ??`, `VN????????` — recorded as unknown, not dropped."""


class Coordinate(BaseModel):
    raw: str
    label: str | None = None
    """`Top entrance`, `Bottom entrance #5451` on multi-entrance pages."""
    entrance_site_number: SiteNumber | None = None
    system: CoordSystem = CoordSystem.UTM
    zone: str | None = "30T"
    easting: int | None = None
    northing: int | None = None
    vn_ref: str | None = None
    datum: str | None = None
    accuracy_code: str | None = None
    altitude_m: float | None = None
    """Altitude belongs to the entrance, not to the site."""
    latitude: float | None = None
    longitude: float | None = None

    @property
    def is_placeholder(self) -> bool:
        return self.system is CoordSystem.PLACEHOLDER


class Header(BaseModel):
    raw: str
    area_raw: str | None = None
    area_canonical: str | None = None
    coordinates: list[Coordinate] = Field(default_factory=list)
    quantities: list[Quantity] = Field(default_factory=list)
    field_order: list[str] = Field(default_factory=list)
    duplicate_labels: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Update history
# --------------------------------------------------------------------------


class DatePrecision(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    RANGE = "range"


class UpdateDate(BaseModel):
    raw: str
    group_index: int
    """Index of the `;`-separated group this date came from."""
    date: dt.date | None = None
    precision: DatePrecision = DatePrecision.DAY
    end_date: dt.date | None = None
    """`13th-15th November 2019`."""
    attribution: str | None = None
    """`9th July 2024 (Simon Cornhill)`."""
    inferred_month: bool = False
    inferred_year: bool = False
    """Within a group the month and year are omitted until the last date:
    `9th November, 6th December 2003` — the 9th is November *2003*."""


# --------------------------------------------------------------------------
# Body
# --------------------------------------------------------------------------


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    SECTION_HEADING = "section_heading"
    EDITORIAL_NOTE = "editorial_note"
    """Red `<FONT COLOR="#ff0000">` caveats — metadata about the page, not
    description of the cave."""
    TABLE = "table"
    LIST = "list"
    KEYVALUE = "keyvalue"


class Block(BaseModel):
    index: int
    kind: BlockKind = BlockKind.PARAGRAPH
    text: str
    section_index: int | None = None


class Section(BaseModel):
    index: int
    heading: str
    heading_kind: str = "bold"
    """`bold`, `underline` (1930 uses `<U>`), `font` or `hr`."""
    canonical: str | None = None
    block_first: int = 0
    block_last: int = 0


class BodyTable(BaseModel):
    index: int
    caption: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    looks_like: str = "unknown"
    """`survey_batches` tables carry surveyor names, dates and batch IDs — the
    only place in the corpus where those three are reliably co-located."""


class BatObservation(BaseModel):
    """The `Bat information` key/value block, present on 13 pages."""

    date: dt.date | None = None
    date_raw: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)


class CrossRef(BaseModel):
    target_site: SiteNumber
    kind: str
    """`hyperlink`, `plaintext`, `gallery`, `alias`, `prose_quantity`."""
    href: str | None = None
    fragment: str | None = None
    link_text: str | None = None
    context: str = ""
    from_zone: str = "body"


class Season(StrEnum):
    EASTER = "Easter"
    WHIT = "Whit"
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"
    CHRISTMAS = "Christmas"
    NEW_YEAR = "New Year"


class DateMention(BaseModel):
    raw: str
    kind: str
    year: int | None = None
    month: int | None = None
    day: int | None = None
    season: Season | None = None
    batch_code: str | None = None


class PersonMention(BaseModel):
    """A named explorer.

    `evidence` is mandatory and never `bigram`. Naive capitalised-bigram
    extraction is ~60% wrong on this corpus — it surfaces `Cueva Hoyuca`,
    `Four Valleys` and `Bronze Age` as people. A table trustworthy at 40% recall
    beats an untrustworthy one at 80%.
    """

    name_raw: str
    name_canonical: str | None = None
    evidence: str = "by_pattern"
    """`by_pattern`, `attribution`, `survey_table` or `roster_match`."""
    confidence: float = 1.0


class Body(BaseModel):
    raw_html: str
    text: str
    """The canonical plain text — what gets indexed, chunked and embedded."""
    char_count: int = 0
    is_minimal: bool = False
    """Under 70 visible characters. 911 pages qualify; a legitimate class of
    record, not an extraction failure."""
    blocks: list[Block] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    tables: list[BodyTable] = Field(default_factory=list)
    editorial_notes: list[str] = Field(default_factory=list)
    bat_observations: list[BatObservation] = Field(default_factory=list)
    cross_refs: list[CrossRef] = Field(default_factory=list)
    dates: list[DateMention] = Field(default_factory=list)
    people: list[PersonMention] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Footer
# --------------------------------------------------------------------------


class LinkBucket(StrEnum):
    LOGBOOK_PDF = "logbook_pdf"
    SCANNED_PUB = "scanned_pub"
    ENTRANCE_PICS = "entrance_pics"
    UNDERGROUND_PICS = "underground_pics"
    MISC_PICS = "misc_pics"
    SURVEY_IMAGE = "survey_image"
    SURVEY_PDF = "survey_pdf"
    SURVEY_3D = "survey_3d"
    SURVEY_OTHER = "survey_other"
    VIDEO_FILE = "video_file"
    VIDEO_EXTERNAL = "video_external"
    ROSE_DIAGRAM = "rose_diagram"
    CANTAB = "cantab"
    SCIENCE = "science"
    DIVING = "diving"
    MISC_DOC = "misc_doc"
    SITE_PAGE = "site_page"
    EXTERNAL = "external"
    ANCHOR = "anchor"
    BOILERPLATE = "boilerplate"
    OTHER = "other"


class ResourceLink(BaseModel):
    href: str
    text: str = ""
    bucket: LinkBucket = LinkBucket.OTHER
    target_site: SiteNumber | None = None
    is_range: bool = False
    """Gallery filenames encode ranges: `1930-2011e.htm`."""
    unquoted_in_source: bool = False


class CitationKind(StrEnum):
    PUBLICATION = "publication"
    LOGBOOK = "logbook"
    SURVEY = "survey"
    PROVENANCE = "provenance"
    """`material in file`, `card`, `pers comm` — a note about where the record
    lives, not a bibliographic entry."""
    UNPARSED = "unparsed"


class Citation(BaseModel):
    raw: str
    ordinal: int = 0
    author_raw: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    disambiguator: str | None = None
    """The `b` of `anon., 2005b`, which maps to the qualifier."""
    qualifier: str | None = None
    qualifier_is_known: bool = False
    kind: CitationKind = CitationKind.UNPARSED
    links: list[ResourceLink] = Field(default_factory=list)
    """A citation split across two anchors — `anon., 2005b (<a>Easter</a> &amp;
    <a>summer</a>)`, 234 pages — stays one citation with two links."""
    attachments: list[ResourceLink] = Field(default_factory=list)
    """A nested `(<a>survey</a>)` belongs to the preceding citation."""


class FooterField(BaseModel):
    label_raw: str
    label: str
    is_known_label: bool = True
    order: int = 0
    text: str = ""
    is_empty: bool = True
    links: list[ResourceLink] = Field(default_factory=list)


class Footer(BaseModel):
    raw: str
    reference_label: str = "Reference"
    citations: list[Citation] = Field(default_factory=list)
    fields: list[FooterField] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where a record came from and how well it parsed."""

    site_number: SiteNumber
    source_path: str
    content_sha256: str
    byte_size: int
    parser_version: str
    encoding_used: str
    segments_found: SegmentFlags
    confidence: dict[str, float] = Field(default_factory=dict)
    confidence_reasons: dict[str, list[str]] = Field(default_factory=dict)
    anomalies: list[Anomaly] = Field(default_factory=list)

    @property
    def anomaly_codes(self) -> set[str]:
        return {a.code for a in self.anomalies}


class ParsedSite(BaseModel):
    """One cave description page, fully parsed.

    Serialises losslessly to JSON — the golden-file tests assert the round trip,
    because a model that cannot survive `model_dump_json` cannot be a golden.
    """

    site_number: SiteNumber
    title: Title | None = None
    header: Header | None = None
    updated: list[UpdateDate] = Field(default_factory=list)
    body: Body
    footer: Footer | None = None
    provenance: Provenance

    @property
    def is_stub(self) -> bool:
        return self.header is None and self.footer is None
