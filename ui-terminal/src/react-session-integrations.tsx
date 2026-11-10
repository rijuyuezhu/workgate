import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

type Entry = { name: string; state: string };
type Group = { heading: string; entries: Entry[] };

function GroupedList({ groups }: { groups: Group[] }) {
  return <>{groups.map((group) => <section key={group.heading} className="executor-manage-group">
    <h4>{group.heading}</h4>
    {group.entries.map((entry, index) => <div key={`${entry.name}:${index}`} className="executor-manage-row">
      <strong>{entry.name}</strong>
      <span className="executor-manage-pill"
        data-enabled={String(entry.state === "Effective" || entry.state === "Enabled")}>
        {entry.state}
      </span>
    </div>)}
  </section>)}</>;
}

function mount(mcpHost: HTMLElement, skillsHost: HTMLElement) {
  const mcpRoot = createRoot(mcpHost);
  const skillsRoot = createRoot(skillsHost);
  return {
    render(kind: "mcp" | "skills", groups: Group[]) {
      // Session switching must synchronously clear a previous session's rows.
      flushSync(() => {
        (kind === "mcp" ? mcpRoot : skillsRoot).render(<GroupedList groups={groups} />);
      });
    },
  };
}

export const sessionIntegrations = { mount };
