// Board setup dialog: the MCUs on the board (chip type, firmware, clock), add/remove an MCU, new board, open a
// board from boards/, save to boards/<name>.yaml. Server side: chipem/boardedit.py via /api/catalog,
// /api/board/setup and POST /api/board/{chip/add,chip/set,chip/remove,new,open,save}.
// A structural change rebuilds the emulation (virtual time restarts at 0; nets, links and parts are kept), and the
// page reloads onto the new setup. Wiring itself stays in the board view (wire mode, links panel, + part).

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const OTHER = "\u0000other";

let deps = null;          // {api, toast, toastErr}
let cat = null;           // /api/catalog
let setup = null;         // /api/board/setup

export function initBoardDialog(d) {
  deps = d;
  $("btnBoard").addEventListener("click", openBoardDialog);
  $("bdClose").addEventListener("click", closeBoardDialog);
  $("bdAdd").addEventListener("click", () => addChip());
  $("bdSave").addEventListener("click", () => save(false));
  $("bdOpen").addEventListener("click", openBoard);
  $("bdNew").addEventListener("click", newBoard);
  $("bdChips").addEventListener("click", onChipsClick);
  $("bdChips").addEventListener("change", onFieldChange);
  $("bdChips").addEventListener("input", onFieldChange);
  $("bdAddRow").addEventListener("change", onFieldChange);
}

export function boardDialogOpen() { return !$("boardDlg").hidden; }

export function closeBoardDialog() { $("boardDlg").hidden = true; }

async function openBoardDialog() {
  $("boardDlg").hidden = false;
  $("bdBusy").textContent = "loading…";
  try {
    [cat, setup] = await Promise.all([cat ? Promise.resolve(cat) : deps.api("/api/catalog"), deps.api("/api/board/setup")]);
  } catch (e) { $("bdBusy").textContent = ""; deps.toastErr(`board setup: ${e.message}`); return; }
  $("bdBusy").textContent = "";
  render();
}

// ---------------------------------------------------------------- rendering
function chipOptions(sel) {
  const byFam = new Map();
  for (const c of cat.chips) {
    if (!byFam.has(c.family)) byFam.set(c.family, []);
    byFam.get(c.family).push(c);
  }
  let selId = sel ? String(sel).replace(/^.*\//, "").replace(/\.ya?ml$/i, "").toLowerCase() : "";
  let html = "";
  for (const [fam, list] of byFam) {
    html += `<optgroup label="${esc(fam || "other")}">` + list.map((c) =>
      `<option value="${esc(c.id)}"${c.id === selId ? " selected" : ""}>${esc(c.name)} · ${esc(c.package)}</option>`).join("") + "</optgroup>";
  }
  if (selId && !cat.chips.some((c) => c.id === selId)) html = `<option value="${esc(sel)}" selected>${esc(sel)}</option>` + html;
  return html;
}

function fwOptions(sel) {
  const byDir = new Map();
  for (const f of cat.firmware) {
    if (!byDir.has(f.dir)) byDir.set(f.dir, []);
    byDir.get(f.dir).push(f);
  }
  const known = cat.firmware.some((f) => f.path === sel);
  let html = `<option value=""${!sel ? " selected" : ""}>(no firmware)</option>`;
  for (const [dir, list] of byDir) {
    html += `<optgroup label="${esc(dir)}">` + list.map((f) =>
      `<option value="${esc(f.path)}"${f.path === sel ? " selected" : ""}>${esc(dir)}/${esc(f.name)}</option>`).join("") + "</optgroup>";
  }
  if (sel && !known) html += `<option value="${esc(sel)}" selected>${esc(sel.replace(/^.*\//, ""))} (current)</option>`;
  html += `<option value="${OTHER}">other path…</option>`;
  return html;
}

function fmtHz(hz) { return hz ? String(+hz / 1e6) : ""; }

function render() {
  $("bdName").value = setup.name || "";
  $("bdChips").innerHTML = (setup.chips || []).map((c) => `
    <div class="bd-chip" data-id="${esc(c.id)}">
      <div class="bd-chip-head"><b>${esc(c.id)}</b>
        <button type="button" class="btn small ghost" data-act="apply" hidden title="Rebuild the board with these settings">apply</button>
        <button type="button" class="btn small ghost danger" data-act="remove" title="Remove this MCU and its wiring">remove</button></div>
      <label class="pd-row"><span>chip</span><select data-f="chip" data-orig="${esc(c.chip)}">${chipOptions(c.chip)}</select></label>
      <label class="pd-row"><span>firmware</span><select data-f="firmware">${fwOptions(c.firmware || "")}</select></label>
      <label class="pd-row bd-path" hidden><span>path</span><input type="text" data-f="path" spellcheck="false" placeholder="/path/to/firmware.elf"></label>
      <label class="pd-row"><span>clock MHz</span><input type="text" data-f="clock" value="${esc(fmtHz(c.clock_hz))}" placeholder="reset default" size="8"></label>
    </div>`).join("");
  for (const row of $("bdChips").querySelectorAll(".bd-chip")) row._orig = snapshot(row);
  $("bdAddChip").innerHTML = chipOptions(null);
  $("bdAddFw").innerHTML = fwOptions("");
  $("bdAddPathRow").hidden = true;
  $("bdOpenSel").innerHTML = (cat.boards || []).map((b) => `<option value="${esc(b.path)}">${esc(b.name)}</option>`).join("");
  $("bdOpen").disabled = !(cat.boards || []).length;
}

function fwValue(scope) {
  const sel = scope.querySelector('[data-f="firmware"]');
  if (sel.value !== OTHER) return sel.value;
  return scope.querySelector('[data-f="path"]').value.trim();
}

function snapshot(row) {
  return JSON.stringify([row.querySelector('[data-f="chip"]').value, fwValue(row), row.querySelector('[data-f="clock"]').value.trim()]);
}

function onFieldChange(e) {
  const f = e.target.dataset.f;
  const scope = e.target.closest(".bd-chip, #bdAddRow");
  if (!scope) return;
  if (f === "firmware") {
    const pathRow = scope.querySelector(".bd-path");
    pathRow.hidden = e.target.value !== OTHER;
    if (!pathRow.hidden) pathRow.querySelector("input").focus();
  }
  if (scope.id !== "bdAddRow") scope.querySelector('[data-act="apply"]').hidden = snapshot(scope) === scope._orig;
}

function clockHz(v) {
  v = String(v || "").trim();
  if (!v) return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) throw new Error(`clock: ${v} is not a frequency in MHz`);
  return n * 1e6;
}

