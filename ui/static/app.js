"use strict";

/* ================================================================ helpers */
const $ = (sel, root = document) => root.querySelector(sel);
const SVG_NS = "http://www.w3.org/2000/svg";
const REFRESH_MS = 3000;
const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
function s(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
function replace(target, ...children) {
  const el = typeof target === "string" ? $(target) : target;
  el.replaceChildren();
  append(el, children);
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok && !options.allowError) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status, body });
  return { status: res.status, body };
}

const fmtNum = n => (n === null || n === undefined) ? "–" : Number(n).toLocaleString();
function fmtMs(ms) {
  if (ms === null || ms === undefined) return "–";
  ms = Number(ms);
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
  if (ms < 3600000) return `${(ms / 60000).toFixed(1)} min`;
  return `${(ms / 3600000).toFixed(1)} h`;
}
function fmtTime(iso, withDate) {
  if (!iso) return "–";
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return (withDate || !today) ? `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}` : time;
}
function fmtAgo(seconds) {
  if (seconds === null || seconds === undefined) return null;
  seconds = Math.max(0, Number(seconds));
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}
const shortId = id => id ? String(id).slice(0, 8) : "–";

/* ================================================================ status badges */
const ICONS = {
  COMPLETED: "M3.5 8.5l3 3 6-6.5",
  DELIVERED: "M3.5 8.5l3 3 6-6.5",
  ok: "M3.5 8.5l3 3 6-6.5",
  FAILED: "M4.5 4.5l7 7M11.5 4.5l-7 7",
  down: "M4.5 4.5l7 7M11.5 4.5l-7 7",
  PENDING: "M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11M8 5v3.3l2.2 1.4",
  IN_PROGRESS: "M13 8a5 5 0 1 1-1.6-3.7M13 2.8v3h-3",
  CANCELLED: "M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11M4.2 4.2l7.6 7.6",
  none: "M4 8h8",
};
const LABELS = { IN_PROGRESS: "In progress", PENDING: "Pending", COMPLETED: "Completed", FAILED: "Failed",
  CANCELLED: "Cancelled", DELIVERED: "Delivered", ok: "Healthy", down: "Down", none: "None" };
const STATUS_COLOR = { COMPLETED: "var(--good)", FAILED: "var(--critical)", PENDING: "var(--warning)",
  IN_PROGRESS: "var(--series-1)", CANCELLED: "var(--neutral)" };

function badge(status, label) {
  const key = status || "none";
  const icon = s("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" });
  icon.append(s("path", { d: ICONS[key] || ICONS.none, fill: "none", stroke: "currentColor",
    "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" }));
  // callback PENDING / FAILED share request status styling
  return h("span", { class: `badge s-${key}` }, icon, label || LABELS[key] || key);
}

/* ================================================================ JSON view */
function jsonView(value) {
  const pre = h("pre", { class: "json" });
  if (value === null || value === undefined) { pre.textContent = "null"; return pre; }
  const text = JSON.stringify(value, null, 2);
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) pre.append(text.slice(last, m.index));
    if (m[1]) {
      pre.append(h("span", { class: m[2] ? "j-key" : "j-str", text: m[1] }));
      if (m[2]) pre.append(m[2]);
    } else if (m[3]) pre.append(h("span", { class: "j-lit", text: m[3] }));
    else pre.append(h("span", { class: "j-num", text: m[4] }));
    last = re.lastIndex;
  }
  pre.append(text.slice(last));
  return pre;
}

