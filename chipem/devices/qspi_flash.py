"""Board-level quad-SPI NOR flash (Winbond W25Qxx-style command set) for a quad-SPI controller (e.g. stm32.quadspi).

Extends chipem.devices.spi_flash.SpiNorFlash: the single-line commands (0x9F RDID, 0x90 REMS, 0xAB RES, 0x05 RDSR,
0x01 WRSR, 0x06 WREN, 0x04 WRDI, 0x03 READ, 0x0B FAST_READ, 0x02 PP, 0x20/0x52/0xD8 erase, 0x60/0xC7 chip erase, 0xB9
deep power-down) keep their meaning and data handling; this class adds what a quad-SPI controller needs:

* Dual/quad commands (W25Q128JV data sheet, "Instructions"): 0x3B Fast Read Dual Output (1-1-2, 8 dummy clocks),
  0xBB Fast Read Dual I/O (1-2-2, M7-0 + 0 dummy = 4 clocks after the address), 0x6B Fast Read Quad Output (1-1-4,
  8 dummy), 0xEB Fast Read Quad I/O (1-4-4, M7-0 + 4 dummy = 6 clocks), 0x32 Quad Page Program (1-1-4); status
  register 2: 0x35 RDSR2, 0x31 WRSR2, 0x01 with two data bytes writes SR1 and SR2; QE = SR2 bit 1 (`qe: sr2`, default;
  `qe: none` for parts without a QE bit, e.g. Micron N25Q).  Quad commands with QE = 0 are not executed (IO2/IO3 are
  /WP and /HOLD then): reads return 0xFF, programs are ignored (one `warn`).
* Phase-level transactions (`command(...)`): the controller passes the instruction, address, alternate bytes, number
  of dummy clocks and the line count of each phase.  The device checks them against its command table: a wrong line
  count for a phase (e.g. 0xEB with a 1-line address) is not decoded by real silicon -> the command is ignored (reads
  0xFF, `warn` once per mismatch); a different number of clocks between address and data than the command needs
  shifts the read data by (difference x data lines) bits, as on the wires (missing leading bits read 1: the lines are
  pulled up).  In continuous-read (M5-4 = 10) the device would skip the next instruction: not modelled (warn).
* Busy time: program/erase/status writes set WIP (SR1 bit 0) for t_pp_us / t_se_us / t_be32_us / t_be64_us /
  t_ce_us / t_w_us (typical W25Q128JV values 400 us / 45 ms / 120 ms / 150 ms / 40 s / 10 ms) starting when chip select
  is released; while busy only RDSR/RDSR2 are executed (others ignored, `warn` once); WEL clears when the operation
  ends.  The data change is applied at release (reads are blocked until the end anyway).
* `stream(addr, n)` (data bytes for reads), `status_at(t_ns)` (SR1 at a time, for automatic status polling:
  piecewise constant, one change at `busy_until_ns`), `can_map(...)` (a memory-mapped read configuration that returns
  plain array data: the controller may then map `mem` straight into the core).

Times are in ns of the chip that owns the controller (`now_ns` arguments); the device keeps no scheduler events.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .spi_flash import SpiNorFlash, _parse_size

# command -> (instruction lines, address lines (0 = no address), data lines, clocks between address and data,
#             kind) ; kind: r = read data, w = write data, s = status read, n = no data
CMDS: dict[int, tuple[int, int, int, int, str]] = {
    0x03: (1, 1, 1, 0, "r"), 0x0B: (1, 1, 1, 8, "r"), 0x3B: (1, 1, 2, 8, "r"), 0xBB: (1, 2, 2, 4, "r"),
    0x6B: (1, 1, 4, 8, "r"), 0xEB: (1, 4, 4, 6, "r"),
    0x02: (1, 1, 1, 0, "w"), 0x32: (1, 1, 4, 0, "w"),
    0x05: (1, 0, 1, 0, "s"), 0x35: (1, 0, 1, 0, "s"), 0x9F: (1, 0, 1, 0, "s"), 0x90: (1, 1, 1, 0, "s"),
    0xAB: (1, 0, 1, 24, "s"),
    0x01: (1, 0, 1, 0, "w"), 0x31: (1, 0, 1, 0, "w"),
    0x06: (1, 0, 0, 0, "n"), 0x04: (1, 0, 0, 0, "n"), 0x20: (1, 1, 0, 0, "n"), 0x52: (1, 1, 0, 0, "n"),
    0xD8: (1, 1, 0, 0, "n"), 0x60: (1, 0, 0, 0, "n"), 0xC7: (1, 0, 0, 0, "n"), 0xB9: (1, 0, 0, 0, "n"),
    0x66: (1, 0, 0, 0, "n"), 0x99: (1, 0, 0, 0, "n"),
}
QUAD = {0x6B, 0xEB, 0x32}
_TIMES = {"t_pp_us": 400.0, "t_se_us": 45_000.0, "t_be32_us": 120_000.0, "t_be64_us": 150_000.0,
          "t_ce_us": 40_000_000.0, "t_w_us": 10_000.0}


class QspiNorFlash(SpiNorFlash):
    def __init__(self, jedec: int = 0xEF4018, size: int | str | None = None, *, image: str | Path | None = None,
                 name: str = "qspi_flash", qe: str = "sr2", log: Any = None, **times: Any):
        if size is None:
            size = {0xEF4018: 16 << 20, 0xEF4017: 8 << 20, 0xEF4016: 4 << 20, 0xEF4019: 32 << 20,
                    0x20BA18: 16 << 20}.get(int(jedec) & 0xFFFFFF)
        super().__init__(jedec, size, image=image, name=name)
        self.qe_mode = str(qe or "sr2").lower()
        self.sr2 = 0
        self.t = {k: float(times.get(k, v)) * 1e3 for k, v in _TIMES.items()}     # ns
        self.busy_until_ns = 0.0
        self.log = log                      # callable(kind, **fields) or None
        self.emit_fn = None                 # set by the controller: emit(kind, **fields) on the chip log
        self._warned: set[str] = set()
        self.commands = 0
        self.history: list[tuple[str, int]] = []      # (command name, address) of program/erase/status writes

    # ------------------------------------------------------------------ helpers
    @property
    def qe(self) -> bool:
        return self.qe_mode == "none" or bool(self.sr2 & 0x02)

    def busy(self, now_ns: float) -> bool:
        return now_ns < self.busy_until_ns

    def status_at(self, t_ns: float) -> int:
        """SR1 at time t: WIP (bit 0) while an operation runs; WEL (bit 1) until it ends."""
        if t_ns < self.busy_until_ns:
            return self.sr | 0x03
        if self.busy_until_ns:
            return self.sr                     # the operation ended: WEL cleared with WIP
        return self.sr | (0x02 if self.wel else 0)

    def _warn(self, key: str, msg: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        if self.emit_fn is not None:
            self.emit_fn("warn", msg=f"{self.name}: {msg}")

    def _finish_busy(self, now_ns: float) -> None:
        if self.busy_until_ns and now_ns >= self.busy_until_ns:
            self.busy_until_ns = 0.0
            self.wel = False

    def stream(self, addr: int, n: int) -> bytes:
        a = addr % self.size
        if a + n <= self.size:
            return bytes(self.mem[a:a + n])
        out = bytearray()
        while len(out) < n:
            k = min(n - len(out), self.size - a)
            out += self.mem[a:a + k]
            a = 0
        return bytes(out)

    def check(self, instr: int | None, lines: tuple[int, int, int], gap_clocks: int, adbytes: int,
              now_ns: float) -> tuple[str, int]:
        """Decode a command: ("ok" | "ignored" | "unknown", shift in bits of the read data)."""
        if instr is None:
            self._warn("noinstr", "command without instruction phase: the flash only decodes commands that start "
                       "with an instruction (continuous-read mode is not modelled); ignored")
            return "ignored", 0
        spec = CMDS.get(instr)
        if spec is None:
            self._warn(f"unk{instr:02x}", f"unknown command 0x{instr:02X}: ignored (lines float: reads 0xFF)")
            return "unknown", 0
        il, al, dl, gap, kind = spec
        self._finish_busy(now_ns)
        if self.busy(now_ns) and instr not in (0x05, 0x35):
            self._warn("busy", f"command 0x{instr:02X} while busy (WIP = 1): ignored, as on silicon")
            return "ignored", 0
        if self.powered_down and instr != 0xAB:
            return "ignored", 0
        il_, al_, dl_ = lines
        bad = []
        if il_ != il:
            bad.append(f"instruction on {il_} line(s), needs {il}")
        if al and al_ != al:
            bad.append(f"address on {al_} line(s), needs {al}")
        if al and adbytes not in (0, 3):
            bad.append(f"{adbytes}-byte address, the device uses 3 bytes")
        if kind in "rws" and dl and dl_ and dl_ != dl and instr not in (0x01, 0x31):
            bad.append(f"data on {dl_} line(s), needs {dl}")
        if bad:
            self._warn(f"lines{instr:02x}{lines}", f"command 0x{instr:02X} with " + ", ".join(bad) + ": not "
                       "decoded by the device (reads 0xFF, writes ignored)")
            return "ignored", 0
        if instr in QUAD and not self.qe:
            self._warn("qe", f"quad command 0x{instr:02X} with QE = 0 (status register 2 bit 1): IO2/IO3 are /WP and "
                       "/HOLD; not executed (reads 0xFF)")
            return "ignored", 0
        shift = 0
        if kind == "r" and gap_clocks != gap:
            shift = (gap_clocks - gap) * dl
            self._warn(f"gap{instr:02x}{gap_clocks}", f"command 0x{instr:02X} with {gap_clocks} clocks between "
                       f"address and data (alternate bytes + dummy cycles), the device needs {gap}: the data is "
                       f"shifted by {shift} bits")
        return "ok", shift

    def can_map(self, instr: int | None, lines: tuple[int, int, int], gap_clocks: int, adbytes: int,
                now_ns: float) -> bool:
        spec = CMDS.get(instr) if instr is not None else None
        if spec is None or spec[4] != "r":
            return False
        st, shift = self.check(instr, lines, gap_clocks, adbytes, now_ns)
        return st == "ok" and shift == 0

    # ------------------------------------------------------------------ transactions
    def read_data(self, instr: int, addr: int, n: int, shift: int, now_ns: float) -> bytes:
        """Bytes clocked out for a read / status command (after `check` returned ok)."""
        if instr in (0x05, 0x35):
            v = self.status_at(now_ns) if instr == 0x05 else self.sr2
            return bytes([v]) * n
        if instr == 0x9F:
            ident = self.jedec.to_bytes(3, "big")
            return bytes(ident[k] if k < 3 else 0 for k in range(n))
        if instr == 0x90:
            m, d = (self.jedec >> 16) & 0xFF, self.jedec & 0xFF
            return bytes((m, d)[k % 2] for k in range(n))
        if instr == 0xAB:
            return bytes([self.jedec & 0xFF]) * n
        if shift == 0:
            return self.stream(addr, n)
        # wrong number of clocks before the data: the controller samples `shift` bits early (<0) or late (>0)
        extra = (abs(shift) + 7) // 8 + 1
        raw = self.stream(addr, n + extra)
        val = int.from_bytes(raw, "big")
        nbits = 8 * len(raw)
        if shift > 0:
            val = (val << shift) & ((1 << nbits) - 1)
        else:
            val = (val >> -shift) | (((1 << -shift) - 1) << (nbits + shift))       # leading bits: pulled up
        return val.to_bytes(len(raw), "big")[:n]

    def execute(self, instr: int, addr: int | None, data_out: bytes, now_ns: float) -> None:
        """A command without read data ends (chip select released at now_ns): WREN/WRDI, erase, program (data_out =
        the bytes the controller sent), status writes, power-down."""
        self.commands += 1
        if instr == 0x06:
            self.wel = True
            return
        if instr == 0x04:
            self.wel = False
            return
        if instr == 0xB9:
            self.powered_down = True
            return
        if instr in (0x66, 0x99):
            return
        if not self.wel:
            if instr in (0x02, 0x32, 0x20, 0x52, 0xD8, 0x60, 0xC7, 0x01, 0x31):
                self._warn(f"wel{instr:02x}", f"command 0x{instr:02X} without WREN (WEL = 0): ignored")
            return
        a = (addr or 0) % self.size
        busy = 0.0
        if instr in (0x02, 0x32):
            for b in data_out:
                self.mem[a] &= b
                a = (a & ~0xFF) | ((a + 1) & 0xFF)
            self.ops.append(("program", (addr or 0) % self.size))
            busy = self.t["t_pp_us"]
        elif instr in (0x20, 0x52, 0xD8):
            span = {0x20: 0x1000, 0x52: 0x8000, 0xD8: 0x10000}[instr]
            a -= a % span
            self.mem[a:a + span] = b"\xff" * span
            self.ops.append((f"erase{span // 1024}k", a))
            busy = self.t[{0x20: "t_se_us", 0x52: "t_be32_us", 0xD8: "t_be64_us"}[instr]]
        elif instr in (0x60, 0xC7):
            self.mem[:] = b"\xff" * self.size
            self.ops.append(("chip_erase", 0))
            busy = self.t["t_ce_us"]
        elif instr == 0x01 and data_out:
            self.sr = data_out[0] & 0x9C
            if len(data_out) > 1:
                self.sr2 = data_out[1] & 0x7B
            self.ops.append(("write_status", 0))
            busy = self.t["t_w_us"]
        elif instr == 0x31 and data_out:
            self.sr2 = data_out[0] & 0x7B
            self.ops.append(("write_status2", 0))
            busy = self.t["t_w_us"]
        else:
            return
        self.history.append((f"0x{instr:02X}", a))
        if busy > 0:
            self.busy_until_ns = now_ns + busy
        else:
            self.wel = False


def from_options(opts: dict[str, Any], name: str = "qspi_flash") -> QspiNorFlash:
    """Board / --spi-flash options -> device (jedec, size, file, qe, t_*_us)."""
    times = {k: float(v) for k, v in opts.items() if k in _TIMES}
    return QspiNorFlash(int(str(opts.get("jedec", "0xEF4018")), 0), opts.get("size"), image=opts.get("file"),
                        name=name, qe=str(opts.get("qe", "sr2")), **times)


__all__ = ["QspiNorFlash", "from_options", "CMDS", "_parse_size"]
