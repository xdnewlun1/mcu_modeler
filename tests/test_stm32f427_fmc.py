"""STM32F42x/43x FMC (chipem/periph/stm32/fmc.py) with the SDRAM controller (RM0090 37.7) and the board SDRAM device
(chipem/devices/sdram.py): register reset values (incl. the BWTR3/4 SVD offset fix), the window gating by the FMC
clock and the JEDEC init sequence, SDSR.BUSY durations, MODES, refresh / retention, RE + IRQ, write protection, pin
mux checks, geometry mismatch, reset, Stop mode, the NOR/SRAM bank 1 inherited from stm32.fsmc, the sample and the
board."""
from pathlib import Path

import pytest

from chipem.board import load_board
from chipem.bus import BusError
from chipem.chipdesc import load_chip
from chipem.devices.sdram import Sdram
from chipem.devices.sram import ParallelSram
from chipem.session import Session
from test_stm32 import rd, spin_chip, warns, wr

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "boards" / "stm32f437ai_sdram.yaml"
ELF = ROOT / "firmware" / "samples_stm32_variants" / "build" / "f42x_sdram.elf"
FMC, AHB1ENR, AHB3ENR, AHB3RSTR = 0xA0000000, 0x40023830, 0x40023838, 0x40023818
SDCR1, SDCR2, SDTR1, SDTR2, SDCMR, SDRTR, SDSR = (FMC + o for o in (0x140, 0x144, 0x148, 0x14C, 0x150, 0x154, 0x158))
BANK1, BANK2 = 0xC0000000, 0xD0000000
GPIOB, GPIOG, GPIOH = 0x40020400, 0x40021800, 0x40021C00
# IS42S16400J on bank 2: NC 8 bits, NR 12 bits, MWID 16, 4 banks, CAS 3, WP 0
SDCR_8MB = (0 << 0) | (1 << 2) | (1 << 4) | (1 << 6) | (3 << 7)
SDTR = (1 << 0) | (6 << 4) | (3 << 8) | (6 << 12) | (1 << 16) | (1 << 20) | (1 << 24)   # TRC 7, TRP 2, TMRD 2
MRD_CAS3 = 0x230


@pytest.fixture(scope="module")
def desc():
    return load_chip(ROOT / "chips" / "stm32f437ai.yaml")


def _af12(chip, port_base, pin):
    wr(chip, port_base, (rd(chip, port_base) & ~(3 << 2 * pin)) | (2 << 2 * pin))
    reg = port_base + (0x20 if pin < 8 else 0x24)
    s = 4 * (pin & 7)
    wr(chip, reg, (rd(chip, reg) & ~(0xF << s)) | (12 << s))


def _chip(desc, dev=None, bank=2, pins=True, clock=True):
    chip = spin_chip(desc, clock_hz=180e6)
    f = chip.peripherals["FMC"]
    if dev is not None:
        f.attach_sdram(dev, bank)
    if pins:
        wr(chip, AHB1ENR, rd(chip, AHB1ENR) | 0xC2)              # GPIOB, GPIOG, GPIOH
        _af12(chip, GPIOG, 8)                                     # FMC_SDCLK
        _af12(chip, GPIOB, 5)                                     # FMC_SDCKE1
        _af12(chip, GPIOB, 6)                                     # FMC_SDNE1
        _af12(chip, GPIOH, 2)                                     # FMC_SDCKE0
        _af12(chip, GPIOH, 3)                                     # FMC_SDNE0
    if clock:
        wr(chip, AHB3ENR, 1)
    return chip


def _cmd(chip, mode, ctb=0x08, nrfs=1, mrd=0):
    wr(chip, SDCMR, (mrd << 9) | ((nrfs - 1) << 5) | ctb | mode)
    chip.run_for(2e3)                                             # longer than any command (BUSY)


def _init(chip, sdcr=SDCR_8MB, bank=2, wait_us=100.0, mrd=MRD_CAS3, count=1386):
    ctb = 0x10 if bank == 1 else 0x08
    wr(chip, SDCR1, (rd(chip, SDCR1) & ~0x7C00) | (2 << 10))     # SDCLK = HCLK/2
    wr(chip, SDCR1 if bank == 1 else SDCR2, sdcr | (rd(chip, SDCR1) & 0x7C00 if bank == 1 else 0))
    wr(chip, SDTR1, SDTR)
    wr(chip, SDTR2, SDTR)
    _cmd(chip, 1, ctb)
    chip.run_for(wait_us * 1e3)
    _cmd(chip, 2, ctb)
    _cmd(chip, 3, ctb, nrfs=8)
    _cmd(chip, 4, ctb, mrd=mrd)
    if count:
        wr(chip, SDRTR, count << 1)


