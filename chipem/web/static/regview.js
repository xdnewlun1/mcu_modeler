// Registers tab (pins tray): peripheral register browser of the selected chip. Tree: peripheral -> register -> bit
// fields (names, widths, enumerated values from the chip's register table, chipem/regtable.py). Values come from
// /api/regs/* (chipem/web/debugapi.py) through a side-effect-free peek: a register whose read would pop a FIFO or
// clear a flag (a read hook the model does not declare pure) shows its stored value, dimmed, with "~"; the "read"
// button does a real CPU-style read (with its side effects) while paused. Live values: every stop, and once a second
// while running (only the expanded peripherals). Changed values are highlighted; double-click a value (register or
// field) to write it while paused (through the bus: write side effects apply). Filter: peripheral / register /
// field name substring or /regex/.
import { esc } from "./fmt.js";
import { growTray } from "./debug.js";

export class RegView {
  constructor(box, opts) {
    this.box = box;
    this.api = opts.api;
    this.toast = opts.toast || (() => {});
    this.toastErr = opts.toastErr || ((m) => console.error(m));
    this.chip = opts.chip || null;
    this.isBoard = !!opts.isBoard;
    this.visible = false;
    this.tree = null;            // {chip, unit_bits, blocks: [{name, base, size, model, regs}]}
    this.open = new Set();       // expanded peripherals
    this.openRegs = new Set();   // "BLK.REG" with fields shown
    this.blocks = new Map();     // name -> /api/regs/block result
    this.prev = new Map();       // "BLK.REG" -> value at the previous refresh
    this.filter = "";
    this.lastKey = "";
    this.lastRun = 0;
    this.build();
  }

  q(s) { return this.box.querySelector(s); }
  chipQ() { return this.isBoard && this.chip ? `&chip=${encodeURIComponent(this.chip)}` : ""; }