/* ================================================================ tooltip & toast */
const tip = $("#tooltip");
function showTip(evt, title, rows) {
  replace(tip, h("div", { class: "t-title", text: title }),
    rows.map(([k, v]) => h("div", { class: "t-row" }, h("span", { text: k }), h("b", { text: v }))));
  tip.hidden = false;
  const pad = 14, w = tip.offsetWidth, hgt = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
  if (y + hgt > window.innerHeight - 8) y = evt.clientY - hgt - pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
const hideTip = () => { tip.hidden = true; };

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

/* ================================================================ tables */
function table(columns, rows, { onRow, empty = "Nothing here yet." } = {}) {
  if (!rows.length) return h("div", { class: "empty", text: empty });
  const thead = h("thead", {}, h("tr", {}, columns.map(c => h("th", { class: c.num ? "num" : null, text: c.label }))));
  const tbody = h("tbody", {}, rows.map(r => h("tr", {
      class: onRow ? "clickable" : null,
      onclick: onRow ? () => onRow(r) : null,
    }, columns.map(c => {
      const v = c.render(r);
      return h("td", { class: [c.num ? "num" : "", c.cls || ""].join(" ").trim() || null }, v ?? "–");
    }))));
  return h("div", { class: "table-wrap" }, h("table", {}, thead, tbody));
}

const REQUEST_COLUMNS = [
  { label: "Received", render: r => fmtTime(r.received_at) },
  { label: "Request", render: r => h("span", { class: "mono", title: r.request_id, text: shortId(r.request_id) }) },
  { label: "Workflow", render: r => r.workflow_name },
  { label: "Status", render: r => badge(r.status) },
  { label: "Workflow status", render: r => r.workflow_status },
  { label: "Retries", num: true, render: r => fmtNum(r.retry_count) },
  { label: "End-to-end", num: true, render: r => TERMINAL.has(r.status) ? fmtMs(r.end_to_end_ms) : "running" },
  { label: "Callback", render: r => r.callback_status ? badge(r.callback_status === "PENDING" ? "PENDING" : r.callback_status) : h("span", { class: "muted", text: "polling" }) },
  { label: "Error", cls: "trunc", render: r => r.error_code ? h("span", { class: "mono", text: r.error_code }) : null },
];

/* ================================================================ charts */
function niceMax(v) {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  const n = v / pow;
  return (n <= 2 ? 2 : n <= 5 ? 5 : 10) * pow;
}
function roundedTopBar(x, y, w, hgt, r) {
  r = Math.min(r, w / 2, hgt);
  return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
}

function hourlyChart(container, data) {
  const W = Math.max(container.clientWidth, 320), H = 220;
  const m = { l: 34, r: 6, t: 10, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = niceMax(Math.max(...data.map(d => d.received), 0));
  const band = iw / data.length;
  const bw = Math.max(2, Math.min(28, band - 2)); // 2px surface gap between bars
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Requests received per hour over the last 24 hours, peak ${Math.max(...data.map(d => d.received))}` });
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i, y = m.t + ih - (v / max) * ih;
    if (i > 0) svg.append(s("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: "gridline" }));
    const t = s("text", { x: m.l - 6, y: y + 4, "text-anchor": "end", class: "axis-text" });
    t.textContent = fmtNum(v);
    svg.append(t);
  }
  data.forEach((d, i) => {
    const x = m.l + i * band + (band - bw) / 2;
    const bh = (d.received / max) * ih;
    const hour = new Date(d.hour);
    const hit = s("rect", { x: m.l + i * band, y: m.t, width: band, height: ih, class: "bar-hit" });
    let bar = null;
    if (d.received > 0) bar = s("path", { d: roundedTopBar(x, m.t + ih - bh, bw, bh, 4), class: "bar" });
    const label = `${hour.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} – ${new Date(hour.getTime() + 3600e3).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    hit.addEventListener("mousemove", e => {
      bar && bar.classList.add("active");
      showTip(e, label, [["Received", fmtNum(d.received)], ["Completed", fmtNum(d.completed)], ["Failed", fmtNum(d.failed)]]);
    });
    hit.addEventListener("mouseleave", () => { bar && bar.classList.remove("active"); hideTip(); });
    svg.append(hit);
    if (bar) svg.append(bar);
    if (i % 6 === 0 || i === data.length - 1) {
      const t = s("text", { x: m.l + i * band + band / 2, y: H - 8, "text-anchor": "middle", class: "axis-text" });
      t.textContent = i === data.length - 1 ? "now" : hour.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      svg.append(t);
    }
  });
  svg.append(s("line", { x1: m.l, x2: W - m.r, y1: m.t + ih, y2: m.t + ih, class: "baseline" }));
  // hit rects must sit above bars for hover; re-append them last
  svg.querySelectorAll(".bar-hit").forEach(r => svg.append(r));
  replace(container, svg);
}

function statusChart(container, statuses) {
  const order = ["COMPLETED", "IN_PROGRESS", "PENDING", "FAILED", "CANCELLED"];
  const byStatus = Object.fromEntries(statuses.map(x => [x.status, Number(x.count)]));
  const total = Object.values(byStatus).reduce((a, b) => a + b, 0);
  const max = Math.max(...Object.values(byStatus), 1);
  replace(container, h("div", { class: "status-rows" }, order.map(st => {
    const n = byStatus[st] || 0;
    const fill = h("div", { class: "status-fill" });
    fill.style.width = `${(n / max) * 100}%`;
    fill.style.background = STATUS_COLOR[st];
    const row = h("div", { class: "status-row" }, badge(st), h("div", { class: "status-track" }, fill),
      h("div", { class: "status-count", text: fmtNum(n) }));
    row.addEventListener("mousemove", e => showTip(e, LABELS[st],
      [["Requests", fmtNum(n)], ["Share", total ? `${((n / total) * 100).toFixed(1)}%` : "–"]]));
    row.addEventListener("mouseleave", hideTip);
    return row;
  })), h("p", { class: "muted small", text: `${fmtNum(total)} requests stored in total` }));
}

/* ================================================================ dashboard */
function kpi(label, value, sub) {
  return h("div", { class: "kpi" }, h("div", { class: "kpi-label", text: label }),
    h("div", { class: "kpi-value", text: value }), h("div", { class: "kpi-sub", text: sub || "\u00a0" }));
}

async function loadDashboard() {
  const [{ body: o }, { body: recent }] = await Promise.all([api("/api/overview"), api("/api/requests?limit=8")]);
  const k = o.kpis, q = o.queue;
  const counts = Object.fromEntries(o.statuses.map(x => [x.status, Number(x.count)]));
  const svc = Object.fromEntries(o.services.map(x => [x.name, x]));
  const app2 = svc["App2"], stats = app2.extra || {};

  const node = (title, ok, meta) => h("div", { class: "flow-node" },
    h("div", { class: "flow-title" }, h("span", { text: title }), badge(ok ? "ok" : "down")),
    meta.map(t => h("div", { class: "flow-meta", text: t, title: t })));
  replace("#flow",
    node("App1", svc["App1 (mock)"].ok, ["Sends requests", `${fmtNum(q.callbacks_delivered)} callbacks received`]),
    node("Integration Layer", svc["Integration Layer"].ok, ["Validates, stores, delivers", `${fmtNum(k.total)} requests in 24 h`]),
    node("PostgreSQL", true, ["Queue + metadata + DWH", `${fmtNum(counts.PENDING)} pending, ${fmtNum(counts.IN_PROGRESS)} in progress`]),
    node("App2", app2.ok, [stats.running ? "Worker running" : "Worker stopped",
      `${fmtNum(stats.inflight ?? 0)} of ${fmtNum(stats.max_concurrency ?? 0)} slots busy`]));

  const finished = Number(k.completed) + Number(k.failed) + Number(k.cancelled);
  const oldest = fmtAgo(q.oldest_pending_s);
  replace("#kpis",
    kpi("Requests, 24 h", fmtNum(k.total), `${fmtNum(k.retried)} needed a retry`),
    kpi("Success rate", finished ? `${((k.completed / finished) * 100).toFixed(1)}%` : "–", `of ${fmtNum(finished)} finished requests`),
    kpi("In flight", fmtNum(k.in_flight), oldest ? `oldest pending ${oldest}` : "queue is empty"),
    kpi("Failed, 24 h", fmtNum(k.failed), `${fmtNum(k.cancelled)} cancelled`),
    kpi("Median end-to-end", fmtMs(k.p50_ms), `p95 ${fmtMs(k.p95_ms)}`),
    kpi("Callbacks delivered", fmtNum(q.callbacks_delivered), `${fmtNum(q.callbacks_pending)} pending, ${fmtNum(q.callbacks_failed)} failed`));

  hourlyChart($("#hourly-chart"), o.hourly.map(d => ({ ...d, received: Number(d.received) })));
  statusChart($("#status-chart"), o.statuses);
  replace("#recent-table", table(REQUEST_COLUMNS, recent, { onRow: r => openDrawer(r.request_id),
    empty: "No requests yet. Use “Send request” to create one." }));
}

/* ================================================================ requests tab */
async function loadRequests() {
  const params = new URLSearchParams({ limit: 200 });
  const st = $("#r-status").value, wf = $("#r-workflow").value, q = $("#r-search").value.trim();
  if (st) params.set("status", st);
  if (wf) params.set("workflow", wf);
  if (q) params.set("q", q);
  const { body } = await api(`/api/requests?${params}`);
  $("#r-count").textContent = `${body.length} shown`;
  replace("#requests-table", table(REQUEST_COLUMNS, body, { onRow: r => openDrawer(r.request_id),
    empty: "No requests match the filters." }));
}

/* ================================================================ progress steps */
function progressSteps(r) {
  const failed = r.status === "FAILED" || r.status === "CANCELLED";
  const steps = [
    { title: "Received by Integration Layer", sub: `validated against schema v${r.input_schema_version ?? "?"}`, time: r.received_at, done: true },
    { title: "Picked up by App2", sub: r.app2_worker_id ? `worker ${r.app2_worker_id}` + (r.retry_count ? `, attempt ${r.retry_count + 1}` : "") : (r.status === "CANCELLED" ? "cancelled before pickup" : "waiting in queue"),
      time: r.picked_at, done: !!r.picked_at, active: r.status === "PENDING" && !r.picked_at },
    { title: failed ? (r.status === "CANCELLED" ? "Cancelled" : "Failed") : "Workflow completed",
      sub: r.status === "PENDING" && r.error_code ? `retry scheduled after ${r.error_code}` : (r.workflow_status || (r.status === "IN_PROGRESS" ? "processing" : "")),
      time: r.workflow_completed_at || (r.status === "CANCELLED" ? r.updated_at : null),
      done: r.status === "COMPLETED", fail: failed, active: r.status === "IN_PROGRESS" || (r.status === "PENDING" && !!r.picked_at) },
  ];
  if (r.callback_url) {
    steps.push({ title: "Callback delivered to App1",
      sub: r.callback_status ? `${LABELS[r.callback_status] || r.callback_status}, ${r.callback_attempts} attempt(s)` + (r.callback_last_error ? `: ${r.callback_last_error}` : "") : "after completion",
      time: r.callback_delivered_at, done: r.callback_status === "DELIVERED", fail: r.callback_status === "FAILED",
      active: r.callback_status === "PENDING" });
  } else {
    steps.push({ title: "Result available by polling", sub: "GET /v1/requests/{id}", time: r.workflow_completed_at, done: TERMINAL.has(r.status) });
  }
  const check = () => { const i = s("svg", { viewBox: "0 0 16 16" }); i.append(s("path", { d: ICONS.COMPLETED })); return i; };
  const cross = () => { const i = s("svg", { viewBox: "0 0 16 16" }); i.append(s("path", { d: ICONS.FAILED })); return i; };
  return h("ol", { class: "steps" }, steps.map(st => h("li", {
      class: `step ${st.done ? "done" : ""} ${st.fail ? "fail" : ""} ${st.active ? "active" : ""}` },
    h("span", { class: "step-dot" }, st.done ? check() : st.fail ? cross() : null),
    h("div", {}, h("div", { class: "step-title", text: st.title }), h("div", { class: "step-sub", text: st.sub })),
    h("div", { class: "step-time", text: st.time ? fmtTime(st.time) : "" }))));
}

/* ================================================================ drawer */
let drawerId = null;
async function openDrawer(id) {
  drawerId = id;
  $("#drawer-title").textContent = id;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-backdrop").hidden = false;
  replace("#drawer-body", h("div", { class: "muted", text: "Loading…" }));
  await refreshDrawer();
}
function closeDrawer() {
  drawerId = null;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawer-backdrop").hidden = true;
}

async function requestAction(id, action) {
  const { status, body } = await api(`/api/requests/${id}/${action}`, { method: "POST", allowError: true });
  toast(status < 300 ? `Request ${action === "cancel" ? "cancelled" : "re-queued"}` : (body?.error?.message || `Failed: HTTP ${status}`));
  refreshDrawer();
  refreshCurrent();
}

async function refreshDrawer() {
  if (!drawerId) return;
  const id = drawerId;
  let data;
  try { ({ body: data } = await api(`/api/requests/${id}`)); } catch (e) {
    replace("#drawer-body", h("div", { class: "empty", text: `Could not load request (${e.message}).` }));
    return;
  }
  if (id !== drawerId) return;
  const r = data.request;
  const kv = (pairs) => h("dl", { class: "kv" }, pairs.map(([k, v]) => h("div", {}, h("dt", { text: k }), h("dd", {}, v ?? "–"))));
  const actions = [];
  if (r.status === "PENDING") actions.push(h("button", { class: "btn danger", onclick: () => requestAction(id, "cancel"), text: "Cancel request" }));
  if (r.status === "FAILED") actions.push(h("button", { class: "btn", onclick: () => requestAction(id, "replay"), text: "Replay (admin)" }));

  replace("#drawer-body",
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", { class: "result-head" }, badge(r.status),
        r.workflow_status ? h("span", { class: "muted", text: `workflow status: ${r.workflow_status}` }) : null),
        actions.length ? h("div", { class: "actions" }, actions) : null),
      progressSteps(r)),
    h("div", { class: "card" }, h("h3", { text: "Metadata" }), kv([
      ["Workflow", r.workflow_name], ["Success flag", r.success_flag === null ? "in flight" : String(r.success_flag)],
      ["Source → target", `${r.source_app} → ${r.target_app}`], ["Retries", `${r.retry_count} of ${r.max_retries}`],
      ["Correlation id", h("span", { class: "mono", text: r.correlation_id })], ["Idempotency key", r.idempotency_key],
      ["Received", fmtTime(r.received_at, true)], ["Picked up", fmtTime(r.picked_at, true)],
      ["Completed", fmtTime(r.workflow_completed_at, true)], ["Processing time", fmtMs(r.processing_duration_ms)],
      ["API call", `${r.http_method} ${r.api_url}`], ["Client IP", r.client_ip],
      ["IL server", `${r.il_server_hostname ?? "–"} (${r.il_server_ip ?? "–"})`], ["IL instance", r.il_instance_id],
      ["App2 worker", r.app2_worker_id ? `${r.app2_worker_id} on ${r.app2_worker_host}` : null], ["DB server", r.db_server_addr],
      ["Schema version", r.input_schema_version], ["Priority", r.priority],
      ["Error", r.error_code ? `${r.error_code}: ${r.error_message ?? ""}` : null],
      ["Callback", r.callback_url ? `${r.callback_status ?? "waiting"} → ${r.callback_url}` : "not requested (polling)"],
    ])),
    h("div", { class: "card" }, h("h3", { text: "Input JSON" }), jsonView(r.input_json),
      h("h3", { text: "Output JSON" }), jsonView(r.output_json)),
    h("div", { class: "card" }, h("h3", { text: "Status history (request_event_log)" }),
      h("ul", { class: "timeline" }, data.events.map(e => h("li", {},
        h("span", { class: "muted small", text: fmtTime(e.event_at) }),
        h("span", {}, badge(e.new_status), " ", h("span", { class: "muted small",
          text: `by ${e.actor}` + (e.error_code ? ` · ${e.error_code}` : "") })))))),
    h("div", { class: "card" }, h("h3", { text: "API calls (api_call_log)" }),
      table([
        { label: "Time", render: c => fmtTime(c.called_at) },
        { label: "Dir", render: c => c.direction === "INBOUND" ? "IN" : "OUT" },
        { label: "Method", render: c => c.http_method },
        { label: "Endpoint", cls: "trunc", render: c => h("span", { class: "mono", title: c.api_url, text: c.endpoint || c.api_url }) },
        { label: "Status", num: true, render: c => c.response_status_code ?? "error" },
        { label: "Latency", num: true, render: c => fmtMs(c.latency_ms) },
      ], data.api_calls, { empty: "No API calls logged yet." })));
}

