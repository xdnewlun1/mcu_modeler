"""Kinetis SDHC (Secured Digital Host Controller, K22 RM ch. 50), SD memory card side.

Registers from the SVD (DSADDR, BLKATTR, CMDARG, XFERTYP, CMDRSP0-3, DATPORT, PRSSTAT, PROCTL, SYSCTL, IRQSTAT,
IRQSTATEN, IRQSIGEN, AC12ERR, HTCAPBLT, WML, FEVT, ADMAES, ADSADDR, VENDOR, MMCBOOT, HOSTVER); behaviour
hand-written. A card is attached with `attach_card(card)` (chipem.devices.sdcard.SDCard: `--sd-card SDHC:...`,
board `devices: [{type: sd_card, host: SDHC}]`); without one, commands end in CTOE (empty socket, PRSSTAT.CINS = 0).

* Clock: SDHC clock = SIM_SOPT2[SDHCSRC]: 0 core/system clock, 1 MCGFLLCLK or MCGPLLCLK (SOPT2[PLLFLLSEL]),
  2 OSCERCLK (the MCG model's oscillator), 3 SDHC0_CLKIN (params `clkin_hz`, default 0 = no clock).
  SDCLK = SDHC clock / (prescaler x divisor), prescaler = 2 x SYSCTL.SDCLKFS (one bit set; 00h = 1), divisor =
  DVS + 1; it runs while SYSCTL.SDCLKEN = 1. PRSSTAT.SDSTB reads 1 (no settling time). The card clock is gated
  automatically between transfers and when the buffer is full (read) / empty (write), so the buffer never over- or
  underruns (PRSSTAT.SDOFF shows it). INITA sends 80 clocks and self-clears.
* Command (XFERTYP write; ignored while CIHB, or with DPSEL while CDIHB, or with DPSEL and BLKSIZE = 0 or a zero
  block count with MSBSEL + BCEN): CIHB (and CDIHB for data / R1b commands) is set, 48 clocks are sent, the card
  answers after 2 clocks (5 for R2/R3) with 48 / 136 bits. CC at the response end bit; CTOE when no response in 64
  clocks; CCE when CCCEN and the CRC7 is wrong (always for R3, so drivers clear CCCEN there), CIE when CICEN and the
  index field differs; a response of the wrong length fails the CRC/index check. CMDRSP0 = R[39:8]; R2:
  {CMDRSP3[23:0], CMDRSP2, CMDRSP1, CMDRSP0} = R[127:8]; the auto CMD12 response goes to CMDRSP3. R1b: DAT0 busy
  keeps DLA/CDIHB set; its release sets TC (DTOE after the DTOCV timeout).
* Data (DPSEL): reads set RTA/DLA after the command, blocks arrive Nac (card) after the response and between
  blocks, in watermark-sized chunks (RDWML words, at 32 / width clocks per word), CRC16 + end bit per block. Writes
  set WTA/DLA; a block starts once WRWML words (or the rest of the block) are in the buffer, then CRC status and the
  card's busy time. Block count: BLKATTR.BLKCNT decrements per block (BCEN); MSBSEL without BCEN is an infinite
  transfer; AC12EN issues CMD12 after the last block (AC12ERR / AC12E). A bad block CRC (card on another bus
  width than PROCTL.DTW, block size not the card's, DAT lines not muxed, injected error) is DCE; no data within
  SDCLK x 2^(13 + DTOCV) is DTOE. TC: reads when the last word left the buffer, writes when the last busy ends.
* Buffer: 128 words. PRSSTAT.BREN when RDWML words (or the rest of the block) are readable, BWEN when WRWML words
  (or the rest of the block) fit; IRQSTAT.BRR/BWR on their rising edges. DATPORT byte order per PROCTL.EMODE (10
  little endian: the first byte on the bus in bits 7:0; 00 big endian; 01 half-word big endian). The K22 DMAMUX
  has no SDHC request slot, so VENDOR.EXTDMAEN has no effect.
* Internal DMA (XFERTYP.DMAEN): simple DMA (PROCTL.DMAS = 00) moves the data to/from DSADDR (which advances; the
  K22 SDHC has no SDMA boundary); ADMA2 (DMAS = 10) walks the 64-bit descriptor table at ADSADDR (valid, end, int,
  act nop/rsv/tran/link; length 0 = 65536 bytes); DINT when the transfer completes (and for descriptors with Int).
  A descriptor with Valid = 0 or a bus error is DMAE (ADMAES state / ADMADCE, ADMALME on a length mismatch).
  ADMA1 (DMAS = 01) is not modelled: DMAE with one `warn`. The data moves in zero bus time.
* IRQSTAT bits are only set while enabled in IRQSTATEN (clearing an enable clears the status); IRQ "SDHC" =
  (IRQSTAT & IRQSIGEN) != 0 (level). FEVT forces status / AC12ERR bits. CINS/CRM follow the card: CINS is set while
  a card is in the socket (PROCTL.CDSS = 1: PROCTL.CDTL is the card detect level instead).
* Resets: SYSCTL.RSTA (everything except HTCAPBLT/HOSTVER/CINS), RSTC (command circuit: CIHB, CC), RSTD (data:
  buffer, BREN/BWEN/RTA/WTA/DLA/CDIHB, CREQ/SABGREQ, BRR/BWR/DINT/BGE/TC); they self-clear.
* Not modelled (one `warn`): stop at block gap / continue request / read wait (PROCTL.SABGREQ/CREQ/RWCTL), SDIO
  card interrupts, suspend/resume (CMDTYP 01/10), MMC boot (MMCBOOT stays a plain register), 8-bit bus, ADMA1.
* Pins (family signals table): SDHC0_DCLK out L, SDHC0_CMD out H, SDHC0_D0-D3 in. The card sees the bus only when
  DCLK and CMD are muxed to pins; data needs D0 (and D1-D3 at 4 bits). DCLK is summarised by `pin` wave events
  (chipem/periph/sdbus.py), not toggled per edge.
* SIM_SCGC3[SDHC] gate (a gated-off access logs one `warn`).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ...bus import BusError
from ..base import MMIOBlock
from ..registry import register
from ..sdbus import ClockWave, lines_muxed
from ._util import GatedBlock, build_regs, find_model, fmt_hz, irq_number

# XFERTYP
DMAEN, BCEN, AC12EN, DTDSEL, MSBSEL = 1, 2, 4, 0x10, 0x20
CCCEN, CICEN, DPSEL = 1 << 19, 1 << 20, 1 << 21
# PRSSTAT
CIHB, CDIHB, DLA, SDSTB, IPGOFF, HCKOFF, PEROFF, SDOFF, WTA, RTA, BWEN, BREN, CINS_P, CLSL = (
    1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x400, 0x800, 1 << 16, 1 << 23)
# PROCTL
D3CD, CDTL, CDSS, SABGREQ, CREQ, RWCTL, IABG = 8, 0x40, 0x80, 1 << 16, 1 << 17, 1 << 18, 1 << 19
# SYSCTL
SDCLKEN, RSTA, RSTC, RSTD, INITA = 8, 1 << 24, 1 << 25, 1 << 26, 1 << 27
# IRQSTAT
CC, TC, BGE, DINT, BWR, BRR, CINS, CRM, CINT = (1 << i for i in range(9))
CTOE, CCE, CEBE, CIE, DTOE, DCE, DEBE, AC12E, DMAE = (1 << 16, 1 << 17, 1 << 18, 1 << 19, 1 << 20, 1 << 21, 1 << 22,
                                                     1 << 24, 1 << 28)
IRQ_MASK = 0x117F01FF
# AC12ERR
AC12NE, AC12TOE, AC12EBE, AC12CE, AC12IE, CNIBAC12E = 1, 2, 4, 8, 0x10, 0x80
BUF_WORDS = 128


@register("kinetis.sdhc")
class SDHC(GatedBlock):
    model = "kinetis.sdhc"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "SDHC")
        ro = {"ro_mask": 0xFFFFFFFF}
        ov = {"BLKATTR": {"on_read": self._blkattr_read, "on_write": self._blkattr_write},
              "CMDARG": {"on_write": self._cmdarg_write},
              "XFERTYP": {"on_write": self._xfertyp_write},
              "CMDRSP0": ro, "CMDRSP1": ro, "CMDRSP2": ro, "CMDRSP3": ro,
              "PRSSTAT": {**ro, "on_read": self._prsstat_read},
              "PROCTL": {"on_write": self._proctl_write},
              "SYSCTL": {"on_write": self._sysctl_write},
              "IRQSTAT": {"on_read": self._irqstat_read},
              "IRQSTATEN": {"on_write": self._irqen_write}, "IRQSIGEN": {"on_write": self._irqen_write},
              "AC12ERR": ro, "HTCAPBLT": ro, "HOSTVER": ro, "ADMAES": ro,
              "FEVT": {"on_write": self._fevt_write, "on_read": lambda b, r: 0},
              "WML": {"on_write": self._wml_write}}
        r = build_regs((name, "SDHC"), size, ov, params.get("regs_file"), skip=("DATPORT",))
        super().__init__(name, base, size, r, log=log, params=params)
        self.card: Any = None
        self.sig = {"CLK": f"{name}0_DCLK", "CMD": f"{name}0_CMD", **{f"D{i}": f"{name}0_D{i}" for i in range(8)}}
        if name[-1:].isdigit():
            self.sig = {"CLK": f"{name}_DCLK", "CMD": f"{name}_CMD", **{f"D{i}": f"{name}_D{i}" for i in range(8)}}
        self.clkin_hz = float(params.get("clkin_hz", 0.0))
        self._irqn: int | None = None
        self._line = False
        self._warned: set[str] = set()
        self.commands: deque[tuple[float, int, int, str]] = deque(maxlen=10000)
        self.wave = ClockWave(self, self.sig["CLK"])
        self._mcg = None
        self._init_state()

    def _init_state(self) -> None:
        self.buf: deque[int] = deque()
        self.irqstat = 0
        self.cihb = False
        self.cdihb = False
        self.dla = False
        self.rta = False
        self.wta = False
        self.sdoff = False
        self._bren = False
        self._bwen = False
        self._cmd_ev = None
        self._dat_ev = None
        self._dto_ev = None
        self._init_ev = None
        self._x: dict | None = None             # current data transfer
        self._sdclk = 0.0
        self._adma: dict | None = None
        self._pending_cmd: tuple | None = None   # command waiting for SDCLK
        self._init_end = 0.0                     # INITA: end of the 80 clocks

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.name, "SDHC")
        self._mcg = find_model(chip, "kinetis.mcg", "MCG")

    def attach_card(self, card: Any) -> None:
        """Insert an SD card (chipem.devices.sdcard.SDCard) into the socket."""
        self.card = card
        self._update()

    def reset(self) -> None:
        if self.chip is not None:
            for n in ("_cmd_ev", "_dat_ev", "_dto_ev", "_init_ev"):
                ev = getattr(self, n, None)
                if ev is not None:
                    self.chip.sched.cancel(ev)
            if self._line and self._irqn is not None:
                self.irq(self._irqn, False)
        self.wave.reset()
        self._line = False
        self._init_state()
        self._warned.clear()
        super().reset()
        self._clk_update()

    # ------------------------------------------------------------------ helpers
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _now(self) -> float:
        return self.chip.now_ns()

    def _at(self, t: float, cb, *args):
        return self.chip.sched.at_ns(t, cb, *args)

    def _cancel(self, attr: str) -> None:
        ev = getattr(self, attr)
        if ev is not None:
            self.chip.sched.cancel(ev)
            setattr(self, attr, None)

    def _T(self) -> float:
        return 1e9 / self._sdclk if self._sdclk else 0.0

    def bus_width(self) -> int:
        return (1, 4, 8, 1)[(self.get("PROCTL") >> 1) & 3]

    def card_inserted(self) -> bool:
        p = self.get("PROCTL")
        if p & CDSS:
            return bool(p & CDTL)
        return self.card is not None

    # ------------------------------------------------------------------ clocks
    def base_hz(self) -> float:
        """SDHC clock selected by SIM_SOPT2[SDHCSRC]."""
        chip = self.chip
        sim = getattr(self, "_sim", None)
        sopt2 = sim.get("SOPT2") if sim is not None and "SOPT2" in sim.regs else 0
        src = (sopt2 >> 28) & 3
        mcg = self._mcg
        if src == 0:
            return float(chip.clock.hz)
        if src == 1:
            if mcg is None:
                return float(chip.clock.hz)
            return float(mcg.pll_hz() if sopt2 & (1 << 16) else mcg.fll_hz())
        if src == 2:
            return float(getattr(mcg, "ext_hz", 8e6)) if mcg is not None else 8e6
        if not self.clkin_hz:
            self._warn_once("clkin", f"{self.name}: SDHCSRC = 11 (SDHC0_CLKIN) without params.clkin_hz: no clock")
        return self.clkin_hz

    def sdclk_hz(self) -> float:
        sc = self.get("SYSCTL")
        if not sc & SDCLKEN:
            return 0.0
        fs = (sc >> 8) & 0xFF
        if fs & (fs - 1):
            self._warn_once("fs", f"{self.name}: SYSCTL.SDCLKFS 0x{fs:02x} has several bits set (undefined)")
        pre = 1 if fs == 0 else 2 * (1 << (fs.bit_length() - 1))
        div = ((sc >> 4) & 0xF) + 1
        return self.base_hz() / (pre * div)

    def _clk_update(self) -> None:
        if self.chip is None:
            return
        hz = self.sdclk_hz()
        if hz != self._sdclk:
            self._sdclk = hz
            if hz > 50e6 * 1.0001:
                self._warn_once("fast", f"{self.name}: SDCLK {fmt_hz(hz)} is above the 50 MHz limit")
            if hz and self.card is not None and hz > 25e6 * 1.0001 and not getattr(self.card, "high_speed", True):
                self._warn_once("hs", f"{self.name}: SDCLK {fmt_hz(hz)} above 25 MHz while the card is not in "
                                "high-speed mode (CMD6)")
        self.wave.set(hz, gated=True)                     # automatic clock gating: counts bus activity only
        if hz:
            if self.get("SYSCTL") & INITA and self._init_ev is None:
                self._start_inita()
            if self._pending_cmd is not None:
                self._start_cmd(*self._pending_cmd)
            x = self._x
            if x is not None and not x["read"] and x.get("go"):
                self._wr_try()

    # ------------------------------------------------------------------ status / IRQ
    def _set(self, bits: int) -> None:
        self.irqstat |= bits & self.get("IRQSTATEN") & IRQ_MASK
        self._update()

    def _status_bits(self) -> int:
        s = self.irqstat
        en = self.get("IRQSTATEN")
        if self.card_inserted():
            s |= CINS & en
        else:
            s |= CRM & en
        return s

    def _update(self) -> None:
        if self.chip is None:
            return
        bren, bwen = self._buf_ready()
        if bren and not self._bren:
            self.irqstat |= BRR & self.get("IRQSTATEN")
        if bwen and not self._bwen:
            self.irqstat |= BWR & self.get("IRQSTATEN")
        self._bren, self._bwen = bren, bwen
        st = self._status_bits()
        self.set("IRQSTAT", st)
        line = bool(st & self.get("IRQSIGEN"))
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)

    def _buf_ready(self) -> tuple[bool, bool]:
        """(BREN, BWEN): RDWML words readable / WRWML words free, or the rest of the transfer (dynamic watermark)."""
        x = self._x
        if x is None:
            return bool(self.buf) and len(self.buf) >= 1 and self.rta, False
        if x["dma"]:
            return False, False
        left = x["cpu_left"]
        if x["read"]:
            need = self._wml(True) if left is None else min(self._wml(True), left)
            return need > 0 and len(self.buf) >= need, False
        need = self._wml(False) if left is None else min(self._wml(False), left)
        return False, need > 0 and BUF_WORDS - len(self.buf) >= need

    def _prsstat(self) -> int:
        v = SDSTB | CLSL
        if self.cihb:
            v |= CIHB
        if self.cdihb:
            v |= CDIHB
        if self.dla:
            v |= DLA
        if self.wta:
            v |= WTA
        if self.rta:
            v |= RTA
        if self.sdoff or not self._sdclk:
            v |= SDOFF
        if self._bren:
            v |= BREN
        if self._bwen:
            v |= BWEN
        if self.card_inserted():
            v |= CINS_P
        busy = self.card is not None and self.card.busy(self._now())
        dl = 0xFF & ~(1 if busy else 0)
        return v | dl << 24

    def _prsstat_read(self, blk, reg):
        return self._prsstat()

    def _irqstat_read(self, blk, reg):
        return self._status_bits()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        if offset == 0x20:
            self._datport_write(value & 0xFFFFFFFF)
            return
        if 0x30 <= offset < 0x34:                           # IRQSTAT: write 1 to clear (any access size)
            sh = 8 * (offset - 0x30)
            self.irqstat &= ~(((value & ((1 << (8 * size)) - 1)) << sh) & IRQ_MASK)
            self._update()
            return
        MMIOBlock.write(self, offset, size, value)

    def read(self, offset: int, size: int) -> int:
        if self._gate_loc is not None:
            self._gate_ok()
        if offset == 0x20:
            return self._datport_read()
        return MMIOBlock.read(self, offset, size)

    def _irqen_write(self, blk, reg, old, new):
        self._store(reg, new)
        if reg.name == "IRQSTATEN":
            self.irqstat &= new | ~IRQ_MASK
        self._update()
        return None

    def _fevt_write(self, blk, reg, old, new):
        ac = 0
        for fbit, abit in ((0, AC12NE), (1, AC12TOE), (2, AC12CE), (3, AC12EBE), (4, AC12IE), (7, CNIBAC12E)):
            if new >> fbit & 1:
                ac |= abit
        if ac:
            self.set("AC12ERR", self.get("AC12ERR") | ac)
        bits = new & 0x117F0000
        if new >> 31 & 1:
            bits |= CINT
        self._set(bits)
        return 0

    def _wml_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    def _wml(self, read: bool) -> int:
        w = self.get("WML")
        v = (w & 0xFF) if read else ((w >> 16) & 0xFF)
        return min(max(v, 1), BUF_WORDS)

    # ------------------------------------------------------------------ register hooks
    def _busy_data(self) -> bool:
        return self.cdihb

    def _blkattr_read(self, blk, reg):
        v = self.get("BLKATTR")
        x = self._x
        msb = (x["xfer"] if x is not None else self.get("XFERTYP")) & MSBSEL
        if not msb:
            v = (v & 0xFFFF) | (1 << 16)
        return v

    def _blkattr_write(self, blk, reg, old, new):
        if self._x is not None:
            return old                                   # ignored during a transfer
        return None

    def _cmdarg_write(self, blk, reg, old, new):
        if self.cihb:
            return old                                   # write protected while CIHB
        return None

    def _proctl_write(self, blk, reg, old, new):
        self._store(reg, new & ~CREQ)                    # CREQ self-clears
        if new & (SABGREQ | CREQ | RWCTL):
            self._warn_once("bgap", f"{self.name}: stop at block gap / continue request / read wait are not modelled")
        if (new >> 8) & 3 == 3:
            self._warn_once("dmas", f"{self.name}: PROCTL.DMAS = 11 is reserved")
        if (new >> 1) & 3 == 2:
            self._warn_once("8bit", f"{self.name}: 8-bit bus (DTW = 10): an SD card has 4 data lines")
        self._update()
        return self.get("PROCTL")

    def _sysctl_write(self, blk, reg, old, new):
        rst = new & (RSTA | RSTC | RSTD)
        inita = new & INITA and not old & INITA
        if inita and (self.cihb or self.cdihb):
            inita = False                                # ignored while the bus is active
        keep_inita = old & INITA
        self._store(reg, (new & ~(RSTA | RSTC | RSTD | INITA)) | keep_inita | (INITA if inita else 0))
        if rst & RSTA:
            self._reset_all()
            return self.get("SYSCTL")
        if rst & RSTC:
            self._reset_cmd()
        if rst & RSTD:
            self._reset_data()
        self._clk_update()
        if inita:
            self._start_inita()
        self._update()
        return self.get("SYSCTL")

    def _start_inita(self) -> None:
        if not self._sdclk:
            return                                       # stays set until a clock runs
        self.wave.active(80)
        self._init_end = self._now() + 80 * self._T()
        self._init_ev = self._at(self._init_end, self._inita_done)

    def _inita_done(self) -> None:
        self._init_ev = None
        self.set("SYSCTL", self.get("SYSCTL") & ~INITA)

    def _reset_all(self) -> None:
        keep = {n: self.get(n) for n in ("HTCAPBLT", "HOSTVER")}
        for n in ("_cmd_ev", "_dat_ev", "_dto_ev", "_init_ev"):
            self._cancel(n)
        self.wave.reset()
        line = self._line
        self._init_state()
        MMIOBlock.reset(self)
        for n, v in keep.items():
            self.set(n, v)
        self._line = line
        self.emit("info", block=self.name, msg=f"{self.name}: software reset for all (SYSCTL.RSTA)")
        self._clk_update()
        self._update()

    def _reset_cmd(self) -> None:
        self._cancel("_cmd_ev")
        self.cihb = False
        self.irqstat &= ~CC

    def _reset_data(self) -> None:
        self._cancel("_dat_ev")
        self._cancel("_dto_ev")
        self.buf.clear()
        self._x = None
        self._adma = None
        self.rta = self.wta = self.dla = self.cdihb = False
        self.sdoff = False
        self.set("PROCTL", self.get("PROCTL") & ~(SABGREQ | CREQ))
        self.irqstat &= ~(BRR | BWR | DINT | BGE | TC)
        self._bren = self._bwen = False

    # ------------------------------------------------------------------ command path
    def _xfertyp_write(self, blk, reg, old, new):
        if self.cihb:
            self._warn_once("cihb", f"{self.name}: XFERTYP written while PRSSTAT.CIHB = 1: ignored")
            return old
        dps = bool(new & DPSEL)
        busy = (new >> 16) & 3 == 3
        if (dps or busy) and self.cdihb and (new >> 24) & 0x3F not in (0, 12, 13):
            self._warn_once("cdihb", f"{self.name}: command with data/busy while PRSSTAT.CDIHB = 1: ignored")
            return old
        if dps:
            ba = self.get("BLKATTR")
            if ba & 0x1FFF == 0 or (new & MSBSEL and new & BCEN and not ba >> 16):
                self._warn_once("blk0", f"{self.name}: data command with BLKSIZE = 0 or BLKCNT = 0: not sent")
                return old
        if (new >> 22) & 3 in (1, 2):
            self._warn_once("susp", f"{self.name}: suspend/resume command types are not modelled")
        self._store(reg, new)
        self.cihb = True
        if dps or busy:
            self.cdihb = True
        if dps:
            self._setup_data(new)
        if not self._sdclk:
            self._warn_once("noclk", f"{self.name}: command issued while SDCLK is off (SYSCTL.SDCLKEN = 0 or no "
                            "SDHC clock): waits for the clock")
        self._start_cmd(new, self.get("CMDARG"))
        self._update()
        return None

    def _start_cmd(self, xfer: int, arg: int, auto12: bool = False) -> None:
        T = self._T()
        if not T:
            self._cmd_ev = None
            self._pending_cmd = (xfer, arg, auto12)
            return
        self._pending_cmd = None
        extra = 0.0
        if self.get("SYSCTL") & INITA and self._init_ev is not None:
            extra = max(0.0, self._init_end - self._now())   # the command follows the 80 init clocks
        self.wave.active(48)
        self._cmd_ev = self._at(self._now() + extra + 48 * T, self._cmd_at_card, xfer, arg, auto12)

    def _cmd_at_card(self, xfer: int, arg: int, auto12: bool) -> None:
        self._cmd_ev = None
        t = self._now()
        T = self._T() or 1.0
        idx = (xfer >> 24) & 0x3F
        rsp = (xfer >> 16) & 3
        resp = None
        if self.card is not None:
            if lines_muxed(self.chip, (self.sig["CLK"], self.sig["CMD"])):
                resp = self.card.command(idx, arg, t)
            else:
                self._warn_once("nowire", f"{self.name}: SDHC0_DCLK / SDHC0_CMD not muxed to a pin: the card does "
                                "not see the command")
        if resp is not None and resp.data == "read" and self._x is not None and not auto12:
            ncr = 5 if resp.nbits == 136 or resp.kind == "R3" else 2
            self._x["card_t"] = t + (ncr + resp.nbits) * T + max(self.card.read_ready_ns, 2 * T)
        if rsp == 0:
            self.commands.append((t, idx, arg, "sent"))
            self.wave.active(8)
            self._cmd_ev = self._at(t + 8 * T, self._cmd_done, idx, None, xfer, auto12)
            return
        if resp is None:
            self.commands.append((t, idx, arg, "timeout"))
            self.wave.active(64)
            self._cmd_ev = self._at(t + 64 * T, self._cmd_done, idx, None, xfer, auto12, True)
            return
        ncr = 5 if resp.nbits == 136 or resp.kind == "R3" else 2
        self.commands.append((t, idx, arg, resp.kind))
        self.wave.active(ncr + resp.nbits)
        self._cmd_ev = self._at(t + (ncr + resp.nbits) * T, self._cmd_done, idx, resp, xfer, auto12)

    def _cmd_done(self, idx: int, resp: Any, xfer: int, auto12: bool, timeout: bool = False) -> None:
        self._cmd_ev = None
        rsp = (xfer >> 16) & 3
        err = 0
        if timeout:
            err = CTOE
        elif resp is not None:
            long = rsp == 1
            if long:
                if resp.nbits == 136:
                    v = resp.payload >> 8
                else:
                    v = (resp.frame >> 8) & 0xFFFFFFFF
                    err |= CCE if xfer & CCCEN else 0
                self.set("CMDRSP0", v & 0xFFFFFFFF)
                self.set("CMDRSP1", (v >> 32) & 0xFFFFFFFF)
                self.set("CMDRSP2", (v >> 64) & 0xFFFFFFFF)
                self.set("CMDRSP3", (self.get("CMDRSP3") & 0xFF000000) | ((v >> 96) & 0xFFFFFF))
                if resp.nbits == 136 and xfer & CCCEN and not resp.crc_ok():
                    err |= CCE
            else:
                if resp.nbits == 136:
                    frame = resp.frame >> 88
                    payload, index, crc_ok = (frame >> 8) & 0xFFFFFFFF, (frame >> 40) & 0x3F, False
                else:
                    payload, index, crc_ok = resp.payload, resp.index, resp.crc_ok()
                self.set("CMDRSP3" if auto12 else "CMDRSP0", payload)
                if xfer & CCCEN and not crc_ok:
                    err |= CCE
                if xfer & CICEN and index != idx:
                    err |= CIE
        if auto12:
            self._auto12_done(err)
            return
        self.cihb = False
        if err & CTOE:
            self._set(CTOE)
            if self._x is not None:
                self._abort_data()                       # no response: the data transfer is abandoned
            elif rsp == 3:
                self.cdihb = self.dla = False
            self._update()
            return
        self._set(CC | err)                              # CC with CCE / CIE: response received with an error
        if rsp == 3 and not xfer & DPSEL:
            busy = resp.busy_ns if resp is not None else 0.0
            self.dla = True
            self._arm_timeout()
            self._dat_ev = self._at(self._now() + max(busy, 0.0), self._r1b_done)
        elif xfer & DPSEL and self._x is not None:
            self._data_start()
        self._update()

    def _r1b_done(self) -> None:
        self._dat_ev = None
        t = self._now()
        if self.card is not None and self.card.busy(t):
            self._dat_ev = self._at(self.card.busy_until, self._r1b_done)
            return
        self._cancel("_dto_ev")
        self.cdihb = self.dla = False
        self._set(TC)

    # ------------------------------------------------------------------ data path
    def _setup_data(self, xfer: int) -> None:
        ba = self.get("BLKATTR")
        bs = ba & 0x1FFF
        if xfer & MSBSEL:
            n = (ba >> 16) if xfer & BCEN else None
        else:
            n = 1
        dma = bool(xfer & DMAEN)
        mode = (self.get("PROCTL") >> 8) & 3 if dma else None
        self._x = {"xfer": xfer, "read": bool(xfer & DTDSEL), "bs": bs, "left": n, "dma": dma, "mode": mode,
                   "addr": self.get("DSADDR"), "rd_block_words": 0, "blk": None, "card_t": None, "started": False,
                   "done_blocks": 0, "sending": False,
                   "cpu_left": None if n is None else n * ((bs + 3) // 4)}   # words the CPU still moves
        self.buf.clear()
        if dma and mode == 1:
            self._warn_once("adma1", f"{self.name}: ADMA1 is not modelled: DMA error")
        if dma and mode == 2:
            self._adma = {"desc": self.get("ADSADDR") & ~3, "addr": 0, "len": 0, "end": False, "int": False}

    def _abort_data(self) -> None:
        self._cancel("_dat_ev")
        self._cancel("_dto_ev")
        self._x = None
        self._adma = None
        self.rta = self.wta = self.dla = self.cdihb = False
        self.sdoff = False

    def _arm_timeout(self) -> None:
        self._cancel("_dto_ev")
        T = self._T()
        if T:
            dtocv = (self.get("SYSCTL") >> 16) & 0xF
            if dtocv == 15:
                self._warn_once("dtocv", f"{self.name}: SYSCTL.DTOCV = 1111b is reserved")
            self._dto_ev = self._at(self._now() + (1 << (13 + min(dtocv, 14))) * T, self._dtimeout)

    def _dtimeout(self) -> None:
        self._dto_ev = None
        self._cancel("_dat_ev")
        self._x = None
        self._adma = None
        self.dla = self.rta = self.wta = self.cdihb = False
        self.sdoff = False
        self._set(DTOE)

    def _data_start(self) -> None:
        x = self._x
        if x["dma"] and x["mode"] == 1:
            self._dma_error(0)
            return
        self.dla = True
        x["go"] = True
        if x["read"]:
            self.rta = True
            self._arm_timeout()
            t = x["card_t"]
            if t is not None:
                self._dat_ev = self._at(max(t, self._now()), self._rd_block_start)
        else:
            self.wta = True
            self._wr_try()
        self._update()

    def _endian_word(self, b: bytes) -> int:
        b = bytes(b).ljust(4, b"\0")
        mode = (self.get("PROCTL") >> 4) & 3
        if mode == 2:
            return int.from_bytes(b, "little")
        if mode == 0:
            return int.from_bytes(b, "big")
        return b[1] | b[0] << 8 | b[3] << 16 | b[2] << 24

    def _word_bytes(self, w: int) -> bytes:
        mode = (self.get("PROCTL") >> 4) & 3
        if mode == 2:
            return w.to_bytes(4, "little")
        if mode == 0:
            return w.to_bytes(4, "big")
        return bytes(((w >> 8) & 0xFF, w & 0xFF, (w >> 24) & 0xFF, (w >> 16) & 0xFF))

    def _lines_ok(self) -> bool:
        w = self.bus_width()
        need = [self.sig["D0"]] if w == 1 else [self.sig[f"D{i}"] for i in range(min(w, 8))]
        ok = lines_muxed(self.chip, need)
        if not ok:
            self._warn_once("nodat", f"{self.name}: data line(s) not all muxed to pins: blocks arrive corrupted")
        return ok

    # ---- read
    def _rd_block_start(self) -> None:
        self._dat_ev = None
        x = self._x
        card = self.card
        if x is None or card is None or not self._sdclk:
            return
        t = self._now()
        width = self.bus_width()
        data = card.read_block(t, width)
        if data is None:
            return                                       # the card sends nothing: DTOE eventually
        self._cancel("_dto_ev")
        bs = x["bs"]
        bad = card.take_crc_error() or card.bus_width != width or len(data) != bs or not self._lines_ok()
        buf = bytes(data[:bs]).ljust(bs, b"\xff")
        x["blk"] = {"buf": buf, "pos": 0, "bad": bad}
        self._rd_chunk()

    def _rd_chunk(self) -> None:
        self._dat_ev = None
        x = self._x
        if x is None or x["blk"] is None:
            return
        T = self._T()
        if not T:
            return
        b = x["blk"]
        left_words = (len(b["buf"]) - b["pos"] + 3) // 4
        n = min(self._wml(True), left_words)
        if BUF_WORDS - len(self.buf) < n:
            self.sdoff = True                            # buffer full: clock gated until there is room
            self._update()
            return
        self.sdoff = False
        width = self.bus_width()
        clocks = n * 32 / width + (1 if b["pos"] == 0 else 0)
        self.wave.active(clocks)
        self._dat_ev = self._at(self._now() + clocks * T, self._rd_chunk_done, n)

    def _rd_chunk_done(self, n: int) -> None:
        self._dat_ev = None
        x = self._x
        if x is None:
            return
        b = x["blk"]
        for _ in range(n):
            chunk = b["buf"][b["pos"]:b["pos"] + 4]
            b["pos"] += 4
            self.buf.append(self._endian_word(chunk))
        if x["dma"]:
            self._dma_drain()
            if self._x is None:
                return
        if b["pos"] < len(b["buf"]):
            self._rd_chunk()
        else:
            self.wave.active(17)
            self._dat_ev = self._at(self._now() + 17 * self._T(), self._rd_block_end)
        self._update()

    def _rd_block_end(self) -> None:
        self._dat_ev = None
        x = self._x
        if x is None:
            return
        b = x["blk"]
        x["blk"] = None
        if b["bad"]:
            self._abort_data()
            self._set(DCE)
            return
        x["done_blocks"] += 1
        if x["left"] is not None:
            x["left"] -= 1
            if x["xfer"] & BCEN:
                ba = self.get("BLKATTR")
                self.set("BLKATTR", (ba & 0xFFFF) | (max(0, (ba >> 16) - 1) << 16))
        if x["left"] == 0:
            self.dla = False
            if x["xfer"] & AC12EN and x["xfer"] & MSBSEL and x["xfer"] & BCEN:
                self._issue_auto12()
            self._rd_check_done()
        else:
            self._arm_timeout()
            T = self._T() or 1.0
            self._dat_ev = self._at(self._now() + max(self.card.read_ready_ns, 2 * T), self._rd_block_start)
        self._update()

    def _rd_check_done(self) -> None:
        x = self._x
        if x is None or not x["read"] or x["left"] != 0 or self.buf or self._cmd_ev is not None and x.get("a12"):
            return
        self._x = None
        self.rta = False
        self.cdihb = False
        self._set(TC | (DINT if x["dma"] else 0))

    def _datport_read(self) -> int:
        x = self._x
        if (x is not None and x["dma"]) or not self.buf:
            return 0
        w = self.buf.popleft()
        if x is not None and x["cpu_left"]:
            x["cpu_left"] -= 1
        if x is not None and x["blk"] is not None and self.sdoff:
            self._rd_chunk()                            # room again: the clock restarts
        if x is not None:
            self._rd_check_done()
        self._update()
        return w

    # ---- write
    def _datport_write(self, v: int) -> None:
        x = self._x
        if x is None or x["read"] or x["dma"]:
            return
        if len(self.buf) >= BUF_WORDS:
            self._warn_once("bufful", f"{self.name}: DATPORT write while the buffer is full: dropped")
            return
        self.buf.append(v)
        if x["cpu_left"]:
            x["cpu_left"] -= 1
        if x["blk"] is not None and self.sdoff and self._dat_ev is None:
            self._wr_chunk()                            # data again: the clock restarts
        else:
            self._wr_try()
        self._update()

    def _block_words(self, x: dict) -> int:
        return (x["bs"] + 3) // 4

    def _wr_try(self) -> None:
        x = self._x
        if x is None or x["read"] or self._dat_ev is not None or not self._sdclk or not x.get("go"):
            return
        if x["left"] == 0:
            return
        if x["dma"]:
            self._dma_fill()
            if self._x is None:
                return
        blk = x["blk"]
        if blk is None:
            card = self.card
            t = self._now()
            if card is not None and card.busy(t):
                self._dat_ev = self._at(card.busy_until, self._wr_resume)
                return
            need = min(self._wml(False), self._block_words(x))
            if len(self.buf) < need:
                return                                   # waits for WRWML words
            x["blk"] = blk = {"data": bytearray(), "words": 0}
            x["started"] = True
            self.wave.active(3)
            self._dat_ev = self._at(t + 3 * self._T(), self._wr_chunk)
            return

    def _wr_resume(self) -> None:
        self._dat_ev = None
        x = self._x
        if x is not None and x["blk"] is not None:
            self._wr_chunk()
        else:
            self._wr_try()

    def _wr_chunk(self) -> None:
        self._dat_ev = None
        x = self._x
        if x is None:
            return
        if x["dma"]:
            self._dma_fill()
            if self._x is None:
                return
        blk = x["blk"]
        left = self._block_words(x) - blk["words"]
        n = min(len(self.buf), left, max(self._wml(False), 1))
        if n == 0:
            self.sdoff = True                            # buffer empty: clock gated until the CPU writes
            self._update()
            return
        self.sdoff = False
        for _ in range(n):
            blk["data"] += self._word_bytes(self.buf.popleft())
        blk["words"] += n
        clocks = n * 32 / self.bus_width()
        self.wave.active(clocks)
        if blk["words"] < self._block_words(x):
            self._dat_ev = self._at(self._now() + clocks * self._T(), self._wr_chunk)
        else:
            self.wave.active(24)
            self._dat_ev = self._at(self._now() + (clocks + 24) * self._T(), self._wr_block_end)
        self._update()

    def _wr_block_end(self) -> None:
        self._dat_ev = None
        x = self._x
        if x is None:
            return
        blk = x["blk"]
        x["blk"] = None
        card = self.card
        t = self._now()
        data = bytes(blk["data"][:x["bs"]])
        if card is None or not card.write_expected():
            self._arm_timeout()                          # no CRC status token: DTOE
            return
        crc_ok = x["bs"] == 512 and self._lines_ok()
        ok, busy = card.write_block(data, t, self.bus_width(), crc_ok=crc_ok)
        if not ok:
            self._abort_data()
            self._set(DCE)
            return
        x["done_blocks"] += 1
        if x["left"] is not None:
            x["left"] -= 1
            if x["xfer"] & BCEN:
                ba = self.get("BLKATTR")
                self.set("BLKATTR", (ba & 0xFFFF) | (max(0, (ba >> 16) - 1) << 16))
        if x["left"] == 0:
            self.wta = False
            if x["xfer"] & AC12EN and x["xfer"] & MSBSEL and x["xfer"] & BCEN:
                self._issue_auto12()
        self._arm_timeout()
        self._dat_ev = self._at(t + busy, self._wr_busy_end)
        self._update()

    def _wr_busy_end(self) -> None:
        self._dat_ev = None
        x = self._x
        if x is None:
            return
        t = self._now()
        if self.card is not None and self.card.busy(t):
            self._dat_ev = self._at(self.card.busy_until, self._wr_busy_end)
            return
        self._cancel("_dto_ev")
        if x["left"] == 0:
            self._x = None
            self.dla = self.cdihb = False
            self._set(TC | (DINT if x["dma"] else 0))
            return
        self._wr_try()
        self._update()

    # ---- auto CMD12
    def _issue_auto12(self) -> None:
        x = self._x
        if x is not None:
            x["a12"] = True
        xfer = (12 << 24) | (3 << 16) | CCCEN | CICEN
        if self._cmd_ev is not None:
            self.set("AC12ERR", self.get("AC12ERR") | CNIBAC12E)
            return
        self._start_cmd(xfer, 0, auto12=True)

    def _auto12_done(self, err: int) -> None:
        ac = 0
        if err & CTOE:
            ac |= AC12TOE
        if err & CCE:
            ac |= AC12CE
        if err & CIE:
            ac |= AC12IE
        self.set("AC12ERR", ac)
        if ac:
            self._set(AC12E)
        x = self._x
        if x is not None:
            x["a12"] = False
            if x["read"]:
                self._rd_check_done()
        self._update()

    # ------------------------------------------------------------------ internal DMA
    def _dma_error(self, state: int, desc_err: bool = False, len_err: bool = False) -> None:
        self.set("ADMAES", state | (4 if len_err else 0) | (8 if desc_err else 0))
        self._abort_data()
        self._set(DMAE)

    def _adma_next(self) -> bool:
        """Fetch ADMA2 descriptors until a tran one with data left. False on error / end of table."""
        a = self._adma
        bus = self.chip.bus
        for _ in range(4096):
            if a["len"]:
                return True
            if a["end"]:
                return False
            d = a["desc"]
            try:
                lo = bus.read(d, 4)
                hi = bus.read(d + 4, 4)
            except BusError:
                self._dma_error(1)
                return False
            if not lo & 1:
                self.set("ADSADDR", d)
                self._dma_error(1, desc_err=True)
                return False
            act = (lo >> 4) & 3
            a["end"] = bool(lo & 2)
            a["int"] = bool(lo & 4)
            a["desc"] = d + 8
            self.set("ADSADDR", a["desc"])
            if act == 2:
                a["addr"] = hi & ~3
                a["len"] = (lo >> 16) or 0x10000
            elif act == 3:
                a["desc"] = hi & ~3
                self.set("ADSADDR", a["desc"])
                if a["end"]:
                    return False
        return False

    def _dma_take(self, n: int) -> list[tuple[int, int]] | None:
        """(address, bytes) pieces for the next n bytes of DMA data, advancing DSADDR / the ADMA pointers."""
        x = self._x
        if x["mode"] == 0:
            a = x["addr"]
            x["addr"] = (a + n) & 0xFFFFFFFF
            self.set("DSADDR", x["addr"])
            return [(a, n)]
        out = []
        while n:
            if not self._adma_next():
                if self._x is not None:
                    self._dma_error(3, len_err=True)
                return None
            a = self._adma
            k = min(n, a["len"])
            out.append((a["addr"], k))
            a["addr"] += k
            a["len"] -= k
            n -= k
            if not a["len"] and a["int"]:
                self._set(DINT)
        return out

    def _dma_drain(self) -> None:
        """Read transfer: the DMA moves every buffered word to memory."""
        bus = self.chip.bus
        while self.buf and self._x is not None:
            data = self._word_bytes(self.buf.popleft())
            pieces = self._dma_take(4)
            if pieces is None:
                return
            try:
                off = 0
                for a, k in pieces:
                    bus.write_bytes(a, data[off:off + k])
                    off += k
            except BusError:
                self._dma_error(3)
                return

    def _dma_fill(self) -> None:
        """Write transfer: the DMA fills the buffer with the rest of the current block (or what fits)."""
        x = self._x
        bus = self.chip.bus
        words = self._block_words(x) - (x["blk"]["words"] if x["blk"] is not None else 0) - len(self.buf)
        words = min(words, BUF_WORDS - len(self.buf))
        for _ in range(max(0, words)):
            pieces = self._dma_take(4)
            if pieces is None:
                return
            try:
                data = b"".join(bus.read_bytes(a, k) for a, k in pieces)
            except BusError:
                self._dma_error(3)
                return
            self.buf.append(self._endian_word(data))
