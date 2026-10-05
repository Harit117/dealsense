// DealSense dashboard. Vanilla JS, talks to the FastAPI backend.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { sort: "score", outKind: "", summary: null };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 3200);
}

// ---------------------------------------------------------------- formatting
const inr = (v) => {
  if (v == null) return "—";
  if (v >= 1e7) return `₹${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `₹${(v / 1e5).toFixed(1)} L`;
  return `₹${Math.round(v).toLocaleString("en-IN")}`;
};
const scoreCls = (s) => (s >= 80 ? "hot" : s >= 50 ? "warm" : "cool");
const scoreBadge = (s) => `<span class="score ${scoreCls(s)}">${s == null ? "—" : Math.round(s)}</span>`;
const stepCls = (s) => ({ "Call now": "call", "Book visit": "visit", Negotiate: "negotiate", "Follow up": "follow", Nurture: "nurture" }[s] || "visit");
const shortName = (n) => { const p = n.split(" "); return p.length === 2 && /^[A-Z][a-z]/.test(p[1]) ? `${p[0]} ${p[1][0]}.` : n; };
const ago = (iso) => {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};
const wants = (l) => {
  const what = l.property_type === "plot" ? "Plot" : l.property_type === "villa" ? `${l.bhk || ""}BHK villa` : l.bhk ? `${l.bhk}BHK` : (l.property_type || "Any");
  return `${what}${l.localities.length ? " · " + l.localities.join(", ") : ""}`;
};
const FEATURE_LABELS = {
  budget_fit: "Budget fit", location: "Location match", requirements: "Requirements", seller_flex: "Seller flexibility",
  urgency: "Buyer urgency", financing: "Financing ready", engagement: "Engagement", responsiveness: "Reply behaviour",
  recency: "Recency (decay)", broker_affinity: "Your conversion here",
};

// ---------------------------------------------------------------- tabs
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
  ({ today: loadToday, leads: loadLeads, properties: loadProps, learning: loadLearning, outbox: loadOutbox }[b.dataset.tab] || (() => {}))();
});

// ---------------------------------------------------------------- today
async function loadSummary() {
  const s = (state.summary = await api("/api/summary"));
  $("#topMeta").innerHTML = `Broker: <b>${esc(s.broker)}</b> · ${
    s.llm.enabled ? `<span class="pill llm">AI extraction: ${esc(s.llm.model)}</span>` : `<span class="pill rules">AI extraction: rules (no API key)</span>`
  }`;
  const r = s.routes;
  $("#kpis").innerHTML = [
    [s.leads, "Active buyers"],
    [s.properties, "Live listings"],
    [s.deals.toLocaleString("en-IN"), "Buyer × property pairs scored"],
    [r.alert || 0, "Hot (80+) → instant alert"],
    [r.digest || 0, "Warm (50–79) → digest"],
    [inr(s.pipeline_value), "Expected commission in pipeline"],
  ].map(([v, l]) => `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
  const d = new Date();
  $("#todayMeta").textContent = `${d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" })} · Broker: ${s.broker} · ` +
    (s.last_refresh ? `last re-scored ${ago(s.last_refresh)}` : "");
}

