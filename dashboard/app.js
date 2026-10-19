"use strict";

const WINDOWS = ["15m", "1h", "24h", "7d", "all"];
const FEED_LIMIT = 50;
const FEED_SESSIONS = 10;
const FEED_DEBOUNCE_MS = 300;
const STATS_DEBOUNCE_MS = 500;
const RECONNECT_MS = 2000;
const EVENT_TYPES = ["turn", "decision", "request", "compaction"];

// Live data redraws charts often; animating every refresh flickers and leaves half-drawn frames.
Chart.defaults.animation = false;

const state = {
  window: "all",
  mode: null,
  stats: null,
  sessions: new Map(),
  turns: new Map(),
  engine: null,
  filter: "",
};
const charts = {};

const $ = (id) => document.getElementById(id);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child);
  }
  return node;
}

function setText(id, value) {
  $(id).textContent = String(value);
}

function usd(value) {
  if (value === null || value === undefined) return "n/a";
  const abs = Math.abs(value);
  const text = abs !== 0 && abs < 0.01 ? abs.toFixed(4) : abs.toFixed(2);
  return (value < 0 ? "-$" : "$") + text;
}

function pct(ratio) {
  return ratio === null || ratio === undefined ? "n/a" : (ratio * 100).toFixed(0) + "%";
}

function ms(value) {
  if (value === null || value === undefined) return "n/a";
  return value >= 1000 ? (value / 1000).toFixed(2) + " s" : value.toFixed(0) + " ms";
}

function shortModel(model) {
  return model ? String(model).replace(/^claude-/, "") : "n/a";
}

function basename(path) {
  if (!path) return "unknown";
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path);
}

function snippet(text, max) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max - 1) + "…" : flat;
}

function clock(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const same = when.toDateString() === new Date().toDateString();
  const hms = when.toLocaleTimeString([], { hour12: false });
  return same ? hms : when.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + hms.slice(0, 5);
}

async function getJSON(url) {
  const res = await fetch(url, { headers: { accept: "application/json" } });
  const body = await res.json().catch(() => null);
  if (!res.ok && url !== "/healthz") throw new Error((body && body.error) || res.statusText);
  return body;
}

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

/* ---------- theme ---------- */

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("smartrouter-theme", theme);
  } catch (e) {
    /* storage unavailable */
  }
  for (const key of Object.keys(charts)) {
    charts[key].destroy();
    delete charts[key];
  }
  if (state.stats) renderStats(state.stats);
}

/* ---------- charts ---------- */

function cssVar(node, name) {
  return getComputedStyle(node).getPropertyValue(name).trim();
}

function chartColors(canvas) {
  return {
    fg: cssVar(canvas, "--fg"),
    muted: cssVar(canvas, "--muted"),
    border: cssVar(canvas, "--border"),
    accent: cssVar(canvas, "--accent"),
    series: ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"].map((name) => cssVar(canvas, name)),
  };
}

function axes(colors, extra) {
  return {
    x: { ticks: { color: colors.muted, maxRotation: 0, autoSkip: true }, grid: { display: false }, border: { color: colors.border } },
    y: { beginAtZero: true, ticks: { color: colors.muted }, grid: { color: colors.border }, border: { display: false }, ...extra },
  };
}

function upsertChart(key, canvasId, build, rows) {
  const canvas = $(canvasId);
  const wrap = canvas.parentElement;
  const empty = wrap.querySelector(".empty");
  const hasData = rows.length > 0;
  empty.hidden = hasData;
  canvas.style.visibility = hasData ? "visible" : "hidden";
  if (!hasData) {
    if (charts[key]) {
      charts[key].destroy();
      delete charts[key];
    }
    return;
  }
  const colors = chartColors(canvas);
  const config = build(colors);
  if (charts[key]) {
    charts[key].data = config.data;
    charts[key].options = config.options;
    charts[key].update("none");
  } else {
    charts[key] = new Chart(canvas, config);
  }
}

