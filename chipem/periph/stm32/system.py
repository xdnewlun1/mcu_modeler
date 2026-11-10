"""STM32F4 system blocks: IWDG, FLASH interface, PWR, DBGMCU (the window watchdog is in wwdg.py).

IWDG (RM0090 ch. 21): KR keys 0xCCCC start (cannot be stopped again), 0xAAAA reload, 0x5555 unlock PR/RLR (any
other key or a reload locks them again; locked writes are ignored with one `warn`).  Counter clock = LSI
(params `lsi_hz`, default 32 kHz) / (4 << PR) (PR >= 6: /256); timeout = (RLR + 1) ticks from start/reload.
SR.PVU/RVU read 0 (updates are immediate).  Timeout: `warn`, RCC_CSR.IWDGRSTF on the next reset, then
chip.reset("watchdog").  Scheduled with Scheduler.at_ns (LSI does not depend on the core clock).

FLASH (RM0090 ch. 3): ACR is storage (LATENCY, PRFTEN, ICEN, DCEN read back; wait states have no timing effect).
KEYR 0x45670123 then 0xCDEF89AB clears CR.LOCK (a wrong key locks until reset); writing CR.LOCK=1 locks.  SR.BSY
always 0; EOP and the error flags are write-1-to-clear.  CR.STRT with SER (sector SNB) or MER erases the flash
memory to 0xFF immediately (sectors 0-3 16 KB, 4 64 KB, 5-11 128 KB from the FLASH memory region) and sets EOP when
EOPIE is set.  Programming (PG) needs nothing: the CPU can write the flash region directly.

PWR (RM0090 ch. 5): CR (VOS made writable, the SVD has it read-only; DBP, FPDS, PLS stored); CSR.VOSRDY reads 1
(regulator always ready); CSR.BRR mirrors BRE. CR.CWUF / CR.CSBF (write 1) clear CSR.WUF / CSR.SBF and read 0.
Low-power modes (RM0090 5.3), entered when the core stops in WFI/WFE (the PWR is a `chip.power_hooks` entry):
* Sleep (SCR.SLEEPDEEP = 0): the core stops, everything else runs; any enabled interrupt wakes it (generic).
* Stop (SLEEPDEEP = 1, CR.PDDS = 0; LPDS / FPDS only change the wake-up latency on silicon, not modelled): if an
  EXTI pending bit (PR & IMR) is already set the entry is ignored and execution continues. On entry the RCC's
  HSEON/PLLON/PLLI2SON (and their RDY bits) are cleared and SYSCLK is switched to HSI (SW = SWS = 0, prescalers kept),
  as the RM says for the Stop exit: firmware must re-initialise its clocks after waking. Peripheral models with
  `clock_stop()` (stm32.usart) freeze; others keep counting (approximation, see docs/LIMITATIONS.md). Only an EXTI
  line wakes the core (`chip.wake_gate`: PR & IMR != 0, i.e. EXTI0-15 pins, PVD, RTC alarm 17, RTC wakeup 22, ...);
  with WFE an EXTI event line (EMR) wakes it too. RTC and IWDG keep running (LSE/LSI).
* Standby (SLEEPDEEP = 1, PDDS = 1): CSR.SBF is set, all peripherals except RTC/backup domain, IWDG, RCC (its CSR
  flags and BDCR) and PWR are reset, the pins return to their reset state (high impedance; PA0 pulled down when
  CSR.EWUP = 1), main SRAM and CCM RAM are cleared (their content is lost on silicon; zero is our choice), the
  backup SRAM is kept only when CSR.BRE = 1. Wake-up: WKUP (PA0) rising edge with EWUP = 1, RTC alarm A/B, RTC
  wakeup (the RTC's interrupt-enabled flags, the same signals that feed EXTI lines 17/22) set CSR.WUF and reset the
  chip ("standby_wakeup"); NRST (Session reset) and an IWDG timeout also end Standby. Entering Standby with WUF
  already set exits at once (reset).
* WFE event register (ARMv7-M B1.5.18, kept here because the arch layer has none): set by an exception return (any
  exception taken since the last WFE), an `SEV` immediately before the `WFE` (halfword 0xBF40 at PC-4), an EXTI
  event line (EMR) and, with SCR.SEVONPEND, an EXTI interrupt line becoming pending. A WFE with the event register
  set clears it and continues; HAL_PWR_EnterSTOPMode(..., PWR_STOPENTRY_WFE) (`SEV; WFE; WFE`) therefore sleeps in the
  second WFE.
* CSR.WUF is also set by those wake-up events in Run/Sleep/Stop (RM 5.4.2). PWR_CR resets with every system reset;
  PWR_CSR (WUF, SBF, EWUP, BRE) survives system resets and Standby (only the first reset / power-on clears it).

DBGMCU: IDCODE reads 0x10076413 (DEV_ID 0x413 STM32F405/407, REV_ID 0x1007; the SVD's 0x411 is wrong for this
part); CR / APB1_FZ / APB2_FZ are storage.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, rcc

KR_START, KR_RELOAD, KR_UNLOCK = 0xCCCC, 0xAAAA, 0x5555
FLASH_KEYS = (0x45670123, 0xCDEF89AB)
CR_PG, CR_SER, CR_MER, CR_STRT, CR_EOPIE, CR_LOCK = 1 << 0, 1 << 1, 1 << 2, 1 << 16, 1 << 24, 1 << 31
SR_EOP = 1 << 0
SECTORS = [(0x0000, 0x4000), (0x4000, 0x4000), (0x8000, 0x4000), (0xC000, 0x4000), (0x10000, 0x10000)] + \
          [(0x20000 * k, 0x20000) for k in range(1, 8)]


@register("stm32.iwdg")
class IWDG(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"KR": {"on_write": self._kr_write, "on_read": lambda b, r: 0},
              "PR": {"on_write": self._cfg_write}, "RLR": {"on_write": self._cfg_write},
              "SR": {"ro_mask": 0xFFFFFFFF, "on_read": lambda b, r: 0}}
        r = build_regs((name, "IWDG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.lsi_hz = float(params.get("lsi_hz", 32000.0))
        self.running = False
        self.unlocked = False
        self._ev = None
        self._start_ns = 0.0
        self._warned_locked = False
        self.timeouts = 0

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self.running = False
        self.unlocked = False
        self._warned_locked = False

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    @property
    def prescaler(self) -> int:
        return min(4 << (self.get("PR") & 7), 256)

    @property
    def timeout_ns(self) -> float:
        return ((self.get("RLR") & 0xFFF) + 1) * self.prescaler * 1e9 / self.lsi_hz

    def counter(self) -> int:
        if not self.running or self.chip is None:
            return self.get("RLR") & 0xFFF
        el = int((self.chip.now_ns() - self._start_ns) * self.lsi_hz / self.prescaler / 1e9)
        return max((self.get("RLR") & 0xFFF) - el, 0)

    def _reload(self) -> None:
        self._cancel()
        if not self.running or self.chip is None:
            return
        self._start_ns = self.chip.now_ns()
        self._ev = self.chip.sched.at_ns(self._start_ns + self.timeout_ns, self._timeout)

    def _timeout(self) -> None:
        self._ev = None
        self.timeouts += 1
        self.emit("warn", block=self.name, msg=f"IWDG timeout after {self.timeout_ns / 1e6:.3f} ms without reload "
                  f"(0xAAAA): watchdog reset")
        r = rcc(self.chip)
        if r is not None:
            r.pending_reset_flags |= (1 << 29) | (1 << 26)          # IWDGRSTF, PINRSTF
        self.chip.reset("watchdog")

    def _kr_write(self, blk, reg, old, new):
        key = new & 0xFFFF
        if key == KR_START:
            if not self.running:
                self.running = True
                self.emit("info", block=self.name, msg=f"IWDG started: timeout {self.timeout_ns / 1e6:.3f} ms "
                          f"(LSI {self.lsi_hz / 1e3:.0f} kHz / {self.prescaler}, RLR={self.get('RLR') & 0xFFF}); "
                          f"reload with 0xAAAA", timeout_ns=self.timeout_ns)
            self.unlocked = False
            self._reload()
        elif key == KR_RELOAD:
            self.unlocked = False
            self._reload()
        elif key == KR_UNLOCK:
            self.unlocked = True
        else:
            self.unlocked = False
        return 0

    def _cfg_write(self, blk, reg, old, new):
        if not self.unlocked:
            if not self._warned_locked:
                self._warned_locked = True
                self.emit("warn", block=self.name, msg=f"IWDG_{reg.name} write without the 0x5555 unlock key: ignored")
            return old
        return None


@register("stm32.flash")
class FLASH(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"KEYR": {"on_write": self._keyr_write, "on_read": lambda b, r: 0},
              "OPTKEYR": {"on_read": lambda b, r: 0},
              "SR": {"w1c_mask": 0xF3, "ro_mask": 0xFFFFFF0C},
              "CR": {"on_write": self._cr_write}}
        r = build_regs((name, "FLASH"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._key_stage = 0
        self._wrong_key = False
        self._warned_lock = False

    def reset(self) -> None:
        super().reset()
        self._key_stage = 0
        self._wrong_key = False
        self._warned_lock = False

    @property
    def latency(self) -> int:
        return self.get("ACR") & 0xF

    def _keyr_write(self, blk, reg, old, new):
        if self._wrong_key or not self.get("CR") & CR_LOCK:
            return 0
        if self._key_stage == 0 and new == FLASH_KEYS[0]:
            self._key_stage = 1
        elif self._key_stage == 1 and new == FLASH_KEYS[1]:
            self._key_stage = 0
            self.set("CR", self.get("CR") & ~CR_LOCK)
        else:
            self._wrong_key = True
            self.emit("warn", block=self.name, msg="FLASH_KEYR: wrong unlock sequence; FLASH_CR locked until reset "
                      "(silicon also raises a bus error)")
        return 0

    def _flash_region(self) -> Any:
        chip = self.chip
        for m in getattr(chip.desc, "memory", []):
            if m.kind == "flash" and m.name.upper().startswith("FLASH"):
                return m
        return None

    def _cr_write(self, blk, reg, old, new):
        if old & CR_LOCK:
            if not self._warned_lock:
                self._warned_lock = True
                self.emit("warn", block=self.name, msg="FLASH_CR write while locked: ignored")
            return old
        if not new & CR_STRT:
            return None
        new &= ~CR_STRT
        mem = self._flash_region()
        if mem is not None and new & (CR_SER | CR_MER):
            if new & CR_MER:
                spans = [(0, mem.size)]
                what = "mass erase"
            else:
                snb = (new >> 3) & 0xF
                spans = [SECTORS[snb]] if snb < len(SECTORS) else []
                what = f"sector {snb} erase"
            for off, n in spans:
                n = max(0, min(n, mem.size - off))
                if n:
                    self.chip.core.write_mem(mem.base + off, b"\xFF" * n)
            flush = getattr(self.chip.core, "flush_code_cache", None)
            if flush is not None:
                flush()
            self.emit("info", block=self.name, msg=f"FLASH {what}")
        if new & CR_EOPIE:
            self.set("SR", self.get("SR") | SR_EOP)
        return new


PWR_LPDS, PWR_PDDS, PWR_CWUF, PWR_CSBF = 1 << 0, 1 << 1, 1 << 2, 1 << 3
CSR_WUF, CSR_SBF, CSR_EWUP, CSR_BRE, CSR_BRR, CSR_VOSRDY = 1 << 0, 1 << 1, 1 << 8, 1 << 9, 1 << 3, 1 << 14
_ALWAYS_ON = ("stm32.rtc", "stm32.iwdg", "stm32.rcc", "stm32.pwr", "stm32.dbgmcu")


@register("stm32.pwr")
class PWR(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "PWR"
        # the SVD marks PWR_CR.VOS (bit 14) read-only; on the F405/F407 it is read-write (RM0090 5.4.1)
        ov = {"CR": {"ro_mask": 0xFFFFBC00, "on_write": self._cr_write},
              "CSR": {"reset": CSR_VOSRDY, "ro_mask": 0xFFFFFFFF & ~(CSR_EWUP | CSR_BRE), "on_read": self._csr_read,
                      "on_write": self._csr_write}}
        r = build_regs((name, "PWR"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.mode = "run"                   # run | sleep | stop | standby
        self.entries = {"sleep": 0, "stop": 0, "standby": 0}
        self._powered = False
        self._wkup_pin: Any = None
        self._wkup_level = 0
        self._wfe = False                   # sleeping in a WFE
        self._event = False                 # WFE event register
        self._exc_seen = 0                  # arch.entries at the last WFE
        self._exti: Any = None
        self._notes: set[str] = set()

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        hooks = getattr(chip, "power_hooks", None)
        if hooks is not None and self not in hooks:
            hooks.append(self)
        from ._util import find_model
        self._exti = find_model(chip, "stm32.exti", "EXTI")
        board = getattr(chip, "pins", None)
        pin = board.gpio("A", 0) if board is not None else None
        if pin is not None:
            self._wkup_pin = pin
            board.input_listeners[pin.id].append(self._wkup_edge)

    def reset(self) -> None:
        keep = self.get("CSR") if self._powered else 0
        super().reset()
        if self._powered:
            self.set("CSR", (keep & (CSR_WUF | CSR_SBF | CSR_EWUP | CSR_BRE)) | CSR_VOSRDY)
        self._powered = True
        self.mode = "run"
        self._wfe = False
        self._event = False
        chip = self.chip
        if chip is not None:
            if getattr(chip, "wake_gate", None) in (self._stop_gate, self._standby_gate):
                chip.wake_gate = None
            self._exc_seen = getattr(chip.arch, "entries", 0)
        if self._wkup_pin is not None:
            self._wkup_level = 1 if self._wkup_pin.level() == "H" else 0

    # ------------------------------------------------------------ registers
    def _csr_read(self, blk, reg):
        v = self.get("CSR") | CSR_VOSRDY
        if v & CSR_BRE:
            v |= CSR_BRR                                         # BRR follows BRE
        return v

    def _csr_write(self, blk, reg, old, new):
        if new & ~old & CSR_EWUP and self._wkup_level:
            new |= CSR_WUF          # "an additional wakeup event is detected if WKUP is enabled while it is high"
        return new

    def _cr_write(self, blk, reg, old, new):
        clr = (CSR_WUF if new & PWR_CWUF else 0) | (CSR_SBF if new & PWR_CSBF else 0)
        if clr:
            self.set("CSR", self.get("CSR") & ~clr)
        return new & ~(PWR_CWUF | PWR_CSBF)

    # ------------------------------------------------------------ wake-up sources
    def _wkup_edge(self, pin: Any) -> None:
        b = 1 if pin.level() == "H" else 0
        prev, self._wkup_level = self._wkup_level, b
        if b and not prev and self.get("CSR") & CSR_EWUP:
            self.wakeup_event("WKUP pin")

    def wakeup_event(self, what: str) -> None:
        """WKUP rising edge or an RTC alarm / wakeup / tamper / time-stamp event: CSR.WUF; ends Standby (reset)."""
        self.set("CSR", self.get("CSR") | CSR_WUF)
        if self.mode == "standby" and self.chip is not None:
            self.emit("info", block=self.name, msg=f"wake-up from Standby by {what}: reset", source=what)
            self.mode = "run"
            self.chip.arch.request_reset("standby_wakeup")

    def exti_event(self) -> None:
        """An EXTI event (EMR line, or with SCR.SEVONPEND an interrupt line becoming pending): WFE wake-up event."""
        chip = self.chip
        if chip is not None and chip.sleeping and self._wfe:
            chip.wake()
        else:
            self._event = True

    # ------------------------------------------------------------ power hook (chip.power_hooks)
    def _stop_gate(self) -> bool:
        ex = self._exti
        return ex is None or bool(ex.get("PR") & ex.get("IMR"))

    def _standby_gate(self) -> bool:
        return False                            # interrupts do not end Standby

    def _sev_before(self, chip: Any) -> bool:
        try:
            pc = chip.core.pc
            return int.from_bytes(chip.core.read_mem((pc - 4) & ~1, 2), "little") == 0xBF40
        except Exception:
            return False

    def on_sleep(self, chip: Any) -> None:
        arch = chip.arch
        wfe = getattr(chip.core, "stop_info", None) == "wfe"
        ent = getattr(arch, "entries", 0)
        if wfe:
            if self._event or ent != self._exc_seen or self._sev_before(chip):
                self._event = False             # event register set: WFE clears it and does not sleep
                self._exc_seen = ent
                chip.wake()
                return
        self._exc_seen = ent
        self._wfe = wfe
        if not getattr(arch, "sleep_deep", False):
            self.mode = "sleep"
            self.entries["sleep"] += 1
            return
        if self.get("CR") & PWR_PDDS:
            self._enter_standby(chip)
        else:
            self._enter_stop(chip)

    def on_wake(self, chip: Any) -> None:
        self._exc_seen = getattr(chip.arch, "entries", 0)
        self._wfe = False
        if self.mode == "stop":
            if chip.wake_gate == self._stop_gate:
                chip.wake_gate = None
            for blk in list(chip.peripherals.values()):
                fn = getattr(blk, "clock_stop", None)
                if fn is not None:
                    fn(False, "stop")
            self.emit("info", block=self.name, msg="exit Stop mode (SYSCLK = HSI)")
        self.mode = "run"

    def _enter_stop(self, chip: Any) -> None:
        if self._exti is not None and self._exti.get("PR") & self._exti.get("IMR"):
            if "stop_pending" not in self._notes:
                self._notes.add("stop_pending")
                self.emit("info", block=self.name, msg="Stop mode entry ignored: an EXTI pending bit is set "
                          "(RM0090 5.3.5); execution continues")
            chip.wake()
            return
        self.mode = "stop"
        self.entries["stop"] += 1
        r = rcc(chip)
        if r is not None:
            from .rcc import HSEON, HSERDY, HSION, HSIRDY, PLLI2SON, PLLI2SRDY, PLLON, PLLRDY
            cr = r.get("CR")
            r.set("CR", (cr | HSION | HSIRDY) & ~(HSEON | HSERDY | PLLON | PLLRDY | PLLI2SON | PLLI2SRDY))
            r.set("CFGR", r.get("CFGR") & ~0xF)         # SW = SWS = HSI
            r.update()
        for blk in list(chip.peripherals.values()):
            fn = getattr(blk, "clock_stop", None)
            if fn is not None:
                fn(True, "stop")
        chip.wake_gate = self._stop_gate
        self.emit("info", block=self.name, msg=f"enter Stop mode ({'WFE' if self._wfe else 'WFI'}, regulator "
                  f"{'low-power' if self.get('CR') & PWR_LPDS else 'main'}); HSE/PLL off, wake-up by EXTI")

    def _enter_standby(self, chip: Any) -> None:
        self.entries["standby"] += 1
        csr = self.get("CSR")
        self.set("CSR", csr | CSR_SBF)
        if csr & CSR_WUF:
            self.emit("info", block=self.name, msg="Standby entered with CSR.WUF set: immediate wake-up (reset)")
            chip.arch.request_reset("standby_wakeup")
            return
        self.emit("info", block=self.name, msg="enter Standby mode: 1.2 V domain off, wake-up by WKUP / RTC / NRST "
                  "/ IWDG (reset)")
        arch_blocks = getattr(chip, "arch_blocks", [])
        for blk in list(chip.peripherals.values()):
            if blk in arch_blocks or getattr(blk, "model", None) in _ALWAYS_ON:
                continue
            blk.reset()
        board = getattr(chip, "pins", None)
        if board is not None:
            board.reset()
            if csr & CSR_EWUP and self._wkup_pin is not None:
                board.mcu_update(self._wkup_pin, direction="in", pull="down")
        for m in getattr(chip.desc, "memory", []):
            if m.kind != "ram" or (m.name.upper() == "BKPSRAM" and csr & CSR_BRE):
                continue
            try:
                chip.core.write_mem(m.base, bytes(m.size))
            except Exception:
                pass
        self.mode = "standby"
        chip.wake_gate = self._standby_gate
        if self._wkup_pin is not None:
            self._wkup_level = 1 if self._wkup_pin.level() == "H" else 0


@register("stm32.dbgmcu")
class DBGMCU(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        idcode = int(params.get("idcode", 0x10076413))
        ov = {"DBGMCU_IDCODE": {"reset": idcode, "ro_mask": 0xFFFFFFFF}}
        r = build_regs((name, "DBGMCU", "DBG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
