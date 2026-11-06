"""C28x base-ISA semantics.

Every table entry names a factory `sem_<name>(c, o, pc, npc)` that returns a closure `f() -> next_pc` with the
decoded operands bound.  `c` is the C28xCore, `o` the operand dict from `ISA.operands` (roles + table args).

Flag rules follow SPRU430 as far as known (see docs/C28X_CORE.md for the deliberate choices):
  * ACC arithmetic: C = carry out of bit 31 (subtract: C = NOT borrow), V is set on signed overflow and otherwise
    left alone (cleared only by testing it with an OV/NOV condition), OVC counts overflows when OVM=0, OVM=1
    saturates ACC to 0x7FFFFFFF / 0x80000000.  `<<16` forms of ADD/SUB only set (ADD) / clear (SUB) C.
  * 16-bit arithmetic: flags from the 16-bit result; no OVC/saturation.
  * compares: N is the sign of the infinitely precise difference (signed compare), Z equality, C = NOT borrow.
  * loc16 destinations @AL/@AH (@ACC for loc32) update N/Z for moves (`nzd`).
"""
from __future__ import annotations

from typing import Any, Callable

M16 = 0xFFFF
M32 = 0xFFFFFFFF
M22 = 0x3FFFFF


class Fault(Exception):
    """Instruction-level fault (memory, unsupported mode); the core reports StopReason.FAULT."""

    def __init__(self, kind: str, **info: Any):
        super().__init__(kind)
        self.kind = kind
        self.info = info


class StopExc(Exception):
    """Raised by a handler that ends the slice (IDLE, ESTOP); npc = where execution resumes."""

    def __init__(self, reason: str, npc: int):
        super().__init__(reason)
        self.reason = reason
        self.npc = npc


def s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def s32(v: int) -> int:
    return v - 0x100000000 if v & 0x80000000 else v


# ============================================================================== loc16 / loc32 addressing
def _ea0() -> int:
    return 0


def loc_access(c, code: int, w32: bool) -> tuple[Callable[[], int], Callable, Callable]:
    """(ea, rd, wr) for addressing byte `code`: `a = ea()` performs the address arithmetic (and post-modify side
    effects) once; `rd(a)` / `wr(a, v)` access the operand.  Register modes return ea=_ea0 and register accessors."""
    xar = c.xar
    sz = 2 if w32 else 1
    rd = c.rd32 if w32 else c.rd16
    wr = c.wr32 if w32 else c.wr16
    if c.amode:
        r = _loc_access_amode1(c, code, sz, rd, wr)
        if r is not None:
            return r
    hi = code >> 6
    if hi == 0:
        off = code

        def ea():
            return ((c.dp << 6) | off) & M22
        return ea, rd, wr
    if hi == 1:
        k = code & 0x3F

        def ea():
            return (c.sp - k) & M16
        return ea, rd, wr
    # SPRU430F 5.6.1: every C28x indirect mode (*XARn++, *--XARn, *+XARn[AR0/AR1/3bit]) also sets ARP = n
    if hi == 3:
        n, k = code & 7, (code >> 3) & 7

        def ea():
            c.arp = n
            return (xar[n] + k) & M22
        return ea, rd, wr
    sub, n = (code >> 3) & 7, code & 7
    if sub == 0:
        def ea():
            c.arp = n
            a = xar[n]
            xar[n] = (a + sz) & M32
            return a & M22
        return ea, rd, wr
    if sub == 1:
        def ea():
            c.arp = n
            a = (xar[n] - sz) & M32
            xar[n] = a
            return a & M22
        return ea, rd, wr
    if sub in (2, 3):
        i = sub - 2

        def ea():
            c.arp = n
            return (xar[n] + (xar[i] & M16)) & M22
        return ea, rd, wr
    if sub == 4:                                     # @ARn / @XARn
        if w32:
            def r(a):
                return xar[n]

            def w(a, v):
                xar[n] = v & M32
        else:
            def r(a):
                return xar[n] & M16

            def w(a, v):
                xar[n] = (xar[n] & 0xFFFF0000) | (v & M16)
        return _ea0, r, w
    if sub == 5 and code < 0xAE:
        return (_ea0,) + _reg_loc(c, code, w32)
    # 10101110/10101111 (*BR0++/--), 10110nnn (*,ARPn), 10111xxx specials
    if code == 0xBD:
        def ea():
            a = c.sp
            c.sp = (a + sz) & M16
            return a
        return ea, rd, wr
    if code == 0xBE:
        def ea():
            a = (c.sp - sz) & M16
            c.sp = a
            return a
        return ea, rd, wr
    if code == 0xBF:                                 # *AR6%++ circular, buffer size AR1[7:0]+1; ARP = 6
        def ea():
            c.arp = 6
            a = xar[6]
            if (a & 0xFF) == (xar[1] & 0xFF):
                xar[6] = a & 0xFFFFFF00
            else:
                xar[6] = (a & 0xFFFF0000) | ((a + sz) & M16)
            return a & M22
        return ea, rd, wr
    if code in (0xAE, 0xAF):                         # *BR0++ / *BR0-- (reverse-carry add of AR0 to AR(ARP))
        sub_ = code == 0xAF

        def ea():
            n_ = c.arp
            a = xar[n_]
            lo = _rc_sub(a & M16, xar[0] & M16) if sub_ else _rc_add(a & M16, xar[0] & M16)
            xar[n_] = (a & 0xFFFF0000) | lo
            return a & M22
        return ea, rd, wr
    if 0xB0 <= code <= 0xB7:                         # *,ARPn: address XAR(ARP), then ARP = n
        def ea():
            a = xar[c.arp]
            c.arp = n
            return a & M22
        return ea, rd, wr
    if code == 0xB8:                                 # *  (XAR(ARP))
        def ea():
            return xar[c.arp] & M22
        return ea, rd, wr
    if code in (0xB9, 0xBA, 0xBB, 0xBC):
        def ea(code=code):
            n_ = c.arp
            a = xar[n_]
            if code == 0xB9:
                xar[n_] = (a + sz) & M32
            elif code == 0xBA:
                xar[n_] = (a - sz) & M32
            elif code == 0xBB:
                xar[n_] = (a + (xar[0] & M16)) & M32
            else:
                xar[n_] = (a - (xar[0] & M16)) & M32
            return a & M22
        return ea, rd, wr
    raise Fault("illegal_addressing_mode", code=code)


def _loc_access_amode1(c, code: int, sz: int, rd, wr):
    """AMODE = 1 (C2xLP-compatible) decodes that differ from AMODE = 0 (SPRU430F Table 5-1, right column); None =
    same as AMODE = 0.  `@@7bit` = DP(15:1):7bit; `11 mmm RRR` = C2xLP post-modify of XAR(ARP), then ARP = n;
    `*+XAR6[AR1%++]` = circular buffer of size XAR1(31:16)+1 indexed by AR1."""
    xar = c.xar
    if code < 0x80:
        off = code & 0x7F

        def ea():
            return (((c.dp & 0xFFFE) << 6) | off) & M22
        return ea, rd, wr
    if code == 0xBF:
        def ea():
            c.arp = 6
            x1 = xar[1]
            idx = x1 & M16
            a = (xar[6] + idx) & M22
            if idx == (x1 >> 16):
                xar[1] = x1 & 0xFFFF0000
            else:
                xar[1] = (x1 & 0xFFFF0000) | ((idx + sz) & M16)
            return a
        return ea, rd, wr
    if code >= 0xC0:
        mod, n = (code >> 3) & 7, code & 7
        if mod > 5:
            raise Fault("illegal_addressing_mode", code=code, amode=1)

        def ea():
            p = c.arp
            a = xar[p]
            if mod == 0:
                xar[p] = (a + sz) & M32
            elif mod == 1:
                xar[p] = (a - sz) & M32
            elif mod == 2:
                xar[p] = (a + (xar[0] & M16)) & M32
            elif mod == 3:
                xar[p] = (a - (xar[0] & M16)) & M32
            else:
                lo = _rc_sub(a & M16, xar[0] & M16) if mod == 5 else _rc_add(a & M16, xar[0] & M16)
                xar[p] = (a & 0xFFFF0000) | lo
            c.arp = n
            return a & M22
        return ea, rd, wr
    return None


