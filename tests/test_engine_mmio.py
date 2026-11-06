from chipem.bus import Bus, BusError, MemoryRegion
from chipem.core.unicorn_cm import UnicornCortexM
from chipem.log import EventLog
from chipem.periph import MMIOBlock, Reg, registry

import pytest


def test_subword_reads_and_writes():
    b = MMIOBlock("T", 0x40000000, 0x100, [Reg("A", 0x0, reset=0x11223344), Reg("H", 0x4, width=2, reset=0xBEEF)])
    assert b.read(0, 4) == 0x11223344
    assert b.read(1, 1) == 0x33
    assert b.read(2, 2) == 0x1122
    b.write(3, 1, 0xAA)
    assert b.get("A") == 0xAA223344
    b.write(0, 2, 0x5566)
    assert b.get("A") == 0xAA225566
    assert b.read(4, 2) == 0xBEEF
    # word access spanning a 16-bit register and an unknown (RAM-like) halfword
    b.write(4, 4, 0x12345678)
    assert b.get("H") == 0x5678
    assert b.read(4, 4) == 0x12345678
    b.reset()
    assert b.get("A") == 0x11223344 and b.get("H") == 0xBEEF


def test_ro_and_w1c_masks():
    b = MMIOBlock("T", 0, 0x10, [Reg("S", 0, reset=0xFF00, ro_mask=0xF000, w1c_mask=0x0F00)])
    b.write(0, 4, 0x0000_0012)          # ro nibble stays F, w1c bits written 0 keep their value
    assert b.get("S") == 0xFF12
    b.write(0, 4, 0x0300)               # clear w1c bits 8,9
    assert b.get("S") == 0xFC00
    b.write(1, 1, 0x04)                 # byte write clears bit 10 only
    assert b.get("S") == 0xF800


def test_hooks_see_full_register_value():
    seen = []

    def on_write(blk, reg, old, new):
        seen.append((old, new))
        return new | 0x80000000

    def on_read(blk, reg):
        return blk.get("R") + 1
    b = MMIOBlock("T", 0, 0x10, [Reg("R", 4, reset=0x1000, on_write=on_write, on_read=on_read)])
    b.write(5, 1, 0x22)
    assert seen == [(0x1000, 0x2200)]
    assert b.get("R") == 0x80002200
    assert b.read(4, 4) == 0x80002201
    assert b.read(7, 1) == 0x80


def test_unknown_offsets_ram_like_and_logged_once():
    log = EventLog()
    b = MMIOBlock("T", 0x40001000, 0x100, log=log)
    b.write(0x10, 4, 0xDEADBEEF)
    b.write(0x12, 1, 0x55)
    assert b.read(0x10, 4) == 0xDE55BEEF
    b.read(0x20, 4)
    ev = log.filter("mmio")
    assert [e.offset for e in ev] == [0x10, 0x20]


def test_registry_models_and_fallback():
    log = EventLog()

    class FakeChip:
        pass
    chip = FakeChip()
    chip.log = log
    s = registry.create("stub", "S", 0x40000000, 0x1000, {}, chip)
    r = registry.create("ram", "R", 0x40001000, 0x20, {}, chip)
    u = registry.create("kinetis.doesnotexist", "U", 0x40002000, 0x1000, {"x": 1}, chip)
    assert type(s).model == "stub" and type(r).model == "ram"
    assert type(u).model == "stub" and u.params == {"x": 1}
    assert any(e.kind == "warn" and e.get("model") == "kinetis.doesnotexist" for e in log)


def _bus():
    core = UnicornCortexM("cortex-m4")
    log = EventLog()
    bus = Bus(core, log=log, bitband=True)
    return core, bus, log


def test_bus_memory_mmio_subpage_and_catch_all():
    core, bus, log = _bus()
    bus.map_memory("RAM", 0x20000000, 0x10000, "ram")
    bus.map_memory("FLASH", 0x0, 0x1000, "flash", fill=0xFF)
    g1 = MMIOBlock("GPIOA", 0x400FF000, 0x40, [Reg("PDOR", 0)])
    g2 = MMIOBlock("GPIOB", 0x400FF040, 0x40, [Reg("PDOR", 0)])
    full = MMIOBlock("SIM", 0x40047000, 0x2000, [Reg("SCGC5", 0x1038, reset=0x40182)])
    for b in (g1, g2, full):
        bus.map_mmio(b)
    bus.write(0x20000010, 4, 0xCAFEBABE)
    assert bus.read(0x20000012, 2) == 0xCAFE
    assert bus.read_bytes(0x0, 4) == b"\xff" * 4
    assert bus.read(0x40048038, 4) == 0x40182
    bus.write(0x400FF040, 4, 7)
    assert g2.get("PDOR") == 7 and g1.get("PDOR") == 0
    assert bus.lookup(0x400FF044) is g2
    assert isinstance(bus.lookup(0x20000000), MemoryRegion)
    # unclaimed bytes of a shared page fall back to a stub
    bus.write(0x400FF200, 4, 0x99)
    assert bus.read(0x400FF200, 4) == 0x99
    # catch-all: unmapped peripheral space becomes a stub on first touch
    assert bus.lookup(0x40030004) is None
    bus.write(0x40030004, 4, 0x1234)
    assert bus.read(0x40030004, 4) == 0x1234
    assert bus.lookup(0x40030004).name == "unmapped_40030000"
    assert any(e.kind == "warn" and e.get("addr") == 0x40030000 for e in log)
    # bit-band alias of SRAM and peripherals
    bus.write(0x22000000 + 0x10 * 32 + 4 * 4, 4, 0)       # clear bit 4 of byte 0x20000010 (0xBE -> 0xAE)
    assert bus.read(0x20000010, 1) == 0xAE
    assert bus.read(0x22000000 + 0x10 * 32 + 1 * 4, 4) == 1
    bus.write(0x42000000 + 0xFF040 * 32 + 3 * 4, 4, 1)
    assert g2.get("PDOR") == 0xF
    with pytest.raises(BusError):
        bus.read(0x60000000, 4)
    with pytest.raises(ValueError):
        bus.map_mmio(MMIOBlock("X", 0x40047000, 0x1000))
