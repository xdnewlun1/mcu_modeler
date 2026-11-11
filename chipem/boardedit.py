"""Board setup editing for front ends (web UI "Board" dialog): what can be put on a board, the current setup as a
board-YAML mapping, structural edits on that mapping, and saving/opening board files.

Structural changes (add/remove an MCU, change its chip or firmware, new board, open a board) cannot be applied to a
running Session in place: a chip owns its core, bus and peripherals. They are applied to the board *description*
(the same mapping `Board.to_dict` exports and `parse_board` reads), and the caller builds a new Session from it
(`build_session`). Wiring (nets, links, parts) survives as long as the chips it names do.

Generic glue: no vendor knowledge. Paths in the mapping are absolute while editing; `save_board` rewrites them
relative to the board file, like `Board.to_yaml`.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .cli import CHIPS_DIR

ROOT = CHIPS_DIR.parent
BOARDS_DIR = ROOT / "boards"
FIRMWARE_DIR = ROOT / "firmware"
FIRMWARE_EXTS = (".elf", ".bin", ".hex", ".srec", ".s19", ".c28img", ".out")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")

_chip_cache: dict[Path, tuple[float, dict]] = {}


# ------------------------------------------------------------------------------------------------ catalog
def _chip_entry(path: Path) -> dict | None:
    st = path.stat().st_mtime
    hit = _chip_cache.get(path)
    if hit is not None and hit[0] == st:
        return hit[1]
    from .chipdesc import load_chip
    try:
        d = load_chip(path)
    except Exception:                       # a broken or partial chip YAML is not offered
        return None
    e = {"id": path.stem, "name": d.name, "package": getattr(d, "package", "") or "",
         "family": d.family or "", "arch": d.core.arch, "pins": len(d.pins)}
    _chip_cache[path] = (st, e)
    return e


def catalog() -> dict:
    """{chips: [{id, name, package, family, arch, pins}], firmware: [{path, name, dir}], boards: [{name, path}]}.
    Chips are the chip YAMLs (not register tables); firmware is every built image under firmware/*/build/."""
    chips = []
    for p in sorted(CHIPS_DIR.glob("*.yaml")):
        if p.stem.endswith("_regs"):
            continue
        e = _chip_entry(p)
        if e is not None:
            chips.append(e)
    fw = []
    for p in sorted(FIRMWARE_DIR.glob("*/build/*")):
        if p.is_file() and p.suffix.lower() in FIRMWARE_EXTS:
            fw.append({"path": str(p.resolve()), "name": p.name, "dir": p.parent.parent.name})
    boards = [{"name": p.stem, "path": str(p.resolve())} for p in sorted(BOARDS_DIR.glob("*.y*ml"))]
    boards += [{"name": f"{p.parent.name}/{p.stem}", "path": str(p.resolve())}       # boards/parts/*.yaml, ...
               for p in sorted(BOARDS_DIR.glob("*/*.y*ml"))]
    return {"chips": chips, "firmware": fw, "boards": boards}


# ------------------------------------------------------------------------------------------------ current setup
def board_dict(session: Any) -> dict:
    """The session's setup as a board mapping with absolute paths (a single chip becomes a one-chip board)."""
    if session.board is not None:
        return session.board.to_dict()
    ctx = session.ctxs[session.view]
    src = getattr(ctx.desc, "source_path", None)
    chip = str(Path(src).resolve()) if src else str(ctx.desc.name).lower()
    c: dict[str, Any] = {"id": ctx.id, "chip": chip}
    img = getattr(ctx.image, "source", None)
    if img:
        c["firmware"] = str(Path(img).resolve())
    if ctx.clock_override:
        c["clock_hz"] = ctx.clock_override
    if ctx.vtor is not None:
        c["vtor"] = hex(ctx.vtor)
    return {"name": str(ctx.desc.name).lower(), "chips": [c]}


def _chip_ids(d: dict) -> list[str]:
    return [str(c["id"]) for c in d.get("chips") or []]


def _next_id(d: dict) -> str:
    used = {i.upper() for i in _chip_ids(d)}
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if ch not in used:
            return ch
    n = 1
    while f"U{n}" in used:
        n += 1
    return f"U{n}"


def _check_chip(ref: str) -> str:
    from .cli import resolve_chip
    return str(resolve_chip(str(ref)).resolve())


def _check_firmware(fw: str | None) -> str | None:
    if fw is None or str(fw).strip() == "":
        return None
    p = Path(str(fw)).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    if not p.is_file():
        raise ValueError(f"firmware not found: {fw}")
    return str(p.resolve())


