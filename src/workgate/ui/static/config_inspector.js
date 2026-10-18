// On-demand, admin-only configuration details. No values enter bootstraps or logs.
export function createConfigurationView({ root, request, authMode, onAuthorize }) {
  let generation = 0;
  function clear() { generation++; root.replaceChildren(); }
  function text(tag, content, className = "") {
    const node = document.createElement(tag);
    node.textContent = content;
    if (className) node.className = className;
    return node;
  }
  function display(value) {
    return value == null ? "—" : typeof value === "string" ? value : JSON.stringify(value);
  }
  async function open(executorId = "") {
    const selected = ++generation;
    root.replaceChildren(text("p", "Loading configuration…", "muted"));
    try {
      const query = executorId ? `?executor_id=${encodeURIComponent(executorId)}` : "";
      const data = await request(`/config${query}`);
      if (selected !== generation) return;
      root.replaceChildren();
      if (data.status === "offline") {
        root.append(text("p", "Executor offline. Live configuration was not inspected; no cached credentials are shown.", "muted"));
        return;
      }
      if (data.status !== "running" || !Array.isArray(data.fields)) throw new Error("Live configuration unavailable");
      const description = text("p", `${data.config_path || "No configuration file selected"} · ${String(data.config_format || "yaml").toUpperCase()} · v${data.startup_version} · Started ${Number.isFinite(Number(data.startup_at)) ? new Date(Number(data.startup_at) * 1000).toLocaleString() : "unknown"}`, "muted");
      root.append(description);
      if (data.file_changed_since_startup) root.append(text("p", "The file has changed since startup. Effective values still reflect the running process.", "executor-config-warning"));
      if (data.file_error) root.append(text("p", data.file_error, "executor-config-warning"));
      root.append(text("p", "Read-only · Credentials are displayed unmasked for the authorized administrator. Saving or restarting is not available here.", "muted"));
      const groups = new Map();
      for (const field of data.fields) {
        if (!groups.has(field.section)) groups.set(field.section, []);
        groups.get(field.section).push(field);
      }
      for (const [section, fields] of groups) {
        const details = document.createElement("details");
        details.className = "executor-config-section";
        details.open = section === groups.keys().next().value;
        details.append(text("summary", `${section} (${fields.length})`));
        const table = document.createElement("table");
        table.className = "executor-config-table";
        const head = document.createElement("thead");
        const header = document.createElement("tr");
        for (const title of ["Setting", "Effective", "File", "Source"]) header.append(text("th", title));
        head.append(header); table.append(head);
        const tbody = document.createElement("tbody");
        for (const field of fields) {
          const tr = document.createElement("tr");
          if (field.file_overridden) tr.className = "executor-config-overridden";
          const name = text("td", field.name);
          name.title = field.description || "";
          tr.append(name);
          tr.append(text("td", display(field.effective_value), "executor-config-value"));
          tr.append(text("td", field.file_present ? display(field.file_value) : "—", "executor-config-value"));
          tr.append(text("td", field.file_overridden ? `${field.source} (overrides file)` : field.source));
          tbody.append(tr);
        }
        table.append(tbody); details.append(table); root.append(details);
      }
    } catch (error) {
      if (selected !== generation) return;
      root.replaceChildren();
      if (error.status === 403) {
        root.append(text("p", "Configuration credentials require an explicitly approved config:admin OAuth grant. Ordinary WebUI access is insufficient.", "muted"));
        if (authMode === "oauth") {
          const button = text("button", "Authorize configuration access");
          button.type = "button";
          button.addEventListener("click", onAuthorize);
          root.append(button);
        }
      } else {
        root.append(text("p", "Unable to inspect running configuration. Try again when the service is available.", "muted"));
      }
    }
  }
  return { open, clear };
}
