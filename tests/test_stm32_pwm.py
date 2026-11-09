"""STM32F4 timer output compare / PWM (chipem/periph/stm32/tim.py): compare matches, OCxREF modes, polarity,
preload, center-aligned PWM, compare interrupts and the complementary outputs of TIM1, observed on the pins."""
import pytest

from test_stm32 import GPIOA, RCC, TIM3, desc, rd, spin_chip, wr  # noqa: F401  (desc is a fixture)

TIM1 = 0x40010000
CR1, DIER, SR, EGR, CCMR1, CCER, CNT, PSC, ARR, CCR1, BDTR = 0x00, 0x0C, 0x10, 0x14, 0x18, 0x20, 0x24, 0x28, 0x2C, 0x34, 0x44
PWM1, PWM2, TOGGLE = 6 << 4, 7 << 4, 3 << 4
CEN = 1
OC1PE = 1 << 3


def _setup(desc, pin_af: dict[int, int], hz=16e6):
    """16 MHz HSI, TIM3 + TIM1 + GPIOA clocked; PA pins (bit -> AF) in alternate-function mode."""
    chip = spin_chip(desc, clock_hz=hz)
    wr(chip, RCC + 0x30, 1)                                  # AHB1ENR.GPIOAEN
    wr(chip, RCC + 0x40, 1 << 1)                             # APB1ENR.TIM3EN
    wr(chip, RCC + 0x44, 1)                                  # APB2ENR.TIM1EN
    moder, afr = 0, [0, 0]
    for bit, af in pin_af.items():
        moder |= 2 << (2 * bit)
        afr[bit // 8] |= af << (4 * (bit % 8))               # AFRL pins 0-7, AFRH pins 8-15
    wr(chip, GPIOA + 0x00, moder)
    wr(chip, GPIOA + 0x20, afr[0])
    wr(chip, GPIOA + 0x24, afr[1])
    return chip


def _edges(chip, pin):
    out = []
    chip.pins.subscribe(lambda ev: ev.pin == pin and ev.field == "level" and out.append((round(chip.clock.cycles_to_ns(
        chip.clock.abs_ns_to_cycles(ev.t_ns)) * chip.clock.hz / 1e9), ev.new)))
    return out


def _cyc(edges, t0):
    return [(c - t0, lvl) for c, lvl in edges]


def test_pwm1_edge_aligned_on_pin(desc):
    chip = _setup(desc, {6: 2})                              # PA6 = TIM3_CH1
    wr(chip, TIM3 + PSC, 0)
    wr(chip, TIM3 + ARR, 99)                                 # period 100 ticks = 100 core cycles
    wr(chip, TIM3 + CCR1, 25)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)                                 # CC1E
    assert chip.pins.get("PA6").level() == "H"               # counter stopped at 0: PWM1 REF = (0 < 25)
    ed = _edges(chip, "PA6")
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, 1)
    chip.run_cycles(1000)
    rel = _cyc(ed, t0)
    highs = [c for c, l in rel if l == "H"]
    lows = [c for c, l in rel if l == "L"]
    assert len(lows) >= 9 and all(b - a == 100 for a, b in zip(lows, lows[1:]))     # 100-cycle period
    assert all(abs((l - h) - 25) <= 1 for h, l in zip(highs, [x for x in lows if x > highs[0]]))  # 25 % duty


def test_pwm_polarity_pwm2_and_stopped_counter_level(desc):
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 25)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    assert chip.pins.get("PA6").level() == "H"               # stopped at CNT=0: 0 < 25 -> REF=1
    wr(chip, TIM3 + CCER, 1 | 2)                              # CC1P: inverted
    assert chip.pins.get("PA6").level() == "L"
    wr(chip, TIM3 + CCMR1, PWM2)
    assert chip.pins.get("PA6").level() == "H"               # PWM2 inverted again
    wr(chip, TIM3 + CCER, 0)                                  # output disabled: the timer stops driving
    assert chip.pins.get("PA6").direction == "in" and chip.pins.get("PA6").level() == "X"


def test_compare_interrupt_and_toggle_mode(desc):
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 199)
    wr(chip, TIM3 + CCR1, 50)
    wr(chip, TIM3 + CCMR1, TOGGLE)
    wr(chip, TIM3 + CCER, 1)
    wr(chip, TIM3 + DIER, 1 << 1)                             # CC1IE
    ed = _edges(chip, "PA6")
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, 1)
    chip.run_cycles(60)
    assert rd(chip, TIM3 + SR) & 2 and (16 + 29) in chip.arch.asserted       # CC1IF -> TIM3 IRQ (29)
    wr(chip, TIM3 + SR, ~2 & 0xFFFF)
    assert (16 + 29) not in chip.arch.asserted
    chip.run_cycles(1000)
    rel = _cyc(ed, t0)
    assert [c for c, _l in rel][:3] == [50, 250, 450]        # one toggle per period, at CNT == CCR


