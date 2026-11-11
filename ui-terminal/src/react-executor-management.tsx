import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Row = {
  title: string; detail?: string; status?: string; authState?: string;
  authMode?: string; notice?: "warning" | "error"; manage?: () => void;
};
type Group = { key: string; title: string; rows: Row[] };
type Model = { scope: string; mcp: Group[]; skills: Group[] };

function ManagementRow({ row }: { row: Row }) {
  if (row.notice) return <p className="executor-notice" data-tone={row.notice}
    role={row.notice === "error" ? "alert" : "status"}>{row.title}</p>;
  const state = ["Enabled", "Disabled", "Overridden"].includes(row.status || "")
    ? row.status!.toLowerCase() : undefined;
  return <div className="executor-manage-row" data-state={state} data-auth={row.authState}>
    {row.status === "Overridden" && <span className="executor-row-warning" aria-label="Warning">!</span>}
    <div>
      <strong>{row.title}</strong>
      {row.detail && <span>{row.detail}</span>}
      {row.authState && <span className="executor-row-auth">
        {row.authMode === "secret"
          ? (row.authState === "authorized" ? "✓ Secret configured" : "Secret required")
          : (row.authState === "authorized" ? "✓ Authorized" : "Authorization required")}
      </span>}
    </div>
    <div className="executor-manage-tail">
      {row.status && <span className="executor-manage-pill" data-state={state}
        data-enabled={String(row.status === "Enabled" || row.status === "Effective")}>{row.status}</span>}
      {row.manage && <button type="button" className="executor-manage-edit" onClick={row.manage}>Manage</button>}
    </div>
  </div>;
}

function ManagementGroups({ groups, scope }: { groups: Group[]; scope: string }) {
  return <>{groups.map(group => <section key={`${scope}:${group.key}`}
    className="executor-manage-group" data-source-key={group.key}>
    <h4>{group.title}</h4>
    {group.rows.map((row, index) => <ManagementRow key={`${row.title}:${index}`} row={row} />)}
  </section>)}</>;
}

function mount(mcpHost: HTMLElement, skillsHost: HTMLElement) {
  const mcpRoot = createRoot(mcpHost);
  const skillsRoot = createRoot(skillsHost);
  return {
    render(model: Model) {
      // Existing controller code may immediately inspect these rows or focus its editor.
      flushSync(() => {
        mcpRoot.render(<ManagementGroups scope={model.scope} groups={model.mcp} />);
        skillsRoot.render(<ManagementGroups scope={model.scope} groups={model.skills} />);
      });
    },
  };
}

export const executorManagement = { mount };
