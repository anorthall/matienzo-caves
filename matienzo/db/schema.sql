-- matienzo.db — derived from pages/ and data/. Safe to delete at any time;
-- `matienzo build` reconstructs it in a couple of minutes.
--
-- Normalisation policy, applied throughout:
--
--  1. Relational columns for anything filtered, joined, sorted or aggregated:
--     site numbers, area ids, coordinates, measurement values, years, link
--     buckets. These get indices.
--  2. A `raw` column beside every parsed group. Non-negotiable — it costs a few
--     megabytes and is the difference between an archive and a lossy summary.
--  3. JSON only on leaf columns nobody queries (`extra`). Never for citations,
--     links, coordinates or cross-references: those are the whole point of
--     having a relational store.
--
-- Every table is STRICT so a type error surfaces at write time rather than as a
-- baffling comparison result months later.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- bookkeeping

CREATE TABLE build (
  build_id        INTEGER PRIMARY KEY,
  started_at      TEXT    NOT NULL,
  finished_at     TEXT,
  parser_version  TEXT    NOT NULL,
  schema_version  INTEGER NOT NULL,
  page_count      INTEGER,
  git_sha         TEXT
) STRICT;

-- One row per source file, keyed by the hash of its bytes. This is what makes
-- `matienzo audit --diff` able to tell a parser regression from an upstream
-- edit: the site is live and hand-maintained, so the corpus does drift.
CREATE TABLE source_file (
  site_number     INTEGER PRIMARY KEY,
  path            TEXT    NOT NULL,
  content_sha256  TEXT    NOT NULL,
  byte_size       INTEGER NOT NULL,
  encoding_used   TEXT    NOT NULL
) STRICT;
CREATE INDEX ix_source_file_sha ON source_file (content_sha256);

CREATE TABLE parse_run (
  site_number     INTEGER PRIMARY KEY REFERENCES site (site_number) ON DELETE CASCADE,
  build_id        INTEGER NOT NULL REFERENCES build (build_id),
  parser_version  TEXT    NOT NULL,
  content_sha256  TEXT    NOT NULL,
  segments_found  TEXT    NOT NULL,   -- JSON
  confidence      TEXT    NOT NULL,   -- JSON {section: float}
  min_confidence  REAL    NOT NULL,
  anomaly_count   INTEGER NOT NULL DEFAULT 0,
  max_severity    TEXT
) STRICT;
CREATE INDEX ix_parse_run_confidence ON parse_run (min_confidence);

CREATE TABLE anomaly (
  anomaly_id      INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  code            TEXT    NOT NULL,
  severity        TEXT    NOT NULL,
  detail          TEXT    NOT NULL,
  field_path      TEXT,
  excerpt         TEXT,
  char_offset     INTEGER
) STRICT;
CREATE INDEX ix_anomaly_site ON anomaly (site_number);
CREATE INDEX ix_anomaly_code ON anomaly (code, severity);

-- ---------------------------------------------------------------------- sites

-- Canonical areas. The corpus spells them 89 different ways for roughly 60
-- places (`S Vega` / `South Vega`, `Coteron` / `Coterón`, `EL Naso`), so the
-- variants live in their own table and the raw string is kept on the site.
CREATE TABLE area (
  area_id         INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL UNIQUE,
  slug            TEXT    NOT NULL UNIQUE
) STRICT;

CREATE TABLE area_variant (
  variant         TEXT    PRIMARY KEY,
  area_id         INTEGER NOT NULL REFERENCES area (area_id),
  source          TEXT    NOT NULL CHECK (source IN ('corpus', 'manual'))
) STRICT;

-- Site numbers are NOT dense: three are reserved or reallocated placeholders,
-- and nothing may assume contiguity.
CREATE TABLE site (
  site_number     INTEGER PRIMARY KEY,
  name            TEXT,
  name_sort       TEXT,               -- de-inverted, unaccented: 'sima del burro'
  site_type       TEXT,               -- shaft | cave | dig | ...
  title_raw       TEXT    NOT NULL,
  area_id         INTEGER REFERENCES area (area_id),
  area_raw        TEXT,
  header_raw      TEXT,
  body_text       TEXT    NOT NULL DEFAULT '',
  body_chars      INTEGER NOT NULL DEFAULT 0,
  is_minimal      INTEGER NOT NULL DEFAULT 0,   -- one-line description
  is_stub         INTEGER NOT NULL DEFAULT 0,   -- reserved or reallocated number
  footer_raw      TEXT,
  updated_first   TEXT,
  updated_last    TEXT,
  update_count    INTEGER NOT NULL DEFAULT 0,
  -- Denormalised for convenience. All derived, all nullable; the authoritative
  -- values live in `quantity` and `coordinate`.
  length_m        REAL,
  depth_m         REAL,
  altitude_m      REAL,
  latitude        REAL,
  longitude       REAL,
  easting         INTEGER,
  northing        INTEGER
) STRICT;
CREATE INDEX ix_site_area   ON site (area_id);
CREATE INDEX ix_site_type   ON site (site_type);
CREATE INDEX ix_site_sort   ON site (name_sort);
CREATE INDEX ix_site_length ON site (length_m) WHERE length_m IS NOT NULL;
CREATE INDEX ix_site_depth  ON site (depth_m)  WHERE depth_m  IS NOT NULL;
CREATE INDEX ix_site_latlon ON site (latitude, longitude);

