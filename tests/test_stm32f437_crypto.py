"""STM32F437 CRYP AES-GCM / GMAC / CCM and HASH SHA-224 / SHA-256 / HMAC (RM0090 Rev 22 ch. 23 and 25, the
"STM32F42xxx/43xxx only" parts) on chips/stm32f437ai.yaml (`params: {gcm: true}` / `{sha2: true}`), the reference
algorithms in chipem/crypto_algos.py against the published vectors (McGrew-Viega GCM test cases = SP 800-38D
examples, SP 800-38C appendix C, RFC 4231), the F427/F417 gating and the f42x_gcm sample end to end."""
from __future__ import annotations

import hashlib
import hmac
import random
from pathlib import Path

import pytest

from chipem import crypto_algos as ca
from chipem.chipdesc import load_chip
from chipem.session import Session
from test_stm32 import rd, spin_chip, wr

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "firmware" / "samples_stm32_variants" / "build" / "f42x_gcm.elf"
CRYP, HASH, RCC = 0x50060000, 0x50060400, 0x40023800
C_CR, C_SR, C_DIN, C_DOUT, C_DMACR, C_IMSCR, C_RISR, C_MISR = 0, 4, 8, 0xC, 0x10, 0x14, 0x18, 0x1C
K0LR, IV0LR, CSGCMCCM0, CSGCM0 = 0x20, 0x40, 0x50, 0x70
H_CR, H_DIN, H_STR, H_HR, H_IMR, H_SR, H_CSR, H_HR8 = 0, 4, 8, 0xC, 0x20, 0x24, 0xF8, 0x310
CRYPEN, FFLUSH, ALGODIR, ALGOMODE3 = 1 << 15, 1 << 14, 1 << 2, 1 << 19
IFEM, IFNF, OFNE, OFFU, BUSY = 1, 2, 4, 8, 16
INIT_PH, HEADER, PAYLOAD, FINAL = 0, 1 << 16, 2 << 16, 3 << 16
GCM, CCM = ALGOMODE3, ALGOMODE3 | 1 << 3
INIT, MODE, ALGO0, ALGO1, LKEY, DCAL = 1 << 2, 1 << 6, 1 << 7, 1 << 18, 1 << 16, 1 << 8
SHA224, SHA256 = ALGO1, ALGO1 | ALGO0
DINIS, DCIS, H_BUSY = 1, 2, 8
CRYP_IRQ, HASH_IRQ = 79, 80
H = bytes.fromhex
_DESC: dict = {}

# McGrew & Viega, "The Galois/Counter Mode of Operation", test cases 1-4 (AES-128)
K3 = H("feffe9928665731c6d6a8f9467308308")
IV3 = H("cafebabefacedbaddecaf888")
P3 = H("d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
       "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255")
C3 = ("42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e"
      "21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091473f5985")
T3 = "4d5c2af327cd64a62cf35abd2ba6fab4"
A4 = H("feedfacedeadbeeffeedfacedeadbeefabaddad2")
T4 = "5bc94fbc3221a5db94fae95ae7121a47"
# NIST SP 800-38C appendix C, examples 1-3
KCCM = H("404142434445464748494a4b4c4d4e4f")
CCM_EX = [
    (H("10111213141516"), H("0001020304050607"), H("20212223"), 4, "7162015b", "4dac255d"),
    (H("1011121314151617"), H("000102030405060708090a0b0c0d0e0f"), H("202122232425262728292a2b2c2d2e2f"), 6,
     "d2a1f0e051ea5f62081a7792073d593d", "1fc64fbfaccd"),
    (H("101112131415161718191a1b"), H("000102030405060708090a0b0c0d0e0f10111213"),
     H("202122232425262728292a2b2c2d2e2f3031323334353637"), 8,
     "e3b201a9f5b71a7a9b1ceaeccd97e70b6176aad9a4428aa5", "484392fbc1b09951"),
]


def desc_of(name: str):
    if name not in _DESC:
        _DESC[name] = load_chip(ROOT / "chips" / f"{name}.yaml")
    return _DESC[name]


def chip_of(name: str = "stm32f437ai"):
    chip = spin_chip(desc_of(name), clock_hz=16e6)
    wr(chip, RCC + 0x34, rd(chip, RCC + 0x34) | 1 << 4 | 1 << 5)      # AHB2ENR: CRYPEN, HASHEN
    return chip


@pytest.fixture
def chip():
    return chip_of()


