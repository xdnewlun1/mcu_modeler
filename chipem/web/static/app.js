// chipem web UI: talks to chipem/web/server.py (JSON API + Server-Sent Events). No build step.
// Works for a single chip and for a board (several chips): /api/chip `chips` lists them; every pin row carries
// `chip`; names sent to the server are qualified ("B.PTC6", "B.UART0") on a board.
// Payloads: docs/CONTRACTS.md and chipem/web/server.py (slim pin frames with `pins_full`, `waves`, `adc`, `periph`,
// `buses`). Formatting: fmt.js (one formatter per quantity).
import { ownerCategory } from "./package.js";
import { BoardView } from "./board.js";
import * as partsMod from "./parts.js";
import { initBoardDialog, boardDialogOpen, closeBoardDialog } from "./boarddlg.js";
import { esc, hex32, fmtTime, fmtTs, fmtFreq, fmtDuty, fmtRate, fmtWave, parseDurationMs, chipCls, fmtReg } from "./fmt.js";

const { stateText, counterValue, ledColor, LED_COLORS, partCategory, portList } = partsMod;
// parts.js partGroup (inputs / indicators / logic / devices / connectors); fallback from the layout category
const partGroup = partsMod.partGroup || ((t) => ({ src: "inputs", sink: "indicators", logic: "logic" }[partCategory(t)]));
const GROUPS = ["inputs", "indicators", "logic", "devices", "connectors"];

const $ = (id) => document.getElementById(id);
const KINDS = ["pin", "part", "uart", "can", "i2s", "eth", "usb", "irq", "reset", "fault", "warn", "info", "mmio"];
const MAX_EVENT_ROWS = 1500;      // DOM rows of the visible kinds
const KIND_RING = 1500;           // events kept in memory per kind (shown again when a kind is switched on)
const MAX_LINES = 5000;           // UART transcript lines kept
const MAX_LINE_CHARS = 2000;      // a line without a line ending (binary data) is wrapped into a new one here
const RAW_KEEP = 1 << 20;         // raw UART text kept in the browser (chars); the full output is a download away
const RAW_SHOW = 200000;          // raw UART text kept in the DOM
const MON_BYTES = 64;             // link monitor: last bytes per direction
const WAVE_MS = 500;              // wave / analog columns refresh at most this often

const ui = {
  info: null,                     // /api/chip
  chips: [],                      // info.chips
  chipById: new Map(),
  metaByChip: new Map(),          // chip id -> Map(num -> static pin)
  isBoard: false,
  sel: null,                      // selected chip id
  board: null,                    // BoardView
  state: null,
  rows: new Map(),                // chip id -> rows
  rowByKey: new Map(),            // "A:77" -> row
  prevKey: new Map(),             // "A:77" -> rowKey
  baseline: null,                 // Map "A:77" -> rowKey at load/reset ("changed only")
  kindsOn: new Set(KINDS.filter((k) => k !== "info" && k !== "mmio")),
  kindCount: {},                  // kind -> events received (also those not shown)
  evDropped: 0,                   // rows evicted from the event view (MAX_EVENT_ROWS)
  raw: {},                        // raw UART view: uart -> {text, base, len} (base = byte offset of text[0])
  uartPicked: false,              // the user chose a UART: no automatic switching to one with output
  uartTs: false,
  hoverPane: null,                // pane under the pointer ("/" focuses its filter)
  lastFilter: null,
  spd: null,                      // speed estimate {t, wall, x}
  pinVisible: [],
  chipsOn: new Set(),
  sort: { k: "num", desc: false },
  uart: null,                     // selected qualified UART name
  uartMode: "raw",                // "lines" (transcript from uart events) | "raw" (/api/uart output)
  uartAll: false,
  uartLen: {},
  lines: [],                      // transcript [{uart, chip, text, open, input}]
  openLine: new Map(),            // uart -> open line object
  afterCR: new Map(),             // uart -> last byte was CR (the LF of CRLF then adds no empty line)
  linkMon: new Map(),             // "A.UART0" -> {bytes: [], n}
  lastEventSeq: 0,
  es: null,
  evQueue: [],
  pendingState: null,
  rafPending: false,
  uartDirty: false,
  uartDrops: new Map(),           // uart -> {n, reason} bytes its receiver dropped (uart events dir "drop")
  uartErrs: new Map(),            // uart -> {fe, pe, nf, brk: counts, last} received characters with line errors
  wiring: 0,                      // board wiring version of ui.info (nets/links); refetched when it changes
  wireMode: false,
  pendingPin: null,               // wire mode: first picked pin {cid, pin}
  selNet: null,
  partState: {},                  // part id -> latest state (state frames + `part` events)
  partViews: {},                  // part id -> previews it offers ({frame: {w, h, seq}}, {samples: {seq, ...}})
  partsDirty: false,
  partTypes: null,                // /api/parts/types (loaded when the add-part dialog first opens)
  pickTarget: null,               // add-part dialog: the port input a board click fills
  wiringSig: "",
  waves: {},                      // "A:77" -> {edges, freq_hz, duty} (state `waves`)
  adc: {},                        // chip -> {channel: value} (state `adc`)
  periph: [],                     // state `periph`
  ring: {},                       // kind -> recent events (also of hidden kinds), bounded
  lastEvRow: null,                // newest event row (collapsing of repeats)
  selPin: null,                   // "A:77": row selected in the pin table / on the canvas
  traced: new Set(),              // "A:77" traced in the logic strip
  offline: false,
  undo: null,
  linesDirty: new Set(),          // transcript lines added / grown since the last render
  uartFull: true,                 // transcript needs a full rebuild
};

// ---------------------------------------------------------------- helpers
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  let r;
  try { r = await fetch(path, opts); } catch {
    if (!ui.leaving) setOffline(true);
    throw new Error("server not reachable");
  }
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const e = new Error((data && data.error) || `${r.status} ${r.statusText}`);
    e.status = r.status;
    if (data && data.scripts) e.scripts = data.scripts;        // 409: script parts need the user's consent
    throw e;
  }
  return data;
}

// Toasts: informational ("copied 12 lines", "net NET3") in the neutral info style, failures in the error style
// (role=alert, shown longer). An optional action button (e.g. "undo") keeps the toast up a little longer.
let toastTimer = null;
function toast(msg, kind = "info", action = null) {
  const t = $("toast");
  $("toastText").textContent = msg;
  t.className = `toast toast-${kind}`;
  t.setAttribute("role", kind === "error" ? "alert" : "status");
  const b = $("toastAct");
  b.hidden = !action;
  b.onclick = null;
  if (action) {
    b.textContent = action.label;
    b.onclick = () => { t.hidden = true; action.fn(); };
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, action ? 8000 : kind === "error" ? 6000 : 3000);
}
const toastErr = (msg) => { if (!ui.offline || !/not reachable/.test(msg)) toast(msg, "error"); };

async function act(path, body) {
  try { return await api(path, body ?? {}); } catch (e) { toastErr(e.message); return null; }
}

// ---------------------------------------------------------------- theme (dark / light)
// index.html sets html[data-theme] before the first paint (saved choice, else the system preference). The header
// button switches and saves; without a saved choice the page follows the system setting live.
const THEME_KEY = "chipem.theme";
function savedTheme() { try { return localStorage.getItem(THEME_KEY); } catch { return null; } }
function setTheme(t, save) {
  document.documentElement.setAttribute("data-theme", t);
  if (save) { try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode */ } }
  const b = $("btnTheme");
  if (b) {
    const other = t === "light" ? "dark" : "light";
    b.title = `Theme: ${t}${savedTheme() ? "" : " (system)"} · click: ${other}`;
    b.setAttribute("aria-label", `Switch to the ${other} theme`);
  }
}
export function initTheme() {
  const cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  setTheme(cur, false);
  $("btnTheme")?.addEventListener("click", () =>
    setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light", true));
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  mq?.addEventListener?.("change", (e) => { if (!savedTheme()) setTheme(e.matches ? "light" : "dark", false); });
}

const qual = (cid, name) => (ui.isBoard ? `${cid}.${name}` : name);
const keyOf = (cid, num) => `${cid}:${num}`;
const evChip = (ev) => ev.chip ?? (ui.chips[0] && ui.chips[0].id) ?? "A";

function ownerCat(pin, row) {
  if (pin.kind === "power") return "power";
  if (pin.kind === "analog" || row.owner === "analog") return "analog";
  if (row.owner === "disabled" || row.owner === pin.kind) return "disabled";
  const c = ownerCategory(pin, row.owner);
  return c === "none" ? "disabled" : c;
}

// change key of a (slim) pin row: the wave summary is not part of it (edge counts would flash every frame)
const rowKey = (r) => `${r.level}|${r.owner}|${r.direction}|${r.pull}|${r.external}|${r.net ?? ""}|${r.cfg_by || ""}`;

function chipState(st, cid) {
  if (!st) return null;
  if (st.chips && st.chips[cid]) return { ...st.chips[cid], running: st.running };
  return st;
}


// ---------------------------------------------------------------- pane toolkit: filter, copy, download, follow, layout
// Filters: plain text = case-insensitive substring; "/re/flags" = regular expression (i by default).
export function makeMatcher(text) {
  const s = String(text || "").trim();
  if (!s) return null;
  const m = /^\/(.+)\/([gimsuy]*)$/.exec(s);
  if (m) {
    try {
      const flags = (m[2] || "").replace("g", "");
      const re = new RegExp(m[1], flags.includes("i") || /[A-Z]/.test(m[1]) ? flags : flags + "i");
      // the server (/api/events?q=) applies the same smart case; an explicit /i on an upper-case pattern is sent as (?i)
      const ci = re.flags.includes("i") && /[A-Z]/.test(m[1]);
      return { test: (x) => re.test(String(x)), re, src: s, server: `re:${ci ? "(?i)" : ""}${m[1]}` };
    } catch { return { bad: true, test: () => true, src: s }; }
  }
  const q = s.toLowerCase();
  return { test: (x) => String(x).toLowerCase().includes(q), q, src: s, server: s };
}

// escaped HTML of `text` with the matches of matcher `m` wrapped in <mark>
function hl(text, m) {
  const t = String(text ?? "");
  if (!m || m.bad) return esc(t);
  const spans = [];
  if (m.re) {
    const re = new RegExp(m.re.source, m.re.flags.includes("g") ? m.re.flags : m.re.flags + "g");
    let x;
    while ((x = re.exec(t)) && spans.length < 50) { if (!x[0]) { re.lastIndex++; continue; } spans.push([x.index, x.index + x[0].length]); }
  } else {
    const lo = t.toLowerCase();
    for (let i = lo.indexOf(m.q); i >= 0 && spans.length < 50; i = lo.indexOf(m.q, i + m.q.length)) spans.push([i, i + m.q.length]);
  }
  if (!spans.length) return esc(t);
  let out = "", p = 0;
  for (const [a, b] of spans) { out += esc(t.slice(p, a)) + `<mark>${esc(t.slice(a, b))}</mark>`; p = b; }
  return out + esc(t.slice(p));
}

// wire a .pane-filter input: debounced onChange(matcher); Esc clears and blurs
function bindFilter(input, onChange) {
  let tm = null;
  const fire = () => {
    const m = makeMatcher(input.value);
    input.classList.toggle("bad", !!(m && m.bad));
    input.classList.toggle("on", !!m && !m.bad);
    input.title = m && m.bad ? "invalid regular expression" : "substring, or /regex/ · Esc: clear";
    input._m = m && !m.bad ? m : null;
    onChange(input._m);
  };
  input.addEventListener("input", () => { clearTimeout(tm); tm = setTimeout(fire, 80); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); input.value = ""; fire(); input.blur(); }
    else if (e.key === "Enter") { clearTimeout(tm); fire(); }
  });
  input.addEventListener("focus", () => { ui.lastFilter = input; });
  input._m = null;
  return () => input._m;
}

async function copyText(text, what = "text") {
  const n = text ? text.split("\n").length - (text.endsWith("\n") ? 1 : 0) : 0;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;left:-9999px;top:0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { toastErr("copy failed (clipboard not available)"); ta.remove(); return; }
    ta.remove();
  }
  toast(`copied ${n} ${what}${n === 1 ? "" : "s"}`);
}

function saveFile(name, text, type = "text/plain") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

function saveUrl(url) {             // server sends Content-Disposition: attachment
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
}

const csvCell = (v) => { const s = String(v ?? ""); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
const toCsv = (rows) => rows.map((r) => r.map(csvCell).join(",")).join("\n") + "\n";
const toTsv = (rows) => rows.map((r) => r.map((v) => String(v ?? "").replace(/[\t\n]/g, " ")).join("\t")).join("\n") + "\n";
const stamp = () => new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
const fileBase = () => (ui.isBoard ? (ui.info.board && ui.info.board.name) : ui.info && ui.info.name) || "chipem";

// visible rows of a <table> as arrays of cell text (header first)
function tableRows(table) {
  const head = [...table.tHead.rows[0].cells].map((c) => c.textContent.trim());
  const body = [...table.tBodies[0].rows].filter((tr) => !tr.hidden && !tr.classList.contains("hidden"))
    .map((tr) => [...tr.cells].map((c) => c.textContent.trim()));
  return [head, ...body];
}

// keeps a log scrolled to its end while the user is there; scrolled up: a "N new" button jumps back
class Follow {
  constructor(el, btn) {
    this.el = el; this.btn = btn; this.stuck = true; this.n = 0;
    el.addEventListener("scroll", () => {
      const at = this.atEnd();
      if (at !== this.stuck) { this.stuck = at; if (at) this.n = 0; this.show(); }
    }, { passive: true });
    btn.addEventListener("click", () => this.end());
  }
  atEnd() { const e = this.el; return e.scrollHeight - e.scrollTop - e.clientHeight < 24; }
  after(added = 0) {
    if (this.stuck) this.el.scrollTop = this.el.scrollHeight;
    else if (added) { this.n += added; this.show(); }
  }
  end() { this.stuck = true; this.n = 0; this.el.scrollTop = this.el.scrollHeight; this.show(); }
  show() {
    this.btn.hidden = this.stuck;
    this.btn.textContent = this.n ? `↓ ${this.n.toLocaleString()} new` : "↓ end";
  }
}

// ---- persisted layout: sidebar width, tray height, pane weights, collapsed flags
const PREF_KEY = "chipem.ui.v1";
const prefs = (() => { try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch { return {}; } })();
prefs.w = prefs.w || {};
prefs.collapsed = prefs.collapsed || {};
function savePrefs() { try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch { /* private mode */ } }
const PANE_W = { uart: 380, events: 320, status: 130, nets: 150, links: 110, parts: 170 };

const visiblePane = (p) => p && p.classList.contains("pane") && p.offsetParent !== null;

function setCollapsed(pane, on, save = true) {
  pane.classList.toggle("collapsed", on);
  const t = pane.querySelector(".pane-tog");
  if (t) t.setAttribute("aria-expanded", String(!on));
  if (save) { prefs.collapsed[pane.dataset.pane] = on; savePrefs(); }
  if (on && pane.classList.contains("maxed")) maximise(pane);
  if (pane.id === "panePins") window.dispatchEvent(new Event("resize"));
}

// double-click a sidebar pane title: that pane takes the whole sidebar (the others shrink to their headers); again:
// restore
function maximise(pane) {
  const side = $("sidebar");
  const on = !pane.classList.contains("maxed");
  for (const p of side.querySelectorAll(":scope > .pane")) p.classList.remove("maxed");
  side.classList.toggle("maxed", on);
  if (on) { pane.classList.add("maxed"); if (pane.classList.contains("collapsed")) setCollapsed(pane, false, false); }
}

function initPanes() {
  const shell = document.querySelector(".shell");
  if (prefs.sideW) shell.style.setProperty("--side-w", `${prefs.sideW}px`);
  else shell.style.setProperty("--side-w", `${Math.round(Math.min(560, Math.max(340, window.innerWidth * 0.3)))}px`);
  if (prefs.trayH) shell.style.setProperty("--tray-h", `${prefs.trayH}px`);
  else shell.style.setProperty("--tray-h", `${Math.round(Math.max(150, window.innerHeight * 0.26))}px`);
  const short = window.innerHeight < 800;
  for (const pane of document.querySelectorAll(".pane[data-pane]")) {
    const name = pane.dataset.pane;
    if (pane.parentElement.id === "sidebar") pane.style.setProperty("--w", prefs.w[name] ?? PANE_W[name] ?? 200);
    // defaults: links / parts collapsed when empty or the window is short; status opens itself once it has rows
    const def = name === "status" ? true
      : !ui.isBoard ? false
        : name === "links" ? short || !((ui.info.links || []).length + (ui.info.buses || []).length)
          : name === "parts" ? short || !(ui.info.parts || []).length : false;
    if (pane.querySelector(".pane-tog")) setCollapsed(pane, prefs.collapsed[name] ?? def, false);
    const tog = pane.querySelector(".pane-tog");
    if (tog) tog.addEventListener("click", () => setCollapsed(pane, !pane.classList.contains("collapsed")));
    pane.querySelector(".pane-head").addEventListener("dblclick", (e) => {
      if (!tog || e.target.closest("button, input, select, label, .tab, .kind-filter, form")) return;
      if (e.target.closest("h2") && pane.parentElement.id === "sidebar") { maximise(pane); return; }
      setCollapsed(pane, !pane.classList.contains("collapsed"));
    });
    pane.addEventListener("mouseenter", () => { ui.hoverPane = pane; });
  }
  // splitters: drag, or focus + arrow keys (Shift: x5)
  const sideW = (w) => {
    w = Math.round(Math.max(260, Math.min(window.innerWidth * 0.75, w)));
    shell.style.setProperty("--side-w", `${w}px`);
    prefs.sideW = w;
    $("splitSide").setAttribute("aria-valuenow", String(w));
  };
  const trayH = (h) => {
    const max = document.querySelector(".maincol").getBoundingClientRect().height - 140;
    h = Math.round(Math.max(60, Math.min(max, h)));
    shell.style.setProperty("--tray-h", `${h}px`);
    prefs.trayH = h;
    $("splitTray").setAttribute("aria-valuenow", String(h));
  };
  drag($("splitSide"), "v", () => ({ w: $("sidebar").getBoundingClientRect().width }), (s, dx) => sideW(s.w - dx));
  drag($("splitTray"), "h", () => ({ h: $("panePins").getBoundingClientRect().height }), (s, _dx, dy) => {
    if (!$("panePins").classList.contains("collapsed")) trayH(s.h - dy);
  });
  splitKeys($("splitSide"), (d) => sideW($("sidebar").getBoundingClientRect().width + d), "x");
  splitKeys($("splitTray"), (d) => trayH($("panePins").getBoundingClientRect().height + d), "y");
  const paneStart = (sp) => {
    const open = [...$("sidebar").children].filter((p) => visiblePane(p) && !p.classList.contains("collapsed"));
    for (const p of open) p.style.setProperty("--w", p.getBoundingClientRect().height);   // weights := px
    let a = sp.previousElementSibling, b = sp.nextElementSibling;
    while (a && !(visiblePane(a) && !a.classList.contains("collapsed"))) a = a.previousElementSibling;
    while (b && !(visiblePane(b) && !b.classList.contains("collapsed"))) b = b.nextElementSibling;
    return a && b ? { a, b, ha: a.getBoundingClientRect().height, hb: b.getBoundingClientRect().height } : null;
  };
  const paneMove = (s, dy) => {
    if (!s) return;
    const tot = s.ha + s.hb;
    const ha = Math.max(60, Math.min(tot - 60, s.ha + dy));
    s.a.style.setProperty("--w", ha);
    s.b.style.setProperty("--w", tot - ha);
  };
  const paneEnd = () => {
    for (const p of $("sidebar").children) {
      if (visiblePane(p) && !p.classList.contains("collapsed")) prefs.w[p.dataset.pane] = Math.round(p.getBoundingClientRect().height);
    }
  };
  for (const sp of document.querySelectorAll(".split-p")) {
    drag(sp, "h", () => paneStart(sp), (s, _dx, dy) => paneMove(s, dy), paneEnd);
    splitKeys(sp, (d) => { paneMove(paneStart(sp), -d); paneEnd(); }, "y");
  }
}

// keyboard resizing of a focused splitter: arrows move it by 10 px (Shift: 50 px)
function splitKeys(el, move, axis) {
  el.addEventListener("keydown", (e) => {
    const k = e.key;
    const keys = axis === "x" ? { ArrowLeft: 1, ArrowRight: -1 } : { ArrowUp: 1, ArrowDown: -1 };
    if (!(k in keys)) return;
    e.preventDefault();
    e.stopPropagation();
    move(keys[k] * (e.shiftKey ? 50 : 10));
    savePrefs();
    window.dispatchEvent(new Event("resize"));
  });
}

// pointer-drag helper for splitters: start() -> state, move(state, dx, dy), end(state)
function drag(handle, dir, start, move, end) {
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const s = start();
    const x0 = e.clientX, y0 = e.clientY;
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("drag");
    document.body.classList.add("dragging", `dragging-${dir}`);
    const mv = (ev) => move(s, ev.clientX - x0, ev.clientY - y0);
    const up = () => {
      handle.removeEventListener("pointermove", mv);
      handle.classList.remove("drag");
      document.body.classList.remove("dragging", `dragging-${dir}`);
      if (end) end(s);
      savePrefs();
      window.dispatchEvent(new Event("resize"));
    };
    handle.addEventListener("pointermove", mv);
    handle.addEventListener("pointerup", up, { once: true });
    handle.addEventListener("pointercancel", up, { once: true });
  });
}

