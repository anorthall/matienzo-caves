"""Parse the footer's reference list into individual citations.

The list is semicolon-separated. So are HTML entities, which is the trap: split
`Fern&aacute;ndez Guti&eacute;rrez et al, 1966; Corrin J S, 1981` on `;` and you
get four fragments instead of two, one of which is the string `Fern&aacute`.
Doing that across the corpus inflates the distinct-citation count by about 40
and litters the bibliography with tokens like `Ruiz Cobo Jes&uacute`.

Entities cannot simply be resolved before splitting either, because the anchors
have to survive: a citation's links are part of it, and one citation in 234
pages is split across *two* anchors —

    anon., 2005b (<a href="…2005e…">Easter</a> &amp; <a href="…2005s…">summer</a>)

— which is one bibliographic entry pointing at two logbooks, not two entries
named "Easter" and "summer". So the entities are masked, the markup is split,
and the entities are put back.
"""

from __future__ import annotations

import re

from matienzo.anomaly import AnomalyCode, AnomalyRecorder
from matienzo.decode import unescape
from matienzo.htmlutil import iter_anchors, strip_tags
from matienzo.models import Citation, CitationKind, ResourceLink
from matienzo.parse.links import make_link

#: Any well-formed entity reference. Masked before splitting so its terminating
#: semicolon is not mistaken for a field separator.
ENTITY_RE = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")

#: `Author(s), YYYYx (qualifier)`, with the variations the corpus actually
#: contains: the year is sometimes parenthesised (`Chandler, I (2009)`),
#: sometimes flagged uncertain (`… Lobetum, 1995?`), and the entry sometimes
#: continues past the qualifier with a title or a note (`anon., 2026a (Easter
#: logbook) See 025 Risco`). The qualifier is `[^)]*` rather than `.*` so it
#: stops at its own bracket instead of swallowing everything after it.
CITATION_RE = re.compile(
    r"^(?P<author>.+?)[,\s]\s*\(?\s*(?P<year>1[6-9]\d{2}|20\d{2})"
    r"(?P<disambiguator>[a-z])?\s*\)?\s*(?P<uncertain>\?)?\s*"
    r"(?:\((?P<qualifier>[^)]*)\))?\s*(?P<trailing>.*?)\s*$",
    re.DOTALL,
)

#: Notes about where a record physically lives. Not bibliography, and mixed into
#: the same semicolon-separated list, so they must be filtered before anyone
#: tries to parse an author out of them.
PROVENANCE_TOKENS: frozenset[str] = frozenset(
    {
        "material in file",
        "card",
        "cards",
        "card/material in file",
        "file",
        "in file",
        "pers comm",
        "pers comm.",
        "personal communication",
        "none",
        "no reference",
        "sead website",
        "acanto web site",
        "survey",
        "photo",
        "photos",
        "logbook",
        "log book",
        "see log book",
        "unknown",
        "?",
    }
)

#: A link inside a citation that is an *attachment* to it rather than a separate
#: reference: `<A>Fernández …, 1966</A> (<A>survey</A>)`.
ATTACHMENT_TEXTS: frozenset[str] = frozenset(
    {
        "survey",
        "surveys",
        "survey and photo",
        "survey and photos",
        "survey and drawings",
        "sketch",
        "photo",
        "photos",
        "plan",
        "elevation",
    }
)

#: The closed vocabulary of parenthetical qualifiers. An unrecognised qualifier
#: is kept but flagged — it usually means the upstream page invented a new
#: descriptor, which is worth knowing about.
KNOWN_QUALIFIERS: frozenset[str] = frozenset(
    {
        "easter logbook",
        "summer logbook",
        "autumn logbook",
        "spring logbook",
        "winter logbook",
        "christmas logbook",
        "xmas logbook",
        "whit logbook",
        "january logbook",
        "february logbook",
        "january, february logbook",
        "spring, summer logbook",
        "autumn + christmas logbook",
        "easter and summer logbooks",
        "easter & summer",
        "easter and summer",
        "logbook",
        "log book",
        "survey",
        "surveys",
        "photo",
        "photos",
        "survey and photo",
        "survey and photos",
        "survey and drawings",
        "volume 1 and volume 2",
        "p smith",
        "sketch",
        "?",
    }
)

LOGBOOK_HINT_RE = re.compile(r"log\s?book", re.IGNORECASE)
ANON_RE = re.compile(r"^anon\.?$", re.IGNORECASE)

#: Personal communications carry free-form trailers — `pers comm 83`,
#: `pers comm. Jan '86` — so they need a prefix rule of their own rather than
#: the exact-or-bracketed match the other provenance tokens use.
PERS_COMM_RE = re.compile(r"^pers(?:onal)?\.?\s+comm", re.IGNORECASE)


