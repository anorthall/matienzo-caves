"""Turn parsed records into database rows.

The build always starts from an empty file. Rebuilding rather than migrating is
what keeps a derived artefact trustworthy: there is no partially-migrated state
to reason about, and `rm matienzo.db && matienzo build` is always a valid
recovery.

Loading is two passes. The first writes every site and its parts; the second
resolves things that need the whole corpus in place — whether a cross-reference
target exists, how many sites cite a work, how often a person is named.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import warnings
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from matienzo import config
from matienzo import overrides as overrides_module
from matienzo.anomaly import Severity
from matienzo.chunk import chunk_site
from matienzo.models import Coordinate, ParsedSite, Quantity, QuantityKind, ResourceLink
from matienzo.normalise import areas as areas_vocab
from matienzo.parse import parse_page
from matienzo.parse.citations import citation_key


class UnmappedAreasError(RuntimeError):
    """The corpus contains an area spelling that `areas.toml` does not know.

    Fatal by design. An unknown spelling silently splits a place in two, and
    every aggregate that mentions it is then wrong in a way nothing surfaces.
    """

    def __init__(self, spellings: set[str]) -> None:
        listing = ", ".join(sorted(repr(s) for s in spellings))
        super().__init__(
            f"{len(spellings)} area spelling(s) not in data/vocab/areas.toml: {listing}.\n"
            f"Add each to the canonical entry it belongs to, or as a new area."
        )


def build(connection: sqlite3.Connection, paths: Iterable[Path] | None = None) -> int:
    """Load every page into an empty database. Returns the build id."""
    pages = list(paths) if paths is not None else config.iter_page_paths()
    build_id = _start_build(connection, len(pages))

    vocabulary = areas_vocab.load()
    _insert_areas(connection, vocabulary)

    overrides = overrides_module.load_all()
    records: list[ParsedSite] = []
    applied: list[overrides_module.Applied] = []
    for path in pages:
        record = parse_page(path)
        record, outcome = overrides_module.apply(record, overrides.get(record.site_number))
        if outcome is not None:
            applied.append(outcome)
        records.append(record)
    _report_overrides(applied)

    unmapped = areas_vocab.unmapped(
        {r.header.area_raw for r in records if r.header and r.header.area_raw}, vocabulary
    )
    if unmapped:
        raise UnmappedAreasError(unmapped)

    citations = _insert_citations(connection, records)
    for record in records:
        _insert_site(connection, record, build_id, vocabulary, citations)

    _resolve_cross_references(connection)
    _count_usage(connection)
    _index_for_search(connection, records)

    connection.execute(
        "UPDATE build SET finished_at = ? WHERE build_id = ?",
        (_now(), build_id),
    )
    return build_id


def _report_overrides(applied: list[overrides_module.Applied]) -> None:
    """Warn about overrides that no longer apply.

    A stale override means the upstream page changed under a correction someone
    made deliberately. That needs a human to look again, so it must not pass
    silently — but it also must not stop a build, or a single upstream edit
    would block every rebuild until someone had time to review it.
    """
    for outcome in applied:
        if outcome.status is overrides_module.OverrideStatus.STALE:
            warnings.warn(
                f"override {outcome.override.path.name} not applied: {outcome.detail}",
                stacklevel=2,
            )
        elif outcome.status is overrides_module.OverrideStatus.SUPERSEDED:
            warnings.warn(
                f"override {outcome.override.path.name} is now redundant: {outcome.detail}",
                stacklevel=2,
            )


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _start_build(connection: sqlite3.Connection, page_count: int) -> int:
    cursor = connection.execute(
        "INSERT INTO build (started_at, parser_version, schema_version, page_count, git_sha)"
        " VALUES (?, ?, ?, ?, ?)",
        (_now(), config.PARSER_VERSION, config.SCHEMA_VERSION, page_count, _git_sha()),
    )
    return int(cursor.lastrowid or 0)


def _git_sha() -> str | None:
    """The commit the build ran from, when there is one."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=config.REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None


