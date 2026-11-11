// SVG rendering of a quad flat package (LQFP/QFP/QFN-style) from one entry of /api/chip `chips`.
// Standard counter-clockwise numbering seen from the top: pin 1 at the top of the left side going down,
// then the bottom side left→right, the right side bottom→top and the top side right→left.
// Generic: any pin count (N/4 pins per side, rounded up). The pin pitch shrinks for big packages so every
// body ends up about the same size (100 pins: 22 units, 144: ~15.6, 176: ~12.7).
// Label room: `labelChars` characters of the native label font are reserved outside each side (16: "PTC10 ADC1_SE6b";
// the BoardView picks the compact 11 when the canvas is too small for the full footprint to show names readably).
// Level of detail (setScale): pin names never render below MIN_LABEL_PX on screen while the pitch leaves room for
// them (they are counter-scaled), and hide only when adjacent names would overlap; an owner is shown when the whole
// label still fits the reserved room (or once zoomed in far enough that it may run into the wire gutter).
// No vendor naming: GPIO pins are `kind == "gpio"`, their name is `short` (PTC5, PC5, GPIO34, ...).

export const NS = "http://www.w3.org/2000/svg";

export function el(tag, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  if (parent) parent.appendChild(e);
  return e;
}

const BASE_PITCH = 22;          // pitch of a 100-pin package (the original look)
const BODY_TARGET = 560;        // pins along one side * pitch is scaled toward this
const MIN_PITCH = 11;
export const LABEL_CHARS = 16;  // room reserved for "PTC10 ADC1_SE6b"-style labels
export const LABEL_CHARS_COMPACT = 11;
export const MIN_LABEL_PX = 9;  // pin names: smallest on-screen size while the pitch allows it
const HIDE_LABEL_PX = 7.5;      // below this (pitch too small) names are hidden
const OWNER_MIN_PX = 5.5;       // owners keep their native size; hidden below this

// Is a function/owner string "just the GPIO" of this pin? ("GPIO", the pin's own GPIO name, or that name followed by
// "/..." as in Kinetis ALT1 "PTC5/LLWU_P9"). Comes from the pin data only, never from a naming pattern.
export function isGpioFunction(value, pin) {
  if (typeof value !== "string" || !value) return false;
  if (value === "GPIO") return true;
  for (const n of [pin && pin.short, pin && pin.gpio_name]) {
    if (n && (value === n || value.startsWith(n + "/"))) return true;
  }
  return false;
}

export function pinClass(pin, st) {
  // st: live row from the pin table {level, owner, direction, pull, external, kind}
  const kind = pin.kind;
  if (kind === "power") return "power";
  const owner = st ? st.owner : null;
  if (kind === "analog" || owner === "analog" || (st && st.direction === "analog")) return "analog";
  const lvl = st ? st.level : "X";
  if (lvl === "H") return "hi";
  if (lvl === "L") return "lo";
  return "x";
}

// "none" | "disabled" | "gpio" | "periph"
export function ownerCategory(pin, owner) {
  owner = owner || "";
  if (owner === "disabled") return "disabled";
  if (isGpioFunction(owner, pin)) return "gpio";
  if (pin.kind === "gpio" || pin.kind === "special") return "periph";
  return "none";
}

// Square J-lead chip carriers (PLCC / LCC) number pin 1 in the middle of the top side, counter-clockwise; QFPs start
// at the top of the left side. Returns how many pins sit before the first pin of the left side (PLCC44: pins 1-6).
export function pin1Offset(pkg, nPins, perSide) {
  if (!/^\s*(PLCC|LCC|CLCC|QFJ)\d*/i.test(String(pkg || "")) || nPins !== perSide * 4) return 0;
  return (perSide + 1) >> 1;
}

