"""Kinetis I2C (chipem/periph/kinetis/i2c.py) on the pin-level I2C engine (chipem/periph/i2cwire.py): bit timing from
F (MULT x Table 48-54), START/STOP/ACK on the pins, NACK of an absent address, EEPROM part, slave mode against the
chip's own second I2C module, arbitration, ODE push-pull, IRQ and DMA requests.  The firmware (button_mirror) only
idles; the tests drive the I2C registers directly between short runs."""
from pathlib import Path

import pytest

from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware/samples/build/button_mirror.elf"
pytestmark = pytest.mark.skipif(not FW.exists(), reason="sample firmware not built")

A1, F, C1, S, D, C2, FLT, RA = range(8)
IICEN, IICIE, MST, TX, TXAK, RSTA, DMAEN = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x01
TCF, IAAS, BUSY, ARBL, SRW, IICIF, RXAK = 0x80, 0x40, 0x20, 0x10, 0x04, 0x02, 0x01
ODE = 0x20
NS = 1e9 / 120e6                  # one bus clock (bus = core = 120 MHz: SIM_CLKDIV1 at reset)


def _session(with_eeprom=True, pullups=True, ode=True, i2c1=False):
    s = Session("mk22fn1m0vll12", FW, clock_hz=120e6)
    chip = s.chip
    pcr = (2 << 8) | (ODE if ode else 0)
    for pin in (2, 3):                                   # PTB2 = I2C0_SCL, PTB3 = I2C0_SDA (ALT2)
        chip.peripherals["PORTB"].write(4 * pin, 4, pcr)
    b = s.ensure_board()
    b.add_net("SCL", [("A", "PTB2")], open_drain=pullups)
    b.add_net("SDA", [("A", "PTB3")], open_drain=pullups)
    if i2c1:                                             # PTC10 = I2C1_SCL, PTC11 = I2C1_SDA (ALT2) on the same bus
        for pin in (10, 11):
            chip.peripherals["PORTC"].write(4 * pin, 4, pcr)
        b.add_net("SCL", [("A", "PTC10")], extend=True)
        b.add_net("SDA", [("A", "PTC11")], extend=True)
    ee = b.add_part("EE", "i2c_eeprom", {"init": "ABCDEFGH"}, {"sda": "SDA", "scl": "SCL"}) if with_eeprom else None
    return s, chip, b, ee


def _edges(chip):
    ev = []
    chip.pins.subscribe(lambda e: ev.append((e.t_ns, e.pin, e.new)) if e.field == "level" and e.pin in
                        ("PTB2", "PTB3") else None)
    return ev


def _wait(b, blk, mask=IICIF, max_ns=5e6):
    t = 0.0
    while not blk.read(S, 1) & mask:
        b.run_for(2000)
        t += 2000
        assert t < max_ns, f"timeout waiting for S & 0x{mask:x} (S=0x{blk.read(S, 1):02x})"
    s = blk.read(S, 1)
    blk.write(S, 1, IICIF)
    return s


def test_registers_reset_and_clock_from_icr_table():
    s, chip, b, _ = _session(with_eeprom=False)
    i2c = chip.peripherals["I2C0"]
    assert i2c.read(S, 1) == TCF and i2c.read(C1, 1) == 0 and i2c.read(0x9, 1) == 0xC2     # S, C1, A2 resets
    i2c.write(F, 1, 0x27)                                # MULT 0, ICR 0x27: divider 480
    assert i2c.baud() == pytest.approx(120e6 / 480)
    i2c.write(F, 1, 0x40 | 0x14)                         # MULT 1 (x2), ICR 0x14: divider 80
    t = i2c.timing()
    assert t.period_ns == pytest.approx(160 * NS) and t.hold_ns == pytest.approx(34 * NS)
    assert t.hd_sta_ns == pytest.approx(68 * NS) and t.su_sto_ns == pytest.approx(82 * NS)
    i2c.write(F, 1, 0x80 | 0x3F)                         # MULT 2 (x4), ICR 0x3F: 3840
    assert i2c.baud() == pytest.approx(120e6 / (4 * 3840))