def test_reset_values_and_bwtr_offsets(desc):
    chip = _chip(desc, pins=False)
    assert rd(chip, SDCR1) == 0x2D0 and rd(chip, SDCR2) == 0x2D0
    assert rd(chip, SDTR1) == 0x0FFFFFFF and rd(chip, SDTR2) == 0x0FFFFFFF
    assert rd(chip, SDCMR) == 0 and rd(chip, SDRTR) == 0 and rd(chip, SDSR) == 0
    assert rd(chip, FMC) == 0x30DB and rd(chip, FMC + 8) == 0x30D2
    f = chip.peripherals["FMC"]
    # STM32F427.svd overlaps BWTR3/4 with BWTR1/2; RM0090 37.5.6: 0x104 + 8 * (x - 1)
    assert [f.regs[f"BWTR{x}"].offset for x in range(1, 5)] == [0x104, 0x10C, 0x114, 0x11C]
    wr(chip, FMC + 0x114, 0x0FFF1234)
    assert rd(chip, FMC + 0x114) == 0x0FFF1234 and rd(chip, FMC + 0x104) == 0x0FFFFFFF
    wr(chip, SDCR2, 0xFFFFFFFF)                                   # RPIPE / SDCLK are read-only in SDCR2
    assert rd(chip, SDCR2) & 0x6C00 == 0 and rd(chip, SDCR2) >> 15 == 0


def test_window_gating_and_floating_bus(desc):
    dev = Sdram(4, 4096, 256, 16)
    chip = _chip(desc, dev, clock=False)
    with pytest.raises(BusError):
        rd(chip, BANK2)                                           # FMC clock off: no window
    wr(chip, AHB3ENR, 1)
    assert rd(chip, BANK2) == 0                                   # SDRAM not initialized: floating data bus
    wr(chip, BANK2, 0x12345678)
    assert dev.mem[0] == 0 and any("SDRAM clock disabled" in w and "floats" in w for w in warns(chip))
    # reset SDCR2 = 0x2D0: 8 cols x 11 rows x 4 banks x 16 bit = 4 MB; above it is a reserved range (AHB error)
    assert rd(chip, BANK2 + 0x3FFFFC) == 0
    with pytest.raises(BusError):
        rd(chip, BANK2 + 0x400000)


def test_init_sequence_busy_and_data(desc):
    dev = Sdram(4, 4096, 256, 16)
    chip = _chip(desc, dev)
    f = chip.peripherals["FMC"]
    wr(chip, SDCMR, 0x08 | 1)                                     # SDCLK still disabled (SDCR1 reset value)
    assert dev.state == "off" and any("SDCLK" in w for w in warns(chip))
    wr(chip, SDCR1, (rd(chip, SDCR1) & ~0x7C00) | (2 << 10))
    wr(chip, SDCR2, SDCR_8MB)
    wr(chip, SDTR1, SDTR)
    wr(chip, SDCMR, 0x08 | 1)                                     # clock configuration enable
    assert dev.cke and dev.state == "powerup"
    chip.run_for(1e3)
    _cmd(chip, 2)                                                 # PALL 2 us after CKE: power-up delay not met
    assert dev.state == "powerup" and any("power-up delay" in w for w in warns(chip))
    chip.run_for(100e3)
    wr(chip, SDCMR, 0x08 | 2 | (7 << 5))
    assert dev.state == "precharged"
    chip.run_for(1e3)
    wr(chip, SDCMR, 0x08 | 3 | (7 << 5))                          # 8 auto-refresh x TRC 7 = 56 SDCLK = 622 ns
    assert rd(chip, SDSR) & 0x20
    wr(chip, SDCMR, 0x08 | 4 | (MRD_CAS3 << 9))                   # written while BUSY: ignored
    assert dev.mode is None and any("BUSY = 1" in w for w in warns(chip))
    chip.run_for(0.55e3)
    assert rd(chip, SDSR) & 0x20                                  # still busy at 550 ns
    chip.run_for(0.1e3)
    assert not rd(chip, SDSR) & 0x20                              # done after 622 ns
    assert dev.state == "refreshed" and dev.refreshes == 8
    _cmd(chip, 4, mrd=MRD_CAS3)
    assert dev.ready and dev.cas_latency == 3 and dev.burst_length == 1
    assert f.sd_open[2][0] == "fast"                              # geometry matches: mapped straight into the core
    wr(chip, BANK2 + 0x7FFFFC, 0xCAFEF00D)
    assert rd(chip, BANK2 + 0x7FFFFC) == 0xCAFEF00D and dev.mem[0x3FFFFE] == 0xF00D
    assert chip.bus.read(BANK2 + 0x10, 1) == 0 and [c[1] for c in f.commands][-1] == "load mode register"
    assert not [w for w in warns(chip) if "CAS" in w or "burst" in w]