def test_ccr_preload_takes_effect_at_update(desc):
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 25)
    wr(chip, TIM3 + CCMR1, PWM1 | OC1PE)
    wr(chip, TIM3 + EGR, 1)                                   # UG: load CCR1=25
    wr(chip, TIM3 + CCER, 1)
    ed = _edges(chip, "PA6")
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, 1)
    chip.run_cycles(130)                                      # into the second period
    wr(chip, TIM3 + CCR1, 75)                                 # preloaded: still 25 in this period
    chip.run_cycles(300)
    rel = _cyc(ed, t0)
    lows = [c for c, l in rel if l == "L"]
    assert 125 in [round(x) for x in lows] or any(abs(x - 125) <= 1 for x in lows)
    assert any(abs(x - 275) <= 1 for x in lows)               # third period uses 75


def test_center_aligned_pwm_is_symmetric(desc):
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 100)
    wr(chip, TIM3 + CCR1, 30)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    ed = _edges(chip, "PA6")
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, 1 | (1 << 5))                       # CMS=01
    chip.run_cycles(600)
    rel = _cyc(ed, t0)
    # period 200 ticks: high while CNT < 30 on the way up and CNT <= 30 on the way down
    lows = [c for c, l in rel if l == "L"]
    highs = [c for c, l in rel if l == "H"]
    assert any(abs(x - 30) <= 1 for x in lows) and any(abs(x - 170) <= 1 for x in highs)
    assert any(abs(x - 230) <= 1 for x in lows)


def test_tim1_complementary_outputs_need_moe(desc):
    chip = _setup(desc, {8: 1, 7: 1})                        # PA8 = TIM1_CH1, PA7 = TIM1_CH1N
    wr(chip, TIM1 + ARR, 99)
    wr(chip, TIM1 + CCR1, 40)
    wr(chip, TIM1 + CCMR1, PWM1)
    wr(chip, TIM1 + CCER, 1 | 4)                              # CC1E, CC1NE
    assert chip.pins.get("PA8").level() == "X" and chip.pins.get("PA7").level() == "X"   # MOE=0: not driven
    wr(chip, TIM1 + BDTR, 1 << 15)                            # MOE
    assert chip.pins.get("PA8").level() == "H" and chip.pins.get("PA7").level() == "L"   # complementary
    a, b = _edges(chip, "PA8"), _edges(chip, "PA7")
    wr(chip, TIM1 + CR1, 1)
    chip.run_cycles(500)
    assert len(a) >= 8 and [(c, "L" if l == "H" else "H") for c, l in a] == b          # always opposite


def test_pin_muxed_later_shows_the_live_level(desc):
    chip = _setup(desc, {})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 50)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)                                  # REF=1 with CNT stopped at 0
    wr(chip, GPIOA + 0x20, 2 << 24)
    wr(chip, GPIOA + 0x00, 2 << 12)                           # now PA6 -> AF2 (TIM3_CH1)
    assert chip.pins.get("PA6").owner == "TIM3_CH1" and chip.pins.get("PA6").level() == "H"


def test_polled_compare_flag_of_an_unused_channel(desc):
    """Frozen mode, no output, no interrupt (the reset state of channels 2-4): no events are scheduled for the
    channel, but CCxIF still sets when the counter passes CCR, as firmware that polls SR expects."""
    chip = _setup(desc, {})
    t3 = chip.peripherals["TIM3"]
    wr(chip, TIM3 + ARR, 999)
    wr(chip, TIM3 + CCR1, 400)
    wr(chip, TIM3 + CR1, 1)
    chip.run_cycles(390)
    assert not rd(chip, TIM3 + SR) & 2 and t3.ch[0].ev is None
    chip.run_cycles(20)
    assert rd(chip, TIM3 + SR) & 2                            # passed CNT == 400
    wr(chip, TIM3 + SR, ~2 & 0xFFFF)
    chip.run_cycles(300)
    assert not rd(chip, TIM3 + SR) & 2                        # once per period
    chip.run_cycles(1000)
    assert rd(chip, TIM3 + SR) & 3 == 3                       # next period: CC1IF again (and UIF)