/* ================================================================ send tab */
let workflowsCache = [];
const rand = () => Math.random().toString(36).slice(2, 8).toUpperCase();
const SAMPLES = {
  order_validation: [
    ["Approved order", () => ({ order_id: `ORD-${rand()}`, customer_id: "CUST-1001", currency: "EUR", order_date: new Date().toISOString().slice(0, 10),
      items: [{ sku: "SKU-100", quantity: 2, unit_price: 49.5 }, { sku: "SKU-200", quantity: 1, unit_price: 120 }] })],
    ["Rejected (over credit limit)", () => ({ order_id: `ORD-${rand()}`, customer_id: "CUST-2002", currency: "USD",
      items: [{ sku: "SKU-900", quantity: 1200, unit_price: 75 }] })],
    ["Invalid payload (422)", () => ({ order_id: "", currency: "euro", items: [{ sku: "SKU-1", quantity: 0, unit_price: -5 }], note: "extra field" })],
    ["Downstream outage (retries)", () => ({ order_id: `ORD-${rand()}`, customer_id: "CUST-DOWNSTREAM-DOWN", currency: "EUR",
      items: [{ sku: "SKU-100", quantity: 1, unit_price: 10 }] })],
  ],
};

async function loadWorkflowsCache() {
  const { body } = await api("/api/workflows");
  workflowsCache = body;
  const sel = $("#f-workflow"), filter = $("#r-workflow");
  const current = sel.value;
  replace(sel, body.filter(w => w.is_active).map(w => h("option", { value: w.workflow_name, text: w.workflow_name })));
  if (current) sel.value = current;
  const fcur = filter.value;
  replace(filter, h("option", { value: "", text: "All workflows" }), body.map(w => h("option", { value: w.workflow_name, text: w.workflow_name })));
  filter.value = fcur;
  renderSamples();
}

