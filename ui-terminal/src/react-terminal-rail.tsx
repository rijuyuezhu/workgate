import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type ExecutorLocation = { id: string; name: string; state: string };
type ShellEntry = { id: string; name: string; title: string };

type ShellList = {
  executorId: string;
  selectedShellId: string;
  entries: ShellEntry[];
  emptyMessage: string;
};

function Locations({ entries, selectedId, onSelect }: {
  entries: ExecutorLocation[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  return <>{entries.map((entry) => <button type="button" key={entry.id}
    className="terminal-location-row" data-executor-id={entry.id}
    aria-current={entry.id === selectedId} disabled={entry.state !== "online"}
    onClick={() => onSelect(entry.id)}>
    <span className={`terminal-location-dot ${entry.state === "online" ? "online" : ""}`} />
    <span>{entry.name}</span>
  </button>)}</>;
}

function Shells({ model, onSelect }: {
  model: ShellList;
  onSelect: (id: string) => void;
}) {
  if (!model.entries.length) return <div className="empty-state">{model.emptyMessage}</div>;
  return <>{model.entries.map((shell) => <button key={`${model.executorId}:${shell.id}`}
    type="button" className="terminal-session" title={shell.title}
    aria-current={shell.id === model.selectedShellId} onClick={() => onSelect(shell.id)}>
    <svg className="terminal-session-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 4h18v16H3z m4 5 3 3-3 3 M13 16h4" />
    </svg>
    {shell.name}
  </button>)}</>;
}

function mount(locationsHost: HTMLElement, sessionsHost: HTMLElement, callbacks: {
  executor: (id: string) => void;
  shell: (id: string) => void;
}) {
  const locationsRoot = createRoot(locationsHost);
  const sessionsRoot = createRoot(sessionsHost);
  return {
    renderExecutors(entries: ExecutorLocation[], selectedId: string) {
      flushSync(() => locationsRoot.render(<Locations entries={entries}
        selectedId={selectedId} onSelect={callbacks.executor} />));
    },
    renderSessions(model: ShellList) {
      flushSync(() => sessionsRoot.render(<Shells model={model}
        onSelect={callbacks.shell} />));
    },
  };
}

export const terminalRail = { mount };
