export function createFilesController({
  elements,
  request,
  text,
  formatFileBytes,
  initialExecutorId = "",
  initialPath = "",
}) {
  const controllerState = {
    fileExecutorId: text(initialExecutorId, ""),
    fileExecutorPinned: Boolean(initialExecutorId),
    filePath: text(initialPath, "."),
    fileParentPath: ".",
    fileEntries: [],
    selectedFilePath: "",
    fileListGeneration: 0,
    filePreviewGeneration: 0,
    fileEditorPath: "",
    fileEditorSha256: "",
    fileMutationBusy: false,
    fileClipboard: null,
    fileDialogAction: "",
    fileSortDescending: false,
    fileExecutorStates: new Map(),
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

  function fileExecutorOnline() {
    return controllerState.fileExecutorStates.get(controllerState.fileExecutorId) === "online";
  }

  function resetFileWorkspace(executorId = "") {
    controllerState.fileExecutorId = executorId;
    controllerState.filePath = ".";
    controllerState.fileParentPath = ".";
    controllerState.fileEntries = [];
    controllerState.selectedFilePath = "";
    controllerState.fileClipboard = null;
    renderClipboard();
    controllerState.fileListGeneration += 1;
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    controllerState.fileMutations = defaultFileMutations(controllerState.fileExecutorId);
    elements.fileExecutor.value = controllerState.fileExecutorId;
    elements.filePath.value = ".";
    renderBreadcrumbs();
    renderFileList();
    showFilePreviewMessage("No file selected", `Select a file or directory on ${controllerState.fileExecutorId}.`);
  }

  function renderFileExecutors(targets) {
    const available = Array.isArray(targets) ? targets : [];
    const wasOnline = fileExecutorOnline();
    controllerState.fileExecutorStates = new Map();
    elements.fileExecutor.replaceChildren();
    let currentAvailable = false;
    for (const executor of available) {
      const executorId = text(executor.executor_id, "");
      if (!executorId) continue;
      const online = executor.status === "online";
      controllerState.fileExecutorStates.set(executorId, executor.status);
      const option = document.createElement("option");
      option.value = executorId;
      const label = text(executor.name, executorId);
      option.textContent = online ? label : `${label} (${text(executor.status, "offline")})`;
      option.disabled = !online;
      option.selected = executorId === controllerState.fileExecutorId;
      if (option.selected && online) currentAvailable = true;
      elements.fileExecutor.append(option);
    }
    if (!currentAvailable) {
      if (controllerState.fileExecutorPinned && controllerState.fileExecutorId) {
        if (!controllerState.fileExecutorStates.has(controllerState.fileExecutorId)) {
          const option = document.createElement("option");
          option.value = controllerState.fileExecutorId;
          option.textContent = `${controllerState.fileExecutorId} (unavailable)`;
          option.disabled = true;
          elements.fileExecutor.append(option);
        }
        if (wasOnline) {
          controllerState.fileListGeneration += 1;
          controllerState.filePreviewGeneration += 1;
          controllerState.fileEntries = [];
          controllerState.selectedFilePath = "";
          clearFileEditor();
          renderFileList();
        }
        elements.fileExecutor.value = controllerState.fileExecutorId;
        elements.fileState.textContent = `Executor unavailable · ${controllerState.fileExecutorId}`;
        setFileControls();
        elements.fileRefresh.disabled = true;
        return;
      }
      const firstOnline = available.find((item) => item.status === "online");
      const nextExecutorId = firstOnline?.executor_id || "";
      const changed = controllerState.fileExecutorId !== nextExecutorId;
      resetFileWorkspace(nextExecutorId);
      if (changed && nextExecutorId) void refreshFiles();
      return;
    }
    elements.fileExecutor.value = controllerState.fileExecutorId;
    if (!wasOnline) void refreshFiles();
    setFileControls();
  }


  function fileQuery(path, value) {
    const query = new URLSearchParams({
      executor_id: controllerState.fileExecutorId,
      path: value,
    });
    return `${path}?${query.toString()}`;
  }

  function fileAction(action, body) {
    if (!fileExecutorOnline()) throw new Error("Executor unavailable");
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
    elements.fileEditorCancel.disabled = busy;
    elements.fileEditorReload.disabled = busy;
    elements.fileShowHidden.disabled = busy;
    elements.fileUpload.disabled = busy;
    elements.fileNewFolder.disabled = busy;
    elements.fileOperationCancel.disabled = busy;
    elements.filePaste.disabled = busy || !fileExecutorOnline() || !controllerState.fileClipboard;
    const goButton = elements.filePathForm.querySelector('button[type="submit"]');
    if (goButton) goButton.disabled = busy;
    setFileControls();
    renderBreadcrumbs();
  }

  function currentFileEntry() {
    return controllerState.fileEntries.find((entry) => entry.path === controllerState.selectedFilePath) || null;
  }

  function clearFileEditor() {
    controllerState.fileEditorPath = "";
    controllerState.fileEditorSha256 = "";
    elements.fileEditor.value = "";
    elements.fileEditorForm.hidden = true;
    elements.filePreviewBody.hidden = false;
  }

  function setFileControls() {
    const entry = currentFileEntry();
    const unavailable = !fileExecutorOnline();
    elements.fileNew.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.write;
    elements.fileNewFolder.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.mkdir;
    elements.fileUpload.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.write;
    elements.filePaste.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileClipboard ||
      !controllerState.fileMutations[controllerState.fileClipboard.mode];
    elements.fileOpen.disabled = unavailable || controllerState.fileMutationBusy || !entry || entry.type !== "dir";
    elements.fileEdit.disabled =
      unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.write || !entry || entry.type !== "file";
    elements.fileCopy.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.copy || !entry;
    elements.fileMove.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.move || !entry;
    elements.fileRename.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.rename || !entry;
    elements.fileDelete.disabled = unavailable || controllerState.fileMutationBusy || !controllerState.fileMutations.delete || !entry;
    elements.fileUp.disabled = unavailable || controllerState.fileMutationBusy || controllerState.filePath === controllerState.fileParentPath;
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
    const needle = elements.fileFilter.value.trim().toLocaleLowerCase();
    const key = elements.fileSort.value;
    const direction = controllerState.fileSortDescending ? -1 : 1;
    return controllerState.fileEntries
      .filter((entry) => (elements.fileShowHidden.checked || !entry.hidden) &&
        (!needle || String(entry.name).toLocaleLowerCase().includes(needle)))
      .sort((a, b) => {
        if (a.type === "dir" && b.type !== "dir") return -1;
        if (b.type === "dir" && a.type !== "dir") return 1;
        const delta = key === "name" ? 0 : Number(a[key] || 0) - Number(b[key] || 0);
        return delta ? delta * direction : String(a.name).localeCompare(String(b.name), undefined, { numeric: true, sensitivity: "base" }) * direction;
      });
  }

  function renderBreadcrumbs() {
    elements.fileBreadcrumbs.replaceChildren();
    const path = controllerState.filePath.replace(/\\/g, "/");
    const absolute = path.startsWith("/");
    const parts = path.split("/").filter((part) => part && part !== ".");
    const segments = [{ label: absolute ? "/" : "Workspace", path: absolute ? "/" : "." }];
    let current = absolute ? "" : ".";
    for (const part of parts) {
      current = current === "" ? `/${part}` : current === "." ? part : `${current}/${part}`;
      segments.push({ label: part, path: current });
    }
    for (const segment of segments) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = segment.label;
      button.disabled = controllerState.fileMutationBusy || segment.path === controllerState.filePath;
      button.addEventListener("click", () => void navigateFiles(segment.path));
      elements.fileBreadcrumbs.append(button);
    }
  }

  function renderClipboard() {
    const clipboard = controllerState.fileClipboard;
    elements.fileClipboardState.textContent = clipboard
      ? `${clipboard.mode === "copy" ? "Copy" : "Cut"}: ${splitFilePath(clipboard.path).name}`
      : "Clipboard empty";
  }

  function renderFileList() {
    const visible = visibleFileEntries();
    if (controllerState.selectedFilePath && !controllerState.fileEntries.some((entry) => entry.path === controllerState.selectedFilePath)) {
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
      image.alt = text(entry.name, "File image");
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
    if (!fileExecutorOnline()) {
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
      renderBreadcrumbs();
      const selected = currentFileEntry();
      if (!selected) {
        controllerState.selectedFilePath = "";
        controllerState.filePreviewGeneration += 1;
        clearFileEditor();
        showFilePreviewMessage("No file selected", `Select a file or directory on ${controllerState.fileExecutorId}.`);
      }
      renderFileList();
      if (selected && previewSelection) void previewFile(selected);
      elements.fileState.textContent = `${controllerState.fileEntries.length} entries${payload.is_truncated ? " · truncated" : ""}`;
      return payload;
    } finally {
      if (generation === controllerState.fileListGeneration) {
        elements.fileRefresh.disabled = controllerState.fileMutationBusy || !fileExecutorOnline();
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
      controllerState.fileEditorSha256 = String(payload.file_sha256 || "");
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

  function validFileName(name) {
    return name && name !== "." && name !== ".." && !/[\\/\0]/.test(name) &&
      new TextEncoder().encode(name).length <= 255;
  }

  function showFileDialog(action, title, name) {
    controllerState.fileDialogAction = action;
    elements.fileOperationTitle.textContent = title;
    elements.fileOperationLabel.firstChild.textContent = action === "paste" ? "Destination name " : "Name ";
    elements.fileOperationName.value = name;
    elements.fileOperationDialog.showModal();
    elements.fileOperationName.select();
  }

  async function finishFileMutation(destination, message) {
    const target = splitFilePath(destination);
    if (await navigateFiles(target.parent, destination)) elements.fileState.textContent = message;
  }

  async function submitFileDialog(event) {
    event.preventDefault();
    if (controllerState.fileMutationBusy) return;
    const name = elements.fileOperationName.value.trim();
    if (!validFileName(name)) {
      elements.fileState.textContent = "Use one valid filename (up to 255 UTF-8 bytes).";
      return;
    }
    const action = controllerState.fileDialogAction;
    const entry = currentFileEntry();
    const clipboard = controllerState.fileClipboard;
    const target = joinFilePath(controllerState.filePath, name);
    setFileMutationBusy(true);
    try {
      if (action === "new-file") {
        await fileAction("write", { path: target, content: "", overwrite: false });
      } else if (action === "new-folder") {
        await fileAction("mkdir", { path: target });
      } else if (action === "rename") {
        if (!entry || !controllerState.fileMutations.rename) return;
        const result = await fileAction("rename", { path: entry.path, name });
        await finishFileMutation(text(result.destination, target), `Renamed ${entry.path}`);
      } else if (action === "paste") {
        if (!clipboard || !controllerState.fileMutations[clipboard.mode]) return;
        const result = await fileAction(clipboard.mode, { path: clipboard.path, destination: target });
        if (clipboard.mode === "move") controllerState.fileClipboard = null;
        renderClipboard();
        await finishFileMutation(text(result.destination, target), `${clipboard.mode === "copy" ? "Copied" : "Moved"} ${clipboard.path}`);
      }
      elements.fileOperationDialog.close();
      if (action === "new-file" || action === "new-folder") {
        controllerState.selectedFilePath = target;
        await refreshFiles({ previewSelection: true });
        if (action === "new-file") await openFileEditor();
        elements.fileState.textContent = `Created ${target}`;
      }
    } catch (error) {
      elements.fileState.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      setFileMutationBusy(false);
    }
  }

  function stageFileClipboard(mode) {
    const entry = currentFileEntry();
    if (!entry || !controllerState.fileMutations[mode]) return;
    controllerState.fileClipboard = { mode, path: entry.path };
    renderClipboard();
    elements.fileState.textContent = `${mode === "copy" ? "Copy" : "Cut"} ${entry.path}; navigate to the destination folder and choose Paste.`;
    setFileControls();
  }

  function pasteFile() {
    const clipboard = controllerState.fileClipboard;
    if (!clipboard) return;
    showFileDialog("paste", clipboard.mode === "copy" ? "Paste copy" : "Move here", splitFilePath(clipboard.path).name);
  }

  function uploadFileBytes(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
      reader.onerror = () => reject(reader.error || new Error("Unable to read local file"));
      reader.readAsDataURL(file);
    });
  }

  async function uploadSelectedFiles() {
    const files = Array.from(elements.fileUploadInput.files || []);
    elements.fileUploadInput.value = "";
    if (!files.length || controllerState.fileMutationBusy) return;
    if (files.length > 8 || files.some((file) => file.size > 2_000_000 || !validFileName(file.name))) {
      elements.fileState.textContent = "Upload up to 8 files, at most 2 MB each, with valid names.";
      return;
    }
    const destination = controllerState.filePath;
    setFileMutationBusy(true);
    try {
      for (const file of files) {
        elements.fileState.textContent = `Uploading ${file.name}`;
        await fileAction("upload", {
          path: joinFilePath(destination, file.name),
          data_base64: await uploadFileBytes(file),
        });
      }
      controllerState.selectedFilePath = joinFilePath(destination, files[files.length - 1].name);
      await refreshFiles({ previewSelection: true });
      elements.fileState.textContent = `Uploaded ${files.length} file(s)`;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      try {
        await refreshFiles();
      } finally {
        elements.fileState.textContent = `Upload failed: ${message}`;
      }
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
    controllerState.fileExecutorPinned = false;
    resetFileWorkspace(elements.fileExecutor.value);
    void refreshFiles();
  });
  elements.filePathForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!controllerState.fileMutationBusy) void navigateFiles(elements.filePath.value.trim() || ".");
  });
  elements.fileUp.addEventListener("click", () => void navigateFiles(controllerState.fileParentPath));
  document.getElementById("file-workspace-shortcut").addEventListener("click", () => {
    if (!controllerState.fileMutationBusy) void navigateFiles(".");
  });
  elements.fileRefresh.addEventListener("click", () => void refreshFiles());
  elements.fileShowHidden.addEventListener("change", () => {
    const entry = currentFileEntry();
    renderFileList();
    if (entry?.type === "dir" && controllerState.selectedFilePath === entry.path) {
      void previewFile(entry);
    }
  });
  elements.fileFilter.addEventListener("input", renderFileList);
  elements.fileSort.addEventListener("change", renderFileList);
  elements.fileSortDirection.addEventListener("click", () => {
    controllerState.fileSortDescending = !controllerState.fileSortDescending;
    elements.fileSortDirection.textContent = controllerState.fileSortDescending ? "Descending" : "Ascending";
    renderFileList();
  });
  elements.fileOperationCancel.addEventListener("click", () => elements.fileOperationDialog.close());
  elements.fileOperationDialog.addEventListener("cancel", (event) => {
    if (controllerState.fileMutationBusy) event.preventDefault();
  });
  elements.fileOperationForm.addEventListener("submit", (event) => void submitFileDialog(event));
  elements.fileNew.addEventListener("click", () => showFileDialog("new-file", "New file", ""));
  elements.fileNewFolder.addEventListener("click", () => showFileDialog("new-folder", "New folder", ""));
  elements.fileUpload.addEventListener("click", () => elements.fileUploadInput.click());
  elements.fileUploadInput.addEventListener("change", () => void uploadSelectedFiles());
  elements.fileOpen.addEventListener("click", openSelectedFile);
  elements.fileEdit.addEventListener("click", () => void openFileEditor());
  elements.fileCopy.addEventListener("click", () => stageFileClipboard("copy"));
  elements.fileMove.addEventListener("click", () => stageFileClipboard("move"));
  elements.filePaste.addEventListener("click", pasteFile);
  elements.fileRename.addEventListener("click", () => {
    const entry = currentFileEntry();
    if (entry) showFileDialog("rename", "Rename", text(entry.name, ""));
  });
  elements.fileDelete.addEventListener("click", () => void deleteSelectedFile());
  elements.fileEditorCancel.addEventListener("click", () => {
    const entry = currentFileEntry();
    controllerState.filePreviewGeneration += 1;
    clearFileEditor();
    elements.fileState.textContent = `${controllerState.fileExecutorId}:${controllerState.filePath}`;
    if (entry) void previewFile(entry);
    else showFilePreviewMessage("No file selected", `Select a file or directory on ${controllerState.fileExecutorId}.`);
  });
  elements.fileEditorReload.addEventListener("click", () => {
    if (controllerState.fileMutationBusy || !controllerState.fileEditorPath) return;
    if (!globalThis.confirm("Reload file from disk? Your unsaved edits will be discarded.")) return;
    void openFileEditor();
  });
  elements.fileEditorForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!controllerState.fileEditorPath || !controllerState.fileMutations.write || controllerState.fileMutationBusy) return;
    if (!/^[0-9a-f]{64}$/.test(controllerState.fileEditorSha256)) {
      elements.fileState.textContent = "File revision unavailable; reload before saving.";
      return;
    }
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
        expected_sha256: controllerState.fileEditorSha256,
      });
      controllerState.selectedFilePath = path;
      clearFileEditor();
      await refreshFiles();
      const entry = currentFileEntry();
      if (entry) await previewFile(entry);
      elements.fileState.textContent = `Saved ${controllerState.fileExecutorId}:${path}`;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("File changed; reload before saving")) {
        elements.fileState.textContent = "File changed on disk; your edits are preserved.";
        elements.filePreviewMeta.textContent = "Copy your edits before using Reload from disk to reconcile changes.";
      } else {
        elements.fileState.textContent = message;
      }
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
