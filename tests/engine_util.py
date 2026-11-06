"""Helpers for engine tests: a minimal in-code ChipDescription and keystone-assembled firmware images."""
from __future__ import annotations

import struct

from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks

from chipem.chip import Chip
from chipem.chipdesc import ChipDescription, CoreDesc, MemoryDesc, PeripheralDesc
from chipem.firmware import FirmwareImage

FLASH = 0x00000000
RAM = 0x20000000
STACK_TOP = 0x20008000
DATA = 0x20000100          # scratch variables used by tests

_ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)


def asm(src: str, addr: int) -> bytes:
    code, _ = _ks.asm(src, addr)
    return bytes(code)


def make_desc(cpu: str = "cortex-m4", fpu: bool = True, arch: str = "armv7e-m", peripherals=(), num_irqs: int = 32,
              hz: float = 1_000_000.0) -> ChipDescription:
    return ChipDescription(
        name="TESTCHIP", core=CoreDesc(arch=arch, cpu=cpu, fpu=fpu, clock_hz=hz, num_irqs=num_irqs),
        family="test",
        memory=[MemoryDesc("FLASH", FLASH, 0x10000, "flash"), MemoryDesc("RAM", RAM, 0x10000, "ram")],
        peripherals=[PeripheralDesc(**p) if isinstance(p, dict) else p for p in peripherals],
        irqs={0: "TEST0", 1: "TEST1"}, pins=[])


def build_image(code: dict[str, str], *, vectors: dict[int, str] | None = None, sp: int = STACK_TOP,
                slot: int = 0x200) -> tuple[FirmwareImage, dict[str, int]]:
    """code: name -> assembly ('reset' required). Each routine is assembled at its own slot starting at 0x400.
    vectors: exception number -> routine name. Returns (image, {name: address})."""
    addrs: dict[str, int] = {}
    blobs: list[tuple[int, bytes]] = []
    a = 0x400
    for name, src in code.items():
        b = asm(src, a)
        if len(b) > slot:
            raise ValueError(f"{name} too long ({len(b)} bytes)")
        addrs[name] = a
        blobs.append((a, b))
        a += slot
    vt = [0] * 64
    vt[0] = sp
    vt[1] = addrs["reset"] | 1
    for n, name in (vectors or {}).items():
        vt[n] = addrs[name] | 1
    img = bytearray(a)
    img[0:256] = struct.pack("<64I", *vt)
    for addr, b in blobs:
        img[addr:addr + len(b)] = b
    return FirmwareImage([(FLASH, bytes(img))]), addrs


def make_chip(code: dict[str, str], vectors: dict[int, str] | None = None, **kw) -> tuple[Chip, dict[str, int]]:
    chip_kw = {k: kw.pop(k) for k in ("precise", "max_slice", "handler_slice", "log") if k in kw}
    desc = make_desc(**kw)
    chip = Chip(desc, **chip_kw)
    img, addrs = build_image(code, vectors=vectors)
    chip.load_firmware(img)
    chip.reset()
    return chip, addrs


def word(chip: Chip, addr: int) -> int:
    return chip.bus.read(addr, 4)

# --- hand encodings for system-register instructions keystone cannot assemble -------------------------
SYSM = {"apsr": 0, "ipsr": 5, "msp": 8, "psp": 9, "primask": 16, "basepri": 17, "faultmask": 19, "control": 20}
CPSID_I = ".short 0xB672"
CPSIE_I = ".short 0xB662"


def msr(sysreg: str, rn: int) -> str:
    """MSR <sysreg>, r<rn>"""
    return f".short 0x{0xF380 | rn:04X}, 0x{0x8800 | SYSM[sysreg]:04X}"


def mrs(rd: int, sysreg: str) -> str:
    """MRS r<rd>, <sysreg>"""
    return f".short 0xF3EF, 0x{0x8000 | (rd << 8) | SYSM[sysreg]:04X}"


def record(tag: int) -> str:
    """Append `tag` to the log at DATA: [DATA] = count, [DATA + 4*count] = tag. Clobbers r0-r3."""
    return f"""
  ldr r0, =0x{DATA:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  lsls r2, r1, #2
  movs r3, #{tag}
  str r3, [r0, r2]
"""


def read_log(chip) -> list[int]:
    n = chip.bus.read(DATA, 4)
    return [chip.bus.read(DATA + 4 * (i + 1), 4) for i in range(n)]
