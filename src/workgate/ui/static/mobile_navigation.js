// One mobile drill-down convention for list/detail views. Desktop remains split-pane.
export function createMobileNavigation() {
  const routes = {
    tasks: ["list", "task", "session", "session-record"],
    executors: ["list", "detail"],
    files: ["list", "detail"],
    terminals: ["list", "detail"],
    audit: ["list", "detail"],
  };
  const parents = {
    tasks: { task: "list", session: "task", "session-record": "session" },
    executors: { detail: "list" },
    files: { detail: "list" },
    terminals: { detail: "list" },
    audit: { detail: "list" },
  };
  const backLabels = {
    tasks: { task: "All tasks", session: "Task", "session-record": "Session audit" },
    executors: { detail: "Executors" },
    files: { detail: "Files" },
    terminals: { detail: "Terminals" },
    audit: { detail: "Audit records" },
  };
  const headings = {
    tasks: { task: "Task details", session: "Session audit", "session-record": "Audit record" },
    executors: { detail: "Executor details" },
    files: { detail: "File preview" },
    terminals: { detail: "Terminal" },
    audit: { detail: "Audit record" },
  };
  const mobile = window.matchMedia("(max-width: 780px)");
  const bars = new Map();

  for (const view of Object.keys(routes)) {
    const panel = document.querySelector(`[data-app-view="${view}"]`);
    const bar = document.createElement("div");
    bar.className = "mobile-subpage-bar";
    const back = document.createElement("button");
    back.className = "mobile-subpage-back";
    back.type = "button";
    const title = document.createElement("strong");
    bar.append(back, title);
    panel.prepend(bar);
    bars.set(view, { panel, bar, back, title });
    back.addEventListener("click", () => {
      const step = panel.dataset.mobileStep;
      if (history.state?.workgateMobileStep === step && history.state?.workgateMobileView === view) {
        history.back();
      } else {
        go(view, parents[view][step], { replace: true });
      }
    });
  }

  function requestedStep(view) {
    if (!routes[view]) return "list";
    const url = new URL(location.href);
    const step = url.searchParams.get("mobile_step");
    if (url.searchParams.get("mobile_view") === view && routes[view].includes(step)) return step;
    // A direct Task/Session URL should start at the useful detail, not an empty list.
    if (view === "tasks" && url.searchParams.has("session_id")) return "session";
    if (view === "tasks" && url.searchParams.has("task_id")) return "task";
    return "list";
  }

  function sync(view) {
    for (const [name, { panel, bar, back, title }] of bars) {
      const step = name === view && mobile.matches ? requestedStep(name) : "list";
      panel.dataset.mobileStep = step;
      bar.hidden = !mobile.matches || step === "list";
      if (step !== "list") {
        back.textContent = `← ${backLabels[name][step]}`;
        title.textContent = headings[name][step];
        back.setAttribute("aria-label", `Back to ${backLabels[name][step]}`);
      }
    }
  }

  function go(view, step, { replace = false } = {}) {
    if (!mobile.matches || !routes[view]?.includes(step)) return;
    if (document.body.dataset.activeView !== view) return;
    const panel = bars.get(view).panel;
    if (panel.dataset.mobileStep === step) return;
    const url = new URL(location.href);
    if (panel.dataset.mobileStep === "list" && !url.searchParams.has("mobile_step") && !replace) {
      const previous = new URL(url);
      previous.searchParams.set("mobile_view", view);
      previous.searchParams.set("mobile_step", "list");
      history.replaceState(history.state, "", previous);
    }
    url.searchParams.set("mobile_view", view);
    url.searchParams.set("mobile_step", step);
    const state = replace ? history.state : { ...history.state, workgateMobileView: view, workgateMobileStep: step };
    history[replace ? "replaceState" : "pushState"](state, "", url);
    sync(view);
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function bind() {
    document.addEventListener("workgate:files:navigated", () => go("files", "list", { replace: true }));
    // Capture selection before view controllers replace clicked list rows.
    // Navigate after their synchronous selection/validation has completed.
    document.addEventListener("click", (event) => {
      if (!mobile.matches || !(event.target instanceof Element)) return;
      const view = document.body.dataset.activeView;
      const match = (selector) => event.target.closest(selector);
      let step = "";
      let validate = () => true;
      if (view === "tasks") {
        const task = match("#task-list .task-entry");
        const unattached = match("#task-list .unattached-session-entry");
        const session = match("#session-list .session-entry");
        const record = match("#session-audit-list .audit-entry");
        if (task) {
          step = "task";
          validate = () => document.querySelector(`#task-list .task-entry[data-task-id="${CSS.escape(task.dataset.taskId)}"]`)?.getAttribute("aria-current") === "true";
        } else if (unattached || session) {
          step = "session";
          const id = (unattached || session).dataset.sessionId;
          validate = () => document.querySelector(`#task-list .unattached-session-entry[data-session-id="${CSS.escape(id)}"], #session-list .session-entry[data-session-id="${CSS.escape(id)}"]`)?.getAttribute("aria-current") === "true";
        } else if (record) {
          step = "session-record";
        }
      } else if (view === "executors" && match("#executor-list .executor-row")) {
        step = "detail";
      } else if (view === "audit" && match("#audit-list .audit-entry")) {
        step = "detail";
      } else if (view === "files" && match("#file-list .file-entry")) {
        step = "detail";
      } else if (view === "terminals" && match("#terminal-list .terminal-session")) {
        step = "detail";
      }
      if (step) window.setTimeout(() => { if (validate()) go(view, step); }, 0);
    }, true);
    mobile.addEventListener("change", () => sync(document.body.dataset.activeView));
  }

  return { bind, sync, go };
}