export function geometry(nPins, labelChars = LABEL_CHARS) {
  const n = Math.max(4, nPins);
  const perSide = Math.ceil(n / 4);
  const pitch = Math.max(MIN_PITCH, Math.min(BASE_PITCH, BODY_TARGET / perSide));
  const k = pitch / BASE_PITCH;
  const pinLen = 22 * Math.max(k, 0.7);
  const pinW = pitch * 0.55;
  const font = Math.max(7, 11.5 * k);
  const numFont = Math.max(6, 9.5 * k);
  const labelW = Math.ceil(font * 0.62 * labelChars) + 8;
  const bodyLen = perSide * pitch + 2 * pitch;      // corner clearance
  const off = labelW + pinLen + 14;                  // body origin inside the footprint
  const size = bodyLen + 2 * off;                    // square footprint incl. pins and labels
  return { n, perSide, pitch, k, pinLen, pinW, font, numFont, labelW, bodyLen, off, size, labelChars };
}

export function ensureDefs(svg) {
  if (svg.querySelector("#hatch")) return;
  const defs = el("defs", {}, svg);
  const pat = el("pattern", { id: "hatch", width: 5, height: 5, patternUnits: "userSpaceOnUse",
                              patternTransform: "rotate(45)" }, defs);
  el("rect", { width: 5, height: 5, class: "hatch-bg" }, pat);
  el("line", { x1: 0, y1: 0, x2: 0, y2: 5, class: "hatch-line" }, pat);
}

export class PackageView {
  // parent: an SVG <g> (positioned by the caller); chip: one /api/chip `chips` entry
  constructor(parent, chip, { onClick, onHover, onLeave, onBody, onFocus, onBlur, showId = false, labelChars = LABEL_CHARS } = {}) {
    this.root = el("g", { class: "package", "data-chip": chip.id }, parent);
    this.chip = chip;
    this.onClick = onClick;
    this.onHover = onHover;
    this.onLeave = onLeave;
    this.onBody = onBody;
    this.onFocus = onFocus;
    this.onBlur = onBlur;
    this.showId = showId;
    this.items = new Map();       // num -> {g, body, tip, arrow, label, ownerSpan, pin, p, row}
    this.timeText = "";
    this.geo = geometry(chip.pins.length, labelChars);
    this.fw = this.geo.font;      // current world font of the pin names (setScale)
    this.ownOk = true;            // owners large enough to draw at all
    this.zoomedIn = false;        // zoomed in far enough that an owner may run past the reserved room
    this.root.style.setProperty("--pf", `${this.geo.font}px`);
    this.root.style.setProperty("--of", `${(this.geo.font * 0.92).toFixed(2)}px`);
    this.build();
  }

  get size() { return this.geo.size; }

  // Centre of pin index i (0-based) on the body edge and its outward direction.
  place(i) {
    const { perSide, bodyLen, off, pitch } = this.geo;
    const side = Math.floor(i / perSide);
    const k = i % perSide;
    const along = pitch * 1.5 + k * pitch;            // from the start corner of that side
    const x0 = off, y0 = off, x1 = off + bodyLen, y1 = off + bodyLen;
    switch (side) {
      case 0: return { side: "L", x: x0, y: y0 + along, dx: -1, dy: 0 };
      case 1: return { side: "B", x: x0 + along, y: y1, dx: 0, dy: 1 };
      case 2: return { side: "R", x: x1, y: y1 - along, dx: 1, dy: 0 };
      default: return { side: "T", x: x1 - along, y: y0, dx: 0, dy: -1 };
    }
  }

  // Outer end of a pin's pad (package-local coordinates) + direction; used to attach wires.
  anchor(num) {
    const it = this.items.get(num);
    if (!it) return null;
    const { p } = it;
    const L = this.geo.pinLen;
    return { side: p.side, x: p.x + p.dx * L, y: p.y + p.dy * L, dx: p.dx, dy: p.dy };
  }

