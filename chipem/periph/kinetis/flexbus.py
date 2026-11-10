"""Kinetis FlexBus (FB, K22 RM ch. 31): chip-select registers and the external memory space behind them.

kinetis.flexbus (0x4000C000, registers from the SVD): CSAR0-5 (BA, 31:16), CSMR0-5 (BAM 31:16, WP 8, V 0), CSCR0-5
(SWS, SWSEN, EXTS, ASET, RDAH, WRAH, WS, BLS, AA, PS, BEM, BSTR, BSTW; reset 0x003FFC00), CSPMCR (pin group
multiplexing, stored).  Gate SIM_SCGC7[FLEXBUS].
kinetis.flexbus_space (the FlexBus address windows 0x60000000-0xDFFFFFFF of the memory map, Table 4-1 / 3.5.1):
every access is decoded by the FlexBus model (31.4.2): a chip select matches when CSMRn[V] = 1 and
(address & ~(BAM << 16 | 0xFFFF)) == (BA << 16 & ~(BAM << 16)); until CSMR0[V] is set, FB_CS0 is the global chip
select and answers any FlexBus address.  No match, more than one match, or a write to a CSMRn[WP] window is a bus
error on silicon: here one `warn` per kind, the write is dropped and a read returns 0.
External devices: `attach_memory(dev, cs=None)` connects a board memory (chipem/devices/sram.py `ParallelSram`:
`read(loc)`, `write(loc, value, mask)`, `size`, `width`; board YAML `devices: [{type: sram, chip: A, bus: FB,
size: 256K, width: 16}]`) to a chip select (the lowest free one when `cs` is None; `dev.cs` is honoured too).  The
data port is CSCRn.PS (32, 8 or 16 bits): a CPU access is split into port-width beats; beat k of a window offset
`off` addresses device location off // port_bytes (address lines A[n:0] of the port's word); a device narrower than
the port only sees its low data lines (one `warn`), a wider one only the port's lines.  A chip select without a device
reads 0 (floating bus; one `warn`).
Not modelled: bus timing (ASET/WS/RDAH/WRAH/burst cycles cost no time), external FB_TA, the FB_AD/FB_CSn/FB_OE_b/
FB_RW_b pin waveforms and pin muxing checks (the bus is modelled at transaction level), BLS/BEM (data is always
right-aligned for the device), instruction fetch from FlexBus memory (MMIO cannot be executed by the core: a jump
there faults), the DMA's FlexBus accesses timing.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, find_model

NCS = 6


@register("kinetis.flexbus")
class FlexBus(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "FB"
        ov: dict[str, dict] = {}
        for n in range(NCS):
            ov[f"CSMR{n}"] = {"on_write": self._csmr_write(n)}
        r = build_regs((name, "FB"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.devices: list[Any] = [None] * NCS
        self._warned: set[str] = set()
        self.accesses = 0

    def reset(self) -> None:
        self._warned = set()
        super().reset()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def attach_memory(self, dev: Any, cs: int | None = None) -> int:
        """Connect an external memory to chip select `cs` (default: dev.cs or the lowest free one)."""
        if cs is None:
            cs = getattr(dev, "cs", None)
        if cs is None:
            free = [n for n in range(NCS) if self.devices[n] is None]
            if not free:
                raise ValueError(f"{self.name}: all {NCS} chip selects already have a device")
            cs = free[0]
        cs = int(cs)
        if not 0 <= cs < NCS:
            raise ValueError(f"{self.name}: chip select {cs} out of range 0-{NCS - 1}")
        self.devices[cs] = dev
        return cs

    # ------------------------------------------------------------------ decode
    def window(self, n: int) -> tuple[int, int] | None:
        """(first, last) address of chip select n while valid."""
        if not self.get(f"CSMR{n}") & 1:
            return None
        ba = self.get(f"CSAR{n}") & 0xFFFF0000
        mask = (self.get(f"CSMR{n}") & 0xFFFF0000) | 0xFFFF
        return ba & ~mask, (ba & ~mask) | mask

    def decode(self, addr: int) -> int | None:
        if not self.get("CSMR0") & 1:
            return 0                                   # global chip select out of reset
        hits = [n for n in range(NCS) if (w := self.window(n)) is not None and w[0] <= addr <= w[1]]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            self._warn_once("nomatch", f"FlexBus: access to 0x{addr:08X} matches no chip select (bus error on "
                            f"silicon): write dropped / read 0")
        else:
            self._warn_once("multi", f"FlexBus: 0x{addr:08X} matches chip selects {hits} (bus error on silicon)")
        return None

    def _port_bytes(self, n: int) -> int:
        ps = (self.get(f"CSCR{n}") >> 6) & 3
        return 4 if ps == 0 else (1 if ps == 1 else 2)

    def access(self, addr: int, size: int, value: int | None) -> int:
        """External access of `size` bytes at `addr` (value None = read)."""
        if self._gate_loc is not None:
            self._gate_ok()
        self.accesses += 1
        n = self.decode(addr)
        if n is None:
            return 0
        if value is not None and self.get(f"CSMR{n}") >> 8 & 1:
            self._warn_once(f"wp{n}", f"FlexBus: write to write-protected FB_CS{n} window at 0x{addr:08X} (bus "
                            f"error on silicon): dropped")
            return 0
        dev = self.devices[n]
        if dev is None:
            self._warn_once(f"nodev{n}", f"FlexBus: FB_CS{n} access at 0x{addr:08X} but no device is attached "
                            f"(floating bus reads 0)")
            return 0
        w = self.window(n)
        off = addr - (w[0] if w is not None else 0x60000000)
        pb = self._port_bytes(n)
        dw = int(getattr(dev, "width", 8 * pb))
        if dw != 8 * pb:
            self._warn_once(f"width{n}", f"FlexBus: FB_CS{n} port is {8 * pb} bits but the device is {dw} bits: "
                            f"only the common low data lines connect")
        lines = (1 << min(dw, 8 * pb)) - 1
        out = 0
        i = 0
        while i < size:
            a = off + i
            loc = a // pb
            lane = a % pb
            nb = min(pb - lane, size - i)
            sh = 8 * lane
            m = ((1 << (8 * nb)) - 1) << sh
            if value is None:
                word = dev.read(loc) & lines
                out |= ((word & m) >> sh) << (8 * i)
            else:
                part = ((value >> (8 * i)) & ((1 << (8 * nb)) - 1)) << sh
                dev.write(loc, part & lines, m & lines)
            i += nb
        return out

    # ------------------------------------------------------------------ hooks
    def _csmr_write(self, n: int):
        def wr(blk, reg, old, new):
            if new & 1 and not old & 1:
                w = self.window(n)
                dev = self.devices[n]
                self.emit("info", block=self.name, msg=f"FlexBus FB_CS{n} valid: 0x{w[0]:08X}-0x{w[1]:08X}, "
                          f"{8 * self._port_bytes(n)}-bit port, "
                          f"{'device ' + str(getattr(dev, 'name', dev)) if dev is not None else 'no device'}")
            return None
        return wr


@register("kinetis.flexbus_space")
class FlexBusSpace(MMIOBlock):
    """The FlexBus external address space: every access goes to the FlexBus chip-select decoder (`params.fb`,
    default "FB").  The window is large (2 GB) but needs no backing store."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, 4, (), log=log, params=params, ram_like=False)
        self.size = size
        self._fb: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._fb = find_model(chip, "kinetis.flexbus", str(self.params.get("fb", "FB")))

    def reset(self) -> None:
        pass

    def read(self, offset: int, size: int) -> int:
        fb = self._fb
        if fb is None:
            return 0
        return fb.access(self.base + offset, size, None)

    def write(self, offset: int, size: int, value: int) -> None:
        fb = self._fb
        if fb is not None:
            fb.access(self.base + offset, size, value)
