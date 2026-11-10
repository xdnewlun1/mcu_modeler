"""TI C2000 peripheral models at the MMIO level on the TMS320F28232 description and a fake C28x core
(tests/c28x_fake.py): CPU timers, SCI, GPIO, XINT, watchdog/PLL, ADC and PIE vector fetch."""
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6


@pytest.fixture
def chip():
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7029, 1, 0x0068)            # WDCR: disable the watchdog (WDCHK=101, WDDIS)
    return c


def rd(c, a, n=1):
    return c.bus.read(a, n)


def wr(c, a, v, n=1):
    c.bus.write(a, n, v)


def pie_flags(c, g):
    return c.arch.pie.ifr[g]


def warns(c):
    return [e.get("msg") for e in c.log if e.kind == "warn"]


# ------------------------------------------------------------------ CPU timers
def test_cputimer_period_tif_and_pie(chip):
    t0 = 0x0C00
    wr(chip, t0 + 4, 0x0010)                  # TSS: stop
    wr(chip, t0 + 2, 999, 2)                  # PRD
    wr(chip, t0 + 6, 0)                       # TPR
    wr(chip, t0 + 4, 0x4020)                  # TIE | TRB, TSS=0 → start
    assert rd(chip, t0, 2) == 999
    chip.run_cycles(500)
    assert rd(chip, t0, 2) == 499
    assert not rd(chip, t0 + 4) & 0x8000
    wr(chip, 0x0CE2, 1 << 6)                  # PIEIER1.7
    chip.run_cycles(501)                      # 1000 decrements → reload at the 1000th cycle
    assert rd(chip, t0 + 4) & 0x8000          # TIF
    assert pie_flags(chip, 1) == 1 << 6 and chip.arch.pie.ack & 1 and chip.core.regs["ifr"] & 1
    tim = chip.peripherals["CPUTIMER0"]
    assert tim.expiries == 1 and tim.period_cycles() == 1000
    wr(chip, t0 + 4, 0xC000)                  # write 1 to TIF clears it (TIE kept)
    assert not rd(chip, t0 + 4) & 0x8000 and rd(chip, t0 + 4) & 0x4000
    chip.run_cycles(3000)
    assert tim.expiries == 4


def test_cputimer_prescaler_and_stop(chip):
    t1 = 0x0C08
    wr(chip, t1 + 4, 0x0010)
    wr(chip, t1 + 2, 9, 2)
    wr(chip, t1 + 6, 3)                       # TDDR = 3 → /4
    wr(chip, t1 + 4, 0x4020)
    chip.run_cycles(40)
    assert chip.peripherals["CPUTIMER1"].expiries == 1
    assert chip.core.regs["ifr"] & (1 << 12)  # TINT1 → INT13 directly
    wr(chip, t1 + 4, 0x0010)                  # stop
    v = rd(chip, t1, 2)
    chip.run_cycles(100)
    assert rd(chip, t1, 2) == v and chip.peripherals["CPUTIMER1"].expiries == 1


def test_pie_vector_fetch_on_timer(chip):
    core = chip.core
    wr(chip, 0x0D00 + 2 * 38, 0x33A000, 2)    # TINT0 vector (INT1.7)
    wr(chip, 0x0CE0, 1)                       # ENPIE
    wr(chip, 0x0CE2, 1 << 6)
    core.regs["ier"] = 1
    core.regs["st1"] &= ~1                    # EINT
    t0 = 0x0C00
    wr(chip, t0 + 4, 0x0010)
    wr(chip, t0 + 2, 99, 2)
    wr(chip, t0 + 4, 0x4020)
    chip.run_cycles(150)
    assert core.taken == [(0x33A000, 1)]
    assert rd(chip, 0x0CE0) & 0xFFFE == 0x0D4C           # PIEVECT
    assert pie_flags(chip, 1) == 0 and chip.arch.pie.ack & 1
    # without the PIEACK write no further INT1 reaches the CPU, although TIF fires again
    core.regs["st1"] &= ~1
    core.regs["ier"] = 1
    chip.run_cycles(200)
    assert len(core.taken) == 1 and pie_flags(chip, 1) == 1 << 6
    wr(chip, 0x0CE1, 1)                       # PIEACK group 1
    chip.run_cycles(1)
    assert len(core.taken) == 2


