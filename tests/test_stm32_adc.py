"""STM32F4 ADC (chipem/periph/stm32/adc.py): conversion timing (sampling + resolution ADCCLK, trigger latency,
ADCPRE), sequences (SCAN, EOCS, CONT, DISCEN), alignment, overrun, analog watchdog, shared IRQ, internal channels,
timer triggers (TRGO / CCx, EXTEN edges), DMA requests and the `--adc` naming (pins, ADCn_INm, volts).
16 MHz HSI, APB2 /1: PCLK2 = 16 MHz, ADCPRE /2 -> ADCCLK = 8 MHz = 2 core cycles; every time is an exact cycle
count."""
import pytest

from chipem.periph.stm32 import _util
from chipem.session import Session, parse_adc_value
from test_stm32 import GPIOA, GPIOB, RCC, TIM2, TIM3, YAML, desc, rd, spin_chip, wr  # noqa: F401  (desc: fixture)

ADC1, ADC2, ADC3, C_ADC = 0x40012000, 0x40012100, 0x40012200, 0x40012300
SR, CR1, CR2, SMPR1, SMPR2, HTR, LTR, SQR1, SQR2, SQR3, DR = 0, 4, 8, 0xC, 0x10, 0x24, 0x28, 0x2C, 0x30, 0x34, 0x4C
CSR, CCR = 0, 4
AWD, EOC, STRT, OVR = 1, 2, 16, 32
EOCIE, AWDIE, SCAN, AWDSGL, DISCEN, AWDEN, OVRIE = 1 << 5, 1 << 6, 1 << 8, 1 << 9, 1 << 11, 1 << 23, 1 << 26
ADON, CONT, DMA, EOCS, ALIGN, SWSTART = 1, 2, 1 << 8, 1 << 10, 1 << 11, 1 << 30
TSVREFE, VBATE = 1 << 23, 1 << 22
ADC_IRQ = 18
CONV12 = 2 * (2 + 3 + 12)          # tlatr 2 + 3 sampling + 12 bits, ADCCLK = 2 core cycles


def ext(sel, edge=1):
    return (sel << 24) | (edge << 28)


def _chip(desc):
    chip = spin_chip(desc, clock_hz=16e6)
    wr(chip, RCC + 0x30, 0b11)                              # GPIOA, GPIOB
    wr(chip, RCC + 0x40, 0b11)                              # TIM2, TIM3
    wr(chip, RCC + 0x44, 7 << 8)                            # ADC1-3
    return chip


def _probe(chip, times, addr):
    out = []
    for t in times:
        chip.sched.at_cycles(t, lambda: out.append(rd(chip, addr)))
    return out


def _irq(chip):
    return (16 + ADC_IRQ) in chip.arch.asserted


def _swstart(chip, base=ADC1, cr2=0):
    wr(chip, base + CR2, ADON | cr2)
    t0 = chip.now_cycles()
    wr(chip, base + CR2, ADON | cr2 | SWSTART)
    return t0


