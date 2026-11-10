// chipem web UI: talks to chipem/web/server.py (JSON API + Server-Sent Events). No build step.
// Works for a single chip and for a board (several chips): /api/chip `chips` lists them; every pin row carries
// `chip`; names sent to the server are qualified ("B.PTC6", "B.UART0") on a board.
import { ownerCategory } from "./package.js";
import { BoardView, fmtRate } from "./board.js";
import { stateText, counterValue, ledColor, LED_COLORS, partCategory, portList } from "./parts.js";
import { initBoardDialog, boardDialogOpen, closeBoardDialog } from "./boarddlg.js";

const $ = (id) => document.getElementById(id);
const KINDS = ["pin", "part", "uart", "can", "i2s", "eth", "usb", "irq", "reset", "fault", "warn", "info", "mmio"];
const MAX_EVENT_ROWS = 1500;
const MAX_LINES = 5000;           // UART transcript lines kept
const MAX_LINE_CHARS = 2000;      // a line without a line ending (binary data) is wrapped into a new one here
const RAW_KEEP = 1 << 20;         // raw UART text kept in the browser (chars); the full output is a download away
const RAW_SHOW = 200000;          // raw UART text kept in the DOM
const MON_BYTES = 64;             // link monitor: last bytes per direction

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
  wiring: 0,                      // board wiring version of ui.info (nets/links); refetched when it changes
  wireMode: false,
  pendingPin: null,               // wire mode: first picked pin {cid, pin}
  selNet: null,
  partState: {},                  // part id -> latest state (state frames + `part` events)
  partsDirty: false,
  partTypes: null,                // /api/parts/types (loaded when the add-part dialog first opens)
  pickTarget: null,               // add-part dialog: the port input a board click fills
  wiringSig: "",
};

// ---------------------------------------------------------------- helpers
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

// Toasts: informational ("copied 12 lines", "net NET3") in the neutral info style, failures in the error style
// (role=alert, shown longer).
let toastTimer = null;
function toast(msg, kind = "info") {
  const t = $("toast");
  t.textContent = msg;
  t.className = `toast toast-${kind}`;
  t.setAttribute("role", kind === "error" ? "alert" : "status");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === "error" ? 6000 : 3000);
}
const toastErr = (msg) => toast(msg, "error");

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

export function fmtTime(ns) {
  if (ns == null) return "—";
  if (ns >= 1e9) return `${(ns / 1e9).toFixed(6)} s`;
  if (ns >= 1e6) return `${(ns / 1e6).toFixed(3)} ms`;
  if (ns >= 1e3) return `${(ns / 1e3).toFixed(3)} µs`;
  return `${Math.round(ns)} ns`;
}
const fmtHz = (hz) => (hz >= 1e6 ? `${+(hz / 1e6).toFixed(3)} MHz` : `${+(hz / 1e3).toFixed(3)} kHz`);
const hex32 = (v) => "0x" + (v >>> 0).toString(16).toUpperCase().padStart(8, "0");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const qual = (cid, name) => (ui.isBoard ? `${cid}.${name}` : name);
const keyOf = (cid, num) => `${cid}:${num}`;
const evChip = (ev) => ev.chip ?? (ui.chips[0] && ui.chips[0].id) ?? "A";

function parseDurationMs(s) {
  const m = /^\s*([0-9]*\.?[0-9]+(?:e[-+]?\d+)?)\s*(ns|us|µs|ms|s)?\s*$/i.exec(s);
  if (!m) return null;
  const mul = { ns: 1e-6, us: 1e-3, "µs": 1e-3, ms: 1, s: 1e3 }[(m[2] || "ms").toLowerCase()];
  return parseFloat(m[1]) * mul;
}

function ownerCat(pin, row) {
  if (pin.kind === "power") return "power";
  if (pin.kind === "analog" || row.owner === "analog") return "analog";
  if (row.owner === "disabled" || row.owner === pin.kind) return "disabled";
  const c = ownerCategory(pin, row.owner);
  return c === "none" ? "disabled" : c;
}

const rowKey = (r) => `${r.level}|${r.owner}|${r.direction}|${r.pull}|${r.external}|${r.cfg_by || ""}|${fmtWave(r.wave)}`;
// pin waveform summary (server: pinreport.pin_waves): "20.000 kHz 25.0%" for a periodic pin, "" otherwise
function fmtWave(w) {
  if (!w || !w.freq_hz) return "";
  const f = w.freq_hz;
  const fs = f >= 1e6 ? `${(f / 1e6).toFixed(3)} MHz` : f >= 1e3 ? `${(f / 1e3).toFixed(3)} kHz` : `${f.toFixed(1)} Hz`;
  return `${fs} ${w.duty == null ? "?" : w.duty.toFixed(1)}%`;
}

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
const PANE_W = { uart: 380, events: 320, nets: 150, links: 110, parts: 170 };

const visiblePane = (p) => p && p.classList.contains("pane") && p.offsetParent !== null;

function setCollapsed(pane, on, save = true) {
  pane.classList.toggle("collapsed", on);
  const t = pane.querySelector(".pane-tog");
  if (t) t.setAttribute("aria-expanded", String(!on));
  if (save) { prefs.collapsed[pane.dataset.pane] = on; savePrefs(); }
  if (pane.id === "panePins") window.dispatchEvent(new Event("resize"));
}

