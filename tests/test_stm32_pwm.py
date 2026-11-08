"""STM32F4 timer output compare / PWM (chipem/periph/stm32/tim.py): compare matches, OCxREF modes, polarity,
preload, center-aligned PWM, compare interrupts and the complementary outputs of TIM1, observed on the pins."""
import pytest

from test_stm32 import GPIOA, RCC, TIM3, desc, rd, spin_chip, wr  # noqa: F401  (desc is a fixture)

TIM1 = 0x40010000
CR1, DIER, SR, EGR, CCMR1, CCER, CNT, PSC, ARR, CCR1, BDTR = 0x00, 0x0C, 0x10, 0x14, 0x18, 0x20, 0x24, 0x28, 0x2C, 0x34, 0x44
PWM1, PWM2, TOGGLE = 6 << 4, 7 << 4, 3 << 4
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
    wr(chip, TIM3 + CCER, 0)                                  # output disabled
    assert chip.pins.get("PA6").level() == "L"


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
    assert chip.pins.get("PA8").level() == "L" and chip.pins.get("PA7").level() == "L"   # MOE=0: outputs off
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
