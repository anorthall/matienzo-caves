"""Named read queries shared by the CLI and (later) the MCP server.

Keeping them here rather than inline in either means the two cannot drift into
answering the same question differently.
"""

from __future__ import annotations

import sqlite3
from typing import Any

STATS: dict[str, str] = {
    "sites": "SELECT count(*) FROM site",
    "  with a name": "SELECT count(*) FROM site WHERE name IS NOT NULL AND name <> '-'",
    "  with an area": "SELECT count(*) FROM site WHERE area_id IS NOT NULL",
    "  with coordinates": "SELECT count(*) FROM site WHERE latitude IS NOT NULL",
    "  with a length": "SELECT count(*) FROM site WHERE length_m IS NOT NULL",
    "  with a depth": "SELECT count(*) FROM site WHERE depth_m IS NOT NULL",
    "  one-line only": "SELECT count(*) FROM site WHERE is_minimal = 1",
    "  stubs": "SELECT count(*) FROM site WHERE is_stub = 1",
    "areas": "SELECT count(*) FROM area",
    "coordinates": "SELECT count(*) FROM coordinate",
    "measurements": "SELECT count(*) FROM quantity",
    "  prose measurements": "SELECT count(*) FROM quantity WHERE kind = 'prose'",
    "update dates": "SELECT count(*) FROM update_date",
    "description blocks": "SELECT count(*) FROM block",
    "sections": "SELECT count(*) FROM section",
    "body tables": "SELECT count(*) FROM body_table",
    "bat observations": "SELECT count(*) FROM bat_observation",
    "date mentions": "SELECT count(*) FROM date_mention",
    "people": "SELECT count(*) FROM person",
    "  mentions": "SELECT count(*) FROM person_mention",
    "cross-references": "SELECT count(*) FROM xref",
    "  to a missing page": "SELECT count(*) FROM xref WHERE to_site_exists = 0",
    "cave systems": "SELECT count(*) FROM cave_system",
    "  memberships": "SELECT count(*) FROM system_member",
    "citations (distinct works)": "SELECT count(*) FROM citation",
    "  uses": "SELECT count(*) FROM site_citation",
    "authors": "SELECT count(*) FROM author",
    "resource links": "SELECT count(*) FROM resource_link",
    "anomalies": "SELECT count(*) FROM anomaly",
    "  errors": "SELECT count(*) FROM anomaly WHERE severity = 'error'",
    "  warnings": "SELECT count(*) FROM anomaly WHERE severity = 'warn'",
}

LONGEST = """
SELECT site_number, name, area, length_m
FROM site_summary WHERE length_m IS NOT NULL
ORDER BY length_m DESC LIMIT ?
"""

DEEPEST = """
SELECT site_number, name, area, depth_m
FROM site_summary WHERE depth_m IS NOT NULL
ORDER BY depth_m DESC LIMIT ?
"""

BY_AREA = """
SELECT a.name AS area, count(*) AS sites,
       sum(s.length_m) AS total_length, max(s.depth_m) AS deepest
FROM site s JOIN area a ON a.area_id = s.area_id
GROUP BY a.area_id ORDER BY sites DESC LIMIT ?
"""

MOST_CITED = """
SELECT c.raw, c.year, c.kind, c.site_count
FROM citation c ORDER BY c.site_count DESC LIMIT ?
"""

HUBS = """
SELECT site_number, name, area, refs_in, refs_out
FROM site_summary ORDER BY refs_in DESC LIMIT ?
"""

SYSTEMS = """
SELECT cs.name, count(*) AS members
FROM cave_system cs JOIN system_member m ON m.system_id = cs.system_id
GROUP BY cs.system_id ORDER BY members DESC
"""


def scalar(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    return int(row[0]) if row else 0


def rows(connection: sqlite3.Connection, sql: str, *params: Any) -> list[sqlite3.Row]:
    return connection.execute(sql, params).fetchall()
