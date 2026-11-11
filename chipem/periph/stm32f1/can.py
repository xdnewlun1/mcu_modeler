"""STM32F1 bxCAN (RM0008 ch. 24): the F4 bxCAN IP (`stm32.can`, chipem/periph/stm32/can.py) by parameter.

The CAN controller of the F103 is the same bxCAN as the STM32F4's CAN1 (same registers, modes, mailboxes, FIFOs,
filters, bit timing on PCLK1), so this model is `stm32.can` with the F1 differences:
* 14 filter banks (DS15060 2.3.19; F0R1..F13R2), all owned by the single controller: FMR.CAN2SB does not exist on the
  non-connectivity F1 (its field is read-only here and keeps the reset value 14).  F14R1..F27R2 are not mapped.
* register names: the F103 SVD prefixes them (CAN_MCR ...); the F405 table (same layout, plain names) is used.
* block "CAN", clock gate RCC_APB1ENR.CANEN (bit 25), pins CAN_RX / CAN_TX (data sheet names; AFIO CAN_REMAP moves
  them: PA11/PA12, PB8/PB9 or PD0/PD1).
* vectors: TX = USB_HP_CAN1_TX (19), RX0 = USB_LP_CAN1_RX0 (20), shared with the USB device; RX1 = CAN1_RX1, SCE =
  CAN1_SCE.
* the USB device and bxCAN share the 512-byte SRAM (DS15060 Figure 8 "shared 512 byte USB/CAN SRAM"; RM0008: they
  cannot be used concurrently):
  leaving Sleep/Initialization mode while the USB is active (stm32f1.usb `active()`) logs one `warn`.
Not verified against RM0008 (not available when written); the F4 model is verified against RM0090.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..stm32.can import BxCAN
from ._util import F4_REGS, irq_number

F1_BANKS = 14
IRQS = {"TX": "USB_HP_CAN1_TX", "RX0": "USB_LP_CAN1_RX0", "RX1": "CAN1_RX1", "SCE": "CAN1_SCE"}


@register("stm32f1.can")
class CAN(BxCAN):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params["regs_file"] = F4_REGS                 # same layout, unprefixed register names (CAN1 of the F405)
        params.setdefault("index", 1)
        params.setdefault("gate", "CAN")
        params.setdefault("filter_banks", F1_BANKS)
        params.setdefault("filters", True)
        super().__init__(name, base, size, regs, params=params, log=log)
        self.rx_signal = "CAN_RX"
        self.tx_signal = "CAN_TX"
        for b in range(F1_BANKS, 28):                  # banks 14..27 do not exist on the F1
            for j in (1, 2):
                r = self.regs.pop(f"F{b}R{j}", None)
                if r is not None:
                    for i in range(r.offset, r.offset + r.width):
                        self._by_off.pop(i, None)
        fmr = self.regs.get("FMR")
        if fmr is not None:                            # CAN2SB: connectivity line only
            fmr.ro_mask |= 0x3F00
            fmr.reset = (fmr.reset & ~0x3F00) | (F1_BANKS << 8)
        self._usb_warned = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for key, nm in IRQS.items():
            self._irqs[key] = irq_number(chip, nm)

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
