// Keep authenticated configuration mutations and restart recovery owned by this controller.
export function createConfigurationView({ root, request, mountConfiguration }) {
  const view = mountConfiguration(root);
  let generation = 0;
  function clear() { view.render({ generation: ++generation, state: "blank" }); }

  async function open(executorId = "", retry = 0, notice = "") {
    const revision = ++generation;
    view.render({ generation: revision, state: "loading" });
    const path = executorId ? `?executor_id=${encodeURIComponent(executorId)}` : "";
    try {
      const data = await request(`/config${path}`);
      if (revision !== generation) return;
      if (data.status === "offline") {
        view.render({ generation: revision, state: "offline", message: "Executor offline",
          events: retry < 5 ? undefined : { retry: () => void open(executorId) } });
        if (retry < 5) {
          await new Promise(resolve => setTimeout(resolve, 1500));
          if (generation === revision) await open(executorId, retry + 1);
        }
        return;
      }
      if (data.status !== "running" || !Array.isArray(data.fields)) throw new Error("No config");
      view.render({ generation: revision, state: "ready", data, notice, events: {
        retry: () => void open(executorId),
        save: async (changes) => {
          if (revision !== generation) return;
          await request(`/config${path}`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ revision: data.revision, changes }),
          });
          if (revision === generation) await open(executorId, 0, "Saved. Restart to apply.");
        },
        apply: async (progress) => {
          if (revision !== generation) return true;
          await request(`/config/apply${path}`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ revision: data.revision }),
          });
          if (revision !== generation) return true;
          progress("Restart requested. Reconnecting…");
          // Allow one bounded startup attempt and one bounded rollback.
          for (let attempt = 0; attempt < 60; attempt++) {
            await new Promise(resolve => setTimeout(resolve, 1000));
            if (revision !== generation) return true;
            try {
              const fresh = await request(`/config${path}`);
              if (fresh.status !== "running" || fresh.startup_at === data.startup_at ||
                !["healthy", "recovered"].includes(fresh.apply_status)) continue;
              await open(executorId, 0, fresh.apply_status === "recovered"
                ? "Previous configuration restored" : "Configuration applied");
              return true;
            } catch { /* The Child may be temporarily unavailable. */ }
          }
          return false;
        },
      } });
    } catch (error) {
      if (revision === generation) view.render({ generation: revision, state: "error",
        message: error.status === 403 ? "Access denied" : "Configuration unavailable" });
    }
  }
  return { open, clear };
}
