// Whole-board canvas: every chip's package on a grid, nets as wires, UART links as dashed arrows.
//
// Layout: the chips sit on a grid of equal square cells (the largest footprint). choosePlan() sizes the drawing from
// the canvas: it tries every column count, parts beside the chips or in a row below them, a smaller part scale and the
// compact package footprint (pin-name room only), and keeps the plan whose fit makes pin names readable (pitch on
// screen ≥ ~10 px, see package.js setScale), then parts, then the largest scale. Between the columns run vertical routing *gutters*, between the
// rows (and above/below) horizontal *streets*; both are sized for the number of wires, and no package body sits
// in them, so wires never cross a body.
// Routing (Manhattan): every wire leaves its pin straight outward: a left/right-side pin horizontally into the
// gutter on that side, a top/bottom-side pin vertically into the street above/below its row. Each net / link
// direction owns one *track* (a fixed offset inside every gutter and every street), so parallel wires never
// overlap. Members that end in the same gutter (or street) are joined along it; otherwise every member runs
// along its gutter to one trunk street (the one with the least vertical travel; members ending in another
// street first run along that street to the nearer gutter), and the trunk joins them.
//
// Parts (switches, LEDs, gates, ...; parts.js) either sit in extra columns of the same grid ("side": sources left of
// the chips, logic and indicators right of them, packed into the row bands, never into a street), or in a parts row
// under the chips ("below": sources, logic, indicators left to right, stacked in narrow columns with their own small
// gutters; their wires rise to the street between the chips and the parts row, which is then the trunk). Either way
// the ports are on the left (inputs) / right (outputs), so each port wire leaves straight into a gutter like a pin.
//
// Pan/zoom: mouse wheel zooms around the cursor, dragging the background pans, double-click (or "fit") resets,
// clicking a chip body zooms to that chip (again: fit); keys + - 0, toolbar − % + fit (% = 1:1). Wire mode (app.js):
// pins are picked to create nets.
// Reading the drawing: level of detail hides pin labels/numbers too small to read; net and link labels keep a fixed
// on-screen size and are placed where they collide with nothing; a link whose TX and RX pins share a net rides on
// that net (no second line; the byte count goes into the net label). Pins: hover shows a data card, click pins it
// (copy, drive H / L / open, change history), alt-click runs the app's click handler (drive cycle). "/" finds a pin.
// Styles: board.css (loaded from here, after style.css).
import { NS, el, ensureDefs, PackageView, ownerCategory, isGpioFunction, geometry, LABEL_CHARS, LABEL_CHARS_COMPACT } from "./package.js";
import { PartView, partGeom, partCategory } from "./parts.js";

if (typeof document !== "undefined" && !document.querySelector('link[data-board-css]')) {
  const l = document.createElement("link");
  l.rel = "stylesheet"; l.href = "/static/board.css"; l.dataset.boardCss = "1";
  document.head.appendChild(l);
}

const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

export function fmtNs(ns) {
  if (ns == null || !isFinite(ns)) return "—";
  const a = Math.abs(ns);
  if (a < 1e3) return `${ns.toFixed(0)} ns`;
  if (a < 1e6) return `${(ns / 1e3).toFixed(3)} µs`;
  if (a < 1e9) return `${(ns / 1e6).toFixed(3)} ms`;
  return `${(ns / 1e9).toFixed(6)} s`;
}

const LABEL_PX = 11;          // on-screen size of net / link labels
const MAX_SCALE = 8;          // most zoomed in: screen px per world unit

const GUTTER_MIN = 90;
const TRACK = 8;              // spacing between parallel tracks
const EDGE = 24;
const PART_GAP = 18;          // vertical gap between parts in a column (part units)
const GP_MIN = 34;            // narrowest gutter between the columns of the parts row
const NAME_PITCH_PX = 8.7;    // pin pitch on screen at which names render at MIN_LABEL_PX (package.js: cap pitch * 1.04)
const PART_UNIT_PX = 0.55;    // part symbols: screen px per part unit wanted at fit

function pinLookup(chip) {
  const m = new Map();
  for (const p of chip.pins) {
    for (const k of [p.short, p.gpio_name, p.qname, ...String(p.name || "").split("/")]) {
      if (k && !m.has(k.toUpperCase())) m.set(k.toUpperCase(), p);
    }
  }
  return m;
}

// "A.PTC6" -> [chipId, "PTC6"]
export function splitQualified(name, ids) {
  const s = String(name);
  const i = s.indexOf(".");
  if (i > 0) {
    const cid = s.slice(0, i);
    const hit = ids.find((x) => x.toUpperCase() === cid.toUpperCase());
    if (hit) return [hit, s.slice(i + 1)];
  }
  return [null, s];
}

// does an owner/function string name the TX (or RX) signal of peripheral `periph`?
// UART0_TX / USART1_TX / LPUART0_TX / SCITXDA (TI: SCI-A) / "PTA2/UART0_TX"
export function isUartSignal(owner, periph, dir) {
  if (!owner || !periph) return false;
  const P = periph.toUpperCase();
  for (const part of String(owner).toUpperCase().split("/")) {
    const re = dir === "tx" ? /_?TXD?/ : /_?RXD?/;
    if (!re.test(part)) continue;
    const base = part.replace(re, "");
    if (base === P || base === P.replace(/_/g, "")) return true;
  }
  return false;
}

export function fmtRate(bps) {
  if (!bps) return "";
  return bps >= 1000 ? `${(bps / 1000).toFixed(bps >= 1e4 ? 0 : 1)} kB/s` : `${Math.round(bps)} B/s`;
}