// ---------------------------------------------------------------- boot
async function boot() {
  const info = await api("/api/chip");
  ui.info = info;
  ui.boot = info.boot;
  ui.chips = info.chips && info.chips.length ? info.chips
    : [{ ...info, id: "A", uart_ids: info.uarts || [], pin_count: (info.pins || []).length }];
  ui.isBoard = !!info.board;
  for (const c of ui.chips) {
    ui.chipById.set(c.id, c);
    ui.metaByChip.set(c.id, new Map(c.pins.map((p) => [p.num, p])));
    ui.chipsOn.add(c.id);
  }
  ui.chipsOn.add("board");
  ui.sel = info.view && ui.chipById.has(info.view) ? info.view : ui.chips[0].id;
  document.body.classList.toggle("board-mode", ui.isBoard);
  initPanes();

  if (ui.isBoard) {
    document.title = `chipem · ${info.board.name}`;
    $("chipName").textContent = `board ${info.board.name}`;
    $("chipMeta").textContent = ui.chips.map((c) => `${c.id}: ${c.name}${c.firmware ? ` (${c.firmware})` : ""}`).join(" · ");
    $("pkgTitle").textContent = "Board";
    $("pkgHint").textContent = "Canvas: wheel zooms, drag pans, double-click fits; hover a pin for its data, click for the pin card, alt-click to drive it H → L → open; click a chip body to select and zoom; W: wire mode.";
  } else {
    const c = info;
    document.title = `chipem · ${c.name}`;
    $("chipName").textContent = c.name;
    $("chipMeta").textContent = [c.package, c.firmware ? `${c.firmware}` : "no firmware",
      c.load_addr != null ? `@ ${hex32(c.load_addr)}` : null].filter(Boolean).join(" · ");
  }

  document.querySelector(".topbar .title").title = `${$("chipName").textContent} · ${$("chipMeta").textContent}`;
  ui.wiring = (info.board && info.board.wiring) || 0;
  ui.board = new BoardView($("pkg"), { ...info, chips: ui.chips }, {
    onPinClick: clickPin, onPartLeave: hidePartTip,
    onBodyClick: (chip) => { selectChip(chip.id); ui.board.zoomTo(chip.id); },
    onNetHover: (name, on) => hoverNet(name, on),
    onNetClick: (name) => selectNet(ui.selNet === name ? null : name),
    onBackgroundClick: () => { if (ui.selNet) selectNet(null); },
    onPartAction: (part, values) => setPart(part.id, values),
    onPortClick: (part, port) => clickPort(part, port),
    onPortHover: showPortTip, onPartHover: showPartTip,
    onPartClick: (part) => focusPartRow(part.id),
    onPinSelect: (chip, pin) => selectPinRow(chip.id, pin.num, true),
    onTracePin: (cid, num) => toggleTrace(cid, num),
    onError: (msg) => toastErr(msg),
    onBusClick: (bus) => focusBusRow(bus && (bus.name ?? bus)),
  });
  ui.board.setPinClickHandler?.(clickPin);
  ui.wiringSig = wiringSig(info);

  buildChipSelector();
  buildUartSelector();
  buildKindFilter();
  buildLinkMonitor();
  buildWiringPanel();
  buildPartsPanel();
  wireControls();
  initDataTools();
  selectChip(ui.sel, true);

  initLogic();
  initDebug();

  const st = await api("/api/state");
  applyState(st);
  // recent history: the last ~3000 events of every kind (rings per kind; only visible kinds become rows)
  const seq0 = Math.max(0, (st.event_seq || 0) - 3000);
  const hist = await api(`/api/events?since=${seq0}&limit=0`);
  ui.lastEventSeq = hist.last || st.event_seq || 0;
  // UART transcript: built by the server from the kept uart events (one row per line, not one event per byte)
  let haveLines = false;
  try {
    const u = await api("/api/uart/lines");
    loadLines(u);
    haveLines = true;
    ui.lastEventSeq = Math.max(ui.lastEventSeq, u.last || 0);
  } catch (e) {
    if (e.status !== 404) console.warn("uart lines", e);
  }
  if (!haveLines) {                                   // older server: one event per byte
    try {
      const u = await api(`/api/events?since=0&kinds=uart&limit=0`);
      for (const ev of u.events) if (ev.seq <= ui.lastEventSeq) uartEvent(ev, false);
    } catch { /* optional */ }
  }
  for (const ev of hist.events) {
    if (ev.seq > ui.lastEventSeq) continue;
    if (ev.kind === "part") partEvent(ev);
    ui.evQueue.push(ev);
  }
  ui.uartDirty = true;
  ui.uartFull = true;
  schedule();
  refreshUart(true);
  connect();
}

// /api/uart/lines -> transcript state (ui.lines, open lines, CR state, drop counters)
function loadLines(u) {
  ui.lines = [];
  ui.openLine.clear();
  ui.afterCR.clear();
  for (const l of u.lines || []) {
    const ln = { uart: l.uart, chip: l.chip ?? evChip({}), text: l.text || "", t: l.t };
    ui.lines.push(ln);
    if (l.open) ui.openLine.set(l.uart, ln);
  }
  if (ui.lines.length > MAX_LINES) ui.lines.splice(0, ui.lines.length - MAX_LINES);
  for (const [k, v] of Object.entries(u.after_cr || {})) ui.afterCR.set(k, !!v);
  for (const [k, v] of Object.entries(u.drops || {})) ui.uartDrops.set(k, { n: v.n || 0, reason: v.reason || "?" });
  showUartDrops();
}

// ---------------------------------------------------------------- chip selection
function buildChipSelector() {
  const box = $("chipSel");
  box.hidden = !ui.isBoard;
  if (!ui.isBoard) return;
  for (const c of ui.chips) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip-tab";
    b.dataset.chip = c.id;
    b.setAttribute("role", "tab");
    b.title = `${c.name} · ${c.package}${c.firmware ? ` · ${c.firmware}` : ""}`;
    b.innerHTML = `<b class="${chipCls(c.id)}">${esc(c.id)}</b><span class="cn"> ${esc(c.name)}</span>`;
    b.addEventListener("click", () => selectChip(c.id));
    box.appendChild(b);
  }
  tabKeys(box, (b) => selectChip(b.dataset.chip));
}

// arrow keys between the tabs of a tablist (roving tabindex), activating the focused one
function tabKeys(list, activate) {
  list.addEventListener("keydown", (e) => {
    const tabs = [...list.querySelectorAll("[role=tab]")].filter((t) => !t.hidden);
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") j = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = tabs.length - 1;
    if (j == null) return;
    e.preventDefault();
    e.stopPropagation();
    tabs[j].focus();
    activate(tabs[j]);
  });
}

function selectChip(cid, initial = false) {
  if (!ui.chipById.has(cid)) return;
  const changed = ui.sel !== cid;
  ui.sel = cid;
  for (const b of $("chipSel").children) {
    const on = b.dataset.chip === cid;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  ui.board.setSelected(cid);
  ui.debug?.setChip?.(cid);
  ui.regview?.setChip?.(cid);
  const c = ui.chipById.get(cid);
  $("pinsChip").textContent = ui.isBoard ? `chip ${cid} · ${c.name}` : "";
  $("btnStep").title = `Execute N instructions on ${ui.isBoard ? `chip ${cid} (the other chips do not move)` : "the chip"} (S)`;
  if (changed || initial) {
    $("pinTable").tBodies[0]._sig = null;
    renderPinTable();
    if (ui.state) renderReadouts(ui.state);
    if (ui.discOn) loadDiscovery();
  }
}

// ---------------------------------------------------------------- live stream, offline state
function setOffline(on) {
  if (ui.offline === on) return;
  ui.offline = on;
  document.body.classList.toggle("offline", on);
  const conn = $("conn");
  conn.className = `conn ${on ? "bad" : "ok"}`;
  conn.textContent = on ? "offline" : "live";
  for (const b of document.querySelectorAll(".run-ctl")) b.disabled = on || b.disabled;
  if (on) {
    const s = $("status");
    s.textContent = "OFFLINE";
    s.className = "status-badge fault";
    s.title = "the emulator server is not reachable; reconnecting…";
    $("tSpeed").textContent = "—";
    toast("server not reachable: reconnecting…", "error");
  } else if (ui.state) {
    renderReadouts(ui.state);
  }
}

function connect() {
  if (ui.es) ui.es.close();
  const es = new EventSource(`/api/stream?since=${ui.lastEventSeq}${ui.kindsOn.has("mmio") ? "&mmio=1" : ""}`);
  ui.es = es;
  ui.pinsFull = false;                          // the first frame of a stream carries every pin row
  es.addEventListener("open", () => { setOffline(false); $("conn").className = "conn ok"; $("conn").textContent = "live"; });
  es.addEventListener("error", () => {
    if (es.readyState === EventSource.CLOSED) setTimeout(() => { if (ui.es === es) connect(); }, 2000);
    setOffline(true);
  });
  // handlers only queue work; rendering happens once per animation frame
  es.addEventListener("state", (m) => {
    const st = JSON.parse(m.data);
    if (ui.offline) setOffline(false);
    if (st.boot && ui.boot && st.boot !== ui.boot) { location.reload(); return; }   // the server was restarted
    const prev = ui.pendingState;
    if (prev) {                                       // not rendered yet: keep its pin rows / waves
      if (st.pins && !st.pins_full && prev.pins) st.pins = mergeRows(prev.pins, st.pins);
      else if (!st.pins && prev.pins) { st.pins = prev.pins; st.pins_full = prev.pins_full; }
      if (!st.waves && prev.waves) st.waves = prev.waves;
      if (!st.periph && prev.periph) st.periph = prev.periph;
    }
    ui.pendingState = st;
    schedule();
  });
  es.addEventListener("event", (m) => {
    const ev = JSON.parse(m.data);
    if (ev.seq && ev.seq <= ui.lastEventSeq) return;
    ui.lastEventSeq = Math.max(ui.lastEventSeq, ev.seq || 0);
    if (ev.kind === "uart") uartEvent(ev, true);
    else if (ev.kind === "part") partEvent(ev);
    ui.evQueue.push(ev);
    if (ui.evQueue.length > 8 * MAX_EVENT_ROWS) ui.evQueue.splice(0, ui.evQueue.length - 4 * MAX_EVENT_ROWS);
    schedule();
  });
}

// two delta lists of pin rows: later rows win
function mergeRows(a, b) {
  const m = new Map(a.map((r) => [keyOf(r.chip ?? ui.chips[0].id, r.num), r]));
  for (const r of b) { const k = keyOf(r.chip ?? ui.chips[0].id, r.num); m.set(k, { ...(m.get(k) || {}), ...r }); }
  return [...m.values()];
}

function schedule() {
  if (ui.rafPending) return;
  ui.rafPending = true;
  const run = () => {
    ui.rafPending = false;
    if (ui.pendingState) { const st = ui.pendingState; ui.pendingState = null; applyState(st); }
    if (ui.evQueue.length) { const q = ui.evQueue; ui.evQueue = []; addEvents(q); }
    if (ui.uartDirty) { ui.uartDirty = false; renderTranscript(); renderLinkMonitor(); }
    if (ui.partsDirty) { ui.partsDirty = false; ui.board.updateParts(ui.partState); renderPartStates(); }
  };
  // rAF does not fire in background tabs: fall back to a timer so the queues still drain
  if (document.hidden) setTimeout(run, 200); else requestAnimationFrame(run);
}

function applyState(st) {
  const prevStop = ui.state ? ui.state.last_stop : null;
  // a frame without pins keeps the last rows (the stream only sends rows that changed)
  ui.state = { ...st, pins: undefined };
  if (st.pins) applyPins(st);
  if (st.waves) { ui.waves = st.waves; ui.wavesDirty = true; }
  if (st.adc) { ui.adc = st.adc; ui.wavesDirty = true; ui.board.setAdcValues?.(st.adc); }
  if (st.periph) ui.periph = st.periph;
  if (ui.wavesDirty && performance.now() - (ui._waveAt || 0) >= WAVE_MS) { ui._waveAt = performance.now(); ui.wavesDirty = false; renderWaveCells(); }
  if (ui.discOn && st.last_stop !== ui._lastStop && !st.running) loadDiscovery();   // run finished: refresh
  ui._lastStop = st.last_stop;
  if (st.last_stop === "RESET" && prevStop !== "RESET") ui.logic?.reset?.();
  ui.logic?.setTime?.(st.board ? st.board.t_ns : (st.t_ns ?? st.time_ns));
  if (st.pins && ui.logic?.setLevels && ui.traced.size) {       // current levels: traced pins without logged changes still draw
    const lv = {};
    for (const k of ui.traced) { const r = ui.rowByKey.get(k); if (r) lv[k] = r.level || "X"; }
    ui.logic.setLevels(lv);
  }
  renderReadouts(st);
  renderStatus(st);
  renderPerf(st);
  ui.debug?.onState?.(st);
  ui.regview?.onState?.(st);
  if (st.board && !ui.isBoard && !ui._reloading) {             // a part was added elsewhere: board layout
    ui._reloading = true;
    location.reload();
    return;
  }
  if (st.board) {
    if ((st.board.wiring ?? 0) !== ui.wiring) reloadWiring(st.board.wiring ?? 0);
    if (st.board.parts) Object.assign(ui.partState, partStateMap(st.board.parts));
    if (Array.isArray(st.board.parts)) for (const p of st.board.parts) if (p.views) ui.partViews[p.id] = p.views;
    ui.board.updateBoard(st.board);
    renderWiringLevels(st.board);
    renderBusStats(st.board);
    renderPartStates();
  }
  // UART output grew?
  const lens = st.uart_out_len || {};
  if (!ui.uartPicked && !(lens[ui.uart] > 0)) {                 // follow the UART that prints (both views)
    const opts = new Set([...$("uartSel").options].map((o) => o.value));
    const best = Object.entries(lens).find(([u, n]) => n > 0 && opts.has(u));
    if (best) {
      ui.uart = best[0]; $("uartSel").value = best[0]; showUartDrops();
      ui.uartFull = true; ui.uartDirty = true;
      refreshUart(true);
    }
  }
  if (ui.uartMode === "raw" && ui.uart && lens[ui.uart] !== undefined && lens[ui.uart] !== ui.uartLen[ui.uart]) refreshUart(false);
}

// virtual time per wall second, averaged over >= 0.5 s of running
function updateSpeed(st) {
  const t = st.board ? st.board.t_ns : (st.t_ns ?? st.time_ns);
  const now = performance.now();
  if (!st.running || t == null) { ui.spd = null; return "—"; }
  if (!ui.spd || t < ui.spd.t) { ui.spd = { t, wall: now, x: null }; return "…"; }
  const dw = now - ui.spd.wall;
  if (dw >= 500) {
    ui.spd.x = (t - ui.spd.t) / (dw * 1e6);
    ui.spd.t = t; ui.spd.wall = now;
  }
  const x = ui.spd.x;
  if (x == null) return "…";
  return `×${x >= 10 ? x.toFixed(0) : x >= 0.01 ? x.toPrecision(2) : x.toExponential(1)}`;
}

// Pin rows: a full set (`pins_full`, /api/state) replaces everything; a delta (the stream) updates the rows it names.
// Rows keep the static metadata out (it comes once from /api/chip): {chip, num, level, owner, direction, ...}.
function applyPins(st) {
  // full set: flagged by the stream, /api/state (no flag) or the first rows we see
  const full = st.pins_full === true || !("pins_full" in st) || !ui.rowByKey.size;
  const changed = new Map(ui.chips.map((c) => [c.id, new Set()]));
  const changedSel = new Set();
  const seen = new Set();
  for (const r0 of st.pins) {
    const cid = r0.chip ?? ui.chips[0].id;
    const k = keyOf(cid, r0.num);
    seen.add(k);
    let r = ui.rowByKey.get(k);
    if (r) Object.assign(r, r0); else { r = { ...r0, chip: cid }; ui.rowByKey.set(k, r); }
    if (r0.wave !== undefined) ui.waves[k] = r0.wave;           // old servers / /api/state: wave in the row
    const rk = rowKey(r);
    const prev = ui.prevKey.get(k);
    if (prev !== rk) {
      if (prev !== undefined) { if (!changed.has(cid)) changed.set(cid, new Set()); changed.get(cid).add(r.num); }
      if (cid === ui.sel) changedSel.add(r.num);
      ui.prevKey.set(k, rk);
    }
  }
  if (full) {
    for (const k of [...ui.rowByKey.keys()]) if (!seen.has(k)) { ui.rowByKey.delete(k); ui.prevKey.delete(k); }
    const byChip = new Map(ui.chips.map((c) => [c.id, []]));
    for (const [k, r] of ui.rowByKey) {
      const cid = r.chip;
      if (!byChip.has(cid)) byChip.set(cid, []);
      byChip.get(cid).push(r);
    }
    for (const list of byChip.values()) list.sort((a, b) => a.num - b.num);
    ui.rows = byChip;
  }
  if (!ui.baseline || (st.last_stop === "RESET" && ui._lastStopPins !== "RESET")) {
    ui.baseline = new Map([...ui.rowByKey].map(([k, r]) => [k, rowKey(r)]));
  }
  ui._lastStopPins = st.last_stop;
  // board.js keeps every chip's row list (link endpoints); PackageView only rewrites what changed
  if (full || [...changed.values()].some((x) => x.size)) ui.board.update(ui.rows, changed);
  renderPinTable(changedSel);
}

function statusOf(st) {
  let label = "paused", cls = "paused", word = "PAUSED", extra = "";
  if (st.running) {
    extra = st.run_target_ns != null ? `→ ${fmtTime(st.run_target_ns)}` : (st.realtime ? "real-time" : "");
    label = `running${extra ? ` ${extra}` : ""}`;
    cls = "running"; word = "RUNNING";
  } else if (st.error) { label = "error"; cls = "fault"; word = "ERROR"; }
  else if (st.lockup || st.last_stop === "FAULT") { label = "fault"; cls = "fault"; word = "FAULT"; }
  else if (st.sleeping) { label = "paused (core asleep)"; extra = "asleep"; }
  return { label, cls, word, extra };
}

function renderReadouts(st) {
  if (ui.offline) return;
  const cs = chipState(st, ui.sel) || st;
  const t = st.board ? st.board.t_ns : (st.t_ns ?? st.time_ns);
  $("tTime").textContent = fmtTime(t);
  renderSpeed(st);
  $("tInsn").textContent = cs.insns != null ? cs.insns.toLocaleString() : "—";
  $("tCycles").textContent = (cs.cycles ?? 0).toLocaleString();
  $("tClock").textContent = cs.hz ? fmtFreq(cs.hz) : "—";
  $("tPc").textContent = cs.pc != null ? hex32(cs.pc) : "—";
  const { label, cls, word, extra } = statusOf(st);
  let lab = label;
  if (cs.mode === "handler" && cs.active_name) lab += ` · in ${cs.active_name}`;
  const s = $("status");
  s.textContent = `${word}${extra ? ` ${extra}` : ""}${cs.mode === "handler" && cs.active_name ? ` · ${cs.active_name}` : ""}`;
  s.className = `status-badge ${cls}`;
  s.title = [lab, ui.isBoard ? `chip ${ui.sel}` : "", st.error || "", st.board ? `board skew ${fmtTime(st.board.skew_ns)}` : "",
    st.last_stop ? `last stop ${st.last_stop}` : ""].filter(Boolean).join(" · ");
  // emulator error / fault detail inline (click copies the full text)
  const err = st.error || (cls === "fault" ? [cs.fault || cs.fault_reason || cs.lockup_reason, st.last_stop === "FAULT" ? `stop FAULT at ${cs.pc != null ? hex32(cs.pc) : "?"}` : ""].filter(Boolean).join(" · ") : "");
  const et = $("errText");
  et.hidden = !err;
  if (err && et.dataset.full !== err) { et.textContent = err; et.dataset.full = err; et.title = `${err}\nclick: copy`; }
  for (const c of ui.chips) {
    const pv = ui.board.pkgs.get(c.id);
    if (!pv) continue;
    const s2 = chipState(st, c.id) || st;
    let l2 = statusOf({ ...st, lockup: s2.lockup, sleeping: s2.sleeping }).label;
    if (s2.mode === "handler" && s2.active_name) l2 += ` · in ${s2.active_name}`;
    pv.setStatus(fmtTime(s2.t_ns ?? t), l2, !!st.running, ui.isBoard && s2.pc != null ? `pc ${hex32(s2.pc)}` : "");
  }
  $("btnRun").disabled = !!st.running;
  $("btnPause").disabled = !st.running;
  for (const id of ["btnStep", "stepN", "btnReset", "btnRestart", "runFor", "btnRunFor"]) $(id).disabled = false;
  if (!$("regsPop").hidden) renderRegs();
}

// CPU registers of the selected chip from the state frame (popover on the pc readout)
const REG_ORDER = ["pc", "sp", "lr", "msp", "psp", "xpsr", "control", "primask", "basepri", "faultmask", "mode", "active",
  "active_name", "pending", "sleeping", "lockup", "insns", "cycles", "hz"];
function regRows() {
  const cs = chipState(ui.state, ui.sel) || ui.state || {};
  const skip = new Set(["chips", "board", "pins", "running", "chip", "host", "periph", "waves", "adc", "uart_out_len",
    "version", "pins_version", "event_seq", "boot", "run_target_ns", "last_stop", "error", "realtime", "t_ns", "pins_full", "time_ns"]);
  // registers: width / format from the server (info.chips[].reg_meta, chipem/regmeta.py); other state fields as is.
  // Order: the registers (in the server's order, ARM ones by REG_ORDER), then the state fields.
  const meta = ui.chipById.get(ui.sel)?.reg_meta || {};
  const isReg = (k) => !!meta[k];
  const first = [...REG_ORDER.filter((k) => k in cs && isReg(k)), ...Object.keys(meta).filter((k) => k in cs && !REG_ORDER.includes(k))];
  const keys = [...first, ...REG_ORDER.filter((k) => k in cs && !first.includes(k)),
    ...Object.keys(cs).filter((k) => !first.includes(k) && !REG_ORDER.includes(k) && !skip.has(k))];
  const val = (k, v) => {
    if (v == null) return "—";
    if (meta[k]) return fmtReg(v, meta[k]);
    if (typeof v === "object") return JSON.stringify(v);
    return typeof v === "number" ? v.toLocaleString() : String(v);
  };
  return keys.map((k) => [k, val(k, cs[k])]);
}
function renderRegs() {
  $("regsTitle").textContent = `CPU ${ui.isBoard ? `chip ${ui.sel}` : ""}`;
  const html = regRows().map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
  if ($("regsTable")._h !== html) { $("regsTable").innerHTML = html; $("regsTable")._h = html; }
}

// ---------------------------------------------------------------- pins
function nextLevel(ext) {
  return ext === "H" ? "L" : ext === "L" ? null : "H";
}

async function clickPin(chip, pin) {
  const cid = chip.id;
  if (ui.pickTarget) { pickDone(`${cid}.${pin.short}`); return; }
  if (ui.wireMode) { wirePick(chip, pin); return; }
  if (ui.isBoard && cid !== ui.sel) selectChip(cid);
  if (pin.kind !== "gpio") { toastErr(`${qual(cid, pin.short)} is a ${pin.kind} pin and cannot be driven`); return; }
  const k = keyOf(cid, pin.num);
  const row = ui.rowByKey.get(k) || {};
  const level = nextLevel(row.external ?? null);
  const r = await act("/api/pin", { name: pin.qname || qual(cid, pin.short), level });
  if (r) {
    row.external = r.external; row.level = r.level;
    ui.board.pkgs.get(cid)?.update([{ ...row, num: pin.num }]);
    if (cid === ui.sel) { $("pinTable").tBodies[0]._keys?.delete(pin.num); renderPinTable(new Set([pin.num])); }
  }
}

// a level only means something on digital pins: power / analog / special pins with no level show "—"
function levelCell(r, p) {
  const oc = ownerCat(p, r);
  const ana = oc === "analog" || r.direction === "analog";
  if ((p.kind !== "gpio" && (!r.level || r.level === "X")) || oc === "power" || (ana && (!r.level || r.level === "X"))) {
    return `<span class="na" title="no digital level">—</span>`;
  }
  const lv = r.level || "X";
  return `<span class="badge ${lv}">${lv}</span>`;
}

// analog input value of a pin (state `adc`, keyed by the channel name in /api/chip pins[].adc)
function analogOf(cid, p) {
  if (!p.adc) return null;
  const m = ui.adc[cid] || {};
  const v = m[p.adc] ?? m[String(p.adc).toUpperCase()];
  return v === undefined ? null : v;
}
const fmtAnalog = (v) => (v == null ? "" : typeof v === "number" && !Number.isInteger(v) ? `${+v.toFixed(4)} V` : String(v));

function waveCell(cid, num) {
  const w = ui.waves[keyOf(cid, num)];
  return `<td class="wave" title="${w ? `${w.edges} edges` : ""}">${esc(fmtWave(w))}</td>`;
}
function anaCell(cid, p) {
  if (!p.adc) return `<td class="ana"></td>`;
  return `<td class="ana edit" tabindex="0" title="${esc(p.adc)}: click or Enter to set (integer = raw code, 1.65V / 1200mV = volts)">${esc(fmtAnalog(analogOf(cid, p))) || "—"}</td>`;
}

function pinCells(r, p, cid) {
  const oc = ownerCat(p, r);
  const drive = p.kind === "gpio";
  return `<td class="num">${r.num}</td><td>${esc(p.short)}</td>
      <td class="owner-${oc}">${esc(r.owner)}</td><td>${esc(r.direction)}</td>
      <td>${levelCell(r, p)}</td>${waveCell(cid, r.num)}${anaCell(cid, p)}<td>${esc(r.pull ?? "—")}</td>
      <td class="ext"${drive ? ` tabindex="0" title="click or Enter: cycle H → L → open"` : ""}>${r.external ? `<span class="badge ${r.external}">${r.external}</span>` : "—"}</td>
      <td class="net">${esc(r.net ?? "")}</td><td class="name">${esc(p.name)}</td>`;
}

// wave / analog columns: refreshed at most every WAVE_MS (not part of the row change detection)
function renderWaveCells() {
  const tb = $("pinTable").tBodies[0];
  if (!tb._rows) return;
  const cid = ui.sel, meta = ui.metaByChip.get(cid);
  for (const [num, tr] of tb._rows) {
    const w = tr.cells[5], a = tr.cells[6];
    if (!w || !a) continue;
    const wv = ui.waves[keyOf(cid, num)];
    const t = fmtWave(wv);
    if (w.textContent !== t) { w.textContent = t; w.title = wv ? `${wv.edges} edges` : ""; }
    const p = meta.get(num);
    if (p && p.adc && document.activeElement?.parentElement !== a) {
      const at = fmtAnalog(analogOf(cid, p)) || "—";
      if (a.textContent !== at) a.textContent = at;
    }
  }
  if (ui.sort.k === "wave" || ui.sort.k === "analog") { tb._sig = null; renderPinTable(); }
}

// Incremental: the row set/order is rebuilt only when filter, sort or membership change; otherwise only the
// rows whose content changed are rewritten.
function renderPinTable(changed = new Set()) {
  const tb = $("pinTable").tBodies[0];
  const cid = ui.sel;
  const meta = ui.metaByChip.get(cid);
  const all = ui.rows.get(cid) || [];
  if (!meta) return;
  const m = $("pinFilter")._m;
  const changedOnly = $("changedOnly").checked;
  const hideDis = $("hideDisabled").checked;
  let rows = all.filter((r) => {
    const p = meta.get(r.num);
    if (!p) return false;
    if (hideDis && (p.kind === "power" || r.owner === "disabled")) return false;
    if (changedOnly && ui.baseline && ui.baseline.get(keyOf(cid, r.num)) === rowKey(r)) return false;
    if (m) {
      const hay = [p.short, p.name, r.owner, r.direction, r.net, p.adc, ...Object.values(p.functions || {})].join(" ");
      if (!m.test(hay)) return false;
    }
    return true;
  });
  const { k, desc } = ui.sort;
  const val = (r) => {
    const p = meta.get(r.num);
    if (k === "wave") return ui.waves[keyOf(cid, r.num)]?.freq_hz ?? -1;
    if (k === "analog") { const v = analogOf(cid, p); return v == null ? -1 : +v; }
    if (k === "short") return shortKey(p.short);
    if (k === "name") return p.name ?? "";
    return r[k] ?? "";
  };
  rows.sort((a, b) => {
    const va = val(a), vb = val(b);
    const c = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
    return (desc ? -c : c) || a.num - b.num;
  });
  ui.pinVisible = rows;
  $("pinCount").textContent = rows.length === all.length ? `${all.length}` : `${rows.length}/${all.length}`;
  const sig = cid + "|" + rows.map((r) => r.num).join(",");
  const selNum = ui.selPin && ui.selPin.startsWith(`${cid}:`) ? +ui.selPin.slice(cid.length + 1) : null;
  if (tb._sig !== sig) {
    tb.innerHTML = rows.map((r) => {
      const p = meta.get(r.num);
      return `<tr data-num="${r.num}" class="${changed.has(r.num) ? "flash" : ""}${p.kind === "gpio" ? " clickable" : ""}${r.num === selNum ? " sel" : ""}">${pinCells(r, p, cid)}</tr>`;
    }).join("");
    tb._sig = sig;
    tb._rows = new Map([...tb.rows].map((tr) => [+tr.dataset.num, tr]));
    tb._keys = new Map(rows.map((r) => [r.num, rowKey(r)]));
  } else if (changed.size) {
    for (const r of rows) {
      if (!changed.has(r.num)) continue;
      const rk = rowKey(r);
      if (tb._keys.get(r.num) === rk) continue;
      const tr = tb._rows.get(r.num);
      if (!tr) continue;
      tr.innerHTML = pinCells(r, meta.get(r.num), cid);
      tr.classList.remove("flash");
      void tr.offsetWidth;            // restart the flash animation
      tr.classList.add("flash");
      tb._keys.set(r.num, rk);
    }
  }
  for (const th of $("pinTable").tHead.rows[0].cells) {
    const on = th.dataset.k === k;
    th.classList.toggle("sorted", on);
    th.classList.toggle("desc", on && desc);
    th.setAttribute("aria-sort", on ? (desc ? "descending" : "ascending") : "none");
  }
}

function shortKey(s) {   // natural sort: PTA2 < PTA10, GPIO2 < GPIO10
  return String(s).replace(/\d+/g, (d) => d.padStart(4, "0"));
}

// select a pin row (table click, canvas pin card): highlight, scroll into view, flash
function selectPinRow(cid, num, fromCanvas = false) {
  if (ui.isBoard && cid !== ui.sel) selectChip(cid);
  ui.selPin = keyOf(cid, num);
  const tb = $("pinTable").tBodies[0];
  for (const tr of tb.querySelectorAll("tr.sel")) tr.classList.remove("sel");
  const tr = tb._rows && tb._rows.get(num);
  if (!tr) return;
  tr.classList.add("sel");
  if (fromCanvas) {
    tr.scrollIntoView({ block: "nearest" });
    tr.classList.remove("flash"); void tr.offsetWidth; tr.classList.add("flash");
  }
}

// analog value editor in the pin table: integer = raw code, "1.65V" / "1200mV" (or a decimal) = volts (/api/adc)
function editAnalog(td, cid, p) {
  if (td.querySelector("input")) return;
  const cur = analogOf(cid, p);
  td.innerHTML = `<input class="ana-in" type="text" value="${esc(cur == null ? "" : typeof cur === "number" && !Number.isInteger(cur) ? `${cur}V` : cur)}" spellcheck="false" placeholder="2048 | 1.65V" aria-label="analog value of ${esc(p.short)}">`;
  const inp = td.querySelector("input");
  inp.focus(); inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const v = inp.value.trim();
    if (save && v) {
      const r = await act("/api/adc", { channel: ui.isBoard ? `${cid}.${p.adc}` : p.adc, value: v });
      if (r) {
        (ui.adc[cid] ||= {})[p.adc] = r.value;
        toast(`${p.adc} = ${fmtAnalog(r.value)}`);
      }
    }
    td.textContent = fmtAnalog(analogOf(cid, p)) || "—";
    td.focus();
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
  });
  inp.addEventListener("blur", () => finish(true));
}

