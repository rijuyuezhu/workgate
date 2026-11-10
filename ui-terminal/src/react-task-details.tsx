import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type TaskSummary = {
  objective: string;
  status: string;
  summary: string;
  nextAction: string;
  findings: string;
  blockers: string;
  hasFindings: boolean;
  hasBlockers: boolean;
};

type SessionSummary = {
  executor: string;
  title: string;
  status: string;
  state: string;
  taskCreated: string;
  sessionId: string;
  executorId: string;
  workdir: string;
  created: string;
  updated: string;
};

const initialTask: TaskSummary = {
  objective: "—", status: "", summary: "—", nextAction: "—",
  findings: "—", blockers: "—", hasFindings: false, hasBlockers: false,
};
const initialSession: SessionSummary = {
  executor: "—", title: "No session selected",
  status: "Select a retained execution session to inspect it", state: "other",
  taskCreated: "", sessionId: "—", executorId: "—", workdir: "—",
  created: "—", updated: "—",
};

function TaskLead({ model }: { model: TaskSummary }) {
  const status = ["active", "completed", "cancelled", "blocked"].includes(model.status)
    ? model.status : "other";
  return <>
    <div className="task-progress-objective">
      <span>Objective</span>
      <strong id="task-objective">{model.objective}</strong>
    </div>
    <span id="task-status" className="task-status-pill" data-status={status}>{model.status}</span>
  </>;
}

function TaskGrid({ model }: { model: TaskSummary }) {
  return <>
    <div className="task-progress-summary"><dt>Summary</dt><dd id="task-summary">{model.summary}</dd></div>
    <div className="task-progress-next"><dt>Next action</dt><dd id="task-next-action">{model.nextAction}</dd></div>
    <div className="task-progress-findings" hidden={!model.hasFindings}>
      <dt>Findings</dt><dd id="task-findings">{model.findings}</dd>
    </div>
    <div className="task-progress-blockers" hidden={!model.hasBlockers}>
      <dt>Blockers</dt><dd id="task-blockers">{model.blockers}</dd>
    </div>
  </>;
}

function SessionHeading({ model }: { model: SessionSummary }) {
  return <>
    <div className="session-detail-executor-identity">
      <span id="session-detail-executor-avatar" className="executor-band-mark" aria-hidden="true">
        {model.executor.slice(0, 1).toUpperCase()}
      </span>
      <strong id="session-detail-executor">{model.executor}</strong>
    </div>
    <div className="session-detail-label">
      <strong id="session-detail-title">{model.title}</strong>
      <span id="session-detail-status" className="muted" data-state={model.state}>{model.status}</span>
      <span id="session-detail-task-created" className="muted" hidden={!model.taskCreated}>
        {model.taskCreated}
      </span>
    </div>
  </>;
}

function SessionMetadata({ model }: { model: SessionSummary }) {
  return <>
    <div><dt>Session ID</dt><dd id="session-detail-id">{model.sessionId}</dd></div>
    <div><dt>Executor ID</dt><dd id="session-detail-executor-id">{model.executorId}</dd></div>
    <div><dt>Workdir</dt><dd id="session-detail-workdir">{model.workdir}</dd></div>
    <div><dt>Created</dt><dd id="session-detail-created">{model.created}</dd></div>
    <div><dt>Last activity</dt><dd id="session-detail-updated">{model.updated}</dd></div>
  </>;
}

function mount(taskLead: HTMLElement, taskGrid: HTMLElement,
  sessionHeading: HTMLElement, sessionMetadata: HTMLElement) {
  const leadRoot = createRoot(taskLead);
  const gridRoot = createRoot(taskGrid);
  const headingRoot = createRoot(sessionHeading);
  const metadataRoot = createRoot(sessionMetadata);
  return {
    renderTask(model: TaskSummary = initialTask) {
      flushSync(() => {
        leadRoot.render(<TaskLead model={model} />);
        gridRoot.render(<TaskGrid model={model} />);
      });
    },
    renderSession(model: SessionSummary = initialSession) {
      flushSync(() => {
        headingRoot.render(<SessionHeading model={model} />);
        metadataRoot.render(<SessionMetadata model={model} />);
      });
    },
  };
}

export const taskDetails = { mount };
