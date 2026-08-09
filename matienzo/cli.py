"""The `matienzo` command line."""

from __future__ import annotations

import collections

import cyclopts
from rich.console import Console
from rich.table import Table

from matienzo import __version__, config
from matienzo.anomaly import AnomalyRecorder, Severity
from matienzo.decode import Encoding, read_document
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
        body_chars.append(len(" ".join(segments.body.text.split())))

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


def _fmt_sites(sites: list[int]) -> str:
    return ", ".join(f"{s:04d}" for s in sites) if sites else "none"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
