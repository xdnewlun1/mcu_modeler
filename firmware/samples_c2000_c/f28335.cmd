/* f28335.cmd -- TI linker command file for the TMS320F28335 C samples (flash build, COFF or EABI, --float_support=fpu32).
 *
 * Memory map (SPRS439Q Figure 8-23, Table 8-22): 256K x 16 flash at 0x300000-0x33FFFF (sectors H..A, 32K each),
 * 34K x 16 SARAM = M0 + M1 + L0-L7 (L0-L3 secure and dual-mapped at 0x3F8000, L4-L7 DMA-accessible).  Addresses and
 * lengths are 16-bit words.  PAGE 0 = program, PAGE 1 = data (the pages only keep the linker from overlapping them).
 * Sections: codestart at BEGIN = 0x33FFF6 (boot-to-flash entry), code / constants / init tables in flash, .stack in
 * M1 (SP = 0x400 after reset; the COFF ABI needs the stack below 0x10000), variables in L4-L5, heap in L6.
 * `ramfuncs` (time-critical code copied by nothing: linked to run from L0 and loaded there directly by the image)
 * is not used by the samples. */

MEMORY
{
PAGE 0:    /* program */
   RAML0      : origin = 0x008000, length = 0x001000     /* L0 SARAM 4K (also at 0x3F8000) */
   RAML1      : origin = 0x009000, length = 0x001000     /* L1 SARAM 4K */
   FLASH      : origin = 0x300000, length = 0x03FF80     /* sectors H..A (32K each), less the CSM area */
   CSM_RSVD   : origin = 0x33FF80, length = 0x000076     /* program with 0x0000 when the CSM is used */
   BEGIN      : origin = 0x33FFF6, length = 0x000002     /* boot-to-flash entry: LB code_start */
   CSM_PWL    : origin = 0x33FFF8, length = 0x000008     /* CSM password */
   OTP        : origin = 0x380400, length = 0x000400     /* user OTP 1K */
   ADC_CAL    : origin = 0x380080, length = 0x000009     /* TI's ADC_cal routine (factory OTP) */
   RESET      : origin = 0x3FFFC0, length = 0x000002     /* boot ROM reset vector (not loaded: DSECT) */

PAGE 1:    /* data */
   BOOT_RSVD  : origin = 0x000000, length = 0x000050     /* M0 words used by the boot ROM's stack */
   RAMM0      : origin = 0x000050, length = 0x0003B0     /* M0 SARAM (rest) */
   RAMM1      : origin = 0x000400, length = 0x000400     /* M1 SARAM 1K: SP = 0x400 after reset */
   RAML2      : origin = 0x00A000, length = 0x001000     /* L2 SARAM 4K */
   RAML3      : origin = 0x00B000, length = 0x001000     /* L3 SARAM 4K */
   RAML4      : origin = 0x00C000, length = 0x001000     /* L4 SARAM 4K */
   RAML5      : origin = 0x00D000, length = 0x001000     /* L5 SARAM 4K */
   RAML6      : origin = 0x00E000, length = 0x001000     /* L6 SARAM 4K */
   RAML7      : origin = 0x00F000, length = 0x001000     /* L7 SARAM 4K */
   PIEVECT    : origin = 0x000D00, length = 0x000100     /* PIE vector table (filled at run time) */
}

SECTIONS
{
   codestart        : > BEGIN,     PAGE = 0
   .text            : > FLASH,     PAGE = 0
   .cinit           : > FLASH,     PAGE = 0
   .switch          : > FLASH,     PAGE = 0
   .stack           : > RAMM1,     PAGE = 1
   .reset           : > RESET,     PAGE = 0, TYPE = DSECT   /* RTS reset vector: not used, not loaded */
   csm_rsvd         : > CSM_RSVD,  PAGE = 0
   csmpasswds       : > CSM_PWL,   PAGE = 0

#if defined(__TI_EABI__)
   .init_array      : > FLASH,     PAGE = 0
   .const           : > FLASH,     PAGE = 0
   .bss             : > RAML4,     PAGE = 1
   .bss:output      : > RAML4,     PAGE = 1
   .data            : > RAML5,     PAGE = 1
   .sysmem          : > RAML6,     PAGE = 1
#else
   .pinit           : > FLASH,     PAGE = 0
   .econst          : > FLASH,     PAGE = 0
   .ebss            : > RAML4,     PAGE = 1
   .esysmem         : > RAML6,     PAGE = 1
#endif
}
