#!/usr/bin/env python3
"""Generate chips/tms320f2802x_regs.yaml and chips/tms320f2806x_regs.yaml (TI Piccolo register tables, 16-bit units)
and check them against the data sheets.

TI publishes no SVD for the C2000 Piccolo families.  The blocks whose IP the Piccolo shares with the 2833x/280x (CPU
timers, PIE, SCI, SPI, I2C, eCAP, eQEP, eCAN, McBSP, DMA, flash and CSM registers) are copied from
chips/tms320f28232_regs.yaml (the same way tools/gen_tms320f2808_regs.py builds the F2808 table) with the documented
Piccolo deltas; the Piccolo-only blocks are transcribed below from the technical reference manuals:

  SPRUI09A  TMS320F2802x/F2802xx TRM (Dec 2018, rev. Jun 2022), svd/c28x-docs/dl18/sprui09.pdf
  SPRUH18I  TMS320x2806x TRM, svd/c28x-docs/dl19/spruh18.pdf
  SPRS523P  TMS320F2802x data sheet (Feb 2021), docs/datasheets/ti/tms320f28027.pdf
  SPRS698J  TMS320F2806x data sheet (Sep 2021), docs/datasheets/ti/tms320f28069.pdf

Block sources (TRM page numbers are PDF pages):
* SYSCTRL 0x7010: SPRUI09 Table 1-14 (p. 46-47), Figures 1-14..1-41 (PCLKCR0/1/3 p. 47-50, LOSPCP p. 51, INTOSCnTRIM
  p. 53, XCLK p. 54, CLKCTL p. 55-57, PLLCR p. 60, PLLSTS p. 61, PLLLOCKPRD p. 63, LPMCR0 p. 74, SCSR/WDCNTR/WDKEY/WDCR
  p. 78-80), JTAGDEBUG 0x702A (Figure 1-54); SPRUH18 Table 1-14 (p. 63) adds PCLKCR2 (0x7019, HRCAP gates), PLL2CTL,
  PLL2MULT, PLL2STS, SYSCLK2CNTR (0x7030-0x7036, Figures 1-29..1-32 p. 82-83) and EPWMCFG (0x703A, Figure 1-33 p. 84);
  2806x PCLKCR0/1/3 per Figures 1-14..1-17 (p. 64-68).  The SYSCTRL block of the 2806x therefore spans 0x7010-0x703F
  (SPRS698 Table 8-8 lists the frame as 0x7010-0x702F, but Table 8-11 has registers up to 0x703A).
* NMIINT 0x7060: SPRUI09 Table 1-26 and Figures 1-29..1-34 (p. 68-71); SPRUH18 Table 1-32 (p. 89).
* DEV_EMU 0x0880 (DEVICECNF, CLASSID, REVID; size 261 words up to 0x0984) and BORCFG 0x0985: SPRUI09 Table 1-102,
  Figures 1-80..1-83 (p. 132-134), Figure 1-98 (p. 164); SPRUH18 Figure 1-90 (p. 163, SYSCLK2DIV2DIS); SPRS523 Table
  9-11 (p. 52) / SPRS698 Table 8-10 (p. 66-67) for CLASSID values.  PARTID is an OTP word (0x3D7FFF on the 2802x,
  0x3D7E80 on the 2806x), not a register: the chip YAML seeds it.
* GPIO 0x6F80: SPRUI09 Tables 1-51..1-53 (p. 92-94), Figures 1-58..1-79 (p. 103-125); SPRUH18 Tables 1-56..1-58 and
  Figures 1-80/1-81 (p. 146: GPAPUD/GPBPUD resets); SPRS523 Table 9-29, SPRS698 Table 8-38.  Port B is GPIO32-38 on the
  2802x and GPIO32-44 + GPIO50-58 on the 2806x (GPBQSEL2/GPBMUX2 only there).  AIO2/4/6/10/12/14 (AIOMUX1 fields reset
  "1,x": taken as 10b).
* XINT 0x7070: SPRUI09 Table 1-77, Figures 1-96/1-97 (p. 118, 161-162).
* ADC 0x7100 (type 3) and ADCRESULT 0x0B00: SPRUI09 Table 6-3 (p. 424), Figures 6-14..6-38 (p. 425-447); SPRUH18 Table
  8-3 (p. 534), Figures 8-14..8-38; ADCCTL2 has CLKDIV4EN only on the 2806x (SPRUH18 Figure 8-15, p. 537).
* COMP1-3 0x6400/0x6420/0x6440: SPRUI09 Table 7-3, Figures 7-5..7-13 (p. 454-457); SPRUH18 Table 9-3 (p. 564).
* EPWMn 0x6800 + 0x40 (n-1): SPRUI09 Table 3-1 (p. 213-214), 3.4.5 TZ registers (Figures 3-94..3-100, p. 316-324),
  3.4.8 DC registers (Figures 3-107..3-116, p. 333-341), 4.4 HRPWM registers (Figures 4-15..4-18, p. 367-370);
  SPRUH18 Table 3-1 (p. 247-248).  HRPWR and HRMSTEP exist on ePWM1 only (SPRS523 Table 9-27, SPRS698 Table 8-33).
  The other ePWM registers (TBCTL..PCCTL) are the F2833x ones.
* 2806x only: CLA1 0x1400 (SPRUH18 Table 10-20 p. 709 and SPRS698 Table 8-20 p. 87), DMA 0x1000 (SPRUH18 Table 11-2
  p. 740, MODE Figure 11-12 p. 746: no SYNCE/SYNCSEL), USB0 0x4000 (SPRUH18 Table 17-3 p. 1069-1071), HRCAP1-4
  (SPRUH18 Figures 5-13..5-18 p. 422-426, SPRS698 Table 8-36 p. 144), eQEP1/2, eCAP2/3, SPI-B, SCI-B, McBSP-A, eCAN-A.
* Copied from chips/tms320f28232_regs.yaml: FLASH, CSM (SPRUI09 Tables 1-1 / 1-12: same addresses and resets),
  CPUTIMER0-2 (Table 1-41), PIE (Table 1-112), SCI (SPRUI09 Table 9-6; the Piccolo SCI FIFOs are 4 deep, SPRUI09 9.1
  p. 498 / SPRUH18 13.1: same register fields), SPI (Table 8-7; 4-level FIFO), I2C (Table 10-8, with I2CEMDR at 0x0B,
  which the TRMs list and the data sheet tables omit; 4-level FIFO), eCAP, eQEP, eCAN, McBSP, DMA.
* Interrupts: SPRS523 Table 9-18 (p. 66) and SPRS698 Table 8-17 (p. 82).  ADCINT1/ADCINT2 appear in PIE group 1 and
  again as INT10.1/INT10.2 in the data sheet: the group-10 copies are named ADCINT1_10 / ADCINT2_10 here so that names
  stay unique (the group-1 lines are the usual ones).

USB0 (2806x): the USB controller registers are byte registers addressed in bytes (SPRUH18 Table 17-3 gives byte
offsets).  On the C28x, byte offset b sits in word 0x4000 + b // 2, low byte for even b, high byte for odd b (TI's
driverlib accesses them with __byte()).  Each 16-bit word that holds one or two 8-bit registers is listed under the
name of its even-byte register (or the odd one when the even byte is unused) with one 8-bit field per byte register;
16-bit registers map to one word, 32-bit registers to a width-2 entry.

    .venv/bin/python -I tools/gen_piccolo_regs.py                       # writes both tables
    .venv/bin/python -I tools/gen_piccolo_regs.py --family 2802x        # one table
    .venv/bin/python -I tools/gen_piccolo_regs.py --check-ds 2802x ds.txt   # ds.txt = pdftotext -layout of SPRS523
    .venv/bin/python -I tools/gen_piccolo_regs.py --check-ds 2806x ds.txt   # ds.txt = pdftotext -layout of SPRS698
    .venv/bin/python -I tools/gen_piccolo_regs.py --bootrom-words chips/<file>.asm
The check parses the register tables of the data sheet text and compares every register address (exit 1 on a
mismatch); registers of covered blocks that the data sheet does not list are printed as NOTE.
"""
from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "chips" / "tms320f28232_regs.yaml"
DST = {"2802x": ROOT / "chips" / "tms320f2802x_regs.yaml", "2806x": ROOT / "chips" / "tms320f2806x_regs.yaml"}
DEVICE = {"2802x": "TMS320F2802x", "2806x": "TMS320F2806x"}


# --------------------------------------------------------------------------------------------- helpers
def _r(name, off, width=1, reset=0, access="read-write", fields=None, desc=None):
    """Register dict.  fields: (name, lsb, width[, access[, write_effect]])."""
    d = {"name": name, "offset": off, "width": width, "reset": reset,
         "reset_mask": 0xFFFFFFFF if width == 2 else 0xFFFF, "access": access}
    if desc:
        d["description"] = desc
    if fields:
        fl = []
        for f in sorted(fields, key=lambda f: f[1]):
            e = {"name": f[0], "lsb": f[1], "width": f[2]}
            if len(f) > 3 and f[3]:
                e["access"] = f[3]
            if len(f) > 4 and f[4]:
                e["write_effect"] = f[4]
            fl.append(e)
        d["fields"] = fl
    return d


