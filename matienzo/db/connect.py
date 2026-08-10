"""Database connections.

The one thing worth knowing here: `sqlite-vec` is a loadable extension and the
system Python on macOS is compiled *without* `enable_load_extension`. This
project pins its own interpreter for that reason; running it on
`/usr/bin/python3` would fail at the vector layer only, several phases in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from matienzo import config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SEARCH_SCHEMA_PATH = Path(__file__).parent / "search_schema.sql"


def connect(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database with the pragmas the schema assumes.

    sqlite-vec is loaded when it is installed. It has to be, even for reads: the
    `chunk_vec` virtual table cannot be opened — or dropped, or even described —
    without the module that defines it.
    """
    target = path or config.DB_PATH
    if read_only:
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(target)

    connection.row_factory = sqlite3.Row
    _load_vector_extension(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        # The loader writes ~200k rows in one transaction; the default sync
        # policy makes that roughly an order of magnitude slower for no benefit
        # on a file that is rebuilt from source in two minutes.
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def _load_vector_extension(connection: sqlite3.Connection) -> None:
    """Load sqlite-vec if present, ignoring its absence.

    The parse and load path does not need vectors, so the core install must keep
    working without the `embed` extra. `matienzo embed` and hybrid search raise
    a clear error of their own when it is genuinely required.
    """
    try:
        from matienzo.embed import load_extension
    except ImportError:
        return
    try:
        load_extension(connection)
    except Exception:
        # Absence is expected, not exceptional: the core install has no vectors.
        return


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the relational schema and the search tables.

    They live in separate files because re-chunking and re-indexing does not
    require re-parsing; keeping them apart makes that separation obvious.
    """
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.executescript(SEARCH_SCHEMA_PATH.read_text(encoding="utf-8"))


@contextmanager
def fresh_database(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Create a new database, replacing any existing one.

    Rebuilding rather than migrating is deliberate: the database is derived, and
    a build that always starts from an empty file cannot accumulate the
    half-migrated state that makes derived data untrustworthy.
    """
    target = path or config.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        candidate = target.with_name(target.name + suffix)
        candidate.unlink(missing_ok=True)

    connection = connect(target)
    try:
        create_schema(connection)
        yield connection
        connection.commit()
    finally:
        connection.close()
