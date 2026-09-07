export function createExecutorsController({
  elements,
  request,
  authMode,
  isAuthenticated,
  reloadApp,
}) {
  const state = {
    executors: [],
    selectedId: "",
    loading: false,
    generation: 0,
    timer: null,
    pairing: null,
  };

  function selectedExecutor() {
    return state.executors.find((item) => item.executor_id === state.selectedId) || null;
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
    elements.executorRefresh.disabled = state.loading;
    elements.executorPairOpen.disabled = state.loading;
    const selected = selectedExecutor();
    const mutable = selected && !selected.revoked_at;
    elements.executorRenameOpen.disabled = state.loading || !mutable;
    elements.executorRevokeOpen.disabled = state.loading || !mutable;
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
      button.className = "remote-row";
      const selected = executor.executor_id === state.selectedId;
      if (selected) button.classList.add("remote-row-selected");
      button.setAttribute("aria-pressed", selected ? "true" : "false");

      const indicator = document.createElement("span");
      const live = !executor.revoked_at && executor.online === true;
      indicator.className = live
        ? "remote-row-status remote-row-status-online"
        : "remote-row-status";
      indicator.setAttribute(
        "aria-label",
        executor.revoked_at ? "revoked" : live ? "online" : "offline",
      );

      const main = document.createElement("span");
      main.className = "remote-row-main";
      const name = document.createElement("strong");
      name.className = "remote-row-name";
      name.textContent = executor.name || executor.executor_id;
      const meta = document.createElement("span");
      meta.className = "remote-row-meta";
      meta.textContent = executor.revoked_at
        ? "revoked"
        : executor.online
          ? "online"
          : "offline";
      main.append(name, meta);

      const version = document.createElement("span");
      version.className = "remote-row-version";
      version.textContent = executor.runtime?.workgate_version || "version —";
      button.append(indicator, main, version);
      button.addEventListener("click", () => {
        state.selectedId = executor.executor_id;
        renderList();
        renderDetails();
      });
      elements.executorList.append(button);
    }
  }

  function renderDetails() {
    const executor = selectedExecutor();
    if (!executor) {
      elements.executorDetailName.textContent = "No executor selected";
      elements.executorDetailStatus.textContent = "Select an executor to inspect it";
      elements.executorDetailId.textContent = "—";
      elements.executorDetailVersion.textContent = "—";
      elements.executorDetailPlatform.textContent = "—";
      elements.executorDetailCreated.textContent = "—";
      elements.executorDetailLastSeen.textContent = "—";
      setControls();
      return;
    }
    const status = executor.revoked_at
      ? "revoked"
      : executor.online
        ? "online"
        : "offline";
    elements.executorDetailName.textContent = executor.name || executor.executor_id;
    elements.executorDetailStatus.textContent = status;
    elements.executorDetailId.textContent = executor.executor_id;
    elements.executorDetailVersion.textContent = executor.runtime?.workgate_version || "Not reported";
    elements.executorDetailPlatform.textContent = executor.runtime?.platform || "Not reported";
    elements.executorDetailCreated.textContent = timestamp(executor.created_at, "Unknown");
    elements.executorDetailLastSeen.textContent = timestamp(executor.last_seen_at);
    setControls();
  }

  function render(payload) {
    state.executors = Array.isArray(payload?.executors) ? payload.executors : [];
    if (!state.executors.some((item) => item.executor_id === state.selectedId)) {
      state.selectedId = state.executors[0]?.executor_id || "";
    }
    const online = state.executors.filter((item) => !item.revoked_at && item.online).length;
    const revoked = state.executors.filter((item) => item.revoked_at).length;
    elements.executorOnline.textContent = String(online);
    elements.executorTotal.textContent = String(state.executors.length);
    elements.executorRevoked.textContent = String(revoked);
    elements.executorState.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    renderList();
    renderDetails();
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
    renderList();
    renderDetails();
  }

  function bind() {
    elements.executorRefresh.addEventListener("click", () => refreshInBackground({ force: true }));
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
    refresh,
    reset,
    startPolling,
    stopPolling,
  };
}
