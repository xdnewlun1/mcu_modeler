"""STM32F415/F417 cryptographic processor (CRYP, RM0090 ch. 23): DES / TDES (ECB, CBC) and AES-128/192/256 (ECB,
CBC, CTR) with AES key preparation, IN / OUT FIFOs, DMA and interrupts.

Reference: RM0090 Rev 22 ch. 23 (section numbers below), the register fields of the SVD (chips/stm32f405_regs.yaml)
and ST's HAL (firmware/vendor/STM32CubeF4 stm32f4xx_hal_cryp.c, CMSIS stm32f417xx.h).  Ciphers: chipem/crypto_algos.py (pure Python, checked
against FIPS-197 / SP 800-38A / FIPS 46-3 vectors).

* Presence: the block exists on STM32F415/F417 only (`params.only`, `_util.part_present`); on other parts it is an
  inactive stub (`_util.make_absent`).  Clock gate RCC_AHB2ENR.CRYPEN (one `warn` on a gated access); RCC_AHB2RSTR
  resets the model.
* CR (23.6.1): ALGODIR (1 = decrypt), ALGOMODE 0 TDES-ECB, 1 TDES-CBC, 2 DES-ECB, 3 DES-CBC, 4 AES-ECB, 5 AES-CBC,
  6 AES-CTR, 7 AES key preparation; DATATYPE (00 none, 01 half-word swap, 10 byte swap, 11 bit reversal of each
  32-bit DIN / DOUT word); KEYSIZE (AES 128/192/256); FFLUSH (write 1 while CRYPEN = 0: both FIFOs emptied; no effect
  while CRYPEN = 1; reads 0); CRYPEN.  Writes to CR[9:2] (KEYSIZE, DATATYPE, ALGOMODE, ALGODIR) while BUSY = 1 are
  ignored (23.3.5, 23.6.1 note).  The reserved bits read 0.  ALGOMODE[3] and GCM_CCMPH (GCM/CCM, 23.6.2) and the
  context swap registers CSGCMCCM0..7 / CSGCM0..7 (23.6.12) exist on STM32F42x/F43x only: on the F415/F417 they are
  reserved (the CR bits read 0 and are ignored with one `warn`; the CSGCM* registers read 0).  With `params.gcm`
  (F43x) GCM/CCM are accepted but not modelled (one `warn`, nothing is processed).
* Keys (23.3.2, 23.6.10): K0LR..K3RR (write-only, read 0); AES-128 uses K2..K3, AES-192 K1..K3, AES-256 K0..K3
  (KxLR = more significant word); DES uses K1, TDES K1|K2|K3 (EDE: E_K3(D_K2(E_K1(P)))).  IV0LR..IV1RR (AES 128-bit
  IV, DES the IV0 pair, 23.6.11) are updated by the hardware after every block (CBC: last ciphertext block; CTR: the
  32-bit counter in IV1RR incremented modulo 2^32, 23.3.4), so software can read them back for context save.  Key and
  IV writes while BUSY = 1 are ignored (23.6.10/11 notes).
* Key preparation (ALGOMODE = 7, CRYPEN set; 23.3.6 step 1d): BUSY, then the key registers hold the last Nk words of
  the encryption key schedule (the decryption key) and CRYPEN clears.  RM0090 gives no duration for it; the model
  uses one AES block time (14/16/18 cycles).  AES-ECB/CBC decryption uses
  the key registers as that decryption key: decrypting without a preceding key preparation (or after rewriting a key
  register) gives the wrong plaintext as on silicon (one `warn`).  CTR decryption needs no preparation.
* FIFOs (23.2, 23.3.5): IN and OUT are 8 x 32 bits.  An AES block (4 words) moves from IN to the core when CRYPEN = 1,
  the core is idle and OUT has room for it; a DES/TDES block (2 words) is popped as soon as IN holds one, and if OUT
  is full when it is done BUSY stays set until 2 words are free (23.3.5 note).  The result enters OUT after the
  processing time.  A DIN write to a full IN FIFO is lost (one `warn`; silicon stalls the bus).  Clearing CRYPEN
  lets the block in progress finish; no further block starts.  Reading DIN while CRYPEN = 0 pops the IN FIFO (oldest
  word first, as written); while CRYPEN = 1 it returns 0 (undefined on silicon, 23.6.4).
* Processing time (HCLK cycles per block, `sched.at_cycles`; RM0090 23.2 Table 111, 23.3.5): AES-128 14, AES-192 16,
  AES-256 18, DES 16, TDES 48.  SR.BUSY is set during it.
* SR (23.6.3, reset 0x3): IFEM (IN empty), IFNF (IN not full), OFNE (OUT not empty), OFFU (OUT full), BUSY.  RISR
  (23.4, 23.6.8, reset 0x1): INRIS while IN holds fewer than 4 words, OUTRIS while OUT is not empty.  MISR: INMIS =
  INRIS & INIM & CRYPEN (the input interrupt is enabled with CRYPEN), OUTMIS = OUTRIS & OUTIM (not gated by CRYPEN);
  IRQ "CRYP" (level) = MISR != 0.
* DMA (DMACR, 23.5): DIEN requests "CRYP_IN" (DMA2 stream 6 channel 2) while IN is not full, DOEN requests "CRYP_OUT"
  (DMA2 stream 5 channel 2) while OUT is not empty (one request per word); all requests are deasserted while
  CRYPEN = 0.  The burst request signals are not modelled separately (single requests only).
`blocks` counts processed blocks; `trace` keeps the last (mode, dir, input, output) tuples.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ... import crypto_algos as ca
from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, dma_request, irq_number, make_absent, part_present

ALGODIR, CRYPEN, FFLUSH = 1 << 2, 1 << 15, 1 << 14
ALGOMODE3, GCM_CCMPH = 1 << 19, 3 << 16
CR_BITS = 0xC3FC                              # CRYPEN, FFLUSH, KEYSIZE, DATATYPE, ALGOMODE[2:0], ALGODIR (23.6.1)
CFG_BITS = 0x3FC                              # CR[9:2]: ignored while BUSY (23.3.5)
IFEM, IFNF, OFNE, OFFU, BUSY = 1, 2, 4, 8, 16
DIEN, DOEN = 1, 2
INR, OUTR = 1, 2
DEPTH = 8
MODES = ("TDES-ECB", "TDES-CBC", "DES-ECB", "DES-CBC", "AES-ECB", "AES-CBC", "AES-CTR", "AES-KEY")
KEYREGS = [f"K{i}{s}R" for i in range(4) for s in "LR"]
IVREGS = ["IV0LR", "IV0RR", "IV1LR", "IV1RR"]


def swap_word(w: int, datatype: int) -> int:
    """DIN/DOUT data swapping (RM0090 23.3.x 'data type'): 0 none, 1 half-words, 2 bytes, 3 bits."""
    w &= 0xFFFFFFFF
    if datatype == 1:
        return ((w << 16) | (w >> 16)) & 0xFFFFFFFF
    if datatype == 2:
        return int.from_bytes(w.to_bytes(4, "little"), "big")
    if datatype == 3:
        return int(f"{w:032b}"[::-1], 2)
    return w


def _words(b: bytes) -> list[int]:
    return [int.from_bytes(b[i:i + 4], "big") for i in range(0, len(b), 4)]


def _bytes(ws) -> bytes:
    return b"".join((w & 0xFFFFFFFF).to_bytes(4, "big") for w in ws)


@register("stm32.cryp")
class CRYP(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "CRYP"
        self.absent = False
        ro = {"ro_mask": 0xFFFFFFFF}
        ov: dict[str, dict] = {
            "CR": {"on_write": self._cr_write, "on_read": lambda b, r: self.get("CR") & ~FFLUSH},
            "SR": {**ro, "on_read": lambda b, r: self._sr()},
            "DIN": {"on_write": self._din_write, "on_read": self._din_read},
            "DOUT": {**ro, "on_read": self._dout_read},
            "DMACR": {"on_write": self._ctl_write},
            "IMSCR": {"on_write": self._ctl_write},
            "RISR": {**ro, "on_read": lambda b, r: self._ris()},
            "MISR": {**ro, "on_read": lambda b, r: self._mis()},
        }
        for k in KEYREGS:
            ov[k] = {"on_write": self._key_write, "on_read": lambda b, r: 0}
        for k in IVREGS:
            ov[k] = {"on_write": self._iv_write}
        if not params.get("gcm", False):          # CSGCMCCM0..7R / CSGCM0..7R: STM32F42x/F43x only (23.6.12)
            for i in range(8):
                for k in (f"CSGCMCCM{i}R", f"CSGCM{i}R"):
                    ov[k] = {**ro, "on_read": lambda b, r: 0}
        r = build_regs((name, "CRYP"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.gcm = bool(params.get("gcm", False))
        self._irqn: int | None = None
        self._warned: set[str] = set()
        self.blocks = 0
        self.trace: deque = deque(maxlen=1000)
        self._init_state()

    def _init_state(self) -> None:
        self.infifo: deque[int] = deque()
        self.outfifo: deque[int] = deque()
        self.busy = False
        self._held: list[int] | None = None          # DES/TDES result waiting for OUT space (BUSY stays set)
        self._ev = None
        self._line = False
        self._dma = [False, False]
        self._key_prepared: tuple | None = None      # (key size, key words) after a key preparation

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if not part_present(chip, self.params):
            make_absent(self, "the cryptographic processor (CRYP, STM32F415/F417 only)")
            return
        self._irqn = irq_number(chip, "CRYP", self.name)

    def reset(self) -> None:
        if self.absent:
            MMIOBlock.reset(self)
            return
        chip = self.chip
        if chip is not None:
            if self._ev is not None:
                chip.sched.cancel(self._ev)
            if self._line and self._irqn is not None:
                self.irq(self._irqn, False)
            for name, on in zip(("CRYP_IN", "CRYP_OUT"), self._dma):
                if on:
                    dma_request(chip, name, False)
        self._init_state()
        self._warned.clear()
        super().reset()
        self._update()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None and not self.absent:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ config
    def _cr(self) -> int:
        return self.get("CR")

    def algomode(self) -> int:
        return (self._cr() >> 3) & 7

    def _datatype(self) -> int:
        return (self._cr() >> 6) & 3

    def _keysize(self) -> int:
        return (16, 24, 32, 32)[(self._cr() >> 8) & 3]

    def _reserved_mode(self) -> bool:
        cr = self._cr()
        if cr & (ALGOMODE3 | GCM_CCMPH):
            self._warn_once("gcm2", f"{self.name}: GCM/CCM modes are not modelled: nothing is processed")
            return True
        return False

    def _kwords(self) -> list[int]:
        return [self.get(k) for k in KEYREGS]

    def _aes_key(self) -> bytes:
        n = self._keysize() // 4
        return _bytes(self._kwords()[8 - n:])

    def _iv(self) -> list[int]:
        return [self.get(k) for k in IVREGS]

    def _set_iv(self, ws: list[int]) -> None:
        for k, w in zip(IVREGS, ws):
            self.set(k, w)

    def block_words(self) -> int:
        return 4 if self.algomode() >= 4 else 2

    def cycles(self) -> int:
        m = self.algomode()
        if m >= 4:
            return {16: 14, 24: 16, 32: 18}[self._keysize()]
        return 48 if m < 2 else 16

    # ------------------------------------------------------------------ status / IRQ / DMA
    def _sr(self) -> int:
        n_in, n_out = len(self.infifo), len(self.outfifo)
        v = 0
        if n_in == 0:
            v |= IFEM
        if n_in < DEPTH:
            v |= IFNF
        if n_out:
            v |= OFNE
        if n_out >= DEPTH:
            v |= OFFU
        if self.busy:
            v |= BUSY
        return v

    def _ris(self) -> int:
        return (INR if len(self.infifo) < 4 else 0) | (OUTR if self.outfifo else 0)

    def _mis(self) -> int:
        """INMIS is enabled with CRYPEN, OUTMIS is not (RM0090 23.4, Figure 231)."""
        m = self._ris() & self.get("IMSCR")
        return m if self._cr() & CRYPEN else m & ~INR

    def _update(self) -> None:
        chip = self.chip
        if chip is None or self.absent:
            return
        line = bool(self._mis())
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        dmacr = self.get("DMACR") if self._cr() & CRYPEN else 0      # requests deasserted while disabled (23.5)
        want = [bool(dmacr & DIEN and len(self.infifo) < DEPTH), bool(dmacr & DOEN and self.outfifo)]
        for i, name in enumerate(("CRYP_IN", "CRYP_OUT")):
            if want[i] != self._dma[i] or want[i]:
                self._dma[i] = want[i]
                dma_request(chip, name, want[i])

    # ------------------------------------------------------------------ engine
    def _kick(self) -> None:
        if self.busy or self.chip is None or not self._cr() & CRYPEN or self._reserved_mode():
            return
        m = self.algomode()
        if m == 7:
            self._start(self.cycles(), self._key_done)
            return
        bw = self.block_words()
        if len(self.infifo) < bw:
            return
        if m >= 4 and len(self.outfifo) + bw > DEPTH:   # AES waits for OUT room; DES/TDES pops anyway (23.3.5)
            return
        data = [self.infifo.popleft() for _ in range(bw)]
        out = self._process(m, data)
        self._start(self.cycles(), self._block_done, out)

    def _start(self, cycles: int, cb, *args) -> None:
        self.busy = True
        self._ev = self.chip.sched.at_cycles(self.chip.now_cycles() + cycles, cb, *args)
        self._update()

    def _key_done(self) -> None:
        self._ev = None
        self.busy = False
        n = self._keysize() // 4
        last = ca.aes_last_words(self._aes_key())
        kw = self._kwords()
        kw[8 - n:] = last
        for k, w in zip(KEYREGS, kw):
            self.set(k, w)
        self._key_prepared = (n, tuple(last))
        self.set("CR", self._cr() & ~CRYPEN)                # CRYPEN cleared by hardware at the end
        self._update()

    def _block_done(self, out: list[int]) -> None:
        self._ev = None
        if len(self.outfifo) + len(out) > DEPTH:          # DES/TDES: BUSY until OUT has room (23.3.5 note)
            self._held = out
            return
        self.busy = False
        self.outfifo.extend(out)
        self.blocks += 1
        self._update()
        self._kick()

    def _process(self, m: int, data: list[int]) -> list[int]:
        dec = bool(self._cr() & ALGODIR)
        blk = _bytes(data)
        iv = self._iv()
        kw = self._kwords()
        if m < 4:                                            # DES / TDES
            if m < 2:
                key = _bytes(kw[2:8])
                enc, decf = ca.tdes_encrypt, ca.tdes_decrypt
            else:
                key = _bytes(kw[2:4])
                enc, decf = ca.des_encrypt, ca.des_decrypt
            ivb = _bytes(iv[:2])
            cbc = m & 1
            if dec:
                out = decf(key, blk)
                if cbc:
                    out = bytes(a ^ b for a, b in zip(out, ivb))
                    self._set_iv(_words(blk) + iv[2:])
            else:
                src = bytes(a ^ b for a, b in zip(blk, ivb)) if cbc else blk
                out = enc(key, src)
                if cbc:
                    self._set_iv(_words(out) + iv[2:])
        else:                                                # AES
            key = self._aes_key()
            ivb = _bytes(iv)
            if m == 6:                                       # CTR (both directions)
                ks = ca.aes_encrypt(key, ivb)
                out = bytes(a ^ b for a, b in zip(blk, ks))
                self._set_iv(iv[:3] + [(iv[3] + 1) & 0xFFFFFFFF])
            elif dec:
                n = len(key) // 4
                last = _words(key)
                if self._key_prepared != (n, tuple(last)):
                    self._warn_once("noprep", f"{self.name}: AES-{'ECB' if m == 4 else 'CBC'} decryption without "
                                    f"a key preparation (ALGOMODE=111) for this key: the key registers are used as "
                                    f"the decryption key and the result is wrong, as on silicon")
                out = ca.aes_decrypt_from_last(last, blk)
                if m == 5:
                    out = bytes(a ^ b for a, b in zip(out, ivb))
                    self._set_iv(_words(blk))
            else:
                src = bytes(a ^ b for a, b in zip(blk, ivb)) if m == 5 else blk
                out = ca.aes_encrypt(key, src)
                if m == 5:
                    self._set_iv(_words(out))
        self.trace.append((MODES[m], "dec" if dec else "enc", blk.hex(), out.hex()))
        return _words(out)

    # ------------------------------------------------------------------ register hooks
    def _cr_write(self, blk, reg, old, new):
        if self.absent:
            return None
        valid = CR_BITS | ((ALGOMODE3 | GCM_CCMPH) if self.gcm else 0)
        if new & (ALGOMODE3 | GCM_CCMPH) and not self.gcm:
            self._warn_once("gcm", f"{self.name}: CR.ALGOMODE[3] / GCM_CCMPH (GCM, CCM) do not exist on the "
                            f"STM32F415/F417 (CRYP of the F42x/F43x only): ignored")
        new &= valid
        if self.busy and (new ^ old) & (CFG_BITS | ALGOMODE3 | GCM_CCMPH):
            self._warn_once("cfgbusy", f"{self.name}: CR[9:2] written while BUSY = 1: ignored (RM0090 23.3.5)")
            new = (new & ~(CFG_BITS | ALGOMODE3 | GCM_CCMPH)) | (old & (CFG_BITS | ALGOMODE3 | GCM_CCMPH))
        if new & FFLUSH:
            if old & CRYPEN:
                self._warn_once("fflush", f"{self.name}: FFLUSH written while CRYPEN = 1: no effect (RM0090 23.6.1)")
            else:
                if self.busy:
                    self._warn_once("fflushbusy", f"{self.name}: FFLUSH while BUSY: the block in progress still "
                                    f"enters the OUT FIFO (RM0090 23.6.1 note)")
                self.infifo.clear()
                self.outfifo.clear()
            new &= ~FFLUSH
        self._store(reg, new)
        self._update()
        if new & CRYPEN:
            self._kick()
        return new

    def _key_write(self, blk, reg, old, new):
        if self.busy:            # ignored while BUSY (RM0090 23.6.10 note)
            self._warn_once("keybusy", f"{self.name}: key register written while BUSY = 1: ignored")
            return old
        return None              # a rewritten key no longer matches _key_prepared (checked per block)

    def _iv_write(self, blk, reg, old, new):
        if self.busy:            # ignored while BUSY (RM0090 23.6.11 note)
            self._warn_once("ivbusy", f"{self.name}: IV register written while BUSY = 1: ignored")
            return old
        return None

    def _ctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    def _din_write(self, blk, reg, old, new):
        if self.absent:
            return None
        if len(self.infifo) >= DEPTH:
            self._warn_once("full", f"{self.name}: DIN written while the IN FIFO is full: word lost (check "
                            f"SR.IFNF; silicon stalls the bus)")
        else:
            self.infifo.append(swap_word(new, self._datatype()))
        self._update()
        self._kick()
        return 0

    def _din_read(self, blk, reg):
        """CRYPEN = 0: pops the IN FIFO, oldest word first, as written (RM0090 23.6.4); CRYPEN = 1: undefined (0)."""
        if self.absent:
            return 0
        if self._cr() & CRYPEN or not self.infifo:
            return 0
        v = swap_word(self.infifo.popleft(), self._datatype())     # the swap is its own inverse
        self._update()
        return v

    def _dout_read(self, blk, reg):
        if self.absent:
            return 0
        if not self.outfifo:
            return 0
        v = swap_word(self.outfifo.popleft(), self._datatype())
        if self._held is not None and len(self.outfifo) + len(self._held) <= DEPTH:
            out, self._held = self._held, None
            self._block_done(out)
            return v
        self._update()
        self._kick()
        return v
