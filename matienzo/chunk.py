"""Split site records into retrievable passages.

The corpus is pathologically bimodal: 1,220 descriptions are a single sentence
("Small shelter.") and a handful run past 100,000 characters with a dozen named
sections. One chunk size cannot serve both, so chunks are built by packing whole
blocks rather than by cutting text at a fixed width.

Two decisions do most of the retrieval work:

**Every site gets a synthetic card chunk first.** A rendered summary — number,
name, aliases, area, type, measurements, update span. Without it, metadata
questions ("shafts in Riva over 50 m deep") have nothing to match on, and the
1,220 one-line sites have almost no text to retrieve at all.

**Every chunk is prefixed with its context.** `Site 1930 Cobadal, Sumidero de —
Wessex Inlet`. A paragraph deep inside a long cave description is otherwise an
anonymous passage about mud and boulders that matches nothing useful; the
prefix is what ties it back to a cave and a place.

Footer citations are deliberately not chunked. They are 14,661 near-identical
strings that would swamp similarity space, and they are already fully queryable
as relational rows.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from matienzo.models import BlockKind, ParsedSite, QuantityKind

#: Target and hard limits, in characters. A block is never split to meet the
#: target — blocks are paragraphs, which are the natural retrieval unit here.
TARGET_CHARS = 900
MAX_CHARS = 1_600

#: A single paragraph longer than this is split at sentence boundaries. Rare.
SENTENCE_OVERLAP = 150


@dataclass(slots=True)
class Chunk:
    site_number: int
    ordinal: int
    kind: str
    """card | prose | table | editorial"""
    text: str
    """What gets indexed and embedded, including the context prefix."""
    section_heading: str | None = None
    block_first: int | None = None
    block_last: int | None = None
    heading_path: str = ""

    @property
    def content_sha256(self) -> str:
        """Cache key for embeddings: re-embed only what actually changed."""
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass(slots=True)
class _Pending:
    blocks: list[tuple[int, str]] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(text) for _, text in self.blocks) + 2 * max(0, len(self.blocks) - 1)

    def text(self) -> str:
        return "\n\n".join(text for _, text in self.blocks)


def chunk_site(record: ParsedSite) -> list[Chunk]:
    """All chunks for one site, card first."""
    chunks: list[Chunk] = [_card(record)]
    prefix = _context_prefix(record)

    for section_heading, blocks in _grouped_blocks(record):
        header = f"{prefix} — {section_heading}" if section_heading else prefix
        for pending in _pack(blocks):
            chunks.append(
                Chunk(
                    site_number=record.site_number,
                    ordinal=len(chunks),
                    kind="prose",
                    text=f"{header}\n\n{pending.text()}",
                    section_heading=section_heading,
                    block_first=pending.blocks[0][0],
                    block_last=pending.blocks[-1][0],
                    heading_path=section_heading or "",
                )
            )

    for table in record.body.tables:
        # Tables are packed by row, for the same reason prose is packed by
        # block: `2889`'s survey-batch register is 18,000 characters, and one
        # chunk that size retrieves as a blur rather than as an answer.
        for part in _render_table(table.headers, table.rows):
            chunks.append(
                Chunk(
                    site_number=record.site_number,
                    ordinal=len(chunks),
                    kind="table",
                    text=f"{prefix} — table\n\n{part}",
                )
            )

    for note in record.body.editorial_notes:
        chunks.append(
            Chunk(
                site_number=record.site_number,
                ordinal=len(chunks),
                kind="editorial",
                text=f"{prefix} — editorial note\n\n{note}",
            )
        )

    return chunks


def _context_prefix(record: ParsedSite) -> str:
    name = record.title.name if record.title else ""
    return f"Site {record.site_number:04d} {name}".strip()


def _card(record: ParsedSite) -> Chunk:
    """A rendered summary of everything outside the description.

    This is what makes "shafts in Riva over 50 m deep" retrievable at all: the
    facts it states live in header fields that appear nowhere in the prose.
    """
    parts: list[str] = [_context_prefix(record)]

    if record.title:
        if record.title.site_type:
            parts.append(f"A {record.title.site_type}.")
        if record.title.aliases:
            parts.append("Also known as " + "; ".join(a.text for a in record.title.aliases) + ".")

    if record.header:
        if record.header.area_raw:
            parts.append(f"Area: {record.header.area_raw}.")

        measurements = [
            f"{q.label.replace('_', ' ')} {q.value_m:g}m"
            for q in record.header.quantities
            if q.value_m is not None and q.kind is not QuantityKind.PROSE
        ]
        altitudes = [
            f"altitude {c.altitude_m:g}m"
            for c in record.header.coordinates
            if c.altitude_m is not None
        ]
        if measurements or altitudes:
            parts.append(", ".join([*measurements, *altitudes[:1]]).capitalize() + ".")

        for quantity in record.header.quantities:
            if quantity.prose and quantity.prose.system_name:
                parts.append(f"Part of the {quantity.prose.system_name}.")

    if record.updated:
        years = sorted({u.date.year for u in record.updated if u.date})
        if years:
            span = f"{years[0]}" if len(years) == 1 else f"{years[0]} to {years[-1]}"
            parts.append(f"Recorded {span}.")

    return Chunk(
        site_number=record.site_number,
        ordinal=0,
        kind="card",
        text=" ".join(parts),
    )


def _grouped_blocks(record: ParsedSite) -> list[tuple[str | None, list[tuple[int, str]]]]:
    """Description blocks grouped by section.

    Section boundaries are hard chunk boundaries: `Wessex Inlet` must never
    bleed into `Passage of Vom`.
    """
    headings = {s.index: s.heading for s in record.body.sections}
    groups: list[tuple[str | None, list[tuple[int, str]]]] = []
    current_heading: str | None = None
    current: list[tuple[int, str]] = []

    for block in record.body.blocks:
        if block.kind is BlockKind.EDITORIAL_NOTE:
            continue
        if block.kind is BlockKind.SECTION_HEADING:
            if current:
                groups.append((current_heading, current))
            current_heading = headings.get(block.section_index or -1, block.text)
            current = []
            continue
        current.append((block.index, block.text))

    if current:
        groups.append((current_heading, current))
    return groups


def _pack(blocks: list[tuple[int, str]]) -> list[_Pending]:
    """Greedily pack whole blocks up to the target size.

    A body under the target becomes exactly one chunk. That matters: without it
    the 1,220 one-sentence sites would be padded or merged into neighbours and
    become noise rather than precise matches.
    """
    packed: list[_Pending] = []
    pending = _Pending()

    for index, text in blocks:
        for piece in _split_oversized(text):
            if pending.blocks and pending.chars + len(piece) > TARGET_CHARS:
                packed.append(pending)
                pending = _Pending()
            pending.blocks.append((index, piece))
            if pending.chars >= MAX_CHARS:
                packed.append(pending)
                pending = _Pending()

    if pending.blocks:
        packed.append(pending)
    return packed


def _split_oversized(text: str) -> list[str]:
    """Split a paragraph that exceeds the hard limit, at sentence boundaries."""
    if len(text) <= MAX_CHARS:
        return [text]

    sentences = text.replace(". ", ".\x00").split("\x00")
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > MAX_CHARS:
            pieces.append(current.strip())
            current = current[-SENTENCE_OVERLAP:] if len(current) > SENTENCE_OVERLAP else ""
        current += sentence + " "
    if current.strip():
        pieces.append(current.strip())
    return pieces


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Flatten a table to `header: value` lines, packed to the target size.

    Labelling each cell reads far better to a retriever than a grid of bare
    values, and it is what makes the survey-batch registers searchable by
    surveyor name.
    """
    parts: list[str] = []
    current: list[str] = []
    size = 0

    for row in rows:
        if headers and len(headers) >= len(row):
            pairs = [f"{headers[i]}: {cell}" for i, cell in enumerate(row) if cell]
        else:
            pairs = [cell for cell in row if cell]
        if not pairs:
            continue

        line = "; ".join(pairs)
        if current and size + len(line) > TARGET_CHARS:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1

    if current:
        parts.append("\n".join(current))
    return parts
