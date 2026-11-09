"""STM32F4 timer inputs and advanced outputs (chipem/periph/stm32/tim.py): input capture from pin edges (direct /
indirect inputs, edge polarity, prescaler, overcapture, CCRx read clears CCxIF), PWM input mode with slave reset,
encoder interface, TIM1/8 dead time and break, TIM9-14 and 32-bit TIM2.  16 MHz HSI, APB /1: one timer tick = one
core cycle (PSC=0), so every expected value below is an exact cycle count."""
import pytest

from chipem.board import Board
from test_stm32 import GPIOA, GPIOB, RCC, TIM2, TIM3, desc, rd, spin_chip, wr  # noqa: F401  (desc is a fixture)

TIM1, TIM9, TIM10, TIM12 = 0x40010000, 0x40014000, 0x40014400, 0x40001800
GPIOE = 0x40021000
CR1, CR2, SMCR, DIER, SR, EGR, CCMR1, CCER, CNT, PSC, ARR = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x20, 0x24, \
    0x28, 0x2C
CCR1, CCR2, CCR3, BDTR = 0x34, 0x38, 0x3C, 0x44
CEN, PWM1 = 1, 6 << 4
MOE, AOE, BKP, BKE, OSSI = 1 << 15, 1 << 14, 1 << 13, 1 << 12, 1 << 10
TIM3_IRQ, TIM1_BRK_TIM9, TIM1_UP_TIM10, TIM1_CC = 29, 24, 25, 27


def _setup(desc, port_af: dict[tuple[int, int], int], hz=16e6):
    """16 MHz, GPIOA/B/E + TIM1/2/3/9/10/12 clocked; pins {(port base, bit): AF} in alternate-function mode."""
    chip = spin_chip(desc, clock_hz=hz)
    wr(chip, RCC + 0x30, 0b10011)                            # AHB1ENR: GPIOA, GPIOB, GPIOE
    wr(chip, RCC + 0x40, 0b1000011)                          # APB1ENR: TIM2, TIM3, TIM12
    wr(chip, RCC + 0x44, 1 | (1 << 16) | (1 << 17))          # APB2ENR: TIM1, TIM9, TIM10
    ports: dict[int, list[int]] = {}
    for (port, bit), af in port_af.items():
        m, a = ports.setdefault(port, [rd(chip, port), [rd(chip, port + 0x20), rd(chip, port + 0x24)]])[0:2]
        ports[port] = [m | (2 << (2 * bit)), a]
        a[bit // 8] |= af << (4 * (bit % 8))
    for port, (m, a) in ports.items():
        wr(chip, port + 0x20, a[0])
        wr(chip, port + 0x24, a[1])
        wr(chip, port, m)
    return chip


def _at(chip, t0, cyc, pin, lvl):
    chip.sched.at_cycles(t0 + cyc, chip.pins.set_external, pin, lvl)


def _edges(chip, pin):
    out = []
    chip.pins.subscribe(lambda ev: ev.pin == pin and ev.field == "level" and out.append(
        (round(ev.t_ns * chip.clock.hz / 1e9), ev.new)))
    return out


def _start(chip, base, cr1=CEN):
    t0 = chip.now_cycles()
    wr(chip, base + CR1, cr1)
    return t0


def _capture_chip(desc):
    """TIM3 CH1 on PA6 as TI1 input capture (CC1S=01), CC1E, CC1IE, free-running 16-bit counter."""
    chip = _setup(desc, {(GPIOA, 6): 2})
    chip.pins.set_external("PA6", "L")
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + CCMR1, 1)
    wr(chip, TIM3 + CCER, 1)
    wr(chip, TIM3 + DIER, 2)
    return chip