def _insert_areas(connection: sqlite3.Connection, vocabulary: areas_vocab.AreaVocabulary) -> None:
    for area in vocabulary.areas:
        connection.execute("INSERT INTO area (name, slug) VALUES (?, ?)", (area.name, area.slug))
    for folded, area in vocabulary.by_folded.items():
        connection.execute(
            "INSERT OR IGNORE INTO area_variant (variant, area_id, source)"
            " SELECT ?, area_id, 'corpus' FROM area WHERE slug = ?",
            (folded, area.slug),
        )


def _insert_citations(connection: sqlite3.Connection, records: list[ParsedSite]) -> dict[str, int]:
    """Insert one row per distinct work and return key → citation_id.

    Done before the sites so that `site_citation` and `resource_link` can point
    at real ids. The key includes the qualifier — `anon., 2005b (Easter
    logbook)` and `anon., 2005b (summer logbook)` are different logbooks.
    """
    ids: dict[str, int] = {}
    authors: dict[str, int] = {}

    for record in records:
        if record.footer is None:
            continue
        for citation in record.footer.citations:
            key = citation_key(citation)
            if key in ids:
                continue
            cursor = connection.execute(
                "INSERT INTO citation (citation_key, raw, author_raw, year, disambiguator,"
                " qualifier, qualifier_known, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    citation.raw,
                    citation.author_raw,
                    citation.year,
                    citation.disambiguator,
                    citation.qualifier,
                    int(citation.qualifier_is_known),
                    citation.kind.value,
                ),
            )
            citation_id = int(cursor.lastrowid or 0)
            ids[key] = citation_id

            for ordinal, name in enumerate(citation.authors):
                if name not in authors:
                    author_cursor = connection.execute(
                        "INSERT INTO author (name) VALUES (?)", (name,)
                    )
                    authors[name] = int(author_cursor.lastrowid or 0)
                connection.execute(
                    "INSERT OR IGNORE INTO citation_author (citation_id, author_id, ordinal)"
                    " VALUES (?, ?, ?)",
                    (citation_id, authors[name], ordinal),
                )
    return ids


def _insert_site(
    connection: sqlite3.Connection,
    record: ParsedSite,
    build_id: int,
    vocabulary: areas_vocab.AreaVocabulary,
    citations: dict[str, int],
) -> None:
    area = vocabulary.resolve(record.header.area_raw if record.header else None)
    area_id = _area_id(connection, area.slug) if area else None

    primary = _primary_coordinate(record)
    dates = [u.date for u in record.updated if u.date]

    connection.execute(
        "INSERT INTO site (site_number, name, name_sort, site_type, title_raw, area_id,"
        " area_raw, header_raw, body_text, body_chars, is_minimal, is_stub, footer_raw,"
        " updated_first, updated_last, update_count, length_m, depth_m, altitude_m,"
        " latitude, longitude, easting, northing)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.site_number,
            record.title.name if record.title else None,
            record.title.name_sort if record.title else None,
            record.title.site_type if record.title else None,
            record.title.raw if record.title else "",
            area_id,
            record.header.area_raw if record.header else None,
            record.header.raw if record.header else None,
            record.body.text,
            record.body.char_count,
            int(record.body.is_minimal),
            int(record.is_stub),
            record.footer.raw if record.footer else None,
            min(dates).isoformat() if dates else None,
            max(dates).isoformat() if dates else None,
            len(record.updated),
            _measurement(record, "length"),
            _measurement(record, "depth"),
            primary.altitude_m if primary else None,
            primary.latitude if primary else None,
            primary.longitude if primary else None,
            primary.easting if primary else None,
            primary.northing if primary else None,
        ),
    )

    _insert_provenance(connection, record, build_id)
    _insert_title_parts(connection, record)
    _insert_header_parts(connection, record)
    _insert_updates(connection, record)
    _insert_body_parts(connection, record)
    _insert_footer_parts(connection, record, citations)


def _area_id(connection: sqlite3.Connection, slug: str) -> int | None:
    row = connection.execute("SELECT area_id FROM area WHERE slug = ?", (slug,)).fetchone()
    return int(row["area_id"]) if row else None


def _primary_coordinate(record: ParsedSite) -> Coordinate | None:
    """The coordinate that stands for the site.

    The first one with a real position: multi-entrance pages list the main
    entrance first, and a placeholder should never displace a real fix.
    """
    if record.header is None:
        return None
    for coordinate in record.header.coordinates:
        if coordinate.easting is not None:
            return coordinate
    return record.header.coordinates[0] if record.header.coordinates else None