def _apply_chip_fields(c: dict, chip: str | None, firmware: Any, clock_hz: Any, load_addr: Any) -> None:
    if chip is not None:
        c["chip"] = _check_chip(chip)
    if firmware is not ...:
        fw = _check_firmware(firmware)
        if fw is None:
            c.pop("firmware", None)
        else:
            c["firmware"] = fw
        c.pop("format", None)
        c.pop("vtor", None)
    if clock_hz is not ...:
        if clock_hz in (None, "", 0):
            c.pop("clock_hz", None)
        else:
            hz = float(clock_hz)
            if not 1e3 <= hz <= 5e9:
                raise ValueError(f"clock_hz out of range: {clock_hz}")
            c["clock_hz"] = int(hz) if hz.is_integer() else hz
    if load_addr is not ...:
        if load_addr in (None, ""):
            c.pop("load_addr", None)
        else:
            c["load_addr"] = hex(int(str(load_addr), 0))


def add_chip(d: dict, chip: str, *, cid: str | None = None, firmware: str | None = None,
             clock_hz: Any = None, load_addr: Any = None) -> tuple[dict, str]:
    """A copy of board mapping `d` with a new MCU; returns (mapping, chip id)."""
    d = copy.deepcopy(d)
    cid = (cid or "").strip() or _next_id(d)
    if not _ID_RE.match(cid):
        raise ValueError(f"bad chip id {cid!r} (a letter, then letters/digits/_; at most 16)")
    if cid.upper() in {i.upper() for i in _chip_ids(d)}:
        raise ValueError(f"chip id {cid} is already used")
    c: dict[str, Any] = {"id": cid}
    _apply_chip_fields(c, chip, firmware, clock_hz, load_addr)
    d.setdefault("chips", []).append(c)
    return d, cid


def set_chip(d: dict, cid: str, *, chip: str | None = None, firmware: Any = ..., clock_hz: Any = ...,
             load_addr: Any = ...) -> dict:
    """A copy of `d` with MCU `cid` changed (`...` = keep). Changing the chip type drops the wiring that names
    pins it may not have (nets keep their other members; links and part ports to it are removed)."""
    d = copy.deepcopy(d)
    c = next((x for x in d.get("chips") or [] if str(x["id"]).upper() == str(cid).upper()), None)
    if c is None:
        raise ValueError(f"no chip {cid!r} on this board")
    old_chip = c.get("chip")
    _apply_chip_fields(c, chip, firmware, clock_hz, load_addr)
    if chip is not None and Path(str(c["chip"])).resolve() != Path(_check_chip(str(old_chip))).resolve():
        d = _drop_chip_wiring(d, str(c["id"]))
    return d


def remove_chip(d: dict, cid: str) -> dict:
    """A copy of `d` without MCU `cid` and without the wiring that names it."""
    d = copy.deepcopy(d)
    chips = d.get("chips") or []
    keep = [c for c in chips if str(c["id"]).upper() != str(cid).upper()]
    if len(keep) == len(chips):
        raise ValueError(f"no chip {cid!r} on this board")
    if not keep:
        raise ValueError("a board needs at least one MCU")
    real = next(str(c["id"]) for c in chips if str(c["id"]).upper() == str(cid).upper())
    d["chips"] = keep
    return _drop_chip_wiring(d, real)


def _names_chip(ref: Any, cid: str) -> bool:
    return isinstance(ref, str) and ref.upper().startswith(cid.upper() + ".")


def _drop_chip_wiring(d: dict, cid: str) -> dict:
    nets = []
    for n in d.get("nets") or []:
        pins = [p for p in n.get("pins") or [] if not _names_chip(p, cid)]
        if pins or n.get("pull") or n.get("drive"):
            nets.append({**n, "pins": pins})
    d["nets"] = nets
    d["links"] = [ln for ln in d.get("links") or []
                  if not (_names_chip(ln.get("from"), cid) or _names_chip(ln.get("to"), cid))]
    for p in d.get("parts") or []:
        p["ports"] = {k: (None if _names_chip(v, cid) else v) for k, v in (p.get("ports") or {}).items()}
    d["devices"] = [dv for dv in d.get("devices") or [] if str(dv.get("chip", "")).upper() != cid.upper()]
    d["stimulus"] = [s for s in d.get("stimulus") or [] if not re.match(rf"^(?:[^:=]*:)?{re.escape(cid)}\.", str(s),
                                                                         re.I)]
    for k in ("nets", "links", "devices", "stimulus"):
        if not d[k]:
            del d[k]
    return d


def new_board(name: str, chip: str, *, firmware: str | None = None, clock_hz: Any = None) -> dict:
    d = {"name": _check_name(name or "board"), "chips": []}
    return add_chip(d, chip, cid="A", firmware=firmware, clock_hz=clock_hz)[0]


