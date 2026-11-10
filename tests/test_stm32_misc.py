"""STM32F4 CRC, RNG, DAC, WWDG and RTC models (chipem/periph/stm32/{crc,rng,dac,wwdg,rtc}.py).
16 MHz HSI, APB prescalers /1 unless set: every time is an exact core-cycle count (RTC/RNG: wall-clock ns)."""
import pytest

from chipem.periph.stm32 import _util
from chipem.periph.stm32.crc import crc32_words
from test_stm32 import GPIOA, PWR, RCC, desc, rd, spin_chip, wr  # noqa: F401  (desc: fixture)

CRC, RNG, DAC, WWDG, RTC, ADC1, EXTI, TIM6 = (0x40023000, 0x50060800, 0x40007400, 0x40002C00, 0x40002800,
                                              0x40012000, 0x40013C00, 0x40001000)
AHB1ENR, AHB2ENR, APB1ENR, APB2ENR, CSR, BDCR = 0x30, 0x34, 0x40, 0x44, 0x74, 0x70


def _chip(desc, **kw):
    chip = spin_chip(desc, clock_hz=16e6, **kw)
    wr(chip, RCC + AHB1ENR, rd(chip, RCC + AHB1ENR) | 1 | 1 << 12)        # GPIOA, CRC
    wr(chip, RCC + AHB2ENR, 1 << 6)                                       # RNG
    wr(chip, RCC + APB1ENR, 1 << 29 | 1 << 11 | 1 << 28 | 1 << 4)         # DAC, WWDG, PWR, TIM6
    wr(chip, RCC + APB2ENR, 1 << 8)                                       # ADC1
    return chip


def _probe(chip, times, addr):
    out = []
    for t in times:
        chip.sched.at_cycles(t, lambda: out.append(rd(chip, addr)))
    return out


def _irq(chip, n):
    return (16 + n) in chip.arch.asserted


def _warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


# ================================================================================================ CRC
def test_crc_known_values(desc):
    chip = _chip(desc)
    assert rd(chip, CRC) == 0xFFFFFFFF
    wr(chip, CRC, 0x12345678)
    assert rd(chip, CRC) == 0xDF8A8A2B                  # the classic STM32 CRC unit check value
    wr(chip, CRC + 8, 1)                                 # RESET
    assert rd(chip, CRC) == 0xFFFFFFFF and rd(chip, CRC + 8) == 0
    # HAL_CRC_Calculate over a uint32_t buffer = "12345678" stored little endian
    words = [int.from_bytes(b"1234", "little"), int.from_bytes(b"5678", "little")]
    for w in words:
        wr(chip, CRC, w)
    assert rd(chip, CRC) == crc32_words(words) == _mpeg2(b"43218765")      # each word's bytes MSB first
    # word-wise MSB first equals CRC-32/MPEG-2 over the big-endian byte stream ("123456789" check 0x0376E6E7)
    assert crc32_words([int.from_bytes(b"1234", "big"), int.from_bytes(b"5678", "big")]) == _mpeg2(b"12345678")
    assert _mpeg2(b"123456789") == 0x0376E6E7


def _mpeg2(data):
    c = 0xFFFFFFFF
    for b in data:
        c ^= b << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c


def test_crc_idr_survives_reset_bit_and_narrow_write_warns(desc):
    chip = _chip(desc)
    wr(chip, CRC + 4, 0x1A5)
    assert rd(chip, CRC + 4) == 0xA5                    # 8-bit scratch register
    wr(chip, CRC + 8, 1)
    assert rd(chip, CRC + 4) == 0xA5
    chip.bus.write(CRC, 1, 0x12)
    assert rd(chip, CRC) == crc32_words([0x12]) and any("32-bit words" in m for m in _warns(chip))


def test_crc_ahb1rstr_resets(desc):
    chip = _chip(desc)
    wr(chip, CRC, 1)
    wr(chip, RCC + 0x10, 1 << 12)                        # AHB1RSTR.CRCRST
    wr(chip, RCC + 0x10, 0)
    assert rd(chip, CRC) == 0xFFFFFFFF


# ================================================================================================ RNG
RNGEN, RNG_IE = 4, 8
DRDY, CECS, SECS, CEIS, SEIS = 1, 2, 4, 32, 64
HASH_RNG = 80


