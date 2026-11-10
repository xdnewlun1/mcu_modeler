"""Vendor-free SD memory card (SD Physical Layer Simplified Specification v2.00/3.01 subset, native SD bus).

The card is a board device attached to an SD host controller model (`stm32.sdio`, `kinetis.sdhc`): the host calls
the methods below with the virtual time of the bus event, the card answers like silicon and keeps its own state.
It never touches pins or the scheduler; bus timing (clock periods, bus width) is the host's business.

Host interface
--------------
    card.command(index, arg, t_ns) -> Response | None      # None: no response (illegal / ignored command, CMD0,
                                                            # CMD4/15, or a card not yet powered: the host times out)
    Response.kind ("R1", "R1b", "R2", "R3", "R6", "R7"), .frame (48- or 136-bit int: start bit .. end bit, real CRC7;
    R2 carries CID/CSD with their internal CRC7, R3 has "1111111" in the CRC field), .nbits, .index (command field,
    0x3F for R2/R3), .status / .payload (32-bit content; R2: 128-bit register), .busy_ns (R1b: DAT0 busy after
    the response), .data ("read" | "write" | None: a data phase follows)
    card.read_block(t_ns, width) -> bytes | None            # next block the card sends on DAT (None: nothing to send)
    card.read_ready_ns                                      # card access time Nac before each read block (ns)
    card.write_block(data, t_ns, width) -> (ok, busy_ns)    # host sent one block: CRC status token positive (ok) or
                                                            # negative ("crc" error), then DAT0 busy for busy_ns
    card.busy(t_ns) -> bool                                 # DAT0 held low (programming / erase / R1b busy)
    card.bus_width (1 or 4; ACMD6), card.high_speed (CMD6 function 1 of group 1)

State machine (SD spec 4.3, Table 4-36/4-37): idle -> (ACMD41 ready) ready -> (CMD2) ident -> (CMD3) stby ->
(CMD7) tran -> data (CMD17/18/CMD6/ACMD13/ACMD51) / rcv (CMD24/25) -> prg -> tran; CMD7 with another RCA
deselects (tran -> stby, prg -> dis); CMD12 ends a multiple-block transfer (data -> tran, rcv -> prg); CMD15 -> ina.
Commands: CMD0, 2, 3, 4 (ignored), 6, 7, 8, 9, 10, 12, 13, 15, 16, 17, 18, 23, 24, 25, 32, 33, 38, 55;
ACMD6, 13, 22, 23, 41, 42, 51. Anything else, or a command not allowed in the current state, gets no response
and sets ILLEGAL_COMMAND, reported in the status of the next valid command (clear condition B). Error bits of
clear condition C (OUT_OF_RANGE, ADDRESS_ERROR, BLOCK_LEN_ERROR, ERASE_SEQ_ERROR, ERASE_PARAM, WP_VIOLATION,
CARD_IS_LOCKED, CC_ERROR, ERROR, APP_CMD...) are cleared once sent.

Capacity: `kind="sdhc"` (CSD v2, block addressing, CCS = 1 in the OCR when the host sent HCS) or `"sdsc"` (CSD v1,
byte addressing, C_SIZE/C_SIZE_MULT/READ_BL_LEN chosen for the size). `version=1` makes a v1.x SDSC card that
ignores CMD8 (illegal) and CMD6. An SDHC card only leaves the busy state of ACMD41 when the host sets HCS and sent
CMD8 first (spec 4.2.3.1). Initialisation completes `init_ns` after the first ACMD41 (default 1 ms, real cards up
to 1 s). Storage is sparse (512-byte blocks, unwritten blocks read 0x00 = DATA_STAT_AFTER_ERASE 0), optionally
loaded from an image file and written back to `save` (or to the image with `writeback=True`) by `save_image()`
(called when the session closes).

Not modelled: SPI mode, CMD42 lock/unlock (command class 7 is not in the CSD CCC: CMD42 is illegal), write
protection, UHS-I voltage switch (S18R/S18A are 0), SDIO functions, card detect pull-up (ACMD42 just records the
setting), speed-class timing. Commands sent with a CRC error cannot happen (host models always generate CRC7).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

BLOCK = 512

# card status bits (SD spec Table 4-42)
OUT_OF_RANGE, ADDRESS_ERROR, BLOCK_LEN_ERROR, ERASE_SEQ_ERROR = 1 << 31, 1 << 30, 1 << 29, 1 << 28
ERASE_PARAM, WP_VIOLATION, CARD_IS_LOCKED, LOCK_UNLOCK_FAILED = 1 << 27, 1 << 26, 1 << 25, 1 << 24
COM_CRC_ERROR, ILLEGAL_COMMAND, CARD_ECC_FAILED, CC_ERROR, ERROR = 1 << 23, 1 << 22, 1 << 21, 1 << 20, 1 << 19
CSD_OVERWRITE, WP_ERASE_SKIP, CARD_ECC_DISABLED, ERASE_RESET = 1 << 16, 1 << 15, 1 << 14, 1 << 13
READY_FOR_DATA, APP_CMD, AKE_SEQ_ERROR = 1 << 8, 1 << 5, 1 << 3
CLEAR_ON_READ = (OUT_OF_RANGE | ADDRESS_ERROR | BLOCK_LEN_ERROR | ERASE_SEQ_ERROR | ERASE_PARAM | WP_VIOLATION
                 | LOCK_UNLOCK_FAILED | CARD_ECC_FAILED | CC_ERROR | ERROR | CSD_OVERWRITE | WP_ERASE_SKIP
                 | ERASE_RESET | APP_CMD | AKE_SEQ_ERROR)

STATES = ("idle", "ready", "ident", "stby", "tran", "data", "rcv", "prg", "dis")
IDLE, READY, IDENT, STBY, TRAN, DATA, RCV, PRG, DIS = range(9)
INA = 15                                              # inactive (not a CURRENT_STATE value; no responses)

OCR_VOLTAGE = 0x00FF8000                              # 2.7-3.6 V
OCR_CCS, OCR_BUSY = 1 << 30, 1 << 31


# ------------------------------------------------------------------------------------------------ CRCs
def crc7(data: int, nbits: int) -> int:
    """CRC7 (x^7 + x^3 + 1) over the `nbits` most significant bits of `data`, MSB first."""
    crc = 0
    for i in range(nbits - 1, -1, -1):
        fb = ((crc >> 6) & 1) ^ ((data >> i) & 1)
        crc = (crc << 1) & 0x7F
        if fb:
            crc ^= 0x09
    return crc


def _crc16_table() -> list[int]:
    t = []
    for b in range(256):
        c = b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
        t.append(c & 0xFFFF)
    return t


_T16 = _crc16_table()


def crc16(data: bytes) -> int:
    """CRC16-CCITT (x^16 + x^12 + x^5 + 1, initial 0) as used on each DAT line."""
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _T16[(c >> 8) ^ b]
    return c


def crc16_lines(data: bytes, width: int) -> list[int]:
    """Per-line CRC16 of a data block on a 1- or 4-bit bus (4-bit: each DAT line carries one bit of every nibble,
    high nibble first). Returned in line order DAT0..DAT(width-1)."""
    if width == 1:
        return [crc16(data)]
    out = []
    for line in range(4):
        bits = []
        for b in data:
            bits.append((b >> (4 + line)) & 1)
            bits.append((b >> line) & 1)
        packed = bytearray()
        for i in range(0, len(bits), 8):
            v = 0
            for x in bits[i:i + 8]:
                v = (v << 1) | x
            packed.append(v)
        out.append(crc16(bytes(packed)))
    return out


def _parse_size(v: Any) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip().upper().removesuffix("B")
    mult = 1
    for suf, m in (("K", 1 << 10), ("M", 1 << 20), ("G", 1 << 30)):
        if s.endswith(suf):
            s, mult = s[:-1], m
    return int(float(s) * mult) if "." in s else int(s, 0) * mult


def _parse_time_ns(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    from ..session import parse_time
    return float(parse_time(str(v)))


def _bits(fields: list[tuple[int, int, int]], nbits: int) -> int:
    """Pack (msb, lsb, value) fields into an nbits-wide register."""
    r = 0
    for msb, lsb, v in fields:
        w = msb - lsb + 1
        r |= (int(v) & ((1 << w) - 1)) << lsb
    return r & ((1 << nbits) - 1)


@dataclass
class Response:
    kind: str
    index: int                  # command index field of the response (0x3F for R2 / R3)
    payload: int                # 32-bit content (R2: 128-bit register incl. its CRC7 and end bit 1)
    frame: int                  # whole token, start bit first (MSB)
    nbits: int                  # 48 or 136
    busy_ns: float = 0.0        # R1b: DAT0 busy after the response
    data: str | None = None     # "read" | "write" | None

    @property
    def status(self) -> int:
        return self.payload

    def crc_ok(self) -> bool:
        """What a host's CRC7 check of this response finds (R3 has no CRC: the field is all ones)."""
        if self.nbits == 136:
            reg = self.payload
            return crc7(reg >> 8, 120) == (reg >> 1) & 0x7F
        return crc7(self.frame >> 8, 40) == (self.frame >> 1) & 0x7F


