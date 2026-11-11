"""STM32F1 EXTI (RM0008 ch. 10 layout: IMR/EMR/RTSR/FTSR/SWIER/PR, lines 0-18 on the F103 performance line).

The register IP is the F4 one: this is `stm32.exti` (chipem/periph/stm32/exti.py: edge detection on the pin selected
for each line, PR write-1-to-clear also clearing SWIER, EMR events for WFE, pin discovery) with the F1 wiring:
* the port of lines 0-15 comes from AFIO_EXTICR1-4 (stm32f1.afio) instead of SYSCFG;
* lines 16 PVD, 17 RTC alarm (stm32f1.rtc, through `_util.exti_line`), 18 USB wakeup (stm32f1.usb); 19-22 do not
  exist (their PR/IMR bits are reserved in the F103 SVD and stay 0);
* NVIC vectors EXTI0..EXTI4, EXTI9_5, EXTI15_10, PVD, RTC_Alarm, USBWakeUp (looked up by name in the chip YAML);
* wake-up / WFE events go to stm32f1.pwr.
Not verified against RM0008 (not available when written).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..stm32.exti import EXTI as _F4EXTI
from ._util import find_model, irq_number

LINE_IRQS = (["EXTI0", "EXTI1", "EXTI2", "EXTI3", "EXTI4"] + ["EXTI9_5"] * 5 + ["EXTI15_10"] * 6
             + ["PVD", "RTC_Alarm", "USBWakeUp"])


class _AfioPorts:
    """SYSCFG-like adapter for stm32.exti: port_index(line) from AFIO_EXTICR."""

    def __init__(self, afio: Any):
        self.afio = afio

    def port_index(self, line: int) -> int:
        return self.afio.exti_port(line)


@register("stm32f1.exti")
class EXTI(_F4EXTI):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        from ._util import DEFAULT_REGS
        params["regs_file"] = params.get("regs_file") or DEFAULT_REGS     # stm32.exti defaults to the F405 table
        super().__init__(name, base, size, regs, params=params, log=log)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        a = find_model(chip, "stm32f1.afio", "AFIO")
        self._syscfg = _AfioPorts(a) if a is not None else None
        if a is not None:
            a.exti = self
        self._pwr = find_model(chip, "stm32f1.pwr", "PWR")
        for ln in range(len(self._irqn)):
            self._irqn[ln] = irq_number(chip, LINE_IRQS[ln]) if ln < len(LINE_IRQS) else None

