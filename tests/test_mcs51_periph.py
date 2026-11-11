"""P89CV51 peripheral models (chipem/periph/mcs51) against the data sheet: ports, Timer 0/1 modes, Timer 2 modes,
UART modes / baud / address recognition, SPI, PCA, watchdog, IAP, 6-clock mode, external bus."""
from __future__ import annotations

import pytest

from chipem.devices.sram import ParallelSram

from m51util import MC_NS, XTAL, chip_for, iram, program


# --------------------------------------------------------------------------------------------- ports
def test_ports_quasi_bidirectional_and_p0_open_drain():
    c = chip_for(program("MOV P1, #0F0h\n MOV P0, #0FEh\n MOV A, P1\n MOV 30h, A"))
    c.run_for(20 * MC_NS)
    p10, p14, p00, p01 = (c.pins.get(n) for n in ("P1.0", "P1.4", "P0.0", "P0.1"))
    assert (p10.direction, p10.drive) == ("out", "L") and p14.direction == "in" and p14.pull == "up"
    assert p14.level() == "H" and p00.level() == "L" and p01.pull is None and p01.level() == "X"
    assert iram(c, 0x30) == 0xF0


def test_port_read_pins_rmw_latch():
    c = chip_for(program("MOV 30h, P1\n ORL P1, #01h\n MOV 31h, P1\n SJMP $"))
    c.pins.set_external("P1.7", "L")                 # pulled low outside: pin reads 0, latch stays 1
    c.run_for(30 * MC_NS)
    assert iram(c, 0x30) == 0x7F and iram(c, 0x31) == 0x7F
    assert c.peripherals["PORTS"].latch[1] == 0xFF   # ORL read the latch (FFh), not the pins (7Fh)


# --------------------------------------------------------------------------------------------- Timer 0/1
@pytest.mark.parametrize("mode,th,tl,counts", [(1, 0xFF, 0x00, 256), (0, 0xFF, 0x00, 32), (2, 0x9C, 0x9C, 100)])
def test_timer0_modes_overflow_period(mode, th, tl, counts):
    c = chip_for(program(f"""
        MOV TMOD, #{mode:02X}h
        MOV TH0, #{th}
        MOV TL0, #{tl}
        SETB TR0
l:      JNB TF0, l
        CLR TF0
        MOV 30h, TL0
        SETB P1.0
        CPL P1.0
        SJMP l
    """))
    c.run_for(5000 * MC_NS)
    t01 = c.peripherals["TIMER01"]
    n = t01.overflows[0]
    if mode == 2:
        assert n == pytest.approx(5000 / counts, abs=2)
    else:
        assert n == 1                                # modes 0/1 are not reloaded: the next overflow is 2^13/2^16 later
    falls = [e.t_ns for e in c.log if e.kind == "pin" and e.fields.get("pin") == "P1.0"
             and e.fields.get("field") == "level" and e.fields.get("new") == "L"]
    if mode == 2:
        # the polling loop sees the flag on its 2-machine-cycle JNB lattice: single periods jitter by one machine
        # cycle as on silicon, the average is the overflow period
        assert len(falls) > 20
        assert (falls[-1] - falls[1]) / (len(falls) - 2) / MC_NS == pytest.approx(counts, abs=0.1)


def test_timer_counter_mode_gate_and_mode3():
    c = chip_for(program("MOV TMOD, #0Dh\n MOV TH0, #0\n MOV TL0, #0\n SETB TR0\n SJMP $"))   # T0: counter, GATE
    c.run_for(50e3)
    for _ in range(5):                                # INT0 high: counting enabled
        c.pins.set_external("P3.4", "L")
        c.run_for(2e3)
        c.pins.set_external("P3.4", "H")
        c.run_for(2e3)
    c.pins.set_external("P3.2", "L")                  # GATE: INT0 low stops it
    for _ in range(3):
        c.pins.set_external("P3.4", "L")
        c.run_for(2e3)
        c.pins.set_external("P3.4", "H")
        c.run_for(2e3)
    assert c.bus.read(0x3008A, 1) == 5
    # mode 3: TL0 under TR0 -> TF0, TH0 under TR1 -> TF1
    c = chip_for(program("MOV TMOD, #03h\n MOV TL0, #0F0h\n MOV TH0, #0E0h\n SETB TR1\n SJMP $"))
    c.run_for(60 * MC_NS)                             # TR1 set at machine cycle 11, TH0 overflows 32 counts later
    tcon = c.bus.read(0x30088, 1)
    assert tcon & 0x80 and not tcon & 0x20            # TH0 overflowed (TF1), TL0 not running
    assert c.bus.read(0x3008A, 1) == 0xF0


