// Debug tab (pins tray): source / disassembly view that follows the PC of the selected chip, gutter breakpoints,
// breakpoint + watchpoint list, step into / over / out, run to cursor, call stack, CPU registers (changed values
// highlighted, editable while paused) and a memory view. app.js mounts it (initDebug) and forwards state frames
// (onState), the selected chip (setChip), tab visibility (show) and function keys (onKey). Server side:
// chipem/web/debugapi.py (/api/debug/*); every value comes from the emulator's Worker thread.
//
// Keys (any tab; F5 / F9 / F4 only while this tab is shown): F5 continue, Shift+F5 pause, F10 step over (next line;
// one instruction in asm mode), F11 step into, Shift+F11 step out, F4 run to cursor, F9 breakpoint at the cursor row.
import { esc, hex32 } from "./fmt.js";

const MODES = ["mixed", "asm", "source"];

// The pins tray is short by default; the debugger needs room: grow it (not persisted) to ~60 % of the main column.
export function growTray() {
  const shell = document.querySelector(".shell"), col = document.querySelector(".maincol"), pane = document.getElementById("panePins");
  if (!shell || !col || !pane || pane.classList.contains("collapsed")) return;
  const max = col.getBoundingClientRect().height - 140;
  const want = Math.min(max, Math.max(420, col.getBoundingClientRect().height * 0.6));
  if (pane.getBoundingClientRect().height < want - 4) shell.style.setProperty("--tray-h", `${Math.round(want)}px`);
}

export class DebugPane {
  constructor(box, opts) {
    this.box = box;
    this.api = opts.api;
    this.toast = opts.toast || (() => {});
    this.toastErr = opts.toastErr || ((m) => console.error(m));
    this.chip = opts.chip || null;
    this.isBoard = !!opts.isBoard;
    this.visible = false;
    this.st = null;               // last /api/debug/state
    this.prevRegs = new Map();    // values at the previous stop (changed highlighting)
    this.curRegs = new Map();
    this.mode = this.pref("mode", "mixed");
    this.follow = true;
    this.viewAddr = null;         // address shown when not following the PC
    this.cursor = null;           // {addr} | {file, line}
    this.memAddr = this.pref("mem", "sp");
    this.memFollow = this.pref("memFollow", "");
    this.lastKey = "";
    this.busy = false;
    this.again = false;
    this.lastRunRefresh = 0;
    this.build();
  }

  pref(k, d) { try { return localStorage.getItem(`chipem.dbg.${k}`) ?? d; } catch { return d; } }
  savePref(k, v) { try { localStorage.setItem(`chipem.dbg.${k}`, v); } catch { /* private mode */ } }
  q(sel) { return this.box.querySelector(sel); }
  chipQ() { return this.isBoard && this.chip ? `chip=${encodeURIComponent(this.chip)}` : ""; }
  chipB() { return this.isBoard && this.chip ? { chip: this.chip } : {}; }

