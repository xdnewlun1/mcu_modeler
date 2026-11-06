/* STM32F405ZG: 1 MB flash at 0x08000000 (boot alias at 0x0 not used: VTOR = 0x08000000), 128 KB SRAM at
 * 0x20000000 (SRAM1 + SRAM2). CCM RAM (0x10000000, 64 KB) is not used by the samples. */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1M
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);     /* 0x20020000 */

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
