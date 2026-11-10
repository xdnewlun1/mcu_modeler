/* fsmc_sram: external 16-bit SRAM on FSMC bank 1 / NE1 (run with boards/stm32_fsmc_sram.yaml: a 64K x 16 SRAM).
 * 16 MHz HSI. FSMC pins (AF12) on ports D/E/F/G are muxed like a real board would (D0-D15, A0-A15, NOE, NWE, NE1,
 * NBL0/1); the model does not toggle them. BCR1 = MBKEN | MWID 16 bits | WREN, BTR1 ADDSET 1 / DATAST 2.
 * Prints over USART1 (PA9, 115200):
 *   "FSMC BCR1 00001091\r\n"       BCR1 after configuration (reset 0x30DB; FACCEN cleared, bit 7 reserved = 1)
 *   "SRAM ok\r\n"                   1024 words written / read back at 0x60000000
 *   "MIRROR ok\r\n"                 128 KB SRAM: 0x60020000 aliases 0x60000000 (A16+ not connected)
 *   "EXEC 42\r\n"                   a Thumb function copied into the SRAM and called from there
 *   "FAULT ok\r\n" "done\r\n"       MBKEN = 0, then a read of 0x60000000 is a bus fault (HardFault handler) */
#include "stm32f405.h"

#define FSMC_BCR1 REG32(0xA0000000u)
#define FSMC_BTR1 REG32(0xA0000004u)
#define RCC_AHB3ENR REG32(RCC_BASE + 0x38)
#define SRAM ((volatile uint16_t *)0x60000000u)
#define SRAM32 ((volatile uint32_t *)0x60000000u)

volatile uint32_t result;
static volatile int faulted;

static void putc1(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
}

static void puts1(const char *s)
{
    while (*s)
        putc1(*s++);
}

static void hex(uint32_t v)
{
    static const char h[] = "0123456789abcdef";
    for (int i = 7; i >= 0; i--)
        putc1(h[(v >> (4 * i)) & 15]);
}

static void fail(const char *what)
{
    puts1("ERR ");
    puts1(what);
    puts1("\r\n");
    for (;;) { }
}

/* HardFault: report and skip the faulting 16-bit load (ldr r, [r]) */
__attribute__((naked)) void HardFault_Handler(void)
{
    __asm volatile(
        "ldr r0, =faulted\n"
        "movs r1, #1\n"
        "str r1, [r0]\n"
        "ldr r1, [sp, #24]\n"          /* stacked PC */
        "adds r1, #2\n"
        "str r1, [sp, #24]\n"
        "bx lr\n");
}

static void af12(volatile GPIO_TypeDef *g, uint32_t pins)
{
    for (unsigned p = 0; p < 16; p++)
        if (pins & (1u << p))
            gpio_af((GPIO_TypeDef *)g, p, 12);
}

/* Thumb code of: int f(void) { return 42; }  ->  movs r0, #42 ; bx lr */
static const uint16_t code42[] = {0x202A, 0x4770};

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | (0xFu << 3);       /* GPIOA, D, E, F, G */
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE;

    af12((GPIO_TypeDef *)0x40020C00u, 0xFFB3u);  /* PD0,1,4,5,7,8-15: D2,D3,NOE,NWE,NE1,D13-D15,A16-A18,D0,D1 */
    af12((GPIO_TypeDef *)0x40021000u, 0xFF83u);  /* PE0,1 NBL0/1, PE7-15 D4-D12 */
    af12((GPIO_TypeDef *)0x40021400u, 0xF03Fu);  /* PF0-5 A0-A5, PF12-15 A6-A9 */
    af12((GPIO_TypeDef *)0x40021800u, 0x003Fu);  /* PG0-5 A10-A15 */

    RCC_AHB3ENR |= 1u;                                     /* FSMCEN */
    FSMC_BTR1 = (1u << 0) | (2u << 8);                     /* ADDSET 1, DATAST 2 (timing not modelled) */
    FSMC_BCR1 = (1u << 0) | (1u << 4) | (1u << 12);        /* MBKEN, MWID = 16, WREN, MTYP = SRAM */
    puts1("FSMC BCR1 ");
    hex(FSMC_BCR1);
    puts1("\r\n");

    for (uint32_t i = 0; i < 1024; i++)
        SRAM32[i] = 0xC0DE0000u ^ (i * 2654435761u);
    for (uint32_t i = 0; i < 1024; i++)
        if (SRAM32[i] != (0xC0DE0000u ^ (i * 2654435761u)))
            fail("SRAM");
    SRAM[5] = 0xBEEF;
    *(volatile uint8_t *)0x6000000Bu = 0x5A;               /* byte lane NBL1 */
    if (SRAM[5] != 0x5AEFu)
        fail("BYTE");
    puts1("SRAM ok\r\n");

    if (SRAM32[0x20000 / 4] != SRAM32[0] || SRAM[0x10000 + 7] != SRAM[7])
        fail("MIRROR");
    puts1("MIRROR ok\r\n");

    volatile uint16_t *fn = (volatile uint16_t *)0x60001000u;
    fn[0] = code42[0];
    fn[1] = code42[1];
    int (*f)(void) = (int (*)(void))(0x60001000u | 1u);
    int r = f();
    puts1("EXEC ");
    putc1((char)('0' + r / 10));
    putc1((char)('0' + r % 10));
    puts1("\r\n");

    FSMC_BCR1 &= ~1u;                                      /* MBKEN = 0: the window closes */
    uint32_t v;
    __asm volatile("ldr %0, [%1]" : "=l"(v) : "l"(0x60000000u) : "memory");
    (void)v;
    puts1(faulted ? "FAULT ok\r\n" : "no fault\r\n");
    result = faulted && r == 42;
    puts1("done\r\n");
    for (;;) { }
}
