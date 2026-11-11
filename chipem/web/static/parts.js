// Board parts (chipem/parts) as small schematic symbols on the board canvas: inputs (switch, button, const/vcc/gnd,
// pulse, pattern), indicators (led, probe, counter) and logic (gates with distinctive shapes, mux2, dff, latch).
// A part is drawn in its own unit coordinates (body top-left at 0,0; input ports on the left, outputs on the right,
// each with a short lead whose outer end is the wire anchor) and placed with translate + scale by the BoardView.
// Generic: knows part *types* (the standard library of docs/CONTRACTS.md), never chip vendors.
import { el } from "./package.js";
import { fmtFreq } from "./fmt.js";

export const PORT_P = 26;         // port pitch
export const LEAD = 16;           // lead length outside the body
const LABEL_H = 20;               // id label above the body
const STATE_H = 22;               // state line below the body

export const SOURCE_TYPES = new Set(["switch", "button", "const", "vcc", "gnd", "pulse", "pattern"]);
export const SINK_TYPES = new Set(["led", "probe", "counter"]);
export const GATE_TYPES = new Set(["and", "nand", "or", "nor", "xor", "xnor", "not", "buf"]);

// peripheral-like parts (chipem/parts: sensors, memories, codecs, adapters) and pure wiring (connectors, cables)
export const DEVICE_TYPES = new Set(["quadrature_encoder", "i2c_eeprom", "i2c_register_device", "i2s_codec", "camera",
  "usb_serial_adapter", "rs232_transceiver"]);
export const CONNECTOR_TYPES = new Set(["connector", "db9", "rj50", "cable"]);
// devices that only drive signals into the board sit with the sources (left of the chips)
const SOURCE_DEVICES = new Set(["quadrature_encoder", "camera"]);

// layout column: "src" (left of the chips), "logic" (right), "sink" (indicators, far right)
export function partCategory(type) {
  if (SOURCE_TYPES.has(type) || SOURCE_DEVICES.has(type)) return "src";
  if (SINK_TYPES.has(type)) return "sink";
  return "logic";
}

// add-part dialog group / parts list order: inputs, indicators, logic, devices, connectors
export const PART_GROUPS = ["inputs", "indicators", "logic", "devices", "connectors"];
export function partGroup(type) {
  if (SOURCE_TYPES.has(type)) return "inputs";
  if (SINK_TYPES.has(type)) return "indicators";
  if (DEVICE_TYPES.has(type)) return "devices";
  if (CONNECTOR_TYPES.has(type)) return "connectors";
  if (GATE_TYPES.has(type) || ["mux2", "dff", "latch"].includes(type)) return "logic";
  return "devices";                       // unknown (newer) part types are peripherals more often than gates
}

// LED colour (params.color): a named colour maps to its theme token (--led-red, ... in style.css, one value per
// theme); anything else (a CSS colour from the board file) is used as given. The value is only used as a CSS
// custom property (--led), never as an SVG presentation attribute, so var() works.
export const LED_COLORS = ["red", "green", "blue", "yellow", "orange", "white", "amber", "purple", "pink", "cyan"];
export function ledColor(c) {
  const k = String(c || "red").toLowerCase();
  return LED_COLORS.includes(k) ? `var(--led-${k})` : String(c);
}

// ports in drawing order: [{name, dir, side: "L"|"R"}]
export function portList(part) {
  const dirs = part.dirs || {};
  const names = Object.keys(part.ports || dirs);
  const order = { d: 0, a: 0, b: 1, sel: 2, clk: 1, en: 1, rst: 2, q: 0, qn: 1 };
  const out = names.map((n) => {
    const dir = dirs[n] || (n === "out" || n === "q" || n === "qn" ? "out" : "in");
    return { name: n, dir, side: dir === "in" ? "L" : "R" };
  });
  // bidirectional ports only (I2C devices, cables): the first half on the left, the rest on the right
  const io = out.filter((p) => p.dir === "inout");
  if (io.length > 1 && !out.some((p) => p.dir === "in")) {
    const sorted = io.slice().sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    sorted.slice(0, Math.ceil(sorted.length / 2)).forEach((p) => { p.side = "L"; });
  }
  const key = (p) => (order[p.name] ?? (/^in(\d+)$/.test(p.name) ? +p.name.slice(2) : 50));
  out.sort((a, b) => key(a) - key(b) || a.name.localeCompare(b.name, undefined, { numeric: true }));
  return out;
}

