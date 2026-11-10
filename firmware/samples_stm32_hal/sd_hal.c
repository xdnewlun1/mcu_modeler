/* sd_hal: ST's STM32CubeF4 HAL SD driver (stm32f4xx_hal_sd.c + stm32f4xx_ll_sdmmc.c) against the CHIP_EM SD card
 * (`--sd-card SDIO` or boards/stm32_sdcard_hal.yaml) on an STM32F405ZG.
 *
 * Clock:  HSE 8 MHz -> PLL -> 168 MHz SYSCLK, PLLQ = 7 -> PLL48CLK = SDIOCLK = 48 MHz; APB2 = 84 MHz (USART1).
 * SDIO:   PC8..PC11 D0..D3, PC12 CK, PD2 CMD (AF12, pull-ups). HAL_SD_Init identifies the card at 400 kHz
 *         (ClockDiv 118), then HAL_SD_ConfigWideBusOperation switches to 4 bits (SCR, ACMD6) at
 *         48 MHz / (ClockDiv 1 + 2) = 16 MHz. Transfers are polled (HAL_SD_ReadBlocks / HAL_SD_WriteBlocks).
 * Program: card info; HAL_SD_WriteBlocks 1 block at 100 and 2 blocks at 200 (CMD24, CMD25 + CMD12); waits for
 *         HAL_SD_CARD_TRANSFER; HAL_SD_ReadBlocks them back (CMD17, CMD18 + CMD12) and compares; HAL_SD_Erase of
 *         block 200..201 and a read showing zeros; HAL_SD_GetCardStatus (ACMD13).
 * Output: "SD HAL\r\nCARD v2 SDHC blocks=131072 bs=512\r\nWRITE ok\r\nREAD ok\r\nMULTI ok\r\nERASE ok\r\n"
 *         "STATUS bus=4 class=4\r\ndone\r\n" (or "... error <code>" lines).
 */
#include "stm32f4xx_hal.h"

UART_HandleTypeDef huart1;
SD_HandleTypeDef hsd;
static uint8_t wbuf[1024], rbuf[1024];
volatile uint32_t sd_error, passed;

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

static void print_hex(uint32_t v)
{
    static const char h[] = "0123456789abcdef";
    char b[9];
    for (int i = 0; i < 8; i++)
        b[i] = h[(v >> (28 - 4 * i)) & 15];
    b[8] = 0;
    print(b);
}

static int fail(const char *what)
{
    sd_error = HAL_SD_GetError(&hsd);
    print(what);
    print(" error ");
    print_hex(sd_error);
    print("\r\n");
    return 0;
}

static int wait_transfer(void)
{
    uint32_t t0 = HAL_GetTick();
    while (HAL_SD_GetCardState(&hsd) != HAL_SD_CARD_TRANSFER)
        if (HAL_GetTick() - t0 > 500)
            return 0;
    return 1;
}

static int same(const uint8_t *a, const uint8_t *b, unsigned n)
{
    while (n--)
        if (a[n] != b[n])
            return 0;
    return 1;
}