def test_start_address_ack_stop_edges_exact():
    s, chip, b, ee = _session()
    i2c = chip.peripherals["I2C0"]
    ev = _edges(chip)
    i2c.write(F, 1, 0x40 | 0x14)                         # 750 kHz: period 160 clocks, half 80
    i2c.write(C1, 1, IICEN)
    t0 = chip.now_ns()
    i2c.write(C1, 1, IICEN | MST | TX)                   # START
    i2c.write(D, 1, 0x50 << 1)                           # address, write
    st = _wait(b, i2c)
    assert st & TCF and not st & RXAK and st & BUSY      # the EEPROM acknowledged
    sda = [(t - t0, lvl) for t, p, lvl in ev if p == "PTB3"]
    scl = [(t - t0, lvl) for t, p, lvl in ev if p == "PTB2"]
    assert sda[0] == (pytest.approx(0, abs=NS), "L")                       # START: SDA falls while SCL is high
    t_f = 68 * NS                                                           # SCL start hold (34 x mul 2)
    assert scl[0] == (pytest.approx(t_f, abs=NS), "L")
    # 9 clocks: rising edges at t_f + 80 + 160 k, falling at t_f + 160 (k + 1)
    rises = [t for t, lvl in scl if lvl == "H"]
    falls = [t for t, lvl in scl if lvl == "L"]
    assert rises[:9] == [pytest.approx(t_f + (80 + 160 * k) * NS, abs=NS) for k in range(9)]
    assert falls[:10] == [pytest.approx(t_f + 160 * k * NS, abs=NS) for k in range(10)]
    # 0xA0 = 1010 0000: SDA set 34 clocks (SDA hold) after each SCL fall
    assert sda[1] == (pytest.approx(t_f + 34 * NS, abs=NS), "H")            # bit 7 = 1
    assert sda[2] == (pytest.approx(t_f + (160 + 34) * NS, abs=NS), "L")    # bit 6 = 0
    # bits 3..0 are 0, then the EEPROM pulls SDA low for the ACK clock (from the 8th SCL fall): SDA stays low and
    # rises when the EEPROM releases it at the 9th SCL fall
    assert sda[3] == (pytest.approx(t_f + (2 * 160 + 34) * NS, abs=NS), "H")
    assert sda[4] == (pytest.approx(t_f + (3 * 160 + 34) * NS, abs=NS), "L")
    assert sda[5] == (pytest.approx(t_f + 9 * 160 * NS, abs=NS), "H")
    i2c.write(C1, 1, IICEN)                              # MST 1 -> 0: STOP
    b.run_for(5000)
    sda = [(t - t0, lvl) for t, p, lvl in ev if p == "PTB3"]
    scl = [(t - t0, lvl) for t, p, lvl in ev if p == "PTB2"]
    assert scl[-1][1] == "H" and sda[-1][1] == "H"
    assert sda[-1][0] - scl[-1][0] == pytest.approx(82 * NS, abs=NS)        # STOP: SCL stop hold
    assert not i2c.read(S, 1) & BUSY and ee.state()["transfers"] == 1


def test_absent_address_nacks_at_ninth_clock():
    s, chip, b, _ = _session()
    i2c = chip.peripherals["I2C0"]
    i2c.write(F, 1, 0x27)                                # 250 kHz: period 480 clocks = 4 us
    i2c.write(C1, 1, IICEN | IICIE)
    t0 = chip.now_ns()
    i2c.write(C1, 1, IICEN | IICIE | MST | TX)
    i2c.write(D, 1, 0x33 << 1)
    flagged = []
    i2c_irq = i2c._irqn
    while not i2c.read(S, 1) & IICIF:
        b.run_for(100)
    flagged.append(chip.now_ns() - t0)
    st = i2c.read(S, 1)
    assert st & RXAK and st & TCF and i2c._line           # NACK: RXAK = 1; IICIF & IICIE drive the I2C0 line
    assert i2c_irq == 24
    t_end = 238 * NS + 9 * 480 * NS                       # START hold (238) + 9 SCL periods
    assert t_end <= flagged[0] < t_end + 120                # within the polling step
    i2c.write(S, 1, IICIF)
    assert not i2c._line
    i2c.write(C1, 1, IICEN)
    b.run_for(20000)
    assert not i2c.read(S, 1) & BUSY


