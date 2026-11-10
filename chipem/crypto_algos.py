"""Pure-Python block ciphers and hash compression functions (vendor-free; no dependencies beyond the stdlib).

Used by the STM32 CRYP / HASH models (chipem/periph/stm32/cryp.py, hash.py), which need what library APIs do not
expose: AES decryption from the *last* round keys (the CRYP "key preparation" result), and hash compression with an
exportable intermediate state (the HASH context swap registers).  Speed is irrelevant here (a few blocks per
emulated operation); correctness is checked against FIPS-197, SP 800-38A, FIPS 46-3 / SP 800-67 and hashlib in
tests/test_stm32_cryp_hash.py.

AES (FIPS-197):  aes_expand(key) -> round-key words; aes_encrypt(key, block), aes_decrypt(key, block);
                 aes_last_words(key) -> the last Nk schedule words; aes_decrypt_from_last(last, block, nk)
DES (FIPS 46-3): des_encrypt(key8, block8), des_decrypt(key8, block8); tdes_encrypt / tdes_decrypt (EDE, K1|K2|K3)
Hashes:          MD5 / SHA-1 / SHA-224 / SHA-256: `HASHES[name]` -> HashAlg(iv, compress(state, block64) -> state,
                 digest_words, length_little); `digest(name, data)`, `pad(name, bitlen)` helpers.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable

M32 = 0xFFFFFFFF


# ================================================================================================ AES
def _xt(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _build_sbox() -> tuple[list[int], list[int]]:
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)
        p &= 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF) ^ ((q << 3 | q >> 5) & 0xFF) \
            ^ ((q << 4 | q >> 4) & 0xFF)
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


SBOX, INV_SBOX = _build_sbox()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


_M2 = [_mul(i, 2) for i in range(256)]
_M3 = [_mul(i, 3) for i in range(256)]
_M9 = [_mul(i, 9) for i in range(256)]
_M11 = [_mul(i, 11) for i in range(256)]
_M13 = [_mul(i, 13) for i in range(256)]
_M14 = [_mul(i, 14) for i in range(256)]


def _sub_word(w: int) -> int:
    return (SBOX[w >> 24] << 24) | (SBOX[w >> 16 & 0xFF] << 16) | (SBOX[w >> 8 & 0xFF] << 8) | SBOX[w & 0xFF]


def _rot_word(w: int) -> int:
    return ((w << 8) | (w >> 24)) & M32


def _rounds(nk: int) -> int:
    return {4: 10, 6: 12, 8: 14}[nk]


def aes_expand(key: bytes) -> list[int]:
    """FIPS-197 key expansion: 4 * (Nr + 1) big-endian 32-bit words."""
    nk = len(key) // 4
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16, 24 or 32 bytes")
    w = list(struct.unpack(f">{nk}I", key))
    total = 4 * (_rounds(nk) + 1)
    for i in range(nk, total):
        t = w[i - 1]
        if i % nk == 0:
            t = _sub_word(_rot_word(t)) ^ (_RCON[i // nk - 1] << 24)
        elif nk > 6 and i % nk == 4:
            t = _sub_word(t)
        w.append(w[i - nk] ^ t)
    return w


def aes_last_words(key: bytes) -> list[int]:
    """The last Nk words of the key schedule (what the CRYP key preparation leaves in its key registers)."""
    nk = len(key) // 4
    return aes_expand(key)[-nk:]


def aes_expand_from_last(last: list[int]) -> list[int]:
    """Run the key expansion backwards from its last Nk words: the full schedule (inverse of aes_last_words)."""
    nk = len(last)
    total = 4 * (_rounds(nk) + 1)
    w: list[int | None] = [None] * total
    w[total - nk:] = list(last)
    for i in range(total - 1, nk - 1, -1):
        t = w[i - 1]
        if i % nk == 0:
            t = _sub_word(_rot_word(t)) ^ (_RCON[i // nk - 1] << 24)
        elif nk > 6 and i % nk == 4:
            t = _sub_word(t)
        w[i - nk] = w[i] ^ t
    return w  # type: ignore[return-value]


def _state(block: bytes) -> list[int]:
    return list(block)


def _add_rk(s: list[int], w: list[int], r: int) -> None:
    for c in range(4):
        k = w[4 * r + c]
        s[4 * c] ^= k >> 24
        s[4 * c + 1] ^= k >> 16 & 0xFF
        s[4 * c + 2] ^= k >> 8 & 0xFF
        s[4 * c + 3] ^= k & 0xFF


def _shift_rows(s: list[int]) -> list[int]:
    return [s[(4 * ((c + r) % 4)) + r] for c in range(4) for r in range(4)]


def _inv_shift_rows(s: list[int]) -> list[int]:
    return [s[(4 * ((c - r) % 4)) + r] for c in range(4) for r in range(4)]


def _mix(s: list[int]) -> list[int]:
    o = []
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        o += [_M2[a0] ^ _M3[a1] ^ a2 ^ a3, a0 ^ _M2[a1] ^ _M3[a2] ^ a3, a0 ^ a1 ^ _M2[a2] ^ _M3[a3],
              _M3[a0] ^ a1 ^ a2 ^ _M2[a3]]
    return o


def _inv_mix(s: list[int]) -> list[int]:
    o = []
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        o += [_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3], _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
              _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3], _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]]
    return o


def aes_encrypt_w(w: list[int], block: bytes) -> bytes:
    nr = len(w) // 4 - 1
    s = _state(block)
    _add_rk(s, w, 0)
    for r in range(1, nr + 1):
        s = _shift_rows([SBOX[x] for x in s])
        if r != nr:
            s = _mix(s)
        _add_rk(s, w, r)
    return bytes(s)


def aes_decrypt_w(w: list[int], block: bytes) -> bytes:
    nr = len(w) // 4 - 1
    s = _state(block)
    _add_rk(s, w, nr)
    for r in range(nr - 1, -1, -1):
        s = [INV_SBOX[x] for x in _inv_shift_rows(s)]
        _add_rk(s, w, r)
        if r:
            s = _inv_mix(s)
    return bytes(s)


def aes_encrypt(key: bytes, block: bytes) -> bytes:
    return aes_encrypt_w(aes_expand(key), block)


def aes_decrypt(key: bytes, block: bytes) -> bytes:
    return aes_decrypt_w(aes_expand(key), block)


def aes_decrypt_from_last(last: list[int], block: bytes) -> bytes:
    """Decrypt with a schedule given by its last Nk words (the CRYP's prepared decryption key)."""
    return aes_decrypt_w(aes_expand_from_last(last), block)


# ================================================================================================ DES
_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
       64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
       37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
      16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
      2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25]
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3,
        60, 52, 44, 36, 63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
        29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_S = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _perm(v: int, table: list[int], nin: int) -> int:
    o = 0
    for p in table:
        o = (o << 1) | ((v >> (nin - p)) & 1)
    return o