# ------------------------------------------------------------------------------------------------ input capture
def test_capture_rising_edge_latches_cnt_and_overcapture(desc):
    chip = _capture_chip(desc)
    t0 = _start(chip, TIM3)
    _at(chip, t0, 123, "PA6", "H")
    _at(chip, t0, 300, "PA6", "L")                           # falling: not an active edge
    chip.run_cycles(310)
    assert rd(chip, TIM3 + SR) & 0x202 == 0x002 and (16 + TIM3_IRQ) in chip.arch.asserted
    assert chip.peripherals["TIM3"].get("CCR1") == 123
    _at(chip, chip.now_cycles(), 190, "PA6", "H")            # cycle 500, CC1IF still set: CC1OF
    chip.run_cycles(200)
    assert chip.peripherals["TIM3"].get("CCR1") == 500 and rd(chip, TIM3 + SR) & 0x202 == 0x202


def test_reading_ccr_clears_ccif_but_not_ccof(desc):
    """RM0090 18.4.5: CC1IF (input) is cleared by software or by reading CCR1; CC1OF only by software."""
    chip = _capture_chip(desc)
    t0 = _start(chip, TIM3)
    _at(chip, t0, 10, "PA6", "H")
    _at(chip, t0, 20, "PA6", "L")
    _at(chip, t0, 30, "PA6", "H")
    chip.run_cycles(40)
    assert rd(chip, TIM3 + SR) & 0x202 == 0x202
    assert rd(chip, TIM3 + CCR1) == 30
    assert rd(chip, TIM3 + SR) & 0x202 == 0x200 and (16 + TIM3_IRQ) not in chip.arch.asserted
    wr(chip, TIM3 + SR, ~0x200 & 0xFFFF)
    assert rd(chip, TIM3 + SR) & 0x202 == 0
    wr(chip, TIM3 + CCR1, 7)                                 # read-only in input mode
    assert rd(chip, TIM3 + CCR1) == 30


@pytest.mark.parametrize("pol,caps", [(0, [100, 300]), (2, [200, 400]), (0xA, [100, 200, 300, 400]),
                                      (8, [100, 300])])
def test_capture_edge_polarity(desc, pol, caps):
    """CC1NP:CC1P = 00 rising, 01 falling, 11 both edges (10 is reserved: rising)."""
    chip = _capture_chip(desc)
    wr(chip, TIM3 + CCER, 1 | pol)
    seen = []
    t0 = _start(chip, TIM3)
    for k, c in enumerate((100, 200, 300, 400)):
        _at(chip, t0, c, "PA6", "H" if k % 2 == 0 else "L")
        chip.sched.at_cycles(t0 + c, lambda: rd(chip, TIM3 + SR) & 2 and seen.append(rd(chip, TIM3 + CCR1)))
    chip.run_cycles(450)
    assert seen == caps


@pytest.mark.parametrize("psc", [0, 1, 2, 3])
def test_capture_prescaler(desc, psc):
    """IC1PSC: a capture every 1, 2, 4 or 8 active edges (rising edges every 20 cycles here)."""
    chip = _capture_chip(desc)
    wr(chip, TIM3 + CCMR1, 1 | (psc << 2))
    caps = []
    t0 = _start(chip, TIM3)
    for k in range(16):
        _at(chip, t0, 20 * k + 10, "PA6", "H")
        _at(chip, t0, 20 * k + 20, "PA6", "L")
        chip.sched.at_cycles(t0 + 20 * k + 10, lambda: rd(chip, TIM3 + SR) & 2 and caps.append(rd(chip, TIM3 + CCR1)))
    chip.run_cycles(340)
    n = 1 << psc
    assert caps == [20 * k + 10 for k in range(16) if (k + 1) % n == 0]


def test_capture_counts_with_prescaled_counter_and_ccxg(desc):
    """PSC=9: CNT = elapsed cycles // 10 at the edge; EGR.CC1G on an input channel captures CNT too."""
    chip = _capture_chip(desc)
    wr(chip, TIM3 + PSC, 9)
    wr(chip, TIM3 + EGR, 1)
    t0 = _start(chip, TIM3)
    _at(chip, t0, 1237, "PA6", "H")
    chip.run_cycles(1300)
    assert rd(chip, TIM3 + CCR1) == 123
    chip.run_cycles(700)                                     # cycle 2000
    wr(chip, TIM3 + EGR, 2)
    assert rd(chip, TIM3 + SR) & 2 and rd(chip, TIM3 + CCR1) == 200


