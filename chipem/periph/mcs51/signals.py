"""MCS-51 (P89CV51) peripheral signal roles (see chipem/periph/signals.py).

On an 80C51 the alternate functions have no mux register: an alternate OUTPUT reaches the pin only while the port
latch bit is 1 (pin = latch AND alternate output), and alternate INPUTS always see the pin.  mcs51.port gives a pin
to a signal when the owning peripheral claims it (UART: TXD always, RXD while REN = 1; SPI while SPEN = 1; PCA CEXn
in PWM / high-speed-output modes; Timer 2 T2 in clock-out mode) and the latch bit is 1.  Roles below are the idle
roles; SPI and PCA report live levels through PinBoard.drive_signal.
"""
from ..signals import IN, OUT_H, OUT_L, SignalTable

TABLE = SignalTable([
    (r"UART_TXD", OUT_H),
    (r"UART_RXD", IN),
    (r"SPI_(SCK|MOSI)", OUT_L),              # master roles; mcs51.spi claims the live role (slave: inputs)
    (r"SPI_MISO", IN),
    (r"SPI_SS", IN),
    (r"CEX[0-4]", OUT_L),                    # PCA PWM / high-speed output (capture mode: input claim)
    (r"T2", OUT_L),                          # Timer 2 clock-out
    (r"WR|RD|ALE|PSEN", OUT_H),              # external bus strobes (active low; ALE pulses)
    (r"A(8|9|1[0-5])|AD[0-7]", OUT_L),       # external bus address / data
    (r"INT[01]|T[01]|T2EX|ECI", IN),
])