def test_cpu_vector_table_when_pie_disabled(chip):
    core = chip.core
    # ENPIE = 0, VMAP = 1: INT13 vector from the boot ROM table at 0x3FFFC0 + 2*13
    chip.bus.write(0x3FFFC0 + 26, 2, 0x339000)
    core.regs["ier"] = 1 << 12
    core.regs["st1"] &= ~1
    chip.arch.set_irq("INT13", True)
    chip.run_cycles(2)
    assert core.taken == [(0x339000, 13)]
    # ITRAP → vector 19 through the same path
    chip.bus.write(0x3FFFC0 + 38, 2, 0x339100)
    assert chip.arch.itrap(0x330010) and core.taken[-1] == (0x339100, 19)


def test_bad_vector_is_reported_not_taken(chip):
    core = chip.core
    wr(chip, 0x0CE0, 1)
    wr(chip, 0x0CE2, 1 << 3)
    core.regs["ier"] = 1
    core.regs["st1"] &= ~1
    chip.arch.set_irq("XINT1", True)
    chip.run_cycles(2)
    assert core.taken == []
    assert any(e.get("type") == "bad_vector" for e in chip.log if e.kind == "fault")


# ------------------------------------------------------------------ SCI
def sci_setup(c, base=0x7050, brr=26, fifo=False, pins=True):
    wr(c, 0x701C, rd(c, 0x701C) | 0x0C00)     # PCLKCR0: SCIAENCLK | SCIBENCLK
    if pins and base == 0x7050:
        wr(c, 0x6F89, rd(c, 0x6F89) | 0x0500) # GPAMUX2 (high word): GPIO28 -> SCIRXDA, GPIO29 -> SCITXDA
    wr(c, base + 0x0, 0x0007)                 # 8N1
    wr(c, base + 0x2, brr >> 8)
    wr(c, base + 0x3, brr & 0xFF)
    if fifo:
        wr(c, base + 0xA, 0xE040)             # SCIRST | SCIFFENA | TXFIFOXRESET, TXFFINTCLR
        wr(c, base + 0xB, 0x2044)             # RXFIFORESET, RXFFINTCLR, RXFFIL = 4
    wr(c, base + 0x1, 0x0023)                 # SWRESET | TXENA | RXENA


def test_sci_baud_frame_and_tx(chip):
    sci = chip.peripherals["SCIA"]
    sci_setup(chip)
    assert sci.module_hz() == pytest.approx(2.5e6)       # LSPCLK = 10 MHz / 4
    assert sci.baud() == pytest.approx(2.5e6 / (27 * 8))
    assert sci.frame() == "8N1" and sci.char_bits() == 10
    got = []
    sci.tx_listeners.append(lambda blk, b: got.append(b))
    wr(chip, 0x7059, ord("h"))
    assert bytes(sci.output) == b"h" and got == [ord("h")]
    st = rd(chip, 0x7054)
    assert st & 0x80 and not st & 0x40                   # TXRDY, not TXEMPTY
    wr(chip, 0x7059, ord("i"))
    assert not rd(chip, 0x7054) & 0x80                   # second byte waits in TXBUF
    cyc = int(sci.char_ns() * chip.clock.hz / 1e9) + 2
    chip.run_cycles(cyc)
    assert bytes(sci.output) == b"hi" and rd(chip, 0x7054) & 0x80
    chip.run_cycles(cyc)
    assert rd(chip, 0x7054) & 0xC0 == 0xC0
    assert any(e.kind == "uart" and e.get("byte") == ord("i") for e in chip.log)


def test_sci_disabled_and_reset_states(chip):
    sci = chip.peripherals["SCIA"]
    wr(chip, 0x7059, 0x41)
    assert sci.output == b"" and any("SWRESET" in (m or "") for m in warns(chip))


def test_sci_rx_and_interrupt_lines(chip):
    sci = chip.peripherals["SCIA"]
    sci_setup(chip)
    wr(chip, 0x0CF2, 0x3)                     # PIEIER9: SCIRXINTA, SCITXINTA
    wr(chip, 0x7054, 0x0002)                  # RXBKINTENA
    sci.feed(b"ok")
    chip.run_cycles(int(sci.char_ns() * 1e-9 * chip.clock.hz) + 5)
    assert rd(chip, 0x7055) & 0x40            # RXRDY
    assert pie_flags(chip, 9) & 1
    assert rd(chip, 0x7057) == ord("o")
    assert not rd(chip, 0x7055) & 0x40
    chip.run_cycles(int(sci.char_ns() * 1e-9 * chip.clock.hz) + 5)
    assert rd(chip, 0x7057) == ord("k")
    # TX interrupt: TXRDY with TXINTENA → SCITXINTA
    wr(chip, 0x7054, 0x0003)
    assert pie_flags(chip, 9) & 2


