/* STM32CubeF4 HAL sample for the STM32F401xE / STM32F411xE device headers (chips/stm32f401re.yaml,
 * chips/stm32f411re.yaml), built twice by build.sh (build/f401_clock_uart_hal.elf, build/f411_clock_uart_hal.elf):
 * __HAL_PWR_VOLTAGESCALING_CONFIG (F401 scale 2, F411 scale 1) + HAL_RCC_OscConfig (HSE 8 MHz bypass, PLL:
 * F401 8/336/4 = 84 MHz, F411 4/100/2 = 100 MHz) + HAL_RCC_ClockConfig (F401 2, F411 3 wait states, APB1 = HCLK/2,
 * APB2 = HCLK), HAL_RCCEx_PeriphCLKConfig: TIMPRE (RCC_DCKCFGR) on both, and the PLLI2S on the F411 with its own input
 * divider PLLI2SM = 8 (PLLI2SN 192, PLLI2SR 2: 8 MHz / 8 x 192 / 2 = 96 MHz; HAL polls RCC_CR.PLLI2SRDY).
 * Then HAL_UART on USART2 (PA2 TX / PA3 RX, AF7, 115200): prints "HAL F4x1 sys <SYSCLK> p1 <PCLK1> p2 <PCLK2>
 * i2s <I2S clock>\r\n", measures HAL_Delay(5) with SysTick, and echoes received bytes upper-cased. */
#include "stm32f4xx_hal.h"

static UART_HandleTypeDef huart2;
volatile uint32_t hal_status;            /* 0 = ok, otherwise the step that failed */
volatile uint32_t delay_ticks;           /* HAL_GetTick() difference across HAL_Delay(5) */

void SysTick_Handler(void) { HAL_IncTick(); }

static void fail(uint32_t step)
{
    hal_status = step;
    for (;;) { }
}

static void SystemClock_Config(void)
{
    RCC_OscInitTypeDef osc = {0};
    RCC_ClkInitTypeDef clk = {0};
    RCC_PeriphCLKInitTypeDef per = {0};

    __HAL_RCC_PWR_CLK_ENABLE();
#if defined(STM32F411xE)
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);
#else
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE2);
#endif
    osc.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    osc.HSEState = RCC_HSE_BYPASS;
    osc.PLL.PLLState = RCC_PLL_ON;
    osc.PLL.PLLSource = RCC_PLLSOURCE_HSE;
#if defined(STM32F411xE)
    osc.PLL.PLLM = 4;
    osc.PLL.PLLN = 100;
    osc.PLL.PLLP = RCC_PLLP_DIV2;
    osc.PLL.PLLQ = 4;
#else
    osc.PLL.PLLM = 8;
    osc.PLL.PLLN = 336;
    osc.PLL.PLLP = RCC_PLLP_DIV4;
    osc.PLL.PLLQ = 7;
#endif
    if (HAL_RCC_OscConfig(&osc) != HAL_OK)
        fail(1);
    clk.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
    clk.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
    clk.AHBCLKDivider = RCC_SYSCLK_DIV1;
    clk.APB1CLKDivider = RCC_HCLK_DIV2;
    clk.APB2CLKDivider = RCC_HCLK_DIV1;
#if defined(STM32F411xE)
    if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_3) != HAL_OK)
        fail(2);
    per.PeriphClockSelection = RCC_PERIPHCLK_TIM | RCC_PERIPHCLK_I2S;
    per.PLLI2S.PLLI2SM = 8;
    per.PLLI2S.PLLI2SN = 192;
    per.PLLI2S.PLLI2SR = 2;
#else
    if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_2) != HAL_OK)
        fail(2);
    per.PeriphClockSelection = RCC_PERIPHCLK_TIM;
#endif
    per.TIMPresSelection = RCC_TIMPRES_ACTIVATED;
    if (HAL_RCCEx_PeriphCLKConfig(&per) != HAL_OK)
        fail(3);
}

static void put_u32(char **p, uint32_t v)
{
    char tmp[10];
    int n = 0;
    do { tmp[n++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    while (n)
        *(*p)++ = tmp[--n];
}

static void put_str(char **p, const char *s)
{
    while (*s)
        *(*p)++ = *s++;
}

int main(void)
{
    HAL_Init();
    SystemClock_Config();

    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_USART2_CLK_ENABLE();
    GPIO_InitTypeDef gp = {0};
    gp.Pin = GPIO_PIN_2 | GPIO_PIN_3;
    gp.Mode = GPIO_MODE_AF_PP;
    gp.Pull = GPIO_PULLUP;
    gp.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    gp.Alternate = GPIO_AF7_USART2;
    HAL_GPIO_Init(GPIOA, &gp);
    huart2.Instance = USART2;
    huart2.Init.BaudRate = 115200;
    huart2.Init.WordLength = UART_WORDLENGTH_8B;
    huart2.Init.StopBits = UART_STOPBITS_1;
    huart2.Init.Parity = UART_PARITY_NONE;
    huart2.Init.Mode = UART_MODE_TX_RX;
    huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart2.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart2) != HAL_OK)
        fail(4);

    uint32_t t0 = HAL_GetTick();
    HAL_Delay(5);
    delay_ticks = HAL_GetTick() - t0;

    char buf[96], *p = buf;
    put_str(&p, "HAL F4x1 sys ");
    put_u32(&p, HAL_RCC_GetSysClockFreq());
    put_str(&p, " p1 ");
    put_u32(&p, HAL_RCC_GetPCLK1Freq());
    put_str(&p, " p2 ");
    put_u32(&p, HAL_RCC_GetPCLK2Freq());
#if defined(STM32F411xE)
    put_str(&p, " i2s ");
    put_u32(&p, HAL_RCCEx_GetPeriphCLKFreq(RCC_PERIPHCLK_I2S));
#endif
    put_str(&p, "\r\n");
    HAL_UART_Transmit(&huart2, (uint8_t *)buf, (uint16_t)(p - buf), 100);

    for (;;) {
        uint8_t c;
        if (HAL_UART_Receive(&huart2, &c, 1, 1000) == HAL_OK) {
            if (c >= 'a' && c <= 'z')
                c = (uint8_t)(c - 'a' + 'A');
            HAL_UART_Transmit(&huart2, &c, 1, 100);
        }
    }
}
