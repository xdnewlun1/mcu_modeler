"""STM32F4 SPI (chipem/periph/stm32/spi.py): master frame timing (BR, APB prescaler), double buffering, pin
waveforms (CPOL/CPHA, LSBFIRST, DFF), MISO from the pin, OVR/MODF/CRC, NSS output, slave mode on pin edges, the
SPI NOR flash responder, IRQ and DMA requests.
16 MHz HSI, APB1/APB2 /1 unless set: SCK half period = 2^BR core cycles; every time is an exact cycle count."""
import pytest

from chipem.devices.spi_flash import SpiNorFlash
from chipem.periph.stm32.spi import crc_update
from test_stm32 import GPIOA, RCC, desc, rd, spin_chip, wr  # noqa: F401  (desc: fixture)
from test_stm32_dma import CR, M0AR, MINC, M2P, NDTR, P2M, PAR, chsel, sreg

SPI1, SPI2, DMA2 = 0x40013000, 0x40003800, 0x40026400
CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR = 0, 4, 8, 0xC, 0x10, 0x14, 0x18, 0x1C
CPHA, CPOL, MSTR, SPE, LSBFIRST, SSI, SSM, RXONLY, DFF, CRCNEXT, CRCEN = (1, 2, 4, 0x40, 0x80, 0x100, 0x200, 0x400,
                                                                        0x800, 0x1000, 0x2000)
RXDMAEN, TXDMAEN, SSOE, ERRIE, RXNEIE, TXEIE = 1, 2, 4, 0x20, 0x40, 0x80
RXNE, TXE, CRCERR, MODF, OVR, BSY = 1, 2, 0x10, 0x20, 0x40, 0x80
SPI1_IRQ = 35


def br(n):
    return n << 3


def _chip(desc, pins=(5, 6, 7), nss=False):
    chip = spin_chip(desc, clock_hz=16e6)
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 1 | 1 << 22)        # GPIOA, DMA2
    wr(chip, RCC + 0x44, rd(chip, RCC + 0x44) | 1 << 12)            # SPI1EN
    moder, afrl = rd(chip, GPIOA), rd(chip, GPIOA + 0x20)
    for p in pins + ((4,) if nss else ()):
        moder = (moder & ~(3 << 2 * p)) | 2 << 2 * p
        afrl = (afrl & ~(0xF << 4 * p)) | 5 << 4 * p
    wr(chip, GPIOA + 0x20, afrl)
    wr(chip, GPIOA, moder)
    return chip


def _probe(chip, times, addr):
    out = []
    for t in times:
        chip.sched.at_cycles(t, lambda: out.append(rd(chip, addr)))
    return out


def _trace(chip, *pins):
    ev = []
    chip.pins.subscribe(lambda e: ev.append((chip.now_cycles(), e.pin, e.new)) if e.field == "level"
                        and e.pin in pins else None)
    return ev


def _irq(chip):
    return (16 + SPI1_IRQ) in chip.arch.asserted


MASTER = MSTR | SSM | SSI | SPE


# ------------------------------------------------------------------------------------------------ timing
@pytest.mark.parametrize("b,ppre", [(0, 1), (1, 1), (3, 1), (7, 1), (2, 2), (1, 4)])
def test_frame_time_is_8_bit_times(desc, b, ppre):
    chip = _chip(desc)
    if ppre != 1:
        wr(chip, RCC + 0x08, rd(chip, RCC + 0x08) | {2: 4, 4: 5}[ppre] << 13)    # PPRE2
    wr(chip, SPI1 + CR1, MASTER | br(b))
    half = (1 << b) * ppre
    assert chip.peripherals["SPI1"].baud() == pytest.approx(16e6 / (2 * half))
    t0 = chip.now_cycles()
    wr(chip, SPI1 + DR, 0xA5)
    assert rd(chip, SPI1 + SR) == TXE                        # buffer moved to the shifter at once; BSY follows
    end = t0 + 16 * half                                     # 2 PCLK later (RM0090 28.3.9 note)
    sr = _probe(chip, [t0 + 2 * ppre - 1, t0 + 2 * ppre + 1, end - 1, end + 1], SPI1 + SR)
    chip.run_cycles(16 * half + 4)
    assert sr == [TXE, TXE | BSY, TXE | BSY, TXE | RXNE]


