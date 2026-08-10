-- The portal's writable state: conversations, rate-limit buckets, the ledger.
--
-- Separate from matienzo.db on purpose. That database is a derived artefact
-- rebuilt from pages/ in seconds and opened read-only everywhere; putting
-- anything durable in it would make the rebuild destructive. Nothing here can
-- be reconstructed from the corpus, so nothing here belongs there.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS session (
  session_id   TEXT PRIMARY KEY,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  ip_hash      TEXT,               -- salted; the address itself is never stored
  title        TEXT                -- the opening question, truncated, for a thread list
) STRICT;

CREATE INDEX IF NOT EXISTS ix_session_seen ON session (last_seen_at);

CREATE TABLE IF NOT EXISTS turn (
  turn_id     INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES session (session_id) ON DELETE CASCADE,
  ordinal     INTEGER NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  created_at  TEXT NOT NULL,
  model       TEXT,                -- assistant turns only; gates thinking-block replay
  stop_reason TEXT,
  cost_micros INTEGER NOT NULL DEFAULT 0,
  UNIQUE (session_id, ordinal)
) STRICT;

-- One row per Anthropic content block, stored verbatim.
--
-- This is the one place a normalised schema would be wrong. The content-block
-- union is a wire format we have to reproduce byte-for-byte on replay, not a
-- data model we own: normalising it means re-deriving tool_use/tool_result
-- pairing on every read (wrong once, and every later turn is a 400), a
-- migration for every block type the API adds, and lossy round-tripping of
-- thinking blocks, which the API rejects if they come back modified.
--
-- `type` is duplicated out of the JSON so replay can filter without parsing
-- every row — the same reason the corpus schema keeps a `raw` column beside
-- each parsed group.
CREATE TABLE IF NOT EXISTS block (
  block_id INTEGER PRIMARY KEY,
  turn_id  INTEGER NOT NULL REFERENCES turn (turn_id) ON DELETE CASCADE,
  ordinal  INTEGER NOT NULL,
  type     TEXT NOT NULL,
  json     TEXT NOT NULL,
  UNIQUE (turn_id, ordinal)
) STRICT;

-- What the tools actually returned, so a replayed conversation renders its
-- links without re-running them.
CREATE TABLE IF NOT EXISTS source (
  source_id       INTEGER PRIMARY KEY,
  turn_id         INTEGER NOT NULL REFERENCES turn (turn_id) ON DELETE CASCADE,
  site_number     INTEGER NOT NULL,
  name            TEXT,
  area            TEXT,
  url             TEXT NOT NULL,
  excerpt         TEXT NOT NULL DEFAULT '',
  first_seen_tool TEXT NOT NULL,
  UNIQUE (turn_id, site_number)
) STRICT;

CREATE TABLE IF NOT EXISTS rate_bucket (
  ip_hash    TEXT PRIMARY KEY,
  tokens     REAL NOT NULL,
  updated_at REAL NOT NULL          -- unix seconds; refilled lazily on read
) STRICT;

-- Reserved and spent are tracked separately because usage is not known until a
-- turn finishes. Checking `spent < cap` and then streaming lets every
-- concurrent request pass the same check; reserving first is what makes the cap
-- hold. See budget.py.
CREATE TABLE IF NOT EXISTS budget_day (
  day             TEXT PRIMARY KEY,  -- YYYY-MM-DD, UTC
  reserved_micros INTEGER NOT NULL DEFAULT 0,
  spent_micros    INTEGER NOT NULL DEFAULT 0,
  request_count   INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS usage_event (
  event_id           INTEGER PRIMARY KEY,
  at                 TEXT NOT NULL,
  day                TEXT NOT NULL,
  session_id         TEXT,
  turn_id            INTEGER,
  model              TEXT NOT NULL,
  iteration          INTEGER NOT NULL,
  input_tokens       INTEGER NOT NULL,
  output_tokens      INTEGER NOT NULL,
  cache_read_tokens  INTEGER NOT NULL,
  cache_write_tokens INTEGER NOT NULL,
  cost_micros        INTEGER NOT NULL,
  stop_reason        TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS ix_usage_day ON usage_event (day);
