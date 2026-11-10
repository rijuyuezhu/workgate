import { createRoot } from "react-dom/client";

type AuditEntry = {
  id?: string;
  tool?: unknown;
  event?: unknown;
  operation?: unknown;
  status?: unknown;
  ok?: boolean;
  duration_ms?: unknown;
  ts?: unknown;
  session?: unknown;
};

function label(value: unknown, fallback: string) {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function auditTimestamp(value: unknown) {
  const date = new Date(Number(value || 0) * 1000);
  return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString();
}

function auditEntryStatus(entry: AuditEntry) {
  const raw = entry.status ? label(entry.status, "recorded").toLowerCase()
    : entry.ok === true ? "success" : entry.ok === false ? "failed" : "recorded";
  return ["success", "failed", "running", "unpaired", "completed", "recorded"].includes(raw)
    ? raw : "recorded";
}

function AuditList({ entries, selectedId, emptyMessage, onSelect }: {
  entries: AuditEntry[];
  selectedId: string;
  emptyMessage: string;
  onSelect: (id: string) => void;
}) {
  if (!entries.length) return <div className="empty-state">{emptyMessage}</div>;
  return <>
    {entries.map((entry) => {
      const id = label(entry.id, "");
      const status = auditEntryStatus(entry);
      const duration = Number.isFinite(Number(entry.duration_ms)) ? `, ${Number(entry.duration_ms)} ms` : "";
      return <button key={id} className="audit-entry" type="button" data-audit-id={id}
        aria-current={entry.id === selectedId} onClick={() => onSelect(id)}>
        <span className="audit-entry-title">{label(entry.tool, label(entry.event, "unknown"))}</span>
        <span className={`audit-entry-meta audit-entry-status-${status}`}>
          {`${label(entry.operation, "other")}: ${status} (${auditTimestamp(entry.ts)}${duration})`}
        </span>
        <span className="audit-entry-session">{label(entry.session, label(entry.event, "record"))}</span>
      </button>;
    })}
  </>;
}

function mount(host: HTMLElement, onSelect: (id: string) => void) {
  const root = createRoot(host);
  return {
    render(entries: AuditEntry[], selectedId: string, emptyMessage: string) {
      root.render(<AuditList entries={entries} selectedId={selectedId}
        emptyMessage={emptyMessage} onSelect={onSelect} />);
    },
  };
}

export const auditLists = { mount };
