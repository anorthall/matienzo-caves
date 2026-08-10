"""Local embeddings for semantic search.

`BAAI/bge-small-en-v1.5` via fastembed: 384 dimensions, ~130 MB of ONNX, no
PyTorch. fastembed rather than sentence-transformers because the latter pulls
about 2.5 GB of Torch to do the same job, and its wheels lag new CPython
releases — which is exactly the friction this project pinned an interpreter to
avoid.

Embeddings are cached by the SHA-256 of the chunk text. Re-running after a
parser change re-embeds only the chunks whose text actually moved, which is
what makes it reasonable to rebuild often.

The model is English and the corpus is English prose full of Spanish proper
nouns. That is fine for retrieval — the place names carry through as tokens
either way — but it is why keyword search stays a first-class retriever rather
than a fallback: an exact name match is something BM25 does better.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

#: Rows per insert batch. Large enough to amortise the transaction, small
#: enough that progress is visible on a 13k-chunk corpus.
BATCH_SIZE = 256

#: bge models are trained with an instruction prefix on the *query* side only.
#: Omitting it costs a few points of retrieval quality; adding it to documents
#: as well would cost more.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbeddingsUnavailableError(RuntimeError):
    """fastembed or sqlite-vec is not installed.

    Raised rather than silently degrading to keyword-only search: a hybrid
    search that quietly stopped being hybrid would look like it was working.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(
            f"Semantic search needs the optional dependencies: {cause}\n    uv sync --extra embed"
        )


@dataclass(frozen=True, slots=True)
class Pending:
    chunk_id: int
    text: str


@cache
def _model() -> Any:
    """The embedding model, loaded once. Typed `Any` because fastembed cannot be
    imported when the optional extra is absent."""
    try:
        from fastembed import TextEmbedding
    except ImportError as error:  # pragma: no cover - exercised by the extra
        raise EmbeddingsUnavailableError(error) from error
    return TextEmbedding(MODEL_NAME)


def load_extension(connection: sqlite3.Connection) -> None:
    """Load sqlite-vec into a connection.

    The system Python on macOS is built without `enable_load_extension`, which
    is why this project pins its own interpreter. If that ever regresses it
    fails here rather than at query time.
    """
    try:
        import sqlite_vec
    except ImportError as error:  # pragma: no cover - exercised by the extra
        raise EmbeddingsUnavailableError(error) from error

    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed documents."""
    return [vector.tolist() for vector in _model().embed(list(texts))]


def embed_query(query: str) -> list[float]:
    """Embed a search query, with the instruction prefix bge expects."""
    return next(iter(_model().query_embed([query]))).tolist()


def pending_chunks(connection: sqlite3.Connection) -> list[Pending]:
    """Chunks with no vector yet.

    Keyed on chunk id rather than on the hash, so a chunk whose text is
    identical to another's still gets its own row — the vec0 table is joined
    back by id.
    """
    rows = connection.execute(
        "SELECT c.chunk_id, c.text FROM chunk c"
        " WHERE NOT EXISTS (SELECT 1 FROM chunk_vec v WHERE v.chunk_id = c.chunk_id)"
        " ORDER BY c.chunk_id"
    ).fetchall()
    return [Pending(chunk_id=row["chunk_id"], text=row["text"]) for row in rows]


def embed_pending(
    connection: sqlite3.Connection,
    pending: Iterable[Pending],
    *,
    batch_size: int = BATCH_SIZE,
) -> Iterator[int]:
    """Embed and store chunks, yielding the running count after each batch."""
    done = 0
    for batch in _batched(list(pending), batch_size):
        vectors = embed_texts([item.text for item in batch])
        connection.executemany(
            "INSERT INTO chunk_vec (chunk_id, embedding, site_number, kind)"
            " SELECT ?, ?, c.site_number, c.kind FROM chunk c WHERE c.chunk_id = ?",
            [
                (item.chunk_id, struct.pack(f"{DIMENSIONS}f", *vector), item.chunk_id)
                for item, vector in zip(batch, vectors, strict=True)
            ],
        )
        connection.commit()
        done += len(batch)
        yield done


def _batched(items: list[Pending], size: int) -> Iterator[list[Pending]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def is_available(connection: sqlite3.Connection) -> bool:
    """Whether this database has vectors to search."""
    try:
        row = connection.execute("SELECT count(*) FROM chunk_vec").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0])