def _write_bytes(b, i2c, addr, data):
    i2c.write(C1, 1, IICEN | MST | TX)
    i2c.write(D, 1, addr << 1)
    acks = [not _wait(b, i2c) & RXAK]
    for x in data:
        i2c.write(D, 1, x)
        acks.append(not _wait(b, i2c) & RXAK)
    i2c.write(C1, 1, IICEN)
    b.run_for(30000)
    return acks


def _read_bytes(b, i2c, addr, word, n):
    i2c.write(C1, 1, IICEN | MST | TX)
    i2c.write(D, 1, addr << 1)
    _wait(b, i2c)
    i2c.write(D, 1, word)
    _wait(b, i2c)
    i2c.write(C1, 1, IICEN | MST | TX | RSTA)            # repeated START
    i2c.write(D, 1, (addr << 1) | 1)
    assert not _wait(b, i2c) & RXAK
    i2c.write(C1, 1, IICEN | MST | (TXAK if n == 1 else 0))
    i2c.read(D, 1)                                       # dummy read starts the first byte
    out = []
    for k in range(n):
        _wait(b, i2c)
        if k == n - 1:
            i2c.write(C1, 1, IICEN)                      # STOP before the last read of D
        elif k == n - 2:
            i2c.write(C1, 1, IICEN | MST | TXAK)
        out.append(i2c.read(D, 1))
    b.run_for(30000)
    return bytes(out)


def test_eeprom_page_write_ack_polling_and_sequential_read():
    s, chip, b, ee = _session()
    i2c = chip.peripherals["I2C0"]
    i2c.write(F, 1, 0x40 | 0x14)
    i2c.write(C1, 1, IICEN)
    assert _read_bytes(b, i2c, 0x50, 0, 4) == b"ABCD"
    assert _write_bytes(b, i2c, 0x50, [0x02, 0x31, 0x32]) == [True] * 4
    assert ee.busy                                       # tWR running: the EEPROM ignores its address
    assert _write_bytes(b, i2c, 0x50, []) == [False]
    b.run_for(5.1e6)
    assert not ee.busy and ee.dump(0, 8) == b"AB12EFGH"
    assert _read_bytes(b, i2c, 0x50, 1, 3) == b"B12"
    assert ee.state()["naks"] == 1


def test_slave_mode_against_second_module():
    s, chip, b, ee = _session(with_eeprom=False, i2c1=True)
    m, sl = chip.peripherals["I2C0"], chip.peripherals["I2C1"]
    m.write(F, 1, 0x27)
    m.write(C1, 1, IICEN)
    sl.write(A1, 1, 0x3A << 1)
    sl.write(C1, 1, IICEN)
    # master writes 0x11 0x22 to 0x3A; the slave handshakes each byte with a D read (stretching SCL meanwhile)
    m.write(C1, 1, IICEN | MST | TX)
    m.write(D, 1, 0x3A << 1)
    st = _wait(b, sl)
    assert st & IAAS and not st & SRW
    sl.write(C1, 1, IICEN)                               # RX; any C1 write clears IAAS
    assert not sl.read(S, 1) & IAAS
    sl.read(D, 1)                                        # dummy read releases SCL
    assert not _wait(b, m) & RXAK
    m.write(D, 1, 0x11)
    _wait(b, sl)                                         # slave: byte received (IICIF at the 9th SCL fall)
    assert not _wait(b, m) & RXAK                        # master: byte done, acknowledged
    m.write(D, 1, 0x22)                                  # next byte: the slave still stretches SCL (D not read)
    b.run_for(20000)
    assert m.port.master.busy and chip.pins.get("PTB2").level() == "L" and not sl.read(S, 1) & IICIF
    got = [sl.read(D, 1)]                                # the handshake releases SCL
    _wait(b, sl)
    assert not _wait(b, m) & RXAK
    got.append(sl.read(D, 1))
    assert got == [0x11, 0x22]
    # read two bytes from the slave: repeated START, slave transmits on D writes
    m.write(C1, 1, IICEN | MST | TX | RSTA)
    m.write(D, 1, (0x3A << 1) | 1)
    st = _wait(b, sl)
    assert st & IAAS and st & SRW
    sl.write(C1, 1, IICEN | TX)
    sl.write(D, 1, 0xC5)
    assert not _wait(b, m) & RXAK
    m.write(C1, 1, IICEN | MST)                          # master receive
    m.read(D, 1)
    _wait(b, m)
    assert m.read(S, 1) & TCF
    st = _wait(b, sl)                                    # the master acknowledged 0xC5
    assert not st & RXAK
    sl.write(D, 1, 0x5C)
    m.write(C1, 1, IICEN | MST | TXAK)                   # NACK the second byte
    assert m.read(D, 1) == 0xC5
    _wait(b, m)
    st = _wait(b, sl)
    assert st & RXAK                                     # the slave sees the NACK
    m.write(C1, 1, IICEN)                                # STOP
    assert m.read(D, 1) == 0x5C
    b.run_for(30000)
    assert not m.read(S, 1) & BUSY and not sl.read(S, 1) & BUSY


