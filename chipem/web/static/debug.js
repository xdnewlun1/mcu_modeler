// Debug tab (pins tray): source / disassembly view that follows the PC of the selected chip, gutter breakpoints,
// breakpoint + watchpoint list, step into / over / out, run to cursor, call stack, CPU registers (changed values
// highlighted, editable while paused) and a memory view. app.js mounts it (initDebug) and forwards state frames
// (onState), the selected chip (setChip), tab visibility (show) and function keys (onKey). Server side:
// chipem/web/debugapi.py (/api/debug/*); every value comes from the emulator's Worker thread.
//
// Keys (any tab; F5 / F9 / F4 only while this tab is shown): F5 continue, Shift+F5 pause, F10 step over (next line;
// one instruction in asm mode), F11 step into, Shift+F11 step out, F4 run to cursor, F9 breakpoint at the cursor row.
import { esc, fmtReg } from "./fmt.js";

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
    this.regMeta = opts.regMeta || (() => ({}));
    this.sideTab = this.pref("side", "regs");     // regs | locals | watch | globals
    this.frame = 0;                               // call stack frame whose locals are shown
    this.watches = [];                            // watch expressions of this chip (localStorage per chip)
    this.globals = null;                          // /api/debug/globals of this chip (fetched on demand)
    this.openVars = new Set();                    // expanded struct / array paths in locals and watch
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
    this.loadWatches();
  }

  pref(k, d) { try { return localStorage.getItem(`chipem.dbg.${k}`) ?? d; } catch { return d; } }
  savePref(k, v) { try { localStorage.setItem(`chipem.dbg.${k}`, v); } catch { /* private mode */ } }
  q(sel) { return this.box.querySelector(sel); }
  chipQ() { return this.isBoard && this.chip ? `chip=${encodeURIComponent(this.chip)}` : ""; }
  chipB() { return this.isBoard && this.chip ? { chip: this.chip } : {}; }
  // a code address, as wide as the chip's PC (register metadata from the server: 32-bit ARM, 22-bit C28x, 16-bit 8051)
  fa(a) { return fmtReg(a, { bits: this.regMeta(this.chip).pc?.bits ?? 32, fmt: "hex" }); }

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
            <div class="dbg-h dbg-tabs" role="tablist" aria-label="Debugger data">
              <button type="button" role="tab" class="dtab" data-t="regs" title="CPU registers: double-click a value to edit (paused)">registers</button>
              <button type="button" role="tab" class="dtab" data-t="locals" title="Arguments and local variables of the selected call stack frame (DWARF)">locals</button>
              <button type="button" role="tab" class="dtab" data-t="watch" title="Watch expressions, re-evaluated at every stop: symbol, a.b, p->x, a[3], *0x20000000, PERIPH.REG.FIELD">watch</button>
              <button type="button" role="tab" class="dtab" data-t="globals" title="Global and static variables (DWARF, else the symbol table): click one to watch it">globals</button>
              <span class="dbg-hint dbg-tab-hint"></span>
              <button type="button" class="btn small ghost" data-a="regcopy" title="Copy the shown table as tab-separated text">copy</button></div>
            <div class="dbg-scroll" data-p="regs"><table class="dbg-regs"></table></div>
            <div class="dbg-scroll" data-p="locals" hidden><table class="dbg-vars dbg-locals"></table></div>
            <div class="dbg-scroll dbg-watch-p" data-p="watch" hidden>
              <form class="dbg-watch-add"><input type="text" spellcheck="false" list="dbgGlobalsList" placeholder="add: counter · buf[2] · p->x · *0x20000000 · USART1.SR.TXE" aria-label="Add watch expression"><button class="btn small" type="submit">add</button></form>
              <datalist id="dbgGlobalsList"></datalist>
              <table class="dbg-vars dbg-watch"></table></div>
            <div class="dbg-scroll dbg-glob-p" data-p="globals" hidden>
              <input type="search" class="dbg-glob-filter pane-filter" spellcheck="false" placeholder="filter globals  /re/" aria-label="Filter globals">
              <table class="dbg-vars dbg-globals"></table></div>
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
        <div class="dbg-h"><button type="button" class="pane-tog dbg-mem-tog" aria-expanded="true" title="collapse / expand the memory view"></button><b>memory</b>
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
      this.viewAddr = v; this.refreshCode();            // shown until the next stop (follow pc) or the next go to
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
    // a call stack row selects that frame: its code is shown (until the next stop) and its locals listed
    this.q(".dbg-stack").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-pc]");
      if (!tr) return;
      this.frame = +tr.dataset.level || 0;
      this.viewAddr = `0x${(+tr.dataset.pc).toString(16)}`;
      this.renderStack();
      this.refreshCode();
      this.refreshVars();
    });
    this.q(".dbg-tabs").addEventListener("click", (e) => {
      const t = e.target.closest(".dtab")?.dataset.t;
      if (t) this.setSideTab(t);
    });
    this.setSideTab(this.sideTab, false);
    for (const sel of [".dbg-locals", ".dbg-watch"]) {
      this.q(sel).addEventListener("click", (e) => {
        const tr = e.target.closest("tr[data-path]");
        if (!tr) return;
        if (e.target.closest("[data-b=del]")) { this.removeWatch(+tr.dataset.w); return; }
        if (e.target.closest("[data-b=mem]") && tr.dataset.addr) { this.showMem(`0x${(+tr.dataset.addr).toString(16)}`); return; }
        if (tr.classList.contains("has-kids")) {
          const k = tr.dataset.path;
          if (this.openVars.has(k)) this.openVars.delete(k); else this.openVars.add(k);
          this.renderVars();
        }
      });
    }
    this.q(".dbg-watch-add").addEventListener("submit", (e) => {
      e.preventDefault();
      const inp = this.q(".dbg-watch-add input");
      const t = inp.value.trim();
      if (t) { this.addWatch(t); inp.value = ""; }
    });
    this.q(".dbg-glob-filter").addEventListener("input", () => this.renderGlobals());
    this.q(".dbg-globals").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-name]");
      if (tr) { this.addWatch(tr.dataset.name); this.setSideTab("watch"); }
    });
    this.q(".dbg-mem-tog").addEventListener("click", () => {
      const on = this.q(".dbg-mem").classList.toggle("collapsed");
      this.q(".dbg-mem-tog").setAttribute("aria-expanded", String(!on));
      this.savePref("memShut", on ? "1" : "");
    });
    if (this.pref("memShut", "")) { this.q(".dbg-mem").classList.add("collapsed"); this.q(".dbg-mem-tog").setAttribute("aria-expanded", "false"); }
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
    this.st = null; this.frame = 0; this.globals = null; this.openVars.clear(); this.cursor = null;
    this._memPrev = null;
    this.loadWatches();
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
      if (newStop && !st.running) {
        this.prevRegs = this.curRegs;
        this.frame = 0;
        if (this.follow) this.viewAddr = null;           // every stop shows the PC again (follow pc)
      }
      this.curRegs = new Map(st.regs);
      this.st = st;
      this.renderRegs();
      this.renderStack();
      this.renderBps();
      this.renderMsg();
      this.fillFollow();
      await this.refreshCode();
      await this.refreshMem();
      if (!st.running || this.sideTab === "watch") await this.refreshVars();
    } catch (e) {
      this.q(".dbg-msg").textContent = `debugger: ${e.message}`;
    } finally {
      this.busy = false;
      if (this.again) { this.again = false; this.refresh(full); }
    }
  }

  async refreshCode() {
    if (!this.st) return;
    const addr = this.viewAddr;
    let r;
    try {
      r = await this.api(`/api/debug/listing?${this.chipQ()}&mode=${this.mode}&before=14&after=48` +
                         (addr ? `&addr=${encodeURIComponent(addr)}` : ""));
    } catch (e) { this.code.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    this.renderCode(r);
  }

  renderCode(r) {
    const fa = (a) => this.fa(a);
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
    if (pc && !this.viewAddr) pc.scrollIntoView({ block: "center" });
    else {
      // a 'go to' / stack frame / breakpoint target: its source line (or instruction) centred and marked
      let tgt = null;
      if (r.mode !== "asm" && r.file && r.line != null) tgt = [...this.code.querySelectorAll(".r.src")].find((x) => +x.dataset.line === r.line && r.file.endsWith(x.dataset.file || "\0"));
      if (!tgt && r.addr != null) tgt = this.code.querySelector(`.r.asm[data-addr="${r.addr}"]`);
      if (tgt) { tgt.classList.add("tgt"); tgt.scrollIntoView({ block: "center" }); }
      else this.code.querySelector(".r")?.scrollIntoView({ block: "nearest" });
    }
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
    const meta = this.regMeta(this.chip);           // widths / formats from the server (chipem/regmeta.py)
    const rows = st.regs.map(([n, v]) => {
      const prev = this.prevRegs.get(n);
      const chg = prev !== undefined && prev !== v ? " chg" : "";
      const hx = fmtReg(v, meta[n]);
      let extra = "";
      if (n === "xpsr") extra = ["N", "Z", "C", "V", "Q"].map((f, i) => (v >>> (31 - i)) & 1 ? f : "-").join("") + ` ipsr ${v & 0x1ff}`;
      else if (/^s\d+$/.test(n)) { const f = new Float32Array(new Uint32Array([v >>> 0]).buffer)[0]; extra = `${+f.toPrecision(7)}`; }
      else if (meta[n]?.fmt !== "flag" && meta[n]?.fmt !== "dec" && v > 9 && v < 100000) extra = String(v);
      return `<tr data-r="${esc(n)}" class="${chg}"><th>${esc(n)}</th><td class="v" title="double-click to edit (paused)">${hx}</td><td class="x">${esc(extra)}</td></tr>`;
    });
    this.q(".dbg-regs").innerHTML = rows.join("");
  }

  renderStack() {
    const st = this.st;
    const rows = (st.stack || []).map((f) => `<tr data-pc="${f.pc}" data-level="${f.level}"${f.level === this.frame ? ' class="sel"' : ""} title="${esc(f.how === "scan" ? "found by a stack scan (a guess)" : f.how === "exception" ? "hardware exception frame" : f.how)} · click: show this frame's code and locals">` +
      `<td class="n">#${f.level}</td><td class="ad">${this.fa(f.pc)}</td><td>${esc(f.desc || f.func)}</td><td class="how">${f.how === "pc" || f.how === "cfi" ? "" : esc(f.how)}</td></tr>`);
    this.q(".dbg-stack").innerHTML = rows.join("");
    const hows = new Set((st.stack || []).map((f) => f.how));
    this.q(".dbg-stack-how").textContent = hows.has("cfi") ? "DWARF CFI" : hows.has("scan") ? "stack scan (guess)" : "";
  }

  renderBps() {
    const bps = this.st.bps || [];
    const fa = (a) => this.fa(a);
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
      const fa = this.fa(a).slice(2);
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
    const tab = this.q(`.dbg-scroll[data-p="${this.sideTab}"] table`);
    const rows = [...(tab?.rows || [])].filter((r) => !r.classList.contains("hdr"))
      .map((r) => [...r.cells].map((c) => c.textContent.replace(/^[▸▾×]\s*/, "").trim()).filter(Boolean).join("\t")).filter(Boolean);
    navigator.clipboard?.writeText(rows.join("\n")).then(() => this.toast(`copied ${rows.length} rows (${this.sideTab})`), () => this.toastErr("clipboard not available"));
  }

  // ---------------------------------------------------------------- locals / watch / globals (DWARF: chipem/dwarfvars.py)
  setSideTab(t, refresh = true) {
    this.sideTab = t;
    this.savePref("side", t);
    for (const b of this.box.querySelectorAll(".dtab")) { const on = b.dataset.t === t; b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on)); }
    for (const p of this.box.querySelectorAll(".dbg-regs-sec > .dbg-scroll")) p.hidden = p.dataset.p !== t;
    this.q(".dbg-tab-hint").textContent = t === "regs" ? "double-click a value to edit (paused)" : "";
    if (!refresh || !this.st) return;
    if (t === "globals") this.refreshGlobals();
    else if (t !== "regs") this.refreshVars();
  }

  watchKey() { return `watch.${this.chip || "A"}`; }
  loadWatches() {
    let w = [];
    try { w = JSON.parse(this.pref(this.watchKey(), "[]")) || []; } catch { w = []; }
    this.watches = w.filter((x) => typeof x === "string").map((expr) => ({ expr, r: null }));
  }
  saveWatches() { this.savePref(this.watchKey(), JSON.stringify(this.watches.map((w) => w.expr))); }
  addWatch(expr) {
    if (!this.watches.some((w) => w.expr === expr)) { this.watches.push({ expr, r: null }); this.saveWatches(); }
    this.refreshVars();
  }
  removeWatch(i) { this.watches.splice(i, 1); this.saveWatches(); this.renderVars(); }

  async refreshVars() {
    if (!this.st) return;
    const q = this.chipQ();
    if (this.sideTab === "locals") {
      try { this.locals = await this.api(`/api/debug/locals?${q}${q ? "&" : ""}frame=${this.frame}`); }
      catch (e) { this.locals = { error: e.message }; }
    } else if (this.sideTab === "watch") {
      await Promise.all(this.watches.map(async (w) => {
        try { w.r = await this.api("/api/debug/eval", { ...this.chipB(), expr: w.expr, frame: this.frame }); }
        catch (e) { w.r = { error: e.message }; }
      }));
      if (!this.globals) this.refreshGlobals(true);
    }
    this.renderVars();
  }

  // one value row (+ children when expanded); path = expansion key
  varRows(v, path, depth, out, extra = "") {
    const kids = v.children && v.children.length;
    const open = kids && this.openVars.has(path);
    const val = v.avail === false ? `<span class="na" title="${esc(v.reason || "")}">&lt;${esc(v.reason || "unavailable")}&gt;</span>` : esc(v.text ?? (v.value ?? ""));
    const prev = this._prevVals?.get(path);
    const chg = prev !== undefined && v.text !== undefined && prev !== v.text ? " chg" : "";
    out.push(`<tr data-path="${esc(path)}"${v.addr != null ? ` data-addr="${v.addr}"` : ""}${extra} class="${kids ? "has-kids" : ""}${chg}">` +
      `<td class="nm" style="padding-left:${4 + depth * 12}px">${kids ? (open ? "▾ " : "▸ ") : ""}${esc(v.name ?? "")}${v.kind === "arg" ? ' <i class="dim">arg</i>' : ""}</td>` +
      `<td class="v">${val}</td><td class="ty" title="${esc(v.type || "")}">${esc(v.type || "")}</td>` +
      `<td class="lc" title="${esc(v.loc || "")}">${v.addr != null ? `<span data-b="mem" title="show in the memory view">${esc(v.loc || "")}</span>` : esc(v.loc || "")}</td></tr>`);
    this._newVals.set(path, v.text);
    if (open) for (const [i, c] of v.children.entries()) this.varRows(c, `${path}/${c.name ?? i}`, depth + 1, out);
  }

  renderVars() {
    this._newVals = new Map();
    const hdr = `<tr class="hdr"><th>name</th><th>value</th><th>type</th><th>location</th></tr>`;
    if (this.sideTab === "locals") {
      const L = this.locals;
      const out = [];
      if (!L) out.push(`<tr><td class="dim">—</td></tr>`);
      else if (L.error) out.push(`<tr><td class="dim" colspan="4">${esc(L.error)}</td></tr>`);
      else {
        out.push(`<tr class="hdr"><th colspan="4" class="fr">frame #${L.frame ?? this.frame} ${esc(L.func || "")}${L.reason ? ` · ${esc(L.reason)}` : ""}</th></tr>`);
        if (!(L.vars || []).length) out.push(`<tr><td class="dim" colspan="4">${esc(L.reason || "no arguments or locals")}</td></tr>`);
        else { out.push(hdr); for (const v of L.vars) this.varRows(v, `L/${L.func}/${v.name}`, 0, out); }
      }
      this.q(".dbg-locals").innerHTML = out.join("");
    } else if (this.sideTab === "watch") {
      const out = [];
      if (!this.watches.length) out.push(`<tr><td class="dim" colspan="4">no watch expressions: type one above, or click a global</td></tr>`);
      else out.push(hdr.replace("<th>name</th>", "<th>expression</th>").replace("<th>location</th>", "<th>location</th><th></th>"));
      this.watches.forEach((w, i) => {
        const r = w.r;
        const before = out.length;
        if (!r) out.push(`<tr data-path="W/${i}" data-w="${i}"><td class="nm">${esc(w.expr)}</td><td class="v dim">…</td><td></td><td></td></tr>`);
        else if (r.error) out.push(`<tr data-path="W/${i}" data-w="${i}"><td class="nm">${esc(w.expr)}</td><td class="v err" colspan="3" title="${esc(r.error)}">${esc(r.error)}</td></tr>`);
        else this.varRows({ ...r, name: w.expr }, `W/${w.expr}`, 0, out, ` data-w="${i}"`);
        out[before] = out[before].replace(/<\/tr>$/, `<td><button type="button" class="btn small ghost" data-b="del" title="remove">×</button></td></tr>`);
      });
      this.q(".dbg-watch").innerHTML = out.join("");
    }
    this._prevVals = this._newVals;
  }

  async refreshGlobals(quiet = false) {
    try {
      const r = await this.api(`/api/debug/globals?${this.chipQ()}`);
      this.globals = r.vars || [];
    } catch (e) { this.globals = []; if (!quiet) this.q(".dbg-globals").innerHTML = `<tr><td class="dim">${esc(e.message)}</td></tr>`; return; }
    const dl = this.box.querySelector("#dbgGlobalsList");
    if (dl) dl.innerHTML = this.globals.slice(0, 2000).map((g) => `<option value="${esc(g.name)}">${esc(g.type || "")}</option>`).join("");
    if (!quiet) this.renderGlobals();
  }

  renderGlobals() {
    const f = this.q(".dbg-glob-filter").value.trim();
    let m = null;
    if (f) {
      const rx = /^\/(.+)\/([a-z]*)$/.exec(f);
      try { m = rx ? new RegExp(rx[1], rx[2] || "i") : null; } catch { m = /$^/; }
      if (!rx) { const low = f.toLowerCase(); m = { test: (s) => s.toLowerCase().includes(low) }; }
    }
    const g = (this.globals || []).filter((x) => !m || m.test(x.name) || m.test(x.type || ""));
    const ub = this.st?.unit_bytes || 1;
    const fa = (a) => (a == null ? "" : "0x" + a.toString(16).toUpperCase().padStart(ub === 2 ? 6 : 8, "0"));
    this.q(".dbg-globals").innerHTML = g.length
      ? `<tr class="hdr"><th>name</th><th>type</th><th>address</th><th>size</th><th>file</th></tr>` + g.slice(0, 1000).map((x) =>
        `<tr data-name="${esc(x.name)}" title="click: watch ${esc(x.name)}"><td class="nm">${esc(x.name)}</td><td class="ty">${esc(x.type || "?")}</td>` +
        `<td class="lc">${fa(x.addr)}</td><td class="n">${x.size ?? ""}</td><td class="dim" title="${esc(x.file || "")}">${esc((x.file || "").split(/[\\/]/).pop())}</td></tr>`).join("") +
        (g.length > 1000 ? `<tr><td class="dim" colspan="5">${g.length - 1000} more: filter</td></tr>` : "")
      : `<tr><td class="dim">${this.globals ? "no globals" + (f ? " match" : "") : "loading…"}</td></tr>`;
  }

  showMem(expr) {
    this.q(".dbg-mem").classList.remove("collapsed");
    this.q(".dbg-mem-addr").value = expr;
    this.memGo();
  }
}
