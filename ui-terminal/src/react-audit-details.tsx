import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

type RecordValue = Record<string, unknown>;
type Pane = "request" | "result";
type Expansion = "split" | Pane;
type Mode = "tree" | "raw";

function label(value: unknown, fallback: string): string {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function isRecord(value: unknown): value is RecordValue {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function cleanValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cleanValue);
  if (isRecord(value)) {
    return Object.fromEntries(Object.entries(value)
      .map(([key, item]) => [key, cleanValue(item)])
      .filter(([, item]) => item !== undefined));
  }
  return value;
}

function parseJsonString(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return value;
  try { return JSON.parse(trimmed) as unknown; }
  catch { return value; }
}

const envelopeFields = new Set(["ok", "message", "data", "error", "error_type"]);
function unwrapToolEnvelope(value: unknown): unknown {
  if (!isRecord(value) || !("data" in value) ||
      Object.keys(value).some((key) => !envelopeFields.has(key))) return value;
  const data = cleanValue(value.data);
  const message = cleanValue(value.message);
  const error = cleanValue(value.error);
  const errorType = cleanValue(value.error_type);
  if (value.ok === false || error !== undefined && error !== null ||
      errorType !== undefined && errorType !== null) {
    return cleanValue({ message, error, error_type: errorType, data });
  }
  return data === undefined ? message : data;
}

function auditInput(entry: RecordValue): unknown {
  if (entry.input !== undefined) return entry.input;
  if (isRecord(entry.arguments)) return entry.arguments.keyword_args ?? entry.arguments;
  const fallback: RecordValue = {};
  for (const key of ["command", "cwd", "path", "url", "session", "machine"]) {
    if (entry[key] !== undefined) fallback[key] = entry[key];
  }
  return Object.keys(fallback).length ? cleanValue(fallback) : undefined;
}

const metadataFields = new Set([
  "id", "ts", "event", "tool", "node", "operation", "paired", "status",
  "source_events", "call_id", "session", "input", "arguments", "output", "result",
  "related_events", "image_preview", "image_preview_error", "command", "cwd",
  "path", "url", "machine", "ok", "error", "error_type", "exit_code",
  "timed_out", "duration_ms", "stdout", "stderr", "truncated",
]);

function auditOutput(entry: RecordValue): unknown {
  let output: unknown;
  if (entry.output !== undefined) output = entry.output;
  else if (entry.result !== undefined) output = entry.result;
  else {
    const fallback: RecordValue = {};
    for (const key of ["ok", "error", "error_type", "exit_code", "timed_out",
      "duration_ms", "stdout", "stderr", "truncated"]) {
      if (entry[key] !== undefined) fallback[key] = entry[key];
    }
    output = Object.keys(fallback).length ? cleanValue(fallback) : undefined;
  }
  const related = cleanValue(entry.related_events);
  const supplemental = Object.fromEntries(Object.entries(entry)
    .filter(([key]) => !metadataFields.has(key)));
  const details = Object.keys(supplemental).length ? cleanValue(supplemental) : undefined;
  if (related === undefined && details === undefined) return output;
  if (output === undefined && related === undefined) return details;
  return cleanValue({ result: unwrapToolEnvelope(output), related_events: related, details });
}

function auditValue(value: unknown, emptyMessage: string) {
  const parsed = typeof value === "string" ? parseJsonString(value) : value;
  const cleaned = cleanValue(unwrapToolEnvelope(parsed));
  if (cleaned === undefined) return { parsed: cleaned, source: emptyMessage, language: "plain" };
  if (typeof cleaned === "string") return { parsed: cleaned, source: cleaned, language: "plain" };
  return { parsed: cleaned, source: JSON.stringify(cleaned, null, 2), language: "json" };
}

function AuditTree({ value, depth = 0, path = [] }: {
  value: unknown; depth?: number; path?: string[];
}): ReactNode {
  const initialize = useCallback((node: HTMLDetailsElement | null) => {
    if (node) node.open = depth === 0;
  }, [depth]);
  if (value === null || typeof value !== "object") {
    return <span className={`audit-tree-value audit-tree-${value === null ? "null" : typeof value}`}>
      {JSON.stringify(value)}
    </span>;
  }
  const entries = Object.entries(value);
  return <details ref={initialize} className="audit-tree-branch" data-path={JSON.stringify(path)}>
    <summary>{Array.isArray(value) ? "Array" : "Object"} ({entries.length})</summary>
    <div className="audit-tree-children">
      {entries.map(([key, child]) => <div className="audit-tree-row" key={key}>
        <span className="audit-tree-key">{key}:</span>
        <AuditTree value={child} depth={depth + 1} path={[...path, key]} />
      </div>)}
    </div>
  </details>;
}

