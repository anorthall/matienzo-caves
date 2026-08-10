"""Paths and version stamps shared across the pipeline.

The two database paths read the environment because a container mounts the
corpus wherever its image layout puts it, which the repo layout cannot predict.
They are still resolved once, at import, and still plain module constants — a
typo in the environment then fails when the process starts rather than when the
first query runs. The CLI's `--db` flag still wins over both, so precedence
reads flag, then environment, then the repo default.
"""

import os
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DATA_DIR: Final = REPO_ROOT / "data"
PAGES_DIR: Final = DATA_DIR / "pages"
VOCAB_DIR: Final = DATA_DIR / "vocab"
OVERRIDES_DIR: Final = DATA_DIR / "overrides"
DB_PATH: Final = Path(os.environ.get("MATIENZO_DB") or REPO_ROOT / "matienzo.db")

#: Conversations, rate-limit buckets and the spend ledger. Deliberately a
#: separate file: it is the only thing the portal writes, which is what keeps
#: `DB_PATH` open read-only everywhere and disposable in the way the build
#: pipeline assumes.
SESSIONS_DB_PATH: Final = Path(os.environ.get("MATIENZO_SESSIONS_DB") or REPO_ROOT / "sessions.db")

# Bump when a parser change alters ParsedSite output. Golden files and the
# `applies_to_sha256` staleness check both key off this.
PARSER_VERSION: Final = "0.1.0"
SCHEMA_VERSION: Final = 1

SITE_NUMBER_MIN: Final = 1
SITE_NUMBER_MAX: Final = 5557


def page_path(site_number: int) -> Path:
    """Path to a site's source page. Site numbers are zero-padded to four digits."""
    return PAGES_DIR / f"{site_number:04d}.htm"


class CorpusMissingError(RuntimeError):
    """`data/pages/` is absent or empty.

    Raised rather than returning an empty list, so that a missing corpus fails
    loudly instead of every audit reporting a cheerful "0 pages" and exiting
    zero.
    """

    def __init__(self) -> None:
        super().__init__(
            f"No pages found in {PAGES_DIR}. The corpus is committed, so this "
            f"usually means a partial checkout; otherwise re-fetch it with:\n"
            f"    uv run download_htm.py"
        )


def iter_page_paths() -> list[Path]:
    """Every source page, in site-number order.

    Site numbers are *not* dense — the upstream site has gaps — so callers must
    never assume `paths[i]` corresponds to site `i + 1`.
    """
    paths = sorted(PAGES_DIR.glob("[0-9][0-9][0-9][0-9].htm"))
    if not paths:
        raise CorpusMissingError
    return paths
