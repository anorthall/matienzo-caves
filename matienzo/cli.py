"""The `matienzo` command line."""

from __future__ import annotations

import collections
import sqlite3
import time
from pathlib import Path

import cyclopts
from rich.console import Console
from rich.table import Table

from matienzo import __version__, config
from matienzo import embed as embed_module
from matienzo import evaluate as evaluate_module
from matienzo import overrides as overrides_module
from matienzo import search as search_module
from matienzo.anomaly import AnomalyRecorder, Severity
from matienzo.db import audit as db_audit
from matienzo.db import load as db_load
from matienzo.db import queries
from matienzo.db.connect import connect, fresh_database
from matienzo.decode import Encoding, read_document
from matienzo.htmlutil import strip_tags
from matienzo.models import ParsedSite
from matienzo.parse import parse_page
from matienzo.parse.segment import segment as segment_document

app = cyclopts.App(
    name="matienzo",
    help="Extract, structure and search the Matienzo Caves site descriptions.",
    version=__version__,
)

console = Console()


@app.command
def decode(site: int | None = None, *, audit: bool = False, head: int | None = None) -> None:
    """Decode a source page to text, or audit the encoding of the whole corpus.

    Parameters
    ----------
    site
        Site number to decode, e.g. `39`. Omit when using `--audit`.
    audit
        Report the encoding breakdown across every page instead of printing one.
    head
        Print only the first N lines of the decoded page.
    """
    if audit:
        _decode_audit()
        return

    if site is None:
        console.print("[red]Give a site number, or pass --audit.[/red]")
        raise SystemExit(2)

    path = config.page_path(site)
    if not path.exists():
        console.print(f"[red]No such page: {path}[/red]")
        raise SystemExit(1)

    recorder = AnomalyRecorder()
    doc = read_document(path, recorder=recorder)

    lines = doc.text.splitlines()
    if head is not None:
        lines = lines[:head]
    print("\n".join(lines))

    for anomaly in recorder.items:
        console.print(f"[yellow]{anomaly.severity}[/yellow] {anomaly.code}: {anomaly.detail}")


def _decode_audit() -> None:
    """Decode every page and report how the bytes broke down.

    This is the Phase 0 checkpoint: the expected result on the corpus as scraped
    is 5,507 ASCII-only, 32 UTF-8, 18 CP1252, and zero mojibake.
    """
    paths = config.iter_page_paths()
    tally: collections.Counter[str] = collections.Counter()
    anomalies: collections.Counter[str] = collections.Counter()
    fallback_sites: list[int] = []
    utf8_sites: list[int] = []
    errors: list[tuple[int, str]] = []

    for path in paths:
        recorder = AnomalyRecorder()
        doc = read_document(path, recorder=recorder)

        if doc.text.isascii():
            tally["ascii-only"] += 1
        elif doc.encoding is Encoding.UTF8:
            tally["utf-8"] += 1
            utf8_sites.append(doc.site_number)
        else:
            tally["cp1252"] += 1
            fallback_sites.append(doc.site_number)

        for anomaly in recorder.items:
            anomalies[anomaly.code] += 1
            if anomaly.severity is Severity.ERROR:
                errors.append((doc.site_number, f"{anomaly.code}: {anomaly.detail}"))

    table = Table(title=f"Encoding of {len(paths)} pages", title_justify="left")
    table.add_column("Path")
    table.add_column("Files", justify="right")
    for key in ("ascii-only", "utf-8", "cp1252"):
        table.add_row(key, f"{tally[key]:,}")
    console.print(table)

    console.print(f"\n[dim]UTF-8 sites:[/dim] {_fmt_sites(utf8_sites)}")
    console.print(f"[dim]CP1252 sites:[/dim] {_fmt_sites(fallback_sites)}")

    if anomalies:
        console.print("\n[bold]Anomalies[/bold]")
        for code, count in anomalies.most_common():
            console.print(f"  {count:>5,}  {code}")

    if errors:
        console.print(f"\n[red]{len(errors)} error-severity anomalies:[/red]")
        for site_number, detail in errors[:20]:
            console.print(f"  {site_number:04d}  {detail}")
        raise SystemExit(1)

    console.print("\n[green]No error-severity anomalies.[/green]")


SEGMENT_NAMES = ("title", "header", "updated", "body", "footer", "modal")