def test_spi2_on_apb1(desc):
    chip = _chip(desc)
    wr(chip, RCC + 0x40, rd(chip, RCC + 0x40) | 1 << 14)            # SPI2EN
    wr(chip, RCC + 0x08, rd(chip, RCC + 0x08) | 5 << 10)            # PPRE1 /4
    wr(chip, SPI2 + CR1, MASTER | br(1))
    assert chip.peripherals["SPI2"].half_cycles() == 2 * 4
    t0 = chip.now_cycles()
    wr(chip, SPI2 + DR, 1)
    sr = _probe(chip, [t0 + 16 * 8 - 1, t0 + 16 * 8 + 1], SPI2 + SR)
    chip.run_cycles(200)
    assert [s & RXNE for s in sr] == [0, RXNE]


def test_double_buffer_back_to_back_and_overrun(desc):
    chip = _chip(desc)
    chip.pins.set_external("PA6", 1)
    wr(chip, SPI1 + CR1, MASTER | br(0))                     # 2 cycles per bit, 16 per frame
    t0 = chip.now_cycles()
    wr(chip, SPI1 + DR, 0x11)
    wr(chip, SPI1 + DR, 0x22)                                # waits in the TX buffer
    assert rd(chip, SPI1 + SR) == 0                          # TXE = 0: buffer full; BSY sets 2 PCLK later
    sr = _probe(chip, [t0 + 15, t0 + 17, t0 + 31, t0 + 33], SPI1 + SR)
    chip.run_cycles(40)
    assert sr == [BSY, TXE | BSY | RXNE, TXE | BSY | RXNE, TXE | RXNE | OVR]
    spi = chip.peripherals["SPI1"]
    assert [f[0] for f in spi.frames] == [0x11, 0x22] and bytes(spi.tx_bytes) == b"\x11\x22"
    # OVR: the second word was lost; clear with DR read then SR read
    assert rd(chip, SPI1 + DR) == 0xFF
    assert rd(chip, SPI1 + SR) & OVR
    assert rd(chip, SPI1 + SR) == TXE


# ------------------------------------------------------------------------------------------------ pins
def _bits_at(ev, sck_edges, pin):
    """Level of `pin` at each SCK edge time (last change at or before the edge)."""
    out = []
    for t in sck_edges:
        lv = [lvl for (tt, p, lvl) in ev if p == pin and tt <= t]
        out.append(1 if lv and lv[-1] == "H" else 0)
    return out


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_sck_mosi_waveform_cpol_cpha(desc, mode):
    chip = _chip(desc)
    cpol, cpha = mode >> 1, mode & 1
    wr(chip, SPI1 + CR1, MASTER | br(2) | (CPOL if cpol else 0) | (CPHA if cpha else 0))
    assert chip.pins.get("PA5").level() == ("H" if cpol else "L")         # SCK idles at CPOL
    ev = _trace(chip, "PA5", "PA7")
    t0 = chip.now_cycles()
    wr(chip, SPI1 + DR, 0xA5)
    chip.run_cycles(100)
    sck = [(t, lvl) for (t, p, lvl) in ev if p == "PA5"]
    assert [t - t0 for t, _ in sck] == [4 * (k + 1) for k in range(16)]   # half period 4 cycles
    assert sck[-1][1] == ("H" if cpol else "L")
    lead = [t for k, (t, _) in enumerate(sck) if k % 2 == 0]
    trail = [t for k, (t, _) in enumerate(sck) if k % 2 == 1]
    sample = trail if cpha else lead
    assert _bits_at(ev, sample, "PA7") == [1, 0, 1, 0, 0, 1, 0, 1]       # 0xA5 MSB first, stable at the sample