function renderSamples() {
  const wf = $("#f-workflow").value;
  const samples = SAMPLES[wf] || [];
  replace("#samples", samples.length ? samples.map(([label, make]) => h("button", { type: "button", class: "chip",
    onclick: () => { $("#f-payload").value = JSON.stringify(make(), null, 2); $("#payload-error").hidden = true; }, text: label }))
    : h("span", { class: "muted small", text: "No samples for this workflow; see its schema on the Workflows tab." }));
  if (!$("#f-payload").value.trim() && samples.length) $("#f-payload").value = JSON.stringify(samples[0][1](), null, 2);
}

let trackTimer = null;
async function sendRequest(evt) {
  evt.preventDefault();
  const errBox = $("#payload-error");
  let payload;
  try { payload = JSON.parse($("#f-payload").value); errBox.hidden = true; } catch (e) {
    errBox.textContent = `Not valid JSON: ${e.message}`; errBox.hidden = false; return;
  }
  const btn = $("#send-btn");
  btn.disabled = true;
  try {
    const body = { workflow_name: $("#f-workflow").value, payload, use_callback: $("#f-callback").checked,
      priority: Number($("#f-priority").value), idempotency_key: $("#f-idem").value.trim() || null };
    const res = await api("/api/requests", { method: "POST", body: JSON.stringify(body), allowError: true });
    renderSendResult(res);
  } catch (e) {
    replace("#send-result", h("div", { class: "field-error", text: `Request failed: ${e.message}` }));
  } finally {
    btn.disabled = false;
  }
}

