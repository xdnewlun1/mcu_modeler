// Compact logic analyser for selected pins (pins tray, "Logic" tab; app.js mounts it). One canvas: a time axis and one
// 16 px row per traced pin. Levels are drawn with the pin encoding of the board (H = thick high trace, L = thin low
// line, X / floating = hatched band), transitions as verticals; a pin stored as `wave` summaries (fast PWM, decimated
// by chipem/log.py) is drawn as a dense band with "frequency / duty". Data: the pin's history from
// /api/events?kinds=pin&pin=<qname>&limit=0 when it is added, then the live `pin` events (onEvents).
// Mouse: wheel zooms around the pointer, drag pans, double-click fits; the cursor line reads out time and levels.
// "follow" keeps the right edge at the current virtual time while it runs (any pan / zoom stops following).
import { fmtTime, fmtFreq, fmtDuty } from "./fmt.js";

const ROW = 16, AXIS = 15, LABEL_W = 92, BAR = 20;
const MAX_TRANSITIONS = 200000;          // per pin; older ones are dropped

const keyOf = (cid, num) => `${cid}:${num}`;

export class LogicStrip {
  // container: an empty element with a fixed height; opts: {fetchJson(url) -> Promise<obj>, onChange(pins)}
  constructor(container, opts = {}) {
    this.box = container;
    this.fetchJson = opts.fetchJson || ((u) => fetch(u).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }));
    this.onChange = opts.onChange || null;
    this.pins = [];                       // [{cid, num, label, qname, short, tr: [[t, lv]], waves: [], init, seq, loading, buf}]
    this.now = 0;
    this.view = null;                     // {t0, t1} ns
    this.follow = true;
    this.cursor = null;                   // x (css px) of the cursor line
    this.build();
    this.readTheme();
    const mo = new MutationObserver(() => { this.readTheme(); this.draw(); });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    window.addEventListener("chipem-theme", () => { this.readTheme(); this.draw(); });
  }

  // ---------------------------------------------------------------- DOM
  build() {
    const b = this.box;
    b.classList.add("logic");
    b.innerHTML = `
      <div class="logic-bar" style="display:flex;align-items:center;gap:4px;height:${BAR}px;padding:0 4px;font:11px var(--mono);border-bottom:1px solid var(--line)">
        <button type="button" class="btn small ghost" data-l="fit" title="Fit all traced data (double-click the plot)">fit</button>
        <button type="button" class="btn small ghost" data-l="follow" aria-pressed="true" title="Keep the right edge at the current virtual time">follow</button>
        <button type="button" class="btn small ghost" data-l="copy" title="Copy the transitions in view as tab-separated text">copy</button>
        <button type="button" class="btn small ghost" data-l="clear" title="Remove every traced pin">clear</button>
        <span class="logic-read" style="margin-left:6px;color:var(--fg-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1 1 auto"></span>
      </div>
      <div class="logic-plot" style="position:relative;flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden">
        <canvas tabindex="0" aria-label="logic analyser: wheel zooms, drag pans, double-click fits" style="display:block;width:100%;cursor:crosshair;outline:none"></canvas>
      </div>`;
    b.style.display = "flex";
    b.style.flexDirection = "column";
    this.plot = b.querySelector(".logic-plot");
    this.cv = b.querySelector("canvas");
    this.read = b.querySelector(".logic-read");
    this.btnFollow = b.querySelector('[data-l="follow"]');
    b.querySelector(".logic-bar").addEventListener("click", (e) => {
      const a = e.target.closest("button")?.dataset.l;
      if (a === "fit") this.fit();
      else if (a === "follow") this.setFollow(!this.follow);
      else if (a === "copy") this.copy(e.target.closest("button"));
      else if (a === "clear") this.clear();
    });
    const cv = this.cv;
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      const x = this.xOf(e);
      this.zoomAt(x, Math.exp(e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)));
    }, { passive: false });
    let drag = null;
    cv.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      const v = this.span();
      drag = { x: e.clientX, v, moved: false, id: e.pointerId };
    });
    cv.addEventListener("pointermove", (e) => {
      this.cursor = this.xOf(e);
      if (drag) {
        const dx = e.clientX - drag.x;
        if (!drag.moved && Math.abs(dx) < 3) { this.draw(); return; }
        if (!drag.moved) { drag.moved = true; cv.setPointerCapture(drag.id); }
        const w = this.plotW();
        const dt = (dx / Math.max(1, w)) * (drag.v.t1 - drag.v.t0);
        this.view = { t0: drag.v.t0 - dt, t1: drag.v.t1 - dt };
        this.setFollow(false);
      }
      this.draw();
    });
    const end = () => { drag = null; };
    cv.addEventListener("pointerup", end);
    cv.addEventListener("pointercancel", end);
    cv.addEventListener("pointerleave", () => { this.cursor = null; this.draw(); });
    cv.addEventListener("dblclick", () => this.fit());
    cv.addEventListener("keydown", (e) => {
      if (e.key === "+" || e.key === "=") { e.preventDefault(); this.zoomAt(this.plotW() / 2 + LABEL_W, 1 / 1.5); }
      else if (e.key === "-" || e.key === "_") { e.preventDefault(); this.zoomAt(this.plotW() / 2 + LABEL_W, 1.5); }
      else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const v = this.span(), d = (v.t1 - v.t0) * 0.2 * (e.key === "ArrowLeft" ? -1 : 1);
        this.view = { t0: v.t0 + d, t1: v.t1 + d };
        this.setFollow(false);
        this.draw();
      } else if (e.key === "0" || e.key === "f" || e.key === "F") { e.preventDefault(); this.fit(); }
    });
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => this.draw()).observe(this.plot);
  }

  readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n, d) => (cs.getPropertyValue(n) || "").trim() || d;
    this.c = {
      bg: v("--panel", "black"), fg: v("--fg", "white"), fg2: v("--fg-2", "silver"), fg3: v("--fg-3", "gray"),
      line: v("--line", "dimgray"), h: v("--lv-h", "green"), l: v("--lv-l", "gray"), x: v("--lv-x", "orange"),
      accent: v("--accent", "dodgerblue"), mono: v("--mono", "monospace"),
    };
  }

  // ---------------------------------------------------------------- pin set
  setPins(list) {
    const want = new Map((list || []).map((p) => [keyOf(p.cid, p.num), p]));
    this.pins = this.pins.filter((p) => want.has(keyOf(p.cid, p.num)));
    for (const [k, p] of want) if (!this.pins.some((x) => keyOf(x.cid, x.num) === k)) this.add(p, false);
    this.changed();
  }

  // current levels ({"A:77": "H"}, from the pin rows): drawn for pins without any logged change
  setLevels(map) {
    this.levels = map || {};
    if (this.pins.some((p) => !p.tr.length)) this.draw();
  }

  has(cid, num) { return this.pins.some((p) => p.cid === cid && p.num === num); }

  keys() { return this.pins.map((p) => keyOf(p.cid, p.num)); }

  toggle(pin) {
    if (this.has(pin.cid, pin.num)) this.pins = this.pins.filter((p) => !(p.cid === pin.cid && p.num === pin.num));
    else this.add(pin, true);
    this.changed();
  }

  add(pin, fresh) {
    const qname = pin.qname || pin.label;
    const short = pin.short || (String(qname).includes(".") && pin.cid ? String(qname).split(".").slice(1).join(".") : String(qname));
    const p = { cid: pin.cid, num: pin.num, label: pin.label || qname, qname, short, tr: [], waves: [], init: null,
                seq: 0, loading: true, buf: [] };
    this.pins.push(p);
    this.load(p);
    if (fresh && this.pins.length === 1) this.follow = true;
  }

  async load(p) {
    try {
      const d = await this.fetchJson(`/api/events?kinds=pin&pin=${encodeURIComponent(p.qname)}&limit=0`);
      p.tr = []; p.waves = [];
      for (const e of d.events || []) this.take(p, e);
      p.seq = d.last || 0;
    } catch (err) {
      p.err = err.message;
    }
    p.loading = false;
    for (const e of p.buf) if (!(e.seq <= p.seq)) this.take(p, e);
    p.buf = [];
    this.draw();
  }

  changed() {
    if (this.onChange) this.onChange(this.pins.map((p) => ({ cid: p.cid, num: p.num, label: p.label, qname: p.qname })));
    this.draw();
  }

  clear() { this.pins = []; this.changed(); }

  // session reset: forget the data (time continues), keep the pins
  reset() {
    for (const p of this.pins) { p.tr = []; p.waves = []; p.init = null; }
    this.follow = true;
    this.draw();
  }

  // ---------------------------------------------------------------- data
  match(p, e) {
    if (e.kind !== "pin") return false;
    if (e.chip != null && e.chip !== p.cid && e.chip !== "board") return false;
    const name = String(e.pin ?? "");
    return name === p.short || name === p.qname;
  }

  take(p, e) {
    if (!this.match(p, e)) return;
    const f = e.field || "level";
    if (f === "level") {
      if (!p.tr.length && p.init == null) p.init = e.old ?? "X";
      p.tr.push([e.t_ns, e.new ?? "X"]);
      if (p.tr.length > MAX_TRANSITIONS) { p.init = p.tr[p.tr.length - MAX_TRANSITIONS - 1][1]; p.tr.splice(0, p.tr.length - MAX_TRANSITIONS); }
    } else if (f === "wave") {
      p.waves.push({ t0: e.since_ns ?? e.t_ns, t1: e.t_ns, freq: e.freq_hz, duty: e.duty, edges: e.edges, lv: e.new });
      p.tr.push([e.t_ns, e.new ?? "X"]);                // level after the summarised window
    }
  }

  onEvents(evs) {
    if (!this.pins.length) return;
    let any = false;
    for (const e of evs) {
      if (e.kind !== "pin") continue;
      for (const p of this.pins) {
        if (!this.match(p, e)) continue;
        if (p.loading) { p.buf.push(e); continue; }
        if (e.seq != null && e.seq <= p.seq) continue;
        this.take(p, e);
        any = true;
      }
    }
    if (any) this.draw();
  }

  setTime(t) {
    if (t == null || t === this.now) return;
    this.now = t;
    if (this.follow && this.pins.length) this.draw();
  }

  setFollow(on) {
    this.follow = !!on;
    this.btnFollow.setAttribute("aria-pressed", String(this.follow));
    this.btnFollow.classList.toggle("on", this.follow);
    this.draw();
  }

  // ---------------------------------------------------------------- view
  plotW() { return Math.max(10, (this.cv.clientWidth || 300) - LABEL_W); }

  xOf(e) { const r = this.cv.getBoundingClientRect(); return e.clientX - r.left; }

  extent() {
    let a = Infinity, b = -Infinity;
    for (const p of this.pins) {
      if (p.tr.length) { a = Math.min(a, p.tr[0][0]); b = Math.max(b, p.tr[p.tr.length - 1][0]); }
      for (const w of p.waves) { a = Math.min(a, w.t0); b = Math.max(b, w.t1); }
    }
    b = Math.max(b, this.now || 0);
    if (!isFinite(a)) a = Math.max(0, b - 1e6);
    return { a, b: Math.max(b, a + 1000) };
  }

  span() {
    if (this.follow || !this.view) {
      const w = this.view ? this.view.t1 - this.view.t0 : null;
      const { a, b } = this.extent();
      const width = w || Math.max(1000, b - a);
      return { t0: (this.now || b) - width, t1: this.now || b };
    }
    return this.view;
  }

  fit() {
    const { a, b } = this.extent();
    const pad = (b - a) * 0.02;
    this.view = { t0: a - pad, t1: b + pad };
    this.follow = false;
    this.setFollow(false);
  }

  zoomAt(x, f) {
    const v = this.span();
    const w = this.plotW();
    const tc = v.t0 + ((x - LABEL_W) / w) * (v.t1 - v.t0);
    const nw = Math.max(20, Math.min(1e13, (v.t1 - v.t0) * f));
    const k = (tc - v.t0) / (v.t1 - v.t0);
    this.view = { t0: tc - k * nw, t1: tc + (1 - k) * nw };
    if (this.follow && f < 1) this.view = { t0: v.t1 - nw, t1: v.t1 };    // zoom keeps the right edge while following
    else this.setFollow(false);
    this.draw();
  }

  levelAt(p, t) {
    const tr = p.tr;
    let lo = 0, hi = tr.length - 1, ans = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (tr[m][0] <= t) { ans = m; lo = m + 1; } else hi = m - 1; }
    return ans < 0 ? (p.init ?? (tr.length ? "X" : (this.levels || {})[keyOf(p.cid, p.num)] ?? "X")) : tr[ans][1];
  }

  // ---------------------------------------------------------------- drawing
  draw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.render(); });
  }

  render() {
    const cv = this.cv, c = this.c;
    const dpr = window.devicePixelRatio || 1;
    const W = this.plot.clientWidth || 300;
    const H = Math.max(this.plot.clientHeight || 60, AXIS + Math.max(1, this.pins.length) * ROW + 2);
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.height = `${H}px`;
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = c.bg;
    g.fillRect(0, 0, W, H);
    g.font = `11px ${c.mono}`;
    g.textBaseline = "middle";
    if (!this.pins.length) {
      g.fillStyle = c.fg3;
      g.fillText("no traced pins: pin card → trace (or T on a pin row)", 8, AXIS + ROW / 2);
      this.read.textContent = "";
      return;
    }
    const v = this.span(), pw = W - LABEL_W, dt = v.t1 - v.t0;
    const X = (t) => LABEL_W + ((t - v.t0) / dt) * pw;
    // axis
    g.strokeStyle = c.line; g.lineWidth = 1;
    g.beginPath(); g.moveTo(LABEL_W, AXIS - 0.5); g.lineTo(W, AXIS - 0.5); g.stroke();
    const step = niceStep(dt / Math.max(1, pw / 90));
    g.fillStyle = c.fg3;
    g.textAlign = "left";
    for (let t = Math.ceil(v.t0 / step) * step; t <= v.t1; t += step) {
      const x = Math.round(X(t)) + 0.5;
      g.strokeStyle = c.line;
      g.beginPath(); g.moveTo(x, AXIS - 4); g.lineTo(x, H); g.stroke();
      g.fillText(fmtTime(t), x + 2, AXIS / 2);
    }
    // rows
    this.pins.forEach((p, i) => {
      const y0 = AXIS + i * ROW, yh = y0 + 3.5, yl = y0 + ROW - 3.5, ym = (yh + yl) / 2;
      g.fillStyle = c.fg2; g.textAlign = "left";
      g.save(); g.beginPath(); g.rect(0, y0, LABEL_W - 4, ROW); g.clip();
      g.fillText(p.loading ? `${p.label} …` : p.err ? `${p.label} !` : p.label, 4, ym);
      g.restore();
      g.strokeStyle = c.line;
      g.beginPath(); g.moveTo(0, y0 + ROW - 0.5); g.lineTo(W, y0 + ROW - 0.5); g.stroke();
      g.save(); g.beginPath(); g.rect(LABEL_W, y0, pw, ROW); g.clip();
      // wave summaries: dense band + "f / duty"
      for (const w of p.waves) {
        if (w.t1 < v.t0 || w.t0 > v.t1) continue;
        const xa = Math.max(LABEL_W, X(w.t0)), xb = Math.min(W, X(w.t1));
        g.globalAlpha = 0.35; g.fillStyle = c.h; g.fillRect(xa, yh, Math.max(1, xb - xa), yl - yh); g.globalAlpha = 1;
        g.strokeStyle = c.h; g.strokeRect(xa + 0.5, yh + 0.5, Math.max(1, xb - xa - 1), yl - yh - 1);
        const txt = `${w.freq ? fmtFreq(w.freq) : `${w.edges} edges`}${w.duty != null ? ` / ${fmtDuty(w.duty)}` : ""}`;
        if (g.measureText(txt).width + 6 < xb - xa) { g.fillStyle = c.fg; g.fillText(txt, xa + 3, ym); }
      }
      // level segments
      const tr = p.tr;
      let lo = 0, hi = tr.length;                       // first transition after t0
      while (lo < hi) { const m = (lo + hi) >> 1; if (tr[m][0] <= v.t0) lo = m + 1; else hi = m; }
      let lv = lo > 0 ? tr[lo - 1][1] : (p.init ?? (tr.length ? "X" : (this.levels || {})[keyOf(p.cid, p.num)] ?? null));
      if (lv == null) { g.restore(); return; }
      let x = LABEL_W, lastPx = -1, busy = 0;
      const seg = (xa, xb, l) => {
        if (xb <= xa) return;
        if (l === "H") { g.strokeStyle = c.h; g.lineWidth = 2; g.beginPath(); g.moveTo(xa, yh); g.lineTo(xb, yh); g.stroke(); }
        else if (l === "L") { g.strokeStyle = c.l; g.lineWidth = 1; g.beginPath(); g.moveTo(xa, yl - 0.5); g.lineTo(xb, yl - 0.5); g.stroke(); }
        else {
          g.strokeStyle = c.x; g.lineWidth = 1;
          g.beginPath();
          for (let hx = Math.ceil(xa / 4) * 4; hx < xb; hx += 4) { g.moveTo(hx, yl); g.lineTo(Math.min(xb, hx + 4), yh); }
          g.stroke();
          g.beginPath(); g.moveTo(xa, ym); g.lineTo(xb, ym); g.stroke();
        }
        g.lineWidth = 1;
      };
      for (let k = lo; k < tr.length && tr[k][0] <= v.t1; k++) {
        const xt = X(tr[k][0]);
        const px = Math.floor(xt);
        if (px === lastPx) { busy++; lv = tr[k][1]; continue; }          // several edges in one pixel: dense
        if (busy) { g.fillStyle = c.fg3; g.fillRect(lastPx, yh, 1, yl - yh); busy = 0; }
        seg(x, xt, lv);
        const nl = tr[k][1];
        if (nl !== lv) { g.strokeStyle = c.fg2; g.beginPath(); g.moveTo(Math.round(xt) + 0.5, yh); g.lineTo(Math.round(xt) + 0.5, yl); g.stroke(); }
        lv = nl; x = xt; lastPx = px;
      }
      if (busy) { g.fillStyle = c.fg3; g.fillRect(lastPx, yh, 1, yl - yh); }
      const end = Math.min(W, X(Math.max(v.t0, this.now || v.t1)));
      seg(x, Math.max(x, end), lv);
      g.restore();
    });
    // cursor
    if (this.cursor != null && this.cursor >= LABEL_W) {
      const t = v.t0 + ((this.cursor - LABEL_W) / pw) * dt;
      g.strokeStyle = c.accent;
      g.beginPath(); g.moveTo(Math.round(this.cursor) + 0.5, 0); g.lineTo(Math.round(this.cursor) + 0.5, H); g.stroke();
      this.read.textContent = `${fmtTime(t)}  ` + this.pins.map((p) => `${p.label}=${this.levelAt(p, t)}`).join("  ");
    } else {
      this.read.textContent = `${fmtTime(v.t0)} … ${fmtTime(v.t1)}  (${fmtTime(dt)} wide)${this.follow ? " · following" : ""}`;
    }
  }

  copy(btn) {
    const v = this.span();
    const lines = ["time_ns\ttime\tpin\tlevel"];
    for (const p of this.pins) {
      lines.push(`${Math.round(v.t0)}\t${fmtTime(v.t0)}\t${p.label}\t${this.levelAt(p, v.t0)}`);
      for (const [t, l] of p.tr) if (t > v.t0 && t <= v.t1) lines.push(`${Math.round(t)}\t${fmtTime(t)}\t${p.label}\t${l}`);
      for (const w of p.waves) if (w.t1 >= v.t0 && w.t0 <= v.t1) {
        lines.push(`${Math.round(w.t0)}\t${fmtTime(w.t0)}\t${p.label}\twave ${w.edges} edges${w.freq ? ` ${fmtFreq(w.freq)}` : ""}${w.duty != null ? ` ${fmtDuty(w.duty)}` : ""} until ${fmtTime(w.t1)}`);
      }
    }
    const text = lines.join("\n") + "\n";
    const done = (ok) => { if (btn) { btn.textContent = ok ? `copied ${lines.length - 1}` : "copy failed"; setTimeout(() => { btn.textContent = "copy"; }, 1200); } };
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject(new Error("no clipboard")))
      .then(() => done(true), () => {
        const ta = document.createElement("textarea");
        ta.value = text; ta.style.cssText = "position:fixed;left:-9999px"; document.body.appendChild(ta); ta.select();
        let ok = false; try { ok = document.execCommand("copy"); } catch { ok = false; }
        ta.remove(); done(ok);
      });
  }
}

// 1, 2, 5 x 10^n ns
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-3))));
  const m = raw / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}
