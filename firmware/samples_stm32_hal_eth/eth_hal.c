/* eth_hal: ST's STM32CubeF4 HAL Ethernet driver (stm32f4xx_hal_eth.c, the 2022+ rewrite with enhanced descriptors)
 * on an STM32F407ZG against the CHIP_EM MAC/PHY/segment (boards/stm32f407_eth_hal.yaml).
 *
 * Clock:  HSE 8 MHz -> PLL -> 168 MHz SYSCLK, APB2 84 MHz (USART1 115200 on PA9).
 * ETH:    RMII (PA1 REF_CLK, PA2 MDIO, PA7 CRS_DV, PB11 TX_EN, PB12 TXD0, PB13 TXD1, PC1 MDC, PC4 RXD0, PC5 RXD1),
 *         LAN8742A PHY at address 0.  HAL_ETH_Init (SYSCFG_PMC, DMABMR software reset, MAC/DMA defaults, chained
 *         descriptor lists, MAC address), PHY soft reset + auto-negotiation through HAL_ETH_Read/WritePHYRegister,
 *         HAL_ETH_SetMACConfig with the negotiated speed/duplex, HAL_ETH_Start (polling mode).
 * Program: a gratuitous ARP, then HAL_ETH_ReadData in a loop: ARP requests for 192.168.0.20 and ICMP echo requests
 *         are answered with HAL_ETH_Transmit (checksums inserted by the MAC: ETH_CHECKSUM_IPHDR_PAYLOAD_INSERT_PHDR_CALC)
 *         and HAL_ETH_ReleaseTxPacket.
 * Output: "ETH HAL\r\nPHY 0007:C131\r\nlink 100FD\r\ngarp\r\narp 192.168.0.1\r\nping 1\r\n..." */
#include "stm32f4xx_hal.h"
void *memcpy(void *d, const void *s, __SIZE_TYPE__ n);   /* ../samples_stm32_hal/startup.c */

static int same(const uint8_t *a, const uint8_t *b, unsigned n)
{
    while (n--)
        if (a[n] != b[n])
            return 0;
    return 1;
}

UART_HandleTypeDef huart1;
ETH_HandleTypeDef heth;
static ETH_TxPacketConfigTypeDef txconf;
static ETH_DMADescTypeDef rxdesc[ETH_RX_DESC_CNT] __attribute__((aligned(4)));
static ETH_DMADescTypeDef txdesc[ETH_TX_DESC_CNT] __attribute__((aligned(4)));
static uint8_t rxpool[8][1536] __attribute__((aligned(4)));
static uint8_t txbuf[1536] __attribute__((aligned(4)));
static unsigned pool_i;
static uint8_t *rx_buf;
static uint16_t rx_len;
static uint8_t my_mac[6] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x20};
static const uint8_t my_ip[4] = {192, 168, 0, 20};
volatile uint32_t pings, arps, eth_error;

static void SystemClock_Config(void);
static void Error_Handler(void);

static void print(const char *s)
{
    uint16_t n = 0;
    while (s[n])
        n++;
    HAL_UART_Transmit(&huart1, (uint8_t *)s, n, 100);
}