async function loadTop() {
  const rows = await api(`/api/top?sort=${state.sort}`);
  $("#topBody").innerHTML = rows.length ? rows.map((d, i) => `
    <tr data-lead="${d.lead_id}" data-prop="${d.property_id}">
      <td class="num">${i + 1}</td>
      <td class="pair"><b>${esc(shortName(d.lead_name))} × ${esc(d.property_label)}</b><div class="sub">${esc(d.source)} · ${inr(d.price)}</div></td>
      <td>${scoreBadge(d.score)}</td>
      <td class="why hide-sm">${esc(d.reasons.filter((r) => r.sign === "+").slice(0, 2).map((r) => r.text).join(" · "))}</td>
      <td class="num hide-sm">${inr(d.expected_value)}</td>
      <td><span class="step ${stepCls(d.next_step)}">${esc(d.next_step)}</span></td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">No deals above 50 yet. Capture a lead to get started.</td></tr>`;
}

async function loadAlerts() {
  const items = await api("/api/outbox?kind=alert&limit=25");
  $("#alerts").innerHTML = items.length ? items.map((o) => `
    <div class="bubble ${o.status === "snoozed" ? "snoozed" : ""}">
      <div class="t">${esc(o.title)}</div>
      <div class="b">${esc(o.body)}</div>
      <div class="acts">
        <button class="call" data-open="${o.lead_id}:${o.property_id}">Call now</button>
        <button data-snooze="${o.id}">${o.status === "snoozed" ? "Snoozed" : "Snooze"}</button>
      </div>
      <div class="time">${ago(o.created_at)} · ${esc(o.channel)}</div>
    </div>`).join("") : `<div class="empty">No hot deals right now.</div>`;
}

async function loadToday() {
  await Promise.all([loadSummary(), loadTop(), loadAlerts()]);
}

$("#topBody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-lead]");
  if (tr) openDeal(+tr.dataset.lead, +tr.dataset.prop);
});
$("#alerts").addEventListener("click", async (e) => {
  const o = e.target.closest("[data-open]");
  if (o) { const [l, p] = o.dataset.open.split(":"); openDeal(+l, +p); }
  const s = e.target.closest("[data-snooze]");
  if (s) { await api(`/api/outbox/${s.dataset.snooze}/snooze`, { method: "POST" }); loadAlerts(); }
});
$("#sortSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-sort]");
  if (!b) return;
  state.sort = b.dataset.sort;
  $$("#sortSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadTop();
});
$("#refreshBtn").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/api/refresh", { method: "POST" });
    toast(`Re-scored ${r.rescored} deals · ${r.actions.alerts} new alerts · ${r.actions.nurtures} nurture messages · digest sent`);
    loadToday();
  } finally { e.target.disabled = false; }
});

// ---------------------------------------------------------------- deal drawer
function openDrawer(html) {
  $("#drawerBody").innerHTML = html;
  $("#drawer").classList.add("open");
  $("#scrim").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#scrim").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
}
$("#drawerClose").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());

