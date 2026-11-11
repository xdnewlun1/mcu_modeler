"""STM32F4 USART behaviour checked against RM0090 ch. 30 (stm32.usart): TDR + shift register (TXE set as soon as a
character moves to the shift register), the TC clear sequence, the idle preamble after TE, break lengths, LIN break
detection length, IDLE, mute-mode details and OVER8 in LIN mode."""
import pytest

from test_stm32 import GPIOA, USART1, desc, rd, spin_chip, wr  # noqa: F401  (desc: fixture)

SR, DR, BRR, CR1, CR2, CR3 = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14
TXE, TC, RXNE, IDLE, LBD, FE = 0x80, 0x40, 0x20, 0x10, 1 << 8, 2
UE, M, WAKE, TE, RE, RWU, SBK, OVER8 = 1 << 13, 1 << 12, 1 << 11, 1 << 3, 1 << 2, 1 << 1, 1, 1 << 15
LINEN, LBDL = 1 << 14, 1 << 5


def _usart(desc, cr1=UE | TE | RE, preamble=True):
    chip = spin_chip(desc)
    wr(chip, 0x40023830, rd(chip, 0x40023830) | 1)                    # GPIOA clock
    wr(chip, 0x40023844, rd(chip, 0x40023844) | 1 << 4)               # USART1 clock
    wr(chip, GPIOA + 0x24, 7 << 8)
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 2 << 20)                  # PA10 = USART1_RX
    u = chip.peripherals["USART1"]
    wr(chip, USART1 + BRR, 139)                                         # 16 MHz / 139
    wr(chip, USART1 + CR1, cr1)
    if preamble:
        chip.run_for(u.char_ns() + 1)
    return chip, u


def test_txe_set_when_data_moves_to_shift_register(desc):
    chip, u = _usart(desc)
    rd(chip, USART1 + SR)
    wr(chip, USART1 + DR, 0x41)                                         # idle: straight to the shift register
    assert rd(chip, USART1 + SR) & (TXE | TC) == TXE and bytes(u.output) == b"A"
    wr(chip, USART1 + DR, 0x42)                                         # TDR full until 'A' has been sent
    assert rd(chip, USART1 + SR) & (TXE | TC) == 0 and bytes(u.output) == b"A"
    chip.run_for(u.char_ns() + 100)
    assert bytes(u.output) == b"AB" and rd(chip, USART1 + SR) & (TXE | TC) == TXE    # B shifting, TDR empty
    chip.run_for(u.char_ns())
    assert rd(chip, USART1 + SR) & (TXE | TC) == TXE | TC
    tx = [e.t_ns for e in chip.log if e.kind == "uart" and e.get("dir") == "tx"]
    assert tx[1] - tx[0] == pytest.approx(u.char_ns(), abs=10)


def test_tc_cleared_only_by_sr_read_then_dr_write_or_writing_zero(desc):
    chip, u = _usart(desc)
    assert rd(chip, USART1 + SR) & TC                                   # reset value 0x00C0
    wr(chip, USART1 + DR, 0x41)                                         # SR read before: TC cleared
    assert not u.get("SR") & TC
    chip.run_for(u.char_ns() + 100)
    assert u.get("SR") & TC
    # a DR write without a preceding SR read leaves TC set (RM0090 30.6.1: "a read from the USART_SR register
    # followed by a write to the USART_DR register"); u.get() looks at SR without a bus read
    wr(chip, USART1 + DR, 0x42)
    assert u.get("SR") & TC and bytes(u.output) == b"AB"
    chip.run_for(u.char_ns() + 100)
    rd(chip, USART1 + SR)
    wr(chip, USART1 + DR, 0x43)
    assert not u.get("SR") & TC
    chip.run_for(u.char_ns() + 100)
    assert u.get("SR") & TC
    wr(chip, USART1 + SR, 0x3FF & ~TC)                                  # rc_w0
    assert not rd(chip, USART1 + SR) & TC


def test_te_sends_an_idle_preamble_before_the_first_character(desc):
    chip, u = _usart(desc, preamble=False)
    t0 = chip.now_ns()
    rd(chip, USART1 + SR)
    wr(chip, USART1 + DR, 0x55)
    assert bytes(u.output) == b"" and not rd(chip, USART1 + SR) & TXE   # waits in TDR behind the idle frame
    chip.run_for(u.char_ns() + 100)
    tx = [e.t_ns for e in chip.log if e.kind == "uart" and e.get("dir") == "tx"]
    assert bytes(u.output) == b"U" and tx[0] - t0 == pytest.approx(u.char_ns(), abs=10)
    # a TE pulse (0 then 1) while 'U' is shifting sends another preamble after it (CR1.TE note)
    wr(chip, USART1 + CR1, UE | RE)
    wr(chip, USART1 + CR1, UE | TE | RE)
    chip.run_for(u.char_ns() + 100)
    rd(chip, USART1 + SR)
    wr(chip, USART1 + DR, 0x56)
    assert bytes(u.output) == b"U"                                      # behind the second preamble
    chip.run_for(u.char_ns() + 100)
    assert bytes(u.output) == b"UV"


