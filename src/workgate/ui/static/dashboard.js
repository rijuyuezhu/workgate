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
  healthView,
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
    healthView.reset();
    healthView.renderStatus("Not loaded");
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
      healthView.renderStatus("Executor offline");
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

    healthView.renderSnapshot(payload);
    controllerState.lastSystem = system;
    renderResources(system, controllerState.history, "Live");
    renderSystemDetails(payload);
    renderOverviewAlerts(alerts, "No active alerts.");

    healthView.renderStatus(`Updated ${new Date().toLocaleTimeString()}`);
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
    healthView.renderStatus("Loading…");
    try {
      const payload = await request(dashboardQueryPath());
      if (generation !== controllerState.generation || requestedExecutor !== controllerState.executorId) return null;
      renderDashboard(payload);
      return payload;
    } catch (error) {
      if (error.authenticationRequired) throw error;
      if (generation !== controllerState.generation || requestedExecutor !== controllerState.executorId) return null;
      healthView.renderStatus(error instanceof Error ? error.message : String(error));
      healthView.renderError(requestedExecutor);
      renderResources(controllerState.lastSystem, controllerState.history, "Unavailable");
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
