#!/usr/bin/env python3
"""new_chip: scaffold a CHIP_EM chip description (+ register table + test skeleton) from an SVD.

Usage:
    python -I tools/new_chip.py --part STM32F103C8 --package LQFP48 --family stm32 --arch armv7-m --cpu cortex-m3 \
        --svd path/or/https-url.svd [--pins pins.csv] [--mem FLASH:flash:0x08000000:0x10000 ...] [--force]
    python -I tools/new_chip.py --part TMS320F28234 --package LQFP176 --family ti_c2000 \
        --regs chips/tms320f28234_regs.yaml          # no SVD (TI): use a hand-written register table instead

Writes (relative to --root, default: this checkout):
    chips/<part>_regs.yaml       register table, generated with tools/svd2chip.py --flat (only with --svd)
    chips/<part>.yaml            chip YAML skeleton: core, memory, peripherals (models mapped per family, see
                                 FAMILIES below), IRQ table from the SVD, gpio_name_format, pins from --pins or a TODO
    tests/test_<part>_chip.py    test skeleton with the standard assertions (the spot-check test fails until written)
Existing files are never overwritten without --force.  Every guess is marked `TODO` in the output: grep for it.

Model mapping: a peripheral gets a family model when its SVD name matches the rule's regex AND the SVD peripheral
has the registers the model is written against (`needs`).  Otherwise it becomes `stub` (with a TODO comment when
the name matched but the registers did not: a different register layout needs a new model, e.g. STM32F1 GPIO has
CRL/CRH instead of MODER).  Blocks in the Cortex-M private peripheral bus (0xE0000000-0xE00FFFFF) are skipped
unless a rule maps them (the arch layer owns the SCS; the rest is auto-stubbed), and so are blocks that lie inside a
declared memory region (e.g. the Kinetis flash configuration field).

pins.csv columns (header row required): num,name,kind,port,bit,functions[,default]
    functions = "KEY=VALUE;KEY=VALUE" (e.g. "AF7=USART1_TX;AF8=UART4_TX;ADC=ADC123_IN0"); port/bit empty for
    non-GPIO pins; kind one of gpio|analog|power|special.

Downloaded SVDs are untrusted: they are stored under svd/dl-<part>/ and parsed with xml.etree only (svd2chip).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for p in (str(ROOT), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import svd2chip  # noqa: E402
import yaml  # noqa: E402


# ------------------------------------------------------------------------------------------------ family tables
@dataclass
class Rule:
    pattern: str                    # full-match regex on the SVD peripheral name
    model: str
    needs: tuple[str, ...] = ()     # registers the model relies on; missing -> stub + TODO
    params: dict | None = None      # static params; "{1}" in a string value = regex group 1, "#1" = int(group 1)
    rename: str | None = None       # chip-YAML name if it differs from the SVD name
    note: str | None = None         # comment written next to the entry
    size: int | None = None         # block size if the model spans more than the SVD extent
    absorb: tuple[str, ...] = ()    # SVD peripherals inside that span (no entries of their own)

    def match(self, name: str):
        return re.fullmatch(self.pattern, name)


def _p(**kw):
    return kw


FAMILIES: dict[str, dict] = {
    "stm32": {
        "doc": "stm32.* models implement the STM32F4 register layout (RM0090): GPIO MODER/AFR, USART SR/DR/BRR, RCC "
               "PLLCFGR/AHB1ENR.  Other series (F0/F1/F3/F7/L4/G0/H7) differ: check every mapped block.",
        "gpio_name_format": "P{port}{bit}",
        "granule": 0x400,
        "arch": "armv7e-m", "cpu": "cortex-m4", "clock_hz": 16_000_000,
        "clock_note": "reset clock = HSI (16 MHz on F4, 8 MHz on F1/F0): TODO check the RM's reset clock",
        "memory": [("FLASH", "flash", 0x08000000, 0x100000), ("SRAM", "ram", 0x20000000, 0x20000)],
        "extra": {"vtor": 0x08000000, "systick_ext_div": 8},
        "rules": [
            Rule(r"GPIO([A-K])", "stm32.gpio", ("MODER", "AFRL", "IDR", "ODR", "BSRR"), _p(port="{1}")),
            Rule(r"USART(\d+)", "stm32.usart", ("SR", "DR", "BRR", "CR1"), _p(index="#1", kind="usart")),
            Rule(r"UART(\d+)", "stm32.usart", ("SR", "DR", "BRR", "CR1"), _p(index="#1", kind="uart")),
            Rule(r"RCC", "stm32.rcc", ("CR", "PLLCFGR", "CFGR", "AHB1ENR"), _p(ext_hz=8_000_000),
                 note="TODO ext_hz = the board's HSE crystal"),
            Rule(r"TIM([1-9]|1[0-4])", "stm32.tim", ("CR1", "PSC", "ARR", "SR", "DIER")),
            Rule(r"IWDG", "stm32.iwdg", ("KR", "PR", "RLR", "SR")),
            Rule(r"WWDG", "stm32.wwdg", ("CR", "CFR", "SR")),
            Rule(r"EXTI", "stm32.exti", ("IMR", "RTSR", "FTSR", "PR")),
            Rule(r"SYSCFG", "stm32.syscfg", ("EXTICR1",)),
            Rule(r"FLASH", "stm32.flash", ("ACR", "KEYR", "SR", "CR"), note="flash interface registers"),
            Rule(r"PWR", "stm32.pwr", ("CR", "CSR")),
            Rule(r"DMA([12])", "stm32.dma", ("LISR", "HISR", "LIFCR", "HIFCR", "S0CR", "S0NDTR", "S0PAR", "S0M0AR",
                                             "S0FCR"), _p(controller="#1"),
                 note="TODO check the request tables (RM0090 Tables 42/43 are hard-coded in stm32.dma)"),
            Rule(r"ADC([1-3])", "stm32.adc", ("SR", "CR1", "CR2", "SMPR1", "SMPR2", "SQR1", "SQR3", "DR"),
                 note="F2/F4 ADC layout (RM0090 ch. 13; F1/F3/L4 ADCs differ); EXTSEL table 51 is hard-coded"),
            Rule(r"(?i)C_ADC|ADC_?(123_)?COMMON", "stm32.adc_common", ("CSR", "CCR"), note="ADC common registers"),
            Rule(r"I2C([1-3])", "stm32.i2c", ("CR1", "CR2", "OAR1", "DR", "SR1", "SR2", "CCR", "TRISE"),
                 note="F1/F2/F4 I2C v1 layout (RM0090 ch. 27; F0/F3/F7/L4 have the TIMINGR I2C v2)"),
            Rule(r"CAN([12])", "stm32.can", ("MCR", "MSR", "TSR", "RF0R", "IER", "ESR", "BTR", "TI0R", "RI0R"),
                 _p(index="#1"), note="bxCAN (RM0090 ch. 32); CAN1 owns the filter banks (params.filters / "
                                      "filter_banks: TODO check the bank count, 14 on single-CAN parts); FDCAN differs"),
            Rule(r"SPI([1-6])", "stm32.spi", ("CR1", "CR2", "SR", "DR", "CRCPR", "I2SCFGR"),
                 note="SPI and I2S mode (RM0090 ch. 28; I2S clock from PLLI2S / I2S_CKIN); SPI1/4/5/6 on APB2, "
                      "others APB1"),
            Rule(r"I2S([23])ext", "stm32.i2s_ext", ("CR1", "CR2", "SR", "DR", "I2SCFGR", "I2SPR"), _p(main="SPI{1}"),
                 note="I2S full-duplex extension of SPI2/SPI3 (RM0090 28.4.4)"),
            Rule(r"Ethernet_MAC", "stm32.eth", ("MACCR", "MACFFR", "MACMIIAR", "MACMIIDR", "MACA0HR"), size=0x1400,
                 absorb=("Ethernet_MMC", "Ethernet_PTP", "Ethernet_DMA"),
                 note="Ethernet MAC + MMC + PTP + DMA in one block (RM0090 ch. 33); TODO parts without Ethernet "
                      "sharing the die: params {only: [part prefixes]}"),
            Rule(r"DCMI", "stm32.dcmi", ("CR", "SR", "RIS", "IER", "MIS", "ICR", "ESCR", "ESUR", "CWSTRT", "CWSIZE",
                                         "DR"),
                 note="camera interface (RM0090 ch. 15); DMA request \"DCMI\" (DMA2 S1/S7 ch1); TODO params.only on "
                      "shared-die YAMLs of parts without a camera interface"),
            Rule(r"CRYP", "stm32.cryp", ("CR", "SR", "DIN", "DOUT", "DMACR", "IMSCR", "RISR", "MISR"),
                 note="AES ECB/CBC/CTR, DES/TDES ECB/CBC (RM0090 ch. 23, no GCM/CCM); F415/F417/F43x only: "
                      "TODO params.only on shared-die YAMLs"),
            Rule(r"HASH", "stm32.hash", ("CR", "DIN", "STR", "HR0", "IMR", "SR"),
                 note="SHA-1/MD5/HMAC (RM0090 ch. 25; params.sha2: true for F43x SHA-224/256); F415/F417/F43x only: "
                      "TODO params.only on shared-die YAMLs"),
            Rule(r"CRC", "stm32.crc", ("DR", "IDR", "CR"), note="fixed CRC-32 polynomial (F4; F0/F3/F7/L4 have POL/INIT)"),
            Rule(r"SDIO", "stm32.sdio", ("POWER", "CLKCR", "ARG", "CMD", "RESPCMD", "RESP1", "DTIMER", "DLEN", "DCTRL",
                                         "STA", "ICR", "MASK", "FIFO"),
                 note="SDIO host (RM0090 ch. 31; the F7/H7 SDMMC differs); SDIOCLK = PLL48CLK, DMA request \"SDIO\""),
            Rule(r"OTG_FS_GLOBAL", "stm32.otg_fs", ("FS_GOTGCTL", "FS_GUSBCFG", "FS_GRSTCTL", "FS_GINTSTS",
                                                    "FS_GINTMSK", "FS_GRXFSIZ", "FS_GCCFG"),
                 size=0x40000, absorb=("OTG_FS_HOST", "OTG_FS_DEVICE", "OTG_FS_PWRCLK"), rename="OTG_FS",
                 note="USB OTG FS core, device mode (RM0090 ch. 34, DWC2 2.81a); TODO params endpoints (F4: 4) / "
                      "fifo_words (1.25 KB = 320); PHY clock = PLL48CLK"),
            Rule(r"OTG_HS_GLOBAL", "stm32.otg_hs", ("OTG_HS_GOTGCTL", "OTG_HS_GUSBCFG", "OTG_HS_GRSTCTL",
                                                    "OTG_HS_GINTSTS", "OTG_HS_GINTMSK", "OTG_HS_GRXFSIZ"),
                 size=0x40000, absorb=("OTG_HS_HOST", "OTG_HS_DEVICE", "OTG_HS_PWRCLK"), rename="OTG_HS",
                 note="USB OTG HS core on its embedded FS PHY (RM0090 ch. 35; ULPI high speed not modelled); TODO "
                      "params endpoints (F4: 6) / fifo_words (4 KB = 1024)"),
            Rule(r"FSMC", "stm32.fsmc", ("BCR1", "BTR1", "BWTR1"),
                 note="NOR/SRAM bank 1 windows at 0x60000000 (RM0090 ch. 36; F42x/F7 FMC adds SDRAM: TODO)"),
            Rule(r"RNG", "stm32.rng", ("CR", "SR", "DR"), note="clock = PLL48CLK (main PLL Q output)"),
            Rule(r"DAC\d?", "stm32.dac", ("CR", "SWTRIGR", "DHR12R1", "DHR12R2", "DHR12RD", "DOR1", "DOR2", "SR"),
                 note="TODO check the TSEL trigger table (RM0090 Table 60 is hard-coded) and the DAC_OUTx pins"),
            Rule(r"RTC", "stm32.rtc", ("TR", "DR", "CR", "ISR", "PRER", "WUTR", "ALRMAR", "WPR", "SSR", "BKP0R"),
                 note="F2/F4 RTC (BCD calendar, RM0090 ch. 26; the F1 RTC is a different counter block)"),
            Rule(r"DBG(MCU)?", "stm32.dbgmcu", ("DBGMCU_IDCODE",),
                 note="TODO DBGMCU_IDCODE: check DEV_ID for this part (stm32.dbgmcu returns 0x413)"),
        ],
    },
    "kinetis": {
        "doc": "kinetis.* models implement the K22F (K22P100M120SF5RM) register layout; KL/KE/KV parts differ.",
        "gpio_name_format": "PT{port}{bit}",
        "granule": 0x1000,
        "arch": "armv7e-m", "cpu": "cortex-m4", "clock_hz": 20_971_520,
        "clock_note": "reset clock: MCG FEI = 32768 * 640; kinetis.mcg recomputes it at reset",
        "memory": [("FLASH", "flash", 0x00000000, 0x100000), ("SRAM_L", "ram", 0x1FFF0000, 0x10000),
                   ("SRAM_U", "ram", 0x20000000, 0x10000)],
        "extra": {"lockup_reset": True},      # RCM LOCKUP reset source (K22 RM 6.2.2.9)
        "rules": [
            Rule(r"PORT([A-E])", "kinetis.port", ("PCR0", "ISFR"), _p(port="{1}")),
            Rule(r"(?:GPIO|PT)([A-E])", "kinetis.gpio", ("PDOR", "PDDR", "PDIR"), _p(port="{1}")),
            Rule(r"UART(\d+)", "kinetis.uart", ("BDH", "BDL", "C2", "S1", "D"), _p(index="#1")),
            Rule(r"SIM", "kinetis.sim", ("SCGC5", "CLKDIV1")),
            Rule(r"MCG", "kinetis.mcg", ("C1", "C2", "C4", "C5", "C6", "S")),
            Rule(r"OSC0?", "kinetis.osc", ("CR",)),
            Rule(r"WDOG", "kinetis.wdog", ("STCTRLH", "REFRESH", "UNLOCK")),
            Rule(r"PIT", "kinetis.pit", ("MCR", "LDVAL0", "TCTRL0", "TFLG0")),
            Rule(r"FTM\d+", "kinetis.ftm", ("SC", "CNT", "MOD", "C0SC", "C0V", "CNTIN", "COMBINE")),
            Rule(r"LPTMR0", "kinetis.lptmr", ("CSR", "PSR", "CMR", "CNR")),
            Rule(r"RTC", "kinetis.rtc", ("TSR", "TPR", "SR", "CR")),
            Rule(r"FTF[ELA]", "kinetis.ftfe", ("FSTAT", "FCCOB0")),
            Rule(r"ADC(\d+)", "kinetis.adc", ("SC1A", "SC3", "RA"), _p(index="#1")),
            Rule(r"SPI(\d+)", "kinetis.dspi", ("MCR", "SR", "PUSHR", "POPR"), _p(index="#1")),
            Rule(r"SDHC", "kinetis.sdhc", ("DSADDR", "BLKATTR", "CMDARG", "XFERTYP", "CMDRSP0", "PRSSTAT", "PROCTL",
                                           "SYSCTL", "IRQSTAT", "IRQSTATEN", "IRQSIGEN", "WML", "ADSADDR"),
                 note="SDHC (K22 RM ch. 50; uSDHC of i.MX RT/K8x differs); clock from SIM_SOPT2[SDHCSRC]"),
            Rule(r"I2C(\d+)", "kinetis.i2c", ("A1", "F", "C1", "S", "D", "C2"), _p(index="#1"),
                 note="TODO check the DMAMUX source (K22: I2C0 18, I2C1/I2C2 19; params.dma_source)"),
            Rule(r"DMA", "kinetis.dma", ("CR", "ERQ")),
            Rule(r"DMAMUX0?", "kinetis.dmamux", ("CHCFG0",)),
            Rule(r"PDB0", "kinetis.pdb", ("SC", "MOD", "CNT")),
            Rule(r"SMC", "kinetis.smc", ("PMPROT", "PMCTRL")),
            Rule(r"PMC", "kinetis.pmc", ("REGSC",)),
            Rule(r"RCM", "kinetis.rcm", ("SRS0",)),
            Rule(r"LLWU", "kinetis.llwu", ("F1",)),
            Rule(r"(?:RF)?VBAT", "kinetis.vbat", (), rename="VBAT", note="battery-backed register file (plain RAM)"),
            Rule(r"RFSYS", "ram", ()),
            Rule(r"CRC", "kinetis.crc", ("DATA", "GPOLY", "CTRL")),
            Rule(r"DAC(\d+)", "kinetis.dac", ("DAT0L", "DAT0H", "SR", "C0", "C1", "C2"), _p(index="#1")),
            Rule(r"CMP(\d+)", "kinetis.cmp", ("CR0", "CR1", "FPR", "SCR", "DACCR", "MUXCR"), _p(index="#1")),
            Rule(r"EWM", "kinetis.ewm", ("CTRL", "SERV", "CMPL", "CMPH")),
            Rule(r"VREF", "kinetis.vref", ("TRM", "SC")),
            Rule(r"CAN(\d+)", "kinetis.flexcan", ("MCR", "CTRL1", "ECR", "ESR1", "IMASK1", "IFLAG1", "CS0", "ID0"),
                 _p(index="#1"), note="FlexCAN; gate SIM_SCGC6 FLEXCAN<n> (TODO check mailbox count, default 16)"),
            Rule(r"USB0", "kinetis.usbfs", ("PERID", "ISTAT", "INTEN", "STAT", "CTL", "ADDR", "BDTPAGE1", "ENDPT0",
                                            "USBCTRL", "CONTROL", "USBTRC0"),
                 note="USB OTG FS device (BDT in RAM); 48 MHz from SIM_SOPT2 USBSRC + CLKDIV2 USBDIV/USBFRAC, gate "
                      "SIM_SCGC4 USBOTG"),
            Rule(r"USBDCD", "kinetis.usbdcd", ("CONTROL", "CLOCK", "STATUS", "TIMER0", "TIMER1"),
                 note="USB device charger detection (sequence timing from CLOCK / TIMERx)"),
            Rule(r"I2S(\d+)", "kinetis.sai", ("TCSR", "TCR2", "TCR4", "TCR5", "TDR0", "TFR0", "RCSR", "RDR0", "MCR",
                                             "MDR"),
                 note="SAI (K22 RM ch. 51); IRQs I2S0_Tx/I2S0_Rx, DMAMUX 13/12 (params.dma_tx/dma_rx), MCLK sources"),
            Rule(r"CMT", "kinetis.cmt", ("CGH1", "CGL1", "OC", "MSC", "CMD1", "CMD4", "PPS", "DMA"),
                 note="carrier modulator transmitter (K22 RM ch. 41); TODO check the DMAMUX source (K22: 47)"),
            Rule(r"MCM", "kinetis.mcm", ("PLASC", "PLAMC", "CR", "ISCR"),
                 note="K22 MCM (CR + ISCR FPU flags); other Kinetis MCMs have PLACR/CPO instead of CR"),
            Rule(r"FMC", "kinetis.fmc", ("PFAPR", "PFB0CR", "TAGVDW0S0")),
            Rule(r"AIPS(\d+)", "kinetis.aips", ("MPRA", "PACRA"), note="registers stored, protection not enforced"),
            Rule(r"AXBS", "kinetis.axbs", ("PRS0", "CRS0"), note="TODO check the masters present (PLAMC)"),
            Rule(r"MPU", "kinetis.mpu", ("CESR", "EAR0", "RGD0_WORD0", "RGD0_WORD3", "RGDAAC0"),
                 note="system MPU registers; access checks are not enforced"),
            Rule(r"FB", "kinetis.flexbus", ("CSAR0", "CSMR0", "CSCR0"),
                 note="FlexBus chip selects; the external windows are the FB_SPACE alias"),
        ],
        # blocks the SVD does not list: (SVD block, its base, alias name, model, alias base, size, params, note).
        # K22 FTM2 is also decoded at 0x4003A000 (K22P100M120SF5RM ch. 4 memory map; FTM2 slot on AIPS0)
        "aliases": [
            ("FTM2", 0x400B8000, "FTM2_ALIAS", "kinetis.ftm_alias", 0x4003A000, 0x1000, {"target": "FTM2"},
             "second address window of FTM2 (not in the SVD)"),
            # K22 DAC0 is also decoded at 0x4003F000 through AIPS0 (K22P100M120SF5V2RM 3.7.3.4)
            ("DAC0", 0x400CC000, "DAC0_ALIAS", "kinetis.ftm_alias", 0x4003F000, 0x1000, {"target": "DAC0"},
             "second address window of DAC0 on AIPS0 (not in the SVD)"),
            # K22 FlexBus external windows 0x60000000-0xDFFFFFFF (K22P100M120SF5RM Table 4-1), decoded by FB
            ("FB", 0x4000C000, "FB_SPACE", "kinetis.flexbus_space", 0x60000000, 0x80000000, {},
             "FlexBus external memory / peripheral windows (decoded by the FB chip selects)"),
        ],
    },
    "ti_c2000": {
        "doc": "ti_c2000.* models implement the F2833x/F2823x layout (SPRUFB0); 16-bit address units.",
        "gpio_name_format": "GPIO{num}",
        "granule": None,
        "arch": "c28x", "cpu": "c28x", "clock_hz": 100_000_000, "backend": "c28x",
        "clock_note": "nominal; ti_c2000.sysctrl derives SYSCLKOUT from ext_hz/PLLCR/DIVSEL",
        "memory": [("M0", "ram", 0x000000, 0x0400), ("M1", "ram", 0x000400, 0x0400)],
        "extra": {"boot": {"entry": 0x33FFF6, "sp": 0x0400}},
        "rules": [
            Rule(r"GPIO", "ti_c2000.gpio", ("GPAMUX1", "GPADIR")),
            Rule(r"SCI([A-C])", "ti_c2000.sci", ("SCICCR", "SCICTL1", "SCITXBUF", "SCIRXBUF")),
            Rule(r"CPUTIMER(\d)", "ti_c2000.cputimer", ("TIM", "PRD", "TCR"), _p(index="#1")),
            Rule(r"SYSCTRL", "ti_c2000.sysctrl", ("PLLSTS", "PLLCR", "WDCR"), _p(ext_hz=20_000_000, boot_divsel=2),
                 note="TODO ext_hz = the board's crystal/oscillator"),
            Rule(r"ADC", "ti_c2000.adc", ("ADCTRL1", "ADCTRL2")),
            Rule(r"ADCMIRROR", "ti_c2000.adcmirror", ("ADCRESULT0",)),
            Rule(r"XINT", "ti_c2000.xint", ("XINT1CR",)),
            Rule(r"SPI([A-D])", "ti_c2000.spi", ("SPICCR", "SPITXBUF")),
            Rule(r"FLASH", "ti_c2000.flash", ("FOPT",), _p(regs="FLASH"), rename="FLASHREGS"),
            Rule(r"CSM", "ti_c2000.csm", ("CSMSCR",)),
            Rule(r"PIE", "ti_c2000.pie", ("PIECTRL",), note="arch-owned (chipem/arch/c28x), incl. vectors"),
            Rule(r"EPWM(\d)", "ti_c2000.epwm", ("TBCTL", "TBPRD", "CMPA", "AQCTLA", "DBCTL", "ETSEL"),
                 _p(gate="{0}ENCLK", index="#1")),
            Rule(r"ECAP(\d)", "ti_c2000.ecap", ("TSCTR", "CTRPHS", "CAP1", "ECCTL1", "ECCTL2", "ECEINT", "ECFLG"),
                 _p(gate="{0}ENCLK", index="#1")),
            Rule(r"EQEP(\d)", "ti_c2000.eqep", ("QPOSCNT", "QPOSMAX", "QUPRD", "QDECCTL", "QEPCTL", "QCAPCTL",
                                               "QFLG"), _p(gate="{0}ENCLK", index="#1")),
            Rule(r"DMA", "ti_c2000.dma", ("DMACTRL", "PRIORITYCTRL1", "CH1_MODE", "CH1_CONTROL", "CH1_BURST_SIZE",
                                          "CH1_SRC_ADDR_SHADOW", "CH6_DST_ADDR_ACTIVE"), _p(gate="DMAENCLK"),
                 note="6 channels at 0x20 n; DINTCH1-6 = PIE 7.1-7.6"),
            Rule(r"I2CA", "ti_c2000.i2c", ("I2COAR", "I2CSTR", "I2CCLKL", "I2CCLKH", "I2CMDR", "I2CPSC"),
                 _p(gate="I2CAENCLK"), note="I2CINT1A/I2CINT2A = PIE 8.1/8.2; SDAA/SCLA on GPIO32/33"),
            Rule(r"ECAN([AB])", "ti_c2000.ecan", ("CANME", "CANMD", "CANTRS", "CANTA", "CANRMP", "CANMC", "CANBTC",
                                                 "CANES", "CANGIF0", "CANGIM", "MBOX0_MSGID"), _p(gate="{0}ENCLK"),
                 note="eCAN (SPRUI07 ch. 13); gate = PCLKCR0 bit"),
            Rule(r"MCBSP[AB]", "ti_c2000.mcbsp", ("DRR1", "DXR1", "SPCR1", "SPCR2", "RCR1", "XCR1", "SRGR1", "SRGR2",
                                                 "PCR", "MFFINT"), _p(gate="{0}ENCLK"),
                 note="McBSP (SPRUI07 ch. 12); MRINTx/MXINTx by name, DMA MXEVTx/MREVTx; gate = PCLKCR0 bit"),
            Rule(r"XINTF", "ti_c2000.xintf", ("XTIMING0", "XINTCNF2", "XBANK"),
                 note="XINTF registers (SPRUI07 ch. 14); add ti_c2000.xintf_zone blocks for zones 0/6/7 by hand"),
            Rule(r".*", "ti_c2000.storage", (), note="storage only"),
        ],
    },
}
GENERIC = {"doc": "new family: every block starts as `stub`", "gpio_name_format": "P{port}{bit}", "granule": 0x400,
           "arch": "armv7-m", "cpu": "cortex-m3", "clock_hz": 8_000_000, "clock_note": "TODO reset clock",
           "memory": [("FLASH", "flash", 0x00000000, 0x10000), ("SRAM", "ram", 0x20000000, 0x5000)],
           "extra": {}, "rules": []}
PPB = (0xE0000000, 0xE0100000)          # Cortex-M private peripheral bus


def family_table(family: str) -> dict:
    return FAMILIES.get(family, GENERIC)


@dataclass
class Periph:
    name: str
    model: str
    base: int
    size: int
    params: dict = field(default_factory=dict)
    note: str = ""


def map_model(family: str, name: str, regs: list[str]) -> tuple[str, dict, str, str]:
    """(model, params, chip-YAML name, note) for an SVD peripheral `name` with register names `regs`."""
    have = set(regs)
    for rule in family_table(family)["rules"]:
        m = rule.match(name)
        if not m:
            continue
        missing = [r for r in rule.needs if r not in have]
        if missing:
            return ("stub", {}, name, f"TODO {rule.model} needs {'/'.join(missing)} (not in the register table): "
                                      f"different layout, write a model or keep the stub")
        params = {}
        for k, v in (rule.params or {}).items():
            if isinstance(v, str) and v.startswith("#"):
                v = int(m.group(int(v[1:])))
            elif isinstance(v, str) and "{" in v:
                v = v.format(*([m.group(0)] + list(m.groups())))
            params[k] = v
        return rule.model, params, rule.rename or name, rule.note or ""
    return "stub", {}, name, ""


def _round_up(v: int, g: int) -> int:
    return (v + g - 1) // g * g


def plan_peripherals(family: str, table: dict, memory: list[tuple], exclude: set[str]) -> list[Periph]:
    """Chip-YAML peripheral entries from a regtable dict (svd2chip schema)."""
    fam = family_table(family)
    g = fam["granule"]
    arm = fam["arch"] != "c28x"
    items = sorted(((n, p) for n, p in (table.get("peripherals") or {}).items()), key=lambda kv: kv[1]["base"])
    spans = {n: r for n, _ in items for r in fam["rules"] if r.match(n) and (r.size or r.absorb)}
    absorbed = {a for r in spans.values() for a in r.absorb}
    items = [(n, p) for n, p in items if n not in absorbed]
    bases = [p["base"] for _, p in items]
    out: list[Periph] = []
    for i, (name, p) in enumerate(items):
        if name in exclude:
            continue
        base, extent = int(p["base"]), int(p.get("size") or 0x400)
        regs = [r["name"] for r in p.get("registers") or []]
        model, params, yname, note = map_model(family, name, regs)
        if arm and PPB[0] <= base < PPB[1] and model == "stub":
            continue                            # SCS (arch-owned) or auto-stubbed debug blocks
        if any(mb <= base < mb + ms for (_, _, mb, ms) in memory):
            continue                            # lives inside a memory region (e.g. flash config field)
        nxt = next((b for b in bases[i + 1:] if b > base), None)
        size = extent if not g else _round_up(base + extent, g) - base
        if name in spans and spans[name].size and model != "stub":
            size = spans[name].size
        if nxt is not None and base + size > nxt:
            size = max(nxt - base, 1)
            if size < extent:
                note = (note + "; " if note else "") + (f"TODO SVD extent {extent:#x} overlaps the next block at "
                                                       f"{nxt:#x}: check the RM memory map")
        out.append(Periph(yname, model, base, size, params, note))
    have = {(p.name, p.base, p.model) for p in out}
    for src, sbase, aname, amodel, abase, asize, aparams, anote in fam.get("aliases", ()):
        if any(n == src and b == sbase and m != "stub" for n, b, m in have) and aname not in exclude \
                and not any(p.base <= abase < p.base + p.size for p in out):
            out.append(Periph(aname, amodel, abase, asize, dict(aparams), anote))
    out.sort(key=lambda p: p.base)
    return out


# ------------------------------------------------------------------------------------------------ YAML text
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_./+\-]*\Z")
_RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}


def ys(v) -> str:
    """A YAML scalar for flow context."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in _RESERVED:
        return s
    return json.dumps(s)


