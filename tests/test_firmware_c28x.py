"""C28x image loaders: TI COFF2 (cl2000 --abi=coffabi) and TI C2000 ELF (cl2000 EABI, the v25 default), built here
byte by byte in the layout the TI linker produces (no compiler needed)."""
from __future__ import annotations

import struct

import pytest

from chipem.core.c28x.asm import assemble
from chipem.firmware import EM_TI_C2000, FirmwareError, detect_format, load_firmware

# ----------------------------------------------------------------------------------------------- program
# A cl2000-style layout: codestart (LB _c_int00) at 0x33FFF6, .text in flash, .econst in flash, .cinit records in
# flash, .ebss/.stack in RAM (uninitialised).  _c_int00 sets SP, calls _main with LCR; _main reads a constant and
# stores it to a global, then idles in a loop.
TEXT = 0x330000
ECONST = 0x338000
CINIT = 0x338100
EBSS = 0x8000
STACK = 0x0400

ASM = f"""
_g      .set    {EBSS}
        .sect "codestart", 0x33FFF6
        LB      _c_int00
        .sect ".text", {TEXT}
_c_int00:
        MOV     @SP,#{STACK}
        SPM     0
        C28OBJ
        C28ADDR
        CLRC    PAGE0
        MOVW    DP,#0
        CLRC    OVM
        LCR     #_main
_end:   SB      _end,UNC
_main:
        MOVL    XAR4,#_k
        MOV     AL,*XAR4
        MOVL    XAR5,#_g
        MOV     *XAR5,AL
        ADDB    SP,#2
        SUBB    SP,#2
        LRETR
        .sect ".econst", {ECONST}
_k:     .word   0x1234
"""


def _program():
    r = assemble(ASM)
    syms = dict(r.symbols)
    syms["_g"] = EBSS
    return r, syms


# ----------------------------------------------------------------------------------------------- COFF2 writer
STYP_TEXT, STYP_DATA, STYP_BSS, STYP_COPY = 0x20, 0x40, 0x80, 0x10


def make_coff2(sections, symbols, entry, *, opthdr=True, target=0x009D) -> bytes:
    """sections: [(name, paddr, words_or_size, flags, page)] (int size for no-data sections);
    symbols: [(name, value, scn, sclass, naux)].  Long names go to the string table, as the TI tools do."""
    strtab = b""

    def name8(nm: str) -> bytes:
        nonlocal strtab
        b = nm.encode()
        if len(b) <= 8:
            return b.ljust(8, b"\0")
        off = 4 + len(strtab)
        strtab += b + b"\0"
        return struct.pack("<II", 0, off)

    nsec = len(sections)
    hdr_end = 22 + (28 if opthdr else 0) + 48 * nsec
    raw = b""
    shdrs = b""
    for name, addr, body, flags, page in sections:
        if isinstance(body, int):
            size, ptr, data = body, 0, b""
        else:
            data = struct.pack(f"<{len(body)}H", *body)
            size, ptr = len(body), hdr_end + len(raw)
        shdrs += name8(name) + struct.pack("<iiiiiiIII", addr, addr, size, ptr, 0, 0, 0, 0, flags) \
            + struct.pack("<HH", 0, page)
        raw += data
    syms = b""
    nsyms = 0
    for nm, value, scn, sclass, naux in symbols:
        syms += name8(nm) + struct.pack("<ihHBB", value, scn, 0, sclass, naux)
        syms += b"\0" * 18 * naux
        nsyms += 1 + naux
    symptr = hdr_end + len(raw)
    fh = struct.pack("<HHiiiHHH", 0x00C2, nsec, 0x5F000000, symptr, nsyms, 28 if opthdr else 0, 0x0103 | 0x1000,
                     target)
    oh = struct.pack("<hhiiiiii", 0x0108, 0x0119, 0, 0, 0, entry, TEXT, ECONST) if opthdr else b""
    return fh + oh + shdrs + raw + syms + struct.pack("<I", 4 + len(strtab)) + strtab


def _words(r, addr):
    for a, d in r.segments:
        if a == addr:
            return list(struct.unpack(f"<{len(d) // 2}H", d))
    raise KeyError(hex(addr))