def _drops(c):
    return [(e.get("byte"), e.get("reason")) for e in c.log if e.kind == "uart" and e.get("dir") == "drop"]


def test_sci_rx_dropped_when_not_listening(chip):
    sci = chip.peripherals["SCIA"]
    sci_setup(chip, pins=False)
    char = int(sci.char_ns() * 1e-9 * chip.clock.hz) + 5     # (in SW RESET the host falls back to 115200 baud)
    cyc = lambda n: chip.run_cycles(n * char)     # noqa: E731               # GPIO28 still a GPIO: nothing reaches SCIRXDA
    sci.feed(b"a")
    cyc(1)
    assert not rd(chip, 0x7055) & 0x40 and _drops(chip) == [(ord("a"), "pin_not_muxed")]
    wr(chip, 0x6F89, rd(chip, 0x6F89) | 0x0100)
    wr(chip, 0x7051, 0x0022)                  # RXENA = 0
    sci.feed(b"b")
    cyc(1)
    wr(chip, 0x7051, 0x0001)                  # SW RESET = 0
    sci.feed(b"c")
    cyc(1)
    assert _drops(chip)[1:] == [(ord("b"), "rx_disabled"), (ord("c"), "reset")]
    wr(chip, 0x7051, 0x0023)
    sci.feed(b"d")
    cyc(1)
    assert rd(chip, 0x7055) & 0x40 and rd(chip, 0x7057) == ord("d") and sci.rx_dropped == 3
    assert not warns(chip)


def test_sci_overrun_overwrites_previous(chip):
    sci = chip.peripherals["SCIA"]
    sci_setup(chip)
    sci.feed(b"xyz")                          # nobody reads SCIRXBUF
    chip.run_cycles(int(3.5 * sci.char_ns() * 1e-9 * chip.clock.hz))
    st = rd(chip, 0x7055)
    assert st & 0x40 and st & 0x08 and st & 0x80          # RXRDY, OE, RXERROR
    assert rd(chip, 0x7057) == ord("z")                   # the newest character; x and y were overwritten
    assert _drops(chip) == [(ord("x"), "overrun"), (ord("y"), "overrun")]
    assert rd(chip, 0x7055) & 0x08                        # OE survives the SCIRXBUF read ...
    wr(chip, 0x7051, 0x0003)
    wr(chip, 0x7051, 0x0023)
    assert not rd(chip, 0x7055) & 0x88                    # ... and clears on SW RESET


def test_sci_fifo_overflow(chip):
    sci = chip.peripherals["SCIA"]
    sci_setup(chip, fifo=True)
    data = bytes(range(0x41, 0x41 + 18))
    sci.feed(data)
    chip.run_cycles(int(18.5 * sci.char_ns() * 1e-9 * chip.clock.hz))
    ffrx = rd(chip, 0x705B)
    assert (ffrx >> 8) & 0x1F == 16 and ffrx & 0x8000     # RXFFST = 16, RXFFOVF
    assert bytes(rd(chip, 0x7057) for _ in range(16)) == data[:16]
    assert [b for b, _ in _drops(chip)] == list(data[16:])
    wr(chip, 0x705B, rd(chip, 0x705B) | 0x4000)           # RXFFOVRCLR
    assert not rd(chip, 0x705B) & 0x8000


def test_sci_fifo(chip):
    sci = chip.peripherals["SCIA"]
    sci_setup(chip, fifo=True)
    for b in b"abcd":
        wr(chip, 0x7059, b)
    assert (rd(chip, 0x705A) >> 8) & 0x1F == 3           # one byte in the shifter, 3 queued
    chip.run_cycles(int(5 * sci.char_ns() * 1e-9 * chip.clock.hz) + 10)
    assert bytes(sci.output) == b"abcd" and (rd(chip, 0x705A) >> 8) & 0x1F == 0
    assert rd(chip, 0x705A) & 0x80                        # TXFFINT (TXFFST <= TXFFIL = 0)
    wr(chip, 0x0CF2, 0x1)
    wr(chip, 0x705B, rd(chip, 0x705B) | 0x0020)          # RXFFIENA
    sci.feed(b"12345")
    chip.run_cycles(int(3 * sci.char_ns() * 1e-9 * chip.clock.hz))
    assert (rd(chip, 0x705B) >> 8) & 0x1F == 3 and not rd(chip, 0x705B) & 0x80
    chip.run_cycles(int(2 * sci.char_ns() * 1e-9 * chip.clock.hz) + 10)
    ffrx = rd(chip, 0x705B)
    assert (ffrx >> 8) & 0x1F == 5 and ffrx & 0x80 and pie_flags(chip, 9) & 1
    assert bytes(rd(chip, 0x7057) for _ in range(5)) == b"12345"
    wr(chip, 0x705B, rd(chip, 0x705B) | 0x40)            # RXFFINTCLR
    assert not rd(chip, 0x705B) & 0x80


