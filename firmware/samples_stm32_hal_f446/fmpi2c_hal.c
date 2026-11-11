/* STM32CubeF4 HAL_FMPI2C sample for the STM32F446xx device header (chips/stm32f446re.yaml, board
 * boards/stm32f446re_fmpi2c_eeprom.yaml: 24C02 EEPROM at 0x50 on PC6 SCL / PC7 SDA, AF4).
 * HSI 16 MHz (no PLL); HAL_RCCEx_PeriphCLKConfig selects HSI as the FMPI2C1 kernel clock (DCKCFGR2.FMPI2C1SEL = 10),
 * HAL_FMPI2CEx_EnableFastModePlus sets SYSCFG_CFGR FMPI2C1_SCL/SDA, Timing 0x00200204 = RM0390 Table 135 Fm+ 1 MHz at
 * fI2CCLK 16 MHz.  HAL_FMPI2C_Mem_Write per 8-byte page + HAL_FMPI2C_IsDeviceReady (acknowledge polling of the write
 * cycle), HAL_FMPI2C_Mem_Read of the whole string, HAL_UART on USART2 (PA2) prints "HAL EEPROM: Hello HAL FMPI2C!".
 * Polling APIs only: the shared startup (../samples_stm32_hal/startup.c) has the 82-vector STM32F405 table, without
 * the FMPI2C1 vectors 95/96. */
#include "stm32f4xx_hal.h"

static unsigned slen(const char *s) { unsigned n = 0; while (s[n]) n++; return n; }

static UART_HandleTypeDef huart2;
static FMPI2C_HandleTypeDef hfmpi2c;
volatile uint32_t hal_status;            /* 0 = ok, otherwise the step that failed */

void SysTick_Handler(void) { HAL_IncTick(); }

static void fail(uint32_t step)
{
    hal_status = step;
    for (;;) { }
}

static void puts2(const char *s)
{
    HAL_UART_Transmit(&huart2, (uint8_t *)s, (uint16_t)slen(s), 100);
}

void HAL_UART_MspInit(UART_HandleTypeDef *h)
{
    GPIO_InitTypeDef g = {0};
    (void)h;
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_USART2_CLK_ENABLE();
    g.Pin = GPIO_PIN_2 | GPIO_PIN_3;
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_PULLUP;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF7_USART2;
    HAL_GPIO_Init(GPIOA, &g);
}

void HAL_FMPI2C_MspInit(FMPI2C_HandleTypeDef *h)
{
    GPIO_InitTypeDef g = {0};
    RCC_PeriphCLKInitTypeDef per = {0};
    (void)h;
    per.PeriphClockSelection = RCC_PERIPHCLK_FMPI2C1;
    per.Fmpi2c1ClockSelection = RCC_FMPI2C1CLKSOURCE_HSI;
    if (HAL_RCCEx_PeriphCLKConfig(&per) != HAL_OK)
        fail(10);
    __HAL_RCC_GPIOC_CLK_ENABLE();
    g.Pin = GPIO_PIN_6 | GPIO_PIN_7;
    g.Mode = GPIO_MODE_AF_OD;
    g.Pull = GPIO_NOPULL;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF4_FMPI2C1;
    HAL_GPIO_Init(GPIOC, &g);
    __HAL_RCC_FMPI2C1_CLK_ENABLE();
}

int main(void)
{
    static const char msg[] = "Hello HAL FMPI2C!";
    char buf[sizeof msg];
    uint16_t n = sizeof msg - 1, i;

    HAL_Init();
    huart2.Instance = USART2;
    huart2.Init.BaudRate = 115200;
    huart2.Init.WordLength = UART_WORDLENGTH_8B;
    huart2.Init.StopBits = UART_STOPBITS_1;
    huart2.Init.Parity = UART_PARITY_NONE;
    huart2.Init.Mode = UART_MODE_TX_RX;
    huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart2.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart2) != HAL_OK)
        fail(1);

    hfmpi2c.Instance = FMPI2C1;
    hfmpi2c.Init.Timing = 0x00200204u;
    hfmpi2c.Init.OwnAddress1 = 0;
    hfmpi2c.Init.AddressingMode = FMPI2C_ADDRESSINGMODE_7BIT;
    hfmpi2c.Init.DualAddressMode = FMPI2C_DUALADDRESS_DISABLE;
    hfmpi2c.Init.OwnAddress2 = 0;
    hfmpi2c.Init.OwnAddress2Masks = FMPI2C_OA2_NOMASK;
    hfmpi2c.Init.GeneralCallMode = FMPI2C_GENERALCALL_DISABLE;
    hfmpi2c.Init.NoStretchMode = FMPI2C_NOSTRETCH_DISABLE;
    if (HAL_FMPI2C_Init(&hfmpi2c) != HAL_OK)
        fail(2);
    if (HAL_FMPI2CEx_ConfigAnalogFilter(&hfmpi2c, FMPI2C_ANALOGFILTER_ENABLE) != HAL_OK)
        fail(3);
    HAL_FMPI2CEx_EnableFastModePlus(FMPI2C_FASTMODEPLUS_SCL | FMPI2C_FASTMODEPLUS_SDA);

    for (i = 0; i < n; ) {                              /* 8-byte pages of the 24C02 */
        uint16_t a = (uint16_t)(0x40 + i), k = (uint16_t)(8 - (a & 7));
        if (k > n - i)
            k = (uint16_t)(n - i);
        if (HAL_FMPI2C_Mem_Write(&hfmpi2c, 0x50 << 1, a, FMPI2C_MEMADD_SIZE_8BIT, (uint8_t *)msg + i, k, 50) != HAL_OK)
            fail(4);
        i = (uint16_t)(i + k);
        if (HAL_FMPI2C_IsDeviceReady(&hfmpi2c, 0x50 << 1, 1000, 50) != HAL_OK)
            fail(5);
    }
    if (HAL_FMPI2C_Mem_Read(&hfmpi2c, 0x50 << 1, 0x40, FMPI2C_MEMADD_SIZE_8BIT, (uint8_t *)buf, n, 50) != HAL_OK)
        fail(6);
    buf[n] = 0;
    puts2("HAL EEPROM: ");
    puts2(buf);
    puts2("\r\n");
    for (;;) { }
}
