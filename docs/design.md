I verified the corpus and the local toolchain before designing. Key confirmations: `data/pages/` holds **5,288** `.htm` files (269 numbers in 0001–5557 are absent — the site 404s them), and both the Homebrew CPython 3.14.6 and uv-managed CPythons on this machine ship `sqlite3` with `enable_load_extension` **available**, FTS5, the `trigram` tokenizer, and R*Tree. That last fact is load-bearing for the vector-search recommendation below.

---

# matienzo — design document

## 0. Governing principle

**The `data/` tree — the scraped corpus in `data/pages/` plus a small hand-curated set of vocabularies and overrides — is the only source of truth. `matienzo.db` is a disposable derived artifact that can be deleted and rebuilt from scratch at any time in a couple of minutes.**

Every design decision below follows from that. It is what makes the review queue tractable (§5), what makes re-scraping safe (§6), and what lets you throw away and redo the embedding layer without touching parse work.

---

## 1. Repo / package layout

### Single uv project, not standalone scripts

`uv init --script` is right for `download_htm.py` — one dependency, one job, runs anywhere. It is wrong for everything downstream: parse/load/embed/search/CLI/MCP share the Pydantic models, the entity-decoding helpers, and the DB connection setup. Duplicating those across PEP-723 headers would be a maintenance disaster, and the MCP server needs a stable installable entry point (`uvx --from . matienzo-mcp` or `uv run matienzo-mcp`) that a script header can't give you cleanly.

**Recommendation: one uv project at the repo root, `requires-python = ">=3.13"`, package `matienzo/`, with optional-dependency extras so the parse path doesn't drag in ONNX.** Leave `download_htm.py` exactly as it is — a working PEP-723 script — and add a thin `matienzo fetch` subcommand that shells out to it via `uv run`, or (better, later) move its body into `matienzo/fetch.py` and keep a 5-line script shim. Don't rewrite it now; it works.

```
/Users/andrew/Code/matienzo/
├── pyproject.toml            # uv project, [project.scripts] matienzo, matienzo-mcp
├── uv.lock                   # committed
├── .python-version           # 3.13  (see §8 risk on onnxruntime)
├── README.md
├── download_htm.py           # unchanged PEP-723 script
├── data/                     # source of truth, committed
│   ├── pages/                # 5288 .htm — COMMITTED (see §8)
│   ├── vocab/
│   │   ├── areas.toml        # canonical area -> variants/mojibake
│   │   ├── authors.toml      # "Corrin J S" -> "Corrin, Juan"
│   │   ├── qualifiers.toml   # closed citation-qualifier vocabulary
│   │   ├── systems.toml      # canonical cave-system names
│   │   └── people.toml       # allow/deny list for person extraction
│   └── overrides/
│       └── 0249.toml         # per-site human/LLM corrections (§5)
├── matienzo/
│   ├── __init__.py
│   ├── cli.py                # cyclopts app
│   ├── config.py             # paths, PARSER_VERSION, SCHEMA_VERSION
│   ├── models.py             # ALL Pydantic models (single module — see below)
│   ├── decode.py             # bytes -> NFC str; entity handling; mojibake repair
│   ├── htmlutil.py           # tolerant anchor scan, tag-strip, text flatten
│   ├── anomaly.py            # AnomalyCode enum + recorder
│   ├── parse/
│   │   ├── __init__.py       # parse_document(path|bytes) -> ParsedSite
│   │   ├── segment.py        # split into title/header/updated/body/footer/modal
│   │   ├── title.py          # number, name, type, nested-paren aliases
│   │   ├── header.py         # area, coords, measurements (label-scan, not positional)
│   │   ├── updated.py        # semicolon groups -> dates
│   │   ├── body.py           # blocks, sections, tables, xrefs, dates, people
│   │   ├── footer.py         # resource fields + links
│   │   └── citations.py      # split, author/year/qualifier, attachments
│   ├── normalise/
│   │   ├── areas.py  authors.py  people.py  systems.py  measures.py  geo.py
│   ├── db/
│   │   ├── schema.sql        # full DDL, versioned
│   │   ├── connect.py        # pragmas, extension loading
│   │   ├── load.py           # ParsedSite -> rows (idempotent upsert)
│   │   └── queries.py        # named read queries used by CLI + MCP
│   ├── chunk.py              # ParsedSite -> list[Chunk]
│   ├── embed.py              # fastembed wrapper, batched, cached by content hash
│   ├── search.py             # fts / vector / hybrid RRF — the ONE search API
│   └── mcp_server.py         # FastMCP, thin wrapper over search.py + queries.py
└── tests/
    ├── conftest.py
    ├── fixtures/             # ~40 nominated .htm copies (§6)
    ├── golden/               # NNNN.json snapshots of ParsedSite
    ├── test_decode.py  test_segment.py  test_title.py  test_header.py
    ├── test_citations.py  test_footer.py  test_body.py
    ├── test_golden.py        # fixture -> golden JSON
    └── test_corpus.py        # slow-marked, whole-corpus invariants (§6)
```

### Module boundary rules

- **`parse/` never touches the database and never touches the network.** `parse_document(bytes) -> ParsedSite` is a pure function of bytes + `PARSER_VERSION` + the vocab files. This is what makes golden tests and re-parse idempotency work.
- **`normalise/` is the only place that consults `data/vocab/`.** Parsers record what the page *says*; normalisers map it to canonical entities. Both values are kept in the DB (`raw_*` and `*_id` columns). Never let a normaliser destroy the raw string.
- **`search.py` is the single entry point for both CLI and MCP.** The MCP server must contain no query logic — it's an adapter. Otherwise CLI and Claude Code drift apart.
- **`models.py` is one module, not a package.** ~25 models, all mutually referential; splitting them buys nothing and costs circular imports.

### Dependency set (deliberately small)

| Purpose | Choice | Why this one |
|---|---|---|
| HTML | **lxml** | libxml2's `recover=True` survives `0048.htm`'s unterminated attribute, `1111.htm`'s missing `</BODY>`, `1561.htm`'s duplicate closers. stdlib `html.parser` chokes or silently truncates; `html5lib` is spec-correct but ~20× slower and *normalises away* the malformedness you want to detect; `selectolax` is fastest but its recovery is weaker and its API can't give you source offsets. At 5,288 files lxml's speed is irrelevant — recovery quality is everything. |
| Validation | **pydantic** (v2) | user convention |
| CLI | **cyclopts** | Native `X \| None` / `dict` / `list` type-hint handling with no `Annotated[..., typer.Option()]` ceremony, which matters given the modern-type-hints convention. Typer is the fallback if you want the larger ecosystem. |
| Terminal output | **rich** | tables for `matienzo show`, progress for load/embed |
| Vectors | **sqlite-vec** | §4 |
| Embeddings | **fastembed** | ONNX Runtime only — no PyTorch (2.5 GB → ~60 MB). §4/§8. |
| UTM → lat/lon | **utm** (pure Python) | ~200 lines, zero binary deps. `pyproj` drags in the PROJ C library and a 30 MB data bundle to do one zone-30T conversion where ETRS89↔WGS84 differ by well under a metre. Not worth it. |
| MCP | **mcp** (official SDK, FastMCP) | §4 |
| Tests | **pytest** + committed JSON goldens | Prefer plain committed JSON over `syrupy`/`pytest-golden`: the diffs land in `git diff` where you'll actually read them. |

