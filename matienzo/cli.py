"""The `matienzo` command line."""

from __future__ import annotations

import collections

import cyclopts
from rich.console import Console
from rich.table import Table

from matienzo import __version__, config
from matienzo.anomaly import AnomalyRecorder, Severity
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


def _fmt_sites(sites: list[int]) -> str:
    return ", ".join(f"{s:04d}" for s in sites) if sites else "none"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