# ------------------------------------------------------------------------------------------------ RM0090 corner cases
CR2, CCR2 = 0x04, 0x38
DIR, OPM, ARPE = 1 << 4, 1 << 3, 1 << 7


def _run_from(chip, pin, cr1):
    """Start TIM3 with CR1=`cr1`; the edges of `pin` relative to that cycle are collected in the returned list."""
    ed = _edges(chip, pin)
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, cr1)
    return ed, t0


def test_one_pulse_pwm2_single_pulse(desc):
    """OPM + PWM2: REF rises at CNT == CCR and the update event that clears CEN ends the pulse (CNT back to 0)."""
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 50)
    wr(chip, TIM3 + CCR1, 20)
    wr(chip, TIM3 + CCMR1, PWM2)
    wr(chip, TIM3 + CCER, 1)
    assert chip.pins.get("PA6").level() == "L"
    ed, t0 = _run_from(chip, "PA6", CEN | OPM)
    chip.run_cycles(500)
    assert _cyc(ed, t0) == [(20, "H"), (51, "L")]
    assert not rd(chip, TIM3 + CR1) & CEN and rd(chip, TIM3 + CNT) == 0


@pytest.mark.parametrize("cms", [1, 2, 3])
@pytest.mark.parametrize("output", [True, False])
def test_center_aligned_compare_flag_follows_cms(desc, cms, output):
    """CMS=01: CCxIF only on the down-counting match, 10: only up, 11: both; OCxREF changes on both matches. With
    the output disabled (no events, flags computed on SR reads) the same rule applies."""
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 100)
    wr(chip, TIM3 + CCR1, 30)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1 if output else 0)
    ed, t0 = _run_from(chip, "PA6", CEN | (cms << 5))
    chip.run_cycles(40)                                       # up-counting match at tick 30
    assert bool(rd(chip, TIM3 + SR) & 2) == (cms in (2, 3))
    wr(chip, TIM3 + SR, 0)
    chip.run_cycles(140)                                      # down-counting match at tick 170
    assert bool(rd(chip, TIM3 + SR) & 2) == (cms in (1, 3))
    if output:
        assert _cyc(ed, t0) == [(30, "L"), (170, "H")]


@pytest.mark.parametrize("action", ["arr", "cnt", "dir"])
def test_restart_in_the_match_tick_does_not_match_again(desc, action):
    """Toggle mode, PSC=99 (100 cycles per tick), CCR=5: the match at tick 5 (cycle 500) toggles once. Re-anchoring
    the counter during that tick (ARR write without preload, CNT write, DIR change) neither toggles again nor
    re-sets CC1IF, and keeps the prescaler phase."""
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + PSC, 99)
    wr(chip, TIM3 + EGR, 1)
    wr(chip, TIM3 + SR, 0)
    wr(chip, TIM3 + ARR, 199)
    wr(chip, TIM3 + CCR1, 5)
    wr(chip, TIM3 + CCMR1, TOGGLE)
    wr(chip, TIM3 + CCER, 1)
    wr(chip, TIM3 + DIER, 2)                                  # CC1IE: exact match events
    ed, t0 = _run_from(chip, "PA6", CEN)
    chip.run_cycles(520)
    assert _cyc(ed, t0) == [(500, "H")] and rd(chip, TIM3 + SR) & 2
    wr(chip, TIM3 + SR, 0)
    if action == "arr":
        wr(chip, TIM3 + ARR, 150)
    elif action == "cnt":
        wr(chip, TIM3 + CNT, 5)
    else:
        wr(chip, TIM3 + CR1, CEN | DIR)
    assert _cyc(ed, t0) == [(500, "H")] and not rd(chip, TIM3 + SR) & 2
    chip.run_cycles(1000)
    assert _cyc(ed, t0) == [(500, "H")] and not rd(chip, TIM3 + SR) & 2
    # ticks still at cycles 600, 700, ...: up 5 -> 15; down 5 -> 0, underflow at 1100 -> 199 -> 195
    assert rd(chip, TIM3 + CNT) == (15 if action != "dir" else 195)
    if action == "arr":
        chip.run_cycles(15600 - 1520 + 10)                    # 146 ticks to the wrap, then 5: cycle 15600
        assert _cyc(ed, t0) == [(500, "H"), (15600, "L")]