def _pll_on(chip):
    wr(chip, RCC, rd(chip, RCC) | 1 << 24)               # PLLON; reset PLLCFGR: 16/16 x 192 / Q4 = 48 MHz


def test_rng_ready_after_40_clocks_and_dr_read(desc):
    chip = _chip(desc)
    _pll_on(chip)
    rng = chip.peripherals["RNG"]
    assert rng.rng_hz() == pytest.approx(48e6)
    t0 = chip.now_ns()
    wr(chip, RNG, RNGEN | RNG_IE)
    # 40 / 48 MHz = 833.3 ns = 13.33 core cycles at 16 MHz
    c0 = chip.now_cycles()
    sr = _probe(chip, [c0 + 12, c0 + 15], RNG + 4)
    chip.run_cycles(16)
    assert sr == [0, DRDY] and _irq(chip, HASH_RNG)
    v1 = rd(chip, RNG + 8)
    assert rd(chip, RNG + 4) == 0 and not _irq(chip, HASH_RNG)
    chip.run_cycles(16)
    v2 = rd(chip, RNG + 8)
    assert v1 != v2 and rng.generated == 2
    assert chip.now_ns() - t0 >= 2 * 40 / 48e6 * 1e9


def test_rng_repeatable_seed_and_no_repeat(desc):
    vals = []
    for _ in range(2):
        chip = _chip(desc)
        _pll_on(chip)
        wr(chip, RNG, RNGEN)
        seq = []
        for _ in range(20):
            chip.run_cycles(15)
            assert rd(chip, RNG + 4) & DRDY
            seq.append(rd(chip, RNG + 8))
        vals.append(seq)
    assert vals[0] == vals[1]
    assert all(a != b for a, b in zip(vals[0], vals[0][1:]))


def test_rng_clock_error_without_pll(desc):
    chip = _chip(desc)
    wr(chip, RNG, RNGEN | RNG_IE)
    assert rd(chip, RNG + 4) == CECS | CEIS and _irq(chip, HASH_RNG)
    chip.run_cycles(100)
    assert not rd(chip, RNG + 4) & DRDY
    _pll_on(chip)                                        # clock good again: CECS clears, CEIS stays (rc_w0)
    assert rd(chip, RNG + 4) & (CECS | CEIS) == CEIS
    wr(chip, RNG + 4, 0)
    assert not _irq(chip, HASH_RNG)
    chip.run_cycles(20)
    assert rd(chip, RNG + 4) & DRDY


def test_rng_seed_error_hook(desc):
    chip = _chip(desc)
    _pll_on(chip)
    wr(chip, RNG, RNGEN | RNG_IE)
    chip.peripherals["RNG"].inject_seed_error()
    assert rd(chip, RNG + 4) & (SECS | SEIS) == SECS | SEIS and _irq(chip, HASH_RNG)


# ================================================================================================ DAC
EN, TEN, DMAEN, DMAUDRIE = 1, 4, 1 << 12, 1 << 13
CR, SWTRIGR, DHR12R1, DHR12L1, DHR8R1, DHR12R2, DHR12RD, DHR8RD, DOR1, DOR2, DSR = (0, 4, 8, 0xC, 0x10, 0x14, 0x20,
                                                                                   0x28, 0x2C, 0x30, 0x34)
TIM6_DAC = 54


def _analog(chip, pin):
    m = rd(chip, GPIOA)
    wr(chip, GPIOA, m | 3 << 2 * pin)


