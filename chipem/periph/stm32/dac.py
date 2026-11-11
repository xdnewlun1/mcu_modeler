"""STM32F4 12-bit DAC, two channels (RM0090 ch. 14).

Registers from the SVD: CR, SWTRIGR, DHR12R1/DHR12L1/DHR8R1, DHR12R2/DHR12L2/DHR8R2, DHR12RD/DHR12LD/DHR8RD, DOR1,
DOR2, SR.
* Data holding: the 12/8-bit right/left aligned and dual registers all write the channel's DHR (14.3.4); DHR12Rx
  reads back the DHR, the aligned views read their own encoding of it.
* Conversion (14.3.4): with TENx = 0 DHRx moves to DORx one APB1 clock cycle after the write; with TENx = 1 three
  APB1 cycles after the trigger (`sched.at_cycles`, APB1 = HCLK / PPRE1, exact core cycles). The digital interface
  (DHR, DOR, triggers, DMA) works whatever ENx is (14.3.1 note); ENx only powers the analog output. TSELx and MAMPx
  cannot be changed while ENx = 1 (14.3.6 / 14.3.9 notes: such a write keeps the old field, one `warn`). Triggers
  (CR.TSELx, Table 75): 000 TIM6_TRGO, 001 TIM8_TRGO, 010 TIM7_TRGO, 011 TIM5_TRGO, 100 TIM2_TRGO, 101 TIM4_TRGO (rising edges,
  through the timer's `add_trigger_listener("TRGO")`), 110 EXTI line 9 (not modelled: one `warn`), 111 software
  (SWTRIGR.SWTRIGx, cleared one APB1 cycle later when the DHR has moved).
* Output: Vout = VREF+ x DOR / 4096 (VREF+ = `params.vref`, 3.3 V; 14.3.5) while ENx = 1; `voltage(ch)` (ch 1/2)
  returns it, None while the channel is off. Every change is one `info` event ("DAC_OUT1 = 1.6500 V", fields
  `signal`, `volts`, `code`) and is passed to `listeners` (cb(dac, ch)). The output buffer (BOFFx) and settling time
  are not modelled (BOFF is stored). The DAC pins PA4 (DAC_OUT1) / PA5 (DAC_OUT2) show owner DAC_OUTx when the pin
  is in analog mode and the channel is enabled (the RM's recommended configuration; otherwise one `warn`), and the
  voltage is put on the pin's analog value, so ADC1/ADC2 channel 4/5 on the same pin convert the DAC output (the
  STM32F4 ADC has no internal DAC channel: only this pin path exists, as on silicon).
* Wave generation (14.3.8/14.3.9, needs TENx = 1): WAVEx = 01 noise: a 12-bit LFSR (preset 0xAAA, Figure 68 taps
  X^12+X^6+X^4+X+1, a NOR of all bits injects a '1' into a zero register) masked by MAMPx is added to DHR, then the
  LFSR shifts (Figure 69: DHR = 0 gives DOR 0xAAA, then 0xD55); WAVEx = 1x triangle: a counter steps up to
  2^(MAMPx+1) - 1 and back down to 0, incremented before it is added to DHR. Sums are added "without overflow": they
  saturate at 0xFFF (the literal reading; not checked on silicon).
* DMA (14.3.8): with DMAENx a hardware trigger (not SWTRIG) issues the request "DAC1"/"DAC2" (RM0090 Table 43:
  DMA1 stream 5/6 channel 7). The DMA's write to a DHR register of the channel acknowledges it. A second trigger
  while the request is not yet acknowledged sets SR.DMAUDRx (rc_w1), no new request is issued while DMAUDRx is set
  (the channel keeps converting old data, 14.3.7) and, with CR.DMAUDRIEx, raises IRQ "TIM6_DAC" (shared with TIM6,
  OR'd).
Clock gate RCC_APB1ENR.DACEN (a gated-off access logs one `warn`); RCC_APB1RSTR resets the block.
"""
from __future__ import annotations

from typing import Any, Callable

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, dma_request, find_model, gpio_resync, irq_number, shared_irq

EN, BOFF, TEN, DMAEN, DMAUDRIE = 1, 2, 4, 1 << 12, 1 << 13
SR_DMAUDR = (1 << 13, 1 << 29)
TSEL_TIMERS = ("TIM6", "TIM8", "TIM7", "TIM5", "TIM2", "TIM4")


