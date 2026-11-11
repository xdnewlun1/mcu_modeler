"""STM32F4 USB on-the-go cores (RM0090 ch. 34 OTG_FS, ch. 35 OTG_HS; Synopsys DWC2 2.81a) in device mode, on a
chipem.usbbus cable.

One class, two instances: `stm32.otg_fs` (0x50000000, 4 bidirectional endpoints, 1.25 KB FIFO RAM = 320 words,
embedded FS PHY) and `stm32.otg_hs` (0x40040000, 6 endpoints, 4 KB FIFO RAM, internal DMA; run with its embedded
FS PHY only: selecting the ULPI PHY logs a `warn` and the core never connects, since no external high-speed PHY is
modelled).  Params: `endpoints`, `fifo_words`, `dma`, `prefix` (signal / IRQ name prefix "OTG_FS" / "OTG_HS"),
`gate` (RCC clock gate), `exti_line` (wakeup line: 18 FS, 20 HS), `clk48_hz` (48 MHz clock when there is no RCC),
`channels` (host channels, storage), `core: otg2`.

`core: otg2` (STM32F446, RM0390 ch. 31, OTG 2.0 core with LPM; chip YAML 6 FS / 9 HS endpoints, 12 / 16 channels,
Table 221): GCCFG has only PWRDWN (16) and VBDEN (21, VBUS sensing on PA9 / PB13) (31.15.15); GOTGCTL adds the
session-valid overrides BVALOEN/BVALOVAL (B-valid in device mode), AVALOEN/AVALOVAL, VBVALOEN/VBVALOVAL, EHEN, OTGVER
and CURMOD / DBCT status (31.15.1); the B-session is valid when BVALOEN = 1 and BVALOVAL = 1, or with VBDEN = 1 and
VBUS on the pin; GINTMSK LPMINTM / RSTDETM writable; GUSBCFG bit 31 reserved; CID 0x2000 (HS 0x2100, 31.15.16);
GLPMCFG (0x54, 31.15.17: storage, LPMEN logs one `warn`: the host model sends no LPM tokens) and GDFIFOCFG (0x5C,
31.15.18, storage).  No battery charging detection on the F446 (Table 221: BCD not supported).

Register file (offsets in the 256 KB window; reset values and writable bits from the RM0090 register descriptions
34.16 / 35.12, checked by tests/test_stm32_usb.py; where they differ from the SVD the RM wins: GOTGCTL 0x00010000,
GRSTCTL 0x80000000, CID 0x1200 (HS 0x1100), HPTXFSIZ 0x02000400 (HS 0x02000800), DIEPTXF1 0x02000200, HS GUSBCFG
0x1440 / GRXFSIZ 0x400 / GNPTXSTS 0x80400; reserved bits read 0):
  0x000 GOTGCTL (BSVLD / ASVLD / CIDSTS live), 0x004 GOTGINT (rc_w1: SEDET ...), 0x008 GAHBCFG (GINT, TXFELVL; HS:
  DMAEN, HBSTLEN), 0x00C GUSBCFG (PHYSEL, TRDT, FHMOD/FDMOD), 0x010 GRSTCTL (CSRST, RXFFLSH, TXFFLSH + TXFNUM
  self-clearing; AHBIDL = 1), 0x014 GINTSTS, 0x018 GINTMSK, 0x01C GRXSTSR, 0x020 GRXSTSP, 0x024 GRXFSIZ, 0x028
  DIEPTXF0 (HNPTXFSIZ), 0x02C HNPTXSTS, 0x038 GCCFG (PWRDWN, VBUSASEN, VBUSBSEN, SOFOUTEN, NOVBUSSENS), 0x03C CID,
  0x040 GSNPSID (0x4F54281A: core 2.81a, read by the HAL), 0x100 HPTXFSIZ, 0x104.. DIEPTXFx,
  0x400.. host registers (HCFG, HFIR, HFNUM, HPTXSTS, HAINT, HAINTMSK, HPRT, HCCHARx/HCINTx/HCINTMSKx/HCTSIZx/HCDMAx:
  storage only; forcing host mode logs one `warn`),
  0x800 DCFG, 0x804 DCTL, 0x808 DSTS, 0x810 DIEPMSK, 0x814 DOEPMSK, 0x818 DAINT, 0x81C DAINTMSK, 0x828 DVBUSDIS,
  0x82C DVBUSPULSE, 0x834 DIEPEMPMSK, (HS: 0x830 DTHRCTL, 0x838 DEACHINT, 0x83C DEACHMSK, 0x844 DINEP1MSK,
  0x884 DOUTEP1MSK), 0x900 + 0x20n DIEPCTLn / +8 DIEPINTn / +0x10 DIEPTSIZn / +0x14 DIEPDMAn (HS) / +0x18 DTXFSTSn,
  0xB00 + 0x20n DOEPCTLn / DOEPINTn / DOEPTSIZn / DOEPDMAn, 0xE00 PCGCCTL, 0x1000 * (n + 1) data FIFO push/pop
  windows, 0x20000 direct access to the FIFO RAM.  A host register accessed in device mode (or a device register in
  host mode) sets GINTSTS.MMIS and is ignored (34.15).

Behaviour (device mode, DWC2 dedicated-FIFO mode as used by the HAL PCD driver and ST's USB device library; section
numbers are RM0090 Rev 22, ch. 34 OTG_FS, the OTG_HS chapter 35 says the same):
* Mode: GINTSTS.CMOD = 0 (device) unless GUSBCFG.FHMOD forces host (or the ID pin is grounded); a forced mode
  change takes effect 25 ms after the write ("must wait at least 25 ms", 34.16.2 GUSBCFG).  Host mode is not
  modelled.
* Connection: the D+ pull-up is on when the core is in device mode, the transceiver is powered (GCCFG.PWRDWN), the
  B-session is valid (NOVBUSSENS = 1, or VBUSBSEN = 1 and VBUS present on the OTG_FS_VBUS pin PA9 / OTG_HS_VBUS
  PB13; the cable drives that pin), DCTL.SDIS = 0 and the DP/DM pins are muxed to the OTG function (AF10 / AF12).
  VBUS rising with sensing on -> GINTSTS.SRQINT; falling -> GOTGINT.SEDET (OTGINT) (34.5.2 "Powered state").
* Clocks: the FS PHY needs 48 MHz +-0.25 % (34.3.2) and HCLK above 14.2 MHz (OTG_FS, 34.3.3) / 30 MHz (OTG_HS,
  35.3.3); otherwise the core does not answer on the bus (the host times out: "device not recognised") and one
  `warn` explains why.  GUSBCFG.TRDT below the minimum of Table 203 / 213 for HCLK: one `warn` only.
  RCC_AHBxENR gate (an access with the clock off logs one `warn`; the core ignores the bus), RCC_AHBxRSTR resets
  the model.
* Bus events: USB reset -> GINTSTS.USBRST (2.5 us after SE0 started; USBAEP cleared on endpoints 1..n; NAKing the
  OUT endpoints is the firmware's job, 34.17.5) and ENUMDNE at its end (DSTS.ENUMSPD = 3).  3 ms idle -> ESUSP,
  "confirmed 3 ms later" by USBSUSP with DSTS.SUSPSTS = 1 (34.5.2); resume K -> WKUINT (SUSPSTS = 0) and the EXTI
  wakeup line (18 / 20).  SOF -> GINTSTS.SOF, DSTS.FNSOF; GCCFG.SOFOUTEN gives a 20-HCLK pulse on OTG_FS_SOF
  (34.7.2).  DCTL.RWUSIG set while suspended = remote wakeup signalling and clears SUSPSTS (34.16.4 DSTS).
* Address: tokens are answered for DCFG.DAD; a DAD change during a control transfer takes effect after its status
  stage: 34.17.5 has the firmware program DAD on SetAddress *before* the status IN, which must still go out at the
  old address.
* SETUP (34.17.6 "SETUP transactions"): accepted on EP0 whatever NAK / STALL / EPENA say, written to the RX FIFO
  "without checking for available space" into the 10 words the core keeps for SETUPs (never used for data):
  entry PKTSTS 6 (8 bytes, DATA0), DOEPTSIZ0.STUPCNT decremented; the core sets NAK on IN and OUT EP0 (DIEPINT0.INEPNE)
  and clears STALL; EP0 toggles reset.  The setup-stage-done entry (PKTSTS 4) is written "when the Setup stage
  changes to a Data IN/OUT stage" (the next IN or OUT token on EP0); popping it sets DOEPINT0.STUP and clears
  DOEPCTL0.EPENA.  More than 3 back-to-back SETUPs set B2BSTUP (34.17.6; the FS DOEPINT table marks bit 6 reserved).
* OUT (34.17.6): ACK if the endpoint is enabled, not NAKing, global OUT NAK off and the RX FIFO has room for status
  + data outside the SETUP reserve, else NAK (DOEPINT.NAK); STALL if DOEPCTL.STALL.  Entry PKTSTS 2 (BCNT, DPID);
  PKTCNT counts down per packet written, XFRSIZ as the application pops the packets (status-word pop).  PKTCNT = 0 or
  a short packet: the core sets the endpoint NAK and writes PKTSTS 3; popping it clears EPENA and sets XFRC.  Data
  with the wrong toggle is ACKed and dropped.  OUT token while EP0 is disabled: OTEPDIS.  A non-zero-length status
  OUT of a control read is STALLed when DCFG.NZLSOHSK = 1.  OTG_FS: the status IN token of a control write sets
  DOEPINT0.STSPHSRX ("status phase received for control write").  GRXSTSP pops the status word, reading any FIFO
  window pops data words, all from the RX FIFO ring in the FIFO RAM (GRXFSIZ words at address 0).
* IN (34.17.6 "Generic non-periodic IN data transfers"): each packet the application writes into the TX FIFO
  (min(MPS, XFRSIZ) bytes) decrements XFRSIZ; an IN token sends the oldest complete packet (or a zero-length packet
  when XFRSIZ = 0 and PKTCNT > 0) with DPID (EP0: DATA1 first after SETUP); the host's ACK pops it and decrements
  PKTCNT; XFRSIZ = PKTCNT = 0 -> XFRC, EPENA = 0.  STALL if DIEPCTL.STALL; NAK (DIEPINT.NAK) if NAKSTS / global IN
  NAK; with no complete packet: ITTXFE + NAK.  TX FIFOs are rings in the FIFO RAM at DIEPTXF0 / DIEPTXFx;
  DIEPINT.TXFE = half / completely empty (TXFELVL), unmasked through DIEPEMPMSK; DTXFSTS.INEPTFSAV = free words.
  Pushing into a full FIFO is lost (one `warn`; the RM requires the firmware to check DTXFSTS first).
* Endpoint control: CNAK/SNAK (NAKSTS; SNAK on IN sets INEPNE, CNAK clears it), SD0PID/SD1PID (DPID), EPDIS on an
  enabled endpoint disables it at once (EPDISD; EP0 OUT cannot be disabled), STALL (EP0: set only, cleared by the
  next SETUP).  DCTL SGINAK/CGINAK (GINAKEFF, GINSTS), SGONAK/CGONAK (RX FIFO entry PKTSTS 1, GONAKEFF when popped,
  GONSTS).
* Interrupts: DAINT bit n = DIEPINTn & (DIEPMSK | TXFE if DIEPEMPMSK bit n), bit 16+n = DOEPINTn & DOEPMSK;
  GINTSTS.IEPINT/OEPINT = DAINT & DAINTMSK; RXFLVL = RX FIFO not empty; OTGINT = GOTGINT != 0; the NVIC line
  "<prefix>" = GAHBCFG.GINT & (GINTSTS & GINTMSK) != 0.  HS dedicated EP1 lines (OTG_HS_EP1_IN/OUT) follow DEACHINT
  & DEACHMSK.
* DMA (HS, GAHBCFG.DMAEN): OUT data and SETUP packets are written to memory at DOEPDMAn (incremented), IN data read
  from DIEPDMAn at the IN token; no RX FIFO entries, XFRC/STUP set directly.  A DMA address in Flash reads zeros
  with a `warn` ("The USB DMA cannot directly address the internal Flash memory", Figure 410).
* Core soft reset (GRSTCTL.CSRST, 34.16.2): all CSRs back to reset except PCGCCTL, DCFG.DSPD, HCFG.FSLSPCS (RM) and
  GUSBCFG (DWC2: the force-mode bits survive; Linux dwc2_core_reset), FIFOs flushed.  CSRST, RXFFLSH and TXFFLSH
  act at once and read 1 for 8 clocks of the slower of PHY / AHB clock (the RM gives 8 clocks for RXFFLSH and
  "several clocks" for CSRST).
Not modelled: isochronous endpoints (no IISOIXFR / INCOMPISOOUT / EOPF), high speed, host mode, HNP/SRP protocol
(SRQ/HNPRQ are storage), LPM, threshold control, TOCAL, the TX FIFO 8-packet limit.
"""
from __future__ import annotations

