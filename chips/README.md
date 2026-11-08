# chips/ — chip descriptions and register tables

Two kinds of files live here:

| file | written by | content |
|------|-----------|---------|
| `<part>.yaml` (e.g. `mk22fn1m0vll12.yaml`) | hand | core/arch/family, memory map, pin table, peripheral instances → models, IRQ names (schema: docs/PLAN.md "Chip description", docs/CONTRACTS.md) |
| `<family>_regs.yaml` (e.g. `mk22_regs.yaml`) | `tools/svd2chip.py` | register table generated from CMSIS-SVD: peripheral bases, registers, offsets, widths, reset values, access, fields, enum labels |

Never edit a `*_regs.yaml` by hand; fix the generator or the SVD and regenerate.

## K22 references

Documents used for `mk22fn1m0vll12.yaml` / `mk22fx512vll12.yaml` (PDFs in the checkout root; data, search them with
`pdftotext -layout`):

- **Data sheet** `K22P100M120SF5.pdf` (Rev 4, 11/2014): pinout and signal multiplexing (section 5.1), electricals.
  Covers the non-A parts MK22FN1M0VLL12 / MK22FX512VLL12 that the YAMLs model.
- **Reference manual** `K22P100M120SF5V2RM.pdf` (K22 Sub-Family RM, Rev. 5, 03/2015, 1613 pages). Note: it names
  the "A" mask parts MK22FN1M0AVLL12 / MK22FX512AVLL12 (SVD `MK22FA12`). Chapter numbers used in the model
  docstrings come from it (SIM 12, DMAMUX 21, eDMA 22, WDOG 24, MCG 25, FTFE 29, ADC 33, PDB 37, PIT 39, LPTMR 40,
  RTC 42, SPI 47, UART 49, GPIO 52). Facts taken from it: SysTick has no external reference (§3.2.1.2, hence
  `systick_ext_div: 0`), interrupt table (Table 3-5), DMAMUX sources (Table 3-25), module clocks (Table 5-2).
  See docs/LIMITATIONS.md "K22 checks" for what was confirmed and what differs. UART5 and DAC1, which the
  MK22F12 SVD describes but the 100-pin part lacks, are listed under `omitted:` in `mk22fn1m0vll12.yaml`.
- **Bootloader manual** `KBTLDR120RM.pdf` (Kinetis Bootloader v1.2.0 RM, Rev. 0, 07/2015). Useful for images that
  sit behind a flash-resident bootloader, such as the Withings image linked at 0x4000 (the bootloader occupies
  0x0-0x3FFF, including the flash configuration field at 0x400). The K22 has no ROM bootloader (RM §6.3.1, boot
  only from internal flash), so the reset vector at 0x0 belongs to that flash bootloader. Relevant facts:
  the bootloader jumps to the application only if its vector table holds a valid PC and SP (§2.4); the
  application's Bootloader Configuration Area (BCA) is at application start + 0x3C0, valid only with tag `kcfg`
  (§2.3, Table 2-1); the stock KBOOT places the application at 0xA000 (`BL_APP_VECTOR_TABLE_ADDRESS`, §10.4.3),
  not 0x4000; the manual does not say that the bootloader writes VTOR. The Withings image has no `kcfg` at
  0x4000 + 0x3C0, so its bootloader is a custom one, not stock KBOOT. CHIP_EM has no bootloader image, so
  `Session` presets VTOR to the load address of a raw `.bin` (and `Chip.reset()` falls back to the image start
  when `[VTOR]` is not a valid table), standing in for the bootloader's jump.

## mk22_regs.yaml provenance

- Source: CMSIS-SVD `MK22F12.svd` (Freescale/NXP, `<version>1.6</version>`), from the `cmsis-svd/cmsis-svd-data`
  GitHub repo, commit `c65f8551e57c770344d229dcaa0bf838fa29aff4` (main, fetched 2026-11-04):
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/Freescale/MK22F12.svd>
- `MK22F12` is the NXP device name covering MK22FN1M0Vxx12 and MK22FX512Vxx12 (120 MHz K22 with FTFE/FlexNVM,
  UART0–5 (UART5 only on larger packages), SDHC, CAN0, 2×ADC, 4×FTM). `MK22FA12.svd` (same repo, same commit) is the later "A" mask
  (MK22FN1M0AVxx12/MK22FX512AVxx12); it is not used.
