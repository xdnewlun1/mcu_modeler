"""TI C2000 F28232 low-power modes, watchdog and missing-clock detection (ti_c2000.sysctrl, SPRUI07 1.3.2-1.3.4):
IDLE / STANDBY / HALT through LPMCR0 + IDLE, GPIOLPMSEL wake-up with QUALSTDBY qualification, the watchdog
interrupt as a STANDBY wake-up, peripheral clocks frozen while asleep, WDINT / WDINTS / WDKEY / WDCHK details,
limp mode (OSCOFF, MCLKSTS / MCLKCLR, MCLKOFF) and GPIO qualification of peripheral inputs."""
import importlib.util
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip

from c28x_fake import make_fake_chip

ROOT = Path(__file__).resolve().parent.parent
CHIP = ROOT / "chips" / "tms320f28232.yaml"
HAVE_CORE = importlib.util.find_spec("chipem.core.c28x") is not None
needs_core = pytest.mark.skipif(not HAVE_CORE, reason="chipem.core.c28x not available")

WDCR, WDKEY, SCSR, WDCNTR, LPMCR0, PLLSTS, PLLCR = 0x7029, 0x7025, 0x7022, 0x7023, 0x701E, 0x7011, 0x7021
OSC_NS = 1e9 / 20e6                         # OSCCLK period (20 MHz in the F28232 YAML)


def _asm_session(tmp_path, body: str, **kw):
    from chipem.core.c28x.asm import assemble
    from chipem.session import Session
    src = ('        .sect "codestart", 0x33FFF6\n        LB      start\n        .sect "text", 0x330000\nstart:\n'
           + body)
    out = tmp_path / "t.c28img"
    assemble(src, filename="t.asm").write_c28img(out)
    return Session("tms320f28232", out, quiet=True, **kw)


def _fake():
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    c.core.regs["st1"] |= 1 << 6                       # EALLOW
    return c


def _ea(c):
    c.core.regs["st1"] |= 1 << 6                       # EALLOW again (a reset clears it)


def _resets(c):
    return [e for e in c.events if e.kind == "reset"]


def _infos(s, after=0.0):
    return [e.get("msg") for e in s.events(kinds={"info"}) if e.t_ns >= after]


def _edges(s, pin):
    return [(e.t_ns, e.get("new")) for e in s.events(kinds={"pin"}) if e.get("pin") == pin and e.get("field") == "level"]


# ------------------------------------------------------------------ STANDBY sample
@needs_core
def test_standby_sample_freezes_timer_and_wakes_on_watchdog_and_gpio(tmp_path):
    from chipem.core.c28x.asm import assemble
    from chipem.session import Session
    src = ROOT / "firmware" / "samples_c2000" / "standby.asm"
    out = tmp_path / "standby.c28img"
    assemble(src.read_text(), filename=str(src)).write_c28img(out)
    s = Session("tms320f28232", out, quiet=True)
    s.run_for(20e6)
    sc = s.chip.peripherals["SYSCTRL"]
    msgs = _infos(s)
    i = msgs.index(next(m for m in msgs if m.startswith("STANDBY entered")))
    assert "watchdog wake-up on" in msgs[i]
    assert "STANDBY exit (watchdog interrupt)" in msgs
    led = [t for t, _ in _edges(s, "GPIO34")]
    ent = next(e.t_ns for e in s.events(kinds={"info"}) if e.get("msg", "").startswith("STANDBY entered"))
    ext = next(e.t_ns for e in s.events(kinds={"info"}) if e.get("msg", "") == "STANDBY exit (watchdog interrupt)")
    assert not [t for t in led if ent < t < ext]               # timer 0 frozen: no TINT0 while in STANDBY
    nxt = min(t for t in led if t > ext)
    assert 0.99e6 < nxt - ext < 1.01e6                          # the frozen timer resumes with its remaining count
    assert abs((ext - 6.554e6) - 6.5536e6) < 2e3                # WDCLK = OSCCLK/512: 256 counts = 6.55 ms
    assert _edges(s, "GPIO35")                                   # WAKEINT ISR ran
    # GPIO12 wake-up: a 0.3 us glitch (< 12 OSCCLK = 0.6 us) does not wake, a 2 us low does
    s.run_for(3e6)                                              # 23 ms: in the STANDBY from 22.66 ms
    assert sc.lpm == "STANDBY"
    t0 = s.chip.now_ns()
    s.chip.pins.set_external("GPIO12", "L")
    s.run_for(300)
    s.chip.pins.set_external("GPIO12", "H")
    s.run_for(5_000)
    assert sc.lpm == "STANDBY"
    s.chip.pins.set_external("GPIO12", "L")
    t1 = s.chip.now_ns()
    s.run_for(2_000)
    s.chip.pins.set_external("GPIO12", "H")
    ex = [e for e in s.events(kinds={"info"}) if e.get("msg") == "STANDBY exit (GPIO12)"]
    assert ex and 12 * OSC_NS - 1 <= ex[0].t_ns - t1 <= 12 * OSC_NS + 20
    assert ex[0].t_ns > t0