def be_words(b: bytes) -> list[int]:
    return [int.from_bytes(b[i:i + 4], "big") for i in range(0, len(b), 4)]


def asserted(chip, n):
    return (16 + n) in chip.arch.asserted


# ================================================================================================ algorithms
def test_gcm_reference_mcgrew_viega():
    assert ca.gcm_encrypt(bytes(16), bytes(12), b"")[1].hex() == "58e2fccefa7e3061367f1d57a4e7455a"      # TC1
    c, t = ca.gcm_encrypt(bytes(16), bytes(12), bytes(16))                                              # TC2
    assert (c.hex(), t.hex()) == ("0388dace60b6a392f328c2b971b2fe78", "ab6e47d42cec13bdf53a67b21257bddf")
    c, t = ca.gcm_encrypt(K3, IV3, P3)                                                                  # TC3
    assert (c.hex(), t.hex()) == (C3, T3)
    c, t = ca.gcm_encrypt(K3, IV3, P3[:60], A4)                                                         # TC4
    assert (c.hex(), t.hex()) == (C3[:120], T4)
    assert ca.gcm_decrypt(K3, IV3, c, A4) == (P3[:60], H(T4))


@pytest.mark.parametrize("nonce,aad,pt,tlen,ct,tag", CCM_EX)
def test_ccm_reference_sp800_38c(nonce, aad, pt, tlen, ct, tag):
    c, t = ca.ccm_encrypt(KCCM, nonce, pt, aad, tlen)
    assert (c.hex(), t.hex()) == (ct, tag)


# ================================================================================================ CRYP drivers
def set_key(chip, key: bytes):
    ws = be_words(key)
    for i, w in enumerate(ws):
        wr(chip, CRYP + K0LR + 4 * (8 - len(ws) + i), w)


def set_iv(chip, iv: bytes):
    for i, w in enumerate(be_words(iv)):
        wr(chip, CRYP + IV0LR + 4 * i, w)


def ks_bits(key) -> int:
    return {16: 0, 24: 1, 32: 2}[len(key)] << 8


def cr(chip, v):
    wr(chip, CRYP + C_CR, v)


def blocks(data: bytes):
    data = data + bytes(-len(data) % 16)
    return [data[i:i + 16] for i in range(0, len(data), 16)]


def push(chip, blk: bytes):
    for w in be_words(blk):
        wr(chip, CRYP + C_DIN, w)


def pull(chip, n=4) -> bytes:
    return b"".join(rd(chip, CRYP + C_DOUT).to_bytes(4, "big") for _ in range(n))


def gcm_init(chip, key, iv12, mode=GCM):
    """RM0090 23.3.2 GCM init phase a-g (HAL order: phase 00, key, initial counter block with counter 2, CRYPEN)."""
    cr(chip, mode | ks_bits(key) | INIT_PH)
    set_key(chip, key)
    set_iv(chip, iv12 + b"\0\0\0\2")
    cr(chip, mode | ks_bits(key) | INIT_PH | CRYPEN)
    assert rd(chip, CRYP + C_SR) & BUSY
    chip.run_cycles(40)
    assert not rd(chip, CRYP + C_SR) & BUSY
    assert not rd(chip, CRYP + C_CR) & CRYPEN                     # cleared by hardware (step f)


def header(chip, key, aad, mode=GCM):
    cr(chip, mode | ks_bits(key) | HEADER | CRYPEN)
    for b in blocks(aad) if aad else []:
        push(chip, b)
        chip.run_cycles(20)
        assert not rd(chip, CRYP + C_SR) & OFNE                   # no output in the header phase


def payload(chip, key, data, dec=False, mode=GCM) -> bytes:
    cr(chip, mode | ks_bits(key) | PAYLOAD)                         # HAL: disable, select the phase, enable
    cr(chip, mode | ks_bits(key) | PAYLOAD | (ALGODIR if dec else 0) | CRYPEN)
    out = b""
    for b in blocks(data) if data else []:
        push(chip, b)
        chip.run_cycles(40)
        out += pull(chip)
    return out[:len(data)]


def final(chip, key, blk: bytes, mode=GCM) -> bytes:
    cr(chip, mode | ks_bits(key) | FINAL)
    cr(chip, mode | ks_bits(key) | FINAL | CRYPEN)
    push(chip, blk)
    chip.run_cycles(40)
    assert rd(chip, CRYP + C_SR) & OFNE
    tag = pull(chip)
    cr(chip, mode | ks_bits(key) | FINAL)
    return tag