  build() {
    const g = this.geo;
    const root = this.root;
    const { off, bodyLen } = g;
    const chip = this.chip;
    const s = Math.min(1.25, bodyLen / 594);           // text scale relative to the 100-pin body
    root.setAttribute("aria-label", `${chip.id ? chip.id + " " : ""}${chip.name} ${chip.package} pinout`);

    const bodyG = el("g", { class: "body-g" }, root);
    this.bodyRect = el("rect", { x: off, y: off, width: bodyLen, height: bodyLen, rx: 2, class: "body" }, bodyG);
    el("circle", { cx: off + 18, cy: off + 18, r: 5, class: "pin1-dot" }, bodyG);
    if (this.onBody) bodyG.addEventListener("click", (e) => this.onBody(chip, e));

    // body text lines, top to bottom: native size (world units, scaled with the body) and the smallest on-screen size;
    // layoutBody() counter-scales them at small zoom and drops the least important ones that no longer fit
    const cx = off + bodyLen / 2;
    this.bodyLines = [];
    const line = (cls, text, size, minPx, prio, gap = 0.35) => {
      const t = el("text", { x: cx, y: off + bodyLen / 2, class: cls }, bodyG);
      t.textContent = text;
      const L = { el: t, size: size * s, minPx, prio, gap, cw: /title|sub(?! mono)/.test(cls) && !/mono/.test(cls) ? 0.56 : 0.62,
                  len: text.length, shown: true };
      this.bodyLines.push(L);
      return t;
    };
    if (this.showId) line("body-id", chip.id, 44, 16, 0, 0.25);
    line("body-title", chip.name, 20, 10, 3);
    line("body-sub", `${chip.package} · ${chip.cpu}${chip.fpu ? " + FPU" : ""}`, 13, 9, 5);
    this.bodyFw = line("body-sub", chip.firmware ? `firmware: ${chip.firmware}` : "no firmware", 13, 9, 6, 0.9);
    this.bodyTime = line("body-time", "0.000 ms", 19, 10, 1);
    this.bodyStatus = line("body-sub dim", "paused", 13, 9, 2);
    this.bodyPc = line("body-sub dim mono", "", 13, 9, 4);
    this._bodyScale = 1;
    this.layoutBody(1);

    const pins = [...chip.pins].sort((a, b) => a.num - b.num);
    this.order = pins.map((p) => p.num);              // keyboard order (counter-clockwise)
    const rot = pin1Offset(chip.package, pins.length, this.geo.perSide);
    pins.forEach((pin, i) => this.buildPin(pin, this.place((i - rot + pins.length) % pins.length), i === 0));
  }

  // Body text at `scale` screen px per world unit: each line at max(native, minPx on screen), stacked around the body
  // centre; lines too wide for the body, then the least important ones while the stack is too tall, are hidden.
  layoutBody(scale) {
    this._bodyScale = scale;
    const { off, bodyLen } = this.geo;
    const W = bodyLen * 0.9, Hmax = bodyLen * 0.86;
    const lines = this.bodyLines;
    for (const L of lines) {
      L.fs = Math.max(L.size, L.minPx / Math.max(scale, 1e-6));
      L.len = (L.el.textContent || "").length;
      L.shown = L.len === 0 ? true : L.len * L.fs * L.cw <= W;
    }
    const h = () => lines.reduce((a, L) => a + (L.shown && L.len ? L.fs * (1 + L.gap) : 0), 0);
    const byPrio = lines.filter((L) => L.shown).sort((a, b) => b.prio - a.prio);
    while (h() > Hmax && byPrio.length) byPrio.shift().shown = false;
    let y = off + bodyLen / 2 - h() / 2;
    for (const L of lines) {
      const vis = L.shown && L.len > 0;
      if (vis) {
        y += L.fs * (1 + L.gap / 2);
        L.el.setAttribute("y", Math.round((y - L.fs * 0.18) * 10) / 10);
        L.el.style.fontSize = `${Math.round(L.fs * 100) / 100}px`;
        y += L.fs * (L.gap / 2);
      }
      if ((L.el.getAttribute("visibility") !== "hidden") !== (vis || L.len === 0)) {
        L.el.setAttribute("visibility", vis || L.len === 0 ? "visible" : "hidden");
      }
    }
  }

