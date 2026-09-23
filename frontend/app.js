const API = "";
const $ = id => document.getElementById(id);
const fmt = (value, digits = 2) => value === null || value === undefined || Number.isNaN(Number(value)) ? "—" : Number(value).toLocaleString("en-IN", {maximumFractionDigits: digits, minimumFractionDigits: digits});
const money = value => value === null || value === undefined ? "—" : Number(value) === 0 ? "₹0.00" : `${value < 0 ? "−" : "+"}₹${fmt(Math.abs(value), 2)}`;
const percent = value => value === null || value === undefined ? "—" : Number(value) === 0 ? "0.00%" : `${value < 0 ? "−" : "+"}${fmt(Math.abs(value), 2)}%`;
const pnlClass = value => value > 0 ? "pos" : value < 0 ? "neg" : "neutral";
const slLabel = state => state === "SL_TRIGGERED" ? "🚨 SL TRIGGERED" : state === "APPROACHING_SL" ? "⚠ APPROACHING SL" : state || "ACTIVE";
const signed = value => value === null || value === undefined ? "—" : `${value < 0 ? "−" : "+"}${fmt(Math.abs(value), 1)}`;
async function api(path, options) { const response = await fetch(API + path, options); const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.detail || "Request failed"); return data; }
let optionChain = [];
let activeTab = "LIVE";
let authState = "AUTHENTICATION_REQUIRED";
let chainRequestId = 0;
let expiryRequestId = 0;
function selectTab(tab) {
  activeTab = tab;
  const isHistory = tab === "HISTORY";
  $("trading-workspace").hidden = isHistory;
  $("history-panel").classList.toggle("active", isHistory);
  document.body.classList.toggle("paper-mode", tab === "PAPER");
  document.querySelectorAll(".primary-tab").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  if (!isHistory) {
    $("mode-select").value = tab;
    $("workspace-eyebrow").textContent = `01 / ${tab} POSITION`;
    $("workspace-heading").textContent = tab === "PAPER" ? "Simulate four legs with live Fyers prices" : "Enter your four legs";
    refresh();
  } else refreshHistory();
}
document.querySelectorAll(".primary-tab").forEach(button => button.addEventListener("click", () => selectTab(button.dataset.tab)));
function setChainMessage(message) { document.querySelectorAll('[data-field="strike"]').forEach(select => { select.innerHTML = `<option value="">${message}</option>`; }); }
function clearLegSelections() { document.querySelectorAll('[data-field="symbol"]').forEach(input => { input.value = ""; }); document.querySelectorAll('[data-field="entry_price"]').forEach(input => { input.value = ""; }); setChainMessage("Select an expiry first"); }
function populateChain(chain) {
	optionChain = chain.quotes.filter(quote => quote.symbol).sort((left, right) => left.strike - right.strike);
	document.querySelectorAll('[data-field="strike"]').forEach(select => {
		const type = select.dataset.optionType;
		const quotes = optionChain.filter(quote => quote.option_type === type);
		select.innerHTML = `<option value="">Select ${type === "CE" ? "call" : "put"} strike / price</option>` + quotes.map(quote => `<option value="${quote.strike}">${fmt(quote.strike, 0)} ${type} | LTP ₹${fmt(quote.ltp)}</option>`).join("");
	});
}
async function loadChain(expiry) {
  const requestId = ++chainRequestId;
	clearLegSelections();
	if (!expiry) return;
	setChainMessage("Loading option chain...");
  try {
    const chain = await api(`/chain?expiry=${encodeURIComponent(expiry)}`);
    if (requestId === chainRequestId && $("expiry").value === expiry) populateChain(chain);
  }
	catch (error) { setChainMessage("Unable to load option chain. Please try again."); $("form-message").textContent = error.message; }
}
async function loadExpiries() {
  const requestId = ++expiryRequestId;
  const selectedExpiry = $("expiry").value;
	try {
		const data = await api("/expiries");
		if (requestId !== expiryRequestId) return;
		const currentExpiry = $("expiry").value || selectedExpiry;
    if (currentExpiry) return;
		$("expiry").innerHTML = '<option value="">Select expiry</option>' + data.expiries.map(expiry => `<option value="${expiry.value}">${expiry.label}</option>`).join("");
  } catch (error) {
    if (requestId !== expiryRequestId || $("expiry").value) return;
    const message = authState === "AUTHENTICATION_REQUIRED" || authState === "AUTHENTICATION_FAILED" ? "FYERS authentication required" : "Unable to load expiries";
    $("expiry").innerHTML = `<option value="">${message}</option>`;
    $("form-message").textContent = error.message;
    setChainMessage("Unable to load option chain. Please try again.");
  }
}
$("expiry").addEventListener("change", event => loadChain(event.target.value));
document.querySelectorAll('[data-field="strike"]').forEach(select => select.addEventListener("change", event => {
	const quote = optionChain.find(item => item.option_type === select.dataset.optionType && String(item.strike) === event.target.value);
	const leg = event.target.dataset.leg;
	if (!quote) return;
	document.querySelector(`[data-leg="${leg}"][data-field="symbol"]`).value = quote.symbol;
	document.querySelector(`[data-leg="${leg}"][data-field="entry_price"]`).value = quote.ltp;
}));
$("connect-fyers").addEventListener("click", async () => { try { const result = await api("/fyers/login-url"); window.location.href = result.url; } catch (error) { $("alerts").innerHTML = `<li>⚠ Could not start FYERS login: ${error.message}</li>`; } });
function collectLegs() { return ["CALL BUY", "CALL SELL", "PUT SELL", "PUT BUY"].map(name => { const values = {leg: name}; document.querySelectorAll(`[data-leg="${name}"]`).forEach(input => { values[input.dataset.field] = ["number", "select-one"].includes(input.type) ? Number(input.value) : input.value.trim(); }); return values; }); }
function previewPayload() { return {expiry: $("expiry").value, mode: $("mode-select").value, slippage_points: Number($("slippage-points").value || 0.05), call_sl: null, put_sl: null, legs: collectLegs()}; }
function previewReady() { return Boolean($("expiry").value) && collectLegs().every(leg => leg.symbol && leg.strike > 0 && leg.quantity > 0 && leg.entry_price > 0); }
function renderSlPreview(data) {
  const set = (id, value, digits = 0, prefix = "₹") => $(id).textContent = value == null ? "—" : `${prefix}${fmt(value, digits)}`;
  const allLegs = data?.legs || collectLegs();
  const byLeg = Object.fromEntries(allLegs.map(leg => [leg.leg, leg]));
  const spot = data?.spot;
  $("sl-preview-status").textContent = !data || data.available === false ? (data?.message || "Enter all four legs") : spot == null ? "Trigger calculated; live spot unavailable" : "Live NIFTY spot";
  $("sl-preview-spot").textContent = spot == null ? "—" : `₹${fmt(spot, 2)}`;
  [["call", "CALL", "CALL SELL", "CALL BUY"], ["put", "PUT", "PUT SELL", "PUT BUY"]].forEach(([side, label, shortName, hedgeName]) => {
    const item = data?.[side] || {};
    const trigger = item.sl_trigger_price;
    const distance = item.distance == null && spot != null && trigger != null ? Math.abs(trigger - spot) : item.distance;
    const unavailable = !data || data.available === false || trigger == null;
    set(`preview-${side}-credit`, item.net_credit, 2);
    set(`preview-${side}-profit`, item.max_profit);
    set(`preview-${side}-loss`, item.sl_loss);
    set(`preview-${side}-trigger`, trigger);
    set(`preview-${side}-distance`, distance, 0, "");
    $(`preview-${side}-distance`).textContent = distance == null ? "—" : `${fmt(Math.abs(distance), 0)} pts`;
    $(`preview-${side}-spot`).textContent = spot == null ? "—" : fmt(spot, 0);
    $(`preview-${side}-gauge-trigger`).textContent = trigger == null ? "—" : fmt(trigger, 0);
    $(`preview-${side}-sell-entry`).textContent = byLeg[shortName]?.entry_price ? `₹${fmt(byLeg[shortName].entry_price, 2)}` : "—";
    $(`preview-${side}-buy-entry`).textContent = byLeg[hedgeName]?.entry_price ? `₹${fmt(byLeg[hedgeName].entry_price, 2)}` : "—";
    const shortStrike = byLeg[shortName]?.strike;
    const hedgeStrike = byLeg[hedgeName]?.strike;
    const low = Math.min(hedgeStrike || trigger || 0, trigger || hedgeStrike || 0);
    const high = Math.max(hedgeStrike || trigger || 0, trigger || hedgeStrike || 0);
    const ratio = spot != null && high > low ? Math.max(0, Math.min(100, ((spot - low) / (high - low)) * 100)) : 0;
    $(`preview-${side}-gauge`).style.width = unavailable ? "0%" : `${ratio}%`;
    let status = unavailable ? "UNAVAILABLE" : data[`${side}_sl_state`] || (distance != null && trigger != null && distance <= Math.abs(trigger - (shortStrike || trigger)) * 0.2 ? "APPROACHING_SL" : "ACTIVE");
    if (data[`${side}_sl_triggered`]) status = "SL_TRIGGERED";
    const statusElement = $(`preview-${side}-status`);
    statusElement.className = `sl-status ${status.toLowerCase()}`;
    statusElement.querySelector("span").textContent = status === "SL_TRIGGERED" ? "SL TRIGGERED" : status === "APPROACHING_SL" ? "APPROACHING SL" : status === "UNAVAILABLE" ? "UNAVAILABLE" : "SAFE";
    statusElement.querySelector("i").textContent = status === "SL_TRIGGERED" ? "●" : status === "APPROACHING_SL" ? "●" : status === "UNAVAILABLE" ? "○" : "●";
    $(`${side}-sl`).textContent = item.sl_loss == null ? "—" : `₹${fmt(item.sl_loss, 0)}`;
  });
}
let previewTimer; async function updateSlPreview() { clearTimeout(previewTimer); previewTimer = setTimeout(async () => { if (!previewReady()) { renderSlPreview({available: false}); return; } try { renderSlPreview(await api("/manual-monitor/preview", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(previewPayload())})); } catch (error) { renderSlPreview({available: false, message: error.message}); } }, 250); }
document.querySelectorAll("#setup-form input, #setup-form select").forEach(input => input.addEventListener("input", updateSlPreview)); document.querySelectorAll("#setup-form input, #setup-form select").forEach(input => input.addEventListener("change", updateSlPreview));
$("setup-form").addEventListener("submit", async event => { event.preventDefault(); $("form-message").textContent = ""; try { const result = await api("/manual-monitor/setup", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(previewPayload())}); $("form-message").textContent = `${result.mode} position active: ${result.position_id}`; await refresh(); await loadHistory(); } catch (error) { $("form-message").textContent = error.message; } });
$("clear-position").addEventListener("click", async () => { if (!window.confirm("Stop monitoring the active position? Its history will be kept.")) return; await api(`/manual-monitor/setup?mode=${activeTab}`, {method: "DELETE"}); await refresh(); await loadHistory(); });
$("close-position").addEventListener("click", async () => { await api(`/manual-monitor/close?reason=MANUALLY+CLOSED&mode=${activeTab}`); await refresh(); await loadHistory(); });
function setConnection(state) { const labels = {CONNECTED: "CONNECTED", AUTHENTICATION_REQUIRED: "AUTHENTICATION REQUIRED", AUTHENTICATION_FAILED: "AUTHENTICATION FAILED", API_ERROR: "API ERROR"}; if (!labels[state]) return; const resolved = state; const el = $("connection"); const connectButton = $("connect-fyers"); el.textContent = `● ${labels[resolved]}`; el.className = `connection ${resolved.toLowerCase()}`; connectButton.textContent = resolved === "CONNECTED" ? "Reconnect FYERS" : "Connect FYERS"; connectButton.disabled = false; }
async function loadAuthStatus() { try { const status = await api("/fyers/status"); authState = status.state; setConnection(authState); if (status.connected) await loadExpiries(); } catch (error) { authState = "API_ERROR"; setConnection(authState); } }
function legPnlFromValues(leg) { const entry = Number(leg.entry_price); const ltp = Number(leg.ltp); const quantity = Number(leg.quantity); if (![entry, ltp, quantity].every(Number.isFinite)) return null; return (String(leg.leg).includes("BUY") ? ltp - entry : entry - ltp) * quantity; }
function displayedLegPnl(leg) { const backendPnl = Number(leg.pnl); return Number.isFinite(backendPnl) ? backendPnl : legPnlFromValues(leg); }
function displayedLegPnlPct(leg, pnl) { const backendPct = Number(leg.pnl_pct); if (Number.isFinite(backendPct)) return backendPct; const entry = Number(leg.entry_price); const quantity = Number(leg.quantity); return pnl == null || !Number.isFinite(entry) || !Number.isFinite(quantity) || entry === 0 ? null : (pnl / (Math.abs(entry) * Math.max(1, quantity))) * 100; }
function renderLegs(legs) { $("legs-body").innerHTML = legs.length ? legs.map(leg => { const pnl = displayedLegPnl(leg); const pnlPct = displayedLegPnlPct(leg, pnl); return `<tr><td><strong>${leg.leg}</strong></td><td>${fmt(leg.strike, 0)}</td><td>${fmt(leg.quantity, 0)}</td><td>₹${fmt(leg.entry_price)}</td><td>${leg.ltp == null ? "—" : `₹${fmt(leg.ltp)}`}</td><td class="pnl-value ${pnlClass(pnl)}">${money(pnl)}</td><td class="pnl-value ${pnlClass(pnlPct)}">${percent(pnlPct)}</td></tr>`; }).join("") : '<tr><td colspan="7" class="empty">Enter your position above to begin.</td></tr>'; }
function renderCharges(charges, gross, net, netPct) { const breakdown = charges?.breakdown || []; $("charges-body").innerHTML = breakdown.length ? breakdown.map(item => `<div><span>${item.label}</span><strong class="charge-value">₹${fmt(item.amount)}</strong></div>`).join("") : '<div><span>No charge data</span><strong>—</strong></div>'; const total = charges?.total || 0; $("charges-total").textContent = `₹${fmt(total)}`; $("summary-gross").textContent = money(gross); $("summary-gross").className = `pnl-value ${pnlClass(gross)}`; $("summary-charges").textContent = `₹${fmt(total)}`; $("summary-net").textContent = money(net); $("summary-net").className = `pnl-value ${pnlClass(net)}`; $("summary-net-pct").textContent = percent(netPct); $("summary-net-pct").className = `pnl-value ${pnlClass(netPct)}`; }
const expandedHistoryIds = new Set();
function renderHistory(list) { const target = $("history-list"); if (!Array.isArray(list) || !list.length) { expandedHistoryIds.clear(); target.innerHTML = '<div class="subtle">No prior positions yet.</div>'; return; } const visibleIds = new Set(list.map(item => item.position_id)); expandedHistoryIds.forEach(id => { if (!visibleIds.has(id)) expandedHistoryIds.delete(id); }); target.innerHTML = list.map(item => { const positionId = item.position_id || ""; const isExpanded = expandedHistoryIds.has(positionId); return `
    <div class="history-item">
  <div class="row"><strong>${positionId || "Position"}</strong><span class="history-actions"><span class="mode-pill ${item.mode && item.mode.toLowerCase() === "paper" ? "paper" : "market"}">${item.mode || "MARKET"}</span><button class="history-delete" type="button" data-history-delete="${positionId}" aria-label="Delete ${positionId || "history record"}">Delete</button></span></div>
      <div class="row"><span class="history-label">${item.expiry || "—"}</span><span>${item.status || "OPEN"}</span></div>
      <div class="row"><span>Entry</span><span>${item.opened_at ? new Date(item.opened_at).toLocaleString("en-IN") : "—"}</span></div>
      <div class="row"><span>Exit</span><span>${item.closed_at ? new Date(item.closed_at).toLocaleString("en-IN") : "—"}</span></div>
      <div class="row"><span>Initial credit</span><span>₹${fmt(item.initial_net_credit, 0)}</span></div>
      <div class="row"><span>Gross P&amp;L</span><span class="pnl-value ${pnlClass(item.gross_pnl)}">${money(item.gross_pnl)}</span></div>
      <div class="row"><span>Total charges</span><span>₹${fmt(item.charges)}</span></div>
      <div class="row"><span>Net P&amp;L</span><span class="pnl-value ${pnlClass(item.net_pnl)}">${money(item.net_pnl)}</span></div>
      <div class="row"><span>Net P&amp;L %</span><span class="pnl-value ${pnlClass(item.net_pnl_pct)}">${percent(item.net_pnl_pct)}</span></div>
      <details class="history-details" data-history-details="${positionId}"${isExpanded ? " open" : ""}><summary><span class="history-chevron" aria-hidden="true">${isExpanded ? "▼" : "▶"}</span>View four legs</summary><div class="history-leg-table"><div class="history-leg-head"><span>LEG</span><span>STRIKE</span><span>QTY</span><span>ENTRY</span><span>${item.status === "OPEN" ? "CURRENT LTP" : "EXIT LTP"}</span><span>P&amp;L</span><span>P&amp;L %</span></div>${(item.legs || []).map(leg => { const mark = leg.exit_price == null ? leg.current_ltp : leg.exit_price; const displayLeg = {...leg, ltp: mark}; const pnl = displayedLegPnl(displayLeg); const pnlPct = displayedLegPnlPct(displayLeg, pnl); return `<div><strong>${leg.leg}</strong><span>${fmt(leg.strike, 0)}</span><span>${fmt(leg.quantity, 0)}</span><span>₹${fmt(leg.entry_price)}</span><span>${mark == null ? "—" : `₹${fmt(mark)}`}</span><span class="pnl-value ${pnlClass(pnl)}">${money(pnl)}</span><span class="pnl-value ${pnlClass(pnlPct)}">${percent(pnlPct)}</span></div>`; }).join("") || "No leg detail saved."}</div><div class="history-charge-list">${(item.charge_breakdown?.breakdown || []).map(charge => `<div><span>${charge.label}</span><strong>₹${fmt(charge.amount)}</strong></div>`).join("")}<div class="charge-total"><span>Total Charges</span><strong>₹${fmt(item.charge_breakdown?.total ?? item.charges ?? 0)}</strong></div></div>${item.reason_for_closing ? `<p class="subtle">${item.reason_for_closing}</p>` : ""}</details>
    </div>
  `; }).join(""); target.querySelectorAll("[data-history-details]").forEach(details => details.addEventListener("toggle", () => { const id = details.dataset.historyDetails; if (details.open) expandedHistoryIds.add(id); else expandedHistoryIds.delete(id); const chevron = details.querySelector(".history-chevron"); if (chevron) chevron.textContent = details.open ? "▼" : "▶"; })); target.querySelectorAll("[data-history-delete]").forEach(button => button.addEventListener("click", () => openDeleteHistory(button.dataset.historyDelete))); }
