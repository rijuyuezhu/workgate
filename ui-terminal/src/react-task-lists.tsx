import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";

type TaskRow = { id: string; title: string; status: string; meta: string };
type SessionRow = {
  id: string;
  title: string;
  meta: string;
  executor: string;
  terminated?: boolean;
  tooltip: string;
};
type TaskModel = {
  tasks: TaskRow[];
  unattached: SessionRow[];
  selectedTaskId: string;
  selectedSessionId: string;
  emptyMessage: string;
};
type SessionModel = {
  sessions: SessionRow[];
  selectedSessionId: string;
  emptyMessage: string;
};
type Events = {
  task: (id: string) => void;
  unattached: (id: string) => void;
  session: (id: string) => void;
};

function SessionRowButton({ row, selected, unattached, onSelect }: {
  row: SessionRow;
  selected: boolean;
  unattached: boolean;
  onSelect: (id: string) => void;
}) {
  return <button key={row.id} type="button"
    className={`session-entry${unattached ? " unattached-session-entry" : ""}`}
    data-session-id={row.id} aria-current={selected} title={row.tooltip}
    onClick={() => onSelect(row.id)}>
    <span className="executor-band-mark" aria-hidden="true">{row.executor.slice(0, 1).toUpperCase()}</span>
    <span className="session-entry-main">
      <strong>{row.title}</strong>
      <span className={`session-entry-meta${row.terminated ? " session-entry-terminated" : ""}`}>
        {row.meta}
      </span>
    </span>
  </button>;
}

function TaskList({ model, events }: { model: TaskModel; events: Events }) {
  if (!model.tasks.length && !model.unattached.length) {
    return <div className="empty-state">{model.emptyMessage}</div>;
  }
  return <>
    {model.tasks.map((task) => <button type="button" key={`task:${task.id}`}
      className="session-entry task-entry" data-task-id={task.id}
      aria-current={task.id === model.selectedTaskId} onClick={() => events.task(task.id)}>
      <span className="task-entry-heading">
        <strong>{task.title}</strong>
        <span className="task-entry-status" data-status={task.status}>{task.status}</span>
      </span>
      <span className="session-entry-meta">{task.meta}</span>
    </button>)}
    {model.unattached.length ? <div className="session-group-label">Unattached sessions</div> : null}
    {model.unattached.map((session) => <SessionRowButton key={`session:${session.id}`} row={session}
      unattached selected={!model.selectedTaskId && session.id === model.selectedSessionId}
      onSelect={events.unattached} />)}
  </>;
}

function SessionList({ model, onSelect }: { model: SessionModel; onSelect: Events["session"] }) {
  if (!model.sessions.length) return <div className="empty-state">{model.emptyMessage}</div>;
  return <>{model.sessions.map((session) => <SessionRowButton key={session.id} row={session}
    unattached={false} selected={session.id === model.selectedSessionId}
    onSelect={onSelect} />)}</>;
}

function mount(taskHost: HTMLElement, sessionHost: HTMLElement, events: Events) {
  const taskRoot = createRoot(taskHost);
  const sessionRoot = createRoot(sessionHost);
  return {
    renderTasks(model: TaskModel) {
      // Some existing controller paths focus the first remaining Task immediately
      // after deletion. Keep the presentation committed before returning.
      flushSync(() => taskRoot.render(<TaskList model={model} events={events} />));
    },
    renderSessions(model: SessionModel) {
      flushSync(() => sessionRoot.render(<SessionList model={model} onSelect={events.session} />));
    },
  };
}

export const taskLists = { mount };