def _measurement(record: ParsedSite, label: str) -> float | None:
    """The numeric value for a measurement, or None when it is prose.

    Only `numeric` and `compound` kinds produce a number. A prose Length means
    the site's length is accounted for elsewhere — putting anything in this
    column would make `sum(length_m)` double-count a cave system.
    """
    if record.header is None:
        return None
    for quantity in record.header.quantities:
        if quantity.label == label and quantity.kind in (
            QuantityKind.NUMERIC,
            QuantityKind.COMPOUND,
        ):
            return quantity.value_m
    return None


def _insert_provenance(connection: sqlite3.Connection, record: ParsedSite, build_id: int) -> None:
    provenance = record.provenance
    connection.execute(
        "INSERT INTO source_file (site_number, path, content_sha256, byte_size, encoding_used)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            record.site_number,
            provenance.source_path,
            provenance.content_sha256,
            provenance.byte_size,
            provenance.encoding_used,
        ),
    )

    severities = [a.severity for a in provenance.anomalies]
    order = (Severity.INFO, Severity.WARN, Severity.ERROR)
    connection.execute(
        "INSERT INTO parse_run (site_number, build_id, parser_version, content_sha256,"
        " segments_found, confidence, min_confidence, anomaly_count, max_severity)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.site_number,
            build_id,
            provenance.parser_version,
            provenance.content_sha256,
            json.dumps(provenance.segments_found.model_dump()),
            json.dumps(provenance.confidence),
            min(provenance.confidence.values(), default=1.0),
            len(provenance.anomalies),
            max(severities, key=order.index).value if severities else None,
        ),
    )

    connection.executemany(
        "INSERT INTO anomaly (site_number, code, severity, detail, field_path, excerpt,"
        " char_offset) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                a.code.value,
                a.severity.value,
                a.detail,
                a.field_path,
                a.excerpt,
                a.char_offset,
            )
            for a in provenance.anomalies
        ],
    )


def _insert_title_parts(connection: sqlite3.Connection, record: ParsedSite) -> None:
    if record.title is None:
        return
    connection.executemany(
        "INSERT INTO site_alias (site_number, ordinal, text, text_sort, kind, target_site)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                ordinal,
                alias.text,
                areas_vocab.fold(alias.text),
                alias.kind.value,
                alias.site_number,
            )
            for ordinal, alias in enumerate(record.title.aliases)
        ],
    )


def _insert_header_parts(connection: sqlite3.Connection, record: ParsedSite) -> None:
    if record.header is None:
        return

    connection.executemany(
        "INSERT INTO coordinate (site_number, ordinal, raw, label, entrance_site,"
        " coord_system, zone, easting, northing, vn_ref, datum, accuracy_code, altitude_m,"
        " latitude, longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                ordinal,
                c.raw,
                c.label,
                c.entrance_site_number,
                c.system.value,
                c.zone,
                c.easting,
                c.northing,
                c.vn_ref,
                c.datum,
                c.accuracy_code,
                c.altitude_m,
                c.latitude,
                c.longitude,
            )
            for ordinal, c in enumerate(record.header.coordinates)
        ],
    )

    for quantity in record.header.quantities:
        extra: dict[str, Any] = {}
        if quantity.parts:
            extra["parts"] = quantity.parts
        if quantity.prose is not None:
            extra["prose"] = quantity.prose.model_dump()
        connection.execute(
            "INSERT INTO quantity (site_number, label, label_raw, raw, kind, value_m,"
            " min_m, max_m, modifier, unit_stated, extra)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.site_number,
                quantity.label,
                quantity.label_raw,
                quantity.raw,
                quantity.kind.value,
                quantity.value_m,
                quantity.min_m,
                quantity.max_m,
                quantity.modifier.value,
                int(quantity.unit_stated),
                json.dumps(extra) if extra else None,
            ),
        )
        if quantity.prose is not None:
            _insert_system_membership(connection, record, quantity)


