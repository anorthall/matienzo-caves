import { useCallback, useEffect, useRef, useState } from "react";

import { RateLimited, ask } from "./api";
import { Answer } from "./components/Answer";
import { Sources } from "./components/Sources";
import { ToolTrail } from "./components/ToolTrail";
import type { Exchange, Mode, PortalEvent } from "./types";

const OPENERS = [
  "Which caves in Cobadal take water in wet weather?",
  "What is the deepest shaft in the corpus?",
  "Which sites did Corrin survey in 2003?",
  "Are there caves used as animal shelters?",
];

export default function App() {
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<Mode | null>(null);
  const abort = useRef<AbortController | null>(null);
  const foot = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/healthz")
      .then((r) => r.json())
      .then((body: { mode?: Mode }) => setMode(body.mode ?? null))
      .catch(() => setMode(null));
  }, []);

  useEffect(() => {
    foot.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [exchanges]);

  const submit = useCallback(
    async (asked: string) => {
      const text = asked.trim();
      if (text === "" || busy) return;

      const id = Date.now();
      setQuestion("");
      setBusy(true);
      setExchanges((current) => [
        ...current,
        {
          id,
          question: text,
          answer: "",
          mode: mode ?? "agent",
          tools: [],
          sources: [],
          notice: null,
          error: null,
          streaming: true,
        },
      ]);

      const update = (change: (exchange: Exchange) => Exchange) =>
        setExchanges((current) => current.map((e) => (e.id === id ? change(e) : e)));

      const controller = new AbortController();
      abort.current = controller;

      try {
        await ask(text, {
          signal: controller.signal,
          onEvent: (event: PortalEvent) => update((e) => apply(e, event)),
        });
      } catch (error) {
        const message =
          error instanceof RateLimited
            ? `Too many questions just now — try again in ${error.retryAfterSeconds}s.`
            : error instanceof DOMException && error.name === "AbortError"
              ? null
              : "The connection dropped before the answer finished.";
        update((e) => ({ ...e, error: message, streaming: false }));
      } finally {
        update((e) => ({ ...e, streaming: false }));
        abort.current = null;
        setBusy(false);
      }
    },
    [busy, mode],
  );

  return (
    <div className="shell">
      <header className="masthead">
        <div className="masthead__mark">
          <span className="masthead__depth">−</span>
          <span className="masthead__rule" />
        </div>
        <div>
          <h1 className="masthead__title">Matienzo</h1>
          <p className="masthead__sub">
            5,557 cave and shaft descriptions from the Matienzo depression, Cantabria
          </p>
        </div>
        {mode === "search_only" ? (
          <p className="banner" role="status">
            Answering is unavailable — showing matching sites only.
          </p>
        ) : null}
      </header>

      <main className="stream">
        {exchanges.length === 0 ? (
          <section className="opening">
            <p className="opening__lede">
              Ask about a cave, a description you half remember, or something the whole
              corpus would have to be counted to answer.
            </p>
            <ul className="opening__examples">
              {OPENERS.map((opener) => (
                <li key={opener}>
                  <button type="button" onClick={() => void submit(opener)} disabled={busy}>
                    {opener}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {exchanges.map((exchange) => (
          <article key={exchange.id} className="exchange">
            <h2 className="exchange__question">{exchange.question}</h2>
            <ToolTrail steps={exchange.tools} thinking={exchange.streaming} />
            {exchange.notice ? (
              <p className="notice" role="status">
                {exchange.notice}
              </p>
            ) : null}
            <div className="exchange__body">
              <Answer
                text={exchange.answer}
                sources={exchange.sources}
                streaming={exchange.streaming}
              />
              <Sources sources={exchange.sources} />
            </div>
            {exchange.error ? (
              <p className="failure" role="alert">
                {exchange.error}
              </p>
            ) : null}
          </article>
        ))}
        <div ref={foot} />
      </main>

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submit(question);
        }}
      >
        <label className="visually-hidden" htmlFor="question">
          Your question
        </label>
        <input
          id="question"
          autoComplete="off"
          placeholder={busy ? "Looking…" : "Ask about the caves"}
          value={question}
          disabled={busy}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <button type="submit" disabled={busy || question.trim() === ""}>
          Ask
        </button>
      </form>

      <footer className="colophon">
        <p>
          Descriptions from{" "}
          <a href="https://www.matienzocaves.org.uk/" target="_blank" rel="noreferrer noopener">
            matienzocaves.org.uk
          </a>
          . Answers are generated and can be wrong — every citation links to the page it
          came from. Questions are kept for 14 days.
        </p>
      </footer>
    </div>
  );
}

/** Fold one server event into the exchange it belongs to. */
function apply(exchange: Exchange, event: PortalEvent): Exchange {
  switch (event.event) {
    case "start":
      return { ...exchange, mode: event.mode };
    case "delta":
      return { ...exchange, answer: exchange.answer + event.text };
    case "tool_use":
      return {
        ...exchange,
        tools: [...exchange.tools, { id: event.id, name: event.name, args: event.args }],
      };
    case "tool_result":
      return {
        ...exchange,
        tools: exchange.tools.map((step) =>
          step.id === event.id
            ? { ...step, ok: event.ok, ms: event.ms, summary: event.summary }
            : step,
        ),
      };
    case "source":
      return {
        ...exchange,
        sources: [
          ...exchange.sources,
          {
            site_number: event.site_number,
            name: event.name,
            area: event.area,
            url: event.url,
            excerpt: event.excerpt,
          },
        ],
      };
    case "notice":
      return { ...exchange, notice: event.message };
    case "error":
      return { ...exchange, error: event.message, streaming: false };
    case "done":
      return { ...exchange, streaming: false };
    default:
      return exchange;
  }
}