def gcm_regs(chip, key, iv12, pt, aad=b"", dec=False) -> tuple[bytes, bytes]:
    gcm_init(chip, key, iv12)
    header(chip, key, aad)
    out = payload(chip, key, pt, dec)
    lens = (8 * len(aad)).to_bytes(8, "big") + (8 * len(pt)).to_bytes(8, "big")
    return out, final(chip, key, lens)


def ccm_regs(chip, key, nonce, aad, data, tlen, dec=False) -> tuple[bytes, bytes]:
    """RM0090 23.3.2 CCM: software formats B0, B1.. (header) and the counters (HAL CRYP_AESCCM_Process)."""
    b0, a, ctr0 = ca.ccm_blocks(nonce, aad, len(data), tlen)
    cr(chip, CCM | ks_bits(key) | INIT_PH)
    set_key(chip, key)
    set_iv(chip, ctr0[:15] + b"\1")                                # CTR1
    cr(chip, CCM | ks_bits(key) | INIT_PH | CRYPEN)
    push(chip, b0)
    chip.run_cycles(40)
    assert not rd(chip, CRYP + C_CR) & CRYPEN and not rd(chip, CRYP + C_SR) & OFNE
    header(chip, key, a, CCM)
    out = payload(chip, key, data, dec, CCM)
    return out, final(chip, key, ctr0, CCM)[:tlen]


# ================================================================================================ GCM / GMAC / CCM
def test_gcm_test_case_2_and_3_through_registers(chip):
    assert gcm_regs(chip, bytes(16), bytes(12), bytes(16)) == (H("0388dace60b6a392f328c2b971b2fe78"),
                                                               H("ab6e47d42cec13bdf53a67b21257bddf"))
    assert gcm_regs(chip, K3, IV3, P3) == (H(C3), H(T3))
    assert gcm_regs(chip, K3, IV3, P3, A4) == ca.gcm_encrypt(K3, IV3, P3, A4)   # header padded with zeros
    assert chip.peripherals["CRYP"].trace[-1][:2] == ("AES-GCM", "final")


def test_gcm_decrypt_and_256_bit_key(chip):
    ct, tag = gcm_regs(chip, K3, IV3, H(C3), A4, dec=True)
    assert ct == P3 and tag == ca.gcm_encrypt(K3, IV3, P3, A4)[1]
    k = bytes(range(32))
    assert gcm_regs(chip, k, IV3, P3[:32], A4) == ca.gcm_encrypt(k, IV3, P3[:32], A4)


def test_gmac_header_only(chip):
    """GMAC = GCM with a header and no payload phase (RM0090 23.3.2): test case 1 and a 20-byte AAD."""
    gcm_init(chip, bytes(16), bytes(12))
    header(chip, bytes(16), b"")
    assert final(chip, bytes(16), bytes(16)).hex() == "58e2fccefa7e3061367f1d57a4e7455a"
    gcm_init(chip, K3, IV3)
    header(chip, K3, A4)
    tag = final(chip, K3, (8 * len(A4)).to_bytes(8, "big") + bytes(8))
    assert tag == ca.gcm_encrypt(K3, IV3, b"", A4)[1]


def test_gcm_partial_last_block_tag_like_silicon(chip):
    """Test case 4 (60-byte payload, zero-padded by software): the hardware hashes the whole last output block, so
    the encryption tag is wrong (HAL's 'Workaround 2'); decryption hashes the zero-padded input and is right."""
    ct, tag = gcm_regs(chip, K3, IV3, P3[:60], A4)
    assert ct == H(C3)[:60] and tag.hex() != T4
    pt, tag = gcm_regs(chip, K3, IV3, H(C3)[:60], A4, dec=True)
    assert pt == P3[:60] and tag.hex() == T4


@pytest.mark.parametrize("ex", [0, 1, 2])
def test_ccm_sp800_38c_through_registers(chip, ex):
    nonce, aad, pt, tlen, ct, tag = CCM_EX[ex]
    assert ccm_regs(chip, KCCM, nonce, aad, pt, tlen) == (H(ct), H(tag))   # zero-padded plaintext MACs correctly


def test_ccm_decrypt_full_blocks(chip):
    nonce, aad, pt, tlen, ct, tag = CCM_EX[1]
    assert ccm_regs(chip, KCCM, nonce, aad, H(ct), tlen, dec=True) == (pt, H(tag))


