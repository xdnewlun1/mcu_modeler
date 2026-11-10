/* i2c_reg_master: the i2c_eeprom sample aimed at the STM32 I2C slave of boards/k22_stm32_i2c.yaml (address 0x42,
 * register map with the "first byte = register pointer, auto-increment" convention, acknowledges at once, so the
 * acknowledge polling ends immediately). */
#define I2C_TARGET  0x42u
#define TARGET_NAME "STM32 slave"
#define PAGE_SIZE   16u
#include "i2c_eeprom.c"
