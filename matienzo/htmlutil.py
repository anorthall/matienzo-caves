"""Tolerant HTML helpers for hand-written legacy markup.

The corpus is not well-formed: tag case is mixed, attributes are sometimes
unquoted or unterminated, `<P>` is almost never closed, and five pages carry
`<div data-speak>` wrappers that open and close mid-sentence. Anything here must
degrade rather than raise.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from matienzo.decode import collapse_whitespace, unescape

TAG_RE = re.compile(r"<[^>]*>")

#: An opening `<a …>` tag. The attribute blob is parsed separately — see
#: `parse_attributes` for why a regex over the whole tag is not enough.
ANCHOR_OPEN_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>", re.IGNORECASE)
ANCHOR_CLOSE_RE = re.compile(r"</a\s*>", re.IGNORECASE)

#: `name = value` where value is double-quoted, single-quoted, or bare.
#: Quoted values may contain anything, including `>` and nested quotes of the
#: other kind — which is exactly how the logbook `onclick` handler hides a
#: literal `href='…'` inside itself.
ATTR_RE = re.compile(
    r"""(?P<name>[A-Za-z_:][-\w:.]*)   # attribute name
        (?:\s*=\s*
          (?: "(?P<dq>[^"]*)"          # "…"
            | '(?P<sq>[^']*)'          # '…'
            | (?P<bare>[^\s"'>`]+)     # bare
          )
        )?""",
    re.VERBOSE,
)

#: Block-level markup that ends a paragraph. `<P>` is unclosed on all but ~105
#: pages, so opening tags — not closing ones — are the break signal.
BREAK_RE = re.compile(r"<(?:/?p|br|hr|/?div|/?tr|/?li|/?table)\b[^>]*>", re.IGNORECASE)

#: Three or more non-breaking spaces, used as a paragraph indent on 281 pages.
#: In files like `0081` this is a *more* reliable paragraph marker than the tags.
INDENT_RE = re.compile(r"(?:&nbsp;| ){3,}")


def strip_tags(html: str) -> str:
    """Plain text from a markup fragment: tags out, entities resolved, wrapped
    lines rejoined."""
    return collapse_whitespace(unescape(TAG_RE.sub(" ", html)))


def parse_attributes(blob: str) -> dict[str, str]:
    """Attributes of one tag, lowercased by name, last occurrence winning.

    Written by hand rather than regexing `href="…"` out of the raw tag because
    every page carries this::

        <a href="../logbook.php" onclick="… window.location.href='../logbook.php?code=' …">

    Searching the tag text for `href=` finds the one inside the `onclick` value
    and yields a second, non-existent link — about 2,300 phantom anchors across
    the corpus. Consuming whole quoted values makes that impossible.
    """
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(blob):
        value = match.group("dq")
        if value is None:
            value = match.group("sq")
        if value is None:
            value = match.group("bare")
        attrs[match.group("name").lower()] = value if value is not None else ""
    return attrs


@dataclass(frozen=True, slots=True)
class Anchor:
    """One `<a>` element: where it points, what it says, and where it sat."""

    href: str
    text: str
    attrs: dict[str, str]
    start: int
    end: int
    unquoted: bool
    """The `href` was written without quotes — seven anchors in the corpus."""


def iter_anchors(html: str, offset: int = 0) -> Iterator[Anchor]:
    """Every anchor in a fragment, in document order.

    Anchors with no `href` (such as the `<a name="Refs">` bookmarks) are skipped.
    An unclosed `<a>` — 31 pages have one in the header — takes its text up to
    the next tag rather than swallowing the rest of the document.
    """
    for match in ANCHOR_OPEN_RE.finditer(html):
        attrs = parse_attributes(match.group("attrs"))
        href = attrs.get("href", "").strip()
        if not href:
            continue

        close = ANCHOR_CLOSE_RE.search(html, match.end())
        next_open = ANCHOR_OPEN_RE.search(html, match.end())
        if close is not None and (next_open is None or close.start() < next_open.start()):
            text = strip_tags(html[match.end() : close.start()])
            end = close.end()
        else:
            text = strip_tags(html[match.end() : next_open.start()]) if next_open else ""
            end = match.end()

        yield Anchor(
            href=href,
            text=text,
            attrs=attrs,
            start=offset + match.start(),
            end=offset + end,
            unquoted=f'"{href}"' not in match.group("attrs")
            and f"'{href}'" not in match.group("attrs"),
        )


def split_blocks(html: str) -> list[str]:
    """Split a fragment into paragraph-ish blocks.

    Bare newlines are *not* separators — source lines are hard-wrapped at ~72
    characters by a legacy editor, so treating `\\n` as a break would shred every
    sentence. Breaks come from block tags and from the `&nbsp;&nbsp;&nbsp;`
    indent convention.
    """
    marked = INDENT_RE.sub("\x00", html)
    marked = BREAK_RE.sub("\x00", marked)
    blocks = [strip_tags(part) for part in marked.split("\x00")]
    return [block for block in blocks if block]
