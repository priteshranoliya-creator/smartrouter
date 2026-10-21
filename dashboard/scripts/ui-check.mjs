#!/usr/bin/env node
// Drives the live dashboard in headless Chrome over CDP with real mouse events and prints PASS/FAIL per control.
// Needs the router on SMARTROUTER_URL (default http://127.0.0.1:8787) and Node 22+. It ends with the router in shadow mode.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.env.SMARTROUTER_URL ?? "http://127.0.0.1:8787";
const CHROME = process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function record(name, ok, detail = "") {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
}

function skip(name, why) {
  console.log(`SKIP  ${name}  ${why}`);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = createServer();
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
    s.on("error", reject);
  });
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.next = 1;
    this.pending = new Map();
    this.logs = [];
    ws.addEventListener("message", (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
      } else if (msg.method === "Runtime.exceptionThrown") {
        this.logs.push("exception: " + (msg.params.exceptionDetails.exception?.description ?? msg.params.exceptionDetails.text));
      } else if (msg.method === "Runtime.consoleAPICalled" && ["error", "assert"].includes(msg.params.type)) {
        this.logs.push("console." + msg.params.type + ": " + msg.params.args.map((a) => a.value ?? a.description).join(" "));
      } else if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
        this.logs.push("log: " + msg.params.entry.text + " " + (msg.params.entry.url ?? ""));
      }
    });
  }
  send(method, params = {}) {
    const id = this.next++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    return r.result.value;
  }
}

async function launch(port, dir) {
  const proc = spawn(
    CHROME,
    ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,1000", "--no-first-run", "--disable-gpu", "about:blank"],
    { stdio: "ignore" },
  );
  for (let i = 0; i < 100; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return { proc, wsUrl: page.webSocketDebuggerUrl };
    } catch {
      /* not up yet */
    }
    await sleep(100);
  }
  proc.kill();
  throw new Error("Chrome did not start");
}

// Page-side helpers, installed once per document.
const HELPERS = `
(() => {
  const lum = (c) => { const [r,g,b] = c.map((v) => { v/=255; return v<=0.03928 ? v/12.92 : ((v+0.055)/1.055)**2.4; }); return 0.2126*r+0.7152*g+0.0722*b; };
  const parse = (s) => (s.match(/[\\d.]+/g) || []).map(Number);
  window.__ratio = (fg, bg) => { const a = lum(parse(fg)), b = lum(parse(bg)); return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05); };
  window.__style = (el) => { const s = getComputedStyle(el); return { bg: s.backgroundColor, color: s.color, cls: el.className, ratio: window.__ratio(s.color, s.backgroundColor) }; };
  window.__rect = (sel, i = 0) => { const el = document.querySelectorAll(sel)[i]; if (!el) return null; el.scrollIntoView({ block: 'center', behavior: 'instant' }); const r = el.getBoundingClientRect(); return { x: r.x + r.width/2, y: r.y + r.height/2 }; };
  const px = document.createElement('canvas'); px.width = px.height = 1;
  const pctx = px.getContext('2d', { willReadFrequently: true });
  const rgba = (css) => { pctx.clearRect(0, 0, 1, 1); pctx.fillStyle = '#000'; pctx.fillStyle = css; pctx.fillRect(0, 0, 1, 1); const d = pctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const over = (top, under) => [0, 1, 2].map((i) => top[i] * top[3] + under[i] * (1 - top[3])).concat([1]);
  window.__effective = (el) => {
    const stack = [];
    let opacity = 1;
    for (let n = el; n; n = n.parentElement) { const s = getComputedStyle(n); stack.push(rgba(s.backgroundColor)); opacity *= Number(s.opacity); }
    let bg = [255, 255, 255, 1];
    for (let i = stack.length - 1; i >= 0; i--) bg = over(stack[i], bg);
    const fg = rgba(getComputedStyle(el).color);
    const text = over([fg[0], fg[1], fg[2], fg[3] * opacity], bg);
    const css = (c) => 'rgb(' + c.slice(0, 3).map(Math.round).join(',') + ')';
    return { fg: css(text), bg: css(bg) };
  };
  window.__tokens = () => { const s = getComputedStyle(document.documentElement); return { fg: s.getPropertyValue('--fg').trim(), bg: s.getPropertyValue('--bg').trim() }; };
})();
`;