@needs_core
def test_sci_drops_bytes_in_standby(tmp_path):
    s = _asm_session(tmp_path, """
        EALLOW
        MOVW    DP, #DP(0x7029)
        MOV     @0x29, #0x0068
        MOV     @0x1E, #0x0001          ; LPMCR0: STANDBY
        MOVW    DP, #DP(0x701C)
        OR      @0x1C, #0x0400          ; PCLKCR0.SCIAENCLK
        EDIS
        MOVW    DP, #DP(0x7051)
        MOV     @0x51, #0x0003          ; SCICTL1: RXENA | TXENA (in reset)
        MOV     @0x53, #0x0020          ; SCILBAUD
        MOV     @0x51, #0x0023          ; leave SW reset
        IDLE
stop:   SB      stop, UNC
""")
    s.run_for(50_000)
    assert s.chip.peripherals["SYSCTRL"].lpm == "STANDBY"
    scia = s.chip.peripherals["SCIA"]
    scia.receive(b"x")
    s.run_for(5e6)
    drops = [e for e in s.events(kinds={"uart"}) if e.get("dir") == "drop"]
    assert drops and drops[0].get("reason") == "clock_stopped"


# ------------------------------------------------------------------ HALT
HALT_PROG = """
        SETC    INTM
        EALLOW
        MOVW    DP, #DP(0x7029)
        MOV     @0x29, #0x0068          ; watchdog off
        MOV     @0x1E, #0x0002          ; LPMCR0: HALT
        MOVW    DP, #DP(0x6FE8)
        MOV     @0x28, #0x1000          ; GPIOLPMSEL: GPIO12
        MOVW    DP, #DP(0x6F9A)
        OR      @0x1A, #0x0004          ; GPIO34 output
        MOVW    DP, #DP(0x0D4E)
        MOVL    XAR4, #wake
        MOVL    @0x0E, XAR4
        EDIS
        MOVW    DP, #DP(0x0CE0)
        MOV     @0x20, #1               ; ENPIE
        OR      @0x22, #0x0080          ; PIEIER1.8 WAKEINT
        OR      IER, #0x0001
        EINT
        IDLE
        MOVW    DP, #DP(0x6FCA)
        MOV     @0x0A, #0x0004          ; GPBSET: GPIO34 high after HALT
done:   SB      done, UNC
wake:   MOVW    DP, #DP(0x0CE1)
        MOV     @0x21, #0x0001
        IRET
"""


@needs_core
def test_halt_wakes_on_gpio_after_oscillator_and_pll_lock(tmp_path):
    s = _asm_session(tmp_path, HALT_PROG)
    s.run_for(100_000)
    sc = s.chip.peripherals["SYSCTRL"]
    assert sc.lpm == "HALT" and sc._wd_held
    s.chip.pins.set_external("GPIO12", "L")
    t0 = s.chip.now_ns()
    s.run_for(1e6)
    assert sc.lpm == "HALT"                                     # PLL relocking (131072 OSCCLK = 6.55 ms)
    s.run_for(7e6)
    assert sc.lpm is None
    ex = next(e.t_ns for e in s.events(kinds={"info"}) if e.get("msg") == "HALT exit (GPIO12)")
    assert abs(ex - t0 - 131072 * OSC_NS) < 1e3
    assert s.chip.pins.get("GPIO34").level() == "H"


