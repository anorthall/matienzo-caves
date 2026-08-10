"""Links back to the pages the corpus was scraped from.

There is no URL column anywhere in the schema, and there should not be: a site's
public URL is a total function of its number, nothing filters or sorts on it,
and a column would be 5,557 copies of a format string regenerated on every
build. The schema's rule is that a column earns its place by being filtered,
joined, sorted or aggregated on; a URL is none of those.

`resource_link.href` is the harder half. Those 38,501 values are stored exactly
as the source page wrote them, which means `../surveys/x.pdf`, `/photos/y.jpg`
and already-absolute `http://…` all appear in the same column. Only `urljoin`
resolves all three; string concatenation silently produces a broken link for two
of them.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urljoin

#: The directory every site page lives in, and the base every relative `href`
#: in the corpus was written against. `scripts/download_htm.py` fetched the
#: corpus from here; `tests/test_links.py` asserts the two have not drifted.
SITE_BASE: Final = "https://www.matienzocaves.org.uk/descrip/"


def site_url(site_number: int) -> str:
    """The public page for a site.

    Zero-padded to four digits, matching `config.page_path` and the filenames in
    `pages/` — the padding is not cosmetic, an unpadded number 404s.
    """
    return f"{SITE_BASE}{site_number:04d}.htm"


def resource_url(href: str) -> str:
    """Resolve a stored `resource_link.href` to something a browser can follow.

    Hrefs are relative to `/descrip/`, the directory of the page they were
    scraped from — not to the site root. `urljoin` handles that, and passes
    absolute URLs through unchanged.
    """
    return urljoin(SITE_BASE, href)
