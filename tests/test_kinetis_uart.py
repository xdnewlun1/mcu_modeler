"""Kinetis UART model (chipem/periph/kinetis/uart.py): TX capture, baud pacing, RX by polling and by IRQ."""
from engine_util import DATA
from kutil import SPIN, UART0_BASE, UART2_BASE, count_handler, counter, enable_irq, kchip

BDH, BDL, C2, S1, D, C4, PFIFO, TCFIFO, RCFIFO = 0, 1, 3, 4, 7, 0xA, 0x10, 0x14, 0x16
CFIFO, SFIFO, TWFIFO, RWFIFO = 0x11, 0x12, 0x13, 0x15


def uart_init(base: int, sbr: int, brfa: int = 0, c2: int = 0x0C) -> str:
    return f"""
  ldr r0, =0x{base:08X}
  movs r1, #{(sbr >> 8) & 0x1F}
  strb r1, [r0, #{BDH}]
  movs r1, #{sbr & 0xFF}
  strb r1, [r0, #{BDL}]
  movs r1, #{brfa}
  strb r1, [r0, #{C4}]
  movs r1, #{c2}
  strb r1, [r0, #{C2}]
"""


def puts_poll(base: int, text: bytes) -> str:
    s = f"  ldr r0, =0x{base:08X}\n"
    for i, b in enumerate(text):
        s += f"""
w{i}:
  ldrb r2, [r0, #{S1}]
  tst r2, #0x80
  beq w{i}
  movs r1, #{b}
  strb r1, [r0, #{D}]
"""
    return s


ECHO_POLL = f"""
  ldr r0, =0x{UART0_BASE:08X}
loop:
  ldrb r2, [r0, #{S1}]
  tst r2, #0x20
  beq loop
  ldrb r1, [r0, #{D}]
txw:
  ldrb r2, [r0, #{S1}]
  tst r2, #0x80
  beq txw
  strb r1, [r0, #{D}]
  b loop
"""


def test_tx_capture_and_events():
    chip, _ = kchip({"reset": uart_init(UART0_BASE, 0) + puts_poll(UART0_BASE, b"ok\n") + SPIN})
    chip.run_for(1e6)
    u = chip.peripherals["UART0"]
    assert bytes(u.output) == b"ok\n"
    assert chip.uart_out("UART0") == b"ok\n"
    evs = chip.log.filter("uart")
    assert [e.byte for e in evs] == list(b"ok\n") and all(e.uart == "UART0" and e.dir == "tx" for e in evs)
    assert u.get("S1") & 0xC0 == 0xC0                 # unconfigured baud -> TDRE/TC immediately


def test_tx_without_te_is_dropped():
    chip, _ = kchip({"reset": uart_init(UART0_BASE, 0, c2=0x04) + puts_poll(UART0_BASE, b"x") + SPIN})
    chip.run_for(0.2e6)
    assert chip.uart_out("UART0") == b""
    assert any("transmitter disabled" in str(e.get("msg")) for e in chip.log.filter("warn"))


def test_baud_pacing():
    # UART0 clocked by the core clock (10 MHz here): SBR 5, BRFA 13 -> 10e6 / (16 * 5.40625) = 115,607 baud
    hz = 10e6
    chip, _ = kchip({"reset": uart_init(UART0_BASE, 5, 13) + puts_poll(UART0_BASE, b"0123456789") + SPIN}, hz=hz)
    u = chip.peripherals["UART0"]
    chip.run_for(10)  # let init run a little
    chip.run_for(0.1e6)
    assert abs(u.baud() - 10e6 / (16 * (5 + 13 / 32))) < 1
    char_ns = 10 * 1e9 / u.baud()                     # 8N1 = 10 bits
    chip.run_for(10 * char_ns - 0.1e6 + 2000)
    n_full = len(u.output)
    assert n_full == 10
    times = [e.t_ns for e in chip.log.filter("uart")]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(abs(g - char_ns) < 0.02 * char_ns for g in gaps), gaps


