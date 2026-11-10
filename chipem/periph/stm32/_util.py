"""Shared helpers for the STM32 models: SVD-derived register lists, peer lookup, IRQ numbers, clock lookup.

Register tables come from the chip YAML's `regs_file` (the Chip passes it to every model as `params.regs_file`);
without one, `chips/stm32f405_regs.yaml` is used.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ...regtable import regs_for
from ..base import MMIOBlock, Reg

DEFAULT_REGS = str(Path(__file__).resolve().parents[3] / "chips" / "stm32f405_regs.yaml")
HSI_HZ = 16_000_000.0


def svd_regs(names: Iterable[str], regs_file: str | None = None):
    """First register table found among `names` (block name first, then the generic SVD name)."""
    for n in names:
        if not n:
            continue
        pr = regs_for(n, regs_file or DEFAULT_REGS)
        if pr is not None:
            return pr
    return None


def build_regs(names: Iterable[str], size: int, overrides: dict[str, dict] | None = None,
               regs_file: str | None = None, skip: Iterable[str] = ()) -> list[Reg]:
    """Reg descriptors from the SVD table for the first of `names`, with per-register keyword overrides
    (on_read, on_write, ro_mask, w1c_mask, reset). Registers outside the block or overlapping an earlier one
    (SVD alternate registers such as TIMx CCMR1_Input/CCMR1_Output) are dropped."""
    names = list(names)
    pr = svd_regs(names, regs_file)
    if pr is None:
        raise KeyError(f"no SVD register table for any of {names}")
    overrides = overrides or {}
    skip = set(skip)
    out: list[Reg] = []
    used: set[int] = set()
    for spec in pr.reg_specs():
        if spec["name"] in skip:
            continue
        spec.update(overrides.get(spec["name"], {}))
        off, w = spec["offset"], spec["width"]
        if off + w > size or any(i in used for i in range(off, off + w)):
            continue
        used.update(range(off, off + w))
        out.append(Reg(**spec))
    return out


def find_model(chip: Any, model: str, name: str | None = None) -> MMIOBlock | None:
    """Peer peripheral by name (preferred) or by model name."""
    if chip is None:
        return None
    periphs = getattr(chip, "peripherals", {}) or {}
    if name is not None:
        b = periphs.get(name)
        if b is not None and getattr(b, "model", None) == model:
            return b
    for b in periphs.values():
        if getattr(b, "model", None) == model:
            return b
    return None


def irq_number(chip: Any, *names: str) -> int | None:
    irqs = getattr(getattr(chip, "desc", None), "irqs", None) or {}
    for want in names:
        for n, nm in irqs.items():
            if nm == want:
                return n
    return None


def dma_request(chip: Any, name: str, level: bool = True) -> bool:
    """Assert (level=True) or deassert a DMA request on every stm32.dma controller of the chip (DMA1 and DMA2).
    `name` is the RM0090 Table 42/43 request name ("USART1_TX", "TIM3_UP", "ADC1", ...). Returns True if a controller
    maps the request to some stream/channel (whether or not a stream is enabled), False if none does (ignored).
    See chipem/periph/stm32/dma.py for the request/acknowledge semantics."""
    ctls = getattr(chip, "_stm32_dmas", None)
    if ctls is None:
        from .dma import controllers
        ctls = controllers(chip)
    routed = False
    for c in ctls:
        routed |= c.request(name, level)
    return routed


def rcc(chip: Any) -> Any:
    """The chip's RCC model (cached on the chip), or None."""
    r = getattr(chip, "_stm32_rcc", None)
    if r is None:
        r = find_model(chip, "stm32.rcc", "RCC")
        if r is not None:
            try:
                chip._stm32_rcc = r
            except AttributeError:
                pass
    return r


def apb_hz(chip: Any, bus: int) -> float:
    """APB1/APB2 peripheral clock (PCLK1/PCLK2), consistent with the clock the core actually runs at."""
    r = rcc(chip)
    if r is not None:
        return r.effective_apb_hz(bus)
    return float(chip.clock.hz)


def timer_hz(chip: Any, bus: int) -> float:
    """Timer kernel clock on APB1/APB2 (x2 when the APB prescaler is not 1)."""
    r = rcc(chip)
    if r is not None:
        return r.effective_timer_hz(bus)
    return float(chip.clock.hz)


def fmt_hz(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz / 1e6:.3f} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.3f} kHz"
    return f"{hz:.0f} Hz"


