"""STM32F1 peripheral signal roles for pins owned by a peripheral (see chipem/periph/signals.py).

On the F1 there is no per-pin AF number: a pin in "alternate function output" mode (GPIOx_CRL/CRH CNF1 = 1) is driven
by the peripheral output that the AFIO remap routes to it, and peripheral inputs read any pin in input mode
(stm32f1.gpio decides the owner).  The roles below are the static fallbacks, as in stm32/signals.py; stm32.spi
(`signal_state`) and stm32.tim (`signal_driven`, drive_signal) override them live.  Names are the data sheet's
(DS15060 Table 5): CAN_RX/CAN_TX (no instance number), USBDM/USBDP, MCO, TAMPER-RTC, JTMS/SWDIO ...
Debug pins (RM0008 31.4.3/31.4.4): JTMS/JTDI/NJTRST/JTCK are inputs (with the internal pulls applied by stm32f1.gpio),
JTDO is tri-stated without a debugger; TRACESWO / TRACECK / TRACEDx are outputs once DBGMCU_CR.TRACE_IOEN assigns them.
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
    (r"CAN\d?_TX", OUT_H),                  # CAN_TX, or CAN1_TX / CAN2_TX on the connectivity line
    (r"CAN\d?_RX", IN),
    (r"UART\d+_TX", OUT_H),                 # UART4/UART5 (high density, connectivity line)
    (r"UART\d+_RX", IN),
    (r"TIM\d+_CH\d+N?(_ETR)?", OUT_L),
    (r"TIM\d+_(ETR|BKIN)", IN),
    (r"USBD[MP]", IN),                      # driven by the USB transceiver on the bus (stm32f1.usb)
    # connectivity line (RM0008 9.1.11 Tables 26, 30, 32)
    (r"OTG_FS_D[MP]|OTG_FS_VBUS|OTG_FS_ID", IN),   # DM/DP: the OTG PHY (stm32f1.otg_fs); VBUS / ID inputs
    (r"OTG_FS_SOF", OUT_L),
    (r"ETH_MDC", OUT_L),
    (r"ETH_MDIO", OD),
    (r"ETH_(MII|RMII)_(TX_EN|TXD\d)", OUT_L),
    (r"ETH_PPS_OUT", OUT_L),
    (r"ETH_(MII|RMII)_\w+", IN),          # TX_CLK, RX_CLK, REF_CLK, RX_DV, CRS_DV, RXD, RX_ER, CRS, COL
    (r"I2S\d_(CK|WS|MCK)", OUT_L),         # master (stm32.spi signal_state gives the live direction)
    (r"I2S\d_SD", OUT_L),
    # high density (RM0008 22.4.1 SDIO pins, 21.3.1 FSMC external device signals): bidirectional data / command lines
    # are configured as AF push-pull outputs on the F1 (9.1.11 Tables 22/23), so they are listed as outputs here
    (r"SDIO_CK", OUT_L),                    # card clock (stm32.sdio summarises it, no per-edge toggling)
    (r"SDIO_CMD|SDIO_D\d", OUT_H),          # idle high (card pull-ups); the bit stream is not put on the pins
    (r"FSMC_(A\d+|CLK|ALE|CLE)", OUT_L),
    (r"FSMC_D\d+", OUT_L),
    (r"FSMC_(NOE|NWE|NE\d|NBL\d|NADV|NCE\d(_\d)?|NIORD|NIOWR|NREG)", OUT_H),   # active-low strobes / selects
    (r"FSMC_(NWAIT|INT\d|INTR|CD|NIOS16)", IN),
    (r"DAC_OUT\d", ANALOG),
    (r"MCO", OUT_L),
    (r"TAMPER-RTC", IN),
    (r"WKUP", IN),
    (r"JTMS|SWDIO|JTCK|SWCLK|JTDI|NJTRST", IN),
    (r"JTDO", IN),                          # tri-stated unless a JTAG debugger shifts data (RM0008 31.4.4)
    (r"TRACESWO", OUT_H),
    (r"TRACECK|TRACED\d", OUT_L),
    (r"ADC\d*_IN\d+", ANALOG),
    (r"OSC(32)?_(IN|OUT)", ANALOG),
])
