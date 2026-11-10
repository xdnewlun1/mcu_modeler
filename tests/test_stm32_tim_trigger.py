"""STM32F4 timer synchronisation (chipem/periph/stm32/tim.py): master mode TRGO (CR2.MMS), slave mode controller
(SMCR.SMS reset / gated / trigger / external clock mode 1, TS = ITR0-3, TI1F_ED, TI1FP1, TI2FP2, ETRF), external
clock mode 2 (ECE, ETP, ETPS), CR2.TI1S, TRC capture, DMA requests.  16 MHz HSI, APB /1: one timer tick = one core
cycle (PSC=0), so every expected value is an exact cycle count."""
import pytest

from chipem.periph.stm32 import _util
from chipem.periph.stm32.tim import ITR
from test_stm32 import GPIOA, GPIOB, RCC, TIM2, TIM3, desc, rd, spin_chip, wr  # noqa: F401  (desc is a fixture)

TIM1, TIM4, TIM5, TIM8 = 0x40010000, 0x40000800, 0x40000C00, 0x40010400
TIM9, TIM10, TIM11, TIM12, TIM13, TIM14 = 0x40014000, 0x40014400, 0x40014800, 0x40001800, 0x40001C00, 0x40002000
BASES = {"TIM1": TIM1, "TIM2": TIM2, "TIM3": TIM3, "TIM4": TIM4, "TIM5": TIM5, "TIM8": TIM8, "TIM9": TIM9,
         "TIM10": TIM10, "TIM11": TIM11, "TIM12": TIM12, "TIM13": TIM13, "TIM14": TIM14}
CR1, CR2, SMCR, DIER, SR, EGR, CCMR1, CCMR2, CCER, CNT, PSC, ARR = 0, 4, 8, 0xC, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, \
    0x28, 0x2C
CCR1, CCR2 = 0x34, 0x38
CEN, OPM, UG, TIF = 1, 8, 1, 1 << 6
PWM1, TOGGLE = 6 << 4, 3 << 4
SMS_RESET, SMS_GATED, SMS_TRIGGER, SMS_EXT = 4, 5, 6, 7
TS_TI1F_ED, TS_TI1FP1, TS_TI2FP2, TS_ETRF = 4, 5, 6, 7
ECE, ETP = 1 << 14, 1 << 15


def mms(n):
    return n << 4


def smcr(sms, ts):
    return sms | (ts << 4)


def _chip(desc, af: dict | None = None):
    """16 MHz; GPIOA/B and every timer clocked; pins {(port, bit): AF} in alternate-function mode."""
    chip = spin_chip(desc, clock_hz=16e6)
    wr(chip, RCC + 0x30, 0b11)                               # GPIOA, GPIOB
    wr(chip, RCC + 0x40, 0x1FF)                              # TIM2-7, TIM12-14
    wr(chip, RCC + 0x44, 0b11 | (7 << 16))                   # TIM1, TIM8, TIM9-11
    for (port, bit), a in (af or {}).items():
        r = port + (0x20 if bit < 8 else 0x24)
        wr(chip, r, rd(chip, r) | (a << (4 * (bit % 8))))
        wr(chip, port, (rd(chip, port) & ~(3 << 2 * bit)) | (2 << 2 * bit))
    return chip


def _probe(chip, base, times, reg=CNT):
    """Read `reg` at the given absolute core cycles (scheduler callbacks: exact time)."""
    out = []
    for t in times:
        chip.sched.at_cycles(t, lambda: out.append(rd(chip, base + reg)))
    return out


def _master_update(chip, base, arr=99):
    """Master: TRGO = update event, period arr+1 cycles; returns the start cycle."""
    wr(chip, base + ARR, arr)
    wr(chip, base + CR2, mms(2))
    t0 = chip.now_cycles()
    wr(chip, base + CR1, CEN)
    return t0


def _trgo_times(chip, name):
    seen = []
    chip.peripherals[name].add_trigger_listener("TRGO", lambda t, lvl: lvl and seen.append(t))
    return seen


# ------------------------------------------------------------------------------------------------ master mode
def test_trgo_update_clocks_slave_in_external_clock_mode_1(desc):
    """TIM3 MMS=010 (update) -> TIM2 ITR2 (Table 93), SMS=111: TIM2 counts one per TIM3 update (every 100 cycles)."""
    chip = _chip(desc)
    wr(chip, TIM2 + ARR, 0xFFFF)
    wr(chip, TIM2 + SMCR, smcr(SMS_EXT, 2))
    wr(chip, TIM2 + CR1, CEN)
    seen = _trgo_times(chip, "TIM3")
    t0 = _master_update(chip, TIM3)
    got = _probe(chip, TIM2, [t0 + 99, t0 + 101, t0 + 550, t0 + 1001])
    chip.run_cycles(1100)
    assert seen[:3] == [t0 + 100, t0 + 200, t0 + 300]
    assert got == [0, 1, 5, 10]
    assert rd(chip, TIM2 + SR) & TIF