# ------------------------------------------------------------------------------------------------ timing / values
def test_single_conversion_timing_value_and_dr_read(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    assert adc.set_channel("PA0", 1.65)
    wr(chip, ADC1 + CR1, EOCIE)
    t0 = _swstart(chip)
    assert rd(chip, ADC1 + CR2) & SWSTART == 0 and rd(chip, ADC1 + SR) == STRT
    sr = _probe(chip, [t0 + CONV12 - 1, t0 + CONV12 + 1], ADC1 + SR)
    chip.run_cycles(CONV12 + 10)
    assert sr == [STRT, STRT | EOC] and _irq(chip)
    assert rd(chip, ADC1 + DR) == 2048
    assert rd(chip, ADC1 + SR) == STRT and not _irq(chip)          # DR read clears EOC
    assert adc.conversions == 1


@pytest.mark.parametrize("smp,res,pre,align,expect", [
    (7, 2, 3, 0, 128), (7, 2, 3, ALIGN, 0x8000), (1, 1, 0, ALIGN, 0x8000),
    (4, 3, 1, 0, 32), (4, 3, 1, ALIGN, 32 << 2), (2, 0, 2, ALIGN, 2048 << 4)])
def test_sampling_time_resolution_prescaler_alignment(desc, smp, res, pre, align, expect):
    chip = _chip(desc)
    chip.peripherals["ADC1"].set_channel(3, 1.65)
    wr(chip, C_ADC + CCR, pre << 16)
    wr(chip, ADC1 + SMPR2, smp << 9)                        # SMP3
    wr(chip, ADC1 + SQR3, 3)
    wr(chip, ADC1 + CR1, res << 24)
    clk = 2 * (pre + 1)
    total = (2 + (3, 15, 28, 56, 84, 112, 144, 480)[smp] + (12, 10, 8, 6)[res]) * clk
    t0 = _swstart(chip, cr2=align)
    sr = _probe(chip, [t0 + total - 1, t0 + total + 1], ADC1 + SR)
    chip.run_cycles(total + 5)
    assert [s & EOC for s in sr] == [0, EOC]
    assert rd(chip, ADC1 + DR) == expect


def test_scan_sequence_eocs_and_internal_channels(desc):
    """SQ1=IN0 (PA0 1.0 V), SQ2=IN1 (raw 1234), SQ3=IN17 VREFINT (TSVREFE): back to back after one latency; EOCS=0 sets
    EOC only at the end of the sequence, EOCS=1 after each conversion."""
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel("ADC1_IN0", 1.0)
    adc.set_channel("IN1", 1234)
    wr(chip, C_ADC + CCR, TSVREFE)
    wr(chip, ADC1 + SQR1, 2 << 20)
    wr(chip, ADC1 + SQR3, 0 | (1 << 5) | (17 << 10))
    wr(chip, ADC1 + CR1, SCAN)
    t0 = _swstart(chip)
    ends = [t0 + 4 + 30, t0 + 4 + 60, t0 + 4 + 90]
    sr = _probe(chip, [e + 1 for e in ends], ADC1 + SR)
    dr = _probe(chip, [e + 1 for e in ends], ADC1 + DR)
    chip.run_cycles(100)
    assert dr == [int(1.0 / 3.3 * 4096), 1234, int(1.21 / 3.3 * 4096)]
    assert [s & EOC for s in sr] == [0, 0, EOC]
    t1 = _swstart(chip, cr2=EOCS)
    ends = [t1 + 34, t1 + 64, t1 + 94]
    sr = _probe(chip, [e + 1 for e in ends], ADC1 + SR)
    dr = _probe(chip, [e + 1 for e in ends], ADC1 + DR)        # reading DR clears EOC before the next one
    chip.run_cycles(100)
    assert [s & EOC for s in sr] == [EOC, EOC, EOC] and dr[2] == 1501
    assert rd(chip, ADC1 + SR) & OVR == 0


def test_internal_channels_temp_vbat_and_disabled(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    assert adc.volts(16) == 0 and adc.volts(17) == 0 and adc.volts(18) == 0     # TSVREFE / VBATE off
    wr(chip, C_ADC + CCR, TSVREFE | VBATE)
    assert adc.volts(16) == pytest.approx(0.76) and adc.volts(17) == pytest.approx(1.21)
    assert adc.volts(18) == pytest.approx(1.65)
    adc.set_channel("ADC1_TEMP", 40)
    adc.set_channel("VBAT", 3.0)
    assert adc.code12(16) == int((0.76 + 0.0025 * 15) / 3.3 * 4096) and adc.volts(18) == pytest.approx(1.5)
    assert chip.peripherals["ADC2"].volts(17) == 0                                 # internal channels: ADC1 only


def test_continuous_overrun_stops_conversions(desc):
    """CONT=1, EOCS=1, DR never read: the second conversion sets OVR (OVRIE -> IRQ) and the ADC stops."""
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + CR1, OVRIE)
    t0 = _swstart(chip, cr2=CONT | EOCS)
    sr = _probe(chip, [t0 + 34 + 29, t0 + 34 + 31], ADC1 + SR)
    chip.run_cycles(300)
    assert [s & OVR for s in sr] == [0, OVR] and _irq(chip)
    assert adc.conversions == 1 and adc._ev is None
    wr(chip, ADC1 + CR2, ADON | CONT | EOCS | SWSTART)            # ignored while OVR is set
    assert adc._ev is None
    wr(chip, ADC1 + SR, ~OVR & 0xFFFF)
    assert not _irq(chip)


def test_continuous_without_eocs_has_no_overrun(desc):
    """RM0090 13.8.1: overrun detection only with DMA=1 or EOCS=1; CONT converts back to back (30 cycles each)."""
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    _swstart(chip, cr2=CONT)
    chip.run_cycles(34 + 30 * 9 + 5)
    assert adc.conversions == 10 and not rd(chip, ADC1 + SR) & OVR
    wr(chip, ADC1 + CR2, 0)                                       # ADON=0: the running conversion is lost
    chip.run_cycles(100)
    assert adc.conversions == 10


def test_discontinuous_mode(desc):
    """DISCEN, DISCNUM=1 (2 per trigger), L=4 (5 channels): triggers convert 2, 2, then the last 1; EOC (EOCS=0) at
    the end of the sequence."""
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + SQR1, 4 << 20)
    wr(chip, ADC1 + CR1, DISCEN | (1 << 13))
    counts = []
    for _ in range(4):
        _swstart(chip)
        chip.run_cycles(200)
        counts.append(adc.conversions)
        if rd(chip, ADC1 + SR) & EOC:
            counts.append("EOC")
            rd(chip, ADC1 + DR)
    assert counts == [2, 4, 5, "EOC", 7]


