"""STM32F105/F107 connectivity-line chip descriptions (chips/stm32f107vc.yaml base, stm32f107rc, stm32f105rc,
stm32f105vc): pin spot checks against ST's open pin data and RM0008 9.3 remap tables, memory, IRQs (RM0008 10.1.2
Table 61), DBGMCU IDCODE (31.6.1), omitted / gated blocks, AFIO connectivity-line MAPR (9.4.2), Ethernet / OTG / CAN
differences from the F4 models, pin_table_check --strict and svd2chip --check."""
from __future__ import annotations

import io
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
FILES = ("stm32f107vc", "stm32f107rc", "stm32f105rc", "stm32f105vc")
OPD = ROOT / "svd" / "dl-open-pin-data-f10x-cl"
OPD_FILE = {"stm32f107vc": "STM32F107V(B-C)Tx.xml", "stm32f107rc": "STM32F107R(B-C)Tx.xml",
            "stm32f105rc": "STM32F105R(8-B-C)Tx.xml", "stm32f105vc": "STM32F105V(8-B-C)Tx.xml"}
SVD = {"stm32f107vc": "dl-stm32f107/STM32F107xx.svd", "stm32f107rc": "dl-stm32f107/STM32F107xx.svd",
       "stm32f105rc": "dl-stm32f105/STM32F105xx.svd", "stm32f105vc": "dl-stm32f105/STM32F105xx.svd"}
FLASH = 0x08000000


@pytest.fixture(scope="module", params=FILES)
def desc(request):
    return load_chip(ROOT / "chips" / f"{request.param}.yaml")


def descs():
    return {f: load_chip(ROOT / "chips" / f"{f}.yaml") for f in FILES}


def pin(d, gpio):
    return next(p for p in d.pins if p.kind == "gpio" and p.gpio_name == gpio)


def make(d) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20010000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(d)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def test_identity_and_memory(desc):
    assert desc.family == "stm32f1" and desc.core.cpu == "cortex-m3" and desc.core.fpu is False
    assert desc.core.num_irqs == 68 and desc.core.clock_hz == 8_000_000
    mem = {m.name: m for m in desc.memory}
    assert (mem["FLASH"].base, mem["FLASH"].size) == (0x08000000, 256 * 1024)
    assert (mem["SRAM"].base, mem["SRAM"].size) == (0x20000000, 64 * 1024)
    assert desc.package == ("LQFP100" if desc.name.endswith("VC") else "LQFP64")
    assert desc.extra["systick_calib"] == 9000                    # RM0008 10.1.1


def test_irq_table(desc):
    """RM0008 10.1.2 Table 61 (connectivity line): 68 vectors, 43-49 reserved."""
    irqs = desc.irqs
    assert {n: irqs[n] for n in (18, 19, 20, 21, 22, 41, 42)} == {
        18: "ADC1_2", 19: "CAN1_TX", 20: "CAN1_RX0", 21: "CAN1_RX1", 22: "CAN1_SCE", 41: "RTC_Alarm",
        42: "OTG_FS_WKUP"}
    assert not any(n in irqs for n in range(43, 50))
    assert [irqs[n] for n in range(56, 68)] == [
        "DMA2_Channel1", "DMA2_Channel2", "DMA2_Channel3", "DMA2_Channel4", "DMA2_Channel5", "ETH", "ETH_WKUP",
        "CAN2_TX", "CAN2_RX0", "CAN2_RX1", "CAN2_SCE", "OTG_FS"]
    assert irqs[50] == "TIM5" and irqs[54] == "TIM6" and irqs[55] == "TIM7"


def test_pins_match_open_pin_data(desc):
    """Every package position and pin type equals ST's open pin data (mcu/<part>.xml)."""
    key = desc.name.lower()
    root = ET.parse(OPD / OPD_FILE[key]).getroot()
    ns = "{http://dummy.com}"
    xml = {int(p.get("Position")): (p.get("Name"), p.get("Type")) for p in root.iter(f"{ns}Pin")}
    assert len(xml) == len(desc.pins)
    for p in desc.pins:
        name, typ = xml[p.num]
        if p.kind == "gpio":
            assert typ == "I/O" and name.split("-")[0] == p.gpio_name, (p.num, name, p.name)
        elif p.kind == "power":
            assert typ == "Power" and name == p.name
        else:
            assert name == p.name


