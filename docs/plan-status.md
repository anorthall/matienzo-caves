# Plan status

Tracks the approved plan against what is actually built. Update it when a phase
checkpoint is met or an assumption turns out to be wrong.

## Where things stand

| Phase | Estimate | State | Checkpoint |
| --- | --- | --- | --- |
| 0 · Skeleton + decode | ½ day | **Done** | `matienzo decode --audit` → 5,507 ASCII / 32 UTF-8 / 18 CP1252, 0 errors |
| 1 · Segmentation | 1 day | **Done** | `matienzo segment --audit` → title on 5,557; 3 pages lack a header, 2 of them stubs; 0 leaks |
| 2 · Parsers + models | 3–4 days | **Done** | `matienzo parse --audit` → 5,557 records, 0 errors |
| 3 · Schema + load | 2 days | **Done** | `matienzo build` → 62.6 MB in 4.3 s, reproducible; `matienzo stats` |
| 4 · Review queue | 1–2 days | **Done** | `matienzo review`; overrides; 0 error-severity anomalies |
| 5 · Chunking + FTS + CLI | 1 day | **Done** | `matienzo search / show / nearby / graph`; 13,280 chunks |
| 6 · Embeddings + hybrid | 1–2 days | **Done** | `matienzo embed` / `evaluate`; hybrid wins at recall@1 and @10, ties at @5 |
| 7 · MCP server | 1 day | **Done** | 8 tools over stdio; guarded read-only `sql` |

505 tests pass; `ruff check` and `mypy --strict` clean. **All seven phases
are complete.**

32 of those 505 skip themselves when `matienzo.db` is absent: 11 each in
`test_hybrid.py` and `test_mcp.py`, 6 in `test_db.py` (including
`TestFullCorpus`) and 4 in `test_search.py`. A clean checkout therefore reports
`473 passed, 32 skipped`, which is not a failure but is also not full coverage.
Run `matienzo build` (and `matienzo embed` for the hybrid cases) before reading
a green suite as complete.

Three phase checkpoints above were originally recorded with figures that no
longer hold, and have been corrected here: phase 2 recorded 1 error, which
became 0 when the `5255` anomaly was regraded to `warn` under amendment 4;
phase 3 recorded 25 MB, a figure predating chunking, FTS and the resource-link
table — the build is 62.6 MB today, or 41.6 MB of it before `matienzo embed`
adds the vectors; and phase 6 recorded hybrid beating both single strategies,
which overstates what the harness prints.

### What the database contains

5,557 sites · 81 areas · 5,502 coordinates · 6,774 measurements (62 of them
prose) · 7,051 update dates · 12,089 description blocks · 139 sections ·
752 distinct citations used 14,661 times · 163 authors · 2,378
cross-references · 9 cave systems with 25 memberships · 80 people from 283
evidence-gated mentions · 38,501 resource links.

Plus 13,280 search chunks, each with a 384-dimension embedding. Zero
error-severity anomalies.

## Verdict: the plan holds, with amendments

The architecture survived contact with the corpus. Three decisions in particular
earned their keep and should not be revisited:

- **Anomalies as data rather than exceptions.** Every surprise so far — an
  upstream typo, an unrecorded coordinate, a prose measurement — landed as a
  queryable row instead of a crash or a silent `None`.
- **Locating the footer before the header.** Non-obvious, and `5528` breaks any
  other ordering.
- **`Quantity` carrying an interpretation kind.** 61 prose lengths yielded 25
  `system_member` rows — 22 of them a plain `part_of_system`, plus 2
  `traverse_of` and 1 `see_other` — and dozens of cross-references that a
  `float | None` column would have discarded.

No new plan is needed. The following amendments are.

### 1 · The threshold invariants must be generated, not asserted

The plan's §6 table (distinct citations 700–720, hyperlink xrefs 1,240–1,270,
areas 55–65, …) came from reconnaissance on a **partial corpus while the scrape
was still running**. Every figure re-measured so far has moved — see the
corrections table at the end of `design.md`. Writing those numbers into tests as
given would bake in wrong expectations.

**Change:** derive the threshold table from the finished parser at the end of
Phase 2, review it by eye once, then freeze it. Treat every number still
unverified — citation totals, xref counts, area counts — as provisional.

### 2 · Split Phase 2 into three checkpoints

