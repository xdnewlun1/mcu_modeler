"""STM32F4 timer details checked against RM0090 Rev 22 (chipem/periph/stm32/tim.py): CR1.OPM on TIM10/11/13/14
(19.5.1), URS gating the UG update DMA request (18.4.1), BDTR.LOCK write protection (17.4.18).  16 MHz HSI, APB /1:
one timer tick = one core cycle."""
from chipem.periph.stm32 import _util
from test_stm32 import RCC, TIM3, desc, rd, spin_chip, wr  # noqa: F401  (desc is a fixture)

TIM1, TIM10, TIM14 = 0x40010000, 0x40014400, 0x40002000
CR1, CR2, DIER, SR, EGR, CCMR1, CCER, CNT, ARR, BDTR = 0, 4, 0xC, 0x10, 0x14, 0x18, 0x20, 0x24, 0x2C, 0x44
CEN, URS, OPM = 1, 4, 8
MOE, OSSI = 1 << 15, 1 << 10


def _chip(desc):
    chip = spin_chip(desc, clock_hz=16e6)
    wr(chip, RCC + 0x40, 0x1FF)                              # TIM2-7, TIM12-14
    wr(chip, RCC + 0x44, 0b11 | (7 << 16))                   # TIM1, TIM8, TIM9-11
    return chip


def test_tim10_tim14_one_pulse_mode(desc):
    chip = _chip(desc)
    for base in (TIM10, TIM14):
        wr(chip, base + ARR, 99)
        wr(chip, base + CR1, OPM | CEN)
        assert rd(chip, base + CR1) == OPM | CEN             # OPM is implemented (RM0090 19.5.1)
    chip.run_cycles(150)
    for base in (TIM10, TIM14):
        assert rd(chip, base + CR1) == OPM and rd(chip, base + CNT) == 0     # stopped at the update event
        assert rd(chip, base + SR) & 1


def test_ug_with_urs_raises_neither_uif_nor_up_dma(desc, monkeypatch):
    chip = _chip(desc)
    reqs = []
    monkeypatch.setattr(_util, "dma_request", lambda c, name, level=True: reqs.append(name), raising=False)
    wr(chip, TIM3 + DIER, 1 << 8)                            # UDE
    wr(chip, TIM3 + CR1, URS)
    wr(chip, TIM3 + EGR, 1)
    assert reqs == [] and not rd(chip, TIM3 + SR) & 1        # URS: only over/underflow (RM0090 18.4.1)
    wr(chip, TIM3 + CR1, 0)
    wr(chip, TIM3 + EGR, 1)
    assert reqs == ["TIM3_UP"] and rd(chip, TIM3 + SR) & 1


def test_bdtr_lock_levels(desc):
    chip = _chip(desc)
    wr(chip, TIM1 + BDTR, 1 << 8 | 10)                       # LOCK level 1, DTG = 10
    wr(chip, TIM1 + BDTR, 3 << 8 | 20 | MOE | OSSI | 1 << 12)
    assert rd(chip, TIM1 + BDTR) == 1 << 8 | 10 | MOE | OSSI     # LOCK written once, DTG/BKE frozen; MOE, OSSI free
    wr(chip, TIM1 + CR2, 1 << 8)                             # OIS1 frozen at level 1
    assert rd(chip, TIM1 + CR2) == 0
    wr(chip, TIM1 + CCER, 1 << 1)                            # CC1P is free at level 1
    assert rd(chip, TIM1 + CCER) == 2
    chip.reset()                                             # a reset unlocks
    assert rd(chip, TIM1 + BDTR) == 0
    wr(chip, TIM1 + CCMR1, 1)                                # CH1 input, CH2 output
    wr(chip, TIM1 + BDTR, 3 << 8)                            # level 3
    wr(chip, TIM1 + CCER, 1 << 1 | 1 << 5)                   # CC1P (input channel) writable, CC2P frozen
    assert rd(chip, TIM1 + CCER) == 1 << 1
    wr(chip, TIM1 + CCMR1, 1 | 6 << 12 | 1 << 11)            # OC2M / OC2PE frozen
    assert rd(chip, TIM1 + CCMR1) == 1
    wr(chip, TIM1 + BDTR, OSSI)
    assert rd(chip, TIM1 + BDTR) == 3 << 8                   # OSSI frozen at level 2+
