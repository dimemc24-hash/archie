/**
 * Hermes Dashboard Plugin — Swarm / Hetzner
 *
 * Swarm box liveness (via hetzner_power.sh) + swarm-report.json and fix-log.json
 * from the active or most recent fix branch. Shows build→swarm status, fixes
 * committed, wave results, and findings.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React +
 * shadcn primitives + fetchJSON.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const {
    Card, CardHeader, CardTitle, CardContent,
    Badge, Button, Separator,
  } = SDK.components;
  const { useState, useEffect } = SDK.hooks;
  const { timeAgo } = SDK.utils;

  var API = "/api/plugins/swarm-hetzner";

  var STATUS_COLORS = {
    running: "#22c55e",
    off: "#ef4444",
    starting: "#f59e0b",
    unknown: "#6b7280",
    error: "#ef4444",
  };

  function SwarmHetznerPage() {
    var livenessSt = useState(null);
    var liveness = livenessSt[0], setLiveness = livenessSt[1];
    var runsSt = useState(null);
    var runs = runsSt[0], setRuns = runsSt[1];
    var selSt = useState(null);
    var selectedRun = selSt[0], setSelectedRun = selSt[1];
    var detailSt = useState(null);
    var detail = detailSt[0], setDetail = detailSt[1];
    var errSt = useState(null);
    var error = errSt[0], setError = errSt[1];

    // Fetch liveness + runs on mount, auto-refresh liveness every 15s
    useEffect(function () {
      function loadLiveness() {
        SDK.fetchJSON(API + "/liveness")
          .then(function (res) { setLiveness(res); })
          .catch(function (e) { /* silent — liveness is best-effort */ });
      }
      function loadRuns() {
        SDK.fetchJSON(API + "/runs")
          .then(function (res) {
            setRuns(res);
            if (!selectedRun && res.runs && res.runs.length > 0) {
              setSelectedRun(res.runs[0].run_id);
            }
          })
          .catch(function (e) { setError(String(e.message || e)); });
      }
      loadLiveness();
      loadRuns();
      var lv = setInterval(loadLiveness, 15000);
      return function () { clearInterval(lv); };
    }, []);

    // Fetch detail when selectedRun changes
    useEffect(function () {
      if (!selectedRun) { setDetail(null); return; }
      SDK.fetchJSON(API + "/runs/" + encodeURIComponent(selectedRun))
        .then(function (res) { setDetail(res); setError(null); })
        .catch(function (e) { setDetail(null); setError(String(e.message || e)); });
    }, [selectedRun]);

    return h("div", { style: { padding: "1.5rem", maxWidth: "1100px" } },
      // Header
      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "1rem" } },
        h("h2", { style: { fontSize: "1.25rem", fontWeight: 600, margin: 0 } }, "Swarm / Hetzner"),
      ),
      // Liveness card
      h(Card, { style: { marginBottom: "1rem" } },
        h(CardHeader, null, h(CardTitle, null, "Swarm Box Liveness")),
        h(CardContent, null,
          liveness
            ? h("div", { style: { display: "flex", alignItems: "center", gap: "0.75rem" } },
                h("div", {
                  style: {
                    width: "12px", height: "12px", borderRadius: "50%",
                    background: STATUS_COLORS[liveness.status] || "#6b7280",
                    display: "inline-block",
                  },
                }),
                h("span", { style: { fontWeight: 600, textTransform: "capitalize" } }, liveness.status),
                liveness.raw
                  ? h("span", { style: { fontSize: "0.8rem", color: "#6b7280", fontFamily: "monospace" } }, liveness.raw)
                  : null,
                liveness.checked_at
                  ? h("span", { style: { fontSize: "0.75rem", color: "#6b7280", marginLeft: "auto" } },
                      "checked " + timeAgo(new Date(liveness.checked_at).getTime()))
                  : null,
              )
            : h("span", { style: { color: "#6b7280" } }, "Checking…"),
        )
      ),
      // Run selector
      runs && runs.runs && runs.runs.length > 0
        ? h("div", { style: { marginBottom: "1rem", display: "flex", gap: "0.5rem", flexWrap: "wrap" } },
            runs.runs.map(function (r) {
              var isSel = r.run_id === selectedRun;
              return h(Button, {
                key: r.run_id, size: "sm",
                variant: isSel ? "default" : "outline",
                onClick: function () { setSelectedRun(r.run_id); },
              }, r.run_id);
            })
          )
        : null,
      // Error
      error ? h(Card, { style: { borderColor: "#ef4444", marginBottom: "1rem" } },
        h(CardContent, { style: { paddingTop: "0.75rem" } },
          h("pre", { style: { color: "#ef4444", fontSize: "0.85rem", whiteSpace: "pre-wrap" } }, error)
        )
      ) : null,
      // Detail
      detail ? h(SwarmDetail, { detail: detail }) : null,
    );
  }

  function SwarmDetail(props) {
    var d = props.detail;
    var sr = d.swarm_report;
    var ws = d.wave_summaries || [];

    return h("div", { style: { display: "grid", gap: "1rem" } },
      // Swarm report
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Swarm Report — " + d.run_id)),
        h(CardContent, null,
          sr
            ? h("div", { style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "0.5rem 1rem", fontSize: "0.875rem" } },
                h("span", { style: { color: "#6b7280" } }, "Status:"),
                h("span", { style: { fontWeight: 600 } }, sr.status || "—"),
                h("span", { style: { color: "#6b7280" } }, "Fixes committed:"),
                h("span", null, String(sr.fixes_committed || "—")),
                h("span", { style: { color: "#6b7280" } }, "Build branch:"),
                h("span", { style: { fontFamily: "monospace", fontSize: "0.8rem" } }, sr.build_branch || "—"),
                h("span", { style: { color: "#6b7280" } }, "Waves:"),
                h("span", null, String(sr.waves || "—")),
                h("span", { style: { color: "#6b7280" } }, "Source:"),
                h(Badge, { variant: "secondary" }, d.swarm_report_source || "—"),
              )
            : h("p", { style: { color: "#6b7280" } },
                "No swarm-report.json found for this run (not a harness fix branch, or the run hasn't reached Stage 3 yet)."),
        )
      ),
      // Wave summaries
      ws.length > 0
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Wave Results (" + ws.length + " waves)")),
            h(CardContent, null,
              h("div", { style: { display: "grid", gap: "0.75rem" } },
                ws.map(function (w) {
                  return h("div", {
                    key: w.wave,
                    style: { border: "1px solid #e5e7eb", borderRadius: "0.5rem", padding: "0.5rem" },
                  },
                    h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "0.25rem" } },
                      h("span", { style: { fontWeight: 600, fontSize: "0.85rem" } }, "Wave " + w.wave),
                      h("div", { style: { display: "flex", gap: "0.25rem" } },
                        Object.entries(w.severities).map(function (entry) {
                          var sev = entry[0], count = entry[1];
                          var color = sev === "critical" || sev === "high" ? "#ef4444"
                                   : sev === "medium" ? "#f59e0b" : "#6b7280";
                          return h(Badge, {
                            key: sev,
                            variant: "secondary",
                            style: { color: color, border: "1px solid " + color },
                          }, sev + ": " + count);
                        })
                      )
                    ),
                    h("div", { style: { fontSize: "0.75rem", color: "#6b7280" } },
                      w.results.length + " findings"),
                    // Show first few findings
                    w.results.slice(0, 5).map(function (r, i) {
                      return h("div", {
                        key: i,
                        style: { fontSize: "0.75rem", fontFamily: "monospace", marginTop: "0.25rem", color: "#374151" },
                      },
                        h("span", { style: { color: "#6b7280" } }, "[" + (r.severity || "?").toUpperCase() + "] "),
                        h("span", { style: { color: "#6b7280" } }, r.file),
                        r.line ? h("span", { style: { color: "#9ca3af" } }, ":" + r.line) : null,
                        " ",
                        r.title,
                      );
                    }),
                    w.results.length > 5
                      ? h("div", { style: { fontSize: "0.75rem", color: "#6b7280", marginTop: "0.25rem" } },
                          "…" + (w.results.length - 5) + " more")
                      : null,
                  );
                })
              )
            )
          )
        : null,
      // Fix-log source
      d.fix_log_source
        ? h(Card, null,
            h(CardContent, null,
              h("div", { style: { fontSize: "0.8rem", color: "#6b7280" } },
                "fix-log.json source: " + d.fix_log_source),
            )
          )
        : null,
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("swarm-hetzner", SwarmHetznerPage);
  }
})();