def test_indirect_inputs_and_pwm_input_mode(desc):
    """PWM input (RM0090 18.3.6): IC1 = TI1 rising (CC1S=01), IC2 = TI1 falling (CC2S=10, CC2P), slave reset mode
    on TI1FP1 (SMS=100, TS=101): CCR1 = period, CCR2 = high time, TIF set; CNT restarts at each rising edge."""
    chip = _capture_chip(desc)
    wr(chip, TIM3 + CCMR1, 1 | (2 << 8))
    wr(chip, TIM3 + CCER, 1 | 0x10 | 0x20)
    wr(chip, TIM3 + SMCR, 4 | (5 << 4))
    t0 = _start(chip, TIM3)
    for k in range(4):                                       # 30 % duty, 250-cycle period, from cycle 100
        _at(chip, t0, 100 + 250 * k, "PA6", "H")
        _at(chip, t0, 175 + 250 * k, "PA6", "L")
    chip.run_cycles(900)
    assert rd(chip, TIM3 + CCR1) == 250 and rd(chip, TIM3 + CCR2) == 75
    assert rd(chip, TIM3 + SR) & 0x40 and rd(chip, TIM3 + CNT) == 900 - 850
    assert not [e for e in chip.log if e.kind == "warn" and "TIM3" in str(e.fields.get("msg"))]


def test_cc4_captures_ti3_indirect(desc):
    """CC4S=10: IC4 from TI3 (TIM3_CH3 on PB0), CCR4 latches the edge."""
    chip = _setup(desc, {(GPIOB, 0): 2})
    chip.pins.set_external("PB0", "L")
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + 0x1C, 2 << 8)                            # CCMR2: CC4S=10
    wr(chip, TIM3 + CCER, 1 << 12)                           # CC4E
    t0 = _start(chip, TIM3)
    _at(chip, t0, 77, "PB0", "H")
    chip.run_cycles(100)
    assert rd(chip, TIM3 + SR) & 0x10 and rd(chip, TIM3 + 0x40) == 77


def test_capture_on_unmuxed_pin_does_nothing(desc):
    chip = _setup(desc, {})
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + CCMR1, 1)
    wr(chip, TIM3 + CCER, 1)
    t0 = _start(chip, TIM3)
    _at(chip, t0, 50, "PA6", "H")                            # PA6 is a GPIO input, not TIM3_CH1
    chip.run_cycles(100)
    assert not rd(chip, TIM3 + SR) & 2


def test_capture_from_another_chip_on_a_board_net(desc):
    """Chip a: TIM3 CH1 PWM on PA6 (period 200, high 50). Chip b: TIM3 CH1 captures the rising edges on its PA6,
    wired to a.PA6: successive captures are one PWM period apart."""
    a = _setup(desc, {(GPIOA, 6): 2})
    b = _capture_chip(desc)
    b.pins.set_external("PA6", None)
    board = Board({"a": a, "b": b})
    board.add_net("pwm", [("a", "PA6"), ("b", "PA6")])
    wr(a, TIM3 + ARR, 199)
    wr(a, TIM3 + CCR1, 50)
    wr(a, TIM3 + CCMR1, PWM1)
    wr(a, TIM3 + CCER, 1)
    wr(b, TIM3 + DIER, 0)
    wr(b, TIM3 + CR1, CEN)
    wr(a, TIM3 + CR1, CEN)
    caps = []
    for _ in range(6):
        board.run_for(200 / 16e6 * 1e9)
        if rd(b, TIM3 + SR) & 2:
            caps.append(rd(b, TIM3 + CCR1))
    assert len(caps) >= 4 and all(y - x == 200 for x, y in zip(caps, caps[1:])), caps