def test_sci_loopback(chip):
    sci = chip.peripherals["SCIB"]
    sci_setup(chip, base=0x7750)
    wr(chip, 0x7750, 0x0017)                  # LOOPBKENA
    wr(chip, 0x7759, 0x5A)
    chip.run_cycles(int(2 * sci.char_ns() * 1e-9 * chip.clock.hz) + 5)
    assert rd(chip, 0x7755) & 0x40 and rd(chip, 0x7757) == 0x5A


# ------------------------------------------------------------------ GPIO
def test_gpio_output_toggle_and_pin_events(chip):
    pins = chip.pins
    gp34 = pins.get("GPIO34")
    assert gp34.owner == "GPIO" and gp34.direction == "in" and gp34.pull == "up"
    wr(chip, 0x6F96, 0, 2)                    # GPBMUX1: GPIO32-47 = GPIO
    wr(chip, 0x6F9A, 1 << 2, 2)               # GPBDIR: GPIO34 out
    assert gp34.direction == "out" and gp34.level() == "L"
    wr(chip, 0x6FCA, 1 << 2, 2)               # GPBSET
    assert gp34.level() == "H" and rd(chip, 0x6FC8, 2) & 4
    wr(chip, 0x6FCE, 1 << 2, 2)               # GPBTOGGLE
    assert gp34.level() == "L"
    wr(chip, 0x6FCE, 1 << 2)                  # 16-bit write to the low word works too
    assert gp34.level() == "H"
    wr(chip, 0x6FCC, 1 << 2, 2)               # GPBCLEAR
    assert gp34.level() == "L"
    assert rd(chip, 0x6FCA, 2) == 0 and rd(chip, 0x6FCE, 2) == 0   # SET/TOGGLE read 0
    st = chip.peripherals["GPIO"].pin_stats()
    assert st["GPIO34"]["changes"] == 4
    wr(chip, 0x6FC8, 0x0004)                  # 16-bit GPBDAT low-word write: GPIO34 high, upper latch untouched
    assert gp34.level() == "H" and chip.peripherals["GPIO"].latch["B"] == 0x0004


def test_gpio_inputs_pullups_and_floating(chip):
    pins = chip.pins
    assert rd(chip, 0x6FC0, 2) >> 12 & 1                  # GPIO12: pull-up enabled at reset → reads 1
    pins.set_external("GPIO12", "L")
    assert not rd(chip, 0x6FC0, 2) >> 12 & 1
    assert rd(chip, 0x6FC0) >> 12 & 1 == 0                # 16-bit read of the low word
    pins.set_external("GPIO12", None)
    assert rd(chip, 0x6FC0, 2) >> 12 & 1
    assert pins.get("GPIO5").pull is None                 # GPIO0-11: pull-ups disabled at reset
    rd(chip, 0x6FC0, 2)
    assert any("GPIO5" in (m or "") and "floating" in m for m in warns(chip))
    assert sum("floating" in (m or "") for m in warns(chip)) == 1     # one warning for the whole port read
    wr(chip, 0x6F8C, 0, 2)                                # GPAPUD = 0 → all pull-ups on
    assert pins.get("GPIO5").pull == "up" and rd(chip, 0x6FC0, 2) & 0x20