function initPanes() {
  const shell = document.querySelector(".shell");
  if (prefs.sideW) shell.style.setProperty("--side-w", `${prefs.sideW}px`);
  else shell.style.setProperty("--side-w", `${Math.round(Math.min(560, Math.max(340, window.innerWidth * 0.3)))}px`);
  if (prefs.trayH) shell.style.setProperty("--tray-h", `${prefs.trayH}px`);
  else shell.style.setProperty("--tray-h", `${Math.round(Math.max(150, window.innerHeight * 0.26))}px`);
  for (const pane of document.querySelectorAll(".pane[data-pane]")) {
    const name = pane.dataset.pane;
    if (pane.parentElement.id === "sidebar") pane.style.setProperty("--w", prefs.w[name] ?? PANE_W[name] ?? 200);
    // defaults: the links list is short and the parts list may be empty: collapsed
    const def = !ui.isBoard ? false : name === "links" || (name === "parts" && !(ui.info.parts || []).length);
    if (pane.querySelector(".pane-tog")) setCollapsed(pane, prefs.collapsed[name] ?? def, false);
    const tog = pane.querySelector(".pane-tog");
    if (tog) tog.addEventListener("click", () => setCollapsed(pane, !pane.classList.contains("collapsed")));
    pane.querySelector(".pane-head").addEventListener("dblclick", (e) => {
      if (!tog || e.target.closest("button, input, select, label, .tab, .kind-filter")) return;
      setCollapsed(pane, !pane.classList.contains("collapsed"));
    });
    pane.addEventListener("mouseenter", () => { ui.hoverPane = pane; });
  }
  // splitters
  drag($("splitSide"), "v", () => ({ w: $("sidebar").getBoundingClientRect().width }), (s, dx) => {
    const w = Math.round(Math.max(260, Math.min(window.innerWidth * 0.75, s.w - dx)));
    shell.style.setProperty("--side-w", `${w}px`);
    prefs.sideW = w;
  });
  drag($("splitTray"), "h", () => ({ h: $("panePins").getBoundingClientRect().height,
                                     max: document.querySelector(".maincol").getBoundingClientRect().height - 140 }), (s, _dx, dy) => {
    if ($("panePins").classList.contains("collapsed")) return;
    const h = Math.round(Math.max(60, Math.min(s.max, s.h - dy)));
    shell.style.setProperty("--tray-h", `${h}px`);
    prefs.trayH = h;
  });
  for (const sp of document.querySelectorAll(".split-p")) {
    drag(sp, "h", () => {
      const open = [...$("sidebar").children].filter((p) => visiblePane(p) && !p.classList.contains("collapsed"));
      for (const p of open) p.style.setProperty("--w", p.getBoundingClientRect().height);   // weights := px
      let a = sp.previousElementSibling, b = sp.nextElementSibling;
      while (a && !(visiblePane(a) && !a.classList.contains("collapsed"))) a = a.previousElementSibling;
      while (b && !(visiblePane(b) && !b.classList.contains("collapsed"))) b = b.nextElementSibling;
      return a && b ? { a, b, ha: a.getBoundingClientRect().height, hb: b.getBoundingClientRect().height } : null;
    }, (s, _dx, dy) => {
      if (!s) return;
      const tot = s.ha + s.hb;
      const ha = Math.max(64, Math.min(tot - 64, s.ha + dy));
      s.a.style.setProperty("--w", ha);
      s.b.style.setProperty("--w", tot - ha);
    }, () => {
      for (const p of $("sidebar").children) {
        if (visiblePane(p) && !p.classList.contains("collapsed")) prefs.w[p.dataset.pane] = Math.round(p.getBoundingClientRect().height);
      }
    });
  }
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
  });
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

  const st = await api("/api/state");
  applyState(st);
  const hist = await api(`/api/events?since=0&limit=500`);
  for (const ev of hist.events) if (ev.kind !== "mmio" || ui.kindsOn.has("mmio")) ui.evQueue.push(ev);
  ui.lastEventSeq = hist.last || 0;
  // full UART history for the transcript / link monitor (one event per byte)
  try {
    const u = await api(`/api/events?since=0&kinds=uart&limit=0`);
    for (const ev of u.events) if (ev.seq <= ui.lastEventSeq) uartEvent(ev, false);
  } catch { /* optional */ }
  ui.uartDirty = true;
  schedule();
  refreshUart(true);
  connect();
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
    b.title = `${c.name} · ${c.package}${c.firmware ? ` · ${c.firmware}` : ""}`;
    b.innerHTML = `<b>${esc(c.id)}</b><span class="cn"> ${esc(c.name)}</span>`;
    b.addEventListener("click", () => selectChip(c.id));
    box.appendChild(b);
  }
}

function selectChip(cid, initial = false) {
  if (!ui.chipById.has(cid)) return;
  const changed = ui.sel !== cid;
  ui.sel = cid;
  for (const b of $("chipSel").children) b.classList.toggle("on", b.dataset.chip === cid);
  ui.board.setSelected(cid);
  const c = ui.chipById.get(cid);
  $("pinsChip").textContent = ui.isBoard ? `chip ${cid} · ${c.name}` : "";
  if (changed || initial) {
    $("pinTable").tBodies[0]._sig = null;
    renderPinTable();
    if (ui.state) renderReadouts(ui.state);
    if (ui.discOn) loadDiscovery();
  }
}

