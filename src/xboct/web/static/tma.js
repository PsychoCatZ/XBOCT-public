// Telegram Mini App adapter. Loaded only on Mini App pages, after the official SDK.
// Theme colours are set through the CSSOM (style.setProperty), which the CSP allows.
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) { return; }
  var root = document.documentElement;
  var body = document.body;

  function version(v) {
    try { return tg.isVersionAtLeast(v); } catch (e) { return false; }
  }
  function set(name, value) {
    if (value) { root.style.setProperty(name, value); }
  }

  function applyTheme() {
    var p = tg.themeParams || {};
    root.setAttribute("data-scheme", tg.colorScheme === "dark" ? "dark" : "light");
    set("--bg", p.secondary_bg_color || p.bg_color);
    set("--card", p.bg_color);
    set("--text", p.text_color);
    set("--muted", p.hint_color);
    set("--accent", p.button_color);
    set("--accent-text", p.button_text_color);
    set("--danger", p.destructive_text_color);
    set("--line", p.section_separator_color);
    try {
      if (version("6.1")) {
        tg.setHeaderColor("bg_color");
        tg.setBackgroundColor("secondary_bg_color");
      }
    } catch (e) { /* the colours are cosmetic */ }
  }

  function applyInsets() {
    var s = tg.safeAreaInset || {};
    var c = tg.contentSafeAreaInset || {};
    root.style.setProperty("--tma-top", ((s.top || 0) + (c.top || 0)) + "px");
    root.style.setProperty("--tma-bottom", (s.bottom || 0) + "px");
  }

  function back() {
    var button = tg.BackButton;
    if (!button) { return; }
    if (window.location.pathname === "/") {
      button.hide();
    } else {
      button.show();
    }
  }

  tg.ready();
  tg.expand();
  applyTheme();
  applyInsets();
  back();
  tg.onEvent("themeChanged", applyTheme);
  if (version("8.0")) {
    tg.onEvent("safeAreaChanged", applyInsets);
    tg.onEvent("contentSafeAreaChanged", applyInsets);
  }
  if (tg.BackButton) {
    tg.BackButton.onClick(function () {
      if (window.history.length > 1) { window.history.back(); } else { window.location.assign("/"); }
    });
  }

  // The bottom bar must not cover fields while the keyboard is open.
  var fields = "input, textarea, select";
  document.addEventListener("focusin", function (event) {
    if (event.target.matches && event.target.matches(fields)) { body.classList.add("kb"); }
  });
  document.addEventListener("focusout", function () {
    window.setTimeout(function () {
      var active = document.activeElement;
      if (!(active && active.matches && active.matches(fields))) { body.classList.remove("kb"); }
    }, 50);
  });

  // Links to other sites leave the app through Telegram, not inside the webview.
  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href]") : null;
    if (!link || event.defaultPrevented) { return; }
    var url;
    try { url = new URL(link.href, window.location.href); } catch (e) { return; }
    // Marked links (the way out to the ordinary web) leave the Mini App even for our own
    // origin: that page must not be opened inside the frame.
    var leaves = url.origin !== window.location.origin || link.hasAttribute("data-external");
    if (leaves && /^https?:$/.test(url.protocol)) {
      event.preventDefault();
      tg.openLink(url.href);
    }
  });

  // Native confirmation where the client has one (runs before app.js's window.confirm).
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var question = form.getAttribute && form.getAttribute("data-confirm");
    if (!question || !version("6.2") || typeof tg.showConfirm !== "function") { return; }
    event.preventDefault();
    event.stopImmediatePropagation();
    var submitter = event.submitter;
    tg.showConfirm(question, function (ok) {
      if (!ok) { return; }
      form.removeAttribute("data-confirm");
      if (form.requestSubmit) { form.requestSubmit(submitter || undefined); } else { form.submit(); }
    });
  }, true);
})();
