"""TI-compiled C on the F28232: firmware/samples_c2000_c/*.c built by TI's cl2000 (firmware/samples_c2000_c/build.sh).

Runs the real compiler output (RTS `_c_int00`, `.cinit` copy tables, compiler idioms) on the interpreter:
  - loader: EABI ELF (`<name>.out`) and COFF (`<name>_coff.out`) images, entry `code_start`, symbols
  - blinky: GPIO34 toggles every 1 ms after the PLL relock
  - uart_chat alone with UART input: HELLO, the once-only HELLO answer, PONG, PINGs every 5 ms
  - boards/c2000_stm32_chat_c.yaml: the chat with the STM32 HAL firmware, same conversation as the asm version
  - selftest: integer/long long arithmetic via the RTS helpers, soft-float, library functions, checked on the host
  - epwm: 20 kHz / 25 % on GPIO0 (EPWM1A), the ePWM event-trigger ISR (ETCLR, PIEACK 3) and the SOCA -> ADC ISR
Skipped when the images are not built (the compiler is not part of the repository).  Timings within 5 %."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "firmware" / "samples_c2000_c" / "build"
BOARD = ROOT / "boards" / "c2000_stm32_chat_c.yaml"
STM_ELF = ROOT / "firmware" / "samples_stm32_hal" / "build" / "uart_chat_hal.elf"
TOL = 0.05

pytestmark = pytest.mark.skipif(not (BUILD / "blinky.out").exists() or not (BUILD / "uart_chat.out").exists(),
                                reason="TI-compiled C samples not built (firmware/samples_c2000_c/build.sh)")


def _img(name):
    p = BUILD / name
    if not p.exists():
        pytest.skip(f"{p.name} not built")
    return p


def _lines(evs, cid=None):
    out, cur, t0 = [], bytearray(), None
    for e in evs:
        f = e.fields
        if e.kind != "uart" or f.get("dir") != "tx" or (cid is not None and f.get("chip") != cid):
            continue
        if t0 is None:
            t0 = e.t_ns / 1e6
        cur.append(f["byte"])
        if f["byte"] == 0x0A:
            out.append((t0, cur.decode("ascii").rstrip("\r\n")))
            cur, t0 = bytearray(), None
    return out


def _edges(evs, pin, cid=None):
    return [(e.t_ns / 1e6, e.fields["new"]) for e in evs if e.kind == "pin" and e.fields.get("pin") == pin
            and e.fields.get("field") == "level" and (cid is None or e.fields.get("chip") == cid)]


def _word(chip, addr, n=1):
    b = chip.core.read_mem(addr, n)
    return int.from_bytes(b, "little")


# ------------------------------------------------------------------------------------------------ loader
@pytest.mark.parametrize("name,fmt,c_int00,main", [
    ("blinky.out", "elf", "_c_int00", "main"), ("uart_chat.out", "elf", "_c_int00", "main"),
    ("blinky_coff.out", "coff", "_c_int00", "_main"), ("uart_chat_coff.out", "coff", "_c_int00", "_main")])
def test_loader(name, fmt, c_int00, main):
    from chipem.firmware import load_firmware
    im = load_firmware(_img(name))
    assert im.fmt == fmt and im.unit_bytes == 2
    assert im.entry == 0x33FFF6 == im.symbols["code_start"]
    assert c_int00 in im.symbols and main in im.symbols and "wd_disable" in im.symbols
    assert im.read(0x33FFF6, 1) == b"\x73\x00"                     # LB wd_disable (codestart section)
    assert all(0x330000 <= a and a + len(d) // 2 <= 0x340000 for a, d in im.segments)    # flash only


# ------------------------------------------------------------------------------------------------ blinky
@pytest.mark.parametrize("name", ["blinky.out", "blinky_coff.out"])
def test_blinky_toggles_every_ms(name):
    from chipem.session import Session
    s = Session("tms320f28232", _img(name), quiet=True)
    try:
        s.run_for(12e6)
        edges = [t for t, _ in _edges(s.events(kinds={"pin"}), "GPIO34")]
        chip = s.chips["A"]
        syms = s.ctxs["A"].image.symbols
        toggles = _word(chip, syms.get("toggles", syms.get("_toggles")), 2)
    finally:
        s.close()
    # first edge: configured as output; then one toggle per ms from ~6.6 ms (PLL relock) + 1 ms
    ticks = edges[1:]
    assert len(ticks) == 5 and abs(ticks[0] - 7.6) < 0.2
    for a, b in zip(ticks, ticks[1:]):
        assert abs((b - a) - 1.0) <= TOL * 1.0
    assert toggles == len(ticks)


# ------------------------------------------------------------------------------------------------ chat alone
@pytest.mark.parametrize("name", ["uart_chat.out", "uart_chat_coff.out"])
def test_uart_chat_alone(name):
    from chipem.session import Session
    s = Session("tms320f28232", _img(name), quiet=True)
    try:
        s.run_for(7.5e6)                                  # SCI-A listens after the PLL relock (~6.6 ms)
        s.uart_input("HELLO STM32\r\nPING 1\r\n", "SCIA")
        s.run_for(22.5e6)
        evs = s.events(kinds={"uart", "pin"})
        syms = s.ctxs["A"].image.symbols
        chip = s.chips["A"]
        pre = "_" if name.endswith("_coff.out") else ""
        vars_ = {v: _word(chip, syms[pre + v]) for v in ("ping_n", "pongs", "peer_seen", "pings_answered",
                                                         "hello_answered")}
    finally:
        s.close()
    lines = _lines(evs)
    texts = [ln for _, ln in lines]
    assert texts[:3] == ["HELLO C2000", "HELLO C2000", "PONG 1"]
    pings = [(t, ln) for t, ln in lines if ln.startswith("PING ")]
    assert [ln for _, ln in pings] == [f"PING {n}" for n in range(1, len(pings) + 1)] and len(pings) == 4
    assert abs(pings[0][0] - lines[0][0] - 5.0) <= TOL * 5.0
    for (a, _), (b, _) in zip(pings, pings[1:]):
        assert abs((b - a) - 5.0) <= TOL * 5.0
    assert vars_ == {"ping_n": 4, "pongs": 0, "peer_seen": 1, "pings_answered": 1, "hello_answered": 1}
    gpio35 = _edges(evs, "GPIO35")
    assert gpio35[-1][1] == "H"


# ------------------------------------------------------------------------------------------------ board
ASM_BOARD = ROOT / "boards" / "c2000_stm32_chat_hal.yaml"
ASM_IMG = ROOT / "firmware" / "samples_c2000" / "build" / "uart_chat.c28img"


def _board(path):
    from chipem.session import Session
    s = Session.from_board(path, quiet=True)
    try:
        s.run_for(40e6)
        evs = s.events(kinds={"uart", "pin"})
        return s, {"A": _lines(evs, "A"), "B": _lines(evs, "B"), "evs": evs,
                   "links": sorted(ln.describe() for ln in s.board.links),
                   "levels": {p: str(s.chips[c].pins.get(p).level()) for c, p in
                              (("A", "GPIO34"), ("A", "GPIO35"), ("B", "PB0"), ("B", "PB1"))}}
    except BaseException:
        s.close()
        raise


@pytest.fixture(scope="module")
def board_run():
    if not STM_ELF.exists():
        pytest.skip("STM32 HAL sample not built")
    s, r = _board(BOARD)
    try:
        a = s.chips["A"]
        syms = s.ctxs["A"].image.symbols
        r["c"] = {v: _word(a, syms[v]) for v in ("ping_n", "pongs", "peer_seen", "pings_answered", "hello_answered")}
    finally:
        s.close()
    asm = None
    if ASM_IMG.exists():
        s2, asm = _board(ASM_BOARD)
        s2.close()
    r["asm"] = asm
    return r


def test_board_conversation(board_run):
    r = board_run
    assert r["links"] == ["A.SCIA -> B.USART1", "B.USART1 -> A.SCIA"]
    a = [ln for _, ln in r["A"]]
    b = [ln for _, ln in r["B"]]
    assert a[:3] == ["HELLO C2000", "HELLO C2000", "PONG 1"] and [x for x in a if x.startswith("HELLO")] == a[:2]
    assert b[:3] == ["HELLO STM32", "HELLO STM32", "PING 1"] and [x for x in b if x.startswith("HELLO")] == b[:2]
    assert 6.4 < r["A"][0][0] < 6.9                       # after the F28232's PLL relock (+ C start-up)
    pings_a = [t for t, ln in r["A"] if ln.startswith("PING ")]
    assert [ln for ln in a if ln.startswith("PING ")] == [f"PING {k}" for k in range(1, 7)]
    assert abs(pings_a[0] - r["A"][0][0] - 5.0) <= TOL * 5.0
    pings_b = [t for t, ln in r["B"] if ln.startswith("PING ")]
    assert [ln for ln in b if ln.startswith("PING ")] == [f"PING {k}" for k in range(1, 8)]
    # B's PING 1 waits behind its HELLO answer (blocking HAL transmit); from PING 2 on both sides tick every 5 ms
    for ts in (pings_a, pings_b[1:]):
        for t0, t1 in zip(ts, ts[1:]):
            assert abs((t1 - t0) - 5.0) <= TOL * 5.0
    for pinger, ponger in (("A", "B"), ("B", "A")):     # every PING answered with the same number, in order
        pings = [ln.split()[1] for _, ln in r[pinger] if ln.startswith("PING ")]
        pongs = [ln.split()[1] for _, ln in r[ponger] if ln.startswith("PONG ")]
        assert pongs == pings[:len(pongs)] and len(pongs) >= len(pings) - 1


def test_board_same_as_asm_version(board_run):
    """The C firmware reproduces the assembler firmware's conversation line for line; the C start-up (RTS
    _c_int00, .cinit) only delays it by ~0.1 ms."""
    asm = board_run["asm"]
    if asm is None:
        pytest.skip("firmware/samples_c2000/build/uart_chat.c28img not built")
    for cid in "AB":
        assert [ln for _, ln in board_run[cid]] == [ln for _, ln in asm[cid]]
        for (tc, _), (ta, _) in zip(board_run[cid], asm[cid]):
            assert -0.01 <= tc - ta <= 0.15


def test_board_counters_and_pins(board_run):
    r = board_run
    pongs_to_a = sum(ln.startswith("PONG ") for _, ln in r["B"])
    pongs_to_b = sum(ln.startswith("PONG ") for _, ln in r["A"])
    assert (pongs_to_a, pongs_to_b) == (6, 7)
    assert r["c"] == {"ping_n": 6, "pongs": pongs_to_a, "peer_seen": 1, "pings_answered": pongs_to_b,
                      "hello_answered": 1}
    assert len(_edges(r["evs"], "GPIO34", "A")) == 1 + pongs_to_a     # first edge: configured as output (L)
    assert len(_edges(r["evs"], "PB0", "B")) == 1 + pongs_to_b
    assert r["levels"]["GPIO35"].endswith("H") and r["levels"]["PB1"].endswith("H")


# ------------------------------------------------------------------------------------------------ selftest
def _cdiv(a, b):
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _cmod(a, b):
    return a - b * _cdiv(a, b)


def _f32(x):
    import struct
    return struct.unpack("<I", struct.pack("<f", x))[0]


def _f(x):
    import struct
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _l_tofs(v):
    """TI RTS L$$TOFS (lib/src/l_tofs28.asm): normalise |v| and drop the bits below the 24-bit mantissa
    (`SFR ACC,7`, no rounding), so it truncates where IEEE rounds to nearest."""
    a = abs(v)
    e = a.bit_length() - 1
    m = a >> (e - 23) if e > 23 else a << (23 - e)
    return ((1 << 31) if v < 0 else 0) | ((e + 127) << 23) | (m & 0x7FFFFF)


def selftest_expected(double_bits):
    """selftest.c's results[] computed on the host with C28x C semantics (int 16, long 32, long long 64 bit;
    float IEEE single; double 64 bit (EABI) or 32 bit (COFF ABI)).  Two results follow TI's soft-float RTS rather
    than IEEE round-to-nearest (checked against the RTS sources in lib/src):
      3.25f / -1.5f = 0xC00AAAAA (IEEE 0xC00AAAAB): FS$$DIV (fs_div28.asm) computes the quotient by SUBCU steps and a
        first-order Taylor correction; the same integer steps modelled on the host give 0xC00AAAAA.
      (float)-1234567890L = 0xCE932C05 (IEEE ...06): L$$TOFS truncates (_l_tofs)."""
    m16, m32, m64 = 0xFFFF, 0xFFFFFFFF, (1 << 64) - 1
    a16, b16, ua16, ub16 = -12345, 77, 50000, 123
    a32, b32, ua32, ub32 = -1234567890, 98765, 4000000000, 65537
    a64, b64, ua64 = -123456789012345, 987654321, 18000000000000000000
    f1, f2, f3 = _f(3.25), _f(-1.5), _f(1.0e-3)
    sh = 13
    if double_bits == 64:
        d1, d2 = 2.5, 0.1
        dd = d1 * d2
        d35 = int(d1 * 1000.0 + d2)
    else:
        d1, d2 = _f(2.5), _f(0.1)
        dd = _f(d1 * d2)
        d35 = int(_f(_f(d1 * 1000.0) + d2))
    e = [_cdiv(a16, b16) & m16, _cmod(a16, b16) & m16, ua16 // ub16, ua16 % ub16, (a16 * b16) & m16,
         (a16 >> 3) & m16,
         _cdiv(a32, b32) & m32, _cmod(a32, b32) & m32, ua32 // ub32, ua32 % ub32, (a32 * b32) & m32, ua32 >> sh,
         (a32 >> sh) & m32, (ua32 << sh) & m32, (a16 * b32) & m32,
         _cdiv(a64, b64) & m64, _cmod(a64, b64) & m64, ua64 // ub32, ua64 % ub32, (a64 * b64) & m64,
         (a64 >> sh) & m64, ua64 >> sh, (ua64 << sh) & m64, 1, 1,
         _f32(f1 * f2), _f32(f1 + f2), 0xC00AAAAA, _f32(f1 - f3), int(_f(f1 * 1000.0)) & m32, _l_tofs(a32),
         _f32(ua32), int(_f(f2 * 100.0)) & m32, 1, _f32(dd), d35 & m32,
         11, 1, 1, 123456 + 7 + ord("d"), 66, 0xFFFF, 610, 42,
         (0xFFF8 << 48) | (2 << 32) | (5 << 16) | 9, -98765 & m32, len("-12345|-1234567890|50000|beef")]
    return e


@pytest.mark.parametrize("name,double_bits", [("selftest.out", 64), ("selftest_coff.out", 32)])
def test_selftest_results(name, double_bits):
    """firmware/samples_c2000_c/selftest.c: integer/long/long long arithmetic through the RTS helpers, soft-float,
    conversions, string/memory functions, switch tables, recursion, function pointers, qsort, snprintf."""
    from chipem.session import Session
    s = Session("tms320f28232", _img(name), quiet=True)
    try:
        s.run_for(20e6)
        chip = s.chips["A"]
        syms = s.ctxs["A"].image.symbols
        pre = "_" if name.endswith("_coff.out") else ""
        assert _word(chip, syms[pre + "selftest_done"]) == 0x600D
        n = _word(chip, syms[pre + "nresults"])
        base = syms[pre + "results"]
        got = [_word(chip, base + 4 * i, 4) for i in range(n)]
        text = bytes(_word(chip, syms[pre + "text"] + i) & 0xFF for i in range(29)).decode()
        faults = [e for e in s.events(kinds={"fault", "warn"})]
    finally:
        s.close()
    exp = selftest_expected(double_bits)
    assert n == len(exp) and not faults
    bad = [(i, hex(g), hex(x)) for i, (g, x) in enumerate(zip(got, exp)) if g != x]
    assert not bad, bad
    assert text == "-12345|-1234567890|50000|beef"


# ------------------------------------------------------------------------------------------------ epwm
@pytest.mark.parametrize("name", ["epwm.out", "epwm_coff.out"])
def test_epwm_pwm_isr_and_adc(name):
    """firmware/samples_c2000_c/epwm.c: up count TBPRD 4999 at 100 MHz (50 us), CMPA 1250 (12.5 us high) on GPIO0;
    the CTR=ZERO interrupt and the CTR=PRD SOCA -> ADCINT interrupt run once per period."""
    from chipem.session import Session
    s = Session("tms320f28232", _img(name), quiet=True)
    try:
        s.chips["A"].peripherals["ADC"].set_channel("ADCINA0", 0x5A5)
        s.run_for(8e6)
        chip = s.chips["A"]
        syms = s.ctxs["A"].image.symbols
        pre = "_" if name.endswith("_coff.out") else ""
        n0 = _word(chip, syms[pre + "epwm_isr_count"], 2)
        a0 = _word(chip, syms[pre + "adc_isr_count"], 2)
        s.run_for(2e6)                                    # 2 ms = 40 periods
        n1 = _word(chip, syms[pre + "epwm_isr_count"], 2)
        a1 = _word(chip, syms[pre + "adc_isr_count"], 2)
        last = _word(chip, syms[pre + "adc_last"])
        evs = s.events(kinds={"pin", "warn", "fault"})
    finally:
        s.close()
    assert n0 > 0 and abs((n1 - n0) - 40) <= 1
    assert a0 > 0 and abs((a1 - a0) - 40) <= 1
    assert last == 0x5A5
    assert not [e for e in evs if e.kind in ("warn", "fault")]
    e = [(t, v) for t, v in _edges(evs, "GPIO0") if t > 7.0]
    rises = [t for t, v in e if v == "H"]
    falls = [t for t, v in e if v == "L" and t > rises[0]]
    assert len(rises) >= 39
    assert all(abs((b - a) - 0.050) < 1e-6 for a, b in zip(rises, rises[1:]))
    assert all(abs((f - r) - 0.0125) < 1e-6 for r, f in zip(rises, falls))
