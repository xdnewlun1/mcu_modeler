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

- **Data sheet** `docs/datasheets/nxp/K22P100M120SF5.pdf` (Rev 4, 11/2014): pinout and signal multiplexing (section 5.1), electricals.
  Covers the non-A parts MK22FN1M0VLL12 / MK22FX512VLL12 that the YAMLs model.
- **Reference manual** `docs/datasheets/nxp/K22P100M120SF5V2RM.pdf` (K22 Sub-Family RM, Rev. 5, 03/2015, 1613 pages). Note: it names
  the "A" mask parts MK22FN1M0AVLL12 / MK22FX512AVLL12 (SVD `MK22FA12`). Chapter numbers used in the model
  docstrings come from it (SIM 12, DMAMUX 21, eDMA 22, WDOG 24, MCG 25, FTFE 29, ADC 33, PDB 37, PIT 39, LPTMR 40,
  RTC 42, SPI 47, UART 49, GPIO 52). Facts taken from it: SysTick has no external reference (§3.2.1.2, hence
  `systick_ext_div: 0`), interrupt table (Table 3-5), DMAMUX sources (Table 3-25), module clocks (Table 5-2).
  See docs/LIMITATIONS.md "K22 checks" for what was confirmed and what differs. UART5 and DAC1, which the
  MK22F12 SVD describes but the 100-pin part lacks, are listed under `omitted:` in `mk22fn1m0vll12.yaml`.
- **Bootloader manual** `docs/datasheets/nxp/KBTLDR120RM.pdf` (Kinetis Bootloader v1.2.0 RM, Rev. 0, 07/2015). Useful for images that
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
- Pin tables: generated from DS8626 Rev 12 (formerly also kept as `DS_dm00037051.pdf`; now only `docs/datasheets/st/DS_stm32f405zg.pdf`) with
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

## STM32F415ZG / STM32F417ZG (`chips/stm32f415zg.yaml`, `chips/stm32f417zg.yaml`)

The F415/F417 are the F405/F407 with the cryptographic processor (CRYP) and hash processor (HASH) bonded out (same die,
DEV_ID 0x413; DS8597 shares the package pinouts and alternate functions of DS8626). `stm32f415zg.yaml` only includes
`stm32f405zg.yaml` and overrides the name; `stm32f417zg.yaml` is `stm32f407zg.yaml` with the name and header changed (an
include is one level deep, so it cannot include the F407 variant). The CRYP / HASH entries of the base file carry
`params: {only: [STM32F415, STM32F417]}`: on other part names the `stm32.cryp` / `stm32.hash` models stay inactive
stubs (`_util.part_present` / `make_absent`). Both files pass `tools/pin_table_check.py` with 0 errors; they are not in
`tests/test_stm32_variants.py` (covered by `tests/test_stm32_cryp_hash.py`). The F405 SVD omits RCC AHB2ENR/AHB2RSTR
bits 4/5 (CRYPEN/HASHEN, CRYPRST/HASHRST); `stm32.rcc` makes them writable.

## STM32F427xx / STM32F437xx (`chips/stm32f427vg.yaml` base, `stm32f427{zg,ig,igh}.yaml`, `stm32f437{ai,zi,ii}.yaml`)

- **Die**: DEV_ID 0x419 (RM0090 38.6.1), a different die from the F405/F407 (180 MHz with over-drive, 256 KB RAM,
  FMC with SDRAM, SAI1, DMA2D, UART7/8, SPI4-6, dual-bank flash). `stm32f427vg.yaml` (LQFP100, 1 MB) is the base file
  with the core, memory map, peripherals and IRQs; the other F427/F437 files `include:` it and override name,
  package, pins and (2 MB "I" parts) memory. Part-number decoding from the ordering information (DS9405 / DS9484
  section 8): V = 100 pins, Z = 143/144, A = 169, I = 176 pins; G = 1024 KB, I = 2048 KB flash; H = BGA package.
  The STM32F437 adds CRYP/HASH, gated with `params: {only: [STM32F437, STM32F439]}` in the base file (the F437 CRYP
  has GCM/GMAC/CCM: `gcm: true`; the HASH SHA-224/256: `sha2: true`). LCD-TFT (LTDC) is F429/F439 only: listed under
  `omitted:` (with IRQs 88/89), and the `LCD_*` alternate functions are left out of the pin tables.
