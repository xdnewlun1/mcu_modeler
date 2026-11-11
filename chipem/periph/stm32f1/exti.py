"""STM32F1 EXTI (RM0008 10.2-10.3: IMR/EMR/RTSR/FTSR/SWIER/PR).

Verified against RM0008 Rev 21 10.2 (10.2.3 wake-up events, 10.2.4 functional description, 10.2.5 line mapping) and
10.3.1-10.3.7.  The register IP is the F4 one: this is `stm32.exti` (chipem/periph/stm32/exti.py: edge detection on the
pin selected for each line, rising and/or falling, PR rc_w1, a 0 -> 1 SWIER write sets PR when IMR enables the line
and clearing PR clears SWIER (10.3.5), PR is set only for unmasked lines; an event line (EMR) generates a pulse for WFE
and does not set PR (10.2.4); pin discovery) with the F1 wiring:
* lines 0-15: the port is chosen by AFIO_EXTICR1-4 (stm32f1.afio, 0000 = PA ... 0110 = PG, 9.4.3-9.4.6) instead of
  SYSCFG.  PD0/PD1 remapped onto OSC_IN/OSC_OUT (36/48/64-pin packages) have no EXTI function (9.3.2 note): pins whose
  `analog` function is OSC_IN/OSC_OUT are not watched;
* line 16 PVD (stm32f1.pwr), 17 RTC alarm (stm32f1.rtc, through `_util.exti_line`), 18 USB wakeup (stm32f1.usb), and
  on the connectivity line 19 Ethernet wakeup (10.2.5).  `params.lines`: 19 (default) or 20 (connectivity line); the
  bits above are reserved (read 0).  A rising RTC alarm input (line 17) is also a PWR wake-up event (PWR_CSR.WUF,
  Standby exit) whatever the EXTI configuration (5.3.6: "no need to configure EXTI line 17" for Standby);
* NVIC vectors (Tables 61-63, looked up by name in the chip YAML): EXTI0..EXTI4, EXTI9_5, EXTI15_10, PVD, RTC_Alarm
  (or RTCAlarm), USBWakeUp (OTG_FS_WKUP on the connectivity line), ETH_WKUP;
* wake-up / WFE events go to stm32f1.pwr.
Not modelled: the rule that an edge arriving while RTSR/FTSR is being written is lost (10.3.3/10.3.4), the 32-bit-only
access rule (10.3).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..stm32.exti import EXTI as _F4EXTI
from ._util import find_model, irq_number

LINE_IRQS: list[tuple[str, ...]] = ([("EXTI0",), ("EXTI1",), ("EXTI2",), ("EXTI3",), ("EXTI4",)]
                                    + [("EXTI9_5",)] * 5 + [("EXTI15_10",)] * 6
                                    + [("PVD",), ("RTC_Alarm", "RTCAlarm"),
                                       ("USBWakeUp", "USBWakeup", "USB_FS_WKUP", "OTG_FS_WKUP"), ("ETH_WKUP",)])


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
        self.n_lines = int(params.get("lines", 19))
        ro = ~((1 << self.n_lines) - 1) & 0xFFFFFFFF
        for rn in ("IMR", "EMR", "RTSR", "FTSR", "SWIER", "PR"):
            reg = self.regs.get(rn)
            if reg is not None:
                r, _, w1c = reg._raw
                reg._raw = (r & ~ro, ro, w1c & ~ro)
                reg.set_unit_bits(reg.unit_bits)
        self._no_exti: set[tuple[int, int]] = set()

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        a = find_model(chip, "stm32f1.afio", "AFIO")
        self._syscfg = _AfioPorts(a) if a is not None else None
        if a is not None:
            a.exti = self
        self._pwr = find_model(chip, "stm32f1.pwr", "PWR")
        for ln in range(len(self._irqn)):
            self._irqn[ln] = irq_number(chip, *LINE_IRQS[ln]) if ln < min(len(LINE_IRQS), self.n_lines) else None
        for key, pin in list(self._pins.items()):
            if str((pin.functions or {}).get("analog") or "").startswith("OSC_"):
                self._no_exti.add(key)                 # PD0/PD1 on OSC_IN/OSC_OUT: no EXTI (9.3.2)
                del self._pins[key]

    def _on_level(self, pi: int, line: int, pin: Any) -> None:
        if (pi, line) in self._no_exti:
            return
        super()._on_level(pi, line, pin)
