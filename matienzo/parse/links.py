"""Classify the hrefs found on a page.

Roughly 29,700 real anchors across the corpus fall into a small number of
buckets. Getting them sorted matters for two reasons: the footer's picture and
survey fields are only meaningful once you know what a link points at, and the
gallery paths (`../entpics/NNNN.htm`) embed a site number, which makes them
edges in the cross-reference graph.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from matienzo.models import LinkBucket, ResourceLink

#: Paths that appear on every page and carry no information about the site.
#: The accuracy-code PDF is the exception that proves the rule — the *link* is
#: boilerplate but its link *text* is the accuracy code, which is real data, so
#: it is read in `parse.header` before ever reaching here.
BOILERPLATE_RE = re.compile(
    r"style-sheet\.css|SiteCoordinatesAccuracy\.pdf|Leaflet-latest\.html|logbook\.php",
    re.IGNORECASE,
)

#: A gallery or sibling page whose filename encodes a site number, optionally
#: with a range (`1930-2011e.htm`) or an initials suffix (`4271-jc.htm`).
SITE_FILE_RE = re.compile(r"(?P<site>\d{4})(?P<suffix>-[\w-]+)?\.htm$", re.IGNORECASE)
RANGE_SUFFIX_RE = re.compile(r"^-\d{4}", re.IGNORECASE)

_PATH_BUCKETS: tuple[tuple[re.Pattern[str], LinkBucket], ...] = (
    (re.compile(r"/logbook/logbook-pdfs/", re.I), LinkBucket.LOGBOOK_PDF),
    (re.compile(r"/history/scanned-pubs/", re.I), LinkBucket.SCANNED_PUB),
    (re.compile(r"/entpics/", re.I), LinkBucket.ENTRANCE_PICS),
    (re.compile(r"/ugpics/", re.I), LinkBucket.UNDERGROUND_PICS),
    (re.compile(r"/miscpics/", re.I), LinkBucket.MISC_PICS),
    (re.compile(r"/rose-diags/", re.I), LinkBucket.ROSE_DIAGRAM),
    (re.compile(r"/cantab/", re.I), LinkBucket.CANTAB),
    (re.compile(r"/science/", re.I), LinkBucket.SCIENCE),
    (re.compile(r"/diving/", re.I), LinkBucket.DIVING),
    (re.compile(r"/miscdocs/", re.I), LinkBucket.MISC_DOC),
    (re.compile(r"/videos?/", re.I), LinkBucket.VIDEO_FILE),
)

_SURVEY_EXTENSIONS: tuple[tuple[tuple[str, ...], LinkBucket], ...] = (
    ((".3d", ".svx", ".trox", ".tro"), LinkBucket.SURVEY_3D),
    ((".pdf",), LinkBucket.SURVEY_PDF),
    ((".jpg", ".jpeg", ".gif", ".png", ".svg"), LinkBucket.SURVEY_IMAGE),
)

VIDEO_HOSTS = frozenset({"youtu.be", "www.youtube.com", "youtube.com", "vimeo.com"})
INTERNAL_HOSTS = frozenset({"www.matienzocaves.org.uk", "matienzocaves.org.uk"})


def classify(href: str) -> LinkBucket:
    """Which bucket an href belongs to."""
    if BOILERPLATE_RE.search(href):
        return LinkBucket.BOILERPLATE
    if href.startswith("#"):
        return LinkBucket.ANCHOR

    parsed = urlparse(href)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc in VIDEO_HOSTS:
            return LinkBucket.VIDEO_EXTERNAL
        if parsed.netloc not in INTERNAL_HOSTS:
            return LinkBucket.EXTERNAL

    path = parsed.path or href
    lowered = path.lower()

    if "/surveys/" in lowered:
        for extensions, bucket in _SURVEY_EXTENSIONS:
            if lowered.endswith(extensions):
                return bucket
        return LinkBucket.SURVEY_OTHER

    for pattern, bucket in _PATH_BUCKETS:
        if pattern.search(lowered):
            return bucket

    # A bare sibling `NNNN.htm` is another site's description page.
    if SITE_FILE_RE.search(lowered) and "/" not in path.strip("./"):
        return LinkBucket.SITE_PAGE
    if re.search(r"(?:\.\./)?descrip/\d{4}\.htm$", lowered):
        return LinkBucket.SITE_PAGE

    return LinkBucket.OTHER


def target_site(href: str) -> tuple[int | None, bool]:
    """The site number an href refers to, and whether it names a range.

    Gallery links carry the site number in their filename, so `../entpics/
    1930-2011e.htm` is an edge from this page to site 1930 just as much as a
    bare `1930.htm` is.
    """
    match = SITE_FILE_RE.search(urlparse(href).path or href)
    if match is None:
        return None, False
    suffix = match.group("suffix") or ""
    return int(match.group("site")), bool(RANGE_SUFFIX_RE.match(suffix))


def make_link(href: str, text: str = "", *, unquoted: bool = False) -> ResourceLink:
    """Build a fully classified `ResourceLink`."""
    site, is_range = target_site(href)
    bucket = classify(href)
    return ResourceLink(
        href=href,
        text=text,
        bucket=bucket,
        target_site=site if bucket is not LinkBucket.BOILERPLATE else None,
        is_range=is_range,
        unquoted_in_source=unquoted,
    )