def _rc_add(a: int, b: int) -> int:
    ra = int(f"{a:016b}"[::-1], 2)
    rb = int(f"{b:016b}"[::-1], 2)
    return int(f"{(ra + rb) & M16:016b}"[::-1], 2)


def _rc_sub(a: int, b: int) -> int:
    ra = int(f"{a:016b}"[::-1], 2)
    rb = int(f"{b:016b}"[::-1], 2)
    return int(f"{(ra - rb) & M16:016b}"[::-1], 2)


def _reg_loc(c, code: int, w32: bool):
    if w32:
        if code == 0xA9:
            def r(a): return c.acc
            def w(a, v): c.acc = v & M32
        elif code == 0xAB:
            def r(a): return c.p
            def w(a, v): c.p = v & M32
        elif code == 0xAC:
            def r(a): return c.xt
            def w(a, v): c.xt = v & M32
        else:
            raise Fault("illegal_addressing_mode", code=code)
        return r, w
    if code == 0xA8:
        def r(a): return c.acc >> 16
        def w(a, v): c.acc = (c.acc & M16) | ((v & M16) << 16)
    elif code == 0xA9:
        def r(a): return c.acc & M16
        def w(a, v): c.acc = (c.acc & 0xFFFF0000) | (v & M16)
    elif code == 0xAA:
        def r(a): return c.p >> 16
        def w(a, v): c.p = (c.p & M16) | ((v & M16) << 16)
    elif code == 0xAB:
        def r(a): return c.p & M16
        def w(a, v): c.p = (c.p & 0xFFFF0000) | (v & M16)
    elif code == 0xAC:
        def r(a): return c.xt >> 16
        def w(a, v): c.xt = (c.xt & M16) | ((v & M16) << 16)
    elif code == 0xAD:
        def r(a): return c.sp
        def w(a, v): c.sp = v & M16
    else:
        raise Fault("illegal_addressing_mode", code=code)
    return r, w


def loc_get(c, code: int, w32: bool) -> Callable[[], int]:
    """Single closure reading the operand (address side effects included); fast paths for common modes."""
    hi = code >> 6
    if not w32 and c.amode == 0:
        mem, rs, slow = c.mem, c._rslow, c._slow_rd
        if hi == 0:
            off = code

            def g():
                a = (c.dp << 6) | off
                return slow(a, 1) if rs[a >> 10] else mem[a]
            return g
        if hi == 1:
            k = code & 0x3F

            def g():
                a = (c.sp - k) & M16
                return slow(a, 1) if rs[a >> 10] else mem[a]
            return g
        if hi == 3:
            xar, n, k = c.xar, code & 7, (code >> 3) & 7

            def g():
                c.arp = n
                a = (xar[n] + k) & M22
                return slow(a, 1) if rs[a >> 10] else mem[a]
            return g
        if code >> 3 == 0x10:
            xar, n = c.xar, code & 7

            def g():
                c.arp = n
                a = xar[n]
                xar[n] = (a + 1) & M32
                a &= M22
                return slow(a, 1) if rs[a >> 10] else mem[a]
            return g
    ea, rd, wr = loc_access(c, code, w32)
    if ea is _ea0:
        return lambda: rd(0)
    return lambda: rd(ea())


def loc_set(c, code: int, w32: bool) -> Callable[[int], None]:
    ea, rd, wr = loc_access(c, code, w32)
    if ea is _ea0:
        return lambda v: wr(0, v)
    if not w32 and code >> 6 == 0 and not c.amode:
        mem, ws, slow = c.mem, c._wslow, c._slow_wr
        off = code

        def s(v):
            a = (c.dp << 6) | off
            if ws[a >> 10]:
                slow(a, 1, v & M16)
            else:
                mem[a] = v & M16
        return s
    return lambda v: wr(ea(), v)


def loc_is_reg(code: int) -> bool:
    return 0xA0 <= code < 0xAE


# ============================================================================== flag helpers
def set_nz16(c, r: int) -> None:
    c.n = r >> 15
    c.z = 1 if r == 0 else 0


def set_nz32(c, r: int) -> None:
    c.n = r >> 31
    c.z = 1 if r == 0 else 0


def ax_dest_nz(c, code: int, w32: bool = False):
    """The SPRU430F rule of the store-type instructions (PREAD, POP loc16, MOVB loc16,AX.LSB, MOV loc16,*(0:16bit),
    the conditional MOV/MOVB/MOVL ...,COND): "if (loc16 = @AX)" -- or loc32 = @ACC -- "then N/Z are set from AX
    (ACC) after the move".  Returns a function that sets N/Z from the destination register, or None when the
    destination is not @AL/@AH/@ACC."""
    if c.amode and code < 0x80:
        return None
    if w32:
        if code != 0xA9:
            return None
        def f():
            v = c.acc
            c.n = v >> 31
            c.z = 1 if v == 0 else 0
        return f
    if code not in (0xA8, 0xA9):
        return None
    sh = 16 if code == 0xA8 else 0

    def f():
        v = (c.acc >> sh) & M16
        c.n = v >> 15
        c.z = 1 if v == 0 else 0
    return f


def _with_nz(h, nz):
    """Wrap an instruction closure (and its .rpt) so the destination-register N/Z rule runs after it."""
    if nz is None:
        return h
    rep_ = getattr(h, "rpt", None)

    def g():
        r = h()
        nz()
        return r
    if rep_ is not None:
        def grep(n):
            r = rep_(n)
            nz()
            return r
        g.rpt = grep
    return g


def _ovc_bump(c, d: int) -> None:
    o = c.ovc + d
    if o > 31:
        o -= 64
    elif o < -32:
        o += 64
    c.ovc = o


def acc_add(c, b: int, cin: int = 0, carry_mode: int = 0) -> None:
    """ACC += b (+cin). carry_mode 1: C only set (the <<16 form)."""
    a = c.acc
    r = a + b + cin
    if carry_mode:
        if r >> 32:
            c.c = 1
    else:
        c.c = r >> 32
    r &= M32
    if (a ^ r) & (b ^ r) & 0x80000000:
        c.v = 1
        if c.ovm:
            r = 0x80000000 if a >> 31 else 0x7FFFFFFF
        else:
            _ovc_bump(c, -1 if a >> 31 else 1)
    c.acc = r
    c.n = r >> 31
    c.z = 1 if r == 0 else 0


def acc_sub(c, b: int, bin_: int = 0, carry_mode: int = 0) -> None:
    """ACC -= b (+borrow). C = NOT borrow; carry_mode 1: C only cleared (the <<16 form)."""
    a = c.acc
    r = a - b - bin_
    if carry_mode:
        if r < 0:
            c.c = 0
    else:
        c.c = 0 if r < 0 else 1
    r &= M32
    if (a ^ b) & (a ^ r) & 0x80000000:
        c.v = 1
        if c.ovm:
            r = 0x80000000 if a >> 31 else 0x7FFFFFFF
        else:
            _ovc_bump(c, -1 if a >> 31 else 1)
    c.acc = r
    c.n = r >> 31
    c.z = 1 if r == 0 else 0


def add32_flags(c, a: int, b: int, sub: bool) -> int:
    """32-bit add/sub with C/N/Z/V/OVC (no saturation) for loc32 destinations and P."""
    r = a - b if sub else a + b
    c.c = (0 if r < 0 else 1) if sub else r >> 32
    r &= M32
    ov = ((a ^ b) & (a ^ r)) if sub else ((a ^ r) & (b ^ r))
    if ov & 0x80000000:
        c.v = 1
        if not c.ovm:
            _ovc_bump(c, -1 if a >> 31 else 1)
    c.n = r >> 31
    c.z = 1 if r == 0 else 0
    return r


def cmp32(c, a: int, b: int) -> None:
    r = a - b
    c.c = 0 if r < 0 else 1
    c.z = 1 if a == b else 0
    c.n = 1 if s32(a) < s32(b) else 0


def cmp16(c, a: int, b: int) -> None:
    c.c = 0 if a < b else 1
    c.z = 1 if a == b else 0
    c.n = 1 if s16(a) < s16(b) else 0


