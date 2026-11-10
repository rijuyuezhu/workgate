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

  let currentTheme = savedTheme();

  function applyTheme(theme) {
    currentTheme = theme;
    document.documentElement.dataset.theme = theme;
    window.dispatchEvent(new Event("workgate-theme-change"));
  }

  // The head script stays responsible for pre-paint application, persistence
  // and cross-tab changes. React only renders the preference control.
  window.WorkgateTheme = Object.freeze({
    get: () => currentTheme,
    set(value) {
      const theme = choices.has(value) ? value : "auto";
      applyTheme(theme);
      try {
        localStorage.setItem(key, theme);
      } catch {
        // The selected theme still applies to this tab.
      }
    },
  });

  applyTheme(currentTheme);
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) applyTheme(savedTheme());
  });

})();