// ---------------------------------------------------------------- live stream
function connect() {
  if (ui.es) ui.es.close();
  const es = new EventSource(`/api/stream?since=${ui.lastEventSeq}${ui.kindsOn.has("mmio") ? "&mmio=1" : ""}`);
  ui.es = es;
  const conn = (cls, txt) => { $("conn").className = `conn ${cls}`; $("conn").textContent = txt; };
  es.addEventListener("open", () => conn("ok", "live"));
  es.addEventListener("error", () => conn("bad", "offline"));
  // handlers only queue work; rendering happens once per animation frame
  es.addEventListener("state", (m) => {
    const st = JSON.parse(m.data);
    if (st.boot && ui.boot && st.boot !== ui.boot) { location.reload(); return; }   // the server was restarted
    if (!st.pins && ui.pendingState && ui.pendingState.pins) st.pins = ui.pendingState.pins;   // keep unrendered pins
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
    if (ui.evQueue.length > 4 * MAX_EVENT_ROWS) ui.evQueue.splice(0, ui.evQueue.length - MAX_EVENT_ROWS);
    schedule();
  });
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
  ui.state = st;
  if (st.pins) applyPins(st);
  if (ui.discOn && st.last_stop !== ui._lastStop && !st.running) loadDiscovery();   // run finished: refresh
  ui._lastStop = st.last_stop;
  renderReadouts(st);
  renderHostSerial(st);
  if (st.board && !ui.isBoard && !ui._reloading) {             // a part was added elsewhere: board layout
    ui._reloading = true;
    location.reload();
    return;
  }
  if (st.board) {
    if ((st.board.wiring ?? 0) !== ui.wiring) reloadWiring(st.board.wiring ?? 0);
    if (st.board.parts) Object.assign(ui.partState, partStateMap(st.board.parts));
    ui.board.updateBoard(st.board);
    renderWiringLevels(st.board);
    renderPartStates();
  }
  // UART output grew?
  const lens = st.uart_out_len || {};
  if (!ui.uartPicked && ui.uartMode === "raw" && !(lens[ui.uart] > 0)) {   // follow the UART that prints
    const opts = new Set([...$("uartSel").options].map((o) => o.value));
    const best = Object.entries(lens).find(([u, n]) => n > 0 && opts.has(u));
    if (best) { ui.uart = best[0]; $("uartSel").value = best[0]; showUartDrops(); refreshUart(true); }
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

function applyPins(st) {
  const rows = st.pins;
  const byChip = new Map(ui.chips.map((c) => [c.id, []]));
  for (const r of rows) {
    const cid = r.chip ?? ui.chips[0].id;
    if (!byChip.has(cid)) byChip.set(cid, []);
    byChip.get(cid).push(r);
  }
  if (!ui.baseline || (st.last_stop === "RESET" && ui._lastStopPins !== "RESET")) {
    ui.baseline = new Map(rows.map((r) => [keyOf(r.chip ?? ui.chips[0].id, r.num), rowKey(r)]));
  }
  ui._lastStopPins = st.last_stop;
  const changed = new Map();
  const changedSel = new Set();
  for (const [cid, list] of byChip) {
    const set = new Set();
    for (const r of list) {
      const k = keyOf(cid, r.num);
      const rk = rowKey(r);
      const prev = ui.prevKey.get(k);
      if (prev !== rk) {
        if (prev !== undefined) set.add(r.num);
        if (cid === ui.sel) changedSel.add(r.num);
        ui.prevKey.set(k, rk);
      }
      ui.rowByKey.set(k, r);
    }
    changed.set(cid, set);
    ui.rows.set(cid, list);
  }
  ui.board.update(byChip, changed);
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
  const cs = chipState(st, ui.sel) || st;
  const t = st.board ? st.board.t_ns : (st.t_ns ?? st.time_ns);
  $("tTime").textContent = fmtTime(t);
  $("tSpeed").textContent = updateSpeed(st);
  $("tInsn").textContent = cs.insns != null ? cs.insns.toLocaleString() : "—";
  $("tCycles").textContent = (cs.cycles ?? 0).toLocaleString();
  $("tClock").textContent = cs.hz ? fmtHz(cs.hz) : "—";
  $("tPc").textContent = cs.pc != null ? hex32(cs.pc) : "—";
  const { label, cls, word, extra } = statusOf(st);
  let lab = label;
  if (cs.mode === "handler" && cs.active_name) lab += ` · in ${cs.active_name}`;
  const s = $("status");
  s.textContent = `${word}${extra ? ` ${extra}` : ""}${cs.mode === "handler" && cs.active_name ? ` · ${cs.active_name}` : ""}`;
  s.className = `status-badge ${cls}`;
  s.title = [lab, ui.isBoard ? `chip ${ui.sel}` : "", st.error || "", st.board ? `board skew ${fmtTime(st.board.skew_ns)}` : "",
    st.last_stop ? `last stop ${st.last_stop}` : ""].filter(Boolean).join(" · ");
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
    if (cid === ui.sel) renderPinTable(new Set([pin.num]));
  }
}

function pinCells(r, p) {
  const oc = ownerCat(p, r);
  const lv = r.level || "X";
  return `<td class="num">${r.num}</td><td>${esc(p.short)}</td>
      <td class="owner-${oc}">${esc(r.owner)}</td><td>${esc(r.direction)}</td>
      <td><span class="badge ${lv}">${lv}</span></td>
      <td class="wave" title="${r.wave ? `${r.wave.edges} edges` : ""}">${esc(fmtWave(r.wave))}</td><td>${esc(r.pull ?? "—")}</td>
      <td class="ext" title="${p.kind === "gpio" ? "click to cycle H → L → open" : ""}">${r.external ? `<span class="badge ${r.external}">${r.external}</span>` : "—"}</td>
      <td class="net">${esc(r.net ?? "")}</td><td class="name">${esc(r.name)}</td>`;
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
      const hay = [p.short, r.name, r.owner, r.direction, r.net, ...Object.values(p.functions || {})].join(" ");
      if (!m.test(hay)) return false;
    }
    return true;
  });
  const { k, desc } = ui.sort;
  rows.sort((a, b) => {
    let va = a[k] ?? "", vb = b[k] ?? "";
    if (k === "wave") { va = a.wave?.freq_hz ?? -1; vb = b.wave?.freq_hz ?? -1; }
    if (k === "short") { va = shortKey(meta.get(a.num).short); vb = shortKey(meta.get(b.num).short); }
    const c = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
    return desc ? -c : c || a.num - b.num;
  });
  ui.pinVisible = rows;
  $("pinCount").textContent = rows.length === all.length ? `${all.length}` : `${rows.length}/${all.length}`;
  const sig = cid + "|" + rows.map((r) => r.num).join(",");
  if (tb._sig !== sig) {
    tb.innerHTML = rows.map((r) => {
      const p = meta.get(r.num);
      return `<tr data-num="${r.num}" class="${changed.has(r.num) ? "flash" : ""}${p.kind === "gpio" ? " clickable" : ""}">${pinCells(r, p)}</tr>`;
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
      tr.innerHTML = pinCells(r, meta.get(r.num));
      tr.classList.remove("flash");
      void tr.offsetWidth;            // restart the flash animation
      tr.classList.add("flash");
      tb._keys.set(r.num, rk);
    }
  }
  for (const th of $("pinTable").tHead.rows[0].cells) {
    th.classList.toggle("sorted", th.dataset.k === k);
    th.classList.toggle("desc", th.dataset.k === k && desc);
  }
}

function shortKey(s) {   // natural sort: PTA2 < PTA10, GPIO2 < GPIO10
  return String(s).replace(/\d+/g, (d) => d.padStart(4, "0"));
}

// ---------------------------------------------------------------- events
function buildKindFilter() {
  const box = $("kindFilter");
  const mk = (label, cls, on, toggle, title) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `chip-toggle ${cls}` + (on ? " on" : "");
    b.innerHTML = `${esc(label)}<span class="n"></span>`;
    b.title = title || "";
    b.addEventListener("click", () => { b.classList.toggle("on", toggle()); refilterEvents(); });
    box.appendChild(b);
    return b;
  };
  ui.kindBtn = {};
  for (const k of KINDS) {
    ui.kindBtn[k] = mk(k, `k-${k}`, ui.kindsOn.has(k), () => {
      if (ui.kindsOn.has(k)) ui.kindsOn.delete(k); else ui.kindsOn.add(k);
      if (k === "mmio") connect();              // the stream sends mmio events only on request
      return ui.kindsOn.has(k);
    }, k === "mmio" ? "MMIO accesses (only logged when the session logs them; off by default)" : `show ${k} events`);
  }
  if (ui.isBoard) {
    const sep = document.createElement("span");
    sep.className = "sep";
    box.appendChild(sep);
    for (const cid of [...ui.chips.map((c) => c.id), "board"]) {
      mk(cid, "c-chip", true, () => {
        if (ui.chipsOn.has(cid)) ui.chipsOn.delete(cid); else ui.chipsOn.add(cid);
        return ui.chipsOn.has(cid);
      });
    }
  }
}

