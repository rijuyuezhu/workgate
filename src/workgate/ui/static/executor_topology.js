// Directional, cached-only peer evidence rendered as a small SVG/DOM graph.
// GET reads never initiate a probe. Only a deliberately clicked Test button POSTs.
const SMALL_LIMIT = 5;
const UNKNOWN = Object.freeze({ status: "unknown" });
const EVIDENCE_FRESH_MS = 300_000;
const MAX_RESULTS = 512;
const STATUS_TEXT = Object.freeze({
  peer_succeeded: "Direct OK",
  unreachable: "Failed",
  ineligible: "Not eligible",
  stale: "Expired",
  untested: "Not tested",
  unknown: "Unknown",
});
const pairKey = (a, b) => [a, b].sort().join("::");
const directedKey = (a, b) => a + "\u0000" + b;

function pairStatus(forward, reverse) {
  const states = [forward.status, reverse.status];
  if (states.every((value) => value === "peer_succeeded")) return "verified";
  if (states.includes("peer_succeeded")) return "one-way";
  if (states.includes("stale")) return "stale";
  if (states.includes("unreachable")) return "unreachable";
  if (states.includes("ineligible")) return "ineligible";
  if (states.includes("unknown")) return "unknown";
  return "untested";
}

function readablePairStatus(status) {
  return {
    verified: "Direct OK both ways",
    "one-way": "Direct OK one way",
    stale: "Expired result",
    unreachable: "Direct failed",
    ineligible: "Direct not eligible",
    unknown: "Unknown",
    untested: "Not tested",
  }[status] || "Not tested";
}

function timestamp(value) {
  const time = Date.parse(value || "");
  return Number.isFinite(time) ? new Date(time).toLocaleString() : "";
}

const REASONS = Object.freeze({
  executor_offline: "Executor offline",
  measurement_expired: "Measurement expired",
  overlay_unavailable: "Tailscale unavailable",
  tailnet_mismatch: "Different tailnets",
  receiver_unavailable: "Destination unavailable",
  peer_handshake_failed: "Handshake failed",
  evidence_unavailable: "Evidence unavailable",
});

function nodePositions(rows, mode, focusId) {
  if (mode === "pair") {
    return new Map(rows.map((row, index) => [row.executor_id, {
      x: index ? 0.73 : 0.27, y: 0.5,
    }]));
  }
  if (mode === "all" && rows.length === 2) {
    return new Map(rows.map((row, index) => [row.executor_id, {
      x: index === 0 ? 0.27 : 0.73, y: 0.5,
    }]));
  }
  if (mode === "focus") {
    const others = rows.filter((row) => row.executor_id !== focusId);
    const mobile = window.matchMedia("(max-width: 780px)").matches;
    const perRing = mobile ? 14 : 10;
    const rings = Math.max(1, Math.ceil(others.length / perRing));
    const positions = new Map([[focusId, { x: 0.5, y: 0.5 }]]);
    others.forEach((row, index) => {
      const ring = Math.floor(index / perRing);
      const inRing = Math.min(perRing, others.length - ring * perRing);
      const angle = -Math.PI / 2 + (2 * Math.PI * (index % perRing)) / inRing;
      const inner = Math.max(0.14, 0.22 - 0.02 * (rings - 2));
      const radius = mobile
        ? (rings === 1 ? 0.39 : inner + ((0.43 - inner) * ring) / (rings - 1))
        : rings === 1 ? 0.33
          : rings === 2 ? 0.27 + ring * 0.15
            : 0.16 + (0.27 * ring) / (rings - 1);
      positions.set(row.executor_id, {
        x: 0.5 + radius * Math.cos(angle),
        y: 0.5 + radius * Math.sin(angle),
      });
    });
    return positions;
  }
  const positions = new Map();
  rows.forEach((row, index) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * index) / Math.max(1, rows.length);
    positions.set(row.executor_id, {
      x: 0.5 + 0.36 * Math.cos(angle),
      y: 0.5 + 0.34 * Math.sin(angle),
    });
  });
  return positions;
}

