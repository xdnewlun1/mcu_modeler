"""FPU32 (C28x+FPU; SPRUEO2B / SPRUHS1C chapter 1) semantics.  R0H..R7H are kept as raw IEEE-754 single bit patterns (c.r[i]).

Modelling choices: denormal inputs are treated as zero; results below the smallest normal flush to ±0 (LUF=1);
overflow gives ±infinity (LVF=1); STF.RND32=1 rounds to nearest-even, RND32=0 truncates toward zero.  The
pipeline delay slots of 2p instructions are not modelled (results are visible to the next instruction).
EINVF32/EISQRTF32 return the correctly rounded value instead of an 8-bit estimate (Newton-Raphson code converges
the same way).
"""
from __future__ import annotations

import math
import struct

from .sem import M16, M22, M32, loc_access, loc_get, loc_set, s16, s32

STF_LVF, STF_LUF, STF_NF, STF_ZF, STF_NI, STF_ZI, STF_TF = 1, 2, 4, 8, 16, 32, 64
STF_RND32 = 1 << 9
STF_SHDWS = 1 << 31
_F32_MAX = 3.4028234663852886e38
_F32_MIN_NORMAL = 1.1754943508222875e-38

_pack_f = struct.Struct("<f").pack
_unpack_f = struct.Struct("<f").unpack
_pack_I = struct.Struct("<I").pack
_unpack_I = struct.Struct("<I").unpack


def latch(c, bits: int) -> None:
    """Set latched STF flags (LVF/LUF).  A 0 -> 1 change of LVF or LUF is reported to `c.fpu_flag_cb(bit)` (the
    arch pulses PIE 12.7 LVF / 12.8 LUF on F2833x parts, SPRS439Q Table 8-32; SPRUEO2B 2.1.1: the flags "may also be
    connected to the PIE")."""
    new = bits & ~c.stf
    c.stf |= bits
    if new & (STF_LVF | STF_LUF):
        cb = getattr(c, "fpu_flag_cb", None)
        if cb is not None:
            for b in (STF_LVF, STF_LUF):
                if new & b:
                    cb(b)


def _stf_store(c, value: int) -> None:
    """STF := value (MOV32 STF,mem32 / RESTORE / SETFLG), reporting LVF/LUF rising edges like `latch`."""
    rise = value & ~c.stf & (STF_LVF | STF_LUF)
    c.stf = value
    if rise:
        cb = getattr(c, "fpu_flag_cb", None)
        if cb is not None:
            for b in (STF_LVF, STF_LUF):
                if rise & b:
                    cb(b)


def fval(b: int) -> float:
    """Value of a register bit pattern; denormals read as signed zero."""
    if not b & 0x7F800000:
        return -0.0 if b & 0x80000000 else 0.0
    return _unpack_f(_pack_I(b & M32))[0]


def fbits(c, x: float) -> int:
    """Round a Python float to single precision per STF.RND32 and update LVF/LUF."""
    if x != x:
        return 0x7FC00000
    ax = abs(x)
    if ax == math.inf or ax > _F32_MAX:
        if ax != math.inf or True:
            latch(c, STF_LVF)
        return 0xFF800000 if x < 0 else 0x7F800000
    if ax != 0.0 and ax < _F32_MIN_NORMAL:
        latch(c, STF_LUF)
        return 0x80000000 if x < 0 else 0
    try:
        b = _unpack_I(_pack_f(x))[0]
    except OverflowError:
        latch(c, STF_LVF)
        return 0xFF800000 if x < 0 else 0x7F800000
    if not c.stf & STF_RND32:
        y = _unpack_f(_pack_I(b))[0]
        if abs(y) > ax:
            b -= 1
    if (b & 0x7F800000) == 0x7F800000:
        latch(c, STF_LVF)
    return b & M32


def _cndf(c, k: int):
    """FPU condition on STF (None = UNC/UNCF)."""
    if k in (14, 15):
        return None
    tests = {
        0: lambda s: not s & STF_ZF, 1: lambda s: s & STF_ZF, 2: lambda s: not s & (STF_ZF | STF_NF),
        3: lambda s: not s & STF_NF, 4: lambda s: s & STF_NF, 5: lambda s: s & (STF_ZF | STF_NF),
        10: lambda s: s & STF_TF, 11: lambda s: not s & STF_TF, 12: lambda s: s & STF_LUF, 13: lambda s: s & STF_LVF,
    }
    t = tests.get(k, lambda s: 0)
    return lambda: bool(t(c.stf))


