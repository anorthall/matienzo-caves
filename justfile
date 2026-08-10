# Every recipe goes through `uv run`, so they use the versions pinned in
# uv.lock — the same ones pre-commit and CI use. Running the tools directly
# would pick up whatever happens to be on PATH.

# List the available recipes.
default:
    @just --list

# Reformat the code in place.
format:
    uv run ruff format

# Type-check in strict mode. Scope is `files` in pyproject.toml, not this line.
mypy:
    uv run mypy

# Everything the CI checks, without changing any files.
lint:
    uv run ruff check
    uv run ruff format --check
    uv run mypy

# Run the test suite. 32 tests skip themselves without a built matienzo.db.
test:
    uv run pytest

# Lint and test — what to run before pushing.
check: lint test