class GatedBlock(MMIOBlock):
    """MMIOBlock whose accesses are checked against an RCC clock-enable bit (`gate` = RCC gate name, e.g.
    "GPIOA"). A gated-off access logs one `warn` per peripheral (real silicon ignores writes and reads 0;
    here the access proceeds) ."""

    gate: str | None = None
    _gate_loc: tuple[int, int] | None = None
    _rcc: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._rcc = rcc(chip)
        self._gate_loc = self._rcc.gate_location(self.gate) if (self._rcc is not None and self.gate) else None

    def _gate_ok(self) -> bool:
        loc = self._gate_loc
        if loc is None:
            return True
        off, mask = loc
        if self._rcc.mem[off] & mask:
            return True
        self._rcc.check_gate(self.gate, self.name)
        return False


def shared_irq(chip: Any, n: int | None, who: str, on: bool) -> None:
    """Drive NVIC line n as the OR of several blocks (TIM6_DAC, HASH_RNG, ...): `who` asserts/deasserts its share.
    Uses the same per-chip table as stm32.tim (`chip._stm32_tim_lines`), so a timer and another block sharing a
    vector combine correctly."""
    if n is None or chip is None:
        return
    shared = getattr(chip, "_stm32_tim_lines", None)
    if shared is None:
        shared = {}
        try:
            chip._stm32_tim_lines = shared
        except AttributeError:
            pass
    s = shared.setdefault(n, set())
    if on:
        s.add(who)
    else:
        s.discard(who)
    chip.arch.set_irq(n, bool(s))


def exti_input(chip: Any, line: int, level: bool) -> None:
    """Internal EXTI line input (16 PVD, 17 RTC alarm, 21 tamper/timestamp, 22 RTC wakeup): an edge selected by
    EXTI_RTSR/FTSR sets EXTI_PR (when IMR enables the line) and raises the line's NVIC vector through the EXTI
    model.  Levels are remembered per chip so only edges count."""
    lv = getattr(chip, "_stm32_exti_inputs", None)
    if lv is None:
        lv = {}
        try:
            chip._stm32_exti_inputs = lv
        except AttributeError:
            pass
    prev = lv.get(line, False)
    lv[line] = bool(level)
    if prev == bool(level):
        return
    ex = find_model(chip, "stm32.exti", "EXTI")
    if ex is None:
        return
    ex.line_event(line, bool(level))


def part_present(chip: Any, params: dict | None) -> bool:
    """Is a block that exists only on some members of the die family bonded out on this chip?  `params.only` lists
    part-name prefixes ("STM32F407", "STM32F417"): the block is present when the chip YAML name starts with one of
    them (no `only`: always present).  F405/F407/F415/F417 share one die and one base chip YAML
    (chips/stm32f405zg.yaml), so Ethernet / DCMI (F407/F417) and CRYP / HASH (F415/F417) are gated this way."""
    only = (params or {}).get("only")
    if not only:
        return True
    if isinstance(only, str):
        only = [only]
    name = str(getattr(getattr(chip, "desc", None), "name", "") or "").upper()
    return any(name.startswith(str(p).upper()) for p in only)


def make_absent(block: MMIOBlock, what: str) -> None:
    """Turn a model into the behaviour of the `stub` model (RAM-like storage, first touch of each word logged as an
    unmodelled access) because the peripheral is not present on this part; the first access logs one `warn`.  Call
    from attach(); the model must skip its own behaviour while `block.absent` is True."""
    block.absent = True
    block.regs = {}
    block._by_off = {}
    block.ram_like = True
    block.mem[:] = bytes(len(block.mem))
    name = getattr(getattr(block.chip, "desc", None), "name", "?")
    seen = []

    def first() -> None:
        if not seen:
            seen.append(True)
            block.emit("warn", block=block.name, msg=f"{block.name}: {what} is not present on the {name}: the "
                       f"access goes to an unmodelled stub (model {block.model} inactive)")

    def rd(offset: int, size: int) -> int:
        first()
        return MMIOBlock.read(block, offset, size)

    def wr(offset: int, size: int, value: int) -> None:
        first()
        MMIOBlock.write(block, offset, size, value)
    block.read = rd
    block.write = wr


def gpio_resync(chip: Any, sig: str) -> None:
    """Ask every STM32 GPIO port to re-evaluate the pins carrying `sig` (after a peripheral's `signal_state`
    changed)."""
    for b in (getattr(chip, "peripherals", None) or {}).values():
        if getattr(b, "model", None) == "stm32.gpio" and hasattr(b, "resync_signal"):
            b.resync_signal(sig)


def signal_pin_level(chip: Any, sig: str) -> str | None:
    """Level ("H"/"L"/"X") of the first pin currently owned by peripheral signal `sig` (a pin muxed to it), or None
    when no pin carries it."""
    board = getattr(chip, "pins", None)
    if board is None:
        return None
    for p in board.signal_pins(sig):
        if board.signal_matches(p.owner, sig):
            return p.level()
    return None