static void print_dec(uint32_t v)
{
    char b[12];
    int n = 11;
    b[n] = 0;
    do {
        b[--n] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    print(&b[n]);
}

static void print_hex16(uint32_t v)
{
    static const char h[] = "0123456789ABCDEF";
    char b[5];
    for (int i = 0; i < 4; i++)
        b[i] = h[(v >> (12 - 4 * i)) & 15];
    b[4] = 0;
    print(b);
}

static void print_ip(const uint8_t *ip)
{
    for (int i = 0; i < 4; i++) {
        print_dec(ip[i]);
        if (i < 3)
            print(".");
    }
}

static uint32_t phy_read(uint32_t reg)
{
    uint32_t v = 0;
    if (HAL_ETH_ReadPHYRegister(&heth, 0, reg, &v) != HAL_OK)
        Error_Handler();
    return v;
}

static void phy_write(uint32_t reg, uint32_t v)
{
    if (HAL_ETH_WritePHYRegister(&heth, 0, reg, v) != HAL_OK)
        Error_Handler();
}

/* RX buffer management required by HAL_ETH_ReadData */
void HAL_ETH_RxAllocateCallback(uint8_t **buff)
{
    *buff = rxpool[pool_i++ % 8u];
}

void HAL_ETH_RxLinkCallback(void **pStart, void **pEnd, uint8_t *buff, uint16_t Length)
{
    *pStart = buff;
    *pEnd = buff;
    rx_buf = buff;
    rx_len = Length;
}

void HAL_ETH_TxFreeCallback(uint32_t *buff)
{
    (void)buff;
}

static void send(uint32_t len)
{
    ETH_BufferTypeDef b = {txbuf, len, NULL};
    txconf.Length = len;
    txconf.TxBuffer = &b;
    txconf.pData = txbuf;
    if (HAL_ETH_Transmit(&heth, &txconf, 20) != HAL_OK)
        eth_error++;
    HAL_ETH_ReleaseTxPacket(&heth);
}

static void arp(uint16_t op, const uint8_t *tha, const uint8_t *tpa, const uint8_t *dst)
{
    uint8_t *f = txbuf;
    memcpy(f, dst, 6);
    memcpy(f + 6, my_mac, 6);
    f[12] = 0x08; f[13] = 0x06;
    f[14] = 0; f[15] = 1; f[16] = 0x08; f[17] = 0; f[18] = 6; f[19] = 4;
    f[20] = 0; f[21] = (uint8_t)op;
    memcpy(f + 22, my_mac, 6);
    memcpy(f + 28, my_ip, 4);
    memcpy(f + 32, tha, 6);
    memcpy(f + 38, tpa, 4);
    send(42);
}

static void handle(const uint8_t *f, unsigned len)
{
    unsigned type = (unsigned)f[12] << 8 | f[13];
    if (type == 0x0806 && len >= 42 && f[21] == 1 && same(f + 38, my_ip, 4)) {
        arp(2, f + 22, f + 28, f + 6);
        arps++;
        print("arp ");
        print_ip(f + 28);
        print("\r\n");
    } else if (type == 0x0800 && len >= 42 && f[23] == 1 && same(f + 30, my_ip, 4) && f[34] == 8) {
        unsigned ihl = (f[14] & 0xFu) * 4u;
        unsigned tot = (unsigned)f[16] << 8 | f[17];
        if (14u + tot > len || tot > sizeof txbuf - 14u)
            return;
        memcpy(txbuf, f + 6, 6);
        memcpy(txbuf + 6, my_mac, 6);
        memcpy(txbuf + 12, f + 12, 14u + tot - 12u);
        memcpy(txbuf + 26, f + 30, 4);
        memcpy(txbuf + 30, f + 26, 4);
        txbuf[22] = 64;
        txbuf[14 + ihl] = 0;
        txbuf[14 + ihl + 2] = txbuf[14 + ihl + 3] = 0;   /* inserted by the MAC */
        send(14u + tot);
        pings++;
        print("ping ");
        print_dec((unsigned)f[14 + ihl + 6] << 8 | f[14 + ihl + 7]);
        print("\r\n");
    }
}

int main(void)
{
    HAL_Init();
    SystemClock_Config();
    huart1.Instance = USART1;
    huart1.Init.BaudRate = 115200;
    huart1.Init.WordLength = UART_WORDLENGTH_8B;
    huart1.Init.StopBits = UART_STOPBITS_1;
    huart1.Init.Parity = UART_PARITY_NONE;
    huart1.Init.Mode = UART_MODE_TX_RX;
    huart1.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart1.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart1) != HAL_OK)
        Error_Handler();
    print("ETH HAL\r\n");

    heth.Instance = ETH;
    heth.Init.MACAddr = my_mac;
    heth.Init.MediaInterface = HAL_ETH_RMII_MODE;
    heth.Init.TxDesc = txdesc;
    heth.Init.RxDesc = rxdesc;
    heth.Init.RxBuffLen = 1524;
    if (HAL_ETH_Init(&heth) != HAL_OK)
        Error_Handler();
    HAL_ETH_SetMDIOClockRange(&heth);                 /* MDC <= 2.5 MHz for the current HCLK */

    print("PHY ");
    print_hex16(phy_read(2));
    print(":");
    print_hex16(phy_read(3));
    print("\r\n");
    phy_write(0, 0x8000u);
    while (phy_read(0) & 0x8000u) { }
    phy_write(0, 0x1200u);                            /* auto-negotiation enable + restart */
    uint32_t t0 = HAL_GetTick();
    while (1) {
        (void)phy_read(1);
        if ((phy_read(1) & 0x24u) == 0x24u)
            break;
        if (HAL_GetTick() - t0 > 5000u) {
            print("no link\r\n");
            Error_Handler();
        }
    }
    uint32_t sp = (phy_read(31) >> 2) & 7u;
    ETH_MACConfigTypeDef mac;
    HAL_ETH_GetMACConfig(&heth, &mac);
    mac.Speed = (sp & 2u) ? ETH_SPEED_100M : ETH_SPEED_10M;
    mac.DuplexMode = (sp & 4u) ? ETH_FULLDUPLEX_MODE : ETH_HALFDUPLEX_MODE;
    HAL_ETH_SetMACConfig(&heth, &mac);
    print("link ");
    print((sp & 2u) ? "100" : "10");
    print((sp & 4u) ? "FD\r\n" : "HD\r\n");

    txconf.Attributes = ETH_TX_PACKETS_FEATURES_CSUM | ETH_TX_PACKETS_FEATURES_CRCPAD;
    txconf.ChecksumCtrl = ETH_CHECKSUM_IPHDR_PAYLOAD_INSERT_PHDR_CALC;
    txconf.CRCPadCtrl = ETH_CRC_PAD_INSERT;
    if (HAL_ETH_Start(&heth) != HAL_OK)
        Error_Handler();

    static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    static const uint8_t zero[6] = {0};
    arp(1, zero, my_ip, bcast);
    print("garp\r\n");

    for (;;) {
        void *p = NULL;
        if (HAL_ETH_ReadData(&heth, &p) == HAL_OK && p != NULL)
            handle(rx_buf, rx_len);
    }
}

