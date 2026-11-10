import { createRoot } from "react-dom/client";

const items = [
  { view: "overview", title: "Overview", icon: <><path d="M4 13h6V4H4zm10 7h6v-9h-6zM4 20h6v-3H4zm10-13h6V4h-6z"/></> },
  { view: "files", title: "Files", icon: <><path d="M3 7h7l2 2h9v10H3z"/><path d="M3 7V5h7l2 2"/></> },
  { view: "terminals", title: "Terminals", icon: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/></> },
  { view: "tasks", title: "Tasks", icon: <><path d="M5 4h14v5H5zM5 11h14v9H5z"/><path d="M8 7h5M8 15h8M8 18h5"/></> },
  { view: "executors", title: "Executors", icon: <><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6"/></> },
  { view: "audit", title: "Audit", icon: <><path d="M4 18V9M10 18V5M16 18v-7M22 18V3"/></> },
] as const;

type View = (typeof items)[number]["view"];
const views = Object.freeze(
  Object.fromEntries(items.map(({ view, title }) => [view, { title }]))
) as Record<View, { title: string }>;

function mount(host: HTMLElement, onNavigate: (view: View) => void) {
  const root = createRoot(host);
  return {
    render(activeView: string) {
      root.render(items.map(({ view, title, icon }) => (
        <button
          key={view}
          type="button"
          className={view === activeView ? "nav-item active" : "nav-item"}
          data-view={view}
          aria-label={title}
          title={title}
          aria-current={view === activeView ? "page" : undefined}
          onClick={() => onNavigate(view)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">{icon}</svg>
          <span>{title}</span>
        </button>
      )));
    },
  };
}

export const navigation = { mount, views };