// body size (units) of a part
const PORT_CH = 6.2;              // port label (10 px mono) advance
const TYPE_CH = 6.9;              // type name (11 px 600 mono) advance
const TYPE_HEAD = 16;             // header row for the type name of a crowded box
const STATE_CH = 6.7;             // state line (11 px mono) advance
const SYMBOL_TYPES = new Set(["led", "const", "vcc", "gnd", "switch", "button", "pulse", "pattern", "probe", "counter",
  "not", "buf", "and", "nand", "or", "nor", "xor", "xnor", "mux2", "dff", "latch"]);
const PORT_TEXT = { qn: "Q̅", q: "Q", d: "D", en: "EN", rst: "R", sel: "S", a: "0", b: "1" };
const portText = (name) => PORT_TEXT[name] ?? name;

export function partGeom(part) {
  const ports = portList(part);
  const nL = ports.filter((p) => p.side === "L").length, nR = ports.filter((p) => p.side === "R").length;
  const t = part.type;
  let bw = 56, bh = Math.max(nL, nR, 1) * PORT_P + 10, head = 0;
  if (t === "led") { bw = 34; bh = 34; }
  else if (["const", "vcc", "gnd"].includes(t)) { bw = 40; bh = 40; }
  else if (t === "switch") { bw = 60; bh = 36; }
  else if (t === "button") { bw = 50; bh = 38; }
  else if (t === "pulse" || t === "pattern") { bw = 64; bh = 40; }
  else if (t === "probe") { bw = 96; bh = 40; }
  else if (t === "counter") { bw = 70; bh = 36; }
  else if (t === "not" || t === "buf") { bw = 46; bh = 36; }
  else if (GATE_TYPES.has(t)) { bw = 58; bh = Math.max(36, nL * PORT_P + 6); }
  else if (t === "mux2") { bw = 46; bh = Math.max(bh, 3 * PORT_P + 8); }
  else if (t === "dff" || t === "latch") { bw = 58; bh = Math.max(bh, 2 * PORT_P + 12); }
  else if (!SYMBOL_TYPES.has(t)) {
    // generic box: port labels inside along both edges, the type name between them (or in a header row when the
    // columns leave no room)
    const lw = (side) => Math.max(0, ...ports.filter((p) => p.side === side).map((p) => portText(p.name).length));
    const cl = lw("L") * PORT_CH, cr = lw("R") * PORT_CH, tn = String(t).length * TYPE_CH;
    const inline = cl + cr + tn + 30;
    bw = Math.max(56, Math.min(inline, Math.max(cl + cr + 24, tn + 16)));
    if (inline > bw) { bh += TYPE_HEAD; head = TYPE_HEAD; }     // the type name gets its own row above the ports
  }
  bh = Math.max(bh, 34);
  const idW = String(part.id).length * 8.4 + 8;
  const w = Math.max(bw + 2 * LEAD, idW, 96);
  return { bw, bh, ports, w, h: LABEL_H + bh + STATE_H, nL, nR, head };
}

const fmtHzShort = (hz) => fmtFreq(hz, true);

