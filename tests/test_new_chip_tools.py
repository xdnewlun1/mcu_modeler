"""tools/new_chip.py (chip scaffolder) and tools/pin_table_check.py (pin table validator).

The scaffolder is run on the cached STM32F405 SVD into a temp dir: the chip YAML must load, map the STM32 models the
way chips/stm32f405zg.yaml does, pass `svd2chip --check`, build a Chip without stub fallbacks, and its generated test
file must pass everything except the deliberate TODO spot-check test.  pin_table_check must pass the real chips and
catch a broken table."""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
import yaml

from chipem.chip import Chip
from chipem.chipdesc import load_chip

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SVD = sorted((ROOT / "svd").glob("dl-stm32f405-*/STM32F405.svd"))
K22_SVD = sorted((ROOT / "svd").glob("dl-*/MK22F12.svd"))
sys.path.insert(0, str(TOOLS))
import new_chip  # noqa: E402
import pin_table_check  # noqa: E402
import svd2chip  # noqa: E402

sys.path.remove(str(TOOLS))

PINS_CSV = """num,name,kind,port,bit,functions,default
1,VDD,power,,,,
2,PA9,gpio,A,9,AF7=USART1_TX,
3,PA10,gpio,A,10,AF7=USART1_RX;AF10=OTG_FS_ID,
4,NRST,special,,,,
5,PB0,gpio,B,0,AF1=TIM1_CH2N;ADC=ADC12_IN8,
6,PC14/OSC32_IN,gpio,C,14,analog=OSC32_IN,
7,BOOT0,special,,,,
8,VSS,power,,,,
"""


@pytest.fixture(scope="module")
def scaffold(tmp_path_factory):
    if not SVD:
        pytest.skip("STM32F405.svd not downloaded")
    root = tmp_path_factory.mktemp("scaffold")
    csv = root / "pins.csv"
    csv.write_text(PINS_CSV)
    rc = new_chip.main(["--part", "TESTF405", "--package", "TSSOP8", "--family", "stm32", "--svd", str(SVD[0]),
                        "--fpu", "--pins", str(csv), "--root", str(root),
                        "--mem", "FLASH:flash:0x08000000:0x100000", "--mem", "SRAM:ram:0x20000000:0x20000"])
    assert rc == 0
    return root


def test_scaffold_writes_three_files(scaffold):
    for rel in ("chips/testf405.yaml", "chips/testf405_regs.yaml", "tests/test_testf405_chip.py"):
        assert (scaffold / rel).is_file(), rel


def test_scaffold_yaml_loads_and_maps_models(scaffold):
    desc = load_chip(scaffold / "chips" / "testf405.yaml")
    assert desc.name == "TESTF405" and desc.package == "TSSOP8" and desc.family == "stm32"
    assert desc.gpio_name_format == "P{port}{bit}" and desc.regs_file == "testf405_regs.yaml"
    assert desc.core.cpu == "cortex-m4" and desc.core.fpu and desc.core.clock_hz == 16e6
    assert desc.extra["vtor"] == 0x08000000
    models = {p.name: p for p in desc.peripherals}
    assert models["GPIOA"].model == "stm32.gpio" and models["GPIOA"].params == {"port": "A"}
    assert models["USART1"].model == "stm32.usart" and models["USART1"].params == {"index": 1, "kind": "usart"}
    assert models["UART4"].params == {"index": 4, "kind": "uart"}
    assert models["RCC"].model == "stm32.rcc" and models["IWDG"].model == "stm32.iwdg"
    assert models["TIM2"].model == "stm32.tim" and models["TIM9"].model == "stm32.tim"
    assert models["EXTI"].model == "stm32.exti" and models["SPI1"].model == "stm32.spi"
    assert models["SPI3"].model == "stm32.spi" and models["I2S2ext"].model == "stm32.i2s_ext"
    assert models["I2S3ext"].params == {"main": "SPI3"}
    assert models["Ethernet_MAC"].model == "stm32.eth" and models["Ethernet_MAC"].size == 0x1400
    assert not {"Ethernet_MMC", "Ethernet_PTP", "Ethernet_DMA"} & set(models)      # inside the stm32.eth block
    assert (models["DCMI"].model, models["CRYP"].model, models["HASH"].model) == ("stm32.dcmi", "stm32.cryp",
                                                                                    "stm32.hash")
    assert (models["CRC"].model, models["RNG"].model, models["DAC"].model, models["RTC"].model,
            models["WWDG"].model) == ("stm32.crc", "stm32.rng", "stm32.dac", "stm32.rtc", "stm32.wwdg")
    assert models["DMA1"].model == "stm32.dma" and models["DMA1"].params == {"controller": 1}
    assert models["DMA2"].model == "stm32.dma" and models["DMA2"].params == {"controller": 2}
    assert models["ADC1"].size == 0x100                              # clamped to the next block (ADC2)
    assert models["ADC1"].model == models["ADC3"].model == "stm32.adc" and models["C_ADC"].model == "stm32.adc_common"
    assert not any(0xE000E000 <= p.base < 0xE000F000 for p in desc.peripherals)   # SCS is arch-owned
    assert desc.irqs[37] == "USART1" and desc.irqs[6] == "EXTI0"
    assert [p.num for p in desc.pins] == list(range(1, 9))
    assert desc.pin("PA9").functions["AF7"] == "USART1_TX" and desc.pin("PB0").functions["ADC"] == "ADC12_IN8"
    assert desc.pin(4).kind == "special" and desc.pin("PA9").default == "GPIO"


