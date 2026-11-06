"""A duck-typed stand-in for the C28x core (docs/CONTRACTS.md "16-bit address units and the C28x core") used by
the engine/arch/peripheral tests so they do not depend on chipem.core.c28x.

Memory is word-addressed (16-bit units) and stored as a little-endian byte image.  `run(n)` executes `n` "instructions"
by calling `self.program(core)` once per instruction if set (tests use it to script loads/stores), else just counts.
`ld`/`st` go through RAM, MMIO callbacks or the unmapped handler exactly like a real core would.
"""
from __future__ import annotations

from chipem.core.base import StopReason


class FakeC28xCore:
    page_size = 0x40
    precise = True
    exact_counts = True
    cpu = "c28x"

    def __init__(self, *a, **kw):
        self.ram: list[tuple[int, int, bytearray, bool]] = []      # (base, size_units, image, writable)
        self.mmio: list[tuple[int, int, object, object]] = []
        self.unmapped = None
        self.regs = {n: 0 for n in ("pc", "sp", "acc", "p", "xt", "xar0", "xar1", "xar2", "xar3", "xar4", "xar5",
                                    "xar6", "xar7", "dp", "st0", "st1", "ier", "ifr", "dbgier", "rpc")}
        self.running = False
        self.last_fault = None
        self.stop_info = None
        self.taken: list[tuple[int, int]] = []
        self.program = None
        self._stop = False
        self._n = 0
        self.stop_requests = 0

    # ---- memory
    def map_memory(self, base, size, *, writable=True, executable=True):
        self.ram.append((base, size, bytearray(2 * size), writable))

    def map_mmio(self, base, size, read, write):
        self.mmio.append((base, size, read, write))

    def set_unmapped_handler(self, fn):
        self.unmapped = fn

    def flush_code_cache(self):
        pass

    def _ram(self, addr):
        for b, s, img, w in self.ram:
            if b <= addr < b + s:
                return b, s, img, w
        return None

    def read_mem(self, addr, n):
        out = bytearray()
        for i in range(n):
            r = self._ram(addr + i)
            if r is None:
                raise KeyError(f"unmapped 0x{addr + i:x}")
            b, s, img, w = r
            o = 2 * (addr + i - b)
            out += img[o:o + 2]
        return bytes(out)

    def write_mem(self, addr, data):
        data = bytes(data)
        for i in range(len(data) // 2):
            r = self._ram(addr + i)
            if r is None:
                raise KeyError(f"unmapped 0x{addr + i:x}")
            b, s, img, w = r
            o = 2 * (addr + i - b)
            img[o:o + 2] = data[2 * i:2 * i + 2]

    def _find(self, addr, access, retry=True):
        r = self._ram(addr)
        if r is not None:
            return ("ram", r)
        for b, s, rd, wr in self.mmio:
            if b <= addr < b + s:
                return ("mmio", (b, rd, wr))
        if retry and self.unmapped is not None and self.unmapped(addr, 1, access):
            return self._find(addr, access, retry=False)
        raise KeyError(f"bus fault at 0x{addr:x}")

    def ld(self, addr, size=1):
        kind, x = self._find(addr, "r")
        if kind == "ram":
            return int.from_bytes(self.read_mem(addr, size), "little")
        b, rd, wr = x
        return rd(addr - b, size)

    def st(self, addr, size, value):
        kind, x = self._find(addr, "w")
        if kind == "ram":
            if x[3]:
                self.write_mem(addr, (value & ((1 << (16 * size)) - 1)).to_bytes(2 * size, "little"))
            return
        b, rd, wr = x
        wr(addr - b, size, value)

    # ---- registers / execution
    def reset(self, pc, sp):
        for k in self.regs:
            self.regs[k] = 0
        self.regs["pc"], self.regs["sp"] = pc, sp

    def read_reg(self, n):
        return self.regs[n]

    def write_reg(self, n, v):
        self.regs[n] = v

    def read_regs(self, names):
        return [self.regs[n] for n in names]

    def write_regs(self, names, values):
        for n, v in zip(names, values):
            self.regs[n] = v

    @property
    def pc(self):
        return self.regs["pc"]

    @property
    def sp(self):
        return self.regs["sp"]

    def run(self, max_insns):
        self.running = True
        self._stop = False
        self._n = 0
        try:
            while self._n < max_insns:
                if self.program is not None:
                    self.program(self)
                self._n += 1
                if self._stop:
                    return StopReason.USER, self._n
            return StopReason.COUNT, self._n
        finally:
            self.running = False

    def insns_in_slice(self):
        return self._n if self.running else 0

    def request_stop(self, from_mmio=False):
        self.stop_requests += 1
        self._stop = True
        return True

    def take_interrupt(self, target, int_number):
        self.taken.append((target, int_number))
        self.regs["ier"] &= ~(1 << (int_number - 1)) if int_number <= 16 else 0xFFFF
        self.regs["st1"] |= 1                     # INTM
        self.regs["pc"] = target


def fake_chip_desc(extra_yaml: str = "", peripherals: str = "", irqs: str = ""):
    """A minimal 16-bit chip description (M0/M1 RAM, flash, PIE) for engine tests."""
    import yaml
    from chipem.chipdesc import load_chip
    import tempfile
    from pathlib import Path
    text = f"""
name: fake28
family: ti_c2000
core: {{arch: c28x, backend: c28x, cpu: c28x, fpu: false, clock_hz: 100000000, addr_unit_bits: 16, num_irqs: 0}}
gpio_name_format: "GPIO{{num}}"
memory:
  - {{name: M0, base: 0x0000, size: 0x400, kind: ram}}
  - {{name: M1, base: 0x0400, size: 0x400, kind: ram}}
  - {{name: FLASH, base: 0x330000, size: 0x10000, kind: flash, fill: 0xFF}}
  - {{name: BROM, base: 0x3FE000, size: 0x2000, kind: rom, fill: 0xFF}}
peripherals:
  - {{name: PIE, model: ti_c2000.pie, base: 0x0CE0, size: 0x120}}
{peripherals}
irqs:
  38: TINT0
  35: XINT1
{irqs}
boot: {{entry: 0x33FFF6}}
auto_stub_ranges: [[0x0800, 0x0FFF], [0x5000, 0x7FFF]]
{extra_yaml}
pins: []
"""
    d = Path(tempfile.mkdtemp()) / "fake28.yaml"
    d.write_text(text)
    return load_chip(d)


def make_fake_chip(desc=None, monkeypatch=None, **kw):
    """Chip on the fake core (patches chipem.chip._make_core for the construction)."""
    import chipem.chip as chipmod
    desc = desc or fake_chip_desc()
    orig = chipmod._make_core
    chipmod._make_core = lambda cd, precise: FakeC28xCore()
    try:
        return chipmod.Chip(desc, **kw)
    finally:
        chipmod._make_core = orig