def test_trigger_ignored_while_converting(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    t0 = _swstart(chip)
    chip.sched.at_cycles(t0 + 10, lambda: wr(chip, ADC1 + CR2, ADON | SWSTART))
    chip.run_cycles(200)
    assert adc.conversions == 1


def test_analog_watchdog_and_shared_irq(desc):
    chip = _chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    a1.set_channel("PA0", 1.65)                                    # PA0 = ADC123_IN0: ADC2 sees it too
    assert a2.volts(0) == pytest.approx(1.65)
    wr(chip, ADC1 + HTR, 1000)
    wr(chip, ADC1 + CR1, AWDEN | AWDSGL | AWDIE | 0)
    wr(chip, ADC2 + CR1, EOCIE)
    _swstart(chip, ADC1)
    _swstart(chip, ADC2)
    chip.run_cycles(50)
    assert rd(chip, ADC1 + SR) & AWD and rd(chip, ADC2 + SR) & EOC and _irq(chip)
    assert rd(chip, C_ADC + CSR) & 0x3F3F == (STRT | EOC | AWD) | ((STRT | EOC) << 8)
    wr(chip, ADC1 + SR, ~AWD & 0xFFFF)
    assert _irq(chip)                                              # ADC2 EOC still holds the shared line
    rd(chip, ADC2 + DR)
    assert not _irq(chip)
    wr(chip, ADC1 + CR1, AWDEN | AWDSGL | AWDIE | 1)               # watchdog on channel 1 only: no AWD for IN0
    _swstart(chip, ADC1)
    chip.run_cycles(50)
    assert not rd(chip, ADC1 + SR) & AWD


def test_pin_level_when_no_value(desc):
    """A channel without an injected voltage reads its pin: PB0 (ADC12_IN8) driven H -> full scale, L -> 0."""
    chip = _chip(desc)
    wr(chip, GPIOB, 1)                                             # PB0 output
    wr(chip, GPIOB + 0x14, 1)                                      # ODR: H
    wr(chip, ADC1 + SQR3, 8)
    _swstart(chip)
    chip.run_cycles(50)
    assert rd(chip, ADC1 + DR) == 4095
    wr(chip, GPIOB + 0x14, 0)
    _swstart(chip)
    chip.run_cycles(50)
    assert rd(chip, ADC1 + DR) == 0


# ------------------------------------------------------------------------------------------------ external triggers
def test_tim3_trgo_triggers_conversions(desc):
    """EXTSEL=1000 TIM3_TRGO (MMS=010 update every 1000 cycles), rising: EOC 1 PCLK2 + 2 ADCCLK + 15 ADCCLK after each
    update."""
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + CR2, ADON | ext(8))
    wr(chip, TIM3 + 0x2C, 999)
    wr(chip, TIM3 + 4, 2 << 4)
    t0 = chip.now_cycles()
    wr(chip, TIM3, 1)
    lat = 1 + CONV12
    sr = _probe(chip, [t0 + 1000 + lat - 1, t0 + 1000 + lat + 1], ADC1 + SR)
    chip.run_cycles(3000 + lat + 5)
    assert [s & EOC for s in sr] == [0, EOC]
    assert adc.conversions == 3