export class BoardView {
  constructor(svg, info, handlers = {}) {
    this.svg = svg;
    this.h = handlers;
    this.rows = new Map();          // chip id -> latest rows (for link endpoint search)
    this.selCid = null;
    this.selNet = null;
    this.hlNet = null;
    this.wireMode = false;
    this.pending = null;            // wire mode: first picked pin {cid, num} or part port {part, port}
    this.partStates = {};           // part id -> latest state
    this.netLevels = new Map();     // net name -> level
    this.clickFn = null;            // setPinClickHandler: the app's pin click (drive cycle, wire / pick modes)
    this.card = null;               // pinned/hover pin card state {cid, num, pinned}
    this.pinLog = new Map();        // "A:77" -> level changes from the event log {n, t, last, seq} (loadHistory)
    svg.classList.add("bv");
    this.buildOverlay();
    this.autoFit = true;
    this.setInfo(info, true);
    this.wirePanZoom();
    this.wireKeys();
    // re-fit after the first layout pass, and whenever the canvas changes size
    requestAnimationFrame(() => { if (this.autoFit) this.fit(); });
    if (typeof ResizeObserver !== "undefined") {
      let rt = null;
      this._ro = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => this.onCanvasResize(), 60); });
      this._ro.observe(svg);
    }
    let tm = null;
    window.addEventListener("resize", () => { clearTimeout(tm); tm = setTimeout(() => this.replanIfNeeded(), 250); });
  }

  // (re)build everything from /api/chip-shaped info (runtime wiring changes call this again)
  setInfo(info, first = false) {
    const keep = !first && this.vb ? { vb: { ...this.vb }, W: this.W, H: this.H } : null;
    this.info = info;
    this.isBoard = !!info.board;
    this.chips = info.chips;
    this.ids = this.chips.map((c) => c.id);
    this.lookups = new Map(this.chips.map((c) => [c.id, pinLookup(c)]));
    this.pkgs = new Map();
    this.build();
    this.buildFindIndex();
    this.legend?.querySelectorAll(".board-only").forEach((e) => { e.hidden = !this.isBoard; });
    if (keep && !this.autoFit && Math.abs(keep.W - this.W) < 1 && Math.abs(keep.H - this.H) < 1) {
      const v = keep.vb;
      this.setViewBox(v.x, v.y, v.w, v.h);
    } else {
      this.fit();
    }
    for (const [cid, rows] of this.rows) this.pkgs.get(cid)?.update(rows);
    this.updateParts();
    if (this.links.length) this.layoutLinks();
    if (this.selCid) this.setSelected(this.selCid);
    if (this.selNet && !this.netEls.has(this.selNet)) this.selNet = null;
    this.applyNetClasses();
    if (this.pending) {
      if (this.pending.part) this.setPendingPort(this.pending.part, this.pending.port);
      else this.setPending(this.pending.cid, this.pending.num);
    }
    this.placeLabels();
    if (this.findInput && this.findInput.value) this.runFind(false);
    if (this.card && !this.pkgs.get(this.card.cid)?.items.get(this.card.num)) this.hideCard(true);
  }

  build() {
    const svg = this.svg;
    svg.innerHTML = "";
    ensureDefs(svg);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", this.isBoard ? `board ${this.info.board.name}` : `${this.info.name} pinout`);
    const defs = svg.querySelector("defs");
    const mk = el("marker", { id: "arrowHead", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7,
                              orient: "auto-start-reverse" }, defs);
    el("path", { d: "M0,0 L10,5 L0,10 z", class: "arrow-head" }, mk);

    this.world = el("g", { class: "world" }, svg);
    this.wireLayer = el("g", { class: "wires" }, this.world);
    this.pkgLayer = el("g", { class: "packages" }, this.world);
    this.partLayer = el("g", { class: "parts" }, this.world);
    this.linkLayer = el("g", { class: "links" }, this.world);
    this.labelLayer = el("g", { class: "labels" }, this.world);
    this.markerLayer = el("g", { class: "markers" }, this.world);
    this.rubber = null;

    let nDirs = 0;
    for (const ln of this.info.links || []) nDirs += ln.bidir === false ? 1 : 2;
    this.parts = this.isBoard ? (this.info.parts || []) : [];
    this.netList = this.allNets();
    const nRoutes = this.netList.length + nDirs;
    this.nTracks = Math.max(1, nRoutes);
    const B = this.isBoard;
    this.G = B ? Math.max(GUTTER_MIN, 44 + this.nTracks * TRACK) : 0;       // gutter width
    this.ST = B ? Math.max(40, 28 + this.nTracks * TRACK) : 0;              // street height
    this.edge = B ? EDGE : 0;

    this.partGeo = new Map(this.parts.map((p) => [p.id, partGeom(p)]));
    this.trackOf = new Map(this.netList.map((n, i) => [n.name, i]));      // net -> track (= routeNets order)
    const plan = this.plan = this.choosePlan();
    this.svg.dataset.plan = this.planSig(plan);         // for tests / debugging
    const sizes = this.chips.map((c) => {
      const pv = new PackageView(this.pkgLayer, c, {
        onClick: (chip, pin, e) => this.pinClick(chip, pin, e),
        onHover: (chip, pin, e) => this.showCard(chip.id, pin.num, e),
        onLeave: () => this.hideCard(false),
        onBody: B ? (chip, e) => this.h.onBodyClick && this.h.onBodyClick(chip, e) : null, showId: B,
        labelChars: plan.chars,
      });
      this.pkgs.set(c.id, pv);
      return pv.size;
    });
    this.sizes = sizes;
    this.S = Math.max(...sizes);
    this.pk = plan.pk;                                  // part units -> world units
    this.svg.style.setProperty("--pkinv", String(1 / this.pk));
    this.cols = B ? plan.cols : 1;
    this.nrows = Math.ceil(this.chips.length / this.cols);
    this.below = B && this.parts.length > 0 && plan.mode === "below";
    const { S, G, ST } = this;
    // columns: [source parts...] [chips...] [logic parts...] [indicator parts...] (parts row: no part columns)
    const pc = this.below ? { src: [], logic: [], sink: [] } : this.packParts(this.nrows, this.pk, S);
    const colW = [...pc.src.map((c) => c.w), ...Array(this.cols).fill(S), ...pc.logic.map((c) => c.w),
                  ...pc.sink.map((c) => c.w)];
    this.chip0 = pc.src.length;
    this.ncols = colW.length;
    this.colW = colW;
    this.colLeft = [];
    let x0 = this.edge + G;
    for (const w of colW) { this.colLeft.push(x0); x0 += w + G; }
    this.boxes = [];
    this.chips.forEach((c, i) => {
      const pv = this.pkgs.get(c.id);
      const r = Math.floor(i / this.cols), col = this.chip0 + (i % this.cols);
      const x = this.colX(col) + (S - sizes[i]) / 2;
      const y = this.rowY(r) + (S - sizes[i]) / 2;
      pv.root.setAttribute("transform", `translate(${x} ${y})`);
      pv.ox = x; pv.oy = y;
      this.boxes.push({ x, y, w: sizes[i], h: sizes[i], body: pv.geo.off, bodyLen: pv.geo.bodyLen, r, c: col });
    });
    this.partViews = new Map();
    this.partBox = new Map();       // part id -> box index
    this.pg = null;                 // parts row gutters {x: [centre], w: [width], loc: [Map track -> slot]}
    this.W = 2 * this.edge + colW.reduce((a, b) => a + b, 0) + (B ? (this.ncols + 1) * G : 0);
    this.H = this.nrows * S + (B ? (this.nrows + 1) * ST : 0);
    if (this.below) {
      const pr = this.packBelow(this.pk, plan.stack);
      this.W = Math.max(this.W, pr.W + 2 * this.edge);
      this.placePartsBelow(pr);
      this.H += pr.H + EDGE / 2;
    } else {
      this.placeParts([...pc.src, ...Array(this.cols).fill(null), ...pc.logic, ...pc.sink]);
    }
    this.svg.style.aspectRatio = `${this.W} / ${this.H}`;

    this.routeNets();
    this.buildLinks();
  }

  // ---------------------------------------------------------------- layout planning
  // canvas size (px) and the toolbar box the fitted drawing keeps clear of
  canvasBox() {
    const wrap = this.svg.parentElement;
    const aw = Math.max(320, this.svg.clientWidth || (wrap && wrap.clientWidth) || 1200);
    const ah = Math.max(240, (this.svg.clientHeight > 150 && this.svg.clientHeight) ||
                             (wrap && wrap.clientHeight > 150 && wrap.clientHeight) || window.innerHeight * 0.6);
    const tb = this.toolbarBox();
    return { aw, ah, tb };
  }

  toolbarBox() {
    return this.toolbarOver && this.toolbar && this.toolbar.offsetWidth
      ? { w: this.toolbar.offsetWidth + 16, h: this.toolbar.offsetHeight + 10 } : { w: 0, h: 0 };
  }

  // fit scale of a W x H drawing: the toolbar strip above it, or the toolbar column beside it, whichever is larger
  static fitScale(W, H, cb) {
    const { aw, ah, tb } = cb;
    const top = ah - tb.h > 200 ? tb.h : 0;
    return Math.max(Math.min(aw / W, (ah - top) / H), Math.min((aw - tb.w) / W, ah / H));
  }

  // world size of a layout o = {chars, cols, mode: "side"|"below", pk, stack}
  dims(o) {
    const S = Math.max(...this.chips.map((c) => geometry(c.pins.length, o.chars).size));
    if (!this.isBoard) return { W: S, H: S };
    const G = this.G, ST = this.ST;
    const rows = Math.ceil(this.chips.length / o.cols);
    const W = 2 * EDGE + o.cols * S + (o.cols + 1) * G, H = rows * S + (rows + 1) * ST;
    if (!this.parts.length) return { W, H };
    if (o.mode === "side") {
      const pc = this.packParts(rows, o.pk, S);
      return { W: W + [...pc.src, ...pc.logic, ...pc.sink].reduce((a, c) => a + c.w + G, 0), H };
    }
    const pr = this.packBelow(o.pk, o.stack);
    return { W: Math.max(W, pr.W + 2 * EDGE), H: H + pr.H + EDGE / 2 };
  }

  // the layout whose fit reads best: pin names first (pitch on screen), then part symbols, the full label room
  // (owners at fit), then scale
  choosePlan() {
    const n = this.chips.length, B = this.isBoard;
    const cb = this.canvasBox();
    const g0 = this.chips.map((c) => geometry(c.pins.length));
    const pitch = Math.min(...g0.map((g) => g.pitch)), font = Math.min(...g0.map((g) => g.font));
    const cands = [];
    for (const chars of [LABEL_CHARS, LABEL_CHARS_COMPACT]) {
      const S = Math.max(...this.chips.map((c) => geometry(c.pins.length, chars).size));
      const pk0 = Math.min(2.6, Math.max(1, S / 430));
      const colsList = !B ? [1] : n <= 2 ? [n] : Array.from({ length: n }, (_, i) => n - i);
      for (const cols of colsList) {
        if (!B || !this.parts.length) { cands.push({ chars, cols, mode: "side", pk: pk0 }); continue; }
        for (const f of [1, 0.8, 0.64, 0.5]) {
          const pk = Math.max(0.8, pk0 * f);
          cands.push({ chars, cols, mode: "side", pk });
          for (let stack = 1; stack <= Math.min(4, this.parts.length); stack++) cands.push({ chars, cols, mode: "below", pk, stack });
        }
      }
    }
    let best = null;
    for (const o of cands) {
      const d = this.dims(o);
      const s = BoardView.fitScale(d.W, d.H, cb);
      const names = Math.min(1, pitch * s / NAME_PITCH_PX);
      const parts = B && this.parts.length ? Math.min(1, o.pk * s / PART_UNIT_PX) : 1;
      // the full label room only pays off if owners are large enough to be drawn at this fit (package.js OWNER_MIN_PX)
      const owners = o.chars === LABEL_CHARS && font * 0.92 * s >= 5.5 ? 0.4 : 0;
      const score = 3 * names + parts + owners + 0.3 * Math.min(1, s) + (o.mode === "side" ? 0.01 : 0);
      if (!best || score > best.score + 1e-9) best = { ...o, score, s };
    }
    return best;
  }

  planSig(p) { return p ? `${p.chars}|${p.cols}|${p.mode}|${p.pk.toFixed(3)}|${p.stack || 0}` : ""; }

  // the canvas changed size: rebuild if another layout now reads better
  replanIfNeeded() {
    if (this.planSig(this.choosePlan()) !== this.planSig(this.plan)) this.setInfo(this.info);
  }

  colX(c) { return this.colLeft[c]; }
  rowY(r) { return this.ST + r * (this.S + this.ST); }
  // gutter g runs left of column g (g = ncols: right of the last column); "p<i>": gutter i of the parts row
  gutterC(g) {
    if (typeof g === "string") return this.pg.x[+g.slice(1)];
    if (g < this.ncols) return this.colLeft[g] - this.G / 2;
    return this.colLeft[this.ncols - 1] + this.colW[this.ncols - 1] + this.G / 2;
  }

  // ---------------------------------------------------------------- parts layout
  // pack the parts of each category into columns of `nrows` row bands (height S each):
  // {src|logic|sink: [{w, items: [{p, r, y}]}]} with y relative to the band top, each band's stack centred
  packParts(nrows, k = this.pk, S = this.S) {
    const out = { src: [], logic: [], sink: [] };
    if (!this.parts.length) return out;
    for (const cat of ["src", "logic", "sink"]) {
      const list = this.parts.filter((p) => partCategory(p.type) === cat);
      let col = null, r = 0, y = 0;
      const newCol = () => { col = { w: 0, items: [] }; out[cat].push(col); r = 0; y = 0; };
      for (const p of list) {
        const g = this.partGeo.get(p.id);
        const h = g.h * k;
        if (!col) newCol();
        while (y > 0 && y + h > S - 16) {          // band full: next band, else next column
          r += 1; y = 0;
          if (r >= nrows) newCol();
        }
        col.items.push({ p, r, y, h });
        col.w = Math.max(col.w, g.w * k + 16);
        y += h + PART_GAP * k;
      }
      for (const c of out[cat]) {                   // centre each band's stack vertically
        for (let rr = 0; rr < nrows; rr++) {
          const its = c.items.filter((it) => it.r === rr);
          if (!its.length) continue;
          const last = its[its.length - 1];
          const off = (S - (last.y + last.h)) / 2;
          for (const it of its) it.y += off;
        }
      }
    }
    return out;
  }

  // parts row under the chips: sources, logic, indicators left to right, `stack` per column; the gutters around the
  // columns only need room for the nets whose part ports leave into them
  packBelow(pk, stack) {
    const order = ["src", "logic", "sink"].flatMap((cat) => this.parts.filter((p) => partCategory(p.type) === cat));
    const cols = [];
    let col = null;
    for (const p of order) {
      const g = this.partGeo.get(p.id);
      if (!col || col.items.length >= stack) { col = { w: 0, h: 0, items: [] }; cols.push(col); }
      const y = col.items.length ? col.h + PART_GAP * pk : 0;
      col.items.push({ p, y, h: g.h * pk });
      col.h = y + g.h * pk;
      col.w = Math.max(col.w, g.w * pk + 16);
    }
    const gtr = Array.from({ length: cols.length + 1 }, () => new Set());
    cols.forEach((c, ci) => {
      for (const it of c.items) {
        for (const port of this.partGeo.get(it.p.id).ports) {
          const k = this.trackOf.get((it.p.ports || {})[port.name]);
          if (k != null) gtr[port.side === "L" ? ci : ci + 1].add(k);
        }
      }
    });
    const gw = gtr.map((t) => Math.max(GP_MIN, 24 + t.size * TRACK));
    const H = Math.max(0, ...cols.map((c) => c.h));
    const W = cols.reduce((a, c) => a + c.w, 0) + gw.reduce((a, b) => a + b, 0);
    return { cols, gtr, gw, W, H };
  }

  placePartsBelow(pr) {
    const k = this.pk;
    const top = this.rowY(this.nrows);                  // the parts row starts below street `nrows`
    let x = (this.W - pr.W) / 2;
    const gx = [], loc = [];
    pr.cols.forEach((c, ci) => {
      gx.push(x + pr.gw[ci] / 2);
      x += pr.gw[ci];
      for (const it of c.items) {
        const g = this.partGeo.get(it.p.id);
        const px = x + (c.w - g.w * k) / 2, py = top + (pr.H - c.h) / 2 + it.y;
        this.addPartView(it.p, px, py, { w: g.w * k, h: g.h * k, r: this.nrows, c: -1, prow: true, pc: ci });
      }
      x += c.w;
    });
    gx.push(x + pr.gw[pr.cols.length] / 2);
    for (const t of pr.gtr) loc.push(new Map([...t].sort((a, b) => a - b).map((kk, i) => [kk, i])));
    this.pg = { x: gx, w: pr.gw, loc };
  }

  partHandlers() {
    return {
      onAction: (p, v) => this.h.onPartAction && this.h.onPartAction(p, v),
      onPortClick: (p, port, e) => this.h.onPortClick && this.h.onPortClick(p, port, e),
      onPortHover: (p, port, e) => this.h.onPortHover && this.h.onPortHover(p, port, e),
      onPortLeave: () => this.h.onPartLeave && this.h.onPartLeave(),
      onBodyHover: (p, e) => this.h.onPartHover && this.h.onPartHover(p, e),
      onBodyLeave: () => this.h.onPartLeave && this.h.onPartLeave(),
      onBodyClick: (p, e) => this.h.onPartClick && this.h.onPartClick(p, e),
    };
  }

  addPartView(p, x, y, box) {
    const pv = new PartView(this.partLayer, p, this._ph || (this._ph = this.partHandlers()));
    pv.root.setAttribute("transform", `translate(${x} ${y}) scale(${this.pk})`);
    pv.ox = x; pv.oy = y;
    this.partViews.set(p.id, pv);
    this.partBox.set(p.id, this.boxes.length);
    this.boxes.push({ x, y, part: true, ...box });
  }

  // cols: one entry per grid column (null for chip columns)
  placeParts(cols) {
    const k = this.pk;
    cols.forEach((c, ci) => {
      if (!c) return;
      for (const it of c.items) {
        const g = this.partGeo.get(it.p.id);
        const x = this.colLeft[ci] + (c.w - g.w * k) / 2;
        this.addPartView(it.p, x, this.rowY(it.r) + it.y, { w: g.w * k, h: g.h * k, r: it.r, c: ci });
      }
    });
  }

  // world anchor of a part port (outer lead end)
  portAnchor(partId, port) {
    const pv = this.partViews && this.partViews.get(partId);
    const a = pv && pv.portAnchor(port);
    if (!a) return null;
    const k = this.pk;
    return { side: a.side, x: pv.ox + a.x * k, y: pv.oy + a.y * k, dx: a.side === "L" ? -1 : 1, dy: 0,
             ci: this.partBox.get(partId) };
  }

  // nets to draw: the board's nets plus nets only parts are on (names from the parts' port maps)
  allNets() {
    const nets = [...(this.info.nets || [])];
    const have = new Set(nets.map((n) => n.name));
    for (const p of this.parts) {
      for (const n of Object.values(p.ports || {})) {
        if (n && !have.has(n)) { have.add(n); nets.push({ name: n, pins: [], level: "X" }); }
      }
    }
    return nets;
  }

  // part states (/api/state board.parts, `part` events) and net levels -> symbols
  updateParts(states, netLevels) {
    if (states) Object.assign(this.partStates, states);
    if (netLevels) this.netLevels = netLevels;
    for (const [id, pv] of this.partViews || []) {
      const lv = {};
      for (const [port, net] of Object.entries(pv.part.ports || {})) lv[port] = net ? (this.netLevels.get(net) || "X") : null;
      pv.update(this.partStates[id] || pv.part.state || {}, lv);
    }
  }

  highlightPart(id) {
    for (const [pid, pv] of this.partViews || []) pv.setClass("hl", pid === id);
  }
  streetC(s) { return this.ST / 2 + s * (this.S + this.ST); }
  off(k, room) {
    const sp = Math.min(TRACK, Math.max(2, (room - 20) / this.nTracks));
    return (k - (this.nTracks - 1) / 2) * sp;
  }
  gx(g, k) {
    if (typeof g === "string") {                       // parts row gutter: local slots for the nets that use it
      const i = +g.slice(1), loc = this.pg.loc[i], n = Math.max(1, loc.size);
      const sp = Math.min(TRACK, Math.max(2, (this.pg.w[i] - 16) / n));
      return this.pg.x[i] + ((loc.get(k) ?? 0) - (n - 1) / 2) * sp;
    }
    return this.gutterC(g) + this.off(k, this.G);
  }
  sy(s, k) { return this.streetC(s) + this.off(k, this.ST); }
  trackX(g, k) { return this.gx(g, k); }

  // ---------------------------------------------------------------- coordinates
  pinRef(qname) {
    const [cid0, rest] = splitQualified(qname, this.ids);
    const cid = cid0 || this.ids[0];
    const pin = this.lookups.get(cid)?.get(rest.toUpperCase());
    return pin ? { cid, pin } : null;
  }

  // world anchor of a pin: outer pad end, outward direction, side, chip index
  anchor(cid, num) {
    const pv = this.pkgs.get(cid);
    const a = pv && pv.anchor(num);
    if (!a) return null;
    return { ...a, x: a.x + pv.ox, y: a.y + pv.oy, ci: this.ids.indexOf(cid) };
  }

  // anchor at the middle of a chip body's edge facing chip `towardIdx` (fallback for links)
  bodyAnchor(cid, towardIdx) {
    const ci = this.ids.indexOf(cid);
    const b = this.boxes[ci];
    const t = this.boxes[towardIdx];
    const right = t ? (t.c > b.c || (t.c === b.c && towardIdx !== ci && b.c < this.chip0 + this.cols - 1)) : true;
    const x = b.x + b.body + (right ? b.bodyLen : 0);
    return { side: right ? "R" : "L", x, y: b.y + b.h / 2, dx: right ? 1 : -1, dy: 0, ci, body: true };
  }

  // ---------------------------------------------------------------- routing
  // first leg of a member: from the pin straight out into the gutter (L/R side) or street (T/B side)
  exitOf(m, k) {
    const b = this.boxes[m.ci];
    if (b.prow) {                                      // parts row: into the gutter beside the part's column
      const g = `p${m.side === "L" ? b.pc : b.pc + 1}`;
      return { type: "v", idx: g, m, pts: [[m.x, m.y], [this.gx(g, k), m.y]] };
    }
    if (m.side === "L" || m.side === "R") {
      const g = m.side === "L" ? b.c : b.c + 1;
      return { type: "v", idx: g, m, pts: [[m.x, m.y], [this.gx(g, k), m.y]] };
    }
    const s = m.side === "T" ? b.r : b.r + 1;
    return { type: "h", idx: s, m, pts: [[m.x, m.y], [m.x, this.sy(s, k)]] };
  }

  // members: anchors; k: track. Returns {d, segs} (or {d, pts} for a directed two-member route).
  route(members, k, { directed = false } = {}) {
    const ex = members.map((m) => this.exitOf(m, k));
    const last = (pts) => pts[pts.length - 1];
    let paths, trunk = null;
    const sameV = ex.every((e) => e.type === "v" && e.idx === ex[0].idx);
    const sameH = ex.every((e) => e.type === "h" && e.idx === ex[0].idx);
    if (sameV || sameH) {
      paths = ex.map((e) => e.pts);
      const ends = paths.map(last);
      if (ends.length > 1) {
        if (sameV) {
          const ys = ends.map((p) => p[1]);
          trunk = [[ends[0][0], Math.min(...ys)], [ends[0][0], Math.max(...ys)]];
        } else {
          const xs = ends.map((p) => p[0]);
          trunk = [[Math.min(...xs), ends[0][1]], [Math.max(...xs), ends[0][1]]];
        }
      }
    } else {
      let ts = 0, bestCost = Infinity;
      // parts-row gutters only reach the street right above the parts row: that one is the trunk then
      const s0 = ex.some((e) => typeof e.idx === "string") ? this.nrows : 0;
      for (let s = s0; s <= this.nrows; s++) {
        const y = this.sy(s, k);
        const cost = ex.reduce((acc, e) => acc + Math.abs(last(e.pts)[1] - y), 0);
        if (cost < bestCost - 0.5) { bestCost = cost; ts = s; }
      }
      const ty = this.sy(ts, k);
      paths = ex.map((e) => {
        const pts = e.pts.slice();
        const [x, y] = last(pts);
        if (e.type === "h" && e.idx === ts) return pts;
        if (e.type === "h") {
          // along this street to the gutter beside the chip that faces the other members, then to the trunk
          const b = this.boxes[e.m.ci];
          const others = members.filter((o) => o !== e.m);
          const toward = others.reduce((acc, o) => acc + o.x, 0) / Math.max(1, others.length);
          const g = toward < b.x + b.w / 2 ? b.c : b.c + 1;
          const gx = this.gx(g, k);
          pts.push([gx, y], [gx, ty]);
          return pts;
        }
        pts.push([x, ty]);
        return pts;
      });
      const xs = paths.map((p) => last(p)[0]);
      trunk = [[Math.min(...xs), ty], [Math.max(...xs), ty]];
    }
    const r = (v) => Math.round(v * 10) / 10;
    const poly = (pts) => "M" + pts.map((p) => `${r(p[0])},${r(p[1])}`).join(" L");
    if (directed && members.length === 2) {
      // one continuous polyline from member 0 to member 1 (so markers can travel along it)
      const pts = [...paths[0], ...paths[1].slice().reverse()];
      return { d: poly(pts), pts };
    }
    const segs = trunk ? [...paths, trunk] : paths;
    return { d: segs.map(poly).join(" "), segs };
  }

  static spread(k) { return 0.22 + 0.56 * ((k * 0.618034) % 1); }

  // a point on the longest segment: {x, y, vert}
  // f: position along that segment (labels of parallel wires are spread by track so they do not stack)
  static labelSpot(polylines, f = 0.5) {
    let best = -1, spot = null;
    for (const pts of polylines) {
      for (let i = 0; i + 1 < pts.length; i++) {
        const [a, b] = [pts[i], pts[i + 1]];
        const l = Math.hypot(b[0] - a[0], b[1] - a[1]);
        if (l > best) {
          best = l;
          spot = { x: a[0] + (b[0] - a[0]) * f, y: a[1] + (b[1] - a[1]) * f,
                   vert: Math.abs(b[0] - a[0]) < Math.abs(b[1] - a[1]) };
        }
      }
    }
    return spot;
  }

  routeNets() {
    this.netEls = new Map();
    this.netTrack = new Map();      // net name -> track
    this.netSegs = new Map();       // net name -> polylines (label placement)
    this.netLabel = new Map();      // net name -> <text>
    this.netPins = new Map();       // net name -> [{cid, num}]
    this.netPorts = new Map();      // net name -> [{part, port}]
    for (const p of this.parts) {
      for (const [port, n] of Object.entries(p.ports || {})) {
        if (!n) continue;
        if (!this.netPorts.has(n)) this.netPorts.set(n, []);
        this.netPorts.get(n).push({ part: p.id, port });
      }
    }
    let k = 0;
    for (const net of this.netList) {
      const members = [];
      const refs = [];
      for (const q of net.pins || []) {
        const ref = this.pinRef(q);
        const a = ref && this.anchor(ref.cid, ref.pin.num);
        if (a) { members.push(a); refs.push({ cid: ref.cid, num: ref.pin.num }); }
      }
      const ports = this.netPorts.get(net.name) || [];
      for (const pr of ports) {
        const a = this.portAnchor(pr.part, pr.port);
        if (a) members.push(a);
      }
      this.netPins.set(net.name, refs);
      const track = k++;
      this.netTrack.set(net.name, track);
      if (members.length < 1) continue;
      const { d, segs } = this.route(members, track);
      const g = el("g", { class: `net net-${net.level || "X"}`, "data-net": net.name }, this.wireLayer);
      el("path", { d, class: "wire-hit" }, g);
      el("path", { d, class: "wire-halo" }, g);
      el("path", { d, class: "wire" }, g);
      for (const m of members) el("circle", { cx: m.x, cy: m.y, r: 3, class: "wire-end" }, g);
      const t = el("title", {}, g);
      t.textContent = `net ${net.name}: ${[...(net.pins || []), ...ports.map((x) => `${x.part}.${x.port}`)].join(", ")}` +
        `${net.pull ? ` (pull ${net.pull})` : ""} · click: select`;
      // name tag: placed once, collision-free (placeLabels)
      this.netSegs.set(net.name, segs);
      const lab = el("text", { class: "net-label", "text-anchor": "middle" }, this.labelLayer);
      lab.textContent = net.name;
      lab.dataset.base = net.name;
      lab.dataset.net = net.name;
      this.netLabel.set(net.name, lab);
      lab.addEventListener("mouseenter", () => this.h.onNetHover && this.h.onNetHover(net.name, true));
      lab.addEventListener("mouseleave", () => this.h.onNetHover && this.h.onNetHover(net.name, false));
      lab.addEventListener("click", (e) => { e.stopPropagation(); this.h.onNetClick && this.h.onNetClick(net.name); });
      g.addEventListener("mouseenter", () => this.h.onNetHover && this.h.onNetHover(net.name, true));
      g.addEventListener("mouseleave", () => this.h.onNetHover && this.h.onNetHover(net.name, false));
      g.addEventListener("click", (e) => { e.stopPropagation(); this.h.onNetClick && this.h.onNetClick(net.name); });
      this.netEls.set(net.name, g);
    }
    this.linkTrack0 = k;
  }

  // ---------------------------------------------------------------- net highlight / selection / wire mode
  applyNetClasses() {
    const hl = this.hlNet || this.selNet;
    this.svg.classList.toggle("hl-on", !!hl);
    for (const [name, g] of this.netEls || []) {
      g.classList.toggle("hl", name === hl);
      g.classList.toggle("sel", name === this.selNet);
    }
    for (const [name, lab] of this.netLabel || []) lab.classList.toggle("hl", name === hl);
    for (const it of this._hlPins || []) it.g.classList.remove("net-hl");
    this._hlPins = [];
    for (const ref of (hl && this.netPins.get(hl)) || []) {
      const it = this.pkgs.get(ref.cid)?.items.get(ref.num);
      if (it) { it.g.classList.add("net-hl"); this._hlPins.push(it); }
    }
    for (const pv of (this.partViews || new Map()).values()) for (const port of pv.portEls.keys()) pv.setPortClass(port, "net-hl", false);
    for (const pr of (hl && this.netPorts.get(hl)) || []) this.partViews.get(pr.part)?.setPortClass(pr.port, "net-hl", true);
  }

  highlightNet(name) { this.hlNet = name || null; this.applyNetClasses(); }
  selectNet(name) { this.selNet = name || null; this.applyNetClasses(); }

  setWireMode(on) {
    this.wireMode = !!on;
    this.svg.classList.toggle("wire-mode", this.wireMode);
    if (!on) this.setPending(null);
  }

  // wire mode: mark the first picked pin and draw a rubber band from it to the pointer
  setPending(cid, num) {
    if (this._pendingIt) this._pendingIt.g.classList.remove("wire-pending");
    this._pendingIt = null;
    if (this._pendingPort) { this._pendingPort.pv.setPortClass(this._pendingPort.port, "wire-pending", false); this._pendingPort = null; }
    if (this.rubber) { this.rubber.remove(); this.rubber = null; }
    this.pending = cid ? { cid, num } : null;
    if (!cid) return;
    const it = this.pkgs.get(cid)?.items.get(num);
    if (it) { it.g.classList.add("wire-pending"); this._pendingIt = it; }
    const a = this.anchor(cid, num);
    if (a) {
      this.rubber = el("line", { x1: a.x, y1: a.y, x2: a.x, y2: a.y, class: "rubber" }, this.markerLayer);
      this.rubberFrom = a;
    }
  }

  // wire mode: first pick was a part port
  setPendingPort(partId, port) {
    this.setPending(null);
    const pv = this.partViews && this.partViews.get(partId);
    if (!pv) return;
    this.pending = { part: partId, port };
    pv.setPortClass(port, "wire-pending", true);
    this._pendingPort = { pv, port };
    const a = this.portAnchor(partId, port);
    if (a) {
      this.rubber = el("line", { x1: a.x, y1: a.y, x2: a.x, y2: a.y, class: "rubber" }, this.markerLayer);
      this.rubberFrom = a;
    }
  }

  // ---------------------------------------------------------------- UART links
  buildLinks() {
    this.links = [];
    this.linkByDesc = new Map();
    let k = this.linkTrack0 || 0;
    for (const ln of this.info.links || []) {
      const [ca, pa] = splitQualified(ln.from, this.ids);
      const [cb, pb] = splitQualified(ln.to, this.ids);
      const dirs = [{ src: [ca, pa], dst: [cb, pb], key: "ab" }];
      if (ln.bidir !== false) dirs.push({ src: [cb, pb], dst: [ca, pa], key: "ba" });
      const L = { ln, dirs: [], stats: ln.stats || {} };
      for (const d of dirs) {
        L.dirs.push({ ...d, track: k++, g: el("g", { class: "link" + (ln.auto ? " link-auto" : "") }, this.linkLayer),
                      sig: null, busy: 0, rate: null });
      }
      this.links.push(L);
      this.linkByDesc.set(ln.describe, L);
    }
    this.layoutLinks();
  }

  findUartPin(cid, periph, dir) {
    const rows = this.rows.get(cid) || [];
    const chip = this.chips.find((c) => c.id === cid);
    for (const r of rows) {
      if (isUartSignal(r.owner, periph, dir)) return chip.pins.find((p) => p.num === r.num) || null;
    }
    return null;
  }

  // (re)route each link direction from the TX pin of the sender to the RX pin of the receiver, as found from
  // the live pin owners; else between the chip bodies. Rebuilt only when the endpoints change.
  layoutLinks() {
    for (const L of this.links || []) {
      for (const D of L.dirs) {
        const [sc, sper] = D.src, [dc, dp] = D.dst;
        const tx = this.findUartPin(sc, sper, "tx");
        const rx = this.findUartPin(dc, dp, "rx");
        // a direction whose TX and RX pins are on one net rides on that net: no second line
        const netOf = (cid, pin) => pin && ((this.rows.get(cid) || []).find((r) => r.num === pin.num) || {}).net;
        const nTx = netOf(sc, tx), nRx = netOf(dc, rx);
        const onNet = tx && rx && nTx && nTx === nRx && this.netTrack.has(nTx) ? nTx : null;
        const sig = `${tx ? tx.num : "-"}|${rx ? rx.num : "-"}|${onNet || ""}`;
        if (sig === D.sig) continue;
        D.sig = sig;
        D.onNet = onNet;
        if (D.label) { D.label.remove(); D.label = null; }
        const si = this.ids.indexOf(sc), di = this.ids.indexOf(dc);
        const a = tx ? this.anchor(sc, tx.num) : this.bodyAnchor(sc, di);
        const b = rx ? this.anchor(dc, rx.num) : this.bodyAnchor(dc, si);
        if (a.body) a.y += (D.key === "ab" ? -1 : 1) * 18;
        if (b.body) b.y += (D.key === "ab" ? -1 : 1) * 18;
        const { d, pts } = this.route([a, b], onNet ? this.netTrack.get(onNet) : D.track, { directed: true });
        D.g.innerHTML = "";
        D.g.setAttribute("class", `link link-${D.key}` + (L.ln.auto ? " link-auto" : "") + (tx && rx ? "" : " link-body") +
                         (onNet ? " link-onnet" : ""));
        D.path = el("path", { d, class: "link-line", "marker-end": onNet ? null : "url(#arrowHead)" }, D.g);
        D.pts = pts;
        D.text = `${sc}.${sper} → ${dc}.${dp}`;
        D.title = el("title", {}, D.g);
        if (!onNet) {
          D.label = el("text", { class: "link-label", "text-anchor": "middle" }, this.labelLayer);
          D.label.dataset.link = "1";
        }
        this.setLinkText(L, D);
        this._labelsDirty = true;
      }
    }
    if (this._labelsDirty) { this._labelsDirty = false; this.placeLabels(); }
  }

  setLinkText(L, D) {
    const n = D.key === "ab" ? L.stats.a_to_b : L.stats.b_to_a;
    const rate = D.rate && D.rate.bps ? ` · ${fmtRate(D.rate.bps)}` : "";
    if (D.onNet) {
      // the net label carries the byte count of the link(s) riding on it
      const lab = this.netLabel.get(D.onNet);
      let max = 0;
      for (const L2 of this.links) for (const D2 of L2.dirs) {
        if (D2.onNet !== D.onNet) continue;
        max = Math.max(max, (D2.key === "ab" ? L2.stats.a_to_b : L2.stats.b_to_a) || 0);
      }
      const t = `${lab.dataset.base} · ${D.text.split(" → ")[0]} ${max} B`;
      if (lab && lab.textContent !== t) lab.textContent = t;
    }
    if (!D.label) {
      if (D.title) D.title.textContent = `UART link ${L.ln.describe || ""} on net ${D.onNet || "?"}: ${n ?? 0} bytes`;
      return;
    }
    const t = `${D.text}${n != null ? ` · ${n} B` : ""}${rate}`;
    if (D.label.textContent !== t) D.label.textContent = t;
    D.title.textContent = `UART link ${L.ln.describe || ""}${L.ln.auto ? ` (automatic, from net ${L.ln.net})` : ""}: ` +
      `${D.text}${n != null ? `, ${n} bytes` : ""}${rate ? `, ${fmtRate(D.rate.bps)} (virtual time)` : ""}` +
      (D.g.classList.contains("link-body") ? " (TX/RX pins not muxed yet: drawn between the chip bodies)" : "");
  }

  // bytes per virtual second of a link direction ("A.UART0 -> B.UART0", "ab" | "ba")
  linkRate(desc, key) {
    const L = this.linkByDesc && this.linkByDesc.get(desc);
    const D = L && L.dirs.find((d) => d.key === key);
    return D && D.rate ? D.rate.bps : 0;
  }

  // a byte was transmitted by `cid`.`uart`: animate a marker along each matching link direction
  pulse(cid, uart, byte) {
    for (const L of this.links || []) {
      for (const D of L.dirs) {
        if (D.src[0] !== cid || String(D.src[1]).toUpperCase() !== String(uart).toUpperCase() || !D.path) continue;
        D.g.classList.add("active");
        clearTimeout(D.tm);
        D.tm = setTimeout(() => D.g.classList.remove("active"), 400);
        if (D.busy >= 3) continue;           // at most 3 markers in flight per direction
        D.busy++;
        const g = el("g", { class: `marker marker-${D.key}` }, this.markerLayer);
        el("circle", { r: 5 }, g);
        const t = el("text", { y: -8, "text-anchor": "middle" }, g);
        t.textContent = byte >= 32 && byte < 127 ? String.fromCharCode(byte) : byte === 10 ? "↵" : byte === 13 ? "⏎" : "·";
        const path = D.path;
        const len = path.getTotalLength();
        const t0 = performance.now(), dur = 650;
        const step = (now) => {
          const f = Math.min(1, (now - t0) / dur);
          const p = path.getPointAtLength(f * len);
          g.setAttribute("transform", `translate(${p.x} ${p.y})`);
          if (f < 1 && g.isConnected) requestAnimationFrame(step);
          else { g.remove(); D.busy--; }
        };
        requestAnimationFrame(step);
      }
    }
  }

  // ---------------------------------------------------------------- live updates
  // rowsByChip: Map chip id -> rows; changed: Map chip id -> Set(num)
  update(rowsByChip, changed = new Map()) {
    for (const [cid, rows] of rowsByChip) {
      this.rows.set(cid, rows);
      const pv = this.pkgs.get(cid);
      if (pv) pv.update(rows, changed.get(cid) || new Set());
    }
    if (this.links && this.links.length) this.layoutLinks();
    if (this._hlPins && this._hlPins.length) for (const it of this._hlPins) it.g.classList.add("net-hl");
    if (this._pendingIt) this._pendingIt.g.classList.add("wire-pending");
    if (this._findCls) for (const it of this._findCls) it.g.classList.add(it === this._findCur ? "find-cur" : "find-match");
    if (this.card && this.card.pinned) this.renderCard();
  }

  updateBoard(board) {
    if (!board) return;
    if (board.nets) this.netLevels = new Map(board.nets.map((n) => [n.name, n.level || "X"]));
    let ps = board.parts || null;
    if (Array.isArray(ps)) ps = Object.fromEntries(ps.map((p) => [p.id, p.state || {}]));
    this.updateParts(ps);
    for (const n of board.nets || []) {
      const g = this.netEls && this.netEls.get(n.name);
      if (!g) continue;
      const c = `net net-${n.level || "X"}${n.conflict ? " conflict" : ""}` +
        (n.name === (this.hlNet || this.selNet) ? " hl" : "") + (n.name === this.selNet ? " sel" : "");
      if (g.getAttribute("class") !== c) g.setAttribute("class", c);
    }
    const t = board.t_ns;
    for (const s of board.links || []) {
      const L = this.linkByDesc && this.linkByDesc.get(s.link);
      if (!L) continue;
      L.stats = s;
      for (const D of L.dirs) {
        const n = (D.key === "ab" ? s.a_to_b : s.b_to_a) || 0;
        const r = D.rate;
        if (!r || t < r.t || n < r.n) D.rate = { t, n, bps: r && t >= r.t ? r.bps : 0 };
        else if (t - r.t >= 2e6) D.rate = { t, n, bps: (n - r.n) / ((t - r.t) / 1e9) };   // over >= 2 ms virtual
        this.setLinkText(L, D);
      }
    }
  }

  setSelected(cid) {
    this.selCid = cid;
    for (const [id, pv] of this.pkgs) pv.setSelected(this.isBoard && id === cid);
  }

  // ---------------------------------------------------------------- pan / zoom
  setViewBox(x, y, w, h) {
    this.vb = { x, y, w, h };
    this.svg.setAttribute("viewBox", `${x} ${y} ${w} ${h}`);
    this.applyScale();
  }

  // screen px per world unit (preserveAspectRatio: meet)
  scale() {
    const cw = this.svg.clientWidth, ch = this.svg.clientHeight;
    if (!cw || !ch || !this.vb) return 1;
    return Math.min(cw / this.vb.w, ch / this.vb.h);
  }

  applyScale() {
    const s = this.scale();
    this._lastScale = s;
    for (const pv of this.pkgs.values()) pv.setScale(s);
    this.svg.style.setProperty("--ls", `${LABEL_PX / s}px`);
    this.svg.style.setProperty("--lw", `${1 / s}px`);
    if (this.pctBtn) this.pctBtn.textContent = `${Math.round(s * 100)}%`;
    if (Math.abs(s - (this._placedAt || 0)) / s > 0.02) {
      clearTimeout(this._placeTm);
      this._placeTm = setTimeout(() => this.placeLabels(), 120);
    }
  }

  // world bounding box of the drawing from the layout model (footprints incl. their label room, parts, wires,
  // links), not from getBBox: pin labels change size with the zoom, the fit must not depend on them
  contentBox() {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const add = (x, y, w = 0, h = 0) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h); };
    for (const b of this.boxes || []) {
      // compact footprints: names at their minimum size may run a little past the reserved room
      const o = b.part || this.plan.chars >= 16 ? 0 : b.body * 0.2;
      add(b.x - o, b.y - o, b.w + 2 * o, b.h + 2 * o);
    }
    for (const segs of (this.netSegs || new Map()).values()) for (const pts of segs) for (const [x, y] of pts) add(x, y);
    for (const L of this.links || []) for (const D of L.dirs) for (const [x, y] of D.pts || []) add(x, y);
    if (!isFinite(x0)) return { x: 0, y: 0, w: this.W, h: this.H };
    const pad = 8;
    return { x: x0 - pad, y: y0 - pad, w: x1 - x0 + 2 * pad, h: y1 - y0 + 2 * pad };
  }

  // fit the drawing into the whole canvas (its own aspect ratio, no letterboxing), keeping clear of the toolbar
  // (strip above the drawing or column beside it, whichever leaves it larger); stays "auto-fit" (re-fitted on
  // canvas resize) until the user zooms or pans
  fit() {
    const cw = this.svg.clientWidth, ch = this.svg.clientHeight;
    const bb = this.contentBox();
    this.zoomed = null;
    this.autoFit = true;
    if (!cw || !ch) { this.setViewBox(bb.x, bb.y, bb.w, bb.h); return; }
    const tb = this.toolbarBox();
    const top = ch - tb.h > 200 ? tb.h : 0;
    // room for the net / link labels (constant on-screen size) on the outermost wires: they prefer the side above /
    // left of a wire, so most of it goes there
    const wired = (this.netSegs && this.netSegs.size) || (this.links && this.links.length);
    const m = wired ? (LABEL_PX + 6) / Math.min(cw / bb.w, ch / bb.h) : 0;
    bb.x -= m; bb.y -= m; bb.w += 1.4 * m; bb.h += 1.4 * m;
    const sA = Math.min(cw / bb.w, (ch - top) / bb.h), sB = Math.min((cw - tb.w) / bb.w, ch / bb.h);
    let sc, ox, oy;                                     // screen offset of the drawing's top-left corner
    if (sA >= sB) { sc = sA; ox = (cw - bb.w * sc) / 2; oy = top + (ch - top - bb.h * sc) / 2; }
    else { sc = sB; ox = (cw - tb.w - bb.w * sc) / 2; oy = (ch - bb.h * sc) / 2; }
    this.setViewBox(bb.x - ox / sc, bb.y - oy / sc, cw / sc, ch / sc);
  }

  // canvas resized (window, splitter drag, pane collapse): re-fit while auto-fitting, else keep the centre and scale
  onCanvasResize() {
    const cw = this.svg.clientWidth, ch = this.svg.clientHeight;
    if (!cw || !ch || !this.vb) return;
    clearTimeout(this._replanTm);
    this._replanTm = setTimeout(() => this.replanIfNeeded(), 300);
    if (this.autoFit) { this.fit(); return; }
    const v = this.vb, s = this._lastScale || this.scale();
    const w = cw / s, h = ch / s;
    this.setViewBox(v.x + v.w / 2 - w / 2, v.y + v.h / 2 - h / 2, w, h);
  }

  // 1:1 (one world unit = one CSS px), around the current centre
  oneToOne() { this.zoomScale(1); }

  zoomScale(target, cx, cy) {
    const v = this.vb, s = this.scale();
    if (cx == null) { cx = v.x + v.w / 2; cy = v.y + v.h / 2; }
    const k = s / Math.min(MAX_SCALE, Math.max(0.02, target));
    this.setViewBox(cx - v.w * k / 2, cy - v.h * k / 2, v.w * k, v.h * k);
    this.zoomed = null;
    this.autoFit = false;
  }

  // centre and zoom onto one chip (keeps the canvas aspect ratio); again on the same chip: fit
  zoomTo(cid) {
    const i = this.ids.indexOf(cid);
    const b = this.boxes[i];
    if (!b) return;
    if (this.zoomed === cid) { this.fit(); return; }
    const pad = Math.max(this.G, this.ST) * 0.6 + 10;
    let w = b.w + 2 * pad, h = b.h + 2 * pad;
    const ar = (this.svg.clientWidth && this.svg.clientHeight) ? this.svg.clientWidth / this.svg.clientHeight : this.W / this.H;
    this.autoFit = false;
    if (w / h < ar) w = h * ar; else h = w / ar;
    this.setViewBox(b.x + b.w / 2 - w / 2, b.y + b.h / 2 - h / 2, w, h);
    this.zoomed = cid;
  }

  zoom(f, cx, cy) {
    const v = this.vb;
    const s = this.scale();
    const minW = v.w * s / MAX_SCALE;                    // at most MAX_SCALE px per unit
    const w = Math.min(this.W * 3, Math.max(minW, v.w * f));
    const k = w / v.w;
    const h = v.h * k;
    if (cx == null) { cx = v.x + v.w / 2; cy = v.y + v.h / 2; }
    this.setViewBox(cx - (cx - v.x) * k, cy - (cy - v.y) * k, w, h);
    this.autoFit = false;
  }

  toWorld(clientX, clientY) {
    const m = this.svg.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    const p = new DOMPoint(clientX, clientY).matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  }

  wirePanZoom() {
    const svg = this.svg;
    svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = this.toWorld(e.clientX, e.clientY);
      this.zoom(Math.exp(e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)), p.x, p.y);
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, vb: { ...this.vb }, moved: false, id: e.pointerId };
    });
    svg.addEventListener("pointermove", (e) => {
      if (this.rubber && this.rubberFrom) {
        const p = this.toWorld(e.clientX, e.clientY);
        this.rubber.setAttribute("x2", p.x);
        this.rubber.setAttribute("y2", p.y);
      }
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      if (!drag.moved) { drag.moved = true; svg.setPointerCapture(drag.id); svg.classList.add("panning"); }
      const m = svg.getScreenCTM();
      const s = m ? m.a : 1;
      this.setViewBox(drag.vb.x - dx / s, drag.vb.y - dy / s, drag.vb.w, drag.vb.h);
      this.autoFit = false;
    });
    const end = () => {
      if (drag && drag.moved) {
        this._suppressClick = true;
        setTimeout(() => { this._suppressClick = false; }, 0);
        svg.classList.remove("panning");
      }
      drag = null;
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    // a drag that ends on a pin must not count as a click
    svg.addEventListener("click", (e) => { if (this._suppressClick) { e.stopPropagation(); e.preventDefault(); } }, true);
    svg.addEventListener("dblclick", (e) => { if (!e.target.closest(".pin, .part")) this.fit(); });
    // click on the empty background: clear the net selection
    svg.addEventListener("click", (e) => {
      if (e.target.closest(".pin, .net, .net-label, .body-g, .part")) return;
      if (this.card && this.card.pinned) this.hideCard(true);
      if (this.h.onBackgroundClick) this.h.onBackgroundClick();
    });
  }

  // ---------------------------------------------------------------- canvas overlay: toolbar, legend, pin card
  buildOverlay() {
    const wrap = this.svg.parentElement;
    if (!wrap) return;
    if (getComputedStyle(wrap).position === "static") wrap.style.position = "relative";
    wrap.classList.add("bv-wrap");
    const bar = document.createElement("div");
    bar.className = "bv-tools";
    bar.innerHTML = `
      <input type="search" class="bv-find" placeholder="find pin  /" spellcheck="false" autocomplete="off"
             aria-label="Find a pin: name, chip.name, datasheet name or owner (Enter: next, Shift+Enter: previous, Esc: clear)"
             title="Find a pin: name, chip.name, datasheet name or owner · Enter: next · Shift+Enter: previous · Esc: clear">
      <span class="bv-count" aria-live="polite"></span>
      <button type="button" data-z="out" title="Zoom out (-)">−</button>
      <button type="button" data-z="one" class="bv-pct" title="Zoom level · click: 1:1 (one unit = one pixel)">100%</button>
      <button type="button" data-z="in" title="Zoom in (+)">+</button>
      <button type="button" data-z="fit" title="Fit (0, F or double-click)">fit</button>`;
    // in the board pane's header when there is one (the canvas keeps its full height), else floating on the canvas
    const head = wrap.closest(".pane") && wrap.closest(".pane").querySelector(":scope > .pane-head");
    if (head) {
      bar.classList.add("in-head");
      const tools = head.querySelector(":scope > .head-tools");
      head.insertBefore(bar, tools || null);
    } else {
      wrap.appendChild(bar);
    }
    this.toolbar = bar;
    this.toolbarOver = !head;      // overlaps the canvas: the fit keeps clear of it
    this.findInput = bar.querySelector(".bv-find");
    this.findCount = bar.querySelector(".bv-count");
    this.pctBtn = bar.querySelector(".bv-pct");
    bar.addEventListener("click", (e) => {
      const z = e.target.closest("button")?.dataset.z;
      if (z === "in") this.zoom(1 / 1.4); else if (z === "out") this.zoom(1.4);
      else if (z === "fit") this.fit(); else if (z === "one") this.oneToOne();
    });
    this.findInput.addEventListener("input", () => this.runFind(true));
    this.findInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.findStep(e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); this.findInput.value = ""; this.runFind(true); this.findInput.blur(); }
    });

    // compact legend: drawn with the canvas' own classes, so it always matches
    const lg = document.createElement("details");
    lg.className = "bv-legend";
    const pad = (cls, extra = "") =>
      `<svg class="bv" width="22" height="12" viewBox="0 0 22 12"><g class="pin ${cls}"><rect class="pad" x="2" y="2" width="18" height="8" rx="1"/>${extra}</g></svg>`;
    const wire = (cls) => `<svg class="bv" width="24" height="12" viewBox="0 0 24 12"><g class="net ${cls}"><path class="wire" d="M2,6 H22"/></g></svg>`;
    const arrowIn = `<path class="arrow" d="M14,6 L8,3 L8,9 Z"/>`;
    lg.innerHTML = `<summary>legend</summary>
      <div class="bv-lg-grid">
        <span>${pad("hi")}H</span><span>${pad("lo")}L (hollow)</span><span>${pad("x")}floating / X</span>
        <span>${pad("power")}power</span><span>${pad("analog")}analog</span><span>${pad("lo own-disabled")}disabled</span>
        <span>${pad("hi", arrowIn)}input ▸ in / ◂ out</span>
        <span><svg class="bv" width="14" height="12" viewBox="0 0 14 12"><g class="pin"><circle class="ext ext-H" cx="7" cy="6" r="3.5"/></g></svg>driven externally</span>
        <span><b class="bv-own">UART0_TX</b> / <b class="bv-gpio">GPIO</b> owner</span>
        <span class="board-only">${wire("net-H")}net H</span><span class="board-only">${wire("net-L")}net L</span>
        <span class="board-only">${wire("net-X")}net X</span><span class="board-only">${wire("net-X conflict")}conflict</span>
        <span class="board-only"><svg class="bv" width="24" height="12" viewBox="0 0 24 12"><g class="link"><path class="link-line" d="M2,6 H22"/></g></svg>UART link</span>
      </div>
      <div class="bv-keys">hover pin: data · click: pin card (copy, drive) · alt-click: drive H→L→open · / find · + − 0 zoom</div>`;
    wrap.appendChild(lg);
    this.legend = lg;

    const card = document.createElement("div");
    card.className = "bv-card";
    card.hidden = true;
    card.setAttribute("role", "dialog");
    document.body.appendChild(card);
    this.cardEl = card;
    card.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b || !this.card) return;
      const act = b.dataset.act;
      if (act === "close") this.hideCard(true);
      else if (act === "copy") this.copyCard(b);
      else if (act === "drive") this.drive(this.card.cid, this.card.num, b.dataset.lv || null);
      else if (act === "find") this.zoomToPin(this.card.cid, this.card.num);
    });
  }

  wireKeys() {
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.target.closest && e.target.closest("input, select, textarea, [contenteditable]")) return;
      if (e.key === "+" || e.key === "=") { e.preventDefault(); this.zoom(1 / 1.4); }
      else if (e.key === "-" || e.key === "_") { e.preventDefault(); this.zoom(1.4); }
      else if (e.key === "0") { e.preventDefault(); this.fit(); }
      else if (e.key === "/") { e.preventDefault(); this.findInput && this.findInput.focus(); this.findInput && this.findInput.select(); }
      else if (e.key === "Escape" && this.card && this.card.pinned) this.hideCard(true);
    });
  }

  // the app's pin click (drive cycle / wire mode / pick mode); plain clicks open the pin card instead
  setPinClickHandler(fn) { this.clickFn = fn; }

  pinClick(chip, pin, e) {
    const appFn = this.clickFn || this.h.onPinClick;
    const modal = this.wireMode || document.body.classList.contains("picking");
    if (appFn && (modal || (e && e.altKey))) { appFn(chip, pin, e); if (this.card && this.card.pinned) this.renderCard(); return; }
    this.pinCard(chip.id, pin.num, e);
  }

  // hover card (follows the pointer; ignored while a card is pinned)
  showCard(cid, num, e) {
    if (this.card && this.card.pinned) return;
    this.card = { cid, num, pinned: false, x: e ? e.clientX : 0, y: e ? e.clientY : 0 };
    this.renderCard();
    if (!this.pinLog.has(`${cid}:${num}`)) {         // count from the event log once the pointer rests on the pin
      clearTimeout(this._histTm);
      this._histTm = setTimeout(() => { if (this.card && this.card.cid === cid && this.card.num === num) this.loadHistory(cid, num); }, 150);
    }
  }

  // pinned card: stays open, selectable, with copy / drive buttons and the pin's change history
  pinCard(cid, num, e) {
    const it = this.pkgs.get(cid)?.items.get(num);
    if (!it) return;
    let x = e && e.clientX, y = e && e.clientY;
    if (x == null || (!x && !y)) {
      const r = it.g.getBoundingClientRect();
      x = r.right; y = r.bottom;
    }
    this.card = { cid, num, pinned: true, x, y };
    this.renderCard();
    this.loadHistory(cid, num);
    if (this.h.onPinSelect) this.h.onPinSelect(this.chips.find((c) => c.id === cid), it.pin);
  }

  hideCard(evenPinned) {
    if (!this.card || (this.card.pinned && !evenPinned)) return;
    this.card = null;
    this.cardEl.hidden = true;
    this._cardHtml = "";
  }

  // [label, value] rows of a pin, from the chip metadata and the live pin row
  cardModel(cid, num) {
    const pv = this.pkgs.get(cid);
    const it = pv && pv.items.get(num);
    if (!it) return null;
    const pin = it.pin, r = it.row || {};
    const name = this.isBoard ? `${cid}.${pin.short}` : pin.short;
    const net = r.net ? `${r.net}${this.netMembers(r.net, `${cid}.${pin.short}`)}` : "";
    const rows = [
      ["pin", `${pin.num}`], ["datasheet", pin.name || ""], ["kind", pin.kind || ""],
      ["owner", r.owner ?? "—"], ["dir", r.direction ?? "—"], ["level", r.level ?? "X"],
      ["drive", r.drive ?? "—"], ["pull", r.pull ?? "none"], ["external", r.external ?? "open"],
    ];
    if (this.isBoard) rows.unshift(["chip", `${cid} ${pv.chip.name}`]);
    if (net) rows.push(["net", net]);
    if (r.cfg_by) rows.push(["config", `${r.cfg_by} @ ${fmtNs(r.cfg_t)}${r.cfg_what ? ` · ${r.cfg_what}` : ""}`]);
    // level changes counted from the event log (fetched once per pin, then kept current by notePinEvents)
    const h = this.pinLog.get(`${cid}:${num}`);
    rows.push(["changes", !h || h.loading ? "… (event log)" : h.err ? h.err
      : `${h.n} in the event log` + (h.n ? `, last ${h.last} @ ${fmtNs(h.t)}` : "")]);
    if (this.card && this.card.err) rows.push(["error", this.card.err]);
    const alts = Object.entries(pin.functions || {}).map(([k, v]) =>
      [k, v, v === r.owner || (r.owner === "GPIO" && isGpioFunction(v, pin))]);
    return { pin, row: r, name, rows, alts, cat: ownerCategory(pin, r.owner) };
  }

  netMembers(net, self) {
    const n = (this.info.nets || []).find((x) => x.name === net);
    const others = n ? (n.pins || []).filter((q) => q.toUpperCase() !== self.toUpperCase()) : [];
    return others.length ? ` → ${others.join(", ")}` : "";
  }

  renderCard() {
    const c = this.card;
    if (!c) return;
    const m = this.cardModel(c.cid, c.num);
    if (!m) { this.hideCard(true); return; }
    const lv = m.row.level || "X";
    const gpio = m.pin.kind === "gpio";
    const ext = m.row.external ?? null;
    const hint = this.wireMode ? "click: wire" : c.pinned ? "" : `click: pin card${gpio ? " · alt-click: drive" : ""}`;
    const html = `
      <div class="bv-card-h"><b class="mono">${esc(m.name)}</b><span class="bv-lv lv-${lv}">${lv}</span>
        ${c.pinned ? `<button type="button" data-act="find" title="Centre the drawing on this pin">locate</button>
        <button type="button" data-act="copy" title="Copy as text">copy</button>
        <button type="button" data-act="close" title="Close (Esc)" aria-label="close">×</button>` : ""}</div>
      <dl>${m.rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      ${m.alts.length ? `<div class="bv-alts">${m.alts.map(([k, v, on]) => `<span>${esc(k)}</span><span${on ? ' class="on"' : ""}>${esc(v)}</span>`).join("")}</div>` : ""}
      ${c.pinned && gpio ? `<div class="bv-drive"><span>drive</span>${["H", "L", ""].map((l) =>
        `<button type="button" data-act="drive" data-lv="${l}" class="${(ext || "") === l ? "on" : ""}">${l || "open"}</button>`).join("")}</div>` : ""}
      ${hint ? `<div class="bv-hint">${esc(hint)}</div>` : ""}`;
    if (html !== this._cardHtml) { this.cardEl.innerHTML = html; this._cardHtml = html; }
    this.cardEl.classList.toggle("pinned", c.pinned);
    this.cardEl.hidden = false;
    // position: beside the pointer, kept inside the window
    const pad = 14, w = this.cardEl.offsetWidth, h = this.cardEl.offsetHeight;
    let x = c.x + pad, y = c.y + pad;
    if (x + w > window.innerWidth - 8) x = c.x - w - pad;
    if (y + h > window.innerHeight - 8) y = Math.max(8, window.innerHeight - h - 8);
    this.cardEl.style.left = `${Math.max(8, x)}px`;
    this.cardEl.style.top = `${Math.max(8, y)}px`;
  }

  cardText() {
    const c = this.card;
    const m = c && this.cardModel(c.cid, c.num);
    if (!m) return "";
    const lines = [`${m.name}`, ...m.rows.map(([k, v]) => `${k}: ${v}`)];
    if (m.alts.length) lines.push(`functions: ${m.alts.map(([k, v, on]) => `${k}=${v}${on ? "*" : ""}`).join(" ")}`);
    return lines.join("\n");
  }

  async copyCard(btn) {
    const text = this.cardText();
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      ta.remove();
    }
    if (btn) { btn.textContent = ok ? "copied" : "copy failed"; setTimeout(() => { if (btn.isConnected) btn.textContent = "copy"; }, 1200); }
  }

  // Level changes of a pin, counted from the server's event log (the exact count, not UI frames): fetched once per
  // pin when its card first opens, then kept current from the live stream (notePinEvents, called by app.js).
  async loadHistory(cid, num) {
    const key = `${cid}:${num}`;
    if (this.pinLog.has(key)) return;
    const pin = this.pkgs.get(cid)?.items.get(num)?.pin;
    if (!pin) return;
    const h = { n: 0, t: null, last: "", seq: Infinity, loading: true };
    this.pinLog.set(key, h);
    const qs = `since=0&kinds=pin&limit=0&q=${encodeURIComponent(pin.short)}` + (this.isBoard ? `&chip=${encodeURIComponent(cid)}` : "");
    try {
      const r = await fetch(`/api/events?${qs}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const evs = (d.events || []).filter((e) => e.pin === pin.short && (e.field || "level") === "level" &&
                                                (!this.isBoard || (e.chip ?? cid) === cid));
      const last = evs[evs.length - 1];
      Object.assign(h, { n: evs.length, t: last ? last.t_ns : null, last: last ? `${last.old}→${last.new}` : "",
                         seq: d.last ?? 0, loading: false });
    } catch (err) {
      Object.assign(h, { err: `event log: ${err.message}`, loading: false });
      setTimeout(() => { if (this.pinLog.get(key) === h) this.pinLog.delete(key); }, 5000);   // retry later
    }
    if (this.card && this.card.cid === cid && this.card.num === num) this.renderCard();
  }

  // live `pin` events (app.js): keep the counts of pins whose history was loaded current
  notePinEvents(evs) {
    if (!this.pinLog.size) return;
    let touched = false;
    for (const e of evs) {
      if (e.kind !== "pin" || (e.field || "level") !== "level") continue;
      const cid = this.isBoard ? (e.chip ?? this.ids[0]) : this.ids[0];
      const pin = this.lookups.get(cid)?.get(String(e.pin || "").toUpperCase());
      const h = pin && this.pinLog.get(`${cid}:${pin.num}`);
      if (!h || h.loading || h.err || !(e.seq > h.seq)) continue;
      h.n++; h.t = e.t_ns; h.last = `${e.old}→${e.new}`; h.seq = e.seq;
      touched = true;
    }
    if (touched && this.card) this.renderCard();
  }

  async drive(cid, num, level) {
    const it = this.pkgs.get(cid)?.items.get(num);
    if (!it) return;
    const pin = it.pin;
    const name = pin.qname || (this.isBoard ? `${cid}.${pin.short}` : pin.short);
    try {
      const r = await fetch("/api/pin", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ name, level }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      const row = { ...(it.row || {}), num, external: d.external ?? level, level: d.level ?? (it.row || {}).level };
      this.pkgs.get(cid).update([row]);
      if (this.card) { this.card.err = null; if (this.card.pinned) this.renderCard(); }
    } catch (err) {
      if (this.card) { this.card.err = `drive failed: ${err.message}`; this.renderCard(); }
    }
  }

  // ---------------------------------------------------------------- find a pin
  buildFindIndex() {
    this.findIdx = [];
    for (const c of this.chips) {
      for (const p of c.pins) {
        const keys = new Set([p.short, p.gpio_name, `${c.id}.${p.short}`, p.qname, p.name, ...String(p.name || "").split("/")]
          .filter(Boolean).map((k) => String(k).toUpperCase()));
        this.findIdx.push({ cid: c.id, num: p.num, keys: [...keys] });
      }
    }
  }

  runFind(reset) {
    if (!this.findInput) return;
    const q = (this.findInput.value || "").trim().toUpperCase();
    for (const it of this._findCls || []) it.g.classList.remove("find-match", "find-cur");
    this._findCls = [];
    this._findCur = null;
    this.svg.classList.toggle("find-on", !!q);
    if (!q) { this.findHits = []; this.findCount.textContent = ""; return; }
    const hits = [];
    for (const e of this.findIdx) {
      const it = this.pkgs.get(e.cid)?.items.get(e.num);
      if (!it) continue;
      const owner = String((it.row && it.row.owner) || "").toUpperCase();
      const keys = owner && owner !== "DISABLED" ? [...e.keys, ...owner.split("/")] : e.keys;
      let score = 9;
      for (const k of keys) {
        if (k === q) { score = 0; break; }
        if (k.startsWith(q)) score = Math.min(score, 1);
        else if (k.includes(q)) score = Math.min(score, 2);
      }
      if (score < 9) hits.push({ ...e, it, score });
    }
    hits.sort((a, b) => a.score - b.score || this.ids.indexOf(a.cid) - this.ids.indexOf(b.cid) || a.num - b.num);
    this.findHits = hits;
    if (reset) this.findPos = -1;
    for (const h of hits) { h.it.g.classList.add("find-match"); this._findCls.push(h.it); }
    this.findCount.textContent = hits.length ? `${Math.max(0, this.findPos) + (this.findPos >= 0 ? 1 : 0)}/${hits.length}` : "none";
    if (!hits.length) this.findCount.textContent = "no match";
    else this.findCount.textContent = this.findPos >= 0 ? `${this.findPos + 1}/${hits.length}` : `${hits.length} · Enter`;
  }

  findStep(dir) {
    if (!this.findHits || !this.findHits.length) { this.runFind(true); if (!this.findHits.length) return; }
    const n = this.findHits.length;
    this.findPos = ((this.findPos ?? -1) + dir + n) % n;
    const h = this.findHits[this.findPos];
    if (this._findCur) { this._findCur.g.classList.remove("find-cur"); this._findCur.g.classList.add("find-match"); }
    this._findCur = h.it;
    h.it.g.classList.remove("find-match");
    h.it.g.classList.add("find-cur");
    this.findCount.textContent = `${this.findPos + 1}/${n}`;
    this.zoomToPin(h.cid, h.num);
    const r = h.it.g.getBoundingClientRect();
    this.pinCard(h.cid, h.num, { clientX: r.right, clientY: r.bottom });
  }

  // centre on a pin, zoomed in far enough to read its label
  zoomToPin(cid, num) {
    const a = this.anchor(cid, num);
    const pv = this.pkgs.get(cid);
    if (!a || !pv) return;
    const want = Math.max(this.scale(), Math.min(MAX_SCALE, 13 / pv.geo.font));
    const v = this.vb, s = this.scale();
    const k = s / want;
    const w = v.w * k, h = v.h * k;
    this.setViewBox(a.x - w / 2, a.y - h / 2, w, h);
    this.zoomed = null;
    this.autoFit = false;
  }

  // ---------------------------------------------------------------- label placement
  // Net and link labels have a fixed on-screen size (--ls); each is put on the first spot along its own wire where
  // it overlaps no package footprint, part and no label placed before it (else on its longest run).
  placeLabels() {
    if (!this.labelLayer) return;
    const s = this.scale();
    this._placedAt = s;
    const fs = LABEL_PX / s, cw = fs * 0.62, gap = 3 / s;
    const taken = this.boxes.map((b) => ({ x: b.x, y: b.y, w: b.w, h: b.h }));
    const labels = [];              // label boxes placed so far
    // every wire segment is an obstacle too, except for the label of that wire
    const wires = [];
    const addWire = (owner, polylines) => {
      for (const pts of polylines || []) for (let i = 0; i + 1 < pts.length; i++) {
        const [a, b] = [pts[i], pts[i + 1]], t = 1 / s;
        wires.push({ owner, x: Math.min(a[0], b[0]) - t, y: Math.min(a[1], b[1]) - t,
                     w: Math.abs(b[0] - a[0]) + 2 * t, h: Math.abs(b[1] - a[1]) + 2 * t });
      }
    };
    for (const [name, segs] of this.netSegs || []) addWire(name, segs);
    for (const L of this.links || []) for (const D of L.dirs) if (D.pts && !D.onNet) addWire(D, [D.pts]);
    const ov = (b, t) => b.x < t.x + t.w && t.x < b.x + b.w && b.y < t.y + t.h && t.y < b.y + b.h;
    let owner = null;
    const hit = (b) => labels.some((t) => ov(b, t)) || taken.some((t) => ov(b, t)) || wires.some((w) => w.owner !== owner && ov(b, w));
    const hitLabel = (b) => labels.some((t) => ov(b, t));
    const place = (lab, polylines, sides, own) => {
      owner = own;
      if (!lab || !polylines) return;
      const tw = (lab.textContent || "").length * cw + 4 / s;
      const segs = [];
      for (const pts of polylines) for (let i = 0; i + 1 < pts.length; i++) {
        const [a, b] = [pts[i], pts[i + 1]];
        segs.push({ a, b, l: Math.hypot(b[0] - a[0], b[1] - a[1]), vert: Math.abs(b[0] - a[0]) < Math.abs(b[1] - a[1]) });
      }
      segs.sort((p, q) => q.l - p.l);
      let first = null, pick = null, soft = null;
      outer:
      for (const sg of segs) {
        if (sg.l < tw + 2 * gap && first) continue;
        for (const f of [0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9]) {
          for (const side of sides) {
            const x = sg.a[0] + (sg.b[0] - sg.a[0]) * f, y = sg.a[1] + (sg.b[1] - sg.a[1]) * f;
            const c = sg.vert
              ? { x, y, vert: true, tx: x + side * gap + (side > 0 ? fs * 0.8 : 0), box: { x: side < 0 ? x - gap - fs : x + gap, y: y - tw / 2, w: fs, h: tw } }
              : { x, y, vert: false, ty: y + side * gap + (side > 0 ? fs * 0.8 : 0), box: { x: x - tw / 2, y: side < 0 ? y - gap - fs : y + gap, w: tw, h: fs } };
            if (!first) first = c;
            if (!soft && !hitLabel(c.box)) soft = c;     // fallback: crosses a wire or footprint, but no label
            if (sg.l >= tw + 2 * gap && !hit(c.box)) { pick = c; break outer; }
          }
        }
      }
      // no spot that is clear of other labels: hide it (its <title>, the nets / links panes still have it)
      const c = pick || soft;
      if (!c) { lab.setAttribute("visibility", "hidden"); return; }
      lab.removeAttribute("visibility");
      if (c.vert) {
        lab.setAttribute("x", c.tx); lab.setAttribute("y", c.y);
        lab.setAttribute("transform", `rotate(-90 ${c.tx} ${c.y})`);
      } else {
        lab.setAttribute("x", c.x); lab.setAttribute("y", c.ty);
        lab.removeAttribute("transform");
      }
      labels.push(c.box);
    };
    for (const [name, lab] of this.netLabel || []) place(lab, this.netSegs.get(name), [-1, 1], name);
    for (const L of this.links || []) for (const D of L.dirs) {
      if (D.label && D.pts) place(D.label, [D.pts], D.key === "ab" ? [-1, 1] : [1, -1], D);
    }
  }

}

export { NS };
