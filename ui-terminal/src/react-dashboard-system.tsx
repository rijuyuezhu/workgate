import { createRoot } from "react-dom/client";
import { bytes, numeric, percent, rate } from "./react-dashboard-resources";

type Data = Record<string, unknown>;
type Snapshot = { system?: unknown; version?: unknown; generated_at?: unknown } | null;

function record(value: unknown): Data {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Data : {};
}

function text(value: unknown) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function duration(value: unknown) {
  const number = numeric(value);
  if (number === null || number < 0) return "—";
  const seconds = Math.floor(number);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m ${seconds % 60}s`;
}

function timestamp(value: unknown) {
  const seconds = numeric(value);
  if (seconds === null || seconds <= 0) return "Unknown sample time";
  const date = new Date(seconds * 1000);
  return Number.isNaN(date.getTime()) ? "Unknown sample time" : date.toLocaleString();
}

const progress = [
  { key: "cpu_percent", title: "CPU utilization", id: "dashboard-cpu" },
  { key: "memory_percent", title: "Memory pressure", id: "dashboard-memory" },
  { key: "disk_percent", title: "Disk", id: "dashboard-disk" },
] as const;

function SystemDetails({ snapshot }: { snapshot: Snapshot }) {
  const system = record(snapshot?.system);
  const version = record(snapshot?.version);
  const load = numeric(system.load_1m);
  const details = [
    ["Platform", "dashboard-platform", text(version.platform)],
    ["Python", "dashboard-python", text(version.python)],
    ["CPUs", "dashboard-cpu-count", text(system.cpu_count)],
    ["Load (1m)", "dashboard-load", load === null ? "—" : load.toFixed(2)],
    ["Uptime", "dashboard-uptime", duration(system.uptime_s)],
    ["Memory used", "dashboard-memory-used", system.memory_total_bytes === null ? "—" : `${bytes(system.memory_used_bytes)} / ${bytes(system.memory_total_bytes)}`],
    ["Disk used", "dashboard-disk-used", system.disk_total_bytes === null ? "—" : `${bytes(system.disk_used_bytes)} / ${bytes(system.disk_total_bytes)}`],
    ["Network RX", "dashboard-network-rx", rate(system.network_rx_bps)],
    ["Network TX", "dashboard-network-tx", rate(system.network_tx_bps)],
  ];
  return <>
    <div className="dashboard-section-heading">
      <span id="dashboard-generated" className="muted">{snapshot ? timestamp(snapshot.generated_at) : "—"}</span>
    </div>
    <div className="dashboard-resource-list">
      {progress.map(({ key, title, id }) => {
        const value = numeric(system[key]);
        const width = value === null ? 0 : Math.max(0, Math.min(100, value));
        return <div key={key} className="dashboard-resource-row">
          <span>{title}</span>
          <div className={value === null ? "dashboard-resource-track dashboard-resource-missing" : "dashboard-resource-track"}>
            <i id={id + "-bar"} style={{ width: width + "%" }}/>
          </div>
          <strong id={id + "-value"}>{percent(system[key])}</strong>
        </div>;
      })}
    </div>
    <dl className="dashboard-details">
      {details.map(([label, id, value]) => <div key={id}>
        <dt>{label}</dt><dd id={id}>{value}</dd>
      </div>)}
    </dl>
  </>;
}

function mount(host: HTMLElement) {
  const root = createRoot(host);
  return {
    render(snapshot: Snapshot = null) {
      root.render(<SystemDetails snapshot={snapshot} />);
    },
  };
}

export const dashboardSystem = { mount };