def pshift(c, v: int) -> int:
    """P << PM (ST0.PM: 0 = <<1, 1 = none, 2..7 = >> 1..6).  SPRU430F 2.x: right shifts always sign-extend."""
    pm = c.pm
    if pm == 1:
        return v
    if pm == 0:
        return (v << 1) & M32
    sh = pm - 1
    if v & 0x80000000:
        return ((v - 0x100000000) >> sh) & M32
    return v >> sh


def push2x16(c, v: int) -> None:
    """LC/LCR: [SP] = v(15:0); SP++; [SP] = v(21:16); SP++ (two 16-bit writes, no alignment; SPRU430F LC/LCR)."""
    sp = c.sp
    c.wr16(sp, v & M16)
    c.wr16((sp + 1) & M16, (v >> 16) & M16)
    c.sp = (sp + 2) & M16


def pop2x16(c) -> int:
    """LRET/LRETE/LRETR: SP--; hi = [SP]; SP--; lo = [SP]."""
    sp = (c.sp - 1) & M16
    hi = c.rd16(sp)
    sp = (sp - 1) & M16
    lo = c.rd16(sp)
    c.sp = sp
    return (hi << 16) | lo


def make_cond(c, k: int) -> Callable[[], int] | None:
    """Condition test closure (None = UNC).  OV/NOV clear V after testing it."""
    if k == 15:
        return None
    if k == 0:
        return lambda: not c.z
    if k == 1:
        return lambda: c.z
    if k == 2:
        return lambda: not c.z and not c.n
    if k == 3:
        return lambda: not c.n
    if k == 4:
        return lambda: c.n
    if k == 5:
        return lambda: c.z or c.n
    if k == 6:
        return lambda: c.c and not c.z
    if k == 7:
        return lambda: c.c
    if k == 8:
        return lambda: not c.c
    if k == 9:
        return lambda: not c.c or c.z
    if k == 10:
        def nov():
            r = not c.v
            c.v = 0
            return r
        return nov
    if k == 11:
        def ov():
            r = c.v
            c.v = 0
            return r
        return ov
    if k == 12:
        return lambda: not c.tc
    if k == 13:
        return lambda: c.tc
    return lambda: 0                                  # NBIO: BIO input is high (not modelled)


# ============================================================================== generic register accessors
def _src(c, spec: str, o: dict) -> Callable[[], int]:
    """Value getter for a move source spec."""
    if spec == "loc16":
        return loc_get(c, o.get("loc", o.get("L")), False)
    if spec == "loc32":
        return loc_get(c, o.get("loc", o.get("L")), True)
    if spec in ("imm", "addr"):
        v = o["imm"] if spec == "imm" else o["addr"]
        v &= M32
        return lambda: v
    if spec == "zero":
        return lambda: 0
    if spec == "ax":
        if o["ax"]:
            return lambda: c.acc >> 16
        return lambda: c.acc & M16
    if spec == "acc":
        return lambda: c.acc
    if spec == "p":
        return lambda: c.p
    if spec == "xt":
        return lambda: c.xt
    if spec == "t":
        return lambda: c.xt >> 16
    if spec == "ier":
        return lambda: c.ier
    if spec == "ovc":
        return lambda: (c.ovc & 0x3F) << 10
    if spec == "ovclo":                              # MOVU loc16,OVC: [loc16] = 0:OVC
        return lambda: c.ovc & 0x3F
    if spec == "ar":
        xar, n = c.xar, o["n"]
        return lambda: xar[n] & M16
    if spec == "xar":
        xar, n = c.xar, o["n"]
        return lambda: xar[n]
    if spec == "ppm":
        return lambda: pshift(c, c.p)
    if spec == "ppm_lo":
        return lambda: pshift(c, c.p) & M16
    if spec == "ppm_hi":
        return lambda: pshift(c, c.p) >> 16
    raise ValueError(f"unknown move source {spec}")


def _dst(c, spec: str, o: dict) -> Callable[[int], None]:
    if spec == "loc16":
        return loc_set(c, o.get("loc", o.get("L")), False)
    if spec == "loc32":
        return loc_set(c, o.get("loc", o.get("L")), True)
    if spec == "ax":
        if o["ax"]:
            def w(v): c.acc = (c.acc & M16) | ((v & M16) << 16)
        else:
            def w(v): c.acc = (c.acc & 0xFFFF0000) | (v & M16)
        return w
    if spec == "acc":
        def w(v): c.acc = v & M32
        return w
    if spec == "p":
        def w(v): c.p = v & M32
        return w
    if spec == "ph":
        def w(v): c.p = (c.p & M16) | ((v & M16) << 16)
        return w
    if spec == "pl":
        def w(v): c.p = (c.p & 0xFFFF0000) | (v & M16)
        return w
    if spec == "xt":
        def w(v): c.xt = v & M32
        return w
    if spec == "t":
        def w(v): c.xt = (c.xt & M16) | ((v & M16) << 16)
        return w
    if spec == "tl":
        def w(v): c.xt = (c.xt & 0xFFFF0000) | (v & M16)
        return w
    if spec == "tlx":                                # MOVX TL,loc16: TL = loc16, T = sign extension
        def w(v):
            v &= M16
            c.xt = (0xFFFF0000 if v & 0x8000 else 0) | v
        return w
    if spec == "ier":
        def w(v): c.ier = v & M16
        return w
    if spec in ("ovc", "ovcu"):                      # MOV OVC,loc16: OVC = [loc16(15:10)]; MOVU: OVC = [loc16(5:0)]
        sh = 10 if spec == "ovc" else 0

        def w(v):
            o_ = (v >> sh) & 0x3F
            c.ovc = o_ - 64 if o_ & 0x20 else o_
        return w
    if spec == "pm":
        def w(v): c.pm = v & 7
        return w
    if spec == "dp":
        def w(v): c.dp = v & M16
        return w
    if spec == "dp10":
        def w(v): c.dp = (c.dp & 0xFC00) | (v & 0x3FF)
        return w
    xar, n = c.xar, o.get("n")
    if spec == "xar":
        def w(v): xar[n] = v & M32
        return w
    if spec == "ar":
        def w(v): xar[n] = (xar[n] & 0xFFFF0000) | (v & M16)
        return w
    if spec == "arz":
        def w(v): xar[n] = v & M16
        return w
    raise ValueError(f"unknown move destination {spec}")


# ============================================================================== moves
def sem_mov(c, o, pc, npc):
    d, s, f = o["d"], o["s"], o.get("f", "")
    code = o.get("loc", o.get("L"))
    if f == "nzd":
        if d == "loc16" and code in (0xA8, 0xA9):
            f = "nz16"
        elif d == "loc32" and code == 0xA9:
            f = "nz32"
        else:
            f = ""
    # fast paths
    if d == "ax" and s == "loc16" and f == "nz16":
        get = loc_get(c, code, False)
        if o["ax"]:
            def h():
                v = get()
                c.acc = (c.acc & M16) | (v << 16)
                c.n = v >> 15
                c.z = 1 if v == 0 else 0
                return npc
        else:
            def h():
                v = get()
                c.acc = (c.acc & 0xFFFF0000) | v
                c.n = v >> 15
                c.z = 1 if v == 0 else 0
                return npc
        return h
    if d == "loc16" and s == "ax" and not f:
        st = loc_set(c, code, False)
        if o["ax"]:
            def h():
                st(c.acc >> 16)
                return npc
        else:
            def h():
                st(c.acc & M16)
                return npc
        return h
    if d == "acc" and s == "loc32":
        get = loc_get(c, code, True)

        def h():
            v = get()
            c.acc = v
            c.n = v >> 31
            c.z = 1 if v == 0 else 0
            return npc
        return h
    get = _src(c, s, o)
    put = _dst(c, d, o)
    if f == "nz16":
        def h():
            v = get() & M16
            put(v)
            c.n = v >> 15
            c.z = 1 if v == 0 else 0
            return npc
    elif f == "nz32":
        def h():
            v = get() & M32
            put(v)
            c.n = v >> 31
            c.z = 1 if v == 0 else 0
            return npc
    else:
        def h():
            put(get())
            return npc
    return h