def test_lsbfirst_and_16bit(desc):
    chip = _chip(desc)
    wr(chip, SPI1 + CR1, MASTER | DFF | LSBFIRST)
    ev = _trace(chip, "PA5", "PA7")
    t0 = chip.now_cycles()
    wr(chip, SPI1 + DR, 0x8001)
    sr = _probe(chip, [t0 + 31, t0 + 33], SPI1 + SR)
    chip.run_cycles(40)
    assert [s & RXNE for s in sr] == [0, RXNE]                            # 16 bits x 2 cycles
    lead = [t for k, (t, p, l) in enumerate([e for e in ev if e[1] == "PA5"]) if k % 2 == 0]
    assert _bits_at(ev, lead, "PA7") == [1] + [0] * 14 + [1]
    assert bytes(chip.peripherals["SPI1"].tx_bytes) == b"\x80\x01"


def test_miso_sampled_from_pin_by_a_python_slave(desc):
    """A bit-banged slave answers on PA6, changing MISO on the trailing (falling) SCK edge (mode 0)."""
    chip = _chip(desc)
    reply = 0x3C
    bits = [(reply >> (7 - i)) & 1 for i in range(8)]
    state = {"i": 0}
    chip.pins.set_external("PA6", bits[0])

    def on_sck(pin):
        if pin.level() == "L" and state["i"] < 7:
            state["i"] += 1
            chip.pins.set_external("PA6", bits[state["i"]])
    wr(chip, SPI1 + CR1, MASTER | br(1))                     # SCK starts driving (idle L) before the slave listens
    chip.pins.input_listeners["PA5"].append(on_sck)
    wr(chip, SPI1 + DR, 0xFF)
    chip.run_cycles(80)
    assert rd(chip, SPI1 + DR) == reply