def _frame48(index: int, payload: int, crc: int | None = None) -> int:
    head = (0 << 47) | (0 << 46) | ((index & 0x3F) << 40) | ((payload & 0xFFFFFFFF) << 8)
    c = crc7(head >> 8, 40) if crc is None else crc
    return head | (c << 1) | 1


class SDCard:
    """An SD memory card. See the module docstring."""

    def __init__(self, size: int | str | None = None, *, kind: str | None = None, version: int = 2,
                 image: str | Path | None = None, save: str | Path | None = None, writeback: bool = False,
                 init_time: Any = 1e6, busy: Any = 250e3, read_time: Any = 50e3, erase_time: Any = 1e6,
                 name: str = "sd_card", mid: int = 0x45, oid: str = "CE", pnm: str = "CHPEM", psn: int = 0x0C1D0001,
                 crc_errors: int = 0):
        data = b""
        if image is not None:
            data = Path(image).read_bytes()
        if size is None:
            size = max(len(data), 64 << 20) if data else (1 << 30)
        size = _parse_size(size)
        kind = (str(kind).lower() if kind else "sdhc").replace("sdxc", "sdhc")
        if kind not in ("sdhc", "sdsc"):
            raise ValueError(f"sd card: kind must be sdhc or sdsc, not {kind!r}")
        if kind == "sdsc" and size > (2 << 30):
            raise ValueError("sd card: an SDSC card holds at most 2 GB (use kind=sdhc)")
        self.version = int(version)
        if self.version == 1 and kind == "sdhc":
            raise ValueError("sd card: a version 1.x card cannot be SDHC")
        self.name = name
        self.kind = kind
        self.sdhc = kind == "sdhc"
        self.image_path = Path(image) if image is not None else None
        self.save_path = Path(save) if save else (self.image_path if writeback else None)
        self.init_ns = _parse_time_ns(init_time)
        self.busy_ns = _parse_time_ns(busy)
        self.read_ready_ns = _parse_time_ns(read_time)
        self.erase_ns = _parse_time_ns(erase_time)
        self.crc_errors = int(crc_errors)       # test hook: corrupt the CRC of the next N read blocks
        self._geometry(size)
        self._base = data[: self.capacity]
        self.blocks: dict[int, bytes] = {}      # written blocks (sparse); base image underneath
        self.mid, self.oid, self.pnm, self.psn = int(mid) & 0xFF, oid[:2].ljust(2), pnm[:5].ljust(5), int(psn)
        self.cid = self._make_cid()
        self.csd = self._make_csd()
        self.scr = self._make_scr()
        self.log: list[tuple[float, str]] = []  # (t_ns, text) command trace (bounded)
        self.ops: list[tuple[str, int, int]] = []   # (op, block, count) of completed reads/writes/erases
        self.power_on()

    # ------------------------------------------------------------------ geometry / registers
    def _geometry(self, size: int) -> None:
        if self.sdhc:
            units = max(1, -(-size // (512 << 10)))              # C_SIZE + 1 units of 512 KiB
            self.c_size = units - 1
            self.capacity = units * (512 << 10)
            self.read_bl_len = 9
        else:
            for bl in (9, 10, 11):
                for mult in range(8):
                    unit = (1 << (mult + 2)) * (1 << bl)
                    c = -(-size // unit)
                    if c <= 4096:
                        self.read_bl_len, self.c_size_mult, self.c_size = bl, mult, c - 1
                        self.capacity = c * unit
                        break
                else:
                    continue
                break
        self.nblocks = self.capacity // BLOCK

    def _make_cid(self) -> int:
        oid = self.oid.encode("ascii", "replace")
        pnm = self.pnm.encode("ascii", "replace")
        reg = _bits([(127, 120, self.mid), (119, 104, int.from_bytes(oid, "big")),
                     (103, 64, int.from_bytes(pnm, "big")), (63, 56, 0x10), (55, 24, self.psn),
                     (19, 8, (26 << 4) | 11)], 128)                           # PRV 1.0, MDT 2026-11
        return reg | (crc7(reg >> 8, 120) << 1) | 1

    def _make_csd(self) -> int:
        if self.sdhc:
            reg = _bits([(127, 126, 1), (119, 112, 0x0E), (111, 104, 0x00), (103, 96, 0x32), (95, 84, 0x535),
                         (83, 80, 9), (79, 79, 0), (78, 78, 0), (77, 77, 0), (76, 76, 0), (69, 48, self.c_size),
                         (46, 46, 1), (45, 39, 0x7F), (38, 32, 0), (28, 26, 2), (25, 22, 9), (21, 21, 0)], 128)
        else:
            reg = _bits([(127, 126, 0), (119, 112, 0x26), (111, 104, 0x00), (103, 96, 0x32), (95, 84, 0x535),
                         (83, 80, self.read_bl_len), (79, 79, 1), (78, 78, 0), (77, 77, 0), (76, 76, 0),
                         (73, 62, self.c_size), (61, 59, 7), (58, 56, 6), (55, 53, 7), (52, 50, 6),
                         (49, 47, self.c_size_mult), (46, 46, 1), (45, 39, 0x7F), (38, 32, 0), (28, 26, 2),
                         (25, 22, self.read_bl_len), (21, 21, 0)], 128)
        return reg | (crc7(reg >> 8, 120) << 1) | 1

    def _make_scr(self) -> int:
        spec = 0 if self.version == 1 else 2
        sec = 3 if self.sdhc else 2
        return _bits([(63, 60, 0), (59, 56, spec), (55, 55, 0), (54, 52, sec), (51, 48, 0b0101),
                      (47, 47, 0 if self.version == 1 else 1), (33, 32, 0b10)], 64)   # 1+4 bit, CMD23

    # ------------------------------------------------------------------ power / reset
    def power_on(self) -> None:
        """Power cycle: back to idle with RCA 0, 1-bit bus, default speed."""
        self.state = IDLE
        self.rca = 0
        self.status = 0
        self._illegal_pending = False
        self.app = False
        self.cmd8_seen = False
        self.hcs = False
        self.ocr_ready = False
        self._init_t0: float | None = None
        self.bus_width = 1
        self.high_speed = False
        self.blocklen = BLOCK
        self.block_count: int | None = None         # CMD23
        self._xfer: dict | None = None              # current data transfer
        self._busy_until = 0.0
        self._busy_state = TRAN                     # state once busy ends
        self.erase_start: int | None = None
        self.erase_end: int | None = None
        self.card_detect = True
        self.written_count = 0

    def reset(self) -> None:
        """Host-side power cycle (board reset)."""
        self.power_on()

    # ------------------------------------------------------------------ storage
    def read_bytes(self, addr: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            blk, off = divmod(addr, BLOCK)
            k = min(n, BLOCK - off)
            out += self._block(blk)[off:off + k]
            addr += k
            n -= k
        return bytes(out)

    def write_bytes(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        while data:
            blk, off = divmod(addr, BLOCK)
            k = min(len(data), BLOCK - off)
            b = bytearray(self._block(blk))
            b[off:off + k] = data[:k]
            self.blocks[blk] = bytes(b)
            addr += k
            data = data[k:]

    def _block(self, blk: int) -> bytes:
        b = self.blocks.get(blk)
        if b is not None:
            return b
        a = blk * BLOCK
        b = self._base[a:a + BLOCK]
        return b + bytes(BLOCK - len(b)) if len(b) < BLOCK else b

    def save_image(self, path: str | Path | None = None) -> Path | None:
        """Write the card contents (base image + written blocks, trimmed to the highest used block) to `path`, or to
        the `save` / writeback path. Returns the path written, or None."""
        p = Path(path) if path is not None else self.save_path
        if p is None:
            return None
        top = max([len(self._base)] + [(b + 1) * BLOCK for b in self.blocks])
        img = bytearray(self._base) + bytes(max(0, top - len(self._base)))
        for b, d in self.blocks.items():
            img[b * BLOCK:(b + 1) * BLOCK] = d
        p.write_bytes(bytes(img[:top]))
        return p

    def close(self) -> None:
        self.save_image()

    # ------------------------------------------------------------------ time
    def _settle(self, t: float) -> None:
        if self._busy_until and t >= self._busy_until:
            self._busy_until = 0.0
            if self.state == PRG:
                self.state = self._busy_state
            elif self.state == DIS:
                self.state = STBY

    @property
    def busy_until(self) -> float:
        """Virtual time DAT0 busy ends (0: not busy)."""
        return self._busy_until

    def busy(self, t_ns: float) -> bool:
        self._settle(t_ns)
        return t_ns < self._busy_until

    def current_state(self, t_ns: float | None = None) -> str:
        if t_ns is not None:
            self._settle(t_ns)
        return "ina" if self.state == INA else STATES[self.state]

    # ------------------------------------------------------------------ responses
    def _status_word(self) -> int:
        st = self.status | ((self.state & 0xF) << 9)
        if not self._busy_until:
            st |= READY_FOR_DATA                                  # buffer empty: not programming
        if self._illegal_pending:
            st |= ILLEGAL_COMMAND
        return st

    def _r1(self, index: int, *, busy: float = 0.0, data: str | None = None, extra: int = 0,
            state_before: int | None = None) -> Response:
        st = self._status_word() | extra
        if state_before is not None:
            st = (st & ~(0xF << 9)) | (state_before << 9)       # R1 reports the state when the command arrived
        self.status &= ~CLEAR_ON_READ
        self._illegal_pending = False
        kind = "R1b" if busy else "R1"
        return Response(kind, index, st, _frame48(index, st), 48, busy_ns=busy, data=data)

    def _r2(self, reg: int) -> Response:
        self._illegal_pending = False
        frame = (0b00111111 << 128) | reg
        return Response("R2", 0x3F, reg, frame, 136)

    def _r3(self) -> Response:
        self._illegal_pending = False
        ocr = OCR_VOLTAGE
        if self.ocr_ready:
            ocr |= OCR_BUSY | (OCR_CCS if self.sdhc else 0)
        frame = (0b00111111 << 40) | (ocr << 8) | (0x7F << 1) | 1
        return Response("R3", 0x3F, ocr, frame, 48)

    def _r6(self, index: int, state_before: int) -> Response:
        st = (self._status_word() & ~(0xF << 9)) | (state_before << 9)
        bits = (((st >> 23) & 1) << 15) | (((st >> 22) & 1) << 14) | (((st >> 19) & 1) << 13) | (st & 0x1FFF)
        payload = (self.rca << 16) | bits
        self.status &= ~CLEAR_ON_READ
        self._illegal_pending = False
        return Response("R6", index, payload, _frame48(index, payload), 48)

    def _illegal(self, index: int, why: str, t: float) -> None:
        self._illegal_pending = True
        self.app = False
        self._trace(t, f"CMD{index} illegal ({why})")
        return None

    def _trace(self, t: float, text: str) -> None:
        if len(self.log) < 10000:
            self.log.append((t, text))

    # ------------------------------------------------------------------ commands
    def command(self, index: int, arg: int, t_ns: float = 0.0) -> Response | None:
        """The host finished sending command `index` with argument `arg` at `t_ns`. Returns the response."""
        self._settle(t_ns)
        index &= 0x3F
        arg &= 0xFFFFFFFF
        if self.state == INA:
            return None
        app, self.app = self.app, False
        if index == 0:
            self.power_on()
            self._trace(t_ns, "CMD0 GO_IDLE_STATE")
            return None
        if app:
            r = self._acmd(index, arg, t_ns)
            if r is not NotImplemented:
                return r
            # an ACMD index with no application command meaning is a regular command (spec 4.3.9.1)
        return self._cmd(index, arg, t_ns)

    def _addressed(self, arg: int) -> bool:
        return (arg >> 16) == self.rca

    def _addr(self, arg: int) -> int:
        return arg * BLOCK if self.sdhc else arg

    def _cmd(self, index: int, arg: int, t: float) -> Response | None:
        s = self.state
        if index == 8:
            if self.version == 1 or s != IDLE:
                return self._illegal(8, "state" if s != IDLE else "v1 card", t)
            self.cmd8_seen = True
            vhs = (arg >> 8) & 0xF
            if vhs != 1:
                self._trace(t, f"CMD8 VHS={vhs:x} not supported: no response")
                return None
            payload = (1 << 8) | (arg & 0xFF)
            self._trace(t, f"CMD8 SEND_IF_COND pattern 0x{arg & 0xFF:02X}")
            return Response("R7", 8, payload, _frame48(8, payload), 48)
        if index == 2:
            if s != READY:
                return self._illegal(2, STATES[s], t)
            self.state = IDENT
            self._trace(t, "CMD2 ALL_SEND_CID")
            return self._r2(self.cid)
        if index == 3:
            if s not in (IDENT, STBY):
                return self._illegal(3, STATES[s], t)
            self.rca = ((self.rca or 0x1234) * 0x9E3 + 0x5A5) & 0xFFFF or 0x0001
            prev = self.state
            self.state = STBY
            r = self._r6(3, prev)
            self._trace(t, f"CMD3 SEND_RELATIVE_ADDR -> RCA 0x{self.rca:04X}")
            return r
        if index == 4:
            if s not in (STBY,):
                return self._illegal(4, STATES[s], t)
            return None                                            # SET_DSR: no response
        if index == 7:
            sel = self._addressed(arg) and self.rca != 0
            if s == STBY and sel:
                prev = s
                self.state = TRAN
                self._trace(t, f"CMD7 select RCA 0x{self.rca:04X}")
                return self._r1(7, busy=1.0, state_before=prev)
            if s in (TRAN, DATA) and not sel:
                self.state = STBY
                self._xfer = None
                return None                                        # deselected card does not respond
            if s == PRG and not sel:
                self.state = DIS
                return None
            if s == DIS and sel:
                self.state = PRG
                return self._r1(7, busy=max(1.0, self._busy_until - t), state_before=DIS)
            if s in (TRAN, PRG) and sel:
                return self._illegal(7, "already selected", t)
            return None if s in (STBY, DIS) else self._illegal(7, STATES[s], t)
        if index in (9, 10):
            if s != STBY or not self._addressed(arg):
                return None if s == STBY else self._illegal(index, STATES[s], t)
            self._trace(t, f"CMD{index} SEND_{'CSD' if index == 9 else 'CID'}")
            return self._r2(self.csd if index == 9 else self.cid)
        if index == 12:
            if s == DATA:
                self.state = TRAN
                self._xfer = None
                self.block_count = None
                self._trace(t, "CMD12 STOP_TRANSMISSION (read)")
                return self._r1(12, busy=1.0, state_before=DATA)
            if s == RCV:
                x = self._xfer
                self._xfer = None
                self.block_count = None
                self._trace(t, "CMD12 STOP_TRANSMISSION (write)")
                if self._busy_until > t:
                    self.state = PRG
                    self._busy_state = TRAN
                    return self._r1(12, busy=self._busy_until - t, state_before=RCV)
                self.state = TRAN
                return self._r1(12, busy=1.0, state_before=RCV) if x is not None else self._r1(12, state_before=RCV)
            return self._illegal(12, STATES[s], t)
        if index == 13:
            if s not in (STBY, TRAN, DATA, RCV, PRG, DIS) or not self._addressed(arg):
                return None if s in (STBY, TRAN, DATA, RCV, PRG, DIS) else self._illegal(13, STATES[s], t)
            return self._r1(13)
        if index == 15:
            if s in (IDLE, READY, IDENT):
                return self._illegal(15, STATES[s], t)
            if self._addressed(arg):
                self.state = INA
            return None
        if index == 16:
            if s != TRAN:
                return self._illegal(16, STATES[s], t)
            if arg == 0 or arg > BLOCK:
                return self._r1(16, extra=BLOCK_LEN_ERROR)
            self.blocklen = arg
            return self._r1(16)
        if index in (17, 18):
            if s != TRAN:
                return self._illegal(index, STATES[s], t)
            addr = self._addr(arg)
            n = BLOCK if self.sdhc else self.blocklen
            if addr + n > self.capacity:
                return self._r1(index, extra=OUT_OF_RANGE)
            if not self.sdhc and (addr // BLOCK) != (addr + n - 1) // BLOCK:
                return self._r1(index, extra=ADDRESS_ERROR)
            prev = s
            self.state = DATA
            count = 1 if index == 17 else self.block_count
            self.block_count = None
            self._xfer = {"op": "read", "addr": addr, "len": n, "left": count, "multi": index == 18, "n": 0,
                          "src": None}
            self._trace(t, f"CMD{index} READ_{'SINGLE' if index == 17 else 'MULTIPLE'}_BLOCK 0x{arg:08X}")
            return self._r1(index, data="read", state_before=prev)
        if index == 23:
            if s != TRAN:
                return self._illegal(23, STATES[s], t)
            self.block_count = arg & 0xFFFF or None
            return self._r1(23)
        if index in (24, 25):
            if s != TRAN:
                return self._illegal(index, STATES[s], t)
            addr = self._addr(arg)
            if not self.sdhc and self.blocklen != BLOCK:
                return self._r1(index, extra=BLOCK_LEN_ERROR)
            if addr % BLOCK:
                return self._r1(index, extra=ADDRESS_ERROR)
            if addr + BLOCK > self.capacity:
                return self._r1(index, extra=OUT_OF_RANGE)
            prev = s
            self.state = RCV
            count = 1 if index == 24 else self.block_count
            self.block_count = None
            self._xfer = {"op": "write", "addr": addr, "len": BLOCK, "left": count, "multi": index == 25, "n": 0}
            self.written_count = 0
            self._trace(t, f"CMD{index} WRITE_{'BLOCK' if index == 24 else 'MULTIPLE_BLOCK'} 0x{arg:08X}")
            return self._r1(index, data="write", state_before=prev)
        if index in (32, 33):
            if s != TRAN:
                return self._illegal(index, STATES[s], t)
            a = self._addr(arg)
            if index == 32:
                self.erase_start, self.erase_end = a, None
            elif self.erase_start is None:
                return self._r1(33, extra=ERASE_SEQ_ERROR)
            else:
                self.erase_end = a
            if a >= self.capacity:
                return self._r1(index, extra=OUT_OF_RANGE)
            return self._r1(index)
        if index == 38:
            if s != TRAN:
                return self._illegal(38, STATES[s], t)
            if self.erase_start is None or self.erase_end is None:
                self.erase_start = self.erase_end = None
                return self._r1(38, extra=ERASE_SEQ_ERROR)
            b0, b1 = sorted((self.erase_start // BLOCK, self.erase_end // BLOCK))
            for b in range(b0, b1 + 1):
                if b in self.blocks or b * BLOCK < len(self._base):
                    self.blocks[b] = bytes(BLOCK)
            self.erase_start = self.erase_end = None
            self.ops.append(("erase", b0, b1 - b0 + 1))
            self.state = PRG
            self._busy_state = TRAN
            self._busy_until = t + self.erase_ns
            return self._r1(38, busy=self.erase_ns, state_before=TRAN)
        if index == 55:
            if s in (READY, IDENT):
                return self._illegal(55, STATES[s], t)
            if s != IDLE and not self._addressed(arg):
                return None
            self.app = True
            return self._r1(55, extra=APP_CMD)
        if index == 6:
            if self.version == 1 or s != TRAN:
                return self._illegal(6, STATES[s] if s != TRAN else "v1 card", t)
            data = self._switch(arg)
            self.state = DATA
            self._xfer = {"op": "read", "addr": 0, "len": 64, "left": 1, "multi": False, "n": 0, "src": data}
            self._trace(t, f"CMD6 SWITCH_FUNC 0x{arg:08X}")
            return self._r1(6, data="read", state_before=TRAN)
        return self._illegal(index, "not supported", t)

    def _acmd(self, index: int, arg: int, t: float) -> Response | None | Any:
        s = self.state
        if index == 41:
            if s != IDLE:
                return self._illegal(41, STATES[s], t)
            if arg & OCR_VOLTAGE == 0:
                return self._r3()                                  # inquiry: OCR without starting init
            self.hcs = bool(arg & OCR_CCS)
            if self._init_t0 is None:
                self._init_t0 = t
            can = not self.sdhc or (self.hcs and self.cmd8_seen)
            if can and t - self._init_t0 >= self.init_ns:
                self.ocr_ready = True
                self.state = READY
                self._trace(t, f"ACMD41 ready ({'SDHC, CCS=1' if self.sdhc else 'SDSC'})")
            return self._r3()
        if index == 6:
            if s != TRAN:
                return self._illegal(6, STATES[s], t)
            w = arg & 3
            if w not in (0, 2):
                return self._r1(6, extra=APP_CMD | ERROR)
            self.bus_width = 4 if w == 2 else 1
            self._trace(t, f"ACMD6 bus width {self.bus_width}")
            return self._r1(6, extra=APP_CMD)
        if index == 13:
            if s != TRAN:
                return self._illegal(13, STATES[s], t)
            self.state = DATA
            self._xfer = {"op": "read", "addr": 0, "len": 64, "left": 1, "multi": False, "n": 0,
                          "src": self._sd_status()}
            return self._r1(13, data="read", extra=APP_CMD, state_before=TRAN)
        if index == 22:
            if s != TRAN:
                return self._illegal(22, STATES[s], t)
            n = self.written_count
            self.state = DATA
            self._xfer = {"op": "read", "addr": 0, "len": 4, "left": 1, "multi": False, "n": 0,
                          "src": int(n).to_bytes(4, "big")}
            return self._r1(22, data="read", extra=APP_CMD, state_before=TRAN)
        if index == 23:
            if s != TRAN:
                return self._illegal(23, STATES[s], t)
            return self._r1(23, extra=APP_CMD)                    # pre-erase count: accepted, no effect
        if index == 42:
            if s != TRAN:
                return self._illegal(42, STATES[s], t)
            self.card_detect = bool(arg & 1)
            return self._r1(42, extra=APP_CMD)
        if index == 51:
            if s != TRAN:
                return self._illegal(51, STATES[s], t)
            self.state = DATA
            self._xfer = {"op": "read", "addr": 0, "len": 8, "left": 1, "multi": False, "n": 0,
                          "src": self.scr.to_bytes(8, "big")}
            return self._r1(51, data="read", extra=APP_CMD, state_before=TRAN)
        return NotImplemented

    def _switch(self, arg: int) -> bytes:
        """CMD6 status (512 bits). Group 1 (access mode): 0 default, 1 high speed; other groups: default only."""
        mode = arg >> 31
        sel = [(arg >> (4 * g)) & 0xF for g in range(6)]
        result = []
        for g in range(6):
            f = sel[g]
            if f == 0xF:
                result.append(1 if (g == 0 and self.high_speed) else 0)
            elif g == 0 and f in (0, 1):
                result.append(f)
            elif f == 0:
                result.append(0)
            else:
                result.append(0xF)                                   # not supported
        if mode == 1 and 0xF not in result:
            self.high_speed = result[0] == 1
        fields = [(511, 496, 100)]                                     # max current 100 mA
        support = [0x8003, 0x8001, 0x8001, 0x8001, 0x8001, 0x8001]    # group1 .. group6
        for g in range(6):
            fields.append((415 + 16 * g, 400 + 16 * g, support[g]))
            fields.append((379 + 4 * g, 376 + 4 * g, result[g]))
        fields.append((375, 368, 1))                                    # data structure version 1
        return _bits(fields, 512).to_bytes(64, "big")

    def _sd_status(self) -> bytes:
        bw = 2 if self.bus_width == 4 else 0
        fields = [(511, 510, bw), (509, 509, 0), (495, 480, 0), (479, 448, 0 if not self.sdhc else 0),
                  (447, 440, 4), (439, 432, 0), (431, 428, 9), (423, 408, 1), (407, 402, 1), (401, 400, 0)]
        return _bits(fields, 512).to_bytes(64, "big")

    # ------------------------------------------------------------------ data phase
    def read_block(self, t_ns: float = 0.0, width: int | None = None) -> bytes | None:
        """The next block the card drives on DAT (None: the card is not sending)."""
        self._settle(t_ns)
        x = self._xfer
        if x is None or x["op"] != "read" or self.state != DATA:
            return None
        if x["src"] is not None:
            data = x["src"]
        else:
            if x["addr"] + x["len"] > self.capacity:
                self.status |= OUT_OF_RANGE
                self._xfer = None
                self.state = TRAN if not x["multi"] else DATA
                return None
            data = self.read_bytes(x["addr"], x["len"])
            x["addr"] += x["len"]
        x["n"] += 1
        if x["left"] is not None:
            x["left"] -= 1
        if x["src"] is None:
            if x["n"] == 1:
                self.ops.append(("read", (x["addr"] - x["len"]) // BLOCK, 1))
            else:
                op, b, n = self.ops[-1]
                self.ops[-1] = (op, b, n + 1)
        if x["left"] == 0:
            self._xfer = None
            self.state = TRAN
        return data

    def take_crc_error(self) -> bool:
        """Test hook: True when the block just read should carry a bad CRC."""
        if self.crc_errors > 0:
            self.crc_errors -= 1
            return True
        return False

    def write_block(self, data: bytes, t_ns: float = 0.0, width: int | None = None,
                    crc_ok: bool = True) -> tuple[bool, float]:
        """The host sent one data block. Returns (CRC status positive, DAT0 busy time in ns). A negative CRC status
        (bad CRC, wrong bus width) discards the block; for CMD25 the card then waits for CMD12."""
        self._settle(t_ns)
        x = self._xfer
        if x is None or x["op"] != "write" or self.state not in (RCV,):
            return False, 0.0
        if not crc_ok or (width is not None and width != self.bus_width):
            self._trace(t_ns, "write block: CRC error (negative CRC status)")
            if not x["multi"]:
                self._xfer = None
                self.state = TRAN
            return False, 0.0
        self.write_bytes(x["addr"], bytes(data[:BLOCK]).ljust(BLOCK, b"\0"))
        blk = x["addr"] // BLOCK
        if x["n"] > 0:
            op, b, n = self.ops[-1]
            self.ops[-1] = (op, b, n + 1)
        else:
            self.ops.append(("write", blk, 1))
        self.written_count += 1
        x["addr"] += BLOCK
        x["n"] += 1
        if x["left"] is not None:
            x["left"] -= 1
        self._busy_until = t_ns + self.busy_ns
        if not x["multi"] or x["left"] == 0:
            self._xfer = None
            self.state = PRG
            self._busy_state = TRAN
        elif x["addr"] + BLOCK > self.capacity:
            self.status |= OUT_OF_RANGE
        return True, self.busy_ns

    def write_expected(self) -> bool:
        x = self._xfer
        return x is not None and x["op"] == "write" and self.state == RCV

    def info(self) -> dict:
        return {"name": self.name, "kind": self.kind, "capacity": self.capacity, "state": self.current_state(),
                "rca": self.rca, "bus_width": self.bus_width, "high_speed": self.high_speed,
                "written_blocks": len(self.blocks)}


# ------------------------------------------------------------------------------------------------ spec parsing
def parse_spec(spec: str) -> tuple[str, dict]:
    """"SDIO:size=1G,kind=sdhc,image=card.img,save=out.img" -> ("SDIO", {...})."""
    host, _, rest = str(spec).partition(":")
    opts: dict[str, Any] = {}
    for part in filter(None, (p.strip() for p in rest.split(","))):
        k, _, v = part.partition("=")
        opts[k.strip().lower()] = v.strip()
    return host.strip(), opts


_TRUE = ("1", "true", "yes", "on")


def card_from_options(o: dict, name: str = "sd_card") -> SDCard:
    o = dict(o)
    img = o.pop("image", None) or o.pop("file", None)
    kw: dict[str, Any] = {}
    for k in ("init_time", "busy", "read_time", "erase_time"):
        if k in o:
            kw[k] = o.pop(k)
    wb = str(o.pop("writeback", "")).lower() in _TRUE
    card = SDCard(o.pop("size", None), kind=o.pop("kind", None) or o.pop("type", None),
                  version=int(o.pop("version", 2)), image=img, save=o.pop("save", None), writeback=wb,
                  name=name, **kw)
    return card


def find_host(chip: Any, host: str | None) -> tuple[str, Any]:
    """The SD host block named `host` (any block with `attach_card`), or the first one."""
    per = getattr(chip, "peripherals", {}) or {}
    if host:
        for n, b in per.items():
            if n.upper() == host.upper() and hasattr(b, "attach_card"):
                return n, b
        raise ValueError(f"no SD host controller named {host!r} (hosts: "
                         f"{', '.join(n for n, b in per.items() if hasattr(b, 'attach_card')) or 'none'})")
    for n, b in per.items():
        if hasattr(b, "attach_card"):
            return n, b
    raise ValueError("this chip has no SD host controller model (stm32.sdio / kinetis.sdhc)")


def attach_from_spec(chip: Any, spec: str) -> SDCard:
    host, o = parse_spec(spec)
    name, blk = find_host(chip, host or None)
    card = card_from_options(o, name=f"{name}_card")
    blk.attach_card(card)
    return card