// ---------------------------------------------------------------- debugger + register browser (debug.js, regview.js)
async function initDebug() {
  const opts = () => ({ api, toast, toastErr, chip: ui.sel, isBoard: ui.isBoard,
    regMeta: (cid) => ui.chipById.get(cid || ui.sel)?.reg_meta || {} });
  try {
    const { DebugPane } = await import("./debug.js");
    ui.debug = new DebugPane($("debugPane"), opts());
  } catch (e) { $("debugPane").innerHTML = `<div class="empty">debugger: ${esc(e.message)}</div>`; }
  try {
    const { RegView } = await import("./regview.js");
    ui.regview = new RegView($("regsPane"), opts());
  } catch (e) { $("regsPane").innerHTML = `<div class="empty">registers: ${esc(e.message)}</div>`; }
  if (ui.tab === "debug") ui.debug?.show(true);
  if (ui.tab === "regs") ui.regview?.show(true);
}

// ---------------------------------------------------------------- logic strip (logic.js, optional)
async function initLogic() {
  ui.traced = new Set(prefs.traced?.[fileBase()] || []);
  let mod = null;
  try { mod = await import("./logic.js"); } catch { mod = null; }
  const box = $("logicPane");
  if (!mod || !mod.LogicStrip) {
    box.innerHTML = `<div class="empty">logic strip not available (logic.js)</div>`;
    return;
  }
  try {
    ui.logic = new mod.LogicStrip(box, {
      fetchJson: (u) => api(u),
      onChange: (pins) => {
        ui.traced = new Set(pins.map((p) => keyOf(p.cid, p.num)));
        (prefs.traced ||= {})[fileBase()] = [...ui.traced];
        savePrefs();
        $("logicCount").textContent = ui.traced.size ? `${ui.traced.size}` : "";
        ui.board.setTraced?.(ui.traced);
      },
    });
  } catch (e) { box.innerHTML = `<div class="empty">logic strip: ${esc(e.message)}</div>`; return; }
  const pins = [...ui.traced].map(tracePin).filter(Boolean);
  if (pins.length) ui.logic.setPins(pins);
  $("logicCount").textContent = ui.traced.size ? `${ui.traced.size}` : "";
  ui.board.setTraced?.(ui.traced);
}

function tracePin(key) {
  const i = key.lastIndexOf(":");
  const cid = key.slice(0, i), num = +key.slice(i + 1);
  const p = ui.metaByChip.get(cid)?.get(num);
  if (!p) return null;
  return { cid, num, label: qual(cid, p.short), qname: qual(cid, p.short) };
}

function toggleTrace(cid, num) {
  const k = keyOf(cid, num);
  const p = tracePin(k);
  if (!p) return;
  if (!ui.logic) { toastErr("logic strip not available"); return; }
  ui.logic.toggle(p);
  const on = ui.logic.has ? !!ui.logic.has(cid, num) : !ui.traced.has(k);
  if (on) ui.traced.add(k); else ui.traced.delete(k);
  (prefs.traced ||= {})[fileBase()] = [...ui.traced];
  savePrefs();
  $("logicCount").textContent = ui.traced.size ? `${ui.traced.size}` : "";
  ui.board.setTraced?.(ui.traced);
  toast(`${p.label}: ${on ? "traced (Logic tab)" : "trace removed"}`);
}

// ---------------------------------------------------------------- events
// Only events of the switched-on kinds and chips become DOM rows (at most MAX_EVENT_ROWS). Every kind keeps a
// bounded ring of its recent events in memory; switching a kind on rebuilds the view from the rings (and refetches the
// kind from the server's buffer first). Consecutive identical rows (kind + chip + text) fold into one with ×N.
function buildKindFilter() {
  const box = $("kindFilter");
  const mk = (label, cls, on, toggle, title) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `chip-toggle ${cls}` + (on ? " on" : "");
    b.setAttribute("aria-pressed", String(on));
    b.innerHTML = `${esc(label)}<span class="n"></span>`;
    b.title = title || "";
    b.addEventListener("click", () => {
      const v = toggle();
      b.classList.toggle("on", v);
      b.setAttribute("aria-pressed", String(v));
      renderKindChips();
    });
    box.appendChild(b);
    return b;
  };
  ui.kindBtn = {};
  for (const k of KINDS) {
    ui.kindBtn[k] = mk(k, `k-${k}`, ui.kindsOn.has(k), () => {
      const on = !ui.kindsOn.has(k);
      if (on) ui.kindsOn.add(k); else ui.kindsOn.delete(k);
      if (k === "mmio") connect();              // the stream sends mmio events only on request
      if (on) refetchKind(k); else rebuildEvents();
      return on;
    }, k === "mmio" ? "MMIO accesses (only logged when the session logs them; off by default)" : `show ${k} events`);
  }
  // kinds without events (and switched off) hide behind one "+N" chip
  ui.kindMore = mk("+", "more", false, () => { ui.kindsAll = !ui.kindsAll; return ui.kindsAll; },
    "show the event kinds that have no events yet");
  if (ui.isBoard) {
    const sep = document.createElement("span");
    sep.className = "sep";
    box.appendChild(sep);
    for (const cid of [...ui.chips.map((c) => c.id), "board"]) {
      mk(cid, `c-chip ${chipCls(cid)}`, true, () => {
        if (ui.chipsOn.has(cid)) ui.chipsOn.delete(cid); else ui.chipsOn.add(cid);
        rebuildEvents();
        return ui.chipsOn.has(cid);
      }, cid === "board" ? "board-level events (nets, parts, links)" : `events of chip ${cid}`);
    }
  }
  renderKindChips();
}

function renderKindChips() {
  if (!ui.kindBtn) return;
  let hiddenN = 0;
  for (const k of KINDS) {
    const n = ui.kindCount[k] || 0;
    const b = ui.kindBtn[k];
    const el = b.querySelector(".n");
    el.textContent = n ? (n >= 1e4 ? `${Math.round(n / 1e3)}k` : String(n)) : "";
    const hide = !n && !ui.kindsAll;
    b.hidden = hide;
    if (hide) hiddenN++;
  }
  const more = ui.kindMore;
  more.hidden = !hiddenN && !ui.kindsAll;
  more.firstChild.textContent = ui.kindsAll ? "−" : `+${hiddenN}`;
}

const evVisible = (kind, chip) => ui.kindsOn.has(kind) && ui.chipsOn.has(chip);

function ringAdd(ev) {
  const r = ui.ring[ev.kind] || (ui.ring[ev.kind] = []);
  r.push(ev);
  if (r.length > KIND_RING * 1.25) r.splice(0, r.length - KIND_RING);
}

// a kind was switched on: fetch its kept events from the server (the ring may be short), then rebuild
async function refetchKind(k) {
  rebuildEvents();
  try {
    const since = Math.max(0, ui.lastEventSeq - 20000);
    const r = await api(`/api/events?since=${since}&kinds=${encodeURIComponent(k)}&limit=0`);
    const evs = (r.events || []).filter((e) => e.seq <= ui.lastEventSeq).slice(-KIND_RING);
    const have = new Set((ui.ring[k] || []).map((e) => e.seq));
    const merged = [...evs.filter((e) => !have.has(e.seq)), ...(ui.ring[k] || [])].sort((a, b) => a.seq - b.seq);
    ui.ring[k] = merged.slice(-KIND_RING);
    if (ui.kindsOn.has(k)) rebuildEvents();
  } catch { /* keep the ring */ }
}

function rebuildEvents() {
  const box = $("events");
  box.innerHTML = "";
  ui.lastEvRow = null;
  ui.evDropped = 0;
  const evs = [];
  for (const k of KINDS) if (ui.kindsOn.has(k)) for (const ev of ui.ring[k] || []) if (ui.chipsOn.has(evChip(ev))) evs.push(ev);
  evs.sort((a, b) => (a.seq || 0) - (b.seq || 0));
  appendRows(evs);
  ui.evFollow.end();
}

function refilterEvents() {
  const m = $("evFilter")._m;
  const src = m ? m.src : "";
  for (const row of $("events").children) {
    row.hidden = !!m && !m.test(row._txt);
    if (!row.hidden && row._hm !== src) renderEvRow(row, m);      // hidden rows are re-marked when shown
  }
  renderEvCount();
  ui.evFollow.after(0);
}

function renderEvCount() {
  const box = $("events");
  let shown = 0;
  for (const row of box.children) if (!row.hidden) shown++;
  const total = box.childElementCount;
  $("evCount").textContent = (shown === total ? `${total}` : `${shown}/${total}`) + (ui.evDropped ? ` +${ui.evDropped}` : "");
  $("evCount").title = `rows in view (max ${MAX_EVENT_ROWS}, repeats folded into ×N)${ui.evDropped ? `; +${ui.evDropped} older rows dropped from the view` : ""}; the downloads contain every event the emulator keeps (20000)`;
  renderKindChips();
  // empty state: no rows at all, or every row hidden by the text filter / kinds
  let note = "";
  if (!shown) {
    const all = Object.values(ui.kindCount).reduce((a, b) => a + b, 0);
    if (total) note = `no events match the filter (${total} rows hidden)`;
    else if (all) {
      const off = KINDS.filter((k) => !ui.kindsOn.has(k) && ui.kindCount[k]).map((k) => `${k} ${ui.kindCount[k]}`);
      note = `no events of the shown kinds${off.length ? `; switched off: ${off.join(", ")}` : ""}`;
    } else note = "no events yet";
  }
  const el = $("evEmpty");
  el.hidden = !note;
  if (el.textContent !== note) el.textContent = note;
}