Title and header were the regular parts of the page. What remains is not.
Footer/citations carries the entity-versus-separator trap and the 234
split-anchor citations; the body carries section detection, which is the plan's
own risk #2 and the place where a silent corruption is most likely. Bundling
them into one checkpoint means the risky work has no independent gate.

| Sub-phase | Deliverable | Checkpoint |
| --- | --- | --- |
| 2a | `parse/footer.py`, `parse/citations.py` | citation count stable under two independent counting methods; no `Fern&aacute` fragments; the split-anchor pages yield one citation each (the plan said 234; the finished parser records 384 such anomalies over 359 pages) |
| 2b | `parse/updated.py` | 2,615 update lines yield dates; the month/year back-fill within `;` groups verified on `0105` and `0381` |
| 2c | `parse/body.py`, `ParsedSite`, goldens | all 67 goldens reviewed by eye; section headings hand-verified on every long page |

### 3 · Move the corpus audit-diff forward into Phase 3

`data/pages/` is committed byte-exactly, which settles the plan's open question. The
`-text` attribute is load-bearing, not cosmetic: 5,556 of the 5,557 pages use
CRLF, and a global `core.autocrlf=input` silently stripped it on the first
attempt to commit them. Since every parsed record is keyed by the SHA-256 of its
source bytes, and override staleness is decided by comparing those hashes,
normalised bytes would have made provenance disagree between clones.

The upstream site is still hand-edited, so the corpus will drift from what the
parsers were written against. That makes `source_file.content_sha256` and
`matienzo audit --diff` the mechanism for telling a parser regression apart from
an upstream edit — worth having from the first build rather than at the end.

**Change:** `source_file` with its content hash lands in the first schema, and
`matienzo audit --diff` ships with Phase 3 rather than Phase 6.

### 4 · Carry two known-bad upstream records as pinned exceptions

Both are real errors on the site, not parse failures, and both are already
asserted as exact sets so that a *new* occurrence fails rather than hiding:

- `5255.htm` is headed `5254: cave (2955 (French: SCD))`; its `<title>` and
  filename both say 5255. The filename wins.
- `4968`'s northing of 4,901,207 puts it 94.5 km north of every other site, the
  next-highest being 4,806,685.

Neither turned out to want an override. `5255` already parses correctly —
the parser trusts the filename — so the anomaly was regraded from error to
warning: the *source* is wrong, not the data, and grading it fatal would mean
the corpus could never reach a clean audit without editing someone else's
website. `4968` is flagged and left as it stands. The override mechanism is
built and tested; it simply has no work to do yet, which is the right outcome.

### 5 · The known weak spot was dropped, not tightened

`parse/header.py::_parse_prose` used to extract `target_names` with a loose
capitalised-word regex that had never been validated against the corpus. When it
finally was, it was wrong more often than right: the pattern stops at lowercase
Spanish articles, so `Cueva del Risco` came out as `Cueva` and `Sistema de
Colmenas-Escalón` as `Sistema`. Nothing read the field. Every prose measurement
already yields a `relation`, a `target_sites` number or a `system_name`, all of
which are checked, so the field was deleted rather than repaired — see the
comment left in its place at `parse/header.py`. Amendment 5 is discharged.

### 6 · The onnxruntime risk is discharged

The plan's most likely install failure was fastembed's dependency on
onnxruntime, historically slow to ship wheels for a new CPython minor. Probed
in an isolated venv: `fastembed==0.8.0` and `onnxruntime==1.28.0` resolve
cleanly on **both 3.13 and 3.14**, as does `sqlite-vec==0.1.9`, and the model
runs on both. The project has since moved to 3.14 and nothing about the
embedding stack needed changing, so no fallback interpreter is needed.

### 7 · One vocab file, not five; one normaliser, not six

`design.md` §1 sketches `data/vocab/` with five files (areas, authors,
qualifiers, systems, people) and a `normalise/` package with six modules. What
shipped is `data/vocab/areas.toml` and `normalise/areas.py`.

Areas are the only field where the page's own words needed a curated mapping to
canonical entities. Authors, citation qualifiers, systems and people all turned
out to be resolvable from the text itself — authors and qualifiers from the
citation grammar in `parse/citations.py`, systems from the prose `Quantity`
relations, people from evidence-gated mentions in `parse/body.py` — so a
hand-maintained vocabulary would have been a second, drifting source of truth
for something the parser already knew. The DB carries 163 authors, 9 systems and
80 people on that basis.

