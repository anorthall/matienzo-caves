/**
 * The event vocabulary, mirrored from matienzo/web/sse.py.
 *
 * Written out rather than generated because it is small and because a
 * hand-written mirror is the thing that fails loudly in `tsc` when the server
 * changes shape — a generated one would just quietly agree with whatever it was
 * generated from.
 */

export type Mode = "agent" | "search_only";

export interface StartEvent {
  event: "start";
  seq: number;
  session_id: string;
  turn_id: number;
  mode: Mode;
  model: string | null;
}

export interface DeltaEvent {
  event: "delta";
  seq: number;
  text: string;
}

export interface ThinkingEvent {
  event: "thinking";
  seq: number;
  text: string;
}

export interface ToolUseEvent {
  event: "tool_use";
  seq: number;
  id: string;
  name: string;
  args: Record<string, unknown>;
}

export interface ToolResultEvent {
  event: "tool_result";
  seq: number;
  id: string;
  name: string;
  ok: boolean;
  ms: number;
  summary: string;
  site_numbers: number[];
}

export interface SourceEvent {
  event: "source";
  seq: number;
  site_number: number;
  name: string | null;
  area: string | null;
  url: string;
  excerpt: string;
  first_seen_tool: string;
}

export interface CitationsEvent {
  event: "citations";
  seq: number;
  cited: number[];
  unverified: number[];
}

export interface UsageEvent {
  event: "usage";
  seq: number;
  cost_usd: number;
  remaining_pct: number;
  [key: string]: unknown;
}

export interface NoticeEvent {
  event: "notice";
  seq: number;
  code: string;
  message: string;
}

export interface DoneEvent {
  event: "done";
  seq: number;
  stop_reason: string;
  truncated: boolean;
}

export interface ErrorEvent {
  event: "error";
  seq: number;
  code: string;
  message: string;
}

export type PortalEvent =
  | StartEvent
  | DeltaEvent
  | ThinkingEvent
  | ToolUseEvent
  | ToolResultEvent
  | SourceEvent
  | CitationsEvent
  | UsageEvent
  | NoticeEvent
  | DoneEvent
  | ErrorEvent;

/**
 * A tool call as the trail renders it: the request, then its outcome.
 *
 * `ok` is what says the outcome arrived. A replayed conversation has no elapsed
 * time and no summary — neither is stored, because both are presentational and
 * keeping them would mean a second copy of every tool payload in the database —
 * so the trail cannot use their absence to mean "still running".
 */
export interface ToolStep {
  id: string;
  name: string;
  args: Record<string, unknown>;
  ok?: boolean;
  ms?: number;
  summary?: string;
}

export interface Source {
  site_number: number;
  name: string | null;
  area: string | null;
  url: string;
  excerpt: string;
}

export interface Exchange {
  id: number;
  question: string;
  answer: string;
  tools: ToolStep[];
  sources: Source[];
  notice: string | null;
  error: string | null;
  streaming: boolean;
}

/** One conversation, as the thread list shows it. */
export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * A stored conversation, read back.
 *
 * Mirrored from `matienzo/web/routes/conversations.py` for the same reason the
 * event vocabulary above is: a hand-written mirror fails in `tsc` when the
 * server changes shape.
 */
export interface TranscriptTool {
  id: string;
  name: string;
  args: Record<string, unknown>;
  ok: boolean | null;
}

export interface TranscriptExchange {
  question: string;
  answer: string;
  tools: TranscriptTool[];
  sources: Source[];
}

export interface ConversationDetail {
  id: string;
  title: string | null;
  exchanges: TranscriptExchange[];
}
