"""K22 low-power modes (SMC/LLWU/PMC/RCM), MCG clock monitors, UART RTS/CTS and LPTMR pulse counting, on the real
chip description with keystone-assembled firmware (K22P100M120SF5V2RM ch. 7, 13, 14, 16, 25, 40, 49)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from engine_util import build_image

ROOT = Path(__file__).resolve().parent.parent
K22 = ROOT / "chips" / "mk22fn1m0vll12.yaml"

SMC, PMC, RCM, LLWU, VBAT = 0x4007E000, 0x4007D000, 0x4007F000, 0x4007C000, 0x4003E000
SIM = 0x40047000
SCGC5, SCGC6 = SIM + 0x1038, SIM + 0x103C
RTC, LPTMR, PIT, MCG = 0x4003D000, 0x40040000, 0x40037000, 0x40064000
UART0 = 0x4006A000
PORTA, PORTE, PORTB = 0x40049000, 0x4004D000, 0x4004A000
SCR = 0xE000ED10
NVIC_ISER0, NVIC_ISER1 = 0xE000E100, 0xE000E104
CNT = 0x20000100                     # SRAM_U scratch
MARK = 0x20003000                    # beyond RAM1 (4 KB): lost in VLLS2 without RAM2PO

WDOG_OFF = """
  ldr r0, =0x4005200E
  movw r1, #0xC520
  strh r1, [r0]
  movw r1, #0xD928
  strh r1, [r0]
  ldr r0, =0x40052000
  movw r1, #0x01D2
  strh r1, [r0]
"""


def st(addr: int, val: int, size: str = "") -> str:
    return f"""
  ldr r0, =0x{addr:08X}
  ldr r1, =0x{val:08X}
  str{size} r1, [r0]
