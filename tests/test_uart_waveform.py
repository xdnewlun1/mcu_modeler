"""UART TX pins carry the real bit-level waveform (uartwire.frame_edges -> PinBoard.drive_wave): the VCD of every
family's UART decodes back to the transmitted bytes at the transmitter's baud, a GPIO input read of the TX pin
mid-frame returns the bit on the line, an unmuxed TX gives no waveform, and a timer input capture on another chip
measures the bit time through a board net."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from chipem.periph.uartwire import Frame, frame_edges
from chipem.pins import Pin, PinBoard

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- helpers
def vcd_changes(path: Path, pin_id: str) -> list[tuple[int, str]]:
    """(t_ns, '0'|'1'|'x') value changes of wire `pin_id` (initial value at the $dumpvars time first)."""
    text = path.read_text()
    m = re.search(r"\$var wire 1 (\S+) " + re.escape(pin_id) + r" \$end", text)
    assert m, f"{pin_id} not in VCD"
    vid = m.group(1)
    out, t = [], 0
    for line in text.split("$enddefinitions $end", 1)[1].splitlines():
        if line.startswith("#"):
            t = int(line[1:])
        elif len(line) >= 2 and line[0] in "01xz" and line[1:] == vid:
            out.append((t, line[0]))
    return out


def decode_8n1(changes: list[tuple[int, str]], baud: float, nbits: int = 8) -> bytes:
    """Asynchronous decode of a TX line (idle high) from its value changes, sampling mid-bit."""
    tb = 1e9 / baud
    ts = [t for t, _ in changes]
    vs = [v for _, v in changes]

    def lvl(t: float) -> str:
        import bisect
        i = bisect.bisect_right(ts, t) - 1
        return vs[i] if i >= 0 else "1"
    out = bytearray()
    i = 0
    pos = -1.0
    while i < len(changes):
        t, v = changes[i]
        i += 1
        if v != "0" or t < pos or lvl(t - 1) != "1":
            continue
        if lvl(t + 0.5 * tb) != "0":
            continue
        b = 0
        for k in range(nbits):
            if lvl(t + (1.5 + k) * tb) == "1":
                b |= 1 << k
        assert lvl(t + (1.5 + nbits) * tb) == "1", f"framing error at {t} ns"
        out.append(b)
        pos = t + (1 + nbits + 0.5) * tb
    return bytes(out)


def tx_pin(chip, blk):
    pb = chip.pins
    pins = [p for p in pb.pins if pb.signal_matches(p.owner, blk.tx_signal)]
    assert pins, f"no pin carries {blk.tx_signal}"
    return pins[0]


# ---------------------------------------------------------------- unit: frame -> edges, PinBoard waves
def test_frame_edges_lsb_first_with_parity_and_inversion():
    e = frame_edges(Frame(0x55, 8, "N", 1.0, 1e6), 0.0)
    assert [lv for _, lv in e] == ["L", "H", "L", "H", "L", "H", "L", "H", "L", "H"]
    assert [t for t, _ in e] == [i * 1000.0 for i in range(10)]
    e = frame_edges(Frame(0x01, 8, "E", 2.0, 1e6), 0.0)      # 0x01 + even parity 1
    assert e == [(0.0, "L"), (1000.0, "H"), (2000.0, "L"), (9000.0, "H")]
    e = frame_edges(Frame(0x00, 8, "N", 1.0, 1e6, inv=True), 0.0)
    assert e == [(0.0, "H"), (9000.0, "L")]
    e = frame_edges(Frame.brk_frame(13, 1.0, 1e6), 0.0)
    assert e == [(0.0, "L"), (13000.0, "H")]
    e = frame_edges(Frame(0x01, 8, "N", 1.0, 1e6, msbf=True), 0.0)
    assert e == [(0.0, "L"), (8000.0, "H")]
    assert frame_edges(Frame(0x55, baud=0.0), 0.0) == []


def _board():
    pb = PinBoard([Pin(1, "PA9", {"AF7": "USART1_TX"}, port="A", bit=9, gpio_name_format="P{port}{bit}", owner="USART1_TX", direction="out",
                       drive="H"),
                   Pin(2, "PA10", {"AF7": "USART1_RX"}, port="A", bit=10, gpio_name_format="P{port}{bit}")])
    now = [0.0]
    pb.time_fn = lambda: now[0]
    evs = []
    pb.subscribe(lambda ev: evs.append((ev.t_ns, ev.pin, ev.new)) if ev.field == "level" else None)
    return pb, now, evs


def test_wave_is_applied_lazily_in_time_order():
    pb, now, evs = _board()
    edges = frame_edges(Frame(0x55, 8, "N", 1.0, 1e6), 0.0)
    assert pb.drive_wave("USART1_TX", edges)
    assert evs == [(0.0, "PA9", "L")]                      # the start bit at once
    now[0] = 3500.0
    pb.sync_waves()
    assert [e[0] for e in evs] == [0.0, 1000.0, 2000.0, 3000.0]
    # another pin event at 5200 ns first publishes the wave edges before it
    now[0] = 5200.0
    pb.set_external("PA10", "L")
    assert [e[0] for e in evs] == [0.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 5200.0]
    assert evs[-1][1] == "PA10"
    now[0] = 20000.0
    pb.sync_waves()
    assert pb.get("PA9").level() == "H" and not pb.wave_pending()
    assert [e[2] for e in evs if e[1] == "PA9"] == ["L", "H", "L", "H", "L", "H", "L", "H", "L", "H"]


def test_unmuxed_signal_gets_no_wave():
    pb, now, evs = _board()
    pb.mcu_update("PA9", owner="GPIO", direction="out", drive="H")
    assert not pb.drive_wave("USART1_TX", frame_edges(Frame(0x55, baud=1e6), 0.0))
    assert evs == [] and not pb.wave_pending()


def test_open_drain_wave_pulls_low_and_releases():
    pb, now, evs = _board()
    pb.mcu_update("PA9", direction="in", drive=None, pull="up")
    assert pb.drive_wave("USART1_TX", frame_edges(Frame(0x01, baud=1e6), 0.0), od=True)
    assert pb.get("PA9").level() == "L"
    now[0] = 1500.0
    pb.sync_waves()
    assert pb.get("PA9").level() == "H"                    # released: the pull-up


def test_realtime_listener_gets_scheduled_edges():
    """An input listener that must see edges live (wave_rt) gets one scheduler event per edge."""
    from chipem.time import Clock, Scheduler
    pb, now, evs = _board()
    clock = Clock(1e9)                                      # 1 cycle = 1 ns
    sched = Scheduler(clock)
    pb.sched = sched
    pb.time_fn = clock.now_ns
    seen = []

    def cb(pin):
        seen.append((clock.now_ns(), pin.level()))
    cb.wave_rt = True
    pb.input_listeners["PA9"].append(cb)
    pb.drive_wave("USART1_TX", frame_edges(Frame(0x0F, baud=1e6), 0.0))
    assert len(sched) == 1                                  # one event: the next edge only
    for _ in range(20):
        nd = sched.next_due()
        if nd is None:
            break
        clock.advance(nd - clock.cycles)
        sched.run_due()
    assert seen == [(0.0, "L"), (1000.0, "H"), (5000.0, "L"), (9000.0, "H")]


# ---------------------------------------------------------------- every family: VCD decodes to the bytes
def _need(p: Path):
    if not p.exists():
        pytest.skip(f"{p.name} not built")
    return p


FAMILIES = {      # chip, firmware, UART, clock, (input time, bytes) for echo samples, run time
    "kinetis": ("mk22fn1m0vll12", "firmware/samples/build/uart_echo.elf", "UART0", 120e6, (2e6, b"ok"), 8e6),
    "stm32": ("stm32f405zg", "firmware/samples_stm32/build/uart_echo.elf", "USART1", None, (2e6, b"ok"), 8e6),
    "stm32f1": ("stm32f103t6", "firmware/samples_stm32f1/build/uart_echo.elf", "USART1", None, (2e6, b"ok"), 8e6),
    "c2000": ("tms320f28232", "firmware/samples_c2000/build/sci_echo.c28img", "SCIA", None, (7e6, b"ok"), 10e6),
    "mcs51": ("p89cv51rd2", "firmware/samples_mcs51/build/uart_hello.hex", "UART", None, None, 70e6),
}


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_vcd_of_uart_tx_decodes_to_the_bytes(fam, tmp_path):
    from chipem.session import Session
    chip, fw, uart, clk, feed, dur = FAMILIES[fam]
    fw = _need(ROOT / fw)
    if not (ROOT / "chips" / f"{chip}.yaml").exists():
        pytest.skip(f"no chip {chip}")
    vcd = tmp_path / "t.vcd"
    s = Session(chip, fw, clock_hz=clk, quiet=True, vcd_path=vcd)
    try:
        name = s.chip.find_uart(uart)
        blk = s.chip.peripherals[name]
        if feed:
            s.schedule_uart(feed[0], name, feed[1])
        s.run_for(dur)
        sent = bytes(blk.output)
        assert sent, "nothing transmitted"
        pin = tx_pin(s.chip, blk)
        baud = blk.tx_frame.baud
        bits = blk.tx_frame.bits
    finally:
        s.close()
    got = decode_8n1(vcd_changes(vcd, pin.id), baud, nbits=bits)
    assert got == sent


# ---------------------------------------------------------------- GPIO input register mid-frame
def test_stm32_idr_reads_the_tx_bit_mid_frame():
    """IDR of an AF pin returns the pin level: while USART1 shifts a frame out, IDR.9 follows the bits."""
    from chipem.session import Session
    fw = _need(ROOT / "firmware/samples_stm32/build/uart_echo.elf")
    s = Session("stm32f405zg", fw, quiet=True)
    try:
        chip = s.chip
        blk = chip.peripherals["USART1"]
        gpio = chip.peripherals["GPIOA"]
        s.run_for(3e6)                                        # "hello\r\n" is out; the echo loop polls RXNE
        t_start = []
        blk.frame_listeners.append(lambda u, f: t_start.append((chip.now_ns(), f.baud)))
        chip.bus.write(blk.base + 4, 4, 0x35)                 # DR = '5' (0b00110101), as the firmware would
        assert t_start
        t0, baud = t_start[0]
        tb = 1e9 / baud
        want = [0] + [(0x35 >> k) & 1 for k in range(8)] + [1]
        seen = []
        for k in range(10):
            s.run_for(max(t0 + (k + 0.5) * tb - chip.now_ns(), 1.0))
            seen.append((gpio.read(0x10, 4) >> 9) & 1)          # IDR, like a firmware read (no cache mid-frame)
        assert seen == want
    finally:
        s.close()


# ---------------------------------------------------------------- input capture on another chip (board net)
STM32_TX_U = """
    ldr r0, =0x40023830     @ RCC_AHB1ENR: GPIOA
    ldr r1, [r0]
    orr r1, r1, #1
    str r1, [r0]
    ldr r0, =0x40023844     @ RCC_APB2ENR: USART1
    ldr r1, [r0]
    orr r1, r1, #0x10
    str r1, [r0]
    ldr r0, =0x40020000     @ GPIOA
    ldr r1, [r0]            @ MODER9 = AF
    bic r1, r1, #0xC0000
    orr r1, r1, #0x80000
    str r1, [r0]
    ldr r1, [r0, #0x24]     @ AFRH: AF7 on PA9
    bic r1, r1, #0xF0
    orr r1, r1, #0x70
    str r1, [r0, #0x24]
    ldr r0, =0x40011000     @ USART1
    movw r1, #1667          @ 16 MHz / 1667 = 9598 baud
    str r1, [r0, #8]
    movw r1, #0x2008        @ UE | TE
    str r1, [r0, #0xC]
    movs r2, #0x55
loop:
    ldr r1, [r0]
    tst r1, #0x80
    beq loop
    str r2, [r0, #4]
    b loop
"""


def test_timer_capture_on_another_chip_measures_the_bit_time():
    """Chip A (STM32F405) sends 'U' (0x55: a square wave of 2 bit times) at 9600 baud on PA9; a board net takes
    it to chip B's PA7 (TIM3_CH2) running tim_capture, which measures the period between rising edges in us."""
    from chipem.board import Board
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    from chipem.firmware import FirmwareImage, load_firmware
    from elftools.elf.elffile import ELFFile
    import keystone
    fw_b = _need(ROOT / "firmware/samples_stm32/build/tim_capture.elf")
    desc = load_chip(ROOT / "chips" / "stm32f405zg.yaml")
    ks = keystone.Ks(keystone.KS_ARCH_ARM, keystone.KS_MODE_THUMB)
    code, _ = ks.asm(STM32_TX_U, 0x08000200)
    vt = (0x20010000).to_bytes(4, "little") + (0x08000200 | 1).to_bytes(4, "little")
    img = bytearray(0x200) + bytes(code)
    img[:8] = vt
    a = Chip(desc)
    a.load_firmware(FirmwareImage([(0x08000000, bytes(img))], 0x08000201))
    b = Chip(desc)
    imgb = load_firmware(str(fw_b))
    b.load_firmware(imgb)
    board = Board({"A": a, "B": b}, quantum_ns=2_000.0)
    board.add_net("U", [("A", "PA9"), ("B", "PA7")])
    board.run_for(12e6)
    with open(fw_b, "rb") as f:
        st = ELFFile(f).get_section_by_name(".symtab")
        sym = {n: st.get_symbol_by_name(n)[0]["st_value"] for n in ("period_us", "captures")}
    period = b.bus.read(sym["period_us"], 4)
    captures = b.bus.read(sym["captures"], 4)
    assert captures > 40                  # 5 rising edges per character, ~1 ms per character
    bit_us = 1e6 / a.peripherals["USART1"].tx_frame.baud
    assert abs(period - 2 * bit_us) <= 1.0, (period, 2 * bit_us)


def test_three_chip_demo_board_advances_with_realtime_waves():
    """Regression: a realtime wave edge whose scheduler cycle converts back to a hair before its ns time made
    _wave_tick reschedule itself at the same cycle forever (virtual time stuck at 0 on boards/three_chip_demo.yaml,
    where TX nets reach observed pins). The tick now applies edges up to its own scheduled time."""
    import threading
    from chipem.session import Session
    board = ROOT / "boards" / "three_chip_demo.yaml"
    for fw in ("firmware/samples/build/uart_link_sender.elf", "firmware/samples_stm32/build/uart_link_echo.elf",
               "firmware/samples_c2000/build/uart_link_echo.c28img"):
        _need(ROOT / fw)
    s = Session.from_board(board, quiet=True)
    try:
        t = threading.Timer(60.0, s.stop)            # a hang becomes a failure instead of a stuck suite
        t.start()
        try:
            s.run_for(3e6)
        finally:
            t.cancel()
        assert s.board.now_ns() >= 3e6
        assert s.uart_output("A.UART0").startswith(b"ping 1")
    finally:
        s.close()


def test_wave_tick_rounding_does_not_loop():
    """Unit form of the regression: at a clock whose cycle grid does not hit the edge time exactly."""
    from chipem.time import Clock, Scheduler
    pb, now, evs = _board()
    clock = Clock(120e6)
    sched = Scheduler(clock)
    pb.sched, pb.time_fn = sched, clock.now_ns
    cb = lambda pin: None                          # noqa: E731
    cb.wave_rt = True
    pb.input_listeners["PA9"].append(cb)
    pb.drive_wave("USART1_TX", frame_edges(Frame(0x55, baud=115200.0), 0.0))
    fired = 0
    while sched.next_due() is not None and fired < 100:
        clock.advance(max(sched.next_due() - clock.cycles, 0))
        fired += sched.run_due()
    assert fired <= 12 and not pb.wave_pending()
