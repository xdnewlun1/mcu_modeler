"""MCS-51 / P89CV51 behaviour added after the first round (docs/MCS51_CORE.md, docs/LIMITATIONS.md): Timer 2 vector
and polling order, the interrupt polling cycle (response >= 3 machine cycles), the power-down oscillator restart
(1024 clocks), IDATA read watchpoints, UART mode 0 receive, the lazily computed external bus waveform, the generic
80C52 chip."""
from __future__ import annotations

from pathlib import Path

from chipem.session import Session

from m51util import MC_NS, chip_for, iram, program

ROOT = Path(__file__).resolve().parents[1]


def test_timer2_vector_002b_and_polling_order():
    c = chip_for(program("""
        MOV RCAP2H, #0FFh
        MOV RCAP2L, #0F0h
        MOV TH2, #0FFh
        MOV TL2, #0F0h
        SETB ET2
        SETB EA
        SETB TR2
        SJMP $
    """, isrs={0x2B: " CLR TF2\n INC 30h\n RETI"}))
    c.run_for(200 * MC_NS)
    assert iram(c, 0x30) >= 5                                     # Timer 2 overflows vector to 002BH
    names = [s.name for s in c.arch._poll]
    assert names == ["INT0", "TIMER0", "INT1", "TIMER1", "PCA", "SERIAL", "TIMER2"]   # Fig. 25 / P89V51 Table 43


def test_interrupt_response_includes_polling_cycle():
    """Timer 0 mode 2 (100 counts): TR0 is set at the end of machine cycle 14, the 100th count ends cycle 114
    (TF0 set), cycle 115 polls (the NOP in it completes), the LCALL takes 116-117: the handler starts at 118."""
    src = """
        ORG 0
        LJMP main
        ORG 0Bh
        INC 30h
        RETI
        ORG 40h
main:   MOV SP, #60h
        MOV 30h, #0
        MOV TMOD, #02h
        MOV TH0, #9Ch
        MOV TL0, #9Ch
        SETB ET0
        SETB EA
        SETB TR0
loop:   NOP
        NOP
        MOV A, 30h
        SJMP loop
"""
    c = chip_for(src)
    c.core.set_breakpoints([0x0B])
    entries = []
    for _ in range(4):
        c.run_for(1e6)
        entries.append(c.clock.cycles // 12)
        c.core.bp_skip = 0x0B
    assert entries[0] == 118
    assert [b - a for a, b in zip(entries, entries[1:])] == [100, 100, 100]


def _pd_chip():
    return chip_for(program("CLR IT0\n SETB EX0\n SETB EA\n ORL PCON, #02h\n MOV 30h, #1\n SJMP $",
                            isrs={0x03: " CLR EX0\n INC 31h\n RETI"}))


def test_powerdown_needs_1024_clocks_of_int0_low():
    c = _pd_chip()
    c.run_for(20e3)
    assert c.sleeping
    c.pins.set_external("P3.2", "L")
    c.run_for(500 * 1e9 / c.clock.hz)                              # 500 clocks: oscillator still starting
    c.pins.set_external("P3.2", "H")
    c.run_for(5e4)
    assert c.sleeping and iram(c, 0x31) == 0
    assert any(e.kind == "warn" and "Power-down not left" in e.fields.get("msg", "") for e in c.log)
    c.pins.set_external("P3.2", "L")
    t0 = c.clock.cycles
    c.run_for(2000 * 1e9 / c.clock.hz)
    assert not c.sleeping and iram(c, 0x31) == 1 and iram(c, 0x30) == 1
    wake = [e for e in c.log if e.kind == "info" and e.fields.get("msg") == "Power-down mode left"][-1]
    assert c.clock.ns_to_cycles(wake.t_ns - c.clock.cycles_to_ns(t0)) == 1024


def test_idata_read_watchpoint():
    s = Session(ROOT / "chips" / "p89cv51rd2.yaml", ROOT / "firmware" / "samples_mcs51" / "build" / "timer_irq.hex",
                quiet=True)
    d = s.debug.dbg()
    bp = d.add_watch("0x20031", "r")                               # TICKS_L, read by the main loop's CJNE
    s.run_for(5e6)
    info = s.chip.core.stop_info
    assert info["type"] == "watchpoint" and info["access"] == "r" and info["addr"] == 0x20031
    assert bp.hits == 1


def test_uart_mode0_receive_from_shift_register():
    c = chip_for(program("""
        MOV SCON, #10h          ; mode 0, REN: reception starts
w1:     JNB RI, w1
        MOV 30h, SBUF
        CLR RI                  ; next reception
w2:     JNB RI, w2
        MOV 31h, SBUF
        SJMP $
    """))
    data = [0xA5, 0x3C]
    state = {"byte": 0, "bit": 0}

    def on_pin(ev):                                                # a 74HC165-like partner: next bit on TXD low
        if ev.pin == "P3.1" and ev.field == "level" and ev.new == "L":
            b = data[state["byte"]] >> state["bit"] & 1
            c.pins.set_external("P3.0", "H" if b else "L")
            state["bit"] += 1
            if state["bit"] == 8:
                state["bit"], state["byte"] = 0, min(state["byte"] + 1, 1)
    c.pins.subscribe(on_pin)
    c.run_for(100 * MC_NS)
    assert iram(c, 0x30) == 0xA5 and iram(c, 0x31) == 0x3C
    rx = [e for e in c.log if e.kind == "uart" and e.fields.get("dir") == "rx"]
    assert [e.fields["byte"] for e in rx] == [0xA5, 0x3C]


def test_external_bus_waveform():
    c = chip_for(program("ORL AUXR, #02h\n MOV DPTR, #1234h\n MOV A, #5Ah\n MOVX @DPTR, A\n MOVX A, @DPTR\n"))
    c.run_for(40 * MC_NS)
    xb = c.peripherals["XBUS"]
    (tw, _, kw, aw, dw), (tr, _, kr, ar, dr) = list(xb.cycles)
    assert (kw, aw, dw, kr, ar, dr) == ("w", 0x1234, 0x5A, "r", 0x1234, 0xFF) and tr - tw == 24
    w = xb.waveform(0)
    osc = 1e9 / c.clock.hz
    wr = [(round(t / osc), v) for t, v in w["WR"]]
    rd = [(round(t / osc), v) for t, v in w["RD"]]
    assert wr == [(tw + 16, "L"), (tw + 28, "H")] and rd == [(tr + 16, "L"), (tr + 28, "H")]    # 6 Tcy x 2
    p0 = [(round(t / osc), v) for t, v in w["P0"]]
    assert (tw + 6, 0x34) in p0 and (tw + 16, 0x5A) in p0
    assert (tw + 6, 0x12) in [(round(t / osc), v) for t, v in w["P2"]]
    ale = [round(t / osc) for t, v in w["ALE"] if v == "H"]
    assert tw + 6 in ale and not any(tw + 8 <= t < tw + 30 for t in ale if t != tr + 6)


def test_generic_80c52_runs_8052_firmware():
    s = Session("i80c52", ROOT / "firmware" / "samples_mcs51" / "build" / "uart_hello.hex", quiet=True)
    s.run_for(60e6)
    assert s.uart_output("UART") == b"Hello from P89CV51RD2 (UART 9600 from Timer 1)\r\n"
    assert "PCA" not in s.chip.peripherals and "SPI" not in s.chip.peripherals
    assert s.chip.arch.addrs["iph"] is None and s.chip.desc.package == "DIP40"
    assert not [e for e in s.events() if e.kind in ("warn", "fault")]