def test_switch_to_input_capture_releases_the_pin(desc):
    chip = _setup(desc, {6: 2})
    t3 = chip.peripherals["TIM3"]
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 50)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    ed, t0 = _run_from(chip, "PA6", CEN)
    chip.run_cycles(20)
    assert chip.pins.get("PA6").level() == "H" and chip.pins.signal_levels.get("TIM3_CH1") == "H"
    wr(chip, TIM3 + CCMR1, 1)                                 # CC1S=01: input capture
    assert "TIM3_CH1" not in chip.pins.signal_levels and t3.ch[0].out is None and t3.ch[0].ev is None
    pa6 = chip.pins.get("PA6")
    assert pa6.owner == "TIM3_CH1" and pa6.direction == "in" and pa6.level() == "X"     # an input, not driven
    chip.run_cycles(500)
    assert _cyc(ed, t0) == [(20, "X")]                        # the release, then nothing


def test_apb_clock_gate_off_freezes_counter_and_outputs(desc):
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 50)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    ed, t0 = _run_from(chip, "PA6", CEN)
    chip.run_cycles(130)
    wr(chip, RCC + 0x40, 0)                                   # APB1ENR.TIM3EN off at CNT=30
    chip.run_cycles(400)
    assert _cyc(ed, t0) == [(50, "L"), (100, "H")] and chip.pins.get("PA6").level() == "H"
    assert rd(chip, TIM3 + CNT) == 30 and rd(chip, TIM3 + CR1) & CEN
    wr(chip, RCC + 0x40, 1 << 1)                              # on again at cycle 530: resumes from CNT=30
    chip.run_cycles(160)
    assert _cyc(ed, t0) == [(50, "L"), (100, "H"), (550, "L"), (600, "H"), (650, "L")]


def test_ug_while_stopped_reevaluates_ref(desc):
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCMR1, PWM1 | OC1PE)
    wr(chip, TIM3 + CCER, 1)
    assert chip.pins.get("PA6").level() == "L"                # active CCR1=0: 0 %
    wr(chip, TIM3 + CCR1, 50)
    assert chip.pins.get("PA6").level() == "L"                # preloaded
    wr(chip, TIM3 + EGR, 1)                                   # UG: CCR1=50, CNT=0 -> REF = 0 < 50
    assert chip.pins.get("PA6").level() == "H"
    wr(chip, TIM3 + CR1, DIR)
    wr(chip, TIM3 + EGR, 1)                                   # down-counting: CNT=ARR=99 -> REF = 99 <= 50 = 0
    assert rd(chip, TIM3 + CNT) == 99 and chip.pins.get("PA6").level() == "L"


def test_down_counting_pwm1(desc):
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 25)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    wr(chip, TIM3 + CR1, DIR)
    wr(chip, TIM3 + EGR, 1)                                   # CNT = ARR
    assert chip.pins.get("PA6").level() == "L"
    ed, t0 = _run_from(chip, "PA6", CEN | DIR)
    chip.run_cycles(320)
    assert _cyc(ed, t0) == [(74, "H"), (100, "L"), (174, "H"), (200, "L"), (274, "H"), (300, "L")]


@pytest.mark.parametrize("ccr,level,flag", [(0, "L", True), (100, "H", False), (150, "H", False)])
def test_pwm1_ccr_zero_and_above_arr(desc, ccr, level, flag):
    """CCR > ARR: REF held at 1 and the counter never matches (no CCxIF); CCR = 0: REF held at 0, and CNT = 0 is
    a compare match (CCxIF set each period)."""
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, ccr)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    ed, t0 = _run_from(chip, "PA6", CEN)
    chip.run_cycles(450)
    assert ed == [] and chip.pins.get("PA6").level() == level
    assert bool(rd(chip, TIM3 + SR) & 2) == flag and rd(chip, TIM3 + SR) & 1


def test_arpe_pwm_with_psc_change_at_the_update_event(desc):
    """ARPE + OC1PE: ARR, CCR1 and PSC written mid-period take effect together at the next update event."""
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 50)
    wr(chip, TIM3 + CCMR1, PWM1 | OC1PE)
    wr(chip, TIM3 + CR1, ARPE)
    wr(chip, TIM3 + EGR, 1)
    wr(chip, TIM3 + CCER, 1)
    ed, t0 = _run_from(chip, "PA6", CEN | ARPE)
    chip.run_cycles(30)
    wr(chip, TIM3 + PSC, 1)                                   # 2 cycles per tick from the next period
    wr(chip, TIM3 + ARR, 49)
    wr(chip, TIM3 + CCR1, 25)
    chip.run_cycles(240)
    assert _cyc(ed, t0) == [(50, "L"), (100, "H"), (150, "L"), (200, "H"), (250, "L")]