// one-line state text under the symbol
export function stateText(part, st) {
  st = st || {};
  const p = part.params || {};
  switch (part.type) {
    case "switch": return `out ${st.level ?? p.level ?? "?"}`;
    case "button": return st.pressed ? `pressed · ${st.level ?? p.pressed ?? ""}` : `released · ${st.level ?? p.released ?? ""}`;
    case "const": case "vcc": case "gnd": return `${st.level ?? p.level ?? (part.type === "vcc" ? "H" : part.type === "gnd" ? "L" : "?")}`;
    case "pulse": return `${p.period ?? "?"}${p.duty != null && +p.duty !== 0.5 ? ` · ${Math.round(+p.duty * 100)}%` : ""}`;
    case "pattern": return `${(p.steps || []).length} steps${p.repeat ? " ↻" : ""}${st.step != null ? ` · #${st.step}` : ""}`;
    case "led": return st.on ? "on" : "off";
    case "probe": {
      return `↑${st.rises ?? 0} ↓${st.falls ?? 0}`;
    }
    case "counter": return `${p.edge || "rise"} edges`;
    case "script": {                      // chipem/parts/script.py: user state, or the fault that stopped it
      if (st.fault) return `FAULT ${st.fault}`;
      const outs = Object.entries(st).filter(([k, v]) => k !== "params" && (v === null || typeof v !== "object"))
        .map(([k, v]) => `${k}=${v ?? "—"}`);
      return outs.slice(0, 3).join(" ");
    }
    default: {
      const outs = Object.entries(st).filter(([k, v]) => k !== "changes" && k !== "action" && (v === null || typeof v !== "object"))
        .map(([k, v]) => `${k}=${v ?? "—"}`);
      return outs.slice(0, 3).join(" ");
    }
  }
}

export function counterValue(st) {
  st = st || {};
  for (const k of ["count", "value", "edges", "n"]) if (st[k] != null) return st[k];
  return 0;
}

export class PartView {
  // parent: SVG <g>; part: /api/board parts entry; handlers: {onAction(part, values), onPortClick(part, port, e),
  // onPortHover(part, port, e), onPortLeave(), onBodyHover(part, e), onBodyLeave(), onBodyClick(part, e)}
  constructor(parent, part, handlers = {}) {
    this.part = part;
    this.h = handlers;
    this.geo = partGeom(part);
    this.root = el("g", { class: `part part-${part.type}`, "data-part": part.id }, parent);
    this.portEls = new Map();      // port -> {g, lead, x, y, side}
    this.build();
  }

  // local anchor (outer lead end) of a port, in part units
  portAnchor(name) {
    const p = this.portEls.get(name);
    return p ? { x: p.x, y: p.y, side: p.side } : null;
  }

  build() {
    const { bw, bh, ports, w } = this.geo;
    const part = this.part;
    const t = part.type;
    const ox = (w - bw) / 2;       // body x inside the cell
    this.ox = ox;
    const g = el("g", { transform: `translate(${ox} ${LABEL_H})` }, this.root);
    this.g = g;
    const label = el("text", { x: bw / 2, y: -7, class: "part-id", "text-anchor": "middle" }, g);
    label.textContent = part.id;
    this.stateEl = el("text", { x: bw / 2, y: bh + 15, class: "part-state", "text-anchor": "middle" }, g);

    // port leads (drawn under the body)
    const leads = el("g", { class: "part-leads" }, g);
    const body = el("g", { class: "part-body" }, g);
    this.body = body;
    const L = ports.filter((p) => p.side === "L"), R = ports.filter((p) => p.side === "R");
    const hd = this.geo.head || 0;
    const ys = (n) => Array.from({ length: n }, (_, i) => hd + (bh - hd) / 2 + (i - (n - 1) / 2) * PORT_P);
    const placeSide = (list, side) => {
      const yy = ys(list.length);
      list.forEach((p, i) => {
        let y = yy[i];
        const inner = this.innerX(side, y);
        const x0 = side === "L" ? -LEAD : bw + LEAD;
        const pg = el("g", { class: "port lv-none", "data-port": p.name }, leads);
        el("line", { x1: x0, y1: y, x2: inner, y2: y, class: "lead-hit" }, pg);
        const lead = el("line", { x1: x0, y1: y, x2: inner, y2: y, class: "lead" }, pg);
        el("circle", { cx: x0, cy: y, r: 2.6, class: "lead-end" }, pg);
        pg.addEventListener("click", (e) => { e.stopPropagation(); this.h.onPortClick && this.h.onPortClick(part, p.name, e); });
        pg.addEventListener("mousemove", (e) => this.h.onPortHover && this.h.onPortHover(part, p.name, e));
        pg.addEventListener("mouseleave", () => this.h.onPortLeave && this.h.onPortLeave());
        this.portEls.set(p.name, { g: pg, lead, x: ox + x0, y: LABEL_H + y, side, ly: y, lv: "none" });
      });
    };
    placeSide(L, "L");
    placeSide(R, "R");

    const draw = SYMBOLS[t] || (GATE_TYPES.has(t) ? gateSymbol : boxSymbol);
    draw.call(this, body, bw, bh);
    body.addEventListener("mousemove", (e) => this.h.onBodyHover && this.h.onBodyHover(part, e));
    body.addEventListener("mouseleave", () => this.h.onBodyLeave && this.h.onBodyLeave());
    if (!this.interactive) {
      body.addEventListener("click", (e) => { e.stopPropagation(); this.h.onBodyClick && this.h.onBodyClick(part, e); });
    }
    this.update(part.state || {}, {});
  }

