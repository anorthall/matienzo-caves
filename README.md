# matienzo

Turns the ~5,557 hand-written HTML cave-description pages from
[matienzocaves.org.uk](https://www.matienzocaves.org.uk/descrip/) into a single
queryable SQLite database with full-text and semantic search.

## Layout

| Path | What it is |
| --- | --- |
| `pages/` | The scraped corpus, 5,557 pages. Source of truth, committed byte-exactly. |
| `data/vocab/` | Hand-curated canonical names (areas, authors, systems). |
| `data/overrides/` | Per-site corrections applied to parser output. |
| `matienzo/` | The pipeline: decode → parse → normalise → load → chunk → embed → search. |
| `tests/fixtures/` | Frozen copies of the pages the tests assert on. Committed. |
| `docs/design.md` | Corpus reconnaissance, the design it produced, and corrections. |
| `docs/plan-status.md` | What is built, what is not, and how the plan has changed. |
| `matienzo.db` | Derived. Delete it any time; `matienzo build` rebuilds it. |

`pages/` and `data/` are the only sources of truth. The database is a disposable
artefact — which is why corrections live in `data/overrides/*.toml` rather than
as database writes, and so survive a rebuild.

## The corpus

`pages/` is committed, so a clone has everything needed to reproduce a build.
It is stored with `-text` in `.gitattributes` so git never normalises line
endings: 5,556 of the 5,557 pages use CRLF, every parsed record is keyed by the
SHA-256 of its source bytes, and override staleness is decided by comparing
those hashes. Bytes that change between clones would make that provenance lie.

To re-fetch from the live site:

```bash
uv run download_htm.py
```

The site is hand-edited and still maintained, so a re-fetch may differ from what
this code was written against. `matienzo audit --diff` reports which pages
changed and re-parses them, rather than letting the drift go unnoticed.

## Usage

```bash
uv sync
uv run matienzo decode --audit
```

```bash
uv run matienzo build
uv run matienzo stats
uv run matienzo audit --diff
```

Inspect a single page at any stage of the pipeline:

```bash
uv run matienzo parse 1930
```

## Development

```bash
uv run pytest
uv run ruff check
```