class _Chan:
    def __init__(self, n: int):
        self.n = n
        self.dhr = 0
        self.dor = 0
        self.ev = None
        self.sub = None              # (timer block, cb)
        self.trig = False            # last trigger level
        self.req = False             # DMA request outstanding
        self.lfsr = 0xAAA
        self.tri = 0
        self.tri_up = True
        self.volts: float | None = None
        self.pin_key: str | None = None
        self.saved: Any = None
        self.owns = False


@register("stm32.dac")
class DAC(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "DAC"
        dhr = {"on_write": self._dhr_write, "on_read": self._dhr_read}
        ov = {"CR": {"on_write": self._cr_write},
              "SWTRIGR": {"on_write": self._swtrig_write},
              "DOR1": {"ro_mask": 0xFFFFFFFF, "on_read": lambda b, r: self.ch[0].dor},
              "DOR2": {"ro_mask": 0xFFFFFFFF, "on_read": lambda b, r: self.ch[1].dor},
              "SR": {"w1c_mask": SR_DMAUDR[0] | SR_DMAUDR[1], "on_write": self._sr_write}}
        for n in ("DHR12R1", "DHR12L1", "DHR8R1", "DHR12R2", "DHR12L2", "DHR8R2", "DHR12RD", "DHR12LD", "DHR8RD"):
            ov[n] = dhr
        r = build_regs((name, "DAC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.vref = float(params.get("vref", 3.3))
        self.ch = [_Chan(1), _Chan(2)]
        self.listeners: list[Callable[[Any, int], None]] = []
        self._irqn: int | None = None
        self._line = False
        self._warned: set[str] = set()
        self.conversions = [0, 0]

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "TIM6_DAC", "DAC")
        board = getattr(chip, "pins", None)
        for c in self.ch:
            sig = f"DAC_OUT{c.n}"
            for p in (board.pins if board is not None else ()):
                if (p.functions or {}).get("analog") == sig:
                    c.pin_key = p.id
                    break

    def reset(self) -> None:
        chans = getattr(self, "ch", None) or []
        for c in chans:
            if c.ev is not None and self.chip is not None:
                self.chip.sched.cancel(c.ev)
            if c.sub is not None:
                c.sub[0].remove_trigger_listener("TRGO", c.sub[1])
            if c.req and self.chip is not None:
                dma_request(self.chip, f"DAC{c.n}", False)
        super().reset()
        if self.chip is not None:
            if self._line:
                self._line = False
                shared_irq(self.chip, self._irqn, self.name, False)
            for c in chans:
                self._output(c)                  # EN is 0 now: "off", the pin gets its own value back
        for c in chans:
            c.dhr = c.dor = 0
            c.ev = c.sub = None
            c.trig = c.req = False
            c.lfsr, c.tri, c.tri_up = 0xAAA, 0, True

    # ------------------------------------------------------------------ helpers
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _apb_cycles(self) -> int:
        r = self._rcc
        return r.ppre(1) if r is not None else 1

    def _cr(self, c: _Chan) -> int:
        return (self.get("CR") >> (16 * (c.n - 1))) & 0xFFFF

    def voltage(self, ch: int = 1) -> float | None:
        c = self.ch[ch - 1]
        if not self._cr(c) & EN:
            return None
        return self.vref * c.dor / 4096.0

    def _output(self, c: _Chan) -> None:
        """Publish the channel's output (event, listeners, pin analog value) if it changed."""
        v = self.voltage(c.n)
        if v == c.volts:
            return
        was_on = c.volts is not None
        c.volts = v
        sig = f"DAC_OUT{c.n}"
        if v is None:
            self.emit("info", block=self.name, signal=sig, volts=None, msg=f"{sig} off (EN{c.n}=0)")
        else:
            self.emit("info", block=self.name, signal=sig, volts=v, code=c.dor, msg=f"{sig} = {v:.4f} V (code {c.dor})")
        self._pin_value(c, v)
        if was_on != (v is not None) and self.chip is not None:
            gpio_resync(self.chip, sig)
            if v is not None:
                self._check_pin(c)
        for cb in list(self.listeners):
            cb(self, c.n)

    def _pin_value(self, c: _Chan, v: float | None) -> None:
        """The DAC drives its pin: ADC channels on the same pin see the voltage (stm32.adc analog store); the value
        the user had set on the pin (--adc) comes back when the channel is disabled."""
        if c.pin_key is None or self.chip is None:
            return
        from .adc import _store
        vals = _store(self.chip)
        if v is None:
            if c.owns:
                if c.saved is not None:
                    vals[c.pin_key] = c.saved
                else:
                    vals.pop(c.pin_key, None)
            c.owns, c.saved = False, None
            return
        if not c.owns:
            c.saved = vals.get(c.pin_key)
            c.owns = True
        vals[c.pin_key] = v

    def _check_pin(self, c: _Chan) -> None:
        if c.pin_key is None:
            return
        pin = self.chip.pins.get(c.pin_key)
        if pin.direction != "analog":
            self._warn_once(f"pin{c.n}", f"{self.name}: channel {c.n} enabled but {c.pin_key} is not in analog mode "
                            f"(RM0090 14.3.6: configure it as analog to avoid parasitic consumption)")

    def signal_state(self, sig: str) -> tuple[str, str | None] | None:
        """GPIO hook: DAC_OUTx owns its (analog-mode) pin while channel x is enabled."""
        for c in self.ch:
            if sig == f"DAC_OUT{c.n}":
                return ("analog", None) if self._cr(c) & EN else None
        return None

    def _update_irq(self) -> None:
        if self.chip is None:
            return
        sr, cr = self.get("SR"), self.get("CR")
        on = bool((sr & SR_DMAUDR[0] and cr & DMAUDRIE) or (sr & SR_DMAUDR[1] and cr & (DMAUDRIE << 16)))
        if on != self._line:
            self._line = on
            shared_irq(self.chip, self._irqn, self.name, on)

    # ------------------------------------------------------------------ conversion
    def _transfer_in(self, c: _Chan, apb: int) -> None:
        if c.ev is not None:
            self.chip.sched.cancel(c.ev)
        c.ev = self.chip.sched.in_cycles(apb * self._apb_cycles(), self._transfer, c)

    def _transfer(self, c: _Chan) -> None:
        c.ev = None
        cr = self._cr(c)
        val = c.dhr
        wave = (cr >> 6) & 3
        if cr & TEN and wave:
            mamp = (cr >> 8) & 0xF
            mask = (1 << (min(mamp, 11) + 1)) - 1
            if wave == 1:
                lf = c.lfsr
                val = min(val + (lf & mask), 0xFFF)          # Figure 69: the first DOR is the preload 0xAAA
                fb = (lf ^ (lf >> 1) ^ (lf >> 4) ^ (lf >> 6)) & 1    # X^12 + X^6 + X^4 + X + 1 (Figure 68)
                fb ^= int(lf == 0)                           # NOR of all bits: a '1' is injected (anti lock-up)
                c.lfsr = (lf >> 1) | (fb << 11)
            else:
                if c.tri_up:
                    if c.tri >= mask:
                        c.tri_up = False
                        c.tri -= 1
                    else:
                        c.tri += 1
                else:
                    if c.tri <= 0:
                        c.tri_up = True
                        c.tri += 1
                    else:
                        c.tri -= 1
                val = min(val + c.tri, 0xFFF)            # "added ... without overflow" (14.3.9)
        c.dor = val
        self.conversions[c.n - 1] += 1
        self.set("SWTRIGR", self.get("SWTRIGR") & ~(1 << (c.n - 1)))
        self._output(c)

    def _trigger(self, c: _Chan, hw: bool) -> None:
        cr = self._cr(c)
        if not cr & TEN:                         # 14.3.1: the digital interface works even with ENx = 0
            return
        if hw and cr & DMAEN:
            udr = SR_DMAUDR[c.n - 1]
            if c.req and not self.get("SR") & udr:
                self.set("SR", self.get("SR") | udr)
                self.emit("warn", block=self.name, msg=f"{self.name}: channel {c.n} DMA underrun (trigger before the "
                          f"DMA served the previous request)")
                self._update_irq()
            elif not self.get("SR") & udr:       # 14.3.7: no request is treated while DMAUDRx is set
                c.req = True
                dma_request(self.chip, f"DAC{c.n}", True)
        self._transfer_in(c, 3 if hw else 1)     # the channel keeps converting (old data after an underrun)

    lp_frozen: Any = False              # PWR freezer: Stop / gated Sleep (no APB1 clock: triggers are not seen)

    def _ext_trig(self, c: _Chan, t: int, level: bool) -> None:
        if level and not c.trig:
            c.trig = True
            if not self.lp_frozen:
                self._trigger(c, True)
        elif not level:
            c.trig = False

    def _subscribe(self, c: _Chan) -> None:
        cr = self._cr(c)
        want = None
        if cr & TEN:
            tsel = (cr >> 3) & 7
            if tsel < 6:
                blk = find_model(self.chip, "stm32.tim", TSEL_TIMERS[tsel])
                if blk is not None and blk.name == TSEL_TIMERS[tsel]:
                    want = blk
            elif tsel == 6:
                self._warn_once("exti9", f"{self.name}: trigger EXTI line 9 (TSEL{c.n}=110) is not modelled")
        if c.sub is not None and (want is None or c.sub[0] is not want):
            c.sub[0].remove_trigger_listener("TRGO", c.sub[1])
            c.sub = None
        if want is not None and c.sub is None:
            cb = lambda t, lvl, c=c: self._ext_trig(c, t, lvl)   # noqa: E731
            want.add_trigger_listener("TRGO", cb)
            c.sub = (want, cb)
            c.trig = bool(want.trigger_level("TRGO"))

    # ------------------------------------------------------------------ register hooks
    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _cr_write(self, blk, reg, old, new):
        for c in self.ch:                        # 14.3.6 / 14.3.9: TSELx and MAMPx cannot change while ENx = 1
            sh = 16 * (c.n - 1)
            lock = (0x38 | 0xF00) << sh
            if old >> sh & EN and (old ^ new) & lock:
                new = (new & ~lock) | (old & lock)
                self._warn_once(f"lock{c.n}", f"{self.name}: TSEL{c.n}/MAMP{c.n} written while EN{c.n} = 1: "
                                f"ignored (RM0090 14.3.6, 14.3.9)")
        self._store(reg, new)
        for c in self.ch:
            sh = 16 * (c.n - 1)
            o, n = (old >> sh) & 0xFFFF, (new >> sh) & 0xFFFF
            if (o ^ n) & 0xC0 or (o ^ n) & 0xF00:
                c.lfsr, c.tri, c.tri_up = 0xAAA, 0, True
            self._subscribe(c)
            if (o ^ n) & EN:
                self._output(c)
            if not n & DMAEN and c.req:
                c.req = False
                dma_request(self.chip, f"DAC{c.n}", False)
        self._update_irq()
        return None

    def _swtrig_write(self, blk, reg, old, new):
        self._store(reg, new & 3)
        for c in self.ch:
            if new >> (c.n - 1) & 1:
                cr = self._cr(c)
                if cr & TEN and (cr >> 3) & 7 == 7:
                    self._trigger(c, False)
                else:
                    self.set("SWTRIGR", self.get("SWTRIGR") & ~(1 << (c.n - 1)))
        return self.get("SWTRIGR")

    def _sr_write(self, blk, reg, old, new):
        self._update_irq()
        return None

    def _dhr_write(self, blk, reg, old, new):
        nm = reg.name
        vals: list[tuple[int, int]] = []           # (channel index, 12-bit value)
        if nm.endswith("D"):
            if nm == "DHR12RD":
                vals = [(0, new & 0xFFF), (1, (new >> 16) & 0xFFF)]
            elif nm == "DHR12LD":
                vals = [(0, (new >> 4) & 0xFFF), (1, (new >> 20) & 0xFFF)]
            else:
                vals = [(0, (new & 0xFF) << 4), (1, ((new >> 8) & 0xFF) << 4)]
        else:
            i = int(nm[-1]) - 1
            if nm.startswith("DHR12R"):
                vals = [(i, new & 0xFFF)]
            elif nm.startswith("DHR12L"):
                vals = [(i, (new >> 4) & 0xFFF)]
            else:
                vals = [(i, (new & 0xFF) << 4)]
        for i, v in vals:
            c = self.ch[i]
            c.dhr = v
            if c.req:                                # the DMA's write acknowledges the request
                c.req = False
                dma_request(self.chip, f"DAC{c.n}", False)
            if not self._cr(c) & TEN:
                self._transfer_in(c, 1)
        return 0

    def _dhr_read(self, blk, reg):
        nm = reg.name
        d1, d2 = self.ch[0].dhr, self.ch[1].dhr
        if nm == "DHR12RD":
            return d1 | (d2 << 16)
        if nm == "DHR12LD":
            return (d1 << 4) | (d2 << 20)
        if nm == "DHR8RD":
            return (d1 >> 4) | ((d2 >> 4) << 8)
        d = self.ch[int(nm[-1]) - 1].dhr
        if nm.startswith("DHR12R"):
            return d
        if nm.startswith("DHR12L"):
            return d << 4
        return d >> 4
