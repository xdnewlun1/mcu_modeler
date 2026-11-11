"""C2833x/C2823x 6-channel DMA controller (0x1000; SPRUI07 ch. 8 "Direct Memory Access (DMA) Module", which replaces
SPRUFB8).  Registers (chips/tms320f28232_regs.yaml "DMA", 16-bit units): DMACTRL 0x00, DEBUGCTRL 0x01, REVISION 0x02,
PRIORITYCTRL1 0x04, PRIORITYSTAT 0x06, channel n (1-6) at 0x20 * n: MODE +0, CONTROL +1, BURST_SIZE +2, BURST_COUNT
+3, SRC/DST_BURST_STEP +4/+5, TRANSFER_SIZE +6, TRANSFER_COUNT +7, SRC/DST_TRANSFER_STEP +8/+9, SRC_WRAP_SIZE/COUNT/
STEP +A..+C, DST_WRAP_SIZE/COUNT/STEP +D..+F, then the 32-bit (low word first, 22-bit) pointers SRC_BEG_ADDR_SHADOW
+10, SRC_ADDR_SHADOW +12, SRC_BEG_ADDR_ACTIVE +14, SRC_ADDR_ACTIVE +16, DST_* +18..+1E (TI's DSP2833x_DMA.h layout;
SPRUI07 Table 8-2's "i * E3h" channel stride is a typo).  Every register write is EALLOW protected (SPRUI07 Table
8-2 note); PCLKCR3.DMAENCLK gates the module.

Triggers (MODE.PERINTSEL, SPRUI07 Table 8-9): 1 SEQ1INT, 2 SEQ2INT (ADC, the gated interrupt pulse: INT_ENA_SEQx),
3-9 XINT1-7, 10 XINT13 (XNMI with XNMICR.SELECT), 11-13 TINT0-2 (TCR.TIE), 14/15 McBSP-A MXEVTA / MREVTA (XRDY / RRDY
0 -> 1), 16/17 McBSP-B, 18 + 2(n-1) / 19 + 2(n-1) ePWMn SOCA / SOCB.  F2806x (SPRUH18 Table 11-9, `params.epwm_first:
2`, `epwm_last: 7`): 1/2 = ADCINT1/ADCINT2 of the type-3 ADC (`ti_c2000.adc_soc.adcint_listeners`), 3-5 XINT1-3, 18-29
ePWM2-7 SOCA/SOCB; the USB endpoint triggers (7-10, 30, 31) never fire.  0, 30-31 and the events of a McBSP the part
does not have (McBSP-B on the F28232) never fire (one `warn` when such a channel is started).  The models call back
through their listener lists (adc.dma_listeners, epwm.soc_listeners, cputimer.tint_listeners, xint.int_listeners,
mcbsp.dma_listeners).  A trigger is latched in CONTROL.PERINTFLG when
MODE.PERINTE = 1 (also while the channel is stopped, as on silicon: firmware clears it with PERINTCLR before RUN); a
trigger while PERINTFLG is still set is lost and sets OVRFLG (not PERINTFRC), which with OVRINTE and CHINTE pulses
DINTCHn.  CONTROL.PERINTFRC latches a software trigger whatever PERINTSEL is.  A channel that takes an ADC trigger
clears ADCST.INT_SEQx (the "clear signal to the interrupt source", SPRUI07 8.2.2), so the next end of sequence
interrupts again.  ADCSYNC (PERINTSEL = 1, MODE.SYNCE; the ADC raises it with SEQ1INT for a sequence starting at
RESULT0 when CONT_RUN and SEQ_OVRD are set) sets SYNCFLG; after RUN all triggers are ignored until the first sync
(SPRUI07 8.7).  SYNCFRC / SYNCCLR / ERRCLR as documented.

State machine (SPRUI07 Figure 8-5), per channel: RUN sets RUNSTS.  A serviced trigger with no transfer active starts
a transfer: shadow pointers -> active, TRANSFER_COUNT = TRANSFER_SIZE, WRAP_COUNTs = WRAP_SIZEs, TRANSFERSTS = 1,
DINTCHn if CHINTMODE = 0.  Each burst: ADCSYNC check (SYNCE, SYNCFLG and the SYNCSEL-selected WRAP_COUNT !=
WRAP_SIZE: WRAP_COUNT reloaded, ADDR = BEG_ADDR, SYNCERR), BURST_COUNT = BURST_SIZE, BURSTSTS = 1, PERINTFLG and
SYNCFLG cleared; BURST_SIZE + 1 sixteen-bit words (DATASIZE = 1: (BURST_SIZE + 1) / 2 32-bit items, even addresses)
are read from SRC_ADDR and written to DST_ADDR, the pointers stepping by the signed SRC/DST_BURST_STEP between items
and by SRC/DST_TRANSFER_STEP after the last one; BURSTSTS = 0.  Then, if TRANSFER_COUNT = 0 the transfer ends
(TRANSFERSTS = 0, DINTCHn if CHINTMODE = 1, RUNSTS = 0 unless CONTINUOUS, which re-arms: the next trigger starts a new
transfer from the shadows - the ping-pong scheme); otherwise each WRAP_COUNT = 0 wraps (BEG_ADDR += WRAP_STEP, ADDR =
BEG_ADDR, WRAP_COUNT = WRAP_SIZE) or decrements, TRANSFER_COUNT decrements, and with ONESHOT the channel stays ready
for its next burst without a trigger.  Pointers are 22-bit; BURST_SIZE/BURST_COUNT 5-bit; the active registers are
read-only.
Timing (SPRUI07 8.3): a burst takes 1 + 4 x items SYSCLKOUT cycles (32-bit items take as long as 16-bit ones; a McBSP
DRR source 5 cycles per item).  The burst starts at the cycle of the trigger (or when the previous burst ends) and its
data is written at its last cycle (one scheduler event per burst, never inside the triggering access); interrupts,
flags and counters change then too.  CPU/DMA bus arbitration stalls are not modelled.
Priority (SPRUI07 8.5): one burst at a time.  Round robin CH1..CH6: after a burst the next ready channel after it
is served, else the machine goes idle and restarts at CH1; DMACTRL.PRIORITYRESET returns to idle.  PRIORITYCTRL1.
CH1PRIORITY: CH1 is served first and preempts another channel's burst after the word in progress (the CH1 trigger
cycle's word; the next one if it comes during the burst's first cycle); that burst resumes 1 cycle after the CH1 burst
(PRIORITYSTAT.ACTIVESTS_SHADOW names it meanwhile), and CH2..CH6 rotate.  CONTROL.HALT stops the channel after the
word in progress (RUNSTS = 0; RUN resumes the rest of the burst), SOFTRESET completes that word and then clears
RUNSTS/TRANSFERSTS/BURSTSTS and the counters, DMACTRL.HARDRESET resets the whole module at once.
DMA bus (SPRUI07 8.2.3): L4-L7 SARAM (0xC000-0xFFFF), XINTF zones 0/6/7, the ADC result registers in PF0 (0x0B00-
0x0B0F; not the PF2 copies at 0x7108), the McBSP-A/B DRR2/DRR1/DXR2/DXR1 and, with SYSCTRL MAPCNF.MAPEPWM = 1, the
ePWM1-6 registers in Peripheral Frame 3 (0x5800 + 0x40 (n-1); the access goes to the ePWM model, the CPU view is not
remapped).  An address outside these (or not implemented on the part, e.g. L6/L7 on the F28232) has no defined
behaviour on silicon: the access is skipped (a read moves nothing) and one `warn` per channel is logged.
With DMAENCLK = 0 triggers are ignored and no burst runs (one `warn`).  Not modelled: DEBUGCTRL.FREE (no emulation
suspend), CPU/DMA access conflicts and XINTF stalls, the "ePWM not on the DMA bus" of silicon rev 0, the one-cycle
HARDRESET latency.  CH1 high priority with ONESHOT (not allowed by SPRUI07 8.5.2) runs one burst per arbitration.
"""
from __future__ import annotations

