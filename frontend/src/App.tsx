import { useCallback, useEffect, useRef, useState } from "react";

import {
  RateLimited,
  ask,
  deleteConversation,
  listConversations,
  readConversation,
} from "./api";
import { Composer } from "./components/Composer";
import { Exchange as ExchangeView } from "./components/Exchange";
import { Sidebar } from "./components/Sidebar";
import { SourcesPanel } from "./components/SourcesPanel";
import type { Conversation, Exchange, Mode, PortalEvent, Source } from "./types";

const OPENERS = [
  { kicker: "Locate", text: "Which caves in Cobadal take water in wet weather?" },
  { kicker: "Measure", text: "What is the deepest shaft in the corpus?" },
  { kicker: "Trace", text: "What is the biggest resurgence in the area?" },
  { kicker: "Find", text: "Are there caves used as animal shelters?" },
];

/** Mirrors `store._title`, so an optimistic row reads the same as the stored
 *  one and does not visibly rewrite itself a second later. */
const TITLE_CHARS = 60;

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<Mode | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);

  const abort = useRef<AbortController | null>(null);
  const foot = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/healthz")
      .then((r) => r.json())
      .then((body: { mode?: Mode }) => setMode(body.mode ?? null))
      .catch(() => setMode(null));
    listConversations()
      .then(setConversations)
      .catch(() => setConversations([]));
  }, []);

  useEffect(() => {
    foot.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [exchanges]);

  /** Stop a stream that is still arriving for a conversation we are leaving. */
  const stop = useCallback(() => {
    abort.current?.abort();
    abort.current = null;
  }, []);

  const startNew = useCallback(() => {
    stop();
    setActiveId(null);
    setExchanges([]);
    setQuestion("");
    setNavOpen(false);
  }, [stop]);

  const open = useCallback(
    async (id: string) => {
      stop();
      setNavOpen(false);
      setActiveId(id);
      setExchanges([]);
      try {
        const detail = await readConversation(id);
        setExchanges(
          detail.exchanges.map((exchange, index) => ({
            id: index,
            question: exchange.question,
            answer: exchange.answer,
            // `ok: null` on the wire means no result was ever recorded. The
            // trail reads `undefined` as still-running, and they are the same
            // state, so the two spellings meet here.
            tools: exchange.tools.map((tool) => ({
              id: tool.id,
              name: tool.name,
              args: tool.args,
              ok: tool.ok ?? undefined,
            })),
            sources: exchange.sources,
            notice: null,
            error: null,
            streaming: false,
          })),
        );
      } catch {
        setExchanges([]);
        setActiveId(null);
      }
    },
    [stop],
  );

  const remove = useCallback(
    async (id: string) => {
      setConversations((current) => current.filter((c) => c.id !== id));
      if (id === activeId) startNew();
      await deleteConversation(id).catch(() => {
        // It stays gone from the list either way; a failed delete surfaces on
        // the next load rather than as a dialog over a chat.
      });
    },
    [activeId, startNew],
  );

  const submit = useCallback(
    async (asked: string) => {
      const text = asked.trim();
      if (text === "" || busy) return;

      const id = Date.now();
      const continuing = activeId;
      setQuestion("");
      setBusy(true);
      setExchanges((current) => [
        ...current,
        {
          id,
          question: text,
          answer: "",
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
          conversationId: continuing,
          onEvent: (event: PortalEvent) => {
            // The server names a new conversation on `start`, and that is the
            // only place the id ever comes from: the browser does not get to
            // choose it, and asking without one is what creates it.
            if (event.event === "start" && continuing === null) {
              setActiveId(event.session_id);
              setConversations((current) => [
                {
                  id: event.session_id,
                  title: text.split(/\s+/).join(" ").slice(0, TITLE_CHARS),
                  created_at: new Date().toISOString(),
                  updated_at: new Date().toISOString(),
                },
                ...current,
              ]);
            }
            update((e) => apply(e, event));
          },
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
        if (abort.current === controller) abort.current = null;
        setBusy(false);
      }
    },
    [activeId, busy],
  );

  const consulted = dedupe(exchanges);
  const title =
    conversations.find((c) => c.id === activeId)?.title ?? (activeId ? "Untitled" : "New chat");

  return (
    <div
      className="shell"
      data-nav={navOpen ? "open" : "shut"}
      data-sources={sourcesOpen ? "open" : "shut"}
    >
      <div
        className="scrim"
        onClick={() => {
          setNavOpen(false);
          setSourcesOpen(false);
        }}
      />

      <Sidebar
        conversations={conversations}
        activeId={activeId}
        drafting={activeId === null && exchanges.length > 0}
        busy={busy}
        onNew={startNew}
        onOpen={(id) => void open(id)}
        onDelete={(id) => void remove(id)}
        onClose={() => setNavOpen(false)}
      />

      <main className="main">
        <header className="topbar">
          <button
            type="button"
            className="icon-button drawer-toggle"
            onClick={() => setNavOpen(true)}
            aria-label="Open conversations"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <path d="M3 6h18M3 12h18M3 18h18" />
            </svg>
          </button>
          <h1 className="topbar__title">{title}</h1>
          <button
            type="button"
            className="sources-toggle"
            aria-expanded={sourcesOpen}
            onClick={() => setSourcesOpen((current) => !current)}
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
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
            </svg>
            Sites
            <span className="sources-toggle__count">{consulted.length}</span>
          </button>
        </header>

        {mode === "search_only" ? (
          <p className="banner" role="status">
            Answering is unavailable — showing matching sites only.
          </p>
        ) : null}

        <div className="stream">
          <div className="stream__column">
            {exchanges.length === 0 ? (
              <section className="opening">
                <h2 className="opening__title">Ask the archive</h2>
                <p className="opening__lede">
                  5,557 cave and shaft descriptions from the Matienzo depression, Cantabria.
                  Answers are drawn from the corpus and returned with the sites they came
                  from. Start with a prompt, or ask your own.
                </p>
                <ul className="opening__examples">
                  {OPENERS.map((opener) => (
                    <li key={opener.text}>
                      <button
                        type="button"
                        className="preset"
                        onClick={() => void submit(opener.text)}
                        disabled={busy}
                      >
                        <div className="preset__kicker">{opener.kicker}</div>
                        <div className="preset__text">{opener.text}</div>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            {exchanges.map((exchange) => (
              <ExchangeView
                key={exchange.id}
                exchange={exchange}
                onShowSources={() => setSourcesOpen(true)}
              />
            ))}
            <div ref={foot} />
          </div>
        </div>

        <Composer
          value={question}
          busy={busy}
          onChange={setQuestion}
          onSubmit={() => void submit(question)}
        />
      </main>

      <SourcesPanel
        sources={consulted}
        open={sourcesOpen}
        onClose={() => setSourcesOpen(false)}
      />
    </div>
  );
}

/** Every site this conversation consulted, first mention winning.
 *
 *  Deduped across exchanges rather than per answer: the panel is a record of
 *  what the corpus returned for the conversation, and a follow-up question
 *  usually retrieves several of the same sites again. */
function dedupe(exchanges: Exchange[]): Source[] {
  const seen = new Map<number, Source>();
  for (const exchange of exchanges) {
    for (const source of exchange.sources) {
      if (!seen.has(source.site_number)) seen.set(source.site_number, source);
    }
  }
  return [...seen.values()];
}

/** Fold one server event into the exchange it belongs to. */
function apply(exchange: Exchange, event: PortalEvent): Exchange {
  switch (event.event) {
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
