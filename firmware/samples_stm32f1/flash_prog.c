/* STM32F103T6 flash_prog: FPEC page erase and half-word programming of the last page (31, 0x08007C00).
 * Unlock (KEYR), erase (PER, AR, STRT, wait BSY, check EOP), program four half-words (PG), program one of them again
 * with another value (not erased: PGERR, value unchanged), lock.  Results: result[] = the four half-words read back,
 * pgerr = 1, status = 0x600D on success.  USART1 prints "FLASH OK" / "FLASH FAIL". */
#include "console.h"

#define PAGE_ADDR 0x08007C00u
volatile uint32_t status, pgerr, erase_cycles;
volatile uint16_t result[4];

static void wait_bsy(void)
{
    while (FLASH_SR & FLASH_SR_BSY)
        ;
}

int main(void)
{
    static const uint16_t data[4] = {0x1234, 0xABCD, 0x0000, 0x55AA};
    volatile uint16_t *p = (volatile uint16_t *)PAGE_ADDR;

    console_init(8000000u, 115200u);
    FLASH_KEYR = FLASH_KEY1;
    FLASH_KEYR = FLASH_KEY2;
    FLASH_CR = FLASH_CR_PER;
    FLASH_AR = PAGE_ADDR;
    SYST_RVR = 0xFFFFFF;
    SYST_CVR = 0;
    SYST_CSR = 5;
    FLASH_CR = FLASH_CR_PER | FLASH_CR_STRT;
    wait_bsy();
    erase_cycles = 0xFFFFFF - SYST_CVR;              /* HCLK cycles spent erasing (one SysTick wrap max: 2 s) */
    FLASH_SR = FLASH_SR_EOP;
    FLASH_CR = FLASH_CR_PG;
    for (int i = 0; i < 4; i++) {
        p[i] = data[i];
        wait_bsy();
    }
    p[0] = 0x0001;                                   /* not erased: PGERR */
    wait_bsy();
    pgerr = (FLASH_SR & FLASH_SR_PGERR) ? 1u : 0u;
    FLASH_SR = FLASH_SR_PGERR | FLASH_SR_EOP;
    FLASH_CR = FLASH_CR_LOCK;
    int ok = pgerr == 1;
    for (int i = 0; i < 4; i++) {
        result[i] = p[i];
        ok &= result[i] == data[i];
    }
    status = ok ? 0x600Du : 0xBADu;
    puts_(ok ? "FLASH OK\r\n" : "FLASH FAIL\r\n");
    for (;;)
        __asm__ volatile("wfi");
}