import re
from typing import Any

from ...canbus import pin_reason
from ..base import Reg, pure_read
from ..registry import register
from ._util import GatedBlock, exti_input, fmt_hz, irq_number, rcc

# GINTSTS
CMOD, MMIS, OTGINT, SOF, RXFLVL, NPTXFE, GINAKEFF, GONAKEFF = (1 << b for b in (0, 1, 2, 3, 4, 5, 6, 7))
ESUSP, USBSUSP, USBRST, ENUMDNE, ISOODRP, EOPF = (1 << b for b in (10, 11, 12, 13, 14, 15))
IEPINT, OEPINT, IISOIXFR, IPXFR, HPRTINT, HCINT, PTXFE = (1 << b for b in (18, 19, 20, 21, 24, 25, 26))
CIDSCHG, DISCINT, SRQINT, WKUINT = (1 << b for b in (28, 29, 30, 31))
GINTSTS_W1C = MMIS | SOF | ESUSP | USBSUSP | USBRST | ENUMDNE | ISOODRP | EOPF | IISOIXFR | IPXFR | CIDSCHG | \
    DISCINT | SRQINT | WKUINT
GINTSTS_LIVE = CMOD | OTGINT | RXFLVL | NPTXFE | GINAKEFF | GONAKEFF | IEPINT | OEPINT | HPRTINT | HCINT | PTXFE
# GCCFG
PWRDWN, VBUSASEN, VBUSBSEN, SOFOUTEN, NOVBUSSENS = 1 << 16, 1 << 18, 1 << 19, 1 << 20, 1 << 21
# GUSBCFG
PHYSEL, FHMOD, FDMOD = 1 << 6, 1 << 29, 1 << 30
# GRSTCTL
CSRST, HSRST, FCRST, RXFFLSH, TXFFLSH, AHBIDL = 1, 2, 4, 16, 32, 1 << 31
# GAHBCFG
GINT, DMAEN, TXFELVL = 1, 1 << 5, 1 << 7
# DCTL
RWUSIG, SDIS, GINSTS, GONSTS, SGINAK, CGINAK, SGONAK, CGONAK = 1, 2, 4, 8, 1 << 7, 1 << 8, 1 << 9, 1 << 10
# DSTS
SUSPSTS = 1
# EP control
USBAEP, DPID, NAKSTS, STALL, CNAK, SNAK, SD0PID, SD1PID, EPDIS, EPENA = (1 << 15, 1 << 16, 1 << 17, 1 << 21, 1 << 26,
                                                                        1 << 27, 1 << 28, 1 << 29, 1 << 30, 1 << 31)
# DIEPINT
XFRC, EPDISD, TOC, ITTXFE, INEPNM, INEPNE, TXFE, EPNAK = 1, 2, 8, 16, 32, 64, 128, 1 << 13
# DOEPINT
AHBERR, STUP, OTEPDIS, STSPHSRX, B2BSTUP, OUTPKTERR, BERR, NYET = 4, 8, 16, 32, 64, 1 << 8, 1 << 12, 1 << 14
# rc_w1 bits (RM0090 34.16.4 / 35.12.4 DIEPINTx / DOEPINTx; TXFE is read-only, the rest reserved)
DIEPINT_W1C = XFRC | EPDISD | TOC | ITTXFE | INEPNM | INEPNE | EPNAK
DOEPINT_W1C_FS = XFRC | EPDISD | STUP | OTEPDIS | STSPHSRX | B2BSTUP | OUTPKTERR | BERR | EPNAK
DOEPINT_W1C_HS = DOEPINT_W1C_FS | AHBERR | NYET
# writable bits of the endpoint control registers (RM0090 34.16.4: reserved bits read 0)
DIEPCTL0_RW = EPENA | EPDIS | STALL | (0xF << 22) | 3
DOEPCTL0_RW = EPENA | STALL | (1 << 20)                                 # EPDIS: EP0 OUT cannot be disabled
DIEPCTLX_RW = EPENA | EPDIS | STALL | (0xF << 22) | (3 << 18) | USBAEP | 0x7FF
DOEPCTLX_RW = EPENA | EPDIS | STALL | (1 << 20) | (3 << 18) | USBAEP | 0x7FF
# GOTGINT
SEDET = 4
# RX status PKTSTS
ST_GONAK, ST_OUT, ST_OUT_DONE, ST_SETUP_DONE, ST_SETUP = 1, 2, 3, 4, 6

GSNPSID = 0x4F54281A
EP0_MPS = (64, 32, 16, 8)
USB_TOL = 0.0025
SETUP_RESERVE = 10            # RX FIFO words the core keeps for SETUP packets (RM0090 34.17.6 "SETUP transactions")
FLUSH_CLOCKS = 8              # RXFFLSH: "8 clocks (slowest of PHY or AHB clock)" (34.16.2 GRSTCTL)
SOF_PULSE_HCLK = 20           # SOF output pulse width in HCLK cycles (34.7.2)
# Table 203 (OTG_FS) minimum GUSBCFG.TRDT per AHB frequency (MHz lower bound, TRDT); Table 213 (OTG_HS)
TRDT_FS = ((32, 0x6), (27.5, 0x7), (24, 0x8), (21.8, 0x9), (20, 0xA), (18.5, 0xB), (17.2, 0xC), (16, 0xD),
           (15, 0xE), (14.2, 0xF))
TRDT_HS = ((30, 0x9),)


