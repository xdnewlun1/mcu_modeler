"""Generate CHIP_EM stm32f1 pin tables for the STM32F105/F107 connectivity line from ST open pin data
(MCU XML + GPIO-STM32F107_gpio_v1_0_Modes.xml). Untrusted XML: xml.etree only.

usage: python -I tools/opd_stm32f1_cl_pins.py <mcu.xml> <modes.xml>      # prints the `pins:` rows
       python -I tools/opd_stm32f1_cl_pins.py <modes.xml> --remaps        # remap blocks (cross-check of cl.py REMAP_CL)
"""
import re
import sys
import xml.etree.ElementTree as ET

GPIO_RE = re.compile(r'P([A-G])(\d+)')
DROP = re.compile(r'(ADC\d_EXTI\d+|DAC_EXTI\d+)$')
RENAME = {'RCC_MCO': 'MCO', 'SYS_TRACECLK': 'TRACECK', 'SYS_TRACED0': 'TRACED0', 'SYS_TRACED1': 'TRACED1',
          'SYS_TRACED2': 'TRACED2', 'SYS_TRACED3': 'TRACED3', 'USB_OTG_FS_ID': 'OTG_FS_ID',
          'USB_OTG_FS_SOF': 'OTG_FS_SOF',
          # Ethernet: RM0008 29.3 / Table 58 signal names, MII and RMII variants of a pin both listed
          'ETH_COL': 'ETH_MII_COL', 'ETH_CRS': 'ETH_MII_CRS', 'ETH_CRS_DV': 'ETH_RMII_CRS_DV',
          'ETH_REF_CLK': 'ETH_RMII_REF_CLK', 'ETH_RX_CLK': 'ETH_MII_RX_CLK', 'ETH_RX_DV': 'ETH_MII_RX_DV',
          'ETH_RX_ER': 'ETH_MII_RX_ER', 'ETH_TX_CLK': 'ETH_MII_TX_CLK', 'ETH_RXD2': 'ETH_MII_RXD2',
          'ETH_RXD3': 'ETH_MII_RXD3', 'ETH_TXD2': 'ETH_MII_TXD2', 'ETH_TXD3': 'ETH_MII_TXD3'}
BOTH = {'ETH_RXD0', 'ETH_RXD1', 'ETH_TXD0', 'ETH_TXD1', 'ETH_TX_EN'}     # MII and RMII share the pin and the name
DEBUG = {'SYS_JTMS-SWDIO': 'JTMS/SWDIO', 'SYS_JTCK-SWCLK': 'JTCK/SWCLK', 'SYS_JTDI': 'JTDI',
         'SYS_JTDO': 'JTDO/TRACESWO', 'SYS_TRACESWO': 'JTDO/TRACESWO', 'SYS_NJTRST': 'NJTRST'}
ANALOG = {'RCC_OSC_IN': 'OSC_IN', 'RCC_OSC_OUT': 'OSC_OUT', 'RCC_OSC32_IN': 'OSC32_IN',
          'RCC_OSC32_OUT': 'OSC32_OUT', 'DAC_OUT1': 'DAC_OUT1', 'DAC_OUT2': 'DAC_OUT2'}
ADDITIONAL = {'SYS_WKUP': 'WKUP', 'USB_OTG_FS_DM': 'OTG_FS_DM', 'USB_OTG_FS_DP': 'OTG_FS_DP',
              'USB_OTG_FS_VBUS': 'OTG_FS_VBUS', 'RTC_TAMPER': 'TAMPER-RTC', 'RTC_OUT': 'TAMPER-RTC'}
ORDER = ('debug', 'AF', 'ADC', 'REMAP', 'analog', 'additional')


def strip(t):
    return t.split('}', 1)[1] if t.startswith('{') else t


def mcu(path):
    root = ET.parse(path).getroot()
    info = {'package': root.get('Package'), 'ref': root.get('RefName'),
            'ips': [e.get('InstanceName') for e in root if strip(e.tag) == 'IP']}
    pins = []
    for e in root:
        if strip(e.tag) != 'Pin':
            continue
        pins.append({'pos': int(e.get('Position')), 'name': e.get('Name'), 'type': e.get('Type'),
                     'sigs': [s.get('Name') for s in e if strip(s.tag) == 'Signal']})
    return info, pins


def modes(path):
    """{gpio: {signal: [(remap block, default?), ...]}}"""
    root = ET.parse(path).getroot()
    out = {}
    for e in root:
        if strip(e.tag) != 'GPIO_Pin':
            continue
        d = {}
        for ps in e:
            if strip(ps.tag) != 'PinSignal':
                continue
            d.setdefault(ps.get('Name'), []).extend(
                (rb.get('Name'), rb.get('DefaultRemap') == 'true') for rb in ps if strip(rb.tag) == 'RemapBlock')
        m = GPIO_RE.match(e.get('Name') or '')
        if m:                       # "PD0" and "PD0-OSC_IN" are separate entries: merge them
            for k, v in d.items():
                out.setdefault(m.group(0), {}).setdefault(k, []).extend(v)
    return out