CREATE TABLE site_alias (
  alias_id        INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  text            TEXT    NOT NULL,
  text_sort       TEXT    NOT NULL,
  kind            TEXT    NOT NULL,
  target_site     INTEGER
) STRICT;
CREATE INDEX ix_alias_site ON site_alias (site_number);
CREATE INDEX ix_alias_sort ON site_alias (text_sort);

-- Altitude belongs to the entrance, not the site: multi-entrance pages give
-- each entrance its own coordinate and its own height.
CREATE TABLE coordinate (
  coord_id        INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  raw             TEXT    NOT NULL,
  label           TEXT,               -- 'Top entrance'
  entrance_site   INTEGER,            -- 'Bottom entrance #5451'
  coord_system    TEXT    NOT NULL CHECK (coord_system IN ('utm', 'vn_grid', 'placeholder')),
  zone            TEXT,
  easting         INTEGER,
  northing        INTEGER,
  vn_ref          TEXT,
  datum           TEXT,
  accuracy_code   TEXT,
  altitude_m      REAL,
  latitude        REAL,
  longitude       REAL
) STRICT;
CREATE INDEX ix_coordinate_site   ON coordinate (site_number);
CREATE INDEX ix_coordinate_latlon ON coordinate (latitude, longitude);

-- `kind` states how the row should be read. About 61 Length values are prose
-- rather than numbers, and that prose carries cave-system membership — see
-- `system_member`. Aggregate honestly by filtering on kind = 'numeric'.
CREATE TABLE quantity (
  quantity_id     INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  label           TEXT    NOT NULL,   -- length | depth | vertical_range | height
  label_raw       TEXT    NOT NULL,
  raw             TEXT    NOT NULL,
  kind            TEXT    NOT NULL,   -- numeric | range | compound | prose | unknown
  value_m         REAL,
  min_m           REAL,
  max_m           REAL,
  modifier        TEXT    NOT NULL DEFAULT 'exact',
  unit_stated     INTEGER NOT NULL DEFAULT 1,
  extra           TEXT                -- JSON: compound parts, prose detail
) STRICT;
CREATE INDEX ix_quantity_site  ON quantity (site_number);
CREATE INDEX ix_quantity_label ON quantity (label, kind);