function bucketLabel(bucket, hourly) {
  const when = new Date(bucket);
  return hourly
    ? when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
    : when.toLocaleDateString([], { month: "short", day: "numeric" });
}

function isHourly(windowName) {
  return windowName === "15m" || windowName === "1h" || windowName === "24h";
}

function cumulative(series) {
  let running = 0;
  return series.map((value) => (running += value));
}

/* ---------- stats rendering ---------- */

function renderHero(s) {
  const a = $("headline-a");
  const b = $("headline-b");
  if (!s.requests) {
    a.textContent = "Waiting for the first routed turn";
    b.textContent = "";
  } else if (state.mode === "off") {
    a.textContent = "Routing is off";
    b.textContent = "recording " + usd(s.without_router_usd) + " of Claude Code traffic";
  } else {
    const active = state.mode === "active";
    const verb = s.saving_usd >= 0 ? (active ? "saved " : "would have saved ") : (active ? "cost " : "would have cost ");
    const amount = usd(Math.abs(s.saving_usd)) + (s.saving_usd >= 0 ? "" : " more");
    a.textContent = "Routing " + verb + amount;
    b.textContent = "on " + usd(s.without_router_usd) + " of Claude Code traffic";
  }
  const sub = $("hero-sub");
  sub.replaceChildren();
  const mode = state.mode || "unknown";
  const behaviour =
    mode === "active"
      ? " applies the router's choice to each request."
      : mode === "off"
        ? " passes every request straight through."
        : " records what the router would do and never changes a request.";
  const engine = state.engine ? " Engine " + state.engine + "." : "";
  sub.append(
    el("b", { text: mode + " mode" }),
    behaviour + engine + " ",
    el("b", { text: "Prompts never leave this machine except to the decision engine." }),
  );
}

function renderTiles(s) {
  setText("tile-sessions", s.sessions);
  setText("tile-turns", s.turns);
  setText("tile-requests", s.requests);
  setText("tile-cache", pct(s.cache_hit_ratio));
  setText("tile-switches", s.switches);
  setText("tile-timeouts", s.timeouts);
  setText("nav-sessions", s.sessions);
  setText("nav-switches", s.switches);
  setText("nav-ledger", s.switch_ledger.length);
  setText("nav-mix", s.by_model.length);
  setText("nav-quality", Object.values(s.decisions_by_rule).reduce((n, c) => n + c, 0));
}

function renderSpend(s) {
  setText("spend-without", usd(s.without_router_usd));
  setText("spend-with", usd(s.with_router_usd));
  setText("spend-delta", usd(s.saving_usd));
  setText("spend-saving", s.requests ? s.requests + " requests" : "");
  const hourly = isHourly(s.window);
  const labels = s.timeseries.map((p) => bucketLabel(p.bucket, hourly));
  upsertChart("spend", "chart-spend", (c) => ({
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Without router", data: cumulative(s.timeseries.map((p) => p.without_router_usd)), borderColor: c.muted, backgroundColor: c.muted, borderDash: [5, 4], pointRadius: 2, tension: 0.25 },
        { label: "With router", data: cumulative(s.timeseries.map((p) => p.with_router_usd)), borderColor: c.accent, backgroundColor: c.accent, pointRadius: 2, tension: 0.25 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: c.fg, boxWidth: 12, usePointStyle: true } },
        tooltip: { callbacks: { label: (ctx) => ctx.dataset.label + ": " + usd(ctx.parsed.y) } },
      },
      scales: axes(c, { ticks: { color: c.muted, callback: (v) => usd(v) } }),
    },
  }), s.timeseries);
}