class _Ring:
    """A FIFO in the core's FIFO RAM: `depth` words from word address `start` (wrapping inside the FIFO)."""
    __slots__ = ("ram", "start", "depth", "rd", "n")

    def __init__(self, ram: list[int], start: int = 0, depth: int = 0):
        self.ram, self.start, self.depth, self.rd, self.n = ram, start, depth, 0, 0

    def config(self, start: int, depth: int) -> None:
        if (start, depth) != (self.start, self.depth):
            self.start, self.depth = start, depth
            self.rd = self.n = 0

    @property
    def free(self) -> int:
        return max(0, self.depth - self.n)

    def push(self, w: int) -> bool:
        if self.n >= self.depth or self.depth <= 0:
            return False
        ram = self.ram
        ram[(self.start + (self.rd + self.n) % self.depth) % len(ram)] = w & 0xFFFFFFFF
        self.n += 1
        return True

    def peek(self, i: int = 0) -> int:
        if i >= self.n or self.depth <= 0:
            return 0
        ram = self.ram
        return ram[(self.start + (self.rd + i) % self.depth) % len(ram)]

    def pop(self) -> int:
        if self.n <= 0:
            return 0
        w = self.peek(0)
        self.rd = (self.rd + 1) % self.depth
        self.n -= 1
        return w

    def flush(self) -> None:
        self.rd = self.n = 0


def _words(data: bytes) -> list[int]:
    data = bytes(data)
    pad = data + bytes(-len(data) % 4)
    return [int.from_bytes(pad[i:i + 4], "little") for i in range(0, len(pad), 4)]