def test_capture_from_wall_clock_stimulus(desc):
    """A level scheduled in ns (what Session.schedule_pin / --at TIME:PA6=1 do) captures at its exact cycle."""
    chip = _capture_chip(desc)
    t0 = _start(chip, TIM3)
    chip.sched.at_ns(chip.clock.cycles_to_ns(t0) + 10_000, chip.pins.set_external, "PA6", "1")   # 10 us
    chip.run_cycles(200)
    assert rd(chip, TIM3 + CCR1) == 160


# ------------------------------------------------------------------------------------------------ encoder
def _quad(chip, t0, steps, start=0, period=10):
    """Quadrature on PA6 (TI1) / PA7 (TI2): +1 steps = TI1 leads TI2, -1 = TI2 leads. Returns the end cycle."""
    seq = [("L", "L"), ("H", "L"), ("H", "H"), ("L", "H")]
    st = start
    c = 0
    for d in steps:
        nst = (st + d) % 4
        for k, pin in ((0, "PA6"), (1, "PA7")):
            if seq[nst][k] != seq[st][k]:
                c += period
                _at(chip, t0, c, pin, seq[nst][k])
        st = nst
    return c


def test_encoder_mode_counts_both_edges_up_and_down(desc):
    chip = _setup(desc, {(GPIOA, 6): 2, (GPIOA, 7): 2})
    chip.pins.set_external("PA6", "L")
    chip.pins.set_external("PA7", "L")
    wr(chip, TIM3 + ARR, 9)
    wr(chip, TIM3 + CCMR1, 1 | (1 << 8))
    wr(chip, TIM3 + SMCR, 3)                                 # SMS=011: TI1 and TI2 edges
    wr(chip, TIM3 + DIER, 1)
    t0 = _start(chip, TIM3)
    end = _quad(chip, t0, [1] * 7)
    chip.run_cycles(end + 5)
    assert rd(chip, TIM3 + CNT) == 7 and not rd(chip, TIM3 + CR1) & 0x10
    chip.run_cycles(1000)
    assert rd(chip, TIM3 + CNT) == 7                         # no internal clock in encoder mode
    t1 = chip.now_cycles()
    end = _quad(chip, t1, [-1] * 9, start=7 % 4)
    chip.run_cycles(end + 5)
    assert rd(chip, TIM3 + CNT) == 8 and rd(chip, TIM3 + CR1) & 0x10           # 7 - 9 wraps through 0 -> ARR=9
    assert rd(chip, TIM3 + SR) & 1 and (16 + TIM3_IRQ) in chip.arch.asserted


@pytest.mark.parametrize("sms,count", [(1, 4), (2, 4), (3, 8)])
def test_encoder_mode_ti1_or_ti2_only(desc, sms, count):
    chip = _setup(desc, {(GPIOA, 6): 2, (GPIOA, 7): 2})
    chip.pins.set_external("PA6", "L")
    chip.pins.set_external("PA7", "L")
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + SMCR, sms)
    t0 = _start(chip, TIM3)
    end = _quad(chip, t0, [1] * 8)
    chip.run_cycles(end + 5)
    assert rd(chip, TIM3 + CNT) == count


def test_encoder_polarity_inverts_direction(desc):
    chip = _setup(desc, {(GPIOA, 6): 2, (GPIOA, 7): 2})
    chip.pins.set_external("PA6", "L")
    chip.pins.set_external("PA7", "L")
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + CNT, 100)
    wr(chip, TIM3 + CCER, 2)                                 # CC1P: TI1FP1 inverted
    wr(chip, TIM3 + SMCR, 3)
    t0 = _start(chip, TIM3)
    end = _quad(chip, t0, [1] * 4)
    chip.run_cycles(end + 5)
    assert rd(chip, TIM3 + CNT) == 96


