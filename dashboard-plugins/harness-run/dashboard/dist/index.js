/**
 * Hermes Dashboard Plugin — Harness Run
 *
 * Live view of the 4-stage dev harness: lists all runs in ~/harness/artifacts/,
 * shows the active/most-recent run's state, checkpoint log, burn log tail,
 * and ledger summary. Read-only — all mutations go through harness-control skills.
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
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { cn, timeAgo } = SDK.utils;

  var API = "/api/plugins/harness-run";

  function fmtCost(n) {
    if (typeof n !== "number") return "$0.00";
    return "$" + n.toFixed(2);
  }

  function HarnessRunPage() {
    var s = useState(null);
    var data = s[0], setData = s[1];
    var eSt = useState(null);
    var error = eSt[0], setError = eSt[1];
    var l = useState(true);
    var loading = l[0], setLoading = l[1];
    var sel = useState(null);
    var selectedRun = sel[0], setSelectedRun = sel[1];
    var tick = useState(0);
    var tickVal = tick[0], bumpTick = tick[1];

    // Auto-refresh every 5s
    useEffect(function () {
      function load() {
        setLoading(true);
        SDK.fetchJSON(API + "/runs")
          .then(function (res) {
            setData(res);
            setError(null);
            // Auto-select the newest run if none selected
            if (!selectedRun && res.runs && res.runs.length > 0) {
              setSelectedRun(res.runs[0].run_id);
            }
          })
          .catch(function (e) {
            setError(String(e.message || e));
          })
          .finally(function () { setLoading(false); });
      }
      load();
      var iv = setInterval(load, 5000);
      return function () { clearInterval(iv); };
    }, []);

    // Load selected run detail
    var d = useState(null);
    var detail = d[0], setDetail = d[1];
    useEffect(function () {
      if (!selectedRun) { setDetail(null); return; }
      SDK.fetchJSON(API + "/runs/" + encodeURIComponent(selectedRun))
        .then(function (res) { setDetail(res); })
        .catch(function (e) { setDetail(null); setError(String(e.message || e)); });
    }, [selectedRun, tickVal]);

    return h("div", { className: "harness-run-page", style: { padding: "1.5rem", maxWidth: "1100px" } },
      // Header
      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "1rem" } },
        h("h2", { style: { fontSize: "1.25rem", fontWeight: 600, margin: 0 } }, "Harness Run"),
        h("div", { style: { display: "flex", gap: "0.5rem" } },
          h(Badge, { variant: "secondary" },
            (data && data.total) ? data.total + " runs" : "—"),
          h(Button, {
            size: "sm", variant: "outline",
            onClick: function () { bumpTick(tickVal + 1); },
          }, "Refresh"),
        ),
      ),
      error ? h(Card, { style: { borderColor: "#ef4444", marginBottom: "1rem" } },
        h(CardContent, { style: { paddingTop: "0.75rem" } },
          h("pre", { style: { color: "#ef4444", fontSize: "0.85rem", whiteSpace: "pre-wrap" } }, error)
        )
      ) : null,
      // Run selector
      data && data.runs && data.runs.length > 0
        ? h("div", { style: { marginBottom: "1rem", display: "flex", gap: "0.5rem", flexWrap: "wrap" } },
            data.runs.map(function (r) {
              var isSel = r.run_id === selectedRun;
              return h(Button, {
                key: r.run_id,
                size: "sm",
                variant: isSel ? "default" : "outline",
                onClick: function () { setSelectedRun(r.run_id); },
                title: "modified " + timeAgo(r.mtime * 1000),
              }, r.run_id);
            })
          )
        : !loading ? h(Card, null,
            h(CardContent, { style: { paddingTop: "0.75rem" } },
              h("p", { style: { color: "#6b7280" } }, "No harness runs found. Artifacts directory is empty or doesn't exist.")
            )
          ) : null,
      // Detail view
      detail ? h(RunDetail, { detail: detail }) : null,
    );
  }

  function RunDetail(props) {
    var d = props.detail;
    var state = d.state || {};
    var cpFlags = d.checkpoint_flags || {};
    var ledger = d.ledger_summary || {};

    return h("div", { style: { display: "grid", gap: "1rem" } },
      // State card
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Run State — " + d.run_id)),
        h(CardContent, null,
          h("div", { style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "0.5rem 1rem", fontSize: "0.875rem" } },
            h("span", { style: { color: "#6b7280" } }, "Session:"),
            h("span", { style: { fontFamily: "monospace" } }, state.sid || "—"),
            h("span", { style: { color: "#6b7280" } }, "Segment:"),
            h("span", null, String(state.segment || "—")),
            h("span", { style: { color: "#6b7280" } }, "Last checkpoint:"),
            h("span", null, state.last_checkpoint || "—"),
          )
        )
      ),
      // Checkpoint flags
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Checkpoint Log")),
        h(CardContent, null,
          h("div", { style: { display: "grid", gap: "0.5rem" } },
            h("div", null,
              h(Badge, { variant: "secondary" }, cpFlags.n_entries + " entries"),
            ),
            cpFlags.fallbacks && cpFlags.fallbacks.length > 0
              ? h("div", { style: { color: "#f59e0b", fontSize: "0.85rem" } },
                  "⚠️ FALLBACK (no council): " + cpFlags.fallbacks.join(", "))
              : null,
            cpFlags.blindspots && cpFlags.blindspots.length > 0
              ? h("div", { style: { color: "#f59e0b", fontSize: "0.85rem" } },
                  "⚠️ blindspots: " + cpFlags.blindspots.slice(0, 3).join("; "))
              : null,
            cpFlags.coverage_failures && cpFlags.coverage_failures.length > 0
              ? h("div", { style: { color: "#ef4444", fontSize: "0.85rem" } },
                  "⚠️ COVERAGE FAILURES: " + JSON.stringify(cpFlags.coverage_failures))
              : null,
          )
        )
      ),
      // Ledger summary
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Cost Ledger")),
        h(CardContent, null,
          h("div", { style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "0.5rem 1rem", fontSize: "0.875rem" } },
            h("span", { style: { color: "#6b7280" } }, "Total:"),
            h("span", { style: { fontWeight: 600 } }, fmtCost(ledger.total_cost_usd)),
            h("span", { style: { color: "#6b7280" } }, "Entries:"),
            h("span", null, String(ledger.n_entries || 0)),
          ),
          ledger.by_role && Object.keys(ledger.by_role).length > 0
            ? h(Separator, { style: { margin: "0.75rem 0" } })
            : null,
          ledger.by_role && Object.keys(ledger.by_role).length > 0
            ? h("div", { style: { fontSize: "0.8rem" } },
                Object.entries(ledger.by_role).map(function (entry) {
                  return h("div", { key: entry[0], style: { display: "flex", justifyContent: "space-between" } },
                    h("span", { style: { color: "#6b7280" } }, entry[0]),
                    h("span", null, fmtCost(entry[1]))
                  );
                })
              )
            : null
        )
      ),
      // Burn log tail
      d.burn_tail
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Burn Log (tail)")),
            h(CardContent, null,
              h("pre", {
                style: {
                  fontSize: "0.75rem", fontFamily: "monospace",
                  whiteSpace: "pre-wrap", maxHeight: "300px",
                  overflow: "auto", background: "#1e1e2e",
                  color: "#cdd6f4", padding: "0.75rem",
                  borderRadius: "0.5rem",
                },
              }, d.burn_tail)
            )
          )
        : null,
      // Transport log tail
      d.transport_tail
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Transport Log (tail)")),
            h(CardContent, null,
              h("pre", {
                style: {
                  fontSize: "0.75rem", fontFamily: "monospace",
                  whiteSpace: "pre-wrap", maxHeight: "200px",
                  overflow: "auto", background: "#1e1e2e",
                  color: "#cdd6f4", padding: "0.75rem",
                  borderRadius: "0.5rem",
                },
              }, d.transport_tail)
            )
          )
        : null,
      // Files
      d.files && d.files.length > 0
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Artifact Files")),
            h(CardContent, null,
              h("div", { style: { display: "grid", gap: "0.25rem", fontSize: "0.8rem" } },
                d.files.map(function (f) {
                  return h("div", {
                    key: f.name,
                    style: { display: "flex", justifyContent: "space-between" },
                  },
                    h("span", { style: { fontFamily: "monospace" } }, f.name),
                    h("span", { style: { color: "#6b7280" } }, f.size + " B")
                  );
                })
              )
            )
          )
        : null,
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("harness-run", HarnessRunPage);
  }
})();