void HAL_ETH_MspInit(ETH_HandleTypeDef *h)
{
    GPIO_InitTypeDef g = {0};
    if (h->Instance != ETH)
        return;
    __HAL_RCC_ETH_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_NOPULL;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF11_ETH;
    g.Pin = GPIO_PIN_1 | GPIO_PIN_2 | GPIO_PIN_7;
    HAL_GPIO_Init(GPIOA, &g);
    g.Pin = GPIO_PIN_11 | GPIO_PIN_12 | GPIO_PIN_13;
    HAL_GPIO_Init(GPIOB, &g);
    g.Pin = GPIO_PIN_1 | GPIO_PIN_4 | GPIO_PIN_5;
    HAL_GPIO_Init(GPIOC, &g);
}

void HAL_UART_MspInit(UART_HandleTypeDef *huart)
{
    GPIO_InitTypeDef g = {0};
    if (huart->Instance != USART1)
        return;
    __HAL_RCC_USART1_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    g.Pin = GPIO_PIN_9 | GPIO_PIN_10;
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_PULLUP;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF7_USART1;
    HAL_GPIO_Init(GPIOA, &g);
}

/* As generated by STM32CubeMX for an 8 MHz crystal and 168 MHz SYSCLK. */
static void SystemClock_Config(void)
{
    RCC_OscInitTypeDef osc = {0};
    RCC_ClkInitTypeDef clk = {0};

    __HAL_RCC_PWR_CLK_ENABLE();
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);

    osc.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    osc.HSEState = RCC_HSE_ON;
    osc.PLL.PLLState = RCC_PLL_ON;
    osc.PLL.PLLSource = RCC_PLLSOURCE_HSE;
    osc.PLL.PLLM = 8;
    osc.PLL.PLLN = 336;
    osc.PLL.PLLP = RCC_PLLP_DIV2;
    osc.PLL.PLLQ = 7;
    if (HAL_RCC_OscConfig(&osc) != HAL_OK)
        Error_Handler();

    clk.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
    clk.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
    clk.AHBCLKDivider = RCC_SYSCLK_DIV1;
    clk.APB1CLKDivider = RCC_HCLK_DIV4;
    clk.APB2CLKDivider = RCC_HCLK_DIV2;
    if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_5) != HAL_OK)
        Error_Handler();
}

static void Error_Handler(void)
{
    eth_error = 0xDEAD;
    __disable_irq();
    for (;;) { }
}

void SysTick_Handler(void)
{
    HAL_IncTick();
}
