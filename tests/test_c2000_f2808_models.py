"""TMS320F2808 differences from the F2823x, on a fake C28x core (tests/c28x_fake.py):
  - SYSCTRL 280x layout: PLLSTS.CLKINDIV divider, PLL relock, limp mode, PCLKCR0/1 gates from the register table
    (SPI-B/C/D gates, no PCLKCR3), watchdog
  - XINT: only XINT1, XINT2, XNMI
  - CSM: password at 0x3F7FF8, protected L0/L1 + OTP + flash (H0 is not secure)
  - eCAN clock = SYSCLKOUT (no /2)
  - GPIO: port B is GPIO32-34, reset pull-ups (GPIO0-11 off), SPI-D / SCI-B / eCAN-B mux options
  - SPI-B/C/D instances (interrupt lines, gates, loopback transfer)"""
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip

from c28x_fake import make_fake_chip

CHIP = Path(__file__).resolve().parent.parent / "chips" / "tms320f2808.yaml"
EALLOW = 1 << 6
OSC = 20e6
LOCK_NS = 131072 / OSC * 1e9


def fresh():
    c = make_fake_chip(load_chip(CHIP))
    c.reset()
    return c


@pytest.fixture
def chip():
    c = fresh()
    c.core.regs["st1"] |= EALLOW
    c.bus.write(0x7029, 1, 0x0068)            # WDCR: watchdog off
    return c


def rd(c, a, n=1):
    return c.bus.read(a, n)


def wr(c, a, v, n=1):
    c.bus.write(a, n, v)


def warns(c):
    return [e.get("msg") or "" for e in c.log if e.kind == "warn"]


# ------------------------------------------------------------------ system control (SPRU712H ch. 3)
def test_reset_clock_and_clkindiv(chip):
    sc = chip.peripherals["SYSCTRL"]
    assert sc.clkindiv and chip.clock.hz == OSC / 2                 # PLL bypass, CLKINDIV = 0: OSCCLK / 2
    assert rd(chip, 0x7011) == 0x0001                                 # PLLSTS reset: PLLLOCKS = 1, CLKINDIV = 0
    assert rd(chip, 0x701A) == 1 and rd(chip, 0x701B) == 2            # HISPCP /2, LOSPCP /4
    assert sc.hspclk_hz() == 5e6 and sc.lspclk_hz() == 2.5e6
    wr(chip, 0x7011, 0x0003)                                          # CLKINDIV = 1: OSCCLK
    assert chip.clock.hz == OSC
    wr(chip, 0x7011, 0x0001)
    assert chip.clock.hz == OSC / 2
    assert rd(chip, 0x7010) == 0                                      # XCLK: XCLKOUTDIV = 00 (/4)


def test_pll_relock_and_100mhz(chip):
    wr(chip, 0x7021, 10)                                              # PLLCR.DIV = 10
    assert not rd(chip, 0x7011) & 1 and chip.clock.hz == OSC / 2      # relocking in bypass
    chip.run_for(LOCK_NS - 1000)
    assert not rd(chip, 0x7011) & 1
    chip.run_for(2000)
    assert rd(chip, 0x7011) & 1 and chip.clock.hz == 100e6           # 20 MHz x 10 / 2
    sc = chip.peripherals["SYSCTRL"]
    assert sc.lspclk_hz() == 25e6 and sc.hspclk_hz() == 50e6
    assert not warns(chip)
    wr(chip, 0x7011, 0x0003)                                          # CLKINDIV = 1 after lock: x10 / 1
    assert chip.clock.hz == 200e6
    wr(chip, 0x7021, 5)                                               # PLLCR with CLKINDIV = 1: TI says no
    assert any("CLKINDIV" in m for m in warns(chip))
    chip.core.regs["st1"] &= ~EALLOW
    wr(chip, 0x7021, 3)
    assert rd(chip, 0x7021) == 5                                      # EALLOW-protected


