"""Web API of the debugger (Debug tab, static/debug.js) and the peripheral register browser (Registers tab,
static/regview.js). Every closure runs on the Worker thread (worker.call); nothing here touches the Session from an
HTTP thread. Wired into server.py by path prefix (/api/debug/*, /api/regs/*).

Debugger (chipem/debug.py; `chip` = board chip id, default the view chip):
  GET  /api/debug/state?chip=          {chip, arch, pc, regs [[name, value]], stack [frames], bps [...], last_stop,
                                        stops (counter: changes at every debugger-visible stop), has_dwarf,
                                        running, unit_bytes}
  GET  /api/debug/listing?chip=&mode=asm|mixed|source&addr=EXPR&before=&after=
                                        rows around addr (default PC): asm {addr, size, bytes, text, sym, bp, pc,
                                        line}, src {file, line, text, code, bp, pc}
  GET  /api/debug/mem?chip=&addr=EXPR&n=  {addr, hex, inexact: [unit offsets shown as stored, not peeked]}
                                        (side-effect free: MMIO registers are peeked)
  POST /api/debug/break {location, chip?, cond?, temp?}      -> {bps: [...]}
  POST /api/debug/toggle {addr, chip?}  breakpoint at addr on/off (gutter click)
  POST /api/debug/watch {target, access: r|w|rw, chip?, cond?, size?}
  POST /api/debug/bp {id, delete?: true, enabled?, cond?, ignore?}
  POST /api/debug/step {kind: stepi|nexti|next|step|finish|until, chip?, location?}
                                        one instruction now, or the Worker runs until the next debugger stop
  POST /api/debug/reg {chip?, name, value}   (paused only)
  POST /api/debug/mem {chip?, addr, hex}     (paused only; MMIO writes go through the bus)
Register browser:
  GET  /api/regs/tree?chip=             [{name, base, size, model, regs: [names]}] (static)
  GET  /api/regs/block?chip=&name=      {name, base, regs: [{name, offset, width, reset, access, fields: [{name, lsb,
                                        width, access, enum}], value, exact}]}
  GET  /api/regs/values?chip=&blocks=A,B  {A: {REG: [value, exact]}}  (peek: no read side effects)
  POST /api/regs/write {chip?, block, reg, value}   (paused only; through the bus: write side effects apply)
  POST /api/regs/read {chip?, block, reg}           (paused only; a real CPU-style read WITH side effects)
"""
from __future__ import annotations

from typing import Any

from ..cmd_shell import jsonable


def _q(qs: dict, k: str, d: str | None = None) -> str | None:
    v = qs.get(k, [d])[0]
    return v if v not in ("", None) else d


def _paused(w: Any) -> None:
    if w.running:
        raise ValueError("pause the emulation first")


def _dbg(s: Any, chip: str | None):
    return s.debug.dbg(chip or None)


def _state(s: Any, w: Any, chip: str | None) -> dict:
    D = s.debug
    d = D.dbg(chip)
    try:
        stack = d.backtrace(24)
    except Exception as e:                       # noqa: BLE001 - never fail the whole state over the stack
        stack = [{"level": 0, "pc": d.core.pc, "func": "?", "desc": f"(stack unavailable: {e})", "how": "pc"}]
    return {"chip": d.cid, "arch": "c28x" if d.c28x else "arm", "pc": d.core.pc, "regs": d.registers(),
            "stack": stack, "bps": [b.to_dict() for b in D.breakpoints()], "last_stop": D.last_stop,
            "stops": D.stops, "has_dwarf": d.dwarf is not None, "running": w.running, "unit_bytes": d.ub,
            "fpu": d._fpu, "func": d.describe(d.core.pc)}


def _block_regs(chip: Any, blk: Any) -> list[dict]:
    """Register rows of one block, with bit fields from the chip's register table (regtable)."""
    from ..regtable import regs_for
    table = None
    try:
        rf = chip._regs_file(chip.desc)
        if rf:
            table = regs_for(blk.name, rf)
    except Exception:
        table = None
    out = []
    for r in sorted((getattr(blk, "regs", {}) or {}).values(), key=lambda r: r.offset):
        row = {"name": r.name, "offset": r.offset, "width": r.width, "reset": r.reset, "bits": r.width *
               getattr(blk, "unit_bits", 8), "fields": [], "access": None, "hook": r.on_read is not None}
        tr = None
        if table is not None:
            try:
                tr = table[r.name] if r.name in table else None
            except Exception:
                tr = None
        if tr is not None:
            row["access"] = tr.access
            row["fields"] = [{"name": f.name, "lsb": f.lsb, "width": f.width, "access": f.access or tr.access,
                              "enum": {str(k): v for k, v in (f.enum or {}).items()}} for f in tr.fields]
        out.append(row)
    return out