function eventRowsText() {
  return [...$("events").children].filter((r) => !r.hidden).map((r) => r._txt + (r._n > 1 ? ` ×${r._n}` : "")).join("\n") + "\n";
}

function eventsUrl(fmt) {
  const p = new URLSearchParams({ since: "0", format: fmt, limit: "0", kinds: [...ui.kindsOn].join(",") });
  if (ui.isBoard) p.set("chip", [...ui.chipsOn].join(","));
  const m = $("evFilter")._m;
  if (m && !m.bad) p.set("q", m.server);       // an invalid /regex/ filters nothing in the view either
  return `/api/events?${p}`;
}

// Event text: the server formats every event once (chipem/web/evfmt.py, field `desc`), so the rows here, the
// server-side filter (/api/events?q=) and the txt download all use the same text. Fallback for events without it.
const evText = (ev) => ev.desc ?? Object.entries(ev).filter(([k]) => !["kind", "t_ns", "seq", "chip"].includes(k))
  .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");

// (re)write one event row; matches of the text filter are highlighted (<mark>) cell by cell
function renderEvRow(row, m) {
  const [t, chip, kind, msg] = row._cells;
  const h = (x) => hl(x, m);
  row.innerHTML = `<span class="t">${h(t)}</span>` +
    (ui.isBoard ? `<span class="c ${chipCls(chip)}">${h(chip)}</span>` : "") +
    `<span class="kind k-${esc(kind)}">${h(kind)}</span><span class="msg">${h(msg)}${row._n > 1 ? `<span class="rep" title="${row._n} identical events in a row; time = the last">×${row._n}</span>` : ""}</span>`;
  row._hm = m ? m.src : "";
}

function addEvents(evs) {
  for (const ev of evs) { ui.kindCount[ev.kind] = (ui.kindCount[ev.kind] || 0) + 1; ringAdd(ev); }
  ui.board?.notePinEvents?.(evs);                 // pin card: level-change counts from the event log
  ui.logic?.onEvents?.(evs);
  appendRows(evs.filter((ev) => evVisible(ev.kind, evChip(ev))));
}

function appendRows(evs) {
  const box = $("events");
  // the row cap applies after folding repeats (a flood of one message must not push other rows out)
  if (evs.length > 20 * MAX_EVENT_ROWS) { ui.evDropped += evs.length - 20 * MAX_EVENT_ROWS; evs = evs.slice(-20 * MAX_EVENT_ROWS); }
  const m = $("evFilter")._m;
  const frag = document.createDocumentFragment();
  let added = 0;
  let last = ui.lastEvRow && ui.lastEvRow.isConnected ? ui.lastEvRow : null;
  for (const ev of evs) {
    const chip = evChip(ev);
    const t = fmtTime(ev.t_ns), msg = evText(ev);
    const key = `${ev.kind}\t${chip}\t${msg}`;
    if (last && last._key === key) {                  // repeat: fold into the previous row
      last._n++;
      last._cells[0] = t;
      last._txt = `${t}\t${ui.isBoard ? `${chip}\t` : ""}${ev.kind}\t${msg}`;
      if (!last.hidden) renderEvRow(last, m);
      continue;
    }
    const row = document.createElement("div");
    row.className = uartErr(ev) ? "ev ev-err" : "ev";
    row.dataset.kind = ev.kind;
    row.dataset.chip = chip;
    row._key = key;
    row._n = 1;
    row._txt = `${t}\t${ui.isBoard ? `${chip}\t` : ""}${ev.kind}\t${msg}`;     // = evfmt.event_line on the server
    row._cells = [t, chip, ev.kind, msg];
    row.hidden = !!m && !m.test(row._txt);
    if (!row.hidden) added++;
    renderEvRow(row, m);
    frag.appendChild(row);
    last = row;
  }
  ui.lastEvRow = last;
  box.appendChild(frag);
  let extra = box.childElementCount - MAX_EVENT_ROWS;
  if (extra > 0) ui.evDropped += extra;
  while (extra-- > 0) box.firstElementChild.remove();
  renderEvCount();
  ui.evFollow.after(added);
}

// ---------------------------------------------------------------- UART: transcript, link monitor, raw output
// line errors of one received character (chipem/periph/uartwire.py): framing / parity / noise flags, a break
const uartErr = (ev) => ev.kind === "uart" && (ev.dir === "rx" || ev.dir === "drop") && !!(ev.fe || ev.pe || ev.nf || ev.brk);
const ERR_NAMES = { fe: "framing", pe: "parity", nf: "noise", brk: "break" };

function showUartDrops() {
  const el = $("uartDrops");
  const d = ui.uartDrops.get(ui.uart), e = ui.uartErrs.get(ui.uart);
  el.hidden = !d && !e;
  const parts = [];
  if (d) parts.push(`RX dropped ${d.n} (${d.reason})`);
  if (e) parts.push(Object.entries(e).filter(([k]) => k !== "last").map(([k, n]) => `${n} ${ERR_NAMES[k]}`).join(", "));
  el.textContent = parts.join(" · ");
  el.title = "bytes the receiver lost (not listening, overrun) and received characters with line errors (framing / parity / " +
    "noise / break), as on hardware" + (e?.last ? `\nlast error: ${e.last}` : "") + "\nfilter the Events pane with /FE|PE|NF|BREAK/ to list them";
}

function uartEvent(ev, live) {
  const cid = evChip(ev);
  const u = qual(cid, ev.uart || ev.name || "UART");
  if (uartErr(ev)) {
    const e = ui.uartErrs.get(u) || {};
    for (const k of ["fe", "pe", "nf", "brk"]) if (ev[k]) e[k] = (e[k] || 0) + 1;
    e.last = `${fmtTime(ev.t_ns)} ${ev.desc || ""}`;
    ui.uartErrs.set(u, e);
    if (u === ui.uart) showUartDrops();
  }
  if (ev.byte == null) {
    // a break sent by this UART: mark it in the transcript (it is not a character)
    if (ev.dir === "tx" && ev.brk) {
      const open = ui.openLine.get(u);
      if (open) { ui.openLine.delete(u); open.done = true; }
      pushLine({ uart: u, chip: cid, text: `\u27e8BREAK ${ev.brk} bits\u27e9`, t: ev.t_ns, done: true, mark: true });
      ui.uartDirty = true;
    }
    return;
  }
  if (ev.dir === "drop") {
    const d = ui.uartDrops.get(u) || { n: 0, reason: "" };
    d.n += 1;
    d.reason = ev.reason || "?";
    ui.uartDrops.set(u, d);
    if (u === ui.uart) showUartDrops();
    return;
  }
  if (ev.dir === "tx") {
    const b = ev.byte;
    const cr = ui.afterCR.get(u);
    ui.afterCR.set(u, b === 13);
    let m = ui.linkMon.get(u);
    if (!m) { m = { bytes: [] }; ui.linkMon.set(u, m); }
    m.bytes.push(b);
    if (m.bytes.length > MON_BYTES) m.bytes.splice(0, m.bytes.length - MON_BYTES);
    if (live && ui.board) ui.board.pulse(cid, ev.uart || ev.name, b);
    ui.uartDirty = true;
    // LF, CR and CRLF each end a line; the LF of a CRLF only closes what the CR already closed (no empty line)
    if (b === 10 || b === 13) {
      const open = ui.openLine.get(u);
      if (open) { ui.openLine.delete(u); open.done = true; }
      else if (b === 13 || !cr) pushLine({ uart: u, chip: cid, text: "", t: ev.t_ns, done: true });   // empty line
      return;
    }
    let ln = ui.openLine.get(u);
    if (!ln) {
      ln = pushLine({ uart: u, chip: cid, text: "", t: ev.t_ns });
      ui.openLine.set(u, ln);
    }
    ln.text += b >= 32 && b < 127 ? String.fromCharCode(b) : `\\x${b.toString(16).padStart(2, "0")}`;
    ui.linesDirty.add(ln);
    if (ln.text.length >= MAX_LINE_CHARS) ui.openLine.delete(u);        // binary stream: bound each line
  }
}

function pushLine(ln) {
  ui.lines.push(ln);
  if (ui.lines.length > MAX_LINES) ui.lines.splice(0, ui.lines.length - MAX_LINES);
  ui.linesDirty.add(ln);
  return ln;
}

function linkPeers(u) {
  const out = new Set([u]);
  for (const ln of ui.info.links || []) {
    if (ln.from === u) out.add(ln.to);
    if (ln.to === u) out.add(ln.from);
  }
  return out;
}

function buildUartSelector() {
  const sel = $("uartSel");
  for (const c of ui.chips) {
    const ids = c.uart_ids || c.uarts || [];
    if (!ids.length) continue;
    const grp = ui.isBoard ? document.createElement("optgroup") : sel;
    if (ui.isBoard) { grp.label = `chip ${c.id} · ${c.name}`; sel.appendChild(grp); }
    for (const u of ids) grp.appendChild(new Option(u, u));
  }
  const firstLink = (ui.info.links || [])[0];
  ui.uart = firstLink ? firstLink.from : (sel.options[0] ? sel.options[0].value : null);
  if (ui.uart) sel.value = ui.uart;
  // one default: the transcript ("lines": both sides of a link, timestamps) when the session has UART links, else the
  // raw output; a choice made in the selector is remembered
  ui.uartMode = prefs.uartMode || ((ui.info.links || []).length ? "lines" : "raw");
  $("uartMode").value = ui.uartMode;
  ui.uartTs = !!prefs.uartTs;
  $("uartTs").checked = ui.uartTs;
  const modeUi = () => {
    $("uartAllWrap").hidden = ui.uartMode !== "lines";
    $("uartTsWrap").hidden = ui.uartMode !== "lines";
  };
  sel.addEventListener("change", () => {
    ui.uart = sel.value; ui.uartPicked = true; ui.uartDirty = true; ui.uartFull = true;
    showUartDrops(); refreshUart(true); schedule(); ui.uartFollow.end();
  });
  $("uartMode").addEventListener("change", () => {
    ui.uartMode = prefs.uartMode = $("uartMode").value;
    savePrefs();
    modeUi();
    ui.uartDirty = true; ui.uartFull = true;
    $("uartOut")._rawShown = null;
    if (ui.uartMode === "raw") refreshUart(true); else schedule();
    ui.uartFollow.end();
  });
  modeUi();
  $("uartAll").addEventListener("change", () => { ui.uartAll = $("uartAll").checked; ui.uartDirty = true; ui.uartFull = true; schedule(); });
  $("uartTs").addEventListener("change", () => {
    ui.uartTs = prefs.uartTs = $("uartTs").checked; savePrefs(); ui.uartDirty = true; ui.uartFull = true; schedule();
  });
}

const lineWho = (l) => (ui.isBoard || ui.uartAll || l.input ? (l.input ? `${l.uart} RX ← typed` : `${l.uart} TX`) : "");
function lineWanted(l, want, m) { return (!want || want.has(l.uart)) && (!m || m.test(l.text)); }

// transcript lines (lines view) after the peer/"all" and text filters
function transcriptLines() {
  const want = ui.uartAll ? null : linkPeers(ui.uart);
  const m = $("uartFilter")._m;
  return ui.lines.filter((l) => lineWanted(l, want, m)).map((l) => ({ l, who: lineWho(l) }));
}

function transcriptText() {
  return transcriptLines().map(({ l, who }) =>
    `${ui.uartTs ? `${fmtTs(l.t)}  ` : ""}${who ? `${who}: ` : ""}${l.text}`).join("\n") + "\n";
}

function lineHtml(l, m) {
  const who = lineWho(l);
  return `${ui.uartTs ? `<span class="ts">${esc(fmtTs(l.t))}</span>  ` : ""}` +
    `${who ? `<span class="who">${esc(who)}:</span> ` : ""}${l.mark ? `<span class="umark">${hl(l.text, m)}</span>` : hl(l.text, m)}`;
}

function lineEl(l, m) {
  const sp = document.createElement("span");
  sp.className = `ln ${chipCls(l.chip)}${l.input ? " in" : ""}`;
  sp.innerHTML = lineHtml(l, m);
  sp._l = l;
  l.el = sp;
  return sp;
}

// Lines view, append-only: new lines are appended and the open (growing) line is rewritten; a full rebuild only
// when the UART, the mode, the filter or a view option changed (ui.uartFull)
function renderTranscript() {
  if (ui.uartMode !== "lines") { ui.linesDirty.clear(); return; }
  const out = $("uartOut");
  const m = $("uartFilter")._m;
  const want = ui.uartAll ? null : linkPeers(ui.uart);
  let added = 0;
  if (ui.uartFull || out._mode !== "lines") {
    ui.uartFull = false;
    out._mode = "lines";
    for (const l of ui.lines) l.el = null;
    const lines = ui.lines.filter((l) => lineWanted(l, want, m)).slice(-2000);
    out.textContent = "";
    const frag = document.createDocumentFragment();
    for (const l of lines) { frag.appendChild(lineEl(l, m)); frag.appendChild(document.createTextNode("\n")); }
    out.appendChild(frag);
    added = lines.length;
    ui.linesDirty.clear();
    ui.uartFollow.end();
  } else if (ui.linesDirty.size) {
    const frag = document.createDocumentFragment();
    for (const l of ui.linesDirty) {
      const ok = lineWanted(l, want, m);
      if (l.el && l.el.isConnected) {
        if (ok) l.el.innerHTML = lineHtml(l, m);
        else { l.el.nextSibling?.remove(); l.el.remove(); l.el = null; }
      } else if (ok) {
        frag.appendChild(lineEl(l, m)); frag.appendChild(document.createTextNode("\n"));
        added++;
      }
    }
    ui.linesDirty.clear();
    out.appendChild(frag);
    let extra = out.childNodes.length - 4000;          // 2000 lines (span + newline each)
    while (extra-- > 0) out.firstChild.remove();
  }
  const any = ui.lines.some((l) => !want || want.has(l.uart));
  out.dataset.empty = m && any ? "no lines match the filter" : `no TX lines${ui.uartAll ? "" : ui.uart ? ` on ${[...linkPeers(ui.uart)].join(" / ")}` : ""} yet`;
  if (added) ui.uartFollow.after(added);
}

// raw view: the UART's output, fetched incrementally (/api/uart?since=)
function rawVisible(name) {
  const r = ui.raw[name];
  if (!r) return "";
  const skip = Math.max(0, (ui.uartSkip?.[name] || 0) - r.base);
  return r.text.slice(skip);
}

function renderRaw(appended) {
  if (ui.uartMode !== "raw") return;
  const out = $("uartOut");
  const m = $("uartFilter")._m;
  if (out._mode !== "raw") { out._mode = "raw"; out._rawShown = null; out._rawHtml = null; }
  out.dataset.empty = `no output on ${ui.uart || "—"} yet`;
  const text = rawVisible(ui.uart);
  if (m) {
    const lines = text.split("\n").filter((ln) => m.test(ln));
    const html = lines.slice(-5000).map((ln) => hl(ln, m)).join("\n");
    if (out._rawHtml !== html) {
      const added = Math.max(0, lines.length - (out._rawN || 0));
      out.innerHTML = html; out._rawHtml = html; out._rawN = lines.length; out._rawShown = null;
      ui.uartFollow.after(added);
    }
    return;
  }
  out._rawHtml = null;
  const show = text.length > RAW_SHOW ? text.slice(-RAW_SHOW) : text;
  if (appended && out._rawShown != null && out._rawShown + appended.length === show.length && out._rawShown > 0 &&
      out.textContent.length === out._rawShown) {
    out.append(appended);                               // cheap path: append the new bytes
  } else if (out.textContent !== show) {
    out.textContent = show;
  }
  out._rawShown = show.length;
  ui.uartFollow.after(appended ? (appended.match(/\n/g) || []).length || 1 : 0);
}

let uartBusy = false;
async function refreshUart(force) {
  if (ui.uartMode !== "raw") return;
  if (!ui.uart || (uartBusy && !force)) return;
  uartBusy = true;
  try {
    const name = ui.uart;
    let r0 = ui.raw[name];
    const since = force || !r0 ? 0 : r0.base + r0.text.length;
    const r = await api(`/api/uart?uart=${encodeURIComponent(name)}&since=${since}`);
    if (ui.uartMode !== "raw" || name !== ui.uart) return;
    ui.uartLen[name] = r.len;
    r0 = ui.raw[name];
    let appended = null;
    if (!r0 || r.since === 0 || r.since !== r0.base + r0.text.length) {
      ui.raw[name] = r0 = { text: r.text, base: r.since || 0 };
      $("uartOut")._rawShown = null;
    } else {
      r0.text += r.text;
      appended = r.text;
    }
    if (r0.text.length > RAW_KEEP) { const cut = r0.text.length - RAW_KEEP; r0.text = r0.text.slice(cut); r0.base += cut; }
    renderRaw(appended);
  } catch (e) { /* busy: next state frame retries */ }
  finally { uartBusy = false; }
}

function buildLinkMonitor() {
  const box = $("linkMon");
  const links = ui.info.links || [];
  $("linkMonWrap").hidden = !links.length;
  $("linkMonWrap").open = prefs.linkMon ?? false;
  $("linkMonCount").textContent = links.length ? `${links.length}` : "";
  box.innerHTML = "";
  ui.monRows = [];
  for (const ln of links) {
    const dirs = [[ln.from, ln.to, "a_to_b", "ab"]];
    if (ln.bidir !== false) dirs.push([ln.to, ln.from, "b_to_a", "ba"]);
    for (const [src, dst, stat, key] of dirs) {
      if (!ui.linkMon.has(src)) ui.linkMon.set(src, { bytes: [] });
      const row = document.createElement("div");
      row.className = "mon-row";
      row.title = ln.auto ? `automatic link from net ${ln.net}` : "explicit link";
      row.innerHTML = `<span class="mon-dir"><span class="${chipCls(src.split(".")[0])}">${esc(src)}</span> → <span class="${chipCls(dst.split(".")[0])}">${esc(dst)}</span>${ln.auto ? ' <span class="auto-tag">auto</span>' : ""}</span><code class="mon-bytes"></code><span class="mon-n"></span>`;
      box.appendChild(row);
      ui.monRows.push({ src, stat, key, desc: ln.describe, bytesEl: row.querySelector(".mon-bytes"), nEl: row.querySelector(".mon-n") });
    }
  }
}

function renderLinkMonitor() {
  if (!ui.monRows || !ui.monRows.length || !$("linkMonWrap").open) return;
  const links = (ui.state && ui.state.board && ui.state.board.links) || [];
  for (const m of ui.monRows) {
    const bytes = ui.linkMon.get(m.src)?.bytes || [];
    const html = bytes.map((b) => (b >= 32 && b < 127 ? esc(String.fromCharCode(b))
      : `<i>${b === 10 ? "↵" : b === 13 ? "⏎" : b.toString(16).padStart(2, "0")}</i>`)).join("");
    if (m.bytesEl._html !== html) { m.bytesEl.innerHTML = html; m.bytesEl._html = html; }
    const s = links.find((x) => x.link === m.desc);
    const n = s ? s[m.stat] : null;
    const rate = fmtRate(ui.board.linkRate ? ui.board.linkRate(m.desc, m.key) : 0);
    const t = n != null ? `${n} B${rate ? ` · ${rate}` : ""}` : "";
    if (m.nEl.textContent !== t) m.nEl.textContent = t;
  }
}

// ---------------------------------------------------------------- status pane: peripherals, buses, host serial, real time
const STAT_KEYS = {          // fields shown first (bold), per status type; the rest follow as key=value
  can: ["state", "tec", "rec", "bitrate", "tx", "rx", "lost", "bus"],
  eth: ["link", "phy", "tx", "rx", "filtered", "crc", "missed", "lost", "segment"],
  usb: ["host_state", "bus_state", "address", "device", "strings", "transactions", "naks", "stalls", "timeouts", "bytes_in", "bytes_out", "cdc_rx"],
  sd: ["state", "card", "kind", "capacity", "rca", "bus_width", "high_speed", "written_blocks"],
};
function fmtCap(n) { return n >= 2 ** 30 ? `${+(n / 2 ** 30).toFixed(2)} GiB` : n >= 2 ** 20 ? `${+(n / 2 ** 20).toFixed(1)} MiB` : `${n} B`; }
function statVal(k, v) {
  if (v == null || v === "") return "—";
  if (k === "capacity" && typeof v === "number") return fmtCap(v);
  if (k === "bitrate" && typeof v === "number") return v ? `${+(v / 1e3).toFixed(1)} kbit/s` : "no bit clock";
  if (/_ns$/.test(k) && typeof v === "number") return fmtTime(v);
  if (/_hz$/.test(k) && typeof v === "number") return fmtFreq(v);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "object") return Object.entries(v).filter(([, x]) => x != null && x !== "").map(([a, b]) => `${a}: ${b}`).join(", ") || "—";
  return String(v);
}
function statClass(k, v) {
  const s = String(v ?? "");
  if (/^(busoff|bus-off|error|failed|down|detached|fault)/i.test(s) || (k === "error" && v)) return "bad";
  if (/^(passive|warn|suspended)/i.test(s)) return "warn";
  if (/^(active|up|configured|connected|ok|tran|ready)/i.test(s) && ["state", "link", "host_state", "bus_state"].includes(k)) return "ok";
  return "";
}