def test_timer_reads_are_cycle_exact():
    c = chip_for(program("MOV TMOD, #01h\n SETB TR0\n MOV A, TL0\n MOV B, TL0\n SJMP $"))
    c.run_for(50 * MC_NS)
    a, b = c.core.read_reg("a"), c.core.read_reg("b")
    assert b - a == 1                                 # one MOV A,dir = 1 machine cycle between the reads


# --------------------------------------------------------------------------------------------- Timer 2
def test_timer2_autoreload_capture_down_and_clockout():
    c = chip_for(program("MOV RCAP2H, #0FFh\n MOV RCAP2L, #0F6h\n MOV TH2, #0FFh\n MOV TL2, #0F6h\n"
                         " SETB TR2\nl: JNB TF2, l\n CLR TF2\n INC 30h\n SJMP l"))
    c.run_for(1000 * MC_NS)
    assert iram(c, 0x30) == pytest.approx(100, abs=2)  # reload every 10 counts
    # capture on T2EX falling edge
    c = chip_for(program("MOV T2CON, #0Dh\n SJMP $"))  # EXEN2, TR2, CP/RL2
    c.run_for(300 * MC_NS)
    c.pins.set_external("P1.1", "L")
    c.run_for(10 * MC_NS)
    t2 = c.peripherals["TIMER2"]
    assert t2.get("T2CON") & 0x40 and 290 <= t2.rcap() <= 305
    # down counting with DCEN and T2EX = 0: underflow at RCAP2 reloads FFFFh
    c = chip_for(program("MOV T2MOD, #01h\n MOV RCAP2H, #0FFh\n MOV RCAP2L, #0F0h\n MOV TH2, #0FFh\n"
                         " MOV TL2, #0FFh\n SETB TR2\n SJMP $"))
    c.pins.set_external("P1.1", "L")
    c.run_for(40 * MC_NS)
    assert c.peripherals["TIMER2"].get("T2CON") & 0x80
    # clock-out: T2 toggles every (65536 - RCAP2) counts at fosc/2
    c = chip_for(program("MOV T2MOD, #02h\n MOV RCAP2H, #0FEh\n MOV RCAP2L, #00h\n SETB TR2\n SJMP $"))
    c.run_for(5e6)
    t2 = c.peripherals["TIMER2"]
    assert t2.clock_out_hz() == pytest.approx(XTAL / (4 * 512))
    edges = [e.t_ns for e in c.log if e.kind == "pin" and e.fields.get("pin") == "P1.0" and e.fields.get("field") == "level"]
    assert len(edges) > 10 and (edges[-1] - edges[-2]) == pytest.approx(512 * 2 * 1e9 / XTAL, rel=1e-6)


# --------------------------------------------------------------------------------------------- UART
def _uart_prog(setup: str, tx: str = "") -> str:
    return program(setup + "\n SETB TR1\n" + tx + """
rx:     JNB RI, rx
        MOV A, SBUF
        CLR RI
        MOV @R0, A
        INC R0
        SJMP rx
""" + " MOV R0, #40h\n")


@pytest.mark.parametrize("smod,th1,baud", [(0, 0xFD, 9600), (1, 0xFD, 19200), (0, 0xE8, 1200)])
def test_uart_mode1_timer1_baud(smod, th1, baud):
    c = chip_for(program(f"""
        MOV TMOD, #20h
        MOV TH1, #{th1}
        MOV TL1, #{th1}
        MOV PCON, #{0x80 if smod else 0}
        MOV SCON, #50h
        SETB TR1
        MOV SBUF, #55h
        SJMP $
    """))
    c.run_for(2 * 10e9 / baud + 1e5)
    u = c.peripherals["UART"]
    assert u.baud() == pytest.approx(baud, rel=0.002) and bytes(u.output) == b"\x55"
    assert u.frame() == "8N1"
    assert c.bus.read(0x30098, 1) & 0x02                          # TI