def test_clock_gates_from_the_280x_table(chip):
    sc = chip.peripherals["SYSCTRL"]
    for g in ("SPIBENCLK", "SPICENCLK", "SPIDENCLK", "ECANBENCLK", "ECAP4ENCLK", "EQEP2ENCLK"):
        assert not sc.clock_enabled(g), g
    wr(chip, 0x701C, (1 << 9) | (1 << 6) | (1 << 7))                  # PCLKCR0: SPI-B, SPI-C, SPI-D
    assert sc.clock_enabled("SPIBENCLK") and sc.clock_enabled("SPICENCLK") and sc.clock_enabled("SPIDENCLK")
    assert sc.clock_enabled("CPUTIMER0ENCLK") and sc.clock_enabled("GPIOINENCLK")   # no PCLKCR3 on the 280x
    assert "PCLKCR3" not in sc.regs and rd(chip, 0x7020) == 0         # reserved word (stub)
    chip.peripherals["SPIA"]._gate_warned = False
    rd(chip, 0x7040)
    wr(chip, 0x7041, 0)                                               # SPI-A clock still off -> one warn
    assert any("SPIAENCLK" in m for m in warns(chip))


def test_limp_mode_after_missing_clock(chip):
    sc = chip.peripherals["SYSCTRL"]
    sc.stop_oscillator()
    chip.run_for(100e3)                                               # detector fires, MCLKRES is taken by the Chip
    assert [e for e in chip.log if e.kind == "reset" and e.get("reason") == "missing_clock"]
    assert rd(chip, 0x7011) & (1 << 3)                                # MCLKSTS survives the MCLKRES
    assert chip.clock.hz == sc.limp_hz / 2                            # bypass, CLKINDIV = 0: limp / 2


def test_watchdog_runs_from_reset():
    c = fresh()
    c.run_for(6.4e6)                                                  # 20 MHz / 512 / 256 = 6.55 ms
    assert not [e for e in c.log if e.kind == "reset" and e.get("reason") == "watchdog"]
    c.run_for(0.3e6)
    assert [e for e in c.log if e.kind == "reset" and e.get("reason") == "watchdog"]


# ------------------------------------------------------------------ XINT (SPRS230P Table 9-30)
def test_only_xint1_xint2_xnmi(chip):
    x = chip.peripherals["XINT"]
    assert x.present == [0, 1, 2]
    wr(chip, 0x6FE0, 15)                                              # GPIOXINT1SEL = GPIO15
    wr(chip, 0x7070, 0x0005)                                          # XINT1CR: enable, rising
    wr(chip, 0x0CE2, 1 << 3)                                          # PIEIER1.4
    chip.pins.set_external("GPIO15", "L")
    chip.pins.set_external("GPIO15", "H")
    assert chip.arch.pie.ifr[1] & (1 << 3) and x.edges[1] == 1
    wr(chip, 0x6FE2, 16)                                              # XNMI source GPIO16
    wr(chip, 0x7077, 0x0001)                                          # XNMICR enable, falling
    chip.pins.set_external("GPIO16", "H")
    chip.pins.set_external("GPIO16", "L")
    assert x.edges[0] == 1


# ------------------------------------------------------------------ CSM (SPRU712H ch. 2)
def test_csm_password_location_and_secure_memory():
    c = fresh()
    csm = c.peripherals["CSM"]
    assert csm.pwl_base == 0x3F7FF8 and rd(c, 0x0AEF) & 1 == 0        # erased password: unsecure after the dummy read
    assert csm.in_secure(0x8000) and csm.in_secure(0x9FFF) and csm.in_secure(0x3D7800) and csm.in_secure(0x3E8000)
    assert not csm.in_secure(0xA000) and not csm.in_secure(0x3FA000)  # H0 is not in the secure zone
    pw = [0x1111 * (i + 1) for i in range(8)]
    for i, w in enumerate(pw):
        c.bus.write(0x3F7FF8 + i, 1, w)
    c.reset()                                                         # dummy read at reset: password != all ones
    assert csm.secure and rd(c, 0x0AEF) & 1
    c.core.regs["st1"] |= EALLOW
    for i, w in enumerate(pw):
        wr(c, 0x0AE0 + i, w)                                          # KEY0-7 = PWL0-7: unsecure
    assert not csm.secure