def test_uart2_uses_bus_clock():
    chip, _ = kchip({"reset": uart_init(UART2_BASE, 1) + SPIN}, hz=8e6)
    chip.run_for(0.05e6)
    assert abs(chip.peripherals["UART2"].baud() - 4e6 / 16) < 1   # bus = core/2 without an MCG model
    assert abs(chip.peripherals["UART0"].baud() - 8e6 / (16 * 4)) < 1   # UART0: core clock, reset SBR = 4


def test_rx_polling_echo():
    chip, _ = kchip({"reset": uart_init(UART0_BASE, 5, 13) + ECHO_POLL}, hz=10e6)
    u = chip.peripherals["UART0"]
    u.feed(b"abc")
    chip.run_for(1e6)
    assert bytes(u.output) == b"abc"
    rx = [e for e in chip.log.filter("uart") if e.dir == "rx"]
    assert [e.byte for e in rx] == list(b"abc")
    char_ns = 10 * 1e9 / u.baud()
    gaps = [b.t_ns - a.t_ns for a, b in zip(rx, rx[1:])]
    assert all(g >= char_ns * 0.98 for g in gaps)
    assert u.rx_pending() == 0


def test_rx_interrupt():
    # RIE: handler reads D and stores it at DATA+0x100+count
    handler = f"""
  ldr r0, =0x{UART0_BASE:08X}
  ldrb r1, [r0, #{D}]
  ldr r0, =0x{DATA + 0x80:08X}
  ldr r2, [r0]
  adds r3, r0, r2
  strb r1, [r3, #0x80]
  adds r2, #1
  str r2, [r0]
  bx lr
"""
    code = uart_init(UART0_BASE, 5, 13, c2=0x2C) + enable_irq(7) + SPIN
    chip, _ = kchip({"reset": code, "rx": handler}, vectors={16 + 7: "rx"}, hz=10e6)
    chip.run_for(0.05e6)
    chip.peripherals["UART0"].feed(b"xyz")
    chip.run_for(1e6)
    assert counter(chip) == 3
    assert chip.core.read_mem(DATA + 0x100, 3) == b"xyz"
    assert 16 + 7 not in chip.arch.pending          # (RTC: TIF&TIIE at power-on keeps IRQ5 pending)


def test_tx_interrupt_tie():
    # TIE with TDRE set: the IRQ fires; the handler writes one byte per interrupt, then disables TIE after 4
    handler = f"""
  ldr r0, =0x{DATA + 0x80:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  ldr r0, =0x{UART0_BASE:08X}
  adds r2, r1, #0x40
  strb r2, [r0, #{D}]
  cmp r1, #4
  bne done
  movs r2, #0x0C
  strb r2, [r0, #{C2}]
done:
  bx lr
"""
    code = uart_init(UART0_BASE, 5, 13, c2=0x8C) + enable_irq(7) + SPIN
    chip, _ = kchip({"reset": code, "tx": handler}, vectors={16 + 7: "tx"}, hz=10e6)
    chip.run_for(2e6)
    assert chip.uart_out("UART0") == b"ABCD"
    assert counter(chip) == 4


def test_fifo_registers():
    chip, _ = kchip({"reset": SPIN})
    u0 = UART0_BASE
    assert chip.bus.read(u0 + PFIFO, 1) == 0x22         # 8-entry TX/RX FIFOs (RM §3.9.5.1), TXFE/RXFE off
    assert chip.bus.read(UART2_BASE + PFIFO, 1) == 0x00  # UART2: 1-entry
    assert chip.bus.read(u0 + SFIFO, 1) == 0xC0 and chip.bus.read(u0 + RWFIFO, 1) == 1
    assert chip.bus.read(u0 + TCFIFO, 1) == 0
    assert chip.bus.read(u0 + RCFIFO, 1) == 0
    assert chip.bus.read(u0 + S1, 1) == 0xC0


def _drops(chip):
    return [(chr(e.byte), e.reason) for e in chip.log.filter("uart") if e.dir == "drop"]


