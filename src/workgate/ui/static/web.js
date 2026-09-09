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
    { createAuditView },
    { createAuditController },
    { createTerminalController },
    { createFilesController },
    { createSessionsController },
  ] = await Promise.all([
    import(assetUrl("dashboard.js")),
    import(assetUrl("executors.js")),
    import(assetUrl("audit_view.js")),
    import(assetUrl("audit.js")),
    import(assetUrl("terminal.js")),
    import(assetUrl("files.js")),
    import(assetUrl("sessions.js")),
  ]);
  const apiPrefix = String(config.apiPrefix || "/api/ui").replace(/\/$/, "");
  const oauth = config.oauth && typeof config.oauth === "object" ? config.oauth : null;
  const wallpaper = ["aurora", "grid", "none"].includes(String(config.wallpaper || ""))
    ? String(config.wallpaper)
    : "aurora";
  document.body.dataset.wallpaper = wallpaper;
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
  const viewDefinitions = Object.freeze({
    overview: {
      title: "Overview",
      description: "System health across connected executor environments.",
    },
    executors: {
      title: "Executors",
      description: "Pair, inspect, rename, and revoke final executor identities.",
    },
    sessions: {
      title: "Sessions",
      description: "Inspect agent sessions, todos, lifecycle state, and scoped Audit records.",
    },
    terminals: {
      title: "Terminals",
      description: "Persistent executor terminals with interactive streaming.",
    },
    files: {
      title: "Files",
      description: "Browse and edit workspace-scoped files on the selected executor.",
    },
    audit: {
      title: "Audit",
      description: "Search control-owned product and tool activity.",
    },
    console: {
      title: "OpenTUI",
      description: "Run the optional terminal-native interface inside the browser.",
    },
  });
  const encoder = new TextEncoder();
  let authenticated = config.authMode !== "oauth";
  const elements = {
    appNavItems: Array.from(document.querySelectorAll(".nav-item[data-view]")),
    appViews: Array.from(document.querySelectorAll("[data-app-view]")),
    pageDescription: document.getElementById("page-description"),
    pageTitle: document.getElementById("page-title"),
    dashboardActivity: document.getElementById("dashboard-activity"),
    dashboardAlertCount: document.getElementById("dashboard-alert-count"),
    dashboardAlerts: document.getElementById("dashboard-alerts"),
    dashboardAuditDetail: document.getElementById("dashboard-audit-detail"),
    dashboardAuditTotal: document.getElementById("dashboard-audit-total"),
    dashboardCpu: document.getElementById("dashboard-cpu"),
    dashboardCpuBar: document.getElementById("dashboard-cpu-bar"),
    dashboardCpuCount: document.getElementById("dashboard-cpu-count"),
    dashboardCpuTrend: document.getElementById("dashboard-cpu-trend"),
    dashboardCpuValue: document.getElementById("dashboard-cpu-value"),
    dashboardDisk: document.getElementById("dashboard-disk"),
    dashboardDiskBar: document.getElementById("dashboard-disk-bar"),
    dashboardDiskTrend: document.getElementById("dashboard-disk-trend"),
    dashboardDiskUsed: document.getElementById("dashboard-disk-used"),
    dashboardDiskValue: document.getElementById("dashboard-disk-value"),
    dashboardGenerated: document.getElementById("dashboard-generated"),
    dashboardHealth: document.getElementById("dashboard-health"),
    dashboardHealthCard: document.getElementById("dashboard-health-card"),
    dashboardHealthDetail: document.getElementById("dashboard-health-detail"),
    dashboardLoad: document.getElementById("dashboard-load"),
    dashboardExecutor: document.getElementById("dashboard-executor"),
    dashboardMemory: document.getElementById("dashboard-memory"),
    dashboardMemoryBar: document.getElementById("dashboard-memory-bar"),
    dashboardMemoryTrend: document.getElementById("dashboard-memory-trend"),
    dashboardMemoryUsed: document.getElementById("dashboard-memory-used"),
    dashboardMemoryValue: document.getElementById("dashboard-memory-value"),
    dashboardNetwork: document.getElementById("dashboard-network"),
    dashboardNetworkRx: document.getElementById("dashboard-network-rx"),
    dashboardNetworkTrend: document.getElementById("dashboard-network-trend"),
    dashboardNetworkTx: document.getElementById("dashboard-network-tx"),
    dashboardPlatform: document.getElementById("dashboard-platform"),
    dashboardPython: document.getElementById("dashboard-python"),
    dashboardRefresh: document.getElementById("dashboard-refresh"),
    dashboardSourceState: document.getElementById("dashboard-source-state"),
    dashboardState: document.getElementById("dashboard-state"),
    dashboardUptime: document.getElementById("dashboard-uptime"),
    dashboardVersion: document.getElementById("dashboard-version"),
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
    authMode: document.getElementById("auth-mode"),
    authPanel: document.getElementById("auth-panel"),
    connectionState: document.getElementById("connection-state"),
    fileCopy: document.getElementById("file-copy"),
    fileDelete: document.getElementById("file-delete"),
    fileEdit: document.getElementById("file-edit"),
    fileEditor: document.getElementById("file-editor"),
    fileEditorCancel: document.getElementById("file-editor-cancel"),
    fileEditorForm: document.getElementById("file-editor-form"),
    fileList: document.getElementById("file-list"),
    fileExecutor: document.getElementById("file-executor"),
    fileMove: document.getElementById("file-move"),
    fileNew: document.getElementById("file-new"),
    fileOpen: document.getElementById("file-open"),
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
    lastUpdated: document.getElementById("last-updated"),
                oauthLogin: document.getElementById("oauth-login"),
    executorDetailCreated: document.getElementById("executor-detail-created"),
    executorDetailId: document.getElementById("executor-detail-id"),
    executorDetailLastSeen: document.getElementById("executor-detail-last-seen"),
    executorDetailName: document.getElementById("executor-detail-name"),
    executorDetailPlatform: document.getElementById("executor-detail-platform"),
    executorDetailStatus: document.getElementById("executor-detail-status"),
    executorDetailVersion: document.getElementById("executor-detail-version"),
    executorList: document.getElementById("executor-list"),
    executorOnline: document.getElementById("executor-online"),
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
    executorPairReplace: document.getElementById("executor-pair-replace"),
    executorPairReplaceWrap: document.getElementById("executor-pair-replace-wrap"),
    executorPairRequestedName: document.getElementById("executor-pair-requested-name"),
    executorPairReview: document.getElementById("executor-pair-review"),
    executorRefresh: document.getElementById("executor-refresh"),
    executorRenameDialog: document.getElementById("executor-rename-dialog"),
    executorRenameForm: document.getElementById("executor-rename-form"),
    executorRenameName: document.getElementById("executor-rename-name"),
    executorRenameOpen: document.getElementById("executor-rename-open"),
    executorRevokeDialog: document.getElementById("executor-revoke-dialog"),
    executorRevokeForm: document.getElementById("executor-revoke-form"),
    executorRevokeName: document.getElementById("executor-revoke-name"),
    executorRevokeOpen: document.getElementById("executor-revoke-open"),
    executorRevoked: document.getElementById("executor-revoked"),
    executorState: document.getElementById("executor-state"),
    executorTargetOnline: document.getElementById("executor-target-online"),
    executorTargetTotal: document.getElementById("executor-target-total"),
    executorTotal: document.getElementById("executor-total"),
    refresh: document.getElementById("refresh"),
    signOut: document.getElementById("sign-out"),
    terminalInput: document.getElementById("terminal-input"),
    terminalInputForm: document.getElementById("terminal-input-form"),
    terminalKeyButtons: Array.from(document.querySelectorAll("[data-terminal-key]")),
    terminalKill: document.getElementById("terminal-kill"),
    terminalLatest: document.getElementById("terminal-latest"),
    terminalList: document.getElementById("terminal-list"),
    terminalExecutor: document.getElementById("terminal-executor"),
    terminalName: document.getElementById("terminal-name"),
    terminalOutput: document.getElementById("terminal-output"),
    terminalPendingCount: document.getElementById("terminal-pending-count"),
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
    sessionDetailStatus: document.getElementById("session-detail-status"),
        sessionDetailTitle: document.getElementById("session-detail-title"),
    sessionDetailUpdated: document.getElementById("session-detail-updated"),
    sessionDetailWorkdir: document.getElementById("session-detail-workdir"),
    sessionIncludeInactive: document.getElementById("session-include-inactive"),
    sessionList: document.getElementById("session-list"),
    sessionExecutor: document.getElementById("session-executor"),
    sessionRefresh: document.getElementById("session-refresh"),
    sessionState: document.getElementById("session-state"),
    sessionTerminate: document.getElementById("session-terminate"),
    todoAdd: document.getElementById("todo-add"),
    todoFilter: document.getElementById("todo-filter"),
    todoList: document.getElementById("todo-list"),
    todoRefresh: document.getElementById("todo-refresh"),
    todoSave: document.getElementById("todo-save"),
    todoState: document.getElementById("todo-state"),
    todoSummary: document.getElementById("todo-summary"),
    tokenInput: document.getElementById("access-token"),
    version: document.getElementById("version"),
  };
  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  const {
    auditEntryButton,
    auditEntryTitle,
    auditTimestamp,
    renderAuditDetailInto,
    renderAuditDetailMessage,
  } = createAuditView({ text, formatFileBytes });

  const audit = createAuditController({
    elements,
    request,
    text,
    auditEntryButton,
    auditEntryTitle,
    auditTimestamp,
    renderAuditDetailInto,
    renderAuditDetailMessage,
  });
  audit.bind();

  const terminal = createTerminalController({
    elements,
    request,
    text,
    encoder,
    uiPath,
    sessionBindingToken,
    sessionBindingProtocolPrefix,
    showAuthentication,
    onAuthenticationRequired: () => void load(),
  });
  terminal.bind();

  const files = createFilesController({
    elements,
    request,
    text,
    formatFileBytes,
  });
  files.bind();


  const sessions = createSessionsController({
    elements,
    request,
    text,
    auditEntryButton,
    auditEntryTitle,
    auditTimestamp,
    renderAuditDetailInto,
    renderAuditDetailMessage,
  });
  sessions.bind();

  const dashboard = createDashboardController({
    elements,
    request,
    text,
    authMode: config.authMode,
    isAuthenticated: () => authenticated,
    onAuthenticationRequired: () => void load(),
  });
  dashboard.bind();

  const executors = createExecutorsController({
    elements,
    request,
    authMode: config.authMode,
    isAuthenticated: () => authenticated,
    reloadApp: () => load(),
  });
  executors.bind();

  function normalizeView(value) {
    const candidate = String(value || "").replace(/^#/, "");
    if (!Object.prototype.hasOwnProperty.call(viewDefinitions, candidate)) return "overview";
    if (candidate === "console" && !config.opentuiAvailable) return "overview";
    return candidate;
  }

  function viewFromLocation() {
    const hashView = location.hash.slice(1);
    if (!hashView && location.pathname === "/pair") return "executors";
    return normalizeView(hashView);
  }

  function setActiveView(value, { syncHash = true, replaceHash = false } = {}) {
    const view = normalizeView(value);
    const definition = viewDefinitions[view];
    document.body.dataset.activeView = view;
    elements.pageTitle.textContent = definition.title;
    elements.pageDescription.textContent = definition.description;
    document.title = `${definition.title} · Workgate`;

    for (const item of elements.appNavItems) {
      const active = item.dataset.view === view;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
    for (const panel of elements.appViews) {
      panel.hidden = panel.dataset.appView !== view;
    }

    if (syncHash && location.hash !== `#${view}`) {
      const url = `${location.pathname}${location.search}#${view}`;
      if (replaceHash) history.replaceState({}, "", url);
      else history.pushState({}, "", url);
    }
    window.requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }

  function setConnection(label, state) {
    elements.connectionState.textContent = label;
    elements.connectionState.className = `status status-${state}`;
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
    elements.signOut.hidden = true;
    elements.tokenInput.setAttribute("aria-invalid", "true");
    elements.authDetail.textContent = detail || "Sign in through the Workgate OAuth approval page.";
    setConnection(message || "Authentication required", "error");
  }

  function hideAuthentication() {
    authenticated = true;
    elements.authPanel.hidden = true;
    elements.tokenInput.removeAttribute("aria-invalid");
    elements.signOut.hidden = config.authMode !== "oauth";
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
    const response = await fetch(`${apiPrefix}${path}`, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
    const payload = await responsePayload(response);
    if (response.status === 401) {
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

  function cleanCallbackUrl() {
    history.replaceState({}, "", uiPath);
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
    cleanCallbackUrl();
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

  function render(data) {
    const executorCounts = data.executor_counts || {};
    const executorTargets = Array.isArray(data.executor_targets) ? data.executor_targets : [];
    elements.version.textContent = text(data.version && data.version.version);
    elements.executorTargetTotal.textContent = text(executorCounts.total, executorTargets.length);
    elements.executorTargetOnline.textContent = text(executorCounts.online, "0");
    elements.authMode.textContent = text(data.ui && data.ui.auth_mode, config.authMode);
    elements.lastUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;

    dashboard.renderExecutors(executorTargets);
    terminal.renderExecutors(executorTargets);
    files.renderExecutors(executorTargets);
    sessions.renderExecutors(executorTargets);
    hideAuthentication();
    setConnection("Connected", "online");
  }

  async function load() {
    setConnection("Connecting", "idle");
    elements.refresh.disabled = true;
    try {
      render(await request("/bootstrap"));
      await dashboard.refresh({ force: true });
      dashboard.startPolling();
      await executors.refresh({ force: true });
      executors.startPolling();
      try {
        await terminal.refresh();
      } catch (error) {
        if (error.authenticationRequired) throw error;
        elements.terminalState.textContent = "Terminal list unavailable";
        terminal.showMessage(error instanceof Error ? error.message : String(error));
      }
      try {
        await files.refresh();
      } catch (error) {
        if (error.authenticationRequired) throw error;
        elements.fileState.textContent = "File list unavailable";
        files.showMessage("Files unavailable", error instanceof Error ? error.message : String(error));
      }
      try {
        await sessions.refresh();
      } catch (error) {
        if (error.authenticationRequired) throw error;
        elements.sessionState.textContent = error instanceof Error ? error.message : "Session list unavailable";
        elements.todoState.textContent = "Session Todo unavailable";
        elements.sessionAuditState.textContent = "Session Audit unavailable";
      }
      await audit.refresh();
    } catch (error) {
      if (error.authenticationRequired) {
        terminal.reset("");
        elements.terminalState.textContent = "Authentication required";
        dashboard.stopPolling();
        executors.stopPolling();
        dashboard.invalidate();
        files.invalidate();
        sessions.invalidate();
        audit.invalidate();

        dashboard.reset("");
        executors.reset("Authentication required");
        elements.dashboardState.textContent = "Authentication required";
        showAuthentication("Authentication required");
      } else {
        setConnection("Unavailable", "error");
        elements.lastUpdated.textContent = error instanceof Error ? error.message : String(error);
      }
    } finally {
      elements.refresh.disabled = false;
    }
  }

  async function boot() {
    try {
      await finishOAuthCallback();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await load();
      if (elements.authPanel.hidden) {
        elements.lastUpdated.textContent = `OAuth callback ignored: ${message}`;
      } else {
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
        throw new Error(result.message || result.detail || "Unable to establish Human UI session.");
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
  elements.refresh.addEventListener("click", () => void load());
  elements.signOut.addEventListener("click", async () => {
    try {
      await request("/session/logout", { method: "POST" });
    } catch {
      // Local UI state is still cleared when the server session already expired.
    }
    terminal.reset("");
    dashboard.stopPolling();
    executors.stopPolling();
    dashboard.reset("");
    executors.reset("Authentication required");
    files.reset("");
    sessions.reset("");
    audit.reset();
    elements.terminalState.textContent = "Authentication required";
    elements.dashboardState.textContent = "Authentication required";
    elements.fileState.textContent = "Authentication required";
    elements.sessionState.textContent = "Authentication required";
    elements.todoState.textContent = "Authentication required";
    elements.sessionAuditState.textContent = "Authentication required";
    elements.auditState.textContent = "Authentication required";
    sessionStorage.removeItem(pendingStorageKey);
    clearSessionBindingToken();
    elements.tokenInput.value = "";
    showAuthentication("Signed out", "The persistent browser session was cleared.");
  });

  for (const item of elements.appNavItems) {
    item.addEventListener("click", () => setActiveView(item.dataset.view));
  }
  if (!config.opentuiAvailable) {
    const consoleNav = elements.appNavItems.find((item) => item.dataset.view === "console");
    if (consoleNav) consoleNav.hidden = true;
  }
  const restoreViewFromLocation = () => setActiveView(viewFromLocation(), { syncHash: false });
  window.addEventListener("popstate", restoreViewFromLocation);
  window.addEventListener("hashchange", restoreViewFromLocation);
  window.addEventListener("storage", (event) => {
    if (event.key === sessionBindingStorageKey) {
      if (event.oldValue === null && event.newValue !== null) return;
    } else if (event.key !== sessionEstablishedStorageKey) {
      return;
    }
    terminal.close();
    void load();
  });

  window.addEventListener("resize", () => window.requestAnimationFrame(terminal.resize));
  window.addEventListener("beforeunload", () => {
    dashboard.stopPolling();
    executors.stopPolling();
    terminal.close();
  });
  elements.oauthLogin.hidden = !oauthAvailable();
  elements.authMode.textContent = text(config.authMode);
  setActiveView(viewFromLocation(), { replaceHash: true });
  void boot();
  window.setInterval(() => {
    terminal.ping();
    if (config.authMode !== "oauth" || authenticated) void load();
  }, 30000);
})();