int main(void)
{
    HAL_SD_CardInfoTypeDef info;
    HAL_SD_CardStatusTypeDef st;
    int ok = 1;

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
    print("SD HAL\r\n");

    hsd.Instance = SDIO;
    hsd.Init.ClockEdge = SDIO_CLOCK_EDGE_RISING;
    hsd.Init.ClockBypass = SDIO_CLOCK_BYPASS_DISABLE;
    hsd.Init.ClockPowerSave = SDIO_CLOCK_POWER_SAVE_DISABLE;
    hsd.Init.BusWide = SDIO_BUS_WIDE_4B;
    hsd.Init.HardwareFlowControl = SDIO_HARDWARE_FLOW_CONTROL_DISABLE;
    hsd.Init.ClockDiv = 1;
    if (HAL_SD_Init(&hsd) != HAL_OK) {
        fail("INIT");
        for (;;) { }
    }
    HAL_SD_GetCardInfo(&hsd, &info);
    print(info.CardVersion == CARD_V2_X ? "CARD v2 " : "CARD v1 ");
    print(info.CardType == CARD_SDHC_SDXC ? "SDHC blocks=" : "SDSC blocks=");
    print_dec(info.LogBlockNbr);
    print(" bs=");
    print_dec(info.LogBlockSize);
    print("\r\n");

    for (unsigned i = 0; i < sizeof wbuf; i++)
        wbuf[i] = (uint8_t)(i * 13 + 5);
    const char *tag = "HAL SD block";
    for (unsigned i = 0; tag[i]; i++)
        wbuf[i] = (uint8_t)tag[i];

    /* single block */
    if (HAL_SD_WriteBlocks(&hsd, wbuf, 100, 1, 1000) != HAL_OK || !wait_transfer())
        ok = fail("WRITE");
    else
        print("WRITE ok\r\n");
    if (HAL_SD_ReadBlocks(&hsd, rbuf, 100, 1, 1000) != HAL_OK || !wait_transfer())
        ok = fail("READ");
    else
        print(same(wbuf, rbuf, 512) ? "READ ok\r\n" : (ok = 0, "READ mismatch\r\n"));

    /* two blocks: CMD25 / CMD18 + CMD12 */
    if (HAL_SD_WriteBlocks(&hsd, wbuf, 200, 2, 1000) != HAL_OK || !wait_transfer())
        ok = fail("MULTI WRITE");
    for (unsigned i = 0; i < sizeof rbuf; i++)
        rbuf[i] = 0;
    if (HAL_SD_ReadBlocks(&hsd, rbuf, 200, 2, 1000) != HAL_OK || !wait_transfer())
        ok = fail("MULTI READ");
    else
        print(same(wbuf, rbuf, 1024) ? "MULTI ok\r\n" : (ok = 0, "MULTI mismatch\r\n"));

    /* erase 200..201: reads back as zeros (DATA_STAT_AFTER_ERASE = 0) */
    if (HAL_SD_Erase(&hsd, 200, 201) != HAL_OK || !wait_transfer())
        ok = fail("ERASE");
    else if (HAL_SD_ReadBlocks(&hsd, rbuf, 200, 1, 1000) != HAL_OK || !wait_transfer())
        ok = fail("ERASE READ");
    else {
        int zero = 1;
        for (unsigned i = 0; i < 512; i++)
            zero &= rbuf[i] == 0;
        print(zero ? "ERASE ok\r\n" : (ok = 0, "ERASE mismatch\r\n"));
    }

    if (HAL_SD_GetCardStatus(&hsd, &st) != HAL_OK)
        ok = fail("STATUS");
    else {
        print("STATUS bus=");
        print_dec(st.DataBusWidth == 2 ? 4 : 1);
        print(" class=");
        print_dec(st.SpeedClass);
        print("\r\n");
    }
    passed = ok;
    print(ok ? "done\r\n" : "failed\r\n");
    for (;;) {
        __WFI();
    }
}

void HAL_SD_MspInit(SD_HandleTypeDef *h)
{
    GPIO_InitTypeDef g = {0};
    if (h->Instance != SDIO)
        return;
    __HAL_RCC_SDIO_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();
    g.Pin = GPIO_PIN_8 | GPIO_PIN_9 | GPIO_PIN_10 | GPIO_PIN_11;
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_PULLUP;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF12_SDIO;
    HAL_GPIO_Init(GPIOC, &g);
    g.Pin = GPIO_PIN_12;
    g.Pull = GPIO_NOPULL;
    HAL_GPIO_Init(GPIOC, &g);
    g.Pin = GPIO_PIN_2;
    g.Pull = GPIO_PULLUP;
    HAL_GPIO_Init(GPIOD, &g);
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

/* As generated by STM32CubeMX for an 8 MHz crystal, 168 MHz SYSCLK and 48 MHz PLL48CLK. */
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
    __disable_irq();
    for (;;) { }
}

void SysTick_Handler(void)
{
    HAL_IncTick();
}