export function createExecutorTopology({ request, mountTopology, onAuthenticationRequired = () => {}, onClose = () => {} }) {
  const panel = document.getElementById("executors-panel");
  const root = document.getElementById("executor-topology");
  const graph = document.getElementById("topology-graph");
  const stage = document.getElementById("topology-stage");
  const inspector = document.getElementById("topology-inspector");
  const directionRows = document.getElementById("topology-direction-rows");
  const source = document.getElementById("topology-source");
  const destination = document.getElementById("topology-destination");
  const feedback = document.getElementById("topology-feedback");
  const summary = document.getElementById("topology-summary");
  const instructions = document.getElementById("topology-instructions");
  const reset = document.getElementById("topology-reset");
  const refresh = document.getElementById("topology-refresh");
  const testAll = document.getElementById("topology-test-all");
  const testProgress = document.getElementById("topology-test-progress");
  const choosePair = document.getElementById("topology-show-pair");
  const back = document.getElementById("topology-back");
  const state = {
    rows: [], mode: "all", focusId: "", pair: null, open: false,
    centerOnDraw: false,
    results: new Map(), pending: new Map(), revisions: new Map(),
    activeTest: "", bulk: null, requestGeneration: 0, renderedStatus: "",
  };

  const topologyView = mountTopology(stage, directionRows, {
    pickPair, focus, test: (a, b) => void testDirection(a, b),
  });

  function names(id) {
    const row = state.rows.find((item) => item.executor_id === id);
    return row?.name || id;
  }

  function nodes() {
    if (state.mode === "pair" && state.pair) {
      return state.rows.filter((row) => state.pair.includes(row.executor_id));
    }
    return state.rows;
  }

  function pairs() {
    const rows = nodes();
    if (state.mode === "grid") return [];
    if (state.mode === "pair") return state.pair ? [state.pair] : [];
    if (state.mode === "focus") {
      return rows.filter((row) => row.executor_id !== state.focusId)
        .map((row) => [state.focusId, row.executor_id]);
    }
    const all = [];
    for (let i = 0; i < rows.length; i++) {
      for (let j = i + 1; j < rows.length; j++) {
        all.push([rows[i].executor_id, rows[j].executor_id]);
      }
    }
    return all;
  }

  function observed(a, b) {
    const record = state.results.get(directedKey(a, b)) || UNKNOWN;
    if (!["peer_succeeded", "unreachable", "ineligible"].includes(record.status)) return record;
    const observedTime = Date.parse(record.tested_at || "");
    const expired = !Number.isFinite(observedTime) || Date.now() - observedTime >= EVIDENCE_FRESH_MS;
    const offline = [a, b].some((id) => {
      const row = state.rows.find((item) => item.executor_id === id);
      return !row || !row.online || row.revoked_at;
    });
    return expired || offline ? {
      ...record, status: "stale", previous_status: record.status,
      reason: expired ? "measurement_expired" : "executor_offline",
    } : record;
  }

  function edgeOutcome(a, b) {
    return pairStatus(observed(a, b), observed(b, a));
  }

  function pickPair(a, b) {
    feedback.textContent = "";
    state.pair = [a, b];
    source.value = a;
    destination.value = b;
    render();
  }

  function focus(id) {
    stopBulk();
    feedback.textContent = "";
    state.focusId = id;
    state.mode = "focus";
    state.centerOnDraw = true;
    state.pair = null;
    render();
    void fetchVisible();
  }

  function draw() {
    const rows = nodes();
    graph.dataset.layout = state.mode;
    graph.dataset.size = String(rows.length);
    graph.classList.toggle("topology-empty", rows.length === 0);
    const mobile = window.matchMedia("(max-width: 780px)").matches;
    const rings = Math.ceil(Math.max(0, rows.length - 1) / (mobile ? 14 : 10));
    const pannable = state.mode === "focus" && mobile && rings >= 2;
    graph.dataset.scrollable = String(pannable);
    stage.style.width = pannable ? `${840 + (rings - 2) * 150}px` : "100%";
    stage.style.height = pannable ? `${850 + (rings - 2) * 320}px` : "100%";
    graph.style.height = pannable ? "520px"
      : state.mode === "focus" && !mobile && rings >= 3 ? `${rings * 270 + 100}px`
        : "";
    const positions = nodePositions(rows, state.mode, state.focusId);
    const currentPairs = pairs();
    state.renderedStatus = currentPairs.flatMap(([a, b]) => [observed(a, b).status, observed(b, a).status]).join("|");
    const width = 1000;
    const height = 660;
    const edges = state.mode === "grid" ? [] : currentPairs.flatMap(([a, b]) => {
      const start = positions.get(a);
      const end = positions.get(b);
      if (!start || !end) return [];
      const status = edgeOutcome(a, b);
      return [{
        key: pairKey(a, b), a, b, status,
        selected: Boolean(state.pair && pairKey(...state.pair) === pairKey(a, b)),
        label: `Inspect connection between ${names(a)} and ${names(b)}`,
        title: `${names(a)} ↔ ${names(b)}: ${readablePairStatus(status)}. Click to inspect.`,
        x1: start.x * width, y1: start.y * height,
        x2: end.x * width, y2: end.y * height,
      }];
    });
    const nodeRows = rows.map((row) => {
      const id = row.executor_id;
      const at = state.mode === "grid" ? null : positions.get(id);
      return {
        id, name: row.name || id, online: Boolean(row.online) && !row.revoked_at,
        presence: row.revoked_at ? "Revoked" : row.online ? "Online" : "Offline",
        focused: state.focusId === id && state.mode === "focus",
        left: at ? (at.x * 100).toFixed(3) + "%" : undefined,
        top: at ? (at.y * 100).toFixed(3) + "%" : undefined,
      };
    });
    topologyView.renderGraph({edges, nodes: nodeRows});
    if (state.centerOnDraw) {
      state.centerOnDraw = false;
      if (pannable) {
        graph.scrollLeft = (stage.scrollWidth - graph.clientWidth) / 2;
        graph.scrollTop = (stage.scrollHeight - graph.clientHeight) / 2;
      }
    }
    const modeLabel = state.mode === "grid" ? "Nodes only" :
      state.mode === "focus" ? `Focused on ${names(state.focusId)}` :
        state.mode === "pair" ? "Selected pair" : "All pairs";
    summary.textContent = `${state.rows.length} Executors, ${currentPairs.length} pairs (${modeLabel})`;
    instructions.textContent = state.mode === "grid"
      ? "Click a node to show its links, or inspect a pair above."
      : pannable ? "Scroll inside the graph and click a line to inspect it."
        : "Click a line to inspect it, or click a node to focus its links.";
    reset.textContent = state.rows.length <= SMALL_LIMIT ? "All pairs" : "All nodes";
    reset.disabled = state.mode === (state.rows.length <= SMALL_LIMIT ? "all" : "grid");
  }

  function directionEvidence(from, to) {
    const row = observed(from, to);
    return {
      from, to, label: `${names(from)} → ${names(to)}`,
      testLabel: `Test connection from ${names(from)} to ${names(to)}`,
      status: row.status, statusLabel: STATUS_TEXT[row.status] || STATUS_TEXT.unknown,
      details: [timestamp(row.tested_at),
        row.reason && row.reason !== "measurement_expired" ? REASONS[row.reason] || String(row.reason).replaceAll("_", " ") : "",
        row.fallback ? "Relay not tested" : ""].filter(Boolean),
    };
  }

  function renderInspector() {
    inspector.hidden = !state.pair || !state.pair.every((id) => state.rows.some((row) => row.executor_id === id));
    if (inspector.hidden) {
      topologyView.renderDirections([], Boolean(state.activeTest));
      return;
    }
    const [a, b] = state.pair;
    document.getElementById("topology-pair-title").textContent = `${names(a)} ↔ ${names(b)}`;
    const badge = document.getElementById("topology-pair-status");
    const status = edgeOutcome(a, b);
    badge.dataset.status = status;
    badge.textContent = readablePairStatus(status);
    topologyView.renderDirections([directionEvidence(a, b), directionEvidence(b, a)], Boolean(state.activeTest));
  }

  function render() {
    if (!state.open) return;
    draw();
    renderInspector();
    choosePair.disabled = !source.value || !destination.value || source.value === destination.value;
    testAll.textContent = state.bulk ? state.bulk.cancelled ? "Stopping…" : "Stop" : "Test all links";
    testAll.disabled = state.bulk ? state.bulk.cancelled : Boolean(state.activeTest) || pairs().length === 0;
  }

  function stopBulk() {
    if (!state.bulk) return;
    state.bulk.cancelled = true;
    if (state.open) {
      testAll.disabled = true;
      testAll.textContent = "Stopping…";
    }
  }

  function showBulkProgress(message) {
    testProgress.hidden = !message;
    testProgress.textContent = message;
  }

  async function probeDirection(a, b) {
    const measured = await request("/executors/peer-link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_executor_id: a, destination_executor_id: b }),
    });
    // POST is authoritative; an older cached GET must not replace its evidence.
    const key = directedKey(a, b);
    state.revisions.set(key, (state.revisions.get(key) || 0) + 1);
    state.results.delete(key);
    state.results.set(key, measured);
    if (state.results.size > MAX_RESULTS) state.results.delete(state.results.keys().next().value);
    if (state.open) render();
  }

  async function testVisibleLinks() {
    if (!state.open || state.bulk || state.activeTest) return;
    const directions = pairs().flatMap(([a, b]) => [[a, b], [b, a]]);
    if (!directions.length) return;
    const run = { total: directions.length, done: 0, errors: 0, cancelled: false, failure: "" };
    state.bulk = run;
    showBulkProgress(`Testing 0/${run.total} directions…`);
    render();
    for (const [a, b] of directions) {
      if (run.cancelled) break;
      try {
        await probeDirection(a, b);
      } catch (error) {
        if (error.authenticationRequired) {
          onAuthenticationRequired();
        }
        run.errors++;
        if (error.authenticationRequired || [401, 403, 429].includes(error.status)) {
          run.cancelled = true;
          run.failure = error.status === 429 ? "Probe rate limit reached"
            : error.status === 403 ? "Permission denied" : "Authentication required";
        }
      }
      run.done++;
      if (state.open) showBulkProgress(`Testing ${run.done}/${run.total} directions…`);
    }
    if (state.bulk === run) {
      state.bulk = null;
      if (state.open) {
        showBulkProgress(`${run.cancelled ? "Stopped" : "Finished"}: ${run.done}/${run.total} directions${run.errors ? `, ${run.errors} could not be tested` : ""}${run.failure ? ` (${run.failure})` : ""}.`);
        render();
      }
    }
  }

  async function fetchDirection(a, b, force = false) {
    const key = directedKey(a, b);
    if (!force && state.results.has(key)) return;
    if (state.pending.has(key)) return state.pending.get(key);
    const stamp = state.requestGeneration;
    const revision = state.revisions.get(key) || 0;
    const pending = request(`/executors/peer-link?source_executor_id=${encodeURIComponent(a)}&destination_executor_id=${encodeURIComponent(b)}`)
      .then((data) => {
        if (stamp !== state.requestGeneration || revision !== (state.revisions.get(key) || 0)) return;
        state.results.delete(key);
        state.results.set(key, data && typeof data.status === "string" ? data : UNKNOWN);
        if (state.results.size > MAX_RESULTS) state.results.delete(state.results.keys().next().value);
      }).catch((error) => {
        if (error.authenticationRequired) onAuthenticationRequired();
        if (stamp !== state.requestGeneration || revision !== (state.revisions.get(key) || 0)) return;
        state.results.delete(key);
        state.results.set(key, { status: "unknown", reason: "evidence_unavailable" });
        if (state.results.size > MAX_RESULTS) state.results.delete(state.results.keys().next().value);
      }).finally(() => {
        if (state.pending.get(key) === pending) state.pending.delete(key);
        if (stamp === state.requestGeneration && state.open) render();
      });
    state.pending.set(key, pending);
    return pending;
  }

  async function fetchVisible(force = false) {
    if (!state.open) return;
    const directed = [];
    for (const [a, b] of pairs()) directed.push([a, b], [b, a]);
    let index = 0;
    const worker = async () => {
      while (index < directed.length && state.open) {
        const [a, b] = directed[index++];
        await fetchDirection(a, b, force);
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, directed.length) }, () => worker()));
  }

  async function testDirection(a, b) {
    if (!state.open || state.activeTest || state.bulk) return;
    const generation = state.requestGeneration;
    const selection = state.pair ? pairKey(...state.pair) : "";
    const isSelected = () => state.open && generation === state.requestGeneration &&
      state.pair && pairKey(...state.pair) === selection;
    state.activeTest = directedKey(a, b);
    feedback.textContent = `Checking ${names(a)} → ${names(b)}…`;
    render();
    try {
      await probeDirection(a, b);
      if (isSelected()) feedback.textContent = `Measurement finished for ${names(a)} → ${names(b)}.`;
    } catch (error) {
      if (error.authenticationRequired) onAuthenticationRequired();
      if (isSelected()) feedback.textContent = error instanceof Error ? error.message : "Connection test unavailable";
    } finally {
      state.activeTest = "";
      if (state.open) render();
    }
  }

  function update(rows) {
    const next = Array.isArray(rows) ? rows.slice().sort((a, b) =>
      (a.name || a.executor_id).localeCompare(b.name || b.executor_id) ||
      a.executor_id.localeCompare(b.executor_id)) : [];
    const before = state.rows.map((item) => `${item.executor_id}:${item.name}:${item.online}:${item.revoked_at || ""}`).join("|");
    const after = next.map((item) => `${item.executor_id}:${item.name}:${item.online}:${item.revoked_at || ""}`).join("|");
    const changedIds = state.rows.map((item) => item.executor_id).join("|") !==
      next.map((item) => item.executor_id).join("|");
    state.rows = next;
    if (changedIds) {
      stopBulk();
      if (state.mode === "all" && next.length > SMALL_LIMIT) state.mode = "grid";
      const ids = new Set(next.map((item) => item.executor_id));
      if (!ids.has(state.focusId) && state.mode === "focus") state.mode = next.length <= SMALL_LIMIT ? "all" : "grid";
      if (state.pair?.some((id) => !ids.has(id))) state.pair = null;
      if (state.mode === "pair" && !state.pair) state.mode = next.length <= SMALL_LIMIT ? "all" : "grid";
      syncSelectors();
    }
    if (state.open) {
      // The Control's five-minute evidence TTL also expires without further
      // requests; update labels on the existing Executor inventory poll.
      const currentStatus = pairs().flatMap(([a, b]) => [observed(a, b).status, observed(b, a).status]).join("|");
      if (before !== after || currentStatus !== state.renderedStatus) render();
      if (before !== after && (changedIds || state.mode !== "grid")) void fetchVisible(true);
    }
  }

  function syncSelectors() {
    const sourceId = source.value;
    const destinationId = destination.value;
    source.replaceChildren();
    destination.replaceChildren();
    for (const row of state.rows) {
      source.add(new Option(row.name || row.executor_id, row.executor_id));
      destination.add(new Option(row.name || row.executor_id, row.executor_id));
    }
    source.value = state.rows.some((row) => row.executor_id === sourceId)
      ? sourceId : state.rows[0]?.executor_id || "";
    destination.value = state.rows.some((row) => row.executor_id === destinationId)
      ? destinationId : state.rows.find((row) => row.executor_id !== source.value)?.executor_id || "";
    choosePair.disabled = !source.value || !destination.value || source.value === destination.value;
  }

  function open() {
    state.open = true;
    state.mode = state.mode === "focus" && state.rows.some((row) => row.executor_id === state.focusId)
      ? state.mode : state.pair && state.mode === "pair"
        ? state.mode : state.rows.length <= SMALL_LIMIT ? "all" : "grid";
    panel.classList.add("topology-open");
    root.hidden = false;
    syncSelectors();
    render();
    void fetchVisible();
  }

  function close() {
    stopBulk();
    state.open = false;
    panel.classList.remove("topology-open");
    root.hidden = true;
    state.requestGeneration++;
    state.pending.clear();
    state.revisions.clear();
    feedback.textContent = "";
  }

  function bind() {
    back.addEventListener("click", () => { close(); onClose(); });
    reset.addEventListener("click", () => {
      stopBulk();
      feedback.textContent = "";
      state.mode = state.rows.length <= SMALL_LIMIT ? "all" : "grid";
      state.focusId = "";
      state.pair = null;
      render();
      void fetchVisible();
    });
    source.addEventListener("change", () => { choosePair.disabled = source.value === destination.value; });
    destination.addEventListener("change", () => { choosePair.disabled = source.value === destination.value; });
    choosePair.addEventListener("click", () => {
      if (!source.value || !destination.value || source.value === destination.value) return;
      stopBulk();
      feedback.textContent = "";
      state.mode = "pair";
      state.pair = [source.value, destination.value];
      state.focusId = "";
      render();
      void fetchVisible();
    });
    refresh.addEventListener("click", () => void fetchVisible(true));
    testAll.addEventListener("click", () => {
      if (state.bulk) stopBulk();
      else void testVisibleLinks();
    });
    window.addEventListener("resize", () => {
      if (state.open && state.mode === "focus") {
        state.centerOnDraw = true;
        render();
      }
    });
  }

  return { bind, open, close, update, isOpen: () => state.open };
}
