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
    (r"I2S\d+_(CK|WS|SD|MCK)", IN),          # stm32.spi (I2S mode) sets the live direction (master/slave, TX/RX)
    (r"I2S\d+ext_SD", IN),                   # stm32.i2s_ext: output while a transmitting extension runs
    (r"I2S_CKIN", IN),
    (r"I2C\d+_(SDA|SCL)", OD),
    (r"I2C\d+_SMBA", IN),
    (r"CAN\d+_TX", OUT_H),
    (r"CAN\d+_RX", IN),
    (r"TIM\d+_CH\d+N?(_ETR)?", OUT_L),
    (r"TIM\d+_(ETR|BKIN)", IN),
    (r"SDIO_CK", OUT_L),                     # card clock (not toggled per edge: stm32.sdio summarises it)
    (r"SDIO_CMD", OUT_H),                    # idles high (pulled up)
    (r"SDIO_D\d", IN),
    (r"DCMI_(D\d+|HSYNC|VSYNC|PIXCLK)", IN),   # camera interface: all inputs (stm32.dcmi)
    (r"LCD_(CLK|HSYNC|VSYNC|DE|[RGB]\d)", OUT_L),   # LCD-TFT outputs (stm32.ltdc sets the live idle levels)
    (r"ETH_MDC", OUT_L),                     # MDIO clock (stm32.eth summarises it as a `wave`)
    (r"ETH_MDIO", OD),                       # management data: pulled up between transactions
    (r"ETH_(MII|RMII)_(TX_EN|TXD\d)", OUT_L),  # transmit data (not toggled per bit)
    (r"ETH_(MII|RMII)_(TX_CLK|RX_CLK|REF_CLK|RXD\d|RX_DV|CRS_DV|CRS|COL|RX_ER)", IN),   # clocks / data from the PHY
    (r"ETH_PPS_OUT", OUT_L),
    (r"OTG_(FS|HS)_(DP|DM)", IN),             # USB data lines: driven by the PHY on the bus (stm32.otg_fs/_hs)
    (r"OTG_(FS|HS)_(ID|VBUS)", IN),
    (r"OTG_(FS|HS)_SOF", OUT_L),              # SOF pulse (GCCFG.SOFOUTEN)
    (r"OTG_HS_ULPI_\w+", IN),                 # external ULPI PHY: not modelled
    (r"MCO\d*|RTC_OUT", OUT_L),
    (r"TRACE(CLK|D\d+)", OUT_L),
    (r"JTMS-SWDIO|JTCK-SWCLK|JTDI|NJTRST", IN),
    (r"JTDO|TRACESWO", OUT_H),
    (r"(ADC\d*_IN\d+|DAC_OUT\d+)", ANALOG),
    # STM32F446 (stm32.quadspi drives CLK/NCS/IO live while a command runs; the others are register storage only)
    (r"QUADSPI_CLK", OUT_L),
    (r"QUADSPI_BK\d_NCS", OUT_H),           # chip select, active low
    (r"QUADSPI_BK\d_IO\d", IN),             # bidirectional data; released (pulled up on boards) between commands
    (r"FMPI2C\d+_(SDA|SCL)", OD),
    (r"FMPI2C\d+_SMBA", IN),
    (r"CEC", OD),                            # HDMI-CEC single-wire bus (open-drain)
    (r"SPDIFRX_IN\d", IN),
])