def _blk(base, size, desc, regs, interrupts=None, group=None):
    d = {"base": base, "size": size, "derived_from": None, "description": desc, "group": group}
    if interrupts:
        d["interrupts"] = interrupts
    d["registers"] = sorted(regs, key=lambda r: r["offset"])
    return d


# --------------------------------------------------------------------------------------------- interrupts
def interrupts(fam: str) -> dict[int, str]:
    v = lambda x, y: 32 + 8 * (x - 1) + (y - 1)          # noqa: E731
    irq = {13: "INT13", 14: "INT14", 18: "NMI", 19: "ILLEGAL",
           v(1, 1): "ADCINT1", v(1, 2): "ADCINT2", v(1, 4): "XINT1", v(1, 5): "XINT2", v(1, 6): "ADCINT9",
           v(1, 7): "TINT0", v(1, 8): "WAKEINT",
           v(6, 1): "SPIRXINTA", v(6, 2): "SPITXINTA", v(8, 1): "I2CINT1A", v(8, 2): "I2CINT2A",
           v(9, 1): "SCIRXINTA", v(9, 2): "SCITXINTA",
           v(10, 1): "ADCINT1_10", v(10, 2): "ADCINT2_10", v(12, 1): "XINT3"}
    for i in range(3, 9):
        irq[v(10, i)] = f"ADCINT{i}"
    n_pwm = 4 if fam == "2802x" else 8
    for i in range(1, n_pwm + 1):
        irq[v(2, i)] = f"EPWM{i}_TZINT"
        irq[v(3, i)] = f"EPWM{i}_INT"
    irq[v(4, 1)] = "ECAP1_INT"
    if fam == "2806x":
        irq.update({v(4, 2): "ECAP2_INT", v(4, 3): "ECAP3_INT", v(4, 7): "HRCAP1_INT", v(4, 8): "HRCAP2_INT",
                    v(5, 1): "EQEP1_INT", v(5, 2): "EQEP2_INT", v(5, 4): "HRCAP3_INT", v(5, 5): "HRCAP4_INT",
                    v(5, 8): "USB0_INT",
                    v(6, 3): "SPIRXINTB", v(6, 4): "SPITXINTB", v(6, 5): "MRINTA", v(6, 6): "MXINTA",
                    v(9, 3): "SCIRXINTB", v(9, 4): "SCITXINTB", v(9, 5): "ECAN0_INTA", v(9, 6): "ECAN1_INTA",
                    v(12, 7): "LVF", v(12, 8): "LUF"})
        for i in range(1, 7):
            irq[v(7, i)] = f"DINTCH{i}"
        for i in range(1, 9):
            irq[v(11, i)] = f"CLA1_INT{i}"
    return dict(sorted(irq.items()))


def _irqs_named(fam: str, names) -> dict[int, str]:
    want = set(names)
    return {n: nm for n, nm in interrupts(fam).items() if nm in want}


# --------------------------------------------------------------------------------------------- system control
def sysctrl(fam: str) -> dict:
    if fam == "2802x":   # SPRUI09 Figures 1-14..1-16
        pclk0 = [("HRPWMENCLK", 0, 1), ("TBCLKSYNC", 2, 1), ("ADCENCLK", 3, 1), ("I2CAENCLK", 4, 1),
                 ("SPIAENCLK", 8, 1), ("SCIAENCLK", 10, 1)]
        pclk1 = [*((f"EPWM{i}ENCLK", i - 1, 1) for i in range(1, 5)), ("ECAP1ENCLK", 8, 1)]
        pclk3 = [("COMP1ENCLK", 0, 1), ("COMP2ENCLK", 1, 1), ("CPUTIMER0ENCLK", 8, 1), ("CPUTIMER1ENCLK", 9, 1),
                 ("CPUTIMER2ENCLK", 10, 1), ("GPIOINENCLK", 13, 1)]
    else:                # SPRUH18 Figures 1-14..1-17
        pclk0 = [("HRPWMENCLK", 0, 1), ("TBCLKSYNC", 2, 1), ("ADCENCLK", 3, 1), ("I2CAENCLK", 4, 1),
                 ("SPIAENCLK", 8, 1), ("SPIBENCLK", 9, 1), ("SCIAENCLK", 10, 1), ("SCIBENCLK", 11, 1),
                 ("MCBSPAENCLK", 12, 1), ("ECANAENCLK", 14, 1)]
        pclk1 = [*((f"EPWM{i}ENCLK", i - 1, 1) for i in range(1, 9)), ("ECAP1ENCLK", 8, 1), ("ECAP2ENCLK", 9, 1),
                 ("ECAP3ENCLK", 10, 1), ("EQEP1ENCLK", 14, 1), ("EQEP2ENCLK", 15, 1)]
        # USB0ENCLK: Figure 1-17 prints "R-0" but Table 1-18 describes a clock enable (treated as R/W).  Bit 13 is
        # reserved and reads 1 (no GPIOINENCLK on the 2806x).
        pclk3 = [("COMP1ENCLK", 0, 1), ("COMP2ENCLK", 1, 1), ("COMP3ENCLK", 2, 1), ("CPUTIMER0ENCLK", 8, 1),
                 ("CPUTIMER1ENCLK", 9, 1), ("CPUTIMER2ENCLK", 10, 1), ("DMAENCLK", 11, 1), ("CLA1ENCLK", 14, 1),
                 ("USB0ENCLK", 15, 1)]
    regs = [
        _r("XCLK", 0x00, fields=[("XCLKOUTDIV", 0, 2), ("XCLKINSEL", 6, 1)],
           desc="XCLKOUT divide (00 = SYSCLKOUT/4 at reset), XCLKIN pin select (0 = GPIO38, 1 = GPIO19)"),
        _r("PLLSTS", 0x01, reset=0x0001,
           fields=[("PLLLOCKS", 0, 1, "read-only"), ("PLLOFF", 2, 1), ("MCLKSTS", 3, 1, "read-only"),
                   ("MCLKCLR", 4, 1), ("OSCOFF", 5, 1), ("MCLKOFF", 6, 1), ("DIVSEL", 7, 2), ("NORMRDYE", 15, 1)],
           desc="PLL status: DIVSEL 0/1 = /4, 2 = /2, 3 = /1; reset by XRS / watchdog only"),
        _r("CLKCTL", 0x02,
           fields=[("OSCCLKSRCSEL", 0, 1), ("OSCCLKSRC2SEL", 1, 1), ("WDCLKSRCSEL", 2, 1), ("TMR2CLKSRCSEL", 3, 2),
                   ("TMR2CLKPRESCALE", 5, 3), ("INTOSC1OFF", 8, 1), ("INTOSC1HALTI", 9, 1), ("INTOSC2OFF", 10, 1),
                   ("INTOSC2HALTI", 11, 1), ("WDHALTI", 12, 1), ("XCLKINOFF", 13, 1), ("XTALOSCOFF", 14, 1),
                   ("NMIRESETSEL", 15, 1)],
           desc="Clock source selection (INTOSC1 at reset), oscillator enables, CPU timer 2 clock, NMI reset select"),
        _r("PLLLOCKPRD", 0x03, reset=0xFFFF, fields=[("PLLLOCKPRD", 0, 16)],
           desc="PLL lock period in OSCCLK cycles (reset 65535; XRS only)"),
        _r("INTOSC1TRIM", 0x04, fields=[("COARSETRIM", 0, 8), ("FINETRIM", 9, 6)],
           desc="INTOSC1 trim (loaded by Device_cal from OTP)"),
        _r("INTOSC2TRIM", 0x06, fields=[("COARSETRIM", 0, 8), ("FINETRIM", 9, 6)],
           desc="INTOSC2 trim (loaded by Device_cal from OTP)"),
        _r("LOSPCP", 0x0B, reset=0x0002, fields=[("LSPCLK", 0, 3)], desc="LSPCLK = SYSCLKOUT / (2 x LSPCLK), 0 = /1"),
        _r("PCLKCR0", 0x0C, fields=pclk0),
        _r("PCLKCR1", 0x0D, fields=pclk1),
        _r("LPMCR0", 0x0E, reset=0x00FC, fields=[("LPM", 0, 2), ("QUALSTDBY", 2, 6), ("WDINTE", 15, 1)]),
        _r("PCLKCR3", 0x10, reset=0x2700, fields=pclk3),
        (_r("PLLCR", 0x11, fields=[("DIV", 0, 5)], desc="PLL multiplier (0 = bypass, 1..18; SPRUH18 Figure 1-25, Table 1-24)")
         if fam == "2806x" else
         _r("PLLCR", 0x11, fields=[("DIV", 0, 4)], desc="PLL multiplier (0 = bypass, 1..12; SPRUI09 Figure 1-24)")),
        _r("SCSR", 0x12, reset=0x0005, fields=[("WDOVERRIDE", 0, 1, None, "oneToClear"), ("WDENINT", 1, 1),
                                               ("WDINTS", 2, 1, "read-only")]),
        _r("WDCNTR", 0x13, access="read-only", fields=[("WDCNTR", 0, 8)]),
        _r("WDKEY", 0x15, fields=[("WDKEY", 0, 8)]),
        _r("WDCR", 0x19, fields=[("WDPS", 0, 3), ("WDCHK", 3, 3), ("WDDIS", 6, 1),
                                 ("WDFLAG", 7, 1, None, "oneToClear")]),
        _r("JTAGDEBUG", 0x1A, fields=[("JTAGDIS", 0, 1)],
           desc="JTAG port disable (0x702A; SPRUI09 Figure 1-54 / SPRUH18 Figure 1-61; forced 0 while TRST = 0)"),
    ]
    if fam == "2806x":
        regs += [
            _r("PCLKCR2", 0x09, fields=[(f"HRCAP{i}ENCLK", 7 + i, 1) for i in range(1, 5)]),
            _r("PLL2CTL", 0x20, reset=0x0004, fields=[("PLL2CLKSRCSEL", 0, 2), ("PLL2EN", 2, 1)],
               desc="PLL2 (USB / HRCAP clock): source 00/01 INTOSC1, 10 X1, 11 XCLKIN; PLL2EN = 1 at reset "
                    "(SPRUH18 Figure 1-29; the text also says X1 is selected after XRS, the figure says 00)"),
            _r("PLL2MULT", 0x22, fields=[("PLL2MULT", 0, 4)]),
            _r("PLL2STS", 0x24, access="read-only", fields=[("PLL2LOCKS", 0, 1)]),
            _r("SYSCLK2CNTR", 0x26, fields=[("COUNT", 0, 16)], desc="free-running SYSCLK2 counter (R/C)"),
            _r("EPWMCFG", 0x2A, fields=[("CONFIG", 0, 1)],
               desc="ePWM registers on the CLA bus (0) or the DMA bus (1)"),
        ]
        size, desc = 0x30, ("System control (2806x, SPRUH18 Table 1-14): clock sources, PLL, PLL2, clock gates "
                            "PCLKCR0-3, low-power modes, watchdog, EPWMCFG")
    else:
        size, desc = 0x20, ("System control (2802x, SPRUI09 Table 1-14): clock sources, PLL, clock gates PCLKCR0/1/3, "
                            "low-power modes, watchdog")
    return _blk(0x7010, size, desc, regs, interrupts=_irqs_named(fam, ["WAKEINT"]), group="SYSCTRL")