const evVisible = (kind, chip) => ui.kindsOn.has(kind) && ui.chipsOn.has(chip);
const evShow = (row, m) => evVisible(row.dataset.kind, row.dataset.chip) && (!m || m.test(row._txt));

function refilterEvents() {
  const m = $("evFilter")._m;
  const src = m ? m.src : "";
  for (const row of $("events").children) {
    row.hidden = !evShow(row, m);
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
  $("evCount").textContent = (shown === total ? `${total}` : `${shown}/${total}`) + (ui.evDropped ? ` · ${ui.evDropped} older dropped` : "");
  $("evCount").title = `rows in view (max ${MAX_EVENT_ROWS}); the downloads contain every event the emulator keeps (20000)`;
  for (const k of KINDS) {
    const n = ui.kindCount[k] || 0;
    const el = ui.kindBtn && ui.kindBtn[k] && ui.kindBtn[k].querySelector(".n");
    if (el) el.textContent = n ? (n >= 1e4 ? `${Math.round(n / 1e3)}k` : String(n)) : "";
  }
  box.dataset.empty = total ? `no events match (${total} hidden by the filters)` : "no events yet";
}

function eventRowsText() {
  return [...$("events").children].filter((r) => !r.hidden).map((r) => r._txt).join("\n") + "\n";
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
    (ui.isBoard ? `<span class="c c-${esc(chip)}">${h(chip)}</span>` : "") +
    `<span class="kind k-${esc(kind)}">${h(kind)}</span><span class="msg">${h(msg)}</span>`;
  row._hm = m ? m.src : "";
}

function addEvents(evs) {
  const box = $("events");
  for (const ev of evs) ui.kindCount[ev.kind] = (ui.kindCount[ev.kind] || 0) + 1;
  ui.board?.notePinEvents?.(evs);                 // pin card: level-change counts from the event log
  if (evs.length > MAX_EVENT_ROWS) { ui.evDropped += evs.length - MAX_EVENT_ROWS; evs = evs.slice(-MAX_EVENT_ROWS); }
  const m = $("evFilter")._m;
  const frag = document.createDocumentFragment();
  let added = 0;
  for (const ev of evs) {
    if (ev.kind === "mmio" && !ui.kindsOn.has("mmio")) continue;
    const chip = evChip(ev);
    const row = document.createElement("div");
    row.className = "ev";
    row.dataset.kind = ev.kind;
    row.dataset.chip = chip;
    const t = fmtTime(ev.t_ns), msg = evText(ev);
    row._txt = `${t}\t${ui.isBoard ? `${chip}\t` : ""}${ev.kind}\t${msg}`;     // = evfmt.event_line on the server
    row._cells = [t, chip, ev.kind, msg];
    row.hidden = !evShow(row, m);
    if (!row.hidden) added++;
    renderEvRow(row, m);
    frag.appendChild(row);
  }
  box.appendChild(frag);
  let extra = box.childElementCount - MAX_EVENT_ROWS;
  if (extra > 0) ui.evDropped += extra;
  while (extra-- > 0) box.firstElementChild.remove();
  renderEvCount();
  ui.evFollow.after(added);
}

// ---------------------------------------------------------------- UART: transcript, link monitor, raw output
function showUartDrops() {
  const el = $("uartDrops");
  const d = ui.uartDrops.get(ui.uart);
  el.hidden = !d;
  if (d) el.textContent = `RX dropped ${d.n} (${d.reason})`;
}

function uartEvent(ev, live) {
  if (ev.byte == null) return;
  const cid = evChip(ev);
  const u = qual(cid, ev.uart || ev.name || "UART");
  if (ev.dir === "drop") {
    const d = ui.uartDrops.get(u) || { n: 0, reason: "" };
    d.n += 1;
    d.reason = ev.reason || "?";
    ui.uartDrops.set(u, d);
    if (u === ui.uart) showUartDrops();
    return;
  }
  if (ev.dir === "tx") {
    let ln = ui.openLine.get(u);
    if (!ln) {
      ln = { uart: u, chip: cid, text: "", t: ev.t_ns };
      ui.openLine.set(u, ln);
      ui.lines.push(ln);
      if (ui.lines.length > MAX_LINES) ui.lines.splice(0, ui.lines.length - MAX_LINES);
    }
    const b = ev.byte;
    // LF, CR and CRLF each end a line (the LF of a CRLF only closes what the CR already closed)
    if (b === 10) { if (!ui.afterCR.get(u)) ui.openLine.delete(u); }
    else if (b === 13) ui.openLine.delete(u);
    else {
      ln.text += b >= 32 && b < 127 ? String.fromCharCode(b) : `\\x${b.toString(16).padStart(2, "0")}`;
      if (ln.text.length >= MAX_LINE_CHARS) ui.openLine.delete(u);        // binary stream: bound each line
    }
    ui.afterCR.set(u, b === 13);
    let m = ui.linkMon.get(u);
    if (!m) { m = { bytes: [] }; ui.linkMon.set(u, m); }
    m.bytes.push(b);
    if (m.bytes.length > MON_BYTES) m.bytes.splice(0, m.bytes.length - MON_BYTES);
    if (live && ui.board) ui.board.pulse(cid, ev.uart || ev.name, b);
    ui.uartDirty = true;
  }
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
  ui.uartMode = ui.isBoard ? "lines" : "raw";
  $("uartMode").value = ui.uartMode;
  ui.uartTs = !!prefs.uartTs;
  $("uartTs").checked = ui.uartTs;
  const modeUi = () => {
    $("uartAllWrap").hidden = ui.uartMode !== "lines";
    $("uartTsWrap").hidden = ui.uartMode !== "lines";
  };
  sel.addEventListener("change", () => {
    ui.uart = sel.value; ui.uartPicked = true; ui.uartDirty = true;
    showUartDrops(); refreshUart(true); schedule(); ui.uartFollow.end();
  });
  $("uartMode").addEventListener("change", () => {
    ui.uartMode = $("uartMode").value;
    modeUi();
    ui.uartDirty = true;
    $("uartOut")._html = null;
    if (ui.uartMode === "raw") refreshUart(true); else schedule();
    ui.uartFollow.end();
  });
  modeUi();
  $("uartAll").addEventListener("change", () => { ui.uartAll = $("uartAll").checked; ui.uartDirty = true; schedule(); });
  $("uartTs").addEventListener("change", () => {
    ui.uartTs = prefs.uartTs = $("uartTs").checked; savePrefs(); ui.uartDirty = true; schedule();
  });
}

const fmtTs = (ns) => (ns == null ? "".padStart(13) : `${(ns / 1e6).toFixed(3).padStart(10)} ms`);

// transcript lines (lines view) after the peer/"all" and text filters
function transcriptLines() {
  const want = ui.uartAll ? null : linkPeers(ui.uart);
  const m = $("uartFilter")._m;
  const who = (l) => (ui.isBoard || ui.uartAll || l.input ? (l.input ? `${l.uart} RX ← typed` : `${l.uart} TX`) : "");
  return ui.lines.filter((l) => (!want || want.has(l.uart)) && (!m || m.test(l.text)))
    .map((l) => ({ l, who: who(l) }));
}

function transcriptText() {
  return transcriptLines().map(({ l, who }) =>
    `${ui.uartTs ? `${fmtTs(l.t)}  ` : ""}${who ? `${who}: ` : ""}${l.text}`).join("\n") + "\n";
}

function renderTranscript() {
  if (ui.uartMode !== "lines") return;
  const out = $("uartOut");
  const m = $("uartFilter")._m;
  const lines = transcriptLines();
  const html = lines.slice(-2000).map(({ l, who }) =>
    `<span class="ln c-${esc(l.chip)}${l.input ? " in" : ""}">${ui.uartTs ? `<span class="ts">${esc(fmtTs(l.t))}</span>  ` : ""}` +
    `${who ? `<span class="who">${esc(who)}:</span> ` : ""}${hl(l.text, m)}</span>`).join("\n");
  const n = lines.length;
  out.dataset.empty = ui.lines.length ? "no lines match the filter" : `no TX lines${ui.uart ? ` on ${ui.uart}` : ""} yet`;
  if (out._html !== html) {
    const added = Math.max(0, n - (out._n || 0));
    out.innerHTML = html; out._html = html; out._n = n;
    ui.uartFollow.after(added);
  }
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
  out._html = null;
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
      row.innerHTML = `<span class="mon-dir"><span class="c-${esc(src.split(".")[0])}">${esc(src)}</span> → <span class="c-${esc(dst.split(".")[0])}">${esc(dst)}</span>${ln.auto ? ' <span class="auto-tag">auto</span>' : ""}</span><code class="mon-bytes"></code><span class="mon-n"></span>`;
      box.appendChild(row);
      ui.monRows.push({ src, stat, key, desc: ln.describe, bytesEl: row.querySelector(".mon-bytes"), nEl: row.querySelector(".mon-n") });
    }
  }
}