def test_trgo_enable_gates_slave(desc):
    """TIM3 MMS=001 (CNT_EN) -> TIM2 gated mode (SMS=101, ITR2): TIM2 runs exactly while TIM3 is enabled; TIF on both
    edges."""
    chip = _chip(desc)
    wr(chip, TIM2 + ARR, 0xFFFF)
    wr(chip, TIM2 + SMCR, smcr(SMS_GATED, 2))
    wr(chip, TIM2 + CR1, CEN)
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + CR2, mms(1))
    t0 = chip.now_cycles() + 100
    chip.sched.at_cycles(t0, lambda: wr(chip, TIM3 + CR1, CEN))
    chip.sched.at_cycles(t0 + 500, lambda: wr(chip, TIM3 + CR1, 0))
    got = _probe(chip, TIM2, [t0 - 10, t0 + 250, t0 + 700])
    chip.run_cycles(900)
    assert got == [0, 250, 500]
    assert rd(chip, TIM2 + SR) & TIF and chip.peripherals["TIM3"].trigger_level("TRGO") is False


def test_trgo_reset_ug_resets_slave(desc):
    """MMS=000: EGR.UG of TIM3 is TRGO; TIM2 in reset mode on ITR2 re-initialises its counter."""
    chip = _chip(desc)
    wr(chip, TIM2 + ARR, 0xFFFF)
    wr(chip, TIM2 + SMCR, smcr(SMS_RESET, 2))
    t0 = chip.now_cycles()
    wr(chip, TIM2 + CR1, CEN)
    chip.sched.at_cycles(t0 + 300, lambda: wr(chip, TIM3 + EGR, UG))
    got = _probe(chip, TIM2, [t0 + 299, t0 + 400])
    chip.run_cycles(450)
    assert got == [299, 100]
    assert rd(chip, TIM2 + SR) & (TIF | 1) == TIF | 1                 # TIF and the update flag of the reset


def test_trigger_mode_starts_slave_one_pulse(desc):
    """TIM3 update -> TIM2 trigger mode (SMS=110): TIM2 CEN is set at TIM3's first update; with OPM it stops at its
    own update event and is re-armed by the next trigger."""
    chip = _chip(desc)
    wr(chip, TIM2 + ARR, 49)
    wr(chip, TIM2 + SMCR, smcr(SMS_TRIGGER, 2))
    wr(chip, TIM2 + CR1, OPM)
    t0 = _master_update(chip, TIM3, arr=199)
    got = _probe(chip, TIM2, [t0 + 150, t0 + 230, t0 + 260], reg=CNT)
    cen = _probe(chip, TIM2, [t0 + 199, t0 + 201, t0 + 260], reg=CR1)
    chip.run_cycles(300)
    assert got == [0, 30, 0]                         # started at t0+200, update (and stop) at t0+250
    assert [c & CEN for c in cen] == [0, 1, 0]


def test_trgo_oc1ref_gates_slave_without_output_enabled(desc):
    """MMS=100: TRGO = OC1REF (PWM1, CCR1=30, period 100) with CC1E=0; TIM2 gated by it counts 30 per period."""
    chip = _chip(desc)
    wr(chip, TIM2 + ARR, 0xFFFF)
    wr(chip, TIM2 + SMCR, smcr(SMS_GATED, 2))
    wr(chip, TIM2 + CR1, CEN)
    wr(chip, TIM3 + CCMR1, PWM1)
    wr(chip, TIM3 + CCR1, 30)
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CR2, mms(4))
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, CEN)
    got = _probe(chip, TIM2, [t0 + 20, t0 + 70, t0 + 1050])
    chip.run_cycles(1100)
    assert got == [20, 30, 330]


def test_trgo_compare_pulse_counts_cc1_matches(desc):
    """MMS=011: a TRGO pulse whenever CC1IF is to be set (here every compare match, even with CC1IF still set)."""
    chip = _chip(desc)
    wr(chip, TIM2 + ARR, 0xFFFF)
    wr(chip, TIM2 + SMCR, smcr(SMS_EXT, 2))
    wr(chip, TIM2 + CR1, CEN)
    wr(chip, TIM3 + CCR1, 10)
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CR2, mms(3))
    seen = _trgo_times(chip, "TIM3")
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, CEN)
    chip.run_cycles(520)
    assert seen == [t0 + 10, t0 + 110, t0 + 210, t0 + 310, t0 + 410, t0 + 510]
    assert rd(chip, TIM2 + CNT) == 6