function renderSendResult({ status, body }) {
  clearInterval(trackTimer);
  const ok = status < 300;
  const head = h("div", { class: "result-head" }, h("span", { class: `http ${ok ? "ok" : "err"}`, text: `HTTP ${status}` }),
    ok ? h("span", { text: body.is_duplicate ? "Duplicate idempotency key: original request returned" : "Accepted and stored" })
       : h("span", { text: body?.error?.message || body?.detail || "Rejected" }));
  if (!ok) {
    const errs = body?.error?.errors || [];
    replace("#send-result", head,
      body?.error?.code ? h("p", {}, "Error code ", h("code", { text: body.error.code }),
        body.error.schema_version ? ` (schema v${body.error.schema_version})` : "") : null,
      errs.length ? h("ul", { class: "err-list" }, errs.map(e => h("li", {}, h("code", { text: e.path }), " ", e.message))) : null,
      h("h3", { text: "Response body" }), jsonView(body));
    return;
  }
  const tracker = h("div", {}, h("p", { class: "muted", text: "Following the request…" }));
  replace("#send-result", head, h("p", { class: "small" }, "Request id ", h("code", { text: body.request_id })),
    tracker, h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => openDrawer(body.request_id), text: "Open full details" })));
  const started = Date.now();
  const poll = async () => {
    try {
      const { body: d } = await api(`/api/requests/${body.request_id}`);
      const r = d.request;
      replace(tracker, progressSteps(r), TERMINAL.has(r.status) ? h("div", {}, h("h3", { text: "Output JSON" }), jsonView(r.output_json)) : null);
      const callbackDone = !r.callback_url || ["DELIVERED", "FAILED"].includes(r.callback_status);
      if ((TERMINAL.has(r.status) && callbackDone) || Date.now() - started > 90000) clearInterval(trackTimer);
    } catch { /* keep polling */ }
  };
  poll();
  trackTimer = setInterval(poll, 1000);
}

