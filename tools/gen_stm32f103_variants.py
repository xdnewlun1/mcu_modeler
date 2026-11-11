#!/usr/bin/env python3
"""gen_stm32f103_variants: generate the STM32F103 package-variant chip YAMLs from ST's open pin data.

Writes chips/stm32f103{vb,ze}.yaml (medium / high density base files: core, memory map, peripherals, IRQs, pins) and the
package variants that include them (stm32f103{t8,tb,c8,cb,r8,rb,v8}.yaml, stm32f103{rc,re,vc,ve,zc}.yaml) plus the
low-density variants of chips/stm32f103t6.yaml (stm32f103{t4,c4,c6,r4,r6}.yaml; the T6 file itself is hand-written).

Pin tables come from ST's STM32_open_pin_data (cached in svd/dl-open-pin-data-f1/, untrusted data parsed with
xml.etree only; run this tool with `python -I`):
* mcu/<part>.xml gives each package position, the pin name and the list of signals;
* mcu/IP/GPIO-STM32F103x{4,8,C}_gpio_v1_0_Modes.xml says which signals are on a pin only after an AFIO_MAPR remap
  (RemapBlock without DefaultRemap) -> key REMAP; every other peripheral signal -> key AF.
Naming follows the data sheets / chips/stm32f103t6.yaml: ADC1_INn + ADC2_INn -> ADC12_INn (ADC123_INn), TIM2_CH1 +
TIM2_ETR -> TIM2_CH1_ETR, RCC_MCO -> MCO, FSMC_NL -> FSMC_NADV (RM0008 21.3), SYS_* -> debug (JTMS/SWDIO ...) or
additional (WKUP), RTC_TAMPER/RTC_OUT -> additional TAMPER-RTC, USB_DM/DP -> additional USBDM/USBDP, RCC_OSC* and
DAC_OUTx -> analog; PB2 gets additional BOOT1 (RM0008 3.4).  Dropped: ADCx_EXTIn / DAC_EXTI9 (EXTI trigger lines,
not pin functions) and FSMC_DAx (CubeMX names for the multiplexed AD bus, the same pins as FSMC_Dx).  A GPIO with no
function at all gets `additional: EXTIn` (it still is an EXTI line source).  Supply pins of the 36/48/64-pin packages
use the DS15060 Table 5 names (VDD_1 ...; the same pinouts on every F103 density); LQFP100/144 keep ST's VDD/VSS.
Checks run on every table (an assertion stops the tool): every REMAP / AF entry of a remappable signal against the
RM0008 Rev 21 9.3 remap tables (`RM_REMAP`), with the PD0/PD1 CAN remap absent on 36/48/64 pins (Table 34 note 3).

Usage:
    python -I tools/gen_stm32f103_variants.py                  # write the chip YAMLs
    python -I tools/gen_stm32f103_variants.py --check          # compare with the files in chips/ (exit 1 on a diff)
    python -I tools/gen_stm32f103_variants.py --check-ds15060 ds15060.txt   # low-density tables vs DS15060 Table 5
           (ds15060.txt = pdftotext -layout docs/datasheets/st/DS_stm32f103t6.pdf)
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPD = ROOT / "svd" / "dl-open-pin-data-f1"
OUT = ROOT / "chips"


# ----------------------------------------------------------------------------------------------- open pin data
def _strip(tag: str) -> str:
    return tag.split('}', 1)[1] if tag.startswith('{') else tag


def mcu(path) -> tuple[dict, list[dict]]:
    """MCU XML: package info and [{pos, name, type, sigs}] per package pin."""
    root = ET.parse(path).getroot()
    kids = list(root)
    info = {'package': root.get('Package'), 'ref': root.get('RefName'),
            'ram': [e.text for e in kids if _strip(e.tag) == 'Ram'],
            'flash': [e.text for e in kids if _strip(e.tag) == 'Flash'],
            'die': [e.text for e in kids if _strip(e.tag) == 'Die'],
            'ips': {e.get('InstanceName'): e.get('Version') for e in kids if _strip(e.tag) == 'IP'}}
    pins = []
    for e in kids:
        if _strip(e.tag) != 'Pin':
            continue
        sigs = [x.get('Name') for x in e if _strip(x.tag) == 'Signal']
        pins.append({'pos': e.get('Position'), 'name': e.get('Name'), 'type': e.get('Type'), 'sigs': sigs})
    return info, pins


def modes(path) -> dict[str, dict[str, list[tuple[str, bool]]]]:
    """GPIO Modes XML: {pin name: {signal: [(remap block, is the reset mapping)]}}."""
    out: dict = {}
    for e in ET.parse(path).getroot():
        if _strip(e.tag) != 'GPIO_Pin':
            continue
        d: dict = {}
        for ps in e:
            if _strip(ps.tag) != 'PinSignal':
                continue
            blocks = [(rb.get('Name'), rb.get('DefaultRemap') == 'true') for rb in ps if _strip(rb.tag) == 'RemapBlock']
            d.setdefault(ps.get('Name'), []).extend(blocks)
        out[e.get('Name')] = d
    return out


# ----------------------------------------------------------------------------------------------- pin tables
GPIO_RE = re.compile(r'P([A-G])(\d+)')
DROP = re.compile(r'(ADC\d_EXTI\d+|DAC_EXTI\d+|FSMC_DA\d+)$')
RENAME = {'RCC_MCO': 'MCO', 'FSMC_NL': 'FSMC_NADV', 'SYS_TRACECLK': 'TRACECK', 'SYS_TRACED0': 'TRACED0',
          'SYS_TRACED1': 'TRACED1', 'SYS_TRACED2': 'TRACED2', 'SYS_TRACED3': 'TRACED3'}
DEBUG = {'SYS_JTMS-SWDIO': 'JTMS/SWDIO', 'SYS_JTCK-SWCLK': 'JTCK/SWCLK', 'SYS_JTDI': 'JTDI',
         'SYS_JTDO-TRACESWO': 'JTDO/TRACESWO', 'SYS_NJTRST': 'NJTRST'}
ANALOG = {'RCC_OSC_IN': 'OSC_IN', 'RCC_OSC_OUT': 'OSC_OUT', 'RCC_OSC32_IN': 'OSC32_IN', 'RCC_OSC32_OUT': 'OSC32_OUT',
          'DAC_OUT1': 'DAC_OUT1', 'DAC_OUT2': 'DAC_OUT2'}
ADDITIONAL = {'SYS_WKUP': 'WKUP', 'USB_DM': 'USBDM', 'USB_DP': 'USBDP', 'RTC_TAMPER': 'TAMPER-RTC', 'RTC_OUT': 'TAMPER-RTC'}
ORDER = ('debug', 'AF', 'ADC', 'REMAP', 'analog', 'additional')


def modes_by_gpio(md):
    out = {}
    for k, v in md.items():
        m = GPIO_RE.match(k)
        if m:
            d = out.setdefault(m.group(0), {})
            for sig, blocks in v.items():
                d.setdefault(sig, []).extend(blocks)
    return out


def pin_entries(mcu_path, modes_path, drop_signals=()):
    info, pins = mcu(mcu_path)
    md = modes_by_gpio(modes(modes_path))
    rows = []
    for p in pins:
        pos = int(p['pos'])
        nm = p['name']
        m = GPIO_RE.match(nm)
        if p['type'] == 'I/O' and m:
            g = m.group(0)
            port, bit = m.group(1), int(m.group(2))
            fn = {k: [] for k in ORDER}
            sigs = [s for s in p['sigs'] if s != 'GPIO' and not DROP.match(s) and s not in drop_signals]
            # TIM2_CH1 + TIM2_ETR share one pin function (data sheet "TIM2_CH1_ETR")
            if 'TIM2_CH1' in sigs and 'TIM2_ETR' in sigs:
                sigs = [s for s in sigs if s != 'TIM2_ETR']
                sigs = ['TIM2_CH1_ETR' if s == 'TIM2_CH1' else s for s in sigs]
                tim2key = 'TIM2_CH1'
            adc = {}
            for s in sigs:
                a = re.match(r'ADC(\d)_IN(\d+)$', s)
                if a:
                    adc.setdefault(int(a.group(2)), []).append(a.group(1))
                    continue
                if s in DEBUG:
                    fn['debug'].append(DEBUG[s]); continue
                if s in ANALOG:
                    fn['analog'].append(ANALOG[s]); continue
                if s in ADDITIONAL:
                    if ADDITIONAL[s] not in fn['additional']:
                        fn['additional'].append(ADDITIONAL[s])
                    continue
                look = 'TIM2_CH1' if s == 'TIM2_CH1_ETR' else s
                blocks = md.get(g, {}).get(look)
                name = RENAME.get(s, s)
                if blocks and not any(d for _, d in blocks):
                    fn['REMAP'].append(name)
                else:
                    fn['AF'].append(name)
            for ch, units in sorted(adc.items()):
                fn['ADC'].append('ADC' + ''.join(sorted(units)) + f'_IN{ch}')
            if g == 'PB2':
                fn['additional'].append('BOOT1')
            name = g
            # pin name: GPIO name plus the main additional function (data sheet style "PA0/WKUP")
            if fn['analog'] and fn['analog'][0] in ('OSC_IN', 'OSC_OUT'):
                name = f"{fn['analog'][0]}/{g}"
                default = fn['analog'][0]
            else:
                extra = [x for x in fn['analog'] if x.startswith('OSC32')] + [x for x in fn['additional'] if x in ('WKUP', 'TAMPER-RTC', 'BOOT1')]
                if extra:
                    name = f"{g}/{extra[0]}"
                default = fn['debug'][0] if fn['debug'] else 'GPIO'
            if not any(fn.values()):
                # no alternate / additional function at all (e.g. PC10-12 on the low-density LQFP64, PG8, PG15): the pin
                # is still an EXTI line source through AFIO_EXTICR (RM0008 9.4.3-9.4.6, 10.2.5)
                fn['additional'].append(f'EXTI{bit}')
            f = {k: '/'.join(sorted(v, key=_sortkey)) for k, v in fn.items() if v}
            rows.append(dict(num=pos, name=name, kind='gpio', port=port, bit=bit, default=default, functions=f))
        else:
            t = p['type']
            kind = 'power' if t == 'Power' else 'special'
            rows.append(dict(num=pos, name=nm, kind=kind, default=nm))
    rows.sort(key=lambda r: r['num'])
    return info, rows


def _sortkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]


def fmt(r):
    s = f"  - {{num: {r['num']}, name: {r['name']}, kind: {r['kind']}"
    if r['kind'] == 'gpio':
        s += f", port: {r['port']}, bit: {r['bit']}"
    s += f", default: {r['default']}"
    if r.get('functions'):
        s += ', functions: {' + ', '.join(f'{k}: {v}' for k, v in r['functions'].items()) + '}'
    return s + '}'


# ----------------------------------------------------------------------------------------------- RM0008 remaps
# RM0008 Rev 21 9.3 remap tables (non-connectivity-line F10x): Tables 34, 43-46, 52-56 and the AFIO_MAPR bit
# descriptions (9.4.2).  signal -> {mapping value: pin}; value 0 = the reset mapping.
def _m(sigs, *maps):
    out = {}
    for v, pins in maps:
        for s, p in zip(sigs, pins):
            out.setdefault(s, {})[v] = p
    return out
RM_REMAP: dict = {}
RM_REMAP.update(_m(['SPI1_NSS', 'SPI1_SCK', 'SPI1_MISO', 'SPI1_MOSI'], (0, ['PA4', 'PA5', 'PA6', 'PA7']), (1, ['PA15', 'PB3', 'PB4', 'PB5'])))
RM_REMAP.update(_m(['I2C1_SCL', 'I2C1_SDA'], (0, ['PB6', 'PB7']), (1, ['PB8', 'PB9'])))
RM_REMAP.update(_m(['USART1_TX', 'USART1_RX'], (0, ['PA9', 'PA10']), (1, ['PB6', 'PB7'])))
RM_REMAP.update(_m(['USART2_CTS', 'USART2_RTS', 'USART2_TX', 'USART2_RX', 'USART2_CK'], (0, ['PA0', 'PA1', 'PA2', 'PA3', 'PA4']), (1, ['PD3', 'PD4', 'PD5', 'PD6', 'PD7'])))
RM_REMAP.update(_m(['USART3_TX', 'USART3_RX', 'USART3_CK', 'USART3_CTS', 'USART3_RTS'], (0, ['PB10', 'PB11', 'PB12', 'PB13', 'PB14']), (1, ['PC10', 'PC11', 'PC12', 'PB13', 'PB14']), (3, ['PD8', 'PD9', 'PD10', 'PD11', 'PD12'])))
RM_REMAP.update(_m(['TIM1_ETR', 'TIM1_CH1', 'TIM1_CH2', 'TIM1_CH3', 'TIM1_CH4', 'TIM1_BKIN', 'TIM1_CH1N', 'TIM1_CH2N', 'TIM1_CH3N'],
             (0, ['PA12', 'PA8', 'PA9', 'PA10', 'PA11', 'PB12', 'PB13', 'PB14', 'PB15']),
             (1, ['PA12', 'PA8', 'PA9', 'PA10', 'PA11', 'PA6', 'PA7', 'PB0', 'PB1']),
             (3, ['PE7', 'PE9', 'PE11', 'PE13', 'PE14', 'PE15', 'PE8', 'PE10', 'PE12'])))
RM_REMAP.update(_m(['TIM2_CH1_ETR', 'TIM2_CH2', 'TIM2_CH3', 'TIM2_CH4'], (0, ['PA0', 'PA1', 'PA2', 'PA3']), (1, ['PA15', 'PB3', 'PA2', 'PA3']),
             (2, ['PA0', 'PA1', 'PB10', 'PB11']), (3, ['PA15', 'PB3', 'PB10', 'PB11'])))
RM_REMAP.update(_m(['TIM3_CH1', 'TIM3_CH2', 'TIM3_CH3', 'TIM3_CH4'], (0, ['PA6', 'PA7', 'PB0', 'PB1']), (2, ['PB4', 'PB5', 'PB0', 'PB1']), (3, ['PC6', 'PC7', 'PC8', 'PC9'])))
RM_REMAP.update(_m(['TIM4_CH1', 'TIM4_CH2', 'TIM4_CH3', 'TIM4_CH4'], (0, ['PB6', 'PB7', 'PB8', 'PB9']), (1, ['PD12', 'PD13', 'PD14', 'PD15'])))
RM_REMAP.update(_m(['CAN_RX', 'CAN_TX'], (0, ['PA11', 'PA12']), (2, ['PB8', 'PB9']), (3, ['PD0', 'PD1'])))


def check(rows):
    """Problems of a generated pin table against RM: REMAP entries must be a non-reset mapping of the signal, AF
    entries of remappable signals the reset mapping; every RM pin present in the package must carry the signal."""
    probs = []
    have = {}
    for r in rows:
        if r['kind'] != 'gpio':
            continue
        g = f"P{r['port']}{r['bit']}"
        f = r.get('functions') or {}
        for key in ('AF', 'REMAP'):
            for s in filter(None, str(f.get(key, '')).split('/')):
                have.setdefault(s, {})[g] = key
                if s not in RM_REMAP:
                    if key == 'REMAP':
                        probs.append(f'{g}: REMAP {s} is not in the RM0008 remap tables')
                    continue
                pins = RM_REMAP[s]
                if key == 'AF' and pins.get(0) != g:
                    probs.append(f'{g}: AF {s} but RM0008 reset mapping is {pins.get(0)}')
                if key == 'REMAP' and (g not in pins.values() or pins.get(0) == g):
                    probs.append(f'{g}: REMAP {s} but RM0008 maps it to {pins}')
    gpios = {f"P{r['port']}{r['bit']}" for r in rows if r['kind'] == 'gpio'}
    for s, pins in RM_REMAP.items():
        if s not in have:
            continue                      # peripheral not on this part
        for v, p in pins.items():
            if p in gpios and p not in have[s]:
                probs.append(f'{p}: RM0008 maps {s} here (value {v}) but the pin table does not list it')
    return probs


# ----------------------------------------------------------------------------------------------- chip files
C = '7d1f1514'
MODES = {'x4': 'GPIO-STM32F103x4_gpio_v1_0_Modes.xml', 'x8': 'GPIO-STM32F103x8_gpio_v1_0_Modes.xml',
         'xC': 'GPIO-STM32F103xC_gpio_v1_0_Modes.xml'}

# DS15060 Rev 7 Table 5 supply names for the 36/48/64-pin packages (all F103 densities share these pinouts;
# checked: the supply positions of ST's open pin data are identical for the low-, medium- and high-density files)
DS_SUPPLY = {
    'VFQFPN36': {1: 'VDD_3', 18: 'VSS_1', 19: 'VDD_1', 26: 'VSS_2', 27: 'VDD_2', 36: 'VSS_3'},
    'LQFP48': {23: 'VSS_1', 24: 'VDD_1', 35: 'VSS_2', 36: 'VDD_2', 47: 'VSS_3', 48: 'VDD_3'},
    'LQFP64': {18: 'VSS_4', 19: 'VDD_4', 31: 'VSS_1', 32: 'VDD_1', 47: 'VSS_2', 48: 'VDD_2', 63: 'VSS_3', 64: 'VDD_3'},
}


def rows_for(xml, mset):
    info, rows = pin_entries(str(OPD / f'{xml}.xml'), str(OPD / MODES[mset]))
    pkg = info['package']
    sup = DS_SUPPLY.get(pkg, {})
    for r in rows:
        if r['kind'] == 'power' and r['num'] in sup:
            assert r['name'] in ('VDD', 'VSS'), (xml, r)
            assert sup[r['num']].startswith(r['name']), (xml, r)
            r['name'] = r['default'] = sup[r['num']]
        if r['kind'] == 'gpio' and r['default'] == 'JTDO/TRACESWO':
            r['default'] = 'JTDO'
    probs = [p for p in check(rows)
             if not (re.match(r'PD[01]: RM0008 maps CAN_', p) and pkg in DS_SUPPLY)]   # RM0008 Table 34 note 3
    assert not probs, (xml, probs)
    return info, rows


def pins_block(rows):
    return '\n'.join(fmt(r) for r in rows) + '\n'


CORE = "core: {arch: armv7-m, backend: unicorn, cpu: cortex-m3, fpu: false, clock_hz: 8000000, addr_unit_bits: 8, num_irqs: %d}"
COMMON_TOP = """family: stm32f1
regs_file: stm32f103_regs.yaml
gpio_name_format: "P{port}{bit}"
# 8 MHz = HSI after reset; stm32f1.rcc derives HCLK from CFGR/PLL and retunes the clock (72 MHz max with HSE x 9).
%s
# SysTick CLKSOURCE=0 reference: HCLK/8 (RM0008 Figure 8 clock tree "to Cortex System timer /8").
systick_ext_div: 8
# SYST_CALIB: RM0008 10.1.1 "The SysTick calibration value is set to 9000" (TENMS = 9000 HCLK/8 ticks = 1 ms at 72 MHz),
# NOREF = SKEW = 0.
systick_calib: 9000
# BOOT0 = 0: the vector table is fetched through the alias at 0x00000000; CHIP_EM does not map the alias, so VTOR
# starts at the flash base (images are linked at 0x08000000).
vtor: 0x08000000
"""

APB1_MD = """  # APB1
  - {name: TIM2, model: stm32.tim, base: 0x40000000, size: 0x400, params: {bits: 16, reserved_zero: true}}    # 16-bit on the F1
  - {name: TIM3, model: stm32.tim, base: 0x40000400, size: 0x400, params: {reserved_zero: true}}
  - {name: TIM4, model: stm32.tim, base: 0x40000800, size: 0x400, params: {reserved_zero: true}}
