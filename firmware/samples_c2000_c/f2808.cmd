/* f2808.cmd -- TI linker command file for the TMS320F2808 C samples (flash build, COFF or EABI).
 *
 * Memory map (SPRS230P Figure 9-19, Table 9-19): 64K x 16 flash at 0x3E8000 (sectors D, C, B, A), 18K x 16 SARAM =
 * M0 + M1 (1K each) + L0, L1 (4K each, secure, dual-mapped at 0x3F8000) + H0 (8K, 0xA000, also at 0x3FA000), 1K OTP
 * at 0x3D7800, 4K boot ROM at 0x3FF000.  Addresses and lengths are 16-bit words.  Sections as in f28232.cmd:
 * codestart at BEGIN = 0x3F7FF6 (boot-to-flash entry), code / constants / init tables in flash, .stack in M1,
 * variables in H0.  build.sh links the F2808 samples (f2808_*.c) with this file and without --float_support. */

MEMORY
{
PAGE 0:    /* program */
   RAML0      : origin = 0x008000, length = 0x001000     /* L0 SARAM 4K (also at 0x3F8000) */
   RAML1      : origin = 0x009000, length = 0x001000     /* L1 SARAM 4K */
   OTP        : origin = 0x3D7800, length = 0x000400     /* user OTP 1K */
   FLASH      : origin = 0x3E8000, length = 0x00FF80     /* sectors D, C, B, A (16K each), less the CSM area */
   CSM_RSVD   : origin = 0x3F7F80, length = 0x000076     /* program with 0x0000 when the CSM is used */
   BEGIN      : origin = 0x3F7FF6, length = 0x000002     /* boot-to-flash entry: LB code_start */
   CSM_PWL    : origin = 0x3F7FF8, length = 0x000008     /* CSM password */
   ROM        : origin = 0x3FF000, length = 0x000FC0     /* boot ROM */
   RESET      : origin = 0x3FFFC0, length = 0x000002     /* boot ROM reset vector (not loaded: DSECT) */

PAGE 1:    /* data */
   BOOT_RSVD  : origin = 0x000000, length = 0x000050     /* M0 words used by the boot ROM's stack */
   RAMM0      : origin = 0x000050, length = 0x0003B0     /* M0 SARAM (rest) */
   RAMM1      : origin = 0x000400, length = 0x000400     /* M1 SARAM 1K: SP = 0x400 after reset */
   RAMH0      : origin = 0x00A000, length = 0x002000     /* H0 SARAM 8K */
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
   .bss             : > RAMH0,     PAGE = 1
   .bss:output      : > RAMH0,     PAGE = 1
   .data            : > RAMH0,     PAGE = 1
   .sysmem          : > RAMH0,     PAGE = 1
#else
   .pinit           : > FLASH,     PAGE = 0
   .econst          : > FLASH,     PAGE = 0
   .ebss            : > RAMH0,     PAGE = 1
   .esysmem         : > RAMH0,     PAGE = 1
#endif
}