@pytest.mark.parametrize("edge", [1, 2, 3])
def test_tim2_cc2_edges(desc, edge):
    """EXTSEL=0011 TIM2_CC2 = OC2REF (PWM1, CCR2=300, period 1000; CC2E=0): rising edge at each update, falling at the
    compare match.  OC2REF is already high when the trigger is enabled (PWM1 with the stopped CNT=0 < CCR2), so the
    counter start is no edge."""
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    times = []
    adc_done = adc._done
    adc._done = lambda: (times.append(chip.now_cycles()), adc_done())
    wr(chip, TIM2 + 0x18, 6 << 12)                                # CCMR1: OC2M = PWM1
    wr(chip, TIM2 + 0x38, 300)
    wr(chip, TIM2 + 0x2C, 999)
    wr(chip, ADC1 + CR2, ADON | ext(3, edge))
    t0 = chip.now_cycles()
    wr(chip, TIM2, 1)
    chip.run_cycles(2500)
    expect = sorted({t0 + c + 1 + CONV12 for c in (300, 1300, 2300) if edge & 2} |
                    {t0 + c + 1 + CONV12 for c in (1000, 2000) if edge & 1})
    assert times == expect


def test_exti11_trigger_warns(desc):
    chip = _chip(desc)
    wr(chip, ADC1 + CR2, ADON | ext(15))
    assert any("EXTI_11" in str(e.fields.get("msg")) for e in chip.log if e.kind == "warn")


def test_dma_request_per_conversion(desc, monkeypatch):
    reqs = []
    chip = _chip(desc)
    monkeypatch.setattr(_util, "dma_request", lambda c, name, level=True: reqs.append((name, level,
                                                                                       chip.now_cycles())))
    wr(chip, ADC1 + SQR1, 1 << 20)
    wr(chip, ADC1 + CR1, SCAN)
    t0 = _swstart(chip, cr2=DMA)
    chip.sched.at_cycles(t0 + 35, lambda: rd(chip, ADC1 + DR))      # "the DMA" reads the first result
    chip.run_cycles(80)
    assert reqs == [("ADC1", True, t0 + 34), ("ADC1", False, t0 + 35), ("ADC1", True, t0 + 64)]


# ------------------------------------------------------------------------------------------------ session naming
def test_parse_adc_value():
    assert parse_adc_value("2048") == 2048 and parse_adc_value("0x800") == 2048
    assert parse_adc_value("1.65") == 1.65 and parse_adc_value("1.65V") == 1.65
    assert parse_adc_value("1650mV") == pytest.approx(1.65) and parse_adc_value("3V") == 3.0


def test_session_adc_names():
    s = Session(YAML)
    a1, a2, a3 = (s.chip.peripherals[f"ADC{i}"] for i in (1, 2, 3))
    s.set_adc("PA0", 1.65)
    assert a1.volts(0) == a2.volts(0) == a3.volts(0) == 1.65
    s.set_adc("ADC1_IN1", 1000)                                    # PA1 = ADC123_IN1: a pin value for all three
    assert a3.code12(1) == 1000
    s.set_adc("ADC123_IN2", 0.5)
    assert a2.volts(2) == 0.5
    s.set_adc("ADC3_IN4", 2.0)                                     # ADC3 IN4 is PF6, ADC1 IN4 is PA4
    assert a3.volts(4) == 2.0 and a1.volts(4) == 0.0
    s.set_adc("VBAT", 3.0)
    assert a1.vals["VBAT"] == 3.0
    assert s.adc_values["PA0"] == 1.65 and s.adc_values["ADC1_IN1"] == 1000
    with pytest.raises(ValueError):
        a1.set_channel("PB8", 1.0)                                 # no ADC function on PB8
    s.close()


def test_cli_adc_volts_on_stm32():
    import subprocess
    import sys
    from test_stm32 import ROOT
    elf = ROOT / "firmware" / "samples_stm32" / "build" / "blinky.elf"
    if not elf.exists():
        pytest.skip("not built")
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--chip", "stm32f405zg", str(elf), "--for", "1ms",
                        "--adc", "PA0=1.65V", "--adc", "ADC2_IN3=2048", "--summary-only"],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "rejected" not in r.stdout + r.stderr and "stored only" not in r.stdout + r.stderr


