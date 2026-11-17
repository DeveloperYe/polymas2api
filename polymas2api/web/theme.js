/* Theme bootstrap: run before the stylesheet paints so the page never flashes
 * the wrong mode. The choice is "auto" (follow the system) or a fixed mode.
 */
(function () {
  "use strict";

  var KEY = "polymas2api.theme";
  var root = document.documentElement;

  function stored() {
    try { return localStorage.getItem(KEY) || "auto"; } catch (e) { return "auto"; }
  }

  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function apply(mode) {
    var resolved = mode === "auto" ? (systemPrefersDark() ? "dark" : "light") : mode;
    // The colour scheme follows the data-theme attribute through CSS
    // (`color-scheme` in :root and html[data-theme="dark"]), so no inline
    // style is needed — and an inline style would be blocked by the CSP.
    root.setAttribute("data-theme", resolved);
    var toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.setAttribute("title", mode === "auto" ? "主题：跟随系统" : ("主题：" + mode));
      toggle.setAttribute("aria-label", toggle.getAttribute("title"));
    }
  }

  // Paint immediately with the stored preference.
  var mode = stored();
  root.setAttribute("data-theme", mode === "auto" ? (systemPrefersDark() ? "dark" : "light") : mode);

  window.polymasTheme = {
    get: function () { return stored(); },
    set: function (next) {
      try { localStorage.setItem(KEY, next); } catch (e) { /* private mode */ }
      apply(next);
      return next;
    },
    cycle: function () {
      var current = stored();
      var next = current === "auto" ? "light" : (current === "light" ? "dark" : "auto");
      return this.set(next);
    },
    apply: apply
  };

  // Follow the system while the mode is "auto".
  if (window.matchMedia) {
    var query = window.matchMedia("(prefers-color-scheme: dark)");
    var onChange = function () { if (stored() === "auto") { apply("auto"); } };
    if (query.addEventListener) { query.addEventListener("change", onChange); }
    else if (query.addListener) { query.addListener(onChange); }
  }
})();
