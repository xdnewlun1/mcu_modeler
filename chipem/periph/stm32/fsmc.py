"""STM32F4 FSMC (flexible static memory controller, RM0090 ch. 36): NOR/PSRAM/SRAM bank 1 (four sub-banks NE1-NE4).

Registers from the SVD (BCR1-4, BTR1-4, BWTR1-4, PCR2-4, SR2-4, PMEM2-4, PATT2-4, PIO4, ECCR2/3) with the reset
values of RM0090 36.5.6 (the SVD has BCRx = 0x30D0 and BTRx = 0xFFFFFFFF): BCR1 0x000030DB (MBKEN = 1, NOR),
BCR2-4 0x000030D2, BTRx / BWTRx 0x0FFFFFFF. NAND / PC Card registers (PCRx 0x18, SRx 0x40 with FEMPT = 1,
PMEMx / PATTx 0xFCFCFCFC) are plain registers: banks 2-4 (0x70000000-0x9FFFFFFF) are not mapped, so accesses there
bus-fault, and the FSMC interrupt (NAND/PC Card IRQ flags) is never raised.

* Sub-bank x (1-4) occupies the 64 MB window 0x60000000 + (x-1) x 0x04000000. It answers only while BCRx.MBKEN = 1
  and the FSMC clock is enabled (RCC_AHB3ENR.FSMCEN); otherwise the window is unmapped and an access is a bus fault
  (HardFault/BusFault through the core, as on silicon, where the FSMC returns an AHB error).
* External memories are board devices (chipem.devices.sram `ParallelSram`, board `devices: [{type: sram, bus: FSMC,
  size: 1M, width: 16}]`, `attach_memory(dev, bank=None)`: the first sub-bank without a memory, NE1 first). A memory
  smaller than the window is mirrored across it (its upper address lines are not connected), as an undersized chip
  would be. 8/16-bit memories whose data array is a native buffer are mapped straight into the core (full speed,
  executable); other ones go through per-access callbacks. BCRx.WREN = 0 makes the window read-only: a write is a
  bus fault (RM0090: AHB error). BCRx.MWID different from the memory's width logs one `warn` (data would be garbled on
  silicon; the model keeps the bytes). An enabled sub-bank with no memory reads 0 and ignores writes (floating
  data bus; one `warn`).
* Not modelled: access timing (ADDSET/ADDHLD/DATAST/BUSTURN/CLKDIV/DATLAT, extended mode BWTR): accesses take the
  CPU's normal memory time; synchronous burst (BURSTEN/CBURSTRW), wait signal (WAITEN/ASYNCWAIT), NOR flash command
  sets (MTYP = 10 is treated as a memory like SRAM), address/data multiplexing (MUXEN). The address/data/control
  pins (FSMC_A*, FSMC_D*, FSMC_NE*, NOE, NWE, NBL*) are not toggled; they keep their static table role.
* RCC: AHB3ENR.FSMCEN gate (register access while gated: one `warn`; the windows close), AHB3RSTR.FSMCRST resets the
  block (all windows close; BCR1 MBKEN = 1 then reopens NE1 if a memory is attached and the clock is on).
* STM32F1 high density (RM0008 ch. 21): the same bank 1 NOR/PSRAM controller (register map 21.6.9, reset values
  BCR1 0x30DB / BCR2-4 0x30D2 / BTRx, BWTRx 0x0FFFFFFF as in RM0008 21.5.6), control registers at 0xA0000000,
  gate RCC_AHBENR.FSMCEN (bit 8; read from the stm32f1.rcc GATES table), no reset bit.  The LQFP100 packages bond
  out NE1 only (no PG9/PG10/PG12 for NE2-NE4); the window still opens, the memory simply is not wired.
* STM32F2 (RM0033 31.5.6): `params.bcr_rw: 0x000FFF7F` (BCRx CPSIZE[18:16] and BCR1 WRAPMOD writable; the F2 SVD
  marks them read-only) and `params.bwtr_rw: 0x300FFFFF` (BWTRx: ACCMOD, BUSTURN[19:16], DATAST, ADDHLD, ADDSET;
  bits 27:20 reserved, keeping their reset value 1; the SVD has DATLAT/CLKDIV there and BUSTURN read-only).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs

MBKEN, MUXEN, WREN = 1, 2, 1 << 12
BANK1_BASE, WINDOW = 0x60000000, 0x04000000


@register("stm32.fsmc")
class FSMC(GatedBlock):
    model = "stm32.fsmc"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "FSMC")
        ov: dict[str, dict] = {}
        # `bcr_rw` / `bwtr_rw`: writable bits of BCRx / BWTRx where the part's RM differs from its SVD (STM32F2)
        bcr_rw, bwtr_rw = params.get("bcr_rw"), params.get("bwtr_rw")
        for x in range(1, 5):
            ov[f"BCR{x}"] = {"reset": 0x30DB if x == 1 else 0x30D2, "on_write": self._bcr_write(x)}
            ov[f"BTR{x}"] = {"reset": 0x0FFFFFFF}
            ov[f"BWTR{x}"] = {"reset": 0x0FFFFFFF}
            if bcr_rw is not None:
                ov[f"BCR{x}"]["ro_mask"] = ~int(bcr_rw) & 0xFFFFFFFF
            if bwtr_rw is not None:
                ov[f"BWTR{x}"]["ro_mask"] = ~int(bwtr_rw) & 0xFFFFFFFF
        for x in range(2, 5):
            ov[f"SR{x}"] = {"reset": 0x40}
        r = build_regs((name, "FSMC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.memories: dict[int, Any] = {}          # sub-bank (1-4) -> external memory device
        self.open: dict[int, tuple] = {}            # sub-bank -> (device, writable) currently mapped
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self._rcc is not None:
            self._rcc.gate_listeners.append(self._gate_changed)

    def attach_memory(self, dev: Any, bank: int | None = None) -> None:
        """Connect an external memory (chipem.devices.sram.ParallelSram) to sub-bank `bank` (NE1-NE4), or to the
        first free one."""
        if bank is None:
            bank = next((b for b in range(1, 5) if b not in self.memories), None)
            if bank is None:
                raise ValueError(f"{self.name}: all four NOR/SRAM sub-banks already have a memory")
        bank = int(bank)
        if not 1 <= bank <= 4:
            raise ValueError(f"{self.name}: sub-bank must be 1..4")
        if getattr(dev, "width", 16) not in (8, 16):
            raise ValueError(f"{self.name}: the FSMC NOR/SRAM data bus is 8 or 16 bits wide, not {dev.width}")
        self.memories[bank] = dev
        self._sync(bank)

    def reset(self) -> None:
        for b in list(getattr(self, "open", {})):
            self._close(b)
        self._warned = set()
        super().reset()
        if self.chip is not None:
            for b in range(1, 5):
                self._sync(b)

    # ------------------------------------------------------------------ helpers
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def window(self, bank: int) -> tuple[int, int]:
        return BANK1_BASE + (bank - 1) * WINDOW, WINDOW

    def _clock_on(self) -> bool:
        r = self._rcc
        return r is None or r.clock_enabled(self.gate)

    def _gate_changed(self, enr: str, old: int, new: int) -> None:
        g = getattr(self._rcc, "GATES", {}).get(self.gate)       # stm32f1.rcc: FSMC gate in AHBENR (RM0008 7.3.6)
        if enr == "AHB3ENR" or (g is not None and enr == g[0]):
            for b in range(1, 5):
                self._sync(b)

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _bcr_write(self, x: int):
        def wr(blk, reg, old, new):
            self._store(reg, new)
            if new & MUXEN:
                self._warn_once(f"mux{x}", f"{self.name}: BCR{x}.MUXEN (multiplexed address/data) is not modelled")
            if (new >> 4) & 3 not in (0, 1):
                self._warn_once(f"mwid{x}", f"{self.name}: BCR{x}.MWID {(new >> 4) & 3:02b} is reserved")
            if (new >> 2) & 3 == 3:
                self._warn_once(f"mtyp{x}", f"{self.name}: BCR{x}.MTYP = 11 is reserved")
            self._sync(x)
            return None
        return wr

    # ------------------------------------------------------------------ windows
    def _want(self, bank: int) -> tuple | None:
        bcr = self.get(f"BCR{bank}")
        if not bcr & MBKEN or not self._clock_on():
            return None
        return (self.memories.get(bank), bool(bcr & WREN))

    def _sync(self, bank: int) -> None:
        if self.chip is None:
            return
        want = self._want(bank)
        cur = self.open.get(bank)
        if want == cur:
            if want is not None:
                self._check_width(bank)
            return
        if cur is not None:
            self._close(bank)
        if want is not None:
            self._open(bank, *want)

    def _close(self, bank: int) -> None:
        if bank in self.open and self.chip is not None:
            self.chip.bus.unmap_window(f"{self.name}_NE{bank}")
        self.open.pop(bank, None)

    def _check_width(self, bank: int) -> None:
        dev = self.memories.get(bank)
        if dev is None:
            return
        mwid = 8 << ((self.get(f"BCR{bank}") >> 4) & 3)
        if mwid != dev.width:
            self._warn_once(f"w{bank}", f"{self.name}: BCR{bank}.MWID = {mwid} bits but the memory on NE{bank} is "
                            f"{dev.width} bits wide (data would be garbled on silicon)")

    def _open(self, bank: int, dev: Any, writable: bool) -> None:
        base, size = self.window(bank)
        name = f"{self.name}_NE{bank}"
        bus = self.chip.bus
        self.open[bank] = (dev, writable)
        if dev is None:
            # BCR1.MBKEN resets to 1, so an open window alone is no mistake: warn on the first access only
            def none() -> None:
                self._warn_once(f"none{bank}", f"{self.name}: NE{bank} accessed with no external memory attached: "
                                "reads return 0 (floating data bus), writes are lost")

            def rd(off: int, n: int) -> int:
                none()
                return 0
            nowr = self._no_write(bank, writable)

            def wr(off: int, n: int, v: int) -> None:
                none()
                nowr(off, n, v)
            bus.map_window(name, base, size, read=rd, write=wr)
            return
        self._check_width(bank)
        nb = dev.width // 8
        period = dev.size * nb
        mem = getattr(dev, "mem", None)
        fast = (mem is not None and getattr(mem, "itemsize", 0) == nb and period % bus.page == 0
                and WINDOW % period == 0)
        if fast:
            bus.map_window(name, base, size, buffer=mem, period=period, writable=writable)
        else:
            bus.map_window(name, base, size, read=self._slow_read(dev), write=self._slow_write(dev, bank, writable))

    def _no_write(self, bank: int, writable: bool):
        def wr(off: int, n: int, v: int) -> None:
            if not writable:
                self._write_fault(bank, off)
        return wr

    def _write_fault(self, bank: int, off: int) -> None:
        self._warn_once(f"wren{bank}", f"{self.name}: write to NE{bank} with BCR{bank}.WREN = 0 (AHB error on "
                        "silicon); ignored by the slow access path")

    def _slow_read(self, dev: Any):
        nb = dev.width // 8

        def rd(off: int, n: int) -> int:
            v = 0
            for i in range(n):
                a = off + i
                word = dev.read(a // nb)
                v |= ((word >> (8 * (a % nb))) & 0xFF) << (8 * i)
            return v
        return rd

    def _slow_write(self, dev: Any, bank: int, writable: bool):
        nb = dev.width // 8

        def wr(off: int, n: int, value: int) -> None:
            if not writable:
                self._write_fault(bank, off)
                return
            for i in range(n):
                a = off + i
                sh = 8 * (a % nb)
                dev.write(a // nb, ((value >> (8 * i)) & 0xFF) << sh, 0xFF << sh)
        return wr