Deliberately **not** used: BeautifulSoup (a slow wrapper over a parser you'd pick anyway), `ftfy` (a 2-file mojibake problem — a 20-line guarded round-trip repair with a strict check is better than a dependency), `dateutil` (its fuzzy parsing will happily turn `2nd Febrary 2013` into garbage; you need a bespoke grammar anyway), SQLAlchemy (the schema is fixed and hand-tuned; an ORM adds indirection over ~30 tables you write once), pandas.

Extras: `[embed]` = fastembed, sqlite-vec; `[mcp]` = mcp; `[dev]` = pytest, ruff, ty/mypy. Core install (parse + load + FTS search) has four dependencies.

---

## 2. Pydantic model set

Design rules that make the model set work:

1. **Every model that comes from text carries the exact source text it came from.** `raw: str` alongside every structured field group. Extraction is lossy by nature; carrying `raw` makes it recoverable and makes the review queue reviewable.
2. **Never model "sometimes a number, sometimes prose" as `float | str`.** That pushes the ambiguity onto every consumer. Model it as a *record with an interpretation kind*, always populated, sometimes with a numeric slot filled.
3. **Anomalies are data, not exceptions.** A parse never raises for corpus weirdness; it appends an `Anomaly` and continues with a degraded value.

```python
# matienzo/models.py  (sketch — signatures and field types, not full code)

type SiteNumber = int  # 1..5557; always from the filename


class Anomaly(BaseModel):
    code: AnomalyCode  # StrEnum, closed vocabulary (see §5)
    severity: Severity  # StrEnum: info | warn | error
    field_path: str | None  # "header.measurements[1].length", "footer.citations[7]"
    detail: str
    excerpt: str | None  # <=200 chars of offending source
    char_offset: int | None  # into the decoded document


class Provenance(BaseModel):
    """Per-file parse record. One per document, always present."""

    site_number: SiteNumber
    source_path: str
    content_sha256: str  # of the raw bytes on disk
    byte_size: int
    parser_version: str
    parsed_at: datetime
    encoding_used: Literal["utf-8", "cp1252"]
    had_mojibake_repair: bool
    entity_names_seen: list[str]
    segments_found: SegmentFlags  # which of title/header/updated/body/footer resolved
    anomalies: list[Anomaly]
    confidence: dict[str, float]  # section name -> 0..1 (see §5)


# ---------- quantities ----------


class Quantity(BaseModel):
    """A measurement slot. `raw` is always set; the rest may not be."""

    label: str  # normalised: "length"|"depth"|"altitude"|"vertical_range"|"height"|<other>
    label_raw: str  # "Length", "Alt.", "Vertical Range"
    raw: str  # entity-decoded value text, verbatim
    kind: QuantityKind  # StrEnum below
    value_m: float | None = None  # single value, metres, signed
    min_m: float | None = None  # range low  (vertical range, "5 & 5m")
    max_m: float | None = None  # range high
    modifier: Modifier = Modifier.EXACT  # exact | circa | at_least | uncertain | relative
    unit_stated: bool = True  # False for unitless "11"
    parts: list[float] = []  # compound "5 & 5m" -> [5.0, 5.0]
    prose: ProseQuantity | None = None  # populated iff kind is PROSE


class QuantityKind(StrEnum):
    NUMERIC = "numeric"  # 94m, 58.6m, -43m, c100m, 290m+, ?m-with-no-number -> UNKNOWN
    RANGE = "range"  # "from 232m (Top Tip) to 130m (downstream soakaway)"
    COMPOUND = "compound"  # "5 & 5m"
    PROSE = "prose"  # "included in the length of 0246"
    UNKNOWN = "unknown"  # "?m", "m", empty-but-label-present
    ABSENT = "absent"  # label not present at all (not stored as a row)


class ProseQuantity(BaseModel):
    """Structured reading of a non-numeric Length/Depth. Nothing is discarded."""

    relation: ProseRelation  # included_in | see_other | added_to | part_of_system | traverse_of | unparsed
    target_sites: list[
        SiteNumber
    ] = []  # from links AND plain text ("included in #0028", "see 1538")
    target_names: list[str] = []  # "Apilicueta", "Cueva de la Espada"
    system_name: str | None = None  # "South Vega System", "Four Valleys System"
    extra_value_m: float | None = None  # "(870m added to Risco)" -> 870.0


# ---------- title ----------


class Alias(BaseModel):
    text: str
    kind: AliasKind  # spanish_alt | french_ref | entrance_qualifier | bare_number | system_name | unknown
    site_number: SiteNumber | None = None  # for bare_number aliases


class Title(BaseModel):
    raw: str
    site_number: SiteNumber  # cross-checked against filename; mismatch -> anomaly
    name: str  # "Burro, Sima del"  (as written, inverted form preserved)
    name_sort: str  # de-inverted + unaccented: "sima del burro"
    site_type: str | None  # "shaft" | "cave" | "dig" | ... from the name tail
    aliases: list[Alias] = []  # depth-aware paren scan, not `\(([^)]*)\)`
    separator: Literal["colon", "space"]


# ---------- header ----------


class Coordinate(BaseModel):
    raw: str
    label: str | None  # "Top entrance", "Bottom entrance #5451"
    entrance_site_number: SiteNumber | None
    system: CoordSystem  # utm | vn_grid | placeholder | absent
    zone: str | None = "30T"
    easting: int | None
    northing: int | None
    vn_ref: str | None  # "VN12345678"
    datum: str | None  # "ETRS89", "Eur79"
    accuracy_code: Literal["G", "M", "A", "U", "P"] | None
    altitude_m: float | None  # altitude belongs to the entrance, not the site
    latitude: float | None  # derived
    longitude: float | None
    is_placeholder: bool = False  # "30T 04- 47-", "30T ??", "VN????????"


class Header(BaseModel):
    raw: str
    area_raw: str | None
    area_canonical: str | None  # resolved via data/vocab/areas.toml
    coordinates: list[Coordinate] = []
    quantities: list[Quantity] = []
    duplicate_labels: list[str] = []  # ~8 files
    field_order: list[str]  # for corpus-wide ordering analysis (25 distinct)


# ---------- updated ----------


class UpdateDate(BaseModel):
    raw: str  # "9th November" (before month/year back-fill)
    group_index: int  # semicolon group
    date: date | None
    precision: DatePrecision  # day | month | year | range
    end_date: date | None  # "13th-15th November 2019"
    attribution: str | None  # "(Simon Cornhill)"
    inferred_month: bool  # back-filled from a later date in the group
    inferred_year: bool


# ---------- body ----------


class Block(BaseModel):
    index: int
    kind: BlockKind  # paragraph | section_heading | editorial_note | table | list | rule | keyvalue
    text: str  # plain text, entities resolved, hard-wraps unwrapped
    html: str  # source fragment, for re-render / debugging
    section_index: int | None
    char_start: int
    char_end: int


class Section(BaseModel):
    index: int
    heading: str
    heading_kind: Literal["bold", "underline", "font", "hr"]
    canonical: str | None  # "hydrology" | "exploration_history" | "bat_information" | ...
    block_range: tuple[int, int]


class BodyTable(BaseModel):
    index: int
    caption: str | None
    headers: list[str]
    rows: list[list[str]]
    looks_like: Literal["survey_batches", "unknown"]


class BatObservation(BaseModel):  # 13 files, consistent key/value block
    date: date | None
    date_raw: str | None
    fields: dict[str, str]  # "Evidence of occupation" -> "feeding remains; droppings"


class CrossRef(BaseModel):
    target_site: SiteNumber
    kind: Literal["hyperlink", "plaintext", "gallery", "alias", "prose_quantity", "leaflet"]
    href: str | None
    fragment: str | None
    link_text: str | None
    context: str  # ~120 chars around the reference
    char_offset: int
    from_zone: Literal["header", "body", "footer"]


class DateMention(BaseModel):
    raw: str
    kind: Literal["year", "month_year", "season_year", "dmy", "full", "survey_batch"]
    year: int | None
    month: int | None
    day: int | None
    season: Season | None  # closed StrEnum, case-normalised
    batch_code: str | None  # "1930-08-01"
    char_offset: int


class PersonMention(BaseModel):
    name_raw: str
    name_canonical: str | None
    evidence: Literal["by_pattern", "attribution", "roster_match"]  # never bare bigram
    confidence: float
    char_offset: int


class Body(BaseModel):
    raw_html: str
    text: str  # canonical plain text (what gets embedded/indexed)
    char_count: int
    is_minimal: bool  # < 70 chars — a legitimate class, not a failure
    blocks: list[Block]
    sections: list[Section]
    tables: list[BodyTable]
    editorial_notes: list[str]  # <FONT COLOR="#ff0000"> content — metadata, NOT description
    bat_observations: list[BatObservation]
    cross_refs: list[CrossRef]
    dates: list[DateMention]
    people: list[PersonMention]


# ---------- footer ----------


class Citation(BaseModel):
    raw: str
    author_raw: str | None
    authors: list[str]  # split multi-author strings; canonicalised separately
    year: int | None
    disambiguator: str | None  # "a".."f"
    qualifier: str | None  # "Easter logbook" — validated against closed vocab
    qualifier_is_known: bool
    is_provenance_token: bool  # "material in file", "card", "pers comm", "SEAD website", "none"
    kind: CitationKind  # publication | logbook | survey | provenance | unparsed
    links: list[ResourceLink]  # 1..n; a citation split across two anchors stays ONE citation
    attachments: list[ResourceLink]  # nested "(survey)" / "(survey and photo)" links


class ResourceLink(BaseModel):
    href: str
    text: str
    bucket: LinkBucket  # logbook_pdf | scanned_pub | entpic | ugpic | survey_jpg |
    # survey_3d | survey_pdf | survey_gif | video | youtube |
    # cantab | rose_diagram | site_page | other
    target_site: SiteNumber | None  # entpics/ugpics/sibling refs
    is_range: bool = False  # "1930-2011e.htm", "NNNN-NNNNe.htm"
    unquoted_in_source: bool = False


class FooterField(BaseModel):
    label_raw: str  # "Entrance pictures :" as found
    label: str  # normalised slug: "entrance_pictures"
    is_known_label: bool  # against the stable ~10 + long-tail list
    order: int
    text: str  # '' if &nbsp;-only
    is_empty: bool
    links: list[ResourceLink]


class Footer(BaseModel):
    raw: str
    reference_label: Literal["Reference", "References"]
    citations: list[Citation]
    fields: list[FooterField]


# ---------- root ----------


class ParsedSite(BaseModel):
    site_number: SiteNumber
    title: Title | None
    header: Header | None
    updated: list[UpdateDate]
    body: Body
    footer: Footer | None
    provenance: Provenance
```

### On the Length/Depth modelling decision specifically

This is the one place where a naive schema loses real data. The corpus shows (I confirmed: 74 files with alphabetic Length values, 15 of the form `included in `, plus `see`, `of 800m included in`, `c40m`, `?m`, bare `m`, and the trailing-`?` variants like `5m ?`):

- `<B>Length</B> 94m` → `kind=NUMERIC, value_m=94.0, modifier=EXACT`
- `<B>Length</B> c100m` → `kind=NUMERIC, value_m=100.0, modifier=CIRCA`
- `<B>Length</B> 290m+` → `kind=NUMERIC, value_m=290.0, modifier=AT_LEAST`
- `<B>Length</B> 5m ?` / `?m` → `kind=UNKNOWN` (or NUMERIC+UNCERTAIN when a number is present)
- `<B>Length</B> 5 &amp; 5m` → `kind=COMPOUND, parts=[5,5], value_m=10.0`
- `<B>Vertical Range</B> from 232m (Top Tip) to 130m (…)` → `kind=RANGE, min_m=130, max_m=232, value_m=102`
- `<B>Length</B> included in the Four Valleys System: see <a href="0107.htm">Cueva Hoyuca</a>` → `kind=PROSE`, `prose.relation=part_of_system`, `system_name="Four Valleys System"`, `target_sites=[107]`, `target_names=["Cueva Hoyuca"]`

That last case is the payoff: it becomes a `system_member` row **and** an `xref` row **and** keeps the raw string. Aggregate queries then work honestly — `sum(value_m)` over `kind='numeric'` gives a defensible total length, and a separate query tells you how many sites are length-suppressed because they're system members.

---

## 3. SQLite schema

### Normalisation policy (the justification)

Three tiers, applied consistently:

1. **Relational columns** for anything you will filter, join, sort, or aggregate on: site numbers, area ids, coordinates, quantity values, years, link buckets, author ids. These get indices.
2. **A `raw` TEXT column beside every parsed group.** Non-negotiable. It costs ~15 MB total and it is the difference between "the DB is the archive" and "the DB is a lossy summary".
3. **A `extra JSON` column** on the messy leaf tables (`quantity.extra`, `footer_field.extra`, `citation.extra`) for parser detail that has no query use today — field ordering quirks, entity names seen, tag-case oddities. JSON here, not tables, because these are read-only-for-debugging bags with no stable key set. **Do not** put citations, links, coordinates, or cross-refs in JSON: those are the whole point of having a relational DB, and you *will* want `WHERE author = ? AND year BETWEEN ?`.

