"""STM32F1 peripheral signal roles for pins owned by a peripheral (see chipem/periph/signals.py).

On the F1 there is no per-pin AF number: a pin in "alternate function output" mode (GPIOx_CRL/CRH CNF1 = 1) is driven
by the peripheral output that the AFIO remap routes to it, and peripheral inputs read any pin in input mode
(stm32f1.gpio decides the owner).  The roles below are the static fallbacks, as in stm32/signals.py; stm32.spi
(`signal_state`) and stm32.tim (`signal_driven`, drive_signal) override them live.  Names are the data sheet's
(DS15060 Table 5): CAN_RX/CAN_TX (no instance number), USBDM/USBDP, MCO, TAMPER-RTC, JTMS/SWDIO ...
"""
from ..signals import ANALOG, IN, OD, OUT_H, OUT_L, SignalTable

TABLE = SignalTable([
    (r"USART\d+_TX", OUT_H),
    (r"USART\d+_RX", IN),
    (r"USART\d+_CTS", IN),
    (r"USART\d+_RTS", OUT_H),               # deasserted
    (r"USART\d+_CK", OUT_L),
    (r"SPI\d+_(SCK|MOSI)", OUT_L),
    (r"SPI\d+_MISO", IN),
    (r"SPI\d+_NSS", OUT_H),
    (r"I2C\d+_(SDA|SCL)", OD),
    (r"I2C\d+_SMBA", OD),
    (r"CAN_TX", OUT_H),
    (r"CAN_RX", IN),
    (r"TIM\d+_CH\d+N?(_ETR)?", OUT_L),
    (r"TIM\d+_(ETR|BKIN)", IN),
    (r"USBD[MP]", IN),                      # driven by the USB transceiver on the bus (stm32f1.usb)
    (r"MCO", OUT_L),
    (r"TAMPER-RTC", IN),
    (r"WKUP", IN),
    (r"JTMS|SWDIO|JTCK|SWCLK|JTDI|NJTRST", IN),
    (r"JTDO|TRACESWO", OUT_H),
    (r"ADC\d*_IN\d+", ANALOG),
    (r"OSC(32)?_(IN|OUT)", ANALOG),
])
