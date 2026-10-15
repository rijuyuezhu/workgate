// On-demand native GUI observation; no streaming, persistent frames or input takeover.
export function createGuiViewer({ request }) {
  const panel = document.getElementById("session-gui");
  const layout = panel.closest(".task-sessions-layout");
  const openButton = document.getElementById("session-gui-open");
  const backButton = document.getElementById("session-gui-back");
  const state = document.getElementById("session-gui-state");
  const windows = document.getElementById("session-gui-windows");
  const load = document.getElementById("session-gui-load");
  const capture = document.getElementById("session-gui-capture");
  const image = document.getElementById("session-gui-image");
  let sessionId = "";
  let selectedKey = "";
  let generation = 0;

  function close() {
    layout.classList.remove("gui-open");
    panel.hidden = true;
    generation++;
    clearImage();
  }

  function open() {
    if (!sessionId) return;
    layout.classList.add("gui-open");
    panel.hidden = false;
    void loadWindows();
  }

  function clearImage() {
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
    clearImage();
    state.textContent = sessionId
      ? "Opening native window list…"
      : "GUI is only available for an active, connected session";
  }

  async function loadWindows() {
    if (!sessionId) return;
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
      const count = windows.options.length - 1;
      state.textContent = count
        ? `${count} window${count === 1 ? "" : "s"} · list refreshed at ${new Date().toLocaleTimeString()}`
        : "No native GUI windows available";
    } catch (error) {
      if (revision === generation && sessionId === currentSession) {
        windows.disabled = windows.options.length < 2;
        capture.disabled = !windows.value;
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
    clearImage();
    state.textContent = "Capturing window…";
    try {
      const data = await request(`/gui/state?${new URLSearchParams({ session_id: currentSession, window_id: id })}`);
      if (revision !== generation || sessionId !== currentSession || windows.value !== id) return;
      const mime = String(data?.image?.mime_type || "");
      if (!["image/png", "image/jpeg", "image/webp"].includes(mime) || !data.image.data) {
        throw new Error("Native GUI screenshot is unavailable");
      }
      image.src = `data:${mime};base64,${data.image.data}`;
      image.hidden = false;
      state.textContent = "Captured frame · refresh manually to observe changes";
    } catch (error) {
      if (revision === generation && sessionId === currentSession) {
        state.textContent = error instanceof Error ? error.message : String(error);
      }
    } finally {
      if (revision === generation && sessionId === currentSession) capture.disabled = false;
    }
  }

  function bind() {
    openButton.addEventListener("click", open);
    backButton.addEventListener("click", close);
    window.addEventListener("popstate", close);
    load.addEventListener("click", () => void loadWindows());
    capture.addEventListener("click", () => void captureWindow());
    windows.addEventListener("change", () => {
      generation++;
      clearImage();
      capture.disabled = !sessionId || !windows.value;
      state.textContent = windows.value ? "Capture a fresh frame" : "Select a window";
    });
  }

  return { bind, selectSession };
}
