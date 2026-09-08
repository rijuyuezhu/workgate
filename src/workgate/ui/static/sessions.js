export function createSessionsController({
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
    todoMachine: "local",
    todoSessionId: "",
    todoSessions: [],
    sessionIncludeInactive: false,
    sessionLoading: false,
    sessionTerminating: false,
    todoItems: [],
    todoRevision: 0,
    todoGeneration: 0,
    todoMutationBusy: false,
    todoDirty: false,
    todoSequence: 0,
    todoMachineStates: new Map([["local", "online"]]),
    todoLimits: {
      todos: 1000,
      bytes: 1000000,
      id_bytes: 256,
      content_bytes: 16384,
      label_bytes: 64,
    },
    sessionAuditEntries: [],
    sessionAuditSelectedId: "",
    sessionAuditGeneration: 0,
    sessionAuditDetailGeneration: 0,
    sessionAuditLoading: false,
  };

  function todoMachineOnline(machine = controllerState.todoMachine) {
    return machine === "local" || controllerState.todoMachineStates.get(machine) === "online";
  }

  function selectedSession() {
    return controllerState.todoSessions.find((session) => session.session_id === controllerState.todoSessionId) || null;
  }

  function sessionTimestamp(value) {
    const date = new Date(Number(value || 0) * 1000);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
  }

  function sessionTerminated(session = selectedSession()) {
    return Boolean(session && (session.termination_requested || session.termination_requested_at));
  }

  function sessionAvailability(session = selectedSession()) {
    return text(session && session.availability, "");
  }

  function sessionResourcesAvailable(session = selectedSession()) {
    const availability = sessionAvailability(session);
    return Boolean(session && (!availability || availability === "available"));
  }

  function sessionUnavailableLabel(session = selectedSession()) {
    const availability = sessionAvailability(session);
    if (availability === "missing_on_executor") return "Missing on executor";
    if (availability === "executor_offline") return "Executor offline";
    if (availability && availability !== "available") return text(availability, "Unavailable");
    return "";
  }

  function sessionActivityKnown(session = selectedSession()) {
    return Boolean(session && session.activity_known !== false);
  }

  function sessionActivityTimestamp(session = selectedSession()) {
    if (!sessionActivityKnown(session)) return "activity time unavailable";
    return sessionTimestamp(
      session && session.last_active_at != null ? session.last_active_at : session && session.updated_at,
    );
  }

  function setTodoControls() {
    const online = todoMachineOnline();
    const sessionReady = Boolean(controllerState.todoSessionId);
    const session = selectedSession();
    const resourcesAvailable = sessionReady && sessionResourcesAvailable(session);
    const executorOffline = sessionAvailability(session) === "executor_offline";
    elements.sessionMachine.disabled = controllerState.sessionLoading || controllerState.todoMutationBusy || controllerState.sessionTerminating;
    elements.sessionIncludeInactive.disabled = controllerState.sessionLoading || controllerState.todoMutationBusy || controllerState.sessionTerminating;
    elements.sessionRefresh.disabled = controllerState.sessionLoading || controllerState.todoMutationBusy || controllerState.sessionTerminating || !online;
    elements.sessionTerminate.disabled =
      controllerState.sessionLoading || controllerState.sessionTerminating || !online || !sessionReady || executorOffline || sessionTerminated(session);
    elements.todoRefresh.disabled = controllerState.todoMutationBusy || !online || !resourcesAvailable;
    elements.todoAdd.disabled = controllerState.todoMutationBusy || !online || !resourcesAvailable || controllerState.todoItems.length >= controllerState.todoLimits.todos;
    elements.todoSave.disabled = controllerState.todoMutationBusy || !online || !resourcesAvailable || !controllerState.todoDirty;
    elements.sessionAuditRefresh.disabled = controllerState.sessionAuditLoading || !online || !resourcesAvailable;
    for (const control of elements.sessionAuditFilterForm.querySelectorAll("input, select")) {
      control.disabled = controllerState.sessionAuditLoading || !online || !resourcesAvailable;
    }
    for (const control of elements.todoList.querySelectorAll("input, select, button")) {
      control.disabled = controllerState.todoMutationBusy || !online || !resourcesAvailable;
    }
    for (const row of elements.todoList.querySelectorAll(".todo-row")) {
      row.setAttribute("aria-disabled", controllerState.todoMutationBusy || !online || !resourcesAvailable ? "true" : "false");
    }
  }

  function setTodoMutationBusy(busy) {
    controllerState.todoMutationBusy = busy;
    setTodoControls();
  }

  function setTodoDirty(dirty = true) {
    controllerState.todoDirty = dirty;
    if (dirty) elements.todoState.textContent = `Unsaved changes · ${controllerState.todoSessionId}`;
    setTodoControls();
  }

  function clearSelectedSessionResources(message = "Select a session") {
    controllerState.todoItems = [];
    controllerState.todoRevision = 0;
    controllerState.todoDirty = false;
    controllerState.todoGeneration += 1;
    renderTodos();
    elements.todoState.textContent = message;
    resetSessionAuditWorkspace(message);
  }

  function resetTodoWorkspace(machine) {
    controllerState.todoMachine = machine || "local";
    controllerState.todoSessionId = "";
    controllerState.todoSessions = [];
    controllerState.sessionLoading = false;
    controllerState.sessionTerminating = false;
    elements.sessionMachine.value = controllerState.todoMachine;
    elements.sessionIncludeInactive.checked = controllerState.sessionIncludeInactive;
    elements.sessionList.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Loading active sessions…";
    elements.sessionList.append(empty);
    clearSelectedSessionResources("Select a session");
    elements.sessionState.textContent = `Not loaded · ${controllerState.todoMachine}`;
    renderSessionDetail();
    setTodoControls();
  }

  function sessionOptionLabel(session) {
    const sessionId = text(session && session.session_id, "");
    const label = text(session && session.label, "");
    const workdir = text(session && session.workdir, "");
    return `${label || workdir || "session"} · ${sessionId}`;
  }

  function renderSessionDetail() {
    const session = selectedSession();
    if (!session) {
      elements.sessionDetailTitle.textContent = "No session selected";
      elements.sessionDetailStatus.textContent = "Select a session to inspect its state";
      elements.sessionDetailId.textContent = "—";
      elements.sessionDetailTarget.textContent = "—";
      elements.sessionDetailMachine.textContent = "—";
      elements.sessionDetailWorkdir.textContent = "—";
      elements.sessionDetailCreated.textContent = "—";
      elements.sessionDetailUpdated.textContent = "—";
      setTodoControls();
      return;
    }
    const terminated = sessionTerminated(session);
    const finalStatus = text(session.status, "");
    const availability = sessionAvailability(session);
    elements.sessionDetailTitle.textContent = text(session.label, text(session.session_id, "session"));
    elements.sessionDetailStatus.textContent = finalStatus === "ended"
      ? "Ended · executor confirmed session absence"
      : finalStatus === "terminating"
        ? "Termination in progress · waiting for executor absence"
        : availability === "missing_on_executor"
          ? "Unavailable · missing on executor"
          : availability === "executor_offline"
            ? "Unavailable · executor offline"
            : terminated
              ? `Immediate termination requested ${sessionTimestamp(session.termination_requested_at)}`
              : !sessionActivityKnown(session)
                ? "Available · activity time unavailable"
                : session.active === false
                  ? "Inactive · outside the recent 5 hour window"
                  : "Active · responded within the last 5 hours";
    elements.sessionDetailId.textContent = text(session.session_id);
    elements.sessionDetailTarget.textContent = text(session.target);
    elements.sessionDetailMachine.textContent = text(session.machine, session.target === "local" ? "local" : "—");
    elements.sessionDetailWorkdir.textContent = text(session.workdir);
    elements.sessionDetailCreated.textContent = sessionTimestamp(session.created_at);
    elements.sessionDetailUpdated.textContent = sessionTimestamp(session.updated_at);
    setTodoControls();
  }

  function renderTodoSessions(sessions) {
    controllerState.todoSessions = Array.isArray(sessions) ? sessions : [];
    if (!controllerState.todoSessions.some((session) => session.session_id === controllerState.todoSessionId)) {
      controllerState.todoSessionId = text(controllerState.todoSessions[0] && controllerState.todoSessions[0].session_id, "");
      clearSelectedSessionResources(controllerState.todoSessionId ? "Loading selected session" : "No sessions available");
    }
    elements.sessionList.replaceChildren();
    if (!controllerState.todoSessions.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = controllerState.sessionIncludeInactive
        ? `No agent sessions on ${controllerState.todoMachine}.`
        : `No sessions active in the last 5 hours on ${controllerState.todoMachine}.`;
      elements.sessionList.append(empty);
    } else {
      for (const session of controllerState.todoSessions) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "session-entry";
        button.dataset.sessionId = text(session.session_id, "");
        button.setAttribute("aria-current", session.session_id === controllerState.todoSessionId ? "true" : "false");
        const title = document.createElement("strong");
        title.textContent = sessionOptionLabel(session);
        const meta = document.createElement("span");
        meta.className = sessionTerminated(session) ? "session-entry-meta session-entry-terminated" : "session-entry-meta";
        const unavailable = sessionUnavailableLabel(session);
        meta.textContent = sessionTerminated(session)
          ? "termination requested"
          : unavailable
            ? `${unavailable.toLowerCase()} · ${sessionTimestamp(session.updated_at)}`
            : !sessionActivityKnown(session)
              ? "available · activity time unavailable"
              : session.active === false
                ? `inactive · ${sessionActivityTimestamp(session)}`
                : `active · ${sessionActivityTimestamp(session)}`;
        button.append(title, meta);
        button.addEventListener("click", () => void selectTodoSession(session.session_id));
        elements.sessionList.append(button);
      }
    }
    renderSessionDetail();
    setTodoControls();
  }

  function sessionSnapshotPath() {
    const params = new URLSearchParams({
      machine: controllerState.todoMachine,
      session_id: controllerState.todoSessionId,
      limit: elements.sessionAuditLimit.value || "300",
      sort: elements.sessionAuditSort.value || "desc",
    });
    if (controllerState.sessionAuditSelectedId) params.set("selected_id", controllerState.sessionAuditSelectedId);
    if (elements.sessionAuditOperation.value) params.set("operation", elements.sessionAuditOperation.value);
    if (elements.sessionAuditSearch.value.trim()) params.set("search", elements.sessionAuditSearch.value.trim());
    return `/sessions/snapshot?${params.toString()}`;
  }

  async function refreshSelectedSessionResourcesIndependently() {
    const results = await Promise.allSettled([
      refreshTodos({ force: true }),
      refreshSessionAudit(),
    ]);
    for (const result of results) {
      if (result.status === "rejected" && result.reason?.authenticationRequired) throw result.reason;
    }
    return results;
  }

  async function refreshSelectedSessionResources() {
    if (!controllerState.todoSessionId || !todoMachineOnline()) return null;
    const session = selectedSession();
    if (!sessionResourcesAvailable(session)) {
      const unavailable = sessionUnavailableLabel(session) || "Session unavailable";
      clearSelectedSessionResources(unavailable);
      setTodoControls();
      return null;
    }
    const todoRequestGeneration = ++controllerState.todoGeneration;
    const auditRequestGeneration = ++controllerState.sessionAuditGeneration;
    const requestedMachine = controllerState.todoMachine;
    const requestedSession = controllerState.todoSessionId;
    const previousSelection = controllerState.sessionAuditSelectedId;
    controllerState.sessionAuditLoading = true;
    elements.todoState.textContent = `Loading ${requestedSession}`;
    elements.sessionAuditState.textContent = `Loading ${requestedSession}`;
    setTodoControls();
    try {
      const payload = await request(sessionSnapshotPath());
      if (
        todoRequestGeneration !== controllerState.todoGeneration ||
        auditRequestGeneration !== controllerState.sessionAuditGeneration ||
        requestedMachine !== controllerState.todoMachine ||
        requestedSession !== controllerState.todoSessionId
      ) return null;
      applyTodoPayload(payload, requestedSession);
      if (!payload.audit || typeof payload.audit !== "object") {
        throw new Error("Session snapshot returned malformed Audit state");
      }
      applySessionAuditPayload(payload.audit, requestedSession, previousSelection);
      return payload;
    } catch (error) {
      if (
        todoRequestGeneration !== controllerState.todoGeneration ||
        auditRequestGeneration !== controllerState.sessionAuditGeneration ||
        requestedMachine !== controllerState.todoMachine ||
        requestedSession !== controllerState.todoSessionId
      ) return null;
      if (error?.status === 403) {
        controllerState.sessionAuditLoading = false;
        setTodoControls();
        return await refreshSelectedSessionResourcesIndependently();
      }
      const message = error instanceof Error ? error.message : String(error);
      elements.todoState.textContent = message;
      controllerState.sessionAuditEntries = [];
      controllerState.sessionAuditSelectedId = "";
      renderSessionAuditList();
      elements.sessionAuditState.textContent = message;
      if (error?.authenticationRequired) throw error;
      return null;
    } finally {
      if (auditRequestGeneration === controllerState.sessionAuditGeneration) {
        controllerState.sessionAuditLoading = false;
      }
      setTodoControls();
    }
  }

  async function selectTodoSession(next) {
    if (!next || next === controllerState.todoSessionId || controllerState.todoMutationBusy || controllerState.sessionLoading) return;
    if (controllerState.todoDirty && !globalThis.confirm(`Discard unsaved changes in ${controllerState.todoSessionId}?`)) return;
    controllerState.todoSessionId = next;
    clearSelectedSessionResources("Loading selected session");
    renderTodoSessions(controllerState.todoSessions);
    await refreshSelectedSessionResources();
  }

  async function refreshTodoSessions() {
    const requestedMachine = controllerState.todoMachine;
    const previousSession = controllerState.todoSessionId;
    controllerState.sessionLoading = true;
    setTodoControls();
    elements.sessionState.textContent = `Loading sessions on ${requestedMachine}`;
    try {
      const params = new URLSearchParams({ machine: requestedMachine });
      if (controllerState.sessionIncludeInactive) params.set("include_inactive", "true");
      const payload = await request(`/sessions?${params.toString()}`);
      if (requestedMachine !== controllerState.todoMachine) return null;
      renderTodoSessions(payload.sessions);
      elements.sessionState.textContent = `${payload.count || 0} ${controllerState.sessionIncludeInactive ? "total" : "active"} sessions · ${requestedMachine}`;
      if (!controllerState.todoSessionId) clearSelectedSessionResources(`No agent sessions on ${requestedMachine}`);
      else if (controllerState.todoSessionId !== previousSession) clearSelectedSessionResources("Loading selected session");
      return payload;
    } catch (error) {
      if (requestedMachine !== controllerState.todoMachine) return null;
      renderTodoSessions([]);
      elements.sessionState.textContent = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      if (requestedMachine === controllerState.todoMachine) {
        controllerState.sessionLoading = false;
        setTodoControls();
      }
    }
  }

  async function refreshTodoContext() {
    await refreshTodoSessions();
    if (!controllerState.todoSessionId) return null;
    await refreshSelectedSessionResources();
    return selectedSession();
  }

  function renderTodoMachines(machines) {
    const available = Array.isArray(machines) ? machines : [];
    controllerState.todoMachineStates = new Map([["local", "online"]]);
    elements.sessionMachine.replaceChildren();
    let localPresent = false;
    let currentPresent = false;
    let currentOnline = controllerState.todoMachine === "local";
    for (const machine of available) {
      const name = text(machine.name, "");
      if (!name) continue;
      if (name === "local") localPresent = true;
      const state = name === "local" ? "online" : text(machine.status, "offline");
      const online = name === "local" || state === "online";
      controllerState.todoMachineStates.set(name, state);
      const option = document.createElement("option");
      option.value = name;
      option.textContent = online ? name : `${name} (${state})`;
      option.disabled = !online;
      option.selected = name === controllerState.todoMachine;
      if (option.selected) {
        currentPresent = true;
        currentOnline = online;
      }
      elements.sessionMachine.append(option);
    }
    if (!localPresent) {
      const local = document.createElement("option");
      local.value = "local";
      local.textContent = "local";
      local.selected = controllerState.todoMachine === "local";
      elements.sessionMachine.prepend(local);
      if (local.selected) {
        currentPresent = true;
        currentOnline = true;
      }
    }
    if (!currentPresent && controllerState.todoMachine !== "local") {
      const stale = document.createElement("option");
      stale.value = controllerState.todoMachine;
      stale.textContent = `${controllerState.todoMachine} (unavailable)`;
      stale.disabled = true;
      stale.selected = true;
      elements.sessionMachine.append(stale);
    }
    if ((!currentPresent || !currentOnline) && !controllerState.todoDirty && !controllerState.todoMutationBusy) {
      const changed = controllerState.todoMachine !== "local";
      resetTodoWorkspace("local");
      if (changed) void refreshTodoContext();
    } else {
      elements.sessionMachine.value = controllerState.todoMachine;
      if (!currentOnline) elements.sessionState.textContent = `${controllerState.todoMachine} is offline`;
      setTodoControls();
    }
  }

  async function terminateSelectedSession() {
    const session = selectedSession();
    if (!session || sessionTerminated(session) || controllerState.sessionTerminating) return;
    const label = sessionOptionLabel(session);
    if (!globalThis.confirm(`Immediately terminate ${label}? Any later model tool call for this session will be told to stop all work.`)) return;
    controllerState.sessionTerminating = true;
    setTodoControls();
    elements.sessionState.textContent = `Requesting immediate termination for ${session.session_id}`;
    try {
      const payload = await request("/sessions/terminate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ machine: controllerState.todoMachine, session_id: session.session_id }),
      });
      const updated = payload && payload.session ? payload.session : null;
      if (updated) {
        controllerState.todoSessions = controllerState.todoSessions.map((item) => item.session_id === updated.session_id ? updated : item);
      }
      renderTodoSessions(controllerState.todoSessions);
      elements.sessionState.textContent = updated && updated.status === "ended"
        ? `${session.session_id} ended`
        : `${session.session_id} marked for immediate termination`;
    } catch (error) {
      elements.sessionState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      controllerState.sessionTerminating = false;
      setTodoControls();
    }
  }

  function todoOption(select, value, choices) {
    const normalized = text(value, choices[0]);
    const values = choices.includes(normalized) ? choices : [...choices, normalized];
    for (const choice of values) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice.replaceAll("_", " ");
      option.selected = choice === normalized;
      select.append(option);
    }
  }

  function todoField(labelText, className, control) {
    const label = document.createElement("label");
    label.className = `todo-field ${className}`;
    const caption = document.createElement("span");
    caption.textContent = labelText;
    label.append(caption, control);
    return label;
  }

  function visibleTodos() {
    const filter = elements.todoFilter.value;
    return controllerState.todoItems
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => {
        if (filter === "completed") return item.status === "completed";
        if (filter === "open") return item.status !== "completed";
        return true;
      });
  }

  function renderTodoSummary() {
    const completed = controllerState.todoItems.filter((item) => item.status === "completed").length;
    const open = controllerState.todoItems.length - completed;
    const labels = [
      `${controllerState.todoItems.length} total`,
      `${open} open`,
      `${completed} completed`,
      `revision ${controllerState.todoRevision}`,
    ];
    elements.todoSummary.replaceChildren(
      ...labels.map((label) => {
        const span = document.createElement("span");
        span.textContent = label;
        return span;
      }),
    );
  }

  function renderTodos() {
    renderTodoSummary();
    const visible = visibleTodos();
    elements.todoList.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = controllerState.todoItems.length ? "No todos match this filter." : "No todos in this machine session.";
      elements.todoList.append(empty);
      setTodoControls();
      return;
    }

    for (const { item, index } of visible) {
      const row = document.createElement("article");
      row.className = "todo-row";
      row.dataset.todoId = item.id;

      const content = document.createElement("input");
      content.type = "text";
      content.value = item.content;
      content.maxLength = controllerState.todoLimits.content_bytes;
      content.autocomplete = "off";
      content.spellcheck = true;
      content.addEventListener("input", () => {
        controllerState.todoItems[index].content = content.value;
        setTodoDirty();
      });

      const status = document.createElement("select");
      todoOption(status, item.status, ["pending", "in_progress", "completed"]);
      status.addEventListener("change", () => {
        controllerState.todoItems[index].status = status.value;
        setTodoDirty();
        renderTodoSummary();
        if (elements.todoFilter.value !== "all") renderTodos();
      });

      const priority = document.createElement("select");
      todoOption(priority, item.priority, ["high", "medium", "low"]);
      priority.addEventListener("change", () => {
        controllerState.todoItems[index].priority = priority.value;
        setTodoDirty();
      });

      const identifier = document.createElement("span");
      identifier.className = "todo-id";
      identifier.textContent = item.id;
      identifier.title = item.id;

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "todo-remove";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        controllerState.todoItems.splice(index, 1);
        setTodoDirty();
        renderTodos();
      });

      row.append(
        identifier,
        todoField("Content", "todo-field-content", content),
        todoField("Status", "todo-field-status", status),
        todoField("Priority", "todo-field-priority", priority),
        remove,
      );
      elements.todoList.append(row);
    }
    setTodoControls();
  }

  function newTodoId() {
    let candidate;
    do {
      controllerState.todoSequence += 1;
      candidate = `ui-${Date.now().toString(36)}-${controllerState.todoSequence.toString(36)}`;
    } while (controllerState.todoItems.some((item) => item.id === candidate));
    return candidate;
  }

  function addTodo() {
    if (controllerState.todoMutationBusy || !todoMachineOnline() || !controllerState.todoSessionId || controllerState.todoItems.length >= controllerState.todoLimits.todos) return;
    const item = { id: newTodoId(), content: "", status: "pending", priority: "medium" };
    controllerState.todoItems.push(item);
    elements.todoFilter.value = "all";
    setTodoDirty();
    renderTodos();
    const row = elements.todoList.querySelector(`[data-todo-id="${CSS.escape(item.id)}"]`);
    row?.querySelector("input")?.focus();
  }

  function todoQuery() {
    return `/todos?${new URLSearchParams({ machine: controllerState.todoMachine, session_id: controllerState.todoSessionId }).toString()}`;
  }

  function applyTodoPayload(payload, requestedSession) {
    controllerState.todoItems = Array.isArray(payload.todos)
      ? payload.todos.map((item) => ({
          id: text(item.id, ""),
          content: text(item.content, ""),
          status: text(item.status, "pending"),
          priority: text(item.priority, "medium"),
        }))
      : [];
    controllerState.todoRevision = Number.isInteger(payload.revision) && payload.revision >= 0 ? payload.revision : 0;
    if (payload.limits && typeof payload.limits === "object") {
      controllerState.todoLimits = { ...controllerState.todoLimits, ...payload.limits };
    }
    controllerState.todoDirty = false;
    renderTodos();
    elements.todoState.textContent = `${requestedSession} · loaded ${controllerState.todoItems.length} todos`;
  }

  async function refreshTodos({ force = false } = {}) {
    if (!controllerState.todoSessionId || (!force && (controllerState.todoDirty || controllerState.todoMutationBusy))) return null;
    const generation = ++controllerState.todoGeneration;
    const requestedMachine = controllerState.todoMachine;
    const requestedSession = controllerState.todoSessionId;
    elements.todoState.textContent = `Loading ${requestedSession}`;
    setTodoControls();
    try {
      const payload = await request(todoQuery());
      if (generation !== controllerState.todoGeneration || requestedMachine !== controllerState.todoMachine || requestedSession !== controllerState.todoSessionId) return null;
      applyTodoPayload(payload, requestedSession);
      return payload;
    } catch (error) {
      if (generation !== controllerState.todoGeneration || requestedMachine !== controllerState.todoMachine || requestedSession !== controllerState.todoSessionId) return null;
      elements.todoState.textContent = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      if (generation === controllerState.todoGeneration) setTodoControls();
    }
  }

  async function saveTodos() {
    if (!controllerState.todoDirty || controllerState.todoMutationBusy || !todoMachineOnline() || !controllerState.todoSessionId) return;
    const generation = ++controllerState.todoGeneration;
    const requestedMachine = controllerState.todoMachine;
    const requestedSession = controllerState.todoSessionId;
    const expectedRevision = controllerState.todoRevision;
    const todos = controllerState.todoItems.map((item) => ({ ...item }));
    setTodoMutationBusy(true);
    elements.todoState.textContent = `Saving ${requestedSession} revision ${expectedRevision}`;
    try {
      const payload = await request("/todos", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          machine: requestedMachine,
          session_id: requestedSession,
          expected_revision: expectedRevision,
          todos,
        }),
      });
      if (generation !== controllerState.todoGeneration || requestedMachine !== controllerState.todoMachine || requestedSession !== controllerState.todoSessionId) return;
      controllerState.todoItems = Array.isArray(payload.todos) ? payload.todos.map((item) => ({ ...item })) : [];
      controllerState.todoRevision = Number(payload.revision) || expectedRevision + 1;
      controllerState.todoDirty = false;
      renderTodos();
      elements.todoState.textContent = `Saved ${requestedSession} · revision ${controllerState.todoRevision}`;
    } catch (error) {
      if (generation !== controllerState.todoGeneration || requestedMachine !== controllerState.todoMachine || requestedSession !== controllerState.todoSessionId) return;
      if (error && error.status === 409) {
        controllerState.todoDirty = false;
        try {
          await refreshTodos({ force: true });
          elements.todoState.textContent = "Todo list changed elsewhere; reloaded the latest revision";
        } catch (reloadError) {
          elements.todoState.textContent = reloadError instanceof Error ? reloadError.message : String(reloadError);
        }
      } else {
        elements.todoState.textContent = error instanceof Error ? error.message : String(error);
      }
    } finally {
      if (requestedMachine === controllerState.todoMachine && requestedSession === controllerState.todoSessionId) setTodoMutationBusy(false);
    }
  }

  function clearSessionAuditDetail(message = "Select a session Audit record.") {
    controllerState.sessionAuditDetailGeneration += 1;
    elements.sessionAuditDetailTitle.textContent = "No record selected";
    elements.sessionAuditDetailMeta.textContent = controllerState.todoSessionId || controllerState.todoMachine;
    renderAuditDetailMessage(elements.sessionAuditDetailBody, message);
  }

  function resetSessionAuditWorkspace(message = "Select a session") {
    controllerState.sessionAuditEntries = [];
    controllerState.sessionAuditSelectedId = "";
    controllerState.sessionAuditLoading = false;
    controllerState.sessionAuditGeneration += 1;
    elements.sessionAuditList.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = message;
    elements.sessionAuditList.append(empty);
    elements.sessionAuditSummary.textContent = "0 entries";
    elements.sessionAuditState.textContent = message;
    clearSessionAuditDetail();
    setTodoControls();
  }

  function renderSessionAuditList() {
    elements.sessionAuditList.replaceChildren();
    if (!controllerState.sessionAuditEntries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = controllerState.todoSessionId
        ? `No local Audit records match for ${controllerState.todoSessionId}.`
        : "Select a session to load its Audit records.";
      elements.sessionAuditList.append(empty);
      clearSessionAuditDetail("No matching session Audit record is available.");
      return;
    }
    for (const entry of controllerState.sessionAuditEntries) {
      elements.sessionAuditList.append(
        auditEntryButton(entry, controllerState.sessionAuditSelectedId, () => {
          controllerState.sessionAuditSelectedId = text(entry.id, "");
          renderSessionAuditList();
          void loadSessionAuditDetail(controllerState.sessionAuditSelectedId);
        }),
      );
    }
  }

  function renderSessionAuditDetailEntry(entry, requestedSession) {
    elements.sessionAuditDetailTitle.textContent = auditEntryTitle(entry);
    elements.sessionAuditDetailMeta.textContent = `${requestedSession} · ${auditTimestamp(entry.ts)}`;
    renderAuditDetailInto(entry, elements.sessionAuditDetailBody);
  }

  function applySessionAuditPayload(payload, requestedSession, previousSelection) {
    controllerState.sessionAuditDetailGeneration += 1;
    controllerState.sessionAuditEntries = Array.isArray(payload.entries)
      ? payload.entries.map((entry) => ({ ...entry }))
      : [];
    controllerState.sessionAuditSelectedId = controllerState.sessionAuditEntries.some((entry) => entry.id === previousSelection)
      ? previousSelection
      : text(controllerState.sessionAuditEntries[0] && controllerState.sessionAuditEntries[0].id, "");
    const total = Number.isInteger(payload.total_matched) ? payload.total_matched : controllerState.sessionAuditEntries.length;
    elements.sessionAuditSummary.textContent = `${controllerState.sessionAuditEntries.length} shown · ${total} matched · ${requestedSession}`;
    elements.sessionAuditState.textContent = `${requestedSession} · loaded ${controllerState.sessionAuditEntries.length} records`;
    renderSessionAuditList();
    const selected = payload && payload.entry && typeof payload.entry === "object" ? payload.entry : null;
    if (selected && selected.id === controllerState.sessionAuditSelectedId) {
      renderSessionAuditDetailEntry(selected, requestedSession);
    } else if (payload && payload.entry_error) {
      elements.sessionAuditDetailMeta.textContent = "Details unavailable";
      renderAuditDetailMessage(elements.sessionAuditDetailBody, text(payload.entry_error));
    } else if (controllerState.sessionAuditSelectedId) {
      void loadSessionAuditDetail(controllerState.sessionAuditSelectedId);
    }
  }

  function sessionAuditQueryPath() {
    const params = new URLSearchParams({
      machine: controllerState.todoMachine,
      scope: "session",
      session: controllerState.todoSessionId,
      limit: elements.sessionAuditLimit.value || "300",
      sort: elements.sessionAuditSort.value || "desc",
      include_selected: "true",
    });
    if (controllerState.sessionAuditSelectedId) params.set("selected_id", controllerState.sessionAuditSelectedId);
    if (elements.sessionAuditOperation.value) params.set("operation", elements.sessionAuditOperation.value);
    if (elements.sessionAuditSearch.value.trim()) params.set("search", elements.sessionAuditSearch.value.trim());
    return `/audit?${params.toString()}`;
  }

  async function loadSessionAuditDetail(entryId) {
    if (!entryId || !controllerState.todoSessionId || !todoMachineOnline()) {
      clearSessionAuditDetail();
      return null;
    }
    const generation = ++controllerState.sessionAuditDetailGeneration;
    const requestedMachine = controllerState.todoMachine;
    const requestedSession = controllerState.todoSessionId;
    elements.sessionAuditDetailTitle.textContent = auditEntryTitle(
      controllerState.sessionAuditEntries.find((entry) => entry.id === entryId) || {},
    );
    elements.sessionAuditDetailMeta.textContent = "Loading details";
    renderAuditDetailMessage(elements.sessionAuditDetailBody, `Loading ${requestedSession}:${entryId}`);
    try {
      const params = new URLSearchParams({
        machine: requestedMachine,
        scope: "session",
        session: requestedSession,
        id: entryId,
      });
      const payload = await request(`/audit/detail?${params.toString()}`);
      if (
        generation !== controllerState.sessionAuditDetailGeneration ||
        requestedMachine !== controllerState.todoMachine ||
        requestedSession !== controllerState.todoSessionId ||
        entryId !== controllerState.sessionAuditSelectedId
      ) return null;
      const entry = payload && payload.entry && typeof payload.entry === "object" ? payload.entry : null;
      if (!entry) throw new Error("Session Audit detail response was malformed");
      renderSessionAuditDetailEntry(entry, requestedSession);
      return entry;
    } catch (error) {
      if (
        generation !== controllerState.sessionAuditDetailGeneration ||
        requestedMachine !== controllerState.todoMachine ||
        requestedSession !== controllerState.todoSessionId ||
        entryId !== controllerState.sessionAuditSelectedId
      ) return null;
      elements.sessionAuditDetailMeta.textContent = "Details unavailable";
      renderAuditDetailMessage(
        elements.sessionAuditDetailBody,
        error instanceof Error ? error.message : String(error),
      );
      return null;
    }
  }

  async function refreshSessionAudit() {
    if (controllerState.sessionAuditLoading || !controllerState.todoSessionId || !todoMachineOnline()) return null;
    const generation = ++controllerState.sessionAuditGeneration;
    const requestedMachine = controllerState.todoMachine;
    const requestedSession = controllerState.todoSessionId;
    const previousSelection = controllerState.sessionAuditSelectedId;
    controllerState.sessionAuditLoading = true;
    setTodoControls();
    elements.sessionAuditState.textContent = `Loading ${requestedSession}`;
    try {
      const payload = await request(sessionAuditQueryPath());
      if (
        generation !== controllerState.sessionAuditGeneration ||
        requestedMachine !== controllerState.todoMachine ||
        requestedSession !== controllerState.todoSessionId
      ) return null;
      applySessionAuditPayload(payload, requestedSession, previousSelection);
      return payload;
    } catch (error) {
      if (
        generation !== controllerState.sessionAuditGeneration ||
        requestedMachine !== controllerState.todoMachine ||
        requestedSession !== controllerState.todoSessionId
      ) return null;
      controllerState.sessionAuditEntries = [];
      controllerState.sessionAuditSelectedId = "";
      renderSessionAuditList();
      elements.sessionAuditState.textContent = error instanceof Error ? error.message : String(error);
      return null;
    } finally {
      if (generation === controllerState.sessionAuditGeneration) {
        controllerState.sessionAuditLoading = false;
        setTodoControls();
      }
    }
  }

  function invalidate() {
    controllerState.todoGeneration += 1;
    controllerState.todoDirty = false;
  }

  function bind() {
    elements.sessionMachine.addEventListener("change", () => {
      if (controllerState.todoMutationBusy || controllerState.sessionLoading || controllerState.sessionTerminating) return;
      const next = elements.sessionMachine.value || "local";
      if (controllerState.todoDirty && !globalThis.confirm(`Discard unsaved changes in ${controllerState.todoSessionId}?`)) {
        elements.sessionMachine.value = controllerState.todoMachine;
        return;
      }
      resetTodoWorkspace(next);
      void refreshTodoContext();
    });
    elements.sessionIncludeInactive.addEventListener("change", () => {
      if (controllerState.sessionLoading || controllerState.sessionTerminating) return;
      if (controllerState.todoDirty && !globalThis.confirm(`Discard unsaved changes in ${controllerState.todoSessionId}?`)) {
        elements.sessionIncludeInactive.checked = controllerState.sessionIncludeInactive;
        return;
      }
      controllerState.sessionIncludeInactive = elements.sessionIncludeInactive.checked;
      controllerState.todoDirty = false;
      void refreshTodoContext();
    });
    elements.sessionRefresh.addEventListener("click", () => {
      if (controllerState.sessionLoading || controllerState.sessionTerminating) return;
      if (controllerState.todoDirty && !globalThis.confirm(`Discard unsaved changes in ${controllerState.todoSessionId}?`)) return;
      controllerState.todoDirty = false;
      void refreshTodoContext();
    });
    elements.sessionTerminate.addEventListener("click", () => void terminateSelectedSession());

    elements.sessionAuditFilterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void refreshSessionAudit();
    });
    for (const control of [elements.sessionAuditOperation, elements.sessionAuditSort, elements.sessionAuditLimit]) {
      control.addEventListener("change", () => void refreshSessionAudit());
    }

    elements.todoFilter.addEventListener("change", renderTodos);
    elements.todoAdd.addEventListener("click", addTodo);
    elements.todoSave.addEventListener("click", () => void saveTodos());
    elements.todoRefresh.addEventListener("click", () => {
      if (controllerState.todoMutationBusy || !controllerState.todoSessionId) return;
      if (controllerState.todoDirty && !globalThis.confirm(`Discard unsaved changes in ${controllerState.todoSessionId}?`)) return;
      controllerState.todoDirty = false;
      void refreshTodos({ force: true });
    });

  }

  return {
    bind,
    invalidate,
    refresh: refreshTodoContext,
    renderMachines: renderTodoMachines,
    reset: resetTodoWorkspace,
  };
}