async function main() {
  const port = await freePort();
  const dir = mkdtempSync(join(tmpdir(), "ui-check-"));
  const { proc, wsUrl } = await launch(port, dir);
  const ws = new WebSocket(wsUrl);
  await new Promise((r) => ws.addEventListener("open", r, { once: true }));
  const cdp = new Cdp(ws);
  let startMode = null;
  try {
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");
    await cdp.send("Page.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    startMode = (await (await fetch(`${BASE}/api/mode`)).json()).mode;
    await cdp.send("Page.navigate", { url: BASE + "/" });
    await cdp.eval(`new Promise((r) => { const t = setInterval(() => { if (document.querySelector('#mode-switch')) { clearInterval(t); r(); } }, 50); })`);
    await sleep(1200);
    await cdp.eval(HELPERS);

    const click = async (sel, i = 0) => {
      const p = await cdp.eval(`window.__rect(${JSON.stringify(sel)}, ${i})`);
      if (!p) throw new Error("no element " + sel);
      await sleep(80);
      const q = await cdp.eval(`window.__rect(${JSON.stringify(sel)}, ${i})`);
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: q.x, y: q.y });
      await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: q.x, y: q.y, button: "left", clickCount: 1 });
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: q.x, y: q.y, button: "left", clickCount: 1 });
      return q;
    };
    const moveAway = () => cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 700, y: 3 });
    const pick = (sel, i = 0) => `window.__style(document.querySelectorAll(${JSON.stringify(sel)})[${i}])`;

    // A selected segmented button must be the inverted pair: --fg background, --bg text, readable.
    const checkSegmented = async (name, sel, i, expectedOn) => {
      const tokens = await cdp.eval(`window.__tokens()`);
      const same = await cdp.eval(pick(sel, i));
      const groupBox = `(() => { const r = document.querySelectorAll(${JSON.stringify(sel)})[${i}].parentElement.getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map((v) => Math.round(v * 10) / 10).join(); })()`;
      const boxBefore = await cdp.eval(groupBox);
      await click(sel, i);
      const t0 = await cdp.eval(pick(sel, i));
      await sleep(50);
      const t50 = await cdp.eval(pick(sel, i));
      await moveAway();
      await sleep(250);
      const away = await cdp.eval(pick(sel, i));
      const rgb = (hex) => {
        const h = hex.replace("#", "");
        const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
        return `rgb(${parseInt(full.slice(0, 2), 16)}, ${parseInt(full.slice(2, 4), 16)}, ${parseInt(full.slice(4, 6), 16)})`;
      };
      const boxAfter = await cdp.eval(groupBox);
      const wantBg = rgb(tokens.fg);
      const wantFg = rgb(tokens.bg);
      const inverted = (s) => s.bg === wantBg && s.color === wantFg && s.ratio >= 4.5;
      const ok = expectedOn
        ? t0.cls.includes("on") && inverted(t0) && inverted(t50) && inverted(away) && boxBefore === boxAfter
        : same.cls.includes("on") === t0.cls.includes("on");
      record(
        name,
        ok,
        ok ? "" : `expected inverted (bg ${wantBg}, text ${wantFg}); observed same-frame ${JSON.stringify([t0.cls, t0.bg, t0.color, +t0.ratio.toFixed(2)])} +50ms ${JSON.stringify([t50.cls, t50.bg, t50.color, +t50.ratio.toFixed(2)])} mouse-away ${JSON.stringify([away.cls, away.bg, away.color])} group box ${boxBefore} -> ${boxAfter}`,
      );
    };

    // 1. Window selector.
    for (let i = 0; i < 5; i++) {
      const name = await cdp.eval(`document.querySelectorAll('#window-switch button')[${i}].dataset.window`);
      await checkSegmented(`window selector: ${name}`, "#window-switch button", i, true);
      const onNow = await cdp.eval(`[...document.querySelectorAll('#window-switch button.on')].map((b) => b.dataset.window).join()`);
      record(`window selector: only ${name} selected`, onNow === name, onNow === name ? "" : `observed on=${onNow}`);
    }

    // 2. Mode switcher (this ends in shadow whatever it started in).
    await checkSegmented("mode switcher: off", "#mode-switch button", 0, true);
    let apiMode = (await (await fetch(`${BASE}/api/mode`)).json()).mode;
    record("mode switcher: off reached the server", apiMode === "off", `GET /api/mode=${apiMode}`);
    await checkSegmented("mode switcher: shadow", "#mode-switch button", 1, true);
    apiMode = (await (await fetch(`${BASE}/api/mode`)).json()).mode;
    record("mode switcher: shadow reached the server", apiMode === "shadow", `GET /api/mode=${apiMode}`);
    const active = await cdp.eval(`(() => { const b = document.querySelectorAll('#mode-switch button')[2]; return { disabled: b.disabled, on: b.classList.contains('on') }; })()`);
    await click("#mode-switch button", 2);
    await sleep(100);
    const afterActive = await cdp.eval(`document.querySelector('#mode-switch button.on')?.dataset.mode`);
    record("mode switcher: active stays disabled", active.disabled && !active.on && afterActive === "shadow", `disabled=${active.disabled} on=${afterActive}`);

    // 3. Theme toggle, both directions, measured on the selected pills of the sidebar and the window selector.
    const startTheme = await cdp.eval(`document.documentElement.dataset.theme`);
    const themeSeen = [];
    for (let round = 0; round < 2; round++) {
      const before = await cdp.eval(`({ t: document.documentElement.dataset.theme, bg: getComputedStyle(document.body).backgroundColor })`);
      await click("#theme-toggle");
      const t0 = await cdp.eval(`({ t: document.documentElement.dataset.theme, bg: getComputedStyle(document.body).backgroundColor, ls: localStorage.getItem('smartrouter-theme'), icons: [...document.querySelectorAll('#theme-toggle svg')].map((s) => getComputedStyle(s).display) })`);
      await sleep(50);
      const t50 = await cdp.eval(`getComputedStyle(document.body).backgroundColor`);
      await moveAway();
      await sleep(300);
      const pill = await cdp.eval(`window.__style(document.querySelector('.nav-item.active'))`);
      const win = await cdp.eval(`window.__style(document.querySelector('#window-switch button.on'))`);
      const flipped = t0.t !== before.t && t0.bg !== before.bg && t50 === t0.bg;
      const oneIcon = t0.icons.filter((d) => d !== "none").length === 1;
      const stored = t0.ls === t0.t;
      const readable = pill.ratio >= 4.5 && win.ratio >= 4.5;
      themeSeen.push(t0.t);
      record(
        `theme toggle -> ${t0.t}`,
        flipped && oneIcon && stored && readable,
        flipped && oneIcon && stored && readable ? "" : `flipped=${flipped} oneIcon=${oneIcon} stored=${stored} activePill=${pill.ratio.toFixed(2)} windowOn=${win.ratio.toFixed(2)}`,
      );
      // Selected controls in this theme
      await checkSegmented(`window selector selected state in ${t0.t} theme`, "#window-switch button", 2, true);
      await checkSegmented(`mode switcher selected state in ${t0.t} theme`, "#mode-switch button", 1, true);
    }
    record("theme toggle returns to start theme", (await cdp.eval(`document.documentElement.dataset.theme`)) === startTheme && themeSeen.length === 2);
    await checkSegmented("window selector: back to all", "#window-switch button", 4, true);

    // 4. Sidebar nav and panel anchors: the active pill follows the clicked item, and the target scrolls into view.
    const navCount = await cdp.eval(`document.querySelectorAll('.sidebar .nav-item[href^="#"]').length`);
    for (let i = 0; i < navCount; i++) {
      if ((await cdp.eval(`document.querySelectorAll('.sidebar .nav-item[href^="#"]')[${i}].innerText.trim()`)) === "Technical details") continue;
      const label = await cdp.eval(`document.querySelectorAll('.sidebar .nav-item[href^="#"]')[${i}].innerText.split('\\n')[0].trim()`);
      const href = await cdp.eval(`document.querySelectorAll('.sidebar .nav-item[href^="#"]')[${i}].getAttribute('href')`);
      await click('.sidebar .nav-item[href^="#"]', i);
      const read = () => cdp.eval(`(() => { const items = [...document.querySelectorAll('.sidebar .nav-item[href^="#"]')]; const on = items.map((e, n) => e.classList.contains('active') ? n : -1).filter((n) => n >= 0); const el = items[${i}]; return { on, st: window.__style(el) }; })()`);
      const t0 = await read();
      await sleep(50);
      const t50 = await read();
      await moveAway();
      await sleep(900);
      const settled = await read();
      const target = await cdp.eval(`(() => { const el = document.querySelector(${JSON.stringify(href)}); if (!el) return false; const r = el.getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0; })()`);
      const tokens = await cdp.eval(`window.__tokens()`);
      const ok = t0.on.length === 1 && t0.on[0] === i && t50.on[0] === i && settled.on.length === 1 && settled.on[0] === i && settled.st.ratio >= 4.5 && target;
      record(
        `sidebar nav: ${label} (${href}, item ${i})`,
        ok,
        ok ? "" : `expected only item ${i} active and target visible; observed active=[${t0.on}] +50ms=[${t50.on}] settled=[${settled.on}] ratio=${settled.st.ratio.toFixed(2)} targetVisible=${target} (tokens ${tokens.fg}/${tokens.bg})`,
      );
    }
    await cdp.eval(`window.scrollTo(0, 0)`);

    // 4b. Tabs: Technical details holds the relocated panels, Overview comes back intact.
    await click("#tab-technical");
    await sleep(400);
    const tech = await cdp.eval(`({ on: document.querySelector('#tab-technical').getAttribute('aria-selected'), ids: ['panel-counters', 'panel-cache', 'panel-engine', 'panel-models'].filter((id) => document.getElementById(id)), feed: !!document.getElementById('panel-feed'), nav: document.querySelector('.nav-item.active')?.innerText.trim() })`);
    record("tabs: Technical details shows the relocated panels", tech.on === "true" && tech.ids.length === 4 && !tech.feed && tech.nav === "Technical details", JSON.stringify(tech));
    await click('.sidebar .nav-item[href="#panel-cache"]');
    await sleep(900);
    record("tabs: technical sidebar item scrolls to its panel and is active", (await cdp.eval(`document.querySelector('.nav-item.active')?.innerText.split('\\n')[0].trim()`)) === "Cache health");
    await click("#tab-overview");
    await sleep(400);
    const over = await cdp.eval(`({ on: document.querySelector('#tab-overview').getAttribute('aria-selected'), ids: ['panel-spend', 'panel-feed', 'panel-ledger', 'panel-mix', 'panel-quality', 'panel-eval'].filter((id) => document.getElementById(id)).length, nav: document.querySelector('.nav-item.active')?.innerText.trim() })`);
    record("tabs: Overview restores its six panels", over.on === "true" && over.ids === 6 && over.nav === "Overview", JSON.stringify(over));

    // 4c. Card toggles: technical detail on the feed, estimate columns on the ledger.
    const feedBefore = await cdp.eval(`document.querySelectorAll('#feed .feed-row .f-meta .badge:not(.switch)').length`);
    await click("#feed-detail");
    const feedAfter = await cdp.eval(`({ pressed: document.querySelector('#feed-detail').getAttribute('aria-pressed'), badges: document.querySelectorAll('#feed .feed-row .f-meta .badge:not(.switch)').length, tok: document.querySelector('#feed .f-meta').innerText.includes('tok') })`);
    record("feed: Technical detail toggle reveals rule and token detail", feedAfter.pressed === "true" && feedAfter.tok && feedAfter.badges >= feedBefore, JSON.stringify(feedAfter));
    await click("#feed-detail");
    const ledCols = () => cdp.eval(`document.querySelectorAll('#ledger-table thead th').length`);
    const c0 = await ledCols();
    if (c0 === 0 || (await cdp.eval(`document.getElementById('ledger-table').hidden`))) skip("ledger: Show estimates", "no switches in this database");
    else {
      await click("#ledger-estimates");
      const c1 = await ledCols();
      await click("#ledger-estimates");
      record("ledger: Show estimates adds and removes the estimate columns", c1 === c0 + 2 && (await ledCols()) === c0, `columns ${c0} -> ${c1}`);
    }
    await cdp.eval(`window.scrollTo(0, 0)`);

    // 5. Eval run selector and refresh.
    await cdp.eval(`document.querySelector('#panel-eval').scrollIntoView({ block: 'start', behavior: 'instant' })`);
    await sleep(600);
    const runOpts = await cdp.eval(`[...document.querySelectorAll('#eval-run option')].map((o) => o.value)`);
    if (runOpts.length < 2) {
      record("eval run selector", false, `needs 2 eval runs to test, found ${runOpts.length}`);
    } else {
      const firstTitle = await cdp.eval(`document.querySelector('#panel-eval #eval-accuracy').innerText`);
      const target = runOpts[1];
      await cdp.eval(`(() => { const s = document.querySelector('#eval-run'); s.value = ${JSON.stringify(target)}; s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
      const t0 = await cdp.eval(`({ v: document.querySelector('#eval-run').value, loading: !!document.querySelector('#panel-eval .empty:not([hidden])') })`);
      await sleep(900);
      const settled = await cdp.eval(`({ v: document.querySelector('#eval-run').value, badges: document.querySelector('#panel-eval #eval-accuracy')?.innerText, rows: document.querySelectorAll('#panel-eval table.arms tbody tr').length })`);
      record("eval run selector: switches run and renders it", t0.v === target && settled.v === target && !!settled.badges, `value ${t0.v}->${settled.v}, badges "${settled.badges?.replace(/\n/g, " ")}" (was "${firstTitle.replace(/\n/g, " ")}")`);
      await cdp.eval(`(() => { const s = document.querySelector('#eval-run'); s.value = ${JSON.stringify(runOpts[0])}; s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
      await sleep(900);
    }
    const hBefore = await cdp.eval(`document.querySelector('#panel-eval').getBoundingClientRect().height`);
    await cdp.eval(`window.__fetches = 0; const f = window.fetch; window.fetch = (...a) => { window.__fetches++; return f(...a); };`);
    await click('#panel-eval .icon-btn.small');
    await sleep(700);
    const refreshed = await cdp.eval(`({ n: window.__fetches, h: document.querySelector('#panel-eval').getBoundingClientRect().height, body: !!document.querySelector('#panel-eval table.arms') })`);
    record("eval refresh: refetches without collapsing the panel", refreshed.n >= 2 && Math.abs(refreshed.h - hBefore) < 4 && refreshed.body, `fetches=${refreshed.n} height ${Math.round(hBefore)}->${Math.round(refreshed.h)}`);

    // 6. Eval item rows: the button and the whole row toggle, collapse restores. Rows live in a collapsed disclosure.
    const discState = () => cdp.eval(`[...document.querySelectorAll('#panel-eval .disc-btn')].map((b) => b.getAttribute('aria-expanded'))`);
    const discBefore = await discState();
    record("eval: Charts and Per-question results start collapsed", discBefore.length === 2 && discBefore.every((v) => v === "false") && !(await cdp.eval(`!!document.querySelector('#panel-eval table.items')`)), JSON.stringify(discBefore));
    await click('#panel-eval .disc-btn', 1);
    await sleep(200);
    const toggleIds = await cdp.eval(`document.querySelectorAll('#panel-eval .row-toggle').length`);
    if (!toggleIds) {
      record("eval item row expand/collapse", false, "no item rows rendered");
    } else {
      const state = () => cdp.eval(`({ expanded: document.querySelectorAll('#panel-eval .row-toggle')[0].getAttribute('aria-expanded'), detail: document.querySelectorAll('#panel-eval tr.item-detail').length, rot: getComputedStyle(document.querySelector('#panel-eval .row-toggle .chev')).transform })`);
      const closed = await state();
      await click('#panel-eval .row-toggle', 0);
      const open0 = await state();
      await sleep(50);
      const open50 = await state();
      record("eval item row: label button expands", closed.detail === 0 && open0.expanded === "true" && open0.detail === 1 && open50.detail === 1, `before ${JSON.stringify(closed)} after ${JSON.stringify(open0)}`);
      await click('#panel-eval .row-toggle', 0);
      const shut = await state();
      record("eval item row: label button collapses", shut.expanded === "false" && shut.detail === 0, JSON.stringify(shut));
      await click('#panel-eval tr.item-row td:nth-child(3)', 0);
      const viaCell = await state();
      record("eval item row: clicking anywhere on the row expands (hint says 'click a row')", viaCell.expanded === "true" && viaCell.detail === 1, `observed ${JSON.stringify(viaCell)}`);
      if (viaCell.detail === 1) await click('#panel-eval tr.item-row td:nth-child(3)', 0);
      await moveAway();
    }

    // 7. Search box.
    const total = await cdp.eval(`document.querySelectorAll('#feed .feed-row').length`);
    await click("#search");
    if (total === 0) skip("search: filtering real feed rows", "the feed has no turns in this database, so only the empty state is checked");
    await cdp.send("Input.insertText", { text: "zzzz-no-such-prompt" });
    await sleep(60);
    const none = await cdp.eval(`({ rows: document.querySelectorAll('#feed .feed-row').length, empty: !document.querySelector('#feed-empty').hidden, meta: document.querySelector('#feed-meta').innerText, nav: document.querySelector('#nav-feed').innerText })`);
    record("search: a non-matching filter empties the feed immediately", none.rows === 0 && none.empty && none.nav === "0", JSON.stringify(none));
    await cdp.eval(`(() => { const i = document.querySelector('#search'); i.value = ''; i.dispatchEvent(new Event('input', { bubbles: true })); })()`);
    await sleep(60);
    const back = await cdp.eval(`document.querySelectorAll('#feed .feed-row').length`);
    record("search: clearing restores the feed", back === total, `rows ${total} -> ${back}`);
    await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "k", code: "KeyK", modifiers: 4, windowsVirtualKeyCode: 75 });
    await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "k", code: "KeyK", modifiers: 4, windowsVirtualKeyCode: 75 });
    await sleep(60);
    record("search: Ctrl K focuses the box", (await cdp.eval(`document.activeElement?.id`)) === "search");

    // 7b. Text contrast in both themes, against the nearest painted background.
    const startTheme2 = await cdp.eval(`document.documentElement.dataset.theme`);
    for (const theme of ["dark", "light"]) {
      await cdp.eval(`document.documentElement.dataset.theme = ${JSON.stringify(theme)}`);
      await sleep(250);
      const low = await cdp.eval(`(() => {
        const bad = new Map();
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        for (let t = walker.nextNode(); t; t = walker.nextNode()) {
          if (!t.textContent.trim()) continue;
          const el = t.parentElement;
          const s = getComputedStyle(el);
          if (s.visibility === 'hidden' || s.display === 'none' || el.closest('[hidden], canvas, script, style') || el.closest('button:disabled')) continue;
          const r = el.getBoundingClientRect();
          if (!r.width || !r.height) continue;
          const large = parseFloat(s.fontSize) >= 24 || (parseFloat(s.fontSize) >= 18.66 && Number(s.fontWeight) >= 700);
          const need = large ? 3 : 4.5;
          const eff = window.__effective(el);
          const ratio = window.__ratio(eff.fg, eff.bg);
          if (ratio < need) { const k = el.className + '|' + eff.fg + '|' + eff.bg; if (!bad.has(k)) bad.set(k, { cls: (el.className || el.tagName).toString().slice(0, 40), ratio: +ratio.toFixed(2), color: eff.fg + ' on ' + eff.bg }); }
        }
        return [...bad.values()];
      })()`);
      record(`contrast: all text readable in ${theme} theme`, low.length === 0, low.map((l) => `.${l.cls} ${l.color} ${l.ratio}:1`).join("; "));
    }
    await cdp.eval(`document.documentElement.dataset.theme = ${JSON.stringify(startTheme2)}`);
    await sleep(200);

    // 8. Eval scatter: router and oracle labels must not overlap.
    await cdp.eval(`document.querySelector('#panel-eval').scrollIntoView({ block: 'start', behavior: 'instant' })`);
    await sleep(300);
    await click('#panel-eval .disc-btn', 0);
    await sleep(500);
    const labels = await cdp.eval(`(() => { const raw = document.querySelector('#panel-eval canvas')?.dataset.labelBoxes; return raw ? JSON.parse(raw) : null; })()`);
    if (labels === null) {
      record("eval scatter: arm labels do not overlap", false, "canvas has no data-label-boxes, so label placement cannot be checked");
    } else {
      const boxes = labels;
      let hit = null;
      for (let a = 0; a < boxes.length; a++)
        for (let b = a + 1; b < boxes.length; b++) {
          const p = boxes[a], q = boxes[b];
          if (p.x < q.x + q.w && q.x < p.x + p.w && p.y < q.y + q.h && q.y < p.y + p.h) hit = hit ?? `${p.arm} x ${q.arm}`;
        }
      record("eval scatter: arm labels do not overlap", hit === null, hit ? `overlap ${hit}` : "");
    }

    // 9. Keyboard focus visibility on every control.
    const focusProbe = await cdp.eval(`(() => { const out = []; for (const sel of ['#window-switch button', '#mode-switch button:not(:disabled)', '#theme-toggle', '.nav-item', '#eval-run', '#panel-eval .icon-btn.small', '#panel-eval .row-toggle', '#search', '#tab-overview', '#feed-detail', '.disc-btn']) { const el = document.querySelector(sel); if (!el) continue; el.focus({ focusVisible: true }); const s = getComputedStyle(el); const ring = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0; const field = el.id === 'search' ? getComputedStyle(el.closest('label')).borderColor : null; out.push({ sel, ring, field, shown: el.matches(':focus-visible') }); } return out; })()`);
    const noRing = focusProbe.filter((p) => !p.ring && p.sel !== "#search" && p.sel !== "#eval-run");
    record("keyboard focus: every button and link shows a focus ring", noRing.length === 0, noRing.map((p) => p.sel).join(", "));

    // 10. Phone width: nothing overflows horizontally.
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 500, height: 900, deviceScaleFactor: 1, mobile: false });
    await sleep(400);
    const wrap = await cdp.eval(`new Promise((resolve) => { const f = document.createElement('iframe'); f.style.cssText = 'position:fixed;inset:0;width:390px;height:900px;border:0;z-index:99;background:#fff'; f.src = '/'; document.body.appendChild(f); f.onload = () => setTimeout(() => { const d = f.contentDocument; const vis = (q) => { const e = d.querySelector(q); const r = e && e.getBoundingClientRect(); return !!r && r.width > 0 && r.right <= 391 && r.left >= 0; }; resolve({ sw: d.documentElement.scrollWidth, cw: d.documentElement.clientWidth, mode: vis('#mode-switch'), search: vis('#search'), tabs: vis('#tab-technical') }); }, 1500); })`);
    record("phone 390px: no horizontal page scroll", wrap.sw <= wrap.cw + 1, `scrollWidth ${wrap.sw} vs ${wrap.cw}`);
    record("phone 390px: mode switch, search and tabs stay reachable", wrap.mode && wrap.search && wrap.tabs, JSON.stringify(wrap));
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

    const errs = cdp.logs.filter((l) => !/fonts\.g(oogleapis|static)\.com|ERR_(INTERNET_DISCONNECTED|NAME_NOT_RESOLVED|BLOCKED)/.test(l));
    record("console: no errors or exceptions", errs.length === 0, errs.slice(0, 3).join(" | "));
  } finally {
    // The router must be left in shadow mode.
    try {
      await fetch(`${BASE}/api/mode`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: "shadow" }) });
    } catch {
      /* reported below */
    }
    const finalMode = await fetch(`${BASE}/api/mode`).then((r) => r.json()).catch(() => ({ mode: "unreachable" }));
    record("router left in shadow mode", finalMode.mode === "shadow", `started in ${startMode}, now ${finalMode.mode}`);
    ws.close();
    proc.kill();
    await sleep(300);
    rmSync(dir, { recursive: true, force: true });
  }
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(2);
});