def test_uart_timer2_baud_rx_and_overrun():
    c = chip_for(program("""
        MOV RCAP2H, #0FFh
        MOV RCAP2L, #0DCh
        MOV T2CON, #34h
        MOV SCON, #50h
        MOV R0, #40h
rx:     JNB RI, rx
        MOV A, SBUF
        CLR RI
        MOV @R0, A
        INC R0
        SJMP rx
    """))
    c.run_for(1e5)
    u = c.peripherals["UART"]
    assert u.baud() == pytest.approx(9600, rel=0.001)
    u.feed(b"Hi!")
    c.run_for(5e6)
    assert bytes(c.core.iram[0x40:0x43]) == b"Hi!"
    # RI never cleared: the second byte is lost (frame "irretrievably lost")
    c2 = chip_for(program("MOV RCAP2H, #0FFh\n MOV RCAP2L, #0DCh\n MOV T2CON, #34h\n MOV SCON, #50h\n SJMP $"))
    c2.run_for(1e5)
    u2 = c2.peripherals["UART"]
    u2.feed(b"ab")
    c2.run_for(5e6)
    assert u2.rx_buf == ord("a") and u2.rx_dropped == 1


def test_uart_rx_disabled_and_mode2_9bit_address_recognition():
    c = chip_for(program("MOV TMOD, #20h\n MOV TH1, #0FDh\n SETB TR1\n MOV SCON, #40h\n SJMP $"))   # REN = 0
    c.run_for(1e5)
    u = c.peripherals["UART"]
    u.feed(b"x")
    c.run_for(3e6)
    assert u.rx_dropped == 1 and not c.bus.read(0x30098, 1) & 0x01
    # mode 2 (fosc/64), SM2 = 1, SADDR/SADEN: only an address byte (9th bit 1) that matches sets RI
    from chipem.periph.uartwire import Frame
    c = chip_for(program("MOV SADDR, #0C0h\n MOV SADEN, #0FDh\n MOV SCON, #0B0h\n SJMP $"))     # mode 2, SM2, REN
    c.run_for(1e5)
    u = c.peripherals["UART"]
    assert u.baud() == pytest.approx(XTAL / 64)
    for data, ok in [(0x1C0, True), (0x0C0, False), (0x1C2, True), (0x1C1, False), (0x1FF, True)]:
        c.bus.write(0x30098, 1, c.bus.read(0x30098, 1) & ~0x01)
        u.receive_frame(Frame(data, 9, "N", 1.0, u.baud()))
        c.run_for(1e5)
        assert bool(c.bus.read(0x30098, 1) & 0x01) == ok, hex(data)


def test_uart_framing_error_flag_with_smod0():
    from chipem.periph.uartwire import Frame
    c = chip_for(program("MOV TMOD, #20h\n MOV TH1, #0FDh\n SETB TR1\n MOV PCON, #40h\n MOV SCON, #50h\n SJMP $"))
    c.run_for(1e5)
    u = c.peripherals["UART"]
    u.receive_frame(Frame.brk_frame(12, 1.0, u.baud()))          # a break: the stop bit reads 0
    c.run_for(3e6)
    scon = c.bus.read(0x30098, 1)
    assert scon & 0x80 and u.rx_errors                             # FE (SCON.7 while SMOD0 = 1)
    c.bus.write(0x30098, 1, scon & 0x7F)                           # software clears FE
    assert not c.bus.read(0x30098, 1) & 0x80


# --------------------------------------------------------------------------------------------- PCA
def test_pca_pwm_duty_and_software_timer_flag():
    c = chip_for(program("MOV CCAP0L, #40h\n MOV CCAP0H, #40h\n MOV CCAPM0, #42h\n"
                         " MOV CCAP1L, #00h\n MOV CCAP1H, #02h\n MOV CCAPM1, #48h\n SETB CR\n SJMP $"))
    c.run_for(3000 * MC_NS)
    ed = [(e.t_ns, e.fields["new"]) for e in c.log if e.kind == "pin" and e.fields.get("pin") == "P1.3"
          and e.fields.get("field") == "level"]
    rises = [t for t, v in ed if v == "H"]
    falls = [t for t, v in ed if v == "L"]
    assert len(rises) >= 3
    hi = [f - r for r in rises for f in falls if f > r][:1]
    assert hi and hi[0] / MC_NS == pytest.approx(256 - 0x40, abs=0.01)      # high while CL >= 40h
    assert (rises[2] - rises[1]) / MC_NS == pytest.approx(256, abs=0.01)
    assert c.bus.read(0x300D8, 1) & 0x02                                    # CCF1: CH:CL reached 0200h


def test_pca_watchdog_module4_resets():
    c = chip_for(program("MOV CCAP4L, #0FFh\n MOV CCAP4H, #00h\n MOV CCAPM4, #48h\n MOV CMOD, #40h\n SETB CR\n SJMP $"))
    c.run_for(1000 * MC_NS)
    assert any(e.kind == "reset" and e.fields.get("reason") == "pca_wdt" for e in c.log)


