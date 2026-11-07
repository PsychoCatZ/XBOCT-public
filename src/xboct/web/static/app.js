// Optional enhancements. Every form works without this file.
(function () {
  "use strict";

  // Confirm destructive actions.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      event.preventDefault();
      return;
    }
    // Double submit is harmless (the same idem key replays), but avoid the extra request.
    var buttons = form.querySelectorAll("button[type=submit]");
    window.setTimeout(function () {
      buttons.forEach(function (b) { b.disabled = true; });
    }, 0);
  });

  // Show only the fields that apply: data-when="name=value1,value2".
  function currentValue(name, scope) {
    var fields = (scope || document).querySelectorAll("[name='" + name + "']");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "radio") {
        if (f.checked) return f.value;
      } else {
        return f.value;
      }
    }
    return null;
  }

  function refresh() {
    document.querySelectorAll("[data-when]").forEach(function (el) {
      var rule = el.getAttribute("data-when");
      var eq = rule.indexOf("=");
      var name = rule.slice(0, eq);
      var allowed = rule.slice(eq + 1).split(",");
      var value = currentValue(name, el.closest("form"));
      el.hidden = value !== null && allowed.indexOf(value) === -1;
    });
  }

  document.addEventListener("change", function (event) {
    if (event.target.name) refresh();
  });
  document.addEventListener("DOMContentLoaded", refresh);
  // Coming back via the browser history: enable buttons again.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("button[disabled]").forEach(function (b) { b.disabled = false; });
  });
})();
