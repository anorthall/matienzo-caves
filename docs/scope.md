# Scope: what of matienzocaves.org.uk is in, and what is out

`docs/design.md` is what was planned. `docs/plan-status.md` is what was built. This
is what is **in scope**, and — more importantly — what is deliberately not.

It exists because the design left five questions hanging (§8) and the answer to
four of them turned out to be "nobody decided". The same failure applied to the
site itself: the corpus is `descrip/` because that is what the reconnaissance
looked at, not because anything else was ruled out. This file is the ledger that
makes the difference visible.

**The rule: every part of the site appears below with a status.** A reader must
be able to tell "we decided against this" from "nobody looked". If something on
the site is not in this table, that is a bug in this file.

| Status | Meaning |
| --- | --- |
| **In** | Ingested, in `matienzo.db` today |
| **#N** | Under an open decision — see that issue |
| **Out** | Deliberately excluded, reason given |

Every row has one of the three. Nothing says "undecided" — see
[Verdicts taken](#verdicts-taken) for the sixteen that were settled here and the
reasoning behind each.

## How this inventory was built

There is no `sitemap.xml` and no `robots.txt` — both 404. So the site was
enumerated from three independent sources and the results unioned:

1. **The homepage menu**, `page1.php` — ~123 links across ABOUT / CAVES /
   EXPEDITIONS / SCIENCE / MISC / CONTACTS / PUBLICATIONS. (`matienzocaves.org.uk/`
   itself is a two-second redirect to `page1.htm`, which meta-refreshes to
   `page1.php`; fetchers that do not follow it see only a logo.)
2. **The inner-page nav bar**, present on `yeartab.htm` and most legacy pages.
   This is *not* the same as the homepage menu and surfaced sections the menu
   omits — `geog.htm`, `green.htm`, `cavelink.htm`, `indexaf.htm`, `indexnf.htm`.
3. **`resource_link`** — 38,501 rows of outbound links from the 5,557 corpus
   pages, which is the only source that reveals what the descriptions actually
   depend on, as opposed to what the site advertises.

Source 3 is the one that matters for completeness, and sources 1 and 2 are the
ones that matter for finding things no description happens to link.

### Two findings from doing it

**Paths are case-sensitive.** `miscdocs/Simonnot-G-2022.htm` returns 200;
`miscdocs/simonnot-g-2022.htm` returns 404. Normalising hrefs to lowercase for
grouping — which the queries in several issues do — is fine for counting and
wrong for fetching.

```sql
SELECT count(*) FROM resource_link
WHERE href <> lower(href) AND href NOT LIKE 'http%';
-- 1428
```

1,428 relative hrefs carry uppercase. Issues #1, #11, #16 and #19 all involve
resolving hrefs to files; all four must preserve case. This is written down here
because it is the kind of thing that gets rediscovered three times.

**Link counts are not file counts.** Repeatedly: `logbook/` is 20,649 links to
194 files; `history/` is 5,658 links to 169; `cantab/` is 292 links to 2. The
design's §8 figures ("5,521 logbook PDFs", "474 .3d files") are link instances
from a partial scrape. Any figure quoted below is distinct files unless it says
otherwise.

---

## The ledger

### The corpus

| Path | What it is | Status |
| --- | --- | --- |
| `descrip/*.htm` | 5,557 site descriptions | **In** — this is the corpus |
| `descrip/pdfs/` | The same descriptions as 7-volume PDFs | **Out** — the corpus reprinted; ingesting it would store the same facts twice in a worse format |
| `indexaf.htm`, `indexnf.htm`, `indexin.htm`, `browse.htm`, `Random.htm`, `qa.htm` | Alphabetical / numerical / frames indices, browse and random-site aids, quick-access descriptions | **Out** — navigation over data already held |
| `cave_search_01Z.php`, `descrip/typeahead.php`, `get_suggestions.php`, `extenders.php`, `research-essentials.php` | Query interfaces: cave search, autocomplete, exploration-potential finder | **Out** as data. Worth reading as prior art for the RAG portal — `extenders.php` in particular is an existing answer to "find sites with potential" |
| `AppendixWord.pdf` | Cave statistics | **Out** — `matienzo stats` computes these from the corpus |

### Pictures and media

| Path | What it is | Status |
| --- | --- | --- |
| `entpics/` | 4,079 entrance-picture pages, 3,617 sites | **#1** |
| `ugpics/` | 1,972 underground-picture pages, 982 sites | **#1** |
| `videos/` | 553 files (MPG/WMV/AVI/MOV), 245 sites | **#16** |
| YouTube links | 663 links, 360 sites | **#16** |
| `archaeology/3232-youtube video transcript-en.pdf` | A video transcript — evidence that at least some footage has one | **#16** |
| `miscpics/` | 38 pages, 29 sites | **#1** — same kind of thing as the entrance/underground galleries; decided with them |
| `aerialphotos/`, `AerialPhotos/`, `aerialphotos/panos/`, `360help.htm` | Aerial photography, 360° panoramas, viewer help (24 links, 19 sites) | **#1** — site imagery; decided with the galleries |

### Surveys

| Path | What it is | Status |
| --- | --- | --- |
| `surveys/*.3d` | 718 Survex files, 681 sites | **#2** |
| `surveys/*.pdf\|jpg\|gif\|png\|svg` | 1,855 drawn surveys, 999 sites | **#11** |
| `rose-diags/*.png` | 69 rose diagrams, 105 sites — derived from the `.3d` files | **#11** |
| `surveys/all.3d` | Whole-area centre lines, single file | **#19** |
| `SurvexRecreate/`, `surveying-help/` | Survex recreation; surveying help documents | **Out** — method documentation, not data about caves |

### Logbooks and expedition record

| Path | What it is | Status |
| --- | --- | --- |
| `logbook.php` | The Logbook Search index — 21,718 dated entries | **#9** |
| `logbook/*.pdf` | 194 logbook PDFs, 5,206 sites | **#10** |
| `logbook/*.jpg` | Page scans, one per logbook page | **#10** |
| `history/*.htm` | **49 annual report pages** — 1978–2026 plus `earlyyrs.htm` (no 1990; 1998 is `1998all.htm`) | **#12** |
| `history/*.pdf` | 169 PDFs, 1,187 sites | **#12** |
| `yeartab.htm` | The index over both | **#12** |
| `logbook/matienzo_dashboard.html`, `references-dashboard.php` | Dashboards over the logbook and reference data | **Out** — renderings of #9 and #15 |
| `logbook/logbook-sheet.pdf` | Blank trip-report form | **Out** — a form, not a record |
| `history/50years/`, `history/60years/` | The 50th and 60th anniversary books | **#12** — project record. #14 already depends on the 2010–2019 book, which is where the sump index comes from |
| `history/Press.htm` | Newspaper articles | **#12** — project record |

### Systems, geography and maps

| Path | What it is | Status |
| --- | --- | --- |
| `Systems/intro.php` | 63 systems, 148 member entrances | **#3** |
| `science/index-water-tracing.htm` | ~50 dye traces, 43 sites | **#13** |
| `Leaflet/`, `MCP-QGIS3/`, `ge/`, `OruxMaps/` | Interactive, QGIS, Google Earth and offline map layers | **#19** |
| `Mat-Map-latest.jpg`, `Mat-Map-2022March.jpg`, `matienzo-map-latest.pdf` | Area maps | **#19** |
| `No_Grid_Refs.htm` | 72 sites with no grid reference, plus duplicate-site assertions | **#19** |
| `area-permit.jpg`, `area-permit-2014.pdf`, `history/permit-2021-sites.jpg` | Permit-area boundaries, dated | **#19** |
| `geog.htm` | "Matienzo: Scenery" — surface views | **Out** — surface scenery photography, no site data |

### Science

| Path | What it is | Status |
| --- | --- | --- |
| `science.htm` | Index of ~15 research strands | **#17** |
| `science/Bats/` | Bats in Caves Project — roost records, species tables | **#17** (the `bat_observation` table currently holds 15 rows) |
| `science/index-radon.htm`, `science/suscept.htm`, `science/IR.htm` | Radon, magnetic susceptibility, infrared photography | **#17** |
| `science/Hypogene/`, `science/Breakdown/`, `speleothems/` | Hypogene caves, roof breakdown, speleothems & speleogens | **#17** |
| `science/*.pdf` | 7 papers linked from descriptions, 20 sites | **#17** / **#15** |
| `archaeol.htm`, `Archaeology/` | Archaeology overview, Bronze Age pottery, teeth & bones, Barandas | **#17** |
| `mkep.htm` | Matienzo Karst Entomology Project | **#17** |
| `dowsing/index.htm` | "Matienzo — Dowsing for Caves" — 17 links, 17 sites | **Out** — checked: a single article about dowsing as a prospecting method. The 17 links are references *to* it, not findings *from* it |

### Publications and reference

| Path | What it is | Status |
| --- | --- | --- |
| `bibliog.htm` | 485 full references | **#15** |
| `cantab/cantasub2010-vol1.pdf`, `vol2.pdf` | Cantabria Subterránea 2010 — 292 links, 69 sites | **#15** — resolve as citations |
| `miscdocs/Simonnot-G-2022.htm` | A bibliography page. **634 links from 317 sites — the most-linked single document on the site** | **#15** |
| `miscdocs/MEMORIA-FCE-LACAMBERA-2021.pdf` | 88 links, 44 sites | **#15** |
| `miscdocs/sautuola_XX_Matienzo.pdf` | 36 links, 18 sites | **#15** |
| `miscdocs/ADEMCO2019.pdf`, `KA-Memoria-...-Vallina-2023.pdf`, `3234-StreamDeposit.pdf` | Cited papers, few links each | **#15** |
| `miscdocs/index.htm` | Index listing UIS cave & karst protection guidelines, Eurospeleo Forum 2022 actas, BCE-12-Riotuerto | **Out** — external guideline documents, not Matienzo data |
| `Library-Books/library.php` | Book library catalogue | **Out** — checked: a searchable register of physical books the project holds, not works about Matienzo. `bibliog.htm` (#15) is the bibliography |
| `dicts/intro.html` | Spanish–English caving dictionary | **#23** — vocabulary, not payload |

### Project record and practicalities

| Path | What it is | Status |
| --- | --- | --- |
| `latest-website-updates.htm` | Maintenance table: which section was refreshed after which expedition, and when | **#18** — this is a currency signal, not content |
| `quarry.htm`, `windfarm.htm`, `windfarms-most-recent.htm` | Threats to the caves | **#12** — project record. The 2025 annual report devotes substantial space to the wind farms, so excluding these would leave dangling references inside ingested prose |
| `miscpics/conserve/` | Conservation | **Out** — conservation policy and guidance, not observations about sites |
| `nathist/web_matienzo/` | Matienzo environment article, English and Spanish | **#17** — an environmental study; belongs with the science strands, and adds to the language question |
| `nathist/flowers/` | Flowers catalogue | **Out** — not about caves |
| `music/music.html` | Songs | **Out** — not about caves |
| `green.htm` | Expedition details | **Out** — practicalities |
| `travelweather.php`, `asiul.htm` | Weather, travel, accommodation | **Out** — practicalities |
| `finances/`, `recent_grants.htm`, `Obligations.pdf`, `data-collection.pdf` | Assets, tackle, GPF grants, obligations, data-collection policy | **Out** — project administration |
| `MatienzoCavesProject.htm`, `contacts.htm`, `feedback/`, `miscdocs/StudentInvite/` | Meetings, contacts, feedback reports, new-member material | **Out** — project administration |
| `cavelink.htm`, `genlinks.htm` | Other caving sites, general links | **Out** — outbound links |
| `miscdocs/via-Ferratas-...pdf`, `miscdocs/FAQs.htm` | Via ferratas, FAQs | **Out** |

### The corpus itself

| Concern | Status |
| --- | --- |
| Corpus is 54 sites behind the live site (5,557 vs 5,611 at 2026-07-16) | **#18** |
| Whether the database models history or stays a snapshot | **#4** |

---

## Verdicts taken

Sixteen sections had no status when this audit started. They are settled below.
The reasoning is recorded because a verdict without one is just a default with
better handwriting — which is the thing this file exists to prevent. Reversing
any of them means editing one row and this section.

### In, routed to an existing issue

**The cited-document group → #15.** `cantab/`, `Simonnot-G-2022.htm`, and the
`miscdocs/` PDFs:

```sql
SELECT count(*) links, count(DISTINCT site_number) sites, count(DISTINCT href) hrefs
FROM resource_link
WHERE href LIKE '%cantab/cantasub2010-vol%' OR href LIKE '%Simonnot-G-2022%'
   OR href LIKE '%memoria-fce-lacambera-2021%' OR href LIKE '%sautuola_xx_matienzo%'
   OR href LIKE '%ademco2019%' OR href LIKE '%vallina-2023%'
   OR href LIKE '%3234-streamdeposit%';
-- 1060 | 439 | 9
```

**1,060 links from 439 sites, across 9 hrefs — but only 8 works.** The Vallina
2023 memoria is linked at two paths, `miscdocs/KA-Memoria-de-Exploracion-Vallina-2023.pdf`
and `history/0733-Memoria-de-Exploracion-Vallina-2023.pdf`. That is a worked
example of the de-duplication problem #15 has to solve, and a reason to resolve
these as citations rather than as files: by path they are two things, by work
they are one.

They are already *citations* in everything but name, and leaving them as dangling
hrefs while building a bibliography table would be incoherent.

**Imagery → #1.** `miscpics/`, and the aerial / 360° material. These are the
same kind of thing as `entpics/` and `ugpics/` — hand-written gallery pages
whose payload is captions — so they should be decided once, with the galleries,
not separately.

**The environment article → #17.** `nathist/web_matienzo/` is an environmental
study, so it belongs with the science strands rather than with natural-history
miscellany. It is also bilingual, which adds a third item to the language
question below.

**The dictionary → #23**, raised for it. Vocabulary, not payload; the one
leftover with leverage disproportionate to its size.

### In, on the "project record" question

The largest of the sixteen turned on a single question: **is the project's own
record in scope, or only the caves?** Answered **yes, it is in scope**, routing
`history/50years/`, `history/60years/`, `Press.htm` and the threats pages
(`quarry.htm`, `windfarm.htm`, `windfarms-most-recent.htm`) to **#12**.

The argument is consistency rather than value. #12 ingests the 49 annual
reports; the 2025 report devotes substantial space to the wind farms, and the
anniversary books are already load-bearing — #14's sump index is drawn from
*Matienzo Caves Project 2010 – 2019*, pages 432–444. Ingesting the reports while
excluding what they reference leaves dangling links inside ingested prose.

If this is the verdict to overturn, overturn it here: it is one question
governing five sections, and #12 is where the cost lands.

### Out

| Section | Why |
| --- | --- |
| `dowsing/` | Checked rather than assumed: "Matienzo — Dowsing for Caves" is one article about a prospecting method. The 17 site links are references *to* it, not findings *from* it. Nothing to ingest. |
| `Library-Books/` | Checked: a searchable register of physical books the project owns. Books *held*, not works *about* Matienzo — `bibliog.htm` (#15) is the bibliography. |
| `geog.htm` | Surface scenery photography, no site data. |
| `miscpics/conserve/` | Conservation policy and guidance, not observations about sites. |
| `nathist/flowers/`, `music/` | Not about caves. A caves corpus that returns flower records and song lyrics is worse at its job, not better. |

**Two cross-cutting questions surfaced by the audit, neither owned by any issue:**

- **Language policy.** The sump index (#14) is bilingual, the environment
  article is bilingual, and the gastropods material is Castellano and English.
  Three separate items now need the same decision: does `chunk` carry a
  language, and does search index both? #14 raises it; nothing owns it.
- **A `document`-shaped home.** #12, #14, #15 and #17 each independently want to
  store text that is not a site. Four issues asking for the same table means it
  should be designed once, before the first of them lands.

## What this file does not cover

Stated so the gaps are gaps on purpose:

- **Depth.** Directories were classified by what they are, not by walking every
  file. `entpics/` is one row here; it is 4,079 pages. The per-directory counts
  come from `resource_link`, so they count what the descriptions link — a file
  on the server that nothing links to would not appear.
- **The live site moves.** Everything here was checked on 2026-08-10 against a
  site whose own data table updated 2026-07-16. `latest-website-updates.htm`
  is the upstream signal for what has changed since.
- **Two kinds of claim share one column.** Rows marked **In** are statements of
  fact about `matienzo.db` today. Rows marked **Out** or **#N** are decisions,
  and decisions can be wrong — [Verdicts taken](#verdicts-taken) gives the
  reasoning for each so it can be argued with rather than guessed at.