def sem_movcond(c, o, pc, npc):
    w32 = o.get("w32", 0)
    ea, rd, wr = loc_access(c, o["loc"], bool(w32))
    cond = make_cond(c, o["cond"]) or (lambda: 1)
    if w32:
        get = lambda: c.acc
    elif o.get("imm") is not None and "ax" not in o:
        k = o["imm"] & 0xFF
        get = lambda: k
    else:
        get = _src(c, "ax", o)

    nz = ax_dest_nz(c, o["loc"], bool(w32))
    if nz is None:
        def h():
            a = ea()
            if cond():
                wr(a, get())
            return npc
    else:
        def h():
            a = ea()
            if cond():
                wr(a, get())
                nz()
            return npc
    return h


def _byte_sel(c, code: int) -> Callable[[], int]:
    """MOVB AX.LSB/MSB <-> loc16 byte select (SPRU430F MOVB pages): for `*+XARn[offset]` (3-bit, AR0 or AR1 index)
    an odd offset selects the high byte of the word; every other mode uses the low byte.  Evaluated before the
    address arithmetic."""
    if not c.amode and code >> 6 == 3:
        k = (code >> 3) & 1
        return lambda: k
    if code >> 4 == 9:                               # *+XARn[AR0] (10010nnn) / *+XARn[AR1] (10011nnn)
        i, xar = (code >> 3) & 1, c.xar
        return lambda: xar[i] & 1
    return lambda: 0


def sem_movb_ld(c, o, pc, npc):
    """MOVB AX.LSB,loc16 (AX.MSB := 0) / MOVB AX.MSB,loc16 (AX.LSB unchanged)."""
    code = o["loc"]
    ea, rd, _ = loc_access(c, code, False)
    sel = _byte_sel(c, code)
    sh = 16 if o["ax"] else 0
    msb = o["msb"]

    def h():
        odd = sel()
        v = (rd(ea()) >> (8 if odd else 0)) & 0xFF
        if msb:
            ax = (((c.acc >> sh) & 0x00FF) | (v << 8))
        else:
            ax = v
        c.acc = (c.acc & ~(M16 << sh)) & M32 | (ax << sh)
        set_nz16(c, ax)
        return npc
    return h


def sem_movb_st(c, o, pc, npc):
    """MOVB loc16,AX.LSB/MSB: read-modify-write of one byte of [loc16] (odd *+XARn[offset] = high byte)."""
    code = o["loc"]
    ea, rd, wr = loc_access(c, code, False)
    sel = _byte_sel(c, code)
    sh = 16 if o["ax"] else 0
    msb = o["msb"]

    def h():
        odd = sel()
        a = ea()
        ax = (c.acc >> sh) & M16
        b = (ax >> 8) if msb else (ax & 0xFF)
        old = rd(a)
        wr(a, ((old & 0x00FF) | (b << 8)) if odd else ((old & 0xFF00) | b))
        return npc
    return _with_nz(h, ax_dest_nz(c, code))


def sem_movabs(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)
    addr = o["imm"]
    rd16, wr16 = c.rd16, c.wr16
    if o["ld"]:
        def h():
            wr(ea(), rd16(addr))
            return npc

        def rep(n):
            for i in range(n):
                wr(ea(), rd16((addr + i) & M16))
            return npc
    else:
        def h():
            wr16(addr, rd(ea()))
            return npc

        def rep(n):
            for i in range(n):
                wr16((addr + i) & M16, rd(ea()))
            return npc
    h.rpt = rep
    return _with_nz(h, ax_dest_nz(c, o["loc"])) if o["ld"] else h


def sem_movabs32(c, o, pc, npc):
    """MOV32 *(0:16bit),loc32 / MOV32 loc32,*(0:16bit) (SPRUHS1C, FPU-family data moves)."""
    ea, rd, wr = loc_access(c, o["loc"], True)
    addr = o["imm"]
    rd32, wr32 = c.rd32, c.wr32
    if o["ld"]:
        def h():
            wr(ea(), rd32(addr))
            return npc
    else:
        def h():
            wr32(addr, rd(ea()))
            return npc
    return h


def sem_movacc(c, o, pc, npc):
    """MOV loc16,ACC<<1..8 (low word) / MOVH loc16,ACC<<1..8 (high word); N/Z only when loc16 is @AL/@AH."""
    sh = o["sh"]
    hi = o.get("hi", 0)
    code = o["loc"]
    put = loc_set(c, code, False)
    nz = code in (0xA8, 0xA9)

    def h():
        v = (c.acc << sh) & M32
        v = v >> 16 if hi else v & M16
        put(v)
        if nz:
            c.n = v >> 15
            c.z = 1 if v == 0 else 0
        return npc
    return h


def sem_movpc(c, o, pc, npc):
    """MOV XARn,PC: XARn = 0:PC (address of this instruction)."""
    xar, n = c.xar, o["n"]

    def h():
        xar[n] = pc & M22
        return npc
    return h


def sem_dmov(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)

    def h():
        a = ea()
        wr((a + 1) & M22, rd(a))
        return npc
    return h


def sem_movdl(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], True)

    def h():
        a = ea()
        v = rd(a)
        c.xt = v
        wr((a + 2) & M22, v)
        return npc
    return h


def _prog_addr_src(c, o):
    src = o.get("src", "xar7")
    if src == "al":
        return lambda: 0x3F0000 | (c.acc & M16)
    if src == "pma":
        k = 0x3F0000 | o["imm"]
        return lambda: k
    xar = c.xar
    return lambda: xar[7] & M22


def sem_pread(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)
    src = _prog_addr_src(c, o)
    rd16 = c.rd16

    def h():
        wr(ea(), rd16(src()))
        return npc

    def rep(n):
        base = src()
        for i in range(n):
            wr(ea(), rd16((base + i) & M22))
        return npc
    h.rpt = rep
    return _with_nz(h, ax_dest_nz(c, o["loc"]))


def sem_pwrite(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)
    src = _prog_addr_src(c, o)
    wr16 = c.wr16

    def h():
        wr16(src(), rd(ea()))
        return npc

    def rep(n):
        base = src()
        for i in range(n):
            wr16((base + i) & M22, rd(ea()))
        return npc
    h.rpt = rep
    return h


def sem_pushloc(c, o, pc, npc):
    get = loc_get(c, o["loc"], False)

    def h():
        v = get()
        c.wr16(c.sp, v)
        c.sp = (c.sp + 1) & M16
        return npc
    return h


def sem_poploc(c, o, pc, npc):
    put = loc_set(c, o["loc"], False)

    def h():
        c.sp = (c.sp - 1) & M16
        put(c.rd16(c.sp))
        return npc
    return _with_nz(h, ax_dest_nz(c, o["loc"]))


_PUSHREG = {
    "ar0": (lambda c: c.xar[0] & M16, lambda c, v: c.xar.__setitem__(0, (c.xar[0] & 0xFFFF0000) | v)),
    "ar1": (lambda c: c.xar[1] & M16, lambda c, v: c.xar.__setitem__(1, (c.xar[1] & 0xFFFF0000) | v)),
    "ar2": (lambda c: c.xar[2] & M16, lambda c, v: c.xar.__setitem__(2, (c.xar[2] & 0xFFFF0000) | v)),
    "ar3": (lambda c: c.xar[3] & M16, lambda c, v: c.xar.__setitem__(3, (c.xar[3] & 0xFFFF0000) | v)),
    "ar4": (lambda c: c.xar[4] & M16, lambda c, v: c.xar.__setitem__(4, (c.xar[4] & 0xFFFF0000) | v)),
    "ar5": (lambda c: c.xar[5] & M16, lambda c, v: c.xar.__setitem__(5, (c.xar[5] & 0xFFFF0000) | v)),
    "ar0h": (lambda c: c.xar[0] >> 16, lambda c, v: c.xar.__setitem__(0, (c.xar[0] & M16) | (v << 16))),
    "ar1h": (lambda c: c.xar[1] >> 16, lambda c, v: c.xar.__setitem__(1, (c.xar[1] & M16) | (v << 16))),
    "dbgier": (lambda c: c.dbgier, lambda c, v: setattr(c, "dbgier", v)),
    "dp": (lambda c: c.dp, lambda c, v: setattr(c, "dp", v)),
    "st0": (lambda c: c.st0, lambda c, v: setattr(c, "st0", v)),
    "st1": (lambda c: c.st1, lambda c, v: setattr(c, "st1", v)),
    "ifr": (lambda c: c.ifr, lambda c, v: setattr(c, "ifr", v)),
    "t": (lambda c: c.xt >> 16, lambda c, v: setattr(c, "xt", (c.xt & M16) | (v << 16))),
}