from typing import Any

from ...bus import BusError
from ..registry import register
from ._util import C2000Block, build_regs, find_model, irq_line, sysctrl

NCH = 6
ADDR_MASK = 0x3FFFFF
# MODE
PERINTSEL, OVRINTE, PERINTE, CHINTMODE, ONESHOT, CONTINUOUS, SYNCE, SYNCSEL, DATASIZE, CHINTE = \
    0x1F, 1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14, 1 << 15
# CONTROL: strobes (read 0) and status (read-only)
RUN, HALT, SOFTRESET, PERINTFRC, PERINTCLR, SYNCFRC, SYNCCLR, ERRCLR = 1, 2, 4, 8, 16, 32, 64, 128
PERINTFLG, SYNCFLG, SYNCERR, TRANSFERSTS, BURSTSTS, RUNSTS, OVRFLG = \
    1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14
STATUS = 0x7F00
HARDRESET, PRIORITYRESET = 1, 2
WORD_CYCLES = 4                     # SPRUI07 8.3: 4 cycles/word, 5 for a McBSP read
SRC_NAMES = {0: "none", 1: "SEQ1INT", 2: "SEQ2INT", 10: "XINT13", 11: "TINT0", 12: "TINT1", 13: "TINT2",
             14: "MXEVTA", 15: "MREVTA", 16: "MXEVTB", 17: "MREVTB"}