- The downloaded file is cached (untrusted data, parsed with `xml.etree` only) under `svd/dl-*/MK22F12.svd`.
- Regenerate:
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-Sasv/MK22F12.svd --flat -o chips/mk22_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/Freescale/MK22F12.svd (SVD version 1.6)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-Sasv/MK22F12.svd --check chips/mk22fn1m0vll12.yaml
  ```
- Known gaps of this SVD (compared with NXP's `MK22F12.h`): no `<interrupt>` entries for IRQ 23 (RNG), 30
  (UART0_LON), 55 (reserved), 57 (MCG), 64 (SWI); no RNG peripheral block. The SVD names the VBAT register file
  `RFVBAT`, GPIO ports `GPIOA..E` (the RM's `PTA..PTE`), the oscillator `OSC`, and the flash config field
  `FTFE_FlashConfig` (0x400). `regtable.ALIASES` maps the usual alternative names.
- Kinetis write-1-to-clear flags (PORT ISFR, PCR ISF, LLWU Fx, RTC SR, ...) are plain `read-write` in this SVD (no
  `modifiedWriteValues`), so `Register.w1c_mask` is 0 for them: peripheral models must declare w1c themselves.
- Registers that the SVD gives no `<fields>` for are treated as fully writable unless `read-only`.

## stm32f405_regs.yaml provenance

- Source: CMSIS-SVD `STM32F405.svd` (STMicro, `<version>1.2</version>`), `cmsis-svd/cmsis-svd-data` repo, same pinned
  commit as the K22 file, `c65f8551e57c770344d229dcaa0bf838fa29aff4` (fetched 2026-11-04):
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F405.svd>
  (sha256 `261c70ebbeb17bc982abc7d3c8e257b78e5a89457ddf2d9655bf2f2502f95134`), cached under
  `svd/dl-stm32f405-Ij9M/STM32F405.svd` (untrusted data, parsed with `xml.etree` only).
- Regenerate / check:
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f405-Ij9M/STM32F405.svd --flat -o chips/stm32f405_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F405.svd (SVD version 1.2)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f405-Ij9M/STM32F405.svd --check chips/stm32f405zg.yaml   # 0 mismatches
  ```
- Known quirks of this SVD (handled in `chips/stm32f405zg.yaml` or the `stm32.*` models, never by editing the
  table): it describes a superset (F42x-style UART7/8, SPI4-6, SAI1, LTDC, GPIOJ/K), which the F405 chip YAML does
  not map; `<interrupt>` 4 (FLASH) is missing; `fpuPresent` is false and `nvicPrioBits` 3 (the F405 has an FPU and 4
  priority bits); USART `SR` reset is 0x00C00000 instead of 0x000000C0 (`stm32.usart` overrides it); `PWR_CR.VOS`
  is marked read-only (`stm32.pwr` makes it writable); `DBGMCU_IDCODE` resets to 0x10006411 (an F2 id;
  `stm32.dbgmcu` returns 0x10076413); OTG_HS_PWRCLK claims a 0x3F200-byte block (the chip YAML maps OTG HS/FS as
  one 256 KB stub each). Status flags that are rc_w0 (USART SR, TIM SR) are plain `read-write`, so the models
  implement the clearing semantics.
- Pin table (`chips/stm32f405zg.yaml`): DS8626 Rev 12 Table 7 (LQFP144 column) and Table 9 (AF0-AF15), parsed
  from `pdftotext -layout` output by column position and cross-checked against Figure 14 (LQFP144 pinout) and ST's
  STM32_open_pin_data (`mcu/STM32F405ZGTx.xml`, `mcu/IP/GPIO-STM32F417_gpio_v1_0_Modes.xml` at commit
  `7d1f1514ed5583ec5007ad91236b4e1d377295b1`): pin numbers and AF numbers agree except for naming (ST data says
  `USB_OTG_HS_*`, `RCC_MCO_1`; the data sheet `OTG_HS_*`, `MCO1`, used here) and the data sheet typo PG13 AF8
  `UART6_CTS` (corrected to `USART6_CTS`).

## STM32F405xx / STM32F407xx package variants (`chips/stm32f405{rg,og,oe,vg}.yaml`, `chips/stm32f407*.yaml`)

- Same die as the STM32F405ZG: each file `include:`s `stm32f405zg.yaml` (core, memory map, peripherals, IRQs,
  `regs_file: stm32f405_regs.yaml`) and overrides `name`, `package`, `memory` (512 KB flash for the "E" parts,
  DS8626 Table 2) and `pins`.