/* ================================================================ callbacks, api log, workflows, reports */
async function loadCallbacks() {
  const { body } = await api("/api/callbacks");
  replace("#callbacks-table", table([
    { label: "Received at App1", render: c => fmtTime(c._received_at) },
    { label: "Request", render: c => h("span", { class: "mono", text: shortId(c.request_id) }) },
    { label: "Event", render: c => h("span", { class: "mono", text: c.event }) },
    { label: "Status", render: c => badge(c.status) },
    { label: "Workflow status", render: c => c.workflow_status },
    { label: "Attempt", num: true, render: c => c.attempt },
    { label: "Error", render: c => c.error ? h("span", { class: "mono", text: c.error.code }) : null },
  ], body, { onRow: c => openDrawer(c.request_id), empty: "No callbacks received yet. Send a request with callback enabled." }));
}

async function loadApiLog() {
  const { body } = await api("/api/api-calls");
  replace("#apilog-table", table([
    { label: "Time", render: c => fmtTime(c.called_at) },
    { label: "Direction", render: c => c.direction === "INBOUND" ? "IN  (App1 → IL)" : "OUT (IL → App1)" },
    { label: "Method", render: c => h("span", { class: "mono", text: c.http_method }) },
    { label: "Endpoint", cls: "trunc", render: c => h("span", { class: "mono", title: c.api_url, text: c.endpoint || c.api_url }) },
    { label: "Status", num: true, render: c => c.response_status_code ?? "error" },
    { label: "Latency", num: true, render: c => fmtMs(c.latency_ms) },
    { label: "Client", render: c => c.client_ip },
    { label: "Request", render: c => c.request_id ? h("span", { class: "mono", text: shortId(c.request_id) }) : h("span", { class: "muted", text: "none" }) },
  ], body, { onRow: c => c.request_id && openDrawer(c.request_id), empty: "No API calls logged yet." }));
}