let pendingHistoryId = null;
function openDeleteHistory(positionId) { pendingHistoryId = positionId; $("delete-history-dialog").showModal(); }
$("cancel-delete-history").addEventListener("click", () => { pendingHistoryId = null; $("delete-history-dialog").close(); });
$("confirm-delete-history").addEventListener("click", async () => { if (!pendingHistoryId) return; const positionId = pendingHistoryId; const button = $("confirm-delete-history"); button.disabled = true; try { await api(`/manual-monitor/history/${encodeURIComponent(positionId)}`, {method: "DELETE"}); expandedHistoryIds.delete(positionId); $("delete-history-dialog").close(); pendingHistoryId = null; await loadHistory(); } catch (error) { $("delete-history-message").textContent = error.message; } finally { button.disabled = false; } });
async function loadHistory() { try { const list = await api("/manual-monitor/history"); renderHistory(list); } catch (error) { $("history-list").innerHTML = `<div class="subtle">Unable to load history: ${error.message}</div>`; } }
async function refreshHistory() {
  await loadHistory();
}
function renderFund(fund, mode) { if (!fund) return; $("fund-mode-tag").textContent = mode || "MARKET"; $("gross-hedge").textContent = `₹${fmt(fund.gross_hedge_requirement, 0)}`; $("short-proceeds").textContent = `₹${fmt(fund.short_leg_proceeds, 0)}`; $("net-cash").textContent = `₹${fmt(fund.net_initial_cash_requirement, 0)}`; $("fund-charges").textContent = `₹${fmt(fund.charges, 0)}`; $("final-funds").textContent = `₹${fmt(fund.final_estimated_fund_requirement, 0)}`; }
function renderLadder(data) { ["lower_hedge", "lower_short", "lower_breakeven", "spot", "upper_breakeven", "upper_short", "upper_hedge"].forEach(key => { const className = key === "spot" ? "spot-mark" : key.replace("_", "-"); const el = document.querySelector(`.${className}`); if (el && data[key] !== undefined) el.textContent = key === "spot" ? "●" : fmt(data[key], 0); }); const spot = document.querySelector(".spot-mark"); const range = Number(data.upper_hedge) - Number(data.lower_hedge); if (spot && Number.isFinite(Number(data.spot)) && range > 0) { const position = Math.max(0, Math.min(162, (Number(data.upper_hedge) - Number(data.spot)) / range * 162)); spot.style.top = `${position}px`; } }
function timeLeft(seconds) { if (seconds === undefined) return "—"; const d = Math.floor(seconds / 86400); const h = Math.floor(seconds % 86400 / 3600); const m = Math.floor(seconds % 3600 / 60); return `${d ? `${String(d).padStart(2, "0")}D ` : ""}${String(h).padStart(2, "0")}H ${String(m).padStart(2, "0")}M`; }
function render(data) { setConnection(data.connection || "DISCONNECTED"); $("last-update").textContent = `Updated ${new Date().toLocaleTimeString()}`; if (!data.configured) return; $("expiry-label").textContent = new Date(`${data.expiry}T00:00:00`).toLocaleDateString("en-IN", {weekday: "long", day: "2-digit", month: "short", year: "numeric"}); if (data.spot === undefined) { renderLegs(data.legs || []); return; } $("spot").textContent = `₹${fmt(data.spot, 1)}`; $("spot-change").textContent = `${signed(data.spot_change)} ${data.spot_change_pct == null ? "" : `(${signed(data.spot_change_pct)}%)`}`; $("time-left").textContent = timeLeft(data.seconds_to_expiry); $("total-pnl").textContent = money(data.total_pnl); $("total-pnl").className = `pnl-value ${pnlClass(data.total_pnl)}`; $("today-pnl").textContent = money(data.total_pnl); $("today-pnl").className = `pnl-value ${pnlClass(data.total_pnl)}`; renderLegs(data.legs); renderCharges(data.charges, data.total_gross_pnl, data.net_pnl, data.net_pnl_pct); $("call-credit").textContent = `₹${fmt(data.call_credit)}`; $("put-credit").textContent = `₹${fmt(data.put_credit)}`; $("net-credit").textContent = `₹${fmt(data.net_credit)}`; $("max-profit").textContent = `₹${fmt(data.max_initial_profit, 0)}`; $("call-spread-pnl").textContent = money(data.call_spread_pnl); $("put-spread-pnl").textContent = money(data.put_spread_pnl); $("distance-put").textContent = `${fmt(Math.abs(data.distance_put_short), 0)} pts`; $("distance-call").textContent = `${fmt(Math.abs(data.distance_call_short), 0)} pts`; $("distance-lower-be").textContent = `${fmt(Math.abs(data.distance_lower_breakeven), 0)} pts`; $("distance-upper-be").textContent = `${fmt(Math.abs(data.distance_upper_breakeven), 0)} pts`; $("risk-badge").textContent = data.risk; $("risk-badge").className = `risk-badge ${data.risk === "WARNING" ? "watch" : data.risk.toLowerCase()}`; renderLadder(data); $("call-iv").textContent = data.call_iv == null ? "—" : `${fmt(data.call_iv, 1)}%`; $("put-iv").textContent = data.put_iv == null ? "—" : `${fmt(data.put_iv, 1)}%`; $("average-iv").textContent = data.average_short_iv == null ? "—" : `${fmt(data.average_short_iv, 1)}% (${signed(data.iv_change)})`; $("call-sl-status").textContent = data.call_sl == null ? "Not set" : `₹${fmt(data.call_sl, 0)} / ${money(data.call_spread_pnl)}`; $("put-sl-status").textContent = data.put_sl == null ? "Not set" : `₹${fmt(data.put_sl, 0)} / ${money(data.put_spread_pnl)}`; $("alerts").innerHTML = data.alerts?.length ? data.alerts.map(alert => `<li>⚠ ${alert}</li>`).join("") : `<li class="subtle">No alerts.</li>`; }
function renderSlMonitor(data) { if (!data || !data.configured) return; const advance = data.advance_sl || {}; const call = advance.call || {}; const put = advance.put || {}; renderSlPreview({available: true, spot: data.spot, legs: data.legs, call, put, call_sl_state: data.call_sl_state, put_sl_state: data.put_sl_state, call_sl_triggered: data.call_sl_triggered, put_sl_triggered: data.put_sl_triggered}); $("call-sl-status").textContent = `${slLabel(data.call_sl_state)} · ${call.sl_trigger_price == null ? "SL trigger unavailable" : `NIFTY ${fmt(call.sl_trigger_price, 0)}`} / ${money(data.call_spread_pnl)}`; $("put-sl-status").textContent = `${slLabel(data.put_sl_state)} · ${put.sl_trigger_price == null ? "SL trigger unavailable" : `NIFTY ${fmt(put.sl_trigger_price, 0)}`} / ${money(data.put_spread_pnl)}`; }
const baseRender = render;
render = data => { baseRender(data); renderSlMonitor(data); if (data.configured && data.spot !== undefined) { $("total-pnl").textContent = money(data.net_pnl); $("total-pnl").className = `pnl-value ${pnlClass(data.net_pnl)}`; $("today-pnl").textContent = money(data.net_pnl); $("today-pnl").className = `pnl-value ${pnlClass(data.net_pnl)}`; } };
async function refresh() { if (activeTab === "HISTORY") return; try { const snapshot = await api(`/manual-monitor?mode=${activeTab}`); render(snapshot); renderFund(snapshot.fund_requirement, snapshot.mode); } catch (error) { $("alerts").innerHTML = `<li>⚠ ${error.message}</li>`; } }
loadAuthStatus(); loadHistory(); refresh(); setInterval(loadAuthStatus, 5000); setInterval(refresh, 3000); setInterval(() => { if (activeTab === "HISTORY") refreshHistory(); }, 3000);