def test_dac_dhr_to_dor_one_apb1_cycle_and_voltage(desc):
    chip = _chip(desc)
    dac = chip.peripherals["DAC"]
    _analog(chip, 4)
    wr(chip, DAC + CR, EN)
    assert chip.pins.get("PA4").owner == "DAC_OUT1"
    wr(chip, RCC + 0x08, rd(chip, RCC + 0x08) | 5 << 10)     # PPRE1 /4: one APB1 cycle = 4 core cycles
    c0 = chip.now_cycles()
    wr(chip, DAC + DHR12R1, 2048)
    dor = _probe(chip, [c0 + 3, c0 + 5], DAC + DOR1)
    chip.run_cycles(8)
    assert dor == [0, 2048] and dac.voltage(1) == pytest.approx(1.65)
    ev = [e for e in chip.log if e.kind == "info" and e.fields.get("signal") == "DAC_OUT1"]
    assert ev[-1].fields["volts"] == pytest.approx(1.65)
    wr(chip, DAC + DHR12L1, 0xFFF0)
    wr(chip, DAC + DHR8R1, 0x80)
    chip.run_cycles(8)
    assert rd(chip, DAC + DOR1) == 0x800 and rd(chip, DAC + DHR12R1) == 0x800 and rd(chip, DAC + DHR8R1) == 0x80
    wr(chip, DAC + DHR12RD, 0x0FFF_0123)
    chip.run_cycles(8)
    assert rd(chip, DAC + DOR1) == 0x123 and rd(chip, DAC + DOR2) == 0   # channel 2 off: DHR only
    assert rd(chip, DAC + DHR12R2) == 0xFFF and rd(chip, DAC + DHR8RD) == (0x12 | 0xFF << 8)
    wr(chip, DAC + CR, 0)
    assert dac.voltage(1) is None and chip.pins.get("PA4").owner == "ADC12_IN4"


def test_dac_output_feeds_adc_on_the_same_pin(desc):
    chip = _chip(desc)
    _analog(chip, 4)
    chip.peripherals["ADC1"].set_channel("PA4", 0.5)
    wr(chip, DAC + CR, EN)
    wr(chip, DAC + DHR12R1, 1024)
    chip.run_cycles(4)
    wr(chip, ADC1 + 0x34, 4)                                 # SQ1 = IN4
    wr(chip, ADC1 + 8, 1)
    wr(chip, ADC1 + 8, 1 | 1 << 30)
    chip.run_cycles(200)
    assert rd(chip, ADC1 + 0x4C) == 1024
    wr(chip, DAC + CR, 0)                                    # the user's 0.5 V comes back
    assert chip.peripherals["ADC1"].volts(4) == pytest.approx(0.5)


def test_dac_pin_not_analog_warns(desc):
    chip = _chip(desc)
    wr(chip, DAC + CR, EN)
    assert any("not in analog mode" in m for m in _warns(chip))


def _tim6_update_every(chip, n):
    wr(chip, TIM6 + 0x04, 2 << 4)                            # CR2.MMS = update
    wr(chip, TIM6 + 0x2C, n - 1)                             # ARR
    wr(chip, TIM6, 1)                                        # CEN


def test_dac_tim6_trgo_trigger_three_apb1_cycles(desc):
    chip = _chip(desc)
    _analog(chip, 4)
    dac = chip.peripherals["DAC"]
    trg, outs = [], []
    chip.peripherals["TIM6"].add_trigger_listener("TRGO", lambda t, lvl: trg.append(t) if lvl else None)
    dac.listeners.append(lambda d, ch: outs.append(chip.now_cycles()))
    wr(chip, DAC + CR, EN | TEN)                             # TSEL1 = 000 TIM6_TRGO
    wr(chip, DAC + DHR12R1, 100)
    chip.run_cycles(50)
    assert rd(chip, DAC + DOR1) == 0                         # TEN = 1: no automatic transfer
    _tim6_update_every(chip, 100)
    chip.run_cycles(450)
    assert len(trg) >= 4 and [o - t for o, t in zip(outs[1:], trg)][:1] == [3]
    assert rd(chip, DAC + DOR1) == 100 and dac.conversions[0] == len(trg)


def test_dac_swtrig_and_triangle_wave(desc):
    chip = _chip(desc)
    _analog(chip, 4)
    wr(chip, DAC + CR, EN | TEN | 7 << 3 | 2 << 6 | 1 << 8)   # SWTRIG, triangle, MAMP=1 (amplitude 3)
    wr(chip, DAC + DHR12R1, 10)
    seq = []
    for _ in range(8):
        wr(chip, DAC + SWTRIGR, 1)
        assert rd(chip, DAC + SWTRIGR) == 1
        chip.run_cycles(2)
        assert rd(chip, DAC + SWTRIGR) == 0                  # cleared when DHR moved (one APB1 cycle)
        seq.append(rd(chip, DAC + DOR1))
    assert seq == [11, 12, 13, 12, 11, 10, 11, 12]


