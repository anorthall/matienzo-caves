# Plan status

Tracks the approved plan against what is actually built. Update it when a phase
checkpoint is met or an assumption turns out to be wrong.

## Where things stand

| Phase | Estimate | State | Checkpoint |
| --- | --- | --- | --- |
| 0 · Skeleton + decode | ½ day | **Done** | `matienzo decode --audit` → 5,507 ASCII / 32 UTF-8 / 18 CP1252, 0 errors |
| 1 · Segmentation | 1 day | **Done** | `matienzo segment --audit` → title on 5,557; 3 stubs lack a header; 0 leaks |
| 2 · Parsers + models | 3–4 days | **Done** | `matienzo parse --audit` → 5,557 records, 1 error (a known upstream typo) |
| 3 · Schema + load | 2 days | **Done** | `matienzo build` → 25 MB in 4.3 s, reproducible; `matienzo stats` |
| 4 · Review queue | 1–2 days | **Done** | `matienzo review`; overrides; 0 error-severity anomalies |
| 5 · Chunking + FTS + CLI | 1 day | **Done** | `matienzo search / show / nearby / graph`; 13,280 chunks |
| 6 · Embeddings + hybrid | 1–2 days | Next | |
| 7 · MCP server | 1 day | Not started | |

461 tests pass; `ruff check` clean.

### What the database contains

5,557 sites · 81 areas · 5,502 coordinates · 6,774 measurements (62 of them
prose) · 7,051 update dates · 12,089 description blocks · 139 sections ·
752 distinct citations used 14,661 times · 163 authors · 2,378
cross-references · 9 cave systems with 25 memberships · 80 people from 283
evidence-gated mentions · 38,501 resource links.

One error-severity anomaly remains: the upstream `5254`/`5255` heading typo,
which wants an override in Phase 4.

## Verdict: the plan holds, with amendments

The architecture survived contact with the corpus. Three decisions in particular
earned their keep and should not be revisited:

- **Anomalies as data rather than exceptions.** Every surprise so far — an
  upstream typo, an unrecorded coordinate, a prose measurement — landed as a
  queryable row instead of a crash or a silent `None`.
- **Locating the footer before the header.** Non-obvious, and `5528` breaks any
  other ordering.
- **`Quantity` carrying an interpretation kind.** 61 prose lengths yielded 22
  system-membership facts and dozens of cross-references that a `float | None`
  column would have discarded.

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
| 2a | `parse/footer.py`, `parse/citations.py` | citation count stable under two independent counting methods; no `Fern&aacute` fragments; the 234 split-anchor pages yield one citation each |
| 2b | `parse/updated.py` | 2,615 update lines yield dates; the month/year back-fill within `;` groups verified on `0105` and `0381` |
| 2c | `parse/body.py`, `ParsedSite`, goldens | all 66 goldens reviewed by eye; section headings hand-verified on every long page |

### 3 · Move the corpus audit-diff forward into Phase 3

`pages/` is committed byte-exactly, which settles the plan's open question. The
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
- `4968`'s northing of 4,901,207 puts it 96 km north of every other site.

Both want a `data/overrides/*.toml` entry in Phase 4.

### 5 · Known weak spot, still open

`parse/header.py::_parse_prose` extracts `target_names` with a loose
capitalised-word regex that has not been validated against the corpus. It is
still the least trustworthy code in the package. Nothing depends on it — the
useful outputs of a prose measurement are `relation`, `target_sites` and
`system_name`, all of which are checked — so it is not urgent, but it should
either be tightened or dropped.

## Revised estimate

Roughly **4–6 days** remaining, against the original 10.5–14 total. Phases 0–3
came in under estimate, largely because segmentation held: every later parser
could assume clean boundaries rather than re-deriving them.

## Amendments that came out of phases 2 and 3

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