async function openDeal(leadId, propId) {
  const { lead, deals, events } = await api(`/api/leads/${leadId}`);
  const d = deals.find((x) => x.property_id === propId) || deals[0];
  if (!d) return openLead(leadId);
  const msgs = lead.raw_messages.slice(-3).reverse();
  openDrawer(`
    <div class="muted">${esc(lead.source)} lead · ${esc(lead.phone || "no phone")}</div>
    <h3>${esc(lead.name)} × ${esc(d.property_label)}</h3>
    <div class="muted">${esc(d.title)} · ${inr(d.price)}</div>
    <div class="big-score"><span class="n" style="color:var(--${scoreCls(d.score) === "hot" ? "hot" : scoreCls(d.score) === "warm" ? "warm" : "cool"})">${Math.round(d.score)}</span><span class="muted">/100 · P(close) ${(d.p_close * 100).toFixed(0)}% × commission ${inr(d.deal_value)} = <b>${inr(d.expected_value)}</b> expected</span></div>
    <span class="step ${stepCls(d.next_step)}">${esc(d.next_step)}</span> <span class="muted">route: ${esc(d.route)}${d.outcome ? ` · logged: <b>${esc(d.outcome)}</b>` : ""}</span>

    <h4>Why</h4>
    <ul class="reasons">${d.reasons.map((r) => `<li><span class="s ${r.sign === "+" ? "p" : "n"}">${r.sign === "+" ? "✓" : "!"}</span>${esc(r.text)}</li>`).join("")}</ul>

    <h4>Signals</h4>
    <div class="bars">${Object.entries(d.features).map(([k, v]) => `
      <div class="bar"><span>${FEATURE_LABELS[k] || k}</span><div class="track"><div class="fill" style="width:${Math.round(v * 100)}%"></div></div><span class="num">${v.toFixed(2)}</span></div>`).join("")}
    </div>

    <h4>Log what happened <span class="muted" style="text-transform:none;font-weight:400">(feeds the learning loop)</span></h4>
    <div class="btn-row">
      <button class="btn" data-outcome="called">Called</button>
      <button class="btn" data-outcome="visited">Site visit done</button>
      <button class="btn" data-outcome="closed">Closed 🎉</button>
      <button class="btn danger" data-outcome="lost">Lost</button>
      <button class="btn ghost" data-outcome="override_up" title="Your gut says this is better than the score">▲ Override up</button>
      <button class="btn ghost" data-outcome="override_down" title="Your gut says this is worse than the score">▼ Override down</button>
    </div>
    <h4>Simulate buyer activity</h4>
    <div class="btn-row">
      <button class="btn ghost" data-event="view">Buyer viewed listing</button>
      <button class="btn ghost" data-event="reply">Buyer replied</button>
    </div>

    <h4>Call brief</h4>
    <div id="briefBox"><button class="btn primary" id="briefBtn">Generate brief</button></div>

    <h4>Recent messages</h4>
    ${msgs.map((m) => `<div class="msg"><div class="meta">${esc(m.source || lead.source)} · ${ago(m.at)}</div>${esc(m.text)}</div>`).join("")}

    <h4>Other matches for ${esc(lead.name.split(" ")[0])} (${deals.length})</h4>
    ${deals.filter((x) => x !== d).slice(0, 5).map((x) => `
      <div class="match" data-goto="${x.property_id}">${scoreBadge(x.score)}<div class="info"><b>${esc(x.property_label)}</b><div class="muted">${esc(x.title)} · ${inr(x.price)}</div></div></div>`).join("") || `<div class="muted">None</div>`}
  `);

  $("#drawerBody").onclick = async (e) => {
    const out = e.target.closest("[data-outcome]");
    const ev = e.target.closest("[data-event]");
    const go = e.target.closest("[data-goto]");
    if (out) {
      const r = await api(`/api/deals/${leadId}/${d.property_id}/outcome`, { method: "POST", body: { outcome: out.dataset.outcome } });
      toast(r.retrained ? `Logged. Model retrained on ${r.retrained.n_train} outcomes.` : `Logged "${out.dataset.outcome}". ${r.broker_outcomes} broker outcomes so far.`);
      await loadToday();
      if (out.dataset.outcome === "closed") closeDrawer(); else openDeal(leadId, d.property_id);
    } else if (ev) {
      const r = await api("/api/events", { method: "POST", body: { lead_id: leadId, kind: ev.dataset.event, property_id: d.property_id } });
      const nd = r.deal;
      toast(nd ? `Score ${Math.round(d.score)} → ${Math.round(nd.score)}${r.actions.alerts ? " · HOT alert sent" : ""}` : "Logged");
      await loadToday();
      openDeal(leadId, d.property_id);
    } else if (go) {
      openDeal(leadId, +go.dataset.goto);
    } else if (e.target.id === "briefBtn") {
      e.target.disabled = true;
      e.target.textContent = "Writing…";
      const b = await api(`/api/deals/${leadId}/${d.property_id}/brief`);
      $("#briefBox").innerHTML = `<div class="brief">${esc(b.brief)}</div><div class="muted" style="margin-top:4px">${b.source === "llm" ? "Written by Claude" : "Template (set ANTHROPIC_API_KEY for AI briefs)"}</div>`;
    }
  };
}

async function openLead(leadId) {
  const { lead, deals } = await api(`/api/leads/${leadId}`);
  if (deals.length) return openDeal(leadId, deals[0].property_id);
  openDrawer(`<h3>${esc(lead.name)}</h3><div class="muted">${esc(wants(lead))}</div><p>No matching listings yet. This buyer is in nurture mode.</p>
    ${lead.raw_messages.map((m) => `<div class="msg"><div class="meta">${esc(m.source || lead.source)} · ${ago(m.at)}</div>${esc(m.text)}</div>`).join("")}`);
}

