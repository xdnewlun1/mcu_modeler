"""TI C2000 (F2833x/F28232) peripheral signal roles for pins muxed to MUX1..MUX3 (see chipem/periph/signals.py).

TI names carry the instance letter without a separator (SCITXDA, SPISIMOA, CANRXB, EPWM1A). SPICLK idles low:
the clock polarity is an SPICCR bit the GPIO model does not see. I2C pins (SDAA/SCLA) are open-drain and follow
GPxPUD.
"""
from ..signals import ANALOG, IN, OD, OUT_H, OUT_L, SignalTable

TABLE = SignalTable([
    (r"SCITXD[A-C]", OUT_H),
    (r"SCIRXD[A-C]", IN),
    (r"SPI(CLK|SIMO)[A-D]", OUT_L),
    (r"SPISOMI[A-D]", IN),
    (r"SPISTE[A-D]", OUT_H),
    (r"SDAA|SCLA", OD),
    (r"CANTX[AB]", OUT_H),
    (r"CANRX[AB]", IN),
    (r"EPWM\d+[AB]|EPWMSYNCO", OUT_L),
    (r"ADCSOC[AB]O", OUT_H),                 # active-low start-of-conversion pulses
    (r"EPWMSYNCI|TZ\d+|EQEP\d+[ABIS]", IN),
    (r"XCLKOUT", OUT_L),
    (r"ADCIN[AB]\d+", ANALOG),
])
