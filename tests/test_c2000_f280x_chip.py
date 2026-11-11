"""TMS320F280x / F2801x family descriptions (chips/tms320f2809.yaml, f2806, f2802, f2801, f28016, f28015 including
chips/tms320f2808.yaml): per-device differences from SPRS230P Tables 6-1 / 6-2 (device comparison), Figures 9-18..9-22
and Tables 9-18..9-21 (memory maps, flash sectors), Table 9-27 (PARTID), the clock / ADC-clock limits, and the samples
running on the smaller parts.  Values are copied by eye from the data sheet."""
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
SAMPLES = ROOT / "firmware" / "samples_c2000"

# part: (MHz, flash base, flash words, L SARAM words, PARTID, ePWMs, HRPWM ePWMs, eCAPs, eQEPs, SPI, SCI, eCAN,
#        ADCCLK max MHz)
PARTS = {
    "F2809": (100, 0x3D8000, 0x20000, 0x4000, 0x00FE, 6, 6, 4, 2, "ABCD", "AB", "AB", 25),
    "F2808": (100, 0x3E8000, 0x10000, 0x4000, 0x003C, 6, 4, 4, 2, "ABCD", "AB", "AB", 12.5),
    "F2806": (100, 0x3F0000, 0x08000, 0x2000, 0x0034, 6, 4, 4, 2, "ABCD", "AB", "A", 12.5),
    "F2802": (100, 0x3F0000, 0x08000, 0x1000, 0x0024, 3, 3, 2, 1, "AB", "A", "A", 12.5),
    "F2801": (100, 0x3F4000, 0x04000, 0x1000, 0x002C, 3, 3, 2, 1, "AB", "A", "A", 12.5),
    "F28016": (60, 0x3F4000, 0x04000, 0x1000, 0x0014, 4, 4, 2, 0, "A", "A", "A", 7.5),
    "F28015": (60, 0x3F4000, 0x04000, 0x1000, 0x001C, 4, 4, 2, 0, "A", "A", "", 7.5),
}


def _chip(part: str):
    from chipem.chip import Chip
    from chipem.log import EventLog
    log = EventLog()
    ev: list = []
    log.subscribe(ev.append)
    c = Chip(load_chip(CHIPS / f"tms320{part.lower()}.yaml"), log=log)
    c.reset()
    return c, ev


@pytest.mark.parametrize("part", PARTS)
def test_device_differences(part):
    mhz, fb, fs, lram, partid, npwm, nhr, necap, nqep, spis, scis, cans, adc = PARTS[part]
    d = load_chip(CHIPS / f"tms320{part.lower()}.yaml")
    assert d.name == f"TMS320{part}" and d.core.fpu is False
    assert d.core.clock_hz == (10e6 if part == "F2808" else mhz * 1e6)     # F2808: the reset SYSCLKOUT
    assert len(d.pins) == 100 and d.package == "LQFP100 (PZ)"
    mem = {m.name: m for m in d.memory}
    assert (mem["FLASH"].base, mem["FLASH"].size) == (fb, fs) and fb + fs == 0x3F8000
    assert (mem["M0"].size, mem["M1"].size) == (0x400, 0x400)
    lr = [m for m in d.memory if m.base == 0x8000]
    assert len(lr) == 1 and lr[0].size == lram
    alias = [p for p in d.peripherals if p.model == "ti_c2000.alias"]
    assert len(alias) == 1 and alias[0].base == 0x3F8000 and alias[0].size == lram     # program-space mirror
    names = {p.name for p in d.peripherals}
    assert {f"EPWM{i}" for i in range(1, 7)} & names == {f"EPWM{i}" for i in range(1, npwm + 1)}
    hr = [p.name for p in d.peripherals if p.model == "ti_c2000.epwm" and p.params.get("hrpwm")]
    assert hr == [f"EPWM{i}" for i in range(1, nhr + 1)]
    assert {f"ECAP{i}" for i in range(1, 5)} & names == {f"ECAP{i}" for i in range(1, necap + 1)}
    assert {"EQEP1", "EQEP2"} & names == {f"EQEP{i}" for i in range(1, nqep + 1)}
    assert {f"SPI{x}" for x in "ABCD"} & names == {f"SPI{x}" for x in spis}
    assert {"SCIA", "SCIB"} & names == {f"SCI{x}" for x in scis}
    assert {"ECANA", "ECANB"} & names == {f"ECAN{x}" for x in cans}
    assert {"I2CA", "ADC", "GPIO", "SYSCTRL", "XINT", "CPUTIMER0", "PIE"} <= names
    adcp = next(p for p in d.peripherals if p.name == "ADC").params
    assert adcp["adcclk_max_hz"] == adc * 1e6
    irqs = set(d.irqs.values())
    assert ("EPWM4_INT" in irqs) is (npwm >= 4) and ("ECAN0INTB" in irqs) is ("B" in cans)
    assert ("SPIRXINTD" in irqs) is ("D" in spis) and ("EQEP1_INT" in irqs) is (nqep >= 1)
    c, ev = _chip(part)
    assert not [e for e in ev if e.kind in ("warn", "fault")]
    assert c.bus.read(0x0882, 1) == partid                                  # PARTID (SPRS230P Table 9-27)
    assert c.peripherals["SYSCTRL"].max_hz == mhz * 1e6
    c.bus.write(0x8010, 1, 0x1234)                                          # L0 and its program-space mirror
    assert c.bus.read(0x3F8010, 1) == 0x1234


