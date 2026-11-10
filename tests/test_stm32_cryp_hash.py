"""STM32F415/F417 CRYP and HASH (chipem/periph/stm32/cryp.py, hash.py) and the pure-Python algorithms behind them
(chipem/crypto_algos.py): FIPS-197, SP 800-38A, FIPS 46-3 / SP 800-67 known answers, hashlib cross-checks, RFC 2202 /
RFC 4231 HMAC vectors, all driven through the registers of a chip built from chips/stm32f415zg.yaml (16 MHz HSI)."""
from __future__ import annotations

import hashlib
import hmac
import random
import struct
from pathlib import Path

import pytest

from chipem import crypto_algos as ca
from chipem.chipdesc import load_chip
from test_stm32 import rd, spin_chip, wr
from test_stm32_dma import CR, M0AR, M2P, MINC, NDTR, P2M, PAR, chsel, msize, psize, sreg

ROOT = Path(__file__).resolve().parent.parent
CRYP, HASH, RCC, DMA2 = 0x50060000, 0x50060400, 0x40023800, 0x40026400
RAM = 0x20001000
C_CR, C_SR, C_DIN, C_DOUT, C_DMACR, C_IMSCR, C_RISR, C_MISR = 0, 4, 8, 0xC, 0x10, 0x14, 0x18, 0x1C
K0LR, IV0LR = 0x20, 0x40
H_CR, H_DIN, H_STR, H_HR, H_IMR, H_SR, H_CSR = 0, 4, 8, 0xC, 0x20, 0x24, 0xF8
CRYPEN, FFLUSH, ALGODIR = 1 << 15, 1 << 14, 1 << 2
IFEM, IFNF, OFNE, OFFU, BUSY = 1, 2, 4, 8, 16
TDES_ECB, TDES_CBC, DES_ECB, DES_CBC, AES_ECB, AES_CBC, AES_CTR, AES_KEY = range(8)
INIT, DMAE, MODE, ALGO_MD5, LKEY = 1 << 2, 1 << 3, 1 << 6, 1 << 7, 1 << 16
DCAL = 1 << 8
DINIS, DCIS, DMAS, H_BUSY = 1, 2, 4, 8
CRYP_IRQ, HASH_IRQ = 79, 80
_DESC: dict = {}


def desc_of(name: str):
    if name not in _DESC:
        _DESC[name] = load_chip(ROOT / "chips" / f"{name}.yaml")
    return _DESC[name]


def chip_of(name: str = "stm32f415zg"):
    chip = spin_chip(desc_of(name), clock_hz=16e6)
    wr(chip, RCC + 0x34, rd(chip, RCC + 0x34) | 1 << 4 | 1 << 5)      # AHB2ENR: CRYPEN, HASHEN
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 1 << 22)              # DMA2
    return chip


@pytest.fixture
def chip():
    return chip_of()


def be_words(b: bytes) -> list[int]:
    return [int.from_bytes(b[i:i + 4], "big") for i in range(0, len(b), 4)]


def asserted(chip, n):
    return (16 + n) in chip.arch.asserted