@register("stm32.otg_fs")
class OtgCore(GatedBlock):
    """DWC2 OTG core in device mode (see the module doc)."""

    DEFAULTS = {"endpoints": 4, "fifo_words": 320, "dma": False, "prefix": "OTG_FS", "gate": "OTG_FS",
                "exti_line": 18}

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        p = {**self.DEFAULTS, **(params or {})}
        self.n_eps = int(p["endpoints"])
        self.fifo_words = int(p["fifo_words"])
        self.has_dma = bool(p["dma"])
        self.prefix = str(p["prefix"])
        self.gate = str(p["gate"])
        self.exti_line = int(p["exti_line"]) if p.get("exti_line") is not None else None
        # `core: otg2` = the OTG 2.0 core revision of the STM32F446 (RM0390 31.15): GCCFG.VBDEN instead of
        # NOVBUSSENS/VBUSxSEN, GOTGCTL session-valid overrides, LPM registers, CID 0x2000/0x2100
        self.otg2 = str(p.get("core", "")).lower() == "otg2"
        self.n_channels = int(p.get("channels", 12 if self.has_dma else 8))
        self.usb_name = re.sub(r"_GLOBAL$", "", name)
        self.ram = [0] * self.fifo_words
        self.rx = _Ring(self.ram, 0, 0)
        self.tx = [_Ring(self.ram) for _ in range(16)]
        self.bus = None                       # chipem.usbbus.UsbBus
        self._warned: set[str] = set()
        self._evs: list = []
        self._mode_ev = None
        self._cmod = 0
        self._session = False
        self._suspended = False
        self._setup_pending = False           # a SETUP waits for its "setup stage done" entry
        self._b2b = 0
        self._ctrl_in = False                 # direction of the control transfer in progress
        self._ep0_in_tog = 1
        self._ep0_out_tog = 1
        self._addr_old: int | None = None
        self._in_pending: dict[int, tuple[int, int, bytes]] = {}
        self._gonak = None                    # None | "pending" | "effective"
        self._ginak = False
        self._line = False
        self._ep1_lines = (False, False)
        self._irqn: int | None = None
        self._irq_ep1in: int | None = None
        self._irq_ep1out: int | None = None
        self._pins: dict = {}
        self._doepint_w1c = DOEPINT_W1C_HS if self.has_dma else DOEPINT_W1C_FS
        self.hclk_min = float(p.get("hclk_min", 30e6 if self.has_dma else 14.2e6))   # 35.3.3 / 34.3.3 cautions
        self._inq: dict[int, list[int]] = {}          # IN endpoint -> lengths of complete packets in its TX FIFO
        self._in_part: dict[int, list[int]] = {}      # IN endpoint -> [packet length, words written so far]
        self._grst_busy: dict[int, float] = {}        # GRSTCTL self-clearing bit -> time (ns) it clears
        self._susp_ev = None                          # pending USBSUSP (3 ms after ESUSP)
        self._esusp = False
        self._ctrl_wlen = 0                           # wLength of the control transfer in progress
        self._trdt_key = None
        self.stats = {"setup": 0, "out": 0, "in": 0, "nak": 0, "stall": 0, "sof": 0, "resets": 0}
        regs = self._build_regs()
        super().__init__(name, base, size, regs, log=log, params=p)

    # ------------------------------------------------------------------ register list
    def _build_regs(self) -> list[Reg]:
        """Reset values and writable bits from the RM0090 register descriptions (34.16 OTG_FS, 35.12 OTG_HS), which
        differ from the SVD in GOTGCTL, GRSTCTL, CID, HPTXFSIZ, DIEPTXF1 and (HS) GUSBCFG / GRXFSIZ / GNPTXSTS."""
        R = Reg
        hs = self.has_dma
        out = [
            R("GOTGCTL", 0x000, ro_mask=~(0x00101EFE if self.otg2 else 0x00000E02), on_read=self._gotgctl_read,
              on_write=self._gotgctl_write),                                              # CIDSTS (live) = 1
            R("GOTGINT", 0x004, ro_mask=~0x000E0304, w1c_mask=0x000E0304, on_write=self._irq_write),
            R("GAHBCFG", 0x008, ro_mask=~(0x1BF if hs else 0x181), on_write=self._irq_write),
            R("GUSBCFG", 0x00C, reset=0x00001440 if hs else 0x00000A40,
              ro_mask=~((0xE3FEBF47 if hs else 0xE0003F47) & (0x7FFFFFFF if self.otg2 else 0xFFFFFFFF)),
              on_write=self._gusbcfg_write),                    # otg2: bit 31 reserved (RM0390 31.15.4)
            R("GRSTCTL", 0x010, reset=AHBIDL, ro_mask=~(0x7C0 | 0x37), on_write=self._grstctl_write,
              on_read=self._grstctl_read, on_peek=self._grstctl_peek),
            R("GINTSTS", 0x014, reset=0x04000020, ro_mask=GINTSTS_LIVE, w1c_mask=GINTSTS_W1C,
              on_read=self._gintsts_read, on_write=self._irq_write),
            R("GINTMSK", 0x018, ro_mask=0x00430301 if self.otg2 else 0x08C30301,    # otg2: LPMINTM, RSTDETM
              on_write=self._irq_write),
            R("GRXSTSR", 0x01C, ro_mask=0xFFFFFFFF, on_read=pure_read(lambda b, r: b._rx_status(pop=False))),
            R("GRXSTSP", 0x020, ro_mask=0xFFFFFFFF, on_read=lambda b, r: b._rx_status(pop=True), on_peek=lambda b, r: b._rx_status(pop=False)),
            R("GRXFSIZ", 0x024, reset=0x00000400 if hs else 0x00000200, ro_mask=0xFFFF0000,
              on_write=self._fifo_cfg_write),
            R("DIEPTXF0", 0x028, reset=0x00000200, on_write=self._fifo_cfg_write),
            R("HNPTXSTS", 0x02C, reset=0x00080400 if hs else 0x00080200, ro_mask=0xFFFFFFFF),
            R("GCCFG", 0x038, ro_mask=~(0x00210000 if self.otg2 else 0x003F0000 if hs else 0x003D0000),
              on_write=self._gccfg_write),                      # otg2: PWRDWN, VBDEN only (RM0390 31.15.15)
            R("CID", 0x03C, reset=(0x00002100 if hs else 0x00002000) if self.otg2 else
              (0x00001100 if hs else 0x00001200)),
            R("GSNPSID", 0x040, reset=GSNPSID, ro_mask=0xFFFFFFFF),
            R("HPTXFSIZ", 0x100, reset=0x02000800 if hs else 0x02000400),
        ]
        for n in range(1, self.n_eps):
            # DIEPTXF1 = 0x02000200 (34.16.2 text and Table 205 agree); DIEPTXF2/3: Table 205 and the SVD 0x02000400
            # (the text gives 0x02000200 for all x); OTG_HS: 0x02000400 (35.12.2)
            rst = 0x02000200 if (n == 1 and not hs) else 0x02000400
            out.append(R(f"DIEPTXF{n}", 0x100 + 4 * n, reset=rst, on_write=self._fifo_cfg_write))
        # host mode (storage)
        for nm, off, rst in (("HCFG", 0x400, 0), ("HFIR", 0x404, 0xEA60), ("HFNUM", 0x408, 0x3FFF),
                             ("HPTXSTS", 0x410, 0x00080100), ("HAINT", 0x414, 0), ("HAINTMSK", 0x418, 0),
                             ("HPRT", 0x440, 0)):
            out.append(R(nm, off, reset=rst))
        if self.otg2:                                       # RM0390 31.15.17 / 31.15.18
            out += [R("GLPMCFG", 0x054, ro_mask=~0x11FE1F83, on_write=self._glpmcfg_write),
                    R("GDFIFOCFG", 0x05C, reset=0x03EE0400 if hs else 0x03F40140)]
        for c in range(self.n_channels):
            b = 0x500 + 0x20 * c
            out += [R(f"HCCHAR{c}", b), R(f"HCINT{c}", b + 8), R(f"HCINTMSK{c}", b + 0xC), R(f"HCTSIZ{c}", b + 0x10)]
            if self.has_dma:
                out.append(R(f"HCDMA{c}", b + 0x14))
        out += [
            R("DCFG", 0x800, reset=0x02200000, ro_mask=~(0x03001FF7 if hs else 0x1FF7), on_write=self._dcfg_write),
            R("DCTL", 0x804, ro_mask=~0xFF3, on_write=self._dctl_write, on_read=self._dctl_read),
            R("DSTS", 0x808, reset=0x00000010, ro_mask=0xFFFFFFFF),
            R("DIEPMSK", 0x810, ro_mask=~(0x217F if hs else 0x207B), on_write=self._irq_write),
            R("DOEPMSK", 0x814, ro_mask=~(0x717F if hs else 0x313B), on_write=self._irq_write),
            R("DAINT", 0x818, ro_mask=0xFFFFFFFF, on_read=pure_read(lambda b, r: b._daint())),
            R("DAINTMSK", 0x81C, on_write=self._irq_write),
            R("DVBUSDIS", 0x828, reset=0x000017D7, ro_mask=0xFFFF0000),
            R("DVBUSPULSE", 0x82C, reset=0x000005B8, ro_mask=0xFFFFF000),
            R("DIEPEMPMSK", 0x834, ro_mask=0xFFFF0000, on_write=self._irq_write),
        ]
        if self.has_dma:
            out += [R("DTHRCTL", 0x830), R("DEACHINT", 0x838, w1c_mask=0x00030003, on_write=self._irq_write),
                    R("DEACHMSK", 0x83C, on_write=self._irq_write), R("DINEP1MSK", 0x844, on_write=self._irq_write),
                    R("DOUTEP1MSK", 0x884, on_write=self._irq_write)]
        for n in range(self.n_eps):
            b = 0x900 + 0x20 * n
            out += [R(f"DIEPCTL{n}", b, ro_mask=DPID | NAKSTS | (USBAEP if n == 0 else 0),
                      reset=USBAEP if n == 0 else 0, on_write=self._ctl_writer(n, True)),
                    R(f"DIEPINT{n}", b + 8, reset=TXFE, ro_mask=~DIEPINT_W1C, w1c_mask=DIEPINT_W1C,
                      on_read=self._diepint_reader(n), on_write=self._irq_write),
                    R(f"DIEPTSIZ{n}", b + 0x10, ro_mask=~(0x0018007F if n == 0 else 0x7FFFFFFF)),
                    R(f"DTXFSTS{n}", b + 0x18, ro_mask=0xFFFFFFFF, on_read=self._dtxfsts_reader(n))]
            if self.has_dma:
                out.append(R(f"DIEPDMA{n}", b + 0x14))
            b = 0xB00 + 0x20 * n
            out += [R(f"DOEPCTL{n}", b, ro_mask=DPID | NAKSTS | (USBAEP if n == 0 else 0),
                      reset=USBAEP if n == 0 else 0, on_write=self._ctl_writer(n, False)),
                    R(f"DOEPINT{n}", b + 8, reset=0x80, ro_mask=~self._doepint_w1c, w1c_mask=self._doepint_w1c,
                      on_write=self._irq_write),
                    R(f"DOEPTSIZ{n}", b + 0x10, ro_mask=~(0x6008007F if n == 0 else 0x7FFFFFFF))]
            if self.has_dma:
                out.append(R(f"DOEPDMA{n}", b + 0x14))
        out.append(R("PCGCCTL", 0xE00, ro_mask=~0x13))
        return out

    # ------------------------------------------------------------------ attach / reset
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.prefix)
        self._irq_ep1in = irq_number(chip, f"{self.prefix}_EP1_IN")
        self._irq_ep1out = irq_number(chip, f"{self.prefix}_EP1_OUT")
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._clock_changed)

    def reset(self) -> None:
        for ev in self._evs:
            self.chip.sched.cancel(ev)
        self._evs = []
        if self._mode_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._mode_ev)
        self._mode_ev = None
        self._cancel_susp()
        self._esusp = False
        self._inq.clear()
        self._in_part.clear()
        self._grst_busy.clear()
        self._ctrl_wlen = 0
        super().reset()
        self.ram[:] = [0] * self.fifo_words
        self.rx.config(0, 0)
        for t in self.tx:
            t.config(0, 0)
        self._cmod = 0
        self._suspended = False
        self._setup_pending = False
        self._b2b = 0
        self._addr_old = None
        self._in_pending.clear()
        self._gonak = None
        self._ginak = False
        self._fifo_cfg_write(self, None, 0, 0)
        if self.chip is not None:
            self._session = self._session_valid()
            self._update_irq()
        if self.bus is not None:
            self.bus.device_changed()

    # ------------------------------------------------------------------ helpers
    def _cancel_susp(self) -> None:
        if self._susp_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._susp_ev)
        self._susp_ev = None

    def _r(self, off: int) -> int:
        return int.from_bytes(self.mem[off:off + 4], "little")

    def _w(self, off: int, v: int) -> None:
        self.mem[off:off + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")

    def _set_bits(self, off: int, bits: int) -> None:
        self._w(off, self._r(off) | bits)

    def _clr_bits(self, off: int, bits: int) -> None:
        self._w(off, self._r(off) & ~bits)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    @staticmethod
    def _ictl(n: int) -> int:
        return 0x900 + 0x20 * n

    @staticmethod
    def _octl(n: int) -> int:
        return 0xB00 + 0x20 * n

    def _mps(self, n: int, is_in: bool) -> int:
        ctl = self._r(self._ictl(n) if is_in or n == 0 else self._octl(n))
        if n == 0:
            return EP0_MPS[self._r(self._ictl(0)) & 3]
        return ctl & 0x7FF

    def _dma(self) -> bool:
        return self.has_dma and bool(self._r(0x008) & DMAEN)

    # ------------------------------------------------------------------ live register values
    @pure_read
    def _gintsts_read(self, blk, reg) -> int:
        v = self._r(0x014) & ~GINTSTS_LIVE
        v |= self._cmod
        if self._r(0x004):
            v |= OTGINT
        if self.rx.n:
            v |= RXFLVL
        if self._ginak:
            v |= GINAKEFF
        if self._gonak == "effective":
            v |= GONAKEFF
        da = self._daint() & self._r(0x81C)
        if da & 0xFFFF:
            v |= IEPINT
        if da >> 16:
            v |= OEPINT
        v |= NPTXFE | PTXFE
        return v

    def _gotgctl_write(self, blk, reg, old, new):
        if self.otg2 and (old ^ new) & 0xFC:
            self._store(reg, new)
            self._after_cfg()                                # session-valid override changed
        return None

    def _glpmcfg_write(self, blk, reg, old, new):
        if new & 1 and not old & 1:
            self._warn_once("lpm", f"{self.name}: GLPMCFG.LPMEN = 1: LPM (L1 sleep) is not modelled; the USB host "
                            "model sends no LPM tokens, so the device never enters L1")
        return None

    @pure_read
    def _gotgctl_read(self, blk, reg) -> int:
        v = self._r(0x000) & ~(0x003F0000 if self.otg2 else 0x000F0000)
        v |= 1 << 16                                         # CIDSTS: B-device (ID pin floating / pulled up)
        if self.otg2:
            v |= 1 << 17                                     # DBCT (debounce done) as soon as the core is up
            if self._cmod:
                v |= 1 << 21                                 # CURMOD: host
        if self._session:
            v |= (1 << 18) if self._cmod else (1 << 19)      # ASVLD (host mode) / BSVLD (device mode)
        return v

    @pure_read
    def _dctl_read(self, blk, reg) -> int:
        v = self._r(0x804) & ~(GINSTS | GONSTS)
        if self._ginak:
            v |= GINSTS
        if self._gonak == "effective":
            v |= GONSTS
        return v

    def _txfe(self, n: int) -> bool:
        f = self.tx[self._txfnum(n)]
        if self._r(0x008) & TXFELVL:
            return f.n == 0
        return f.n <= f.depth // 2

    def _txfnum(self, n: int) -> int:
        return 0 if n == 0 else (self._r(self._ictl(n)) >> 22) & 0xF

    def _diepint_reader(self, n: int):
        def rd(blk, reg):
            v = self._r(self._ictl(n) + 8) & ~TXFE
            return v | (TXFE if self._txfe(n) else 0)
        rd.pure_read = True
        return rd

    def _dtxfsts_reader(self, n: int):
        def rd(blk, reg):
            return self.tx[self._txfnum(n)].free & 0xFFFF
        rd.pure_read = True
        return rd

    def _daint(self) -> int:
        v = 0
        dmsk, omsk, emp = self._r(0x810), self._r(0x814), self._r(0x834)
        for n in range(self.n_eps):
            iv = self._r(self._ictl(n) + 8) & ~TXFE
            m = dmsk
            if (emp >> n) & 1:
                m |= TXFE
                if self._txfe(n):
                    iv |= TXFE
            if iv & m:
                v |= 1 << n
            if self._r(self._octl(n) + 8) & omsk:
                v |= 1 << (16 + n)
        return v

    def _update_irq(self) -> None:
        if self.chip is None or self._irqn is None:
            return
        g = self._gintsts_read(self, None) & self._r(0x018)
        line = bool(self._r(0x008) & GINT) and g != 0
        if line != self._line:
            self._line = line
            self.irq(self._irqn, line)
        if self.has_dma and (self._irq_ep1in is not None or self._irq_ep1out is not None):
            ein = bool(self._r(0x900 + 0x20 + 8) & self._r(0x844)) and bool(self._r(0x83C) & 2)
            eout = bool(self._r(0xB00 + 0x20 + 8) & self._r(0x884)) and bool(self._r(0x83C) & (2 << 16))
            if (ein, eout) != self._ep1_lines:
                self._ep1_lines = (ein, eout)
                if self._irq_ep1in is not None:
                    self.irq(self._irq_ep1in, ein)
                if self._irq_ep1out is not None:
                    self.irq(self._irq_ep1out, eout)

    def _irq_write(self, blk, reg, old, new):
        self._update_irq()
        return None

    def _gint(self, bits: int) -> None:
        self._set_bits(0x014, bits)
        self._update_irq()

    # ------------------------------------------------------------------ fast path (FIFO windows, RAM)
    def _mode_mismatch(self, offset: int) -> bool:
        """Host register (0x400..0x7FF) in device mode or device register (0x800..0xDFF) in host mode: GINTSTS.MMIS,
        the access completes on the AHB but the core ignores it (RM0090 34.15 / 34.16)."""
        if (0x400 <= offset < 0x800 and not self._cmod) or (0x800 <= offset < 0xE00 and self._cmod):
            self._gint(MMIS)
            return True
        return False

    def peek_reg(self, reg):
        off = reg.offset
        if (0x400 <= off < 0x800 and not self._cmod) or (0x800 <= off < 0xE00 and self._cmod):
            return 0, True                               # host register in device mode (or the reverse): reads 0
        return super().peek_reg(reg)

    def read(self, offset: int, size: int) -> int:
        if 0x400 <= offset < 0xE00 and self._mode_mismatch(offset):
            self._gate_ok()
            return 0
        if 0x1000 <= offset < 0x20000:
            self._gate_ok()
            if not self.rx.n:
                self._warn_once("rxempty", f"{self.name}: read from the data FIFO while the RX FIFO is empty")
                return 0
            w = self.rx.pop()
            self._update_irq()
            return w & ((1 << (8 * size)) - 1)
        if 0x20000 <= offset < 0x40000:
            i = (offset - 0x20000) >> 2
            if i < self.fifo_words:
                return (self.ram[i] >> (8 * (offset & 3))) & ((1 << (8 * size)) - 1)
            return 0
        if offset < 0x1000:
            self._gate_ok()
        return super().read(offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if 0x1000 <= offset < 0x20000:
            self._gate_ok()
            n = (offset >> 12) - 1
            if n >= self.n_eps:
                self._warn_once(f"fifo{n}", f"{self.name}: write to the FIFO window of endpoint {n}, which "
                                f"this core does not have ({self.n_eps} endpoints)")
                return
            f = self.tx[self._txfnum(n)]
            if not f.push(value):
                self._warn_once(f"txfull{n}", f"{self.name}: TX FIFO {self._txfnum(n)} (IN endpoint {n}) "
                                f"overflow: word lost (depth {f.depth} words)")
            elif not self._dma():
                self._in_word(n)
            self._update_irq()
            return
        if 0x20000 <= offset < 0x40000:
            i = (offset - 0x20000) >> 2
            if i < self.fifo_words:
                sh = 8 * (offset & 3)
                m = ((1 << (8 * size)) - 1) << sh
                self.ram[i] = (self.ram[i] & ~m) | ((value << sh) & m)
            return
        if offset < 0x1000:
            self._gate_ok()
            if 0x400 <= offset < 0xE00 and self._mode_mismatch(offset):
                return
        super().write(offset, size, value)

    def _in_word(self, n: int) -> None:
        """A word pushed into IN endpoint n's TX FIFO.  The core counts packets as the application writes them:
        each complete packet (min(MPS, XFRSIZ) bytes) decrements DIEPTSIZ.XFRSIZ by its size (RM0090 34.17.6,
        "Generic non-periodic IN data transfers", internal data flow 3)."""
        part = self._in_part.get(n)
        ts_off = self._ictl(n) + 0x10
        xmask = 0x7F if n == 0 else 0x7FFFF
        if part is None:
            xfr = self._r(ts_off) & xmask
            if xfr == 0:
                self._warn_once(f"txnoxfr{n}", f"{self.name}: data written to the TX FIFO of IN endpoint {n} while "
                                f"DIEPTSIZ{n}.XFRSIZ = 0: not part of any packet")
                return
            part = self._in_part[n] = [min(self._mps(n, True), xfr), 0]
        part[1] += 1
        if part[1] * 4 >= part[0]:
            del self._in_part[n]
            self._inq.setdefault(n, []).append(part[0])
            ts = self._r(ts_off)
            self._w(ts_off, (ts & ~xmask) | max(0, (ts & xmask) - part[0]))

    # ------------------------------------------------------------------ register hooks
    def _grst_clocks_ns(self, n: int) -> float:
        """n clocks of the slower of the PHY (48 MHz) and AHB clocks."""
        r = rcc(self.chip) if self.chip is not None else None
        hclk = getattr(r, "core_hz", 0) if r is not None else 0
        f = min(48e6, hclk) if hclk else 48e6
        return n * 1e9 / f

    def _grstctl_peek(self, blk, reg) -> int:
        v = (self._r(0x010) & ~0x37) | AHBIDL
        now = self.chip.now_ns() if self.chip is not None else 0.0
        for bit, t in self._grst_busy.items():
            if now < t:
                v |= bit
        return v

    def _grstctl_read(self, blk, reg) -> int:
        v = (self._r(0x010) & ~0x37) | AHBIDL
        if self._grst_busy:
            now = self.chip.now_ns()
            for bit, t in list(self._grst_busy.items()):
                if now < t:
                    v |= bit
                else:
                    del self._grst_busy[bit]
        return v

    def _grstctl_write(self, blk, reg, old, new):
        """Self-clearing reset / flush bits (RM0090 34.16.2 GRSTCTL).  The effect is immediate; the bit reads 1
        until the core is done: RXFFLSH "requires 8 clocks (slowest of PHY or AHB clock)"; TXFFLSH and CSRST
        ("several clocks") are given the same 8 clocks.  HSRST and FCRST (host) clear at once."""
        busy = self._grst_clocks_ns(FLUSH_CLOCKS) if self.chip is not None else 0.0
        now = self.chip.now_ns() if self.chip is not None else 0.0
        if new & CSRST:
            self._soft_reset()
            self._grst_busy[CSRST] = now + busy
        if new & RXFFLSH:
            self.rx.flush()
            self._grst_busy[RXFFLSH] = now + busy
        if new & TXFFLSH:
            num = (new >> 6) & 0x1F
            for i, f in enumerate(self.tx):
                if num == 0x10 or i == num:
                    f.flush()
            for n in range(self.n_eps):
                if num == 0x10 or self._txfnum(n) == num:
                    self._in_pending.pop(n, None)
                    self._inq.pop(n, None)
                    self._in_part.pop(n, None)
            self._grst_busy[TXFFLSH] = now + busy
        self._update_irq()
        return (new & ~(CSRST | HSRST | FCRST | RXFFLSH | TXFFLSH)) | AHBIDL

    def _soft_reset(self) -> None:
        """GRSTCTL.CSRST (RM0090 34.16.2): "clears the interrupts and all the CSR register bits except" PCGCCTL
        (RSTPDMODL, GATEHCLK, PWRCLMP, STPPCLK), HCFG.FSLSPCS and DCFG.DSPD; all FIFOs are flushed and the state
        machines go idle.  GUSBCFG is kept too: the DWC2 force-mode bits survive a core soft reset (Linux dwc2
        driver, dwc2_core_reset) and PHYSEL is what the reset is for (the RM: "when you change the PHY ...")."""
        keep = {"GUSBCFG": self._r(0x00C), "PCGCCTL": self._r(0xE00), "HCFG": self._r(0x400) & 3}
        dspd = self._r(0x800) & 3
        for name, reg in self.regs.items():
            if name in ("GSNPSID", "GRXSTSR", "GRXSTSP"):
                continue
            self.set(name, reg.reset)
        for k, v in keep.items():
            self.set(k, v)
        self._w(0x800, (self._r(0x800) & ~3) | dspd)
        self._fifo_cfg_write(self, None, 0, 0)
        self.rx.flush()
        for f in self.tx:
            f.flush()
        self._in_pending.clear()
        self._inq.clear()
        self._in_part.clear()
        self._setup_pending = False
        self._gonak = None
        self._ginak = False
        self._suspended = False
        self._esusp = False
        self._cancel_susp()
        self._addr_old = None
        self._after_cfg()

    def _gusbcfg_write(self, blk, reg, old, new):
        if not self.has_dma:
            new |= PHYSEL                                    # OTG_FS: always the embedded FS PHY (RO 1)
        if self.has_dma and not (new & PHYSEL):
            self._warn_once("ulpi", f"{self.name}: ULPI (external high-speed PHY) selected: not modelled; the "
                            f"core only runs with its embedded full-speed PHY (GUSBCFG.PHYSEL = 1)")
        want = 1 if new & FHMOD else 0
        if new & (FHMOD | FDMOD) and want != self._cmod and self.chip is not None:
            if self._mode_ev is not None:
                self.chip.sched.cancel(self._mode_ev)
            self._mode_ev = self.chip.sched.in_ns(25e6, self._set_mode, want)   # forced mode after 25 ms
        self._after_cfg()
        return new

    def _set_mode(self, mode: int) -> None:
        self._mode_ev = None
        self._cmod = mode
        if mode:
            self._warn_once("host", f"{self.name}: host mode forced (GUSBCFG.FHMOD): the OTG host is not modelled "
                            f"(host registers are plain storage)")
        self._after_cfg()

    def _gccfg_write(self, blk, reg, old, new):
        self._after_cfg()
        return None

    def _fifo_cfg_write(self, blk, reg, old, new):
        rxd = self._r(0x024) & 0xFFFF
        self.rx.config(0, rxd)
        v = self._r(0x028)
        self.tx[0].config(v & 0xFFFF, v >> 16)
        for n in range(1, self.n_eps):
            v = self._r(0x100 + 4 * n)
            self.tx[n].config(v & 0xFFFF, v >> 16)
        return None

    def _check_fifos(self, fifos: tuple[int, ...] = (0,)) -> None:
        """Warn (once per FIFO) when a FIFO in use runs past the FIFO RAM or overlaps the RX FIFO / another FIFO in
        use (unused FIFOs keep their reset sizes, which exceed the RAM: harmless, as on silicon)."""
        used = {0} | {self._txfnum(n) for n in range(1, self.n_eps) if self._r(self._ictl(n)) & USBAEP}
        spans = {"RX": (0, self.rx.depth)} | {f"TX{k}": (self.tx[k].start, self.tx[k].depth) for k in used}
        for k in fifos:
            name = f"TX{k}"
            s0, d0 = spans.get(name, (self.tx[k].start, self.tx[k].depth))
            if s0 + d0 > self.fifo_words:
                self._warn_once(f"fiforam{k}", f"{self.name}: TX FIFO {k} (words {s0}..{s0 + d0 - 1}) runs past the "
                                f"{self.fifo_words}-word FIFO RAM: it wraps onto other FIFOs")
            for other, (s1, d1) in spans.items():
                if other != name and d0 and d1 and s0 < s1 + d1 and s1 < s0 + d0:
                    self._warn_once(f"fifolap{k}{other}", f"{self.name}: TX FIFO {k} (words {s0}..{s0 + d0 - 1}) "
                                    f"overlaps {other} FIFO (words {s1}..{s1 + d1 - 1}): their data corrupt each other")
        if self.rx.depth > self.fifo_words:
            self._warn_once("fiforamrx", f"{self.name}: RX FIFO depth {self.rx.depth} words exceeds the "
                            f"{self.fifo_words}-word FIFO RAM")

    def _dcfg_write(self, blk, reg, old, new):
        if (old ^ new) & 0x7F0:
            if self._addr_old is None:
                self._addr_old = (old >> 4) & 0x7F
        return None

    def _dctl_write(self, blk, reg, old, new):
        if new & SGINAK:
            self._ginak = True
        if new & CGINAK:
            self._ginak = False
        if new & SGONAK and self._gonak is None:
            self._gonak = "pending"
            if not self._dma():
                self.rx.push(ST_GONAK << 17)
            else:
                self._gonak = "effective"
        if new & CGONAK:
            self._gonak = None
        if (new & RWUSIG) and not (old & RWUSIG) and self._suspended:
            self._clr_bits(0x808, SUSPSTS)          # DSTS.SUSPSTS: cleared by the RWUSIG write (34.16.4 DSTS)
            if self.bus is not None:
                self.bus.remote_wakeup()
        if (old ^ new) & SDIS:
            self._after_cfg()
        self._update_irq()
        return new & ~(SGINAK | CGINAK | SGONAK | CGONAK)

    def _ctl_writer(self, n: int, is_in: bool):
        rw = (DIEPCTL0_RW if is_in else DOEPCTL0_RW) if n == 0 else (DIEPCTLX_RW if is_in else DOEPCTLX_RW)

        def wr(blk, reg, old, new):
            off = self._ictl(n) if is_in else self._octl(n)
            ioff = off + 8
            v = (new & rw) | (old & (DPID | NAKSTS))          # reserved bits read 0 (34.16.4)
            v |= old & EPENA                                  # EPENA: cleared by the core only
            if n == 0:
                v |= USBAEP
                if old & STALL:
                    v |= STALL                                # EP0 STALL: cleared by the next SETUP only
                if not is_in:
                    v = (v & ~3) | (self._r(self._ictl(0)) & 3)   # DOEPCTL0.MPSIZ mirrors DIEPCTL0
            if is_in and new & EPENA and not old & EPENA:
                self._check_fifos((self._txfnum(n),))
            if new & CNAK:
                v &= ~NAKSTS
                if is_in:
                    self._clr_bits(ioff, INEPNE)              # "cleared ... by writing to the CNAK bit" (DIEPINTx)
            if new & SNAK:
                v |= NAKSTS
                if is_in:
                    self._set_bits(ioff, INEPNE)
            if n and new & SD0PID:
                v &= ~DPID
            if n and new & SD1PID:
                v |= DPID
            if new & EPDIS and (n or is_in):                  # "The application cannot disable control OUT EP 0"
                if old & EPENA:
                    v &= ~(EPENA | EPDIS)
                    self._set_bits(ioff, EPDISD)
                    if is_in:
                        self._in_pending.pop(n, None)
                else:
                    v &= ~EPDIS
            self._w(off, v)
            self._update_irq()
            return v
        return wr

    # ------------------------------------------------------------------ RX FIFO
    def _rx_status(self, pop: bool) -> int:
        if not self.rx.n:
            return 0
        w = self.rx.peek(0)
        if not pop:
            return w
        self.rx.pop()
        st = (w >> 17) & 0xF
        ep = w & 0xF
        if st == ST_OUT and ep < self.n_eps:
            # XFRSIZ counts down as packets are read out of the RX FIFO (34.16.4 DOEPTSIZx, 34.17.6 step 5)
            ts_off = self._octl(ep) + 0x10
            xmask = 0x7F if ep == 0 else 0x7FFFF
            ts = self._r(ts_off)
            self._w(ts_off, (ts & ~xmask) | max(0, (ts & xmask) - ((w >> 4) & 0x7FF)))
        elif st == ST_OUT_DONE and ep < self.n_eps:
            # "When the application pops this entry, a transfer completed interrupt is generated for the endpoint
            # and the endpoint enable is cleared" (34.17.6 step 7)
            self._clr_bits(self._octl(ep), EPENA)
            self._set_bits(self._octl(ep) + 8, XFRC)
        elif st == ST_SETUP_DONE:
            # STUP on popping the setup-stage-done word; "the core clears the endpoint enable bit for control OUT
            # endpoints" (34.17.6 "SETUP transactions", internal data flow 5)
            self._clr_bits(self._octl(0), EPENA)
            self._set_bits(self._octl(0) + 8, STUP | (B2BSTUP if self._b2b > 3 else 0))
            self._b2b = 0
        elif st == ST_GONAK and self._gonak == "pending":
            self._gonak = "effective"
        self._update_irq()
        return w

    def _rx_entry(self, st: int, ep: int, data: bytes = b"", dpid: int = 0, reserve: int = 0) -> bool:
        words = _words(data)
        if self.rx.free < 1 + len(words) + reserve:
            return False
        self.rx.push((st << 17) | ((dpid & 3) << 15) | ((len(data) & 0x7FF) << 4) | (ep & 0xF))
        for w in words:
            self.rx.push(w)
        return True

    # ------------------------------------------------------------------ clocks / connection
    def _clock_changed(self) -> None:
        if self.chip is not None and self._r(0x038) & PWRDWN:
            self._clock_ok()

    def _clk48(self) -> float:
        r = rcc(self.chip)
        if r is None or not hasattr(r, "pll_q_hz"):
            return float(self.params.get("clk48_hz", 48e6))
        g = getattr(r, "clk48_hz", None)                 # STM32F446 DCKCFGR2.CK48MSEL (stm32.rcc_f446)
        if g is not None:
            return float(g())
        return r.pll_q_hz() if r.get("CR") & (1 << 25) else 0.0

    def _clock_ok(self) -> bool:
        f = self._clk48()
        if abs(f - 48e6) > USB_TOL * 48e6:
            if self._r(0x038) & PWRDWN:
                what = "stopped (main PLL off)" if f <= 0 else f"{fmt_hz(f)}"
                self._warn_once(f"clk{round(f)}", f"{self.name}: the USB PHY clock PLL48CLK is {what}: it must be "
                                f"48 MHz +-0.25 % (RCC_PLLCFGR PLLQ); the core does not answer the host")
            return False
        r = rcc(self.chip)
        hclk = getattr(r, "core_hz", None) if r is not None else None
        if hclk is not None and hclk < self.hclk_min:
            self._warn_once("hclk", f"{self.name}: HCLK {fmt_hz(hclk)} is below the {fmt_hz(self.hclk_min)} the "
                            f"{self.prefix} core needs (RM0090 {'35.3.3' if self.has_dma else '34.3.3'})")
            return False
        if hclk is not None:
            self._check_trdt(hclk)
        return True

    def _check_trdt(self, hclk: float) -> None:
        """GUSBCFG.TRDT below the minimum of Table 203 (OTG_FS) / Table 213 (OTG_HS) for this AHB frequency: one
        `warn` (silicon may underrun the TX FIFO on IN tokens; the data are still sent correctly here)."""
        trdt = (self._r(0x00C) >> 10) & 0xF
        key = (round(hclk), trdt)
        if key == self._trdt_key:
            return
        self._trdt_key = key
        need = next((t for mhz, t in (TRDT_HS if self.has_dma else TRDT_FS) if hclk >= mhz * 1e6), None)
        if need is not None and trdt < need:
            self._warn_once(f"trdt{key}", f"{self.name}: GUSBCFG.TRDT = {trdt} is below the minimum {need} for "
                            f"HCLK {fmt_hz(hclk)} (RM0090 Table {213 if self.has_dma else 203})")

    def _vbus_pin(self) -> Any:
        pb = getattr(self.chip, "pins", None)
        if pb is None:
            return None
        pins = pb.signal_pins(f"{self.prefix}_VBUS")
        return pins[0] if pins else None

    def _vbus_present(self) -> bool:
        pin = self._vbus_pin()
        if pin is not None:
            return pin.external in ("H", 1, True)
        return bool(self.bus is not None and self.bus.vbus)

    def _session_valid(self) -> bool:
        g = self._r(0x038)
        if self.otg2:
            # RM0390 31.15.1 / 31.15.15: BVALOEN overrides B-valid with BVALOVAL; otherwise VBDEN enables the VBUS
            # comparators (no sensing: no valid session)
            o = self._r(0x000)
            if o & (1 << 6):
                return bool(o & (1 << 7))
            return bool(g & (1 << 21)) and self._vbus_present()
        if g & NOVBUSSENS:
            return True
        if g & (VBUSBSEN | VBUSASEN):
            return self._vbus_present()
        return False

    def _pins_ok(self) -> bool:
        if self.chip is None:
            return True
        for s in ("DP", "DM"):
            why = pin_reason(self.chip, f"{self.prefix}_{s}", self._pins)
            if why:
                self._warn_once("pins", f"{self.name}: {self.prefix}_DP/DM pins are not muxed to the OTG function "
                                f"(GPIO alternate function): the device is not connected to the bus")
                return False
        return True

    def usb_pullup(self, bus) -> str | None:
        if self._cmod or not self._session or self._r(0x804) & SDIS or not self._r(0x038) & PWRDWN:
            return None
        if self.has_dma and not (self._r(0x00C) & PHYSEL):
            return None
        if not self._pins_ok():
            return None
        return "fs"

    def _after_cfg(self) -> None:
        if self.chip is None:
            return
        sess = self._session_valid()
        if sess != self._session:
            self._session = sess
            g = self._r(0x038)
            sensing = (g & (1 << 21) and not self._r(0x000) & (1 << 6)) if self.otg2 else \
                (g & (VBUSBSEN | VBUSASEN) and not g & NOVBUSSENS)
            if sensing and not self._cmod:
                if sess:
                    self._set_bits(0x014, SRQINT)
                else:
                    self._set_bits(0x004, SEDET)
        if self.bus is not None:
            self.bus.device_changed()
        self._update_irq()

    def _ready(self) -> bool:
        """The core can take part in bus traffic now."""
        if self.chip is None or self._cmod or not self._session or not self._r(0x038) & PWRDWN:
            return False
        if getattr(self, "lp_frozen", False):
            return False                 # Stop: no clock, tokens time out (resume K still raises the EXTI wake-up)
        if self._rcc_gate_off():
            self._warn_once("gate", f"{self.name}: bus traffic while the {self.gate} clock (RCC) is off: ignored")
            return False
        return self._clock_ok()

    def _rcc_gate_off(self) -> bool:
        loc = self._gate_loc
        return loc is not None and not (self._rcc.mem[loc[0]] & loc[1])

    def _addr_match(self, addr: int) -> bool:
        dad = (self._r(0x800) >> 4) & 0x7F
        return addr == dad or (self._addr_old is not None and addr == self._addr_old)

    # ------------------------------------------------------------------ usbbus device protocol
    def usb_attach(self, bus) -> None:
        self.bus = bus

    def usb_detach(self) -> None:
        self.bus = None

    def usb_vbus(self, bus, on: bool, t: float) -> None:
        pin = self._vbus_pin()
        if pin is not None:
            self.chip.pins.set_external(pin.name, "H" if on else None)
        self._after_cfg()

    def usb_reset(self, bus, active: bool, t: float) -> None:
        if self._cmod:
            return
        self._cancel_susp()
        self._esusp = False
        if active:
            # The core only flags the reset and deactivates endpoints 1..n (USBAEP, 34.16.4 DIEPCTLx/DOEPCTLx);
            # NAKing the OUT endpoints is the application's job (34.17.5 "Endpoint initialization on USB reset").
            self.stats["resets"] += 1
            self._suspended = False
            self._clr_bits(0x808, SUSPSTS)
            for n in range(1, self.n_eps):
                self._clr_bits(self._octl(n), USBAEP)
                self._clr_bits(self._ictl(n), USBAEP)
            self._setup_pending = False
            self._addr_old = None
            self._gint(USBRST)
        else:
            self._w(0x808, (self._r(0x808) & ~0x6) | (3 << 1))   # ENUMSPD: full speed, 48 MHz PHY clock
            self._check_fifos()
            self._gint(ENUMDNE)

    def usb_suspend(self, bus, t: float) -> None:
        """3 ms of bus idle: ESUSP; "confirmed 3 ms later, if appropriate, by the suspend interrupt (USBSUSP)", with
        DSTS.SUSPSTS set then (RM0090 34.5.2 "Suspended state")."""
        self._esusp = True
        self._gint(ESUSP)
        self._cancel_susp()
        if self.chip is not None:
            self._susp_ev = self.chip.sched.in_ns(3e6, self._enter_suspend)

    def _enter_suspend(self) -> None:
        self._susp_ev = None
        if not self._esusp:
            return
        self._esusp = False
        self._suspended = True
        self._set_bits(0x808, SUSPSTS)
        self._gint(USBSUSP)

    def _bus_activity(self) -> None:
        """Traffic during early suspend: the idle bus was not a suspend after all."""
        if self._esusp:
            self._esusp = False
            self._cancel_susp()

    def usb_resume(self, bus, t: float) -> None:
        if self._esusp:
            self._bus_activity()
            return
        if not self._suspended:
            return
        self._suspended = False
        self._clr_bits(0x808, SUSPSTS)
        self._gint(WKUINT)
        if self.exti_line is not None:
            exti_input(self.chip, self.exti_line, True)
            exti_input(self.chip, self.exti_line, False)

    def usb_sof(self, bus, frame: int, t: float) -> None:
        self._bus_activity()
        self.stats["sof"] += 1
        self._w(0x808, (self._r(0x808) & ~(0x3FFF << 8)) | ((frame & 0x3FFF) << 8))
        if self._r(0x018) & SOF or not (self._r(0x014) & SOF):
            self._gint(SOF)
        if self._r(0x038) & SOFOUTEN:
            pb = self.chip.pins
            sig = f"{self.prefix}_SOF"
            if any(pb.signal_matches(p.owner, sig) for p in pb.signal_pins(sig)):
                pb.drive_signal(sig, "H")
                r = rcc(self.chip)
                hclk = getattr(r, "core_hz", 0) if r is not None else 0
                width = SOF_PULSE_HCLK * 1e9 / hclk if hclk else 1e3     # 20 HCLK cycles (34.7.2)
                self._evs.append(self.chip.sched.in_ns(width, pb.drive_signal, sig, "L"))
                if len(self._evs) > 8:
                    self._evs = [e for e in self._evs if not e.cancelled]

    def _setup_done(self) -> None:
        """An IN/OUT token on EP0 after a SETUP: "when the Setup stage changes to a Data IN/OUT stage, the core writes
        an entry (Setup stage done word)" (34.17.6 "SETUP transactions", internal data flow 3)."""
        if not self._setup_pending:
            return
        self._setup_pending = False
        if self._dma():
            self._clr_bits(self._octl(0), EPENA)
            self._set_bits(self._octl(0) + 8, STUP)
            self._update_irq()
        else:
            if not self.rx.push((ST_SETUP_DONE << 17) | 0):
                self._warn_once("setupfull", f"{self.name}: SETUP stage done word lost: RX FIFO full")
            self._update_irq()

    def _nak(self, intoff: int) -> str:
        """A NAK handshake on an endpoint: DIEPINTx / DOEPINTx.NAK (bit 13, "generated when a NAK is transmitted")."""
        self.stats["nak"] += 1
        self._set_bits(intoff, EPNAK)
        self._update_irq()
        return "NAK"

    def usb_setup(self, bus, x) -> str | None:
        if not self._ready() or x.ep >= self.n_eps or not self._addr_match(x.addr):
            return None
        if x.ep != 0:
            return None
        self._bus_activity()
        self.stats["setup"] += 1
        if self._dma():
            dst = self._r(self._octl(0) + 0x14)
            for i, b in enumerate(x.data[:8]):
                self.chip.bus.write(dst + i, 1, b)
            self._w(self._octl(0) + 0x14, dst + 8)
        elif not self._rx_entry(ST_SETUP, 0, x.data[:8], dpid=0):
            # written "without checking for available space" into the 10 words the core keeps for SETUPs
            # (34.17.6); a FIFO that is really full (entries never popped) loses it
            self._warn_once("setupfull", f"{self.name}: SETUP packet lost: no room in the RX FIFO (GRXFSIZ too "
                            f"small or entries not popped)")
            return "ACK"
        ts = self._r(self._octl(0) + 0x10)
        cnt = (ts >> 29) & 3
        ts = (ts & ~(3 << 29)) | (max(cnt - 1, 0) << 29)
        self._w(self._octl(0) + 0x10, ts)
        self._b2b = self._b2b + 1 if self._setup_pending else 1
        # "The core internally sets the IN NAK and OUT NAK bits for the control IN/OUT endpoints on which the SETUP
        # packet was received" (34.17.6); STALL is cleared by a SETUP (DIEPCTL0/DOEPCTL0.STALL)
        for off in (self._ictl(0), self._octl(0)):
            self._w(off, (self._r(off) & ~STALL) | NAKSTS)
        self._set_bits(self._ictl(0) + 8, INEPNE)      # NAK set "by the core" has taken effect (DIEPINTx.INEPNE)
        self._in_pending.pop(0, None)
        self._ep0_in_tog = 1
        self._ep0_out_tog = 1
        self._ctrl_in = bool(x.data[0] & 0x80)
        self._ctrl_wlen = int.from_bytes(bytes(x.data[6:8]), "little") if len(x.data) >= 8 else 0
        self._setup_pending = True
        self._update_irq()
        return "ACK"

    def usb_out(self, bus, x) -> str | None:
        if not self._ready() or x.ep >= self.n_eps or not self._addr_match(x.addr):
            return None
        self._bus_activity()
        n = x.ep
        o = self._octl(n)
        ctl = self._r(o)
        if n == 0:
            self._setup_done()
            if self._ctrl_in and x.data and self._r(0x800) & 4:
                # status OUT of a control read with data and DCFG.NZLSOHSK: STALL, packet not delivered (34.16.4)
                self.stats["stall"] += 1
                return "STALL"
        elif not ctl & USBAEP:
            return None
        if ctl & STALL:
            self.stats["stall"] += 1
            return "STALL"
        if not ctl & EPENA:
            if n == 0:
                self._set_bits(o + 8, OTEPDIS)
            return self._nak(o + 8)
        if ctl & NAKSTS or self._gonak is not None:
            return self._nak(o + 8)
        data = x.data
        # data toggle
        if n == 0:
            want = self._ep0_out_tog
        else:
            want = 1 if ctl & DPID else 0
        got = 1 if x.pid == "DATA1" else 0
        if ((ctl >> 18) & 3) != 1 and got != want:              # not isochronous: duplicate -> ACK and drop
            return "ACK"
        mps = self._mps(n, False)
        ts = self._r(o + 0x10)
        xfr_mask = 0x7F if n == 0 else 0x7FFFF
        if self._dma():
            dst = self._r(o + 0x14)
            for i, b in enumerate(data):
                self.chip.bus.write(dst + i, 1, b)
            self._w(o + 0x14, dst + len(data))
            ts = (ts & ~xfr_mask) | max(0, (ts & xfr_mask) - len(data))
        elif not self._rx_entry(ST_OUT, n, data, dpid=2 if got else 0, reserve=SETUP_RESERVE):
            # "If there is no space in the receive FIFO ... non-isochronous OUT tokens receive a NAK" (34.17.6);
            # the 10 SETUP words are never used for data
            return self._nak(o + 8)
        self.stats["out"] += 1
        if n == 0:
            self._ep0_out_tog ^= 1
        else:
            self._w(o, ctl ^ DPID)
        pk_mask = 1 if n == 0 else 0x3FF
        pk = max(0, ((ts >> 19) & pk_mask) - 1)                  # PKTCNT: per packet written to the RX FIFO
        ts = (ts & ~(pk_mask << 19)) | (pk << 19)
        self._w(o + 0x10, ts)
        if pk == 0 or len(data) < mps:
            # transfer done on the USB side: the core sets the endpoint NAK (34.17.6 OUT internal data flow 3)
            self._set_bits(o, NAKSTS)
            if self._dma():
                self._clr_bits(o, EPENA)
                self._set_bits(o + 8, XFRC)
            else:
                self._rx_entry(ST_OUT_DONE, n)
        self._update_irq()
        return "ACK"

    def usb_in(self, bus, x):
        if not self._ready() or x.ep >= self.n_eps or not self._addr_match(x.addr):
            return None
        self._bus_activity()
        n = x.ep
        i = self._ictl(n)
        ctl = self._r(i)
        if n == 0:
            self._setup_done()
            if not self._ctrl_in and self._ctrl_wlen and not self.has_dma:
                # IN token of the status stage of a control write: DOEPINT0.STSPHSRX (34.16.4, OTG_FS only)
                self._set_bits(self._octl(0) + 8, STSPHSRX)
        elif not ctl & USBAEP:
            return None
        if ctl & STALL:
            self.stats["stall"] += 1
            return "STALL"
        if ctl & NAKSTS or self._ginak:
            return self._nak(i + 8)
        ts = self._r(i + 0x10)
        pk = (ts >> 19) & (3 if n == 0 else 0x3FF)
        xfr = ts & (0x7F if n == 0 else 0x7FFFF)
        if self._dma():
            if not ctl & EPENA or pk == 0:
                return self._nak(i + 8)
            ln = min(self._mps(n, True), xfr)
            src = self._r(i + 0x14)
            if 0x08000000 <= src < 0x08100000 or 0x1FFF0000 <= src < 0x1FFF7A20:
                self._warn_once(f"dmaflash{n}", f"{self.name}: IN endpoint {n} DMA address 0x{src:08X} is in Flash: "
                                f"the USB DMA cannot address the internal Flash memory (RM0090 Figure 410): "
                                f"zeros sent")
                data = bytes(ln)
            else:
                data = bytes(self.chip.bus.read(src + k, 1) for k in range(ln))
            words = 0
        else:
            q = self._inq.get(n)
            f = self.tx[self._txfnum(n)]
            if ctl & EPENA and pk and q and f.n >= (q[0] + 3) // 4:
                ln = q[0]
                words = (ln + 3) // 4
                data = b"".join(f.peek(k).to_bytes(4, "little") for k in range(words))[:ln]
            elif ctl & EPENA and pk and not q and n not in self._in_part and xfr == 0:
                ln, words, data = 0, 0, b""                     # zero-length packet (internal ZLP flag)
            else:
                # no complete packet in the TX FIFO: ITTXFE ("provided that the endpoint NAK bit is not set"), NAK
                self._set_bits(i + 8, ITTXFE)
                return self._nak(i + 8)
        tog = self._ep0_in_tog if n == 0 else (1 if ctl & DPID else 0)
        self._in_pending[n] = (ln, words, data)
        return ("DATA1" if tog else "DATA0", data)

    def usb_in_done(self, bus, x, acked: bool, t: float) -> None:
        n = x.ep
        p = self._in_pending.pop(n, None)
        if p is None or not acked:
            return                                     # timeout: PKTCNT not decremented, the FIFO rewinds
        ln, words, _data = p
        i = self._ictl(n)
        self.stats["in"] += 1
        ts = self._r(i + 0x10)
        xfr_mask = 0x7F if n == 0 else 0x7FFFF
        pk_mask = 3 if n == 0 else 0x3FF
        if self._dma():
            self._w(i + 0x14, self._r(i + 0x14) + ln)
            ts = (ts & ~xfr_mask) | max(0, (ts & xfr_mask) - ln)
        else:
            f = self.tx[self._txfnum(n)]
            for _ in range(words):
                f.pop()
            q = self._inq.get(n)
            if q and words:
                q.pop(0)
        if n == 0:
            self._ep0_in_tog ^= 1
            if self._addr_old is not None and ln == 0:
                self._addr_old = None                  # status stage of SET_ADDRESS done: new address only
        else:
            self._w(i, self._r(i) ^ DPID)
        pk = max(0, ((ts >> 19) & pk_mask) - 1)        # PKTCNT: decremented on the host's ACK (34.17.6)
        ts = (ts & ~(pk_mask << 19)) | (pk << 19)
        self._w(i + 0x10, ts)
        if pk == 0 and ts & xfr_mask == 0:
            # "When the transfer size is 0 and the packet count is 0, the transfer complete (XFRC) interrupt for the
            # endpoint is generated and the endpoint enable is cleared"
            self._w(i, self._r(i) & ~EPENA)
            self._set_bits(i + 8, XFRC)
        self._update_irq()

    # ------------------------------------------------------------------ status
    def usb_stats(self) -> dict:
        return dict(self.stats, address=(self._r(0x800) >> 4) & 0x7F, suspended=self._suspended,
                    connected=bool(self.bus is not None and self.bus.state != "detached"))


@register("stm32.otg_hs")
class OtgHsCore(OtgCore):
    """OTG_HS core (6 endpoints, 4 KB FIFO RAM, internal DMA, dedicated EP1 interrupts) on its embedded FS PHY."""
    DEFAULTS = {"endpoints": 6, "fifo_words": 1024, "dma": True, "prefix": "OTG_HS", "gate": "OTG_HS",
                "exti_line": 20}