def test_rx_disabled_drops_and_overrun():
    # receiver off (C2.RE = 0): bytes are lost, not queued; then nobody reads D: OR, later bytes lost
    chip, _ = kchip({"reset": uart_init(UART0_BASE, 5, 13, c2=0x08) + SPIN}, hz=10e6)
    u = chip.peripherals["UART0"]
    chip.run_for(0.02e6)
    char_ns = u.char_ns()
    u.feed(b"ab")
    chip.run_for(2 * char_ns + 1000)
    assert _drops(chip) == [("a", "rx_disabled"), ("b", "rx_disabled")] and u.rx_pending() == 0
    chip.bus.write(UART0_BASE + C2, 1, 0x0C)
    u.feed(b"xyz")
    chip.run_for(3 * char_ns + 1000)
    s1 = chip.bus.read(UART0_BASE + S1, 1)
    assert s1 & 0x28 == 0x28                                  # RDRF | OR
    assert _drops(chip)[2:] == [("y", "overrun"), ("z", "overrun")]
    assert chip.bus.read(UART0_BASE + D, 1) == ord("x")      # S1 (with OR) then D: both clear
    assert chip.bus.read(UART0_BASE + S1, 1) & 0x28 == 0
    assert u.rx_dropped == 4
    assert not chip.log.filter("warn")


def test_rx_or_blocks_until_cleared():
    # OR set: no data is stored even after D was read without the S1-read step
    chip, _ = kchip({"reset": uart_init(UART0_BASE, 5, 13) + SPIN}, hz=10e6)
    u = chip.peripherals["UART0"]
    chip.run_for(0.02e6)
    u.feed(b"12")
    chip.run_for(2 * u.char_ns() + 1000)
    assert u.get("S1") & 0x28 == 0x28
    assert u.read(D, 1) == ord("1")                           # D without a prior S1 read: RDRF clears, OR stays
    assert u.get("S1") & 0x28 == 0x08
    u.feed(b"3")
    chip.run_for(u.char_ns() + 1000)
    assert u.get("S1") & 0x20 == 0 and _drops(chip)[-1] == ("3", "overrun")


def test_rx_gate_and_pin_on_k22():
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    from engine_util import asm
    from chipem.firmware import FirmwareImage
    import struct
    chip = Chip(load_chip("chips/mk22fn1m0vll12.yaml"), clock_hz=120e6)
    code = asm("spin: b spin", 0x400)
    img = bytearray(0x400 + len(code))
    img[0:8] = struct.pack("<II", 0x20000000, 0x401)
    img[0x400:] = code
    chip.load_firmware(FirmwareImage([(0, bytes(img))]))
    chip.reset()
    u = chip.peripherals["UART0"]
    w8 = lambda a, v: chip.bus.write(a, 1, v)                 # noqa: E731
    w8(UART0_BASE + BDL, 65)
    w8(UART0_BASE + C2, 0x0C)
    t = u.char_ns() + 1000
    u.feed(b"g")                                              # SIM_SCGC4.UART0 still off
    chip.run_for(t)
    chip.bus.write(0x40048034, 4, chip.bus.read(0x40048034, 4) | 1 << 10)     # SIM_SCGC4: UART0
    u.feed(b"p")                                              # PTA1/PTA15/PTB16/PTD6 not muxed to UART0_RX
    chip.run_for(t)
    chip.bus.write(0x40048038, 4, chip.bus.read(0x40048038, 4) | 1 << 10)     # SIM_SCGC5: PORTB
    chip.bus.write(0x4004A040, 4, 3 << 8)                     # PORTB_PCR16: MUX 3 = UART0_RX
    u.feed(b"k")
    chip.run_for(t)
    assert _drops(chip) == [("g", "clock_gated"), ("p", "pin_not_muxed")]
    assert u.get("S1") & 0x20 and u.read(D, 1) == ord("k")


# ---------------------------------------------------------------- FIFOs (RM §49.3.16-49.3.22, §49.8.4)
def _fifo_chip(base=UART0_BASE, pfifo=0x88, rwfifo=1, twfifo=0, sbr=5, brfa=13):
    """UART with baud set, FIFOs enabled per `pfifo` (written with TE/RE clear, then both flushed), TE/RE on."""
    chip, _ = kchip({"reset": SPIN}, hz=10e6)
    w8 = lambda o, v: chip.bus.write(base + o, 1, v)         # noqa: E731
    r8 = lambda o: chip.bus.read(base + o, 1)                # noqa: E731
    w8(BDH, sbr >> 8)
    w8(BDL, sbr & 0xFF)
    w8(C4, brfa)
    w8(PFIFO, pfifo)
    w8(CFIFO, 0xC0)
    w8(RWFIFO, rwfifo)
    w8(TWFIFO, twfifo)
    w8(C2, 0x0C)
    u = chip.peripherals["UART0" if base == UART0_BASE else "UART2"]
    chip.run_for(u.char_ns() + 1000)                          # TE 0->1 idle preamble (§49.4.1.3)
    return chip, u, w8, r8


