import { createRoot } from "react-dom/client";

type FleetEntry = {
  key: string;
  name: string;
  label: string;
  status: string;
  resources?: string;
};
type Summary = { total: number | null; online: number | null; revoked: number | null };

function InventoryRow({ entry, selected, onSelect }: {
  entry: FleetEntry;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const status = entry.status === "available" ? "online"
    : entry.status === "unavailable" ? "offline" : entry.status;
  return <button type="button" className={`executor-band executor-row${selected ? " executor-row-selected" : ""}`}
    data-selection={entry.key} aria-pressed={selected} title={entry.name}
    onClick={() => onSelect(entry.key)}>
    <span className="executor-band-mark">{entry.name.slice(0, 1).toUpperCase()}</span>
    <span className="executor-band-main">
      <strong className="executor-band-name">{entry.name}</strong>
      <span className="executor-band-meta">{entry.label}</span>
      <span className="executor-band-resources">{entry.resources || ""}</span>
    </span>
    <span className="executor-band-state" data-status={status}>{entry.status}</span>
  </button>;
}

function InventoryList({ entries, selectedId, onSelect }: {
  entries: FleetEntry[]; selectedId: string; onSelect: (id: string) => void;
}) {
  const [control, ...executors] = entries;
  return <>
    <div className="executor-control-group">
      {control && <InventoryRow key={control.key} entry={control} selected={control.key === selectedId} onSelect={onSelect} />}
    </div>
    <div className="executor-peer-group" hidden={!executors.length}>
      <div className="executor-peer-heading">Executors</div>
      <div className="executor-peer-rows">
        {executors.map((entry) => <InventoryRow key={entry.key} entry={entry}
          selected={entry.key === selectedId} onSelect={onSelect} />)}
      </div>
    </div>
  </>;
}

function InventorySummary({ summary }: { summary: Summary }) {
  return <>
    <span><strong id="executor-total">{summary.total ?? "—"}</strong> executors</span>
    <span><strong id="executor-online">{summary.online ?? "—"}</strong> online</span>
    <span id="executor-revoked-summary" hidden={!summary.revoked}>
      <strong id="executor-revoked">{summary.revoked ?? "—"}</strong> revoked
    </span>
  </>;
}

function mount(listHost: HTMLElement, summaryHost: HTMLElement, onSelect: (id: string) => void) {
  const listRoot = createRoot(listHost);
  const summaryRoot = createRoot(summaryHost);
  summaryRoot.render(<InventorySummary summary={{ total: null, online: null, revoked: null }} />);
  return {
    renderList(entries: FleetEntry[], selectedId: string) {
      listRoot.render(<InventoryList entries={entries} selectedId={selectedId} onSelect={onSelect} />);
    },
    renderSummary(total: number | null, online: number | null, revoked: number | null) {
      summaryRoot.render(<InventorySummary summary={{ total, online, revoked }} />);
    },
  };
}

export const executorInventory = { mount };