@app.command
def segment(site: int | None = None, *, audit: bool = False, show: str | None = None) -> None:
    """Split a page into its structural segments, or audit the whole corpus.

    Parameters
    ----------
    site
        Site number to segment, e.g. `1930`.
    audit
        Report segment coverage across every page instead of showing one.
    show
        Print just one segment's raw markup: title, header, updated, body,
        footer or modal.
    """
    if audit:
        _segment_audit()
        return

    if site is None:
        console.print("[red]Give a site number, or pass --audit.[/red]")
        raise SystemExit(2)

    path = config.page_path(site)
    if not path.exists():
        console.print(f"[red]No such page: {path}[/red]")
        raise SystemExit(1)

    recorder = AnomalyRecorder()
    segments = segment_document(read_document(path, recorder=recorder), recorder)

    if show is not None:
        if show not in SEGMENT_NAMES:
            console.print(f"[red]--show must be one of: {', '.join(SEGMENT_NAMES)}[/red]")
            raise SystemExit(2)
        span = getattr(segments, show)
        if span is None:
            console.print(f"[yellow]No {show} segment on site {site:04d}.[/yellow]")
            raise SystemExit(1)
        print(span.text)
        return

    table = Table(title=f"Site {site:04d} segments", title_justify="left")
    table.add_column("Segment")
    table.add_column("Chars", justify="right")
    table.add_column("Offsets", justify="right")
    table.add_column("Preview", overflow="ellipsis", max_width=60)
    for name in SEGMENT_NAMES:
        span = getattr(segments, name)
        if span is None:
            table.add_row(name, "—", "—", "[dim]absent[/dim]")
        else:
            preview = " ".join(span.text.split())[:200]
            table.add_row(name, f"{len(span):,}", f"{span.start}–{span.end}", preview)
    console.print(table)

    for anomaly in recorder.items:
        console.print(f"[yellow]{anomaly.severity}[/yellow] {anomaly.code}: {anomaly.detail}")


def _segment_audit() -> None:
    """Segment every page and report coverage.

    The Phase 1 checkpoint: every page yields a title, exactly three stub pages
    lack a header and footer, and no page leaks header or footer content into
    its description.
    """
    paths = config.iter_page_paths()
    found: collections.Counter[str] = collections.Counter()
    anomalies: collections.Counter[str] = collections.Counter()
    missing: dict[str, list[int]] = {name: [] for name in SEGMENT_NAMES}
    errors: list[tuple[int, str]] = []
    body_chars: list[int] = []
    stubs: list[int] = []

    for path in paths:
        recorder = AnomalyRecorder()
        segments = segment_document(read_document(path, recorder=recorder), recorder)

        for name, present in segments.flags.model_dump().items():
            if present:
                found[name] += 1
            else:
                missing[name].append(segments.site_number)
        if segments.is_stub:
            stubs.append(segments.site_number)
        # Measured on the visible text, not the markup: collapsing the raw
        # fragment counts every tag and attribute, which roughly triples the
        # apparent size of a short description.
        body_chars.append(len(strip_tags(segments.body.text)))

        for anomaly in recorder.items:
            anomalies[anomaly.code] += 1
            if anomaly.severity is Severity.ERROR:
                errors.append((segments.site_number, f"{anomaly.code}: {anomaly.detail}"))

    total = len(paths)
    table = Table(title=f"Segment coverage over {total:,} pages", title_justify="left")
    table.add_column("Segment")
    table.add_column("Found", justify="right")
    table.add_column("%", justify="right")
    table.add_column("Missing from", overflow="fold")
    for name in SEGMENT_NAMES:
        absent = missing[name]
        shown = _fmt_sites(absent) if len(absent) <= 8 else f"{len(absent):,} pages"
        table.add_row(name, f"{found[name]:,}", f"{100 * found[name] / total:5.1f}", shown)
    console.print(table)

    body_chars.sort()
    console.print(
        f"\n[dim]Body size (visible chars):[/dim] "
        f"min {body_chars[0]:,} · "
        f"p50 {body_chars[total // 2]:,} · "
        f"p90 {body_chars[total * 9 // 10]:,} · "
        f"max {body_chars[-1]:,}"
    )
    console.print(f"[dim]Minimal records (<70 chars):[/dim] {sum(c < 70 for c in body_chars):,}")
    console.print(f"[dim]Stub pages (no header, no footer):[/dim] {_fmt_sites(stubs)}")

    if anomalies:
        console.print("\n[bold]Anomalies[/bold]")
        for code, count in anomalies.most_common():
            console.print(f"  {count:>5,}  {code}")

    if errors:
        console.print(f"\n[red]{len(errors)} error-severity anomalies:[/red]")
        for site_number, detail in errors[:20]:
            console.print(f"  {site_number:04d}  {detail}")
        raise SystemExit(1)

    console.print("\n[green]No error-severity anomalies.[/green]")


@app.command
def parse(site: int | None = None, *, audit: bool = False, json: bool = False) -> None:
    """Parse a page into a full record, or audit the whole corpus.

    Parameters
    ----------
    site
        Site number to parse, e.g. `1930`.
    audit
        Report parse coverage and anomalies across every page.
    json
        Emit the record as JSON rather than a summary table.
    """
    if audit:
        _parse_audit()
        return

    if site is None:
        console.print("[red]Give a site number, or pass --audit.[/red]")
        raise SystemExit(2)

    path = config.page_path(site)
    if not path.exists():
        console.print(f"[red]No such page: {path}[/red]")
        raise SystemExit(1)

    record = parse_page(path)
    if json:
        print(record.model_dump_json(indent=2))
        return

    _show_record(record)