def test_dac_noise_wave_bounded_by_mamp(desc):
    chip = _chip(desc)
    wr(chip, DAC + CR, EN | TEN | 7 << 3 | 1 << 6 | 3 << 8)   # noise, MAMP=3: 4 LSBs
    vals = set()
    for _ in range(30):
        wr(chip, DAC + SWTRIGR, 1)
        chip.run_cycles(2)
        vals.add(rd(chip, DAC + DOR1))
    assert len(vals) > 4 and max(vals) <= 15


def test_dac_dma_request_and_underrun(desc):
    chip = _chip(desc)
    seen = []
    orig = _util.dma_request
    import chipem.periph.stm32.dac as dacmod
    dacmod.dma_request = lambda c, name, lvl=True: seen.append((name, lvl)) or True
    try:
        wr(chip, DAC + CR, EN | TEN | DMAEN | DMAUDRIE)
        _tim6_update_every(chip, 100)
        chip.run_cycles(150)
        assert seen == [("DAC1", True)]
        wr(chip, DAC + DHR12R1, 5)                           # the DMA's write acknowledges
        assert seen[-1] == ("DAC1", False)
        chip.run_cycles(100)                                 # next trigger: new request
        chip.run_cycles(100)                                 # and another one without ack: underrun
        assert rd(chip, DAC + DSR) & 1 << 13 and _irq(chip, TIM6_DAC)
        wr(chip, DAC + DSR, 1 << 13)                         # rc_w1
        assert not rd(chip, DAC + DSR) and not _irq(chip, TIM6_DAC)
    finally:
        dacmod.dma_request = orig


# ================================================================================================ WWDG
WDGA, EWI = 0x80, 0x200
WWDG_IRQ = 0


def _resets(chip):
    return [e.fields.get("reason") for e in chip.log if e.kind == "reset"]


def test_wwdg_counter_ewi_and_timeout_reset(desc):
    chip = _chip(desc)
    wr(chip, WWDG + 4, 0x7F | EWI)                           # W = 0x7F (no window), EWI
    c0 = chip.now_cycles()
    wr(chip, WWDG, WDGA | 0x7F)
    tick = 4096
    cr = _probe(chip, [c0 + tick - 1, c0 + tick + 1, c0 + 10 * tick + 1], WWDG)
    sr = _probe(chip, [c0 + 63 * tick - 1, c0 + 63 * tick + 1], WWDG + 8)
    chip.run_cycles(63 * tick + 10)
    assert cr == [WDGA | 0x7F, WDGA | 0x7E, WDGA | 0x75]
    assert sr == [0, 1] and _irq(chip, WWDG_IRQ)
    wr(chip, WWDG + 8, 0)
    assert not _irq(chip, WWDG_IRQ)
    assert "wwdg" not in _resets(chip)
    chip.run_cycles(tick)
    assert "wwdg" in _resets(chip)
    assert rd(chip, RCC + CSR) & 1 << 30                     # WWDGRSTF
    assert rd(chip, WWDG) == 0x7F                            # reset value, WDGA off


def test_wwdg_refresh_inside_window_keeps_running(desc):
    chip = _chip(desc)
    wr(chip, WWDG + 4, 0x50 | 1 << 7)                        # W = 0x50, WDGTB = 1: tick 8192 cycles
    wr(chip, WWDG, WDGA | 0x7F)
    for _ in range(3):
        chip.run_cycles(0x30 * 8192 + 5)                     # counter 0x4F: inside the window
        assert rd(chip, WWDG) & 0x7F == 0x4F
        wr(chip, WWDG, WDGA | 0x7F)
    chip.run_cycles(10)
    assert "wwdg" not in _resets(chip)


def test_wwdg_refresh_too_early_resets(desc):
    chip = _chip(desc)
    wr(chip, WWDG + 4, 0x50)
    wr(chip, WWDG, WDGA | 0x7F)
    chip.run_cycles(4096 * 3)
    wr(chip, WWDG, WDGA | 0x7F)                              # counter 0x7C > W
    chip.run_cycles(2)
    assert "wwdg" in _resets(chip)
    assert any("outside the window" in m for m in _warns(chip))


