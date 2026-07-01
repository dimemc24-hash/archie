/**
 * Hermes Dashboard Plugin — Router Ledger
 *
 * Cost ledger for the harness model router: spend by role/model vs the $25/day
 * OpenRouter cap, circuit-breaker state, and per-entry cost log.
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

  var API = "/api/plugins/router-ledger";

  function fmtCost(n) {
    if (typeof n !== "number") return "$0.00";
    return "$" + n.toFixed(2);
  }

  function fmtPct(n) {
    if (typeof n !== "number") return "0%";
    return n.toFixed(1) + "%";
  }

  var SEV_COLOR = {
    critical: "#ef4444",
    high: "#f59e0b",
    medium: "#6b7280",
    low: "#22c55e",
  };

  function RouterLedgerPage() {
    var runsSt = useState(null);
    var runs = runsSt[0], setRuns = runsSt[1];
    var selSt = useState(null);
    var selectedRun = selSt[0], setSelectedRun = selSt[1];
    var detailSt = useState(null);
    var detail = detailSt[0], setDetail = detailSt[1];
    var rowsSt = useState(null);
    var rowsData = rowsSt[0], setRowsData = rowsSt[1];
    var errSt = useState(null);
    var error = errSt[0], setError = errSt[1];

    useEffect(function () {
      SDK.fetchJSON(API + "/runs")
        .then(function (res) {
          setRuns(res);
          if (!selectedRun && res.runs && res.runs.length > 0) {
            setSelectedRun(res.runs[0].run_id);
          }
        })
        .catch(function (e) { setError(String(e.message || e)); });
    }, []);

    useEffect(function () {
      if (!selectedRun) { setDetail(null); setRowsData(null); return; }
      SDK.fetchJSON(API + "/runs/" + encodeURIComponent(selectedRun))
        .then(function (res) { setDetail(res); setError(null); })
        .catch(function (e) { setDetail(null); setError(String(e.message || e)); });
      SDK.fetchJSON(API + "/runs/" + encodeURIComponent(selectedRun) + "/rows?limit=50")
        .then(function (res) { setRowsData(res); })
        .catch(function (e) { setRowsData(null); });
    }, [selectedRun]);

    return h("div", { style: { padding: "1.5rem", maxWidth: "1100px" } },
      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "1rem" } },
        h("h2", { style: { fontSize: "1.25rem", fontWeight: 600, margin: 0 } }, "Router Ledger"),
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
        : !runs ? h("p", { style: { color: "#6b7280" } }, "Loading…")
        : h(Card, null,
            h(CardContent, { style: { paddingTop: "0.75rem" } },
              h("p", { style: { color: "#6b7280" } }, "No runs with a ledger.json found.")
            )
          ),
      error ? h(Card, { style: { borderColor: "#ef4444", marginBottom: "1rem" } },
        h(CardContent, { style: { paddingTop: "0.75rem" } },
          h("pre", { style: { color: "#ef4444", fontSize: "0.85rem", whiteSpace: "pre-wrap" } }, error)
        )
      ) : null,
      detail ? h(LedgerDetail, { detail: detail, rowsData: rowsData }) : null,
    );
  }

  function LedgerDetail(props) {
    var agg = props.detail.aggregated;
    var rowsData = props.rowsData;
    var cap = agg.daily_cap_usd;
    var pctOfCap = cap ? (agg.total_cost_usd / cap) * 100 : 0;

    return h("div", { style: { display: "grid", gap: "1rem" } },
      // Summary card
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Cost Summary — " + props.detail.run_id)),
        h(CardContent, null,
          h("div", { style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "0.5rem 1rem", fontSize: "0.875rem" } },
            h("span", { style: { color: "#6b7280" } }, "Total spend:"),
            h("span", { style: { fontWeight: 600, fontSize: "1.1rem" } }, fmtCost(agg.total_cost_usd)),
            h("span", { style: { color: "#6b7280" } }, "Daily cap:"),
            h("span", null, fmtCost(cap)),
            h("span", { style: { color: "#6b7280" } }, "% of daily cap:"),
            h("span", {
              style: {
                fontWeight: 600,
                color: pctOfCap > 80 ? "#ef4444" : pctOfCap > 50 ? "#f59e0b" : "#22c55e",
              },
            }, fmtPct(pctOfCap)),
            h("span", { style: { color: "#6b7280" } }, "Entries:"),
            h("span", null, String(agg.n_entries)),
            h("span", { style: { color: "#6b7280" } }, "Tokens in/out:"),
            h("span", null, String(agg.tokens_in) + " / " + String(agg.tokens_out)),
          ),
          // Circuit breaker state
          agg.circuit_broken
            ? h("div", {
                style: {
                  marginTop: "0.75rem", padding: "0.5rem 0.75rem",
                  background: "#fef2f2", border: "1px solid #ef4444",
                  borderRadius: "0.375rem", fontSize: "0.85rem", color: "#ef4444",
                },
              }, "⚠️ CIRCUIT BREAKER TRIPPED — daily/global cost cap reached")
            : h("div", {
                style: {
                  marginTop: "0.75rem", padding: "0.5rem 0.75rem",
                  background: "#f0fdf4", border: "1px solid #22c55e",
                  borderRadius: "0.375rem", fontSize: "0.85rem", color: "#16a34a",
                },
              }, "✓ Circuit breaker: OK"),
        )
      ),
      // Daily totals vs cap
      agg.daily_totals && agg.daily_totals.length > 0
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Daily Spend vs Cap")),
            h(CardContent, null,
              h("div", { style: { display: "grid", gap: "0.5rem" } },
                agg.daily_totals.map(function (d) {
                  var barColor = d.pct_of_cap > 80 ? "#ef4444" : d.pct_of_cap > 50 ? "#f59e0b" : "#22c55e";
                  return h("div", { key: d.day, style: { fontSize: "0.85rem" } },
                    h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "0.25rem" } },
                      h("span", { style: { fontFamily: "monospace" } }, d.day),
                      h("span", null, fmtCost(d.total) + " / " + fmtCost(d.cap) + " (" + fmtPct(d.pct_of_cap) + ")"),
                    ),
                    h("div", {
                      style: {
                        height: "6px", background: "#e5e7eb", borderRadius: "3px", overflow: "hidden",
                      },
                    },
                      h("div", {
                        style: {
                          width: Math.min(d.pct_of_cap, 100) + "%",
                          height: "100%", background: barColor,
                        },
                      })
                    ),
                  );
                })
              )
            )
          )
        : null,
      // By role
      agg.by_role && Object.keys(agg.by_role).length > 0
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Spend by Role")),
            h(CardContent, null,
              h("div", { style: { display: "grid", gap: "0.25rem" } },
                Object.entries(agg.by_role).map(function (entry) {
                  var role = entry[0], cost = entry[1];
                  return h("div", {
                    key: role,
                    style: { display: "flex", justifyContent: "space-between", fontSize: "0.875rem" },
                  },
                    h("span", { style: { fontFamily: "monospace" } }, role),
                    h("span", { style: { fontWeight: 600 } }, fmtCost(cost)),
                  );
                })
              )
            )
          )
        : null,
      // By model
      agg.by_model && Object.keys(agg.by_model).length > 0
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Spend by Model")),
            h(CardContent, null,
              h("div", { style: { display: "grid", gap: "0.25rem" } },
                Object.entries(agg.by_model).map(function (entry) {
                  var model = entry[0], cost = entry[1];
                  return h("div", {
                    key: model,
                    style: { display: "flex", justifyContent: "space-between", fontSize: "0.8rem" },
                  },
                    h("span", { style: { fontFamily: "monospace" } }, model),
                    h("span", { style: { fontWeight: 600 } }, fmtCost(cost)),
                  );
                })
              )
            )
          )
        : null,
      // Raw rows (recent)
      rowsData && rowsData.rows && rowsData.rows.length > 0
        ? h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Recent Entries (" + rowsData.total + " total, showing " + rowsData.rows.length + ")")),
            h(CardContent, null,
              h("div", { style: { maxHeight: "300px", overflow: "auto", fontSize: "0.75rem" } },
                h("table", { style: { width: "100%", borderCollapse: "collapse" } },
                  h("thead", null,
                    h("tr", { style: { borderBottom: "1px solid #e5e7eb", textAlign: "left" } },
                      h("th", { style: { padding: "0.25rem" } }, "Role"),
                      h("th", { style: { padding: "0.25rem" } }, "Model"),
                      h("th", { style: { padding: "0.25rem" } }, "Tokens"),
                      h("th", { style: { padding: "0.25rem", textAlign: "right" } }, "Cost"),
                    )
                  ),
                  h("tbody", null,
                    rowsData.rows.map(function (r, i) {
                      return h("tr", {
                        key: i,
                        style: { borderBottom: "1px solid #f3f4f6" },
                      },
                        h("td", { style: { padding: "0.25rem", fontFamily: "monospace" } }, r.role || "—"),
                        h("td", { style: { padding: "0.25rem", fontFamily: "monospace" } }, r.model || "—"),
                        h("td", { style: { padding: "0.25rem", color: "#6b7280" } },
                          (r.tokensIn || 0) + "→" + (r.tokensOut || 0)),
                        h("td", { style: { padding: "0.25rem", textAlign: "right", fontWeight: 600 } },
                          fmtCost(parseFloat(r.costUsd || 0))),
                      );
                    })
                  )
                )
              )
            )
          )
        : null,
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("router-ledger", RouterLedgerPage);
  }
})();