def test_phase_timings_table_112(chip):
    """RM0090 Table 112 (STM32F43xxx), AES-128: GCM init 24, header 10, payload 14, tag 14; CCM payload 25."""
    def busy_for(write):
        write()
        t0 = chip.now_cycles()
        n = 0
        while rd(chip, CRYP + C_SR) & BUSY:
            chip.run_cycles(1)
            n += 1
        return chip.now_cycles() - t0 if n else 0

    k = K3
    cr(chip, GCM | INIT_PH)
    set_key(chip, k)
    set_iv(chip, IV3 + b"\0\0\0\2")
    assert 23 <= busy_for(lambda: cr(chip, GCM | INIT_PH | CRYPEN)) <= 26
    cr(chip, GCM | HEADER | CRYPEN)
    assert 9 <= busy_for(lambda: push(chip, A4[:16])) <= 12
    cr(chip, GCM | PAYLOAD | CRYPEN)
    assert 13 <= busy_for(lambda: push(chip, P3[:16])) <= 16
    pull(chip)
    k256 = bytes(32)
    cr(chip, GCM | ks_bits(k256) | INIT_PH)
    set_key(chip, k256)
    assert 31 <= busy_for(lambda: cr(chip, GCM | ks_bits(k256) | INIT_PH | CRYPEN)) <= 34
    cr(chip, CCM | PAYLOAD | CRYPEN)
    set_key(chip, k)
    assert 24 <= busy_for(lambda: push(chip, P3[:16])) <= 27


def test_crypen_auto_clear_and_no_reinit(chip):
    """After the init phase CRYPEN reads 0; setting it again in phase 00 (RM0090 23.3.2 step g) does not restart the
    init (no BUSY) and the header phase then continues the message."""
    gcm_init(chip, K3, IV3)
    cr(chip, GCM | INIT_PH | CRYPEN)
    assert not rd(chip, CRYP + C_SR) & BUSY and rd(chip, CRYP + C_CR) & CRYPEN
    header(chip, K3, A4)
    out = payload(chip, K3, P3)
    tag = final(chip, K3, (8 * len(A4)).to_bytes(8, "big") + (8 * len(P3)).to_bytes(8, "big"))
    assert (out, tag) == ca.gcm_encrypt(K3, IV3, P3, A4)


def test_counter_in_iv_registers(chip):
    gcm_init(chip, K3, IV3)
    header(chip, K3, b"")
    payload(chip, K3, P3[:32])
    assert rd(chip, CRYP + IV0LR + 12) == 4                         # counter 2 -> 4 after two payload blocks


def test_context_swap_gcm_mid_payload(chip):
    """RM0090 23.3.7: save CR, IVs and CSGCMCCM0..7 / CSGCM0..7 mid message, run another GCM, restore, finish."""
    gcm_init(chip, K3, IV3)
    header(chip, K3, A4)
    out = payload(chip, K3, P3[:32])
    assert rd(chip, CRYP + C_SR) == IFEM | IFNF                     # FIFOs empty, not busy
    cr(chip, rd(chip, CRYP + C_CR) & ~CRYPEN)
    saved_cr = rd(chip, CRYP + C_CR)
    saved_iv = [rd(chip, CRYP + IV0LR + 4 * i) for i in range(4)]
    saved_cs = [rd(chip, CRYP + CSGCMCCM0 + 4 * i) for i in range(16)]
    assert any(saved_cs[8:12])                                      # H is in CSGCM0..3
    assert gcm_regs(chip, bytes(16), bytes(12), bytes(16))[1].hex() == "ab6e47d42cec13bdf53a67b21257bddf"
    cr(chip, saved_cr)                                              # restore: configuration, key, IV, context
    set_key(chip, K3)
    for i, w in enumerate(saved_iv):
        wr(chip, CRYP + IV0LR + 4 * i, w)
    for i, w in enumerate(saved_cs):
        wr(chip, CRYP + CSGCMCCM0 + 4 * i, w)
    cr(chip, saved_cr | CRYPEN)
    for b in blocks(P3[32:]):
        push(chip, b)
        chip.run_cycles(40)
        out += pull(chip)
    tag = final(chip, K3, (8 * len(A4)).to_bytes(8, "big") + (8 * len(P3)).to_bytes(8, "big"))
    assert (out, tag) == ca.gcm_encrypt(K3, IV3, P3, A4)