// rows of the status pane: [{key, head, cls, fields: [[k, v]]}]
function statusRows(st) {
  const rows = [];
  for (const p of ui.periph || []) {
    const keys = STAT_KEYS[p.type] || [];
    const fields = [];
    for (const k of keys) if (k in p) fields.push([k, p[k]]);
    for (const [k, v] of Object.entries(p)) if (!keys.includes(k) && !["chip", "block", "type"].includes(k)) fields.push([k, v]);
    rows.push({ key: `p:${p.chip}.${p.block}`, head: `${ui.isBoard ? `${p.chip}.` : ""}${p.block}`, chip: p.chip, type: p.type, fields });
  }
  const h = st.host;
  if (h) {
    const rt = h.realtime || {};
    if (rt.enabled) {
      rows.push({ key: "rt", head: "real time", type: "rt", fields: [["speed", `×${rt.speed}`], ["lag", `${Math.round(rt.lag_ms || 0)} ms`],
        ["max lag", `${Math.round(rt.max_lag_ms || 0)} ms`], ...(rt.rebases ? [["rebases", rt.rebases]] : []), ...(rt.behind ? [["behind", "yes"]] : [])] });
    }
    for (const e of h.endpoints || []) {
      const modem = (o) => (o && typeof o === "object" ? Object.entries(o).filter(([, x]) => x).map(([k]) => k.toUpperCase()).join(" ") || "—" : o ?? null);
      const f = [["transport", e.transport], ["path", e.path], ["conn", e.connected ? "connected" : "not connected"], ["line", e.line],
        ["in", e.bytes_in ?? 0], ["out", e.bytes_out ?? 0]];
      if (e.queued != null) f.push(["queued", e.queued]);
      const lost = (e.dropped ?? 0) + (e.dropped_out ?? 0);
      if (lost) f.push(["lost", `${lost} (framing ${e.dropped ?? 0}, host not connected ${e.dropped_out ?? 0})`]);
      if (e.modem_in !== undefined) f.push(["modem in", modem(e.modem_in)]);
      if (e.modem_out !== undefined) f.push(["modem out", modem(e.modem_out)]);
      if (e.error) f.push(["error", e.error]);
      rows.push({ key: `h:${e.name}`, head: e.name, type: "host", fields: f });
    }
  }
  return rows;
}

function renderStatus(st) {
  const rows = statusRows(st);
  const rt = (st.host && st.host.realtime) || {};
  const lag = rt.enabled ? `real time ×${rt.speed}${rt.lag_ms > 1 ? ` · lag ${Math.round(rt.lag_ms)} ms` : ""}${rt.rebases ? ` · ${rt.rebases} rebase${rt.rebases === 1 ? "" : "s"}` : ""}` : "";
  if ($("hostRt").textContent !== lag) $("hostRt").textContent = lag;
  $("hostRt").classList.toggle("warn", !!(rt.enabled && (rt.behind || rt.rebases)));
  if (document.activeElement !== $("rtOn")) $("rtOn").checked = !!rt.enabled;
  if (rt.enabled && document.activeElement !== $("rtSpeed")) $("rtSpeed").value = rt.speed;
  const html = rows.map((r) => `<div class="stat-row" data-key="${esc(r.key)}"><span class="sh ${r.chip ? chipCls(r.chip) : ""}">${esc(r.head)} <span class="type-tag">${esc(r.type)}</span></span>` +
    `<span class="sv">${r.fields.map(([k, v]) => { const c = statClass(k, v); return `${esc(k)} <b${c ? ` class="${c}"` : ""}>${esc(statVal(k, v))}</b>`; }).join(" · ")}</span></div>`).join("")
    || `<div class="empty">no peripheral status yet (CAN, USB, Ethernet, SD card and host serial bridges appear here once active)</div>`;
  const box = $("statList");
  if (box._html !== html) {
    box.innerHTML = html; box._html = html;
    ui.statRows = rows;
    applyStatFilter();
    // the pane starts collapsed; it opens itself once there is something to show (unless the user chose)
    if (rows.length && prefs.collapsed.status === undefined && !ui._statOpened) { ui._statOpened = true; setCollapsed($("paneStatus"), false, false); }
  }
}

function applyStatFilter() {
  filterList($("statList"), ".stat-row", $("statFilter")._m, $("statCount"), (ui.statRows || []).length);
}

function statRowsCsv() {
  const vis = visibleNames($("statList"), ".stat-row", "key");
  return [["name", "type", "fields"], ...(ui.statRows || []).filter((r) => vis.has(r.key))
    .map((r) => [r.head, r.type, r.fields.map(([k, v]) => `${k}=${statVal(k, v)}`).join(" ")])];
}

// ---------------------------------------------------------------- emulation rate + performance (chipem/perf.py)
const fmtX = (x) => (x == null ? "—" : `×${x >= 100 ? x.toFixed(0) : x >= 0.01 ? x.toPrecision(2) : x.toExponential(1)}`);
const fmtNsShort = (ns) => (ns == null ? "" : ns >= 1e6 ? `${+(ns / 1e6).toPrecision(3)}ms` : ns >= 1e3 ? `${+(ns / 1e3).toPrecision(3)}us` : `${+ns.toPrecision(3)}ns`);

// toolbar speed readout: achieved (server meter over the last second of running) vs requested; warn when behind
function renderSpeed(st) {
  const sp = st.speed;
  const el = $("tSpeed");
  if (!sp || !st.running) {
    el.textContent = st.running ? updateSpeed(st) : (sp && sp.ratio != null ? fmtX(sp.ratio) : "—");
    el.classList.remove("behind");
    el.parentElement.title = "Speed: virtual time per wall-clock second while running";
    return;
  }
  const req = sp.requested;
  el.textContent = `${fmtX(sp.ratio)}${req != null ? `/${fmtX(req).slice(1)}` : ""}`;
  el.classList.toggle("behind", !!sp.behind);
  const chips = Object.entries(sp.chips || {}).map(([c, v]) => `${c} ${v.mips.toFixed(2)} MIPS${v.share != null && Object.keys(sp.chips).length > 1 ? ` (${Math.round(v.share * 100)} %)` : ""}`).join(", ");
  el.parentElement.title = `achieved ${fmtX(sp.ratio)} of real time, requested ${req == null ? "max (unpaced)" : fmtX(req)}` +
    (sp.behind ? " — the emulation cannot keep up with the requested rate" : "") + (chips ? `\n${chips}` : "") +
    (sp.bottleneck ? `\nbottleneck: chip ${sp.bottleneck}` : "");
}

function setIfIdle(id, v) {
  const e = $(id);
  if (document.activeElement === e) return;
  if (e.type === "checkbox") e.checked = !!v; else if (String(e.value) !== String(v)) e.value = v;
}

function renderPerf(st) {
  const sp = st.speed;
  if (!sp) return;
  const req = sp.requested;
  // toolbar rate control
  if (document.activeElement !== $("rateIn")) $("rateIn").value = req == null ? "max" : String(req);
  const opt = req == null ? "max" : String(req);
  setIfIdle("rateSel", [...$("rateSel").options].some((o) => o.value === opt) ? opt : "custom");
  // settings
  const p = sp.settings || {};
  setIfIdle("perfPreset", p.preset || "default");
  setIfIdle("perfSlice", p.max_slice ?? "");
  setIfIdle("perfQuantum", sp.quantum_ns != null ? fmtNsShort(sp.quantum_ns) : "");
  setIfIdle("perfAuto", sp.auto_quantum);
  setIfIdle("perfPoll", p.poll_skip_ns ? fmtNsShort(p.poll_skip_ns) : "0");
  setIfIdle("perfPin", p.pin_log || "auto");
  setIfIdle("perfDrop", (p.drop_kinds || []).join(","));
  setIfIdle("perfUi", p.ui_interval_s != null ? `${Math.round(p.ui_interval_s * 1000)}ms` : "");
  // summary line + per-chip table
  const sum = `${p.preset || ""} · ${fmtX(sp.ratio)}${req != null ? ` of ${fmtX(req)}` : ""}${sp.behind ? " · cannot keep up" : ""}` +
    (sp.quantum_ns != null ? ` · quantum ${fmtNsShort(sp.quantum_ns)}${sp.auto_quantum ? "+auto" : ""}` : "") +
    (sp.bottleneck ? ` · bottleneck ${sp.bottleneck}` : "");
  if ($("perfSum").textContent !== sum) $("perfSum").textContent = sum;
  $("perfSum").classList.toggle("warn", !!sp.behind);
  const rows = Object.entries(sp.chips || {});
  const multi = rows.length > 1;
  const html = rows.length ? `<tr><th>chip</th><th>MHz</th><th>MIPS</th><th>×real</th>${multi ? "<th>wall</th>" : ""}</tr>` +
    rows.map(([c, v]) => `<tr${c === sp.bottleneck ? ' class="bott" title="bottleneck: this chip takes the largest share of the wall time"' : ""}><td>${esc(c)}</td><td>${v.mhz}</td><td>${v.mips.toFixed(2)}</td><td>${fmtX(v.ratio)}</td>` +
      `${multi ? `<td>${v.share != null ? `${Math.round(v.share * 100)} %` : "—"}</td>` : ""}</tr>`).join("") : "";
  if ($("perfLive")._h !== html) { $("perfLive").innerHTML = html; $("perfLive")._h = html; }
}

async function setRate(v) {
  const t = String(v).trim().toLowerCase().replace(/^[x×]/, "");
  try {
    if (t === "max" || t === "inf" || t === "") await api("/api/realtime", { on: false });
    else {
      const x = parseFloat(t);
      if (!(x > 0) || !Number.isFinite(x)) throw new Error(`bad rate ${v} (a factor such as 0.1, 1, 10, or max)`);
      await api("/api/realtime", { on: true, speed: x });
    }
  } catch (e) { toastErr(`rate: ${e.message}`); }
}

async function setPerf(patch) {
  try {
    const r = await api("/api/perf", patch);
    if (r && r.speed && ui.state) renderPerf({ ...ui.state, speed: { ...(ui.state.speed || {}), ...r.speed } });   // now, not at the next frame
  } catch (e) { toastErr(`performance: ${e.message}`); }
}

function bindPerf() {
  $("rateSel").addEventListener("change", () => { const v = $("rateSel").value; if (v !== "custom") setRate(v); });
  $("rateIn").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); setRate($("rateIn").value); $("rateIn").blur(); } });
  $("rateIn").addEventListener("change", () => setRate($("rateIn").value));
  $("perfPreset").addEventListener("change", () => { if ($("perfPreset").value !== "custom") setPerf({ preset: $("perfPreset").value }); });
  const onEnter = (id, fn) => {
    $(id).addEventListener("change", () => fn($(id).value.trim()));
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $(id).blur(); } });
  };
  onEnter("perfSlice", (v) => setPerf({ max_slice: parseInt(v, 10) }));
  onEnter("perfQuantum", (v) => setPerf({ quantum: v === "" ? null : (/^[\d.]+$/.test(v) ? `${v}us` : v) }));
  onEnter("perfPoll", (v) => setPerf({ poll_skip: v === "" || v === "0" ? 0 : (/^[\d.]+$/.test(v) ? `${v}us` : v) }));
  onEnter("perfDrop", (v) => setPerf({ drop_kinds: v }));
  onEnter("perfUi", (v) => setPerf({ ui_interval: /^[\d.]+$/.test(v) ? `${v}ms` : v }));
  $("perfAuto").addEventListener("change", () => setPerf({ auto_quantum: $("perfAuto").checked }));
  $("perfPin").addEventListener("change", () => setPerf({ pin_log: $("perfPin").value }));
}

async function setRealtime() {
  const sp = parseFloat($("rtSpeed").value);
  try {
    await api("/api/realtime", { on: $("rtOn").checked, speed: Number.isFinite(sp) && sp > 0 ? sp : undefined });
  } catch (e) { toastErr(`real time: ${e.message}`); }
}

// ---------------------------------------------------------------- discovery (pin report)
// pins tray tabs: "pins" | "disc" | "logic"
function showTab(tab) {
  if (tab === true) tab = "disc"; else if (tab === false) tab = "pins";
  ui.tab = tab;
  ui.discOn = tab === "disc";
  for (const [id, t, panel] of [["tabPins", "pins", "pinWrap"], ["tabDisc", "disc", "discPane"], ["tabLogic", "logic", "logicPane"],
                                 ["tabDebug", "debug", "debugPane"], ["tabRegs", "regs", "regsPane"]]) {
    const on = t === tab;
    $(id).classList.toggle("on", on);
    $(id).setAttribute("aria-selected", String(on));
    $(id).tabIndex = on ? 0 : -1;
    $(panel).hidden = !on;
  }
  $("pinTools").hidden = tab !== "pins";
  $("discTools").hidden = tab !== "disc";
  // the stimulus field and the pin filter / copy / csv / vcd act on pins: not shown over the debugger / registers
  const pinsy = tab === "pins" || tab === "disc" || tab === "logic";
  $("stimForm").hidden = !pinsy;
  document.querySelector("#panePins > .pane-head > .pane-tools").hidden = !pinsy;
  if (tab === "disc") loadDiscovery();
  if (tab === "logic") ui.logic?.resize?.();
  ui.debug?.show?.(tab === "debug");                 // debug.js / regview.js (initDebug)
  ui.regview?.show?.(tab === "regs");
  prefs.tab = tab; savePrefs();
}

async function loadDiscovery() {
  if (ui.discBusy) { ui.discAgain = true; return; }
  ui.discBusy = true;
  try {
    const cid = ui.sel;
    const r = await api(`/api/pinreport?static=${$("discStatic").checked ? 1 : 0}` +
                        (ui.isBoard ? `&chip=${encodeURIComponent(cid)}` : ""));
    if (cid === ui.sel) renderDiscovery(r);
  } catch (e) {
    $("discTable").tBodies[0].innerHTML = `<tr><td colspan="11" class="na">pin report failed: ${esc(e.message)}</td></tr>`;
    toastErr(`pin report: ${e.message}`);
  } finally {
    ui.discBusy = false;
    if (ui.discAgain) { ui.discAgain = false; loadDiscovery(); }
  }
}

function renderDiscovery(r) {
  const tb = $("discTable").tBodies[0];
  const fmtN = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : String(n));
  tb.innerHTML = (r.pins || []).map((p) => {
    const hist = (p.history || []).map((h) => `${fmtTime(h.t)}  ${h.by}  ${h.what}`).join("\n");
    const notes = (p.notes || []).join("; ");
    return `<tr title="${esc(hist)}">
      <td class="num">${p.num}</td><td>${esc(p.pin)}</td><td>${esc(p.role)}</td>
      <td class="mono">${esc(p.cfg_by)}</td><td class="mono">${p.cfg_t != null ? esc(fmtTime(p.cfg_t)) : "—"}</td>
      <td class="num">${fmtN(p.reads)}</td><td class="num">${fmtN(p.writes)}</td>
      <td class="num">${fmtN(p.changes)}</td><td class="num">${fmtN(p.irqs)}</td>
      <td>${esc(p.static)}</td><td class="notes${/FLOATING/.test(notes) ? " warn" : ""}">${esc(notes)}</td></tr>`;
  }).join("");
  if (!(r.pins || []).length) tb.innerHTML = `<tr><td colspan="11" class="na">no pins in the report (run the firmware, then refresh)</td></tr>`;
  filterDiscovery();
  const text = r.text || "";
  const k = text.indexOf("Discovery summary");
  $("discText").textContent = (ui.isBoard ? `chip ${r.chip || ui.sel}\n` : "") + (k >= 0 ? text.slice(k) : text);
}

// ---------------------------------------------------------------- board wiring: wire mode, nets, links
function setWireMode(on) {
  ui.wireMode = !!on;
  ui.pendingPin = null;
  ui.board.setWireMode(ui.wireMode);
  $("btnWire").classList.toggle("on", ui.wireMode);
  $("btnWire").setAttribute("aria-pressed", String(ui.wireMode));
  document.body.classList.toggle("wiring", ui.wireMode);
  if (ui.wireMode) toast("wire mode: click a pin, then another pin (any chip) to connect them · Esc: cancel");
}

async function wirePick(chip, pin) {
  const p = ui.pendingPin;
  if (!p) {
    ui.pendingPin = { cid: chip.id, pin };
    ui.board.setPending(chip.id, pin.num);
    return;
  }
  ui.pendingPin = null;
  ui.board.setPending(null);
  if (p.part) {                                                        // part port, then a pin
    const r = await act("/api/part/port", { id: p.part, port: p.port, to: `${chip.id}.${pin.short}` });
    if (r) { toast(`${p.part}.${p.port} → net ${r.net}`); await reloadWiring(r.wiring); if (r.net) selectNet(r.net); }
    return;
  }
  if (p.cid === chip.id && p.pin.num === pin.num) return;              // same pin again: cancel
  const pins = [`${p.cid}.${p.pin.short}`, `${chip.id}.${pin.short}`];
  const r = await act("/api/net", { pins });
  if (r) {
    toast(`net ${r.net.name}: ${r.net.pins.join(" — ")}`);
    await reloadWiring(r.wiring);
    selectNet(r.net.name);
  }
}

// ---------------------------------------------------------------- parts on the canvas
const partById = (id) => (ui.info.parts || []).find((p) => p.id === id);

async function setPart(id, values) {
  const r = await act("/api/part/set", { id, values });
  if (r) {
    ui.partState[id] = { ...(ui.partState[id] || {}), ...(r.state || {}) };
    ui.partsDirty = true;
    schedule();
  }
  return r;
}

// state.board.parts: [{id, state, ...}] (Session.state) or {id: state}
function partStateMap(parts) {
  if (!Array.isArray(parts)) return parts;
  const m = {};
  for (const p of parts) m[p.id] = p.state || {};
  return m;
}

function partEvent(ev) {
  if (!ev.part) return;
  const st = ui.partState[ev.part] || (ui.partState[ev.part] = {});
  for (const [k, v] of Object.entries(ev)) {
    if (!["kind", "t_ns", "seq", "chip", "part", "port", "msg", "action"].includes(k)) st[k] = v;
  }
  ui.partsDirty = true;
}

// a new net name for a port that is not connected yet
function freshNetName(part, port) {
  const names = new Set((ui.info.nets || []).map((n) => n.name));
  for (const p of ui.info.parts || []) for (const n of Object.values(p.ports || {})) if (n) names.add(n);
  let base = `${part}_${port}`.toUpperCase(), n = base, i = 2;
  while (names.has(n)) n = `${base}_${i++}`;
  return n;
}

// click on a part port: dialog pick, wire mode (port ↔ pin / port ↔ port), else select its net
async function clickPort(part, port) {
  const net = (part.ports || {})[port] || null;
  if (ui.pickTarget) {
    let name = net;
    if (!name) {                                   // give the port a net so the new part can join it
      name = freshNetName(part.id, port);
      const r = await act("/api/part/port", { id: part.id, port, to: name });
      if (!r) return;
      name = r.net || name;
      reloadWiring(r.wiring);
    }
    pickDone(name);
    return;
  }
  if (!ui.wireMode) { if (net) selectNet(ui.selNet === net ? null : net); return; }
  const p = ui.pendingPin;
  if (!p) {
    ui.pendingPin = { part: part.id, port };
    ui.board.setPendingPort(part.id, port);
    return;
  }
  ui.pendingPin = null;
  ui.board.setPending(null);
  let r = null;
  if (!p.part) {                                   // a pin, then this port
    r = await act("/api/part/port", { id: part.id, port, to: `${p.cid}.${p.pin.short}` });
  } else {
    if (p.part === part.id && p.port === port) return;
    const other = partById(p.part);
    const onet = other && (other.ports || {})[p.port];
    if (net) r = await act("/api/part/port", { id: p.part, port: p.port, to: net });
    else if (onet) r = await act("/api/part/port", { id: part.id, port, to: onet });
    else {
      const name = freshNetName(p.part, p.port);
      r = await act("/api/part/port", { id: p.part, port: p.port, to: name });
      if (r) r = await act("/api/part/port", { id: part.id, port, to: r.net || name });
    }
  }
  if (r) { toast(`net ${r.net}`); await reloadWiring(r.wiring); if (r.net) selectNet(r.net); }
}

function netLevel(name) {
  const n = ui.state && ui.state.board ? (ui.state.board.nets || []).find((x) => x.name === name) : null;
  return n ? n.level || "X" : null;
}

function netMembers(name) {
  const n = (ui.info.nets || []).find((x) => x.name === name);
  const out = n ? [...(n.pins || [])] : [];
  for (const p of ui.info.parts || []) {
    for (const [port, nn] of Object.entries(p.ports || {})) if (nn === name) out.push(`${p.id}.${port}`);
  }
  return out;
}

