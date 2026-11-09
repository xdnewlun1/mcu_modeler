"""Kinetis FlexTimer inputs and outputs to other modules (chipem/periph/kinetis/ftm.py) on the K22F description:
input capture and dual edge capture from pin edges, fault control, the trigger output to the PDB (EXTTRIG), DMA
requests, the external clock (FTM_CLKINx), write protection, the quadrature decoder and the CNTIN-above-counter
stretch. K22P100M120SF5V2RM chapter 38."""
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from engine_util import build_image

K22 = Path(__file__).resolve().parent.parent / "chips" / "mk22fn1m0vll12.yaml"
SIM_SOPT4, SIM_SCGC5, SIM_SCGC6, SIM_SCGC7, SIM_CLKDIV1 = 0x4004800C, 0x40048038, 0x4004803C, 0x40048040, 0x40048044
PORTA, PORTB, PORTC = 0x40049000, 0x4004A000, 0x4004B000
FTM0, FTM1, PDB0, DMA, DMAMUX = 0x40038000, 0x40039000, 0x40036000, 0x40008000, 0x40021000
RAM = 0x20000100
SC, CNT, MOD, C0SC, C0V, C1SC, C1V = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18
CNTIN, STATUS, MODE, SYNC, COMBINE, EXTTRIG, POL, FMS, FLTCTRL, QDCTRL, FLTPOL = \
    0x4C, 0x50, 0x54, 0x58, 0x64, 0x6C, 0x70, 0x74, 0x7C, 0x80, 0x88
CLK_SYS, CLK_EXT, CPWMS, TOF, TOIE = 1 << 3, 3 << 3, 1 << 5, 1 << 7, 1 << 6
FTMEN, WPDIS, WPEN = 1 << 0, 1 << 2, 1 << 6
CHF, CHIE, MSB, MSA, ELSB, ELSA, CDMA = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 3, 1 << 2, 1 << 0
FAULTF, FAULTIN, FAULTIE = 1 << 7, 1 << 5, 1 << 7
PS3 = 3                                                    # /8: 7.5 MHz from the 60 MHz bus, a tick = 16 core cycles
FTM0_IRQ, PDB_IRQ = 42, 52


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def w(chip, a, v):
    chip.bus.write(a, 4, v)


def r(chip, a):
    return chip.bus.read(a, 4)


@pytest.fixture
def chip(desc):
    c = Chip(desc, clock_hz=120e6)
    img, _ = build_image({"reset": "spin:\n  b spin"})
    c.load_firmware(img)
    c.reset()
    c.clock_override = 120e6
    c.peripherals["MCG"].frozen = True
    c.clock.set_hz(120e6)
    w(c, SIM_CLKDIV1, 1 << 24)                              # bus 60 MHz
    w(c, SIM_SCGC5, r(c, SIM_SCGC5) | (0x1F << 9))           # PORTA..E
    w(c, SIM_SCGC6, r(c, SIM_SCGC6) | (1 << 24) | (1 << 25) | (1 << 22) | (1 << 1))   # FTM0 FTM1 PDB DMAMUX
    w(c, SIM_SCGC7, r(c, SIM_SCGC7) | (1 << 1))              # DMA
    w(c, PORTC + 4 * 1, 4 << 8)                              # PTC1 = FTM0_CH0
    w(c, PORTC + 4 * 2, 4 << 8)                              # PTC2 = FTM0_CH1
    return c


def tk(chip, name="FTM0"):
    return 1e9 / chip.peripherals[name].counter_hz()


def run_to(chip, t0, n, name="FTM0"):
    """Run until n FTM ticks after t0."""
    chip.run_for(t0 + n * tk(chip, name) - chip.now_ns())


def start(chip, mod=0xFFFF, ps=PS3, base=FTM0):
    w(chip, base + MOD, mod)
    w(chip, base + SC, CLK_SYS | ps)
    return chip.now_ns()


