import { createRoot } from "react-dom/client";

type Executor = {
  executor_id: string;
  name?: string;
  online?: boolean;
  draining?: boolean;
  revoked_at?: number | null;
  runtime?: { workgate_version?: string; ownership?: string; platform?: string };
  runtime_update_required?: boolean;
  required_workgate_version?: string;
  session_admission?: boolean;
  session_admission_reasons?: string[];
  active_sessions?: number;
  queued_commands?: number;
  offered_commands?: number;
  command_limit?: number;
  capabilities?: string[];
  created_at?: number;
  last_seen_at?: number;
};
type Detail = {
  control: boolean;
  available: boolean;
  stale: boolean;
  onlineCount: number;
  executor: Executor | null;
  disabled: boolean;
};
type Action = "reset" | "drain" | "rename" | "revoke";

function timestamp(value: unknown, fallback = "Never") {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return fallback;
  const date = new Date(seconds * 1000);
  return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
}

function selectedStatus(executor: Executor) {
  return executor.revoked_at ? "revoked" : executor.draining ? "draining" : executor.online ? "online" : "offline";
}

function Header({ detail, onAction }: { detail: Detail; onAction: (action: Action) => void }) {
  const { executor, control, disabled } = detail;
  const name = control ? "Control" : executor ? executor.name || executor.executor_id : "No executor selected";
  const status = control ? detail.stale ? "Stale" : detail.available ? "Available" : "Unavailable"
    : executor ? selectedStatus(executor) : "Select an executor to inspect it";
  return <div className="executor-details-header">
    <div><strong id="executor-detail-name">{name}</strong>
      <span id="executor-detail-status" className="muted">{status}</span>
    </div>
    <div className="executor-detail-actions" hidden={control}>
      <button id="executor-reset" type="button" disabled={disabled} onClick={() => onAction("reset")}>Reset queued</button>
      <button id="executor-drain" type="button" disabled={disabled} onClick={() => onAction("drain")}>{executor?.draining ? "Resume" : "Drain"}</button>
      <button id="executor-rename-open" type="button" disabled={disabled} onClick={() => onAction("rename")}>Rename</button>
      <button id="executor-revoke-open" className="danger-action" type="button" disabled={disabled} onClick={() => onAction("revoke")}>Revoke</button>
    </div>
  </div>;
}

function Row({ label, id, value }: { label: string; id?: string; value: string }) {
  return <div><dt>{label}</dt><dd id={id}>{value}</dd></div>;
}

function Overview({ detail }: { detail: Detail }) {
  const executor = detail.executor;
  const empty = "—";
  return <>
    <div id="executor-control-overview" hidden={!detail.control}>
      <dl className="executor-detail-grid">
        <Row label="Role" value="Control" />
        <Row id="executor-control-connection" label="Connection" value={detail.stale ? "Stale" : detail.available ? "Connected" : "Unavailable"} />
        <Row id="executor-control-online" label="Online Executors" value={String(detail.onlineCount)} />
      </dl>
    </div>
    <dl id="executor-detail-grid" className="executor-detail-grid" hidden={detail.control}>
      <Row label="Executor ID" id="executor-detail-id" value={executor?.executor_id ?? empty} />
      <Row label="Workgate version" id="executor-detail-version" value={executor ? executor.runtime?.workgate_version || "Not reported" : empty} />
      <Row label="Runtime owner" id="executor-detail-runtime-owner" value={executor ? executor.runtime?.ownership || "Not reported" : empty} />
      <Row label="Compatibility" id="executor-detail-compatibility" value={executor ? !executor.runtime ? "Not reported" : executor.runtime_update_required ? `Update required: ${executor.required_workgate_version}` : "Compatible" : empty} />
      <Row label="Platform" id="executor-detail-platform" value={executor ? executor.runtime?.platform || "Not reported" : empty} />
      <Row label="Session admission" id="executor-detail-admission" value={executor ? executor.session_admission ? "Eligible" : (executor.session_admission_reasons || []).join(", ") || "Not eligible" : empty} />
      <Row label="Active sessions" id="executor-detail-sessions" value={executor ? String(executor.active_sessions || 0) : empty} />
      <Row label="Commands" id="executor-detail-commands" value={executor ? `${executor.queued_commands || 0} queued, ${executor.offered_commands || 0} offered (limit ${executor.command_limit || 0})` : empty} />
      <Row label="Capabilities" id="executor-detail-capabilities" value={executor ? (executor.capabilities || []).join(", ") || "None reported" : empty} />
      <Row label="Created" id="executor-detail-created" value={executor ? timestamp(executor.created_at, "Unknown") : empty} />
      <Row label="Last seen" id="executor-detail-last-seen" value={executor ? executor.online ? "Now" : timestamp(executor.last_seen_at) : empty} />
    </dl>
  </>;
}

function mount(headerHost: HTMLElement, overviewHost: HTMLElement, onAction: (action: Action) => void) {
  const headerRoot = createRoot(headerHost);
  const overviewRoot = createRoot(overviewHost);
  let detail: Detail = { control: true, available: false, stale: false, onlineCount: 0, executor: null, disabled: true };
  function render() {
    headerRoot.render(<Header detail={detail} onAction={onAction} />);
    overviewRoot.render(<Overview detail={detail} />);
  }
  render();
  return {
    renderDetails(update: Omit<Detail, "disabled">) {
      detail = { ...detail, ...update };
      render();
    },
    renderControls(disabled: boolean) {
      detail = { ...detail, disabled };
      headerRoot.render(<Header detail={detail} onAction={onAction} />);
    },
  };
}

export const executorDetail = { mount };