def flow(d: dict, hexkeys: tuple[str, ...] = ()) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(v, dict):
            parts.append(f"{k}: {flow(v, hexkeys)}")
        elif isinstance(v, int) and not isinstance(v, bool) and k in hexkeys:
            parts.append(f"{k}: 0x{v:04X}" if v < 0x10000 else f"{k}: 0x{v:08X}")
        else:
            parts.append(f"{k}: {ys(v)}")
    return "{" + ", ".join(parts) + "}"


def read_pins_csv(path: Path, family: str) -> list[dict]:
    pins = []
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        need = {"num", "name", "kind", "port", "bit", "functions"}
        if not need <= set(rd.fieldnames or ()):
            raise SystemExit(f"new_chip: {path}: header must contain {', '.join(sorted(need))} "
                             f"(got {rd.fieldnames})")
        for i, row in enumerate(rd, 2):
            kind = (row["kind"] or "gpio").strip()
            if kind not in ("gpio", "analog", "power", "special"):
                raise SystemExit(f"new_chip: {path}:{i}: bad kind {kind!r}")
            pin = {"num": int(row["num"]), "name": row["name"].strip(), "kind": kind}
            port, bit = (row["port"] or "").strip(), (row["bit"] or "").strip()
            if port or bit:
                pin["port"], pin["bit"] = port.upper(), int(bit)
            funcs = {}
            for item in (row["functions"] or "").split(";"):
                if item.strip():
                    if "=" not in item:
                        raise SystemExit(f"new_chip: {path}:{i}: function {item!r} is not KEY=VALUE")
                    k, v = item.split("=", 1)
                    funcs[k.strip()] = v.strip()
            default = (row.get("default") or "").strip()
            if not default:
                default = ("GPIO" if family in ("stm32", "ti_c2000") else funcs.get("ALT0", "DISABLED")) \
                    if kind == "gpio" else pin["name"]
            pin["default"] = default
            if funcs:
                pin["functions"] = funcs
            pins.append(pin)
    return pins