  // x where an input/output lead meets the body outline (gates have curved backs)
  innerX(side, y) {
    const t = this.part.type;
    const { bw, bh } = this.geo;
    if (side === "R") {
      if (["nand", "nor", "xnor", "not"].includes(t)) return bw;        // bubble ends at bw
      if (GATE_TYPES.has(t)) return bw - 8;
      return bw;
    }
    if (t === "or" || t === "nor") return backCurveX(y, bh, 0);
    if (t === "xor" || t === "xnor") return backCurveX(y, bh, 0);
    return 0;
  }

  // st: part state; netLv: port -> "H"|"L"|"X"|null (level of the port's net, null = unconnected)
  update(st, netLv) {
    this.st = st || {};
    for (const [name, p] of this.portEls) {
      const lv = netLv[name] ?? "none";
      if (lv !== p.lv) { p.g.setAttribute("class", `port lv-${lv}`); p.lv = lv; }
    }
    if (this.refresh) this.refresh(this.st, netLv);
    const txt = stateText(this.part, this.st);
    if (this._stateFull !== txt) {
      this._stateFull = txt;
      const max = Math.max(6, Math.floor(this.geo.w / STATE_CH));
      const short = txt.length > max ? `${txt.slice(0, max - 1)}…` : txt;
      this.stateEl.textContent = short;
      if (short !== txt) el("title", {}, this.stateEl).textContent = txt;
    }
  }

  setClass(cls, on) { this.root.classList.toggle(cls, !!on); }
  setPortClass(port, cls, on) { const p = this.portEls.get(port); if (p) p.g.classList.toggle(cls, !!on); }
}

// ---------------------------------------------------------------- symbols (this = PartView)
function backCurveX(y, h, x0) {        // quadratic back curve of OR/XOR: x0 at the ends, x0 + 9 in the middle
  const tt = Math.min(1, Math.max(0, y / h));
  return x0 + 4 * 9 * tt * (1 - tt);
}

function bubble(body, x, y) { el("circle", { cx: x + 4, cy: y, r: 4, class: "gate-bubble" }, body); }

