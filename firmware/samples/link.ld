/* Kinetis K22F (MK22FN1M0): 1 MB flash at 0x0, 128 KB SRAM (SRAM_L 0x1FFF0000 + SRAM_U 0x20000000). */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 1M
  RAM   (rwx) : ORIGIN = 0x1FFF0000, LENGTH = 128K
}

ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);     /* 0x20010000 */

SECTIONS
{
  .isr_vector 0x00000000 :
  {
    KEEP(*(.isr_vector))
  } > FLASH

  .flash_config 0x00000400 :
  {
    KEEP(*(.flash_config))
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
