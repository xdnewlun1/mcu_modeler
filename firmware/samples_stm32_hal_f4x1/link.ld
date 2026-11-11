/* STM32F401/F411 HAL samples: 256 KB flash link region at 0x08000000 (VTOR = 0x08000000), 64 KB SRAM at 0x20000000
 * (fits every F401/F411 part: the smallest, STM32F401xB/C, has 64 KB). Otherwise ../samples_stm32_hal/link.ld. */
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