// ---------------------------------------------------------------- actions
async function rebuild(path, body, what) {
  $("bdBusy").textContent = `${what}…`;
  for (const b of $("boardDlg").querySelectorAll("button")) b.disabled = true;
  try {
    await deps.api(path, body);
    deps.toast(`${what}: done, reloading`);
    location.reload();
  } catch (e) {
    deps.toastErr(`${what}: ${e.message}`);
    for (const b of $("boardDlg").querySelectorAll("button")) b.disabled = false;
    $("bdOpen").disabled = !(cat.boards || []).length;
    $("bdBusy").textContent = "";
  }
}

async function onChipsClick(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const row = btn.closest(".bd-chip");
  const id = row.dataset.id;
  if (btn.dataset.act === "remove") {
    if ((setup.chips || []).length <= 1) { deps.toastErr("a board needs at least one MCU"); return; }
    if (!confirm(`Remove MCU ${id} and the wires, links and part connections to it? The emulation restarts at t=0.`)) return;
    return rebuild("/api/board/chip/remove", { id }, `remove ${id}`);
  }
  if (btn.dataset.act === "apply") {
    const body = { id };
    const chipSel = row.querySelector('[data-f="chip"]');
    const orig = chipSel.dataset.orig.replace(/^.*\//, "").replace(/\.ya?ml$/i, "").toLowerCase();
    if (chipSel.value !== orig) {
      if (!confirm(`Change ${id} to ${chipSel.value}? Its wiring is removed (pin names differ). The emulation restarts at t=0.`)) return;
      body.chip = chipSel.value;
    }
    try { body.clock_hz = clockHz(row.querySelector('[data-f="clock"]').value); } catch (err) { deps.toastErr(err.message); return; }
    body.firmware = fwValue(row) || null;
    return rebuild("/api/board/chip/set", body, `update ${id}`);
  }
}

async function addChip() {
  const body = { chip: $("bdAddChip").value, id: $("bdAddId").value.trim() || undefined };
  body.firmware = fwValue($("bdAddRow")) || null;
  try { body.clock_hz = clockHz($("bdAddClock").value); } catch (err) { deps.toastErr(err.message); return; }
  return rebuild("/api/board/chip/add", body, `add ${body.chip}`);
}

async function newBoard() {
  if (!confirm("Start a new board with only the MCU chosen under “Add MCU” (as chip A)? The current setup is discarded unless you saved it.")) return;
  const body = { name: $("bdName").value.trim() || "board", chip: $("bdAddChip").value, firmware: fwValue($("bdAddRow")) || null };
  try { body.clock_hz = clockHz($("bdAddClock").value); } catch (err) { deps.toastErr(err.message); return; }
  return rebuild("/api/board/new", body, "new board");
}

async function openBoard() {
  const p = $("bdOpenSel").value;
  if (!p) return;
  if (!confirm(`Open ${p.replace(/^.*\//, "")}? The current setup is discarded unless you saved it.`)) return;
  return rebuild("/api/board/open", { path: p }, "open board");
}

async function save(overwrite) {
  const name = $("bdName").value.trim();
  if (!name) { deps.toastErr("enter a board name"); $("bdName").focus(); return; }
  try {
    const r = await deps.api("/api/board/save", { name, overwrite });
    deps.toast(`saved ${r.saved.replace(/^.*\/(boards\/)/, "$1")}`);
    cat = null;                                   // the board list changed
    cat = await deps.api("/api/catalog");
    render();
  } catch (e) {
    if (!overwrite && /exists/.test(e.message)) {
      if (confirm(`boards/${name}.yaml already exists. Replace it?`)) return save(true);
      return;
    }
    deps.toastErr(`save: ${e.message}`);
  }
}