# --------------------------------------------------------------------------------------------- SPI
def test_spi_master_bit_level_loopback():
    """MOSI wired to MISO (stimulus mirror): the master receives what it sends, edges at fosc/16."""
    c = chip_for(program("MOV SPCR, #51h\n MOV SPDAT, #0A5h\nw: MOV A, SPSR\n JNB ACC.7, w\n MOV 30h, SPDAT\n SJMP $"))
    pb = c.pins
    pb.input_listeners["P1.5"].append(lambda p: pb.set_external("P1.6", p.level()))
    c.run_for(300 * MC_NS)
    spi = c.peripherals["SPI"]
    assert iram(c, 0x30) == 0xA5 and spi.frames[-1] == (0xA5, 0xA5)
    sck = [e.t_ns for e in c.log if e.kind == "pin" and e.fields.get("pin") == "P1.7" and e.fields.get("field") == "level"]
    assert len(sck) >= 16 and (sck[-1] - sck[-3]) == pytest.approx(16 * 1e9 / XTAL)     # one SPICLK = fosc/16


def test_spi_wcol_and_responder():
    c = chip_for(program("MOV SPCR, #53h\n MOV SPDAT, #9Fh\n MOV SPDAT, #00h\n MOV 31h, SPSR\nw: MOV A, SPSR\n"
                         " JNB ACC.7, w\n MOV 30h, SPDAT\n SJMP $"))
    c.peripherals["SPI"].responder = lambda tx, pcs, bits: tx ^ 0xFF
    c.run_for(2000 * MC_NS)
    assert iram(c, 0x31) & 0x40 and iram(c, 0x30) == 0x60


# --------------------------------------------------------------------------------------------- watchdog / IAP / X2
def test_wdt_period_and_feed():
    c = chip_for(program("MOV WDTRST, #1Eh\n MOV WDTRST, #0E1h\n SJMP $"))
    c.run_for(16000 * MC_NS)
    assert not any(e.kind == "reset" and e.fields.get("reason") == "wdt" for e in c.log)
    c.run_for(500 * MC_NS)
    resets = [e.t_ns for e in c.log if e.kind == "reset" and e.fields.get("reason") == "wdt"]
    # enabled by the WDTRST write ending at machine cycle 8 (LJMP, MOV SP, 2 x MOV dir,#imm), reset 16383 later
    assert len(resets) == 1 and resets[0] / MC_NS == pytest.approx(8 + 16383, abs=1.5)


def test_iap_calls_program_erase_read_and_security():
    c = chip_for(program("""
        ORL AUXR1, #20h
        MOV R1, #08h
        MOV DPTR, #3000h
        LCALL 0FFF0h            ; erase page 3000h
        MOV 30h, A
        MOV R1, #02h
        MOV DPTR, #3005h
        MOV A, #5Ah
        LCALL 0FFF0h            ; program
        MOV R1, #02h
        MOV A, #0Fh
        LCALL 0FFF0h            ; program again: bits only clear (5Ah AND 0Fh = 0Ah)
        MOV R1, #03h
        LCALL 0FFF0h            ; read
        MOV 31h, A
        MOV R1, #05h
        MOV DPL, #00h
        LCALL 0FFF0h            ; security bit 1 (write protect)
        MOV R1, #02h
        MOV DPTR, #3006h
        MOV A, #00h
        LCALL 0FFF0h            ; refused
        MOV 32h, A
        MOV R1, #07h
        MOV DPL, #00h
        LCALL 0FFF0h
        MOV 33h, A
    """))
    c.core.write_mem(0x3000, b"\x00" * 16)
    c.run_for(500 * MC_NS)
    assert iram(c, 0x30) == 0 and iram(c, 0x31) == 0x0A and iram(c, 0x32) != 0 and iram(c, 0x33) & 1
    assert c.core.code[0x3000] == 0xFF and c.core.code[0x3005] == 0x0A and c.core.code[0x3006] == 0xFF


def test_boot_rom_overlay_hides_user_flash_top():
    c = chip_for(program("MOV DPTR, #0FC00h\n CLR A\n MOVC A, @A+DPTR\n MOV 30h, A\n ORL AUXR1, #20h\n CLR A\n"
                         " MOVC A, @A+DPTR\n MOV 31h, A\n ANL AUXR1, #0DFh\n CLR A\n MOVC A, @A+DPTR\n MOV 32h, A"))
    c.core.write_mem(0xFC00, b"\x77")
    c.run_for(100 * MC_NS)
    assert (iram(c, 0x30), iram(c, 0x32)) == (0x77, 0x77) and iram(c, 0x31) == 0x80     # boot ROM: SJMP $ stub