# ================================================================================================ algorithms
FIPS197 = [("000102030405060708090a0b0c0d0e0f", "69c4e0d86a7b0430d8cdb78070b4c55a"),
           ("000102030405060708090a0b0c0d0e0f1011121314151617", "dda97ca4864cdfe06eaf70a0ec0d7191"),
           ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f", "8ea2b7ca516745bfeafc49904b496089")]
PT197 = bytes.fromhex("00112233445566778899aabbccddeeff")


@pytest.mark.parametrize("key,ct", FIPS197)
def test_aes_fips197(key, ct):
    k = bytes.fromhex(key)
    assert ca.aes_encrypt(k, PT197).hex() == ct
    assert ca.aes_decrypt(k, bytes.fromhex(ct)) == PT197
    assert ca.aes_decrypt_from_last(ca.aes_last_words(k), bytes.fromhex(ct)) == PT197
    assert ca.aes_expand_from_last(ca.aes_last_words(k)) == ca.aes_expand(k)


def test_aes_key_expansion_fips197_a1():
    w = ca.aes_expand(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"))
    assert w[4] == 0xa0fafe17 and w[43] == 0xb6630ca6


def test_des_tdes_kat():
    assert ca.des_encrypt(bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")).hex() == \
        "85e813540f0ab405"
    # FIPS 81 / NBS: key 0123456789ABCDEF, "Now is t" -> 3FA40E8A984D4815
    assert ca.des_encrypt(bytes.fromhex("0123456789ABCDEF"), b"Now is t").hex() == "3fa40e8a984d4815"
    # SP 800-67 Rev 2 example (TDEA ECB, keys K1, K2, K3)
    key = bytes.fromhex("0123456789ABCDEF23456789ABCDEF01456789ABCDEF0123")
    pt = b"The qufc"
    ct = ca.tdes_encrypt(key, pt)
    assert ct.hex() == "a826fd8ce53b855f"
    assert ca.tdes_decrypt(key, ct) == pt
    k1 = bytes.fromhex("0123456789ABCDEF")                       # K1 = K2 = K3: single DES
    assert ca.tdes_encrypt(k1 * 3, b"Now is t") == ca.des_encrypt(k1, b"Now is t")


@pytest.mark.parametrize("name", ["md5", "sha1", "sha224", "sha256"])
def test_hash_compression_vs_hashlib(name):
    rnd = random.Random(7)
    for n in (0, 1, 3, 55, 56, 63, 64, 65, 119, 200):
        d = bytes(rnd.getrandbits(8) for _ in range(n))
        assert ca.digest(name, d) == hashlib.new(name, d).digest()


# ================================================================================================ CRYP helpers
def set_key(chip, key: bytes):
    ws = be_words(key)
    for i, w in enumerate(ws):
        wr(chip, CRYP + K0LR + 4 * (8 - len(ws) + i), w)


def set_iv(chip, iv: bytes):
    for i, w in enumerate(be_words(iv)):
        wr(chip, CRYP + IV0LR + 4 * i, w)


def cr_val(mode, dec=False, keysize=16, dt=0):
    return (mode << 3) | (ALGODIR if dec else 0) | ({16: 0, 24: 1, 32: 2}[keysize] << 8) | (dt << 6)


def key_prep(chip, key):
    wr(chip, CRYP + C_CR, cr_val(AES_KEY, keysize=len(key)))
    set_key(chip, key)
    wr(chip, CRYP + C_CR, cr_val(AES_KEY, keysize=len(key)) | CRYPEN)
    assert rd(chip, CRYP + C_SR) & BUSY
    chip.run_cycles(40)
    assert not rd(chip, CRYP + C_SR) & BUSY
    assert not rd(chip, CRYP + C_CR) & CRYPEN                     # cleared by hardware


def run_cipher(chip, mode, data: bytes, key: bytes, iv: bytes | None = None, dec=False, prep=True) -> bytes:
    """Polling driver like HAL_CRYP_Encrypt/Decrypt (32-bit data type: big-endian words)."""
    if mode in (AES_ECB, AES_CBC) and dec and prep:
        key_prep(chip, key)
    else:
        set_key(chip, key)
    wr(chip, CRYP + C_CR, cr_val(mode, dec, len(key) if mode >= 4 else 16))
    if iv is not None:
        set_iv(chip, iv)
    wr(chip, CRYP + C_CR, rd(chip, CRYP + C_CR) | FFLUSH)
    wr(chip, CRYP + C_CR, rd(chip, CRYP + C_CR) | CRYPEN)
    bw = 16 if mode >= 4 else 8
    out = b""
    for i in range(0, len(data), bw):
        for w in be_words(data[i:i + bw]):
            wr(chip, CRYP + C_DIN, w)
        chip.run_cycles(60)
        for _ in range(bw // 4):
            assert rd(chip, CRYP + C_SR) & OFNE
            out += rd(chip, CRYP + C_DOUT).to_bytes(4, "big")
    return out


# SP 800-38A F.1.1 / F.2.1 / F.5.1 (AES-128)
K38A = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
P38A = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51"
                     "30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710")
ECB38A = ("3ad77bb40d7a3660a89ecaf32466ef97f5d3d58503b9699de785895a96fdbaaf"
          "43b1cd7f598ece23881b00e3ed0306887b0c785e27e8ad3f8223207104725dd4")
CBC38A = ("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2"
          "73bed6b8e3c1743b7116e69e222295163ff1caa1681fac09120eca307586e1a7")
CTR38A = ("874d6191b620e3261bef6864990db6ce9806f66b7970fdff8617187bb9fffdff"
          "5ae4df3edbd5d35e5b4f09020db03eab1e031dda2fbe03d1792170a0f3009cee")
IV38A = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
CTR0 = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")


# ================================================================================================ presence
def test_absent_on_f405_present_on_f415_f417():
    c5 = spin_chip(desc_of("stm32f405zg"), clock_hz=16e6)
    for n in ("CRYP", "HASH"):
        assert c5.peripherals[n].absent
    wr(c5, CRYP + C_DIN, 0x1234)
    assert rd(c5, CRYP + C_DIN) == 0x1234                        # plain stub storage
    assert any("not present" in str(e.get("msg", "")) for e in c5.log.filter("warn"))
    for nm in ("stm32f415zg", "stm32f417zg"):
        c = chip_of(nm)
        assert not c.peripherals["CRYP"].absent and not c.peripherals["HASH"].absent
        assert rd(c, CRYP + C_SR) == IFEM | IFNF and rd(c, HASH + H_SR) == DINIS
    assert desc_of("stm32f417zg").package == "LQFP144"


# ================================================================================================ CRYP
@pytest.mark.parametrize("key,ct", FIPS197)
def test_aes_ecb_fips197_through_registers(chip, key, ct):
    k = bytes.fromhex(key)
    assert run_cipher(chip, AES_ECB, PT197, k).hex() == ct
    assert run_cipher(chip, AES_ECB, bytes.fromhex(ct), k, dec=True) == PT197


def test_aes_ecb_cbc_ctr_sp800_38a(chip):
    assert run_cipher(chip, AES_ECB, P38A, K38A).hex() == ECB38A
    assert run_cipher(chip, AES_CBC, P38A, K38A, IV38A).hex() == CBC38A
    assert rd(chip, CRYP + IV0LR + 12) == be_words(bytes.fromhex(CBC38A))[-1]   # IV = last ciphertext block
    assert run_cipher(chip, AES_CBC, bytes.fromhex(CBC38A), K38A, IV38A, dec=True) == P38A
    assert run_cipher(chip, AES_CTR, P38A, K38A, CTR0).hex() == CTR38A
    assert rd(chip, CRYP + IV0LR + 12) == 0xfcfdff03                 # counter + 4
    assert run_cipher(chip, AES_CTR, bytes.fromhex(CTR38A), K38A, CTR0, dec=True) == P38A


def test_aes_decrypt_without_key_preparation_is_wrong(chip):
    out = run_cipher(chip, AES_ECB, bytes.fromhex(ECB38A[:32]), K38A, dec=True, prep=False)
    assert out != P38A[:16]
    assert any("key preparation" in e.get("msg", "") for e in chip.log.filter("warn"))
    assert out == ca.aes_decrypt_from_last(be_words(K38A), bytes.fromhex(ECB38A[:32]))


def test_des_tdes_through_registers(chip):
    chip2 = chip                                                 # DES key in K1 (K1LR = key[63:32])
    wr(chip2, CRYP + K0LR + 8, 0x13345779)
    wr(chip2, CRYP + K0LR + 12, 0x9BBCDFF1)
    wr(chip2, CRYP + C_CR, cr_val(DES_ECB) | CRYPEN)
    wr(chip2, CRYP + C_DIN, 0x01234567)
    wr(chip2, CRYP + C_DIN, 0x89ABCDEF)
    assert rd(chip2, CRYP + C_SR) & BUSY
    chip2.run_cycles(15)
    assert rd(chip2, CRYP + C_SR) & BUSY                         # DES: 16 cycles
    chip2.run_cycles(2)
    assert [rd(chip2, CRYP + C_DOUT) for _ in range(2)] == [0x85E81354, 0x0F0AB405]
    key = bytes.fromhex("0123456789ABCDEF23456789ABCDEF01456789ABCDEF0123")
    iv = bytes.fromhex("1122334455667788")
    pt = bytes(range(32))
    c3 = chip_of()
    for i, w in enumerate(be_words(key)):
        wr(c3, CRYP + K0LR + 8 + 4 * i, w)
    for enc in (True, False):
        wr(c3, CRYP + C_CR, cr_val(TDES_CBC, not enc))
        set_iv(c3, iv)
        wr(c3, CRYP + C_CR, rd(c3, CRYP + C_CR) | CRYPEN)
        src = pt if enc else ct
        out = b""
        for i in range(0, 32, 8):
            for w in be_words(src[i:i + 8]):
                wr(c3, CRYP + C_DIN, w)
            c3.run_cycles(50)
            out += b"".join(rd(c3, CRYP + C_DOUT).to_bytes(4, "big") for _ in range(2))
        if enc:
            ref, prev = b"", iv
            for i in range(0, 32, 8):
                prev = ca.tdes_encrypt(key, bytes(a ^ b for a, b in zip(pt[i:i + 8], prev)))
                ref += prev
            assert out == ref
            ct = out
        else:
            assert out == pt


@pytest.mark.parametrize("keysize,cycles", [(16, 14), (24, 16), (32, 18)])
def test_aes_busy_time(chip, keysize, cycles):
    set_key(chip, bytes(keysize))
    wr(chip, CRYP + C_CR, cr_val(AES_ECB, keysize=keysize) | CRYPEN)
    for w in range(3):
        wr(chip, CRYP + C_DIN, w)
    assert rd(chip, CRYP + C_SR) == IFNF                            # 3 words: not a block yet
    wr(chip, CRYP + C_DIN, 3)
    assert rd(chip, CRYP + C_SR) == IFEM | IFNF | BUSY
    chip.run_cycles(cycles - 1)
    assert rd(chip, CRYP + C_SR) & BUSY
    chip.run_cycles(2)
    assert rd(chip, CRYP + C_SR) == IFEM | IFNF | OFNE


def test_fifo_flags_flush_and_datatype(chip):
    set_key(chip, K38A)
    wr(chip, CRYP + C_CR, cr_val(AES_ECB))                          # CRYPEN = 0: data waits in the IN FIFO
    for w in range(8):
        wr(chip, CRYP + C_DIN, w)
    assert rd(chip, CRYP + C_SR) == 0
    wr(chip, CRYP + C_CR, cr_val(AES_ECB) | FFLUSH)
    assert rd(chip, CRYP + C_SR) == IFEM | IFNF and not rd(chip, CRYP + C_CR) & FFLUSH
    # byte swapping: little-endian words of the byte stream (HAL CRYP_DATATYPE_8B)
    wr(chip, CRYP + C_CR, cr_val(AES_ECB, dt=2) | CRYPEN)
    for i in range(0, 16, 4):
        wr(chip, CRYP + C_DIN, struct.unpack("<I", P38A[i:i + 4])[0])
    chip.run_cycles(20)
    out = b"".join(struct.pack("<I", rd(chip, CRYP + C_DOUT)) for _ in range(4))
    assert out.hex() == ECB38A[:32]
    # half-word and bit swapping
    from chipem.periph.stm32.cryp import swap_word
    assert swap_word(0x12345678, 1) == 0x56781234 and swap_word(0x1, 3) == 0x80000000


def test_output_fifo_full_stalls(chip):
    set_key(chip, K38A)
    wr(chip, CRYP + C_CR, cr_val(AES_ECB) | CRYPEN)
    for i in range(12):
        wr(chip, CRYP + C_DIN, i)
        chip.run_cycles(20)
    sr = rd(chip, CRYP + C_SR)
    assert sr & OFFU and not sr & IFEM                           # third block waits for OUT space
    for _ in range(4):
        rd(chip, CRYP + C_DOUT)
    chip.run_cycles(20)
    assert rd(chip, CRYP + C_SR) & IFEM


def test_cryp_irq(chip):
    set_key(chip, K38A)
    wr(chip, CRYP + C_CR, cr_val(AES_ECB) | CRYPEN)
    assert rd(chip, CRYP + C_RISR) == 1 and rd(chip, CRYP + C_MISR) == 0
    wr(chip, CRYP + C_IMSCR, 1)
    assert asserted(chip, CRYP_IRQ)
    wr(chip, CRYP + C_IMSCR, 2)
    assert not asserted(chip, CRYP_IRQ)
    for w in range(4):
        wr(chip, CRYP + C_DIN, w)
    chip.run_cycles(20)
    assert rd(chip, CRYP + C_MISR) == 2 and asserted(chip, CRYP_IRQ)
    for _ in range(4):
        rd(chip, CRYP + C_DOUT)
    assert not asserted(chip, CRYP_IRQ)


def test_cryp_dma(chip):
    for i, w in enumerate(be_words(P38A)):
        chip.bus.write(RAM + 4 * i, 4, int.from_bytes(w.to_bytes(4, "big"), "little"))   # byte stream in RAM
    set_key(chip, K38A)
    wr(chip, CRYP + C_CR, cr_val(AES_CBC, dt=2))
    set_iv(chip, IV38A)
    sz = psize(4) | msize(4) | chsel(2) | MINC
    wr(chip, sreg(DMA2, 6, PAR), CRYP + C_DIN)
    wr(chip, sreg(DMA2, 6, M0AR), RAM)
    wr(chip, sreg(DMA2, 6, NDTR), 16)
    wr(chip, sreg(DMA2, 6, CR), sz | M2P | 1)
    wr(chip, sreg(DMA2, 5, PAR), CRYP + C_DOUT)
    wr(chip, sreg(DMA2, 5, M0AR), RAM + 0x100)
    wr(chip, sreg(DMA2, 5, NDTR), 16)
    wr(chip, sreg(DMA2, 5, CR), sz | P2M | 1)
    wr(chip, CRYP + C_DMACR, 3)
    wr(chip, CRYP + C_CR, rd(chip, CRYP + C_CR) | CRYPEN)
    chip.run_cycles(500)
    out = b"".join(chip.bus.read(RAM + 0x100 + 4 * i, 4).to_bytes(4, "little") for i in range(16))
    assert out.hex() == CBC38A
    assert rd(chip, sreg(DMA2, 5, NDTR)) == 0 and rd(chip, sreg(DMA2, 6, NDTR)) == 0


# ================================================================================================ HASH helpers
def hash_regs(chip, algo_bits, data: bytes, *, dt=2, nbits=None):
    """Write `data` as little-endian words with byte swapping (HAL HASH_DATATYPE_8B) and set NBLW."""
    wr(chip, HASH + H_CR, algo_bits | (dt << 4) | INIT)
    feed(chip, data, nbits)
    nb = (nbits if nbits is not None else 8 * len(data)) % 32
    wr(chip, HASH + H_STR, nb | DCAL)
    chip.run_cycles(66 * ((len(data) // 64) + 3))
    return digest_of(chip)


def feed(chip, data: bytes, nbits=None):
    if nbits is not None:
        data = data[:(nbits + 7) // 8]
    for i in range(0, len(data), 4):
        wr(chip, HASH + H_DIN, int.from_bytes(data[i:i + 4].ljust(4, b"\0"), "little"))


def digest_of(chip, n=5):
    return b"".join(rd(chip, HASH + H_HR + 4 * i).to_bytes(4, "big") for i in range(n))


@pytest.mark.parametrize("name,bits,n", [("sha1", 0, 5), ("md5", ALGO_MD5, 4)])
def test_hash_vs_hashlib(chip, name, bits, n):
    rnd = random.Random(3)
    for ln in (0, 1, 2, 3, 4, 5, 55, 56, 63, 64, 65, 128, 300):
        d = bytes(rnd.getrandbits(8) for _ in range(ln))
        assert hash_regs(chip, bits, d)[:4 * n] == hashlib.new(name, d).digest(), ln
        assert rd(chip, HASH + H_SR) & DCIS


def test_hash_abc_32bit_datatype(chip):
    wr(chip, HASH + H_CR, INIT)                                    # DATATYPE 00: big-endian words
    wr(chip, HASH + H_DIN, 0x61626300)
    wr(chip, HASH + H_STR, 24 | DCAL)
    chip.run_cycles(100)
    assert digest_of(chip).hex() == "a9993e364706816aba3e25717850c26c9cd0d89d"


def test_hash_nblw_bit_lengths(chip):
    """NBLW counts bits: a 5-bit message (no whole byte) is padded at the bit level."""
    wr(chip, HASH + H_CR, INIT)
    wr(chip, HASH + H_DIN, 0xA8000000)                              # bits 10101
    wr(chip, HASH + H_STR, 5 | DCAL)
    chip.run_cycles(100)
    st, _ = ca.finish(ca.HASHES["sha1"], ca.HASHES["sha1"].iv, 0, 0b10101, 5)
    assert digest_of(chip) == ca.HASHES["sha1"].state_bytes(st)


def test_hash_flags_nbw_busy_timing(chip):
    wr(chip, HASH + H_CR, 2 << 4 | INIT)
    assert rd(chip, HASH + H_SR) == DINIS and rd(chip, HASH + H_CR) & 0x1F00 == 0
    wr(chip, HASH + H_DIN, 1)
    assert rd(chip, HASH + H_SR) == 0                               # cleared by the DIN write
    assert rd(chip, HASH + H_CR) & 0x1F00 == 0x1000                  # DINNE, NBW 0
    for i in range(15):
        wr(chip, HASH + H_DIN, i)
    assert rd(chip, HASH + H_CR) & 0x1F00 == 0x1F00                  # DINNE, NBW 15
    wr(chip, HASH + H_DIN, 99)                                      # 17th word: block to the core
    assert rd(chip, HASH + H_SR) == DINIS | H_BUSY
    chip.run_cycles(65)
    assert rd(chip, HASH + H_SR) & H_BUSY
    chip.run_cycles(2)
    assert rd(chip, HASH + H_SR) == DINIS
    wr(chip, HASH + H_SR, 0)                                        # rc_w0
    assert rd(chip, HASH + H_SR) == 0


def test_hash_irq_shared_with_rng(chip):
    wr(chip, HASH + H_IMR, 2)                                       # DCIE
    hash_regs(chip, 0, b"abc")
    assert asserted(chip, HASH_IRQ)
    wr(chip, HASH + H_SR, 0)
    assert not asserted(chip, HASH_IRQ)
    wr(chip, HASH + H_IMR, 1)                                       # DINIE: DINIS after INIT
    wr(chip, HASH + H_CR, INIT)
    assert asserted(chip, HASH_IRQ)


RFC2202 = [
    ("sha1", bytes([0x0b] * 20), b"Hi There", "b617318655057264e28bc0b6fb378c8ef146be00"),
    ("sha1", b"Jefe", b"what do ya want for nothing?", "effcdf6ae5eb2fa2d27416d5f184df9c259a7c79"),
    ("sha1", bytes([0xaa] * 80), b"Test Using Larger Than Block-Size Key - Hash Key First",
     "aa4ae5e15272d00e95705637ce8a3b55ed402112"),
    ("md5", bytes([0x0b] * 16), b"Hi There", "9294727a3638bb1c13f48ef8158bfc9d"),
    ("md5", bytes([0xaa] * 80), b"Test Using Larger Than Block-Size Key - Hash Key First",
     "6b1ab7fe4bd7bf8f0b62e6ce61b9d0cd"),
]


def hmac_regs(chip, name, key, msg, *, extra=0):
    bits = (ALGO_MD5 if name == "md5" else 0) | extra
    wr(chip, HASH + H_CR, bits | MODE | (LKEY if len(key) > 64 else 0) | 2 << 4 | INIT)
    for data in (key, msg, key):                                    # HAL HMAC_Processing steps 1-3
        feed(chip, data)
        wr(chip, HASH + H_STR, (8 * len(data)) % 32 | DCAL)
        chip.run_cycles(66 * 6)
        assert not rd(chip, HASH + H_SR) & H_BUSY
    return digest_of(chip)


@pytest.mark.parametrize("name,key,msg,mac", RFC2202)
def test_hmac_rfc2202(chip, name, key, msg, mac):
    assert hmac_regs(chip, name, key, msg)[:len(mac) // 2].hex() == mac


def test_sha2_reserved_on_f415_and_rfc4231_with_sha2_param(chip):
    wr(chip, HASH + H_CR, 1 << 18 | INIT)                           # ALGO[1]: reserved on F415/F417 -> SHA-1
    assert any("STM32F43x" in e.get("msg", "") for e in chip.log.filter("warn"))
    assert chip.peripherals["HASH"].alg.name == "sha1"
    h = chip.peripherals["HASH"]
    h.sha2 = True                                                   # the F43x variant (params.sha2)
    key, msg = bytes([0x0b] * 20), b"Hi There"
    ref = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"   # RFC 4231 4.2
    assert hmac_regs(chip, "sha256", key, msg, extra=1 << 18 | ALGO_MD5)[:20].hex() == ref[:40]
    out = b"".join(rd(chip, HASH + 0x310 + 4 * i).to_bytes(4, "big") for i in range(8))
    assert out.hex() == ref
    assert out == hmac.new(key, msg, hashlib.sha256).digest()


def test_context_swap(chip):
    rnd = random.Random(11)
    a = bytes(rnd.getrandbits(8) for _ in range(200))
    b = bytes(rnd.getrandbits(8) for _ in range(77))
    wr(chip, HASH + H_IMR, 1)
    wr(chip, HASH + H_CR, ALGO_MD5 | 2 << 4 | INIT)
    feed(chip, a[:100])                                             # interrupted after 25 words
    chip.run_cycles(200)
    saved = [rd(chip, HASH + H_IMR), rd(chip, HASH + H_STR), rd(chip, HASH + H_CR) & 0x500F0]
    saved += [rd(chip, HASH + H_CSR + 4 * i) for i in range(54)]
    assert hash_regs(chip, 0, b) == hashlib.sha1(b).digest()          # another message meanwhile
    wr(chip, HASH + H_IMR, saved[0])                                # HAL_HASH_ContextRestoring order
    wr(chip, HASH + H_STR, saved[1])
    wr(chip, HASH + H_CR, saved[2])
    wr(chip, HASH + H_CR, saved[2] | INIT)
    for i in range(54):
        wr(chip, HASH + H_CSR + 4 * i, saved[3 + i])
    feed(chip, a[100:])
    wr(chip, HASH + H_STR, (8 * len(a)) % 32 | DCAL)
    chip.run_cycles(400)
    assert digest_of(chip, 4) == hashlib.md5(a).digest()


def test_hmac_context_swap_between_phases(chip):
    key, msg = b"Jefe", b"what do ya want for nothing?"
    wr(chip, HASH + H_CR, MODE | 2 << 4 | INIT)
    feed(chip, key)
    wr(chip, HASH + H_STR, 0 | DCAL)
    chip.run_cycles(300)
    feed(chip, msg[:12])
    saved = [rd(chip, HASH + H_CR) & 0x500F0] + [rd(chip, HASH + H_CSR + 4 * i) for i in range(54)]
    hash_regs(chip, ALGO_MD5, b"other")
    wr(chip, HASH + H_CR, saved[0] | INIT)
    for i in range(54):
        wr(chip, HASH + H_CSR + 4 * i, saved[1 + i])
    feed(chip, msg[12:])
    wr(chip, HASH + H_STR, 0 | DCAL)
    chip.run_cycles(300)
    feed(chip, key)
    wr(chip, HASH + H_STR, 0 | DCAL)
    chip.run_cycles(300)
    assert digest_of(chip).hex() == "effcdf6ae5eb2fa2d27416d5f184df9c259a7c79"


def test_hash_dma_auto_dcal(chip):
    rnd = random.Random(5)
    d = bytes(rnd.getrandbits(8) for _ in range(150))
    for i in range(0, len(d), 4):
        chip.bus.write(RAM + i, 4, int.from_bytes(d[i:i + 4].ljust(4, b"\0"), "little"))
    wr(chip, HASH + H_CR, 2 << 4 | INIT)
    wr(chip, HASH + H_STR, (8 * len(d)) % 32)
    n = (len(d) + 3) // 4
    wr(chip, sreg(DMA2, 7, PAR), HASH + H_DIN)
    wr(chip, sreg(DMA2, 7, M0AR), RAM)
    wr(chip, sreg(DMA2, 7, NDTR), n)
    wr(chip, sreg(DMA2, 7, CR), psize(4) | msize(4) | chsel(2) | MINC | M2P | 1)
    wr(chip, HASH + H_CR, rd(chip, HASH + H_CR) | DMAE)
    assert rd(chip, HASH + H_SR) & DMAS
    chip.run_cycles(2000)
    assert rd(chip, sreg(DMA2, 7, NDTR)) == 0
    assert not rd(chip, HASH + H_CR) & DMAE                         # cleared by hardware at the end
    assert rd(chip, HASH + H_SR) & DCIS
    assert digest_of(chip) == hashlib.sha1(d).digest()


def test_rcc_reset_and_gate(chip):
    set_key(chip, K38A)
    wr(chip, CRYP + C_CR, cr_val(AES_ECB))
    wr(chip, CRYP + C_DIN, 5)
    wr(chip, RCC + 0x14, 1 << 4)                                    # AHB2RSTR.CRYPRST
    wr(chip, RCC + 0x14, 0)
    assert rd(chip, CRYP + C_SR) == IFEM | IFNF and rd(chip, CRYP + C_CR) == 0
    assert rd(chip, RCC + 0x34) & 0x30 == 0x30                    # CRYPEN/HASHEN writable (absent from the SVD)
    assert not any("clock is disabled" in e.get("msg", "") for e in chip.log.filter("warn"))
    wr(chip, RCC + 0x34, 0)                                       # gate off: one warn
    wr(chip, CRYP + C_CR, 0)
    assert any("clock is disabled" in e.get("msg", "") for e in chip.log.filter("warn"))
