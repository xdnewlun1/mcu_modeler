"""STM32F4 FSMC (chipem/periph/stm32/fsmc.py) with the parallel SRAM device: reset values, NE1-NE4 windows opened by
MBKEN and the FSMC clock, faults while closed, mirroring, WREN, slow-path memories, the dynamic Bus windows, the
sample and the board."""
from pathlib import Path

import pytest

from chipem.bus import BusError
from chipem.devices.sram import ParallelSram
from chipem.session import Session
from test_stm32 import desc, rd, spin_chip, warns, wr  # noqa: F401  (desc: fixture)

ROOT = Path(__file__).resolve().parents[1]
FSMC, AHB3ENR, AHB3RSTR = 0xA0000000, 0x40023838, 0x40023818
NE = [0x60000000, 0x64000000, 0x68000000, 0x6C000000]


def _chip(desc, *mems, clock=True):
    chip = spin_chip(desc, clock_hz=16e6)
    for m in mems:
        chip.peripherals["FSMC"].attach_memory(m)
    if clock:
        wr(chip, AHB3ENR, 1)
    return chip


def test_reset_values(desc):
    chip = _chip(desc, clock=False)
    assert [rd(chip, FSMC + 8 * i) for i in range(4)] == [0x30DB, 0x30D2, 0x30D2, 0x30D2]
    assert [rd(chip, FSMC + 4 + 8 * i) for i in range(4)] == [0x0FFFFFFF] * 4
    assert [rd(chip, FSMC + 0x104 + 8 * i) for i in range(4)] == [0x0FFFFFFF] * 4
    assert rd(chip, FSMC + 0x60) == 0x18 and rd(chip, FSMC + 0x64) == 0x40 and rd(chip, FSMC + 0x68) == 0xFCFCFCFC


def test_windows_follow_mbken_and_clock(desc):
    m = ParallelSram("64K", 16)
    chip = _chip(desc, m, clock=False)
    with pytest.raises(BusError):
        rd(chip, NE[0])                                  # clock off: closed although BCR1.MBKEN resets to 1
    wr(chip, AHB3ENR, 1)
    wr(chip, NE[0] + 0x10, 0x12345678)                    # BCR1 reset value has WREN = 1
    assert m.mem[8] == 0x5678 and m.mem[9] == 0x1234
    with pytest.raises(BusError):
        rd(chip, NE[1])                                  # NE2: MBKEN = 0
    wr(chip, FSMC, rd(chip, FSMC) & ~1)
    with pytest.raises(BusError):
        rd(chip, NE[0] + 0x10)
    wr(chip, FSMC, rd(chip, FSMC) | 1)
    assert rd(chip, NE[0] + 0x10) == 0x12345678
    wr(chip, AHB3RSTR, 1)                                # RCC reset: BCR1 back to its reset value (still open)
    wr(chip, AHB3RSTR, 0)
    assert rd(chip, FSMC) == 0x30DB and rd(chip, NE[0] + 0x10) == 0x12345678


def test_mirror_and_second_bank(desc):
    a, b = ParallelSram("64K", 16), ParallelSram("1K", 8)
    chip = _chip(desc, a, b)
    wr(chip, FSMC + 8, 0x1001)                            # BCR2: MBKEN, MWID 8, WREN
    wr(chip, NE[0] + 4, 0xA5A55A5A)
    assert rd(chip, NE[0] + 0x20004) == 0xA5A55A5A and rd(chip, NE[0] + 0x3FE0004) == 0xA5A55A5A
    wr(chip, NE[1] + 3, 0x77)                             # 1 KB: mirrored every 0x400
    assert chip.bus.read(NE[1] + 0x403, 1) == 0x77 and b.mem[3] == 0x77
    assert not [w for w in warns(chip) if "MWID" in w]


def test_wren_and_width_warning(desc):
    m = ParallelSram("64K", 16)
    chip = _chip(desc, m)
    wr(chip, FSMC, 0x0001 | 0x10)                         # WREN = 0
    assert rd(chip, NE[0]) == 0
    region = chip.bus.lookup(NE[0])
    assert region is not None and not region.writable     # the core maps it read-only: writes bus-fault
    wr(chip, FSMC, 0x1001)                                # MWID 8 with a 16-bit memory
    assert any("MWID" in w for w in warns(chip))


def test_no_memory_and_slow_path(desc):
    chip = _chip(desc)
    assert rd(chip, NE[0]) == 0 and any("no external memory" in w for w in warns(chip))
    small = ParallelSram(100, 16)                          # 200 bytes: not a page multiple -> callbacks
    chip2 = _chip(desc, small)
    wr(chip2, NE[0] + 2, 0xCAFEBABE)
    assert small.mem[1] == 0xBABE and small.mem[2] == 0xCAFE and rd(chip2, NE[0] + 2) == 0xCAFEBABE
    chip2.bus.write(NE[0] + 200 + 2, 1, 0x11)              # wraps at 100 locations
    assert small.mem[1] == 0xBA11


def test_sample_and_board():
    fw = ROOT / "firmware" / "samples_stm32" / "build" / "fsmc_sram.elf"
    if not fw.exists():
        pytest.skip("fsmc_sram.elf not built")
    s = Session.from_board(ROOT / "boards" / "stm32_fsmc_sram.yaml")
    s.run_for(20e6)
    assert s.uart_output("A.USART1").decode() == ("FSMC BCR1 00001091\r\nSRAM ok\r\nMIRROR ok\r\nEXEC 42\r\n"
                                                 "FAULT ok\r\ndone\r\n")
    dev = s.ctxs["A"].devices[0]
    assert dev.mem[0x800] == 0x202A                         # the copied Thumb code at 0x60001000
