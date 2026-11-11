"""Static store scan (firmware.analyze): base + displacement tracking in compiled code (ARM movw/movt/add-derived
bases, C28x XARn-indirect / absolute stores, read-modify-write values in ACC), loop-head invalidation, and the
compiled samples (TI cl2000 C and STM32 HAL) compared with what a run configures."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from chipem.chipdesc import load_chip
from chipem.core.c28x.asm import assemble
from chipem.firmware import FirmwareImage, analyze, format_report, load_firmware, parse_c28img

ROOT = Path(__file__).resolve().parent.parent
C2K_C = ROOT / "firmware" / "samples_c2000_c" / "build"
HAL = ROOT / "firmware" / "samples_stm32_hal" / "build" / "uart_chat_hal.elf"


def _thumb_image(src: str) -> FirmwareImage:
    from engine_util import asm
    code = asm(src, 0x400)
    img = bytearray(0x400 + len(code))
    img[0:8] = struct.pack("<II", 0x20000000, 0x401)
    img[0x400:] = code
    return FirmwareImage([(0, bytes(img))])


def test_arm_long_lived_and_derived_bases():
    # clang -O2 style: a base built once with movw/movt, used 20+ instructions later, and a second base derived from
    # it with add.w (GPIOB = PWR + 0x19400 in the STM32 HAL sample); here PORTA and PORTC = PORTA + 0x2000
    filler = "\n".join("  adds r0, r0, #1" for _ in range(24))
    src = f"""
  push {{r4, r5, r6, r7, lr}}
  movw r7, #0x9000
  movt r7, #0x4004
{filler}
  mov.w r1, #0x100
  str r1, [r7, #0x14]
  add.w r6, r7, #0x2000
{filler}
  ldr r2, [r6, #0x18]
  bic r2, r2, #0x700
  orr r2, r2, #0x300
  str r2, [r6, #0x18]
  pop {{r4, r5, r6, r7, pc}}
"""
    r = analyze(_thumb_image(src), load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml"))
    assert "MUX=ALT1 (GPIO)" in r.pins_configured["PTA5"][0]
    assert any("MUX=ALT3" in n for n in r.pins_configured["PTC6"])
    assert r.store_sites.get("PORTA") == 1 and r.store_sites.get("PORTC") == 1


def test_arm_loop_head_forgets_registers_the_body_redefines():
    # r5 walks PCR7, PCR8, ... in the loop: at the loop head its value is not the one set before the loop
    src = """
  push {r4, r5, lr}
  movw r5, #0xB01C
  movt r5, #0x4004
  movs r1, #0
  mov.w r2, #0x100
loop:
  str r2, [r5]
  adds r5, r5, #4
  adds r1, r1, #1
  cmp r1, #4
  bne loop
  pop {r4, r5, pc}
"""
    r = analyze(_thumb_image(src), load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml"))
    assert "PTC7" not in r.pins_configured and not r.store_sites.get("PORTC")


def test_arm_branch_join_keeps_only_common_facts():
    # two paths reach `join` with different r3: the store after the join has an unknown base
    src = """
  push {r4, lr}
  cmp r0, #0
  beq other
  movw r3, #0x9000
  movt r3, #0x4004
  b join
other:
  movw r3, #0xB000
  movt r3, #0x4004
join:
  mov.w r1, #0x100
  str r1, [r3, #0x14]
  pop {r4, pc}
"""
    r = analyze(_thumb_image(src), load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml"))
    assert not r.pins_configured


C28_SRC = """
        .sect "codestart", 0x33FFF6
        LB      _c_int00
        .sect ".text", 0x330000
_c_int00:
        EALLOW
        MOVL    XAR4,#0x6F88          ; GPAMUX2
        MOVL    ACC,*+XAR4[0]
        OR      @AH,#0x0500           ; GPIO28/29 -> mux 1
        MOVL    *+XAR4[0],ACC
        MOVL    XAR5,#0x6F98          ; GPBMUX2 - 2: *+XAR5[2] = GPBDIR (0x6F9A)
        MOVL    ACC,*+XAR5[2]
        ORB     AL,#0x4               ; GPIO34 output
        MOVL    *+XAR5[2],ACC
        MOVB    AL,#0x8
        MOV     *(0:0x6FCA),@AL       ; GPBSET: GPIO35 (absolute address)
        EDIS
        LCR     sub
        MOVB    AL,#0x1
        MOVL    *+XAR4[0],ACC         ; XAR4 is not preserved across the call: no store predicted
loop:   SB      loop,UNC
sub:    LRETR
"""


def test_c28x_xar_indirect_absolute_and_acc_rmw():
    res = assemble(C28_SRC)
    img = parse_c28img(json.dumps(res.to_c28img()))
    r = analyze(img, load_chip(ROOT / "chips" / "tms320f28232.yaml"))
    pins = r.pins_configured
    assert pins["GPIO28"] == ["GPIO_GPAMUX2=0x1 mux 1 (SCIRXDA)"]
    assert pins["GPIO29"] == ["GPIO_GPAMUX2=0x1 mux 1 (SCITXDA)"]
    assert pins["GPIO34"] == ["GPIO_GPBDIR=0x1 output"]
    assert pins["GPIO35"] == ["GPIO_GPBSET=0x1 set"]
    assert r.store_sites["GPIO"] == 3                      # the store after LCR has no known base
    assert "Peripheral stores" in format_report(r)


@pytest.mark.skipif(not (C2K_C / "uart_chat.out").exists(), reason="cl2000 samples not built")
@pytest.mark.parametrize("name", ["uart_chat.out", "uart_chat_coff.out"])
def test_ti_compiled_uart_chat(name):
    r = analyze(load_firmware(C2K_C / name), load_chip(ROOT / "chips" / "tms320f28232.yaml"))
    pins = r.pins_configured
    assert set(pins) == {"GPIO28", "GPIO29", "GPIO34", "GPIO35"}
    assert any("SCIRXDA" in n for n in pins["GPIO28"]) and any("SCITXDA" in n for n in pins["GPIO29"])
    assert any("output" in n for n in pins["GPIO34"]) and any("toggle" in n for n in pins["GPIO34"])
    assert any("output" in n for n in pins["GPIO35"]) and any("set" in n for n in pins["GPIO35"])


@pytest.mark.skipif(not HAL.exists(), reason="STM32 HAL sample not built")
def test_stm32_hal_gpio_init_arguments():
    r = analyze(load_firmware(HAL), load_chip(ROOT / "chips" / "stm32f405zg.yaml"))
    pins = r.pins_configured
    assert set(pins) == {"PA9", "PA10", "PB0", "PB1"}
    assert "GPIOA_AFRH=0x7 AF7 (USART1_TX) (HAL_GPIO_Init)" in pins["PA9"]
    assert "GPIOA_PUPDR=0x1 pull-up (HAL_GPIO_Init)" in pins["PA10"]
    assert "GPIOB_MODER=0x1 mode output (HAL_GPIO_Init)" in pins["PB0"]
    assert r.store_sites.get("RCC", 0) >= 10              # RCC enables through the movw/movt base kept in r7


@pytest.mark.skipif(not HAL.exists() or not (C2K_C / "uart_chat.out").exists(), reason="samples not built")
@pytest.mark.parametrize("fw,chip", [(str(C2K_C / "uart_chat.out"), "tms320f28232"), (str(HAL), "stm32f405zg")])
def test_pin_report_static_agreement(fw, chip):
    from chipem.pinreport import report_dict
    from chipem.session import Session
    s = Session(chip, fw, quiet=True)
    s.run_for(20e6)
    d = report_dict(s.chip)
    assert d["pins"] and all(p["static"] == "confirmed" for p in d["pins"])


F1 = ROOT / "firmware" / "samples_stm32f1" / "build"


@pytest.mark.skipif(not (F1 / "uart_echo.elf").exists(), reason="STM32F1 samples not built")
def test_stm32f1_crl_crh_static_roles():
    """STM32F1 GPIOx_CRL/CRH (RM0008 9.2.1/9.2.2, Table 20): one MODE/CNF nibble per pin, AF outputs named from the
    pin's default/remap functions; the GPIOA base held in lr (clang) is followed."""
    r = analyze(load_firmware(F1 / "uart_echo.elf"), load_chip(ROOT / "chips" / "stm32f103t6.yaml"))
    pins = r.pins_configured
    assert "GPIOA_CRH=0xb AF push-pull 50 MHz (USART1_TX/TIM1_CH2)".lower() in [n.lower() for n in pins["PA9"]]
    assert any("input floating" in n for n in pins["PA10"])


def test_stm32f1_crl_decoding_thumb():
    """Immediate CRL/BSRR/BRR/ODR stores on the F1 (RM0008 9.2): output, input pull with ODR, AF open-drain."""
    src = """
  movw r0, #0x0800
  movt r0, #0x4001
  movw r1, #0x8f43
  movt r1, #0x4444
  str r1, [r0]
  movs r2, #0x08
  str r2, [r0, #0x0c]
  movs r2, #0x01
  str r2, [r0, #0x10]
  movs r2, #0x02
  str r2, [r0, #0x14]
  b .
"""
    r = analyze(_thumb_image(src), load_chip(ROOT / "chips" / "stm32f103t6.yaml"))
    pins = r.pins_configured
    assert any("output push-pull 50 MHz" in n for n in pins["PA0"])
    assert not any("CRL" in n for n in pins["PA1"])        # full write, PA1 nibble at its reset value 0x4
    assert any("AF open-drain 50 MHz (USART2_TX/TIM2_CH3)" in n for n in pins["PA2"])
    assert any("input pull-up/down" in n for n in pins["PA3"])
    assert any("pull-up as input" in n for n in pins["PA3"])
    assert any(n.endswith("set") for n in pins["PA0"]) and any(n.endswith("reset") for n in pins["PA1"])


@pytest.mark.skipif(not (F1 / "uart_echo.elf").exists(), reason="STM32F1 samples not built")
def test_stm32f1_pin_report_static_agreement():
    from chipem.pinreport import report_dict
    from chipem.session import Session
    s = Session("chips/stm32f103t6.yaml", str(F1 / "uart_echo.elf"), quiet=True)
    s.run_for(5e6)
    d = report_dict(s.chip)
    by = {p["pin"]: p for p in d["pins"]}
    assert by["PA9"]["static"] == "confirmed"