def _flags_mov(c, v: int) -> None:
    """NF/ZF/NI/ZI update of MOV32 ...,UNCF style loads."""
    s = c.stf & ~(STF_NF | STF_ZF | STF_NI | STF_ZI)
    if not v & 0x7F800000:
        s |= STF_ZF
    elif v & 0x80000000:
        s |= STF_NF
    if v & 0x80000000:
        s |= STF_NI
    if v == 0:
        s |= STF_ZI
    c.stf = s


def _flags_nz(c, v: int) -> None:
    s = c.stf & ~(STF_NF | STF_ZF)
    if not v & 0x7F800000:
        s |= STF_ZF
    elif v & 0x80000000:
        s |= STF_NF
    c.stf = s


def sem_f_ld32(c, o, pc, npc):
    get = loc_get(c, o["loc"], True)
    r, a = c.r, o["ra"]
    k = o.get("cond", 15)
    cond = _cndf(c, k)
    if o.get("dmov"):
        ea, rd, wr = loc_access(c, o["loc"], True)

        def h():
            ad = ea()
            v = rd(ad)
            r[a] = v
            wr((ad + 2) & M22, v)
            return npc
        return h

    def h():
        v = get()
        if cond is None or cond():
            r[a] = v
        if k == 15:
            _flags_mov(c, v)
        return npc
    return h


def sem_f_mvloc(c, o, pc, npc):
    """MOV32 RaH,{ACC|P|XT|XARn} (to: f; no STF flags) / MOV32 {ACC|P|XT|XARn},RaH (to: r; ST0 N/Z when the
    destination is ACC).  TI's encoding: MOV32 *(0:0x0F12+4a),loc32 / MOV32 loc32,*(0:0x0F12+4a)."""
    code = o["loc"]
    get = loc_get(c, code, True)
    put = loc_set(c, code, True)
    r, a = c.r, o["ra"]
    if o["to"] == "f":
        def h():
            r[a] = get()
            return npc
        return h
    if code == 0xA9:
        def h():
            v = r[a]
            put(v)
            c.n = v >> 31
            c.z = 1 if v == 0 else 0
            return npc
        return h

    def h():
        put(r[a])
        return npc
    return h


def sem_f_st32(c, o, pc, npc):
    put = loc_set(c, o["loc"], True)
    r, a = c.r, o["ra"]

    def h():
        put(r[a])
        return npc
    return h


def sem_f_st16(c, o, pc, npc):
    put = loc_set(c, o["loc"], False)
    r, a = c.r, o["ra"]

    def h():
        put(r[a] & M16)
        return npc
    return h


def sem_f_stf(c, o, pc, npc):
    if o["st"]:
        put = loc_set(c, o["loc"], True)

        def h():
            put(c.stf)
            return npc
    else:
        get = loc_get(c, o["loc"], True)

        def h():
            _stf_store(c, get() & (0x7F | STF_RND32 | STF_SHDWS))
            return npc
    return h


def _int_to_f(c, kind: str, v: int) -> int:
    if kind == "i16":
        x = float(s16(v & M16))
    elif kind == "u16":
        x = float(v & M16)
    elif kind == "i32":
        x = float(s32(v & M32))
    else:
        x = float(v & M32)
    return fbits(c, x)


def sem_f_cvtmem(c, o, pc, npc):
    kind = o["k"]
    get = loc_get(c, o["loc"], kind in ("i32", "u32"))
    r, a = c.r, o["ra"]

    def h():
        r[a] = _int_to_f(c, kind, get())
        return npc
    return h


def sem_f_mvreg(c, o, pc, npc):
    reg, to_f = o["r"], o["to"] == "f"
    r, a, xar = c.r, o["ra"], c.xar
    n = o.get("n", 0)
    if reg == "xar":
        if to_f:
            def h():
                r[a] = xar[n]
                return npc
        else:
            def h():
                xar[n] = r[a]
                return npc
        return h
    if to_f:
        def h():
            r[a] = getattr(c, reg)
            return npc
    elif reg == "acc":                               # MOV32 ACC,RaH: ST0 N and Z follow ACC (SPRUHS1C)
        def h():
            v = r[a]
            c.acc = v
            c.n = v >> 31
            c.z = 1 if v == 0 else 0
            return npc
    else:
        def h():
            setattr(c, reg, r[a])
            return npc
    return h


