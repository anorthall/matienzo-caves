"""Compare the database against the corpus on disk.

The upstream site is live and hand-edited, so `pages/` will drift from what the
parsers were written against. Without this, a changed count after a re-scrape is
indistinguishable from a parser regression — and the wrong diagnosis is the
expensive one.

Every parsed record carries the SHA-256 of the bytes it came from, so the
comparison is exact rather than heuristic.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from matienzo import config
from matienzo.parse import parse_page


@dataclass(frozen=True, slots=True)
class Change:
    site_number: int
    kind: str
    """`new`, `changed` or `vanished`."""
    detail: str = ""


@dataclass
class AuditReport:
    changes: list[Change] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)
    new_anomaly_codes: set[str] = field(default_factory=set)

    @property
    def is_clean(self) -> bool:
        return not self.changes and not self.new_anomaly_codes


def corpus_diff(connection: sqlite3.Connection) -> list[Change]:
    """Which pages are new, changed or gone since the database was built."""
    recorded = {
        int(row["site_number"]): row["content_sha256"]
        for row in connection.execute("SELECT site_number, content_sha256 FROM source_file")
    }

    changes: list[Change] = []
    on_disk: set[int] = set()

    for path in config.iter_page_paths():
        site_number = int(path.stem)
        on_disk.add(site_number)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        if site_number not in recorded:
            changes.append(Change(site_number, "new"))
        elif recorded[site_number] != digest:
            changes.append(
                Change(
                    site_number,
                    "changed",
                    f"{recorded[site_number][:12]} → {digest[:12]}",
                )
            )

    changes.extend(
        Change(site_number, "vanished") for site_number in sorted(recorded.keys() - on_disk)
    )
    return sorted(changes, key=lambda c: (c.kind, c.site_number))


def anomaly_codes(connection: sqlite3.Connection) -> set[str]:
    return {row["code"] for row in connection.execute("SELECT DISTINCT code FROM anomaly")}


def reparse_changed(changes: list[Change]) -> dict[int, list[str]]:
    """Re-parse the new and changed pages and report what each now says.

    Only the affected pages, so this stays fast enough to run after every
    re-scrape rather than being something nobody bothers with.
    """
    findings: dict[int, list[str]] = {}
    for change in changes:
        if change.kind == "vanished":
            continue
        record = parse_page(config.page_path(change.site_number))
        notes = [
            f"{a.severity.value}: {a.code.value}"
            for a in record.provenance.anomalies
            if a.severity.value in {"warn", "error"}
        ]
        findings[change.site_number] = notes
    return findings


def audit(connection: sqlite3.Connection) -> AuditReport:
    report = AuditReport()
    report.changes = corpus_diff(connection)
    report.metrics = {
        "sites": _count(connection, "site"),
        "citations": _count(connection, "site_citation"),
        "cross_references": _count(connection, "xref"),
        "update_dates": _count(connection, "update_date"),
        "coordinates": _count(connection, "coordinate"),
        "anomalies": _count(connection, "anomaly"),
        "errors": _scalar(connection, "SELECT count(*) FROM anomaly WHERE severity = 'error'"),
    }
    return report


def _count(connection: sqlite3.Connection, table: str) -> int:
    return _scalar(connection, f"SELECT count(*) FROM {table}")


def _scalar(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    return int(row[0]) if row else 0