def test_status_bit_boots_from_boot_vector():
    c = chip_for(program("NOP"))
    fl = c.peripherals["FLASHCTL"]
    fl.boot_vector, fl.status_bit = 0x40, 1
    c.core.write_mem(0x4000, b"\x75\x30\x99\x80\xfe")     # MOV 30h,#99h; SJMP $
    c.reset()
    c.run_for(10 * MC_NS)
    assert iram(c, 0x30) == 0x99 and c.core.read_reg("auxr1") & 0x20


def test_x2_six_clock_mode_doubles_cpu_and_timer_rates():
    c = chip_for(program("ORL CKCON, #01h\n MOV TMOD, #02h\n MOV TH0, #9Ch\n SETB TR0\nl: JNB TF0, l\n CLR TF0\n"
                         " CPL P1.0\n SJMP l"))
    c.run_for(5000 * 6 * 1e9 / XTAL)
    assert c.core.clocks_per_mc == 6
    falls = [e.t_ns for e in c.log if e.kind == "pin" and e.fields.get("pin") == "P1.0" and e.fields.get("field") == "level"]
    assert (falls[3] - falls[2]) == pytest.approx(100 * 6 * 1e9 / XTAL)
    # CKCON.T0X2 = 1 keeps Timer 0 at 12 clocks while the CPU runs at 6
    c = chip_for(program("MOV CKCON, #03h\n MOV TMOD, #02h\n MOV TH0, #9Ch\n SETB TR0\nl: JNB TF0, l\n CLR TF0\n"
                         " CPL P1.0\n SJMP l"))
    c.run_for(5000 * 12 * 1e9 / XTAL)
    falls = [e.t_ns for e in c.log if e.kind == "pin" and e.fields.get("pin") == "P1.0" and e.fields.get("field") == "level"]
    assert (falls[3] - falls[2]) == pytest.approx(100 * 12 * 1e9 / XTAL)


def test_xbus_external_sram_and_floating_bus():
    c = chip_for(program("MOV DPTR, #1234h\n MOVX A, @DPTR\n MOV 30h, A\n MOV A, #66h\n MOVX @DPTR, A\n"
                         " MOV AUXR, #02h\n MOV DPTR, #0010h\n MOV A, #77h\n MOVX @DPTR, A\n MOVX A, @DPTR\n MOV 31h, A"))
    c.run_for(100 * MC_NS)
    assert iram(c, 0x30) == 0xFF and iram(c, 0x31) == 0xFF            # nothing on the bus
    c = chip_for(program("MOV DPTR, #1234h\n MOV A, #66h\n MOVX @DPTR, A\n MOVX A, @DPTR\n MOV 30h, A\n"
                         " MOV AUXR, #02h\n MOV DPTR, #0010h\n MOV A, #77h\n MOVX @DPTR, A"))
    ram = ParallelSram("32K", 8)
    c.peripherals["XBUS"].attach_memory(ram)
    c.run_for(100 * MC_NS)
    assert iram(c, 0x30) == 0x66 and ram.read(0x1234) == 0x66 and ram.read(0x10) == 0x77
    assert c.core.xram[0x10] == 0                                       # EXTRAM: on-chip XRAM untouched


def test_powerdown_holds_uart_frame_until_wake():
    """The oscillator stops in power-down: a character being shifted out finishes (TI) only after the wake-up, with
    its remaining time (chipem/periph/lowpower.Freezer holds the UART's events)."""
    c = chip_for(program("""
        MOV TMOD, #20h
        MOV TH1, #0FDh
        SETB TR1
        MOV SCON, #50h
        CLR IT0
        SETB EX0
        SETB EA
        MOV SBUF, #41h
        ORL PCON, #02h
        MOV 30h, SCON
        SJMP $
    """, isrs={0x03: " CLR EX0\n RETI"}))
    c.run_for(5e6)                                   # 5 ms asleep: a 9600-baud frame (1.04 ms) would be long done
    assert c.sleeping and not c.peripherals["UART"].get("SCON") & 0x02
    c.pins.set_external("P3.2", "L")
    c.run_for(100 * MC_NS)
    assert not c.sleeping and not iram(c, 0x30) & 0x02      # TI still clear right after the wake-up
    c.run_for(1.2e6)
    assert c.peripherals["UART"].get("SCON") & 0x02