SRC_NAMES.update({2 + n: f"XINT{n}" for n in range(1, 8)})
SRC_NAMES.update({18 + 2 * (n - 1) + k: f"ePWM{n}SOC{'AB'[k]}" for n in range(1, 7) for k in (0, 1)})
# DMA bus windows (SPRUI07 8.2.3), word addresses inclusive
BUS = [(0x000B00, 0x000B0F, "ADC result registers (PF0)"),
       (0x004000, 0x004FFF, "XINTF zone 0"),
       (0x005000, 0x005003, "McBSP-A DRR2/DRR1/DXR2/DXR1"),
       (0x005040, 0x005043, "McBSP-B DRR2/DRR1/DXR2/DXR1"),
       (0x00C000, 0x00FFFF, "L4-L7 SARAM"),
       (0x100000, 0x2FFFFF, "XINTF zones 6/7")]
MCBSP_DRR = ((0x5000, 0x5001), (0x5040, 0x5041))
PF3_EPWM = (0x5800, 0x597F)
_CH_REGS = ("MODE", "CONTROL", "BURST_SIZE", "BURST_COUNT", "SRC_BURST_STEP", "DST_BURST_STEP", "TRANSFER_SIZE",
            "TRANSFER_COUNT", "SRC_TRANSFER_STEP", "DST_TRANSFER_STEP", "SRC_WRAP_SIZE", "SRC_WRAP_COUNT",
            "SRC_WRAP_STEP", "DST_WRAP_SIZE", "DST_WRAP_COUNT", "DST_WRAP_STEP", "SRC_BEG_ADDR_SHADOW",
            "SRC_ADDR_SHADOW", "SRC_BEG_ADDR_ACTIVE", "SRC_ADDR_ACTIVE", "DST_BEG_ADDR_SHADOW", "DST_ADDR_SHADOW",
            "DST_BEG_ADDR_ACTIVE", "DST_ADDR_ACTIVE")


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class _Chan:
    __slots__ = ("i", "r", "susp", "oneshot", "wait_sync", "warned", "src_warned")

    def __init__(self, i: int):
        self.i = i
        self.r = {k: f"CH{i + 1}_{k}" for k in _CH_REGS}
        self.clear()

    def clear(self) -> None:
        self.susp = 0               # items left of a halted / preempted burst
        self.oneshot = False        # ONESHOT: next burst of the transfer needs no trigger
        self.wait_sync = False      # SYNCE: triggers ignored until the first ADCSYNC after RUN
        self.warned = False
        self.src_warned = False


class _Burst:
    __slots__ = ("ch", "left", "t0", "cost", "stop", "soft")

    def __init__(self, ch: _Chan, left: int, t0: int, cost: int):
        self.ch, self.left, self.t0, self.cost = ch, left, t0, cost
        self.stop: int | None = None        # items to do before a cut (HALT / SOFTRESET / CH1 preemption)
        self.soft = False                   # SOFTRESET once the cut is reached

    def end(self) -> int:
        return self.t0 + (self.left if self.stop is None else self.stop) * self.cost