// part / port tooltip (#tooltip); pins use the board's own pin card
function hidePartTip() { $("tooltip").hidden = true; }

function placeTip(tip, e) {
  tip.hidden = false;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}

function showPortTip(part, port, e) {
  const net = (part.ports || {})[port] || null;
  const dir = (part.dirs || {})[port] || "";
  const lv = net ? netLevel(net) || "X" : null;
  const self = `${part.id}.${port}`;
  const others = net ? netMembers(net).filter((m) => m !== self) : [];
  const tip = $("tooltip");
  tip.innerHTML = `<h3>${esc(self)} ${lv ? `<span class="lv lv-${lv}">${lv}</span>` : ""}</h3>
    <div class="sub">${esc(part.type)} ${esc(dir)} port</div>
    <dl><dt>net</dt><dd>${net ? esc(net) : "not connected"}</dd>
    ${others.length ? `<dt>with</dt><dd>${esc(others.join(", "))}</dd>` : ""}</dl>
    <div class="tip-hint">${ui.pickTarget ? "click: use this port's net" : ui.wireMode
      ? (ui.pendingPin ? "click: connect" : "click: start a wire here") : net ? "click: select the net" : "W, then click: wire it"}</div>`;
  placeTip(tip, e);
}

// one part value as text: duty (a fraction in the part API) in %, frequencies, times
function partVal(k, v) {
  if (v === null || v === undefined) return "—";
  if (k === "duty" && typeof v === "number") return fmtDuty(v, true);
  if (/(^|_)hz$/.test(k) && typeof v === "number") return fmtFreq(v);
  if (/_ns$/.test(k) && typeof v === "number") return fmtTime(v);
  return typeof v === "string" ? v : JSON.stringify(v);
}

function showPartTip(part, e) {
  const st = ui.partState[part.id] || part.state || {};
  const tip = $("tooltip");
  const params = Object.entries(part.params || {}).map(([k, v]) => `${k}=${partVal(k, v)}`).join(" ");
  const ports = Object.entries(part.ports || {}).map(([p, n]) => `<dt>${esc(p)}</dt><dd>${n ? esc(n) : "—"}${n ? ` <span class="lv lv-${netLevel(n) || "X"}">${netLevel(n) || "X"}</span>` : ""}</dd>`).join("");
  const sts = Object.entries(st).map(([k, v]) => `${k}=${partVal(k, v)}`).join(" ");
  const hint = { switch: "click: toggle", button: "hold: pressed" }[part.type] || "click: show in the parts list";
  tip.innerHTML = `<h3>${esc(part.id)} <span class="sub">${esc(part.type)}</span></h3>
    ${params ? `<div class="sub">${esc(params)}</div>` : ""}<dl>${ports}</dl>
    <div class="alts"><span>state</span><span>${esc(sts || "—")}</span></div>
    <div class="tip-hint">${hint}</div>`;
  placeTip(tip, e);
}

// ---------------------------------------------------------------- parts panel
function wiringSig(info) {
  return JSON.stringify([info.nets && info.nets.map((n) => [n.name, n.pins, n.pull]),
    info.links && info.links.map((l) => [l.from, l.to, l.bidir, l.auto]),
    (info.buses || []).map((b) => [b.type, b.name, b.nodes]),
    (info.parts || []).map((p) => [p.id, p.type, p.params, p.ports])]);
}

const SEG = (cur, opts, key) => `<span class="seg">${opts.map((o) =>
  `<button class="seg-b${cur === o ? " on" : ""}" data-set='${esc(JSON.stringify({ [key]: o }))}'>${o}</button>`).join("")}</span>`;

const fmtSteps = (steps) => (steps || []).map((s) => (Array.isArray(s) ? `${s[0]}:${s[1]}` : String(s))).join(" ");
function parseSteps(text) {
  const out = [];
  for (const tok of String(text).split(/[\s,;]+/).filter(Boolean)) {
    const m = /^(.+?)[:=]([HLXhlx01])$/.exec(tok);
    if (!m) throw new Error(`pattern step ${JSON.stringify(tok)}: use time:level, e.g. 0:L 1ms:H 3ms:L`);
    const lv = m[2].toUpperCase();
    const t = /^[0-9.]+$/.test(m[1]) ? Number(m[1]) : m[1];
    out.push([t, lv === "1" ? "H" : lv === "0" ? "L" : lv]);
  }
  return out;
}

function partControls(p) {
  const st = ui.partState[p.id] || p.state || {};
  const pa = p.params || {};
  switch (p.type) {
    case "switch": return SEG(st.level ?? pa.level, ["L", "H", "X", "Z"], "level");
    case "button": return `<button class="btn small part-press" title="hold: pressed">press</button>`;
    case "const": return SEG(st.level ?? pa.level, ["L", "H", "X", "Z"], "level");
    case "probe": return `<button class="btn small ghost" data-set='{"clear":true}'>clear</button>`;
    case "counter": return `<button class="btn small ghost" data-set='{"count":0}'>clear</button>`;
    case "pulse": return `<label class="pp">period <input type="text" data-param="period" value="${esc(pa.period ?? "")}" size="6" spellcheck="false"></label>` +
      `<label class="pp" title="duty cycle in % high">duty % <input type="text" data-param="duty" value="${esc(+((pa.duty ?? 0.5) * 100).toFixed(3))}" size="3" spellcheck="false"></label>`;
    case "pattern": return `<input type="text" class="pat" data-param="steps" value="${esc(fmtSteps(pa.steps))}" spellcheck="false" title="time:level steps, e.g. 0:L 1ms:H 3ms:L (Enter applies)">` +
      `<label class="toggle"><input type="checkbox" data-param="repeat" ${pa.repeat ? "checked" : ""}> repeat</label>`;
    case "script": return scriptControls(p, st);
    default: return "";
  }
}

// script parts (chipem/parts/script.py): PARAMS as fields, @action methods as buttons, reload / edit the source
function scriptControls(p, st) {
  const sc = p.script || {};
  const vals = st.params || {};
  const ctl = (sc.controls || []).map((c) => {
    const v = vals[c.name] ?? c.default;
    const tip = [c.doc, c.unit && `unit ${c.unit}`, c.min != null && `min ${c.min}`, c.max != null && `max ${c.max}`]
      .filter(Boolean).join(" · ") || `${c.name} (Enter applies)`;
    let inp;
    if (typeof c.default === "boolean") inp = `<input type="checkbox" data-param="${esc(c.name)}" ${v ? "checked" : ""}>`;
    else if (Array.isArray(c.choices)) inp = `<select data-param="${esc(c.name)}">${c.choices.map((o) => `<option${o === v ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    else inp = `<input type="text" data-param="${esc(c.name)}" value="${esc(v ?? "")}" spellcheck="false">`;
    return `<label class="pp" title="${esc(tip)}">${esc(c.name)} ${inp}${c.unit ? `<span class="unit">${esc(c.unit)}</span>` : ""}</label>`;
  }).join("");
  const acts = (sc.actions || []).map((a) => `<button class="btn small ghost" data-set='${esc(JSON.stringify({ action: a }))}' title="run the script's @action ${esc(a)}">${esc(a)}</button>`).join("");
  return ctl + acts +
    `<button class="btn small ghost" data-script="reload" title="re-read ${esc(sc.file || "the inline code")}: state restarts with on_reset; ports cannot change without reloading the board">reload</button>` +
    `<button class="btn small ghost" data-script="edit" title="edit the script source">edit</button>`;
}

async function scriptReload(id) {
  const r = await act("/api/part/reload", { id });
  if (!r) return;
  scriptUpdated(id, r);
  toast(`${id}: script reloaded`);
}

function scriptUpdated(id, r) {
  const p = partById(id);
  if (p && r.script) p.script = r.script;
  if (r.state) ui.partState[id] = r.state;
  renderPartsPanel();
  ui.partsDirty = true;
  schedule();
}

let scriptEdit = null;                      // part id being edited
async function openScriptEditor(id) {
  let src;
  try { src = await api(`/api/part/script?id=${encodeURIComponent(id)}`); } catch (e) { toastErr(`script: ${e.message}`); return; }
  scriptEdit = id;
  $("sdTitle").textContent = `Script ${id}`;
  $("sdFile").textContent = src.file || "inline code (saved in the board)";
  $("sdFile").title = src.file || "";
  $("sdCode").value = src.code || "";
  $("sdErr").textContent = "";
  $("scriptDlg").showModal();
  $("sdCode").focus();
}

async function saveScript() {
  if (!scriptEdit) return;
  $("sdErr").textContent = "";
  try {
    const r = await api("/api/part/script", { id: scriptEdit, code: $("sdCode").value });
    scriptUpdated(scriptEdit, r);
    toast(`${scriptEdit}: saved and reloaded`);
  } catch (e) {
    $("sdErr").textContent = e.message;
    const m = /line (\d+)/.exec(e.message);              // put the caret on the reported line
    if (m) {
      const ta = $("sdCode"), lines = ta.value.split("\n"), n = Math.min(+m[1], lines.length) - 1;
      const at = lines.slice(0, n).reduce((a, l) => a + l.length + 1, 0);
      ta.focus(); ta.setSelectionRange(at, at + (lines[n] || "").length);
      ta.scrollTop = Math.max(0, n * (parseFloat(getComputedStyle(ta).lineHeight) || 16) - ta.clientHeight / 2);
    }
  }
}

function wireScriptEditor() {
  const dlg = $("scriptDlg");
  if (!dlg) return;
  const close = () => { scriptEdit = null; if (dlg.open) dlg.close(); };
  $("sdClose").addEventListener("click", close);
  $("sdCancel").addEventListener("click", close);
  $("sdSave").addEventListener("click", saveScript);
  dlg.addEventListener("close", () => { scriptEdit = null; });
  $("sdCode").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveScript(); return; }
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey) {     // indent / dedent with spaces
      e.preventDefault();
      const ta = e.target, a = ta.selectionStart, b = ta.selectionEnd, v = ta.value;
      if (!e.shiftKey && a === b) { ta.setRangeText("    ", a, b, "end"); return; }
      const ls = v.lastIndexOf("\n", a - 1) + 1;
      const block = v.slice(ls, b);
      const out = e.shiftKey ? block.replace(/^ {1,4}/gm, "") : block.replace(/^/gm, "    ");
      ta.setRangeText(out, ls, b, "select");
    }
  });
}

// POST that may need the user's consent to run Python script parts (server answers 409 with the script list)
async function apiScripts(path, body) {
  try { return await api(path, body); } catch (e) {
    if (e.status !== 409 || !/script parts/.test(e.message)) throw e;
    const list = (e.scripts || []).map((x) => String(x).replace(/^.*\//, "")).join(", ");
    if (!await askConfirm("Run Python script parts?", `${e.message.replace(/ \(send allow_scripts: true\)$/, "")}${list && !e.message.includes(list) ? ` (${list})` : ""}.\n\n` +
      "A script part runs with your user's privileges and can do anything you can do. Only continue if you trust it.", "Run scripts")) return null;
    return api(path, { ...body, allow_scripts: true });
  }
}

// modal yes / no in the page's own style (a native confirm() cannot be themed or read by screen captures)
function askConfirm(title, text, okLabel = "OK") {
  const d = $("askDlg");
  $("askTitle").textContent = title;
  $("askText").textContent = text;
  $("askYes").textContent = okLabel;
  return new Promise((resolve) => {
    const done = (v) => { $("askYes").onclick = $("askNo").onclick = null; d.onclose = null; if (d.open) d.close(); document.body.classList.remove("modal-open"); resolve(v); };
    $("askYes").onclick = () => done(true);
    $("askNo").onclick = () => done(false);
    d.onclose = () => done(false);                       // Esc
    document.body.classList.add("modal-open");
    d.showModal();
    $("askNo").focus();
  });
}

function panelStateText(p, st) {
  if (p.type === "counter") return `${counterValue(st)}`;
  if (p.type === "probe") return `${st.level ?? "X"} · ${stateText(p, st)}${st.freq_hz ? ` · ${fmtFreq(st.freq_hz)}` : ""}`;
  return stateText(p, st);
}

function partIcon(p, st) {
  if (p.type === "led") return `<i class="part-ico led-ico${st.on ? " on" : ""}" style="--led:${esc(ledColor(p.params && p.params.color))}"></i>`;
  const out = Object.entries(p.dirs || {}).find(([, d]) => d !== "in");
  const lv = out ? (p.ports[out[0]] ? netLevel(p.ports[out[0]]) : null) : (st.level || null);
  return `<i class="part-ico lv-${lv || "none"}"></i>`;
}

// datalist of the port fields: nets and every GPIO pin (qualified)
function fillNetPinList() {
  const parts = ui.info.parts || [];
  const dl = $("netPinList");
  const opts = [];
  for (const n of new Set([...(ui.info.nets || []).map((x) => x.name), ...parts.flatMap((p) => Object.values(p.ports || {})).filter(Boolean)])) {
    opts.push(`<option value="${esc(n)}">net</option>`);
  }
  for (const c of ui.chips) {
    for (const pin of c.pins) if (pin.kind === "gpio") opts.push(`<option value="${esc(c.id)}.${esc(pin.short)}">${esc(c.name)} pin ${pin.num}</option>`);
  }
  dl.innerHTML = opts.join("");
}

function renderPartsPanel() {
  if (!ui.isBoard) return;
  const parts = ui.info.parts || [];
  $("partCount").textContent = parts.length ? `${parts.length}` : "";
  fillNetPinList();
  const gi = (t) => { const i = GROUPS.indexOf(partGroup(t)); return i < 0 ? GROUPS.length : i; };
  const sorted = [...parts].sort((a, b) => gi(a.type) - gi(b.type) || a.id.localeCompare(b.id, undefined, { numeric: true }));
  $("partList").innerHTML = sorted.length ? sorted.map((p) => {
    const st = ui.partState[p.id] || p.state || {};
    const ports = portList(p).map((pp) => `<label class="pport" title="${esc(pp.dir)} port · net name or CHIP.PIN · empty: disconnect">` +
      `<span class="pn">${esc(pp.name)}</span><input type="text" list="netPinList" data-port="${esc(pp.name)}" value="${esc(p.ports[pp.name] || "")}" placeholder="—" spellcheck="false"></label>`).join("");
    return `<div class="part-row" data-part="${esc(p.id)}">
      <span data-ico>${partIcon(p, st)}</span>
      <span class="part-name">${esc(p.id)}</span><span class="part-type">${esc(p.type)}</span>
      <span class="part-st" data-st>${esc(panelStateText(p, st))}</span>
      <button class="btn small ghost part-del" title="remove part (undo in the toast)" aria-label="remove part ${esc(p.id)}">×</button>
      <div class="part-ctl">${partControls(p)}${previewControls(p)}</div>
      <div class="part-ports">${ports}</div>
    </div>`;
  }).join("") : `<div class="empty">no parts — add a switch, LED or gate and wire it to a net or pin</div>`;
  applyWiringFilters();
}

// previews announced by the part (views: frame / samples; parts.js FramePreview, WavePreviews), never by type
function partViews(p) { return ui.partViews[p.id] || p.views || null; }
function previewControls(p) {
  const v = partViews(p) || {};
  return (v.frame ? `<button class="btn small ghost" data-preview="frame" title="show the part's current frame (refreshed while open)">frame</button>` : "") +
    (v.samples ? `<canvas class="part-wave" aria-label="${esc(p.id)} recent samples"></canvas>` : "");
}

function renderPartStates() {
  if (!ui.isBoard) return;
  for (const row of $("partList").children) {
    const p = partById(row.dataset.part);
    if (!p) continue;
    const views = partViews(p);
    if (views) {
      ui.wavePrev?.update(p.id, views, row.querySelector(".part-wave"));
      ui.framePrev?.update(p.id, views);
    }
    const st = ui.partState[p.id] || p.state || {};
    const t = panelStateText(p, st);
    const se = row.querySelector("[data-st]");
    if (se.textContent !== t) se.textContent = t;
    const ico = partIcon(p, st);
    const ie = row.querySelector("[data-ico]");
    if (ie._h !== ico) { ie.innerHTML = ico; ie._h = ico; }
    const cur = st.level;
    for (const b of row.querySelectorAll(".seg-b")) {
      const v = JSON.parse(b.dataset.set);
      b.classList.toggle("on", v.level === cur);
    }
    const pr = row.querySelector(".part-press");
    if (pr) pr.classList.toggle("on", !!st.pressed);
    if (p.type === "script") {
      row.classList.toggle("faulted", !!st.fault);
      row.title = st.fault ? `faulted: ${st.fault} (reset or reload)` : "";
      for (const inp of row.querySelectorAll(".part-ctl [data-param]")) {      // values the script changed itself
        const v = (st.params || {})[inp.dataset.param];
        if (v === undefined || document.activeElement === inp) continue;
        if (inp.type === "checkbox") inp.checked = !!v;
        else if (inp.value !== String(v ?? "")) inp.value = String(v ?? "");
      }
    }
  }
}

function focusPartRow(id) {
  const row = [...$("partList").children].find((r) => r.dataset.part === id);
  if (!row) return;
  row.scrollIntoView({ block: "nearest" });
  row.classList.remove("flash");
  void row.offsetWidth;
  row.classList.add("flash");
}

async function partParamChanged(row, input) {
  const id = row.dataset.part;
  const key = input.dataset.param;
  let v = input.type === "checkbox" ? input.checked : input.value.trim();
  const isScript = (partById(id) || {}).type === "script";     // script PARAMS: sent as typed, the part converts
  try {
    if (isScript) { /* as is */ } else if (key === "steps") v = parseSteps(v);
    else if (key === "duty") {                       // entered in %, the part takes a fraction
      const d = parseFloat(String(v).replace("%", ""));
      if (!Number.isFinite(d) || d < 0 || d > 100) throw new Error("duty: a percentage 0 … 100");
      v = d / 100;
    }
  } catch (e) { toastErr(e.message); return; }
  const r = await setPart(id, { [key]: v });
  if (r) reloadWiring(r.wiring);
}

function wirePartDialog() {
  initBoardDialog({ api, toast, toastErr });
  wireScriptEditor();
  $("btnAddPart").addEventListener("click", openPartDialog);
  $("btnAddPart0").addEventListener("click", openPartDialog);
  $("pdClose").addEventListener("click", closePartDialog);
  $("pdCancel").addEventListener("click", closePartDialog);
  $("pdType").addEventListener("change", () => renderPartDialog());
  $("partForm").addEventListener("submit", (e) => { e.preventDefault(); submitPart(); });
  $("pdParams").addEventListener("change", (e) => {
    if (e.target.dataset.k === "inputs") renderPartPorts();
  });
  $("pdPorts").addEventListener("click", (e) => {
    const b = e.target.closest(".pick");
    if (!b) return;
    const inp = b.parentElement.querySelector("input");
    if (ui.pickTarget === inp) { pickDone(null); return; }
    ui.pickTarget = inp;
    for (const x of $("pdPorts").querySelectorAll(".pick")) x.classList.toggle("on", x === b);
    document.body.classList.add("picking");
    toast(`click a chip pin or a part port on the board for ${inp.dataset.port} · Esc: cancel`);
  });
}

function buildPartsPanel() {
  wirePartDialog();
  if (!ui.isBoard) return;
  const list = $("partList");
  list.addEventListener("click", async (e) => {
    const row = e.target.closest(".part-row");
    if (!row) return;
    const id = row.dataset.part;
    if (e.target.closest(".part-del")) { removePart(id); return; }
    const sc = e.target.closest("[data-script]");
    if (sc) { if (sc.dataset.script === "reload") scriptReload(id); else openScriptEditor(id); return; }
    const sb = e.target.closest("[data-set]");
    if (sb) { setPart(id, JSON.parse(sb.dataset.set)); return; }
    const pv = e.target.closest("[data-preview]");
    if (pv) {
      if (ui.framePrev.open && ui.framePrev.id === id) ui.framePrev.close();
      else { ui.framePrev.show(id, pv); pv.setAttribute("aria-expanded", "true"); }
    }
  });
  ui.framePrev = new partsMod.FramePreview(api, () => {
    for (const b of list.querySelectorAll("[data-preview]")) b.setAttribute("aria-expanded", "false");
  });
  ui.wavePrev = new partsMod.WavePreviews(api);
  list.addEventListener("pointerdown", (e) => {
    const b = e.target.closest(".part-press");
    if (!b || e.button !== 0) return;
    const id = b.closest(".part-row").dataset.part;
    setPart(id, { pressed: true });
    window.addEventListener("pointerup", () => setPart(id, { pressed: false }), { once: true });
  });
  list.addEventListener("change", async (e) => {
    const row = e.target.closest(".part-row");
    if (!row) return;
    const inp = e.target;
    if (inp.dataset.param) { partParamChanged(row, inp); return; }
    if (inp.dataset.port != null) {
      const to = inp.value.trim() || null;
      const r = await act("/api/part/port", { id: row.dataset.part, port: inp.dataset.port, to });
      if (r) reloadWiring(r.wiring);
    }
  });
  list.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches("input[type=text]")) e.target.blur();
  });
  list.addEventListener("mouseover", (e) => {
    const row = e.target.closest(".part-row");
    ui.board.highlightPart(row ? row.dataset.part : null);
  });
  list.addEventListener("mouseleave", () => ui.board.highlightPart(null));
  renderPartsPanel();
}