def coff_for_program(*, ram_model=False, opthdr=True, target=0x009D):
    r, syms = _program()
    cinit = [0xFFFF, EBSS + 1, 0, 0xBEEF,          # size -1 (stored negative), 32-bit address, data
             0xFFFE, EBSS + 4, 0, 0x1111, 0x2222,
             0]
    sections = [
        ("codestart", 0x33FFF6, _words(r, 0x33FFF6), STYP_TEXT, 0),
        (".text", TEXT, _words(r, TEXT), STYP_TEXT, 0),
        (".cinit", CINIT, cinit, STYP_DATA | (STYP_COPY if ram_model else 0), 0),
        (".econst", ECONST, _words(r, ECONST), STYP_DATA, 1),
        (".ebss", EBSS, 0x100, STYP_BSS, 1),
        (".stack", STACK, 0x300, STYP_BSS, 1),
        (".debug_info", 0, [0xAAAA] * 4, STYP_COPY, 0),                 # debug info: COPY, never loaded
        (".TI.ramfunc_long_name", 0x8100, 2, STYP_BSS, 0),               # long name via the string table
    ]
    symbols = [
        (".file", 0, -2, 103, 1),                                        # C_FILE + aux entry
        (".text", TEXT, 2, C_STAT, 1),                                   # section symbol + aux: skipped
        ("_c_int00", syms["_c_int00"], 2, C_EXT, 0),                     # exactly 8 characters, no NUL
        ("_main", syms["_main"], 2, C_EXT, 0),
        ("_k", syms["_k"], 4, C_EXT, 0),
        ("_g", EBSS, 5, C_EXT, 0),
        ("_a_rather_long_function_name", syms["_end"], 2, C_STAT, 0),
        ("__STACK_SIZE", 0x300, -1, C_EXT, 0),                           # absolute: skipped
        ("_undefined_ext", 0, 0, C_EXT, 0),                              # undefined: skipped
        ("wd_disable", TEXT + 3, 2, 6, 0),                               # C_LABEL (asm label): kept
        ("$C$L1", TEXT + 4, 2, 6, 0),                                    # compiler-generated label: skipped
    ]
    return make_coff2(sections, symbols, syms["_c_int00"] if opthdr else 0, opthdr=opthdr, target=target), syms


C_EXT, C_STAT = 2, 3


def test_coff2_cl2000_layout(tmp_path):
    data, syms = coff_for_program()
    p = tmp_path / "app.out"
    p.write_bytes(data)
    assert detect_format(p) == "coff"
    img = load_firmware(p)
    assert img.fmt == "coff" and img.unit_bytes == 2 and img.entry == syms["_c_int00"]
    addrs = sorted(a for a, _ in img.segments)
    assert 0x33FFF6 in addrs and TEXT in addrs and ECONST in addrs and CINIT in addrs
    assert not img.contains(EBSS) and not img.contains(STACK) and not img.contains(0)     # BSS / COPY skipped
    assert img.read(ECONST, 1) == b"\x34\x12"
    assert img.symbols["_c_int00"] == syms["_c_int00"] and img.symbols["_main"] == syms["_main"]
    assert img.symbols["_g"] == EBSS and img.symbols["_a_rather_long_function_name"] == syms["_end"]
    assert img.symbols["wd_disable"] == TEXT + 3
    for skipped in (".file", ".text", "__STACK_SIZE", "_undefined_ext", "$C$L1"):
        assert skipped not in img.symbols