# ------------------------------------------------------------------------------------------------ injected group
JOFR1, JSQR, JDR1, CDR = 0x14, 0x38, 0x3C, 8
JEOC, JSTRT = 4, 8
JEOCIE, JAUTO, JDISCEN, JAWDEN = 1 << 7, 1 << 10, 1 << 12, 1 << 22
JSWSTART = 1 << 22
INJ12 = 2 * (3 + 3 + 12)           # tlatinj 3 + 3 sampling + 12 bits, ADCCLK = 2 core cycles
CONV = 2 * (3 + 12)                # one conversion, back to back


def jsqr(*chs):
    """JSQR for the sequence chs (1-4 channels): the last len(chs) JSQx fields, JL = len - 1."""
    v = (len(chs) - 1) << 20
    for k, ch in enumerate(chs):
        v |= ch << (5 * (4 - len(chs) + k))
    return v


def _times(chip, adc, name):
    out = []
    orig = getattr(adc, name)
    setattr(adc, name, lambda: (out.append(chip.now_cycles()), orig()))
    return out


def test_injected_sequence_offsets_alignment_and_jeoc(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    for ch, code in ((1, 1000), (2, 2000), (3, 3000)):
        adc.set_channel(ch, code)
    wr(chip, ADC1 + JSQR, jsqr(1, 2, 3))
    assert rd(chip, ADC1 + JSQR) == (2 << 20) | (1 << 5) | (2 << 10) | (3 << 15)
    wr(chip, ADC1 + JOFR1 + 4, 2500)                                # JOFR2
    wr(chip, ADC1 + CR1, SCAN | JEOCIE)
    wr(chip, ADC1 + CR2, ADON)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | JSWSTART)
    assert rd(chip, ADC1 + CR2) & JSWSTART == 0 and rd(chip, ADC1 + SR) == JSTRT
    sr = _probe(chip, [t0 + INJ12 + 2 * CONV - 1, t0 + INJ12 + 2 * CONV + 1], ADC1 + SR)
    chip.run_cycles(200)
    assert sr == [JSTRT, JSTRT | JEOC] and _irq(chip)              # JEOC only at the end of the sequence, no EOC
    assert [rd(chip, ADC1 + JDR1 + 4 * k) for k in range(3)] == [1000, (2000 - 2500) & 0xFFFF, 3000]
    assert rd(chip, ADC1 + SR) & JEOC                              # JDR reads do not clear JEOC
    wr(chip, ADC1 + SR, ~JEOC & 0x3F)
    assert not _irq(chip)
    wr(chip, ADC1 + CR2, ADON | ALIGN | JSWSTART)                  # left: one bit less than regular, sign in bit 15
    chip.run_cycles(200)
    assert [rd(chip, ADC1 + JDR1 + 4 * k) for k in range(3)] == [1000 << 3, (-500 << 3) & 0xFFFF, 3000 << 3]
    wr(chip, ADC1 + CR1, 0)                                        # SCAN = 0: only the first channel (JSQ2 here)
    wr(chip, ADC1 + JOFR1, 1000)
    wr(chip, ADC1 + CR2, ADON | JSWSTART)
    chip.run_cycles(200)
    assert rd(chip, ADC1 + JDR1) == 0 and adc.injected_conversions == 7


def test_injected_preempts_regular_which_resumes(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + SMPR1, 3 << 15)                                 # SMP15 = 56 cycles: 68 ADCCLK = 136 core cycles
    wr(chip, ADC1 + SQR1, 1 << 20)
    wr(chip, ADC1 + SQR3, 15 | (6 << 5))
    wr(chip, ADC1 + JSQR, jsqr(1))
    wr(chip, ADC1 + CR1, SCAN)
    rdone, jdone = _times(chip, adc, "_done"), _times(chip, adc, "_jdone")
    t0 = _swstart(chip)
    chip.sched.at_cycles(t0 + 50, lambda: wr(chip, ADC1 + CR2, ADON | JSWSTART))
    chip.run_cycles(400)
    assert jdone == [t0 + 50 + 6 + CONV]
    assert rdone == [jdone[0] + 136, jdone[0] + 136 + CONV]        # IN15 converted again from scratch, then IN6
    assert adc.conversions == 2