def pin_line(p: dict) -> str:
    d = {k: p[k] for k in ("num", "name", "kind", "port", "bit", "default") if k in p}
    s = flow(d)
    if p.get("functions"):
        s = s[:-1] + ", functions: " + flow(p["functions"]) + "}"
    return f"  - {s}"


def chip_yaml(a, fam: dict, periphs: list[Periph], irqs: dict[int, str], unit_bits: int, fpu: bool,
              pins: list[dict] | None, regs_name: str | None, source: str, memory: list[tuple],
              mem_from_args: bool) -> str:
    part = a.part
    expected = re.search(r"(\d+)", a.package or "")
    arch = a.arch or fam["arch"]
    cpu = a.cpu or fam["cpu"]
    backend = "c28x" if arch == "c28x" else "unicorn"
    clock = float(a.clock_hz) if a.clock_hz else fam["clock_hz"]
    num_irqs = 0 if arch == "c28x" else (max(irqs) + 1 if irqs else 32)
    L = [f"# {part} ({a.package or 'TODO package'}): generated by tools/new_chip.py -- TODO review every line.",
         f"# Sources: {source}",
         "# TODO: data sheet (title, revision, pin table / AF table numbers), reference manual (memory map, IRQ table).",
         f"# Family note: {fam['doc']}",
         f"name: {ys(part)}",
         f"package: {ys(a.package or 'TODO')}",
         f"family: {ys(a.family)}"]
    if regs_name:
        L.append(f"regs_file: {regs_name}")
    L.append(f"gpio_name_format: {json.dumps(fam['gpio_name_format'])}")
    L.append(f"# clock_hz: {fam['clock_note']}")
    core = {"arch": arch, "backend": backend, "cpu": cpu, "fpu": fpu, "clock_hz": int(clock),
            "addr_unit_bits": unit_bits, "num_irqs": num_irqs}
    L.append(f"core: {flow(core)}")
    for k, v in fam["extra"].items():
        if isinstance(v, dict):
            L.append(f"{k}: {flow(v, hexkeys=('entry', 'sp'))}    # TODO verify")
        else:
            L.append(f"{k}: 0x{v:08X}    # TODO verify" if k == "vtor" else f"{k}: {ys(v)}    # TODO verify")
    if arch == "c28x":
        L.append("# auto_stub_ranges: [[0x0800, 0x0FFF], [0x5000, 0x7FFF]]   # TODO peripheral frames (word addresses)")
    L += ["", "memory:"]
    for (n, kind, base, size) in memory:
        tail = "" if mem_from_args else "    # TODO base/size from the data sheet memory map"
        L.append(f"  - {{name: {n}, base: 0x{base:08X}, size: {size:#x}, kind: {kind}}}{tail}")
    L += ["", "# armv7m.scs (0xE000E000, SysTick/NVIC/SCB) is created by the arch layer, not listed here." if arch != "c28x"
          else "# Addresses and sizes count 16-bit words.",
          "peripherals:" if periphs else "peripherals: []    # TODO from the data sheet memory map / register table"]
    for p in periphs:
        d = {"name": p.name, "model": p.model, "base": p.base, "size": p.size}
        if p.params:
            d["params"] = p.params
        s = "  - " + flow(d, hexkeys=("base", "size"))
        if p.note:
            s += f"   # {p.note}"
        L.append(s)
    L += ["", "# External interrupt numbers (exception = 16 + n) from the register table." if arch != "c28x"
          else "# PIE vector numbers (INTx.y = 32 + 8(x-1) + (y-1)).", "irqs:"]
    if irqs:
        gaps = [n for n in range(max(irqs) + 1) if n not in irqs] if arch != "c28x" else []
        if gaps:
            L.append(f"  # TODO the table defines no interrupt {gaps}: check the RM vector table (SVDs drop some)")
        for n, nm in sorted(irqs.items()):
            L.append(f"  {n}: {ys(nm)}")
    else:
        L[-1] = "irqs: {}    # TODO IRQ table from the reference manual (vector table)"
    L.append("")
    if pins:
        L.append(f"# {len(pins)} package pins (from {Path(a.pins).name}).  TODO: cross-check against the pinout figure.")
        L.append("pins:")
        L += [pin_line(p) for p in sorted(pins, key=lambda p: p["num"])]
    else:
        n = expected.group(1) if expected else "?"
        L.append(f"# TODO all {n} package pins (data sheet pin table; see docs/AI_NEW_CHIP_PLAYBOOK.md section B):")
        L.append("#   - {num: 1, name: PE2, kind: gpio, port: E, bit: 2, default: GPIO, functions: {AF0: TRACECLK}}")
        L.append("pins: []")
    return "\n".join(L) + "\n"