CREATE TABLE update_date (
  update_id       INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  group_index     INTEGER NOT NULL,
  raw             TEXT    NOT NULL,
  edited_on       TEXT,               -- ISO date
  end_date        TEXT,
  precision       TEXT    NOT NULL,
  attribution     TEXT,
  inferred_month  INTEGER NOT NULL DEFAULT 0,
  inferred_year   INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX ix_update_site ON update_date (site_number);
CREATE INDEX ix_update_date ON update_date (edited_on);

-- ---------------------------------------------------------------- description

CREATE TABLE section (
  section_id      INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  heading         TEXT    NOT NULL,
  heading_kind    TEXT    NOT NULL,
  canonical       TEXT,
  block_first     INTEGER NOT NULL,
  block_last      INTEGER NOT NULL
) STRICT;
CREATE INDEX ix_section_site ON section (site_number);

CREATE TABLE block (
  block_id        INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  kind            TEXT    NOT NULL,
  text            TEXT    NOT NULL,
  section_ordinal INTEGER
) STRICT;
CREATE INDEX ix_block_site ON block (site_number, ordinal);

CREATE TABLE body_table (
  table_id        INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  looks_like      TEXT    NOT NULL,
  headers         TEXT    NOT NULL,   -- JSON list
  rows            TEXT    NOT NULL    -- JSON list of lists
) STRICT;
CREATE INDEX ix_body_table_site ON body_table (site_number);

CREATE TABLE bat_observation (
  observation_id  INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  observed_on     TEXT,
  date_raw        TEXT,
  fields          TEXT    NOT NULL    -- JSON key/value
) STRICT;
CREATE INDEX ix_bat_site ON bat_observation (site_number);

CREATE TABLE date_mention (
  mention_id      INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  raw             TEXT    NOT NULL,
  kind            TEXT    NOT NULL,
  year            INTEGER,
  month           INTEGER,
  day             INTEGER,
  season          TEXT,
  batch_code      TEXT
) STRICT;
CREATE INDEX ix_date_mention_site ON date_mention (site_number);
CREATE INDEX ix_date_mention_year ON date_mention (year);

-- Evidence-gated only. Never populated from bare capitalised bigrams, which are
-- about 60% place names on this corpus.
CREATE TABLE person (
  person_id       INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL UNIQUE,
  mention_count   INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE person_mention (
  mention_id      INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  person_id       INTEGER NOT NULL REFERENCES person (person_id),
  name_raw        TEXT    NOT NULL,
  evidence        TEXT    NOT NULL,
  confidence      REAL    NOT NULL DEFAULT 1.0
) STRICT;
CREATE INDEX ix_person_mention_site   ON person_mention (site_number);
CREATE INDEX ix_person_mention_person ON person_mention (person_id);

-- ------------------------------------------------------------------ the graph

-- `to_site_exists` records whether the target has a page. References to
-- unpublished or reallocated numbers are real references and are kept.
CREATE TABLE xref (
  xref_id         INTEGER PRIMARY KEY,
  from_site       INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  to_site         INTEGER NOT NULL,
  kind            TEXT    NOT NULL,
  href            TEXT,
  fragment        TEXT,
  link_text       TEXT,
  context         TEXT,
  from_zone       TEXT    NOT NULL,
  to_site_exists  INTEGER NOT NULL DEFAULT 0,
  CHECK (from_site <> to_site)
) STRICT;
CREATE INDEX ix_xref_from ON xref (from_site);
CREATE INDEX ix_xref_to   ON xref (to_site);

CREATE TABLE cave_system (
  system_id       INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL UNIQUE
) STRICT;

-- Derived from prose Length values: `included in the Four Valleys System: see
-- Cueva Hoyuca`. A nullable-float column would have discarded all of this.
CREATE TABLE system_member (
  system_id       INTEGER NOT NULL REFERENCES cave_system (system_id),
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  relation        TEXT    NOT NULL,
  evidence        TEXT    NOT NULL,
  PRIMARY KEY (system_id, site_number)
) STRICT;

-- --------------------------------------------------------------- bibliography

CREATE TABLE author (
  author_id       INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL UNIQUE,
  citation_count  INTEGER NOT NULL DEFAULT 0
) STRICT;

-- One row per distinct work. The qualifier is part of the key: `anon., 2005b
-- (Easter logbook)` and `anon., 2005b (summer logbook)` are different logbooks,
-- and merging on author-plus-year alone silently collapses them.
CREATE TABLE citation (
  citation_id     INTEGER PRIMARY KEY,
  citation_key    TEXT    NOT NULL UNIQUE,
  raw             TEXT    NOT NULL,
  author_raw      TEXT,
  year            INTEGER,
  disambiguator   TEXT,
  qualifier       TEXT,
  qualifier_known INTEGER NOT NULL DEFAULT 0,
  kind            TEXT    NOT NULL,
  site_count      INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX ix_citation_year ON citation (year);
CREATE INDEX ix_citation_kind ON citation (kind);

CREATE TABLE citation_author (
  citation_id     INTEGER NOT NULL REFERENCES citation (citation_id) ON DELETE CASCADE,
  author_id       INTEGER NOT NULL REFERENCES author (author_id),
  ordinal         INTEGER NOT NULL,
  PRIMARY KEY (citation_id, author_id)
) STRICT;

CREATE TABLE site_citation (
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  citation_id     INTEGER NOT NULL REFERENCES citation (citation_id),
  ordinal         INTEGER NOT NULL,
  PRIMARY KEY (site_number, citation_id, ordinal)
) STRICT;
CREATE INDEX ix_site_citation_citation ON site_citation (citation_id);

CREATE TABLE footer_field (
  field_id        INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  label           TEXT    NOT NULL,
  label_raw       TEXT    NOT NULL,
  is_known_label  INTEGER NOT NULL DEFAULT 1,
  text            TEXT    NOT NULL DEFAULT '',
  is_empty        INTEGER NOT NULL DEFAULT 1
) STRICT;
CREATE INDEX ix_footer_field_site ON footer_field (site_number, label);

-- Links from anywhere on the page: footer fields, citations and their
-- attachments. `target_site` is set for gallery and sibling links, which makes
-- them edges in the graph as well as resources.
CREATE TABLE resource_link (
  link_id         INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  href            TEXT    NOT NULL,
  text            TEXT    NOT NULL DEFAULT '',
  bucket          TEXT    NOT NULL,
  origin          TEXT    NOT NULL,   -- footer_field | citation | attachment
  field_id        INTEGER REFERENCES footer_field (field_id) ON DELETE CASCADE,
  citation_id     INTEGER REFERENCES citation (citation_id),
  target_site     INTEGER,
  is_range        INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX ix_resource_link_site   ON resource_link (site_number);
CREATE INDEX ix_resource_link_bucket ON resource_link (bucket);

-- ------------------------------------------------------------------- views

CREATE VIEW site_summary AS
SELECT
  s.site_number,
  s.name,
  s.site_type,
  a.name AS area,
  s.length_m,
  s.depth_m,
  s.altitude_m,
  s.latitude,
  s.longitude,
  s.body_chars,
  s.update_count,
  s.updated_last,
  (SELECT count(*) FROM site_citation c WHERE c.site_number = s.site_number) AS citations,
  (SELECT count(*) FROM xref x WHERE x.from_site = s.site_number) AS refs_out,
  (SELECT count(*) FROM xref x WHERE x.to_site = s.site_number) AS refs_in
FROM site s
LEFT JOIN area a ON a.area_id = s.area_id;