def test_mode_register_mismatch_and_geometry(desc):
    dev = Sdram(4, 4096, 256, 16)
    chip = _chip(desc, dev)
    _init(chip, mrd=0x221)                                        # CAS 2, burst length 2
    ws = warns(chip)
    assert any("CAS latency 2 differs from FMC_SDCR2.CAS = 3" in w for w in ws)
    assert any("burst length 2" in w for w in ws)
    small = Sdram(2, 2048, 256, 16)                               # 2 MB device behind an 8 MB configuration
    chip = _chip(desc, small)
    _init(chip)
    f = chip.peripherals["FMC"]
    assert f.sd_open[2][0] == "slow" and any("address lines mapped modulo" in w for w in warns(chip))
    wr(chip, BANK2 + 0x200, 0x11223344)                           # bank 0, row 1, col 0
    loc = (0 * 2048 + 1) * 256
    assert small.mem[loc] == 0x3344 and small.mem[loc + 1] == 0x1122
    # internal bank 2 row 2049: device has 2 banks / 2048 rows -> bank 0 row 1 (upper lines not connected)
    assert rd(chip, BANK2 + ((2 * 4096 + 2049) * 256) * 2) == 0x11223344


def test_pins_must_be_muxed(desc):
    dev = Sdram()
    chip = _chip(desc, dev, pins=False)
    _init(chip)
    assert not dev.cke and dev.state == "off"
    assert any("FMC_SDCLK" in w and "not muxed" in w for w in warns(chip))
    assert rd(chip, BANK2) == 0                                   # the memory never saw the init: bus floats


def test_package_without_sdram_pins():
    vg = load_chip(ROOT / "chips" / "stm32f427vg.yaml")           # LQFP100: no port G, no FMC_SDCLK
    chip = spin_chip(vg, clock_hz=180e6)
    chip.peripherals["FMC"].attach_sdram(Sdram(), 1)
    wr(chip, AHB3ENR, 1)
    wr(chip, SDCR1, (rd(chip, SDCR1) & ~0x7C00) | (2 << 10))
    wr(chip, SDCMR, 0x10 | 1)
    assert any("LQFP100 package has no" in w and "FMC_SDCLK" in w for w in warns(chip))


def test_refresh_keeps_data_and_missing_refresh_loses_it(desc):
    dev = Sdram()
    chip = _chip(desc, dev)
    _init(chip)                                                   # COUNT 1386 at 90 MHz: 15.6 us x 4096 = 63.9 ms
    assert dev.refreshed_ok()
    wr(chip, BANK2 + 0x100, 0xA5A5A5A5)
    chip.run_for(70e6)
    assert rd(chip, BANK2 + 0x100) == 0xA5A5A5A5 and dev.losses == 0
    dev2 = Sdram()
    chip = _chip(desc, dev2)
    _init(chip, count=0)                                          # refresh timer never programmed
    wr(chip, BANK2 + 0x100, 0xA5A5A5A5)
    chip.run_for(60e6)
    assert rd(chip, BANK2 + 0x100) == 0xA5A5A5A5                  # still within the 64 ms retention
    chip.run_for(10e6)
    assert rd(chip, BANK2 + 0x100) == 0 and dev2.losses == 1
    assert any("data lost" in w for w in warns(chip))
    dev3 = Sdram()
    chip = _chip(desc, dev3)
    _init(chip, count=8000)                                       # 88.9 us x 4096 rows = 364 ms > 64 ms
    assert not dev3.refreshed_ok()


def test_refresh_error_flag_and_irq(desc):
    chip = _chip(desc, Sdram())
    _init(chip, count=0)
    wr(chip, SDRTR, (1 << 14) | (5 << 1))                         # COUNT 5 < TRP 2 + TRC 7; REIE
    assert any("< 41" in w for w in warns(chip))
    chip.run_for(1e3)
    assert rd(chip, SDSR) & 1 and (16 + 48) in chip.arch.asserted   # FMC IRQ 48
    wr(chip, SDRTR, (1 << 14) | (5 << 1) | 1)                     # CRE
    assert not rd(chip, SDSR) & 1 and (16 + 48) not in chip.arch.asserted
    assert rd(chip, SDRTR) & 1 == 0                               # CRE reads 0


def test_write_protection(desc):
    dev = Sdram()
    chip = _chip(desc, dev)
    _init(chip, sdcr=SDCR_8MB | (1 << 9))                         # WP
    region = chip.bus.lookup(BANK2)
    assert region is not None and not region.writable             # the core maps it read-only: writes fault
    dev.mem[4] = 0xBEEF
    assert chip.bus.read(BANK2 + 8, 2) == 0xBEEF


