"""STM32F1 RTC (RM0008 ch. 18 layout: CRH/CRL, 20-bit prescaler PRL, divider DIV, 32-bit counter CNT, alarm ALR).

Not the F4 calendar RTC.  Not verified against RM0008 (not available when written); behaviour as documented for the
F1 family and used by ST's HAL/SPL drivers.

* Clock: RTCCLK = rcc.rtc_clock_hz() (BDCR.RTCSEL: LSE 32.768 kHz, LSI 40 kHz, HSE/128) while BDCR.RTCEN = 1.  The
  divider DIV counts RTCCLK down from PRL to 0; on the next RTCCLK edge it reloads PRL and the time base TR_CLK ticks:
  CNT += 1, CRL.SECF is set, CNT == ALR sets CRL.ALRF, CNT wrapping to 0 sets CRL.OWF.  Counting is lazy (CNT/DIV are
  computed from the time) and only the interesting ticks are scheduled (every tick when SECIE, else the alarm and the
  overflow).  The backup-domain core (PRL, DIV, CNT, ALR) keeps counting over system resets and Standby; only a
  backup domain reset (RCC_BDCR.BDRST) resets it.  CRH/CRL are reset by every system reset (CRL = 0x0020).
* Configuration: PRL/CNT/ALR can only be written with CRL.CNF = 1 (else the write is ignored, one `warn`); the
  values are latched when CNF goes back to 0 (the "write operation"), which keeps CRL.RTOFF = 0 for 3 RTCCLK
  periods; a write to any RTC register while RTOFF = 0 is ignored (one `warn`).  PRLH/PRLL and ALRH/ALRL are
  write-only (read 0).  All RTC writes also need PWR_CR.DBP = 1 (backup domain write protection).
* CRL.SECF/ALRF/OWF are rc_w0 (write 0 clears).  CRL.RSF (rc_w0) is set by hardware at the next RTCCLK edge after
  software cleared it (firmware waits for it after a reset before reading CNT).
* Interrupts: the RTC vector = (SECF & SECIE) | (ALRF & ALRIE) | (OWF & OWIE) (level); the alarm also pulses EXTI
  line 17 (RTC_Alarm vector when EXTI is configured; Stop/Standby wake-up), whatever ALRIE says.
* BKP_RTCCR calibration (CAL) is not applied (no pulse masking).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, exti_line, find_model, irq_number, rcc

SECIE, ALRIE, OWIE = 1, 2, 4
SECF, ALRF, OWF, RSF, CNF, RTOFF = 1, 2, 4, 8, 16, 32
CORE = ("PRLH", "PRLL", "CNTH", "CNTL", "ALRH", "ALRL")


@register("stm32f1.rtc")
class RTC(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = None                    # on APB1, no own enable bit (needs PWR/BKP clocks + DBP)
        z = lambda b, r: 0                  # noqa: E731
        ov: dict[str, dict] = {
            "CRH": {"on_write": self._crh_write, "ro_mask": 0xFFFFFFF8},
            "CRL": {"on_write": self._crl_write, "on_read": self._crl_read, "ro_mask": 0xFFFFFFC0},
            "PRLH": {"on_write": self._core_write, "on_read": z, "ro_mask": 0xFFFFFFF0},
            "PRLL": {"on_write": self._core_write, "on_read": z, "ro_mask": 0xFFFF0000},
            "DIVH": {"on_read": self._div_read(True), "ro_mask": 0xFFFFFFFF},
            "DIVL": {"on_read": self._div_read(False), "ro_mask": 0xFFFFFFFF},
            "CNTH": {"on_write": self._core_write, "on_read": self._cnt_read(True), "ro_mask": 0xFFFF0000},
            "CNTL": {"on_write": self._core_write, "on_read": self._cnt_read(False), "ro_mask": 0xFFFF0000},
            "ALRH": {"on_write": self._core_write, "on_read": z, "ro_mask": 0xFFFF0000},
            "ALRL": {"on_write": self._core_write, "on_read": z, "ro_mask": 0xFFFF0000},
        }
        r = build_regs((name, "RTC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._powered = False
        # backup-domain core state
        self.prl = 0x8000
        self.alr = 0xFFFFFFFF
        self._cnt0 = 0                      # CNT at the anchor
        self._div0 = 0x8000                 # DIV at the anchor
        self._t0 = 0.0                      # anchor time (ns)
        self._hz = 0.0                      # RTCCLK while counting (0: stopped)
        self._pend: dict[str, int] = {}     # PRL/CNT/ALR halves written in configuration mode
        self._busy_until = -1.0             # RTOFF = 0 until then (write operation in progress)
        self._rsf_at: float | None = None   # time software last cleared RSF (None: reset; RSF follows at once)
        self._ev = None
        self._irqn: int | None = None
        self._line = False
        self._warned: set[str] = set()
        self._pwr: Any = None
        self.alarms = 0
        self._sec_cnt = 0                   # CNT when SECF was last brought up to date

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "RTC")
        self._pwr = find_model(chip, "stm32f1.pwr", "PWR")
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._clock_changed)

    def reset(self) -> None:
        """System reset: CRH/CRL to reset values; the counter (backup domain) keeps running."""
        if not self._powered:
            self._powered = True
            self._core_reset()
        self._cancel()
        super().reset()
        self._pend.clear()
        self._busy_until = -1.0
        self._rsf_at = self.chip.now_ns() if self.chip is not None else None   # RSF = 0 after a system reset
        self._warned.clear()
        if self._line and self.chip is not None:
            self.irq(self._irqn, False)
        self._line = False
        self._clock_changed()
        self._sec_cnt = self.state()[0] if self.chip is not None else 0
        self._schedule()

    def backup_domain_reset(self) -> None:
        self._cancel()
        self._core_reset()
        MMIOBlock.reset(self)
        self._clock_changed()

    def _core_reset(self) -> None:
        self.prl = 0x8000
        self.alr = 0xFFFFFFFF
        self._cnt0 = 0
        self._div0 = 0x8000
        self._t0 = self.chip.now_ns() if self.chip is not None else 0.0
        self._hz = 0.0

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def write(self, offset: int, size: int, value: int) -> None:
        if self.chip is not None and not self._writable(offset):
            return
        MMIOBlock.write(self, offset, size, value)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _writable(self, offset: int) -> bool:
        p = self._pwr
        if p is not None and not p.get("CR") & (1 << 8):
            self._warn_once("dbp", "RTC write ignored: backup domain write protection (set PWR_CR.DBP first)")
            return False
        if self.chip.now_ns() < self._busy_until:
            self._warn_once("rtoff", "RTC write ignored: previous write operation in progress (CRL.RTOFF = 0)")
            return False
        return True

    # ------------------------------------------------------------ counting
    def rtcclk_hz(self) -> float:
        r = rcc(self.chip) if self.chip is not None else None
        if r is None:
            return 32768.0
        if not r.get("BDCR") & (1 << 15):          # RTCEN
            return 0.0
        return r.rtc_clock_hz()

    def _ticks(self, now: float) -> int:
        return int((now - self._t0) * self._hz / 1e9) if self._hz > 0 else 0

    def state(self, now: float | None = None) -> tuple[int, int]:
        """(CNT, DIV) at `now` (default: the current instruction)."""
        if now is None:
            now = self.chip.now_ns() if self.chip is not None else 0.0
        n = self._ticks(now)
        if n <= self._div0:
            return self._cnt0, self._div0 - n
        m = n - (self._div0 + 1)
        p = self.prl + 1
        return (self._cnt0 + 1 + m // p) & 0xFFFFFFFF, self.prl - (m % p)

    def _tick_time(self, k: int) -> float:
        """Time of the k-th TR_CLK tick after the anchor (k >= 1)."""
        ticks = self._div0 + 1 + (k - 1) * (self.prl + 1)
        return self._t0 + ticks * 1e9 / self._hz

    def _reanchor(self, cnt: int | None = None, div: int | None = None) -> None:
        now = self.chip.now_ns() if self.chip is not None else 0.0
        c, d = self.state(now)
        self._cnt0 = c if cnt is None else cnt & 0xFFFFFFFF
        self._div0 = d if div is None else div
        self._t0 = now
        self._hz = self.rtcclk_hz()
        if cnt is not None:
            self._sec_cnt = self._cnt0

    def _clock_changed(self) -> None:
        if self.chip is None:
            return
        hz = self.rtcclk_hz()
        if hz != self._hz:
            self._reanchor()
            self._hz = hz
            self._schedule()

    def _schedule(self) -> None:
        self._cancel()
        if self.chip is None or self._hz <= 0:
            return
        cnt, _ = self.state()
        crh = self.get("CRH")
        if crh & SECIE:
            k = 1
        else:
            to_alarm = (self.alr - cnt) & 0xFFFFFFFF or 0x100000000
            to_ovf = (0 - cnt) & 0xFFFFFFFF or 0x100000000
            k = min(to_alarm, to_ovf)
        k_abs = (cnt - self._cnt0) & 0xFFFFFFFF
        t = self._tick_time(k_abs + k)
        self._ev = self.chip.sched.at_ns(t, self._tick)

    def _tick(self) -> None:
        self._ev = None
        cnt, _ = self.state()
        self._sec_cnt = cnt
        crl = self.get("CRL") | SECF
        if cnt == self.alr:
            crl |= ALRF
            self.alarms += 1
            exti_line(self.chip, 17, True)
            exti_line(self.chip, 17, False)
            self.emit("info", block=self.name, msg=f"RTC alarm (CNT = {cnt})", cnt=cnt)
        if cnt == 0:
            crl |= OWF
        self.set("CRL", crl)
        self._update_irq()
        self._schedule()

    def _update_irq(self) -> None:
        crl, crh = self.get("CRL"), self.get("CRH")
        lvl = bool(crl & crh & 7)
        if lvl != self._line and self.chip is not None and self._irqn is not None:
            self._line = lvl
            self.irq(self._irqn, lvl)

    # ------------------------------------------------------------ registers
    def _crh_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update_irq()
        if (old ^ new) & SECIE:
            self._schedule()
        return None

    def _crl_read(self, blk, reg):
        now = self.chip.now_ns() if self.chip is not None else 0.0
        v = self.get("CRL")
        if self._hz > 0:
            cnt, _ = self.state(now)
            if cnt != self._sec_cnt:                 # ticks not scheduled one by one (SECIE = 0) still set SECF
                self._sec_cnt = cnt
                v |= SECF
                self.set("CRL", v)
        if not v & RSF:                              # set by the next RTCCLK resynchronisation after a clear
            hz = self.rtcclk_hz()
            if hz > 0 and (self._rsf_at is None or now >= self._rsf_at + 1e9 / hz):
                v |= RSF
                self.set("CRL", v)
        v &= ~RTOFF
        if now >= self._busy_until:
            v |= RTOFF
        return v

    def _crl_write(self, blk, reg, old, new):
        # SECF/ALRF/OWF/RSF are rc_w0: writing 1 keeps the flag, 0 clears it
        flags = old & new & (SECF | ALRF | OWF | RSF)
        v = (new & CNF) | flags | (old & RTOFF)
        if not new & RSF and self.chip is not None:
            self._rsf_at = self.chip.now_ns()
        self._store(reg, v)
        if old & CNF and not new & CNF:
            self._commit()
        self._update_irq()
        return v

    def _core_write(self, blk, reg, old, new):
        if not self.get("CRL") & CNF:
            self._warn_once("cnf", f"RTC_{reg.name} write ignored: not in configuration mode (CRL.CNF = 0)")
            return old
        self._pend[reg.name] = new & (0xF if reg.name == "PRLH" else 0xFFFF)
        return 0 if reg.name in ("PRLH", "PRLL", "ALRH", "ALRL") else None

    def _cnt_read(self, high: bool):
        def rd(blk, reg):
            cnt, _ = self.state()
            return (cnt >> 16) & 0xFFFF if high else cnt & 0xFFFF
        return rd

    def _div_read(self, high: bool):
        def rd(blk, reg):
            _, div = self.state()
            return (div >> 16) & 0xF if high else div & 0xFFFF
        return rd

    def _commit(self) -> None:
        """CNF 1 -> 0: the write operation copies the configuration-mode writes into the RTC core."""
        p = self._pend
        if not p:
            return
        cnt, div = self.state()
        if "PRLH" in p or "PRLL" in p:
            self.prl = (p.get("PRLH", self.prl >> 16) << 16) | p.get("PRLL", self.prl & 0xFFFF)
            div = self.prl                       # the divider restarts from the new reload value
        if "CNTH" in p or "CNTL" in p:
            cnt = (p.get("CNTH", cnt >> 16) << 16) | p.get("CNTL", cnt & 0xFFFF)
        if "ALRH" in p or "ALRL" in p:
            self.alr = (p.get("ALRH", self.alr >> 16) << 16) | p.get("ALRL", self.alr & 0xFFFF)
        p.clear()
        self._reanchor(cnt, div)
        hz = self._hz or self.rtcclk_hz()
        if self.chip is not None:
            self._busy_until = self.chip.now_ns() + (3e9 / hz if hz > 0 else 0.0)
        self._schedule()