def _show_record(record: ParsedSite) -> None:
    title = record.title
    console.print(
        f"[bold]{record.site_number:04d}[/bold] "
        f"{title.name if title else '[dim]unnamed[/dim]'}"
        + (f"  [dim]({title.site_type})[/dim]" if title and title.site_type else "")
    )
    if title and title.aliases:
        console.print("  [dim]also known as:[/dim] " + "; ".join(a.text for a in title.aliases))

    if record.header:
        for coordinate in record.header.coordinates:
            label = f"{coordinate.label} " if coordinate.label else ""
            position = (
                f"{coordinate.zone} {coordinate.easting} {coordinate.northing}"
                if coordinate.easting
                else coordinate.raw
            )
            altitude = f", {coordinate.altitude_m:g}m" if coordinate.altitude_m else ""
            console.print(f"  [dim]{label}position:[/dim] {position}{altitude}")
        for quantity in record.header.quantities:
            value = (
                f"{quantity.value_m:g}m"
                if quantity.value_m is not None
                else f"[yellow]{quantity.raw[:60]}[/yellow]"
            )
            modifier = "" if quantity.modifier.value == "exact" else f" ({quantity.modifier})"
            console.print(f"  [dim]{quantity.label}:[/dim] {value}{modifier}")
        if record.header.area_raw:
            console.print(f"  [dim]area:[/dim] {record.header.area_raw}")

    if record.updated:
        first, last = record.updated[0].date, record.updated[-1].date
        console.print(f"  [dim]updated:[/dim] {len(record.updated)} entries, {first} … {last}")

    body = record.body
    console.print(
        f"  [dim]description:[/dim] {body.char_count:,} chars, "
        f"{len(body.blocks)} blocks, {len(body.sections)} sections"
        + (" [yellow](minimal)[/yellow]" if body.is_minimal else "")
    )
    for section in body.sections:
        console.print(f"      · {section.heading}")
    if body.cross_refs:
        console.print(
            "  [dim]references sites:[/dim] "
            + ", ".join(f"{r.target_site:04d}" for r in body.cross_refs[:12])
        )
    if record.footer:
        console.print(f"  [dim]citations:[/dim] {len(record.footer.citations)}")

    confidence = record.provenance.confidence
    worst = min(confidence, key=lambda k: confidence[k])
    console.print(
        "  [dim]confidence:[/dim] "
        + " ".join(f"{k}={v:.2f}" for k, v in confidence.items())
        + (f"   [yellow]weakest: {worst}[/yellow]" if confidence[worst] < 1.0 else "")
    )
    for anomaly in record.provenance.anomalies:
        colour = {"error": "red", "warn": "yellow", "info": "dim"}[anomaly.severity.value]
        console.print(
            f"  [{colour}]{anomaly.severity:5}[/{colour}] {anomaly.code}: {anomaly.detail}"
        )


def _parse_audit() -> None:
    """Parse every page and report coverage, anomalies and confidence.

    The Phase 2 checkpoint.
    """
    paths = config.iter_page_paths()
    anomalies: collections.Counter[str] = collections.Counter()
    errors: list[tuple[int, str]] = []
    totals: collections.Counter[str] = collections.Counter()
    low_confidence: list[tuple[float, int, str]] = []

    for path in paths:
        record = parse_page(path)
        totals["sites"] += 1
        totals["with_title"] += record.title is not None
        totals["with_area"] += bool(record.header and record.header.area_raw)
        totals["with_coords"] += bool(record.header and record.header.coordinates)
        totals["with_latlon"] += bool(
            record.header and any(c.latitude for c in record.header.coordinates)
        )
        totals["numeric_length"] += bool(
            record.header
            and any(q.label == "length" and q.value_m is not None for q in record.header.quantities)
        )
        totals["update_dates"] += len(record.updated)
        totals["citations"] += len(record.footer.citations) if record.footer else 0
        totals["cross_refs"] += len(record.body.cross_refs)
        totals["sections"] += len(record.body.sections)
        totals["people"] += len(record.body.people)
        totals["minimal_bodies"] += record.body.is_minimal

        for anomaly in record.provenance.anomalies:
            anomalies[anomaly.code] += 1
            if anomaly.severity is Severity.ERROR:
                errors.append((record.site_number, f"{anomaly.code}: {anomaly.detail}"))

        worst = min(record.provenance.confidence.values(), default=1.0)
        if worst < 0.8:
            section = min(
                record.provenance.confidence, key=lambda k: record.provenance.confidence[k]
            )
            low_confidence.append((worst, record.site_number, section))

    total = totals["sites"]
    table = Table(title=f"Parsed {total:,} pages", title_justify="left")
    table.add_column("Measure")
    table.add_column("Count", justify="right")
    table.add_column("%", justify="right")
    for key in ("with_title", "with_area", "with_coords", "with_latlon", "numeric_length"):
        table.add_row(key, f"{totals[key]:,}", f"{100 * totals[key] / total:5.1f}")
    for key in (
        "update_dates",
        "citations",
        "cross_refs",
        "sections",
        "people",
        "minimal_bodies",
    ):
        table.add_row(key, f"{totals[key]:,}", "")
    console.print(table)

    if anomalies:
        console.print("\n[bold]Anomalies[/bold]")
        for code, count in anomalies.most_common(20):
            console.print(f"  {count:>6,}  {code}")

    low_confidence.sort()
    console.print(f"\n[dim]Pages below 0.8 confidence:[/dim] {len(low_confidence):,}")
    for score, site_number, section in low_confidence[:10]:
        console.print(f"  {site_number:04d}  {section}={score:.2f}")

    if errors:
        console.print(f"\n[red]{len(errors)} error-severity anomalies:[/red]")
        for site_number, detail in errors[:20]:
            console.print(f"  {site_number:04d}  {detail}")
        raise SystemExit(1)

    console.print("\n[green]No error-severity anomalies.[/green]")


