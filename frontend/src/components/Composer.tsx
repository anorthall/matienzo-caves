/**
 * The question box.
 *
 * A textarea rather than an input, because the design's Enter-to-send /
 * Shift-Enter-for-a-newline pair needs a control that can hold a newline. It
 * grows with its content up to the height the stylesheet caps it at; the height
 * is set imperatively because that is the only way to measure wrapped text.
 */

import { useEffect, useRef } from "react";

const MAX_HEIGHT = 180;

interface Props {
  value: string;
  busy: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
}

export function Composer({ value, busy, onChange, onSubmit }: Props) {
  const input = useRef<HTMLTextAreaElement>(null);

  // Runs on every value change, including the reset to "" after sending, which
  // is the case that matters: without it the box stays as tall as the question
  // that has just left it.
  useEffect(() => {
    const element = input.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="composer__column">
        <div className="composer__box">
          <label className="visually-hidden" htmlFor="question">
            Your question
          </label>
          <textarea
            id="question"
            ref={input}
            rows={1}
            className="composer__input"
            placeholder="Ask a question about the caves…"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                onSubmit();
              }
            }}
          />
          <div className="composer__row">
            <div className="composer__hint">
              {busy ? "Looking…" : "Enter to send · Shift + Enter for a new line"}
            </div>
            <button
              type="submit"
              className="composer__send"
              disabled={busy || value.trim() === ""}
            >
              Ask
              <svg
                width="15"
                height="15"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M5 12h14M13 6l6 6-6 6" />
              </svg>
            </button>
          </div>
        </div>
        <p className="smallprint">
          Answers are generated and can be wrong. Every citation links to the page it came from.
        </p>
      </div>
    </form>
  );
}