  buildPin(pin, p, first = false) {
    const G = this.geo;
    // roving tabindex: one tab stop per package (the last focused pin), arrow keys move between pins
    const grp = el("g", { class: "pin", "data-num": pin.num, tabindex: first ? 0 : -1, role: "button",
                          "aria-label": `${this.chip.id ? this.chip.id + "." : ""}${pin.short} pin ${pin.num} ${pin.name || ""}` }, this.root);
    const horiz = p.dx !== 0;
    const len = G.pinLen, w = G.pinW;
    let rx, ry, rw, rh;
    if (horiz) {
      rw = len; rh = w;
      rx = p.dx < 0 ? p.x - len : p.x; ry = p.y - w / 2;
    } else {
      rw = w; rh = len;
      rx = p.x - w / 2; ry = p.dy < 0 ? p.y - len : p.y;
    }
    el("rect", { x: rx - 2, y: ry - 2, width: rw + 4, height: rh + 4, class: "hit" }, grp);
    const body = el("rect", { x: rx, y: ry, width: rw, height: rh, rx: 2 * G.k, class: "pad" }, grp);

    // external stimulus marker at the outer tip
    const tipX = p.x + p.dx * (len + 6 * G.k), tipY = p.y + p.dy * (len + 6 * G.k);
    const tip = el("circle", { cx: tipX, cy: tipY, r: 3.6 * Math.max(G.k, 0.7), class: "ext" }, grp);

    // direction arrow drawn on the pad: ▶ into the chip for inputs, ◀ out of the chip for outputs
    const arrow = el("path", { class: "arrow", d: "" }, grp);

    // pin number just inside the body
    const numX = p.x - p.dx * 12 * G.k, numY = p.y - p.dy * 12 * G.k;
    const num = el("text", { x: numX, y: numY, class: "pin-num" }, grp);
    num.style.fontSize = `${G.numFont}px`;
    num.textContent = pin.num;
    if (!horiz) num.setAttribute("transform", `rotate(-90 ${numX} ${numY})`);

    // label outside
    const lx = p.x + p.dx * (len + 14 * G.k), ly = p.y + p.dy * (len + 14 * G.k);
    // plate behind the label of a netted pin: its wire leaves along the label's axis and passes under it
    const lblBg = el("rect", { class: "lbl-bg" }, grp);
    const label = el("text", { x: lx, y: ly, class: "pin-label" }, grp);      // font size: --pf / --of (setScale)
    const anchor = (p.dx < 0 || p.dy > 0) ? "end" : "start";
    label.setAttribute("text-anchor", anchor);
    if (p.dy > 0) label.setAttribute("transform", `rotate(90 ${lx} ${ly})`), label.setAttribute("text-anchor", "start");
    if (p.dy < 0) label.setAttribute("transform", `rotate(-90 ${lx} ${ly})`), label.setAttribute("text-anchor", "start");
    const nameSpan = el("tspan", { class: "lbl-name" }, label);
    nameSpan.textContent = pin.short;
    const ownerSpan = el("tspan", { class: "lbl-owner", dx: 5 * G.k }, label);
    if (anchor === "end" && p.dy === 0) {
      // left side: owner before the name so the name stays next to the pin
      label.textContent = "";
      label.appendChild(ownerSpan);
      ownerSpan.setAttribute("dx", 0);
      nameSpan.setAttribute("dx", 5 * G.k);
      label.appendChild(nameSpan);
    }

    const it = { g: grp, body, tip, arrow, label, ownerSpan, pin, p, horiz, cls: "", d: "", ext: "", own: "",
                 row: null, lblBg, lx, ly, nameLen: pin.short.length, ownLen: 0, ownShow: null };
    this.items.set(pin.num, it);
    grp.addEventListener("click", (e) => this.onClick && this.onClick(this.chip, pin, e));
    grp.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && this.onClick) { e.preventDefault(); e.stopPropagation(); this.onClick(this.chip, pin, e); return; }
      const to = this.keyTarget(it, e.key);
      if (to != null) { e.preventDefault(); e.stopPropagation(); this.focusPin(to); }
    });
    grp.addEventListener("focus", (e) => this.onFocus && this.onFocus(this.chip, pin, e));
    grp.addEventListener("blur", () => this.onBlur && this.onBlur(this.chip, pin));
    grp.addEventListener("mousemove", (e) => this.onHover && this.onHover(this.chip, pin, e));
    grp.addEventListener("mouseleave", () => this.onLeave && this.onLeave(this.chip, pin));
  }

  // pin number reached from pin `it` with key `key` (null: not a navigation key). Along a side the arrow that follows the
  // counter-clockwise numbering moves to the next pin, the opposite one to the previous; PageUp/PageDown jump a side.
  keyTarget(it, key) {
    const order = this.order, i = order.indexOf(it.pin.num), n = order.length;
    if (i < 0) return null;
    const fwd = { L: "ArrowDown", B: "ArrowRight", R: "ArrowUp", T: "ArrowLeft" }[it.p.side];
    const back = { L: "ArrowUp", B: "ArrowLeft", R: "ArrowDown", T: "ArrowRight" }[it.p.side];
    if (key === fwd) return order[(i + 1) % n];
    if (key === back) return order[(i - 1 + n) % n];
    if (key === "Home") return order[0];
    if (key === "End") return order[n - 1];
    const per = this.geo.perSide;
    if (key === "PageDown") return order[Math.min(n - 1, (Math.floor(i / per) + 1) * per) % n];
    if (key === "PageUp") return order[Math.max(0, (Math.floor(i / per) - 1) * per)];
    return null;
  }

  // move the package's tab stop to pin `num` and focus it
  focusPin(num) {
    const it = this.items.get(num);
    if (!it) return;
    for (const x of this.items.values()) if (x !== it && x.g.getAttribute("tabindex") === "0") x.g.setAttribute("tabindex", "-1");
    it.g.setAttribute("tabindex", "0");
    it.g.focus();
  }

  arrowPath(it, dir) {
    // triangle centred on the pad pointing toward (in) or away from (out) the body
    const { p } = it;
    const G = this.geo;
    const c = { x: p.x + p.dx * (G.pinLen / 2), y: p.y + p.dy * (G.pinLen / 2) };
    const s = dir === "in" ? -1 : 1;
    const ux = p.dx * s, uy = p.dy * s;       // arrow direction unit
    const vx = -uy, vy = ux;                  // perpendicular
    const L = 5 * Math.max(G.k, 0.7), W = G.pinW * 0.3;
    const r = (v) => Math.round(v * 100) / 100;
    return `M${r(c.x + ux * L)},${r(c.y + uy * L)} L${r(c.x - ux * L + vx * W)},${r(c.y - uy * L + vy * W)} ` +
           `L${r(c.x - ux * L - vx * W)},${r(c.y - uy * L - vy * W)} Z`;
  }

  // rows: live pin rows of this chip; only attributes that changed are written to the DOM
  update(rows, changed = new Set()) {
    if (this._lab === false) queueMicrotask(() => this.lodKeep());     // nets / owners may have changed
    for (const r of rows) {
      const it = this.items.get(r.num);
      if (!it) continue;
      const pin = it.pin;
      it.row = r;
      const cls = pinClass(pin, r);
      const owner = r.owner || "";
      const cat = ownerCategory(pin, owner);
      const c = `pin ${cls} own-${cat}` + (pin.kind === "gpio" ? " clickable" : "") + (r.net ? " in-net" : "") +
                (changed.has(r.num) ? " flash" : "");
      if (c !== it.cls) { it.g.setAttribute("class", c + (it.keep ? " lod-keep" : "")); it.cls = c; if (r.net && it.bgFw !== this.fw) this.fitOwner(it, true); }
      const dir = r.direction;
      const d = dir === "in" || dir === "out" ? this.arrowPath(it, dir) : "";
      if (d !== it.d) { it.arrow.setAttribute("d", d); it.d = d; }
      const ext = "ext" + (r.external ? ` ext-${r.external}` : "");
      if (ext !== it.ext) { it.tip.setAttribute("class", ext); it.ext = ext; }
      const showOwner = cat === "periph" && owner !== "analog" && owner !== pin.kind;
      // owner tiers: peripheral function (data colour), GPIO (grey), disabled / power (no owner text)
      const txt = showOwner ? owner.split("/")[0] : (cat === "gpio" ? "GPIO" : "");
      const oc = "lbl-owner" + (cat === "gpio" ? " gpio" : "");
      if (it.own !== txt + oc) {
        it.ownerSpan.textContent = txt;
        it.ownerSpan.setAttribute("class", oc);
        it.own = txt + oc;
        it.ownLen = txt.length;
        this.fitOwner(it, true);
      }
    }
  }

  // show the owner only if name + owner fit the reserved label room at the current sizes (or when zoomed in)
  fitOwner(it, force = false) {
    const G = this.geo;
    const ownW = it.ownLen ? 0.62 * it.ownLen * G.font * 0.92 + 5 * G.k : 0;
    const w = 0.62 * it.nameLen * this.fw + ownW;
    const show = it.ownLen > 0 && this.ownOk && (w <= G.labelW + 4 || this.zoomedIn);
    if (show !== it.ownShow) {
      it.ownShow = show;
      it.ownerSpan.setAttribute("visibility", show ? "visible" : "hidden");
    }
    // the plate is drawn for netted pins only: size it for those (others when they join a net, see update)
    if ((force || it.bgFw !== this.fw) && it.row && it.row.net) {
      it.bgFw = this.fw;
      this.sizeLabelBg(it, 0.62 * it.nameLen * this.fw + (show ? ownW : 0));
    }
  }

  // plate behind a netted pin's label: `w` = label length (world units)
  sizeLabelBg(it, tw) {
    const f = this.fw, w = tw + 6, h = f + 3, { lx, ly, p } = it;
    let x, y, rw = w, rh = h;
    if (p.dx > 0) { x = lx - 3; y = ly - h / 2; }
    else if (p.dx < 0) { x = lx - w + 3; y = ly - h / 2; }
    else if (p.dy < 0) { x = lx - h / 2; y = ly - w + 3; rw = h; rh = w; }
    else { x = lx - h / 2; y = ly - 3; rw = h; rh = w; }
    const r = (v) => Math.round(v * 10) / 10;
    for (const [k, v] of [["x", x], ["y", y], ["width", rw], ["height", rh]]) it.lblBg.setAttribute(k, r(v));
  }

  setStatus(timeText, statusText, running, pcText = "") {
    this.timeText = timeText;
    const lens = [this.bodyTime, this.bodyStatus, this.bodyPc].map((e) => (e.textContent || "").length).join();
    if (this.bodyTime.textContent !== timeText) this.bodyTime.textContent = timeText;
    if (this.bodyStatus.textContent !== statusText) this.bodyStatus.textContent = statusText;
    const sc = "body-sub " + (running ? "run" : "dim");
    if (this.bodyStatus.getAttribute("class") !== sc) this.bodyStatus.setAttribute("class", sc);
    if (this.bodyPc.textContent !== pcText) this.bodyPc.textContent = pcText;
    if ([this.bodyTime, this.bodyStatus, this.bodyPc].map((e) => (e.textContent || "").length).join() !== lens) this.layoutBody(this._bodyScale);
  }

  setSelected(on) {
    this.root.classList.toggle("selected", !!on);
  }

  // Level of detail at `scale` screen px per world unit. Names: native size, but never below MIN_LABEL_PX while the
  // pitch has room (counter-scaled), capped by the pitch so adjacent names do not overlap; hidden only below HIDE_LABEL_PX.
  // Owners keep their native size and show when the label fits (fitOwner). Numbers hide below 4.5 px.
  // Level of detail below the name threshold: name the pins that matter: wired to a net, selected / pinned in the
  // card (setMarked), then pins owned by a peripheral, as long as their names do not collide on screen (greedy along
  // each side, MIN_LABEL_PX names need ~1.15 x that much pitch). Every other pin shows its name on hover / focus.
  setMarked(nums) { this.marked = new Set(nums || []); this._keepSig = null; if (this._lab === false) this.lodKeep(); }

  lodKeep() {
    const scale = this._scale || 1;
    const prio = (it) => {
      const r = it.row || {};
      if (this.marked && this.marked.has(it.pin.num)) return 0;
      if (r.net) return 1;
      if (it.pin.kind === "gpio" && ownerCategory(it.pin, r.owner || "") === "periph" && r.owner !== "analog") return 2;
      return 9;
    };
    const cands = [];
    for (const it of this.items.values()) { const pr = prio(it); if (pr < 9) cands.push([pr, it]); }
    const sig = cands.map(([pr, it]) => `${pr}${it.pin.num}`).join(",") + `|${Math.round(scale * 200)}`;
    if (sig === this._keepSig) return;
    this._keepSig = sig;
    cands.sort((a, b) => a[0] - b[0] || a[1].pin.num - b[1].pin.num);
    const need = (MIN_LABEL_PX * 1.15) / scale;
    const placed = { L: [], R: [], T: [], B: [] };
    const keep = new Set();
    for (const [, it] of cands) {
      const side = it.p.side, pos = side === "L" || side === "R" ? it.p.y : it.p.x;
      if (placed[side].some((q) => Math.abs(q - pos) < need)) continue;
      placed[side].push(pos);
      keep.add(it);
    }
    for (const it of this.items.values()) {
      const k = keep.has(it);
      if (k !== !!it.keep) { it.keep = k; it.g.classList.toggle("lod-keep", k); }
    }
  }

  setScale(scale) {
    const G = this.geo;
    // names may use the full pitch: mono capitals are ~0.7 em tall, so 9 px names in a 8.7 px pitch keep a 2 px gap
    const px = Math.min(Math.max(G.font * scale, MIN_LABEL_PX), G.pitch * scale * 1.04);
    const lab = px >= HIDE_LABEL_PX, num = G.numFont * scale >= 4.5;
    if (Math.abs(scale - (this._bodyScale || 0)) / scale > 0.02) this.layoutBody(scale);
    if (lab !== this._lab) { this.root.classList.toggle("lod-nolabel", !lab); this._lab = lab; this._keepSig = null; }
    this._scale = scale;
    // names too small for every pin: still name the pins that matter (lodKeep), at the readable minimum size
    this.root.style.setProperty("--pfk", `${(MIN_LABEL_PX / scale).toFixed(2)}px`);
    if (!lab) this.lodKeep();
    if (num !== this._num) { this.root.classList.toggle("lod-nonum", !num); this._num = num; }
    const fw = Math.round(Math.max(G.font, px / scale) * 20) / 20;
    const ownOk = G.font * 0.92 * scale >= OWNER_MIN_PX, zoomedIn = G.font * scale >= 11;
    if (fw === this.fw && ownOk === this.ownOk && zoomedIn === this.zoomedIn) return;
    this.fw = fw; this.ownOk = ownOk; this.zoomedIn = zoomedIn;
    this.root.style.setProperty("--pf", `${fw}px`);
    for (const it of this.items.values()) this.fitOwner(it);
  }
}
