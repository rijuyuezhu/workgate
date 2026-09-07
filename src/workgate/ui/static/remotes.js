export function createRemotesController({
  elements,
  request,
  text,
  authMode,
  isAuthenticated,
  reloadApp,
}) {
  const controllerState = {
    machines: [],
    selectedName: "",
    enabled: false,
    generation: 0,
    loading: false,
    timer: null,
  };

  function selectedRemote() {
    return controllerState.machines.find((machine) => machine.name === controllerState.selectedName) || null;
  }

  function remoteTimestamp(value, fallback = "Never") {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) return fallback;
    const date = new Date(seconds * 1000);
    return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
  }

  function remoteAge(value) {
    if (value === null || value === undefined || value === "") return "unknown age";
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "unknown age";
    if (seconds < 5) return "just now";
    if (seconds < 60) return `${Math.floor(seconds)}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  function closeRemoteDialog(dialog) {
    if (dialog?.open) dialog.close();
  }

  function setRemoteControls() {
    elements.remoteRefresh.disabled = controllerState.loading;
    const selected = selectedRemote();
    elements.remoteRenameOpen.disabled = controllerState.loading || !controllerState.enabled || !selected;
    elements.remoteRevokeOpen.disabled = controllerState.loading || !controllerState.enabled || !selected;
    elements.remoteReconnectCopy.disabled = controllerState.loading || !text(selected?.reconnect_command, "");
  }

  function resetRemotes(message = "Not loaded") {
    controllerState.generation += 1;
    controllerState.loading = false;
    controllerState.machines = [];
    controllerState.selectedName = "";
    controllerState.enabled = false;
    closeRemoteDialog(elements.remoteRenameDialog);
    closeRemoteDialog(elements.remoteRevokeDialog);
    elements.remoteRenameForm.reset();
    elements.remoteState.textContent = message;
    elements.remoteOnline.textContent = "—";
    elements.remoteOffline.textContent = "—";
    elements.remoteTotal.textContent = "—";
    elements.remoteController.textContent = "—";
    renderRemoteList();
    renderRemoteDetails();
    setRemoteControls();
  }

  function renderRemoteDetails() {
    const machine = selectedRemote();
    const info = machine && machine.info && typeof machine.info === "object" ? machine.info : {};
    const values = [
      elements.remoteDetailVersion,
      elements.remoteDetailLastSeen,
      elements.remoteDetailWorkdir,
      elements.remoteDetailQueue,
      elements.remoteDetailHostname,
      elements.remoteDetailUser,
      elements.remoteDetailPlatform,
      elements.remoteDetailPython,
      elements.remoteDetailProfile,
    ];
    if (!machine) {
      elements.remoteDetailName.textContent = "No remote selected";
      elements.remoteDetailStatus.textContent = controllerState.enabled
        ? "Select a worker to inspect it"
        : "Remote workers are disabled";
      for (const element of values) element.textContent = "—";
      elements.remoteDetailReconnect.textContent = "Unavailable for this worker";
      elements.remoteReconnectCopy.textContent = "Copy reconnect";
      elements.remoteDetailCapabilities.replaceChildren();
      const empty = document.createElement("span");
      empty.className = "muted";
      empty.textContent = "—";
      elements.remoteDetailCapabilities.append(empty);
      setRemoteControls();
      return;
    }

    const online = machine.status === "online";
    elements.remoteDetailName.textContent = text(machine.name, "unnamed");
    elements.remoteDetailStatus.textContent = `${online ? "online" : "offline"} · ${remoteAge(machine.last_seen_age_s)}`;
    elements.remoteDetailVersion.textContent = text(info.workgate_version, "Not reported");
    elements.remoteDetailLastSeen.textContent = remoteTimestamp(machine.last_seen);
    elements.remoteDetailWorkdir.textContent = text(machine.workdir, text(info.workdir, "Not reported"));
    elements.remoteDetailQueue.textContent = text(machine.queue_depth, "0");
    elements.remoteDetailHostname.textContent = text(info.hostname, "Not reported");
    elements.remoteDetailUser.textContent = text(info.user, "Not reported");
    elements.remoteDetailPlatform.textContent = text(info.platform, "Not reported");
    elements.remoteDetailPython.textContent = text(info.python, "Not reported");
    elements.remoteDetailProfile.textContent = text(machine.profile_id, "Legacy worker");
    elements.remoteDetailReconnect.textContent = text(
      machine.reconnect_command,
      "Unavailable for this worker",
    );
    elements.remoteReconnectCopy.textContent = "Copy reconnect";
    elements.remoteDetailCapabilities.replaceChildren();
    const capabilities = Array.isArray(machine.capabilities) ? machine.capabilities : [];
    if (!capabilities.length) {
      const empty = document.createElement("span");
      empty.className = "muted";
      empty.textContent = "Not reported";
      elements.remoteDetailCapabilities.append(empty);
    } else {
      for (const capability of capabilities) {
        const chip = document.createElement("span");
        chip.className = "remote-capability";
        chip.textContent = text(capability, "unknown");
        elements.remoteDetailCapabilities.append(chip);
      }
    }
    setRemoteControls();
  }

  function renderRemoteList() {
    elements.remoteList.replaceChildren();
    if (!controllerState.enabled) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "Remote workers are disabled.";
      elements.remoteList.append(empty);
      return;
    }
    if (!controllerState.machines.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No remote workers are registered.";
      elements.remoteList.append(empty);
      return;
    }
    for (const machine of controllerState.machines) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "remote-row";
      if (machine.name === controllerState.selectedName) button.classList.add("remote-row-selected");
      button.setAttribute("aria-pressed", machine.name === controllerState.selectedName ? "true" : "false");

      const indicator = document.createElement("span");
      indicator.className = machine.status === "online"
        ? "remote-row-status remote-row-status-online"
        : "remote-row-status";
      indicator.setAttribute("aria-label", machine.status === "online" ? "online" : "offline");

      const main = document.createElement("span");
      main.className = "remote-row-main";
      const name = document.createElement("strong");
      name.className = "remote-row-name";
      name.textContent = text(machine.name, "unnamed");
      const meta = document.createElement("span");
      meta.className = "remote-row-meta";
      meta.textContent = `${machine.status === "online" ? "online" : "offline"} · ${remoteAge(machine.last_seen_age_s)} · queue ${text(machine.queue_depth, "0")}`;
      main.append(name, meta);

      const version = document.createElement("span");
      version.className = "remote-row-version";
      version.textContent = text(machine.info?.workgate_version, "version —");
      button.append(indicator, main, version);
      button.addEventListener("click", () => {
        controllerState.selectedName = machine.name;
        renderRemoteList();
        renderRemoteDetails();
      });
      elements.remoteList.append(button);
    }
  }

  function renderRemotes(payload) {
    controllerState.enabled = payload?.enabled === true;
    controllerState.machines = Array.isArray(payload?.machines) ? payload.machines : [];
    if (!controllerState.machines.some((machine) => machine.name === controllerState.selectedName)) {
      controllerState.selectedName = controllerState.machines[0]?.name || "";
    }
    const counts = payload && payload.counts && typeof payload.counts === "object" ? payload.counts : {};
    elements.remoteOnline.textContent = text(counts.online, "0");
    elements.remoteOffline.textContent = text(counts.offline, "0");
    elements.remoteTotal.textContent = text(counts.total, controllerState.machines.length);
    elements.remoteController.textContent = controllerState.enabled ? "enabled" : "disabled";
    elements.remoteState.textContent = controllerState.enabled
      ? `Updated ${new Date().toLocaleTimeString()}`
      : "Remote workers disabled";
    renderRemoteList();
    renderRemoteDetails();
  }

  async function refreshRemotes({ force = false } = {}) {
    if (controllerState.loading && !force) return null;
    const generation = ++controllerState.generation;
    controllerState.loading = true;
    setRemoteControls();
    elements.remoteState.textContent = "Loading remote workers";
    try {
      const payload = await request("/remotes");
      if (generation !== controllerState.generation) return null;
      renderRemotes(payload);
      return payload;
    } catch (error) {
      if (error.authenticationRequired) throw error;
      if (generation !== controllerState.generation) return null;
      elements.remoteState.textContent = error instanceof Error ? error.message : String(error);
      return null;
    } finally {
      if (generation === controllerState.generation) {
        controllerState.loading = false;
        setRemoteControls();
      }
    }
  }

  function refreshRemotesInBackground(options = {}) {
    void refreshRemotes(options).catch((error) => {
      if (error.authenticationRequired) void reloadApp();
    });
  }

  function stopRemotePolling() {
    if (controllerState.timer !== null) {
      globalThis.clearInterval(controllerState.timer);
      controllerState.timer = null;
    }
  }

  function startRemotePolling() {
    stopRemotePolling();
    controllerState.timer = globalThis.setInterval(() => {
      if (authMode !== "oauth" || isAuthenticated()) refreshRemotesInBackground();
    }, 4000);
  }

  async function remoteAction(path, body) {
    return request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }


  function bind() {
    elements.remoteRefresh.addEventListener("click", () => refreshRemotesInBackground({ force: true }));
    elements.remoteReconnectCopy.addEventListener("click", async () => {
      const command = text(selectedRemote()?.reconnect_command, "");
      if (!command) return;
      try {
        if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
          throw new Error("Clipboard API is unavailable");
        }
        await navigator.clipboard.writeText(command);
        elements.remoteReconnectCopy.textContent = "Copied";
      } catch (error) {
        elements.remoteReconnectCopy.textContent = "Copy unavailable";
        elements.remoteState.textContent = error instanceof Error ? error.message : String(error);
      }
    });
    elements.remoteRenameOpen.addEventListener("click", () => {
      const machine = selectedRemote();
      if (!machine || elements.remoteRenameDialog.open) return;
      elements.remoteRenameName.value = machine.name;
      elements.remoteRenameDialog.showModal();
      elements.remoteRenameName.select();
    });
    elements.remoteRevokeOpen.addEventListener("click", () => {
      const machine = selectedRemote();
      if (!machine || elements.remoteRevokeDialog.open) return;
      elements.remoteRevokeName.textContent = machine.name;
      elements.remoteRevokeDialog.showModal();
    });
    for (const button of document.querySelectorAll("[data-close-dialog]")) {
      button.addEventListener("click", () => {
        const dialog = document.getElementById(button.dataset.closeDialog || "");
        closeRemoteDialog(dialog);
      });
    }
    elements.remoteRenameForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const machine = selectedRemote();
      if (!machine) return;
      const newName = elements.remoteRenameName.value.trim();
      const button = elements.remoteRenameForm.querySelector('button[type="submit"]');
      button.disabled = true;
      elements.remoteState.textContent = `Renaming ${machine.name}`;
      try {
        const result = await remoteAction("/remotes/rename", {
          machine: machine.name,
          new_name: newName,
        });
        controllerState.selectedName = text(result?.new_name, newName);
        closeRemoteDialog(elements.remoteRenameDialog);
        await reloadApp();
      } catch (error) {
        if (error.authenticationRequired) {
          void reloadApp();
          return;
        }
        elements.remoteState.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        button.disabled = false;
      }
    });
    elements.remoteRevokeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const machine = selectedRemote();
      if (!machine) return;
      const button = elements.remoteRevokeForm.querySelector('button[type="submit"]');
      button.disabled = true;
      elements.remoteState.textContent = `Revoking ${machine.name}`;
      try {
        await remoteAction("/remotes/revoke", { machine: machine.name });
        controllerState.selectedName = "";
        closeRemoteDialog(elements.remoteRevokeDialog);
        await reloadApp();
      } catch (error) {
        if (error.authenticationRequired) {
          void reloadApp();
          return;
        }
        elements.remoteState.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        button.disabled = false;
      }
    });

  }

  return {
    bind,
    refresh: refreshRemotes,
    reset: resetRemotes,
    startPolling: startRemotePolling,
    stopPolling: stopRemotePolling,
  };
}