def test_output_irq_and_dma_requests(chip):
    wr(chip, CRYP + C_IMSCR, 2)                                     # OUTIM
    gcm_init(chip, K3, IV3)
    header(chip, K3, A4)
    assert not asserted(chip, CRYP_IRQ)                             # header blocks give no output
    cr(chip, GCM | PAYLOAD | CRYPEN)
    push(chip, P3[:16])
    chip.run_cycles(20)
    assert rd(chip, CRYP + C_MISR) == 2 and asserted(chip, CRYP_IRQ)
    pull(chip)
    assert not asserted(chip, CRYP_IRQ)
    wr(chip, CRYP + C_IMSCR, 1)                                     # INIM: IN FIFO below 4 words, CRYPEN = 1
    assert asserted(chip, CRYP_IRQ)


def test_reserved_algomode(chip):
    cr(chip, ALGOMODE3 | 2 << 3 | CRYPEN)                           # 1010: reserved
    push(chip, bytes(16))
    chip.run_cycles(40)
    assert not rd(chip, CRYP + C_SR) & (BUSY | OFNE)
    assert any("reserved" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


def test_f417_gcm_bits_reserved_and_f427_absent():
    """STM32F417 (no `gcm` param): CR.ALGOMODE[3] and GCM_CCMPH are reserved; a write of them is dropped (the bits
    read 0, one `warn`), so ALGOMODE 1000 (GCM) reads back as 000 = TDES-ECB, and CSGCM*R read 0.  STM32F427: CRYP
    and HASH are inactive stubs (`only: [STM32F437, STM32F439]`)."""
    c7 = chip_of("stm32f417zg")
    cr(c7, GCM | HEADER)
    assert rd(c7, CRYP + C_CR) == 0
    assert any("F43x" in e.fields.get("msg", "") for e in c7.log if e.kind == "warn")
    assert rd(c7, CRYP + CSGCMCCM0) == 0
    c27 = chip_of("stm32f427vg")
    assert c27.peripherals["CRYP"].absent and c27.peripherals["HASH"].absent


# ================================================================================================ HASH
def hash_regs(chip, algo, data: bytes, nbits=None, extra=0):
    wr(chip, HASH + H_CR, algo | extra | 2 << 4 | INIT)            # DATATYPE 10: byte swap (HAL 8-bit data)
    feed(chip, data, nbits)
    nb = (nbits if nbits is not None else 8 * len(data)) % 32
    wr(chip, HASH + H_STR, nb)
    wr(chip, HASH + H_STR, nb | DCAL)
    chip.run_cycles(66 * (len(data) // 64 + 4))
    return b"".join(rd(chip, HASH + H_HR8 + 4 * i).to_bytes(4, "big") for i in range(8))


def feed(chip, data: bytes, nbits=None):
    if nbits is not None:
        data = data[:(nbits + 7) // 8]
    for i in range(0, len(data), 4):
        wr(chip, HASH + H_DIN, int.from_bytes(data[i:i + 4].ljust(4, b"\0"), "little"))


@pytest.mark.parametrize("name,algo", [("sha224", SHA224), ("sha256", SHA256)])
def test_sha2_vs_hashlib(chip, name, algo):
    rnd = random.Random(5)
    n = 28 if name == "sha224" else 32
    for ln in (0, 3, 55, 56, 64, 65, 200):
        d = bytes(rnd.getrandbits(8) for _ in range(ln))
        assert hash_regs(chip, algo, d)[:n] == hashlib.new(name, d).digest(), ln
    d = hash_regs(chip, SHA256, b"abc")
    assert d.hex() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert b"".join(rd(chip, HASH + H_HR + 4 * i).to_bytes(4, "big") for i in range(5)) == d[:20]   # HR0..4


def test_sha256_nblw_bits_and_timing(chip):
    """NBLW: a 5-bit message; BUSY 50 HCLK cycles per SHA-256 block (RM0090 25.3.1)."""
    wr(chip, HASH + H_CR, SHA256 | INIT)
    wr(chip, HASH + H_DIN, 0xA8000000)
    wr(chip, HASH + H_STR, 5)
    wr(chip, HASH + H_STR, 5 | DCAL)
    assert rd(chip, HASH + H_SR) & H_BUSY
    chip.run_cycles(49)
    assert rd(chip, HASH + H_SR) & H_BUSY
    chip.run_cycles(2)
    assert rd(chip, HASH + H_SR) == DINIS | DCIS
    st, _ = ca.finish(ca.HASHES["sha256"], ca.HASHES["sha256"].iv, 0, 0b10101, 5)
    assert rd(chip, HASH + H_HR8) == st[0]


RFC4231 = [  # (key, data, HMAC-SHA-224, HMAC-SHA-256): test cases 2 and 6 (131-byte key -> LKEY)
    (b"Jefe", b"what do ya want for nothing?",
     "a30e01098bc6dbbf45690f3a7e9e6d0f8bbea2a39e6148008fd05e44",
     "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"),
    (bytes([0xaa] * 131), b"Test Using Larger Than Block-Size Key - Hash Key First",
     "95e9a0db962095adaebe9b2d6f0dbce2d499f112f2d2b7273fa6870e",
     "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"),
]


@pytest.mark.parametrize("key,msg,m224,m256", RFC4231)
def test_hmac_sha2_rfc4231(chip, key, msg, m224, m256):
    for algo, ref in ((SHA224, m224), (SHA256, m256)):
        wr(chip, HASH + H_CR, algo | MODE | (LKEY if len(key) > 64 else 0) | 2 << 4 | INIT)
        for data in (key, msg, key):                                # RM0090 25.3.6 HMAC phases
            feed(chip, data)
            nb = (8 * len(data)) % 32
            wr(chip, HASH + H_STR, nb)
            wr(chip, HASH + H_STR, nb | DCAL)
            chip.run_cycles(50 * 8)
            assert not rd(chip, HASH + H_SR) & H_BUSY
        assert rd(chip, HASH + H_SR) & DCIS
        out = b"".join(rd(chip, HASH + H_HR8 + 4 * i).to_bytes(4, "big") for i in range(8))
        assert out[:len(ref) // 2].hex() == ref
        assert out[:len(ref) // 2] == hmac.new(key, msg, algo == SHA224 and hashlib.sha224 or hashlib.sha256).digest()


def test_sha256_context_swap_csr51_53(chip):
    """RM0090 25.3.7 / 25.4.8: CSR0..53 on the F43x; save mid message, hash something else, restore, finish."""
    rnd = random.Random(9)
    a = bytes(rnd.getrandbits(8) for _ in range(150))
    wr(chip, HASH + H_CR, SHA256 | 2 << 4 | INIT)
    feed(chip, a[:100])
    chip.run_cycles(200)
    saved = [rd(chip, HASH + H_CSR + 4 * i) for i in range(54)]
    assert hash_regs(chip, SHA224, b"xyz")[:28] == hashlib.sha224(b"xyz").digest()
    wr(chip, HASH + H_CR, SHA256 | 2 << 4 | INIT)
    for i, w in enumerate(saved):
        wr(chip, HASH + H_CSR + 4 * i, w)
    feed(chip, a[100:])
    wr(chip, HASH + H_STR, (8 * len(a)) % 32)
    wr(chip, HASH + H_STR, (8 * len(a)) % 32 | DCAL)
    chip.run_cycles(400)
    out = b"".join(rd(chip, HASH + H_HR8 + 4 * i).to_bytes(4, "big") for i in range(8))
    assert out == hashlib.sha256(a).digest()


def test_hash_irq_shared_with_rng(chip):
    wr(chip, HASH + H_IMR, 2)                                       # DCIE
    hash_regs(chip, SHA256, b"abc")
    assert asserted(chip, HASH_IRQ)
    wr(chip, HASH + H_SR, 0)
    assert not asserted(chip, HASH_IRQ)


# ================================================================================================ sample
def test_f42x_gcm_sample():
    """firmware/samples_stm32_variants/f42x_gcm.c on the STM32F437AI: GCM (test case 3 data + test case 4 AAD),
    SHA-256("abc") and HMAC-SHA256 (RFC 4231 case 2), results in RAM and printed on UART7."""
    if not SAMPLE.exists():
        pytest.skip("f42x_gcm.elf not built (firmware/samples_stm32_variants/build.sh)")
    s = Session(str(ROOT / "chips" / "stm32f437ai.yaml"), str(SAMPLE))
    s.run_for(25e6)                                                  # ~210 characters at 115200 baud: ~18 ms
    out = s.uart_output("UART7").decode()
    ct, tag = ca.gcm_encrypt(K3, IV3, P3, A4)
    assert "GCM ct ok" in out
    assert f"tag {tag.hex()}" in out
    assert f"sha256 {hashlib.sha256(b'abc').hexdigest()}" in out
    assert f"hmac {hmac.new(b'Jefe', b'what do ya want for nothing?', hashlib.sha256).hexdigest()}" in out
    assert "done" in out
    cr_ = s.chip.peripherals["CRYP"]
    assert [t[1] for t in cr_.trace][-7:] == ["header", "header", "payload", "payload", "payload", "payload", "final"]
