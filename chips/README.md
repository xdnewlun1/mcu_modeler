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

## STM32F429xx / STM32F439xx (`chips/stm32f429zi.yaml` base, `stm32f429{vi,ii,iih,ai,bi,ni}.yaml`, `stm32f439{zi,ii,ai,bi,ni}.yaml`)

- **Die**: the STM32F427/F437 die (DEV_ID 0x419) with the LCD-TFT controller (LTDC) bonded out (DS9405 / DS9484 Table 2).
  `stm32f429zi.yaml` (LQFP144, 2 MB, the 32F429IDISCOVERY / NUCLEO-F429ZI part) is the base file: the F427 base file's
  memory map, peripherals and IRQs plus `LTDC` (`stm32.ltdc`, 0x40016800, IRQ 88 `LCD_TFT` / 89 `LCD_TFT_1`, the
  STM32F429.svd names; RM0090 Table 63 and the HAL say LTDC / LTDC_ER) and `params: {ltdc: true}` on `stm32.rcc_f42x`
  (makes APB2ENR/RSTR/LPENR bit 26 writable, reserved on the F427). The other files `include:` it and override name,
  package and pins (`stm32f439zi.yaml` only name). CRYP/HASH stay gated `only: [STM32F437, STM32F439]`, so they are active
  on the F439 files. Ordering code (section 8): V = 100, Z = 144, A = 169, I = 176, B = 208, N = 216 pins; I = 2048 KB;
  H = BGA. All files are "I" (2 MB) parts.
