// The shell opens this page with ?hub=<origin>&next=<hub /ui URL>&reason=<text>.
// It probes the hub with a no-cors fetch: an opaque response means something
// answered, a rejection means it is still unreachable. No Tauri IPC is used.
(function () {
  "use strict";
  var q = new URLSearchParams(location.search);
  var hub = q.get("hub") || "";
  var next = q.get("next") || "";
  var hubOrigin;
  try { hubOrigin = new URL(hub).origin; } catch (e) { hubOrigin = null; }
  // Only ever continue to a page on the hub this page was told about.
  try { if (!hubOrigin || new URL(next).origin !== hubOrigin) next = hubOrigin ? hubOrigin + "/ui" : ""; }
  catch (e) { next = hubOrigin ? hubOrigin + "/ui" : ""; }

  document.getElementById("hub").textContent = hubOrigin || "the hub (no URL configured)";
  var reason = q.get("reason");
  if (reason) {
    var el = document.getElementById("reason");
    el.textContent = "Last error: " + reason;
    el.hidden = false;
  }
  var status = document.getElementById("status");
  var busy = false;

  function probe() {
    if (!hubOrigin || busy) return;
    busy = true;
    status.textContent = "Checking…";
    fetch(hubOrigin + "/healthz", { mode: "no-cors", cache: "no-store" })
      .then(function () { status.textContent = "Hub answered. Opening…"; location.replace(next); })
      .catch(function () { status.textContent = "Still unreachable. Retrying every 5 seconds."; })
      .then(function () { busy = false; });
  }

  document.getElementById("retry").addEventListener("click", probe);
  setInterval(probe, 5000);
})();
