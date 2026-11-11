"""STM32F42xxx/43xxx FMC (flexible memory controller, RM0090 ch. 37): the FSMC NOR/PSRAM/SRAM bank 1 (same IP as
stm32.fsmc, reused by subclassing) plus the SDRAM controller (RM0090 37.7) for SDRAM bank 1 (0xC0000000, SDNE0 /
SDCKE0, FMC_SDCR1 / SDTR1) and bank 2 (0xD0000000, SDNE1 / SDCKE1, FMC_SDCR2 / SDTR2; HADDR[28] selects the bank,
37.4.3 Table 258).

NOR/PSRAM/SRAM (37.5): everything of chipem/periph/stm32/fsmc.py (NE1-NE4 windows at 0x60000000, MBKEN, WREN, MWID,
board `sram` devices with `bus: FMC`), with the RCC gate AHB3ENR.FMCEN.  SVD quirk: STM32F427.svd puts BWTR3/BWTR4 at
0x104/0x10C (on top of BWTR1/BWTR2, as does RM0090 Rev 22 Table 297); the register text (37.5.6, "0x104 + 8 * (x -
1)") gives 0x114/0x11C, which this model adds.  NAND / PC Card (37.6) are plain registers as in stm32.fsmc.

SDRAM controller (37.7.3 - 37.7.5), external memories = board `sdram` devices (chipem/devices/sdram.py, `devices:
[{type: sdram, bus: FMC, bank: 2, ...}]`, `attach_sdram(dev, bank)`):
* FMC_SDCR1/2 (reset 0x2D0): NC (8-11 column bits), NR (11-13 row bits), MWID (8/16/32), NB (2/4 internal banks), CAS,
  WP, and in SDCR1 only SDCLK (00 disabled, 10 HCLK/2, 11 HCLK/3), RBURST, RPIPE (read-only in SDCR2).  The bank
  window is NB x 2^NR x 2^NC x MWID/8 bytes from the bank base, linear in bank|row|column order (Tables 259-262); an
  access above it (reserved address range) is an AHB error (bus fault, 37.3).  WP = 1: the window is read-only and a
  write is an AHB error (37.3; fast path: the core faults; slow path: the write is dropped with one `warn`).
* FMC_SDCMR (37.7.5): writing MODE issues the command to the target banks CTB1/CTB2 (none: `warn`, nothing sent):
  001 clock configuration enable (SDCKE high), 010 PALL, 011 NRFS+1 auto-refresh, 100 load mode register MRD, 101
  self-refresh (MODESx = 01), 110 power-down (MODESx = 10), 000 normal (exit self-refresh / power-down); 111 reserved
  (`warn`).  The device enforces the JEDEC power-up sequence (37.7.3 steps 3-7: CKE, >= 100 us, PALL, auto-refresh,
  load mode register): until it is complete the bank's data bus floats (reads 0, writes are lost, one `warn`).  The
  load mode register value is checked against SDCR.CAS and the burst length 1 required by step 7b (`warn` on a
  mismatch; the data path is not garbled).  Commands need SDCR1.SDCLK = 10/11 (otherwise `warn`, not issued) and,
  like silicon, the FMC_SDCLK, FMC_SDNEx and FMC_SDCKEx pins muxed to the FMC (an SDRAM that sees no clock or chip
  select ignores the command: `warn`; `params.check_pins: false` disables the check).
* FMC_SDSR.BUSY is 1 while a command runs: clock enable 1, PALL TRP, auto-refresh (NRFS+1) x TRC, load mode TMRD,
  self-refresh TRP + TRAS (BUSY stays high for TRAS, 37.7.4), power-down 1, exit self-refresh TXSR SDCLK cycles (TRP/
  TRC from SDTR1; TMRD/TRAS/TXSR from SDTR1 when both banks are targeted, 37.7.5 note).  A command written while BUSY
  = 1 is ignored with one `warn`.  MODES1/MODES2 report normal / self-refresh / power-down; the first access to a
  bank in self-refresh or power-down exits it (37.7.4: "After the memory access, the selected device remains in
  Normal mode").
* FMC_SDRTR: COUNT (refresh period in SDCLK cycles; 0 = no refresh; < 41 `warn`; equal to TWR+TRP+TRC+TRCD+4 `warn`,
  both RM0090 37.7.5 notes), REIE, CRE (write 1: clear SDSR.RE; reads 0).  The refresh timer runs while the FMC clock
  and SDCLK run and COUNT != 0: every COUNT SDCLK cycles one auto-refresh per initialized device.  RE (refresh error,
  37.7.3 "SDRAM controller refresh cycle") is set when a refresh request comes before the previous one was served:
  with zero-time memory accesses that only happens when COUNT < TRP + TRC (PALL + auto-refresh do not fit), one
  COUNT period after the timer starts; FMC IRQ (48, "FMC") = RE & REIE.
* Data retention: the device loses its content `refresh_ms` (default 64 ms) after its refresh became insufficient
  (no COUNT, rows x COUNT period > refresh_ms, SDCLK disabled, FMC clock gated off, FMC/chip reset, Stop mode
  (`clock_stop`) without self-refresh).  A system reset closes the banks and drops SDCKE: firmware must run the
  init sequence again (37.7.3: "the SDRAM device must be first reinitialized after reset"); the data survives if
  that happens within the retention time.
* Fast path: a device whose geometry equals the SDCR configuration (banks, rows, columns, width) is mapped straight
  into the core (Bus.map_window with the device's buffer; executable); other geometries go through callbacks that
  decompose the address into bank/row/column (upper address lines not connected: modulo the device; one `warn`).
Not modelled: access timing (CAS latency, TRCD, RBURST read FIFO, RPIPE: accesses take the CPU's memory time), the
SDRAM pin waveforms (address/data/control pins keep their static role), extended mode register of mobile SDRAM (37.7.3
step 9), SYSCFG_MEMRM.SWP_FMC bank swapping, NAND/PC Card (as stm32.fsmc).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock, Reg
from ..registry import register
from ._util import irq_number, rcc
from .fsmc import FSMC

SDRAM_BASE = {1: 0xC0000000, 2: 0xD0000000}
SDRAM_WINDOW = 0x10000000
MODE_NAMES = ("normal", "clock configuration enable", "PALL", "auto-refresh", "load mode register", "self-refresh",
              "power-down", "reserved")


def _sdtr_field(v: int, lsb: int) -> int:
    return ((v >> lsb) & 0xF) + 1             # SDTR fields: 0000 = 1 cycle ... 1111 = 16 cycles


@register("stm32.fmc")
class FMC(FSMC):
    model = "stm32.fmc"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params.setdefault("gate", "FMC")
        super().__init__(name, base, size, regs, params=params, log=log)
        # BWTR3/BWTR4 at their RM0090 37.5.6 offsets (the SVD overlaps them with BWTR1/BWTR2)
        for x, off in ((3, 0x114), (4, 0x11C)):
            if f"BWTR{x}" not in self.regs and off + 4 <= size:
                self.add_reg(Reg(f"BWTR{x}", off, 4, reset=0x0FFFFFFF, ro_mask=0xC0000000))
        hooks = {
            "SDCR1": {"ro_mask": 0xFFFF8000, "on_write": self._sdcr_write},
            "SDCR2": {"ro_mask": 0xFFFF8000 | 0x6C00, "on_write": self._sdcr_write},   # RPIPE/SDCLK: SDCR1 only
            "SDTR1": {"ro_mask": 0xF0000000}, "SDTR2": {"ro_mask": 0xF0000000},
            "SDCMR": {"ro_mask": 0xFFC00000, "on_write": self._sdcmr_write, "on_read": self._sdcmr_read},
            "SDRTR": {"ro_mask": 0xFFFF8000, "on_write": self._sdrtr_write, "on_read": lambda b, r: b.get("SDRTR") & ~1},
            "SDSR": {"ro_mask": 0xFFFFFFFF, "on_read": self._sdsr_read},
        }
        for rn, kw in hooks.items():
            r = self.regs.get(rn)
            if r is None:
                continue
            for k, v in kw.items():
                setattr(r, k, v)
        self.check_pins = bool(params.get("check_pins", True))
        self.sdram: dict[int, Any] = {}            # SDRAM bank (1/2) -> device
        self.sd_open: dict[int, tuple] = {}        # bank -> window key currently mapped
        self.modes = {1: 0, 2: 0}                  # MODES1/MODES2: 0 normal, 1 self-refresh, 2 power-down
        self.commands: list[tuple[float, str, tuple[int, ...]]] = []    # (t_ns, command, banks) history
        self._busy_until = -1.0
        self._re = False
        self._re_ev = None
        self._irq_n: int | None = None
        self._stopped = False
        self._sd_sync_ev = None
        self._refresh_key: tuple | None = None

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq_n = irq_number(chip, "FMC", "FSMC")
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._clock_changed)
        for dev in self.sdram.values():
            self._wire(dev)

    def attach_sdram(self, dev: Any, bank: int | None = None) -> None:
        """Connect an SDRAM device (chipem.devices.sdram.Sdram) to SDRAM bank 1 (SDNE0, 0xC0000000) or 2 (SDNE1,
        0xD0000000); default: the first free bank."""
        if bank is None:
            bank = next((b for b in (1, 2) if b not in self.sdram), None)
            if bank is None:
                raise ValueError(f"{self.name}: both SDRAM banks already have a device")
        bank = int(bank)
        if bank not in (1, 2):
            raise ValueError(f"{self.name}: SDRAM bank must be 1 or 2")
        if bank in self.sdram:
            raise ValueError(f"{self.name}: SDRAM bank {bank} already has a device")
        self.sdram[bank] = dev
        self._wire(dev)
        self._refresh_update()
        self._sd_sync(bank)

    def _wire(self, dev: Any) -> None:
        chip = self.chip
        if chip is not None:
            dev.schedule = lambda t, cb: chip.sched.at_ns(t, cb)
            dev.cancel = chip.sched.cancel
            dev.now = chip.now_ns
            dev.lost = self._data_lost

    def _data_lost(self, msg: str) -> None:
        self.emit("warn", block=self.name, msg=f"{self.name}: SDRAM {msg}")
        f = getattr(self.chip.core, "flush_code_cache", None) if self.chip is not None else None
        if f is not None:
            f()

    def reset(self) -> None:
        for b in list(getattr(self, "sd_open", {})):
            self._sd_close(b)
        ev = getattr(self, "_re_ev", None)
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)
        self._re_ev = None
        if getattr(self, "_sd_sync_ev", None) is not None and self.chip is not None:
            self.chip.sched.cancel(self._sd_sync_ev)
        self._sd_sync_ev = None
        super().reset()
        self.modes = {1: 0, 2: 0}
        self._busy_until = -1.0
        self._re = False
        self._refresh_key = None
        for dev in getattr(self, "sdram", {}).values():      # SDCKE low: the device needs a new init sequence
            dev.clock_disable()
        if self.chip is not None:
            self._update_irq()
            self._refresh_update()
            for b in (1, 2):
                self._sd_sync_req(b)

    def clock_stop(self, stopped: bool, mode: str) -> None:
        """Stop mode (PWR): HCLK stops, so the controller no longer refreshes (self-refresh keeps the data)."""
        self._stopped = bool(stopped)
        self._refresh_update()

    def _clock_changed(self) -> None:
        self._refresh_update()

    def _gate_changed(self, enr: str, old: int, new: int) -> None:
        super()._gate_changed(enr, old, new)
        if enr == "AHB3ENR":
            self._refresh_update()
            for b in (1, 2):
                self._sd_sync_req(b)

    # ------------------------------------------------------------------ helpers
    def _sdclk_div(self) -> int:
        return {2: 2, 3: 3}.get((self.get("SDCR1") >> 10) & 3, 0)

    def _sdclk_ns(self) -> float:
        div = self._sdclk_div()
        hz = float(self.chip.clock.hz) if self.chip is not None else 0.0
        return div * 1e9 / hz if div and hz > 0 else 0.0

    def geometry(self, bank: int) -> tuple[int, int, int, int]:
        """(internal banks, rows, columns, width bits) programmed in FMC_SDCRx."""
        v = self.get(f"SDCR{bank}")
        nc, nr, mwid, nb = v & 3, (v >> 2) & 3, (v >> 4) & 3, (v >> 6) & 1
        return (4 if nb else 2), 1 << (11 + min(nr, 2)), 1 << (8 + nc), 8 << min(mwid, 2)

    def bank_size(self, bank: int) -> int:
        b, r, c, w = self.geometry(bank)
        return b * r * c * w // 8

    def _sd_now(self) -> float:
        return self.chip.now_ns() if self.chip is not None else 0.0

    def busy(self) -> bool:
        return self._sd_now() < self._busy_until

    def _update_irq(self) -> None:
        if self._irq_n is not None and self.chip is not None:
            self.irq(self._irq_n, bool(self._re and self.get("SDRTR") & (1 << 14)))

    def _pin_ok(self, sig: str) -> bool | None:
        """Is a pin muxed to `sig`? None when the package has no pin offering it."""
        board = getattr(self.chip, "pins", None)
        if board is None:
            return True
        pins = board.signal_pins(sig)
        if not pins:
            return None
        return any(board.signal_matches(p.owner, sig) for p in pins)

    def _bank_pins_ok(self, bank: int) -> bool:
        if not self.check_pins:
            return True
        missing, absent = [], []
        for sig in ("FMC_SDCLK", f"FMC_SDNE{bank - 1}", f"FMC_SDCKE{bank - 1}"):
            ok = self._pin_ok(sig)
            if ok is None:
                absent.append(sig)
            elif not ok:
                missing.append(sig)
        if absent:
            pkg = getattr(getattr(self.chip, "desc", None), "package", "?")
            self._warn_once(f"absent{bank}", f"{self.name}: the {pkg} package has no {', '.join(absent)} pin: SDRAM "
                            f"bank {bank} cannot be connected; commands are not seen by the memory")
            return False
        if missing:
            self._warn_once(f"pins{bank}", f"{self.name}: SDRAM command to bank {bank} while {', '.join(missing)} "
                            f"{'is' if len(missing) == 1 else 'are'} not muxed to a pin (GPIO AF12): the memory does "
                            "not see it")
            return False
        return True

    def _flush_notes(self, dev: Any) -> None:
        for n in dev.notes:
            self._warn_once(n, f"{self.name}: SDRAM {n}")
        dev.notes.clear()

    # ------------------------------------------------------------------ registers
    def _sdcr_write(self, blk, reg, old, new):
        self._store(reg, new)
        bank = 1 if reg.name == "SDCR1" else 2
        if (new >> 4) & 3 == 3:
            self._warn_once(f"sdmwid{bank}", f"{self.name}: SDCR{bank}.MWID = 11 is reserved")
        if (new >> 2) & 3 == 3:
            self._warn_once(f"sdnr{bank}", f"{self.name}: SDCR{bank}.NR = 11 is reserved")
        if bank == 1 and (new >> 10) & 3 == 1:
            self._warn_once("sdclk01", f"{self.name}: SDCR1.SDCLK = 01 is reserved (SDRAM clock stays off)")
        if bank == 1 and (old ^ new) & 0xC00:
            if not (new >> 10) & 2:             # clock disabled: the memories lose CKE
                for dev in self.sdram.values():
                    dev.clock_disable()
            self._refresh_update()
        for b in (1, 2):
            self._sd_sync_req(b)
        return None

    def _sdcmr_read(self, blk, reg):
        return self.get("SDCMR") & ~0x1F                 # MODE / CTB1 / CTB2 are write-only

    def _sdcmr_write(self, blk, reg, old, new):
        mode = new & 7
        banks = tuple(b for b, bit in ((1, 0x10), (2, 0x08)) if new & bit)
        if mode == 7:
            self._warn_once("sdmode7", f"{self.name}: FMC_SDCMR.MODE = 111 is reserved: no command")
            return None
        if not banks:
            self._warn_once("noctb", f"{self.name}: FMC_SDCMR command {MODE_NAMES[mode]} without CTB1/CTB2: not issued")
            return None
        if not self._clock_on():
            return None
        if self._sdclk_div() == 0:
            self._warn_once("sdclkoff", f"{self.name}: SDRAM command {MODE_NAMES[mode]} while FMC_SDCR1.SDCLK "
                            "disables the SDRAM clock: not issued")
            return None
        if self.busy():
            self._warn_once("sdbusy", f"{self.name}: FMC_SDCMR written while FMC_SDSR.BUSY = 1: command "
                            f"{MODE_NAMES[mode]} ignored")
            return None
        now = self._sd_now()
        t1 = self.get("SDTR1")
        tb = self.get(f"SDTR{banks[0]}") if len(banks) == 1 else t1
        nrfs = ((new >> 5) & 0xF) + 1
        if nrfs == 16:
            self._warn_once("nrfs", f"{self.name}: FMC_SDCMR.NRFS = 1111 is reserved")
        trp, trc = _sdtr_field(t1, 20), _sdtr_field(t1, 12)
        cycles = {0: 1, 1: 1, 2: trp, 3: nrfs * trc, 4: _sdtr_field(tb, 0), 5: trp + _sdtr_field(tb, 8),
                  6: 1}[mode]
        if mode == 0 and any(self.modes[b] == 1 for b in banks):
            cycles = _sdtr_field(tb, 4)
        mrd = (new >> 9) & 0x1FFF
        self.commands.append((now, MODE_NAMES[mode], banks))
        for b in banks:
            dev = self.sdram.get(b)
            if mode == 5:
                self.modes[b] = 1
            elif mode == 6:
                self.modes[b] = 2
            elif mode == 0:
                self.modes[b] = 0
            if dev is None or not self._bank_pins_ok(b):
                continue
            if mode == 1:
                dev.clock_enable(now)
            elif mode == 2:
                dev.precharge_all(now)
            elif mode == 3:
                dev.auto_refresh(nrfs)
            elif mode == 4:
                dev.load_mode(mrd)
                if dev.mode == mrd:
                    self._check_mode(b, dev)
            elif mode == 5:
                dev.self_refresh(True)
            elif mode in (0, 6):
                dev.self_refresh(False)
            self._flush_notes(dev)
        self._busy_until = now + cycles * self._sdclk_ns()
        self._refresh_update()
        for b in (1, 2):
            self._sd_sync_req(b)
        return None

    def _check_mode(self, bank: int, dev: Any) -> None:
        cas = (self.get(f"SDCR{bank}") >> 7) & 3
        if dev.cas_latency != cas:
            self._warn_once(f"cas{bank}", f"{self.name}: SDRAM bank {bank} mode register CAS latency "
                            f"{dev.cas_latency} differs from FMC_SDCR{bank}.CAS = {cas} (RM0090 37.7.3 step 7a: "
                            "read data would be sampled at the wrong clock on silicon)")
        if dev.burst_length != 1:
            self._warn_once(f"bl{bank}", f"{self.name}: SDRAM bank {bank} mode register burst length "
                            f"{dev.burst_length} (RM0090 37.7.3 step 7b requires 1, M[2:0] = 000)")
        geo, dgeo = self.geometry(bank), dev.geometry()
        if geo != dgeo:
            self._warn_once(f"geo{bank}", f"{self.name}: FMC_SDCR{bank} describes {geo[0]} banks x {geo[1]} rows x "
                            f"{geo[2]} columns x {geo[3]} bit, the device on SDRAM bank {bank} is {dgeo[0]} x "
                            f"{dgeo[1]} x {dgeo[2]} x {dgeo[3]} bit (address lines mapped modulo the device)")

    def _sdrtr_write(self, blk, reg, old, new):
        if new & 1:
            self._re = False
        new &= ~1
        self._store(reg, new)
        count = (new >> 1) & 0x1FFF
        if count and (old >> 1) & 0x1FFF != count:
            if count < 41:
                self._warn_once("count41", f"{self.name}: FMC_SDRTR.COUNT = {count} < 41 SDRAM clock cycles "
                                "(RM0090 37.7.5 minimum)")
            t1 = self.get("SDTR1")
            tsum = _sdtr_field(t1, 16) + _sdtr_field(t1, 20) + _sdtr_field(t1, 12) + _sdtr_field(t1, 24) + 4
            if count == tsum:
                self._warn_once("counteq", f"{self.name}: FMC_SDRTR.COUNT = TWR+TRP+TRC+TRCD+4 = {count} is not "
                                "allowed (RM0090 37.7.5 note)")
        self._refresh_update(force=bool((old ^ new) & 0x3FFE))
        self._update_irq()
        return new

    def _sdsr_read(self, blk, reg):
        return ((0x20 if self.busy() else 0) | (self.modes[2] << 3) | (self.modes[1] << 1) |
                (1 if self._re else 0))

    # ------------------------------------------------------------------ refresh
    def _refresh_update(self, force: bool = False) -> None:
        chip = self.chip
        if chip is None:
            return
        count = (self.get("SDRTR") >> 1) & 0x1FFF
        per = self._sdclk_ns()
        running = bool(count and per and self._clock_on() and not self._stopped)
        interval = count * per if running else None
        key = (interval,)
        if key == self._refresh_key and not force:
            pass
        else:
            self._refresh_key = key
            if self._re_ev is not None:
                chip.sched.cancel(self._re_ev)
                self._re_ev = None
            if running:
                t1 = self.get("SDTR1")
                if count < _sdtr_field(t1, 20) + _sdtr_field(t1, 12):
                    self._re_ev = chip.sched.at_ns(chip.now_ns() + interval, self._refresh_error)
        for dev in self.sdram.values():
            dev.set_refresh(interval if (running and dev.cke) else None)

    def _refresh_error(self) -> None:
        self._re_ev = None
        if not self._re:
            self._re = True
            self.emit("info", block=self.name, msg=f"{self.name}: SDRAM refresh error (FMC_SDSR.RE): COUNT shorter "
                      "than PALL + auto-refresh (TRP + TRC)")
        self._update_irq()

    # ------------------------------------------------------------------ SDRAM windows
    def _sd_want(self, bank: int) -> tuple | None:
        if not self._clock_on():
            return None
        size = self.bank_size(bank)
        wp = bool(self.get(f"SDCR{bank}") & (1 << 9))
        dev = self.sdram.get(bank)
        if dev is None or not dev.ready or not self._sdclk_div():
            return ("float", size, wp, id(dev))
        if self.modes[bank]:
            return ("slow", size, wp, id(dev))
        mem = getattr(dev, "mem", None)
        if (self.geometry(bank) == dev.geometry() and mem is not None and mem.itemsize * 8 == dev.width
                and dev.nbytes == size and self.chip.bus.page and size % self.chip.bus.page == 0):
            return ("fast", size, wp, id(dev))
        return ("slow", size, wp, id(dev))

    def _sd_sync(self, bank: int) -> None:
        if self.chip is None:
            return
        want = self._sd_want(bank)
        if want == self.sd_open.get(bank):
            return
        self._sd_close(bank)
        if want is not None:
            self._sd_open(bank, want)

    def _sd_close(self, bank: int) -> None:
        if bank in self.sd_open and self.chip is not None:
            self.chip.bus.unmap_window(f"{self.name}_SDRAM{bank}")
        self.sd_open.pop(bank, None)

    def _sd_open(self, bank: int, want: tuple) -> None:
        kind, size, wp, _ = want
        name = f"{self.name}_SDRAM{bank}"
        bus = self.chip.bus
        base = SDRAM_BASE[bank]
        self.sd_open[bank] = want
        dev = self.sdram.get(bank)
        if kind == "fast":
            bus.map_window(name, base, size, buffer=dev.mem, period=size, writable=not wp)
        elif kind == "slow":
            self._check_mode(bank, dev)
            bus.map_window(name, base, size, read=self._sd_read(bank, dev), write=self._sd_write(bank, dev, wp))
        else:
            bus.map_window(name, base, size, read=self._float_read(bank), write=self._float_write(bank, wp))

    def _float_note(self, bank: int) -> None:
        dev = self.sdram.get(bank)
        why = ("no SDRAM device attached" if dev is None else
               "SDRAM clock disabled (FMC_SDCR1.SDCLK)" if not self._sdclk_div() else
               f"the SDRAM is not initialized ({dev.state}; RM0090 37.7.3: clock enable, PALL, auto-refresh, load "
               "mode register)")
        self._warn_once(f"float{bank}", f"{self.name}: access to SDRAM bank {bank} while {why}: the data bus floats "
                        "(reads 0, writes are lost)")

    def _float_read(self, bank: int):
        def rd(off: int, n: int) -> int:
            self._float_note(bank)
            return 0
        return rd

    def _float_write(self, bank: int, wp: bool):
        def wr(off: int, n: int, v: int) -> None:
            if wp:
                self._wp_fault(bank)
            self._float_note(bank)
        return wr

    def _wp_fault(self, bank: int) -> None:
        self._warn_once(f"wp{bank}", f"{self.name}: write to SDRAM bank {bank} with FMC_SDCR{bank}.WP = 1 (AHB error "
                        "on silicon); ignored by the slow access path")

    def _touch(self, bank: int, dev: Any) -> None:
        """First access to a bank in self-refresh / power-down: the controller exits that mode (37.7.4)."""
        if self.modes[bank]:
            was = self.modes[bank]
            self.modes[bank] = 0
            if was == 1:
                dev.self_refresh(False)
            self.emit("info", block=self.name, msg=f"{self.name}: SDRAM bank {bank} access exits "
                      f"{'self-refresh' if was == 1 else 'power-down'} mode")
            if self._sd_sync_ev is None and self.chip is not None:
                self._sd_sync_ev = self.chip.sched.in_cycles(0, self._deferred_sync)

    def _sd_sync_req(self, bank: int) -> None:
        """Window changes requested from an MMIO access (register write) while the core runs are applied after the
        access (scheduler event at the current cycle, which ends the slice): Unicorn does not survive unmapping a
        region from inside a memory callback (prefetch abort on the next fetch)."""
        chip = self.chip
        if chip is None:
            return
        if getattr(chip.core, "running", False):
            if self._sd_sync_ev is None:
                self._sd_sync_ev = chip.sched.in_cycles(0, self._deferred_sync)
        else:
            self._sd_sync(bank)

    def _deferred_sync(self) -> None:
        self._sd_sync_ev = None
        for b in (1, 2):
            self._sd_sync(b)

    def _loc(self, bank: int, dev: Any, a: int) -> tuple[int, int]:
        """Byte address in the bank window -> (device location, byte lane)."""
        nb = dev.width // 8
        cb, cr, cc, _w = self.geometry(bank)
        loc = a // nb
        col, row, ib = loc % cc, (loc // cc) % cr, loc // (cc * cr)
        return ((ib % dev.banks) * dev.rows + row % dev.rows) * dev.cols + col % dev.cols, a % nb

    def _sd_read(self, bank: int, dev: Any):
        def rd(off: int, n: int) -> int:
            self._touch(bank, dev)
            v = 0
            for i in range(n):
                loc, lane = self._loc(bank, dev, off + i)
                v |= ((dev.read(loc) >> (8 * lane)) & 0xFF) << (8 * i)
            return v
        return rd

    def _sd_write(self, bank: int, dev: Any, wp: bool):
        def wr(off: int, n: int, value: int) -> None:
            self._touch(bank, dev)
            if wp:
                self._wp_fault(bank)
                return
            for i in range(n):
                loc, lane = self._loc(bank, dev, off + i)
                sh = 8 * lane
                dev.write(loc, ((value >> (8 * i)) & 0xFF) << sh, 0xFF << sh)
        return wr

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)
