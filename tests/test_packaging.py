"""Guards on the project metadata.

Small, but each one covers a failure that is silent rather than loud.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


@pytest.fixture(scope="module")
def config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_local_group_mirrors_the_optional_extras(config: dict) -> None:
    """`[dependency-groups] local` exists only to keep the extras installed
    through a bare `uv run`, which re-syncs to the default set and has no
    `default-extras`. If the two lists drift, `uv run` quietly reverts to an
    environment without sqlite-vec and the next build dies on `no such module:
    vec0` — a long way from the edit that caused it.
    """
    extras = config["project"]["optional-dependencies"]
    declared = {spec for group in extras.values() for spec in group}
    local = set(config["dependency-groups"]["local"])
    assert local == declared, (
        "pyproject.toml: [dependency-groups] local must list exactly the "
        "packages in [project.optional-dependencies]"
    )


def test_the_local_group_is_installed_by_default(config: dict) -> None:
    assert "local" in config["tool"]["uv"]["default-groups"]


def test_the_pinned_interpreter_satisfies_the_declared_floor(config: dict) -> None:
    """`.python-version` drives what uv builds the venv with; `requires-python`
    is what consumers see. A venv below the floor fails at install time with a
    resolver error rather than anything explanatory."""
    pinned = (PYPROJECT.parent / ".python-version").read_text().strip()
    floor = config["project"]["requires-python"].lstrip(">=").strip()

    pinned_parts = tuple(int(p) for p in pinned.split("."))
    floor_parts = tuple(int(p) for p in floor.split("."))
    assert pinned_parts >= floor_parts[: len(pinned_parts)], (
        f".python-version is {pinned} but requires-python is >={floor}"
    )


def test_the_console_scripts_point_at_real_entry_points(config: dict) -> None:
    import importlib

    for target in config["project"]["scripts"].values():
        module_name, _, function = target.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, function)), target
