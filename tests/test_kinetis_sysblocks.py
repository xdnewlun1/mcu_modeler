"""Kinetis K22 system blocks (chipem/periph/kinetis/sysblocks.py, flexbus.py): MCM (FPU flags + IRQ, SRAM write
protect warning, ETB reload), FMC (reset values, cache way invalidation), AIPS (storage + warnings), AXBS (priority
checks, RO lock), MPU (reset regions, VLD clearing, RGD0 core restrictions, RGDAAC alias, not-enforced warning) and
FlexBus (chip-select decode, global chip select, external SRAM device, port sizes, write protect)."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.session import Session
from engine_util import build_image

ROOT = Path(__file__).resolve().parent.parent
K22 = ROOT / "chips" / "mk22fn1m0vll12.yaml"
MCM, FMC, AIPS0, AXBS, MPU, FB = 0xE0080000, 0x4001F000, 0x40000000, 0x40004000, 0x4000D000, 0x4000C000
SCGC7 = 0x40047040


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


@pytest.fixture
def chip(desc):
    return Chip(desc)


def r(chip, a, n=4):
    return chip.bus.read(a, n)


def w(chip, a, v, n=4):
    chip.bus.write(a, n, v)


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


# ---------------------------------------------------------------------------------------------------------- MCM
def test_mcm_reset_values_and_sram_write_protect(chip):
    assert r(chip, MCM + 8, 2) == 0x1F and r(chip, MCM + 0xA, 2) == 0x37
    w(chip, MCM + 8, 0, 2)                                         # read-only
    assert r(chip, MCM + 8, 2) == 0x1F
    w(chip, MCM + 0xC, (1 << 30) | (2 << 28))
    assert r(chip, MCM + 0xC) == (1 << 30) | (2 << 28)
    assert any("SRAM_L write protection is not enforced" in m for m in warns(chip))
    w(chip, MCM + 0x18, 0x102)                                     # ETBRL not 0 mod 4: ignored
    assert r(chip, MCM + 0x18) == 0
    w(chip, MCM + 0x18, 0x100)
    w(chip, MCM + 0x14, 0x8)                                       # RLRQ
    assert r(chip, MCM + 0x1C) == 0x100 and r(chip, MCM + 0x14) == 0


def test_mcm_iscr_mirrors_fpscr_and_raises_irq():
    code = {"reset": """
  ldr r0, =0xE000ED88
  ldr r1, [r0]
  orr r1, r1, #(0xF << 20)
  str r1, [r0]
  dsb
  isb
  vmov.f32 s0, #1.0
  vsub.f32 s1, s0, s0
  vdiv.f32 s2, s0, s1
spin:
  b spin
