"""STM32F1 PWR (RM0008 ch. 5: CR LPDS/PDDS/CWUF/CSBF/PVDE/PLS/DBP, CSR WUF/SBF/PVDO/EWUP) and low-power modes.

Verified against RM0008 Rev 21 ch. 5 (5.2 supervisor, 5.3 low-power modes, Tables 11-15, 5.4.1/5.4.2 registers);
the generic CHIP_EM low-power hooks are described in CONTRACTS.md "Low-power modes" (chipem/periph/lowpower.py).

Registers (5.4): CR reset 0 (also by the Standby exit); CWUF / CSBF (write 1, read 0) clear CSR.WUF / CSR.SBF.  DBP
enables writes to the backup domain (RCC_BDCR LSEON/LSEBYP/RTCSEL/RTCEN, BKP registers, RTC; stm32f1.rcc / bkp / rtc
check it).  PVD (5.2.2): CSR.PVDO = PVDE and VDD (params `vdd`, default 3.3 V; `set_vdd()`) below the PLS level
(000 2.2 V ... 111 2.9 V, 5.4.1); PVDO drives EXTI line 16; 0 after reset / Standby until PVDE is set.  CSR is not
reset by the Standby exit (5.3.5); otherwise a system reset clears WUF and EWUP, SBF only a power-on reset (or CSBF).
WUF is set by a WKUP rising edge (EWUP = 1) or an RTC alarm, and when EWUP is set while WKUP is already high (5.4.2
note).  EWUP = 1 forces the WKUP pin (PA0) to input pull-down, in every mode (stm32f1.gpio shows it as owner "WKUP").
Low-power modes, entered when the core stops in WFI/WFE (this block is a `chip.power_hooks` entry, 5.3 Table 11):
* Sleep (SCR.SLEEPDEEP = 0): the core stops, peripherals run, any enabled interrupt (WFI) or event (WFE) wakes it.
* Stop (SLEEPDEEP = 1, PDDS = 0, Table 14): ignored (execution continues) when an EXTI pending bit or the RTC alarm
  flag (RTC_CRL.ALRF) is set.  On entry PLL and HSE stop and, as on the exit, SYSCLK is the HSI (stm32f1.rcc
  `enter_stop()`: SW = SWS = 00, prescalers kept, 5.3.4).  The 1.8 V domain clocks stop: every block except RCC, PWR,
  BKP, RTC, IWDG, EXTI, AFIO, GPIO, FLASH, DBG and the system memory is frozen (chipem.periph.lowpower.Freezer: pending
  events held, `clock_stop()` called where defined).  Only an EXTI line wakes the core (`chip.wake_gate`: PR & IMR, or
  an EMR event with WFE): pins EXTI0-15, PVD (16), RTC alarm (17), USB wakeup (18).  Option byte nRST_STOP = 0 resets
  instead (7.1.1, RCC_CSR.LPWRRSTF).
* Standby (SLEEPDEEP = 1, PDDS = 1, Table 15): CSR.SBF set; all blocks except the backup domain (RTC, BKP), IWDG,
  RCC (CSR flags, BDCR) and PWR are reset, pins go high impedance (5.3.5; PA0 pulled down when CSR.EWUP = 1), SRAM is
  cleared (lost on silicon).  Wake-up: WKUP rising edge with EWUP = 1 or an RTC alarm rising edge set CSR.WUF and reset
  the chip ("standby_wakeup"); NRST and an IWDG timeout end Standby too.  Entering Standby with WUF set exits at once
  (Table 15 requires WUF = 0).  Option byte nRST_STDBY = 0 resets instead.
* WFE event register as in stm32.pwr (exception return, SEV before WFE, EXTI EMR event, SEVONPEND, 10.2.3).
Not modelled: LPDS (only the Stop wake-up latency: HSI start-up + regulator wake-up, Table 14), the Stop-entry delay
while a flash program/erase or APB access is ongoing, "all peripheral interrupt pending bits must be reset" before Stop
(only EXTI_PR and ALRF are checked; a pending enabled NVIC interrupt prevents WFI sleep anyway), CWUF's 2-cycle delay,
PVD hysteresis (100 mV), DBGMCU_CR.DBG_SLEEP/STOP/STANDBY (debugger only).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, exti_line, find_model, rcc

LPDS, PDDS, CWUF, CSBF, PVDE, DBP = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 8
WUF, SBF, PVDO, EWUP = 1 << 0, 1 << 1, 1 << 2, 1 << 8
PVD_LEVELS = (2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9)        # PLS rising thresholds (DS15060 Table 11)
ALWAYS_ON = ("stm32f1.rtc", "stm32f1.bkp", "stm32.iwdg", "stm32f1.rcc", "stm32f1.pwr", "stm32f1.dbgmcu",
             "stm32f1.sysmem", "stm32f1.optbytes")
STOP_KEEP = ALWAYS_ON + ("stm32f1.exti", "stm32f1.gpio", "stm32f1.afio", "stm32f1.flash")


@register("stm32f1.pwr")
class PWR(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "PWR"
        ov = {"CR": {"on_write": self._cr_write, "ro_mask": 0xFFFFFE00},
              "CSR": {"ro_mask": 0xFFFFFFFF & ~EWUP, "on_write": self._csr_write}}
        r = build_regs((name, "PWR"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.mode = "run"
        self.entries = {"sleep": 0, "stop": 0, "standby": 0}
        self._powered = False
        self._wkup_pin: Any = None
        self._wkup_level = 0
        self._wfe = False
        self._event = False
        self._exc_seen = 0
        self._exti: Any = None
        self._rtc: Any = None
        self._notes: set[str] = set()
        self.vdd = float(params.get("vdd", 3.3))
        self._pvdo = False
        self._standby_wake = False
        self._frz: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        hooks = getattr(chip, "power_hooks", None)
        if hooks is not None and self not in hooks:
            hooks.append(self)
        from ..lowpower import Freezer
        self._frz = Freezer(chip)
        self._exti = find_model(chip, "stm32f1.exti", "EXTI")
        self._rtc = find_model(chip, "stm32f1.rtc", "RTC")
        board = getattr(chip, "pins", None)
        pin = board.gpio("A", 0) if board is not None else None
        if pin is not None:
            self._wkup_pin = pin
            board.input_listeners[pin.id].append(self._wkup_edge)

    def reset(self) -> None:
        keep = self.get("CSR") if self._powered else 0
        mask = SBF | ((WUF | EWUP) if self._standby_wake else 0)
        self._standby_wake = False
        if self._frz is not None:
            self._frz.drop()
        super().reset()
        if self._powered:
            self.set("CSR", keep & mask)
        self._powered = True
        self._pvd_update()
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
    def _csr_write(self, blk, reg, old, new):
        if new & ~old & EWUP and self._wkup_level:
            new |= WUF               # enabling WKUP while the pin is high is a wake-up event (5.4.2 note)
        if (old ^ new) & EWUP and self.chip is not None:
            self._store(reg, new)
            from ._util import gpio_ports
            for p in gpio_ports(self.chip):      # EWUP = 1 forces the WKUP pin to input pull-down (5.4.2)
                if p.port == "A":
                    p.resync_all()
        return new

    def _cr_write(self, blk, reg, old, new):
        clr = (WUF if new & CWUF else 0) | (SBF if new & CSBF else 0)
        if clr:
            self.set("CSR", self.get("CSR") & ~clr)
        new &= ~(CWUF | CSBF)
        self._store(reg, new)
        if (old ^ new) & (PVDE | 0xE0):
            self._pvd_update()
        return new

    def dbp(self) -> bool:
        return bool(self.get("CR") & DBP)

    # ------------------------------------------------------------ PVD
    def pvd_threshold(self) -> float:
        return PVD_LEVELS[(self.get("CR") >> 5) & 7]

    def set_vdd(self, volts: float) -> None:
        self.vdd = float(volts)
        self._pvd_update()

    def _pvd_update(self) -> None:
        lvl = bool(self.get("CR") & PVDE) and self.vdd < self.pvd_threshold()
        self.set("CSR", (self.get("CSR") & ~PVDO) | (PVDO if lvl else 0))
        if lvl != self._pvdo:
            self._pvdo = lvl
            if self.chip is not None:
                exti_line(self.chip, 16, lvl)

    # ------------------------------------------------------------ wake-up sources
    def _wkup_edge(self, pin: Any) -> None:
        b = 1 if pin.level() == "H" else 0
        prev, self._wkup_level = self._wkup_level, b
        if b and not prev and self.get("CSR") & EWUP:
            self.wakeup_event("WKUP pin")

    def wakeup_event(self, what: str) -> None:
        """WKUP rising edge or RTC alarm: CSR.WUF; ends Standby (reset)."""
        self.set("CSR", self.get("CSR") | WUF)
        if self.mode == "standby" and self.chip is not None:
            self.emit("info", block=self.name, msg=f"wake-up from Standby by {what}: reset", source=what)
            self.mode = "run"
            self._standby_wake = True
            self.chip.arch.request_reset("standby_wakeup")

    def exti_event(self) -> None:
        chip = self.chip
        if chip is not None and chip.sleeping and self._wfe:
            chip.wake()
        else:
            self._event = True

    # ------------------------------------------------------------ power hook
    def _stop_gate(self) -> bool:
        ex = self._exti
        return ex is None or bool(ex.get("PR") & ex.get("IMR"))

    def _standby_gate(self) -> bool:
        return False

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
        if wfe and (self._event or ent != self._exc_seen or self._sev_before(chip)):
            self._event = False
            self._exc_seen = ent
            chip.wake()
            return
        self._exc_seen = ent
        self._wfe = wfe
        if not getattr(arch, "sleep_deep", False):
            self.mode = "sleep"
            self.entries["sleep"] += 1
            return
        if self.get("CR") & PDDS:
            self._enter_standby(chip)
        else:
            self._enter_stop(chip)

    def on_wake(self, chip: Any) -> None:
        self._exc_seen = getattr(chip.arch, "entries", 0)
        self._wfe = False
        if self.mode == "stop" and chip.wake_gate == self._stop_gate:
            chip.wake_gate = None
        if self._frz is not None and self._frz.active:
            self._frz.thaw()
        if self.mode == "stop":
            self.emit("info", block=self.name, msg="exit Stop mode (SYSCLK = HSI 8 MHz)")
        self.mode = "run"

    def _lowpower_reset(self, chip: Any, mode: str) -> bool:
        """Option bytes nRST_STOP / nRST_STDBY = 0: entering the mode resets the chip (RCC_CSR.LPWRRSTF)."""
        fl = find_model(chip, "stm32f1.flash", "FLASH")
        if fl is None or not fl.rst_on(mode):
            return False
        r = rcc(chip)
        if r is not None:
            from .rcc import LPWRRSTF, PINRSTF
            r.pending_reset_flags |= LPWRRSTF | PINRSTF
        self.emit("info", block=self.name, msg=f"{mode} entry with option byte nRST_{mode.upper()[:5]} = 0: reset")
        chip.arch.request_reset("lowpower")
        return True

    def _enter_stop(self, chip: Any) -> None:
        if self._lowpower_reset(chip, "stop"):
            return
        why = None
        if self._exti is not None and self._exti.get("PR"):
            why = "an EXTI pending bit is set"
        elif self._rtc is not None and self._rtc.get("CRL") & 2:
            why = "the RTC alarm flag (RTC_CRL.ALRF) is set"
        if why is not None:
            if why not in self._notes:
                self._notes.add(why)
                self.emit("info", block=self.name, msg=f"Stop mode entry ignored: {why}; execution continues")
            chip.wake()
            return
        self.mode = "stop"
        self.entries["stop"] += 1
        r = rcc(chip)
        if r is not None:
            stop = getattr(r, "enter_stop", None)
            if stop is not None:
                stop()
            else:
                from .rcc import HSEON, HSERDY, HSION, HSIRDY, PLLON, PLLRDY
                r.set("CR", (r.get("CR") | HSION | HSIRDY) & ~(HSEON | HSERDY | PLLON | PLLRDY))
                r.set("CFGR", r.get("CFGR") & ~0xF)
                r.update()
        arch_blocks = getattr(chip, "arch_blocks", [])
        self._frz.freeze([b for b in chip.peripherals.values()
                          if b not in arch_blocks and getattr(b, "model", None) not in STOP_KEEP], "stop")
        chip.wake_gate = self._stop_gate
        self.emit("info", block=self.name, msg=f"enter Stop mode ({'WFE' if self._wfe else 'WFI'}, regulator "
                  f"{'low-power' if self.get('CR') & LPDS else 'on'}); HSE/PLL off, wake-up by EXTI")

    def _enter_standby(self, chip: Any) -> None:
        if self._lowpower_reset(chip, "standby"):
            return
        self.entries["standby"] += 1
        csr = self.get("CSR")
        self.set("CSR", csr | SBF)
        if csr & WUF:
            self.emit("info", block=self.name, msg="Standby entered with CSR.WUF set: immediate wake-up (reset)")
            self._standby_wake = True
            chip.arch.request_reset("standby_wakeup")
            return
        self.emit("info", block=self.name, msg="enter Standby mode: 1.8 V domain off, wake-up by WKUP / RTC alarm / "
                  "NRST / IWDG (reset)")
        arch_blocks = getattr(chip, "arch_blocks", [])
        for blk in list(chip.peripherals.values()):
            if blk in arch_blocks or getattr(blk, "model", None) in ALWAYS_ON:
                continue
            blk.reset()
        board = getattr(chip, "pins", None)
        if board is not None:
            board.reset()
            if csr & EWUP and self._wkup_pin is not None:
                board.mcu_update(self._wkup_pin, direction="in", pull="down")
        for m in getattr(chip.desc, "memory", []):
            if m.kind == "ram":
                try:
                    chip.core.write_mem(m.base, bytes(m.size))
                except Exception:
                    pass
        self.mode = "standby"
        chip.wake_gate = self._standby_gate
        if self._wkup_pin is not None:
            self._wkup_level = 1 if self._wkup_pin.level() == "H" else 0
