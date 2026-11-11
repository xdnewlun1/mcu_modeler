"""FPU32 on the TMS320F28335: TI-compiled C (cl2000 25.11.1, --float_support=fpu32) run on the interpreter and
compared with IEEE single precision computed on the host.

firmware/samples_c2000_c/f28335_fpu.c (+ f28335_fpu_asm.asm): FPU arithmetic, conversions (F32TOI32/UI32, I32/UI32/
I16TOF32, F32TOI16), CMPF32 + MOVST0 compares, MINF32/MAXF32, ABSF32/NEGF32, RPTB loops with RPT || MACF32 (dot
product and an 8-tap FIR), an RPTB #imm Horner loop, STF.RND32 truncation, LVF/LUF in STF and MOVST0 -> ST0.V.
Host expectations: Python floats rounded to float32 with struct (exact for +, -, *, / and sqrt of float32 operands:
double has more than 2 x 24 + 2 bits, so the double rounding is innocuous), round to nearest even (TI's _c_int00
sets RNDF32 = 1).  f28335_pid.c: an ADC-driven float PI loop in an `interrupt` function (FPU context save) while
main runs float code; boards/f28335_fpu_loop.yaml closes the loop with a scripted plant.
Skipped when the images are not built (firmware/samples_c2000_c/build.sh f28335_fpu f28335_pid)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "firmware" / "samples_c2000_c" / "build"
BOARD = ROOT / "boards" / "f28335_fpu_loop.yaml"


def _img(name):
    p = BUILD / name
    if not p.exists():
        pytest.skip(f"{p.name} not built (firmware/samples_c2000_c/build.sh f28335_fpu f28335_pid)")
    return p


def f(x: float) -> float:
    """Round to float32 (nearest even)."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def fb(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def trunc32(x: float) -> float:
    """x (exact in double) rounded toward zero to float32 (STF.RND32 = 0)."""
    r = f(x)
    if abs(r) > abs(x):
        r = struct.unpack("<f", struct.pack("<I", fb(r) - 1))[0]
    return r


def fpu_expected() -> list[int]:
    f1, f2, f3, f4, f5 = f(3.25), f(-1.5), f(1.0e-3), f(0.1), f(7.0 / 3.0)
    neg, two = f(-2.75), f(2.0)
    m32 = 0xFFFFFFFF
    e = [fb(f1 * f2), fb(f1 + f2), fb(f1 - f3),
         0xC00AAAAA,                               # FS$$DIV: SUBCU quotient, 1 ulp below IEEE (test_c2000_compiled)
         fb(f(f5 * f4) + f3), fb(f(two ** 0.5)), fb(-neg), fb(-f4),
         int(f(f1 * f(1000.0))) & m32, int(f(neg * f(100.7))) & m32, 3000000000,
         fb(-1234567890), fb(16777217), fb(16777219), fb(4000000000), int(f(f2 * 3.0)) & 0xFFFF,
         0, 1, 1, 0, fb(max(f1, f2)), fb(min(f1, f2)), fb(-321.0), fb(f5),
         fb(trunc32(f5 * f4)), fb(f5 * f4), 0x7F800000,
         0x201,                                    # STF: RNDF32 (bit 9) | LVF (bit 0)
         0x202,                                    # STF: RNDF32 | LUF (bit 1)
         None]                                     # MOVST0: checked separately
    xs = [f((i % 7) - 3.0) for i in range(40)]
    ys = [f(0.5 * (i % 5) + 0.25) for i in range(40)]
    for k in (1, 2, 5, 16, 31, 0):
        e.append(fb(sum(xs[i] * ys[i] for i in range(k))))       # exact: every partial sum is representable
    coef = [f((i + 1) * 0.125) for i in range(8)]
    fir = [sum(coef[j] * xs[i + j] for j in range(8)) for i in range(32)]
    e += [fb(fir[0]), fb(fir[7]), fb(fir[31]), fb(sum(fir))]
    poly = [f(v) for v in (0.5, -1.25, 2.0, 0.375, -3.0, 1.0)]

    def horner(x):
        acc = poly[0]
        for c in poly[1:]:
            acc = f(f(acc * x) + c)
        return fb(acc)
    e += [horner(f5), horner(f5), horner(f4), horner(f4)]
    return e


def _run(name, ms):
    from chipem.session import Session
    s = Session("tms320f28335", _img(name), quiet=True)
    s.run_for(ms * 1e6)
    return s


def _word(chip, addr, n=1):
    return int.from_bytes(chip.core.read_mem(addr, n), "little")