// remove a part; the toast offers undo (re-created with the same id, type, parameters and port nets)
async function removePart(id) {
  const p = partById(id);
  const def = p && { id: p.id, type: p.type, params: { ...(p.params || {}) },
    ports: Object.fromEntries(Object.entries(p.ports || {}).filter(([, n]) => n)) };
  if (def && p.type === "script") def.allow_scripts = true;     // it ran already: undo needs no new consent
  const r = await act("/api/part/remove", { id });
  if (!r) return;
  reloadWiring(r.wiring);
  toast(`part ${id} removed`, "info", def ? { label: "undo", fn: async () => {
    const q = await act("/api/part", def);
    if (q) { toast(`part ${id} restored`); await reloadWiring(q.wiring); focusPartRow(id); }
  } } : null);
}

function pickDone(value) {
  const inp = ui.pickTarget;
  ui.pickTarget = null;
  document.body.classList.remove("picking");
  for (const x of $("pdPorts").querySelectorAll(".pick")) x.classList.remove("on");
  if (inp && value != null) { inp.value = value; inp.focus(); }
}

// ---------------------------------------------------------------- add-part dialog
const LEVEL_PARAMS = new Set(["level", "active", "start", "released", "pressed_level", "released_level", "init", "rst_active"]);
const CHOICES = { edge: ["rise", "fall", "both"], color: LED_COLORS };

async function openPartDialog() {
  if (!ui.partTypes) {
    try { ui.partTypes = await api("/api/parts/types"); } catch (e) { toastErr(`part types: ${e.message}`); return; }
    const sel = $("pdType");
    for (const label of [...GROUPS, "other"]) {
      const g = document.createElement("optgroup");
      g.label = label;
      for (const t of ui.partTypes) {
        const grp = GROUPS.includes(partGroup(t.type)) ? partGroup(t.type) : "other";
        if (grp === label) g.appendChild(new Option(t.type, t.type));
      }
      if (g.children.length) sel.appendChild(g);
    }
  }
  $("partDlg").hidden = false;
  fillNetPinList();
  renderPartDialog();
  $("pdType").focus();
}

function closePartDialog() {
  pickDone(null);
  $("partDlg").hidden = true;
}

function paramField(k, v) {
  const id = `pd_${k}`;
  let inp;
  if (typeof v === "boolean") inp = `<input type="checkbox" id="${id}" data-k="${esc(k)}" data-t="bool" ${v ? "checked" : ""}>`;
  else if (k === "duty" && typeof v === "number") {             // shown and entered in %, sent as a fraction
    inp = `<input type="text" id="${id}" data-k="duty" data-t="pct" value="${+(v * 100).toFixed(3)}" size="6" title="duty cycle in % high">`;
    return `<label class="pd-row" for="${id}"><span>duty %</span>${inp}</label>`;
  } else if (typeof v === "number") inp = `<input type="text" id="${id}" data-k="${esc(k)}" data-t="num" value="${v}" size="6">`;
  else if (k === "steps") inp = `<input type="text" id="${id}" data-k="steps" data-t="steps" value="${esc(fmtSteps(v))}" placeholder="0:L 1ms:H 3ms:L">`;
  else if (Array.isArray(v) || (v && typeof v === "object")) inp = `<input type="text" id="${id}" data-k="${esc(k)}" data-t="json" value="${esc(JSON.stringify(v))}">`;
  else if (CHOICES[k] || LEVEL_PARAMS.has(k) || (k === "pressed" && typeof v === "string")) {
    const opts = CHOICES[k] || ["L", "H", "X"];
    if (v != null && !opts.includes(v)) opts.unshift(v);
    inp = `<select id="${id}" data-k="${esc(k)}" data-t="str">${opts.map((o) => `<option${o === v ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  } else inp = `<input type="text" id="${id}" data-k="${esc(k)}" data-t="${v === null ? "nullable" : "str"}" value="${esc(v ?? "")}" spellcheck="false">`;
  return `<label class="pd-row" for="${id}"><span>${esc(k)}</span>${inp}</label>`;
}

function renderPartDialog() {
  const t = (ui.partTypes || []).find((x) => x.type === $("pdType").value);
  if (!t) return;
  $("pdParams").innerHTML = Object.entries(t.params || {}).map(([k, v]) => paramField(k, v)).join("") ||
    `<div class="pd-none">no parameters</div>`;
  renderPartPorts();
}

function renderPartPorts() {
  const t = (ui.partTypes || []).find((x) => x.type === $("pdType").value);
  if (!t) return;
  let ports = Object.entries(t.ports || {});
  const ni = $("pdParams").querySelector('[data-k="inputs"]');
  if (ni && ports.some(([n]) => /^in\d+$/.test(n))) {
    const n = Math.max(1, Math.min(16, parseInt(ni.value, 10) || 2));
    ports = [...Array.from({ length: n }, (_, i) => [`in${i}`, "in"]), ...ports.filter(([nm]) => !/^in\d+$/.test(nm))];
  }
  const keep = new Map([...$("pdPorts").querySelectorAll("input")].map((i) => [i.dataset.port, i.value]));
  $("pdPorts").innerHTML = `<div class="pd-sub">ports → net name or CHIP.PIN (empty: leave unconnected)</div>` +
    ports.map(([n, d]) => `<label class="pd-row"><span>${esc(n)} <i>${esc(d)}</i></span>` +
      `<input type="text" list="netPinList" data-port="${esc(n)}" value="${esc(keep.get(n) || "")}" spellcheck="false" placeholder="e.g. BTN or A.PTC5">` +
      `<button type="button" class="btn small ghost pick" title="pick a pin or part port on the board">pick</button></label>`).join("");
}

async function submitPart() {
  const type = $("pdType").value;
  const params = {};
  try {
    for (const inp of $("pdParams").querySelectorAll("[data-k]")) {
      const k = inp.dataset.k, tt = inp.dataset.t;
      if (tt === "bool") params[k] = inp.checked;
      else if (tt === "num") params[k] = Number(inp.value);
      else if (tt === "pct") {
        const d = parseFloat(inp.value.replace("%", ""));
        if (!Number.isFinite(d) || d < 0 || d > 100) throw new Error(`${k}: a percentage 0 … 100`);
        params[k] = d / 100;
      }
      else if (tt === "steps") params[k] = parseSteps(inp.value);
      else if (tt === "json") params[k] = JSON.parse(inp.value || "null");
      else if (tt === "nullable") { if (inp.value.trim()) params[k] = inp.value.trim(); }
      else params[k] = inp.value;
    }
  } catch (e) { toastErr(e.message); return; }
  const ports = {};
  for (const inp of $("pdPorts").querySelectorAll("input[data-port]")) if (inp.value.trim()) ports[inp.dataset.port] = inp.value.trim();
  const body = { type, params, ports };
  const id = $("pdId").value.trim();
  if (id) body.id = id;
  let r = null;
  try { r = await apiScripts("/api/part", body); } catch (e) { toastErr(e.message); }
  if (r) {
    toast(`added ${r.part ? r.part.id : type}`);
    if (!ui.isBoard) { location.reload(); return; }          // the lone chip is now a one-chip board
    $("pdId").value = "";
    closePartDialog();
    await reloadWiring(r.wiring);
    if (r.part) focusPartRow(r.part.id);
  }
}

// refetch nets/links after a wiring change (ours, another client's, or an auto UART link) and redraw
async function reloadWiring(version) {
  if (ui.wiringBusy) { ui.wiringAgain = true; return; }
  if (version != null && version === ui.wiring) return;
  ui.wiringBusy = true;
  try {
    const info = await api("/api/chip");
    ui.info.nets = info.nets || [];
    ui.info.links = info.links || [];
    ui.info.buses = info.buses || [];
    ui.info.parts = info.parts || [];
    ui.info.board = info.board;
    ui.wiring = (info.board && info.board.wiring) || 0;
    for (const p of ui.info.parts) if (!ui.partState[p.id]) ui.partState[p.id] = p.state || {};
    const sig = wiringSig(ui.info);
    if (sig === ui.wiringSig) {                              // only part states changed (switch set, ...)
      ui.partsDirty = true;
      schedule();
      return;
    }
    ui.wiringSig = sig;
    ui.board.setInfo({ ...ui.info, chips: ui.chips });
    if (ui.state) renderReadouts(ui.state);                 // the rebuilt packages need their status texts
    if (ui.selNet && !ui.info.nets.some((n) => n.name === ui.selNet) && !(ui.info.parts || []).some((p) => Object.values(p.ports || {}).includes(ui.selNet))) ui.selNet = null;
    ui.board.selectNet(ui.selNet);
    buildLinkMonitor();
    renderWiringPanel();
    renderPartsPanel();
    ui.uartDirty = true; ui.uartFull = true;
    schedule();
  } catch (e) {
    toastErr(`wiring: ${e.message}`);
  } finally {
    ui.wiringBusy = false;
    if (ui.wiringAgain) { ui.wiringAgain = false; reloadWiring(); }
  }
}

function hoverNet(name, on) {
  ui.board.highlightNet(on ? name : null);
  for (const row of $("netList").children) row.classList.toggle("hl", on && row.dataset.net === name);
}

function selectNet(name) {
  ui.selNet = name;
  ui.board.selectNet(name);
  for (const row of $("netList").children) row.classList.toggle("sel", row.dataset.net === name);
}

// remove a net; the toast offers undo (re-created with its pins, name and pull; part ports re-attached)
async function removeNet(name) {
  const n = (ui.info.nets || []).find((x) => x.name === name);
  const ports = [];
  for (const p of ui.info.parts || []) for (const [port, nn] of Object.entries(p.ports || {})) if (nn === name) ports.push([p.id, port]);
  const r = await act("/api/net/remove", { name });
  if (!r) return;
  if (ui.selNet === name) selectNet(null);
  reloadWiring(r.wiring);
  const undo = n && ((n.pins || []).length || ports.length) ? async () => {
    let w = null;
    if ((n.pins || []).length) {
      const q = await act("/api/net", { pins: n.pins, name, ...(n.pull ? { pull: n.pull } : {}) });
      if (!q) return;
      w = q.wiring;
    }
    for (const [id, port] of ports) { const x = await act("/api/part/port", { id, port, to: name }); if (x) w = x.wiring; }
    toast(`net ${name} restored`);
    await reloadWiring(w);
    selectNet(name);
  } : null;
  toast(`net ${name} removed`, "info", undo ? { label: "undo", fn: undo } : null);
}

// inline rename: the name becomes an input (Enter applies, Esc cancels)
function renameNet(name) {
  const row = [...$("netList").children].find((r) => r.dataset.net === name);
  const cell = row && row.querySelector(".net-name");
  if (!cell || cell.querySelector("input")) return;
  cell.innerHTML = `<input type="text" value="${esc(name)}" spellcheck="false" aria-label="new name of net ${esc(name)}">`;
  const inp = cell.querySelector("input");
  inp.focus(); inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const nn = inp.value.trim();
    if (!save || !nn || nn === name) { cell.textContent = name; return; }
    const r = await act("/api/net/rename", { name, new: nn });
    if (r) { if (ui.selNet === name) ui.selNet = r.net.name; reloadWiring(r.wiring); }
    else cell.textContent = name;
  };
  inp.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  inp.addEventListener("blur", () => finish(true));
  inp.addEventListener("click", (e) => e.stopPropagation());
}

async function removeLink(from, to) {
  const ln = (ui.info.links || []).find((l) => l.from === from && l.to === to);
  const r = await act("/api/link/remove", { from, to });
  if (!r) return;
  reloadWiring(r.wiring);
  toast(`link ${r.removed} removed`, "info", ln ? { label: "undo", fn: async () => {
    const q = await act("/api/link", { from, to, bidir: ln.bidir !== false });
    if (q) { toast(`link ${q.link.describe} restored`); reloadWiring(q.wiring); }
  } } : null);
}

function buildWiringPanel() {
  if (!ui.isBoard) return;
  const fill = (sel) => {
    sel.innerHTML = "";
    for (const c of ui.chips) {
      const ids = c.uart_ids || [];
      if (!ids.length) continue;
      const grp = document.createElement("optgroup");
      grp.label = `${c.id} · ${c.name}`;
      for (const u of ids) grp.appendChild(new Option(u, u));
      sel.appendChild(grp);
    }
  };
  fill($("linkFrom"));
  fill($("linkTo"));
  if ($("linkTo").options.length > 1) {
    const other = [...$("linkTo").options].find((o) => o.value.split(".")[0] !== $("linkFrom").value.split(".")[0]);
    if (other) $("linkTo").value = other.value;
  }
  $("linkAdd").addEventListener("click", async () => {
    const body = { from: $("linkFrom").value, to: $("linkTo").value, bidir: $("linkBidir").checked };
    const r = await act("/api/link", body);
    if (r) { toast(`link ${r.link.describe}`); reloadWiring(r.wiring); }
  });
  $("btnExport").addEventListener("click", () => saveUrl("/api/board/yaml?download=1"));
  $("netList").addEventListener("click", (e) => {
    const row = e.target.closest(".net-row");
    if (!row || e.target.closest(".net-name input")) return;
    const name = row.dataset.net;
    if (e.target.closest(".net-del")) { removeNet(name); return; }
    if (e.target.closest(".net-ren")) { renameNet(name); return; }
    selectNet(ui.selNet === name ? null : name);
  });
  $("netList").addEventListener("dblclick", (e) => {
    const row = e.target.closest(".net-row");
    if (row && !e.target.closest("button, input")) renameNet(row.dataset.net);
  });
  $("netList").addEventListener("mouseover", (e) => {
    const row = e.target.closest(".net-row");
    if (row && ui.board.hlNet !== row.dataset.net) ui.board.highlightNet(row.dataset.net);
  });
  $("netList").addEventListener("mouseleave", () => ui.board.highlightNet(null));
  $("linkList").addEventListener("click", async (e) => {
    const b = e.target.closest(".link-del");
    if (!b) return;
    removeLink(b.dataset.from, b.dataset.to);
  });
  renderWiringPanel();
}

function renderWiringPanel() {
  if (!ui.isBoard) return;
  const nets = ui.info.nets || [];
  $("netCount").textContent = nets.length ? `${nets.length}` : "";
  $("netList").innerHTML = nets.length ? nets.map((n) => {
    const lv = n.level || "X";
    return `<div class="net-row${n.name === ui.selNet ? " sel" : ""}" data-net="${esc(n.name)}" title="click: select · double-click: rename">
      <span class="badge ${lv}" data-lv>${lv}</span>
      <span class="net-name">${esc(n.name)}</span>
      <span class="net-conf" ${n.conflict ? "" : "hidden"}>conflict</span>
      <span class="net-tools"><button class="btn small ghost net-ren" title="rename (inline)" aria-label="rename net ${esc(n.name)}">✎</button><button class="btn small ghost net-del" title="remove net (undo in the toast)" aria-label="remove net ${esc(n.name)}">×</button></span>
      <span class="net-pins">${n.pins.map((q) => `<span class="${chipCls(q.split(".")[0])}">${esc(q)}</span>`).join(" ")}${(n.ports || []).map((q) => ` <span class="net-port">${esc(q)}</span>`).join("")}${n.pull ? ` <i>pull ${esc(n.pull)}</i>` : ""}</span>
    </div>`;
  }).join("") : `<div class="empty">no nets — press W (wire mode) and click two pins</div>`;
  const links = ui.info.links || [];
  const buses = ui.info.buses || [];
  $("linkCount").textContent = links.length + buses.length ? `${links.length + buses.length}` : "";
  const uartRows = links.map((ln) => `<div class="link-row${ln.auto ? " auto" : ""}" data-link="${esc(ln.describe || `${ln.from}-${ln.to}`)}">
      <span class="type-tag uart">uart</span>
      <span class="${chipCls(ln.from.split(".")[0])}">${esc(ln.from)}</span> ${ln.bidir === false ? "→" : "↔"}
      <span class="${chipCls(ln.to.split(".")[0])}">${esc(ln.to)}</span>
      <span class="lstat" data-ustat></span>
      ${ln.auto ? `<span class="auto-tag" title="created from net ${esc(ln.net)}: TX and RX pins on one net">auto · ${esc(ln.net)}</span>`
        : `<button class="btn small ghost link-del" data-from="${esc(ln.from)}" data-to="${esc(ln.to)}" title="remove link (undo in the toast)" aria-label="remove link ${esc(ln.from)} to ${esc(ln.to)}">×</button>`}
    </div>`);
  const busRows = buses.map((b, i) => `<div class="link-row bus-row" data-bus="${esc(b.name)}" data-i="${i}" title="${esc(b.describe || "")}">
      <span class="type-tag">${esc(b.type)}</span><b>${esc(b.name)}</b>
      ${(b.nodes || []).map((q) => `<span class="${chipCls(String(q).split(".")[0])}">${esc(q)}</span>`).join(" ")}
      ${b.host ? `<span class="na">+ host${typeof b.host === "object" && b.host.ip ? ` ${esc(b.host.ip)}` : ""}</span>` : ""}
      <span class="lstat" data-bstat>${esc(busStatText(b.type, b.stats))}</span>
    </div>`);
  $("linkList").innerHTML = uartRows.length + busRows.length ? [...uartRows, ...busRows].join("")
    : `<div class="empty">no links (UART links: add below; CAN / Ethernet / USB: board YAML)</div>`;
  if (ui.state && ui.state.board) { renderWiringLevels(ui.state.board); renderBusStats(ui.state.board); }
  applyWiringFilters();
}

// live counters of a bus (state.board.buses[i].stats / /api/chip buses[i].stats)
function busStatText(type, st) {
  if (!st) return "";
  const n = (v) => (v == null ? "—" : Number(v).toLocaleString());
  if (type === "can") return `${n(st.frames)} frames${st.error_frames ? ` · ${n(st.error_frames)} errors` : ""}`;
  if (type === "eth") return `${n(st.frames)} frames · ${n(st.bytes)} B${st.host ? ` · host ${n(st.host.sent)}↑ ${n(st.host.received)}↓${st.host.ping_replies ? ` · ${st.host.ping_replies} ping replies` : ""}` : ""}`;
  if (type === "usb") return `${st.host_state || "—"}${st.device ? ` · ${st.device}` : ""} · ${n(st.transactions)} tr · ${n(st.bytes_in)}↓ ${n(st.bytes_out)}↑ B`;
  return Object.entries(st).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
}

function renderBusStats(board) {
  const buses = board.buses || [];
  for (const row of $("linkList").querySelectorAll(".bus-row")) {
    const i = +row.dataset.i;
    const b = (ui.info.buses || [])[i];
    const live = buses.find((x) => x.name === row.dataset.bus) || buses[i];
    if (!b || !live) continue;
    const t = busStatText(live.type || b.type, live.stats || live);
    const el = row.querySelector("[data-bstat]");
    if (el.textContent !== t) el.textContent = t;
  }
  const ls = board.links || [];
  for (const row of $("linkList").querySelectorAll(".link-row:not(.bus-row)")) {
    const s = ls.find((x) => x.link === row.dataset.link);
    if (!s) continue;
    const t = `${s.a_to_b ?? 0} B${s.b_to_a ? ` / ${s.b_to_a} B` : ""}`;
    const el = row.querySelector("[data-ustat]");
    if (el && el.textContent !== t) el.textContent = t;
  }
}

function focusBusRow(name) {
  if (name == null) return;
  const pane = $("paneLinks");
  if (pane.classList.contains("collapsed")) setCollapsed(pane, false);
  const row = [...$("linkList").querySelectorAll(".bus-row")].find((r) => r.dataset.bus === String(name));
  if (!row) return;
  row.scrollIntoView({ block: "nearest" });
  row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
}

function renderWiringLevels(board) {
  const box = $("netList");
  if (!box || !ui.isBoard) return;
  const by = new Map((board.nets || []).map((n) => [n.name, n]));
  for (const row of box.children) {
    const n = by.get(row.dataset.net);
    if (!n) continue;
    const b = row.querySelector("[data-lv]");
    const lv = n.level || "X";
    if (b.textContent !== lv) { b.textContent = lv; b.className = `badge ${lv}`; }
    const c = row.querySelector(".net-conf");
    if (c.hidden === !!n.conflict) c.hidden = !n.conflict;
    row.classList.toggle("conflict", !!n.conflict);
  }
}

// ---------------------------------------------------------------- data tools: filters, copy and download per pane
function filterList(list, sel, m, countEl, total) {
  let shown = 0;
  for (const row of list.querySelectorAll(sel)) {
    const hide = !!m && !m.test(row.textContent + " " + (row.dataset.net || row.dataset.part || row.dataset.bus || ""));
    row.classList.toggle("hidden", hide);
    if (!hide) shown++;
  }
  if (countEl) countEl.textContent = total ? (m ? `${shown}/${total}` : `${total}`) : "";
}

