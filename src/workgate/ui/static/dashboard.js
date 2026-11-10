function dashboardNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }


export function createDashboardController({
  elements,
  request,
  text,
  authMode,
  isAuthenticated,
  onAuthenticationRequired,
  onOpenTask,
  renderResources,
  renderSystemDetails,
  initialExecutorId = "",
}) {
  const controllerState = {
    executorId: initialExecutorId,
    executorPinned: Boolean(initialExecutorId),
    generation: 0,
    loading: false,
    executorStates: new Map(),
    history: [],
    lastSystem: {},
    timer: null,
  };

  function dashboardExecutorOnline(executorId = controllerState.executorId) {
    return Boolean(executorId) && controllerState.executorStates.get(executorId) === "online";
  }

  function setDashboardControls() {
    const online = dashboardExecutorOnline();
    elements.dashboardExecutor.disabled = controllerState.loading;
  }

  function stopDashboardPolling() {
    if (controllerState.timer !== null) {
      window.clearInterval(controllerState.timer);
      controllerState.timer = null;
    }
  }

  function startDashboardPolling() {
    stopDashboardPolling();
    controllerState.timer = window.setInterval(() => {
      if (!document.hidden && (authMode !== "oauth" || isAuthenticated()) && dashboardExecutorOnline()
          && !document.getElementById("dashboard-panel")?.hidden) {
        refreshDashboardInBackground();
      }
    }, 5000);
  }

  function dashboardEmpty(container, message) {
    container.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = message;
    container.append(empty);
  }


  function resetDashboardExecutor(executorId = "") {
    controllerState.executorId = executorId;
    controllerState.loading = false;
    controllerState.generation += 1;
    controllerState.history = [];
    controllerState.lastSystem = {};
    renderResources({}, [], "—");
    elements.dashboardExecutor.value = controllerState.executorId;
    elements.dashboardState.textContent = "Not loaded";
    elements.dashboardHealth.textContent = "—";
    elements.dashboardHealthDetail.textContent = "Waiting for telemetry";
    elements.dashboardHealthCard.className = "dashboard-card dashboard-health-card";
    document.querySelector(".dashboard-health-banner").dataset.health = "unknown";
    elements.dashboardAuditTotal.textContent = "—";
    elements.dashboardAuditDetail.textContent = "No activity loaded";
    renderSystemDetails(null);
    dashboardEmpty(elements.dashboardAlerts, `Alerts for ${controllerState.executorId} are not loaded.`);
    setDashboardControls();
  }

  function renderDashboardExecutors(targets) {
    const available = Array.isArray(targets) ? targets : [];
    const wasOnline = dashboardExecutorOnline();
    controllerState.executorStates = new Map();
    elements.dashboardExecutor.replaceChildren();
    const machineList = document.getElementById("dashboard-machine-list");
    machineList.replaceChildren();
    if (!available.length) dashboardEmpty(machineList, "No executors paired.");
    for (const executor of available) {
      const executorId = text(executor.executor_id, "");
      if (!executorId) continue;
      const state = text(executor.status, "offline");
      controllerState.executorStates.set(executorId, state);
      const option = document.createElement("option");
      option.value = executorId;
      const label = text(executor.name, executorId);
      option.textContent = state === "online" ? label : `${label} (${state})`;
      option.disabled = state !== "online";
      elements.dashboardExecutor.append(option);
      const row = document.createElement("button");
      row.type = "button";
      row.className = "executor-band dashboard-machine-row";
      row.setAttribute("aria-label", `Select ${label}`);
      row.dataset.executorId = executorId;
      row.setAttribute("aria-current", String(executorId === controllerState.executorId));
      row.disabled = state !== "online";
      const avatar = document.createElement("span");
      avatar.className = "executor-band-mark";
      avatar.textContent = label.slice(0, 1).toUpperCase();
      const details = document.createElement("span");
      details.className = "executor-band-main";
      const name = document.createElement("strong");
      name.textContent = label;
      const version = document.createElement("small");
      version.textContent = text(executor.runtime?.workgate_version, "Executor");
      details.append(name, version);
      const status = document.createElement("span");
      status.className = "executor-band-state";
      status.dataset.status = state;
      status.textContent = state;
      row.append(avatar, details, status);
      row.addEventListener("click", () => {
        if (controllerState.loading || executorId === controllerState.executorId) return;
        elements.dashboardExecutor.value = executorId;
        elements.dashboardExecutor.dispatchEvent(new Event("change", { bubbles: true }));
        for (const item of machineList.children) {
          item.setAttribute("aria-current", String(item.dataset.executorId === executorId));
        }
      });
      machineList.append(row);
    }
    const previousExecutorId = controllerState.executorId;
    if (!dashboardExecutorOnline() && !controllerState.executorPinned) {
      const firstOnline = available.find((item) => item.status === "online");
      if (controllerState.executorId !== (firstOnline?.executor_id || "")) {
        resetDashboardExecutor(firstOnline?.executor_id || "");
      }
    }
    if (controllerState.executorPinned && controllerState.executorId && !controllerState.executorStates.has(controllerState.executorId)) {
      const option = document.createElement("option");
      option.value = controllerState.executorId;
      option.textContent = `${controllerState.executorId} (unavailable)`;
      option.disabled = true;
      elements.dashboardExecutor.append(option);
    }
    elements.dashboardExecutor.value = controllerState.executorId;
    for (const item of machineList.children) {
      item.setAttribute("aria-current", String(item.dataset.executorId === controllerState.executorId));
    }
    if (!dashboardExecutorOnline() && wasOnline) resetDashboardExecutor(controllerState.executorId);
    if (dashboardExecutorOnline() && (!wasOnline || previousExecutorId !== controllerState.executorId)) {
      refreshDashboardInBackground({ force: true });
    }
    if (!dashboardExecutorOnline() && controllerState.executorId) {
      elements.dashboardState.textContent = "Executor offline";
    }
    setDashboardControls();
  }

  function renderTasks(tasks) {
    const list = document.getElementById("dashboard-task-list");
    list.replaceChildren();
    const active = (Array.isArray(tasks) ? tasks : []).filter(
      (task) => task.status !== "completed" && task.status !== "cancelled",
    );
    if (!active.length) {
      dashboardEmpty(list, "No active tasks.");
      return;
    }
    for (const task of active.slice(0, 4)) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "dashboard-task-row";
      row.dataset.taskId = task.task_id;
      const title = document.createElement("strong");
      title.textContent = text(task.objective, text(task.task_id));
      const state = document.createElement("span");
      state.className = "dashboard-task-state";
      state.textContent = text(task.status);
      row.append(title, state);
      row.addEventListener("click", () => onOpenTask(task.task_id));
      list.append(row);
    }
  }

  function dashboardListItem(kind, titleText, detailText, metaText = "") {
    const article = document.createElement("article");
    article.className = `dashboard-list-item dashboard-list-${kind}`;
    const icon = document.createElement("span");
    icon.className = "dashboard-event-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = ["failed", "critical", "warning", "attention"].includes(kind) ? "!" : "✓";
    const header = document.createElement("div");
    header.className = "dashboard-list-header";
    const title = document.createElement("strong");
    title.textContent = titleText;
    const meta = document.createElement("span");
    meta.textContent = metaText;
    header.append(title, meta);
    const detail = document.createElement("p");
    detail.textContent = detailText;
    article.append(icon, header, detail);
    return article;
  }

  function renderDashboard(payload) {
    const system = payload && payload.system && typeof payload.system === "object" ? payload.system : {};
    const alerts = Array.isArray(payload?.alerts) ? payload.alerts : [];
    controllerState.history.push({
      cpu: dashboardNumber(system.cpu_percent),
      memory: dashboardNumber(system.memory_percent),
      disk: dashboardNumber(system.disk_percent),
    });
    controllerState.history = controllerState.history.slice(-60);

    const health = ["healthy", "attention", "critical"].includes(payload.health)
      ? payload.health
      : "attention";
    elements.dashboardHealth.textContent = {
      healthy: "All systems operational",
      attention: "Attention needed",
      critical: "Critical issue detected",
    }[health];
    elements.dashboardHealthCard.className = `dashboard-card dashboard-health-card dashboard-health-${health}`;
    document.querySelector(".dashboard-health-banner").dataset.health = health;
    elements.dashboardHealthDetail.textContent = alerts.length
      ? `${alerts.length} active alert${alerts.length === 1 ? "" : "s"}.`
      : "No active alerts.";
    controllerState.lastSystem = system;
    renderResources(system, controllerState.history, "Live");
    renderSystemDetails(payload);
    elements.dashboardAuditTotal.textContent = text(payload.audit_total_24h, "0");
    elements.dashboardAuditDetail.textContent = `${text(payload.audit_failed_24h, "0")} failed in 24h`;
    elements.dashboardAlerts.replaceChildren();
    if (!alerts.length) {
      dashboardEmpty(elements.dashboardAlerts, "No active alerts.");
    } else {
      for (const alert of alerts) {
        elements.dashboardAlerts.append(
          dashboardListItem(
            text(alert.severity, "info"),
            text(alert.title, "Dashboard alert"),
            text(alert.detail, ""),
            text(alert.node, controllerState.executorId),
          ),
        );
      }
    }

    elements.dashboardState.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  function dashboardQueryPath() {
    const params = new URLSearchParams({ executor_id: controllerState.executorId });
    return `/dashboard?${params.toString()}`;
  }

  async function refreshDashboard({ force = false } = {}) {
    if ((controllerState.loading && !force) || !dashboardExecutorOnline()) return null;
    const generation = ++controllerState.generation;
    const requestedExecutor = controllerState.executorId;
    controllerState.loading = true;
    setDashboardControls();
    elements.dashboardState.textContent = "Loading…";
    try {
      const payload = await request(dashboardQueryPath());
      if (generation !== controllerState.generation || requestedExecutor !== controllerState.executorId) return null;
      renderDashboard(payload);
      return payload;
    } catch (error) {
      if (error.authenticationRequired) throw error;
      if (generation !== controllerState.generation || requestedExecutor !== controllerState.executorId) return null;
      elements.dashboardState.textContent = error instanceof Error ? error.message : String(error);
      elements.dashboardHealth.textContent = "unavailable";
      renderResources(controllerState.lastSystem, controllerState.history, "Unavailable");
      elements.dashboardHealthCard.className = "dashboard-card dashboard-health-card dashboard-health-attention";
      elements.dashboardHealthDetail.textContent = `Telemetry for ${requestedExecutor} could not be loaded`;
      return null;
    } finally {
      if (generation === controllerState.generation) {
        controllerState.loading = false;
        setDashboardControls();
      }
    }
  }

  function refreshDashboardInBackground(options = {}) {
    void refreshDashboard(options).catch((error) => {
      if (error.authenticationRequired) onAuthenticationRequired();
    });
  }


  function invalidate() {
    controllerState.generation += 1;
    controllerState.loading = false;
    setDashboardControls();
  }

  function bind() {
    elements.dashboardExecutor.addEventListener("change", () => {
      if (controllerState.loading) return;
      controllerState.executorPinned = true;
      resetDashboardExecutor(elements.dashboardExecutor.value);
      refreshDashboardInBackground({ force: true });
    });
  }

  return {
    bind,
    renderTasks,
    invalidate,
    refresh: refreshDashboard,
    renderExecutors: renderDashboardExecutors,
    reset: resetDashboardExecutor,
    startPolling: startDashboardPolling,
    stopPolling: stopDashboardPolling,
  };
}