# ------------------------------------------------------------------------------------------------ dead time
def _tim1_pair(desc, dtg, ccr=40, ccer=1 | 4, cr1=0):
    chip = _setup(desc, {(GPIOA, 8): 1, (GPIOA, 7): 1})     # PA8 = TIM1_CH1, PA7 = TIM1_CH1N
    wr(chip, TIM1 + ARR, 99)
    wr(chip, TIM1 + CCR1, ccr)
    wr(chip, TIM1 + CCMR1, PWM1)
    wr(chip, TIM1 + CCER, ccer)
    wr(chip, TIM1 + CR1, cr1)
    wr(chip, TIM1 + BDTR, MOE | dtg)
    return chip


def test_dead_time_delays_rising_edges(desc):
    chip = _tim1_pair(desc, 10)
    assert chip.pins.get("PA8").level() == "H" and chip.pins.get("PA7").level() == "L"   # settled at enable
    a, b = _edges(chip, "PA8"), _edges(chip, "PA7")
    t0 = _start(chip, TIM1)
    chip.run_cycles(320)
    rel = lambda ed: [(c - t0, lvl) for c, lvl in ed]                         # noqa: E731
    assert rel(a) == [(40, "L"), (110, "H"), (140, "L"), (210, "H"), (240, "L"), (310, "H")]
    assert rel(b) == [(50, "H"), (100, "L"), (150, "H"), (200, "L"), (250, "H"), (300, "L")]


def test_dead_time_with_polarity(desc):
    """CC1P / CC1NP invert the outputs after the dead-time generator: the delayed edges become falling edges."""
    chip = _tim1_pair(desc, 10, ccer=1 | 2 | 4 | 8)
    a, b = _edges(chip, "PA8"), _edges(chip, "PA7")
    t0 = _start(chip, TIM1)
    chip.run_cycles(120)
    assert [(c - t0, v) for c, v in a] == [(40, "H"), (110, "L")]
    assert [(c - t0, v) for c, v in b] == [(50, "L"), (100, "H")]


def test_pulse_shorter_than_dead_time_is_suppressed(desc):
    chip = _tim1_pair(desc, 10, ccr=5)
    a, b = _edges(chip, "PA8"), _edges(chip, "PA7")
    t0 = _start(chip, TIM1)
    chip.run_cycles(220)
    assert [(c - t0, v) for c, v in a] == [(5, "L")]          # REF high for 5 cycles < 10: OC1 never rises again
    assert [(c - t0, v) for c, v in b] == [(15, "H"), (100, "L"), (115, "H"), (200, "L"), (215, "H")]


@pytest.mark.parametrize("dtg,ckd,cyc", [(0x7F, 0, 127), (0x85, 0, 138), (0xC3, 0, 280), (0xE1, 0, 528),
                                          (0x10, 1, 32), (0x10, 2, 64)])
def test_dead_time_encoding(desc, dtg, ckd, cyc):
    chip = _tim1_pair(desc, dtg, cr1=ckd << 8)
    assert chip.peripherals["TIM1"].ch[0].dt == cyc


def test_dead_time_needs_both_outputs(desc):
    """Only CC1E (or OSSR off-state for CH1N): no dead time, CH1 follows OC1REF directly."""
    chip = _tim1_pair(desc, 10, ccer=1)
    a = _edges(chip, "PA8")
    t0 = _start(chip, TIM1)
    chip.run_cycles(120)
    assert [(c - t0, v) for c, v in a] == [(40, "L"), (100, "H")]


# ------------------------------------------------------------------------------------------------ break
def _tim1_break(desc, bdtr):
    chip = _setup(desc, {(GPIOA, 8): 1, (GPIOA, 7): 1, (GPIOA, 6): 1})   # PA6 = TIM1_BKIN
    chip.pins.set_external("PA6", "L")
    wr(chip, TIM1 + ARR, 99)
    wr(chip, TIM1 + CCR1, 40)
    wr(chip, TIM1 + CCMR1, PWM1)
    wr(chip, TIM1 + CCER, 1 | 4)
    wr(chip, TIM1 + CR2, 1 << 8)                             # OIS1 = 1, OIS1N = 0
    wr(chip, TIM1 + DIER, 1 << 7)                            # BIE
    wr(chip, TIM1 + BDTR, bdtr)
    return chip