def test_self_refresh_power_down_and_exit_on_access(desc):
    dev = Sdram()
    chip = _chip(desc, dev)
    _init(chip, count=0)
    wr(chip, BANK2, 0x55AA55AA)
    _cmd(chip, 5)                                                 # self-refresh
    assert (rd(chip, SDSR) >> 3) & 3 == 1 and dev.in_self_refresh and dev.refreshed_ok()
    chip.run_for(80e6)                                            # no controller refresh, but self-refresh
    assert dev.losses == 0
    assert rd(chip, BANK2) == 0x55AA55AA                          # access exits self-refresh (37.7.4)
    chip.run_for(1e3)
    assert (rd(chip, SDSR) >> 3) & 3 == 0 and not dev.in_self_refresh
    _cmd(chip, 6)                                                 # power-down
    assert (rd(chip, SDSR) >> 3) & 3 == 2
    _cmd(chip, 0)                                                 # normal mode command exits it
    assert (rd(chip, SDSR) >> 3) & 3 == 0
    _cmd(chip, 7)
    assert any("MODE = 111 is reserved" in w for w in warns(chip))


def test_bank1_and_stop_mode(desc):
    dev = Sdram()
    chip = _chip(desc, dev, bank=1)
    _init(chip, bank=1)
    wr(chip, BANK1 + 0x40, 0x01020304)
    assert dev.mem[0x20] == 0x0304
    f = chip.peripherals["FMC"]
    f.clock_stop(True, "stop")                                   # HCLK stopped: no refresh
    assert not dev.refreshed_ok()
    f.clock_stop(False, "stop")
    assert dev.refreshed_ok()


def test_chip_reset_needs_reinit_but_keeps_data(desc):
    dev = Sdram()
    chip = _chip(desc, dev)
    _init(chip)
    wr(chip, BANK2 + 0x1000, 0x600DDA7A)
    chip.reset()
    assert not dev.cke and dev.state == "off"
    chip.run_for(1e6)
    wr(chip, AHB1ENR, 0xC2)
    for port, pin in ((GPIOG, 8), (GPIOB, 5), (GPIOB, 6)):
        _af12(chip, port, pin)
    wr(chip, AHB3ENR, 1)
    assert rd(chip, BANK2 + 0x1000) == 0                          # not initialized after the reset: bus floats
    _init(chip)
    assert rd(chip, BANK2 + 0x1000) == 0x600DDA7A                 # re-initialized within the retention time
    wr(chip, AHB3RSTR, 1)                                         # RCC reset of the FMC: also needs a new init
    wr(chip, AHB3RSTR, 0)
    assert not dev.ready


def test_nor_sram_bank1_inherited(desc):
    chip = _chip(desc, pins=False)
    m = ParallelSram("64K", 16)
    chip.peripherals["FMC"].attach_memory(m)
    wr(chip, 0x60000010, 0x12345678)
    assert m.mem[8] == 0x5678 and rd(chip, 0x60000010) == 0x12345678


def test_board_device_type():
    bd = load_board(BOARD)
    d = bd.devices[0]
    assert (d.type, d.chip, d.options["bus"], d.options["bank"]) == ("sdram", "A", "FMC", 2)


@pytest.mark.skipif(not ELF.exists(), reason="firmware/samples_stm32_variants/build.sh not run")
def test_sample_board_end_to_end():
    s = Session.from_board(BOARD)
    s.run_for(20e6)
    chip = next(iter(s.ctxs.values())).chip
    out = bytes(chip.peripherals["USART1"].output)
    assert out == b"SDRAM OK 8192 KB\r\n"
    f = chip.peripherals["FMC"]
    t = {c[1]: c[0] for c in f.commands}
    assert [c[1] for c in f.commands] == ["clock configuration enable", "PALL", "auto-refresh", "load mode register"]
    assert all(c[2] == (2,) for c in f.commands)
    assert t["PALL"] - t["clock configuration enable"] >= 100e3   # power-up delay (RM0090 37.7.3 step 4)
    assert t["PALL"] - t["clock configuration enable"] < 110e3
    assert (rd(chip, SDRTR) >> 1) & 0x1FFF == 1386                # 64 ms / 4096 rows x 90 MHz - 20
    assert f._sdclk_ns() == pytest.approx(1e9 / 90e6)            # SDCLK = HCLK / 2 at 180 MHz
    dev = next(d for d in next(iter(s.ctxs.values())).devices if isinstance(d, Sdram))
    assert dev.ready and dev.refreshed_ok() and dev.writes == 0   # fast path: the core wrote the buffer directly
    assert not [e for e in chip.log if e.kind in ("fault",)]
    assert not [w for w in warns(chip) if "SDRAM" in w]