function gateSymbol(body, bw, bh) {
  const t = this.part.type;
  const inv = ["nand", "nor", "xnor", "not"].includes(t);
  const sw = inv ? bw - 8 : bw - 8;            // shape right end (bubble / output lead after it)
  const h = bh;
  let d;
  if (t === "and" || t === "nand") {
    const r = h / 2, a = Math.max(8, sw - r);
    d = `M0,0 H${a} A${r},${r} 0 0 1 ${a},${h} H0 Z`;
  } else if (t === "or" || t === "nor" || t === "xor" || t === "xnor") {
    const x0 = t.startsWith("x") ? 7 : 0;
    d = `M${x0},0 C${x0 + (sw - x0) * 0.55},0 ${sw - 6},${h * 0.18} ${sw},${h / 2} ` +
        `C${sw - 6},${h * 0.82} ${x0 + (sw - x0) * 0.55},${h} ${x0},${h} Q${x0 + 18},${h / 2} ${x0},0 Z`;
    if (x0) el("path", { d: `M0,${h} Q18,${h / 2} 0,0`, class: "gate-xor" }, body);
  } else {                                     // not / buf: triangle
    d = `M0,${h / 2 - 16} L${sw},${h / 2} L0,${h / 2 + 16} Z`;
    el("rect", { x: 0, y: 0, width: sw, height: h, class: "gate-hitbox" }, body);
  }
  this.shape = el("path", { d, class: "gate" }, body);
  if (inv) bubble(body, sw, h / 2);
  else el("line", { x1: sw, y1: h / 2, x2: bw, y2: h / 2, class: "gate-stub" }, body);
  const lab = el("text", { x: (t === "not" || t === "buf") ? sw * 0.33 : sw * 0.42, y: h / 2 + 4, class: "gate-name",
                            "text-anchor": "middle" }, body);
  lab.textContent = { and: "&", nand: "&", or: "≥1", nor: "≥1", xor: "=1", xnor: "=1", not: "1", buf: "1" }[t] || "";
  this.refresh = (st, lv) => {
    const out = lv.out ?? st.out ?? st.level;
    this.shape.setAttribute("class", `gate out-${out || "none"}`);
  };
}

function boxSymbol(body, bw, bh) {
  this.shape = el("rect", { x: 0, y: 0, width: bw, height: bh, rx: 4, class: "part-box" }, body);
  const t = this.part.type;
  const hd = this.geo.head || 0;
  // the type name: in the header row of a crowded box, else centred between the label columns where no port label is
  let ty = hd ? hd - 4 : bh / 2 + 4;
  if (!hd) {
    const ly = [...this.portEls.values()].map((p) => p.ly);
    const free = (y) => ly.every((v) => Math.abs(v - (y - 4)) >= 11);
    if (!free(ty)) {
      for (let d = 6; d < bh / 2; d += 3) {
        if (free(bh / 2 + 4 + d) && bh / 2 + 4 + d < bh - 3) { ty = bh / 2 + 4 + d; break; }
        if (free(bh / 2 + 4 - d) && bh / 2 + 4 - d > 11) { ty = bh / 2 + 4 - d; break; }
      }
    }
  }
  if (hd) el("line", { x1: 0, y1: hd, x2: bw, y2: hd, class: "part-sep" }, body);
  const title = el("text", { x: bw / 2, y: ty, class: "gate-name", "text-anchor": "middle" }, body);
  title.textContent = t === "dff" ? "" : t === "latch" ? "" : t;
  // port labels inside the box
  for (const [name, p] of this.portEls) {
    const ly = p.ly;
    const left = p.side === "L";
    if (name === "clk") {
      el("path", { d: `M0,${ly - 6} L9,${ly} L0,${ly + 6}`, class: "clk-tri" }, body);
      continue;
    }
    const txt = el("text", { x: left ? 6 : bw - 6, y: ly + 4, class: "port-label", "text-anchor": left ? "start" : "end" }, body);
    txt.textContent = portText(name);
  }
  if (t === "dff") title.textContent = "FF";
  else if (t === "latch") title.textContent = "L";
}