@register("ti_c2000.dma")
class DMA(C2000Block):
    eallow_regs = "*"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {
            "DMACTRL": {"on_write": self._dmactrl_write, "on_read": lambda b, r: 0, "on_peek": lambda b, r: 0},
            "DEBUGCTRL": {"ro_mask": 0x7FFF},
            "REVISION": {"ro_mask": 0xFFFF},
            "PRIORITYCTRL1": {"ro_mask": 0xFFFE},
            "PRIORITYSTAT": {"ro_mask": 0xFFFF, "on_read": self._prioritystat_read, "on_peek": self._prioritystat_read},
        }
        self.ch = [_Chan(i) for i in range(NCH)]
        for c in self.ch:
            r = c.r
            ov[r["MODE"]] = {"ro_mask": 0x0060}
            ov[r["CONTROL"]] = {"ro_mask": STATUS | 0x8000, "on_write": self._control_write(c)}
            ov[r["BURST_SIZE"]] = {"ro_mask": 0xFFE0}
            ov[r["BURST_COUNT"]] = {"ro_mask": 0xFFE0}
            for p in ("SRC", "DST"):
                ov[r[f"{p}_BEG_ADDR_SHADOW"]] = {"ro_mask": 0xFFC00000}
                ov[r[f"{p}_ADDR_SHADOW"]] = {"ro_mask": 0xFFC00000}
                ov[r[f"{p}_BEG_ADDR_ACTIVE"]] = {"ro_mask": 0xFFFFFFFF}
                ov[r[f"{p}_ADDR_ACTIVE"]] = {"ro_mask": 0xFFFFFFFF}
        regs = build_regs((params.get("regs") or name, "DMA"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate", "DMAENCLK")
        self._lines: list = [f"7.{n}" for n in range(1, NCH + 1)]
        self._ev = None
        self._busy: _Burst | None = None
        self._preempted: _Chan | None = None
        self._rr = -1                       # last channel served by the round robin (-1: idle)
        self._adc = None
        self._gate_trig_warned = False
        self.bursts = 0
        self.words = 0
        self.lost = 0                       # triggers lost to an overflow

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._lines = [irq_line(chip, f"DINTCH{n}", f"7.{n}") for n in range(1, NCH + 1)]
        self._adc = find_model(chip, "ti_c2000.adc")
        self._mcbsp: set[str] = set()
        for b in (getattr(chip, "peripherals", {}) or {}).values():
            m = getattr(b, "model", None)
            if "ti_c2000.epwm" in getattr(b, "implements", ()):
                m = "ti_c2000.epwm"                  # Piccolo ti_c2000.epwm_dc
            if m == "ti_c2000.adc" and hasattr(b, "dma_listeners"):
                b.dma_listeners.append(self._on_adc)
            elif m == "ti_c2000.adc_soc" and hasattr(b, "adcint_listeners"):
                b.adcint_listeners.append(self._on_adcint)     # F2806x: ADCINT1/2 = PERINTSEL 1/2
            elif m == "ti_c2000.epwm" and hasattr(b, "soc_listeners"):
                b.soc_listeners.append(self._on_epwm)
            elif m == "ti_c2000.cputimer" and hasattr(b, "tint_listeners"):
                b.tint_listeners.append(self._on_timer)
            elif m == "ti_c2000.xint" and hasattr(b, "int_listeners"):
                b.int_listeners.append(self._on_xint)
            elif m == "ti_c2000.mcbsp" and hasattr(b, "dma_listeners"):
                b.dma_listeners.append(self._on_mcbsp)
                self._mcbsp.add(getattr(b, "letter", "A"))

    def reset(self) -> None:
        if self.chip is not None and self._ev is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self._busy = None
        self._preempted = None
        self._rr = -1
        for c in self.ch:
            c.clear()
        super().reset()

    # ------------------------------------------------------------ helpers
    def _g(self, c: _Chan, k: str) -> int:
        return self.get(c.r[k])

    def _s(self, c: _Chan, k: str, v: int) -> None:
        self.set(c.r[k], v)

    def _ctl_set(self, c: _Chan, set_: int = 0, clr: int = 0) -> None:
        n = c.r["CONTROL"]
        self.set(n, (self.get(n) & ~clr) | set_)

    def _clk_on(self, what: str = "") -> bool:
        sc = self._sysctrl
        if sc is None or sc.clock_enabled(self.gate):
            return True
        if what and not self._gate_trig_warned:
            self._gate_trig_warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}: {what} ignored, PCLKCR3.DMAENCLK = 0")
        return False

    def _hp(self) -> bool:
        return bool(self.get("PRIORITYCTRL1") & 1)

    def _chint(self, c: _Chan) -> None:
        if self._g(c, "MODE") & CHINTE:
            self.pulse_irq(self._lines[c.i])

    def _ready(self, c: _Chan) -> bool:
        ctl = self._g(c, "CONTROL")
        if not ctl & RUNSTS:
            return False
        if c.susp or c.oneshot:
            return True
        return bool(ctl & PERINTFLG and self._g(c, "MODE") & PERINTE)

    def _now(self) -> int:
        return self.chip.now_cycles()

    # ------------------------------------------------------------ triggers
    def _on_adc(self, seq: int, sync: bool) -> None:
        if sync:
            for c in self.ch:
                m = self._g(c, "MODE")
                if m & PERINTSEL == 1 and m & SYNCE:
                    self._ctl_set(c, SYNCFLG)
                    c.wait_sync = False
        if self.trigger(seq) and self._adc is not None:
            adc = self._adc
            adc.set("ADCST", adc.get("ADCST") & ~(1 << (seq - 1)))      # clear INT_SEQx at the source

    def _on_epwm(self, index: int, which: str) -> None:
        first = int(self.params.get("epwm_first", 1))     # F2806x: ePWM2..7 SOCA/B = 18..29 (SPRUH18 Table 11-9)
        last = int(self.params.get("epwm_last", 6))
        if first <= index <= last:
            self.trigger(18 + 2 * (index - first) + (which == "B"))

    def _on_adcint(self, n: int) -> None:
        if n in (1, 2):
            self.trigger(n)

    def _on_timer(self, index: int) -> None:
        self.trigger(11 + index)

    def _on_xint(self, n: int) -> None:
        self.trigger(10 if n == 13 else 2 + n)

    def _on_mcbsp(self, letter: str, which: str) -> None:
        """McBSP MXEVTx (XRDY 0 -> 1) / MREVTx (RRDY 0 -> 1): PERINTSEL 14/15 (A), 16/17 (B)."""
        self.trigger((14 if letter == "A" else 16) + (which == "R"))

    def trigger(self, code: int) -> bool:
        """A peripheral interrupt event `code` (MODE.PERINTSEL value).  Returns True if a channel received it
        (selected with PERINTE; latched, lost to an overflow, or ignored while waiting for the first ADCSYNC)."""
        if self.chip is None:
            return False
        hit = latched = False
        clk = None
        for c in self.ch:
            m = self._g(c, "MODE")
            if m & PERINTSEL != code or not m & PERINTE:
                continue
            if clk is None:
                clk = self._clk_on(f"{SRC_NAMES.get(code, code)} trigger")
            if not clk:
                return False
            hit = True
            if c.wait_sync:
                continue
            latched = True
            ctl = self._g(c, "CONTROL")
            if ctl & PERINTFLG:
                self.lost += 1
                self._ctl_set(c, OVRFLG)
                if m & OVRINTE:
                    self._chint(c)
            else:
                self._ctl_set(c, PERINTFLG)
        if latched:
            self._kick()
        return hit

    # ------------------------------------------------------------ registers
    def _dmactrl_write(self, blk, reg, old, new):
        if new & HARDRESET:
            self.reset()
        elif new & PRIORITYRESET:
            self._rr = -1
        return 0

    def _prioritystat_read(self, blk, reg):
        a = self._busy.ch.i + 1 if self._busy is not None else 0
        s = self._preempted.i + 1 if self._preempted is not None else 0
        return a | (s << 4)

    def _control_write(self, c: _Chan):
        def wr(blk, reg, old, new):
            self._store(reg, old & STATUS)
            b = self._busy if self._busy is not None and self._busy.ch is c else None
            if new & SOFTRESET:
                if b is not None:
                    b.soft = True
                    self._cut(b, self._now())
                else:
                    self._soft_reset(c)
            if new & HALT:
                self._ctl_set(c, clr=RUNSTS)
                if b is not None:
                    self._cut(b, self._now())
            if new & ERRCLR:
                self._ctl_set(c, clr=SYNCERR | OVRFLG)
            if new & SYNCCLR:
                self._ctl_set(c, clr=SYNCFLG)
            if new & PERINTCLR:
                self._ctl_set(c, clr=PERINTFLG)
            if new & SYNCFRC:
                self._ctl_set(c, SYNCFLG)
                c.wait_sync = False
            if new & PERINTFRC:
                self._ctl_set(c, PERINTFLG)
            if new & RUN and not (new & (HALT | SOFTRESET)):
                m = self._g(c, "MODE")
                if not self._g(c, "CONTROL") & RUNSTS:
                    if m & SYNCE and m & PERINTSEL == 1 and not self._g(c, "CONTROL") & SYNCFLG:
                        c.wait_sync = True
                    sel = m & PERINTSEL
                    mc = "AABB"[sel - 14] if 14 <= sel <= 17 else None
                    if (mc is not None and mc not in self._mcbsp or sel >= 30) and not c.src_warned:
                        c.src_warned = True
                        why = f"no McBSP-{mc} on this part" if sel <= 17 else \
                            "reserved, no peripheral connection"
                        self.emit("warn", block=self.name, msg=f"{self.name} CH{c.i + 1}: PERINTSEL {sel} "
                                  f"({SRC_NAMES.get(sel, 'reserved')}) never triggers: {why}")
                self._ctl_set(c, RUNSTS)
            if new & (RUN | PERINTFRC):
                self._kick()
            return None
        return wr

    def _soft_reset(self, c: _Chan) -> None:
        self._ctl_set(c, clr=RUNSTS | TRANSFERSTS | BURSTSTS)
        for k in ("BURST_COUNT", "TRANSFER_COUNT", "SRC_WRAP_COUNT", "DST_WRAP_COUNT"):
            self._s(c, k, 0)
        c.susp = 0
        c.oneshot = False
        if self._preempted is c:
            self._preempted = None

    # ------------------------------------------------------------ engine
    def _kick(self) -> None:
        if self.chip is None:
            return
        b = self._busy
        if b is not None:
            if self._hp() and b.ch.i != 0 and self._ready(self.ch[0]):
                self._cut(b, self._now())                       # CH1 high priority: preempt after this word
            return
        if self._ev is None:
            self._ev = self.chip.sched.at_cycles(self._now(), self._service)

    def _cut(self, b: _Burst, now: int) -> None:
        """Stop burst `b` after the item in progress at cycle `now`."""
        started = 1 if now < b.t0 else (now - b.t0) // b.cost + 1
        if b.stop is not None:
            started = min(started, b.stop)
        if started >= b.left and not b.soft:
            return
        b.stop = min(started, b.left)
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = self.chip.sched.at_cycles(max(b.end(), now), self._service)

    def _service(self) -> None:
        self._ev = None
        now = self._now()
        b = self._busy
        if b is not None:
            self._busy = None
            c = b.ch
            n = b.left if b.stop is None else b.stop
            self._items(c, n, b.left)
            left = b.left - n
            if b.soft:
                self._soft_reset(c)
            elif left:
                c.susp = left                                     # halted or preempted mid-burst
                if self._g(c, "CONTROL") & RUNSTS:
                    self._preempted = c
            else:
                c.susp = 0
                self._end_burst(c)
        self._start_next(now)

    def _pick(self) -> _Chan | None:
        hp = self._hp()
        if hp and self._ready(self.ch[0]):
            return self.ch[0]
        p = self._preempted
        if p is not None and self._ready(p):
            return p
        order = self.ch[1:] if hp else self.ch
        ready = [c for c in order if self._ready(c)]
        if not ready:
            return None
        for c in ready:
            if c.i > self._rr:
                return c
        return ready[0]

    def _start_next(self, t: int) -> None:
        c = self._pick()
        if c is None:
            self._rr = -1                                         # nothing pending: idle
            return
        if not self._clk_on(f"CH{c.i + 1} burst"):
            return
        if c.susp:                    # resume a halted / preempted burst: 1 cycle (the return from a CH1 burst)
            if self._preempted is c:
                self._preempted = None
            left = c.susp
            c.susp = 0
        else:
            left = self._begin_burst(c)
        t0 = t + 1                    # 1-cycle burst overhead
        if not (c.i == 0 and self._hp()):
            self._rr = c.i
        src = self._g(c, "SRC_ADDR_ACTIVE") & ADDR_MASK
        cost = WORD_CYCLES + (1 if any(lo <= src <= hi for lo, hi in MCBSP_DRR) else 0)
        b = _Burst(c, left, t0, cost)
        self._busy = b
        self.bursts += 1
        self._ev = self.chip.sched.at_cycles(b.end(), self._service)

    def clock_resumed(self, dt_cycles: int) -> None:
        """STANDBY/HALT exit: a burst in progress continues with its remaining words."""
        if self._busy is not None:
            self._busy.t0 += dt_cycles

    def _begin_burst(self, c: _Chan) -> int:
        """Transfer start (if none is active), ADCSYNC check, burst start; returns the number of items."""
        ctl = self._g(c, "CONTROL")
        m = self._g(c, "MODE")
        cont = c.oneshot
        c.oneshot = False
        if not ctl & TRANSFERSTS:
            for p in ("SRC", "DST"):
                self._s(c, f"{p}_BEG_ADDR_ACTIVE", self._g(c, f"{p}_BEG_ADDR_SHADOW"))
                self._s(c, f"{p}_ADDR_ACTIVE", self._g(c, f"{p}_ADDR_SHADOW"))
                self._s(c, f"{p}_WRAP_COUNT", self._g(c, f"{p}_WRAP_SIZE"))
            self._s(c, "TRANSFER_COUNT", self._g(c, "TRANSFER_SIZE"))
            self._ctl_set(c, TRANSFERSTS)
            if not m & CHINTMODE:
                self._chint(c)
        elif not cont and m & SYNCE and ctl & SYNCFLG:
            p = "DST" if m & SYNCSEL else "SRC"
            if self._g(c, f"{p}_WRAP_COUNT") != self._g(c, f"{p}_WRAP_SIZE"):
                self._s(c, f"{p}_WRAP_COUNT", self._g(c, f"{p}_WRAP_SIZE"))
                self._s(c, f"{p}_ADDR_ACTIVE", self._g(c, f"{p}_BEG_ADDR_ACTIVE"))
                self._ctl_set(c, SYNCERR)
        bs = self._g(c, "BURST_SIZE") & 0x1F
        self._s(c, "BURST_COUNT", bs)
        self._ctl_set(c, BURSTSTS, PERINTFLG | SYNCFLG)
        return max(1, (bs + 1) // 2) if m & DATASIZE else bs + 1

    def _items(self, c: _Chan, n: int, left: int) -> None:
        """Move `n` of the `left` remaining items of channel c's burst (the last one ends the burst)."""
        if n <= 0:
            return
        m = self._g(c, "MODE")
        w = 2 if m & DATASIZE else 1
        sstep, dstep = _s16(self._g(c, "SRC_BURST_STEP")), _s16(self._g(c, "DST_BURST_STEP"))
        src = self._g(c, "SRC_ADDR_ACTIVE") & ADDR_MASK
        dst = self._g(c, "DST_ADDR_ACTIVE") & ADDR_MASK
        bc = self._g(c, "BURST_COUNT")
        for k in range(n):
            a_s, a_d = (src & ~1, dst & ~1) if w == 2 else (src, dst)
            v = self._read(c, a_s, w)
            if v is not None:
                self._write(c, a_d, w, v)
            self.words += w
            if left - k > 1:                                      # not the burst's last item
                src = (src + sstep) & ADDR_MASK
                dst = (dst + dstep) & ADDR_MASK
                bc = max(bc - w, 0)
        self._s(c, "SRC_ADDR_ACTIVE", src)
        self._s(c, "DST_ADDR_ACTIVE", dst)
        self._s(c, "BURST_COUNT", bc)

    def _end_burst(self, c: _Chan) -> None:
        for p in ("SRC", "DST"):
            k = f"{p}_ADDR_ACTIVE"
            self._s(c, k, (self._g(c, k) + _s16(self._g(c, f"{p}_TRANSFER_STEP"))) & ADDR_MASK)
        self._s(c, "BURST_COUNT", 0)
        self._ctl_set(c, clr=BURSTSTS)
        m = self._g(c, "MODE")
        tc = self._g(c, "TRANSFER_COUNT")
        if tc == 0:
            self._ctl_set(c, clr=TRANSFERSTS if m & CONTINUOUS else TRANSFERSTS | RUNSTS)
            c.oneshot = False
            if m & CHINTMODE:
                self._chint(c)
            return
        for p in ("SRC", "DST"):
            wc = self._g(c, f"{p}_WRAP_COUNT")
            if wc == 0:
                beg = (self._g(c, f"{p}_BEG_ADDR_ACTIVE") + _s16(self._g(c, f"{p}_WRAP_STEP"))) & ADDR_MASK
                self._s(c, f"{p}_BEG_ADDR_ACTIVE", beg)
                self._s(c, f"{p}_ADDR_ACTIVE", beg)
                self._s(c, f"{p}_WRAP_COUNT", self._g(c, f"{p}_WRAP_SIZE"))
            else:
                self._s(c, f"{p}_WRAP_COUNT", wc - 1)
        self._s(c, "TRANSFER_COUNT", tc - 1)
        c.oneshot = bool(m & ONESHOT)

    # ------------------------------------------------------------ DMA bus
    def _target(self, addr: int) -> tuple[int | None, str]:
        """Bus address for DMA address `addr`, or None (not on the DMA bus), and a description."""
        lo, hi = PF3_EPWM
        if lo <= addr <= hi:
            sc = self._sysctrl or sysctrl(self.chip)
            if sc is None or not sc.get("MAPCNF") & 1:
                return None, "ePWM in Peripheral Frame 3 needs MAPCNF.MAPEPWM = 1"
            n, off = divmod(addr - lo, 0x40)
            blk = self.chip.peripherals.get(f"EPWM{n + 1}")
            if blk is None:
                return None, f"no EPWM{n + 1} on this part"
            return blk.base + off, ""
        for lo, hi, what in BUS:
            if lo <= addr <= hi:
                if self.chip.bus.lookup(addr) is None:
                    return None, f"{what}: not implemented on this part"
                return addr, ""
        return None, "not on the DMA bus (L4-L7, XINTF, ADC PF0 results, McBSP data, ePWM PF3)"

    def _bad(self, c: _Chan, kind: str, addr: int, why: str) -> None:
        if not c.warned:
            c.warned = True
            self.emit("warn", block=self.name, msg=f"{self.name} CH{c.i + 1}: {kind} address 0x{addr:06X} {why}; "
                      "the access is skipped")

    def _read(self, c: _Chan, addr: int, w: int) -> int | None:
        t, why = self._target(addr)
        if t is None or (w == 2 and self._target(addr + 1)[0] is None):
            self._bad(c, "source", addr, why or "(second word) not on the DMA bus")
            return None
        try:
            return self.chip.bus.read(t, w)
        except BusError:
            self._bad(c, "source", addr, "bus error")
            return None

    def _write(self, c: _Chan, addr: int, w: int, v: int) -> None:
        t, why = self._target(addr)
        if t is None or (w == 2 and self._target(addr + 1)[0] is None):
            self._bad(c, "destination", addr, why or "(second word) not on the DMA bus")
            return
        try:
            self.chip.bus.write(t, w, v)
        except BusError:
            self._bad(c, "destination", addr, "bus error")
