// Directional, cached-only peer evidence rendered as a small SVG/DOM graph.
// GET reads never initiate a probe. Only a deliberately clicked Test button POSTs.
const SMALL_LIMIT = 5;
const SVG_NS = "http://www.w3.org/2000/svg";
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

export function createExecutorTopology({ request, onAuthenticationRequired = () => {}, onClose = () => {} }) {
  const panel = document.getElementById("executors-panel");
  const root = document.getElementById("executor-topology");
  const graph = document.getElementById("topology-graph");
  const stage = document.getElementById("topology-stage");
  const edgeLayer = document.getElementById("topology-edges");
  const nodeLayer = document.getElementById("topology-nodes");
  const inspector = document.getElementById("topology-inspector");
  const directionRows = document.getElementById("topology-direction-rows");
  const source = document.getElementById("topology-source");
  const destination = document.getElementById("topology-destination");
  const feedback = document.getElementById("topology-feedback");
  const summary = document.getElementById("topology-summary");
  const instructions = document.getElementById("topology-instructions");
  const reset = document.getElementById("topology-reset");
  const refresh = document.getElementById("topology-refresh");
  const choosePair = document.getElementById("topology-show-pair");
  const back = document.getElementById("topology-back");
  const state = {
    rows: [], mode: "all", focusId: "", pair: null, open: false,
    centerOnDraw: false,
    results: new Map(), pending: new Map(),
    activeTest: "", requestGeneration: 0, renderedStatus: "",
  };

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
    feedback.textContent = "";
    state.focusId = id;
    state.mode = "focus";
    state.centerOnDraw = true;
    state.pair = null;
    render();
    void fetchVisible();
  }

  function newSvg(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    return node;
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
    edgeLayer.replaceChildren();
    nodeLayer.replaceChildren();
    const currentPairs = pairs();
    state.renderedStatus = currentPairs.map(([a, b]) => edgeOutcome(a, b)).join("|");
    const width = 1000;
    const height = 660;
    edgeLayer.setAttribute("viewBox", `0 0 ${width} ${height}`);
    edgeLayer.setAttribute("preserveAspectRatio", "none");

    if (state.mode !== "grid") {
      for (const [a, b] of currentPairs) {
        const start = positions.get(a);
        const end = positions.get(b);
        if (!start || !end) continue;
        const line = newSvg("g", {
          role: "button", tabindex: "0",
          "aria-label": `Inspect connection between ${names(a)} and ${names(b)}`,
          "data-pair": pairKey(a, b),
          "data-status": edgeOutcome(a, b),
        });
        if (state.pair && pairKey(...state.pair) === pairKey(a, b)) line.classList.add("selected");
        const coords = { x1: start.x * width, y1: start.y * height, x2: end.x * width, y2: end.y * height };
        const visible = newSvg("line", { ...coords, class: "topology-edge-line" });
        const hit = newSvg("line", { ...coords, class: "topology-edge-hit" });
        const title = newSvg("title", {});
        title.textContent = `${names(a)} ↔ ${names(b)}: ${readablePairStatus(edgeOutcome(a, b))}. Click to inspect.`;
        line.append(visible, hit, title);
        line.addEventListener("click", () => pickPair(a, b));
        line.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          pickPair(a, b);
        });
        edgeLayer.append(line);
      }
    }

    for (const row of rows) {
      const id = row.executor_id;
      const name = row.name || id;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "topology-node";
      button.dataset.executorId = id;
      button.dataset.online = String(Boolean(row.online) && !row.revoked_at);
      button.setAttribute("aria-label", `Focus connections for ${name} (${row.online && !row.revoked_at ? "online" : "offline"})`);
      button.setAttribute("aria-pressed", String(state.focusId === id && state.mode === "focus"));
      const mark = document.createElement("span");
      mark.className = "executor-band-mark";
      mark.textContent = name.slice(0, 1).toUpperCase();
      const label = document.createElement("span");
      label.className = "topology-node-name";
      label.textContent = name;
      const presence = document.createElement("span");
      presence.className = "topology-node-presence";
      presence.textContent = row.revoked_at ? "Revoked" : row.online ? "Online" : "Offline";
      button.append(mark, label, presence);
      if (state.mode !== "grid") {
        const at = positions.get(id);
        if (at) {
          button.style.left = (at.x * 100).toFixed(3) + "%";
          button.style.top = (at.y * 100).toFixed(3) + "%";
        }
      }
      button.addEventListener("click", () => focus(id));
      nodeLayer.append(button);
    }

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
    const wrapper = document.createElement("div");
    wrapper.className = "topology-direction";
    wrapper.dataset.status = row.status;
    const main = document.createElement("div");
    main.className = "topology-direction-main";
    const label = document.createElement("strong");
    label.textContent = `${names(from)} → ${names(to)}`;
    const status = document.createElement("span");
    status.className = "topology-direction-status";
    status.textContent = STATUS_TEXT[row.status] || STATUS_TEXT.unknown;
    const heading = document.createElement("div");
    heading.className = "topology-direction-title";
    heading.append(label, status);
    main.append(heading);
    const details = [timestamp(row.tested_at),
      row.reason && row.reason !== "measurement_expired" ? REASONS[row.reason] || String(row.reason).replaceAll("_", " ") : "",
      row.fallback ? "Relay not tested" : ""].filter(Boolean);
    if (details.length) {
      const meta = document.createElement("div");
      meta.className = "topology-direction-meta";
      for (const item of details) {
        const detail = document.createElement("span");
        detail.textContent = item;
        meta.append(detail);
      }
      main.append(meta);
    }
    const test = document.createElement("button");
    test.type = "button";
    test.textContent = "Test";
    test.disabled = Boolean(state.activeTest);
    test.setAttribute("aria-label", `Test connection from ${names(from)} to ${names(to)}`);
    test.addEventListener("click", () => void testDirection(from, to));
    wrapper.append(main, test);
    return wrapper;
  }

  function renderInspector() {
    inspector.hidden = !state.pair || !state.pair.every((id) => state.rows.some((row) => row.executor_id === id));
    if (inspector.hidden) {
      directionRows.replaceChildren();
      return;
    }
    const [a, b] = state.pair;
    document.getElementById("topology-pair-title").textContent = `${names(a)} ↔ ${names(b)}`;
    const badge = document.getElementById("topology-pair-status");
    const status = edgeOutcome(a, b);
    badge.dataset.status = status;
    badge.textContent = readablePairStatus(status);
    directionRows.replaceChildren(directionEvidence(a, b), directionEvidence(b, a));
  }

  function render() {
    if (!state.open) return;
    const focused = document.activeElement;
    const focusedEdge = focused?.getAttribute?.("data-pair");
    const focusedNode = focused?.getAttribute?.("data-executor-id");
    draw();
    renderInspector();
    choosePair.disabled = !source.value || !destination.value || source.value === destination.value;
    if (focusedEdge) {
      const edge = [...edgeLayer.querySelectorAll("g[data-pair]")]
        .find((item) => item.dataset.pair === focusedEdge);
      edge?.focus({ preventScroll: true });
    } else if (focusedNode) {
      const node = [...nodeLayer.querySelectorAll(".topology-node")]
        .find((item) => item.dataset.executorId === focusedNode);
      node?.focus({ preventScroll: true });
    }
  }

  async function fetchDirection(a, b, force = false) {
    const key = directedKey(a, b);
    if (!force && state.results.has(key)) return;
    if (state.pending.has(key)) return state.pending.get(key);
    const stamp = state.requestGeneration;
    const pending = request(`/executors/peer-link?source_executor_id=${encodeURIComponent(a)}&destination_executor_id=${encodeURIComponent(b)}`)
      .then((data) => {
        if (stamp !== state.requestGeneration) return;
        state.results.delete(key);
        state.results.set(key, data && typeof data.status === "string" ? data : UNKNOWN);
        if (state.results.size > MAX_RESULTS) state.results.delete(state.results.keys().next().value);
      }).catch((error) => {
        if (error.authenticationRequired) onAuthenticationRequired();
        if (stamp !== state.requestGeneration) return;
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
    if (!state.open || state.activeTest) return;
    const selection = state.pair ? pairKey(...state.pair) : "";
    const isSelected = () => state.open && state.pair && pairKey(...state.pair) === selection;
    state.activeTest = directedKey(a, b);
    feedback.textContent = `Checking ${names(a)} → ${names(b)}…`;
    renderInspector();
    try {
      await request("/executors/peer-link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_executor_id: a, destination_executor_id: b }),
      });
      state.results.delete(directedKey(a, b));
      await fetchDirection(a, b, true);
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
      const currentStatus = pairs().map(([a, b]) => edgeOutcome(a, b)).join("|");
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
    state.open = false;
    panel.classList.remove("topology-open");
    root.hidden = true;
    state.requestGeneration++;
    state.pending.clear();
    feedback.textContent = "";
  }

  function bind() {
    back.addEventListener("click", () => { close(); onClose(); });
    reset.addEventListener("click", () => {
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
      feedback.textContent = "";
      state.mode = "pair";
      state.pair = [source.value, destination.value];
      state.focusId = "";
      render();
      void fetchVisible();
    });
    refresh.addEventListener("click", () => void fetchVisible(true));
    window.addEventListener("resize", () => {
      if (state.open && state.mode === "focus") {
        state.centerOnDraw = true;
        render();
      }
    });
  }

  return { bind, open, close, update, isOpen: () => state.open };
}
