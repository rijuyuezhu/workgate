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
  renderResources,
  renderOverviewExecutors,
  renderOverviewSelection,
  renderOverviewAlerts,
  renderOverviewTasks,
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
    elements.dashboardExecutor.disabled = controllerState.loading;
    renderOverviewSelection(controllerState.executorId, controllerState.loading);
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
    renderOverviewAlerts([], `Alerts for ${controllerState.executorId} are not loaded.`);
    setDashboardControls();
  }

  function renderDashboardExecutors(targets) {
    const available = Array.isArray(targets) ? targets : [];
    const wasOnline = dashboardExecutorOnline();
    controllerState.executorStates = new Map();
    elements.dashboardExecutor.replaceChildren();
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
    if (!dashboardExecutorOnline() && wasOnline) resetDashboardExecutor(controllerState.executorId);
    if (dashboardExecutorOnline() && (!wasOnline || previousExecutorId !== controllerState.executorId)) {
      refreshDashboardInBackground({ force: true });
    }
    if (!dashboardExecutorOnline() && controllerState.executorId) {
      elements.dashboardState.textContent = "Executor offline";
    }
    setDashboardControls();
    renderOverviewExecutors(available, controllerState.executorId, controllerState.loading);
  }

  function renderTasks(tasks) {
    renderOverviewTasks(tasks);
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
    renderOverviewAlerts(alerts, "No active alerts.");

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