@app.command
def build(*, out: Path | None = None) -> None:
    """Rebuild `matienzo.db` from data/pages/ and data/.

    Always starts from an empty file. The database is derived, so there is no
    migration path to get wrong — deleting it is a supported recovery.

    Parameters
    ----------
    out
        Write to this path instead of the default `matienzo.db`.
    """
    target = out or config.DB_PATH
    started = time.monotonic()

    # Read the old file's vectors out before it is replaced. Embeddings are
    # cached by chunk content hash, but the cache lives inside the database, so
    # without this every rebuild would throw them away and cost a five-minute
    # re-embed for chunks that mostly did not change.
    cached_vectors = embed_module.export_vectors(target)
    restored = 0

    try:
        with fresh_database(target) as connection:
            build_id = db_load.build(connection)
            restored = embed_module.import_vectors(connection, cached_vectors)
    except db_load.UnmappedAreasError as error:
        console.print(f"[red]{error}[/red]")
        raise SystemExit(1) from error

    elapsed = time.monotonic() - started
    size = target.stat().st_size / 1_000_000
    console.print(
        f"[green]Built[/green] {target.name} (build {build_id}, {size:.1f} MB, {elapsed:.1f}s)"
    )
    if cached_vectors:
        console.print(
            f"[dim]Carried over {restored:,} of {len(cached_vectors):,} cached embeddings.[/dim]"
        )
        if restored < len(cached_vectors):
            console.print("[dim]Run `matienzo embed` to fill the rest.[/dim]")


@app.command
def stats(*, db: Path | None = None, top: int = 10) -> None:
    """Summarise what is in the database.

    Parameters
    ----------
    db
        Read this database instead of the default.
    top
        How many rows to show in each ranked table.
    """
    path = db or config.DB_PATH
    if not path.exists():
        console.print(f"[red]No database at {path}. Run `matienzo build`.[/red]")
        raise SystemExit(1)

    connection = connect(path, read_only=True)
    try:
        table = Table(title="Contents", title_justify="left", box=None)
        table.add_column("")
        table.add_column("", justify="right")
        for label, sql in queries.STATS.items():
            table.add_row(label, f"{queries.scalar(connection, sql):,}")
        console.print(table)

        _ranked(connection, queries.LONGEST, top, "Longest caves", ("site", "name", "area", "m"))
        _ranked(connection, queries.DEEPEST, top, "Deepest caves", ("site", "name", "area", "m"))
        _ranked(
            connection,
            queries.BY_AREA,
            top,
            "Busiest areas",
            ("area", "sites", "total length", "deepest"),
        )
        _ranked(
            connection,
            queries.HUBS,
            top,
            "Most referenced sites",
            ("site", "name", "area", "refs in", "refs out"),
        )
        _ranked(
            connection,
            queries.MOST_CITED,
            top,
            "Most cited works",
            ("citation", "year", "kind", "sites"),
        )

        systems = queries.rows(connection, queries.SYSTEMS)
        if systems:
            console.print("\n[bold]Cave systems[/bold]")
            for row in systems:
                console.print(f"  {row['members']:>3}  {row['name']}")
    finally:
        connection.close()


def _ranked(connection: object, sql: str, limit: int, title: str, headers: tuple[str, ...]) -> None:

    assert isinstance(connection, sqlite3.Connection)
    results = queries.rows(connection, sql, limit)
    if not results:
        return
    table = Table(title=title, title_justify="left")
    for index, header in enumerate(headers):
        table.add_column(header, justify="right" if index else "left")
    for row in results:
        cells = []
        for value in tuple(row):
            if isinstance(value, float):
                cells.append(f"{value:,.0f}")
            elif value is None:
                cells.append("—")
            else:
                cells.append(str(value))
        table.add_row(*cells)
    console.print()
    console.print(table)


