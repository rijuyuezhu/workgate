import { createRoot } from "react-dom/client";

type Executor = Record<string, unknown>;
type Alert = Record<string, unknown>;
type Task = Record<string, unknown>;

type Model = {
  executors: Executor[] | null;
  executorId: string;
  loading: boolean;
  alerts: Alert[] | null;
  alertMessage: string;
  tasks: Task[] | null;
};
type Events = {
  navigate: (view: "executors" | "audit" | "tasks") => void;
  selectExecutor: (id: string) => void;
  openTask: (id: string) => void;
};

function label(value: unknown, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

const concerning = ["failed", "critical", "warning", "attention"];

function OverviewLists({ model, events }: { model: Model; events: Events }) {
  const activeTasks = model.tasks?.filter(
    (task) => task.status !== "completed" && task.status !== "cancelled",
  ).slice(0, 4);
  return <>
    <article className="dashboard-section dashboard-executors-section">
      <div className="dashboard-section-heading"><div><h3>Executors</h3></div>
        <button className="dashboard-panel-link" type="button" onClick={() => events.navigate("executors")}>Manage executors →</button>
      </div>
      <div id="dashboard-machine-list" className="dashboard-machine-list" aria-live="polite">
        {!model.executors ? "Loading executors…" : model.executors.length === 0 ?
          <div className="empty-state">No executors paired.</div> :
          model.executors.map((executor) => {
            const id = label(executor.executor_id, "");
            if (!id) return null;
            const state = label(executor.status, "offline");
            const name = label(executor.name, id);
            const runtime = executor.runtime && typeof executor.runtime === "object"
              ? executor.runtime as Record<string, unknown> : {};
            return <button key={id} className="executor-band dashboard-machine-row" type="button"
              aria-label={`Select ${name}`} data-executor-id={id}
              aria-current={id === model.executorId} disabled={state !== "online"}
              onClick={() => {
                if (!model.loading && id !== model.executorId) events.selectExecutor(id);
              }}>
              <span className="executor-band-mark">{name.slice(0, 1).toUpperCase()}</span>
              <span className="executor-band-main"><strong>{name}</strong>
                <small>{label(runtime.workgate_version, "Executor")}</small>
              </span>
              <span className="executor-band-state" data-status={state}>{state}</span>
            </button>;
          })}
      </div>
    </article>
    <article className="dashboard-section dashboard-alerts-section">
      <div className="dashboard-section-heading"><div><h3>Needs attention</h3></div>
        <button className="dashboard-panel-link" type="button" onClick={() => events.navigate("audit")}>View audit →</button>
      </div>
      <div id="dashboard-alerts" className="dashboard-list" aria-live="polite">
        {!model.alerts || model.alerts.length === 0 ?
          <div className="empty-state">{model.alertMessage}</div> :
          model.alerts.map((alert, index) => {
            const kind = label(alert.severity, "info");
            return <article key={index} className={`dashboard-list-item dashboard-list-${kind}`}>
              <span className="dashboard-event-icon" aria-hidden="true">{concerning.includes(kind) ? "!" : "✓"}</span>
              <div className="dashboard-list-header"><strong>{label(alert.title, "Dashboard alert")}</strong>
                <span>{label(alert.node, model.executorId)}</span>
              </div>
              <p>{label(alert.detail, "")}</p>
            </article>;
          })}
      </div>
    </article>
    <article className="dashboard-section dashboard-workloads-section">
      <div className="dashboard-section-heading"><div><h3>Active tasks</h3></div>
        <button className="dashboard-panel-link" type="button" onClick={() => events.navigate("tasks")}>View all →</button>
      </div>
      <div id="dashboard-task-list" className="dashboard-task-list" aria-live="polite">
        {!activeTasks ? <div className="empty-state">Loading tasks…</div> :
          activeTasks.length === 0 ?
            <div className="empty-state">No active tasks.</div> :
            activeTasks.map((task) => {
              const id = label(task.task_id, "");
              return <button key={id} className="dashboard-task-row" type="button" data-task-id={id}
                onClick={() => events.openTask(id)}>
                <strong>{label(task.objective, label(task.task_id))}</strong>
                <span className="dashboard-task-state">{label(task.status)}</span>
              </button>;
            })}
      </div>
    </article>
  </>;
}

function mount(host: HTMLElement, events: Events) {
  const root = createRoot(host);
  let model: Model = {
    executors: null, executorId: "", loading: false,
    alerts: null, alertMessage: "No telemetry loaded.", tasks: null,
  };
  function render(update: Partial<Model> = {}) {
    model = { ...model, ...update };
    root.render(<OverviewLists model={model} events={events} />);
  }
  render();
  return {
    renderExecutors(executors: Executor[], executorId: string, loading: boolean) {
      render({ executors, executorId, loading });
    },
    renderSelection(executorId: string, loading: boolean) {
      render({ executorId, loading });
    },
    renderAlerts(alerts: Alert[] | null, alertMessage: string) {
      render({ alerts, alertMessage });
    },
    renderTasks(tasks: Task[]) { render({ tasks }); },
  };
}

export const overviewLists = { mount };
