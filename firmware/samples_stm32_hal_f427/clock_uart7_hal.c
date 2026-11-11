/* STM32CubeF4 HAL compatibility sample for the STM32F427xx device header (chips/stm32f427vg.yaml):
 * HAL_RCC_OscConfig (HSE 8 MHz, PLL 360/8/2 = 180 MHz) + HAL_PWREx_EnableOverDrive (polls PWR_CSR.ODRDY/ODSWRDY,
 * RM0090 5.1.4) + HAL_RCC_ClockConfig (5 wait states, APB1 45 MHz / APB2 90 MHz), HAL_RCCEx_PeriphCLKConfig for the
 * SAI1 block A clock from PLLSAI (PLLSAIN 344, PLLSAIQ 7, PLLSAIDIVQ 1: polls RCC_CR.PLLSAIRDY, writes RCC_DCKCFGR),
 * then HAL_UART on UART7 (PE8 TX / PE7 RX, AF8): prints the clock frequencies, waits with HAL_Delay (SysTick at
 * 180 MHz) and echoes received bytes upper-cased. */
#include "stm32f4xx_hal.h"

static UART_HandleTypeDef huart7;
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
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);
    osc.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    osc.HSEState = RCC_HSE_ON;
    osc.PLL.PLLState = RCC_PLL_ON;
    osc.PLL.PLLSource = RCC_PLLSOURCE_HSE;
    osc.PLL.PLLM = 8;
    osc.PLL.PLLN = 360;
    osc.PLL.PLLP = RCC_PLLP_DIV2;
    osc.PLL.PLLQ = 7;
    if (HAL_RCC_OscConfig(&osc) != HAL_OK)
        fail(1);
    if (HAL_PWREx_EnableOverDrive() != HAL_OK)
        fail(2);
    clk.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
    clk.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
    clk.AHBCLKDivider = RCC_SYSCLK_DIV1;
    clk.APB1CLKDivider = RCC_HCLK_DIV4;
    clk.APB2CLKDivider = RCC_HCLK_DIV2;
    if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_5) != HAL_OK)
        fail(3);
    per.PeriphClockSelection = RCC_PERIPHCLK_SAI_PLLSAI;
    per.PLLSAI.PLLSAIN = 344;
    per.PLLSAI.PLLSAIQ = 7;
    per.PLLSAIDivQ = 1;
    if (HAL_RCCEx_PeriphCLKConfig(&per) != HAL_OK)
        fail(4);
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

    __HAL_RCC_GPIOE_CLK_ENABLE();
    __HAL_RCC_UART7_CLK_ENABLE();
    GPIO_InitTypeDef gp = {0};
    gp.Pin = GPIO_PIN_7 | GPIO_PIN_8;
    gp.Mode = GPIO_MODE_AF_PP;
    gp.Pull = GPIO_PULLUP;
    gp.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    gp.Alternate = GPIO_AF8_UART7;
    HAL_GPIO_Init(GPIOE, &gp);
    huart7.Instance = UART7;
    huart7.Init.BaudRate = 115200;
    huart7.Init.WordLength = UART_WORDLENGTH_8B;
    huart7.Init.StopBits = UART_STOPBITS_1;
    huart7.Init.Parity = UART_PARITY_NONE;
    huart7.Init.Mode = UART_MODE_TX_RX;
    huart7.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart7.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart7) != HAL_OK)
        fail(5);

    uint32_t t0 = HAL_GetTick();
    HAL_Delay(5);
    delay_ticks = HAL_GetTick() - t0;

    char buf[96], *p = buf;
    put_str(&p, "HAL F427 sys ");
    put_u32(&p, HAL_RCC_GetSysClockFreq());
    put_str(&p, " p1 ");
    put_u32(&p, HAL_RCC_GetPCLK1Freq());
    put_str(&p, " p2 ");
    put_u32(&p, HAL_RCC_GetPCLK2Freq());
    put_str(&p, "\r\n");
    HAL_UART_Transmit(&huart7, (uint8_t *)buf, (uint16_t)(p - buf), 100);

    for (;;) {
        uint8_t c;
        if (HAL_UART_Receive(&huart7, &c, 1, 1000) == HAL_OK) {
            if (c >= 'a' && c <= 'z')
                c = (uint8_t)(c - 'a' + 'A');
            HAL_UART_Transmit(&huart7, &c, 1, 100);
        }
    }
}
