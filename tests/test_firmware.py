import struct
import time
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip
from chipem.firmware import (FirmwareError, FirmwareImage, analyze, format_report, guess_load_addr, load_firmware,
                             parse_ihex, parse_srec)

ROOT = Path(__file__).resolve().parent.parent
WITHINGS = ROOT / "s1_off000084_503080.bin"
SAMPLES = ROOT / "firmware" / "samples" / "build"


@pytest.fixture(scope="module")
def fn():
    return load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml")


@pytest.fixture(scope="module")
def fx():
    return load_chip(ROOT / "chips" / "mk22fx512vll12.yaml")


# --- writers used to round-trip the parsers ------------------------------------------------------------------------
def to_ihex(segs, entry=None, rec=16):
    out = []

    def line(typ, addr, data):
        b = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, typ]) + data
        out.append(":" + (b + bytes([(-sum(b)) & 0xFF])).hex().upper())

    for a, d in segs:
        upper = None
        for i in range(0, len(d), rec):
            addr = a + i
            if addr >> 16 != upper:
                upper = addr >> 16
                line(4, 0, upper.to_bytes(2, "big"))
            line(0, addr & 0xFFFF, d[i:i + rec])
    if entry is not None:
        line(5, 0, entry.to_bytes(4, "big"))
    line(1, 0, b"")
    return "\n".join(out) + "\n"


def to_srec(segs, entry=0, kind=3):
    al = {1: 2, 2: 3, 3: 4}[kind]
    out = []

    def rec(t, addr, data, alen):
        b = bytes([alen + len(data) + 1]) + addr.to_bytes(alen, "big") + data
        out.append(f"S{t}" + (b + bytes([0xFF - (sum(b) & 0xFF)])).hex().upper())

    rec(0, 0, b"test", 2)
    for a, d in segs:
        for i in range(0, len(d), 16):
            rec(kind, a + i, d[i:i + 16], al)
    rec(10 - kind, entry, b"", al)
    return "\n".join(out) + "\n"


SEGS = [(0x0, bytes(range(256)) * 3), (0x1FFF0000, b"\x11\x22\x33\x44\x55"), (0x10010, b"abc")]


def test_bin(tmp_path):
    p = tmp_path / "fw.bin"
    p.write_bytes(b"\x01\x02\x03\x04" * 4)
    img = load_firmware(p)
    assert img.segments == [(0, b"\x01\x02\x03\x04" * 4)] and img.fmt == "bin" and img.entry is None
    img = load_firmware(p, load_addr=0x4000)
    assert img.start == 0x4000 and img.read_word(0x4004) == 0x04030201 and img.read(0x4010, 1) is None
    assert img.rebased(0x8000).start == 0x8000


def test_ihex_roundtrip(tmp_path):
    p = tmp_path / "fw.hex"
    p.write_text(to_ihex(SEGS, entry=0x1A5))
    img = load_firmware(p)
    assert img.fmt == "hex" and img.entry == 0x1A5
    assert img.segments == sorted(SEGS)
    bad = to_ihex(SEGS).replace(":10000000000102", ":10000000000103", 1)
    with pytest.raises(FirmwareError):
        parse_ihex(bad)


@pytest.mark.parametrize("kind", [1, 2, 3])
def test_srec_roundtrip(tmp_path, kind):
    segs = [(a, d) for a, d in SEGS if a < (1 << (8 * {1: 2, 2: 3, 3: 4}[kind]))]
    p = tmp_path / "fw.srec"
    p.write_text(to_srec(segs, entry=0x101, kind=kind))
    img = load_firmware(p)
    assert img.fmt == "srec" and img.entry == 0x101 and img.segments == sorted(segs)
    with pytest.raises(FirmwareError):
        parse_srec(to_srec(segs, kind=kind).replace("S0", "S4", 1))


def test_format_sniffing(tmp_path):
    p = tmp_path / "fw.dat"
    p.write_text(to_ihex([(0x100, b"xyz")]))
    assert load_firmware(p).segments == [(0x100, b"xyz")]
    assert load_firmware(p, fmt="bin").segments[0][0] == 0


def test_segment_merge():
    img = FirmwareImage([(4, b"BB"), (0, b"AAAA"), (6, b"C")])
    assert img.segments == [(0, b"AAAABBC")] and img.size == 7 and img.end == 7


@pytest.mark.skipif(not (SAMPLES / "port_irq.elf").exists(), reason="samples not built (firmware/samples/build.sh)")
def test_elf_sample(fn):
    elf = load_firmware(SAMPLES / "port_irq.elf")
    binimg = load_firmware(SAMPLES / "port_irq.bin")
    assert elf.fmt == "elf" and elf.entry & 1
    assert elf.read_word(0) == 0x20010000
    flat = bytes(binimg.segments[0][1])
    for a, d in elf.segments:          # every ELF load segment (incl. .data LMA) matches the flat image
        assert flat[a:a + len(d)] == d
    assert guess_load_addr(binimg, fn) == 0
    r = analyze(elf, fn)
    assert r.bootable and r.initial_sp == 0x20010000 and r.vtor == 0
    assert [name for _, name, _ in r.handlers] == ["PORTC"]
    assert "PTC5" in r.pins_configured and "PTC6" in r.pins_configured
    assert any("IRQC=10" in n and "pull-up" in n for n in r.pins_configured["PTC6"])
    assert {"PORTC", "GPIOC", "SIM", "WDOG"} <= set(r.periph_refs)
    assert "PORTC" in format_report(r)


def test_not_bootable(fn):
    vec = struct.pack("<4I", 0x30000000, 0x100, 0, 0)       # SP outside RAM, PC even
    r = analyze(FirmwareImage([(0, vec + b"\0" * 0x200)]), fn, disasm=False)
    assert not r.bootable and len(r.boot_problems) == 2
    assert "NOT bootable" in format_report(r)


@pytest.mark.skipif(not WITHINGS.exists(), reason="example image not present")
def test_withings_image(fx):
    img = load_firmware(WITHINGS)
    assert guess_load_addr(img, fx) == 0x4000
    img = load_firmware(WITHINGS, load_addr=0x4000)
    t = time.perf_counter()
    r = analyze(img, fx)
    assert time.perf_counter() - t < 5.0
    assert r.vtor == 0x4000 and r.initial_sp == 0x20010000 and r.reset_pc == 0x19A4D and r.bootable
    names = {name: addr for _, name, addr in r.handlers}
    assert {"SVCall", "PendSV", "SysTick", "PORTA", "PORTE", "UART0_RX_TX", "LPTMR0"} <= set(names)
    assert names["SysTick"] == 0x4D121 and r.default_handler == 0x19B01
    for p in ("SIM", "PORTA", "GPIOA", "WDOG", "MCG", "VBAT", "SCS", "RTC", "ADC0", "LLWU"):
        assert p in r.periph_refs, p
    assert r.memory_refs.get("FlexNVM", 0) > 0
    text = format_report(r)
    assert "Vector table @ 0x00004000" in text and "PendSV" in text


@pytest.mark.skipif(not WITHINGS.exists(), reason="example image not present")
def test_cli_info(capsys):
    from chipem.cli import main
    assert main(["info", str(WITHINGS), "--chip", "mk22fx512vll12"]) == 0
    out = capsys.readouterr()
    assert "Vector table @ 0x00004000" in out.out and "auto-detected load address 0x4000" in out.err
    assert main(["info", str(WITHINGS), "--chip", "nosuchchip"]) == 1
