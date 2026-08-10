# The portal, with everything it needs baked in.
#
# Three things are built rather than mounted, each for a specific reason.
#
# The corpus, because `matienzo.db` is a derived artefact rebuilt from data/ in
# seconds — baking it makes the image reproducible and immutable, which a mounted
# volume would not be. Note the checkpoint after building it: SQLite in WAL mode
# needs a `-shm` file, a read-only connection cannot create one, and the portal
# opens the corpus read-only everywhere. Shipping a WAL database would fail at
# every request with `unable to open database file`, a message that says nothing
# about WAL. `journal_mode = DELETE` leaves a single self-contained file.
#
# The embedding model, because fastembed downloads ~130 MB from HuggingFace on
# first use. A container that has to reach the internet before it can answer its
# first question is a container that fails on a locked-down VM, at the worst
# possible moment, in the retrieval layer.
#
# The SPA, because a wheel that carries its own front end has no second deploy
# step and no way for the two to be out of step with each other.

# ── The front end ────────────────────────────────────────────────────────
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# vite.config.ts writes into ../matienzo/web/static, so give it somewhere to go.
RUN mkdir -p /matienzo/web/static && \
    sed -i 's#\.\./matienzo/web/static#/matienzo/web/static#' vite.config.ts && \
    npm run build

# ── The corpus ───────────────────────────────────────────────────────────
FROM python:3.14-slim AS corpus

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV HF_HOME=/opt/models \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY matienzo/ ./matienzo/
RUN uv sync --frozen --no-dev --extra embed

COPY data/ ./data/
RUN uv run --no-dev --extra embed matienzo build && \
    uv run --no-dev --extra embed matienzo embed && \
    uv run --no-dev python -c "\
import sqlite3; \
c = sqlite3.connect('matienzo.db'); \
c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); \
c.execute('PRAGMA journal_mode = DELETE'); \
c.close()" && \
    rm -f matienzo.db-wal matienzo.db-shm

# ── Runtime ──────────────────────────────────────────────────────────────
FROM python:3.14-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/opt/models \
    # ONNX will otherwise take every core it can see, which makes tail latency
    # worse rather than better once several questions are in flight.
    OMP_NUM_THREADS=2 \
    MATIENZO_DB=/srv/matienzo/matienzo.db \
    MATIENZO_SESSIONS_DB=/var/lib/matienzo/sessions.db

WORKDIR /srv/matienzo

COPY pyproject.toml uv.lock README.md ./
COPY matienzo/ ./matienzo/
RUN uv sync --frozen --no-dev --extra web --extra embed

COPY --from=frontend /matienzo/web/static/ ./matienzo/web/static/
COPY --from=corpus /src/matienzo.db ./matienzo.db
COPY --from=corpus /opt/models /opt/models

# Unprivileged, and owning only the one directory it writes to.
RUN useradd --system --uid 10001 --home /srv/matienzo matienzo && \
    mkdir -p /var/lib/matienzo && \
    chown matienzo:matienzo /var/lib/matienzo && \
    chmod -R a+rX /opt/models /srv/matienzo
USER matienzo

VOLUME ["/var/lib/matienzo"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "\
import sys, urllib.request, json; \
body = json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)); \
sys.exit(0 if body.get('ok') else 1)"

CMD ["uv", "run", "--no-dev", "--extra", "web", "--extra", "embed", \
     "uvicorn", "matienzo.web.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