  // ---------------------------------------------------------------- DOM
  build() {
    const b = this.box;
    b.classList.add("dbg");
    b.innerHTML = `
      <div class="dbg-bar" role="toolbar" aria-label="Debugger">
        <button type="button" class="btn small" data-a="cont" title="Continue until a breakpoint / watchpoint (F5)">continue</button>
        <button type="button" class="btn small" data-a="pause" title="Pause (Shift+F5)">pause</button>
        <span class="sep"></span>
        <button type="button" class="btn small" data-a="step" title="Step into: next source line, entering calls; one instruction in asm mode (F11)">into</button>
        <button type="button" class="btn small" data-a="next" title="Step over: next source line, calls run; one instruction in asm mode (F10)">over</button>
        <button type="button" class="btn small" data-a="finish" title="Step out: run until the current function returns (Shift+F11)">out</button>
        <button type="button" class="btn small" data-a="stepi" title="One instruction, into calls">stepi</button>
        <button type="button" class="btn small" data-a="until" title="Run to the cursor row (F4)">to cursor</button>
        <span class="sep"></span>
        <select data-a="mode" aria-label="View" title="mixed: source lines above their instructions · asm: instructions · source: the source file">
          ${MODES.map((m) => `<option value="${m}">${m}</option>`).join("")}</select>
        <label class="toggle" title="Scroll the view to the PC at every stop"><input type="checkbox" data-a="follow" checked> follow pc</label>
        <input class="dbg-goto" type="text" spellcheck="false" placeholder="go to: main, 0x4ac, file.c:12" aria-label="Show location">
        <span class="dbg-msg" aria-live="polite"></span>
      </div>
      <div class="dbg-main">
        <div class="dbg-code" tabindex="0" aria-label="Code: click a row to place the cursor, click the gutter to toggle a breakpoint"></div>
        <div class="split-dbg" aria-hidden="true"></div>
        <div class="dbg-side">
          <section class="dbg-sec dbg-regs-sec">
            <div class="dbg-h"><b>registers</b><span class="dbg-hint">double-click a value to edit (paused)</span>
              <button type="button" class="btn small ghost" data-a="regcopy" title="Copy as tab-separated text">copy</button></div>
            <div class="dbg-scroll"><table class="dbg-regs"></table></div>
          </section>
          <section class="dbg-sec dbg-stack-sec">
            <div class="dbg-h"><b>call stack</b><span class="dbg-hint dbg-stack-how"></span></div>
            <div class="dbg-scroll"><table class="dbg-stack"></table></div>
          </section>
          <section class="dbg-sec dbg-bps-sec">
            <div class="dbg-h"><b>breakpoints</b>
              <form class="dbg-add" title="Breakpoint: main, *0x4ac, file.c:12 [if r0 == 3] · watchpoint: w|r|a TARGET (0x20000000:4, symbol, USART1.SR)">
                <input type="text" spellcheck="false" placeholder="main · file.c:12 if r0==3 · w USART1.SR" aria-label="Add breakpoint or watchpoint">
                <button class="btn small" type="submit">add</button></form></div>
            <div class="dbg-scroll"><table class="dbg-bps"></table></div>
          </section>
        </div>
      </div>
      <div class="dbg-mem">
        <div class="dbg-h"><b>memory</b>
          <input class="dbg-mem-addr" type="text" spellcheck="false" aria-label="Memory address" title="Address or expression: 0x20000000, sp, buffer, sp+16">
          <select class="dbg-mem-follow" aria-label="Follow register" title="Follow a register: the address is re-read from it at every stop"><option value="">follow: —</option></select>
          <button type="button" class="btn small ghost" data-a="memgo">show</button>
          <span class="dbg-hint">MMIO is peeked without side effects; <span class="nx">dim</span> = stored value (a real read would have side effects)</span></div>
        <pre class="dbg-dump term"></pre>
      </div>`;
    this.code = this.q(".dbg-code");
    this.q('[data-a="mode"]').value = this.mode;
    this.q(".dbg-mem-addr").value = this.memAddr;
    this.q(".dbg-bar").addEventListener("click", (e) => {
      const a = e.target.closest("button")?.dataset.a;
      if (a) this.action(a);
    });
    this.q('[data-a="mode"]').addEventListener("change", (e) => { this.mode = e.target.value; this.savePref("mode", this.mode); this.refreshCode(); });
    this.q('[data-a="follow"]').addEventListener("change", (e) => { this.follow = e.target.checked; if (this.follow) { this.viewAddr = null; this.refreshCode(); } });
    this.q(".dbg-goto").addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const v = e.target.value.trim();
      if (!v) return;
      this.follow = false; this.q('[data-a="follow"]').checked = false;
      this.viewAddr = v; this.refreshCode();
    });
    this.code.addEventListener("click", (e) => this.codeClick(e));
    this.code.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); this.moveCursor(e.key === "ArrowDown" ? 1 : -1); }
    });
    this.q(".dbg-add").addEventListener("submit", (e) => { e.preventDefault(); this.addFromInput(); });
    this.q(".dbg-bps").addEventListener("click", (e) => this.bpClick(e));
    this.q(".dbg-bps").addEventListener("change", (e) => {
      const tr = e.target.closest("tr[data-id]");
      if (tr && e.target.type === "checkbox") this.post("/api/debug/bp", { id: +tr.dataset.id, enabled: e.target.checked });
    });
    this.q(".dbg-regs").addEventListener("dblclick", (e) => this.editReg(e));
    this.q('[data-a="regcopy"]').addEventListener("click", () => this.copyRegs());
    this.q(".dbg-stack").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-pc]");
      if (!tr) return;
      this.follow = false; this.q('[data-a="follow"]').checked = false;
      this.viewAddr = `0x${(+tr.dataset.pc).toString(16)}`; this.refreshCode();
    });
    this.q('[data-a="memgo"]').addEventListener("click", () => this.memGo());
    this.q(".dbg-mem-addr").addEventListener("keydown", (e) => { if (e.key === "Enter") this.memGo(); });
    this.q(".dbg-mem-follow").addEventListener("change", (e) => {
      this.memFollow = e.target.value; this.savePref("memFollow", this.memFollow); this.refreshMem();
    });
  }

  // ---------------------------------------------------------------- app.js hooks
  show(on) {
    this.visible = on;
    if (on) { growTray(); this.refresh(true); }
  }

  setChip(cid) {
    if (cid === this.chip) return;
    this.chip = cid;
    this.prevRegs = new Map(); this.curRegs = new Map();
    this.viewAddr = null;
    if (this.visible) this.refresh(true);
  }

  // every state frame: refresh when the selected chip stopped somewhere new; about once a second while running
  onState(st) {
    if (!this.visible || !st) return;
    const cs = st.chips && this.chip ? st.chips[this.chip] || st : st;
    const key = `${st.running}|${st.last_stop}|${cs.pc}|${cs.insns}`;
    if (st.running) {
      const now = performance.now();
      if (now - this.lastRunRefresh > 1000) { this.lastRunRefresh = now; this.refresh(false); }
      this.lastKey = key;
      return;
    }
    if (key !== this.lastKey) { this.lastKey = key; this.refresh(true); }
  }

  // F-keys; returns true when handled
  onKey(e) {
    const k = e.key;
    if (!/^F(4|5|9|10|11)$/.test(k)) return false;
    if ((k === "F5" || k === "F4" || k === "F9") && !this.visible) return false;
    e.preventDefault();
    if (k === "F5") this.action(e.shiftKey ? "pause" : "cont");
    else if (k === "F10") this.action("next");
    else if (k === "F11") this.action(e.shiftKey ? "finish" : "step");
    else if (k === "F4") this.action("until");
    else if (k === "F9") this.toggleAtCursor();
    return true;
  }

  // ---------------------------------------------------------------- actions
  async post(path, body) {
    try { return await this.api(path, { ...this.chipB(), ...body }); } catch (e) { this.toastErr(e.message); return null; }
  }

  async action(a) {
    if (a === "cont") { await this.post("/api/run", {}); return; }
    if (a === "pause") { await this.post("/api/pause", {}); return; }
    if (a === "until") {
      const loc = this.cursorLoc();
      if (!loc) { this.toastErr("place the cursor on a row first (click it)"); return; }
      await this.step("until", loc);
      return;
    }
    if (a === "step" || a === "next") {
      // asm view: instruction steps (stepi / nexti); source views: line steps
      const k = this.mode === "asm" || !this.st?.has_dwarf ? (a === "step" ? "stepi" : "nexti") : a;
      await this.step(k);
      return;
    }
    if (a === "finish" || a === "stepi") await this.step(a);
  }

  async step(kind, location) {
    const r = await this.post("/api/debug/step", { kind, ...(location ? { location } : {}) });
    if (r && r.done) this.refresh(true);
  }

  cursorLoc() {
    const c = this.cursor;
    if (!c) return null;
    return c.addr != null ? `*0x${c.addr.toString(16)}` : `${c.file}:${c.line}`;
  }

  async toggleAtCursor() {
    const c = this.cursor;
    if (!c) return;
    if (c.addr != null) await this.post("/api/debug/toggle", { addr: c.addr });
    else await this.toggleLine(c.file, c.line, c.bp);
    this.refresh(false);
  }

  async toggleLine(file, line, ids) {
    if (ids && ids.length) { for (const id of ids) await this.post("/api/debug/bp", { id, delete: true }); }
    else await this.post("/api/debug/break", { location: `${file}:${line}` });
  }

  async addFromInput() {
    const inp = this.q(".dbg-add input");
    const t = inp.value.trim();
    if (!t) return;
    const m = /^(w|r|a|rw|watch|rwatch|awatch)\s+(.+)$/i.exec(t);
    let r;
    if (m) {
      const acc = { w: "w", watch: "w", r: "r", rwatch: "r", a: "rw", rw: "rw", awatch: "rw" }[m[1].toLowerCase()];
      const mm = /^(.*?)\s+if\s+(.+)$/.exec(m[2]);
      r = await this.post("/api/debug/watch", { target: mm ? mm[1] : m[2], access: acc, ...(mm ? { cond: mm[2] } : {}) });
    } else {
      const mm = /^(.*?)\s+if\s+(.+)$/.exec(t);
      const temp = /^t(break)?\s+/.test(t);
      const loc = (mm ? mm[1] : t).replace(/^t(break)?\s+/, "");
      r = await this.post("/api/debug/break", { location: loc, temp, ...(mm ? { cond: mm[2] } : {}) });
    }
    if (r) { inp.value = ""; this.refresh(false); }
  }

  async bpClick(e) {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const id = +tr.dataset.id;
    const a = e.target.closest("[data-b]")?.dataset.b;
    if (a === "del") { await this.post("/api/debug/bp", { id, delete: true }); this.refresh(false); }
    else if (a === "cond") {
      const cur = tr.dataset.cond || "";
      const v = prompt(`Condition for breakpoint ${id} (empty: none). e.g. r0 == 3, counter > 10, mem32[0x20000000] & 1`, cur);
      if (v !== null) { await this.post("/api/debug/bp", { id, cond: v }); this.refresh(false); }
    } else if (a === "go") {
      this.follow = false; this.q('[data-a="follow"]').checked = false;
      this.viewAddr = `0x${(+tr.dataset.addr).toString(16)}`; this.refreshCode();
    }
  }

  codeClick(e) {
    const row = e.target.closest(".r");
    if (!row) return;
    const gutter = e.target.closest(".g");
    if (row.dataset.addr != null) {
      const addr = +row.dataset.addr;
      if (gutter) { this.post("/api/debug/toggle", { addr }).then(() => this.refresh(false)); return; }
      this.setCursor({ addr }, row);
    } else if (row.dataset.line != null) {
      const file = row.dataset.file, line = +row.dataset.line;
      const ids = (row.dataset.bp || "").split(",").filter(Boolean).map(Number);
      if (gutter) {
        if (row.dataset.code !== "1" && !ids.length) { this.toastErr(`no code on line ${line}`); return; }
        this.toggleLine(file, line, ids).then(() => this.refresh(false));
        return;
      }
      this.setCursor({ file, line, bp: ids }, row);
    }
  }

  setCursor(c, row) {
    this.cursor = c;
    for (const r of this.code.querySelectorAll(".r.cur")) r.classList.remove("cur");
    row?.classList.add("cur");
  }

  moveCursor(d) {
    const rows = [...this.code.querySelectorAll(".r")];
    let i = rows.findIndex((r) => r.classList.contains("cur"));
    i = Math.max(0, Math.min(rows.length - 1, (i < 0 ? rows.findIndex((r) => r.classList.contains("pc")) : i) + d));
    const r = rows[i];
    if (!r) return;
    if (r.dataset.addr != null) this.setCursor({ addr: +r.dataset.addr }, r);
    else this.setCursor({ file: r.dataset.file, line: +r.dataset.line, bp: (r.dataset.bp || "").split(",").filter(Boolean).map(Number) }, r);
    r.scrollIntoView({ block: "nearest" });
  }

  // ---------------------------------------------------------------- refresh
  async refresh(full) {
    if (this.busy) { this.again = true; return; }
    this.busy = true;
    try {
      const st = await this.api(`/api/debug/state?${this.chipQ()}`);
      const newStop = !this.st || st.stops !== this.st.stops || st.pc !== this.st.pc;
      if (newStop && !st.running) { this.prevRegs = this.curRegs; }
      this.curRegs = new Map(st.regs);
      this.st = st;
      this.renderRegs();
      this.renderStack();
      this.renderBps();
      this.renderMsg();
      this.fillFollow();
      await this.refreshCode();
      await this.refreshMem();
    } catch (e) {
      this.q(".dbg-msg").textContent = `debugger: ${e.message}`;
    } finally {
      this.busy = false;
      if (this.again) { this.again = false; this.refresh(full); }
    }
  }

  async refreshCode() {
    if (!this.st) return;
    const addr = this.follow || this.viewAddr == null ? null : this.viewAddr;
    let r;
    try {
      r = await this.api(`/api/debug/listing?${this.chipQ()}&mode=${this.mode}&before=14&after=48` +
                         (addr ? `&addr=${encodeURIComponent(addr)}` : ""));
    } catch (e) { this.code.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    this.renderCode(r);
  }

  renderCode(r) {
    const c28 = this.st?.arch === "c28x";
    const fa = (a) => c28 ? `0x${a.toString(16).toUpperCase().padStart(6, "0")}` : hex32(a);
    const out = [];
    const head = r.mode === "source" ? (r.path || r.file || "") : (r.func ? `${r.func}` : "");
    if (r.mode !== this.mode && this.mode === "source") out.push(`<div class="note">no source for this address: ${r.has_dwarf ? "source file not found (--source-dir)" : "no line info (symbols + assembly)"}</div>`);
    if (head) out.push(`<div class="note">${esc(head)}</div>`);
    for (const row of r.rows) {
      if (row.kind === "src") {
        const bp = (row.bp || []).join(",");
        const cls = `r src${row.pc ? " pc" : ""}${bp ? " bp" : ""}${row.code === false ? " nocode" : ""}`;
        const cur = this.cursor && this.cursor.file === row.file && this.cursor.line === row.line ? " cur" : "";
        out.push(`<div class="${cls}${cur}" data-file="${esc(row.file)}" data-line="${row.line}" data-code="${row.code === false ? 0 : 1}" data-bp="${bp}">` +
                 `<span class="g">${bp ? "●" : ""}${row.pc ? "▶" : ""}</span><span class="ln">${row.base ? esc(row.base) + ":" : ""}${row.line}</span><span class="tx">${esc(row.text)}</span></div>`);
      } else {
        const bp = (row.bp || []).length;
        const cls = `r asm${row.pc ? " pc" : ""}${bp ? " bp" : ""}`;
        const cur = this.cursor && this.cursor.addr === row.addr ? " cur" : "";
        if (row.sym) out.push(`<div class="sym">${esc(row.sym)}:</div>`);
        out.push(`<div class="${cls}${cur}" data-addr="${row.addr}"><span class="g">${bp ? "●" : ""}${row.pc ? "▶" : ""}</span>` +
                 `<span class="ad">${fa(row.addr)}</span><span class="by">${esc(row.bytes)}</span><span class="tx">${esc(row.text)}</span></div>`);
      }
    }
    this.code.innerHTML = out.join("") || `<div class="empty">no code</div>`;
    const pc = this.code.querySelector(".r.pc") || this.code.querySelector(".r.cur");
    if (pc && (this.follow || !this.viewAddr)) pc.scrollIntoView({ block: "center" });
    else if (!this.follow) this.code.querySelector(".r")?.scrollIntoView({ block: "nearest" });
  }

  renderMsg() {
    const st = this.st, m = this.q(".dbg-msg");
    const ls = st.last_stop;
    let t = st.running ? "running…" : "";
    if (!st.running && ls) {
      t = ls.msg || `${(ls.reason || "").toLowerCase()} at ${ls.where || ""}`;
      if (ls.chip && this.isBoard) t = `chip ${ls.chip}: ${t}`;
      if (ls.skew_ns) t += `  (board skew ${Math.round(ls.skew_ns)} ns)`;
    }
    if (!st.running && !t) t = `stopped at ${st.func}`;
    m.textContent = t;
    m.title = t;
    m.classList.toggle("hit", !st.running && !!(ls && ls.bp && ls.bp.length));
  }

  renderRegs() {
    const st = this.st;
    const w = st.arch === "c28x" ? 8 : 8;
    const rows = st.regs.map(([n, v]) => {
      const prev = this.prevRegs.get(n);
      const chg = prev !== undefined && prev !== v ? " chg" : "";
      const hx = (v >>> 0).toString(16).toUpperCase().padStart(w, "0");
      let extra = "";
      if (n === "xpsr") extra = ["N", "Z", "C", "V", "Q"].map((f, i) => (v >>> (31 - i)) & 1 ? f : "-").join("") + ` ipsr ${v & 0x1ff}`;
      else if (/^s\d+$/.test(n)) { const f = new Float32Array(new Uint32Array([v >>> 0]).buffer)[0]; extra = `${+f.toPrecision(7)}`; }
      else if (v > 9 && v < 100000) extra = String(v);
      return `<tr data-r="${esc(n)}" class="${chg}"><th>${esc(n)}</th><td class="v" title="double-click to edit (paused)">0x${hx}</td><td class="x">${esc(extra)}</td></tr>`;
    });
    this.q(".dbg-regs").innerHTML = rows.join("");
  }

  renderStack() {
    const st = this.st;
    const c28 = st.arch === "c28x";
    const rows = (st.stack || []).map((f) => `<tr data-pc="${f.pc}" title="${esc(f.how === "scan" ? "found by a stack scan (a guess)" : f.how === "exception" ? "hardware exception frame" : f.how)}">` +
      `<td class="n">#${f.level}</td><td class="ad">${c28 ? "0x" + f.pc.toString(16).toUpperCase() : hex32(f.pc)}</td><td>${esc(f.desc || f.func)}</td><td class="how">${f.how === "pc" || f.how === "cfi" ? "" : esc(f.how)}</td></tr>`);
    this.q(".dbg-stack").innerHTML = rows.join("");
    const hows = new Set((st.stack || []).map((f) => f.how));
    this.q(".dbg-stack-how").textContent = hows.has("cfi") ? "DWARF CFI" : hows.has("scan") ? "stack scan (guess)" : "";
  }

  renderBps() {
    const bps = this.st.bps || [];
    const c28 = this.st.arch === "c28x";
    const fa = (a) => c28 ? "0x" + a.toString(16).toUpperCase() : hex32(a);
    this.q(".dbg-bps").innerHTML = bps.length ? bps.map((b) => {
      const typ = b.kind === "watch" ? ({ w: "watch", r: "rwatch", rw: "awatch" }[b.access]) : (b.temp ? "tbreak" : "break");
      return `<tr data-id="${b.id}" data-addr="${b.addr}" data-cond="${esc(b.cond || "")}">` +
        `<td><input type="checkbox" ${b.enabled ? "checked" : ""} aria-label="enabled"></td><td class="n">${b.id}</td><td>${typ}</td>` +
        `<td class="ad" data-b="go" title="show in the code view">${this.isBoard ? esc(b.chip) + ":" : ""}${fa(b.addr)}</td>` +
        `<td class="wh" title="${esc(b.location)}">${esc(b.label || b.location)}${b.kind === "watch" ? ` [${b.size}]` : ""}</td>` +
        `<td class="n" title="hits">${b.hits}</td>` +
        `<td class="cd" data-b="cond" title="click: edit the condition">${b.cond ? "if " + esc(b.cond) : "<span class='dim'>cond…</span>"}</td>` +
        `<td><button type="button" class="btn small ghost" data-b="del" title="delete">×</button></td></tr>`;
    }).join("") : `<tr><td class="dim">none — click the gutter, F9, or type above</td></tr>`;
  }

  fillFollow() {
    const sel = this.q(".dbg-mem-follow");
    const names = (this.st.regs || []).map(([n]) => n).filter((n) => !/^s\d+$|^r\dh$/.test(n));
    const sig = names.join(",");
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    sel.innerHTML = `<option value="">follow: —</option>` + names.map((n) => `<option value="${esc(n)}">follow: ${esc(n)}</option>`).join("");
    sel.value = names.includes(this.memFollow) ? this.memFollow : "";
  }

  memGo() {
    this.memAddr = this.q(".dbg-mem-addr").value.trim() || "sp";
    this.savePref("mem", this.memAddr);
    this.memFollow = ""; this.q(".dbg-mem-follow").value = "";
    this.refreshMem();
  }

  async refreshMem() {
    const expr = this.memFollow || this.memAddr;
    if (!expr) return;
    const pre = this.q(".dbg-dump");
    let r;
    try { r = await this.api(`/api/debug/mem?${this.chipQ()}&addr=${encodeURIComponent(expr)}&n=${this.st?.unit_bytes === 2 ? 128 : 256}`); }
    catch (e) { pre.textContent = e.message; return; }
    const ub = r.unit_bytes || 1;
    const bytes = r.hex.match(/../g) || [];
    const nx = new Set();
    for (const u of r.inexact || []) for (let k = 0; k < ub; k++) nx.add(u * ub + k);
    const lines = [];
    const prevKey = `${this.chip}|${expr}`;
    const prev = this._memPrev && this._memPrev.key === prevKey && this._memPrev.addr === r.addr ? this._memPrev.bytes : null;
    for (let i = 0; i < bytes.length; i += 16) {
      const a = r.addr + i / ub;
      let hx = "", asc = "";
      for (let k = i; k < Math.min(i + 16, bytes.length); k++) {
        const b = parseInt(bytes[k], 16);
        const cls = (nx.has(k) ? "nx" : "") + (prev && prev[k] !== undefined && prev[k] !== bytes[k] ? " chg" : "");
        hx += (cls ? `<span class="${cls.trim()}">${bytes[k]}</span>` : bytes[k]) + (k % 16 === 7 ? "  " : " ");
        asc += b >= 32 && b < 127 ? esc(String.fromCharCode(b)) : ".";
      }
      const fa = ub === 2 ? a.toString(16).toUpperCase().padStart(6, "0") : a.toString(16).toUpperCase().padStart(8, "0");
      lines.push(`${fa}  ${hx.padEnd(49)} ${asc}`);
    }
    if (!this.st?.running) this._memPrev = { key: prevKey, addr: r.addr, bytes };
    pre.innerHTML = lines.join("\n");
  }

  async editReg(e) {
    const tr = e.target.closest("tr[data-r]");
    if (!tr || !e.target.closest("td.v")) return;
    if (this.st?.running) { this.toastErr("pause first to edit registers"); return; }
    const td = tr.querySelector("td.v");
    const old = td.textContent;
    td.innerHTML = `<input type="text" spellcheck="false" value="${esc(old)}" aria-label="new value">`;
    const inp = td.querySelector("input");
    inp.focus(); inp.select();
    const done = async (ok) => {
      if (!td.contains(inp)) return;
      if (ok) {
        const r = await this.post("/api/debug/reg", { name: tr.dataset.r, value: inp.value.trim() });
        if (r) { this.refresh(false); return; }
      }
      td.textContent = old;
    };
    inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") done(true); else if (ev.key === "Escape") { ev.stopPropagation(); done(false); } });
    inp.addEventListener("blur", () => done(false));
  }

  copyRegs() {
    const t = (this.st?.regs || []).map(([n, v]) => `${n}\t0x${(v >>> 0).toString(16).toUpperCase()}`).join("\n");
    navigator.clipboard?.writeText(t).then(() => this.toast(`copied ${this.st.regs.length} registers`), () => this.toastErr("clipboard not available"));
  }
}