"""
APB1_HD_EXTRA = """  - {name: TIM5, model: stm32.tim, base: 0x40000C00, size: 0x400, params: {bits: 16, reserved_zero: true}}    # 16-bit on the F1
  - {name: TIM6, model: stm32.tim, base: 0x40001000, size: 0x400, params: {irq: TIM6, reserved_zero: true}}   # basic timer (DAC trigger)
  - {name: TIM7, model: stm32.tim, base: 0x40001400, size: 0x400, params: {irq: TIM7, reserved_zero: true}}   # basic timer
"""
APB1_MID = """  - {name: RTC, model: stm32f1.rtc, base: 0x40002800, size: 0x400}
  - {name: WWDG, model: stm32.wwdg, base: 0x40002C00, size: 0x400}
  - {name: IWDG, model: stm32.iwdg, base: 0x40003000, size: 0x400, params: {lsi_hz: 40000}}   # LSI 40 kHz
"""
SPI23_MD = """  - {name: SPI2, model: stm32.spi, base: 0x40003800, size: 0x400, params: {i2s: false, reserved_zero: true}}   # no I2S below high density
"""
SPI23_HD = """  - {name: SPI2, model: stm32.spi, base: 0x40003800, size: 0x400, params: {i2s: true, i2s_clock: sysclk, reserved_zero: true}}    # SPI2/I2S2 (RM0008 25.1)
  - {name: SPI3, model: stm32.spi, base: 0x40003C00, size: 0x400, params: {i2s: true, i2s_clock: sysclk, reserved_zero: true}}    # SPI3/I2S3
