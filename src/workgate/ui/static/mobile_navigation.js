// Phone-only list/detail navigation; controllers own the selected item.
export function createMobileNavigation() {
  const pages = {
    tasks: { task: ["All tasks", "Task details"], session: ["Task", "Session audit"], "session-record": ["Session audit", "Audit record"] },
    executors: { detail: ["Executors", "Executor details"] },
    files: { detail: ["Files", "File preview"] },
    terminals: { detail: ["Terminals", "Terminal"] },
    audit: { detail: ["Audit records", "Audit record"] },
  };
  const mobile = matchMedia("(max-width: 780px)");
  const bars = new Map();

  // Full reloads can restore a Task/Session deep link, but not an anonymous
  // audit/file/terminal selection that only existed in the previous DOM.
  history.replaceState({}, "", location.href);

  for (const view of Object.keys(pages)) {
    const panel = document.querySelector(`[data-app-view="${view}"]`);
    const bar = document.createElement("div");
    bar.className = "mobile-subpage-bar";
    const back = document.createElement("button");
    back.type = "button";
    back.className = "mobile-subpage-back";
    const title = document.createElement("strong");
    bar.append(back, title);
    panel.prepend(bar);
    bars.set(view, { panel, bar, back, title });
    back.addEventListener("click", () => {
      const step = panel.dataset.mobileStep;
      if (history.state?.mobileView === view && history.state?.mobileStep === step) {
        history.back();
      } else {
        const parent = view === "tasks"
          ? ({ "session-record": "session", session: "task", task: "list" }[step])
          : "list";
        go(view, parent, { replace: true });
      }
    });
  }

  function currentStep(view) {
    const state = history.state;
    if (state?.mobileView === view) return state.mobileStep;
    if (view === "tasks") {
      const params = new URLSearchParams(location.search);
      if (params.has("session_id")) return "session";
      if (params.has("task_id")) return "task";
    }
    return "list";
  }

  function sync(view) {
    for (const [name, { panel, bar, back, title }] of bars) {
      const step = mobile.matches && name === view ? currentStep(name) : "list";
      panel.dataset.mobileStep = step;
      bar.hidden = step === "list";
      if (step !== "list") {
        const [destination, heading] = pages[name][step];
        back.textContent = `← ${destination}`;
        back.setAttribute("aria-label", `Back to ${destination}`);
        title.textContent = heading;
      }
    }
  }

  function go(view, step, { replace = false } = {}) {
    if (!mobile.matches || document.body.dataset.activeView !== view) return;
    if (step !== "list" && !pages[view]?.[step]) return;
    if (bars.get(view).panel.dataset.mobileStep === step) return;
    // Task/Session selection updates its own deep-link URL before we push the
    // detail entry. Explicitly record the preceding list so Back stays a list.
    if (!replace && bars.get(view).panel.dataset.mobileStep === "list") {
      history.replaceState({ mobileView: view, mobileStep: "list" }, "", location.href);
    }
    history[replace ? "replaceState" : "pushState"](
      { mobileView: view, mobileStep: step }, "", location.href,
    );
    sync(view);
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function bind() {
    mobile.addEventListener("change", () => sync(document.body.dataset.activeView));
  }

  return { bind, sync, go };
}