# ------------------------------------------------------------------------------------------- input capture
def test_input_capture_rising_edge_latches_the_counter(chip):
    """ELS=01: CnV = the counter 3 system clocks after the edge (synchronizer), CHnF and the FTM interrupt."""
    w(chip, FTM0 + C1SC, ELSA | CHIE)
    pin = chip.pins.get("PTC2")
    assert pin.direction == "in" and pin.owner == "FTM0_CH1"     # the FTM does not drive a capture pin
    t0 = start(chip)
    run_to(chip, t0, 100.5)
    chip.pins.set_external("PTC2", 1)                      # rising at tick 100.5: captured at 100.875 -> 100
    assert not r(chip, FTM0 + C1SC) & CHF                  # not yet: 3 system clocks of synchronizer
    run_to(chip, t0, 102)
    assert r(chip, FTM0 + C1SC) & CHF and r(chip, FTM0 + C1V) == 100
    assert (16 + FTM0_IRQ) in chip.arch.asserted
    w(chip, FTM0 + C1SC, ELSA | CHIE)                      # read above, write 0: clear
    assert (16 + FTM0_IRQ) not in chip.arch.asserted
    run_to(chip, t0, 200.75)
    chip.pins.set_external("PTC2", 0)                      # falling: not selected
    run_to(chip, t0, 300.75)
    chip.pins.set_external("PTC2", 1)                      # rising at 300.75: +3/8 tick -> 301
    run_to(chip, t0, 303)
    assert r(chip, FTM0 + C1V) == 301 and chip.peripherals["FTM0"].captures == 2


def test_pin_muxed_before_the_channel_is_configured_is_never_driven(chip):
    """ELSnB:ELSnA = 00 means "pin not used for FTM": muxing PTA12 to FTM1_CH0 with C0SC = 0 leaves it an input
    (no L glitch onto a wire driven from outside), and configuring capture later keeps it one; a PWM channel that is
    already configured drives from the mux instant."""
    ev = []
    chip.pins.subscribe(lambda e: e.pin == "PTA12" and ev.append((e.field, e.new)))
    chip.pins.set_external("PTA12", 1)                     # the wire is driven high from outside
    w(chip, PORTA + 4 * 12, 3 << 8)                         # PTA12 = FTM1_CH0, C0SC still 0
    pin = chip.pins.get("PTA12")
    assert pin.owner == "FTM1_CH0" and pin.direction == "in" and pin.level() == "H"
    w(chip, FTM1 + C0SC, ELSA)                              # input capture configured afterwards
    assert pin.direction == "in" and pin.level() == "H"
    assert [x for x in ev if x[0] in ("level", "direction")] == [("level", "H"), ("direction", "in")]
    assert ("direction", "out") not in ev
    # FTM0 CH2 configured as EPWM before PTC3 is muxed to it
    w(chip, FTM0 + 0x20, 50)                                # C2V
    w(chip, FTM0 + 0x1C, MSB | ELSB)                        # C2SC: EPWM
    start(chip, mod=99)
    w(chip, PORTC + 4 * 3, 4 << 8)                          # PTC3 = FTM0_CH2: an output at once
    assert chip.pins.get("PTC3").direction == "out" and chip.pins.get("PTC3").level() == "H"
    w(chip, FTM0 + 0x1C, MSB)                               # ELS = 00: released to an input
    assert chip.pins.get("PTC3").direction == "in"


def test_input_capture_both_edges_and_cnv_writes_ignored(chip):
    w(chip, FTM0 + C1SC, ELSA | ELSB)
    t0 = start(chip)
    run_to(chip, t0, 40.25)
    chip.pins.set_external("PTC2", 1)
    run_to(chip, t0, 50)
    assert r(chip, FTM0 + C1V) == 40
    w(chip, FTM0 + C1V, 1234)                              # ignored in capture mode (38.3.7)
    assert r(chip, FTM0 + C1V) == 40
    run_to(chip, t0, 70.25)
    chip.pins.set_external("PTC2", 0)
    run_to(chip, t0, 80)
    assert r(chip, FTM0 + C1V) == 70


def test_capture_of_a_scheduled_edge_and_a_stopped_counter(chip):
    """An edge from a scheduler event (as `--at` injects it) and a capture while the counter clock is off."""
    w(chip, FTM0 + C1SC, ELSA)
    t0 = start(chip)
    chip.sched.at_ns(t0 + 500.5 * tk(chip), lambda: chip.pins.set_external("PTC2", 1))
    run_to(chip, t0, 510)
    assert r(chip, FTM0 + C1V) == 500
    w(chip, FTM0 + SC, 0)                                  # counter stopped at 510
    w(chip, FTM0 + C1SC, ELSA)
    chip.pins.set_external("PTC2", 0)
    chip.pins.set_external("PTC2", 1)
    chip.run_for(1000)
    assert r(chip, FTM0 + C1V) == r(chip, FTM0 + CNT) == 510