"""
UART_MD = """  - {name: USART2, model: stm32.usart, base: 0x40004400, size: 0x400, params: {index: 2, kind: usart, reserved_zero: true}}
  - {name: USART3, model: stm32.usart, base: 0x40004800, size: 0x400, params: {index: 3, kind: usart, reserved_zero: true}}
"""
UART_HD = """  - {name: UART4, model: stm32.usart, base: 0x40004C00, size: 0x400, params: {index: 4, kind: uart, reserved_zero: true}}
  - {name: UART5, model: stm32.usart, base: 0x40005000, size: 0x400, params: {index: 5, kind: uart, reserved_zero: true}}
"""
I2C_USB = """  - {name: I2C1, model: stm32.i2c, base: 0x40005400, size: 0x400, params: {reserved_zero: true}}
  - {name: I2C2, model: stm32.i2c, base: 0x40005800, size: 0x400, params: {reserved_zero: true}}
  - {name: USB, model: stm32f1.usb, base: 0x40005C00, size: 0x400}            # USB full-speed device registers
  - {name: USB_CAN_SRAM, model: stm32f1.pma, base: 0x40006000, size: 0x400}   # 512-byte packet memory shared with bxCAN
  - {name: CAN, model: stm32f1.can, base: 0x40006400, size: 0x400}            # bxCAN, 14 filter banks