The body is the interesting judgement call. **Store the body three ways**: `site.body_text` (canonical plain text, one column — this is what humans read and what most queries want), `block` rows (paragraph-level, for citation-precise retrieval and section attribution), and `site.body_html` (source fragment, for fidelity). Three copies of ~15 MB of text is nothing; re-deriving paragraphs at query time is a permanent tax.

All tables are `STRICT` (real type enforcement, SQLite ≥3.37 — you have 3.50+). FTS5/vec0 virtual tables cannot be STRICT.

### DDL sketch

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============ provenance / build bookkeeping ============
CREATE TABLE build (                    -- one row per full load run
  build_id       INTEGER PRIMARY KEY,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  parser_version TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  file_count     INTEGER,
  git_sha        TEXT
) STRICT;

CREATE TABLE source_file (
  site_number    INTEGER PRIMARY KEY,
  path           TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  byte_size      INTEGER NOT NULL,
  mtime          TEXT NOT NULL,
  first_seen_build INTEGER REFERENCES build(build_id),
  last_changed_build INTEGER REFERENCES build(build_id)
) STRICT;
CREATE INDEX ix_source_file_sha ON source_file(content_sha256);

CREATE TABLE parse_run (
  site_number    INTEGER PRIMARY KEY REFERENCES site(site_number) ON DELETE CASCADE,
  build_id       INTEGER NOT NULL REFERENCES build(build_id),
  parser_version TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  encoding_used  TEXT NOT NULL CHECK (encoding_used IN ('utf-8','cp1252')),
  mojibake_repaired INTEGER NOT NULL DEFAULT 0,
  segments_found TEXT NOT NULL,          -- JSON flags
  confidence     TEXT NOT NULL,          -- JSON {section: float}
  anomaly_count  INTEGER NOT NULL DEFAULT 0,
  max_severity   TEXT
) STRICT;

-- ============ core ============
CREATE TABLE area (
  area_id      INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,     -- canonical, e.g. 'South Vega'
  slug         TEXT NOT NULL UNIQUE
) STRICT;

CREATE TABLE area_variant (              -- 'S Vega','EL Naso','Enaso','Coteron las Llanas', mojibake
  variant      TEXT PRIMARY KEY,
  area_id      INTEGER NOT NULL REFERENCES area(area_id),
  source       TEXT NOT NULL CHECK (source IN ('corpus','manual'))
) STRICT;

CREATE TABLE site (
  site_number  INTEGER PRIMARY KEY CHECK (site_number BETWEEN 1 AND 9999),
  name         TEXT,                     -- as written: 'Burro, Sima del'
  name_sort    TEXT,                     -- 'sima del burro', unaccented, de-inverted
  site_type    TEXT,                     -- shaft|cave|dig|...
  title_raw    TEXT NOT NULL,
  area_id      INTEGER REFERENCES area(area_id),
  area_raw     TEXT,
  header_raw   TEXT,
  body_text    TEXT NOT NULL DEFAULT '',
  body_html    TEXT NOT NULL DEFAULT '',
  body_chars   INTEGER NOT NULL DEFAULT 0,
  is_minimal   INTEGER NOT NULL DEFAULT 0,   -- body < 70 chars
  is_stub      INTEGER NOT NULL DEFAULT 0,   -- e.g. 0249 'To be re-allocated'
  updated_last TEXT,                         -- max resolved date, for sorting
  footer_raw   TEXT,
  -- denormalised convenience, all derived, all nullable:
  length_m     REAL, depth_m REAL, altitude_m REAL,
  latitude     REAL, longitude REAL,
  primary_easting INTEGER, primary_northing INTEGER
) STRICT;
CREATE INDEX ix_site_area ON site(area_id);
CREATE INDEX ix_site_type ON site(site_type);
CREATE INDEX ix_site_len  ON site(length_m) WHERE length_m IS NOT NULL;
CREATE INDEX ix_site_depth ON site(depth_m) WHERE depth_m IS NOT NULL;
CREATE INDEX ix_site_latlon ON site(latitude, longitude);

CREATE TABLE site_alias (
  alias_id     INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  text         TEXT NOT NULL,
  text_sort    TEXT NOT NULL,
  kind         TEXT NOT NULL,            -- spanish_alt|french_ref|entrance_qualifier|bare_number|system_name|unknown
  target_site  INTEGER,                  -- for bare_number aliases
  ordinal      INTEGER NOT NULL
) STRICT;
CREATE INDEX ix_alias_site ON site_alias(site_number);
CREATE INDEX ix_alias_sort ON site_alias(text_sort);

CREATE TABLE coordinate (
  coord_id     INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  label        TEXT,                     -- 'Top entrance'
  entrance_site INTEGER,                 -- 'Bottom entrance #5451' -> 5451
  coord_system TEXT NOT NULL CHECK (coord_system IN ('utm','vn_grid','placeholder')),
  zone         TEXT, easting INTEGER, northing INTEGER,
  vn_ref       TEXT,
  datum        TEXT, accuracy_code TEXT CHECK (accuracy_code IN ('G','M','A','U','P')),
  altitude_m   REAL,
  latitude     REAL, longitude REAL,
  is_placeholder INTEGER NOT NULL DEFAULT 0,
  raw          TEXT NOT NULL,
  UNIQUE (site_number, ordinal)
) STRICT;
CREATE INDEX ix_coord_en ON coordinate(easting, northing);

-- proximity search: 'what's within 500 m of here'
CREATE VIRTUAL TABLE coordinate_rtree USING rtree(
  coord_id, min_e, max_e, min_n, max_n
);

CREATE TABLE quantity (
  quantity_id  INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  label        TEXT NOT NULL,            -- length|depth|altitude|vertical_range|height|<slug>
  label_raw    TEXT NOT NULL,
  ordinal      INTEGER NOT NULL,         -- duplicate labels keep both rows
  kind         TEXT NOT NULL CHECK (kind IN ('numeric','range','compound','prose','unknown')),
  modifier     TEXT NOT NULL DEFAULT 'exact'
               CHECK (modifier IN ('exact','circa','at_least','uncertain','relative')),
  value_m      REAL, min_m REAL, max_m REAL,
  unit_stated  INTEGER NOT NULL DEFAULT 1,
  raw          TEXT NOT NULL,
  extra        TEXT                      -- JSON: parts[], notes
) STRICT;
CREATE INDEX ix_qty_site ON quantity(site_number, label);
CREATE INDEX ix_qty_label_value ON quantity(label, value_m) WHERE value_m IS NOT NULL;

-- ============ systems (derived from prose Length/Depth) ============
CREATE TABLE system (
  system_id    INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,     -- 'Four Valleys System'
  slug         TEXT NOT NULL UNIQUE,
  primary_site INTEGER REFERENCES site(site_number)   -- the site carrying the real length
) STRICT;

CREATE TABLE system_variant (
  variant TEXT PRIMARY KEY,              -- '4 Valleys System','Cubija System (North Vega System)'
  system_id INTEGER NOT NULL REFERENCES system(system_id)
) STRICT;

CREATE TABLE system_member (
  system_id    INTEGER NOT NULL REFERENCES system(system_id),
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  relation     TEXT NOT NULL,            -- included_in|part_of|traverse_of|added_to|see_other
  via_quantity INTEGER REFERENCES quantity(quantity_id),
  target_site  INTEGER,                  -- e.g. 'included in the length of 0246'
  evidence     TEXT NOT NULL,            -- the raw prose
  confidence   REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (system_id, site_number, relation)
) STRICT;
CREATE INDEX ix_sysmem_site ON system_member(site_number);

-- ============ updated dates ============
CREATE TABLE site_update (
  update_id    INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  group_index  INTEGER NOT NULL,
  ordinal      INTEGER NOT NULL,
  date         TEXT,                     -- ISO; NULL when unresolvable ('May')
  end_date     TEXT,
  precision    TEXT NOT NULL CHECK (precision IN ('day','month','year','range','unknown')),
  inferred_month INTEGER NOT NULL DEFAULT 0,
  inferred_year  INTEGER NOT NULL DEFAULT 0,
  attribution  TEXT,
  raw          TEXT NOT NULL
) STRICT;
CREATE INDEX ix_update_site_date ON site_update(site_number, date);
CREATE INDEX ix_update_date ON site_update(date);

-- ============ body structure ============
CREATE TABLE section (
  section_id   INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  heading      TEXT NOT NULL,
  heading_kind TEXT NOT NULL,            -- bold|underline|font|hr
  canonical    TEXT                      -- hydrology|exploration_history|bat_information|...
) STRICT;

CREATE TABLE block (
  block_id     INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  section_id   INTEGER REFERENCES section(section_id),
  kind         TEXT NOT NULL,            -- paragraph|section_heading|editorial_note|table|list|rule|keyvalue
  text         TEXT NOT NULL,
  char_start   INTEGER NOT NULL,
  char_end     INTEGER NOT NULL,
  UNIQUE (site_number, ordinal)
) STRICT;
CREATE INDEX ix_block_section ON block(section_id);

CREATE TABLE body_table (
  table_id     INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  caption      TEXT,
  looks_like   TEXT NOT NULL DEFAULT 'unknown',
  headers      TEXT NOT NULL             -- JSON list[str]
) STRICT;

CREATE TABLE body_table_cell (           -- long form: survives ragged rows
  table_id     INTEGER NOT NULL REFERENCES body_table(table_id) ON DELETE CASCADE,
  row_index    INTEGER NOT NULL,
  col_index    INTEGER NOT NULL,
  header       TEXT,
  value        TEXT NOT NULL,
  PRIMARY KEY (table_id, row_index, col_index)
) STRICT;

CREATE TABLE bat_observation (
  obs_id       INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  date         TEXT, date_raw TEXT,
  fields       TEXT NOT NULL             -- JSON dict; key set is not closed
) STRICT;