def split_units(html: str) -> list[str]:
    """Split reference-list markup on `;`, leaving entities intact.

    Returns markup fragments, not text — each fragment still contains its
    anchors, because a citation's links belong to it.
    """
    stash: list[str] = []

    def mask(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return f"\x01{len(stash) - 1}\x02"

    masked = ENTITY_RE.sub(mask, html)
    restore = re.compile(r"\x01(\d+)\x02")
    return [restore.sub(lambda m: stash[int(m.group(1))], part) for part in masked.split(";")]


def parse_citations(html: str, recorder: AnomalyRecorder) -> list[Citation]:
    """Parse the value of the footer's `Reference(s)` field."""
    citations: list[Citation] = []
    for ordinal, fragment in enumerate(split_units(html)):
        text = strip_tags(fragment)
        if not text and not list(iter_anchors(fragment)):
            continue
        citations.append(_parse_one(fragment, text, ordinal, recorder))
    return citations


def _parse_one(fragment: str, text: str, ordinal: int, recorder: AnomalyRecorder) -> Citation:
    citation = Citation(raw=text, ordinal=ordinal)
    links, attachments = _split_links(fragment)
    citation.links = links
    citation.attachments = attachments

    if len(links) > 1:
        # One entry, two logbooks — see the module docstring.
        recorder.add(
            AnomalyCode.CITATION_SPLIT_ANCHORS,
            f"citation {text[:60]!r} spans {len(links)} anchors",
            field_path="footer.citations",
        )

    if _is_provenance(text):
        citation.kind = CitationKind.PROVENANCE
        return citation

    match = CITATION_RE.match(text.strip())
    if match is None:
        citation.kind = CitationKind.UNPARSED
        if text.strip():
            recorder.add(
                AnomalyCode.CITATION_UNPARSED,
                f"no author/year in {text[:60]!r}",
                field_path="footer.citations",
            )
        return citation

    author = match.group("author").strip().rstrip(",")
    citation.author_raw = author
    citation.authors = _split_authors(author)
    citation.year = int(match.group("year"))
    citation.disambiguator = match.group("disambiguator")

    qualifier = (match.group("qualifier") or "").strip()
    if qualifier:
        citation.qualifier = qualifier
        citation.qualifier_is_known = qualifier.lower() in KNOWN_QUALIFIERS
        if not citation.qualifier_is_known:
            recorder.add(
                AnomalyCode.CITATION_QUALIFIER_UNKNOWN,
                f"qualifier {qualifier!r} is not in the known vocabulary",
                field_path="footer.citations",
            )

    citation.kind = _classify(citation)
    return citation


def _is_provenance(text: str) -> bool:
    """Whether an entry is a note about where the record lives, not a citation.

    Matched on the leading token rather than the whole string, because these
    carry qualifiers of their own: `pers comm (emails 21/5/02 & 10/6/02)` is
    still `pers comm`, and trying to read an author and year out of it produces
    nonsense.
    """
    normalised = re.sub(r"\s+", " ", text.strip().rstrip(".")).lower()
    if normalised in PROVENANCE_TOKENS or PERS_COMM_RE.match(normalised):
        return True
    return any(
        normalised.startswith(f"{token} (") or normalised.startswith(f"{token},")
        for token in PROVENANCE_TOKENS
    )


def _split_links(fragment: str) -> tuple[list[ResourceLink], list[ResourceLink]]:
    """Separate a citation's own links from attachments hanging off it.

    A `(survey)` link following a citation is a scan of that publication's
    survey, not an independent reference. Treating it as one both invents a
    bibliography entry called "survey" and loses the association.
    """
    links: list[ResourceLink] = []
    attachments: list[ResourceLink] = []
    for anchor in iter_anchors(fragment):
        link = make_link(anchor.href, anchor.text, unquoted=anchor.unquoted)
        is_attachment = anchor.text.strip().lower() in ATTACHMENT_TEXTS and bool(links)
        (attachments if is_attachment else links).append(link)
    return links, attachments


def _split_authors(author: str) -> list[str]:
    """Split a multi-author string into individual names.

    Deliberately conservative. `Fernández Gutiérrez et al` is one name plus an
    `et al` marker, not three authors, and Spanish surname pairs mean a
    space-separated split would be nonsense.
    """
    if ANON_RE.match(author.strip()):
        return ["anon."]
    cleaned = re.sub(r"\bet\s+al\.?\b", "", author, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s+and\s+|\s+et\s+|\s*&\s*", cleaned, flags=re.IGNORECASE)
    return [" ".join(p.split()) for p in parts if p.strip()]


def _classify(citation: Citation) -> CitationKind:
    qualifier = (citation.qualifier or "").lower()
    if LOGBOOK_HINT_RE.search(qualifier) or (
        citation.author_raw and ANON_RE.match(citation.author_raw.strip())
    ):
        return CitationKind.LOGBOOK
    if "survey" in qualifier:
        return CitationKind.SURVEY
    return CitationKind.PUBLICATION


def citation_key(citation: Citation) -> str:
    """A stable identity for de-duplicating citations across pages.

    The qualifier is part of the key on purpose. `anon., 2005b (Easter
    logbook)` appearing on 400 pages is one work; `anon., 2005b` with a
    different qualifier is a different one, and merging them on author-plus-year
    alone would silently collapse distinct logbooks together.
    """
    if citation.kind is CitationKind.PROVENANCE or citation.year is None:
        return f"raw:{unescape(citation.raw).strip().lower()}"
    return "|".join(
        (
            (citation.author_raw or "").strip().lower(),
            str(citation.year),
            citation.disambiguator or "",
            (citation.qualifier or "").strip().lower(),
        )
    )
