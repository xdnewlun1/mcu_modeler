"""Shared helpers for the STM32F1 models (RM0008 register layout).

Register tables come from the chip YAML's `regs_file` (the Chip passes it to every model as `params.regs_file`);
without one, `chips/stm32f103_regs.yaml` (generated from ST's STM32F103xx.svd) is used.  Peer lookup, IRQ numbers and
the clock-gated block base are the STM32F4 helpers (`chipem/periph/stm32/_util.py`): the F1 RCC model offers the same
interface as `stm32.rcc` (gate_location / check_gate / effective_apb_hz / effective_timer_hz / ppre / listeners /
gate_listeners / GATES), so the F4 models reused on the F1 by parameter (TIM, USART, SPI, I2C, bxCAN, IWDG, WWDG, CRC)
find it through `stm32._util.rcc()`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..base import Reg
from ..stm32 import _util as _f4
from ..stm32._util import (GatedBlock, find_model, fmt_hz, irq_number, rcc, shared_irq)  # noqa: F401 (re-exported)

DEFAULT_REGS = str(Path(__file__).resolve().parents[3] / "chips" / "stm32f103_regs.yaml")
F4_REGS = _f4.DEFAULT_REGS
HSI_HZ = 8_000_000.0
LSI_HZ = 40_000.0
LSE_HZ = 32_768.0


def build_regs(names: Iterable[str], size: int, overrides: dict[str, dict] | None = None,
               regs_file: str | None = None, skip: Iterable[str] = ()) -> list[Reg]:
    """`stm32._util.build_regs` with the F103 table as default."""
    return _f4.build_regs(names, size, overrides, regs_file or DEFAULT_REGS, skip)


def apb_hz(chip: Any, bus: int) -> float:
    return _f4.apb_hz(chip, bus)


def dma_request(chip: Any, name: str, level: bool = True) -> bool:
    return _f4.dma_request(chip, name, level)


def gpio_ports(chip: Any) -> list:
    """The chip's stm32f1.gpio blocks."""
    c = getattr(chip, "_stm32f1_gpios", None)
    if c is None:
        c = [b for b in (getattr(chip, "peripherals", None) or {}).values()
             if getattr(b, "model", None) == "stm32f1.gpio"]
        try:
            chip._stm32f1_gpios = c
        except AttributeError:
            pass
    return c


def afio(chip: Any) -> Any:
    a = getattr(chip, "_stm32f1_afio", None)
    if a is None:
        a = find_model(chip, "stm32f1.afio", "AFIO")
        if a is not None:
            try:
                chip._stm32f1_afio = a
            except AttributeError:
                pass
    return a


def exti_line(chip: Any, line: int, level: bool) -> None:
    """Internal EXTI line input (16 PVD, 17 RTC alarm, 18 USB wakeup): an edge selected by EXTI_RTSR/FTSR sets
    EXTI_PR when IMR enables the line (stm32f1.exti)."""
    lv = getattr(chip, "_stm32f1_exti_inputs", None)
    if lv is None:
        lv = {}
        try:
            chip._stm32f1_exti_inputs = lv
        except AttributeError:
            pass
    prev = lv.get(line, False)
    lv[line] = bool(level)
    if prev == bool(level):
        return
    ex = find_model(chip, "stm32f1.exti", "EXTI")
    if ex is not None:
        ex.line_event(line, bool(level))