CREATE TABLE date_mention (
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  year INTEGER, month INTEGER, day INTEGER,
  season       TEXT, batch_code TEXT,
  char_offset  INTEGER NOT NULL,
  raw          TEXT NOT NULL,
  PRIMARY KEY (site_number, ordinal)
) STRICT;
CREATE INDEX ix_datemention_year ON date_mention(year);
CREATE INDEX ix_datemention_batch ON date_mention(batch_code) WHERE batch_code IS NOT NULL;

CREATE TABLE person (
  person_id    INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,     -- canonical 'Simon Cornhill'
  kind         TEXT NOT NULL DEFAULT 'person'
) STRICT;

CREATE TABLE person_mention (
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  person_id    INTEGER REFERENCES person(person_id),
  name_raw     TEXT NOT NULL,
  evidence     TEXT NOT NULL,            -- by_pattern|attribution|roster_match
  confidence   REAL NOT NULL,
  char_offset  INTEGER NOT NULL,
  PRIMARY KEY (site_number, char_offset)
) STRICT;

-- ============ footer resources & citations ============
CREATE TABLE footer_field (
  field_id     INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  label        TEXT NOT NULL,            -- slug
  label_raw    TEXT NOT NULL,
  is_known     INTEGER NOT NULL,
  text         TEXT NOT NULL DEFAULT '',
  is_empty     INTEGER NOT NULL,
  extra        TEXT                      -- JSON: tag-case oddity, colon-inside-bold flag
) STRICT;
CREATE INDEX ix_footer_label ON footer_field(label, site_number);

