// «Добавить на главный экран»: only inside the Mini App, only with the official API
// (Telegram.WebApp.checkHomeScreenStatus / addToHomeScreen, Bot API 8.0+).
//
// - The shortcut is requested only when the owner presses the button, never automatically
//   (also not as a retry after an error).
// - A call is never reported as a success: on some devices the status and the
//   homeScreenAdded event are not available, so «added» is shown only when Telegram itself
//   says so.
// - A confirmed «added» (status or event) is final for this page: a later «unknown» (or any
//   other answer) does not take it back, and neither does a stale answer of an older check
//   or the initial fallback timer.
(function () {
  "use strict";
  var box = document.querySelector("[data-home-screen]");
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!box || !tg) { return; }

  var supported = false;
  try { supported = tg.isVersionAtLeast("8.0"); } catch (e) { supported = false; }
  if (!supported || typeof tg.checkHomeScreenStatus !== "function" ||
      typeof tg.addToHomeScreen !== "function") {
    return;  // the section stays hidden
  }

  var button = box.querySelector("[data-action=add-home-screen]");
  var confirmed = false;  // Telegram said «added»
  var requested = false;  // the owner pressed the button (no confirmation yet)
  var failed = false;     // the last press raised an error
  var answered = false;   // some check has answered
  var seq = 0;            // number of the latest check
  var applied = 0;        // number of the newest check whose answer was used

  function show(name, withButton) {
    box.querySelectorAll("[data-state]").forEach(function (el) {
      el.hidden = el.getAttribute("data-state") !== name;
    });
    button.hidden = !withButton;
    box.hidden = name === null;
  }

  function confirm() {
    confirmed = true;
    show("added", false);
  }

  function showOpen(status) {
    if (failed) { show("error", true); }
    else if (requested) { show("requested", true); }
    else { show(status === "missed" ? "offer" : "unknown", true); }
  }

  function onStatus(status, n) {
    if (n < applied) { return; }  // an older check answering after a newer one
    applied = n;
    answered = true;
    if (confirmed) { return; }    // a confirmed «added» is not taken back
    if (status === "added") { confirm(); }
    else if (status === "missed" || status === "unknown") { showOpen(status); }
    else { show(null, false); }   // "unsupported" or anything unexpected: nothing to offer
  }

  function check() {
    var n = ++seq;
    try { tg.checkHomeScreenStatus(function (status) { onStatus(status, n); }); }
    catch (e) { /* no answer: the fallback below covers the first check */ }
  }

  button.addEventListener("click", function () {
    if (confirmed) { return; }
    requested = true;
    failed = false;
    show("requested", true);
    try {
      tg.addToHomeScreen();
    } catch (e) {
      requested = false;
      failed = true;
      show("error", true);  // neutral; trying again is the owner's click, not ours
      return;
    }
    window.setTimeout(check, 1500);  // ask again; «added» only if Telegram reports it
  });

  tg.onEvent("homeScreenAdded", confirm);
  tg.onEvent("homeScreenChecked", function (data) {
    if (data && typeof data.status === "string") { onStatus(data.status, seq); }
  });

  check();
  // Some devices never answer: the status is then simply unknown, the button stays usable.
  window.setTimeout(function () {
    if (!answered && !confirmed) { onStatus("unknown", seq); }
  }, 1500);
})();