TEST_TEMPLATE = '''"""{part}: chip description checks.  Generated by tools/new_chip.py: replace every TODO; the spot-check test
fails until it holds real assertions from the data sheet."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

import chipem
from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "{stem}.yaml"
TOOLS = Path(chipem.__file__).resolve().parent.parent / "tools"
SVD = [p for p in ([ROOT / {svd_rel!r}] if {svd_rel!r} else []) if p.exists()]   # svd/dl-*/ (committed; skipped if absent)
EXPECTED_PINS = {expected}          # package pin count
EXPECTED_GPIO = {n_gpio}            # TODO: GPIO-capable pins per the data sheet


def _tool(name: str):
    sys.path.insert(0, str(TOOLS))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def test_core_and_package(desc):
    assert desc.name == {part!r} and desc.package == {package!r} and desc.family == {family!r}
    assert desc.core.arch == {arch!r} and desc.core.cpu == {cpu!r}
    assert desc.core.fpu is {fpu} and desc.core.addr_unit_bits == {unit_bits}
    assert desc.core.clock_hz == {clock_hz}         # reset clock, not the maximum
    assert desc.gpio_name_format == {fmt!r}


def test_pin_table_checker(desc):
    rep = _tool("pin_table_check").check_desc(desc, EXPECTED_PINS)
    assert rep.ok, rep.text()


def test_pin_count_numbers_and_gpio(desc):
    assert len(desc.pins) == EXPECTED_PINS
    assert sorted(p.num for p in desc.pins) == list(range(1, EXPECTED_PINS + 1))
    gpio = [p for p in desc.pins if p.kind == "gpio"]
    assert all(p.port is not None and p.bit is not None for p in gpio)
    assert len({{p.gpio_name for p in gpio}}) == len(gpio) == EXPECTED_GPIO


def test_pin_spot_checks(desc):
    # TODO: 8-15 assertions copied from the data sheet, e.g. (STM32F405ZG):
    #   assert desc.pin("PA9").num == 101 and desc.pin("PA9").functions["AF7"] == "USART1_TX"
    #   assert desc.pin(25).name == "NRST" and desc.pin(25).kind == "special"
    # Cover: the console UART TX/RX, an LED/button pin, a pin with a long multi-function name, the reset pin,
    # every power-pin kind, the first and the last pin, and any data sheet typo you corrected.
    pytest.fail("TODO: pin spot checks from the data sheet")


def test_memory_and_irqs(desc):
    for m in desc.memory:
        assert m.base % 0x400 == 0 and m.size % 0x400 == 0, m.name       # Unicorn 1 KB pages
    assert desc.irqs, "IRQ table is empty"
    # TODO: memory sizes and 3-5 IRQ numbers from the reference manual, e.g. desc.irqs[37] == "USART1"


@pytest.mark.skipif(not SVD, reason="SVD not downloaded")
def test_svd_check_zero_mismatches():
    out = io.StringIO()
    assert _tool("svd2chip").check(SVD[0], YAML, out=out) == 0, out.getvalue()


def test_chip_builds_without_stub_fallbacks(desc):
    chip = Chip(desc)
    bad = [e.fields.get("msg", "") for e in chip.log if e.kind == "warn" and "not available" in e.fields.get("msg", "")]
    assert not bad, bad
    fam = sys.modules.get(f"chipem.periph.{{desc.family}}")
    assert getattr(fam, "IMPORT_ERRORS", {{}}) == {{}}
'''


