/* STM32F427/F437 SAI1: a 1 kHz tone to an I2S codec, full duplex (boards/stm32f427vg_sai_codec.yaml).
 *
 * Clocks: 180 MHz core with over-drive (clock_180mhz, HSE 8 MHz / PLLM 8 = 1 MHz VCO input). PLLSAI: PLLSAIN 344,
 * PLLSAIQ 7 -> PLLSAI_Q = 344 / 7 = 49.142857 MHz; DCKCFGR PLLSAIDIVQ /1, SAI1ASRC = PLLSAI (RM0090 6.3.24/6.3.25).
 * SAI block A master transmitter, I2S standard as ST's HAL sets it up (SAI_InitI2S): free protocol, 16-bit data,
 * 2 slots, FRL 31 (32-bit frame), FSALL 15, FSDEF = 1, FSPOL = 0 (active low = left), FSOFF = 1, CKSTR = 1.
 * MCKDIV 2 (HAL: 49.142857 MHz / (48 kHz x 512) = 2.0): MCLK = 49.142857 / 4 = 12.285714 MHz, FS = MCLK / 256
 * = 47.991 kHz (not exactly 48 kHz: PLLSAI_Q / 1024), SCK = 32 FS = 1.535714 MHz (RM0090 29.9).
 * Pins (DS9405 Table 12, AF6): PE2 SAI1_MCLK_A, PE4 SAI1_FS_A, PE5 SAI1_SCK_A, PE6 SAI1_SD_A (to the codec),
 * PE3 SAI1_SD_B (from the codec).
 * Block B: slave receiver synchronous with block A (SYNCEN = 01, RM0090 29.5): it uses A's SCK/FS and reads SD_B.
 * The SAI1 interrupt (87) refills A's FIFO (FTH 1/4: FREQ while at most 2 words are queued) with a 48-sample sine
 * (left) and a square wave (right), and drains B's FIFO (FTH empty: FREQ while a word is queued) into rx_buf.
 * After FRAMES frames the feeder stops: A underruns (OVRUDR, its interrupt counted in `underrun_irqs`), then both
 * blocks are disabled (SAIxEN reads 1 until the end of the frame, RM0090 29.15) and `done` is set. */
#include "stm32f42x.h"

#define SAI1_BASE   0x40015800u
#define SAI_CR1(x)   REG32(SAI1_BASE + 0x04u + 0x20u * (x))
#define SAI_CR2(x)   REG32(SAI1_BASE + 0x08u + 0x20u * (x))
#define SAI_FRCR(x)  REG32(SAI1_BASE + 0x0Cu + 0x20u * (x))
#define SAI_SLOTR(x) REG32(SAI1_BASE + 0x10u + 0x20u * (x))
#define SAI_IM(x)    REG32(SAI1_BASE + 0x14u + 0x20u * (x))
#define SAI_SR(x)    REG32(SAI1_BASE + 0x18u + 0x20u * (x))
#define SAI_CLRFR(x) REG32(SAI1_BASE + 0x1Cu + 0x20u * (x))
#define SAI_DR(x)    REG32(SAI1_BASE + 0x20u + 0x20u * (x))
#define BLK_A 0u
#define BLK_B 1u
#define SAI_CR1_MODE_SLAVE_RX 3u
#define SAI_CR1_DS16      (4u << 5)
#define SAI_CR1_CKSTR     (1u << 9)
#define SAI_CR1_SYNCEN    (1u << 10)
#define SAI_CR1_SAIEN     (1u << 16)
#define SAI_CR1_MCKDIV(n) ((uint32_t)(n) << 20)
#define SAI_SR_OVRUDR 1u
#define SAI_SR_FREQ   8u
#define SAI_IM_OVRUDRIE 1u
#define SAI_IM_FREQIE   8u
#define FRAMES 96u

static const int16_t sine48[48] = {
    0, 1044, 2071, 3061, 4000, 4870, 5657, 6347, 6928, 7391, 7727, 7932, 8000, 7932, 7727, 7391, 6928, 6347, 5657,
    4870, 4000, 3061, 2071, 1044, 0, -1044, -2071, -3061, -4000, -4870, -5657, -6347, -6928, -7391, -7727, -7932,
    -8000, -7932, -7727, -7391, -6928, -6347, -5657, -4870, -4000, -3061, -2071, -1044,
};

volatile uint32_t tx_words, rx_words, sai_irqs, underrun_irqs, done;
volatile uint16_t rx_buf[64];