def test_break_input_clears_moe_and_drives_idle_levels(desc):
    chip = _tim1_break(desc, MOE | OSSI | BKE | BKP)        # active high
    pa8, pa7 = chip.pins.get("PA8"), chip.pins.get("PA7")
    t0 = _start(chip, TIM1)
    chip.run_cycles(50)
    assert pa8.level() == "L" and pa7.level() == "H"         # CNT=50 > CCR1: OC1REF = 0
    _at(chip, t0, 60, "PA6", "H")
    chip.run_cycles(20)
    assert not rd(chip, TIM1 + BDTR) & MOE and rd(chip, TIM1 + SR) & 0x80
    assert (16 + TIM1_BRK_TIM9) in chip.arch.asserted
    assert pa8.level() == "H" and pa7.level() == "L"         # OSSI=1: idle levels OIS1 / OIS1N
    wr(chip, TIM1 + BDTR, MOE | OSSI | BKE | BKP)            # MOE cannot be set while the break is active
    wr(chip, TIM1 + SR, 0)                                   # BIF cannot be cleared either
    assert not rd(chip, TIM1 + BDTR) & MOE and rd(chip, TIM1 + SR) & 0x80
    chip.pins.set_external("PA6", "L")
    wr(chip, TIM1 + SR, 0)
    assert not rd(chip, TIM1 + SR) & 0x80 and (16 + TIM1_BRK_TIM9) not in chip.arch.asserted
    wr(chip, TIM1 + BDTR, MOE | OSSI | BKE | BKP)
    assert rd(chip, TIM1 + BDTR) & MOE and pa8.direction == "out"


def test_break_ossi0_releases_and_aoe_restores_at_update(desc):
    chip = _tim1_break(desc, MOE | AOE | BKE)                # active low (BKP=0): PA6 is low -> break at once
    assert not rd(chip, TIM1 + BDTR) & MOE and rd(chip, TIM1 + SR) & 0x80
    assert "TIM1_CH1" not in chip.pins.signal_levels         # OSSI=0: outputs released
    chip.pins.set_external("PA6", "H")                       # break inactive
    wr(chip, TIM1 + SR, 0)
    a = _edges(chip, "PA8")
    t0 = _start(chip, TIM1)
    chip.run_cycles(99)
    assert not rd(chip, TIM1 + BDTR) & MOE
    chip.run_cycles(2)                                       # update event at cycle 100: AOE sets MOE
    assert rd(chip, TIM1 + BDTR) & MOE and [(c - t0, v) for c, v in a] == [(100, "H")]


def test_software_break_bg(desc):
    chip = _tim1_break(desc, MOE | OSSI)
    assert chip.pins.get("PA8").level() == "H"               # stopped at CNT=0 < 40
    wr(chip, TIM1 + CCMR1, 4 << 4)                           # force inactive: CH1 L, CH1N H
    assert chip.pins.get("PA8").level() == "L"
    wr(chip, TIM1 + EGR, 1 << 7)                             # BG
    assert not rd(chip, TIM1 + BDTR) & MOE and rd(chip, TIM1 + SR) & 0x80
    assert chip.pins.get("PA8").level() == "H" and chip.pins.get("PA7").level() == "L"
    wr(chip, TIM1 + BDTR, MOE | OSSI)                        # BKE=0: nothing holds the break
    assert rd(chip, TIM1 + BDTR) & MOE and chip.pins.get("PA8").level() == "L"


