"""C2833x 12-bit ADC (0x7100) — minimal sequencer model (SPRU812).

Start/stop mode: a start of conversion (ADCTRL2.SOC_SEQ1 written, or an ePWM SOC) converts MAX_CONV1 + 1 channels
(cascaded mode, ADCTRL1.SEQ_CASC = 1: up to 16, states CONV00-15; dual mode: up to 8, CONV00-07) taken from
ADCCHSELSEQ1-4 (0-7 = ADCINA0-7, 8-15 = ADCINB0-7) into RESULTn of the state CONVn; SOC_SEQ2 (dual mode) converts
MAX_CONV2 + 1 channels in states CONV08-15.  As in SPRU812 (sequencer start/stop mode), the sequencer stays at the
state after the last conversion and the next SOC continues from there (wrapping at the end of the sequencer);
ADCTRL2.RST_SEQx returns it to CONV00 / CONV08 immediately (aborting a running sequence).  ADCASEQSR shows the
states (SEQ_CNTR reads 0 when idle).  A SOC while the sequencer is busy is held pending (ADCTRL2.SOC_SEQx reads 1)
and starts when the current sequence ends; a further SOC while one is pending is lost.  Each conversion takes `params.conv_ns`
(default 160 ns, i.e. ACQ + conversion at 12.5 MHz ADCCLK).  Results are written left-justified to ADCRESULTn
(value << 4, Peripheral Frame 2) and right-justified to the 0x0B00 mirror (`ti_c2000.adcmirror`).  At the end of a
sequence ADCST.INT_SEQx is set (cleared by writing INT_SEQx_CLR) and, with ADCTRL2.INT_ENA_SEQx, SEQ1INT (PIE 1.1) /
SEQ2INT (1.2) and ADCINT (1.6) are pulsed.  Channel values come from `set_channel("ADCINA3" | "A3", value)`
(default `params.default`, 0).  ePWM start-of-conversion triggers (`epwm_soc("A"|"B")`, called by ti_c2000.epwm):
ADCTRL2.EPWM_SOCA_SEQ1 lets SOCA start SEQ1, EPWM_SOCB_SEQ2 lets SOCB start SEQ2 (dual mode), EPWM_SOCB_SEQ lets
SOCB start the cascaded sequencer.  An ePWM trigger does not convert while PCLKCR0.ADCENCLK = 0 or the ADC core is
powered down (ADCTRL3.ADCPWDN = 0; one `warn`); a software SOC_SEQx only warns about the gate (as for any access).
Not modelled: continuous run mode (CONT_RUN, SEQ_OVRD), SEQ1/SEQ2 arbitration in dual mode (they convert in
parallel), INT_MOD_SEQx (interrupt every other sequence), EXT_SOC_SEQ1 (ADCSOC pin), simultaneous sampling (SMODE_SEL),
ACQ_PS / ADCCLKPS timing (the conversion time is `params.conv_ns`).
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model, irq_line

SOC_SEQ1, RST_SEQ1, INT_ENA_SEQ1 = 1 << 13, 1 << 14, 1 << 11
SOC_SEQ2, RST_SEQ2, INT_ENA_SEQ2 = 1 << 5, 1 << 6, 1 << 3
SEQ_CASC, CONT_RUN = 1 << 4, 1 << 6
ADCPWDN = 1 << 5
SOC_BIT = {1: SOC_SEQ1, 2: SOC_SEQ2}


def channel_index(name: str) -> int | None:
    m = re.match(r"^(?:ADC0?_?)?(?:ADCIN)?([AB])(\d)$", str(name).strip().upper())
    if not m:
        return None
    n = int(m.group(2))
    if n > 7:
        return None
    return (0 if m.group(1) == "A" else 8) + n


@register("ti_c2000.adc")
class ADC(C2000Block):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"ADCTRL1": {"on_write": self._ctrl1_write}, "ADCTRL2": {"on_write": self._ctrl2_write},
              "ADCST": {"on_write": self._st_write, "ro_mask": 0x00CF}}
        regs = build_regs((name, "ADC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = "ADCENCLK"
        self.values = [int(params.get("default", 0)) & 0xFFF] * 16
        self.conv_ns = float(params.get("conv_ns", 160.0))
        self.results = [0] * 16
        self._ev: dict[int, Any] = {}
        self._pending: set[int] = set()
        self._state = {1: 0, 2: 8}                 # sequencer state pointers (CONVnn)
        self.conversions = 0
        self.soc_lost = 0
        self._soc_warned = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._lines = {1: irq_line(chip, "SEQ1INT", "1.1"), 2: irq_line(chip, "SEQ2INT", "1.2"),
                       0: irq_line(chip, "ADCINT", "1.6")}

    def reset(self) -> None:
        if self.chip is not None:
            for ev in self._ev.values():
                self.chip.sched.cancel(ev)
        self._ev = {}
        self._pending = set()
        self._state = {1: 0, 2: 8}
        super().reset()
        self.results = [0] * 16

    def set_channel(self, name: str, value: int) -> bool:
        i = channel_index(name)
        if i is None:
            raise KeyError(name)
        self.values[i] = max(0, min(int(value), 0xFFF))
        return True

    def _conv_channel(self, k: int) -> int:
        reg = self.get(f"ADCCHSELSEQ{k // 4 + 1}")
        return (reg >> (4 * (k % 4))) & 0xF

    def _ctrl1_write(self, blk, reg, old, new):
        if new & (1 << 14):
            self.reset()
            return self.get("ADCTRL1")
        return None

    def _ctrl2_write(self, blk, reg, old, new):
        self.check_gate()
        keep = old & (SOC_SEQ1 | SOC_SEQ2)                   # pending-trigger bits are status, not written
        self._store(reg, (new & ~(SOC_SEQ1 | SOC_SEQ2 | RST_SEQ1 | RST_SEQ2)) | keep)
        casc = bool(self.get("ADCTRL1") & SEQ_CASC)
        if new & RST_SEQ1:
            self._seq_reset(1)
        if new & RST_SEQ2 and not casc:
            self._seq_reset(2)
        if new & SOC_SEQ1:
            self._trigger(1)
        if new & SOC_SEQ2 and not casc:
            self._trigger(2)
        return None

    def epwm_soc(self, which: str) -> None:
        """An ePWM SOCA/SOCB pulse (SPRU812 ADCTRL2: EPWM_SOCA_SEQ1 bit 8, EPWM_SOCB_SEQ2 bit 0, EPWM_SOCB_SEQ 15)."""
        c2 = self.get("ADCTRL2")
        casc = bool(self.get("ADCTRL1") & SEQ_CASC)
        if which == "A" and c2 & (1 << 8):
            seq = 1
        elif which == "B" and casc and c2 & (1 << 15):
            seq = 1
        elif which == "B" and not casc and c2 & 1:
            seq = 2
        else:
            return
        sc = self._sysctrl
        clk = sc is None or sc.clock_enabled(self.gate)
        if not clk or not self.get("ADCTRL3") & ADCPWDN:
            if not self._soc_warned:
                self._soc_warned = True
                why = "PCLKCR0.ADCENCLK = 0" if not clk else "ADCTRL3.ADCPWDN = 0 (ADC core powered down)"
                self.emit("warn", block=self.name, msg=f"{self.name}: ePWM SOC{which} ignored, {why}")
            return
        self._trigger(seq)

    def _seq_range(self, seq: int) -> tuple[int, int]:
        """(first state, number of states) of sequencer `seq` in the current mode."""
        if seq == 1:
            return (0, 16) if self.get("ADCTRL1") & SEQ_CASC else (0, 8)
        return 8, 8

    def _seq_reset(self, seq: int) -> None:
        ev = self._ev.pop(seq, None)
        if ev is not None:
            self.chip.sched.cancel(ev)
        self._pending.discard(seq)
        self.set("ADCTRL2", self.get("ADCTRL2") & ~SOC_BIT[seq])
        self.set("ADCST", self.get("ADCST") & ~(1 << (seq + 1)))
        self._state[seq] = self._seq_range(seq)[0]
        self._show_state()

    def _show_state(self) -> None:
        self.set("ADCASEQSR", (self._state[1] & 0xF) | (((self._state[2] - 8) & 7) << 4))

    def _trigger(self, seq: int) -> None:
        """A start of conversion for `seq`: start now, hold one pending while busy, else lose it."""
        if seq in self._ev:
            if seq in self._pending:
                self.soc_lost += 1                       # SPRU812 ADCTRL2.SOC_SEQx case 3: trigger lost
            else:
                self._pending.add(seq)                   # case 2: SOC_SEQx set, starts after this sequence
                self.set("ADCTRL2", self.get("ADCTRL2") | SOC_BIT[seq])
            return
        mx = self.get("ADCMAXCONV")
        if seq == 1:
            n = (mx & 0xF) + 1 if self.get("ADCTRL1") & SEQ_CASC else (mx & 7) + 1
        else:
            n = ((mx >> 4) & 7) + 1
        self.set("ADCST", self.get("ADCST") | (1 << (seq + 1)))       # SEQx_BSY
        self._ev[seq] = self.chip.sched.in_ns(self.conv_ns * n, self._done, seq, n)

    def _done(self, seq: int, n: int) -> None:
        self._ev.pop(seq, None)
        lo, size = self._seq_range(seq)
        k = self._state[seq]
        if not lo <= k < lo + size:
            k = lo
        mirror = find_model(self.chip, "ti_c2000.adcmirror")
        for _ in range(n):
            v = self.values[self._conv_channel(k)]
            self.results[k] = v
            self.set(f"ADCRESULT{k}", v << 4)
            self.conversions += 1
            k = lo + (k - lo + 1) % size
        self._state[seq] = lo if self.get("ADCTRL1") & CONT_RUN else k
        self._show_state()
        st = (self.get("ADCST") & ~(1 << (seq + 1))) | (1 << (seq - 1))
        self.set("ADCST", st)
        if mirror is not None:
            mirror.refresh(self)
        if self.get("ADCTRL2") & (INT_ENA_SEQ1 if seq == 1 else INT_ENA_SEQ2):
            self.pulse_irq(self._lines[seq])
            self.pulse_irq(self._lines[0])
        if seq in self._pending:
            self._pending.discard(seq)
            self.set("ADCTRL2", self.get("ADCTRL2") & ~SOC_BIT[seq])
            self._trigger(seq)

    def _st_write(self, blk, reg, old, new):
        v = old
        if new & (1 << 4):
            v &= ~1
        if new & (1 << 5):
            v &= ~2
        return v


@register("ti_c2000.adcmirror")
class ADCMirror(C2000Block):
    """ADC results, right-justified, at 0x0B00 (Peripheral Frame 0, read-only)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        regs = build_regs((name, "ADCMIRROR"), size, {}, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)

    def refresh(self, adc: ADC) -> None:
        for k in range(16):
            self.set(f"ADCRESULT{k}", adc.results[k])
