"""STM32F4 random number generator (RM0090 ch. 24).

Registers from the SVD: CR (+0: RNGEN bit 2, IE bit 3), SR (+4: DRDY 0, CECS 1, SECS 2, CEIS 5, SEIS 6), DR (+8).
* Clock: RNG_CLK = PLL48CLK, the main PLL's Q output (RCC PLLCFGR: input / PLLM x PLLN / PLLQ), running only while
  RCC_CR.PLLON = 1 (`params.clk_hz` overrides it). With CR.RNGEN = 1 a new 32-bit number is ready 40 RNG_CLK periods
  after enable and after every DR read (24.3.1); SR.DRDY = 1 then, and reading DR returns it and clears DRDY.
  Scheduled with `sched.at_ns` (PLL48CLK does not follow the core clock prescalers).
* Clock error (24.3.2): RNG_CLK below HCLK / 16, or stopped (PLL off / PLLQ < 2), sets CECS and CEIS and no numbers
  are produced; CECS clears when the clock is correct again (checked at enable, at DR reads, at SR reads while
  CECS = 1 and at every RCC clock-tree change), CEIS is rc_w0.
* Values: deterministic, from a Python PRNG seeded with `params.seed` (default 0x5EED) so runs are repeatable; a new
  value never equals the previous one (24.3.1: firmware compares consecutive numbers and discards a repeat, the
  FIPS continuous test). Seed errors (SECS/SEIS: 64 identical bits, 32 alternations) never occur; tests can call
  `inject_seed_error()`. SEIS is rc_w0, SECS clears on the next good number.
* IRQ "HASH_RNG" (shared with HASH, OR'd): IE & (DRDY | CEIS | SEIS).
Clock gate RCC_AHB2ENR.RNGEN (a gated-off access logs one `warn`); RCC_AHB2RSTR resets the block.
"""
from __future__ import annotations

import random
from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number, shared_irq

RNGEN, IE = 1 << 2, 1 << 3
DRDY, CECS, SECS, CEIS, SEIS = 1, 2, 4, 1 << 5, 1 << 6
PLLON = 1 << 24


@register("stm32.rng")
class RNG(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "RNG"
        ov = {"CR": {"on_write": self._cr_write},
              "SR": {"ro_mask": 0xFFFFFFFF & ~(CEIS | SEIS), "on_write": self._sr_write, "on_read": self._sr_read,
                     "on_peek": self._peek_stored},
              "DR": {"ro_mask": 0xFFFFFFFF, "on_read": self._dr_read, "on_peek": self._peek_stored}}
        r = build_regs((name, "RNG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.seed = int(params.get("seed", 0x5EED))
        self.clk_override = params.get("clk_hz")
        self._prng = random.Random(self.seed)
        self._last: int | None = None
        self._ev = None
        self._irqn: int | None = None
        self.generated = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "HASH_RNG", "RNG")
        if self._rcc is not None:
            self._rcc.listeners.append(self._clock_changed)

    def reset(self) -> None:
        if getattr(self, "_ev", None) is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        super().reset()
        if self.chip is not None:
            shared_irq(self.chip, self._irqn, self.name, False)

    # ------------------------------------------------------------------ clock
    def rng_hz(self) -> float:
        if self.clk_override is not None:
            return float(self.clk_override)
        r = self._rcc
        if r is None:
            return 48e6
        if not r.get("CR") & PLLON:
            return 0.0
        return r.pll_q_hz()

    def _clock_ok(self) -> bool:
        hz = self.rng_hz()
        return hz > 0 and hz >= self.chip.clock.hz / 16

    def _check_clock(self) -> bool:
        ok = self._clock_ok()
        sr = self.get("SR")
        if ok:
            if sr & CECS:
                self.set("SR", sr & ~CECS)
        elif not sr & CECS:
            self.set("SR", sr | CECS | CEIS)
            self.emit("warn", block=self.name, msg=f"{self.name}: clock error: RNG_CLK {self.rng_hz() / 1e6:.3f} MHz "
                      f"(PLL48CLK) is below HCLK/16 or stopped: no random numbers")
        self._update()
        return ok

    def _clock_changed(self) -> None:
        if self.chip is not None and self.get("CR") & RNGEN:
            if self._check_clock() and self._ev is None and not self.get("SR") & DRDY:
                self._schedule()

    # ------------------------------------------------------------------ generation
    def _schedule(self) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = self.chip.sched.in_ns(40 * 1e9 / self.rng_hz(), self._ready)

    def _ready(self) -> None:
        self._ev = None
        if not self.get("CR") & RNGEN or not self._check_clock():
            return
        v = self._prng.getrandbits(32)
        while v == self._last:
            v = self._prng.getrandbits(32)
        self._last = v
        self.generated += 1
        self.set("DR", v)
        self.set("SR", (self.get("SR") | DRDY) & ~SECS)
        self._update()

    def inject_seed_error(self) -> None:
        """Test hook: a seed error (SECS, SEIS) as if the noise source had failed."""
        self.set("SR", (self.get("SR") | SECS | SEIS) & ~DRDY)
        self._update()

    def _update(self) -> None:
        if self.chip is None:
            return
        on = bool(self.get("CR") & IE and self.get("SR") & (DRDY | CEIS | SEIS))
        shared_irq(self.chip, self._irqn, self.name, on)

    # ------------------------------------------------------------------ register hooks
    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _cr_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new & RNGEN and not old & RNGEN:
            if self._check_clock():
                self._schedule()
        elif not new & RNGEN and old & RNGEN:
            if self._ev is not None:
                self.chip.sched.cancel(self._ev)
                self._ev = None
        self._update()
        return None

    def _peek_stored(self, blk, reg):
        return None

    def _sr_read(self, blk, reg):
        if self.get("SR") & CECS and self.chip is not None:      # clock may be back (PLL started): re-check
            self._clock_changed()
        return self.get("SR")

    def _sr_write(self, blk, reg, old, new):
        v = (old & ~(CEIS | SEIS)) | (old & new & (CEIS | SEIS))     # rc_w0
        self._store(reg, v)
        self._update()
        return v

    def _dr_read(self, blk, reg):
        v = self.get("DR")
        sr = self.get("SR")
        if sr & DRDY:
            self.set("SR", sr & ~DRDY)
            if self.get("CR") & RNGEN and self._check_clock():
                self._schedule()
            self._update()
        return v
