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
| `st/RM0090_stm32f4_reference_manual.pdf` | RM0090 Rev 22, STM32F405/415/407/417/427/437/429/439 reference manual | STM32 peripheral models; F42x/F43x: RCC ch. 6, PWR 5.5, dual-bank flash 3.9, DMA2D ch. 11, SAI ch. 29, FMC ch. 37, CRYP GCM/CCM ch. 23, HASH ch. 25, vector Table 63, DMA Tables 43/44 |
| `st/DS_stm32f427vg.pdf` | DS9405 Rev 14, STM32F427xx/429xx data sheet | `chips/stm32f427*.yaml` pin tables (Table 10, Table 12 AF0-15), ordering code, frequency limits (Table 17), over-drive switch times (Table 23) |
| `st/DS_stm32f437ai.pdf` | DS9484 Rev 14, STM32F437xx/439xx data sheet | `chips/stm32f437*.yaml` (same Tables 10/12 as DS9405, UFBGA169 ballout Figure 16) |
| `st/DS_stm32f103t6.pdf` | DS15060 Rev 7, STM32F103x4/x6 low-density performance line data sheet | `chips/stm32f103t6.yaml` (Table 5 pins, Table 2 resources, Figure 8 memory map); RM0008 (the F1 reference manual) is not in the repo |
| `ti/tms320f28232.pdf` | SPRS439Q, TMS320F2833x/2823x data sheet | `chips/tms320f28232.yaml` |
| `ti/tms320f2808.pdf` | SPRS230P, TMS320F280x/C280x/F2801x data sheet, Rev. P | `chips/tms320f2808.yaml` pin table (Table 7-1, Table 9-17, Figure 7-1), memory map (Figure 9-19, Table 9-19), PIE table (Table 9-28), boot modes (Table 9-1); `chips/tms320f2808_regs.yaml` check (Tables 9-2..9-31, `tools/gen_tms320f2808_regs.py --check-ds`); 280x guides SPRU712/716/721/722/790/791/807/924, SPRUFK7, SPRUG72, SPRUEU0 in `svd/c28x-docs/dl4-dl17` |
| `ti/tms320vc5502.pdf` | TMS320VC5502 data sheet, Rev K (C55x DSP) | chip not supported yet |

The C28x CPU, assembler and peripheral guides (SPRU430, SPRU514, SPRUEO2, ...) are under `svd/c28x-docs/` together
with their extracted text; `docs/C28X_CORE.md` cites them by that path. New data sheets go in the vendor directory
here, named after the part (`DS_<part>.pdf` for ST, the TI/NXP literature name otherwise).

## NXP P89CV51RB2/RC2/RD2

`nxp/P89CV51RB2_RC2_RD2.pdf`: data sheet Rev. 03 (25 Aug 2009), 76 pages: pins (Table 3), SFRs (Table 4), memory,
flash/IAP (Tables 10-12), timers, UART, SPI, WDT, PCA, interrupts (Table 41), power modes, CKCON (Tables 50-52).
Used for chips/p89cv51*.yaml and chips/p89cv51_regs.yaml (docs/MCS51_CORE.md).  The 8051 instruction set itself is
not in it (Intel MCS-51 user's manual / NXP 80C51 programmer's guide; not stored here).