def test_fifo_enable_reports_depth():
    chip, u, w8, r8 = _fifo_chip()
    assert r8(PFIFO) == 0xAA and u.tx_depth() == 8 and u.rx_depth() == 8
    w8(PFIFO, 0x00)                                           # size fields are read-only
    assert r8(PFIFO) == 0x22 and u.rx_depth() == 1


def test_rx_fifo_watermark_counts_and_overrun_on_ninth():
    chip, u, w8, r8 = _fifo_chip(rwfifo=4)
    t = u.char_ns()
    u.feed(b"abc")
    chip.run_for(3 * t + 1000)
    assert r8(RCFIFO) == 3 and not r8(S1) & 0x20 and not r8(SFIFO) & 0x40   # below RXWATER: no RDRF
    u.feed(b"d")
    chip.run_for(t + 1000)
    assert r8(RCFIFO) == 4 and r8(S1) & 0x20                  # RCFIFO >= RXWATER
    u.feed(b"efgh")
    chip.run_for(4 * t + 1000)
    assert r8(RCFIFO) == 8 and not r8(S1) & 0x08 and not _drops(chip)
    u.feed(b"ij")
    chip.run_for(2 * t + 1000)
    assert _drops(chip) == [("i", "overrun"), ("j", "overrun")]
    assert r8(S1) & 0x28 == 0x28 and r8(SFIFO) & 0x04         # OR and RXOF; the 8 stored bytes are kept
    got = bytes(r8(D) for _ in range(8))                      # S1 was read with OR set, then D: OR clears
    assert got == b"abcdefgh" and r8(RCFIFO) == 0
    s1 = r8(S1)
    assert not s1 & 0x28 and r8(SFIFO) & 0x40                 # RDRF/OR clear, RXEMPT
    assert u.rx_pending() == 0


def test_rx_fifo_rdrf_follows_level_and_or_blocks_storage():
    chip, u, w8, r8 = _fifo_chip(rwfifo=2)
    t = u.char_ns()
    u.feed(b"12")
    chip.run_for(2 * t + 1000)
    assert r8(S1) & 0x20
    assert r8(D) == ord("1") and not r8(S1) & 0x20            # 1 left < RXWATER: RDRF clears
    assert r8(D) == ord("2") and r8(RCFIFO) == 0


def test_rx_underflow_flag_and_flush():
    chip, u, w8, r8 = _fifo_chip()
    t = u.char_ns()
    u.feed(b"xy")
    chip.run_for(2 * t + 1000)
    assert r8(RCFIFO) == 2
    w8(CFIFO, 0x40)                                           # RXFLUSH
    assert r8(RCFIFO) == 0 and not r8(S1) & 0x20 and r8(SFIFO) & 0x40
    assert r8(CFIFO) == 0                                     # flush bits read 0
    assert not r8(SFIFO) & 0x01
    r8(D)                                                     # read from an empty buffer
    assert r8(SFIFO) & 0x01                                   # RXUF
    assert not u._err_line
    w8(CFIFO, 0x01)                                           # RXUFE: RXUF drives UART0_ERR (§3.9.5.3)
    assert u._err_line
    w8(SFIFO, 0x01)                                           # w1c
    assert not r8(SFIFO) & 0x01


def test_tx_fifo_queue_counts_and_overflow():
    chip, u, w8, r8 = _fifo_chip()
    t = u.char_ns()
    for b in b"012345678":                                    # 1 into the shift register + 8 in the FIFO
        w8(D, b)
    assert r8(TCFIFO) == 8 and not r8(S1) & 0xC0 and not r8(SFIFO) & 0x80
    w8(D, ord("X"))                                           # 10th: FIFO full -> lost, TXOF
    assert r8(SFIFO) & 0x02 and r8(TCFIFO) == 8
    assert chip.uart_out("UART0") == b"0"
    chip.run_for(9 * t + 2000)
    assert chip.uart_out("UART0") == b"012345678"
    assert r8(S1) & 0xC0 == 0xC0 and r8(TCFIFO) == 0 and r8(SFIFO) & 0x80
    times = [e.t_ns for e in chip.log.filter("uart") if e.dir == "tx"]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(abs(g - t) < 0.02 * t for g in gaps), gaps


