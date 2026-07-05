/**
 * Hermes Dashboard Plugin — Listings
 *
 * Antiques listing pipeline queue view: listings grouped by status with
 * counts, listing detail (fields + signed photo URLs), and write operations
 * (price, approve, reject, publish).
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
  var _ = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var timeAgo = SDK.utils.timeAgo;
  var Button = SDK.components.Button;
  var Badge = SDK.components.Badge;
  var Card = SDK.components.Card;
  var CardHeader = SDK.components.CardHeader;
  var CardTitle = SDK.components.CardTitle;
  var CardContent = SDK.components.CardContent;
  var Separator = SDK.components.Separator;
  var Input = SDK.components.Input;
  var Label = SDK.components.Label;
  var Checkbox = SDK.components.Checkbox;
  var Textarea = SDK.components.Textarea;

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

  // Action form modals
  function PriceForm(props) {
    var listing = props.listing;
    var onClose = props.onClose;
    var onSuccess = props.onSuccess;

    var compsState = useState("[{\"price\": 100}, {\"price\": 150}]");
    var compsStr = compsState[0], setCompsStr = compsState[1];
    var posting = useState(false);
    var isPosting = posting[0], setPosting = posting[1];
    var error = useState(null);
    var errMsg = error[0], setError = error[1];

    function handleSubmit(e) {
      e.preventDefault();
      setPosting(true);
      setError(null);
      var comps;
      try {
        comps = JSON.parse(compsStr);
      } catch (ex) {
        setError("Invalid JSON: " + ex.message);
        setPosting(false);
        return;
      }
      SDK.fetchJSON(API + "/listings/" + encodeURIComponent(listing.id) + "/price", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(comps),
      })
        .then(function (res) {
          onSuccess(res.listing);
          onClose();
        })
        .catch(function (e) {
          var msg = e.detail || e.message || String(e);
          setError(msg);
        })
        .finally(function () { setPosting(false); });
    }

    return h("div", { style: {
      position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.5)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
    } },
      h(Card, { style: { width: "400px", maxHeight: "80vh", overflow: "auto" } },
        h(CardHeader, null,
          h(CardTitle, null, "Price Listing: " + (listing.title || listing.id))
        ),
        h(CardContent, null,
          h("form", { onSubmit: handleSubmit },
            h("div", { style: { marginBottom: "1rem" } },
              h(Label, { htmlFor: "comps" }, "Comparables (JSON array of {price} objects)"),
              h(Textarea, {
                id: "comps",
                value: compsStr,
                onChange: function (e) { setCompsStr(e.target.value); },
                rows: 6,
                placeholder: '[{"price": 100}, {"price": 150}]',
              })
            ),
            errMsg ? h("p", { style: { color: "#ef4444", fontSize: "0.85rem", marginBottom: "0.5rem" } }, errMsg) : null,
            h("div", { style: { display: "flex", gap: "0.5rem", justifyContent: "flex-end" } },
              h(Button, { variant: "ghost", type: "button", onClick: onClose, disabled: isPosting }, "Cancel"),
              h(Button, { type: "submit", disabled: isPosting }, isPosting ? "Pricing..." : "Price"),
            ),
          )
        )
      )
    );
  }

  function ApproveForm(props) {
    var listing = props.listing;
    var onClose = props.onClose;
    var onSuccess = props.onSuccess;

    // Extract confidence from the listing's appraisal
    var appraisal = listing.appraisal || {};
    var confidence = appraisal.confidence || {};
    var idConf = confidence.id || confidence.identification || "unknown";
    var valConf = confidence.value || confidence.valuation || "unknown";
    var isHighConfidence = idConf === "high" && valConf === "high";

    var weightState = useState("5.0");
    var weight = weightState[0], setWeight = weightState[1];
    var dimsState = useState("");
    var dims = dimsState[0], setDims = dimsState[1];
    var priceOverrideState = useState("");
    var priceOverride = priceOverrideState[0], setPriceOverride = priceOverrideState[1];
    // For non-high-confidence: requires typed reason (not just checkbox)
    var reasonState = useState("");
    var reason = reasonState[0], setReason = reasonState[1];
    var posting = useState(false);
    var isPosting = posting[0], setPosting = posting[1];
    var error = useState(null);
    var errMsg = error[0], setError = error[1];

    // Computed: can approve if high-confidence OR (reason typed for non-high)
    var canApprove = isHighConfidence || (reason.trim().length > 0);

    function handleSubmit(e) {
      e.preventDefault();
      if (!canApprove) {
        setError("Please provide a typed reason for approving this low-confidence appraisal.");
        return;
      }
      setPosting(true);
      setError(null);
      var weightOz = parseFloat(weight);
      if (isNaN(weightOz)) {
        setError("Invalid weight");
        setPosting(false);
        return;
      }
      var dimsObj = null;
      if (dims.trim()) {
        try { dimsObj = JSON.parse(dims); } catch (ex) {
          setError("Invalid dims JSON: " + ex.message);
          setPosting(false);
          return;
        }
      }
      var priceOverrideVal = null;
      if (priceOverride.trim()) {
        priceOverrideVal = parseFloat(priceOverride);
        if (isNaN(priceOverrideVal)) {
          setError("Invalid price override");
          setPosting(false);
          return;
        }
      }
      SDK.fetchJSON(API + "/listings/" + encodeURIComponent(listing.id) + "/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          weight_oz: weightOz,
          dims: dimsObj,
          price_override: priceOverrideVal,
          acknowledge_low_confidence: !isHighConfidence,
          approval_reason: isHighConfidence ? null : reason.trim(),
        }),
      })
        .then(function (res) {
          onSuccess(res.listing);
          onClose();
        })
        .catch(function (e) {
          var detail = e.detail || {};
          var msg = typeof detail === "string" ? detail : (detail.message || JSON.stringify(detail));
          if (detail.error === "low_confidence") {
            msg = "Confidence is " + detail.confidence.identification + "/" + detail.confidence.valuation + ". Please provide a typed reason to proceed.";
          }
          if (detail.error === "approval_reason") {
            msg = "A typed reason is required for this low-confidence appraisal.";
          }
          setError(msg);
        })
        .finally(function () { setPosting(false); });
    }

    return h("div", { style: {
      position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.5)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
    } },
      h(Card, { style: { width: "450px", maxHeight: "80vh", overflow: "auto" } },
        h(CardHeader, null,
          h(CardTitle, null, "Approve Listing: " + (listing.title || listing.id))
        ),
        h(CardContent, null,
          h("form", { onSubmit: handleSubmit },
            // Confidence display block - always visible
            h("div", { style: { 
              marginBottom: "1rem", 
              padding: "0.75rem", 
              backgroundColor: isHighConfidence ? "#ecfdf5" : "#fffbeb",
              borderRadius: "0.375rem",
              border: "1px solid " + (isHighConfidence ? "#d1fae5" : "#fcd34d")
            } },
              h("div", { style: { fontSize: "0.8rem", color: "#6b7280", marginBottom: "0.25rem" } }, "Appraisal Confidence"),
              h("div", { style: { fontWeight: 600, color: isHighConfidence ? "#065f46" : "#92400e" } },
                "ID: " + idConf + " · Valuation: " + valConf
              ),
              !isHighConfidence ? h("div", { style: { fontSize: "0.75rem", color: "#b45309", marginTop: "0.5rem" } }, 
                "⚠️ Non-high confidence. You must type a reason below to approve."
              ) : null
            ),
            h("div", { style: { marginBottom: "1rem" } },
              h(Label, { htmlFor: "weight" }, "Weight (oz) *"),
              h(Input, { id: "weight", type: "text", value: weight, onChange: function (e) { setWeight(e.target.value); }, placeholder: "5.0" })
            ),
            h("div", { style: { marginBottom: "1rem" } },
              h(Label, { htmlFor: "dims" }, "Dimensions (JSON, optional)"),
              h(Textarea, { id: "dims", value: dims, onChange: function (e) { setDims(e.target.value); }, rows: 2, placeholder: '{"l":6,"w":4,"h":2}' })
            ),
            h("div", { style: { marginBottom: "1rem" } },
              h(Label, { htmlFor: "priceOverride" }, "Price Override (optional)"),
              h(Input, { id: "priceOverride", type: "text", value: priceOverride, onChange: function (e) { setPriceOverride(e.target.value); }, placeholder: "199.99" })
            ),
            // Inline expansion for non-high-confidence: typed reason required
            !isHighConfidence ? h("div", { style: { marginBottom: "1rem" } },
              h(Label, { htmlFor: "reason" }, "Reason for approving this appraisal *"),
              h(Textarea, { 
                id: "reason", 
                value: reason, 
                onChange: function (e) { setReason(e.target.value); }, 
                rows: 3, 
                placeholder: "Brief reason (e.g., 'Comps confirm value', 'Familiar with this category')",
                style: { borderColor: reason.trim() ? "#22c55e" : "#d1d5db" }
              }),
              h("p", { style: { fontSize: "0.75rem", color: "#6b7280", marginTop: "0.25rem" } }, 
                "This typed reason will be stored as an audit artifact."
              )
            ) : null,
            errMsg ? h("p", { style: { color: "#ef4444", fontSize: "0.85rem", marginBottom: "0.5rem" } }, errMsg) : null,
            h("div", { style: { display: "flex", gap: "0.5rem", justifyContent: "flex-end" } },
              h(Button, { variant: "ghost", type: "button", onClick: onClose, disabled: isPosting }, "Cancel"),
              h(Button, { type: "submit", disabled: isPosting || !canApprove }, isPosting ? "Approving..." : "Approve"),
            ),
          )
        )
      )
    );
  }

  function RejectForm(props) {
    var listing = props.listing;
    var onClose = props.onClose;
    var onSuccess = props.onSuccess;

    var reasonState = useState("");
    var reason = reasonState[0], setReason = reasonState[1];
    var posting = useState(false);
    var isPosting = posting[0], setPosting = posting[1];
    var error = useState(null);
    var errMsg = error[0], setError = error[1];

    function handleSubmit(e) {
      e.preventDefault();
      if (!reason.trim()) {
        setError("Reason is required");
        return;
      }
      setPosting(true);
      setError(null);
      SDK.fetchJSON(API + "/listings/" + encodeURIComponent(listing.id) + "/reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: reason }),
      })
        .then(function (res) {
          onSuccess(res.listing);
          onClose();
        })
        .catch(function (e) {
          var msg = e.detail || e.message || String(e);
          setError(msg);
        })
        .finally(function () { setPosting(false); });
    }

    return h("div", { style: {
      position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.5)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
    } },
      h(Card, { style: { width: "400px", maxHeight: "80vh", overflow: "auto" } },
        h(CardHeader, null,
          h(CardTitle, null, "Reject Listing: " + (listing.title || listing.id))
        ),
        h(CardContent, null,
          h("form", { onSubmit: handleSubmit },
            h("div", { style: { marginBottom: "1rem" } },
              h(Label, { htmlFor: "reason" }, "Reason *"),
              h(Textarea, { id: "reason", value: reason, onChange: function (e) { setReason(e.target.value); }, rows: 3, placeholder: "Why is this listing being rejected?" })
            ),
            errMsg ? h("p", { style: { color: "#ef4444", fontSize: "0.85rem", marginBottom: "0.5rem" } }, errMsg) : null,
            h("div", { style: { display: "flex", gap: "0.5rem", justifyContent: "flex-end" } },
              h(Button, { variant: "ghost", type: "button", onClick: onClose, disabled: isPosting }, "Cancel"),
              h(Button, { variant: "destructive", type: "submit", disabled: isPosting }, isPosting ? "Rejecting..." : "Reject"),
            ),
          )
        )
      )
    );
  }

  function PublishForm(props) {
    var listing = props.listing;
    var onClose = props.onClose;
    var onSuccess = props.onSuccess;

    var applyState = useState(false);
    var apply = applyState[0], setApply = applyState[1];
    var posting = useState(false);
    var isPosting = posting[0], setPosting = posting[1];
    var result = useState(null);
    var resultData = result[0], setResult = result[1];
    var error = useState(null);
    var errMsg = error[0], setError = error[1];

    function handleSubmit(e) {
      e.preventDefault();
      setPosting(true);
      setError(null);
      SDK.fetchJSON(API + "/listings/" + encodeURIComponent(listing.id) + "/publish?apply=" + apply, {
        method: "POST",
      })
        .then(function (res) {
          setResult(res);
          if (!apply || res.dry_run) {
            onSuccess(res.listing);
          }
          if (!apply) {
            // Dry run — show what would be sent
          } else if (!res.dry_run) {
            onSuccess(res.listing);
            onClose();
          }
        })
        .catch(function (e) {
          var msg = e.detail || e.message || String(e);
          setError(msg);
        })
        .finally(function () { setPosting(false); });
    }

    return h("div", { style: {
      position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.5)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50,
    } },
      h(Card, { style: { width: "450px", maxHeight: "80vh", overflow: "auto" } },
        h(CardHeader, null,
          h(CardTitle, null, "Publish Listing: " + (listing.title || listing.id))
        ),
        h(CardContent, null,
          h("form", { onSubmit: handleSubmit },
            h("div", { style: { marginBottom: "1rem", display: "flex", alignItems: "center", gap: "0.5rem" } },
              h(Checkbox, { id: "apply", checked: apply, onCheckedChange: function (checked) { setApply(!!checked); }, disabled: isPosting }),
              h(Label, { htmlFor: "apply", style: { cursor: "pointer" } }, "Actually publish (uncheck for dry-run)"),
            ),
            resultData ? h("div", { style: { marginBottom: "1rem", padding: "0.5rem", backgroundColor: "#f0fdf4", borderRadius: "0.375rem", fontSize: "0.8rem" } },
              h("p", { style: { fontWeight: 600, marginBottom: "0.25rem" } }, resultData.dry_run ? "Dry-run preview:" : "Published!"),
              resultData.provider_result ? h("pre", { style: { whiteSpace: "pre-wrap", fontSize: "0.75rem" } }, JSON.stringify(resultData.provider_result, null, 2)) : null
            ) : null,
            errMsg ? h("p", { style: { color: "#ef4444", fontSize: "0.85rem", marginBottom: "0.5rem" } }, errMsg) : null,
            h("div", { style: { display: "flex", gap: "0.5rem", justifyContent: "flex-end" } },
              h(Button, { variant: "ghost", type: "button", onClick: onClose, disabled: isPosting }, "Cancel"),
              h(Button, { type: "submit", disabled: isPosting }, isPosting ? (apply ? "Publishing..." : "Running...") : (apply ? "Publish Now" : "Run Dry-Run")),
            ),
          )
        )
      )
    );
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

    // Modal states
    var showPrice = useState(false);
    var priceFormVisible = showPrice[0], setPriceFormVisible = showPrice[1];
    var showApprove = useState(false);
    var approveFormVisible = showApprove[0], setApproveFormVisible = showApprove[1];
    var showReject = useState(false);
    var rejectFormVisible = showReject[0], setRejectFormVisible = showReject[1];
    var showPublish = useState(false);
    var publishFormVisible = showPublish[0], setPublishFormVisible = showPublish[1];

    function refresh() {
      bumpTick(tickVal + 1);
    }

    function loadQueue() {
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

    useEffect(function () {
      loadQueue();
      var iv = setInterval(loadQueue, 10000);
      return function () { clearInterval(iv); };
    }, []);

    useEffect(function () {
      if (!selectedId) { setDetail(null); return; }
      SDK.fetchJSON(API + "/listings/" + encodeURIComponent(selectedId))
        .then(function (res) { setDetail(res); })
        .catch(function (e) { setDetail(null); setError(String(e.message || e)); });
    }, [selectedId, tickVal]);

    function handleActionSuccess(updatedListing) {
      // Refresh queue after action
      loadQueue();
      if (selectedId === updatedListing.id) {
        SDK.fetchJSON(API + "/listings/" + encodeURIComponent(updatedListing.id))
          .then(function (res) { setDetail(res); });
      }
    }

    // Determine which actions are available for the selected listing
    var actions = [];
    if (detail) {
      var status = detail.status;
      if (status === "draft") {
        actions.push({ label: "Price", onClick: function () { setPriceFormVisible(true); } });
      }
      if (status === "priced" || status === "draft") {
        actions.push({ label: "Approve", onClick: function () { setApproveFormVisible(true); } });
        actions.push({ label: "Reject", onClick: function () { setRejectFormVisible(true); } });
      }
      if (status === "approved") {
        actions.push({ label: "Publish", onClick: function () { setPublishFormVisible(true); } });
      }
    }

    return h("div", { style: { padding: "1.5rem", maxWidth: "1200px" } },
      // Header
      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "1rem" } },
        h("h2", { style: { fontSize: "1.25rem", fontWeight: 600, margin: 0 } }, "Listings"),
        h("div", { style: { display: "flex", gap: "0.5rem" } },
          queue ? h(Badge, { variant: "secondary" }, queue.total + " total") : null,
          h(Button, { size: "sm", variant: "outline", onClick: refresh }, "Refresh"),
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
          detail ? h(ListingDetail, { detail: detail, actions: actions, onClose: function () { setSelectedId(null); } })
                 : h(Card, null, h(CardContent, { style: { paddingTop: "0.75rem" } },
                     h("p", { style: { color: "#6b7280" } }, "Select a listing to see details.")
                   )),
        ),
      ),

      // Modals
      priceFormVisible && detail ? h(PriceForm, { listing: detail, onClose: function () { setPriceFormVisible(false); }, onSuccess: handleActionSuccess }) : null,
      approveFormVisible && detail ? h(ApproveForm, { listing: detail, onClose: function () { setApproveFormVisible(false); }, onSuccess: handleActionSuccess }) : null,
      rejectFormVisible && detail ? h(RejectForm, { listing: detail, onClose: function () { setRejectFormVisible(false); }, onSuccess: handleActionSuccess }) : null,
      publishFormVisible && detail ? h(PublishForm, { listing: detail, onClose: function () { setPublishFormVisible(false); }, onSuccess: handleActionSuccess }) : null,
    );
  }

  function ListingDetail(props) {
    var d = props.detail;
    var actions = props.actions || [];
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
        // Action buttons
        actions.length > 0 ? h("div", { style: { display: "flex", gap: "0.5rem", marginBottom: "1rem" } },
          actions.map(function (action, i) {
            return h(Button, { key: i, size: "sm", variant: "default", onClick: action.onClick }, action.label);
          })
        ) : null,
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
