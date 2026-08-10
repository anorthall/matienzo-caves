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

# ── Corpus ───────────────────────────────────────────────────────────────

# The build is seconds; the embed is too after the first run, because vectors
# are carried across a rebuild by content hash.
# Rebuild matienzo.db and its embeddings.
corpus:
    uv run --extra embed matienzo build
    uv run --extra embed matienzo embed

# ── Portal ───────────────────────────────────────────────────────────────

# Without ANTHROPIC_API_KEY it still starts and serves search — that is a
# supported mode, not a broken one.
# Run the portal at http://127.0.0.1:8000.
web:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        echo "ANTHROPIC_API_KEY is unset — starting in search-only mode." >&2
    elif [ -z "${MATIENZO_IP_SALT:-}" ]; then
        echo "MATIENZO_IP_SALT is unset; using a throwaway one for this run." >&2
        export MATIENZO_IP_SALT="dev-$(date +%s)"
    fi
    uv run --extra web --extra embed matienzo-web

# Install the front-end toolchain. Needed once, and after package.json changes.
frontend-install:
    cd frontend && npm install

# Proxies /api to a `just web` on :8000, so run both — this in one terminal,
# `just web` in another.
# Vite dev server with hot reload.
frontend-dev: frontend-install
    cd frontend && npm run dev

# Build the SPA into matienzo/web/static/, where the portal serves it from.
frontend-build: frontend-install
    cd frontend && npm run build

# Type-check the front end. `frontend-build` does this too; this skips the build.
frontend-check: frontend-install
    cd frontend && npm run typecheck

# ── Deployment ───────────────────────────────────────────────────────────

# Build the portal image: SPA, corpus and embedding model all baked in.
docker-build:
    docker build -t matienzo-portal .

# Reads ANTHROPIC_API_KEY and MATIENZO_IP_SALT from the environment;
# sessions.db is a named volume, so it survives a restart.
# Run the image locally on :8000.
docker-run: docker-build
    docker run --rm -it \
        -p 8000:8000 \
        -e ANTHROPIC_API_KEY \
        -e MATIENZO_IP_SALT \
        -v matienzo-sessions:/var/lib/matienzo \
        matienzo-portal

# Measures time-to-first-token, which is the only way to catch a reverse proxy
# buffering the stream — the answer still arrives, so nothing errors or logs.
# Check a *deployed* portal, by URL.
smoke url:
    uv run deploy/smoke.py {{url}}