def test_tx_watermark_and_flush():
    chip, u, w8, r8 = _fifo_chip(twfifo=2)
    t = u.char_ns()
    for b in b"abcdef":                                       # a shifting, b..f buffered (5 > TXWATER 2)
        w8(D, b)
    assert not r8(S1) & 0x80
    chip.run_for(3 * t + 1000)                                # b, c, d moved on (d shifting): e, f left
    assert r8(TCFIFO) == 2 and r8(S1) & 0x80
    w8(CFIFO, 0x80)                                           # TXFLUSH: buffer emptied, shift register keeps d
    assert r8(TCFIFO) == 0 and r8(SFIFO) & 0x80 and not r8(S1) & 0x40
    chip.run_for(2 * t)
    assert chip.uart_out("UART0") == b"abcd" and r8(S1) & 0x40


def test_legacy_buffer_is_double_buffered():
    # FIFOs off (reset state): 1-entry buffer + shift register, so two back-to-back writes are both sent
    chip, u, w8, r8 = _fifo_chip(pfifo=0x00)
    t = u.char_ns()
    w8(D, ord("p"))
    assert r8(S1) & 0xC0 == 0x80                              # p moved to the shifter: TDRE again, TC clear
    w8(D, ord("q"))
    assert not r8(S1) & 0x80 and r8(TCFIFO) == 1
    w8(D, ord("r"))                                           # buffer full -> TXOF, lost
    assert r8(SFIFO) & 0x02
    chip.run_for(2 * t + 1000)
    assert chip.uart_out("UART0") == b"pq"


def test_te_preamble_delays_first_character():
    chip, _ = kchip({"reset": SPIN}, hz=10e6)
    u = chip.peripherals["UART0"]
    chip.bus.write(UART0_BASE + BDL, 1, 5)
    chip.bus.write(UART0_BASE + C2, 1, 0x08)
    t0 = chip.now_ns()
    chip.bus.write(UART0_BASE + D, 1, ord("z"))
    assert chip.uart_out("UART0") == b""                      # still shifting the idle preamble
    chip.run_for(u.char_ns() + 1000)
    ev = [e for e in chip.log.filter("uart") if e.dir == "tx"]
    assert len(ev) == 1 and abs(ev[0].t_ns - t0 - u.char_ns()) < 0.02 * u.char_ns()


def test_uart2_one_entry_overruns_after_one_byte():
    chip, u, w8, r8 = _fifo_chip(base=UART2_BASE, sbr=1, brfa=0)
    assert r8(PFIFO) == 0x88 and u.rx_depth() == 1            # RXFE set but RXFIFOSIZE = 1 entry
    u.feed(b"AB")
    chip.run_for(2 * u.char_ns() + 1000)
    assert r8(S1) & 0x28 == 0x28 and _drops(chip) == [("B", "overrun")] and r8(RCFIFO) == 1
    assert r8(D) == ord("A") and not r8(S1) & 0x28


def test_k22_chip_yaml_fifo_depths():
    from chipem.chipdesc import load_chip
    desc = load_chip("chips/mk22fn1m0vll12.yaml")
    depth = {p.name: (p.params or {}).get("fifo_depth", 1) for p in desc.peripherals if p.model == "kinetis.uart"}
    assert depth == {"UART0": 8, "UART1": 8, "UART2": 1, "UART3": 1, "UART4": 1}


# ---------------------------------------------------------------- frames, errors, breaks, wake-up (§49.3, §49.4)
from chipem.periph.uartwire import Frame  # noqa: E402

C1, S2, C3, MA1, ED = 2, 5, 6, 8, 0xC


def _rx(u, fr):
    u._rx_frame(fr, "host")                                   # the test chip has no pin table: host path