// ---------------------------------------------------------------- intake
const EXAMPLES = [
  ["WhatsApp", "", "hi sir looking 3bhk whitefield or marathahalli around 1.3cr, loan approved, want to shift in 2 months. 98450 11111"],
  ["99acres", "Kavitha Shenoy", "[99acres] Enquiry for 2BHK in HSR Layout. Budget: 1.3 Cr to 1.5 Cr. Buyer note: urgent, relocating from Pune. +91 98800 22222"],
  ["Email", "", "Hello, I am Farhan Siddiqui. We want a plot in Sarjapur or Electronic City under 95 lakhs, just exploring for now. farhan.s@example.com"],
  ["Call", "Divya Hegde", "Call note: wants 2 bhk jp nagar / jayanagar, 1.1 to 1.3 cr, within 30 days, full cash. 99001 33333"],
];
$("#examples").innerHTML = EXAMPLES.map((e, i) => `<button data-ex="${i}">${esc(e[0])} example</button>`).join("");
$("#examples").onclick = (e) => {
  const b = e.target.closest("[data-ex]");
  if (!b) return;
  const [src, name, text] = EXAMPLES[+b.dataset.ex];
  $("#inSource").value = src; $("#inName").value = name; $("#inText").value = text;
};
$("#inSubmit").onclick = async () => {
  const text = $("#inText").value.trim();
  if (!text) return toast("Paste a message first");
  $("#inSubmit").disabled = true;
  $("#inStatus").textContent = state.summary?.llm.enabled ? " Extracting with Claude…" : " Processing…";
  try {
    const r = await api("/api/intake", { method: "POST", body: { text, source: $("#inSource").value, name: $("#inName").value || null } });
    renderIntake(r);
    loadSummary();
  } catch (err) {
    toast(`Failed: ${err.message}`);
  } finally {
    $("#inSubmit").disabled = false;
    $("#inStatus").textContent = "";
  }
};

function renderIntake(r) {
  const l = r.lead, x = r.extracted;
  const best = r.top_matches[0];
  const routeTxt = !best ? "No matching listing yet → nurture" :
    best.route === "alert" ? `Score ${Math.round(best.score)} → instant WhatsApp alert to broker` :
    best.route === "digest" ? `Score ${Math.round(best.score)} → today's Top-10 digest` : `Score ${Math.round(best.score)} → auto-nurture message queued`;
  $("#inResult").innerHTML = `
    <h2>${esc(l.name)} ${r.deduplicated ? `<span class="pill rules">merged with existing buyer</span>` : `<span class="pill llm">new buyer</span>`}</h2>
    <div class="muted">Extracted by ${r.extraction === "llm" ? "Claude" : "rule-based parser"}</div>
    <dl class="kv">
      <dt>Budget</dt><dd>${x.budget_min || x.budget_max ? `${inr(x.budget_min)} – ${inr(x.budget_max)}` : "—"}</dd>
      <dt>Localities</dt><dd>${x.localities.map((t) => `<span class="tag">${esc(t)}</span>`).join("") || "—"}</dd>
      <dt>Configuration</dt><dd>${x.bhk ? x.bhk + "BHK " : ""}${esc(x.property_type || "—")}</dd>
      <dt>Urgency</dt><dd>${x.urgency_days ? `within ~${x.urgency_days} days` : "—"}</dd>
      <dt>Financing</dt><dd>${x.loan_preapproved ? "Loan pre-approved / cash" : "Not confirmed"}</dd>
      <dt>Phone / email</dt><dd>${esc(l.phone || "—")} ${l.email ? " · " + esc(l.email) : ""}</dd>
      <dt>Routing</dt><dd>${esc(routeTxt)}</dd>
    </dl>
    <h4 class="muted" style="margin:0 0 4px">Top matches (${r.top_matches.length ? "of " + r.top_matches.length + "+" : "none"})</h4>
    ${r.top_matches.map((d) => `<div class="match" data-l="${d.lead_id}" data-p="${d.property_id}">${scoreBadge(d.score)}<div class="info"><b>${esc(d.property_label)}</b> <span class="muted">${inr(d.price)}</span> <span class="muted">· ${esc(d.next_step)}</span><div class="muted">${esc(d.reasons.slice(0, 3).map((x) => x.text).join(" · "))}</div></div></div>`).join("") || `<p class="muted">Nothing in inventory fits yet.</p>`}
  `;
  $("#inResult").onclick = (e) => {
    const m = e.target.closest("[data-l]");
    if (m) openDeal(+m.dataset.l, +m.dataset.p);
  };
}

