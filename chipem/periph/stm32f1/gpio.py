"""STM32F1 GPIO ports and AFIO (RM0008 ch. 9 layout: CRL/CRH/IDR/ODR/BSRR/BRR/LCKR; AFIO EVCR/MAPR/EXTICR1-4).

Not verified against RM0008 (not available when written): register layout from the SVD, behaviour as documented by
ST for the F1 family (see docs/LIMITATIONS.md "STM32F103T6").

This module is the only F1 code that turns pin registers into PinBoard state (`mcu_update`) and pin levels into IDR.
There is no per-pin alternate-function number on the F1: a pin configured as "alternate function output" is driven by
the peripheral output that the AFIO remap routes to it, and every peripheral input reads its pin whatever the mode.

Per pin, 4 bits in CRL (pins 0-7) / CRH (8-15): MODE[1:0] (00 input, 01/10/11 output 10/2/50 MHz) and CNF[1:0]:
* input  CNF 00 analog: owner = the pin's ADC channel ("ADC12_IN0"), else "analog"; direction "analog".
         CNF 01 floating (reset state), CNF 10 pull-up (ODR bit = 1) / pull-down (ODR bit = 0), CNF 11 reserved
         (treated as floating, one `warn`).  Owner "GPIO", or the peripheral input routed here when its block is
         clocked and uses the input: USARTx_RX/CTS, CAN_RX (clock on), SPIx_* (stm32.spi `signal_state` says input),
         TIMx_CHn in input-capture mode (CCxS != 0), TIMx_ETR (external clock / ETR trigger), TIMx_BKIN (BDTR.BKE).
* output CNF 00 push-pull / 01 open-drain: owner "GPIO", drive = ODR bit (open-drain 1 = released, direction "in").
         CNF 10 AF push-pull / 11 AF open-drain: owner = the routed peripheral output whose block is clocked (the first
         in Table 5 order when several are; DS15060 note 4: enable only one), direction/idle drive from the F1 signal
         table (`signals.py`), overridden live like stm32.gpio (stm32.tim `signal_driven`, stm32.spi `signal_state`).
         No routed output at all: owner "disabled", one `warn`.
Pins that a system function owns whatever CRL/CRH say:
* SWJ debug pins (PA13 JTMS/SWDIO, PA14 JTCK/SWCLK, PA15 JTDI, PB3 JTDO, PB4 NJTRST) until AFIO_MAPR.SWJ_CFG releases
  them (001: NJTRST; 010: JTAG-DP off, SW-DP on (PA15/PB3/PB4 free); 100: all free; SWJ_CFG is write-only and
  reads 0).  Internal pulls as on silicon: JTMS/JTDI/NJTRST pull-up, JTCK pull-down.
* OSC_IN/OSC_OUT (PD0/PD1 on the 36/48/64-pin parts): oscillator pins unless AFIO_MAPR.PD01_REMAP = 1 and the HSE is
  off (the HSE takes them back when HSEON = 1).
* USBDM/USBDP (PA11/PA12): the USB transceiver as soon as the USB block is clocked and powered (CNTR.PDWN = 0,
  stm32f1.usb `transceiver_on()`).
Remap: AFIO_MAPR SPI1/I2C1/USART1/USART2/TIM1/TIM2/TIM3/CAN/PD01 fields move signals between pins (`REMAP` table
below); a MAPR write re-evaluates every port.

IDR = resolved levels of all pins (H -> 1, L/X -> 0) except analog-mode pins (read 0); floating reads warn once per
pin after the firmware configured it.  Cached and invalidated by PinBoard input listeners (IDR polling is one attribute
read).  ODR, BSRR (bits 0-15 set, 16-31 reset; set wins) and BRR (bits 0-15 reset) drive outputs (and choose the pull
of pull-mode inputs).  LCKR: write LCKK=1, 0, 1 with the same mask, then read: the CRL/CRH fields of the locked pins
are frozen until reset (writes ignored, one `warn`).  Pin-report hooks (chipem.pinreport) as in stm32.gpio.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, gpio_ports, rcc
from .signals import TABLE as SIGNALS

_TIM_CH = re.compile(r"(TIM\d+)_CH(\d)(N?)(?:_|$)")
LCKK = 1 << 16

# AFIO_MAPR remap fields: name -> (lsb, width, {field value: {signal: "Pxn"}})   (RM0008 9.3 remap tables; low-,
# medium- and high-density pin names; pins absent from a package are simply not routed anywhere)
REMAP: dict[str, tuple[int, int, dict[int, dict[str, str]]]] = {
    "SPI1": (0, 1, {0: {"SPI1_NSS": "PA4", "SPI1_SCK": "PA5", "SPI1_MISO": "PA6", "SPI1_MOSI": "PA7"},
                    1: {"SPI1_NSS": "PA15", "SPI1_SCK": "PB3", "SPI1_MISO": "PB4", "SPI1_MOSI": "PB5"}}),
    "I2C1": (1, 1, {0: {"I2C1_SCL": "PB6", "I2C1_SDA": "PB7"}, 1: {"I2C1_SCL": "PB8", "I2C1_SDA": "PB9"}}),
    "USART1": (2, 1, {0: {"USART1_TX": "PA9", "USART1_RX": "PA10"}, 1: {"USART1_TX": "PB6", "USART1_RX": "PB7"}}),
    "USART2": (3, 1, {0: {"USART2_CTS": "PA0", "USART2_RTS": "PA1", "USART2_TX": "PA2", "USART2_RX": "PA3",
                          "USART2_CK": "PA4"},
                      1: {"USART2_CTS": "PD3", "USART2_RTS": "PD4", "USART2_TX": "PD5", "USART2_RX": "PD6",
                          "USART2_CK": "PD7"}}),
    "TIM1": (6, 2, {0: {"TIM1_ETR": "PA12", "TIM1_CH1": "PA8", "TIM1_CH2": "PA9", "TIM1_CH3": "PA10",
                        "TIM1_CH4": "PA11", "TIM1_BKIN": "PB12", "TIM1_CH1N": "PB13", "TIM1_CH2N": "PB14",
                        "TIM1_CH3N": "PB15"},
                    1: {"TIM1_ETR": "PA12", "TIM1_CH1": "PA8", "TIM1_CH2": "PA9", "TIM1_CH3": "PA10",
                        "TIM1_CH4": "PA11", "TIM1_BKIN": "PA6", "TIM1_CH1N": "PA7", "TIM1_CH2N": "PB0",
                        "TIM1_CH3N": "PB1"},
                    3: {"TIM1_ETR": "PE7", "TIM1_CH1": "PE9", "TIM1_CH2": "PE11", "TIM1_CH3": "PE13",
                        "TIM1_CH4": "PE14", "TIM1_BKIN": "PE15", "TIM1_CH1N": "PE8", "TIM1_CH2N": "PE10",
                        "TIM1_CH3N": "PE12"}}),
    "TIM2": (8, 2, {0: {"TIM2_CH1_ETR": "PA0", "TIM2_CH2": "PA1", "TIM2_CH3": "PA2", "TIM2_CH4": "PA3"},
                    1: {"TIM2_CH1_ETR": "PA15", "TIM2_CH2": "PB3", "TIM2_CH3": "PA2", "TIM2_CH4": "PA3"},
                    2: {"TIM2_CH1_ETR": "PA0", "TIM2_CH2": "PA1", "TIM2_CH3": "PB10", "TIM2_CH4": "PB11"},
                    3: {"TIM2_CH1_ETR": "PA15", "TIM2_CH2": "PB3", "TIM2_CH3": "PB10", "TIM2_CH4": "PB11"}}),
    "TIM3": (10, 2, {0: {"TIM3_CH1": "PA6", "TIM3_CH2": "PA7", "TIM3_CH3": "PB0", "TIM3_CH4": "PB1"},
                     2: {"TIM3_CH1": "PB4", "TIM3_CH2": "PB5", "TIM3_CH3": "PB0", "TIM3_CH4": "PB1"},
                     3: {"TIM3_CH1": "PC6", "TIM3_CH2": "PC7", "TIM3_CH3": "PC8", "TIM3_CH4": "PC9"}}),
    "CAN": (13, 2, {0: {"CAN_RX": "PA11", "CAN_TX": "PA12"}, 2: {"CAN_RX": "PB8", "CAN_TX": "PB9"},
                    3: {"CAN_RX": "PD0", "CAN_TX": "PD1"}}),
}
PD01_REMAP = 1 << 15
SWJ_RELEASE = {0: (), 1: ("NJTRST",), 2: ("JTDI", "JTDO", "NJTRST"), 4: ("JTMS", "JTCK", "JTDI", "JTDO", "NJTRST")}
DEBUG_PULL = {"JTMS": "up", "JTDI": "up", "NJTRST": "up", "JTCK": "down"}
# signal -> remap field it belongs to
SIGNAL_FIELD = {sig: f for f, (_, _, vals) in REMAP.items() for v in vals.values() for sig in v}


def _block_of(sig: str) -> str:
    """Peripheral instance carrying a signal: "USART1_TX" -> USART1, "CAN_RX" -> CAN, "MCO" -> RCC."""
    if sig == "MCO":
        return "RCC"
    return sig.split("_", 1)[0]


@register("stm32f1.afio")
class AFIO(GatedBlock):
    """AFIO: MAPR (remap, SWJ_CFG), EXTICR1-4 (EXTI line -> port), EVCR (Cortex EVENTOUT pin, stored)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "AFIO"
        ov = {"MAPR": {"on_write": self._mapr_write, "on_read": self._mapr_read}}
        for k in range(1, 5):
            ov[f"EXTICR{k}"] = {"on_write": self._exticr_write, "ro_mask": 0xFFFF0000}
        r = build_regs((name, "AFIO"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.swj = 0
        self.exti: Any = None              # stm32f1.exti (sets itself here): told about EXTICR changes
        self.listeners: list = []          # cb() after a MAPR change

    USER_REGS = ("CR1", "CR2", "CR3", "CCMR1_Input", "CCMR1_Output", "CCMR2_Input", "CCMR2_Output", "CCER", "SMCR",
                 "BDTR")

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        # the pin owner depends on how the peripheral uses a signal (USART CR3.CTSE, TIM CCxS, I2C PE ...): re-evaluate
        # the pins of a block after a write to one of its configuration registers (wrapped on_write hooks; config
        # writes are rare, the data/status paths are not touched)
        blocks = {_block_of(s) for f in REMAP.values() for v in f[2].values() for s in v}
        board = getattr(chip, "pins", None)
        if board is not None:
            for p in board.pins:
                for key in ("AF", "REMAP"):
                    for s in str((p.functions or {}).get(key) or "").split("/"):
                        if s.strip():
                            blocks.add(_block_of(s.strip()))
        for name in sorted(blocks):
            blk = chip.peripherals.get(name)
            if blk is None or name in ("RCC", "AFIO"):
                continue
            for rn in self.USER_REGS:
                reg = blk.regs.get(rn)
                if reg is None or getattr(reg.on_write, "_f1_pin_hook", False):
                    continue
                reg.on_write = self._wrap(reg.on_write, name)

    def _wrap(self, orig, block: str):
        def hook(blk, reg, old, new):
            x = orig(blk, reg, old, new) if orig is not None else None
            if x is not None:
                blk._store(reg, x)
            if (x if x is not None else new) != old and self.chip is not None:
                for p in gpio_ports(self.chip):
                    p.resync_block(block)
            return x
        hook._f1_pin_hook = True
        return hook

    def reset(self) -> None:
        super().reset()
        self.swj = 0
        if self.chip is not None:
            for p in gpio_ports(self.chip):
                p.resync_all()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def remap(self, field: str) -> int:
        lsb, width, _ = REMAP[field]
        return (self.get("MAPR") >> lsb) & ((1 << width) - 1)

    def signal_pin(self, sig: str) -> str | None:
        """GPIO name the remap routes `sig` to now (None: not a remappable signal, or no pin for this value)."""
        f = SIGNAL_FIELD.get(sig)
        if f is None:
            return None
        lsb, width, vals = REMAP[f]
        v = (self.get("MAPR") >> lsb) & ((1 << width) - 1)
        if v not in vals:                    # reserved value (e.g. TIM3_REMAP = 01): no pin
            return ""
        return vals[v].get(sig, "")

    def pd01_remap(self) -> bool:
        return bool(self.get("MAPR") & PD01_REMAP)

    def released(self, fn: str) -> bool:
        """Is SWJ debug function `fn` (JTMS, JTDO ...) released to GPIO by SWJ_CFG?"""
        return fn in SWJ_RELEASE.get(self.swj, ("JTMS", "JTCK", "JTDI", "JTDO", "NJTRST"))

    def exti_port(self, line: int) -> int:
        """Port index (0 = A) selected for EXTI line 0..15."""
        reg = f"EXTICR{line // 4 + 1}"
        return (self.get(reg) >> (4 * (line % 4))) & 0xF

    def _mapr_read(self, blk, reg):
        return self.get("MAPR") & ~(7 << 24)          # SWJ_CFG is write-only

    def _mapr_write(self, blk, reg, old, new):
        swj = (new >> 24) & 7
        if swj != self.swj:
            if swj not in SWJ_RELEASE:
                self.emit("warn", block=self.name, msg=f"AFIO_MAPR.SWJ_CFG = {swj:03b} is reserved")
            elif swj:
                self.emit("info", block=self.name, msg=f"SWJ_CFG = {swj:03b}: " + ", ".join(SWJ_RELEASE[swj])
                          + " released to GPIO")
            self.swj = swj
        new &= ~(7 << 24)
        self._store(reg, new)
        if old != new:
            for f, (lsb, width, vals) in REMAP.items():
                v = (new >> lsb) & ((1 << width) - 1)
                if v != (old >> lsb) & ((1 << width) - 1):
                    if v not in vals:
                        self.emit("warn", block=self.name, msg=f"AFIO_MAPR.{f}_REMAP = {v} is reserved: no pin")
                    else:
                        self.emit("info", block=self.name, msg=f"{f} remap {v}: " + ", ".join(
                            f"{s}->{p}" for s, p in vals[v].items()))
        if self.chip is not None:
            for p in gpio_ports(self.chip):
                p.resync_all()
            for cb in list(self.listeners):
                cb()
        return new

    def _exticr_write(self, blk, reg, old, new):
        if old != new and self.exti is not None:
            k = int(reg.name[-1]) - 1
            for j in range(4):
                if ((old ^ new) >> (4 * j)) & 0xF:
                    self.exti.selection_changed(4 * k + j)
        return None


@register("stm32f1.gpio")
class GPIO(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.port = str(params.get("port") or name[-1]).upper()
        self.gate = f"GPIO{self.port}"
        ov: dict[str, dict] = {"CRL": {"on_write": self._cfg_write}, "CRH": {"on_write": self._cfg_write}}
        ov["IDR"] = {"on_read": self._idr_read, "ro_mask": 0xFFFFFFFF}
        ov["ODR"] = {"on_write": self._odr_write, "ro_mask": 0xFFFF0000}
        ov["BSRR"] = {"on_write": self._bsrr_write, "on_read": lambda b, r: 0}
        ov["BRR"] = {"on_write": self._brr_write, "on_read": lambda b, r: 0}
        ov["LCKR"] = {"on_write": self._lckr_write, "on_read": self._lckr_read}
        r = build_regs((name, f"GPIO{self.port}", "GPIOA"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.pins: list[Any] = [None] * 16
        self.odr = 0
        self._idr: int | None = None
        self._cr = 0x4444444444444444          # CRH:CRL as one 64-bit value (cached)
        self._warned: set[Any] = set()
        self._lock_seq = 0
        self._lock_val = 0
        self._locked = 0
        self._out_mask = 0
        self._an_mask = 0
        self.touched = 0
        self._state: list[tuple | None] = [None] * 16
        self._cands: list[tuple] = [()] * 16     # (signal, ...) offered by each pin (AF + REMAP columns)
        self._afio: Any = None
        self._rep: Any = None
        self.n_idr = 0
        self._rd_acc = [0] * 16
        self._rd_mark = 0
        self._rd_mask = 0
        self._w_set: dict[int, int] = {}
        self._w_fold = [0] * 16
        self.n_odr_w = 0
        self.changes = [0] * 16
        self.ever_out = 0
        self.x_read_mask = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        from ._util import afio
        self._afio = afio(chip)
        board = getattr(chip, "pins", None)
        if board is not None:
            for i in range(16):
                pin = board.gpio(self.port, i)
                self.pins[i] = pin
                if pin is None:
                    continue
                board.input_listeners[pin.id].append(self._invalidate)
                c = []
                for key in ("AF", "REMAP"):
                    for s in str(pin.functions.get(key) or "").split("/"):
                        s = s.strip()
                        if s and s not in c:
                            c.append(s)
                self._cands[i] = tuple(c)
            from ...pinreport import ensure
            self._rep = ensure(chip)
            if self._rep is not None:
                self._rep.hooked = True
        r = rcc(chip)
        if r is not None:
            r.gate_listeners.append(lambda enr, old, new: self.resync_all())
            r.listeners.append(self._clock_tree_changed)

    def reset(self) -> None:
        super().reset()
        self.odr = 0
        self._idr = None
        self._cr = (self.get("CRH") << 32) | self.get("CRL")
        self._warned.clear()
        self._lock_seq = 0
        self._locked = 0
        self._calc_masks()
        self.touched = 0
        self._state = [None] * 16
        if self.chip is not None:
            self._flush_reads()
            for i in range(16):
                self._sync_pin(i, quiet=True)

    # ------------------------------------------------------------ field helpers
    def cfg(self, i: int) -> tuple[int, int]:
        """(MODE, CNF) of pin i."""
        v = (self._cr >> (4 * i)) & 0xF
        return v & 3, v >> 2

    def _calc_masks(self) -> None:
        out = an = 0
        for i in range(16):
            mode, cnf = self.cfg(i)
            if mode and cnf < 2:
                out |= 1 << i
            if not mode and not cnf:
                an |= 1 << i
        self._out_mask, self._an_mask = out, an

    @property
    def output_mask(self) -> int:
        return self._out_mask

    @property
    def input_mask(self) -> int:
        return sum(1 << i for i in range(16) if self.cfg(i)[0] == 0 and self.cfg(i)[1] != 0)

    def _warn_once(self, key: Any, msg: str, pin: Any = None) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg, **({"pin": pin.id} if pin is not None else {}))

    # ------------------------------------------------------------ routing
    def _routed(self, pin: Any, sig: str) -> bool:
        """Does the AFIO remap route `sig` to this pin now?  Signals without a remap field always are."""
        a = self._afio
        if a is None:
            return sig in str(pin.functions.get("AF") or "").split("/")
        p = a.signal_pin(sig)
        if p is None:
            return sig in str(pin.functions.get("AF") or "").split("/")
        return p == f"P{self.port}{pin.bit if pin.bit is not None else ''}"

    def _clocked(self, sig: str) -> bool:
        r = rcc(self.chip) if self.chip is not None else None
        blk = _block_of(sig)
        if blk == "RCC":
            return r is not None and ((r.get("CFGR") >> 24) & 4) != 0       # MCO selects a clock
        if r is None:
            return True
        if blk not in r.GATES:
            return (self.chip.peripherals.get(blk) is not None) if self.chip is not None else False
        return r.clock_enabled(blk)

    def _periph(self, name: str) -> Any:
        return (getattr(self.chip, "peripherals", None) or {}).get(name)

    def _tim_released(self, owner: str) -> bool:
        m = _TIM_CH.match(owner)
        if m is None:
            return False
        f = getattr(self._periph(m[1]), "signal_driven", None)
        if f is None:
            return False
        d = f(int(m[2]), bool(m[3]))
        return d is not None and not d

    def _signal_state(self, owner: str) -> tuple[str, str | None] | None:
        f = getattr(self._periph(_block_of(owner)), "signal_state", None)
        return f(owner) if f is not None else None

    def _reg(self, blk: Any, name: str) -> int:
        return blk.get(name) if blk is not None and name in blk.regs else 0

    def _score(self, sig: str, inputs: bool) -> int:
        """How much the peripheral uses `sig` now: 2 = enabled for this signal, 1 = block clocked (the signal may be
        used once the firmware enables it), 0 = not used (wrong direction, function disabled or clock off)."""
        if not self._clocked(sig):
            return 0
        blk = self._periph(_block_of(sig))
        part = sig.split("_", 1)[1] if "_" in sig else sig
        m = _TIM_CH.match(sig)
        etr_used = False
        if sig.endswith("_ETR"):
            smcr = self._reg(blk, "SMCR")
            etr_used = bool(smcr & (1 << 14)) or bool((smcr & 7) and (smcr >> 4) & 7 == 7)   # ECE, or TRGI = ETRF
        if m is not None:
            if blk is None:
                return 0
            n = int(m[2])
            reg = next((x for x in (f"CCMR{(n + 1) // 2}_Input", f"CCMR{(n + 1) // 2}_Output") if x in blk.regs), None)
            ccmr = blk.get(reg) if reg is not None else 0
            own, other = (ccmr >> (8 * ((n - 1) & 1))) & 3, (ccmr >> (8 * (n & 1))) & 3
            # TIx (the pin of channel n) feeds ICn when CCnS = 01 and the partner channel's ICm when CCmS = 10;
            # a channel in input mode does not drive its pin
            capture = not m[3] and (own == 1 or other == 2)
            if not inputs and own:
                return 0
            if inputs:
                return 2 if capture or etr_used else 0
            if capture:
                return 0
            return 1 if self._tim_released(sig) else 2
        if sig.endswith("_ETR"):
            return 2 if inputs and etr_used else 0
        if sig.endswith("_BKIN"):
            return 2 if inputs and self._reg(blk, "BDTR") & (1 << 12) else 0
        if sig.startswith("USART"):
            cr1, cr2, cr3 = self._reg(blk, "CR1"), self._reg(blk, "CR2"), self._reg(blk, "CR3")
            if part == "TX":
                return 0 if inputs else (2 if cr1 & 0x2008 == 0x2008 else 1)
            if part == "RX":
                return (2 if cr1 & 0x2004 == 0x2004 else 1) if inputs else 0
            if part == "CTS":
                return 2 if inputs and cr3 & (1 << 9) else 0
            if part == "RTS":
                return 2 if not inputs and cr3 & (1 << 8) else 0
            if part == "CK":
                return 2 if not inputs and cr2 & (1 << 11) else 0
        if sig.startswith("I2C"):
            cr1 = self._reg(blk, "CR1")
            if part == "SMBA":
                return 2 if cr1 & 1 and cr1 & 2 else 0
            return 0 if inputs else (2 if cr1 & 1 else 1)
        if sig == "MCO":
            return 0 if inputs else 2
        live = self._signal_state(sig)
        if live is not None:                          # SPI: live direction from the block (master/slave, SPE)
            return 2 if (live[0] == "in") == inputs else 0
        role = SIGNALS.role(sig)
        if inputs:
            return 1 if role.name in ("in", "od") else 0
        return 1 if role.name in ("out_h", "out_l", "od") else 0

    def _af_owner(self, i: int, pin: Any, inputs: bool) -> str | None:
        """Routed peripheral signal owning pin i: in AF output mode the output with the highest usage score (Table 5
        order on a tie; with no clocked candidate the first routed output, shown released); in input mode the input
        with the highest score >= 1, else None (GPIO)."""
        best, best_s, first_out = None, 0, None
        for sig in self._cands[i]:
            if not self._routed(pin, sig):
                continue
            sc = self._score(sig, inputs)
            if not inputs and first_out is None and SIGNALS.role(sig).name in ("out_h", "out_l", "od"):
                first_out = sig
            if sc > best_s:
                best, best_s = sig, sc
        if best is None and not inputs:
            return first_out
        return best

    def _system_owner(self, i: int, pin: Any) -> tuple | None:
        """A system function owning the pin whatever CRL/CRH say (SWJ, oscillator, USB transceiver)."""
        f = pin.functions
        dbg = f.get("debug")
        if dbg:
            a = self._afio
            fn0 = dbg.split("/")[0]
            if a is None or not a.released(fn0):
                d, drv = SIGNALS.pin_state(dbg)
                return dbg, d, drv, DEBUG_PULL.get(fn0)
        osc = f.get("analog")
        if osc and osc.startswith("OSC"):
            a = self._afio
            r = rcc(self.chip) if self.chip is not None else None
            hse = r is not None and bool(r.get("CR") & (1 << 16))
            if a is None or not a.pd01_remap() or hse:
                return osc, "analog", None, None
        usb = f.get("additional")
        if usb in ("USBDM", "USBDP"):
            u = self._periph("USB")
            on = getattr(u, "transceiver_on", None)
            if on is not None and on():
                return usb, "in", None, None
        return None

    def pin_config(self, i: int) -> tuple[str, str, str | None, str | None]:
        """(owner, direction, drive, pull) of pin i from the current registers."""
        pin = self.pins[i]
        sysown = self._system_owner(i, pin) if pin is not None else None
        if sysown is not None:
            return sysown
        mode, cnf = self.cfg(i)
        if mode == 0:
            if cnf == 0:
                fn = (pin.functions.get("ADC") or pin.functions.get("analog")) if pin is not None else None
                return (fn or "analog"), "analog", None, None
            pull = None
            if cnf == 2:
                pull = "up" if self.odr >> i & 1 else "down"
            elif cnf == 3:
                self._warn_once(("cnf3", i), f"{pin.id if pin else i}: CNF = 11 in input mode is reserved "
                                f"(treated as floating input)", pin)
            owner = self._af_owner(i, pin, True) if pin is not None else None
            return (owner or "GPIO"), "in", None, pull
        if cnf < 2:
            high = bool(self.odr >> i & 1)
            if cnf == 1 and high:
                return "GPIO", "in", None, None
            return "GPIO", "out", ("H" if high else "L"), None
        owner = self._af_owner(i, pin, False) if pin is not None else None
        if owner is None:
            if pin is not None:
                self._warn_once(("af", i), f"{pin.id}: alternate function output mode but no peripheral output is "
                                f"routed to this pin (check AFIO_MAPR)", pin)
            return "disabled", "none", None, None
        d, drv = SIGNALS.pin_state(owner)
        if d == "out" and owner.startswith("TIM") and self._tim_released(owner):
            d, drv = "in", None
        live = self._signal_state(owner)
        if live is not None:
            d, drv = live
        return owner, d, drv, None

    def resync_signal(self, sig: str) -> None:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return
        for i, pin in enumerate(self.pins):
            if pin is not None and (board.signal_matches(pin.owner, sig) or sig in self._cands[i]
                                    or any(board.signal_matches(c, sig) for c in self._cands[i])):
                self._sync_pin(i, quiet=True)

    def resync_block(self, block: str) -> None:
        """Peripheral `block` changed its configuration: re-evaluate the pins offering one of its signals."""
        if self.chip is None:
            return
        for i, c in enumerate(self._cands):
            if self.pins[i] is not None and any(_block_of(s) == block for s in c):
                self._sync_pin(i, quiet=True)

    def resync_all(self) -> None:
        if self.chip is None:
            return
        for i in range(16):
            if self.pins[i] is not None:
                self._sync_pin(i, quiet=True)

    def _clock_tree_changed(self) -> None:
        for i, pin in enumerate(self.pins):
            if pin is not None and str(pin.functions.get("analog") or "").startswith("OSC"):
                self._sync_pin(i, quiet=True)

    def _sync_pin(self, i: int, *, quiet: bool = False) -> None:
        pin = self.pins[i]
        board = self.chip.pins if self.chip is not None else None
        if pin is None or board is None:
            return
        st = self.pin_config(i)
        if st == self._state[i]:
            return
        prev = self._state[i]
        self._state[i] = st
        owner, direction, drive, pull = st
        board.mcu_update(pin, owner=owner, direction=direction, drive=drive, pull=pull)
        self._idr = None
        if not quiet and (prev is None or prev[0] != owner or prev[1] != direction or prev[3] != pull):
            if owner == "GPIO":
                what = "output" if direction == "out" else ("open-drain output (released)" if self.cfg(i)[0]
                                                           else "input")
                msg = f"{pin.id} → GPIO {what}" + (f" ({drive})" if drive else "")
            else:
                msg = f"{pin.id} → {owner}"
            if pull:
                msg += f" (pull-{pull})"
            self.emit("info", block=self.name, pin=pin.id, msg=msg)

    # ------------------------------------------------------------ fast paths
    def read(self, offset: int, size: int) -> int:
        if offset == 0x08 and size == 4 and not self.log_all:
            self.n_idr += 1
            v = self._idr
            return v if v is not None else self._idr_compute()
        return MMIOBlock.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if offset == 0x10 and size == 4 and not self.log_all:
            self._count_write(value)
            self._set_odr((self.odr & ~(value >> 16)) | (value & 0xFFFF))
            return
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------ pin discovery (chipem.pinreport)
    def _count_write(self, value: int) -> None:
        d = self._w_set
        c = d.get(value)
        d[value] = 1 if c is None else c + 1
        if c is not None:
            return
        if len(d) > 256:
            fold = self._w_fold
            for v, n in d.items():
                m = (v | v >> 16) & 0xFFFF
                while m:
                    low = m & -m
                    fold[low.bit_length() - 1] += n
                    m ^= low
            d.clear()
        rep = self._rep
        if rep is None:
            return
        pc = rep.pc()
        m = (value | value >> 16) & 0xFFFF
        while m:
            low = m & -m
            pin = self.pins[low.bit_length() - 1]
            if pin is not None:
                rep.touch_data(pin, pc)
            m ^= low

    def _flush_reads(self) -> None:
        d = self.n_idr - self._rd_mark
        if d:
            m = self._rd_mask
            acc = self._rd_acc
            while m:
                low = m & -m
                acc[low.bit_length() - 1] += d
                m ^= low
        self._rd_mark = self.n_idr
        self._rd_mask = self.input_mask & self.touched & sum(1 << i for i, x in enumerate(self.pins) if x is not None)

    def pin_stats(self) -> dict[str, dict]:
        self._flush_reads()
        out: dict[str, dict] = {}
        for i, pin in enumerate(self.pins):
            if pin is None:
                continue
            bit = 1 << i
            w = self._w_fold[i] + sum(c for v, c in self._w_set.items() if (v | v >> 16) & bit)
            if self.ever_out & bit:
                w += self.n_odr_w
            reads = self._rd_acc[i]
            fl = bool(self.x_read_mask & bit)
            if w or reads or self.changes[i] or fl:
                out[pin.id] = {"reads": reads, "writes": w, "changes": self.changes[i], "floating": fl}
        return out

    def _invalidate(self, pin: Any = None) -> None:
        self._idr = None

    # ------------------------------------------------------------ output
    def _set_odr(self, new: int) -> None:
        new &= 0xFFFF
        old = self.odr
        self.odr = new
        o = self.regs["ODR"].boff
        self.mem[o:o + 4] = new.to_bytes(4, "little")
        diff = old ^ new
        ch = diff & self._out_mask
        pulls = diff & ~self._out_mask & ~self._an_mask
        if ch:
            rep = self._rep
            pc = rep.pc() if rep is not None else None
            while ch:
                low = ch & -ch
                i = low.bit_length() - 1
                ch ^= low
                pin = self.pins[i]
                if pin is not None:
                    self.changes[i] += 1
                    if rep is not None:
                        rep.touch_data(pin, pc)
                    self._sync_pin(i, quiet=True)
        while pulls:                         # input pull-up/down follows ODR
            low = pulls & -pulls
            i = low.bit_length() - 1
            pulls ^= low
            if self.pins[i] is not None and self.cfg(i) == (0, 2):
                self._sync_pin(i, quiet=True)

    def _odr_write(self, blk, reg, old, new):
        self.n_odr_w += 1
        if self.n_odr_w == 1 and self._rep is not None:
            self._count_write(self._out_mask)
        self._set_odr(new)
        return None

    def _bsrr_write(self, blk, reg, old, new):
        self._count_write(new)
        self._set_odr((self.odr & ~(new >> 16)) | (new & 0xFFFF))
        return 0

    def _brr_write(self, blk, reg, old, new):
        self._count_write((new & 0xFFFF) << 16)
        self._set_odr(self.odr & ~new)
        return 0

    # ------------------------------------------------------------ configuration
    def _describe(self, i: int, before: int, after: int) -> list[str]:
        pin = self.pins[i]
        m0, c0 = before & 3, before >> 2
        m1, c1 = after & 3, after >> 2
        parts = []
        if m1 == 0:
            what = ("analog" + (f" ({pin.functions.get('ADC')})" if pin.functions.get("ADC") else ""),
                    "input floating", "input pull-up/down", "input (reserved CNF)")[c1]
        else:
            what = ("output push-pull", "output open-drain", "alternate function push-pull",
                    "alternate function open-drain")[c1] + f" {('', '10', '2', '50')[m1]} MHz"
            if c1 >= 2:
                routed = [s for s in self._cands[i] if self._routed(pin, s)]
                if routed:
                    what += f" ({'/'.join(routed)})"
        if (m0, c0) != (m1, c1):
            parts.append(what)
        return parts

    def _cfg_write(self, blk, reg, old, new):
        first = 0 if reg.name == "CRL" else 8
        if self._locked:
            lm = 0
            for k in range(8):
                if self._locked >> (first + k) & 1:
                    lm |= 0xF << (4 * k)
            if (old ^ new) & lm:
                new = (new & ~lm) | (old & lm)
                self._warn_once("lock", f"{self.name}_{reg.name}: write to a locked pin (LCKR) ignored")
                self._store(reg, new)
        if old == new:
            return None
        self._store(reg, new)
        self._cr = (self.get("CRH") << 32) | self.get("CRL")
        self._flush_reads()
        self._calc_masks()
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        for k in range(8):
            o4 = (old >> (4 * k)) & 0xF
            n4 = (new >> (4 * k)) & 0xF
            if o4 == n4:
                continue
            i = first + k
            self.touched |= 1 << i
            if n4 & 3 and (n4 >> 2) < 2:
                self.ever_out |= 1 << i
            pin = self.pins[i]
            if pin is None:
                continue
            if rep is not None:
                parts = self._describe(i, o4, n4)
                if parts:
                    rep.config(pin, ", ".join(parts), pc)
                else:
                    rep.touch_cfg(pin, pc)
            self._sync_pin(i)
        return None

    def _lckr_write(self, blk, reg, old, new):
        if self._locked:
            return (self._locked & 0xFFFF) | LCKK
        mask = new & 0xFFFF
        k = new & LCKK
        seq = self._lock_seq
        if seq == 0 and k:
            self._lock_seq, self._lock_val = 1, mask
        elif seq == 1 and not k and mask == self._lock_val:
            self._lock_seq = 2
        elif seq == 2 and k and mask == self._lock_val:
            self._lock_seq = 3
        else:
            self._lock_seq = 1 if k else 0
            self._lock_val = mask
        return mask

    def _lckr_read(self, blk, reg):
        if self._lock_seq == 3:
            self._lock_seq = 0
            self._locked = self._lock_val
            self.emit("info", block=self.name, msg=f"{self.name}: pins 0x{self._locked:04x} locked (LCKR)")
        if self._locked:
            return self._locked | LCKK
        return None

    # ------------------------------------------------------------ input
    def _idr_read(self, blk, reg):
        self.n_idr += 1
        v = self._idr
        return v if v is not None else self._idr_compute()

    def _idr_compute(self) -> int:
        v = 0
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        an = self._an_mask
        for i, pin in enumerate(self.pins):
            if pin is None or an >> i & 1:
                continue
            lvl = pin.level()
            inp = self.cfg(i)[0] == 0
            if rep is not None and inp and self.touched >> i & 1:
                rep.touch_data(pin, pc)
            if lvl == "H":
                v |= 1 << i
            elif lvl == "X" and inp and self.touched >> i & 1:
                self.x_read_mask |= 1 << i
                self._warn_once(("x", i), f"reading floating pin {pin.id} (no pull, nothing connected): reads 0",
                                pin)
        self._idr = v
        o = self.regs["IDR"].boff
        self.mem[o:o + 4] = v.to_bytes(4, "little")
        return v