def sem_push(c, o, pc, npc):
    regs = o["r"]
    if regs == ["rpc32"]:
        def h():
            c.push32(c.rpc)
            return npc
        return h
    gets = [_PUSHREG[r][0] for r in regs]
    if len(gets) == 2:
        g0, g1 = gets

        def h():
            c.push32(g0(c) | (g1(c) << 16))
            return npc
    else:
        g0 = gets[0]

        def h():
            c.wr16(c.sp, g0(c))
            c.sp = (c.sp + 1) & M16
            return npc
    return h


def sem_pop(c, o, pc, npc):
    regs = o["r"]
    if regs == ["rpc32"]:
        def h():
            c.rpc = c.pop32() & M22
            return npc
        return h
    sets = [_PUSHREG[r][1] for r in regs]
    if len(sets) == 2:
        s0, s1 = sets

        def h():
            v = c.pop32()
            s0(c, v & M16)
            s1(c, v >> 16)
            return npc
    else:
        s0 = sets[0]

        def h():
            c.sp = (c.sp - 1) & M16
            s0(c, c.rd16(c.sp))
            return npc
    return h


# ============================================================================== ACC with 16-bit operand + shift
def sem_accsh(c, o, pc, npc):
    op = o["op"]
    zero_ext = o.get("ext") == "zero"
    sh = o.get("sh", 0)
    tshift = sh == "T"
    if o["s"] == "imm":
        k = o["imm"] & M16
        get = lambda: k
    else:
        get = loc_get(c, o["loc"], False)
    cmode = 1 if sh == 16 and op in ("add", "sub") and not zero_ext else 0

    def operand():
        v = get()
        if not zero_ext and c.sxm and v & 0x8000:
            v -= 0x10000
        s = (c.xt >> 16) & 0xF if tshift else sh
        return (v << s) & M32

    if op == "mov":
        if not tshift and sh == 0 and not zero_ext and o["s"] == "loc16":
            def h():
                v = get()
                if c.sxm and v & 0x8000:
                    v |= 0xFFFF0000
                c.acc = v
                c.n = v >> 31
                c.z = 1 if v == 0 else 0
                return npc
            return h

        def h():
            v = operand()
            c.acc = v
            c.n = v >> 31
            c.z = 1 if v == 0 else 0
            return npc
    elif op == "add":
        if zero_ext and o["s"] == "imm" and not tshift:
            b = (k << sh) & M32

            def h():
                acc_add(c, b)
                return npc
            return h

        def h():
            acc_add(c, operand(), 0, cmode)
            return npc
    elif op == "sub":
        if zero_ext and o["s"] == "imm" and not tshift:
            b = (k << sh) & M32

            def h():
                acc_sub(c, b)
                return npc
            return h

        def h():
            acc_sub(c, operand(), 0, cmode)
            return npc
    elif op == "addc":
        def h():
            acc_add(c, operand(), c.c)
            return npc
    elif op == "subb":
        def h():
            acc_sub(c, operand(), 1 - c.c)
            return npc
    else:
        def h():
            v = operand()
            if op == "and":
                r = c.acc & v
            elif op == "or":
                r = c.acc | v
            else:
                r = c.acc ^ v
            c.acc = r
            c.n = r >> 31
            c.z = 1 if r == 0 else 0
            return npc
    return h


# ============================================================================== 32-bit ACC / P / loc32 ops
def sem_acc32(c, o, pc, npc):
    op = o["op"]
    get = (lambda: pshift(c, c.p)) if o["s"] == "ppm" else loc_get(c, o["loc"], True)
    if op == "add":
        def h():
            acc_add(c, get())
            return npc
    elif op == "addu":
        def h():
            acc_add(c, get())
            return npc
    elif op == "sub" or op == "subu":
        def h():
            acc_sub(c, get())
            return npc
    elif op == "addc":
        def h():
            acc_add(c, get(), c.c)
            return npc
    elif op == "subb":
        def h():
            acc_sub(c, get(), 1 - c.c)
            return npc
    elif op == "cmp":
        def h():
            cmp32(c, c.acc, get())
            return npc
    elif op == "and":
        def h():
            r = c.acc & get()
            c.acc = r
            set_nz32(c, r)
            return npc
    elif op in ("max", "min"):
        mx = op == "max"

        def h():
            b = get()
            a = c.acc
            cmp32(c, a, b)
            if (s32(a) < s32(b)) if mx else (s32(a) > s32(b)):
                c.acc = b
            set_nz32(c, c.acc)
            return npc
    else:
        raise ValueError(op)
    return h


def sem_p32(c, o, pc, npc):
    op = o["op"]
    get = loc_get(c, o["loc"], True)

    def h():
        b = get()
        a = c.p
        if op == "addu":
            c.p = add32_flags(c, a, b, False)
        elif op == "subu":
            c.p = add32_flags(c, a, b, True)
        elif op == "maxcu":
            r = (a & 0xFFFF_FFFF) - b
            c.c = 0 if r < 0 else 1
            c.z = 1 if a == b else 0
            c.n = 0
            if a < b:
                c.p = b
        else:  # mincu
            r = a - b
            c.c = 0 if r < 0 else 1
            c.z = 1 if a == b else 0
            c.n = 0
            if a > b:
                c.p = b
        return npc
    return h


def sem_loc32op(c, o, pc, npc):
    op = o["op"]
    ea, rd, wr = loc_access(c, o["loc"], True)

    def h():
        a = ea()
        m = rd(a)
        if op == "add":
            r = add32_flags(c, m, c.acc, False)
        elif op == "sub":
            r = add32_flags(c, m, c.acc, True)
        else:
            r = add32_flags(c, c.acc, m, True)
        wr(a, r)
        return npc
    return h


def sem_cmp64(c, o, pc, npc):
    def h():
        v = (c.acc << 32) | c.p
        n = c.acc >> 31
        if c.v:
            n ^= 1
        c.n = n
        c.z = 1 if v == 0 else 0
        c.v = 0
        return npc
    return h


def sem_subcu(c, o, pc, npc):
    get = loc_get(c, o["loc"], False)

    def h():
        m = get()
        acc = c.acc
        t = (acc << 1) - (m << 16)
        if t >= 0:
            r = (t + 1) & M32
            c.c = 1
        else:
            r = (acc << 1) & M32
            c.c = 0
        c.acc = r
        set_nz32(c, r)
        return npc
    return h


def sem_subcul(c, o, pc, npc):
    get = loc_get(c, o["loc"], True)

    def h():
        m = get()
        v = (c.acc << 32) | c.p
        t = (v << 1) - (m << 32)
        if t >= 0:
            r = (t + 1) & 0xFFFFFFFFFFFFFFFF
            c.c = 1
        else:
            r = (v << 1) & 0xFFFFFFFFFFFFFFFF
            c.c = 0
        c.acc, c.p = r >> 32, r & M32
        set_nz32(c, c.acc)                     # SPRU430F SUBCUL: N/Z from ACC (the remainder), not ACC:P
        return npc
    return h


def sem_zalr(c, o, pc, npc):
    get = loc_get(c, o["loc"], False)

    def h():
        v = (get() << 16) | 0x8000
        c.acc = v
        set_nz32(c, v)
        return npc
    return h


def sem_zapa(c, o, pc, npc):
    def h():
        c.acc = 0
        c.p = 0
        c.ovc = 0
        c.n = 0
        c.z = 1
        return npc
    return h