def _peek_block(blk: Any) -> dict:
    vals = {}
    peek = getattr(blk, "peek_reg", None)
    for r in (getattr(blk, "regs", {}) or {}).values():
        if peek is not None:
            v, ex = peek(r)
        else:
            v, ex = int.from_bytes(bytes(blk.mem[r.boff:r.boff + r.bw]), "little"), False
        vals[r.name] = [v, bool(ex)]
    return vals


def _block(chip: Any, name: str) -> Any:
    blk = chip.peripherals.get(name) or chip.peripherals.get(str(name).upper())
    if blk is None:
        raise ValueError(f"no peripheral {name!r}")
    return blk


def get(app: Any, path: str, qs: dict) -> dict | list:
    w = app.worker
    chip = _q(qs, "chip")
    if path == "/api/debug/state":
        return w.call(lambda: jsonable(_state(w.s, w, chip)))
    if path == "/api/debug/listing":
        mode = _q(qs, "mode", "mixed")
        if mode not in ("asm", "mixed", "source"):
            raise ValueError("mode must be asm, mixed or source")
        before = max(0, min(int(_q(qs, "before", "12")), 400))
        after = max(1, min(int(_q(qs, "after", "40")), 800))
        addr = _q(qs, "addr")

        def fn():
            d = _dbg(w.s, chip)
            a = None if addr is None else d.eval_expr(addr, values=False)
            return jsonable(d.listing(a, before, after, mode))
        return w.call(fn)
    if path == "/api/debug/mem":
        n = max(1, min(int(_q(qs, "n", "256"), 0), 4096))
        addr = _q(qs, "addr", "0")

        def fn():
            d = _dbg(w.s, chip)
            a = d.eval_expr(addr, values=False)
            data, exact = d.read_memory(a, n)
            return {"addr": a, "hex": data.hex(), "unit_bytes": d.ub,
                    "inexact": [i for i, e in enumerate(exact) if not e]}
        return w.call(fn)
    if path == "/api/regs/tree":
        def fn():
            ctx = w.s._ctx(chip)
            c = ctx.chip
            out = []
            for name, blk in c.peripherals.items():
                out.append({"name": name, "base": blk.base, "size": blk.size,
                            "model": getattr(blk, "model", type(blk).__name__),
                            "absent": bool(getattr(blk, "absent", False)),
                            "regs": [r.name for r in sorted((getattr(blk, "regs", {}) or {}).values(),
                                                            key=lambda r: r.offset)]})
            return {"chip": ctx.id, "unit_bits": c.unit_bits, "blocks": out}
        return w.call(fn)
    if path == "/api/regs/block":
        name = _q(qs, "name") or ""

        def fn():
            c = w.s._ctx(chip).chip
            blk = _block(c, name)
            rows = _block_regs(c, blk)
            vals = _peek_block(blk)
            for r in rows:
                r["value"], r["exact"] = vals.get(r["name"], [0, False])
            return jsonable({"name": blk.name, "base": blk.base, "size": blk.size, "unit_bits": blk.unit_bits,
                             "model": getattr(blk, "model", type(blk).__name__), "regs": rows})
        return w.call(fn)
    if path == "/api/regs/values":
        names = [x for x in (_q(qs, "blocks", "") or "").split(",") if x]

        def fn():
            c = w.s._ctx(chip).chip
            out = {}
            for n in names[:64]:
                blk = c.peripherals.get(n)
                if blk is not None:
                    out[n] = _peek_block(blk)
            return out
        return w.call(fn)
    raise ValueError(f"not found: {path}")


