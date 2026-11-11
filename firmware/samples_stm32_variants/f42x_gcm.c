/* STM32F437: AES-128-GCM encryption with the CRYP (RM0090 23.3.2, the four GCM phases), SHA-256 and HMAC-SHA256
 * with the HASH (RM0090 25.3.4 / 25.3.6), polling, at the reset clock (HSI 16 MHz). Results on UART7 (PE8, AF8,
 * 115200 baud from PCLK1 = 16 MHz) and in the globals gcm_ct / gcm_tag / sha / mac.
 * Data: McGrew-Viega GCM test case 3 key / IV / plaintext (64 bytes, whole blocks) with the 20-byte AAD of test
 * case 4 zero-padded to 32 bytes (GHASH pads the AAD with zeros, so the header phase sees the standard input);
 * SHA-256("abc"); HMAC-SHA256 RFC 4231 test case 2 (key "Jefe"). DATATYPE = 32-bit: the words below are the
 * big-endian words of the byte strings. */
#include "stm32f42x.h"

#define CRYP_BASE 0x50060000u
#define CRYP_CR   REG32(CRYP_BASE + 0x00)
#define CRYP_SR   REG32(CRYP_BASE + 0x04)
#define CRYP_DIN  REG32(CRYP_BASE + 0x08)
#define CRYP_DOUT REG32(CRYP_BASE + 0x0C)
#define CRYP_K(i) REG32(CRYP_BASE + 0x20 + 4u * (i))   /* K0LR .. K3RR */
#define CRYP_IV(i) REG32(CRYP_BASE + 0x40 + 4u * (i))  /* IV0LR .. IV1RR */
#define CRYP_CR_CRYPEN  (1u << 15)
#define CRYP_CR_GCM     ((1u << 19) | (0u << 3))      /* ALGOMODE 1000 */
#define CRYP_PH_HEADER  (1u << 16)
#define CRYP_PH_PAYLOAD (2u << 16)
#define CRYP_PH_FINAL   (3u << 16)
#define CRYP_SR_IFNF (1u << 1)
#define CRYP_SR_OFNE (1u << 2)
#define CRYP_SR_BUSY (1u << 4)

#define HASH_BASE 0x50060400u
#define HASH_CR   REG32(HASH_BASE + 0x00)
#define HASH_DIN  REG32(HASH_BASE + 0x04)
#define HASH_STR  REG32(HASH_BASE + 0x08)
#define HASH_SR   REG32(HASH_BASE + 0x24)
#define HASH_HR(i) REG32(HASH_BASE + 0x310 + 4u * (i))
#define HASH_CR_INIT   (1u << 2)
#define HASH_CR_MODE   (1u << 6)
#define HASH_CR_SHA256 ((1u << 18) | (1u << 7))
#define HASH_STR_DCAL  (1u << 8)
#define HASH_SR_DCIS   (1u << 1)
#define HASH_SR_BUSY   (1u << 3)

static const uint32_t key[4] = {0xfeffe992u, 0x8665731cu, 0x6d6a8f94u, 0x67308308u};
static const uint32_t icb[4] = {0xcafebabeu, 0xfacedbadu, 0xdecaf888u, 0x00000002u};   /* IV || counter 2 */
static const uint32_t aad[8] = {0xfeedfaceu, 0xdeadbeefu, 0xfeedfaceu, 0xdeadbeefu, 0xabaddad2u, 0, 0, 0};
static const uint32_t pt[16] = {
    0xd9313225u, 0xf88406e5u, 0xa55909c5u, 0xaff5269au, 0x86a7a953u, 0x1534f7dau, 0x2e4c303du, 0x8a318a72u,
    0x1c3c0c95u, 0x95680953u, 0x2fcf0e24u, 0x49a6b525u, 0xb16aedf5u, 0xaa0de657u, 0xba637b39u, 0x1aafd255u};
static const uint32_t ct_ref[16] = {   /* test case 3 ciphertext (does not depend on the AAD) */
    0x42831ec2u, 0x21777424u, 0x4b7221b7u, 0x84d0d49cu, 0xe3aa212fu, 0x2c02a4e0u, 0x35c17e23u, 0x29aca12eu,
    0x21d514b2u, 0x5466931cu, 0x7d8f6a5au, 0xac84aa05u, 0x1ba30b39u, 0x6a0aac97u, 0x3d58e091u, 0x473f5985u};

volatile uint32_t gcm_ct[16], gcm_tag[4], sha[8], mac[8];

static void putc7(char c)
{
    while (!(UART7->SR & USART_SR_TXE)) { }
    UART7->DR = (uint8_t)c;
}

static void puts7(const char *s)
{
    while (*s)
        putc7(*s++);
}

static void hex7(volatile const uint32_t *w, unsigned n)
{
    static const char d[] = "0123456789abcdef";
    for (unsigned i = 0; i < n; i++)
        for (int s = 28; s >= 0; s -= 4)
            putc7(d[(w[i] >> s) & 0xFu]);
}

