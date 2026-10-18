// Executor-managed Skill auxiliary text files, scoped to a single saved Skill.
export function createSkillFileEditor({ request, onClose = () => {} }) {
  const get = (name) => document.getElementById("executor-skill-files-" + name);
  const dialog = get("dialog");
  const title = get("title");
  const status = get("status");
  const list = get("list");
  const path = get("path");
  const content = get("content");
  const form = get("form");
  const save = get("save");
  const remove = get("delete");
  const reload = get("reload");
  const refresh = get("refresh");
  const create = get("new");
  const closeButton = get("close");
  let context = null;
  let generation = 0;
  let listGeneration = 0;
  let loading = false;
  let current = { path: "", revision: "", baseline: "", existing: false };
  let busy = false;

  function notice(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
  }

  function dirty() {
    return content.value !== current.baseline ||
      (!current.existing && Boolean(path.value));
  }

  function acceptDiscard() {
    return !dirty() || globalThis.confirm("Discard unsaved file changes?");
  }

  function updateButtons() {
    path.readOnly = current.existing || busy || loading;
    content.readOnly = busy || loading;
    save.disabled = busy || loading || !context;
    remove.hidden = !current.existing;
    reload.hidden = !current.existing;
    remove.disabled = busy || loading;
    reload.disabled = busy || loading;
    list.disabled = busy || loading;
    refresh.disabled = busy;
    create.disabled = busy || loading;
    closeButton.disabled = busy;
    save.textContent = current.existing ? "Save changes" : "Create file";
  }

  function fresh() {
    current = { path: "", revision: "", baseline: "", existing: false };
    path.value = "";
    content.value = "";
    list.value = "";
    updateButtons();
    notice("");
    path.focus();
  }

  function query(action, filename = "") {
    const params = new URLSearchParams({
      executor_id: context.executorId,
      name: context.name,
      action,
    });
    if (filename) params.set("path", filename);
    return "/integrations/skill-files?" + params.toString();
  }

  async function loadList() {
    if (!context) return;
    const ctx = context;
    const seq = ++listGeneration;
    try {
      const result = await request(query("list"));
      if (ctx !== context || seq !== listGeneration) return;
      const selected = current.existing ? current.path : "";
      list.replaceChildren(new Option("Select file…", ""));
      for (const filename of result.files || []) {
        if (typeof filename === "string") list.add(new Option(filename, filename));
      }
      list.value = selected;
      notice(result.files?.length ? "" : "No auxiliary files yet.");
    } catch (error) {
      if (ctx === context && seq === listGeneration) {
        notice(error instanceof Error ? error.message : "Cannot list Skill files", true);
      }
    }
  }

  async function openFile(filename) {
    if (!context || busy || !filename || !acceptDiscard()) {
      list.value = current.existing ? current.path : "";
      return;
    }
    const ctx = context;
    const seq = ++generation;
    loading = true;
    notice("Loading file…");
    updateButtons();
    try {
      const row = await request(query("get", filename));
      if (ctx !== context || seq !== generation) return;
      current = { path: row.path, revision: row.revision, baseline: row.content, existing: true };
      path.value = row.path;
      content.value = row.content;
      list.value = row.path;
      notice("");
    } catch (error) {
      if (ctx !== context || seq !== generation) return;
      list.value = current.existing ? current.path : "";
      notice(error instanceof Error ? error.message : "Cannot read file", true);
    } finally {
      if (ctx === context && seq === generation) {
        loading = false;
        updateButtons();
      }
    }
  }

  async function mutate(action) {
    if (!context || busy || loading) return;
    const ctx = context;
    const filename = current.existing ? current.path : path.value.trim();
    if (!filename) {
      notice("Enter a relative path.", true);
      return;
    }
    busy = true;
    updateButtons();
    notice("Applying file change…");
    try {
      const data = {
        executor_id: ctx.executorId,
        name: ctx.name,
        action,
        path: filename,
      };
      if (action !== "delete") data.content = content.value;
      if (current.existing) data.expected_revision = current.revision;
      const result = await request("/integrations/skill-files", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (ctx !== context) return;
      current.baseline = content.value;
      if (action === "delete") {
        current = { path: "", revision: "", baseline: "", existing: false };
        path.value = "";
        content.value = "";
      } else {
        current = { path: filename, revision: result.revision, baseline: content.value, existing: true };
        path.value = filename;
      }
      await loadList();
      if (ctx === context) notice(action === "delete" ? "File deleted." : "File saved.");
    } catch (error) {
      if (ctx === context) notice(
        (error instanceof Error ? error.message : "File operation failed") +
        " · Reload before retrying if the outcome is uncertain.", true,
      );
    } finally {
      busy = false;
      if (ctx === context) updateButtons();
    }
  }

  function close(force = false) {
    if (!dialog.open) return;
    if (!force && (busy || !acceptDiscard())) return;
    generation++;
    listGeneration++;
    loading = false;
    context = null;
    dialog.close();
    if (!force) onClose();
  }

  function open({ executorId, name }) {
    if (dialog.open) return;
    context = { executorId, name };
    generation++;
    listGeneration++;
    loading = false;
    busy = false;
    title.textContent = name + " · Executor";
    current = { path: "", revision: "", baseline: "", existing: false };
    list.replaceChildren(new Option("Select file…", ""));
    path.value = "";
    content.value = "";
    notice("");
    updateButtons();
    dialog.showModal();
    void loadList();
  }

  function bind() {
    closeButton.addEventListener("click", () => close());
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      close();
    });
    create.addEventListener("click", () => {
      if (acceptDiscard()) fresh();
    });
    refresh.addEventListener("click", () => void loadList());
    list.addEventListener("change", () => {
      if (list.value) void openFile(list.value);
      else if (acceptDiscard()) fresh();
      else list.value = current.existing ? current.path : "";
    });
    reload.addEventListener("click", () => void openFile(current.path));
    remove.addEventListener("click", () => {
      if (current.existing && globalThis.confirm(`Delete “${current.path}”?`)) void mutate("delete");
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!busy) void mutate(current.existing ? "update" : "create");
    });
  }

  return { open, close, bind };
}