- **Register table**: the shared blocks use `chips/stm32f427_regs.yaml` (STM32F427.svd), not STM32F429.svd: STM32F429.svd
  (same cmsis-svd-data commit c65f8551, sha256 `7b14d8a1d20c0924078059f1c51fb85082f279589a69d30e7b9fdead1bf8343d`,
  cached in `svd/dl-stm32f427/` and `svd/dl-stm32f429/`) lacks the UART7/UART8 enable bits, names SAI1 `SAI` and differs
  from STM32F427.svd in DMA2D, FMC, GPIOI/K, PWR, RCC and SYSCFG register descriptions that the existing models and tests
  were verified against. The LTDC registers come from a table generated from STM32F429.svd for that block only:
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f429/STM32F429.svd --flat --peripherals LTDC -o chips/stm32f429_ltdc_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F429.svd (SVD version 1.2; --peripherals LTDC)"
  ```
  `stm32.ltdc` uses the chip's table when it has an LTDC block, otherwise this file. SVD quirk fixed in the model:
  LxCACR reset 0 (RM0090 16.7.19: 0xFF). `svd2chip --check` gives 0 mismatches for all twelve files against both
  STM32F429.svd and STM32F427.svd.
- **Pin tables**: the five packages the F427/F437 files already have (LQFP100, LQFP144, LQFP176, UFBGA176+25, UFBGA169)
  are those files plus the `LCD_*` alternate functions of DS9405 Table 12 (AF9 / AF14). LCD AF numbers from ST's
  STM32_open_pin_data @ 7d1f1514 (`IP/GPIO-STM32F427_gpio_v1_0_Modes.xml`, LTDC_* signals), cross-checked against DS9405
  Table 12 parsed with `pdftotext -bbox`: 81 LCD AF entries on 79 GPIOs, 0 differences. LQFP208 (`*bi`) and TFBGA216
  (`*ni`, row-major ball index `num` over rows A..R without I/O + `ball:` key) are new: positions and pin types from open
  pin data (`mcu/STM32F429B(E-G-I)Tx.xml`, `STM32F429NIHx.xml`), checked against DS9405 Table 10 (LQFP208 / TFBGA216
  columns, `pdftotext -layout`): 0 differences for the 168 GPIOs and for the 40 / 48 supply and system pins. The 28
  GPIOs only these packages have (PI12-PI15, PJ0-PJ15, PK0-PK7) carry only LCD_* (AF14) and EVENTOUT (DS9405 Table 12).
  Positions of every file: 0 differences against the matching F429 and F439 open pin data file (`svd/dl-open-pin-data-f429/`,
  which also caches the F429I-DISC1 `.ioc` used for the board wiring). DS9484 (F437/F439) Table 10 parses identically
  to DS9405. `pin_table_check.py --strict` 0 errors / 0 warnings on all twelve files; `Chip(desc)` builds without
  warnings. Not done: WLCSP143 (`STM32F429ZGYx`), UFBGA176 F439IIH, the "E"/"G" flash sizes.
  The tables are checked by `tests/test_stm32f429_pins.py` (positions vs
  open pin data, LCD AFs vs the modes file, F427 functions unchanged, packages agree, spot checks).

  | file (svd/dl-open-pin-data-f429/) | sha256 |
  |---|---|
  | `C46_Discovery_STM32F429I-DISC1_STM32F429ZI_Board_AllConfig.ioc` | `c7797e6b1c008436fd06e6830d3eb03c41949c279a62e90273d42c5d006a4b9e` |
  | `GPIO-STM32F427_gpio_v1_0_Modes.xml` | `7f324a063f74f138a0448332d7103fc5dde57af6c36bec9d39935ae06d59cc85` |
  | `STM32F429A(G-I)Hx.xml` | `d517c1471598ef2dd1fb0799496577b2711a0b1bb10b9d808e3d5872e329e806` |
  | `STM32F429B(E-G-I)Tx.xml` | `341b641f51cdc26c4f90300344b0040718a86ca3c285ec289b485a712721fefe` |
  | `STM32F429I(E-G)Tx.xml` | `06290e23f44a70b84b9bf2d95df7eca079954cea3437efd76d37cdccab493cec` |
  | `STM32F429I(E-G-I)Hx.xml` | `3b792f4775ee6d366f5e5314cb0e487c62ffe2fc9eedd4f3d72904e08dc36cab` |
  | `STM32F429IITx.xml` | `4e742b1d4c22c15546d8b107378cbce863bdc9d4a4278d360a241bce17ccb589` |
  | `STM32F429NIHx.xml` | `2a68bee1989b3ef1e1eea1648659067debdd052b0b4c70aa1aabeda4c0b230ac` |
  | `STM32F429VITx.xml` | `60baf24d4e4d4c0240bb0368fe9e3b3f5e9c979c0255906022703fe9d8de29ba` |
  | `STM32F429Z(E-G)Tx.xml` | `b605c71d3f67a1a2c22604e1eab35a0dae09d7f830e49f298f9efe7e8be69ca8` |
  | `STM32F429ZITx.xml` | `8010a61425f8dfc4c56dcb3c0ff51c43bcbff7b57c12bc69a516a24e95b703a4` |
  | `STM32F439AIHx.xml` | `89319a04d2ab1d35dffa8872dbbf7eb44bcf12118de32a813efbc5fd6a3c7bb8` |
  | `STM32F439B(G-I)Tx.xml` | `f970e785a2129a440ce9dcdb24edad79af7f49c719c992250d3e3e3e0e6e5651` |
  | `STM32F439I(G-I)Hx.xml` | `bfaa2272c00aec32474828b17d43617cb9d1783edde253e9a09a1fa100287afd` |
  | `STM32F439I(G-I)Tx.xml` | `5ae0fc4c50ac44c0f9aee5cf22d460a0198fa924513eadb32c5823edf04fc526` |
  | `STM32F439N(G-I)Hx.xml` | `963d631b187a6d71089fb5f73cdc9b6f5b5f9abb1a810665baa6309915b91fd9` |
  | `STM32F439V(G-I)Tx.xml` | `1209d3cad417c8cf3680dacbe70b8e44ff8b5c799b553e197884fd975707c5b7` |
  | `STM32F439Z(G-I)Tx.xml` | `3e2a0179043d601ccbc8e8b14f3dbf92a0e67879e4e3771bf59bb196071f91a0` |

## STM32F401xx / STM32F411xx (`chips/stm32f401re.yaml` + `stm32f401{cc,ve}.yaml`, `chips/stm32f411re.yaml` + `stm32f411{ce,ve}.yaml`)

- **Dies**: STM32F401xB/C (DEV_ID 0x423: 128/256 KB flash, 64 KB SRAM), STM32F401xD/E (0x433: 384/512 KB, 96 KB),
  STM32F411xC/E (0x431: 256/512 KB, 128 KB, 100 MHz, SPI5, I2S on SPI1-5, PLLI2SM, FISSR/FMSSR). The xB/C and xD/E
  F401 dies have identical IRQ and peripheral lists in the CubeF4 headers, so `stm32f401cc.yaml` includes
  `stm32f401re.yaml` and overrides memory, pins and the DBGMCU IDCODE (`peripheral_params`). Part numbers: C = 48 pins,
  R = 64, V = 100; C = 256 KB, E = 512 KB; U = UFQFPN, T = LQFP.
- **Documents**: the reference manuals are in `docs/datasheets/st/`: RM0368 Rev 6 (STM32F401xB/C/D/E,
  `RM0368_stm32f401_reference_manual.pdf`) and RM0383 Rev 4 (STM32F411xC/E, `RM0383_stm32f411_reference_manual.pdf`),
  added 2026-11-11 after the chips were first built from second sources (the STM32CubeF4 device headers
  `stm32f401xc.h`, `stm32f401xe.h`, `stm32f411xe.h`, HAL headers, RM0090 Rev 22 for the shared IP). The data sheets
  DS9716 (F401xB/C), DS10086 (F401xD/E) and DS10314 (F411xC/E) are still missing — st.com did not answer: add them as
  `docs/datasheets/st/DS_stm32f401cc.pdf`, `DS_stm32f401re.pdf`, `DS_stm32f411re.pdf` for the pin-table check.
- **Checked against RM0368 Rev 6 / RM0383 Rev 4** (2026-11-11; regression tests `tests/test_stm32f4x1_rm_*.py`):
  peripheral list and bases (2.3 Table 1), memory sizes (2.3.1, 3.3 Table 5 / Table 4), vector tables (10.1 Table 38
  / 37, equal to the CubeF4 `IRQn_Type`), EXTI lines (10.2.5; `reserved_lines: [19, 20]`), DMA request maps (9.3.3
  Tables 28/29 / 27/28, explicit tables in `periph/stm32/f4x1.py`), DBGMCU (23.6.1, 23.16), the electronic signature
  (24.1/24.2, `SYSMEM` block = `stm32f1.sysmem` with `uid_offset: 0x210, fsize_offset: 0x222`), RCC (6.3: every
  register's fields and reset value), PWR (5.4), FLASH (3.4.1 wait-state tables, 3.8), and the shared peripheral
  models as these dies use them (see docs/LIMITATIONS.md "STM32F401 / STM32F411" for the list and what is still
  approximate). Where the SVD reset value disagrees with the RM, the RM wins through a model override.
- **Register tables** `chips/stm32f401_regs.yaml`, `chips/stm32f411_regs.yaml`: CMSIS-SVD `STM32F401.svd` / `STM32F411.svd`
  (STMicro, `<version>1.1</version>`), cmsis-svd-data commit `c65f8551e57c770344d229dcaa0bf838fa29aff4` (fetched
  2026-11-10), cached in `svd/dl-stm32f401/`, `svd/dl-stm32f411/`:
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F401.svd>
  (sha256 `7cbcddfc76b0d8c022a999ad39a5b464147aa3ad69130b8350b4f315979c3d80`),
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F411.svd>
  (sha256 `e89450855e4ff2c8dde8684f910e2d0857e463ec5718793dd2fe5d9e757cd317`).
  ```
  C=c65f8551e57c770344d229dcaa0bf838fa29aff4
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f401/STM32F401.svd --flat -o chips/stm32f401_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/$C/data/STMicro/STM32F401.svd (SVD version 1.1)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f411/STM32F411.svd --flat -o chips/stm32f411_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/$C/data/STMicro/STM32F411.svd (SVD version 1.1)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f401/STM32F401.svd --check chips/stm32f401re.yaml   # 0 mismatches
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f411/STM32F411.svd --check chips/stm32f411re.yaml   # 0 mismatches
  ```
  `--check` gives 0 mismatches for all six files; the NOTEs are the merged OTG FS window, DBG (= DBGMCU), the
  core-private blocks and the 28/29 IRQs the SVDs lack (the IRQ table comes from the CubeF4 `IRQn_Type`).
- **Quirks of STM32F401.svd / STM32F411.svd** (fixed in the models or the chip YAML, never in the table): only 34
  `<interrupt>` entries (no DMA streams, USART1/2/6, TIM4/5, WWDG, SPI5); TIM8 listed although the dies have none
  (`omitted:`); the block is called `ADC_Common` (C_ADC in the F405 SVD; the YAML uses the SVD name); no RCC_DCKCFGR
  (TIMPRE), no APB2ENR/RSTR/LPENR SPI4 bit on the F401 and no SPI5 bits on the F411, no PLLI2SCFGR.PLLI2SM and BDCR.LSEMOD
  on the F411, no PWR_CR LPLVDS/MRLVDS (and F411 FISSR/FMSSR), no FLASH_SR.RDERR: `stm32.rcc_f4x1` / `stm32.pwr_f4x1` /
  `stm32.flash_f4x1` add them; F405 reset values copied into AHB1ENR (0x00100000: CCMDATARAMEN, there is no CCM RAM),
  the LPENR registers and PLLI2SCFGR: the models use the RM0368/RM0383 6.3 reset values (AHB1LPENR 0x0061900F, AHB2LPENR
  0x80, APB1LPENR 0x10E2C80F, APB2LPENR 0x00077930, PLLI2SCFGR 0x24003000; the CubeF4 LL header's
  RCC_PLLI2SCFGR_RST_VALUE 0x20003000 / F411 0x20003010 disagrees with the RM too); `DBGMCU_IDCODE` 0x10006411
  (the model returns 0x10006433 F401xD/E, 0x10016423 F401xB/C, 0x10006431 F411); `fpuPresent` false.
- **Pin tables**: generated from ST's STM32_open_pin_data at commit `7d1f1514ed5583ec5007ad91236b4e1d377295b1`
  (positions and per-package signals: `mcu/STM32F401R(D-E)Tx.xml`, `STM32F401C(B-C)Ux.xml`, `STM32F401V(D-E)Tx.xml`,
  `STM32F411R(C-E)Tx.xml`, `STM32F411C(C-E)Ux.xml`, `STM32F411V(C-E)Tx.xml`; AF numbers: `mcu/IP/GPIO-STM32F401_gpio_v1_0_Modes.xml`,
  `GPIO-STM32F411_gpio_v1_0_Modes.xml`; also cached: `STM32F401R(B-C)Tx.xml`, `STM32F401C(D-E)Ux.xml` (identical to their
  siblings)), all in `svd/dl-open-pin-data-f4x1/`, with ST's signal names normalised to the CHIP_EM spelling
  (SYS_JTMS-SWDIO → JTMS-SWDIO, SYS_JTDO-SWO → JTDO/TRACESWO, RCC_MCO_1 → MCO1, USB_OTG_FS_* → OTG_FS_*, I2S2_ext_SD →
  I2S2ext_SD, RCC_OSC_IN → `analog: OSC_IN`, SYS_WKUP → `additional: WKUP`, RTC_AF1 → `additional: RTC_AF1`, ADC1_INx →
  `ADC`, OTG_FS_VBUS → `additional`). Second source: the AF number of every alternate function equals a
  `GPIO_AF<n>_<periph>` definition of the HAL for STM32F401xC / STM32F411xE (`tests/test_stm32f4x1_pins.py`). Not
  checked against the data sheet tables (not available). Corrections: open pin data names pin 12 of the F401 LQFP64 /
  pin 8 of the UFQFPN48 / pin 20 of the LQFP100 "VSSA/VREF-": written `VSSA` (VREF- bonded to VSSA); PB8/PB9 AF12
  SDIO_D4/D5 are missing from the F401 UFQFPN48 XML but are in the die's AF table: added. The modes file also lists
  I2S2ext_CK/WS/MISO and I2S3ext_CK/WS/MISO on F401 pins, which no package XML has: left out.

  | file (svd/dl-open-pin-data-f4x1/) | sha256 |
  |---|---|
  | `GPIO-STM32F401_gpio_v1_0_Modes.xml` | `4491b4ee54faa9d6ccf722f6e79ca752b2dcd168f5279ad249959a561db4fbff` |
  | `GPIO-STM32F411_gpio_v1_0_Modes.xml` | `ac1753a62670b4b27dfb1b9b7d8c3106d5e7926066e2f1cc6e8385178b28c34b` |
  | `STM32F401C(B-C)Ux.xml` | `367709d75b2401b73f15fee9df8c0efe8c2b7607ba20be373642a78a2548f464` |
  | `STM32F401C(D-E)Ux.xml` | `0aa5d456777f96264d3d89f449a5bcf8b8b5b947ab72c0700e7bac7d2aecc305` |
  | `STM32F401R(B-C)Tx.xml` | `a37f14996c23131c1d8b3cb2b8b3bed3f75ee68771c7f8712bf37e1a04553c3c` |
  | `STM32F401R(D-E)Tx.xml` | `43fd289df73ad74880e3b450d89ae6f16e429e6eb885300ddbf8af64d175ab15` |
  | `STM32F401V(D-E)Tx.xml` | `64cca058dbef1bcdf998629c6e8066e6ed23d3fa734c79536e65505471181820` |
  | `STM32F411C(C-E)Ux.xml` | `962e848cc9ce9682aa74213706ed79583e94f6f6d67dc45e314bc7c83a7fe87d` |
  | `STM32F411R(C-E)Tx.xml` | `e94f0b99a3300bae665f211079e114eef33656cdfac7c2da671d3717fe7881ab` |
  | `STM32F411V(C-E)Tx.xml` | `41a2a9389caf336f7efad91b5e410eee8e28959ba349978d86445117454678af` |

## STM32F446xx (`chips/stm32f446re.yaml` base, `stm32f446{ve,ze,me}.yaml`, `stm32f446_regs.yaml`)

- **Die**: DEV_ID 0x421 (DBGMCU_IDCODE 0x10006421, rev A), Cortex-M4F, 180 MHz with over-drive, 512 KB flash in one
  bank (sectors 0-3 16 KB, 4 64 KB, 5-7 128 KB), 128 KB SRAM (SRAM1 112 KB + SRAM2 16 KB), no CCM RAM. New against
  the F42x: QUADSPI (registers 0xA0001000, window 0x90000000), SAI2, SPDIFRX, HDMI-CEC, FMPI2C1, PLL_R as SYSCLK,
  PLLSAI/PLLI2S with own M and P dividers, DCKCFGR2. Not on the F446: Ethernet, RNG, CRYP/HASH, LTDC, DMA2D, UART7/8,
  SPI5/6, I2S2ext/I2S3ext, GPIOI-K. `stm32f446re.yaml` (LQFP64, NUCLEO-F446RE) holds core, memory, peripherals and IRQs;
  the VE (LQFP100), ZE (LQFP144, NUCLEO-F446ZE) and ME (WLCSP81, row-major ball `num`, rows A..J without I) files
  `include:` it.
- **Documents**: RM0390 Rev 9 (STM32F446 reference manual, `docs/datasheets/st/RM0390_stm32f446_reference_manual.pdf`,
  added 2026-11-11): the chip YAML and models were checked against it chapter by chapter (memory map Table 1, vector
  Table 38, DMA Tables 28/29, RCC/PWR/FLASH/SYSCFG/EXTI registers, FMC, QUADSPI, FMPI2C, SPDIFRX, SAI, SDIO, OTG,
  HDMI-CEC, DBGMCU, electronic signature; docs/LIMITATIONS.md "STM32F446", `tests/test_stm32f446_rm0390.py`). The data
  sheet DS10693 is still missing (please add `docs/datasheets/st/DS_stm32f446re.pdf`): pins, electrical limits and the
  package list are not checked against it. Earlier sources, still used: STM32F446.svd; the STM32CubeF4 device header
  `stm32f446xx.h` (IRQn_Type, FMPI2C1 base and FMPI2C_TypeDef, DBGMCU fields) and HAL driver (`stm32f4xx_hal_rcc_ex.c/.h`,
  `stm32f4xx_hal_qspi.c`, `stm32f4xx_hal_gpio_ex.h` AF cross-check, `stm32f4xx_hal_pwr_ex.h` PWR_WAKEUP_PIN2 = PC13);
  STM32CubeF4 example projects for the STM32446E-EVAL / NUCLEO-F446ZE (github.com/STMicroelectronics/STM32CubeF4,
  read 2026-11-10); RM0090 Rev 22 for the IP shared with the F42x.
- **Register table** `chips/stm32f446_regs.yaml`: CMSIS-SVD `STM32F446.svd` (`<version>1.1</version>`), cmsis-svd-data at
  commit `c65f8551e57c770344d229dcaa0bf838fa29aff4` (fetched 2026-11-10),
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F446.svd>
  (sha256 `962f478d38d6f4a934b800dd565505231bf51b794d658cf6de99476c09e8b3c4`), cached in `svd/dl-stm32f446/`.
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f446/STM32F446.svd --flat -o chips/stm32f446_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F446.svd (SVD version 1.1)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f446/STM32F446.svd --check chips/stm32f446re.yaml   # 0 mismatches
  ```
  `--check` gives 0 mismatches for all four files (svd2chip ALIASES gained SDIO->SDMMC, SPDIFRX->SPDIF_RX,
  CEC->HDMI_CEC); the NOTEs are FMPI2C1 (not in the SVD), the merged OTG windows, DBGMCU / core-private blocks and the
  IRQs the SVD does not define (1, 49, 74-77, 87, 91-96).
