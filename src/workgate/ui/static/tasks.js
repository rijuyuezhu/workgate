export function createTasksController({
  elements,
  request,
  text,
  auditEntryButton,
  auditEntryTitle,
  auditTimestamp,
  renderAuditDetailInto,
  renderAuditDetailMessage,
  onInventory = () => {},
  onNavigate = () => {},
  initialTaskId = "",
  initialSessionId = "",
}) {
  const controllerState = {
    taskId: text(initialTaskId, ""),
    sessionId: text(initialSessionId, ""),
    tasks: [],
    unattachedSessions: [],
    sessions: [],
    executorStates: new Map(),
    workspaceLoading: false,
    sessionTerminating: false,
    task: null,
    todoItems: [],
    todoGeneration: 0,
    todoMutationBusy: false,
    todoDirty: false,
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
  };
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
    return controllerState.tasks.find((task) => task.task_id === controllerState.taskId) || null;
  }

  function selectedSession() {
    return (
      controllerState.sessions.find(
        (session) => session.session_id === controllerState.sessionId,
      ) ||
      controllerState.unattachedSessions.find(
        (session) => session.session_id === controllerState.sessionId,
      ) ||
      null
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
    if (session && controllerState.executorStates.get(session.executor_id) !== "online") {
      return "executor_offline";
    }
    return text(session && session.availability, "");
  }

  function renderExecutors(targets) {
    const previous = sessionAvailability();
    controllerState.executorStates = new Map(
      targets.map((item) => [item.executor_id, item.status]),
    );
    renderSessionDetail();
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
    elements.tasksRefresh.disabled = controllerState.workspaceLoading || controllerState.todoMutationBusy || controllerState.sessionTerminating;
    elements.sessionTerminate.disabled =
      controllerState.workspaceLoading || controllerState.sessionTerminating || !sessionReady || executorOffline || sessionTerminated(session);
    elements.todoRefresh.disabled = controllerState.todoMutationBusy || !taskReady;
    elements.todoAdd.disabled = controllerState.todoMutationBusy || !taskMutable || controllerState.todoItems.length >= controllerState.todoLimits.todos;
    elements.todoSave.disabled = controllerState.todoMutationBusy || !taskMutable || !controllerState.todoDirty;
    elements.sessionAuditRefresh.disabled = controllerState.sessionAuditLoading || !sessionReady;
    for (const control of elements.sessionAuditFilterForm.querySelectorAll("input, select")) {
      control.disabled = controllerState.sessionAuditLoading || !sessionReady;
    }
    for (const control of elements.todoList.querySelectorAll("input, select, button")) {
      control.disabled = controllerState.todoMutationBusy || !taskMutable;
    }
    for (const row of elements.todoList.querySelectorAll(".todo-row")) {
      row.setAttribute("aria-disabled", controllerState.todoMutationBusy || !taskMutable ? "true" : "false");
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
    elements.taskStatus.textContent = status;
    elements.taskStatus.dataset.status = ["active", "completed", "cancelled", "blocked"].includes(status)
      ? status : "other";
    elements.taskObjective.textContent = text(task && task.objective);
    elements.taskSummary.textContent = text(progress.summary);
    elements.taskNextAction.textContent = text(progress.next_action);
    elements.taskFindings.textContent = taskListText(progress.findings);
    elements.taskBlockers.textContent = taskListText(progress.blockers);
    elements.taskFindings.closest(".task-progress-findings").hidden = !Array.isArray(progress.findings) || !progress.findings.length;
    elements.taskBlockers.closest(".task-progress-blockers").hidden = !Array.isArray(progress.blockers) || !progress.blockers.length;
    elements.taskState.textContent = task
      ? text(task.task_id, "task")
      : selectedTaskId()
        ? `Loading ${selectedTaskId()}`
        : "No semantic task selected";
  }

  function clearTaskResources(message = "Select a task") {
    controllerState.task = null;
    controllerState.todoItems = [];
    controllerState.todoDirty = false;
    controllerState.todoGeneration += 1;
    renderTaskState();
    renderTodos();
    elements.taskState.textContent = message;
    elements.todoState.textContent = message;
  }

  function resetWorkspace() {
    controllerState.taskId = "";
    controllerState.sessionId = "";
    controllerState.tasks = [];
    controllerState.unattachedSessions = [];
    controllerState.sessions = [];
    controllerState.workspaceLoading = false;
    controllerState.sessionTerminating = false;
    elements.taskList.replaceChildren();
    const taskEmpty = document.createElement("div");
    taskEmpty.className = "empty-state";
    taskEmpty.textContent = "Loading tasks…";
    elements.taskList.append(taskEmpty);
    elements.sessionList.replaceChildren();
    const sessionEmpty = document.createElement("div");
    sessionEmpty.className = "empty-state";
    sessionEmpty.textContent = "Select a task to inspect its execution sessions.";
    elements.sessionList.append(sessionEmpty);
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
    return `${label || workdir || "session"} · ${sessionId}`;
  }

  function renderSessionDetail() {
    const session = selectedSession();
    document.querySelector(".task-sessions-main").classList.toggle("has-session", Boolean(session));
    if (!session) {
      elements.sessionDetailStatus.dataset.state = "other";
      elements.sessionDetailTitle.textContent = "No session selected";
      elements.sessionDetailStatus.textContent = "Select a retained execution session to inspect it";
      elements.sessionDetailId.textContent = "—";
      elements.sessionDetailExecutor.textContent = "—";
      elements.sessionDetailWorkdir.textContent = "—";
      elements.sessionDetailCreated.textContent = "—";
      elements.sessionDetailUpdated.textContent = "—";
      syncControls();
      return;
    }
    const finalStatus = text(session.status, "");
    const availability = sessionAvailability(session);
    let state = "available";
    if (finalStatus === "ended") state = "ended";
    else if (finalStatus === "terminating" || (availability && availability !== "available")) state = "warning";
    elements.sessionDetailStatus.dataset.state = state;
    elements.sessionDetailTitle.textContent = text(session.label, text(session.session_id, "session"));
    elements.sessionDetailStatus.textContent = finalStatus === "ended"
      ? "Ended · retained history available"
      : finalStatus === "terminating"
        ? "Termination in progress · waiting for executor absence"
        : availability === "missing_on_executor"
          ? "Unavailable · missing on executor"
          : availability === "executor_offline"
            ? "Unavailable · executor offline"
            : `Available · ${sessionActivityTimestamp(session)}`;
    elements.sessionDetailId.textContent = text(session.session_id);
    elements.sessionDetailExecutor.textContent = text(session.executor_id);
    elements.sessionDetailWorkdir.textContent = text(session.workdir);
    elements.sessionDetailCreated.textContent = sessionTimestamp(session.created_at);
    elements.sessionDetailUpdated.textContent = sessionTimestamp(session.updated_at);
    syncControls();
  }

  function renderSessions(sessions) {
    controllerState.sessions = Array.isArray(sessions) ? sessions : [];
    elements.sessionList.replaceChildren();
    if (!controllerState.sessions.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = selectedTaskId()
        ? "This task has no retained execution sessions."
        : controllerState.sessionId
          ? "Unattached session selected from the task list."
          : "No retained execution session selected.";
      elements.sessionList.append(empty);
    } else {
      for (const session of controllerState.sessions) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "session-entry";
        button.dataset.sessionId = text(session.session_id, "");
        button.setAttribute(
          "aria-current",
          session.session_id === controllerState.sessionId ? "true" : "false",
        );
        const title = document.createElement("strong");
        title.textContent = text(session.label, text(session.workdir, "Session"));
        button.title = sessionOptionLabel(session);
        const meta = document.createElement("span");
        meta.className = sessionTerminated(session)
          ? "session-entry-meta session-entry-terminated"
          : "session-entry-meta";
        const unavailable = sessionUnavailableLabel(session);
        meta.textContent = session.status === "ended"
          ? `ended · ${sessionTimestamp(session.updated_at)}`
          : session.status === "terminating"
            ? `terminating · ${sessionTimestamp(session.updated_at)}`
            : unavailable
              ? `${unavailable.toLowerCase()} · ${sessionTimestamp(session.updated_at)}`
              : `available · ${sessionActivityTimestamp(session)}`;
        button.append(title, meta);
        button.addEventListener("click", () => {
          if (session.session_id === controllerState.sessionId) onNavigate("session");
          else void selectSession(session.session_id);
        });
        elements.sessionList.append(button);
      }
    }
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
    const count = sessions.length;
    return `${count} session${count === 1 ? "" : "s"} · ${sessionTimestamp(task && task.updated_at)}`;
  }

  function renderTaskList() {
    const active = controllerState.tasks.filter(
      (task) => task.status !== "completed" && task.status !== "cancelled",
    ).length;
    document.getElementById("task-count-active").textContent = String(active);
    document.getElementById("task-count-total").textContent = String(controllerState.tasks.length);
    elements.taskList.replaceChildren();
    if (!controllerState.tasks.length && !controllerState.unattachedSessions.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No tasks yet.";
      elements.taskList.append(empty);
      return;
    }
    for (const task of controllerState.tasks) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "session-entry task-entry";
      button.dataset.taskId = text(task.task_id, "");
      button.setAttribute(
        "aria-current",
        task.task_id === controllerState.taskId ? "true" : "false",
      );
      const title = document.createElement("strong");
      title.textContent = taskOptionLabel(task);
      const heading = document.createElement("span");
      heading.className = "task-entry-heading";
      const status = document.createElement("span");
      status.className = "task-entry-status";
      status.dataset.status = text(task.status, "unknown");
      status.textContent = text(task.status, "unknown");
      heading.append(title, status);
      const meta = document.createElement("span");
      meta.className = "session-entry-meta";
      meta.textContent = taskMeta(task);
      button.append(heading, meta);
      button.addEventListener("click", () => {
        void selectTask(task.task_id);
      });
      elements.taskList.append(button);
    }
    if (!controllerState.unattachedSessions.length) return;
    const label = document.createElement("div");
    label.className = "session-group-label";
    label.textContent = "Unattached sessions";
    elements.taskList.append(label);
    for (const session of controllerState.unattachedSessions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "session-entry unattached-session-entry";
      button.dataset.sessionId = text(session.session_id, "");
      button.setAttribute(
        "aria-current",
        !selectedTaskId() && session.session_id === controllerState.sessionId
          ? "true"
          : "false",
      );
      const title = document.createElement("strong");
      title.textContent = sessionOptionLabel(session);
      const meta = document.createElement("span");
      meta.className = "session-entry-meta";
      meta.textContent = session.status === "ended"
        ? `ended · ${sessionTimestamp(session.updated_at)}`
        : `${sessionUnavailableLabel(session) || text(session.status, "session")} · ${sessionTimestamp(session.updated_at)}`;
      button.append(title, meta);
      button.addEventListener(
        "click",
        () => void selectUnattachedSession(session.session_id),
      );
      elements.taskList.append(button);
    }
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
    controllerState.tasks = Array.isArray(payload && payload.tasks)
      ? payload.tasks
      : [];
    onInventory(controllerState.tasks);
    controllerState.unattachedSessions = Array.isArray(
      payload && payload.unattached_sessions,
    )
      ? payload.unattached_sessions
      : [];

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
      controllerState.todoMutationBusy ||
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

    controllerState.taskId = next;
    showTab("progress");
    const task = selectedTaskSummary();
    controllerState.sessions = Array.isArray(task && task.sessions)
      ? task.sessions
      : [];
    controllerState.sessionId = "";
    controllerState.todoDirty = false;
    clearTaskResources("Loading selected task");
    resetSessionAuditWorkspace(
      controllerState.sessionId
        ? "Loading selected session"
        : "No execution session selected",
    );
    renderTaskList();
    renderSessions(controllerState.sessions);
    syncDeepLink();
    onNavigate("task");
    await refreshTodos({ force: true });
    if (controllerState.sessionId) await refreshSessionAudit();
  }

  async function selectUnattachedSession(next) {
    if (
      !next ||
      controllerState.todoMutationBusy ||
      controllerState.workspaceLoading
    ) return;
    if (
      controllerState.todoDirty &&
      !globalThis.confirm(`Discard unsaved changes in ${selectedTaskId()}?`)
    ) return;

    controllerState.taskId = "";
    controllerState.sessionId = next;
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
    await refreshSessionAudit();
  }

  async function selectSession(next) {
    if (
      !next ||
      next === controllerState.sessionId ||
      controllerState.workspaceLoading
    ) return;
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

  async function refreshWorkspace() {
    controllerState.workspaceLoading = true;
    syncControls();
    elements.tasksState.textContent = "Loading tasks";
    const previousTask = selectedTaskId();
    try {
      const payload = await request("/tasks");
      applyInventory(payload);
      elements.tasksState.textContent = `Updated ${new Date().toLocaleTimeString()}`;

      if (!selectedTaskId()) {
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
        await refreshTodos({ force: true });
      }

      if (controllerState.sessionId) {
        await refreshSessionAudit();
      } else {
        resetSessionAuditWorkspace("No execution session selected");
      }
      return payload;
    } catch (error) {
      elements.tasksState.textContent =
        error instanceof Error ? error.message : String(error);
      if (error?.authenticationRequired) throw error;
      return null;
    } finally {
      controllerState.workspaceLoading = false;
      syncControls();
    }
  }

  async function terminateSelectedSession() {
    const session = selectedSession();
    if (
      !session ||
      sessionTerminated(session) ||
      controllerState.sessionTerminating
    ) return;
    const label = sessionOptionLabel(session);
    if (
      !globalThis.confirm(
        `Immediately terminate ${label}? Any later model tool call for this session will be told to stop all work.`,
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
      await refreshWorkspace();
    } catch (error) {
      elements.tasksState.textContent =
        error instanceof Error ? error.message : String(error);
    } finally {
      controllerState.sessionTerminating = false;
      syncControls();
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

  function renderTodoSummary() {
    const completed = controllerState.todoItems.filter((item) => item.status === "completed").length;
    const skipped = controllerState.todoItems.filter((item) => item.status === "skipped").length;
    const open = controllerState.todoItems.length - completed - skipped;
    const labels = [
      `${controllerState.todoItems.length} total`,
      `${open} open`,
      `${completed} completed`,
      `${skipped} skipped`,
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
      empty.textContent = controllerState.todoItems.length ? "No plan steps match this filter." : "No plan steps in this task.";
      elements.todoList.append(empty);
      syncControls();
      return;
    }

    for (const { item, index } of visible) {
      const row = document.createElement("article");
      row.className = "todo-row";
      row.dataset.todoId = item.id;
      row.dataset.status = item.status;
      row.dataset.priority = item.priority;

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
      todoOption(status, item.status, ["pending", "in_progress", "blocked", "completed", "skipped"]);
      status.addEventListener("change", () => {
        controllerState.todoItems[index].status = status.value;
        row.dataset.status = status.value;
        setTodoDirty();
        renderTodoSummary();
        if (elements.todoFilter.value !== "all") renderTodos();
      });

      const priority = document.createElement("select");
      todoOption(priority, item.priority, ["high", "medium", "low"]);
      priority.addEventListener("change", () => {
        controllerState.todoItems[index].priority = priority.value;
        row.dataset.priority = priority.value;
        setTodoDirty();
      });

      const identifier = document.createElement("span");
      identifier.className = "todo-id";
      identifier.textContent = `Step ${index + 1}`;
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

      const heading = document.createElement("div");
      heading.className = "todo-row-heading";
      heading.append(identifier, remove);
      row.append(
        heading,
        todoField("Content", "todo-field-content", content),
        todoField("Status", "todo-field-status", status),
        todoField("Priority", "todo-field-priority", priority),
      );
      elements.todoList.append(row);
    }
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

  function applyTodoPayload(payload, requestedTask) {
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

  async function refreshTodos({ force = false } = {}) {
    const requestedTask = selectedTaskId();
    if (!requestedTask || (!force && (controllerState.todoDirty || controllerState.todoMutationBusy))) return null;
    const generation = ++controllerState.todoGeneration;
    elements.todoState.textContent = "Loading…";
    syncControls();
    try {
      const payload = await request(todoQuery());
      if (generation !== controllerState.todoGeneration || requestedTask !== selectedTaskId()) return null;
      applyTodoPayload(payload, requestedTask);
      return payload;
    } catch (error) {
      if (generation !== controllerState.todoGeneration || requestedTask !== selectedTaskId()) return null;
      elements.todoState.textContent = error instanceof Error ? error.message : String(error);
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
    syncControls();
  }

  function renderSessionAuditList() {
    elements.sessionAuditList.replaceChildren();
    if (!controllerState.sessionAuditEntries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = controllerState.sessionId
        ? `No Audit records match for ${controllerState.sessionId}.`
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
          onNavigate("session-record");
        }),
      );
    }
  }

  function renderSessionAuditDetailEntry(entry) {
    elements.sessionAuditDetailTitle.textContent = auditEntryTitle(entry);
    elements.sessionAuditDetailMeta.textContent = auditTimestamp(entry.ts);
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
    const shown = controllerState.sessionAuditEntries.length;
    elements.sessionAuditSummary.textContent = total > shown ? `${shown} of ${total} records` : `${shown} records`;
    elements.sessionAuditState.textContent = "";
    renderSessionAuditList();
    const selected = payload && payload.entry && typeof payload.entry === "object" ? payload.entry : null;
    if (selected && selected.id === controllerState.sessionAuditSelectedId) {
      renderSessionAuditDetailEntry(selected);
    } else if (payload && payload.entry_error) {
      elements.sessionAuditDetailMeta.textContent = "Details unavailable";
      renderAuditDetailMessage(elements.sessionAuditDetailBody, text(payload.entry_error));
    } else if (controllerState.sessionAuditSelectedId) {
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

  async function refreshSessionAudit() {
    if (controllerState.sessionAuditLoading || !controllerState.sessionId) return null;
    const generation = ++controllerState.sessionAuditGeneration;
    const requestedSession = controllerState.sessionId;
    const previousSelection = controllerState.sessionAuditSelectedId;
    controllerState.sessionAuditLoading = true;
    syncControls();
    elements.sessionAuditState.textContent = "Loading…";
    try {
      const payload = await request(sessionAuditQueryPath());
      if (
        generation !== controllerState.sessionAuditGeneration ||
        requestedSession !== controllerState.sessionId
      ) return null;
      applySessionAuditPayload(payload, requestedSession, previousSelection);
      return payload;
    } catch (error) {
      if (
        generation !== controllerState.sessionAuditGeneration ||
        requestedSession !== controllerState.sessionId
      ) return null;
      controllerState.sessionAuditEntries = [];
      controllerState.sessionAuditSelectedId = "";
      renderSessionAuditList();
      elements.sessionAuditState.textContent = error instanceof Error ? error.message : String(error);
      return null;
    } finally {
      if (generation === controllerState.sessionAuditGeneration) {
        controllerState.sessionAuditLoading = false;
        syncControls();
      }
    }
  }

  function invalidate() {
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
    elements.tasksRefresh.addEventListener("click", () => {
      if (controllerState.workspaceLoading || controllerState.sessionTerminating) return;
      void refreshWorkspace();
    });
    elements.sessionTerminate.addEventListener(
      "click",
      () => void terminateSelectedSession(),
    );

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
      if (controllerState.todoMutationBusy || !selectedTaskId()) return;
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
