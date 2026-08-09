-- Search tables. Kept separate from schema.sql because they are rebuilt
-- independently: re-chunking and re-indexing does not require re-parsing.
--
-- `remove_diacritics 2` on every text index is the single most useful setting
-- here. It is what lets `riano` find `Riaño` and `fernandez` find `Fernández`,
-- which is otherwise the most common way a search of this corpus fails.
--
-- No porter stemmer: it mangles Spanish proper nouns, and cave and place names
-- are most of what anyone searches for.

CREATE TABLE chunk (
  chunk_id        INTEGER PRIMARY KEY,
  site_number     INTEGER NOT NULL REFERENCES site (site_number) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  kind            TEXT    NOT NULL,   -- card | prose | table | editorial
  section_heading TEXT,
  block_first     INTEGER,
  block_last      INTEGER,
  text            TEXT    NOT NULL,
  n_chars         INTEGER NOT NULL,
  content_sha256  TEXT    NOT NULL,   -- embedding cache key
  UNIQUE (site_number, ordinal)
) STRICT;
CREATE INDEX ix_chunk_site ON chunk (site_number);
CREATE INDEX ix_chunk_sha  ON chunk (content_sha256);
CREATE INDEX ix_chunk_kind ON chunk (kind);

-- Passage-level index, delegating storage to `chunk`.
CREATE VIRTUAL TABLE chunk_fts USING fts5(
  text,
  content = 'chunk',
  content_rowid = 'chunk_id',
  tokenize = "unicode61 remove_diacritics 2"
);

CREATE TRIGGER chunk_fts_insert AFTER INSERT ON chunk BEGIN
  INSERT INTO chunk_fts (rowid, text) VALUES (new.chunk_id, new.text);
END;
CREATE TRIGGER chunk_fts_delete AFTER DELETE ON chunk BEGIN
  INSERT INTO chunk_fts (chunk_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
END;
CREATE TRIGGER chunk_fts_update AFTER UPDATE ON chunk BEGIN
  INSERT INTO chunk_fts (chunk_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
  INSERT INTO chunk_fts (rowid, text) VALUES (new.chunk_id, new.text);
END;

-- Document-level index, for "which sites mention optical brightener".
CREATE VIRTUAL TABLE site_fts USING fts5(
  site_number UNINDEXED,
  name, aliases, area, body_text, footer_text,
  tokenize = "unicode61 remove_diacritics 2"
);

-- Trigram index over names, for partial and misspelled lookups that the word
-- tokeniser cannot reach: `hoyuc`, `azpili`, `reñada`.
CREATE VIRTUAL TABLE name_fts USING fts5(
  site_number UNINDEXED,
  name,
  tokenize = "trigram"
);
