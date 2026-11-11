import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Location = { id: string; name: string; state: string };
type Breadcrumb = { path: string; label: string };

function Locations({ entries, selectedId, onSelect }: {
  entries: Location[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  return <>{entries.map((entry) => <button key={entry.id} type="button"
    className="file-location-row" data-executor-id={entry.id}
    aria-current={entry.id === selectedId} disabled={entry.state !== "online"}
    onClick={() => onSelect(entry.id)}>
    <span className={`file-location-dot ${entry.state === "online" ? "online" : ""}`} />
    <span>{entry.name}</span>
  </button>)}</>;
}

function Breadcrumbs({ entries, currentPath, busy, onNavigate }: {
  entries: Breadcrumb[];
  currentPath: string;
  busy: boolean;
  onNavigate: (path: string) => void;
}) {
  return <>{entries.map((entry) => <button key={entry.path} type="button"
    disabled={busy || entry.path === currentPath}
    onClick={() => onNavigate(entry.path)}>{entry.label}</button>)}</>;
}

function mount(locationsHost: HTMLElement, breadcrumbsHost: HTMLElement, callbacks: {
  executor: (id: string) => void;
  path: (path: string) => void;
}) {
  const locationsRoot = createRoot(locationsHost);
  const breadcrumbsRoot = createRoot(breadcrumbsHost);
  return {
    renderLocations(entries: Location[], selectedId: string) {
      flushSync(() => locationsRoot.render(<Locations entries={entries}
        selectedId={selectedId} onSelect={callbacks.executor} />));
    },
    renderBreadcrumbs(entries: Breadcrumb[], currentPath: string, busy: boolean) {
      flushSync(() => breadcrumbsRoot.render(<Breadcrumbs entries={entries}
        currentPath={currentPath} busy={busy} onNavigate={callbacks.path} />));
    },
  };
}

export const filesNavigation = { mount };