def fetch_svd(src: str, part: str, root: Path) -> Path:
    if not re.match(r"https://", src):
        p = Path(src)
        if not p.exists():
            raise SystemExit(f"new_chip: SVD {src} not found (give a path or an https:// URL)")
        return p
    dest_dir = root / "svd" / f"dl-{part.lower()}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.\-]", "_", src.rsplit("/", 1)[-1]) or "device.svd"
    dest = dest_dir / name
    if not dest.exists():
        with urllib.request.urlopen(src, timeout=60) as r:          # noqa: S310 - https only, see above
            data = r.read(64 << 20)
        dest.write_bytes(data)
    print(f"svd: {dest} (sha256 {hashlib.sha256(dest.read_bytes()).hexdigest()}) from {src}", file=sys.stderr)
    return dest


def parse_mem(spec: str) -> tuple:
    try:
        n, kind, base, size = spec.split(":")
        return (n, kind, int(base, 0), int(size, 0))
    except ValueError:
        raise SystemExit(f"new_chip: --mem {spec!r}: expected NAME:KIND:BASE:SIZE (e.g. FLASH:flash:0x08000000:0x10000)")


def write(path: Path, text: str, force: bool, written: list) -> None:
    if path.exists() and not force:
        raise SystemExit(f"new_chip: {path} exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    written.append(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--part", required=True, help="part number, e.g. STM32F103C8 (file stem = lower case)")
    ap.add_argument("--package", default="", help="package, e.g. LQFP48 (its number is the expected pin count)")
    ap.add_argument("--family", required=True, help=f"peripheral family: {', '.join(FAMILIES)} or a new name (all stubs)")
    ap.add_argument("--arch", default=None, help="armv6-m | armv7-m | armv7e-m | c28x (default: family's)")
    ap.add_argument("--cpu", default=None, help="cortex-m0 | cortex-m0plus | cortex-m3 | cortex-m4 | cortex-m7 | c28x")
    fp = ap.add_mutually_exclusive_group()
    fp.add_argument("--fpu", dest="fpu", action="store_true", default=None, help="core has an FPU")
    fp.add_argument("--no-fpu", dest="fpu", action="store_false", help="core has no FPU")
    ap.add_argument("--clock-hz", type=float, default=None, help="core clock after reset (not the maximum)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--svd", help="CMSIS-SVD file or https:// URL (cached under svd/dl-<part>/)")
    src.add_argument("--regs", help="existing register table YAML (svd2chip schema; e.g. hand-written for TI)")
    ap.add_argument("--source", default=None, help="provenance for the regs header (URL @ commit, SVD version)")
    ap.add_argument("--pins", help="pins.csv: num,name,kind,port,bit,functions[,default]")
    ap.add_argument("--mem", action="append", default=[], metavar="NAME:KIND:BASE:SIZE",
                    help="memory region (repeatable); default: the family's typical map, marked TODO")
    ap.add_argument("--exclude", default="", help="comma-separated SVD peripherals to leave out (not on this part)")
    ap.add_argument("--root", default=str(ROOT), help="output root (chips/, tests/ and svd/ below it)")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    a = ap.parse_args(argv)

    root = Path(a.root)
    stem = re.sub(r"[^a-z0-9_]", "_", a.part.lower())
    fam = family_table(a.family)
    if a.family not in FAMILIES:
        print(f"new_chip: family {a.family!r} is new: every peripheral is a stub; see the playbook section E",
              file=sys.stderr)
    written: list[Path] = []
    regs_name = None
    svd_path = None
    if a.svd:
        svd_path = fetch_svd(a.svd, a.part, root)
        table = svd2chip.convert(svd_path, flat=True)
        regs_name = f"{stem}_regs.yaml"
        source = a.source or f"{a.svd} (SVD version {table.get('version')})"
        write(root / "chips" / regs_name, svd2chip.emit_yaml(table, source), a.force, written)
        unit_bits = int(table.get("address_unit_bits") or 8)
        svd_fpu = (table.get("cpu") or {}).get("fpu")
        irqs = {int(k): str(v) for k, v in (table.get("interrupts") or {}).items()}
    elif a.regs:
        rp = Path(a.regs)
        table = yaml.load(rp.read_text(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
        try:
            regs_name = str(rp.resolve().relative_to((root / "chips").resolve()))
        except ValueError:
            regs_name = str(rp.resolve())
        source = a.source or f"register table {a.regs}"
        unit_bits = int(table.get("address_unit_bits") or 8)
        svd_fpu = (table.get("cpu") or {}).get("fpu")
        irqs = {int(k): str(v) for k, v in (table.get("interrupts") or {}).items()}
    else:
        table, source, unit_bits, svd_fpu, irqs = {}, "TODO (no SVD / register table given)", 8, None, {}
    fpu = a.fpu if a.fpu is not None else bool(svd_fpu) if svd_fpu is not None else \
        (a.cpu or fam["cpu"]) in ("cortex-m4", "cortex-m7")
    memory = [parse_mem(m) for m in a.mem] or list(fam["memory"])
    exclude = {x.strip() for x in a.exclude.split(",") if x.strip()}
    periphs = plan_peripherals(a.family, table, memory, exclude)
    pins = read_pins_csv(Path(a.pins), a.family) if a.pins else None

    text = chip_yaml(a, fam, periphs, irqs, unit_bits, fpu, pins, regs_name, source, memory, bool(a.mem))
    yaml.safe_load(text)                                   # must at least parse
    write(root / "chips" / f"{stem}.yaml", text, a.force, written)

    m = re.search(r"(\d+)", a.package or "")
    n_gpio = sum(1 for p in pins or [] if p["kind"] == "gpio") or "None  # TODO"
    svd_rel = ""
    if svd_path is not None:
        try:
            svd_rel = str(svd_path.resolve().relative_to(root.resolve()))
        except ValueError:
            svd_rel = str(svd_path.resolve())
    arch = a.arch or fam["arch"]
    test = TEST_TEMPLATE.format(part=a.part, stem=stem, package=a.package or "TODO", family=a.family, arch=arch,
                                cpu=a.cpu or fam["cpu"], fpu=fpu, unit_bits=unit_bits,
                                clock_hz=float(a.clock_hz) if a.clock_hz else float(fam["clock_hz"]),
                                fmt=fam["gpio_name_format"], expected=int(m.group(1)) if m else "None  # TODO",
                                n_gpio=n_gpio, svd_rel=svd_rel)
    write(root / "tests" / f"test_{stem}_chip.py", test, a.force, written)

    counts: dict[str, int] = {}
    for p in periphs:
        counts[p.model] = counts.get(p.model, 0) + 1
    todo = sum(1 for p in periphs if "TODO" in p.note)
    for w in written:
        print(f"wrote {w}")
    print(f"{len(periphs)} peripherals ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}), "
          f"{len(irqs)} IRQs, {len(pins or [])} pins; {todo} peripheral TODOs.  Next: grep TODO "
          f"{root / 'chips' / (stem + '.yaml')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
