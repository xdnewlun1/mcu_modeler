/* STM32CubeF4 HAL sample for the STM32F446xx device header (chips/stm32f446re.yaml, NUCLEO-F446RE):
 * HAL_RCC_OscConfig (HSE bypass 8 MHz, PLLM 4, PLLN 180, PLLP/PLLR /2, PLLQ 7) + HAL_PWREx_EnableOverDrive +
 * HAL_RCC_ClockConfig with RCC_SYSCLKSOURCE_PLLRCLK (SYSCLK = PLL_R = 180 MHz, 5 wait states, APB1 45 / APB2 90 MHz),
 * HAL_RCCEx_PeriphCLKConfig for CLK48 = PLLSAI_P (PLLSAIM 4, PLLSAIN 96, PLLSAIP /4 = 48 MHz: DCKCFGR2.CK48MSEL),
 * HAL_UART on USART2 (PA2/PA3) and HAL_QSPI against a W25Q128-style flash: HAL_QSPI_Init, read ID (0x9F),
 * write enable + sector erase + HAL_QSPI_AutoPolling (WIP = 0, APMS), page program (HAL_QSPI_Transmit), Fast Read
 * Dual I/O 0xBB (HAL_QSPI_Receive) and HAL_QSPI_MemoryMapped with 0x3B.  The LQFP64 package has no
 * QUADSPI_BK1_IO2 pin, hence the dual I/O commands (see ../samples_stm32f446/qspi_flash.c). */
#include "stm32f4xx_hal.h"

static unsigned slen(const char *s) { unsigned n = 0; while (s[n]) n++; return n; }
static void clr(uint8_t *p, unsigned n) { while (n--) *p++ = 0; }

static UART_HandleTypeDef huart2;
static QSPI_HandleTypeDef hqspi;
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

static void hex(uint32_t v, int digits)
{
    char b[9];
    for (int i = 0; i < digits; i++)
        b[i] = "0123456789ABCDEF"[(v >> (4 * (digits - 1 - i))) & 15u];
    b[digits] = 0;
    puts2(b);
}

static void SystemClock_Config(void)
{
    RCC_OscInitTypeDef osc = {0};
    RCC_ClkInitTypeDef clk = {0};
    RCC_PeriphCLKInitTypeDef per = {0};

    __HAL_RCC_PWR_CLK_ENABLE();
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);
    osc.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    osc.HSEState = RCC_HSE_BYPASS;
    osc.PLL.PLLState = RCC_PLL_ON;
    osc.PLL.PLLSource = RCC_PLLSOURCE_HSE;
    osc.PLL.PLLM = 4;
    osc.PLL.PLLN = 180;
    osc.PLL.PLLP = RCC_PLLP_DIV2;
    osc.PLL.PLLQ = 7;
    osc.PLL.PLLR = 2;
    if (HAL_RCC_OscConfig(&osc) != HAL_OK)
        fail(1);
    if (HAL_PWREx_EnableOverDrive() != HAL_OK)
        fail(2);
    clk.ClockType = RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
    clk.SYSCLKSource = RCC_SYSCLKSOURCE_PLLRCLK;
    clk.AHBCLKDivider = RCC_SYSCLK_DIV1;
    clk.APB1CLKDivider = RCC_HCLK_DIV4;
    clk.APB2CLKDivider = RCC_HCLK_DIV2;
    if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_5) != HAL_OK)
        fail(3);
    per.PeriphClockSelection = RCC_PERIPHCLK_CLK48;
    per.Clk48ClockSelection = RCC_CLK48CLKSOURCE_PLLSAIP;
    per.PLLSAI.PLLSAIM = 4;
    per.PLLSAI.PLLSAIN = 96;
    per.PLLSAI.PLLSAIP = RCC_PLLSAIP_DIV4;
    per.PLLSAI.PLLSAIQ = 2;
    if (HAL_RCCEx_PeriphCLKConfig(&per) != HAL_OK)
        fail(4);
}

void HAL_QSPI_MspInit(QSPI_HandleTypeDef *h)
{
    GPIO_InitTypeDef g = {0};
    (void)h;
    __HAL_RCC_QSPI_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_NOPULL;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Pin = GPIO_PIN_2;                          /* QUADSPI_CLK */
    g.Alternate = GPIO_AF9_QSPI;
    HAL_GPIO_Init(GPIOB, &g);
    g.Pin = GPIO_PIN_6;                          /* QUADSPI_BK1_NCS */
    g.Alternate = GPIO_AF10_QSPI;
    HAL_GPIO_Init(GPIOB, &g);
    g.Pin = GPIO_PIN_9 | GPIO_PIN_10;            /* QUADSPI_BK1_IO0 / IO1 */
    g.Alternate = GPIO_AF9_QSPI;
    HAL_GPIO_Init(GPIOC, &g);
}

static void command(uint32_t instr, uint32_t admode, uint32_t addr, uint32_t dmode, uint32_t nbdata)
{
    QSPI_CommandTypeDef c = {0};
    c.InstructionMode = QSPI_INSTRUCTION_1_LINE;
    c.Instruction = instr;
    c.AddressMode = admode;
    c.AddressSize = QSPI_ADDRESS_24_BITS;
    c.Address = addr;
    c.AlternateByteMode = QSPI_ALTERNATE_BYTES_NONE;
    c.DataMode = dmode;
    c.NbData = nbdata;
    c.DummyCycles = 0;
    c.DdrMode = QSPI_DDR_MODE_DISABLE;
    c.SIOOMode = QSPI_SIOO_INST_EVERY_CMD;
    if (HAL_QSPI_Command(&hqspi, &c, 100) != HAL_OK)
        fail(20 + instr);
}