def _des_subkeys(key: bytes) -> list[int]:
    k = _perm(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k >> 28, k & 0xFFFFFFF
    out = []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0xFFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0xFFFFFFF
        out.append(_perm((c << 28) | d, _PC2, 56))
    return out


def _f(r: int, k: int) -> int:
    x = _perm(r, _E, 32) ^ k
    o = 0
    for i in range(8):
        six = (x >> (42 - 6 * i)) & 0x3F
        row = ((six >> 4) & 2) | (six & 1)
        o = (o << 4) | _S[i][row * 16 + ((six >> 1) & 0xF)]
    return _perm(o, _P, 32)


def _des(key: bytes, block: bytes, decrypt: bool) -> bytes:
    ks = _des_subkeys(key)
    if decrypt:
        ks = ks[::-1]
    v = _perm(int.from_bytes(block, "big"), _IP, 64)
    left, right = v >> 32, v & M32
    for k in ks:
        left, right = right, left ^ _f(right, k)
    return _perm((right << 32) | left, _FP, 64).to_bytes(8, "big")


def des_encrypt(key: bytes, block: bytes) -> bytes:
    return _des(key, block, False)


def des_decrypt(key: bytes, block: bytes) -> bytes:
    return _des(key, block, True)


def tdes_encrypt(key: bytes, block: bytes) -> bytes:
    """TDES EDE (SP 800-67): E_K3(D_K2(E_K1(P))), key = K1 | K2 | K3 (24 bytes)."""
    return des_encrypt(key[16:24], des_decrypt(key[8:16], des_encrypt(key[0:8], block)))


def tdes_decrypt(key: bytes, block: bytes) -> bytes:
    return des_decrypt(key[0:8], des_encrypt(key[8:16], des_decrypt(key[16:24], block)))


# ================================================================================================ hashes
def _rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & M32


def _ror(x: int, n: int) -> int:
    return ((x >> n) | (x << (32 - n))) & M32


_MD5_S = [7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4
_MD5_K = [int(abs(__import__("math").sin(i + 1)) * 2 ** 32) & M32 for i in range(64)]


def md5_compress(st: tuple, block: bytes) -> tuple:
    m = struct.unpack("<16I", block)
    a, b, c, d = st
    for i in range(64):
        if i < 16:
            f, g = (b & c) | (~b & d), i
        elif i < 32:
            f, g = (d & b) | (~d & c), (5 * i + 1) % 16
        elif i < 48:
            f, g = b ^ c ^ d, (3 * i + 5) % 16
        else:
            f, g = c ^ (b | (~d & M32)), (7 * i) % 16
        f = (f + a + _MD5_K[i] + m[g]) & M32
        a, d, c = d, c, b
        b = (b + _rol(f, _MD5_S[i])) & M32
    return tuple((x + y) & M32 for x, y in zip(st, (a, b, c, d)))


def sha1_compress(st: tuple, block: bytes) -> tuple:
    w = list(struct.unpack(">16I", block))
    for i in range(16, 80):
        w.append(_rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1))
    a, b, c, d, e = st
    for i in range(80):
        if i < 20:
            f, k = (b & c) | (~b & d), 0x5A827999
        elif i < 40:
            f, k = b ^ c ^ d, 0x6ED9EBA1
        elif i < 60:
            f, k = (b & c) | (b & d) | (c & d), 0x8F1BBCDC
        else:
            f, k = b ^ c ^ d, 0xCA62C1D6
        t = (_rol(a, 5) + (f & M32) + e + k + w[i]) & M32
        a, b, c, d, e = t, a, _rol(b, 30), c, d
    return tuple((x + y) & M32 for x, y in zip(st, (a, b, c, d, e)))


_K256 = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]