def nmiint(fam: str) -> dict:
    """SPRUI09 Figures 1-29..1-34."""
    regs = [
        _r("NMICFG", 0x0, fields=[("CLOCKFAIL", 1, 1)], desc="CLOCKFAIL NMI enable (write 1 only, reset clears)"),
        _r("NMIFLG", 0x1, access="read-only", fields=[("NMIINT", 0, 1), ("CLOCKFAIL", 1, 1)],
           desc="NMI flags (XRS reset only)"),
        _r("NMIFLGCLR", 0x2, fields=[("NMIINT", 0, 1, "write-only"), ("CLOCKFAIL", 1, 1, "write-only")]),
        _r("NMIFLGFRC", 0x3, fields=[("CLOCKFAIL", 1, 1, "write-only")]),
        _r("NMIWDCNT", 0x4, access="read-only", fields=[("NMIWDCNT", 0, 16)], desc="counts SYSCLKOUT while a flag is set"),
        _r("NMIWDPRD", 0x5, reset=0xFFFF, fields=[("NMIWDPRD", 0, 16)]),
    ]
    return _blk(0x7060, 0x10, "NMI watchdog / CLOCKFAIL interrupt (SPRUI09 1.3.2.7, SPRUH18 Table 1-32)", regs,
                interrupts={18: "NMI"}, group="NMIINT")


def dev_emu(fam: str) -> dict:
    devicecnf = [("VMAPS", 3, 1, "read-only"), ("XRS", 5, 1, "read-only"), ("ENPROT", 19, 1),
                 ("TRST", 27, 1, "read-only")]
    if fam == "2806x":
        devicecnf.append(("SYSCLK2DIV2DIS", 30, 1))
        cls = (0x009F, "0x009F = F28069/U/M/F; F28068x/67/66 0x008F, F28065 0x007F, F28064/63/62 0x006F (SPRS698 "
                       "Table 8-10)")
    else:
        cls = (0x00CF, "0x00CF = F28027/F/F28023/F28021; F28026/F/F28022/F28020/F280200 0x00C7 (SPRS523 Table 9-11)")
    regs = [
        _r("DEVICECNF", 0x00, width=2, reset=0x000F002B, fields=devicecnf,
           desc="Device configuration (bits 18:16 and 2:0 reserved, reset 111 / 011; XRS reads 1 while running)"),
        _r("CLASSID", 0x02, reset=cls[0], access="read-only", desc=cls[1]),
        _r("REVID", 0x03, reset=0x0002, access="read-only",
           desc="silicon revision: 0 = rev 0, 1 = rev A, 2 = rev B (rev B assumed)"),
    ]
    return _blk(0x0880, 0x105, "Device emulation registers 0x0880-0x0984 (DEVICECNF, CLASSID, REVID; PARTID is in "
                "OTP)", regs, group="DEV_EMU")


def borcfg(fam: str) -> dict:
    regs = [_r("BORCFG", 0x0, reset=0x0004, fields=[("BORENZ", 0, 1)],
               desc="BOR enable (active low); bit 2 reserved, reads 1")]
    return _blk(0x0985, 0x3, "System power control (BORCFG, SPRUI09 Figure 1-98 / SPRUH18 Figure 1-107)", regs,
                group="BORCFG")


# --------------------------------------------------------------------------------------------- GPIO / XINT
def gpio(fam: str) -> dict:
    a_pins = list(range(32))
    if fam == "2802x":
        b_pins = list(range(7))                          # GPIO32-38
        apud, bpud = 0x00000FFF, 0x00000000
    else:
        b_pins = list(range(13)) + list(range(18, 27))   # GPIO32-44, GPIO50-58
        apud, bpud = 0x00000FFF, 0x00000F00              # SPRUH18 Figures 1-80/1-81: GPIO0-11, GPIO40-43 off
    aios = (2, 4, 6, 10, 12, 14)

    def per_pin(prefix_port, pins, base_num):
        return [(f"GPIO{base_num + b}", b, 1) for b in pins]

    def mux(pins, lo, base_num):
        return [(f"GPIO{base_num + b}", 2 * (b - lo), 2) for b in pins if lo <= b < lo + 16]

    def ctrl(pins):
        return [(f"QUALPRD{k}", 8 * k, 8) for k in range(4) if any(8 * k <= b < 8 * k + 8 for b in pins)]

    regs = [
        _r("GPACTRL", 0x00, width=2, fields=ctrl(a_pins)),
        _r("GPAQSEL1", 0x02, width=2, fields=mux(a_pins, 0, 0)),
        _r("GPAQSEL2", 0x04, width=2, fields=mux(a_pins, 16, 0)),
        _r("GPAMUX1", 0x06, width=2, fields=mux(a_pins, 0, 0)),
        _r("GPAMUX2", 0x08, width=2, fields=mux(a_pins, 16, 0)),
        _r("GPADIR", 0x0A, width=2, fields=per_pin("A", a_pins, 0)),
        _r("GPAPUD", 0x0C, width=2, reset=apud, fields=per_pin("A", a_pins, 0)),
        _r("GPBCTRL", 0x10, width=2, fields=ctrl(b_pins)),
        _r("GPBQSEL1", 0x12, width=2, fields=mux(b_pins, 0, 32)),
        _r("GPBMUX1", 0x16, width=2, fields=mux(b_pins, 0, 32)),
        _r("GPBDIR", 0x1A, width=2, fields=per_pin("B", b_pins, 32)),
        _r("GPBPUD", 0x1C, width=2, reset=bpud, fields=per_pin("B", b_pins, 32)),
        _r("AIOMUX1", 0x36, width=2, reset=sum(2 << (2 * n) for n in aios),
           fields=[(f"AIO{n}", 2 * n, 2) for n in aios],
           desc="AIOn: 00/01 = AIO (digital) enabled, 10/11 = disabled (reset 1x; x taken as 0)"),
        _r("AIODIR", 0x3A, width=2, fields=[(f"AIO{n}", n, 1) for n in aios], desc="1 = output (reset x: 0)"),
        _r("GPADAT", 0x40, width=2, fields=per_pin("A", a_pins, 0)),
        _r("GPASET", 0x42, width=2, fields=per_pin("A", a_pins, 0)),
        _r("GPACLEAR", 0x44, width=2, fields=per_pin("A", a_pins, 0)),
        _r("GPATOGGLE", 0x46, width=2, fields=per_pin("A", a_pins, 0)),
        _r("GPBDAT", 0x48, width=2, fields=per_pin("B", b_pins, 32)),
        _r("GPBSET", 0x4A, width=2, fields=per_pin("B", b_pins, 32)),
        _r("GPBCLEAR", 0x4C, width=2, fields=per_pin("B", b_pins, 32)),
        _r("GPBTOGGLE", 0x4E, width=2, fields=per_pin("B", b_pins, 32)),
        _r("AIODAT", 0x58, width=2, fields=[(f"AIO{n}", n, 1) for n in aios]),
        _r("AIOSET", 0x5A, width=2, fields=[(f"AIO{n}", n, 1) for n in aios]),
        _r("AIOCLEAR", 0x5C, width=2, fields=[(f"AIO{n}", n, 1) for n in aios]),
        _r("AIOTOGGLE", 0x5E, width=2, fields=[(f"AIO{n}", n, 1) for n in aios]),
        _r("GPIOXINT1SEL", 0x60, fields=[("GPIOSEL", 0, 5)]),
        _r("GPIOXINT2SEL", 0x61, fields=[("GPIOSEL", 0, 5)]),
        _r("GPIOXINT3SEL", 0x62, fields=[("GPIOSEL", 0, 5)]),
        _r("GPIOLPMSEL", 0x68, width=2, fields=per_pin("A", a_pins, 0),
           desc="32-bit (Figure 1-73 and the data sheet; SPRUI09 Table 1-52 says size 1)"),
    ]
    if fam == "2806x":
        regs += [_r("GPBQSEL2", 0x14, width=2, fields=mux(b_pins, 16, 32)),
                 _r("GPBMUX2", 0x18, width=2, fields=mux(b_pins, 16, 32))]
        desc = ("GPIO ports A (GPIO0-31), B (GPIO32-44, GPIO50-58), analog I/O AIO2/4/6/10/12/14, XINT1-3 and LPM "
                "select (SPRUH18 1.4, SPRS698 Table 8-38)")
    else:
        desc = ("GPIO ports A (GPIO0-31), B (GPIO32-38), analog I/O AIO2/4/6/10/12/14, XINT1-3 and LPM select "
                "(SPRUI09 1.4, SPRS523 Table 9-29)")
    return _blk(0x6F80, 0x80, desc, regs, group="GPIO")