- **Documents**: DS9405 Rev 14 (`docs/datasheets/st/DS_stm32f427vg.pdf`, STM32F427xx/429xx) and DS9484 Rev 14
  (`docs/datasheets/st/DS_stm32f437ai.pdf`, STM32F437xx/439xx): Table 2 (peripheral and GPIO counts), Table 10 (pin
  and ball definitions, pages 53-71), Table 12 (AF0..AF15, pages 75-86), Table 13 (register boundary addresses),
  Table 17 (frequency limits per voltage scale / over-drive), Table 23 (over-drive switching times), Figure 11
  (LQFP100 pinout), Figure 16 (UFBGA169 ballout). RM0090 Rev 22 (`docs/datasheets/st/RM0090_stm32f4_reference_manual.pdf`):
  Table 63 (vector table for STM32F42xxx/43xxx), chapter 6 (RCC for STM32F42xxx/43xxx), 5.5 (PWR registers for
  STM32F42xxx/43xxx), 3.5 / 3.9 and Tables 6-9 (dual-bank flash), Tables 43/44 (DMA request mapping with the
  F42x-only requests), chapters 11 (DMA2D), 23 (CRYP), 25 (HASH), 27.6.10 (I2C_FLTR), 29 (SAI), 37 (FMC).
- **Register table** `chips/stm32f427_regs.yaml`: CMSIS-SVD `STM32F427.svd` (STMicro, `<version>1.2</version>`),
  cmsis-svd-data at the same pinned commit `c65f8551e57c770344d229dcaa0bf838fa29aff4` (fetched 2026-11-10):
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F427.svd>
  (sha256 `ffe9741404427aebfbe1fcb2388bd41a12c715314ebe64e922475a61d852c13b`), cached in `svd/dl-stm32f427/`.
  cmsis-svd-data has no STM32F437.svd (the F437 is the F427 die with CRYP/HASH, which STM32F427.svd already
  describes). `STM32F429.svd` (sha256 `7b14d8a1d20c0924078059f1c51fb85082f279589a69d30e7b9fdead1bf8343d`) is cached
  next to it as a second source; it is not used for the table (it adds LTDC and lacks the UART7/8 enable bits).
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f427/STM32F427.svd --flat -o chips/stm32f427_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F427.svd (SVD version 1.2)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f427/STM32F427.svd --check chips/stm32f427vg.yaml   # 0 mismatches
  ```
  `--check` gives 0 mismatches for all seven F427/F437 files; the NOTEs are the merged OTG/Ethernet windows, the
  core-private blocks, DBGMCU, and IRQ 87 (SAI1) / 90 (DMA2D), which RM0090 Table 63 has and the SVD lacks.
- **Quirks of STM32F427.svd** (fixed in the models or the chip YAML, never in the table): no `<interrupt>` for 87
  SAI1 and 90 DMA2D; IRQ 48 is named `FMC` (RM0090 Table 63 still says FSMC; the YAML follows the SVD); no
  RCC_PLLSAICFGR / RCC_DCKCFGR, no fields for RCC_CR PLLSAION/PLLSAIRDY, AHB1ENR GPIOJ/K, CCMDATARAM, DMA2D,
  APB2ENR SAI1 (and the matching reset / low-power bits), PLLI2SCFGR PLLI2SQ: `stm32.rcc_f42x` adds the registers
  and makes the bits writable; wrong reset values PLLI2SCFGR 0x20003000 (RM 0x24003000), AHB1LPENR 0x7E6791FF (RM
  0x7EEF97FF), APB1LPENR 0x36FEC9FF (RM 0xF6FEC9FF), APB2LPENR 0x00075F33 (RM 0x04777F33) -- with the SVD values the
  F42x-only blocks would be clock-gated in Sleep mode; FLASH_OPTCR without SPRMOD/DB1M/BFB2 (`stm32.flash_f42x`);
  FMC BWTR3/BWTR4 listed at the offsets of BWTR1/BWTR2 (`stm32.fmc` adds them at 0x114/0x11C); PWR_CSR.VOSRDY
  marked writable; `fpuPresent` false and `nvicPrioBits` 3 as in the F405 SVD; DBGMCU IDCODE (the model returns
  0x20036419: DEV_ID 0x419, REV_ID 0x2003). The superset LTDC block is `omitted:`.
- **Pin tables**: generated from `pdftotext -bbox` word coordinates of DS9405 Table 10 (one column per package)
  and Table 12 (AF0..AF15): Table 10 rows anchored on the pin-type column, wrapped cells attached to the nearest
  row; Table 12 rows anchored on the port name, fragments assigned to the nearest AF column. DS9484 (F437) parses to
  the identical tables (0 differences). Packages: LQFP100 (`stm32f427vg`, 82 GPIOs), LQFP144 (`stm32f427zg`,
  `stm32f437zi`, 114), LQFP176 (`stm32f427ig`, `stm32f437ii`, 140), UFBGA176+25 (`stm32f427igh`, 201 balls, 140),
  UFBGA169 (`stm32f437ai`, 169 balls, 130 GPIOs; rows A..N without I, row-major `num`, `ball:` key; the corner balls
  A1/A13/N1/N13 are NC). Not done: LQFP208, TFBGA216, WLCSP143.
  Checks: ST's STM32_open_pin_data at commit `7d1f1514ed5583ec5007ad91236b4e1d377295b1` (`mcu/STM32F427V(G-I)Tx.xml`,
  `STM32F427Z(G-I)Tx.xml`, `STM32F427I(G-I)Tx.xml`, `STM32F427I(G-I)Hx.xml`, `STM32F427A(G-I)Hx.xml`,
  `STM32F437AIHx.xml`, `IP/GPIO-STM32F427_gpio_v1_0_Modes.xml`, cached in `svd/dl-open-pin-data-f42x/`, sha256 in the
  table below): 0 differences in pin/ball positions for every package, 0 differences in AF numbers for all 168
  GPIOs (spelling only: ETH_CRS vs ETH_MII_CRS, USB_OTG_* vs OTG_*); the LQFP144 table has the same pin at every
  position as `stm32f407zg.yaml`; Figure 11 and Figure 16 spot-read by eye; `tools/pin_table_check.py --strict`
  0 errors / 0 warnings on all seven files. Data sheet corrections: `UART7_Tx/Rx`, `UART8_Tx/Rx` (mixed case in
  Tables 10/12) written `UART7_TX`/`UART7_RX`/...; PH6/PH7 AF15 is EVENTOUT (Table 12 prints "-"). Kept as DS9405
  spells them (they differ from the F405 files): PA0/PA5 AF1 `TIM2_CH1/TIM2_ETR`, PC13 additional `TAMP_1`, PI8
  `TAMP_2`, PB15 `RTC_REFIN` as AF0 only, PG4/PG5 AF12 `FMC_A14/FMC_BA0`, `FMC_A15/FMC_BA1`.
  `tests/test_stm32f427_pins.py` checks that every GPIO shared between the seven files has identical functions.

  | file (svd/dl-open-pin-data-f42x/) | sha256 |
  |---|---|
  | `GPIO-STM32F427_gpio_v1_0_Modes.xml` | `7f324a063f74f138a0448332d7103fc5dde57af6c36bec9d39935ae06d59cc85` |
  | `STM32F427A(G-I)Hx.xml` | `1e0d731660fc1af23fa3f20c535e1ef07c5f2941e648fedf4e03d2e718618a12` |
  | `STM32F427I(G-I)Hx.xml` | `a327d76f86cfac85da497f0fe1fdcc7c216d0d6f3dcdf5ba5aa5b1bafd8f94bc` |
  | `STM32F427I(G-I)Tx.xml` | `b101879000b07b0ba1075608f2f7f730216c3c72ac1e966dedcce6f32f206aa0` |
  | `STM32F427V(G-I)Tx.xml` | `b4d7707fc1099ec8d5b032405352caa0a3b71a19aec08b388f8296c61b3aa003` |
  | `STM32F427Z(G-I)Tx.xml` | `b518f29ba090a9c843cb10229757d2f6965cb0fc42b5fc871290febbda6fd8be` |
  | `STM32F437AIHx.xml` | `5cbe4dcb24d2a724f1ac497774838178c55ad975663fc75b24ad889462a45adc` |

- **RM0090 errata noticed**: Table 9 lists bank-2 sector 14 of a dual-bank 1 MB part at 0x0808 0000 (it is
  0x0808 8000, following sectors 12/13); the FLASH_CR description for the F42x labels SNB `0000: sector 0`
  with four bits although the field is SNB[4:0]; the RCC_APB2LPENR reset value is printed "0x0x0477 7F33".

## TMS320F2808 (`chips/tms320f2808.yaml`, `tms320f2808_regs.yaml`, `tms320f2808_bootrom.asm`)

- **Inputs**: data sheet `docs/datasheets/ti/tms320f2808.pdf` = SPRS230P (TMS320F280x/C280x/F2801x, Rev. P, Feb 2021);
  part TMS320F2808, package 100-pin LQFP "PZ" (the GGM/ZGM BGA balls are in Table 7-1 but not described). No SVD (TI
  publishes none). TI reference guides downloaded from `https://www.ti.com/lit/pdf/<lit>` (one new directory each,
  text extracted with `pdftotext -layout` next to the other C28x docs in `svd/c28x-docs/`):

  | dir | document | sha256 |
  |---|---|---|
  | `dl4/spru712h.pdf` | SPRU712H 280x/2801x/2804x System Control and Interrupts | `62b0f48b8544090e56adec59dadb877d8307c773846a2660d267d9e666a546b1` |
  | `dl5/spru716.pdf` | SPRU716 280x ADC | `0fa5cbe761933bc31c50e656a6155decded08aafb6f6c8640f850b48d5407971` |
  | `dl6/spru791.pdf` | SPRU791 280x ePWM | `ef739feed1856a6d940ed2deebcd3a08e61d63daa45c4448c5af1d0c1602e46e` |
  | `dl7/spru924.pdf` | SPRU924F 280x HRPWM | `6352f1fac7b430e1ecb88be342153c5b2d7d604123b533bac9c6a8668814babc` |
  | `dl8/spru807.pdf` | SPRU807 280x eCAP | `960b441c67026dddd4c915fb64d721efe2477368c3f2926c8fed1cf443c6379b` |
  | `dl9/spru790.pdf` | SPRU790 280x eQEP | `9b316622d38f0955bdb9a09b228504c707197e8cf32cb6023029fcb71b8dbd9e` |
  | `dl10/spru051.pdf` | SPRU051D 281x SCI | `cc2d157b0caf5eba4157dcc795a456b3c1848fae0ad854bbe846442b783c0be0` |
  | `dl11/spru059.pdf` | SPRU059E 281x SPI | `ad2f5fe5a3e4b6c980dbbb3202f56c84af64ed68fc121a50f0292806977283a2` |
  | `dl12/spru721.pdf` | SPRU721C 280x I2C | `2dc19f33a699bad6f325bedfbe0cb05035d336d905233a56903f974c1af9d46c` |
  | `dl13/spru074.pdf` | SPRU074 281x eCAN | `21fbd1b2c3863e8a8f74a101330c9fbd0f68bbc7f18babd47714233524b4d775` |
  | `dl14/spru722.pdf` | SPRU722C 280x boot ROM | `4cc73232a6d7ac1e524e457918fc3073d7eabb548f259d2d97fb01561510d6b7` |
  | `dl15/sprufk7.pdf` | SPRUFK7B 280x SCI | `b6c4449dcd5cda60254a1ddfe916f18422db248c77a9a1e392f3f37b44e34bf9` |
  | `dl16/sprug72.pdf` | SPRUG72 280x SPI | `87beb1deda03666a79305f72dd59e07502f690148d66a4ffb9b2e5dbd84c407d` |
  | `dl17/sprueu0.pdf` | SPRUEU0 280x/2801x eCAN | `a469bd765e2625fc5d933d1286f4186e64b582954b3c793000ef37061bedca01` |