@needs_core
def test_halt_without_wakeint_stays_idle(tmp_path):
    s = _asm_session(tmp_path, HALT_PROG.replace("OR      @0x22, #0x0080", "OR      @0x22, #0x0000"))
    s.run_for(100_000)
    s.chip.pins.set_external("GPIO12", "L")
    s.run_for(8e6)
    assert s.chip.peripherals["SYSCTRL"].lpm is None
    assert s.chip.sleeping                                      # clocks back, CPU still in IDLE
    assert any("WAKEINT is not enabled" in e.get("msg", "") for e in s.events(kinds={"warn"}))
    assert s.chip.pins.get("GPIO34").level() != "H"


@needs_core
def test_idle_mode_wakes_on_any_interrupt(tmp_path):
    from chipem.core.c28x.asm import assemble
    from chipem.session import Session
    src = ROOT / "firmware" / "samples_c2000" / "timer_irq.asm"
    out = tmp_path / "timer_irq.c28img"
    assemble(src.read_text(), filename=str(src)).write_c28img(out)
    s = Session("tms320f28232", out, quiet=True)
    s.run_for(12e6)
    sc = s.chip.peripherals["SYSCTRL"]
    assert sc.lpm_entries["IDLE"] >= 4 and sc.lpm_entries["STANDBY"] == 0
    assert len(_edges(s, "GPIO34")) >= 5


# ------------------------------------------------------------------ watchdog details (fake core)
def test_wdkey_reads_wdcr_and_key_sequence():
    c = _fake()
    c.bus.write(WDCR, 1, 0x002B)                                # WDCHK=101, WDPS=3
    assert c.bus.read(WDKEY, 1) == 0x0003                      # WDKEY reads WDCR (WDCHK reads 0)
    c.run_for(3e6)
    sc = c.peripherals["SYSCTRL"]
    assert sc.wd_count() > 0
    c.bus.write(WDKEY, 1, 0x55)
    c.bus.write(WDKEY, 1, 0x32)                                 # wrong value disarms
    c.bus.write(WDKEY, 1, 0xAA)
    assert sc.wd_count() > 0
    c.bus.write(WDKEY, 1, 0x55)
    c.bus.write(WDKEY, 1, 0x55)
    c.bus.write(WDKEY, 1, 0xAA)
    assert sc.wd_count() == 0


def test_watchdog_interrupt_mode_wdints_and_wdenint_clear_resets():
    c = _fake()
    hits = []
    orig = c.arch.pulse_irq
    c.arch.pulse_irq = lambda n: (hits.append(n), orig(n))
    c.bus.write(SCSR, 1, 0x0002)                                # WDENINT
    assert c.bus.read(SCSR, 1) & 0x7 == 0x7                    # WDOVERRIDE, WDENINT, WDINTS = 1 (inactive)
    c.run_for(256 * 512 * OSC_NS + 1_000)
    assert hits                                                  # WAKEINT
    assert c.bus.read(SCSR, 1) & 4 == 0                         # WDINT low for 512 OSCCLK
    c.run_for(512 * OSC_NS)
    assert c.bus.read(SCSR, 1) & 4 == 4
    assert not [e for e in _resets(c) if e.t_ns > 0]
    # bad WDCHK in interrupt mode: interrupt, no reset
    n = len(hits)
    c.bus.write(WDCR, 1, 0x0000)
    assert len(hits) == n + 1
    assert not c.arch.reset_requested
    # clearing WDENINT while WDINT is low resets the chip at once
    c.bus.write(SCSR, 1, 0x0000)
    assert c.arch.reset_requested == "watchdog"


def test_watchdog_reset_sets_wdflag_and_xrs_clears_it():
    c = _fake()
    c.bus.write(WDCR, 1, 0x0050)                                # bad WDCHK, reset mode: immediate reset
    assert c.arch.reset_requested == "watchdog"
    c.run_for(1_000)
    _ea(c)
    assert c.bus.read(WDCR, 1) & 0x80
    c.bus.write(WDCR, 1, 0x00A8)                                # write 1 clears WDFLAG (WDCHK = 101)
    assert not c.bus.read(WDCR, 1) & 0x80
    c.bus.write(WDCR, 1, 0x0050)
    c.run_for(1_000)
    _ea(c)
    assert c.bus.read(WDCR, 1) & 0x80
    c.reset()                                                    # XRS
    assert not c.bus.read(WDCR, 1) & 0x80