// ---------------------------------------------------------------- host serial bridges + real-time pacing
function renderHostSerial(st) {
  const h = st.host;
  const wrap = $("hostSerWrap");
  if (!h) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const eps = h.endpoints || [];
  const rt = h.realtime || {};
  $("hostSerCount").textContent = eps.length ? `${eps.length}` : "";
  const lag = rt.enabled ? `real time ×${rt.speed}${rt.lag_ms > 1 ? ` · lag ${Math.round(rt.lag_ms)} ms` : ""}${rt.rebases ? ` · behind ×${rt.rebases}` : ""}` : "";
  if ($("hostRt").textContent !== lag) $("hostRt").textContent = lag;
  $("hostRt").classList.toggle("warn", !!(rt.enabled && (rt.behind || rt.rebases)));
  if (document.activeElement !== $("rtOn")) $("rtOn").checked = !!rt.enabled;
  if (rt.enabled && document.activeElement !== $("rtSpeed")) $("rtSpeed").value = rt.speed;
  const html = eps.map((e) => `<tr><td>${esc(e.name)}</td><td>${esc(e.transport || "—")}</td><td class="mono" title="${esc(e.path || "")}">${esc(e.path || "—")}</td>`
    + `<td class="${e.connected ? "ok" : "dim"}">${e.connected ? "yes" : "no"}</td><td>${esc(e.line || "—")}</td>`
    + `<td>${e.bytes_in ?? 0}</td><td>${e.bytes_out ?? 0}</td><td${(e.dropped || e.dropped_out) ? ' class="warn"' : ""} title="framing ${e.dropped ?? 0} · host not connected ${e.dropped_out ?? 0}">${(e.dropped ?? 0) + (e.dropped_out ?? 0)}</td></tr>`).join("")
    || `<tr><td colspan="8" class="dim">none: --uart-pty UART0, --uart-tcp UART0=5555, --uart-port UART0=COM5, or a usb_serial_adapter part</td></tr>`;
  const body = $("hostSerBody");
  if (body._html !== html) { body.innerHTML = html; body._html = html; }
}

async function setRealtime() {
  const sp = parseFloat($("rtSpeed").value);
  try {
    await api("/api/realtime", { on: $("rtOn").checked, speed: Number.isFinite(sp) && sp > 0 ? sp : undefined });
  } catch (e) { toastErr(`real time: ${e.message}`); }
}

function renderLinkMonitor() {
  if (!ui.monRows || !ui.monRows.length) return;
  const links = (ui.state && ui.state.board && ui.state.board.links) || [];
  for (const m of ui.monRows) {
    const bytes = ui.linkMon.get(m.src)?.bytes || [];
    const html = bytes.map((b) => (b >= 32 && b < 127 ? esc(String.fromCharCode(b))
      : `<i>${b === 10 ? "↵" : b === 13 ? "⏎" : b.toString(16).padStart(2, "0")}</i>`)).join("");
    if (m.bytesEl._html !== html) { m.bytesEl.innerHTML = html; m.bytesEl._html = html; }
    const s = links.find((x) => x.link === m.desc);
    const n = s ? s[m.stat] : null;
    const rate = fmtRate(ui.board.linkRate(m.desc, m.key));
    const t = n != null ? `${n} B${rate ? ` · ${rate}` : ""}` : "";
    if (m.nEl.textContent !== t) m.nEl.textContent = t;
  }
}