- **Quirks of STM32F446.svd** (fixed in models / chip YAML, never in the table; each confirmed against RM0390): the
  `<interrupt>` elements are attached to the wrong peripherals and include vectors the part does not have (UART7/8,
  ETH, LCD-TFT): the chip YAML IRQs follow stm32f446xx.h = RM0390 Table 38; no FMPI2C1 peripheral (`stm32.fmpi2c`
  builds its registers from RM0390 23.9); the block at 0x40012C00 is described as an SDMMC v2 (IDMA, VER/ID
  registers) -- the F446 SDIO is the F405 SDIO without CE-ATA / STBITERR (RM0390 ch. 29), so `stm32.sdio` falls back to
  the F405 SDIO table with `ceata: false`; RCC_PLLCFGR without PLLR fields, RCC_CR without a writable PLLSAION, no
  APB1RSTR.CECRST, wrong reset values RCC_PLLI2SCFGR 0x20003000 (RM0390: 0x24003010), RCC_PLLSAICFGR 0x24003000
  (0x04003010), AHB1ENR 0x00100000 (0), F405 low-power enable resets (RM0390 6.3.15-6.3.19), CFGR bit 23 writable;
  no SYSCFG_CFGR (RM0390 8.2.8); FMC NAND bank 2 / PC Card bank 4 registers (absent, RM0390 Table 80); DBG IDCODE
  0x10006411 (the model returns 0x10006421, RM0390 33.6.1); `fpuPresent` false. The OTG blocks use `core: otg2`
  (RM0390 ch. 31). The device electronic signature (UID / F_SIZE / package, RM0390 ch. 34) is `stm32.sysmem_f446`.
- **Pin tables**: generated from ST's STM32_open_pin_data at commit `7d1f1514ed5583ec5007ad91236b4e1d377295b1`
  (positions and per-package signals: `mcu/STM32F401R(D-E)Tx.xml`, `STM32F401C(B-C)Ux.xml`, `STM32F401V(D-E)Tx.xml`,
  `STM32F411R(C-E)Tx.xml`, `STM32F411C(C-E)Ux.xml`, `STM32F411V(C-E)Tx.xml`; AF numbers: `mcu/IP/GPIO-STM32F401_gpio_v1_0_Modes.xml`,
  `GPIO-STM32F411_gpio_v1_0_Modes.xml`; also cached: `STM32F401R(B-C)Tx.xml`, `STM32F401C(D-E)Ux.xml` (identical to their
  siblings)), all in `svd/dl-open-pin-data-f4x1/`, with ST's signal names normalised to the CHIP_EM spelling
  (SYS_JTMS-SWDIO → JTMS-SWDIO, SYS_JTDO-SWO → JTDO/TRACESWO, RCC_MCO_1 → MCO1, USB_OTG_FS_* → OTG_FS_*, I2S2_ext_SD →
  I2S2ext_SD, RCC_OSC_IN → `analog: OSC_IN`, SYS_WKUP → `additional: WKUP`, RTC_AF1 → `additional: RTC_AF1`, ADC1_INx →
  `ADC`, OTG_FS_VBUS → `additional`). Second source: the AF number of every alternate function equals a
  `GPIO_AF<n>_<periph>` definition of the HAL for STM32F401xC / STM32F411xE (`tests/test_stm32f4x1_pins.py`). Not
  checked against the data sheet tables (not available). Corrections: open pin data names pin 12 of the F401 LQFP64 /
  pin 8 of the UFQFPN48 / pin 20 of the LQFP100 "VSSA/VREF-": written `VSSA` (VREF- bonded to VSSA); PB8/PB9 AF12
  SDIO_D4/D5 are missing from the F401 UFQFPN48 XML but are in the die's AF table: added. The modes file also lists
  I2S2ext_CK/WS/MISO and I2S3ext_CK/WS/MISO on F401 pins, which no package XML has: left out.

  | file (svd/dl-open-pin-data-f4x1/) | sha256 |
  |---|---|
  | `GPIO-STM32F401_gpio_v1_0_Modes.xml` | `4491b4ee54faa9d6ccf722f6e79ca752b2dcd168f5279ad249959a561db4fbff` |
  | `GPIO-STM32F411_gpio_v1_0_Modes.xml` | `ac1753a62670b4b27dfb1b9b7d8c3106d5e7926066e2f1cc6e8385178b28c34b` |
  | `STM32F401C(B-C)Ux.xml` | `367709d75b2401b73f15fee9df8c0efe8c2b7607ba20be373642a78a2548f464` |
  | `STM32F401C(D-E)Ux.xml` | `0aa5d456777f96264d3d89f449a5bcf8b8b5b947ab72c0700e7bac7d2aecc305` |
  | `STM32F401R(B-C)Tx.xml` | `a37f14996c23131c1d8b3cb2b8b3bed3f75ee68771c7f8712bf37e1a04553c3c` |
  | `STM32F401R(D-E)Tx.xml` | `43fd289df73ad74880e3b450d89ae6f16e429e6eb885300ddbf8af64d175ab15` |
  | `STM32F401V(D-E)Tx.xml` | `64cca058dbef1bcdf998629c6e8066e6ed23d3fa734c79536e65505471181820` |
  | `STM32F411C(C-E)Ux.xml` | `962e848cc9ce9682aa74213706ed79583e94f6f6d67dc45e314bc7c83a7fe87d` |
  | `STM32F411R(C-E)Tx.xml` | `e94f0b99a3300bae665f211079e114eef33656cdfac7c2da671d3717fe7881ab` |
  | `STM32F411V(C-E)Tx.xml` | `41a2a9389caf336f7efad91b5e410eee8e28959ba349978d86445117454678af` |

## STM32F446xx (`chips/stm32f446re.yaml` base, `stm32f446{ve,ze,me}.yaml`, `stm32f446_regs.yaml`)

- **Die**: DEV_ID 0x421 (DBGMCU_IDCODE 0x10006421, rev A), Cortex-M4F, 180 MHz with over-drive, 512 KB flash in one
  bank (sectors 0-3 16 KB, 4 64 KB, 5-7 128 KB), 128 KB SRAM (SRAM1 112 KB + SRAM2 16 KB), no CCM RAM. New against
  the F42x: QUADSPI (registers 0xA0001000, window 0x90000000), SAI2, SPDIFRX, HDMI-CEC, FMPI2C1, PLL_R as SYSCLK,
  PLLSAI/PLLI2S with own M and P dividers, DCKCFGR2. Not on the F446: Ethernet, RNG, CRYP/HASH, LTDC, DMA2D, UART7/8,
  SPI5/6, I2S2ext/I2S3ext, GPIOI-K. `stm32f446re.yaml` (LQFP64, NUCLEO-F446RE) holds core, memory, peripherals and IRQs;
  the VE (LQFP100), ZE (LQFP144, NUCLEO-F446ZE) and ME (WLCSP81, row-major ball `num`, rows A..J without I) files
  `include:` it.
- **Documents**: RM0390 Rev 9 (STM32F446 reference manual, `docs/datasheets/st/RM0390_stm32f446_reference_manual.pdf`,
  added 2026-11-11): the chip YAML and models were checked against it chapter by chapter (memory map Table 1, vector
  Table 38, DMA Tables 28/29, RCC/PWR/FLASH/SYSCFG/EXTI registers, FMC, QUADSPI, FMPI2C, SPDIFRX, SAI, SDIO, OTG,
  HDMI-CEC, DBGMCU, electronic signature; docs/LIMITATIONS.md "STM32F446", `tests/test_stm32f446_rm0390.py`). The data
  sheet DS10693 is still missing (please add `docs/datasheets/st/DS_stm32f446re.pdf`): pins, electrical limits and the
  package list are not checked against it. Earlier sources, still used: STM32F446.svd; the STM32CubeF4 device header
  `stm32f446xx.h` (IRQn_Type, FMPI2C1 base and FMPI2C_TypeDef, DBGMCU fields) and HAL driver (`stm32f4xx_hal_rcc_ex.c/.h`,
  `stm32f4xx_hal_qspi.c`, `stm32f4xx_hal_gpio_ex.h` AF cross-check, `stm32f4xx_hal_pwr_ex.h` PWR_WAKEUP_PIN2 = PC13);
  STM32CubeF4 example projects for the STM32446E-EVAL / NUCLEO-F446ZE (github.com/STMicroelectronics/STM32CubeF4,
  read 2026-11-10); RM0090 Rev 22 for the IP shared with the F42x.