# ------------------------------------------------------------------------------------------------ TIM9-14
def test_tim9_registers_follow_the_svd(desc):
    chip = _setup(desc, {})
    t9 = chip.peripherals["TIM9"]
    assert t9.model == "stm32.tim" and len(t9.ch) == 2 and "CCR3" not in t9.regs and "BDTR" not in t9.regs
    assert len(chip.peripherals["TIM10"].ch) == 1 and len(chip.peripherals["TIM14"].ch) == 1
    wr(chip, TIM9 + ARR, 0x12345)
    assert rd(chip, TIM9 + ARR) == 0x2345                    # 16-bit
    wr(chip, TIM9 + CR1, 0x70)                               # DIR / CMS do not exist: read 0
    assert rd(chip, TIM9 + CR1) == 0
    assert not chip.bus.auto_stubs


def test_tim9_pwm_and_capture_on_its_two_channels(desc):
    """TIM9 (APB2) CH1 PWM on PE5, CH2 capture on PE6; the CC interrupt is on TIM1_BRK_TIM9."""
    chip = _setup(desc, {(GPIOE, 5): 3, (GPIOE, 6): 3})
    chip.pins.set_external("PE6", "L")
    wr(chip, TIM9 + ARR, 99)
    wr(chip, TIM9 + CCR1, 30)
    wr(chip, TIM9 + CCMR1, PWM1 | (1 << 8))                  # CC1 PWM1, CC2S=01
    wr(chip, TIM9 + CCER, 1 | 0x10)
    wr(chip, TIM9 + DIER, 4)                                 # CC2IE
    a = _edges(chip, "PE5")
    t0 = _start(chip, TIM9)
    _at(chip, t0, 155, "PE6", "H")
    chip.run_cycles(210)
    assert [(c - t0, v) for c, v in a] == [(30, "L"), (100, "H"), (130, "L"), (200, "H")]
    assert (16 + TIM1_BRK_TIM9) in chip.arch.asserted and rd(chip, TIM9 + CCR2) == 55


def test_tim10_and_tim1_share_the_update_vector(desc):
    chip = _setup(desc, {})
    for base in (TIM1, TIM10):
        wr(chip, base + ARR, 49)
        wr(chip, base + DIER, 1)
        wr(chip, base + CR1, CEN)
    chip.run_cycles(60)
    assert rd(chip, TIM1 + SR) & 1 and rd(chip, TIM10 + SR) & 1
    wr(chip, TIM1 + SR, 0)
    assert (16 + TIM1_UP_TIM10) in chip.arch.asserted        # TIM10 still requests it
    wr(chip, TIM10 + SR, 0)
    assert (16 + TIM1_UP_TIM10) not in chip.arch.asserted


def test_tim12_on_apb1_counts_and_interrupts(desc):
    chip = _setup(desc, {})
    wr(chip, TIM12 + ARR, 999)
    wr(chip, TIM12 + DIER, 1)
    t0 = _start(chip, TIM12)
    chip.run_cycles(999)
    assert not rd(chip, TIM12 + SR) & 1
    chip.run_cycles(2)
    assert rd(chip, TIM12 + SR) & 1 and (16 + 43) in chip.arch.asserted       # TIM8_BRK_TIM12
    assert chip.now_cycles() - t0 == 1001


# ------------------------------------------------------------------------------------------------ 32-bit TIM2
def test_tim2_32bit_pwm_above_65535(desc):
    chip = _setup(desc, {(GPIOA, 5): 1})                     # PA5 = TIM2_CH1_ETR
    wr(chip, TIM2 + ARR, 99_999)
    wr(chip, TIM2 + CCR1, 70_000)
    wr(chip, TIM2 + CCMR1, PWM1)
    wr(chip, TIM2 + CCER, 1)
    assert rd(chip, TIM2 + ARR) == 99_999 and rd(chip, TIM2 + CCR1) == 70_000
    a = _edges(chip, "PA5")
    t0 = _start(chip, TIM2)
    chip.run_cycles(170_010)
    assert [(c - t0, v) for c, v in a] == [(70_000, "L"), (100_000, "H"), (170_000, "L")]
    assert rd(chip, TIM2 + CNT) == 70_010
