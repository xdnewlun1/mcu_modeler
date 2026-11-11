"""Kinetis high-speed comparator with 6-bit DAC and analog mux (CMP0/CMP1/CMP2, K22P100M120SF5V2RM ch. 34; chip
configuration 3.7.2).

Registers (SVD table, one 8-byte block per instance): CR0 (FILTER_CNT 6-4, HYSTCTR 1-0), CR1 (SE 7, WE 6, PMODE 4,
INV 3, COS 2, OPE 1, EN 0), FPR (FILT_PER), SCR (DMAEN 6, IER 4, IEF 3, CFR 2 / CFF 1 write-1-to-clear, COUT 0
read-only), DACCR (DACEN 7, VRSEL 6, VOSEL 5-0), MUXCR (PSTM 7, PSEL 5-3, MSEL 2-0).

Inputs (3.7.2.1; INn of CMPx):
* external pins CMPx_INn, in volts only (`adc_volts`, `adc_volts_only`): `set_channel("CMP0_IN0", 1.2)` (what
  `--adc CMP0_IN0=1.2V` / `=1200mV` reaches through Session.set_adc) or `set_input(n | "CMP0_IN0", volts)`. A bare
  integer is a raw ADC code elsewhere, so it is rejected here (ValueError naming the V / mV form). Unset inputs
  read 0 V.
* CMP1 IN3 = DAC0_OUT (the 12-bit DAC's output while DAC0 C0.DACEN=1, else the external value).
* CMP0/CMP1 IN5 = VREF_OUT (the VREF model's 1.2 V while its output is on, else the external value).
* IN6 = bandgap (`params.bandgap`, 1.0 V, data sheet V_BG).
* IN7 = this instance's 6-bit DAC: DACO = Vin * (VOSEL + 1) / 64 with DACEN=1, Vin = Vin1 = VREF_OUT (VRSEL=0) or
  Vin2 = VDD (`params.vdd`, 3.3 V, VRSEL=1); 0 V (analog ground) with DACEN=0 (34.7).

Comparator (34.3): COUTA = (V(PSEL) > V(MSEL)) ^ CR1.INV, re-evaluated whenever an input, the DAC, the VREF or a
register changes (no polling). Hysteresis CR0.HYSTCTR 0..3 = `params.hyst_mv` (5/10/20/30 mV, data sheet V_H):
the output changes only when the difference passes +-hyst/2. With EN=0, or PSEL == MSEL (the comparator shuts
itself down), SCR.COUT reads CR1.INV and CMPO is 0 (34.2.4, 34.3.1.1).
* Filter (Table 34-29): continuous mode (FILTER_CNT=0 or FILT_PER=0, SE=WE=0): COUT = COUTA immediately. Sampled /
  filtered mode with the bus clock (SE=0, WE=0, FILTER_CNT >= 1, FILT_PER > 0): a COUTA change reaches COUT after
  FILTER_CNT * FILT_PER bus clocks, provided COUTA did not change back meanwhile (the worst-case latency of Table
  34-30 without TPD; the sample phase is not modelled). SE=1 (external SAMPLE) and WE=1 (WINDOW) are driven by the
  PDB pulse-out on this chip, which kinetis.pdb does not generate: COUT then holds its value (one `warn`).
* CMPO (to the pin) = COUT (CR1.COS=0) or COUTA (COS=1). With CR1.OPE=1 the CMPx_OUT pin (PTB20/PTC5 CMP0_OUT,
  PTB21/PTC4 CMP1_OUT, PTA5/PTB22 CMP2_OUT, ALT6/ALT5 in the PORT mux) follows CMPO via PinBoard.drive_signal.
* Flags: a rising COUT edge sets SCR.CFR, a falling edge SCR.CFF (write 1 to clear). Interrupt "CMPx" (level)
  while (CFR & IER) | (CFF & IEF), unless SCR.DMAEN: then DMAMUX request 42 + x (Table 3-25) instead; the DMA's
  completion clears the flags (34.5).
* `cout_listeners`: cb(cmp, level) on every COUT change (the ADC alternate trigger, SIM_SOPT7 ADCxTRGSEL 1..3).

Not modelled: propagation delay (TPD) and PMODE speed, the sample/window inputs (PDB pulse-out), MUXCR.PSTM pass-
through, Stop/LLS wake-up, the CMP outputs routed to the FTM fault inputs / FTM1-2 CH0 capture (SIM_SOPT4) and to
the UART0/1 RX inputs (SIM_SOPT5). External inputs are independent of the ADC channel values set on the same pins
(e.g. CMP1_IN0 = ADC0_SE4b): set both. Clock gate: SIM_SCGC4[CMP] (shared by the three instances).
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .clockutil import bus_hz
from .dma import dma_request

SE, WE, INV, COS, OPE, EN = 0x80, 0x40, 0x08, 0x04, 0x02, 0x01
DMAEN, IER, IEF, CFR, CFF, COUT = 0x40, 0x10, 0x08, 0x04, 0x02, 0x01
DACEN, VRSEL = 0x80, 0x40
DMA_SOURCE = 42
_NAME = re.compile(r"^(?:CMP(\d)_)?IN(\d)$", re.I)


@register("kinetis.cmp")
class CMP(GatedBlock):
    adc_volts = True                 # analog inputs are volts ...
    adc_volts_only = True            # ... and there is no raw code: Session rejects a bare integer
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 0))
        self.gate = params.get("gate", "CMP")
        ov = {"CR0": {"ro_mask": 0x8C, "on_write": self._cfg_write},
              "CR1": {"ro_mask": 0x20, "on_write": self._cr1_write},
              "FPR": {"on_write": self._cfg_write},
              "SCR": {"ro_mask": 0xA1, "w1c_mask": CFR | CFF, "on_write": self._scr_write, "on_read": self._scr_read,
                     "on_peek": self._scr_read},
              "DACCR": {"on_write": self._cfg_write},
              "MUXCR": {"ro_mask": 0x40, "on_write": self._cfg_write}}
        r = build_regs((name, f"CMP{self.index}", "CMP0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.vdd = float(params.get("vdd", 3.3))
        self.bandgap = float(params.get("bandgap", 1.0))
        self.hyst_v = [float(x) / 1000.0 for x in params.get("hyst_mv", (5, 10, 20, 30))]
        self.dma_source = int(params.get("dma_source", DMA_SOURCE + self.index))
        self.inputs: dict[int, float] = {}
        self.cout_listeners: list = []
        self.flag_listeners: list = []          # cb(cmp) when CFR/CFF may have changed (LLWU module wake-up input)
        self.edges = 0
        self._irqn: int | None = None
        self._line = False
        self._dac: Any = None
        self._vref: Any = None
        self._ev: Any = None
        self._warned = False
        self._init_state()

    def _init_state(self) -> None:
        self.raw = 0             # comparator output before INV (with hysteresis memory)
        self.couta = 0
        self.cout = 0
        self.cmpo: int | None = None     # last level driven on the CMPx_OUT signal (None = released)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, f"CMP{self.index}")
        self._dac = find_model(chip, "kinetis.dac", "DAC0")
        self._vref = find_model(chip, "kinetis.vref", "VREF")
        for src in (self._dac, self._vref):
            if src is not None:
                src.listeners.append(self._source_changed)
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.dma_source, self._dma_active)
                if hasattr(b, "register_ack"):
                    b.register_ack(self.dma_source, self._dma_done)

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self._init_state()
        self._warned = False
        if self.chip is not None:
            if self.chip.pins is not None:
                self.chip.pins.release_signal(self._sig())
            if self._irqn is not None and self._line:
                self.irq(self._irqn, False)
            self._line = False

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def _sig(self) -> str:
        return f"CMP{self.index}_OUT"

    # ------------------------------------------------------------ inputs
    def _input_index(self, ch: int | str) -> int | None:
        """INn index of `ch` (n, "IN3", "CMP0_IN3"); None for another instance's input or a name that is no CMP
        input (with `strict`, the caller raises)."""
        if isinstance(ch, int):
            return ch
        m = _NAME.match(str(ch).strip())
        if not m or (m.group(1) is not None and int(m.group(1)) != self.index):
            return None
        return int(m.group(2))

    def set_channel(self, ch: int | str, value: float) -> bool:
        """External input CMPx_INn in volts (the --adc path). False if the name is another instance's."""
        if not isinstance(value, float):
            raise ValueError(f"CMP{self.index} input {ch}: give volts (e.g. 1.2V or 1200mV), not a raw code {value!r}")
        if isinstance(ch, int):
            n = ch
        else:
            m = _NAME.match(str(ch).strip())
            if not m:
                raise ValueError(f"bad CMP input name {ch!r} (expected CMP{self.index}_IN0..IN7)")
            if m.group(1) is not None and int(m.group(1)) != self.index:
                return False
            n = int(m.group(2))
        if not 0 <= n <= 7:
            raise ValueError(f"CMP input IN{n} out of range")
        return self.set_input(n, value)

    def set_input(self, n: int | str, volts: float) -> bool:
        """External voltage on input INn (volts); `n` may be a name ("CMP0_IN3"). False for a name that is not one of
        this instance's inputs (the Session.set_adc fallback offers pin names to every analog model)."""
        k = self._input_index(n)
        if k is None or not 0 <= k <= 7:
            return False
        self.inputs[k] = float(volts)
        self._evaluate()
        return True

    def dac6_voltage(self) -> float:
        d = self.get("DACCR")
        if not d & DACEN:
            return 0.0
        if d & VRSEL:
            vin = self.vdd
        else:
            v = self._vref.voltage() if self._vref is not None else None
            vin = v if v is not None else 0.0
        return vin * ((d & 0x3F) + 1) / 64.0

    def input_voltage(self, n: int) -> float:
        if n == 7:
            return self.dac6_voltage()
        if n == 6:
            return self.bandgap
        if n == 5 and self.index in (0, 1) and self._vref is not None:
            v = self._vref.voltage()
            if v is not None:
                return v
        if n == 3 and self.index == 1 and self._dac is not None:
            v = self._dac.voltage()
            if v is not None:
                return v
        return self.inputs.get(n, 0.0)

    def _source_changed(self, src: Any) -> None:
        self._evaluate()

    # ------------------------------------------------------------ comparator
    def enabled(self) -> bool:
        mux = self.get("MUXCR")
        return bool(self.get("CR1") & EN) and ((mux >> 3) & 7) != (mux & 7)

    def _evaluate(self) -> None:
        if self.chip is None:
            return
        cr1 = self.get("CR1")
        if not self.enabled():
            self._cancel()
            self.raw = 0
            self.couta = 0
            self._set_cout(1 if cr1 & INV else 0, flags=False)
            self._drive()
            return
        mux = self.get("MUXCR")
        vp, vm = self.input_voltage((mux >> 3) & 7), self.input_voltage(mux & 7)
        h = self.hyst_v[self.get("CR0") & 3] / 2.0
        if vp > vm + h:
            self.raw = 1
        elif vp < vm - h:
            self.raw = 0
        couta = self.raw ^ (1 if cr1 & INV else 0)
        changed = couta != self.couta
        self.couta = couta
        if cr1 & (SE | WE):
            if not self._warned:
                self._warned = True
                self.emit("warn", block=self.name, msg=f"{self.name}: CR1.{'SE' if cr1 & SE else 'WE'}=1 uses the "
                          "PDB pulse-out as SAMPLE/WINDOW signal, which is not modelled: COUT holds its value")
            self._drive()
            return
        cnt, per = (self.get("CR0") >> 4) & 7, self.get("FPR")
        if cnt == 0 or per == 0:
            self._cancel()
            self._set_cout(couta)
        elif changed or (self._ev is None and couta != self.cout):
            self._cancel()
            if couta != self.cout:
                delay_ns = cnt * per * 1e9 / bus_hz(self.chip)
                self._ev = self.chip.sched.in_ns(delay_ns, self._filter_done, couta)
        self._drive()

    def _filter_done(self, level: int) -> None:
        self._ev = None
        if level == self.couta:
            self._set_cout(level)
            self._drive()

    def _set_cout(self, level: int, flags: bool = True) -> None:
        old = self.cout
        if level == old:
            return
        self.cout = level
        if flags and self.get("CR1") & EN:
            self.edges += 1
            self.set("SCR", self.get("SCR") | (CFR if level else CFF))
            self._update_irq()
        for cb in self.cout_listeners:
            cb(self, level)

    def _drive(self) -> None:
        pins = self.chip.pins
        if pins is None:
            return
        cr1 = self.get("CR1")
        if not cr1 & OPE:
            if self.cmpo is not None:
                self.cmpo = None
                pins.release_signal(self._sig())
            return
        lvl = 0 if not self.enabled() else (self.couta if cr1 & COS else self.cout)
        if lvl != self.cmpo:
            self.cmpo = lvl
            pins.drive_signal(self._sig(), "H" if lvl else "L")

    # ------------------------------------------------------------ IRQ / DMA
    def _pending(self) -> bool:
        s = self.get("SCR")
        return bool((s & CFR and s & IER) or (s & CFF and s & IEF))

    def _dma_active(self) -> bool:
        return bool(self.get("SCR") & DMAEN) and self._pending()

    def _dma_done(self) -> None:
        s = self.get("SCR")
        clr = (CFR if s & IER else 0) | (CFF if s & IEF else 0)
        self.set("SCR", s & ~clr)
        self._update_irq()

    def _update_irq(self) -> None:
        if self.chip is None:
            return
        pend = self._pending()
        dma = bool(self.get("SCR") & DMAEN)
        on = pend and not dma
        if self._irqn is not None and on != self._line:
            self._line = on
            self.irq(self._irqn, on)
        if pend and dma:
            dma_request(self.chip, self.dma_source)
        for cb in self.flag_listeners:
            cb(self)

    # ------------------------------------------------------------ register hooks
    def _scr_read(self, blk, reg):
        return (self.get("SCR") & ~COUT) | self.cout

    def _scr_write(self, blk, reg, old, new):
        self._update_irq()
        return None

    def _cr1_write(self, blk, reg, old, new):
        if new & SE and new & WE:
            new &= ~WE                          # 34.2.2: both set -> SE set, WE cleared
        self._store(reg, new)
        if (old ^ new) & (SE | WE) and not new & (SE | WE):
            self._warned = False
        if (old ^ new) & EN and not new & EN:
            self._cancel()
        self._evaluate()
        return new

    def _cfg_write(self, blk, reg, old, new):
        if old != new:
            self._store(reg, new)
            self._evaluate()
        return None
