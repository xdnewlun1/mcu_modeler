"""TI C2000 (F2833x/F28232) peripheral signal roles for pins muxed to MUX1..MUX3 (see chipem/periph/signals.py).

TI names carry the instance letter without a separator (SCITXDA, SPISIMOA, CANRXB, EPWM1A). The SPI rows are the
master roles at reset polarity; ti_c2000.spi reports the live role (master/slave, TALK, CLKPOLARITY) through
`signal_state`. I2C pins (SDAA/SCLA) are open-drain and follow
GPxPUD. McBSP clocks / frame syncs are inputs or outputs per PCR and MDX is high impedance while the transmitter is
in reset: the McBSP model reports the live role (`signal_state`), the table only gives the fallback (input); MDXx is
not listed (role "none" without a McBSP block).
"""
from ..signals import ANALOG, IN, OD, OUT_H, OUT_L, SignalTable

TABLE = SignalTable([
    (r"SCITXD[A-C]", OUT_H),
    (r"SCIRXD[A-C]", IN),
    (r"SPI(CLK|SIMO)[A-D]", OUT_L),          # master roles: static fallback; ti_c2000.spi sets the live role
    (r"SPISOMI[A-D]", IN),
    (r"SPISTE[A-D]", OUT_H),
    (r"SDAA|SCLA", OD),
    (r"CANTX[AB]", OUT_H),
    (r"CANRX[AB]", IN),
    (r"EPWM\d+[AB]|EPWMSYNCO", OUT_L),
    (r"ADCSOC[AB]O", OUT_H),                 # active-low start-of-conversion pulses
    (r"EPWMSYNCI|TZ\d+|EQEP\d+[ABIS]", IN),
    (r"MDR[AB]|MCLK[RX][AB]|MFS[RX][AB]", IN),  # McBSP: static fallback; ti_c2000.mcbsp sets the live role
    (r"XCLKOUT", OUT_L),
    (r"ADCIN[AB]\d+", ANALOG),
])
