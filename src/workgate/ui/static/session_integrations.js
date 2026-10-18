// Session-bound integrations are inspected here; edits reuse the Executor manager.
export function createSessionIntegrationsViewer({ request, onManage, onOpen = () => {} }) {
  const panel = document.getElementById("session-integrations");
  const layout = panel.closest(".task-sessions-layout");
  const openButton = document.getElementById("session-integrations-open");
  const backButton = document.getElementById("session-integrations-back");
  const tabs = [...panel.querySelectorAll("[data-session-integrations-tab]")];
  const views = {
    mcp: {
      pane: document.getElementById("session-integrations-mcp"),
      status: document.getElementById("session-integrations-mcp-status"),
      list: document.getElementById("session-integrations-mcp-list"),
      refresh: document.getElementById("session-integrations-mcp-refresh"),
      manage: document.getElementById("session-integrations-mcp-manage"),
    },
    skills: {
      pane: document.getElementById("session-integrations-skills"),
      status: document.getElementById("session-integrations-skills-status"),
      list: document.getElementById("session-integrations-skills-list"),
      refresh: document.getElementById("session-integrations-skills-refresh"),
      manage: document.getElementById("session-integrations-skills-manage"),
    },
  };
  const sources = { project: "Project", managed: "Executor", global: "Executor (User Global)", control: "Control" };
  let session = null;
  let sessionKey = "";
  let active = "skills";
  let generation = 0;
  let loaded = { mcp: false, skills: false };

  function close() {
    generation += 1;
    loaded = { mcp: false, skills: false };
    layout.classList.remove("integrations-open");
    panel.hidden = true;
  }

  function group(list, heading, entries) {
    if (!entries.length) return;
    const section = document.createElement("section");
    section.className = "executor-manage-group";
    const title = document.createElement("h4");
    title.textContent = heading;
    section.append(title);
    for (const entry of entries) {
      const row = document.createElement("div");
      row.className = "executor-manage-row";
      const name = document.createElement("strong");
      name.textContent = entry.name;
      const state = document.createElement("span");
      state.className = "executor-manage-pill";
      state.dataset.enabled = String(entry.state === "Effective" || entry.state === "Enabled");
      state.textContent = entry.state;
      row.append(name, state);
      section.append(row);
    }
    list.append(section);
  }

  async function loadSkills() {
    const view = views.skills;
    const revision = ++generation;
    const id = session?.session_id || "";
    loaded.skills = false;
    view.status.textContent = "Loading Skills…";
    view.list.replaceChildren();
    view.refresh.disabled = true;
    try {
      const result = await request(`/sessions/skills?session_id=${encodeURIComponent(id)}`);
      if (revision !== generation || id !== session?.session_id) return;
      const rows = Array.isArray(result.skills) ? result.skills : [];
      for (const key of ["project", "managed", "global", "control"]) {
        group(view.list, sources[key], rows.filter((row) => row.source === key && typeof row.name === "string")
          .map((row) => ({ name: row.name, state: "Effective" })));
      }
      view.status.textContent = `${rows.length} effective Skills · Project is read-only`;
      loaded.skills = true;
    } catch (error) {
      if (revision === generation) view.status.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      if (revision === generation) view.refresh.disabled = false;
    }
  }

  async function loadMcp() {
    const view = views.mcp;
    const revision = ++generation;
    loaded.mcp = false;
    view.status.textContent = "Loading MCPs…";
    view.list.replaceChildren();
    view.refresh.disabled = true;
    const executorId = session?.executor_id || "";
    try {
      const control = await request("/integrations?owner=control&kind=mcp");
      let executor = null;
      let executorUnavailable = false;
      try {
        executor = await request(`/integrations?owner=executor&kind=mcp&executor_id=${encodeURIComponent(executorId)}`);
      } catch {
        executorUnavailable = true;
      }
      if (revision !== generation || executorId !== session?.executor_id) return;
      const controls = Array.isArray(control.servers) ? control.servers : [];
      const executors = Array.isArray(executor?.servers) ? executor.servers : [];
      const activeExecutorNames = new Set(executors.filter((row) => row.enabled).map((row) => row.name));
      const describe = (row, overridden = false) => ({
        name: row.name,
        state: !row.enabled ? "Disabled" : overridden ? "Overridden" : "Enabled",
      });
      group(view.list, "Executor", executors.filter((row) => typeof row.name === "string").map((row) => describe(row)));
      group(view.list, "Control", controls.filter((row) => typeof row.name === "string")
        .map((row) => describe(row, activeExecutorNames.has(row.name))));
      view.status.textContent = executorUnavailable
        ? `${controls.length} Control MCPs · Executor unavailable`
        : `${executors.length + controls.length} configured MCPs`;
      loaded.mcp = true;
    } catch (error) {
      if (revision === generation) view.status.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      if (revision === generation) view.refresh.disabled = false;
    }
  }

  function selectTab(name) {
    if (!views[name]) return;
    active = name;
    for (const tab of tabs) {
      const selected = tab.dataset.sessionIntegrationsTab === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    for (const [key, view] of Object.entries(views)) view.pane.hidden = key !== name;
    if (!loaded[name]) void (name === "skills" ? loadSkills() : loadMcp());
  }

  function open() {
    if (!session?.session_id) return;
    onOpen();
    layout.classList.add("integrations-open");
    panel.hidden = false;
    selectTab(active);
  }

  function selectSession(next) {
    const key = [next?.session_id, next?.executor_id, next?.workdir].join("|");
    session = next;
    openButton.disabled = !next?.session_id;
    if (key === sessionKey) return;
    sessionKey = key;
    close();
    for (const view of Object.values(views)) {
      view.list.replaceChildren();
      view.status.textContent = "Open this tab to inspect";
    }
  }

  function bind() {
    openButton.addEventListener("click", open);
    backButton.addEventListener("click", close);
    for (const tab of tabs) {
      tab.addEventListener("click", () => selectTab(tab.dataset.sessionIntegrationsTab));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const index = tabs.indexOf(tab);
        const delta = event.key === "ArrowRight" ? 1 : -1;
        const next = tabs[(index + delta + tabs.length) % tabs.length];
        selectTab(next.dataset.sessionIntegrationsTab);
        next.focus();
      });
    }
    for (const [kind, view] of Object.entries(views)) {
      view.refresh.addEventListener("click", () => void (kind === "skills" ? loadSkills() : loadMcp()));
      view.manage.addEventListener("click", () => {
        if (!session?.executor_id) return;
        onManage(session.executor_id, kind);
      });
    }
  }

  return { bind, selectSession, close };
}
