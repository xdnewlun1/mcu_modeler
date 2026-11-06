"""16-bit address units in the engine (docs/CONTRACTS.md "16-bit address units and the C28x core"):
MMIOBlock/Reg in units, the Bus on a word-addressed core, chip-provided catch-all windows, and a Chip with the
C28x arch on a fake core."""
import pytest

from chipem.bus import Bus, BusError, DEFAULT_CATCH_ALL, MemoryRegion
from chipem.core.base import StopReason
from chipem.periph.base import MMIOBlock, Reg
from chipem.firmware import FirmwareImage

from c28x_fake import FakeC28xCore, fake_chip_desc, make_fake_chip


def make_block(**kw):
    seen = []

    def on_w(blk, reg, old, new):
        seen.append((reg.name, old, new))
        return None

    regs = [Reg("CTL", 0x0, 1, reset=0x8001, ro_mask=0x8000, on_write=on_w),
            Reg("ST", 0x1, 1, reset=0x00F0, w1c_mask=0x00F0),
            Reg("CNT", 0x2, 2, reset=0x12345678, on_write=on_w),
            Reg("RD", 0x4, 1, on_read=lambda b, r: 0xBEEF)]
    blk = MMIOBlock("T", 0x7000, 0x10, regs, unit_bits=16, **kw)
    return blk, seen


def test_reg_masks_follow_units():
    r = Reg("X", 0, 1, reset=0x1234, unit_bits=16)
    assert r.mask == 0xFFFF and r.reset == 0x1234 and r.bw == 2
    r8 = Reg("Y", 0, 1, reset=0x1234)
    assert r8.mask == 0xFF and r8.reset == 0x34
    blk, _ = make_block()
    # a Reg declared with 8-bit masks is rescaled to the block's units (raw values kept)
    assert blk.add_reg(Reg("LATE", 0x8, 2, reset=0xAABBCCDD)).reset == 0xAABBCCDD


def test_mmio16_reads_writes_hooks_and_reset():
    blk, seen = make_block()
    assert len(blk.mem) == 0x20
    assert blk.read(0x0, 1) == 0x8001
    assert blk.read(0x2, 2) == 0x12345678
    assert blk.read(0x2, 1) == 0x5678 and blk.read(0x3, 1) == 0x1234      # low word at the lower address
    assert bytes(blk.mem[4:8]) == bytes.fromhex("78563412")
    blk.write(0x0, 1, 0x0002)                                            # ro bit 15 kept
    assert blk.get("CTL") == 0x8002 and seen[-1] == ("CTL", 0x8001, 0x8002)
    blk.write(0x3, 1, 0xCAFE)                                            # high word of a 2-unit register
    assert blk.get("CNT") == 0xCAFE5678 and seen[-1] == ("CNT", 0x12345678, 0xCAFE5678)
    blk.write(0x1, 1, 0x0030)                                            # w1c
    assert blk.get("ST") == 0x00C0
    assert blk.read(0x4, 1) == 0xBEEF
    # 32-bit access spanning two 1-unit registers
    assert blk.read(0x0, 2) == (0x00C0 << 16) | 0x8002
    # unknown offsets behave as 16-bit RAM
    blk.write(0x9, 1, 0x1234)
    assert blk.read(0x9, 1) == 0x1234 and blk.read(0x8, 2) == 0x12340000
    blk.set("CNT", 0x1_0000_0001)
    assert blk.get("CNT") == 0x00000001
    blk.reset()
    assert blk.get("CTL") == 0x8001 and blk.get("CNT") == 0x12345678 and blk.read(0x9, 1) == 0


def test_unit_bits_from_params_and_class():
    b = MMIOBlock("P", 0, 4, params={"unit_bits": 16})
    assert b.unit_bytes == 2 and len(b.mem) == 8

    class W(MMIOBlock):
        unit_bits = 16
    assert W("W", 0, 4).unit_bytes == 2
    assert MMIOBlock("B", 0, 4).unit_bytes == 1


def test_bus16_memory_and_mmio():
    core = FakeC28xCore()
    bus = Bus(core, addr_unit_bits=16, catch_all=((0x0800, 0x1000), (0x5000, 0x8000)))
    bus.map_memory("M0", 0x0, 0x400, "ram", data=bytes.fromhex("3412cdab"))
    assert bus.read(0x0, 1) == 0x1234 and bus.read(0x1, 1) == 0xABCD and bus.read(0x0, 2) == 0xABCD1234
    bus.write(0x10, 2, 0x11223344)
    assert bus.read(0x10, 1) == 0x3344 and bus.read(0x11, 1) == 0x1122
    assert bus.read_bytes(0x10, 4) == bytes.fromhex("44332211")
    assert bus.read_bytes(0x10, 3) == bytes.fromhex("443322")
    bus.write_bytes(0x20, b"\x01\x02\x03")               # odd length keeps the other byte of the last word
    assert bus.read(0x20, 1) == 0x0201 and bus.read(0x21, 1) == 0x0003
    blk, _ = make_block()
    blk.base = 0x7010
    bus.map_mmio(blk)                                     # sub-page block → page dispatcher
    assert bus.read(0x7012, 2) == 0x12345678
    bus.write(0x7013, 1, 0x9999)
    assert blk.get("CNT") == 0x99995678
    assert bus.read_bytes(0x7012, 4) == bytes.fromhex("78569999")
    assert isinstance(bus.lookup(0x7010), MMIOBlock) and isinstance(bus.lookup(0x3FF), MemoryRegion)
    # the core sees the same through its callbacks
    assert core.ld(0x7012, 2) == 0x99995678