def _insert_system_membership(
    connection: sqlite3.Connection, record: ParsedSite, quantity: Quantity
) -> None:
    """Record what a prose measurement says about cave-system membership.

    This is the payoff for modelling measurements with an interpretation kind:
    `Length: included in the Four Valleys System` becomes a real relation rather
    than a discarded string.
    """
    prose = quantity.prose
    if prose is None or not prose.system_name:
        return
    connection.execute("INSERT OR IGNORE INTO cave_system (name) VALUES (?)", (prose.system_name,))
    row = connection.execute(
        "SELECT system_id FROM cave_system WHERE name = ?", (prose.system_name,)
    ).fetchone()
    connection.execute(
        "INSERT OR IGNORE INTO system_member (system_id, site_number, relation, evidence)"
        " VALUES (?, ?, ?, ?)",
        (int(row["system_id"]), record.site_number, prose.relation.value, quantity.raw),
    )


def _insert_updates(connection: sqlite3.Connection, record: ParsedSite) -> None:
    connection.executemany(
        "INSERT INTO update_date (site_number, ordinal, group_index, raw, edited_on,"
        " end_date, precision, attribution, inferred_month, inferred_year)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                ordinal,
                entry.group_index,
                entry.raw,
                entry.date.isoformat() if entry.date else None,
                entry.end_date.isoformat() if entry.end_date else None,
                entry.precision.value,
                entry.attribution,
                int(entry.inferred_month),
                int(entry.inferred_year),
            )
            for ordinal, entry in enumerate(record.updated)
        ],
    )


def _insert_body_parts(connection: sqlite3.Connection, record: ParsedSite) -> None:
    body = record.body
    connection.executemany(
        "INSERT INTO section (site_number, ordinal, heading, heading_kind, canonical,"
        " block_first, block_last) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                s.index,
                s.heading,
                s.heading_kind,
                s.canonical,
                s.block_first,
                s.block_last,
            )
            for s in body.sections
        ],
    )
    connection.executemany(
        "INSERT INTO block (site_number, ordinal, kind, text, section_ordinal)"
        " VALUES (?, ?, ?, ?, ?)",
        [(record.site_number, b.index, b.kind.value, b.text, b.section_index) for b in body.blocks],
    )
    connection.executemany(
        "INSERT INTO body_table (site_number, ordinal, looks_like, headers, rows)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                t.index,
                t.looks_like,
                json.dumps(t.headers),
                json.dumps(t.rows),
            )
            for t in body.tables
        ],
    )
    connection.executemany(
        "INSERT INTO bat_observation (site_number, observed_on, date_raw, fields)"
        " VALUES (?, ?, ?, ?)",
        [
            (
                record.site_number,
                o.date.isoformat() if o.date else None,
                o.date_raw,
                json.dumps(o.fields),
            )
            for o in body.bat_observations
        ],
    )
    connection.executemany(
        "INSERT INTO date_mention (site_number, raw, kind, year, month, day, season,"
        " batch_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                d.raw,
                d.kind,
                d.year,
                d.month,
                d.day,
                d.season.value if d.season else None,
                d.batch_code,
            )
            for d in body.dates
        ],
    )
    connection.executemany(
        "INSERT INTO xref (from_site, to_site, kind, href, fragment, link_text, context,"
        " from_zone) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                record.site_number,
                r.target_site,
                r.kind,
                r.href,
                r.fragment,
                r.link_text,
                r.context,
                r.from_zone,
            )
            for r in body.cross_refs
        ],
    )

    for mention in body.people:
        connection.execute("INSERT OR IGNORE INTO person (name) VALUES (?)", (mention.name_raw,))
        row = connection.execute(
            "SELECT person_id FROM person WHERE name = ?", (mention.name_raw,)
        ).fetchone()
        connection.execute(
            "INSERT INTO person_mention (site_number, person_id, name_raw, evidence,"
            " confidence) VALUES (?, ?, ?, ?, ?)",
            (
                record.site_number,
                int(row["person_id"]),
                mention.name_raw,
                mention.evidence,
                mention.confidence,
            ),
        )