# ------------------------------------------------------------------ eCAN (SPRUEU0 3.1.2: module clock = SYSCLKOUT)
def test_ecan_bit_rate_uses_sysclkout(chip):
    ec = chip.peripherals["ECANB"]
    assert ec.clock_div == 1 and ec.clock_hz() == chip.clock.hz
    wr(chip, 0x701C, 1 << 15)                                         # ECANBENCLK
    wr(chip, 0x6214, 0x3000, 2)                                       # CANMC: SCB | CCR
    chip.run_for(100e3)
    assert rd(chip, 0x6218, 2) & 0x10                                 # CCE
    wr(chip, 0x6216, (1 << 16) | (6 << 3) | 1, 2)                     # BRPreg 1, TSEG1reg 6, TSEG2reg 1: 10 tq
    assert ec.bitrate() == pytest.approx(chip.clock.hz / 2 / 10)       # 10 MHz / 20 = 500 kbit/s


# ------------------------------------------------------------------ GPIO (Table 9-16, 9-17; SPRU712H Figures 4-15/16)
def test_gpio_ports_pullups_and_mux(chip):
    pins, g = chip.pins, chip.peripherals["GPIO"]
    assert pins.get("GPIO34").pull == "up" and pins.get("GPIO12").pull == "up" and pins.get("GPIO11").pull is None
    assert rd(chip, 0x6FC8, 2) & 0x7 == 0x7                           # GPIO32-34 read high through their pull-ups
    assert rd(chip, 0x6F9C, 2) == 0x18                                # GPBPUD: GPIO35 (internal) pull-up off
    wr(chip, 0x6F86, (2 << 2) | (2 << 6) | (2 << 10) | (2 << 14), 2)  # GPAMUX1: GPIO1/3/5/7 -> mux 2 (SPI-D)
    assert [pins.get(f"GPIO{i}").owner for i in (1, 3, 5, 7)] == ["SPISIMOD", "SPISOMID", "SPICLKD", "SPISTED"]
    wr(chip, 0x6F88, (2 << 4) | (2 << 6), 2)                          # GPAMUX2: GPIO18/19 -> SCITXDB / SCIRXDB
    tx = pins.get("GPIO18")
    assert tx.owner == "SCITXDB" and tx.direction == "out" and tx.level() == "H"
    wr(chip, 0x6F96, 1 << 4, 2)                                       # GPBMUX1 GPIO34 = 1: reserved (Table 9-17)
    assert g.pin_function("B", 2) == "reserved"
    assert g.pins["B"][3] is None                                     # GPIO35 is not pinned out


# ------------------------------------------------------------------ SPI-B/C/D (same IP as SPI-A)
@pytest.mark.parametrize("name,base,gate_bit,rx_vec", [("SPIB", 0x7740, 9, 74), ("SPIC", 0x7760, 6, 76),
                                                        ("SPID", 0x7780, 7, 78)])
def test_spi_instances(chip, name, base, gate_bit, rx_vec):
    spi = chip.peripherals[name]
    assert spi.rx_irq == rx_vec and spi.tx_irq == rx_vec + 1 and spi.gate == f"{name}ENCLK"
    wr(chip, 0x701C, 1 << gate_bit)
    wr(chip, base + 1, 0x0007)                                        # SPICTL: master, TALK, SPIINTENA
    wr(chip, base + 0, 0x0097)                                        # SPICCR: release reset, loopback, 8 bit
    wr(chip, base + 8, 0xA500)                                        # SPITXBUF (left-justified)
    chip.run_cycles(400)
    assert rd(chip, base + 2) & 0x40 and rd(chip, base + 7) & 0xFF == 0xA5
    assert chip.arch.pie.ifr[6] & (1 << (rx_vec - 72))                # SPIRXINTx in PIE group 6
    assert not [m for m in warns(chip) if name in m]