function renderLedger(s) {
  const body = $("ledger-body");
  body.replaceChildren();
  const rows = [...s.switch_ledger].sort((x, y) => String(y.created_at).localeCompare(String(x.created_at)));
  $("ledger-empty").hidden = rows.length > 0;
  $("ledger-table").hidden = rows.length === 0;
  for (const row of rows) {
    const realised = row.realised_saving_usd;
    body.append(
      el("tr", {},
        el("td", { text: "#" + row.seq + " " + String(row.session_id).slice(0, 6) }),
        el("td", {}, el("span", { text: shortModel(row.from_model) }), el("span", { class: "arrow", text: " → " }), el("span", { text: shortModel(row.to_model) })),
        el("td", { class: "num", text: usd(row.est_switch_tax_usd) }),
        el("td", { class: "num", text: usd(row.est_saving_usd) }),
        el("td", { class: "num " + (realised > 0 ? "pos" : realised < 0 ? "neg" : ""), text: usd(realised) }),
      ),
    );
  }
}

function renderCache(s) {
  upsertChart("cacheRatio", "chart-cache-ratio", (c) => ({
    type: "bar",
    data: {
      labels: s.by_model.map((m) => shortModel(m.model)),
      datasets: [{ label: "Cache hit", data: s.by_model.map((m) => m.cache_hit_ratio * 100), backgroundColor: c.series[1], borderRadius: 6, maxBarThickness: 48 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => ctx.parsed.y.toFixed(0) + "%" } } },
      scales: axes(c, { max: 100, ticks: { color: c.muted, callback: (v) => v + "%" } }),
    },
  }), s.by_model);
  const hourly = isHourly(s.window);
  const writes = s.timeseries.filter((p) => p.cache_write_tokens > 0);
  upsertChart("cacheWrites", "chart-cache-writes", (c) => ({
    type: "bar",
    data: {
      labels: s.timeseries.map((p) => bucketLabel(p.bucket, hourly)),
      datasets: [{ label: "Cache write tokens", data: s.timeseries.map((p) => p.cache_write_tokens), backgroundColor: c.series[2], borderRadius: 6, maxBarThickness: 48 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: axes(c, {}),
    },
  }), writes);
}

function renderMix(s) {
  upsertChart("mix", "chart-mix", (c) => ({
    type: "doughnut",
    data: {
      labels: s.by_model.map((m) => shortModel(m.model)),
      datasets: [{ data: s.by_model.map((m) => m.spend_usd), backgroundColor: s.by_model.map((_, i) => c.series[(i + 1) % c.series.length]), borderColor: c.border, borderWidth: 2 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      plugins: {
        legend: { position: "right", labels: { color: c.fg, boxWidth: 12, usePointStyle: true } },
        tooltip: { callbacks: { label: (ctx) => ctx.label + ": " + usd(ctx.parsed) } },
      },
    },
  }), s.by_model);
  const body = $("mix-body");
  body.replaceChildren();
  for (const m of s.by_model) {
    body.append(el("tr", {}, el("td", { text: shortModel(m.model) }), el("td", { class: "num", text: m.turns }), el("td", { class: "num", text: m.requests })));
  }
}

function renderQuality(s) {
  setText("q-p50", ms(s.engine_latency_ms.p50));
  setText("q-p95", ms(s.engine_latency_ms.p95));
  setText("q-low", s.low_confidence);
  setText("q-timeouts", s.timeouts);
  const bins = s.tier_probability_histogram;
  const labels = bins.map((_, i) => i * 10 + "–" + (i + 1) * 10 + "%");
  upsertChart("quality", "chart-quality", (c) => ({
    type: "bar",
    data: { labels, datasets: [{ label: "Decisions", data: bins, backgroundColor: c.series[3], borderRadius: 4 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { ...axes(c, { ticks: { color: c.muted, precision: 0 } }), x: { ...axes(c, {}).x, ticks: { color: c.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 5 } } },
    },
  }), bins.some((n) => n > 0) ? bins : []);
  const rules = $("q-rules");
  rules.replaceChildren();
  for (const [rule, count] of Object.entries(s.decisions_by_rule).sort((a, b) => b[1] - a[1])) {
    rules.append(el("span", { class: "badge" }, rule, el("b", { text: count })));
  }
}

function renderStats(s) {
  renderHero(s);
  renderTiles(s);
  renderSpend(s);
  renderLedger(s);
  renderCache(s);
  renderMix(s);
  renderQuality(s);
}

async function refreshStats() {
  try {
    state.stats = await getJSON("/api/stats?window=" + encodeURIComponent(state.window));
    renderStats(state.stats);
  } catch (e) {
    /* keep the last good render */
  }
}
const scheduleStats = debounce(refreshStats, STATS_DEBOUNCE_MS);

/* ---------- feed ---------- */

function mainRequest(turn) {
  return turn.requests.find((r) => r.kind === "main") || turn.requests[0] || null;
}

function routingAnswer(decision) {
  if (!decision) return null;
  const answers = Object.values(decision.answers || {});
  const choice = answers.length && answers[0] ? answers[0].choice : null;
  if (choice === null) return null;
  return decision.tier_probability === null || decision.tier_probability === undefined
    ? String(choice)
    : choice + " " + pct(decision.tier_probability);
}

function feedRow(item) {
  const { turn, session } = item;
  const decision = turn.decision;
  const req = mainRequest(turn);
  if (decision && decision.engine) state.engine = decision.engine;
  const tokens = turn.requests.reduce(
    (n, r) => n + (r.input_tokens || 0) + (r.output_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_write_5m_tokens || 0) + (r.cache_write_1h_tokens || 0),
    0,
  );
  const answer = routingAnswer(decision);
  const meta = el("div", { class: "f-meta" });
  if (answer) meta.append(el("span", { text: answer }));
  if (decision) meta.append(el("span", { class: "badge", text: decision.rule }));
  if (decision) {
    meta.append(el("span", { text: "chose " + shortModel(decision.chosen_model) }));
  }
  if (req) meta.append(el("span", { text: "sent " + shortModel(req.sent_model) }));
  meta.append(el("span", { text: tokens.toLocaleString() + " tok" }));
  if (!decision) meta.append(el("span", { class: "badge", text: "no decision" }));
  const cost = el("div", { class: "f-cost", title: turn.requests.length + " request(s) in this turn" },
    el("b", { text: req ? usd(req.cost_usd) : "n/a" }),
    el("span", { text: "alt " + (req ? usd(req.counterfactual_cost_usd) : "n/a") }),
  );
  return el("div", { class: "feed-row" },
    el("div", { class: "f-time", text: clock(turn.created_at) }),
    el("div", { class: "f-prompt" },
      el("span", { class: "proj", text: basename(session && session.project_path) }),
      el("span", { class: "prompt-text", text: snippet(turn.prompt, 80) }),
    ),
    meta,
    cost,
  );
}

function renderFeed() {
  const needle = state.filter.trim().toLowerCase();
  const items = [];
  for (const [sid, turns] of state.turns) {
    const session = state.sessions.get(sid);
    for (const turn of turns) items.push({ turn, session });
  }
  items.sort((a, b) => String(b.turn.created_at).localeCompare(String(a.turn.created_at)));
  const visible = items.filter((i) => !needle || String(i.turn.prompt || "").toLowerCase().includes(needle)).slice(0, FEED_LIMIT);
  const feed = $("feed");
  feed.replaceChildren(...visible.map(feedRow));
  $("feed-empty").hidden = visible.length > 0;
  $("feed-empty").textContent = needle && items.length ? "No turns match the filter" : "No turns yet";
  setText("feed-meta", needle ? visible.length + " of " + items.length : items.length + " turns");
  setText("nav-feed", visible.length);
  if (state.stats) renderHero(state.stats);
}

async function refreshSessions() {
  const list = await getJSON("/api/sessions");
  state.sessions = new Map(list.map((s) => [s.id, s]));
  return list;
}

async function refreshTurns(sessionId) {
  try {
    state.turns.set(sessionId, await getJSON("/api/sessions/" + encodeURIComponent(sessionId) + "/turns"));
  } catch (e) {
    state.turns.delete(sessionId);
  }
}

async function loadFeed() {
  try {
    const list = await refreshSessions();
    const recent = list.slice(0, FEED_SESSIONS).map((s) => s.id);
    await Promise.all(recent.map(refreshTurns));
    renderFeed();
  } catch (e) {
    /* next event retries */
  }
}

const pendingSessions = new Set();
let pendingAll = false;
const flushFeed = debounce(async () => {
  const ids = [...pendingSessions];
  const all = pendingAll;
  pendingSessions.clear();
  pendingAll = false;
  try {
    const list = await refreshSessions();
    const targets = all ? list.slice(0, FEED_SESSIONS).map((s) => s.id) : ids;
    await Promise.all(targets.map(refreshTurns));
    renderFeed();
  } catch (e) {
    /* next event retries */
  }
}, FEED_DEBOUNCE_MS);

function onStreamEvent(message) {
  let data = null;
  try {
    data = JSON.parse(message.data);
  } catch (e) {
    /* payload unreadable, refresh everything */
  }
  if (data && data.session_id) pendingSessions.add(data.session_id);
  else pendingAll = true;
  flushFeed();
  scheduleStats();
}

/* ---------- live stream ---------- */

function setLive(on) {
  const dot = $("live");
  dot.classList.toggle("off", !on);
  dot.title = on ? "Stream connected" : "Stream disconnected";
  setText("live-label", on ? "live" : "offline");
}

function connectStream() {
  const source = new EventSource("/api/stream");
  source.onopen = () => {
    setLive(true);
    pendingAll = true;
    flushFeed();
    scheduleStats();
  };
  source.onerror = () => {
    setLive(false);
    if (source.readyState === EventSource.CLOSED) {
      setTimeout(connectStream, RECONNECT_MS);
    }
  };
  for (const type of EVENT_TYPES) source.addEventListener(type, onStreamEvent);
}

/* ---------- mode and window controls ---------- */

function renderMode() {
  for (const button of $("mode-switch").querySelectorAll("button")) {
    button.classList.toggle("on", button.dataset.mode === state.mode);
  }
}

async function loadMode() {
  try {
    state.mode = (await getJSON("/api/mode")).mode;
  } catch (e) {
    state.mode = null;
  }
  renderMode();
  if (state.stats) renderHero(state.stats);
}

async function putMode(mode) {
  const msg = $("mode-msg");
  msg.hidden = true;
  try {
    const res = await fetch("/api/mode", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      msg.textContent = body.error || "Mode change failed (" + res.status + ")";
      msg.hidden = false;
      return;
    }
    state.mode = body.mode;
  } catch (e) {
    msg.textContent = "Mode change failed: " + e.message;
    msg.hidden = false;
  }
  renderMode();
  if (state.stats) renderHero(state.stats);
}

function renderWindow() {
  for (const button of $("window-switch").querySelectorAll("button")) {
    button.classList.toggle("on", button.dataset.window === state.window);
  }
}

function init() {
  $("theme-toggle").addEventListener("click", () => setTheme(currentTheme() === "dark" ? "light" : "dark"));
  $("window-switch").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-window]");
    if (!button || !WINDOWS.includes(button.dataset.window)) return;
    state.window = button.dataset.window;
    renderWindow();
    refreshStats();
  });
  $("mode-switch").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (button && !button.disabled) putMode(button.dataset.mode);
  });
  $("search").addEventListener("input", (event) => {
    state.filter = event.target.value;
    renderFeed();
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("search").focus();
      $("search").select();
    }
  });
  renderWindow();
  setLive(false);
  loadMode();
  refreshStats();
  loadFeed();
  connectStream();
}

init();