def test_gpio_mux_and_eallow(chip):
    pins = chip.pins
    wr(chip, 0x6F88, (1 << 24) | (1 << 26), 2)            # GPAMUX2: GPIO28/29 → MUX1 (SCIRXDA / SCITXDA)
    assert pins.get("GPIO28").owner == "SCIRXDA"
    tx = pins.get("GPIO29")
    assert tx.owner == "SCITXDA" and tx.direction == "out" and tx.level() == "H"
    wr(chip, 0x6F88, 2 << 24, 2)                          # MUX2 → XZCS6
    assert pins.get("GPIO28").owner == "XZCS6" and pins.get("GPIO29").owner == "GPIO"
    wr(chip, 0x6FA6, 1, 2)                                # GPCMUX1 GPIO64 = 1 is still GPIO on port C
    assert pins.get("GPIO64").owner == "GPIO"
    chip.core.regs["st1"] &= ~EALLOW
    wr(chip, 0x6F9A, 0xFFFF, 2)                           # protected: ignored
    assert rd(chip, 0x6F9A, 2) == 0
    assert any("EALLOW" in (m or "") for m in warns(chip))
    wr(chip, 0x6FCA, 1 << 2, 2)                           # data registers are not protected
    assert chip.peripherals["GPIO"].latch["B"] & 4


# ------------------------------------------------------------------ XINT
def test_xint_edge_to_pie(chip):
    wr(chip, 0x6FE0, 12)                                  # GPIOXINT1SEL = GPIO12
    wr(chip, 0x7070, 0x0001)                              # XINT1CR: enable, falling
    wr(chip, 0x0CE2, 1 << 3)                              # PIEIER1.4
    chip.pins.set_external("GPIO12", "H")
    assert pie_flags(chip, 1) == 0
    chip.pins.set_external("GPIO12", "L")
    assert pie_flags(chip, 1) == 1 << 3
    chip.run_cycles(100)
    assert 95 <= rd(chip, 0x7078) <= 105                  # XINT1CTR counts since the edge
    wr(chip, 0x6FE1, 13)                                  # XINT2 on GPIO13, both edges
    wr(chip, 0x7071, 0x000D)
    chip.pins.set_external("GPIO13", "L")
    chip.pins.set_external("GPIO13", "H")
    assert pie_flags(chip, 1) & (1 << 4) and chip.peripherals["XINT"].edges[2] == 2


# ------------------------------------------------------------------ system control
def test_watchdog_timeout_resets(chip):
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    # WDCLK = 20 MHz / 512 → 256 counts ≈ 6.55 ms
    c.run_for(6e6)
    assert not [e for e in c.log if e.kind == "reset" and e.get("reason") == "watchdog"]
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7025, 1, 0x55)
    c.bus.write(0x7025, 1, 0xAA)                          # key sequence clears WDCNTR
    assert c.bus.read(0x7023, 1) == 0
    c.run_for(6e6)
    assert not [e for e in c.log if e.kind == "reset" and e.get("reason") == "watchdog"]
    c.run_for(1e6)
    assert [e for e in c.log if e.kind == "reset" and e.get("reason") == "watchdog"]
    assert c.bus.read(0x7029, 1) & 0x80                   # WDFLAG after the watchdog reset
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7029, 1, 0x0018)                        # WDCHK = 011 (wrong) → immediate reset request
    assert c.arch.reset_requested == "watchdog"


def test_watchdog_disable_and_override_lock(chip):
    sc = chip.peripherals["SYSCTRL"]
    assert not sc.wd_enabled()
    wr(chip, 0x7022, 0x0001)                              # SCSR: clear WDOVERRIDE → WDDIS locked
    wr(chip, 0x7029, 0x0028)                              # try to enable: WDDIS stays 1
    assert not sc.wd_enabled()
    chip.run_for(20e6)
    assert not [e for e in chip.log if e.kind == "reset" and e.get("reason") == "watchdog"]


def test_pll_and_peripheral_clocks(chip):
    sc = chip.peripherals["SYSCTRL"]
    wr(chip, 0x7011, 0x0001)                              # DIVSEL = 0 (/4) before changing PLLCR
    assert chip.clock.hz == 5e6
    wr(chip, 0x7021, 10)                                  # PLLCR.DIV = 10
    assert not rd(chip, 0x7011) & 1                       # relocking
    chip.run_for(131072 / 20e6 * 1e9 + 1000)
    assert rd(chip, 0x7011) & 1 and chip.clock.hz == 50e6
    wr(chip, 0x7011, 0x0101)                              # DIVSEL = 2 → 100 MHz
    assert chip.clock.hz == 100e6
    assert sc.lspclk_hz() == 25e6 and sc.hspclk_hz() == 50e6
    wr(chip, 0x701B, 0)                                   # LOSPCP = 0 → /1
    assert sc.lspclk_hz() == 100e6
    assert not sc.clock_enabled("SCIAENCLK")
    wr(chip, 0x701C, 1 << 10)
    assert sc.clock_enabled("SCIAENCLK") and sc.clock_enabled("CPUTIMER0ENCLK")
    chip.core.regs["st1"] &= ~EALLOW
    wr(chip, 0x7021, 5)
    assert rd(chip, 0x7021) == 10                         # EALLOW-protected