- **Pin table**: generated from the data sheet text (Table 7-1 p. 14-19, `pdftotext -layout`; Table 9-17 p. 106 for
  MUX1-3) by a scratch parser. Cross-checks: 100 unique pins 1..100; for all 35 GPIOs the Table 7-1 function list equals
  the Table 9-17 row; all 100 pin labels of Figure 7-1 (p. 9, `pdftotext -bbox`, label matched to the nearest pin
  number on its side) equal the YAML names; SPRU712H Table 4-9 ("2809 and 2808 GPIO MUX") gives 0 differences.
  `tools/pin_table_check.py chips/tms320f2808.yaml --strict` reports 0 errors / 0 warnings. Names join the GPIO and its
  mux functions like Figure 7-1 (`GPIO12/TZ1/CANTXB/SPISIMOB`); GPIO28/29 have no MUX2 (`GPIO29/SCITXDA/TZ6`, the figure
  prints `GPIO29/SCITXDA//TZ6`). No data sheet typo was found (Table 7-1 calls GPIO21's EQEP1B "input A"; the name is
  right).
- **Register table**: `tools/gen_tms320f2808_regs.py` copies the blocks whose IP the F2823x shares from
  `tms320f28232_regs.yaml` and applies the documented 280x differences (SYSCTRL with XCLK and PLLSTS.CLKINDIV, PCLKCR0/1
  280x gates, no PCLKCR3/MAPCNF; GPIO ports A/B only, GPBPUD reset 0x18; XINT1/2/XNMI only; DEV_EMU PARTID 0x003C /
  REVID 3; SPI-B/C/D; I2C without I2CEMDR; the Table 9-28 interrupt list). Regenerate and check against the data sheet:

  ```sh
  .venv/bin/python -I tools/gen_tms320f2808_regs.py
  pdftotext -layout docs/datasheets/ti/tms320f2808.pdf $SCRATCH/ds.txt
  .venv/bin/python -I tools/gen_tms320f2808_regs.py --check-ds $SCRATCH/ds.txt     # 551 registers, 0 mismatch(es)
  ```

  The check parses Tables 9-2..9-31 (CPU timers, ePWM1-6, eCAP1-4, eQEP1-2, ADC + mirror, eCAN-A/B control registers,
  SCI-A/B, SPI-A..D, I2C, GPIO, DEV_EMU, PIE, XINT, SYSCTRL) and compares every address; the flash and CSM register
  addresses were compared by hand with SPRU712H Table 1-1 (0x0A80-0x0A87) and Tables 2-4 / 5-7 (0x0AE0-0x0AEF). The eCAN mailbox
  RAM, LAM/MOTS/MOTO (SPRUEU0) and the reserved TBPHSHR/CMPAHR locations of ePWM5/6 are not in the data sheet tables.
  `tests/test_c2000_f2808_chip.py` asserts the file equals the generator output.
