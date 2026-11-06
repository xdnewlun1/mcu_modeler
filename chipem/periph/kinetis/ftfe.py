"""Kinetis FTFE flash memory controller (K22 RM ch. 29), synchronous command model.

* FSTAT: CCIF (bit 7) reads 1 when idle; writing 1 to CCIF launches the command in FCCOB0..B and it completes
  before the write returns (so CCIF stays 1). ACCERR (5), FPVIOL (4), RDCOLERR (6) are w1c; MGSTAT0 (0) is
  read-only. A launch with ACCERR/FPVIOL still set is ignored.
* FSEC/FOPT/FPROT0-3/FEPROT/FDPROT are loaded at reset from the flash configuration field (0x400..0x40F) if
  the loaded firmware covers it, else FSEC=0xFE (unsecured), the rest 0xFF.
* FCCOB register order follows the SVD (byte-swapped within each word): FCCOB0 = command, FCCOB1..3 = 24-bit
  address (bit 23 selects FlexNVM at 0x10000000), FCCOB4..B = data; data byte k of a phrase lives at block
  offset 8+k (FCCOB7, 6, 5, 4, B, A, 9, 8).
* Commands: RD1BLK 0x00, RD1SEC 0x01, PGMCHK 0x02, RDRSRC 0x03, PGM8 0x07 (program phrase; flash can only go
  1->0, so data is ANDed in), ERSBLK 0x08, ERSSCR 0x09 (4 KB sector -> 0xFF), PGMSEC 0x0B (no-op), RD1ALL 0x40,
  RDONCE 0x41, PGMONCE 0x43, ERSALL 0x44, VFYKEY 0x45, PGMPART 0x80, SETRAM 0x81 (acknowledged). Unknown
  commands set ACCERR. Every command logs an `info` event; the FTFE IRQ line is FCNFG.CCIE & FSTAT.CCIF.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, irq_number

CCIF, RDCOLERR, ACCERR, FPVIOL, MGSTAT0 = 0x80, 0x40, 0x20, 0x10, 0x01
SECTOR = 0x1000
BLOCK = 0x80000
FLEXNVM_BASE = 0x10000000
CMD_NAMES = {0x00: "RD1BLK", 0x01: "RD1SEC", 0x02: "PGMCHK", 0x03: "RDRSRC", 0x07: "PGM8", 0x08: "ERSBLK",
             0x09: "ERSSCR", 0x0B: "PGMSEC", 0x40: "RD1ALL", 0x41: "RDONCE", 0x43: "PGMONCE", 0x44: "ERSALL",
             0x45: "VFYKEY", 0x80: "PGMPART", 0x81: "SETRAM"}
CFG_FIELD = (("FPROT3", 0x8), ("FPROT2", 0x9), ("FPROT1", 0xA), ("FPROT0", 0xB), ("FSEC", 0xC), ("FOPT", 0xD),
             ("FEPROT", 0xE), ("FDPROT", 0xF))


@register("kinetis.ftfe")
class FTFE(GatedBlock):
    gate = "FTFE"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"FSTAT": {"reset": CCIF, "ro_mask": MGSTAT0, "w1c_mask": RDCOLERR | ACCERR | FPVIOL,
                        "on_write": self._fstat_write},
              "FCNFG": {"reset": 0x02, "on_write": self._fcnfg_write}}     # RAMRDY: FlexRAM available as RAM
        r = build_regs((name, "FTFE"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.config_addr = int(params.get("config_addr", 0x400))
        self.commands: list[tuple[int, int]] = []          # (command, address) history
        self._irqn: int | None = None
        self._line = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "FTFE", "FTFL", "FTFA")

    def reset(self) -> None:
        if self._line and self.chip is not None:
            self._line = False
            self.irq(self._irqn, False)
        super().reset()
        cfg = self._config_field()
        for reg, off in CFG_FIELD:
            if reg in self.regs:
                if cfg is not None:
                    self.set(reg, cfg[off])
                else:
                    self.set(reg, 0xFE if reg == "FSEC" else 0xFF)

    def _config_field(self) -> bytes | None:
        chip = self.chip
        fw = getattr(chip, "firmware", None) if chip is not None else None
        if fw is None:
            return None
        try:
            if hasattr(fw, "contains") and not fw.contains(self.config_addr, 16):
                return None
            return bytes(chip.bus.read_bytes(self.config_addr, 16))
        except Exception:
            return None

    # ------------------------------------------------------------ helpers
    def fccob(self, n: int) -> int:
        return self.get(f"FCCOB{n:X}")

    def _addr(self) -> int:
        a = (self.fccob(1) << 16) | (self.fccob(2) << 8) | self.fccob(3)
        if a & 0x800000:
            return FLEXNVM_BASE + (a & 0x7FFFFF)
        return a

    def _region(self, addr: int):
        from ...bus import MemoryRegion
        r = self.chip.bus.lookup(addr)
        return r if isinstance(r, MemoryRegion) and r.kind in ("flash", "flexnvm", "rom") else None

    def _erase(self, start: int, n: int) -> None:
        self.chip.bus.write_bytes(start, b"\xff" * n)
        self._flush()

    def _flush(self) -> None:
        core = self.chip.core
        f = getattr(core, "flush_code_cache", None)
        if f is not None and not getattr(core, "running", False):
            f()                 # inside a running slice Unicorn's own write path invalidates translated code

    # ------------------------------------------------------------ FSTAT / commands
    def _fstat_write(self, blk, reg, old, new):
        launch = bool(new & CCIF)
        st = (new & (RDCOLERR | ACCERR | FPVIOL)) | (old & MGSTAT0) | CCIF
        if launch:
            if st & (ACCERR | FPVIOL):
                self.emit("warn", block=self.name, msg="FTFE command launched with ACCERR/FPVIOL set: ignored")
            else:
                st = self._execute(st & ~MGSTAT0)
        self._store(reg, st)
        self._update_line()
        return st

    def _execute(self, st: int) -> int:
        cmd = self.fccob(0)
        addr = self._addr()
        name = CMD_NAMES.get(cmd, f"0x{cmd:02X}")
        self.commands.append((cmd, addr))
        detail = ""
        if cmd in (0x00, 0x01, 0x02):                 # read-1s block / section, program check
            r = self._region(addr)
            if r is None:
                st |= ACCERR
            else:
                if cmd == 0x00:
                    start = r.base + ((addr - r.base) // BLOCK) * BLOCK
                    n = min(BLOCK, r.end - start)
                elif cmd == 0x01:
                    start, n = addr, ((self.fccob(4) << 8) | self.fccob(5)) * 16
                else:
                    start, n = addr, 4
                if cmd != 0x02:
                    data = self.chip.bus.read_bytes(start, max(0, min(n, r.end - start)))
                    if data.count(0xFF) != len(data):
                        st |= MGSTAT0
                detail = f" 0x{addr:08x}"
        elif cmd == 0x03:                              # read resource
            for k in range(4, 12):
                self.set(f"FCCOB{k:X}", 0xFF)
            detail = f" 0x{addr:06x}"
        elif cmd == 0x07:                              # program phrase
            r = self._region(addr)
            if r is None or addr % 8 or addr + 8 > r.end:
                st |= ACCERR
            else:
                data = bytes(self.mem[8:16])
                old = self.chip.bus.read_bytes(addr, 8)
                self.chip.bus.write_bytes(addr, bytes(a & b for a, b in zip(old, data)))
                self._flush()
                if any((~a & b) & 0xFF for a, b in zip(old, data)):
                    self.emit("warn", block=self.name, msg=f"FTFE program 0x{addr:08x}: target not erased "
                              "(bits can only be programmed 1->0)")
                detail = f" 0x{addr:08x} = {data.hex()}"
        elif cmd == 0x08:                              # erase block
            r = self._region(addr)
            if r is None:
                st |= ACCERR
            else:
                start = r.base + ((addr - r.base) // BLOCK) * BLOCK
                self._erase(start, min(BLOCK, r.end - start))
                detail = f" 0x{start:08x}"
        elif cmd == 0x09:                              # erase sector
            r = self._region(addr)
            if r is None or addr % 16:
                st |= ACCERR
            else:
                start = addr - addr % SECTOR
                self._erase(start, min(SECTOR, r.end - start))
                detail = f" 0x{start:08x}"
        elif cmd == 0x44:                              # erase all blocks
            from ...bus import MemoryRegion
            for r in self.chip.bus.regions.values():
                if isinstance(r, MemoryRegion) and r.kind in ("flash", "flexnvm"):
                    self._erase(r.base, r.size)
        elif cmd == 0x41:                              # read once
            for k in range(4, 12):
                self.set(f"FCCOB{k:X}", 0xFF)
        elif cmd == 0x40:                              # read 1s all blocks
            pass
        elif cmd in (0x0B, 0x43, 0x45, 0x80, 0x81):
            pass
        else:
            st |= ACCERR
            name = f"unknown command 0x{cmd:02X}"
        self.emit("info", block=self.name, msg=f"FTFE {name}{detail}" + (" → ACCERR" if st & ACCERR else ""),
                  command=cmd, addr=addr)
        return st

    def _update_line(self) -> None:
        """Command-complete IRQ line = FCNFG.CCIE & FSTAT.CCIF (level; commands complete synchronously, so it
        stays asserted until the handler clears CCIE, as on silicon)."""
        line = bool(self.get("FCNFG") & 0x80 and self.get("FSTAT") & CCIF)
        if line != self._line and self._irqn is not None and self.chip is not None:
            self._line = line
            self.irq(self._irqn, line)

    def _fcnfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update_line()
        return None