"""
BKP_PWR = """  - {name: BKP, model: stm32f1.bkp, base: 0x40006C04, size: 0x3FC%s}            # SVD base: DR1 is at 0x40006C04
  - {name: PWR, model: stm32f1.pwr, base: 0x40007000, size: 0x400}
"""
DAC = """  - {name: DAC, model: stm32.dac, base: 0x40007400, size: 0x400, params: {f1: true}}   # 2 x 12-bit (RM0008 ch. 12)
"""
APB2 = """  # APB2
  - {name: AFIO, model: stm32f1.afio, base: 0x40010000, size: 0x400, params: {density: %s}}
  - {name: EXTI, model: stm32f1.exti, base: 0x40010400, size: 0x400}
  - {name: GPIOA, model: stm32f1.gpio, base: 0x40010800, size: 0x400, params: {port: A}}
  - {name: GPIOB, model: stm32f1.gpio, base: 0x40010C00, size: 0x400, params: {port: B}}
  - {name: GPIOC, model: stm32f1.gpio, base: 0x40011000, size: 0x400, params: {port: C}}
  - {name: GPIOD, model: stm32f1.gpio, base: 0x40011400, size: 0x400, params: {port: D}}
  - {name: GPIOE, model: stm32f1.gpio, base: 0x40011800, size: 0x400, params: {port: E}}
"""
APB2_FG = """  - {name: GPIOF, model: stm32f1.gpio, base: 0x40011C00, size: 0x400, params: {port: F}}
  - {name: GPIOG, model: stm32f1.gpio, base: 0x40012000, size: 0x400, params: {port: G}}
"""
APB2_B = """  - {name: ADC1, model: stm32f1.adc, base: 0x40012400, size: 0x400}
  - {name: ADC2, model: stm32f1.adc, base: 0x40012800, size: 0x400}
  - {name: TIM1, model: stm32.tim, base: 0x40012C00, size: 0x400, params: {irq: TIM1_UP, reserved_zero: true}}
  - {name: SPI1, model: stm32.spi, base: 0x40013000, size: 0x400, params: {i2s: false, reserved_zero: true}}
"""
APB2_HD = """  - {name: TIM8, model: stm32.tim, base: 0x40013400, size: 0x400, params: {irq: TIM8_UP, reserved_zero: true}}
"""
APB2_U1 = """  - {name: USART1, model: stm32.usart, base: 0x40013800, size: 0x400, params: {index: 1, kind: usart, reserved_zero: true}}
"""
APB2_ADC3 = """  - {name: ADC3, model: stm32f1.adc, base: 0x40013C00, size: 0x400}
"""
AHB = """  # AHB
%s  - {name: DMA1, model: stm32f1.dma, base: 0x40020000, size: 0x400}           # 7 channels
%s  - {name: RCC, model: stm32f1.rcc, base: 0x40021000, size: 0x400, params: {ext_hz: 8000000}}
  - {name: FLASH, model: stm32f1.flash, base: 0x40022000, size: 0x400, params: {page_size: %d}}   # FPEC; %s
  - {name: CRC, model: stm32.crc, base: 0x40023000, size: 0x400}