def xint(fam: str) -> dict:
    regs = []
    for n in (1, 2, 3):
        regs.append(_r(f"XINT{n}CR", n - 1, fields=[("ENABLE", 0, 1), ("POLARITY", 2, 2)]))
        regs.append(_r(f"XINT{n}CTR", 7 + n, access="read-only", fields=[("INTCTR", 0, 16)]))
    return _blk(0x7070, 0x10, "External interrupts XINT1-3 (SPRUI09 Figures 1-96/1-97)", regs,
                interrupts=_irqs_named(fam, ["XINT1", "XINT2", "XINT3"]), group="XINT")


# --------------------------------------------------------------------------------------------- ADC / comparators
def adc(fam: str) -> dict:
    ctl2 = [("CLKDIV2EN", 0, 1), ("ADCNONOVERLAP", 1, 1)]
    if fam == "2806x":
        ctl2.append(("CLKDIV4EN", 2, 1))
    intflg = [(f"ADCINT{i}", i - 1, 1) for i in range(1, 10)]
    regs = [
        # ADCENABLE: both TRM figures print "R-1"; the field text says it must be set by software (R/W, reset 0).
        _r("ADCCTL1", 0x00, fields=[("TEMPCONV", 0, 1), ("VREFLOCONV", 1, 1), ("INTPULSEPOS", 2, 1),
                                    ("ADCREFSEL", 3, 1), ("ADCREFPWD", 5, 1), ("ADCBGPWD", 6, 1), ("ADCPWDN", 7, 1),
                                    ("ADCBSYCHN", 8, 5, "read-only"), ("ADCBSY", 13, 1, "read-only"),
                                    ("ADCENABLE", 14, 1), ("RESET", 15, 1)],
           desc="RESET reads 0 (write 1: module reset); ADCPWDN printed ADCPWN in the TRM figures"),
        _r("ADCCTL2", 0x01, fields=ctl2),
        _r("ADCINTFLG", 0x04, access="read-only", fields=intflg),
        _r("ADCINTFLGCLR", 0x05, fields=[(n, l, w, "write-only") for n, l, w in intflg]),
        _r("ADCINTOVF", 0x06, access="read-only", fields=intflg),
        _r("ADCINTOVFCLR", 0x07, fields=[(n, l, w, "write-only") for n, l, w in intflg]),
    ]
    for k, (a, b) in enumerate(((1, 2), (3, 4), (5, 6), (7, 8), (9, 10))):
        f = [(f"INT{a}SEL", 0, 5), (f"INT{a}E", 5, 1), (f"INT{a}CONT", 6, 1)]
        if b != 10:
            f += [(f"INT{b}SEL", 8, 5), (f"INT{b}E", 13, 1), (f"INT{b}CONT", 14, 1)]
        regs.append(_r(f"INTSEL{a}N{b}", 0x08 + k, fields=f))
    regs += [
        _r("SOCPRICTL", 0x10, reset=0x0400, fields=[("SOCPRIORITY", 0, 5), ("RRPOINTER", 5, 6, "read-only"),
                                                    ("ONESHOT", 15, 1)],
           desc="RRPOINTER resets to 0x20 (no SOC converted yet)"),
        _r("ADCSAMPLEMODE", 0x12, fields=[(f"SIMULEN{2 * i}", i, 1) for i in range(8)]),
        _r("ADCINTSOCSEL1", 0x14, fields=[(f"SOC{i}", 2 * i, 2) for i in range(8)]),
        _r("ADCINTSOCSEL2", 0x15, fields=[(f"SOC{i + 8}", 2 * i, 2) for i in range(8)]),
        _r("ADCSOCFLG1", 0x18, access="read-only", fields=[(f"SOC{i}", i, 1) for i in range(16)]),
        _r("ADCSOCFRC1", 0x1A, fields=[(f"SOC{i}", i, 1, "write-only") for i in range(16)]),
        _r("ADCSOCOVF1", 0x1C, access="read-only", fields=[(f"SOC{i}", i, 1) for i in range(16)]),
        _r("ADCSOCOVFCLR1", 0x1E, fields=[(f"SOC{i}", i, 1, "write-only") for i in range(16)]),
    ]
    for i in range(16):
        regs.append(_r(f"ADCSOC{i}CTL", 0x20 + i, fields=[("ACQPS", 0, 6), ("CHSEL", 6, 4), ("TRIGSEL", 11, 5)]))
    regs += [
        _r("ADCREFTRIM", 0x40, fields=[("BG_FINE_TRIM", 0, 5), ("BG_COARSE_TRIM", 5, 4), ("EXTREF_FINE_TRIM", 9, 5)],
           desc="loaded by Device_cal"),
        _r("ADCOFFTRIM", 0x41, fields=[("OFFTRIM", 0, 9)], desc="2's complement offset trim, loaded by Device_cal"),
        _r("COMPHYSTCTL", 0x4C, fields=[("COMP1_HYST_DISABLE", 1, 1), ("COMP2_HYST_DISABLE", 6, 1),
                                        ("COMP3_HYST_DISABLE", 11, 1)]),
        _r("ADCREV", 0x4F, reset=0x0003, access="read-only", fields=[("TYPE", 0, 8), ("REV", 8, 8)],
           desc="TYPE = 3; REV not documented (R-x, 0 here)"),
    ]
    trig = "0 software, 1-3 CPU timer 0-2, 4 XINT2, 5.. ePWMnSOCA/B (5 + 2(n-1) / 6 + 2(n-1))"
    irqs = {n: nm for n, nm in interrupts(fam).items() if nm.startswith("ADCINT")}
    return _blk(0x7100, 0x80, f"Type-3 SOC ADC (SPRUI09 ch. 6 / SPRUH18 ch. 8); TRIGSEL: {trig}", regs,
                interrupts=irqs, group="ADC")


def adcresult(fam: str) -> dict:
    regs = [_r(f"ADCRESULT{i}", i, access="read-only", fields=[("RESULT", 0, 12)]) for i in range(16)]
    return _blk(0x0B00, 0x10, "ADC results, right-justified (Peripheral Frame 0, SPRUI09 Figure 6-38)", regs,
                group="ADC")