def sha256_compress(st: tuple, block: bytes) -> tuple:
    w = list(struct.unpack(">16I", block))
    for i in range(16, 64):
        s0 = _ror(w[i - 15], 7) ^ _ror(w[i - 15], 18) ^ (w[i - 15] >> 3)
        s1 = _ror(w[i - 2], 17) ^ _ror(w[i - 2], 19) ^ (w[i - 2] >> 10)
        w.append((w[i - 16] + s0 + w[i - 7] + s1) & M32)
    a, b, c, d, e, f, g, h = st
    for i in range(64):
        t1 = (h + (_ror(e, 6) ^ _ror(e, 11) ^ _ror(e, 25)) + ((e & f) ^ (~e & g)) + _K256[i] + w[i]) & M32
        t2 = ((_ror(a, 2) ^ _ror(a, 13) ^ _ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) & M32
        a, b, c, d, e, f, g, h = (t1 + t2) & M32, a, b, c, (d + t1) & M32, e, f, g
    return tuple((x + y) & M32 for x, y in zip(st, (a, b, c, d, e, f, g, h)))


@dataclass(frozen=True)
class HashAlg:
    name: str
    iv: tuple
    compress: Callable[[tuple, bytes], tuple]
    digest_words: int               # state words that form the digest
    little: bool                    # MD5: little-endian words and length

    @property
    def digest_size(self) -> int:
        return 4 * self.digest_words

    def state_bytes(self, st: tuple) -> bytes:
        fmt = "<" if self.little else ">"
        return struct.pack(f"{fmt}{self.digest_words}I", *st[:self.digest_words])


HASHES = {
    "md5": HashAlg("md5", (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476), md5_compress, 4, True),
    "sha1": HashAlg("sha1", (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0), sha1_compress, 5, False),
    "sha224": HashAlg("sha224", (0xc1059ed8, 0x367cd507, 0x3070dd17, 0xf70e5939, 0xffc00b31, 0x68581511,
                                 0x64f98fa7, 0xbefa4fa4), sha256_compress, 7, False),
    "sha256": HashAlg("sha256", (0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
                                 0x1f83d9ab, 0x5be0cd19), sha256_compress, 8, False),
}


def finish(alg: HashAlg, st: tuple, done_bits: int, tail: int, tail_bits: int) -> tuple[tuple, int]:
    """Pad and compress the unprocessed message tail (`tail_bits` bits, MSB first in the integer `tail`, fewer than
    512 + 32) after `done_bits` already-compressed bits.  Returns (final state, blocks compressed)."""
    total = done_bits + tail_bits
    v = (tail << 1) | 1                                  # append the '1' bit
    n = tail_bits + 1
    pad = (448 - n) % 512
    v <<= pad
    n += pad
    body = v.to_bytes(n // 8, "big")
    body += total.to_bytes(8, "little" if alg.little else "big")
    k = 0
    for i in range(0, len(body), 64):
        st = alg.compress(st, body[i:i + 64])
        k += 1
    return st, k


def digest(name: str, data: bytes) -> bytes:
    """Whole-message digest through the compression functions (reference for the HASH model)."""
    alg = HASHES[name]
    st = alg.iv
    nfull = len(data) // 64
    for i in range(nfull):
        st = alg.compress(st, data[64 * i:64 * i + 64])
    rest = data[64 * nfull:]
    st, _ = finish(alg, st, 512 * nfull, int.from_bytes(rest, "big") if rest else 0, 8 * len(rest))
    return alg.state_bytes(st)
