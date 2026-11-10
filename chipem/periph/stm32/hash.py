"""STM32F415/F417 hash processor (HASH, RM0090 ch. 25): SHA-1 and MD5, HMAC, DMA, interrupts, context swapping.

Reference: RM0090 Rev 22 ch. 25 (section numbers below), the SVD fields (chips/stm32f405_regs.yaml) and ST's HAL
(firmware/vendor/STM32CubeF4 stm32f4xx_hal_hash.c: HMAC steps, DCAL / BUSY polling, context save / restore order).  Algorithms: chipem/crypto_algos.py (pure-Python compression functions
with an explicit state, checked against hashlib).

* Presence: STM32F415/F417 only (`params.only`, `_util.part_present`); elsewhere an inactive stub.  Clock gate
  RCC_AHB2ENR.HASHEN; RCC_AHB2RSTR resets the model.  SHA-1 and MD5 only on the F415/F417 (RM0090 25.2: SHA-224 /
  SHA-256 are F43x only).  `params.sha2: true` gives the STM32F43x variant: CR.ALGO[1] (SHA-224 / SHA-256), CR.MDMAT,
  HASH_HR0..7 at 0x310 and CSR51..53.  Without it ALGO[1] (bit 18) and MDMAT are reserved: writing them logs one
  `warn` and they are ignored; the reserved CR bits read 0 (25.4.1).
* CR.INIT (write 1, reads 0) starts a new digest with the ALGO / MODE (HMAC) / LKEY configuration of that write; it
  clears DCIS, the input buffer and HR0..HR7 (25.4.5 note); DMAE is kept (25.4.1).  DATATYPE (00 none, 01 half-word
  swap, 10 byte swap, 11 bit reversal) applies to every DIN write; the swapped word is the next 32 message bits, most
  significant first.
* Input buffer (25.3.3: DIN + 16-word IN FIFO = 17 words): CR.DINNE = DIN holds a word, CR.NBW = words pushed into
  the FIFO behind it.  Writing a 17th word moves the 16 FIFO words (one 512-bit block) to the core: BUSY for the block
  time and SR.DINIS is set (16 locations free).  DINIS clears on a DIN write or by writing it 0 (rc_w0, 25.4.7).
  Reading DIN returns the last word written (as written, before swapping; 0 after reset, 25.4.3).
* STR (25.4.4): NBLW = valid bits of the last DIN word (0 = 32); a write with DCAL = 1 leaves NBLW unchanged (NBLW
  must be written before DCAL, as HAL does).  DCAL pads the message (any bit length) and computes the digest: BUSY
  for every remaining block, then HR0..HR4 (HASH_HR0..7 with sha2; the digest as big-endian words, as HAL_HASH reads
  it with __REV) and SR.DCIS (cleared by writing 0 or INIT).  A digest register read while the final digest is being
  computed stalls on silicon until it is ready (25.4.5): the model returns the final value.
* HMAC (MODE = 1), the three DCAL-terminated phases of RM0090 25.3.6 / HAL HMAC_Processing: (1) key: with LKEY = 0
  (key <= 64 bytes) the key words are only buffered; with LKEY = 1 the key is hashed first; the core then processes
  the block (key ^ ipad).  (2) message: the inner digest is computed and kept.  (3) key again: the core computes the
  outer hash (key ^ opad, inner digest) and sets DCIS.  BUSY covers each phase's blocks.
* Processing time per 512-bit block (HCLK cycles, RM0090 25.3.1 / 25.3.5): SHA-1 66, MD5 50, SHA-224/256 50.  The
  RM adds "at least 16 cycles" to load a block and a factor 1..2.5 (hash) / 2.5..5 (HMAC key) for the last block;
  the model charges 66 / 50 cycles per compressed block (padding blocks and HMAC pad blocks included).
* DMA: CR.DMAE requests "HASH_IN" (DMA2 stream 7 channel 2) while the input buffer accepts a word.  When the DMA
  stream serving HASH_IN writes its last item (NDTR 1 -> 0) the hardware sets DCAL itself and clears DMAE, unless
  MDMAT (sha2 parts).  SR.DMAS = DMAE.
* IRQ "HASH_RNG" (shared with RNG, `_util.shared_irq`) = SR & IMR & (DINIS | DCIS).
* Context swap registers (25.3.7, 25.4.8): CSR0..CSR50 on the F415/F417 (CSR51..53 read 0, writes ignored), CSR0..53
  with sha2.  ST documents only their reset values (CSR0 = 0x00000002, others 0) and groups: CSR22..37 hold the IN FIFO
  (not needed when NBW = 0), CSR38..50 the HMAC state (not needed for a plain hash).  CHIP_EM's layout keeps those
  groups: CSR0 = 0x5A000000 | algo (bits 0-1) | HMAC (2) | LKEY (3) | phase (4-5) | buffered words (8-13) | inner
  digest valid (16) | HMAC done (17); CSR1 = 512-bit blocks compressed in the current hash; CSR2..9 = hash state words;
  CSR10 = the DIN word; CSR22..37 = the FIFO words (message order, after swapping); CSR38..45 = HMAC inner digest.
  Until the first INIT after reset they read their reset values.  Reading them snapshots the live state; writing them
  (after INIT, as RM0090 25.3.7 and HAL_HASH_ContextRestoring do) restores it, so suspend / resume works across other
  hashes.  A key phase with LKEY = 0 longer than 17 words cannot be saved (one `warn`).  HAL saves 54 registers on
  every part (HASH_NUMBER_OF_CSR_REGISTERS); on the F415/F417 the last three are reserved and harmless.
`digests` keeps (algo, hmac, digest hex) of every completed computation.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ... import crypto_algos as ca
from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, dma_request, irq_number, make_absent, part_present, shared_irq
from .cryp import swap_word

INIT, DMAE, MODE, ALGO0, DINNE, MDMAT, LKEY, ALGO1 = 1 << 2, 1 << 3, 1 << 6, 1 << 7, 1 << 12, 1 << 13, 1 << 16, 1 << 18
DCAL = 1 << 8
DINIS, DCIS, DMAS, BUSY = 1, 2, 4, 8
ALGOS = ("sha1", "md5", "sha224", "sha256")
CYCLES = {"sha1": 66, "md5": 50, "sha224": 50, "sha256": 50}
NCSR = 54                       # register slots in the SVD; the F415/F417 implement CSR0..50 (RM0090 25.4.8)
NCSR_F415 = 51
MAGIC = 0x5A000000
CR_BITS = LKEY | ALGO0 | MODE | (3 << 4) | DMAE                  # writable CR bits on the F415/F417 (25.4.1)
CSR_DIN, CSR_FIFO, CSR_HMAC = 10, 22, 38                          # DIN word, IN FIFO (16), HMAC inner digest


@register("stm32.hash")
class HASH(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "HASH"
        self.absent = False
        ro = {"ro_mask": 0xFFFFFFFF}
        ov: dict[str, dict] = {
            "CR": {"on_write": self._cr_write, "on_read": self._cr_read},
            "DIN": {"on_write": self._din_write, "on_read": lambda b, r: self._din_last},
            "STR": {"on_write": self._str_write, "on_read": lambda b, r: self.get("STR") & 0x1F},
            "IMR": {"on_write": self._imr_write},
            "SR": {"ro_mask": 0xFFFFFFFF & ~(DINIS | DCIS), "on_write": self._sr_write, "on_read": self._sr_read},
        }
        for i in range(5):
            ov[f"HR{i}"] = {**ro, "on_read": self._hr_read(i)}
        for i in range(8):
            ov[f"HASH_HR{i}"] = {**ro, "on_read": self._hr_read(i, True)}
        sha2 = bool(params.get("sha2", False))
        for i in range(NCSR):
            if i < NCSR_F415 or sha2:
                ov[f"CSR{i}"] = {"on_read": self._csr_read(i), "on_write": self._csr_write}
            else:                       # CSR51..53: STM32F43x only
                ov[f"CSR{i}"] = {**ro, "on_read": lambda b, r: 0}
        r = build_regs((name, "HASH"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.sha2 = sha2
        self._irqn: int | None = None
        self._warned: set[str] = set()
        self.digests: deque = deque(maxlen=1000)
        self._init_state()

    def _init_state(self) -> None:
        self.alg = ca.HASHES["sha1"]
        self.hmac = False
        self.lkey = False
        self.step = 0                    # 0 plain hash; HMAC: 1 key, 2 message, 3 outer key, 4 done
        self.st: tuple = self.alg.iv
        self.nblocks = 0
        self.pending: list[int] = []
        self.inner: bytes | None = None
        self.busy_until = 0              # core cycle at which BUSY clears
        self._ev = None
        self._done_ev = None
        self._line = False
        self._dma = False
        self._restoring = False
        self._din_last = 0               # HASH_DIN reads back the last word written (25.4.3)
        self._ctx_live = False           # CSRx read their reset values until the first INIT (25.4.8)
        self._pending_digest: list[int] | None = None   # digest being computed (an HR read "stalls" until it is ready)

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if not part_present(chip, self.params):
            make_absent(self, "the hash processor (HASH, STM32F415/F417 only)")
            return
        self._irqn = irq_number(chip, "HASH_RNG", "HASH")

    def reset(self) -> None:
        if self.absent:
            MMIOBlock.reset(self)
            return
        chip = self.chip
        if chip is not None:
            for ev in (self._ev, self._done_ev):
                if ev is not None:
                    chip.sched.cancel(ev)
            shared_irq(chip, self._irqn, self.name, False)
            if self._dma:
                dma_request(chip, "HASH_IN", False)
        self._init_state()
        self._warned.clear()
        super().reset()
        self.set("SR", DINIS)
        self._update()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None and not self.absent:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ helpers
    def _now(self) -> int:
        return self.chip.now_cycles()

    def busy(self) -> bool:
        return self.chip is not None and self._now() < self.busy_until

    def _occupy(self, blocks: int) -> None:
        """The core processes `blocks` 512-bit blocks after what it is doing now."""
        if self.chip is None or blocks <= 0:
            return
        start = max(self._now(), self.busy_until)
        self.busy_until = start + blocks * CYCLES[self.alg.name]
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = self.chip.sched.at_cycles(self.busy_until, self._idle)

    def _idle(self) -> None:
        self._ev = None
        self._update()

    def _sr_flags(self, bits: int, on: bool) -> None:
        sr = self.get("SR")
        self.set("SR", (sr | bits) if on else (sr & ~bits))

    def _accepts(self) -> bool:
        """Input buffer can take a word without stalling (DMA request level)."""
        return not (len(self.pending) >= 16 and self.busy())

    def _update(self) -> None:
        chip = self.chip
        if chip is None or self.absent:
            return
        on = bool(self.get("SR") & self.get("IMR") & (DINIS | DCIS))
        if on != self._line:
            self._line = on
            shared_irq(chip, self._irqn, self.name, on)
        want = bool(self.get("CR") & DMAE) and self._accepts()
        if want != self._dma or want:
            self._dma = want
            dma_request(chip, "HASH_IN", want)

    # ------------------------------------------------------------------ input
    def _key_phase(self) -> bool:
        return self.hmac and self.step in (1, 3)

    def _push(self, w: int) -> None:
        if self.step == 4:
            self._warn_once("after", f"{self.name}: DIN written after the HMAC digest: ignored until INIT")
            return
        self.pending.append(w)
        if self._key_phase() and not self.lkey:
            if len(self.pending) > 16:
                self._warn_once("longkey", f"{self.name}: HMAC key longer than 64 bytes with CR.LKEY = 0")
            return
        if len(self.pending) == 17:
            blk = b"".join(x.to_bytes(4, "big") for x in self.pending[:16])
            self.st = self.alg.compress(self.st, blk)
            self.nblocks += 1
            self.pending = self.pending[16:]
            self._occupy(1)
            self._sr_flags(DINIS, True)                  # 16 FIFO locations free again

    def _tail(self) -> tuple[int, int]:
        """(message bits, bit count) of the buffered words, the last one cut to STR.NBLW bits."""
        if not self.pending:
            return 0, 0
        nblw = self.get("STR") & 0x1F
        valid = nblw or 32
        v = 0
        for w in self.pending:
            v = (v << 32) | w
        return v >> (32 - valid), 32 * (len(self.pending) - 1) + valid

    def _final(self, st: tuple, done_blocks: int) -> tuple[bytes, int]:
        tail, nbits = self._tail()
        st2, k = ca.finish(self.alg, st, 512 * done_blocks, tail, nbits)
        return self.alg.state_bytes(st2), k

    def _hmac_key(self) -> tuple[bytes, int]:
        """The HMAC key of a key phase (hashed when LKEY) and the blocks that took."""
        if self.lkey:
            return self._final(self.st, self.nblocks)
        tail, nbits = self._tail()
        return (tail << ((-nbits) % 8)).to_bytes((nbits + 7) // 8, "big") if nbits else b"", 0

    def _dcal(self) -> None:
        alg = self.alg
        blocks = 0
        final: bytes | None = None
        if not self.hmac:
            final, blocks = self._final(self.st, self.nblocks)
        elif self.step == 1:
            key, blocks = self._hmac_key()
            pad = bytes(b ^ 0x36 for b in key.ljust(64, b"\0"))
            self.st = alg.compress(alg.iv, pad)
            self.nblocks = 1
            blocks += 1
            self.step = 2
        elif self.step == 2:
            self.inner, blocks = self._final(self.st, self.nblocks)
            self.st, self.nblocks = alg.iv, 0
            self.step = 3
        elif self.step == 3:
            key, blocks = self._hmac_key()
            pad = bytes(b ^ 0x5C for b in key.ljust(64, b"\0"))
            st = alg.compress(alg.iv, pad)
            inner = self.inner or b""
            st, k = ca.finish(alg, st, 512, int.from_bytes(inner, "big"), 8 * len(inner))
            final = alg.state_bytes(st)
            blocks += 1 + k
            self.step = 4
        else:
            self._warn_once("dcal4", f"{self.name}: DCAL after the HMAC digest: ignored until INIT")
            return
        self.pending = []
        self._occupy(max(blocks, 1))
        if final is not None:
            if self._done_ev is not None:
                self.chip.sched.cancel(self._done_ev)
            self._pending_digest = [int.from_bytes(final[i:i + 4], "big") for i in range(0, len(final), 4)]
            self._done_ev = self.chip.sched.at_cycles(self.busy_until, self._digest_done, final)
        self._sr_flags(DINIS, True)

    def _digest_done(self, d: bytes) -> None:
        self._done_ev = None
        self._pending_digest = None
        ws = [int.from_bytes(d[i:i + 4], "big") for i in range(0, len(d), 4)]
        for i in range(5):
            self.set(f"HR{i}", ws[i] if i < len(ws) else 0)
        if self.sha2:
            for i in range(8):
                self.set(f"HASH_HR{i}", ws[i] if i < len(ws) else 0)
        self.digests.append((self.alg.name, self.hmac, d.hex()))
        self._sr_flags(DCIS, True)
        self._update()

    def _dma_last(self) -> bool:
        """The DMA stream feeding HASH_IN is moving its last item (NDTR 1, enabled)."""
        from .dma import EN, controllers
        for c in controllers(self.chip):
            for s in range(8):
                if "HASH_IN" in c.stream_request(s) and c.get(f"S{s}CR") & EN and (c.get(f"S{s}NDTR") & 0xFFFF) == 1:
                    return True
        return False

    # ------------------------------------------------------------------ context swap
    def _ctx(self) -> list[int]:
        words = [0] * NCSR
        n = len(self.pending)
        if n > 17:
            self._warn_once("ctxkey", f"{self.name}: context saved during an HMAC key phase of {n} words (LKEY = 0): "
                            f"only 17 words fit the context")
            n = 17
        words[0] = (MAGIC | ALGOS.index(self.alg.name) | (self.hmac << 2) | (self.lkey << 3) | ((self.step & 3) << 4)
                    | (n << 8) | ((self.inner is not None) << 16) | ((self.step == 4) << 17))
        words[1] = self.nblocks
        for i, v in enumerate(self.st):
            words[2 + i] = v
        if n:
            buf = self.pending[:n]
            words[CSR_DIN] = buf[-1]                       # the newest word sits in HASH_DIN
            for i, v in enumerate(buf[:-1]):               # the older ones in the IN FIFO
                words[CSR_FIFO + i] = v
        if self.inner is not None:
            for i in range(len(self.inner) // 4):
                words[CSR_HMAC + i] = int.from_bytes(self.inner[4 * i:4 * i + 4], "big")
        return words

    def _csr_read(self, i: int):
        def rd(blk, reg):
            if not self._ctx_live:
                return 2 if i == 0 else (0x20000000 if i == 2 and self.sha2 else 0)   # reset values (25.4.8)
            if self.busy():
                self._warn_once("ctxbusy", f"{self.name}: context swap registers read while BUSY (RM0090 25.3.7: "
                                f"wait until no block is being processed before saving the context)")
            return self._ctx()[i]
        return rd

    def _csr_write(self, blk, reg, old, new):
        self._store(reg, new)
        w = [self.get(f"CSR{i}") for i in range(NCSR)]
        if w[0] & 0xFF000000 != MAGIC:
            return None
        c0 = w[0]
        self.alg = ca.HASHES[ALGOS[c0 & 3]]
        self.hmac = bool(c0 >> 2 & 1)
        self.lkey = bool(c0 >> 3 & 1)
        self.step = 4 if c0 >> 17 & 1 else (c0 >> 4) & 3
        n = min((c0 >> 8) & 0x3F, 17)
        self.nblocks = w[1]
        self.st = tuple(w[2:2 + len(self.alg.iv)])
        self.pending = (list(w[CSR_FIFO:CSR_FIFO + n - 1]) + [w[CSR_DIN]]) if n else []
        self.inner = (b"".join(x.to_bytes(4, "big") for x in w[CSR_HMAC:CSR_HMAC + self.alg.digest_words])
                      if c0 >> 16 & 1 else None)
        self._ctx_live = True
        return None

    # ------------------------------------------------------------------ register hooks
    def _hr_read(self, i: int, wide: bool = False):
        def rd(blk, reg):
            if wide and not self.sha2:
                return 0
            if self._pending_digest is not None:                  # read stalled until the digest is ready (25.4.5)
                return self._pending_digest[i] if i < len(self._pending_digest) and (wide or i < 5) else 0
            return self.get(reg.name)
        return rd

    def _cr_read(self, blk, reg):
        v = self.get("CR") & ~(INIT | DINNE | (0xF << 8))
        n = len(self.pending)
        if n:
            v |= DINNE | (((n - 1) & 0xF) << 8)
        return v

    def _cr_write(self, blk, reg, old, new):
        if self.absent:
            return None
        if not self.sha2 and new & (ALGO1 | MDMAT):
            self._warn_once("sha2", f"{self.name}: CR.ALGO[1] / MDMAT (SHA-224/256, multi-DMA) exist on the "
                            f"STM32F43x only: ignored on this part")
            new &= ~(ALGO1 | MDMAT)
        new &= CR_BITS | INIT | ((ALGO1 | MDMAT) if self.sha2 else 0)
        if new & INIT:
            new &= ~INIT
            algo = (1 if new & ALGO0 else 0) | (2 if new & ALGO1 else 0)
            self.alg = ca.HASHES[ALGOS[algo]]
            self.hmac = bool(new & MODE)
            self.lkey = bool(new & LKEY)
            self.step = 1 if self.hmac else 0
            self.st, self.nblocks = self.alg.iv, 0
            self.pending = []
            self.inner = None
            if self._done_ev is not None:
                self.chip.sched.cancel(self._done_ev)
                self._done_ev = None
            self._pending_digest = None
            for i in range(5):                            # digest registers take their reset value (25.4.5)
                self.set(f"HR{i}", 0)
            for i in range(8):
                self.set(f"HASH_HR{i}", 0)
            self._ctx_live = True
            self._store(reg, new)
            self.set("SR", (self.get("SR") & ~DCIS) | DINIS)
        self._store(reg, new)
        self._update()
        return new

    def _din_write(self, blk, reg, old, new):
        if self.absent:
            return None
        cr = self.get("CR")
        self._din_last = new & 0xFFFFFFFF
        self._sr_flags(DINIS, False)
        self._push(swap_word(new, (cr >> 4) & 3))
        if cr & DMAE and self._dma_last():
            if not cr & MDMAT:
                self.set("CR", cr & ~DMAE)               # end of the DMA transfer: DCAL set by hardware
                self._dcal()
        self._update()
        return 0

    def _str_write(self, blk, reg, old, new):
        if self.absent:
            return None
        if new & DCAL:                                    # NBLW is not changed by a write with DCAL = 1 (25.4.4)
            self._store(reg, old & 0x1F)
            self._dcal()
            self._update()
            return old & 0x1F
        self._store(reg, new & 0x1F)
        return new & 0x1F

    def _imr_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    def _sr_read(self, blk, reg):
        v = self.get("SR") & (DINIS | DCIS)
        if self.get("CR") & DMAE:
            v |= DMAS
        if self.busy():
            v |= BUSY
        return v

    def _sr_write(self, blk, reg, old, new):
        v = (old & ~(DINIS | DCIS)) | (old & new & (DINIS | DCIS))       # rc_w0
        self._store(reg, v)
        self._update()
        return v
