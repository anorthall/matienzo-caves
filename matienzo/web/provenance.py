"""Which sites an answer is allowed to link to.

Two mechanisms, and only one of them is trusted. The ledger records every site
number that a tool actually returned; the model is asked to write `[[site:NNNN]]`
after the claims those sites support. The browser renders a marker as a link
only when that number is in the ledger, so a hallucinated citation degrades into
inert text rather than into a live link to a 404 on somebody else's website.

Trusting the model alone would put that link there. Recording provenance alone
would give a correct list of sources with no way to attach a claim to one, which
matters in a corpus where two caves three hundred metres apart read almost
identically. Hence both.

The scanner exists because of streaming. A marker will be split across delta
boundaries — the model emits `…tunnel [[si` and then `te:1930]] which…` — and no
regex on the client can reliably put those back together. So text is held back
just long enough to be sure a marker is not half-arrived.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Final

from matienzo import links
from matienzo.web.sessions.store import Source

#: `[[site:1930]]`. Double brackets because they do not occur anywhere in the
#: corpus, so a marker can never be something a cave description said.
MARKER_RE: Final = re.compile(r"\[\[site:(\d{1,4})\]\]")

#: The longest a marker can be, and therefore how much text has to be withheld
#: at the tail of a delta before the rest can be released.
MAX_MARKER_CHARS: Final = len("[[site:5557]]")

#: Excerpt kept per source for the browser's panel.
EXCERPT_CHARS: Final = 300


@dataclass(slots=True)
class MarkerScanner:
    """Releases streamed text once it cannot be holding half a marker.

    `feed` returns the portion safe to send now; `flush` returns whatever is
    left when the block ends. Everything fed is also accumulated, because the
    citation report at the end needs the whole answer, not the fragments.
    """

    _pending: str = ""
    _full: list[str] = field(default_factory=list)

    def feed(self, text: str) -> str:
        self._full.append(text)
        buffer = self._pending + text

        cut = len(buffer)
        window = max(0, len(buffer) - MAX_MARKER_CHARS)
        opening = buffer.rfind("[", window)
        if opening != -1 and not MARKER_RE.fullmatch(buffer[opening:]):
            # Could be the start of a marker that has not finished arriving.
            cut = opening

        self._pending = buffer[cut:]
        return buffer[:cut]

    def flush(self) -> str:
        remainder, self._pending = self._pending, ""
        return remainder

    @property
    def text(self) -> str:
        return "".join(self._full)


@dataclass(frozen=True, slots=True)
class CitationReport:
    cited: tuple[int, ...]
    unverified: tuple[int, ...]
    """Markers naming sites no tool returned. Should always be empty; when it is
    not, the system prompt has drifted and this is the signal that says so."""


class Ledger:
    """Every site the corpus returned during one answer, in first-seen order."""

    def __init__(self) -> None:
        self._sources: dict[int, Source] = {}

    def __contains__(self, site_number: int) -> bool:
        return site_number in self._sources

    @property
    def sources(self) -> tuple[Source, ...]:
        return tuple(self._sources.values())

    def record(
        self, connection: sqlite3.Connection, site_numbers: tuple[int, ...], *, tool: str
    ) -> list[Source]:
        """Hydrate and remember any sites not seen yet, returning the new ones.

        Only the new ones, because the caller emits a `source` event per result
        and re-announcing a site on every tool call would fill the panel with
        duplicates.
        """
        fresh = [n for n in site_numbers if n not in self._sources]
        if not fresh:
            return []

        rows = connection.execute(
            "SELECT s.site_number, s.name, s.body_text, a.name AS area"
            " FROM site s LEFT JOIN area a ON a.area_id = s.area_id"
            f" WHERE s.site_number IN ({','.join('?' * len(fresh))})",
            tuple(fresh),
        ).fetchall()
        by_number = {row["site_number"]: row for row in rows}

        added: list[Source] = []
        for number in fresh:
            row = by_number.get(number)
            source = Source(
                site_number=number,
                name=row["name"] if row else None,
                area=row["area"] if row else None,
                url=links.site_url(number),
                excerpt=" ".join((row["body_text"] or "").split())[:EXCERPT_CHARS]
                if row
                else "",
                first_seen_tool=tool,
            )
            self._sources[number] = source
            added.append(source)
        return added

    def report(self, answer: str) -> CitationReport:
        """Split the markers in an answer into verified and not."""
        cited: dict[int, None] = {}
        unverified: dict[int, None] = {}
        for match in MARKER_RE.finditer(answer):
            number = int(match.group(1))
            (cited if number in self._sources else unverified).setdefault(number, None)
        return CitationReport(cited=tuple(cited), unverified=tuple(unverified))
