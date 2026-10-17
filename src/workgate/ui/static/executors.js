import { renderResourceCards } from "./dashboard.js";

export function createExecutorsController({
  elements,
  request,
  authMode,
  isAuthenticated,
  reloadApp,
  onInventory,
  onNavigate = () => {},
}) {
  const state = {
    executors: [],
    selectedId: "",
    loading: false,
    generation: 0,
    timer: null,
    pairing: null,
    skillSourceId: "",
    skillSources: null,
    skillSourceGeneration: 0,
    metricHistory: [],
    metricGeneration: 0,
    metricInFlightId: "",
    metricOnline: false,
    detailTabs: new Map(),
    management: null,
    managementId: "",
    managementGeneration: 0,
    editor: null,
  };

  function selectedExecutor() {
    return state.executors.find((item) => item.executor_id === state.selectedId) || null;
  }

  function resetMetrics() {
    state.metricGeneration += 1;
    state.metricInFlightId = "";
    state.metricHistory = [];
    state.metricOnline = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
    document.getElementById("executor-overview-metrics")?.replaceChildren();
  }

  function select(executorId) {
    if (!state.executors.some((item) => item.executor_id === executorId)) return;
    const changed = state.selectedId !== executorId;
    if (changed && elements.executorIntegrationDialog.open) {
      elements.executorIntegrationDialog.close();
      state.editor = null;
    }
    state.selectedId = executorId;
    if (changed) {
      state.skillSourceGeneration += 1;
      state.skillSourceId = "";
      state.skillSources = null;
      state.managementGeneration += 1;
      state.managementId = "";
      state.management = null;
    }
    if (changed) resetMetrics();
    renderList();
    renderDetails();
    renderManagement();
    void loadSkillSources(true);
    void refreshMetrics();
    if (["mcp", "skills"].includes(activeSubtab())) void loadManagement();
  }

  function timestamp(value, fallback = "Never") {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) return fallback;
    const date = new Date(seconds * 1000);
    return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
  }

  function closeDialog(dialog) {
    if (dialog?.open) dialog.close();
  }

  function setControls() {
    elements.executorPairOpen.disabled = state.loading;
    const selected = selectedExecutor();
    const mutable = selected && !selected.revoked_at;
    elements.executorReset.disabled = state.loading || !mutable;
    elements.executorDrain.disabled = state.loading || !mutable;
    elements.executorRenameOpen.disabled = state.loading || !mutable;
    elements.executorRevokeOpen.disabled = state.loading || !mutable;
    elements.executorDrain.textContent = selected?.draining ? "Resume" : "Drain";
  }

  function renderList() {
    elements.executorList.replaceChildren();
    if (!state.executors.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No executors are paired yet.";
      elements.executorList.append(empty);
      return;
    }
    for (const executor of state.executors) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "executor-band executor-row";
      const selected = executor.executor_id === state.selectedId;
      if (selected) button.classList.add("executor-row-selected");
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.title = executor.name || executor.executor_id;

      const indicator = document.createElement("span");
      indicator.className = "executor-band-mark";
      indicator.textContent = (executor.name || executor.executor_id).slice(0, 1).toUpperCase();

      const main = document.createElement("span");
      main.className = "executor-band-main";
      const name = document.createElement("strong");
      name.className = "executor-band-name";
      name.textContent = executor.name || executor.executor_id;
      const meta = document.createElement("span");
      meta.className = "executor-band-meta";
      meta.textContent = executor.runtime?.workgate_version || "Executor";
      main.append(name, meta);

      const status = document.createElement("span");
      status.className = "executor-band-state";
      status.textContent = executor.revoked_at
        ? "revoked" : executor.draining ? "draining" : executor.online ? "online" : "offline";
      status.dataset.status = status.textContent;
      button.append(indicator, main, status);
      button.addEventListener("click", () => {
        select(executor.executor_id);
        onNavigate("detail");
      });
      elements.executorList.append(button);
    }
  }

  function activeSubtab() {
    return state.detailTabs.get(state.selectedId) || "overview";
  }

  function setSubtab(tab) {
    if (!selectedExecutor() || !["overview", "mcp", "skills"].includes(tab)) return;
    state.detailTabs.set(state.selectedId, tab);
    renderSubtabs();
    if (tab === "overview") void refreshMetrics();
    if (tab === "mcp" || tab === "skills") void loadManagement();
  }

  function renderSubtabs() {
    const active = activeSubtab();
    for (const button of elements.executorSubtabs) {
      const selected = button.dataset.executorSubtab === active;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    for (const pane of elements.executorSubtabPanes) {
      pane.hidden = pane.dataset.executorPane !== active;
    }
  }

  function rowIcon(kind) {
    if (!["http", "stdio", "skill", "project", "info"].includes(kind)) return null;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('executor-row-icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    const shapes = {
      http: ['<circle cx="12" cy="12" r="9"/>', '<path d="M3 12h18M12 3c-5 5-5 13 0 18M12 3c5 5 5 13 0 18"/>'],
      stdio: ['<path d="M5 7l5 5-5 5M12 17h7"/>'],
      skill: ['<path d="M7 3h7l4 4v14H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM14 3v5h4M9 13h6M9 17h6"/>'],
      project: ['<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3z"/>'],
      info: ['<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>'],
    };
    // All icon paths are static, independent of configuration content.
    svg.innerHTML = shapes[kind].join('');
    return svg;
  }

  function addGroup(container, title, rows) {
    const group = document.createElement("section");
    group.className = "executor-manage-group";
    const heading = document.createElement("h4");
    heading.textContent = title;
    group.append(heading);
    for (const row of rows) {
      const item = document.createElement("div");
      item.className = "executor-manage-row";
      if (row.empty) item.classList.add("executor-manage-empty");
      if (row.status === "Disabled") item.dataset.state = "disabled";
      if (row.status === "Enabled") item.dataset.state = "enabled";
      if (row.status === "Overridden") item.dataset.state = "overridden";
      const icon = rowIcon(row.icon);
      if (icon) {
        const mark = document.createElement('span');
        mark.className = 'executor-row-mark';
        mark.dataset.kind = row.icon;
        mark.append(icon);
        item.append(mark);
      }
      if (row.status === "Overridden" || row.warning) {
        const alert = document.createElement("span");
        alert.className = "executor-row-warning";
        alert.textContent = "!";
        alert.setAttribute("aria-label", "Warning");
        item.append(alert);
      }
      const text = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = row.title;
      const detail = document.createElement("span");
      detail.textContent = row.detail || "";
      text.append(name);
      if (row.detail) text.append(detail);
      item.append(text);
      const tail = document.createElement("div");
      tail.className = "executor-manage-tail";
      if (row.status) {
        const badge = document.createElement("span");
        badge.className = "executor-manage-pill";
        if (row.status === 'Enabled') badge.dataset.state = 'enabled';
        if (row.status === 'Disabled') badge.dataset.state = 'disabled';
        if (row.status === 'Overridden') badge.dataset.state = 'overridden';
        badge.dataset.enabled = String(row.status === "Enabled" || row.status === "Effective");
        badge.textContent = row.status;
        tail.append(badge);
      }
      if (row.manage) {
        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "executor-manage-edit";
        edit.textContent = "Manage";
        edit.addEventListener("click", row.manage);
        tail.append(edit);
      }
      item.append(tail);
      group.append(item);
    }
    container.append(group);
  }

  function renderDiagnosticCards(container, system) {
    const template = document.querySelector("#dashboard-panel .dashboard-summary");
    if (!template) return;
    const cards = template.cloneNode(true);
    cards.removeAttribute("aria-label");
    cards.querySelectorAll("[id]").forEach((item) => item.removeAttribute("id"));
    cards.querySelectorAll(".dashboard-live").forEach((item) => item.remove());
    const history = state.metricHistory.map((sample) => ({
      cpu: sample.cpu_percent, memory: sample.memory_percent, disk: sample.disk_percent,
    }));
    renderResourceCards(cards, system, history);
    container.append(cards);
  }

  function managementQuery(owner, kind, action = "list", name = "") {
    const params = new URLSearchParams({ owner, kind, action });
    if (owner === "executor") params.set("executor_id", state.selectedId);
    if (name) params.set("name", name);
    return `/integrations?${params.toString()}`;
  }

  async function loadManagement(force = false) {
    const executorId = state.selectedId;
    const scope = executorId || "__control__";
    if (state.managementId === scope && !force) return;
    const generation = ++state.managementGeneration;
    state.managementId = scope;
    state.management = null;
    renderManagement();
    const sources = [["control", "mcp"], ["executor", "mcp"], ["executor", "skill"]];
    const online = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
    const results = await Promise.all(sources.map(async ([owner, kind]) => {
      if (owner === "executor" && !online) return { error: "Offline" };
      try { return await request(managementQuery(owner, kind)); }
      catch (error) {
        if (error.authenticationRequired) { void reloadApp(); return { error: "Authentication required" }; }
        return { error: error instanceof Error ? error.message : String(error) };
      }
    }));
    if (generation !== state.managementGeneration || executorId !== state.selectedId) return;
    state.management = {control: results[0], executorMcp: results[1], skills: results[2]};
    renderManagement();
  }

  function renderManagement() {
    const mcp = elements.executorMcpBody;
    const skills = elements.executorSkillsBody;
    mcp.replaceChildren();
    skills.replaceChildren();
    const stateRows = (message) => [{ title: message, detail: "" }];
    const snap = state.management;
    const renderGroup = (container, title, result, owner, kind) => {
      if (!result || result.error) {
        addGroup(container, title, stateRows(result?.error || "Loading…"));
        return;
      }
      const items = kind === "mcp" ? result.servers : result.skills;
      const rows = (Array.isArray(items) ? items : []).map((item) => ({
        title: item.name,
        detail: kind === "mcp" ? item.type : "SKILL.md",
        status: item.enabled ? "Enabled" : "Disabled",
        manage: result.can_manage ? () => void openEditor({owner, kind, item}) : null,
      }));
      addGroup(container, title, rows.length ? rows : stateRows("None"));
    };
    renderGroup(mcp, "Executor", snap?.executorMcp, "executor", "mcp");
    renderGroup(mcp, "Control", snap?.control, "control", "mcp");
    elements.executorControlStandalone.hidden = Boolean(selectedExecutor());
    elements.executorControlMcpBody.replaceChildren();
    if (!selectedExecutor()) {
      renderGroup(elements.executorControlMcpBody, "Control", snap?.control, "control", "mcp");
    }
    elements.executorControlMcpAdd.disabled = !Boolean(snap?.control?.can_manage);
    renderGroup(skills, "Executor", snap?.skills, "executor", "skill");
    const globalsAvailable = Boolean(selectedExecutor()?.online && state.skillSources?.status === "available" && state.skillSourceId === state.selectedId);
    const globalSkills = state.skillSources?.executor?.skills;
    const readOnly = globalsAvailable ? [
      ...(globalSkills?.selected || []).filter((skill) => skill.source === "global")
        .map((skill) => ({title: skill.name, detail: "", status: "Effective"})),
      ...(globalSkills?.overridden || []).filter((skill) => skill.source === "global")
        .map((skill) => ({title: skill.name, detail: "", status: "Overridden"})),
    ] : [];
    const globalStatus = !selectedExecutor()?.online ? "Offline"
      : !globalsAvailable ? "Unavailable" : "None";
    addGroup(skills, "User Global", readOnly.length ? readOnly : stateRows(globalStatus));
    const active = Boolean(snap?.skills?.can_manage && selectedExecutor()?.online);
    elements.executorSkillsAdd.disabled = !active;
    elements.executorMcpAdd.disabled = !Boolean((snap?.control?.can_manage || snap?.executorMcp?.can_manage));
  }

  function setEditorType() {
    const editing = state.editor;
    if (!editing) return;
    const skill = editing.kind === "skill";
    const owner = elements.executorIntegrationOwner.value;
    elements.executorIntegrationMcpFields.hidden = skill;
    elements.executorIntegrationSkillWrap.hidden = !skill;
    elements.executorIntegrationValidate.hidden = !skill;
    elements.executorMcpAuthManagement.hidden = skill || !editing.item;
    elements.executorIntegrationTestWrap.hidden = skill || !editing.item;
    if (skill) return;
    const mode = elements.executorIntegrationAuthMode.value;
    elements.executorIntegrationOAuthScopesWrap.hidden = mode !== "oauth";
    elements.executorMcpOAuthActions.hidden = mode !== "oauth" || !editing.item;
    elements.executorIntegrationTransport.value = owner === "control" ? "http" : "stdio";
    elements.executorIntegrationTransport.disabled = true;
    elements.executorIntegrationUrlWrap.hidden = owner !== "control";
    elements.executorIntegrationCommandWrap.hidden = owner !== "executor";
    elements.executorIntegrationArgsWrap.hidden = owner !== "executor";
  }

  function editorStatus(message) {
    elements.executorIntegrationStatus.textContent = message;
  }

  async function openEditor({owner, kind, item = null}) {
    if (!selectedExecutor() && owner !== "control") return;
    state.editor = { owner, kind, executorId: state.selectedId, name: item?.name || "", revision: item?.revision || "", item,
                     enabled: item?.enabled !== false };
    const edit = state.editor;
    elements.executorIntegrationForm.reset();
    elements.executorIntegrationTitle.textContent = `${item ? "Edit" : "Add"} ${kind === "skill" ? "Skill" : "MCP server"}`;
    elements.executorIntegrationName.value = edit.name;
    elements.executorIntegrationSave.disabled = Boolean(item);
    for (const option of elements.executorIntegrationOwner.options) {
      option.disabled = kind === "mcp" && !Boolean(option.value === "control"
        ? state.management?.control?.can_manage : state.management?.executorMcp?.can_manage);
    }
    elements.executorIntegrationName.readOnly = Boolean(item);
    elements.executorIntegrationOwner.value = owner;
    elements.executorIntegrationOwner.disabled = Boolean(item) || kind === "skill";
    elements.executorIntegrationContent.value = "";
    elements.executorIntegrationArgs.value = "[]";
    elements.executorIntegrationAdvanced.value = "{}";
    elements.executorIntegrationAuthMode.value = "none";
    elements.executorIntegrationOAuthScopes.value = "";
    elements.executorMcpSecretName.value = "";
    elements.executorMcpSecretValue.value = "";
    elements.executorMcpSecretList.replaceChildren();
    elements.executorMcpAuthStatus.textContent = "Loading auth status…";
    elements.executorIntegrationTestResult.textContent = "";
    elements.executorIntegrationToggle.hidden = !item;
    elements.executorIntegrationDelete.hidden = !item;
    elements.executorIntegrationToggle.textContent = edit.enabled ? "Disable" : "Enable";
    elements.executorIntegrationSave.textContent = item ? "Save changes" : "Create";
    editorStatus(item ? "Loading current configuration…" : "");
    setEditorType();
    elements.executorIntegrationDialog.showModal();
    if (!item) { elements.executorIntegrationName.focus(); return; }
    try {
      const data = await request(managementQuery(owner, kind, "get", item.name));
      if (state.editor !== edit || !elements.executorIntegrationDialog.open) return;
      if (kind === "skill") {
        edit.revision = data.skill?.revision || edit.revision;
        elements.executorIntegrationContent.value = data.skill?.content || "";
      } else {
        const config = data.editable_config;
        if (!config || data.requires_replacement) throw new Error("This legacy server requires replacement; it cannot be edited safely in the WebUI.");
        edit.revision = data.server?.revision || item.revision || "";
        elements.executorIntegrationUrl.value = config.url || "";
        elements.executorIntegrationCommand.value = config.command || "";
        elements.executorIntegrationArgs.value = JSON.stringify(config.args || [], null, 2);
        const advanced = {};
        for (const key of ["integrationId", "env", "headers"]) if (config[key] !== undefined) advanced[key] = config[key];
        elements.executorIntegrationAdvanced.value = JSON.stringify(advanced, null, 2);
        elements.executorIntegrationAuthMode.value = config.auth?.mode || "none";
        elements.executorIntegrationOAuthScopes.value = (config.auth?.scopes || []).join(" ");
        setEditorType();
        void loadEditorAuth(edit);
      }
      editorStatus("");
      elements.executorIntegrationSave.disabled = false;
    } catch (error) {
      editorStatus(error instanceof Error ? error.message : String(error));
      elements.executorIntegrationSave.disabled = true;
    }
  }

  async function mutateEditor(action, config = null) {
    const edit = state.editor;
    if (!edit) return;
    const body = {owner: edit.owner, kind: edit.kind, action,
                  executor_id: edit.executorId, name: elements.executorIntegrationName.value.trim()};
    if (edit.kind === "skill") {
      if (["create", "update", "validate"].includes(action)) body.content = elements.executorIntegrationContent.value;
    } else if (config !== null) body.config = config;
    if (edit.revision && ["update", "enable", "disable", "delete", "remove"].includes(action)) body.expected_revision = edit.revision;
    elements.executorIntegrationSave.disabled = true;
    elements.executorIntegrationToggle.disabled = true;
    elements.executorIntegrationDelete.disabled = true;
    try {
      const result = await request("/integrations", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      if (state.editor !== edit) return;
      if (action === "validate") { editorStatus("SKILL.md is valid."); return; }
      elements.executorIntegrationDialog.close();
      state.editor = null;
      await loadManagement(true);
      void loadSkillSources(true);
      return result;
    } catch (error) {
      editorStatus(error instanceof Error ? error.message : String(error));
    } finally {
      elements.executorIntegrationSave.disabled = false;
      elements.executorIntegrationToggle.disabled = false;
      elements.executorIntegrationDelete.disabled = false;
    }
  }

  function authQuery(edit) {
    const params = new URLSearchParams({owner: edit.owner, name: edit.name});
    if (edit.owner === "executor") params.set("executor_id", edit.executorId);
    return `/integrations/auth?${params.toString()}`;
  }

  async function loadEditorAuth(edit) {
    if (edit.kind !== "mcp" || !edit.item) return;
    try {
      const result = await request(authQuery(edit));
      if (state.editor !== edit) return;
      edit.authStatus = result;
      const status = result.auth || {};
      elements.executorMcpAuthStatus.textContent = `Authorization: ${status.status || "unavailable"}${status.expires_at ? ` · expires ${new Date(status.expires_at * 1000).toLocaleString()}` : ""}`;
      const list = elements.executorMcpSecretList;
      list.replaceChildren();
      for (const key of result.secret_names || []) {
        const line = document.createElement("div");
        line.className = "executor-auth-secret-row";
        const name = document.createElement("code");
        name.textContent = key;
        const label = document.createElement("span");
        label.textContent = "Stored · value hidden";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.addEventListener("click", () => {
          if (globalThis.confirm(`Remove stored secret “${key}”?`)) void mutateAuth("delete_secret", {secret_name:key});
        });
        line.append(name, label, remove);
        list.append(line);
      }
      if (!list.childElementCount) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "No private secrets saved for this MCP.";
        list.append(empty);
      }
    } catch (error) {
      if (state.editor === edit) elements.executorMcpAuthStatus.textContent = error instanceof Error ? error.message : String(error);
    }
  }

  async function mutateAuth(action, fields = {}) {
    const edit = state.editor;
    if (!edit || edit.kind !== "mcp" || !edit.item) return;
    const body = {owner:edit.owner, executor_id:edit.executorId, name:edit.name, action, ...fields};
    elements.executorMcpSecretSet.disabled = true;
    elements.executorMcpOAuthClear.disabled = true;
    try {
      await request("/integrations/auth", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      if (state.editor !== edit) return;
      elements.executorMcpSecretValue.value = "";
      editorStatus(action === "clear_oauth" ? "Local OAuth state cleared (remote tokens not revoked)." : "Private secret updated. Values are never shown.");
      await loadEditorAuth(edit);
    } catch (error) {
      if (state.editor === edit) editorStatus(error instanceof Error ? error.message : String(error));
    } finally {
      elements.executorMcpSecretValue.value = "";
      elements.executorMcpSecretSet.disabled = false;
      elements.executorMcpOAuthClear.disabled = false;
    }
  }

  async function testEditorMcp() {
    const edit = state.editor;
    if (!edit || edit.kind !== "mcp" || !edit.item) return;
    elements.executorIntegrationTest.disabled = true;
    elements.executorIntegrationTestResult.textContent = "Testing saved MCP server…";
    try {
      const result = await request("/integrations/test", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({owner: edit.owner, executor_id: edit.executorId, name: edit.name}),
      });
      if (state.editor !== edit) return;
      const tools = Array.isArray(result.tools) ? result.tools : [];
      elements.executorIntegrationTestResult.replaceChildren();
      const summary = document.createElement("p");
      summary.textContent = result.status === "available"
        ? `Available · ${tools.length} discovered tools (none were called)`
        : `MCP ${result.status || "unavailable"} · check the owning host`;
      elements.executorIntegrationTestResult.append(summary);
      for (const tool of tools) {
        const line = document.createElement("div");
        line.textContent = `${tool.name}${tool.description ? ` · ${tool.description}` : ""}`;
        elements.executorIntegrationTestResult.append(line);
      }
    } catch (error) {
      if (state.editor === edit) elements.executorIntegrationTestResult.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      elements.executorIntegrationTest.disabled = false;
    }
  }

  function editorConfig() {
    const edit = state.editor;
    if (!edit) return null;
    let advanced = {};
    try { advanced = JSON.parse(elements.executorIntegrationAdvanced.value || "{}"); }
    catch { throw new Error("Advanced settings must be valid JSON."); }
    if (!advanced || Array.isArray(advanced) || typeof advanced !== "object") throw new Error("Advanced settings must be an object.");
    if (Object.keys(advanced).some((key) => !["env", "headers", "integrationId"].includes(key))) throw new Error("Advanced settings may contain only env, headers and integrationId.");
    const mode = elements.executorIntegrationAuthMode.value;
    if (mode === "oauth" && edit.owner !== "control") throw new Error("OAuth requires a network HTTP MCP server.");
    const config = { type: edit.owner === "control" ? "http" : "stdio", ...advanced,
      enabled: edit.item ? edit.enabled : true,
      auth: {mode, scopes: mode === "oauth" ? [...new Set(elements.executorIntegrationOAuthScopes.value.trim().split(/\s+/).filter(Boolean))] : []} };
    if (edit.owner === "control") config.url = elements.executorIntegrationUrl.value.trim();
    else {
      config.command = elements.executorIntegrationCommand.value.trim();
      try { config.args = JSON.parse(elements.executorIntegrationArgs.value || "[]"); }
      catch { throw new Error("Arguments must be a JSON array."); }
      if (!Array.isArray(config.args) || !config.args.every((arg) => typeof arg === "string")) throw new Error("Arguments must be strings in a JSON array.");
    }
    return config;
  }

  async function refreshMetrics() {
    const executor = selectedExecutor();
    const view = document.getElementById("executor-overview-metrics")?.closest("[data-app-view]");
    if (!executor?.online || executor.revoked_at || activeSubtab() !== "overview" || view?.hidden) return;
    const executorId = executor.executor_id;
    if (state.metricInFlightId === executorId) return;
    const generation = state.metricGeneration;
    state.metricInFlightId = executorId;
    try {
      const data = await request(`/dashboard?executor_id=${encodeURIComponent(executorId)}`);
      if (generation !== state.metricGeneration || state.selectedId !== executorId || !selectedExecutor()?.online) return;
      const system = data?.system || {};
      const sample = {};
      for (const key of ["cpu_percent", "memory_percent", "disk_percent", "network_rx_bps", "network_tx_bps", "load_1m", "cpu_count", "memory_used_bytes", "memory_total_bytes", "disk_used_bytes", "disk_total_bytes"]) {
        sample[key] = typeof system[key] === "number" && Number.isFinite(system[key]) ? system[key] : null;
      }
      state.metricHistory.push(sample);
      state.metricHistory = state.metricHistory.slice(-60);
      const body = document.getElementById("executor-overview-metrics");
      if (body) { body.replaceChildren(); renderDiagnosticCards(body, sample); }
    } catch (error) {
      if (error.authenticationRequired) { void reloadApp(); return; }
      if (generation !== state.metricGeneration || state.selectedId !== executorId) return;
      const message = document.createElement("p");
      message.className = "executor-metric-error";
      message.textContent = error.status === 403 ? "Resource metrics require shell:read access." : "Resource metrics unavailable.";
      const body = document.getElementById("executor-overview-metrics");
      body?.querySelector(".executor-metric-error")?.remove();
      body?.append(message);
    } finally {
      if (generation === state.metricGeneration && state.metricInFlightId === executorId) state.metricInFlightId = "";
    }
  }

  // Fetch user-global Skill provenance on selection, not on inventory polls.
  async function loadSkillSources(force = false) {
    const executorId = state.selectedId;
    if (elements.executorMcpBody.closest("[data-app-view]")?.hidden) return;
    if (!executorId || !selectedExecutor() || selectedExecutor().revoked_at) {
      state.skillSourceGeneration += 1;
      state.skillSourceId = "";
      state.skillSources = null;
      return;
    }
    if (state.skillSourceId === executorId && !force) return;
    const generation = ++state.skillSourceGeneration;
    state.skillSourceId = executorId;
    state.skillSources = null;
    try {
      const data = await request(`/executors/diagnostics?executor_id=${encodeURIComponent(executorId)}`);
      if (generation !== state.skillSourceGeneration || executorId !== state.selectedId) return;
      state.skillSources = data;
    } catch (error) {
      if (generation !== state.skillSourceGeneration || executorId !== state.selectedId) return;
      if (error.authenticationRequired) { void reloadApp(); return; }
    } finally {
      if (generation === state.skillSourceGeneration && state.management) renderManagement();
    }
  }

  function renderDetails() {
    renderSubtabs();
    const executor = selectedExecutor();
    if (!executor) {
      elements.executorDetailName.textContent = "No executor selected";
      elements.executorDetailStatus.textContent = "Select an executor to inspect it";
      elements.executorDetailId.textContent = "—";
      elements.executorDetailVersion.textContent = "—";
      elements.executorDetailRuntimeOwner.textContent = "—";
      elements.executorDetailCompatibility.textContent = "—";
      elements.executorDetailPlatform.textContent = "—";
      elements.executorDetailAdmission.textContent = "—";
      elements.executorDetailSessions.textContent = "—";
      elements.executorDetailCommands.textContent = "—";
      elements.executorDetailCapabilities.textContent = "—";
      elements.executorDetailCreated.textContent = "—";
      elements.executorDetailLastSeen.textContent = "—";
      setControls();
      return;
    }
    const status = executor.revoked_at
      ? "revoked"
      : executor.draining
        ? "draining"
        : executor.online
          ? "online"
          : "offline";
    elements.executorDetailName.textContent = executor.name || executor.executor_id;
    elements.executorDetailStatus.textContent = status;
    elements.executorDetailId.textContent = executor.executor_id;
    elements.executorDetailVersion.textContent = executor.runtime?.workgate_version || "Not reported";
    elements.executorDetailRuntimeOwner.textContent = executor.runtime?.ownership || "Not reported";
    elements.executorDetailCompatibility.textContent = !executor.runtime
      ? "Not reported"
      : executor.runtime_update_required
        ? "Update required · " + executor.required_workgate_version
        : "Compatible";
    elements.executorDetailPlatform.textContent = executor.runtime?.platform || "Not reported";
    elements.executorDetailAdmission.textContent = executor.session_admission
      ? "Eligible"
      : (executor.session_admission_reasons || []).join(", ") || "Not eligible";
    elements.executorDetailSessions.textContent = String(executor.active_sessions || 0);
    elements.executorDetailCommands.textContent =
      String(executor.queued_commands || 0) + " queued · " + String(executor.offered_commands || 0) + " offered · " + String(executor.command_limit || 0) + " max";
    elements.executorDetailCapabilities.textContent =
      (executor.capabilities || []).join(", ") || "None reported";
    elements.executorDetailCreated.textContent = timestamp(executor.created_at, "Unknown");
    elements.executorDetailLastSeen.textContent = timestamp(executor.last_seen_at);
    setControls();
  }

  function render(payload) {
    const previousId = state.selectedId;
    state.executors = Array.isArray(payload?.executors) ? payload.executors : [];
    if (!state.executors.some((item) => item.executor_id === state.selectedId)) {
      state.selectedId = (state.executors.find((entry) => !entry.revoked_at) || state.executors[0])?.executor_id || "";
    }
    const online = state.executors.filter((item) => !item.revoked_at && item.online).length;
    const revoked = state.executors.filter((item) => item.revoked_at).length;
    elements.executorOnline.textContent = String(online);
    elements.executorTotal.textContent = String(state.executors.length);
    elements.executorRevoked.textContent = String(revoked);
    document.getElementById("executor-revoked-summary").hidden = revoked === 0;
    elements.executorState.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    renderList();
    renderDetails();
    if (state.selectedId !== previousId) {
      state.skillSourceGeneration += 1;
      state.skillSourceId = "";
      state.skillSources = null;
      state.managementGeneration += 1;
      state.managementId = "";
      state.management = null;
      renderManagement();
      if (state.selectedId) {
        resetMetrics();
        void loadSkillSources();
        void refreshMetrics();
        if (["mcp", "skills"].includes(activeSubtab())) void loadManagement();
      }
    } else {
      if (selectedExecutor()?.revoked_at && state.skillSourceId === state.selectedId) {
        state.skillSourceGeneration += 1;
        state.skillSourceId = "";
        state.skillSources = null;
        renderManagement();
      }
      const showMetrics = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
      if (showMetrics !== state.metricOnline) {
        resetMetrics();
        state.metricOnline = showMetrics;
        state.managementGeneration += 1;
        state.managementId = "";
        state.management = null;
        renderManagement();
        if (["mcp", "skills"].includes(activeSubtab())) void loadManagement();
        if (showMetrics) {
          void loadSkillSources(true);
          void refreshMetrics();
        }
      }
      const metricsBody = document.getElementById("executor-overview-metrics");
      if (metricsBody) metricsBody.hidden = !showMetrics;
    }
    onInventory(state.executors);
    if (!state.selectedId && state.managementId !== "__control__") void loadManagement();
  }

  async function refresh({ force = false } = {}) {
    if (state.loading && !force) return null;
    const generation = ++state.generation;
    state.loading = true;
    setControls();
    elements.executorState.textContent = "Loading executors";
    try {
      const payload = await request("/executors");
      if (generation !== state.generation) return null;
      render(payload);
      return payload;
    } catch (error) {
      if (error.authenticationRequired) throw error;
      if (generation !== state.generation) return null;
      elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      return null;
    } finally {
      if (generation === state.generation) {
        state.loading = false;
        setControls();
      }
    }
  }

  function refreshInBackground(options = {}) {
    void refresh(options).catch((error) => {
      if (error.authenticationRequired) void reloadApp();
    });
  }

  function startPolling() {
    stopPolling();
    state.timer = globalThis.setInterval(() => {
      if (authMode !== "oauth" || isAuthenticated()) {
        refreshInBackground();
        if (selectedExecutor()?.online) void refreshMetrics();
      }
    }, 4000);
  }

  function stopPolling() {
    if (state.timer !== null) {
      globalThis.clearInterval(state.timer);
      state.timer = null;
    }
  }

  function clearPairReview() {
    state.pairing = null;
    elements.executorPairReview.hidden = true;
    elements.executorPairRequestedName.textContent = "—";
    elements.executorPairHostname.textContent = "—";
    elements.executorPairPlatform.textContent = "—";
    elements.executorPairBuild.textContent = "—";
    elements.executorPairExisting.textContent = "—";
    elements.executorPairExpiry.textContent = "—";
    elements.executorPairName.value = "";
    elements.executorPairReplace.checked = false;
    elements.executorPairReplaceWrap.hidden = true;
  }

  function showPairReview(view) {
    state.pairing = view;
    elements.executorPairReview.hidden = false;
    elements.executorPairRequestedName.textContent = view.requested_name || "Not requested";
    elements.executorPairHostname.textContent = view.metadata?.hostname || "Not reported";
    elements.executorPairPlatform.textContent = view.metadata?.platform || "Not reported";
    elements.executorPairBuild.textContent = view.metadata?.build || "Not reported";
    elements.executorPairExisting.textContent = view.existing_executor_id || "None";
    elements.executorPairExpiry.textContent = `${Number(view.expires_in) || 0}s remaining`;
    elements.executorPairName.value = view.requested_name || "";
    elements.executorPairReplace.checked = false;
    elements.executorPairReplaceWrap.hidden = !view.existing_executor_id;
  }

  async function pairDecision(decision) {
    const view = state.pairing;
    if (!view) return;
    const body = {
      user_code: view.user_code,
      decision,
    };
    if (decision === "approve") {
      const name = elements.executorPairName.value.trim();
      if (name) body.name = name;
      if (view.existing_executor_id && elements.executorPairReplace.checked) {
        body.replace_executor_id = view.existing_executor_id;
      }
    }
    await request("/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    closeDialog(elements.executorPairDialog);
    clearPairReview();
    elements.executorPairForm.reset();
    await refresh({ force: true });
  }

  function reset(message = "Not loaded") {
    state.generation += 1;
    state.loading = false;
    state.executors = [];
    state.selectedId = "";
    state.skillSourceGeneration += 1;
    state.skillSourceId = "";
    state.skillSources = null;
    resetMetrics();
    state.metricOnline = false;
    state.detailTabs.clear();
    state.managementGeneration += 1;
    state.managementId = "";
    state.management = null;
    state.editor = null;
    elements.executorIntegrationDialog.close();
    closeDialog(elements.executorPairDialog);
    closeDialog(elements.executorRenameDialog);
    closeDialog(elements.executorRevokeDialog);
    clearPairReview();
    elements.executorPairForm.reset();
    elements.executorRenameForm.reset();
    elements.executorState.textContent = message;
    elements.executorOnline.textContent = "—";
    elements.executorTotal.textContent = "—";
    elements.executorRevoked.textContent = "—";
    document.getElementById("executor-revoked-summary").hidden = true;
    renderList();
    renderDetails();
    renderManagement();
  }

  function bind() {
    elements.executorControlMcpAdd.addEventListener("click", () => void openEditor({owner: "control", kind: "mcp"}));
    elements.executorIntegrationTest.addEventListener("click", () => void testEditorMcp());
    elements.executorMcpAdd.addEventListener("click", () => {
      const owner = state.management?.control?.can_manage ? "control" : "executor";
      void openEditor({owner, kind: "mcp"});
    });
    elements.executorSkillsAdd.addEventListener("click", () => void openEditor({owner: "executor", kind: "skill"}));
    elements.executorIntegrationOwner.addEventListener("change", () => {
      if (state.editor && !state.editor.item && state.editor.kind === "mcp") {
        state.editor.owner = elements.executorIntegrationOwner.value;
      }
      setEditorType();
    });
    elements.executorIntegrationAuthMode.addEventListener("change", setEditorType);
    elements.executorMcpSecretSet.addEventListener("click", () => {
      const key = elements.executorMcpSecretName.value.trim();
      const value = elements.executorMcpSecretValue.value;
      if (!key || !value) { editorStatus("Enter both a secret reference name and a value."); return; }
      void mutateAuth("set_secret", {secret_name:key, secret_value:value});
    });
    elements.executorMcpOAuthClear.addEventListener("click", () => {
      if (globalThis.confirm("Clear locally stored OAuth state? This does not revoke tokens at the provider."))
        void mutateAuth("clear_oauth");
    });
    elements.executorIntegrationClose.addEventListener("click", () => elements.executorIntegrationDialog.close());
    elements.executorIntegrationCancel.addEventListener("click", () => elements.executorIntegrationDialog.close());
    elements.executorIntegrationDialog.addEventListener("close", () => { state.editor = null; elements.executorMcpSecretValue.value = ""; elements.executorIntegrationSave.disabled = false; });
    elements.executorIntegrationForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const edit = state.editor;
      if (!edit) return;
      try { void mutateEditor(edit.item ? "update" : (edit.kind === "skill" ? "create" : "register"), edit.kind === "mcp" ? editorConfig() : null); }
      catch (error) { editorStatus(error instanceof Error ? error.message : String(error)); }
    });
    elements.executorIntegrationValidate.addEventListener("click", () => void mutateEditor("validate"));
    elements.executorIntegrationToggle.addEventListener("click", () => { if (state.editor) void mutateEditor(state.editor.enabled ? "disable" : "enable"); });
    elements.executorIntegrationDelete.addEventListener("click", () => {
      const edit = state.editor;
      if (edit && globalThis.confirm(`Delete ${edit.kind === "skill" ? "Skill" : "MCP server"} “${edit.name}”?`))
        void mutateEditor(edit.kind === "skill" ? "delete" : "remove");
    });
    const tabs = elements.executorSubtabs;
    for (const [index, button] of tabs.entries()) {
      button.addEventListener("click", () => setSubtab(button.dataset.executorSubtab));
      button.addEventListener("keydown", (event) => {
        const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
          : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home" ? 0
          : event.key === "End" ? tabs.length - 1 : null;
        if (next === null) return;
        event.preventDefault();
        setSubtab(tabs[next].dataset.executorSubtab);
        tabs[next].focus();
      });
    }
    elements.executorPairOpen.addEventListener("click", () => {
      clearPairReview();
      elements.executorPairForm.reset();
      elements.executorPairDialog.showModal();
      elements.executorPairCode.focus();
    });
    elements.executorPairForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const code = elements.executorPairCode.value.trim().toUpperCase();
      if (!code) return;
      const button = elements.executorPairForm.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        const view = await request(`/pair?code=${encodeURIComponent(code)}`);
        showPairReview(view);
      } catch (error) {
        if (error.authenticationRequired) {
          void reloadApp();
          return;
        }
        clearPairReview();
        elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        button.disabled = false;
      }
    });
    elements.executorPairApprove.addEventListener("click", () => {
      void pairDecision("approve").catch((error) => {
        if (error.authenticationRequired) void reloadApp();
        else elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      });
    });
    elements.executorPairDeny.addEventListener("click", () => {
      void pairDecision("deny").catch((error) => {
        if (error.authenticationRequired) void reloadApp();
        else elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      });
    });
    async function executorAction(action) {
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      await request("/executors/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ executor_id: executor.executor_id }),
      });
      await refresh({ force: true });
    }
    elements.executorReset.addEventListener("click", () => {
      void executorAction("reset").catch((error) => {
        if (error.authenticationRequired) void reloadApp();
        else elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      });
    });
    elements.executorDrain.addEventListener("click", () => {
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      const action = executor.draining ? "resume" : "drain";
      void executorAction(action).catch((error) => {
        if (error.authenticationRequired) void reloadApp();
        else elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      });
    });
    elements.executorRenameOpen.addEventListener("click", () => {
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      elements.executorRenameName.value = executor.name || "";
      elements.executorRenameDialog.showModal();
      elements.executorRenameName.select();
    });
    elements.executorRenameForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      const name = elements.executorRenameName.value.trim();
      if (!name) return;
      try {
        await request("/executors/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ executor_id: executor.executor_id, name }),
        });
        closeDialog(elements.executorRenameDialog);
        await refresh({ force: true });
      } catch (error) {
        if (error.authenticationRequired) void reloadApp();
        else elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      }
    });
    elements.executorRevokeOpen.addEventListener("click", () => {
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      elements.executorRevokeName.textContent = executor.name || executor.executor_id;
      elements.executorRevokeDialog.showModal();
    });
    elements.executorRevokeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      try {
        await request("/executors/revoke", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ executor_id: executor.executor_id }),
        });
        closeDialog(elements.executorRevokeDialog);
        await refresh({ force: true });
      } catch (error) {
        if (error.authenticationRequired) void reloadApp();
        else elements.executorState.textContent = error instanceof Error ? error.message : String(error);
      }
    });
    for (const button of document.querySelectorAll("[data-close-executor-dialog]")) {
      button.addEventListener("click", () => {
        const dialog = document.getElementById(button.dataset.closeExecutorDialog || "");
        closeDialog(dialog);
      });
    }
    elements.executorPairDialog.addEventListener("close", clearPairReview);
  }

  function activate() {
    if (!selectedExecutor()) return;
    void loadSkillSources(true);
    void refreshMetrics();
  }

  return {
    activate,
    bind,
    select,
    refresh,
    reset,
    startPolling,
    stopPolling,
  };
}