# ------------------------------------------------------------------ ADC
def test_adc_software_conversion(chip):
    adc = chip.peripherals["ADC"]
    adc.set_channel("ADCINA3", 0x800)
    adc.set_channel("B1", 0x123)
    wr(chip, 0x7100, 0x0010)                              # SEQ_CASC
    wr(chip, 0x7102, 1)                                   # MAX_CONV1 = 1 → 2 conversions
    wr(chip, 0x7103, 0x0093)                              # CONV00 = A3, CONV01 = B1
    wr(chip, 0x0CE2, 1)                                   # PIEIER1.1 SEQ1INT
    wr(chip, 0x7101, 0x2800)                              # SOC_SEQ1 | INT_ENA_SEQ1
    chip.run_for(3000)                                    # ADCCLK = HSPCLK = 5 MHz here: results at 15, 19 cycles
    assert rd(chip, 0x7108) == 0x800 << 4 and rd(chip, 0x7109) == 0x123 << 4
    assert rd(chip, 0x0B00) == 0x800 and rd(chip, 0x0B01) == 0x123
    assert rd(chip, 0x7119) & 1 and pie_flags(chip, 1) & 1
    wr(chip, 0x7119, 0x0010)                              # INT_SEQ1_CLR
    assert not rd(chip, 0x7119) & 1


ADCTRL1, ADCTRL2, ADCMAXCONV, ADCCHSEL1, ADCASEQSR, ADCRES0, ADCTRL3, ADCST, HISPCP = \
    0x7100, 0x7101, 0x7102, 0x7103, 0x7107, 0x7108, 0x7118, 0x7119, 0x701A
CASC, CONT_RUN, SEQ_OVRD = 1 << 4, 1 << 6, 1 << 5
SOC1, RST1, INT_ENA1, INT_MOD1, EXT_SOC1, SOC2, INT_ENA2 = 1 << 13, 1 << 14, 1 << 11, 1 << 10, 1 << 7, 1 << 5, 1 << 3


def adc_on(c):
    wr(c, 0x701C, 1 << 3)                                 # PCLKCR0.ADCENCLK
    wr(c, ADCTRL3, 0x00E0)                                # bandgap, reference and core powered up


def run_to(c, t):
    c.run_cycles(t - c.now_cycles())


@pytest.mark.parametrize("hispcp,clkps,cps,acq,first,step", [
    (1, 0, 0, 0, 15, 4),                                  # ADCCLK = HSPCLK = SYSCLKOUT / 2: 2 cycles
    (0, 0, 0, 0, 8, 2),                                   # ADCCLK = SYSCLKOUT: 3 (2.5 rounded up) + 1 + 4
    (1, 1, 1, 3, 84, 40),                                 # HSPCLK / 2 / 2 = 8 cycles; ACQ_PS = 3
])
def test_adc_conversion_timing_from_the_clock_registers(chip, hispcp, clkps, cps, acq, first, step):
    """SPRS439 7.10.4: first result 2.5 + (ACQ_PS + 1) + 4 ADCCLK after the trigger, then one every 2 + ACQ_PS."""
    adc = chip.peripherals["ADC"]
    for i in range(3):
        adc.set_channel(f"A{i}", 0x100 + i)
    adc_on(chip)
    wr(chip, HISPCP, hispcp)
    wr(chip, ADCTRL3, 0x00E0 | (clkps << 1))
    wr(chip, ADCTRL1, CASC | (cps << 7) | (acq << 8))
    wr(chip, ADCMAXCONV, 2)                               # 3 conversions
    wr(chip, ADCCHSEL1, 0x210)
    c0 = chip.now_cycles()
    wr(chip, ADCTRL2, SOC1 | INT_ENA1)
    assert rd(chip, ADCST) & 4 and (rd(chip, ADCASEQSR) >> 8) & 0xF == 2      # SEQ1_BSY, SEQ_CNTR = MAX_CONV1
    times = [first, first + step, first + 2 * step]
    for k, t in enumerate(times):
        run_to(chip, c0 + t - 1)
        assert rd(chip, ADCRES0 + k) == 0
        run_to(chip, c0 + t)
        assert rd(chip, ADCRES0 + k) == (0x100 + k) << 4 and rd(chip, 0x0B00 + k) == 0x100 + k
        if k < 2:
            assert (rd(chip, ADCASEQSR) >> 8) & 0xF == 1 - k and not rd(chip, ADCST) & 1
    st = rd(chip, ADCST)
    assert st & 1 and not st & 4 and pie_flags(chip, 1) & 1      # EOS at the last result: INT_SEQ1, SEQ1INT
    assert rd(chip, ADCASEQSR) & 0xF == 3


