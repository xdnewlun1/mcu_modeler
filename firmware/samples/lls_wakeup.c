/* lls_wakeup: sleep in LLS (low-leakage stop) and wake every 100 ms through the LLWU (module 0 = LPTMR0).
 * K22P100M120SF5V2RM ch. 14 (SMC), ch. 16 (LLWU), ch. 40 (LPTMR). The LPTMR runs from the 1 kHz LPO, which keeps
 * running in LLS; the LLWU ISR (IRQ 21) clears the LPTMR flag (that also clears LLWU_F3.MWUF0), then main toggles
 * PTC5 and goes back to sleep. wakeups counts the LLS exits; the pin shows a 5 Hz square wave. */
#include "k22.h"

#define SMC_PMPROT   REG8(0x4007E000u)
#define SMC_PMCTRL   REG8(0x4007E001u)
#define SMC_PMSTAT   REG8(0x4007E003u)
#define LLWU_ME      REG8(0x4007C004u)
#define LLWU_F3      REG8(0x4007C007u)
#define LPTMR_CSR    REG32(0x40040000u)
#define LPTMR_PSR    REG32(0x40040004u)
#define LPTMR_CMR    REG32(0x40040008u)
#define SCB_SCR      REG32(0xE000ED10u)
#define LLWU_IRQn    21

volatile uint32_t wakeups;
volatile uint32_t llwu_f3;

void LLWU_IRQHandler(void)
{
    llwu_f3 = LLWU_F3;
    LPTMR_CSR |= 0x80u;                            /* TCF w1c: the LLWU module flag follows it */
}

int main(void)
{
    WDOG_Disable();
    SIM_SCGC5 |= SIM_SCGC5_PORTC | SIM_SCGC5_LPTMR;
    PORTC->PCR[5] = PORT_PCR_MUX(1) | PORT_PCR_DSE;
    GPIOC->PDDR |= 1u << 5;

    SMC_PMPROT = 0x08;                             /* ALLS: allow LLS (write-once) */
    SMC_PMCTRL = 0x03;                             /* STOPM = LLS */
    LLWU_ME = 0x01;                                /* WUME0: LPTMR0 */

    LPTMR_CSR = 0;
    LPTMR_PSR = 0x05;                              /* PCS = LPO 1 kHz, prescaler bypassed */
    LPTMR_CMR = 99;                                /* 100 ticks = 100 ms */
    LPTMR_CSR = 0x41;                              /* TIE | TEN */

    NVIC_ClearPendingIRQ(LLWU_IRQn);
    NVIC_EnableIRQ(LLWU_IRQn);
    __enable_irq();
    SCB_SCR |= 0x4u;                               /* SLEEPDEEP */
    for (;;) {
        __asm volatile("dsb");
        __WFI();                                   /* LLS until the LLWU wakes the chip */
        wakeups++;
        GPIOC->PTOR = 1u << 5;
    }
}
