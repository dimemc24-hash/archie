/**
 * Archie Console — no-build-step plain JS IIFE frontend.
 * Fetches config/models/deploy via the JSON API. Token stored in localStorage.
 */
(function () {
  "use strict";

  var API = "";
  var ROLES = ["panel_full", "panel_budget", "judge", "synth_full", "synth_budget"];
  var pendingChanges = {};
  var currentConfig = null;

  function getToken() {
    return localStorage.getItem("archie-console-token") || "";
  }

  function saveToken() {
    var v = document.getElementById("tokenInput").value;
    localStorage.setItem("archie-console-token", v);
    document.getElementById("tokenPrompt").classList.remove("show");
    loadConfig();
  }
  window.saveToken = saveToken;

  function showTokenPrompt() {
    document.getElementById("tokenPrompt").classList.add("show");
  }

  function authHeaders() {
    var t = getToken();
    return t ? { "Authorization": "Bearer " + t } : {};
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, authHeaders(), opts.headers || {});
    return fetch(API + path, opts).then(function (r) {
      if (r.status === 401) { showTokenPrompt(); throw new Error("401"); }
      return r.json().then(function (body) {
        if (!r.ok) throw body;
        return body;
      });
    });
  }

  function showStatus(msg, isErr) {
    var el = document.getElementById("status");
    if (!msg) { el.innerHTML = ""; return; }
    el.innerHTML = '<div class="status-msg ' + (isErr ? "status-err" : "status-ok") + '">' + msg + "</div>";
  }

  function loadConfig() {
    api("/api/config").then(function (res) {
      currentConfig = res;
      renderConfig(res);
    }).catch(function (e) { showStatus("Config load failed: " + (e.detail || e), true); });
  }

  function renderConfig(res) {
    var tbody = document.getElementById("configBody");
    tbody.innerHTML = "";
    ROLES.forEach(function (role) {
      var info = res.roles[role] || { value: "—", source: "default" };
      var tr = document.createElement("tr");
      var srcClass = info.source === "file" ? "badge-file" : "badge-default";
      var val = info.value;
      if (Array.isArray(val)) val = val.join(", ");
      tr.innerHTML = "<td>" + role + "</td><td>" + val + "</td>" +
        '<td><span class="badge ' + srcClass + '">' + info.source + "</span></td>";
      tbody.appendChild(tr);
    });
  }

  function searchModels() {
    var q = document.getElementById("searchInput").value;
    api("/api/models?q=" + encodeURIComponent(q)).then(function (res) {
      renderModels(res.models);
    }).catch(function (e) { showStatus("Models load failed: " + (e.detail || e), true); });
  }

  function refreshCatalog() {
    api("/api/models/refresh", { method: "POST" }).then(function (res) {
      searchModels();
    }).catch(function (e) { showStatus("Refresh failed: " + (e.detail || e), true); });
  }
  window.refreshCatalog = refreshCatalog;

  function renderModels(models) {
    var tbody = document.getElementById("modelsBody");
    tbody.innerHTML = "";
    models.forEach(function (m) {
      var tr = document.createElement("tr");
      var polClass = m.policy === "ok" ? "badge-ok" : m.policy === "warn-anthropic" ? "badge-warn" : "badge-blocked";
      var priced = m.priced_in_ledger ? '<span class="badge badge-ok">yes</span>' : '<span class="badge badge-warn">no</span>';
      var actions = ROLES.map(function (role) {
        var label = role.replace("_", " ").replace("panel", "panel:");
        return '<button onclick="useAs(\'' + role + "','" + m.id + "')\">" + label + "</button>";
      }).join("");
      tr.innerHTML = "<td>" + m.id + "</td>" +
        "<td>$" + (m.prompt_per_1m || 0) + " / $" + (m.completion_per_1m || 0) + "</td>" +
        "<td>" + (m.context_length || 0) + "</td>" +
        "<td>" + priced + "</td>" +
        '<td><span class="badge ' + polClass + '">' + m.policy + "</span></td>" +
        '<td class="model-actions">' + actions + "</td>";
      tbody.appendChild(tr);
    });
  }

  function useAs(role, slug) {
    pendingChanges[role] = slug;
    renderPending();
  }
  window.useAs = useAs;

  function renderPending() {
    var el = document.getElementById("pendingDiff");
    if (Object.keys(pendingChanges).length === 0) {
      el.innerHTML = '<p style="color:var(--muted);font-size:0.8rem">No pending changes.</p>';
      return;
    }
    var html = "";
    Object.keys(pendingChanges).forEach(function (role) {
      var old = currentConfig && currentConfig.roles[role] ? currentConfig.roles[role].value : "—";
      if (Array.isArray(old)) old = old.join(", ");
      html += '<div><span class="diff-old">' + role + ": " + old + "</span> → " +
        '<span class="diff-new">' + role + ": " + pendingChanges[role] + "</span></div>";
    });
    el.innerHTML = html;
  }

  function clearPending() {
    pendingChanges = {};
    renderPending();
  }
  window.clearPending = clearPending;

  function deployConfig() {
    if (Object.keys(pendingChanges).length === 0) { showStatus("Nothing to deploy", true); return; }
    api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pendingChanges),
    }).then(function (res) {
      showStatus("Deployed" + (res.warnings.length ? " with warnings" : " ok"), false);
      if (res.warnings && res.warnings.length) {
        var w = document.querySelector(".warnings");
        if (w) w.remove();
        w = document.createElement("div");
        w.className = "warnings";
        res.warnings.forEach(function (msg) {
          w.innerHTML += "<div>" + msg + "</div>";
        });
        document.querySelector(".pending").appendChild(w);
      }
      pendingChanges = {};
      renderPending();
      loadConfig();
    }).catch(function (e) {
      // 409 build_locked: surface the structured field the API returns.
      if (e && e.blocked && e.reason === "build_locked") {
        showStatus("Deploy blocked: a build is running. Retry after it completes.", true);
      } else {
        showStatus("Deploy failed: " + (e.detail || e), true);
      }
    });
  }
  window.deployConfig = deployConfig;

  // Init
  loadConfig();
  searchModels();
})();
