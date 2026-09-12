export function createAuditController({
  elements,
  request,
  text,
  auditEntryButton,
  auditEntryTitle,
  auditTimestamp,
  renderAuditDetailInto,
  renderAuditDetailMessage,
}) {
  const controllerState = {
    auditEntries: [],
    auditSelectedId: "",
    auditGeneration: 0,
    auditDetailGeneration: 0,
    auditLoading: false,
  };

  function setAuditControls() {
    elements.auditRefresh.disabled = controllerState.auditLoading;
    for (const control of elements.auditFilterForm.querySelectorAll("input, select")) {
      control.disabled = controllerState.auditLoading;
    }
  }

  function clearAuditDetail(message = "Select a Global Audit record.") {
    controllerState.auditDetailGeneration += 1;
    elements.auditDetailTitle.textContent = "No record selected";
    elements.auditDetailMeta.textContent = "Control Audit";
    renderAuditDetailMessage(elements.auditDetailBody, message);
  }

  function resetAuditWorkspace() {
    controllerState.auditLoading = false;
    controllerState.auditEntries = [];
    controllerState.auditSelectedId = "";
    controllerState.auditGeneration += 1;
    elements.auditList.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Global control Audit records are not loaded.";
    elements.auditList.append(empty);
    elements.auditSummary.textContent = "0 entries";
    elements.auditState.textContent = "Not loaded · control";
    clearAuditDetail();
    setAuditControls();
  }

  function renderAuditList() {
    elements.auditList.replaceChildren();
    if (!controllerState.auditEntries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No Global Audit records match.";
      elements.auditList.append(empty);
      clearAuditDetail("No matching Global Audit record is available.");
      return;
    }
    for (const entry of controllerState.auditEntries) {
      elements.auditList.append(
        auditEntryButton(entry, controllerState.auditSelectedId, () => {
          controllerState.auditSelectedId = text(entry.id, "");
          renderAuditList();
          void loadAuditDetail(controllerState.auditSelectedId);
        }),
      );
    }
  }

  function auditQueryPath() {
    const params = new URLSearchParams({
      scope: "global",
      limit: elements.auditLimit.value || "300",
      sort: elements.auditSort.value || "desc",
      include_selected: "true",
    });
    if (controllerState.auditSelectedId) params.set("selected_id", controllerState.auditSelectedId);
    const filters = [
      ["operation", elements.auditOperation.value],
      ["event", elements.auditEvent.value.trim()],
      ["search", elements.auditSearch.value.trim()],
    ];
    for (const [name, value] of filters) {
      if (value) params.set(name, value);
    }
    return `/audit?${params.toString()}`;
  }

  async function loadAuditDetail(entryId) {
    if (!entryId) {
      clearAuditDetail();
      return null;
    }
    const generation = ++controllerState.auditDetailGeneration;
    elements.auditDetailTitle.textContent = auditEntryTitle(
      controllerState.auditEntries.find((entry) => entry.id === entryId) || {},
    );
    elements.auditDetailMeta.textContent = "Loading details";
    renderAuditDetailMessage(elements.auditDetailBody, `Loading ${entryId}`);
    try {
      const params = new URLSearchParams({ scope: "global", id: entryId });
      const payload = await request(`/audit/detail?${params.toString()}`);
      if (
        generation !== controllerState.auditDetailGeneration ||
        entryId !== controllerState.auditSelectedId
      ) return null;
      const entry = payload && payload.entry && typeof payload.entry === "object" ? payload.entry : null;
      if (!entry) throw new Error("Audit detail response was malformed");
      elements.auditDetailTitle.textContent = auditEntryTitle(entry);
      elements.auditDetailMeta.textContent = `Global · ${auditTimestamp(entry.ts)}`;
      renderAuditDetailInto(entry, elements.auditDetailBody);
      return entry;
    } catch (error) {
      if (
        generation !== controllerState.auditDetailGeneration ||
        entryId !== controllerState.auditSelectedId
      ) return null;
      elements.auditDetailMeta.textContent = "Details unavailable";
      renderAuditDetailMessage(
        elements.auditDetailBody,
        error instanceof Error ? error.message : String(error),
      );
      return null;
    }
  }

  async function refreshAudit() {
    if (controllerState.auditLoading) return null;
    const generation = ++controllerState.auditGeneration;
    const previousSelection = controllerState.auditSelectedId;
    controllerState.auditLoading = true;
    setAuditControls();
    elements.auditState.textContent = "Loading Global Audit";
    try {
      const payload = await request(auditQueryPath());
      if (generation !== controllerState.auditGeneration) return null;
      controllerState.auditDetailGeneration += 1;
      controllerState.auditEntries = Array.isArray(payload.entries)
        ? payload.entries.map((entry) => ({ ...entry }))
        : [];
      controllerState.auditSelectedId = controllerState.auditEntries.some((entry) => entry.id === previousSelection)
        ? previousSelection
        : text(controllerState.auditEntries[0] && controllerState.auditEntries[0].id, "");
      const total = Number.isInteger(payload.total_matched)
        ? payload.total_matched
        : controllerState.auditEntries.length;
      elements.auditSummary.textContent = `${controllerState.auditEntries.length} shown · ${total} matched · Global`;
      elements.auditState.textContent = `Loaded ${controllerState.auditEntries.length} global records`;
      renderAuditList();
      const selected = payload && payload.entry && typeof payload.entry === "object" ? payload.entry : null;
      if (selected && selected.id === controllerState.auditSelectedId) {
        elements.auditDetailTitle.textContent = auditEntryTitle(selected);
        elements.auditDetailMeta.textContent = `Global · ${auditTimestamp(selected.ts)}`;
        renderAuditDetailInto(selected, elements.auditDetailBody);
      } else if (payload && payload.entry_error) {
        elements.auditDetailMeta.textContent = "Details unavailable";
        renderAuditDetailMessage(elements.auditDetailBody, text(payload.entry_error));
      } else if (controllerState.auditSelectedId) {
        void loadAuditDetail(controllerState.auditSelectedId);
      }
      return payload;
    } catch (error) {
      if (generation !== controllerState.auditGeneration) return null;
      controllerState.auditEntries = [];
      controllerState.auditSelectedId = "";
      renderAuditList();
      elements.auditState.textContent = error instanceof Error ? error.message : String(error);
      return null;
    } finally {
      if (generation === controllerState.auditGeneration) {
        controllerState.auditLoading = false;
        setAuditControls();
      }
    }
  }

  function invalidate() {
    controllerState.auditGeneration += 1;
    controllerState.auditDetailGeneration += 1;
  }

  function bind() {
    elements.auditFilterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void refreshAudit();
    });
    for (const control of [elements.auditOperation, elements.auditSort, elements.auditLimit]) {
      control.addEventListener("change", () => void refreshAudit());
    }
  }

  return {
    bind,
    invalidate,
    refresh: refreshAudit,
    reset: resetAuditWorkspace,
  };
}