void SAI1_IRQHandler(void)
{
    sai_irqs++;
    while (SAI_SR(BLK_B) & SAI_SR_FREQ) {             /* B: received words */
        uint16_t v = (uint16_t)SAI_DR(BLK_B);
        if (rx_words < 64u)
            rx_buf[rx_words] = v;
        rx_words++;
    }
    if ((SAI_IM(BLK_A) & SAI_IM_OVRUDRIE) && (SAI_SR(BLK_A) & SAI_SR_OVRUDR)) {   /* A: underrun (feeder stopped) */
        SAI_CLRFR(BLK_A) = SAI_SR_OVRUDR;
        SAI_IM(BLK_A) = 0;
        underrun_irqs++;
        return;
    }
    if (!(SAI_IM(BLK_A) & SAI_IM_FREQIE))
        return;
    while (SAI_SR(BLK_A) & SAI_SR_FREQ) {
        if (tx_words >= 2u * FRAMES) {                /* stop feeding: next empty slot underruns */
            SAI_IM(BLK_A) = SAI_IM_OVRUDRIE;
            break;
        }
        unsigned frame = tx_words >> 1;
        int16_t s = (tx_words & 1u) ? ((frame % 48u) < 24u ? 4000 : -4000) : sine48[frame % 48u];
        SAI_DR(BLK_A) = (uint16_t)s;
        tx_words++;
    }
}

int main(void)
{
    clock_180mhz();

    /* PLLSAI for 48 kHz audio: 1 MHz x 344 / 7 = 49.142857 MHz on PLLSAI_Q (PLLSAIR stays at its reset value 2) */
    RCC_PLLSAICFGR = (344u << 6) | (7u << 24) | (2u << 28);
    RCC_DCKCFGR &= ~((0x1Fu << 8) | (3u << 20));      /* PLLSAIDIVQ /1, SAI1ASRC = PLLSAI_Q */
    RCC_CR |= RCC_CR_PLLSAION;
    while (!(RCC_CR & RCC_CR_PLLSAIRDY)) { }

    RCC_AHB1ENR |= 1u << 4;                            /* GPIOE */
    RCC_APB2ENR |= RCC_APB2ENR_SAI1EN;
    gpio_af(GPIOE, 2, 6);                              /* SAI1_MCLK_A */
    gpio_af(GPIOE, 3, 6);                              /* SAI1_SD_B */
    gpio_af(GPIOE, 4, 6);                              /* SAI1_FS_A */
    gpio_af(GPIOE, 5, 6);                              /* SAI1_SCK_A */
    gpio_af(GPIOE, 6, 6);                              /* SAI1_SD_A */

    for (unsigned x = BLK_A; x <= BLK_B; x++) {
        SAI_FRCR(x) = 31u | (15u << 8) | (1u << 16) | (1u << 18);     /* FRL 31, FSALL 15, FSDEF, FSOFF, FSPOL 0 */
        SAI_SLOTR(x) = (1u << 8) | (3u << 16);                          /* 2 slots of DS bits, both active */
    }
    SAI_CR1(BLK_A) = SAI_CR1_DS16 | SAI_CR1_CKSTR | SAI_CR1_MCKDIV(2);  /* master TX */
    SAI_CR2(BLK_A) = 1u;                                                /* FTH 1/4 */
    SAI_CR1(BLK_B) = SAI_CR1_MODE_SLAVE_RX | SAI_CR1_DS16 | SAI_CR1_CKSTR | SAI_CR1_SYNCEN;
    SAI_CR2(BLK_B) = 0u;                                                /* FTH empty: FREQ per received word */

    for (unsigned i = 0; i < 4u; i++) {                /* pre-fill A: two stereo frames */
        unsigned frame = tx_words >> 1;
        SAI_DR(BLK_A) = (uint16_t)((tx_words & 1u) ? 4000 : sine48[frame % 48u]);
        tx_words++;
    }
    SAI_IM(BLK_B) = SAI_IM_FREQIE;
    SAI_IM(BLK_A) = SAI_IM_FREQIE;
    NVIC_EnableIRQ(SAI1_IRQn);
    __enable_irq();
    SAI_CR1(BLK_B) |= SAI_CR1_SAIEN;                   /* slave first (RM0090 29.4) */
    SAI_CR1(BLK_A) |= SAI_CR1_SAIEN;

    while (!underrun_irqs) { }
    SAI_CR1(BLK_A) &= ~SAI_CR1_SAIEN;                  /* master first when synchronous (RM0090 29.15) */
    while (SAI_CR1(BLK_A) & SAI_CR1_SAIEN) { }         /* reads 1 until the end of the current frame */
    SAI_CR1(BLK_B) &= ~SAI_CR1_SAIEN;
    while (SAI_CR1(BLK_B) & SAI_CR1_SAIEN) { }
    done = 1;
    for (;;)
        __WFI();
}