@pytest.mark.parametrize("slave", sorted(ITR))
def test_itr_tables(desc, slave):
    """Every ITRx of RM0090 Tables 86/93/97: the master's trigger clocks the slave (external clock mode 1)."""
    for i, (master, sig) in enumerate(ITR[slave]):
        chip = _chip(desc)
        s, m = BASES[slave], BASES[master]
        wr(chip, s + ARR, 0xFFFF)
        wr(chip, s + SMCR, smcr(SMS_EXT, i))
        wr(chip, s + CR1, CEN)
        wr(chip, m + ARR, 9)
        if sig == "TRGO":
            wr(chip, m + CR2, mms(2))
        else:                                        # TIM10/11/13/14 OC1REF in toggle mode, CCR1=0
            wr(chip, m + CCMR1, TOGGLE)
        for other in BASES.values():                 # only the master runs
            if other not in (s, m):
                wr(chip, other + CR1, 0)
        wr(chip, m + CR1, CEN)
        chip.run_cycles(205)
        n = rd(chip, s + CNT)
        assert n == (20 if sig == "TRGO" else 10), (slave, i, master, n)


# ------------------------------------------------------------------------------------------------ TI / ETR inputs
def _pin_at(chip, t, pin, lvl):
    chip.sched.at_cycles(t, chip.pins.set_external, pin, lvl)


def test_ti1fp1_trigger_and_gated_from_pin(desc):
    """TIM3 CH1 on PA6: trigger mode on TI1FP1 (CC1P=1: falling edge) starts the counter; then gated mode on TI1FP1
    (CC1P=0) counts while PA6 is high."""
    chip = _chip(desc, {(GPIOA, 6): 2})
    chip.pins.set_external("PA6", "H")
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + CCMR1, 1)                        # CC1S=01: TI1 (input)
    wr(chip, TIM3 + CCER, 2)                         # CC1P: inverted -> trigger on the falling edge
    wr(chip, TIM3 + SMCR, smcr(SMS_TRIGGER, TS_TI1FP1))
    t0 = chip.now_cycles()
    _pin_at(chip, t0 + 100, "PA6", "L")
    got = _probe(chip, TIM3, [t0 + 99, t0 + 160])
    chip.run_cycles(200)
    assert got == [0, 60] and rd(chip, TIM3 + CR1) & CEN and rd(chip, TIM3 + SR) & TIF
    # gated, non-inverted: runs while PA6 = H
    wr(chip, TIM3 + CR1, 0)
    wr(chip, TIM3 + CNT, 0)
    wr(chip, TIM3 + CCER, 0)
    wr(chip, TIM3 + SMCR, smcr(SMS_GATED, TS_TI1FP1))
    wr(chip, TIM3 + CR1, CEN)
    t1 = chip.now_cycles()
    _pin_at(chip, t1 + 50, "PA6", "H")
    _pin_at(chip, t1 + 80, "PA6", "L")
    _pin_at(chip, t1 + 200, "PA6", "H")
    _pin_at(chip, t1 + 210, "PA6", "L")
    got = _probe(chip, TIM3, [t1 + 40, t1 + 70, t1 + 150, t1 + 300])
    chip.run_cycles(320)
    assert got == [0, 20, 30, 40]


def test_ti1f_ed_counts_both_edges_and_ti2fp2_rising(desc):
    """External clock mode 1: TS=TI1F_ED counts every TI1 edge; TS=TI2FP2 counts rising TI2 edges (PSC=1: /2)."""
    chip = _chip(desc, {(GPIOA, 6): 2, (GPIOA, 7): 2})
    for p in ("PA6", "PA7"):
        chip.pins.set_external(p, "L")
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + CCMR1, 1 | (1 << 8))
    wr(chip, TIM3 + SMCR, smcr(SMS_EXT, TS_TI1F_ED))
    wr(chip, TIM3 + CR1, CEN)
    t0 = chip.now_cycles()
    for k in range(10):
        _pin_at(chip, t0 + 10 + 10 * k, "PA6", "H" if k % 2 == 0 else "L")
    chip.run_cycles(150)
    assert rd(chip, TIM3 + CNT) == 10
    wr(chip, TIM3 + SMCR, smcr(SMS_EXT, TS_TI2FP2))
    wr(chip, TIM3 + PSC, 1)
    wr(chip, TIM3 + EGR, UG)                         # PSC active now, counter 0
    t1 = chip.now_cycles()
    for k in range(10):
        _pin_at(chip, t1 + 10 + 10 * k, "PA7", "H" if k % 2 == 0 else "L")
    chip.run_cycles(150)
    assert rd(chip, TIM3 + CNT) == 2                 # 5 rising edges / 2 (prescaler)