def sem_f_mov(c, o, pc, npc):
    r, a, b = c.r, o["ra"], o["rb"]
    k = o.get("cond", 15)
    cond = _cndf(c, k)

    def h():
        if cond is None or cond():
            r[a] = r[b]
        if k == 15:
            _flags_mov(c, r[a])
        return npc
    return h


def sem_f_imm(c, o, pc, npc):
    r, a, k = c.r, o["ra"], o["imm"] & M16
    if o["k"] == "iz":
        v = k << 16

        def h():
            r[a] = v
            return npc
    else:
        def h():
            r[a] = (r[a] & 0xFFFF0000) | k
            return npc
    return h


def _cmp_flags(c, x: float, y: float) -> None:
    s = c.stf & ~(STF_NF | STF_ZF)
    if x == y:
        s |= STF_ZF
    elif x < y:
        s |= STF_NF
    c.stf = s


def sem_f_cmp(c, o, pc, npc):
    r, a = c.r, o["ra"]
    if o.get("zero"):
        yget = lambda: 0.0
    elif "imm" in o:
        y = fval(o["imm"] << 16)
        yget = lambda: y
    else:
        b = o["rb"]
        yget = lambda: fval(r[b])

    def h():
        _cmp_flags(c, fval(r[a]), yget())
        return npc
    return h


def sem_f_minmax(c, o, pc, npc):
    r, a = c.r, o["ra"]
    mx = o["op"] == "max"
    if "imm" in o:
        yb = o["imm"] << 16
        yget = lambda: yb
    else:
        b = o["rb"]
        yget = lambda: r[b]

    def h():
        x, yb_ = fval(r[a]), yget()
        y = fval(yb_)
        _cmp_flags(c, x, y)
        if (x < y) if mx else (x > y):
            r[a] = yb_
        return npc
    return h


def sem_f_arith(c, o, pc, npc):
    r, a, b = c.r, o["ra"], o["rb"]
    op = o["op"]
    if "imm" in o:
        k = fval(o["imm"] << 16)
        xget = lambda: k
        yget = lambda: fval(r[b])
    else:
        cc = o["rc"]
        xget = lambda: fval(r[b])
        yget = lambda: fval(r[cc])

    def h():
        x, y = xget(), yget()
        if op == "add":
            v = x + y
        elif op == "sub":
            v = x - y
        else:
            v = x * y
        r[a] = fbits(c, v)
        return npc
    return h


def sem_f_mac(c, o, pc, npc):
    r = c.r
    d, e, f = o["rd"], o["re"], o["rf"]

    def h():
        r3 = fbits(c, fval(r[3]) + fval(r[2]))
        r[d] = fbits(c, fval(r[e]) * fval(r[f]))
        r[3] = r3
        return npc
    return h


def sem_f_macmem(c, o, pc, npc):
    """MACF32 R7H,R3H,mem32,*XAR7++ (SPRUHS1C): R3H = R3H + R2H, R2H = [mem32] * [XAR7++].  Under RPT the
    accumulate alternates: odd cycles use R3H/R2H, even cycles R7H/R6H."""
    r, xar, rd32 = c.r, c.xar, c.rd32
    ea, rd, wr = loc_access(c, o["loc"], True)

    def one(acc, tmp):
        x = fval(rd(ea()))
        y = fval(rd32(xar[7] & M22))
        xar[7] = (xar[7] + 2) & M32
        va = fbits(c, fval(r[acc]) + fval(r[tmp]))
        r[tmp] = fbits(c, x * y)
        r[acc] = va

    def h():
        one(3, 2)
        return npc

    def rep(n):
        for i in range(n):
            if i & 1:
                one(7, 6)
            else:
                one(3, 2)
        return npc
    h.rpt = rep
    return h


def _f2int(x: float, lo: int, hi: int, rnd: bool) -> int:
    if x != x:
        return 0
    if rnd:
        y = math.floor(abs(x) + 0.5)
        y = -y if x < 0 else y
    else:
        y = math.trunc(x) if math.isfinite(x) else (hi if x > 0 else lo)
    return max(lo, min(hi, int(y)))


