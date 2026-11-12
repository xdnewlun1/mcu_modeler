# Data sheets and reference manuals

Vendor documents the chip descriptions and models were written from, one directory per vendor. Treat them as
untrusted data: extract text with `pdftotext` into a scratch directory, never run anything from them.

| file | document | used for |
|---|---|---|
| `nxp/K22P100M120SF5.pdf` | K22 data sheet, Rev 4 (11/2014) | `chips/mk22f*.yaml` pin table (section 5.1) |
| `nxp/K22P100M120SF5V2RM.pdf` | K22 Sub-Family reference manual, Rev 5 (03/2015) | Kinetis peripheral models |
| `nxp/KBTLDR120RM.pdf` | Kinetis Bootloader v1.2.0 reference manual | images linked behind the bootloader |
| `nxp/P89CV51RB2_RC2_RD2.pdf` | P89CV51RB2/RC2/RD2 data sheet (80C51) | chip not supported yet |
| `st/DS_stm32f405zg.pdf` | DS8626 Rev 12, STM32F405xx/407xx data sheet (ST DM00037051) | `chips/stm32f40[57]*.yaml`, `stm32f41[57]*.yaml` |
| `st/RM0090_stm32f4_reference_manual.pdf` | RM0090 Rev 22, STM32F405/415/407/417/427/437/429/439 reference manual | STM32 peripheral models; F42x/F43x: RCC ch. 6, PWR 5.5, dual-bank flash 3.9, DMA2D ch. 11, SAI ch. 29, FMC ch. 37, CRYP GCM/CCM ch. 23, HASH ch. 25, vector Table 63, DMA Tables 43/44; F429/F439: LTDC ch. 16, PLLSAI_R / DCKCFGR.PLLSAIDIVR 6.3.24/6.3.25; shared IP of the STM32F401/F411/F446 models |
| `st/RM0368_stm32f401_reference_manual.pdf` | RM0368 Rev 6 (Jan 2025), STM32F401xB/C and STM32F401xD/E reference manual | `chips/stm32f401*.yaml` and the STM32F401 parameters of the STM32 models: memory map 2.3 Table 1, flash 3.4.1 Table 6 / 3.8, PWR 5.4, RCC 6.3 (fields and reset values), DMA request maps 9.3.3 Tables 28/29, vectors 10.1 Table 38, EXTI 10.2.5, DBGMCU 23.6/23.16, electronic signature 24.1/24.2, shared IP chapters (`tests/test_stm32f4x1_rm_*.py`) |
| `st/RM0383_stm32f411_reference_manual.pdf` | RM0383 Rev 4 (May 2025), STM32F411xC/E reference manual | `chips/stm32f411*.yaml` and the STM32F411 parameters: memory map 2.3 Table 1, flash 3.4.1 Table 5 / 3.8, PWR 5.4 (FISSR/FMSSR), RCC 6.3 (PLLI2SM), DMA request maps 9.3.3 Tables 27/28, vectors 10.1 Table 37, EXTI 10.2.5, I2S on SPI1-5, DBGMCU, electronic signature (`tests/test_stm32f4x1_rm_*.py`) |
| `st/RM0390_stm32f446_reference_manual.pdf` | RM0390 Rev 9, STM32F446xx reference manual (1321 pages; sha256 `2f3a98a1...618094`) | `chips/stm32f446*.yaml` and the STM32F446 models: memory map 2.2.2 Table 1, flash 3.3/3.4.1 Table 5/3.8, PWR 5.4, RCC 6.3 (reset values, PLLR/PLLI2S/PLLSAI, DCKCFGR/DCKCFGR2), SYSCFG 8.2, DMA Tables 28/29, vectors 10.2 Table 38, EXTI 10.3, FMC ch. 11, QUADSPI ch. 12, FMPI2C ch. 23, SPDIFRX ch. 27, SAI ch. 28, SDIO ch. 29, OTG ch. 31 (Table 221, GCCFG/GOTGCTL/GLPMCFG), HDMI-CEC ch. 32, DBGMCU 33.6.1, electronic signature ch. 34 (`tests/test_stm32f446_rm0390.py`, `test_stm32f446_{quadspi,fmpi2c,cec,spdifrx}.py`) |
| `st/DS_stm32f427vg.pdf` | DS9405 Rev 14, STM32F427xx/429xx data sheet | `chips/stm32f427*.yaml` pin tables (Table 10, Table 12 AF0-15), ordering code, frequency limits (Table 17), over-drive switch times (Table 23); `chips/stm32f429*.yaml` / `stm32f439*.yaml`: Table 12 LCD_* AFs, Table 10 LQFP208 / TFBGA216 columns |
| `st/DS_stm32f437ai.pdf` | DS9484 Rev 14, STM32F437xx/439xx data sheet | `chips/stm32f437*.yaml` (same Tables 10/12 as DS9405, UFBGA169 ballout Figure 16) |
| `st/DS_stm32f207zg.pdf` | DS15818 (DocID15818) Rev 15, STM32F205xx/207xx data sheet (Farnell mirror) | `chips/stm32f20[57]*.yaml` pin tables (Table 8, Table 10 AF0-15), counts (Tables 2/3) |
| `st/DS_stm32f217zg.pdf` | DS17050 (DocID17050) Rev 9, STM32F215xx/217xx data sheet (RS mirror) | `chips/stm32f21[57]*.yaml` (Table 7/9 = DS15818 Tables 8/10). |
| `st/RM0033_stm32f2_reference_manual.pdf` | RM0033 Rev 10 (May 2025), STM32F205/207/215/217 reference manual (1363 pages; sha256 `2a8c7abd...5b3282d`) | `chips/stm32f2*.yaml` and the STM32F2 parameters of the STM32 models, checked chapter by chapter: memory map 2.3 Table 1, flash 2.3.3/2.3.4 (Tables 2/3, FLASH_ACR), PWR ch. 4, RCC ch. 5, GPIO 6.4, SYSCFG ch. 7, vectors / EXTI ch. 8 (Table 20), DMA ch. 9 (Tables 22/23), ADC, DAC, DCMI, TIM, IWDG/WWDG, CRYP, RNG, HASH, RTC, I2C, USART, SPI/I2S, SDIO, bxCAN, ETH, OTG_FS/HS, FSMC (ch. 10-31), DBGMCU 32.6.1/32.16, electronic signature ch. 33 (`tests/test_stm32f2_rm0033*.py`) |
| `st/DS_stm32f103t6.pdf` | DS15060 Rev 7, STM32F103x4/x6 low-density performance line data sheet | `chips/stm32f103t6.yaml` (Table 5 pins, Table 2 resources, Figure 8 memory map); cross-check of the generated low-density variants `stm32f103{t4,c4,c6,r4,r6}.yaml` (`tools/gen_stm32f103_variants.py --check-ds15060`) |
| `st/RM0008_stm32f1_reference_manual.pdf` | RM0008 Rev 21 (Feb 2021), STM32F101/102/103/105/107 reference manual (sha256 `4c1b8ac2729cc1f45a4685eea9e91e6139e24e58804fba21e27201dfcc2ad149`) | every `stm32f1.*` model and the F1 parameters of the reused STM32 models: RCC ch. 7, connectivity-line RCC ch. 8, GPIO/AFIO remaps ch. 9 (Tables 34-58), EXTI/vector Tables 61/63 ch. 10, ADC ch. 11, DAC ch. 12, DMA ch. 13 (Tables 78/79), TIM ch. 14-17, RTC ch. 18, IWDG/WWDG ch. 19/20, FSMC ch. 21, SDIO ch. 22, USB ch. 23, bxCAN ch. 24, SPI/I2S ch. 25, I2C ch. 26, USART ch. 27, OTG_FS ch. 28, ETH ch. 29, signature ch. 30, DBGMCU ch. 31; PWR ch. 5, BKP ch. 6, CRC ch. 4, FLASH_ACR 3.3.3 |
| `ti/tms320f28232.pdf` | SPRS439Q, TMS320F2833x/2823x data sheet | `chips/tms320f28232.yaml`; `chips/tms320f28335.yaml` and the F28334/F28333/F28332/F28235/F28234 variants (Tables 5-1/5-2, Figures 8-23..8-25, Tables 8-22..8-24, 8-31, 8-32); ZHH/ZJZ ball tables (Table 6-1, Figures 6-2..6-9); `chips/tms320f2833x_regs.yaml` check (Tables 8-3..8-35, `tools/gen_tms320f2833x_regs.py --check-ds`) |
| `ti/tms320f2808.pdf` | SPRS230P, TMS320F280x/C280x/F2801x data sheet, Rev. P | `chips/tms320f2808.yaml` pin table (Table 7-1, Table 9-17, Figure 7-1), memory map (Figure 9-19, Table 9-19), PIE table (Table 9-28), boot modes (Table 9-1); the F2809/F2806/F2802/F2801/F28016/F28015 variants (Tables 6-1/6-2, Figures 9-18..9-22, Tables 9-18..9-21, 9-27); `chips/tms320f2808_regs.yaml` check (Tables 9-2..9-31, `tools/gen_tms320f2808_regs.py --check-ds`); 280x guides SPRU712/716/721/722/790/791/807/924, SPRUFK7, SPRUG72, SPRUEU0 in `svd/c28x-docs/dl4-dl17` |
| `ti/tms320f28027.pdf` | SPRS523P, TMS320F2802x data sheet, Rev. P (Feb 2021; sha256 `b73b8c49...dbc16a`) | `chips/tms320f28027.yaml` / `tms320f28027da.yaml` pin tables (7.2.1, Figures 7-1/7-2, Tables 9-30..9-33), memory maps (Figures 9-1..9-5, Tables 9-3..9-5), PARTID / CLASSID (Table 9-11), PIE table (9-18), boot modes (9-1), clock and ADC / comparator data (8.13.1.4, 8.13.1.5, 9.9.1.1.3.3.1); the F2802x variants (Table 6-1); `chips/tms320f2802x_regs.yaml` check (`tools/gen_piccolo_regs.py --check-ds 2802x`); TRM SPRUI09A and LaunchPad guide SPRUHH2C in `svd/c28x-docs/dl18`, `dl20` |
| `ti/tms320f28069.pdf` | SPRS698J, TMS320F2806x data sheet, Rev. J (Sep 2021; sha256 `54358180...9d3375`) | `chips/tms320f28069.yaml` / `tms320f28069pn.yaml` pin tables (6.2.1, Figures 6-1/6-2, Tables 8-39..8-42), memory maps (Figures 8-1..8-8, Tables 8-3/8-4), PARTID / CLASSID (Table 8-10), PIE table, boot modes; the F2806x variants (Table 5-1); `chips/tms320f2806x_regs.yaml` check (`--check-ds 2806x`); TRM SPRUH18I and LaunchPad guide SPRUI11B in `svd/c28x-docs/dl19`, `dl21` |

