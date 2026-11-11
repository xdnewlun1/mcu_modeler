/* f2802x.cmd -- TI linker command file for the TMS320F28027 C samples (f28027_*.c; flash build, COFF or EABI).
 *
 * Memory map (SPRS523P Figure 9-1, Table 9-3): 32K x 16 flash at 0x3F0000 (sectors D..A, 8K each), 6K x 16 SARAM =
 * M0 + M1 (1K each) + L0 (4K at 0x8000, dual-mapped at 0x3F8000), 1K user OTP at 0x3D7800, TI-reserved OTP
 * (Device_cal at 0x3D7C80), 8K boot ROM at 0x3FE000.  Words.  codestart at BEGIN = 0x3F7FF6 (boot-to-flash entry),
 * code and constants in flash, .stack in M1, variables in L0.  The boot ROM's stack uses M0 0x0002-0x004E
 * (SPRUI09 Figure 2-4). */

MEMORY
{
PAGE 0:    /* program */
   OTP        : origin = 0x3D7800, length = 0x000400     /* user OTP 1K */
   FLASH      : origin = 0x3F0000, length = 0x007F80     /* sectors D, C, B, A less the CSM area */
   CSM_RSVD   : origin = 0x3F7F80, length = 0x000076
   BEGIN      : origin = 0x3F7FF6, length = 0x000002     /* boot-to-flash entry: LB code_start */
   CSM_PWL    : origin = 0x3F7FF8, length = 0x000008
   RESET      : origin = 0x3FFFC0, length = 0x000002     /* boot ROM reset vector (DSECT) */

PAGE 1:    /* data */
   BOOT_RSVD  : origin = 0x000000, length = 0x000050     /* M0 words used by the boot ROM's stack */
   RAMM0      : origin = 0x000050, length = 0x0003B0
   RAMM1      : origin = 0x000400, length = 0x000400     /* SP = 0x400 after reset */
   RAML0      : origin = 0x008000, length = 0x001000     /* L0 SARAM 4K */
   PIEVECT    : origin = 0x000D00, length = 0x000100
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
   .bss             : > RAML0,     PAGE = 1
   .bss:output      : > RAML0,     PAGE = 1
   .data            : > RAML0,     PAGE = 1
   .sysmem          : > RAML0,     PAGE = 1
#else
   .pinit           : > FLASH,     PAGE = 0
   .econst          : > FLASH,     PAGE = 0
   .ebss            : > RAML0,     PAGE = 1
   .esysmem         : > RAML0,     PAGE = 1
#endif
}
