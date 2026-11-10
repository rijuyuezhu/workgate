void (async () => {
  "use strict";

  const config = JSON.parse(document.body.dataset.workgateConfig || "{}");
  const uiPath = String(config.uiPath || "/ui").replace(/\/$/, "");
  const assetRevision = encodeURIComponent(String(config.assetRevision || ""));
  const assetUrl = (name) =>
    `${uiPath}/assets/${name}${assetRevision ? `?v=${assetRevision}` : ""}`;
  const [
    { createDashboardController },
    { createExecutorsController },
    { createAuditController },
    { createTerminalController },
    { createFilesController },
    { createTasksController },
    { createGuiViewer },
    { createSessionIntegrationsViewer },
    { createMobileNavigation },
  ] = await Promise.all([
    import(assetUrl("dashboard.js")),
    import(assetUrl("executors.js")),
    import(assetUrl("audit.js")),
    import(assetUrl("terminal.js")),
    import(assetUrl("files.js")),
    import(assetUrl("tasks.js")),
    import(assetUrl("gui_view.js")),
    import(assetUrl("session_integrations.js")),
    import(assetUrl("mobile_navigation.js")),
  ]);
  // Keep one authenticated Files controller; React owns list presentation.
  const stylesheet = document.createElement("link");
  stylesheet.rel = "stylesheet";
  stylesheet.href = assetUrl("react_files_bundle.css");
  document.head.append(stylesheet);
  await import(assetUrl("react_files_bundle.js"));
  const mountReactFiles = window.WorkgateReactFiles?.mount;
  const reactNavigation = window.WorkgateReactNavigation;
  const mountDashboardResources = window.WorkgateReactDashboardResources?.mount;
  const mountDashboardSystem = window.WorkgateReactDashboardSystem?.mount;
  const mountOverviewLists = window.WorkgateReactOverviewLists?.mount;
  const mountDashboardHealth = window.WorkgateReactDashboardHealth?.mount;
  const mountExecutorInventory = window.WorkgateReactExecutorInventory?.mount;
  const mountExecutorDetail = window.WorkgateReactExecutorDetail?.mount;
  const mountAuditLists = window.WorkgateReactAuditLists?.mount;
  const mountAuditDetails = window.WorkgateReactAuditDetails?.mount;
  if (typeof mountReactFiles !== "function" || typeof reactNavigation?.mount !== "function"
      || typeof mountDashboardResources !== "function" || typeof mountDashboardSystem !== "function" || typeof mountOverviewLists !== "function" || typeof mountDashboardHealth !== "function" || typeof mountExecutorInventory !== "function" || typeof mountExecutorDetail !== "function" || typeof mountAuditLists !== "function" || typeof mountAuditDetails !== "function") {
    throw new Error("React UI failed to initialize");
  }
  const apiPrefix = String(config.apiPrefix || "/api/ui").replace(/\/$/, "");
  const oauth = config.oauth && typeof config.oauth === "object" ? config.oauth : null;
  const legacyTokenStorageKey = "workgate-ui-access-token";
  const pendingStorageKey = "workgate-ui-oauth-pending";
  const pendingMaxAgeMs = 10 * 60 * 1000;
  const csrfCookieName = String(config.csrfCookieName || "");
  const csrfHeaderName = String(config.csrfHeaderName || "x-workgate-ui-csrf");
  const sessionBindingHeaderName = String(
    config.sessionBindingHeaderName || "x-workgate-ui-binding",
  );
  const sessionBindingProtocolPrefix = String(
    config.sessionBindingProtocolPrefix || "workgate-ui-binding.",
  );
  const sessionBindingStorageKey = String(
    config.sessionBindingStorageKey || "workgate-ui-session-binding",
  );
  const sessionEstablishedStorageKey = String(
    config.sessionEstablishedStorageKey || "workgate-ui-session-established",
  );
  sessionStorage.removeItem(legacyTokenStorageKey);

  function normalizeUiReturnUrl(value) {
    try {
      const candidate = new URL(String(value || uiPath), location.origin);
      if (candidate.origin !== location.origin || candidate.pathname !== uiPath) return uiPath;
      return `${candidate.pathname}${candidate.search}${candidate.hash}`;
    } catch {
      return uiPath;
    }
  }

  function startupUiLocation() {
    const current = new URL(location.href);
    if (current.pathname !== `${uiPath}/callback`) return current;
    try {
      const pending = JSON.parse(sessionStorage.getItem(pendingStorageKey) || "null");
      if (pending && typeof pending.returnUrl === "string") {
        return new URL(normalizeUiReturnUrl(pending.returnUrl), location.origin);
      }
    } catch {
      // Invalid pending OAuth state is handled by finishOAuthCallback().
    }
    return current;
  }

  const initialUiLocation = startupUiLocation();
  const viewDefinitions = reactNavigation.views;
  const encoder = new TextEncoder();
  let authenticated = config.authMode !== "oauth";
  let loadGeneration = 0;
  let loadedOnce = false;
  const pendingReads = new Map();

  function cancelPendingReads() {
    loadGeneration += 1;
    for (const controller of pendingReads.keys()) controller.abort();
    pendingReads.clear();
  }

  function cancelAuditReads() {
    for (const [controller, path] of pendingReads) {
      if (!path.startsWith("/audit?") && !path.startsWith("/audit/detail?")) continue;
      controller.abort();
      pendingReads.delete(controller);
    }
  }

  const elements = {
    appViews: Array.from(document.querySelectorAll("[data-app-view]")),
    appHeader: document.querySelector(".app-header"),
    pageTitle: document.getElementById("page-title"),
    dashboardExecutor: document.getElementById("dashboard-executor"),
    auditDetailBody: document.getElementById("audit-detail-body"),
    auditDetailMeta: document.getElementById("audit-detail-meta"),
    auditDetailTitle: document.getElementById("audit-detail-title"),
    auditEvent: document.getElementById("audit-event"),
    auditFilterForm: document.getElementById("audit-filter-form"),
    auditLimit: document.getElementById("audit-limit"),
    auditList: document.getElementById("audit-list"),
        auditOperation: document.getElementById("audit-operation"),
    auditRefresh: document.getElementById("audit-refresh"),
    auditSearch: document.getElementById("audit-search"),
    auditSort: document.getElementById("audit-sort"),
    auditState: document.getElementById("audit-state"),
    auditSummary: document.getElementById("audit-summary"),
    authDetail: document.getElementById("auth-detail"),
    authForm: document.getElementById("auth-form"),
    authPanel: document.getElementById("auth-panel"),
    fileBreadcrumbs: document.getElementById("file-breadcrumbs"),
    fileClipboardState: document.getElementById("file-clipboard-state"),
    fileCopy: document.getElementById("file-copy"),
    fileDelete: document.getElementById("file-delete"),
    fileEdit: document.getElementById("file-edit"),
    fileEditor: document.getElementById("file-editor"),
    fileEditorCancel: document.getElementById("file-editor-cancel"),
    fileEditorReload: document.getElementById("file-editor-reload"),
    fileEditorForm: document.getElementById("file-editor-form"),
    fileList: document.getElementById("file-list"),
    fileExecutor: document.getElementById("file-executor"),
    fileFilter: document.getElementById("file-filter"),
    fileMove: document.getElementById("file-move"),
    fileNewFolder: document.getElementById("file-new-folder"),
    fileOperationDialog: document.getElementById("file-operation-dialog"),
    fileOperationForm: document.getElementById("file-operation-form"),
    fileOperationTitle: document.getElementById("file-operation-title"),
    fileOperationLabel: document.getElementById("file-operation-label"),
    fileOperationName: document.getElementById("file-operation-name"),
    fileOperationCancel: document.getElementById("file-operation-cancel"),
    filePaste: document.getElementById("file-paste"),
    fileSort: document.getElementById("file-sort"),
    fileSortDirection: document.getElementById("file-sort-direction"),
    fileUpload: document.getElementById("file-upload"),
    fileUploadCancel: document.getElementById("file-upload-cancel"),
    fileUploadInput: document.getElementById("file-upload-input"),
    fileNew: document.getElementById("file-new"),
    fileOpen: document.getElementById("file-open"),
    fileDownload: document.getElementById("file-download"),
    filePath: document.getElementById("file-path"),
    filePathForm: document.getElementById("file-path-form"),
    filePreviewBody: document.getElementById("file-preview-body"),
    filePreviewMeta: document.getElementById("file-preview-meta"),
    filePreviewTitle: document.getElementById("file-preview-title"),
    fileRefresh: document.getElementById("file-refresh"),
    fileRename: document.getElementById("file-rename"),
    fileShowHidden: document.getElementById("file-show-hidden"),
    fileState: document.getElementById("file-state"),
    fileUp: document.getElementById("file-up"),
    oauthLogin: document.getElementById("oauth-login"),
    executorSubtabs: Array.from(document.querySelectorAll("#executor-subtabs [data-executor-subtab]")),
    executorSubtabPanes: Array.from(document.querySelectorAll(".executor-subtab-pane[data-executor-pane]")),
    executorMcpAdd: document.getElementById("executor-mcp-add"),
    executorSkillsAdd: document.getElementById("executor-skills-add"),
    executorIntegrationDialog: document.getElementById("executor-integration-dialog"),
    executorIntegrationForm: document.getElementById("executor-integration-form"),
    executorIntegrationTitle: document.getElementById("executor-integration-title"),
    executorIntegrationStatus: document.getElementById("executor-integration-status"),
    executorIntegrationName: document.getElementById("executor-integration-name"),
    executorIntegrationOwner: document.getElementById("executor-integration-owner"),
    executorIntegrationUrl: document.getElementById("executor-integration-url"),
    executorIntegrationCommand: document.getElementById("executor-integration-command"),
    executorIntegrationArgs: document.getElementById("executor-integration-args"),
    executorIntegrationTestWrap: document.getElementById("executor-integration-test-wrap"),
    executorIntegrationTest: document.getElementById("executor-integration-test"),
    executorIntegrationTestResult: document.getElementById("executor-integration-test-result"),
    executorIntegrationAdvanced: document.getElementById("executor-integration-advanced"),
    executorIntegrationAuthMode: document.getElementById("executor-integration-auth-mode"),
    executorIntegrationOAuthScopes: document.getElementById("executor-integration-oauth-scopes"),
    executorIntegrationOAuthScopesWrap: document.getElementById("executor-integration-oauth-scopes-wrap"),
    executorMcpAuthManagement: document.getElementById("executor-mcp-auth-management"),
    executorMcpAuthStatus: document.getElementById("executor-mcp-auth-status"),
    executorMcpSecretDetails: document.getElementById("executor-mcp-secret-details"),
    executorMcpSecretList: document.getElementById("executor-mcp-secret-list"),
    executorMcpSecretName: document.getElementById("executor-mcp-secret-name"),
    executorMcpSecretValue: document.getElementById("executor-mcp-secret-value"),
    executorMcpSecretSet: document.getElementById("executor-mcp-secret-set"),
    executorMcpOAuthActions: document.getElementById("executor-mcp-oauth-actions"),
    executorMcpOAuthAdvanced: document.getElementById("executor-mcp-oauth-advanced"),
    executorMcpOAuthAuthorize: document.getElementById("executor-mcp-oauth-authorize"),
    executorMcpOAuthLink: document.getElementById("executor-mcp-oauth-link"),
    executorMcpOAuthCopy: document.getElementById("executor-mcp-oauth-copy"),
    executorMcpOAuthCancel: document.getElementById("executor-mcp-oauth-cancel"),
    executorMcpOAuthProgress: document.getElementById("executor-mcp-oauth-progress"),
    executorMcpOAuthLogout: document.getElementById("executor-mcp-oauth-logout"),
    executorMcpOAuthClear: document.getElementById("executor-mcp-oauth-clear"),
    executorIntegrationToggle: document.getElementById("executor-integration-toggle"),
    executorIntegrationDelete: document.getElementById("executor-integration-delete"),
    executorIntegrationContent: document.getElementById("executor-integration-content"),
    executorIntegrationSkillWrap: document.getElementById("executor-integration-skill-wrap"),
    executorSkillFilesEntry: document.getElementById("executor-skill-files-entry"),
    executorSkillFilesOpen: document.getElementById("executor-skill-files-open"),
    executorIntegrationMcpFields: document.getElementById("executor-integration-mcp-fields"),
    executorIntegrationUrlWrap: document.getElementById("executor-integration-url-wrap"),
    executorIntegrationCommandWrap: document.getElementById("executor-integration-command-wrap"),
    executorIntegrationArgsWrap: document.getElementById("executor-integration-args-wrap"),
    executorIntegrationSave: document.getElementById("executor-integration-save"),
    executorIntegrationValidate: document.getElementById("executor-integration-validate"),
    executorIntegrationClose: document.getElementById("executor-integration-close"),
    executorIntegrationCancel: document.getElementById("executor-integration-cancel"),
    executorMcpBody: document.getElementById("executor-mcp-body"),
    executorSkillsBody: document.getElementById("executor-skills-body"),
    executorList: document.getElementById("executor-list"),
    executorPairApprove: document.getElementById("executor-pair-approve"),
    executorPairBuild: document.getElementById("executor-pair-build"),
    executorPairCode: document.getElementById("executor-pair-code"),
    executorPairDeny: document.getElementById("executor-pair-deny"),
    executorPairDialog: document.getElementById("executor-pair-dialog"),
    executorPairExisting: document.getElementById("executor-pair-existing"),
    executorPairExpiry: document.getElementById("executor-pair-expiry"),
    executorPairForm: document.getElementById("executor-pair-form"),
    executorPairHostname: document.getElementById("executor-pair-hostname"),
    executorPairName: document.getElementById("executor-pair-name"),
    executorPairOpen: document.getElementById("executor-pair-open"),
    executorPairPlatform: document.getElementById("executor-pair-platform"),
    executorPairStatus: document.getElementById("executor-pair-status"),
    executorPairReplace: document.getElementById("executor-pair-replace"),
    executorPairReplaceWrap: document.getElementById("executor-pair-replace-wrap"),
    executorPairRequestedName: document.getElementById("executor-pair-requested-name"),
    executorPairReview: document.getElementById("executor-pair-review"),
    executorRenameDialog: document.getElementById("executor-rename-dialog"),
    executorRenameForm: document.getElementById("executor-rename-form"),
    executorRenameName: document.getElementById("executor-rename-name"),
    executorRevokeDialog: document.getElementById("executor-revoke-dialog"),
    executorRevokeForm: document.getElementById("executor-revoke-form"),
    executorRevokeName: document.getElementById("executor-revoke-name"),
    executorActionStatus: document.getElementById("executor-action-status"),
    executorState: document.getElementById("executor-state"),
    terminalCopy: document.getElementById("terminal-copy"),
    terminalFeedback: document.getElementById("terminal-feedback"),
    terminalInput: document.getElementById("terminal-input"),
    terminalInputForm: document.getElementById("terminal-input-form"),
    terminalKeyButtons: Array.from(document.querySelectorAll("[data-terminal-key]")),
    terminalKill: document.getElementById("terminal-kill"),
    terminalList: document.getElementById("terminal-list"),
    terminalExecutor: document.getElementById("terminal-executor"),
    terminalName: document.getElementById("terminal-name"),
    terminalOutput: document.getElementById("terminal-output"),
    terminalPaste: document.getElementById("terminal-paste"),
    terminalReconnect: document.getElementById("terminal-reconnect"),
    terminalXterm: document.getElementById("terminal-xterm"),
    terminalStartForm: document.getElementById("terminal-start-form"),
    terminalState: document.getElementById("terminal-state"),
    terminalTitle: document.getElementById("terminal-title"),
    sessionAuditDetailBody: document.getElementById("session-audit-detail-body"),
    sessionAuditDetailMeta: document.getElementById("session-audit-detail-meta"),
    sessionAuditDetailTitle: document.getElementById("session-audit-detail-title"),
    sessionAuditFilterForm: document.getElementById("session-audit-filter-form"),
    sessionAuditLimit: document.getElementById("session-audit-limit"),
    sessionAuditList: document.getElementById("session-audit-list"),
    sessionAuditOperation: document.getElementById("session-audit-operation"),
    sessionAuditRefresh: document.getElementById("session-audit-refresh"),
    sessionAuditSearch: document.getElementById("session-audit-search"),
    sessionAuditSort: document.getElementById("session-audit-sort"),
    sessionAuditState: document.getElementById("session-audit-state"),
    sessionAuditSummary: document.getElementById("session-audit-summary"),
    sessionDetailCreated: document.getElementById("session-detail-created"),
    sessionDetailId: document.getElementById("session-detail-id"),
    sessionDetailExecutor: document.getElementById("session-detail-executor"),
    sessionDetailExecutorAvatar: document.getElementById("session-detail-executor-avatar"),
    sessionDetailExecutorId: document.getElementById("session-detail-executor-id"),
    sessionDetailStatus: document.getElementById("session-detail-status"),
    sessionDetailTitle: document.getElementById("session-detail-title"),
    sessionDetailUpdated: document.getElementById("session-detail-updated"),
    sessionDetailWorkdir: document.getElementById("session-detail-workdir"),
    sessionList: document.getElementById("session-list"),
    taskList: document.getElementById("task-list"),
    tasksFilter: document.getElementById("tasks-filter"),
    tasksPrev: document.getElementById("tasks-prev"),
    tasksNext: document.getElementById("tasks-next"),
    tasksPageState: document.getElementById("tasks-page-state"),
    tasksRefresh: document.getElementById("tasks-refresh"),
    tasksState: document.getElementById("tasks-state"),
    sessionTerminate: document.getElementById("session-terminate"),
    taskBlockers: document.getElementById("task-blockers"),
    taskFindings: document.getElementById("task-findings"),
    taskNextAction: document.getElementById("task-next-action"),
    taskObjective: document.getElementById("task-objective"),
    taskState: document.getElementById("task-state"),
    taskStatus: document.getElementById("task-status"),
    taskActionState: document.getElementById("task-action-state"),
    taskSummary: document.getElementById("task-summary"),
    todoAdd: document.getElementById("todo-add"),
    todoFilter: document.getElementById("todo-filter"),
    todoList: document.getElementById("todo-list"),
    todoRefresh: document.getElementById("todo-refresh"),
    todoSave: document.getElementById("todo-save"),
    todoState: document.getElementById("todo-state"),
    todoSummary: document.getElementById("todo-summary"),
    tokenInput: document.getElementById("access-token"),
  };

  const headerStatus = {
    connectionLabel: "Connecting",
    connectionKind: "idle",
    version: "—",
    authMode: text(config.authMode),
    showSignOut: false,
  };
  const navigation = reactNavigation.mount(
    document.getElementById("app-header-navigation"),
    navigateTo,
    () => setNavigationOpen(!elements.appHeader.classList.contains("menu-open")),
    () => void signOut(),
  );
  function renderHeader() {
    navigation.render(
      document.body.dataset.activeView || "overview",
      elements.appHeader.classList.contains("menu-open"),
      headerStatus,
    );
  }

  // Compact inline line icons for commonly used actions; no font or icon package required.
  const iconPaths = {
    refresh: "M20 11a8 8 0 0 0-14.6-4.6L3 9m0-5v5h5M4 13a8 8 0 0 0 14.6 4.6L21 15m0 5v-5h-5",
    plus: "M12 5v14M5 12h14",
    terminal: "m4 7 5 5-5 5M11 17h9M3 4h18v16H3z",
    folder: "M3 7h7l2 2h9v11H3zM3 7V5h7l2 2",
    file: "M6 3h8l4 4v14H6zM14 3v5h5",
    upload: "M12 16V3m-5 5 5-5 5 5M4 16v4h16v-4",
    copy: "M8 7h12v14H8zM4 17H3V3h13v2",
    paste: "M8 4h2a2 2 0 0 1 4 0h2v3H8zM6 5H4v16h16V5h-2",
    edit: "m4 16-1 5 5-1L20 8l-4-4zM13 7l4 4",
    trash: "M4 7h16M9 7V4h6v3m-9 0 1 14h10l1-14M10 11v6M14 11v6",
    save: "M4 3h13l4 4v14H4zM8 3v6h9V3M8 21v-8h9v8",
    up: "M12 20V4m-6 6 6-6 6 6",
    retry: "M20 7v5h-5M4 17v-5h5M5 9a8 8 0 0 1 14-2l1 5M19 15a8 8 0 0 1-14 2l-1-5",
    scissors: "M9 6a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm0 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM8 8l13 13M8 16 21 3",
    link: "M9 15 15 9M8 7h-2a5 5 0 0 0 0 10h4m4-10h4a5 5 0 0 1 0 10h-2",
  };
  const controlIcons = {
    "tasks-refresh": "refresh", "audit-refresh": "refresh", "session-audit-refresh": "refresh",
    "file-refresh": "refresh", "todo-refresh": "refresh", "terminal-reconnect": "retry",
    "executor-pair-open": "plus", "terminal-create": "terminal", "terminal-kill": "trash", "terminal-copy": "copy",
    "terminal-paste": "paste", "file-new": "file", "file-new-folder": "folder",
    "file-upload": "upload", "file-open": "link", "file-edit": "edit",
    "file-copy": "copy", "file-move": "scissors", "file-paste": "paste",
    "file-rename": "edit", "file-delete": "trash", "file-up": "up",
    "file-workspace-shortcut": "folder", "file-parent-shortcut": "up",
    "todo-add": "plus", "todo-save": "save",
  };
  for (const [id, name] of Object.entries(controlIcons)) {
    const button = document.getElementById(id);
    if (!button) continue;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "control-icon");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", iconPaths[name]);
    svg.append(path);
    button.prepend(svg);
  }
  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  const deepLinkParams = initialUiLocation.searchParams;
  function deepLinkValue(name, maxLength) {
    const value = String(deepLinkParams.get(name) || "").trim();
    return value.slice(0, maxLength);
  }
  const deepLink = Object.freeze({
    taskId: deepLinkValue("task_id", 128),
    sessionId: deepLinkValue("session_id", 128),
    executorId: deepLinkValue("executor_id", 255),
    workdir: deepLinkValue("workdir", 4096),
    shellId: deepLinkValue("shell_id", 255),
  });
  const mobileNavigation = createMobileNavigation();
  mobileNavigation.bind();

  const auditEntryTitle = (entry) => text(entry.tool, text(entry.event, "unknown"));
  function auditTimestamp(value) {
    const date = new Date(Number(value || 0) * 1000);
    return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString();
  }
  const auditDetailViews = new Map([
    [elements.auditDetailBody, mountAuditDetails(elements.auditDetailBody, formatFileBytes)],
    [elements.sessionAuditDetailBody, mountAuditDetails(elements.sessionAuditDetailBody, formatFileBytes)],
  ]);
  function renderAuditDetailInto(entry, target) {
    auditDetailViews.get(target).renderEntry(entry);
  }
  function renderAuditDetailMessage(target, message) {
    auditDetailViews.get(target).renderMessage(message);
  }

  const audit = createAuditController({
    elements,
    request,
    text,
    mountAuditLists,
    auditEntryTitle,
    auditTimestamp,
    renderAuditDetailInto,
    renderAuditDetailMessage,
    initialSessionId: deepLink.sessionId,
    onNavigate: (step, options) => mobileNavigation.go("audit", step, options),
  });
  audit.bind();

  const terminal = createTerminalController({
    elements,
    request,
    text,
    encoder,
    onAuthenticationRequired: () => void load(),
    initialExecutorId: deepLink.executorId,
    initialSessionId: deepLink.sessionId,
    initialShellId: deepLink.shellId,
    onNavigate: (step, options) => mobileNavigation.go("terminals", step, options),
  });
  terminal.bind();

  const files = createFilesController({
    elements,
    request,
    mountReactFiles,
    text,
    formatFileBytes,
    initialExecutorId: deepLink.executorId,
    initialPath: deepLink.workdir,
    onNavigate: (step, options) => mobileNavigation.go("files", step, options),
  });
  files.bind();

  const guiViewer = createGuiViewer({ request, onOpen: () => integrationsViewer.close() });
  const integrationsViewer = createSessionIntegrationsViewer({
    request,
    onOpen: () => guiViewer.close(),
    onManage: (executorId, kind) => {
      navigateTo("executors");
      void executors.openManagement(executorId, kind);
    },
  });
  guiViewer.bind();
  integrationsViewer.bind();

  const tasks = createTasksController({
    elements,
    request,
    text,
    mountAuditLists,
    auditEntryTitle,
    auditTimestamp,
    renderAuditDetailInto,
    renderAuditDetailMessage,
    onInventory: (items) => dashboard.renderTasks(items),
    initialTaskId: deepLink.taskId,
    initialSessionId: deepLink.sessionId,
    onSessionChange: (session) => {
      guiViewer.selectSession(session);
      integrationsViewer.selectSession(session);
    },
    onNavigate: (step, options) => mobileNavigation.go("tasks", step, options),
  });
  tasks.bind();

  const resourceCards = mountDashboardResources(
    document.querySelector("#dashboard-panel .dashboard-summary"),
  );
  resourceCards.render();
  const systemDetails = mountDashboardSystem(document.getElementById("dashboard-system-root"));
  systemDetails.render();
  const healthView = mountDashboardHealth(
    document.getElementById("dashboard-health-root"),
    document.getElementById("dashboard-state"),
    () => navigateTo("audit"),
  );
  const overviewLists = mountOverviewLists(document.getElementById("dashboard-overview-lists"), {
    navigate: (view) => navigateTo(view),
    selectExecutor: (id) => {
      elements.dashboardExecutor.value = id;
      elements.dashboardExecutor.dispatchEvent(new Event("change", { bubbles: true }));
    },
    openTask: (taskId) => {
      navigateTo("tasks");
      void tasks.select(taskId);
    },
  });
  const dashboard = createDashboardController({
    renderOverviewExecutors: overviewLists.renderExecutors,
    renderOverviewSelection: overviewLists.renderSelection,
    renderOverviewAlerts: overviewLists.renderAlerts,
    renderOverviewTasks: overviewLists.renderTasks,
    renderSystemDetails: systemDetails.render,
    healthView,
    renderResources: resourceCards.render,
    elements,
    request,
    text,
    authMode: config.authMode,
    isAuthenticated: () => authenticated,
    onAuthenticationRequired: () => void load(),
    initialExecutorId: deepLink.executorId,
  });
  dashboard.bind();

  const executors = createExecutorsController({
    mountExecutorInventory,
    mountExecutorDetail,
    elements,
    request,
    mountDashboardResources,
    authMode: config.authMode,
    isAuthenticated: () => authenticated,
    reloadApp: () => load(),
    onInventory: (rows) => {
      const targets = rows
        .filter((row) => !row.revoked_at)
        .map((row) => ({
          executor_id: row.executor_id,
          name: row.name,
          status: row.online ? "online" : "offline",
        }));
      renderExecutorTargets(targets);
    },
    onNavigate: (step, options) => mobileNavigation.go("executors", step, options),
  });
  executors.bind();

  function normalizeView(value) {
    const candidate = String(value || "").replace(/^#/, "");
    if (!Object.prototype.hasOwnProperty.call(viewDefinitions, candidate)) return "overview";
    return candidate;
  }

  function viewFromLocation(source = location) {
    const hashView = source.hash.slice(1);
    if (!hashView && source.pathname === "/pair") return "executors";
    return normalizeView(hashView);
  }

  function setActiveView(value, { syncHash = true, replaceHash = false } = {}) {
    const view = normalizeView(value);
    const previousView = document.body.dataset.activeView;
    if (previousView === "audit" && view !== "audit") {
      cancelAuditReads();
      audit.invalidate();
    }
    const definition = viewDefinitions[view];
    document.body.dataset.activeView = view;
    elements.pageTitle.textContent = definition.title;
    document.title = `${definition.title} — Workgate`;

    renderHeader();
    for (const panel of elements.appViews) {
      panel.hidden = panel.dataset.appView !== view;
    }
    if (view !== "executors") {
      executors.closeTopology();
      executors.clearConfiguration();
    }
    if (view !== "tasks") {
      guiViewer.close();
      integrationsViewer.close();
    }
    mobileNavigation.sync(view);
    if (view === "executors" && (config.authMode !== "oauth" || authenticated)) {
      executors.activate();
    }
    if (view === "audit" && previousView !== "audit" && loadedOnce && authenticated)
      void audit.refresh();

    if (syncHash && location.hash !== `#${view}`) {
      const url = `${location.pathname}${location.search}#${view}`;
      if (replaceHash) history.replaceState({}, "", url);
      else history.pushState({}, "", url);
    }
    window.requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
      if (view === "terminals") terminal.focus();
    });
  }

  function setConnection(label, state) {
    headerStatus.connectionLabel = label;
    headerStatus.connectionKind = state;
    renderHeader();
  }

  function setNavigationOpen(open) {
    elements.appHeader.classList.toggle("menu-open", open);
    renderHeader();
  }

  function cookieValue(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    for (const part of document.cookie.split(";")) {
      const item = part.trim();
      if (item.startsWith(prefix)) return decodeURIComponent(item.slice(prefix.length));
    }
    return "";
  }

  function oauthAvailable() {
    return config.authMode === "oauth" && oauth !== null;
  }

  function showAuthentication(message, detail) {
    authenticated = false;
    elements.authPanel.hidden = false;
    elements.oauthLogin.hidden = !oauthAvailable();
    elements.oauthLogin.disabled = false;
    headerStatus.showSignOut = false;
    renderHeader();
    elements.tokenInput.setAttribute("aria-invalid", "true");
    elements.authDetail.textContent = detail || "Sign in through the Workgate OAuth approval page.";
    setConnection(message || "Authentication required", "error");
  }

  function hideAuthentication() {
    authenticated = true;
    elements.authPanel.hidden = true;
    elements.tokenInput.removeAttribute("aria-invalid");
    headerStatus.showSignOut = config.authMode === "oauth";
    renderHeader();
  }

  async function responsePayload(response) {
    try {
      return await response.json();
    } catch {
      return {};
    }
  }

  function validSessionBindingToken(value) {
    return /^[A-Za-z0-9_-]{43,128}$/.test(String(value || ""));
  }

  function sessionBindingToken() {
    if (config.authMode !== "oauth") return "";
    try {
      const value = localStorage.getItem(sessionBindingStorageKey) || "";
      return validSessionBindingToken(value) ? value : "";
    } catch {
      return "";
    }
  }

  function ensureSessionBindingToken() {
    const existing = sessionBindingToken();
    if (existing) return existing;
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    const created = base64Url(bytes);
    try {
      localStorage.setItem(sessionBindingStorageKey, created);
    } catch {
      throw new Error("Persistent browser storage is unavailable for secure session binding.");
    }
    return created;
  }

  function clearSessionBindingToken() {
    try {
      localStorage.removeItem(sessionBindingStorageKey);
    } catch {
      // The server-side session is still cleared when browser storage is unavailable.
    }
  }

  function announceSessionEstablished() {
    try {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      localStorage.setItem(
        sessionEstablishedStorageKey,
        `${Date.now()}.${base64Url(bytes)}`,
      );
    } catch {
      // The current tab can still use the new cookie when cross-tab signaling is unavailable.
    }
  }

  async function request(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    const bindingToken = sessionBindingToken();
    if (bindingToken) headers[sessionBindingHeaderName] = bindingToken;
    const method = String(options.method || "GET").toUpperCase();
    if (!new Set(["GET", "HEAD", "OPTIONS"]).has(method)) {
      const csrfToken = cookieValue(csrfCookieName);
      if (csrfToken) headers[csrfHeaderName] = csrfToken;
    }
    const read = method === "GET" || method === "HEAD";
    const controller = read ? new AbortController() : null;
    const generation = loadGeneration;
    if (controller) pendingReads.set(controller, path);
    let timedOut = false;
    const deadline = controller && globalThis.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 12000);
    let response;
    let payload;
    try {
      response = await fetch(`${apiPrefix}${path}`, {
        ...options,
        headers,
        signal: controller?.signal || options.signal,
        cache: "no-store",
        credentials: "same-origin",
      });
      payload = await responsePayload(response);
    } catch (error) {
      if (timedOut && generation === loadGeneration) throw new Error("Request timed out");
      throw error;
    } finally {
      if (deadline) globalThis.clearTimeout(deadline);
      if (controller) pendingReads.delete(controller);
    }
    // Writes are never aborted: their eventual outcome may be committed remotely.
    // But a response from another login must not report success in this UI.
    if (generation !== loadGeneration) {
      const error = new Error(read ? "Obsolete response"
        : "Session changed; check the remote result before retrying this action.");
      error.obsoleteRequest = true;
      throw error;
    }
    if (response.status === 401) {
      // A background read may be the first to notice expiry; clear every
      // protected view now, not only when its caller handles this error.
      requireAuthentication();
      const error = new Error("Authentication required");
      error.authenticationRequired = true;
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.message || payload.detail || `Request failed (${response.status})`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload.data;
  }

  function base64Url(bytes) {
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function randomVerifier() {
    if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== "function") {
      throw new Error("Secure browser randomness is unavailable. Use HTTPS or localhost.");
    }
    return base64Url(globalThis.crypto.getRandomValues(new Uint8Array(48)));
  }

  async function sha256(value) {
    if (!globalThis.crypto || !globalThis.crypto.subtle) {
      throw new Error("Web Crypto is unavailable. Use HTTPS or localhost for OAuth sign-in.");
    }
    const digest = await globalThis.crypto.subtle.digest("SHA-256", encoder.encode(value));
    return base64Url(new Uint8Array(digest));
  }

  function oauthEndpoint(name) {
    const value = oauth && oauth[name];
    if (typeof value !== "string" || !value) {
      throw new Error(`OAuth configuration is missing ${name}.`);
    }
    return new URL(value, location.origin);
  }

  function callbackUrl() {
    return new URL(`${uiPath}/callback`, location.origin).href;
  }

  function normalizeIssuer(value) {
    return String(value || "").replace(/\/+$/, "");
  }

  function cleanCallbackUrl(returnUrl = uiPath) {
    history.replaceState({}, "", normalizeUiReturnUrl(returnUrl));
  }

  function parsePendingOAuth() {
    const raw = sessionStorage.getItem(pendingStorageKey);
    if (!raw) throw new Error("The OAuth request state is missing. Start authentication again.");
    let pending;
    try {
      pending = JSON.parse(raw);
    } catch {
      sessionStorage.removeItem(pendingStorageKey);
      throw new Error("The saved OAuth request state is invalid. Start authentication again.");
    }
    const valid =
      pending &&
      typeof pending.clientId === "string" &&
      pending.clientId.length > 0 &&
      typeof pending.verifier === "string" &&
      pending.verifier.length >= 43 &&
      pending.verifier.length <= 128 &&
      typeof pending.state === "string" &&
      pending.state.length >= 32 &&
      typeof pending.redirectUri === "string" &&
      pending.redirectUri === callbackUrl() &&
      typeof pending.returnUrl === "string" &&
      pending.returnUrl === normalizeUiReturnUrl(pending.returnUrl) &&
      typeof pending.createdAt === "number" &&
      Number.isFinite(pending.createdAt);
    if (!valid) {
      sessionStorage.removeItem(pendingStorageKey);
      throw new Error("The saved OAuth request state is incomplete. Start authentication again.");
    }
    const ageMs = Date.now() - pending.createdAt;
    if (ageMs < -60000 || ageMs > pendingMaxAgeMs) {
      sessionStorage.removeItem(pendingStorageKey);
      throw new Error("The OAuth request expired. Start authentication again.");
    }
    return pending;
  }

  async function startOAuth() {
    if (!oauthAvailable()) {
      showAuthentication(
        "OAuth unavailable",
        "This server did not advertise a browser OAuth configuration. Use an existing access token instead.",
      );
      return;
    }
    elements.oauthLogin.disabled = true;
    elements.authDetail.textContent = "Preparing a secure OAuth authorization request…";
    try {
      ensureSessionBindingToken();
      const redirectUri = callbackUrl();
      const registration = await fetch(oauthEndpoint("registrationEndpoint"), {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          client_name: "Workgate WebUI",
          redirect_uris: [redirectUri],
        }),
        cache: "no-store",
        credentials: "same-origin",
      });
      const registered = await responsePayload(registration);
      if (!registration.ok || typeof registered.client_id !== "string") {
        throw new Error(
          registered.error_description || registered.error || `Client registration failed (${registration.status})`,
        );
      }

      const verifier = randomVerifier();
      const state = randomVerifier();
      const challenge = await sha256(verifier);
      sessionStorage.setItem(
        pendingStorageKey,
        JSON.stringify({
          clientId: registered.client_id,
          verifier,
          state,
          redirectUri,
          returnUrl: normalizeUiReturnUrl(location.href),
          createdAt: Date.now(),
        }),
      );

      const authorize = oauthEndpoint("authorizationEndpoint");
      authorize.searchParams.set("response_type", "code");
      authorize.searchParams.set("client_id", registered.client_id);
      authorize.searchParams.set("redirect_uri", redirectUri);
      authorize.searchParams.set("scope", String(oauth.scope || ""));
      authorize.searchParams.set("resource", String(oauth.resource || ""));
      authorize.searchParams.set("code_challenge", challenge);
      authorize.searchParams.set("code_challenge_method", "S256");
      authorize.searchParams.set("state", state);
      location.assign(authorize);
    } catch (error) {
      sessionStorage.removeItem(pendingStorageKey);
      elements.oauthLogin.disabled = false;
      elements.authDetail.textContent = error instanceof Error ? error.message : String(error);
    }
  }

  async function finishOAuthCallback() {
    const url = new URL(location.href);
    const callbackError = url.searchParams.get("error");
    if (callbackError) {
      sessionStorage.removeItem(pendingStorageKey);
      cleanCallbackUrl();
      throw new Error(url.searchParams.get("error_description") || callbackError);
    }

    const code = url.searchParams.get("code");
    if (!code) return false;
    if (!oauthAvailable()) {
      cleanCallbackUrl();
      throw new Error("OAuth callback received while browser OAuth is unavailable.");
    }
    let pending;
    try {
      pending = parsePendingOAuth();
    } catch (error) {
      cleanCallbackUrl();
      throw error;
    }
    if (url.searchParams.get("state") !== pending.state) {
      sessionStorage.removeItem(pendingStorageKey);
      cleanCallbackUrl();
      throw new Error("OAuth state verification failed.");
    }

    const expectedIssuer = normalizeIssuer(oauth && oauth.issuer);
    const responseIssuer = normalizeIssuer(url.searchParams.get("iss"));
    if (!expectedIssuer || responseIssuer !== expectedIssuer) {
      sessionStorage.removeItem(pendingStorageKey);
      cleanCallbackUrl();
      throw new Error("OAuth issuer verification failed.");
    }

    const form = new URLSearchParams({
      grant_type: "authorization_code",
      code,
      client_id: pending.clientId,
      redirect_uri: pending.redirectUri,
      resource: String(oauth.resource || ""),
      code_verifier: pending.verifier,
    });
    const response = await fetch(oauthEndpoint("sessionOAuthEndpoint"), {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
        [sessionBindingHeaderName]: ensureSessionBindingToken(),
      },
      body: form,
      cache: "no-store",
      credentials: "same-origin",
    });
    const result = await responsePayload(response);
    if (!response.ok || !result.ok) {
      throw new Error(
        result.error_description || result.error || result.detail || "OAuth session exchange failed.",
      );
    }
    announceSessionEstablished();
    sessionStorage.removeItem(pendingStorageKey);
    cleanCallbackUrl(pending.returnUrl);
    return true;
  }

  function formatFileBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "size unavailable";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KiB", "MiB", "GiB"];
    let size = bytes / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && size >= 1024; index += 1) {
      size /= 1024;
      unit = units[index];
    }
    return `${size.toFixed(size >= 10 ? 1 : 2)} ${unit}`;
  }

  let executorTargetsSignature = "";
  function renderExecutorTargets(targets) {
    const signature = JSON.stringify(targets);
    if (signature === executorTargetsSignature) return;
    executorTargetsSignature = signature;
    healthView.renderTargets(targets);
    dashboard.renderExecutors(targets);
    terminal.renderExecutors(targets);
    files.renderExecutors(targets);
    tasks.renderExecutors(targets);
  }

  function render(data) {
    executorTargetsSignature = "";
    headerStatus.version = text(data.version && data.version.version);
    headerStatus.authMode = text(data.ui && data.ui.auth_mode, config.authMode);
    renderExecutorTargets(Array.isArray(data.executor_targets) ? data.executor_targets : []);
    hideAuthentication();
    setConnection("Connected", "online");
  }

  function requireAuthentication() {
    cancelPendingReads();
    clearAuthenticatedViews();
    elements.terminalState.textContent = "Authentication required";
    healthView.renderStatus("Authentication required");
    showAuthentication("Authentication required");
  }

  function clearAuthenticatedViews() {
    dashboard.stopPolling();
    executors.stopPolling();
    dashboard.invalidate();
    tasks.invalidate();
    audit.invalidate();
    files.invalidate();
    terminal.reset("");
    dashboard.reset("");
    healthView.renderTargets(null);
    overviewLists.renderExecutors([], "", false);
    executors.reset("Authentication required");
    files.reset("");
    tasks.reset();
    dashboard.renderTasks([]); // Task shortcuts belong to the prior login.
    audit.reset();
  }

  async function load() {
    cancelPendingReads();
    // Initial deep links belong to the newly constructed controllers. Only
    // subsequent login/reload cycles may discard a previous user's selection.
    if (loadedOnce) clearAuthenticatedViews();
    loadedOnce = true;
    const generation = loadGeneration;
    setConnection("Connecting", "idle");
    try {
      const bootstrap = await request("/bootstrap");
      if (generation !== loadGeneration) return;
      render(bootstrap);
      // Each view starts as soon as Control authentication is established.
      // No slow/offline Executor request can delay another view.
      const onError = (error) => {
        if (generation === loadGeneration && error.authenticationRequired)
          requireAuthentication();
      };
      const start = (refresh, onUnavailable = () => {}) => {
        void refresh().catch((error) => {
          if (generation !== loadGeneration) return;
          if (error.authenticationRequired) return onError(error);
          if (!error.obsoleteRequest) onUnavailable(error);
        });
      };
      start(() => dashboard.refresh({ force: true }));
      dashboard.startPolling();
      start(() => executors.refresh({ force: true }));
      executors.startPolling();
      start(() => terminal.refresh(), (error) => {
        elements.terminalState.textContent = "Terminal list unavailable";
        terminal.showMessage(error instanceof Error ? error.message : String(error));
      });
      start(() => files.refresh(), (error) => {
        elements.fileState.textContent = "File list unavailable";
        files.showMessage("Files unavailable", error instanceof Error ? error.message : String(error));
      });
      start(() => tasks.refresh(), (error) => {
        elements.tasksState.textContent = error instanceof Error ? error.message : "Task list unavailable";
        elements.todoState.textContent = "Plan unavailable";
        elements.sessionAuditState.textContent = "Session Audit unavailable";
      });
      start(() => audit.refresh());
    } catch (error) {
      if (generation !== loadGeneration || error.obsoleteRequest) return;
      if (error.authenticationRequired) requireAuthentication();
      else setConnection("Unavailable", "error");
    }
  }

  async function boot() {
    try {
      await finishOAuthCallback();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await load();
      if (!elements.authPanel.hidden) {
        showAuthentication("Unable to sign in", message);
      }
      return;
    }
    await load();
  }

  elements.authForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    terminal.close();
    const token = elements.tokenInput.value.trim();
    if (!token) {
      showAuthentication("Access token required", "Paste a valid OAuth access token.");
      return;
    }
    elements.tokenInput.disabled = true;
    try {
      const response = await fetch(oauthEndpoint("sessionTokenEndpoint"), {
        method: "POST",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
          [sessionBindingHeaderName]: ensureSessionBindingToken(),
        },
        cache: "no-store",
        credentials: "same-origin",
      });
      const result = await responsePayload(response);
      if (!response.ok || !result.ok) {
        throw new Error(result.message || result.detail || "Unable to establish WebUI session.");
      }
      announceSessionEstablished();
      elements.tokenInput.value = "";
      await load();
    } catch (error) {
      showAuthentication(
        "Unable to sign in",
        error instanceof Error ? error.message : String(error),
      );
    } finally {
      elements.tokenInput.disabled = false;
    }
  });

  elements.oauthLogin.addEventListener("click", () => void startOAuth());
  async function signOut() {
    cancelPendingReads();
    clearAuthenticatedViews();
    try {
      await request("/session/logout", { method: "POST" });
    } catch {
      // Local UI state is still cleared when the server session already expired.
    }
    terminal.reset("");
    dashboard.stopPolling();
    executors.stopPolling();
    dashboard.reset("");
    healthView.renderTargets(null);
    overviewLists.renderExecutors([], "", false);
    executors.reset("Authentication required");
    files.reset("");
    tasks.reset();
    audit.reset();
    elements.terminalState.textContent = "Authentication required";
    healthView.renderStatus("Authentication required");
    elements.fileState.textContent = "Authentication required";
    elements.tasksState.textContent = "Authentication required";
    elements.todoState.textContent = "Authentication required";
    elements.sessionAuditState.textContent = "Authentication required";
    elements.auditState.textContent = "Authentication required";
    sessionStorage.removeItem(pendingStorageKey);
    clearSessionBindingToken();
    elements.tokenInput.value = "";
    showAuthentication("Signed out", "The persistent browser session was cleared.");
  }

  function navigateTo(target) {
    setNavigationOpen(false);
    const view = normalizeView(target);
    const url = new URL(location.href);
    for (const key of ["task_id", "session_id", "executor_id", "workdir", "shell_id"]) {
      url.searchParams.delete(key);
    }
    url.hash = view;
    history.pushState({}, "", url);
    setActiveView(view, { syncHash: false });
    if (view === "audit") audit.showGlobalAudit();
  }
  document.addEventListener("pointerdown", (event) => {
    if (elements.appHeader.classList.contains("menu-open") && !elements.appHeader.contains(event.target)) {
      setNavigationOpen(false);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && elements.appHeader.classList.contains("menu-open")) {
      setNavigationOpen(false);
      document.getElementById("nav-toggle")?.focus();
    }
  });
  window.matchMedia("(max-width: 1160px)").addEventListener("change", () => setNavigationOpen(false));
  // Dashboard shortcuts are still WorkGate-owned buttons; the brand navigation
  // is now React-owned, mounted after these listeners are registered.
  for (const link of document.querySelectorAll("[data-go-view]")) {
    link.addEventListener("click", () => navigateTo(link.dataset.goView));
  }
  const restoreViewFromLocation = () => {
    setNavigationOpen(false);
    setActiveView(viewFromLocation(), { syncHash: false });
  };
  window.addEventListener("popstate", restoreViewFromLocation);
  window.addEventListener("hashchange", restoreViewFromLocation);
  window.addEventListener("storage", (event) => {
    if (event.key === sessionBindingStorageKey) {
      if (event.oldValue === null && event.newValue !== null) return;
    } else if (event.key !== sessionEstablishedStorageKey) {
      return;
    }
    void load();
  });

  window.addEventListener("resize", () => window.requestAnimationFrame(terminal.resize));
  window.addEventListener("beforeunload", () => {
    dashboard.stopPolling();
    executors.stopPolling();
    terminal.close();
  });
  elements.oauthLogin.hidden = !oauthAvailable();
  setActiveView(viewFromLocation(initialUiLocation), { replaceHash: true });
  void boot();
  window.setInterval(() => {
    terminal.ping(); // Keep an attached terminal alive while the tab is hidden.
    if (document.hidden) return;
    if (config.authMode !== "oauth" || authenticated) {
      const view = document.body.dataset.activeView;
      if (view === "tasks" || view === "overview") {
        void tasks.refresh({ background: true }).catch((error) => {
          if (error.authenticationRequired) void load();
        });
      }
      if (view === "audit") void audit.refresh({ background: true });
    }
  }, 30000);
})();
