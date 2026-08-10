# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "httpx>=0.28.1",
# ]
# ///
"""Download a numbered range of .htm pages (0001.htm .. 5557.htm) into a folder."""

import argparse
import asyncio
import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urljoin

import httpx

USER_AGENT = "htm-archiver/1.0 (+personal archival copy)"
KNOWN_MISSING_FILE = "known-missing.txt"

SAVED = "saved"
MISSING = "missing"
FAILED = "failed"
OUTCOMES = (SAVED, MISSING, FAILED)


class Progress:
    """Live one-line counter on a terminal, periodic lines when piped to a file."""

    def __init__(self, total: int, every: int = 100) -> None:
        self.total = total
        self.every = every
        self.done = 0
        self.tally = dict.fromkeys(OUTCOMES, 0)
        self.tty = sys.stdout.isatty()

    def update(self, name: str, outcome: str) -> None:
        self.done += 1
        self.tally[outcome] += 1

        # Anomalies get a durable line of their own, above the live counter.
        if outcome in (MISSING, FAILED):
            self.note(f"  {name}  {outcome}")

        if self.tty:
            self._render(name)
        elif self.done % self.every == 0 or self.done == self.total:
            print(f"  {self._counts()}", flush=True)

    def note(self, message: str) -> None:
        """Print a permanent line without leaving the live counter behind."""
        if self.tty:
            print(f"\r{' ' * shutil.get_terminal_size().columns}\r", end="")
        print(message, flush=True)

    def _counts(self) -> str:
        percent = 100 * self.done / self.total if self.total else 100.0
        parts = " ".join(f"{k} {self.tally[k]}" for k in OUTCOMES if self.tally[k])
        return f"[{self.done}/{self.total}] {percent:5.1f}%  {parts}"

    def _render(self, name: str) -> None:
        line = f"{self._counts()}  <- {name}"
        width = shutil.get_terminal_size().columns
        print(f"\r{line[: width - 1]:<{width - 1}}", end="", flush=True)

    def finish(self) -> None:
        if self.tty:
            self._render("done")
            print()


def summarise_ranges(numbers: Iterable[int], limit: int = 12) -> str:
    """Condense [1,2,3,7,9,10] into '0001-0003, 0007, 0009-0010'."""
    ordered = sorted(numbers)
    if not ordered:
        return "none"

    spans: list[tuple[int, int]] = []
    span_start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        spans.append((span_start, previous))
        span_start = previous = number
    spans.append((span_start, previous))

    shown = [f"{lo:04d}" if lo == hi else f"{lo:04d}-{hi:04d}" for lo, hi in spans[:limit]]
    if len(spans) > limit:
        shown.append(f"... (+{len(spans) - limit} more ranges)")
    return ", ".join(shown)


def scan_folder(out_dir: Path, start: int, end: int) -> tuple[list[int], list[int]]:
    """Split the range into pages already saved and pages absent from the folder."""
    present: list[int] = []
    absent: list[int] = []
    for number in range(start, end + 1):
        page = out_dir / f"{number:04d}.htm"
        if page.exists() and page.stat().st_size > 0:
            present.append(number)
        else:
            absent.append(number)
    return present, absent


def load_known_missing(out_dir: Path) -> set[int]:
    """Page numbers a previous run confirmed the server has no page for (404)."""
    log = out_dir / KNOWN_MISSING_FILE
    if not log.exists():
        return set()
    return {int(line) for line in log.read_text().split() if line.strip().isdigit()}


def save_known_missing(out_dir: Path, numbers: set[int]) -> None:
    if not numbers:
        return
    lines = "\n".join(f"{n:04d}" for n in sorted(numbers))
    (out_dir / KNOWN_MISSING_FILE).write_text(lines + "\n")


def write_atomic(dest: Path, payload: bytes) -> None:
    """Write via a temp file so an interrupted run never leaves a partial page."""
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(payload)
    os.replace(tmp, dest)


async def fetch_one(
    client: httpx.AsyncClient,
    base_url: str,
    number: int,
    out_dir: Path,
    delay: float,
    retries: int,
) -> str:
    name = f"{number:04d}.htm"
    dest = out_dir / name
    url = urljoin(base_url, name)

    for attempt in range(retries + 1):
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            if attempt == retries:
                print(f"  {name}: request failed after {retries + 1} tries: {exc}")
                return FAILED
        else:
            if response.status_code == 404:
                return MISSING
            if response.is_success:
                write_atomic(dest, response.content)
                if delay:
                    await asyncio.sleep(delay)
                return SAVED
            if response.status_code < 500 and response.status_code != 429:
                print(f"  {name}: HTTP {response.status_code}")
                return FAILED
            if attempt == retries:
                print(f"  {name}: HTTP {response.status_code} after {retries + 1} tries")
                return FAILED

        await asyncio.sleep(2**attempt)

    return FAILED


