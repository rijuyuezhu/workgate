// Edits only the authoritative file; live Settings remain unchanged until Apply.
export function createConfigurationView({ root, request }) {
  let generation = 0;
  const text = (tag, value, className = "") => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  const display = value => value === null ? "null" : value === "" ? '""' : typeof value === "string" ? value : JSON.stringify(value);
  function clear() { generation++; root.replaceChildren(); }
  async function open(executorId = "") {
    const revision = ++generation;
    root.replaceChildren(text("span", "Loading…", "muted"));
    const path = executorId ? `?executor_id=${encodeURIComponent(executorId)}` : "";
    try {
      const data = await request(`/config${path}`);
      if (revision !== generation) return;
      root.replaceChildren();
      if (data.status === "offline") { root.append(text("span", "Executor offline", "muted")); return; }
      if (data.status !== "running" || !Array.isArray(data.fields)) throw new Error("No config");
      const pending = new Map();
      const rows = new Map();
      const sections = new Map();
      const metadata = text("div", "", "executor-config-meta");
      const filename = data.config_path ? data.config_path.split(/[\\/]/).pop() : "No config file";
      const file = text("span", filename, "executor-config-file");
      file.title = data.config_path || "";
      metadata.append(file, text("span", `${data.fields.length} settings`, "executor-config-count"));
      if (data.file_changed_since_startup) metadata.append(text("span", "File changed", "executor-config-warning"));
      if (data.file_error) metadata.append(text("span", "File unavailable", "executor-config-warning"));
      if (!data.editable && data.edit_reason) metadata.append(text("span", data.edit_reason, "muted"));
      root.append(metadata);

      const filters = text("div", "", "executor-config-filters");
      const filter = text("label", "", "executor-config-filter");
      const changedOnly = document.createElement("input");
      changedOnly.type = "checkbox";
      const filterLabel = text("span", "Only changed (0)");
      filter.append(changedOnly, filterLabel);
      filter.hidden = !data.editable;
      const nonDefaultFilter = text("label", "", "executor-config-filter");
      nonDefaultFilter.title = "Show settings whose file, pending, or effective value differs from Default";
      const nonDefaultOnly = document.createElement("input");
      nonDefaultOnly.type = "checkbox";
      const nonDefaultLabel = text("span", "Non-default");
      nonDefaultFilter.append(nonDefaultOnly, nonDefaultLabel);
      filters.append(filter, nonDefaultFilter);
      root.append(filters);
      const toolbar = text("div", "", "executor-config-actions");
      const save = text("button", "Save changes"); save.type = "button";
      const discard = text("button", "Discard"); discard.type = "button";
      const status = text("span", "", "executor-config-status");
      toolbar.append(save, discard, status); root.append(toolbar);
      const controls = new Map();
      const empty = text("p", "No unsaved changes", "executor-config-empty");
      empty.hidden = true;
      const differs = (a, b) => JSON.stringify(a) !== JSON.stringify(b);
      const isNonDefault = (field, action) =>
        differs(field.effective_value, field.default_value) ||
        (action ? action.op === "set" && differs(action.value, field.default_value) :
          field.file_present && differs(field.file_value, field.default_value));
      const updateToolbar = () => {
        toolbar.hidden = pending.size === 0 && !status.textContent;
        save.disabled = pending.size === 0;
        discard.hidden = pending.size === 0;
        filterLabel.textContent = `Only changed (${pending.size})`;
        const visibleNames = new Set();
        let nonDefaultCount = 0;
        for (const [name, {item, field, oldValue, arrow, newValue}] of rows) {
          const action = pending.get(name);
          const nonDefault = isNonDefault(field, action);
          if (nonDefault) nonDefaultCount++;
          const visible = (!changedOnly.checked || !!action) && (!nonDefaultOnly.checked || nonDefault);
          item.classList.toggle("executor-config-changed", !!action);
          item.hidden = !visible;
          if (visible) visibleNames.add(name);
          const oldText = field.file_present ? display(field.file_value) : "unset";
          const newText = action?.op === "remove" ? "unset" : action ? display(action.value) : "";
          oldValue.textContent = oldText;
          oldValue.classList.toggle("executor-config-unset", !field.file_present);
          arrow.hidden = !action;
          newValue.hidden = !action;
          newValue.textContent = newText;
          newValue.classList.toggle("executor-config-unset", !!action && action.op === "remove");
        }
        nonDefaultLabel.textContent = `Non-default (${nonDefaultCount})`;
        const filtering = changedOnly.checked || nonDefaultOnly.checked;
        for (const {details, count, names} of sections.values()) {
          const shown = names.filter(name => visibleNames.has(name)).length;
          details.hidden = shown === 0;
          count.textContent = filtering ? `${shown}/${names.length}` : String(names.length);
          if (filtering && shown) details.open = true;
        }
        empty.textContent = changedOnly.checked && !nonDefaultOnly.checked ? "No unsaved changes" : "No matching settings";
        empty.hidden = !filtering || visibleNames.size > 0;
      };
      let wasFiltering = false;
      const updateFilters = () => {
        const filtering = changedOnly.checked || nonDefaultOnly.checked;
        if (filtering && !wasFiltering) {
          for (const section of sections.values()) section.priorOpen = section.details.open;
        } else if (!filtering && wasFiltering) {
          for (const {details, priorOpen} of sections.values()) details.open = priorOpen;
        }
        wasFiltering = filtering;
        updateToolbar();
      };
      changedOnly.addEventListener("change", updateFilters);
      nonDefaultOnly.addEventListener("change", updateFilters);
      const stage = (field, action) => {
        const same = action.op === "remove" ? !field.file_present : field.file_present && JSON.stringify(action.value) === JSON.stringify(field.file_value);
        if (same) pending.delete(field.name);
        else pending.set(field.name, action);
        status.textContent = "";
        controls.get(field.name)?.();
        updateToolbar();
      };
      const closeEditors = () => {
        for (const close of controls.values()) close();
        pending.clear(); status.textContent = ""; updateToolbar();
      };
      discard.addEventListener("click", closeEditors);
      save.addEventListener("click", async () => {
        if (revision !== generation || !data.editable || !pending.size) return;
        save.disabled = true;
        status.textContent = "Saving…";
        try {
          const changes = Object.fromEntries(pending);
          await request(`/config${path}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({revision: data.revision, changes})});
          if (revision !== generation) return;
          await open(executorId);
          if (revision + 1 === generation) {
            const notice = text("span", "Saved · Restart to apply", "executor-config-success");
            root.prepend(notice);
          }
        } catch (error) {
          if (revision !== generation) return;
          status.textContent = error.status === 409 ? "File changed · Reload to edit" : error.status === 400 ? "Invalid value" : "Save failed";
          save.disabled = false;
        }
      });
      const groups = new Map();
      for (const field of data.fields) {
        if (!groups.has(field.section)) groups.set(field.section, []);
        groups.get(field.section).push(field);
      }
      const headings = text("div", "", "executor-config-headings");
      headings.append(text("span", "Setting"), text("span", "Config"), text("span", "Default"));
      const list = text("div", "", "executor-config-list");
      for (const [section, fields] of groups) {
        const details = document.createElement("details");
        details.className = "executor-config-section executor-manage-group";
        details.open = true;
        const summary = document.createElement("summary");
        const count = text("span", String(fields.length), "executor-config-count");
        summary.append(text("strong", section), count);
        details.append(summary);
        sections.set(section, {details, count, names: fields.map(field => field.name), priorOpen: details.open});
        for (const field of fields) {
          const item = text("div", "", "executor-config-row");
          const name = text("span", field.name, "executor-config-name"); name.title = field.description || "";
          const info = text("div", "", "executor-config-info");
          const fileValue = text("div", "", "executor-config-transition");
          const oldValue = text("code", field.file_present ? display(field.file_value) : "unset", "executor-config-value");
          const arrow = text("span", "→", "executor-config-arrow"); arrow.hidden = true;
          const newValue = text("code", "", "executor-config-value"); newValue.hidden = true;
          fileValue.append(oldValue, arrow, newValue);
          info.append(fileValue);
          const expected = field.file_present ? field.file_value : field.default_value;
          if (JSON.stringify(field.effective_value) !== JSON.stringify(expected)) {
            const effective = text("div", "", "executor-config-effective");
            effective.append(text("span", "Effective"), text("code", display(field.effective_value)), text("span", field.source, "executor-config-source"));
            info.append(effective);
          }
          const defaultCell = text("div", "", "executor-config-default");
          defaultCell.append(text("span", "Default", "executor-config-mobile-heading"), text("code", display(field.default_value)));
          item.append(name, info, defaultCell);
          rows.set(field.name, {item, field, oldValue, arrow, newValue});
          if (data.editable) {
            const edit = text("button", "Edit", "executor-config-edit"); edit.type = "button";
            item.append(edit);
            const editor = text("div", "", "executor-config-editor"); editor.hidden = true;
            item.append(editor);
            const close = () => { editor.replaceChildren(); editor.hidden = true; edit.hidden = false; };
            controls.set(field.name, close);
            edit.addEventListener("click", () => {
              close(); edit.hidden = true; editor.hidden = false;
              const action = pending.get(field.name);
              const start = action && action.op === "set" ? action.value : field.file_present ? field.file_value : field.default_value;
              let input;
              if (field.type === "bool" || (field.choices && field.choices.length)) {
                input = document.createElement("select");
                const options = field.type === "bool" ? ["true", "false"] : field.choices;
                for (const choice of options) { const opt = text("option", choice); opt.value = choice; input.append(opt); }
                input.value = display(start);
              } else {
                input = document.createElement(field.type === "json" ? "textarea" : "input");
                if (field.type === "int" || field.type === "float") { input.type = "number"; input.step = field.type === "int" ? "1" : "any"; }
                else if (field.type !== "json") input.type = "text";
                input.value = field.type === "json" ? JSON.stringify(start) : start == null ? "" : String(start);
              }
              input.className = "executor-config-input";
              const set = text("button", "Set"); set.type = "button";
              const unset = text("button", "Unset"); unset.type = "button";
              const setNull = text("button", "Set null"); setNull.type = "button";
              const cancel = text("button", "Cancel"); cancel.type = "button";
              set.addEventListener("click", () => {
                let value = input.value;
                try {
                  if (field.type === "bool") value = value === "true";
                  else if (field.type === "int" || field.type === "float") { value = Number(value); if (!input.value.trim() || !Number.isFinite(value) || (field.type === "int" && !Number.isInteger(value))) throw Error(); }
                  else if (field.type === "json") value = JSON.parse(value);
                } catch { status.textContent = "Invalid value"; updateToolbar(); return; }
                stage(field, {op: "set", value});
              });
              unset.addEventListener("click", () => stage(field, {op: "remove"}));
              setNull.addEventListener("click", () => stage(field, {op: "set", value: null}));
              cancel.addEventListener("click", close);
              editor.append(input, set);
              if (field.file_present || pending.has(field.name)) editor.append(unset);
              if (field.nullable) editor.append(setNull);
              editor.append(cancel);
              input.focus();
            });
          }
          details.append(item);
        }
        list.append(details);
      }
      root.append(headings, list, empty); updateToolbar();
    } catch (error) {
      if (revision !== generation) return;
      root.replaceChildren(text("span", error.status === 403 ? "Access denied" : "Configuration unavailable", "muted"));
    }
  }
  return { open, clear };
}
