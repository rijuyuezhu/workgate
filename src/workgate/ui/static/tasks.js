export function createTasksController({
  elements,
  request,
  text,
  mountAuditLists,
  mountTaskLists,
  mountTaskDetails,
  mountTaskPlan,
  auditEntryTitle,
  auditTimestamp,
  renderAuditDetailInto,
  renderAuditDetailMessage,
  onInventory = () => {},
  onNavigate = () => {},
  onSessionChange = () => {},
  initialTaskId = "",
  initialSessionId = "",
}) {
  const controllerState = {
    taskId: text(initialTaskId, ""),
    sessionId: text(initialSessionId, ""),
    tasks: [],
    selectedTaskRow: null,
    selectedLegacySession: null,
    taskFilter: "active",
    taskCursor: "",
    taskCursorStack: [],
    taskNextCursor: null,
    taskTotal: 0,
    taskOverallCount: 0,
    taskActiveTotal: 0,
    taskPageCount: 0,
    unattachedSessions: [],
    sessions: [],
    executorTargets: new Map(),
    workspaceLoading: false,
    workspaceRefreshing: false,
    workspaceGeneration: 0,
    selectionRevision: 0,
    inventorySignature: "",
    sessionTerminating: false,
    taskMutationBusy: false,
    task: null,
    todoItems: [],
    todoGeneration: 0,
    todoMutationBusy: false,
    todoDirty: false,
    todoSignature: "",
    todoSequence: 0,
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
    sessionAuditRefreshing: false,
  };
  const auditListView = mountAuditLists(elements.sessionAuditList, (id) => {
    controllerState.sessionAuditSelectedId = id;
    renderSessionAuditList();
    void loadSessionAuditDetail(id);
    onNavigate("session-record");
  });
  auditListView.render([], "", "Select a session to load its Audit records.");
  const taskLists = mountTaskLists(elements.taskList, elements.sessionList, {
    task: (id) => void selectTask(id),
    unattached: (id) => void selectUnattachedSession(id),
    session: (id) => {
      if (id === controllerState.sessionId) onNavigate("session");
      else void selectSession(id);
    },
  });
  taskLists.renderTasks({
    tasks: [], unattached: [], selectedTaskId: "", selectedSessionId: "",
    emptyMessage: "Loading tasks…",
  });
  taskLists.renderSessions({
    sessions: [], selectedSessionId: "",
    emptyMessage: "Select a task to inspect its execution sessions.",
  });

  const taskDetails = mountTaskDetails(
    elements.taskProgressLead, elements.taskProgressGrid,
    elements.sessionDetailHeading, elements.sessionDetailGrid,
  );
  taskDetails.renderTask();
  taskDetails.renderSession();

  function updateTodo(id, field, value) {
    const item = controllerState.todoItems.find((candidate) => candidate.id === id);
    if (!item) return;
    item[field] = value;
    setTodoDirty();
    renderTodos();
  }

  const planView = mountTaskPlan(elements.todoSummary, elements.todoList, {
    content: (id, value) => updateTodo(id, "content", value),
    status: (id, value) => updateTodo(id, "status", value),
    priority: (id, value) => updateTodo(id, "priority", value),
    remove: (id) => {
      const index = controllerState.todoItems.findIndex((candidate) => candidate.id === id);
      if (index === -1) return;
      controllerState.todoItems.splice(index, 1);
      setTodoDirty();
      renderTodos();
    },
  });
  planView.render({
    rows: [], counts: { total: 0, open: 0, completed: 0, skipped: 0 },
    contentMaxLength: controllerState.todoLimits.content_bytes,
    emptyMessage: "Select a task to load its plan.",
  });

  let activeTab = initialSessionId ? "sessions" : "progress";

  function showTab(tab) {
    activeTab = tab;
    for (const button of document.querySelectorAll("[data-task-tab]")) {
      const selected = button.dataset.taskTab === tab;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      document.getElementById(`task-panel-${button.dataset.taskTab}`).hidden = !selected;
    }
  }

  function updateTaskTabs() {
    const hasTask = Boolean(selectedTaskId());
    for (const button of document.querySelectorAll("[data-task-tab]")) {
      button.disabled = !hasTask && button.dataset.taskTab !== "sessions";
    }
    if (!hasTask && controllerState.sessionId) showTab("sessions");
  }

  function selectedTaskId() {
    return text(controllerState.taskId, "");
  }

  function selectedTaskSummary() {
    return controllerState.selectedTaskRow?.task_id === controllerState.taskId
      ? controllerState.selectedTaskRow
      : controllerState.tasks.find((task) => task.task_id === controllerState.taskId) || null;
  }

  function selectedSession() {
    return (
      controllerState.sessions.find(
        (session) => session.session_id === controllerState.sessionId,
      ) ||
      controllerState.unattachedSessions.find(
        (session) => session.session_id === controllerState.sessionId,
      ) ||
      (controllerState.selectedLegacySession?.session_id === controllerState.sessionId
        ? controllerState.selectedLegacySession : null)
    );
  }

  function sessionTimestamp(value) {
    const date = new Date(Number(value || 0) * 1000);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
  }

  function sessionTerminated(session = selectedSession()) {
    return Boolean(
      session && (session.status === "ended" || session.status === "terminating")
    );
  }

  function sessionAvailability(session = selectedSession()) {
    if (session && controllerState.executorTargets.get(session.executor_id)?.status !== "online") {
      return "executor_offline";
    }
    return text(session && session.availability, "");
  }

  function executorName(executorId) {
    return text(controllerState.executorTargets.get(executorId)?.name, text(executorId, "Unknown executor"));
  }

  function renderExecutors(targets) {
    const previous = sessionAvailability();
    controllerState.executorTargets = new Map(
      targets.map((item) => [item.executor_id, item]),
    );
    const previousExecutor = elements.taskSessionExecutor.value;
    elements.taskSessionExecutor.replaceChildren();
    for (const target of targets) {
      if (target.status !== "online") continue;
      const option = document.createElement("option");
      option.value = target.executor_id;
      option.textContent = text(target.name, target.executor_id);
      elements.taskSessionExecutor.append(option);
    }
    if (controllerState.executorTargets.get(previousExecutor)?.status === "online") {
      elements.taskSessionExecutor.value = previousExecutor;
    }
    // React updates executor identity in place without replacing focused rows.
    if (controllerState.inventorySignature) {
      renderTaskList();
      renderSessions(controllerState.sessions);
    } else {
      renderSessionDetail();
    }
    if (previous === "executor_offline" && sessionAvailability() !== "executor_offline") {
      void refreshWorkspace();
    }
  }

  function sessionUnavailableLabel(session = selectedSession()) {
    const availability = sessionAvailability(session);
    if (availability === "missing_on_executor") return "Missing on executor";
    if (availability === "executor_offline") return "Executor offline";
    if (availability && availability !== "available") return text(availability, "Unavailable");
    return "";
  }

  function sessionEntryMetaText(session, legacy = false) {
    const updated = sessionTimestamp(session.updated_at);
    if (session.status === "ended") return `ended (${updated})`;
    if (legacy) {
      return `${sessionUnavailableLabel(session) || text(session.status, "session")} (${updated})`;
    }
    if (session.status === "terminating") return `terminating (${updated})`;
    const unavailable = sessionUnavailableLabel(session);
    return unavailable
      ? `${unavailable.toLowerCase()} (${updated})`
      : `available (${sessionActivityTimestamp(session)})`;
  }

  function sessionActivityTimestamp(session = selectedSession()) {
    const value =
      session && session.last_active_at != null
        ? session.last_active_at
        : session && session.updated_at;
    return value == null ? "activity time unavailable" : sessionTimestamp(value);
  }

  function syncControls() {
    const session = selectedSession();
    const sessionReady = Boolean(controllerState.sessionId && session);
    const taskReady = Boolean(selectedTaskId());
    const executorOffline = sessionAvailability(session) === "executor_offline";
    const taskStatus = text(controllerState.task && controllerState.task.status, "");
    const taskTerminal = taskStatus === "cancelled";
    const taskCompleted = taskStatus === "completed";
    const taskMutable = taskReady && !taskTerminal && !taskCompleted;
    const unfinished = (controllerState.task?.plan?.steps || []).some(
      (step) => step.status !== "completed" && step.status !== "skipped",
    );
    for (const button of document.querySelectorAll("[data-task-action]")) {
      const action = button.dataset.taskAction;
      if (action === "resume") button.textContent = taskStatus === "blocked" ? "Resume" : "Reopen";
      const allowed = action === "resume"
        ? taskStatus === "blocked" || taskCompleted
        : action === "delete"
          ? taskCompleted || taskTerminal
          : action === "block"
            ? taskStatus === "active"
            : action === "finish"
              ? (taskStatus === "active" || taskStatus === "blocked") && !unfinished
              : action === "cancel" && (taskStatus === "active" || taskStatus === "blocked");
      button.disabled = !taskReady || !controllerState.task || !allowed ||
        controllerState.taskMutationBusy || controllerState.workspaceLoading ||
        controllerState.todoMutationBusy || controllerState.todoDirty;
      button.title = action === "finish" && unfinished ? "Complete or skip all Plan steps first" : "";
    }
    elements.tasksFilter.disabled = controllerState.workspaceLoading || controllerState.taskMutationBusy;
    elements.tasksPrev.disabled = controllerState.workspaceLoading || controllerState.taskMutationBusy || !controllerState.taskCursorStack.length;
    elements.tasksNext.disabled = controllerState.workspaceLoading || controllerState.taskMutationBusy || !controllerState.taskNextCursor;
    elements.tasksRefresh.disabled = controllerState.workspaceLoading || controllerState.todoMutationBusy || controllerState.taskMutationBusy || controllerState.sessionTerminating;
    elements.sessionTerminate.disabled =
      controllerState.workspaceLoading || controllerState.sessionTerminating || !sessionReady || executorOffline || sessionTerminated(session);
    const otherLiveSessions = controllerState.sessions.filter(
      (item) => item.status !== "ended" && item.session_id !== session?.session_id,
    );
    elements.sessionFinishTask.disabled =
      elements.sessionTerminate.disabled || session?.status !== "active" ||
      !taskReady || !["active", "blocked"].includes(taskStatus) ||
      unfinished || controllerState.todoDirty || controllerState.todoMutationBusy ||
      otherLiveSessions.length > 0;
    elements.taskSessionStartOpen.disabled =
      !taskReady || taskStatus !== "active" || controllerState.taskMutationBusy ||
      controllerState.workspaceLoading || controllerState.sessionTerminating;
    elements.todoRefresh.disabled = controllerState.todoMutationBusy || controllerState.taskMutationBusy || !taskReady;
    elements.todoAdd.disabled = controllerState.todoMutationBusy || controllerState.taskMutationBusy || !taskMutable || controllerState.todoItems.length >= controllerState.todoLimits.todos;
    elements.todoSave.disabled = controllerState.todoMutationBusy || controllerState.taskMutationBusy || !taskMutable || !controllerState.todoDirty;
    elements.sessionAuditRefresh.disabled = controllerState.sessionAuditLoading || !sessionReady;
    for (const control of elements.sessionAuditFilterForm.querySelectorAll("input, select")) {
      control.disabled = controllerState.sessionAuditLoading || !sessionReady;
    }
    const planDisabled = controllerState.todoMutationBusy || controllerState.taskMutationBusy || !taskMutable;
    for (const control of elements.todoList.querySelectorAll("input, select, button")) {
      if (control.disabled !== planDisabled) control.disabled = planDisabled;
    }
    for (const row of elements.todoList.querySelectorAll(".todo-row")) {
      const disabled = planDisabled ? "true" : "false";
      if (row.getAttribute("aria-disabled") !== disabled) row.setAttribute("aria-disabled", disabled);
    }
  }

  function setTodoMutationBusy(busy) {
    controllerState.todoMutationBusy = busy;
    syncControls();
  }

  function setTodoDirty(dirty = true) {
    controllerState.todoDirty = dirty;
    if (dirty) elements.todoState.textContent = "Unsaved changes";
    syncControls();
  }

  function taskListText(values) {
    return Array.isArray(values) && values.length
      ? values.map((value) => `• ${text(value, "")}`).join("\n")
      : "—";
  }

  function renderTaskState() {
    document.querySelector(".tasks-workspace").classList.toggle(
      "has-selection", Boolean(selectedTaskId() || controllerState.sessionId),
    );
    updateTaskTabs();
    const task = controllerState.task;
    const progress = task && task.progress && typeof task.progress === "object" ? task.progress : {};
    const status = text(task && task.status);
    taskDetails.renderTask({
      objective: text(task && task.objective),
      status,
      summary: text(progress.summary),
      nextAction: text(progress.next_action),
      findings: taskListText(progress.findings),
      blockers: taskListText(progress.blockers),
      hasFindings: Array.isArray(progress.findings) && progress.findings.length > 0,
      hasBlockers: Array.isArray(progress.blockers) && progress.blockers.length > 0,
    });
    elements.taskState.textContent = task
      ? text(task.task_id, "task")
      : selectedTaskId()
        ? `Loading ${selectedTaskId()}`
        : "No semantic task selected";
  }

  function clearTaskResources(message = "Select a task") {
    controllerState.todoSignature = "";
    controllerState.task = null;
    controllerState.todoItems = [];
    controllerState.todoDirty = false;
    controllerState.todoGeneration += 1;
    elements.taskActionState.textContent = "";
    renderTaskState();
    renderTodos();
    elements.taskState.textContent = message;
    elements.todoState.textContent = message;
  }

  function resetWorkspace() {
    controllerState.selectionRevision += 1;
    controllerState.workspaceGeneration += 1;
    controllerState.inventorySignature = "";
    controllerState.todoSignature = "";
    controllerState.workspaceRefreshing = false;
    controllerState.taskId = "";
    controllerState.sessionId = "";
    controllerState.tasks = [];
    controllerState.selectedTaskRow = null;
    controllerState.selectedLegacySession = null;
    controllerState.taskCursor = "";
    controllerState.taskCursorStack = [];
    controllerState.taskNextCursor = null;
    controllerState.taskTotal = 0;
    controllerState.taskOverallCount = 0;
    controllerState.taskActiveTotal = 0;
    controllerState.taskPageCount = 0;
    elements.tasksFilter.value = controllerState.taskFilter;
    controllerState.unattachedSessions = [];
    controllerState.sessions = [];
    controllerState.workspaceLoading = false;
    controllerState.sessionTerminating = false;
    controllerState.taskMutationBusy = false;
    taskLists.renderTasks({
      tasks: [], unattached: [], selectedTaskId: "", selectedSessionId: "",
      emptyMessage: "Loading tasks…",
    });
    taskLists.renderSessions({
      sessions: [], selectedSessionId: "",
      emptyMessage: "Select a task to inspect its execution sessions.",
    });
    clearTaskResources("Select a task");
    resetSessionAuditWorkspace("Select a session");
    elements.tasksState.textContent = "Not loaded";
    renderSessionDetail();
    syncControls();
  }

  function sessionOptionLabel(session) {
    const sessionId = text(session && session.session_id, "");
    const label = text(session && session.label, "");
    const workdir = text(session && session.workdir, "");
    return `${label || workdir || "session"} (${sessionId})`;
  }

  function renderSessionDetail() {
    const session = selectedSession();
    onSessionChange(session && { ...session, availability: sessionAvailability(session) });
    document.querySelector(".task-sessions-main").classList.toggle("has-session", Boolean(session));
    if (!session) {
      taskDetails.renderSession();
      syncControls();
      return;
    }
    const finalStatus = text(session.status, "");
    const availability = sessionAvailability(session);
    let state = "available";
    if (finalStatus === "ended") state = "ended";
    else if (finalStatus === "terminating" || (availability && availability !== "available")) state = "warning";
    const name = executorName(session.executor_id);
    taskDetails.renderSession({
      executor: name,
      title: text(session.label, text(session.session_id, "session")),
      state,
      status: finalStatus === "ended"
        ? "Ended (history retained)"
        : finalStatus === "terminating"
          ? "Terminating (awaiting Executor absence)"
          : availability === "missing_on_executor"
            ? "Unavailable (missing on executor)"
            : availability === "executor_offline"
              ? "Unavailable (executor offline)"
              : `Available (${sessionActivityTimestamp(session)})`,
      taskCreated: session.task_created ? `Created Task ${session.task_id} automatically` : "",
      sessionId: text(session.session_id),
      executorId: text(session.executor_id),
      workdir: text(session.workdir),
      created: sessionTimestamp(session.created_at),
      updated: sessionTimestamp(session.updated_at),
    });
    syncControls();
  }

  function sessionRow(session, legacy = false) {
    const name = executorName(session.executor_id);
    return {
      id: text(session.session_id, ""),
      title: legacy
        ? sessionOptionLabel(session)
        : text(session.label, text(session.workdir, "Session")),
      meta: sessionEntryMetaText(session, legacy),
      executor: name,
      terminated: !legacy && sessionTerminated(session),
      tooltip: `${sessionOptionLabel(session)} — ${name}`,
    };
  }

  function renderSessions(sessions) {
    controllerState.sessions = Array.isArray(sessions) ? sessions : [];
    taskLists.renderSessions({
      sessions: controllerState.sessions.map((session) => sessionRow(session)),
      selectedSessionId: controllerState.sessionId,
      emptyMessage: !controllerState.sessions.length && selectedTaskId()
        ? "This task has no retained execution sessions."
        : controllerState.sessionId
          ? "Unattached session selected from the task list."
          : "No retained execution session selected.",
    });
    renderSessionDetail();
    syncControls();
  }

  function taskOptionLabel(task) {
    return text(
      task && task.label,
      text(task && task.objective, text(task && task.task_id, "task")),
    );
  }

  function taskMeta(task) {
    const sessions = Array.isArray(task && task.sessions) ? task.sessions : [];
    const count = Number.isInteger(task?.session_count) ? task.session_count : sessions.length;
    return `${count} session${count === 1 ? "" : "s"} (updated ${sessionTimestamp(task && task.updated_at)})`;
  }

  function renderTaskList() {
    document.getElementById("task-count-active").textContent = String(controllerState.taskActiveTotal);
    document.getElementById("task-count-total").textContent = String(controllerState.taskOverallCount);
    elements.tasksPageState.textContent = `Page ${controllerState.taskCursorStack.length + 1} (${controllerState.taskPageCount} of ${controllerState.taskTotal})`;
    document.getElementById("tasks-panel").dataset.taskFilter = controllerState.taskFilter;
    taskLists.renderTasks({
      tasks: controllerState.tasks.map((task) => ({
        id: text(task.task_id, ""),
        title: taskOptionLabel(task),
        status: text(task.status, "unknown"),
        meta: taskMeta(task),
      })),
      unattached: controllerState.unattachedSessions.map((session) => sessionRow(session, true)),
      selectedTaskId: controllerState.taskId,
      selectedSessionId: controllerState.sessionId,
      emptyMessage: "Nothing matches this filter.",
    });
  }

  function syncDeepLink() {
    if (document.body.dataset.activeView !== "tasks") return;
    const url = new URL(globalThis.location.href);
    if (selectedTaskId()) url.searchParams.set("task_id", selectedTaskId());
    else url.searchParams.delete("task_id");
    if (controllerState.sessionId) {
      url.searchParams.set("session_id", controllerState.sessionId);
    } else {
      url.searchParams.delete("session_id");
    }
    globalThis.history.replaceState(
      globalThis.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }

  function applyInventory(payload) {
    const signature = JSON.stringify([payload?.tasks, payload?.unattached_sessions, payload?.selected_task,
      payload?.selected_session, payload?.total, payload?.all_count, payload?.active_count,
      payload?.next_cursor, controllerState.taskFilter, controllerState.taskCursor,
      selectedTaskId(), controllerState.sessionId]);
    if (signature === controllerState.inventorySignature) return;
    controllerState.inventorySignature = signature;
    controllerState.tasks = Array.isArray(payload?.tasks) ? payload.tasks : [];
    // Keep the selected detail independent of the filtered list. Never inject
    // an out-of-filter Task or legacy Session into the visible page.
    controllerState.selectedTaskRow = payload?.selected_task || null;
    controllerState.selectedLegacySession = payload?.selected_session || null;
    onInventory(controllerState.tasks);
    controllerState.unattachedSessions = Array.isArray(payload?.unattached_sessions)
      ? payload.unattached_sessions : [];
    controllerState.taskTotal = Number(payload?.total || 0);
    controllerState.taskPageCount = Number(payload?.count || 0);
    if (Number.isInteger(payload?.active_count)) controllerState.taskActiveTotal = payload.active_count;
    if (Number.isInteger(payload?.all_count)) controllerState.taskOverallCount = payload.all_count;
    controllerState.taskNextCursor = payload?.next_cursor || null;

    let selectedTask = selectedTaskSummary();
    if (!selectedTask && !controllerState.taskId && controllerState.sessionId) {
      selectedTask =
        controllerState.tasks.find(
          (task) =>
            Array.isArray(task.sessions) &&
            task.sessions.some(
              (session) => session.session_id === controllerState.sessionId,
            ),
        ) || null;
      controllerState.taskId = selectedTask
        ? text(selectedTask.task_id, "")
        : "";
    }
    if (!selectedTask && controllerState.taskId) {
      controllerState.taskId = "";
      controllerState.sessionId = "";
    }

    if (selectedTask) {
      controllerState.sessions = Array.isArray(selectedTask.sessions)
        ? selectedTask.sessions
        : [];
      if (
        !controllerState.sessions.some(
          (session) => session.session_id === controllerState.sessionId,
        )
      ) {
        controllerState.sessionId = "";
      }
    } else {
      controllerState.taskId = "";
      controllerState.sessions = [];
      if (
        !controllerState.unattachedSessions.some(
          (session) => session.session_id === controllerState.sessionId,
        )
      ) {
        controllerState.sessionId = "";
      }
    }

    renderTaskList();
    renderSessions(controllerState.sessions);
    if (controllerState.sessionId) showTab("sessions");
    updateTaskTabs();
    syncDeepLink();
  }

  async function selectTask(next) {
    if (
      !next ||
      controllerState.todoMutationBusy || controllerState.taskMutationBusy ||
      controllerState.workspaceLoading
    ) return;
    if (next === controllerState.taskId && !controllerState.sessionId) {
      onNavigate("task");
      return;
    }
    if (
      controllerState.todoDirty &&
      !globalThis.confirm(`Discard unsaved changes in ${selectedTaskId()}?`)
    ) return;

    controllerState.selectionRevision += 1;
    const sameTask = next === controllerState.taskId;
    controllerState.taskId = next;
    if (!sameTask) controllerState.selectedTaskRow = null;
    controllerState.selectedLegacySession = null;
    showTab("progress");
    const task = selectedTaskSummary();
    controllerState.sessions = Array.isArray(task && task.sessions)
      ? task.sessions
      : [];
    controllerState.sessionId = "";
    controllerState.todoDirty = false;
    if (!sameTask) clearTaskResources("Loading selected task");
    resetSessionAuditWorkspace(
      controllerState.sessionId
        ? "Loading selected session"
        : "No execution session selected",
    );
    renderTaskList();
    renderSessions(controllerState.sessions);
    syncDeepLink();
    onNavigate("task");
    await refreshWorkspace();
  }

  async function selectUnattachedSession(next) {
    if (
      !next ||
      controllerState.todoMutationBusy || controllerState.taskMutationBusy ||
      controllerState.workspaceLoading
    ) return;
    if (
      controllerState.todoDirty &&
      !globalThis.confirm(`Discard unsaved changes in ${selectedTaskId()}?`)
    ) return;

    controllerState.selectionRevision += 1;
    controllerState.taskId = "";
    controllerState.selectedTaskRow = null;
    controllerState.sessionId = next;
    controllerState.selectedLegacySession = controllerState.unattachedSessions.find(
      (session) => session.session_id === next,
    ) || null;
    showTab("sessions");
    controllerState.sessions = [];
    clearTaskResources(
      "No semantic task attached to this execution session",
    );
    resetSessionAuditWorkspace("Loading selected session");
    renderTaskList();
    renderSessions(controllerState.sessions);
    syncDeepLink();
    onNavigate("session");
    await refreshWorkspace();
  }

  async function selectSession(next) {
    if (
      !next ||
      next === controllerState.sessionId ||
      controllerState.workspaceLoading
    ) return;
    controllerState.selectionRevision += 1;
    controllerState.sessionId = next;
    showTab("sessions");
    controllerState.sessionAuditSelectedId = "";
    resetSessionAuditWorkspace("Loading selected session");
    renderTaskList();
    renderSessions(controllerState.sessions);
    syncDeepLink();
    onNavigate("session");
    await refreshSessionAudit();
  }

  async function refreshWorkspace({ background = false } = {}) {
    if (controllerState.workspaceLoading || (background && (controllerState.workspaceRefreshing || controllerState.taskMutationBusy))) return null;
    const revision = controllerState.selectionRevision;
    const generation = ++controllerState.workspaceGeneration;
    controllerState.workspaceRefreshing = true;
    if (!background) {
      controllerState.workspaceLoading = true;
      syncControls();
      elements.tasksState.textContent = "Loading tasks";
    }
    const previousTask = selectedTaskId();
    const previousSession = controllerState.sessionId;
    try {
      const query = new URLSearchParams({ status: controllerState.taskFilter });
      if (controllerState.taskCursor) query.set("cursor", controllerState.taskCursor);
      const payload = await request(`/tasks?${query}`);
      if (generation !== controllerState.workspaceGeneration || revision !== controllerState.selectionRevision) return null;
      const lookup = selectedTaskId()
        ? new URLSearchParams({ task_id: selectedTaskId() })
        : controllerState.sessionId
          ? new URLSearchParams({ session_id: controllerState.sessionId }) : null;
      if (lookup) {
        try {
          const detail = await request(`/tasks/detail?${lookup}`);
          if (generation !== controllerState.workspaceGeneration || revision !== controllerState.selectionRevision) return null;
          if (detail.task && !selectedTaskId()) controllerState.taskId = detail.task.task_id;
          payload.selected_task = detail.task;
          payload.selected_session = detail.session;
        } catch (error) {
          // A Task/Session may have been deleted concurrently; drop only the stale selection.
          if (error?.status !== 404) throw error;
          controllerState.taskId = "";
          controllerState.sessionId = "";
        }
      }
      applyInventory(payload);
      elements.tasksState.textContent = `Updated ${new Date().toLocaleTimeString()}`;

      if (!selectedTaskId() && (!background || previousTask)) {
        clearTaskResources(
          previousTask
            ? "Selected task is no longer retained"
            : controllerState.sessionId
              ? "No semantic task attached to this execution session"
              : "Select a task",
        );
      } else if (
        !controllerState.todoDirty &&
        !controllerState.todoMutationBusy
      ) {
        await refreshTodos({ force: !background, background });
      }

      if (generation !== controllerState.workspaceGeneration || revision !== controllerState.selectionRevision) return null;
      if (controllerState.sessionId) {
        await refreshSessionAudit({ background });
      } else if (!background || previousSession) {
        resetSessionAuditWorkspace("No execution session selected");
      }
      return payload;
    } catch (error) {
      if (!background) elements.tasksState.textContent = error instanceof Error ? error.message : String(error);
      if (error?.authenticationRequired) throw error;
      return null;
    } finally {
      if (generation === controllerState.workspaceGeneration) {
        controllerState.workspaceRefreshing = false;
        controllerState.workspaceLoading = false;
        syncControls();
      }
    }
  }

  async function startSessionInTask(event) {
    event.preventDefault();
    const taskId = selectedTaskId();
    if (!taskId || controllerState.workspaceLoading || controllerState.sessionTerminating) return;
    const executorId = elements.taskSessionExecutor.value;
    if (!executorId || controllerState.executorTargets.get(executorId)?.status !== "online") return;
    controllerState.sessionTerminating = true;
    syncControls();
    try {
      const result = await request("/sessions/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: taskId, executor_id: executorId,
          workdir: elements.taskSessionWorkdir.value.trim() || null,
        }),
      });
      elements.taskSessionStartForm.hidden = true;
      controllerState.sessionId = result.session.session_id;
      showTab("sessions");
      await refreshWorkspace();
      onNavigate("session");
      elements.tasksState.textContent = "Session created in existing Task";
    } catch (error) {
      elements.tasksState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      controllerState.sessionTerminating = false;
      syncControls();
    }
  }

  async function terminateSelectedSession({ finishTask = false } = {}) {
    const session = selectedSession();
    if (
      !session ||
      sessionTerminated(session) ||
      controllerState.sessionTerminating
    ) return;
    const taskId = selectedTaskId();
    if (finishTask && (!taskId || elements.sessionFinishTask.disabled)) return;
    const label = sessionOptionLabel(session);
    if (
      !globalThis.confirm(
        finishTask
          ? `End ${label} and complete Task ${taskId}? This will stop Session work and finish the Task if no other Sessions are running.`
          : `Immediately terminate ${label}? Any later model tool call for this session will be told to stop all work.`,
      )
    ) return;
    controllerState.sessionTerminating = true;
    syncControls();
    elements.tasksState.textContent =
      `Requesting immediate termination for ${session.session_id}`;
    try {
      await request("/sessions/terminate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: session.session_id }),
      });
      if (finishTask) {
        await request("/tasks/finish", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task_id: taskId, require_no_live_sessions: true }),
        });
      }
      await refreshWorkspace();
      if (finishTask) elements.tasksState.textContent = "Session ended; Task completed";
    } catch (error) {
      // A failed Task transition must not conceal a successful Session ending.
      await refreshWorkspace();
      elements.tasksState.textContent =
        error instanceof Error ? error.message : String(error);
    } finally {
      controllerState.sessionTerminating = false;
      syncControls();
    }
  }

  async function mutateSelectedTask(action) {
    const taskId = selectedTaskId();
    if (!taskId || !controllerState.task || controllerState.taskMutationBusy ||
        controllerState.workspaceLoading || controllerState.todoMutationBusy || controllerState.todoDirty) return;
    const button = document.querySelector(`[data-task-action="${action}"]`);
    if (!button || button.disabled) return;
    if (action === "delete" && !globalThis.confirm(
      `Permanently delete Task ${taskId}? Its document cannot be restored; retained Sessions will become unattached.`,
    )) return;
    if (action === "cancel" && !globalThis.confirm(
      `Cancel Task ${taskId}? It cannot be reopened, and running Sessions will not be terminated.`,
    )) return;
    controllerState.taskMutationBusy = true;
    controllerState.selectionRevision += 1; // Invalidate any in-flight inventory refresh.
    elements.taskActionState.textContent = `Applying ${action}…`;
    syncControls();
    try {
      const result = await request(`/tasks/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: taskId }),
      });
      if (action === "delete") {
        controllerState.selectionRevision += 1;
        controllerState.taskId = "";
        controllerState.sessionId = "";
        controllerState.selectedTaskRow = null;
        controllerState.selectedLegacySession = null;
        clearTaskResources("Task deleted");
        controllerState.tasks = controllerState.tasks.filter((task) => task.task_id !== taskId);
        controllerState.sessions = [];
        renderTaskList();
        renderSessions([]);
        showTab("progress");
        syncDeepLink();
        onNavigate("list", { replace: true });
        elements.taskList.querySelector(".task-entry")?.focus({ preventScroll: true });
      } else if (result.task && taskId === selectedTaskId()) {
        controllerState.task = result.task;
        renderTaskState();
      }
      await refreshWorkspace();
      const completed = { finish: "completed", resume: "reopened", block: "blocked", cancel: "cancelled" };
      elements.taskActionState.textContent = action === "delete" ? "" : `Task ${completed[action]}`;
      if (action === "delete") elements.tasksState.textContent = `Deleted ${taskId}`;
    } catch (error) {
      elements.taskActionState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      controllerState.taskMutationBusy = false;
      syncControls();
    }
  }

  function planStepClosed(item) {
    return item.status === "completed" || item.status === "skipped";
  }

  function visibleTodos() {
    const filter = elements.todoFilter.value;
    return controllerState.todoItems
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => {
        if (filter === "completed") return planStepClosed(item);
        if (filter === "open") return !planStepClosed(item);
        return true;
      });
  }

  function renderTodos() {
    const completed = controllerState.todoItems.filter((item) => item.status === "completed").length;
    const skipped = controllerState.todoItems.filter((item) => item.status === "skipped").length;
    const visible = visibleTodos();
    planView.render({
      rows: visible.map(({ item, index }) => ({ ...item, index })),
      counts: {
        total: controllerState.todoItems.length,
        open: controllerState.todoItems.length - completed - skipped,
        completed,
        skipped,
      },
      contentMaxLength: controllerState.todoLimits.content_bytes,
      emptyMessage: controllerState.todoItems.length
        ? "No plan steps match this filter."
        : "No plan steps in this task.",
    });
    syncControls();
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
    if (controllerState.todoMutationBusy || !selectedTaskId() || controllerState.todoItems.length >= controllerState.todoLimits.todos) return;
    const item = { id: newTodoId(), content: "", status: "pending", priority: "medium" };
    controllerState.todoItems.push(item);
    elements.todoFilter.value = "all";
    setTodoDirty();
    renderTodos();
    const row = elements.todoList.querySelector(`[data-todo-id="${CSS.escape(item.id)}"]`);
    row?.querySelector("input")?.focus();
  }

  function todoQuery() {
    return "/todos?" + new URLSearchParams({ task_id: selectedTaskId() }).toString();
  }

  function applyTodoPayload(payload, requestedTask, { background = false } = {}) {
    const signature = JSON.stringify([payload.task, payload.todos, payload.limits]);
    if (background && signature === controllerState.todoSignature) return;
    controllerState.todoSignature = signature;
    controllerState.task = payload.task && typeof payload.task === "object" ? payload.task : null;
    controllerState.todoItems = Array.isArray(payload.todos)
      ? payload.todos.map((item) => ({
          id: text(item.id, ""),
          content: text(item.content, ""),
          status: text(item.status, "pending"),
          priority: text(item.priority, "medium"),
        }))
      : [];
    if (payload.limits && typeof payload.limits === "object") {
      controllerState.todoLimits = { ...controllerState.todoLimits, ...payload.limits };
    }
    controllerState.todoDirty = false;
    renderTaskState();
    renderTodos();
    elements.todoState.textContent = "";
  }

  async function refreshTodos({ force = false, background = false } = {}) {
    const requestedTask = selectedTaskId();
    if (!requestedTask || (!force && (controllerState.todoDirty || controllerState.todoMutationBusy))) return null;
    const generation = ++controllerState.todoGeneration;
    if (!background) {
      elements.todoState.textContent = "Loading…";
      syncControls();
    }
    try {
      const payload = await request(todoQuery());
      if (generation !== controllerState.todoGeneration || requestedTask !== selectedTaskId() || (!force && (controllerState.todoDirty || controllerState.todoMutationBusy))) return null;
      applyTodoPayload(payload, requestedTask, { background });
      return payload;
    } catch (error) {
      if (generation !== controllerState.todoGeneration || requestedTask !== selectedTaskId()) return null;
      if (!background) elements.todoState.textContent = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      if (generation === controllerState.todoGeneration) syncControls();
    }
  }

  async function saveTodos() {
    const requestedTask = selectedTaskId();
    if (!controllerState.todoDirty || controllerState.todoMutationBusy || !requestedTask) return;
    const generation = ++controllerState.todoGeneration;
    const todos = controllerState.todoItems.map((item) => ({ ...item }));
    setTodoMutationBusy(true);
    elements.todoState.textContent = "Saving…";
    try {
      const payload = await request("/todos", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: requestedTask,
          todos,
        }),
      });
      if (generation !== controllerState.todoGeneration || requestedTask !== selectedTaskId()) return;
      controllerState.todoItems = Array.isArray(payload.todos) ? payload.todos.map((item) => ({ ...item })) : [];
      controllerState.task = payload.task && typeof payload.task === "object" ? payload.task : controllerState.task;
      controllerState.todoDirty = false;
      renderTaskState();
      renderTodos();
      elements.todoState.textContent = "Saved";
    } catch (error) {
      if (generation !== controllerState.todoGeneration || requestedTask !== selectedTaskId()) return;
      elements.todoState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      if (requestedTask === selectedTaskId()) setTodoMutationBusy(false);
    }
  }

  function clearSessionAuditDetail(message = "Select a session Audit record.") {
    controllerState.sessionAuditDetailGeneration += 1;
    elements.sessionAuditDetailTitle.textContent = "No record selected";
    elements.sessionAuditDetailMeta.textContent = "";
    renderAuditDetailMessage(elements.sessionAuditDetailBody, message);
  }

  function resetSessionAuditWorkspace(message = "Select a session") {
    controllerState.sessionAuditRefreshing = false;
    controllerState.sessionAuditEntries = [];
    controllerState.sessionAuditSelectedId = "";
    controllerState.sessionAuditLoading = false;
    controllerState.sessionAuditGeneration += 1;
    auditListView.render([], "", message);
    elements.sessionAuditSummary.textContent = "0 entries";
    elements.sessionAuditState.textContent = message;
    clearSessionAuditDetail();
    syncControls();
  }

  function renderSessionAuditList() {
    const entries = controllerState.sessionAuditEntries;
    auditListView.render(entries, controllerState.sessionAuditSelectedId,
      controllerState.sessionId
        ? `No Audit records match for ${controllerState.sessionId}.`
        : "Select a session to load its Audit records.");
    if (!entries.length) {
      clearSessionAuditDetail("No matching session Audit record is available.");
    }
  }

  function renderSessionAuditDetailEntry(entry) {
    elements.sessionAuditDetailTitle.textContent = auditEntryTitle(entry);
    elements.sessionAuditDetailMeta.textContent = auditTimestamp(entry.ts);
    renderAuditDetailInto(entry, elements.sessionAuditDetailBody);
  }

  function applySessionAuditPayload(payload, background) {
    controllerState.sessionAuditEntries = Array.isArray(payload.entries)
      ? payload.entries.map((entry) => ({ ...entry }))
      : [];
    const selectedId = controllerState.sessionAuditSelectedId;
    controllerState.sessionAuditSelectedId = controllerState.sessionAuditEntries.some((entry) => entry.id === selectedId)
      ? selectedId
      : text(controllerState.sessionAuditEntries[0] && controllerState.sessionAuditEntries[0].id, "");
    if (selectedId !== controllerState.sessionAuditSelectedId) controllerState.sessionAuditDetailGeneration += 1;
    const total = Number.isInteger(payload.total_matched) ? payload.total_matched : controllerState.sessionAuditEntries.length;
    const shown = controllerState.sessionAuditEntries.length;
    elements.sessionAuditSummary.textContent = total > shown ? `${shown} of ${total} records` : `${shown} records`;
    elements.sessionAuditState.textContent = "";
    renderSessionAuditList();
    const selected = payload && payload.entry && typeof payload.entry === "object" ? payload.entry : null;
    if (selected && selected.id === controllerState.sessionAuditSelectedId) {
      renderSessionAuditDetailEntry(selected);
    } else if (payload && payload.entry_error && !background) {
      elements.sessionAuditDetailMeta.textContent = "Details unavailable";
      renderAuditDetailMessage(elements.sessionAuditDetailBody, text(payload.entry_error));
    } else if (controllerState.sessionAuditSelectedId && (!background || selectedId !== controllerState.sessionAuditSelectedId)) {
      void loadSessionAuditDetail(controllerState.sessionAuditSelectedId);
    }
  }

  function sessionAuditQueryPath() {
    const params = new URLSearchParams({
      scope: "session",
      session: controllerState.sessionId,
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
    if (!entryId || !controllerState.sessionId) {
      clearSessionAuditDetail();
      return null;
    }
    const generation = ++controllerState.sessionAuditDetailGeneration;
    const requestedSession = controllerState.sessionId;
    elements.sessionAuditDetailTitle.textContent = auditEntryTitle(
      controllerState.sessionAuditEntries.find((entry) => entry.id === entryId) || {},
    );
    elements.sessionAuditDetailMeta.textContent = "Loading details";
    renderAuditDetailMessage(elements.sessionAuditDetailBody, `Loading ${requestedSession}:${entryId}`);
    try {
      const params = new URLSearchParams({
        scope: "session",
        session: requestedSession,
        id: entryId,
      });
      const payload = await request(`/audit/detail?${params.toString()}`);
      if (
        generation !== controllerState.sessionAuditDetailGeneration ||
        requestedSession !== controllerState.sessionId ||
        entryId !== controllerState.sessionAuditSelectedId
      ) return null;
      const entry = payload && payload.entry && typeof payload.entry === "object" ? payload.entry : null;
      if (!entry) throw new Error("Session Audit detail response was malformed");
      renderSessionAuditDetailEntry(entry);
      return entry;
    } catch (error) {
      if (
        generation !== controllerState.sessionAuditDetailGeneration ||
        requestedSession !== controllerState.sessionId ||
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

  async function refreshSessionAudit({ background = false } = {}) {
    if (controllerState.sessionAuditLoading || !controllerState.sessionId || (background && controllerState.sessionAuditRefreshing)) return null;
    const generation = ++controllerState.sessionAuditGeneration;
    const requestedSession = controllerState.sessionId;
    controllerState.sessionAuditRefreshing = true;
    if (!background) {
      controllerState.sessionAuditLoading = true;
      syncControls();
      elements.sessionAuditState.textContent = "Loading…";
    }
    try {
      const payload = await request(sessionAuditQueryPath());
      if (
        generation !== controllerState.sessionAuditGeneration ||
        requestedSession !== controllerState.sessionId
      ) return null;
      applySessionAuditPayload(payload, background);
      return payload;
    } catch (error) {
      if (
        generation !== controllerState.sessionAuditGeneration ||
        requestedSession !== controllerState.sessionId
      ) return null;
      if (error.status === 403) {
        resetSessionAuditWorkspace("Audit access denied");
      } else if (!background) {
        controllerState.sessionAuditEntries = [];
        controllerState.sessionAuditSelectedId = "";
        renderSessionAuditList();
      }
      elements.sessionAuditState.textContent = error instanceof Error ? error.message : String(error);
      return null;
    } finally {
      if (generation === controllerState.sessionAuditGeneration) {
        controllerState.sessionAuditRefreshing = false;
        controllerState.sessionAuditLoading = false;
        syncControls();
      }
    }
  }

  function invalidate() {
    controllerState.selectionRevision += 1;
    controllerState.workspaceGeneration += 1;
    controllerState.sessionAuditGeneration += 1;
    controllerState.sessionAuditRefreshing = false;
    controllerState.workspaceRefreshing = false;
    controllerState.todoGeneration += 1;
    controllerState.todoDirty = false;
  }

  function bind() {
    const tabs = Array.from(document.querySelectorAll("[data-task-tab]"));
    for (const [index, button] of tabs.entries()) {
      button.addEventListener("click", () => showTab(button.dataset.taskTab));
      button.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const direction = event.key === "ArrowRight" ? 1 : -1;
        for (let offset = 1; offset <= tabs.length; offset += 1) {
          const next = tabs[(index + direction * offset + tabs.length * 2) % tabs.length];
          if (next.disabled) continue;
          showTab(next.dataset.taskTab);
          next.focus();
          break;
        }
      });
    }
    showTab(activeTab);
    updateTaskTabs();
    elements.tasksFilter.addEventListener("change", () => {
      if (controllerState.todoDirty && !globalThis.confirm("Discard unsaved Plan changes?")) {
        elements.tasksFilter.value = controllerState.taskFilter;
        return;
      }
      controllerState.todoDirty = false;
      controllerState.taskFilter = elements.tasksFilter.value;
      controllerState.taskCursor = "";
      controllerState.taskCursorStack = [];
      controllerState.selectionRevision += 1;
      void refreshWorkspace();
    });
    elements.tasksNext.addEventListener("click", () => {
      if (!controllerState.taskNextCursor || controllerState.workspaceLoading) return;
      controllerState.taskCursorStack.push(controllerState.taskCursor);
      controllerState.taskCursor = controllerState.taskNextCursor;
      controllerState.selectionRevision += 1;
      void refreshWorkspace();
    });
    elements.tasksPrev.addEventListener("click", () => {
      if (!controllerState.taskCursorStack.length || controllerState.workspaceLoading) return;
      controllerState.taskCursor = controllerState.taskCursorStack.pop();
      controllerState.selectionRevision += 1;
      void refreshWorkspace();
    });
    elements.tasksRefresh.addEventListener("click", () => {
      if (controllerState.workspaceLoading || controllerState.sessionTerminating) return;
      void refreshWorkspace();
    });
    for (const button of document.querySelectorAll("[data-task-action]")) {
      button.addEventListener("click", () => void mutateSelectedTask(button.dataset.taskAction));
    }
    elements.sessionTerminate.addEventListener(
      "click",
      () => void terminateSelectedSession(),
    );
    elements.sessionFinishTask.addEventListener(
      "click",
      () => void terminateSelectedSession({ finishTask: true }),
    );
    elements.taskSessionStartOpen.addEventListener("click", () => {
      elements.taskSessionStartForm.hidden = !elements.taskSessionStartForm.hidden;
      if (!elements.taskSessionStartForm.hidden) elements.taskSessionExecutor.focus();
    });
    elements.taskSessionStartCancel.addEventListener("click", () => {
      elements.taskSessionStartForm.hidden = true;
    });
    elements.taskSessionStartForm.addEventListener("submit", (event) => {
      void startSessionInTask(event);
    });
    elements.sessionAuditFilterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void refreshSessionAudit();
    });
    for (const control of [
      elements.sessionAuditOperation,
      elements.sessionAuditSort,
      elements.sessionAuditLimit,
    ]) {
      control.addEventListener("change", () => void refreshSessionAudit());
    }

    elements.todoFilter.addEventListener("change", renderTodos);
    elements.todoAdd.addEventListener("click", addTodo);
    elements.todoSave.addEventListener("click", () => void saveTodos());
    elements.todoRefresh.addEventListener("click", () => {
      if (controllerState.todoMutationBusy || controllerState.taskMutationBusy || !selectedTaskId()) return;
      if (
        controllerState.todoDirty &&
        !globalThis.confirm(`Discard unsaved changes in ${selectedTaskId()}?`)
      ) return;
      controllerState.todoDirty = false;
      void refreshTodos({ force: true });
    });
  }

  return {
    bind,
    select: selectTask,
    invalidate,
    renderExecutors,
    refresh: refreshWorkspace,
    reset: resetWorkspace,
  };
}