# ============================================================================== 16-bit ALU
def sem_alu16(c, o, pc, npc):
    op, d, s = o["op"], o["d"], o["s"]
    if s == "imm":
        k = o["imm"] & M16
        get = lambda: k
    elif s == "one":
        get = lambda: 1
    elif s == "ax":
        get = (lambda: c.acc >> 16) if o["ax"] else (lambda: c.acc & M16)
    else:
        get = loc_get(c, o["loc"], False)

    def compute(a, b):
        if op == "add":
            r = a + b
            c.c = r >> 16
            r &= M16
            if (a ^ r) & (b ^ r) & 0x8000:
                c.v = 1
            set_nz16(c, r)
            return r
        if op == "sub" or op == "subr":
            if op == "subr":
                a, b = b, a
            r = a - b
            c.c = 0 if r < 0 else 1
            r &= M16
            if (a ^ b) & (a ^ r) & 0x8000:
                c.v = 1
            set_nz16(c, r)
            return r
        if op == "cmp":
            cmp16(c, a, b)
            return None
        if op in ("max", "min"):
            cmp16(c, a, b)
            if (s16(a) < s16(b)) if op == "max" else (s16(a) > s16(b)):
                a = b
            return a
        if op == "and":
            r = a & b
        elif op == "or":
            r = a | b
        else:
            r = a ^ b
        set_nz16(c, r)
        return r

    if d == "ax":
        hi = o["ax"]
        if op == "cmp":
            if hi:
                def h():
                    cmp16(c, c.acc >> 16, get())
                    return npc
            else:
                def h():
                    cmp16(c, c.acc & M16, get())
                    return npc
            return h
        if hi:
            def h():
                r = compute(c.acc >> 16, get())
                c.acc = (c.acc & M16) | (r << 16)
                return npc
        else:
            def h():
                r = compute(c.acc & M16, get())
                c.acc = (c.acc & 0xFFFF0000) | r
                return npc
        return h
    # loc16 destination (read-modify-write); operand order: loc op src
    ea, rd, wr = loc_access(c, o["loc"], False)
    if s == "loc16":
        raise ValueError("loc16,loc16")
    if op == "cmp":
        def h():
            cmp16(c, rd(ea()), get())
            return npc
        return h

    def h():
        a = ea()
        r = compute(rd(a), get())
        wr(a, r)
        return npc
    return h


def sem_and3(c, o, pc, npc):
    get = loc_get(c, o["loc"], False)
    k = o["imm"] & M16
    hi = o["ax"]

    def h():
        r = get() & k
        if hi:
            c.acc = (c.acc & M16) | (r << 16)
        else:
            c.acc = (c.acc & 0xFFFF0000) | r
        set_nz16(c, r)
        return npc
    return h


def sem_regop(c, o, pc, npc):
    r, op, k = o["r"], o["op"], o["imm"] & M16

    def h():
        v = getattr(c, r)
        setattr(c, r, (v & k) if op == "and" else (v | k))
        return npc
    return h


def sem_tbit(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)
    use_t = o.get("t")
    st = o.get("set")
    bit = o.get("bit", 0)
    if st is None:
        if use_t:
            def h():
                b = 15 - ((c.xt >> 16) & 0xF)
                c.tc = (rd(ea()) >> b) & 1
                return npc
        else:
            def h():
                c.tc = (rd(ea()) >> bit) & 1
                return npc
        return h
    m = 1 << bit

    def h():
        a = ea()
        v = rd(a)
        c.tc = 1 if v & m else 0
        v = (v | m) if st else (v & ~m & M16)
        wr(a, v)
        set_nz16(c, v)
        return npc
    return h


def sem_unary(c, o, pc, npc):
    op = o["op"]

    def h():
        a = c.acc
        if op == "test":
            set_nz32(c, a)
            return npc
        if op == "not":
            r = a ^ M32
        elif op in ("neg", "negtc", "abs", "abstc"):
            neg = a >> 31 if op in ("abs", "abstc") else (1 if op == "neg" else c.tc)
            if op == "abstc":
                c.tc ^= a >> 31
            if not neg:
                r = a
            elif a == 0x80000000:
                c.v = 1
                r = 0x7FFFFFFF if c.ovm else a
            else:
                r = (-a) & M32
            c.c = 1 if (op == "neg" and a == 0) else 0
        elif op == "sat":
            if c.ovc > 0:
                r = 0x7FFFFFFF
            elif c.ovc < 0:
                r = 0x80000000
            else:
                r = a
            c.ovc = 0
        else:
            raise ValueError(op)
        c.acc = r
        set_nz32(c, r)
        return npc
    return h


def sem_unary16(c, o, pc, npc):
    op, hi = o["op"], o["ax"]
    sh = 16 if hi else 0

    def h():
        a = (c.acc >> sh) & M16
        if op == "neg":
            r = (-a) & M16
            if a == 0x8000:
                c.v = 1
            c.c = 1 if a == 0 else 0
        elif op == "not":
            r = a ^ M16
        else:  # flip: bit-reverse
            r = int(f"{a:016b}"[::-1], 2)
        c.acc = (c.acc & ~(M16 << sh) & M32) | (r << sh)
        set_nz16(c, r)
        return npc
    return h


def sem_unary64(c, o, pc, npc):
    op = o["op"]
    M64 = 0xFFFFFFFFFFFFFFFF

    def h():
        v = (c.acc << 32) | c.p
        if op == "neg":
            if v == 0x8000000000000000:
                c.v = 1
                r = 0x7FFFFFFFFFFFFFFF if c.ovm else v
            else:
                r = (-v) & M64
            c.c = 1 if v == 0 else 0
        else:  # sat
            if c.ovc > 0:
                r = 0x7FFFFFFFFFFFFFFF
            elif c.ovc < 0:
                r = 0x8000000000000000
            else:
                r = v
            c.ovc = 0
        c.acc, c.p = r >> 32, r & M32
        c.n = r >> 63
        c.z = 1 if r == 0 else 0
        return npc
    return h


def sem_csb(c, o, pc, npc):
    def h():
        a = c.acc
        sign = a >> 31
        n = 0
        for b in range(30, -1, -1):
            if (a >> b) & 1 != sign:
                break
            n += 1
        c.xt = (c.xt & M16) | (n << 16)
        c.tc = sign
        set_nz32(c, a)
        return npc
    return h


def sem_sqr(c, o, pc, npc):
    get = loc_get(c, o["loc"], False)
    add = o["op"] == "add"

    def h():
        p = pshift(c, c.p)
        if add:
            acc_add(c, p)
        else:
            acc_sub(c, p)
        t = get()
        c.xt = (c.xt & M16) | (t << 16)
        st = s16(t)
        c.p = (st * st) & M32
        return npc
    return h


# ============================================================================== shifts
def sem_shacc(c, o, pc, npc):
    op, use_t, longt = o["op"], o.get("t"), o.get("l")
    k = o.get("sh", 0)

    def h():
        a = c.acc
        n = ((c.xt >> 16) & (0x1F if longt else 0xF)) if use_t else k
        if n == 0:
            if use_t:
                c.c = 0
            set_nz32(c, a)
            return npc
        if op == "lsl":
            c.c = (a >> (32 - n)) & 1
            r = (a << n) & M32
        else:
            arith = op == "asr" or (op == "sfr" and c.sxm)
            c.c = (a >> (n - 1)) & 1
            r = ((s32(a) >> n) & M32) if arith else a >> n
        c.acc = r
        set_nz32(c, r)
        return npc
    return h


def sem_shax(c, o, pc, npc):
    op, use_t, hi = o["op"], o.get("t"), o["ax"]
    k = o.get("sh", 0)
    sh = 16 if hi else 0

    def h():
        a = (c.acc >> sh) & M16
        n = ((c.xt >> 16) & 0xF) if use_t else k
        if n == 0:
            if use_t:
                c.c = 0
            set_nz16(c, a)
            return npc
        if op == "lsl":
            c.c = (a >> (16 - n)) & 1 if n <= 16 else 0
            r = (a << n) & M16
        elif op == "lsr":
            c.c = (a >> (n - 1)) & 1
            r = a >> n
        else:
            c.c = (a >> (n - 1)) & 1 if n <= 16 else a >> 15
            r = (s16(a) >> n) & M16
        c.acc = (c.acc & ~(M16 << sh) & M32) | (r << sh)
        set_nz16(c, r)
        return npc
    return h


def sem_sh64(c, o, pc, npc):
    op, use_t = o["op"], o.get("t")
    k = o.get("sh", 0)
    M64 = 0xFFFFFFFFFFFFFFFF

    def h():
        v = (c.acc << 32) | c.p
        n = ((c.xt >> 16) & 0x3F) if use_t else k
        if n == 0:
            c.n = v >> 63
            c.z = 1 if v == 0 else 0
            return npc
        if op == "lsl":
            c.c = (v >> (64 - n)) & 1 if n <= 64 else 0
            r = (v << n) & M64
        elif op == "lsr":
            c.c = (v >> (n - 1)) & 1
            r = v >> n
        else:
            sv = v - (1 << 64) if v >> 63 else v
            c.c = (v >> (n - 1)) & 1 if n <= 64 else v >> 63
            r = (sv >> n) & M64
        c.acc, c.p = r >> 32, r & M32
        c.n = r >> 63
        c.z = 1 if r == 0 else 0
        return npc
    return h