@app.command
def audit(*, db: Path | None = None, diff: bool = False) -> None:
    """Check the database against the corpus and report its health.

    Parameters
    ----------
    db
        Audit this database instead of the default.
    diff
        Also compare `data/pages/` against the bytes the database was built from,
        and re-parse anything that changed.
    """
    path = db or config.DB_PATH
    if not path.exists():
        console.print(f"[red]No database at {path}. Run `matienzo build`.[/red]")
        raise SystemExit(1)

    connection = connect(path, read_only=True)
    try:
        report = db_audit.audit(connection)

        table = Table(title="Database", title_justify="left", box=None)
        table.add_column("")
        table.add_column("", justify="right")
        for name, value in report.metrics.items():
            table.add_row(name, f"{value:,}")
        console.print(table)

        if report.metrics["errors"]:
            console.print(
                f"\n[yellow]{report.metrics['errors']} error-severity "
                f"anomalies await an override (see `matienzo review`).[/yellow]"
            )
            for row in connection.execute(
                "SELECT site_number, code, detail FROM anomaly WHERE severity = 'error'"
                " ORDER BY site_number LIMIT 20"
            ):
                console.print(f"  {row['site_number']:04d}  {row['code']}: {row['detail']}")

        if not diff:
            console.print("\n[dim]Pass --diff to compare against the corpus on disk.[/dim]")
            return

        if not report.changes:
            console.print("\n[green]Corpus unchanged since this database was built.[/green]")
            return

        console.print(f"\n[bold]{len(report.changes)} page(s) differ from the build[/bold]")
        for change in report.changes[:40]:
            console.print(f"  {change.site_number:04d}  {change.kind:9} {change.detail}")

        findings = db_audit.reparse_changed(report.changes)
        flagged = {site: notes for site, notes in findings.items() if notes}
        if flagged:
            console.print("\n[yellow]Re-parsing those pages raises:[/yellow]")
            for site_number, notes in list(flagged.items())[:20]:
                console.print(f"  {site_number:04d}  {'; '.join(sorted(set(notes)))}")
        console.print("\n[dim]Run `matienzo build` to bring the database up to date.[/dim]")
    finally:
        connection.close()


@app.command
def review(
    site: int | None = None,
    *,
    code: str | None = None,
    severity: str = "warn",
    limit: int = 30,
    db: Path | None = None,
) -> None:
    """List pages needing review, or show one in detail.

    Ranked by how badly the parse degraded, not by anomaly count: a page with
    twenty routine warnings is healthier than one with a single lost section.

    Parameters
    ----------
    site
        Show this page's anomalies and the source behind them.
    code
        List only pages carrying this anomaly code.
    severity
        Minimum severity to list: info, warn or error.
    limit
        How many pages to list.
    db
        Read this database instead of the default.
    """
    path = db or config.DB_PATH
    if not path.exists():
        console.print(f"[red]No database at {path}. Run `matienzo build`.[/red]")
        raise SystemExit(1)

    connection = connect(path, read_only=True)
    try:
        if site is not None:
            _review_one(connection, site)
        else:
            _review_list(connection, code, severity, limit)
    finally:
        connection.close()


def _review_one(connection: sqlite3.Connection, site: int) -> None:
    row = connection.execute(
        "SELECT p.confidence, p.min_confidence, p.anomaly_count, p.max_severity, s.name"
        " FROM parse_run p JOIN site s USING (site_number) WHERE p.site_number = ?",
        (site,),
    ).fetchone()
    if row is None:
        console.print(f"[red]Site {site:04d} is not in the database.[/red]")
        raise SystemExit(1)

    console.print(f"[bold]{site:04d}[/bold] {row['name'] or ''}")
    console.print(f"  confidence: {row['confidence']}")
    for anomaly in connection.execute(
        "SELECT code, severity, detail, field_path, excerpt FROM anomaly"
        " WHERE site_number = ? ORDER BY severity DESC, code",
        (site,),
    ):
        colour = {"error": "red", "warn": "yellow", "info": "dim"}[anomaly["severity"]]
        console.print(
            f"  [{colour}]{anomaly['severity']:5}[/{colour}] {anomaly['code']}  {anomaly['detail']}"
        )
        if anomaly["excerpt"]:
            console.print(f"        [dim]{anomaly['excerpt'][:160]}[/dim]")

    override = overrides_module.load_all().get(site)
    if override is None:
        console.print(f"\n[dim]To correct this page, add data/overrides/{site:04d}.toml:[/dim]")
        record = parse_page(config.page_path(site))
        console.print(
            f"[dim]  site = {site}\n"
            f'  applies_to_sha256 = "{record.provenance.content_sha256}"\n'
            f'  author = "you"\n\n'
            f"  [[fix]]\n"
            f'  field_path = "header.area_raw"\n'
            f'  value = "..."\n'
            f'  rationale = "..."[/dim]'
        )
    else:
        console.print(f"\n[dim]Override:[/dim] {override.path.name} by {override.author}")
        for fix in override.fixes:
            console.print(f"  {fix.field_path} = {fix.value!r}  [dim]{fix.rationale}[/dim]")