function applyWiringFilters() {
  if (!ui.isBoard) return;
  filterList($("netList"), ".net-row", $("netFilter")._m, $("netCount"), (ui.info.nets || []).length);
  filterList($("linkList"), ".link-row", $("linkFilter")._m, $("linkCount"), (ui.info.links || []).length + (ui.info.buses || []).length);
  filterList($("partList"), ".part-row", $("partFilter")._m, $("partCount"), (ui.info.parts || []).length);
}

const visibleNames = (list, sel, attr) => new Set([...list.querySelectorAll(sel)].filter((r) => !r.classList.contains("hidden"))
  .map((r) => r.dataset[attr]));

function netRows() {
  const vis = visibleNames($("netList"), ".net-row", "net");
  const lv = new Map(((ui.state && ui.state.board && ui.state.board.nets) || []).map((n) => [n.name, n]));
  return [["net", "level", "conflict", "pull", "pins"],
    ...(ui.info.nets || []).filter((n) => vis.has(n.name)).map((n) => {
      const l = lv.get(n.name) || n;
      return [n.name, l.level || "X", l.conflict ? "yes" : "", n.pull || "", n.pins.join(" ")];
    })];
}

function linkRows() {
  const out = [["type", "name", "from_or_nodes", "to", "both_ways", "auto", "net", "stats"]];
  const rows = [...$("linkList").querySelectorAll(".link-row")].filter((r) => !r.classList.contains("hidden"));
  const visU = new Set(rows.filter((r) => !r.classList.contains("bus-row")).map((r) => r.dataset.link));
  const visB = new Set(rows.filter((r) => r.classList.contains("bus-row")).map((r) => r.dataset.bus));
  for (const ln of ui.info.links || []) {
    if (!visU.has(ln.describe || `${ln.from}-${ln.to}`)) continue;
    const s = ((ui.state && ui.state.board && ui.state.board.links) || []).find((x) => x.link === ln.describe);
    out.push(["uart", "", ln.from, ln.to, ln.bidir === false ? "no" : "yes", ln.auto ? "yes" : "", ln.net || "",
      s ? `a_to_b=${s.a_to_b} b_to_a=${s.b_to_a}` : ""]);
  }
  const live = (ui.state && ui.state.board && ui.state.board.buses) || [];
  for (const b of ui.info.buses || []) {
    if (!visB.has(b.name)) continue;
    const l = live.find((x) => x.name === b.name);
    out.push([b.type, b.name, (b.nodes || []).join(" "), b.host ? "host" : "", "", "", "", busStatText(b.type, (l && (l.stats || l)) || b.stats)]);
  }
  return out;
}

function partRows() {
  const vis = visibleNames($("partList"), ".part-row", "part");
  return [["id", "type", "state", "ports", "params"],
    ...(ui.info.parts || []).filter((p) => vis.has(p.id)).map((p) => {
      const st = ui.partState[p.id] || p.state || {};
      return [p.id, p.type, panelStateText(p, st),
        Object.entries(p.ports || {}).map(([k, v]) => `${k}=${v || "-"}`).join(" "), JSON.stringify(p.params || {})];
    })];
}

function pinRows() {
  if (ui.discOn) return tableRows($("discTable"));
  const meta = ui.metaByChip.get(ui.sel);
  return [["chip", "num", "pin", "owner", "dir", "level", "freq_hz", "duty_pct", "edges", "analog", "pull", "external", "net", "datasheet_name"],
    ...ui.pinVisible.map((r) => {
      const p = meta.get(r.num) || {};
      const w = ui.waves[keyOf(ui.sel, r.num)];
      const a = analogOf(ui.sel, p);
      return [ui.sel, r.num, p.short ?? "", r.owner, r.direction, r.level || "X", w?.freq_hz ?? "", w?.duty ?? "", w?.edges ?? "",
        a ?? "", r.pull ?? "", r.external ?? "", r.net ?? "", p.name ?? ""];
    })];
}

function filterDiscovery() {
  const m = $("pinFilter")._m;
  for (const tr of $("discTable").tBodies[0].rows) tr.classList.toggle("hidden", !!m && !m.test(tr.textContent));
}

function initDataTools() {
  // UART
  bindFilter($("uartFilter"), () => { $("uartOut")._rawHtml = null; $("uartOut")._rawShown = null; ui.uartFull = true;
    if (ui.uartMode === "raw") renderRaw(null); else renderTranscript(); });
  ui.uartFollow = new Follow($("uartOut"), $("uartFollow"));
  $("uartCopy").addEventListener("click", () => {
    const m = $("uartFilter")._m;
    const t = ui.uartMode === "raw" ? (m ? rawVisible(ui.uart).split("\n").filter((l) => m.test(l)).join("\n") + "\n" : rawVisible(ui.uart))
      : transcriptText();
    copyText(t, "line");
  });
  $("uartDl").addEventListener("click", () => {
    if (ui.uartMode === "raw" && !$("uartFilter")._m) saveUrl(`/api/uart?uart=${encodeURIComponent(ui.uart)}&format=txt`);
    else if (ui.uartMode === "raw") {
      const m = $("uartFilter")._m;
      saveFile(`${ui.uart}_filtered_${stamp()}.txt`, rawVisible(ui.uart).split("\n").filter((l) => m.test(l)).join("\n") + "\n");
    } else saveFile(`${ui.uartAll ? "uart_all" : ui.uart}_lines_${stamp()}.txt`, transcriptText());
  });
  $("linkMonWrap").addEventListener("toggle", () => { prefs.linkMon = $("linkMonWrap").open; savePrefs(); ui.uartDirty = true; schedule(); });
  $("rtOn").addEventListener("change", setRealtime);
  $("rtSpeed").addEventListener("change", setRealtime);
  bindPerf();
  // status
  bindFilter($("statFilter"), applyStatFilter);
  $("statCopy").addEventListener("click", () => copyText(toTsv(statRowsCsv()), "row"));
  $("statCsv").addEventListener("click", () => saveFile(`${fileBase()}_status_${stamp()}.csv`, toCsv(statRowsCsv()), "text/csv"));
  // events
  bindFilter($("evFilter"), () => refilterEvents());
  ui.evFollow = new Follow($("events"), $("evFollow"));
  $("evCopy").addEventListener("click", () => copyText(eventRowsText(), "event"));
  $("evTxt").addEventListener("click", () => saveUrl(eventsUrl("txt")));
  $("evCsv").addEventListener("click", () => saveUrl(eventsUrl("csv")));
  // pins / discovery
  bindFilter($("pinFilter"), () => { renderPinTable(); filterDiscovery(); });
  $("pinCopy").addEventListener("click", () => copyText(toTsv(pinRows()), "row"));
  $("pinCsv").addEventListener("click", () => saveFile(`${fileBase()}_${ui.discOn ? "discovery" : "pins"}_${ui.sel}_${stamp()}.csv`,
    toCsv(pinRows()), "text/csv"));
  $("pinVcd").addEventListener("click", () => {
    const p = new URLSearchParams();
    if (ui.traced.size) p.set("pins", [...ui.traced].map(tracePin).filter(Boolean).map((x) => x.qname).join(","));
    else if (ui.isBoard) p.set("chip", ui.sel);
    saveUrl(`/api/vcd?${p}`);
  });
  // wiring panes
  for (const id of ["netFilter", "linkFilter", "partFilter"]) bindFilter($(id), applyWiringFilters);
  $("netCopy").addEventListener("click", () => copyText(toTsv(netRows()), "row"));
  $("netCsv").addEventListener("click", () => saveFile(`${fileBase()}_nets_${stamp()}.csv`, toCsv(netRows()), "text/csv"));
  $("linkCopy").addEventListener("click", () => copyText(toTsv(linkRows()), "row"));
  $("linkCsv").addEventListener("click", () => saveFile(`${fileBase()}_links_${stamp()}.csv`, toCsv(linkRows()), "text/csv"));
  $("partCopy").addEventListener("click", () => copyText(toTsv(partRows()), "row"));
  $("partCsv").addEventListener("click", () => saveFile(`${fileBase()}_parts_${stamp()}.csv`, toCsv(partRows()), "text/csv"));
  // popovers: help, CPU registers
  const help = (on) => { $("helpPop").hidden = !on; $("btnHelp").setAttribute("aria-expanded", String(on)); };
  const regs = (on) => { $("regsPop").hidden = !on; $("btnRegs").setAttribute("aria-expanded", String(on)); if (on) { placeRegs(); renderRegs(); } };
  $("btnHelp").addEventListener("click", (e) => { e.stopPropagation(); help($("helpPop").hidden); });
  $("btnRegs").addEventListener("click", (e) => { e.stopPropagation(); regs($("regsPop").hidden); });
  $("regsCopy").addEventListener("click", () => copyText(regRows().map(([k, v]) => `${k}\t${v}`).join("\n") + "\n", "register"));
  document.addEventListener("click", (e) => {
    if (!$("helpPop").hidden && !e.target.closest("#helpPop")) help(false);
    if (!$("regsPop").hidden && !e.target.closest("#regsPop")) regs(false);
    if (!$("restartPop").hidden && !e.target.closest("#restartPop, #btnRestart")) $("restartPop").hidden = true;
  });
  ui.help = help;
  ui.regs = regs;
  $("errText").addEventListener("click", () => copyText($("errText").dataset.full || "", "line"));
  $("uartOut").dataset.empty = "no output yet";
}

function placeRegs() {
  const r = $("btnRegs").getBoundingClientRect();
  const pop = $("regsPop");
  pop.style.left = `${Math.max(8, Math.min(window.innerWidth - 310, r.left))}px`;
}

// "/" target: the filter of the pane under the pointer, else the last used one, else the event log's
function focusFilter() {
  let pane = ui.hoverPane;
  let f = pane && visiblePane(pane) ? pane.querySelector(".pane-filter") : null;
  if (!f) f = ui.lastFilter || $("evFilter");
  pane = f.closest(".pane");
  if (pane && pane.classList.contains("collapsed")) setCollapsed(pane, false);
  f.focus();
  f.select();
}

// ---------------------------------------------------------------- controls
function stepN() {
  const n = parseInt($("stepN").value, 10);
  if (!Number.isFinite(n) || n < 1 || n > 1e6) { toastErr("step: N must be 1 … 1000000"); return; }
  act("/api/step", ui.isBoard ? { n, chip: ui.sel } : { n });
}

function doReset() {
  act("/api/reset");
  ui.baseline = null;
}

// timed stimulus: "10ms:PTC6=0" (absolute), "+5ms:B.PA0=H" (from now), "PTC6=H" (now); history in a datalist
async function sendStimulus() {
  const spec = $("stimText").value.trim().replace(/^at\s+/i, "");
  if (!spec) return;
  const r = await act("/api/stimulus", { spec });
  if (!r) return;
  toast(r.t_ns != null ? `${spec}: scheduled at ${fmtTime(r.t_ns)}` : `${spec}: applied`);
  const h = (prefs.stimHist ||= []).filter((x) => x !== spec);
  h.unshift(spec);
  prefs.stimHist = h.slice(0, 20);
  savePrefs();
  fillStimHist();
  $("stimText").value = "";
}
function fillStimHist() {
  $("stimHist").innerHTML = (prefs.stimHist || []).map((x) => `<option value="${esc(x)}"></option>`).join("");
}

function wireControls() {
  $("tabPins").addEventListener("click", () => showTab("pins"));
  $("tabDisc").addEventListener("click", () => showTab("disc"));
  $("tabLogic").addEventListener("click", () => showTab("logic"));
  $("tabDebug").addEventListener("click", () => showTab("debug"));
  $("tabRegs").addEventListener("click", () => showTab("regs"));
  tabKeys(document.querySelector("#panePins .tabs"), (b) => b.click());
  const urlTab = new URLSearchParams(location.search).get("tab");     // deep link: /?tab=debug | regs | logic | disc
  if (urlTab && $(`tab${{ pins: "Pins", disc: "Disc", logic: "Logic", debug: "Debug", regs: "Regs" }[urlTab] || ""}`)) showTab(urlTab);
  else if (prefs.tab && prefs.tab !== "pins") showTab(prefs.tab);
  $("discRefresh").addEventListener("click", loadDiscovery);
  $("discStatic").addEventListener("change", loadDiscovery);
  $("btnRun").addEventListener("click", () => act("/api/run", {}));
  $("btnPause").addEventListener("click", () => act("/api/pause"));
  $("btnStep").addEventListener("click", stepN);
  $("stepN").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); stepN(); } });
  $("btnReset").addEventListener("click", doReset);
  $("btnRestart").addEventListener("click", () => {
    const pop = $("restartPop");
    pop.hidden = !pop.hidden;
    if (!pop.hidden) {
      const r = $("btnRestart").getBoundingClientRect();
      pop.style.left = `${Math.max(8, r.left)}px`;
      $("restartNo").focus();
    }
  });
  $("restartNo").addEventListener("click", () => { $("restartPop").hidden = true; $("btnRestart").focus(); });
  $("restartYes").addEventListener("click", async () => {
    $("restartPop").hidden = true;
    const r = await act("/api/restart", {});
    if (r) { toast("restarting at t=0…"); setTimeout(() => location.reload(), 300); }
  });
  const runFor = () => {
    const ms = parseDurationMs($("runFor").value);
    if (ms == null || ms <= 0) { toastErr("duration: e.g. 500us, 10ms, 1.5s"); return; }
    act("/api/run", { ms });
  };
  $("btnRunFor").addEventListener("click", runFor);
  $("runFor").addEventListener("keydown", (e) => { if (e.key === "Enter") runFor(); });
  $("btnWire").addEventListener("click", () => setWireMode(!ui.wireMode));
  $("stimForm").addEventListener("submit", (e) => { e.preventDefault(); sendStimulus(); });
  fillStimHist();

  $("uartForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!ui.uart) { toastErr("no UART"); return; }
    const eol = $("uartEol").value;
    const typed = $("uartText").value;
    const r = await act("/api/uart", { uart: ui.uart, text: typed + eol });
    if (r) {
      $("uartText").value = "";
      pushLine({ uart: ui.uart, chip: ui.isBoard ? ui.uart.split(".")[0] : evChip({}), text: typed, input: true });
      ui.uartDirty = true;
      schedule();
    }
  });
  // clear: the view of every UART (transcript and raw) and every drop counter; the emulator keeps the output
  $("uartClear").addEventListener("click", () => {
    ui.uartSkip = ui.uartSkip || {};
    const lens = (ui.state && ui.state.uart_out_len) || {};
    for (const u of new Set([...Object.keys(lens), ...Object.keys(ui.uartLen)])) ui.uartSkip[u] = lens[u] ?? ui.uartLen[u] ?? 0;
    ui.lines.length = 0;
    ui.openLine.clear(); ui.afterCR.clear(); ui.linesDirty.clear();
    $("uartOut").textContent = "";
    $("uartOut")._rawShown = null;
    ui.uartFull = true;
    ui.uartDrops.clear(); ui.uartErrs.clear();
    showUartDrops();
  });
  $("evClear").addEventListener("click", () => {
    $("events").innerHTML = ""; ui.evDropped = 0; ui.kindCount = {}; ui.ring = {}; ui.lastEvRow = null;
    renderEvCount(); ui.evFollow.end();
  });

  for (const id of ["changedOnly", "hideDisabled"]) $(id).addEventListener("input", () => renderPinTable());
  for (const th of $("pinTable").tHead.rows[0].cells) {
    const sort = () => {
      const k = th.dataset.k;
      ui.sort = { k, desc: ui.sort.k === k ? !ui.sort.desc : false };
      $("pinTable").tBodies[0]._sig = null;
      renderPinTable();
    };
    th.addEventListener("click", sort);
    th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); sort(); } });
  }
  const tbody = $("pinTable").tBodies[0];
  const rowAction = (e, kbd) => {
    const tr = e.target.closest("tr[data-num]");
    if (!tr) return;
    const num = +tr.dataset.num;
    const chip = ui.chipById.get(ui.sel);
    const pin = ui.metaByChip.get(ui.sel)?.get(num);
    if (!pin) return;
    if (e.target.closest("td.ext") && tr.classList.contains("clickable")) { clickPin(chip, pin); return; }
    if (e.target.closest("td.ana.edit")) { editAnalog(e.target.closest("td.ana"), ui.sel, pin); return; }
    if (e.target.closest("input")) return;
    selectPinRow(ui.sel, num);
    if (!kbd) {
      ui.board.zoomToPin?.(ui.sel, num);
      const it = ui.board.pkgs.get(ui.sel)?.items.get(num);
      if (it && ui.board.pinCard) { const r = it.g.getBoundingClientRect(); ui.board.pinCard(ui.sel, num, { clientX: r.right, clientY: r.bottom }); }
    }
  };
  tbody.addEventListener("click", (e) => rowAction(e, false));
  tbody.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.closest("td.ext, td.ana.edit")) { e.preventDefault(); e.stopPropagation(); rowAction(e, true); }
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(); });
  document.addEventListener("keydown", onKey);
}

// global shortcuts: case-insensitive, never while typing, never behind a modal dialog, Space presses a focused button
function onKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (/^F\d+$/.test(e.key) && !document.body.classList.contains("modal-open") && ui.debug?.onKey?.(e)) return;   // debugger F-keys
  const t = e.target;
  const inField = t.closest && t.closest("input, select, textarea, [contenteditable]");
  const inDlg = t.closest && t.closest("#partDlg, #boardDlg, #restartPop");
  if (e.key === "Escape") {                          // Esc works everywhere, also from inside dialog fields
    if (!$("restartPop").hidden) { $("restartPop").hidden = true; $("btnRestart").focus(); return; }
    if (ui.framePrev?.open) { ui.framePrev.close(); return; }
    if (!$("helpPop").hidden) { ui.help(false); return; }
    if (!$("regsPop").hidden) { ui.regs(false); $("btnRegs").focus(); return; }
    if (ui.pickTarget) { e.preventDefault(); pickDone(null); return; }
    if (!$("partDlg").hidden) { e.preventDefault(); closePartDialog(); return; }
    if (boardDialogOpen()) return;                   // <dialog> handles Esc itself (cancel event)
    if (inField) return;                             // filters clear themselves (bindFilter)
    if (ui.pendingPin) { ui.pendingPin = null; ui.board.setPending(null); }
    else if (ui.wireMode) setWireMode(false);
    else if (ui.selNet) selectNet(null);
    else if (ui.selPin) { ui.selPin = null; for (const tr of $("pinTable").tBodies[0].querySelectorAll("tr.sel")) tr.classList.remove("sel"); }
    return;
  }
  if (inField || inDlg || document.body.classList.contains("modal-open")) return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k === " ") {
    if (t.closest && t.closest("button, summary, [role=tab], a[href], th[tabindex], td[tabindex], .pin, [tabindex]:not(body):not(#events):not(#uartOut)")) return;   // native activation
    e.preventDefault(); act(ui.state?.running ? "/api/pause" : "/api/run", {}); return;
  }
  if (k === "/") {                                   // over the board: board.js focuses its pin finder
    if (ui.hoverPane && ui.hoverPane.id !== "paneBoard") { e.preventDefault(); focusFilter(); }
    return;
  }
  if (k === "?") { e.preventDefault(); ui.help($("helpPop").hidden); return; }
  if (k === "End") {
    const p = ui.hoverPane;
    if (p && p.id === "paneEvents") { e.preventDefault(); ui.evFollow.end(); return; }
    if (!p || p.id === "paneUart") { e.preventDefault(); ui.uartFollow.end(); return; }
    return;
  }
  if (k === "s") stepN();
  else if (k === "r") doReset();
  else if (k === "f") ui.board.fit();
  else if (k === "b") $("btnBoard").click();
  else if (k === "t" && ui.selPin) { const i = ui.selPin.lastIndexOf(":"); toggleTrace(ui.selPin.slice(0, i), +ui.selPin.slice(i + 1)); }
  else if (ui.isBoard && k === "w") setWireMode(!ui.wireMode);
  else if ((k === "Delete" || k === "Backspace") && ui.selNet) { e.preventDefault(); removeNet(ui.selNet); }
  else if (ui.isBoard && /^[1-9]$/.test(k) && ui.chips[+k - 1]) selectChip(ui.chips[+k - 1].id);
}

// ---------------------------------------------------------------- start: retry until the server answers
initTheme();
let bootTries = 0;
window.addEventListener("pagehide", () => { ui.leaving = true; });
function startBoot() {
  boot().then(() => { $("bootMsg").hidden = true; }).catch((e) => {
    if (ui.leaving) return;                          // navigation away aborted the requests: not a failure
    console.error(e);
    bootTries++;
    if (ui.board) {                                   // UI built, a later step failed: report, do not rebuild
      toastErr(`failed to load: ${e.message}`);
      return;
    }
    const m = $("bootMsg");
    m.hidden = false;
    m.textContent = `cannot load the emulator state: ${e.message}\nretrying every 2 s (attempt ${bootTries})…`;
    $("conn").className = "conn bad"; $("conn").textContent = "offline";
    $("status").textContent = "OFFLINE"; $("status").className = "status-badge fault";
    setTimeout(startBoot, 2000);
  });
}
startBoot();
