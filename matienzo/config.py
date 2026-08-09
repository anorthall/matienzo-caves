"""Paths and version stamps shared across the pipeline."""

from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
PAGES_DIR: Final = REPO_ROOT / "pages"
DATA_DIR: Final = REPO_ROOT / "data"
VOCAB_DIR: Final = DATA_DIR / "vocab"
OVERRIDES_DIR: Final = DATA_DIR / "overrides"
DB_PATH: Final = REPO_ROOT / "matienzo.db"

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
    """`pages/` is absent or empty.

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
