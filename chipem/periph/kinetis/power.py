"""Kinetis SMC (power modes), PMC, RCM, LLWU and the VBAT register file (K22P100M120SF5V2RM ch. 7, 13, 14, 15, 16).

* SMC (ch. 14): PMPROT is write-once after a reset (AVLP/ALLS/AVLLS); PMCTRL.RUNM = VLPR (needs AVLP and PMSTAT=RUN)
  and back; STOPM writes to a mode PMPROT does not allow are blocked (the field keeps its value); a successful PMPROT
  write after a reset clears STOPM; STOPA is set when a stop entry is aborted (an enabled interrupt already pending).
  The SMC is the chip's power hook (chip.power_hooks, CONTRACTS.md "Low-power modes"): when the core executes WFI/WFE
  it picks the mode from SCR.SLEEPDEEP, PMCTRL and VLLSCTRL (Table 14-7): WAIT / VLPW (SLEEPDEEP=0), STOP / VLPS / LLS
  / VLLS3-0. PMSTAT shows STOP/VLPS/LLS/VLLS/VLPW while asleep. Exit follows Table 14-7 and LPWUI (VLPS entered from
  RUN always returns to RUN; LLS returns to the run mode it was entered from; an LPWUI exit clears RUNM).
  Clocks (Table 7-2): in STOP/VLPS the core, system and bus clocks stop: every block's scheduler events are held back
  ("frozen") except those of the LPTMR, RTC, CMP, DAC, VREF, LLWU, power/clock blocks, PORT/GPIO, the WDOG (with
  STCTRLH.STOPEN) and an ADC on its asynchronous clock (CFG1.ADICLK=3). Blocks with `clock_stop(stopped, mode)` (PIT,
  WDOG, UART, DSPI, SAI, FlexCAN) are told instead and keep their counters right; the held ones get `time_shift()`
  on exit (FTM, PDB, CMT, EWM, USB, USBDCD, DMA: counter anchors move by the time slept; chipem/periph/lowpower.py).
  Wake-capable while held: I2C address match with C1.WUEN (STOP/VLPS), FlexCAN self wake, SAI with STOPE. In LLS/VLLS only the LPTMR, RTC, CMP, DAC, LLWU and
  the power/clock blocks run; the NVIC is off (`chip.wake_gate`), only the LLWU wakes the chip. WAIT/VLPW stop only
  the core (the WDOG too unless STCTRLH.WAITEN). Frozen events resume with their remaining time.
  MCG on exit (25.4.1 notes): PEE -> PBE after VLPS/LLS (C5.PLLSTEN0 cleared) and after STOP with PLLSTEN0=0.
  VLLSx exit is a reset ("wakeup"): RCM SRS0 = 0x01 (0x41 by the RESET pin), PMC REGSC.ACKISO=1, SMC PMPROT/PMCTRL/
  VLLSCTRL and the LLWU keep their values, SRAM is lost per 3.5.3.3 (VLLS2: all but 0x20000000 + 4 KB, +12 KB with
  VLLSCTRL.RAM2PO; VLLS1/0: all SRAM; it reads 0).
* PMC: REGSC.ACKISO (bit 3) is write-1-to-clear (set by a VLLS wake-up); LVDSC1.LVDACK / LVDSC2.LVWACK (bit 6) read 0.
  LVD/LVW are storage (no supply voltage).
* RCM: SRS0/SRS1 report the cause of the last reset from the `reset` event reason: watchdog -> WDOG, lockup ->
  LOCKUP, SYSRESETREQ -> SW, pin -> PIN, loc -> LOC, lol -> LOL, wakeup -> WAKEUP (0x01), wakeup_pin -> 0x41,
  anything else -> power-on 0x82. A falling edge on the RESET_b pin (board net / `--set RESET_b=0`) resets the chip
  ("pin"; in LLS/VLLS "wakeup_pin").
* LLWU (ch. 16, 3.8.x Table "LLWU inputs"): PE1-4 (rising / falling / any edge per pin), ME (modules), F1/F2 (w1c),
  F3 (module flags, read-only, follow the module flag), FILT1/FILT2 (FILTE edge, FILTSEL pin, FILTF w1c), RST. Pins
  LLWU_P0-15 come from the pin table (functions named LLWU_Pn); modules: M0 LPTMR0, M1-3 CMP0-2, M5 RTC alarm, M7
  RTC seconds. Active only in LLS/VLLS; an enabled event sets the flag and wakes the chip (LLS: interrupt flow with
  the LLWU IRQ asserted while a flag is set; VLLS: reset). Registers survive a VLLS wake-up reset.
* VBAT: battery-backed register file = plain RAM that survives chip.reset().
"""
from __future__ import annotations