%s  # system memory: device electronic signature (F_SIZE, UID) and option bytes
  - {name: SYSMEM, model: stm32f1.sysmem, base: 0x1FFFF000, size: 0x800}
  - {name: OB, model: stm32f1.optbytes, base: 0x1FFFF800, size: 0x400}
  # Cortex-M3 debug MCU
  - {name: DBG, model: stm32f1.dbgmcu, base: 0xE0042000, size: 0x400, params: {density: %s}}   # %s
"""
SDIO = """  - {name: SDIO, model: stm32.sdio, base: 0x40018000, size: 0x400, params: {f1: true}}   # SDIOCLK = HCLK (RM0008 22.2)
"""
DMA2 = """  - {name: DMA2, model: stm32f1.dma, base: 0x40020400, size: 0x400}           # 5 channels (RM0008 13.3.7 Table 79)
"""
FSMC = """  # FSMC control registers (bank windows 0x60000000-0x9FFFFFFF)
  - {name: FSMC, model: stm32.fsmc, base: 0xA0000000, size: 0x1000}
"""


def periph(density):
    hd = density == 'high'
    s = "peripherals:\n" + APB1_MD + (APB1_HD_EXTRA if hd else '') + APB1_MID
    s += SPI23_HD if hd else SPI23_MD
    s += UART_MD + (UART_HD if hd else '') + I2C_USB
    s += BKP_PWR % (', params: {data_regs: 42}' if hd else '')
    s += DAC if hd else ''
    s += APB2 % density + (APB2_FG if hd else '') + APB2_B + (APB2_HD if hd else '') + APB2_U1 + (APB2_ADC3 if hd else '')
    if hd:
        s += AHB % (SDIO, DMA2, 2048, 'high density: 2 KB pages (RM0008 3.3.3 Table 6)', FSMC, 'high',
                    'DEV_ID 0x414 high density, REV_ID 0x1003 (RM0008 31.6.1)')
    else:
        s += AHB % ('', '', 1024, 'medium density: 1 KB pages (RM0008 3.3.3 Table 5)', '', 'medium',
                    'DEV_ID 0x410 medium density, REV_ID 0x2003 (RM0008 31.6.1)')
    return s


OMIT_XL = """  - {name: TIM9, reason: "XL density only"}
  - {name: TIM10, reason: "XL density only"}
  - {name: TIM11, reason: "XL density only"}
  - {name: TIM12, reason: "XL density only"}
  - {name: TIM13, reason: "XL density only"}
  - {name: TIM14, reason: "XL density only"}
"""
OMIT_MD = """# SVD peripherals of the STM32F103xx superset that the medium-density die does not have (RM0008 3.3 Figure 1 and
# Table 3 "medium-density" register boundary addresses; ST open pin data IP list of STM32F103V(8-B)Tx.xml).
omitted:
  - {name: TIM5, reason: "high density only"}
  - {name: TIM6, reason: "high density only"}
  - {name: TIM7, reason: "high density only"}
  - {name: TIM8, reason: "high density only"}
""" + OMIT_XL + """  - {name: SPI3, reason: "high density only"}
  - {name: UART4, reason: "high density only"}
  - {name: UART5, reason: "high density only"}
  - {name: DAC, reason: "high density only"}
  - {name: GPIOF, reason: "high density only"}
  - {name: GPIOG, reason: "high density only"}
  - {name: ADC3, reason: "high density only"}
  - {name: SDIO, reason: "high density only"}
  - {name: DMA2, reason: "high density only (medium density: 7-channel DMA1)"}
  - {name: FSMC, reason: "high density only"}