def test_adc_simultaneous_sampling(chip):
    adc = chip.peripherals["ADC"]
    for i in range(8):
        adc.set_channel(f"A{i}", 0x10 + i)
        adc.set_channel(f"B{i}", 0x20 + i)
    adc_on(chip)
    wr(chip, ADCTRL3, 0x00E1)                             # SMODE_SEL
    wr(chip, ADCTRL1, CASC)
    wr(chip, ADCMAXCONV, 1)                               # 2 pairs
    wr(chip, ADCCHSEL1, 0x92)                             # CONV00 = A2/B2, CONV01 = 9 -> A1/B1 (MSB ignored)
    c0 = chip.now_cycles()
    wr(chip, ADCTRL2, SOC1)
    run_to(chip, c0 + 15)                                 # A at 15, B one ADCCLK (2) later, next pair 3 ADCCLK on
    assert [rd(chip, 0x0B00 + k) for k in range(4)] == [0x12, 0, 0, 0]
    run_to(chip, c0 + 17)
    assert [rd(chip, 0x0B00 + k) for k in range(4)] == [0x12, 0x22, 0, 0]
    run_to(chip, c0 + 22)
    assert not rd(chip, ADCST) & 1
    run_to(chip, c0 + 23)
    assert [rd(chip, 0x0B00 + k) for k in range(4)] == [0x12, 0x22, 0x11, 0x21] and rd(chip, ADCST) & 1


@pytest.mark.parametrize("ovrd", [0, 1])
def test_adc_continuous_run(chip, ovrd):
    adc = chip.peripherals["ADC"]
    adc_on(chip)
    wr(chip, ADCTRL1, CASC | CONT_RUN | (SEQ_OVRD if ovrd else 0))
    wr(chip, ADCMAXCONV, 1)                               # 2 conversions per session
    wr(chip, ADCCHSEL1, 0x1010)                           # A0, A1, A0, A1
    wr(chip, 0x7104, 0x1010)
    adc.set_channel("A0", 5)
    adc.set_channel("A1", 6)
    c0 = chip.now_cycles()
    wr(chip, ADCTRL2, SOC1 | INT_ENA1)
    run_to(chip, c0 + 19)                                 # session 1 ends at 19 (15, 19)
    assert rd(chip, ADCST) & 1
    wr(chip, ADCST, 1 << 4)                               # INT_SEQ1_CLR
    run_to(chip, c0 + 26)                                 # session 2 runs on back to back: 23, 27
    assert not rd(chip, ADCST) & 1 and rd(chip, ADCST) & 4
    run_to(chip, c0 + 27)
    assert rd(chip, ADCST) & 1
    run_to(chip, c0 + 35)                                 # session 3: 31, 35
    res = [rd(chip, 0x0B00 + k) for k in range(8)]
    if ovrd:                                              # SEQ_OVRD: on through the result registers
        assert res == [5, 6, 5, 6, 5, 6, 0, 0] and rd(chip, ADCASEQSR) & 0xF == 6
    else:                                                 # back to CONV00 at every end of sequence
        assert res == [5, 6, 0, 0, 0, 0, 0, 0] and rd(chip, ADCASEQSR) & 0xF == 0
    n = adc.conversions
    wr(chip, ADCTRL1, CASC)                               # CONT_RUN off: stops after the current session
    run_to(chip, c0 + 200)
    assert adc.conversions == n + 2 and not rd(chip, ADCST) & 4


def test_adc_int_mod_every_other_end_of_sequence(chip):
    adc_on(chip)
    wr(chip, ADCTRL1, CASC)
    wr(chip, ADCTRL2, INT_ENA1 | INT_MOD1)
    wr(chip, ADCTRL2, INT_ENA1 | INT_MOD1 | SOC1)
    chip.run_cycles(40)
    st = rd(chip, ADCST)
    assert not st & 1 and st & (1 << 6) and not pie_flags(chip, 1) & 1      # EOS_BUF1 set, no interrupt
    wr(chip, ADCTRL2, INT_ENA1 | INT_MOD1 | SOC1)
    chip.run_cycles(40)
    st = rd(chip, ADCST)
    assert st & 1 and not st & (1 << 6) and pie_flags(chip, 1) & 1