@pytest.mark.parametrize("cr1_m, cr2, low", [(0, 0, 10), (M, 0, 11), (0, LINEN, 13)])
def test_break_length(desc, cr1_m, cr2, low):
    chip, u = _usart(desc, cr1=UE | TE | RE | cr1_m)
    wr(chip, USART1 + CR2, cr2)
    bit = u.char_ns() / u.char_bits()
    wr(chip, USART1 + CR1, UE | TE | RE | cr1_m | SBK)
    chip.run_for((low - 0.5) * bit)
    assert rd(chip, USART1 + CR1) & SBK
    chip.run_for(bit)
    assert not rd(chip, USART1 + CR1) & SBK                             # reset during the stop bit of the break
    assert u.break_low_bits() == low


def test_lin_break_detection_length_lbdl(desc):
    chip, u = _usart(desc)
    wr(chip, USART1 + CR2, LINEN | LBDL)                                # 11-bit detection
    u.receive_break(10)
    sr = rd(chip, USART1 + SR)
    assert sr & (FE | RXNE) == FE | RXNE and not sr & LBD               # framing error only
    rd(chip, USART1 + DR)
    u.receive_break(11)
    assert rd(chip, USART1 + SR) & LBD
    wr(chip, USART1 + SR, 0x3FF & ~LBD)
    wr(chip, USART1 + CR2, LINEN)                                       # 10-bit detection
    rd(chip, USART1 + DR)
    u.receive_break(10)
    assert rd(chip, USART1 + SR) & LBD


def test_idle_not_cleared_by_a_new_character(desc):
    chip, u = _usart(desc)
    u.feed(b"a")
    chip.run_for(2 * u.char_ns() + 100)
    assert rd(chip, USART1 + SR) & (IDLE | RXNE) == IDLE | RXNE
    u.feed(b"b")
    chip.run_for(u.char_ns() + 100)                                     # overrun, IDLE still set (needs SR + DR)
    assert rd(chip, USART1 + SR) & IDLE
    rd(chip, USART1 + DR)
    assert not rd(chip, USART1 + SR) & IDLE


def test_rwu_not_writable_while_rxne_in_address_mark_mode(desc):
    chip, u = _usart(desc, cr1=UE | TE | RE | WAKE)
    u.receive(b"\x01")
    assert rd(chip, USART1 + SR) & RXNE
    wr(chip, USART1 + CR1, UE | TE | RE | WAKE | RWU)
    assert not rd(chip, USART1 + CR1) & RWU                             # ignored (RM0090 30.3.6)
    rd(chip, USART1 + DR)
    wr(chip, USART1 + CR1, UE | TE | RE | WAKE | RWU)
    assert rd(chip, USART1 + CR1) & RWU


def test_idle_line_wakeup_needs_idle_detection_after_mute(desc):
    chip, u = _usart(desc)
    ct = u.char_ns()
    u.feed(b"x")
    chip.run_for(4 * ct)                                                # idle already detected (IDLE set)
    rd(chip, USART1 + SR)
    rd(chip, USART1 + DR)
    wr(chip, USART1 + CR1, UE | TE | RE | RWU)                          # mute after the idle detection
    chip.run_for(2 * ct)
    u.feed(b"a")                                                        # no idle frame seen since RWU: stays muted
    chip.run_for(ct + 100)
    assert not rd(chip, USART1 + SR) & RXNE and rd(chip, USART1 + CR1) & RWU
    chip.run_for(2 * ct)
    u.feed(b"b")                                                        # idle frame after 'a': wakes, 'b' received
    chip.run_for(ct + 100)
    assert rd(chip, USART1 + DR) == ord("b") and not rd(chip, USART1 + CR1) & RWU


def test_over8_forced_to_zero_in_lin_mode(desc):
    chip, u = _usart(desc, cr1=UE | TE | RE | OVER8)
    assert u.baud() == pytest.approx(16e6 / (8 * (8 + 3 / 8)))         # BRR 139 = 0x8B: OVER8 ignores bit 3
    wr(chip, USART1 + CR2, LINEN)
    assert u.baud() == pytest.approx(16e6 / 139)                        # OVER8 forced to 0 (30.6.4)


