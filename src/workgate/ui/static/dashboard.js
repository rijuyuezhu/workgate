export function createDashboardController({
  elements,
  request,
  text,
  authMode,
  isAuthenticated,
  onAuthenticationRequired,
  onOpenTask,
  initialExecutorId = "",
}) {
  const controllerState = {
    executorId: initialExecutorId,
    executorPinned: Boolean(initialExecutorId),
    generation: 0,
    loading: false,
    executorStates: new Map(),
    history: [],
    timer: null,
  };

  function dashboardExecutorOnline(executorId = controllerState.executorId) {
    return Boolean(executorId) && controllerState.executorStates.get(executorId) === "online";
  }

  function dashboardNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function dashboardPercent(value) {
    const number = dashboardNumber(value);
    return number === null ? "—" : `${number.toFixed(1)}%`;
  }

  function dashboardBytes(value) {
    const number = dashboardNumber(value);
    if (number === null || number < 0) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let scaled = number;
    let unit = 0;
    while (scaled >= 1024 && unit < units.length - 1) {
      scaled /= 1024;
      unit += 1;
    }
    const digits = scaled >= 100 || unit === 0 ? 0 : scaled >= 10 ? 1 : 2;
    return `${scaled.toFixed(digits)} ${units[unit]}`;
  }

  function dashboardRate(value) {
    const formatted = dashboardBytes(value);
    return formatted === "—" ? formatted : `${formatted}/s`;
  }

  function dashboardDuration(value) {
    let seconds = dashboardNumber(value);
    if (seconds === null || seconds < 0) return "—";
    seconds = Math.floor(seconds);
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m ${seconds % 60}s`;
  }

  function dashboardTimestamp(value, fallback = "Unknown time") {
    const seconds = dashboardNumber(value);
    if (seconds === null || seconds <= 0) return fallback;
    const date = new Date(seconds * 1000);
    return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
  }

  function setDashboardControls() {
    const online = dashboardExecutorOnline();
    elements.dashboardExecutor.disabled = controllerState.loading;
  }

  function setMetricLive(label) {
    for (const badge of document.querySelectorAll(".dashboard-live")) badge.textContent = label;
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
      if ((authMode !== "oauth" || isAuthenticated()) && dashboardExecutorOnline()) {
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

  function renderDashboardSparkline(element, rawValues, fixedMaximum = null) {
    const values = Array.isArray(rawValues) ? rawValues : [];
    element.replaceChildren();
    const finite = values
      .map((value, index) => ({ index, value: dashboardNumber(value) }))
      .filter((item) => item.value !== null);
    if (finite.length < 2) return;
    const normalizedMaximum = dashboardNumber(fixedMaximum);
    const maximum = normalizedMaximum !== null
      ? Math.max(1, normalizedMaximum)
      : Math.max(1, ...finite.map((item) => item.value));
    const lastIndex = Math.max(1, values.length - 1);
    const points = finite
      .map((item) => {
        const x = (item.index / lastIndex) * 100;
        const y = 30 - Math.max(0, Math.min(1, item.value / maximum)) * 26;
        return `${x.toFixed(2)},${y.toFixed(2)}`;
      })
      .join(" ");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", points);
    line.setAttribute("fill", "none");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    element.append(line);
  }

  function setDashboardBar(element, value) {
    const number = dashboardNumber(value);
    const width = number === null ? 0 : Math.max(0, Math.min(100, number));
    element.style.width = `${width}%`;
    element.parentElement?.classList.toggle("dashboard-resource-missing", number === null);
  }

  function resetDashboardExecutor(executorId = "") {
    controllerState.executorId = executorId;
    controllerState.loading = false;
    controllerState.generation += 1;
    controllerState.history = [];
    elements.dashboardExecutor.value = controllerState.executorId;
    elements.dashboardState.textContent = "Not loaded";
    elements.dashboardHealth.textContent = "—";
    elements.dashboardHealthDetail.textContent = "Waiting for telemetry";
    elements.dashboardHealthCard.className = "dashboard-card dashboard-health-card";
    document.querySelector(".dashboard-health-banner").dataset.health = "unknown";
    setMetricLive("—");
    for (const element of [
      elements.dashboardCpu,
      elements.dashboardMemory,
      elements.dashboardDisk,
      elements.dashboardNetwork,
      elements.dashboardAuditTotal,
      elements.dashboardCpuValue,
      elements.dashboardMemoryValue,
      elements.dashboardDiskValue,
      elements.dashboardPlatform,
      elements.dashboardPython,
      elements.dashboardCpuCount,
      elements.dashboardLoad,
      elements.dashboardUptime,
      elements.dashboardMemoryUsed,
      elements.dashboardDiskUsed,
      elements.dashboardNetworkRx,
      elements.dashboardNetworkTx,
      elements.dashboardGenerated,
      elements.dashboardSourceState,
    ]) element.textContent = "—";
    elements.dashboardAuditDetail.textContent = "No activity loaded";
    for (const id of [
      "dashboard-cpu-foot-load", "dashboard-cpu-foot-cores",
      "dashboard-memory-foot-used", "dashboard-memory-foot-total",
      "dashboard-disk-foot-used", "dashboard-disk-foot-total",
      "dashboard-network-down", "dashboard-network-up",
    ]) document.getElementById(id).textContent = "—";
    document.getElementById("dashboard-network-rx-bar").style.width = "0%";
    document.getElementById("dashboard-network-tx-bar").style.width = "0%";
    elements.dashboardAlertCount.textContent = "0";
    setDashboardBar(elements.dashboardCpuBar, null);
    setDashboardBar(elements.dashboardMemoryBar, null);
    setDashboardBar(elements.dashboardDiskBar, null);
    for (const trend of [
      elements.dashboardCpuTrend,
      elements.dashboardMemoryTrend,
      elements.dashboardDiskTrend,
      elements.dashboardNetworkTrend,
    ]) trend.replaceChildren();
    dashboardEmpty(elements.dashboardAlerts, `Alerts for ${controllerState.executorId} are not loaded.`);
    dashboardEmpty(elements.dashboardActivity, `Activity for ${controllerState.executorId} is not loaded.`);
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
      row.className = "dashboard-machine-row";
      row.setAttribute("aria-label", `Select ${label}`);
      row.dataset.executorId = executorId;
      row.setAttribute("aria-current", String(executorId === controllerState.executorId));
      row.disabled = state !== "online";
      const avatar = document.createElement("span");
      avatar.className = "dashboard-machine-avatar";
      avatar.textContent = label.slice(0, 1).toUpperCase();
      const details = document.createElement("span");
      details.className = "dashboard-machine-details";
      const name = document.createElement("strong");
      name.textContent = label;
      const version = document.createElement("small");
      version.textContent = text(executor.runtime?.workgate_version, "Executor");
      details.append(name, version);
      const status = document.createElement("span");
      status.className = `dashboard-machine-status ${state === "online" ? "is-online" : ""}`;
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
    const version = payload && payload.version && typeof payload.version === "object" ? payload.version : {};
    const alerts = Array.isArray(payload?.alerts) ? payload.alerts : [];
    const activity = Array.isArray(payload?.activity) ? payload.activity : [];
    const sources = payload && payload.sources && typeof payload.sources === "object" ? payload.sources : {};
    const networkRx = dashboardNumber(system.network_rx_bps);
    const networkTx = dashboardNumber(system.network_tx_bps);
    const networkTotal = networkRx !== null && networkTx !== null
      ? networkRx + networkTx
      : null;
    controllerState.history.push({
      cpu: dashboardNumber(system.cpu_percent),
      memory: dashboardNumber(system.memory_percent),
      disk: dashboardNumber(system.disk_percent),
      network: networkTotal,
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
    setMetricLive("Live");
    elements.dashboardHealthDetail.textContent = alerts.length
      ? `${alerts.length} active alert${alerts.length === 1 ? "" : "s"}.`
      : "No active alerts.";
    elements.dashboardCpu.textContent = dashboardPercent(system.cpu_percent);
    elements.dashboardMemory.textContent = dashboardPercent(system.memory_percent);
    elements.dashboardDisk.textContent = dashboardPercent(system.disk_percent);
    elements.dashboardNetwork.textContent = networkTotal === null ? "—" : dashboardRate(networkTotal);
    document.getElementById("dashboard-network-down").textContent = dashboardBytes(networkRx);
    document.getElementById("dashboard-network-up").textContent = dashboardBytes(networkTx);
    const total = Math.max(0, networkRx || 0) + Math.max(0, networkTx || 0);
    document.getElementById("dashboard-network-rx-bar").style.width = total > 0 ? `${100 * Math.max(0, networkRx || 0) / total}%` : "0%";
    document.getElementById("dashboard-network-tx-bar").style.width = total > 0 ? `${100 * Math.max(0, networkTx || 0) / total}%` : "0%";
    const load = dashboardNumber(system.load_1m);
    document.getElementById("dashboard-cpu-foot-load").textContent = load === null ? "Load —" : `Load ${load.toFixed(2)}`;
    document.getElementById("dashboard-cpu-foot-cores").textContent = `${text(system.cpu_count)} cores`;
    document.getElementById("dashboard-memory-foot-used").textContent = `${dashboardBytes(system.memory_used_bytes)} used`;
    document.getElementById("dashboard-memory-foot-total").textContent = `${dashboardBytes(system.memory_total_bytes)} total`;
    document.getElementById("dashboard-disk-foot-used").textContent = `${dashboardBytes(system.disk_used_bytes)} used`;
    document.getElementById("dashboard-disk-foot-total").textContent = `${dashboardBytes(system.disk_total_bytes)} total`;
    elements.dashboardAuditTotal.textContent = text(payload.audit_total_24h, "0");
    elements.dashboardAuditDetail.textContent = `${text(payload.audit_failed_24h, "0")} failed · last 24h`;
    elements.dashboardGenerated.textContent = dashboardTimestamp(payload.generated_at, "Unknown sample time");

    elements.dashboardCpuValue.textContent = dashboardPercent(system.cpu_percent);
    elements.dashboardMemoryValue.textContent = dashboardPercent(system.memory_percent);
    elements.dashboardDiskValue.textContent = dashboardPercent(system.disk_percent);
    setDashboardBar(elements.dashboardCpuBar, system.cpu_percent);
    setDashboardBar(elements.dashboardMemoryBar, system.memory_percent);
    setDashboardBar(elements.dashboardDiskBar, system.disk_percent);
    renderDashboardSparkline(
      elements.dashboardCpuTrend,
      controllerState.history.map((sample) => sample.cpu),
      100,
    );
    renderDashboardSparkline(
      elements.dashboardMemoryTrend,
      controllerState.history.map((sample) => sample.memory),
      100,
    );
    renderDashboardSparkline(
      elements.dashboardDiskTrend,
      controllerState.history.map((sample) => sample.disk),
      100,
    );
    renderDashboardSparkline(
      elements.dashboardNetworkTrend,
      controllerState.history.map((sample) => sample.network),
    );

    elements.dashboardPlatform.textContent = text(version.platform);
    elements.dashboardPython.textContent = text(version.python);
    elements.dashboardCpuCount.textContent = text(system.cpu_count);
    elements.dashboardLoad.textContent = load === null ? "—" : load.toFixed(2);
    elements.dashboardUptime.textContent = dashboardDuration(system.uptime_s);
    elements.dashboardMemoryUsed.textContent = system.memory_total_bytes === null
      ? "—"
      : `${dashboardBytes(system.memory_used_bytes)} / ${dashboardBytes(system.memory_total_bytes)}`;
    elements.dashboardDiskUsed.textContent = system.disk_total_bytes === null
      ? "—"
      : `${dashboardBytes(system.disk_used_bytes)} / ${dashboardBytes(system.disk_total_bytes)}`;
    elements.dashboardNetworkRx.textContent = dashboardRate(system.network_rx_bps);
    elements.dashboardNetworkTx.textContent = dashboardRate(system.network_tx_bps);

    elements.dashboardAlertCount.textContent = String(alerts.length);
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

    elements.dashboardActivity.replaceChildren();
    if (!activity.length) {
      dashboardEmpty(elements.dashboardActivity, "No recent Audit activity.");
    } else {
      for (const item of activity) {
        const durationMs = dashboardNumber(item.duration_ms);
        const duration = durationMs === null ? "" : `${durationMs.toFixed(0)} ms`;
        elements.dashboardActivity.append(
          dashboardListItem(
            text(item.kind, "success"),
            text(item.title, "MCP activity"),
            text(item.detail, ""),
            `${dashboardTimestamp(item.timestamp)}${duration ? ` · ${duration}` : ""}`,
          ),
        );
      }
    }
    elements.dashboardSourceState.textContent = `system ${text(sources.system, "unknown")} · audit ${text(sources.audit, "unknown")}`;
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
      setMetricLive("Unavailable");
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