def test_f2809_revid_and_csm_ranges():
    c, _ = _chip("F2809")
    assert c.bus.read(0x0883, 1) == 0x0001                                  # F2809 silicon rev. A (Table 9-27)
    csm = c.peripherals["CSM"]
    assert (0x3D8000, 0x3F8000) in csm.secure_ranges and csm.pwl_base == 0x3F7FF8


def test_pin_tables_clean():
    import subprocess
    import sys
    for part in PARTS:
        r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "pin_table_check.py"),
                            str(CHIPS / f"tms320{part.lower()}.yaml"), "--expect", "100", "--strict"],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0 and "0 error(s), 0 warning(s)" in r.stdout, (part, r.stdout[-800:])


def _blinky(tmp_path: Path, pllcr: int) -> Path:
    from chipem.core.c28x.asm import assemble
    src = SAMPLES / "f2808_blinky.asm"
    text = src.read_text().replace("MOV     @PLLCR, #10", f"MOV     @PLLCR, #{pllcr}")
    text = text.replace('.sect "text", 0x3E8000', '.sect "text", 0x3F4000')     # 16K flash: sectors D..A
    assert f"#{pllcr}" in text
    out = tmp_path / f"blinky{pllcr}.c28img"
    assemble(text, filename=str(src)).write_c28img(out)
    return out


@pytest.mark.parametrize("part,pllcr,mhz,warn", [("F28016", 6, 60, False), ("F28015", 10, 100, True),
                                                 ("F2801", 10, 100, False)])
def test_blinky_on_small_parts_and_clock_limit(tmp_path, part, pllcr, mhz, warn):
    """The F2808 blinky (GPIO34, CPU timer 0 at 100000 counts) on the 16K-flash parts: 1 ms at 100 MHz, 1.667 ms at
    60 MHz (20 MHz x 6 / 2).  PLLCR = 10 on a 60-MHz part runs, with one `warn` (SPRS230P: 60-MHz devices)."""
    from chipem.session import Session
    s = Session(str(CHIPS / f"tms320{part.lower()}.yaml"), _blinky(tmp_path, pllcr), quiet=True)
    s.run_for(12e6)
    assert s.chip.clock.hz == mhz * 1e6
    ev = [e.t_ns for e in s.events(kinds={"pin"}) if e.get("pin") == "GPIO34" and e.get("field") == "level"]
    gaps = [b - a for a, b in zip(ev[1:], ev[2:])]
    assert gaps and all(abs(g - 100000 / (mhz * 1e6) * 1e9) < 1e3 for g in gaps), gaps
    w = [e.get("msg") for e in s.events(kinds={"warn"})]
    assert any("exceeds the 60.000 MHz maximum" in m for m in w) is warn, w


