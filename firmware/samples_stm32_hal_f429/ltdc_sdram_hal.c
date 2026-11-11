/* STM32CubeF4 HAL sample for the STM32F429xx device header (chips/stm32f429zi.yaml, board
 * boards/stm32f429zi_disco_lcd.yaml: 32F429IDISCOVERY wiring, 8 MB SDRAM on FMC bank 2, 240x320 RGB TFT).
 * HAL_RCC_OscConfig + HAL_PWREx_EnableOverDrive + HAL_RCC_ClockConfig: 180 MHz; HAL_RCCEx_PeriphCLKConfig LTDC:
 * PLLSAIN 192, PLLSAIR 4, PLLSAIDIVR /8 -> LCD_CLK 6 MHz (polls RCC_CR.PLLSAIRDY);
 * HAL_SDRAM_Init + HAL_SDRAM_SendCommand (clock enable, PALL, 4 auto-refresh, load mode 0x230; polls FMC_SDSR.BUSY)
 * + HAL_SDRAM_ProgramRefreshRate(1386), like ST's BSP (stm32f429i_discovery_sdram.c);
 * HAL_DMA2D register-to-memory fills of the RGB565 frame buffer at 0xD0000000 (top half red, bottom half blue; polls
 * DMA2D TCIF through HAL_DMA2D_PollForTransfer); HAL_LTDC_Init (BSP ILI9341 RGB timings: HSYNC 10, HBP 20, HFP 10,
 * VSYNC 2, VBP 2, VFP 4; all polarities active low, IPC) + HAL_LTDC_ConfigLayer (layer 1 RGB565 240x320, constant
 * alpha 255, PAxCA blending), HAL_LTDC_ProgramLineEvent(0) re-armed in HAL_LTDC_LineEventCallback (LTDC IRQ 88).
 * USART1 (PA9, 115200) prints "HAL F429 LTDC sys 180000000 sdram ok\r\n" and after 5 frames "frames 5\r\n". */
#include <stddef.h>
#include "stm32f4xx_hal.h"

static UART_HandleTypeDef huart1;
static SDRAM_HandleTypeDef hsdram;
static LTDC_HandleTypeDef hltdc;
static DMA2D_HandleTypeDef hdma2d;
volatile uint32_t hal_status;            /* 0 = ok, otherwise the step that failed */
volatile uint32_t line_events;

#define FB 0xD0000000u
#define W 240u
#define H 320u

/* no libc: the f42x startup does not provide these (clang may emit calls for struct initialisers) */
void *memset(void *d, int c, size_t n)
{
    unsigned char *p = d;
    while (n--)
        *p++ = (unsigned char)c;
    return d;
}

void *memcpy(void *d, const void *s, size_t n)
{
    unsigned char *p = d;
    const unsigned char *q = s;
    while (n--)
        *p++ = *q++;
    return d;
}

void SysTick_Handler(void) { HAL_IncTick(); }
void LTDC_IRQHandler(void) { HAL_LTDC_IRQHandler(&hltdc); }

void HAL_LTDC_LineEventCallback(LTDC_HandleTypeDef *h)
{
    line_events++;
    HAL_LTDC_ProgramLineEvent(h, 0);
}

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
    per.PeriphClockSelection = RCC_PERIPHCLK_LTDC;
    per.PLLSAI.PLLSAIN = 192;
    per.PLLSAI.PLLSAIR = 4;
    per.PLLSAIDivR = RCC_PLLSAIDIVR_8;
    if (HAL_RCCEx_PeriphCLKConfig(&per) != HAL_OK)
        fail(4);
}

static void gpio_af(GPIO_TypeDef *port, uint32_t pins, uint32_t af)
{
    GPIO_InitTypeDef g = {0};
    g.Pin = pins;
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_NOPULL;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = af;
    HAL_GPIO_Init(port, &g);
}

