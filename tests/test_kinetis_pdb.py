"""PDB0 model (software trigger, pre-triggers into the ADC hardware trigger, continuous mode, back-to-back, IDLY
interrupt, LDOK buffering) and the Withings battery measurement (PDB -> ADC0 -> DMA)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import ChipDescription, CoreDesc, MemoryDesc, PeripheralDesc

from engine_util import FLASH, RAM, build_image

PDB, ADC0, ADC1 = 0x40036000, 0x4003B000, 0x400BB000
DMA, DMAMUX = 0x40008000, 0x40021000
SC, MOD, CNT, IDLY, CH0C1, CH0S, CH0DLY0, CH0DLY1 = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C
A_SC1A, A_SC1B, A_SC2, A_RA, A_RB = 0x00, 0x04, 0x20, 0x10, 0x14
LDOK, CONT, PDBIE, PDBIF, PDBEN, SWTRG, SWTRIG = 1, 2, 1 << 5, 1 << 6, 1 << 7, 15 << 8, 1 << 16
ADTRG = 0x40
PDB_IRQ = 5
ROOT = Path(__file__).resolve().parent.parent

# core 1 MHz, no MCG -> bus 500 kHz -> one PDB count = 2 us (prescaler 1, MULT 1); ADC conversion 1 us
TICK_US = 2
CONV_US = 1


def pchip():
    desc = ChipDescription(
        name="PTEST", core=CoreDesc(arch="armv7e-m", cpu="cortex-m4", fpu=True, clock_hz=1e6, num_irqs=32),
        family="kinetis",
        memory=[MemoryDesc("FLASH", FLASH, 0x10000, "flash"), MemoryDesc("RAM", RAM, 0x10000, "ram")],
        peripherals=[PeripheralDesc("PDB0", "kinetis.pdb", PDB),
                     PeripheralDesc("ADC0", "kinetis.adc", ADC0, params={"index": 0, "conv_ns": CONV_US * 1000}),
                     PeripheralDesc("ADC1", "kinetis.adc", ADC1, params={"index": 1, "conv_ns": CONV_US * 1000}),
                     PeripheralDesc("DMA", "kinetis.dma", DMA, 0x2000),
                     PeripheralDesc("DMAMUX", "kinetis.dmamux", DMAMUX)],
        irqs={0: "DMA0", 3: "ADC0", 4: "ADC1", PDB_IRQ: "PDB0"}, pins=[])
    chip = Chip(desc)
    img, _ = build_image({"reset": "spin:\n  b spin\n"})
    chip.load_firmware(img)
    chip.reset()
    return chip


def wr(chip, addr, v):
    chip.bus.write(addr, 4, v)


def rd(chip, addr):
    return chip.bus.read(addr, 4)


def run_us(chip, us):
    chip.run_cycles(int(us))          # 1 MHz core: one cycle per microsecond


def setup_adc(chip, base=ADC0, adch=5, value=1234, sc1b=None):
    adc = chip.peripherals["ADC0" if base == ADC0 else "ADC1"]
    adc.set_channel(adch, value)
    wr(chip, base + 0x08, 0x0C)                           # CFG1.MODE = 16-bit
    wr(chip, base + A_SC2, ADTRG)
    wr(chip, base + A_SC1A, adch)
    if sc1b is not None:
        adc.set_channel(sc1b[0], sc1b[1])
        wr(chip, base + A_SC1B, sc1b[0])
    return adc


def program(chip, *, mod=99, dly0=10, dly1=0, idly=0xFFFF, c1=0x0101, cont=False, extra=0):
    base = PDBEN | SWTRG | (CONT if cont else 0) | extra
    wr(chip, PDB + SC, base)
    wr(chip, PDB + MOD, mod)
    wr(chip, PDB + IDLY, idly)
    wr(chip, PDB + CH0DLY0, dly0)
    wr(chip, PDB + CH0DLY1, dly1)
    wr(chip, PDB + CH0C1, c1)
    wr(chip, PDB + SC, base | LDOK)
    assert not rd(chip, PDB + SC) & LDOK                 # LDMOD=0: loaded immediately
    return base


def test_software_trigger_pretrigger_converts_at_delay():
    chip = pchip()
    adc = setup_adc(chip)
    base = program(chip, mod=99, dly0=10)
    assert adc.conversions == 0                           # SC1A write in hardware-trigger mode does not convert
    wr(chip, PDB + SC, base | SWTRIG)
    assert not rd(chip, PDB + SC) & SWTRIG               # SWTRIG reads 0
    run_us(chip, 10 * TICK_US - 1)
    assert 8 <= rd(chip, PDB + CNT) <= 10                 # live counter
    assert not adc.get("SC1A") & 0x80
    run_us(chip, 1 + CONV_US + 1)
    assert adc.get("SC1A") & 0x80 and adc.get("RA") == 1234
    assert rd(chip, PDB + CH0S) & (1 << 16)              # CF0
    assert adc.conversions == 1
    run_us(chip, 100 * TICK_US)                           # one-shot: stops after MOD, CNT back to 0
    assert adc.conversions == 1 and rd(chip, PDB + CNT) == 0
    assert rd(chip, ADC0 + A_RA) == 1234 and not adc.get("SC1A") & 0x80   # read clears COCO


def test_tos_clear_fires_at_trigger_and_continuous_period():
    chip = pchip()
    adc = setup_adc(chip)
    base = program(chip, mod=49, dly0=10, c1=0x0001, cont=True)   # EN0, TOS0=0: at every (re)start
    wr(chip, PDB + SC, base | SWTRIG)
    run_us(chip, CONV_US + 1)
    assert adc.conversions == 1
    rd(chip, ADC0 + A_RA)
    # period = (MOD+1) counts = 100 us; after 1000 us there were 10 more starts (t=100..1000)
    run_us(chip, 1000 - CONV_US - 1 + CONV_US + 1)
    assert adc.conversions == 11
    assert rd(chip, PDB + CH0S) & 1                       # results never read -> sequence error ERR0
    wr(chip, PDB + CH0S, 0)                               # write 0 clears
    assert rd(chip, PDB + CH0S) & 0xFF == 0
    wr(chip, PDB + SC, 0)                                 # PDBEN=0 stops the counter
    n = adc.conversions
    run_us(chip, 500)
    assert adc.conversions == n and rd(chip, PDB + CNT) == 0


def test_pdb_adc_dma_five_samples():
    """The Withings pattern: continuous PDB pre-trigger -> ADC0 (ADTRG, DMAEN) -> DMA0 copies RA to RAM."""
    chip = pchip()
    adc = setup_adc(chip, value=0x1234)
    wr(chip, ADC0 + A_SC2, ADTRG | 0x04)                  # + DMAEN
    dst = RAM + 0x100
    t = DMA + 0x1000
    wr(chip, t + 0x00, ADC0 + A_RA)                       # SADDR
    wr(chip, t + 0x04, 0x0101_0000)                       # SOFF 0, ATTR 16-bit -> 16-bit
    wr(chip, t + 0x08, 2)                                 # NBYTES
    wr(chip, t + 0x0C, 0)                                 # SLAST
    wr(chip, t + 0x10, dst)                               # DADDR
    wr(chip, t + 0x14, (5 << 16) | 2)                     # DOFF 2, CITER 5
    wr(chip, t + 0x18, (-10) & 0xFFFFFFFF)                # DLAST
    wr(chip, t + 0x1C, (5 << 16) | 0x0A)                  # CSR INTMAJOR|DREQ, BITER 5
    chip.bus.write(DMAMUX, 1, 0x80 | 40)                  # channel 0 <- ADC0
    chip.bus.write(DMA + 0x1B, 1, 0)                      # SERQ 0
    base = program(chip, mod=49, dly0=10, cont=True)
    wr(chip, PDB + SC, base | SWTRIG)
    run_us(chip, 4 * 100 + 50)                           # conversions at 21, 121, ..., 421 us
    assert chip.bus.read_bytes(dst, 10) == bytes.fromhex("3412") * 5
    assert chip.peripherals["DMA"].get("INT") & 1 and 16 + 0 in chip.arch.asserted
    assert not adc.get("SC1A") & 0x80                     # DMA read RA -> COCO cleared
    assert rd(chip, PDB + CH0S) & 0xFF == 0               # no sequence error


def test_back_to_back_sc1b_after_sc1a():
    chip = pchip()
    adc = setup_adc(chip, sc1b=(7, 999))
    done = []
    adc.on_complete.append(lambda a, k: done.append((k, round(chip.now_ns() / 1000))))
    # EN0|EN1, TOS0 (pre-trigger 0 at DLY0), BB1 (pre-trigger 1 when SC1A completes)
    base = program(chip, mod=99, dly0=5, dly1=50, c1=0x0002_0103)
    wr(chip, PDB + SC, base | SWTRIG)
    run_us(chip, 100 * TICK_US + 10)
    t_a = 5 * TICK_US + CONV_US
    assert done == [(0, t_a), (1, t_a + CONV_US)]         # BB pre-trigger ignores DLY1
    assert adc.get("RA") == 1234 and adc.get("RB") == 999 and adc.get("SC1B") & 0x80


def test_idly_interrupt_and_w0c():
    chip = pchip()
    base = program(chip, mod=99, idly=20, c1=0, extra=PDBIE)
    wr(chip, PDB + SC, base | SWTRIG)
    run_us(chip, 20 * TICK_US - 2)
    assert not rd(chip, PDB + SC) & PDBIF and 16 + PDB_IRQ not in chip.arch.asserted
    run_us(chip, 3)
    assert rd(chip, PDB + SC) & PDBIF
    assert 16 + PDB_IRQ in chip.arch.asserted
    wr(chip, PDB + SC, rd(chip, PDB + SC))                # writing 1 keeps it
    assert rd(chip, PDB + SC) & PDBIF
    wr(chip, PDB + SC, rd(chip, PDB + SC) & ~PDBIF)       # writing 0 clears it and drops the line
    assert not rd(chip, PDB + SC) & PDBIF
    assert 16 + PDB_IRQ not in chip.arch.asserted


def test_adtrg_clear_ignores_hardware_trigger():
    chip = pchip()
    adc = chip.peripherals["ADC0"]
    adc.set_channel(5, 1234)
    wr(chip, ADC0 + A_SC1A, 0x1F)                         # software mode, module disabled
    assert adc.hw_trigger(0) is False
    base = program(chip, mod=99, dly0=10)
    wr(chip, PDB + SC, base | SWTRIG)
    run_us(chip, 300)
    assert adc.conversions == 0 and not adc.get("SC1A") & 0x80


def test_buffered_mod_needs_ldok():
    chip = pchip()
    adc = setup_adc(chip)
    base = program(chip, mod=49, dly0=0, c1=0x0001, cont=True)
    wr(chip, PDB + MOD, 9)                                # buffered: no LDOK yet -> period stays 100 us
    assert rd(chip, PDB + MOD) == 9
    wr(chip, PDB + SC, base | SWTRIG)
    run_us(chip, 250)
    assert adc.conversions == 3                           # t = 0, 100, 200
    wr(chip, PDB + SC, base | LDOK)                       # now period = 10 counts = 20 us
    run_us(chip, 98)
    assert adc.conversions == 3 + 5


def test_unsupported_trigger_source_logged_once():
    chip = pchip()
    wr(chip, PDB + SC, PDBEN | (3 << 8))
    wr(chip, PDB + SC, PDBEN | (3 << 8) | LDOK)
    wr(chip, PDB + SC, PDBEN | (3 << 8) | LDOK)
    msgs = [e for e in chip.log if e.kind == "info" and "TRGSEL" in str(e.fields.get("msg"))]
    assert len(msgs) == 1


def test_withings_battery_measurement():
    """Real image: PDB0 (software trigger, continuous, MOD 3999, delay 1999) hardware-triggers ADC0 SE18, DMA0 moves
    5 results to RAM, and the firmware prints the battery voltage (mV = counts * 0.141)."""
    fw = ROOT / "s1_off000084_503080.bin"
    if not fw.exists():
        pytest.skip("Withings image not present")
    from chipem.session import Session
    s = Session(ROOT / "chips" / "mk22fx512vll12.yaml", fw, quiet=True)
    s.attach_spi_flash("SPI0:cs=PTC4")
    s.apply_stimulus("PTD2=1")
    s.set_adc("ADC0_SE18", 40000)
    s.run_for(0.1e9)
    out = s.uart_output("UART2").decode("latin-1")
    m = re.search(r"\[BAT\] (\d+) mv \((\d+) %\).*State <(\w+)>", out)
    assert m, out
    assert int(m.group(1)) == 5640 and int(m.group(2)) > 0 and m.group(3) == "OK"