def test_frames_need_muxed_pins(desc):
    chip = _chip(desc, pins=())
    seen = []
    chip.peripherals["SPI1"].responder = lambda tx, pcs, bits: seen.append(tx) or 0x55
    wr(chip, SPI1 + CR1, MASTER)
    t0 = chip.now_cycles()
    wr(chip, SPI1 + DR, 0x42)
    sr = _probe(chip, [t0 + 17], SPI1 + SR)
    chip.run_cycles(20)
    assert sr == [TXE | RXNE] and rd(chip, SPI1 + DR) == 0 and seen == []      # master still clocks
    assert any("not muxed" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


def test_pins_released_when_spe_clear_and_directions(desc):
    chip = _chip(desc)
    pa5, pa6, pa7 = (chip.pins.get(p) for p in ("PA5", "PA6", "PA7"))
    assert (pa5.owner, pa5.direction) == ("SPI1_SCK", "in")                # not enabled: not driven
    wr(chip, SPI1 + CR1, MASTER | CPOL)
    assert (pa5.direction, pa5.level(), pa7.direction, pa6.direction) == ("out", "H", "out", "in")
    wr(chip, SPI1 + CR1, MSTR | SSM | SSI | CPOL)
    assert pa5.direction == "in" and pa7.direction == "in"


# ------------------------------------------------------------------------------------------------ NSS / MODF
def test_hardware_nss_output(desc):
    chip = _chip(desc, nss=True)
    pa4 = chip.pins.get("PA4")
    wr(chip, SPI1 + CR2, SSOE)
    wr(chip, SPI1 + CR1, MSTR | br(0))
    assert pa4.direction == "in"
    wr(chip, SPI1 + CR1, MSTR | br(0) | SPE)
    assert (pa4.direction, pa4.level()) == ("out", "L")
    wr(chip, SPI1 + CR1, MSTR | br(0))
    assert pa4.direction == "in"


def test_modf_on_nss_low_and_clear_sequence(desc):
    chip = _chip(desc, nss=True)
    chip.pins.set_external("PA4", 1)
    wr(chip, SPI1 + CR2, ERRIE)
    wr(chip, SPI1 + CR1, MSTR | SPE)
    assert rd(chip, SPI1 + CR1) & (MSTR | SPE) == MSTR | SPE
    chip.pins.set_external("PA4", 0)                                    # another master pulls NSS low
    assert rd(chip, SPI1 + CR1) & (MSTR | SPE) == 0 and _irq(chip)
    wr(chip, SPI1 + CR1, MSTR | SPE)                                    # no SR access yet: still blocked
    assert rd(chip, SPI1 + CR1) & (MSTR | SPE) == 0
    assert rd(chip, SPI1 + SR) & MODF
    chip.pins.set_external("PA4", 1)
    wr(chip, SPI1 + CR1, MSTR | SPE)
    assert not rd(chip, SPI1 + SR) & MODF and rd(chip, SPI1 + CR1) & SPE and not _irq(chip)


def test_modf_ssm_ssi_low(desc):
    chip = _chip(desc)
    wr(chip, SPI1 + CR1, MSTR | SSM | SPE)
    assert rd(chip, SPI1 + SR) & MODF and not rd(chip, SPI1 + CR1) & SPE


# ------------------------------------------------------------------------------------------------ CRC
def test_crc_next_and_crcerr(desc):
    chip = _chip(desc)
    spi = chip.peripherals["SPI1"]
    echo = {"bad": False}
    spi.responder = lambda tx, pcs, bits: (tx ^ 1) if echo["bad"] else tx
    wr(chip, SPI1 + CRCPR, 7)
    wr(chip, SPI1 + CR1, MASTER | CRCEN)
    data = [0x12, 0x34]
    for d in data:
        wr(chip, SPI1 + DR, d)
    wr(chip, SPI1 + CR1, MASTER | CRCEN | CRCNEXT)
    chip.run_cycles(100)
    exp = 0
    for d in data:
        exp = crc_update(exp, d, 8, 7)
    assert [f[0] for f in spi.frames] == data + [exp]
    assert rd(chip, SPI1 + TXCRCR) == exp == rd(chip, SPI1 + RXCRCR)
    assert not rd(chip, SPI1 + SR) & CRCERR and not rd(chip, SPI1 + CR1) & CRCNEXT
    # a wrong CRC word from the slave
    wr(chip, SPI1 + CR1, MASTER)
    wr(chip, SPI1 + CR1, MASTER | CRCEN)                     # CRCEN toggle clears the CRC registers
    assert rd(chip, SPI1 + TXCRCR) == 0
    rd(chip, SPI1 + DR)
    wr(chip, SPI1 + DR, 0x55)
    wr(chip, SPI1 + CR1, MASTER | CRCEN | CRCNEXT)
    chip.run_cycles(10)                                      # data frame still shifting
    echo["bad"] = True
    chip.run_cycles(40)
    assert rd(chip, SPI1 + SR) & CRCERR
    wr(chip, SPI1 + SR, 0)
    assert not rd(chip, SPI1 + SR) & CRCERR
    assert crc_update(0, 0x12, 8, 7) == 0x7E                 # CRC-8 (poly 0x07) of 0x12


# ------------------------------------------------------------------------------------------------ IRQ / DMA
def test_irq_txe_rxne(desc):
    chip = _chip(desc)
    wr(chip, SPI1 + CR1, MASTER)
    wr(chip, SPI1 + CR2, TXEIE)
    assert _irq(chip)
    wr(chip, SPI1 + CR2, RXNEIE)
    assert not _irq(chip)
    wr(chip, SPI1 + DR, 1)
    chip.run_cycles(20)
    assert _irq(chip)
    rd(chip, SPI1 + DR)
    assert not _irq(chip)


def test_dma_tx_and_rx(desc):
    chip = _chip(desc)
    ram_tx, ram_rx = 0x20001000, 0x20001100
    chip.bus.write_bytes(ram_tx, b"\x9f\x00\x00\x00")
    flash = SpiNorFlash(0xEF4016)
    chip.peripherals["SPI1"].responder = flash.transfer
    for s, cr, m in ((0, P2M, ram_rx), (3, M2P, ram_tx)):      # DMA2 stream 0 ch3 SPI1_RX, stream 3 ch3 SPI1_TX
        wr(chip, sreg(DMA2, s, NDTR), 4)
        wr(chip, sreg(DMA2, s, PAR), SPI1 + DR)
        wr(chip, sreg(DMA2, s, M0AR), m)
        wr(chip, sreg(DMA2, s, CR), cr | MINC | chsel(3) | 1)
    wr(chip, SPI1 + CR1, MASTER | br(1))
    wr(chip, SPI1 + CR2, TXDMAEN | RXDMAEN)
    chip.run_cycles(4 * 32 + 50)
    assert [f[0] for f in chip.peripherals["SPI1"].frames] == [0x9F, 0, 0, 0]
    assert chip.bus.read_bytes(ram_rx, 4) == b"\xff\xef\x40\x16"
    assert rd(chip, sreg(DMA2, 0, NDTR)) == 0 and rd(chip, sreg(DMA2, 3, NDTR)) == 0
    assert not rd(chip, SPI1 + SR) & OVR


# ------------------------------------------------------------------------------------------------ flash device
def _gpio_out(chip, pin, level):
    moder = rd(chip, GPIOA)
    wr(chip, GPIOA, (moder & ~(3 << 2 * pin)) | 1 << 2 * pin)
    odr = rd(chip, GPIOA + 0x14)
    wr(chip, GPIOA + 0x14, (odr | 1 << pin) if level else (odr & ~(1 << pin)))


def _xfer(chip, data):
    out = []
    for b in data:
        wr(chip, SPI1 + DR, b)
        chip.run_until(lambda c: rd(c, SPI1 + SR) & RXNE, max_ns=1e6, poll_ns=100)
        out.append(rd(chip, SPI1 + DR))
    return out


def test_spi_flash_device_with_gpio_cs(desc):
    from chipem.devices.spi_flash import attach_from_spec
    chip = _chip(desc)
    _gpio_out(chip, 4, 1)
    dev = attach_from_spec(chip, "SPI1:cs=PA4,jedec=0x202016")
    dev.mem[0x100:0x104] = b"DATA"
    wr(chip, SPI1 + CR1, MASTER | br(1))
    _gpio_out(chip, 4, 0)
    assert _xfer(chip, [0x9F, 0, 0, 0]) == [0xFF, 0x20, 0x20, 0x16]
    _gpio_out(chip, 4, 1)
    _gpio_out(chip, 4, 0)
    assert _xfer(chip, [0x03, 0x00, 0x01, 0x00, 0, 0, 0, 0])[4:] == list(b"DATA")
    _gpio_out(chip, 4, 1)


def test_spi_flash_via_session_name(desc):
    from chipem.session import Session
    s = Session.__new__(Session)              # only the parser: the device attaches by block name
    from chipem.devices.spi_flash import parse_spec
    assert parse_spec("SPI1:cs=PA4") == ("SPI1", {"cs": "PA4"})
    del s


# ------------------------------------------------------------------------------------------------ slave
def test_slave_mode_shifts_on_pin_edges(desc):
    chip = _chip(desc, nss=True)
    pins = chip.pins
    pins.set_external("PA4", 1)
    pins.set_external("PA5", 0)
    wr(chip, SPI1 + CR1, SPE)                                       # slave, mode 0, hardware NSS
    wr(chip, SPI1 + DR, 0xC3)                                       # slave reply
    assert pins.get("PA6").direction == "in"                        # MISO released while not selected
    pins.set_external("PA4", 0)
    assert pins.get("PA6").direction == "out" and rd(chip, SPI1 + SR) & TXE
    got = []
    send = 0x5A
    for i in range(8):
        pins.set_external("PA7", (send >> (7 - i)) & 1)
        pins.set_external("PA5", 1)                                 # leading edge: both sides sample
        got.append(1 if pins.get("PA6").level() == "H" else 0)
        pins.set_external("PA5", 0)
    assert rd(chip, SPI1 + SR) & RXNE and rd(chip, SPI1 + DR) == send
    assert int("".join(map(str, got)), 2) == 0xC3
    pins.set_external("PA4", 1)
    assert pins.get("PA6").direction == "in"


def test_slave_ignores_clock_when_not_selected(desc):
    chip = _chip(desc, nss=True)
    pins = chip.pins
    pins.set_external("PA4", 1)
    wr(chip, SPI1 + CR1, SPE)
    for _ in range(8):
        pins.set_external("PA5", 1)
        pins.set_external("PA5", 0)
    assert not rd(chip, SPI1 + SR) & RXNE


def test_i2s_mode_warns(desc):
    chip = _chip(desc)
    wr(chip, SPI1 + I2SCFGR, 1 << 11)
    assert any("I2S" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


def test_rxonly_master_clocks_continuously(desc):
    chip = _chip(desc)
    chip.pins.set_external("PA6", 1)
    wr(chip, SPI1 + CR1, MASTER | RXONLY)
    chip.run_cycles(16 * 3 + 1)
    spi = chip.peripherals["SPI1"]
    assert len(spi.frames) == 3 and rd(chip, SPI1 + SR) & OVR
    assert chip.pins.get("PA7").direction == "in"                   # MOSI not driven in RXONLY
    wr(chip, SPI1 + CR1, MSTR | SSM | SSI | RXONLY)
    n = len(spi.frames)
    chip.run_cycles(100)
    assert len(spi.frames) == n


@pytest.mark.parametrize("cpha", [0, 1])
def test_slave_two_frames_per_select_and_reselect(desc, cpha):
    """Slave answers frame by frame from the TX buffer (refilled on TXE), across NSS high/low, CPHA 0 and 1."""
    chip = _chip(desc, nss=True)
    pins = chip.pins
    pins.set_external("PA4", 1)
    pins.set_external("PA5", 0)
    wr(chip, SPI1 + CR1, SPE | (CPHA if cpha else 0))
    replies = iter([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
    wr(chip, SPI1 + DR, next(replies))

    def frame(send):
        got = 0
        for i in range(8):
            if rd(chip, SPI1 + SR) & TXE:
                wr(chip, SPI1 + DR, next(replies))
            if cpha:
                pins.set_external("PA5", 1)                     # leading: slave shifts out
                pins.set_external("PA7", (send >> (7 - i)) & 1)
                got = got << 1 | (pins.get("PA6").level() == "H")
                pins.set_external("PA5", 0)                     # trailing: both sample
            else:
                pins.set_external("PA7", (send >> (7 - i)) & 1)
                got = got << 1 | (pins.get("PA6").level() == "H")
                pins.set_external("PA5", 1)
                pins.set_external("PA5", 0)
        assert rd(chip, SPI1 + DR) == send
        return got

    pins.set_external("PA4", 0)
    assert [frame(0xA0), frame(0xA1)] == [0x11, 0x22]
    pins.set_external("PA4", 1)
    pins.set_external("PA4", 0)
    assert [frame(0xA2), frame(0xA3)] == [0x33, 0x44]


# ------------------------------------------------------------------------------------------------ RM0090 28.3.6-28.3.10
FRF, BIDIMODE, BIDIOE, FRE = 0x10, 0x8000, 0x4000, 0x100


def test_bidi_receive_master_keeps_bsy_low(desc):
    """28.3.7: master bidirectional receive mode (MSTR=1, BIDIMODE=1, BIDIOE=0) keeps BSY low; RXONLY sets it."""
    chip = _chip(desc)
    wr(chip, SPI1 + CR1, MASTER | BIDIMODE)
    srs = _probe(chip, [chip.now_cycles() + k for k in (1, 5, 9, 13)], SPI1 + SR)
    chip.run_cycles(20)
    assert not any(s & BSY for s in srs) and chip.peripherals["SPI1"].frames
    wr(chip, SPI1 + CR1, MSTR | SSM | SSI)
    rd(chip, SPI1 + DR)
    rd(chip, SPI1 + SR)
    wr(chip, SPI1 + CR1, MASTER | RXONLY)
    srs = _probe(chip, [chip.now_cycles() + k for k in (5, 9)], SPI1 + SR)
    chip.run_cycles(12)
    assert all(s & BSY for s in srs)


def test_overrun_loses_every_later_word_until_cleared(desc):
    """28.3.10: on overrun the RX buffer keeps the old word and all later words are lost until OVR is cleared."""
    chip = _chip(desc)
    spi = chip.peripherals["SPI1"]
    words = iter([0x11, 0x22, 0x33, 0x44])
    spi.responder = lambda tx, pcs, bits: next(words)
    wr(chip, SPI1 + CR1, MASTER | br(0))
    for d in (1, 2):
        wr(chip, SPI1 + DR, d)
    chip.run_cycles(40)
    assert rd(chip, SPI1 + DR) == 0x11                         # the second word was lost: OVR
    wr(chip, SPI1 + DR, 3)
    chip.run_cycles(20)
    assert not spi.get("SR") & RXNE                            # OVR still set: 0x33 lost as well
    assert rd(chip, SPI1 + SR) & OVR                           # DR read then SR read clears OVR
    wr(chip, SPI1 + DR, 4)
    chip.run_cycles(20)
    assert rd(chip, SPI1 + SR) & RXNE and rd(chip, SPI1 + DR) == 0x44


def _slave_frame(chip, send, cpha=0):
    pins = chip.pins
    got, bsy = 0, []
    for i in range(8):
        pins.set_external("PA7", (send >> (7 - i)) & 1)
        if not cpha:
            got = got << 1 | (pins.get("PA6").level() == "H")
        pins.set_external("PA5", 1)
        if cpha:
            got = got << 1 | (pins.get("PA6").level() == "H")
        pins.set_external("PA5", 0)
        bsy.append(bool(chip.peripherals["SPI1"].get("SR") & BSY))
    return got, bsy


def test_slave_bsy_follows_transfers(desc):
    """28.3.7: a slave sets BSY when a transfer starts (first SCK edge), not on NSS; low between frames."""
    chip = _chip(desc, nss=True)
    pins = chip.pins
    pins.set_external("PA4", 1)
    pins.set_external("PA5", 0)
    wr(chip, SPI1 + CR1, SPE)
    pins.set_external("PA4", 0)
    assert not rd(chip, SPI1 + SR) & BSY
    _, bsy = _slave_frame(chip, 0x5A)
    assert bsy == [True] * 7 + [False]
    _, bsy = _slave_frame(chip, 0xA5)
    assert bsy == [True] * 7 + [False]


def test_slave_crcnext_sends_and_checks_crc(desc):
    """28.3.6: CRC in slave mode: CRCNEXT after the last data written makes the next frame TXCRCR, the received
    frame is checked against RXCRCR (CRCERR on mismatch)."""
    chip = _chip(desc, nss=True)
    pins = chip.pins
    pins.set_external("PA4", 1)
    pins.set_external("PA5", 0)
    wr(chip, SPI1 + CRCPR, 7)
    wr(chip, SPI1 + CR1, SPE | CRCEN)
    wr(chip, SPI1 + DR, 0x12)
    wr(chip, SPI1 + CR1, SPE | CRCEN | CRCNEXT)
    pins.set_external("PA4", 0)
    got, _ = _slave_frame(chip, 0x34)
    assert got == 0x12 and rd(chip, SPI1 + DR) == 0x34
    rx_crc = crc_update(0, 0x34, 8, 7)
    got, _ = _slave_frame(chip, rx_crc)
    assert got == crc_update(0, 0x12, 8, 7)
    assert not rd(chip, SPI1 + SR) & CRCERR and rd(chip, SPI1 + DR) == rx_crc
    # a wrong CRC from the master (CRC cleared between deselection and reselection as 28.3.6 prescribes)
    pins.set_external("PA4", 1)
    wr(chip, SPI1 + CR1, 0)
    wr(chip, SPI1 + CR1, CRCEN)
    wr(chip, SPI1 + CR1, SPE | CRCEN)
    wr(chip, SPI1 + DR, 0x55)
    wr(chip, SPI1 + CR1, SPE | CRCEN | CRCNEXT)
    pins.set_external("PA4", 0)
    got, _ = _slave_frame(chip, 0x66)
    assert got == 0x55
    rd(chip, SPI1 + DR)
    _slave_frame(chip, crc_update(0, 0x66, 8, 7) ^ 1)
    assert rd(chip, SPI1 + SR) & CRCERR


def test_ti_master_frame_pulse_and_timing(desc):
    """28.3.3, Figures 251/252: TI master: CPOL=0/CPHA=1 and MSB first forced, NSS pulses high for one SCK cycle
    before the first frame and during the LSB of a frame followed at once by the next one (no extra clock)."""
    chip = _chip(desc, nss=True)
    wr(chip, SPI1 + CR2, FRF)
    wr(chip, SPI1 + CR1, MSTR | SPE | CPOL | LSBFIRST | br(1))   # CPOL / LSBFIRST ignored in TI mode
    assert chip.pins.get("PA5").level() == "L" and chip.pins.get("PA4").level() == "L"
    ev = _trace(chip, "PA4", "PA5", "PA7")
    t0 = chip.now_cycles()
    wr(chip, SPI1 + DR, 0xA5)
    wr(chip, SPI1 + DR, 0x3C)
    chip.run_cycles(200)
    sck = [t - t0 for (t, p, lvl) in ev if p == "PA5"]
    assert len(sck) == 2 * (1 + 8 + 8) and sck == [2 * (k + 1) for k in range(34)]   # one prefix clock only
    nss = [(t - t0, lvl) for (t, p, lvl) in ev if p == "PA4"]
    assert nss == [(2, "H"), (6, "L"), (2 + 4 * 8, "H"), (2 + 4 * 9, "L")]
    rises = [t for (t, p, lvl) in ev if p == "PA5" and lvl == "H"][1:]
    falls = [t for (t, p, lvl) in ev if p == "PA5" and lvl == "L"][1:]
    assert _bits_at(ev, falls, "PA7") == [1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 1, 0, 0]    # sampled on falling
    assert len(rises) == 16
    assert not any("TI frame" in e.fields.get("msg", "") for e in chip.log if e.kind == "warn")


def test_ti_slave_receive_transmit_and_frame_error(desc):
    """28.3.2 / 28.3.10: TI slave: NSS sampled on falling SCK edges, data out on rising edges, MISO released after
    the frame; an NSS pulse inside a frame sets FRE (cleared by an SR read) and the frame is dropped."""
    chip = _chip(desc, nss=True)
    pins = chip.pins
    pins.set_external("PA4", 0)
    pins.set_external("PA5", 0)
    wr(chip, SPI1 + CR2, FRF)
    wr(chip, SPI1 + CR1, SPE | CPHA)
    wr(chip, SPI1 + DR, 0xC3)
    assert pins.get("PA6").direction == "in"

    def clk(nss=0, mosi=0):
        pins.set_external("PA4", nss)
        pins.set_external("PA7", mosi)
        pins.set_external("PA5", 1)
        b = pins.get("PA6").level() == "H"
        pins.set_external("PA5", 0)
        return b

    clk(nss=1)                                                 # frame pulse
    assert pins.get("PA6").direction == "out"
    got = 0
    for i in range(8):
        got = got << 1 | clk(mosi=(0x96 >> (7 - i)) & 1)
    assert got == 0xC3 and rd(chip, SPI1 + DR) == 0x96
    chip.run_cycles(64)
    assert pins.get("PA6").direction == "in"                   # t_release after the LSB
    clk(nss=1)
    for _ in range(3):
        clk(mosi=1)
    clk(nss=1)                                                 # pulse inside the frame: FRE
    assert rd(chip, SPI1 + SR) & FRE
    assert not rd(chip, SPI1 + SR) & FRE
    for _ in range(8):
        clk(mosi=1)
    assert not rd(chip, SPI1 + SR) & RXNE                      # waits for the next pulse


def test_reset_values_and_reserved_bits(desc):
    """28.5.2/28.5.8/28.5.9: I2SPR resets to 0x0002 (I2SDIV = 2); reserved bits read 0 (CR2 bit 3, I2SCFGR bit 6)."""
    chip = _chip(desc)
    assert rd(chip, SPI1 + 0x20) == 0x0002 and rd(chip, SPI1 + SR) == TXE
    wr(chip, SPI1 + CR2, 0xFFFF)
    assert rd(chip, SPI1 + CR2) == 0xF7
    wr(chip, SPI1 + CR2, 0)