import re
from typing import Any

from ..base import MMIOBlock
from ..lowpower import Freezer
from ..registry import register
from ._util import build_regs, find_model, irq_number

PMSTAT = {"RUN": 0x01, "STOP": 0x02, "VLPR": 0x04, "VLPW": 0x08, "VLPS": 0x10, "LLS": 0x20, "VLLS": 0x40}
AVLP, ALLS, AVLLS = 0x20, 0x08, 0x02
STOPA = 0x08
LPWUI = 0x80

# blocks whose clocks keep running (Table 7-2), by model name
_KEEP_ALWAYS = {"kinetis.smc", "kinetis.pmc", "kinetis.rcm", "kinetis.llwu", "kinetis.vbat", "kinetis.mcg",
                "kinetis.osc", "kinetis.lptmr", "kinetis.rtc", "kinetis.cmp", "kinetis.dac", "kinetis.sim"}
_KEEP_STOP = _KEEP_ALWAYS | {"kinetis.port", "kinetis.gpio", "kinetis.vref"}

# reset reasons that are not a power-on reset (everything else is POR)
_WARM = ("watchdog", "wdog", "lockup", "sysresetreq", "vectreset", "sw", "software", "aircr", "pin", "loc", "lol",
         "wakeup")


def reset_kind(chip: Any) -> str:
    """'por', 'wakeup' (VLLS wake-up, also by the RESET pin) or 'warm' for the reset in progress (from the RCM)."""
    rcm = find_model(chip, "kinetis.rcm", "RCM")
    reason = getattr(rcm, "last_reason", "por") if rcm is not None else "por"
    if reason.startswith("wakeup"):
        return "wakeup"
    if any(k in reason for k in _WARM):
        return "warm"
    return "por"


_Freezer = Freezer          # shared with the STM32 PWR (chipem/periph/lowpower.py)


