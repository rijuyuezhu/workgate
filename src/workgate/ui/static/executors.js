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
  };

  function selectedExecutor() {
    return state.executors.find((item) => item.executor_id === state.selectedId) || null;
  }

  function select(executorId) {
    if (!state.executors.some((item) => item.executor_id === executorId)) return;
    state.selectedId = executorId;
    renderList();
    renderDetails();
    void loadDiagnostics();
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
        renderList();
        renderDetails();
        void loadDiagnostics();
        onNavigate("detail");
      });
      elements.executorList.append(button);
    }
  }

  function renderDiagnostics() {
    const body = elements.executorDiagnosticsBody;
    body.replaceChildren();
    elements.executorDiagnosticsRefresh.disabled = !selectedExecutor() || state.diagnosticLoading;
    if (!selectedExecutor()) {
      elements.executorDiagnosticsState.textContent = "Select an executor to inspect its diagnostics.";
      return;
    }
    if (state.diagnosticId !== state.selectedId) {
      elements.executorDiagnosticsState.textContent = "Diagnostics have not been requested.";
      return;
    }
    if (state.diagnosticLoading) {
      elements.executorDiagnosticsState.textContent = "Loading read-only diagnostics…";
      return;
    }
    if (state.diagnosticError) {
      elements.executorDiagnosticsState.textContent = state.diagnosticError;
      return;
    }
    const data = state.diagnostic;
    if (!data) return;
    elements.executorDiagnosticsState.textContent =
      data.status === "available" ? "On-demand snapshot · no MCP server probes" :
      data.status === "offline" ? "Executor offline · local diagnostics unavailable" :
      "Executor diagnostic RPC unavailable · retry when connected";

    function group(title, lines) {
      const section = document.createElement("section");
      section.className = "executor-diagnostics-group";
      const heading = document.createElement("h4");
      heading.textContent = title;
      section.append(heading);
      for (const line of lines) {
        const p = document.createElement("p");
        p.textContent = line;
        section.append(p);
      }
      body.append(section);
    }
    function serverLines(snapshot) {
      if (!snapshot) return ["Unavailable"];
      const explanations = {
        missing_config: "No manifest found on this host",
        invalid_config: "Manifest validation failed; inspect local logs (details withheld)",
        unavailable: "Manifest cannot be read on this host",
      };
      const rows = (snapshot.servers || []).map((server) =>
        `${server.name} · ${server.enabled ? "enabled" : "disabled"} (${server.type})`);
      return [
        `Manifest: ${snapshot.status || "unavailable"}${snapshot.enabled === false ? " · disabled by Control" : ""}`,
        ...(explanations[snapshot.status] ? [explanations[snapshot.status]] : []),
        ...(snapshot.errors ? [`Configuration errors: ${snapshot.errors} (details withheld)`] : []),
        ...(rows.length ? rows : ["No configured servers for this owner"]),
        ...(snapshot.truncated ? ["Additional servers omitted"] : []),
      ];
    }
    group("Control-owned network MCP", serverLines(data.control_mcp));
    if (!data.executor) {
      group("Executor Agent / Bridge / Skills", ["Unavailable until this Executor reconnects and supports diagnostics."]);
      return;
    }
    const local = data.executor;
    group("Agent runtime", ["Independent Agent runtime status: not reported", "Agent and Agent Bridge are separate concerns."]);
    group("Executor Agent Bridge", [
      `Manifest: ${local.bridge?.status || "unavailable"}`,
      ...(local.bridge?.errors ? [`Configuration errors: ${local.bridge.errors} (details withheld)`] : []),
    ]);
    group("Executor-local stdio MCP", serverLines(local.bridge));
    const skills = local.skills || {};
    const names = (skills.selected || []).map((skill) => `${skill.name} · ${skill.source}`);
    group("Executor Skill sources", [
      skills.enabled ? "Enabled" : "Disabled or unavailable",
      ...(skills.sources || []).map((source) => `${source.source}: ${source.available ? "present" : "missing"}`),
      ...(names.length ? names : ["No selected Executor/global Skills"]),
      `${skills.warnings || 0} source warnings · ${skills.duplicates || 0} duplicate overrides`,
      "Project Skills resolve per Session; Control-wide precedence follows in #354.",
    ]);
    const health = local.health || {};
    const metrics = ["cpu_percent", "memory_percent", "disk_percent"]
      .filter((key) => typeof health[key] === "number")
      .map((key) => `${key.replace("_percent", "").toUpperCase()}: ${health[key].toFixed(1)}%`);
    group("Resource health", metrics.length ? metrics : ["Resource metrics unavailable"]);
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
      if (authMode !== "oauth" || isAuthenticated()) refreshInBackground();
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
    elements.executorDiagnosticsRefresh.addEventListener("click", () => { void loadDiagnostics(true); });
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