def sem_rot(c, o, pc, npc):
    left = o["op"] == "rol"

    def h():
        a = c.acc
        if left:
            r = ((a << 1) | c.c) & M32
            c.c = a >> 31
        else:
            r = (a >> 1) | (c.c << 31)
            c.c = a & 1
        c.acc = r
        set_nz32(c, r)
        return npc
    return h


def sem_norm(c, o, pc, npc):
    """NORM ACC,XARn++/-- and NORM ACC,*ind (XAR(ARP)): the pointer is modified only when ACC is shifted."""
    if "mod" in o:
        step = _arp_modify(c, o["mod"])
    else:
        inc = o["inc"]
        xar, n = c.xar, o["n"]

        def step():
            xar[n] = (xar[n] + inc) & M32

    def h():
        a = c.acc
        if a != 0 and (a >> 31) == ((a >> 30) & 1):
            a = (a << 1) & M32
            c.acc = a
            c.tc = 0
            step()
        else:
            c.tc = 1
        set_nz32(c, a)
        return npc
    return h


# ============================================================================== multiply / MAC
def _mul16(a: int, b: int, kind: str) -> int:
    if kind == "ss":
        return (s16(a) * s16(b)) & M32
    if kind == "uu":
        return (a * b) & M32
    return (s16(a) * b) & M32                         # su: T signed, operand unsigned


def sem_mpy(c, o, pc, npc):
    d, kind, acc_op = o["d"], o["k"], o.get("acc")
    has_loc = "loc" in o
    get = loc_get(c, o["loc"], False) if has_loc else None
    # forms: (T, loc16) | (loc16, #imm) [T := loc16] | (T, #imm)
    if has_loc and "imm" in o:
        mode = "loc_imm"
    elif has_loc:
        mode = "t_loc"
    else:
        mode = "t_imm"
    kk = o["imm"] & M16 if mode != "t_loc" else 0

    def h():
        if acc_op is not None:
            p = pshift(c, c.p)
            if acc_op == "add":
                acc_add(c, p)
            else:
                acc_sub(c, p)
        if mode == "t_loc":
            t, x = c.xt >> 16, get()
        elif mode == "loc_imm":
            t = get()
            c.xt = (c.xt & M16) | (t << 16)
            x = kk
        else:
            t, x = c.xt >> 16, kk
        r = _mul16(t, x, kind)
        if d == "acc":
            c.acc = r
            set_nz32(c, r)
        else:
            c.p = r
        return npc
    return h


def sem_movt(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)
    acc_op, dmov = o["acc"], o.get("dmov")

    def h():
        a = ea()
        t = rd(a)
        if dmov:
            wr((a + 1) & M22, t)
        c.xt = (c.xt & M16) | (t << 16)
        p = pshift(c, c.p)
        if acc_op == "add":
            acc_add(c, p)
        elif acc_op == "sub":
            acc_sub(c, p)
        else:
            c.acc = p
            set_nz32(c, p)
        return npc
    return h


def sem_mac(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)
    src, dmov = o["src"], o.get("dmov")
    rd16 = c.rd16
    xar = c.xar
    if src == "pma":
        base = o["imm"]
    elif src == "xpma":
        base = 0x3F0000 | o["imm"]
    else:
        base = None

    def one(paddr):
        p = pshift(c, c.p)
        acc_add(c, p)
        a = ea()
        t = rd(a)
        if dmov:
            wr((a + 1) & M22, t)
        c.xt = (c.xt & M16) | (t << 16)
        c.p = (s16(t) * s16(rd16(paddr & M22))) & M32

    def h():
        if base is not None:
            one(base)
        else:
            one(xar[7])
            if src == "xar7inc":
                xar[7] = (xar[7] + 1) & M32
        return npc

    def rep(n):
        if base is not None:
            for i in range(n):
                one(base + i)
        elif src == "xar7inc":
            for _ in range(n):
                one(xar[7])
                xar[7] = (xar[7] + 1) & M32
        else:
            b = xar[7]
            for i in range(n):
                one(b + i)
        return npc
    h.rpt = rep
    return h


def _mul32(a: int, b: int, kind: str) -> int:
    if kind == "ss":
        return s32(a) * s32(b)
    if kind == "uu":
        return a * b
    return s32(a) * b


def sem_mpyl(c, o, pc, npc):
    get = loc_get(c, o["loc"], True)
    d, kind, hi, acc_op = o["d"], o["k"], o["hi"], o.get("acc")

    def h():
        if acc_op is not None:
            p = pshift(c, c.p) if hi else c.p
            if acc_op in ("add", "addu"):
                acc_add(c, p)
            else:
                acc_sub(c, p)
        prod = _mul32(c.xt, get(), kind)
        r = ((prod >> 32) if hi else prod) & M32
        if d == "acc":
            c.acc = r
            set_nz32(c, r)
        else:
            c.p = r
        return npc
    return h


def sem_macl(c, o, pc, npc):
    get = loc_get(c, o["loc"], True)
    hi, inc = o["hi"], o["inc"]
    xar, rd32 = c.xar, c.rd32

    def h():
        acc_add(c, pshift(c, c.p) if hi else c.p)
        x = get()
        c.xt = x
        prod = s32(x) * s32(rd32(xar[7] & M22))
        if inc:
            xar[7] = (xar[7] + 2) & M32
        c.p = ((prod >> 32) if hi else prod) & M32
        return npc
    return h


def sem_dmac(c, o, pc, npc):
    get = loc_get(c, o["loc"], True)
    inc = o["inc"]
    xar, rd32 = c.xar, c.rd32

    def h():
        x = get()
        y = rd32(xar[7] & M22)
        if inc:
            xar[7] = (xar[7] + 2) & M32
        hi = s16(x >> 16) * s16(y >> 16)
        lo = s16(x & M16) * s16(y & M16)
        acc_add(c, pshift(c, hi & M32))
        c.p = (c.p + pshift(c, lo & M32)) & M32
        return npc
    return h


# ============================================================================== address registers / SP
def sem_spadd(c, o, pc, npc):
    k = o["imm"] * o["sign"]

    def h():
        c.sp = (c.sp + k) & M16
        return npc
    return h


def sem_xaradd(c, o, pc, npc):
    k = o["imm"] * o["sign"]
    xar = c.xar
    if o.get("arp"):
        def h():
            n = c.arp
            xar[n] = (xar[n] + k) & M32
            return npc
    else:
        n = o["n"]

        def h():
            xar[n] = (xar[n] + k) & M32
            return npc
    return h


def sem_cmpr(c, o, pc, npc):
    k = o["k"]
    xar = c.xar

    def h():
        a, b = xar[c.arp] & M16, xar[0] & M16
        c.tc = int(a == b if k == 0 else a < b if k == 1 else a > b if k == 2 else a != b)
        return npc
    return h


# ============================================================================== control flow
def sem_b(c, o, pc, npc):
    t = o["target"]
    k = o["cond"]
    if k == 15:
        return lambda: t
    if k == 0:
        return lambda: npc if c.z else t
    if k == 1:
        return lambda: t if c.z else npc
    if k == 2:
        return lambda: npc if (c.z or c.n) else t
    if k == 3:
        return lambda: npc if c.n else t
    if k == 4:
        return lambda: t if c.n else npc
    if k == 5:
        return lambda: t if (c.z or c.n) else npc
    if k == 12:
        return lambda: npc if c.tc else t
    if k == 13:
        return lambda: t if c.tc else npc
    cond = make_cond(c, k)
    return lambda: t if cond() else npc


def sem_banz(c, o, pc, npc):
    t, n, xar = o["target"], o["n"], c.xar

    def h():
        v = xar[n]
        xar[n] = (v & 0xFFFF0000) | ((v - 1) & M16)
        return t if v & M16 else npc
    return h