def _review_list(
    connection: sqlite3.Connection, code: str | None, severity: str, limit: int
) -> None:
    ranks = {"info": 0, "warn": 1, "error": 2}
    if severity not in ranks:
        console.print(f"[red]--severity must be one of: {', '.join(ranks)}[/red]")
        raise SystemExit(2)

    wanted = [name for name, rank in ranks.items() if rank >= ranks[severity]]
    placeholders = ",".join("?" * len(wanted))
    params: list[object] = list(wanted)
    filter_sql = ""
    if code is not None:
        filter_sql = " AND a.code = ?"
        params.append(code)

    rows = connection.execute(
        f"SELECT s.site_number, s.name, p.min_confidence,"
        f" count(*) AS hits, group_concat(DISTINCT a.code) AS codes"
        f" FROM anomaly a JOIN site s USING (site_number)"
        f" JOIN parse_run p USING (site_number)"
        f" WHERE a.severity IN ({placeholders}){filter_sql}"
        f" GROUP BY s.site_number"
        f" ORDER BY p.min_confidence ASC, hits DESC LIMIT ?",
        (*params, limit),
    ).fetchall()

    if not rows:
        console.print("[green]Nothing to review at that severity.[/green]")
        return

    table = Table(title="Review queue", title_justify="left")
    table.add_column("site")
    table.add_column("name", max_width=26, overflow="ellipsis")
    table.add_column("conf", justify="right")
    table.add_column("n", justify="right")
    table.add_column("codes", overflow="fold")
    for row in rows:
        table.add_row(
            f"{row['site_number']:04d}",
            row["name"] or "",
            f"{row['min_confidence']:.2f}",
            str(row["hits"]),
            row["codes"],
        )
    console.print(table)

    total = connection.execute(
        f"SELECT count(DISTINCT site_number) FROM anomaly WHERE severity IN ({placeholders})",
        wanted,
    ).fetchone()[0]
    console.print(f"[dim]{total:,} pages carry an anomaly at {severity} or above.[/dim]")


@app.command
def embed(*, db: Path | None = None, batch: int = 256) -> None:
    """Compute embeddings for any chunks that lack them.

    Incremental: a chunk already carrying a vector is skipped, so re-running
    after a parser change only touches what actually moved.

    Parameters
    ----------
    db
        Embed into this database instead of the default.
    batch
        Chunks per batch.
    """
    path = db or config.DB_PATH
    if not path.exists():
        console.print(f"[red]No database at {path}. Run `matienzo build`.[/red]")
        raise SystemExit(1)

    connection = connect(path)
    try:
        embed_module.load_extension(connection)
        pending = embed_module.pending_chunks(connection)
        if not pending:
            total = connection.execute("SELECT count(*) FROM chunk_vec").fetchone()[0]
            console.print(f"[green]All {total:,} chunks already embedded.[/green]")
            return

        console.print(f"Embedding {len(pending):,} chunk(s) with {embed_module.MODEL_NAME}…")
        started = time.monotonic()
        with console.status("") as status:
            for done in embed_module.embed_pending(connection, pending, batch_size=batch):
                rate = done / max(time.monotonic() - started, 1e-6)
                status.update(f"{done:,}/{len(pending):,}  ({rate:.0f}/s)")
        elapsed = time.monotonic() - started
        console.print(
            f"[green]Embedded[/green] {len(pending):,} chunks in {elapsed:.0f}s "
            f"({len(pending) / elapsed:.0f}/s)"
        )
    except embed_module.EmbeddingsUnavailableError as error:
        console.print(f"[red]{error}[/red]")
        raise SystemExit(1) from error
    finally:
        connection.close()


@app.command
def evaluate(*, db: Path | None = None, at: int = 10) -> None:
    """Score keyword, vector and hybrid retrieval against the eval query set.

    The Phase 6 checkpoint. If hybrid does not beat both single strategies, the
    chunking is wrong rather than the fusion.

    Parameters
    ----------
    db
        Evaluate against this database instead of the default.
    at
        The cut-off for the widest recall column.
    """
    connection = _open(db)
    try:
        if not embed_module.is_available(connection):
            console.print("[red]No embeddings. Run `matienzo embed` first.[/red]")
            raise SystemExit(1)

        queries = evaluate_module.load_queries()
        console.print(f"[dim]{len(queries)} queries[/dim]\n")

        overall = evaluate_module.evaluate(connection, queries, at=at)
        _eval_table("Overall", overall, at)

        for kind, results in evaluate_module.by_kind(connection, queries).items():
            _eval_table(kind, results, at)

        _eval_verdict(overall, at)
    finally:
        connection.close()


