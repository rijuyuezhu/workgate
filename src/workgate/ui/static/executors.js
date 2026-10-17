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
    diagnosticId: "",
    diagnostic: null,
    diagnosticGeneration: 0,
    diagnosticLoading: false,
    diagnosticError: "",
    metricHistory: [],
    metricGeneration: 0,
    metricInFlightId: "",
    metricOnline: false,
    detailTabs: new Map(),
  };

  function selectedExecutor() {
    return state.executors.find((item) => item.executor_id === state.selectedId) || null;
  }

  function resetMetrics() {
    state.metricGeneration += 1;
    state.metricInFlightId = "";
    state.metricHistory = [];
    state.metricOnline = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
    elements.executorDiagnosticsBody.replaceChildren();
  }

  function select(executorId) {
    if (!state.executors.some((item) => item.executor_id === executorId)) return;
    state.selectedId = executorId;
    resetMetrics();
    renderList();
    renderDetails();
    renderDiagnostics();
    void loadDiagnostics(true);
    void refreshMetrics();
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
        state.selectedId = executor.executor_id;
        resetMetrics();
        renderList();
        renderDetails();
        renderDiagnostics();
        void loadDiagnostics();
        void refreshMetrics();
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
      if (row.status) {
        const badge = document.createElement("span");
        badge.className = "executor-manage-pill";
        if (row.status === 'Enabled') badge.dataset.state = 'enabled';
        if (row.status === 'Disabled') badge.dataset.state = 'disabled';
        if (row.status === 'Overridden') badge.dataset.state = 'overridden';
        badge.textContent = row.status;
        item.append(badge);
      }
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

  function renderDiagnostics() {
    const body = elements.executorDiagnosticsBody;
    const mcp = elements.executorMcpBody;
    const skills = elements.executorSkillsBody;
    mcp.replaceChildren();
    skills.replaceChildren();
    const showMetrics = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
    body.hidden = !showMetrics;
    if (showMetrics && !body.childElementCount) renderDiagnosticCards(body, state.metricHistory.at(-1) || {});
    if (!selectedExecutor()) return;
    const status = (message) => {
      addGroup(mcp, "Executor", [{title: message, empty: true}]);
      addGroup(mcp, "Control", [{title: message, empty: true}]);
      addGroup(skills, "Executor", [{title: message, empty: true}]);
      addGroup(skills, "Executor (User Global)", [{title: message, empty: true}]);
    };
    if (state.diagnosticId !== state.selectedId) return status("Unavailable");
    if (state.diagnosticLoading) {
      status("Loading");
      return;
    }
    if (state.diagnosticError) return status("Unavailable");
    const data = state.diagnostic;
    if (!data) return status("Unavailable");

    function mcpRows(snapshot, globallyEnabled = true) {
      if (!snapshot || snapshot.status === "invalid_config" || snapshot.status === "unavailable") {
        return [{title: "Configuration unavailable", empty: true}];
      }
      const rows = (snapshot.servers || []).map((server) => ({
        title: server.name || "Unknown MCP",
        detail: typeof server.type === "string" ? server.type.toUpperCase() : "Unknown",
        icon: server.type,
        status: typeof server.enabled !== "boolean" ? "Unknown" : server.enabled && globallyEnabled ? "Enabled" : "Disabled",
      }));
      if (rows.length) return rows;
      return [{title: globallyEnabled ? "No configured servers" : "Agent Bridge disabled", empty: true}];
    }
    addGroup(mcp, "Executor", mcpRows(data.executor?.bridge));
    addGroup(mcp, "Control", mcpRows(data.control_mcp, data.control_mcp?.enabled !== false));
    if (data.executor?.bridge?.errors || data.control_mcp?.errors) {
      addGroup(mcp, "Configuration warnings", [{title: "MCP configuration invalid", warning: true}]);
    }

    const local = data.executor;
    if (!local) {
      addGroup(skills, "Executor", [{title: "Unavailable", empty: true}]);
      addGroup(skills, "Executor (User Global)", [{title: "Unavailable", empty: true}]);
      return;
    }
    const skillData = local.skills || {};
    const selected = skillData.selected || [];
    const overridden = skillData.overridden || [];
    for (const source of ["managed", "global"]) {
      const rows = selected.filter((skill) => skill.source === source).map((skill) => ({
        title: skill.name, icon: "skill",
      }));
      rows.push(...overridden.filter((skill) => skill.source === source).map((skill) => ({
        title: skill.name, icon: "skill", status: "Overridden",
      })));
      addGroup(skills, source === "managed" ? "Executor" : "Executor (User Global)",
        rows.length ? rows : [{title: local.bridge?.status === "invalid_config" ? "Configuration unavailable" : skillData.unavailable ? "Skills unavailable" : skillData.enabled === false ? "Skills disabled" : "No Skills", empty: true}]);
    }
    if (skillData.warnings > skillData.duplicates) {
      addGroup(skills, "Warnings", [{title: `${skillData.warnings - skillData.duplicates} Skill warnings`, icon: "info", warning: true}]);
    }

  }

  async function refreshMetrics() {
    const executor = selectedExecutor();
    const view = elements.executorDiagnosticsBody.closest("[data-app-view]");
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
      const body = elements.executorDiagnosticsBody;
      body.replaceChildren();
      renderDiagnosticCards(body, sample);
    } catch (error) {
      if (error.authenticationRequired) { void reloadApp(); return; }
      if (generation !== state.metricGeneration || state.selectedId !== executorId) return;
      const message = document.createElement("p");
      message.className = "executor-metric-error";
      message.textContent = error.status === 403 ? "Resource metrics require shell:read access." : "Resource metrics unavailable.";
      elements.executorDiagnosticsBody.querySelector(".executor-metric-error")?.remove();
      elements.executorDiagnosticsBody.append(message);
    } finally {
      if (generation === state.metricGeneration && state.metricInFlightId === executorId) state.metricInFlightId = "";
    }
  }

  async function loadDiagnostics(force = false) {
    const executorId = state.selectedId;
    if (elements.executorMcpBody.closest("[data-app-view]")?.hidden) return;
    if (!executorId || !selectedExecutor() || selectedExecutor().revoked_at) {
      state.diagnosticGeneration += 1;
      state.diagnosticId = "";
      state.diagnostic = null;
      state.diagnosticLoading = false;
      state.diagnosticError = "";
      renderDiagnostics();
      return;
    }
    if (state.diagnosticId === executorId && !force) return;
    const generation = ++state.diagnosticGeneration;
    state.diagnosticId = executorId;
    state.diagnostic = null;
    state.diagnosticLoading = true;
    state.diagnosticError = "";
    renderDiagnostics();
    try {
      const data = await request(`/executors/diagnostics?executor_id=${encodeURIComponent(executorId)}`);
      if (generation !== state.diagnosticGeneration || executorId !== state.selectedId) return;
      state.diagnostic = data;
    } catch (error) {
      if (generation !== state.diagnosticGeneration || executorId !== state.selectedId) return;
      if (error.authenticationRequired) { void reloadApp(); return; }
      state.diagnosticError = error instanceof Error ? error.message : String(error);
    } finally {
      if (generation === state.diagnosticGeneration) {
        state.diagnosticLoading = false;
        renderDiagnostics();
      }
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
      state.diagnosticGeneration += 1;
      state.diagnosticId = "";
      state.diagnostic = null;
      state.diagnosticLoading = false;
      state.diagnosticError = "";
      renderDiagnostics();
      if (state.selectedId) {
        resetMetrics();
        void loadDiagnostics();
        void refreshMetrics();
      }
    } else {
      if (selectedExecutor()?.revoked_at && state.diagnosticId === state.selectedId) {
        state.diagnosticGeneration += 1;
        state.diagnosticId = "";
        state.diagnostic = null;
        state.diagnosticLoading = false;
        state.diagnosticError = "";
        renderDiagnostics();
      }
      const showMetrics = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
      if (showMetrics !== state.metricOnline) {
        resetMetrics();
        state.metricOnline = showMetrics;
        if (showMetrics) {
          renderDiagnostics();
          void loadDiagnostics(true);
          void refreshMetrics();
        }
      }
      elements.executorDiagnosticsBody.hidden = !showMetrics;
    }
    onInventory(state.executors);
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
    state.diagnosticGeneration += 1;
    state.diagnosticId = "";
    state.diagnostic = null;
    state.diagnosticError = "";
    state.diagnosticLoading = false;
    resetMetrics();
    state.metricOnline = false;
    state.detailTabs.clear();
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
    renderDiagnostics();
  }

  function bind() {
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
    void loadDiagnostics(true);
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