def test_bus_catch_all_windows_from_chip():
    core = FakeC28xCore()
    bus = Bus(core, addr_unit_bits=16, catch_all=((0x0800, 0x1000), (0x5000, 0x8000)))
    bus.map_memory("M0", 0x0, 0x400, "ram")
    blk, _ = make_block()
    blk.base = 0x0CE0
    bus.map_mmio(blk)
    assert core.ld(0x6000) == 0                           # auto stub 0x6000-0x6FFF
    stub = bus.auto_stubs[0x6000]
    assert stub.size == 0x1000 and stub.unit_bytes == 2
    core.st(0x6001, 1, 0xABCD)
    assert core.ld(0x6001) == 0xABCD
    core.st(0x0900, 1, 0x55AA)                            # window starts at 0x0800: stub clipped to it
    s2 = bus.lookup(0x0900)
    assert s2.base >= 0x0800 and s2.base + s2.size <= 0x1000
    with pytest.raises(KeyError):
        core.ld(0x2000)                                    # outside every window: bus fault
    with pytest.raises(BusError):
        bus.read(0x9000, 1)


def test_bus8_defaults_unchanged():
    from chipem.core.unicorn_cm import UnicornCortexM
    bus = Bus(UnicornCortexM("cortex-m4"))
    assert bus.unit_bytes == 1 and bus.catch_all == list(DEFAULT_CATCH_ALL)
    bus.map_memory("SRAM", 0x20000000, 0x1000, "ram")
    bus.write(0x20000000, 4, 0xA1B2C3D4)
    assert bus.read(0x20000002, 2) == 0xA1B2 and bus.read_bytes(0x20000000, 2) == b"\xd4\xc3"
    assert bus.read(0x40001000, 4) == 0 and 0x40001000 in bus.auto_stubs


def test_firmware_image_units():
    img = FirmwareImage([(0x33FFF6, bytes.fromhex("0000ff00")), (0x330000, bytes(8))], 0x330000, unit_bytes=2)
    assert img.start == 0x330000 and img.end == 0x33FFF8 and img.size == 6
    assert img.contains(0x33FFF7) and not img.contains(0x33FFF8)
    assert img.read(0x33FFF6, 2) == bytes.fromhex("0000ff00")
    merged = FirmwareImage([(0x10, b"\x01\x00\x02\x00"), (0x12, b"\x03\x00")], unit_bytes=2)
    assert merged.segments == [(0x10, b"\x01\x00\x02\x00\x03\x00")]


def test_chip16_fake_core_reset_load_and_pie():
    chip = make_fake_chip()
    assert chip.unit_bits == 16 and chip.bus.unit_bytes == 2 and "PIE" in chip.peripherals
    img = FirmwareImage([(0x33FFF6, bytes.fromhex("4800aa00")), (0x0D4E, bytes.fromhex("0080"))], None,
                        unit_bytes=2)
    chip.load_firmware(img)
    assert chip.bus.read(0x33FFF6, 2) == 0x00AA0048
    chip.reset()
    core = chip.core
    assert core.pc == 0x33FFF6 and core.sp == 0x400 and core.regs["st1"] & 1
    pie = chip.arch.pie
    # program the PIE: vector for TINT0 (INT1.7, vector 38 at 0x0D4C), enable, and INT1 in IER, INTM clear
    chip.bus.write(0x0D00 + 2 * 38, 2, 0x330100)
    chip.bus.write(0x0CE0, 1, 1)                          # ENPIE
    chip.bus.write(0x0CE2, 1, 1 << 6)                     # PIEIER1.7
    core.regs["ier"] = 1
    core.regs["st1"] &= ~1
    chip.arch.set_irq("TINT0", True)
    assert pie.ifr[1] == 1 << 6 and pie.ack & 1 and core.regs["ifr"] & 1
    chip.run_cycles(10)
    assert core.taken == [(0x330100, 1)] and pie.ifr[1] == 0 and not core.regs["ifr"] & 1
    assert chip.bus.read(0x0CE0, 1) & 0xFFFE == (0x0D00 + 2 * 38) & 0xFFFE      # PIEVECT
    # a second edge while the group is still acknowledged only latches the flag
    chip.arch.set_irq("TINT0", False)
    chip.arch.set_irq("1.7", True)
    assert pie.ifr[1] == 1 << 6 and not core.regs["ifr"] & 1
    core.regs["st1"] &= ~1
    core.regs["ier"] = 1
    chip.bus.write(0x0CE1, 1, 1)                          # PIEACK: re-raises INT1
    assert core.regs["ifr"] & 1 and pie.ack & 1
    chip.run_cycles(5)
    assert core.taken[-1] == (0x330100, 1)


def test_chip16_auto_stub_ranges_from_yaml():
    chip = make_fake_chip()
    assert chip.bus.catch_all == [(0x0800, 0x1000), (0x5000, 0x8000)]
    assert chip.bus.read(0x7400, 1) == 0
    assert chip.bus.auto_stubs