// ---------------------------------------------------------------- leads & properties
async function loadLeads() {
  const q = $("#leadSearch").value;
  const rows = await api(`/api/leads${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  $("#leadsBody").innerHTML = rows.map((l) => `
    <tr data-lead="${l.id}">
      <td><b>${esc(l.name)}</b>${l.status !== "active" ? ` <span class="tag">${esc(l.status)}</span>` : ""}<div class="muted">${esc(l.phone || "")}</div></td>
      <td>${esc(l.source)}</td>
      <td>${esc(wants(l))}</td>
      <td class="hide-sm num">${l.budget_max ? inr(l.budget_max) : "—"}</td>
      <td class="hide-sm">${l.urgency_days ? `${l.urgency_days} days` : "—"}</td>
      <td>${scoreBadge(l.best_score)} <span class="muted">${l.matches} matches</span></td>
    </tr>`).join("");
}
$("#leadsBody").onclick = (e) => { const tr = e.target.closest("tr[data-lead]"); if (tr) openLead(+tr.dataset.lead); };
let searchT;
$("#leadSearch").oninput = () => { clearTimeout(searchT); searchT = setTimeout(loadLeads, 200); };

async function loadProps() {
  const rows = await api("/api/properties");
  $("#propsBody").innerHTML = rows.map((p) => `
    <tr>
      <td><b>${esc(p.title)}</b>${p.status !== "available" ? ` <span class="tag">${esc(p.status)}</span>` : ""}</td>
      <td>${esc(p.locality)}</td>
      <td class="num">${inr(p.price)}</td>
      <td class="hide-sm">${p.seller_flexibility >= 0.6 ? "Negotiable" : p.seller_flexibility >= 0.3 ? "Some" : "Firm"}</td>
      <td class="num">${p.interested}</td>
      <td>${scoreBadge(p.best_score)}</td>
    </tr>`).join("");
}

// ---------------------------------------------------------------- learning
async function loadLearning() {
  const m = await api("/api/model");
  const kindTxt = m.kind === "rules" ? "Rule-based (day 1)" : `Blended: ${Math.round(m.alpha * 100)}% learned`;
  $("#modelStats").innerHTML = [
    [kindTxt, "Current weights"],
    [m.n_train ?? 0, "Outcomes trained on"],
    [m.broker_outcomes, "Outcomes you logged"],
    [m.auc_model_holdout ? `${m.auc_rules_holdout} → ${m.auc_model_holdout}` : "—", "Holdout AUC: rules → model"],
  ].map(([v, l]) => `<div class="stat"><div class="v">${esc(v)}</div><div class="l">${l}</div></div>`).join("") +
    (m.note ? `<p class="muted">${esc(m.note)}</p>` : "") +
    (m.trained_at ? `<p class="muted">Last trained ${ago(m.trained_at)}</p>` : "");
  const keys = Object.keys(m.rule_weights).filter((k) => k !== "intercept");
  const max = Math.max(...keys.flatMap((k) => [Math.abs(m.rule_weights[k]), Math.abs(m.weights[k])]));
  $("#weights").innerHTML = keys.map((k) => {
    const r = m.rule_weights[k], c = m.weights[k];
    return `<div class="wrow"><span>${FEATURE_LABELS[k] || k}</span>
      <div class="wbars"><div class="r" style="width:${(Math.abs(r) / max) * 100}%"></div><div class="c" style="width:${(Math.abs(c) / max) * 100}%"></div></div>
      <span class="num">${c.toFixed(2)}</span></div>`;
  }).join("");
}
$("#retrainBtn").onclick = async (e) => {
  e.target.disabled = true;
  try {
    const m = await api("/api/retrain", { method: "POST" });
    toast(m.kind === "rules" ? m.note : `Retrained on ${m.n_train} outcomes · holdout AUC ${m.auc_rules_holdout} → ${m.auc_model_holdout}`);
    loadLearning();
    loadSummary();
  } finally { e.target.disabled = false; }
};

// ---------------------------------------------------------------- outbox
async function loadOutbox() {
  const items = await api(`/api/outbox?limit=80${state.outKind ? `&kind=${state.outKind}` : ""}`);
  $("#outList").innerHTML = items.map((o) => `
    <div class="out">
      <div><div class="k ${o.kind}">${esc(o.kind)}</div><div class="muted">${esc(o.channel)}</div></div>
      <div><b>${esc(o.title || "")}</b><div class="body">${esc(o.body)}</div></div>
      <div class="when">${ago(o.created_at)}<br>${esc(o.status)}</div>
    </div>`).join("") || `<div class="empty">Nothing here yet.</div>`;
}
$("#outSeg").onclick = (e) => {
  const b = e.target.closest("button[data-kind]");
  if (!b) return;
  state.outKind = b.dataset.kind;
  $$("#outSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadOutbox();
};

loadToday().catch((err) => toast(`Could not reach the API: ${err.message}`));