def test_wwdg_write_t6_zero_resets_and_apb1_prescaler(desc):
    chip = _chip(desc)
    wr(chip, RCC + 0x08, rd(chip, RCC + 0x08) | 4 << 10)     # PPRE1 /2
    assert chip.peripherals["WWDG"].tick_cycles() == 2 * 4096
    wr(chip, WWDG, WDGA | 0x3F)
    chip.run_cycles(2)
    assert "wwdg" in _resets(chip)


def test_wwdg_free_running_when_disabled_no_reset(desc):
    chip = _chip(desc)
    chip.run_cycles(4096 * 0x45)
    assert rd(chip, WWDG) & WDGA == 0 and rd(chip, WWDG + 8) == 1    # EWIF set at 0x40 also when disabled
    assert "wwdg" not in _resets(chip)


# ================================================================================================ RTC
TR, DR_, RCR, ISR, PRER, WUTR, ALRMAR, WPR, SSR, ALRMASSR, BKP0R = (0, 4, 8, 0xC, 0x10, 0x14, 0x1C, 0x24, 0x28, 0x44,
                                                                   0x50)
INIT, INITF, RSF, INITS, ALRAF, WUTF = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 8, 1 << 10
ALRAE, ALRAIE, WUTE, WUTIE, FMT, BYPSHAD = 1 << 8, 1 << 12, 1 << 10, 1 << 14, 1 << 6, 1 << 5
RTC_ALARM_IRQ, RTC_WKUP_IRQ = 41, 3


def _sleepy(desc):
    """The chip with a firmware that sleeps in WFI (time fast-forwards to the next event)."""
    import struct
    from chipem.chip import Chip
    from chipem.firmware import FirmwareImage
    from engine_util import asm
    code = asm("spin: wfi\n b spin", 0x08000200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, 0x08000201)
    img[0x200:] = code
    chip = Chip(desc, clock_hz=16e6)
    chip.load_firmware(FirmwareImage([(0x08000000, bytes(img))]))
    chip.reset()
    wr(chip, RCC + APB1ENR, 1 << 28)                          # PWREN
    return chip


def _rtc_on(chip, sel=1):
    wr(chip, PWR, rd(chip, PWR) | 1 << 8)                     # DBP
    if sel == 2:
        wr(chip, RCC + CSR, 1)                                # LSION
    wr(chip, RCC + BDCR, 1 | sel << 8 | 1 << 15)              # LSEON, RTCSEL, RTCEN
    wr(chip, RTC + WPR, 0xCA)
    wr(chip, RTC + WPR, 0x53)


def _dr(yy, wd, mo, dd):
    """RTC_DR value from BCD fields (YT/YU 23-16, WDU 15-13, MT/MU 12-8, DT/DU 5-0)."""
    return yy << 16 | wd << 13 | mo << 8 | dd


def _set_time(chip, tr, dr, cr=0):
    wr(chip, RTC + ISR, INIT)
    assert rd(chip, RTC + ISR) & INITF
    if cr:
        wr(chip, RTC + RCR, cr)
    wr(chip, RTC + TR, tr)
    wr(chip, RTC + DR_, dr)
    wr(chip, RTC + ISR, 0)
    return chip.now_ns()


def test_rtc_calendar_runs_from_lse_across_leap_day(desc):
    chip = _sleepy(desc)
    _rtc_on(chip)
    t0 = _set_time(chip, 0x235958, _dr(0x24, 3, 0x02, 0x28))                # Wed 2024-02-28 23:59:58
    assert rd(chip, RTC + ISR) & (INITS | INIT) == INITS
    chip.run_for(500e6 + 1e3)
    assert rd(chip, RTC + SSR) == 255 - 128                   # 0.5 s of ck_apre (256 Hz)
    assert rd(chip, RTC + TR) == 0x235958 and rd(chip, RTC + DR_) == _dr(0x24, 3, 0x02, 0x28)
    chip.run_for(t0 + 3e9 + 1e3 - chip.now_ns())
    assert rd(chip, RTC + TR) == 0x000001 and rd(chip, RTC + DR_) == _dr(0x24, 4, 0x02, 0x29)     # Thu 2024-02-29
    assert str(chip.peripherals["RTC"].now_calendar()).startswith("2024-02-29")


