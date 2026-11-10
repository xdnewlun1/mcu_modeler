/* uart_dma_hal: USART1 transmit and receive by DMA with ST's STM32CubeF4 HAL (HAL_UART_Transmit_DMA /
 * HAL_UART_Receive_DMA), on an STM32F405ZG.
 *
 * Clock:  HSE 8 MHz -> PLL -> 168 MHz (APB2 = 84 MHz), as in main.c (uart_chat_hal).
 * UART:   USART1 115200 8N1 on PA9 (TX, AF7) / PA10 (RX, AF7).
 * DMA:    DMA2 Stream7 Channel 4 = USART1_TX, DMA2 Stream2 Channel 4 = USART1_RX (RM0090 Table 43), normal mode,
 *         byte to byte, memory increment; HAL_DMA_IRQHandler in DMA2_Stream7/DMA2_Stream2_IRQHandler.
 * Program: arm HAL_UART_Receive_DMA(8 bytes), send "HAL DMA\r\n" with HAL_UART_Transmit_DMA; then forever: when 8 bytes
 *         have arrived (HAL_UART_RxCpltCallback), re-arm the receive and echo them with "\r\n" by DMA once the previous
 *         transmission is complete (HAL_UART_TxCpltCallback, called from the USART TC interrupt after the DMA's).
 */
#include "stm32f4xx_hal.h"

UART_HandleTypeDef huart1;
DMA_HandleTypeDef hdma_usart1_tx;
DMA_HandleTypeDef hdma_usart1_rx;

static const char banner[] = "HAL DMA\r\n";
uint8_t rx_buf[8];
uint8_t tx_buf[10];
volatile uint32_t rx_done;          /* HAL_UART_RxCpltCallback count */
volatile uint32_t tx_done;          /* HAL_UART_TxCpltCallback count */
volatile uint32_t uart_errors;
static volatile uint32_t rx_pending;
static volatile uint32_t tx_busy;

static void SystemClock_Config(void);
static void DMA_Init(void);
static void USART1_Init(void);
static void Error_Handler(void);

int main(void)
{
    HAL_Init();
    SystemClock_Config();
    DMA_Init();
    USART1_Init();

    if (HAL_UART_Receive_DMA(&huart1, rx_buf, sizeof rx_buf) != HAL_OK)
        Error_Handler();
    tx_busy = 1;
    if (HAL_UART_Transmit_DMA(&huart1, (uint8_t *)banner, sizeof banner - 1) != HAL_OK)
        Error_Handler();
    for (;;) {
        if (rx_pending && !tx_busy) {
            rx_pending = 0;
            for (uint32_t i = 0; i < 8; i++)
                tx_buf[i] = rx_buf[i];
            tx_buf[8] = '\r';
            tx_buf[9] = '\n';
            if (HAL_UART_Receive_DMA(&huart1, rx_buf, sizeof rx_buf) != HAL_OK)
                Error_Handler();
            tx_busy = 1;
            if (HAL_UART_Transmit_DMA(&huart1, tx_buf, sizeof tx_buf) != HAL_OK)
                Error_Handler();
        }
        __WFI();
    }
}

/* ------------------------------------------------------------------ HAL callbacks */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == USART1) {
        rx_done++;
        rx_pending = 1;
    }
}

void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == USART1) {
        tx_done++;
        tx_busy = 0;
    }
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == USART1)
        uart_errors++;
}

void HAL_UART_MspInit(UART_HandleTypeDef *huart)
{
    if (huart->Instance != USART1)
        return;
    GPIO_InitTypeDef g = {0};
    __HAL_RCC_USART1_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    g.Pin = GPIO_PIN_9 | GPIO_PIN_10;
    g.Mode = GPIO_MODE_AF_PP;
    g.Pull = GPIO_PULLUP;
    g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF7_USART1;
    HAL_GPIO_Init(GPIOA, &g);

    hdma_usart1_tx.Instance = DMA2_Stream7;
    hdma_usart1_tx.Init.Channel = DMA_CHANNEL_4;
    hdma_usart1_tx.Init.Direction = DMA_MEMORY_TO_PERIPH;
    hdma_usart1_tx.Init.PeriphInc = DMA_PINC_DISABLE;
    hdma_usart1_tx.Init.MemInc = DMA_MINC_ENABLE;
    hdma_usart1_tx.Init.PeriphDataAlignment = DMA_PDATAALIGN_BYTE;
    hdma_usart1_tx.Init.MemDataAlignment = DMA_MDATAALIGN_BYTE;
    hdma_usart1_tx.Init.Mode = DMA_NORMAL;
    hdma_usart1_tx.Init.Priority = DMA_PRIORITY_LOW;
    hdma_usart1_tx.Init.FIFOMode = DMA_FIFOMODE_DISABLE;
    if (HAL_DMA_Init(&hdma_usart1_tx) != HAL_OK)
        Error_Handler();
    __HAL_LINKDMA(huart, hdmatx, hdma_usart1_tx);

    hdma_usart1_rx.Instance = DMA2_Stream2;
    hdma_usart1_rx.Init.Channel = DMA_CHANNEL_4;
    hdma_usart1_rx.Init.Direction = DMA_PERIPH_TO_MEMORY;
    hdma_usart1_rx.Init.PeriphInc = DMA_PINC_DISABLE;
    hdma_usart1_rx.Init.MemInc = DMA_MINC_ENABLE;
    hdma_usart1_rx.Init.PeriphDataAlignment = DMA_PDATAALIGN_BYTE;
    hdma_usart1_rx.Init.MemDataAlignment = DMA_MDATAALIGN_BYTE;
    hdma_usart1_rx.Init.Mode = DMA_NORMAL;
    hdma_usart1_rx.Init.Priority = DMA_PRIORITY_HIGH;
    hdma_usart1_rx.Init.FIFOMode = DMA_FIFOMODE_DISABLE;
    if (HAL_DMA_Init(&hdma_usart1_rx) != HAL_OK)
        Error_Handler();
    __HAL_LINKDMA(huart, hdmarx, hdma_usart1_rx);

    HAL_NVIC_SetPriority(USART1_IRQn, 5, 0);
    HAL_NVIC_EnableIRQ(USART1_IRQn);
}

/* ------------------------------------------------------------------ init */
static void DMA_Init(void)
{
    __HAL_RCC_DMA2_CLK_ENABLE();
    HAL_NVIC_SetPriority(DMA2_Stream2_IRQn, 4, 0);
    HAL_NVIC_EnableIRQ(DMA2_Stream2_IRQn);
    HAL_NVIC_SetPriority(DMA2_Stream7_IRQn, 4, 0);
    HAL_NVIC_EnableIRQ(DMA2_Stream7_IRQn);
}

static void USART1_Init(void)
{
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
    __disable_irq();
    for (;;) { }
}

/* ------------------------------------------------------------------ interrupt handlers */
void SysTick_Handler(void)
{
    HAL_IncTick();
}

void USART1_IRQHandler(void)
{
    HAL_UART_IRQHandler(&huart1);
}

void DMA2_Stream2_IRQHandler(void)
{
    HAL_DMA_IRQHandler(&hdma_usart1_rx);
}

void DMA2_Stream7_IRQHandler(void)
{
    HAL_DMA_IRQHandler(&hdma_usart1_tx);
}