def sem_bar(c, o, pc, npc):
    t, n, m, eq, xar = o["target"], o["n"], o["m"], o["eq"], c.xar

    def h():
        e = (xar[n] & M16) == (xar[m] & M16)
        return t if e == bool(eq) else npc
    return h


def sem_lb(c, o, pc, npc):
    t = o["addr"]
    return lambda: t


def sem_lb_xar7(c, o, pc, npc):
    xar = c.xar
    return lambda: xar[7] & M22


def sem_lc(c, o, pc, npc):
    xar = c.xar
    if o.get("xar7"):
        def h():
            push2x16(c, npc)
            return xar[7] & M22
        return h
    t = o["addr"]

    def h():
        push2x16(c, npc)
        return t
    return h


def sem_lcr(c, o, pc, npc):
    xar = c.xar
    ind = o.get("ind")
    n = o.get("n", 0)
    t = o.get("addr", 0)

    def h():
        push2x16(c, c.rpc)
        c.rpc = npc
        return (xar[n] & M22) if ind else t
    return h


def sem_ffc(c, o, pc, npc):
    t, xar = o["addr"], c.xar

    def h():
        xar[7] = npc
        return t
    return h


def sem_lret(c, o, pc, npc):
    eint = o.get("eint")

    def h():
        t = pop2x16(c) & M22
        if eint:
            c.intm = 0
        return t
    return h


def sem_lretr(c, o, pc, npc):
    def h():
        t = c.rpc
        c.rpc = pop2x16(c) & M22
        return t
    return h


# C2xLP-compatible flow control (SPRU430F XB/XCALL/XRETC/XBANZ): targets are 0x3F:16bit, the return address is a
# single 16-bit stack word.
def sem_xb(c, o, pc, npc):
    if o.get("al"):
        return lambda: 0x3F0000 | (c.acc & M16)
    t = 0x3F0000 | o["imm"]
    if "arp" in o or "n" in o:                       # XB pma,*,ARPn
        n = o["n"]

        def h():
            c.arp = n
            return t
        return h
    cond = make_cond(c, o["cond"])
    if cond is None:
        return lambda: t
    nt = 0x3F0000 | (npc & M16)                      # "PC(21:16) = 0x3F" also when not taken
    return lambda: t if cond() else nt


def sem_xcall(c, o, pc, npc):
    al = o.get("al")
    t = 0x3F0000 | o.get("imm", 0)
    arp = o.get("n") if o.get("arp") else None
    cond = None if (al or arp is not None) else make_cond(c, o["cond"])

    def h():
        if cond is not None and not cond():
            return npc
        sp = c.sp
        c.wr16(sp, npc & M16)
        c.sp = (sp + 1) & M16
        if arp is not None:
            c.arp = arp
        return (0x3F0000 | (c.acc & M16)) if al else t
    return h


def sem_xretc(c, o, pc, npc):
    cond = make_cond(c, o["cond"])

    def h():
        if cond is not None and not cond():
            return npc
        sp = (c.sp - 1) & M16
        c.sp = sp
        return 0x3F0000 | c.rd16(sp)
    return h


def _arp_modify(c, mod: str):
    """Post-modification of XAR(ARP) for the C2xLP '*', '*++', '*--', '*0++', '*0--' operand forms."""
    xar = c.xar
    if mod == "*":
        return lambda: None
    if mod == "*++":
        def f():
            n = c.arp
            xar[n] = (xar[n] + 1) & M32
    elif mod == "*--":
        def f():
            n = c.arp
            xar[n] = (xar[n] - 1) & M32
    elif mod == "*0++":
        def f():
            n = c.arp
            xar[n] = (xar[n] + (xar[0] & M16)) & M32
    elif mod == "*0--":
        def f():
            n = c.arp
            xar[n] = (xar[n] - (xar[0] & M16)) & M32
    else:
        raise ValueError(mod)
    return f


def sem_xbanz(c, o, pc, npc):
    """XBANZ pma,*ind{,ARPn}: branch to 0x3F:pma if AR(ARP) != 0; then modify XAR(ARP); then ARP = n."""
    t, xar = 0x3F0000 | o["imm"], c.xar
    mod = _arp_modify(c, o["mod"])
    n = o.get("n") if o.get("arp") else None

    def h():
        taken = xar[c.arp] & M16
        mod()
        if n is not None:
            c.arp = n
        return t if taken else npc
    return h


def sem_loopnz(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)
    mask, z = o["imm"] & M16, o["z"]

    def h():
        v = rd(ea()) & mask
        set_nz16(c, v)
        if (v == 0) == bool(z):
            c.loop = 1
            return pc
        c.loop = 0
        return npc
    return h


def sem_rpt(c, o, pc, npc):
    get = loc_get(c, o["loc"], False) if "loc" in o else None
    k = o.get("imm", 0)

    def h():
        f = c._fetch(npc)                       # first: a stop request raised here leaves no side effects
        n = (get() if get is not None else k) + 1
        if getattr(f, "norpt", False):
            c._extra += 1
            return f()
        rep = getattr(f, "rpt", None)
        c._extra += n
        if rep is not None:
            return rep(n)
        r = npc
        for _ in range(n):
            r = f()
        return r
    return h


def sem_trap(c, o, pc, npc):
    n = o["imm"]

    def h():
        return c._sw_interrupt(n, n, npc)
    return h


def sem_intr(c, o, pc, npc):
    # field = vector - 1 (INT1..14 = 0..13, DLOGINT = 14 -> vector 15, RTOSINT = 15 -> vector 16); NMI = 18 and
    # EMUINT = 17 come from the table (`vec`)
    num = o["vec"] if "vec" in o else o["int"] + 1

    def h():
        return c._sw_interrupt(num, num, npc, maskable=num <= 16)
    return h


def sem_iret(c, o, pc, npc):
    def h():
        return c._iret()
    return h


def sem_idle(c, o, pc, npc):
    def h():
        c.idlestat = 1
        raise StopExc("idle", npc)
    return h


def sem_estop(c, o, pc, npc):
    def h():
        raise StopExc("estop", npc)
    return h


def sem_io(c, o, pc, npc):
    def h():
        raise Fault("io_space_unsupported", msg="IN/OUT/UOUT access the I/O space, which is not modelled")
    return h


# ============================================================================== status
_MODE_FLAGS = ((0x01, "sxm"), (0x02, "ovm"), (0x04, "tc"), (0x08, "c"), (0x10, "intm"), (0x20, "dbgm"),
               (0x40, "page0"), (0x80, "vmap"))


def sem_setc(c, o, pc, npc):
    m = o.get("mode", o.get("m", 0))
    names = [n for b, n in _MODE_FLAGS if m & b]
    if names == ["intm"]:
        def h():
            c.intm = 1
            return npc
        return h

    def h():
        for n in names:
            setattr(c, n, 1)
        return npc
    return h


def sem_clrc(c, o, pc, npc):
    m = o.get("mode", o.get("m", 0))
    names = [n for b, n in _MODE_FLAGS if m & b]

    def h():
        for n in names:
            setattr(c, n, 0)
        return npc
    return h


def sem_st1bit(c, o, pc, npc):
    f, v = o["f"], o["v"]
    if f == "amode":
        def h():
            if c.amode != v:
                c.amode = v
                c.flush_code_cache()          # loc decoding depends on AMODE
            return npc
        return h

    def h():
        setattr(c, f, v)
        return npc
    return h


def sem_spm(c, o, pc, npc):
    f = 1 - o["pm"]

    def h():
        c.pm = f
        return npc
    return h


def sem_asp(c, o, pc, npc):
    def h():
        if c.sp & 1:
            c.sp = (c.sp + 1) & M16
            c.spa = 1
        else:
            c.spa = 0
        return npc
    return h


def sem_nasp(c, o, pc, npc):
    def h():
        if c.spa:
            c.sp = (c.sp - 1) & M16
            c.spa = 0
        return npc
    return h


def sem_nop(c, o, pc, npc):
    return lambda: npc


def sem_nop_ind(c, o, pc, npc):
    ea, rd, wr = loc_access(c, o["loc"], False)

    def h():
        ea()
        return npc
    return h


SEMANTICS: dict[str, Callable] = {k[4:]: v for k, v in list(globals().items()) if k.startswith("sem_")}
