"""Kinetis Programmable Delay Block (PDB0, K22 RM ch. 37).

Registers (from the SVD table): SC, MOD, CNT (live, read-only), IDLY, per channel n (0..1) CHnC1, CHnS, CHnDLY0,
CHnDLY1, DAC interval triggers DACINTCn/DACINTn and pulse-out POEN/POnDLY (storage only).

SC fields: LDOK 0, CONT 1, MULT 2-3, PDBIE 5, PDBIF 6 (write 0 to clear), PDBEN 7, TRGSEL 8-11 (15 = software),
PRESCALER 12-14, DMAEN 15, SWTRIG 16 (write 1 = trigger, reads 0), PDBEIE 17, LDMOD 18-19.

Behaviour:
* A trigger (SWTRIG write with PDBEN=1 and TRGSEL=15) restarts the counter at 0. It counts at
  bus_clock / (2^PRESCALER * {1,10,20,40}[MULT]). CNT is computed from elapsed virtual time; nothing polls.
* Pre-trigger m of channel n (CHnC1.EN bit m) hardware-triggers ADCn conversion SC1[m] (m = 0 -> SC1A, 1 -> SC1B;
  pre-triggers 2..7 have no ADC register on K22 and are ignored) via `ADC.hw_trigger(m)`: immediately at the
  trigger when CHnC1.TOS bit m is clear, at CNT == CHnDLYm when it is set (and sets CHnS.CF bit m). The ADC
  ignores the trigger unless its SC2.ADTRG is set.
* Back-to-back (CHnC1.BB bit m): pre-trigger m (m >= 1) fires when ADCn finishes conversion SC1[m-1]; pre-trigger 0
  of channel n fires when the previous channel's ADC (n-1, wrapping) finishes SC1B. BB pre-triggers are not fired by
  the counter.
* Sequence error: a pre-trigger asserted while the target ADC result is still unread (COCO set) or still converting
  sets CHnS.ERR bit m (write 0 to clear); IRQ "PDB0" while ERR and PDBEIE. The conversion is triggered anyway.
* CNT == IDLY sets PDBIF; IRQ "PDB0" while PDBIF and PDBIE, unless SC.DMAEN, which raises DMA request 48 instead.
* CNT == MOD: in continuous mode the counter wraps to 0 (period MOD+1 counts) and the cycle repeats, including the
  TOS=0 pre-triggers; in one-shot mode the counter stops and CNT reads 0.
* MOD, IDLY and CHnDLYm are buffered: written values take effect when loaded. LDMOD=0 loads when LDOK is written
  (PDBEN must be 1); LDMOD=1/2/3 load at the next counter wrap or trigger (best-effort). LDOK reads 1 until loaded.
* PDBEN=0 stops the counter, clears CNT and LDOK.
* TRGSEL other than 15 (external pin, CMP, PIT, FTM, ... sources) is logged once and never triggers.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .clockutil import bus_hz, now_ns
from .dma import dma_request

LDOK, CONT, PDBIE, PDBIF, PDBEN = 1, 2, 1 << 5, 1 << 6, 1 << 7
DMAEN, SWTRIG, PDBEIE = 1 << 15, 1 << 16, 1 << 17
TRG_SOFTWARE = 15
MULTS = (1, 10, 20, 40)
NCH = 2
DMA_SOURCE = 48


@register("kinetis.pdb")
class PDB(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "PDB")
        ov: dict[str, dict] = {
            "SC": {"on_write": self._sc_write},
            "CNT": {"on_read": self._cnt_read},
        }
        for n in range(NCH):
            ov[f"CH{n}S"] = {"on_write": self._chs_write}
            ov[f"CH{n}C1"] = {"on_write": self._c1_write}
        r = build_regs((name, "PDB0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.nch = sum(1 for n in range(NCH) if f"CH{n}C1" in self.regs)
        self._ev: Any = None
        self._irqn: int | None = None
        self._line = False
        self._adcs: list[Any] = [None] * self.nch
        self._warned_trg = False
        self.triggers = 0             # statistics: counter (re)starts, including continuous wraps
        self._init_state()

    def _init_state(self) -> None:
        self.running = False
        self.t0_ns = 0.0
        self.tick_ns = 1.0
        self.frozen_cnt = 0
        self.mod = self.regs["MOD"].reset
        self.idly = self.regs["IDLY"].reset
        self.dly = [[0, 0] for _ in range(self.nch)]
        self._pts: list[int] = []
        self._pi = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.name, "PDB0", "PDB")
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(int(self.params.get("dma_source", DMA_SOURCE)), self._dma_active)
        for n in range(self.nch):
            adc = find_model(chip, "kinetis.adc", f"ADC{n}")
            if adc is not None and getattr(adc, "index", n) != n:
                adc = None
            self._adcs[n] = adc
            if adc is not None and hasattr(adc, "on_complete"):
                adc.on_complete.append(self._adc_complete)

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self._init_state()
        self._warned_trg = False
        if self.chip is not None:
            self._update_irq()

    # ------------------------------------------------------------------ counter
    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def cnt(self) -> int:
        if not self.running or self.chip is None:
            return self.frozen_cnt
        el = int((now_ns(self.chip) - self.t0_ns) / self.tick_ns + 1e-6)
        return max(0, min(el, self.mod))

    def _cnt_read(self, blk, reg):
        return self.cnt()

    def _divider(self) -> int:
        sc = self.get("SC")
        return (1 << ((sc >> 12) & 7)) * MULTS[(sc >> 2) & 3]

    def _load(self) -> None:
        """Buffered MOD/IDLY/CHnDLYm -> active values; clears LDOK."""
        self.mod = self.get("MOD") & 0xFFFF
        self.idly = self.get("IDLY") & 0xFFFF
        for n in range(self.nch):
            self.dly[n] = [self.get(f"CH{n}DLY0") & 0xFFFF, self.get(f"CH{n}DLY1") & 0xFFFF]
        self.set("SC", self.get("SC") & ~LDOK)

    def _points(self) -> list[int]:
        """Counter values (0..MOD) at which something happens in a cycle."""
        pts = set()
        for n in range(self.nch):
            c1 = self.get(f"CH{n}C1")
            en, tos, bb = c1 & 0xFF, (c1 >> 8) & 0xFF, (c1 >> 16) & 0xFF
            for m in range(2):
                bit = 1 << m
                if en & tos & bit and not bb & bit and self.dly[n][m] <= self.mod:
                    pts.add(self.dly[n][m])
        if self.idly <= self.mod:
            pts.add(self.idly)
        return sorted(pts)

    def _start_cycle(self, t_ns: float) -> None:
        """Counter (re)starts at 0 at time t_ns: trigger or continuous-mode wrap."""
        self._cancel()
        sc = self.get("SC")
        if sc & LDOK and (sc >> 18) & 3:
            self._load()
        self.running = True
        self.t0_ns = t_ns
        self.tick_ns = self._divider() * 1e9 / bus_hz(self.chip)
        self.triggers += 1
        # pre-triggers without delay (TOS clear) fire at the trigger
        for n in range(self.nch):
            c1 = self.get(f"CH{n}C1")
            en, tos, bb = c1 & 0xFF, (c1 >> 8) & 0xFF, (c1 >> 16) & 0xFF
            for m in range(2):
                bit = 1 << m
                if en & bit and not tos & bit and not bb & bit:
                    self._pretrigger(n, m)
        self._pts = self._points()
        self._pi = 0
        self._schedule()

    def _schedule(self) -> None:
        """Schedule the next compare point (or the end of the cycle) after the current index."""
        self._cancel()
        if not self.running:
            return
        if self._pi < len(self._pts):
            v = self._pts[self._pi]
            self._ev = self.chip.sched.at_ns(self.t0_ns + v * self.tick_ns, self._compare, v)
        else:
            self._ev = self.chip.sched.at_ns(self.t0_ns + (self.mod + 1) * self.tick_ns, self._wrap)

    def _resync(self) -> None:
        """Active values or channel setup changed while running: recompute pending compare points."""
        if not self.running:
            return
        now = now_ns(self.chip)
        if (now - self.t0_ns) / self.tick_ns >= self.mod + 1:       # new MOD already passed: wrap now
            self._cancel()
            self.t0_ns = now - (self.mod + 1) * self.tick_ns
            self._wrap()
            return
        c = self.cnt()
        self._pts = [v for v in self._points() if v > c]
        self._pi = 0
        self._schedule()

    def _compare(self, v: int) -> None:
        self._ev = None
        self._pi += 1
        for n in range(self.nch):
            c1 = self.get(f"CH{n}C1")
            en, tos, bb = c1 & 0xFF, (c1 >> 8) & 0xFF, (c1 >> 16) & 0xFF
            for m in range(2):
                bit = 1 << m
                if en & tos & bit and not bb & bit and self.dly[n][m] == v:
                    self.set(f"CH{n}S", self.get(f"CH{n}S") | (bit << 16))
                    self._pretrigger(n, m)
        if self.idly == v:
            self.set("SC", self.get("SC") | PDBIF)
            self._update_irq()
            if self.get("SC") & DMAEN:
                dma_request(self.chip, int(self.params.get("dma_source", DMA_SOURCE)))
        if self.running and self._ev is None:
            self._schedule()

    def _wrap(self) -> None:
        self._ev = None
        t = self.t0_ns + (self.mod + 1) * self.tick_ns
        if self.get("SC") & CONT:
            self._start_cycle(t)
        else:
            self.running = False
            self.frozen_cnt = 0
            sc = self.get("SC")
            if sc & LDOK and (sc >> 18) & 3 in (1, 3):
                self._load()

    def _stop(self) -> None:
        self._cancel()
        self.running = False
        self.frozen_cnt = 0

    # ------------------------------------------------------------------ ADC side
    def _pretrigger(self, n: int, m: int) -> None:
        adc = self._adcs[n] if n < len(self._adcs) else None
        if adc is None:
            return
        sc1 = adc.get("SC1A" if m == 0 else "SC1B")
        busy = getattr(adc, "busy", None)
        if sc1 & 0x80 or (busy is not None and busy(m)):
            self.set(f"CH{n}S", self.get(f"CH{n}S") | (1 << m))
            self._update_irq()
        adc.hw_trigger(m)

    def _adc_complete(self, adc: Any, k: int) -> None:
        """ADC conversion SC1[k] finished: fire back-to-back pre-triggers chained to it."""
        if not self.running or not self.get("SC") & PDBEN:
            return
        try:
            n = self._adcs.index(adc)
        except ValueError:
            return
        targets = []
        if k == 0:
            targets.append((n, 1))
        else:
            targets.append(((n + 1) % self.nch, 0))
        for cn, m in targets:
            c1 = self.get(f"CH{cn}C1")
            bit = 1 << m
            if c1 & bit and (c1 >> 16) & bit:
                self._pretrigger(cn, m)

    # ------------------------------------------------------------------ IRQ / DMA
    def _dma_active(self) -> bool:
        sc = self.get("SC")
        return bool(sc & DMAEN and sc & PDBIF)

    def _update_irq(self) -> None:
        if self.chip is None or self._irqn is None:
            return
        sc = self.get("SC")
        on = bool(sc & PDBIF and sc & PDBIE and not sc & DMAEN)
        if sc & PDBEIE:
            on = on or any(self.get(f"CH{n}S") & 0xFF for n in range(self.nch))
        if on != self._line:
            self._line = on
            self.irq(self._irqn, on)

    # ------------------------------------------------------------------ register hooks
    def _sc_write(self, blk, reg, old, new):
        # PDBIF: write 0 to clear (writing 1 keeps the current state); SWTRIG reads 0
        new = (new & ~PDBIF) | (old & new & PDBIF)
        swtrig = bool(new & SWTRIG)
        new &= ~SWTRIG
        if not new & PDBEN:
            new &= ~LDOK
            self._store(reg, new)
            if old & PDBEN:
                self._stop()
            self._update_irq()
            return new
        self._store(reg, new)
        trgsel = (new >> 8) & 0xF
        if trgsel != TRG_SOFTWARE and new & LDOK and not self._warned_trg:      # configuration finished
            self._warned_trg = True
            self.emit("info", block=self.name, msg=f"{self.name}: trigger source TRGSEL={trgsel} is not modelled "
                      "(only the software trigger, TRGSEL=15); the counter will not start")
        if new & LDOK and not (new >> 18) & 3:
            self._load()
            self._resync()
        if swtrig and trgsel == TRG_SOFTWARE:
            self._start_cycle(now_ns(self.chip))
        self._update_irq()
        return self.get("SC")

    def _chs_write(self, blk, reg, old, new):
        # ERR (7..0) and CF (23..16): write 0 to clear
        flags = 0x00FF00FF
        v = (new & ~flags) | (old & new & flags)
        self._store(reg, v)
        self._update_irq()
        return v

    def _c1_write(self, blk, reg, old, new):
        if (old ^ new) & 0xFFFFFF:
            self._store(reg, new)
            self._resync()
        return None
