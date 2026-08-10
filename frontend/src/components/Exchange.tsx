/**
 * One question and everything answering it produced.
 *
 * The order is the order it happened in: the question, then what the portal did
 * to answer it, then the answer, then the sites behind it. A spinner in place of
 * the trail would read as a hang — a multi-hop answer spends twenty seconds
 * inside tool calls with nothing to say.
 */

import type { Exchange as ExchangeData } from "../types";
import { Answer } from "./Answer";
import { ToolTrail } from "./ToolTrail";

/**
 * How many site chips sit under an answer before the rest are summarised.
 *
 * A broad question retrieves dozens — "which caves take water in wet weather"
 * returned 37 — and a chip row that long is taller than the answer it belongs
 * to, especially on a phone. The remainder is counted rather than dropped, and
 * the panel behind the counter holds every one of them.
 */
const CHIP_LIMIT = 8;

interface Props {
  exchange: ExchangeData;
  onShowSources: () => void;
}

export function Exchange({ exchange, onShowSources }: Props) {
  const { question, answer, tools, sources, notice, error, streaming } = exchange;
  const settling = streaming && answer === "" && tools.length === 0;

  return (
    <article className="exchange">
      <h2 className="exchange__question">{question}</h2>

      <div className="exchange__answer">
        <div className="byline">
          <span className="byline__mark" aria-hidden="true" />
          <span className="byline__who">Matienzo</span>
        </div>

        <ToolTrail steps={tools} thinking={streaming} />

        {notice ? (
          <p className="notice" role="status">
            {notice}
          </p>
        ) : null}

        {settling ? (
          <div className="thinking" aria-label="Working">
            <span />
            <span />
            <span />
          </div>
        ) : (
          <Answer text={answer} sources={sources} streaming={streaming} />
        )}

        {error ? (
          <p className="failure" role="alert">
            {error}
          </p>
        ) : null}

        {sources.length > 0 ? (
          <ul className="chips">
            {sources.slice(0, CHIP_LIMIT).map((source) => (
              <li key={source.site_number}>
                <button type="button" className="chip" onClick={onShowSources}>
                  <svg
                    width="12"
                    height="12"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    aria-hidden="true"
                  >
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <path d="M14 2v6h6" />
                  </svg>
                  {/* The number leads, because the corpus names many sites
                      only by their type: a row of chips reading "cave, cave,
                      cave" names nothing a reader can tell apart. */}
                  <span className="chip__number">
                    {String(source.site_number).padStart(4, "0")}
                  </span>
                  <span className="chip__name">{source.name ?? "unnamed"}</span>
                </button>
              </li>
            ))}
            {sources.length > CHIP_LIMIT ? (
              <li>
                <button type="button" className="chip chip--more" onClick={onShowSources}>
                  {sources.length - CHIP_LIMIT} more
                </button>
              </li>
            ) : null}
          </ul>
        ) : null}
      </div>
    </article>
  );
}
