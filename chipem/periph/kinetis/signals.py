"""Kinetis (K22) peripheral signal roles for pins muxed to ALT2..ALT7 (see chipem/periph/signals.py).

ALT0 stays the PORT model's business (analog when the pin has an ALT0 function). SPI clocks idle low in the table;
the DSPI drives SCK at CPOL and PCSn at PCSIS once it is a master (drive_signal), and its, the SAI's and the CMT's
mode-dependent directions come from PORT `signal_dir_hooks`.
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
    (r"SDHC\d_DCLK", OUT_L),                # card clock (not toggled per edge: kinetis.sdhc summarises it)
    (r"SDHC\d_CMD", OUT_H),                 # idles high (pulled up)
    (r"SDHC\d_D\d", IN),
    (r"(RTC_)?CLKOUT", OUT_L),
    (r"TRACE_(CLKOUT|D\d+)", OUT_L),
    (r"JTAG_(TCLK|TMS|TDI|TRST_b)|SWD_(CLK|DIO)", IN),
    (r"JTAG_TDO|TRACE_SWO", OUT_H),
    (r"NMI_b|LPTMR\d+_ALT\d+|PDB\d+_EXTRG|EWM_IN|USB_CLKIN", IN),
    (r"EWM_OUT_b", OUT_H),
    (r"CMP\d+_OUT|CMT_IRO", OUT_L),
    (r"I2S\d+_TXD\d+", OUT_L),                # tristated (input) while no word is sent: kinetis.sai hook
    (r"I2S\d+_(RXD\d+|(TX|RX)_(BCLK|FS)|MCLK)", IN),   # outputs while generated internally: kinetis.sai hook
    (r"(ADC|CMP|DAC)\d+_\w+|VREF_OUT", ANALOG),
])
