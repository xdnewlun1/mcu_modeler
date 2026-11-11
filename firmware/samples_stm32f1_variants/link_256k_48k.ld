/* STM32F103 high density: 256 KB flash at 0x08000000 (boot alias at 0x0 not used: VTOR = 0x08000000), 48 KB SRAM at
 * 0x20000000.  Fits every high-density part (RC/VC/ZC: 256 KB / 48 KB; RE/VE/ZE: 512 KB / 64 KB). */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 48K
}

ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);     /* 0x2000C000 */

SECTIONS
{
  .isr_vector 0x08000000 :
  {
    KEEP(*(.isr_vector))
  } > FLASH

  .text :
  {
    *(.text .text.*)
    *(.rodata .rodata.*)
    . = ALIGN(4);
  } > FLASH

  .ARM.exidx :
  {
    *(.ARM.exidx .ARM.exidx.*)
  } > FLASH

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data .data.*)
    . = ALIGN(4);
    _edata = .;
  } > RAM AT > FLASH
  _sidata = LOADADDR(.data);

  .bss (NOLOAD) :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss .bss.*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM
}