def sem_f_cvt(c, o, pc, npc):
    r, a, b = c.r, o["ra"], o["rb"]
    k = o["k"]
    kc = o.get("cond", 15)
    cond = _cndf(c, kc)                              # NEGF32 RaH,RbH,CNDF: RaH = cond ? -RbH : RbH

    def h():
        v = r[b]
        if k in ("i16", "u16", "i32", "u32"):
            r[a] = _int_to_f(c, k, v)
            return npc
        x = fval(v)
        if k == "f2i32":
            res = _f2int(x, -(1 << 31), (1 << 31) - 1, False) & M32
        elif k == "f2u32":
            res = _f2int(x, 0, M32, False)
        elif k in ("f2i16", "f2i16r"):
            res = _f2int(x, -32768, 32767, k.endswith("r")) & M32
        elif k in ("f2u16", "f2u16r"):
            res = _f2int(x, 0, M16, k.endswith("r"))
        elif k == "frac":
            res = fbits(c, math.fmod(x, 1.0)) if math.isfinite(x) else 0
        elif k == "abs":
            res = v & 0x7FFFFFFF
            _flags_nz(c, res)
        elif k == "neg":
            res = (v ^ 0x80000000) & M32 if (cond is None or cond()) else v
            if kc == 15:
                _flags_nz(c, res)
        elif k == "inv":
            if x == 0.0:
                latch(c, STF_LVF)
                res = 0xFF800000 if v & 0x80000000 else 0x7F800000
            else:
                res = fbits(c, 1.0 / x)
        else:  # isqrt
            if x <= 0.0:
                latch(c, STF_LVF)
                res = 0x7F800000
            else:
                res = fbits(c, 1.0 / math.sqrt(x))
        r[a] = res
        return npc
    return h


def sem_f_swap(c, o, pc, npc):
    r, a, b = c.r, o["ra"], o["rb"]
    cond = _cndf(c, o.get("cond", 15))

    def h():
        if cond is None or cond():
            r[a], r[b] = r[b], r[a]
        return npc
    return h


def sem_f_testtf(c, o, pc, npc):
    cond = _cndf(c, o["cond"])

    def h():
        if cond is None or cond():
            c.stf |= STF_TF
        else:
            c.stf &= ~STF_TF
        return npc
    return h


def sem_f_restore(c, o, pc, npc):
    """RESTORE: R0H-R7H and STF from the shadow set; STF.SHDWS is forced to 0."""
    def h():
        if c.fshadow is not None:
            regs, stf = c.fshadow
            c.r[:] = regs
            _stf_store(c, stf & ~STF_SHDWS)
        else:
            c.stf &= ~STF_SHDWS
        return npc
    return h


_SETFLG_BITS = (0, 1, 2, 3, 4, 5, 6, 9)             # FLAG/VALUE bit = STF bit: LVF LUF NF ZF NI ZI TF, RNDF32


def _setflg_apply(c, mask: int, val: int) -> None:
    s = c.stf
    for bit in _SETFLG_BITS:
        if mask >> bit & 1:
            s = (s | (1 << bit)) if val >> bit & 1 else (s & ~(1 << bit))
    _stf_store(c, s)


def sem_f_setflg(c, o, pc, npc):
    """SETFLG FLAG,VALUE; SAVE FLAG,VALUE copies R0H-R7H/STF to the shadow set first and sets STF.SHDWS."""
    mask, val = o["flagset"]
    save = o.get("save")

    def h():
        if save:
            c.fshadow = (list(c.r), c.stf)
            c.stf |= STF_SHDWS
        _setflg_apply(c, mask, val)
        return npc
    return h


def sem_f_zero(c, o, pc, npc):
    r = c.r
    if o.get("all"):
        def h():
            for i in range(8):
                r[i] = 0
            return npc
        return h
    a = o["ra"]

    def h():
        r[a] = 0
        return npc
    return h


def sem_f_movst0(c, o, pc, npc):
    """MOVST0 FLAG (SPRUHS1C): V = LVF|LUF, N = NF|NI, Z = ZF|ZI, C = TC = TF, for the selected flags only;
    selected LVF/LUF are cleared.  The FLAG field (isa.MOVST0_BITS) is mapped to the STF bit mask first."""
    from .isa import FFLAG_BITS, MOVST0_BITS
    f = o["fflags"]
    m = 0
    for name, bit in MOVST0_BITS.items():
        if f & bit:
            m |= FFLAG_BITS[name]

    def h():
        s = c.stf
        if m & STF_TF:
            c.tc = c.c = 1 if s & STF_TF else 0
        if m & (STF_ZF | STF_ZI):
            c.z = 1 if s & m & (STF_ZF | STF_ZI) else 0
        if m & (STF_NF | STF_NI):
            c.n = 1 if s & m & (STF_NF | STF_NI) else 0
        if m & (STF_LVF | STF_LUF):
            c.v = 1 if s & m & (STF_LVF | STF_LUF) else 0
            c.stf = s & ~(m & (STF_LVF | STF_LUF))
        return npc
    return h