def _eval_verdict(results: dict[str, object], at: int) -> None:
    """Say plainly whether fusion is earning its place.

    Compared at every cut-off, not just one: hybrid can trail on recall@1 while
    clearly winning on recall@10, and a verdict from a single number would call
    that a failure.
    """
    hybrid = results["hybrid"]
    singles = [results["keyword"], results["vector"]]
    verdicts = []
    for cut in (1, 5, at):
        best_single = max(s.recall(cut) for s in singles)
        if hybrid.recall(cut) > best_single:
            verdicts.append(f"@{cut} better")
        elif hybrid.recall(cut) == best_single:
            verdicts.append(f"@{cut} level")
        else:
            verdicts.append(f"@{cut} worse")

    console.print(f"\n[dim]Hybrid vs the best single strategy:[/dim] {', '.join(verdicts)}")
    if all(v.endswith("worse") for v in verdicts):
        console.print(
            "[yellow]Hybrid loses at every cut-off. That points at the chunking,"
            " not the fusion.[/yellow]"
        )


def _eval_table(title: str, results: dict[str, object], at: int) -> None:
    table = Table(title=title, title_justify="left")
    table.add_column("strategy")
    table.add_column("recall@1", justify="right")
    table.add_column("recall@5", justify="right")
    table.add_column(f"recall@{at}", justify="right")
    for name in ("keyword", "vector", "hybrid"):
        result = results[name]
        table.add_row(
            name,
            f"{result.recall(1):.0%}",
            f"{result.recall(5):.0%}",
            f"{result.recall(10):.0%}",
        )
    console.print(table)


@app.command
def search(
    query: str,
    *,
    passages: bool = False,
    hybrid: bool = False,
    area: str | None = None,
    site_type: str | None = None,
    min_length: float | None = None,
    min_depth: float | None = None,
    has_survey: bool = False,
    limit: int = 15,
    db: Path | None = None,
) -> None:
    """Search the corpus.

    Parameters
    ----------
    query
        Words to look for. Accents are ignored, so `riano` finds `Riaño`.
    passages
        Show matching paragraphs rather than ranked sites.
    hybrid
        Fuse keyword and semantic rankings. Needs `matienzo embed` to have run.
    area
        Restrict to an area, matched loosely: `--area vega`.
    site_type
        Restrict to `shaft`, `cave`, `dig`, …
    min_length
        Only sites at least this long, in metres.
    min_depth
        Only sites at least this deep, in metres.
    has_survey
        Only sites with a survey attached.
    limit
        How many results to show.
    db
        Search this database instead of the default.
    """
    connection = _open(db)
    filters = search_module.Filters(
        area=area,
        site_type=site_type,
        min_length_m=min_length,
        min_depth_m=min_depth,
        has_survey=has_survey or None,
    )
    try:
        if passages:
            found = search_module.search_passages(connection, query, filters=filters, limit=limit)
            if not found:
                console.print("[yellow]No matches.[/yellow]")
                return
            for passage in found:
                heading = f" — {passage.section_heading}" if passage.section_heading else ""
                console.print(
                    f"[bold]{passage.site_number:04d}[/bold] "
                    f"{passage.site_name or ''}{heading}  [dim]({passage.kind})[/dim]"
                )
                console.print(f"  {passage.snippet or passage.text[:300]}\n")
            return

        hits = search_module.search_sites(
            connection, query, filters=filters, limit=limit, hybrid=hybrid
        )
        if not hits:
            console.print("[yellow]No matches.[/yellow]")
            return
        _hit_table(f"Sites matching {query!r}", hits)
    finally:
        connection.close()


