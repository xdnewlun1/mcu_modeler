"""Kinetis DSPI (SPI0-2, K22 RM ch. 47): master and slave, bit by bit on the pins, with the CTAR timing.

* MCR: MSTR (31), CONT_SCKE (30), MTFE (26), PCSSE (25), ROOE (24), PCSIS (21-16), MDIS (14), DIS_TXF (13),
  DIS_RXF (12), CLR_TXF (11) / CLR_RXF (10) (flush, self-clearing), HALT (0); reset MDIS=1, HALT=1.  The module
  runs (SR.TXRXS=1) while HALT=0, MDIS=0 and EOQF=0 (47.4.1); HALT/MDIS/EOQF stop it at the next frame boundary.
* Master timing (47.4.3, CTARn with PUSHR.CTAS selecting CTAR0/1): the protocol clock fP is the bus clock
  (clockutil.bus_hz).  SCK period = PBR x BR / (1 + DBR) protocol clocks (PBR 2/3/5/7, BR 2/4/6/8/16..32768);
  tCSC = PCSSCK x CSSCK, tASC = PASC x ASC, tDT = PDT x DT (prescaler 1/3/5/7, scaler 2..65536).  With DBR the
  duty cycle follows Table 47-40 (CPHA=0: the SCK active phase is floor(P/2) clocks, CPHA=1: ceil(P/2); exact for
  BR=2, 50/50 otherwise).  A frame: PCS asserted (to !PCSIS) and, with CPHA=0, the first SOUT bit; tCSC later the
  first SCK edge; FMSZ+1 bits, MSB or LSB first (LSBFE); SOUT changes and SIN is sampled on the edges of
  47.4.4.1/2; after the last edge, TCF is set, the received word enters the RX FIFO and SPI_TCNT counts; tASC
  later PCS returns to its inactive state unless the command had CONT=1 (Continuous Selection Format, 47.4.4.5:
  PCS stays asserted and the next frame starts tASC + tCSC after the last edge, no tDT; PCS also stays asserted
  when the FIFO runs empty after a CONT frame, as on silicon).  The next non-continuous frame waits tDT after
  PCS negation.  PUSHR.EOQ sets EOQF at the end of its frame, which stops the module until EOQF is cleared (w1c).
* Pins: SCK, SOUT and PCS0-5 go through PinBoard.drive_signal (a pin muxed to SPIn_SCK etc. by the PORT shows the
  live level); SIN is sampled from the pin muxed to SPIn_SIN (H = 1, L or floating X = 0; not muxed: `params.miso`,
  default all ones).  While the module is a master (MSTR=1, MDIS=0) SCK idles at the CPOL of the last used CTAR
  (CTAR0 before the first frame) and every PCSn at PCSISn.  When SCK is not muxed to any pin the edges are not
  scheduled one by one: the frame takes the same time but only its end is an event (cheap when nothing watches).
* Responders: `responder(tx, pcs, bits) -> rx` (the SPI NOR flash of chipem/devices/spi_flash.py, `--spi-flash
  SPI0:cs=PTC4`, or any callable) answers a master frame instead of the SIN pin; it is called when the frame
  starts (after PCS asserts).  `frame_end(cont, eoq)` is called after every frame (a flash attached without a
  chip-select pin ends its command when CONT=0 or EOQ).
* Slave (MSTR=0, MDIS=0, running): CTAR0 gives FMSZ/CPOL/CPHA, data MSB first (47.4.2.2).  The model listens to
  the pins muxed to SPIn_SCK and SPIn_PCS0 (= SS, active low); SOUT is driven while selected.  The next TX FIFO
  entry is loaded when SS falls (CPHA=0) or on the first SCK edge (CPHA=1), and after each frame for continuous
  selection; an empty TX FIFO sets TFUF and shifts out zeros (what silicon sends is not specified).  The word
  received at the last edge enters the RX FIFO (TCF, SPI_TCNT); SS negated mid-frame drops the partial word.
  PORT direction hook: in slave mode SCK/PCS/SIN are inputs and SOUT an output only while selected.
* FIFOs (depth `params.fifo`, 4 for SPI0 and 1 for SPI1/2 per the K22 chip configuration; DIS_TXF/DIS_RXF make
  them one entry).  PUSHR (8/16/32-bit writes) pushes command+data; a push to a full FIFO is ignored.  POPR pops;
  overflow sets RFOF and drops the word unless ROOE (then it replaces the newest entry).  SR: TCF, EOQF, TFUF,
  TFFF, RFOF, RFDF are w1c; TFFF (TX FIFO not full), RFDF (RX FIFO not empty), TXCTR, RXCTR, TXNXTPTR/POPNXTPTR
  and TXRXS are live.  TXFRn/RXFRn mirror the FIFOs.
* IRQ "SPI<n>" = OR of (flag & RSER enable) for TCF/EOQF/TFUF/RFOF and TFFF/RFDF unless routed to DMA
  (RSER.TFFF_DIRS/RFDF_DIRS); DMA requests use the DMAMUX sources SPI0 RX/TX = 14/15, SPI1 = 16, SPI2 = 17.
* Every frame is recorded in `self.frames` (pcs, tx, rx); `self.output` collects the transmitted bytes.
Not modelled (one `warn` each when enabled): continuous SCK (MCR.CONT_SCKE), the modified transfer format (MTFE;
classic timing is used), the PCS strobe (PCSSE), pin input/output delays.
Stop modes and Doze (WAIT/VLPW with MCR.DOZE) (`clock_stop`, 47.4.8.1): the frame in progress completes, then the
module is STOPPED until wake-up.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ...pins import PinBoard
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number
from .clockutil import bus_hz
from .dma import dma_request

MCR_MSTR, MCR_CONT_SCKE, MCR_MTFE, MCR_PCSSE, MCR_ROOE = 1 << 31, 1 << 30, 1 << 26, 1 << 25, 1 << 24
MCR_DOZE = 1 << 15
MCR_MDIS, MCR_DIS_TXF, MCR_DIS_RXF, MCR_CLR_TXF, MCR_CLR_RXF, MCR_HALT = 1 << 14, 1 << 13, 1 << 12, 1 << 11, \
    1 << 10, 1
SR_TCF, SR_TXRXS, SR_EOQF, SR_TFUF, SR_TFFF, SR_RFOF, SR_RFDF = (1 << 31, 1 << 30, 1 << 28, 1 << 27, 1 << 25,
                                                                 1 << 19, 1 << 17)
W1C = SR_TCF | SR_EOQF | SR_TFUF | SR_TFFF | SR_RFOF | SR_RFDF
RSER_TFFF_DIRS, RSER_RFDF_DIRS = 1 << 24, 1 << 16
PUSHR_CONT, PUSHR_EOQ, PUSHR_CTCNT = 1 << 31, 1 << 27, 1 << 26
CTAR_DBR, CTAR_CPOL, CTAR_CPHA, CTAR_LSBFE = 1 << 31, 1 << 26, 1 << 25, 1 << 24

PRESCALE = (1, 3, 5, 7)                         # PCSSCK / PASC / PDT
PBR_TAB = (2, 3, 5, 7)
BR_TAB = (2, 4, 6, 8) + tuple(1 << n for n in range(4, 16))
NPCS = 6


def ctar_timing(ctar: int) -> dict[str, float]:
    """Protocol-clock counts of one CTAR: period P, SCK active phase A, tCSC, tASC, tDT (47.3.3, 47.4.3)."""
    dbr = 1 if ctar & CTAR_DBR else 0
    p2 = PBR_TAB[(ctar >> 16) & 3] * BR_TAB[ctar & 0xF]             # = P x (1 + DBR)
    if dbr:
        period = p2 // 2 if p2 % 2 == 0 else p2 / 2                  # BR is even, so P is an integer
        p_int = int(period)
        active = p_int // 2 if not ctar & CTAR_CPHA else p_int - p_int // 2
    else:
        period = p2
        active = p2 / 2
    return {"P": float(period), "A": float(active),
            "csc": PRESCALE[(ctar >> 22) & 3] * (2 << ((ctar >> 12) & 0xF)),
            "asc": PRESCALE[(ctar >> 20) & 3] * (2 << ((ctar >> 8) & 0xF)),
            "dt": PRESCALE[(ctar >> 18) & 3] * (2 << ((ctar >> 4) & 0xF))}


@register("kinetis.dspi")
class DSPI(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        digits = "".join(ch for ch in name if ch.isdigit())
        self.index = int(params.get("index", digits or 0))
        self.gate = f"SPI{self.index}"
        ov = {"MCR": {"on_write": self._mcr_write},
              "SR": {"w1c_mask": W1C, "ro_mask": ~W1C & 0xFFFFFFFF, "on_read": self._sr_read,
                     "on_write": self._sr_write},
              "RSER": {"on_write": lambda b, r, o, n: self._update()},
              "CTAR0": {"on_write": self._ctar_write}, "CTAR1": {"on_write": self._ctar_write},
              "PUSHR": {"on_write": self._pushr_write},
              "POPR": {"ro_mask": 0xFFFFFFFF, "on_read": self._popr_read}}
        for i in range(4):
            ov[f"TXFR{i}"] = {"ro_mask": 0xFFFFFFFF, "on_read": self._fifo_read(True, i)}
            ov[f"RXFR{i}"] = {"ro_mask": 0xFFFFFFFF, "on_read": self._fifo_read(False, i)}
        r = build_regs((name, f"SPI{self.index}", "SPI0"), size, ov, params.get("regs_file"),
                       skip=("CTAR_SLAVE", "PUSHR_SLAVE"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.depth = int(params.get("fifo", 4 if self.index == 0 else 1))
        self.miso = int(params.get("miso", 0xFFFF))
        self.responder: Callable[[int, int, int], int] | None = None
        self.frame_end: Callable[[bool, bool], None] | None = None   # cb(cont, eoq) after each frame
        self.txq: deque[int] = deque()
        self.rxq: deque[int] = deque()
        self.frames: deque[tuple[int, int, int]] = deque(maxlen=100_000)
        self.output = bytearray()
        pre = f"SPI{self.index}"
        self.sig = {"SCK": f"{pre}_SCK", "SOUT": f"{pre}_SOUT", "SIN": f"{pre}_SIN",
                    **{f"PCS{i}": f"{pre}_PCS{i}" for i in range(NPCS)}}
        self._irqn: int | None = None
        self.dma_rx = self.dma_tx = -1
        self._listen: set[str] = set()
        self._warned: set[str] = set()
        self._line = False
        self._init_state()

    def _init_state(self) -> None:
        self._last_rx = 0
        self._busy = False                      # register-hook reentrancy guard
        self._ev = None                         # next master edge / frame start
        self._neg_ev = None                     # pending PCS negation
        self._frame: dict | None = None         # master frame in flight
        self._held_pcs = 0                      # PCS bits kept asserted after a CONT frame
        self._cont_ready = 0.0                  # earliest first edge time - tCSC of the next CONT frame
        self._dt_until = 0.0                    # earliest PCS assertion of the next frame (tDT)
        self._last_ctar: int | None = None      # CTAR of the last master frame (SCK idle level)
        self._wire = False                      # current master frame clocks a muxed SCK pin
        self._out: dict[str, str | None] = {k: None for k in self.sig}
        # slave state
        self._selected = False
        self._sck: str | None = None
        self._s_tx = 0
        self._s_rx = 0
        self._s_bit = 0
        self._s_loaded = False
        self._lp_stop = False                   # Stop / VLPS / LLS: clocks off from the next frame boundary

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, f"SPI{self.index}", self.name)
        rx, tx = {0: (14, 15), 1: (16, 16), 2: (17, 17)}.get(self.index, (-1, -1))
        self.dma_rx = int(self.params.get("dma_rx", rx))
        self.dma_tx = int(self.params.get("dma_tx", tx))
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.dma_rx, lambda: self._dma_rx_active())
                if self.dma_tx != self.dma_rx:
                    b.register_source(self.dma_tx, lambda: self._dma_tx_active())
            if getattr(b, "model", None) == "kinetis.port":
                hooks = getattr(b, "signal_dir_hooks", None)
                if hooks is not None and self._pin_dir not in hooks:
                    hooks.append(self._pin_dir)
        board = getattr(chip, "pins", None)
        if board is not None:
            for k in ("SCK", "PCS0"):
                for p in board.signal_pins(self.sig[k]):
                    if (k, p.id) not in self._listen:
                        self._listen.add((k, p.id))
                        board.input_listeners[p.id].append(self._pin_changed)

    def reset(self) -> None:
        chip = self.chip
        if chip is not None:
            chip.sched.cancel(getattr(self, "_ev", None))
            chip.sched.cancel(getattr(self, "_neg_ev", None))
        if getattr(self, "_line", False) and chip is not None and self._irqn is not None:
            self.irq(self._irqn, False)
        self._line = False
        had_out = any(v is not None for v in getattr(self, "_out", {}).values())
        self.txq.clear()
        self.rxq.clear()
        self._warned.clear()
        self._init_state()
        super().reset()
        if had_out and chip is not None and chip.pins is not None:
            for s in self.sig.values():
                chip.pins.release_signal(s)
            self._refresh_ports()

    # ------------------------------------------------------------------ state
    def running(self) -> bool:
        mcr, sr = self.get("MCR"), self.get("SR")
        return not (mcr & (MCR_HALT | MCR_MDIS)) and not sr & SR_EOQF and not self._lp_stop

    # ------------------------------------------------------------------ low-power modes (SMC)
    def clock_stop(self, stopped: bool, mode: str) -> None:
        """External Stop mode (47.4.8.1): a frame in progress completes (the stop acknowledge waits for the frame
        boundary; its events keep running), then the module is STOPPED (SR.TXRXS = 0) until the clocks return: no
        new master frame starts and slave SCK/SS edges are not seen.  WAIT/VLPW is Doze (3.9.3.8): the same with
        MCR.DOZE = 1, nothing otherwise."""
        if self.chip is None:
            return
        if stopped:
            if mode in ("WAIT", "VLPW") and not self.get("MCR") & MCR_DOZE:
                return
            self._lp_stop = True
            if self._frame is None and self._ev is not None:       # frame start pending (tDT / tASC wait): parked
                self.chip.sched.cancel(self._ev)
                self._ev = None
            return
        if not self._lp_stop:
            return
        self._lp_stop = False
        self._kick()

    def is_master(self) -> bool:
        return bool(self.get("MCR") & MCR_MSTR)

    def _slave_active(self) -> bool:
        mcr = self.get("MCR")
        return not mcr & (MCR_MSTR | MCR_MDIS)

    def _txdepth(self) -> int:
        return 1 if self.get("MCR") & MCR_DIS_TXF else self.depth

    def _rxdepth(self) -> int:
        return 1 if self.get("MCR") & MCR_DIS_RXF else self.depth

    def _status(self) -> int:
        v = self.get("SR") & (SR_TCF | SR_EOQF | SR_TFUF | SR_RFOF)
        if len(self.txq) < self._txdepth():
            v |= SR_TFFF
        if self.rxq:
            v |= SR_RFDF
        if self.running():
            v |= SR_TXRXS
        n_tx, n_rx = len(self.txq), len(self.rxq)
        return v | (min(n_tx, 15) << 12) | (min(n_rx, 15) << 4)

    def baud(self, ctar: int | None = None) -> float:
        """SCK frequency of CTAR0 (or the given CTAR value) in Hz."""
        if self.chip is None:
            return 0.0
        c = ctar_timing(self.get("CTAR0") if ctar is None else ctar)
        return bus_hz(self.chip) / c["P"]

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ pins
    def _muxed(self, k: str) -> bool:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return False
        sig = self.sig[k]
        return any(board.signal_matches(p.owner, sig) for p in board.signal_pins(sig))

    def _pin_level(self, k: str) -> str | None:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return None
        sig = self.sig[k]
        for p in board.signal_pins(sig):
            if board.signal_matches(p.owner, sig):
                return p.level()
        return None

    def _drive(self, k: str, lvl: str) -> None:
        if self._out[k] == lvl:
            return
        self._out[k] = lvl
        board = getattr(self.chip, "pins", None)
        if board is not None and board.signal_pins(self.sig[k]):
            board.drive_signal(self.sig[k], lvl)

    def _release(self, k: str) -> None:
        if self._out[k] is None:
            return
        self._out[k] = None
        board = getattr(self.chip, "pins", None)
        if board is not None:
            board.release_signal(self.sig[k])

    def _pcs_level(self, i: int, asserted: bool) -> str:
        inactive_h = bool(self.get("MCR") >> (16 + i) & 1)
        return ("L" if inactive_h else "H") if asserted else ("H" if inactive_h else "L")

    def _apply_master_idle(self) -> None:
        """Master: SCK at the CPOL of the last CTAR (CTAR0 initially), PCSn at their PCSIS state (held CONT bits
        stay asserted).  Not a master: release everything."""
        if self.chip is None:
            return
        if self.is_master() and not self.get("MCR") & MCR_MDIS:
            if self._frame is None:
                ctar = self.get("CTAR0") if self._last_ctar is None else self._last_ctar
                self._drive("SCK", "H" if ctar & CTAR_CPOL else "L")
                if self._out["SOUT"] is None:
                    self._drive("SOUT", "L")
            asserted = self._frame["pcs"] if self._frame is not None else self._held_pcs
            for i in range(NPCS):
                self._drive(f"PCS{i}", self._pcs_level(i, bool(asserted >> i & 1)))
        else:
            for k in ("SCK", "SOUT", *(f"PCS{i}" for i in range(NPCS))):
                if k != "SOUT" or not self._selected:
                    self._release(k)

    def _pin_dir(self, owner: str) -> tuple[str, Any] | None:
        """PORT hook: in slave mode (MSTR=0, MDIS=0) SCK/PCSn/SIN are inputs and SOUT drives only while selected;
        otherwise None (the signal table: SCK/SOUT/PCS outputs, SIN input)."""
        pre = self.sig["SCK"][:-3]                         # "SPIn_"
        if pre not in owner or not self._slave_active():
            return None
        if PinBoard.signal_matches(owner, self.sig["SOUT"]):
            return ("out", self._out["SOUT"] or "L") if self._selected else ("in", None)
        for k in ("SCK", "SIN", *(f"PCS{i}" for i in range(NPCS))):
            if PinBoard.signal_matches(owner, self.sig[k]):
                return "in", None
        return None

    def _refresh_ports(self) -> None:
        chip = self.chip
        if chip is None or chip.pins is None:
            return
        pre = self.sig["SCK"][:-3]
        for b in chip.peripherals.values():
            f = getattr(b, "refresh_function", None)
            if f is not None and getattr(b, "model", None) == "kinetis.port":
                f(lambda owner: pre in str(owner))

    # ------------------------------------------------------------------ master engine
    def _kick(self) -> None:
        """Start the next master frame if one is due."""
        chip = self.chip
        if (chip is None or self._frame is not None or self._ev is not None or not self.txq
                or not self.is_master() or not self.running()):
            return
        mcr = self.get("MCR")
        if mcr & MCR_CONT_SCKE:
            self._warn_once("cscke", f"{self.name}: continuous SCK (MCR.CONT_SCKE) is not modelled: SCK runs only "
                            f"during frames")
        if mcr & MCR_MTFE:
            self._warn_once("mtfe", f"{self.name}: modified transfer format (MCR.MTFE) is not modelled: classic "
                            f"SPI timing is used")
        if mcr & MCR_PCSSE:
            self._warn_once("pcsse", f"{self.name}: PCS strobe (MCR.PCSSE) is not modelled: PCS5 acts as a chip "
                            f"select")
        now = chip.now_ns()
        cmd = self.txq[0]
        pcs = (cmd >> 16) & 0x3F
        if self._held_pcs and self._held_pcs == pcs:
            start = max(now, self._cont_ready)
        else:
            start = max(now, self._dt_until)
        if start > now + 1e-6:
            self._ev = chip.sched.at_ns(start, self._frame_start, start)
        else:
            self._frame_start(now)

    def _frame_start(self, t: float) -> None:
        self._ev = None
        chip = self.chip
        if not self.txq or not self.is_master() or not self.running():
            return
        cmd = self.txq.popleft()
        ctar = self.get("CTAR1" if (cmd >> 28) & 7 == 1 else "CTAR0")
        self._last_ctar = ctar
        bits = ((ctar >> 27) & 0xF) + 1
        mask = (1 << bits) - 1
        pcs = (cmd >> 16) & 0x3F
        tx = cmd & mask
        if cmd & PUSHR_CTCNT:
            self.set("TCR", self.get("TCR") & 0xFFFF)
        tm = ctar_timing(ctar)
        tp = 1e9 / bus_hz(chip)
        if self._held_pcs and self._held_pcs != pcs:
            self._warn_once("contpcs", f"{self.name}: continuous selection with a different PCS mask (47.4.4.5 "
                            f"requires the same PCS bits)")
        self._held_pcs = 0
        if self._neg_ev is not None:
            chip.sched.cancel(self._neg_ev)
            self._neg_ev = None
        lsb = bool(ctar & CTAR_LSBFE)
        order = list(range(bits)) if lsb else list(range(bits - 1, -1, -1))
        fr = {"cmd": cmd, "ctar": ctar, "bits": bits, "mask": mask, "pcs": pcs, "tx": tx, "rx": 0, "order": order,
              "cpha": bool(ctar & CTAR_CPHA), "cpol": bool(ctar & CTAR_CPOL), "P": tm["P"] * tp, "A": tm["A"] * tp,
              "asc": tm["asc"] * tp, "dt": tm["dt"] * tp, "t0": t + tm["csc"] * tp, "resp": None}
        self._frame = fr
        # PCS asserted, SCK at its idle level, first data bit for CPHA=0
        self._drive("SCK", "H" if fr["cpol"] else "L")
        for i in range(NPCS):
            self._drive(f"PCS{i}", self._pcs_level(i, bool(pcs >> i & 1)))
        if not fr["cpha"]:
            self._drive("SOUT", "H" if tx >> order[0] & 1 else "L")
        if self.responder is not None:
            fr["resp"] = self.responder(tx, pcs, bits) & mask
        self._update()                                 # TX FIFO entry consumed: TFFF / DMA
        self._wire = self._muxed("SCK")
        if self._wire:
            self._ev = chip.sched.at_ns(fr["t0"], self._edge, 0)
        else:
            self._ev = chip.sched.at_ns(self._edge_time(fr, 2 * bits - 1), self._edge, 2 * bits - 1)

    @staticmethod
    def _edge_time(fr: dict, k: int) -> float:
        return fr["t0"] + (k // 2) * fr["P"] + (fr["A"] if k & 1 else 0.0)

    def _sample(self, fr: dict, i: int) -> None:
        if fr["resp"] is not None:
            b = fr["resp"] >> fr["order"][i] & 1
        elif self._wire and self._muxed("SIN"):
            b = 1 if self._pin_level("SIN") == "H" else 0
        else:
            b = self.miso >> fr["order"][i] & 1
        if b:
            fr["rx"] |= 1 << fr["order"][i]

    def _edge(self, k: int) -> None:
        self._ev = None
        fr = self._frame
        if fr is None:
            return
        bits = fr["bits"]
        if self._wire:
            leading = k % 2 == 0
            i = k // 2
            cpol = fr["cpol"]
            self._drive("SCK", ("L" if cpol else "H") if leading else ("H" if cpol else "L"))
            if fr["cpha"]:
                if leading:
                    self._drive("SOUT", "H" if fr["tx"] >> fr["order"][i] & 1 else "L")
                else:
                    self._sample(fr, i)
            else:
                if leading:
                    self._sample(fr, i)
                elif i + 1 < bits:
                    self._drive("SOUT", "H" if fr["tx"] >> fr["order"][i + 1] & 1 else "L")
        else:
            for i in range(bits):
                self._sample(fr, i)
        if k + 1 < 2 * bits:
            self._ev = self.chip.sched.at_ns(self._edge_time(fr, k + 1), self._edge, k + 1)
            return
        self._master_done(fr, self._edge_time(fr, k))

    def _master_done(self, fr: dict, t_last: float) -> None:
        cmd = fr["cmd"]
        self._frame = None
        cont = bool(cmd & PUSHR_CONT)
        eoq = bool(cmd & PUSHR_EOQ)
        self._complete(fr["pcs"], fr["tx"], fr["rx"], fr["bits"], eoq=eoq)
        if cont and not eoq:
            self._held_pcs = fr["pcs"]
            self._cont_ready = t_last + fr["asc"]
        else:
            t_neg = t_last + fr["asc"]
            self._dt_until = t_neg + fr["dt"]
            self._neg_ev = self.chip.sched.at_ns(t_neg, self._negate, fr["pcs"])
        if self.frame_end is not None:
            self.frame_end(cont, eoq)
        self._kick()

    def _negate(self, pcs: int) -> None:
        self._neg_ev = None
        if self._frame is not None or not self.is_master():
            return
        for i in range(NPCS):
            if pcs >> i & 1:
                self._drive(f"PCS{i}", self._pcs_level(i, False))

    def _abort_master(self) -> None:
        if self.chip is not None:
            self.chip.sched.cancel(self._ev)
            self.chip.sched.cancel(self._neg_ev)
        self._ev = self._neg_ev = None
        self._frame = None
        self._held_pcs = 0

    # ------------------------------------------------------------------ frame completion (master and slave)
    def _complete(self, pcs: int, tx: int, rx: int, bits: int, *, eoq: bool = False) -> None:
        self.frames.append((pcs, tx, rx))
        if bits <= 8:
            self.output.append(tx & 0xFF)
        else:
            self.output += (tx & 0xFFFF).to_bytes(2, "big")
        sr = self.get("SR") | SR_TCF
        if len(self.rxq) < self._rxdepth():
            self.rxq.append(rx)
        else:
            sr |= SR_RFOF
            if self.get("MCR") & MCR_ROOE:   # overwrite the newest entry
                self.rxq.pop()
                self.rxq.append(rx)
        if eoq:
            sr |= SR_EOQF
        self.set("SR", sr)
        self.set("TCR", (self.get("TCR") + 0x10000) & 0xFFFFFFFF)
        self._update()

    # ------------------------------------------------------------------ slave engine
    def _slave_bits(self) -> int:
        return ((self.get("CTAR0") >> 27) & 0xF) + 1

    def _slave_load(self, early: bool = False) -> None:
        """Load the next TX FIFO entry into the shift register.  `early`: the first bit of a following frame
        (continuous selection, CPHA=0) goes out at the previous frame's last trailing edge if data is ready; an
        empty FIFO is only an underflow once the frame really starts."""
        if self.txq and self.running():
            self._s_tx = self.txq.popleft()
            self._update()
        elif early:
            self._s_tx = 0
            return
        else:
            self._s_tx = 0
            if self.running():
                self.set("SR", self.get("SR") | SR_TFUF)
                self._update()
        self._s_rx = 0
        self._s_bit = 0
        self._s_loaded = True

    def _slave_out(self) -> None:
        if not self._selected:
            return
        n = self._slave_bits()
        i = self._s_bit
        b = self._s_tx >> (n - 1 - i) & 1 if i < n else 0
        self._drive("SOUT", "H" if b else "L")

    def _select_changed(self) -> None:
        sel = self._slave_active() and self.running() and self._pin_level("PCS0") == "L"
        if sel == self._selected:
            return
        self._selected = sel
        self._s_bit = 0
        self._s_loaded = False
        if sel:
            self._sck = self._pin_level("SCK")
            if not self.get("CTAR0") & CTAR_CPHA:
                self._slave_load()
            self._out["SOUT"] = None
            self._drive("SOUT", "L")
            self._refresh_ports()
            if self._s_loaded:
                self._slave_out()
        else:
            self._release("SOUT")
            self._refresh_ports()

    def _pin_changed(self, pin: Any) -> None:
        if self.chip is None or self._lp_stop or not self._slave_active():
            return
        board = self.chip.pins
        owner = pin.owner
        if board.signal_matches(owner, self.sig["PCS0"]):
            self._select_changed()
            return
        if not self._selected or not board.signal_matches(owner, self.sig["SCK"]):
            return
        lvl = pin.level()
        prev, self._sck = self._sck, lvl
        if lvl == prev or lvl not in ("H", "L") or prev not in ("H", "L"):
            return
        ctar = self.get("CTAR0")
        leading = (lvl == "H") != bool(ctar & CTAR_CPOL)
        if ctar & CTAR_CPHA:
            if leading:
                if not self._s_loaded:
                    self._slave_load()
                self._slave_out()
            else:
                self._slave_sample()
        else:
            if leading:
                self._slave_sample()
            else:
                if not self._s_loaded:
                    self._slave_load(early=self._s_bit == 0)
                self._slave_out()

    def _slave_sample(self) -> None:
        n = self._slave_bits()
        if not self._s_loaded:
            self._slave_load()
        if self._pin_level("SIN") == "H":
            self._s_rx |= 1 << (n - 1 - self._s_bit)
        self._s_bit += 1
        if self._s_bit >= n:
            tx, rx = self._s_tx & ((1 << n) - 1), self._s_rx
            self._s_bit = 0
            self._s_loaded = False
            self._complete(0, tx, rx, n)

    # ------------------------------------------------------------------ IRQ / DMA
    def _dma_rx_active(self) -> bool:
        return bool(self.rxq) and bool(self.get("RSER") & RSER_RFDF_DIRS) and bool(self.get("RSER") & SR_RFDF)

    def _dma_tx_active(self) -> bool:
        return (len(self.txq) < self._txdepth() and bool(self.get("RSER") & RSER_TFFF_DIRS)
                and bool(self.get("RSER") & SR_TFFF))

    def _update(self) -> None:
        if self.chip is None:
            return
        st, rser = self._status(), self.get("RSER")
        en = rser & (SR_TCF | SR_EOQF | SR_TFUF | SR_RFOF)
        if not rser & RSER_TFFF_DIRS:
            en |= rser & SR_TFFF
        if not rser & RSER_RFDF_DIRS:
            en |= rser & SR_RFDF
        line = bool(st & en)
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        if not self._busy:
            self._busy = True
            try:
                if self._dma_rx_active():
                    dma_request(self.chip, self.dma_rx)
                if self._dma_tx_active():
                    dma_request(self.chip, self.dma_tx)
            finally:
                self._busy = False

    # ------------------------------------------------------------------ register hooks
    def _mcr_write(self, blk, reg, old, new):
        if new & MCR_CLR_TXF:
            self.txq.clear()
        if new & MCR_CLR_RXF:
            self.rxq.clear()
        new &= ~(MCR_CLR_TXF | MCR_CLR_RXF)
        self._store(reg, new)
        if (old ^ new) & (MCR_MSTR | MCR_MDIS):
            if self._frame is not None and (not new & MCR_MSTR or new & MCR_MDIS):
                self._abort_master()
            if self._selected and not self._slave_active():
                self._selected = False
            self._last_ctar = None
            self._apply_master_idle()
            self._refresh_ports()
        elif (old ^ new) & (0x3F << 16):
            self._apply_master_idle()                 # PCSIS changed
        if self._slave_active():
            self._select_changed()
        self._update()
        self._kick()
        return new

    def _ctar_write(self, blk, reg, old, new):
        if reg.name == "CTAR0" and self._frame is None and self._last_ctar is None:
            self._apply_master_idle()
        return None

    def _sr_read(self, blk, reg):
        return self._status()

    def _sr_write(self, blk, reg, old, new):
        self._store(reg, new & (SR_TCF | SR_EOQF | SR_TFUF | SR_RFOF))
        self._update()
        self._kick()                            # clearing EOQF restarts the module
        if self._slave_active():
            self._select_changed()
        return None

    def _pushr_write(self, blk, reg, old, new):
        if self.get("MCR") & MCR_MDIS:
            return None                         # Module Disable: PUSHR does not change the TX FIFO (47.4.8.2)
        if len(self.txq) < self._txdepth():
            self.txq.append(new)
        self._update()
        self._kick()
        return None

    def _popr_read(self, blk, reg):
        if self.rxq and not self.get("MCR") & MCR_MDIS:
            self._last_rx = self.rxq.popleft()
            self._update()
        return self._last_rx

    def _fifo_read(self, tx: bool, i: int):
        def rd(blk, reg):
            q = self.txq if tx else self.rxq
            return q[i] if i < len(q) else 0
        return rd