@register("kinetis.smc")
class SMC(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"PMSTAT": {"reset": 0x01, "ro_mask": 0xFF}, "PMCTRL": {"ro_mask": 0x18, "on_write": self._pmctrl},
              "PMPROT": {"ro_mask": 0xD5, "on_write": self._pmprot}, "VLLSCTRL": {"ro_mask": 0xC8}}
        super().__init__(name, base, size, build_regs((name, "SMC"), size, ov, params.get("regs_file")),
                         log=log, params=params)
        self.run_mode = "RUN"
        self.mode: str | None = None          # low-power mode while the core sleeps
        self.vlls_mode: int | None = None      # VLLSM of the VLLS mode a wake-up reset comes from
        self.entered_from = "RUN"
        self._pmprot_written = False
        self._powered = False
        self._frz: _Freezer | None = None
        self._saved_gate = None
        self._llwu = self._pmc = self._mcg = self._rcm = None
        self.history: list[tuple[float, str]] = []

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        hooks = getattr(chip, "power_hooks", None)
        if hooks is not None and self not in hooks:
            hooks.append(self)
        self._frz = _Freezer(chip)
        self._llwu = find_model(chip, "kinetis.llwu", "LLWU")
        self._pmc = find_model(chip, "kinetis.pmc", "PMC")
        self._mcg = find_model(chip, "kinetis.mcg", "MCG")
        self._rcm = find_model(chip, "kinetis.rcm", "RCM")

    def reset(self) -> None:
        kind = reset_kind(self.chip) if (self._powered and self.chip is not None) else "por"
        self._powered = True
        if self.mode is not None:
            if self._frz is not None:
                self._frz.drop()
            if self.chip is not None and self._saved_gate is not False:
                self.chip.wake_gate = self._saved_gate
            self._saved_gate = None
        was_vlls = self.mode == "VLLS"
        vllsm = self.get("VLLSCTRL") & 7
        self.mode = None
        if kind == "por":
            super().reset()
            self.vlls_mode = None
        else:
            prot, ctrl, vctrl = self.get("PMPROT"), self.get("PMCTRL"), self.get("VLLSCTRL")
            super().reset()
            # PMCTRL/VLLSCTRL/PMSTAT: Chip POR not VLLS only; RUNM is cleared by any reset ("any reset returns to RUN")
            self.set("PMCTRL", ctrl & 0x87)
            self.set("VLLSCTRL", vctrl)
            if kind == "wakeup":
                self.set("PMPROT", prot)            # PMPROT: Chip Reset not VLLS
                self.vlls_mode = vllsm if was_vlls or self.vlls_mode is None else self.vlls_mode
                if was_vlls:
                    self._vlls_ram_loss(vllsm)
        self._pmprot_written = kind == "wakeup" and bool(self.get("PMPROT"))
        self.run_mode = "RUN"
        self.set("PMSTAT", PMSTAT["RUN"])

    # ------------------------------------------------------------------ registers
    def _pmprot(self, blk, reg, old, new):
        if self._pmprot_written:
            return old                                   # write-once after a reset
        self._pmprot_written = True
        self.set("PMCTRL", self.get("PMCTRL") & ~0x07)   # STOPM cleared by a successful PMPROT write
        return None

    def _allowed_stopm(self, stopm: int) -> bool:
        prot = self.get("PMPROT")
        return {0: True, 2: bool(prot & AVLP), 3: bool(prot & ALLS), 4: bool(prot & AVLLS)}.get(stopm, False)

    def _pmctrl(self, blk, reg, old, new):
        stopm = new & 7
        if not self._allowed_stopm(stopm):
            if stopm not in (0, 2, 3, 4):
                self.emit("warn", block=self.name, msg=f"SMC_PMCTRL: reserved STOPM={stopm} ignored")
            stopm = old & 7
        runm = (new >> 5) & 3
        oldrun = (old >> 5) & 3
        if runm != oldrun:
            if runm == 2 and not (self.get("PMPROT") & AVLP and self.run_mode == "RUN"):
                runm = oldrun                              # blocked (AVLP=0, or not in RUN)
            elif runm not in (0, 2):
                runm = oldrun
            elif runm == 2:
                self._set_run("VLPR")
            else:
                self._set_run("RUN")
        out = (new & LPWUI) | (runm << 5) | (old & STOPA) | stopm
        if (old ^ out) & 0x67:
            self.emit("info", block=self.name, msg=f"SMC_PMCTRL=0x{out:02x} (RUNM={runm}, STOPM={stopm})")
        return out

    def _set_run(self, mode: str) -> None:
        if mode == self.run_mode:
            return
        self.run_mode = mode
        self.set("PMSTAT", PMSTAT[mode])
        self._note(mode)
        if mode == "VLPR" and self._mcg is not None and self._mcg.mode not in ("BLPI", "BLPE"):
            self.emit("warn", block=self.name, msg=f"VLPR entered with the MCG in {self._mcg.mode} (RM 14.4.3.2: "
                      "BLPI with the fast IRC or BLPE required)")

    def _note(self, mode: str) -> None:
        t = self.chip.now_ns() if self.chip is not None else 0.0
        self.history.append((t, mode))
        if len(self.history) > 256:
            del self.history[:128]
        self.emit("info", block=self.name, msg=f"power mode {mode}", power_mode=mode)

    # ------------------------------------------------------------------ power hook (chip.power_hooks)
    def selected_mode(self, deep: bool) -> str:
        if not deep:
            return "VLPW" if self.run_mode == "VLPR" else "WAIT"
        stopm = self.get("PMCTRL") & 7
        if stopm == 0:
            return "VLPS" if self.run_mode == "VLPR" else "STOP"
        return {2: "VLPS", 3: "LLS", 4: "VLLS"}.get(stopm, "STOP")

    def on_sleep(self, chip: Any) -> None:
        deep = bool(getattr(chip.arch, "sleep_deep", False))
        mode = self.selected_mode(deep)
        if deep:
            self.set("PMCTRL", self.get("PMCTRL") & ~STOPA)
            if chip.arch.wakeup_pending():               # 14.4.2.3: interrupt during the entry sequence
                self.set("PMCTRL", self.get("PMCTRL") | STOPA)
                self.mode = None
                return
            if mode == "VLLS" and (self.get("VLLSCTRL") & 7) > 3:
                self.emit("warn", block=self.name, msg="SMC_VLLSCTRL.VLLSM reserved: STOP entered instead")
                mode = "STOP"
            if self._mcg is not None and self._mcg.monitors_enabled():
                self.emit("warn", block=self.name, msg=f"{mode} entered with an MCG clock monitor enabled (RM "
                          "14.4.5: all clock monitors must be disabled first)")
        self.mode = mode
        self.entered_from = self.run_mode
        if mode != "WAIT":
            self.set("PMSTAT", PMSTAT["VLLS" if mode == "VLLS" else mode])
        self._note(mode + (str(self.get("VLLSCTRL") & 7) if mode == "VLLS" else ""))
        self._frz.freeze(self._frozen_blocks(chip, mode), mode)
        if mode in ("LLS", "VLLS"):
            self._saved_gate = chip.wake_gate
            chip.wake_gate = _never
            if mode == "VLLS":
                self.vlls_mode = self.get("VLLSCTRL") & 7
        else:
            self._saved_gate = False                    # marker: gate untouched
        if self._llwu is not None and mode in ("LLS", "VLLS"):
            self._llwu.activate(mode)

    def on_wake(self, chip: Any) -> None:
        mode = self.mode
        if mode is None:
            return
        self.mode = None
        if self._saved_gate is not False:
            chip.wake_gate = self._saved_gate
        self._saved_gate = None
        if self._llwu is not None:
            self._llwu.deactivate()
        self._frz.thaw()
        if self._mcg is not None and mode in ("STOP", "VLPS", "LLS"):
            self._mcg.stop_exit(mode)
        lpwui = bool(self.get("PMCTRL") & LPWUI)
        frm = self.entered_from
        if mode == "WAIT" or mode == "STOP":
            back = "RUN"
        elif mode == "VLPW":
            back = "RUN" if lpwui else "VLPR"
        elif mode == "VLPS":
            back = "RUN" if (frm == "RUN" or lpwui) else "VLPR"
        else:                                            # LLS: back to the run mode it was entered from
            back = frm
        if back == "RUN" and self.run_mode == "VLPR":
            self.set("PMCTRL", self.get("PMCTRL") & ~0x60)  # RUNM cleared by an LPWUI exit
        self.run_mode = back
        self.set("PMSTAT", PMSTAT[back])
        if mode != "WAIT":
            self._note(back)

    def _frozen_blocks(self, chip: Any, mode: str) -> list:
        out = []
        for b in chip.peripherals.values():
            m = getattr(b, "model", None)
            if mode in ("WAIT", "VLPW"):
                if m == "kinetis.wdog" and not (b.get("STCTRLH") & 0x80):        # WAITEN
                    out.append(b)
                elif m == "kinetis.ewm" and mode == "VLPW":
                    out.append(b)
                elif m == "kinetis.dspi":                                         # Doze (MCR.DOZE), 3.9.3.8
                    out.append(b)
                elif m == "kinetis.uart" and "C1" in b.regs and b.get("C1") & 0x40:  # C1.UARTSWAI, 49.1.2.2
                    out.append(b)
                continue
            if mode in ("STOP", "VLPS"):
                if m in _KEEP_STOP:
                    continue
                if m == "kinetis.wdog" and b.get("STCTRLH") & 0x40:               # STOPEN
                    continue
                if m == "kinetis.adc" and (b.get("CFG1") & 3) == 3:              # ADACK keeps converting
                    continue
            elif m in _KEEP_ALWAYS:                                               # LLS / VLLS
                continue
            out.append(b)
        return out

    def _vlls_ram_loss(self, vllsm: int) -> None:
        """SRAM retention (3.5.3.3): VLLS3 all, VLLS2 RAM1 (4 KB at 0x20000000) + RAM2 (12 KB) with RAM2PO, else none."""
        chip = self.chip
        if chip is None or vllsm >= 3:
            return
        keep_lo = keep_hi = 0
        if vllsm == 2:
            keep_lo = 0x20000000
            keep_hi = keep_lo + (0x4000 if self.get("VLLSCTRL") & 0x10 else 0x1000)
        for r in chip.bus.memory_regions():
            if r.kind != "ram":
                continue
            lo, hi = r.base, r.base + r.size
            spans = [(lo, hi)] if keep_hi <= lo or keep_lo >= hi else [(lo, keep_lo), (keep_hi, hi)]
            for a, b in spans:
                if b > a:
                    chip.core.write_mem(a, bytes(b - a))
        chip.core.flush_code_cache()


