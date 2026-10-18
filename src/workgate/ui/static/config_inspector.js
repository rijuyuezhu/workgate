// Runtime settings; neither values nor errors are cached or logged.
export function createConfigurationView({ root, request }) {
  let generation = 0;
  const text = (tag, value, className = "") => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  const display = value => value === null ? "null" : value === "" ? '""' : typeof value === "string" ? value : JSON.stringify(value);
  function clear() {
    generation++;
    root.replaceChildren();
  }
  async function open(executorId = "") {
    const revision = ++generation;
    root.replaceChildren(text("span", "Loading…", "muted"));
    try {
      const path = executorId ? `?executor_id=${encodeURIComponent(executorId)}` : "";
      const data = await request(`/config${path}`);
      if (revision !== generation) return;
      root.replaceChildren();
      if (data.status === "offline") {
        root.append(text("span", "Executor offline", "muted"));
        return;
      }
      if (data.status !== "running" || !Array.isArray(data.fields)) throw new Error("No config");

      const metadata = text("div", "", "executor-config-meta");
      const filename = data.config_path ? data.config_path.split(/[\\/]/).pop() : "No config file";
      const file = text("span", filename, "executor-config-file");
      file.title = data.config_path || "";
      metadata.append(file);
      if (data.file_changed_since_startup) metadata.append(text("span", "File changed", "executor-config-warning"));
      if (data.file_error) metadata.append(text("span", "File unavailable", "executor-config-warning"));
      root.append(metadata);

      const groups = new Map();
      for (const field of data.fields) {
        if (!groups.has(field.section)) groups.set(field.section, []);
        groups.get(field.section).push(field);
      }
      const list = text("div", "", "executor-config-list");
      for (const [section, fields] of groups) {
        const details = document.createElement("details");
        details.className = "executor-config-section executor-manage-group";
        details.open = section === groups.keys().next().value;
        const summary = document.createElement("summary");
        summary.append(text("strong", section), text("span", String(fields.length), "executor-config-count"));
        details.append(summary);
        for (const field of fields) {
          const item = text("div", "", "executor-config-row");
          const name = text("span", field.name, "executor-config-name");
          name.title = field.description || "";
          const info = text("div", "", "executor-config-info");
          const primary = text("div", "", "executor-config-primary");
          primary.append(
            text("code", display(field.effective_value), "executor-config-value"),
            text("span", field.source, "executor-config-source"),
          );
          info.append(primary);
          if (field.file_present && display(field.file_value) !== display(field.effective_value)) {
            const oldValue = text("div", "", "executor-config-file-value");
            oldValue.append(text("span", "File"), text("code", display(field.file_value)));
            info.append(oldValue);
          }
          item.append(name, info);
          details.append(item);
        }
        list.append(details);
      }
      root.append(list);
    } catch (error) {
      if (revision !== generation) return;
      root.replaceChildren();
      root.append(text("span", error.status === 403 ? "Access denied" : "Configuration unavailable", "muted"));
    }
  }
  return { open, clear };
}