def test_regular_trigger_during_injected_waits(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + JSQR, jsqr(1))
    rdone, jdone = _times(chip, adc, "_done"), _times(chip, adc, "_jdone")
    wr(chip, ADC1 + CR2, ADON)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | JSWSTART)
    chip.sched.at_cycles(t0 + 10, lambda: wr(chip, ADC1 + CR2, ADON | SWSTART))
    chip.run_cycles(200)
    assert jdone == [t0 + 6 + CONV] and rdone == [jdone[0] + 4 + CONV]


def test_auto_injection_after_regular_group(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + SQR3, 5)
    wr(chip, ADC1 + JSQR, jsqr(1, 2))
    wr(chip, ADC1 + CR1, SCAN | JAUTO)
    rdone, jdone = _times(chip, adc, "_done"), _times(chip, adc, "_jdone")
    t0 = _swstart(chip, cr2=CONT)
    chip.run_cycles(CONV12 + 3 * CONV + 1)
    assert rdone == [t0 + CONV12, t0 + CONV12 + 3 * CONV]
    assert jdone == [t0 + CONV12 + CONV, t0 + CONV12 + 2 * CONV]
    assert rd(chip, ADC1 + SR) & (EOC | JEOC) == EOC | JEOC
    wr(chip, ADC1 + CR2, ADON | CONT | JSWSTART)                   # ignored with JAUTO
    assert any("JAUTO" in str(e.fields.get("msg")) for e in chip.log if e.kind == "warn")


def test_injected_discontinuous_one_channel_per_trigger(desc):
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    for ch in (1, 2, 3):
        adc.set_channel(ch, 100 * ch)
    wr(chip, ADC1 + JSQR, jsqr(1, 2, 3))
    wr(chip, ADC1 + CR1, JDISCEN)
    wr(chip, ADC1 + CR2, ADON)
    jeoc = []
    for _ in range(4):
        wr(chip, ADC1 + CR2, ADON | JSWSTART)
        chip.run_cycles(60)
        jeoc.append(bool(rd(chip, ADC1 + SR) & JEOC))
    assert jeoc == [False, False, True, True] and adc.injected_conversions == 4
    assert [rd(chip, ADC1 + JDR1 + 4 * k) for k in range(3)] == [100, 200, 300]


def test_injected_timer_trigger_and_watchdog(desc):
    """JEXTSEL=0011 TIM2_TRGO (update every 1000 cycles), rising: JEOC 1 PCLK2 + 3 ADCCLK + 15 ADCCLK after it;
    JAWDEN flags the injected value outside [LTR, HTR]."""
    chip = _chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.set_channel(4, 4000)
    wr(chip, ADC1 + HTR, 3000)
    wr(chip, ADC1 + JSQR, jsqr(4))
    wr(chip, ADC1 + CR1, JAWDEN)
    jdone = _times(chip, adc, "_jdone")
    wr(chip, TIM2 + 0x2C, 999)
    wr(chip, TIM2 + 4, 2 << 4)
    wr(chip, ADC1 + CR2, ADON | (3 << 16) | (1 << 20))
    t0 = chip.now_cycles()
    wr(chip, TIM2, 1)
    chip.run_cycles(2100)
    assert jdone == [t0 + 1000 + 1 + INJ12, t0 + 2000 + 1 + INJ12]
    assert rd(chip, ADC1 + SR) & AWD and adc.conversions == 0


# ------------------------------------------------------------------------------------------------ multi-ADC mode
def test_dual_regular_simultaneous_dma_mode2_cdr(desc, monkeypatch):
    reqs = []
    chip = _chip(desc)
    monkeypatch.setattr(_util, "dma_request", lambda c, name, level=True: reqs.append((name, level,
                                                                                       chip.now_cycles())))
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    a1.set_channel(1, 111)
    a2.set_channel(2, 222)
    wr(chip, C_ADC + CCR, 0b00110 | (2 << 14))                     # regular simultaneous, DMA mode 2
    wr(chip, ADC1 + SQR3, 1)
    wr(chip, ADC2 + SQR3, 2)
    wr(chip, ADC2 + CR2, ADON)
    wr(chip, ADC2 + CR2, ADON | SWSTART)                           # a slave trigger is ignored
    assert any("multi-ADC mode" in str(e.fields.get("msg")) for e in chip.log if e.kind == "warn")
    d1, d2 = _times(chip, a1, "_done"), _times(chip, a2, "_done")
    t0 = _swstart(chip, cr2=CONT)
    chip.sched.at_cycles(t0 + CONV12 + 1, lambda: reqs.append(("CDR", rd(chip, C_ADC + CDR))))
    chip.run_cycles(CONV12 + 2)
    assert d1 == d2 == [t0 + CONV12]
    assert reqs == [("ADC1", True, t0 + CONV12), ("ADC1", False, t0 + CONV12 + 1), ("CDR", (222 << 16) | 111)]
    chip.run_cycles(3 * CONV)                                      # CDR not read any more: overrun stops both
    assert rd(chip, ADC1 + SR) & OVR and rd(chip, ADC2 + SR) & OVR
    n = a1.conversions
    chip.run_cycles(5 * CONV)
    assert a1.conversions == n