static void cryp_block(const uint32_t *in, volatile uint32_t *out)
{
    for (unsigned i = 0; i < 4; i++) {
        while (!(CRYP_SR & CRYP_SR_IFNF)) { }
        CRYP_DIN = in[i];
    }
    if (out) {
        for (unsigned i = 0; i < 4; i++) {
            while (!(CRYP_SR & CRYP_SR_OFNE)) { }
            out[i] = CRYP_DOUT;
        }
    }
}

static void gcm_encrypt(void)
{
    /* init phase: H = E_K(0) (RM0090 23.3.2 a-f) */
    CRYP_CR = CRYP_CR_GCM;                               /* phase 00, AES-128, DATATYPE 32-bit, encrypt */
    for (unsigned i = 0; i < 4; i++)
        CRYP_K(4 + i) = key[i];                          /* AES-128: K2LR..K3RR */
    for (unsigned i = 0; i < 4; i++)
        CRYP_IV(i) = icb[i];
    CRYP_CR |= CRYP_CR_CRYPEN;
    while (CRYP_CR & CRYP_CR_CRYPEN) { }                 /* cleared by hardware at the end of the init phase */
    /* header phase */
    CRYP_CR = CRYP_CR_GCM | CRYP_PH_HEADER | CRYP_CR_CRYPEN;
    cryp_block(&aad[0], 0);
    cryp_block(&aad[4], 0);
    while (CRYP_SR & CRYP_SR_BUSY) { }
    /* payload phase */
    CRYP_CR = CRYP_CR_GCM | CRYP_PH_PAYLOAD;
    CRYP_CR = CRYP_CR_GCM | CRYP_PH_PAYLOAD | CRYP_CR_CRYPEN;
    for (unsigned b = 0; b < 4; b++)
        cryp_block(&pt[4 * b], &gcm_ct[4 * b]);
    /* final phase: len(A) || len(C) in bits */
    CRYP_CR = CRYP_CR_GCM | CRYP_PH_FINAL;
    CRYP_CR = CRYP_CR_GCM | CRYP_PH_FINAL | CRYP_CR_CRYPEN;
    static const uint32_t lens[4] = {0, 20u * 8u, 0, 64u * 8u};
    cryp_block(lens, gcm_tag);
    CRYP_CR = CRYP_CR_GCM | CRYP_PH_FINAL;
}

static void hash_words(const uint32_t *w, unsigned n, unsigned nblw)
{
    for (unsigned i = 0; i < n; i++)
        HASH_DIN = w[i];
    HASH_STR = nblw;
    HASH_STR = nblw | HASH_STR_DCAL;
    while (HASH_SR & HASH_SR_BUSY) { }
}

static void digest(volatile uint32_t *out)
{
    while (!(HASH_SR & HASH_SR_DCIS)) { }
    for (unsigned i = 0; i < 8; i++)
        out[i] = HASH_HR(i);
}

int main(void)
{
    RCC_AHB1ENR |= 1u << 4;                             /* GPIOE */
    RCC_AHB2ENR |= RCC_AHB2ENR_CRYPEN | RCC_AHB2ENR_HASHEN;
    RCC_APB1ENR |= RCC_APB1ENR_UART7EN;
    gpio_af(GPIOE, 8, 8);                               /* PE8 = UART7_TX */
    UART7->BRR = USART_BRR(16000000u, 115200u);
    UART7->CR1 = USART_CR1_UE | USART_CR1_TE;

    gcm_encrypt();
    int ok = 1;
    for (unsigned i = 0; i < 16; i++)
        ok &= gcm_ct[i] == ct_ref[i];
    puts7(ok ? "GCM ct ok\r\ntag " : "GCM ct BAD\r\ntag ");
    hex7(gcm_tag, 4);

    static const uint32_t abc[1] = {0x61626300u};
    HASH_CR = HASH_CR_SHA256 | HASH_CR_INIT;
    hash_words(abc, 1, 24);
    digest(sha);
    puts7("\r\nsha256 ");
    hex7(sha, 8);

    /* HMAC (RM0090 25.3.6): key, message, key again, each closed with DCAL */
    static const uint32_t jefe[1] = {0x4a656665u};       /* "Jefe" */
    static const uint32_t msg[7] = {0x77686174u, 0x20646f20u, 0x79612077u, 0x616e7420u, 0x666f7220u,
                                    0x6e6f7468u, 0x696e673fu};   /* "what do ya want for nothing?" */
    HASH_CR = HASH_CR_SHA256 | HASH_CR_MODE | HASH_CR_INIT;
    hash_words(jefe, 1, 0);
    hash_words(msg, 7, 0);
    hash_words(jefe, 1, 0);
    digest(mac);
    puts7("\r\nhmac ");
    hex7(mac, 8);
    puts7("\r\ndone\r\n");
    for (;;) { }
}