function AuditValue({ value, emptyMessage, heading }: {
  value: unknown; emptyMessage: string; heading: string;
}) {
  const { parsed, source, language } = auditValue(value, emptyMessage);
  const hasTree = parsed !== null && typeof parsed === "object";
  const [mode, setMode] = useState<Mode>(hasTree ? "tree" : "raw");
  const [copyLabel, setCopyLabel] = useState("Copy");
  const pre = useRef<HTMLPreElement>(null);
  const visibleMode = hasTree ? mode : "raw";
  useLayoutEffect(() => { if (!hasTree) setMode("raw"); }, [hasTree]);
  useLayoutEffect(() => {
    if (!pre.current) return;
    if (window.WorkgateSyntax) window.WorkgateSyntax.render(pre.current, source, language);
    else pre.current.textContent = source;
    setCopyLabel("Copy");
  }, [source, language]);
  return <>
    <div className="audit-value-toolbar" aria-label={`${heading} view controls`}>
      <button type="button" hidden={!hasTree} aria-pressed={visibleMode === "tree"}
        onClick={() => setMode("tree")}>Tree</button>
      <button type="button" aria-pressed={visibleMode === "raw"}
        onClick={() => setMode("raw")}>Raw</button>
      <button type="button" onClick={async () => {
        try { await navigator.clipboard.writeText(source); setCopyLabel("Copied"); }
        catch { setCopyLabel("Copy failed"); }
      }}>{copyLabel}</button>
    </div>
    <div className="audit-value-content">
      <div hidden={visibleMode !== "tree"}>
        {hasTree ? <AuditTree value={parsed} /> : null}
      </div>
      <pre className="audit-detail-json" ref={pre} hidden={visibleMode !== "raw"} />
    </div>
  </>;
}

function AuditPanel({ pane, hostId, expanded, onToggle, children }: {
  pane: Pane; hostId: string; expanded: Expansion;
  onToggle: (pane: Pane) => void; children: ReactNode;
}) {
  const title = pane === "request" ? "Call request" : "Call result";
  const collapsed = expanded !== "split" && expanded !== pane;
  const contentId = `${hostId}-${pane}-content`;
  return <section className="audit-call-panel" aria-label={title} data-collapsed={String(collapsed)}>
    <button type="button" className="audit-call-panel-heading" aria-controls={contentId}
      aria-expanded={!collapsed} onClick={() => onToggle(pane)}>{title}</button>
    <div className="audit-call-panel-body" id={contentId} tabIndex={0}
      aria-label={`${title} content`} hidden={collapsed}>{children}</div>
  </section>;
}

function AuditDetail({ entry, host, formatFileBytes }: {
  entry: RecordValue; host: HTMLElement; formatFileBytes: (value: unknown) => string;
}) {
  const [expanded, setExpanded] = useState<Expansion>("split");
  useLayoutEffect(() => { host.dataset.expandedPane = expanded; }, [host, expanded]);
  const toggle = (pane: Pane) => setExpanded((current) => current === pane ? "split" : pane);
  const preview = isRecord(entry.image_preview) ? entry.image_preview : null;
  return <>
    <AuditPanel pane="request" hostId={host.id} expanded={expanded} onToggle={toggle}>
      <AuditValue value={auditInput(entry)} emptyMessage="No input recorded" heading="Call request" />
    </AuditPanel>
    <AuditPanel pane="result" hostId={host.id} expanded={expanded} onToggle={toggle}>
      {preview && preview.data_base64 && preview.mime_type ? <figure className="audit-image-preview">
        <img alt={label(preview.path, "Audited image result")}
          src={`data:${preview.mime_type};base64,${preview.data_base64}`} />
        <figcaption>{label(preview.path, "image result")} ({formatFileBytes(preview.bytes)})</figcaption>
      </figure> : entry.image_preview_error ? <div className="audit-preview-error">
        Image preview unavailable: {label(entry.image_preview_error, "")}
      </div> : null}
      <AuditValue value={auditOutput(entry)} emptyMessage="No output recorded" heading="Call result" />
    </AuditPanel>
  </>;
}

function mount(host: HTMLElement, formatFileBytes: (value: unknown) => string) {
  const root = createRoot(host);
  return {
    renderEntry(entry: RecordValue) {
      const id = label(entry.id, "");
      if (host.dataset.auditEntryId !== id) host.dataset.auditEntryId = id;
      root.render(<AuditDetail key={id} entry={entry} host={host} formatFileBytes={formatFileBytes} />);
    },
    renderMessage(message: string) {
      delete host.dataset.auditEntryId;
      delete host.dataset.expandedPane;
      root.render(<div className="empty-state audit-detail-message">{message}</div>);
    },
  };
}

export const auditDetails = { mount };

declare global {
  interface Window {
    WorkgateSyntax?: { render: (target: HTMLElement, source: string, language: string) => void };
  }
}