// ---------------------------------------------------------------- discovery (pin report)
function showTab(disc) {
  ui.discOn = disc;
  $("tabPins").classList.toggle("on", !disc);
  $("tabDisc").classList.toggle("on", disc);
  $("tabPins").setAttribute("aria-selected", String(!disc));
  $("tabDisc").setAttribute("aria-selected", String(disc));
  $("pinTools").hidden = disc;
  $("discTools").hidden = !disc;
  $("pinTable").closest(".table-wrap").hidden = disc;
  $("discPane").hidden = !disc;
  if (disc) loadDiscovery();
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

function showPartTip(part, e) {
  const st = ui.partState[part.id] || part.state || {};
  const tip = $("tooltip");
  const params = Object.entries(part.params || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");
  const ports = Object.entries(part.ports || {}).map(([p, n]) => `<dt>${esc(p)}</dt><dd>${n ? esc(n) : "—"}${n ? ` <span class="lv lv-${netLevel(n) || "X"}">${netLevel(n) || "X"}</span>` : ""}</dd>`).join("");
  const sts = Object.entries(st).map(([k, v]) => `${k}=${v === null ? "—" : typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
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
      `<label class="pp">duty <input type="text" data-param="duty" value="${esc(pa.duty ?? 0.5)}" size="3" spellcheck="false"></label>`;
    case "pattern": return `<input type="text" class="pat" data-param="steps" value="${esc(fmtSteps(pa.steps))}" spellcheck="false" title="time:level steps, e.g. 0:L 1ms:H 3ms:L (Enter applies)">` +
      `<label class="toggle"><input type="checkbox" data-param="repeat" ${pa.repeat ? "checked" : ""}> repeat</label>`;
    default: return "";
  }
}

function panelStateText(p, st) {
  if (p.type === "counter") return `${counterValue(st)}`;
  if (p.type === "probe") return `${st.level ?? "X"} · ${stateText(p, st)}${st.freq_hz ? ` · ${fmtHz(st.freq_hz)}` : ""}`;
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
  const order = { src: 0, logic: 1, sink: 2 };
  const sorted = [...parts].sort((a, b) => order[partCategory(a.type)] - order[partCategory(b.type)]);
  $("partList").innerHTML = sorted.length ? sorted.map((p) => {
    const st = ui.partState[p.id] || p.state || {};
    const ports = portList(p).map((pp) => `<label class="pport" title="${esc(pp.dir)} port · net name or CHIP.PIN · empty: disconnect">` +
      `<span class="pn">${esc(pp.name)}</span><input type="text" list="netPinList" data-port="${esc(pp.name)}" value="${esc(p.ports[pp.name] || "")}" placeholder="—" spellcheck="false"></label>`).join("");
    return `<div class="part-row" data-part="${esc(p.id)}">
      <span data-ico>${partIcon(p, st)}</span>
      <span class="part-name">${esc(p.id)}</span><span class="part-type">${esc(p.type)}</span>
      <span class="part-st" data-st>${esc(panelStateText(p, st))}</span>
      <button class="btn small ghost part-del" title="remove part">×</button>
      <div class="part-ctl">${partControls(p)}</div>
      <div class="part-ports">${ports}</div>
    </div>`;
  }).join("") : `<div class="empty">no parts — add a switch, LED or gate and wire it to a net or pin</div>`;
  applyWiringFilters();
}

function renderPartStates() {
  if (!ui.isBoard) return;
  for (const row of $("partList").children) {
    const p = partById(row.dataset.part);
    if (!p) continue;
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
  try {
    if (key === "steps") v = parseSteps(v);
    else if (key === "duty") v = Number(v);
  } catch (e) { toastErr(e.message); return; }
  const r = await setPart(id, { [key]: v });
  if (r) reloadWiring(r.wiring);
}

function wirePartDialog() {
  initBoardDialog({ api, toast, toastErr });
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
    if (e.target.closest(".part-del")) {
      const r = await act("/api/part/remove", { id });
      if (r) { toast(`part ${id} removed`); reloadWiring(r.wiring); }
      return;
    }
    const sb = e.target.closest("[data-set]");
    if (sb) { setPart(id, JSON.parse(sb.dataset.set)); return; }
  });
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
    const groups = { src: "inputs", sink: "indicators", logic: "logic" };
    for (const [cat, label] of Object.entries(groups)) {
      const g = document.createElement("optgroup");
      g.label = label;
      for (const t of ui.partTypes) if (partCategory(t.type) === cat) g.appendChild(new Option(t.type, t.type));
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
  else if (typeof v === "number") inp = `<input type="text" id="${id}" data-k="${esc(k)}" data-t="num" value="${v}" size="6">`;
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
  const r = await act("/api/part", body);
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
    ui.uartDirty = true;
    schedule();
  } catch (e) {
    toastErr(`wiring: ${e.message}`);
  } finally {
    ui.wiringBusy = false;
    if (ui.wiringAgain) { ui.wiringAgain = false; reloadWiring(); }
  }
}

function netSummary(name, self) {
  const n = (ui.info.nets || []).find((x) => x.name === name);
  if (!n) return name;
  const others = n.pins.filter((q) => q.toUpperCase() !== String(self).toUpperCase());
  const lv = ui.state && ui.state.board ? (ui.state.board.nets || []).find((x) => x.name === name) : null;
  return `${name}${lv ? ` (${lv.level}${lv.conflict ? ", CONFLICT" : ""})` : ""}` +
    (others.length ? ` — with ${others.join(", ")}` : "");
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

async function removeNet(name) {
  const r = await act("/api/net/remove", { name });
  if (r) { toast(`net ${name} removed`); if (ui.selNet === name) selectNet(null); reloadWiring(r.wiring); }
}

async function renameNet(name) {
  const nn = window.prompt(`rename net ${name} to:`, name);
  if (!nn || nn === name) return;
  const r = await act("/api/net/rename", { name, new: nn });
  if (r) { if (ui.selNet === name) ui.selNet = r.net.name; reloadWiring(r.wiring); }
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
  $("btnExport").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/board/yaml");
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      const text = await r.text();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([text], { type: "text/yaml" }));
      a.download = `${(ui.info.board && ui.info.board.name) || "board"}.yaml`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (e) { toastErr(`export: ${e.message}`); }
  });
  $("netList").addEventListener("click", (e) => {
    const row = e.target.closest(".net-row");
    if (!row) return;
    const name = row.dataset.net;
    if (e.target.closest(".net-del")) { removeNet(name); return; }
    if (e.target.closest(".net-ren")) { renameNet(name); return; }
    selectNet(ui.selNet === name ? null : name);
  });
  $("netList").addEventListener("dblclick", (e) => {
    const row = e.target.closest(".net-row");
    if (row) renameNet(row.dataset.net);
  });
  $("netList").addEventListener("mouseover", (e) => {
    const row = e.target.closest(".net-row");
    if (row && ui.board.hlNet !== row.dataset.net) ui.board.highlightNet(row.dataset.net);
  });
  $("netList").addEventListener("mouseleave", () => ui.board.highlightNet(null));
  $("linkList").addEventListener("click", async (e) => {
    const b = e.target.closest(".link-del");
    if (!b) return;
    const r = await act("/api/link/remove", { from: b.dataset.from, to: b.dataset.to });
    if (r) { toast(`link ${r.removed} removed`); reloadWiring(r.wiring); }
  });
  renderWiringPanel();
  // the wiring column scrolls inside (about) the board canvas' natural height instead of stretching the row
  const pkgPanel = document.querySelector(".package-panel"), wp = document.querySelector(".wiring-panel");
  if (window.ResizeObserver && pkgPanel && wp) {
    let raf = 0;
    const fit = () => {
      raf = 0;
      const nat = [...pkgPanel.children].reduce((a, c) => a + c.offsetHeight, 2);
      const h = `${Math.round(Math.max(420, nat, window.innerHeight * 0.6))}px`;
      if (wp.style.height !== h) { wp.style.height = h; wp.style.maxHeight = "none"; }
    };
    new ResizeObserver(() => { if (!raf) raf = requestAnimationFrame(fit); }).observe($("pkg"));
  }
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
      <span class="net-tools"><button class="btn small ghost net-ren" title="rename">✎</button><button class="btn small ghost net-del" title="remove net">×</button></span>
      <span class="net-pins">${n.pins.map((q) => `<span class="c-${esc(q.split(".")[0])}">${esc(q)}</span>`).join(" ")}${(n.ports || []).map((q) => ` <span class="net-port">${esc(q)}</span>`).join("")}${n.pull ? ` <i>pull ${esc(n.pull)}</i>` : ""}</span>
    </div>`;
  }).join("") : `<div class="empty">no nets — press W (wire mode) and click two pins</div>`;
  const links = ui.info.links || [];
  $("linkCount").textContent = links.length ? `${links.length}` : "";
  $("linkList").innerHTML = links.length ? links.map((ln) => `<div class="link-row${ln.auto ? " auto" : ""}">
      <span class="c-${esc(ln.from.split(".")[0])}">${esc(ln.from)}</span> ${ln.bidir === false ? "→" : "↔"}
      <span class="c-${esc(ln.to.split(".")[0])}">${esc(ln.to)}</span>
      ${ln.auto ? `<span class="auto-tag" title="created from net ${esc(ln.net)}: TX and RX pins on one net">auto · ${esc(ln.net)}</span>`
        : `<button class="btn small ghost link-del" data-from="${esc(ln.from)}" data-to="${esc(ln.to)}" title="remove link">×</button>`}
    </div>`).join("") : `<div class="empty">no UART links</div>`;
  if (ui.state && ui.state.board) renderWiringLevels(ui.state.board);
  applyWiringFilters();
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
    const hide = !!m && !m.test(row.textContent + " " + (row.dataset.net || row.dataset.part || ""));
    row.classList.toggle("hidden", hide);
    if (!hide) shown++;
  }
  if (countEl) countEl.textContent = total ? (m ? `${shown}/${total}` : `${total}`) : "";
}

