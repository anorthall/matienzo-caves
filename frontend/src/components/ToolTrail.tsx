/**
 * What the portal did to answer.
 *
 * Shown because the alternative — a spinner for twenty seconds while three
 * searches run — reads as a hang. It is also the honest version of the answer:
 * a reader can see that a claim came from a `sql` aggregate rather than from a
 * ranked search, which changes how much to trust it.
 */

import type { ToolStep } from "../types";

const LABELS: Record<string, string> = {
  search_sites: "Searching sites",
  search_passages: "Reading passages",
  get_site: "Opening site",
  nearby_sites: "Looking nearby",
  site_graph: "Tracing references",
  find_by_citation: "Checking the bibliography",
  corpus_stats: "Counting the corpus",
  sql: "Querying",
};

interface Props {
  steps: ToolStep[];
  thinking: boolean;
}

export function ToolTrail({ steps, thinking }: Props) {
  if (steps.length === 0) return null;

  return (
    <ol className="trail" aria-label="Search steps">
      {steps.map((step) => (
        <li
          key={step.id}
          className={`trail__step${step.ok === false ? " trail__step--failed" : ""}${
            // `ok` rather than `summary`: a replayed conversation has no
            // summary for any step, and reading that as "still running" would
            // leave every old trail blinking forever.
            step.ok === undefined ? " trail__step--running" : ""
          }`}
        >
          <span className="trail__label">{LABELS[step.name] ?? step.name}</span>
          <span className="trail__detail">{describe(step)}</span>
          {step.summary !== undefined ? (
            <span className="trail__summary">{step.summary}</span>
          ) : null}
          {step.ms !== undefined ? <span className="trail__ms">{step.ms} ms</span> : null}
        </li>
      ))}
      {thinking && steps.every((s) => s.ok !== undefined) ? (
        <li className="trail__step trail__step--running">
          <span className="trail__label">Working</span>
        </li>
      ) : null}
    </ol>
  );
}

function describe(step: ToolStep): string {
  const args = step.args ?? {};
  if (typeof args.query === "string") return truncate(args.query, 64);
  if (typeof args.site_number === "number") return String(args.site_number).padStart(4, "0");
  if (typeof args.author === "string") return args.author;
  return "";
}

function truncate(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`;
}