def test_start_while_bus_busy_loses_arbitration():
    s, chip, b, ee = _session(i2c1=True)
    m, other = chip.peripherals["I2C0"], chip.peripherals["I2C1"]
    m.write(F, 1, 0x27)
    other.write(C1, 1, IICEN)
    m.write(C1, 1, IICEN | MST | TX)
    m.write(D, 1, 0x50 << 1)
    b.run_for(5000)
    assert other.read(S, 1) & BUSY                       # the other module saw the START
    other.write(C1, 1, IICEN | MST | TX)
    st = other.read(S, 1)
    assert st & ARBL and st & IICIF and not other.read(C1, 1) & MST
    other.write(S, 1, ARBL | IICIF)                      # w1c
    assert not other.read(S, 1) & (ARBL | IICIF)
    assert not _wait(b, m) & RXAK


def test_without_ode_the_released_line_is_push_pull():
    s, chip, b, ee = _session(ode=False, pullups=False)    # plain nets: the EEPROM breakout pull-ups idle them H
    i2c = chip.peripherals["I2C0"]
    sda = chip.pins.get("PTB3")
    assert sda.direction == "in"                         # module disabled: the pin is not driven
    i2c.write(F, 1, 0x27)
    i2c.write(C1, 1, IICEN)
    assert (sda.direction, sda.drive) == ("out", "H")
    i2c.write(C1, 1, IICEN | MST | TX)
    i2c.write(D, 1, 0x50 << 1)
    st = _wait(b, i2c)
    assert st & RXAK                                     # the EEPROM's ACK fights the H drive: X, read as 1
    assert any("PCR.ODE" in str(e.fields.get("msg")) for e in b.log.filter("warn"))
    assert any("drive conflict" in str(e.fields.get("msg")) for e in b.log.filter("warn"))
    i2c.write(C1, 1, 0)
    assert sda.direction == "in"


def test_dma_request_per_byte(monkeypatch):
    import chipem.periph.kinetis.i2c as mod
    reqs = []
    monkeypatch.setattr(mod, "dma_request", lambda chip, src: reqs.append(src) or True)
    s, chip, b, ee = _session()
    i2c = chip.peripherals["I2C0"]
    i2c.write(F, 1, 0x40 | 0x14)
    i2c.write(C1, 1, IICEN | DMAEN | MST | TX)
    i2c.write(D, 1, 0x50 << 1)
    _wait(b, i2c)
    assert reqs == [18] and i2c._dma
    i2c.write(D, 1, 0x00)                                # the DMA (here: we) writes D: request served
    assert not i2c._dma
    _wait(b, i2c)
    assert reqs == [18, 18]
    assert chip.peripherals["I2C2"].dma_source == 19


def test_disable_mid_transfer_releases_lines_and_reset_cancels_events():
    s, chip, b, ee = _session()
    i2c = chip.peripherals["I2C0"]
    i2c.write(F, 1, 0x27)
    i2c.write(C1, 1, IICEN | MST | TX)
    i2c.write(D, 1, 0x50 << 1)
    b.run_for(10000)
    assert i2c.port.master.busy
    i2c.write(C1, 1, 0)
    assert not i2c.port.master.busy and not chip.pins.od_low
    assert chip.pins.get("PTB2").level() == "H" and chip.pins.get("PTB3").level() == "H"
    i2c.write(C1, 1, IICEN | MST | TX)
    i2c.write(D, 1, 0x50 << 1)
    b.run_for(5000)
    chip.reset()
    assert not i2c.port.master.busy and i2c.read(S, 1) == TCF
    assert not any(not ev.cancelled and getattr(ev.cb, "__self__", None) is i2c.port.master
                   for ev in chip.sched.pending())
