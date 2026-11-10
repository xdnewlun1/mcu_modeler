"""STM32F4 SPI (SPI1-3, RM0090 ch. 28), SPI mode, bit-level on the pins.

Registers from the SVD (CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR, I2SPR); behaviour hand-written.

* Clock: SPI1 on APB2, SPI2/SPI3 on APB1 (`params.apb`). Baud = PCLK / 2^(BR+1): one SCK half period is 2^BR PCLK
  cycles = 2^BR x PPRE core cycles, so every edge lands on an exact core cycle (`sched.at_cycles`).
* Double buffering (28.3.7): DR writes go to the TX buffer (SR.TXE = 0). A master with SPE = 1 and an idle shift
  register moves the buffer into the shift register at once (TXE = 1 again) and starts the frame; the next buffered
  word follows back to back. A frame is 8 or 16 bits (CR1.DFF), MSB or LSB first (LSBFIRST) and lasts
  bits x 2^(BR+1) PCLK cycles. When it ends the received word goes to the RX buffer (SR.RXNE = 1); if RXNE is still
  set the new word is lost and SR.OVR is set (cleared by a DR read followed by an SR read). Reading DR clears RXNE.
  SR.BSY is set while a frame shifts or a master has a word waiting.
* Pins (master, MSTR = 1, SPE = 1): SCK idles at CPOL and toggles at every half period; MOSI changes before the
  first edge (CPHA = 0) or on each leading edge (CPHA = 1); MISO is sampled on the other edge from the pin muxed to
  SPIx_MISO (H = 1, L or floating X = 0, like IDR), so another chip or a board net can answer. Outputs go through
  PinBoard.drive_signal; the GPIO model asks `signal_state()` for the live direction (master: SCK/MOSI out, MISO
  in; slave: SCK/MOSI/NSS in, MISO out while selected). With SSM = 0 and CR2.SSOE = 1 the master drives NSS low while
  SPE = 1 (high again when SPE clears). SCK/MOSI/NSS are not driven while SPE = 0 (the pin floats unless pulled).
* Responders: `responder(tx, pcs, bits) -> rx` (the SPI NOR flash of chipem/devices/spi_flash.py, `--spi-flash
  SPI1:cs=PA4`, or any Python callable) answers a master frame instead of the MISO pin; it is called when the frame
  starts. `frame_end(cont, eoq)` is called after every frame with cont=True and with cont=False when SPE clears (a
  flash attached without a chip-select pin ends its command there). A frame is only seen by the device when the
  SCK and MOSI pins are muxed (the received word is 0 when MISO is not muxed); otherwise the master still clocks
  and sets its flags (one `warn`).
* Slave (MSTR = 0): the model listens to the pins muxed to SPIx_SCK / SPIx_MOSI / SPIx_NSS. Selected when NSS is
  low (SSM = 0; NSS pin not muxed = not selected) or SSI = 0 (SSM = 1). Bits shift on SCK pin edges per CPOL/CPHA;
  MISO is driven while selected and SPE = 1. The TX buffer is loaded into the shift register when a frame starts (the
  first leading edge, or NSS falling / the previous frame's last trailing edge with CPHA = 0); with an empty TX buffer
  the slave sends the previous frame again (UDR is I2S-only on the F4). Deselecting mid-frame drops the partial word.
* Modes: RXONLY = 1 and BIDIMODE = 1 / BIDIOE = 0 make a master clock continuously while SPE = 1 (receive only;
  BIDIMODE receives on the MOSI pin); BIDIOE = 1 is transmit only (received data not stored, RXNE/OVR unchanged).
* MODF (28.4.8): a master whose NSS input goes low (SSM = 0, SSOE = 0, NSS pin L) or with SSM = 1 and SSI = 0 sets
  SR.MODF and clears SPE and MSTR; MODF clears with an SR access followed by a CR1 write.
* CRC (28.3.6): with CRCEN the TXCRCR / RXCRCR registers accumulate a CRC (polynomial CRCPR, 8 or 16 bits per DFF,
  MSB first over the data word, initial value 0) of every transmitted / received word. CRCNEXT set before the last
  frame ends makes the next frame transmit TXCRCR; the word received in that frame is compared with RXCRCR (taken
  before it) and SR.CRCERR (rc_w0) is set on a mismatch. Writing CRCEN clears both CRC registers.
* IRQ "SPIx" (level): (TXE & TXEIE) | (RXNE & RXNEIE) | ((OVR | MODF | CRCERR) & ERRIE).
* DMA (28.3.9): CR2.TXDMAEN requests "SPIx_TX" while TXE = 1, RXDMAEN requests "SPIx_RX" while RXNE = 1 (RM0090
  Tables 42/43, through `_util.dma_request`).
Every frame is recorded in `frames` (tx, rx) and the transmitted words in `tx_bytes` (bytes; 16-bit words big
endian; not `output`, which marks UART-like blocks for Session.uart_output).
I2S mode (I2SCFGR.I2SMOD = 1) of SPI2/SPI3: chipem/periph/stm32/i2s.py (`I2SEngine`; SPI1 has no I2S mode on the F4:
setting I2SMOD there logs one `warn` and the block stays idle); the I2S2ext/I2S3ext blocks are `stm32.i2s_ext` below.
The IRQ line goes through `_util.shared_irq` (SPIx and I2SxEXT share it).
Not modelled: TI frame format
(CR2.FRF: one `warn`, Motorola timing is used), slave BIDIMODE, the NSS pulse / inter-frame gaps of later families,
pin speed / slew. The Kinetis DSPI (kinetis.dspi) works the same way (boards/k22_stm32_spi.yaml).
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, dma_request, gpio_resync, irq_number, shared_irq
from .i2s import FRE, I2SE, UDR, I2SEngine, block_index

CPHA, CPOL, MSTR, SPE, LSBFIRST, SSI, SSM, RXONLY = 1, 2, 4, 0x40, 0x80, 0x100, 0x200, 0x400
DFF, CRCNEXT, CRCEN, BIDIOE, BIDIMODE = 0x800, 0x1000, 0x2000, 0x4000, 0x8000
RXDMAEN, TXDMAEN, SSOE, FRF, ERRIE, RXNEIE, TXEIE = 1, 2, 4, 0x10, 0x20, 0x40, 0x80
RXNE, TXE, CRCERR, MODF, OVR, BSY = 1, 2, 0x10, 0x20, 0x40, 0x80
I2SMOD = 1 << 11
APB2_SPIS = ("SPI1", "SPI4", "SPI5", "SPI6")


def crc_update(crc: int, data: int, bits: int, poly: int) -> int:
    """CRC register step over one `bits`-wide word, MSB first (RM0090 28.3.6)."""
    top = 1 << (bits - 1)
    mask = (1 << bits) - 1
    for i in range(bits - 1, -1, -1):
        fb = bool(crc & top) ^ bool(data >> i & 1)
        crc = (crc << 1) & mask
        if fb:
            crc ^= poly & mask
    return crc


@register("stm32.spi")
class SPI(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = name
        self.apb = int(params.get("apb", 2 if name in APB2_SPIS else 1))
        ov = {"CR1": {"on_write": self._cr1_write},
              "CR2": {"on_write": self._cr2_write},
              "SR": {"reset": TXE, "ro_mask": 0xFFFFFFFF & ~CRCERR, "on_read": self._sr_read,
                     "on_write": self._sr_write},
              "DR": {"on_read": self._dr_read, "on_write": self._dr_write},
              "CRCPR": {"on_write": self._crcpr_write},
              "RXCRCR": {"ro_mask": 0xFFFFFFFF}, "TXCRCR": {"ro_mask": 0xFFFFFFFF},
              "I2SCFGR": {"on_write": self._i2s_write}, "I2SPR": {"on_write": self._i2spr_write}}
        r = build_regs((name, "SPI1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        i2s = params.get("i2s", name in ("SPI2", "SPI3"))
        self.i2s: I2SEngine | None = I2SEngine(self, block_index(name)) if i2s else None
        self.responder: Callable[[int, int, int], int] | None = None
        self.frame_end: Callable[[bool, bool], None] | None = None
        self.frames: deque[tuple[int, int]] = deque(maxlen=100_000)
        self.tx_bytes = bytearray()
        self.sig = {k: f"{name}_{k}" for k in ("SCK", "MOSI", "MISO", "NSS")}
        self._irqn: int | None = None
        self._line = False
        self._dma = [False, False]                  # TX, RX request levels last sent
        self._dma_names = (f"{name}_TX", f"{name}_RX")
        self._listen: set[str] = set()
        self._warned: set[str] = set()
        self._init_state()

    def _init_state(self) -> None:
        self.txbuf: int | None = None
        self.rxbuf = 0
        self.busy = False
        self._ev = None
        self._tx = 0                    # word in the shift register
        self._rx = 0
        self._bits = 8
        self._bit = 0                   # slave: bits sampled in the current frame
        self._crc_frame = False
        self._resp_rx: int | None = None
        self._wire = False
        self._out = {"SCK": None, "MOSI": None, "MISO": None, "NSS": None}   # levels this block drives (None = released)
        self._sck = None
        self._selected = False
        self._ovr_dr_read = False
        self._modf_seen = False
        self._last_tx = 0
        self._crc_after = False
        self._preloaded = False

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.name)
        if self.i2s is not None and self._rcc is not None:
            self._rcc.listeners.append(self.i2s.clock_changed)
        board = getattr(chip, "pins", None)
        if board is not None:
            sigs = [self.sig[k] for k in ("SCK", "MOSI", "NSS")]
            if self.i2s is not None and not self.i2s.ext:
                sigs.append(self.i2s.sig["CK"])
            for sig in sigs:
                for p in board.signal_pins(sig):
                    if p.id not in self._listen:
                        self._listen.add(p.id)
                        board.input_listeners[p.id].append(self._pin_changed)

    def reset(self) -> None:
        if getattr(self, "_ev", None) is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        had_out = any(v is not None for v in getattr(self, "_out", {}).values())
        if getattr(self, "_line", False) and self.chip is not None:
            shared_irq(self.chip, self._irqn, self.name, False)
        self._line = False
        eng = getattr(self, "i2s", None)
        if eng is not None and self.chip is not None:
            self.chip.sched.cancel(eng.ev)
            self.chip.sched.cancel(eng.mck_ev)
            eng.mck_ev = None
            eng.mck_hz = 0.0
            eng._wave.reset()
            eng.release_all()
        if eng is not None:
            eng.init()
        if self.chip is not None:
            for i, on in enumerate(self._dma):
                if on:
                    dma_request(self.chip, self._dma_names[i], False)
        self._dma = [False, False]
        self._init_state()
        self._warned.clear()
        super().reset()
        if had_out and self.chip is not None:
            for k in self.sig:
                self._release(k)
            self._resync()

    # ------------------------------------------------------------------ helpers
    def _cr1(self) -> int:
        return self.get("CR1")

    def _sr(self) -> int:
        return self.get("SR")

    def _sr_mod(self, set_bits: int = 0, clr_bits: int = 0) -> None:
        self.set("SR", (self.get("SR") | set_bits) & ~clr_bits)
        self._update()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def half_cycles(self) -> int:
        """Core cycles per SCK half period: 2^BR PCLK cycles."""
        r = self._rcc
        ppre = r.ppre(self.apb) if r is not None else 1
        return (1 << ((self._cr1() >> 3) & 7)) * ppre

    def baud(self) -> float:
        return self.chip.clock.hz / (2 * self.half_cycles()) if self.chip is not None else 0.0

    def is_master(self) -> bool:
        return bool(self._cr1() & MSTR)

    def enabled(self) -> bool:
        return bool(self._cr1() & SPE) and not self.get("I2SCFGR") & I2SMOD

    def _bitsize(self) -> int:
        return 16 if self._cr1() & DFF else 8

    def _pin_level(self, k: str) -> str | None:
        board = self.chip.pins
        sig = self.sig[k]
        for p in board.signal_pins(sig):
            if board.signal_matches(p.owner, sig):
                return p.level()
        return None

    def _muxed(self, k: str) -> bool:
        board = self.chip.pins
        sig = self.sig[k]
        return any(board.signal_matches(p.owner, sig) for p in board.signal_pins(sig))

    # ------------------------------------------------------------------ pins
    def signal_state(self, sig: str) -> tuple[str, str | None] | None:
        """Live (direction, idle drive) of one of this block's pins, for the GPIO model."""
        eng = self.i2s
        if eng is not None:
            role = self._i2s_role(sig)
            if role is not None:
                return eng.pin_state(role) if role != "-" else ("in", None)
        for k, s in self.sig.items():
            if s == sig:
                lvl = self._out[k]
                return ("out", lvl) if lvl is not None else ("in", None)
        return None

    def _i2s_role(self, sig: str) -> str | None:
        """I2S pin role ("CK", "WS", "SD", "MCK", "-" = unused) of signal `sig` while in I2S mode, else None."""
        eng = self.i2s
        for k, v in eng.sig.items():
            if v == sig:
                return k if eng.i2s_on() else ("-" if k != "MCK" else "MCK")
        if not eng.i2s_on() or eng.ext:
            return None
        return {"SCK": "CK", "NSS": "WS", "MOSI": "SD", "MISO": "-"}.get(
            next((k for k, v in self.sig.items() if v == sig), ""))

    def _want_out(self) -> dict[str, str | None]:
        """Signals this block should drive now and their idle level (None = released)."""
        cr1, cr2 = self._cr1(), self.get("CR2")
        out: dict[str, str | None] = {"SCK": None, "MOSI": None, "MISO": None, "NSS": None}
        if not self.enabled():
            return out
        if cr1 & MSTR:
            out["SCK"] = "H" if cr1 & CPOL else "L"
            if not (cr1 & BIDIMODE and not cr1 & BIDIOE) and not cr1 & RXONLY:
                out["MOSI"] = self._out["MOSI"] or "L"
            if cr2 & SSOE and not cr1 & SSM:
                out["NSS"] = "L"
        elif self._selected:
            out["MISO"] = self._out["MISO"] or "L"
        return out

    def _apply_pins(self) -> None:
        """Re-evaluate which signals this block drives after a mode change."""
        if self.chip is None:
            return
        newly: list[str] = []
        changed = False
        for k, lvl in self._want_out().items():
            cur = self._out[k]
            if lvl is None:
                if cur is not None:
                    self._release(k)
                    changed = True
            elif cur is None:
                self._out[k] = lvl
                newly.append(k)
                changed = True
            elif lvl != cur and (k == "NSS" or (k == "SCK" and not self.busy)):
                self._drive(k, lvl)
        if changed:
            self._resync()
        for k in newly:
            self.chip.pins.drive_signal(self.sig[k], self._out[k])

    def _resync(self) -> None:
        for s in self.sig.values():
            gpio_resync(self.chip, s)

    def _drive(self, k: str, lvl: str) -> None:
        if self._out[k] is None:
            return
        self._out[k] = lvl
        self.chip.pins.drive_signal(self.sig[k], lvl)

    def _release(self, k: str) -> None:
        self._out[k] = None
        if self.chip is not None:
            self.chip.pins.release_signal(self.sig[k])

    # ------------------------------------------------------------------ IRQ / DMA
    def _update(self) -> None:
        if self.chip is None:
            return
        sr, cr2 = self.get("SR"), self.get("CR2")
        line = bool((sr & TXE and cr2 & TXEIE) or (sr & RXNE and cr2 & RXNEIE)
                    or (sr & (OVR | MODF | CRCERR | UDR | FRE) and cr2 & ERRIE))
        if line != self._line and self._irqn is not None:
            self._line = line
            shared_irq(self.chip, self._irqn, self.name, line)
        tx = bool(cr2 & TXDMAEN and sr & TXE)
        rx = bool(cr2 & RXDMAEN and sr & RXNE)
        if tx != self._dma[0]:
            self._dma[0] = tx
            dma_request(self.chip, self._dma_names[0], tx)
        if rx != self._dma[1]:
            self._dma[1] = rx
            dma_request(self.chip, self._dma_names[1], rx)

    # ------------------------------------------------------------------ master engine
    def _bit_of(self, word: int, i: int) -> int:
        """Bit transmitted in position i (0 = first on the wire)."""
        n = self._bits
        return (word >> (i if self._cr1() & LSBFIRST else n - 1 - i)) & 1

    def _set_rx_bit(self, i: int, b: int) -> None:
        n = self._bits
        pos = i if self._cr1() & LSBFIRST else n - 1 - i
        if b:
            self._rx |= 1 << pos

    def _rx_continuous(self) -> bool:
        cr1 = self._cr1()
        return bool(cr1 & RXONLY or (cr1 & BIDIMODE and not cr1 & BIDIOE))

    def _master_kick(self) -> None:
        """Start the next master frame if one is due."""
        if self.busy or not self.enabled() or not self.is_master() or self.chip is None:
            return
        cr1 = self._cr1()
        self._bits = self._bitsize()
        mask = (1 << self._bits) - 1
        if self._crc_after:
            self._crc_after = False
            self._crc_frame = True
            tx = self.get("TXCRCR") & mask
        elif self.txbuf is not None:
            tx = self.txbuf & mask
            self.txbuf = None
            self._crc_frame = False
        elif self._rx_continuous():
            tx = 0
            self._crc_frame = False
        else:
            return
        self.busy = True
        self._tx = tx
        self._rx = 0
        self._sr_mod(TXE | BSY)
        self._wire = self._muxed("SCK") and (self._muxed("MOSI") or self._rx_continuous())
        if not self._wire:
            self._warn_once("nowire", f"{self.name}: master frame with SCK/MOSI not muxed to a pin: nobody sees it")
        self._resp_rx = None
        if self.responder is not None and self._wire:
            r = self.responder(tx, 0, self._bits)
            self._resp_rx = (r & mask) if self._muxed("MISO") else 0
        half = self.half_cycles()
        t0 = self.chip.now_cycles()
        self._t0 = t0
        self._half = half
        if not self._wire or self._out["SCK"] is None:
            self._ev = self.chip.sched.at_cycles(t0 + 2 * self._bits * half, self._master_done)   # timing only
            return
        if not cr1 & CPHA:
            self._drive_mosi(0)
        self._ev = self.chip.sched.at_cycles(t0 + half, self._master_edge, 0)

    def _drive_mosi(self, i: int) -> None:
        if self._out["MOSI"] is not None and not self._rx_continuous():
            self._drive("MOSI", "H" if self._bit_of(self._tx, i) else "L")

    def _sample_master(self, i: int) -> None:
        if self._resp_rx is not None:
            b = self._bit_of(self._resp_rx, i)
        else:
            k = "MOSI" if self._cr1() & BIDIMODE else "MISO"
            b = 1 if self._pin_level(k) == "H" else 0
        self._set_rx_bit(i, b)

    def _master_edge(self, k: int) -> None:
        self._ev = None
        cr1 = self._cr1()
        leading = k % 2 == 0
        i = k // 2
        cpol_h = bool(cr1 & CPOL)
        self._drive("SCK", ("L" if cpol_h else "H") if leading else ("H" if cpol_h else "L"))
        if cr1 & CPHA:
            if leading:
                self._drive_mosi(i)
            else:
                self._sample_master(i)
        else:
            if leading:
                self._sample_master(i)
            elif i + 1 < self._bits:
                self._drive_mosi(i + 1)
        if k + 1 < 2 * self._bits:
            self._ev = self.chip.sched.at_cycles(self._t0 + (k + 2) * self._half, self._master_edge, k + 1)
        else:
            self._master_done()

    def _master_done(self) -> None:
        self._ev = None
        self._frame_complete(self._tx, self._rx, receive=not (self._cr1() & BIDIMODE and self._cr1() & BIDIOE))
        self.busy = False
        if self.frame_end is not None and self._wire:
            self.frame_end(True, False)
        self._master_kick()
        if not self.busy:
            self._sr_mod(clr_bits=BSY)

    # ------------------------------------------------------------------ frame completion (master and slave)
    def _frame_complete(self, tx: int, rx: int, *, receive: bool = True) -> None:
        cr1 = self._cr1()
        bits = self._bits
        set_bits = 0
        if cr1 & CRCEN:
            poly = self.get("CRCPR") & 0xFFFF
            if self._crc_frame:
                if rx != (self.get("RXCRCR") & ((1 << bits) - 1)):
                    set_bits |= CRCERR
            else:
                self.set("TXCRCR", crc_update(self.get("TXCRCR"), tx, bits, poly))
                self.set("RXCRCR", crc_update(self.get("RXCRCR"), rx, bits, poly))
                if cr1 & CRCNEXT and self.is_master() and self.txbuf is None:
                    self.set("CR1", cr1 & ~CRCNEXT)
                    self._crc_after = True
        self._crc_frame = False
        self.frames.append((tx, rx))
        self._last_tx = tx
        if bits <= 8:
            self.tx_bytes.append(tx & 0xFF)
        else:
            self.tx_bytes += (tx & 0xFFFF).to_bytes(2, "big")
        if receive:
            if self.get("SR") & RXNE:
                set_bits |= OVR                             # the new word is lost
                self._ovr_dr_read = False
            else:
                self.rxbuf = rx
                set_bits |= RXNE
        self._sr_mod(set_bits)

    # ------------------------------------------------------------------ slave engine
    def _compute_selected(self) -> bool:
        cr1 = self._cr1()
        if cr1 & SSM:
            return not cr1 & SSI
        return self._pin_level("NSS") == "L"

    def _slave_load(self) -> None:
        if self.txbuf is not None:
            self._tx = self.txbuf & ((1 << self._bitsize()) - 1)
            self.txbuf = None
            self._sr_mod(TXE)
        else:
            self._tx = self._last_tx
        self._bits = self._bitsize()
        self._rx = 0
        self._bit = 0
        self._preloaded = True

    def _slave_out(self, i: int) -> None:
        if self._out["MISO"] is not None:
            self._drive("MISO", "H" if self._bit_of(self._tx, i) else "L")

    def _select_changed(self) -> None:
        sel = self._compute_selected() and self.enabled() and not self.is_master()
        if sel == self._selected:
            return
        self._selected = sel
        if self._bit:
            self._preloaded = False                  # deselected mid-frame: the partial word is dropped
        self._bit = 0
        if sel:
            self._sck = self._pin_level("SCK")
            cpha0 = not self._cr1() & CPHA
            if cpha0 and not self._preloaded:
                self._slave_load()                  # CPHA = 0: the first bit goes out when NSS falls
            self._apply_pins()
            if cpha0:
                self._slave_out(0)
            self._sr_mod(BSY)
        else:
            self._apply_pins()
            self._sr_mod(clr_bits=BSY)

    def _pin_changed(self, pin: Any) -> None:
        if self.chip is None:
            return
        board = self.chip.pins
        cr1 = self._cr1()
        owner = pin.owner
        if self.i2s is not None and self.i2s.i2s_on():
            self.i2s.pin_changed(pin)
            return
        if board.signal_matches(owner, self.sig["NSS"]):
            if cr1 & MSTR:
                if (cr1 & SPE and not cr1 & SSM and not self.get("CR2") & SSOE and pin.level() == "L"):
                    self._mode_fault()
            elif not cr1 & SSM:
                self._select_changed()
            return
        if not board.signal_matches(owner, self.sig["SCK"]) or cr1 & MSTR or not self._selected:
            return
        lvl = pin.level()
        prev = self._sck
        if lvl == prev:
            return
        self._sck = lvl
        if lvl not in ("H", "L") or prev not in ("H", "L"):
            return                                  # floating SCK: no clock edge
        leading = (lvl == "H") != bool(cr1 & CPOL)
        if cr1 & CPHA:
            if leading:
                if self._bit == 0:
                    self._slave_load()
                self._slave_out(self._bit)
            else:
                self._slave_sample()
        else:
            if leading:
                self._slave_sample()
            else:
                if self._bit == 0 and not self._preloaded:
                    self._slave_load()
                self._slave_out(self._bit)

    def _slave_sample(self) -> None:
        self._preloaded = False
        b = 1 if self._pin_level("MOSI") == "H" else 0
        self._set_rx_bit(self._bit, b)
        self._bit += 1
        if self._bit >= self._bits:
            self._bit = 0
            tx, rx = self._tx, self._rx
            self._rx = 0
            self._frame_complete(tx, rx)

    # ------------------------------------------------------------------ MODF
    def _mode_fault(self) -> None:
        self.emit("warn", block=self.name, msg=f"{self.name}: mode fault (NSS low in master mode): SPE and MSTR "
                  f"cleared")
        self._modf_seen = False
        self.set("CR1", self._cr1() & ~(SPE | MSTR))
        self._stop()
        self._sr_mod(MODF)

    def _stop(self) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        was = self.busy
        self.busy = False
        self._crc_after = False
        self._selected = False
        self._apply_pins()
        if self.frame_end is not None and (was or self.frames):
            self.frame_end(False, False)
        self._sr_mod(clr_bits=BSY)

    # ------------------------------------------------------------------ register hooks
    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _cr1_write(self, blk, reg, old, new):
        sr = self.get("SR")
        if sr & MODF:
            if self._modf_seen:
                self._sr_mod(clr_bits=MODF)                 # SR access then CR1 write clears MODF
                self._modf_seen = False
            else:
                new &= ~(SPE | MSTR)                        # SPE/MSTR stay cleared while MODF is set
        self._store(reg, new)
        if (old ^ new) & CRCEN:
            self.set("TXCRCR", 0)
            self.set("RXCRCR", 0)
        if new & SPE and new & MSTR and new & SSM and not new & SSI:
            self._mode_fault()
            return self.get("CR1")
        if new & MSTR and new & SPE and not new & SSM and not self.get("CR2") & SSOE \
                and self._pin_level("NSS") == "L":
            self._mode_fault()
            return self.get("CR1")
        if (old & SPE) and not (new & SPE):
            self._stop()
        elif (old ^ new) & MSTR and self.busy:
            self._stop()
        self._select_changed()
        self._apply_pins()
        self._master_kick()
        return None

    def _cr2_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new & FRF:
            self._warn_once("frf", f"{self.name}: TI frame format (CR2.FRF) is not modelled: Motorola timing used")
        if (old ^ new) & SSOE:
            self._apply_pins()
        self._update()
        return None

    def _i2s_write(self, blk, reg, old, new):
        eng = self.i2s
        if eng is None:
            if new & I2SMOD:
                self._warn_once("i2s", f"{self.name}: no I2S mode on this SPI (I2SCFGR.I2SMOD ignored): the block "
                                f"is idle")
            return None
        self._store(reg, new)
        on_old, on_new = old & I2SMOD and old & I2SE, new & I2SMOD and new & I2SE
        if (old ^ new) & I2SMOD and self.get("CR1") & SPE:
            self._warn_once("i2sspe", f"{self.name}: I2SMOD changed while SPI SPE = 1")
        if on_old and not on_new:
            eng.disable()
        elif on_new and not on_old:
            self._i2s_enable()
        elif on_new and (old ^ new) & 0x3BF:
            self._warn_once("i2scfg", f"{self.name}: I2SCFGR changed while I2SE = 1 (RM0090: configure with I2SE = "
                            f"0): applied at the next enable")
        eng.mck_update()
        self._update()
        return None

    def _i2s_enable(self) -> None:
        self.i2s.start()

    def _i2spr_write(self, blk, reg, old, new):
        eng = self.i2s
        if eng is not None and old != new:
            self._store(reg, new)
            if eng.running and eng.is_master():
                self._warn_once("i2spr", f"{self.name}: I2SPR changed while the I2S master runs")
            eng.clock_changed()
        return None

    def _crcpr_write(self, blk, reg, old, new):
        return None

    def _sr_read(self, blk, reg):
        sr = self.get("SR")
        if sr & (UDR | FRE):
            self.set("SR", sr & ~(UDR | FRE))           # I2S: UDR / FRE clear when SR is read (returned set)
            self._update()
        if sr & MODF:
            self._modf_seen = True
        if sr & OVR and self._ovr_dr_read:
            self._ovr_dr_read = False
            self.set("SR", sr & ~OVR)                    # DR read then SR read clears OVR (returns it set)
            self._update()
        return sr

    def _sr_write(self, blk, reg, old, new):
        if old & MODF:
            self._modf_seen = True
        v = (old & ~CRCERR) | (new & old & CRCERR)       # CRCERR is rc_w0
        self._store(reg, v)
        self._update()
        return v

    def _dr_read(self, blk, reg):
        sr = self.get("SR")
        if sr & OVR:
            self._ovr_dr_read = True
        if sr & RXNE:
            self._sr_mod(clr_bits=RXNE)
        return self.rxbuf

    def _dr_write(self, blk, reg, old, new):
        if self.get("I2SCFGR") & I2SMOD:
            if self.i2s is not None:
                self.txbuf = new & 0xFFFF
                self._sr_mod(clr_bits=TXE)
            return old
        self.txbuf = new & ((1 << self._bitsize()) - 1)
        self._sr_mod(clr_bits=TXE)
        if self.is_master():
            self._master_kick()
        return old


