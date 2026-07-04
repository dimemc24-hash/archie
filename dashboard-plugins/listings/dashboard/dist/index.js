/**
 * Hermes Dashboard Plugin — Listings
 *
 * Antiques listing pipeline queue view: listings grouped by status with
 * counts, listing detail (fields + signed photo URLs). Read-only — all
 * mutations go through the antiques pipeline modules.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React +
 * shadcn primitives + fetchJSON.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  var React = SDK.React;
  var h = React.createElement;
  var {
    Card, CardHeader, CardTitle, CardContent,
    Badge, Button, Separator,
  } = SDK.components;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var timeAgo = SDK.utils.timeAgo;

  var API = "/api/plugins/listings";

  var STATUS_COLORS = {
    draft: "secondary",
    priced: "outline",
    approved: "default",
    listed: "default",
    sold: "default",
    shipped: "secondary",
    rejected: "destructive",
    error: "destructive",
  };

  function fmtPrice(n) {
    if (typeof n !== "number") return "—";
    return "$" + n.toFixed(2);
  }

  function ListingsPage() {
    var qs = useState(null);
    var queue = qs[0], setQueue = qs[1];
    var es = useState(null);
    var error = es[0], setError = es[1];
    var ls = useState(true);
    var loading = ls[0], setLoading = ls[1];
    var sel = useState(null);
    var selectedId = sel[0], setSelectedId = sel[1];
    var ds = useState(null);
    var detail = ds[0], setDetail = ds[1];
    var tick = useState(0);
    var tickVal = tick[0], bumpTick = tick[1];

    useEffect(function () {
      function load() {
        setLoading(true);
        SDK.fetchJSON(API + "/queue")
          .then(function (res) {
            setQueue(res);
            setError(null);
          })
          .catch(function (e) {
            setError(String(e.message || e));
          })
          .finally(function () { setLoading(false); });
      }
      load();
      var iv = setInterval(load, 10000);
      return function () { clearInterval(iv); };
    }, []);

    useEffect(function () {
      if (!selectedId) { setDetail(null); return; }
      SDK.fetchJSON(API + "/listings/" + encodeURIComponent(selectedId))
        .then(function (res) { setDetail(res); })
        .catch(function (e) { setDetail(null); setError(String(e.message || e)); });
    }, [selectedId, tickVal]);

    return h("div", { style: { padding: "1.5rem", maxWidth: "1200px" } },
      // Header
      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "1rem" } },
        h("h2", { style: { fontSize: "1.25rem", fontWeight: 600, margin: 0 } }, "Listings"),
        h("div", { style: { display: "flex", gap: "0.5rem" } },
          queue ? h(Badge, { variant: "secondary" }, queue.total + " total") : null,
          h(Button, { size: "sm", variant: "outline", onClick: function () { bumpTick(tickVal + 1); } }, "Refresh"),
        ),
      ),
      error ? h(Card, { style: { borderColor: "#ef4444", marginBottom: "1rem" } },
        h(CardContent, { style: { paddingTop: "0.75rem" } },
          h("pre", { style: { color: "#ef4444", fontSize: "0.85rem", whiteSpace: "pre-wrap" } }, error)
        )
      ) : null,
      h("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" } },
        // Queue (left)
        h("div", null,
          !loading && (!queue || !queue.groups || queue.groups.length === 0)
            ? h(Card, null, h(CardContent, { style: { paddingTop: "0.75rem" } },
                h("p", { style: { color: "#6b7280" } }, "No listings found.")
              ))
            : null,
          queue && queue.groups
            ? queue.groups.map(function (grp) {
                return h(Card, { key: grp.status, style: { marginBottom: "0.75rem" } },
                  h(CardHeader, null,
                    h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between" } },
                      h(CardTitle, { style: { fontSize: "1rem" } },
                        h(Badge, { variant: STATUS_COLORS[grp.status] || "secondary" }, grp.status),
                        " ",
                        h("span", { style: { color: "#6b7280", fontSize: "0.85rem" } }, grp.count + " listing" + (grp.count !== 1 ? "s" : "")),
                      ),
                    ),
                  ),
                  h(CardContent, null,
                    h("div", { style: { display: "grid", gap: "0.25rem" } },
                      grp.listings.map(function (item) {
                        var isSel = item.id === selectedId;
                        return h("div", {
                          key: item.id,
                          onClick: function () { setSelectedId(item.id); },
                          style: {
                            cursor: "pointer", padding: "0.4rem 0.5rem", borderRadius: "0.375rem",
                            background: isSel ? "#f0f9ff" : "transparent",
                            display: "flex", justifyContent: "space-between", alignItems: "center",
                            fontSize: "0.85rem",
                          },
                        },
                          h("span", { style: { fontWeight: isSel ? 600 : 400 } }, item.title || item.id,
                            (function (c) {
                              if (!c) return null;
                              var parts = [];
                              if (c.id !== "high") parts.push("id:" + c.id);
                              if (c.value !== "high") parts.push("val:" + c.value);
                              if (!parts.length) return null;
                              return h("span", { style: { marginLeft: "0.4rem", fontSize: "0.7rem", fontWeight: 400, color: "#b45309", border: "1px solid #f59e0b", borderRadius: "0.375rem", padding: "0 0.3rem" } }, parts.join(" "));
                            })(item.confidence)),
                          h("span", { style: { color: "#6b7280" } },
                            fmtPrice(item.price), " · ", item.n_photos, " 📷",
                          ),
                        );
                      })
                    )
                  )
                );
              })
            : null,
        ),
        // Detail (right)
        h("div", null,
          detail ? h(ListingDetail, { detail: detail, onClose: function () { setSelectedId(null); } })
                 : h(Card, null, h(CardContent, { style: { paddingTop: "0.75rem" } },
                     h("p", { style: { color: "#6b7280" } }, "Select a listing to see details.")
                   )),
        ),
      ),
    );
  }

  function ListingDetail(props) {
    var d = props.detail;
    var pricing = d.pricing || {};
    var approval = d.approval || {};
    var provider = d.provider || {};
    var photos = d.signed_photos || [];

    return h(Card, null,
      h(CardHeader, null,
        h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between" } },
          h(CardTitle, { style: { fontSize: "1rem" } }, d.title || d.id),
          h("div", { style: { display: "flex", gap: "0.5rem" } },
            h(Badge, { variant: STATUS_COLORS[d.status] || "secondary" }, d.status),
            h(Button, { size: "sm", variant: "ghost", onClick: props.onClose }, "×"),
          ),
        ),
      ),
      h(CardContent, null,
        h("div", { style: { display: "grid", gridTemplateColumns: "auto 1fr", gap: "0.4rem 1rem", fontSize: "0.85rem" } },
          h("span", { style: { color: "#6b7280" } }, "ID:"),
          h("span", { style: { fontFamily: "monospace", fontSize: "0.75rem" } }, d.id),
          h("span", { style: { color: "#6b7280" } }, "Category:"),
          h("span", null, d.category_guess || "—"),
          h("span", { style: { color: "#6b7280" } }, "Price:"),
          h("span", { style: { fontWeight: 600 } }, fmtPrice(pricing.recommended)),
          pricing.range ? h("span", { style: { color: "#6b7280" } }, "Range:") : null,
          pricing.range ? h("span", null, fmtPrice(pricing.range.low) + " – " + fmtPrice(pricing.range.high)) : null,
          h("span", { style: { color: "#6b7280" } }, "Source:"),
          h("span", null, d.source || "—"),
          h("span", { style: { color: "#6b7280" } }, "Confidence:"),
          h("span", null, (function (c) {
            c = c || {};
            var idc = c.id || c.identification || "unknown";
            var valc = c.value || c.valuation || "unknown";
            var hot = idc !== "high" || valc !== "high";
            var txt = "id:" + idc + " · val:" + valc;
            if (c.basis) txt += " — " + c.basis;
            if (c.flags && c.flags.length) txt += " [" + c.flags.join(", ") + "]";
            return h("span", { style: hot ? { color: "#b45309", fontWeight: 600 } : {} }, txt);
          })((d.appraisal || {}).confidence)),
          h("span", { style: { color: "#6b7280" } }, "Created:"),
          h("span", null, d.created_at ? timeAgo(new Date(d.created_at).getTime()) : "—"),
        ),
        d.description ? h(Separator, { style: { margin: "0.75rem 0" } }) : null,
        d.description ? h("p", { style: { fontSize: "0.85rem", whiteSpace: "pre-wrap" } }, d.description) : null,
        photos.length > 0 ? h(Separator, { style: { margin: "0.75rem 0" } }) : null,
        photos.length > 0 ? h("div", null,
          h("div", { style: { fontSize: "0.8rem", color: "#6b7280", marginBottom: "0.4rem" } }, "Photos (" + photos.length + ")"),
          h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(100px, 1fr))", gap: "0.5rem" } },
            photos.map(function (p) {
              return h("a", { key: p.index, href: p.url || "#", target: "_blank" },
                h("img", {
                  src: p.url || "",
                  alt: "photo " + p.index,
                  style: { width: "100%", height: "80px", objectFit: "cover", borderRadius: "0.375rem", border: "1px solid #e5e7eb" },
                  onError: function (e) { e.target.style.opacity = 0.3; },
                })
              );
            })
          ),
        ) : null,
        Object.keys(approval).length > 0 ? h(Separator, { style: { margin: "0.75rem 0" } }) : null,
        Object.keys(approval).length > 0 ? h("div", { style: { fontSize: "0.8rem" } },
          h("span", { style: { color: "#6b7280" } }, "Approved by "),
          h("span", { style: { fontWeight: 600 } }, approval.approved_by || "?"),
          h("span", { style: { color: "#6b7280" } }, " · weight: " + (approval.weight_oz || "?") + "oz"),
        ) : null,
        Object.keys(provider).length > 0 ? h(Separator, { style: { margin: "0.75rem 0" } }) : null,
        Object.keys(provider).length > 0 ? h("div", { style: { fontSize: "0.8rem" } },
          h("span", { style: { color: "#6b7280" } }, "Provider: "),
          h("span", { style: { fontFamily: "monospace" } }, provider.kind || "?"),
          provider.listing_id ? h("span", { style: { color: "#6b7280" } }, " · " + provider.listing_id) : null,
        ) : null,
      ),
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("listings", ListingsPage);
  }
})();
