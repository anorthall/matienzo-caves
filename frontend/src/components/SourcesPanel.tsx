/**
 * Every site the corpus returned during this conversation.
 *
 * Fills while the answer is still being written, because `source` events are
 * emitted the moment a tool returns rather than at the end. That ordering is
 * deliberate on the server, and this panel is the reason for it: there is
 * something to read during the seconds the model spends composing.
 *
 * The design puts a relevance score and a bar on each card. There is no score
 * to put there — the server reports which sites a tool returned, not how well
 * each one matched — so the cards carry the site number and area instead.
 * Inventing a percentage would be inventing evidence, on the one panel whose
 * whole job is to show where the answer came from.
 */

import type { Source } from "../types";

interface Props {
  sources: Source[];
  open: boolean;
  onClose: () => void;
}

export function SourcesPanel({ sources, open, onClose }: Props) {
  return (
    <aside
      className={`panel${open ? " panel--open" : ""}`}
      aria-label="Sites consulted"
      aria-hidden={!open}
      // Focus must not land inside a pane that is animating shut, or a keyboard
      // visitor tabs into 340px of nothing.
      inert={!open}
    >
      <div className="panel__body">
        <div className="panel__head">
          <h2 className="panel__title">Sites consulted</h2>
          <button
            type="button"
            className="icon-button icon-button--plain"
            onClick={onClose}
            aria-label="Close sites consulted"
          >
            <svg
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <ul className="panel__list">
          {sources.map((source) => (
            <li key={source.site_number} className="source">
              <a
                className="source__link"
                href={source.url}
                target="_blank"
                rel="noreferrer noopener"
              >
                <span className="source__name">{source.name ?? "unnamed"}</span>
                <span className="source__number">
                  {String(source.site_number).padStart(4, "0")}
                </span>
              </a>
              {source.area ? <p className="source__area">{source.area}</p> : null}
              {source.excerpt ? <p className="source__excerpt">{source.excerpt}</p> : null}
            </li>
          ))}
          {sources.length === 0 ? (
            <li className="panel__empty">
              Nothing retrieved yet. Sites appear here once a question has been answered.
            </li>
          ) : null}
        </ul>
      </div>
    </aside>
  );
}
