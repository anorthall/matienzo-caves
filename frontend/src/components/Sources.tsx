/**
 * The sites the corpus returned for one question.
 *
 * Fills while the answer is still being written, because `source` events are
 * emitted the moment a tool returns rather than at the end. That ordering is
 * deliberate on the server, and this panel is the reason for it: there is
 * something to read during the seconds the model spends composing.
 */

import type { Source } from "../types";

interface Props {
  sources: Source[];
}

export function Sources({ sources }: Props) {
  if (sources.length === 0) return null;

  return (
    <aside className="sources" aria-label="Sites consulted">
      <h2 className="sources__heading">
        Sites consulted<span className="sources__count">{sources.length}</span>
      </h2>
      <ol className="sources__list">
        {sources.map((source) => (
          <li key={source.site_number} className="source">
            <a
              className="source__link"
              href={source.url}
              target="_blank"
              rel="noreferrer noopener"
            >
              <span className="source__number">
                {String(source.site_number).padStart(4, "0")}
              </span>
              <span className="source__name">{source.name ?? "unnamed"}</span>
            </a>
            {source.area ? <span className="source__area">{source.area}</span> : null}
            {source.excerpt ? <p className="source__excerpt">{source.excerpt}</p> : null}
          </li>
        ))}
      </ol>
    </aside>
  );
}
