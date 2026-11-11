/* STM32F105xC / STM32F107xC: 256 KB flash at 0x08000000 (VTOR = 0x08000000), 64 KB SRAM at
 * 0x20000000. */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}

ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);     /* 0x20010000 */

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