def comp(n: int) -> dict:
    regs = [
        _r("COMPCTL", 0x00, fields=[("COMPDACEN", 0, 1), ("COMPSOURCE", 1, 1), ("CMPINV", 2, 1), ("QUALSEL", 3, 5),
                                    ("SYNCSEL", 8, 1)]),
        _r("COMPSTS", 0x02, access="read-only", fields=[("COMPSTS", 0, 1)]),
        _r("DACCTL", 0x04, fields=[("DACSOURCE", 0, 1), ("RAMPSOURCE", 1, 4), ("FREE_SOFT", 14, 2)]),
        _r("DACVAL", 0x06, fields=[("DACVAL", 0, 10)]),
        _r("RAMPMAXREF_ACTIVE", 0x08, access="read-only", fields=[("RAMPMAXREFA", 0, 16)]),
        _r("RAMPMAXREF_SHDW", 0x0A, fields=[("RAMPMAXREFS", 0, 16)]),
        _r("RAMPDECVAL_ACTIVE", 0x0C, access="read-only", fields=[("RAMPDECVALA", 0, 16)]),
        _r("RAMPDECVAL_SHDW", 0x0E, fields=[("RAMPDECVALS", 0, 16)]),
        _r("RAMPSTS", 0x10, access="read-only", fields=[("RAMPVALUE", 0, 16)]),
    ]
    return _blk(0x6400 + 0x20 * (n - 1), 0x20, f"Comparator {n} with 10-bit DAC and ramp generator (SPRUI09 Table "
                "7-3, SPRUH18 Table 9-3)", regs, group="COMP")


# --------------------------------------------------------------------------------------------- ePWM type 1
_DCEVT = [("DCAEVT1", 3), ("DCAEVT2", 4), ("DCBEVT1", 5), ("DCBEVT2", 6)]


def epwm(src: dict, fam: str, n: int) -> dict:
    keep = {r["name"]: copy.deepcopy(r) for r in src["EPWM1"]["registers"]}
    for k in ("TZSEL", "TZCTL", "TZEINT", "TZFLG", "TZCLR", "TZFRC", "HRCNFG"):
        del keep[k]
    regs = list(keep.values())
    regs += [
        _r("TBPRDHR", 0x06, fields=[("TBPRDHR", 8, 8)], desc="HRPWM period extension (bits 7:0 reserved)"),
        _r("TZSEL", 0x12, fields=[*((f"CBC{i}", i - 1, 1) for i in range(1, 7)), ("DCAEVT2", 6, 1),
                                   ("DCBEVT2", 7, 1), *((f"OSHT{i}", 7 + i, 1) for i in range(1, 7)),
                                   ("DCAEVT1", 14, 1), ("DCBEVT1", 15, 1)]),
        _r("TZDCSEL", 0x13, fields=[("DCAEVT1", 0, 3), ("DCAEVT2", 3, 3), ("DCBEVT1", 6, 3), ("DCBEVT2", 9, 3)]),
        _r("TZCTL", 0x14, fields=[("TZA", 0, 2), ("TZB", 2, 2), ("DCAEVT1", 4, 2), ("DCAEVT2", 6, 2),
                                   ("DCBEVT1", 8, 2), ("DCBEVT2", 10, 2)]),
        _r("TZEINT", 0x15, fields=[("CBC", 1, 1), ("OST", 2, 1), *((nm, b, 1) for nm, b in _DCEVT)]),
        _r("TZFLG", 0x16, access="read-only", fields=[("INT", 0, 1), ("CBC", 1, 1), ("OST", 2, 1),
                                                      *((nm, b, 1) for nm, b in _DCEVT)]),
        _r("TZCLR", 0x17, fields=[("INT", 0, 1, "write-only"), ("CBC", 1, 1, "write-only"),
                                   ("OST", 2, 1, "write-only"), *((nm, b, 1, "write-only") for nm, b in _DCEVT)]),
        _r("TZFRC", 0x18, fields=[("CBC", 1, 1, "write-only"), ("OST", 2, 1, "write-only"),
                                   *((nm, b, 1, "write-only") for nm, b in _DCEVT)]),
        _r("HRCNFG", 0x20, fields=[("EDGMODE", 0, 2), ("CTLMODE", 2, 1), ("HRLOAD", 3, 2), ("SELOUTB", 5, 1),
                                    ("AUTOCONV", 6, 1), ("SWAPAB", 7, 1)]),
        _r("HRPCTL", 0x28, fields=[("HRPE", 0, 1), ("PWMSYNCSEL", 1, 1), ("TBPHSHRLOADE", 2, 1)]),
        _r("TBPRDHRM", 0x2A, fields=[("TBPRDHR", 8, 8)], desc="mirror of TBPRDHR (writes; 32-bit write with TBPRDM)"),
        _r("TBPRDM", 0x2B, desc="mirror of TBPRD (writes go to TBPRD; reads return the active TBPRD)"),
        _r("CMPAHRM", 0x2C, fields=[("CMPAHR", 8, 8)], desc="mirror of CMPAHR (writes; 32-bit write with CMPAM)"),
        _r("CMPAM", 0x2D, desc="mirror of CMPA (writes go to CMPA; reads return the active CMPA)"),
        _r("DCTRIPSEL", 0x30, fields=[("DCAHCOMPSEL", 0, 4), ("DCALCOMPSEL", 4, 4), ("DCBHCOMPSEL", 8, 4),
                                       ("DCBLCOMPSEL", 12, 4)],
           desc="DCAH/DCAL/DCBH/DCBL input: 0-2 TZ1-3, 8 COMP1OUT, 9 COMP2OUT, 10 COMP3OUT (2806x)"),
        _r("DCACTL", 0x31, fields=[("EVT1SRCSEL", 0, 1), ("EVT1FRCSYNCSEL", 1, 1), ("EVT1SOCE", 2, 1),
                                    ("EVT1SYNCE", 3, 1), ("EVT2SRCSEL", 8, 1), ("EVT2FRCSYNCSEL", 9, 1)]),
        _r("DCBCTL", 0x32, fields=[("EVT1SRCSEL", 0, 1), ("EVT1FRCSYNCSEL", 1, 1), ("EVT1SOCE", 2, 1),
                                    ("EVT1SYNCE", 3, 1), ("EVT2SRCSEL", 8, 1), ("EVT2FRCSYNCSEL", 9, 1)]),
        _r("DCFCTL", 0x33, fields=[("SRCSEL", 0, 2), ("BLANKE", 2, 1), ("BLANKINV", 3, 1), ("PULSESEL", 4, 2)]),
        _r("DCCAPCTL", 0x34, fields=[("CAPE", 0, 1), ("SHDWMODE", 1, 1)]),
        _r("DCFOFFSET", 0x35, fields=[("OFFSET", 0, 16)],
           desc="blanking window offset (shadowed; the TRM figure prints R-0, the text and Table 3-1 a writable "
                "shadowed register)"),
        _r("DCFOFFSETCNT", 0x36, access="read-only", fields=[("OFFSETCNT", 0, 16)]),
        _r("DCFWINDOW", 0x37, fields=[("WINDOW", 0, 8)]),
        _r("DCFWINDOWCNT", 0x38, access="read-only", fields=[("WINDOWCNT", 0, 8)]),
        _r("DCCAP", 0x39, access="read-only", fields=[("DCCAP", 0, 16)]),
    ]
    if n == 1:
        regs += [_r("HRPWR", 0x21, fields=[("MEPOFF", 6, 4)], desc="HRPWM power (ePWM1 only; TI test bits)"),
                 _r("HRMSTEP", 0x26, fields=[("HRMSTEP", 0, 8)], desc="MEP steps per SYSCLKOUT for AUTOCONV "
                    "(ePWM1 only)")]
    irqs = {nn: nm for nn, nm in interrupts(fam).items() if nm in (f"EPWM{n}_TZINT", f"EPWM{n}_INT")}
    return _blk(0x6800 + 0x40 * (n - 1), 0x40, f"ePWM{n} + HRPWM{n} (type 1 with digital compare; SPRUI09 Table 3-1 "
                "/ SPRUH18 Table 3-1)", regs, interrupts=irqs, group="EPWM")