static void wait_ready(void)
{
    QSPI_CommandTypeDef c = {0};
    QSPI_AutoPollingTypeDef p = {0};
    c.InstructionMode = QSPI_INSTRUCTION_1_LINE;
    c.Instruction = 0x05;
    c.DataMode = QSPI_DATA_1_LINE;
    p.Match = 0x00;
    p.Mask = 0x01;
    p.MatchMode = QSPI_MATCH_MODE_AND;
    p.StatusBytesSize = 1;
    p.Interval = 0x10;
    p.AutomaticStop = QSPI_AUTOMATIC_STOP_ENABLE;
    if (HAL_QSPI_AutoPolling(&hqspi, &c, &p, 1000) != HAL_OK)
        fail(10);
}

static const char msg[] = "HAL QSPI on F446";

int main(void)
{
    uint8_t buf[32];

    HAL_Init();
    SystemClock_Config();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_USART2_CLK_ENABLE();
    GPIO_InitTypeDef g = {0};
    g.Pin = GPIO_PIN_2 | GPIO_PIN_3;
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_PULLUP;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF7_USART2;
    HAL_GPIO_Init(GPIOA, &g);
    huart2.Instance = USART2;
    huart2.Init.BaudRate = 115200;
    huart2.Init.WordLength = UART_WORDLENGTH_8B;
    huart2.Init.StopBits = UART_STOPBITS_1;
    huart2.Init.Parity = UART_PARITY_NONE;
    huart2.Init.Mode = UART_MODE_TX_RX;
    huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart2.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart2) != HAL_OK)
        fail(5);
    puts2("HAL F446 SYSCLK ");
    hex(HAL_RCC_GetSysClockFreq(), 8);
    puts2("\r\n");

    hqspi.Instance = QUADSPI;
    hqspi.Init.ClockPrescaler = 1;               /* 90 MHz */
    hqspi.Init.FifoThreshold = 4;
    hqspi.Init.SampleShifting = QSPI_SAMPLE_SHIFTING_HALFCYCLE;
    hqspi.Init.FlashSize = 23;                   /* 16 MB */
    hqspi.Init.ChipSelectHighTime = QSPI_CS_HIGH_TIME_2_CYCLE;
    hqspi.Init.ClockMode = QSPI_CLOCK_MODE_0;
    hqspi.Init.FlashID = QSPI_FLASH_ID_1;
    hqspi.Init.DualFlash = QSPI_DUALFLASH_DISABLE;
    if (HAL_QSPI_Init(&hqspi) != HAL_OK)
        fail(6);

    command(0x9F, QSPI_ADDRESS_NONE, 0, QSPI_DATA_1_LINE, 3);
    if (HAL_QSPI_Receive(&hqspi, buf, 100) != HAL_OK)
        fail(7);
    puts2("ID ");
    hex(((uint32_t)buf[0] << 16) | ((uint32_t)buf[1] << 8) | buf[2], 6);
    puts2("\r\n");

    command(0x06, QSPI_ADDRESS_NONE, 0, QSPI_DATA_NONE, 0);
    command(0x20, QSPI_ADDRESS_1_LINE, 0x2000, QSPI_DATA_NONE, 0);
    wait_ready();
    command(0x06, QSPI_ADDRESS_NONE, 0, QSPI_DATA_NONE, 0);
    command(0x02, QSPI_ADDRESS_1_LINE, 0x2000, QSPI_DATA_1_LINE, sizeof msg - 1);
    if (HAL_QSPI_Transmit(&hqspi, (uint8_t *)msg, 100) != HAL_OK)
        fail(8);
    wait_ready();

    QSPI_CommandTypeDef c = {0};
    c.InstructionMode = QSPI_INSTRUCTION_1_LINE;
    c.Instruction = 0xBB;                        /* Fast Read Dual I/O: address + M7-0 on 2 lines */
    c.AddressMode = QSPI_ADDRESS_2_LINES;
    c.AddressSize = QSPI_ADDRESS_24_BITS;
    c.Address = 0x2000;
    c.AlternateByteMode = QSPI_ALTERNATE_BYTES_2_LINES;
    c.AlternateBytesSize = QSPI_ALTERNATE_BYTES_8_BITS;
    c.AlternateBytes = 0;
    c.DataMode = QSPI_DATA_2_LINES;
    c.NbData = sizeof msg - 1;
    if (HAL_QSPI_Command(&hqspi, &c, 100) != HAL_OK)
        fail(11);
    clr(buf, sizeof buf);
    if (HAL_QSPI_Receive(&hqspi, buf, 100) != HAL_OK)
        fail(12);
    puts2("read: ");
    puts2((const char *)buf);
    puts2("\r\n");

    QSPI_MemoryMappedTypeDef mm = {0};
    c.Instruction = 0x3B;                        /* Fast Read Dual Output, 8 dummy clocks */
    c.AddressMode = QSPI_ADDRESS_1_LINE;
    c.AlternateByteMode = QSPI_ALTERNATE_BYTES_NONE;
    c.DummyCycles = 8;
    mm.TimeOutActivation = QSPI_TIMEOUT_COUNTER_DISABLE;
    if (HAL_QSPI_MemoryMapped(&hqspi, &c, &mm) != HAL_OK)
        fail(13);
    clr(buf, sizeof buf);
    for (unsigned i = 0; i < sizeof msg - 1; i++)
        buf[i] = ((volatile const uint8_t *)0x90000000u)[0x2000u + i];
    puts2("mm: ");
    puts2((const char *)buf);
    puts2("\r\ndone\r\n");
    for (;;) { }
}