"""}
    desc = load_chip(K22)
    chip = Chip(desc, clock_hz=120e6)
    img, _ = build_image(code)
    chip.load_firmware(img)
    chip.reset()
    mcm = chip.peripherals["MCM"]
    w(chip, MCM + 0x10, 1 << 25)                                   # FDZCE
    chip.run_for(30e3)
    iscr = r(chip, MCM + 0x10)
    assert iscr & (1 << 9) and iscr & (1 << 25)                    # FDZC (divide by zero) mirrors FPSCR.DZC
    assert mcm._line                                               # "MCM" interrupt line (IRQ 17)
    chip.core.write_reg("fpscr", 0)                                # handler clears FPSCR
    chip.run_for(30e3)
    assert not r(chip, MCM + 0x10) & (1 << 9) and not mcm._line


# ---------------------------------------------------------------------------------------------------------- FMC
def test_fmc_reset_and_way_invalidation(chip):
    assert r(chip, FMC) == 0x00F8003F and r(chip, FMC + 4) == 0x3004001F and r(chip, FMC + 8) == 0x3004001F
    w(chip, FMC + 0x100, 0xFFFFFFFF)                               # TAGVDW0S0: tag[19:6] + valid only
    assert r(chip, FMC + 0x100) == 0x000FFFC1
    w(chip, FMC + 0x110, 0x41)                                     # way 1 set 0
    w(chip, FMC + 0x200, 0x12345678)                               # DATAW0S0UM
    w(chip, FMC + 4, r(chip, FMC + 4) | (1 << 20) | (1 << 19))      # invalidate way 0, speculation buffer
    assert r(chip, FMC + 0x100) == 0 and r(chip, FMC + 0x200) == 0 and r(chip, FMC + 0x110) == 0x41
    assert not r(chip, FMC + 4) & (0x1F << 19)                     # CINV_WAY / S_B_INV read 0
    w(chip, FMC + 4, 0)                                            # RWSC / B0MW read-only
    assert r(chip, FMC + 4) >> 28 == 3 and (r(chip, FMC + 4) >> 17) & 3 == 2


# ---------------------------------------------------------------------------------------------------------- AIPS
def test_aips_storage_and_warning(chip):
    assert r(chip, AIPS0) == 0 and r(chip, AIPS0 + 0x20) == 0
    w(chip, AIPS0 + 0x20, 0x44444444)
    assert r(chip, AIPS0 + 0x20) == 0x44444444
    assert any("not enforced" in m and "PACRA" in m for m in warns(chip))
    assert chip.peripherals["AIPS1"].model == "kinetis.aips"


# ---------------------------------------------------------------------------------------------------------- AXBS
def test_axbs_priorities_and_ro_lock(chip):
    assert r(chip, AXBS) == 0x00430210 and r(chip, AXBS + 0x10) == 0
    w(chip, AXBS, 0x00012340)                                      # M1 = 4, M2 = 3, M4 = 1, M5 = 0, M0 = 0: dup
    assert r(chip, AXBS) == 0x00430210 and any("duplicate or invalid" in m for m in warns(chip))
    w(chip, AXBS, 0x00034021)                                      # M0 1, M1 2, M2 0, M4 3, M5 0 -> dup again
    assert r(chip, AXBS) == 0x00430210
    w(chip, AXBS, 0x00340021)                                      # M0 1, M1 2, M2 0, M4 4, M5 3: valid
    assert r(chip, AXBS) == 0x00340021
    w(chip, AXBS + 0x10, (1 << 31) | 0x100)                        # RO + round robin
    w(chip, AXBS, 0x00430210)
    w(chip, AXBS + 0x10, 0)
    assert r(chip, AXBS) == 0x00340021 and r(chip, AXBS + 0x10) == (1 << 31) | 0x100


# ---------------------------------------------------------------------------------------------------------- MPU
def test_mpu_reset_regions_and_restrictions(chip):
    mpu = chip.peripherals["MPU"]
    assert r(chip, MPU) == 0x00815101                              # VLD, 12 regions, 5 slave ports
    assert [r(chip, MPU + 0x400 + 4 * k) for k in range(4)] == [0, 0xFFFFFFFF, 0x0061F7DF, 1]
    assert [r(chip, MPU + 0x410 + 4 * k) for k in range(4)] == [0, 0x1F, 0, 0]
    assert r(chip, MPU + 0x800) == 0x0061F7DF and r(chip, MPU + 0x804) == 0
    w(chip, MPU + 0x400, 0x1000)                                   # RGD0 start: not writable by the core
    w(chip, MPU + 0x40C, 0)
    assert r(chip, MPU + 0x400) == 0 and r(chip, MPU + 0x40C) == 1
    w(chip, MPU + 0x800, 0x18)                                     # RGDAAC0: core = user rights = none
    m1 = 0x0061F7DF & (0x7 << 6 | 0x3 << 9)                        # M1 (debugger) fields kept
    assert r(chip, MPU + 0x408) == 0x18 | m1 and r(chip, MPU + 0x40C) == 1
    assert any("not enforced" in m for m in warns(chip))           # the core lost rwx on the whole map
    w(chip, MPU + 0x800, 0x0061F7DF)
    w(chip, MPU + 0x41C, 1)                                        # RGD1 valid
    w(chip, MPU + 0x414, 0x2000FFFF)                               # writing WORD1 clears VLD
    assert r(chip, MPU + 0x41C) == 0 and r(chip, MPU + 0x414) == 0x2000FFFF
    w(chip, MPU + 0x41C, 1)
    w(chip, MPU + 0x804, 0x1F)                                     # RGDAAC1 does not clear VLD
    assert r(chip, MPU + 0x418) == 0x1F and r(chip, MPU + 0x41C) == 1
    assert mpu.region(1) == {"start": 0, "end": 0x2000FFFF, "word2": 0x1F, "valid": True}
    w(chip, MPU, 1 << 31)                                          # SPERR w1c, VLD written 0 -> disabled
    assert r(chip, MPU) & 1 == 0


# ---------------------------------------------------------------------------------------------------------- FlexBus
def _fb_session(**sram):
    s = Session("mk22fn1m0vll12", ROOT / "firmware/samples/build/button_mirror.elf", clock_hz=120e6)
    s.run_for(10e3)
    if sram:
        s.attach_sram("FB:" + ",".join(f"{k}={v}" for k, v in sram.items()))
    chip = s.chip
    w(chip, SCGC7, r(chip, SCGC7) | 1)
    return s, chip


@pytest.mark.skipif(not (ROOT / "firmware/samples/build/button_mirror.elf").exists(), reason="samples not built")
def test_flexbus_global_cs_then_decode_with_sram():
    s, chip = _fb_session(size="64K", width=16)
    fb = chip.peripherals["FB"]
    dev = fb.devices[0]
    assert dev is not None and dev.width == 16
    w(chip, 0x60000010, 0xBEEF, 2)                                 # out of reset FB_CS0 answers any address
    assert dev.mem[4] == 0xBEEF and r(chip, 0x60000010, 2) == 0xBEEF   # (32-bit port at reset: location 4)
    w(chip, FB + 0x00, 0x60010000)                                 # CSAR0 = 0x6001_0000
    w(chip, FB + 0x08, (r(chip, FB + 0x08) & ~0xC0) | 0x80)       # CSCR0: 16-bit port
    w(chip, FB + 0x04, 0x00000001)                                 # CSMR0: 64 KB, valid
    assert fb.window(0) == (0x60010000, 0x6001FFFF)
    w(chip, 0x60010000, 0x11223344)                                # 32-bit CPU access = two 16-bit beats
    assert dev.mem[0] == 0x3344 and dev.mem[1] == 0x1122
    w(chip, 0x60010003, 0xAA, 1)                                   # odd byte lane
    assert dev.mem[1] == 0xAA22 and r(chip, 0x60010002, 2) == 0xAA22          # little endian: upper byte
    assert r(chip, 0x60020000) == 0                                 # no chip select: bus error on silicon
    assert any("matches no chip select" in m for m in warns(chip))
    w(chip, FB + 0x04, 0x00000101)                                 # write protect
    w(chip, 0x60010000, 0, 2)
    assert dev.mem[0] == 0x3344 and any("write-protected" in m for m in warns(chip))
    s.close()


@pytest.mark.skipif(not (ROOT / "firmware/samples/build/button_mirror.elf").exists(), reason="samples not built")
def test_flexbus_8bit_port_and_no_device():
    s, chip = _fb_session(size="4K", width=8)
    dev = chip.peripherals["FB"].devices[0]
    w(chip, FB + 0x00, 0x70000000)
    w(chip, FB + 0x08, (r(chip, FB + 0x08) & ~0xC0) | 0x40)       # 8-bit port
    w(chip, FB + 0x04, 0x00010001)                                 # 128 KB window
    w(chip, 0x70000004, 0x04030201)
    assert list(dev.mem[4:8]) == [1, 2, 3, 4] and r(chip, 0x70000004) == 0x04030201
    w(chip, FB + 0x0C, 0x80000000)                                 # CS1 at 0x8000_0000, no device
    w(chip, FB + 0x10, 0x00000001)
    assert r(chip, 0x80000000) == 0 and any("no device" in m for m in warns(chip))
    s.close()
