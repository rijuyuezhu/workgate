// Session-owned native GUI observation and explicitly armed one-shot input; no streaming or persistent frames.
export function createGuiViewer({ request, onOpen = () => {} }) {
  const panel = document.getElementById("session-gui");
  const layout = panel.closest(".task-sessions-layout");
  const openButton = document.getElementById("session-gui-open");
  const backButton = document.getElementById("session-gui-back");
  const state = document.getElementById("session-gui-state");
  const windows = document.getElementById("session-gui-windows");
  const load = document.getElementById("session-gui-load");
  const capture = document.getElementById("session-gui-capture");
  const image = document.getElementById("session-gui-image");
  const manual = document.getElementById("session-gui-manual");
  const manualControls = document.getElementById("session-gui-manual-controls");
  const pointerMode = document.getElementById("session-gui-pointer-mode");
  const focus = document.getElementById("session-gui-focus");
  const scrollUp = document.getElementById("session-gui-scroll-up");
  const scrollDown = document.getElementById("session-gui-scroll-down");
  const keyForm = document.getElementById("session-gui-key-form");
  const key = document.getElementById("session-gui-key");
  const textForm = document.getElementById("session-gui-text-form");
  const typed = document.getElementById("session-gui-text");
  let sessionId = "";
  let selectedKey = "";
  let generation = 0;
  let observation = null;
  let manualEnabled = false;
  let busy = false;
  let dragStart = null;
  let pointerAvailable = false;
  let pointerCapableWhileBusy = false;

  function setManual(enabled) {
    if (!enabled && manualEnabled) {
      generation++;
      observation = null;
      manual.disabled = true;
      state.textContent = "Manual mode off · capture again before further input";
    }
    manualEnabled = enabled && Boolean(sessionId) && Boolean(windows.value) && Boolean(observation);
    manualControls.hidden = !manualEnabled;
    manual.setAttribute("aria-pressed", String(manualEnabled));
    manual.textContent = manualEnabled ? "Exit manual mode" : "Enter manual mode";
    image.classList.toggle("session-gui-interactive", manualEnabled);
    image.tabIndex = manualEnabled ? 0 : -1;
    if (!manualEnabled && document.activeElement === image) image.blur();
    dragStart = null;
  }

  function close() {
    layout.classList.remove("gui-open");
    panel.hidden = true;
    setManual(false);
    generation++;
    clearImage();
  }

  function open() {
    if (!sessionId) return;
    onOpen();
    layout.classList.add("gui-open");
    panel.hidden = false;
    void loadWindows();
  }

  function invalidateObservation() {
    observation = null;
    pointerAvailable = false;
    pointerMode.disabled = true;
    scrollUp.disabled = true;
    scrollDown.disabled = true;
  }

  function clearImage() {
    invalidateObservation();
    image.removeAttribute("src");
    image.hidden = true;
  }

  function selectSession(session) {
    const id = String(session?.session_id || "");
    const status = String(session?.status || "");
    const availability = String(session?.availability || "");
    const nextKey = `${id}|${status}|${availability}`;
    if (nextKey === selectedKey) return;
    selectedKey = nextKey;
    sessionId = status === "active" && availability === "available" ? id : "";
    close();
    openButton.disabled = !sessionId;
    windows.replaceChildren(new Option("Select a window", ""));
    windows.disabled = true;
    load.disabled = !sessionId;
    capture.disabled = true;
    manual.disabled = true;
    clearImage();
    state.textContent = sessionId
      ? "Opening native window list…"
      : "GUI is only available for an active, connected session";
  }

  async function loadWindows() {
    if (!sessionId || busy) return;
    const currentSession = sessionId;
    const selectedWindow = windows.value;
    const revision = ++generation;
    load.disabled = true;
    capture.disabled = true;
    windows.disabled = true;
    state.textContent = "Refreshing window list…";
    try {
      const data = await request(`/gui/windows?${new URLSearchParams({ session_id: currentSession })}`);
      if (revision !== generation || sessionId !== currentSession) return;
      if (!Array.isArray(data?.windows)) throw new Error("Invalid native GUI window list");
      windows.replaceChildren(new Option("Select a window", ""));
      for (const window of data.windows) {
        const id = String(window.id || "");
        if (!id) continue;
        const label = String(window.title || window.app || id);
        windows.add(new Option(label, id));
      }
      if (selectedWindow && Array.from(windows.options).some((option) => option.value === selectedWindow)) {
        windows.value = selectedWindow;
      } else {
        clearImage();
      }
      windows.disabled = windows.options.length < 2;
      capture.disabled = !windows.value;
      manual.disabled = !windows.value || (!observation && !manualEnabled);
      if (!windows.value) setManual(false);
      const count = windows.options.length - 1;
      state.textContent = count
        ? `${count} window${count === 1 ? "" : "s"} · list refreshed at ${new Date().toLocaleTimeString()}`
        : "No native GUI windows available";
    } catch (error) {
      if (revision === generation && sessionId === currentSession) {
        windows.disabled = windows.options.length < 2;
        capture.disabled = !windows.value;
        manual.disabled = !windows.value || (!observation && !manualEnabled);
        state.textContent = error instanceof Error ? error.message : String(error);
      }
    } finally {
      if (revision === generation && sessionId === currentSession) load.disabled = false;
    }
  }

  async function captureWindow() {
    const id = windows.value;
    if (!sessionId || !id) return;
    const currentSession = sessionId;
    const revision = ++generation;
    capture.disabled = true;
    // Keep the previous frame in the layout so capturing does not reset scroll.
    // The old state token is invalid immediately; it cannot authorize input.
    invalidateObservation();
    manual.disabled = true;
    state.textContent = "Capturing window…";
    try {
      const data = await request(`/gui/state?${new URLSearchParams({ session_id: currentSession, window_id: id })}`);
      if (revision !== generation || sessionId !== currentSession || windows.value !== id) return;
      const mime = String(data?.image?.mime_type || "");
      if (!["image/png", "image/jpeg", "image/webp"].includes(mime) || !data.image.data) {
        throw new Error("Native GUI screenshot is unavailable");
      }
      const bounds = data?.window?.bounds;
      if (typeof data.state_id === "string" && /^[0-9a-f]{32}$/.test(data.state_id)
        && Number.isSafeInteger(bounds?.width) && Number.isSafeInteger(bounds?.height)
        && bounds.width > 0 && bounds.height > 0) {
        observation = { id: data.state_id, width: bounds.width, height: bounds.height,
          expires: performance.now() + Math.max(0, Number(data.state_ttl_s || 0) * 1000 - 1500) };
      }
      image.src = `data:${mime};base64,${data.image.data}`;
      image.hidden = false;
      pointerAvailable = data?.capabilities?.coordinate_input === true && Boolean(observation);
      pointerMode.disabled = !pointerAvailable;
      scrollUp.disabled = !pointerAvailable;
      scrollDown.disabled = !pointerAvailable;
      manual.disabled = !observation;
      state.textContent = pointerAvailable
        ? "Captured frame · refresh manually to observe changes"
        : "Captured frame · pointer input unavailable on this backend";
    } catch (error) {
      if (revision === generation && sessionId === currentSession) {
        state.textContent = error instanceof Error ? error.message : String(error);
      }
    } finally {
      if (revision === generation && sessionId === currentSession) {
        capture.disabled = false;
        manual.disabled = !observation && !manualEnabled;
      }
    }
  }

  function point(event) {
    if (!observation || !pointerAvailable || image.hidden) return null;
    const rect = image.getBoundingClientRect();
    if (image.clientWidth <= 0 || image.clientHeight <= 0) return null;
    const fx = (event.clientX - rect.left - image.clientLeft) / image.clientWidth;
    const fy = (event.clientY - rect.top - image.clientTop) / image.clientHeight;
    if (fx < 0 || fy < 0 || fx >= 1 || fy >= 1) return null;
    return { x: Math.floor(fx * observation.width), y: Math.floor(fy * observation.height) };
  }

  function readyForInput() {
    return manualEnabled && !busy && Boolean(sessionId) && Boolean(windows.value)
      && Boolean(observation) && performance.now() < observation.expires;
  }

  function keyAction(event) {
    if (event.isComposing || event.key === "Process" || event.key === "Dead"
      || event.getModifierState("AltGraph") || event.altKey) return null;
    const command = event.ctrlKey || event.metaKey;
    if (command) {
      const letter = event.key.toLowerCase();
      if (!/^[acvsxyz]$/.test(letter) || (event.shiftKey && letter !== "z")) return null;
      return { type: "key", keys: `${event.metaKey ? "Meta" : "Ctrl"}+${event.shiftKey ? "Shift+" : ""}${letter.toUpperCase()}` };
    }
    const special = {
      ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
      Enter: "Enter", Tab: "Tab", Backspace: "Backspace", Delete: "Delete",
      Escape: "Escape", Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown", " ": "Space",
    }[event.key];
    if (special && (!event.shiftKey || event.key !== "Tab")) {
      return { type: "key", keys: `${event.shiftKey ? "Shift+" : ""}${special}` };
    }
    return null;
  }

  async function act(action) {
    if (!manualEnabled || busy || !sessionId || !windows.value) return;
    if (!observation || performance.now() >= observation.expires) {
      invalidateObservation();
      state.textContent = "Observation expired; capture a fresh frame";
      return;
    }
    const currentSession = sessionId;
    const currentWindow = windows.value;
    const { id } = observation;
    const revision = ++generation;
    pointerCapableWhileBusy = pointerAvailable;
    busy = true;
    observation = null; // One-use native state, even when action fails or times out.
    capture.disabled = true;
    state.textContent = "Sending manual input…";
    try {
      await request("/gui/action", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: currentSession, window_id: currentWindow, state_id: id, action }) });
      if (revision !== generation || currentSession !== sessionId || currentWindow !== windows.value) return;
      await captureWindow();
    } catch (_error) {
      if (revision === generation && currentSession === sessionId) {
        invalidateObservation();
        state.textContent = "Input failed or observation expired; capture a fresh frame (not retried)";
      }
    } finally {
      busy = false;
      pointerCapableWhileBusy = false;
      if (revision === generation) capture.disabled = !sessionId || !windows.value;
    }
  }

  function imagePointerUp(event) {
    if (!manualEnabled || !dragStart || busy) return;
    const start = dragStart;
    dragStart = null;
    const end = point(event);
    if (!end) return;
    const mode = pointerMode.value;
    if (mode === "drag") {
      if (start.x !== end.x || start.y !== end.y) void act({ type: "drag", ...start, to_x: end.x, to_y: end.y });
    } else if (["click", "double_click", "right_click"].includes(mode)) {
      void act({ type: mode, ...end });
    }
  }

  function bind() {
    openButton.addEventListener("click", open);
    backButton.addEventListener("click", close);
    window.addEventListener("popstate", close);
    manual.addEventListener("click", () => setManual(!manualEnabled));
    image.addEventListener("pointerdown", (event) => {
      if (!readyForInput() || event.button !== 0) return;
      image.focus({ preventScroll: true });
      dragStart = point(event);
      if (dragStart) {
        image.setPointerCapture(event.pointerId);
        event.preventDefault();
      }
    });
    image.addEventListener("wheel", (event) => {
      if (!manualEnabled || event.ctrlKey || event.metaKey || event.altKey) return;
      // A trackpad gesture emits multiple events: never scroll the WebUI while
      // its first accepted native action is still refreshing the frame.
      if (busy) {
        if (pointerCapableWhileBusy) event.preventDefault();
        return;
      }
      if (!readyForInput() || !pointerAvailable) return;
      const target = point(event);
      if (!target || !event.deltaY) return;
      const unit = event.deltaMode === 1 ? 3 : event.deltaMode === 2 ? 1 : 60;
      const amount = Math.sign(event.deltaY) * Math.max(1, Math.min(12, Math.round(Math.abs(event.deltaY) / unit)));
      event.preventDefault();
      void act({ type: "scroll", ...target, amount });
    }, { passive: false });
    image.addEventListener("keydown", (event) => {
      if (!manualEnabled || document.activeElement !== image || event.defaultPrevented) return;
      const action = keyAction(event);
      if (!action) return;
      // Suppress the browser shortcut/scroll even when a native action is busy
      // or this observation has expired. Never queue or repeat native input.
      event.preventDefault();
      if (event.repeat || !readyForInput()) return;
      void act(action);
    });
    image.addEventListener("pointerup", imagePointerUp);
    image.addEventListener("pointercancel", () => { dragStart = null; });
    focus.addEventListener("click", () => void act({ type: "focus" }));
    scrollUp.addEventListener("click", () => void act({ type: "scroll", x: Math.floor((observation?.width || 0) / 2), y: Math.floor((observation?.height || 0) / 2), amount: -3 }));
    scrollDown.addEventListener("click", () => void act({ type: "scroll", x: Math.floor((observation?.width || 0) / 2), y: Math.floor((observation?.height || 0) / 2), amount: 3 }));
    keyForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const keys = key.value.trim();
      if (!keys) return;
      const accepted = readyForInput();
      void act({ type: "key", keys });
      if (accepted) key.value = "";
    });
    textForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const text = typed.value;
      if (!text) return;
      const accepted = readyForInput();
      void act({ type: "type", text });
      if (accepted) typed.value = "";
    });
    load.addEventListener("click", () => void loadWindows());
    capture.addEventListener("click", () => void captureWindow());
    windows.addEventListener("change", () => {
      generation++;
      clearImage();
      capture.disabled = !sessionId || !windows.value;
      setManual(false);
      manual.disabled = true;
      state.textContent = windows.value ? "Capture a fresh frame" : "Select a window";
    });
  }

  return { bind, selectSession, close };
}