static void sdram_init(void)
{
    FMC_SDRAM_TimingTypeDef t = {0};
    FMC_SDRAM_CommandTypeDef cmd = {0};

    __HAL_RCC_FMC_CLK_ENABLE();
    gpio_af(GPIOB, GPIO_PIN_5 | GPIO_PIN_6, GPIO_AF12_FMC);
    gpio_af(GPIOC, GPIO_PIN_0, GPIO_AF12_FMC);
    gpio_af(GPIOD, GPIO_PIN_0 | GPIO_PIN_1 | GPIO_PIN_8 | GPIO_PIN_9 | GPIO_PIN_10 | GPIO_PIN_14 | GPIO_PIN_15,
            GPIO_AF12_FMC);
    gpio_af(GPIOE, GPIO_PIN_0 | GPIO_PIN_1 | GPIO_PIN_7 | GPIO_PIN_8 | GPIO_PIN_9 | GPIO_PIN_10 | GPIO_PIN_11 |
            GPIO_PIN_12 | GPIO_PIN_13 | GPIO_PIN_14 | GPIO_PIN_15, GPIO_AF12_FMC);
    gpio_af(GPIOF, GPIO_PIN_0 | GPIO_PIN_1 | GPIO_PIN_2 | GPIO_PIN_3 | GPIO_PIN_4 | GPIO_PIN_5 | GPIO_PIN_11 |
            GPIO_PIN_12 | GPIO_PIN_13 | GPIO_PIN_14 | GPIO_PIN_15, GPIO_AF12_FMC);
    gpio_af(GPIOG, GPIO_PIN_0 | GPIO_PIN_1 | GPIO_PIN_4 | GPIO_PIN_5 | GPIO_PIN_8 | GPIO_PIN_15, GPIO_AF12_FMC);

    hsdram.Instance = FMC_SDRAM_DEVICE;
    hsdram.Init.SDBank = FMC_SDRAM_BANK2;
    hsdram.Init.ColumnBitsNumber = FMC_SDRAM_COLUMN_BITS_NUM_8;
    hsdram.Init.RowBitsNumber = FMC_SDRAM_ROW_BITS_NUM_12;
    hsdram.Init.MemoryDataWidth = FMC_SDRAM_MEM_BUS_WIDTH_16;
    hsdram.Init.InternalBankNumber = FMC_SDRAM_INTERN_BANKS_NUM_4;
    hsdram.Init.CASLatency = FMC_SDRAM_CAS_LATENCY_3;
    hsdram.Init.WriteProtection = FMC_SDRAM_WRITE_PROTECTION_DISABLE;
    hsdram.Init.SDClockPeriod = FMC_SDRAM_CLOCK_PERIOD_2;
    hsdram.Init.ReadBurst = FMC_SDRAM_RBURST_DISABLE;
    hsdram.Init.ReadPipeDelay = FMC_SDRAM_RPIPE_DELAY_1;
    t.LoadToActiveDelay = 2;
    t.ExitSelfRefreshDelay = 7;
    t.SelfRefreshTime = 4;
    t.RowCycleDelay = 7;
    t.WriteRecoveryTime = 2;
    t.RPDelay = 2;
    t.RCDDelay = 2;
    if (HAL_SDRAM_Init(&hsdram, &t) != HAL_OK)
        fail(5);
    cmd.CommandTarget = FMC_SDRAM_CMD_TARGET_BANK2;
    cmd.CommandMode = FMC_SDRAM_CMD_CLK_ENABLE;
    cmd.AutoRefreshNumber = 1;
    if (HAL_SDRAM_SendCommand(&hsdram, &cmd, 0xFFFF) != HAL_OK)
        fail(6);
    HAL_Delay(1);
    cmd.CommandMode = FMC_SDRAM_CMD_PALL;
    if (HAL_SDRAM_SendCommand(&hsdram, &cmd, 0xFFFF) != HAL_OK)
        fail(7);
    cmd.CommandMode = FMC_SDRAM_CMD_AUTOREFRESH_MODE;
    cmd.AutoRefreshNumber = 4;
    if (HAL_SDRAM_SendCommand(&hsdram, &cmd, 0xFFFF) != HAL_OK)
        fail(8);
    cmd.CommandMode = FMC_SDRAM_CMD_LOAD_MODE;
    cmd.AutoRefreshNumber = 1;
    cmd.ModeRegisterDefinition = 0x0230;          /* burst length 1, sequential, CAS 3, single write burst */
    if (HAL_SDRAM_SendCommand(&hsdram, &cmd, 0xFFFF) != HAL_OK)
        fail(9);
    if (HAL_SDRAM_ProgramRefreshRate(&hsdram, 1386) != HAL_OK)
        fail(10);
}

static void fill(uint32_t addr, uint32_t lines, uint32_t color)
{
    hdma2d.Instance = DMA2D;
    hdma2d.Init.Mode = DMA2D_R2M;
    hdma2d.Init.ColorMode = DMA2D_OUTPUT_RGB565;
    hdma2d.Init.OutputOffset = 0;
    if (HAL_DMA2D_Init(&hdma2d) != HAL_OK)
        fail(11);
    if (HAL_DMA2D_Start(&hdma2d, color, addr, W, lines) != HAL_OK)
        fail(12);
    if (HAL_DMA2D_PollForTransfer(&hdma2d, 100) != HAL_OK)
        fail(13);
}