- **Boot ROM**: `tms320f2808_bootrom.asm` (a functional stand-in at TI's addresses: InitBoot 0x3FFB50, CPU vectors at
  0x3FFFC0 per SPRU722C Table 1) is assembled into `boot.rom_words` (`gen_tms320f2808_regs.py --bootrom-words`; the test
  re-assembles and compares).

## STM32F103T6 (`chips/stm32f103t6.yaml`, `stm32f103_regs.yaml`)

- Data sheet: DS15060 Rev 7 (June 2015), STM32F103x4/x6 low-density performance line,
  `docs/datasheets/st/DS_stm32f103t6.pdf`. Part number decoded with Table 58: STM32 F 103 **T** (36 pins) **6** (32 KB
  flash); the only 36-pin package is VFQFPN36 (Table 2, package letter U). Table 2 gives the T6 resources (10 KB SRAM,
  TIM1 + 2 general-purpose timers, SPI1, I2C1, USART1/2, USB, CAN, 26 GPIOs, 2 ADCs / 10 channels).
- Pin table: transcribed by hand from Table 5 (pages 26-28, VFQFPN36 column; pdftotext -layout) and checked pin by
  pin against Figure 7 (page 25, VFQFPN36 pinout): 36 pins, 26 GPIOs (PA0-15, PB0-7, PD0/PD1 on OSC_IN/OSC_OUT).
  No machine-readable second source was downloaded (ST's STM32_open_pin_data has `STM32F103T(4-6)Ux.xml`; not fetched).
  Data sheet fix: PB3's remap list lacks SPI1_SCK although the other SPI1_REMAP pins are listed; SPI1_SCK is added
  (asserted in `tests/test_stm32f103t6_chip.py`). The exposed pad (Table 51 D2/E2) is not a numbered pin.
  `tools/pin_table_check.py chips/stm32f103t6.yaml --strict`: 0 errors, 0 warnings.
- Register table: CMSIS-SVD `STM32F103xx.svd` (STMicro, `<version>1.3</version>`), cmsis-svd-data commit
  `c65f8551e57c770344d229dcaa0bf838fa29aff4`:
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F103xx.svd>
  (sha256 `1d92b65aaf397a18a599fb6a840812015ad379cdcc0cc3687f673f63e7445367`), cached as
  `svd/dl-stm32f103/STM32F103xx.svd` (untrusted data, xml.etree only). Regenerate / check:
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f103/STM32F103xx.svd --flat -o chips/stm32f103_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F103xx.svd (SVD version 1.3)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f103/STM32F103xx.svd --check chips/stm32f103t6.yaml   # 0 mismatches
  ```
- SVD quirks: it describes the whole F103 line (TIM4-14, SPI2/3, USART3, UART4/5, I2C2, DAC, GPIOE-G, ADC3, SDIO,
  DMA2, FSMC), listed under `omitted:`; BKP starts at 0x40006C04 (DR1; the block is mapped there with size 0x3FC, and
  `--check` clamps an SVD extent that runs into the next block); the bxCAN registers are prefixed (`CAN_MCR` ...) and
  describe 14 filter banks, so `stm32f1.can` uses the F405 table's CAN1 layout; the IRQ names differ from RM0008 /
  CMSIS (`ADC` vs `ADC1_2`, `CAN1_TX` vs `USB_HP_CAN1_TX`, `CAN1_RX0` vs `USB_LP_CAN1_RX0`, `TIM1_BRK_TIM9` vs
  `TIM1_BRK`, `TIM1_UP_TIM10` vs `TIM1_UP`, `TIM1_TRG_COM_TIM11` vs `TIM1_TRG_COM`, `RTCAlarm` vs `RTC_Alarm`,
  `USB_FS_WKUP` vs `USBWakeUp`): the chip YAML uses the CMSIS names and records the SVD ones in `svd_irq_names`
  (`--check` prints them as `RENAMED`). The USB/CAN shared SRAM (0x40006000), system memory and option bytes are not
  in the SVD. `DBG_IDCODE` resets to 0 in the SVD (the model returns 0x10006412).
- Reference manual RM0008 was not obtained (see docs/LIMITATIONS.md "STM32F103T6").

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

A chip YAML may also use other IRQ names than the SVD (the vendor header names) and list the SVD's in
`svd_irq_names: {19: CAN1_TX, ...}`; `--check` reports those as `RENAMED irq n` instead of a mismatch.

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

## P89CV51RB2/RC2/RD2 (`chips/p89cv51rd2.yaml` base, `p89cv51r{b,c,d}2[fbc].yaml`, `p89cv51_regs.yaml`)

- 80C51 (`core: {arch: mcs51, backend: mcs51}`), family `mcs51`, crystal 11.0592 MHz by default (`clock_hz` is the
  oscillator; 40 MHz max in 12-clock mode, 20 MHz in 6-clock mode).  `p89cv51rd2.yaml` is the RD2 in PLCC44 (FA);
  the variants `include:` it and override name, package, the flash size (RB2 16 KB, RC2 32 KB, RD2 64 KB, Table 2)
  and, for TQFP44 (FBC), the pins.  Generated from the data sheet by a transcription script (Table 3: PLCC44 and
  TQFP44 columns; TQFP = PLCC - 6 mod 44 asserted); `tools/pin_table_check.py`: 0 errors, 0 warnings for all six.
- `p89cv51_regs.yaml`: hand-written (no SVD), 56 SFRs in 10 blocks (CPU, INTC, PORTS, SYSCTRL, TIMER01, TIMER2, UART,
  SPI, PCA, WDT); every address and bit name from Table 4 (pp. 10-12), reset values from the per-register tables
  (Tables 5, 8, 13, 16, 19, 21, 24, 27, 30, 32, 35, 37, 42, 44, 46, 50; cited in each register's description) or the
  80C51 family value where none is given (SP 07h, ports FFh).  Offsets are SFR addresses; blocks are sparse (base
  0x30000, size 0x100).  Memory and spaces: docs/MCS51_CORE.md.