def test_pwm_period_measured_through_a_wire(chip):
    """FTM0 CH0 EPWM on PTC1 wired to PTA12 (FTM1_CH0, input capture on rising edges, FTM1 free running): the
    difference of two captures is the PWM period in FTM1 ticks."""
    w(chip, PORTA + 4 * 12, 3 << 8)                         # PTA12 = FTM1_CH0
    chip.pins.subscribe(lambda e: e.pin == "PTC1" and e.field == "level"
                        and chip.pins.set_external("PTA12", e.new))
    w(chip, FTM1 + C0SC, ELSA | CHIE)
    start(chip, mod=0, ps=0, base=FTM1)                    # FTM1: 60 MHz, free running
    w(chip, FTM0 + C0V, 300)
    w(chip, FTM0 + C0SC, MSB | ELSB)
    start(chip, mod=999, ps=1)                              # FTM0: 30 MHz, 1000 ticks = 2000 FTM1 ticks
    caps = []
    for _ in range(4):
        chip.run_for(1000 * tk(chip))
        caps.append(r(chip, FTM1 + C0V))
        w(chip, FTM1 + C0SC, ELSA | CHIE)
    assert [(b - a) & 0xFFFF for a, b in zip(caps, caps[1:])] == [2000, 2000, 2000]


def test_dual_edge_one_shot_pulse_width(chip):
    """DECAPEN0, CH0 rising, CH1 falling, MS0A=0: C0V/C1V = rising/falling counter, DECAP clears, CH1F set;
    C1V is transferred when C0V is read (read coherency)."""
    w(chip, FTM0 + C0SC, ELSA)
    w(chip, FTM0 + C1SC, ELSB | CHIE)
    w(chip, FTM0 + COMBINE, 0x04 | 0x08)                   # DECAPEN0 | DECAP0
    assert chip.pins.get("PTC1").direction == "in"
    t0 = start(chip)
    for n, lvl in ((100.25, 0), (120.25, 1), (170.25, 0), (220.25, 1), (260.25, 0)):
        run_to(chip, t0, n)
        chip.pins.set_external("PTC1", lvl)
    run_to(chip, t0, 300)
    assert r(chip, FTM0 + C0SC) & CHF and r(chip, FTM0 + C1SC) & CHF
    assert not r(chip, FTM0 + COMBINE) & 0x08               # one-shot: DECAP cleared after the second edge
    assert r(chip, FTM0 + C0V) == 120
    assert r(chip, FTM0 + C1V) == 170                       # pulse width 50 ticks; later edges ignored


def test_dual_edge_continuous_period_measurement(chip):
    w(chip, FTM0 + C0SC, ELSA | MSA)                       # continuous
    w(chip, FTM0 + C1SC, ELSA)                             # both rising: period
    w(chip, FTM0 + COMBINE, 0x04 | 0x08)
    t0 = start(chip)
    for k in range(6):                                     # rising edges at 100, 400, 700, ...
        run_to(chip, t0, 100.25 + 300 * k)
        chip.pins.set_external("PTC1", 1)
        run_to(chip, t0, 250 + 300 * k)
        chip.pins.set_external("PTC1", 0)
    run_to(chip, t0, 1900)
    assert r(chip, FTM0 + COMBINE) & 0x08                   # continuous: DECAP stays set
    v0 = r(chip, FTM0 + C0V)
    v1 = r(chip, FTM0 + C1V)
    assert (v0, v1) == (1300, 1600) and v1 - v0 == 300


# ------------------------------------------------------------------------------------------- fault control
def fault_setup(chip, faultm, pol=0):
    w(chip, PORTB + 4 * 3, 6 << 8)                          # PTB3 = FTM0_FLT0
    chip.pins.set_external("PTB3", 1 if pol else 0)
    w(chip, FTM0 + C0V, 50)
    w(chip, FTM0 + C0SC, MSB | ELSB)                       # EPWM high-true on PTC1
    w(chip, FTM0 + COMBINE, 0x40)                           # FAULTEN0
    w(chip, FTM0 + FLTPOL, pol)
    w(chip, FTM0 + FLTCTRL, 0x01)                           # FAULT0EN
    w(chip, FTM0 + MODE, WPDIS | (faultm << 5) | FAULTIE)
    return start(chip, mod=99)