def _insert_footer_parts(
    connection: sqlite3.Connection, record: ParsedSite, citations: dict[str, int]
) -> None:
    if record.footer is None:
        return

    for field in record.footer.fields:
        cursor = connection.execute(
            "INSERT INTO footer_field (site_number, ordinal, label, label_raw,"
            " is_known_label, text, is_empty) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.site_number,
                field.order,
                field.label,
                field.label_raw,
                int(field.is_known_label),
                field.text,
                int(field.is_empty),
            ),
        )
        field_id = int(cursor.lastrowid or 0)
        _insert_links(
            connection, record.site_number, field.links, "footer_field", field_id=field_id
        )

    for ordinal, citation in enumerate(record.footer.citations):
        citation_id = citations.get(citation_key(citation))
        if citation_id is None:
            continue
        connection.execute(
            "INSERT OR IGNORE INTO site_citation (site_number, citation_id, ordinal)"
            " VALUES (?, ?, ?)",
            (record.site_number, citation_id, ordinal),
        )
        _insert_links(
            connection, record.site_number, citation.links, "citation", citation_id=citation_id
        )
        _insert_links(
            connection,
            record.site_number,
            citation.attachments,
            "attachment",
            citation_id=citation_id,
        )


def _insert_links(
    connection: sqlite3.Connection,
    site_number: int,
    links: Iterable[ResourceLink],
    origin: str,
    *,
    field_id: int | None = None,
    citation_id: int | None = None,
) -> None:
    connection.executemany(
        "INSERT INTO resource_link (site_number, href, text, bucket, origin, field_id,"
        " citation_id, target_site, is_range) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                site_number,
                link.href,
                link.text,
                link.bucket.value,
                origin,
                field_id,
                citation_id,
                link.target_site,
                int(link.is_range),
            )
            for link in links
        ],
    )


def _index_for_search(connection: sqlite3.Connection, records: list[ParsedSite]) -> None:
    """Build the chunk table and the document-level search indexes."""
    for record in records:
        for chunk in chunk_site(record):
            connection.execute(
                "INSERT INTO chunk (site_number, ordinal, kind, section_heading,"
                " block_first, block_last, text, n_chars, content_sha256)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.site_number,
                    chunk.ordinal,
                    chunk.kind,
                    chunk.section_heading,
                    chunk.block_first,
                    chunk.block_last,
                    chunk.text,
                    len(chunk.text),
                    chunk.content_sha256,
                ),
            )

    connection.execute(
        "INSERT INTO site_fts (site_number, name, aliases, area, body_text, footer_text)"
        " SELECT s.site_number, coalesce(s.name, ''),"
        "        coalesce((SELECT group_concat(text, ' ') FROM site_alias al"
        "                  WHERE al.site_number = s.site_number), ''),"
        "        coalesce(a.name, ''), s.body_text, coalesce(s.footer_raw, '')"
        " FROM site s LEFT JOIN area a ON a.area_id = s.area_id"
    )
    connection.execute(
        "INSERT INTO name_fts (site_number, name)"
        " SELECT s.site_number,"
        "        coalesce(s.name, '') || ' ' || coalesce(s.name_sort, '') || ' ' ||"
        "        coalesce((SELECT group_concat(text || ' ' || text_sort, ' ')"
        "                  FROM site_alias al WHERE al.site_number = s.site_number), '')"
        " FROM site s"
    )


def _resolve_cross_references(connection: sqlite3.Connection) -> None:
    """Mark which cross-reference targets actually have a page.

    269-odd references point at numbers that were never published or were
    reallocated. They are real references and are kept, not deleted.
    """
    connection.execute(
        "UPDATE xref SET to_site_exists = 1 WHERE to_site IN (SELECT site_number FROM site)"
    )


def _count_usage(connection: sqlite3.Connection) -> None:
    """Fill in the denormalised counts that need the whole corpus loaded."""
    connection.execute(
        "UPDATE citation SET site_count ="
        " (SELECT count(DISTINCT site_number) FROM site_citation sc"
        "  WHERE sc.citation_id = citation.citation_id)"
    )
    connection.execute(
        "UPDATE author SET citation_count ="
        " (SELECT count(*) FROM citation_author ca WHERE ca.author_id = author.author_id)"
    )
    connection.execute(
        "UPDATE person SET mention_count ="
        " (SELECT count(*) FROM person_mention pm WHERE pm.person_id = person.person_id)"
    )


def iter_records(paths: Iterable[Path] | None = None) -> Iterator[ParsedSite]:
    """Parse every page, for callers that want records rather than a database."""
    for path in paths if paths is not None else config.iter_page_paths():
        yield parse_page(path)


__all__ = ["UnmappedAreasError", "build", "iter_records"]