@app.command
def show(site: int, *, db: Path | None = None) -> None:
    """Show everything known about one site.

    Parameters
    ----------
    site
        Site number.
    db
        Read this database instead of the default.
    """
    connection = _open(db)
    try:
        row = connection.execute(
            "SELECT * FROM site_summary WHERE site_number = ?", (site,)
        ).fetchone()
        if row is None:
            console.print(f"[red]Site {site:04d} is not in the database.[/red]")
            raise SystemExit(1)

        console.print(f"[bold]{site:04d}[/bold] {row['name'] or ''}")
        facts = [
            ("area", row["area"]),
            ("type", row["site_type"]),
            ("length", f"{row['length_m']:,.0f} m" if row["length_m"] else None),
            ("depth", f"{row['depth_m']:,.0f} m" if row["depth_m"] else None),
            ("altitude", f"{row['altitude_m']:,.0f} m" if row["altitude_m"] else None),
            (
                "position",
                f"{row['latitude']:.5f}, {row['longitude']:.5f}" if row["latitude"] else None,
            ),
            ("updated", f"{row['update_count']} times, last {row['updated_last']}"),
        ]
        for label, value in facts:
            if value:
                console.print(f"  [dim]{label}:[/dim] {value}")

        aliases = connection.execute(
            "SELECT text, kind FROM site_alias WHERE site_number = ? ORDER BY ordinal", (site,)
        ).fetchall()
        if aliases:
            console.print(
                "  [dim]also known as:[/dim] "
                + "; ".join(f"{a['text']} ({a['kind']})" for a in aliases)
            )

        for member in connection.execute(
            "SELECT cs.name, m.relation FROM system_member m"
            " JOIN cave_system cs USING (system_id) WHERE m.site_number = ?",
            (site,),
        ):
            console.print(f"  [dim]system:[/dim] {member['name']} ({member['relation']})")

        body = connection.execute(
            "SELECT body_text FROM site WHERE site_number = ?", (site,)
        ).fetchone()["body_text"]
        if body:
            console.print(f"\n{body}\n")

        citations = connection.execute(
            "SELECT c.raw FROM site_citation sc JOIN citation c USING (citation_id)"
            " WHERE sc.site_number = ? ORDER BY sc.ordinal",
            (site,),
        ).fetchall()
        if citations:
            console.print("[bold]References[/bold]")
            for citation in citations[:20]:
                console.print(f"  {citation['raw']}")

        refs = connection.execute(
            "SELECT to_site, kind FROM xref WHERE from_site = ? ORDER BY to_site", (site,)
        ).fetchall()
        if refs:
            console.print(
                "\n[dim]refers to:[/dim] " + ", ".join(f"{r['to_site']:04d}" for r in refs)
            )
    finally:
        connection.close()


@app.command
def nearby(site: int, *, radius: float = 500.0, limit: int = 20, db: Path | None = None) -> None:
    """List sites within a radius of another, in metres.

    Parameters
    ----------
    site
        Site number to search around.
    radius
        Radius in metres.
    limit
        How many results to show.
    db
        Read this database instead of the default.
    """
    connection = _open(db)
    try:
        hits = search_module.nearby(connection, site, radius_m=radius, limit=limit)
        if not hits:
            console.print(f"[yellow]Nothing within {radius:.0f} m of {site:04d}.[/yellow]")
            return
        _hit_table(f"Within {radius:.0f} m of {site:04d}", hits)
    finally:
        connection.close()


@app.command
def graph(site: int, *, depth: int = 1, db: Path | None = None) -> None:
    """Show a site's cross-reference neighbourhood.

    Parameters
    ----------
    site
        Site number at the centre.
    depth
        How many hops to follow.
    db
        Read this database instead of the default.
    """
    connection = _open(db)
    try:
        neighbourhood = search_module.neighbourhood(connection, site, depth=depth)
        if not neighbourhood.get(site):
            console.print(f"[yellow]Site {site:04d} has no cross-references.[/yellow]")
            return
        for source, targets in neighbourhood.items():
            name = connection.execute(
                "SELECT name FROM site WHERE site_number = ?", (source,)
            ).fetchone()
            label = (name["name"] if name else None) or ""
            console.print(f"[bold]{source:04d}[/bold] {label}")
            console.print("  " + ", ".join(f"{t:04d}" for t in targets))
    finally:
        connection.close()


def _open(db: Path | None) -> sqlite3.Connection:
    path = db or config.DB_PATH
    if not path.exists():
        console.print(f"[red]No database at {path}. Run `matienzo build`.[/red]")
        raise SystemExit(1)
    return connect(path, read_only=True)


def _hit_table(title: str, hits: list[search_module.SiteHit], score_header: str = "score") -> None:
    table = Table(title=title, title_justify="left")
    # No snippet column. This table answers "which caves"; `--passages` answers
    # "show me the text". Carrying both made every column collapse at the 80
    # columns rich assumes when output is not a terminal.
    table.add_column("site", no_wrap=True)
    table.add_column("name", max_width=30, overflow="ellipsis")
    table.add_column("area", max_width=14, overflow="ellipsis")
    table.add_column("type", max_width=10, overflow="ellipsis")
    table.add_column("length", justify="right", no_wrap=True)
    table.add_column("depth", justify="right", no_wrap=True)
    for hit in hits:
        table.add_row(
            f"{hit.site_number:04d}",
            hit.name or "",
            hit.area or "",
            hit.site_type or "",
            f"{hit.length_m:,.0f} m" if hit.length_m else "",
            f"{hit.depth_m:,.0f} m" if hit.depth_m else "",
        )
    console.print(table)


def _fmt_sites(sites: list[int]) -> str:
    return ", ".join(f"{s:04d}" for s in sites) if sites else "none"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