def test_dual_interleaved_delay_and_dma_mode1(desc, monkeypatch):
    monkeypatch.setattr(_util, "dma_request", lambda c, name, level=True: None)
    chip = _chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    a1.set_channel(0, 1234)
    wr(chip, C_ADC + CCR, 0b00111 | (3 << 8) | (1 << 14))          # interleaved, DELAY 3: 8 ADCCLK, DMA mode 1
    wr(chip, ADC2 + CR2, ADON)
    d1, d2 = _times(chip, a1, "_done"), _times(chip, a2, "_done")
    t0 = _swstart(chip)
    chip.run_cycles(CONV12 + 20)
    assert d1 == [t0 + CONV12] and d2 == [t0 + CONV12 + 8 * 2]
    assert [rd(chip, C_ADC + CDR) for _ in range(2)] == [1234, 1234]


def test_injected_simultaneous_and_alternate_trigger(desc):
    chip = _chip(desc)
    a1, a2, a3 = (chip.peripherals[f"ADC{i}"] for i in (1, 2, 3))
    for a in (a1, a2, a3):
        wr(chip, a.base + JSQR, jsqr(1))
        wr(chip, a.base + CR2, ADON)
    wr(chip, C_ADC + CCR, 0b10101)                                 # triple injected simultaneous
    wr(chip, ADC1 + CR2, ADON | JSWSTART)
    chip.run_cycles(INJ12 + 2)
    assert [a.injected_conversions for a in (a1, a2, a3)] == [1, 1, 1]
    wr(chip, C_ADC + CCR, 0b01001)                                 # dual alternate trigger
    for _ in range(3):
        wr(chip, ADC1 + CR2, ADON | JSWSTART)
        chip.run_cycles(INJ12 + 2)
    assert [a.injected_conversions for a in (a1, a2, a3)] == [3, 2, 1]
    wr(chip, ADC2 + CR2, ADON | JSWSTART)                          # slave: ignored
    chip.run_cycles(INJ12 + 2)
    assert a2.injected_conversions == 2


def test_triple_dma_mode2_packing_order(desc, monkeypatch):
    monkeypatch.setattr(_util, "dma_request", lambda c, name, level=True: None)
    chip = _chip(desc)
    adcs = [chip.peripherals[f"ADC{i}"] for i in (1, 2, 3)]
    for i, a in enumerate(adcs):
        a.set_channel(10 + i, 100 * (i + 1))
        wr(chip, a.base + SQR3, 10 + i)
    for a in adcs[1:]:
        wr(chip, a.base + CR2, ADON)
    wr(chip, C_ADC + CCR, 0b10110 | (2 << 14))
    _swstart(chip)
    chip.run_cycles(CONV12 + 2)
    w1 = rd(chip, C_ADC + CDR)
    wr(chip, ADC1 + CR2, ADON | SWSTART)
    chip.run_cycles(CONV12 + 2)
    assert w1 == (200 << 16) | 100                                  # ADC2:ADC1
    assert [rd(chip, C_ADC + CDR) for _ in range(2)] == [(100 << 16) | 300, (300 << 16) | 200]


def test_reserved_multi_mode_warns_once(desc):
    chip = _chip(desc)
    wr(chip, C_ADC + CCR, 0b00011)
    wr(chip, C_ADC + CCR, 0b00011 | (1 << 16))
    msgs = [e.fields.get("msg") for e in chip.log if e.kind == "warn" and "reserved multi-ADC" in str(e.fields)]
    assert len(msgs) == 1
