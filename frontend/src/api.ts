/**
 * Reading the portal's event stream.
 *
 * SSE framing, but over a POST, so `EventSource` is out — it can only GET, which
 * would put the visitor's question in the URL and therefore in every proxy log
 * between here and the server. `fetch` plus a small line parser gets the same
 * framing without that.
 *
 * The parser holds a buffer because a chunk boundary lands wherever TCP puts it,
 * not on a frame boundary: one `read()` can carry half an event, and the next
 * carries the rest.
 */

import type { Conversation, ConversationDetail, PortalEvent } from "./types";

export interface AskOptions {
  signal?: AbortSignal;
  onEvent: (event: PortalEvent) => void;
  /** Which conversation to continue. Omitted, the server opens a new one and
   *  names it on the `start` event. */
  conversationId?: string | null;
}

export class RateLimited extends Error {
  constructor(readonly retryAfterSeconds: number) {
    super("Too many questions just now.");
    this.name = "RateLimited";
  }
}

export async function listConversations(): Promise<Conversation[]> {
  const response = await fetch("/api/conversations");
  if (!response.ok) throw new Error(`The portal returned ${response.status}.`);
  return (await response.json()) as Conversation[];
}

export async function readConversation(id: string): Promise<ConversationDetail> {
  const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`);
  if (!response.ok) throw new Error(`The portal returned ${response.status}.`);
  return (await response.json()) as ConversationDetail;
}

export async function deleteConversation(id: string): Promise<void> {
  const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  // A 404 means it is already gone, which is the state the caller wanted.
  if (!response.ok && response.status !== 404) {
    throw new Error(`The portal returned ${response.status}.`);
  }
}

export async function ask(
  question: string,
  { signal, onEvent, conversationId }: AskOptions,
): Promise<void> {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question, conversation_id: conversationId ?? null }),
    signal,
  });

  if (response.status === 429) {
    const retry = Number(response.headers.get("retry-after") ?? "30");
    throw new RateLimited(Number.isFinite(retry) ? retry : 30);
  }
  if (!response.ok || !response.body) {
    throw new Error(`The portal returned ${response.status}.`);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let name: string | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value;

    // Frames are separated by a blank line; keep the trailing partial.
    let newline: number;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);

      if (line === "" || line.startsWith(":")) continue; // blank or keepalive
      if (line.startsWith("event: ")) {
        name = line.slice(7);
      } else if (line.startsWith("data: ") && name !== null) {
        const payload = line.slice(6);
        const eventName = name;
        name = null;
        try {
          // The name lives in the `event:` line, not in the payload — folding it
          // in here is what lets the rest of the app switch on a single tagged
          // union rather than tracking the two halves separately.
          onEvent({ event: eventName, ...JSON.parse(payload) } as PortalEvent);
        } catch {
          // A frame we cannot parse is a frame we cannot act on, but it is not
          // a reason to abandon the rest of the answer.
        }
      }
    }
  }
}
