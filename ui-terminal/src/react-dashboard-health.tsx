import { createRoot } from "react-dom/client";

type HealthKind = "unknown" | "healthy" | "attention" | "critical";
type HealthModel = {
  bannerHealth: HealthKind;
  cardHealth: HealthKind;
  message: string;
  detail: string;
  executorTotal: string;
  executorOnline: string;
  auditTotal: string;
  auditDetail: string;
};
type Snapshot = {
  health?: unknown;
  alerts?: unknown;
  audit_total_24h?: unknown;
  audit_failed_24h?: unknown;
};

function text(value: unknown, fallback: string) {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

const healthMessages = {
  healthy: "All systems operational",
  attention: "Attention needed",
  critical: "Critical issue detected",
};

function HealthBanner({ model, onAudit }: { model: HealthModel; onAudit: () => void }) {
  return <div className="dashboard-health-banner" data-health={model.bannerHealth} aria-live="polite">
    <div className="dashboard-status-illustration" aria-hidden="true">
      <div className="dashboard-status-ring" />
      <div className="dashboard-status-ring inner" />
      <div className="dashboard-status-circle">
        <svg className="dashboard-health-check" viewBox="0 0 24 24"><path d="m7 12 3 3 7-7" /></svg>
        <svg className="dashboard-health-warning" viewBox="0 0 24 24"><path d="M12 4 3 20h18zM12 9v5M12 17h.01" /></svg>
      </div>
    </div>
    <article id="dashboard-health-card" className={`dashboard-card dashboard-health-card${model.cardHealth === "unknown" ? "" : ` dashboard-health-${model.cardHealth}`}`}>
      <span>System health</span>
      <strong id="dashboard-health">{model.message}</strong>
      <small id="dashboard-health-detail">{model.detail}</small>
    </article>
    <div className="dashboard-health-stats">
      <div><strong id="executor-target-total">{model.executorTotal}</strong><span>Executors</span></div>
      <div><strong id="executor-target-online">{model.executorOnline}</strong><span>Online</span></div>
      <button className="dashboard-audit-shortcut" type="button" aria-label="Open Audit records" onClick={onAudit}>
        <strong id="dashboard-audit-total">{model.auditTotal}</strong>
        <span>Audit (24h) <span aria-hidden="true">↗</span></span>
        <small id="dashboard-audit-detail" className="visually-hidden">{model.auditDetail}</small>
      </button>
    </div>
  </div>;
}

function mount(bannerHost: HTMLElement, statusHost: HTMLElement, onAudit: () => void) {
  const bannerRoot = createRoot(bannerHost);
  const statusRoot = createRoot(statusHost);
  let model: HealthModel = {
    bannerHealth: "unknown", cardHealth: "unknown", message: "—",
    detail: "Waiting for telemetry", executorTotal: "—", executorOnline: "—",
    auditTotal: "—", auditDetail: "—",
  };
  function render(patch: Partial<HealthModel> = {}) {
    model = { ...model, ...patch };
    bannerRoot.render(<HealthBanner model={model} onAudit={onAudit} />);
  }
  function renderStatus(status: string) {
    statusRoot.render(status);
  }
  render();
  renderStatus("Not loaded");
  return {
    renderStatus,
    renderTargets(targets: { status?: unknown }[] | null) {
      render({
        executorTotal: targets === null ? "—" : String(targets.length),
        executorOnline: targets === null ? "—" : String(targets.filter((row) => row.status === "online").length),
      });
    },
    renderSnapshot(snapshot: Snapshot) {
      const kind: HealthKind = snapshot.health === "healthy" || snapshot.health === "critical"
        ? snapshot.health : "attention";
      const alerts = Array.isArray(snapshot.alerts) ? snapshot.alerts : [];
      render({
        bannerHealth: kind,
        cardHealth: kind,
        message: healthMessages[kind],
        detail: alerts.length ? `${alerts.length} active alert${alerts.length === 1 ? "" : "s"}.` : "No active alerts.",
        auditTotal: text(snapshot.audit_total_24h, "0"),
        auditDetail: `${text(snapshot.audit_failed_24h, "0")} failed in 24h`,
      });
    },
    renderError(executorId: string) {
      render({ cardHealth: "attention", message: "unavailable", detail: `Telemetry for ${executorId} could not be loaded` });
    },
    reset() {
      render({
        bannerHealth: "unknown", cardHealth: "unknown", message: "—",
        detail: "Waiting for telemetry", auditTotal: "—", auditDetail: "No activity loaded",
      });
    },
  };
}

export const dashboardHealth = { mount };