# ------------------------------------------------------------------ missing clock / limp mode
def test_oscoff_triggers_missing_clock_reset_and_limp_mode():
    c = _fake()
    sc = c.peripherals["SYSCTRL"]
    c.bus.write(WDCR, 1, 0x0068)
    c.bus.write(PLLSTS, 1, c.bus.read(PLLSTS, 1) | 0x20)       # OSCOFF: test the detector
    c.run_for(100_000)
    assert sc.limp
    assert c.bus.read(PLLSTS, 1) & 0x08                         # MCLKSTS
    assert c.bus.read(PLLSTS, 1) & 0x20                         # PLLSTS kept across MCLKRES
    assert any(e.get("reason") == "missing_clock" for e in _resets(c))
    _ea(c)
    assert c.clock.hz == sc.limp_hz / 2                          # bypass, DIVSEL = /2 after the boot ROM
    assert sc._wd_held                                           # no watchdog without OSCCLK
    # MCLKCLR with OSCCLK still missing: detected again
    n = len(_resets(c))
    c.bus.write(PLLSTS, 1, c.bus.read(PLLSTS, 1) | 0x10)
    assert not c.bus.read(PLLSTS, 1) & 0x08
    c.run_for(100_000)
    _ea(c)
    assert len(_resets(c)) > n and sc.limp
    # oscillator back + MCLKCLR: normal clock
    c.bus.write(PLLSTS, 1, (c.bus.read(PLLSTS, 1) & ~0x20) | 0x10)
    c.run_for(100_000)
    assert not sc.limp and c.clock.hz == 10e6
    c.reset()
    assert not c.bus.read(PLLSTS, 1) & 0x08


def test_stop_oscillator_with_mclkoff_stops_the_cpu():
    c = _fake()
    sc = c.peripherals["SYSCTRL"]
    c.bus.write(WDCR, 1, 0x0068)
    c.bus.write(PLLSTS, 1, c.bus.read(PLLSTS, 1) | 0x40)       # MCLKOFF
    c.run_for(10_000)
    sc.stop_oscillator()
    c.run_for(10_000)
    assert c.sleeping and sc._dead
    n = c.insns
    c.run_for(100_000)
    assert c.insns == n                                          # no clock: nothing executes
    sc.start_oscillator()
    c.run_for(10_000)
    assert not c.sleeping and c.insns > n
    assert not sc.limp


# ------------------------------------------------------------------ GPIO qualification of peripheral inputs
def test_ecap_input_is_qualified():
    from chipem.board import Board
    c = _fake()
    bd = Board({"A": c}, quantum_ns=100)
    c.bus.write(WDCR, 1, 0x0068)
    c.bus.write(0x701D, 1, 0x0100)                               # PCLKCR1.ECAP1ENCLK
    gpio = c.peripherals["GPIO"]
    # GPIO24 is ECAP1 (MUX1) on the F28232: GPAMUX2 bits 17:16 = 1
    c.bus.write(0x6F88, 2, 1 << 16)
    c.bus.write(0x6F80 + 0x04, 2, 2 << 16)                       # GPAQSEL2: GPIO24 6 samples
    c.bus.write(0x6F80 + 0x00, 2, 5 << 24)                       # GPACTRL QUALPRD3 = 5 -> 1 us periods
    ecap = c.peripherals["ECAP1"]
    c.bus.write(0x6A00 + 0x15, 1, 0x0010)                         # ECCTL2: TSCTRSTOP run, continuous
    c.bus.write(0x6A00 + 0x14, 1, 0x0100)                         # ECCTL1: CAPLDEN
    bd.run_for(5_000)
    assert gpio.owns_pin(c.pins.get("GPIO24"))
    seen = []
    orig = ecap._pin_changed
    ecap._pin_changed = lambda pin: (seen.append(c.now_ns()), orig(pin))
    gpio.periph_listeners["GPIO24"] = [ecap._pin_changed]
    c.pins.set_external("GPIO24", "L")
    t0 = c.now_ns()
    bd.run_for(2_000)
    c.pins.set_external("GPIO24", "H")                           # 2 us glitch: filtered (6 x 1 us)
    bd.run_for(10_000)
    assert not seen
    c.pins.set_external("GPIO24", "L")
    t1 = c.now_ns()
    bd.run_for(10_000)
    assert seen and 5_000 <= seen[0] - t1 <= 6_100
    assert t1 > t0


