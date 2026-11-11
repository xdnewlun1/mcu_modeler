"""STM32F1 GPIO ports and AFIO (RM0008 ch. 9: CRL/CRH/IDR/ODR/BSRR/BRR/LCKR; AFIO EVCR/MAPR/EXTICR1-4/MAPR2).

Verified against RM0008 Rev 21 ch. 9 (9.1 functional description, Tables 20/21, 9.2 GPIO registers, 9.3 remap tables
34-58, 9.4 AFIO registers) and 31.4 (SWJ debug pins).  This module is the only F1 code that turns pin registers into
PinBoard state (`mcu_update`) and pin levels into IDR.  There is no per-pin alternate-function number on the F1: a pin
configured as "alternate function output" is driven by the peripheral output that the AFIO remap routes to it, and
every peripheral input reads its pin whatever the mode (9.1.4).

Per pin, 4 bits in CRL (pins 0-7) / CRH (8-15), reset 0x44444444 = floating inputs (9.1.1, 9.2.1/9.2.2, Table 20):
MODE[1:0] (00 input, 01/10/11 output 10/2/50 MHz, Table 21; the speed only changes edge rates on silicon) and CNF:
* input  CNF 00 analog: Schmitt trigger off, IDR bit reads 0, no pull (9.1.10); owner = the pin's ADC channel
         ("ADC12_IN0"), else "analog".  CNF 01 floating, CNF 10 pull-up (ODR bit = 1) / pull-down (ODR bit = 0)
         (Table 20), CNF 11 reserved (treated as floating, one `warn`).  Owner "GPIO", or the peripheral input routed
         here when its block is clocked and uses it: USARTx_RX/CTS, CAN_RX, SPIx_* (stm32.spi `signal_state`),
         TIMx_CHn in input-capture mode, TIMx_ETR, TIMx_BKIN (BDTR.BKE).
* output CNF 00 push-pull / 01 open-drain (1 = Hi-Z, 9.1.8), pulls off; owner "GPIO", drive = ODR bit.
         CNF 10 AF push-pull / 11 AF open-drain: owner = the routed peripheral output whose block is clocked (the first
         in the data sheet's order when several are); direction/idle drive from the F1 signal table (`signals.py`),
         overridden live (stm32.tim `signal_driven`, stm32.spi `signal_state`).  No routed output: owner "disabled"
         (9.1.4: "its output is not specified"), one `warn`.
Pins that a system function owns whatever CRL/CRH say:
* SWJ debug pins (Table 36/37, 31.4): PA13 JTMS/SWDIO, PA14 JTCK/SWCLK, PA15 JTDI, PB3 JTDO, PB4 NJTRST until
  AFIO_MAPR.SWJ_CFG releases them (000 all; 001 NJTRST free; 010 JTAG-DP off, SW-DP on: PA15/PB3/PB4 free; 100 all
  free; other values: no effect, the previous configuration stays).  Internal pulls JTMS/JTDI/NJTRST up, JTCK down
  (31.4.3); JTDO is tri-stated without a debugger (31.4.4).  DBGMCU_CR.TRACE_IOEN (31.16.3) takes PB3 as TRACESWO in
  asynchronous mode even when SWJ_CFG released it (Table 37 note 1), and TRACECK/TRACED0-3 (PE2-PE6) in synchronous
  mode (1/2/4 data pins).  SWJ_CFG is write-only: it reads 0 here (RM: undefined).
* OSC_IN/OSC_OUT (PD0/PD1 on the 36/48/64-pin parts, 9.3.2): oscillator pins unless AFIO_MAPR.PD01_REMAP = 1 and the
  HSE is off.  Their EXTI function is not remapped (stm32f1.exti ignores them).
* USBDM/USBDP (PA11/PA12): the USB transceiver once the USB block is clocked and powered (stm32f1.usb).
* WKUP (PA0): input with pull-down while PWR_CSR.EWUP = 1 (RM0008 5.4.2).
Remap: AFIO_MAPR (and MAPR2 on XL density) fields move signals between pins (`REMAP` tables below, 9.3.3-9.3.12);
a MAPR write re-evaluates every port.  Pins a package does not bond are simply not routed anywhere.
Product lines (`params.density` of stm32f1.afio: low / medium / high / xl, default low; `LINES`, `register_line()`):
the MAPR remap table and writable bits, and whether MAPR2 exists (XL only, 9.4.7; reserved, reads 0, elsewhere).  The
connectivity line registers its own table (ETH/CAN2/SPI3/MII_RMII_SEL/TIM2ITR1/PTP_PPS fields) with
`register_line("cl", ...)` from its module.  Internal remaps (TIM5CH4_IREMAP: LSI on TIM5_CH4; ADCx_ETRGINJ/REG_REMAP:
TIM8 triggers instead of EXTI15/EXTI11, Tables 38-42) are stored and read with `AFIO.internal_remap(name)`.

IDR (9.2.3) = resolved levels of the pins (H -> 1, L/X -> 0) except analog-mode pins (read 0); floating reads warn
once per pin after the firmware configured it.  Cached and invalidated by PinBoard input listeners.  ODR, BSRR (bits
0-15 set, 16-31 reset; set wins, 9.2.5) and BRR (bits 0-15 reset, 9.2.6) drive outputs and choose the pull of
pull-mode inputs.  LCKR (9.2.7): write LCKK=1, 0, 1 with the same LCK[15:0], read (LCKK reads 0), read (LCKK reads 1:
locked); any other access in between aborts the sequence; locked CRL/CRH fields are frozen until reset (writes ignored,
one `warn`) and LCK[15:0] can no longer change.  Not modelled: the 32-bit-only access rule (9.2: half-word/byte
accesses are not allowed; they work here), EVCR's EVENTOUT pulse on the selected pin (stored only), output speed.
Pin-report hooks (chipem.pinreport) as in stm32.gpio.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import MMIOBlock, pure_read
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, gpio_ports, rcc
from .signals import TABLE as SIGNALS

_TIM_CH = re.compile(r"(TIM\d+)_CH(\d)(N?)(?:_|$)")
LCKK = 1 << 16

# AFIO_MAPR remap fields: name -> (lsb, width, {field value: {signal: "Pxn"}})   (RM0008 9.4.2 bit definitions and
# the remap tables 34, 43-46, 52-56; low-, medium-, high- and XL-density pin names).  A field value missing from the
# dict is "not used" (no pin); an empty dict means the signal is routed internally (no pin).
REMAP: dict[str, tuple[int, int, dict[int, dict[str, str]]]] = {
    "SPI1": (0, 1, {0: {"SPI1_NSS": "PA4", "SPI1_SCK": "PA5", "SPI1_MISO": "PA6", "SPI1_MOSI": "PA7"},
                    1: {"SPI1_NSS": "PA15", "SPI1_SCK": "PB3", "SPI1_MISO": "PB4", "SPI1_MOSI": "PB5"}}),
    "I2C1": (1, 1, {0: {"I2C1_SCL": "PB6", "I2C1_SDA": "PB7"}, 1: {"I2C1_SCL": "PB8", "I2C1_SDA": "PB9"}}),
    "USART1": (2, 1, {0: {"USART1_TX": "PA9", "USART1_RX": "PA10"}, 1: {"USART1_TX": "PB6", "USART1_RX": "PB7"}}),
    "USART2": (3, 1, {0: {"USART2_CTS": "PA0", "USART2_RTS": "PA1", "USART2_TX": "PA2", "USART2_RX": "PA3",
                          "USART2_CK": "PA4"},
                      1: {"USART2_CTS": "PD3", "USART2_RTS": "PD4", "USART2_TX": "PD5", "USART2_RX": "PD6",
                          "USART2_CK": "PD7"}}),
    "USART3": (4, 2, {0: {"USART3_TX": "PB10", "USART3_RX": "PB11", "USART3_CK": "PB12", "USART3_CTS": "PB13",
                          "USART3_RTS": "PB14"},
                      1: {"USART3_TX": "PC10", "USART3_RX": "PC11", "USART3_CK": "PC12", "USART3_CTS": "PB13",
                          "USART3_RTS": "PB14"},
                      3: {"USART3_TX": "PD8", "USART3_RX": "PD9", "USART3_CK": "PD10", "USART3_CTS": "PD11",
                          "USART3_RTS": "PD12"}}),
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
    "TIM4": (12, 1, {0: {"TIM4_CH1": "PB6", "TIM4_CH2": "PB7", "TIM4_CH3": "PB8", "TIM4_CH4": "PB9"},
                     1: {"TIM4_CH1": "PD12", "TIM4_CH2": "PD13", "TIM4_CH3": "PD14", "TIM4_CH4": "PD15"}}),
    "CAN": (13, 2, {0: {"CAN_RX": "PA11", "CAN_TX": "PA12"}, 2: {"CAN_RX": "PB8", "CAN_TX": "PB9"},
                    3: {"CAN_RX": "PD0", "CAN_TX": "PD1"}}),
    "TIM5CH4_I": (16, 1, {0: {"TIM5_CH4": "PA3"}, 1: {}}),        # 1: LSI -> TIM5_CH4 internally (Table 42)
}
# AFIO_MAPR2 (XL density only, 9.4.7, Tables 47-51)
REMAP2_XL: dict[str, tuple[int, int, dict[int, dict[str, str]]]] = {
    "TIM9": (5, 1, {0: {"TIM9_CH1": "PA2", "TIM9_CH2": "PA3"}, 1: {"TIM9_CH1": "PE5", "TIM9_CH2": "PE6"}}),
    "TIM10": (6, 1, {0: {"TIM10_CH1": "PB8"}, 1: {"TIM10_CH1": "PF6"}}),
    "TIM11": (7, 1, {0: {"TIM11_CH1": "PB9"}, 1: {"TIM11_CH1": "PF7"}}),
    "TIM13": (8, 1, {0: {"TIM13_CH1": "PA6"}, 1: {"TIM13_CH1": "PF8"}}),
    "TIM14": (9, 1, {0: {"TIM14_CH1": "PA7"}, 1: {"TIM14_CH1": "PF9"}}),
    "FSMC_NADV": (10, 1, {0: {"FSMC_NADV": "PB7"}, 1: {}}),      # 1: NADV not connected, the pin is free
}
# internal remaps (no pin): name -> MAPR bit (9.4.2; high/XL density)
INTERNAL_REMAP = {"TIM5CH4_IREMAP": 16, "ADC1_ETRGINJ_REMAP": 17, "ADC1_ETRGREG_REMAP": 18,
                  "ADC2_ETRGINJ_REMAP": 19, "ADC2_ETRGREG_REMAP": 20}
MAPR_RW = 0x001FFFFF | (7 << 24)          # low/medium/high/XL bit map (9.4.2): fields 0-20, SWJ_CFG 24-26

# product line -> remap tables and writable bits (see register_line)
LINES: dict[str, dict[str, Any]] = {}


def register_line(name: str, remap: dict, mapr_rw: int, remap2: dict | None = None, mapr2_rw: int = 0) -> None:
    """Declare the AFIO layout of a product line: `remap` (MAPR fields as in REMAP), `mapr_rw` (writable MAPR bits,
    SWJ_CFG included), optional MAPR2 fields `remap2` with their writable bits `mapr2_rw` (0: MAPR2 reserved)."""
    LINES[name] = {"remap": remap, "mapr_rw": mapr_rw, "remap2": remap2 or {}, "mapr2_rw": mapr2_rw}


for _d in ("low", "medium", "high"):
    register_line(_d, REMAP, MAPR_RW)
register_line("xl", REMAP, MAPR_RW, REMAP2_XL, 0x7E0)

PD01_REMAP = 1 << 15
SWJ_RELEASE = {0: (), 1: ("NJTRST",), 2: ("JTDI", "JTDO", "NJTRST"), 4: ("JTMS", "JTCK", "JTDI", "JTDO", "NJTRST")}
DEBUG_PULL = {"JTMS": "up", "JTDI": "up", "NJTRST": "up", "JTCK": "down"}
TRACE_SYNC_PINS = {"TRACECK": 1, "TRACED0": 1, "TRACED1": 2, "TRACED2": 3, "TRACED3": 3}   # min TRACE_MODE
# signal -> remap field it belongs to (low/medium/high density MAPR table; per-instance tables in AFIO)
SIGNAL_FIELD = {sig: f for f, (_, _, vals) in REMAP.items() for v in vals.values() for sig in v}


def _set_ro(reg: Any, ro: int) -> None:
    """Replace the read-only mask of `reg` (reserved bits read 0)."""
    reset, _, w1c = reg._raw
    reg._raw = (reset & ~ro, ro, w1c)
    reg.set_unit_bits(reg.unit_bits)


def _block_of(sig: str) -> str:
    """Peripheral instance carrying a signal: "USART1_TX" -> USART1, "CAN_RX" -> CAN, "MCO" -> RCC."""
    if sig == "MCO":
        return "RCC"
    return sig.split("_", 1)[0]


@register("stm32f1.afio")
class AFIO(GatedBlock):
    """AFIO: MAPR (remap, SWJ_CFG), MAPR2 (XL), EXTICR1-4 (EXTI line -> port), EVCR (Cortex EVENTOUT pin, stored)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "AFIO"
        self.density = str(params.get("density") or params.get("line") or "low").lower()
        if self.density not in LINES:
            raise ValueError(f"stm32f1.afio: unknown density/line {self.density!r} (known: {sorted(LINES)})")
        line = LINES[self.density]
        self.remap_tables: list[tuple[str, dict]] = [("MAPR", line["remap"])]
        if line["mapr2_rw"]:
            self.remap_tables.append(("MAPR2", line["remap2"]))
        ov = {"MAPR": {"on_write": self._mapr_write, "on_read": self._mapr_read}}
        if line["mapr2_rw"]:
            ov["MAPR2"] = {"on_write": self._mapr_write}
        for k in range(1, 5):
            ov[f"EXTICR{k}"] = {"on_write": self._exticr_write, "ro_mask": 0xFFFF0000}
        r = build_regs((name, "AFIO"), size, ov, params.get("regs_file"))
        for reg in r:
            if reg.name == "MAPR":
                _set_ro(reg, ~line["mapr_rw"] & 0xFFFFFFFF)
            elif reg.name == "MAPR2":
                _set_ro(reg, ~line["mapr2_rw"] & 0xFFFFFFFF)
        super().__init__(name, base, size, r, log=log, params=params)
        self._sig_field = {sig: (rn, f) for rn, tab in self.remap_tables for f, (_, _, vals) in tab.items()
                           for v in vals.values() for sig in v}
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
        blocks = {_block_of(s) for _, tab in self.remap_tables for f in tab.values() for v in f[2].values() for s in v}
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

    def _field(self, regname: str, field: str) -> int:
        tab = dict(self.remap_tables)[regname]
        lsb, width, _ = tab[field]
        return (self.get(regname) >> lsb) & ((1 << width) - 1)

    def remap(self, field: str) -> int:
        """Current value of remap field `field` (MAPR, or MAPR2 on XL density)."""
        for rn, tab in self.remap_tables:
            if field in tab:
                return self._field(rn, field)
        raise KeyError(field)

    def internal_remap(self, name: str) -> bool:
        """Internal remap bit (INTERNAL_REMAP: TIM5CH4_IREMAP, ADCx_ETRGINJ_REMAP / ADCx_ETRGREG_REMAP)."""
        return bool(self.get("MAPR") >> INTERNAL_REMAP[name] & 1)

    def signal_pin(self, sig: str) -> str | None:
        """GPIO name the remap routes `sig` to now (None: not a remappable signal; "": no pin for this value)."""
        rf = self._sig_field.get(sig)
        if rf is None:
            return None
        rn, f = rf
        lsb, width, vals = dict(self.remap_tables)[rn][f]
        v = (self.get(rn) >> lsb) & ((1 << width) - 1)
        if v not in vals:                    # "not used" value (e.g. TIM3_REMAP = 01): no pin
            return ""
        return vals[v].get(sig, "")

    def pd01_remap(self) -> bool:
        return bool(self.get("MAPR") & PD01_REMAP)

    def released(self, fn: str) -> bool:
        """Is SWJ debug function `fn` (JTMS, JTDO ...) released to GPIO by SWJ_CFG?"""
        return fn in SWJ_RELEASE.get(self.swj, ())

    def exti_port(self, line: int) -> int:
        """Port index (0 = A) selected for EXTI line 0..15."""
        reg = f"EXTICR{line // 4 + 1}"
        return (self.get(reg) >> (4 * (line % 4))) & 0xF

    @pure_read
    def _mapr_read(self, blk, reg):
        return self.get("MAPR") & ~(7 << 24)          # SWJ_CFG is write-only (reads undefined; 0 here)

    def _mapr_write(self, blk, reg, old, new):
        if reg.name == "MAPR":
            swj = (new >> 24) & 7
            if swj in SWJ_RELEASE and swj != self.swj:         # other values: no effect (9.4.2)
                if swj:
                    self.emit("info", block=self.name, msg=f"SWJ_CFG = {swj:03b}: " + ", ".join(SWJ_RELEASE[swj])
                              + " released to GPIO")
                self.swj = swj
            new &= ~(7 << 24)
        self._store(reg, new)
        if old != new:
            for f, (lsb, width, vals) in dict(self.remap_tables)[reg.name].items():
                v = (new >> lsb) & ((1 << width) - 1)
                if v != (old >> lsb) & ((1 << width) - 1):
                    if v not in vals:
                        self.emit("warn", block=self.name, msg=f"AFIO_{reg.name}.{f}_REMAP = {v} is not used: no pin")
                    else:
                        self.emit("info", block=self.name, msg=f"{f} remap {v}: " + (", ".join(
                            f"{s}->{p}" for s, p in vals[v].items()) or "internal / not connected"))
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
        ov["IDR"] = {"on_read": self._idr_read, "ro_mask": 0xFFFFFFFF, "on_peek": self._idr_peek}
        ov["ODR"] = {"on_write": self._odr_write, "ro_mask": 0xFFFF0000}
        ov["BSRR"] = {"on_write": self._bsrr_write, "on_read": pure_read(lambda b, r: 0)}
        ov["BRR"] = {"on_write": self._brr_write, "on_read": pure_read(lambda b, r: 0)}
        ov["LCKR"] = {"on_write": self._lckr_write, "on_read": self._lckr_read, "on_peek": self._lckr_peek}
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
        self._pwr_blk: Any = None
        self._dbg_blk: Any = None
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
            sel = getattr(r, "mco_enabled", None)                          # connectivity line: MCO[3:0] (8.3.2)
            return r is not None and (sel() if sel is not None else ((r.get("CFGR") >> 24) & 4) != 0)
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
            if live[0] == "none":                     # signal not in use (stm32f1.eth: the other of MII / RMII)
                return 0
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
            trace = self._trace_mode()
            if fn0 == "JTDO" and trace == 0:                   # asynchronous trace: PB3 = TRACESWO (Table 37 note 1)
                return "TRACESWO", "out", "H", None
            if fn0 in TRACE_SYNC_PINS and trace is not None and trace >= TRACE_SYNC_PINS[fn0]:
                return fn0, "out", "L", None
            if fn0 not in TRACE_SYNC_PINS and (a is None or not a.released(fn0)):
                d, drv = SIGNALS.pin_state(dbg)
                return dbg, d, drv, DEBUG_PULL.get(fn0)
        if "WKUP" in str(f.get("additional") or "").split("/"):
            p = self._pwr()
            if p is not None and p.get("CSR") & (1 << 8):      # PWR_CSR.EWUP: WKUP forced input pull-down (5.4.2)
                return "WKUP", "in", None, "down"
        osc = f.get("analog")
        if osc and osc.startswith("OSC"):
            a = self._afio
            r = rcc(self.chip) if self.chip is not None else None
            hse = r is not None and bool(r.get("CR") & (1 << 16))
            if a is None or not a.pd01_remap() or hse:
                return osc, "analog", None, None
        usb = f.get("additional")
        if usb in ("USBDM", "USBDP", "OTG_FS_DM", "OTG_FS_DP"):        # OTG_FS_DM/DP: connectivity line (Table 30)
            u = self._periph("OTG_FS" if usb.startswith("OTG") else "USB")
            on = getattr(u, "transceiver_on", None)
            if on is not None and on():
                return usb, "in", None, None
        return None

    def _pwr(self) -> Any:
        if self._pwr_blk is None and self.chip is not None:
            self._pwr_blk = find_model(self.chip, "stm32f1.pwr", "PWR") or False
        return self._pwr_blk or None

    def _trace_mode(self) -> int | None:
        """DBGMCU_CR trace pin assignment (31.16.3): None with TRACE_IOEN = 0, else TRACE_MODE (0 async)."""
        if self._dbg_blk is None and self.chip is not None:
            self._dbg_blk = find_model(self.chip, "stm32f1.dbgmcu", "DBG") or False
        d = self._dbg_blk
        if not d:
            return None
        cr = d.get("CR")
        return (cr >> 6) & 3 if cr & (1 << 5) else None

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

    def _lckr_peek(self, blk, reg):
        if self._lock_seq == 3:
            return self._lock_val                 # the read completing the sequence returns LCKK = 0
        return (self._locked | LCKK) if self._locked else None

    def _idr_peek(self, blk, reg):
        """IDR from the pin levels, without read counters, pin-report touches, warnings or the cache."""
        v = 0
        an = self._an_mask
        for i, pin in enumerate(self.pins):
            if pin is not None and not an >> i & 1 and pin.level() == "H":
                v |= 1 << i
        return v

    def _lckr_read(self, blk, reg):
        if self._lock_seq == 3:
            # 9.2.7 sequence: write 1, write 0, write 1, read 0, read 1: the first read completes the lock and still
            # returns LCKK = 0; the following reads return LCKK = 1
            self._lock_seq = 0
            self._locked = self._lock_val
            self.emit("info", block=self.name, msg=f"{self.name}: pins 0x{self._locked:04x} locked (LCKR)")
            return self._locked
        if self._locked:
            return self._locked | LCKK
        if self._lock_seq:
            self._lock_seq = 0                 # a read in the middle of the write sequence aborts it
        return None

    # ------------------------------------------------------------ input
    def _idr_read(self, blk, reg):
        self.n_idr += 1
        v = self._idr
        return v if v is not None else self._idr_compute()

    def _idr_compute(self) -> int:
        pb = self.chip.pins if self.chip is not None else None
        if pb is not None and pb._waves:
            pb.sync_waves()                 # waveform sources (UART TX frames): the pins' levels at this instruction
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
        self._idr = v if pb is None or not pb._waves else None      # no cache while a waveform is pending
        o = self.regs["IDR"].boff
        self.mem[o:o + 4] = v.to_bytes(4, "little")
        return v