@pytest.mark.parametrize("name", ["f28335_fpu.out", "f28335_fpu_coff.out"])
def test_fpu_results(name):
    s = _run(name, 1.0)
    try:
        chip = s.chips["A"]
        syms = s.ctxs["A"].image.symbols
        pre = "_" if name.endswith("_coff.out") else ""
        assert _word(chip, syms[pre + "fpu_done"]) == 0x600D
        n = _word(chip, syms[pre + "nresults"])
        got = [_word(chip, syms[pre + "results"] + 2 * i, 2) for i in range(n)]
        faults = list(s.events(kinds={"fault", "warn"}))
    finally:
        s.close()
    exp = fpu_expected()
    assert n == len(exp) == 44 and not faults
    bad = [(i, hex(g), hex(x)) for i, (g, x) in enumerate(zip(got, exp)) if x is not None and g != x]
    assert not bad, bad
    st0, stf = got[29] & 0xFFFF, got[29] >> 16
    assert st0 & 0x40                             # MOVST0 LVF,LUF: overflow -> ST0.V
    assert stf & 3 == 0 and stf & 0x200           # ... and LVF/LUF cleared in STF, RNDF32 still set


def test_fpu_image_uses_fpu32_instructions():
    """The compiled image really exercises the FPU paths checked above (dis2000 listing next to the image)."""
    dis = _img("f28335_fpu.dis").read_text()
    for mn in ("RPTB", "MACF32", "MAXF32", "MINF32", "MOVST0", "CMPF32", "F32TOUI32", "UI32TOF32", "I16TOF32",
               "F32TOI16", "ABSF32", "SETFLG", "MPYF32"):
        assert mn in dis, mn


# ------------------------------------------------------------------------------------------------ PI loop board
@pytest.fixture(scope="module")
def pi_run():
    from chipem.session import Session
    _img("f28335_pid.out")
    s = Session.from_board(BOARD, quiet=True)
    try:
        s.run_for(14e6)
        chip = s.chips["A"]
        syms = s.ctxs["A"].image.symbols
        w = lambda n: int.from_bytes(chip.core.read_mem(syms[n], 2), "little")
        evs = list(s.events())
        out = {"isr": w("isr_count"), "pass": w("chk_pass"), "bad": w("chk_bad"), "evs": evs,
               "plant": dict(s.board.parts["PLANT"].state())}
    finally:
        s.close()
    return out


def test_pi_loop_settles(pi_run):
    """The float PI controller in the ADC ISR drives the RC plant to 1.2 V (duty 1.2 / 3.3 = 36.4 %)."""
    text = bytes(e.fields["byte"] for e in pi_run["evs"] if e.kind == "uart" and e.fields.get("dir") == "tx")
    lines = text.decode("ascii").split("\r\n")
    assert lines[0] == "F28335 PI"
    stat = [dict(kv.split("=") for kv in ln.split()[2:]) for ln in lines[1:] if ln.startswith("PI ") and "bad=" in ln]
    assert len(stat) >= 3
    last = stat[-1]
    assert 1150 <= int(last["y"]) <= 1250 and 340 <= int(last["u"]) <= 390, last
    assert all(st["bad"] == "0" for st in stat)
    assert 1.1 <= pi_run["plant"]["volts"] <= 1.35


def test_pi_isr_rate_and_fpu_context(pi_run):
    """ePWM1 at 20 kHz, one SEQ1INT ISR per PWM period; main's float check work saw no change in ~thousands of
    passes while the ISR (which saves R0H-R5H, STF and RB, and runs its own RPTB loop) interrupted it."""
    rises = [e.t_ns for e in pi_run["evs"] if e.kind == "pin" and e.fields.get("pin") == "GPIO0"
             and e.fields.get("field") == "level" and e.fields.get("new") == "H"]
    assert len(rises) > 100
    gaps = [b - a for a, b in zip(rises, rises[1:])]
    # 20 kHz: every period starts with a rising edge at CTR = ZERO, except periods with u = 0 (CMPA = 0: CAU wins
    # over ZRO), which the controller outputs while the plant overshoots
    assert sum(abs(g - 50_000) < 1 for g in gaps) > 0.9 * len(gaps)
    assert all(abs(g / 50_000 - round(g / 50_000)) < 1e-4 for g in gaps)
    expect = (14e6 - rises[0]) / 50_000 + 1                       # first ISR one period before the first rise
    assert abs(pi_run["isr"] - expect) <= 2, (pi_run["isr"], expect)
    assert pi_run["bad"] == 0 and pi_run["pass"] > 1000
