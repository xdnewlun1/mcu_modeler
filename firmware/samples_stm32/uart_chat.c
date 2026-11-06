/* uart_chat: chip B of boards/c2000_stm32_chat.yaml, the STM32 side of the two-way UART chat protocol
 * (same protocol as firmware/samples_c2000/uart_chat.asm and firmware/samples_stm32_hal/):
 *   115200 8N1, ASCII lines ending "\r\n".  At boot send "HELLO STM32"; every 5 ms send "PING <n>" (n = 1, 2, ...);
 *   a received "PING <n>" is answered with "PONG <n>"; a received "PONG <n>" toggles PB0 and counts `pongs`;
 *   a received "HELLO <x>" sets `peer_seen` and drives PB1 high; the first HELLO received is answered once with our
 *   own "HELLO STM32" (only once, so two chips doing it cannot loop).  Unknown lines are ignored, '\r' is optional.
 * Clocks: 16 MHz HSI (reset default), USART1 on PA9 (TX) / PA10 (RX), AF7, BRR 139 (115108 baud).
 * RX: USART1 RXNE interrupt (IRQ 37) -> 64-byte ring.  Tick: SysTick, core clock, 80000 cycles = 5 ms.
 * The main loop sleeps in WFI with interrupts masked between the "anything to do?" check and the WFI (WFI still
 * wakes on a pending interrupt with PRIMASK set), so no wakeup is lost. */
#include "stm32f405.h"

#define USART_CR1_RXNEIE   (1u << 5)
#define SYST_CSR_TICKINT   (1u << 1)
#define CORE_HZ            HSI_HZ
#define TICK_HZ            200u                    /* 5 ms */
#define LED_PIN            0u                      /* PB0: toggled per PONG */
#define PEER_PIN           1u                      /* PB1: high once a HELLO was seen */
#define RING               64u
#define LINE_MAX           32u

volatile uint32_t pongs;                           /* PONG lines received */
volatile uint32_t pings_answered;                  /* PING lines answered with a PONG */
volatile uint32_t peer_seen;                       /* 1 after a HELLO line */
volatile uint32_t ping_n;                          /* last PING number sent */
volatile uint32_t hello_answered;                  /* 1 once the first received HELLO was answered */
static volatile uint32_t ticks;                    /* SysTick periods not yet handled */
static volatile uint8_t ring[RING];
static volatile uint32_t ring_head, ring_tail;
static char line[LINE_MAX + 1];
static uint32_t line_len;

void USART1_IRQHandler(void)
{
    if (USART1->SR & USART_SR_RXNE) {
        uint8_t c = (uint8_t)USART1->DR;           /* reading DR clears RXNE */
        uint32_t h = ring_head;
        if (((h + 1u) & (RING - 1u)) != ring_tail) {
            ring[h] = c;
            ring_head = (h + 1u) & (RING - 1u);
        }
    }
}

void SysTick_Handler(void)
{
    ticks++;
}

static void putc_(char c)
{
    while (!(USART1->SR & USART_SR_TXE)) { }
    USART1->DR = (uint8_t)c;
}

static void puts_(const char *s)
{
    while (*s)
        putc_(*s++);
}

static void put_dec(uint32_t v)
{
    char buf[10];
    unsigned n = 0;
    do {
        buf[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v);
    while (n)
        putc_(buf[--n]);
}

static int starts_with(const char *s, const char *prefix)
{
    while (*prefix)
        if (*s++ != *prefix++)
            return 0;
    return 1;
}

static int is_digit(char c) { return c >= '0' && c <= '9'; }

static void handle_line(void)
{
    if (starts_with(line, "PING ") && is_digit(line[5])) {
        puts_("PONG ");
        for (const char *p = line + 5; is_digit(*p); p++)
            putc_(*p);                             /* echo n as received */
        puts_("\r\n");
        pings_answered++;
    } else if (starts_with(line, "PONG ")) {
        GPIOB->ODR ^= 1u << LED_PIN;
        pongs++;
    } else if (starts_with(line, "HELLO ")) {
        peer_seen = 1;
        GPIOB->BSRR = 1u << PEER_PIN;
        if (!hello_answered) {
            hello_answered = 1;
            puts_("HELLO STM32\r\n");
        }
    }
}

static void on_byte(char c)
{
    if (c == '\r')
        return;
    if (c == '\n') {
        line[line_len] = 0;
        line_len = 0;
        handle_line();
        return;
    }
    if (line_len < LINE_MAX)
        line[line_len++] = c;
}

static void send_ping(void)
{
    ping_n++;
    puts_("PING ");
    put_dec(ping_n);
    puts_("\r\n");
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN | RCC_AHB1ENR_GPIOBEN;
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);                          /* USART1_TX */
    gpio_af(GPIOA, 10, 7);                         /* USART1_RX */
    gpio_pull(GPIOA, 10, GPIO_PULL_UP);
    gpio_mode(GPIOB, LED_PIN, GPIO_MODE_OUTPUT);
    gpio_mode(GPIOB, PEER_PIN, GPIO_MODE_OUTPUT);

    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE;
    NVIC_ClearPendingIRQ(USART1_IRQn);
    NVIC_EnableIRQ(USART1_IRQn);

    SYST_RVR = CORE_HZ / TICK_HZ - 1u;
    SYST_CVR = 0;
    SYST_CSR = SYST_CSR_CLKSOURCE | SYST_CSR_TICKINT | SYST_CSR_ENABLE;
    __enable_irq();

    puts_("HELLO STM32\r\n");

    for (;;) {
        __asm volatile ("cpsid i" ::: "memory");
        if (ring_head == ring_tail && ticks == 0) {
            __WFI();                               /* wakes on a pending interrupt even with PRIMASK set */
            __enable_irq();
            continue;
        }
        __enable_irq();
        while (ring_head != ring_tail) {
            char c = (char)ring[ring_tail];
            ring_tail = (ring_tail + 1u) & (RING - 1u);
            on_byte(c);
        }
        if (ticks) {
            ticks = 0;
            send_ping();
        }
    }
}