def test_fault_manual_clearing(chip):
    t0 = fault_setup(chip, 2)
    run_to(chip, t0, 10.5)
    assert chip.pins.get("PTC1").level() == "H"
    chip.pins.set_external("PTB3", 1)                      # fault
    run_to(chip, t0, 11)
    fms = r(chip, FTM0 + FMS)
    assert fms & FAULTF and fms & 1 and fms & FAULTIN
    assert chip.pins.get("PTC1").level() == "L"            # safe value = POL0 = 0
    assert (16 + FTM0_IRQ) in chip.arch.asserted
    w(chip, FTM0 + FMS, 0)                                  # fault input still active: FAULTF stays
    assert r(chip, FTM0 + FMS) & FAULTF
    chip.pins.set_external("PTB3", 0)
    run_to(chip, t0, 150.5)                                # new periods: still forced (FAULTF set, manual)
    assert chip.pins.get("PTC1").level() == "L"
    assert not r(chip, FTM0 + FMS) & FAULTIN
    w(chip, FTM0 + FMS, 0)                                  # read (above) + write 0: cleared
    assert not r(chip, FTM0 + FMS) & (FAULTF | 1) and (16 + FTM0_IRQ) not in chip.arch.asserted
    run_to(chip, t0, 199.5)
    assert chip.pins.get("PTC1").level() == "L"            # until the next PWM cycle
    run_to(chip, t0, 200.5)
    assert chip.pins.get("PTC1").level() == "H"


def test_fault_automatic_clearing_and_inverted_polarity(chip):
    t0 = fault_setup(chip, 3, pol=1)                        # FLT0POL=1: a low level is the fault
    run_to(chip, t0, 20.5)
    chip.pins.set_external("PTB3", 0)
    run_to(chip, t0, 21)
    assert chip.pins.get("PTC1").level() == "L" and r(chip, FTM0 + FMS) & FAULTF
    run_to(chip, t0, 120.5)
    assert chip.pins.get("PTC1").level() == "L"            # FAULTIN still 1 at the cycle start (100)
    chip.pins.set_external("PTB3", 1)
    run_to(chip, t0, 199.5)
    assert chip.pins.get("PTC1").level() == "L"
    run_to(chip, t0, 200.5)
    assert chip.pins.get("PTC1").level() == "H"            # automatic: FAULTIN = 0 and a new cycle
    assert r(chip, FTM0 + FMS) & FAULTF                    # the flag still needs software


def test_fault_on_a_disabled_input_or_unprotected_channel_does_nothing(chip):
    t0 = fault_setup(chip, 2)
    w(chip, FTM0 + COMBINE, 0)                              # FAULTEN0 = 0: the channel is not forced
    run_to(chip, t0, 10.5)
    chip.pins.set_external("PTB3", 1)
    run_to(chip, t0, 12)
    assert r(chip, FTM0 + FMS) & FAULTF and chip.pins.get("PTC1").level() == "H"
    w(chip, FTM0 + FLTCTRL, 0)
    chip.pins.set_external("PTB3", 0)
    r(chip, FTM0 + FMS)
    w(chip, FTM0 + FMS, 0)
    chip.pins.set_external("PTB3", 1)
    run_to(chip, t0, 20)
    assert not r(chip, FTM0 + FMS) & FAULTF


def test_fault_mode_01_forces_even_channels_only_and_faultfj_clears_faultf(chip):
    t0 = fault_setup(chip, 1, pol=0)
    w(chip, FTM0 + C1V, 50)
    w(chip, FTM0 + C1SC, MSB | ELSB)                       # CH1 EPWM on PTC2, same pair (FAULTEN0)
    w(chip, FTM0 + POL, 0x01)                               # CH0 active low: safe value H
    run_to(chip, t0, 10.5)
    assert chip.pins.get("PTC1").level() == "L" and chip.pins.get("PTC2").level() == "H"
    chip.pins.set_external("PTB3", 1)
    run_to(chip, t0, 11)
    assert chip.pins.get("PTC1").level() == "H"             # forced to POL0 = 1
    assert chip.pins.get("PTC2").level() == "H"             # odd channel: not affected with FAULTM = 01
    run_to(chip, t0, 60.5)
    assert chip.pins.get("PTC1").level() == "H" and chip.pins.get("PTC2").level() == "L"
    chip.pins.set_external("PTB3", 0)
    r(chip, FTM0 + FMS)
    w(chip, FTM0 + FMS, FAULTF)                             # clear FAULTF0 only (FAULTF written 1: no effect)
    assert not r(chip, FTM0 + FMS) & (FAULTF | 1)           # the last FAULTFj cleared: FAULTF clears too