# ------------------------------------------------------------------ code security module / flash registers
CSM_PROG = """
        .sect "codestart", 0x33FFF6
        LB      main
        .sect "pwl", 0x33FFF8
        .word   0x1111, 0x2222, 0x3333, 0x4444, 0x5555, 0x6666, 0x7777, 0x8888
        .sect "table", 0x338000
        .word   0x1234
        .sect "text", 0x330000
secure_read:                            ; runs from flash (secure memory)
        MOVL    XAR4, #0x338000
        MOV     AL, *XAR4
        MOV     *XAR5++, AL
        LRETR
        .sect "ram", 0xC000             ; L4 SARAM: not secure
main:   MOVL    XAR5, #0x0400
        MOVL    XAR4, #0x338000
        MOV     AL, *XAR4               ; [0x400] secure read from unsecure code
        MOV     *XAR5++, AL
        LCR     secure_read             ; [0x401] from secure code
        MOVL    XAR4, #0x0A86
        MOV     AL, *XAR4               ; [0x402] FBANKWAIT (CSM protected)
        MOV     *XAR5++, AL
        MOVL    XAR4, #0x33FFF8         ; password match flow: dummy reads ...
        MOV     AL, *XAR4++
        MOV     AL, *XAR4++
        MOV     AL, *XAR4++
        MOV     AL, *XAR4++
        MOV     AL, *XAR4++
        MOV     AL, *XAR4++
        MOV     AL, *XAR4++
        MOV     AL, *XAR4++
        EALLOW                          ; ... and the KEY writes
        MOVL    XAR4, #0x0AE0
        MOV     *XAR4++, #0x1111
        MOV     *XAR4++, #0x2222
        MOV     *XAR4++, #0x3333
        MOV     *XAR4++, #0x4444
        MOV     *XAR4++, #0x5555
        MOV     *XAR4++, #0x6666
        MOV     *XAR4++, #0x7777
        MOV     *XAR4++, #0x8888
        MOVL    XAR4, #0x0AEF
        MOV     AL, *XAR4               ; [0x403] CSMSCR: SECURE = 0 now
        MOV     *XAR5++, AL
        MOVL    XAR4, #0x338000
        MOV     AL, *XAR4               ; [0x404] readable now
        MOV     *XAR5++, AL
        MOVL    XAR4, #0x0AEF
        MOV     *XAR4, #0x8000          ; FORCESEC
        MOV     AL, *XAR4               ; [0x405] CSMSCR: SECURE again
        MOV     *XAR5++, AL
        MOVL    XAR4, #0x338000
        MOV     AL, *XAR4               ; [0x406] blocked again
        MOV     *XAR5++, AL
        EDIS
done:   SB      done, UNC
"""


@needs_core
def test_csm_password_match_flow_and_secure_reads(tmp_path):
    from chipem.core.c28x.asm import assemble
    from chipem.session import Session
    out = tmp_path / "csm.c28img"
    assemble(CSM_PROG, filename="csm.asm").write_c28img(out)
    s = Session("tms320f28232", out, quiet=True)
    s.run_for(200_000)
    got = [int.from_bytes(s.chip.core.read_mem(0x400 + i, 1), "little") for i in range(7)]
    assert got == [0, 0x1234, 0, 0x005E, 0x1234, 0x005F, 0], [hex(v) for v in got]
    csm = s.chip.peripherals["CSM"]
    assert csm.secure and csm.blocked_reads >= 2


@needs_core
def test_csm_erased_password_is_unsecure_after_boot(tmp_path):
    s = _asm_session(tmp_path, """
        MOVL    XAR4, #0x0AEF
        MOV     AL, *XAR4
        MOVL    XAR5, #0x0400
        MOV     *XAR5, AL
done:   SB      done, UNC
""")
    s.run_for(10_000)
    assert int.from_bytes(s.chip.core.read_mem(0x400, 1), "little") == 0x005E
    assert not s.chip.peripherals["CSM"].secure


