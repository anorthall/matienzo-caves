/**
 * The answer, with its citations resolved.
 *
 * This component is where the provenance guarantee is actually enforced. The
 * server records which sites the tools returned and streams them as `source`
 * events; a `[[site:NNNN]]` marker becomes a link only if its number is in that
 * set. A marker naming anything else renders as plain text, so a model that
 * invents a citation produces a slightly odd sentence rather than a live link to
 * a 404 on somebody else's website.
 */

import type { Source } from "../types";

const MARKER = /\[\[site:(\d{1,4})\]\]/g;

interface Props {
  text: string;
  sources: Source[];
  streaming: boolean;
}

export function Answer({ text, sources, streaming }: Props) {
  const known = new Map(sources.map((s) => [s.site_number, s]));

  const paragraphs = text.split(/\n{2,}/).filter((p) => p.trim() !== "");

  return (
    <div className="answer">
      {paragraphs.map((paragraph, index) => (
        <p key={index}>
          {render(paragraph, known)}
          {streaming && index === paragraphs.length - 1 ? <span className="caret" /> : null}
        </p>
      ))}
      {paragraphs.length === 0 && streaming ? (
        <p>
          <span className="caret" />
        </p>
      ) : null}
    </div>
  );
}

function render(paragraph: string, known: Map<number, Source>) {
  const parts: React.ReactNode[] = [];
  let cursor = 0;

  MARKER.lastIndex = 0;
  for (let match = MARKER.exec(paragraph); match !== null; match = MARKER.exec(paragraph)) {
    if (match.index > cursor) parts.push(...inline(paragraph.slice(cursor, match.index), cursor));

    const number = Number(match[1]);
    const source = known.get(number);
    if (source) {
      parts.push(
        <a
          key={`${match.index}-${number}`}
          className="citation"
          href={source.url}
          target="_blank"
          rel="noreferrer noopener"
          title={source.name ?? `Site ${number}`}
        >
          {String(number).padStart(4, "0")}
        </a>,
      );
    } else {
      // Deliberately not a link. See the module comment.
      parts.push(
        <span key={`${match.index}-${number}`} className="citation citation--unverified">
          {String(number).padStart(4, "0")}
        </span>,
      );
    }
    cursor = match.index + match[0].length;
  }

  if (cursor < paragraph.length) parts.push(...inline(paragraph.slice(cursor), cursor));
  return parts;
}

const BOLD = /\*\*([^*]+)\*\*/g;

/**
 * The light markdown the model actually emits, as text nodes.
 *
 * Applied to the runs *between* citation markers, never across one, so the
 * marker pass above stays the only thing that decides what becomes a link.
 * Nothing here can produce an anchor or an element from model output — it emits
 * `strong` and `br` around text and nothing else — which is what keeps the
 * provenance guarantee a property of this file rather than a hope.
 *
 * Emphasis and line breaks only. Answers arrive with `**site 675**` and
 * newline-separated list items in them, and the alternative to handling those
 * two is a markdown library parsing untrusted model output on a page whose
 * whole claim is that it does not render what the model says as markup.
 */
function inline(text: string, offset: number): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  let cursor = 0;

  BOLD.lastIndex = 0;
  for (let match = BOLD.exec(text); match !== null; match = BOLD.exec(text)) {
    if (match.index > cursor) parts.push(...lines(text.slice(cursor, match.index), offset + cursor));
    parts.push(<strong key={`b${offset}-${match.index}`}>{match[1]}</strong>);
    cursor = match.index + match[0].length;
  }

  if (cursor < text.length) parts.push(...lines(text.slice(cursor), offset + cursor));
  return parts;
}

/** A run of text with its single newlines kept as line breaks. */
function lines(text: string, offset: number): React.ReactNode[] {
  const segments = text.split("\n");
  return segments.flatMap((segment, index) =>
    index === 0 ? [segment] : [<br key={`n${offset}-${index}`} />, segment],
  );
}
