"""STM32F446 HDMI-CEC controller (chipem/periph/stm32/cec.py, RM0390 Rev 9 ch. 32): bit timing on the line (32.3.4),
OAR / broadcast / listen mode (32.7.2), ACK and TXACKE (32.7.5), arbitration (32.4), TXBR / TXUDR, SBPE error bit
(32.5.4), CFGR write protection (32.7.2), and the two-chip sample board boards/stm32f446re_cec_pair.yaml (skipped when
firmware/samples_stm32f446 is not built).  Clock: HSI 16 MHz -> CEC kernel clock HSI/488 = 32.787 kHz, so every
nominal time is scaled by 32768 / 32786.9 (the controller counts kernel clock ticks)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.board import load_board
from chipem.session import Session

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32f446" / "build"
FLASH = 0x08000000
RCC, GPIOB, C = 0x40023800, 0x40020400, 0x40006C00
CR, CFGR, TXDR, RXDR, ISR, IER = range(0, 0x18, 4)
RXBR, RXEND, RXOVR, BRE, SBPE, LBPE, RXACKE, ARBLST = 1, 2, 4, 8, 16, 32, 64, 128
TXBR, TXEND, TXUDR, TXERR, TXACKE = 0x100, 0x200, 0x400, 0x800, 0x1000
K = 32768.0 / (16e6 / 488)                                  # nominal ms -> kernel-clock scaled ms
MS = 1e6


def spin_bin(tmp: Path) -> Path:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    p = tmp / "spin.bin"
    p.write_bytes(bytes(img))
    return p


def pair(tmp: Path, n: int = 2) -> Session:
    fw = spin_bin(tmp)
    ids = "AB"[:n]
    lines = ["name: cec_test", "quantum: 10us", "chips:"]
    lines += [f"  - {{id: {i}, chip: stm32f446re, firmware: {fw}, load_addr: 0x08000000}}" for i in ids]
    if n > 1:
        lines += ["nets:", "  - {name: CEC, pins: [" + ", ".join(f"{i}.PB6" for i in ids) + "], open_drain: true}"]
    (tmp / "b.yaml").write_text("\n".join(lines) + "\n")
    s = Session.from_board(load_board(tmp / "b.yaml"), quiet=True)
    for i in ids:
        setup(s.chips[i])
    return s


def setup(c, cfgr: int = 0, ier: int = 0) -> None:
    w = c.bus.write
    w(RCC + 0x30, 4, 0x2)                                    # GPIOB
    w(RCC + 0x40, 4, 1 << 27)                                # CECEN (APB1)
    w(GPIOB + 0x04, 4, 1 << 6)                               # PB6 open drain
    w(GPIOB + 0x20, 4, 3 << 24)                              # AF3 = CEC
    w(GPIOB + 0x00, 4, c.bus.read(GPIOB, 4) | (2 << 12))
    w(C + CFGR, 4, cfgr)
    w(C + IER, 4, ier)
    w(C + CR, 4, 1)


def rd(c, off):
    return c.bus.read(C + off, 4)


def wr(c, off, v):
    c.bus.write(C + off, 4, v)


def reconfig(c, cfgr):
    wr(c, CR, 0)
    wr(c, CFGR, cfgr)
    wr(c, CR, 1)


def oar(*addrs):
    v = 0
    for a in addrs:
        v |= 1 << (16 + a)
    return v


def edges(s, chip="A", pin="PB6"):
    return [(e.t_ns, e.new) for e in s.events(kinds={"pin"}, chip=chip) if e.pin == pin and e.field == "level"]


def send(s, c, msg, limit_ms=200.0, rx=None):
    """Polled transmit (RM0390 32.7.1 sequence: TXEOM before the last TXDR write, TXDR before TXSOM).  `rx`: a
    follower chip whose received bytes are read (RXBR cleared) every 0.1 ms -- otherwise its next byte is an RXOVR."""
    wr(c, ISR, 0x1FFF)
    if len(msg) == 1:
        wr(c, CR, rd(c, CR) | 4)
    wr(c, TXDR, msg[0])
    wr(c, CR, rd(c, CR) | 2)
    i = 1
    for _ in range(int(limit_ms * 10)):
        s.run_for(0.1 * MS)
        if rx is not None and rd(rx, ISR) & RXBR:
            rx.rx_bytes = getattr(rx, "rx_bytes", []) + [rd(rx, RXDR)]
            wr(rx, ISR, RXBR)
        isr = rd(c, ISR)
        if isr & TXBR and i < len(msg):
            if i == len(msg) - 1:
                wr(c, CR, rd(c, CR) | 4)
            wr(c, TXDR, msg[i])
            i += 1
            wr(c, ISR, TXBR)
        if isr & (TXEND | TXACKE | TXERR | TXUDR):
            return isr
    raise AssertionError("transmission did not end")


def received(c):
    return c.peripherals["CEC"].messages


# ---------------------------------------------------------------------------------------------- 32.3.4 bit timing
def test_bit_timing_and_ack(tmp_path):
    s = pair(tmp_path)
    a, b = s.chips["A"], s.chips["B"]
    reconfig(b, oar(0))
    wr(b, IER, RXBR | RXEND)
    isr = send(s, a, [0x40, 0x9F], rx=b)
    assert isr & TXEND and not isr & (TXACKE | TXERR)
    ev = edges(s)
    while ev[0][1] != "L":                                    # skip the pin-mux event (line idle high)
        ev.pop(0)
    t0 = ev[0][0]
    rel = [(round((t - t0) / MS / K, 3), lvl) for t, lvl in ev]
    # start bit: low 3.7 ms, period 4.5 ms; header 0x40 = 0100 0000: bit 7 (0) low 1.5, bit 6 (1) low 0.6
    assert rel[0] == (0.0, "L") and rel[1] == (3.7, "H") and rel[2] == (4.5, "L")
    assert rel[3] == (6.0, "H") and rel[4] == (6.9, "L") and rel[5] == (7.5, "H")
    # header ACK bit (block 0, bit 9): the follower holds the line until 1.5 ms (logical 0 = acknowledged)
    ack = 4.5 + 9 * 2.4
    i = rel.index((round(ack, 3), "L"))
    assert rel[i + 1][1] == "H" and abs(rel[i + 1][0] - (ack + 1.5)) < 0.02   # (+ up to one board quantum)
    assert b.rx_bytes == [0x40, 0x9F] and rd(b, ISR) & RXEND
    assert received(b)[-1]["bytes"] == [0x40, 0x9F] and received(b)[-1]["mode"] == "own"
    s.close()


# ---------------------------------------------------------------------------------- 32.7.5 TXACKE, 32.7.2 OAR / LSTN
def test_nack_broadcast_and_listen(tmp_path):
    s = pair(tmp_path)
    a, b = s.chips["A"], s.chips["B"]
    reconfig(b, oar(0))
    assert send(s, a, [0x45]) & TXACKE                       # ping 5: nobody acknowledges
    assert rd(b, ISR) == 0                                    # not for B: ignored without flags
    isr = send(s, a, [0x4F, 0x82, 0x10, 0x00], rx=b)          # broadcast: ACK bits left high = accepted
    assert isr & TXEND
    assert received(b)[-1] == {**received(b)[-1], "bytes": [0x4F, 0x82, 0x10, 0x00], "mode": "bcast"}
    # listen mode: a message to 5 is received but never acknowledged -> TXACKE at A, RXACKE at B
    reconfig(b, oar(0) | (1 << 31))
    wr(b, ISR, 0x1FFF)
    assert send(s, a, [0x45, 0x36]) & TXACKE
    s.run_for(5 * MS)
    assert rd(b, RXDR) == 0x45 and rd(b, ISR) & RXACKE
    s.close()


# --------------------------------------------------------------------------------------------------- 32.4 arbitration
def test_arbitration_lost_then_retry(tmp_path):
    s = pair(tmp_path)
    a, b = s.chips["A"], s.chips["B"]
    reconfig(a, oar(4))
    reconfig(b, oar(3))
    for c in (a, b):
        wr(c, ISR, 0x1FFF)
        wr(c, CR, rd(c, CR) | 4)                              # pings
    wr(a, TXDR, 0x43)                                         # initiator 4 = 0100 ...
    wr(b, TXDR, 0x34)                                         # initiator 3 = 0011 ... wins at the 2nd bit
    wr(a, CR, 7)
    wr(b, CR, 7)
    s.run_for(80 * MS)
    assert rd(a, ISR) & ARBLST and rd(b, ISR) & TXEND
    assert received(a)[0]["bytes"] == [0x34] and received(a)[0]["mode"] == "own"   # A acknowledged B's ping
    assert rd(a, CR) & 2 == 0 and rd(a, ISR) & TXEND          # TXSOM stayed pending: retried after SFT
    assert [m["bytes"] for m in received(b)] == [[0x34], [0x43]]
    s.close()


# ------------------------------------------------------------------------------------------- 32.7.5 TXBR / TXUDR
def test_tx_underrun(tmp_path):
    s = pair(tmp_path)
    a, b = s.chips["A"], s.chips["B"]
    reconfig(b, oar(0))
    wr(a, ISR, 0x1FFF)
    wr(a, TXDR, 0x40)
    wr(a, CR, 3)                                              # header without EOM, nothing more written
    s.run_for(40 * MS)
    isr = rd(a, ISR)
    assert isr & TXBR and isr & TXUDR and rd(a, CR) & 6 == 0
    s.close()


# ----------------------------------------------------------------------------- 32.5.4 SBPE -> error bit on the line
def test_sbpe_generates_error_bit(tmp_path):
    s = pair(tmp_path, n=1)
    c = s.chips["A"]
    reconfig(c, oar(0))
    wr(c, IER, SBPE)
    pins = c.pins
    k = K * MS
    t = c.now_ns() + 1 * MS
    seq = [(0, "L"), (3.7, "H"), (4.5, "L"), (5.1, "H"), (5.9, "L")]   # start bit, then a bit falling at 1.4 ms
    for dt, lvl in seq:
        c.sched.at_ns(t + dt * k, lambda lv=lvl: pins.set_external("PB6", lv))
    c.sched.at_ns(t + 6.5 * k, lambda: pins.set_external("PB6", "H"))
    s.run_for(8 * MS)
    assert rd(c, ISR) & SBPE
    ev = [(round((tt - t) / k, 2), lv) for tt, lv in edges(s)]
    assert (5.9, "L") in ev
    s.run_for(5 * MS)
    ev = [(round((tt - t) / k, 2), lv) for tt, lv in edges(s)]
    assert (9.5, "H") in ev                                   # the controller held the line for 3.6 ms
    s.close()


# ----------------------------------------------------------------------------------- 32.7.2 / 32.7.6 write rules
def test_cfgr_and_ier_ignored_while_enabled(tmp_path):
    s = pair(tmp_path, n=1)
    c = s.chips["A"]
    wr(c, CFGR, 0x7)
    wr(c, IER, 0x1)
    assert rd(c, CFGR) == 0 and rd(c, IER) == 0
    assert any("CEC_CFGR written while CECEN = 1" in e.fields.get("msg", "") for e in s.events(kinds={"warn"}))
    wr(c, CR, 0)
    assert rd(c, CR) == 0
    wr(c, CFGR, 0xFFFFFFFF)
    assert rd(c, CFGR) == 0xFFFF01FF                          # bits 15:9 reserved
    wr(c, TXDR, 0x1FF)
    assert rd(c, TXDR) == 0                                    # write-only
    s.close()


# ------------------------------------------------------------------------------------------------ sample board
def test_board_cec_pair():
    for n in ("cec_ping.elf", "cec_tv.elf"):
        if not (BUILD / n).exists():
            pytest.skip(f"{n} not built")
    s = Session.from_board(ROOT / "boards" / "stm32f446re_cec_pair.yaml", quiet=True)
    s.run_for(400 * MS)
    assert s.uart_output("A.USART2") == (b"CEC A addr 4\r\nping 5: nack\r\nping 0: ack\r\ntx 40 9F: ack\r\n"
                                         b"rx 04 9E 05\r\nbcast 4F 82 10 00: ack\r\ndone\r\n")
    assert s.uart_output("B.USART2") == (b"CEC B addr 0\r\nrx 40\r\nrx 40 9F\r\ntx 04 9E 05: ack\r\n"
                                         b"rx 4F 82 10 00\r\nerrors 00\r\ndone\r\n")
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()


def test_board_cec_hal():
    """HAL_CEC follower (firmware/samples_stm32_hal_f446/cec_hal.c: HAL_CEC_Init / IRQHandler / Transmit_IT, CEC clock
    through HAL_RCCEx_PeriphCLKConfig) against the bare-metal initiator: the same transcript."""
    hal = ROOT / "firmware" / "samples_stm32_hal_f446" / "build" / "cec_hal.elf"
    if not (BUILD / "cec_ping.elf").exists() or not hal.exists():
        pytest.skip("cec samples not built")
    s = Session.from_board(ROOT / "boards" / "stm32f446re_cec_hal.yaml", quiet=True)
    s.run_for(400 * MS)
    assert s.uart_output("A.USART2").endswith(b"rx 04 9E 05\r\nbcast 4F 82 10 00: ack\r\ndone\r\n")
    assert s.uart_output("B.USART2") == (b"CEC B addr 0\r\nrx 40\r\nrx 40 9F\r\ntx 04 9E 05: ack\r\n"
                                         b"rx 4F 82 10 00\r\nerrors 00\r\ndone\r\n")
    assert s.chips["B"].peripherals["RCC"].get("DCKCFGR2") >> 26 & 1 == 0     # CECSEL = HSI/488
    s.close()
