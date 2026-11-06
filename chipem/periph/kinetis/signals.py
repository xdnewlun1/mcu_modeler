"""Kinetis (K22) peripheral signal roles for pins muxed to ALT2..ALT7 (see chipem/periph/signals.py).

ALT0 stays the PORT model's business (analog when the pin has an ALT0 function). SPI clocks idle low: CPOL is a
DSPI CTAR field the mux model does not see.
"""
from ..signals import ANALOG, IN, OD, OUT_H, OUT_L, SignalTable

TABLE = SignalTable([
    (r"(LP)?UART\d+_TX", OUT_H),
    (r"(LP)?UART\d+_RX", IN),
    (r"(LP)?UART\d+_CTS_b", IN),
    (r"(LP)?UART\d+_RTS_b", OUT_H),            # deasserted
    (r"SPI\d+_(SCK|SOUT)", OUT_L),
    (r"SPI\d+_SIN", IN),
    (r"SPI\d+_PCS\d+", OUT_H),
    (r"I2C\d+_(SDA|SCL)", OD),
    (r"CAN\d+_TX", OUT_H),
    (r"CAN\d+_RX", IN),
    (r"FTM\d+_CH\d+", OUT_L),
    (r"FTM\d+_(FLT\d+|QD_PH[AB])|FTM_CLKIN\d+", IN),
    (r"(RTC_)?CLKOUT", OUT_L),
    (r"TRACE_(CLKOUT|D\d+)", OUT_L),
    (r"JTAG_(TCLK|TMS|TDI|TRST_b)|SWD_(CLK|DIO)", IN),
    (r"JTAG_TDO|TRACE_SWO", OUT_H),
    (r"NMI_b|LPTMR\d+_ALT\d+|PDB\d+_EXTRG|EWM_IN|USB_CLKIN", IN),
    (r"EWM_OUT_b", OUT_H),
    (r"CMP\d+_OUT|CMT_IRO", OUT_L),
    (r"(ADC|CMP|DAC)\d+_\w+|VREF_OUT", ANALOG),
])