def test_rtc_write_protection_and_dbp(desc):
    chip = _sleepy(desc)
    wr(chip, RCC + BDCR, 1 | 1 << 8 | 1 << 15)
    wr(chip, RTC + BKP0R, 0x1234)                             # DBP = 0: ignored
    assert rd(chip, RTC + BKP0R) == 0 and any("DBP" in m for m in _warns(chip))
    wr(chip, PWR, 1 << 8)
    wr(chip, RTC + BKP0R, 0x1234)                             # backup registers are not write protected
    assert rd(chip, RTC + BKP0R) == 0x1234
    wr(chip, RTC + ISR, INIT)                                 # locked: ignored
    assert not rd(chip, RTC + ISR) & INITF
    wr(chip, RTC + PRER, 0)
    assert rd(chip, RTC + PRER) == 0x7F00FF
    wr(chip, RTC + WPR, 0xCA)
    wr(chip, RTC + WPR, 0x11)                                 # wrong key locks again
    wr(chip, RTC + WPR, 0x53)
    wr(chip, RTC + ISR, INIT)
    assert not rd(chip, RTC + ISR) & INITF
    wr(chip, RTC + WPR, 0xCA)
    wr(chip, RTC + WPR, 0x53)
    wr(chip, RTC + TR, 0x120000)                              # outside INIT: ignored
    assert rd(chip, RTC + TR) == 0
    wr(chip, RTC + ISR, INIT)
    wr(chip, RTC + PRER, 0x7F00FF - 1)
    assert rd(chip, RTC + PRER) == 0x7F00FE


def test_rtc_shadow_lock_tr_then_dr_and_rsf(desc):
    chip = _sleepy(desc)
    _rtc_on(chip)
    t0 = _set_time(chip, 0x235959, _dr(0x24, 2, 0x12, 0x31))               # Tue 2024-12-31 23:59:59
    assert not rd(chip, RTC + ISR) & RSF
    chip.run_for(2 / 32768 * 1e9 + 100)
    assert rd(chip, RTC + ISR) & RSF                          # 2 RTCCLK after the restart
    chip.run_for(t0 + 0.999e9 - chip.now_ns())
    assert rd(chip, RTC + TR) == 0x235959                     # locks the date ...
    chip.run_for(0.01e9)
    assert rd(chip, RTC + DR_) == _dr(0x24, 2, 0x12, 0x31)                  # ... still the old day
    assert rd(chip, RTC + DR_) == _dr(0x25, 3, 0x01, 0x01)                 # next read: Wed 2025-01-01
    wr(chip, RTC + ISR, rd(chip, RTC + ISR) & ~RSF)
    assert not rd(chip, RTC + ISR) & RSF
    chip.run_for(1e5)
    assert rd(chip, RTC + ISR) & RSF


def test_rtc_alarm_a_sets_flag_at_the_second_and_exti17_irq(desc):
    chip = _sleepy(desc)
    _rtc_on(chip)
    wr(chip, EXTI, 1 << 17)                                   # IMR17
    wr(chip, EXTI + 8, 1 << 17)                               # RTSR17
    t0 = _set_time(chip, 0x101000, _dr(0x24, 1, 0x1, 0x01))
    wr(chip, RTC + RCR, 0)
    assert rd(chip, RTC + ISR) & 1                            # ALRAWF
    wr(chip, RTC + ALRMAR, 1 << 31 | 1 << 23 | 1 << 15 | 0x03)   # seconds = 03 of any minute/hour/date
    wr(chip, RTC + RCR, ALRAE | ALRAIE)
    hits = []
    for dt in (2.999e9, 3.0e9 + 2e3):
        chip.sched.at_ns(t0 + dt, lambda: hits.append(bool(chip.bus.read(RTC + ISR, 4) & ALRAF)))
    chip.run_for(t0 + 3.1e9 - chip.now_ns())
    assert hits == [False, True] and (16 + RTC_ALARM_IRQ) in chip.arch.pending | chip.arch.asserted
    assert rd(chip, EXTI + 0x14) & 1 << 17
    wr(chip, RTC + ISR, ~(ALRAF | INIT) & 0xFFFF)                 # HAL: flags 1 except ALRAF, INIT kept
    wr(chip, EXTI + 0x14, 1 << 17)
    assert not rd(chip, RTC + ISR) & ALRAF and (16 + RTC_ALARM_IRQ) not in chip.arch.asserted
    chip.run_for(70e9)                                        # next match a minute later (seconds = 03)
    assert chip.peripherals["RTC"].alarms[0] == 2


