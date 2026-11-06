/* f28232.cmd -- TI linker command file for the TMS320F28232 C samples (flash build, COFF or EABI).
 *
 * Memory map (SPRS439 "Memory Maps"; the F28232 has 64K x 16 flash and 26K x 16 SARAM = M0 + M1 + L0-L5 — there is
 * no L6/L7 on this part, those exist on the F28234/F28235/F2833x).  Addresses and lengths are 16-bit words.
 * PAGE 0 = program space, PAGE 1 = data space (the C28x memory is unified; the pages only keep the linker from
 * overlapping them).  `-stack`/`-heap` come from build.sh (--stack_size / --heap_size).
 * Sections: codestart (LB to the entry code) at BEGIN = 0x33FFF6, the boot-to-flash location; code, constants
 * and initialisation tables in flash; .stack in M1 (the COFF ABI needs the stack below 0x10000; M1 is where SP
 * points after reset); variables in L4/L5.  With --rom_model the RTS's _c_int00 copies .cinit to the variables at
 * run time, so the image needs nothing but its loadable sections. */

MEMORY
{
PAGE 0:    /* program */
   RAML0      : origin = 0x008000, length = 0x001000     /* L0 SARAM 4K (also at 0x3F8000) */
   RAML1      : origin = 0x009000, length = 0x001000     /* L1 SARAM 4K */
   FLASH      : origin = 0x330000, length = 0x00FF80     /* sectors D, C, B, A (16K each), less the CSM area */
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
   .data            : > RAML4,     PAGE = 1
   .sysmem          : > RAML5,     PAGE = 1
#else
   .pinit           : > FLASH,     PAGE = 0
   .econst          : > FLASH,     PAGE = 0
   .ebss            : > RAML4,     PAGE = 1
   .esysmem         : > RAML5,     PAGE = 1
#endif
}