def test_pin_spot_checks():
    d = descs()
    v, r, f5r = d["stm32f107vc"], d["stm32f107rc"], d["stm32f105rc"]
    # LQFP100: PA9 = pin 68 (USART1_TX, OTG_FS_VBUS), PA11 = 70, OSC_IN pin 12 dedicated, PE2 = pin 1 (TRACECK)
    assert pin(v, "PA9").num == 68 and pin(v, "PA9").functions["additional"] == "OTG_FS_VBUS"
    assert pin(v, "PA11").num == 70 and pin(v, "PA11").functions["additional"] == "OTG_FS_DM"
    assert next(p for p in v.pins if p.num == 12).name == "OSC_IN"
    assert pin(v, "PE2").num == 1 and pin(v, "PE2").functions["AF"] == "TRACECK"
    # Ethernet RMII pins (RM0008 29.3): PA1 REF_CLK, PA7 CRS_DV, PB11 TX_EN, PC1 MDC, PA2 MDIO
    assert "ETH_RMII_REF_CLK" in pin(v, "PA1").functions["AF"].split("/")
    assert "ETH_RMII_CRS_DV" in pin(v, "PA7").functions["AF"].split("/")
    assert "ETH_RMII_TX_EN" in pin(v, "PB11").functions["AF"].split("/")
    assert pin(v, "PC1").functions["AF"] == "ETH_MDC" and "ETH_MDIO" in pin(v, "PA2").functions["AF"]
    # ETH_REMAP (Table 58): RX_DV/RXD0-3 on PD8-PD12; PTP PPS on PB5 (MAPR bit 30)
    assert "ETH_MII_RXD3" in pin(v, "PD12").functions["REMAP"].split("/")
    assert "ETH_PPS_OUT" in pin(v, "PB5").functions["REMAP"].split("/")
    # CAN (Tables 34/35): CAN1 PA11/PA12 default, PB8/PB9 and PD0/PD1 remapped; CAN2 PB12/PB13, PB5/PB6 remapped
    assert "CAN1_RX" in pin(v, "PA11").functions["AF"] and "CAN1_RX" in pin(v, "PD0").functions["REMAP"]
    assert "CAN2_RX" in pin(v, "PB12").functions["AF"] and "CAN2_TX" in pin(v, "PB6").functions["REMAP"]
    # SPI3/I2S3 remap (Table 57): PA15/PB3-5 default, PA4/PC10-12 remapped
    assert "I2S3_WS" in pin(v, "PA15").functions["AF"] and "SPI3_NSS" in pin(v, "PA4").functions["REMAP"]
    # LQFP64: OSC_IN/PD0 on pin 5, PB12 = pin 33, PA9 = pin 42
    assert next(p for p in f5r.pins if p.num == 5).name == "OSC_IN/PD0"
    assert pin(f5r, "PB12").num == 33 and pin(f5r, "PA9").num == 42
    # F105: I2C2 on PB10/PB11 and no Ethernet functions; F107RC: Ethernet, no I2C2
    assert "I2C2_SCL" in pin(f5r, "PB10").functions["AF"]
    assert not any("ETH_" in f for p in f5r.pins for f in p.functions.values())
    assert "ETH_MII_RX_ER" in pin(r, "PB10").functions["AF"] and "I2C2" not in pin(r, "PB10").functions["AF"]
    # shared GPIOs: identical functions between the 64- and 100-pin packages of a part
    for a, b in (("stm32f107vc", "stm32f107rc"), ("stm32f105vc", "stm32f105rc")):
        small = {p.gpio_name: p.functions for p in d[b].pins if p.kind == "gpio" and p.gpio_name not in ("PD0", "PD1")}
        big = {p.gpio_name: p.functions for p in d[a].pins if p.kind == "gpio"}
        for g, f in small.items():
            assert big[g] == f, (a, g)


def test_pin_table_check_strict(desc):
    from pin_table_check import check_desc
    rep = check_desc(desc)
    assert rep.errors == [] and rep.warnings == [], rep.text()


def test_svd2chip_check(desc):
    from svd2chip import check
    out = io.StringIO()
    assert check(ROOT / "svd" / SVD[desc.name.lower()], ROOT / "chips" / f"{desc.name.lower()}.yaml", out) == 0, \
        out.getvalue()


def test_idcode_and_blocks(desc):
    """31.6.1: DEV_ID 0x418, REV_ID 0x1001 (rev Z); the F105 has no Ethernet MAC (inactive stub); no USB device, FSMC,
    SDIO, TIM8, ADC3 on the connectivity line."""
    c = make(desc)
    assert c.bus.read(0xE0042000, 4) & 0xFFFF0FFF == 0x10010418
    eth = c.peripherals["ETH"]
    assert getattr(eth, "absent", False) == desc.name.startswith("STM32F105")
    for n in ("USB", "USB_CAN_SRAM", "FSMC", "SDIO", "TIM8", "ADC3", "GPIOF", "GPIOG"):
        assert n not in c.peripherals
    assert c.peripherals["RCC"].model == "stm32f1.rcc_cl"
    assert c.peripherals["BKP"].n_data == 42