**Systems are the weak case, and the count flatters them.** Those 9 `cave_system`
rows are not 9 real systems: `Four Valleys System` (3 members), `Sistema de
Cuatro Valles` (3), `Valleys System` (5) and `Valley System` (1) are all the same
place under four spellings, and `Sistema de Torca` is a truncation. Systems are
the one entity type where the pages' own words genuinely do not resolve
cleanly — which is an argument for the curated roster the design left as an
open question, not against curation. The decision is tracked as an issue.

The rule in §1 still holds where it applies: `normalise/` is the only place that
consults `data/vocab/`, and the raw string is never destroyed. There is simply
one field it applies to.

### 8 · `data/overrides/` does not exist, and that is the pass condition

Phase 4's checkpoint asked for `data/overrides/` to be committed. The directory
is absent because no site needs an override — the two known-bad upstream records
(amendment 4) were both better served by anomalies than by corrections. The
loader in `overrides.py` and its tests exist and pass against fixtures.

Git cannot commit an empty directory, so `config.OVERRIDES_DIR` points at a path
that is not there on a fresh clone; the loader treats that as "no overrides"
rather than an error. The first real correction creates the directory.

## Outcome against the estimate

All seven phases are complete, against an original estimate of 10.5–14 days.
The largest single reason is that segmentation held: every later parser could
assume clean boundaries instead of re-deriving them, and no phase after 1 had
to revisit them.

## Amendments that came out of phases 2 to 6

**Confidence deductions for repeated per-item problems must be capped.** Vallina
(`0733`) parses perfectly — 114 citations, all resolved — but groups its footer
photo links under year headings, and an uncapped −0.05 per unrecognised label
drove it to 0.00, ranking it the worst page in the corpus. A score a healthy
page can bottom out on cannot rank pages for review.

**The threshold numbers are now real.** Derived from the finished parsers over
the whole corpus and asserted in `tests/test_db.py::TestFullCorpus`: 5,557
sites, 7,051 update dates, 752 distinct citations used 14,661 times, 2,378
cross-references. Amendment 1 is discharged.

**Site numbers and years are the same shape.** Not anticipated by the plan.
`site 2073` is not a date and `sites … 2035, 2036 and 2037` are not years, so
cross-reference spans are claimed before the date scan and bare years are
bounded to 1900–2030. Numbers inside that range with no cue word remain
genuinely ambiguous; that is a property of the source, not of the parser.

**Build the evaluation harness before trusting the retrieval.** Phase 6's eval
immediately found a bug that had been sitting in Phase 5's keyword search since
it shipped: terms were joined by juxtaposition, which FTS5 reads as AND, so
every multi-word descriptive query matched nothing and keyword search scored 0%
on that whole class. Nothing in the Phase 5 checkpoint would have caught it —
the name lookups it tested all worked.

**The eval also caught its own ground truth.** The first descriptive queries
were things any of hundreds of sites answer, so correct retrievals scored as
misses. Worth stating plainly because the failure mode is seductive: a bad
score looks like a system problem, and the instinct is to tune the system.

**Measure before weighting a fusion.** Down-weighting the keyword retriever
looked obviously right and was not. Re-measured over the 28-query set at
`keyword_weight` 0.3 to 1.0, every cut-off moves by at most one query:

| weight | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 | 0.9 | 1.0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| recall@1 | 18 | 17 | 17 | 17 | 17 | 17 | 17 | 16 |
| recall@5 | 22 | 22 | 22 | 21 | 21 | 21 | 21 | 22 |
| recall@10 | 23 | 23 | 23 | 23 | 23 | 24 | 25 | 25 |

Parity ties for the best recall@10 and is one query off the best recall@1. A
single query is noise at this sample size, so shipping a tuned constant would
have been fitting the eval. An earlier version of this note claimed recall@5 was
*identical* at every weighting; it is not, and the sweep above replaces it.

**Caching embeddings inside the artefact they cache is not caching.** The
design said vectors were cached by chunk content hash so a rebuild would only
re-embed what changed — but the cache lived in `matienzo.db`, which
`matienzo build` replaces wholesale, so every rebuild threw all 13,280 away and
cost a five-minute re-embed. `matienzo build` now reads the old file's vectors
out before replacing it and restores the ones whose chunk text is unchanged.