def test_flash_power_state_machine():
    c = _fake()
    FPWR, FSTATUS = 0x0A82, 0x0A83
    assert c.bus.read(FSTATUS, 1) & 3 == 3                       # active after the boot ROM's dummy read
    c.bus.write(FPWR, 1, 0)                                       # lower: at once
    assert c.bus.read(FSTATUS, 1) & 0xF == 0
    c.bus.write(FPWR, 1, 3)                                       # sleep -> standby (511) -> active (511)
    assert c.bus.read(FSTATUS, 1) & 0xF == 0x4                   # STDBYWAITS counting
    c.run_cycles(520)
    assert c.bus.read(FSTATUS, 1) & 0xF == 0x9                   # standby, ACTIVEWAITS counting
    c.run_cycles(520)
    assert c.bus.read(FSTATUS, 1) & 0xF == 0x3


# ------------------------------------------------------------------ STANDBY / HALT freeze of the remaining blocks
def _standby(c, blk, ns):
    """SYSCLKOUT to `blk` off for `ns` (what ti_c2000.sysctrl does to every clocked block in STANDBY/HALT)."""
    blk.clock_stop(True, "STANDBY")
    c.clock.advance(c.clock.ns_to_cycles(ns))
    c.sched.run_due()
    blk.clock_stop(False, "STANDBY")


def test_mcbsp_sample_rate_generator_holds_in_standby():
    c = _fake()
    m = c.peripherals["MCBSPA"]
    m._t_srg, m._Lc = c.clock.cycles, 100
    n0 = m._cycle_now()
    _standby(c, m, 1e6)
    assert m._cycle_now() == n0                           # CLKG cycle count continues from where it stopped


def test_ecan_time_stamp_counter_holds_and_resyncs_in_standby():
    c = _fake()
    e = c.peripherals["ECANA"]
    e._cce = False
    e.can_bit_ns = lambda: 1000.0                        # 1 Mbit/s bit time (CANBTC programming not needed here)
    e._tsc = (0, c.now_ns(), True, 1000.0)               # TSC running (normal mode)
    c.clock.advance(c.clock.ns_to_cycles(50e3))
    v0 = e._tsc_value(c.now_ns())
    assert v0 > 0
    _standby(c, e, 10e6)
    assert e._tsc_value(c.now_ns()) == v0                 # no time stamps counted while SYSCLKOUT was off
    assert e._sync_t > c.now_ns()                         # resynchronising to the bus (11 recessive bits)
    e.clk_stopped = True
    assert e.can_rx_state(e._bus()) is None               # off the bus while stopped
    e.clk_stopped = False


def test_i2c_master_bit_events_park_with_the_block():
    c = _fake()
    i2c = c.peripherals["I2CA"]
    seen = []
    ev = c.sched.in_cycles(1000, i2c.port.master._fire, 0.0)   # a master bit event (bound to the I2CMaster helper)
    i2c.clock_stop(True, "STANDBY")
    assert ev.cycles > 10**15                             # parked (helper events belong to the block: lp_owner)
    i2c.clock_stop(False, "STANDBY")
    assert ev.cycles == c.clock.cycles + 1000
    c.sched.cancel(ev)
    assert i2c.i2c_address(0x10, False) is False or seen == []


def test_adc_session_and_dma_burst_resume_after_standby():
    from chipem.periph.ti_c2000.adc import _Session
    from chipem.periph.ti_c2000.dma import _Burst
    c = _fake()
    adc, dma = c.peripherals["ADC"], c.peripherals["DMA"]
    now = c.clock.cycles
    adc._sess = _Session(1, 0, 4, now + 100, 50, False, 0)
    _standby(c, adc, 1e6)
    dt = c.clock.ns_to_cycles(1e6)
    assert adc._sess.t_first == now + 100 + dt
    adc._sess = None
    b = _Burst(dma._ch[0] if hasattr(dma, "_ch") else None, 8, c.clock.cycles, 4)
    dma._busy = b
    e0 = b.end()
    _standby(c, dma, 1e6)
    assert b.end() == e0 + dt
    dma._busy = None
