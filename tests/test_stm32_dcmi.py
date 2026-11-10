"""STM32F407 DCMI (chipem/periph/stm32/dcmi.py) and the parallel camera part (chipem/parts/camera.py): presence
gating, hardware and embedded synchronisation, packing (8/10-bit), crop, FCRC, snapshot/continuous, FIFO overrun,
flags/IRQ, DMA, pin-mux checks, camera timing and image files, and the sample board end to end."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.board import Board
from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage
from chipem.parts import catalog, create
from chipem.parts.camera import read_pgm
from chipem.session import Session
from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
ELF = ROOT / "firmware/samples_stm32/build/dcmi_capture.elf"
BOARD = ROOT / "boards/stm32f407_dcmi_camera.yaml"
FLASH = 0x08000000
RCC, DCMI, DMA2 = 0x40023800, 0x50050000, 0x40026400
GPIO = {"A": 0x40020000, "B": 0x40020400, "C": 0x40020800, "E": 0x40021000}
CR, SR, RIS, IER, MIS, ICR, ESCR, ESUR, CWSTRT, CWSIZE, DR = range(0, 0x2C, 4)
CAPTURE, CM, CROP, JPEG, ESS, PCKPOL, HSPOL, VSPOL, ENABLE = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x4000
FRAME, OVR, ERR, VSYNC_F, LINE = 1, 2, 4, 8, 0x10
DCMI_IRQ = 78
PINS = {"pixclk": "PA6", "hsync": "PA4", "vsync": "PB7", "d0": "PC6", "d1": "PC7", "d2": "PC8", "d3": "PC9",
        "d4": "PE4", "d5": "PB6", "d6": "PE5", "d7": "PE6", "d8": "PC10", "d9": "PC12"}
_D = {}


def desc(name="stm32f407zg"):
    if name not in _D:
        _D[name] = load_chip(ROOT / "chips" / f"{name}.yaml")
    return _D[name]


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


def make_chip(name="stm32f407zg"):
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc(name), clock_hz=16e6)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def mux(chip, pin, af=13):
    port, bit = pin[1], int(pin[2:])
    g = GPIO[port]
    r = 0x20 + 4 * (bit >> 3)
    s = 4 * (bit & 7)
    wr(chip, g + r, (rd(chip, g + r) & ~(0xF << s)) | af << s)
    wr(chip, g, (rd(chip, g) & ~(3 << 2 * bit)) | 2 << 2 * bit)


def rig(bits=8, skip=(), **cam):
    """F407 + camera on a one-chip board, DCMI pins muxed (except `skip`), clocks on."""
    chip = make_chip()
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 0x17 | 1 << 22)      # GPIOA/B/C/E, DMA2
    wr(chip, RCC + 0x34, rd(chip, RCC + 0x34) | 1)                    # DCMIEN
    board = Board({"A": chip})
    params = dict(width=8, height=2, bits=bits, pixclk=1_000_000, hblank=4, vblank=2)
    params.update(cam)
    ports = {}
    for port, pin in PINS.items():
        if port.startswith("d") and int(port[1:]) >= bits:
            continue
        if pin not in skip:
            mux(chip, pin)
        board.add_net(port.upper(), [("A", pin)])
        ports[port] = port.upper()
    camp = board.add_part("CAM", "camera", params, ports)
    return chip, board, camp


def words_of(samples, per=4):
    out = []
    for i in range(0, len(samples), per):
        g = samples[i:i + per] + [0] * (per - len(samples[i:i + per]))
        out.append(sum(v << ((8 if per == 4 else 16) * k) for k, v in enumerate(g)))
    return out


def drain(chip):
    out = []
    while rd(chip, DCMI + SR) & 4:
        out.append(rd(chip, DCMI + DR))
    return out


HW = PCKPOL | VSPOL                     # VSYNC high / HSYNC low during blanking (camera defaults)


# ------------------------------------------------------------------------------------------------ presence
def test_absent_on_f405_present_on_f407():
    c5 = make_chip("stm32f405zg")
    d = c5.peripherals["DCMI"]
    assert d.absent and not d.regs
    wr(c5, DCMI + CWSTRT, 0x1234)                    # plain storage like the stub
    assert any("not present on the STM32F405ZG" in e.msg for e in c5.log.filter("warn"))
    assert rd(c5, DCMI + CWSTRT) == 0x1234
    c7 = make_chip()
    assert not c7.peripherals["DCMI"].absent
    assert rd(c7, DCMI + SR) == 0                     # reset value: sync status only while ENABLE = 1


def test_registers_and_icr():
    chip = make_chip()
    wr(chip, DCMI + ESCR, 0xB69D80AB)
    wr(chip, DCMI + CWSIZE, 0x3FFF3FFF)
    assert rd(chip, DCMI + ESCR) == 0xB69D80AB and rd(chip, DCMI + CWSIZE) == 0x3FFF3FFF
    wr(chip, DCMI + RIS, 0x1F)
    assert rd(chip, DCMI + RIS) == 0                  # read-only
    assert rd(chip, DCMI + ICR) == 0


# ------------------------------------------------------------------------------------------------ hardware sync
def test_snapshot_packs_8bit_words_and_clears_capture():
    chip, board, cam = rig(pattern="counter")
    wr(chip, DCMI + CR, HW | CM | ENABLE)
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    assert rd(chip, DCMI + RIS) & FRAME
    assert not rd(chip, DCMI + CR) & CAPTURE           # snapshot: one frame
    exp = words_of(cam.line_samples(0, 0) + cam.line_samples(1, 0))
    assert drain(chip) == exp                          # the frame that started after CAPTURE (frame 0)
    assert chip.peripherals["DCMI"].frames_captured == 1
    assert rd(chip, DCMI + RIS) & (VSYNC_F | LINE) == VSYNC_F | LINE


def test_snapshot_data_matches_frame():
    chip, board, cam = rig(pattern="gradient")
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    d = chip.peripherals["DCMI"]
    n = d.frames_captured
    assert n == 1
    assert drain(chip) == words_of(cam.line_samples(0) + cam.line_samples(1))


def test_word_timing_follows_pixel_clock():
    chip, board, cam = rig()
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    seen = []
    d = chip.peripherals["DCMI"]
    orig = d._push_word
    d._push_word = lambda w: (seen.append(chip.now_ns()), orig(w))
    board.run_for(200e3)
    # first frame starts after 2 vblank lines (2 x 12 us), line 0 after 4 hblank clocks; word 0 = samples 0..3
    t_frame = 2 * 12_000
    t0 = t_frame + 4 * 1000
    assert seen[0] == pytest.approx(t0 + 3.5 * 1000, abs=10)
    assert seen[1] == pytest.approx(t0 + 7.5 * 1000, abs=10)


def test_crop_window():
    chip, board, cam = rig(width=16, height=4, pattern="counter")
    wr(chip, DCMI + CWSTRT, (1 << 16) | 4)            # line 1, pixel clock 4
    wr(chip, DCMI + CWSIZE, (1 << 16) | 7)            # 2 lines x 8 clocks
    wr(chip, DCMI + CR, HW | CM | CROP | ENABLE | CAPTURE)
    board.run_for(400e3)
    f = cam.frames
    exp = []
    got = drain(chip)
    assert len(got) == 4
    # the captured frame is the first one that started after CAPTURE (frame 0)
    for y in (1, 2):
        exp += cam.line_samples(y, 0)[4:12]
    assert got == words_of(exp) and f >= 1


def test_continuous_fcrc_every_other_frame():
    chip, board, cam = rig()
    wr(chip, DCMI + CR, HW | (1 << 8) | ENABLE | CAPTURE)
    d = chip.peripherals["DCMI"]
    for _ in range(8):
        board.run_for(48e3)
        drain(chip)
    frames = cam.frames
    assert frames >= 7
    assert d.frames_captured == (frames + 1) // 2 or d.frames_captured == frames // 2


def test_continuous_stops_after_current_frame():
    chip, board, cam = rig(height=8)
    wr(chip, DCMI + CR, HW | ENABLE | CAPTURE)
    d = chip.peripherals["DCMI"]
    board.run_for(24e3 + 30e3)                        # inside frame 0
    assert d._capturing
    wr(chip, DCMI + CR, HW | ENABLE)                  # clear CAPTURE mid-frame
    assert rd(chip, DCMI + CR) & CAPTURE              # still capturing until the frame ends
    for _ in range(6):
        board.run_for(30e3)
        drain(chip)
    assert not rd(chip, DCMI + CR) & CAPTURE
    assert d.frames_captured == 1


def test_overrun_loses_words():
    chip, board, cam = rig(width=40, height=1, pattern="counter")
    wr(chip, DCMI + IER, OVR)
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    assert rd(chip, DCMI + RIS) & OVR and rd(chip, DCMI + MIS) == OVR
    assert (16 + DCMI_IRQ) in chip.arch.asserted
    got = drain(chip)
    assert got == words_of(cam.line_samples(0, 0))[:8]   # 10 words, FIFO of 8
    wr(chip, DCMI + ICR, OVR)
    assert not rd(chip, DCMI + RIS) & OVR and (16 + DCMI_IRQ) not in chip.arch.asserted


def test_frame_irq_and_sr_flags():
    chip, board, cam = rig()
    wr(chip, DCMI + IER, FRAME)
    wr(chip, DCMI + CR, HW | CM | ENABLE)
    assert rd(chip, DCMI + SR) & 3 == 3               # VSYNC high: blanking; HSYNC low: blanking
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    assert (16 + DCMI_IRQ) in chip.arch.asserted
    wr(chip, DCMI + ICR, FRAME)
    assert (16 + DCMI_IRQ) not in chip.arch.asserted


def test_hspol_vspol_follow_camera_levels():
    chip, board, cam = rig(hsync_blank="H", vsync_blank="L", pattern="counter")
    wr(chip, DCMI + CR, PCKPOL | HSPOL | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    assert drain(chip) == words_of(cam.line_samples(0) + cam.line_samples(1))
    # wrong polarity: data are taken during blanking only, nothing captured
    chip, board, cam = rig(hsync_blank="H", vsync_blank="L")
    wr(chip, DCMI + CR, PCKPOL | VSPOL | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    assert drain(chip) == []


def test_edm_10bit_two_samples_per_word():
    chip, board, cam = rig(bits=10, pattern="gradient")
    wr(chip, DCMI + CR, HW | (1 << 10) | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    s = cam.line_samples(0) + cam.line_samples(1)
    assert max(s) > 255
    assert drain(chip)[:8] == words_of(s, per=2)[:8]


def test_unmuxed_data_pin_reads_zero_and_warns():
    chip, board, cam = rig(skip=("PC9",), pattern="checker", width=8, height=2)
    cam.set(pattern="counter")
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    exp = [v & ~8 for v in cam.line_samples(0) + cam.line_samples(1)]
    assert drain(chip) == words_of(exp)
    assert any("DCMI_D3 not muxed" in e.msg for e in chip.log.filter("warn"))


def test_nothing_without_pixclk_mux():
    chip, board, cam = rig(skip=("PA6",))
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    board.run_for(200e3)
    assert drain(chip) == [] and chip.peripherals["DCMI"].frames_captured == 1   # syncs seen, no data


def test_dma_stream1_channel1():
    chip, board, cam = rig(width=32, height=4, pattern="counter")
    s1 = DMA2 + 0x10 + 0x18
    buf = 0x20001000
    wr(chip, s1 + 8, DCMI + DR)                       # PAR
    wr(chip, s1 + 0xC, buf)                           # M0AR
    wr(chip, s1 + 4, 32)                              # NDTR: 128 bytes
    wr(chip, s1, (1 << 25) | (1 << 10) | (2 << 11) | (2 << 13) | 1)
    wr(chip, DCMI + CR, HW | CM | ENABLE | CAPTURE)
    board.run_for(500e3)
    data = bytes(chip.bus.read(buf + i, 1) for i in range(128))
    exp = bytes(sum((cam.line_samples(y, 0) for y in range(4)), []))
    assert data == exp
    assert not rd(chip, DCMI + RIS) & OVR
    assert rd(chip, DMA2) & (1 << 11)                 # LISR TCIF1


# ------------------------------------------------------------------------------------------------ embedded sync
def _ess(chip, escr=0xB69D80AB, esur=0xFFFFFFFF):
    wr(chip, DCMI + ESCR, escr)
    wr(chip, DCMI + ESUR, esur)


def test_embedded_sync_snapshot():
    chip, board, cam = rig(sync="embedded", pattern="bars", height=3)
    _ess(chip)
    wr(chip, DCMI + IER, ERR)
    wr(chip, DCMI + CR, PCKPOL | ESS | CM | ENABLE | CAPTURE)
    board.run_for(300e3)
    assert rd(chip, DCMI + RIS) & FRAME and not rd(chip, DCMI + RIS) & ERR
    s = sum((cam.line_samples(y) for y in range(3)), [])
    assert all(1 <= v <= 254 for v in s)
    assert drain(chip) == words_of(s)


def test_embedded_unknown_code_sets_err():
    chip, board, cam = rig(sync="embedded", height=3)
    _ess(chip, escr=0xB69D81AB)                       # LSC 0x81: the camera's 0x80 line starts are unknown codes
    wr(chip, DCMI + IER, ERR)
    wr(chip, DCMI + CR, PCKPOL | ESS | CM | ENABLE | CAPTURE)
    board.run_for(300e3)
    assert rd(chip, DCMI + RIS) & ERR and (16 + DCMI_IRQ) in chip.arch.asserted


def test_embedded_unmask_matches_partial_code():
    chip, board, cam = rig(sync="embedded", height=3)
    _ess(chip, escr=0xB69D81AB, esur=0xFFFFFEFF)      # LSU ignores bit 0: 0x81 matches 0x80
    wr(chip, DCMI + CR, PCKPOL | ESS | CM | ENABLE | CAPTURE)
    board.run_for(300e3)
    assert not rd(chip, DCMI + RIS) & ERR and rd(chip, DCMI + RIS) & FRAME


# ------------------------------------------------------------------------------------------------ camera part
def test_camera_catalog_and_ports():
    cat = {c["type"]: c for c in catalog()}
    assert "camera" in cat
    assert list(cat["camera"]["ports"])[:3] == ["pixclk", "hsync", "vsync"]
    cam = create("camera", "C1", {"bits": 12})
    assert "d11" in cam.ports and "d12" not in cam.ports
    with pytest.raises(ValueError):
        create("camera", "C2", {"bits": 16})
    with pytest.raises(ValueError):
        create("camera", "C3", {"format": "rgb565", "bits": 10})


def test_camera_sync_edge_timing():
    chip, board, cam = rig()
    ev = []
    chip.pins.subscribe(lambda e: ev.append((chip.now_ns(), e.pin, e.new)) if e.field == "level" else None)
    board.run_for(60e3)
    vs = [(t, v) for t, p, v in ev if p == "PB7"]
    hs = [(t, v) for t, p, v in ev if p == "PA4"]
    assert vs[0] == (pytest.approx(24_000), "L")           # frame start after 2 vblank lines of 12 us
    assert vs[1] == (pytest.approx(48_500), "H")           # half a clock after the last line
    assert hs[0] == (pytest.approx(28_000), "H") and hs[1] == (pytest.approx(36_000), "L")
    assert cam.state()["frames"] >= 1


def test_camera_pixclk_wave_summary():
    chip, board, cam = rig(width=64, height=64, pixclk=1_000_000)   # ~4.4 ms frames
    board.run_for(120e6)
    w = [e for e in board.log.filter("pin") if e.fields.get("field") == "wave" and e.fields.get("pin") == "PA6"]
    assert w and w[0].freq_hz == 1e6 and w[0].edges > 100_000


def test_pgm_image(tmp_path):
    hdr = b"P5\n# test\n4 2\n255\n"
    px = bytes(range(10, 18))
    f = tmp_path / "img.pgm"
    f.write_bytes(hdr + px)
    assert read_pgm(hdr + px) == (4, 2, 255, list(px))
    cam = create("camera", "C", {"image": str(f)})
    assert (cam.width, cam.height) == (4, 2)
    assert cam.line_samples(1) == list(px[4:])
    cam10 = create("camera", "C", {"image": str(f), "bits": 10})
    assert cam10.line_samples(0)[0] == 10 << 2
    raw = tmp_path / "img.raw"
    raw.write_bytes(bytes(range(16)))
    camr = create("camera", "R", {"image": str(raw), "width": 4, "height": 2, "format": "rgb565"})
    assert camr.line_samples(1) == list(range(8, 16))


def test_rgb565_bars_high_byte_first():
    cam = create("camera", "C", {"format": "rgb565", "width": 8})
    s = cam.line_samples(0)
    assert len(s) == 16 and s[:2] == [0xFF, 0xFF] and s[-2:] == [0, 0]


# ------------------------------------------------------------------------------------------------ sample board
@pytest.mark.skipif(not ELF.exists(), reason="firmware/samples_stm32 not built")
def test_sample_board_end_to_end():
    s = Session.from_board(BOARD, quiet=True)
    s.run_for(50e6)
    out = s.uart_output("A.USART1").decode()
    cam = s.board.parts["CAM"]
    full = sum(sum(cam.line_samples(y)) for y in range(60))
    crop = sum(sum(cam.line_samples(y)[20:60]) for y in range(15, 45))
    assert f"DCMI frame 80x60 sum={full:08X}" in out
    assert f"DCMI crop 40x30 sum={crop:08X}" in out
    assert "DCMI continuous frames=3 ovr=0" in out and "done" in out
