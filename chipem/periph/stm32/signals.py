"""STM32F4 peripheral signal roles for pins in alternate-function mode (see chipem/periph/signals.py).

Analog mode (MODER=11) stays the GPIO model's business. SPI clocks idle low: CPOL is an SPI_CR1 bit the GPIO
model does not see. These are static fallbacks: stm32.spi (`signal_state`) overrides the SPI roles live (master/slave,
SPE, CPOL) and toggles the pins; stm32.dac claims DAC_OUTx on its analog-mode pin while the channel is enabled. Open-drain I2C pins follow PUPDR (or an external pull on a net).
"""
from ..signals import ANALOG, IN, OD, OUT_H, OUT_L, SignalTable

TABLE = SignalTable([
    (r"U(S)?ART\d+_TX", OUT_H),
    (r"U(S)?ART\d+_RX", IN),
    (r"U(S)?ART\d+_CTS", IN),
    (r"U(S)?ART\d+_RTS", OUT_H),               # deasserted
    (r"USART\d+_CK", OUT_L),
    (r"SPI\d+_(SCK|MOSI)", OUT_L),
    (r"SPI\d+_MISO", IN),
    (r"SPI\d+_NSS", OUT_H),
    (r"I2C\d+_(SDA|SCL)", OD),
    (r"I2C\d+_SMBA", IN),
    (r"CAN\d+_TX", OUT_H),
    (r"CAN\d+_RX", IN),
    (r"TIM\d+_CH\d+N?(_ETR)?", OUT_L),
    (r"TIM\d+_(ETR|BKIN)", IN),
    (r"SDIO_CK", OUT_L),                     # card clock (not toggled per edge: stm32.sdio summarises it)
    (r"SDIO_CMD", OUT_H),                    # idles high (pulled up)
    (r"SDIO_D\d", IN),
    (r"MCO\d*|RTC_OUT", OUT_L),
    (r"TRACE(CLK|D\d+)", OUT_L),
    (r"JTMS-SWDIO|JTCK-SWCLK|JTDI|NJTRST", IN),
    (r"JTDO|TRACESWO", OUT_H),
    (r"(ADC\d*_IN\d+|DAC_OUT\d+)", ANALOG),
])
