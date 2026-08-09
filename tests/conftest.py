"""Shared fixtures.

`tests/fixtures/` holds frozen copies of nominated pages so that byte-level
assertions stay stable even after a re-scrape. Tests that deliberately measure
the *live* corpus read from `pages/` instead and are marked `slow`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def fixture_path() -> Callable[[int], Path]:
    """Resolve a frozen fixture page by site number."""

    def _resolve(site_number: int) -> Path:
        path = FIXTURES / f"{site_number:04d}.htm"
        if not path.exists():
            pytest.fail(f"missing fixture {path.name} — copy it from pages/")
        return path

    return _resolve
