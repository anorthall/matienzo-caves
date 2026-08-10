/**
 * The thread list.
 *
 * Every conversation here is one the server will hand back in full — the
 * transcript is rebuilt from stored turns rather than replayed through the
 * model — so switching between them costs a read and nothing else.
 *
 * The unsaved conversation is the one case with no id. It appears at the top as
 * soon as there is anything to appear for, and is replaced by the real row the
 * moment the server names it on the `start` event.
 */

import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  /** Whether the composer is showing an unsaved conversation. */
  drafting: boolean;
  busy: boolean;
  onNew: () => void;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}

export function Sidebar({
  conversations,
  activeId,
  drafting,
  busy,
  onNew,
  onOpen,
  onDelete,
  onClose,
}: Props) {
  return (
    <aside className="rail" aria-label="Conversations">
      <div className="rail__head">
        <div className="rail__brand">Matienzo Caves</div>
        <button
          type="button"
          className="icon-button icon-button--plain drawer-toggle"
          onClick={onClose}
          aria-label="Close conversations"
        >
          <Cross />
        </button>
      </div>

      <button type="button" className="rail__new" onClick={onNew}>
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <path d="M12 5v14M5 12h14" />
        </svg>
        New chat
      </button>

      <div className="rail__label">Chats</div>

      <ul className="rail__list">
        {drafting ? (
          <li className="thread thread--on">
            <span className="thread__open">New chat</span>
          </li>
        ) : null}
        {conversations.map((conversation) => (
          <li
            key={conversation.id}
            className={`thread${conversation.id === activeId ? " thread--on" : ""}`}
          >
            <button
              type="button"
              className="thread__open"
              onClick={() => onOpen(conversation.id)}
              disabled={busy}
              title={conversation.title ?? "Untitled"}
            >
              {conversation.title ?? "Untitled"}
            </button>
            <button
              type="button"
              className="thread__delete"
              onClick={() => onDelete(conversation.id)}
              aria-label={`Delete “${conversation.title ?? "Untitled"}”`}
            >
              <Cross size={14} />
            </button>
          </li>
        ))}
        {conversations.length === 0 && !drafting ? (
          <li className="rail__empty">No saved chats.</li>
        ) : null}
      </ul>

      <p className="colophon">
        Descriptions from{" "}
        <a href="https://www.matienzocaves.org.uk/" target="_blank" rel="noreferrer noopener">
          matienzocaves.org.uk
        </a>
      </p>
    </aside>
  );
}

function Cross({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}