def test_coff2_debug_section_sizes_are_bytes(tmp_path):
    """cl2000 25.11.1 gives non-allocated COPY sections (.debug_*, $build.attributes) their size in bytes, unlike
    target sections (words).  Reading size*2 bytes ran past the end of the file ("section .debug_info data runs past
    the end of the file" on every real --abi=coffabi image)."""
    data, syms = coff_for_program()
    nbytes = 0x700
    sections = [("codestart", 0x33FFF6, [0x0073, 0x0000], STYP_TEXT, 0),
                (".debug_info", 0, [0xAAAA] * (nbytes // 2), STYP_COPY, 0)]
    blob = bytearray(make_coff2(sections, [("_c_int00", 0x33FFF6, 1, C_EXT, 0)], 0x33FFF6))
    sh = 22 + 28 + 48                                    # second section header
    struct.pack_into("<i", blob, sh + 16, nbytes)       # s_size in bytes, as cl2000 writes it
    p = tmp_path / "dbg.out"
    p.write_bytes(bytes(blob))
    img = load_firmware(p)
    assert [a for a, _ in img.segments] == [0x33FFF6] and img.entry == 0x33FFF6


def test_coff2_entry_falls_back_to_c_int00_without_optional_header(tmp_path):
    data, syms = coff_for_program(opthdr=False)
    p = tmp_path / "app.out"
    p.write_bytes(data)
    assert load_firmware(p).entry == syms["_c_int00"]


def test_coff2_rejects_other_targets(tmp_path):
    data, _ = coff_for_program(target=0x0099)                 # C6000
    p = tmp_path / "c6x.out"
    p.write_bytes(data)
    with pytest.raises(FirmwareError, match="TMS320C2800"):
        load_firmware(p)


def test_coff2_ram_model_cinit_is_applied_at_load(tmp_path):
    data, _ = coff_for_program(ram_model=True)
    p = tmp_path / "app.out"
    p.write_bytes(data)
    img = load_firmware(p)
    assert not img.contains(CINIT)                            # the COPY table itself is not loaded
    assert img.read(EBSS + 1, 1) == b"\xEF\xBE" and img.read(EBSS + 4, 2) == b"\x11\x11\x22\x22"


def test_coff2_runs_on_the_f28232(tmp_path):
    """Reset → codestart → _c_int00 → LCR _main → store; symbols resolve the PC."""
    from chipem.session import Session
    data, syms = coff_for_program()
    p = tmp_path / "app.out"
    p.write_bytes(data)
    s = Session("tms320f28232", p, quiet=True)
    s.run_for(50e3)
    chip = s.ctxs["A"].chip if hasattr(s, "ctxs") else s.chip
    assert chip.bus.read(EBSS, 1) == 0x1234
    assert chip.core.pc in (syms["_end"],)


# ----------------------------------------------------------------------------------------------- ELF writer
def make_c2000_elf(segments, symbols, entry) -> bytes:
    """ELF32 LE, EM_TI_C2000: one PT_LOAD per segment (p_paddr/p_vaddr in words, p_filesz in bytes), a .symtab
    with STT_FUNC/STT_OBJECT symbols (st_value in words) and the string tables."""
    shstr = b"\0.text0\0.text1\0.text2\0.symtab\0.strtab\0.shstrtab\0"
    strtab = b"\0"
    symtab = b"\0" * 16
    for nm, val, typ, shndx in symbols:
        off = len(strtab)
        strtab += nm.encode() + b"\0"
        symtab += struct.pack("<IIIBBH", off, val, 0, (1 << 4) | typ, 0, shndx)
    ehsize, phsize, shsize = 52, 32, 40
    nph = len(segments)
    off = ehsize + phsize * nph
    blobs, phdrs = b"", b""
    seg_offs = []
    for addr, data in segments:
        seg_offs.append(off + len(blobs))
        phdrs += struct.pack("<IIIIIIII", 1, off + len(blobs), addr, addr, len(data), len(data), 5, 1)
        blobs += data
    body_end = off + len(blobs)
    symtab_off = body_end
    strtab_off = symtab_off + len(symtab)
    shstr_off = strtab_off + len(strtab)
    shoff = shstr_off + len(shstr)
    secs = [b"\0" * shsize]
    names = [1, 8, 15]
    for i, ((addr, data), so) in enumerate(zip(segments, seg_offs)):
        secs.append(struct.pack("<IIIIIIIIII", names[i], 1, 6, addr, so, len(data), 0, 0, 1, 0))
    nsec_text = len(segments)
    secs.append(struct.pack("<IIIIIIIIII", 22, 2, 0, 0, symtab_off, len(symtab), nsec_text + 2, 1, 4, 16))
    secs.append(struct.pack("<IIIIIIIIII", 30, 3, 0, 0, strtab_off, len(strtab), 0, 0, 1, 0))
    secs.append(struct.pack("<IIIIIIIIII", 38, 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0))
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\0" * 8
    eh = ident + struct.pack("<HHIIIIIHHHHHH", 2, EM_TI_C2000, 1, entry, ehsize, shoff, 0, ehsize, phsize, nph,
                             shsize, len(secs), len(secs) - 1)
    return eh + phdrs + blobs + symtab + strtab + shstr + b"".join(secs)


def test_c2000_elf_eabi(tmp_path):
    r, syms = _program()
    segs = [(a, d) for a, d in r.segments if a in (0x33FFF6, TEXT, ECONST)]
    elf_syms = [("_c_int00", syms["_c_int00"], 2, 2), ("main", syms["_main"], 2, 2), ("k", syms["_k"], 1, 3)]
    p = tmp_path / "app_eabi.out"
    p.write_bytes(make_c2000_elf(segs, elf_syms, syms["_c_int00"]))
    assert detect_format(p) == "elf"
    img = load_firmware(p)
    assert img.fmt == "elf" and img.unit_bytes == 2 and img.entry == syms["_c_int00"]
    assert img.read(ECONST, 1) == b"\x34\x12"                    # word address, byte-sized data
    assert img.symbols == {"_c_int00": syms["_c_int00"], "main": syms["_main"], "k": syms["_k"]}
    from chipem.symbols import Symbols
    assert Symbols.for_image(img).name(syms["_main"] + 1).startswith("main")