# ------------------------------------------------------------------------------------------- triggers, DMA
def test_channel_and_init_triggers_start_the_pdb(chip):
    """EXTTRIG CH0TRIG: the C0V match triggers PDB0 (TRGSEL 8 = FTM0); INITTRIGEN: the counter reload does."""
    pdb = chip.peripherals["PDB0"]
    w(chip, PDB0 + 0x04, 0xFFFF)                            # MOD
    w(chip, PDB0 + 0x00, (8 << 8) | (1 << 7) | 1)           # TRGSEL FTM0, PDBEN, LDOK
    w(chip, FTM0 + C0V, 30)
    w(chip, FTM0 + C0SC, MSA)                              # software compare (no pin)
    w(chip, FTM0 + EXTTRIG, 1 << 4)                         # CH0TRIG
    t0 = start(chip, mod=99)
    run_to(chip, t0, 29.5)
    assert pdb.triggers == 0
    run_to(chip, t0, 30.5)
    assert pdb.triggers == 1 and abs(pdb.t0_ns - (t0 + 30 * tk(chip))) < 1
    assert r(chip, FTM0 + EXTTRIG) & 0x80                   # TRIGF
    w(chip, FTM0 + EXTTRIG, 1 << 4)                         # read + write 0 clears TRIGF
    assert not r(chip, FTM0 + EXTTRIG) & 0x80
    w(chip, FTM0 + EXTTRIG, 1 << 6)                         # INITTRIGEN only
    run_to(chip, t0, 100.5)
    assert pdb.triggers == 2 and abs(pdb.t0_ns - (t0 + 100 * tk(chip))) < 1
    assert not r(chip, FTM0 + EXTTRIG) & 0x80               # the init trigger is not a channel trigger
    w(chip, PDB0 + 0x00, (9 << 8) | (1 << 7))               # TRGSEL FTM1: FTM0 no longer triggers it
    run_to(chip, t0, 300.5)
    assert pdb.triggers == 2


def test_capture_dma_request_moves_cnv_and_clears_chf(chip):
    """C1SC.DMA = CHIE = 1: CHnF raises DMAMUX source 21 (FTM0 CH1) instead of the interrupt; the DMA copies C1V
    to RAM and its acknowledge clears CHnF."""
    t = DMA + 0x1000
    w(chip, t + 0x00, FTM0 + C1V)                           # SADDR
    w(chip, t + 0x04, 0x0202_0000)                          # SOFF 0, ATTR 32-bit
    w(chip, t + 0x08, 4)                                    # NBYTES
    w(chip, t + 0x0C, 0)
    w(chip, t + 0x10, RAM)                                  # DADDR
    w(chip, t + 0x14, (4 << 16) | 4)                        # DOFF 4, CITER 4
    w(chip, t + 0x18, 0)
    w(chip, t + 0x1C, (4 << 16) | 0)                        # BITER 4
    chip.bus.write(DMAMUX, 1, 0x80 | 21)
    chip.bus.write(DMA + 0x1B, 1, 0)                        # SERQ 0
    w(chip, FTM0 + C1SC, ELSA | CHIE | CDMA)
    t0 = start(chip)
    for k in range(3):
        run_to(chip, t0, 100.25 + 100 * k)
        chip.pins.set_external("PTC2", 1)
        run_to(chip, t0, 150 + 100 * k)
        chip.pins.set_external("PTC2", 0)
    run_to(chip, t0, 400)
    vals = [int.from_bytes(chip.bus.read_bytes(RAM + 4 * i, 4), "little") for i in range(3)]
    assert vals == [100, 200, 300]
    assert not r(chip, FTM0 + C1SC) & CHF and (16 + FTM0_IRQ) not in chip.arch.asserted
    assert chip.peripherals["DMA"].transfers == 3