def test_parity_error_keeps_data_sets_pf_ed_and_err_irq():
    chip, u, w8, r8 = _fifo_chip(pfifo=0)
    w8(C1, 0x02)                                              # PE, even: 7 data bits + parity
    w8(C3, 0x01)                                              # PEIE
    assert u.frame() == "7E1" and u.char_bits() == 10
    _rx(u, Frame(0x41, 7, "O", baud=u.baud()))                # odd parity on the wire: PF
    s1 = r8(S1)
    assert s1 & 0x21 == 0x21 and r8(ED) & 0x40               # RDRF | PF, ED.PARITYE for this dataword
    assert u._err_line
    assert r8(D) == 0x41 | 0x80                               # D holds the received parity bit in bit 7 (§49.3.8)
    assert not r8(S1) & 0x01 and not u._err_line             # S1 (PF set) then D read clears PF
    ev = [e for e in chip.log.filter("uart") if e.dir == "rx"][-1]
    assert ev.pe == 1 and ev.byte == 0xC1


def test_wrong_baud_gives_framing_errors_and_fe_blocks_reception():
    chip, u, w8, r8 = _fifo_chip(pfifo=0)
    _rx(u, Frame(0x00, 8, baud=u.baud() / 2))                 # half the baud: the stop sample falls on data 0
    s1 = r8(S1)
    assert s1 & 0x22 == 0x22                                  # RDRF | FE
    _rx(u, Frame(0x55, 8, baud=u.baud()))                     # FE inhibits reception until cleared (§49.3.5)
    assert _drops(chip)[-1] == ("U", "framing_error")
    r8(D)                                                     # S1 read (above) then D: FE clears
    assert not r8(S1) & 0x02
    _rx(u, Frame(0x55, 8, baud=u.baud() * 1.03))              # 3 % fast: inside the sample-point tolerance
    assert r8(S1) & 0x27 == 0x20 and r8(D) == 0x55


def test_nine_bit_t8_r8_and_msbf():
    chip, u, w8, r8 = _fifo_chip(pfifo=0)
    w8(C1, 0x10)                                              # M: 9 data bits
    w8(C3, 0x40)                                              # T8
    got = []
    u.frame_listeners.append(lambda blk, fr: got.append(fr))
    w8(D, 0x5A)
    assert got[-1].data == 0x15A and got[-1].bits == 9
    _rx(u, got[-1])
    assert r8(C3) & 0x80 and r8(D) == 0x5A                    # R8 = ninth bit of the dataword in D
    chip.run_for(2 * u.char_ns())
    w8(S2, 0x20)                                              # MSBF: bit order reversed on the wire
    w8(D, 0x03)                                               # word 0x103 (T8 still set)
    assert got[-1].msbf and got[-1].raw() == 0x181            # T8 (bit 8) first, then bit 7 ... bit 0
    chip.run_for(2 * u.char_ns())
    _rx(u, got[-1])                                           # MSB-first receiver: the same word
    assert r8(C3) & 0x80 and r8(D) == 0x03
    w8(S2, 0x00)
    _rx(u, got[-1])                                           # LSB-first receiver: the bits come out reversed
    assert r8(C3) & 0x80 and r8(D) == 0x81


def test_sbk_break_and_lin_break_detect():
    chip, u, w8, r8 = _fifo_chip(pfifo=0)
    got = []
    u.frame_listeners.append(lambda blk, fr: got.append(fr))
    w8(S2, 0x04)                                              # BRK13
    w8(C2, 0x0D)                                              # SBK set ...
    w8(C2, 0x0C)                                              # ... and cleared: one break character
    assert got[-1].brk == 13
    assert not r8(S1) & 0x40                                  # TC = 0 while the break is sent
    chip.run_for(3 * u.char_ns())
    assert len(got) == 1 and r8(S1) & 0x40
    assert any(e.get("brk") == 13 for e in chip.log.filter("uart") if e.dir == "tx")
    _rx(u, got[-1])                                           # LBKDE = 0: FE with an all-0 dataword
    assert r8(S1) & 0x22 == 0x22 and r8(D) == 0
    w8(BDH, 0x80)                                             # LBKDIE (SBR high bits stay 0)
    w8(BDL, 5)
    w8(S2, 0x02)                                              # LBKDE: 11-bit LIN break detection
    _rx(u, Frame.brk_frame(10, baud=u.baud()))
    chip.run_for(2 * u.char_ns())
    assert not r8(S2) & 0x80                                  # 10 bits: below the threshold
    _rx(u, Frame.brk_frame(13, baud=u.baud()))
    chip.run_for(2 * u.char_ns())
    assert r8(S2) & 0x80 and u._line and not r8(S1) & 0x22    # LBKDIF (status IRQ); no RDRF / FE
    w8(S2, 0x82)                                              # w1c
    assert not r8(S2) & 0x80