async function loadWorkflows() {
  await loadWorkflowsCache();
  replace("#workflows", workflowsCache.map(w => h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", { class: "mono", text: w.workflow_name }),
      w.is_active ? badge("ok", "Active") : badge("CANCELLED", "Inactive")),
    w.description ? h("p", { class: "muted", text: w.description }) : null,
    h("dl", { class: "kv" }, [["Target app", w.target_app], ["Schema version", w.schema_version],
      ["Max retries", w.max_retries], ["Updated", fmtTime(w.updated_at, true)]]
      .map(([k, v]) => h("div", {}, h("dt", { text: k }), h("dd", { text: String(v) })))),
    h("div", { class: "grid-send" },
      h("div", {}, h("h3", { text: "Input schema" }), jsonView(w.input_schema)),
      h("div", {}, h("h3", { text: "Output schema" }), jsonView(w.output_schema))))));
}

async function loadReports() {
  const { body } = await api("/api/reports");
  const wm = body.watermark;
  $("#watermark").textContent = wm?.last_run_at ? `DWH last loaded ${fmtTime(wm.last_run_at, true)}` : "DWH not loaded yet";
  replace("#reports-table", table([
    { label: "Date", render: r => new Date(r.full_date + "T00:00:00").toLocaleDateString() },
    { label: "Workflow", render: r => r.workflow_name },
    { label: "Total", num: true, render: r => fmtNum(r.total_requests) },
    { label: "Completed", num: true, render: r => fmtNum(r.completed) },
    { label: "Failed", num: true, render: r => fmtNum(r.failed) },
    { label: "In flight", num: true, render: r => fmtNum(r.in_flight) },
    { label: "Success rate", num: true, render: r => r.success_rate_pct === null ? "–" : `${r.success_rate_pct}%` },
    { label: "p50 end-to-end", num: true, render: r => fmtMs(r.p50_end_to_end_ms) },
    { label: "p95 end-to-end", num: true, render: r => fmtMs(r.p95_end_to_end_ms) },
    { label: "Retries", num: true, render: r => fmtNum(r.total_retries) },
  ], body.daily, { empty: "The warehouse has no rows yet; the scheduler loads it every 15 minutes." }));
}

