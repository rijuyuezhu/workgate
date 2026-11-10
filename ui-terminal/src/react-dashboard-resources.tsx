import { createRoot } from "react-dom/client";

type ResourceData = Record<string, unknown>;
type Sample = { cpu: number | null; memory: number | null; disk: number | null };

function numeric(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function percent(value: unknown) {
  const n = numeric(value);
  return n === null ? "—" : n.toFixed(1) + "%";
}

function bytes(value: unknown) {
  const n = numeric(value);
  if (n === null || n < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let scaled = n;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit++;
  }
  const digits = scaled >= 100 || unit === 0 ? 0 : scaled >= 10 ? 1 : 2;
  return scaled.toFixed(digits) + " " + units[unit];
}

function rate(value: unknown) {
  const amount = bytes(value);
  return amount === "—" ? amount : amount + "/s";
}

function Trend({ samples, name }: { samples: Array<number | null>; name: string }) {
  const valid = samples
    .map((value, index) => ({ value, index }))
    .filter((point): point is { value: number; index: number } =>
      typeof point.value === "number" && Number.isFinite(point.value));
  const last = Math.max(1, samples.length - 1);
  const points = valid.length < 2 ? "" : valid.map(({ value, index }) => {
    const x = (index / last) * 100;
    const y = 30 - Math.max(0, Math.min(1, value / 100)) * 26;
    return x.toFixed(2) + "," + y.toFixed(2);
  }).join(" ");
  return <svg
    id={"dashboard-" + name + "-trend"}
    className="dashboard-sparkline"
    viewBox="0 0 100 32"
    role="img"
    aria-label={"Recent " + name + " trend"}
  >
    {points && <polyline points={points} fill="none" vectorEffect="non-scaling-stroke" />}
  </svg>;
}

const metrics = [
  {
    key: "cpu", title: "CPU usage", color: "violet", valueKey: "cpu_percent" as const,
    icon: <><path d="M8 2v3M16 2v3M8 19v3M16 19v3M2 8h3M2 16h3M19 8h3M19 16h3"/><rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 9h6v6H9z"/></>,
    foot: (system: ResourceData) => {
      const load = numeric(system.load_1m);
      return [load === null ? "Load —" : "Load " + load.toFixed(2), String(system.cpu_count ?? "—") + " cores"];
    },
  },
  {
    key: "memory", title: "Memory usage", color: "blue", valueKey: "memory_percent" as const,
    icon: <><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 8h8M8 12h8M8 16h5"/></>,
    foot: (system: ResourceData) => [bytes(system.memory_used_bytes) + " used", bytes(system.memory_total_bytes) + " total"],
  },
  {
    key: "disk", title: "Workspace disk", color: "amber", valueKey: "disk_percent" as const,
    icon: <><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.66 3.13 3 7 3s7-1.34 7-3V6M5 12v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6"/></>,
    foot: (system: ResourceData) => [bytes(system.disk_used_bytes) + " used", bytes(system.disk_total_bytes) + " total"],
  },
] as const;

function ResourceCards({
  system, history, live,
}: {
  system: ResourceData;
  history: Sample[];
  live: string;
}) {
  const rx = numeric(system.network_rx_bps);
  const tx = numeric(system.network_tx_bps);
  const total = rx === null || tx === null ? null : rx + tx;
  return <>
    {metrics.map((metric) => {
      const foot = metric.foot(system);
      const samples = history.map((sample) => sample[metric.key]);
      return <article key={metric.key} className="dashboard-card dashboard-metric-card" data-resource={metric.key}>
        <div className="dashboard-metric-head">
          <span className={"dashboard-metric-icon " + metric.color}>
            <svg viewBox="0 0 24 24">{metric.icon}</svg>
          </span>
          <span className="dashboard-live">{live}</span>
        </div>
        <span>{metric.title}</span>
        <strong id={"dashboard-" + metric.key}>{percent(system[metric.valueKey])}</strong>
        <Trend name={metric.key} samples={samples} />
        <div className="dashboard-metric-foot">
          <span id={"dashboard-" + metric.key + "-foot-" + (metric.key === "cpu" ? "load" : "used")}>{foot[0]}</span>
          <span id={"dashboard-" + metric.key + "-foot-" + (metric.key === "cpu" ? "cores" : "total")}>{foot[1]}</span>
        </div>
      </article>;
    })}
    <article className="dashboard-card dashboard-metric-card dashboard-network-card" data-resource="network">
      <div className="dashboard-metric-head">
        <span className="dashboard-metric-icon green">
          <svg viewBox="0 0 24 24"><path d="M12 20V10M7 15l5 5 5-5M4 4h16"/></svg>
        </span>
        <span className="dashboard-live positive">{live}</span>
      </div>
      <span>Network throughput</span>
      <strong id="dashboard-network" className="visually-hidden">
        {rate(total)}
      </strong>
      <div className="dashboard-network-values">
        <div><span className="download-arrow">↓</span><strong id="dashboard-network-down">{bytes(rx)}</strong><small>received</small></div>
        <div><span className="upload-arrow">↑</span><strong id="dashboard-network-up">{bytes(tx)}</strong><small>sent</small></div>
      </div>
      <div className="dashboard-network-bar" aria-label="Relative incoming and outgoing traffic">
        <span id="dashboard-network-rx-bar" style={{ width: total !== null && total > 0 && rx !== null ? (100 * rx / total) + "%" : "0%" }} />
        <i id="dashboard-network-tx-bar" style={{ width: total !== null && total > 0 && tx !== null ? (100 * tx / total) + "%" : "0%" }} />
      </div>
      <div className="dashboard-metric-foot"><span>Downlink</span><span>Uplink</span></div>
    </article>
  </>;
}

function mount(host: HTMLElement) {
  const root = createRoot(host);
  return {
    render(system: ResourceData = {}, history: Sample[] = [], live = "—") {
      root.render(<ResourceCards system={system} history={history} live={live} />);
    },
  };
}

export const dashboardResources = { mount };
