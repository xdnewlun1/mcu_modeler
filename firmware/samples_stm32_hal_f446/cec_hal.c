/* STM32CubeF4 HAL_CEC sample (STM32F446RE "B", logical address 0 = TV; board boards/stm32f446re_cec_hal.yaml with
 * ../samples_stm32f446/cec_ping.c on "A"):  HSI 16 MHz (reset clock), HAL_RCCEx_PeriphCLKConfig CEC clock = HSI/488
 * (DCKCFGR2.CECSEL = 0), HAL_CEC_Init (default SFT, standard tolerance, OAR = 0), CEC on PB6 (AF3 open drain),
 * HAL_CEC_IRQHandler + HAL_CEC_RxCpltCallback for reception and HAL_CEC_Transmit_IT for the <CEC Version> reply to a
 * <Get CEC Version> (9F).  Console on USART2 (HAL_UART, PA2).  Same transcript as ../samples_stm32f446/cec_tv.c. */
#include "stm32f4xx_hal.h"

static UART_HandleTypeDef huart2;
static CEC_HandleTypeDef hcec;
static uint8_t rxbuf[16];
static volatile uint32_t rx_len, rx_ready, tx_done, tx_err, errors;
volatile uint32_t hal_status;

void SysTick_Handler(void) { HAL_IncTick(); }
void CEC_IRQHandler(void) { HAL_CEC_IRQHandler(&hcec); }

static unsigned slen(const char *s) { unsigned n = 0; while (s[n]) n++; return n; }

static void puts2(const char *s)
{
    HAL_UART_Transmit(&huart2, (uint8_t *)s, (uint16_t)slen(s), 100);
}

static void hex2(unsigned v)
{
    char b[3] = {"0123456789ABCDEF"[(v >> 4) & 15u], "0123456789ABCDEF"[v & 15u], 0};
    puts2(b);
}

static void fail(uint32_t step)
{
    hal_status = step;
    for (;;) { }
}

void HAL_CEC_RxCpltCallback(CEC_HandleTypeDef *h, uint32_t size)
{
    (void)h;
    rx_len = size;
    rx_ready = 1;
}

void HAL_CEC_TxCpltCallback(CEC_HandleTypeDef *h)
{
    (void)h;
    tx_done = 1;
}

void HAL_CEC_ErrorCallback(CEC_HandleTypeDef *h)
{
    if (h->ErrorCode & (HAL_CEC_ERROR_TXACKE | HAL_CEC_ERROR_TXERR | HAL_CEC_ERROR_TXUDR))
        tx_err = 1;
    else
        errors++;
}

void HAL_UART_MspInit(UART_HandleTypeDef *h)
{
    (void)h;
    GPIO_InitTypeDef g = {0};
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_USART2_CLK_ENABLE();
    g.Pin = GPIO_PIN_2;
    g.Mode = GPIO_MODE_AF_PP;
    g.Speed = GPIO_SPEED_FREQ_HIGH;
    g.Alternate = GPIO_AF7_USART2;
    HAL_GPIO_Init(GPIOA, &g);
}

void HAL_CEC_MspInit(CEC_HandleTypeDef *h)
{
    (void)h;
    RCC_PeriphCLKInitTypeDef per = {0};
    GPIO_InitTypeDef g = {0};
    per.PeriphClockSelection = RCC_PERIPHCLK_CEC;
    per.CecClockSelection = RCC_CECCLKSOURCE_HSI;
    if (HAL_RCCEx_PeriphCLKConfig(&per) != HAL_OK)
        fail(10);
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_CEC_CLK_ENABLE();
    g.Pin = GPIO_PIN_6;
    g.Mode = GPIO_MODE_AF_OD;
    g.Pull = GPIO_NOPULL;
    g.Speed = GPIO_SPEED_FREQ_LOW;
    g.Alternate = GPIO_AF3_CEC;
    HAL_GPIO_Init(GPIOB, &g);
    HAL_NVIC_SetPriority(CEC_IRQn, 1, 0);
    HAL_NVIC_EnableIRQ(CEC_IRQn);
}