def test_afio_cl_mapr(desc):
    """9.4.2 (connectivity line): ETH_REMAP 21, CAN2_REMAP 22, MII_RMII_SEL 23, SPI3_REMAP 28, TIM2ITR1 29, PTP_PPS 30
    writable; bits 17-20 reserved; CAN1_REMAP moves CAN1_RX/TX (Table 34)."""
    c = make(desc)
    c.bus.write(0x40021018, 4, 1)                                  # AFIOEN
    a = c.peripherals["AFIO"]
    c.bus.write(0x40010004, 4, 0xFFFFFFFF & ~(7 << 24))
    v = c.bus.read(0x40010004, 4)
    assert v & (0xF << 17) == 0 and v & (7 << 21) == 7 << 21 and v & (7 << 28) == 7 << 28 and not v >> 31
    c.bus.write(0x40010004, 4, (2 << 13) | (1 << 22) | (1 << 28) | (1 << 21))
    assert a.signal_pin("CAN1_RX") == "PB8" and a.signal_pin("CAN2_TX") == "PB6"
    assert a.signal_pin("I2S3_CK") == "PC10" and a.signal_pin("ETH_RMII_CRS_DV") == "PD8"
    assert a.signal_pin("ETH_PPS_OUT") == ""                       # PTP_PPS_REMAP = 0: PPS not output


def test_exti_lines_and_otg_eth_params():
    """10.2.5: line 19 Ethernet wake-up exists (20 lines); OTG GCCFG has no NOVBUSSENS (28.6.2); DIEPTXFx reset
    0x02000200; the F107 MAC: DMABMR bit 7 (EDFE) reserved, MACA2HR resets to 0x0000FFFF (29.8.1/29.8.4)."""
    c = make(descs()["stm32f107vc"])
    c.bus.write(0x40010400, 4, 0xFFFFFFFF)                         # EXTI_IMR
    assert c.bus.read(0x40010400, 4) == (1 << 20) - 1
    c.bus.write(0x40021014, 4, 0x14 | (1 << 12) | (7 << 14))       # OTGFSEN, ETHMAC/TX/RX
    c.bus.write(0x50000038, 4, 0x003F0000)
    assert c.bus.read(0x50000038, 4) == 0x001D0000
    assert c.bus.read(0x50000104, 4) == 0x02000200 and c.bus.read(0x5000010C, 4) == 0x02000200
    assert c.bus.read(0x40028050, 4) == 0x0000FFFF
    assert c.bus.read(0x40028040, 4) == 0x8000FFFF
    assert c.bus.read(0x40029000, 4) == 0x00020101
    c.bus.write(0x40029000, 4, 0x00020180 | (1 << 25))
    assert not c.bus.read(0x40029000, 4) & (1 << 7)


def test_eth_phy_clock_from_mco():
    """stm32f1.eth `phy_clock: mco` (29.4.4 / 8.2.10): without 50 MHz on MCO (PA8) the RMII REF_CLK is missing and the
    DMABMR software reset never completes; with MCO = PLL3 at 50 MHz on PA8 it does."""
    c = make(descs()["stm32f107vc"])
    eth = c.peripherals["ETH"]
    eth.phy_clock = "mco"
    w = lambda a, v: c.bus.write(a, 4, v)
    w(0x40021000, c.bus.read(0x40021000, 4) | (1 << 16))           # HSEON
    w(0x4002102C, (4 << 4) | (8 << 12))                            # PREDIV2 /5, PLL3 x10 = 50 MHz
    w(0x40021000, c.bus.read(0x40021000, 4) | (1 << 28))           # PLL3ON
    w(0x40021018, 0x1D)                                            # AFIO, GPIOA-C
    w(0x40010004, 1 << 23)                                         # RMII
    w(0x40010800, 0x44444444 & ~(0xF << 4) | (0x4 << 4))           # PA1 input floating
    w(0x40021014, 0x14 | (7 << 14))
    w(0x40029000, 1)
    c.run_cycles(20000)
    assert c.bus.read(0x40029000, 4) & 1                           # no REF_CLK: SR stuck
    w(0x40010804, (c.bus.read(0x40010804, 4) & ~0xF) | 0xB)        # PA8 AF push-pull
    w(0x40021004, 0xB << 24)                                       # MCO = PLL3
    assert eth.mco_ok()
    c.bus.read(0x40029000, 4)                                      # polling SR: the reset now completes
    c.run_cycles(20000)
    assert not c.bus.read(0x40029000, 4) & 1


def test_can_cl_filter_split():
    """24.3.1 / 24.9.5: CAN1 owns the 28 filter banks; FMR.CAN2SB resets to 14 and is writable with FINIT."""
    c = make(descs()["stm32f105rc"])
    c.bus.write(0x4002101C, 4, (1 << 25) | (1 << 26))
    assert (c.bus.read(0x40006600, 4) >> 8) & 0x3F == 14
    c.bus.write(0x40006600, 4, (20 << 8) | 1)
    assert (c.bus.read(0x40006600, 4) >> 8) & 0x3F == 20
    assert c.peripherals["CAN1"].n_banks == 28
    assert c.peripherals["CAN2"]._irqs["RX0"] == 64 and c.peripherals["CAN1"]._irqs["TX"] == 19
