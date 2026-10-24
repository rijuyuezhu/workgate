import { renderResourceCards } from "./dashboard.js";
import { createSkillFileEditor } from "./skill_file_editor.js";
import { createExecutorTopology } from "./executor_topology.js";
import { createConfigurationView } from "./config_inspector.js";

// A UI selection, not an Executor ID; never send it to executor-scoped APIs.
const CONTROL_SELECTION = "__control__";

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
    selectedId: CONTROL_SELECTION,
    controlAvailable: false,
    inventoryUpdatedAt: 0,
    inventoryStale: false,
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
    managementPending: false,
    editor: null,
    oauthSessions: new Map(),
    oauthTimer: null,
  };

  const topology = createExecutorTopology({
    request,
    onAuthenticationRequired: () => void reloadApp(),
    onClose: () => onNavigate("list", {replace: true}),
  });

  const executorConfigView = createConfigurationView({
    root: document.getElementById("executor-config-body"), request,
  });

  const skillFileEditor = createSkillFileEditor({
    request,
    onClose: () => void loadManagement(true),
  });

  function clearConfiguration() {
    executorConfigView.clear();
  }

  function isControlSelected() {
    return state.selectedId === CONTROL_SELECTION;
  }

  function selectedExecutor() {
    return state.executors.find((item) => item.executor_id === state.selectedId) || null;
  }

  function canManageSource(result, owner) {
    return Boolean(result?.can_manage && !result._stale && !state.inventoryStale
      && (owner === "control" || Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at)));
  }

  // Shared presentation for actionable state. Plain progress/empty states stay quiet.
  function notice(element, message, tone = "plain") {
    element.textContent = message;
    element.classList.toggle("executor-notice", Boolean(message) && tone !== "plain");
    if (message && tone !== "plain") element.dataset.tone = tone;
    else delete element.dataset.tone;
    element.setAttribute("role", tone === "error" ? "alert" : "status");
  }

  function resetMetrics() {
    state.metricGeneration += 1;
    state.metricInFlightId = "";
    state.metricHistory = [];
    state.metricOnline = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
    document.getElementById("executor-overview-metrics")?.replaceChildren();
  }

  function select(executorId) {
    if (executorId !== CONTROL_SELECTION && !state.executors.some((item) => item.executor_id === executorId)) return;
    const changed = state.selectedId !== executorId;
    // Changing the selected Executor cannot cancel a committed remote Apply.
    if (changed && state.editor?.busy && state.editor.applying) return;
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
      state.managementPending = false;
      state.managementId = "";
      state.management = null;
      notice(elements.executorActionStatus, "");
    }
    if (changed) { resetMetrics(); executorConfigView.clear(); }
    renderList();
    renderDetails();
    if (changed && activeSubtab() === "config") void executorConfigView.open(isControlSelected() ? "" : state.selectedId);
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
    const mutable = selected && !selected.revoked_at && !state.inventoryStale;
    elements.executorReset.disabled = state.loading || !mutable;
    elements.executorDrain.disabled = state.loading || !mutable;
    elements.executorRenameOpen.disabled = state.loading || !mutable;
    elements.executorRevokeOpen.disabled = state.loading || !mutable;
    elements.executorDrain.textContent = selected?.draining ? "Resume" : "Drain";
    elements.executorReset.closest(".executor-detail-actions").hidden = isControlSelected();
  }

  function renderList() {
    // Preserve card DOM/focus through the 4-second inventory refresh.
    const entries = [
      {key: CONTROL_SELECTION, name: "Control", label: "Control plane",
       status: state.inventoryStale ? "stale" : state.controlAvailable ? "available" : "unavailable"},
      ...state.executors.map(executor => ({
        key: executor.executor_id,
        name: executor.name || executor.executor_id,
        label: executor.runtime?.workgate_version || "Executor",
        status: executor.revoked_at ? "revoked" : executor.draining ? "draining" : executor.online ? "online" : "offline",
      })),
    ];
    const controlGroup = elements.executorList.querySelector(".executor-control-group");
    const peerGroup = elements.executorList.querySelector(".executor-peer-group");
    const peerRows = peerGroup.querySelector(".executor-peer-rows");
    peerGroup.hidden = state.executors.length === 0;
    const existing = new Map(Array.from(elements.executorList.querySelectorAll(".executor-row"),
      child => [child.dataset.selection, child]));
    let peerPosition = 0;
    for (const entry of entries) {
      let button = existing.get(entry.key);
      existing.delete(entry.key);
      if (!button) {
        button = document.createElement("button");
        button.type = "button";
        button.className = "executor-band executor-row";
        button.dataset.selection = entry.key;
        const indicator = document.createElement("span");
        indicator.className = "executor-band-mark";
        const main = document.createElement("span");
        main.className = "executor-band-main";
        const name = document.createElement("strong");
        name.className = "executor-band-name";
        const meta = document.createElement("span");
        meta.className = "executor-band-meta";
        main.append(name, meta);
        const status = document.createElement("span");
        status.className = "executor-band-state";
        button.append(indicator, main, status);
        button.addEventListener("click", () => {
          select(button.dataset.selection);
          onNavigate("detail");
        });
      }
      const selected = entry.key === state.selectedId;
      button.classList.toggle("executor-row-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
      button.title = entry.name;
      button.querySelector(".executor-band-mark").textContent = entry.name.slice(0, 1).toUpperCase();
      button.querySelector(".executor-band-name").textContent = entry.name;
      button.querySelector(".executor-band-meta").textContent = entry.label;
      const status = button.querySelector(".executor-band-state");
      status.textContent = entry.status;
      status.dataset.status = entry.status === "available" ? "online"
        : entry.status === "unavailable" ? "offline" : entry.status;
      const group = entry.key === CONTROL_SELECTION ? controlGroup : peerRows;
      const position = entry.key === CONTROL_SELECTION ? 0 : peerPosition++;
      if (group.children[position] !== button)
        group.insertBefore(button, group.children[position] || null);
    }
    for (const stale of existing.values()) stale.remove();
  }

  function activeSubtab() {
    return state.detailTabs.get(state.selectedId) || "overview";
  }

  function setSubtab(tab) {
    if (!state.selectedId || !["overview", "mcp", "skills", "config"].includes(tab)
      || tab === activeSubtab()) return;
    state.detailTabs.set(state.selectedId, tab);
    renderSubtabs();
    if (tab === "config") void executorConfigView.open(isControlSelected() ? "" : state.selectedId);
    else executorConfigView.clear();
    if (tab === "overview") void refreshMetrics();
    if (tab === "mcp" || tab === "skills") {
      // Retry only unavailable/stale sources; keep fresh Control data.
      void loadManagement();
    }
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

  function addGroup(container, title, rows, key, revision = 0) {
    const signature = JSON.stringify([title, revision, rows.map((row) => [
      row.title, row.detail, row.status, row.authState, row.authMode, row.notice,
      row.icon, row.revision, row.warning, row.empty, Boolean(row.manage),
    ])]);
    const previous = Array.from(container.children).find((node) => node.dataset.sourceKey === key);
    if (previous?.dataset.signature === signature) return;
    const group = document.createElement("section");
    group.dataset.sourceKey = key;
    group.dataset.signature = signature;
    group.className = "executor-manage-group";
    const heading = document.createElement("h4");
    heading.textContent = title;
    group.append(heading);
    for (const row of rows) {
      if (row.notice) {
        const message = document.createElement("p");
        notice(message, row.title, row.notice);
        group.append(message);
        continue;
      }
      const item = document.createElement("div");
      item.className = "executor-manage-row";
      if (row.empty) item.classList.add("executor-manage-empty");
      if (row.status === "Disabled") item.dataset.state = "disabled";
      if (row.status === "Enabled") item.dataset.state = "enabled";
      if (row.status === "Overridden") item.dataset.state = "overridden";
      if (row.authState) item.dataset.auth = row.authState;
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
      if (row.authState) {
        const auth = document.createElement("span");
        auth.className = "executor-row-auth";
        auth.textContent = row.authMode === "secret"
          ? (row.authState === "authorized" ? "✓ Secret configured" : "Secret required")
          : (row.authState === "authorized" ? "✓ Authorized" : "Authorization required");
        text.append(auth);
      }
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
    if (previous) previous.replaceWith(group);
    else container.append(group);
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

  async function loadManagement(force = false, executorOnly = false) {
    const executorId = state.selectedId;
    const sameSelection = state.managementId === executorId;
    const allSources = isControlSelected()
      ? [["control", "mcp", "control"], ["control", "skill", "controlSkills"]]
      : [["control", "mcp", "control"], ["executor", "mcp", "executorMcp"],
         ["executor", "skill", "skills"], ["control", "skill", "controlSkills"]];
    const missing = (field) => !state.management?.[field]
      || state.management[field].error || state.management[field]._stale;
    if (sameSelection && !force && (state.managementPending
      || !allSources.some(([, , field]) => missing(field)))) return;
    const generation = ++state.managementGeneration;
    state.managementId = executorId;
    state.managementPending = true;
    // Connectivity changes affect only this Executor; do not refetch already
    // fresh Control sources. Manual tab return retries only failed sources.
    const sources = sameSelection && state.management
      ? allSources.filter(([owner, , field]) =>
          (force && (!executorOnly || owner === "executor")) || missing(field))
      : allSources;
    // Stale values are read-only and scoped to this selected Executor.
    if (!sameSelection) state.management = null;
    else if (state.management) for (const [, , field] of sources) {
      if (state.management[field] && !state.management[field].error)
        state.management[field] = {...state.management[field], _stale: true};
    }
    renderManagement();
    const online = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
    await Promise.all(sources.map(async ([owner, kind, field]) => {
      let result;
      if (owner === "executor" && !online) result = { error: "Offline" };
      else {
        try { result = await request(managementQuery(owner, kind)); }
        catch (error) {
          if (error.authenticationRequired) {
            void reloadApp();
            return;
          }
          result = { error: error instanceof Error ? error.message : String(error),
                     denied: error.status === 403 };
        }
      }
      if (generation !== state.managementGeneration || executorId !== state.selectedId) return;
      if (!state.management) state.management = {};
      // Keep prior data only for transient failures, never for revoked access.
      if (!result.error) state.management[field] = {...result, _updated: Date.now()};
      else if (result.denied || !state.management[field] || state.management[field].error)
        state.management[field] = result;
      renderManagement();
    }));
    if (generation === state.managementGeneration) state.managementPending = false;
  }

  function renderManagement() {
    const mcp = elements.executorMcpBody;
    const skills = elements.executorSkillsBody;
    const stateRows = (message) => [{ title: message, detail: "" }];
    const snap = state.controlAvailable ? state.management : null;
    const unavailable = state.controlAvailable ? null : {error: "Unavailable"};
    const renderGroup = (container, title, result, owner, kind, shadowed = new Set()) => {
      const key = `${owner}-${kind}`;
      if (!result || result.error) {
        addGroup(container, title, result?.error
          ? [{title: result.error, notice: result.error === "Offline" ? "warning" : "error"}]
          : stateRows("Loading…"), key);
        return;
      }
      const label = result._stale
        ? `${title} (stale ${new Date(result._updated).toLocaleTimeString()})` : title;
      const items = kind === "mcp" ? result.servers : result.skills;
      const rows = (Array.isArray(items) ? items : []).map((item) => ({
        title: item.name,
        revision: item.revision,
        detail: kind === "mcp" ? item.type : "SKILL.md",
        status: item.enabled ? (shadowed.has(item.name) ? "Overridden" : "Enabled") : "Disabled",
        authMode: kind === "mcp" ? item.auth?.mode : null,
        authState: kind === "mcp" && item.enabled && !shadowed.has(item.name)
          && item.auth?.mode !== "none" && typeof item.auth_authorized === "boolean"
          ? (item.auth_authorized ? "authorized" : "required") : null,
        manage: canManageSource(result, owner)
          ? () => void openEditor({owner, kind, item}) : null,
      }));
      addGroup(container, label, rows.length ? rows : stateRows("None"), key, result._updated);
    };
    if (!isControlSelected()) renderGroup(mcp, "Executor", unavailable || snap?.executorMcp, "executor", "mcp");
    const shadowedMcp = new Set((snap?.executorMcp?._stale ? [] : snap?.executorMcp?.servers || [])
      .filter((server) => server.enabled).map((server) => server.name));
    renderGroup(mcp, "Control", unavailable || snap?.control, "control", "mcp", shadowedMcp);
    if (!isControlSelected()) renderGroup(skills, "Executor", unavailable || snap?.skills, "executor", "skill");
    const globalsAvailable = Boolean(state.controlAvailable && selectedExecutor()?.online && state.skillSources?.status === "available" && state.skillSourceId === state.selectedId);
    const globalSkills = state.skillSources?.executor?.skills;
    const readOnly = globalsAvailable ? [
      ...(globalSkills?.selected || []).filter((skill) => skill.source === "global")
        .map((skill) => ({title: skill.name, detail: "", status: "Effective"})),
      ...(globalSkills?.overridden || []).filter((skill) => skill.source === "global")
        .map((skill) => ({title: skill.name, detail: "", status: "Overridden"})),
    ] : [];
    const globalStatus = !selectedExecutor()?.online ? "Offline"
      : !globalsAvailable ? "Unavailable" : "None";
    if (!isControlSelected()) addGroup(skills, "User Global", readOnly.length ? readOnly
      : globalStatus === "None" ? stateRows(globalStatus)
      : [{title: globalStatus, notice: "warning"}], "user-global-skill");
    // Control is the lowest-priority source; a same-name Executor or global
    // Skill masks it. Project-specific overrides are resolved per Session.
    const shadowed = new Set([
      ...((snap?.skills?._stale ? [] : snap?.skills?.skills || []).filter((skill) => skill.enabled).map((skill) => skill.name)),
      ...(globalsAvailable ? (globalSkills?.selected || []).filter((skill) => skill.source === "global").map((skill) => skill.name) : []),
    ]);
    renderGroup(skills, "Control", unavailable || snap?.controlSkills, "control", "skill", shadowed);
    // Preserve unrelated groups and their focused controls as each source resolves.
    for (const [container, keys] of [
      [mcp, isControlSelected() ? ["control-mcp"] : ["executor-mcp", "control-mcp"]],
      [skills, isControlSelected() ? ["control-skill"] : ["executor-skill", "user-global-skill", "control-skill"]],
    ]) {
      const sections = new Map(Array.from(container.children).map((node) => [node.dataset.sourceKey, node]));
      for (const [index, key] of keys.entries()) {
        const node = sections.get(key);
        if (node && container.children[index] !== node)
          container.insertBefore(node, container.children[index] || null);
      }
      for (const node of Array.from(container.children)) {
        if (!keys.includes(node.dataset.sourceKey)) node.remove();
      }
    }

    elements.executorSkillsAdd.disabled =
      !(canManageSource(snap?.skills, "executor") || canManageSource(snap?.controlSkills, "control"));
    elements.executorMcpAdd.disabled =
      !(canManageSource(snap?.control, "control") || canManageSource(snap?.executorMcp, "executor"));
  }

  // Existing Location selector: Control, current Executor, and other online Executors.
  function chosenLocation() {
    const value = elements.executorIntegrationOwner.value;
    return value === "control" ? {owner: "control", executorId: null}
      : {owner: "executor", executorId: value.startsWith("executor:") ? value.slice(9) : state.selectedId};
  }

  function populateLocations(edit) {
    const field = elements.executorIntegrationOwner;
    field.replaceChildren();
    const add = (value, label, disabled = false) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.disabled = disabled;
      field.append(option);
    };
    add("control", "Control", (edit.kind === "skill" ? state.management?.controlSkills : state.management?.control)?.can_manage === false);
    const current = state.selectedId;
    for (const executor of state.executors) {
      if (executor.revoked_at) continue;
      const value = executor.executor_id === current ? "executor" : `executor:${executor.executor_id}`;
      add(value, `Executor ${executor.name || executor.executor_id}`,
        !executor.online || (edit.kind !== "skill" && state.management?.executorMcp?.can_manage === false));
    }
    field.value = edit.owner === "control" ? "control" : (edit.executorId === current ? "executor" : `executor:${edit.executorId}`);
    field.disabled = false;
  }

  function setEditorType() {
    const editing = state.editor;
    if (!editing) return;
    const skill = editing.kind === "skill";
    const target = chosenLocation();
    const owner = target.owner;
    elements.executorIntegrationMcpFields.hidden = skill;
    elements.executorIntegrationSkillWrap.hidden = !skill;
    elements.executorIntegrationValidate.hidden = !skill;
    const moving = Boolean(editing.item && (owner !== editing.owner || (owner === "executor" && target.executorId !== editing.executorId)));
    elements.executorSkillFilesEntry.hidden = !skill || !editing.item
      || editing.owner !== "executor" || !selectedExecutor()?.online || moving;
    elements.executorSkillFilesOpen.disabled = !editing.filesReady || editing.busy || moving;
    elements.executorIntegrationToggle.hidden = !editing.item || moving;
    // Delete always acts on the original owner. Hide it while the selector
    // points elsewhere, so a user cannot mistake it for deleting the target.
    elements.executorIntegrationDelete.hidden = !editing.item || moving;
    elements.executorMcpAuthManagement.hidden = skill || !editing.item || moving;
    elements.executorIntegrationTestWrap.hidden = skill || !editing.item || moving;
    elements.executorIntegrationUrl.required = !skill && owner === "control";
    elements.executorIntegrationCommand.required = !skill && owner === "executor";
    if (skill) {
      if (moving) editorStatus("Changing Skill location; Apply moves SKILL.md. Skills with auxiliary files cannot be moved yet.", "warning");
      else if (editing.item && elements.executorIntegrationStatus.textContent.startsWith("Changing Skill location")) editorStatus("");
      return;
    }
    const mode = elements.executorIntegrationAuthMode.value;
    elements.executorIntegrationOAuthScopesWrap.hidden = mode !== "oauth";
    elements.executorMcpOAuthActions.hidden = mode !== "oauth" || !editing.item;
    // Secrets can be pre-stored even before an MCP switches to secret auth.
    elements.executorMcpSecretDetails.hidden = mode === "oauth" || !editing.item;
    elements.executorMcpOAuthAdvanced.hidden = mode !== "oauth" || !editing.item;
    renderBrowserOAuth(editing);
    elements.executorIntegrationUrlWrap.hidden = owner !== "control";
    elements.executorIntegrationCommandWrap.hidden = owner !== "executor";
    elements.executorIntegrationArgsWrap.hidden = owner !== "executor";
    if (moving) editorStatus(owner === editing.owner
      ? "Move to another Executor; secrets stay at the source."
      : "Move to a new location; configure auth on the target.", "warning");
    else if (editing.item && elements.executorIntegrationStatus.textContent.startsWith("Move to ")) editorStatus("");
  }

  function editorStatus(message, tone = "plain") {
    notice(elements.executorIntegrationStatus, message, tone);
  }

  function lockEditorInputs(locked) {
    for (const field of [elements.executorIntegrationContent, elements.executorIntegrationUrl,
      elements.executorIntegrationCommand, elements.executorIntegrationArgs,
      elements.executorIntegrationAdvanced, elements.executorIntegrationOAuthScopes]) {
      field.readOnly = locked;
    }
    elements.executorIntegrationAuthMode.disabled = locked;
    elements.executorIntegrationName.readOnly = locked || Boolean(state.editor?.item);
    elements.executorMcpSecretName.readOnly = locked;
    elements.executorMcpSecretValue.readOnly = locked;
  }

  // One editor operation at a time: Apply, validation, auth mutation, or MCP test.
  function setEditorBusy(edit, busy) {
    if (state.editor !== edit) return;
    edit.busy = busy;
    const ready = !busy && !edit.needsInspection;
    for (const button of [elements.executorIntegrationSave, elements.executorIntegrationToggle,
      elements.executorIntegrationDelete, elements.executorIntegrationValidate,
      elements.executorIntegrationTest, elements.executorMcpSecretSet,
      elements.executorMcpOAuthClear, elements.executorMcpOAuthLogout, elements.executorMcpOAuthAuthorize,
      elements.executorMcpOAuthCopy, elements.executorMcpOAuthCancel,
      ...elements.executorMcpSecretList.querySelectorAll("button")]) {
      button.disabled = !ready;
    }
    elements.executorSkillFilesOpen.disabled = !ready || !edit.filesReady;
    elements.executorIntegrationOwner.disabled = !ready;
    elements.executorIntegrationCancel.disabled = busy && edit.applying;
    elements.executorIntegrationClose.disabled = busy && edit.applying;
    lockEditorInputs(!ready);
  }

  async function openEditor({owner, kind, item = null}) {
    if (!selectedExecutor() && owner !== "control") return;
    state.editor = { owner, kind, executorId: owner === "executor" ? state.selectedId : null,
                     name: item?.name || "", revision: item?.revision || "", item,
                     enabled: item?.enabled !== false };
    const edit = state.editor;
    edit.filesReady = false;
    elements.executorIntegrationForm.reset();
    // A prior committed Apply may have closed the dialog while both were locked.
    elements.executorIntegrationClose.disabled = false;
    elements.executorIntegrationCancel.disabled = false;
    elements.executorIntegrationTitle.textContent = `${item ? "Edit" : "Add"} ${kind === "skill" ? "Skill" : "MCP server"}`;
    elements.executorIntegrationName.value = edit.name;
    elements.executorIntegrationSave.disabled = Boolean(item);
    lockEditorInputs(Boolean(item));
    populateLocations(edit);
    elements.executorIntegrationOwner.disabled = Boolean(item);
    elements.executorIntegrationName.readOnly = Boolean(item);
    elements.executorIntegrationContent.value = "";
    // Do not accept edits until the current revision/content has loaded;
    // otherwise a delayed GET can overwrite freshly typed changes.
    elements.executorIntegrationContent.disabled = Boolean(item);
    elements.executorIntegrationArgs.value = "[]";
    elements.executorIntegrationAdvanced.value = "{}";
    elements.executorIntegrationAuthMode.value = "none";
    elements.executorIntegrationOAuthScopes.value = "";
    elements.executorMcpSecretName.value = "";
    elements.executorMcpSecretValue.value = "";
    elements.executorMcpSecretDetails.open = false;
    elements.executorMcpSecretList.replaceChildren();
    elements.executorMcpSecretSet.disabled = Boolean(item);
    elements.executorMcpOAuthClear.disabled = Boolean(item);
    elements.executorMcpOAuthLogout.disabled = Boolean(item);
    elements.executorIntegrationTest.disabled = Boolean(item);
    elements.executorMcpAuthStatus.hidden = true;
    notice(elements.executorMcpAuthStatus, "");
    renderBrowserOAuth(edit);
    notice(elements.executorIntegrationTestResult, "");
    elements.executorIntegrationToggle.hidden = !item;
    elements.executorIntegrationDelete.hidden = !item;
    elements.executorIntegrationToggle.disabled = Boolean(item);
    elements.executorIntegrationDelete.disabled = Boolean(item);
    elements.executorIntegrationValidate.disabled = Boolean(item);
    elements.executorIntegrationToggle.textContent = edit.enabled ? "Disable" : "Enable";
    elements.executorIntegrationSave.textContent = item ? "Apply changes" : "Create";
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
        elements.executorIntegrationContent.disabled = false;
        elements.executorIntegrationValidate.disabled = false;
        edit.filesReady = true;
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
        elements.executorMcpSecretSet.disabled = false;
        elements.executorMcpOAuthClear.disabled = false;
        elements.executorMcpOAuthLogout.disabled = false;
        elements.executorMcpOAuthAuthorize.disabled = false;
        elements.executorMcpOAuthCopy.disabled = false;
        elements.executorMcpOAuthCancel.disabled = false;
        elements.executorIntegrationTest.disabled = false;
        void loadEditorAuth(edit);
      }
      editorStatus("");
      setEditorType();
      lockEditorInputs(false);
      elements.executorIntegrationOwner.disabled = false;
      elements.executorIntegrationSave.disabled = false;
      elements.executorIntegrationToggle.disabled = false;
      elements.executorIntegrationDelete.disabled = false;
    } catch (error) {
      if (state.editor !== edit) return;
      editorStatus(error instanceof Error ? error.message : String(error), "error");
      elements.executorIntegrationSave.disabled = true;
    }
  }

  async function mutateEditor(action, config = null) {
    const edit = state.editor;
    if (!edit || edit.busy) return;
    const applying = action !== "validate";
    edit.applying = applying;
    const destination = chosenLocation();
    const moving = Boolean(edit.item && action === "update"
      && (edit.owner !== destination.owner || (edit.owner === "executor" && destination.executorId !== edit.executorId)));
    const body = {owner: edit.owner, kind: edit.kind, action,
                  executor_id: edit.executorId, name: elements.executorIntegrationName.value.trim()};
    if (edit.kind === "skill") {
      if (["create", "update", "validate"].includes(action)) body.content = elements.executorIntegrationContent.value;
    } else if (config !== null) body.config = config;
    if (edit.revision && ["update", "enable", "disable", "delete", "remove"].includes(action)) body.expected_revision = edit.revision;
    setEditorBusy(edit, true);
    if (applying) {
      // A submitted cross-host mutation cannot reliably be cancelled. Keep
      // the dialog present until the server confirms its outcome and refresh.
      elements.executorIntegrationCancel.disabled = true;
      elements.executorIntegrationClose.disabled = true;
      elements.executorIntegrationOwner.disabled = true;
      editorStatus("Applying changes…");
    }
    try {
      const payload = moving ? {
        owner: edit.owner,
        executor_id: edit.owner === "executor" ? edit.executorId : null,
        destination_owner: destination.owner,
        destination_executor_id: destination.executorId,
        name: body.name,
        expected_revision: edit.revision,
        ...(edit.kind === "skill" ? {content: body.content} : {config}),
      } : body;
      const url = moving ? (edit.kind === "skill" ? "/integrations/skill-move" : "/integrations/move") : "/integrations";
      const result = await request(url, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
      });
      if (state.editor !== edit) return;
      if (action === "validate") { editorStatus("SKILL.md is valid.", "success"); return; }
      elements.executorIntegrationDialog.close();
      state.editor = null;
      if ((moving || action === "register" || action === "create") && destination.owner === "executor" && destination.executorId !== state.selectedId) {
        state.detailTabs.set(destination.executorId, edit.kind === "skill" ? "skills" : "mcp");
        select(destination.executorId);
      }
      await loadManagement(true);
      void loadSkillSources(true);
      return result;
    } catch (error) {
      if (state.editor !== edit) return;
      const code = error?.payload?.error;
      if (moving && code === "MoveNeedsEnable") {
        // Source was removed: do not offer Apply against a nonexistent source.
        elements.executorIntegrationDialog.close();
        state.editor = null;
        if (destination.owner === "executor" && destination.executorId !== state.selectedId) {
          state.detailTabs.set(destination.executorId, edit.kind === "skill" ? "skills" : "mcp");
          select(destination.executorId);
        }
        void loadManagement(true);
        void loadSkillSources(true);
        notice(elements.executorActionStatus,
          `${edit.kind === "skill" ? "Skill" : "MCP"} moved to ${destination.owner === "control" ? "Control" : "the destination Executor"}, but remains disabled. Manage it there to enable it${edit.kind === "skill" ? " (check destination-wide Skill settings if necessary)" : ""}.`, "warning");
      } else {
        if (moving && code === "MoveUncertain") edit.needsInspection = true;
        editorStatus(edit.needsInspection
          ? "Move outcome is uncertain. Inspect both locations before attempting another change; close this editor to refresh."
          : error instanceof Error ? error.message : String(error), "error");
      }
    } finally {
      // The old request must never unlock a newly opened editor.
      if (state.editor === edit) {
        edit.applying = false;
        setEditorBusy(edit, false);
      }
    }
  }

  function authQuery(edit) {
    const params = new URLSearchParams({owner: edit.owner, name: edit.name});
    if (edit.owner === "executor") params.set("executor_id", edit.executorId);
    return `/integrations/auth?${params.toString()}`;
  }

  async function loadEditorAuth(edit) {
    if (edit.kind !== "mcp" || !edit.item) return;
    const generation = edit.authGeneration = (edit.authGeneration || 0) + 1;
    try {
      const result = await request(authQuery(edit));
      if (state.editor !== edit || edit.authGeneration !== generation) return;
      edit.authStatus = result;
      const status = result.auth || {};
      if (edit.owner === "control" && status.mode === "oauth") {
        try {
          const pending = await request(`/integrations/oauth/current?name=${encodeURIComponent(edit.name)}`);
          if (state.editor !== edit || edit.authGeneration !== generation) return;
          if (pending?.session_id) state.oauthSessions.set(edit.name, {
            id:pending.session_id, url:pending.authorization_url || "", status:pending.status,
          });
          else state.oauthSessions.delete(edit.name);
        } catch { /* A disconnected status read must not block the MCP editor. */ }
      }
      renderBrowserOAuth(edit);
      const pending = Boolean(state.oauthSessions.get(edit.name));
      elements.executorMcpAuthStatus.hidden = status.status === "not_required" || (status.mode === "oauth" && pending && !status.authorized);
      elements.executorMcpAuthStatus.dataset.state = status.authorized ? "connected" : "";
      if (!elements.executorMcpAuthStatus.hidden) notice(
        elements.executorMcpAuthStatus,
        status.mode === "oauth" ? (status.authorized ? "✓ Connected" : "Not connected")
          : `Authorization: ${status.status || "unavailable"}`,
      );
      const list = elements.executorMcpSecretList;
      list.replaceChildren();
      for (const key of result.secret_names || []) {
        const line = document.createElement("div");
        line.className = "executor-auth-secret-row";
        const name = document.createElement("code");
        name.textContent = key;
        const label = document.createElement("span");
        label.textContent = "Value hidden";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.disabled = Boolean(edit.busy);
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
      if (state.editor === edit && edit.authGeneration === generation) {
        elements.executorMcpAuthStatus.hidden = false;
        notice(elements.executorMcpAuthStatus, error instanceof Error ? error.message : String(error), "error");
      }
    }
  }

  function renderBrowserOAuth(edit) {
    clearTimeout(state.oauthTimer);
    const session = state.oauthSessions.get(edit.name);
    const supported = edit.owner === "control" && edit.kind === "mcp"
      && elements.executorIntegrationAuthMode.value === "oauth";
    const authorized = Boolean(edit.authStatus?.auth?.authorized) || session?.status === "authorized";
    const active = session && ["waiting", "preparing", "completing"].includes(session.status);
    if (active && !authorized) elements.executorMcpAuthStatus.hidden = true;
    elements.executorMcpOAuthAuthorize.hidden = !supported || authorized || active;
    elements.executorMcpOAuthLogout.hidden = !supported || !authorized;
    const link = supported && Boolean(session?.url) && !authorized;
    elements.executorMcpOAuthLink.hidden = !link;
    elements.executorMcpOAuthCopy.hidden = !link;
    if (link) elements.executorMcpOAuthLink.href = session.url;
    elements.executorMcpOAuthCancel.hidden = !supported || !active || session.status === "completing";
    const messages = {
      waiting: "Waiting for authorization…",
      preparing: "Preparing authorization…",
      completing: "Connecting…",
      authorized: "Connected",
      denied: "Authorization denied. Try again.",
      failed: "Authorization failed. Try again.",
    };
    notice(elements.executorMcpOAuthProgress, supported ? (messages[session?.status] || "") : "",
      session?.status === "authorized" ? "success" : "plain");
    if (supported && active && state.editor === edit && !authorized) {
      // Only poll while the MCP editor owns this explicit OAuth session.
      state.oauthTimer = setTimeout(() => {
        if (state.editor === edit) void browserOAuth("check");
      }, 1700);
    }
  }

  async function browserOAuth(action) {
    const edit = state.editor;
    if (!edit || edit.owner !== "control" || edit.kind !== "mcp" || !edit.item || edit.busy) return;
    const name = edit.name;
    let session = state.oauthSessions.get(name);
    setEditorBusy(edit, true);
    try {
      if (action === "start") {
        const result = await request("/integrations/oauth/start", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({owner:"control", name}),
        });
        session = {id:result.session_id, url:result.authorization_url, status:"waiting"};
        state.oauthSessions.set(name, session);
      } else if (session && action === "cancel") {
        await request("/integrations/oauth/cancel", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({session_id:session.id}),
        });
        state.oauthSessions.delete(name);
      } else if (session && action === "check") {
        const result = await request("/integrations/oauth/status?session_id=" + encodeURIComponent(session.id));
        session.status = result.status;
        if (["completing", "authorized", "denied", "failed", "expired"].includes(result.status)) session.url = "";
        if (result.status === "authorized") {
          await loadEditorAuth(edit);
          void loadManagement(true);
        }
      }
    } catch (error) {
      if (state.editor === edit) {
        if (error?.status === 404 && session && action !== "start") {
          state.oauthSessions.delete(name);
          editorStatus("Authorization expired. Try again.", "warning");
        } else editorStatus(error instanceof Error ? error.message : String(error), "error");
      }
    } finally {
      if (state.editor === edit) {
        setEditorBusy(edit, false);
        renderBrowserOAuth(edit);
      }
    }
  }

  async function mutateAuth(action, fields = {}) {
    const edit = state.editor;
    if (!edit || edit.kind !== "mcp" || !edit.item || edit.busy) return;
    const body = {owner:edit.owner, executor_id:edit.executorId, name:edit.name, action, ...fields};
    // A previous auth read must not overwrite the mutation's new state.
    edit.authGeneration = (edit.authGeneration || 0) + 1;
    setEditorBusy(edit, true);
    try {
      const result = await request("/integrations/auth", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      if (state.editor !== edit) return;
      elements.executorMcpSecretValue.value = "";
      const message = action === "set_secret" ? "Secret stored securely."
        : action === "delete_secret" ? (result.changed ? "Secret removed." : "Secret already absent.")
        : action === "logout" ? `Signed out locally (remote revocation: ${result.remote_revocation || "unavailable"}).`
        : result.changed ? "Local OAuth state cleared (remote tokens not revoked)." : "No local OAuth state to clear.";
      editorStatus(message, result.changed === false ? "warning" : "success");
      if (action === "logout" || action === "clear_oauth") {
        state.oauthSessions.delete(edit.name);
        renderBrowserOAuth(edit);
      }
      await loadEditorAuth(edit);
      void loadManagement(true);
    } catch (error) {
      if (state.editor === edit) editorStatus(error instanceof Error ? error.message : String(error), "error");
    } finally {
      if (state.editor === edit) {
        elements.executorMcpSecretValue.value = "";
        setEditorBusy(edit, false);
      }
    }
  }

  async function testEditorMcp() {
    const edit = state.editor;
    if (!edit || edit.kind !== "mcp" || !edit.item || edit.busy) return;
    setEditorBusy(edit, true);
    notice(elements.executorIntegrationTestResult, "Testing saved MCP server…");
    try {
      const result = await request("/integrations/test", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({owner: edit.owner, executor_id: edit.executorId, name: edit.name}),
      });
      if (state.editor !== edit) return;
      const tools = Array.isArray(result.tools) ? result.tools : [];
      notice(
        elements.executorIntegrationTestResult,
        result.status === "available"
          ? `${tools.length} tools discovered (none called)`
          : `MCP ${result.status || "unavailable"}. Check the owning host.`,
        result.status === "available" ? "success" : "warning",
      );
      for (const tool of tools) {
        const line = document.createElement("div");
        line.textContent = `${tool.name}${tool.description ? `: ${tool.description}` : ""}`;
        elements.executorIntegrationTestResult.append(line);
      }
    } catch (error) {
      if (state.editor === edit) notice(elements.executorIntegrationTestResult, error instanceof Error ? error.message : String(error), "error");
    } finally {
      if (state.editor === edit) setEditorBusy(edit, false);
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
    const targetOwner = chosenLocation().owner;
    if (mode === "oauth" && targetOwner !== "control") throw new Error("OAuth requires a network HTTP MCP server.");
    const config = { type: targetOwner === "control" ? "http" : "stdio", ...advanced,
      enabled: edit.item ? edit.enabled : true,
      auth: {mode, scopes: mode === "oauth" ? [...new Set(elements.executorIntegrationOAuthScopes.value.trim().split(/\s+/).filter(Boolean))] : []} };
    if (targetOwner === "control") config.url = elements.executorIntegrationUrl.value.trim();
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
      notice(message, error.status === 403 ? "Resource metrics require shell:read access." : "Resource metrics unavailable.", "warning");
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
    const control = isControlSelected();
    document.getElementById("executor-detail-grid").hidden = control;
    document.getElementById("executor-control-overview").hidden = !control;
    document.getElementById("executor-overview-metrics").hidden = control || !executor?.online;
    if (control) {
      elements.executorDetailName.textContent = "Control";
      elements.executorDetailStatus.textContent = state.inventoryStale
        ? "Stale" : state.controlAvailable ? "Available" : "Unavailable";
      document.getElementById("executor-control-connection").textContent =
        state.inventoryStale ? "Stale" : state.controlAvailable ? "Connected" : "Unavailable";
      document.getElementById("executor-control-online").textContent =
        String(state.executors.filter((item) => item.online && !item.revoked_at).length);
      setControls();
      return;
    }
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
        ? "Update required: " + executor.required_workgate_version
        : "Compatible";
    elements.executorDetailPlatform.textContent = executor.runtime?.platform || "Not reported";
    elements.executorDetailAdmission.textContent = executor.session_admission
      ? "Eligible"
      : (executor.session_admission_reasons || []).join(", ") || "Not eligible";
    elements.executorDetailSessions.textContent = String(executor.active_sessions || 0);
    elements.executorDetailCommands.textContent =
      String(executor.queued_commands || 0) + " queued, " + String(executor.offered_commands || 0) + " offered (limit " + String(executor.command_limit || 0) + ")";
    elements.executorDetailCapabilities.textContent =
      (executor.capabilities || []).join(", ") || "None reported";
    elements.executorDetailCreated.textContent = timestamp(executor.created_at, "Unknown");
    elements.executorDetailLastSeen.textContent = timestamp(executor.last_seen_at);
    setControls();
  }

  function render(payload) {
    const previousId = state.selectedId;
    const wasStale = state.inventoryStale;
    state.inventoryStale = false;
    state.inventoryUpdatedAt = Date.now();
    state.executors = Array.isArray(payload?.executors) ? payload.executors : [];
    const recovered = !state.controlAvailable;
    state.controlAvailable = true;
    topology.update(state.executors);
    if (state.selectedId !== CONTROL_SELECTION && !state.executors.some((item) => item.executor_id === state.selectedId)) {
      state.selectedId = CONTROL_SELECTION;
    }
    const online = state.executors.filter((item) => !item.revoked_at && item.online).length;
    const revoked = state.executors.filter((item) => item.revoked_at).length;
    elements.executorOnline.textContent = String(online);
    elements.executorTotal.textContent = String(state.executors.length);
    elements.executorRevoked.textContent = String(revoked);
    document.getElementById("executor-revoked-summary").hidden = revoked === 0;
    notice(elements.executorState, `Updated ${new Date().toLocaleTimeString()}`);
    renderList();
    renderDetails();
    if (state.selectedId !== previousId) {
      // Inventory can remove an Executor while its integration editor is open.
      // Close stale, non-applying editors just as manual selection does.
      if (elements.executorIntegrationDialog.open && !state.editor?.applying)
        elements.executorIntegrationDialog.close();
      executorConfigView.clear();
      if (activeSubtab() === "config")
        void executorConfigView.open(isControlSelected() ? "" : state.selectedId);
      state.skillSourceGeneration += 1;
      state.skillSourceId = "";
      state.skillSources = null;
      state.managementGeneration += 1;
      state.managementPending = false;
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
        // Executor connectivity invalidates its own data, not Control's.
        state.managementGeneration += 1;
        state.managementPending = false;
        if (state.management) {
          state.management.executorMcp = showMetrics ? null : {error: "Offline"};
          state.management.skills = showMetrics ? null : {error: "Offline"};
        }
        renderManagement();
        if (["mcp", "skills"].includes(activeSubtab())) void loadManagement(true, true);
        if (showMetrics) {
          void loadSkillSources(true);
          void refreshMetrics();
        }
      }
      const metricsBody = document.getElementById("executor-overview-metrics");
      if (metricsBody) metricsBody.hidden = !showMetrics;
    }
    onInventory(state.executors);
    if ((wasStale || recovered) && ["mcp", "skills"].includes(activeSubtab())) void loadManagement(true);
  }

  async function refresh({ force = false } = {}) {
    if (state.loading && !force) return null;
    const generation = ++state.generation;
    state.loading = true;
    setControls();
    notice(elements.executorState, "Loading executors");
    try {
      const payload = await request("/executors");
      if (generation !== state.generation) return null;
      render(payload);
      return payload;
    } catch (error) {
      if (error.authenticationRequired) throw error;
      if (generation !== state.generation) return null;
      if (state.controlAvailable) {
        state.inventoryStale = true;
        notice(elements.executorState,
          `Stale (updated ${new Date(state.inventoryUpdatedAt).toLocaleTimeString()})`, "warning");
        if (state.management) for (const field of Object.keys(state.management)) {
          const value = state.management[field];
          if (value && !value.error) state.management[field] = {...value, _stale: true};
        }
        renderManagement();
        renderList();
        renderDetails();
        return null;
      }
      state.controlAvailable = false;
      state.managementGeneration += 1;
      state.managementPending = false;
      state.managementId = "";
      state.management = null;
      renderManagement();
      renderList();
      renderDetails();
      notice(elements.executorState, error instanceof Error ? error.message : String(error), "error");
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
    notice(elements.executorPairStatus, "");
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
    state.selectedId = CONTROL_SELECTION;
    state.controlAvailable = false;
    state.inventoryStale = false;
    state.inventoryUpdatedAt = 0;
    clearConfiguration();
    state.skillSourceGeneration += 1;
    state.skillSourceId = "";
    state.skillSources = null;
    resetMetrics();
    state.metricOnline = false;
    state.detailTabs.clear();
    state.managementGeneration += 1;
    state.managementId = "";
    state.management = null;
    state.managementPending = false;
    state.editor = null;
    // Discard pending OAuth links when the WebUI session resets.
    state.oauthSessions.clear();
    clearTimeout(state.oauthTimer);
    skillFileEditor.close(true);
    elements.executorIntegrationDialog.close();
    closeDialog(elements.executorPairDialog);
    closeDialog(elements.executorRenameDialog);
    closeDialog(elements.executorRevokeDialog);
    clearPairReview();
    elements.executorPairForm.reset();
    elements.executorRenameForm.reset();
    notice(elements.executorState, message);
    notice(elements.executorActionStatus, "");
    elements.executorOnline.textContent = "—";
    elements.executorTotal.textContent = "—";
    elements.executorRevoked.textContent = "—";
    document.getElementById("executor-revoked-summary").hidden = true;
    renderList();
    renderDetails();
    renderManagement();
  }

  function bind() {
    topology.bind();
    document.getElementById("executor-topology-open").addEventListener("click", () => topology.open());
    elements.executorIntegrationTest.addEventListener("click", () => void testEditorMcp());
    elements.executorMcpAdd.addEventListener("click", () => {
      const owner = canManageSource(state.management?.executorMcp, "executor")
        ? "executor" : "control";
      void openEditor({owner, kind: "mcp"});
    });
    elements.executorSkillsAdd.addEventListener("click", () => void openEditor({
      owner: canManageSource(state.management?.skills, "executor") ? "executor" : "control",
      kind: "skill",
    }));
    elements.executorIntegrationOwner.addEventListener("change", () => {
      // Existing entries retain their source owner until Apply completes.
      // New entries follow the selected location directly.
      if (state.editor && !state.editor.item) {
        const destination = chosenLocation();
        state.editor.owner = destination.owner;
        state.editor.executorId = destination.executorId;
      }
      setEditorType();
    });
    elements.executorIntegrationAuthMode.addEventListener("change", setEditorType);
    elements.executorMcpSecretSet.addEventListener("click", () => {
      const key = elements.executorMcpSecretName.value.trim();
      const value = elements.executorMcpSecretValue.value;
      if (!key || !value) { editorStatus("Enter both a secret reference name and a value.", "warning"); return; }
      void mutateAuth("set_secret", {secret_name:key, secret_value:value});
    });
    elements.executorMcpOAuthAuthorize.addEventListener("click", () => void browserOAuth("start"));
    elements.executorMcpOAuthCopy.addEventListener("click", async () => {
      const session = state.editor && state.oauthSessions.get(state.editor.name);
      if (!session?.url) return;
      try {
        await navigator.clipboard.writeText(session.url);
        notice(elements.executorMcpOAuthProgress, "Link copied · Waiting for authorization…");
      } catch {
        editorStatus("Clipboard unavailable. Open the authorization link instead.", "warning");
      }
    });
    elements.executorMcpOAuthCancel.addEventListener("click", () => void browserOAuth("cancel"));
    elements.executorMcpOAuthLogout.addEventListener("click", () => {
      if (globalThis.confirm("Try to revoke the OAuth token remotely and clear local OAuth state?"))
        void mutateAuth("logout");
    });
    elements.executorMcpOAuthClear.addEventListener("click", () => {
      if (globalThis.confirm("Clear locally stored OAuth state? This does not revoke tokens at the provider."))
        void mutateAuth("clear_oauth");
    });
    skillFileEditor.bind();
    elements.executorSkillFilesOpen.addEventListener("click", () => {
      const edit = state.editor;
      if (!edit || edit.kind !== "skill" || edit.owner !== "executor"
        || !edit.item || !edit.filesReady || edit.busy || chosenLocation().owner !== "executor"
        || chosenLocation().executorId !== edit.executorId) return;
      skillFileEditor.open({ executorId: edit.executorId, name: edit.name });
    });
    elements.executorIntegrationClose.addEventListener("click", () => elements.executorIntegrationDialog.close());
    elements.executorIntegrationCancel.addEventListener("click", () => elements.executorIntegrationDialog.close());
    elements.executorIntegrationDialog.addEventListener("cancel", (event) => {
      if (state.editor?.busy && state.editor.applying) event.preventDefault();
    });
    elements.executorIntegrationDialog.addEventListener("close", () => {
      skillFileEditor.close(true);
      const inspect = Boolean(state.editor?.needsInspection);
      state.editor = null;
      elements.executorMcpSecretValue.value = "";
      elements.executorIntegrationSave.disabled = false;
      if (inspect) {
        void loadManagement(true);
        void loadSkillSources(true);
      }
    });
    elements.executorIntegrationForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const edit = state.editor;
      if (!edit || edit.busy) return;
      try { void mutateEditor(edit.item ? "update" : (edit.kind === "skill" ? "create" : "register"), edit.kind === "mcp" ? editorConfig() : null); }
      catch (error) { editorStatus(error instanceof Error ? error.message : String(error), "error"); }
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
        notice(elements.executorPairStatus, error instanceof Error ? error.message : String(error), "error");
      } finally {
        button.disabled = false;
      }
    });
    elements.executorPairApprove.addEventListener("click", () => {
      void pairDecision("approve").catch((error) => {
        if (error.authenticationRequired) void reloadApp();
        else notice(elements.executorPairStatus, error instanceof Error ? error.message : String(error), "error");
      });
    });
    elements.executorPairDeny.addEventListener("click", () => {
      void pairDecision("deny").catch((error) => {
        if (error.authenticationRequired) void reloadApp();
        else notice(elements.executorPairStatus, error instanceof Error ? error.message : String(error), "error");
      });
    });
    async function executorAction(action) {
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      const executorId = executor.executor_id;
      notice(elements.executorActionStatus, "");
      try {
        await request("/executors/" + action, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ executor_id: executorId }),
        });
        await refresh({ force: true });
        if (state.selectedId === executorId) notice(elements.executorActionStatus, "");
      } catch (error) {
        if (error.authenticationRequired) void reloadApp();
        else if (state.selectedId === executorId)
          notice(elements.executorActionStatus, error instanceof Error ? error.message : String(error), "error");
      }
    }
    elements.executorReset.addEventListener("click", () => void executorAction("reset"));
    elements.executorDrain.addEventListener("click", () => {
      const executor = selectedExecutor();
      if (!executor || executor.revoked_at) return;
      void executorAction(executor.draining ? "resume" : "drain");
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
        notice(elements.executorActionStatus, "");
        await refresh({ force: true });
      } catch (error) {
        if (error.authenticationRequired) void reloadApp();
        else if (state.selectedId === executor.executor_id)
          notice(elements.executorActionStatus, error instanceof Error ? error.message : String(error), "error");
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
        notice(elements.executorActionStatus, "");
        await refresh({ force: true });
      } catch (error) {
        if (error.authenticationRequired) void reloadApp();
        else if (state.selectedId === executor.executor_id)
          notice(elements.executorActionStatus, error instanceof Error ? error.message : String(error), "error");
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

  async function openManagement(executorId, kind) {
    if (!["mcp", "skills"].includes(kind)) return;
    if (!state.executors.some((row) => row.executor_id === executorId)) {
      await refresh({ force: true });
    }
    if (!state.executors.some((row) => row.executor_id === executorId)) return;
    select(executorId);
    setSubtab(kind);
    onNavigate("detail");
  }

  function activate() {
    if (!selectedExecutor()) return;
    void loadSkillSources(true);
    void refreshMetrics();
  }

  return {
    activate,
    closeTopology: () => topology.close(),
    clearConfiguration,
    openManagement,
    bind,
    select,
    refresh,
    reset,
    startPolling,
    stopPolling,
  };
}