# ------------------------------------------------------------------------------------------------ build / files
def build_session(d: dict, *, like: Any = None) -> Any:
    """A new Session from board mapping `d` (absolute paths), with the run options of session `like`."""
    from .board import parse_board
    from .session import Session
    bd = parse_board(d, base=ROOT)
    opts = {}
    if like is not None:
        c0 = next(iter(like.ctxs.values()))
        opts = {"precise": bool(getattr(c0.chip.core, "precise", False))}
    return Session.from_board(bd, **opts)


def _check_name(name: str) -> str:
    name = str(name).strip()
    if name.lower().endswith((".yaml", ".yml")):
        name = name.rsplit(".", 1)[0]
    if not _NAME_RE.match(name):
        raise ValueError(f"bad board name {name!r} (letters, digits, '_', '-', '.'; no paths)")
    return name


def save_board(d: dict, name: str, *, overwrite: bool = False) -> Path:
    """Write mapping `d` as boards/<name>.yaml (paths relative to boards/). Refuses to replace an existing file
    unless `overwrite`."""
    from .board import parse_board
    name = _check_name(name)
    path = BOARDS_DIR / f"{name}.yaml"
    if path.exists() and not overwrite:
        raise FileExistsError(f"boards/{path.name} exists")
    d = copy.deepcopy(d)
    d["name"] = name
    bd = parse_board(d, base=ROOT)              # validate before writing anything
    text = _yaml_text(d, bd)
    BOARDS_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return path


def _yaml_text(d: dict, bd: Any) -> str:
    """Board YAML with paths relative to boards/, via Board.to_yaml's formatting (one flow line per item)."""
    import os
    rel = copy.deepcopy(d)

    def r(p: str) -> str:
        return os.path.relpath(Path(p).resolve(), BOARDS_DIR.resolve())
    for c in rel.get("chips") or []:
        ch = str(c.get("chip", ""))
        if ch.endswith((".yaml", ".yml")):
            pc = Path(ch).resolve()
            c["chip"] = pc.stem if pc.parent == CHIPS_DIR.resolve() else r(ch)
        if c.get("firmware"):
            c["firmware"] = r(c["firmware"])
    for dv in rel.get("devices") or []:
        if dv.get("file"):
            dv["file"] = r(dv["file"])
    for pt in rel.get("parts") or []:
        if isinstance(pt, dict) and pt.get("file"):
            pt["file"] = r(pt["file"])
    import yaml

    def flow(v: Any) -> str:
        out = yaml.safe_dump(v, default_flow_style=True, sort_keys=False, width=10_000).strip()
        return out[:-3].strip() if out.endswith("\n...") else out
    lines = [f"# board saved by chipem serve; paths are relative to this file", f"name: {flow(rel['name'])}"]
    if rel.get("quantum"):
        lines.append(f"quantum: {flow(rel['quantum'])}")
    for key in ("chips", "nets", "links", "devices", "parts"):
        items = rel.get(key) or []
        if not items:
            continue
        lines.append(f"{key}:")
        for it in items:
            if key == "parts":
                it = {k: v for k, v in it.items() if k != "state"}
            lines.append(f"  - {flow(it)}")
    if rel.get("stimulus"):
        lines.append("stimulus:")
        lines += [f"  - {flow(s)}" for s in rel["stimulus"]]
    return "\n".join(lines) + "\n"


def open_board(path: str) -> dict:
    """Board mapping (absolute paths) of a board YAML: a name in boards/ or a path."""
    import yaml

    from .board import load_board
    p = Path(str(path))
    if not p.suffix:
        p = BOARDS_DIR / f"{_check_name(str(path))}.yaml"
    elif not p.is_absolute():
        p = ROOT / p
    if not p.is_file():
        raise ValueError(f"board file not found: {path}")
    bd = load_board(p)                          # validates and resolves paths
    raw = yaml.safe_load(p.read_text()) or {}
    by_id = {c.id: c for c in bd.chips}
    chips = []
    for c in raw.get("chips") or []:
        spec = by_id[str(c["id"])]
        cc = dict(c)
        cc["chip"] = str(Path(spec.chip).resolve()) if str(spec.chip).endswith((".yaml", ".yml")) else spec.chip
        if spec.firmware:
            cc["firmware"] = str(Path(spec.firmware).resolve())
        chips.append(cc)
    out = dict(raw)
    out["chips"] = chips
    out.setdefault("name", p.stem)
    for dv in out.get("devices") or []:
        if dv.get("file") and not Path(dv["file"]).is_absolute():
            dv["file"] = str((p.parent / dv["file"]).resolve())
    for pt in out.get("parts") or []:              # script parts: `file` is relative to the board file
        if isinstance(pt, dict) and pt.get("file") and not Path(str(pt["file"])).is_absolute():
            pt["file"] = str((p.parent / str(pt["file"])).resolve())
    return out