def test_adc_dual_sequencer_arbitration(chip):
    """One converter: a SEQ1 SOC while SEQ2 converts waits; when both are pending SEQ1 goes first."""
    adc = chip.peripherals["ADC"]
    adc.set_channel("A0", 1)
    adc.set_channel("B0", 2)
    adc_on(chip)
    wr(chip, ADCTRL1, 0)                                  # dual sequencers, 1 conversion each
    wr(chip, ADCCHSEL1, 0x0)                              # CONV00 = A0
    wr(chip, 0x7105, 0x8)                                 # CONV08 = B0
    c0 = chip.now_cycles()
    wr(chip, ADCTRL2, SOC2)                               # SEQ2 converts 0 .. 15
    chip.run_cycles(1)
    wr(chip, ADCTRL2, SOC1)                               # converter busy: SEQ1 pending
    chip.run_cycles(1)
    wr(chip, ADCTRL2, SOC2)                               # SEQ2 busy: pending
    assert rd(chip, ADCTRL2) & (SOC1 | SOC2) == SOC1 | SOC2 and rd(chip, ADCST) & 0xC == 8
    run_to(chip, c0 + 15)
    st = rd(chip, ADCST)
    assert st & 2 and not st & 1 and st & 0xC == 4        # SEQ2 done; SEQ1 (priority) converts now
    assert rd(chip, ADCTRL2) & (SOC1 | SOC2) == SOC2
    wr(chip, ADCST, 1 << 5)                               # INT_SEQ2_CLR
    run_to(chip, c0 + 29)
    assert not rd(chip, ADCST) & 1
    run_to(chip, c0 + 30)
    assert rd(chip, ADCST) & 1 and rd(chip, 0x0B00) == 1 and rd(chip, ADCST) & 0xC == 8
    run_to(chip, c0 + 45)
    assert rd(chip, ADCST) & 2 and rd(chip, 0x0B08) == 2 and not rd(chip, ADCTRL2) & (SOC1 | SOC2)


def test_adc_external_soc_from_the_xint2_pin(chip):
    adc = chip.peripherals["ADC"]
    adc_on(chip)
    wr(chip, ADCTRL1, CASC)
    wr(chip, 0x6FE1, 5)                                   # GPIOXINT2SEL = GPIO5
    chip.pins.set_external("GPIO5", "L")
    chip.pins.set_external("GPIO4", "L")
    wr(chip, ADCTRL2, EXT_SOC1 | INT_ENA1)
    chip.pins.set_external("GPIO4", "H")                  # not the XINT2 source
    chip.pins.set_external("GPIO5", "H")                  # rising edge: SOC
    chip.pins.set_external("GPIO5", "L")                  # falling: nothing
    chip.run_cycles(40)
    assert adc.conversions == 1 and rd(chip, ADCST) & 1
    wr(chip, ADCTRL2, INT_ENA1)                           # EXT_SOC_SEQ1 off
    chip.pins.set_external("GPIO5", "H")
    chip.run_cycles(40)
    assert adc.conversions == 1


# ------------------------------------------------------------------ misc
def test_csm_flash_spi_storage(chip):
    assert rd(chip, 0x0AEF) & 1 == 0                       # CSM unsecured
    assert rd(chip, 0x0A86) == 0x0F0F                      # FBANKWAIT reset
    wr(chip, 0x0A86, 0x0303)
    assert rd(chip, 0x0A86) == 0x0303
    wr(chip, 0x7041, 0x0006)                              # SPICTL: master, TALK
    wr(chip, 0x7040, 0x0097)                              # SPICCR: SPISWRESET, loopback, 8 bit
    wr(chip, 0x7048, 0x5500)
    chip.run_cycles(200)                                  # 8 bits x LSPCLK/4 (16 cycles each)
    assert rd(chip, 0x7042) & 0x40 and rd(chip, 0x7047) & 0xFF == 0x55
    assert rd(chip, 0x6800) == 0x0083                      # ePWM1 TBCTL reset value (storage)
    assert rd(chip, 0x7400) == 0                           # unmapped PF2 word → auto stub