# --------------------------------------------------------------------------------------------- 2806x-only blocks
def cla() -> dict:
    ints = [(f"INT{i}", i - 1, 1) for i in range(1, 9)]
    regs = [_r(f"MVECT{i}", i - 1) for i in range(1, 9)]
    regs += [
        _r("MCTL", 0x10, fields=[("HARDRESET", 0, 1), ("SOFTRESET", 1, 1), ("IACKE", 2, 1)]),
        _r("MMEMCFG", 0x11, fields=[("PROGE", 0, 1), ("RAM0E", 4, 1), ("RAM1E", 5, 1), ("RAM2E", 6, 1),
                                    ("RAM0CPUE", 8, 1), ("RAM1CPUE", 9, 1), ("RAM2CPUE", 10, 1)]),
        _r("MPISRCSEL1", 0x14, width=2, fields=[(f"PERINT{i}SEL", 4 * (i - 1), 4) for i in range(1, 9)]),
        _r("MIFR", 0x20, fields=ints), _r("MIOVF", 0x21, fields=ints),
        _r("MIFRC", 0x22, fields=[(n, l, w, "write-only") for n, l, w in ints]),
        _r("MICLR", 0x23, fields=[(n, l, w, "write-only") for n, l, w in ints]),
        _r("MICLROVF", 0x24, fields=[(n, l, w, "write-only") for n, l, w in ints]),
        _r("MIER", 0x25, fields=ints),
        _r("MIRUN", 0x26, access="read-only", fields=ints),
        _r("MIPCTL", 0x27, desc="interrupt priority control (SPRS698 Table 8-20; not in SPRUH18 Table 10-20)"),
        _r("MPC", 0x28, access="read-only"),
        # SPRS698 Table 8-20 puts MAR0/MAR1 at 0x142A/0x142B; SPRUH18 Table 10-20 at 0x29/0x2A (data sheet used).
        _r("MAR0", 0x2A, access="read-only"), _r("MAR1", 0x2B, access="read-only"),
        _r("MSTF", 0x2E, width=2, access="read-only"),
        _r("MR0", 0x30, width=2, access="read-only"), _r("MR1", 0x34, width=2, access="read-only"),
        _r("MR2", 0x38, width=2, access="read-only"), _r("MR3", 0x3C, width=2, access="read-only"),
    ]
    irqs = {n: nm for n, nm in interrupts("2806x").items() if nm.startswith("CLA1_") or nm in ("LVF", "LUF")}
    return _blk(0x1400, 0x80, "Control law accelerator CLA1 registers (SPRUH18 Table 10-20, SPRS698 Table 8-20); the "
                "message RAMs 0x1480-0x157F are memory", regs, interrupts=irqs, group="CLA")


def hrcap(n: int) -> dict:
    base = {1: 0x6AC0, 2: 0x6AE0, 3: 0x6C80, 4: 0x6CA0}[n]
    flags = [("INT", 0, 1), ("RISE", 1, 1), ("FALL", 2, 1), ("COUNTEROVF", 3, 1), ("RISEOVF", 4, 1)]
    regs = [
        _r("HCCTL", 0x00, fields=[("SOFTRESET", 0, 1), ("RISEINTE", 1, 1), ("FALLINTE", 2, 1), ("OVFINTE", 3, 1),
                                  ("HCCAPCLKSEL", 8, 1)]),
        _r("HCIFR", 0x01, access="read-only", fields=flags),
        _r("HCICLR", 0x02, fields=[(a, b, c, "write-only") for a, b, c in flags]),
        _r("HCIFRC", 0x03, fields=[(a, b, c, "write-only") for a, b, c in flags[1:]]),
        _r("HCCOUNTER", 0x04, access="read-only"),
        _r("HCCAPCNTRISE0", 0x10, access="read-only"), _r("HCCAPCNTFALL0", 0x12, access="read-only"),
        _r("HCCAPCNTRISE1", 0x18, access="read-only"), _r("HCCAPCNTFALL1", 0x1A, access="read-only"),
    ]
    irqs = {nn: nm for nn, nm in interrupts("2806x").items() if nm == f"HRCAP{n}_INT"}
    return _blk(base, 0x20, f"High-resolution capture HRCAP{n} (SPRUH18 ch. 5, SPRS698 Table 8-36)", regs,
                interrupts=irqs, group="HRCAP")


# USB0 byte registers (SPRUH18 Table 17-3): (byte offset, name, bits, access, reset)
USB_REGS = [
    (0x000, "USBFADDR", 8, "rw", 0x0), (0x001, "USBPOWER", 8, "rw", 0x20), (0x002, "USBTXIS", 16, "ro", 0x0),
    (0x004, "USBRXIS", 16, "ro", 0x0), (0x006, "USBTXIE", 16, "rw", 0xFFFF), (0x008, "USBRXIE", 16, "rw", 0xFFFE),
    (0x00A, "USBIS", 8, "ro", 0x0), (0x00B, "USBIE", 8, "rw", 0x6), (0x00C, "USBFRAME", 16, "ro", 0x0),
    (0x00E, "USBEPIDX", 8, "rw", 0x0), (0x00F, "USBTEST", 8, "rw", 0x0),
    (0x020, "USBFIFO0", 32, "rw", 0x0), (0x024, "USBFIFO1", 32, "rw", 0x0), (0x028, "USBFIFO2", 32, "rw", 0x0),
    (0x02C, "USBFIFO3", 32, "rw", 0x0), (0x060, "USBDEVCTL", 8, "rw", 0x80), (0x062, "USBTXFIFOSZ", 8, "rw", 0x0),
    (0x063, "USBRXFIFOSZ", 8, "rw", 0x0), (0x064, "USBTXFIFOADD", 16, "rw", 0x0),
    (0x066, "USBRXFIFOADD", 16, "rw", 0x0), (0x07A, "USBCONTIM", 8, "rw", 0x5C), (0x07D, "USBFSEOF", 8, "rw", 0x77),
    (0x07E, "USBLSEOF", 8, "rw", 0x72),
    (0x080, "USBTXFUNCADDR0", 8, "rw", 0x0), (0x082, "USBTXHUBADDR0", 8, "rw", 0x0),
    (0x083, "USBTXHUBPORT0", 8, "rw", 0x0),
    *((0x080 + 8 * e + d, f"USB{k}{e}", 8, "rw", 0x0) for e in (1, 2, 3)
      for d, k in ((0, "TXFUNCADDR"), (2, "TXHUBADDR"), (3, "TXHUBPORT"), (4, "RXFUNCADDR"), (6, "RXHUBADDR"),
                   (7, "RXHUBPORT"))),
    (0x102, "USBCSRL0", 8, "w1c", 0x0), (0x103, "USBCSRH0", 8, "w1c", 0x0), (0x108, "USBCOUNT0", 8, "ro", 0x0),
    (0x10A, "USBTYPE0", 8, "rw", 0x0), (0x10B, "USBNAKLMT", 8, "rw", 0x0),
    *((0x100 + 0x10 * e + d, f"USB{k}{e}", bits, acc, 0x0) for e in (1, 2, 3)
      for d, k, bits, acc in ((0x0, "TXMAXP", 16, "rw"), (0x2, "TXCSRL", 8, "rw"), (0x3, "TXCSRH", 8, "rw"),
                              (0x4, "RXMAXP", 16, "rw"), (0x6, "RXCSRL", 8, "rw"), (0x7, "RXCSRH", 8, "rw"),
                              (0x8, "RXCOUNT", 16, "ro"), (0xA, "TXTYPE", 8, "rw"), (0xB, "TXINTERVAL", 8, "rw"),
                              (0xC, "RXTYPE", 8, "rw"), (0xD, "RXINTERVAL", 8, "rw"))),
    (0x304, "USBRQPKTCOUNT1", 16, "rw", 0x0), (0x308, "USBRQPKTCOUNT2", 16, "rw", 0x0),
    (0x30C, "USBRQPKTCOUNT3", 16, "rw", 0x0), (0x340, "USBRXDPKTBUFDIS", 16, "rw", 0x0),
    (0x342, "USBTXDPKTBUFDIS", 16, "rw", 0x0),
    (0x400, "USBEPC", 32, "rw", 0x0), (0x404, "USBEPCRIS", 32, "ro", 0x0), (0x408, "USBEPCIM", 32, "rw", 0x0),
    (0x40C, "USBEPCISC", 32, "rw", 0x0), (0x410, "USBDRRIS", 32, "ro", 0x0), (0x414, "USBDRIM", 32, "rw", 0x0),
    (0x418, "USBDRISC", 32, "w1c", 0x0), (0x41C, "USBGPCS", 32, "rw", 0x0), (0x450, "USBDMASEL", 32, "rw", 0x332211),
]