  build() {
    this.box.classList.add("rv");
    this.box.innerHTML = `
      <div class="dbg-bar">
        <input class="rv-filter pane-filter" type="search" spellcheck="false" placeholder="filter  peripheral / register / field  /re/" aria-label="filter registers">
        <button type="button" class="btn small ghost" data-a="collapse" title="Collapse every peripheral">collapse</button>
        <button type="button" class="btn small ghost" data-a="refresh" title="Re-read the values (no side effects)">refresh</button>
        <button type="button" class="btn small ghost" data-a="copy" title="Copy the visible registers as tab-separated text">copy</button>
        <span class="dbg-hint">values peeked without side effects; <span class="nx">~dim</span> = stored value (the read hook may have side effects): <b>read</b> does a real read (paused)</span>
      </div>
      <div class="rv-scroll"><table class="rv-table"><tbody></tbody></table></div>`;
    this.tbody = this.q("tbody");
    this.q(".rv-filter").addEventListener("input", (e) => { this.filter = e.target.value; this.render(); });
    this.q(".rv-filter").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; this.filter = ""; this.render(); } });
    this.q(".dbg-bar").addEventListener("click", (e) => {
      const a = e.target.closest("button")?.dataset.a;
      if (a === "collapse") { this.open.clear(); this.openRegs.clear(); this.render(); }
      else if (a === "refresh") this.refreshValues(true);
      else if (a === "copy") this.copy();
    });
    this.tbody.addEventListener("click", (e) => this.click(e));
    this.tbody.addEventListener("dblclick", (e) => this.edit(e));
  }

  // ---------------------------------------------------------------- app.js hooks
  show(on) {
    this.visible = on;
    if (on) { growTray(); if (!this.tree) this.load(); else this.refreshValues(true); }
  }

  setChip(cid) {
    if (cid === this.chip) return;
    this.chip = cid;
    this.tree = null; this.blocks.clear(); this.prev.clear(); this.open.clear(); this.openRegs.clear();
    if (this.visible) this.load();
  }

  onState(st) {
    if (!this.visible || !st || !this.open.size) return;
    const cs = st.chips && this.chip ? st.chips[this.chip] || st : st;
    if (st.running) {
      const now = performance.now();
      if (now - this.lastRun > 1000) { this.lastRun = now; this.refreshValues(false); }
      return;
    }
    const key = `${st.last_stop}|${cs.pc}|${cs.insns}`;
    if (key !== this.lastKey) { this.lastKey = key; this.refreshValues(true); }
  }

  // ---------------------------------------------------------------- data
  async load() {
    try { this.tree = await this.api(`/api/regs/tree?${this.chipQ().slice(1)}`); }
    catch (e) { this.tbody.innerHTML = `<tr><td class="dim">${esc(e.message)}</td></tr>`; return; }
    this.render();
  }

  async loadBlock(name) {
    try {
      const b = await this.api(`/api/regs/block?name=${encodeURIComponent(name)}${this.chipQ()}`);
      this.blocks.set(name, b);
      for (const r of b.regs) this.prev.set(`${name}.${r.name}`, r.value);
    } catch (e) { this.toastErr(e.message); }
  }

  async refreshValues(stopped) {
    const names = [...this.open].filter((n) => this.blocks.has(n));
    if (!names.length) return;
    let v;
    try { v = await this.api(`/api/regs/values?blocks=${encodeURIComponent(names.join(","))}${this.chipQ()}`); }
    catch { return; }
    for (const [bn, regs] of Object.entries(v)) {
      const b = this.blocks.get(bn);
      if (!b) continue;
      for (const r of b.regs) {
        const x = regs[r.name];
        if (!x) continue;
        const k = `${bn}.${r.name}`;
        r.changed = r.value !== x[0];
        if (stopped || r.changed) this.prev.set(k, r.value);
        r.value = x[0]; r.exact = x[1];
      }
    }
    this.render();
  }

  // ---------------------------------------------------------------- render
  matcher() {
    const f = this.filter.trim();
    if (!f) return null;
    const m = /^\/(.+)\/([a-z]*)$/.exec(f);
    if (m) { try { const re = new RegExp(m[1], m[2].includes("i") ? m[2] : m[2] + "i"); return (s) => re.test(s); } catch { return () => false; } }
    const low = f.toLowerCase();
    return (s) => s.toLowerCase().includes(low);
  }

  hexw(v, bits) { return "0x" + (v >>> 0).toString(16).toUpperCase().padStart(Math.max(1, Math.ceil(bits / 4)), "0"); }

  render() {
    if (!this.tree) return;
    const m = this.matcher();
    const ub = this.tree.unit_bits || 8;
    const out = [];
    for (const blk of this.tree.blocks) {
      const bmatch = !m || m(blk.name) || m(blk.model);
      const regHits = m && !bmatch ? blk.regs.filter((r) => m(r)) : null;
      const b = this.blocks.get(blk.name);
      const fieldHits = m && !bmatch && b ? b.regs.filter((r) => r.fields.some((f) => m(f.name))).map((r) => r.name) : [];
      if (m && !bmatch && !regHits.length && !fieldHits.length) continue;
      const open = this.open.has(blk.name) || (m && !bmatch && (regHits.length || fieldHits.length));
      out.push(`<tr class="rv-p${open ? " open" : ""}" data-p="${esc(blk.name)}"><td class="tg">${open ? "▾" : "▸"}</td>` +
               `<td class="nm" colspan="2"><b>${esc(blk.name)}</b> <span class="dim">${esc(blk.model)}${blk.absent ? " (absent on this part)" : ""}</span></td>` +
               `<td class="ad">0x${blk.base.toString(16).toUpperCase()}</td><td class="dim" colspan="3">${blk.regs.length} regs · ${blk.size} ${ub === 16 ? "words" : "bytes"}</td></tr>`);
      if (!open) continue;
      if (!b) { out.push(`<tr><td></td><td class="dim" colspan="6">loading…</td></tr>`); if (!this._loading?.has(blk.name)) this.fetchBlock(blk.name); continue; }
      for (const r of b.regs) {
        if (m && !bmatch && !m(r.name) && !fieldHits.includes(r.name)) continue;
        const k = `${blk.name}.${r.name}`;
        const prev = this.prev.get(k);
        const chg = prev !== undefined && prev !== r.value;
        const ropen = this.openRegs.has(k) || (m && fieldHits.includes(r.name));
        const nx = !r.exact;
        out.push(`<tr class="rv-r${chg ? " chg" : ""}" data-p="${esc(blk.name)}" data-r="${esc(r.name)}">` +
                 `<td></td><td class="tg">${r.fields.length ? (ropen ? "▾" : "▸") : ""}</td>` +
                 `<td class="nm">${esc(r.name)}</td><td class="ad">+0x${r.offset.toString(16).toUpperCase()}</td>` +
                 `<td class="v${nx ? " nx" : ""}" title="${nx ? "stored value: reading this register runs a hook that may have side effects, so it is not read (use 'read' while paused)" : "double-click to write (paused)"}">${nx ? "~" : ""}${this.hexw(r.value, r.bits)}</td>` +
                 `<td class="dim">${esc(r.access || "")}${r.reset !== r.value ? "" : " · reset"}</td>` +
                 `<td>${nx ? `<button type="button" class="btn small ghost" data-a="read" title="Real read with side effects (paused)">read</button>` : ""}</td></tr>`);
        if (!ropen) continue;
        for (const f of r.fields) {
          if (m && !bmatch && !m(r.name) && !m(f.name)) continue;
          const fv = Math.floor(r.value / 2 ** f.lsb) % 2 ** f.width;
          const pv = prev !== undefined ? Math.floor(prev / 2 ** f.lsb) % 2 ** f.width : fv;
          const en = f.enum && f.enum[String(fv)];
          const bits = f.width === 1 ? `[${f.lsb}]` : `[${f.lsb + f.width - 1}:${f.lsb}]`;
          out.push(`<tr class="rv-f${pv !== fv ? " chg" : ""}" data-p="${esc(blk.name)}" data-r="${esc(r.name)}" data-f="${esc(f.name)}" data-lsb="${f.lsb}" data-w="${f.width}">` +
                   `<td></td><td></td><td class="nm">· ${esc(f.name)} <span class="dim">${bits}</span></td><td></td>` +
                   `<td class="v${nx ? " nx" : ""}">${f.width === 1 ? fv : "0x" + fv.toString(16).toUpperCase()}</td>` +
                   `<td class="dim" colspan="2">${esc(en || "")}${f.access && f.access !== r.access ? ` <i>${esc(f.access)}</i>` : ""}</td></tr>`);
        }
      }
    }
    this.tbody.innerHTML = out.join("") || `<tr><td class="dim">no match</td></tr>`;
  }

  async fetchBlock(name) {
    this._loading = this._loading || new Set();
    this._loading.add(name);
    await this.loadBlock(name);
    this._loading.delete(name);
    this.render();
  }

  // ---------------------------------------------------------------- interaction
  click(e) {
    const tr = e.target.closest("tr");
    if (!tr) return;
    const a = e.target.closest("button")?.dataset.a;
    if (a === "read") { this.realRead(tr.dataset.p, tr.dataset.r); return; }
    if (tr.classList.contains("rv-p")) {
      const n = tr.dataset.p;
      if (this.open.has(n)) this.open.delete(n); else { this.open.add(n); if (!this.blocks.has(n)) this.fetchBlock(n); }
      this.render();
    } else if (tr.classList.contains("rv-r") && !e.target.closest("td.v")) {
      const k = `${tr.dataset.p}.${tr.dataset.r}`;
      if (this.openRegs.has(k)) this.openRegs.delete(k); else this.openRegs.add(k);
      this.render();
    }
  }

  async realRead(p, r) {
    try {
      const x = await this.api("/api/regs/read", { block: p, reg: r, ...(this.isBoard ? { chip: this.chip } : {}) });
      this.toast(`${p}.${r} = 0x${(x.value >>> 0).toString(16).toUpperCase()} (real read: side effects applied)`);
      this.refreshValues(true);
    } catch (e) { this.toastErr(e.message); }
  }

  edit(e) {
    const td = e.target.closest("td.v");
    const tr = e.target.closest("tr");
    if (!td || !tr || !tr.dataset.r) return;
    const b = this.blocks.get(tr.dataset.p);
    const reg = b?.regs.find((x) => x.name === tr.dataset.r);
    if (!reg) return;
    const field = tr.dataset.f ? { lsb: +tr.dataset.lsb, w: +tr.dataset.w } : null;
    const old = td.textContent;
    td.innerHTML = `<input type="text" spellcheck="false" value="${esc(old.replace(/^~/, ""))}" aria-label="new value">`;
    const inp = td.querySelector("input");
    inp.focus(); inp.select();
    const done = async (ok) => {
      if (!td.contains(inp)) return;
      if (ok) {
        let v = Number(inp.value.trim());
        if (!Number.isFinite(v)) { this.toastErr("not a number"); td.textContent = old; return; }
        if (field) {
          const sh = 2 ** field.lsb, span = 2 ** field.w;
          const cur = Math.floor(reg.value / sh) % span;          // (no 32-bit bit operators: values may be wider)
          v = reg.value - cur * sh + (((v % span) + span) % span) * sh;
        }
        try {
          await this.api("/api/regs/write", { block: tr.dataset.p, reg: tr.dataset.r, value: v, ...(this.isBoard ? { chip: this.chip } : {}) });
          await this.refreshValues(true);
          return;
        } catch (err) { this.toastErr(err.message); }
      }
      td.textContent = old;
    };
    inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") done(true); else if (ev.key === "Escape") { ev.stopPropagation(); done(false); } });
    inp.addEventListener("blur", () => done(false));
  }

  copy() {
    const lines = [...this.tbody.querySelectorAll("tr.rv-r, tr.rv-f")].map((tr) =>
      [...tr.cells].map((c) => c.textContent.trim()).filter(Boolean).join("\t"));
    navigator.clipboard?.writeText(lines.join("\n")).then(() => this.toast(`copied ${lines.length} rows`), () => this.toastErr("clipboard not available"));
  }
}