- **Register table** `chips/stm32f446_regs.yaml`: CMSIS-SVD `STM32F446.svd` (`<version>1.1</version>`), cmsis-svd-data at
  commit `c65f8551e57c770344d229dcaa0bf838fa29aff4` (fetched 2026-11-10),
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F446.svd>
  (sha256 `962f478d38d6f4a934b800dd565505231bf51b794d658cf6de99476c09e8b3c4`), cached in `svd/dl-stm32f446/`.
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f446/STM32F446.svd --flat -o chips/stm32f446_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F446.svd (SVD version 1.1)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f446/STM32F446.svd --check chips/stm32f446re.yaml   # 0 mismatches
  ```
  `--check` gives 0 mismatches for all four files (svd2chip ALIASES gained SDIO->SDMMC, SPDIFRX->SPDIF_RX,
  CEC->HDMI_CEC); the NOTEs are FMPI2C1 (not in the SVD), the merged OTG windows, DBGMCU / core-private blocks and the
  IRQs the SVD does not define (1, 49, 74-77, 87, 91-96).
- **Quirks of STM32F446.svd** (fixed in models / chip YAML, never in the table): the `<interrupt>` elements are attached
  to the wrong peripherals and include vectors the part does not have (UART7/8, ETH, LCD-TFT): the chip YAML IRQs follow
  stm32f446xx.h; no FMPI2C1 peripheral (`stm32.fmpi2c` builds its registers from FMPI2C_TypeDef); the block at
  0x40012C00 is described as an SDMMC v2 (IDMA, VER/ID registers) -- the F446 SDIO is the F405 one (same SDIO_TypeDef),
  so `stm32.sdio` falls back to the F405 SDIO table; RCC_PLLCFGR without PLLR fields, RCC_CR without a writable
  PLLSAION, wrong reset values RCC_PLLI2SCFGR 0x20003000 (HAL: 0x24003010) and RCC_PLLSAICFGR 0x24003000 (HAL:
  0x04003010), F405 low-power enable resets; DBG IDCODE 0x10006411 (the model returns 0x10006421); `fpuPresent` false.
- **Pin tables**: generated from ST's STM32_open_pin_data at commit `7d1f1514ed5583ec5007ad91236b4e1d377295b1`
  (`mcu/STM32F446R(C-E)Tx.xml`, `V(C-E)Tx`, `Z(C-E)Tx`, `M(C-E)Yx`, `IP/GPIO-STM32F446_gpio_v1_0_Modes.xml`, cached
  in `svd/dl-open-pin-data-f446/`): positions and signals from the package file, AF numbers from the modes file, names
  in the DS8626/DS9405 conventions of the other F4 files (OTG_*, JTMS-SWDIO, MCO1, SPIx_SCK/I2Sx_CK, ADC123_INx,
  WKUPn). Second source: every AF/peripheral pair exists among the STM32F446xx `GPIO_AFn_*` defines of
  `stm32f4xx_hal_gpio_ex.h` (`tests/test_stm32f446_pins.py`). Not checked against the data sheet tables (DS10693 not
  in the repo; RM0390 has no pin tables). Packages: LQFP64 (50 GPIOs), LQFP100 (81), LQFP144 (114), WLCSP81 (63); `pin_table_check.py --strict`
  0/0 on all four. Note: the LQFP64 has no QUADSPI_BK1_IO2 (quad mode needs the 81/100/144-pin packages) and no FMC
  signals; ST's open pin data leaves out signals of blocks a package does not bond out, so smaller packages carry a
  subset of the LQFP144 functions.

  | file (svd/dl-open-pin-data-f446/) | sha256 |
  |---|---|
  | `A42_Nucleo_NUCLEO-F446RE_STM32F446RE_Board_AllConfig.ioc` | `8d0a6d2af566dae914ac6bed5d5b238e4dff5f1df2bc31b8ba11959621159c30` |
  | `GPIO-STM32F446_gpio_v1_0_Modes.xml` | `fe95087cfca51ece0d091a844246d564c12c378f74decd3e780d06c15de53a6c` |
  | `STM32F446M(C-E)Yx.xml` | `89f3d8ac403c9786a1799f211c3a7c074f8ded47f5a6d4d2ba41f1bb2494c9ce` |
  | `STM32F446R(C-E)Tx.xml` | `451d1d286e7e3edc1a90c5f74ed14e85c54101852a3eb7b35b0813794d1a8e59` |
  | `STM32F446V(C-E)Tx.xml` | `4d793604d3f64736b582810bfa4f815f65c93b81b8b02bb66f773d9c0876af87` |
  | `STM32F446Z(C-E)Tx.xml` | `83e6bab59139498ecea4158dfa848ffc214135f2f0b95eabc02c5f5ed462e95d` |

## STM32F205 / F207 / F215 / F217 (`chips/stm32f207zg.yaml` base, `chips/stm32f2*.yaml`, `stm32f2xx_regs.yaml`)

- **Die**: STM32F2 (DEV_ID 0x411), Cortex-M3 120 MHz without FPU, 1 MB flash, 128 KB SRAM + 4 KB backup SRAM.
  `stm32f207zg.yaml` (LQFP144) holds core, memory, peripherals and IRQs; the 14 other files `include:` it and override
  name, package and pins. Ethernet / DCMI: `only: [STM32F207, STM32F217]`; CRYP / HASH: `only: [STM32F215, STM32F217]`.
  Part numbers (DS15818 section 7): R = 64 pins, V = 100, Z = 144, I = 176; G = 1024 KB; T = LQFP, H = UFBGA, Y = WLCSP.
- **Documents**: DS15818 Rev 15 (STM32F205xx/207xx, `docs/datasheets/st/DS_stm32f207zg.pdf`, from
  <https://www.farnell.com/datasheets/2307754.pdf>, sha256 `616830948a2acdd2152f0458eb5eef8bd3965c0c210bffd5c4ec59e336eccbf9`)
  Table 2/3 (counts), Table 8 (pin and ball definitions, pages 47-58), Table 10 (AF mapping, pages 61-66); DS17050
  Rev 9 (STM32F215xx/217xx, `docs/datasheets/st/DS_stm32f217zg.pdf`, from
  <https://nonprod.docs.rs-online.com/5360/0900766b812f91e4.pdf>, sha256
  `a992205be02c24445ea9ff5b633cdacdf7359bfdb2c792b4f4795831967e8848`) Tables 7/9. RM0033 Rev 10 read from the HTML
  rendition at docs.dirba.io (st.com refused the download; see docs/LIMITATIONS.md "STM32F2"). ST's CMSIS device
  headers `stm32f205xx.h`/`stm32f207xx.h`/`stm32f217xx.h` (STMicroelectronics/cmsis-device-f2 @ 49321f1e) for bit
  positions (not stored).
- **Register table** `chips/stm32f2xx_regs.yaml`: CMSIS-SVD `STM32F21x.svd` (STMicro, `<version>1.0</version>`),
  cmsis-svd-data @ `c65f8551e57c770344d229dcaa0bf838fa29aff4`
  (<https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F21x.svd>,
  sha256 `7d1b0edae8c8096617de745b75963f861316c9fc892b5225cda918923cd8b2d7`), cached in `svd/dl-stm32f2/` with
  `STM32F20x.svd` (sha256 `e00aa8e0c8ac07d66eba4f8d2d7af3c7b032bd16c5d81b2d89cebf0e18abe962`; = F21x without CRYP/HASH).
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f2/STM32F21x.svd --flat -o chips/stm32f2xx_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F21x.svd (SVD version 1.0)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f2/STM32F21x.svd --check chips/stm32f207zg.yaml   # 0 mismatches
  ```
  `--check` gives 0 mismatches for all 15 files (also against STM32F20x.svd, which lacks IRQ 79/80 names). The SVD's
  81 interrupts equal RM0033 Table 20 (no FPU vector). The ADC common block keeps the SVD name `ADC_Common`.
- **Quirks of STM32F21x.svd** (fixed in models, never in the table): CAN MCR/MSR/TSR/FMR reset 0 (RM0033 27.9:
  0x00010002 / 0x00000C02 / 0x1C000000 / 0x2A1C0E01; `stm32.can` sets them); UART5 CR3 without DMAR/DMAT
  (`stm32.usart`); SYSCFG_CMPCR.READY at bit 7 (RM0033 7.2.7: bit 8, as the model); DBGMCU_IDCODE 0x10006411 = rev A
  (the model returns 0x20036411, rev X); no `<cpu>` element. It is otherwise a clean F2 map: no VOS/VOSRDY, RTC
  without SSR/SHIFTR/CALR/TSSSR/ALRMxSSR, PREDIV_S 13 bits, HASH without HR5-7/HASH_HR/CSR51-53, CRYP without the
  GCM/CCM registers, DMA SxCR without bit 20, PLLI2SCFGR reset 0x20003000, AHB1ENR reset 0 (no CCMDATARAMEN).
- **Pin tables**: generated with `pdftotext -bbox` from DS15818 Table 8 (rows anchored on the pin-type column,
  the six package columns LQFP64, WLCSP64+2, LQFP100, LQFP144, LQFP176, UFBGA176) and Table 10 (AF0..AF15, rows
  anchored on the port pin). Checks: positions and names of every package equal ST's STM32_open_pin_data @
  `7d1f1514ed5583ec5007ad91236b4e1d377295b1` (0 differences; the F205/F207/F215/F217 XML files of one package are
  identical); AF numbers equal `IP/GPIO-STM32F217_gpio_v1_0_Modes.xml` apart from spelling; additional functions
  (ADC/DAC/WKUP/RTC_AF/OSC/VBUS) equal the XML; DS17050 Table 9 parses to the same AF table as DS15818 Table 10;
  Table 8's alternate-function lists equal Table 10 apart from text-extraction wraps. Corrections and spellings are
  in the `stm32f207zg.yaml` header and asserted in `tests/test_stm32f2_chip.py` (PG13 USART6_CTS, PA6 DCMI_PIXCLK,
  PC3 I2S2_SD, PB4 NJTRST; DS spellings I2Sx_SCK, RTC_50Hz, RTC_AF1/2, I2S2_CKIN/I2S3_CKIN kept). The UFBGA176
  has 201 balls (25 thermal VSS F6..K10 from the XML), the WLCSP 66 (`--expect 201` / `--expect 66` for
  `tools/pin_table_check.py`); all 15 files: 0 errors, 0 warnings with `--strict`. Same GPIO positions as the F407
  files; differences: no I2Sxext_SD, LQFP176 pin 48 VSS (F4 BYPASS_REG), pin 171 RFU (F4 PDR_ON), REGOFF/IRROFF on
  WLCSP/UFBGA.

  | file (svd/dl-open-pin-data-f2/) | sha256 |
  |---|---|
  | `GPIO-STM32F217_gpio_v1_0_Modes.xml` | `f416af1688f492496e7b29b2373394e8b4a9db4c4a24a74ba30d46e7091edb91` |
  | `STM32F205R(B-C-E-F-G)Tx.xml` | `80b8b3f80fd5b6286662007375e3e23cdb972395650e5b4683308d3eb17eadf2` |
  | `STM32F205R(E-G)Yx.xml` | `249eb3c241753caa115b5e6e863395edeb7172a03ef517e8fc32ec0e1712310b` |
  | `STM32F205V(B-C-E-F-G)Tx.xml` | `eb50f096b0cc4194a52afcb5b6e442abd33303ee148615c94132d15d62f1b71d` |
  | `STM32F205Z(C-E-F-G)Tx.xml` | `35b3d413a95311092eea43a72199ca6c0fa6253b8723933ac1ed52fc1916b4ec` |
  | `STM32F207I(C-E-F-G)Hx.xml` | `d5b588d2f07aa61e7473d63a4eaf036787975a965d4d1e728b37a254165f2146` |
  | `STM32F207I(C-E-F-G)Tx.xml` | `964fea492d55b71b806826aaa792d4042927d904b56a113669bbc9a2230c419a` |
  | `STM32F207V(C-E-F-G)Tx.xml` | `85bc1b59397d8d65831933de74e45fd50014a128fc3ac0cb57a2efd8494a57da` |
  | `STM32F207Z(C-E-F-G)Tx.xml` | `172abaf7fdcb0231360bf32578ddd98d2ed7a219938e39c1eb435963d0630d7e` |
  | `STM32F215R(E-G)Tx.xml` | `bec52c83658bb9a7483d5dbcf05bba8332ba78beaf7f41d3b290459326a2bf3c` |
  | `STM32F215V(E-G)Tx.xml` | `ddb9f81b941b07479528b30de5fb6752e5388376ec9acd5f68b3c9d65b584ef4` |
  | `STM32F215Z(E-G)Tx.xml` | `f9f450cda61f5f7991ef943aced4680881dbfb75ad4082ea5ab34c38444a9817` |
  | `STM32F217I(E-G)Hx.xml` | `9075ccc9bc78f5e246127e32ca71895ecf97a675d3f2f88d0f519bafd2256859` |
  | `STM32F217I(E-G)Tx.xml` | `57d2d9d673a47ad277d4988b7c2c29cea03ba76cb37787c2c0041657fc6c62d3` |
  | `STM32F217V(E-G)Tx.xml` | `b5f3c039c16f38d30c1be05badc7df1919a12b4fa6697e30c8c3b6026de36bfc` |
  | `STM32F217Z(E-G)Tx.xml` | `a27ed8de5838dcbd2175a113117c8b682ec5c08b51bb2a6de5a32137dd5de459` |

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

