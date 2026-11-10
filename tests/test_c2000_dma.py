"""C2833x DMA (chipem/periph/ti_c2000/dma.py) on the TMS320F28232 with a spinning C28x core (SPRUI07 ch. 8):
register map and EALLOW, software and peripheral triggers (ADC SEQ1INT with ADCSYNC, ePWM SOCA, CPU timer, XINT),
burst/transfer/wrap address generation, 16/32-bit items, ONESHOT / CONTINUOUS, interrupts at the beginning or end
of a transfer, overflow, round-robin and CH1 high-priority arbitration, HALT / SOFTRESET / HARDRESET, DMA bus
access rules, the DMAENCLK gate.  Timings are exact: a burst of n items starts at the trigger cycle and lands at
+1 + 4n (SPRUI07 8.3)."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble

CHIP = Path(__file__).resolve().parents[1] / "chips" / "tms320f28232.yaml"
EALLOW = 1 << 6
PCLKCR0, PCLKCR1, PCLKCR3, WDCR, MAPCNF = 0x701C, 0x701D, 0x7020, 0x7029, 0x702E
DMACTRL, DEBUGCTRL, REVISION, PRIORITYCTRL1, PRIORITYSTAT = 0x1000, 0x1001, 0x1002, 0x1004, 0x1006
MODE, CONTROL, BURST_SIZE, BURST_COUNT, SRC_BURST_STEP, DST_BURST_STEP, TRANSFER_SIZE, TRANSFER_COUNT = range(8)
SRC_TRANSFER_STEP, DST_TRANSFER_STEP, SRC_WRAP_SIZE, SRC_WRAP_COUNT, SRC_WRAP_STEP = range(8, 13)
DST_WRAP_SIZE, DST_WRAP_COUNT, DST_WRAP_STEP = range(13, 16)
SRC_BEG_SH, SRC_ADDR_SH, SRC_BEG, SRC_ADDR, DST_BEG_SH, DST_ADDR_SH, DST_BEG, DST_ADDR = range(0x10, 0x20, 2)
# MODE
OVRINTE, PERINTE, CHINTMODE, ONESHOT, CONTINUOUS, SYNCE, SYNCSEL, DATASIZE, CHINTE = \
    1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14, 1 << 15
# CONTROL
RUN, HALT, SOFTRESET, PERINTFRC, PERINTCLR, SYNCFRC, SYNCCLR, ERRCLR = 1, 2, 4, 8, 16, 32, 64, 128
PERINTFLG, SYNCFLG, SYNCERR, TRANSFERSTS, BURSTSTS, RUNSTS, OVRFLG = \
    1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14
PIEIFR7 = 0x0CE3 + 2 * 6
ADCTRL1, ADCTRL2, ADCMAXCONV, ADCCHSELSEQ1, ADCCHSELSEQ2, ADCTRL3, ADCST = \
    0x7100, 0x7101, 0x7102, 0x7103, 0x7104, 0x7118, 0x7119
L4, L5 = 0xC000, 0xD000


def CH(n):
    return 0x1000 + 0x20 * n


@pytest.fixture
def chip():
    ch = Chip(load_chip(CHIP))
    ch.load_firmware(assemble('''
        .sect "boot", 0x33FFF6
        LB start
        .sect "code", 0x8000
start:  SB start,UNC
''').image())
    ch.reset()
    ch.core.write_reg("st1", ch.core.read_reg("st1") | EALLOW)       # debugger-style writes below need EALLOW
    w(ch, WDCR, 0x0068)                                              # watchdog off
    w(ch, PCLKCR3, 0x0F00)                                           # CPU timers + DMAENCLK
    return ch


def w(ch, a, v, n=1):
    ch.bus.write(a, n, v)


def r(ch, a, n=1):
    return ch.bus.read(a, n)


def warns(ch):
    return [e.get("msg") for e in ch.log if e.kind == "warn"]


def setup(ch, n, src, dst, *, burst=4, transfer=1, mode=PERINTE, sstep=1, dstep=1, sts=1, dts=1,
          swrap=0xFFFF, dwrap=0xFFFF, swstep=0, dwstep=0, sbeg=None, dbeg=None):
    b = CH(n)
    w(ch, b + MODE, mode)
    w(ch, b + BURST_SIZE, burst - 1)
    w(ch, b + SRC_BURST_STEP, sstep & 0xFFFF)
    w(ch, b + DST_BURST_STEP, dstep & 0xFFFF)
    w(ch, b + TRANSFER_SIZE, transfer - 1)
    w(ch, b + SRC_TRANSFER_STEP, sts & 0xFFFF)
    w(ch, b + DST_TRANSFER_STEP, dts & 0xFFFF)
    w(ch, b + SRC_WRAP_SIZE, swrap)
    w(ch, b + DST_WRAP_SIZE, dwrap)
    w(ch, b + SRC_WRAP_STEP, swstep & 0xFFFF)
    w(ch, b + DST_WRAP_STEP, dwstep & 0xFFFF)
    w(ch, b + SRC_BEG_SH, src if sbeg is None else sbeg, 2)
    w(ch, b + SRC_ADDR_SH, src, 2)
    w(ch, b + DST_BEG_SH, dst if dbeg is None else dbeg, 2)
    w(ch, b + DST_ADDR_SH, dst, 2)
    w(ch, b + CONTROL, PERINTCLR | SYNCCLR | ERRCLR)
    w(ch, b + CONTROL, RUN)


def run_to(ch, t):
    ch.run_cycles(t - ch.now_cycles())


def fill(ch, a, vals):
    for i, v in enumerate(vals):
        w(ch, a + i, v)


def mem(ch, a, n):
    return [r(ch, a + i) for i in range(n)]


def watch(ch, conds, n, t0=None):
    """Step `n` single cycles; {name: predicate(chip)} -> first cycle (relative to t0, default now) it holds."""
    t0 = ch.now_cycles() if t0 is None else t0
    out = {k: None for k in conds}
    for _ in range(n):
        ch.run_cycles(1)
        for k, f in conds.items():
            if out[k] is None and f(ch):
                out[k] = ch.now_cycles() - t0
    return out


# ------------------------------------------------------------------ registers
def test_register_map_eallow_and_masks(chip):
    assert [r(chip, a) for a in (DMACTRL, DEBUGCTRL, REVISION, PRIORITYCTRL1, PRIORITYSTAT)] == [0] * 5
    for n in range(1, 7):                                            # channel n at 0x1000 + 0x20 n
        w(chip, CH(n) + SRC_WRAP_STEP, 0x1230 + n)
        w(chip, CH(n) + DST_ADDR_SH, 0xFFFFFFFF, 2)
    assert [r(chip, CH(n) + SRC_WRAP_STEP) for n in range(1, 7)] == [0x1230 + n for n in range(1, 7)]
    assert r(chip, CH(2) + DST_ADDR_SH, 2) == 0x3FFFFF                # 22-bit pointers
    w(chip, CH(1) + DST_ADDR, 0x1234, 2)                             # active registers are read-only
    assert r(chip, CH(1) + DST_ADDR, 2) == 0
    w(chip, CH(1) + BURST_SIZE, 0xFFFF)
    assert r(chip, CH(1) + BURST_SIZE) == 0x1F                       # 5-bit
    w(chip, CH(1) + MODE, 0xFFFF)
    assert r(chip, CH(1) + MODE) == 0xFF9F                           # bits 6:5 reserved
    w(chip, CH(1) + MODE, 0)
    w(chip, CH(1) + CONTROL, PERINTFRC)                              # strobes read 0, status bits read-only
    assert r(chip, CH(1) + CONTROL) == PERINTFLG
    w(chip, PRIORITYCTRL1, 0xFFFF)
    assert r(chip, PRIORITYCTRL1) == 1
    w(chip, DEBUGCTRL, 0xFFFF)
    assert r(chip, DEBUGCTRL) == 0x8000
    chip.core.write_reg("st1", chip.core.read_reg("st1") & ~EALLOW)  # every DMA register is EALLOW protected
    w(chip, CH(3) + SRC_WRAP_STEP, 0)
    assert r(chip, CH(3) + SRC_WRAP_STEP) == 0x1233
    assert any("EALLOW" in m for m in warns(chip))


# ------------------------------------------------------------------ transfers and timing
def test_oneshot_software_transfer_timing(chip):
    fill(chip, L4, range(0x100, 0x108))
    setup(chip, 1, L4, L5, burst=4, transfer=2, mode=PERINTE | ONESHOT | CHINTE | CHINTMODE)
    assert r(chip, CH(1) + CONTROL) == RUNSTS
    w(chip, CH(1) + CONTROL, PERINTFRC)                              # software trigger at cycle 0
    assert mem(chip, L5, 8) == [0] * 8                               # never inside the triggering access
    t = watch(chip, {"b1": lambda c: r(c, L5) == 0x100, "b2": lambda c: r(c, L5 + 7) == 0x107,
                     "int": lambda c: r(c, PIEIFR7) & 1}, 40)
    assert t == {"b1": 17, "b2": 34, "int": 34}                      # 2 x (1 + 4 x 4) cycles, one trigger
    assert mem(chip, L5, 8) == list(range(0x100, 0x108))
    ctl = r(chip, CH(1) + CONTROL)
    assert not ctl & (RUNSTS | TRANSFERSTS | BURSTSTS | PERINTFLG)   # CONTINUOUS = 0: stopped
    assert r(chip, CH(1) + SRC_ADDR, 2) == L4 + 8 and r(chip, CH(1) + DST_ADDR, 2) == L5 + 8
    assert r(chip, CH(1) + TRANSFER_COUNT) == 0 and r(chip, CH(1) + BURST_COUNT) == 0


def test_burst_per_trigger_interrupt_at_beginning(chip):
    fill(chip, L4, range(1, 13))
    setup(chip, 2, L4, L5, burst=4, transfer=3, mode=PERINTE | CHINTE)          # CHINTMODE = 0: at the start
    w(chip, CH(2) + CONTROL, PERINTFRC)
    chip.run_cycles(1)
    assert r(chip, PIEIFR7) == 1 << 1                                # DINTCH2 when the transfer begins
    ctl = r(chip, CH(2) + CONTROL)
    assert ctl & (RUNSTS | TRANSFERSTS | BURSTSTS) == RUNSTS | TRANSFERSTS | BURSTSTS and not ctl & PERINTFLG
    assert r(chip, CH(2) + TRANSFER_COUNT) == 2 and r(chip, CH(2) + BURST_COUNT) == 3
    chip.run_cycles(20)
    assert mem(chip, L5, 8) == [1, 2, 3, 4, 0, 0, 0, 0]              # one burst per trigger
    assert r(chip, CH(2) + CONTROL) & (TRANSFERSTS | BURSTSTS) == TRANSFERSTS
    assert r(chip, CH(2) + SRC_ADDR, 2) == L4 + 4                    # last item + TRANSFER_STEP
    assert r(chip, CH(2) + TRANSFER_COUNT) == 1                      # decremented after the burst
    for _ in range(2):
        w(chip, CH(2) + CONTROL, PERINTFRC)
        chip.run_cycles(20)
    assert mem(chip, L5, 12) == list(range(1, 13))
    assert not r(chip, CH(2) + CONTROL) & RUNSTS
    w(chip, CH(2) + CONTROL, PERINTFRC)                              # stopped: latched, not served
    chip.run_cycles(40)
    assert r(chip, CH(2) + CONTROL) & PERINTFLG and r(chip, L5 + 12) == 0


def test_continuous_ping_pong_from_shadow(chip):
    fill(chip, L4, [0xA0, 0xA1, 0xB0, 0xB1])
    setup(chip, 1, L4, L5, burst=2, transfer=1, mode=PERINTE | CONTINUOUS | CHINTE)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(1)
    assert r(chip, PIEIFR7) & 1                                      # transfer start: "ISR" moves the shadow
    w(chip, 0x0CE1, 0xFFFF)
    w(chip, CH(1) + SRC_ADDR_SH, L4 + 2, 2)
    w(chip, CH(1) + DST_ADDR_SH, L5 + 0x10, 2)
    chip.run_cycles(20)
    assert mem(chip, L5, 2) == [0xA0, 0xA1]                          # active set unaffected by the shadow write
    ctl = r(chip, CH(1) + CONTROL)
    assert ctl & RUNSTS and not ctl & TRANSFERSTS                    # CONTINUOUS: re-armed
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(20)
    assert mem(chip, L5 + 0x10, 2) == [0xB0, 0xB1]
    assert r(chip, CH(1) + DST_BEG, 2) == L5 and r(chip, CH(1) + DST_ADDR, 2) == L5 + 0x12


def test_wrap_and_negative_steps(chip):
    fill(chip, L4, range(0x10, 0x30))
    # source: bursts of 4 from L4, wrap after 2 bursts to BEG + 0x10; destination written backwards
    setup(chip, 1, L4, L5 + 0x0F, burst=4, transfer=4, mode=PERINTE | ONESHOT, sts=1, dstep=-1, dts=-1,
          swrap=1, swstep=0x10)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(4 * 17)
    src = list(range(0x10, 0x18)) + list(range(0x20, 0x28))
    assert mem(chip, L5, 16) == src[::-1]
    assert r(chip, CH(1) + SRC_BEG, 2) == L4 + 0x10 and r(chip, CH(1) + SRC_ADDR, 2) == L4 + 0x18
    assert r(chip, CH(1) + SRC_WRAP_COUNT) == 0                      # 2nd wrap period ran out with the transfer
    assert r(chip, CH(1) + DST_ADDR, 2) == L5 - 1


def test_32bit_items(chip):
    fill(chip, L4, [0x1111, 0x2222, 0x3333, 0x4444, 0x5555, 0x6666])
    # BURST_SIZE counts 16-bit words: 6 words = 3 x 32 bit; steps of 2; odd pointers use the even word
    setup(chip, 1, L4 + 1, L5, burst=6, transfer=1, mode=PERINTE | DATASIZE, sstep=2, dstep=2)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    t = watch(chip, {"done": lambda c: r(c, L5 + 5) == 0x6666}, 20)
    assert t["done"] == 1 + 3 * 4                                    # 32-bit items at 4 cycles each
    assert mem(chip, L5, 6) == [0x1111, 0x2222, 0x3333, 0x4444, 0x5555, 0x6666]


# ------------------------------------------------------------------ peripheral triggers
def _timer0(ch, period):
    t = 0x0C00
    w(ch, t + 4, 0x0010)
    w(ch, t + 2, period - 1, 2)
    w(ch, t + 6, 0)
    w(ch, t + 4, 0x4020)                                             # TIE | TRB, run: TINT0 every `period`


def test_timer_trigger_overflow_and_errclr(chip):
    fill(chip, L4, [7])
    setup(chip, 3, L4, L5, burst=1, transfer=100, mode=PERINTE | OVRINTE | CHINTE | CHINTMODE | 11, sts=0)
    w(chip, CH(3) + CONTROL, HALT)                                   # not running: triggers only latch
    _timer0(chip, 100)
    chip.run_cycles(150)
    assert r(chip, CH(3) + CONTROL) & (PERINTFLG | OVRFLG) == PERINTFLG and r(chip, PIEIFR7) == 0
    chip.run_cycles(100)                                             # second TINT0 while PERINTFLG is set
    assert r(chip, CH(3) + CONTROL) & OVRFLG and r(chip, PIEIFR7) == 1 << 2
    assert chip.peripherals["DMA"].lost == 1
    w(chip, CH(3) + CONTROL, ERRCLR)
    assert not r(chip, CH(3) + CONTROL) & OVRFLG
    w(chip, CH(3) + CONTROL, PERINTFRC)                              # a forced trigger never overflows
    assert not r(chip, CH(3) + CONTROL) & OVRFLG
    w(chip, CH(3) + CONTROL, RUN)                                    # latched trigger served now
    chip.run_cycles(10)
    assert r(chip, L5) == 7 and r(chip, CH(3) + TRANSFER_COUNT) == 98


def test_timer_triggers_one_burst_each(chip):
    fill(chip, L4, [5])
    setup(chip, 1, L4, L5, burst=1, transfer=3, mode=PERINTE | 11, sts=0, dts=1)
    _timer0(chip, 1000)
    t0 = chip.now_cycles()
    t = watch(chip, {k: (lambda c, k=k: r(c, L5 + k) == 5) for k in range(3)}, 3100)
    exp = chip.peripherals["CPUTIMER0"]
    assert exp.expiries >= 3
    assert t == {0: 1000 + 5, 1: 2000 + 5, 2: 3000 + 5}              # TINT0 at 1000 cycles, burst 1 + 4
    assert not r(chip, CH(1) + CONTROL) & RUNSTS and chip.now_cycles() - t0 == 3100


def test_xint1_trigger(chip):
    fill(chip, L4, [0x55])
    setup(chip, 4, L4, L5, burst=1, transfer=1, mode=PERINTE | 3)    # PERINTSEL 3 = XINT1
    w(chip, 0x6FE0, 12)                                              # GPIOXINT1SEL = GPIO12
    w(chip, 0x7070, 0x0001)                                          # XINT1CR: enable, falling
    chip.pins.set_external("GPIO12", "H")
    chip.run_cycles(10)
    assert r(chip, L5) == 0
    chip.pins.set_external("GPIO12", "L")
    chip.run_cycles(5)
    assert r(chip, L5) == 0x55


def _adc(ch, mode1=0, ctrl2=1 << 11):
    w(ch, PCLKCR0, r(ch, PCLKCR0) | 1 << 3)                          # ADCENCLK
    w(ch, ADCTRL3, 0x00E0 | 4 << 1)                                  # powered up, ADCCLK = HSPCLK / 8
    w(ch, ADCTRL1, mode1)
    w(ch, ADCMAXCONV, 3)                                             # SEQ1: 4 conversions
    w(ch, ADCCHSELSEQ1, 0x3210)
    w(ch, ADCCHSELSEQ2, 0x3210)
    adc = ch.peripherals["ADC"]
    for i, v in enumerate((0x111, 0x222, 0x333, 0x444)):
        adc.set_channel(f"ADCINA{i}", v)
    w(ch, ADCTRL2, ctrl2)                                            # INT_ENA_SEQ1
    return adc


def test_adc_seq1int_to_dma_timing_and_source_clear(chip):
    adc = _adc(chip)
    setup(chip, 1, 0x0B00, L4 + 0x100, burst=4, transfer=2, mode=PERINTE | 1, sts=1, swrap=1, swstep=0)
    first, step, _, _ = adc._timing()
    eos = first + 3 * step
    w(chip, ADCTRL2, r(chip, ADCTRL2) | 1 << 13)                     # SOC_SEQ1 at cycle 0
    t = watch(chip, {"dma": lambda c: r(c, L4 + 0x103) == 0x444}, eos + 30)
    assert t["dma"] == eos + 17
    assert mem(chip, L4 + 0x100, 4) == [0x111, 0x222, 0x333, 0x444]
    assert not r(chip, ADCST) & 1                                    # INT_SEQ1 cleared by the DMA
    assert r(chip, 0x0CE3) & 1                                       # SEQ1INT still reached the PIE
    w(chip, ADCTRL2, r(chip, ADCTRL2) | 1 << 13)                     # second SOC: CONV04-07 -> RESULT4-7
    chip.run_cycles(eos + 30)
    assert mem(chip, L4 + 0x104, 4) == [0x111, 0x222, 0x333, 0x444]
    assert r(chip, CH(1) + SRC_ADDR, 2) == 0x0B08 and not r(chip, CH(1) + CONTROL) & RUNSTS


def test_adc_sync_waits_for_first_sync_and_flags_error(chip):
    adc = _adc(chip, mode1=(1 << 6) | (1 << 5))                      # CONT_RUN | SEQ_OVRD: ADCSYNC every 2nd EOS
    first, step, _, _ = adc._timing()
    period = 4 * step                                                # back-to-back sessions of 4 conversions
    t = chip.now_cycles()
    w(chip, ADCTRL2, r(chip, ADCTRL2) | 1 << 13)
    eos = [t + first + 3 * step + k * period for k in range(8)]      # even k: from RESULT0 (sync), odd: RESULT4
    run_to(chip, eos[0] + 5)
    setup(chip, 1, 0x0B00, L4 + 0x200, burst=4, transfer=0x100, mode=PERINTE | SYNCE | 1, sts=1, swrap=1,
          swstep=0)
    w(chip, ADCST, 1 << 4)                                           # INT_SEQ1_CLR: SEQ1INT pulses again
    dma = chip.peripherals["DMA"]
    run_to(chip, eos[1] + 17)                                        # non-sync EOS: ignored until the first sync
    assert dma.bursts == 0 and r(chip, L4 + 0x200) == 0 and not r(chip, ADCST) & 1
    run_to(chip, eos[2] + 16)
    assert r(chip, L4 + 0x200) == 0
    run_to(chip, eos[2] + 17)                                        # the sync EOS starts the transfer
    assert dma.bursts == 1 and mem(chip, L4 + 0x200, 4) == [0x111, 0x222, 0x333, 0x444]
    run_to(chip, eos[4] + 17)
    assert dma.bursts == 3 and not r(chip, CH(1) + CONTROL) & SYNCERR
    assert r(chip, CH(1) + SRC_ADDR, 2) == 0x0B04 and r(chip, CH(1) + SRC_WRAP_COUNT) == 0
    w(chip, CH(1) + CONTROL, SYNCFRC)                                # a sync while WRAP_COUNT != WRAP_SIZE
    run_to(chip, eos[5] + 17)
    assert r(chip, CH(1) + CONTROL) & SYNCERR                        # resync: WRAP_COUNT reloaded, ADDR = BEG
    assert r(chip, CH(1) + SRC_ADDR, 2) == 0x0B04 and r(chip, CH(1) + SRC_WRAP_COUNT) == 0
    w(chip, CH(1) + CONTROL, ERRCLR)
    run_to(chip, eos[6] + 17)                                        # ADC sync with WRAP_COUNT = 0: error again
    assert r(chip, CH(1) + CONTROL) & SYNCERR and r(chip, CH(1) + SRC_ADDR, 2) == 0x0B04
    w(chip, CH(1) + CONTROL, ERRCLR)
    run_to(chip, eos[7] + 17)
    assert not r(chip, CH(1) + CONTROL) & SYNCERR and r(chip, CH(1) + SRC_ADDR, 2) == 0x0B00


def test_epwm_soca_trigger(chip):
    w(chip, PCLKCR1, 1)                                              # EPWM1ENCLK
    E1 = 0x6800
    w(chip, E1 + 0x05, 999)                                          # TBPRD
    w(chip, E1 + 0x19, (1 << 11) | (2 << 8))                         # ETSEL: SOCA at CTR=PRD
    w(chip, E1 + 0x1A, 1 << 8)                                       # ETPS: SOCAPRD = 1st event
    w(chip, E1 + 0x00, 0x0030)                                       # up count, TBCLK = SYSCLKOUT
    fill(chip, L4, [0x77])
    setup(chip, 6, L4, L5, burst=1, transfer=2, mode=PERINTE | 18, sts=0)   # 18 = ePWM1 SOCA
    w(chip, PCLKCR0, r(chip, PCLKCR0) | 1 << 2)                      # TBCLKSYNC: counter starts
    t = watch(chip, {"a": lambda c: r(c, L5) == 0x77, "b": lambda c: r(c, L5 + 1) == 0x77}, 2100)
    assert t["b"] - t["a"] == 1000                                   # one burst per SOCA (every period)


# ------------------------------------------------------------------ priority
def test_round_robin_example_of_sprui07(chip):
    """SPRUI07 8.5.1: CH1, CH3, CH5 at once; CH2 during CH1; CH1, CH3, CH6 during CH5 -> 1 2 3 5 6 1 3."""
    for n in range(1, 7):
        fill(chip, L4 + 0x10 * n, [n] * 8)
        setup(chip, n, L4 + 0x10 * n, L5 + 0x10 * n, burst=4, transfer=4, mode=PERINTE)
    t0 = chip.now_cycles()
    for n in (1, 3, 5):
        w(chip, CH(n) + CONTROL, PERINTFRC)
    chip.run_cycles(5)
    w(chip, CH(2) + CONTROL, PERINTFRC)                              # during the CH1 burst

    def landed(n, k):
        return lambda c: r(c, L5 + 0x10 * n + 4 * k + 3) == n

    t = watch(chip, {(n, 0): landed(n, 0) for n in (1, 2, 3)}, 50, t0)
    for n in (1, 3, 6):                                               # during the CH5 burst (cycles 51..68)
        w(chip, CH(n) + CONTROL, PERINTFRC)
    t.update(watch(chip, {(5, 0): landed(5, 0), (6, 0): landed(6, 0), (1, 1): landed(1, 1), (3, 1): landed(3, 1)},
                   80, t0))
    order = sorted(t.items(), key=lambda kv: kv[1])
    assert [k[0] for k, v in order] == [1, 2, 3, 5, 6, 1, 3]
    assert [v for k, v in order] == [17, 34, 51, 68, 85, 102, 119]   # back to back, 1 + 4 x 4 cycles each
    assert r(chip, PRIORITYSTAT) == 0                                # idle again


def test_ch1_high_priority_preempts_mid_burst(chip):
    w(chip, PRIORITYCTRL1, 1)
    w(chip, DMACTRL, 2)                                              # PRIORITYRESET after changing it
    fill(chip, L4, range(0x200, 0x220))
    fill(chip, L4 + 0x100, [0xC1] * 4)
    setup(chip, 2, L4, L5, burst=32, transfer=1, mode=PERINTE)
    setup(chip, 1, L4 + 0x100, L5 + 0x100, burst=4, transfer=1, mode=PERINTE)
    w(chip, CH(2) + CONTROL, PERINTFRC)                              # CH2 burst: items at 1, 5, 9, ... ends 129
    chip.run_cycles(10)
    w(chip, CH(1) + CONTROL, PERINTFRC)                              # cycle 10: item 3 (at 9) in progress
    stat = []
    t = watch(chip, {"cut": lambda c: r(c, L5 + 2) == 0x202,
                     "ch1": lambda c: r(c, L5 + 0x103) == 0xC1,
                     "ch2": lambda c: r(c, L5 + 31) == 0x21F,
                     "_": lambda c: stat.append((c.now_cycles(), r(c, PRIORITYSTAT))) and False}, 150)
    assert t["cut"] == 3                                             # 3 items done at 13
    assert t["ch1"] == 20                                            # CH1 burst 13 .. 30 (1 + 16)
    assert t["ch2"] == 137                                           # resumes at 31 (+1), 29 items -> 147
    assert r(chip, L5 + 3) == 0x203
    assert (20, 0x21) in stat                                        # ACTIVESTS = CH1, shadow = CH2
    assert stat[-1][1] == 0


def test_round_robin_ch1_waits_without_high_priority(chip):
    fill(chip, L4, range(0x200, 0x220))
    fill(chip, L4 + 0x100, [0xC1] * 4)
    setup(chip, 2, L4, L5, burst=32, transfer=1, mode=PERINTE)
    setup(chip, 1, L4 + 0x100, L5 + 0x100, burst=4, transfer=1, mode=PERINTE)
    w(chip, CH(2) + CONTROL, PERINTFRC)
    chip.run_cycles(10)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    t = watch(chip, {"ch1": lambda c: r(c, L5 + 0x103) == 0xC1, "ch2": lambda c: r(c, L5 + 31) == 0x21F}, 150)
    assert t == {"ch2": 119, "ch1": 136}                             # 129, then 129 + 17


# ------------------------------------------------------------------ halt / resets
def test_halt_mid_burst_and_resume(chip):
    fill(chip, L4, range(0x300, 0x310))
    setup(chip, 1, L4, L5, burst=16, transfer=1, mode=PERINTE)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(10)
    w(chip, CH(1) + CONTROL, HALT)                                   # cycle 10: item 3 in progress
    assert not r(chip, CH(1) + CONTROL) & RUNSTS
    chip.run_cycles(100)
    assert mem(chip, L5, 4) == [0x300, 0x301, 0x302, 0]
    assert r(chip, CH(1) + CONTROL) & BURSTSTS and r(chip, CH(1) + BURST_COUNT) == 12
    assert r(chip, CH(1) + SRC_ADDR, 2) == L4 + 3
    w(chip, CH(1) + CONTROL, RUN)                                    # rest of the burst, no trigger needed
    t = watch(chip, {"end": lambda c: r(c, L5 + 15) == 0x30F}, 80)
    assert t["end"] == 1 + 13 * 4
    assert mem(chip, L5, 16) == list(range(0x300, 0x310))


def test_softreset_and_hardreset(chip):
    fill(chip, L4, range(0x400, 0x410))
    setup(chip, 1, L4, L5, burst=16, transfer=3, mode=PERINTE)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(6)
    w(chip, CH(1) + CONTROL, SOFTRESET)                              # completes the item in progress (2nd)
    chip.run_cycles(100)
    assert mem(chip, L5, 3) == [0x400, 0x401, 0]
    ctl = r(chip, CH(1) + CONTROL)
    assert not ctl & (RUNSTS | TRANSFERSTS | BURSTSTS)
    assert [r(chip, CH(1) + k) for k in (BURST_COUNT, TRANSFER_COUNT, SRC_WRAP_COUNT, DST_WRAP_COUNT)] == [0] * 4
    w(chip, CH(1) + CONTROL, RUN)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(5)
    w(chip, DMACTRL, 1)                                              # HARDRESET: aborts at once
    chip.run_cycles(100)
    assert r(chip, CH(1) + MODE) == 0 and r(chip, CH(1) + CONTROL) == 0 and r(chip, CH(1) + SRC_ADDR_SH, 2) == 0
    assert r(chip, L5 + 2) == 0 and r(chip, DMACTRL) == 0


# ------------------------------------------------------------------ DMA bus and clock gate
def test_access_rules(chip):
    fill(chip, 0x0400, [0x99])                                       # M1: not on the DMA bus
    setup(chip, 1, 0x0400, L5, burst=1, transfer=1, mode=PERINTE)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(10)
    assert r(chip, L5) == 0 and any("CH1: source address 0x000400" in m for m in warns(chip))
    fill(chip, L4, [0x1234])                                         # ePWM1 TBPRD via Peripheral Frame 3
    setup(chip, 2, L4, 0x5805, burst=1, transfer=1, mode=PERINTE)
    w(chip, CH(2) + CONTROL, PERINTFRC)
    chip.run_cycles(10)
    assert r(chip, 0x6805) == 0 and any("MAPEPWM" in m for m in warns(chip))
    w(chip, MAPCNF, 1)
    w(chip, PCLKCR1, 1)
    setup(chip, 3, L4, 0x5805, burst=1, transfer=1, mode=PERINTE)
    w(chip, CH(3) + CONTROL, PERINTFRC)
    chip.run_cycles(10)
    assert r(chip, 0x6805) == 0x1234
    setup(chip, 4, L4, 0xE000, burst=1, transfer=1, mode=PERINTE)   # L6: not on the F28232
    w(chip, CH(4) + CONTROL, PERINTFRC)
    chip.run_cycles(10)
    assert any("CH4: destination address 0x00E000" in m for m in warns(chip))
    fill(chip, 0x4000, [0xABCD])                                     # XINTF zone 0 -> zone 7
    setup(chip, 5, 0x4000, 0x200010, burst=1, transfer=1, mode=PERINTE)
    w(chip, CH(5) + CONTROL, PERINTFRC)
    chip.run_cycles(10)
    assert r(chip, 0x200010) == 0xABCD


def test_dmaenclk_gate(chip):
    w(chip, PCLKCR3, 0x0700)                                         # DMAENCLK = 0
    fill(chip, L4, [0x42])
    setup(chip, 1, L4, L5, burst=1, transfer=1, mode=PERINTE)
    w(chip, CH(1) + CONTROL, PERINTFRC)
    chip.run_cycles(20)
    assert r(chip, L5) == 0
    assert any("DMAENCLK" in m for m in warns(chip))