# ---------------------------------------------------------------- frames: parity / noise / framing / breaks (30.3.3)
from chipem.periph.uartwire import Frame  # noqa: E402

PE_, NF_, PCE, PS, PEIE = 1, 4, 1 << 10, 1 << 9, 1 << 8
ONEBIT = 1 << 11


def _sr_dr(chip):
    sr = rd(chip, USART1 + SR)
    return sr, rd(chip, USART1 + DR)


def test_parity_error_with_data_and_parity_bit_in_msb(desc):
    chip, u = _usart(desc, cr1=UE | TE | RE | PCE | PEIE)          # 7 data bits + even parity
    assert u.frame() == "7E1"
    u._rx_frame(Frame(0x41, 7, "O", baud=u.baud()), "host")         # odd parity bit on the wire
    sr = rd(chip, USART1 + SR)
    assert sr & (PE_ | RXNE) == PE_ | RXNE and (16 + 37) in chip.arch.asserted     # PEIE
    assert rd(chip, USART1 + DR) == 0x41 | 0x80                     # received parity bit in the MSB
    assert not rd(chip, USART1 + SR) & PE_                          # SR then DR read clears PE
    u._rx_frame(Frame(0x41, 7, "E", baud=u.baud()), "host")
    sr, dr = _sr_dr(chip)
    assert sr & PE_ == 0 and dr == 0x41
    ev = [e for e in chip.log if e.kind == "uart" and e.get("dir") == "rx"]
    assert ev[0].get("pe") == 1 and not ev[1].get("pe")


def test_noise_framing_and_tolerance_by_sample_points(desc):
    chip, u = _usart(desc)
    b = u.baud()
    u._rx_frame(Frame(0x55, baud=b * 1.03), "host")                 # 3 %: every sample inside its bit
    sr, dr = _sr_dr(chip)
    assert sr & (NF_ | FE) == 0 and dr == 0x55
    u._rx_frame(Frame(0x00, baud=b * 1.08), "host")                 # 8 % fast: bit 7 is sampled in the stop
    chip.run_for(2 * u.char_ns())                                   # bit, the stop bit on the idle line
    sr, dr = _sr_dr(chip)
    assert dr == 0x80 and not sr & FE
    u._rx_frame(Frame(0x00, baud=b / 1.08), "host")                 # 8 % slow: the stop sample hits data bit 7
    chip.run_for(2 * u.char_ns())
    sr, dr = _sr_dr(chip)
    assert sr & FE and dr == 0x00
    # a sample triple straddling a bit edge: noise flag (3 samples disagree), majority value kept
    u._rx_frame(Frame(0x0F, baud=b / 1.06), "host")
    chip.run_for(2 * u.char_ns())
    sr, dr = _sr_dr(chip)
    assert sr & NF_
    wr(chip, USART1 + CR3, ONEBIT)                                  # one sample per bit: no noise detection
    u._rx_frame(Frame(0x0F, baud=b / 1.06), "host")
    chip.run_for(2 * u.char_ns())
    sr, dr = _sr_dr(chip)
    assert not sr & NF_


def test_nine_bit_word_and_wrong_word_length(desc):
    chip, u = _usart(desc, cr1=UE | TE | RE | M)
    u._rx_frame(Frame(0x1A5, 9, baud=u.baud()), "host")
    sr, dr = _sr_dr(chip)
    assert dr == 0x1A5 and not sr & FE
    wr(chip, USART1 + CR1, UE | TE | RE)                              # 8-bit receiver, 9-bit frame: bit 8 is
    u._rx_frame(Frame(0x0A5, 9, baud=u.baud()), "host")              # sampled as the stop bit: 0 -> FE
    chip.run_for(2 * u.char_ns())
    sr, dr = _sr_dr(chip)
    assert sr & FE and dr == 0xA5


def test_kinetis_break_reaches_stm32_lin_detection(desc):
    """A K22 UART break (S2.BRK13 = 1: 13 low bits) on a link to a USART in LIN mode: FE + 0x00 and LBD."""
    chip, u = _usart(desc)
    wr(chip, USART1 + CR2, LINEN)
    u.receive_frame(Frame.brk_frame(13, 1.0, u.baud()))             # the pin is muxed: a link delivery
    chip.run_for(u.char_ns())
    sr, dr = _sr_dr(chip)
    assert sr & (LBD | FE | RXNE) == LBD | FE | RXNE and dr == 0
    ev = [e for e in chip.log if e.kind == "uart" and e.get("dir") == "rx"][-1]
    assert ev.get("brk") >= 13 and ev.get("fe") == 1