def test_scaffold_matches_the_real_f405_models(scaffold):
    gen = {p.name: p.model for p in load_chip(scaffold / "chips" / "testf405.yaml").peripherals}
    real = {p.name: p.model for p in load_chip(ROOT / "chips" / "stm32f405zg.yaml").peripherals}
    diff = {n: (real[n], gen[n]) for n in real.keys() & gen.keys() if real[n] != gen[n]}
    assert diff == {}


def test_scaffold_passes_svd_check_and_builds(scaffold):
    out = io.StringIO()
    assert svd2chip.check(SVD[0], scaffold / "chips" / "testf405.yaml", out=out) == 0, out.getvalue()
    chip = Chip(load_chip(scaffold / "chips" / "testf405.yaml"))
    assert not [e for e in chip.log if e.kind == "warn"]
    assert chip.pins.get("PA9").id == "PA9"


def test_generated_test_file_only_todo_fails(scaffold):
    path = scaffold / "tests" / "test_testf405_chip.py"
    spec = importlib.util.spec_from_file_location("gen_test_testf405", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    desc = load_chip(mod.YAML)
    assert mod.EXPECTED_PINS == 8 and mod.EXPECTED_GPIO == 4 and mod.SVD
    for name in ("test_core_and_package", "test_pin_table_checker", "test_pin_count_numbers_and_gpio",
                 "test_memory_and_irqs", "test_chip_builds_without_stub_fallbacks"):
        getattr(mod, name)(desc)
    mod.test_svd_check_zero_mismatches()
    with pytest.raises(pytest.fail.Exception, match="TODO"):
        mod.test_pin_spot_checks(desc)


def test_scaffold_refuses_to_overwrite(scaffold):
    with pytest.raises(SystemExit, match="exists"):
        new_chip.main(["--part", "TESTF405", "--family", "stm32", "--root", str(scaffold)])


def test_scaffold_without_svd_or_pins_is_marked_todo(tmp_path):
    assert new_chip.main(["--part", "ACME100", "--package", "QFN32", "--family", "acme", "--root", str(tmp_path)]) == 0
    text = (tmp_path / "chips" / "acme100.yaml").read_text()
    assert "pins: []" in text and "TODO all 32 package pins" in text
    desc = load_chip(tmp_path / "chips" / "acme100.yaml")
    assert desc.peripherals == [] and desc.pins == [] and desc.core.cpu == "cortex-m3"
    rep = pin_table_check.check_desc(desc)
    assert not rep.ok and any("empty" in e for e in rep.errors)


def test_model_rule_requires_matching_registers():
    # STM32F1-style GPIO (CRL/CRH, no MODER) must not be mapped to the F4 stm32.gpio model
    model, params, name, note = new_chip.map_model("stm32", "GPIOA", ["CRL", "CRH", "IDR", "ODR", "BSRR"])
    assert model == "stub" and "MODER" in note and "TODO" in note
    assert new_chip.map_model("stm32", "GPIOC", ["MODER", "AFRL", "IDR", "ODR", "BSRR"])[:2] == ("stm32.gpio",
                                                                                                {"port": "C"})
    assert new_chip.map_model("kinetis", "UART3", ["BDH", "BDL", "C2", "S1", "D"])[:2] == ("kinetis.uart", {"index": 3})
    assert new_chip.map_model("kinetis", "RFVBAT", [])[0:3:2] == ("kinetis.vbat", "VBAT")
    assert new_chip.map_model("kinetis", "CRC", ["DATA", "GPOLY", "CTRL"])[0] == "kinetis.crc"
    assert new_chip.map_model("kinetis", "DAC0", ["DAT0L", "DAT0H", "SR", "C0", "C1", "C2"])[:2] == ("kinetis.dac",
                                                                                                  {"index": 0})
    cmp_regs = ["CR0", "CR1", "FPR", "SCR", "DACCR", "MUXCR"]
    assert new_chip.map_model("kinetis", "CMP2", cmp_regs)[:2] == ("kinetis.cmp", {"index": 2})
    assert new_chip.map_model("kinetis", "CMP0", ["CR0", "CR1"])[0] == "stub"           # a different layout
    assert new_chip.map_model("kinetis", "EWM", ["CTRL", "SERV", "CMPL", "CMPH"])[0] == "kinetis.ewm"
    assert new_chip.map_model("kinetis", "VREF", ["TRM", "SC"])[0] == "kinetis.vref"
    assert new_chip.map_model("ti_c2000", "SCIB", ["SCICCR", "SCICTL1", "SCITXBUF", "SCIRXBUF"])[0] == "ti_c2000.sci"
    epwm_regs = ["TBCTL", "TBPRD", "CMPA", "AQCTLA", "DBCTL", "ETSEL"]
    assert new_chip.map_model("ti_c2000", "EPWM3", epwm_regs)[:2] == ("ti_c2000.epwm",
                                                                      {"gate": "EPWM3ENCLK", "index": 3})
    assert new_chip.map_model("ti_c2000", "EPWM3", [])[0] == "stub"         # a different register layout
    ecap_regs = ["TSCTR", "CTRPHS", "CAP1", "CAP2", "ECCTL1", "ECCTL2", "ECEINT", "ECFLG", "ECCLR", "ECFRC"]
    assert new_chip.map_model("ti_c2000", "ECAP2", ecap_regs)[:2] == ("ti_c2000.ecap",
                                                                      {"gate": "ECAP2ENCLK", "index": 2})
    assert new_chip.map_model("ti_c2000", "ECAP2", [])[0] == "stub"         # a different register layout
    eqep_regs = ["QPOSCNT", "QPOSINIT", "QPOSMAX", "QPOSCMP", "QUTMR", "QUPRD", "QDECCTL", "QEPCTL", "QCAPCTL",
                 "QPOSCTL", "QEINT", "QFLG", "QCLR", "QFRC", "QEPSTS"]
    assert new_chip.map_model("ti_c2000", "EQEP2", eqep_regs)[:2] == ("ti_c2000.eqep",
                                                                      {"gate": "EQEP2ENCLK", "index": 2})
    assert new_chip.map_model("ti_c2000", "EQEP1", [])[0] == "stub"         # a different register layout
    # CAN: Kinetis FlexCAN, C2000 eCAN (a missing register -> stub, another CAN layout)
    flexcan = ["MCR", "CTRL1", "TIMER", "ECR", "ESR1", "IMASK1", "IFLAG1", "CTRL2", "CS0", "ID0", "WORD00"]
    assert new_chip.map_model("kinetis", "CAN0", flexcan)[:2] == ("kinetis.flexcan", {"index": 0})
    assert new_chip.map_model("kinetis", "CAN1", ["MCR", "CTRL1"])[0] == "stub"
    assert new_chip.map_model("kinetis", "MCM", ["PLASC", "PLAMC", "PLACR", "ISCR"])[0] == "stub"   # PLACR-style MCM
    assert new_chip.map_model("kinetis", "I2S0", ["TCSR", "TCR2"])[0] == "stub"
    bxcan = ["MCR", "MSR", "TSR", "RF0R", "RF1R", "IER", "ESR", "BTR", "TI0R", "TDT0R", "RI0R", "FMR", "FA1R"]
    assert new_chip.map_model("stm32", "CAN2", bxcan)[:2] == ("stm32.can", {"index": 2})
    assert new_chip.map_model("stm32", "CAN1", ["CREL", "ENDN", "DBTP", "NBTP"])[0] == "stub"   # FDCAN layout
    # USB: STM32 OTG (DWC2) whole 256 KB windows renamed OTG_FS/OTG_HS, Kinetis USB0 + USBDCD; other layouts -> stub
    otg = ["FS_GOTGCTL", "FS_GOTGINT", "FS_GAHBCFG", "FS_GUSBCFG", "FS_GRSTCTL", "FS_GINTSTS", "FS_GINTMSK",
           "FS_GRXFSIZ", "FS_GCCFG", "FS_CID"]
    assert new_chip.map_model("stm32", "OTG_FS_GLOBAL", otg)[:3] == ("stm32.otg_fs", {}, "OTG_FS")
    assert new_chip.map_model("stm32", "OTG_HS_GLOBAL", ["OTG_HS_" + n[3:] for n in otg])[:3] == ("stm32.otg_hs", {},
                                                                                                  "OTG_HS")
    assert new_chip.map_model("stm32", "OTG_FS_GLOBAL", ["CNTR", "ISTR", "FNR", "DADDR"])[0] == "stub"   # USB FS dev
    usb0 = ["PERID", "IDCOMP", "ISTAT", "INTEN", "ERRSTAT", "STAT", "CTL", "ADDR", "BDTPAGE1", "ENDPT0", "USBCTRL",
            "CONTROL", "USBTRC0"]
    assert new_chip.map_model("kinetis", "USB0", usb0)[:2] == ("kinetis.usbfs", {})
    assert new_chip.map_model("kinetis", "USB0", usb0[:-2])[0] == "stub"           # no CONTROL/USBTRC0: other USB IP
    assert new_chip.map_model("kinetis", "USBDCD", ["CONTROL", "CLOCK", "STATUS", "TIMER0", "TIMER1"])[0] == \
        "kinetis.usbdcd"
    ecan = ["CANME", "CANMD", "CANTRS", "CANTRR", "CANTA", "CANRMP", "CANMC", "CANBTC", "CANES", "CANGIF0", "CANGIM",
            "MBOX0_MSGID"]
    assert new_chip.map_model("ti_c2000", "ECANB", ecan)[:2] == ("ti_c2000.ecan", {"gate": "ECANBENCLK"})
    assert new_chip.map_model("ti_c2000", "ECANA", [])[0] == "stub"       # e.g. the DCAN of newer C2000 parts
    # I2C: Kinetis I2Cn, STM32 I2C v1 (F4; the TIMINGR v2 layout stays a stub), C2000 I2C-A
    assert new_chip.map_model("kinetis", "I2C2", ["A1", "F", "C1", "S", "D", "C2", "FLT"])[:2] == \
        ("kinetis.i2c", {"index": 2})
    v1 = ["CR1", "CR2", "OAR1", "OAR2", "DR", "SR1", "SR2", "CCR", "TRISE"]
    assert new_chip.map_model("stm32", "I2C3", v1)[0] == "stm32.i2c"
    assert new_chip.map_model("stm32", "I2C1", ["CR1", "CR2", "OAR1", "TIMINGR", "ISR", "ICR", "RXDR", "TXDR"])[0] \
        == "stub"
    c2k = ["I2COAR", "I2CIER", "I2CSTR", "I2CCLKL", "I2CCLKH", "I2CMDR", "I2CPSC"]
    assert new_chip.map_model("ti_c2000", "I2CA", c2k)[:2] == ("ti_c2000.i2c", {"gate": "I2CAENCLK"})
    assert new_chip.map_model("ti_c2000", "FLASH", ["FOPT"])[:3] == ("ti_c2000.flash", {"regs": "FLASH"}, "FLASHREGS")
    dma_c2k = ["DMACTRL", "PRIORITYCTRL1", "CH1_MODE", "CH1_CONTROL", "CH1_BURST_SIZE", "CH1_SRC_ADDR_SHADOW",
               "CH6_DST_ADDR_ACTIVE"]
    assert new_chip.map_model("ti_c2000", "DMA", dma_c2k)[:2] == ("ti_c2000.dma", {"gate": "DMAENCLK"})
    assert new_chip.map_model("ti_c2000", "DMA", ["DMACTRL"])[0] == "stub"     # another DMA layout (e.g. F2837x)
    mcbsp = ["DRR2", "DRR1", "DXR2", "DXR1", "SPCR2", "SPCR1", "RCR2", "RCR1", "XCR2", "XCR1", "SRGR2", "SRGR1",
             "PCR", "MFFINT"]
    assert new_chip.map_model("ti_c2000", "MCBSPB", mcbsp)[:2] == ("ti_c2000.mcbsp", {"gate": "MCBSPBENCLK"})
    assert new_chip.map_model("ti_c2000", "MCBSPA", ["DRR1"])[0] == "stub"
    assert new_chip.map_model("ti_c2000", "XINTF", ["XTIMING0", "XINTCNF2", "XBANK"])[0] == "ti_c2000.xintf"
    # F1-style SPI (no I2SCFGR on some parts), F0/F3 CRC with POL/INIT, F1 RTC (CRH/CNTH): not the F4 models
    assert new_chip.map_model("stm32", "SPI1", ["CR1", "CR2", "SR", "DR", "CRCPR"])[0] == "stub"
    # SD hosts and the FSMC: STM32 SDIO (an SDMMC layout stays a stub), Kinetis SDHC, STM32 FSMC
    sdio = ["POWER", "CLKCR", "ARG", "CMD", "RESPCMD", "RESP1", "DTIMER", "DLEN", "DCTRL", "STA", "ICR", "MASK", "FIFO"]
    assert new_chip.map_model("stm32", "SDIO", sdio)[0] == "stm32.sdio"
    assert new_chip.map_model("stm32", "SDIO", ["POWER", "CLKCR", "ARG", "CMD", "STA", "ICR", "MASK"])[0] == "stub"
    sdhc = ["DSADDR", "BLKATTR", "CMDARG", "XFERTYP", "CMDRSP0", "PRSSTAT", "PROCTL", "SYSCTL", "IRQSTAT",
            "IRQSTATEN", "IRQSIGEN", "WML", "ADSADDR"]
    assert new_chip.map_model("kinetis", "SDHC", sdhc)[0] == "kinetis.sdhc"
    assert new_chip.map_model("kinetis", "SDHC", ["DSADDR", "BLKATTR"])[0] == "stub"
    assert new_chip.map_model("stm32", "FSMC", ["BCR1", "BTR1", "BWTR1", "PCR2"])[0] == "stm32.fsmc"
    assert new_chip.map_model("stm32", "FSMC", ["SDCR1", "SDTR1"])[0] == "stub"
    assert new_chip.map_model("stm32", "SPI2", ["CR1", "CR2", "SR", "DR", "CRCPR", "I2SCFGR"])[0] == "stm32.spi"
    assert new_chip.map_model("stm32", "CRC", ["DR", "IDR", "CR", "INIT", "POL"])[0] == "stm32.crc"
    assert new_chip.map_model("stm32", "RTC", ["CRH", "CRL", "PRLH", "CNTH"])[0] == "stub"
    # TIM9-14 (1-2 channels, up-counting only) use stm32.tim too; their features follow from the SVD registers
    for t in ("TIM9", "TIM12", "TIM14"):
        model, _params, _name, _note = new_chip.map_model("stm32", t, ["CR1", "PSC", "ARR", "SR", "DIER"])
        assert model == "stm32.tim", t
    assert new_chip.map_model("stm32", "TIM8", ["CR1", "PSC", "ARR", "SR", "DIER"])[0] == "stm32.tim"
    adc_regs = ["SR", "CR1", "CR2", "SMPR1", "SMPR2", "HTR", "LTR", "SQR1", "SQR2", "SQR3", "JSQR", "DR"]
    assert new_chip.map_model("stm32", "ADC2", adc_regs)[:2] == ("stm32.adc", {})
    assert new_chip.map_model("stm32", "ADC1", ["ISR", "IER", "CR", "CFGR", "DR"])[0] == "stub"     # L4/G0 layout
    for n in ("C_ADC", "ADC_Common", "ADC123_Common"):
        assert new_chip.map_model("stm32", n, ["CSR", "CCR", "CDR"])[0] == "stm32.adc_common", n
    dma_regs = ["LISR", "HISR", "LIFCR", "HIFCR", "S0CR", "S0NDTR", "S0PAR", "S0M0AR", "S0M1AR", "S0FCR"]
    assert new_chip.map_model("stm32", "DMA2", dma_regs)[:2] == ("stm32.dma", {"controller": 2})
    # an F0/L4-style DMA (ISR/IFCR, CCR1/CNDTR1 channels, no streams) must not map to the F4 stream model
    assert new_chip.map_model("stm32", "DMA1", ["ISR", "IFCR", "CCR1", "CNDTR1", "CPAR1", "CMAR1"])[0] == "stub"


@pytest.mark.skipif(not K22_SVD, reason="MK22F12.svd not downloaded")
def test_scaffold_kinetis_and_c2000_match_the_real_chips(tmp_path):
    assert new_chip.main(["--part", "K22TEST", "--package", "LQFP100", "--family", "kinetis", "--svd",
                          str(K22_SVD[0]), "--root", str(tmp_path)]) == 0
    assert new_chip.main(["--part", "C2KTEST", "--package", "LQFP176", "--family", "ti_c2000", "--regs",
                          str(ROOT / "chips" / "tms320f28232_regs.yaml"), "--root", str(tmp_path)]) == 0
    for gen, real in (("k22test", "mk22fn1m0vll12"), ("c2ktest", "tms320f28232")):
        g = {p.name: (p.model, p.base) for p in load_chip(tmp_path / "chips" / f"{gen}.yaml").peripherals}
        r = {p.name: (p.model, p.base) for p in load_chip(ROOT / "chips" / f"{real}.yaml").peripherals}
        assert {n: (r[n], g[n]) for n in r.keys() & g.keys() if r[n] != g[n]} == {}, gen
        assert len(r.keys() & g.keys()) >= 30
    k22 = {p.name: p for p in load_chip(tmp_path / "chips" / "k22test.yaml").peripherals}
    alias = k22["FTM2_ALIAS"]                       # not in the SVD: added by the family's alias table
    assert (alias.model, alias.base, alias.size, alias.params) == ("kinetis.ftm_alias", 0x4003A000, 0x1000,
                                                                   {"target": "FTM2"})
    assert k22["FTM2"].model == "kinetis.ftm" and k22["FTM2"].base == 0x400B8000
    dalias = k22["DAC0_ALIAS"]
    assert (dalias.model, dalias.base, dalias.params) == ("kinetis.ftm_alias", 0x4003F000, {"target": "DAC0"})
    for n, m in (("CRC", "kinetis.crc"), ("DAC0", "kinetis.dac"), ("CMP0", "kinetis.cmp"), ("CMP1", "kinetis.cmp"),
                 ("CMP2", "kinetis.cmp"), ("EWM", "kinetis.ewm"), ("VREF", "kinetis.vref"), ("CAN0", "kinetis.flexcan")):
        assert k22[n].model == m, n
    for n, m in (("I2S0", "kinetis.sai"), ("CMT", "kinetis.cmt"), ("MCM", "kinetis.mcm"), ("FMC", "kinetis.fmc"),
                 ("AIPS0", "kinetis.aips"), ("AIPS1", "kinetis.aips"), ("AXBS", "kinetis.axbs"), ("MPU", "kinetis.mpu"),
                 ("FB", "kinetis.flexbus")):
        assert k22[n].model == m, n
    fbs = k22["FB_SPACE"]                           # FlexBus windows: not in the SVD, added by the alias table
    assert (fbs.model, fbs.base, fbs.size) == ("kinetis.flexbus_space", 0x60000000, 0x80000000)
    out = io.StringIO()
    assert svd2chip.check(K22_SVD[0], tmp_path / "chips" / "k22test.yaml", out=out) == 0, out.getvalue()
    c2k = load_chip(tmp_path / "chips" / "c2ktest.yaml")
    assert c2k.core.arch == "c28x" and c2k.core.addr_unit_bits == 16 and c2k.gpio_name_format == "GPIO{num}"
    assert c2k.extra["boot"]["entry"] == 0x33FFF6


# ------------------------------------------------------------------------------------------------ pin_table_check
@pytest.mark.parametrize("chip", ["mk22fn1m0vll12", "mk22fx512vll12", "stm32f405zg", "tms320f28232"])
def test_pin_table_check_passes_real_chips(chip):
    rep = pin_table_check.check_file(ROOT / "chips" / f"{chip}.yaml")
    assert rep.ok and not rep.warnings, rep.text()


def test_pin_table_check_cli(capsys):
    assert pin_table_check.main([str(ROOT / "chips" / "stm32f405zg.yaml"), "--expect", "144"]) == 0
    out = capsys.readouterr().out
    assert "144 pins" in out and "0 error(s)" in out
    assert pin_table_check.main([str(ROOT / "chips" / "stm32f405zg.yaml"), "--expect", "100"]) == 1


def _broken_chip(tmp_path) -> Path:
    data = yaml.safe_load((ROOT / "chips" / "stm32f405zg.yaml").read_text())
    pins = data["pins"]
    by = {p["name"]: p for p in pins}
    by["PA10"]["bit"] = 9                         # PA10 now claims PA9's port/bit -> name mismatch + duplicate GPIO
    by["PB7"]["functions"]["AF1"] = "PB6"        # function naming another pin's GPIO
    pins[5]["kind"] = "supply"                    # VBAT: unknown kind
    del by["PC0"]["port"]                         # GPIO without port
    pins.append(dict(pins[0]))                    # duplicate pin number 1 (and pin count 145)
    data["regs_file"] = str(ROOT / "chips" / "stm32f405_regs.yaml")
    p = tmp_path / "broken.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


def test_pin_table_check_catches_a_broken_table(tmp_path):
    rep = pin_table_check.check_file(_broken_chip(tmp_path))
    text = "\n".join(rep.errors)
    assert not rep.ok
    assert "145 pins, expected 144" in text
    assert "duplicate pin numbers: [1]" in text
    assert "'PA10': GPIO name 'PA9'" in text and "GPIO PA9 on several pins" in text
    assert "names GPIO PB6" in text
    assert "kind 'supply'" in text
    assert "'PC0': gpio pin without port/bit" in text
    assert pin_table_check.main([str(tmp_path / "broken.yaml")]) == 1


def test_pin_table_check_warnings_and_wrong_format(tmp_path):
    data = yaml.safe_load((ROOT / "chips" / "stm32f405zg.yaml").read_text())
    data["gpio_name_format"] = "PT{port}{bit}"    # Kinetis format on an STM32 table: every GPIO name mismatches
    data["pins"][24]["kind"] = "power"            # NRST as power -> warning
    data["pins"][0]["functions"]["XYZ"] = "FOO"  # unknown mux key -> warning
    p = tmp_path / "fmt.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    rep = pin_table_check.check_file(p)
    assert sum("is not part of the pin name" in e for e in rep.errors) == 114
    assert any("NRST" in w and "supply" in w for w in rep.warnings)
    assert any("'XYZ' is not a stm32 mux key" in w for w in rep.warnings)