static void ltdc_init(void)
{
    LTDC_LayerCfgTypeDef l = {0};

    __HAL_RCC_LTDC_CLK_ENABLE();
    gpio_af(GPIOA, GPIO_PIN_3 | GPIO_PIN_4 | GPIO_PIN_6 | GPIO_PIN_11 | GPIO_PIN_12, GPIO_AF14_LTDC);
    gpio_af(GPIOB, GPIO_PIN_8 | GPIO_PIN_9 | GPIO_PIN_10 | GPIO_PIN_11, GPIO_AF14_LTDC);
    gpio_af(GPIOB, GPIO_PIN_0 | GPIO_PIN_1, GPIO_AF9_LTDC);
    gpio_af(GPIOC, GPIO_PIN_6 | GPIO_PIN_7 | GPIO_PIN_10, GPIO_AF14_LTDC);
    gpio_af(GPIOD, GPIO_PIN_3 | GPIO_PIN_6, GPIO_AF14_LTDC);
    gpio_af(GPIOF, GPIO_PIN_10, GPIO_AF14_LTDC);
    gpio_af(GPIOG, GPIO_PIN_6 | GPIO_PIN_7 | GPIO_PIN_11, GPIO_AF14_LTDC);
    gpio_af(GPIOG, GPIO_PIN_10 | GPIO_PIN_12, GPIO_AF9_LTDC);

    hltdc.Instance = LTDC;
    hltdc.Init.HSPolarity = LTDC_HSPOLARITY_AL;
    hltdc.Init.VSPolarity = LTDC_VSPOLARITY_AL;
    hltdc.Init.DEPolarity = LTDC_DEPOLARITY_AL;
    hltdc.Init.PCPolarity = LTDC_PCPOLARITY_IPC;
    hltdc.Init.HorizontalSync = 9;
    hltdc.Init.VerticalSync = 1;
    hltdc.Init.AccumulatedHBP = 29;
    hltdc.Init.AccumulatedVBP = 3;
    hltdc.Init.AccumulatedActiveW = 269;
    hltdc.Init.AccumulatedActiveH = 323;
    hltdc.Init.TotalWidth = 279;
    hltdc.Init.TotalHeigh = 327;
    hltdc.Init.Backcolor.Red = 0;
    hltdc.Init.Backcolor.Green = 0;
    hltdc.Init.Backcolor.Blue = 0;
    if (HAL_LTDC_Init(&hltdc) != HAL_OK)
        fail(14);
    l.WindowX0 = 0;
    l.WindowX1 = W;
    l.WindowY0 = 0;
    l.WindowY1 = H;
    l.PixelFormat = LTDC_PIXEL_FORMAT_RGB565;
    l.FBStartAdress = FB;
    l.Alpha = 255;
    l.Alpha0 = 0;
    l.BlendingFactor1 = LTDC_BLENDING_FACTOR1_PAxCA;
    l.BlendingFactor2 = LTDC_BLENDING_FACTOR2_PAxCA;
    l.ImageWidth = W;
    l.ImageHeight = H;
    if (HAL_LTDC_ConfigLayer(&hltdc, &l, 0) != HAL_OK)
        fail(15);
    HAL_NVIC_SetPriority(LTDC_IRQn, 5, 0);
    HAL_NVIC_EnableIRQ(LTDC_IRQn);
    if (HAL_LTDC_ProgramLineEvent(&hltdc, 0) != HAL_OK)
        fail(16);
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
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();
    __HAL_RCC_GPIOE_CLK_ENABLE();
    __HAL_RCC_GPIOF_CLK_ENABLE();
    __HAL_RCC_GPIOG_CLK_ENABLE();
    __HAL_RCC_USART1_CLK_ENABLE();
    __HAL_RCC_DMA2D_CLK_ENABLE();
    gpio_af(GPIOA, GPIO_PIN_9 | GPIO_PIN_10, GPIO_AF7_USART1);
    huart1.Instance = USART1;
    huart1.Init.BaudRate = 115200;
    huart1.Init.WordLength = UART_WORDLENGTH_8B;
    huart1.Init.StopBits = UART_STOPBITS_1;
    huart1.Init.Parity = UART_PARITY_NONE;
    huart1.Init.Mode = UART_MODE_TX_RX;
    huart1.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart1.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart1) != HAL_OK)
        fail(17);

    sdram_init();
    fill(FB, H / 2u, 0xFFFF0000u);                              /* red (HAL takes ARGB8888, converts to RGB565) */
    fill(FB + W * 2u * (H / 2u), H / 2u, 0xFF0000FFu);          /* blue */
    volatile uint16_t *fb = (volatile uint16_t *)FB;
    int ok = fb[0] == 0xF800u && fb[W * H - 1u] == 0x001Fu;
    ltdc_init();

    char buf[64], *p = buf;
    put_str(&p, "HAL F429 LTDC sys ");
    put_u32(&p, HAL_RCC_GetSysClockFreq());
    put_str(&p, ok ? " sdram ok\r\n" : " sdram FAIL\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)buf, (uint16_t)(p - buf), 100);
    while (line_events < 5u) { }
    p = buf;
    put_str(&p, "frames ");
    put_u32(&p, line_events);
    put_str(&p, "\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)buf, (uint16_t)(p - buf), 100);
    for (;;) { }
}
