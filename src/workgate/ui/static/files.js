export function createFilesController({
  elements,
  request,
  text,
  formatFileBytes,
}) {
  const controllerState = {
    fileExecutorId: "",
    filePath: ".",
    fileParentPath: ".",
    fileEntries: [],
    selectedFilePath: "",
    fileListGeneration: 0,
    filePreviewGeneration: 0,
    fileEditorPath: "",
    fileMutationBusy: false,
    fileMutations: {
      write: true,
      delete: true,
      copy: true,
      move: true,
      rename: true,
    },
  };

  function defaultFileMutations() {
    return {
      write: true,
      delete: true,
      copy: true,
      move: true,
      rename: true,
    };
  }

  function resetFileWorkspace(executorId = "") {
    controllerState.fileExecutorId = executorId;
    controllerState.filePath = ".";
    controllerState.fileParentPath = ".";
    controllerState.fileEntries = [];
    controllerState.selectedFilePath = "";
    controllerState.fileListGeneration += 1;
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    controllerState.fileMutations = defaultFileMutations(controllerState.fileExecutorId);
    elements.fileExecutor.value = controllerState.fileExecutorId;
    elements.filePath.value = ".";
    renderFileList();
    showFilePreviewMessage("No file selected", `Select a file or directory on ${controllerState.fileExecutorId}.`);
  }

  function renderFileExecutors(targets) {
    const available = Array.isArray(targets) ? targets : [];
    elements.fileExecutor.replaceChildren();
    let currentAvailable = false;
    for (const executor of available) {
      const executorId = text(executor.executor_id, "");
      if (!executorId) continue;
      const online = executor.status === "online";
      const option = document.createElement("option");
      option.value = executorId;
      const label = text(executor.name, executorId);
      option.textContent = online ? label : `${label} (${text(executor.status, "offline")})`;
      option.disabled = !online;
      option.selected = online && executorId === controllerState.fileExecutorId;
      if (option.selected) currentAvailable = true;
      elements.fileExecutor.append(option);
    }
    if (!currentAvailable) {
      const firstOnline = available.find((item) => item.status === "online");
      const nextExecutorId = firstOnline?.executor_id || "";
      const changed = controllerState.fileExecutorId !== nextExecutorId;
      resetFileWorkspace(nextExecutorId);
      if (changed && nextExecutorId) void refreshFiles();
      return;
    }
    elements.fileExecutor.value = controllerState.fileExecutorId;
  }


  function fileQuery(path, value) {
    const query = new URLSearchParams({
      executor_id: controllerState.fileExecutorId,
      path: value,
    });
    return `${path}?${query.toString()}`;
  }

  function fileAction(action, body) {
    return request(`/files/${encodeURIComponent(action)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, executor_id: controllerState.fileExecutorId }),
    });
  }

  function joinFilePath(parent, name) {
    const child = String(name || "").replace(/^[\\/]+/, "");
    if (!parent || parent === ".") return child;
    return `${String(parent).replace(/[\\/]+$/, "")}/${child}`;
  }

  function splitFilePath(path) {
    const normalized = String(path || ".")
      .replace(/\\/g, "/")
      .replace(/\/+$/, "");
    const separator = normalized.lastIndexOf("/");
    if (separator < 0) return { parent: ".", name: normalized };
    return {
      parent: normalized.slice(0, separator) || ".",
      name: normalized.slice(separator + 1),
    };
  }

  function setFileMutationBusy(busy) {
    controllerState.fileMutationBusy = busy;
    elements.fileExecutor.disabled = busy;
    elements.filePath.disabled = busy;
    elements.fileRefresh.disabled = busy;
    elements.fileShowHidden.disabled = busy;
    const goButton = elements.filePathForm.querySelector('button[type="submit"]');
    if (goButton) goButton.disabled = busy;
    setFileControls();
  }

  function currentFileEntry() {
    return controllerState.fileEntries.find((entry) => entry.path === controllerState.selectedFilePath) || null;
  }

  function clearFileEditor() {
    controllerState.fileEditorPath = "";
    elements.fileEditor.value = "";
    elements.fileEditorForm.hidden = true;
    elements.filePreviewBody.hidden = false;
  }

  function setFileControls() {
    const entry = currentFileEntry();
    elements.fileNew.disabled = controllerState.fileMutationBusy || !controllerState.fileMutations.write;
    elements.fileOpen.disabled = controllerState.fileMutationBusy || !entry || entry.type !== "dir";
    elements.fileEdit.disabled =
      controllerState.fileMutationBusy || !controllerState.fileMutations.write || !entry || entry.type !== "file";
    elements.fileCopy.disabled = controllerState.fileMutationBusy || !controllerState.fileMutations.copy || !entry;
    elements.fileMove.disabled = controllerState.fileMutationBusy || !controllerState.fileMutations.move || !entry;
    elements.fileRename.disabled = controllerState.fileMutationBusy || !controllerState.fileMutations.rename || !entry;
    elements.fileDelete.disabled = controllerState.fileMutationBusy || !controllerState.fileMutations.delete || !entry;
    elements.fileUp.disabled = controllerState.fileMutationBusy || controllerState.filePath === controllerState.fileParentPath;
    elements.fileCopy.title = "";
    elements.fileMove.title = "";
    elements.fileRename.title = "";
  }

  function showFilePreviewMessage(title, detail) {
    elements.filePreviewTitle.textContent = title;
    elements.filePreviewMeta.textContent = detail || "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = detail || title;
    elements.filePreviewBody.replaceChildren(empty);
  }

  function fileEntryLabel(entry) {
    const icon = entry.type === "dir" ? "▰" : entry.type === "link" ? "↗" : "·";
    return `${icon} ${text(entry.name, entry.path)}`;
  }

  function visibleFileEntries() {
    return controllerState.fileEntries.filter((entry) => elements.fileShowHidden.checked || !entry.hidden);
  }

  function renderFileList() {
    const visible = visibleFileEntries();
    if (controllerState.selectedFilePath && !visible.some((entry) => entry.path === controllerState.selectedFilePath)) {
      controllerState.selectedFilePath = "";
      controllerState.filePreviewGeneration += 1;
      clearFileEditor();
      showFilePreviewMessage("No file selected", "Select a file or directory.");
    }

    elements.fileList.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = controllerState.fileEntries.length ? "Hidden entries are not shown." : "This directory is empty.";
      elements.fileList.append(empty);
      setFileControls();
      return;
    }

    for (const entry of visible) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-entry";
      button.setAttribute("aria-current", entry.path === controllerState.selectedFilePath ? "true" : "false");
      button.title = entry.path;

      const label = document.createElement("span");
      label.className = "file-entry-name";
      label.textContent = fileEntryLabel(entry);
      const detail = document.createElement("span");
      detail.className = "file-entry-detail";
      detail.textContent = entry.type === "dir" ? "dir" : entry.type === "link" ? "link" : formatFileBytes(entry.size);
      button.append(label, detail);
      button.addEventListener("click", () => selectFile(entry));
      button.addEventListener("dblclick", () => {
        if (!controllerState.fileMutationBusy && entry.type === "dir") void navigateFiles(entry.path);
      });
      elements.fileList.append(button);
    }
    setFileControls();
  }

  function renderDirectoryPreview(payload) {
    const list = document.createElement("div");
    list.className = "file-preview-directory";
    const entries = (Array.isArray(payload.entries) ? payload.entries : []).filter(
      (entry) => elements.fileShowHidden.checked || !entry.hidden,
    );
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "Directory is empty.";
      list.append(empty);
      return list;
    }
    for (const entry of entries.slice(0, 100)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "file-preview-entry";
      button.textContent = fileEntryLabel(entry);
      button.title = entry.path;
      button.addEventListener("click", () => {
        if (!controllerState.fileMutationBusy) void navigateFiles(payload.path, entry.path);
      });
      list.append(button);
    }
    return list;
  }

  function renderFilePreview(payload, entry) {
    clearFileEditor();
    elements.filePreviewTitle.textContent = text(entry.name, entry.path);
    const metadata = [payload.kind, payload.media_type, payload.bytes === undefined ? "" : formatFileBytes(payload.bytes)]
      .filter(Boolean)
      .join(" · ");
    elements.filePreviewMeta.textContent = metadata;

    if (payload.kind === "directory") {
      elements.filePreviewMeta.textContent = `${payload.count || 0} entries${payload.is_truncated ? " · truncated" : ""}`;
      elements.filePreviewBody.replaceChildren(renderDirectoryPreview(payload));
      return;
    }
    if (payload.kind === "image") {
      if (!payload.inline || !payload.data_base64) {
        showFilePreviewMessage(text(entry.name, entry.path), text(payload.message, "Image preview unavailable."));
        return;
      }
      const image = document.createElement("img");
      image.className = "file-preview-image";
      image.alt = text(entry.name, "Workspace image");
      image.src = `data:${payload.media_type};base64,${payload.data_base64}`;
      elements.filePreviewBody.replaceChildren(image);
      return;
    }

    const pre = document.createElement("pre");
    pre.className = "file-preview-text";
    if (payload.kind === "binary") {
      const hex = String(payload.preview || "");
      pre.textContent = (hex.match(/.{1,32}/g) || []).join("\n") || "No preview bytes.";
      elements.filePreviewMeta.textContent = `${formatFileBytes(payload.bytes)} · ${payload.preview_bytes || 0} preview bytes · hex`;
    } else {
      const source = text(payload.content, "");
      const language = window.WorkgateSyntax
        ? window.WorkgateSyntax.languageForPath(entry.path, payload.media_type)
        : "plain";
      if (window.WorkgateSyntax && language !== "plain") window.WorkgateSyntax.render(pre, source, language);
      else pre.textContent = source;
      if (payload.preview_truncated) elements.filePreviewMeta.textContent += " · preview truncated";
    }
    elements.filePreviewBody.replaceChildren(pre);
  }

  async function previewFile(entry) {
    const generation = ++controllerState.filePreviewGeneration;
    clearFileEditor();
    elements.filePreviewTitle.textContent = text(entry.name, entry.path);
    elements.filePreviewMeta.textContent = "Loading preview…";
    showFilePreviewMessage(text(entry.name, entry.path), "Loading preview…");
    try {
      const payload = await request(fileQuery("/files/preview", entry.path));
      if (generation !== controllerState.filePreviewGeneration || controllerState.selectedFilePath !== entry.path) return;
      renderFilePreview(payload, entry);
    } catch (error) {
      if (generation !== controllerState.filePreviewGeneration || controllerState.selectedFilePath !== entry.path) return;
      elements.fileState.textContent = "Preview unavailable";
      showFilePreviewMessage(text(entry.name, entry.path), error instanceof Error ? error.message : String(error));
    }
  }

  function selectFile(entry) {
    if (controllerState.fileMutationBusy) return;
    controllerState.selectedFilePath = entry.path;
    renderFileList();
    void previewFile(entry);
  }

  async function refreshFiles({ previewSelection = false } = {}) {
    if (!controllerState.fileExecutorId) {
      elements.fileState.textContent = "No online executor available";
      renderFileList({ entries: [] });
      return null;
    }
    const generation = ++controllerState.fileListGeneration;
    const requestedExecutor = controllerState.fileExecutorId;
    const requestedPath = controllerState.filePath;
    elements.fileRefresh.disabled = true;
    elements.fileState.textContent = `Loading ${requestedExecutor}:${requestedPath}`;
    try {
      const payload = await request(fileQuery("/files", requestedPath));
      if (generation !== controllerState.fileListGeneration || requestedExecutor !== controllerState.fileExecutorId) return null;
      controllerState.fileExecutorId = text(payload.executor_id, requestedExecutor);
      controllerState.filePath = text(payload.path, ".");
      controllerState.fileParentPath = text(payload.parent, controllerState.filePath);
      controllerState.fileEntries = Array.isArray(payload.entries) ? payload.entries : [];
      controllerState.fileMutations = {
        ...defaultFileMutations(controllerState.fileExecutorId),
        ...(payload.mutations && typeof payload.mutations === "object" ? payload.mutations : {}),
      };
      elements.fileExecutor.value = controllerState.fileExecutorId;
      elements.filePath.value = controllerState.filePath;
      const selected = currentFileEntry();
      if (!selected) {
        controllerState.selectedFilePath = "";
        controllerState.filePreviewGeneration += 1;
        clearFileEditor();
        showFilePreviewMessage("No file selected", `Select a file or directory on ${controllerState.fileExecutorId}.`);
      }
      renderFileList();
      if (selected && previewSelection) void previewFile(selected);
      elements.fileState.textContent = `${controllerState.fileExecutorId}:${controllerState.filePath} · ${controllerState.fileEntries.length} entries${payload.is_truncated ? " · truncated" : ""}`;
      return payload;
    } finally {
      if (generation === controllerState.fileListGeneration) {
        elements.fileRefresh.disabled = controllerState.fileMutationBusy;
      }
    }
  }

  async function navigateFiles(path, selection = "") {
    controllerState.filePath = path || ".";
    controllerState.selectedFilePath = selection;
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    showFilePreviewMessage("Loading directory", `${controllerState.fileExecutorId}:${controllerState.filePath}`);
    try {
      await refreshFiles({ previewSelection: Boolean(selection) });
      return true;
    } catch (error) {
      elements.fileState.textContent = "Directory unavailable";
      showFilePreviewMessage("Unable to open directory", error instanceof Error ? error.message : String(error));
      return false;
    }
  }

  function openSelectedFile() {
    if (controllerState.fileMutationBusy) return;
    const entry = currentFileEntry();
    if (entry?.type === "dir") void navigateFiles(entry.path);
  }

  async function openFileEditor() {
    const entry = currentFileEntry();
    if (!entry || entry.type !== "file") return;
    const generation = ++controllerState.filePreviewGeneration;
    elements.fileEdit.disabled = true;
    elements.fileState.textContent = `Opening ${entry.path}`;
    try {
      const payload = await request(fileQuery("/files/content", entry.path));
      if (generation !== controllerState.filePreviewGeneration || controllerState.selectedFilePath !== entry.path) return;
      controllerState.fileEditorPath = entry.path;
      elements.fileEditor.value = text(payload.content, "");
      elements.filePreviewBody.hidden = true;
      elements.fileEditorForm.hidden = false;
      elements.filePreviewTitle.textContent = `Edit · ${text(entry.name, entry.path)}`;
      elements.filePreviewMeta.textContent = `${formatFileBytes(payload.bytes)} · complete text`;
      elements.fileState.textContent = "Editing";
      elements.fileEditor.focus();
    } catch (error) {
      if (generation !== controllerState.filePreviewGeneration || controllerState.selectedFilePath !== entry.path) return;
      elements.fileState.textContent = "Editor unavailable";
      showFilePreviewMessage(text(entry.name, entry.path), error instanceof Error ? error.message : String(error));
    } finally {
      setFileControls();
    }
  }

  async function createFile() {
    const name = globalThis.prompt("New file name or relative path:");
    if (name === null || !name.trim()) return;
    const path = joinFilePath(controllerState.filePath, name.trim());
    setFileMutationBusy(true);
    try {
      await fileAction("write", { path, content: "", overwrite: false });
      controllerState.selectedFilePath = path;
      await refreshFiles({ previewSelection: true });
      await openFileEditor();
    } catch (error) {
      elements.fileState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      setFileMutationBusy(false);
    }
  }

  async function finishFileMutation(destination, message) {
    const target = splitFilePath(destination);
    const opened = await navigateFiles(target.parent, destination);
    if (opened) elements.fileState.textContent = message;
  }

  async function copySelectedFile() {
    const entry = currentFileEntry();
    if (!entry) return;
    const current = splitFilePath(entry.path);
    const suggested = joinFilePath(current.parent, `${current.name}.copy`);
    const destination = globalThis.prompt("Copy to workspace path:", suggested);
    if (destination === null || !destination.trim()) return;
    const target = destination.trim();
    setFileMutationBusy(true);
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    try {
      const result = await fileAction("copy", {
        path: entry.path,
        destination: target,
      });
      const copied = text(result.destination, target);
      await finishFileMutation(copied, `Copied ${entry.path} to ${copied}`);
    } catch (error) {
      elements.fileState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      setFileMutationBusy(false);
    }
  }

  async function moveSelectedFile() {
    const entry = currentFileEntry();
    if (!entry) return;
    const destination = globalThis.prompt("Move to workspace path:", entry.path);
    if (destination === null || !destination.trim()) return;
    const target = destination.trim();
    setFileMutationBusy(true);
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    try {
      const result = await fileAction("move", {
        path: entry.path,
        destination: target,
      });
      const moved = text(result.destination, target);
      await finishFileMutation(moved, `Moved ${entry.path} to ${moved}`);
    } catch (error) {
      elements.fileState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      setFileMutationBusy(false);
    }
  }

  async function renameSelectedFile() {
    const entry = currentFileEntry();
    if (!entry) return;
    const current = splitFilePath(entry.path);
    const name = globalThis.prompt("New name:", current.name);
    if (name === null || !name.trim()) return;
    const nextName = name.trim();
    const destination = joinFilePath(current.parent, nextName);
    setFileMutationBusy(true);
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    try {
      const result = await fileAction("rename", {
        path: entry.path,
        name: nextName,
      });
      const renamed = text(result.destination, destination);
      await finishFileMutation(renamed, `Renamed ${entry.path} to ${renamed}`);
    } catch (error) {
      elements.fileState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      setFileMutationBusy(false);
    }
  }

  async function deleteSelectedFile() {
    const entry = currentFileEntry();
    if (!entry) return;
    const detail = entry.type === "dir" ? " and all of its contents" : "";
    if (!globalThis.confirm(`Delete ${entry.path}${detail}?`)) return;
    setFileMutationBusy(true);
    try {
      await fileAction("delete", { path: entry.path, recursive: entry.type === "dir" });
      controllerState.selectedFilePath = "";
      controllerState.filePreviewGeneration += 1;
      clearFileEditor();
      await refreshFiles();
      elements.fileState.textContent = `Deleted ${entry.path}`;
    } catch (error) {
      elements.fileState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      setFileMutationBusy(false);
    }
  }

  function invalidate() {
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
  }

  function bind() {
  elements.fileExecutor.addEventListener("change", () => {
    if (controllerState.fileMutationBusy) return;
    resetFileWorkspace(elements.fileExecutor.value);
    void refreshFiles();
  });
  elements.filePathForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!controllerState.fileMutationBusy) void navigateFiles(elements.filePath.value.trim() || ".");
  });
  elements.fileUp.addEventListener("click", () => void navigateFiles(controllerState.fileParentPath));
  elements.fileRefresh.addEventListener("click", () => void refreshFiles());
  elements.fileShowHidden.addEventListener("change", () => {
    const entry = currentFileEntry();
    renderFileList();
    if (entry?.type === "dir" && controllerState.selectedFilePath === entry.path) {
      void previewFile(entry);
    }
  });
  elements.fileNew.addEventListener("click", () => void createFile());
  elements.fileOpen.addEventListener("click", openSelectedFile);
  elements.fileEdit.addEventListener("click", () => void openFileEditor());
  elements.fileCopy.addEventListener("click", () => void copySelectedFile());
  elements.fileMove.addEventListener("click", () => void moveSelectedFile());
  elements.fileRename.addEventListener("click", () => void renameSelectedFile());
  elements.fileDelete.addEventListener("click", () => void deleteSelectedFile());
  elements.fileEditorCancel.addEventListener("click", () => {
    const entry = currentFileEntry();
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    elements.fileState.textContent = `${controllerState.fileExecutorId}:${controllerState.filePath}`;
    if (entry) void previewFile(entry);
    else showFilePreviewMessage("No file selected", `Select a file or directory on ${controllerState.fileExecutorId}.`);
  });
  elements.fileEditorForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!controllerState.fileEditorPath || !controllerState.fileMutations.write) return;
    const path = controllerState.fileEditorPath;
    const button = elements.fileEditorForm.querySelector('button[type="submit"]');
    button.disabled = true;
    setFileMutationBusy(true);
    elements.fileState.textContent = `Saving ${controllerState.fileExecutorId}:${path}`;
    try {
      await fileAction("write", {
        path,
        content: elements.fileEditor.value,
        overwrite: true,
      });
      controllerState.selectedFilePath = path;
      clearFileEditor();
      await refreshFiles();
      const entry = currentFileEntry();
      if (entry) await previewFile(entry);
      elements.fileState.textContent = `Saved ${controllerState.fileExecutorId}:${path}`;
    } catch (error) {
      elements.fileState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      setFileMutationBusy(false);
      button.disabled = false;
    }
  });

  }

  return {
    bind,
    invalidate,
    refresh: refreshFiles,
    renderExecutors: renderFileExecutors,
    reset: resetFileWorkspace,
    showMessage: showFilePreviewMessage,
  };
}
