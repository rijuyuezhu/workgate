import { dashboardBytes, renderDashboardSparkline } from "./dashboard.js";

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
    metricExecutorId: "",
    metricLoading: false,
    detailTabs: new Map(),
  };

  function selectedExecutor() {
    return state.executors.find((item) => item.executor_id === state.selectedId) || null;
  }

  function select(executorId) {
    if (!state.executors.some((item) => item.executor_id === executorId)) return;
    state.selectedId = executorId;
    state.metricExecutorId = executorId;
    state.metricHistory = [];
    elements.executorDiagnosticsBody.replaceChildren();
    renderList();
    renderDetails();
    renderDiagnostics();
    void loadDiagnostics();
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
        state.metricExecutorId = executor.executor_id;
        state.metricHistory = [];
        elements.executorDiagnosticsBody.replaceChildren();
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

  function renderDiagnosticCards(container, local) {
    // Reuse the actual Overview resource cards, not another card implementation.
    const template = document.querySelector("#dashboard-panel .dashboard-summary");
    if (!template) return;
    const cards = template.cloneNode(true);
    cards.removeAttribute("aria-label");
    for (const node of cards.querySelectorAll("[id]")) node.removeAttribute("id");
    for (const node of cards.querySelectorAll(".dashboard-live")) node.remove();
    const health = local?.health || {};
    const history = state.metricHistory;
    for (const [index, key] of ["cpu_percent", "memory_percent", "disk_percent"].entries()) {
      const number = health[key];
      cards.children[index].querySelector("strong").textContent =
        typeof number === "number" ? `${number.toFixed(1)}%` : "—";
    }
    for (const [index, key] of ["cpu_percent", "memory_percent", "disk_percent"].entries()) {
      renderDashboardSparkline(cards.children[index].querySelector(".dashboard-sparkline"), history.map((value) => value[key]), 100);
    }
    const network = cards.children[3];
    const rates = network.querySelectorAll(".dashboard-network-values strong");
    for (const [index, key] of ["network_rx_bps", "network_tx_bps"].entries()) {
      const value = health[key];
      rates[index].textContent = dashboardBytes(value);
    }
    const rx = health.network_rx_bps;
    const tx = health.network_tx_bps;
    const total = (typeof rx === "number" ? rx : 0) + (typeof tx === "number" ? tx : 0);
    const bar = network.querySelector(".dashboard-network-bar");
    if (bar) {
      bar.children[0].style.width = total > 0 ? `${100 * (rx || 0) / total}%` : "0%";
      bar.children[1].style.width = total > 0 ? `${100 * (tx || 0) / total}%` : "0%";
    }
    const footValues = [
      [`Load ${typeof health.load_1m === "number" ? health.load_1m.toFixed(2) : "—"}`, `${health.cpu_count ?? "—"} cores`],
      [`${dashboardBytes(health.memory_used_bytes)} used`, `${dashboardBytes(health.memory_total_bytes)} total`],
      [`${dashboardBytes(health.disk_used_bytes)} used`, `${dashboardBytes(health.disk_total_bytes)} total`],
    ];
    for (const [index, values] of footValues.entries()) {
      const nodes = cards.children[index].querySelectorAll(".dashboard-metric-foot span");
      nodes[0].textContent = values[0];
      nodes[1].textContent = values[1];
    }
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
    if (showMetrics && !body.childElementCount) renderDiagnosticCards(body, {health: state.metricHistory.at(-1) || {}});
    if (!selectedExecutor()) return;
    const status = (message) => {
      addGroup(mcp, "Control", [{title: message, empty: true}]);
      addGroup(skills, "Executor", [{title: message, empty: true}]);
    };
    if (state.diagnosticId !== state.selectedId) return status("Unavailable");
    if (state.diagnosticLoading) return;
    if (state.diagnosticError) return status("Unavailable");
    const data = state.diagnostic;
    if (!data) return status("Unavailable");

    function mcpRows(snapshot) {
      if (!snapshot) return [{title: "Unavailable", empty: true}];
      const rows = (snapshot.servers || []).map((server) => ({
        title: server.name, detail: server.type.toUpperCase(), icon: server.type,
        status: server.enabled ? "Enabled" : "Disabled",
      }));
      if (rows.length) return rows;
      return [{title: "No configured servers", empty: true}];
    }
    addGroup(mcp, "Executor", mcpRows(data.executor?.bridge));
    addGroup(mcp, "Control", mcpRows(data.control_mcp));
    if (data.executor?.bridge?.errors || data.control_mcp?.errors) {
      addGroup(mcp, "Configuration warnings", [{title: "MCP configuration invalid", warning: true}]);
    }

    const local = data.executor;
    if (!local) {
      addGroup(skills, "Executor", [{title: "Unavailable", empty: true}]);
      renderDiagnosticCards(body, null);
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
        rows.length ? rows : [{title: "No Skills", empty: true}]);
    }
    if (skillData.warnings > skillData.duplicates) {
      addGroup(skills, "Warnings", [{title: `${skillData.warnings - skillData.duplicates} Skill warnings`, icon: "info", warning: true}]);
    }

  }

  async function refreshMetrics() {
    const executor = selectedExecutor();
    if (!executor?.online || executor.revoked_at || state.metricLoading || !elements.executorDiagnosticsBody.closest(".app-view") || elements.executorDiagnosticsBody.closest(".app-view").hidden) return;
    const executorId = executor.executor_id;
    state.metricLoading = true;
    try {
      const data = await request(`/dashboard?executor_id=${encodeURIComponent(executorId)}`);
      if (state.selectedId !== executorId || !selectedExecutor()?.online) return;
      const system = data?.system || {};
      const sample = {};
      for (const key of ["cpu_percent", "memory_percent", "disk_percent", "network_rx_bps", "network_tx_bps", "load_1m", "cpu_count", "memory_used_bytes", "memory_total_bytes", "disk_used_bytes", "disk_total_bytes"]) {
        sample[key] = typeof system[key] === "number" ? system[key] : null;
      }
      sample.network_total = sample.network_rx_bps !== null && sample.network_tx_bps !== null
        ? sample.network_rx_bps + sample.network_tx_bps : null;
      state.metricHistory.push(sample);
      state.metricHistory = state.metricHistory.slice(-60);
      const body = elements.executorDiagnosticsBody;
      if (!selectedExecutor()?.online || executorId !== state.selectedId) return;
      body.replaceChildren();
      renderDiagnosticCards(body, {health: sample});
    } catch (error) {
      if (error.authenticationRequired) void reloadApp();
    } finally {
      state.metricLoading = false;
    }
  }

  async function loadDiagnostics(force = false) {
    const executorId = state.selectedId;
    if (!executorId || (state.diagnosticId === executorId && !force)) return;
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
      state.selectedId = state.executors[0]?.executor_id || "";
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
        state.metricExecutorId = state.selectedId;
        state.metricHistory = [];
        void loadDiagnostics();
        void refreshMetrics();
      }
    } else {
      // Status may change without changing selection; preserve the existing trend DOM.
      const showMetrics = Boolean(selectedExecutor()?.online && !selectedExecutor()?.revoked_at);
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
    for (const button of elements.executorSubtabs) {
      button.addEventListener("click", () => setSubtab(button.dataset.executorSubtab));
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

  return {
    bind,
    select,
    refresh,
    reset,
    startPolling,
    stopPolling,
  };
}
