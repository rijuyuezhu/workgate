import { useState } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Action = { op: "remove" } | { op: "set"; value: unknown };
type Field = {
  name: string; section: string; description?: string; type: string;
  file_present: boolean; file_value?: unknown; default_value?: unknown;
  effective_value?: unknown; source?: string; nullable?: boolean; choices?: string[];
};
type Configuration = {
  status: string; fields: Field[]; editable?: boolean; edit_reason?: string;
  config_path?: string; file_changed_since_startup?: boolean;
  file_error?: string; apply_status?: string;
};
type Events = {
  retry: () => void;
  save: (changes: Record<string, Action>) => Promise<void>;
  apply: (progress: (message: string) => void) => Promise<boolean>;
};
type Model = {
  generation: number; state: "blank" | "loading" | "offline" | "error" | "ready";
  message?: string; notice?: string; data?: Configuration; events?: Events;
};

const display = (value: unknown) => value === null ? "null" : value === "" ? '""'
  : typeof value === "string" ? value : JSON.stringify(value) ?? "";
const differs = (a: unknown, b: unknown) => JSON.stringify(a) !== JSON.stringify(b);
function nonDefault(field: Field, action?: Action) {
  return differs(field.effective_value, field.default_value) ||
    (action ? action.op === "set" && differs(action.value, field.default_value)
      : field.file_present && differs(field.file_value, field.default_value));
}

function Editor({ field, action, onStage, onCancel, onInvalid }: {
  field: Field; action?: Action; onStage: (action: Action) => void;
  onCancel: () => void; onInvalid: () => void;
}) {
  const start = action?.op === "set" ? action.value
    : field.file_present ? field.file_value : field.default_value;
  const [value, setValue] = useState(field.type === "json" ? JSON.stringify(start) ?? "null"
    : field.type === "bool" || field.choices?.length ? display(start)
      : start == null ? "" : String(start));
  function set() {
    let next: unknown = value;
    try {
      if (field.type === "bool") next = value === "true";
      else if (field.type === "int" || field.type === "float") {
        next = Number(value);
        if (!value.trim() || !Number.isFinite(next) ||
          (field.type === "int" && !Number.isInteger(next))) throw new Error("Invalid number");
      } else if (field.type === "json") next = JSON.parse(value);
    } catch { onInvalid(); return; }
    onStage({ op: "set", value: next });
  }
  const common = { className: "executor-config-input", value,
    onChange: (event: { currentTarget: { value: string } }) => setValue(event.currentTarget.value) };
  return <div className="executor-config-editor">
    {field.type === "bool" || field.choices?.length ? <select {...common} autoFocus>
      {(field.type === "bool" ? ["true", "false"] : field.choices || []).map((choice) =>
        <option key={choice} value={choice}>{choice}</option>)}
    </select> : field.type === "json" ? <textarea {...common} autoFocus />
      : <input {...common} autoFocus type={field.type === "int" || field.type === "float" ? "number" : "text"}
        step={field.type === "int" ? "1" : field.type === "float" ? "any" : undefined} />}
    <button type="button" onClick={set}>Set</button>
    {(field.file_present || action) && <button type="button" onClick={() => onStage({ op: "remove" })}>Unset</button>}
    {field.nullable && <button type="button" onClick={() => onStage({ op: "set", value: null })}>Set null</button>}
    <button type="button" onClick={onCancel}>Cancel</button>
  </div>;
}

