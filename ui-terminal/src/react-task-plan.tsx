import { memo, type ReactNode } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Step = { id: string; content: string; status: string; priority: string; index: number };
type Plan = {
  rows: Step[];
  counts: { total: number; open: number; completed: number; skipped: number };
  emptyMessage: string;
  contentMaxLength: number;
};
type Events = {
  content: (id: string, content: string) => void;
  status: (id: string, status: string) => void;
  priority: (id: string, priority: string) => void;
  remove: (id: string) => void;
};

const statuses = ["pending", "in_progress", "blocked", "completed", "skipped"];
const priorities = ["high", "medium", "low"];
function options(known: string[], current: string) {
  return known.includes(current) ? known : [...known, current];
}

function Field({ label, name, children }: {
  label: string; name: string; children: ReactNode;
}) {
  return <label className={`todo-field ${name}`}>
    <span>{label}</span>{children}
  </label>;
}

const PlanStep = memo(function PlanStep({ id, content, status, priority, index, maxLength, events }: Step & {
  maxLength: number; events: Events;
}) {
  return <article className="todo-row" data-todo-id={id} data-status={status}
    data-priority={priority}>
    <div className="todo-row-heading">
      <span className="todo-id" title={id}>Step {index + 1}</span>
      <button type="button" className="todo-remove" onClick={() => events.remove(id)}>Remove</button>
    </div>
    <Field label="Content" name="todo-field-content">
      <input type="text" value={content} maxLength={maxLength} autoComplete="off"
        spellCheck={true} onChange={(event) => events.content(id, event.currentTarget.value)} />
    </Field>
    <Field label="Status" name="todo-field-status">
      <select value={status} onChange={(event) => events.status(id, event.currentTarget.value)}>
        {options(statuses, status).map((value) => <option key={value} value={value}>
          {value.replaceAll("_", " ")}
        </option>)}
      </select>
    </Field>
    <Field label="Priority" name="todo-field-priority">
      <select value={priority} onChange={(event) => events.priority(id, event.currentTarget.value)}>
        {options(priorities, priority).map((value) => <option key={value} value={value}>
          {value.replaceAll("_", " ")}
        </option>)}
      </select>
    </Field>
  </article>;
});

function Summary({ counts }: { counts: Plan["counts"] }) {
  return <>
    <span>{counts.total} total</span><span>{counts.open} open</span>
    <span>{counts.completed} completed</span><span>{counts.skipped} skipped</span>
  </>;
}

function Steps({ model, events }: { model: Plan; events: Events }) {
  if (!model.rows.length) return <div className="empty-state">{model.emptyMessage}</div>;
  return <>{model.rows.map((item) => <PlanStep key={item.id} {...item}
    maxLength={model.contentMaxLength} events={events} />)}</>;
}

function mount(summaryHost: HTMLElement, listHost: HTMLElement, events: Events) {
  const summaryRoot = createRoot(summaryHost);
  const listRoot = createRoot(listHost);
  return {
    render(model: Plan) {
      // The controller focuses a newly added step immediately after rendering.
      flushSync(() => {
        summaryRoot.render(<Summary counts={model.counts} />);
        listRoot.render(<Steps model={model} events={events} />);
      });
    },
  };
}

export const taskPlan = { mount };