def test_etr_external_clock_mode_2_polarity_and_prescaler(desc):
    """TIM2_ETR on PA0 (AF1 "TIM2_CH1_ETR"): ECE=1, ETP=1 (falling edges), ETPS=01 (/2); compare match at CCR1 and
    the update event at ARR."""
    chip = _chip(desc, {(GPIOA, 0): 1})
    chip.pins.set_external("PA0", "H")
    wr(chip, TIM2 + ARR, 4)
    wr(chip, TIM2 + CCR1, 2)
    wr(chip, TIM2 + SMCR, ECE | ETP | (1 << 12))
    wr(chip, TIM2 + CR1, CEN)
    t0 = chip.now_cycles()
    for k in range(24):                              # 12 falling edges -> 6 counts
        _pin_at(chip, t0 + 10 + 10 * k, "PA0", "L" if k % 2 == 0 else "H")
    chip.run_cycles(260)
    assert rd(chip, TIM2 + CNT) == 1                 # 0..4 then wraps: 6 counts -> 1
    assert rd(chip, TIM2 + SR) & 0b11 == 0b11        # CC1IF and UIF
    pa0 = chip.pins.get("PA0")
    assert pa0.direction == "in"                     # CH1 not an output: the pin is an input for ETR


def test_etrf_trigger_mode(desc):
    """TS=ETRF, SMS=110: the first rising ETR edge starts TIM2."""
    chip = _chip(desc, {(GPIOA, 0): 1})
    chip.pins.set_external("PA0", "L")
    wr(chip, TIM2 + ARR, 0xFFFF)
    wr(chip, TIM2 + SMCR, smcr(SMS_TRIGGER, TS_ETRF))
    t0 = chip.now_cycles()
    _pin_at(chip, t0 + 77, "PA0", "H")
    got = _probe(chip, TIM2, [t0 + 76, t0 + 177])
    chip.run_cycles(200)
    assert got == [0, 100]


def test_ti1s_xor_capture(desc):
    """CR2.TI1S: TI1 = CH1 xor CH2 xor CH3 (PA6, PA7, PB0 = TIM3_CH1-3); IC1 on both edges captures every change of
    any of the three pins."""
    chip = _chip(desc, {(GPIOA, 6): 2, (GPIOA, 7): 2, (GPIOB, 0): 2})
    for p in ("PA6", "PA7", "PB0"):
        chip.pins.set_external(p, "L")
    wr(chip, TIM3 + ARR, 0xFFFF)
    wr(chip, TIM3 + CR2, 1 << 7)
    wr(chip, TIM3 + CCMR1, 1)
    wr(chip, TIM3 + CCER, 1 | 2 | 8)                 # CC1E, both edges
    caps = []
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, CEN)
    for k, (c, p, lvl) in enumerate([(10, "PA6", "H"), (20, "PA7", "H"), (30, "PB0", "H"), (40, "PA6", "L")]):
        _pin_at(chip, t0 + c, p, lvl)
        chip.sched.at_cycles(t0 + c + 1, lambda: caps.append(rd(chip, TIM3 + 0x34)))
    chip.run_cycles(60)
    assert caps == [10, 20, 30, 40]


def test_trc_capture_on_itr(desc):
    """CC1S=11 (TRC): TIM2 captures its counter at each TIM3 update (TS=ITR2), no slave mode needed."""
    chip = _chip(desc)
    wr(chip, TIM2 + ARR, 0xFFFFFFFF)
    wr(chip, TIM2 + SMCR, smcr(0, 2))
    wr(chip, TIM2 + CCMR1, 3)
    wr(chip, TIM2 + CCER, 1)
    t2 = chip.now_cycles()
    wr(chip, TIM2 + CR1, CEN)
    chip.run_cycles(37)
    t3 = _master_update(chip, TIM3, arr=99)
    chip.run_cycles(150)
    assert rd(chip, TIM2 + SR) & 2 and rd(chip, TIM2 + CCR1) == t3 + 100 - t2
    assert not [e for e in chip.log if e.kind == "warn" and "TIM" in str(e.fields.get("msg"))]


def test_dma_requests(desc, monkeypatch):
    """DIER.UDE / CC1DE / TDE raise TIMx_UP / TIMx_CH1 / TIMx_TRIG through _util.dma_request at the event time."""
    reqs = []
    chip = _chip(desc)
    monkeypatch.setattr(_util, "dma_request", lambda c, name, level=True: reqs.append((name, chip.now_cycles())),
                        raising=False)
    wr(chip, TIM3 + ARR, 99)
    wr(chip, TIM3 + CCR1, 40)
    wr(chip, TIM3 + DIER, (1 << 8) | (1 << 9) | (1 << 14))
    t0 = chip.now_cycles()
    wr(chip, TIM3 + CR1, CEN)
    chip.run_cycles(150)
    wr(chip, TIM3 + EGR, TIF)
    t1 = chip.now_cycles()
    assert reqs == [("TIM3_CH1", t0 + 40), ("TIM3_UP", t0 + 100), ("TIM3_CH1", t0 + 140), ("TIM3_TRIG", t1)]