def test_rtc_wakeup_timer_rtcclk_div16_and_exti22(desc):
    chip = _sleepy(desc)
    _rtc_on(chip)
    wr(chip, EXTI, 1 << 22)
    wr(chip, EXTI + 8, 1 << 22)
    wr(chip, RTC + WUTR, 2047)                                # 2048 x 16 / 32768 Hz = 1 s
    t0 = chip.now_ns()
    wr(chip, RTC + RCR, WUTE | WUTIE)                         # WUCKSEL = 000: RTCCLK / 16
    assert not rd(chip, RTC + ISR) & 4                        # WUTWF = 0 while WUTE
    wr(chip, RTC + WUTR, 5)
    assert rd(chip, RTC + WUTR) == 2047
    hits = []
    for dt in (0.999e9, 1.0e9 + 1e3):
        chip.sched.at_ns(t0 + dt, lambda: hits.append(bool(chip.bus.read(RTC + ISR, 4) & WUTF)))
    chip.run_for(1.5e9)
    assert hits == [False, True] and (16 + RTC_WKUP_IRQ) in chip.arch.asserted
    chip.run_for(t0 + 3.2e9 - chip.now_ns())
    assert chip.peripherals["RTC"].wakeups == 3


def test_rtc_wakeup_ck_spre(desc):
    chip = _sleepy(desc)
    _rtc_on(chip)
    t0 = _set_time(chip, 0, _dr(0x24, 1, 0x1, 0x01))
    wr(chip, RTC + WUTR, 1)                                   # 2 ck_spre periods
    chip.run_for(0.25e9)
    wr(chip, RTC + RCR, WUTE | 4)                             # WUCKSEL = 10x
    chip.run_for(t0 + 1.999e9 - chip.now_ns())
    assert not rd(chip, RTC + ISR) & WUTF
    chip.run_for(0.002e9)
    assert rd(chip, RTC + ISR) & WUTF                         # 2nd second edge after enabling


def test_rtc_backup_domain_survives_system_reset_bdrst_clears(desc):
    chip = _sleepy(desc)
    _rtc_on(chip)
    _set_time(chip, 0x080000, _dr(0x24, 1, 0x1, 0x01))
    wr(chip, RTC + BKP0R + 4 * 19, 0xCAFE)
    chip.run_for(2e9)
    chip.reset("watchdog")
    assert rd(chip, RTC + BKP0R + 4 * 19) == 0xCAFE
    assert rd(chip, RTC + TR) == 0x080002 and rd(chip, RTC + RCR) == 0
    wr(chip, RTC + TR, 0)                                     # DBP is 0 again after the reset
    assert any("DBP" in m for m in _warns(chip))
    wr(chip, PWR, 1 << 8)
    wr(chip, RCC + BDCR, 1 << 16)                             # BDRST
    assert rd(chip, RTC + BKP0R + 4 * 19) == 0 and rd(chip, RTC + DR_) == 0x2101


def test_rtc_12h_format_and_lsi(desc):
    chip = _sleepy(desc)
    _rtc_on(chip, sel=2)
    rtc = chip.peripherals["RTC"]
    t0 = _set_time(chip, 1 << 22 | 0x115959, _dr(0x24, 1, 0x1, 0x01), cr=FMT)     # 11:59:59 PM
    assert rtc._hz == 32000.0
    chip.run_for(t0 + 1.01e9 * (32768 / 32000) - chip.now_ns())      # PREDIV 128 x 256 = 32768 LSI ticks
    assert rd(chip, RTC + TR) == 0x120000                             # 12:00:00 AM (midnight)
    rd(chip, RTC + DR_)


def test_rtc_no_clock_no_count(desc):
    chip = _sleepy(desc)
    wr(chip, PWR, 1 << 8)
    chip.run_for(2e9)
    assert rd(chip, RTC + TR) == 0 and rd(chip, RTC + DR_) == 0x2101