def usb0() -> dict:
    words: dict[int, list] = {}
    regs = []
    for boff, name, bits, acc, rst in sorted(USB_REGS):
        if bits == 8:
            words.setdefault(boff // 2, []).append((boff, name, acc, rst))
            continue
        access = "read-only" if acc == "ro" else "read-write"
        regs.append(_r(name, boff // 2, width=2 if bits == 32 else 1, reset=rst, access=access,
                       desc=f"byte offset 0x{boff:03X}" + (" (write 1 to clear)" if acc == "w1c" else "")))
    for w, parts in sorted(words.items()):
        name = parts[0][1]
        reset, fields = 0, []
        for boff, nm, acc, rst in parts:
            sh = 8 * (boff & 1)
            reset |= rst << sh
            fields.append((nm, sh, 8, "read-only" if acc == "ro" else None))
        allro = all(p[2] == "ro" for p in parts)
        regs.append(_r(name, w, reset=reset, access="read-only" if allro else "read-write", fields=fields,
                       desc="byte register(s) " + ", ".join(f"{p[1]} @ byte 0x{p[0]:03X}" for p in parts)))
    irqs = {n: nm for n, nm in interrupts("2806x").items() if nm == "USB0_INT"}
    return _blk(0x4000, 0x1000, "USB0 controller (SPRUH18 Table 17-3; byte registers packed two per word, see the "
                "generator docstring); 0x4000-0x4FFF", regs, interrupts=irqs, group="USB")


def dma(src: dict) -> dict:
    d = copy.deepcopy(src["DMA"])
    for r in d["registers"]:
        if r["name"].endswith("_MODE"):
            r["fields"] = [f for f in r.get("fields", []) if f["name"] not in ("SYNCE", "SYNCSEL")]
    d["description"] = ("DMA, 6 channels (SPRUH18 ch. 11, Table 11-2): MODE has no SYNCE/SYNCSEL; PERINTSEL 1 ADCINT1, "
                        "2 ADCINT2, 3-5 XINT1-3, 7-10 USB0EP1RX/TX..EP2RX/TX, 11-13 TINT0-2, 14 MXEVTA, 15 MREVTA, "
                        "18-29 ePWM2-7 SOCA/SOCB, 30/31 USB0EP3RX/TX (Figure 11-12)")
    d["interrupts"] = {n: nm for n, nm in interrupts("2806x").items() if nm.startswith("DINTCH")}
    return d


# --------------------------------------------------------------------------------------------- assembly
def build(src_doc: dict, fam: str) -> dict:
    sp = src_doc["peripherals"]
    out: dict[str, dict] = {}

    def cp(name, new=None, base=None, desc=None, irqs=None):
        b = copy.deepcopy(sp[name])
        if base is not None:
            b["base"] = base
        if desc:
            b["description"] = desc
        if irqs is not None:
            b["interrupts"] = irqs
        else:
            b.pop("interrupts", None)
        out[new or name] = b
        return b

    out["DEV_EMU"] = dev_emu(fam)
    out["BORCFG"] = borcfg(fam)
    cp("FLASH", desc="Flash/OTP configuration (SPRUI09 Table 1-1 / SPRUH18 Table 1-1, same layout as the F2833x)")
    cp("CSM", desc="Code security module KEY0-7, CSMSCR (SPRUI09 Table 1-12)")
    out["ADCRESULT"] = adcresult(fam)
    for i in range(3):
        cp(f"CPUTIMER{i}", desc=f"CPU timer {i} (SPRUI09 Table 1-41)",
           irqs={38: "TINT0"} if i == 0 else ({13: "INT13"} if i == 1 else {14: "INT14"}))
    cp("PIE", desc="PIE control (SPRUI09 Table 1-112 / SPRUH18 Table 1-121)")
    if fam == "2806x":
        out["DMA"] = dma(sp)
        out["CLA1"] = cla()
        out["USB0"] = usb0()
        cp("MCBSPA", desc="McBSP-A (SPRUH18 ch. 15, SPRS698 Table 8-29)",
           irqs=_irqs_named(fam, ["MRINTA", "MXINTA"]))
        cp("ECANA", desc="eCAN-A with mailbox RAM 0x6100 (SPRUH18 ch. 16, SPRS698 Table 8-31)",
           irqs=_irqs_named(fam, ["ECAN0_INTA", "ECAN1_INTA"]))
    ncomp = 2 if fam == "2802x" else 3
    for n in range(1, ncomp + 1):
        out[f"COMP{n}"] = comp(n)
    npwm = 4 if fam == "2802x" else 8
    for n in range(1, npwm + 1):
        out[f"EPWM{n}"] = epwm(sp, fam, n)
    necap = 1 if fam == "2802x" else 3
    for n in range(1, necap + 1):
        cp("ECAP1", f"ECAP{n}", base=0x6A00 + 0x20 * (n - 1), desc=f"eCAP{n} (SPRUI09 ch. 5 / SPRUH18 ch. 6)",
           irqs=_irqs_named(fam, [f"ECAP{n}_INT"]))
    if fam == "2806x":
        for n in (1, 2):
            out[f"HRCAP{n}"] = hrcap(n)
        for n in (1, 2):
            cp(f"EQEP{n}", desc=f"eQEP{n} (SPRUH18 ch. 7, SPRS698 Table 8-37)",
               irqs=_irqs_named(fam, [f"EQEP{n}_INT"]))
        for n in (3, 4):
            out[f"HRCAP{n}"] = hrcap(n)
    out["GPIO"] = gpio(fam)
    out["SYSCTRL"] = sysctrl(fam)
    cp("SPIA", desc="SPI-A (4-level FIFOs; SPRUI09 Table 8-7 / SPRUH18 Table 12-7)",
       irqs=_irqs_named(fam, ["SPIRXINTA", "SPITXINTA"]))
    cp("SCIA", desc="SCI-A (4-level FIFOs; SPRUI09 Table 9-6 / SPRUH18 Table 13-6)",
       irqs=_irqs_named(fam, ["SCIRXINTA", "SCITXINTA"]))
    out["NMIINT"] = nmiint(fam)
    out["XINT"] = xint(fam)
    out["ADC"] = adc(fam)
    if fam == "2806x":
        cp("SPIA", "SPIB", base=0x7740, desc="SPI-B (same layout as SPI-A, SPRS698 Table 8-26)",
           irqs=_irqs_named(fam, ["SPIRXINTB", "SPITXINTB"]))
        cp("SCIB", desc="SCI-B (SPRS698 Table 8-28)", irqs=_irqs_named(fam, ["SCIRXINTB", "SCITXINTB"]))
    i2c = cp("I2CA", desc="I2C-A (4-level FIFOs; SPRUI09 Table 10-8 lists I2CEMDR at 0x0B, the data sheet table "
             "does not)", irqs=_irqs_named(fam, ["I2CINT1A", "I2CINT2A"]))
    i2c["size"] = 0x40
    doc = {"device": DEVICE[fam], "svd_version": "hand-written", "address_unit_bits": 16,
           "cpu": {"name": "C28x", "fpu": fam == "2806x"}, "interrupts": interrupts(fam),
           "peripherals": dict(sorted(out.items(), key=lambda kv: kv[1]["base"]))}
    return doc


HEADER = """\
# {device} (Piccolo) register table, GENERATED by tools/gen_piccolo_regs.py -- do not edit by hand; change the
# generator and rerun it.  TI publishes no SVD for the Piccolo families.  Blocks with the same IP as the F2833x are
# copied from chips/tms320f28232_regs.yaml; the Piccolo blocks (system control with INTOSC1/2 and PLL lock period, NMI
# watchdog, type-3 ADC, comparators, AIO, type-1 ePWM with digital compare and HRPWM extensions{extra}) are
# transcribed from {trm} and checked against the data sheet register tables ({ds}):
# `.venv/bin/python -I tools/gen_piccolo_regs.py --check-ds {fam} <pdftotext -layout of the data sheet>`.
# Address units are 16-bit words: offsets/sizes/widths count words; a width-2 register is 32 bits, low word first.
"""


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"0x{v:X}" if v > 9 else str(v)
    if v is None:
        return "null"
    s = str(v)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", s) and s.lower() not in ("yes", "no", "on", "off", "null", "true",
                                                                              "false"):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _flow(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if k == "fields":
            continue
        if isinstance(v, dict):
            parts.append(f"{k}: {{" + ", ".join(f"{_fmt(a)}: {_fmt(b)}" for a, b in v.items()) + "}")
        elif k in ("reset", "reset_mask") and isinstance(v, int):
            parts.append(f"{k}: 0x{v:0{8 if d.get('width') == 2 else 4}X}")
        elif k == "offset":
            parts.append(f"{k}: 0x{v:02X}")
        else:
            parts.append(f"{k}: {_fmt(v)}")
    return ", ".join(parts)


def dump(doc: dict, fam: str) -> str:
    extra = "; CLA, DMA, USB0, HRCAP, McBSP, eCAN on the 2806x" if fam == "2806x" else ""
    trm = "SPRUH18I (TMS320x2806x TRM)" if fam == "2806x" else "SPRUI09A (TMS320F2802x TRM)"
    ds = "SPRS698J chapter 8" if fam == "2806x" else "SPRS523P chapter 9"
    out = [HEADER.format(device=doc["device"], extra=extra, trm=trm, ds=ds, fam=fam).rstrip("\n")]
    out.append(f"device: {doc['device']}")
    out.append(f'svd_version: "{doc["svd_version"]}"')
    out.append(f"address_unit_bits: {doc['address_unit_bits']}")
    out.append(f"cpu: {{name: C28x, fpu: {_fmt(doc['cpu']['fpu'])}}}")
    out.append("interrupts:                 # PIE vector numbers (INTx.y = 32 + 8(x-1) + (y-1)); 13/14 INT13/INT14")
    for n, nm in doc["interrupts"].items():
        out.append(f"  {n}: {nm}")
    out.append("peripherals:")
    for name, p in doc["peripherals"].items():
        out.append(f"  {name}:")
        out.append(f"    base: 0x{p['base']:04X}")
        out.append(f"    size: 0x{p['size']:03X}")
        out.append("    derived_from: null")
        out.append(f"    description: {_fmt(p.get('description', ''))}")
        out.append(f"    group: {_fmt(p.get('group') or name)}")
        if p.get("interrupts"):
            out.append("    interrupts: {" + ", ".join(f"{k}: {v}" for k, v in p["interrupts"].items()) + "}")
        out.append("    registers:")
        for r in p["registers"]:
            fl = r.get("fields")
            if fl:
                out.append(f"      - {{{_flow(r)}, fields: [")
                for f in fl:
                    out.append(f"          {{{_flow(f)}}},")
                out.append("        ]}")
            else:
                out.append(f"      - {{{_flow(r)}}}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------------- data sheet check
DS_TABLES = {   # register tables of the data sheets (table numbers)
    "2802x": ("9", ["9-11", "9-12", "9-19", "9-20", "9-21", "9-22", "9-23", "9-24", "9-25", "9-26", "9-27", "9-28",
                    "9-29"], "9-18"),
    "2806x": ("8", ["8-10", "8-11", "8-18", "8-19", "8-20", "8-22", "8-23", "8-24", "8-25", "8-26", "8-27", "8-28",
                    "8-29", "8-31", "8-32", "8-33", "8-34", "8-35", "8-36", "8-37", "8-38"], "8-17"),
}
# known data sheet typos / truncations (data sheet text -> table name or address fix), each one printed as a NOTE
DS_NAME_FIX = {"DCCAPCT": "DCCAPCTL"}
DS_ADDR_FIX = {("TZDCSEL", 0x98D3): 0x68D3}       # SPRS523P Table 9-27 prints ePWM4 TZDCSEL at 0x98D3


def parse_ds(text: str, fam: str) -> tuple[list[tuple[str, int]], dict[int, str]]:
    """[(register name, word address)] from the data sheet register tables, and {vector: name} from the PIE table.
    Ranges are expanded: "ADCSOC0CTL to" + "ADCSOC15CTL 0x712F" (first address on the "to" line) and
    "ADCRESULT0 to ADCRESULT15 0x0B00 to 0x0B0F" (one line in SPRS523; in SPRS698 the first address is on the line
    above)."""
    chap, tables, pie_tbl = DS_TABLES[fam]
    text = re.sub(r"0x00[ \u2212-]([0-9A-F]{4})\b", r"0x\1", text)
    text = re.sub(r"\b0x([0-9A-F]{3})\b", r"0x0\1", text)                   # 0xB00 -> 0x0B00
    lines = text.splitlines()
    regs: list[tuple[str, int]] = []
    pie: dict[int, str] = {}
    cur = None
    skip: set[tuple[str, int]] = set()
    for i, ln in enumerate(lines):
        m = re.search(rf"Table ({chap}-\d+)\.", ln)
        if m:
            cur = m.group(1)
            continue
        if cur == pie_tbl:
            pm = re.match(r"^\s*INT(\d+)\.y\s+(.*)$", ln)
            if pm:
                x = int(pm.group(1))
                for k, nm in enumerate(pm.group(2).split()[:8]):     # columns INTx.8 .. INTx.1
                    if nm not in ("Reserved", "\u2013", "-"):
                        pie[32 + 8 * (x - 1) + (7 - k)] = nm
            continue
        if cur not in tables:
            continue
        rm = re.match(r"^\s*([A-Z]+?)(\d+)([A-Z]*)\s+to\b(.*)$", ln)
        if rm:
            rest = rm.group(4) + " " + " ".join(lines[i + 1:i + 3])
            m2 = re.search(rf"\b{rm.group(1)}(\d+){rm.group(3)}\b", rest)
            addrs = (re.findall(r"0x[0-9A-F]{4}", rm.group(4)) or re.findall(r"0x[0-9A-F]{4}", lines[i - 1])
                     or re.findall(r"0x[0-9A-F]{4}", rest))
            if m2 and addrs:
                n0, n1 = int(rm.group(2)), int(m2.group(1))
                a0 = int(addrs[0], 16)
                for k in range(n0, n1 + 1):
                    regs.append((f"{rm.group(1)}{k}{rm.group(3)}", a0 + k - n0))
                skip.add((f"{rm.group(1)}{n1}{rm.group(3)}", a0 + n1 - n0))
            continue
        m = re.match(r"^\s*([A-Z][A-Z0-9_]+)(?:\(\d\))*\s+((?:0x[0-9A-F]{4}(?:\(\d\))?\s+)+)", ln + " ")
        if not m:
            continue
        name = m.group(1)
        if name.startswith("TMS320") or name in ("NAME", "Reserved", "PARTID"):
            continue
        for a in re.findall(r"0x[0-9A-F]{4}", m.group(2)):
            if (name, int(a, 16)) not in skip:
                regs.append((name, int(a, 16)))
    return regs, pie


def check(doc: dict, text: str, fam: str) -> int:
    ds, pie = parse_ds(text, fam)
    by_addr = {}
    for blk, p in doc["peripherals"].items():
        for r in p["registers"]:
            by_addr[p["base"] + r["offset"]] = (blk, r["name"])
    bad = 0
    seen = set()
    for name, a in ds:
        if (name, a) in DS_ADDR_FIX:
            print(f"NOTE data sheet typo: {name} @ 0x{a:04X} (is 0x{DS_ADDR_FIX[(name, a)]:04X})")
            a = DS_ADDR_FIX[(name, a)]
        hit = by_addr.get(a)
        want = DS_NAME_FIX.get(name, name)
        if hit is None:
            print(f"MISSING in table: {name} @ 0x{a:04X}")
            bad += 1
            continue
        blk, tname = hit
        ok = tname == want or (blk in ("SCIA", "SCIB") and want == tname + blk[-1])
        if not ok:
            print(f"MISMATCH @ 0x{a:04X}: data sheet {name}, table {blk}.{tname}")
            bad += 1
            continue
        if want != name:
            print(f"NOTE data sheet spelling {name} = {blk}.{tname} @ 0x{a:04X}")
        seen.add(a)
    covered = {by_addr[a][0] for a in seen}
    extra = [(a, *by_addr[a]) for a in sorted(by_addr) if by_addr[a][0] in covered and a not in seen]
    mbox = [e for e in extra if re.match(r"(MBOX\d+_|LAM\d|MOTS\d|MOTO\d)", e[2])]
    for a, blk, nm in extra:
        if (a, blk, nm) not in mbox:
            print(f"NOTE {blk}.{nm} @ 0x{a:04X}: not in the data sheet tables (from the TRM)")
    if mbox:
        print(f"NOTE {len(mbox)} eCAN mailbox / LAM / MOTS / MOTO registers are not in the data sheet tables")
    # PIE vector table
    ints = doc["interrupts"]
    for v, nm in sorted(pie.items()):
        mine = ints.get(v)
        if mine is None or (mine != nm and mine != f"{nm}_10"):
            print(f"MISMATCH PIE vector {v} (INT{(v - 32) // 8 + 1}.{(v - 32) % 8 + 1}): data sheet {nm}, table {mine}")
            bad += 1
    for v, nm in ints.items():
        if v >= 32 and v not in pie:
            print(f"MISMATCH PIE vector {v}: table {nm}, reserved in the data sheet")
            bad += 1
    print(f"{len(ds)} data sheet register addresses ({len(seen)} distinct) and {len(pie)} PIE vectors compared, "
          f"{bad} mismatch(es)")
    return bad


# --------------------------------------------------------------------------------------------- boot ROM words
def bootrom_words(path: Path) -> dict[int, list[int]]:
    sys.path.insert(0, str(ROOT))
    from chipem.core.c28x.asm import assemble
    res = assemble(path.read_text(), filename=str(path))
    return {a: [int.from_bytes(d[i:i + 2], "little") for i in range(0, len(d), 2)] for a, d in res.segments}


def bootrom_yaml(path: Path) -> str:
    out = []
    for a, words in bootrom_words(path).items():
        out.append(f"    0x{a:06X}: [")
        for i in range(0, len(words), 12):
            out.append("      " + ", ".join(f"0x{w:04X}" for w in words[i:i + 12]) + ",")
        out.append("    ]")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--family", choices=("2802x", "2806x"), help="generate only this table")
    ap.add_argument("--check-ds", nargs=2, metavar=("FAMILY", "TXT"),
                    help="compare with the pdftotext -layout output of the family's data sheet")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--bootrom-words", metavar="ASM", help="print the `boot.rom_words` YAML of a boot ROM asm file")
    a = ap.parse_args(argv)
    if a.bootrom_words:
        print(bootrom_yaml(Path(a.bootrom_words)))
        return 0
    src = yaml.safe_load(SRC.read_text())
    if a.check_ds:
        fam, txt = a.check_ds
        if fam not in DST:
            ap.error("family must be 2802x or 2806x")
        doc = build(src, fam)
        return 1 if check(doc, Path(txt).read_text(errors="replace"), fam) else 0
    for fam in ([a.family] if a.family else ["2802x", "2806x"]):
        doc = build(src, fam)
        if not a.no_write:
            DST[fam].write_text(dump(doc, fam))
            print(f"wrote {DST[fam]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