def post(app: Any, path: str, body: dict) -> dict:
    w = app.worker
    chip = body.get("chip") or None
    if path == "/api/debug/break":
        loc = str(body.get("location") or "").strip()
        if not loc:
            raise ValueError("missing 'location'")
        cond = body.get("cond") or None
        temp = bool(body.get("temp"))
        return w.call(lambda: {"bps": jsonable([b.to_dict() for b in
                                                 w.s.debug.add_break(loc, chip=chip, cond=cond, temp=temp)])})
    if path == "/api/debug/toggle":
        addr = int(body.get("addr"))

        def fn():
            d = _dbg(w.s, chip)
            mine = [b for b in d.user_bps() if b.kind == "break" and b.addr == d.norm_code(addr)]
            if mine:
                for b in mine:
                    d.delete(b.id)
                return {"removed": [b.id for b in mine]}
            b = d.add_break("", addr=addr)[0]
            b.location = d.describe(b.addr)
            return {"added": b.to_dict()}
        return w.call(fn)
    if path == "/api/debug/watch":
        target = str(body.get("target") or "").strip()
        if not target:
            raise ValueError("missing 'target'")
        acc = str(body.get("access") or "w")
        size = body.get("size")
        size = int(size) if size not in (None, "") else None
        cond = body.get("cond") or None
        return w.call(lambda: {"bp": jsonable(w.s.debug.add_watch(target, acc, chip=chip, size=size,
                                                                   cond=cond).to_dict())})
    if path == "/api/debug/bp":
        bid = int(body.get("id"))

        def fn():
            D = w.s.debug
            if body.get("delete"):
                return {"deleted": D.delete(bid).id}
            d, b = D.find(bid)
            if "enabled" in body:
                d.enable(bid, bool(body["enabled"]))
            if "cond" in body or "ignore" in body:
                d.modify(bid, **({"cond": body.get("cond") or None} if "cond" in body else {}),
                         **({"ignore": int(body.get("ignore") or 0)} if "ignore" in body else {}))
            return {"bp": jsonable(b.to_dict())}
        return w.call(fn)
    if path == "/api/debug/step":
        kind = str(body.get("kind") or "stepi")
        loc = body.get("location") or None

        def fn():
            s = w.s
            w.running = False
            w.run_target_ns = None
            p = s.debug.plan(kind, chip, loc)
            if p == "step":
                cid = s.debug.chip_id(chip)
                r = s.step(cid)
                w.last_stop = "STEP"
                return {"done": True, "stop": getattr(r, "name", str(r)), "pc": s.ctxs[cid].chip.core.pc}
            w.error = None
            w.last_stop = None
            w._rt_anchor = None
            w.running = True                      # the Worker runs until the next debugger stop
            return {"running": True}
        return w.call(fn)
    if path == "/api/debug/reg":
        name = str(body.get("name") or "")
        val = body.get("value")
        v = int(val, 0) if isinstance(val, str) else int(val)

        def fn():
            _paused(w)
            return {"name": name, "value": _dbg(w.s, chip).set_register(name, v)}
        return w.call(fn)
    if path == "/api/debug/mem":
        addr = str(body.get("addr") or "")
        data = bytes.fromhex(str(body.get("hex") or "").replace(" ", ""))

        def fn():
            _paused(w)
            d = _dbg(w.s, chip)
            a = d.eval_expr(addr, values=False)
            d.write_memory(a, data)
            return {"addr": a, "bytes": len(data)}
        return w.call(fn)
    if path in ("/api/regs/write", "/api/regs/read"):
        bname = str(body.get("block") or "")
        rname = str(body.get("reg") or "")

        def fn():
            _paused(w)
            c = w.s._ctx(chip).chip
            blk = _block(c, bname)
            r = (getattr(blk, "regs", {}) or {}).get(rname)
            if r is None:
                raise ValueError(f"{blk.name} has no register {rname!r}")
            addr = blk.base + r.offset
            if path == "/api/regs/write":
                val = body.get("value")
                v = int(val, 0) if isinstance(val, str) else int(val)
                c.bus.write(addr, r.width, v & r.mask)
                c.core.flush_code_cache()
                v2, ex = blk.peek_reg(r)
                return {"block": blk.name, "reg": r.name, "value": v2, "exact": ex}
            v = c.bus.read(addr, r.width)
            return {"block": blk.name, "reg": r.name, "value": v, "exact": True, "side_effects": True}
        return w.call(fn)
    raise ValueError(f"not found: {path}")