/* ================================================================ tabs & refresh loop */
const LOADERS = { dashboard: loadDashboard, requests: loadRequests, callbacks: loadCallbacks, apilog: loadApiLog,
  workflows: loadWorkflows, reports: loadReports };
const AUTO_REFRESH = new Set(["dashboard", "requests", "callbacks", "apilog"]);
let currentTab = "dashboard";

async function refreshCurrent() {
  const loader = LOADERS[currentTab];
  const state = $("#refresh-state");
  try {
    if (loader) await loader();
    state.classList.remove("stale");
    $("#refresh-text").textContent = AUTO_REFRESH.has(currentTab) ? `Live · ${new Date().toLocaleTimeString()}` : "Up to date";
  } catch (e) {
    state.classList.add("stale");
    $("#refresh-text").textContent = "Connection problem";
    console.error(e);
  }
}

function selectTab(name) {
  currentTab = name;
  document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab-panel").forEach(p => { p.hidden = p.id !== `tab-${name}`; });
  try { localStorage.setItem("il-console-tab", name); } catch { /* storage unavailable */ }
  history.replaceState(null, "", `#${name}`);
  refreshCurrent();
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("il-console-theme"); } catch { /* ignore */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("il-console-theme", next); } catch { /* ignore */ }
    if (currentTab === "dashboard") refreshCurrent();
  });
}

function init() {
  initTheme();
  document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  document.querySelectorAll("[data-goto]").forEach(b => b.addEventListener("click", () => selectTab(b.dataset.goto)));
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
  $("#send-form").addEventListener("submit", sendRequest);
  $("#f-workflow").addEventListener("change", () => { $("#f-payload").value = ""; renderSamples(); });
  $("#format-btn").addEventListener("click", () => {
    try { $("#f-payload").value = JSON.stringify(JSON.parse($("#f-payload").value), null, 2); $("#payload-error").hidden = true; }
    catch (e) { $("#payload-error").textContent = `Not valid JSON: ${e.message}`; $("#payload-error").hidden = false; }
  });
  let searchTimer;
  $("#r-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadRequests, 300); });
  $("#r-status").addEventListener("change", loadRequests);
  $("#r-workflow").addEventListener("change", loadRequests);
  window.addEventListener("resize", () => { if (currentTab === "dashboard") refreshCurrent(); });

  loadWorkflowsCache().catch(console.error);
  let start = location.hash.slice(1);
  if (!start) {
    try { start = localStorage.getItem("il-console-tab") || "dashboard"; } catch { start = "dashboard"; }
  }
  selectTab(LOADERS[start] || start === "send" ? start : "dashboard");

  setInterval(() => {
    if (document.hidden) return;
    if (AUTO_REFRESH.has(currentTab)) refreshCurrent();
    if (drawerId) refreshDrawer();
  }, REFRESH_MS);
}

init();