function applyWiringFilters() {
  if (!ui.isBoard) return;
  filterList($("netList"), ".net-row", $("netFilter")._m, $("netCount"), (ui.info.nets || []).length);
  filterList($("linkList"), ".link-row", $("linkFilter")._m, $("linkCount"), (ui.info.links || []).length);
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
  const rows = [...$("linkList").querySelectorAll(".link-row")];
  const links = ui.info.links || [];
  return [["from", "to", "both_ways", "auto", "net"],
    ...links.filter((_, i) => rows[i] && !rows[i].classList.contains("hidden"))
      .map((ln) => [ln.from, ln.to, ln.bidir === false ? "no" : "yes", ln.auto ? "yes" : "", ln.net || ""])];
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
  return [["chip", "num", "pin", "owner", "dir", "level", "freq_hz", "duty", "edges", "pull", "external", "net", "datasheet_name"],
    ...ui.pinVisible.map((r) => [ui.sel, r.num, meta.get(r.num)?.short ?? "", r.owner, r.direction, r.level || "X",
      r.wave?.freq_hz ?? "", r.wave?.duty ?? "", r.wave?.edges ?? "", r.pull ?? "", r.external ?? "", r.net ?? "", r.name])];
}

function filterDiscovery() {
  const m = $("pinFilter")._m;
  for (const tr of $("discTable").tBodies[0].rows) tr.classList.toggle("hidden", !!m && !m.test(tr.textContent));
}