function FieldRow({ field, action, visible, editable, editing, onEdit, onStage, onCancel, onInvalid }: {
  field: Field; action?: Action; visible: boolean; editable: boolean; editing: boolean;
  onEdit: () => void; onStage: (action: Action) => void;
  onCancel: () => void; onInvalid: () => void;
}) {
  const expected = field.file_present ? field.file_value : field.default_value;
  return <div className={`executor-config-row${action ? " executor-config-changed" : ""}`} hidden={!visible}>
    <span className="executor-config-name" title={field.description || ""}>{field.name}</span>
    <div className="executor-config-info">
      <div className="executor-config-transition">
        <span className="executor-config-mobile-heading">Config</span>
        <span className="executor-config-values">
          <code className={`executor-config-value${!field.file_present ? " executor-config-unset" : ""}`}>
            {field.file_present ? display(field.file_value) : "unset"}
          </code>
          <span className="executor-config-arrow" hidden={!action}>→</span>
          <code className={`executor-config-value${action?.op === "remove" ? " executor-config-unset" : ""}`} hidden={!action}>
            {action?.op === "remove" ? "unset" : action?.op === "set" ? display(action.value) : ""}
          </code>
        </span>
      </div>
      {differs(field.effective_value, expected) && <div className="executor-config-effective">
        <span className="executor-config-live-label">In use</span>
        <span className="executor-config-values"><code>{display(field.effective_value)}</code>
          <span className="executor-config-source">{field.source}</span></span>
      </div>}
    </div>
    <div className="executor-config-default"><span className="executor-config-mobile-heading">Default</span>
      <code>{display(field.default_value)}</code></div>
    {editable && <button type="button" className="executor-config-edit" hidden={editing} onClick={onEdit}>Edit</button>}
    {editing && <Editor key={field.name} field={field} action={action} onStage={onStage}
      onCancel={onCancel} onInvalid={onInvalid} />}
  </div>;
}