CREATE TABLE author (
  author_id    INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,     -- canonical: 'Corrin, Juan'
  is_anon      INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE author_variant (
  variant      TEXT PRIMARY KEY,         -- 'Corrin J','Corrin J S','Corrin Juan'
  author_id    INTEGER NOT NULL REFERENCES author(author_id)
) STRICT;

CREATE TABLE citation (                  -- de-duplicated: ~710 distinct units
  citation_id  INTEGER PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,     -- 'anon.|2005b|Easter & summer'
  author_raw   TEXT, year INTEGER, disambiguator TEXT,
  qualifier    TEXT, qualifier_known INTEGER NOT NULL DEFAULT 0,
  kind         TEXT NOT NULL,            -- publication|logbook|survey|provenance|unparsed
  raw          TEXT NOT NULL
) STRICT;
CREATE INDEX ix_citation_year ON citation(year);
CREATE INDEX ix_citation_qual ON citation(qualifier);

CREATE TABLE citation_author (
  citation_id  INTEGER NOT NULL REFERENCES citation(citation_id) ON DELETE CASCADE,
  author_id    INTEGER NOT NULL REFERENCES author(author_id),
  ordinal      INTEGER NOT NULL,
  PRIMARY KEY (citation_id, author_id)
) STRICT;

CREATE TABLE site_citation (
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  citation_id  INTEGER NOT NULL REFERENCES citation(citation_id),
  ordinal      INTEGER NOT NULL,
  raw          TEXT NOT NULL,            -- per-site raw (may differ in whitespace/entities)
  PRIMARY KEY (site_number, ordinal)
) STRICT;
CREATE INDEX ix_sitecite_cite ON site_citation(citation_id);

CREATE TABLE resource_link (
  link_id      INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  zone         TEXT NOT NULL CHECK (zone IN ('header','body','footer')),
  field_id     INTEGER REFERENCES footer_field(field_id),
  citation_id  INTEGER REFERENCES citation(citation_id),
  role         TEXT NOT NULL,            -- citation|attachment|resource|inline
  href         TEXT NOT NULL,
  href_resolved TEXT NOT NULL,           -- site-root-relative
  text         TEXT NOT NULL,
  bucket       TEXT NOT NULL,
  target_site  INTEGER,
  is_range     INTEGER NOT NULL DEFAULT 0,
  unquoted     INTEGER NOT NULL DEFAULT 0,
  ordinal      INTEGER NOT NULL
) STRICT;
CREATE INDEX ix_link_site ON resource_link(site_number, bucket);
CREATE INDEX ix_link_bucket ON resource_link(bucket);
CREATE INDEX ix_link_target ON resource_link(target_site) WHERE target_site IS NOT NULL;

-- ============ the site link graph ============
CREATE TABLE xref (
  xref_id      INTEGER PRIMARY KEY,
  from_site    INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  to_site      INTEGER NOT NULL,
  kind         TEXT NOT NULL,            -- hyperlink|plaintext|gallery|alias|prose_quantity|leaflet
  zone         TEXT NOT NULL,
  href         TEXT, fragment TEXT, link_text TEXT,
  context      TEXT NOT NULL,
  char_offset  INTEGER NOT NULL,
  to_site_exists INTEGER NOT NULL DEFAULT 0,   -- 269 numbers have no page
  CHECK (from_site <> to_site)
) STRICT;
CREATE INDEX ix_xref_from ON xref(from_site);
CREATE INDEX ix_xref_to   ON xref(to_site);
CREATE UNIQUE INDEX ux_xref ON xref(from_site, to_site, kind, char_offset);
```

Note `CHECK (from_site <> to_site)` — a hard structural guard against the self-reference trap (forgetting to strip `<title>` and the `<BIG><B>` heading before scanning for `site NNNN`). If the parser regresses, the load fails loudly rather than silently inflating the graph by 5,288 edges.

### Anomalies and overrides

```sql
CREATE TABLE anomaly (
  anomaly_id   INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  code         TEXT NOT NULL,
  severity     TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
  field_path   TEXT,
  detail       TEXT NOT NULL,
  excerpt      TEXT,
  char_offset  INTEGER,
  content_sha256 TEXT NOT NULL,          -- of the file when the anomaly was raised
  parser_version TEXT NOT NULL,
  build_id     INTEGER NOT NULL REFERENCES build(build_id)
) STRICT;
CREATE INDEX ix_anomaly_code ON anomaly(code, severity);
CREATE INDEX ix_anomaly_site ON anomaly(site_number);

CREATE TABLE override (                  -- loaded from data/overrides/*.toml, never written by the parser
  site_number  INTEGER NOT NULL,
  field_path   TEXT NOT NULL,            -- 'header.coordinates[0].easting'
  value_json   TEXT NOT NULL,
  op           TEXT NOT NULL DEFAULT 'set' CHECK (op IN ('set','append','delete')),
  rationale    TEXT NOT NULL,
  author       TEXT NOT NULL,            -- 'andrew' | 'llm:claude-opus-5'
  created_at   TEXT NOT NULL,
  applies_to_sha256 TEXT NOT NULL,       -- file hash the human/LLM actually looked at
  status       TEXT NOT NULL DEFAULT 'active'
               CHECK (status IN ('active','stale','superseded')),
  PRIMARY KEY (site_number, field_path)
) STRICT;

CREATE TABLE review_item (               -- the queue: a VIEW would do, but a table lets you triage
  site_number  INTEGER PRIMARY KEY REFERENCES site(site_number) ON DELETE CASCADE,
  score        REAL NOT NULL,            -- ranking: severity-weighted
  reason_codes TEXT NOT NULL,            -- JSON list
  state        TEXT NOT NULL DEFAULT 'open'
               CHECK (state IN ('open','in_llm','resolved','wontfix','stale')),
  notes        TEXT
) STRICT;
```

### Convenience views

```sql
CREATE VIEW v_site_full AS SELECT s.*, a.name AS area_name,
  (SELECT count(*) FROM xref WHERE from_site = s.site_number) AS outbound,
  (SELECT count(*) FROM xref WHERE to_site  = s.site_number) AS inbound
  FROM site s LEFT JOIN area a USING (area_id);

CREATE VIEW v_length_stats AS SELECT
  count(*) FILTER (WHERE kind='numeric') AS numeric_n,
  count(*) FILTER (WHERE kind='prose')   AS prose_n,
  sum(value_m) FILTER (WHERE kind='numeric' AND modifier IN ('exact','circa')) AS total_m
  FROM quantity WHERE label='length';
```

---

## 4. Search layer

### FTS5 and vectors in one file

Both live in `matienzo.db`. FTS5 is compiled in. `sqlite-vec` is a loadable extension: `import sqlite_vec; conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)`. **I verified `enable_load_extension` works on both the Homebrew 3.14 and the uv-managed CPythons on this machine**, which removes the single biggest historical objection to sqlite-vec on macOS (system `/usr/bin/python3` has it compiled *out* — confirmed above — so never run this project on the system Python).

**Recommendation: sqlite-vec, not a numpy/faiss sidecar.**

- It keeps the "one file" requirement literally true — you can `scp matienzo.db` and everything works.
- `sqlite-vec` ships prebuilt wheels per platform; the loadable `.dylib` is C with no Python ABI coupling, so a Python 3.13→3.14 bump can't break it. faiss-cpu, by contrast, ships compiled Python extensions and is routinely months behind on new CPython minors — exactly the friction to avoid.
- Corpus scale makes ANN indexing pointless: expect **~22–28k chunks**. Brute-force cosine over 25k × 384 float32 (38 MB) is single-digit milliseconds in sqlite-vec's SIMD kernel. faiss's advantage starts around 10⁶ vectors.
- A numpy sidecar means a second file, a second consistency problem, and hand-rolled ID mapping. Rejected.

Fallback if sqlite-vec ever fails to install: store vectors as `BLOB` in a plain STRICT table and brute-force in numpy. Keep `search.py`'s interface identical so this is a 40-line swap. Worth designing for; not worth building now.

### Embedding model

**`BAAI/bge-small-en-v1.5` via fastembed** — 384 dims, 33 M params, ~130 MB ONNX, CPU-fast on Apple Silicon, and consistently strong on retrieval benchmarks for its size. fastembed over sentence-transformers because sentence-transformers pulls PyTorch (~2.5 GB, slow imports, its own CPython-version wheel lag); fastembed is onnxruntime + tokenizers.

Prompt convention: bge wants `"Represent this sentence for searching relevant passages: "` prefixed on *queries only*, not documents. Store the model id and the prefix convention in a `vec_meta` table so a model change is detectable.

Consider `intfloat/multilingual-e5-small` (also 384-d, also in fastembed) if Spanish-language querying of cave names matters — the corpus prose is English but names are Spanish (`Riaño`, `Cueva Hoyuca`, `Cubío`). My recommendation: start with bge-small (better English retrieval, and names are handled by the trigram/FTS path anyway), and make the model swappable by config since re-embedding the whole corpus takes ~3 minutes.

### Chunking strategy

The corpus is pathologically bimodal: ~20% of bodies are one sentence, a handful are 70 KB with 15 sections. One chunk size cannot serve both. Use **structure-aware greedy packing over `block` rows**, with these rules:

1. **Never split a block.** Blocks are paragraphs; they are already the natural retrieval unit here.
2. **Target 900 characters, hard max 1,600.** Pack consecutive blocks from the same section until the target is hit.
3. **A body under the target becomes exactly one chunk** — so `"Small shelter."` is one chunk, not padded, not merged with a neighbour's text. This matters: 1,000+ single-sentence sites would otherwise become embedding noise.
4. **Section boundaries are hard chunk boundaries.** In `1930.htm`, `Wessex Inlet` must never bleed into `Passage of Vom`.
5. **One-block overlap** between consecutive chunks *within* a section only (not across sections). Cheap, and it fixes the "the answer straddles a paragraph break" failure.
6. **A single paragraph over 1,600 chars** splits at sentence boundaries with 150-char overlap. Rare in this corpus.
7. **Synthetic "card" chunk per site, always chunk index 0**: a rendered summary — `0001: Burro, Sima del. Riva. shaft. Length 94m, Depth 50m, Altitude 365m. Aliases: … Updated 2003, 2009, 2021.` This is what makes metadata questions ("shafts in Riva over 50 m deep") retrievable by vector search at all, and it gives minimal-record sites something meaningful to match on.
8. **Footer citations are not embedded.** They're 8,448 near-identical strings that would dominate similarity space. They are fully queryable relationally and via a dedicated FTS table.
9. Body tables get one chunk per table, serialised as `header: value` lines.

```sql
CREATE TABLE chunk (
  chunk_id     INTEGER PRIMARY KEY,
  site_number  INTEGER NOT NULL REFERENCES site(site_number) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  kind         TEXT NOT NULL,            -- card|prose|table|bat|editorial
  section_id   INTEGER REFERENCES section(section_id),
  section_heading TEXT,
  block_first  INTEGER, block_last INTEGER,
  char_start   INTEGER, char_end INTEGER,
  text         TEXT NOT NULL,            -- what gets embedded, WITH the header line prefixed
  n_chars      INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,          -- of `text`: the embedding cache key
  UNIQUE (site_number, ordinal)
) STRICT;
CREATE INDEX ix_chunk_site ON chunk(site_number);
CREATE INDEX ix_chunk_sha  ON chunk(content_sha256);
```

**Chunk text prefix.** Every chunk's embedded text starts with a one-line context header: `"Site 1930 Cobadal, Sumidero de (Cobadal) — Wessex Inlet\n\n"`. Without it, a chunk deep in a long cave description is an anonymous paragraph about mud and boulders and matches nothing useful. This is the highest-leverage single decision in the retrieval design.

**Chunk metadata for filtering** lives in the relational columns above plus a lightweight partitioning column set on the vec table (sqlite-vec supports auxiliary/partition columns; if you'd rather not depend on that, filter by joining `chunk` — at 25k rows the join is free):

```sql
CREATE VIRTUAL TABLE chunk_vec USING vec0(
  chunk_id INTEGER PRIMARY KEY,
  embedding FLOAT[384],
  +site_number INTEGER,       -- auxiliary, for cheap post-filtering
  +kind TEXT
);

CREATE VIRTUAL TABLE chunk_fts USING fts5(
  text,
  content='chunk', content_rowid='chunk_id',
  tokenize="unicode61 remove_diacritics 2"
);
-- + the three standard triggers to keep it in sync

CREATE VIRTUAL TABLE site_fts USING fts5(
  name, aliases, area, body_text, footer_text,
  content='', tokenize="unicode61 remove_diacritics 2"
);   -- doc-level: "which sites mention optical brightener"

CREATE VIRTUAL TABLE name_fts USING fts5(
  name, tokenize="trigram"
);   -- fuzzy substring name lookup: 'hoyuca', 'riano' -> Riaño
```

`remove_diacritics 2` is essential: it makes `riano` find `Riaño` and `fernandez` find `Fernández` — the single most common query failure you'd otherwise hit. The trigram table handles typos and partial names that the unicode61 tokenizer misses. Do **not** use the porter stemmer on `name_fts` (it mangles Spanish); porter on `chunk_fts`/`site_fts` is optional — I'd skip it, since BM25 over English cave prose does fine and stemming interferes with proper nouns.

### Hybrid fusion

**Reciprocal Rank Fusion, k=60.** Correct choice here because BM25 scores and cosine distances are on incomparable scales and their distributions differ wildly across the bimodal document lengths; RRF needs no normalisation, no tuning, and no per-query calibration. Score-normalised linear blending would need per-query min/max normalisation and would still be fragile on one-sentence documents.

```
score(d) = Σ_r  w_r / (k + rank_r(d))
```

with three retrievers: `chunk_fts` BM25 (w=1.0), `chunk_vec` KNN (w=1.0), `name_fts` trigram (w=0.5, only when the query looks like a name — short, capitalised, or no stopwords). Fetch top-100 from each, fuse, return top-k.

Two refinements worth building in from the start:

- **Filters are pushed down, not applied post-fusion.** `search(q, area="Riaño", min_depth=50)` must constrain each retriever's candidate set (a `WHERE chunk_id IN (SELECT …)` for FTS, and sqlite-vec's metadata filter or an oversampled KNN + join for vectors) — otherwise a narrow filter over a top-100 candidate set returns nothing.
- **Two result granularities.** `search_chunks` returns passages (for RAG-style answering); `search_sites` aggregates chunk scores per site (max + 0.3×sum-of-rest) for "find me the site" queries. The MCP server exposes both, because Claude Code wants different things at different moments.

### MCP surface

Keep it small and typed (FastMCP, functions with modern hints, Pydantic return models):

- `search_sites(query, area?, min_length_m?, max_depth_m?, has_survey?, limit)` → ranked site cards
- `search_passages(query, site?, section?, limit)` → chunks with `site_number`, `section_heading`, char span
- `get_site(site_number, include: list[str])` → the full record, sections selectable
- `nearby_sites(site_number | easting/northing, radius_m)` → R*Tree query
- `site_graph(site_number, direction, depth)` → xref neighbourhood
- `list_citations(site_number)` / `find_by_citation(author?, year?, qualifier?)`
- `sql(query)` — **read-only**, on a connection opened with `file:matienzo.db?mode=ro`, statement-timeout via `set_progress_handler`, results truncated. Worth it: the schema is rich, and letting Claude Code write its own aggregate query beats guessing which twelve tools to pre-build. Guard it (reject anything but a single `SELECT`/`WITH`).

---

## 5. Review queue / anomaly workflow

### Confidence

Not a single number. Per-section, in `parse_run.confidence` JSON, computed by deduction from a starting 1.0:

| Section | Deductions |
|---|---|
| title | no `<BIG><B>` (−0.3); space separator (−0.05); number ≠ filename (−0.6); unbalanced parens (−0.2); name is `-` (−0.4) |
| header | no `<SMALL>` (−0.5); no area (−0.3); no coords (−0.15, expected for ~42 files); placeholder coords (−0.1); duplicate labels (−0.1); unrecognised label (−0.1 each) |
| updated | none present (0.0 deduction — half the corpus lacks it, that's normal); group with unresolvable dates (−0.15) |
| body | end boundary regex didn't match (−0.6); body < 20 chars (−0.2); unbalanced tags after recovery (−0.1); `data-speak` wrappers present (−0.1) |
| footer | `Reference` absent (−0.7 — it's present in 100% of non-stub files, so its absence is a strong signal); unknown label (−0.05 each); citation not matching the author/year pattern (−0.05 each, capped) |

### What counts as an anomaly

A closed `AnomalyCode` StrEnum — closed, because an open string vocabulary makes the queue un-triageable within a week. Roughly 40 codes in six families:

- **Structural**: `NO_BODY_TAG`, `NO_HEADER_SMALL`, `FOOTER_BOUNDARY_NOT_FOUND`, `SECOND_BIG_TAG`, `TTS_WRAPPER_PRESENT`, `UNCLOSED_TAG_RECOVERED`, `UNTERMINATED_ATTRIBUTE`, `DUPLICATE_CLOSE_TAG`, `STRAY_SMALL_IN_HEADER`
- **Encoding**: `CP1252_FALLBACK`, `MOJIBAKE_REPAIRED`, `MOJIBAKE_SUSPECTED_UNREPAIRED`, `UNKNOWN_ENTITY`, `CONTROL_CHAR`
- **Title**: `TITLE_TAG_ORDER_SWAPPED`, `TITLE_NUMBER_MISMATCH`, `TITLE_SPACE_SEPARATOR`, `UNBALANCED_PARENS`, `ALIAS_KIND_UNKNOWN`
- **Header**: `AREA_MISSING`, `AREA_UNKNOWN_VARIANT`, `COORD_PLACEHOLDER`, `COORD_LEGACY_VN`, `COORD_OUT_OF_RANGE`, `COORD_COUNT_GT_1`, `MEASURE_PROSE`, `MEASURE_UNPARSED`, `MEASURE_DUPLICATE_LABEL`, `LABEL_UNKNOWN`
- **Footer/citations**: `CITATION_UNPARSED`, `CITATION_QUALIFIER_UNKNOWN`, `CITATION_SPLIT_ANCHORS`, `AUTHOR_UNKNOWN_VARIANT`, `FOOTER_LABEL_UNKNOWN`, `LINK_UNQUOTED`, `LINK_BUCKET_UNKNOWN`
- **Graph/derived**: `XREF_TARGET_MISSING`, `SYSTEM_NAME_UNKNOWN`, `SELF_REFERENCE_DETECTED`

Severity assignment matters more than the codes: `info` = known-and-handled corpus texture (`MEASURE_PROSE`, `COORD_PLACEHOLDER`, `CP1252_FALLBACK`) — these are *recorded* but never enter the queue. `warn` = a value was degraded. `error` = a whole section was lost.

`review_item.score = 3·errors + 1·warns + 0.5·(1 − min(confidence))`, seeded only where `max_severity >= 'warn'`. Expect roughly 150–400 files in the queue after a competent first parser — a tractable afternoon's work with LLM assistance, which is exactly the design target.

### Resolution and idempotency

**Overrides are files, not database writes.** `data/overrides/1930.toml`:

```toml
site = 1930
applies_to_sha256 = "3f2a…"
author = "llm:claude-opus-5"
reviewed_by = "andrew"
created_at = "2026-08-09"

[[fix]]
field_path = "header.coordinates[1].label"
op = "set"
value = "Bottom entrance"
rationale = "parser included the '#5451' site reference in the label text"
```

The load pipeline is: `parse(bytes) → ParsedSite → apply_overrides(ParsedSite, overrides) → rows`. Overrides are applied to the *Pydantic object*, before the DB, so they're re-validated by the same model constraints as parser output, and they survive `rm matienzo.db && matienzo build` unchanged. This is the whole answer to "how do fixes not get clobbered on re-parse": **the DB is never the place a fix lives.**

The staleness rule handles the other direction. Each override records `applies_to_sha256`. On load:

- File hash matches → apply, status `active`.
- File hash differs → **do not apply**; mark `stale`; re-open the review item with reason `SOURCE_CHANGED_UNDER_OVERRIDE`. The upstream site edited that page; a human must look again. Silently re-applying an override to changed source is how you get corrupt data that nobody notices for two years.
- The parser now produces the same value the override specifies → mark `superseded`, emit a `matienzo review prune` suggestion. This keeps override files from accumulating forever as the parser improves — an under-appreciated failure mode.

CLI: `matienzo review list --code CITATION_UNPARSED`, `matienzo review show 1930` (renders source excerpt + parsed record side by side), `matienzo review export --code X --format jsonl` (feeds a batch LLM pass; the LLM's job is to emit override TOML, never to write the DB), `matienzo review import <file>`, `matienzo review prune`. Every LLM-authored override is tagged `author = "llm:…"` and I'd keep a `reviewed_by` field so you can later ask "which of my data is unreviewed machine output?" — a question you will want to answer.

---

## 6. Testing strategy

### Golden fixtures — nominate these

Copy into `tests/fixtures/`, with a committed `tests/golden/NNNN.json` (the `ParsedSite` model dump, `indent=2`, sorted keys, so `git diff` is readable). `pytest --update-goldens` regenerates; the diff is the review.

**Canonical**: `0001` (the archetype — three citations incl. a nested `(survey)` attachment, empty footer fields, stray `</small>`).
**Structure extremes**: `0249` (the only true stub — no `<SMALL>`, no footer), `1930` (multi-entrance coords, vertical-range prose, 15 underline sections, gigantic footer with `Video`/rose-diagram/multi-link fields, `<a name="Refs">` before `<B>References`), `0107` (system membership + body table + red editorial font).
**Title**: `0505`, `1775` (`<B><BIG>` swap), `0007` (space separator), plus one of the 4-group nested-paren titles (`Comellantes` / `Torca (3424 (French: SCD))`).
**Encoding**: `0039`, `0672`, `1232`, `1551` (raw CP1252, incl. `\x91`/`\x92`), `0255`, `0788` (mojibake `RiaÃ±o` — and they're also the two mojibake *area* names, so they test the area normaliser too).
**Coordinates**: `0478` (leading-zero 7-digit easting), `2674`, `2675`, `2683` (`30T 04- 47-`), `2432` (`30T ??`), `0550` (zone, no numbers), `0067`, `0418`, `0419`, `0420`, `0892` (legacy `VN########`), one `VN????????` file.
**Measurements**: `0484` (58.6m decimal), `0713` (−43m — and it's Fuente Aguanaz, a hub node), `0246` + one `included in the length of` file, one `(870m added to Risco)`, one `5 &amp; 5m`, one `290m+`, one `c100m`, one `?m`.
**Citations**: `1930` (the `anon., 2005b (<a>Easter</a> &amp; <a>summer</a>)` split-anchor case *and* `Le&oacute;n Garc&iacute;a Jos&eacute;, 2010 (Volume 1 and Volume 2) (survey)`), one `Fern&aacute;ndez` entity-vs-separator case (`0001` covers it), one file with `material in file` / `card` / `pers comm` / `none`.
**Footer parsing**: `0001` (colon inside bold: `<B>Entrance picture </B>:`), a `<B>Reference</b>` case-mismatch file, `0841` (stray `</B>`).
**Malformed**: `1111` (no `</BODY>`), `1561` (dup close tags), `0048` (unterminated attribute + body table + red font — a triple-threat fixture), `0028`, `0841`, `2081`, `2366` (TTS `data-speak` wrappers, second `<BIG>`, `<script>`), one of the no-`<BODY>` files, one of the 104 stray-`<P>`-before-title files, one `target=_blank"` file.
**Body features**: `0012` (bat information key/value block), a `<HR>`-divided file, an 18-file mid-sentence-bold-date file (asserts it is *not* read as a heading — this is the trap most likely to silently corrupt section structure), a minimal-record file (`"Small shelter."`).
**Areas**: `0908`, `0914` (literal `?`), one `Coterón` vs `Coteron` pair, one `EL Naso`.

That's ~45 files: enough to cover every documented pathology, small enough to eyeball.

### Corpus-wide invariants

`tests/test_corpus.py`, marked `@pytest.mark.slow`, also exposed as `matienzo audit` so it runs outside pytest. These are assertions, not statistics — each one is a bug the moment it fails:

**Hard invariants (must always hold):**
1. Every file parses without raising. Zero uncaught exceptions.
2. `parsed.site_number == int(path.stem)` for all 5,288.
3. No `xref` where `from_site == to_site` (the self-reference trap).
4. Every non-stub file yields a footer with a `Reference`/`References` label.
5. `body.text` is never a superset of the footer text (boundary leakage — assert `"Accuracy code" not in body.text` and `body.text` doesn't contain the first citation's raw string).
6. Body never contains `</SMALL>` or `openModal` or `window.location.href`.
7. Decoded text contains no `&[a-z]+;` residue and no `\ufffd`.
8. Decoded text contains no mojibake signature (`Ã[\x80-\xbf]`, `â€`) after repair.
9. Every `resource_link.href` is non-empty and contains no `document.getElementsByTagName` (the phantom-href trap).
10. Round-trip: `ParsedSite.model_validate(json.loads(site.model_dump_json())) == site`.
11. Every coordinate with `easting IS NOT NULL` falls in zone-30T plausible bounds (E 400 000–520 000, N 4 750 000–4 830 000) — this catches the leading-zero-7-digit mis-parse instantly.
12. `sum(len(citations))` per file equals the semicolon-group count computed independently by a *different* (dumber) counting method, ± the known 234 split-anchor files.

**Threshold invariants (assert against the reconnaissance numbers, with tolerance — these are the regression detectors):**

| Assertion | Expected |
|---|---|
| files with a parsed area | ≥ 99.5% |
| files with ≥1 UTM coordinate | 98.0–99.0% |
| files with an altitude | ≥ 98.0% |
| files with a numeric length | 64–70% |
| distinct citation units | 700–720 |
| total citation instances | 8,300–8,600 |
| distinct areas after normalisation | 55–65 (**and zero unmapped area variants**) |
| xref edges (hyperlink) | 1,240–1,270 |
| xref edges (plaintext) | 140–160 |
| files with ≥1 outbound xref | 63–69% |
| files with `&nbsp;`-only footer field normalised to empty | 100% |
| anomalies with severity `error` | 0 (after the review queue is worked) |

Encode these as a single table in the test file so adding one is a one-line change. **Critically, `assert unmapped_area_variants == set()`** — that turns "new area spelling appeared in the corpus" from an invisible data-quality drift into a red test.

### Detecting regressions when the scrape adds files

`download_htm.py` already tracks known-404s. Add `matienzo audit --diff`:

1. Compare `source_file.content_sha256` against disk → produce three sets: **new**, **changed**, **vanished**.
2. For **new** files, run the parser and report anomaly codes; any `error`-severity code on a new file is a CI failure worth a look. Any *previously unseen* anomaly code across the whole corpus is likewise reported prominently — the corpus is legacy hand-written HTML and the maintainer keeps editing it, so new pathologies are guaranteed over time.
3. For **changed** files, diff the *parsed* record (not the HTML) and print a field-level diff, and mark any covering override `stale` (§5).
4. Re-run the threshold invariants with the new file count as denominator; report any metric that moved more than its tolerance.
5. Store each build's metric vector in a small `build_metric(build_id, name, value)` table so `matienzo audit --history` plots drift across builds. This is the cheapest possible regression detector and it catches the class of bug where a parser "improvement" silently drops 400 citations.

Also: keep the golden `ParsedSite` JSONs *and* a `tests/golden/corpus-digest.txt` — a sorted `site_number: sha256(parsed_json)` line per file, ~5,288 lines. Any parser change shows up as an exact count of affected files in the git diff before you even read it. That single artifact has caught more parser regressions in my experience than any number of unit tests.

---

## 7. Build order

Each phase ends with something you can look at and use. Do not proceed to the next phase before its checkpoint is genuinely satisfying — especially phase 2, where the temptation to rush to embeddings is strongest.

**Phase 0 — Project skeleton (half a day).**
`pyproject.toml` (uv, `>=3.13`), `matienzo/` package, `cli.py` with `matienzo --version`, ruff + pytest wired, `decode.py` complete with its own tests (utf-8 → cp1252 fallback → entity unescape → mojibake repair → NFC). Commit `data/pages/`.
*Checkpoint:* `matienzo decode 0039 | head` prints correct `Riaño`; `matienzo decode --audit` reports exactly 15 utf-8 files, 4 cp1252 files, 2 repaired mojibake files. **You now have provably correct text for the whole corpus** — everything downstream rests on this, so it is worth over-testing.

**Phase 1 — Segmentation (1 day).**
`segment.py` only: split every file into title / header / updated / body / footer / modal, with anomalies. No field parsing at all.
*Checkpoint:* `matienzo segment --audit` reports segment-presence counts across 5,288 files matching the reconnaissance (footer found in 100% of non-stubs, body boundary found everywhere but `0249`), and `matienzo segment 1930 --show body` prints exactly the description with no header/footer leakage. **This is the highest-risk step and it is fully verifiable on its own.**

**Phase 2 — Parsers + Pydantic models (3–4 days).**
title → header → footer → updated → body, in that order (each is independently gold-testable, and body is hardest so it benefits from the others' machinery). Full `ParsedSite`. `matienzo parse NNNN --json`. Golden fixtures and corpus invariants land here, not later.
*Checkpoint:* `matienzo parse --all --out /dev/null` runs clean over 5,288 files in under ~60 s, all goldens pass, all hard invariants pass, and the threshold table is green. `matienzo parse 1930 --json | jq` is a document you'd be happy to publish. **The parser is the project; if this phase is right, nothing after it is hard.**

**Phase 3 — Schema and load (2 days).**
`schema.sql`, `load.py`, override application, `matienzo build`. Normalisers (areas, authors, systems) with their vocab files — build the vocab files by running the parser over the corpus and eyeballing the variant lists, which is a genuinely pleasant hour's work.
*Checkpoint:* `matienzo build` produces `matienzo.db` (~60–80 MB) from a clean checkout in under two minutes, twice in a row, byte-identical modulo timestamps. `matienzo stats` prints a satisfying table (sites, areas, citations, links, xrefs, longest caves, deepest caves). You can now answer real questions in `sqlite3` — this is the first genuinely *useful* checkpoint and it's worth spending an evening just querying it.

**Phase 4 — Review queue (1–2 days).**
`anomaly.py` scoring, `review_item` population, `matienzo review` subcommands, the override TOML loader with staleness rules, one batch LLM pass over the residue.
*Checkpoint:* zero `error`-severity anomalies remain; the queue is empty or every open item has an explicit `wontfix` with a note; `data/overrides/` is committed; `matienzo build` still reproduces exactly.

**Phase 5 — FTS + CLI search (1 day).**
`chunk.py`, the three FTS tables, `matienzo search`, `matienzo show`, `matienzo nearby`, `matienzo graph`.
*Checkpoint:* keyword search works well enough that you'd use it instead of grep. Explicitly test `riano`→`Riaño` and `fernandez`→`Fernández`. **Ship-worthy on its own; the vector layer is optional from here.**

**Phase 6 — Embeddings + hybrid (1–2 days).**
fastembed, sqlite-vec, `matienzo embed` (content-hash cached so re-embedding only touches changed chunks), RRF in `search.py`, `matienzo search --hybrid`.
*Checkpoint:* a hand-written eval set of ~30 queries with known-correct answers ("which cave did the optical brightener test link to Fuente Aguanaz?", "shafts near Cobadal over 50 m deep", "caves with bat occupation evidence") — record recall@5 for FTS-only, vector-only, and hybrid. If hybrid doesn't beat both, the chunking is wrong, not the fusion.

**Phase 7 — MCP server (1 day).**
FastMCP wrapper, `.mcp.json` for the repo, read-only `sql` tool with guards.
*Checkpoint:* Claude Code in this repo answers a multi-hop question ("what connects Cobadal to Fuente Aguanaz, and what's the evidence?") using only MCP tools, with correct site numbers and a citation.

---

## 8. Risks and open questions

**Risks**

1. **onnxruntime wheel availability on your Python.** This machine's default is CPython **3.14.6**, and fastembed depends on onnxruntime, which is historically the slowest major package to ship wheels for a new CPython minor. Mitigation: pin `.python-version` to **3.13** for the project (still satisfies your `>=3.13` convention), or make `[embed]` a separate extra installed under its own interpreter. Verify with `uv add --dry-run fastembed` before phase 6 — I couldn't confirm current wheel coverage offline. This is the single most likely install-friction failure in the plan.
2. **Section detection in the ~29 long files is where subtle corruption will hide.** Bold is used both for headings and for mid-sentence date emphasis (18 files), and `1930.htm` actually uses `<U>` for its headings, not bold — so the heuristic must be *block-initial + block-terminal + one of {`<U>`, `<B>`, `<FONT>`} + short + no terminal punctuation*, and it must be validated by eye on all 29 files, not by aggregate statistics. Aggregate metrics will look fine while the structure is wrong.
3. **Person extraction will be the weakest table in the DB.** The reconnaissance already establishes bigram extraction is ~60% wrong. Recommendation: ship it as *evidence-gated only* (`by X` patterns, video/photo attributions, explicit surveyor lists in body tables), accept low recall, and seed `data/vocab/people.toml` from the survey-batch tables in `0107`/`1930` — those are ground-truth name lists. Never let unevidenced bigrams into `person`; a table you can trust at 40% recall beats one you can't trust at all.
4. **The `xref` graph will over-count `to_site_exists = 0`.** 269 numbers in range have no page. Those are still real references (to reallocated or unpublished sites). Keep them, flag them, don't delete them.
5. **Citation de-duplication by `key` may over-merge.** `anon., 2005b` with different qualifiers must be distinct rows; `anon., 2005b (Easter logbook)` appearing on 400 pages must be one row. Include the qualifier in the key (as sketched) and assert the distinct count lands in 700–720.
6. **`0249`-style stubs and the 269 missing numbers mean `site` is not dense.** Nothing should assume contiguous site numbers.
7. **The source site is live and edited.** Everything about `updated`, hashes, and staleness assumes re-scraping. Re-scrape before starting the review queue so you're not reviewing stale bytes.

**Open questions for you**

1. **Commit the corpus?** 23 MB, ~5,300 files, effectively immutable. I'd commit it: the DB is derived, the site could change or vanish, and reproducibility of the corpus is worth 23 MB. But it makes `git log --stat` noisy after a re-scrape. (Alternative: commit it once on a `corpus` branch/tag and gitignore thereafter — I'd rather not; it complicates the audit diff.)
2. **Scope beyond `descrip/`?** The footer links point at 5,521 logbook PDFs, 2,311 scanned publications, 1,830 entrance-picture pages, 1,178 underground-picture pages. The gallery pages (`entpics/`, `ugpics/`) are the same kind of legacy HTML and would give you photo captions — high-value, low-cost, and they'd make the "picture" footer fields actually resolvable. The PDFs are a much bigger project. Do you want `entpics`/`ugpics` in scope for v1's schema (I'd design `resource_link.target_site` to accommodate it now either way, which the DDL above does), or strictly `descrip/`?
3. **`.3d` Survex files (474 links).** Parsing those would give real 3-D geometry, passage lengths, and station names — a genuinely different tier of data and an excellent cross-check on the `length_m` column. Out of scope for v1, but if it's ever in scope the `coordinate`/`quantity` design should probably anticipate a `survey_station` table. Worth deciding now or explicitly deferring?
4. **Cave systems: derived-only, or curated?** The prose Length values give you membership for ~30 sites, but the *real* system rosters (Four Valleys, South Vega, Cubija) are larger and are documented elsewhere on the site. Are you content with "what the pages say", or do you want `data/vocab/systems.toml` to carry hand-curated rosters too? The schema supports both; it's an editorial call about what `matienzo.db` claims to be.
5. **Historical versions.** Should the DB model "as of build N" (current design: no, it's a snapshot with `build` bookkeeping only), or do you want the `updated` dates plus re-scrapes to eventually support "what did this page say in 2019"? Retrofitting temporality is expensive; saying no now is fine, but say it deliberately.
6. **Search-quality ground truth.** Phase 6's checkpoint needs ~30 queries with known answers. That has to come from you — you know the caves. Worth writing that list during phase 3 while you're querying the DB anyway.

---

### Critical Files for Implementation

- `/Users/andrew/Code/matienzo/matienzo/models.py` — the Pydantic hierarchy in §2; every other module depends on it, and the `Quantity`/`ProseQuantity` design is the decision that determines whether data is lost.
- `/Users/andrew/Code/matienzo/matienzo/parse/segment.py` — the boundary logic (`</small>` after first `<BIG>`; `<(SMALL|P)>\s*(?:<a\s+name=[^>]*>)?\s*<B>\s*Referen`); highest-risk, highest-leverage module.
- `/Users/andrew/Code/matienzo/matienzo/db/schema.sql` — the DDL in §3, including the `CHECK (from_site <> to_site)` guard and the `STRICT` tables.
- `/Users/andrew/Code/matienzo/matienzo/decode.py` — utf-8 → cp1252 → entity → mojibake-repair → NFC; correctness here is a precondition for everything.
- `/Users/andrew/Code/matienzo/matienzo/search.py` — the single hybrid-search API shared by `cli.py` and `mcp_server.py`; keeping this the only query surface prevents CLI/MCP drift.
- `/Users/andrew/Code/matienzo/download_htm.py` — existing, working, unchanged; its `known-missing.txt` and content-hash behaviour feed the `source_file` / audit-diff design in §6.
---

# Corrections: verified against the complete 5,557-file corpus

The reconnaissance above was performed **while the scrape was still running**, on
partial snapshots of 2,000–3,500 files. The following claims were re-measured
against the finished corpus and were wrong. Trust this section over the above.

| Claim in the reconnaissance | Verified fact |
| --- | --- |
| `0255` / `0788` contain mojibake (`RiaÃ±o`) | **No mojibake exists anywhere in the corpus.** Those files are clean UTF-8 (`Ria\xc3\xb1o`); the surveying agent read them as latin-1 and reported its own artefact. No repair pass is needed — only detection. |
| 15 UTF-8 files, 4 CP1252 files | **5,507 ASCII-only, 32 UTF-8, 18 CP1252.** |
| CP1252 is a sufficient fallback | CP1252 leaves 0x81, 0x8D, 0x8F, 0x90 and 0x9D **undefined** and raises on them. A latin-1 last resort is required so one stray byte cannot take down a build. |
| 269 site numbers 404 / ~5,288 files | **All 5,557 pages exist.** No 404s. |
| `<B><BIG>` tag swap in 2 files (`0505`, `1775`) | **3 files** — also `1955`. |
| Files with no `<BODY>` tag: 34–132 | **1,813** (33% of the corpus). Normal texture, not an anomaly — segmentation anchors on `<BIG>`. |
| Header ends at the first `</SMALL>` | Wrong on 9 pages. **The header ends at the `Area position` / `Site entrance in context` / `Logbook search` anchor run.** Eight pages split the info line across more than one `<SMALL>` block (`0061`, `0151`, `0417`, `0420`, `0917`, `1452`, `2410`, `4950` — `0420` also puts a literal `]` between its two blocks, and `0917` has three); `0363` leaves the anchors outside any `<SMALL>` entirely. |
| Body starts at the first `</small>` after `<BIG>` | Insufficient — see above. Also, the **footer must be located before the header**: `5528` has no info line, so its first `<SMALL>` *is* the footer. |
| One true stub (`0249`) | **Three pages are stub-like** — `0249` ("To be re-allocated"), `4540` ("reserved"), `5528` (no info line, empty body, footer only) — but `site.is_stub` counts **two**. The shipped definition is "no header *and* no footer", and `5528`'s only `<SMALL>` block is its footer, so it fails the second clause. Anyone counting stubs from the column gets 2, not 3. |
| Updated line present in ~49–54% | **2,615 of 5,557 (47.1%).** |
| Stray extra `</small>` in header: 19 files | **14 files.** |
| `data-speak` TTS wrappers in 4 files | **5 files** (`0028`, `0841`, `2081`, `2366`, `4732`), with 7–45 wrappers each **inside the body**. Those pages must be read via the DOM. |
| Largest page ~70 KB | **`2889.htm` is 162 KB** on disk; `0733` is 155 KB. The "134,983 visible body characters" first recorded here is a tag-strip of the whole document, not of the body segment: `site.body_chars` gives `2889` **104,081** and `0733` **106,957**, so `0733` has the largest parsed body even though `2889` is the larger file. |
| Minimal records (<70 chars): ~723 | **1,220**, both by `site.is_minimal` and by `body_chars < 70`. The 911 first recorded here was itself wrong. |

Counts still to be re-verified against the full corpus when Phase 2 lands:
citation totals, distinct areas, cross-reference edge counts, and the
measurement-value distributions. Treat every threshold in §6 as provisional
until then.

**Five counts have since been verified and frozen** in
`tests/test_db.py::TestFullCorpus`: 5,557 sites, 7,051 update dates, 752 distinct
citations used 14,661 times, and 2,378 cross-references. The area count of 81 is
measured but not asserted by that test.

**The §6 threshold table itself was never updated, and five of its twelve rows
are now known wrong.** Read it as reconnaissance, not as a specification:

| §6 threshold | Says | Actually |
| --- | --- | --- |
| distinct citation units | 700–720 | **752** |
| total citation instances | 8,300–8,600 | **14,661** |
| distinct areas after normalisation | 55–65 | **81** |
| hyperlink xref edges | 1,240–1,270 | **2,227** |
| files with ≥1 outbound xref | 63–69% | **25.1%** (1,396 sites) |

The remaining seven hold: parsed area 99.91%, UTM 98.58%, altitude 98.45%,
numeric length 64.24%, 151 plaintext xrefs, `&nbsp;` normalisation, and zero
`error`-severity anomalies. The stale 700–720 figure also survives at §5 and in
the `citation` DDL comment in §3.

---

# Where the build diverged from this design

All seven phases in §7 shipped. The following parts of the document above
describe a plan that the implementation deliberately did not follow; trust this
section over them. `docs/plan-status.md` carries the reasoning.

| Designed above | What was built |
| --- | --- |
| `requires-python = ">=3.13"`, `.python-version` pinned to 3.13 (§1, §7, risk 1) | **3.14.** The onnxruntime risk was probed in an isolated venv and discharged: `fastembed==0.8.0`, `onnxruntime==1.28.0` and `sqlite-vec==0.1.9` all resolve on 3.13 and 3.14, and the model runs on both. No fallback interpreter, no separate `[embed]` interpreter. |
| `data/vocab/` holds areas, authors, qualifiers, systems and people (§1) | **`areas.toml` only.** Authors and qualifiers come from the citation grammar, systems from the prose `Quantity` relations, people from evidence-gated mentions. A curated file for those would be a second source of truth for something the parser already derives. |
| `normalise/` holds areas, authors, people, systems, measures, geo (§1) | **`normalise/areas.py` only**, for the same reason. The §1 rule still holds where it applies: normalisers are the only consumers of `data/vocab/`, and the raw string is never destroyed. |
| `data/overrides/` is committed at the Phase 4 checkpoint (§5, §7) | **The directory does not exist.** No site needs an override; the two known-bad upstream records were better served as anomalies. `overrides.py` and its tests exist and pass, and `load_all` reads a missing directory as empty. |
| `matienzo.db` ~60–80 MB, built in under two minutes (§7) | **Not a divergence — the estimate was right.** 62.6 MB with vectors (41.6 MB without), built in ~4.3 s, plus about five minutes for a cold embed. Beware of reading the size mid-checkpoint: with a populated `-wal` the main file measures several MB short. |
| `parse/header.py` yields `target_names` for prose measurements (§2) | **Dropped.** The capitalised-word regex stopped at lowercase Spanish articles (`Cueva del Risco` → `Cueva`), nothing read the field, and `relation` / `target_sites` / `system_name` already carry the useful content. |
| Vector cache keyed by chunk content hash so a rebuild re-embeds only what changed (§4) | Correct in intent, wrong in location: the cache lived in `matienzo.db`, which `matienzo build` replaces wholesale. `build` now reads the previous file's vectors out before replacing it and restores the ones whose chunk text is unchanged. |

### The schema that shipped is not the schema in §3

The DDL sketch was followed in spirit and departed from in detail. Verified
against `matienzo/db/schema.sql` and `search_schema.sql`:

**Never built at all.** `coordinate_rtree`, `override`, `review_item`,
`body_table_cell`, `system_variant`, `author_variant`, `build_metric`, and the
`v_site_full` / `v_length_stats` views — replaced by the single `site_summary`
view. `site.body_html` was never added either, so §3's "**store the body three
ways**" is false: it is stored two ways.

The R*Tree omission propagates. §4 describes `nearby_sites` as an R*Tree query
and the document's opening line calls `sqlite3` shipping R*Tree "load-bearing
for the vector-search recommendation". Nothing in the package uses R*Tree.
`search.nearby` is a squared-distance scan over UTM eastings and northings,
which is exact because those are already metres, and at 5,557 rows is not worth
indexing.

**Renamed.** `system` → `cave_system`; `site_update` → `update_date` with `date`
→ `edited_on`; `citation.key` → `citation_key`; `site.primary_easting` /
`primary_northing` → `easting` / `northing`; `xref.zone` → `from_zone`.

**Dropped columns that §2 and §4 lean on.** `block.char_start` / `char_end`,
`date_mention.char_offset` and `ordinal`, `quantity.ordinal` (§3's stated
mechanism for keeping duplicate labels apart), `coordinate.is_placeholder`
(folded into `coord_system`), `xref.char_offset` and therefore the `ux_xref`
unique index, and `chunk.section_id` / `char_start` / `char_end` — which makes
§4's claim that the MCP passage payload carries a **char span** wrong. The
`list_citations` tool in §4 was never implemented; `corpus_stats` shipped in its
place.

### Further body figures that no correction covers

| Claim | Where | Actually |
| --- | --- | --- |
| "expect ~22–28k chunks"; "at 25k rows the join is free" | §4 | **13,280.** `search_schema.sql` says "~13k" in its own comment. |
| "8,448 near-identical strings" (footer citations) | §4 | **14,661** instances over 752 distinct works. |
| "the known 234 split-anchor files" | §6 | **384** `citation_split_anchors` anomalies. |
| "no coords (−0.15, expected for ~42 files)" | §5 | **72** `coord_missing` anomalies. |
| "5,521 logbook PDFs, 2,311 scanned publications" | §8 | **20,599** `logbook_pdf` links over 183 distinct URLs; **5,587** `scanned_pub` links over 156. Neither reading matches. |
| `~45` golden fixtures | §6 | **67.** |

### The §1 layout tree is out of date beyond the vocab and normalise rows

The corpus is at **`data/pages/`**, not `pages/`. It moved so that `data/` is
the single source-of-truth tree rather than one of two, which is what the
governing principle in §0 always described. The `-text` attribute moved with it,
and the pre-commit exclude names the new path.

`download_htm.py` is at **`scripts/download_htm.py`**, not the repo root — the
tree, the §1 recommendation to "leave `download_htm.py` exactly as it is", and
the Critical Files list at the end all still give the old path.

Shipped but missing from the tree: `matienzo/evaluate.py`, `matienzo/overrides.py`,
`matienzo/parse/links.py`, `matienzo/db/audit.py`, `matienzo/db/search_schema.sql`,
`data/eval/queries.toml`.

Listed but never created: `tests/test_citations.py`, `tests/test_corpus.py` (the
whole-corpus invariants live in `matienzo/db/audit.py` and `tests/test_db.py`),
and `tests/golden/corpus-digest.txt`.

### Open questions §8 still hasn't answered

Questions 1 (commit `pages/`) and 6 (search ground truth) were settled by
building — the corpus is committed byte-exactly with `-text`, and
`data/eval/queries.toml` holds 28 queries. The rest are live editorial calls,
deferred rather than decided:

2. **`entpics/` / `ugpics/` in scope?** `resource_link` accommodates them:
   6,186 of the 38,501 links point into those galleries, and nothing resolves
   them today.
3. **The `.3d` Survex links.** 824 of them, not the 474 the reconnaissance
   counted on a partial corpus. Would give real geometry and an independent
   cross-check on `length_m`.
4. **Curated system rosters.** The DB currently claims only what the pages say —
   9 systems from 25 memberships — by default rather than by decision.
5. **Historical versions.** Currently a snapshot with `build` bookkeeping only.
   Retrofitting temporality is expensive; the "no" has not been made deliberate.