# ------------------------------------------------------------------------------------------- external clock
def test_external_clock_counts_clkin_edges(chip):
    """CLKS=11 with SIM_SOPT4.FTM0CLKSEL=1: FTM_CLKIN1 (PTB17 ALT4) rising edges / 2^PS clock the counter."""
    w(chip, PORTB + 4 * 17, 4 << 8)                         # PTB17 = FTM_CLKIN1
    w(chip, SIM_SOPT4, 1 << 24)
    w(chip, FTM0 + C0V, 3)
    w(chip, FTM0 + C0SC, MSA | CHIE)
    w(chip, FTM0 + MOD, 4)
    w(chip, FTM0 + SC, CLK_EXT | 1 | TOIE)                  # /2
    warns = []
    chip.log.subscribe(lambda e: e.kind == "warn" and warns.append(e))

    def pulses(n):
        for _ in range(n):
            chip.pins.set_external("PTB17", 1)
            chip.run_for(100)
            chip.pins.set_external("PTB17", 0)
            chip.run_for(100)

    pulses(5)                                                # 2 FTM clocks (5 // 2)
    assert r(chip, FTM0 + CNT) == 2
    pulses(1)                                                # 6 edges: CNT 3 = C0V
    assert r(chip, FTM0 + CNT) == 3 and r(chip, FTM0 + C0SC) & CHF
    pulses(4)                                                # 3 -> 4 -> 0 (TOF)
    assert r(chip, FTM0 + CNT) == 0 and r(chip, FTM0 + SC) & TOF
    pulses(2)
    assert r(chip, FTM0 + CNT) == 1 and r(chip, FTM0 + SC) & TOF and chip.peripherals["FTM0"].overflows == 1
    assert not warns
    w(chip, SIM_SOPT4, 0)                                    # FTM_CLKIN0 selected: PTB17 no longer counts
    pulses(4)
    assert r(chip, FTM0 + CNT) == 1


# ------------------------------------------------------------------------------------------- write protection
def test_write_protection_enforced(chip):
    w(chip, FTM0 + C0SC, MSB | ELSB)
    w(chip, FTM0 + MOD, 99)
    w(chip, FTM0 + FMS, WPEN)                                # protect
    w(chip, FTM0 + SC, CLK_SYS | 5)                         # CLKS/PS protected: ignored
    assert r(chip, FTM0 + SC) & 0x3F == 0 and not chip.peripherals["FTM0"].running
    w(chip, FTM0 + SC, TOIE)                                 # TOIE is not protected
    assert r(chip, FTM0 + SC) == TOIE
    w(chip, FTM0 + C0SC, MSA | ELSA | CHIE)                 # mode/edge protected, CHIE not
    assert r(chip, FTM0 + C0SC) == MSB | ELSB | CHIE
    w(chip, FTM0 + COMBINE, 0x21 | 0x10)                     # COMBINE0/DTEN0 protected, SYNCEN0 not
    assert r(chip, FTM0 + COMBINE) == 0x20
    for reg in (POL, FLTCTRL, FLTPOL, 0x68):                 # POL, FLTCTRL, FLTPOL, DEADTIME
        w(chip, FTM0 + reg, 0x0F)
        assert r(chip, FTM0 + reg) == 0, hex(reg)
    w(chip, FTM0 + QDCTRL, 1)
    assert r(chip, FTM0 + QDCTRL) == 0
    w(chip, FTM0 + MODE, FTMEN | (3 << 5))                   # FTMEN / FAULTM protected
    assert r(chip, FTM0 + MODE) == 0
    w(chip, FTM0 + MOD, 50)                                  # MOD, CNTIN, CnV are not protected
    assert r(chip, FTM0 + MOD) == 50
    r(chip, FTM0 + FMS)
    w(chip, FTM0 + MODE, WPDIS)                              # disable protection
    w(chip, FTM0 + SC, CLK_SYS)
    assert r(chip, FTM0 + SC) & 0x18 == CLK_SYS and chip.peripherals["FTM0"].running