def test_address_mark_wakeup_and_match_address():
    chip, u, w8, r8 = _fifo_chip(pfifo=0)
    w8(C1, 0x18)                                              # M (9 bits), WAKE: address-mark wake-up
    w8(C2, 0x0E)                                              # RWU: standby
    b = u.baud()
    _rx(u, Frame(0x033, 9, baud=b))                           # data frame: ignored, no flags
    assert not r8(S1) & 0x20 and _drops(chip)[-1][1] == "mute"
    _rx(u, Frame(0x142, 9, baud=b))                           # address mark: wakes (RWU cleared), stored
    assert not r8(C2) & 0x02 and r8(S1) & 0x20 and r8(C3) & 0x80 and r8(D) == 0x42
    # match address: only the block addressed to MA1 is received
    w8(0xA, 0x80 | 13)                                        # C4: MAEN1 (BRFA kept)
    w8(MA1, 0x42)
    for v in (0x143, 0x011, 0x142, 0x012):
        _rx(u, Frame(v, 9, baud=b))
        if r8(S1) & 0x20:
            r8(D)
    assert [r for _, r in _drops(chip)[-2:]] == ["address_mismatch", "address_mismatch"]
    assert [e.byte for e in chip.log.filter("uart") if e.dir == "rx"][-2:] == [0x42, 0x12]


def test_idle_flag_and_idle_line_wakeup():
    chip, u, w8, r8 = _fifo_chip(pfifo=0)
    w8(C2, 0x1C)                                              # ILIE
    u.feed(b"a")
    chip.run_for(u.char_ns() + 100)
    assert r8(S1) & 0x20 and not r8(S1) & 0x10
    chip.run_for(2 * u.char_ns())                             # 10 idle bits after the start bit (ILT = 0), flagged
    assert r8(S1) & 0x10 and u._line                          # at most one character later: IDLE, status IRQ
    r8(D)                                                     # S1 then D: IDLE (and RDRF) clear
    assert not r8(S1) & 0x30
    chip.run_for(3 * u.char_ns())
    assert not r8(S1) & 0x10                                  # IDLE needs a new frame first
    w8(C2, 0x0E)                                              # RWU, WAKE = 0: wake on an idle line
    u.feed(b"bc")                                             # back-to-back: no idle between them: both ignored
    chip.run_for(2 * u.char_ns() + 100)
    assert [r for _, r in _drops(chip)[-2:]] == ["mute", "mute"]
    chip.run_for(2.5 * u.char_ns())                           # idle character: RWU clears, IDLE stays 0 (RWUID = 0)
    assert not r8(C2) & 0x02 and not r8(S1) & 0x10
    u.feed(b"d")
    chip.run_for(u.char_ns() + 100)
    assert r8(D) == ord("d")


def test_txinv_rxinv_polarity():
    chip, u, w8, r8 = _fifo_chip(pfifo=0)
    got = []
    u.frame_listeners.append(lambda blk, fr: got.append(fr))
    w8(C3, 0x10)                                              # TXINV
    w8(D, 0x41)
    assert got[-1].inv
    _rx(u, got[-1])                                           # RXINV = 0: the inverted line idles low: garbage
    chip.run_for(4 * u.char_ns())
    assert not [e for e in chip.log.filter("uart") if e.dir == "rx" and e.byte == 0x41]
    if r8(S1) & 0x20:
        r8(D)
    w8(S2, 0x10)                                              # RXINV
    _rx(u, Frame(0x41, baud=u.baud(), inv=True))
    chip.run_for(u.char_ns())
    assert [e.byte for e in chip.log.filter("uart") if e.dir == "rx"][-1] == 0x41