def sem_f_par(c, o, pc, npc):
    """Parallel instructions (SPRUHS1C).  `p` = "<op1>_<op2>":
    mpy_add / mpy_sub   MPYF32 RaH,RbH,RcH || ADDF32/SUBF32 RdH,ReH,RfH  (both read the old register values)
    <op>_ld             <op>F32 RdH,ReH,RfH || MOV32 RaH,mem32   (op = mpy/add/sub; MOV32 sets NF/ZF/NI/ZI)
    <op>_st             <op>F32 RdH,ReH,RfH || MOV32 mem32,RaH   (stores the old RaH)
    mac3_ld / mac7_ld   MACF32 R3H,R2H,RdH,ReH,RfH || MOV32 RaH,mem32 (R3H += R2H, RdH = ReH*RfH; R7H/R6H form)
    max_mov / min_mov   MAXF32/MINF32 RaH,RbH || MOV32 RcH,RdH (RcH = RdH only when RaH is replaced)"""
    r = c.r
    kind = o["p"]
    op1, op2 = kind.split("_")
    g = o.get

    def arith(op, x, y):
        if op == "add":
            return fbits(c, fval(x) + fval(y))
        if op == "sub":
            return fbits(c, fval(x) - fval(y))
        return fbits(c, fval(x) * fval(y))

    if op1 in ("max", "min"):
        a, b, cc, d = g("ra"), g("rb"), g("rc"), g("rd")
        mx = op1 == "max"

        def h():
            x, y = fval(r[a]), fval(r[b])
            _cmp_flags(c, x, y)
            if (x < y) if mx else (x > y):
                r[a] = r[b]
                r[cc] = r[d]
            return npc
        return h
    if op1 == "mpy" and op2 in ("add", "sub"):
        a, b, cc, d, e, f = g("ra"), g("rb"), g("rc"), g("rd"), g("re"), g("rf")

        def h():
            v1 = arith("mpy", r[b], r[cc])
            v2 = arith(op2, r[e], r[f])
            r[a] = v1
            r[d] = v2
            return npc
        return h
    a, d, e, f = g("ra"), g("rd"), g("re"), g("rf")
    w32 = True
    if op2 == "ld":
        get = loc_get(c, o["loc"], w32)
    else:
        put = loc_set(c, o["loc"], w32)
    if op1 in ("mac3", "mac7"):
        acc, add = (3, 2) if op1 == "mac3" else (7, 6)

        def calc():
            va = fbits(c, fval(r[acc]) + fval(r[add]))
            vm = fbits(c, fval(r[e]) * fval(r[f]))
            r[acc] = va
            r[d] = vm
    else:
        def calc():
            r[d] = arith(op1, r[e], r[f])
    if op2 == "ld":
        def h():
            v = get()
            calc()
            r[a] = v
            _flags_mov(c, v)
            return npc
    else:
        def h():
            put(r[a])
            calc()
            return npc
    return h


def sem_f_rb(c, o, pc, npc):
    if o["push"]:
        def h():
            c.push32(c.rb)
            return npc
    else:
        def h():
            c.rb = c.pop32()
            return npc
    return h


RB_RAS, RB_RA = 1 << 31, 1 << 30


def sem_f_rptb(c, o, pc, npc):
    """RPTB end,#RC / RPTB end,loc16 (SPRUHS1C): RB = RAS | RA | RSIZE(29:23) | RE(22:16) | RC(15:0) with
    RE = low 7 bits of (pc + 1 + RSIZE) (the last word of the block) and RA = (RC != 0).  The loop-back itself is done
    by the core when execution reaches the word after the block (C28xCore._rptb_wrap)."""
    end = o["end"]
    size = (end - npc) & 0x7F
    re_ = (pc + 1 + size) & 0x7F
    get = loc_get(c, o["loc"], False) if "loc" in o else None
    k = o.get("imm", 0) & M16

    def h():
        rc = get() & M16 if get is not None else k
        rb = (c.rb & RB_RAS) | (size << 23) | (re_ << 16) | rc
        if rc:
            rb |= RB_RA
        c.rb = rb
        c._rptb_arm(end)
        return npc
    return h


SEMANTICS = {k[4:]: v for k, v in list(globals().items()) if k.startswith("sem_")}