### TMS320F280x / F2801x variants (`chips/tms320f2809.yaml`, `f2806`, `f2802`, `f2801`, `f28016`, `f28015`)

Same data sheet (SPRS230P), same 100-pin PZ pinout ("All the devices in the 280x family have the same MUXing scheme",
SPRU712H 4.2; selections of a peripheral the part lacks are reserved), same register table and boot ROM stand-in. Each
file `include:`s `tms320f2808.yaml` and changes only what the data sheet lists as different, through the generic
variant keys of `chipem/chipdesc.py`: `drop:` (blocks and PIE lines the part does not have), `peripheral_params:`
(PARTID/REVID through `ti_c2000.storage` `reset:`, CSM secure ranges, `SYSCTRL.max_hz`, `ADC.adcclk_max_hz`, HRPWM),
`add_peripherals:` (the smaller L0/L1 program-space mirror), plus `name`, `core` and `memory`.

| part | MHz | flash (Tables 9-18..9-21) | SARAM | ePWM (HRPWM) | eCAP | eQEP | SPI | SCI | eCAN | ADCCLK max | PARTID |
|---|---|---|---|---|---|---|---|---|---|---|---|
| F2809 | 100 | 128K @0x3D8000, H..A 16K | 18K M0 M1 L0 L1 H0 | 1-6 (1A-6A) | 1-4 | 1-2 | A-D | A/B | A/B | 25 MHz | 0x00FE (REVID 1) |
| F2808 | 100 | 64K @0x3E8000, D..A 16K | 18K | 1-6 (1A-4A) | 1-4 | 1-2 | A-D | A/B | A/B | 12.5 MHz | 0x003C |
| F2806 | 100 | 32K @0x3F0000, D..A 8K | 10K M0 M1 L0 L1 | 1-6 (1A-4A) | 1-4 | 1-2 | A-D | A/B | A | 12.5 MHz | 0x0034 |
| F2802 | 100 | 32K @0x3F0000, D..A 8K | 6K M0 M1 L0 | 1-3 (1A-3A) | 1-2 | 1 | A/B | A | A | 12.5 MHz | 0x0024 |
| F2801 | 100 | 16K @0x3F4000, D..A 4K | 6K | 1-3 (1A-3A) | 1-2 | 1 | A/B | A | A | 12.5 MHz | 0x002C |
| F28016 | 60 | 16K @0x3F4000 | 6K | 1-4 (1A-4A) | 1-2 | - | A | A | A | 7.5 MHz | 0x0014 |
| F28015 | 60 | 16K @0x3F4000 | 6K | 1-4 (1A-4A) | 1-2 | - | A | A | - | 7.5 MHz | 0x001C |

Sources: SPRS230P Table 6-1 (100-MHz devices; the F2809 ADC converts in 80 ns = 12.5 MSPS, the others in 160 ns),
Table 6-2 (60-MHz devices, 267 ns), Figures 9-18..9-22 (memory maps), Tables 9-18..9-21 (sectors), Table 9-27 (PARTID;
REVID 0x0001 = F2809 rev. A), section 8 "ADC clock" (25 MHz F2809, 12.5 MHz other 100-MHz parts, tc(ADCCLK) 133.33 ns
on the 60-MHz parts). `SYSCTRL.max_hz` and `ADC.adcclk_max_hz` only warn (one `warn` event) when firmware clocks the
part above its rating; the emulation still runs. Not added: the ROM parts C2801/C2802 and the 60-MHz F2801-60/F2802-60
ordering options (identical to F2801/F2802 apart from the clock). `tests/test_c2000_f280x_chip.py` checks every row.

## TMS320F2833x / F2823x (`chips/tms320f28335.yaml` base, `tms320f28335{zhh,zjz}.yaml`, `tms320f2833{4,3,2}.yaml`, `tms320f2823{5,4}.yaml`, `tms320f2833x_regs.yaml`)

- **Inputs**: data sheet `docs/datasheets/ti/tms320f28232.pdf` = SPRS439Q (TMS320F2833x/F2823x, Aug 2022), the same
  file the F28232 came from. No SVD.
- **Base and variants**: `tms320f28335.yaml` is the superset (FPU32, 150 MHz, all instances: SCI-A/B/C, McBSP-A/B,
  eCAP1-6, HRPWM on ePWM1A-6A, the FPU LVF/LUF PIE lines 12.7/12.8); the other parts `include:` it and use `drop:` /
  `peripheral_params:` (see the F280x section above). The F28232 keeps its own original file (same pins; a test
  compares them).

  | part | FPU | MHz | flash (Tables 8-22..8-24) | SARAM | eCAP | HRPWM | McBSP | SCI | PARTID (OTP 0x380090) | CLASSID |
  |---|---|---|---|---|---|---|---|---|---|---|
  | F28335 | yes | 150 | 256K @0x300000, H..A 32K | 34K L0-L7 | 1-6 | 1A-6A | A/B | A/B/C | 0x00EF | 0x00EF |
  | F28334 | yes | 150 | 128K @0x320000, H..A 16K | 34K | 1-4 | 1A-6A | A/B | A/B/C | 0x00EE | 0x00EF |
  | F28333 | yes | 100 | 256K @0x300000 | 34K | 1-6 | 1A-6A | A/B | A/B/C | 0x00E0 | 0x00EF |
  | F28332 | yes | 100 | 64K @0x330000, D..A 16K | 26K L0-L5 | 1-4 | 1A-4A | A | A/B | 0x00ED | 0x00EF |
  | F28235 | no | 150 | 256K @0x300000 | 34K | 1-6 | 1A-6A | A/B | A/B/C | 0x00E8 | 0x00E8 |
  | F28234 | no | 150 | 128K @0x320000 | 34K | 1-4 | 1A-6A | A/B | A/B/C | 0x00E7 | 0x00E8 |
  | F28232 | no | 100 | 64K @0x330000 | 26K | 1-4 | 1A-4A | A | A/B | 0x00E6 | 0x00E8 |

  All parts: XINTF, 6-channel DMA, eQEP1/2, SPI-A, eCAN-A/B, I2C-A, 88 GPIOs, 12.5-MSPS ADC (`ADC.adcclk_max_hz` 25 MHz
  on every file, the F28232 included; `epwm.c` and `adc_dma.c` set ADCCLKPS = 1 for 25 MHz).
  Sources: Tables 5-1 / 5-2 (device comparison, p. 8-10), Figures 8-23..8-25 (memory maps), Tables 8-22..8-24 (flash
  sectors), Table 8-31 (PARTID / CLASSID), Table 8-32 (PIE). Clocks: 30 MHz crystal (150-MHz parts) or 20 MHz
  (100-MHz parts) with PLLCR = 10 and DIVSEL = /2; `SYSCTRL.max_hz` warns above the rating.
- **Register table**: `tools/gen_tms320f2833x_regs.py` copies `tms320f28232_regs.yaml` and adds SCI-C (0x7770), eCAP5/6
  (0x6A80/0x6AA0) and McBSP-B (0x5040) with instance A's layout; CLASSID resets to 0x00EF. `--check-ds` parses SPRS439Q
  Tables 8-3..8-35 (CPU timers, ePWM1-6, eCAP1-6, eQEP, ADC + mirror, McBSP-A/B, eCAN, SCI-A/B/C, SPI-A, I2C, GPIO,
  XINTF, DEV_EMU, PIE, XINT, SYSCTRL): 646 registers, 0 mismatches. Two data sheet spellings are corrected before the
  comparison: `GPIOINT7SEL` (Table 8-17; the XINT5/6 rows read `GPIOXINT5SEL/6SEL`) and `SCIPRC` (Table 8-14; SCI-A/B
  read `SCIPRIA/B`).

  ```sh
  .venv/bin/python -I tools/gen_tms320f2833x_regs.py
  pdftotext -layout docs/datasheets/ti/tms320f28232.pdf $SCRATCH/f2833x.txt
  .venv/bin/python -I tools/gen_tms320f2833x_regs.py --check-ds $SCRATCH/f2833x.txt     # 646 registers, 0 mismatch(es)
  ```
- **Pins**: the LQFP176 table is the F28232 one (PGF and PTP share it). BGA files, generated from the ZHH / ZJZ ball
  columns of Table 6-1 (each ball takes the name and functions of the PGF pin on the same row; power balls from the
  VDD/VDDIO/VSS lists): `tms320f28335zhh.yaml` (179 balls: the three extra are VSS N11, P6, P8; the ZAY nFBGA has the
  same ballout) and `tms320f28335zjz.yaml` (176 balls: no ball for PGF pin 167 VDD, one extra VDDIO N14). Cross-check:
  the ballout figures (Figures 6-2..6-5 ZHH, 6-6..6-9 ZJZ, `pdftotext -bbox`, each word assigned to the nearest row
  letter and column number) give the same first signal name for all 179 / 176 balls; 146 of the 176 Table 6-1 rows
  also name the same signal as the existing PGF table at that pin number (the rest have the number on a line without a
  name). `num` = row-major index over the existing balls (rows A..P without I/O, columns 1..14), `ball:` = the name;
  `tools/pin_table_check.py chips/tms320f28335zhh.yaml --expect 179 --strict` and `... zjz.yaml --expect 176` report
  0 errors / 0 warnings. Only the F28335 has BGA files (the other parts use the same ballouts).
- `tests/test_c2000_f2833x_chip.py` (device table, BGA spot checks by eye, generator reproducibility and data sheet
  check, variant-key errors), `tests/test_c2000_f2833x_samples.py` (samples at the part clocks, SCI-C, the
  F28335 ↔ STM32 UART board), `tests/test_c2000_f2833x_fpu.py` (FPU32 on TI-compiled code).