# ------------------------------------------------------------------------------------------- CNTIN above the counter
def test_cntin_written_above_the_counter_matches_on_the_way_up(chip):
    """Counter at 300, CNTIN := 600 (FTMEN=0: at once): it goes on counting up to MOD; an output compare at 450
    (below CNTIN) still matches on the way, then the counter reloads 600."""
    ftm = chip.peripherals["FTM0"]
    w(chip, FTM0 + C0V, 450)
    w(chip, FTM0 + C0SC, MSA | ELSA)                        # toggle on PTC1
    ev = []
    chip.pins.subscribe(lambda e: e.pin == "PTC1" and e.field == "level" and ev.append(e.t_ns))
    t0 = start(chip, mod=999)
    run_to(chip, t0, 300.5)
    w(chip, FTM0 + CNTIN, 600)
    run_to(chip, t0, 449.5)
    assert not r(chip, FTM0 + C0SC) & CHF
    run_to(chip, t0, 450.5)
    assert r(chip, FTM0 + C0SC) & CHF and round((ev[-1] - t0) / tk(chip)) == 450
    run_to(chip, t0, 1000.5)
    assert r(chip, FTM0 + CNT) == 600 and ftm.overflows == 1
    w(chip, FTM0 + C0SC, MSA | ELSA)
    run_to(chip, t0, 1500)                                   # 600..999: 450 is out of range now
    assert not r(chip, FTM0 + C0SC) & CHF


# ------------------------------------------------------------------------------------------- quadrature decoder
def qd_setup(chip, mode=0, mod=7):
    w(chip, PORTA + 4 * 12, 7 << 8)                          # PTA12 = FTM1_QD_PHA
    w(chip, PORTA + 4 * 13, 7 << 8)                          # PTA13 = FTM1_QD_PHB
    chip.pins.set_external("PTA12", 0)
    chip.pins.set_external("PTA13", 0)
    w(chip, FTM1 + MOD, mod)
    w(chip, FTM1 + CNTIN, 0)
    w(chip, FTM1 + MODE, WPDIS | FTMEN)
    w(chip, FTM1 + QDCTRL, 1 | (mode << 3))                  # QUADEN
    w(chip, FTM1 + SC, CLK_SYS)


def quad(chip, steps):
    """Drive phase A/B through `steps` quadrature states (positive: A leads B)."""
    seq = [(0, 0), (1, 0), (1, 1), (0, 1)]
    a = chip.pins.get("PTA12").level() == "H"
    b = chip.pins.get("PTA13").level() == "H"
    i = seq.index((int(a), int(b)))
    for _ in range(abs(steps)):
        i = (i + (1 if steps > 0 else -1)) % 4
        na, nb = seq[i]
        if na != a:
            chip.pins.set_external("PTA12", na)
        if nb != b:
            chip.pins.set_external("PTA13", nb)
        a, b = na, nb
        chip.run_for(200)


def test_quadrature_phase_a_b_counts_up_and_down_with_tof(chip):
    qd_setup(chip)
    ftm = chip.peripherals["FTM1"]
    assert not ftm.running
    quad(chip, 5)
    assert r(chip, FTM1 + CNT) == 5 and r(chip, FTM1 + QDCTRL) & 0x04          # QUADIR: up
    quad(chip, 3)                                                               # 7 -> 0: TOF, TOFDIR=1
    assert r(chip, FTM1 + CNT) == 0 and r(chip, FTM1 + SC) & TOF and r(chip, FTM1 + QDCTRL) & 0x02
    quad(chip, -2)                                                              # 0 -> 7 (TOF, TOFDIR=0) -> 6
    assert r(chip, FTM1 + CNT) == 6 and not r(chip, FTM1 + QDCTRL) & 0x06
    assert ftm.overflows == 2
    chip.run_for(1e6)
    assert r(chip, FTM1 + CNT) == 6                                             # no time-driven counting


def test_quadrature_count_and_direction_mode(chip):
    qd_setup(chip, mode=1, mod=0xFFFF)
    chip.pins.set_external("PTA13", 1)                                          # B = 1: up
    for _ in range(4):
        chip.pins.set_external("PTA12", 1)
        chip.pins.set_external("PTA12", 0)
    assert r(chip, FTM1 + CNT) == 4
    chip.pins.set_external("PTA13", 0)
    chip.pins.set_external("PTA12", 1)
    assert r(chip, FTM1 + CNT) == 3
