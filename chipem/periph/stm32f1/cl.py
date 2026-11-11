"""STM32F105/F107 connectivity-line blocks reused from the STM32F4 models by subclassing (RM0008 Rev 21).

AFIO line "cl" (stm32f1.afio `params.density: cl`, registered here with `gpio.register_line`): the connectivity-line
AFIO_MAPR layout of RM0008 9.4.2 and the remap tables 9.3.3-9.3.12 (`REMAP_CL`, below).

`stm32f1.eth` -- the Ethernet MAC of the STM32F107 (RM0008 ch. 29), i.e. `stm32.eth` (chipem/periph/stm32/eth.py, the
F407 MAC, checked against RM0090 ch. 33) with the RM0008 differences:
* register table: the F107 SVD blocks ETHERNET_MAC / _MMC / _PTP / _DMA (chips/stm32f107_regs.yaml); the F4-only
  registers MACDBGR, PTPTSSR, PTPPPSCR and DMARSWTR do not exist (reads 0), MACRWUFFR (0x28) is storage.
* DMABMR bit 7 is reserved (29.8.4: no EDFE, so no enhanced 32-byte descriptors, no RDES4 extended status, no time
  stamp words 6/7); MACCR bit 25 (CSTF) is reserved (29.8.1 "Bits 31:24 Reserved"); MMCCR has only CR / CSR / ROR /
  MCF (bits 0-3, 29.8.2: no MCP / MCFHP preset); PTPTSCR bits 0-5 only (29.8.3).
* MACMIIAR.CR (29.8.1): 000 HCLK/42 (60-72 MHz), 010 /16 (20-35 MHz), 011 /26 (35-60 MHz); 001 and 1xx are reserved
  (the F4's /62 and /102 do not exist: one `warn`, /102 assumed).
* SVD fixes (the RM wins): MACA2HR resets to 0x0000FFFF (SVD 0x50), MACA0HR to 0x8000FFFF (SVD 0x0010FFFF), MMCCR.MCF
  is bit 3 (SVD bit 31).
* clocks: RCC_AHBENR ETHMACEN (14) gates the registers, ETHMACTXEN (15) / ETHMACRXEN (16) must be set to send /
  receive (8.3.6); RCC_AHBRSTR.ETHMACRST (8.3.11) resets the MAC.  AHB clock below 25 MHz: one `warn` (stm32f1.rcc_cl).
* MII / RMII: AFIO_MAPR.MII_RMII_SEL (bit 23, 9.4.2), latched when the MAC clock is enabled and at every MAC reset
  (29.4.4: select it "while the Ethernet controller is under reset or before enabling the clocks").
* pins: on the F1 a pin owned by the Ethernet block carries every Ethernet function listed for it (no per-signal AF
  selection): a pin offering ETH_MII_RXD0/ETH_RMII_RXD0 serves both.  AFIO_MAPR.ETH_REMAP moves RX_DV-CRS_DV and
  RXD0-3 to PD8-PD12 (Table 58, stm32f1.afio).
* PHY reference clock (29.4.4, Figure 11 / ST's reference design): params `phy_clock`: "phy" (default: the PHY has
  its own 25 MHz crystal and supplies TX_CLK / RX_CLK or the 50 MHz REF_CLK) or "mco" (the PHY is clocked from the
  MCO pin PA8: MII needs 25 MHz on MCO (XT1 / HSE, or PLL3 / 2 ...), RMII needs 50 MHz on MCO (PLL3) and the PHY
  drives REF_CLK from it).  With "mco" the interface clocks exist only while PA8 is muxed to MCO and outputs that
  frequency (+-0.1 %): otherwise DMABMR.SR never completes and frames are lost, as on a board whose PHY has no clock.
  A board `eth` link sets it per node with the PHY option `clock: mco`.
* wake-up: EXTI line 19 (ETH_WKUP, 10.2.5) on a magic packet with MACPMTCSR.MPE (as on the F4).

`stm32f1.can_cl` -- bxCAN1 / bxCAN2 of the connectivity line (RM0008 ch. 24): the F4 dual bxCAN (`stm32.can`,
chipem/periph/stm32/can.py, CAN1 master with the 28 filter banks shared through FMR.CAN2SB, CAN2 slave that needs the
CAN1 clock; checked against RM0090 ch. 32, which describes the same IP) unchanged except for the register table: the
F107 SVD prefixes the CAN registers (CAN_MCR ...), so the F405 table's CAN1/CAN2 layout is used (same offsets, same
reset values: MCR 0x00010002, MSR 0x00000C02, TSR 0x1C000000, BTR 0x01230000, FMR 0x2A1C0E01 = CAN2SB 14).  Clock
gates RCC_APB1ENR.CAN1EN / CAN2EN (8.3.8), vectors CAN1_TX/RX0/RX1/SCE (19-22), CAN2_TX/RX0/RX1/SCE (63-66), pins
CAN1_RX/TX (AFIO_MAPR.CAN1_REMAP: PA11/PA12, PB8/PB9, PD0/PD1) and CAN2_RX/TX (CAN2_REMAP: PB12/PB13, PB5/PB6).

`stm32f1.otg_fs` -- the USB OTG FS core of the STM32F105/F107 (RM0008 ch. 28), `stm32.otg_fs` (DWC2, device mode;
chipem/periph/stm32/otg.py, checked against RM0090 ch. 34) with the RM0008 differences:
* GCCFG (28.6.2 "OTG_FS general core configuration register"): bits PWRDWN (16), VBUSASEN (18), VBUSBSEN (19),
  SOFOUTEN (20) only: no NOVBUSSENS (bit 21 reserved), so the B-session is valid only with VBUSBSEN = 1 and VBUS
  present on PA9 (OTG_FS_VBUS); the usb link drives that pin.
* DIEPTXFx (x = 1..3) reset value 0x0200 0200 (28.6.2).  4 bidirectional endpoints, 1.25 KB FIFO RAM, TRDT Table 206
  (= RM0090 Table 203), HCLK at least 14.2 MHz (Table 206): unchanged.
* clocks: RCC_AHBENR.OTGFSEN (12) / AHBRSTR.OTGFSRST (12); OTGFSCLK from stm32f1.rcc_cl (`clk48_hz`, PLL VCO / 3 or
  / 2, must be 48 MHz); the wake-up is EXTI line 18 (vector OTG_FS_WKUP).
* pins: OTG_FS_DM / OTG_FS_DP (PA11 / PA12) are "controlled automatically by the USB power-down" (9.1.11 Table 30):
  stm32f1.gpio gives them to the transceiver while the OTG clock is on and GCCFG.PWRDWN = 1 (`transceiver_on()`).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..stm32.can import BxCAN as _F4CAN
from ..stm32.eth import ETH as _F4ETH
from ..stm32.otg import OtgCore as _F4Otg
from ..stm32 import otg as _otg
from ._util import F4_REGS, add_ro, find_model, fmt_hz
from .gpio import REMAP as _REMAP, register_line

# ---------------------------------------------------------------------------------------------- AFIO (9.3, 9.4.2)
# Connectivity-line AFIO_MAPR ("Memory map and bit definitions for connectivity line devices", 9.4.2): the low/high
# density fields 0-16 except CAN_REMAP, which is CAN1_REMAP here (Table 34: CAN1_RX/CAN1_TX); bits 17-20 (ADC trigger
# remaps) are reserved; ETH_REMAP (21, Table 58), CAN2_REMAP (22, Table 35), MII_RMII_SEL (23, no pin), SPI3_REMAP
# (28, Table 57, SPI3 and its I2S3 signal names), TIM2ITR1_IREMAP (29, internal), PTP_PPS_REMAP (30: PPS on PB5).
_ETH0 = {"PA7": ("ETH_MII_RX_DV", "ETH_RMII_CRS_DV"), "PC4": ("ETH_MII_RXD0", "ETH_RMII_RXD0"),
         "PC5": ("ETH_MII_RXD1", "ETH_RMII_RXD1"), "PB0": ("ETH_MII_RXD2",), "PB1": ("ETH_MII_RXD3",)}
_ETH1 = dict(zip(("PD8", "PD9", "PD10", "PD11", "PD12"), _ETH0.values()))
REMAP_CL = {k: v for k, v in _REMAP.items() if k != "CAN"}
REMAP_CL.update({
    "CAN1": (13, 2, {0: {"CAN1_RX": "PA11", "CAN1_TX": "PA12"}, 2: {"CAN1_RX": "PB8", "CAN1_TX": "PB9"},
                     3: {"CAN1_RX": "PD0", "CAN1_TX": "PD1"}}),
    "ETH": (21, 1, {0: {s: p for p, ss in _ETH0.items() for s in ss}, 1: {s: p for p, ss in _ETH1.items() for s in ss}}),
    "CAN2": (22, 1, {0: {"CAN2_RX": "PB12", "CAN2_TX": "PB13"}, 1: {"CAN2_RX": "PB5", "CAN2_TX": "PB6"}}),
    "SPI3": (28, 1, {0: {"SPI3_NSS": "PA15", "I2S3_WS": "PA15", "SPI3_SCK": "PB3", "I2S3_CK": "PB3",
                         "SPI3_MISO": "PB4", "SPI3_MOSI": "PB5", "I2S3_SD": "PB5"},
                     1: {"SPI3_NSS": "PA4", "I2S3_WS": "PA4", "SPI3_SCK": "PC10", "I2S3_CK": "PC10",
                         "SPI3_MISO": "PC11", "SPI3_MOSI": "PC12", "I2S3_SD": "PC12"}}),
    "PTP_PPS": (30, 1, {0: {}, 1: {"ETH_PPS_OUT": "PB5"}}),
})
MAPR_RW_CL = 0x0001FFFF | (7 << 21) | (7 << 24) | (7 << 28)
register_line("cl", REMAP_CL, MAPR_RW_CL)

ETH_BLOCKS = ("ETHERNET_MAC", "ETHERNET_MMC", "ETHERNET_PTP", "ETHERNET_DMA")
_F4_GATE_BITS = {25: 14, 26: 15, 27: 16}            # F4 AHB1ENR ETHMAC/TX/RX -> F1 AHBENR (RM0008 8.3.6)
_CLK_SIGS = ("ETH_RMII_REF_CLK", "ETH_MII_TX_CLK", "ETH_MII_RX_CLK")
MII_RMII_SEL = 1 << 23


def _set_ro(reg: Any, ro: int) -> None:
    """Replace the read-only mask of `reg` (SVD field positions wrong)."""
    reset, _, w1c = reg._raw
    reg._raw = (reset, ro, w1c)
    reg.set_unit_bits(reg.unit_bits)


def _set_reset(reg: Any, value: int) -> None:
    _, ro, w1c = reg._raw
    reg._raw = (value, ro, w1c)
    reg.set_unit_bits(reg.unit_bits)


@register("stm32f1.eth")
class ETH(_F4ETH):
    MDC_DIV = {0: 42, 2: 16, 3: 26}   # 29.8.1 MACMIIAR.CR: 000 60-72 MHz, 010 20-35, 011 35-60; 001 / 1xx reserved

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params.setdefault("svd_blocks", list(ETH_BLOCKS))
        super().__init__(name, base, size, regs, params=params, log=log)
        r = self.regs
        _set_reset(r["MACA2HR"], 0x0000FFFF)                  # 29.8.1 (SVD: 0x50)
        add_ro(r["DMABMR"], 1 << 7)                           # 29.8.4: bit 7 reserved (no EDFE)
        add_ro(r["MACCR"], 0xFF000000)                        # 29.8.1: bits 31:24 reserved (no CSTF)
        _set_ro(r["MMCCR"], 0xFFFFFFF0)                       # 29.8.2: CR, CSR, ROR, MCF (bit 3) only
        add_ro(r["PTPTSCR"], 0xFFFFFFC0)                      # 29.8.3: TSE..TTSARU (bits 5:0) only
        self.phy_clock = str(params.get("phy_clock", "phy")).lower()
        if self.phy_clock not in ("phy", "mco"):
            raise ValueError(f"{name}: phy_clock must be 'phy' or 'mco'")
        self._afio: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._afio = find_model(chip, "stm32f1.afio", "AFIO")
        r = self._rcc
        if r is not None and not self.absent:
            r.listeners.append(self._mco_changed)

    def _mco_changed(self) -> None:
        if self.phy_clock == "mco":
            self._clock_waves()

    def eth_attach(self, segment: Any, **phy: Any) -> None:
        """Board `eth` link: the PHY option `clock: mco` / `clock: phy` sets `phy_clock` (where the PHY's reference
        clock comes from); the other options go to the PHY."""
        clk = phy.pop("clock", None)
        if clk is not None:
            clk = str(clk).lower()
            if clk not in ("phy", "mco"):
                raise ValueError(f"{self.name}: PHY clock must be 'phy' or 'mco', not {clk!r}")
            self.phy_clock = clk
        super().eth_attach(segment, **phy)

    # ------------------------------------------------------------------ RCC gates (AHBENR, 8.3.6)
    def _gate(self, bit: int) -> bool:
        r = self._rcc
        if r is None:
            return True
        return bool(r.get("AHBENR") >> _F4_GATE_BITS.get(bit, bit) & 1)

    def _gate_changed(self, enr: str, old: int, new: int) -> None:
        if enr != "AHBENR":
            return
        if (new & ~old) >> 14 & 1:
            self._latch_iface()
        if (old ^ new) & (1 << 14):
            self._clock_waves()

    # ------------------------------------------------------------------ MII / RMII (AFIO_MAPR.MII_RMII_SEL)
    def _sel(self) -> str:
        a = self._afio
        return "rmii" if a is not None and a.get("MAPR") & MII_RMII_SEL else "mii"

    def _latch_iface(self) -> None:
        old, self._iface = self._iface, self._sel()
        if old != self._iface and self.chip is not None:
            from ._util import gpio_ports
            for p in gpio_ports(self.chip):
                p.resync_block("ETH")

    def _iface_now(self) -> str:
        now = self._sel()
        if now != self._iface:
            self._warn_once("pmc", f"{self.name}: AFIO_MAPR.MII_RMII_SEL changed to {now.upper()} while the MAC clock "
                            f"was running: ignored until the MAC is reset (RCC ETHMACRST or DMABMR.SR, RM0008 "
                            f"29.4.4); still {str(self._iface).upper()}")
        return self._iface or "mii"

    # ------------------------------------------------------------------ pins / PHY clock
    def mco_ok(self) -> bool:
        """phy_clock = mco: PA8 muxed to MCO and MCO at 25 MHz (MII) / 50 MHz (RMII)."""
        r = self._rcc
        board = getattr(self.chip, "pins", None)
        if r is None or board is None or not hasattr(r, "mco_hz"):
            return True
        if not any(p.owner == "MCO" for p in board.signal_pins("MCO")):
            return False
        want = 50e6 if (self._iface or "mii") == "rmii" else 25e6
        return abs(r.mco_hz() - want) <= want * 1e-3

    def _muxed(self, sig: str) -> bool:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return True
        if self.phy_clock == "mco" and sig in _CLK_SIGS and not self.mco_ok():
            if (self._iface or "mii") == "rmii":
                need = "50 MHz (RCC_CFGR.MCO = PLL3)"
            else:
                need = "25 MHz (RCC_CFGR.MCO = XT1 / HSE with a 25 MHz crystal)"
            self._warn_once("mco", f"{self.name}: the PHY is clocked from MCO (PA8) but MCO is not muxed or not at "
                            f"{need}: no {str(self._iface or 'mii').upper()} interface clock")
            return False
        for p in board.signal_pins(sig):
            own = p.owner or ""
            if board.signal_matches(own, sig):
                return True
            if own.startswith("ETH_"):          # the pin is the Ethernet block's: all its ETH functions are connected
                toks = {t.strip() for v in p.functions.values() for t in str(v).split("/")}
                if own in toks:
                    return True
        return False

    def signal_state(self, sig: str) -> tuple[str, str | None] | None:
        """Live role of an Ethernet pin signal for stm32f1.gpio: the signals of the interface not selected by
        MII_RMII_SEL (e.g. ETH_MII_COL in RMII mode) are unused (direction "none"), so a pin offering both names
        (PA1 ETH_MII_RX_CLK / ETH_RMII_REF_CLK) is shown with the one in use."""
        if self.absent:
            return None
        iface = self._sel() if not self._gate(25) else (self._iface or self._sel())
        from .signals import TABLE
        out = TABLE.role(sig).direction == "out"
        if sig.startswith("ETH_MII_") and iface == "rmii" or sig.startswith("ETH_RMII_") and iface == "mii":
            return "none", None                              # unused: stm32f1.gpio gives it no score
        if TABLE.role(sig).name == "od":                         # ETH_MDIO: bidirectional, static role
            return None
        return ("out", "L") if out else ("in", None)

    def _clock_waves(self) -> None:
        if self.phy_clock == "mco" and self.chip is not None and self._waves and not self.mco_ok():
            for w in self._waves.values():
                if w.hz:
                    w.set(0.0, False)
            return
        super()._clock_waves()


@register("stm32f1.can_cl")
class CAN(_F4CAN):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params["regs_file"] = F4_REGS                 # same layout, unprefixed register names (RM0008 24.9.5)
        super().__init__(name, base, size, regs, params=params, log=log)


@register("stm32f1.otg_fs")
class OTGFS(_F4Otg):
    DEFAULTS = {**_F4Otg.DEFAULTS, "endpoints": 4, "fifo_words": 320, "prefix": "OTG_FS", "gate": "OTG_FS",
                "exti_line": 18}

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        add_ro(self.regs["GCCFG"], 1 << 21)                    # 28.6.2: no NOVBUSSENS on the F105/F107
        for n in range(1, self.n_eps):                        # 28.6.2 OTG_FS_DIEPTXFx: reset 0x0200 0200
            reg = self.regs.get(f"DIEPTXF{n}")
            if reg is not None:
                _set_reset(reg, 0x02000200)

    def transceiver_on(self) -> bool:
        """PA11/PA12 belong to the embedded PHY while the core is clocked and GCCFG.PWRDWN = 1 (9.1.11 Table 30)."""
        if self.chip is None or self._rcc_gate_off():
            return False
        return bool(self._r(0x038) & _otg.PWRDWN)

    def _gccfg_write(self, blk, reg, old, new):
        r = super()._gccfg_write(blk, reg, old, new)
        if (old ^ new) & _otg.PWRDWN and self.chip is not None:
            from ._util import gpio_ports
            for p in gpio_ports(self.chip):
                p.resync_all()
        return r

    def _clock_ok(self) -> bool:
        f = self._clk48()
        if abs(f - 48e6) > _otg.USB_TOL * 48e6:
            if self._r(0x038) & _otg.PWRDWN:
                what = "stopped (PLL off)" if f <= 0 else fmt_hz(f)
                self._warn_once(f"clk{round(f)}", f"{self.name}: OTGFSCLK is {what}: it must be 48 MHz +-0.25 % "
                                f"(PLL 72 MHz with RCC_CFGR.OTGFSPRE = 0 or 48 MHz with OTGFSPRE = 1, RM0008 8.2); "
                                f"the core does not answer the host")
            return False
        return super()._clock_ok()