def test_adcclk_limit_warning(tmp_path):
    """f2808_adc runs ADCCLK within 12.5 MHz on the F2808; the same image on a 60-MHz F28016 (whose limit is
    7.5 MHz) gets one ADC `warn` and still converts."""
    from chipem.core.c28x.asm import assemble
    from chipem.session import Session
    src = SAMPLES / "f2808_adc.asm"
    img = tmp_path / "adc.c28img"
    text = src.read_text().replace('.sect "text", 0x3E8000', '.sect "text", 0x3F4000')     # fits every 280x
    assemble(text, filename=str(src)).write_c28img(img)
    res = {}
    for part in ("F2808", "F28016"):
        s = Session(str(CHIPS / f"tms320{part.lower()}.yaml"), img, quiet=True)
        s.run_for(13e6)                                       # PLL lock, 5 ms reference settling, samples
        res[part] = ([e.get("msg") for e in s.events(kinds={"warn"}) if e.get("block") == "ADC"],
                     s.chip.peripherals["ADC"].conversions)
    assert res["F2808"][0] == [] and res["F2808"][1] > 0
    assert len(res["F28016"][0]) == 1 and "ADCCLK" in res["F28016"][0][0] and res["F28016"][1] > 0


def test_absent_block_reads_as_stub():
    """SPI-B..D / SCI-B / eCAN on the F28015 are not instantiated: their frames are plain catch-all storage."""
    c, ev = _chip("F28015")
    assert "ECANA" not in c.peripherals and "SPIB" not in c.peripherals
    assert c.find_uart("SCIB") is None and c.find_uart("SCIA") is not None


def test_peripheral_input_with_two_pins_defaults_high():
    """SPRU712H 4.2 / Table 4-8: with ECAP1 muxed to both GPIO5 (GPAMUX1[11:10] = 11) and GPIO24 (GPAMUX2[17:16] =
    01) the eCAP input is the default 1 and edges on either pin are not seen; with one pin it follows that pin.  The
    eQEP inputs follow the same rule (EQEP1A on GPIO20 alone vs. GPIO20 + a second EQEP1A pin is not possible on the
    280x mux, so only the default with no pin muxed is checked: QEPSTS / the raw A input reads 1)."""
    from chipem.chip import Chip
    from chipem.core.c28x.asm import assemble
    c = Chip(load_chip(CHIPS / "tms320f2808.yaml"))
    c.load_firmware(assemble('''
        .sect "boot", 0x3F7FF6
        LB start
        .sect "code", 0x8000
start:  SB start,UNC
''').image())
    c.reset()
    c.core.write_reg("st1", c.core.read_reg("st1") | (1 << 6))           # EALLOW
    c.bus.write(0x7029, 1, 0x0068)                                       # watchdog off
    c.bus.write(0x701D, 1, 0x0100 | 0x4000)                              # PCLKCR1: ECAP1ENCLK, EQEP1ENCLK
    c.pins.set_external("GPIO24", "L")
    c.pins.set_external("GPIO5", "L")
    c.bus.write(0x6F89, 1, 0x0001)                                       # GPAMUX2 hi: GPIO24 -> ECAP1
    cap = c.peripherals["ECAP1"]
    c.bus.write(0x6A14, 1, 0x0100)                                       # ECCTL1: CAPLDEN, rising edges
    c.bus.write(0x6A15, 1, 0x0016)                                       # ECCTL2: TSCTRSTOP, continuous, wrap 4
    c.run_cycles(50)
    c.pins.set_external("GPIO24", "H")
    c.run_cycles(50)
    assert cap.captures == 1                                             # one pin: its edge is captured
    c.pins.set_external("GPIO24", "L")
    c.bus.write(0x6F86, 1, 0x0C00)                                       # GPAMUX1 lo: GPIO5 -> ECAP1 as well
    n = cap.captures                                                     # (input switches to the default 1)
    for lvl in ("H", "L", "H"):
        c.run_cycles(20)
        c.pins.set_external("GPIO24", lvl)
        c.pins.set_external("GPIO5", lvl)
    assert cap.captures == n and cap._input() == "H"
    c.bus.write(0x6F86, 1, 0x0000)                                       # GPIO5 back to GPIO: GPIO24 alone (H)
    c.pins.set_external("GPIO24", "L")
    c.run_cycles(20)
    c.pins.set_external("GPIO24", "H")
    assert cap.captures == n + 1
    q = c.peripherals["EQEP1"]
    assert q._sig_level("A") == 1 and q._sig_level("I") == 1             # no pin on EQEP1A / EQEP1I: default 1
