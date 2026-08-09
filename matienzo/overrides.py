"""Per-site corrections, applied to parsed records before they reach the database.

The whole point is where a fix *lives*. Corrections are TOML files under
`data/overrides/`, applied to the `ParsedSite` object between parsing and
loading. Three things follow from that, and none of them would hold if fixes
were database writes:

- `rm matienzo.db && matienzo build` reproduces them exactly.
- They are re-validated by the same Pydantic models as parser output, so a
  correction cannot introduce a shape the rest of the pipeline rejects.
- They are reviewable in `git diff` like any other source.

Each override records the SHA-256 of the page it was written against. If the
upstream page changes, the override is **not** applied — it is marked stale and
the review item reopens. Silently re-applying a correction to changed source is
how you end up with data that is wrong and that nothing flags.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from matienzo import config
from matienzo.models import ParsedSite


class OverrideStatus(StrEnum):
    ACTIVE = "active"
    """Applied. The page still hashes to what the override was written for."""
    STALE = "stale"
    """The page changed underneath it. Not applied; needs a human."""
    SUPERSEDED = "superseded"
    """The parser now produces this value unaided. Safe to delete."""


@dataclass(frozen=True, slots=True)
class Fix:
    field_path: str
    """Dotted path into the record, e.g. `title.site_number`."""
    value: Any
    rationale: str


@dataclass(frozen=True, slots=True)
class Override:
    site_number: int
    applies_to_sha256: str
    author: str
    fixes: tuple[Fix, ...]
    path: Path
    reviewed_by: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Applied:
    override: Override
    status: OverrideStatus
    detail: str = ""


def load_all(directory: Path | None = None) -> dict[int, Override]:
    """Read every override file, keyed by site number."""
    source = directory or config.OVERRIDES_DIR
    if not source.exists():
        return {}

    overrides: dict[int, Override] = {}
    for path in sorted(source.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        site_number = int(data.get("site", path.stem))
        overrides[site_number] = Override(
            site_number=site_number,
            applies_to_sha256=data["applies_to_sha256"],
            author=data.get("author", "unknown"),
            reviewed_by=data.get("reviewed_by"),
            note=data.get("note"),
            fixes=tuple(
                Fix(
                    field_path=fix["field_path"],
                    value=fix["value"],
                    rationale=fix.get("rationale", ""),
                )
                for fix in data.get("fix", ())
            ),
            path=path,
        )
    return overrides


def apply(record: ParsedSite, override: Override | None) -> tuple[ParsedSite, Applied | None]:
    """Apply an override to a parsed record, if it still matches the source.

    Returns the record — corrected or untouched — and what happened. The record
    is re-validated on the way out, so an override that would produce an invalid
    shape fails here rather than in the database.
    """
    if override is None:
        return record, None

    if override.applies_to_sha256 != record.provenance.content_sha256:
        return record, Applied(
            override,
            OverrideStatus.STALE,
            f"page now hashes to {record.provenance.content_sha256[:12]}, "
            f"override was written for {override.applies_to_sha256[:12]}",
        )

    data = record.model_dump()
    changed = False
    already_right: list[str] = []

    for fix in override.fixes:
        current = _read(data, fix.field_path)
        if current == fix.value:
            already_right.append(fix.field_path)
            continue
        _write(data, fix.field_path, fix.value)
        changed = True

    if not changed:
        return record, Applied(
            override,
            OverrideStatus.SUPERSEDED,
            "the parser already produces every value this override sets",
        )

    corrected = ParsedSite.model_validate(data)
    detail = f"{len(override.fixes) - len(already_right)} field(s) corrected"
    if already_right:
        detail += f"; {len(already_right)} already correct"
    return corrected, Applied(override, OverrideStatus.ACTIVE, detail)


def _read(data: dict[str, Any], path: str) -> Any:
    cursor: Any = data
    for part in _segments(path):
        if cursor is None:
            return None
        cursor = cursor[part] if isinstance(part, int) else cursor.get(part)
    return cursor


def _write(data: dict[str, Any], path: str, value: Any) -> None:
    segments = _segments(path)
    cursor: Any = data
    for part in segments[:-1]:
        cursor = cursor[part]
    cursor[segments[-1]] = value


def _segments(path: str) -> list[str | int]:
    """Split `header.coordinates[1].label` into ['header','coordinates',1,'label']."""
    parts: list[str | int] = []
    for chunk in path.split("."):
        name, _, rest = chunk.partition("[")
        if name:
            parts.append(name)
        while rest:
            index, _, rest = rest.partition("]")
            if index:
                parts.append(int(index))
            rest = rest.lstrip("[")
    return parts


def write_template(
    record: ParsedSite, field_path: str, value: Any, rationale: str, author: str
) -> str:
    """Render an override file for a record. Used by `matienzo review`."""
    return (
        f"site = {record.site_number}\n"
        f'applies_to_sha256 = "{record.provenance.content_sha256}"\n'
        f'author = "{author}"\n'
        f"\n"
        f"[[fix]]\n"
        f'field_path = "{field_path}"\n'
        f"value = {value!r}\n".replace("'", '"')
        + f'rationale = "{rationale}"\n'
    )