"""


INC = f"""
  ldr r0, =0x{CNT:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
"""


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def mk(desc, code, vectors=None):
    chip = Chip(desc)
    img, _ = build_image(code, vectors=vectors)
    chip.load_firmware(img)
    chip.reset()
    return chip


def r(chip, addr, size=4):
    return chip.bus.read(addr, size)


def modes(chip):
    return [m for _, m in chip.peripherals["SMC"].history]


# --------------------------------------------------------------------------------------------- STOP + RTC alarm
def test_stop_wakes_on_rtc_alarm_and_freezes_pit(desc):
    code = {"reset": WDOG_OFF + st(SCGC6, 0x20800000) + st(PIT, 0) + st(PIT + 0x100, 0x7FFFFFFF)
            + st(PIT + 0x108, 1)                                       # PIT0 running
            + st(RTC + 0x00, 0) + st(RTC + 0x08, 1) + st(RTC + 0x1C, 4) + st(RTC + 0x14, 0x10)
            + st(0xE000E284, 1 << (46 - 32)) + st(NVIC_ISER1, 1 << (46 - 32)) + st(SCR, 4) + """
  wfi
""" + st(CNT + 4, 0xAA) + "spin:\n b spin\n",
            "rtc": st(RTC + 0x08, 100) + INC + "  bx lr\n"}
    chip = mk(desc, code, {16 + 46: "rtc"})
    chip.run_for(1e6)
    pit = chip.peripherals["PIT"]
    assert chip.sleeping and r(chip, SMC + 3, 1) == 0x02             # PMSTAT = STOP
    cv0 = pit.cval(0)
    chip.run_for(1.5e9)
    assert chip.sleeping and pit.cval(0) == cv0                     # bus clock stopped: PIT holds
    chip.run_for(1.0e9)
    assert r(chip, CNT) == 1 and r(chip, CNT + 4) == 0xAA            # woken by the alarm (TSR 1 -> 2 at 2 s)
    assert r(chip, SMC + 3, 1) == 0x01 and modes(chip)[-2:] == ["STOP", "RUN"]
    assert pit.cval(0) < cv0                                         # counting again


def test_wait_mode_keeps_pit_running(desc):
    code = {"reset": WDOG_OFF + st(SCGC6, 0x00800000) + st(PIT, 0) + st(PIT + 0x100, 0x7FFFFFFF)
            + st(PIT + 0x108, 1) + "  wfi\nspin:\n b spin\n"}
    chip = mk(desc, code)
    chip.run_for(1e6)
    pit = chip.peripherals["PIT"]
    cv0 = pit.cval(0)
    chip.run_for(1e6)
    assert chip.sleeping and pit.cval(0) < cv0 and r(chip, SMC + 3, 1) == 0x01
    assert modes(chip) == ["WAIT"]


# --------------------------------------------------------------------------------------------- LLS + LPTMR via LLWU
def test_lls_wakes_through_llwu_lptmr(desc):
    code = {"reset": WDOG_OFF + st(SCGC5, 0x00000001) + st(SMC, 0x08, "b") + st(SMC + 1, 0x03, "b")
            + st(LLWU + 4, 0x01, "b")                                  # ME: WUME0 = LPTMR0
            + st(LPTMR + 4, 0x05) + st(LPTMR + 8, 99) + st(LPTMR, 0x41)  # LPO, bypass, 100 ticks, TEN|TIE
            + st(NVIC_ISER0, 1 << 21) + st(SCR, 4) + "  wfi\n" + st(CNT + 4, 0xBB) + "spin:\n b spin\n",
            "llwu": f"""
  ldr r0, =0x{LLWU + 7:08X}
  ldrb r1, [r0]
  ldr r0, =0x{CNT + 8:08X}
  str r1, [r0]
""" + st(LPTMR, 0xC1) + INC + "  bx lr\n"}
    chip = mk(desc, code, {16 + 21: "llwu"})
    chip.run_for(50e6)
    assert chip.sleeping and r(chip, SMC + 3, 1) == 0x20             # LLS
    chip.run_for(60e6)
    assert r(chip, CNT) == 1 and r(chip, CNT + 4) == 0xBB
    assert r(chip, CNT + 8) == 0x01                                  # F3.MWUF0 seen by the ISR
    assert r(chip, LLWU + 7, 1) == 0                                 # follows TCF (cleared in the LPTMR)
    assert modes(chip)[-2:] == ["LLS", "RUN"]


def test_lls_ignores_ordinary_interrupts(desc):
    # RTC alarm enabled in the NVIC but not as an LLWU module source: LLS is not left
    code = {"reset": WDOG_OFF + st(SCGC6, 0x20000000) + st(SMC, 0x08, "b") + st(SMC + 1, 0x03, "b")
            + st(RTC + 0x00, 0) + st(RTC + 0x08, 0) + st(RTC + 0x1C, 4) + st(RTC + 0x14, 0x10)
            + st(0xE000E284, 1 << (46 - 32)) + st(NVIC_ISER1, 1 << (46 - 32)) + st(SCR, 4) + "  wfi\n" + st(CNT + 4, 0xCC) + "spin:\n b spin\n",
            "rtc": st(RTC + 0x08, 100) + INC + "  bx lr\n"}
    chip = mk(desc, code, {16 + 46: "rtc"})
    chip.run_for(2.5e9)
    assert chip.sleeping and r(chip, CNT + 4) == 0 and r(chip, SMC + 3, 1) == 0x20


def test_stop_abort_sets_stopa(desc):
    code = {"reset": WDOG_OFF + "  cpsid i\n" + st(0xE000E200, 1) + st(NVIC_ISER0, 1) + st(SCR, 4)
            + "  wfi\n" + st(CNT + 4, 0xDD) + "spin:\n b spin\n"}
    chip = mk(desc, code)
    chip.run_for(1e6)
    assert r(chip, CNT + 4) == 0xDD and r(chip, SMC + 1, 1) & 0x08   # PMCTRL.STOPA


# --------------------------------------------------------------------------------------------- VLLS through reset
def _vlls_code(vllsctrl: int) -> dict:
    return {"reset": f"""
  ldr r0, =0x{RCM:08X}
  ldrb r1, [r0]
  tst r1, #1
  bne woke
""" + WDOG_OFF + st(MARK, 0x12345678) + st(CNT + 0x20, 0x55AA)
            + st(SCGC5, 0x2000) + st(PORTE + 4, 0x100)                 # PTE1 = GPIO (input)
            + st(SMC, 0x02, "b") + st(SMC + 1, 0x04, "b") + st(SMC + 2, vllsctrl, "b")
            + st(LLWU + 0, 0x02, "b")                                  # PE1: LLWU_P0 falling edge
            + st(SCR, 4) + "  wfi\nspin:\n b spin\nwoke:\n" + f"""
  ldr r2, =0x{VBAT:08X}
  str r1, [r2]
  ldr r0, =0x{LLWU + 5:08X}
  ldrb r1, [r0]
  str r1, [r2, #4]
  ldr r0, =0x{PMC + 2:08X}
  ldrb r1, [r0]
  str r1, [r2, #8]
  ldr r0, =0x{MARK:08X}
  ldr r1, [r0]
  str r1, [r2, #12]
  ldr r0, =0x{CNT + 0x20:08X}
  ldr r1, [r0]
  str r1, [r2, #16]
hold:
  b hold
"""}


@pytest.mark.parametrize("vllsctrl,mark,low", [(0x01, 0, 0), (0x03, 0x12345678, 0x55AA), (0x02, 0, 0x55AA),
                                              (0x12, 0x12345678, 0x55AA)])
def test_vlls_wakeup_is_a_reset(desc, vllsctrl, mark, low):
    chip = mk(desc, _vlls_code(vllsctrl))
    chip.pins.set_external("PTE1", 1)
    chip.run_for(1e6)
    assert chip.sleeping and r(chip, SMC + 3, 1) == 0x40             # VLLS
    chip.pins.set_external("PTE1", 0)
    chip.run_for(1e6)
    assert any(e.kind == "reset" and e.fields.get("reason") == "wakeup" for e in chip.log)
    assert r(chip, VBAT) == 0x01                                     # SRS0 = WAKEUP
    assert r(chip, VBAT + 4) == 0x01                                 # LLWU_F1.WUF0 survived the reset
    assert r(chip, VBAT + 8) & 0x08                                  # PMC_REGSC.ACKISO
    assert r(chip, VBAT + 12) == mark and r(chip, VBAT + 16) == low  # SRAM retention per VLLS level
    assert r(chip, SMC, 1) == 0x02                                   # PMPROT kept (Chip Reset not VLLS)
    chip.bus.write(PMC + 2, 1, 0x08)
    assert not r(chip, PMC + 2, 1) & 0x08


def test_reset_pin_and_wakeup_pin_causes(desc):
    chip = mk(desc, _vlls_code(0x03))
    chip.pins.set_external("PTE1", 1)
    chip.pins.set_external("RESET_b", 1)
    chip.run_for(1e6)
    chip.bus.write(LLWU + 0xA, 1, 0x02)                              # LLRSTE
    chip.pins.set_external("RESET_b", 0)
    chip.run_for(1e6)
    assert r(chip, VBAT) == 0x41                                     # WAKEUP | PIN
    chip.pins.set_external("RESET_b", 1)
    chip.pins.set_external("RESET_b", 0)
    chip.run_for(1e6)
    assert r(chip, RCM, 1) == 0x40                                   # plain pin reset


# --------------------------------------------------------------------------------------------- MCG clock monitors
def _pee_chip(desc, extra=""):
    # FEI -> FBE -> PBE -> PEE (8 MHz crystal, PRDIV 3 -> 2 MHz, VDIV 24 -> x48 = 96 MHz)
    code = {"reset": WDOG_OFF + st(MCG + 1, 0x24 | 0x80, "b") + st(MCG + 0, 0x98, "b")
            + st(MCG + 4, 0x03, "b") + st(MCG + 5, 0x58 | extra_c6(extra), "b") + st(MCG + 0, 0x18, "b")
            + extra + "spin:\n b spin\n"}
    return mk(desc, code)


def extra_c6(extra):
    return 0x20 if "CME0" in extra else 0


def test_mcg_loss_of_clock_reset(desc):
    chip = _pee_chip(desc, "@ CME0\n")
    chip.run_for(1e5)
    mcg = chip.peripherals["MCG"]
    assert mcg.mode == "PEE" and r(chip, MCG + 6, 1) & 0x40           # LOCK0
    mcg.set_external_clock(False)
    chip.run_for(1e5)
    assert any(e.kind == "reset" and e.fields.get("reason") == "loc" for e in chip.log)
    assert r(chip, RCM, 1) == 0x04                                    # SRS0.LOC
    assert not r(chip, MCG + 6, 1) & 0x02                             # OSCINIT0 = 0: crystal still dead


def test_mcg_clock_loss_without_monitor_halts_core(desc):
    chip = _pee_chip(desc)
    chip.run_for(1e5)
    mcg = chip.peripherals["MCG"]
    mcg.set_external_clock(False)
    insns = chip.insns
    chip.run_for(1e5)
    assert chip.sleeping and chip.insns == insns                      # no MCGOUTCLK: the core is stopped
    assert r(chip, MCG + 6, 1) & 0x80 and not r(chip, MCG + 6, 1) & 0x40   # LOLS0 set, LOCK0 clear
    mcg.set_external_clock(True)
    chip.run_for(1e5)
    assert chip.insns > insns


def test_mcg_stop_exit_pee_to_pbe(desc):
    code = {"reset": WDOG_OFF + st(SCGC6, 0x20000000) + st(MCG + 1, 0x24, "b") + st(MCG + 0, 0x98, "b")
            + st(MCG + 4, 0x03, "b") + st(MCG + 5, 0x58, "b") + st(MCG + 0, 0x18, "b")
            + st(RTC + 0x00, 0) + st(RTC + 0x08, 0) + st(RTC + 0x1C, 4) + st(RTC + 0x14, 0x10)
            + st(0xE000E284, 1 << (46 - 32)) + st(NVIC_ISER1, 1 << (46 - 32)) + st(SCR, 4) + "  wfi\nspin:\n b spin\n",
            "rtc": st(RTC + 0x08, 100) + "  bx lr\n"}
    chip = mk(desc, code, {16 + 46: "rtc"})
    chip.run_for(1.2e9)
    assert chip.peripherals["MCG"].mode == "PBE"                      # PLLSTEN0 = 0: PEE -> PBE (25.4.1)
    assert (r(chip, MCG + 6, 1) >> 2) & 3 == 2


# --------------------------------------------------------------------------------------------- UART RTS / CTS
def _uart_chip(desc, modem: int, extra: str = ""):
    # UART0 on PTB16/PTB17 (ALT3), RTS_b on PTB2 (ALT3), CTS_b on PTB3 (ALT3); SBR = 0 -> characters are instant
    code = {"reset": WDOG_OFF + st(SCGC5, 0x0400) + st(SIM + 0x1034, 0x0400)
            + st(PORTB + 0x40, 0x300) + st(PORTB + 0x44, 0x300) + st(PORTB + 0x08, 0x300) + st(PORTB + 0x0C, 0x300)
            + st(UART0 + 0x00, 0, "b") + st(UART0 + 0x01, 13, "b")
            + st(UART0 + 0x10, 0x88, "b") + st(UART0 + 0x0D, modem, "b") + st(UART0 + 0x03, 0x0C, "b") + extra + "spin:\n b spin\n"}
    return mk(desc, code)


def test_uart_cts_holds_transmitter(desc):
    send = st(UART0 + 7, 0x41, "b") + st(UART0 + 7, 0x42, "b")
    chip = _uart_chip(desc, 0x01, send)
    chip.pins.set_external("PTB3", 1)                                 # CTS_b deasserted
    chip.run_for(2e6)
    u = chip.peripherals["UART0"]
    assert bytes(u.output) == b""
    chip.pins.set_external("PTB3", 0)
    chip.run_for(2e6)
    assert bytes(u.output) == b"AB"


def test_uart_rxrtse_drives_rts(desc):
    chip = _uart_chip(desc, 0x08)
    chip.run_for(1e6)
    rts = chip.pins.get("PTB2")
    assert rts.owner == "UART0_RTS_b" and rts.level() == "L"           # room in the buffer: asserted
    u = chip.peripherals["UART0"]
    u.receive(b"x")
    assert rts.level() == "H"                                          # RXWATER = 1 reached: deasserted
    chip.bus.read(UART0 + 4, 1)
    chip.bus.read(UART0 + 7, 1)
    assert rts.level() == "L"


def test_uart_txrtse_driver_enable(desc):
    chip = _uart_chip(desc, 0x02 | 0x04, st(UART0 + 7, 0x55, "b"))     # TXRTSE, active high
    lv = []
    chip.pins.subscribe(lambda ev: ev.pin == "PTB2" and ev.field == "level" and lv.append((ev.t_ns, ev.new)))
    chip.run_for(1e6)
    rts = chip.pins.get("PTB2")
    assert ("H" in [v for _, v in lv]) and rts.level() == "L"           # asserted while sending, then released
    t_on = [t for t, v in lv if v == "H"][-1]
    t_off = [t for t, v in lv if v == "L"][-1]
    char = 10 / u_baud(chip) * 1e9
    # written during the TE idle preamble: asserted for the rest of the preamble + the character + one bit time
    assert t_off - t_on == pytest.approx(2 * char + char / 10, rel=0.05)


def u_baud(chip):
    return chip.peripherals["UART0"].baud()


def test_uart_stop_mode_drops_and_rxedge_wakes(desc):
    code = {"reset": WDOG_OFF + st(SCGC5, 0x0400) + st(SIM + 0x1034, 0x0400) + st(PORTB + 0x40, 0x300)
            + st(UART0 + 0x00, 0x40, "b") + st(UART0 + 0x01, 13, "b") + st(UART0 + 0x03, 0x04, "b")
            + st(NVIC_ISER0, 1 << 31) + st(SCR, 4) + "  wfi\n" + st(CNT + 4, 0xEE) + "spin:\n b spin\n",
            "uart": st(UART0 + 0x05, 0x40, "b") + INC + "  bx lr\n"}
    chip = mk(desc, code, {16 + 31: "uart"})
    chip.run_for(1e6)
    assert chip.sleeping
    chip.peripherals["UART0"].receive(b"z")
    chip.run_for(1e6)
    assert r(chip, CNT) == 1 and r(chip, CNT + 4) == 0xEE
    drops = [e for e in chip.log if e.kind == "uart" and e.fields.get("dir") == "drop"]
    assert drops and drops[-1].fields["reason"] == "stop_mode"


# --------------------------------------------------------------------------------------------- LPTMR pulse counter
def test_lptmr_pulse_counter_on_alt2_pin(desc):
    # PTC5 ALT3 = LPTMR0_ALT2; TMS=1, TPS=2, rising edges, bypass, CMR = 2 -> TCF on the 3rd edge
    code = {"reset": WDOG_OFF + st(SCGC5, 0x0801) + st(0x4004B000 + 0x14, 0x300)
            + st(LPTMR + 4, 0x04) + st(LPTMR + 8, 2) + st(LPTMR, 0x23) + "spin:\n b spin\n"}
    chip = mk(desc, code)
    chip.run_for(1e5)
    lp = chip.peripherals["LPTMR0"]
    for k in range(3):
        chip.pins.set_external("PTC5", 0)
        chip.run_for(1e4)
        chip.pins.set_external("PTC5", 1)
        chip.run_for(1e4)
        if k < 2:
            assert lp.cnr() == k + 1 and not r(chip, LPTMR, 4) & 0x80
    assert r(chip, LPTMR, 4) & 0x80 and lp.cnr() == 0


def test_lptmr_glitch_filter(desc):
    # LPO 1 kHz, PRESCALE=1 -> a change must hold for 2 ms
    code = {"reset": WDOG_OFF + st(SCGC5, 0x0801) + st(0x4004B000 + 0x14, 0x300)
            + st(LPTMR + 4, 0x09) + st(LPTMR + 8, 100) + st(LPTMR, 0x23) + "spin:\n b spin\n"}
    chip = mk(desc, code)
    chip.pins.set_external("PTC5", 0)
    chip.run_for(1e5)
    lp = chip.peripherals["LPTMR0"]
    chip.pins.set_external("PTC5", 1)
    chip.run_for(1e6)
    chip.pins.set_external("PTC5", 0)                                 # 1 ms glitch: filtered out
    chip.run_for(3e6)
    assert lp.cnr() == 0
    chip.pins.set_external("PTC5", 1)
    chip.run_for(3e6)
    assert lp.cnr() == 1


# --------------------------------------------------------------------------------------------- sample
SAMPLE = ROOT / "firmware/samples/build/lls_wakeup.elf"


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample firmware not built")
def test_lls_wakeup_sample():
    from chipem.session import Session
    s = Session("mk22fn1m0vll12", SAMPLE, clock_hz=120e6)
    s.run_for(520e6)
    ev = [e for e in s.events(kinds={"pin"}) if e.pin == "PTC5" and e.field == "level" and e.new in ("H", "L")]
    t = [e.t_ns for e in ev if e.t_ns > 1e6]
    assert len(t) == 5
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert all(abs(g - 100e6) < 1e4 for g in gaps), gaps          # LPTMR period: 100 LPO ticks
    smc = s.chip.peripherals["SMC"]
    assert [m for _, m in smc.history][-2:] == ["LLS", "RUN"] or smc.mode == "LLS"
    assert s.chip.insns < 2000                                    # asleep almost all the time


# --------------------------------------------------------------------------------------------- ADC compare / average
def test_adc_compare_and_hw_average(desc):
    chip = Chip(desc)
    adc = chip.peripherals["ADC0"]
    adc.conv_ns = 1000.0
    adc.set_channel("SE8", 1000)
    sc1a, sc2, sc3, ra, cv1, cv2 = (0x4003B000 + o for o in (0x00, 0x20, 0x24, 0x10, 0x18, 0x1C))
    chip.bus.write(0x4003B008, 4, 0x0C)                    # CFG1.MODE = 16-bit
    chip.bus.write(cv1, 4, 2000)
    chip.bus.write(sc2, 4, 0x20 | 0x10)                    # ACFE, ACFGT: true if >= 2000
    chip.bus.write(sc1a, 4, 8)
    chip.clock.advance(chip.clock.ns_to_cycles(2000))
    chip.sched.run_due()
    assert not r(chip, sc1a) & 0x80                        # 1000 < 2000: no COCO
    adc.set_channel("SE8", 2500)
    chip.bus.write(sc3, 4, 0x04 | 0x03)                    # AVGE, 32 samples
    chip.bus.write(sc1a, 4, 8)
    chip.clock.advance(chip.clock.ns_to_cycles(31_000))
    chip.sched.run_due()
    assert not r(chip, sc1a) & 0x80                        # still averaging
    chip.clock.advance(chip.clock.ns_to_cycles(2000))
    chip.sched.run_due()
    assert r(chip, sc1a) & 0x80 and r(chip, ra) == 2500
    chip.bus.write(cv2, 4, 3000)
    chip.bus.write(sc2, 4, 0x20 | 0x08)                    # ACREN, ACFGT=0, CV1<=CV2: outside (2000, 3000)
    chip.bus.write(sc3, 4, 0)
    chip.bus.write(sc1a, 4, 8)
    chip.clock.advance(chip.clock.ns_to_cycles(2000))
    chip.sched.run_due()
    assert not r(chip, sc1a) & 0x80


# --------------------------------------------------------------------------------------------- core LOCKUP
def test_lockup_resets_the_k22(desc):
    # a fault inside HardFault is a LOCKUP: the K22 resets (RM 6.2.2.9) and RCM_SRS1[LOCKUP] is set
    code = {"reset": WDOG_OFF + "  udf #0\n", "hf": "  udf #1\n"}
    chip = mk(desc, code, {3: "hf"})
    r_ = chip.run_for(1e5)
    assert str(r_.value) == "count"
    assert any(e.kind == "reset" and e.get("reason") == "lockup" for e in chip.log)
    assert r(chip, RCM + 1, 1) & 0x02                     # SRS1.LOCKUP
    chip.halt_on_fault = True                             # --stop-on-fault still stops the run
    chip.reset()
    assert chip.run_for(1e5).value == "fault"


# --------------------------------------------------------------------------------------------- STOP freezes FTM/PDB/DMA
FTM0, PDB0 = 0x40038000, 0x40036000
RTC_WAKE = (st(SCGC6, 0x20000000, ) + st(RTC + 0x00, 0) + st(RTC + 0x08, 1) + st(RTC + 0x1C, 4) + st(RTC + 0x14, 0x10)
            + st(0xE000E284, 1 << (46 - 32)) + st(NVIC_ISER1, 1 << (46 - 32)))     # RTC alarm interrupt wakes STOP
RTC_ISR = st(RTC + 0x08, 100) + INC + "  bx lr\n"


def test_stop_holds_ftm_and_pdb_counters(desc):
    # Table 7-2: FTM and PDB static in STOP; the counters continue from where they stopped (no jump by the sleep time)
    code = {"reset": WDOG_OFF + RTC_WAKE + st(SCGC6, (1 << 29) | (1 << 24) | (1 << 22))
            + st(FTM0 + 0x08, 0xFFFF) + st(FTM0, 0x0F)                 # MOD, CLKS = system clock, PS = /128
            + st(PDB0 + 0x04, 0xFFFF) + st(PDB0, 0x7F8F)                  # MOD; PRESCALER 7, MULT x40, CONT, LDOK
            + st(PDB0, 0x17F8E)                                          # SWTRIG: start
            + st(SCR, 4) + "  wfi\n" + st(CNT + 4, 0xAA) + "spin:\n b spin\n",
            "rtc": RTC_ISR}
    chip = mk(desc, code, {16 + 46: "rtc"})
    chip.run_for(1e6)
    ftm, pdb = chip.peripherals["FTM0"], chip.peripherals["PDB0"]
    assert chip.sleeping and r(chip, SMC + 3, 1) == 0x02 and ftm.running and pdb.running
    f0, p0 = ftm._cnt_now(), pdb.cnt()
    chip.run_for(500e6)
    assert chip.sleeping and ftm._cnt_now() == f0 and pdb.cnt() == p0      # held while the bus clock is off
    while r(chip, CNT) == 0 and chip.clock.now_ns() < 3e9:
        chip.run_for(100e6)
    assert r(chip, CNT) == 1 and r(chip, CNT + 4) == 0xAA                 # the RTC alarm woke the core (~1-2 s)
    f1, p1 = ftm._cnt_now(), pdb.cnt()
    hz = chip.clock.hz                                                    # bus = core in FEI (OUTDIV2 = 1)
    awake_ns = 10e6                                                        # generous: the core ran < 10 ms awake
    assert 0 < (f1 - f0) & 0xFFFF < awake_ns * 1e-9 * hz / 128 + 2
    assert 0 < p1 - p0 < awake_ns * 1e-9 * hz / (128 * 40) + 2
    assert ftm._t(ftm._p0) <= chip.now_ns()                                # anchor moved, not in the future


def test_wait_keeps_ftm_running(desc):
    code = {"reset": WDOG_OFF + st(SCGC6, 1 << 24) + st(FTM0 + 0x08, 0xFFFF) + st(FTM0, 0x0F)
            + "  wfi\nspin:\n b spin\n"}
    chip = mk(desc, code)
    chip.run_for(1e5)
    ftm = chip.peripherals["FTM0"]
    f0 = ftm._cnt_now()
    chip.run_for(1e6)
    assert chip.sleeping and modes(chip) == ["WAIT"] and ftm._cnt_now() != f0


def test_stop_defers_dma_request_until_wakeup(desc):
    code = {"reset": WDOG_OFF + RTC_WAKE + st(SCR, 4) + "  wfi\nspin:\n b spin\n", "rtc": RTC_ISR}
    chip = mk(desc, code, {16 + 46: "rtc"})
    chip.run_for(1e6)
    assert chip.sleeping
    dma = chip.peripherals["DMA"]
    seen = []
    dma._enqueue = lambda ch: seen.append((ch, chip.sleeping))          # observe the engine entry
    dma._setbit("ERQ", 3, True)
    dma.hw_request(3)                                                    # a request while the system clock is off
    assert seen == [] and 3 in dma._lp_pending
    chip.run_for(2.5e9)
    assert not chip.sleeping and seen and seen[0][0] == 3 and dma._lp_pending == set()


def test_i2c_address_match_wakes_from_stop_with_wuen(desc):
    from chipem.periph.kinetis.i2c import IAAS, IICIF, IICEN, IICIE, WUEN
    code = {"reset": WDOG_OFF + st(SCR, 4) + "  wfi\n" + st(CNT + 4, 0xEE) + "spin:\n b spin\n",
            "i2c": INC + "  ldr r0, =0x40066003\n  movs r1, #2\n  strb r1, [r0]\n  cpsid i\n  bx lr\n"}
    for wuen, woke in ((WUEN, True), (0, False)):
        chip = mk(desc, code, {16 + 24: "i2c"})
        i2c = chip.peripherals["I2C0"]
        i2c.set("A1", 0x50 << 1)
        i2c.set("C1", IICEN | IICIE | wuen)
        chip.arch.enable_irq(24) if hasattr(chip.arch, "enable_irq") else chip.bus.write(NVIC_ISER0, 4, 1 << 24)
        chip.run_for(1e5)
        assert chip.sleeping and r(chip, SMC + 3, 1) == 0x02
        assert i2c.lp_frozen == "STOP"
        acked = i2c.i2c_address(0x50, False)                             # the bus decoder sees our address
        assert acked is woke
        chip.run_for(1e5)
        if woke:
            assert r(chip, CNT) == 1 and r(chip, CNT + 4) == 0xEE and i2c.get("S") & IAAS
            assert not i2c.lp_frozen
        else:
            assert chip.sleeping and r(chip, CNT) == 0 and not i2c.get("S") & (IAAS | IICIF)


def test_stop_lets_the_dspi_frame_finish_then_stops(desc):
    # 47.4.8.1: a transfer in progress completes at the frame boundary, then the DSPI is STOPPED until the wake-up
    SPI0 = 0x4002C000
    code = {"reset": WDOG_OFF + RTC_WAKE + st(SCGC6, (1 << 29) | (1 << 12))
            + st(SPI0 + 0x0C, 0x3800000F)                                 # CTAR0: 8 bits, BR = 32768 (slow SCK)
            + st(SPI0, 0x80000000)                                        # MCR: MSTR, MDIS = HALT = 0
            + st(SPI0 + 0x34, 0x000100A5) + st(SPI0 + 0x34, 0x0001005A)    # two frames on PCS0
            + st(SCR, 4) + "  wfi\n" + st(CNT + 4, 0xAA) + "spin:\n b spin\n",
            "rtc": RTC_ISR}
    chip = mk(desc, code, {16 + 46: "rtc"})
    spi = chip.peripherals["SPI0"]
    chip.run_for(1e6)
    assert chip.sleeping and spi._frame is not None and not spi.frames      # asleep mid-frame
    chip.run_for(200e6)
    assert chip.sleeping and [f[1] for f in spi.frames] == [0xA5]            # first frame completed, second waits
    assert spi._frame is None and not spi._status() & (1 << 30)              # SR.TXRXS = 0 (STOPPED)
    while r(chip, CNT) == 0 and chip.clock.now_ns() < 3e9:
        chip.run_for(100e6)
    chip.run_for(100e6)
    assert [f[1] for f in spi.frames] == [0xA5, 0x5A]                        # resumed after the wake-up


def test_stop_stops_sai_with_internal_bit_clock_after_the_frame(desc):
    # 51.1.3.2: without a bit clock that runs in Stop (or without STOPE) a side finishes its frame and stays disabled
    I2S0 = 0x4002F000
    code = {"reset": WDOG_OFF + RTC_WAKE + st(SCGC6, (1 << 29) | (1 << 15))
            + st(I2S0 + 0x08, (1 << 24) | 3)                             # TCR2: BCD = 1, MSEL = bus clock, DIV 3
            + st(I2S0 + 0x0C, 1 << 16) + st(I2S0 + 0x10, (1 << 16) | (1 << 4) | 1)   # TCE ch0; 2 words, MF, FSD
            + st(I2S0 + 0x14, (7 << 24) | (7 << 16) | (7 << 8))          # 8-bit words
            + st(I2S0, (1 << 31) | (1 << 30) | (1 << 28))                # TE, STOPE, BCE
            + st(SCR, 4) + "  wfi\n" + st(CNT + 4, 0xAA) + "spin:\n b spin\n",
            "rtc": RTC_ISR}
    chip = mk(desc, code, {16 + 46: "rtc"})
    sai = chip.peripherals["I2S0"]
    chip.run_for(1e6)
    assert chip.sleeping and sai.tx.lp_off
    f0 = sai.tx.frames
    chip.run_for(300e6)
    assert sai.tx.frames <= f0 + 1 and not sai.tx.running and not sai.tx.enabled
    assert sai.read(0x00, 4) & (1 << 31)                                  # TE still set: disabled by the stop only
    while r(chip, CNT) == 0 and chip.clock.now_ns() < 3e9:
        chip.run_for(100e6)
    f1 = sai.tx.frames
    chip.run_for(1e6)
    assert sai.tx.running and sai.tx.frames > f1 + 10                      # running again after the wake-up
    sai.tx.lp_off = False
    sai.write(0x08, 4, 0)                                                  # BCD = 0: external bit clock
    assert sai._clock_survives_stop(sai.tx)


def test_flexcan_stop_ack_and_self_wake(desc):
    # 46.4.10.3: Stop sets LPMACK/NOTRDY, ignores Rx; with SLFWAK bus activity sets ESR1.WAKINT (Wake_Up interrupt)
    from chipem.canbus import CanFrame
    from chipem.periph.kinetis.flexcan import ESR1_W1C, LPMACK, NOTRDY, SLFWAK, WAKINT, WAKMSK
    chip = mk(desc, {"reset": WDOG_OFF + "spin:\n b spin\n"})
    chip.bus.write(SCGC5, 4, chip.bus.read(SCGC5, 4) | (1 << 9))           # PORTA
    chip.bus.write(SCGC6, 4, chip.bus.read(SCGC6, 4) | (1 << 4))           # FLEXCAN0
    chip.bus.write(PORTA + 4 * 12, 4, 2 << 8)                               # PTA12 = CAN0_TX
    chip.bus.write(PORTA + 4 * 13, 4, 2 << 8)                               # PTA13 = CAN0_RX
    can = chip.peripherals["CAN0"]
    chip.bus.write(0x40024000, 4, SLFWAK | WAKMSK)                          # MDIS = FRZ = HALT = 0: running
    chip.run_for(1e5)
    t0 = can._timer_value(chip.now_ns())
    can.clock_stop(True, "STOP")
    mcr = chip.bus.read(0x40024000, 4)
    assert mcr & LPMACK and mcr & NOTRDY
    assert can.can_tx_frame(can.can_bus) is None and can.can_rx_state(can.can_bus) == "listen"
    chip.clock.advance(chip.clock.ns_to_cycles(1e6))
    assert can._timer_value(chip.now_ns()) == t0                            # TIMER held
    can.can_rx(can.can_bus, CanFrame(0x123, b"\x01"), chip.now_ns() - 1e5, chip.now_ns())
    assert can.get("ESR1") & WAKINT and can._lines.get("wake")              # Wake_Up interrupt line
    assert can.rx_frames == 0                                               # the frame itself is not received
    can.clock_stop(False, "STOP")
    assert not chip.bus.read(0x40024000, 4) & LPMACK and can._sync_t > chip.now_ns()
    assert ESR1_W1C & WAKINT