@register("stm32.i2s_ext")
class I2SExt(SPI):
    """I2S2ext / I2S3ext (RM0090 28.4.4 full duplex): see chipem/periph/stm32/i2s.py.  `params.main` names the SPI block
    whose I2S clock it uses (default: SPI2 for I2S2ext, SPI3 for I2S3ext)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        idx = block_index(name)
        main = str(params.get("main", f"SPI{idx}"))
        params["i2s"] = False
        super().__init__(name, base, size, regs, params=params, log=log)
        self.main_name = main
        self.gate = main                                 # no own RCC bit: SPIx's clock enable
        self.i2s = I2SEngine(self, block_index(main), ext=True)
        self._dma_names = (f"I2S{block_index(main)}_EXT_TX", f"I2S{block_index(main)}_EXT_RX")
        self.main: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.main_name)
        self.main = (getattr(chip, "peripherals", {}) or {}).get(self.main_name)
        meng = getattr(self.main, "i2s", None)
        if meng is not None:
            meng.partner = self.i2s
            self.i2s.partner = meng

    def _i2s_enable(self) -> None:
        meng = self.i2s.partner
        if meng is not None and meng.running:
            self.i2s.arm()

    def _cr1_write(self, blk, reg, old, new):
        if new & SPE:
            self._warn_once("spimode", f"{self.name}: an I2S extension has no SPI mode (CR1.SPE ignored)")
        self._store(reg, new & ~SPE)
        return None