def _sortkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]


def pin_rows(mcu_path, modes_path):
    info, pins = mcu(mcu_path)
    md = modes(modes_path)
    rows = []
    for p in pins:
        m = GPIO_RE.match(p['name'])
        if p['type'] == 'I/O' and m:
            g = m.group(0)
            fn = {k: [] for k in ORDER}
            sigs = [s for s in p['sigs'] if s != 'GPIO' and not DROP.match(s)]
            merged = {}
            for t in ('TIM2', ):
                if f'{t}_CH1' in sigs and f'{t}_ETR' in sigs:
                    sigs = [s for s in sigs if s != f'{t}_ETR']
                    merged[f'{t}_CH1_ETR'] = f'{t}_CH1'
                    sigs = [f'{t}_CH1_ETR' if s == f'{t}_CH1' else s for s in sigs]
            adc = {}
            for s in sigs:
                a = re.match(r'ADC(\d)_IN(\d+)$', s)
                if a:
                    adc.setdefault(int(a.group(2)), []).append(a.group(1))
                    continue
                if s in DEBUG:
                    if DEBUG[s] not in fn['debug']:
                        fn['debug'].append(DEBUG[s])
                    continue
                if s in ANALOG:
                    fn['analog'].append(ANALOG[s])
                    continue
                if s in ADDITIONAL:
                    if ADDITIONAL[s] not in fn['additional']:
                        fn['additional'].append(ADDITIONAL[s])
                    continue
                look = merged.get(s, s)
                blocks = md.get(g, {}).get(look)
                if s in BOTH:
                    names = ['ETH_MII_' + s[4:], 'ETH_RMII_' + s[4:]]
                else:
                    names = [RENAME.get(s, s)]
                key = 'REMAP' if (blocks and not any(d for _, d in blocks)) else 'AF'
                if s == 'ETH_PPS_OUT':      # RM0008 9.4.2 AFIO_MAPR bit 30 PTP_PPS_REMAP enables PPS on PB5
                    key = 'REMAP'
                for n in names:
                    if n not in fn[key]:
                        fn[key].append(n)
            for ch, units in sorted(adc.items()):
                fn['ADC'].append('ADC' + ''.join(sorted(units)) + f'_IN{ch}')
            if g == 'PB2':
                fn['additional'].append('BOOT1')
            name = g
            if fn['analog'] and fn['analog'][0] in ('OSC_IN', 'OSC_OUT'):
                name = f"{fn['analog'][0]}/{g}"
                default = fn['analog'][0]
            else:
                extra = ([x for x in fn['analog'] if x.startswith('OSC32')]
                         + [x for x in fn['additional'] if x in ('WKUP', 'TAMPER-RTC', 'BOOT1')])
                if extra:
                    name = f"{g}/{extra[0]}"
                default = fn['debug'][0] if fn['debug'] else 'GPIO'
            f = {k: '/'.join(sorted(v, key=_sortkey)) for k, v in fn.items() if v}
            rows.append(dict(num=p['pos'], name=name, kind='gpio', port=m.group(1), bit=int(m.group(2)),
                             default=default, functions=f))
        else:
            t = p['type']
            nm = p['name']
            if t == 'Power':
                kind = 'power'
            elif nm in ('OSC_IN', 'OSC_OUT'):
                kind = 'analog'
            else:
                kind = 'special'
            rows.append(dict(num=p['pos'], name=nm, kind=kind, default=nm))
    rows.sort(key=lambda r: r['num'])
    return info, rows


def remap_table(modes_path):
    """{block: {value: {signal: pin}}} from the GPIO modes XML."""
    md = modes(modes_path)
    out = {}
    for g, d in md.items():
        for sig, blocks in d.items():
            for b, _ in blocks:
                mm = re.match(r'(.*)_REMAP(\d)$', b)
                out.setdefault(mm.group(1), {}).setdefault(int(mm.group(2)), {})[sig] = g
    return out


def fmt(r):
    s = f"  - {{num: {r['num']}, name: {r['name']}, kind: {r['kind']}"
    if r['kind'] == 'gpio':
        s += f", port: {r['port']}, bit: {r['bit']}"
    s += f", default: {r['default']}"
    if r.get('functions'):
        s += ', functions: {' + ', '.join(f'{k}: {v}' for k, v in r['functions'].items()) + '}'
    return s + '}'


if __name__ == '__main__':
    if '--remaps' in sys.argv:
        for blk, vals in sorted(remap_table(sys.argv[1]).items()):
            for v, sm in sorted(vals.items()):
                print(blk, v, ' '.join(f'{s}={p}' for s, p in sorted(sm.items())))
        sys.exit(0)
    info, rows = pin_rows(sys.argv[1], sys.argv[2])
    print('#', info['ref'], info['package'], len(rows), 'pins', sum(r['kind'] == 'gpio' for r in rows), 'gpio')
    for r in rows:
        print(fmt(r))