def test_apb_prescaler_change_mid_period(desc):
    """PPRE1 /1 -> /4 at cycle 130 (CNT=30): the timer kernel clock drops to 2 x PCLK1 = core / 2."""
    chip = _setup(desc, {6: 2})
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 50)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    ed, t0 = _run_from(chip, "PA6", CEN)
    chip.run_cycles(130)
    wr(chip, RCC + 0x08, (rd(chip, RCC + 0x08) & ~(7 << 10)) | (5 << 10))
    chip.run_cycles(300)
    assert _cyc(ed, t0) == [(50, "L"), (100, "H"), (170, "L"), (270, "H"), (370, "L")]


def test_tim1_moe_off_state_and_idle_levels(desc):
    """RM0090 table 81: MOE=0, OSSI=0 releases both outputs; MOE=0, OSSI=1 drives the CR2.OIS1/OIS1N idle levels;
    MOE=1, OSSR=1 drives a disabled output of an active pair at its inactive level (CCxP)."""
    chip = _setup(desc, {8: 1, 7: 1})                        # PA8 = TIM1_CH1, PA7 = TIM1_CH1N
    lv = chip.pins.signal_levels
    wr(chip, TIM1 + ARR, 99)
    wr(chip, TIM1 + CCR1, 40)
    wr(chip, TIM1 + CCMR1, PWM1)                              # REF = 1 with the counter stopped at 0
    wr(chip, TIM1 + CCER, 1 | 4)
    assert "TIM1_CH1" not in lv and "TIM1_CH1N" not in lv
    wr(chip, TIM1 + CR2, 1 << 8)                              # OIS1=1, OIS1N=0
    wr(chip, TIM1 + BDTR, 1 << 10)                            # OSSI
    assert lv.get("TIM1_CH1") == "H" and lv.get("TIM1_CH1N") == "L"
    assert chip.pins.get("PA8").level() == "H" and chip.pins.get("PA7").level() == "L"
    wr(chip, TIM1 + CCER, 0)                                  # both enables off: released
    assert "TIM1_CH1" not in lv and "TIM1_CH1N" not in lv
    wr(chip, TIM1 + BDTR, (1 << 15) | (1 << 11))              # MOE, OSSR
    wr(chip, TIM1 + CCER, 4)                                  # CC1NE only: CH1N = REF, CH1 off-state = CC1P = 0
    assert lv.get("TIM1_CH1") == "L" and lv.get("TIM1_CH1N") == "H"
    wr(chip, TIM1 + BDTR, 1 << 15)                            # OSSR=0: CH1 released
    assert "TIM1_CH1" not in lv and lv.get("TIM1_CH1N") == "H"


def test_capture_channel_pin_is_an_input_on_a_net(desc):
    """A pin muxed to an input-capture channel (CCxS=01) is an input: PUPDR pulls it and an external level shows
    through, instead of the timer driving it. Back to output compare (CCxS=00, CCxE=1) it drives again."""
    chip = _setup(desc, {6: 2})
    pa6 = chip.pins.get("PA6")
    wr(chip, TIM3 + CCMR1, 1)                                 # CC1S=01: TI1 capture
    wr(chip, TIM3 + CCER, 1)                                  # CC1E = capture enable
    assert pa6.owner == "TIM3_CH1" and pa6.direction == "in" and pa6.level() == "X"
    wr(chip, GPIOA + 0x0C, 1 << 12)                           # PUPDR6 = pull-up
    assert pa6.pull == "up" and pa6.level() == "H"
    chip.pins.set_external("PA6", "L")                           # external drive (e.g. a net) is seen
    assert pa6.level() == "L"
    chip.pins.set_external("PA6", None)
    wr(chip, TIM3 + CCR1, 50)                                 # read-only while the channel is an input
    assert rd(chip, TIM3 + CCR1) == 0
    wr(chip, TIM3 + CCMR1, PWM1)                              # output compare again
    wr(chip, TIM3 + CCR1, 50)                                 # REF = 0 < 50
    assert pa6.direction == "out" and pa6.level() == "H"


def test_disabled_output_is_released_with_pull(desc):
    """CCxE=0 on a pin muxed to TIMx_CHn: not driven, the level follows PUPDR (pull-down -> L)."""
    chip = _setup(desc, {6: 2})
    wr(chip, GPIOA + 0x0C, 2 << 12)                           # PUPDR6 = pull-down
    pa6 = chip.pins.get("PA6")
    assert pa6.direction == "in" and pa6.level() == "L"
    wr(chip, TIM3 + CCR1, 50)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCER, 1)
    assert pa6.direction == "out" and pa6.level() == "H"
    wr(chip, TIM3 + CCER, 0)
    assert pa6.direction == "in" and pa6.level() == "L"