"""
OMIT_HD = """# SVD peripherals of the STM32F103xx superset that the high-density die does not have (XL density only).
omitted:
""" + OMIT_XL

IRQ_LD = [(0, 'WWDG'), (1, 'PVD'), (2, 'TAMPER'), (3, 'RTC'), (4, 'FLASH'), (5, 'RCC'), (6, 'EXTI0'), (7, 'EXTI1'),
          (8, 'EXTI2'), (9, 'EXTI3'), (10, 'EXTI4')] + [(10 + c, f'DMA1_Channel{c}') for c in range(1, 8)] + [
          (18, 'ADC1_2'), (19, 'USB_HP_CAN1_TX'), (20, 'USB_LP_CAN1_RX0'), (21, 'CAN1_RX1'), (22, 'CAN1_SCE'),
          (23, 'EXTI9_5'), (24, 'TIM1_BRK'), (25, 'TIM1_UP'), (26, 'TIM1_TRG_COM'), (27, 'TIM1_CC'), (28, 'TIM2'),
          (29, 'TIM3'), (30, 'TIM4'), (31, 'I2C1_EV'), (32, 'I2C1_ER'), (33, 'I2C2_EV'), (34, 'I2C2_ER'), (35, 'SPI1'),
          (36, 'SPI2'), (37, 'USART1'), (38, 'USART2'), (39, 'USART3'), (40, 'EXTI15_10'), (41, 'RTC_Alarm'),
          (42, 'USBWakeUp')]
IRQ_HD = [(43, 'TIM8_BRK'), (44, 'TIM8_UP'), (45, 'TIM8_TRG_COM'), (46, 'TIM8_CC'), (47, 'ADC3'), (48, 'FSMC'),
          (49, 'SDIO'), (50, 'TIM5'), (51, 'SPI3'), (52, 'UART4'), (53, 'UART5'), (54, 'TIM6'), (55, 'TIM7'),
          (56, 'DMA2_Channel1'), (57, 'DMA2_Channel2'), (58, 'DMA2_Channel3'), (59, 'DMA2_Channel4_5')]
SVD_NAMES = "{18: ADC, 19: CAN1_TX, 20: CAN1_RX0, 24: TIM1_BRK_TIM9, 25: TIM1_UP_TIM10, 26: TIM1_TRG_COM_TIM11,\n                41: RTCAlarm, 42: USB_FS_WKUP%s}"


def irqs(density):
    hd = density == 'high'
    lst = IRQ_LD + (IRQ_HD if hd else [])
    s = ("# External interrupt numbers (exception = 16 + n): RM0008 10.1.2 Table 63 \"Vector table for other STM32F10xxx\n"
         "# devices\" with the CMSIS names (USBWakeUp: CMSIS spelling of RM0008's USBWakeup); %s.  The SVD agrees on every\n"
         "# number; where it uses other names they are listed in `svd_irq_names` (svd2chip --check accepts those as renames).\n"
         % ('high density: all 60 vectors' if hd else 'medium density: vectors 0-42 (43 vectors, Table 63 \"other\" devices without 43-59)'))
    s += "irqs:\n" + ''.join(f"  {n}: {nm}\n" for n, nm in lst)
    s += "svd_irq_names: " + SVD_NAMES % (",\n                43: TIM8_BRK_TIM12, 44: TIM8_UP_TIM13, 45: TIM8_TRG_COM_TIM14" if hd else '') + "\n"
    return s


def gpio_count(rows):
    return sum(r['kind'] == 'gpio' for r in rows)


def pin_src(xml, modes):
    return (f"# Pin table generated from ST's STM32_open_pin_data @ {C} (mcu/{xml}.xml positions and signals, mcu/IP/{MODES[modes]}\n"
            f"# default / remap classification; svd/dl-open-pin-data-f1/) and checked against the RM0008 9.3 remap tables (Tables 34,\n"
            f"# 43-46, 52-56); see chips/README.md \"STM32F103 medium / high density\".")


OUTPUT: dict[str, str] = {}


def write(name, text):
    OUTPUT[name] = text


def base_file(fname, part, pkg, xml, modes, density, flash, ram, header):
    info, rows = rows_for(xml, modes)
    assert info['package'] == pkg
    ng = gpio_count(rows)
    txt = header + "\n" + pin_src(xml, modes) + "\n"
    txt += f"name: {part}\npackage: {pkg}\n" + COMMON_TOP % (CORE % (60 if density == 'high' else 43))
    txt += "\nmemory:\n"
    txt += f"  - {{name: FLASH, base: 0x08000000, size: 0x{flash:X}, kind: flash}}     # {flash // 1024} KB main flash\n"
    txt += f"  - {{name: SRAM, base: 0x20000000, size: 0x{ram:X}, kind: ram}}        # {ram // 1024} KB\n"
    txt += ("  # System memory 0x1FFFF000 (2 KB, ST bootloader; F_SIZE at 0x1FFFF7E0, unique ID at 0x1FFFF7E8) and option\n"
            "  # bytes 0x1FFFF800 are MMIO blocks (SYSMEM, OB) below, not plain memory.\n")
    txt += "\n# armv7m.scs (0xE000E000, SysTick/NVIC/SCB) is created by the arch layer, not listed here.\n"
    txt += periph(density) + "\n"
    txt += (OMIT_HD if density == 'high' else OMIT_MD) + "\n" + irqs(density) + "\n"
    txt += f"# All {len(rows)} package pins ({ng} GPIOs).\npins:\n" + pins_block(rows)
    write(fname, txt)
    return rows


def variant_file(fname, part, pkg, base, xml, modes, flash, ram, header, pins=True):
    txt = header + "\n"
    if pins:
        info, rows = rows_for(xml, modes)
        assert info['package'] == pkg, (info['package'], pkg)
        txt += pin_src(xml, modes) + "\n"
    txt += f"include: {base}\nname: {part}\npackage: {pkg}\nregs_file: stm32f103_regs.yaml\n\nmemory:\n"
    txt += f"  - {{name: FLASH, base: 0x08000000, size: 0x{flash:X}, kind: flash}}     # {flash // 1024} KB main flash\n"
    txt += f"  - {{name: SRAM, base: 0x20000000, size: 0x{ram:X}, kind: ram}}        # {ram // 1024} KB\n"
    if pins:
        txt += f"\n# All {len(rows)} package pins ({gpio_count(rows)} GPIOs).\npins:\n" + pins_block(rows)
        return rows
    write(fname, txt)


K = 1024
PKG_LETTER = {'T': ('VFQFPN36', 'U'), 'C': ('LQFP48', 'T'), 'R': ('LQFP64', 'T'), 'V': ('LQFP100', 'T'), 'Z': ('LQFP144', 'T')}


def hdr(part, pkg, flash, ram, die, extra=''):
    if 'low' in die:
        ds = "Data sheet: DS15060 Rev 7 (STM32F103x4/x6, docs/datasheets/st/DS_stm32f103t6.pdf)."
    else:
        doc = 'DS5319 (STM32F103x8/xB)' if 'medium' in die else 'DS5792 (STM32F103xC/xD/xE)'
        ds = f"The data sheet {doc} could not be downloaded (st.com: HTTP/2 stream error); not used."
    return (f"# Generated by tools/gen_stm32f103_variants.py -- edit the generator, not this file.\n"
            f"# STMicroelectronics {part} (Cortex-M3, 72 MHz max, {flash // K} KB flash, {ram // K} KB SRAM, {pkg}; {die}).\n"
            f"# Part number: STM32 F 103 {part[9]} = {pkg}, {part[10]} = {flash // K} KB flash (flash / RAM sizes: ST's open pin data\n"
            f"# <Flash>/<Ram> entries).  {ds}\n" + extra)


def generate() -> dict[str, str]:
    files = {}
    # ---------------------------------------------------------------- medium density (DIE410), base LQFP100 VB
    md = ("# Medium-density performance line, DEV_ID 0x410 (RM0008 31.6.1).  Base file of the medium-density F103 parts:\n"
          "# the package variants (stm32f103{t8,tb,c8,cb,r8,rb,v8}.yaml) include it and override name, package, memory, pins.\n"
          "# Memory map, register boundary addresses and IRQs: RM0008 Rev 21 3.3 (Table 3), 10.1.2 Table 63; CMSIS-SVD\n"
          "# STM32F103xx.svd (cmsis-svd-data @ c65f8551, chips/stm32f103_regs.yaml) for bases and register layouts.\n")
    base_file('stm32f103vb', 'STM32F103VB', 'LQFP100', 'STM32F103V(8-B)Tx', 'x8', 'medium', 128 * K, 20 * K,
              hdr('STM32F103VB', 'LQFP100', 128 * K, 20 * K, 'medium-density performance line') + md)
    for part, flash in (('T8', 64), ('TB', 128), ('C8', 64), ('CB', 128), ('R8', 64), ('RB', 128), ('V8', 64)):
        pkg = PKG_LETTER[part[0]][0]
        xml = f"STM32F103{part[0]}(8-B){PKG_LETTER[part[0]][1]}x"
        h = hdr('STM32F103' + part, pkg, flash * K, 20 * K, 'medium-density performance line')
        h += ("# Same die (DEV_ID 0x410) as chips/stm32f103vb.yaml, which is included below (core, memory map, peripherals,\n"
              "# IRQs); this file overrides name, package, memory and pins.  Blocks without pins on this package stay on the die.\n")
        if part[0] == 'T':
            h += ("# VFQFPN36: SPI2, I2C2 and USART3 have no pins on this package (ST open pin data IP list: 1 SPI, 1 I2C, 2 USARTs);\n"
                  "# their registers remain (same die).\n")
        if part == 'C8':
            h += "# The \"Blue Pill\" board part (LED on PC13, 8 MHz HSE crystal, USB on PA11/PA12).\n"
        rows = variant_file(None, 'STM32F103' + part, pkg, 'stm32f103vb.yaml', xml, 'x8', flash * K, 20 * K, h)
        files['stm32f103' + part.lower()] = (h, rows, part, pkg, xml, 'x8', flash * K, 20 * K, 'stm32f103vb.yaml')
    # ---------------------------------------------------------------- high density (DIE414), base LQFP144 ZE
    hd = ("# High-density performance line, DEV_ID 0x414 (RM0008 31.6.1).  Base file of the high-density F103 parts: the\n"
          "# package variants (stm32f103{rc,re,vc,ve,zc}.yaml) include it and override name, package, memory, pins.\n"
          "# Memory map, register boundary addresses and IRQs: RM0008 Rev 21 3.3 (Table 3), 10.1.2 Table 63 (60 vectors);\n"
          "# CMSIS-SVD STM32F103xx.svd (cmsis-svd-data @ c65f8551, chips/stm32f103_regs.yaml) for bases and register layouts.\n"
          "# Adds to medium density: TIM5-8, SPI3 (SPI2/SPI3 with I2S), UART4/5, DAC, GPIOF/G, ADC3, SDIO, DMA2, FSMC,\n"
          "# BKP DR11-DR42 (RM0008 6.4.5, Table 17), 2 KB flash pages.\n")
    base_file('stm32f103ze', 'STM32F103ZE', 'LQFP144', 'STM32F103Z(C-D-E)Tx', 'xC', 'high', 512 * K, 64 * K,
              hdr('STM32F103ZE', 'LQFP144', 512 * K, 64 * K, 'high-density performance line') + hd)
    for part, flash, ram in (('RC', 256, 48), ('RE', 512, 64), ('VC', 256, 48), ('VE', 512, 64), ('ZC', 256, 48)):
        pkg = PKG_LETTER[part[0]][0]
        xml = f"STM32F103{part[0]}(C-D-E)Tx"
        h = hdr('STM32F103' + part, pkg, flash * K, ram * K, 'high-density performance line')
        h += ("# Same die (DEV_ID 0x414) as chips/stm32f103ze.yaml, which is included below (core, memory map, peripherals,\n"
              "# IRQs); this file overrides name, package, memory and pins.\n")
        if part[0] == 'R':
            h += ("# LQFP64: no FSMC pins (ST open pin data lists no FSMC IP for STM32F103R(C-D-E)Tx); the FSMC registers stay\n"
                  "# (same die) but no external memory can be wired.  Ports E-G have no pins.\n")
        if part[0] == 'V':
            h += "# LQFP100: FSMC NOR/PSRAM bank 1 (NE1) and NAND bank 2 only (no ports F/G: no A0-A15 on PF/PG, no NE2-NE4).\n"
        rows = variant_file(None, 'STM32F103' + part, pkg, 'stm32f103ze.yaml', xml, 'xC', flash * K, ram * K, h)
        files['stm32f103' + part.lower()] = (h, rows, part, pkg, xml, 'xC', flash * K, ram * K, 'stm32f103ze.yaml')
    # ---------------------------------------------------------------- low density (DIE412), base stm32f103t6.yaml
    for part, flash, ram in (('T4', 16, 6), ('C4', 16, 6), ('C6', 32, 10), ('R4', 16, 6), ('R6', 32, 10)):
        pkg = PKG_LETTER[part[0]][0]
        xml = f"STM32F103{part[0]}(4-6){PKG_LETTER[part[0]][1]}x"
        h = hdr('STM32F103' + part, pkg, flash * K, ram * K, 'low-density performance line')
        h += ("# Same die (DEV_ID 0x412) as chips/stm32f103t6.yaml, which is included below (core, memory map, peripherals,\n"
              "# IRQs); this file overrides name, package, memory" + (" and pins" if part[0] != 'T' else "") + ".\n")
        if part[0] == 'T':
            h += "# Same VFQFPN36 package as the T6: the pin table is inherited unchanged.\n"
            variant_file('stm32f103' + part.lower(), 'STM32F103' + part, pkg, 'stm32f103t6.yaml', xml, 'x4', flash * K, ram * K, h, pins=False)
            continue
        h += ("# Pin table cross-checked against DS15060 Rev 7 Table 5 (" + pkg + " column): same positions, same signals (the\n"
              "# data sheet omits SPI1_SCK on PB3, see chips/stm32f103t6.yaml).\n")
        rows = variant_file(None, 'STM32F103' + part, pkg, 'stm32f103t6.yaml', xml, 'x4', flash * K, ram * K, h)
        files['stm32f103' + part.lower()] = (h, rows, part, pkg, xml, 'x4', flash * K, ram * K, 'stm32f103t6.yaml')
    for fn, (h, rows, part, pkg, xml, modes, flash, ram, base) in files.items():
        txt = h + "\n" + pin_src(xml, modes) + "\n"
        txt += f"include: {base}\nname: STM32F103{part}\npackage: {pkg}\nregs_file: stm32f103_regs.yaml\n\nmemory:\n"
        txt += f"  - {{name: FLASH, base: 0x08000000, size: 0x{flash:X}, kind: flash}}     # {flash // 1024} KB main flash\n"
        txt += f"  - {{name: SRAM, base: 0x20000000, size: 0x{ram:X}, kind: ram}}        # {ram // 1024} KB\n"
        txt += f"\n# All {len(rows)} package pins ({gpio_count(rows)} GPIOs).\npins:\n" + pins_block(rows)
        write(fn, txt)
    return OUTPUT


# ----------------------------------------------------------------------------------------------- DS15060 check
DS_ANCHOR = re.compile(r'^\s*(\d+|-)\s+(\d+|-)\s+([A-H]\d|-)\s+(\d+|-)\s+(\S+)\s+(I/O|S|I|O)\b')
DS_TOK = re.compile(r'[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|WKUP|MCO|USBD[MP]|TAMPER-RTC|BOOT1|TRACESWO|NJTRST|JTDI|JTDO')


def parse_ds15060(path) -> dict[str, list[tuple[str, str, str, set]]]:
    """DS15060 Rev 7 Table 5 (pdftotext -layout): {pin name: [(LQFP48, LQFP64, VFQFPN36 position, signal tokens)]}.
    Wrapped cells are attached to the nearest row (tokens are therefore compared with a +-1 row tolerance)."""
    lines = Path(path).read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if 'Table 5. Low-density STM32F103xx pin definitions' in ln and i > 1000)
    end = next(i for i in range(start, len(lines)) if lines[i].startswith('1. I = input'))
    seg = lines[start:end]
    anchors = [i for i, ln in enumerate(seg) if DS_ANCHOR.match(ln)]
    rows: dict[int, list[str]] = {i: [] for i in anchors}
    for i, ln in enumerate(seg):
        if 'Table 5' in ln or 'DocID' in ln or 'Pinouts' in ln or 'STM32F103x4' in ln:
            continue
        near = min(anchors, key=lambda a: (abs(a - i), a < i))
        if abs(near - i) <= 3:
            rows[near].append(ln)
    out: dict = {}
    for a in anchors:
        m = DS_ANCHOR.match(seg[a])
        toks = set(DS_TOK.findall(' '.join(x[60:] for x in rows[a])))
        out.setdefault(m.group(5), []).append((m.group(1), m.group(2), m.group(4), toks))
    return out


def check_ds15060(path) -> list[str]:
    """Low-density LQFP48 / LQFP64 / VFQFPN36 tables against DS15060 Table 5: positions and signal names."""
    ds = parse_ds15060(path)
    order = list(ds)

    def near(name):
        i = order.index(name)
        s: set = set()
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(order):
                for t in ds[order[j]]:
                    s |= t[3]
        return s

    probs = []
    for col, xml in ((0, 'STM32F103C(4-6)Tx'), (1, 'STM32F103R(4-6)Tx'), (2, 'STM32F103T(4-6)Ux')):
        info, rows = rows_for(xml, 'x4')
        pos = {int(e[col]): name for name, ents in ds.items() for e in ents if e[col] != '-'}
        for r in rows:
            dn = pos.get(r['num'])
            if dn is None:          # PC13-15: the pin name is wrapped over several lines (not anchored)
                if not r['name'].startswith(('PC13', 'PC14', 'PC15')):
                    probs.append(f"{xml} pin {r['num']}: not found in DS15060 Table 5")
                continue
            base = re.sub(r'\(\d\)', '', dn)
            g = f"P{r['port']}{r['bit']}" if r['kind'] == 'gpio' else r['name']
            if not (base == g or base.startswith(g + '-') or base == r['name'] or base in r['name'].split('/')):
                probs.append(f"{xml} pin {r['num']}: DS15060 {dn}, table {r['name']}")
            if r['kind'] != 'gpio':
                continue
            fn = set()
            for k, v in r['functions'].items():
                if k != 'debug':
                    fn |= set(v.split('/'))
            for t in sorted(t for t in fn - near(dn) - {'OSC_IN', 'OSC_OUT', 'OSC32_IN'} if not t.startswith('EXTI')):
                if not (g == 'PB3' and t == 'SPI1_SCK'):          # DS15060 omits it (see chips/stm32f103t6.yaml)
                    probs.append(f"{xml} {g}: {t} not in DS15060 Table 5")
            own = set().union(*[e[3] for e in ds[dn]])
            for t in sorted(own - fn - {'JTDI', 'JTDO', 'NJTRST', 'TRACESWO', 'OSC_IN', 'OSC_OUT'}):
                if not any(t in rr.get('functions', {}).get(k, '') for rr in rows if abs(rr['num'] - r['num']) <= 1
                           for k in ('AF', 'REMAP', 'ADC', 'additional', 'analog')):
                    probs.append(f"{xml} {g}: DS15060 lists {t}, the table does not")
    return probs


def check_t6() -> list[str]:
    """The hand-written chips/stm32f103t6.yaml (DS15060) against the generated VFQFPN36 low-density table: same pin
    names, kinds and per-key signal sets (the order inside a key may differ)."""
    import yaml
    t6 = {p['num']: p for p in yaml.safe_load((OUT / 'stm32f103t6.yaml').read_text())['pins']}
    info, rows = rows_for('STM32F103T(4-6)Ux', 'x4')
    probs = []
    for r in rows:
        p = t6[r['num']]
        if (p['name'], p['kind'], p.get('default')) != (r['name'], r['kind'], r['default']):
            probs.append(f"pin {r['num']}: t6 {p['name']}/{p['kind']}/{p.get('default')}, generated "
                         f"{r['name']}/{r['kind']}/{r['default']}")
        a = {k: set(str(v).split('/')) for k, v in (p.get('functions') or {}).items()}
        b = {k: set(str(v).split('/')) for k, v in (r.get('functions') or {}).items()}
        if a != b:
            probs.append(f"pin {r['num']} {p['name']}: t6 {a} != generated {b}")
    return probs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="compare with chips/*.yaml instead of writing")
    ap.add_argument("--check-ds15060", metavar="TXT", help="pdftotext -layout output of DS15060 (low-density check)")
    a = ap.parse_args(argv)
    if a.check_ds15060:
        probs = check_ds15060(a.check_ds15060)
        print("\n".join(probs) or "DS15060 Table 5: 0 differences (LQFP48, LQFP64, VFQFPN36)")
        return 1 if probs else 0
    out = generate()
    bad = check_t6()
    for b in bad:
        print("T6:", b)
    for name, text in out.items():
        path = OUT / f"{name}.yaml"
        if a.check:
            if not path.exists() or path.read_text() != text:
                print(f"DIFF {path}")
                bad.append(name)
        else:
            path.write_text(text)
    print(f"{len(out)} chip files {'checked' if a.check else 'written'}, {len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
