"""STM32F4 real-time clock and backup registers (RTC, RM0090 ch. 26), backup domain.

Registers from the SVD (TR, DR, CR, ISR, PRER, WUTR, CALIBR, ALRMAR, ALRMBR, WPR, SSR, SHIFTR, TSTR, TSDR, TSSSR,
CALR, TAFCR, ALRMASSR, ALRMBSSR, BKP0R-BKP19R); behaviour hand-written.
* Clock (RCC_BDCR): RTCCLK = LSE 32.768 kHz (RTCSEL = 01, needs LSEON; `params.lse_hz`), LSI 32 kHz (10, needs
  RCC_CSR.LSION; `params.lsi_hz`) or HSE / RTCPRE (11, RCC_CFGR.RTCPRE >= 2, needs HSEON), gated by BDCR.RTCEN.
  Without a clock the calendar stands still. ck_apre = RTCCLK / (PREDIV_A + 1), ck_spre = ck_apre / (PREDIV_S + 1)
  (PRER, reset 127/255: 1 Hz from LSE). Time runs in the wall-clock domain (`sched.at_ns`). RCC_BDCR changes
  (RTCSEL, RTCEN, LSEON, BDRST) are picked up at the next RTC register access or RTC event (the RCC model has no
  backup-domain listener).
* Calendar: TR (hour/minute/second BCD, PM), DR (year/month/date BCD, weekday WDU 1-7), SSR (PREDIV_S down-counter:
  SSR = PREDIV_S at a second boundary). The calendar counts from power-on (00:00:00, Monday 2000-01-01) whenever the
  clock runs and INIT = 0, with leap years (year % 4 == 0), 28-31 day months and a weekday that steps once a day.
  CR.FMT = 1 reads hours 1-12 with TR.PM. CR.ADD1H / SUB1H (write-only) move the time by one hour.
* Shadow registers (26.3.6): with CR.BYPSHAD = 0 a read of SSR or TR freezes the calendar values seen by the next
  TR/DR reads until DR is read (the "read TR then DR" rule), and ISR.RSF (rc_w0) is set again 2 RTCCLK periods
  after software clears it (HAL_RTC_WaitForSynchro). BYPSHAD = 1 reads the live counters and RSF reads 0 (cleared by
  hardware, 26.6.4); clearing BYPSHAD sets RSF 2 RTCCLK later. A system reset clears ISR.INIT/INITF/RSF and resets
  the shadow registers (TR 0, DR 0x2101, SSR 0 while BYPSHAD = 0, 26.3.7 / 26.6.1-2): they read those values until
  the first copy, which sets RSF 2 RTCCLK after the reset (an INIT in progress is left as if INIT were cleared).
* Write protection (26.3.5): after a backup-domain reset all registers except ISR[13:8], TAFCR and BKPxR are protected until WPR
  receives 0xCA then 0x53 (any other value locks again; a system reset does not relock); protected writes are
  ignored with one `warn` per register.
  The whole backup domain also needs PWR_CR.DBP = 1 (26.3.1; RM0090 5.1.2): without it every RTC write is ignored
  with one `warn`. RCC_BDCR itself is written without the DBP check (stm32.rcc).
* Initialisation (26.3.5): ISR.INIT = 1 stops the calendar; INITF reads 1 two RTCCLK periods later (never without
  RTCCLK, so HAL_RTC_Init times out like on silicon); TR, DR, PRER and CR.FMT are writable only while INITF = 1
  (otherwise ignored, one `warn`); clearing INIT loads TR/DR and the counters restart 4 RTCCLK later with
  SSR = PREDIV_S (RSF set at that load). ISR.INITS = 1 when the year is not 0.
* Alarms A/B (26.3.9): ALRMxR with MSK1-MSK4 (seconds, minutes, hours/PM, date or weekday per WDSEL); when the
  calendar's second increments to a matching time ALRxF is set. ALRMxR is writable while ALRxWF = 1: it is set 2
  RTCCLK after ALRxE is cleared (26.3.5), cleared at once when ALRxE is set and in initialisation mode. ALRMASSR/ALRMBSSR.MASKSS other than 0 (sub-second compare) are treated as 0 with one `warn`.
  (ALRAF & ALRAIE) | (ALRBF & ALRBIE) drives EXTI line 17 (RTC_Alarm vector via EXTI_IMR/RTSR); flags are rc_w0.
* Wakeup timer (26.3.4): CR.WUTE starts a down-counter reloaded with WUTR; ck_wut = RTCCLK / 16, 8, 4, 2
  (WUCKSEL 0xx) or ck_spre (10x; 11x adds 2^16 to WUTR); WUTF is set every WUTR + 1 ck_wut periods. WUTR and
  CR.WUCKSEL are writable while WUTWF = 1 (set 2 RTCCLK after WUTE is cleared, cleared when WUTE is set). WUTF & WUTIE drives EXTI line 22 (RTC_WKUP).
* Backup domain: all RTC registers, the calendar and BKP0R-BKP19R survive system resets (watchdog, SYSRESETREQ, NRST);
  only the first reset (power-on) and RCC_BDCR.BDRST clear them.
The SVD lacks CR.BYPSHAD (bit 5) and CR.COSEL (bit 19): CR keeps all bits 23:0 (26.6.3).
Not modelled: timestamp (26.3.12: TSE, TSTR/TSDR/TSSSR), tamper (26.3.13: TAFCR TAMPxE; TAMP1F/TAMP2F never set),
coarse/smooth calibration (26.3.10/26.3.11: CALIBR, CALR stored, RECALPF never set), SHIFTR (26.3.8: stored, one
`warn`, SHPF never set), the RTC_AF1 outputs (26.3.14/26.3.15: COE, OSEL stored), the reference clock detection
(26.3.9: REFCKON), the 2-APB-cycle delay of rc_w0 flag clears, the APB1 >= 7 x RTCCLK read rule (26.3.6).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, exti_input, find_model

# CR
FMT, BYPSHAD, ALRAE, ALRBE, WUTE = 1 << 6, 1 << 5, 1 << 8, 1 << 9, 1 << 10
ALRAIE, ALRBIE, WUTIE, ADD1H, SUB1H = 1 << 12, 1 << 13, 1 << 14, 1 << 16, 1 << 17
# ISR
ALRAWF, ALRBWF, WUTWF, SHPF, INITS, RSF, INITF, INIT = (1 << b for b in range(8))
ALRAF, ALRBF, WUTF, TSF, TSOVF, TAMP1F, TAMP2F = (1 << b for b in range(8, 15))
ISR_FLAGS = ALRAF | ALRBF | WUTF | TSF | TSOVF | TAMP1F | TAMP2F
# RCC_BDCR
LSEON, RTCEN, BDRST = 1, 1 << 15, 1 << 16
UNPROTECTED = {"WPR", "TAFCR"} | {f"BKP{i}R" for i in range(20)}
EPS = 1e-3                      # tolerance (in prescaler ticks) when an event lands on a tick boundary


def _bcd(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def _unbcd(v: int) -> int:
    return (v >> 4) * 10 + (v & 0xF)


class Cal:
    """Calendar value: year 0-99 (2000-2099), month, day, weekday 1-7, hours 0-23, minutes, seconds."""
    __slots__ = ("yy", "mo", "dd", "wd", "h", "m", "s")

    def __init__(self, yy=0, mo=1, dd=1, wd=1, h=0, m=0, s=0):
        self.yy, self.mo, self.dd, self.wd, self.h, self.m, self.s = yy, mo, dd, wd, h, m, s

    def copy(self) -> "Cal":
        return Cal(self.yy, self.mo, self.dd, self.wd, self.h, self.m, self.s)

    def add_seconds(self, n: int) -> "Cal":
        if n == 0:
            return self.copy()
        try:
            d0 = _dt.datetime(2000 + self.yy, self.mo, self.dd, self.h, self.m, self.s)
        except ValueError:
            return self.copy()                   # invalid date written by firmware: frozen
        d1 = d0 + _dt.timedelta(seconds=n)
        days = (d1.date() - d0.date()).days
        wd = ((self.wd - 1 + days) % 7) + 1 if self.wd else 0
        return Cal((d1.year - 2000) % 100, d1.month, d1.day, wd, d1.hour, d1.minute, d1.second)

    def tuple(self) -> tuple:
        return (self.yy, self.mo, self.dd, self.wd, self.h, self.m, self.s)

    def __repr__(self) -> str:
        return f"20{self.yy:02d}-{self.mo:02d}-{self.dd:02d} (wd {self.wd}) {self.h:02d}:{self.m:02d}:{self.s:02d}"


@register("stm32.rtc")
class RTC(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {
            "TR": {"on_read": self._tr_read, "on_write": self._cal_write},
            "DR": {"on_read": self._dr_read, "on_write": self._cal_write},
            "SSR": {"ro_mask": 0xFFFFFFFF, "on_read": self._ssr_read},
            # The SVD lacks CR.BYPSHAD (bit 5) and COSEL (bit 19); RM0090 26.6.3 reserves only bits 31:24
            "CR": {"on_write": self._cr_write, "ro_mask": 0xFF000000},
            "ISR": {"ro_mask": 0, "on_read": self._isr_read, "on_write": self._isr_write},
            "PRER": {"on_write": self._prer_write},
            "WUTR": {"on_write": self._wutr_write},
            "ALRMAR": {"on_write": self._alrm_write}, "ALRMBR": {"on_write": self._alrm_write},
            "ALRMASSR": {"on_write": self._alrm_write}, "ALRMBSSR": {"on_write": self._alrm_write},
            "WPR": {"on_write": self._wpr_write, "on_read": lambda b, r: 0},
            "SHIFTR": {"on_write": self._shift_write},
        }
        r = build_regs((name, "RTC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.lse_hz = float(params.get("lse_hz", 32768.0))
        self.lsi_hz = float(params.get("lsi_hz", 32000.0))
        self._powered = False
        self._init_state()

    def _init_state(self) -> None:
        self.cal = Cal()                # calendar at _t_ref
        self._ss_ref = 0x00FF           # SSR at _t_ref
        self._t_ref = 0.0
        self._hz = 0.0                  # RTCCLK the current reference was computed with
        self._shadow: tuple[Cal, int] | None = None
        self._unlock = 0                # WPR key state: 0 locked, 1 got 0xCA, 2 unlocked
        self._rsf_due: float | None = None
        self._initf_due = 0.0           # INITF reads 1 from this time on while INIT = 1
        self._wf_due = {ALRAWF: 0.0, ALRBWF: 0.0, WUTWF: 0.0}   # xWF reads 1 from this time on (None: enabled)
        self._shadow_rst = False        # shadow TR/DR/SSR hold their system-reset values until the next copy
        self._sec_ev = None
        self._wut_ev = None
        self._warned: set[str] = set()
        self._bdcr_seen = 0
        self._exti = {17: False, 22: False}
        self.alarms = [0, 0]
        self.wakeups = 0

    # ------------------------------------------------------------------ reset (backup domain)
    def reset(self) -> None:
        if self._powered:
            self._system_reset()         # backup domain retained (calendar, events, BKPxR, WPR key state)
            return
        self._powered = True
        self._full_reset()

    def _system_reset(self) -> None:
        """26.3.7 / 26.6.4: a system reset clears ISR.INIT, INITF and RSF and resets the calendar shadow registers
        (TR 0, DR 0x2101, SSR 0 while BYPSHAD = 0) until the next shadow copy (1-2 RTCCLK later)."""
        if self.chip is None:
            return
        self._sync()
        isr = self.get("ISR")
        if isr & INIT:
            self._exit_init()
        self.set("ISR", self.get("ISR") & ~(INIT | INITF | RSF))
        self._shadow = None
        self._shadow_rst = True
        self._rsf_due = self._after_rtcclk(2)
        self._reschedule()
        self._start_wut()

    def _full_reset(self) -> None:
        for attr in ("_sec_ev", "_wut_ev"):
            ev = getattr(self, attr, None)
            if ev is not None and self.chip is not None:
                self.chip.sched.cancel(ev)
        super().reset()
        self._init_state()
        self.set("ISR", self.get("ISR") & ~(INIT | INITF | RSF))
        if self.chip is not None:
            self._t_ref = self.chip.now_ns()
            self._hz = self._clock_hz()
            for line in (17, 22):
                exti_input(self.chip, line, False)

    # ------------------------------------------------------------------ clock
    def _clock_hz(self) -> float:
        r = self._rcc
        if r is None:
            return self.lse_hz
        bd = r.get("BDCR")
        if not bd & RTCEN or bd & BDRST:
            return 0.0
        sel = (bd >> 8) & 3
        if sel == 1:
            return self.lse_hz if bd & LSEON else 0.0
        if sel == 2:
            return self.lsi_hz if r.get("CSR") & 1 else 0.0
        if sel == 3:
            pre = (r.get("CFGR") >> 16) & 0x1F
            return r.ext_hz / pre if pre >= 2 and r.get("CR") & (1 << 16) else 0.0
        return 0.0

    def _prediv(self) -> tuple[int, int]:
        p = self.get("PRER")
        return (p >> 16) & 0x7F, p & 0x7FFF

    def _apre_hz(self) -> float:
        a, _ = self._prediv()
        return self._hz / (a + 1)

    def _running(self) -> bool:
        return self._hz > 0 and not self.get("ISR") & INIT

    def _after_rtcclk(self, n: int) -> float:
        """Time n RTCCLK periods from now (inf without a clock: the synchronisation never completes)."""
        return self._now() + n * 1e9 / self._hz if self._hz > 0 else float("inf")

    def _due(self, t: float | None) -> bool:
        return t is not None and self._now() >= t

    def _sync(self) -> None:
        """Pick up RCC_BDCR changes (clock source, enable, backup-domain reset)."""
        r = self._rcc
        if r is not None:
            bd = r.get("BDCR")
            if bd & BDRST and not self._bdcr_seen & BDRST:
                self._bdcr_seen = bd
                self.emit("info", block=self.name, msg="backup domain reset (RCC_BDCR.BDRST): RTC and BKPxR cleared")
                self._full_reset()
                self._bdcr_seen = bd
                return
            self._bdcr_seen = bd
        hz = self._clock_hz()
        if hz != self._hz:
            self._rebase()
            self._hz = hz
            if hz > 0:                           # pending synchronisations complete now that RTCCLK runs
                now = self._now()
                for k, t in self._wf_due.items():
                    if t == float("inf"):
                        self._wf_due[k] = now + 2e9 / hz
                if self._initf_due == float("inf"):
                    self._initf_due = now + 2e9 / hz
                if self._rsf_due == float("inf"):
                    self._rsf_due = now + 2e9 / hz
            self._reschedule()
            self._start_wut()

    # ------------------------------------------------------------------ calendar
    def _now(self) -> float:
        return self.chip.now_ns() if self.chip is not None else 0.0

    def snapshot(self) -> tuple[Cal, int]:
        """(calendar, SSR) now."""
        if not self._running():
            return self.cal.copy(), self._ss_ref
        _, s = self._prediv()
        ticks = int((self._now() - self._t_ref) * self._apre_hz() / 1e9 + EPS)
        pos = (s - self._ss_ref) + max(ticks, 0)
        secs, rem = divmod(pos, s + 1)
        return self.cal.add_seconds(secs), s - rem

    def _rebase(self) -> None:
        if self.chip is None:
            return
        self.cal, self._ss_ref = self.snapshot()
        self._t_ref = self._now()

    def _next_second_ns(self) -> float | None:
        """Time of the next ck_spre edge (second increment) after now."""
        if not self._running():
            return None
        _, s = self._prediv()
        f = self._apre_hz()
        ticks = int((self._now() - self._t_ref) * f / 1e9 + EPS)
        pos = (s - self._ss_ref) + max(ticks, 0)
        nxt = (pos // (s + 1) + 1) * (s + 1)
        return self._t_ref + (nxt - (s - self._ss_ref)) * 1e9 / f

    def now_calendar(self) -> Cal:
        return self.snapshot()[0]

    # ------------------------------------------------------------------ encode / decode
    def _enc_tr(self, c: Cal) -> int:
        h, pm = c.h, 0
        if self.get("CR") & FMT:
            pm = 1 if h >= 12 else 0
            h = h % 12 or 12
        return (pm << 22) | (_bcd(h) << 16) | (_bcd(c.m) << 8) | _bcd(c.s)

    @staticmethod
    def _enc_dr(c: Cal) -> int:
        return (_bcd(c.yy) << 16) | ((c.wd & 7) << 13) | (_bcd(c.mo) << 8) | _bcd(c.dd)

    def _dec(self, tr: int, dr: int) -> Cal:
        h = _unbcd((tr >> 16) & 0x3F)
        if self.get("CR") & FMT:
            h = (h % 12) + (12 if tr >> 22 & 1 else 0)
        return Cal(_unbcd((dr >> 16) & 0xFF), _unbcd((dr >> 8) & 0x1F), _unbcd(dr & 0x3F), (dr >> 13) & 7, h,
                   _unbcd((tr >> 8) & 0x7F), _unbcd(tr & 0x7F))

    # ------------------------------------------------------------------ alarms / wakeup
    def _alarm_match(self, which: int, c: Cal) -> bool:
        a = self.get("ALRMAR" if which == 0 else "ALRMBR")
        if not a & 0x80 and _unbcd(a & 0x7F) != c.s:
            return False
        if not a & 0x8000 and _unbcd((a >> 8) & 0x7F) != c.m:
            return False
        if not a & 0x800000:
            h = _unbcd((a >> 16) & 0x3F)
            if self.get("CR") & FMT:
                h = (h % 12) + (12 if a >> 22 & 1 else 0)
            if h != c.h:
                return False
        if not a & 0x80000000:
            if a >> 30 & 1:
                if (a >> 24) & 0xF != c.wd:
                    return False
            elif _unbcd((a >> 24) & 0x3F) != c.dd:
                return False
        return True

    def _reschedule(self) -> None:
        if self.chip is None:
            return
        sched = self.chip.sched
        cr = self.get("CR")
        if self._sec_ev is not None:
            sched.cancel(self._sec_ev)
            self._sec_ev = None
        if cr & (ALRAE | ALRBE):
            t = self._next_second_ns()
            if t is not None:
                self._sec_ev = sched.at_ns(t, self._second)
        if self._wut_ev is not None and not (cr & WUTE and self._hz > 0):
            sched.cancel(self._wut_ev)
            self._wut_ev = None

    def _second(self) -> None:
        self._sec_ev = None
        self._sync()
        if self._sec_ev is not None:            # _sync re-planned
            return
        c, _ = self.snapshot()
        cr = self.get("CR")
        flags = 0
        for i, (en, fl) in enumerate(((ALRAE, ALRAF), (ALRBE, ALRBF))):
            if cr & en and self._alarm_match(i, c):
                flags |= fl
                self.alarms[i] += 1
                self.emit("info", block=self.name, msg=f"RTC alarm {'AB'[i]} at {c!r}")
        if flags:
            self.set("ISR", self.get("ISR") | flags)
            self._update_lines()
        self._reschedule()

    def _wut_period_ns(self) -> tuple[float, bool]:
        """(period in ns, aligned to ck_spre)."""
        cr = self.get("CR")
        sel = cr & 7
        w = self.get("WUTR") & 0xFFFF
        if sel < 4:
            div = (16, 8, 4, 2)[sel]
            return (w + 1) * div * 1e9 / self._hz, False
        n = w + 1 + (0x10000 if sel >= 6 else 0)
        a, s = self._prediv()
        return n * (a + 1) * (s + 1) * 1e9 / self._hz, True

    def _start_wut(self) -> None:
        if self._wut_ev is not None:
            self.chip.sched.cancel(self._wut_ev)
            self._wut_ev = None
        if self._hz <= 0 or not self.get("CR") & WUTE:
            return
        per, spre = self._wut_period_ns()
        if spre:
            t1 = self._next_second_ns()
            if t1 is None:
                return
            sec_ns = per / ((self.get("WUTR") & 0xFFFF) + 1 + (0x10000 if (self.get("CR") & 7) >= 6 else 0))
            t = t1 + per - sec_ns
        else:
            t = self._now() + per
        self._wut_ev = self.chip.sched.at_ns(t, self._wakeup, per)

    def _wakeup(self, per: float) -> None:
        self._wut_ev = None
        if not self.get("CR") & WUTE or self._hz <= 0:
            return
        self.wakeups += 1
        self.set("ISR", self.get("ISR") | WUTF)
        self._update_lines()
        self._wut_ev = self.chip.sched.in_ns(per, self._wakeup, per)

    def _update_lines(self) -> None:
        isr, cr = self.get("ISR"), self.get("CR")
        alarm = bool((isr & ALRAF and cr & ALRAIE) or (isr & ALRBF and cr & ALRBIE))
        wkup = bool(isr & WUTF and cr & WUTIE)
        for line, lvl in ((17, alarm), (22, wkup)):
            if self._exti[line] != lvl:
                self._exti[line] = lvl
                exti_input(self.chip, line, lvl)

    # ------------------------------------------------------------------ access checks
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _dbp(self) -> bool:
        pwr = getattr(self, "_pwr", None)
        if pwr is None:
            pwr = self._pwr = find_model(self.chip, "stm32.pwr", "PWR") or False
        return not pwr or bool(pwr.get("CR") & (1 << 8))

    def _protected(self, reg: str) -> bool:
        return reg not in UNPROTECTED and self._unlock != 2

    def write(self, offset: int, size: int, value: int) -> None:
        if self.chip is not None:
            self._sync()
            if not self._dbp():
                r = self.reg_at(offset)
                self._warn_once("dbp", f"{self.name}: write to RTC_{r.name if r else hex(offset)} ignored: backup "
                                f"domain write protection (PWR_CR.DBP = 0)")
                return
            r = self.reg_at(offset)
            if r is not None and r.name != "ISR" and self._protected(r.name):
                self._warn_once(f"wp{r.name}", f"{self.name}: write to RTC_{r.name} ignored: registers are write "
                                f"protected (write 0xCA then 0x53 to RTC_WPR)")
                return
        MMIOBlock.write(self, offset, size, value)

    def read(self, offset: int, size: int) -> int:
        if self.chip is not None:
            self._sync()
        return MMIOBlock.read(self, offset, size)

    # ------------------------------------------------------------------ register hooks
    def _wpr_write(self, blk, reg, old, new):
        k = new & 0xFF
        if self._unlock == 0 and k == 0xCA:
            self._unlock = 1
        elif self._unlock == 1 and k == 0x53:
            self._unlock = 2
        else:
            self._unlock = 0
        return 0

    def _stale_shadow(self) -> bool:
        """After a system reset the shadow registers read their reset values until the first copy (RSF)."""
        if not self._shadow_rst or self.get("CR") & BYPSHAD:
            return False
        if self._due(self._rsf_due):
            self._isr_read(None, None)               # sets RSF, ends the stale period
        return self._shadow_rst

    def _tr_read(self, blk, reg):
        if self.get("ISR") & INIT:
            return None
        if self._stale_shadow():
            return 0
        if self.get("CR") & BYPSHAD:
            return self._enc_tr(self.snapshot()[0])
        if self._shadow is None:
            self._shadow = self.snapshot()          # TR read locks the date until DR is read
        return self._enc_tr(self._shadow[0])

    def _dr_read(self, blk, reg):
        if self.get("ISR") & INIT:
            return None
        if self._stale_shadow():
            return 0x2101
        if self._shadow is not None and not self.get("CR") & BYPSHAD:
            c = self._shadow[0]
            self._shadow = None
        else:
            c = self.snapshot()[0]
        return self._enc_dr(c)

    def _ssr_read(self, blk, reg):
        if self._stale_shadow():
            return 0
        c, ss = self.snapshot()
        if not self.get("CR") & BYPSHAD:
            self._shadow = (c, ss)                  # SSR read locks TR and DR until DR is read (26.3.6)
        return ss

    def _initf(self) -> bool:
        return bool(self.get("ISR") & INIT) and self._due(self._initf_due)

    def _wf(self, flag: int) -> bool:
        return self._due(self._wf_due[flag])

    def _cal_write(self, blk, reg, old, new):
        if not self._initf():
            self._warn_once(f"init{reg.name}", f"{self.name}: RTC_{reg.name} written outside initialisation mode "
                            f"(ISR.INITF = 0): ignored")
            return old
        return None

    def _prer_write(self, blk, reg, old, new):
        if not self._initf():
            self._warn_once("initPRER", f"{self.name}: RTC_PRER written outside initialisation mode: ignored")
            return old
        return None

    def _wutr_write(self, blk, reg, old, new):
        if not self._wf(WUTWF):
            self._warn_once("wutr", f"{self.name}: RTC_WUTR written while WUTE = 1 (WUTWF = 0): ignored")
            return old
        return None

    def _alrm_write(self, blk, reg, old, new):
        wf = ALRAWF if reg.name in ("ALRMAR", "ALRMASSR") else ALRBWF
        if not self._wf(wf) or self.get("ISR") & INIT:
            self._warn_once(f"alrm{reg.name}", f"{self.name}: RTC_{reg.name} written while the alarm is enabled "
                            f"(ALRxWF = 0): ignored")
            return old
        if reg.name.endswith("SSR") and (new >> 24) & 0xF:
            self._warn_once("maskss", f"{self.name}: sub-second alarm compare (MASKSS = {(new >> 24) & 0xF}) is not "
                            f"modelled: the alarm matches at the second increment")
        return None

    def _shift_write(self, blk, reg, old, new):
        self._warn_once("shift", f"{self.name}: RTC_SHIFTR (sub-second shift) is not modelled: stored only")
        return None

    def _cr_write(self, blk, reg, old, new):
        adj = 3600 if new & ADD1H else (-3600 if new & SUB1H else 0)
        new &= ~(ADD1H | SUB1H)
        if (old ^ new) & FMT and not self._initf():
            self._warn_once("fmt", f"{self.name}: CR.FMT changed outside initialisation mode: ignored")
            new = (new & ~FMT) | (old & FMT)
        if (old ^ new) & 7 and not self._wf(WUTWF):
            self._warn_once("wucksel", f"{self.name}: CR.WUCKSEL changed while WUTWF = 0: ignored")
            new = (new & ~7) | (old & 7)
        for en, wf in ((ALRAE, ALRAWF), (ALRBE, ALRBWF), (WUTE, WUTWF)):
            if new & en and not old & en:
                self._wf_due[wf] = None
            elif old & en and not new & en:
                self._wf_due[wf] = self._after_rtcclk(2)      # xWF set 1-2 RTCCLK later (26.3.5)
        if old & BYPSHAD and not new & BYPSHAD:
            self._rsf_due = self._after_rtcclk(2)
        if adj:
            self._rebase()
            self.cal = self.cal.add_seconds(adj)
        self._store(reg, new)
        if (old ^ new) & WUTE:
            if new & WUTE:
                self._start_wut()
            elif self._wut_ev is not None:
                self.chip.sched.cancel(self._wut_ev)
                self._wut_ev = None
        self._reschedule()
        self._update_lines()
        return new

    def _isr_read(self, blk, reg):
        isr = self.get("ISR")
        cr = self.get("CR")
        if cr & BYPSHAD:                             # RSF is cleared by hardware in bypass mode (26.6.4)
            if isr & RSF:
                isr &= ~RSF
                self.set("ISR", isr)
        elif not isr & RSF and self._rsf_due is not None and self._running() and self._now() >= self._rsf_due:
            isr |= RSF
            self._rsf_due = None
            self._shadow_rst = False
            self.set("ISR", isr)
        v = isr & (INIT | RSF | ISR_FLAGS)
        if isr & INIT and self._due(self._initf_due):
            v |= INITF
        if not isr & INIT:                          # ALRxWF are cleared in initialisation mode
            if self._wf(ALRAWF):
                v |= ALRAWF
            if self._wf(ALRBWF):
                v |= ALRBWF
        if self._wf(WUTWF):
            v |= WUTWF
        c = self._dec(self.get("TR"), self.get("DR")) if isr & INIT else self.snapshot()[0]
        if c.yy:
            v |= INITS
        return v

    def _exit_init(self) -> None:
        """Leave initialisation mode: the counters load TR/DR and restart 4 RTCCLK later (26.3.5 step 5); the shadow
        registers are synchronised (RSF) with that load."""
        _, s = self._prediv()
        self.cal = self._dec(self.get("TR"), self.get("DR"))
        self._ss_ref = s
        self._t_ref = self._now() + (4e9 / self._hz if self._hz > 0 else 0.0)
        self._rsf_due = self._after_rtcclk(4)
        self.emit("info", block=self.name, msg=f"RTC calendar set to {self.cal!r}")

    def _isr_write(self, blk, reg, old, new):
        v = old
        if self._unlock == 2:
            if new & INIT and not old & INIT:
                self._rebase()
                self.set("TR", self._enc_tr(self.cal))
                self.set("DR", self._enc_dr(self.cal))
                self._initf_due = self._after_rtcclk(2)       # INITF after 1-2 RTCCLK (26.3.5)
                self._shadow_rst = False
                v |= INIT
                v &= ~RSF
            elif not new & INIT and old & INIT:
                self._exit_init()
                v &= ~(INIT | RSF)
            if old & RSF and not new & RSF:
                v &= ~RSF
                self._rsf_due = self._after_rtcclk(2)
        elif (new ^ old) & INIT:
            self._warn_once("wpISR", f"{self.name}: write to RTC_ISR[7:0] ignored: registers are write protected "
                            f"(write 0xCA then 0x53 to RTC_WPR)")
        v = (v & ~ISR_FLAGS) | (old & new & ISR_FLAGS)            # flags are rc_w0
        v &= INIT | RSF | ISR_FLAGS
        self._store(reg, v)
        self._shadow = None
        self._reschedule()
        if self.get("CR") & WUTE and (old ^ v) & INIT:
            self._start_wut()
        self._update_lines()
        return v