function Inspector({ data, events, notice }: { data: Configuration; events: Events; notice: string }) {
  const [pending, setPending] = useState<Map<string, Action>>(new Map());
  const [editing, setEditing] = useState("");
  const [changedOnly, setChangedOnly] = useState(false);
  const [nonDefaultOnly, setNonDefaultOnly] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [saveStatus, setSaveStatus] = useState("");
  const [applyStatus, setApplyStatus] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [applying, setApplying] = useState(false);
  const filtering = changedOnly || nonDefaultOnly;
  const visible = (field: Field) => (!changedOnly || pending.has(field.name)) &&
    (!nonDefaultOnly || nonDefault(field, pending.get(field.name)));
  const nonDefaultCount = data.fields.filter((field) => nonDefault(field, pending.get(field.name))).length;
  const visibleCount = data.fields.filter(visible).length;
  const groups = new Map<string, Field[]>();
  for (const field of data.fields) {
    if (!groups.has(field.section)) groups.set(field.section, []);
    groups.get(field.section)!.push(field);
  }
  function stage(field: Field, action: Action) {
    setPending((current) => {
      const next = new Map(current);
      const same = action.op === "remove" ? !field.file_present :
        field.file_present && !differs(action.value, field.file_value);
      if (same) next.delete(field.name); else next.set(field.name, action);
      return next;
    });
    setEditing(""); setSaveStatus("");
  }
  async function save() {
    if (!pending.size || saving) return;
    setSaving(true); setSaveStatus("Saving…");
    try { await events.save(Object.fromEntries(pending)); }
    catch (error) {
      const code = (error as { status?: number }).status;
      setSaveStatus(code === 409 ? "File changed. Reload to edit." : code === 400 ? "Invalid value" : "Save failed");
    } finally { setSaving(false); }
  }
  async function apply() {
    if (applying) return;
    if (pending.size) { setApplyStatus("Save or discard edits first"); return; }
    if (!confirmed) {
      setConfirmed(true);
      setApplyStatus("Affected roles will restart. CLI / ENV changes require restarting Parent.");
      return;
    }
    setApplying(true); setApplyStatus("Requesting restart…");
    try {
      const success = await events.apply(setApplyStatus);
      if (!success) setApplyStatus("Restart not confirmed. Check Parent logs and saved config.");
    } catch (error) {
      setApplyStatus((error as { status?: number }).status === 409
        ? "File changed or restart unavailable. Reload." : "Apply failed");
      setApplying(false);
    }
  }
  const canApply = data.editable && data.file_changed_since_startup &&
    ["healthy", "recovered"].includes(data.apply_status || "");
  const filename = data.config_path ? data.config_path.split(/[\\/]/).pop() : "No config file";
  return <>
    {notice && <div className={notice === "Previous configuration restored" ? "executor-config-warning" : "executor-config-success"}>{notice}</div>}
    <div className="executor-config-meta">
      <span className="executor-config-file" title={data.config_path || ""}>{filename}</span>
      <span className="executor-config-count">{data.fields.length} settings</span>
      {data.file_changed_since_startup && <span className="executor-config-warning">File changed</span>}
      {data.file_error && <span className="executor-config-warning">File unavailable</span>}
      {!data.editable && data.edit_reason && <span className="muted">{data.edit_reason}</span>}
    </div>
    <div className="executor-config-filters">
      <label className="executor-config-filter" hidden={!data.editable}>
        <input type="checkbox" checked={changedOnly} onChange={(event) => setChangedOnly(event.currentTarget.checked)} />
        <span>Only changed ({pending.size})</span>
      </label>
      <label className="executor-config-filter" title="Show settings whose file, pending, or effective value differs from Default">
        <input type="checkbox" checked={nonDefaultOnly} onChange={(event) => setNonDefaultOnly(event.currentTarget.checked)} />
        <span>Non-default ({nonDefaultCount})</span>
      </label>
    </div>
    <div className="executor-config-actions" hidden={!pending.size && !saveStatus}>
      <button type="button" disabled={!pending.size || saving} onClick={() => void save()}>Save changes</button>
      <button type="button" hidden={!pending.size} onClick={() => { setPending(new Map()); setEditing(""); setSaveStatus(""); }}>Discard</button>
      <span className="executor-config-status">{saveStatus}</span>
    </div>
    {canApply && <div className="executor-config-actions">
      <button type="button" disabled={applying} onClick={() => void apply()}>{confirmed ? "Confirm restart" : "Apply saved file"}</button>
      <button type="button" hidden={!confirmed || applying} onClick={() => { setConfirmed(false); setApplyStatus(""); }}>Cancel</button>
      <span className="executor-config-status">{applyStatus}</span>
    </div>}
    <div className="executor-config-headings"><span>Setting</span><span>Config</span><span>Default</span></div>
    <div className="executor-config-list">
      {[...groups].map(([section, fields]) => {
        const shown = fields.filter(visible).length;
        return <details key={section} className="executor-config-section executor-manage-group"
          hidden={shown === 0} open={filtering || (expanded[section] ?? true)}
          onToggle={(event) => {
            const open = event.currentTarget.open;
            if (!filtering && expanded[section] !== open) {
              setExpanded((current) => ({ ...current, [section]: open }));
            }
          }}>
          <summary><strong>{section}</strong><span className="executor-config-count">{filtering ? `${shown}/${fields.length}` : fields.length}</span></summary>
          {fields.map((field) => <FieldRow key={field.name} field={field} action={pending.get(field.name)}
            visible={visible(field)} editable={Boolean(data.editable)} editing={editing === field.name}
            onEdit={() => setEditing(field.name)} onStage={(action) => stage(field, action)}
            onCancel={() => setEditing("")} onInvalid={() => setSaveStatus("Invalid value")} />)}
        </details>;
      })}
    </div>
    <p className="executor-config-empty" hidden={!filtering || visibleCount > 0}>
      {changedOnly && !nonDefaultOnly ? "No unsaved changes" : "No matching settings"}
    </p>
  </>;
}

function ConfigurationView({ model }: { model: Model }) {
  if (model.state === "blank") return null;
  if (model.state !== "ready" || !model.data || !model.events) return <>
    <span className="muted">{model.state === "loading" ? "Loading…" : model.message}</span>
    {model.state === "offline" && model.events && <button type="button" onClick={model.events.retry}>Retry</button>}
  </>;
  return <Inspector data={model.data} events={model.events} notice={model.notice || ""} />;
}

function mount(host: HTMLElement) {
  const root = createRoot(host);
  return { render(model: Model) {
    // clear() removes previously visible configuration data immediately.
    flushSync(() => root.render(<ConfigurationView key={model.generation} model={model} />));
  } };
}

export const configuration = { mount };