function initDataTools() {
  // UART
  bindFilter($("uartFilter"), () => { $("uartOut")._html = null; $("uartOut")._rawHtml = null; $("uartOut")._rawShown = null;
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
  $("linkMonWrap").addEventListener("toggle", () => { prefs.linkMon = $("linkMonWrap").open; savePrefs(); });
  $("hostSerWrap").open = prefs.hostSer ?? false;
  $("hostSerWrap").addEventListener("toggle", () => { prefs.hostSer = $("hostSerWrap").open; savePrefs(); });
  $("rtOn").addEventListener("change", setRealtime);
  $("rtSpeed").addEventListener("change", setRealtime);
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
  // wiring panes
  for (const id of ["netFilter", "linkFilter", "partFilter"]) bindFilter($(id), applyWiringFilters);
  $("netCopy").addEventListener("click", () => copyText(toTsv(netRows()), "row"));
  $("netCsv").addEventListener("click", () => saveFile(`${fileBase()}_nets_${stamp()}.csv`, toCsv(netRows()), "text/csv"));
  $("linkCopy").addEventListener("click", () => copyText(toTsv(linkRows()), "row"));
  $("linkCsv").addEventListener("click", () => saveFile(`${fileBase()}_links_${stamp()}.csv`, toCsv(linkRows()), "text/csv"));
  $("partCopy").addEventListener("click", () => copyText(toTsv(partRows()), "row"));
  $("partCsv").addEventListener("click", () => saveFile(`${fileBase()}_parts_${stamp()}.csv`, toCsv(partRows()), "text/csv"));
  // help
  const help = (on) => { $("helpPop").hidden = !on; $("btnHelp").setAttribute("aria-expanded", String(on)); };
  $("btnHelp").addEventListener("click", (e) => { e.stopPropagation(); help($("helpPop").hidden); });
  document.addEventListener("click", (e) => { if (!$("helpPop").hidden && !e.target.closest("#helpPop")) help(false); });
  ui.help = help;
  $("events").dataset.empty = "no events yet";
  $("uartOut").dataset.empty = "no output yet";
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
function wireControls() {
  $("tabPins").addEventListener("click", () => showTab(false));
  $("tabDisc").addEventListener("click", () => showTab(true));
  $("discRefresh").addEventListener("click", loadDiscovery);
  $("discStatic").addEventListener("change", loadDiscovery);
  $("btnRun").addEventListener("click", () => act("/api/run", {}));
  $("btnPause").addEventListener("click", () => act("/api/pause"));
  $("btnStep").addEventListener("click", () => act("/api/step", { n: 1 }));
  if (ui.isBoard) $("btnStep").title = "Execute one instruction on the first board chip (the others do not move)";
  $("btnReset").addEventListener("click", async () => {
    await act("/api/reset");
    ui.baseline = null;
  });
  const runFor = () => {
    const ms = parseDurationMs($("runFor").value);
    if (ms == null || ms <= 0) { toastErr("duration: e.g. 500us, 10ms, 1.5s"); return; }
    act("/api/run", { ms });
  };
  $("btnRunFor").addEventListener("click", runFor);
  $("runFor").addEventListener("keydown", (e) => { if (e.key === "Enter") runFor(); });
  $("btnWire").addEventListener("click", () => setWireMode(!ui.wireMode));

  $("uartForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!ui.uart) { toastErr("no UART"); return; }
    const eol = $("uartEol").value;
    const typed = $("uartText").value;
    const r = await act("/api/uart", { uart: ui.uart, text: typed + eol });
    if (r) {
      $("uartText").value = "";
      ui.lines.push({ uart: ui.uart, chip: ui.isBoard ? ui.uart.split(".")[0] : evChip({}), text: typed, input: true });
      ui.uartDirty = true;
      schedule();
    }
  });
  $("uartClear").addEventListener("click", () => {
    ui.uartSkip = ui.uartSkip || {};
    ui.uartSkip[ui.uart] = ui.uartLen[ui.uart] || 0;
    ui.lines.length = 0;
    ui.openLine.clear(); ui.afterCR.clear();
    $("uartOut").textContent = "";
    $("uartOut")._html = "";
    $("uartOut")._rawShown = null;
    $("uartOut")._n = 0;
    ui.uartDrops.delete(ui.uart);
    showUartDrops();
  });
  $("evClear").addEventListener("click", () => { $("events").innerHTML = ""; ui.evDropped = 0; renderEvCount(); ui.evFollow.end(); });

  for (const id of ["changedOnly", "hideDisabled"]) $(id).addEventListener("input", () => renderPinTable());
  for (const th of $("pinTable").tHead.rows[0].cells) {
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      ui.sort = { k, desc: ui.sort.k === k ? !ui.sort.desc : false };
      renderPinTable();
    });
  }
  $("pinTable").tBodies[0].addEventListener("click", (e) => {
    const td = e.target.closest("td.ext");
    const tr = e.target.closest("tr.clickable");
    if (!td || !tr) return;
    const chip = ui.chipById.get(ui.sel);
    const pin = ui.metaByChip.get(ui.sel)?.get(+tr.dataset.num);
    if (pin) clickPin(chip, pin);
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(); });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") {                       // over the board: board.js focuses its pin finder
      if (ui.hoverPane && ui.hoverPane.id !== "paneBoard") { e.preventDefault(); focusFilter(); }
      return;
    }
    if (e.key === "?") { e.preventDefault(); ui.help($("helpPop").hidden); return; }
    if (e.key === "End") {
      const p = ui.hoverPane;
      if (p && p.id === "paneEvents") { e.preventDefault(); ui.evFollow.end(); return; }
      if (!p || p.id === "paneUart") { e.preventDefault(); ui.uartFollow.end(); return; }
    }
    if (e.key === "Escape" && !$("helpPop").hidden) { ui.help(false); return; }
    if (e.key === " ") { e.preventDefault(); act(ui.state?.running ? "/api/pause" : "/api/run", {}); }
    else if (e.key === "s") act("/api/step", { n: 1 });
    else if (e.key === "r") { act("/api/reset"); ui.baseline = null; }
    else if (e.key === "f") ui.board.fit();
    else if (e.key === "b" || e.key === "B") $("btnBoard").click();
    else if (ui.isBoard && (e.key === "w" || e.key === "W")) setWireMode(!ui.wireMode);
    else if (e.key === "Escape") {
      if (ui.pickTarget) pickDone(null);
      else if (!$("partDlg").hidden) closePartDialog();
      else if (boardDialogOpen()) closeBoardDialog();
      else if (ui.pendingPin) { ui.pendingPin = null; ui.board.setPending(null); }
      else if (ui.wireMode) setWireMode(false);
      else if (ui.selNet) selectNet(null);
    } else if ((e.key === "Delete" || e.key === "Backspace") && ui.selNet) { e.preventDefault(); removeNet(ui.selNet); }
    else if (ui.isBoard && /^[1-9]$/.test(e.key) && ui.chips[+e.key - 1]) selectChip(ui.chips[+e.key - 1].id);
  });
}

initTheme();
boot().catch((e) => {
  console.error(e); toastErr(`failed to load: ${e.message}`);
  $("conn").className = "conn bad"; $("conn").textContent = "offline";
  $("status").textContent = "ERROR"; $("status").className = "status-badge fault";
});