- Pin tables: generated from DS8626 Rev 12 (`DS_dm00037051.pdf`, identical to `DS_stm32f405zg.pdf`) with
  `pdftotext -bbox` word coordinates: Table 7 (the six package columns LQFP64, WLCSP90, LQFP100, LQFP144, UFBGA176,
  LQFP176; note 1 for the 25 UFBGA thermal VSS balls F6..K10) and Table 9 (AF0..AF15 per GPIO, including AF11
  `ETH_*` and AF13 `DCMI_*`, which only the F407 files keep). Checks run on the generated tables:
  - the LQFP144 table regenerated this way equals `chips/stm32f405zg.yaml` (0 differences in 144 pins);
  - pin/ball positions equal ST's STM32_open_pin_data at commit `7d1f1514ed5583ec5007ad91236b4e1d377295b1`
    (`mcu/STM32F405RGTx.xml`, `STM32F405O(E-G)Yx.xml`, `STM32F407V(E-G)Tx.xml`, `STM32F407Z(E-G)Tx.xml`,
    `STM32F407I(E-G)Tx.xml`, `STM32F407I(E-G)Hx.xml`): 0 differences for every package;
  - AF numbers equal `mcu/IP/GPIO-STM32F417_gpio_v1_0_Modes.xml` for all 140 GPIOs (ST lists RTC/oscillator/WKUP
    as pseudo-AF0 entries; here they are `additional`/`analog` keys, as in the F405ZG file);
  - pin and GPIO counts equal DS8626 Table 2 (51/72/82/114/140 GPIOs).
  Signal names follow the data sheet (`ETH_MII_RX_CLK/ETH_RMII_REF_CLK`, ST's XML says `ETH_RX_CLK`/`ETH_REF_CLK`).
  Typo fixed as in the F405ZG: PG13 AF8 `USART6_CTS`.
- Downloads (untrusted data, parsed with `xml.etree` only), cached under `svd/dl-open-pin-data/` (sha256):
  `STM32F405RGTx.xml` 3e965c28403876ea586deaf403f5cff069bb127341913fadb58985ddc135b6d3,
  `STM32F405O(E-G)Yx.xml` 2723954f4dbe2fefcd76444f566acf4ccbf94347e0118ad983f9a9aafef10f0f,
  `STM32F405VGTx.xml` 0d1c19c7af7bdbc84a358607f5e2a5e628d64b67eb66c397bb85c1667a11cb4f,
  `STM32F405ZGTx.xml` b40bfb0e439c8c6e4bc57a1fd5c0c2facf45d96682cf44b06124d57b9925abfa,
  `STM32F407V(E-G)Tx.xml` 3771e470e578568e70ee9c10af40173641c29c6e267cf5b268204a8b8aefbea7,
  `STM32F407Z(E-G)Tx.xml` 6638c54654455bd7e1e7ef4f8408c0daf08aea0537ce9c54a51550cdaf419dc4,
  `STM32F407I(E-G)Tx.xml` b708c3eb9b0225050f33ee24db00f394b98d58286a15f2d3a608fc0829c86bd9,
  `STM32F407I(E-G)Hx.xml` 86a6f7e93741748bdc50ea02f272efce452ce074354fe1f7ba581d3ff80ca9ef,
  `GPIO-STM32F417_gpio_v1_0_Modes.xml` 8ccd37f14a82da3b2acbe858e687f8e8b2df8b18fd8e5a711cfff11ad834ee6e
  (`https://raw.githubusercontent.com/STMicroelectronics/STM32_open_pin_data/7d1f1514/mcu/<name>`).
- Second SVD for the F407 parts: cmsis-svd-data `data/STMicro/STM32F407.svd` at `c65f8551e57c770344d229dcaa0bf838fa29aff4`
  (sha256 `e2afca4f03f654225886d8508551cb7ac8f3c95ee28f18953aad466ecb56e6d6`), cached as `svd/dl-stm32f407/STM32F407.svd`.
  `svd2chip --check` gives 0 mismatches for every variant against both STM32F405.svd and STM32F407.svd (same
  superset quirks as above). The register table stays `stm32f405_regs.yaml`.
- Ball numbering (UFBGA176, WLCSP90): `num` = row-major index over the existing balls (rows without I/O), `ball:` =
  the ball name. `tools/pin_table_check.py chips/stm32f407igh.yaml --expect 201` (176 + 25 balls).

## svd2chip usage

```
python tools/svd2chip.py <in.svd> -o chips/<name>_regs.yaml [--peripherals SIM,PORTA,...] [--flat]
                         [--enums auto|names|none] [--source "URL / commit"]
python tools/svd2chip.py <in.svd> --check chips/<part>.yaml
```

- `--peripherals`: emit only these (without `--flat`, the peripherals they derive from are added automatically).
- `--flat`: inline derived peripherals (every peripheral has its full register list, `derived_from: null`).
- `--enums`: `names` = SVD `enumeratedValue/name`; `auto` (default) = the name, or the (collapsed, ≤80 char)
  description when the SVD name is just a number (NXP/Freescale SVDs name values `0`, `1`, ...); `none` = no enums.
- `--check`: compares peripheral base addresses (and IRQ numbers/names) of a hand-written chip YAML against the SVD
  and prints `MISMATCH` / `NOTE` lines. A missing chip YAML is reported, not an error. Exit status is always 0.

Supported SVD features: device→peripheral→cluster→register inheritance of `size`, `access`, `resetValue`,
`resetMask`; `dim`/`dimIncrement`/`dimIndex` (lists `0,1,2`, ranges `0-31`, `A-D`) on registers, clusters and
fields — `NAME%s` → `NAME0..`, `NAME[%s]` → `NAME0..` too; clusters (nested; flattened with names
`CLUSTER_REG`, offsets added); `derivedFrom` on peripherals, clusters, registers (same scope or dotted
`PERIPH.REG`), fields and enumeratedValues; field positions as `bitOffset/bitWidth`, `lsb/msb` or `bitRange [m:l]`;
numbers in hex (`0x`), binary (`#`, `0b`), decimal, with `k/M/G` suffixes; enum values with don't-care bits
(`#1x0`) are expanded.

### Deliberately omitted SVD peripherals (`omitted:`)

A chip YAML may list SVD peripherals that the part does not have (the SVD often covers a family superset):

```yaml
omitted:
  - {name: UART5, irqs: [68, 69], reason: "not on the 100-pin K22: ..."}
```

`--check` prints `OMITTED <name>@<base> (<reason>)` for each (and `OMITTED irq <n>: ...` for the listed IRQ numbers,
whatever the chip YAML names them, e.g. `Reserved84`) instead of a NOTE or MISMATCH. Listing a peripheral under
`omitted:` while still instantiating it is a mismatch. `chipdesc` keeps the list in `ChipDescription.extra`.

## `*_regs.yaml` schema

```yaml
device: MK22F12              # SVD <device><name>
svd_version: "1.6"
address_unit_bits: 8
cpu: {name: CM4, fpu: true, nvic_prio_bits: 4}
interrupts:                  # all <interrupt> elements of all peripherals, by IRQ number (exception = 16 + n)
  0: DMA0
  ...
peripherals:
  PORTA:                     # SVD peripheral name
    base: 0x40049000         # absolute address (chip address units)
    size: 0xA4               # end of the SVD addressBlock(s); if none: max register end rounded up to 0x1000
    derived_from: null       # name of the base peripheral (non-flat output only; registers below then hold only
                             # the derived peripheral's own additions/overrides, usually [])
    description: "Pin Control and Interrupts"   # optional
    group: PORT                                   # optional, SVD groupName
    interrupts: {59: PORTA}                       # optional, this peripheral's IRQs
    registers:               # sorted by offset
      - {name: PCR0, offset: 0x0, width: 4, reset: 0x00000742, reset_mask: 0xFFFFFFFF, access: read-write, fields: [
          {name: PS, lsb: 0, width: 1, enum: {0: "...", 1: "..."}},
          {name: MUX, lsb: 8, width: 3, enum: {0: "Pin disabled (analog).", 1: "Alternative 1 (GPIO).", ...}},
          {name: ISF, lsb: 24, width: 1, enum: {0: "...", 1: "..."}}]}   # + write_effect: oneToClear if the SVD says so
```

Register keys: `name` (dim/cluster expanded), `offset` (from peripheral base, address units), `width` (bytes:
1/2/4), `reset` (already masked by `reset_mask`), `reset_mask` (bits with a defined reset value), `access`
(`read-only` | `write-only` | `read-write` | `writeOnce` | `read-writeOnce`), `fields` (sorted by `lsb`).
Field keys: `name`, `lsb`, `width` (bits), optional `access` (only when it differs from the register's), optional
`write_effect` (SVD `modifiedWriteValues`, e.g. `oneToClear`), optional `enum` {value: label}.
Registers may overlap (SVD alternate registers, e.g. UART `WP7816T0`/`WP7816T1` at 0x1B).

## Python API (`chipem/regtable.py`)

```python
from chipem.regtable import load_regs, regs_for
rt = load_regs("chips/mk22_regs.yaml")       # cached; uses libyaml if available
sim = rt.peripheral("SIM")                   # PeripheralRegs (aliases resolved: VBAT→RFVBAT, PTA→GPIOA, ...)
sim["SCGC5"].offset, sim["SCGC5"].reset      # 0x1038, 0x00040182
sim.at(0x1038); sim.containing(0x1039)       # by offset
sim["SCGC5"].field("PORTA").mask             # Field: lsb, width, mask, get(v), put(v, fv), enum
sim["SCGC5"].ro_mask, .w1c_mask              # read-only = RO fields + reserved bits (whole reg if read-only)
sim.reset_values(); sim.reg_specs()          # reg_specs → kwargs for chipem.periph.base.Reg
regs_for("PORTA", rt)                        # PeripheralRegs | None (path or RegTable; default chips/mk22_regs.yaml)
rt.by_address(0x40048038)                    # (PeripheralRegs, Register|None) | None
```
