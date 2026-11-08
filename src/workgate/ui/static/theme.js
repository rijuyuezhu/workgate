// Apply before the stylesheet paints; the WebUI CSP disallows inline scripts.
(() => {
  "use strict";
  const key = "workgate-ui-theme";
  const choices = new Set(["auto", "light", "dark"]);

  function savedTheme() {
    try {
      const value = localStorage.getItem(key);
      if (choices.has(value)) return value;
    } catch {
      // Private browsing can disable local storage; the system theme still works.
    }
    return "auto";
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const selector = document.getElementById("appearance-mode");
    if (selector) selector.value = theme;
  }

  applyTheme(savedTheme());
  window.addEventListener("DOMContentLoaded", () => {
    const selector = document.getElementById("appearance-mode");
    if (!selector) return;
    // The selector is parsed after this head script has applied the root theme.
    selector.value = savedTheme();
    selector.addEventListener("change", () => {
      const theme = choices.has(selector.value) ? selector.value : "auto";
      applyTheme(theme);
      try {
        localStorage.setItem(key, theme);
      } catch {
        // Apply the chosen theme for this tab even when persistence is denied.
      }
    });
  });
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) applyTheme(savedTheme());
  });
})();