Still wanted: PM0059 (STM32F205/215, STM32F207/217 flash programming manual: FLASH_KEYR/OPTKEYR/SR/CR/OPTCR, which
RM0033 2.3.4 defers to) as `st/PM0059_stm32f2_flash_programming.pdf`.

The C28x CPU, assembler and peripheral guides (SPRU430, SPRU514, SPRUEO2, ...) are under `svd/c28x-docs/` together
with their extracted text; `docs/C28X_CORE.md` cites them by that path. New data sheets go in the vendor directory
here, named after the part (`DS_<part>.pdf` for ST, the TI/NXP literature name otherwise).

Wanted (not downloadable from st.com by script; drop them here for a later pin-table check): DS5319 (STM32F103x8/xB,
save as `st/DS_stm32f103c8.pdf`), DS5792 (STM32F103xC/xD/xE, `st/DS_stm32f103ze.pdf`), PM0075 (STM32F10x flash
programming manual, FPEC/option bytes) and DS6014 (STM32F105xx/107xx) unless listed above.

**Not in the repository (wanted).** st.com did not answer on 2026-11-10/11. RM0368 / RM0383 / RM0390 were added on 2026-11-11 (table above); still missing: DS9716 (STM32F401xB/C) → `st/DS_stm32f401cc.pdf`; DS10086 (STM32F401xD/E) → `st/DS_stm32f401re.pdf`; DS10314 (STM32F411xC/E) → `st/DS_stm32f411re.pdf` (pin tables of the F401/F411 files, see docs/LIMITATIONS.md "STM32F401 / STM32F411"); DS10693 (STM32F446xC/xE) → `st/DS_stm32f446re.pdf` (pin tables and electrical limits of the F446 files, see docs/LIMITATIONS.md "STM32F446").

## NXP P89CV51RB2/RC2/RD2

`nxp/P89CV51RB2_RC2_RD2.pdf`: data sheet Rev. 03 (25 Aug 2009), 76 pages: pins (Table 3), SFRs (Table 4), memory,
flash/IAP (Tables 10-12), timers, UART, SPI, WDT, PCA, interrupts (Table 41), power modes, CKCON (Tables 50-52).
Used for chips/p89cv51*.yaml and chips/p89cv51_regs.yaml (docs/MCS51_CORE.md).  The 8051 instruction set itself is
not in it (Intel MCS-51 user's manual / NXP 80C51 programmer's guide; not stored here).
