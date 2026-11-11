/* f2806x.cmd -- TI linker command file for the TMS320F28069 C samples (f28069_*.c; flash build, COFF or EABI).
 *
 * Memory map (SPRS698J Figure 8-1, Table 8-3): 128K x 16 flash at 0x3D8000 (sectors H..A, 16K each), SARAM M0/M1
 * (1K each), L0-L3 (CLA data / program RAM), L4 8K at 0xA000, L5-L8 8K each (DMA RAMs), user OTP at 0x3D7800, 32K boot
 * ROM at 0x3F8000.  Words.  codestart at BEGIN = 0x3F7FF6, code in flash, .stack in M1, variables in L4. */

MEMORY
{
PAGE 0:    /* program */
   OTP        : origin = 0x3D7800, length = 0x000400
   FLASH      : origin = 0x3D8000, length = 0x01FF80     /* sectors H..A less the CSM area */
   CSM_RSVD   : origin = 0x3F7F80, length = 0x000076
   BEGIN      : origin = 0x3F7FF6, length = 0x000002
   CSM_PWL    : origin = 0x3F7FF8, length = 0x000008
   RESET      : origin = 0x3FFFC0, length = 0x000002

PAGE 1:    /* data */
   BOOT_RSVD  : origin = 0x000000, length = 0x000050
   RAMM0      : origin = 0x000050, length = 0x0003B0
   RAMM1      : origin = 0x000400, length = 0x000400
   RAML4      : origin = 0x00A000, length = 0x002000     /* L4 SARAM 8K */
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
   .bss             : > RAML4,     PAGE = 1
   .bss:output      : > RAML4,     PAGE = 1
   .data            : > RAML4,     PAGE = 1
   .sysmem          : > RAML4,     PAGE = 1
#else
   .pinit           : > FLASH,     PAGE = 0
   .econst          : > FLASH,     PAGE = 0
   .ebss            : > RAML4,     PAGE = 1
   .esysmem         : > RAML4,     PAGE = 1
#endif
}
