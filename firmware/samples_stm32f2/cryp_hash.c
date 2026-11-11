/* cryp_hash: STM32F215/F217 cryptographic processor (RM0033 ch. 19) and hash processor (ch. 21), polling, at the
 * reset clock (HSI 16 MHz). Prints on USART1 (PA9, 115200 from PCLK2 = 16 MHz):
 *   "aes 69C4E0D86A7B0430D8CDB78070B4C55A"   AES-128 ECB, FIPS-197 C.1 (key 000102..0F, plaintext 00112233..FF)
 *   "sha1 A9993E364706816ABA3E25717850C26C9CD0D89D"  SHA-1("abc")
 *   "md5 900150983CD24FB0D6963F7D28E17F72"   MD5("abc")
 *   "algo1 0"                                CRYP_CR.ALGOMODE[3] (bit 19, GCM/CCM on the F43x) reads 0 on the F2
 *   "done"
 * On an STM32F205/F207 (no CRYP/HASH) the blocks are inactive stubs: the wait for CRYP_SR.OFNE never ends. */
#include "stm32f405.h"

#define CRYP_BASE 0x50060000u
#define HASH_BASE 0x50060400u
#define CRYP_CR   REG32(CRYP_BASE + 0x00)
#define CRYP_SR   REG32(CRYP_BASE + 0x04)
#define CRYP_DIN  REG32(CRYP_BASE + 0x08)
#define CRYP_DOUT REG32(CRYP_BASE + 0x0C)
#define CRYP_K(n) REG32(CRYP_BASE + 0x20 + 4u * (n))     /* K0LR .. K3RR */
#define HASH_CR   REG32(HASH_BASE + 0x00)
#define HASH_DIN  REG32(HASH_BASE + 0x04)
#define HASH_STR  REG32(HASH_BASE + 0x08)
#define HASH_HR(n) REG32(HASH_BASE + 0x0C + 4u * (n))
#define HASH_SR   REG32(HASH_BASE + 0x24)
#define RCC_AHB2ENR REG32(RCC_BASE + 0x34)

#define CRYP_CR_ALGOMODE_AES_ECB (4u << 3)
#define CRYP_CR_ALGOMODE3        (1u << 19)
#define CRYP_CR_FFLUSH (1u << 14)
#define CRYP_CR_CRYPEN (1u << 15)
#define CRYP_SR_OFNE   (1u << 2)
#define CRYP_SR_BUSY   (1u << 4)
#define HASH_CR_INIT   (1u << 2)
#define HASH_CR_MD5    (1u << 7)
#define HASH_STR_DCAL  (1u << 8)
#define HASH_SR_BUSY   (1u << 3)
#define HASH_SR_DCIS   (1u << 1)

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

static void hex32(uint32_t v)
{
    for (int i = 28; i >= 0; i -= 4)
        putc1("0123456789ABCDEF"[(v >> i) & 0xF]);
}

static void aes_fips197(void)
{
    static const uint32_t key[4] = {0x00010203u, 0x04050607u, 0x08090A0Bu, 0x0C0D0E0Fu};
    static const uint32_t pt[4] = {0x00112233u, 0x44556677u, 0x8899AABBu, 0xCCDDEEFFu};
    CRYP_CR = CRYP_CR_ALGOMODE_AES_ECB;                /* DATATYPE 00 (32-bit words), KEYSIZE 00 (128 bits) */
    for (unsigned i = 0; i < 4; i++)
        CRYP_K(4 + i) = key[i];                        /* a 128-bit key goes to K2LR..K3RR */
    CRYP_CR |= CRYP_CR_FFLUSH;
    CRYP_CR |= CRYP_CR_CRYPEN;
    for (unsigned i = 0; i < 4; i++)
        CRYP_DIN = pt[i];
    puts1("aes ");
    for (unsigned i = 0; i < 4; i++) {
        while (!(CRYP_SR & CRYP_SR_OFNE)) { }
        hex32(CRYP_DOUT);
    }
    puts1("\r\n");
    CRYP_CR &= ~CRYP_CR_CRYPEN;
    CRYP_CR |= CRYP_CR_ALGOMODE3;                      /* reserved on the F2: stays 0 */
    puts1((CRYP_CR & CRYP_CR_ALGOMODE3) ? "algo1 1\r\n" : "algo1 0\r\n");
}

static void hash_abc(uint32_t algo, unsigned nwords, const char *name)
{
    HASH_CR = algo | HASH_CR_INIT;                     /* DATATYPE 00: 32-bit words, big-endian message order */
    HASH_DIN = 0x61626300u;                            /* "abc" */
    HASH_STR = 24u;                                    /* NBLW: 24 valid bits in the last word */
    HASH_STR = 24u | HASH_STR_DCAL;
    while (HASH_SR & HASH_SR_BUSY) { }
    puts1(name);
    for (unsigned i = 0; i < nwords; i++)
        hex32(HASH_HR(i));
    puts1("\r\n");
}

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC_AHB2ENR |= (1u << 4) | (1u << 5);              /* CRYPEN, HASHEN */
    RCC_APB2ENR |= RCC_APB2ENR_USART1EN;
    gpio_af(GPIOA, 9, 7);
    gpio_af(GPIOA, 10, 7);
    USART1->BRR = USART_BRR(HSI_HZ, 115200u);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    aes_fips197();
    hash_abc(0, 5, "sha1 ");
    hash_abc(HASH_CR_MD5, 4, "md5 ");
    puts1("done\r\n");
    for (;;) { }
}