const SYMBOLS = {
  mux2(body, bw, bh) {
    this.shape = el("path", { d: `M0,0 L${bw},12 L${bw},${bh - 12} L0,${bh} Z`, class: "part-box" }, body);
    for (const [name, p] of this.portEls) {
      if (p.side !== "L") continue;
      const txt = el("text", { x: 6, y: p.ly + 4, class: "port-label", "text-anchor": "start" }, body);
      txt.textContent = { a: "0", b: "1", sel: "S" }[name] ?? name;
    }
    const m = el("text", { x: bw / 2 + 6, y: bh / 2 + 4, class: "gate-name", "text-anchor": "middle" }, body);
    m.textContent = "MUX";
  },

  led(body, bw, bh) {
    const cx = bw / 2, cy = bh / 2;
    this.halo = el("circle", { cx, cy, r: 17, class: "led-halo" }, body);
    this.shape = el("circle", { cx, cy, r: 13, class: "led" }, body);
    el("path", { d: `M${cx - 5},${cy - 6} L${cx + 5},${cy} L${cx - 5},${cy + 6} Z M${cx + 5},${cy - 6} V${cy + 6}`,
                 class: "led-glyph" }, body);
    this.rays = el("path", { d: `M${cx + 9},${cy - 13} l6,-6 m-2,0 h2 v2 M${cx + 14},${cy - 9} l6,-6 m-2,0 h2 v2`,
                             class: "led-rays" }, body);
    const col = ledColor(this.part.params && this.part.params.color);
    this.root.style.setProperty("--led", col);
    this.refresh = (st, lv) => {
      this.root.classList.toggle("on", !!st.on);
      this.root.classList.toggle("x", !st.on && (st.level || lv.in) === "X");
    };
  },

  switch(body, bw, bh) {
    this.interactive = true;
    el("rect", { x: 0, y: 0, width: bw, height: bh, rx: 6, class: "part-box" }, body);
    this.track = el("rect", { x: 7, y: bh / 2 - 8, width: 30, height: 16, rx: 8, class: "sw-track" }, body);
    this.knob = el("circle", { cx: 15, cy: bh / 2, r: 6, class: "sw-knob" }, body);
    this.big = el("text", { x: bw - 11, y: bh / 2 + 6, class: "sw-level", "text-anchor": "middle" }, body);
    body.classList.add("clickable");
    const title = el("title", {}, body);
    title.textContent = "click: toggle H / L";
    body.addEventListener("click", (e) => {
      e.stopPropagation();
      const cur = (this.st && this.st.level) || "L";
      this.h.onAction && this.h.onAction(this.part, { level: cur === "H" ? "L" : "H" });
    });
    this.refresh = (st) => {
      const lv = st.level || "X";
      if (this.big.textContent !== lv) this.big.textContent = lv;
      this.knob.setAttribute("cx", lv === "H" ? 29 : lv === "L" ? 15 : 22);
      this.track.setAttribute("class", `sw-track lv-${lv}`);
      this.big.setAttribute("class", `sw-level lv-${lv}`);
    };
  },

  button(body, bw, bh) {
    this.interactive = true;
    el("rect", { x: 0, y: 0, width: bw, height: bh, rx: 6, class: "part-box" }, body);
    el("circle", { cx: 18, cy: bh / 2, r: 12, class: "btn-ring" }, body);
    this.cap = el("circle", { cx: 18, cy: bh / 2, r: 9, class: "btn-cap" }, body);
    this.big = el("text", { x: bw - 9, y: bh / 2 + 5, class: "sw-level small", "text-anchor": "middle" }, body);
    body.classList.add("clickable");
    const title = el("title", {}, body);
    title.textContent = "hold the mouse button: pressed";
    let down = false;
    const release = () => {
      if (!down) return;
      down = false;
      this.h.onAction && this.h.onAction(this.part, { pressed: false });
    };
    body.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.stopPropagation();          // no pan
      down = true;
      this.h.onAction && this.h.onAction(this.part, { pressed: true });
      window.addEventListener("pointerup", release, { once: true });
    });
    body.addEventListener("click", (e) => e.stopPropagation());
    this.refresh = (st) => {
      const pressed = !!st.pressed;
      this.cap.setAttribute("class", `btn-cap${pressed ? " pressed" : ""}`);
      this.cap.setAttribute("r", pressed ? 7.5 : 9);
      const lv = st.level || (pressed ? this.part.params?.pressed : this.part.params?.released) || "";
      if (this.big.textContent !== lv) this.big.textContent = lv;
      this.big.setAttribute("class", `sw-level small lv-${lv || "X"}`);
    };
  },

  const(body, bw, bh) { railSymbol.call(this, body, bw, bh); },
  vcc(body, bw, bh) { railSymbol.call(this, body, bw, bh); },
  gnd(body, bw, bh) { railSymbol.call(this, body, bw, bh); },

  pulse(body, bw, bh) {
    el("rect", { x: 0, y: 0, width: bw, height: bh, rx: 4, class: "part-box" }, body);
    const y0 = bh / 2 + 9, y1 = bh / 2 - 9;
    let d = `M8,${y0}`, x = 8;
    for (let i = 0; i < 3; i++) { d += ` H${x + 8} V${y1} H${x + 16} V${y0}`; x += 16; }
    d += ` H${bw - 8}`;
    this.wave = el("path", { d, class: "wave" }, body);
    this.refresh = (st, lv) => this.wave.setAttribute("class", `wave lv-${lv.out || st.level || "X"}`);
  },

  pattern(body, bw, bh) {
    el("rect", { x: 0, y: 0, width: bw, height: bh, rx: 4, class: "part-box" }, body);
    const y0 = bh / 2 + 9, y1 = bh / 2 - 9;
    const d = `M8,${y0} H14 V${y1} H30 V${y0} H36 V${y1} H40 V${y0} H50 V${y1} H${bw - 8}`;
    this.wave = el("path", { d, class: "wave" }, body);
    this.refresh = (st, lv) => this.wave.setAttribute("class", `wave lv-${lv.out || st.level || "X"}`);
  },

  probe(body, bw, bh) {
    el("rect", { x: 0, y: 0, width: bw, height: bh, rx: 4, class: "part-box meter" }, body);
    this.big = el("text", { x: 16, y: bh / 2 + 7, class: "sw-level", "text-anchor": "middle" }, body);
    this.freq = el("text", { x: 30, y: bh / 2 + 4, class: "meter-txt" }, body);
    this.refresh = (st, lv) => {
      const l = st.level || lv.in || "X";
      if (this.big.textContent !== l) this.big.textContent = l;
      this.big.setAttribute("class", `sw-level lv-${l}`);
      const f = st.freq_hz ? fmtHzShort(st.freq_hz) : "—";
      if (this.freq.textContent !== f) this.freq.textContent = f;
    };
  },

  counter(body, bw, bh) {
    el("rect", { x: 0, y: 0, width: bw, height: bh, rx: 4, class: "part-box meter" }, body);
    this.num = el("text", { x: bw - 7, y: bh / 2 + 7, class: "counter-num", "text-anchor": "end" }, body);
    this.refresh = (st) => {
      const v = String(counterValue(st));
      if (this.num.textContent !== v) this.num.textContent = v;
    };
  },
};

