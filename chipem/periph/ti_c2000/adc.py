"""C2833x 12-bit ADC (0x7100): sequencers, triggers and conversion timing (SPRUI07 ch. 7 = SPRU812; SPRS439 7.10).

Sequencers: dual mode (ADCTRL1.SEQ_CASC = 0) has SEQ1 (states CONV00-07, MAX_CONV1[2:0]) and SEQ2 (CONV08-15,
MAX_CONV2) sharing one converter; cascaded mode has one 16-state SEQ1 (MAX_CONV1[3:0]; SEQ2 triggers ignored).  A
session converts MAX_CONVn + 1 states starting at the sequencer's current state; each state's CONVnn field
(ADCCHSELSEQ1-4: 0-7 = ADCINA0-7, 8-15 = ADCINB0-7) picks the channel, the result goes to RESULTn of the state.  The
state pointer wraps at the end of the sequencer.
* Start/stop mode (CONT_RUN = 0): the sequencer stays where the session ended; the next SOC continues from there;
  ADCTRL2.RST_SEQx returns it to CONV00 / CONV08 at once (aborting a running session).
* Continuous mode (CONT_RUN = 1): at the end of a session the sequencer restarts by itself, from its reset state, or
  with SEQ_OVRD = 1 from where it is (results fill on to the end of the sequencer, SPRUI07 7.3.3.2); the conversions
  run on back to back (no new trigger latency).  CONT_RUN is sampled at the end of each session.
* Triggers: ADCTRL2.SOC_SEQ1/SOC_SEQ2 written 1, ePWM SOCA (EPWM_SOCA_SEQ1 -> SEQ1), SOCB (EPWM_SOCB_SEQ2 -> SEQ2 in
  dual mode, EPWM_SOCB_SEQ -> the cascaded SEQ1; `epwm_soc`), and with EXT_SOC_SEQ1 a rising edge of the XINT2 source
  pin (GPIOXINT2SEL, GPIO0-31; "always rising edge sensitive", SPRUI07 Table 1-80).  A trigger while that sequencer
  is busy, or (dual mode) while the converter serves the other one, is held pending (ADCTRL2.SOC_SEQx reads 1); a
  further trigger while one is pending is lost (`soc_lost`).
* Arbitration (dual mode): one converter; when a session ends, a pending SEQ1 starts before a pending SEQ2
  (SPRUI07 7.3.2 note), and a continuous-mode restart competes like a pending request.
* Timing (SPRS439 7.10.4/7.10.5): ADCCLK = HSPCLK / (2 x ADCCLKPS, or 1 when 0) / (CPS + 1), counted in SYSCLKOUT
  cycles (HSPCLK from SYSCTRL HISPCP).  Sequential sampling: first result 2.5 + (ACQ_PS + 1) + 4 ADCCLK after the
  trigger, then one every (2 + ACQ_PS) ADCCLK.  Simultaneous sampling (ADCTRL3.SMODE_SEL): each state samples
  ADCINAx and ADCINBx (x = CONVnn[2:0]) at once, A to RESULTn, B to RESULTn+1 (the result pointer advances by two,
  wrapping in the sequencer's result range), A result 2.5 + (ACQ_PS + 1) + 4 ADCCLK, B one ADCCLK later, then a pair
  every (3 + ACQ_PS) ADCCLK.  The end of sequence (flags, interrupt) is at the last result.  Results become visible
  at their own time: reads of ADCRESULTn, the 0x0B00 mirror (`ti_c2000.adcmirror`), ADCASEQSR (SEQ1/SEQ2_STATE and
  SEQ_CNTR = conversions left - 1) and ADCST catch up first, so only one event per session is scheduled.
  Channel values are taken when the result appears.
* Results are left-justified in ADCRESULTn (value << 4, Peripheral Frame 2) and right-justified in the mirror.
* Interrupts: at the end of a session ADCST.INT_SEQx is set (INT_MOD_SEQx = 1: only every other end of sequence, when
  EOS_BUFx was set; EOS_BUFx toggles at every end of sequence); a newly set INT_SEQx with ADCTRL2.INT_ENA_SEQx pulses
  SEQ1INT (PIE 1.1) / SEQ2INT (1.2) and ADCINT (1.6).  Writing INT_SEQx_CLR clears it.  ADCST.SEQx_BSY is set while
  the sequencer converts.
* ADCSOCAO / ADCSOCBO pins (GPIO8 / GPIO32 MUX3, GPIO10 / GPIO33 MUX3): every ePWM SOCA / SOCB drives the signal low
  for 32 HSPCLK cycles (SPRS439 tw(ADCSOCL)), then releases it.  The C2000 signal table (signals.py) does not list
  ADCSOCxO yet, so the GPIO model gives such a pin no output direction and the pulse does not reach it.
* An ePWM or external trigger does not convert while PCLKCR0.ADCENCLK = 0 or the ADC core is powered down
  (ADCTRL3.ADCPWDN = 0; one `warn`); a software SOC_SEQx only warns about the gate (as for any access).
Channel values come from `set_channel("ADCINA3" | "A3", value)` (default `params.default`, 0).
* DMA (ti_c2000.dma): every SEQxINT pulse calls `dma_listeners` cb(seq, sync); `sync` is the ADCSYNC signal (SEQ1
  with CONT_RUN and SEQ_OVRD, for a sequence that started at RESULT0, SPRUI07 7.3.5).  A DMA channel that takes the
  trigger clears INT_SEQx (the DMA's "clear signal to the interrupt source", SPRUI07 8.2.2).
Not modelled: SUSMOD (no emulation suspend), the "few SYSCLKOUT" from the last result to the interrupt flag, ADCCLK
phase (a trigger starts at once), ADCREFSEL / ADCOFFTRIM effects.
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model, irq_line, listen_input, unlisten_input

SOC_SEQ1, RST_SEQ1, INT_ENA_SEQ1, INT_MOD_SEQ1, EPWM_SOCA_SEQ1, EXT_SOC_SEQ1 = \
    1 << 13, 1 << 14, 1 << 11, 1 << 10, 1 << 8, 1 << 7
SOC_SEQ2, RST_SEQ2, INT_ENA_SEQ2, INT_MOD_SEQ2, EPWM_SOCB_SEQ2, EPWM_SOCB_SEQ = \
    1 << 5, 1 << 6, 1 << 3, 1 << 2, 1, 1 << 15
SEQ_CASC, SEQ_OVRD, CONT_RUN, CPS = 1 << 4, 1 << 5, 1 << 6, 1 << 7
ADCPWDN, SMODE_SEL = 1 << 5, 1
SOC_BIT = {1: SOC_SEQ1, 2: SOC_SEQ2}
INT_ENA = {1: INT_ENA_SEQ1, 2: INT_ENA_SEQ2}
INT_MOD = {1: INT_MOD_SEQ1, 2: INT_MOD_SEQ2}
SOCO_HSPCLK = 32                    # ADCSOCxO low pulse, SPRS439 tw(ADCSOCL) = 32 tc(HCO)


def channel_index(name: str) -> int | None:
    m = re.match(r"^(?:ADC0?_?)?(?:ADCIN)?([AB])(\d)$", str(name).strip().upper())
    if not m:
        return None
    n = int(m.group(2))
    if n > 7:
        return None
    return (0 if m.group(1) == "A" else 8) + n


class _Session:
    """One autoconversion session: `n` states from `k0`; the result of state i appears at t_first + i * step
    (simultaneous: A there, B `dt_b` later)."""
    __slots__ = ("seq", "k0", "n", "t_first", "step", "simul", "dt_b", "done")

    def __init__(self, seq, k0, n, t_first, step, simul, dt_b):
        self.seq, self.k0, self.n, self.t_first = seq, k0, n, t_first
        self.step, self.simul, self.dt_b = step, simul, dt_b
        self.done = 0               # states whose results are already written (B included when simultaneous)

    def t_last(self) -> int:
        return self.t_first + (self.n - 1) * self.step + (self.dt_b if self.simul else 0)


@register("ti_c2000.adc")
class ADC(C2000Block):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"ADCTRL1": {"on_write": self._ctrl1_write}, "ADCTRL2": {"on_write": self._ctrl2_write},
              "ADCST": {"on_write": self._st_write, "on_read": self._catch_read, "ro_mask": 0x00CF,
                        "on_peek": lambda b, r: None},
              "ADCASEQSR": {"on_read": self._aseqsr_read, "on_peek": self._aseqsr_peek}}
        for k in range(16):
            ov[f"ADCRESULT{k}"] = {"on_read": self._catch_read, "on_peek": self._result_peek}
        regs = build_regs((name, "ADC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = "ADCENCLK"
        self.values = [int(params.get("default", 0)) & 0xFFF] * 16
        self.results = [0] * 16
        self._sess: Any = None              # the session on the converter
        self._ev = None                     # its end-of-sequence event
        self._pending: set[int] = set()     # SOC_SEQx pending
        self._cont_wait: set[int] = set()   # continuous-mode restart waiting for the converter
        self._state = {1: 0, 2: 8}          # sequencer state pointers (CONVnn)
        self._cntr = 0                      # SEQ_CNTR after the last session
        self._eos_buf = {1: False, 2: False}
        self._soco_ev: dict[str, Any] = {}
        self._ext_pins: list = []
        self._ext_level = False
        self._mirror = None
        self.conversions = 0
        self.soc_lost = 0
        self._soc_warned = False
        mx = params.get("adcclk_max_hz")    # data sheet ADCCLK limit of the part (F2833x / F2809 25 MHz, 280x 12.5 MHz,
        self.adcclk_max_hz = float(mx) if mx else None      # 60-MHz F2801x 7.5 MHz)
        self._adcclk_warned = False
        self.dma_listeners: list = []       # cb(seq, sync) on every SEQ1INT/SEQ2INT pulse (DMA triggers, ADCSYNC)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._lines = {1: irq_line(chip, "SEQ1INT", "1.1"), 2: irq_line(chip, "SEQ2INT", "1.2"),
                       0: irq_line(chip, "ADCINT", "1.6")}
        self._mirror = find_model(chip, "ti_c2000.adcmirror")
        self._gpio = find_model(chip, "ti_c2000.gpio")

    def clock_resumed(self, dt_cycles: int) -> None:
        """STANDBY/HALT exit: a session that was converting continues where it stopped (its result times move)."""
        if self._sess is not None:
            self._sess.t_first += dt_cycles

    def reset(self) -> None:
        if self.chip is not None:
            if self._ev is not None:
                self.chip.sched.cancel(self._ev)
            for sig, ev in self._soco_ev.items():
                self.chip.sched.cancel(ev)
                if self.chip.pins is not None:
                    self.chip.pins.release_signal(sig)
        self._ev = None
        self._sess = None
        self._soco_ev = {}
        self._pending = set()
        self._cont_wait = set()
        self._state = {1: 0, 2: 8}
        self._cntr = 0
        self._eos_buf = {1: False, 2: False}
        super().reset()
        self.results = [0] * 16
        self._watch_ext()

    def set_channel(self, name: str, value: int) -> bool:
        i = channel_index(name)
        if i is None:
            raise KeyError(name)
        self.values[i] = max(0, min(int(value), 0xFFF))
        return True

    # ------------------------------------------------------------ configuration
    def _casc(self) -> bool:
        return bool(self.get("ADCTRL1") & SEQ_CASC)

    def _seq_range(self, seq: int) -> tuple[int, int]:
        """(first state, number of states) of sequencer `seq` in the current mode."""
        if seq == 1:
            return (0, 16) if self._casc() else (0, 8)
        return 8, 8

    def _conv_channel(self, k: int) -> int:
        reg = self.get(f"ADCCHSELSEQ{k // 4 + 1}")
        return (reg >> (4 * (k % 4))) & 0xF

    def hspclk_cycles(self) -> int:
        """HSPCLK period in SYSCLKOUT cycles (SYSCTRL HISPCP: 0 -> 1, n -> 2n)."""
        sc = self._sysctrl
        if sc is None:
            return 2
        p = sc.get("HISPCP") & 7
        return 2 * p if p else 1

    def adcclk_cycles(self) -> int:
        """ADCCLK period in SYSCLKOUT cycles: HSPCLK / (2 x ADCCLKPS or 1) / (CPS + 1)."""
        ps = (self.get("ADCTRL3") >> 1) & 0xF
        return self.hspclk_cycles() * (2 * ps if ps else 1) * (2 if self.get("ADCTRL1") & CPS else 1)

    def _timing(self) -> tuple[int, int, bool, int]:
        """(first-result latency, step, simultaneous, B offset) in SYSCLKOUT cycles."""
        d = self.adcclk_cycles()
        acq = (self.get("ADCTRL1") >> 8) & 0xF
        simul = bool(self.get("ADCTRL3") & SMODE_SEL)
        first = (5 * d + 1) // 2 + (acq + 1) * d + 4 * d          # td(SH) 2.5 + tSH + td(schx_n) 4 ADCCLK
        step = (3 + acq if simul else 2 + acq) * d
        return first, step, simul, d

    def _ctrl1_write(self, blk, reg, old, new):
        if new & (1 << 14):
            self.reset()
            return self.get("ADCTRL1")
        return None

    def _ctrl2_write(self, blk, reg, old, new):
        self.check_gate()
        keep = old & (SOC_SEQ1 | SOC_SEQ2)                   # pending-trigger bits are status, not written
        self._store(reg, (new & ~(SOC_SEQ1 | SOC_SEQ2 | RST_SEQ1 | RST_SEQ2)) | keep)
        casc = self._casc()
        if new & RST_SEQ1:
            self._seq_reset(1)
        if new & RST_SEQ2 and not casc:
            self._seq_reset(2)
        if new & SOC_SEQ1:
            self._trigger(1)
        if new & SOC_SEQ2 and not casc:
            self._trigger(2)
        if (old ^ new) & EXT_SOC_SEQ1:
            self._watch_ext()
        return None

    # ------------------------------------------------------------ triggers
    def _hw_ok(self, what: str) -> bool:
        sc = self._sysctrl
        clk = sc is None or sc.clock_enabled(self.gate)
        if not clk or not self.get("ADCTRL3") & ADCPWDN:
            if not self._soc_warned:
                self._soc_warned = True
                why = "PCLKCR0.ADCENCLK = 0" if not clk else "ADCTRL3.ADCPWDN = 0 (ADC core powered down)"
                self.emit("warn", block=self.name, msg=f"{self.name}: {what} ignored, {why}")
            return False
        return True

    def epwm_soc(self, which: str) -> None:
        """An ePWM SOCA/SOCB pulse (SPRU812 ADCTRL2: EPWM_SOCA_SEQ1 bit 8, EPWM_SOCB_SEQ2 bit 0, EPWM_SOCB_SEQ 15);
        also pulses the ADCSOCAO / ADCSOCBO pin signal."""
        self._soco_pulse(f"ADCSOC{which}O")
        c2 = self.get("ADCTRL2")
        casc = self._casc()
        if which == "A" and c2 & EPWM_SOCA_SEQ1:
            seq = 1
        elif which == "B" and casc and c2 & EPWM_SOCB_SEQ:
            seq = 1
        elif which == "B" and not casc and c2 & EPWM_SOCB_SEQ2:
            seq = 2
        else:
            return
        if self._hw_ok(f"ePWM SOC{which}"):
            self._trigger(seq)

    def _soco_pulse(self, sig: str) -> None:
        chip = self.chip
        pins = getattr(chip, "pins", None)
        if pins is None or not any(p.direction == "out" and pins.signal_matches(p.owner, sig)
                                   for p in pins.signal_pins(sig)):
            return
        pins.drive_signal(sig, "L")                          # active low
        ev = self._soco_ev.pop(sig, None)
        if ev is not None:
            chip.sched.cancel(ev)
        n = SOCO_HSPCLK * self.hspclk_cycles()
        self._soco_ev[sig] = chip.sched.at_cycles(chip.now_cycles() + n, self._soco_end, sig)

    def _soco_end(self, sig: str) -> None:
        self._soco_ev.pop(sig, None)
        self.chip.pins.release_signal(sig)

    def _watch_ext(self) -> None:
        """Watch port A (the XINT2 source can be any of GPIO0-31) only while EXT_SOC_SEQ1 is set."""
        board = getattr(self.chip, "pins", None) if self.chip is not None else None
        if board is None:
            return
        want = bool(self.get("ADCTRL2") & EXT_SOC_SEQ1)
        if want and not self._ext_pins:
            self._ext_pins = [p for p in (board.gpio("A", i) for i in range(32)) if p is not None]
            for p in self._ext_pins:
                listen_input(self.chip, p, self._ext_changed)
            src = self._ext_src()
            self._ext_level = src is not None and src.level() == "H"
        elif not want and self._ext_pins:
            for p in self._ext_pins:
                unlisten_input(self.chip, p, self._ext_changed)
            self._ext_pins = []

    def _ext_src(self) -> Any:
        g = self._gpio
        return g.xint_pin(2) if g is not None and hasattr(g, "xint_pin") else None

    def _ext_changed(self, pin: Any = None) -> None:
        src = self._ext_src()
        if src is None or pin is not None and pin is not src:
            return
        lvl = src.level() == "H"
        rise = lvl and not self._ext_level
        self._ext_level = lvl
        if rise and self.get("ADCTRL2") & EXT_SOC_SEQ1 and self._hw_ok("external SOC (XINT2)"):
            self._trigger(1)

    def _trigger(self, seq: int) -> None:
        """A start of conversion for `seq`: start now, hold one pending while busy, else lose it."""
        self._catch_up()
        busy = self._sess is not None
        if not busy:
            self._start(seq, self.chip.now_cycles(), fresh=True)
            return
        if seq in self._pending:
            self.soc_lost += 1                           # SPRU812 ADCTRL2.SOC_SEQx case 3: trigger lost
            return
        self._pending.add(seq)                           # case 2 (or the converter serves the other sequencer)
        self.set("ADCTRL2", self.get("ADCTRL2") | SOC_BIT[seq])

    def _seq_reset(self, seq: int) -> None:
        self._catch_up()
        s = self._sess
        if s is not None and s.seq == seq:               # abort the running session
            self.chip.sched.cancel(self._ev)
            self._ev = None
            self._sess = None
            self.set("ADCST", self.get("ADCST") & ~(1 << (seq + 1)))
        self._pending.discard(seq)
        self._cont_wait.discard(seq)
        self.set("ADCTRL2", self.get("ADCTRL2") & ~SOC_BIT[seq])
        self._state[seq] = self._seq_range(seq)[0]
        self._cntr = 0
        if self._sess is None:
            self._next_session(self.chip.now_cycles())
        self._show_state()

    # ------------------------------------------------------------ sessions
    def _start(self, seq: int, t: int, fresh: bool, prev_last: int | None = None) -> None:
        lo, size = self._seq_range(seq)
        k0 = self._state[seq]
        if not lo <= k0 < lo + size:
            k0 = lo
        mx = self.get("ADCMAXCONV")
        if seq == 1:
            n = (mx & 0xF) + 1 if self._casc() else (mx & 7) + 1
        else:
            n = ((mx >> 4) & 7) + 1
        first, step, simul, db = self._timing()
        if self.adcclk_max_hz and not self._adcclk_warned:
            self._check_adcclk(db)
        if fresh or prev_last is None:
            t_first = t + first
        else:                                            # continuous: the pipeline runs on
            t_first = prev_last + step - (db if simul else 0)
        s = _Session(seq, k0, n, t_first, step, simul, db)
        self._sess = s
        self._pending.discard(seq)
        self._cont_wait.discard(seq)
        self.set("ADCTRL2", self.get("ADCTRL2") & ~SOC_BIT[seq])
        self.set("ADCST", self.get("ADCST") | (1 << (seq + 1)))       # SEQx_BSY
        self._ev = self.chip.sched.at_cycles(max(s.t_last(), self.chip.now_cycles()), self._eos)

    def _check_adcclk(self, d: int) -> None:
        """One `warn` when a conversion starts with ADCCLK above the part's limit (`params.adcclk_max_hz`); the
        conversion still runs with the programmed timing (silicon would give unreliable results)."""
        hz = float(self.chip.clock.hz) / max(d, 1)
        if hz > self.adcclk_max_hz * 1.0001:
            self._adcclk_warned = True
            self.emit("warn", block=self.name, msg=f"{self.name}: ADCCLK {hz / 1e6:.3f} MHz exceeds the "
                      f"{self.adcclk_max_hz / 1e6:g} MHz maximum of this part (HISPCP / ADCCLKPS / CPS); results "
                      "are converted anyway")

    def _result_slots(self, s: _Session, i: int) -> tuple[int, int | None]:
        """(state, B-result register or None) of the session's i-th state; the A result goes to RESULT[state] in
        sequential mode, to RESULT[lo + 2 (state - lo)] (wrapped) in simultaneous mode."""
        lo, size = self._seq_range(s.seq)
        k = lo + (s.k0 - lo + i) % size
        if not s.simul:
            return k, None
        ra = lo + (2 * (k - lo)) % size
        return ra, lo + (2 * (k - lo) + 1) % size

    def _catch_up(self) -> None:
        """Write every result of the running session whose time has come."""
        s = self._sess
        if s is None or self.chip is None:
            return
        now = self.chip.now_cycles()
        if now < s.t_first or s.done >= s.n:
            return
        lo, size = self._seq_range(s.seq)
        changed = False
        while s.done < s.n:
            i = s.done
            ta = s.t_first + i * s.step
            if ta > now or (s.simul and ta + s.dt_b > now):
                if s.simul and ta <= now:                # A of the pair is out, B not yet
                    ra, _rb = self._result_slots(s, i)
                    k = lo + (s.k0 - lo + i) % size
                    self._write(ra, self.values[self._conv_channel(k) & 7])
                    changed = True
                break
            k = lo + (s.k0 - lo + i) % size
            ch = self._conv_channel(k)
            if s.simul:
                ra, rb = self._result_slots(s, i)
                self._write(ra, self.values[ch & 7])
                self._write(rb, self.values[8 + (ch & 7)])
                self.conversions += 2
            else:
                self._write(k, self.values[ch])
                self.conversions += 1
            s.done += 1
            changed = True
        if changed and self._mirror is not None:
            self._mirror.refresh(self)

    def _due_results(self) -> tuple[dict[int, int], int]:
        """What _catch_up would write now, without writing it (debugger peek): ({slot: value}, session.done after)."""
        s = self._sess
        out: dict[int, int] = {}
        if s is None or self.chip is None:
            return out, 0
        now = self.chip.now_cycles()
        done = s.done
        if now < s.t_first:
            return out, done
        lo, size = self._seq_range(s.seq)
        while done < s.n:
            i = done
            ta = s.t_first + i * s.step
            k = lo + (s.k0 - lo + i) % size
            if ta > now or (s.simul and ta + s.dt_b > now):
                if s.simul and ta <= now:
                    out[self._result_slots(s, i)[0]] = self.values[self._conv_channel(k) & 7]
                break
            ch = self._conv_channel(k)
            if s.simul:
                ra, rb = self._result_slots(s, i)
                out[ra] = self.values[ch & 7]
                out[rb] = self.values[8 + (ch & 7)]
            else:
                out[k] = self.values[ch]
            done += 1
        return out, done

    def result_peek(self, k: int) -> int | None:
        """RESULTk (raw, right-justified) of a conversion done but not written yet (None: nothing pending)."""
        return self._due_results()[0].get(k)

    def _result_peek(self, blk, reg):
        k = int(reg.name[9:])
        due = self._due_results()[0]
        return due[k] << 4 if k in due else None

    def _aseqsr_peek(self, blk, reg):
        s = self._sess
        if s is None:
            return None
        done = self._due_results()[1]
        lo, size = self._seq_range(s.seq)
        st = dict(self._state)
        st[s.seq] = lo + (s.k0 - lo + done) % size
        cntr = max(s.n - 1 - done, 0)
        return (st[1] & 0xF) | (((st[2] - 8) & 7) << 4) | (cntr << 8)

    def _write(self, k: int, v: int) -> None:
        self.results[k] = v
        self.set(f"ADCRESULT{k}", v << 4)

    def _eos(self) -> None:
        """End of sequence: flags and interrupt, then the next session (continuous restart / pending, SEQ1 first)."""
        self._ev = None
        s = self._sess
        if s is None:
            return
        self._catch_up()
        seq = s.seq
        lo, size = self._seq_range(seq)
        k = lo + (s.k0 - lo + s.n) % size                # the state after the last conversion
        self._sess = None
        self._cntr = 0
        st = self.get("ADCST") & ~(1 << (seq + 1))       # SEQx_BSY off
        c2 = self.get("ADCTRL2")
        set_int = True
        if c2 & INT_MOD[seq]:                            # every other end of sequence (EOS_BUFx)
            set_int = self._eos_buf[seq]
            self._eos_buf[seq] = not self._eos_buf[seq]
            st = (st & ~(1 << (seq + 5))) | (int(self._eos_buf[seq]) << (seq + 5))
        newly = set_int and not st & (1 << (seq - 1))
        if set_int:
            st |= 1 << (seq - 1)
        self.set("ADCST", st)
        c1 = self.get("ADCTRL1")
        if c1 & CONT_RUN:
            self._state[seq] = k if c1 & SEQ_OVRD else lo
            self._cont_wait.add(seq)
        else:
            self._state[seq] = k
        self._show_state()
        if newly and c2 & INT_ENA[seq]:
            self.pulse_irq(self._lines[seq])
            self.pulse_irq(self._lines[0])
            sync = seq == 1 and c1 & CONT_RUN and c1 & SEQ_OVRD and s.k0 == lo     # ADCSYNC: sequence from RESULT0
            for cb in list(self.dma_listeners):
                cb(seq, bool(sync))
        self._next_session(self.chip.now_cycles(), prev_last=s.t_last(), prev_seq=seq)

    def _next_session(self, t: int, prev_last: int | None = None, prev_seq: int | None = None) -> None:
        cands = self._pending | self._cont_wait
        if self._casc():
            cands &= {1}
        if not cands:
            return
        seq = 1 if 1 in cands else 2
        cont = seq in self._cont_wait and seq == prev_seq       # continuous restart: the pipeline runs on
        self._start(seq, t, fresh=not cont, prev_last=prev_last)

    def _show_state(self) -> None:
        self.set("ADCASEQSR", (self._state[1] & 0xF) | (((self._state[2] - 8) & 7) << 4) | ((self._cntr & 0xF) << 8))

    # ------------------------------------------------------------ reads
    def _catch_read(self, blk, reg):
        self._catch_up()
        return self.get(reg.name)                        # (the bus sampled the storage before this hook)

    def _aseqsr_read(self, blk, reg):
        self._catch_up()
        s = self._sess
        if s is None:
            return None
        lo, size = self._seq_range(s.seq)
        st = dict(self._state)
        st[s.seq] = lo + (s.k0 - lo + s.done) % size
        cntr = max(s.n - 1 - s.done, 0)
        return (st[1] & 0xF) | (((st[2] - 8) & 7) << 4) | (cntr << 8)

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
        ov = {f"ADCRESULT{k}": {"on_read": self._catch_read, "on_peek": self._result_peek} for k in range(16)}
        regs = build_regs((name, "ADCMIRROR"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self._adc = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._adc = find_model(chip, "ti_c2000.adc")

    def _catch_read(self, blk, reg):
        if self._adc is not None:
            self._adc._catch_up()
        return self.get(reg.name)

    def _result_peek(self, blk, reg):
        a = self._adc
        return a.result_peek(int(reg.name[9:])) if a is not None else None

    def refresh(self, adc: ADC) -> None:
        for k in range(16):
            self.set(f"ADCRESULT{k}", adc.results[k])
