"""Kinetis I2C (I2C0-2, K22 Sub-Family Reference Manual K22P100M120SF5V2RM Rev. 5, ch. 48 "Inter-Integrated Circuit
(I2C)"; §3.9.4 chip configuration; Table 3-25 DMA request sources; §12 SIM_SCGC4/SCGC1 clock gates).

The bus is modelled at pin level (chipem/periph/i2cwire.py): SDA/SCL are the open-drain signals I2Cn_SDA/I2Cn_SCL on
the pins the PORT mux gives them, bits are clocked at the configured rate and a board net (with its pull-up)
carries them to other chips and to I2C parts.  A transfer to an address nobody acknowledges reads the released SDA
(pulled up, or floating X = 1) in the ACK slot: S.RXAK = 1.

Clocking (§48.3.2, Table 48-54): module clock = bus clock; SCL period = mul x SCL divider / bus clock (F.MULT 0/1/2 ->
mul 1/2/4, F.ICR -> divider), 50 % duty; SDA hold = mul x SDA hold value, START hold (SDA fall -> SCL fall) = mul x
SCL start hold value, STOP setup (SCL rise -> SDA rise) = mul x SCL stop hold value, all in bus clocks.

Registers (SVD table): A1, F, C1, S, D, C2, FLT, RA, SMB, A2, SLTH, SLTL.
* C1 (§48.3.3): IICEN enables the module (clearing it aborts a transfer and releases the lines); MST 0 -> 1
  generates a START and selects master mode (if the bus is busy with another master's transfer the START is not
  generated: S.ARBL and IICIF set, MST clears); MST 1 -> 0 generates a STOP (after the byte in progress); RSTA
  (write-only, reads 0) generates a repeated START while master; TX selects transmit/receive; TXAK = 1 sends NACK in
  the ACK slot of received bytes (sampled when the ACK bit is driven, i.e. SMB.FACK = 0 behaviour); IICIE enables
  the "I2C<n>" interrupt (level: IICIF & IICIE); DMAEN raises the DMAMUX request (source 18 for I2C0, 19 for
  I2C1/I2C2, `params.dma_source`) at the end of every byte instead of relying on the interrupt.  Any C1 write clears
  S.IAAS and S.RAM (§48.3.4).
* S (§48.3.4): TCF (0 while a byte is in progress, 1 after it; cleared by writing D in TX mode or reading D in RX
  mode), IAAS, BUSY (START seen .. STOP seen, any master), ARBL (w1c), RAM, SRW, IICIF (w1c), RXAK (1 = no ACK).
* D (§48.3.5): master TX: a write starts the byte transfer (the first byte after START/RSTA is the address + R/W);
  master RX: a read returns the last byte received and starts receiving the next one (switch TX or clear MST
  first to avoid it, as the RM notes); slave: after an address match the same semantics apply.  IICIF sets at the
  end of each byte including the ACK bit (the 9th SCL falling edge); the master then holds SCL low until software
  writes or reads D, sets RSTA or clears MST.
* Slave (MST = 0): 7-bit address match against A1 (C2.GCAEN: general call 0; C2.RMEN: A1 < address <= RA, sets RAM;
  SMB.SIICAEN: A2) sets IAAS, SRW, TCF and IICIF before the ACK bit; the address and every received byte are
  acknowledged automatically unless TXAK = 1.  After each byte the slave stretches SCL until D is accessed (read in
  receive mode, written in transmit mode; the address byte included: software writes the first byte or does the
  dummy read), as the module's handshake (§48.4.1.8).  The master's ACK of a transmitted byte shows in RXAK.
* FLT: STARTF/STOPF (w1c) set on bus START/STOP; with SSIE they set IICIF.  FLT.FLT (glitch filter) is stored.
* C2.HDRS, C2.SBRC, C2.AD[10:8]/ADEXT (10-bit slave addresses), SMB (SMBus timeouts, FACK, ALERTEN), SLTH/SLTL, A2
  without SIICAEN: stored only (see docs/LIMITATIONS.md).
* Low-power modes (48.1.2, 48.4.6.4, Table 7-2): WAIT keeps running.  In STOP/VLPS the module clock is off (the SMC
  freezer holds a master transfer's bit events and resumes them) but with C1.WUEN = 1 the slave address match stays
  active: a matching address (or general call) is acknowledged, sets IAAS/IICIF and the I2C interrupt wakes the core,
  while SCL is stretched until software accesses D.  Without WUEN (and always in LLS/VLLS) an address is not
  acknowledged while the clock is off.
Pins: PORT PCR.ODE must be set for open-drain operation (§3.9.4 note); without it a released line drives H (the PORT
model reports push-pull) and a slave's ACK conflicts with it on a net.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from ..i2cwire import I2CPort, I2CTiming
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number
from .clockutil import bus_hz
from .dma import dma_request

# C1
IICEN, IICIE, MST, TX, TXAK, RSTA, WUEN, DMAEN = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
# S
TCF, IAAS, BUSY, ARBL, RAM, SRW, IICIF, RXAK = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
# C2
GCAEN, ADEXT, RMEN = 0x80, 0x40, 0x08
# FLT
STOPF, SSIE, STARTF = 0x40, 0x20, 0x10
# SMB
SIICAEN = 0x20

# Table 48-54: ICR -> (SCL divider, SDA hold, SCL hold (start), SCL hold (stop))
ICR_TABLE = [
    (20, 7, 6, 11), (22, 7, 7, 12), (24, 8, 8, 13), (26, 8, 9, 14), (28, 9, 10, 15), (30, 9, 11, 16),
    (34, 10, 13, 18), (40, 10, 16, 21), (28, 7, 10, 15), (32, 7, 12, 17), (36, 9, 14, 19), (40, 9, 16, 21),
    (44, 11, 18, 23), (48, 11, 20, 25), (56, 13, 24, 29), (68, 13, 30, 35), (48, 9, 18, 25), (56, 9, 22, 29),
    (64, 13, 26, 33), (72, 13, 30, 37), (80, 17, 34, 41), (88, 17, 38, 45), (104, 21, 46, 53), (128, 21, 58, 65),
    (80, 9, 38, 41), (96, 9, 46, 49), (112, 17, 54, 57), (128, 17, 62, 65), (144, 25, 70, 73), (160, 25, 78, 81),
    (192, 33, 94, 97), (240, 33, 118, 121), (160, 17, 78, 81), (192, 17, 94, 97), (224, 33, 110, 113),
    (256, 33, 126, 129), (288, 49, 142, 145), (320, 49, 158, 161), (384, 65, 190, 193), (480, 65, 238, 241),
    (320, 33, 158, 161), (384, 33, 190, 193), (448, 65, 222, 225), (512, 65, 254, 257), (576, 97, 286, 289),
    (640, 97, 318, 321), (768, 129, 382, 385), (960, 129, 478, 481), (640, 65, 318, 321), (768, 65, 382, 385),
    (896, 129, 446, 449), (1024, 129, 510, 513), (1152, 193, 574, 577), (1280, 193, 638, 641),
    (1536, 257, 766, 769), (1920, 257, 958, 961), (1280, 129, 638, 641), (1536, 129, 766, 769),
    (1792, 257, 894, 897), (2048, 257, 1022, 1025), (2304, 385, 1150, 1153), (2560, 385, 1278, 1281),
    (3072, 513, 1534, 1537), (3840, 513, 1918, 1921),
]
MULT = (1, 2, 4, 4)            # MULT = 3 is reserved (treated as 4, one warn)


@register("kinetis.i2c")
class I2C(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 0))
        self.gate = f"I2C{self.index}"
        ov = {"C1": {"on_write": self._c1_write},
              "S": {"w1c_mask": IICIF | ARBL, "ro_mask": 0xFF & ~(IICIF | ARBL), "on_write": self._s_write},
              "D": {"on_read": self._d_read, "on_write": self._d_write, "on_peek": lambda b, r: self._rx},
              "F": {"on_write": self._f_write},
              "FLT": {"w1c_mask": STOPF | STARTF, "on_write": self._flt_write},
              "SMB": {"w1c_mask": 0x0A, "ro_mask": 0x04}}
        r = build_regs((name, f"I2C{self.index}", "I2C0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.sda_sig, self.scl_sig = f"I2C{self.index}_SDA", f"I2C{self.index}_SCL"
        self.port: I2CPort | None = None
        self._irqn: int | None = None
        self._line = False
        self._rx = 0                    # D read value (last byte received)
        self._tx: int | None = None     # byte waiting for the START to finish
        self._stop_pending = False
        self._dma = False
        self._svc = True                # slave: software has serviced the last byte event (D accessed)
        self._addr_phase = False
        self._rx_new = 0
        self._tx_slave: int | None = None
        self._warned_mult = False
        self._warned_pp = False
        self.dma_source = int(params.get("dma_source", {0: 18, 1: 19, 2: 19}.get(self.index, -1)))
        self.transfers: deque[tuple[str, int, bool]] = deque(maxlen=4096)   # (dir w/r/sw/sr, byte, acked), tests/UI

    # ------------------------------------------------------------------ wiring
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, f"I2C{self.index}", self.name)
        self.port = I2CPort(chip, self.sda_sig, self.scl_sig, self, self.timing, self._arb_lost)
        self.port.master.lp_owner = self               # the SMC freezer holds the master's bit events with ours
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.dma_source, lambda: self._dma)

    def reset(self) -> None:
        if self.port is not None:
            self.port.reset()
            self.port.target.enabled = False
        if self._line and self._irqn is not None and self.chip is not None:
            self.irq(self._irqn, False)
        self._line = False
        self._rx = 0
        self._tx = None
        self._stop_pending = False
        self._dma = False
        self._svc = True
        self._addr_phase = False
        self._tx_slave = None
        super().reset()

    # ------------------------------------------------------------------ timing
    def module_hz(self) -> float:
        return bus_hz(self.chip) if self.chip is not None else 0.0

    def timing(self) -> I2CTiming:
        f = self.get("F")
        mult = (f >> 6) & 3
        if mult == 3 and not self._warned_mult:
            self._warned_mult = True
            self.emit("warn", block=self.name, msg=f"{self.name}.F.MULT = 3 is reserved (treated as mul = 4)")
        mul = MULT[mult]
        div, sda_hold, start_hold, stop_hold = ICR_TABLE[f & 0x3F]
        t = 1e9 / max(self.module_hz(), 1.0) * mul
        half = div * t / 2.0
        return I2CTiming(low_ns=half, high_ns=half, hold_ns=sda_hold * t, hd_sta_ns=start_hold * t,
                         su_sta_ns=start_hold * t, su_sto_ns=stop_hold * t, buf_ns=half)

    def baud(self) -> float:
        return self.timing().hz

    # ------------------------------------------------------------------ status / IRQ
    def _s(self, set_bits: int = 0, clear_bits: int = 0) -> None:
        self.set("S", (self.get("S") | set_bits) & ~clear_bits)
        self._update()

    def _update(self) -> None:
        if self.chip is None:
            return
        line = bool(self.get("S") & IICIF and self.get("C1") & IICIE)
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)

    def _byte_done(self) -> None:
        """End of a byte (9th SCL fall) or an address match: TCF, IICIF, DMA request."""
        self._s(TCF | IICIF)
        if self.get("C1") & DMAEN:
            self._dma = True
            dma_request(self.chip, self.dma_source)

    def _master(self) -> bool:
        return bool(self.get("C1") & MST)

    def lines_active(self) -> bool:
        """The module drives its SDA/SCL lines (IICEN): asked by the PORT model for pins without PCR.ODE."""
        return bool(self.get("C1") & IICEN)

    def _refresh_pins(self) -> None:
        sigs = (self.sda_sig, self.scl_sig)
        board = self.chip.pins if self.chip is not None else None
        if board is None:
            return
        for b in self.chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.port":
                b.refresh_function(lambda owner: any(board.signal_matches(owner, sg) for sg in sigs))
        self._check_pp()

    def _check_pp(self) -> None:
        if self._warned_pp or not self.get("C1") & IICEN:
            return
        board = self.chip.pins
        for sg in (self.sda_sig, self.scl_sig):
            for p in board.signal_pins(sg):
                if board.signal_matches(p.owner, sg) and p.direction == "out":
                    self._warned_pp = True
                    self.emit("warn", block=self.name, pin=p.id, msg=f"{self.name}: {sg} on {p.id} is push-pull "
                              f"(PORT PCR.ODE = 0): the released line drives H (K22 RM §3.9.4 requires ODE for I2C)")
                    return

    # ------------------------------------------------------------------ register hooks
    def _c1_write(self, blk, reg, old, new):
        self.set("S", self.get("S") & ~(IAAS | RAM))
        port = self.port
        if port is None:
            return new & ~RSTA
        if not new & IICEN:
            self._tx = None
            self._stop_pending = False
            self._store(reg, new & ~RSTA)
            if old & IICEN:
                port.reset()
                self._refresh_pins()
            port.target.enabled = False
            self._update()
            return new & ~RSTA
        port.target.enabled = True
        self._store(reg, new & ~RSTA)
        if not old & IICEN:
            self._refresh_pins()
        m = port.master
        if new & MST and not old & MST:
            if self.get("S") & BUSY and not m.owns_bus:
                self._store(reg, new & ~(MST | RSTA))
                self._s(ARBL | IICIF)                   # START while another master holds the bus
                return new & ~(MST | RSTA)
            self.set("S", self.get("S") & ~TCF)
            self._stop_pending = False
            self._check_pp()
            if m.busy:
                m.abort(release=False)
            m.start(self._start_done)
        elif old & MST and not new & MST:
            if m.busy:
                self._stop_pending = True
            else:
                m.stop(self._stop_done)
        elif new & RSTA and new & MST:
            if m.busy:
                self.emit("warn", block=self.name, msg=f"{self.name}: RSTA during a byte transfer (ignored)")
            else:
                m.start(self._start_done)
        if (old ^ new) & IICIE:
            self._update()
        return new & ~RSTA

    def _s_write(self, blk, reg, old, new):
        self._update()
        return None

    def _f_write(self, blk, reg, old, new):
        return None

    def _flt_write(self, blk, reg, old, new):
        return None

    def _d_write(self, blk, reg, old, new):
        byte = new & 0xFF
        c1 = self.get("C1")
        port = self.port
        if port is None or not c1 & IICEN or not c1 & TX:
            return None
        self.set("S", self.get("S") & ~TCF)
        self._dma = False
        if c1 & MST:
            m = port.master
            if m.busy:
                self._tx = byte                         # START still being generated: send when it is done
            else:
                m.write(byte, self._tx_done)
        else:
            t = port.target
            self._svc = True
            if t.addressed and t.read:
                if t.holding:
                    t.send(byte)                        # slave transmit: the stretched SCL resumes
                else:
                    self._tx_slave = byte               # written before the ACK slot ended
        return None

    def _d_read(self, blk, reg):
        v = self._rx
        c1 = self.get("C1")
        port = self.port
        if port is None or not c1 & IICEN or c1 & TX:
            return v
        self.set("S", self.get("S") & ~TCF)
        self._dma = False
        if c1 & MST:
            m = port.master
            if not m.busy and m.owns_bus:
                m.read(lambda: not self.get("C1") & TXAK, self._rx_done)
        else:
            t = port.target
            self._svc = True
            if t.holding and not t.read:
                t.release()                             # slave receive: D read releases the stretched SCL
        return v

    # ------------------------------------------------------------------ master callbacks
    def _start_done(self) -> None:
        if self._tx is not None:
            b, self._tx = self._tx, None
            self.port.master.write(b, self._tx_done)
        elif self._stop_pending:
            self._stop_pending = False
            self.port.master.stop(self._stop_done)

    def _tx_done(self, acked: bool) -> None:
        self.set("S", (self.get("S") & ~RXAK) | (0 if acked else RXAK))
        self.transfers.append(("w", -1, acked))
        self._byte_done()
        self._after_byte()

    def _rx_done(self, byte: int, acked: bool) -> None:
        self._rx = byte
        self.transfers.append(("r", byte, acked))
        self._byte_done()
        self._after_byte()

    def _after_byte(self) -> None:
        if self._stop_pending:
            self._stop_pending = False
            self.port.master.stop(self._stop_done)

    def _stop_done(self) -> None:
        pass

    def _arb_lost(self) -> None:
        self.set("C1", self.get("C1") & ~MST)
        self._tx = None
        self._stop_pending = False
        self._s(ARBL | IICIF)

    # ------------------------------------------------------------------ bus decoder / slave (I2CTarget handler)
    def i2c_bus(self, busy: bool) -> None:
        self._s(BUSY if busy else 0, 0 if busy else BUSY)

    def i2c_start(self, repeated: bool) -> None:
        if self.get("C1") & IICEN:
            flt = self.get("FLT")
            self.set("FLT", flt | STARTF)
            if flt & SSIE:
                self._s(IICIF)

    def i2c_stop(self) -> None:
        if self.get("C1") & IICEN:
            flt = self.get("FLT")
            self.set("FLT", flt | STOPF)
            if flt & SSIE:
                self._s(IICIF)

    lp_frozen: Any = False                               # SMC freezer: the low-power mode while the clock is off

    def i2c_address(self, addr: int, read: bool) -> bool:
        c1 = self.get("C1")
        if not c1 & IICEN or c1 & MST or self.port.master.owns_bus:
            return False
        if self.lp_frozen and not (c1 & WUEN and self.lp_frozen in ("STOP", "VLPS")):
            return False                                 # clock off, no wake-up: the address is not acknowledged
        c2 = self.get("C2")
        a1 = (self.get("A1") >> 1) & 0x7F
        ra = (self.get("RA") >> 1) & 0x7F
        hit, rng = False, False
        if c2 & ADEXT:
            hit = False                                  # 10-bit slave addressing not modelled
        elif addr == a1:
            hit = True
        elif addr == 0 and c2 & GCAEN:
            hit = True
        elif c2 & RMEN and ra and a1 < addr <= ra:
            hit = rng = True
        elif self.get("SMB") & SIICAEN and addr == (self.get("A2") >> 1) & 0x7F:
            hit = True
        if not hit:
            return False
        if c1 & TXAK:
            return False
        s = self.get("S") & ~SRW
        self.set("S", s | IAAS | (SRW if read else 0) | (RAM if rng else 0))
        self._svc = False
        self._tx_slave = None
        self._addr_phase = True
        self._byte_done()                                # IAAS/IICIF set before the ACK bit (§48.3.4)
        return True

    def i2c_ack_done(self, read: bool) -> bool:
        """9th SCL fall after our ACK: a received data byte completes (TCF, IICIF); the slave stretches SCL until
        software has accessed D for the last event (the module's handshake)."""
        if self._addr_phase:
            self._addr_phase = False
        else:
            self._rx = self._rx_new
            self._svc = False
            self._byte_done()
        return not self._svc

    def i2c_rx_byte(self, byte: int) -> bool:
        if not self.port.target.addressed or (self.lp_frozen and self._svc):
            return False
        ack = not self.get("C1") & TXAK
        self.transfers.append(("sw", byte, ack))
        self._rx_new = byte
        if not ack:                                      # NACK sent: the byte is complete without an ACK slot
            self._rx = byte
            self._byte_done()
        return ack

    def i2c_tx_byte(self):
        b, self._tx_slave = self._tx_slave, None
        return b                                         # None: stretch until software writes D

    def i2c_tx_ack(self, acked: bool) -> None:
        self.set("S", (self.get("S") & ~RXAK) | (0 if acked else RXAK))
        self.transfers.append(("sr", -1, acked))
        self._svc = False
        self._tx_slave = None
        self._byte_done()