## Piccolo: TMS320F2802x / TMS320F2806x (`chips/tms320f28027.yaml`, `tms320f28069.yaml` bases, part variants, `tms320f2802x_regs.yaml`, `tms320f2806x_regs.yaml`, `tms320f2802x_bootrom.asm`, `tms320f2806x_bootrom.asm`)

- **Parts**: TMS320F28027 (LAUNCHXL-F28027 part; 60 MHz, no FPU, LQFP48 "PT" `tms320f28027.yaml`, TSSOP38 "DA"
  `tms320f28027da.yaml`) with the F2802x siblings `tms320f2802{6,3,2,1,0}.yaml`, `tms320f280200.yaml` (PT; flash /
  SARAM / HRPWM / eCAP / PARTID / CLASSID per SPRS523 Table 6-1, Figures 9-1..9-5, Tables 9-3..9-5, 9-11); TMS320F28069
  (90 MHz, FPU32, LQFP100 "PZ" `tms320f28069.yaml`, LQFP80 "PN" `tms320f28069pn.yaml`), `tms320f28069m.yaml` (the
  LAUNCHXL-F28069M part: USB0 storage added) and `tms320f2806{7,6,5,4,3,2}.yaml` (flash / SARAM / CLA / PARTID per
  SPRS698 Table 5-1, Figures 8-1..8-8, Tables 8-3, 8-4, 8-10).  Variants `include:` the base and use `drop:` /
  `peripheral_params:` / `add_peripherals:`.  No SVD (TI publishes none).
- **Documents** (downloaded from `https://www.ti.com/lit/pdf/<lit>`, no other request data; each guide in a new
  `svd/c28x-docs/dlN/`, text extracted with `pdftotext -layout` next to the other C28x docs):

  | file | document | sha256 |
  |---|---|---|
  | `docs/datasheets/ti/tms320f28027.pdf` | SPRS523P TMS320F2802x data sheet (Rev. P, Feb 2021, 147 pp.) | `b73b8c499c36b0e2f0a216c86df6058129d7c596eb73b868fa45f83f11dbc16a` |
  | `docs/datasheets/ti/tms320f28069.pdf` | SPRS698J TMS320F2806x data sheet (Rev. J, Sep 2021, 204 pp.) | `5435818005351068b50f72e12751a2cdd1ab6ad73b3ebbd4953b2faca89d3375` |
  | `svd/c28x-docs/dl18/sprui09.pdf` | SPRUI09A TMS320F2802x / F2802xx TRM (579 pp.: system control, boot ROM, ePWM, HRPWM, eCAP, ADC, COMP, SPI, SCI, I2C) | `fb3a41da5d2fd6e61c059a166ef47e400c474ddc80f9f1c2fb2e8425c447bbba` |
  | `svd/c28x-docs/dl19/spruh18.pdf` | SPRUH18I TMS320x2806x TRM (1141 pp.; + HRCAP, eQEP, CLA, DMA, McBSP, eCAN, USB) | `56278732b337b223fd94682c6bbd6e6983b671e466f3846842dc230bda485dd5` |
  | `svd/c28x-docs/dl20/spruhh2.pdf` | SPRUHH2C LAUNCHXL-F28027 user guide (LEDs, buttons, boot switch, UART) | `e3a81b752ba55b5abc77e819ec4c249e6e1ca6c74144ec6c37448c0ebf7f1296` |
  | `svd/c28x-docs/dl21/sprui11.pdf` | SPRUI11B LAUNCHXL-F28069M overview (LEDs D9/D10 GPIO34/39, CAN GPIO30/31) | `b4819632d1b4d3f7e553b9f130976082a058f04feaa38fa86bb6f78c24fb8b70` |

  The older per-module 2802x guides (SPRUFN3, SPRUGE5, SPRUGE9, SPRUGE8, SPRUFZ8, SPRUGH1, SPRUG71, SPRUFZ9, SPRUFN6)
  were not fetched: SPRUI09A consolidates them.  TI's C2000Ware headers were not used (the samples define their own
  registers in `firmware/samples_c2000_c/piccolo.h`).
- **Pin tables**: generated by a scratch parser from the signal-description tables (SPRS523 7.2.1 pp. 10-13, SPRS698
  6.2.1 pp. 12-20; pin numbers per package), the GPIO mux tables (SPRS523 Tables 9-30/9-31, analog Tables 9-32/9-33;
  SPRS698 Tables 8-39..8-42) and the pinout figures (SPRS523 Figures 7-1/7-2 pp. 8-9, SPRS698 Figures 6-1/6-2 pp. 9-10,
  `pdftotext -bbox`, label matched to the nearest pin number on its side).  Cross-checks, all packages: pin numbers
  unique 1..N; figure label == signal table for every pin (0 differences); GPIO + MUX1-3 == data sheet mux table ==
  TRM mux table (SPRUI09 Tables 1-59..1-61, SPRUH18 Tables 1-64..1-66; 0 differences); GPIO / AIO / ADC channel counts
  equal the device comparison tables (PT 22/6/13, DA 20/6/7, PZ 54/6/16, PN 40/6/12).  Package footnotes applied: DA
  has no COMP2OUT / COMP2A/B (COMP2 dropped), PN has no eQEP2 pins (EQEP2 dropped).  Names: figure labels;
  "GPIO35 (TDI)" -> `GPIO35/TDI`, "GPIO38/XCLKIN (TCK)" -> `GPIO38/XCLKIN/TCK`; XCLKIN, JTAG and (F2806x) USB0DP/DM are
  not GPxMUX selections and are kept in the name only.  Analog pins: `ANALOG` = ADC channel + comparator input
  (+ VREFHI), `AIO` = the AIOn function (pin_table_check accepts the AIO key).  "VSSA/VREFLO" is `kind: special`
  (a "/" name is not a supply name for pin_table_check).  No data sheet typo was found in the pin data.
  `tools/pin_table_check.py --strict`: 0 errors / 0 warnings on all 17 Piccolo files.
- **Register tables**: `tools/gen_piccolo_regs.py` (blocks the Piccolo shares with the 2833x copied from
  `tms320f28232_regs.yaml` with the documented deltas; new blocks -- SYSCTRL with CLKCTL / PLLLOCKPRD / INTOSCnTRIM /
  PCLKCR3 (and PCLKCR2, PLL2, EPWMCFG on the 2806x), NMIINT, the type-3 ADC, ADCRESULT, COMP1-3, the type-1 ePWM with
  digital compare / HRPWM type 1, GPIO with AIO, XINT1-3, DEV_EMU, BORCFG, CLA1, HRCAP, USB0 -- transcribed from the TRM
  register figures, each block citing its table / page).  Check against the data sheets:

  ```sh
  .venv/bin/python -I tools/gen_piccolo_regs.py
  pdftotext -layout docs/datasheets/ti/tms320f28027.pdf $SCRATCH/ds27.txt
  .venv/bin/python -I tools/gen_piccolo_regs.py --check-ds 2802x $SCRATCH/ds27.txt   # 379 registers, 31 PIE vectors, 0 mismatch(es)
  pdftotext -layout docs/datasheets/ti/tms320f28069.pdf $SCRATCH/ds69.txt
  .venv/bin/python -I tools/gen_piccolo_regs.py --check-ds 2806x $SCRATCH/ds69.txt   # 798 registers, 72 PIE vectors, 0 mismatch(es)
  ```

  Not in the data sheet address tables (taken from the TRMs): NMIINT, CPU timers, FLASH/CSM, DMA, USB0, DEVICECNF,
  I2CEMDR, JTAGDEBUG, the comparator ramp registers, DCFOFFSETCNT/DCFWINDOWCNT.  Document discrepancies: SPRS523 Table
  9-27 prints ePWM4 TZDCSEL at 0x98D3 (it is 0x68D3); both data sheets spell DCCAPCTL "DCCAPCT"; CLA MAR0/MAR1 at
  0x142A/0x142B in SPRS698 vs offsets 0x29/0x2A in SPRUH18 (data sheet used); SPRUI09 prints ADCCTL1.ADCENABLE as
  "R-1" (modelled read-write, reset 0) and ADCPWDN as "ADCPWN"; 2806x PCLKCR3.USB0ENCLK printed "R-0" (read-write);
  DCFOFFSET printed read-only but described as writable; GPIOLPMSEL is 32-bit (SPRUI09 Table 1-52 says 16).  Unknown
  ("x") resets are 0; AIOMUX1 resets 0x22202220 (all AIOs disabled).  ADCINT1/2 sit in PIE groups 1 and 10: the
  group-10 copies are named `ADCINT1_10` / `ADCINT2_10`.  `tests/test_c2000_piccolo_chip.py` asserts reproducibility
  and runs `--check-ds` when pdftotext is installed.