def _never() -> bool:
    return False


@register("kinetis.pmc")
class PMC(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ack = {"on_write": lambda b, r, o, n: n & ~0x40}
        ov = {"REGSC": {"w1c_mask": 0x08, "ro_mask": 0x04, "on_write": self._regsc}, "LVDSC1": dict(ack),
              "LVDSC2": dict(ack)}
        super().__init__(name, base, size, build_regs((name, "PMC"), size, ov, params.get("regs_file")),
                         log=log, params=params)

    def reset(self) -> None:
        kind = reset_kind(self.chip) if self.chip is not None else "por"
        super().reset()
        if kind == "wakeup":
            self.set("REGSC", self.get("REGSC") | 0x08)      # ACKISO: I/O held until software acknowledges

    def _regsc(self, blk, reg, old, new):
        if old & 0x08 and not new & 0x08:
            self.emit("info", block=self.name, msg="PMC_REGSC.ACKISO cleared: I/O isolation released")
        return None


@register("kinetis.rcm")
class RCM(MMIOBlock):
    POR = (0x82, 0x00)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        super().__init__(name, base, size, build_regs((name, "RCM"), size, None, params.get("regs_file")),
                         log=log, params=params)
        self._cause = self.POR
        self.last_reason = "por"
        self._pin_level = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        log = getattr(chip, "log", None)
        if log is not None and hasattr(log, "subscribe"):
            log.subscribe(self._on_reset_event, kinds=("reset",))
        pins = getattr(chip, "pins", None)
        if pins is not None:
            try:
                p = pins.get("RESET_b")
            except Exception:
                p = None
            if p is not None:
                self._pin_level = p.level()
                pins.input_listeners[p.id].append(self._reset_pin)

    def _reset_pin(self, pin: Any) -> None:
        lvl = pin.level()
        old, self._pin_level = self._pin_level, lvl
        if lvl == "L" and old != "L" and self.chip is not None:
            smc = find_model(self.chip, "kinetis.smc", "SMC")
            low_leak = smc is not None and smc.mode in ("LLS", "VLLS")
            llwu = find_model(self.chip, "kinetis.llwu", "LLWU")
            if low_leak and llwu is not None and not llwu.get("RST") & 0x02:
                return                                   # LLRSTE=0: RESET pin not a low-leakage wake-up
            self.chip.arch.request_reset("wakeup_pin" if low_leak else "pin")

    def _on_reset_event(self, ev: Any) -> None:
        reason = str(ev.get("reason", "") if hasattr(ev, "get") else "").lower()
        self.last_reason = reason
        if reason.startswith("wakeup"):
            self._cause = (0x41, 0x00) if "pin" in reason else (0x01, 0x00)
        elif "watchdog" in reason or "wdog" in reason:
            self._cause = (0x20, 0x00)
        elif "lockup" in reason:
            self._cause = (0x00, 0x02)
        elif "sysresetreq" in reason or "vectreset" in reason or reason in ("sw", "software", "aircr"):
            self._cause = (0x00, 0x04)
        elif "pin" in reason:
            self._cause = (0x40, 0x00)
        elif reason == "loc":
            self._cause = (0x04, 0x00)
        elif reason == "lol":
            self._cause = (0x08, 0x00)
        else:
            self._cause = self.POR

    def reset(self) -> None:
        super().reset()
        s0, s1 = self._cause
        self.set("SRS0", s0)
        self.set("SRS1", s1)
        self._cause = self.POR


# LLWU module inputs (K22 Table 3-x "LLWU inputs"): index -> (model name, kind)
LLWU_MODULES = {0: ("LPTMR0", "lptmr"), 1: ("CMP0", "cmp"), 2: ("CMP1", "cmp"), 3: ("CMP2", "cmp"),
                5: ("RTC", "alarm"), 7: ("RTC", "seconds")}


@register("kinetis.llwu")
class LLWU(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"F1": {"w1c_mask": 0xFF, "on_write": self._flags_w}, "F2": {"w1c_mask": 0xFF, "on_write": self._flags_w},
              "F3": {"ro_mask": 0xFF, "on_read": self._f3_read, "on_peek": self._f3_peek},
              "FILT1": {"w1c_mask": 0x80, "ro_mask": 0x10, "on_write": self._flags_w},
              "FILT2": {"w1c_mask": 0x80, "ro_mask": 0x10, "on_write": self._flags_w},
              "RST": {"ro_mask": 0xFC}}
        super().__init__(name, base, size, build_regs((name, "LLWU"), size, ov, params.get("regs_file")),
                         log=log, params=params)
        self.active: str | None = None
        self.pin_map: dict[int, Any] = {}             # LLWU_Pn -> Pin
        self._levels: dict[int, str] = {}
        self._mflags = 0                               # latched module wake-up flags (F3)
        self._line = False
        self._irq: int | None = None
        self._mods: dict[int, Any] = {}
        self._powered = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, "LLWU")
        pins = getattr(chip, "pins", None)
        if pins is not None:
            pat = re.compile(r"LLWU_P(\d+)$")
            for p in pins.pins:
                for f in [p.name, *(p.functions or {}).values()]:
                    for part in str(f).split("/"):
                        m = pat.match(part.strip())
                        if m:
                            n = int(m.group(1))
                            if n not in self.pin_map:
                                self.pin_map[n] = p
                                pins.input_listeners[p.id].append(self._pin_cb(n))
        per = chip.peripherals
        for idx, (pname, kind) in LLWU_MODULES.items():
            blk = per.get(pname)
            if blk is None:
                continue
            self._mods[idx] = (blk, kind)
            if kind == "seconds":
                if hasattr(blk, "trigger_listeners"):
                    blk.trigger_listeners.append(lambda _b, k, i=idx: k == "seconds" and self._module_event(i))
            elif hasattr(blk, "flag_listeners"):
                # cb(module) whenever the module's interrupt flag may have changed (set or cleared)
                blk.flag_listeners.append(lambda _b, i=idx: self._module_changed(i))

    def reset(self) -> None:
        kind = reset_kind(self.chip) if (self._powered and self.chip is not None) else "por"
        self._powered = True
        self.active = None
        if kind == "wakeup":                          # LLWU registers survive a VLLS wake-up reset
            self._update_line()
            return
        self._mflags = 0
        super().reset()
        self._update_line()

    # ------------------------------------------------------------------ activation (called by the SMC)
    def activate(self, mode: str) -> None:
        self.active = mode
        self._levels = {n: p.level() for n, p in self.pin_map.items()}
        # a module flag still set at entry wakes the chip at once (16.4.3)
        me = self.get("ME")
        for idx in self._mods:
            if me & (1 << idx) and self._module_flag(idx):
                self._wake(f3=1 << idx)
                return

    def deactivate(self) -> None:
        self.active = None

    # ------------------------------------------------------------------ sources
    def _pin_cb(self, n: int):
        def cb(pin):
            old = self._levels.get(n)
            new = pin.level()
            self._levels[n] = new
            if self.active is None or new == old or new not in ("H", "L"):
                return
            rising = new == "H"
            reg = f"PE{n // 4 + 1}"
            cfg = (self.get(reg) >> (2 * (n % 4))) & 3
            f1 = f2 = 0
            if cfg == 3 or (cfg == 1 and rising) or (cfg == 2 and not rising):
                if n < 8:
                    f1 = 1 << n
                else:
                    f2 = 1 << (n - 8)
            filt = 0
            for k, fr in ((1, "FILT1"), (2, "FILT2")):
                v = self.get(fr)
                fe = (v >> 5) & 3
                if (v & 0x0F) == n and (fe == 3 or (fe == 1 and rising) or (fe == 2 and not rising)):
                    filt |= k
            if f1 or f2 or filt:
                self._wake(f1=f1, f2=f2, filt=filt)
        return cb

    def _module_changed(self, idx: int) -> None:
        if self._module_flag(idx):
            self._module_event(idx)
        elif self._mflags & (1 << idx):
            self._update_line()

    def _module_event(self, idx: int) -> None:
        if self.active is None or not self.get("ME") & (1 << idx):
            return
        self._wake(f3=1 << idx)

    def _module_flag(self, idx: int) -> bool:
        blk, kind = self._mods.get(idx, (None, None))
        if blk is None:
            return False
        if kind == "lptmr":
            return bool(blk.get("CSR") & 0x80)
        if kind == "cmp":
            return bool(blk.get("SCR") & 0x06)
        if kind == "alarm":
            return bool(blk.get("SR") & 0x04)
        return False                                  # seconds: a pulse, no flag in the RTC

    def _wake(self, f1: int = 0, f2: int = 0, filt: int = 0, f3: int = 0) -> None:
        if f1:
            self.set("F1", self.get("F1") | f1)
        if f2:
            self.set("F2", self.get("F2") | f2)
        for k in (1, 2):
            if filt & k:
                self.set(f"FILT{k}", self.get(f"FILT{k}") | 0x80)
        self._mflags |= f3
        mode = self.active
        self.active = None
        self.emit("info", block=self.name, msg=f"LLWU wake-up from {mode} (F1=0x{self.get('F1'):02x} "
                  f"F2=0x{self.get('F2'):02x} F3=0x{self._f3():02x})")
        chip = self.chip
        if mode == "VLLS":
            chip.arch.request_reset("wakeup")
        else:
            self._update_line()
            chip.wake()

    # ------------------------------------------------------------------ flags / IRQ
    def _f3(self) -> int:
        out = 0
        for idx in range(8):
            if self._mflags & (1 << idx):
                if LLWU_MODULES.get(idx, (None, ""))[1] == "seconds" or self._module_flag(idx):
                    out |= 1 << idx
                else:
                    self._mflags &= ~(1 << idx)       # the module flag was cleared in the module
        return out

    def _f3_peek(self, blk, reg):
        return sum(1 << i for i in range(8) if self._mflags >> i & 1 and (
            LLWU_MODULES.get(i, (None, ""))[1] == "seconds" or self._module_flag(i)))

    def _f3_read(self, blk, reg):
        v = self._f3()
        self._mflags &= ~0x80                         # RTC seconds has no flag of its own: reading F3 consumes it
        if not v & ~0x80:
            self._update_line()
        return v

    def _flags_w(self, blk, reg, old, new):
        self._update_line()
        return None

    def pending(self) -> bool:
        return bool(self.get("F1") or self.get("F2") or self.get("FILT1") & 0x80 or self.get("FILT2") & 0x80
                    or self._f3())

    def _update_line(self) -> None:
        lvl = self.pending()
        if lvl != self._line:
            self._line = lvl
            if self._irq is not None and self.chip is not None:
                self.irq(self._irq, lvl)

    def poll(self) -> None:
        """Re-evaluate the LLWU IRQ after a module flag was cleared in its module."""
        self._update_line()


@register("kinetis.vbat")
class VBAT(MMIOBlock):
    """32-byte VBAT register file (the whole block is plain RAM). Battery-backed: chip.reset() keeps it."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, (), ram_like=True, log=log, params=params)
        self.log_unknown = False

    def reset(self) -> None:
        self._seen.clear()          # contents survive every reset (cleared only by power-on = new Chip)

    def power_on_reset(self) -> None:
        self.mem[:] = bytes(self.size)