async def download(
    base_url: str,
    targets: list[int],
    out_dir: Path,
    concurrency: int,
    delay: float,
    retries: int,
    timeout: float,
) -> tuple[dict[str, int], set[int]]:
    semaphore = asyncio.Semaphore(concurrency)
    progress = Progress(len(targets))
    missing_now: set[int] = set()

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        async def worker(number: int) -> tuple[int, str]:
            async with semaphore:
                return number, await fetch_one(client, base_url, number, out_dir, delay, retries)

        tasks = [asyncio.create_task(worker(n)) for n in targets]
        try:
            for task in asyncio.as_completed(tasks):
                number, outcome = await task
                if outcome == MISSING:
                    missing_now.add(number)
                progress.update(f"{number:04d}.htm", outcome)
        finally:
            progress.finish()

    return progress.tally, missing_now


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url",
        nargs="?",
        default="https://www.matienzocaves.org.uk/descrip/",
        help="Directory URL the pages live under (default: %(default)s)",
    )
    parser.add_argument("-o", "--out-dir", type=Path, default=Path("data/pages"))
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=5557)
    parser.add_argument(
        "-c", "--concurrency", type=int, default=5, help="Parallel requests (default: 5)"
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=float,
        default=0.2,
        help="Seconds to pause after each download (default: 0.2)",
    )
    parser.add_argument(
        "--redo-last",
        type=int,
        default=1,
        help="Re-fetch this many of the highest-numbered pages already on disk, in case "
        "an earlier run was cut off mid-page (default: 1, 0 disables)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Report which pages are absent from the folder and exit without downloading",
    )
    parser.add_argument(
        "--skip-known-missing",
        action="store_true",
        help=f"Don't re-request pages a previous run got a 404 for (see {KNOWN_MISSING_FILE})",
    )
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    base_url = args.base_url if args.base_url.endswith("/") else args.base_url + "/"
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for leftover in out_dir.glob("*.part"):
        leftover.unlink()

    total = args.end - args.start + 1
    present, absent = scan_folder(out_dir, args.start, args.end)
    known_missing = load_known_missing(out_dir)

    print(f"Range {args.start:04d}-{args.end:04d} ({total} pages) in {out_dir}/")
    print(f"  on disk:  {len(present)}")
    print(f"  absent:   {len(absent)}")
    if absent:
        print(f"    {summarise_ranges(absent)}")
    if known_missing:
        confirmed = sorted(known_missing & set(absent))
        print(f"  of those, {len(confirmed)} previously returned 404 from the server")
        if confirmed:
            print(f"    {summarise_ranges(confirmed)}")

    if args.audit:
        return

    targets = list(absent)
    if args.skip_known_missing:
        targets = [n for n in targets if n not in known_missing]
        print(f"  skipping {len(absent) - len(targets)} known-404 pages")

    redo = present[-args.redo_last :] if args.redo_last > 0 else []
    if redo:
        print(f"  re-fetching last {len(redo)} on disk: {summarise_ranges(redo)}")
    targets = sorted(set(targets) | set(redo))

    if not targets:
        print("\nNothing to fetch.")
        return

    print(f"\nFetching {len(targets)} page(s) from {base_url}")
    try:
        tally, missing_now = asyncio.run(
            download(
                base_url,
                targets,
                out_dir,
                args.concurrency,
                args.delay,
                args.retries,
                args.timeout,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted - re-run the same command to resume.")
        raise SystemExit(130)

    # Record 404s for future runs, dropping any number that now has a file on disk.
    on_disk = set(scan_folder(out_dir, args.start, args.end)[0])
    save_known_missing(out_dir, (known_missing | missing_now) - on_disk)

    print("\nDone:")
    for outcome in OUTCOMES:
        print(f"  {outcome:8} {tally[outcome]}")
    if tally[FAILED]:
        print("\nSome pages failed - re-run the same command to retry just those.")


if __name__ == "__main__":
    main()