- **Boot ROM / TI OTP stand-ins**: `tms320f2802x_bootrom.asm` (InitBoot at 0x3FF4B0) and `tms320f2806x_bootrom.asm`
  (0x3FF3B0), CPU vectors at 0x3FFFC0, Device_cal at 0x3D7C80, Get_mode at 0x3D7CC0, the temperature getters (2802x
  0x3D7E80/83, 2806x 0x3D7E82/85); assembled into `boot.rom_words` (`gen_piccolo_regs.py --bootrom-words`); PARTID is the
  `ti_c2000.partid` OTP page block so that every variant can set its value.  The trim values Device_cal writes are
  placeholders (TI's factory values are per device and not public).
- **LaunchPads**: LAUNCHXL-F28027 (SPRUHH2C): LEDs GPIO0-3, button GPIO12, backchannel UART SCI-A GPIO28/29, boot
  switch GPIO34/GPIO37/TRST, no crystal.  LAUNCHXL-F28069M (SPRUI11B schematic): LEDs D9/D10 on GPIO34/GPIO39, UART
  SCI-A GPIO28/29 (JP6 on, JP7 off), CAN transceiver on GPIO30/31, no crystal.

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
- Reference manual: RM0008 Rev 21 (`docs/datasheets/st/RM0008_stm32f1_reference_manual.pdf`); the IRQ table was checked
  against Table 63 and the models against the RM (docs/LIMITATIONS.md "STM32F1"). The low-density package variants and
  the medium/high-density / connectivity-line files are described in the sections below.

## STM32F103 medium / high density and the low-density package variants (`chips/stm32f103vb.yaml`, `stm32f103ze.yaml` bases, `chips/stm32f103{t4,c4,c6,r4,r6,t8,tb,c8,cb,r8,rb,v8,rc,re,vc,ve,zc}.yaml`)

- **Dies and files** (one-level `include:` per die; variants override name, package, memory, pins):

  | die (DEV_ID, RM0008 31.6.1) | base file | variants | flash / RAM |
  |---|---|---|---|
  | low density 0x412 | `stm32f103t6.yaml` (hand-written, DS15060) | `t4` (T6 pins), `c4`, `c6`, `r4`, `r6` | x4 16K/6K, x6 32K/10K |
  | medium density 0x410 | `stm32f103vb.yaml` (LQFP100) | `t8`, `tb`, `c8` (Blue Pill), `cb`, `r8`, `rb`, `v8` | x8 64K/20K, xB 128K/20K |
  | high density 0x414 | `stm32f103ze.yaml` (LQFP144) | `rc`, `re`, `vc`, `ve`, `zc` | xC 256K/48K, xE 512K/64K |

  Part letters: T = VFQFPN36, C = LQFP48, R = LQFP64, V = LQFP100, Z = LQFP144; flash/RAM per part from the open pin
  data `<Flash>`/`<Ram>` entries. RD/VD/ZD (384K/64K) would be one line each in the generator. Blocks without pins on a
  package (SPI2/I2C2/USART3 on the medium VFQFPN36, FSMC and ports E-G on the high-density LQFP64, ports F/G on
  LQFP100) stay on the die, as their registers do on silicon.
- **Generated**, do not edit by hand: `.venv/bin/python -I tools/gen_stm32f103_variants.py` writes the 19 files,
  `--check` compares them with chips/ (`tests/test_stm32f103_variants.py` runs it) and also checks that the hand-written
  `stm32f103t6.yaml` equals the generated VFQFPN36 low-density table (0 differences: names, kinds, defaults, signal sets).
- **Pin tables**: ST's STM32_open_pin_data at commit `7d1f1514ed5583ec5007ad91236b4e1d377295b1`
  (`https://raw.githubusercontent.com/STMicroelectronics/STM32_open_pin_data/7d1f1514ed5583ec5007ad91236b4e1d377295b1/mcu/<name>`),
  cached in `svd/dl-open-pin-data-f1/` (untrusted data, xml.etree only): `mcu/<part>.xml` gives positions and signals,
  `mcu/IP/GPIO-STM32F103x{4,8,C}_gpio_v1_0_Modes.xml` says which signals need an AFIO_MAPR remap (key `REMAP`) and
  which are on the pin at reset (key `AF`). Name mapping and dropped CubeMX-only signals: see the tool's docstring
  (ADC12_INn/ADC123_INn, TIM2_CH1_ETR, MCO, FSMC_NADV for ST's FSMC_NL, TAMPER-RTC, USBDM/USBDP, OSC*, DAC_OUTx,
  BOOT1 on PB2; ADCx_EXTIn/DAC_EXTI9/FSMC_DAx dropped). A GPIO without any function (PC10-12 on the low-density
  LQFP64, PE1 on medium density, PG8/PG15) gets `additional: EXTIn` so that `pin_table_check --strict` has 0 warnings.
  Signal order inside a key is alphabetical (the data sheets order them otherwise).
  Checks: (1) every AF / REMAP entry of a remappable signal equals the RM0008 Rev 21 9.3 remap tables (Tables 34,
  43-46, 52-56; typed into the tool as `RM_REMAP`), and every RM0008 mapping whose pin exists in the package is
  listed (0 differences; the PD0/PD1 CAN remap is absent on 36/48/64 pins as Table 34 note 3 says); the SPI3 remap is
  connectivity-line only (9.3.11) and does not appear; (2) low density: positions and signal names equal DS15060
  Rev 7 Table 5 for LQFP48, LQFP64 and VFQFPN36 (`--check-ds15060 <pdftotext -layout of DS_stm32f103t6.pdf>`:
  0 differences, apart from the known PB3 SPI1_SCK omission of the data sheet); (3) equal packages of different
  densities have the same pin at every position (F103 pin compatibility); (4) shared GPIOs of one die have equal
  functions except PD0/PD1 (oscillator pins on 36/48/64 pins) and the FSMC signals absent from the LQFP64.
  Supply pins of the 36/48/64-pin packages use the DS15060 names (VDD_1 ...); LQFP100/144 keep ST's plain VDD/VSS.
  `tools/pin_table_check.py --strict`: 0 errors, 0 warnings on all 19 files.

  | file (svd/dl-open-pin-data-f1/) | sha256 |
  |---|---|
  | `GPIO-STM32F103x4_gpio_v1_0_Modes.xml` | `e0f2c51f03e100c1a614f608f31a63aaa10325d21e98c7ba81328b5f769103ef` |
  | `GPIO-STM32F103x8_gpio_v1_0_Modes.xml` | `756d917c0a3a49faa88f768432d98ec9feb1ee81e392a62bf6efaf90ff75a8dd` |
  | `GPIO-STM32F103xC_gpio_v1_0_Modes.xml` | `c5b7beba4752b7999d6664fbd6de58b9e896b7bb85d175de27210cd2306e208a` |
  | `STM32F103C(4-6)Tx.xml` | `f45d3a71b99ea360d80bdd11a119cc04ca7903f76505e27ba29c69687ce6b611` |
  | `STM32F103C(8-B)Tx.xml` | `f3ba50d99567684e1aebcf644e6e9e4e09fcd3e96e8270a3f533d0072ccfbdcd` |
  | `STM32F103R(4-6)Tx.xml` | `daabd13cc9fa114ac36277b42478755cc3a6b6b60afd9bceb09a4493050290e8` |
  | `STM32F103R(8-B)Tx.xml` | `3e7bfa16a4f56e8f1b9370ca6838dd3bbef94c5beed6389da0d8e41d63603682` |
  | `STM32F103R(C-D-E)Tx.xml` | `16795da02748f176a76aa2bdbf1302e6b4bad8bc5241593cfc3746449017ad7a` |
  | `STM32F103T(4-6)Ux.xml` | `34772df1c2b007b4dd242bf7c998db40d72eb5b1ee30c68de2e4a8680988a892` |
  | `STM32F103T(8-B)Ux.xml` | `168fb15e750cf953844ebe05f50755c1d9eb3ad5c09ecd5aeb3d5e74b75bedae` |
  | `STM32F103V(8-B)Tx.xml` | `517a0b83b7bffe3aeb7ce0d92f20afada387c69cdf459aaa12982ebdab3c09f0` |
  | `STM32F103V(C-D-E)Tx.xml` | `d9f4aa691bb5968c8f2bf9a42a456701c4f13004fd568efe912defb8fb59f41c` |
  | `STM32F103Z(C-D-E)Tx.xml` | `117b495fd9d088e6c22657d95f6f63c2688c7ce567d4c7c8a3ebbb435768edcb` |

  The same directory also caches the STM32F101/F102 files of the same commit (not used yet; sha256 via `sha256sum`).
- **Data sheets**: DS5319 (STM32F103x8/xB) and DS5792 (STM32F103xC/xD/xE) could not be downloaded (st.com answered
  with an HTTP/2 stream error, one plain attempt each); the medium/high-density tables therefore rest on ST's open pin
  data + RM0008 only. Drop `DS_stm32f103c8.pdf` (DS5319) and `DS_stm32f103ze.pdf` (DS5792) into docs/datasheets/st/
  for a later Table 5 cross-check like the DS15060 one.
- **Memory map, IRQs, peripherals**: RM0008 Rev 21 3.3 Table 3 (bases), 10.1.2 Table 63 (medium density: vectors
  0-42 incl. 30 TIM4, 33/34 I2C2, 36 SPI2, 39 USART3; high density: 60 vectors, 43-59 TIM8_BRK ... DMA2_Channel4_5;
  CMSIS names, SVD names in `svd_irq_names`), 3.3.3 Tables 5/6 (flash pages 1 KB / 2 KB: `stm32f1.flash`
  `page_size`), 6.4.1 (BKP DR1-DR42 on high density: `data_regs: 42`), 10.1.1 (`systick_calib: 9000`), 31.6.1
  (`stm32f1.dbgmcu density`: IDCODE 0x20036410 / 0x10036414), `stm32f1.afio density` (high-density remaps). Reused
  F4 models with F1 parameters: `stm32.dac` (`f1: true`: no DAC_SR / DMAUDR / DAC interrupt, RM0008 12.3.7, 12.5.1),
  `stm32.sdio` (`f1: true`: SDIOCLK = HCLK, RM0008 22.2), `stm32.fsmc` (AHBENR gate), `stm32.spi` (`i2s: true,
  i2s_clock: sysclk` on SPI2/SPI3 high density), `reserved_zero: true` on TIM/USART/SPI/I2C.
- **Register table**: the existing `stm32f103_regs.yaml` (STM32F103xx.svd is a superset with every high-density block);
  no regeneration was needed. `svd2chip --check` against `svd/dl-stm32f103/STM32F103xx.svd`: 0 mismatches for all 19
  files (NOTEs: USB_CAN_SRAM, SYSMEM, OB, NVIC; OMITTED the XL-only TIM9-14 and, on medium density, the high-density
  blocks; RENAMED IRQs). SVD quirk found: SDIO_RESP1 is spelled `RESPI1` (RM0008 22.9.6 SDIO_RESP1 at 0x14);
  `stm32.sdio` renames it.
- **Other SVDs cached** (not used by any chip file yet): cmsis-svd-data `c65f8551e57c770344d229dcaa0bf838fa29aff4`
  `data/STMicro/STM32F101xx.svd` -> `svd/dl-stm32f101/` (sha256
  `97d41462f14a3208b0c896226dc50853ddb828c41ffcebb3896dfee52d110b66`), `STM32F102xx.svd` -> `svd/dl-stm32f102/`
  (sha256 `4cfe9fecfae97784f6232c37e4ff17f114842fdfedc04cf6434a286adf1a2e28`).
- **Samples / boards / tests**: `firmware/samples_stm32f1_variants/` (`build.sh`, `startup_f1x.c` with the 60
  vectors, `f1x.h` on top of `../samples_stm32f1/stm32f103.h`, `link_64k_20k.ld`, `link_256k_48k.ld`): `bluepill`,
  `hd_uart_tim`, `hd_dac_adc`, `hd_spi_i2c`, `hd_sdio`, `hd_fsmc`; boards `stm32f103c8_stm32f405_uart.yaml`,
  `stm32f103ze_{spi_i2c,sdcard,fsmc_sram}.yaml`; tests `test_stm32f103_variants.py`, `test_stm32f103xx_chips.py`,
  `test_stm32f1_hd_samples.py`.

## STM32F105/F107 connectivity line (`chips/stm32f107vc.yaml` base, `stm32f107rc.yaml`, `stm32f105rc.yaml`, `stm32f105vc.yaml`, `stm32f107_regs.yaml`)

- **Die**: DEV_ID 0x418 (RM0008 31.6.1; REV_ID 0x1001 rev Z, `stm32f1.dbgmcu` `density: cl`), Cortex-M3 72 MHz, 256 KB
  flash (2 KB pages), 64 KB SRAM, 68 vectors (RM0008 10.1.2 Table 61; 43-49 reserved), Ethernet MAC (F107 only), USB
  OTG FS, two bxCANs (28 filter banks), I2S2/I2S3 with the PLL3 audio clock, DMA1 (7) + DMA2 (5 channels), TIM1-7,
  USART1-3, UART4/5, SPI1-3, I2C1/2, ADC1/2, DAC, BKP DR1-42; no FSMC, SDIO, TIM8, ADC3, USB device or USB/CAN SRAM.
  `stm32f107vc.yaml` (LQFP100) is the base; the others `include:` it (one level) and override name, package, pins.
  The Ethernet MAC (`ETH`, `stm32f1.eth`) carries `params: {only: [STM32F107]}` (an inactive stub on the F105).
  I2C2 is instantiated on every part (it is in both SVDs and on the die); ST's open pin data gives the F107 no I2C2
  pins (PB10/PB11 carry ETH_MII_RX_ER / TX_EN), so the F107 tables have no I2C2 function.
- **Data sheet**: DS6014 (STM32F105xx/STM32F107xx) could not be downloaded (one plain attempt to st.com,
  `https://www.st.com/resource/en/datasheet/stm32f107vc.pdf`: HTTP/2 stream error, no data).  Nothing in these files
  was read from it.  The owner should drop it in as `docs/datasheets/st/DS_stm32f107xx.pdf`; then cross-check the pin
  tables (Table 5), the power-pin suffixes (VSS_1.. / VDD_1.., which the tables leave out: they keep the open pin
  data names `VSS` / `VDD`) and the PLL2/PLL3 input/output ranges, which `stm32f1.rcc_cl` does not check.
- **Reference manual**: RM0008 Rev 21 (`docs/datasheets/st/RM0008_stm32f1_reference_manual.pdf`): ch. 8 (connectivity
  line RCC, `stm32f1.rcc_cl`), 9.3 / 9.4.2 (AFIO remaps, connectivity-line MAPR: `REMAP_CL` in
  `chipem/periph/stm32f1/cl.py`, `stm32f1.afio` `density: cl`), 10.1.2 Table 61, 10.2.5 (EXTI line 18 OTG FS wake-up,
  line 19 Ethernet wake-up: `stm32f1.exti` `lines: 20`), 13.3.7 (DMA requests), ch. 24 (bxCAN), ch. 28 (OTG_FS),
  ch. 29 (Ethernet), 10.1.1 (SYST_CALIB = 9000).
- **Register table** `chips/stm32f107_regs.yaml`: CMSIS-SVD `STM32F107xx.svd` (STMicro, `<version>1.2</version>`),
  cmsis-svd-data commit `c65f8551e57c770344d229dcaa0bf838fa29aff4` (fetched 2026-11-10):
  <https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F107xx.svd>
  (sha256 `f9a5937f9cb27a5773e43b4e5313e7824698aba4ccca861282e9b165c54812ca`, cached in `svd/dl-stm32f107/`).
  `STM32F105xx.svd` (same commit, sha256 `2bdd3a94179e1d8a223f6455ff297933a25947e1dde36f495bfb8d70ce76f144`, cached in
  `svd/dl-stm32f105/`) is the F107 file without the four ETHERNET blocks (every other register identical), so the
  F105 files use the F107 table too.
  ```
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f107/STM32F107xx.svd --flat -o chips/stm32f107_regs.yaml \
      --source "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/c65f8551e57c770344d229dcaa0bf838fa29aff4/data/STMicro/STM32F107xx.svd (SVD version 1.2)"
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f107/STM32F107xx.svd --check chips/stm32f107vc.yaml   # and stm32f107rc
  .venv/bin/python -I tools/svd2chip.py svd/dl-stm32f105/STM32F105xx.svd --check chips/stm32f105rc.yaml   # and stm32f105vc
  ```
  `--check`: 0 mismatches for all four files (NOTEs: OTG_FS / SYSMEM / OB not in the SVD, ETH named ETHERNET_MAC
  there, the USB_OTG_* sub-blocks, IRQ 42 OTG_FS_WKUP missing from the SVD, on the F105 IRQs 61/62).
- **SVD quirks** (fixed in the models or the chip YAML, never in the table): every interrupt is named with an `_IRQ`
  suffix (`ADC_IRQ`, `RTCAlarm_IRQ` ...; the YAML uses the CMSIS stm32f107xc.h names and lists the SVD ones in
  `svd_irq_names`); no OTG_FS_WKUP (42); the bxCAN registers are prefixed (`CAN_MCR` ...): `stm32f1.can_cl` uses the
  F405 table's CAN1/CAN2 layout; Ethernet: MACA2HR reset 0x50 (RM 0x0000FFFF), MACA0HR 0x0010FFFF (RM 0x8000FFFF),
  MMCCR.MCF at bit 31 (RM bit 3) -- fixed in `stm32f1.eth`; RCC APB1RSTR calls bits 19/20 USART4RST/USART5RST.
- **Pin tables**: generated from ST's STM32_open_pin_data at commit `7d1f1514ed5583ec5007ad91236b4e1d377295b1`
  (`mcu/STM32F107V(B-C)Tx.xml`, `STM32F107R(B-C)Tx.xml`, `STM32F105R(8-B-C)Tx.xml`, `STM32F105V(8-B-C)Tx.xml` for
  positions and signals; `mcu/IP/GPIO-STM32F107_gpio_v1_0_Modes.xml` for default vs remapped functions), cached in
  `svd/dl-open-pin-data-f10x-cl/` (untrusted data, xml.etree only):
  ```
  .venv/bin/python -I tools/opd_stm32f1_cl_pins.py "svd/dl-open-pin-data-f10x-cl/STM32F107V(B-C)Tx.xml" \
      svd/dl-open-pin-data-f10x-cl/GPIO-STM32F107_gpio_v1_0_Modes.xml        # the `pins:` rows of stm32f107vc.yaml
  .venv/bin/python -I tools/opd_stm32f1_cl_pins.py svd/dl-open-pin-data-f10x-cl/GPIO-STM32F107_gpio_v1_0_Modes.xml --remaps
  .venv/bin/python -I tools/pin_table_check.py chips/stm32f10[57][rv]c.yaml --strict      # 0 errors, 0 warnings each
  ```
  The output equals the files except PE1 (no alternate function), which gets `additional: EXTI1` by hand so that
  `--strict` has no "gpio pin without functions" warning.  Conventions: Ethernet signal names are RM0008's
  (`ETH_MII_*` / `ETH_RMII_*`, both where a pin serves MII and RMII; `ETH_MDC`, `ETH_MDIO`, `ETH_PPS_OUT`), the open
  pin data's `USB_OTG_FS_*` are `OTG_FS_*` (DM/DP/VBUS under `additional`: RM0008 9.1.11 Table 30), `RCC_MCO` is
  `MCO`, TIM2_CH1 + TIM2_ETR are merged into `TIM2_CH1_ETR`, ADC1/ADC2 channels into `ADC12_INx`; ETH_PPS_OUT is a
  REMAP of PB5 (MAPR.PTP_PPS_REMAP, 9.4.2: the XML lists it as default).  The remap blocks of the modes XML equal
  RM0008 Tables 34, 35, 43-46, 52-58 (the XML numbers its blocks 0/1/2, not by MAPR field value: CAN1 "REMAP1" =
  CAN1_REMAP 10, "REMAP2" = 11).  Checks: every position / type equals the MCU XML (`tests/test_stm32f10x_cl_chip.py`),
  shared GPIOs have identical functions between the LQFP64 and LQFP100 file of each part.  Not done: STM32F107VCHx
  (LFBGA100) and STM32F105V(8-B)Hx, whose XMLs are cached too.

  | file (svd/dl-open-pin-data-f10x-cl/) | sha256 |
  |---|---|
  | `GPIO-STM32F107_gpio_v1_0_Modes.xml` | `12fc1461bd276647ca72669f74ebdadd15e08a37974a49e17828c5b4a144490e` |
  | `STM32F105R(8-B-C)Tx.xml` | `0862380b59d207d715a4bd39039234fade47eca41412793db9206cee52cdfb6a` |
  | `STM32F105V(8-B-C)Tx.xml` | `1e09c5c1e19eb8f2dd164cdf2f8b3984b52d487f0434b57d954137c2e7448b6c` |
  | `STM32F105V(8-B)Hx.xml` | `4bee237e4cc476f23b5a8774870e52115215a95562569289d6a9f001452d8e25` |
  | `STM32F107R(B-C)Tx.xml` | `bd1c8e79159136479534acf5db195134b3d615121337f3c074aeecf0667c181f` |
  | `STM32F107V(B-C)Tx.xml` | `bb9eeab9640d05e4b14747f463972682a9ffc69b21beb70c6372a7c9c9ee9741` |
  | `STM32F107VCHx.xml` | `aab963b1c1faadfc2684277b7fa87153c0004c5fe58c32ad85e1fda828ab299a` |

- **Samples / boards / tests**: `firmware/samples_stm32f1_cl/` (`build.sh`; `uart_echo`, `eth_ping` (F107),
  `usb_cdc` (F105), `can_dual`, `dma2_m2m`), `boards/stm32f107vc_eth_ping.yaml`, `boards/stm32f105rc_usb_cdc.yaml`,
  `boards/stm32f105rc_dual_can.yaml`; `tests/test_stm32f10x_cl_chip.py`, `tests/test_stm32f1_rcc_cl.py`,
  `tests/test_stm32f10x_cl_samples.py`.

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
- Interrupts: Timer 2 at 002Bh and the polling order of Fig. 25 (PCA, UART/SPI, T2), not Table 41's 003Bh row
  (reasons in docs/MCS51_CORE.md "Timer 2 vector").

## Generic 80C52 (`chips/i80c52.yaml`, `i80c52_regs.yaml`) - not from a data sheet

- A stand-in for running arbitrary 8051/8052 firmware: the classic 80C52 SFR set (P0-P3, Timers 0/1/2 with T2MOD,
  UART with SADDR/SADEN, PCON, IE/IP with two levels), 256 B IRAM, 64 KB code, standard DIP40 pinout, written from the
  public Intel MCS-51 architecture.  `i80c52_regs.yaml` was derived from `p89cv51_regs.yaml` by a script keeping the
  80C52 registers.  `tools/pin_table_check.py`: 0 errors.
