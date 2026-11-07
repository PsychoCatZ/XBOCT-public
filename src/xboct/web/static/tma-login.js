// Mini App sign-in: send the signed initData to the server, then enter the app.
// Only the server decides; initDataUnsafe and the mere presence of Telegram.WebApp grant nothing.
(function () {
  "use strict";
  var box = document.querySelector(".tma-entry");
  if (!box || !box.querySelector("[data-state]")) { return; }  // «session ended» page

  function show(name) {
    box.querySelectorAll("[data-state]").forEach(function (el) {
      el.hidden = el.getAttribute("data-state") !== name;
    });
  }

  var closeButton = box.querySelector("[data-action=close]");
  if (closeButton) {
    closeButton.addEventListener("click", function () {
      if (window.Telegram && Telegram.WebApp) { Telegram.WebApp.close(); }
    });
  }

  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg || !tg.initData) { show("no-telegram"); return; }

  var next = box.getAttribute("data-next") || "/";
  fetch("/tma/auth", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ init_data: tg.initData })
  }).then(function (response) {
    return response.json().catch(function () { return {}; }).then(function (body) {
      if (response.ok && body.ok) {
        // The cookie must really be kept before navigating: otherwise «/» would send the
        // browser back here and the sign-in would repeat.
        return fetch("/tma/session", { credentials: "same-origin" }).then(function (check) {
          if (check.ok) { window.location.replace(next); } else { show("cookies"); }
        });
      }
      if (response.status === 429) { show("throttled"); }
      else if (body.error === "stale" || body.error === "future") { show("stale"); }
      else { show("refused"); }
    });
  }).catch(function () { show("error"); });
})();