/* The shared HAL startup (../samples_stm32_hal/startup.c) has the 82 STM32F405 vectors: the F446 CEC vector (93) lies
 * beyond it, so the table is copied to SRAM, extended to the 97 F446 vectors and VTOR is pointed at it. */
static uint32_t vtab[16 + 97] __attribute__((aligned(512)));

static void vectors_f446(void)
{
    const uint32_t *flash = (const uint32_t *)SCB->VTOR;
    for (unsigned i = 0; i < 16 + 82; i++)
        vtab[i] = flash[i];
    for (unsigned i = 16 + 82; i < 16 + 97; i++)
        vtab[i] = flash[16 + 1];                 /* unused: same handler as vector 1 (Default_Handler) */
    vtab[16 + CEC_IRQn] = (uint32_t)CEC_IRQHandler;
    SCB->VTOR = (uint32_t)vtab;
    __DSB();
}

int main(void)
{
    vectors_f446();
    HAL_Init();
    huart2.Instance = USART2;
    huart2.Init.BaudRate = 115200;
    huart2.Init.WordLength = UART_WORDLENGTH_8B;
    huart2.Init.StopBits = UART_STOPBITS_1;
    huart2.Init.Parity = UART_PARITY_NONE;
    huart2.Init.Mode = UART_MODE_TX;
    huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart2.Init.OverSampling = UART_OVERSAMPLING_16;
    if (HAL_UART_Init(&huart2) != HAL_OK)
        fail(1);

    hcec.Instance = CEC;
    hcec.Init.SignalFreeTime = CEC_DEFAULT_SFT;
    hcec.Init.Tolerance = CEC_STANDARD_TOLERANCE;
    hcec.Init.BRERxStop = CEC_RX_STOP_ON_BRE;
    hcec.Init.BREErrorBitGen = CEC_BRE_ERRORBIT_NO_GENERATION;
    hcec.Init.LBPEErrorBitGen = CEC_LBPE_ERRORBIT_NO_GENERATION;
    hcec.Init.BroadcastMsgNoErrorBitGen = CEC_BROADCASTERROR_ERRORBIT_GENERATION;
    hcec.Init.SignalFreeTimeOption = CEC_SFT_START_ON_TXSOM;
    hcec.Init.ListenMode = CEC_REDUCED_LISTENING_MODE;
    hcec.Init.OwnAddress = CEC_OWN_ADDRESS_0;
    hcec.Init.RxBuffer = rxbuf;
    if (HAL_CEC_Init(&hcec) != HAL_OK)
        fail(2);
    puts2("CEC B addr 0\r\n");

    for (unsigned msgs = 0; msgs < 3;) {        /* ping 0, 40 9F, broadcast 4F 82 10 00 */
        while (!rx_ready)
            __WFI();
        rx_ready = 0;
        msgs++;
        /* RxFrameSize counts the header too (RXBR of every block) */
        uint8_t m[16];
        unsigned n = rx_len;
        for (unsigned i = 0; i < n && i < sizeof m; i++)
            m[i] = rxbuf[i];
        puts2("rx");
        for (unsigned i = 0; i < n; i++) {
            puts2(" ");
            hex2(m[i]);
        }
        puts2("\r\n");
        if (n >= 2 && m[1] == 0x9F) {
            static uint8_t ver[] = {0x9E, 0x05};
            tx_done = tx_err = 0;
            if (HAL_CEC_Transmit_IT(&hcec, 0x0, 0x4, ver, 2) != HAL_OK)
                fail(3);
            while (!tx_done && !tx_err)
                __WFI();
            puts2(tx_done ? "tx 04 9E 05: ack\r\n" : "tx 04 9E 05: failed\r\n");
        }
    }
    puts2("errors ");
    hex2(errors);
    puts2("\r\ndone\r\n");
    for (;;)
        __WFI();
}
