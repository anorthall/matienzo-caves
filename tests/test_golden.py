"""Golden-file tests over the nominated fixtures.

Each fixture is parsed and compared against a committed JSON snapshot of the
whole `ParsedSite`. The point is not that JSON comparison is clever — it is that
the *diff* becomes the review. A parser change shows up in `git diff` as exactly
which fields on exactly which pages moved, before anyone reads a line of the
implementation.

Regenerate after an intended change with::

    uv run pytest tests/test_golden.py --update-goldens

and read the diff before committing it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from matienzo.models import ParsedSite
from matienzo.parse import parse_page

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def fixture_sites() -> list[int]:
    return sorted(int(p.stem) for p in FIXTURES.glob("[0-9][0-9][0-9][0-9].htm"))


def golden_path(site_number: int) -> Path:
    return GOLDEN / f"{site_number:04d}.json"


def render(record: ParsedSite) -> str:
    """Stable JSON for a parsed record.

    Three fields are massaged so the diff stays readable, which is the entire
    value of a golden:

    - `source_path` differs between a fixture run and a corpus run.
    - `parser_version` would rewrite all 67 files on every bump, burying the
      changes that matter.
    - `body.raw_html` is a verbatim copy of the input. Keeping it inflated the
      goldens to 4.9 MB and buried every real change in a wall of markup; it is
      replaced by its SHA-256, which still fails loudly if segmentation moves
      the body boundary.
    """
    data = json.loads(record.model_dump_json())

    provenance = data.get("provenance", {})
    provenance.pop("source_path", None)
    provenance.pop("parser_version", None)

    body = data.get("body", {})
    if "raw_html" in body:
        digest = hashlib.sha256(body["raw_html"].encode()).hexdigest()[:16]
        body["raw_html"] = f"sha256:{digest} ({len(body['raw_html'])} chars)"

    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("site", fixture_sites())
def test_matches_golden(site: int, request: pytest.FixtureRequest) -> None:
    rendered = render(parse_page(FIXTURES / f"{site:04d}.htm"))
    path = golden_path(site)

    if request.config.getoption("--update-goldens"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        pytest.skip(f"regenerated {path.name}")

    if not path.exists():
        pytest.fail(f"no golden for {site:04d}; run pytest --update-goldens")

    expected = path.read_text(encoding="utf-8")
    assert rendered == expected, f"parsed output for {site:04d} changed"


@pytest.mark.parametrize("site", fixture_sites())
def test_round_trips_through_json(site: int) -> None:
    """A model that cannot survive `model_dump_json` cannot be a golden."""
    record = parse_page(FIXTURES / f"{site:04d}.htm")
    assert ParsedSite.model_validate(json.loads(record.model_dump_json())) == record


def test_every_fixture_has_a_golden() -> None:
    missing = [s for s in fixture_sites() if not golden_path(s).exists()]
    assert missing == [], "run pytest --update-goldens"


def test_no_orphaned_goldens() -> None:
    """A golden with no fixture is a fixture someone deleted without noticing."""
    sites = set(fixture_sites())
    orphans = [p.name for p in GOLDEN.glob("*.json") if int(p.stem) not in sites]
    assert orphans == []
