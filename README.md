# matienzo

Turns the ~5,557 hand-written HTML cave-description pages from
[matienzocaves.org.uk](https://www.matienzocaves.org.uk/descrip/) into a single
queryable SQLite database with full-text and semantic search.

## Layout

| Path | What it is |
| --- | --- |
| `pages/` | The scraped corpus. Source of truth, but **not committed** — see below. |
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

## Getting the corpus

`pages/` is gitignored, so a fresh clone has no corpus. Fetch it:

```bash
uv run download_htm.py
```

The upstream site is live and hand-edited, so a re-fetch may differ from the one
this code was written against. Every parsed record carries the SHA-256 of the
bytes it came from precisely so that drift is detectable rather than silent.

## Usage

```bash
uv sync
uv run matienzo decode --audit
```

```bash
uv run matienzo segment --audit
```

## Development

```bash
uv run pytest
uv run ruff check
```
