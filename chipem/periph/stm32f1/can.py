"""STM32F1 bxCAN (RM0008 ch. 24 "Controller area network (bxCAN)"): the F4 bxCAN IP (`stm32.can`,
chipem/periph/stm32/can.py, verified against RM0090 ch. 32) by parameter.

Checked against RM0008 Rev 21 ch. 24: the F1 bxCAN is the same IP (24.2-24.8: modes, mailboxes, FIFOs, filters,
time-triggered mode TTCM, bit timing on PCLK1, error management, interrupts) with the same register map and reset
values (24.9: MCR 0x0001 0002 incl. DBF, MSR 0x0000 0C02, TSR 0x1C00 0000, BTR 0x0123 0000, FMR 0x2A1C 0E01).  The
F1 differences, all parameters of this wrapper:
* `filter_banks` (default 14): "28 filter banks shared between CAN1 and CAN2 in connectivity line devices, 14 filter
  banks in other STM32F10xxx devices" (24.2, 24.3.4).  With 14 banks F14R1..F27R2 are not mapped, FM1R / FS1R /
  FFA1R / FA1R bits 27:14 are reserved (24.9.4 notes: read 0, writes ignored) and FMR.CAN2SB does not exist (kept at
  its reset value 14, read-only).  With 28 (connectivity line) CAN2SB is writable as on the F4 (24.9.4: banks
  0 .. CAN2SB - 1 for CAN1).
* `index` (1 or 2; CAN2 = slave on the connectivity line, no filter registers of its own: 24.9.5 "registers from offset
  0x200 to 0x31C are present only in CAN1"), `gate` (RCC enable bit name: "CAN" on the performance line (CANEN),
  "CAN1"/"CAN2" on the connectivity line), `signal` (pin function prefix: "CAN" -> CAN_RX / CAN_TX on the performance
  line, "CAN1"/"CAN2" on the connectivity line).
* vectors (RM0008 10.1.2 Tables 62/63): performance line TX = USB_HP_CAN1_TX (19), RX0 = USB_LP_CAN1_RX0 (20) shared
  with the USB device, RX1 = CAN1_RX1, SCE = CAN1_SCE; connectivity line CAN1_TX/RX0/RX1/SCE (19-22) and
  CAN2_TX/RX0/RX1/SCE (63-66).
* register names: the F103 SVD prefixes them (CAN_MCR ...); the F405 table (same layout, plain names) is used.
* performance line: the USB device and bxCAN share the 512-byte SRAM (RM0008 24.1 / DS15060 Figure 8; they cannot be
  used at the same time): leaving Sleep/Initialization mode while the USB is active (stm32f1.usb `active()`) logs one
  `warn` (the shared-memory corruption itself is not modelled).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..stm32.can import BxCAN
from ._util import F4_REGS, add_ro, irq_number

F1_BANKS = 14
IRQS = {"TX": ("USB_HP_CAN1_TX", "CAN1_TX"), "RX0": ("USB_LP_CAN1_RX0", "CAN1_RX0"), "RX1": ("CAN1_RX1",),
        "SCE": ("CAN1_SCE",)}
FILTER_REGS = ("FM1R", "FS1R", "FFA1R", "FA1R")


@register("stm32f1.can")
class CAN(BxCAN):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params.setdefault("reserved_zero", True)        # reserved offsets read 0, writes ignored
        params["regs_file"] = F4_REGS                 # same layout, unprefixed register names (CAN1 of the F405)
        idx = int(params.setdefault("index", 1))
        params.setdefault("gate", "CAN" if idx == 1 else f"CAN{idx}")
        banks = int(params.setdefault("filter_banks", F1_BANKS))
        if banks not in (14, 28):
            raise ValueError(f"{name}: stm32f1.can filter_banks must be 14 or 28 (RM0008 24.3.4)")
        params.setdefault("filters", idx == 1)
        super().__init__(name, base, size, regs, params=params, log=log)
        sig = str(params.get("signal", "CAN" if idx == 1 else f"CAN{idx}"))
        self.rx_signal = f"{sig}_RX"
        self.tx_signal = f"{sig}_TX"
        if banks < 28:
            for b in range(banks, 28):                 # banks 14..27 do not exist on the non-connectivity F1
                for j in (1, 2):
                    r = self.regs.pop(f"F{b}R{j}", None)
                    if r is not None:
                        for i in range(r.offset, r.offset + r.width):
                            self._by_off.pop(i, None)
            fmr = self.regs.get("FMR")
            if fmr is not None:                        # CAN2SB: connectivity line only
                add_ro(fmr, 0x3F00)
                fmr.reset = (fmr.reset & ~0x3F00) | (F1_BANKS << 8)
            for nm in FILTER_REGS:                     # 24.9.4: bits 27:14 reserved outside the connectivity line
                r = self.regs.get(nm)
                if r is not None:
                    add_ro(r, ((1 << 28) - 1) & ~((1 << banks) - 1))
        self._usb_warned = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self.index == 1:
            for key, nms in IRQS.items():
                self._irqs[key] = irq_number(chip, *nms)

    def active(self) -> bool:
        """Clock on and not in Sleep mode: the controller uses the shared SRAM."""
        return self._gate_on() and self.mode != "sleep"

    def _mcr_write(self, blk, reg, old, new):
        r = super()._mcr_write(blk, reg, old, new)
        if not self._usb_warned and self.chip is not None and not self.get("MCR") & 2:      # SLEEP left
            usb = self.chip.peripherals.get("USB")
            act = getattr(usb, "active", None)
            if act is not None and act():
                self._usb_warned = True
                self.emit("warn", block=self.name, msg="bxCAN and the USB device share the 512-byte SRAM and cannot "
                          "be used at the same time: CAN active while the USB is enabled")
        return r