function railSymbol(body, bw, bh) {
  const t = this.part.type;
  const cy = bh / 2;
  el("rect", { x: 0, y: 0, width: bw, height: bh, class: "gate-hitbox" }, body);
  this.rail = el("g", { class: "rail" }, body);
  this.refresh = (st) => {
    const lv = st.level || (t === "vcc" ? "H" : t === "gnd" ? "L" : (this.part.params && this.part.params.level) || "X");
    if (this._lv === lv) return;
    this._lv = lv;
    this.rail.innerHTML = "";
    this.rail.setAttribute("class", `rail lv-${lv}`);
    const g = this.rail;
    if (lv === "H") {
      el("path", { d: `M8,6 H32 M20,6 V${cy} H${bw}`, class: "rail-line" }, g);
      const tx = el("text", { x: 20, y: bh - 2, class: "rail-txt", "text-anchor": "middle" }, g);
      tx.textContent = t === "vcc" ? "VCC" : "H";
    } else if (lv === "L") {
      el("path", { d: `M${bw},${cy} H20 V${cy + 8} M8,${cy + 8} H32 M12,${cy + 13} H28 M16,${cy + 18} H24`,
                   class: "rail-line" }, g);
      const tx = el("text", { x: 20, y: cy - 6, class: "rail-txt", "text-anchor": "middle" }, g);
      tx.textContent = t === "gnd" ? "GND" : "L";
    } else {
      el("rect", { x: 6, y: cy - 11, width: 26, height: 22, rx: 3, class: "part-box" }, g);
      el("path", { d: `M32,${cy} H${bw}`, class: "rail-line" }, g);
      const tx = el("text", { x: 19, y: cy + 5, class: "rail-txt", "text-anchor": "middle" }, g);
      tx.textContent = lv;
    }
  };
}
