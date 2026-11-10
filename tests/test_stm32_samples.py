"""End-to-end runs of the STM32F405ZG sample firmware (firmware/samples_stm32/build/*.elf) and of the mixed
K22 + STM32 board (boards/k22_stm32_uart.yaml)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32" / "build"
YAML = ROOT / "chips" / "stm32f405zg.yaml"
BOARD = ROOT / "boards" / "k22_stm32_uart.yaml"
K22_SENDER = ROOT / "firmware" / "samples" / "build" / "uart_link_sender.elf"


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def boot(desc, name: str, pin: str = "PB0", **kw) -> tuple[Chip, list]:
    elf = BUILD / f"{name}.elf"
    if not elf.exists():
        pytest.skip(f"{elf} not built (firmware/samples_stm32/build.sh)")
    chip = Chip(desc, **kw)
    chip.load_firmware(load_firmware(elf))
    chip.reset()
    events: list = []
    chip.pins.subscribe(lambda e: events.append(e) if e.pin == pin and e.field == "level" else None)
    return chip, events


def data_symbol(elf: Path, name: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(elf, "rb") as f:
        sym = ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(name)
    return sym[0]["st_value"]


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def test_blinky_toggles_pb0_and_pb7(desc):
    chip, ev = boot(desc, "blinky")
    ev7: list = []
    chip.pins.subscribe(lambda e: ev7.append(e) if e.pin == "PB7" and e.field == "level" else None)
    chip.run_for(5e6)
    pb0, pb7 = chip.pins.get("PB0"), chip.pins.get("PB7")
    assert (pb0.owner, pb0.direction, pb7.direction) == ("GPIO", "out", "out")
    assert chip.clock.hz == 16e6
    assert len(ev) > 10 and len(ev7) > 10
    assert pb0.level() != pb7.level()                              # opposite phase
    rises = [e.t_ns for e in ev if e.new == "H"]
    periods = [b - a for a, b in zip(rises[1:], rises[2:])]
    assert max(periods) - min(periods) < 1e3                       # regular
    assert not warns(chip)


def test_button_mirror_follows_pa0(desc):
    chip, ev = boot(desc, "button_mirror")
    chip.run_for(0.2e6)
    pa0, pb0 = chip.pins.get("PA0"), chip.pins.get("PB0")
    assert (pa0.direction, pa0.pull, pa0.level()) == ("in", "down", "L")
    assert pb0.level() == "L"
    chip.pins.set_external("PA0", "H")
    chip.run_for(0.1e6)
    assert pb0.level() == "H"
    chip.pins.set_external("PA0", None)                            # released: pull-down wins
    chip.run_for(0.1e6)
    assert pb0.level() == "L"
    assert [e.new for e in ev][-2:] == ["H", "L"]
    assert not [w for w in warns(chip) if "floating" in w]


def test_uart_echo_via_session(desc):
    elf = BUILD / "uart_echo.elf"
    if not elf.exists():
        pytest.skip("not built")
    s = Session(YAML, elf)
    s.schedule_uart(1e6, "USART1", b"abc")     # after "hello\r\n" (~0.6 ms): the polling echo loop keeps up
    s.run_for(5e6)
    assert s.uart_output("USART1") == b"hello\r\nabc"
    assert s.pin_table()[100]["owner"] == "USART1_TX"              # pin 101 = PA9
    blk = s.chip.peripherals["USART1"]
    assert blk.baud() == pytest.approx(16e6 / 139)
    rx = [e for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "rx"]
    tx = [e for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "tx"]
    # the host sends from 1 ms, one character time (10 bits at 115,108 baud = 86.9 us) per byte
    char_ns = 10 * 139 / 16e6 * 1e9
    assert rx[0].t_ns == pytest.approx(1e6 + char_ns, abs=100)
    assert all(b.t_ns - a.t_ns >= char_ns * 0.99 for a, b in zip(rx, rx[1:]))
    assert len(tx) == 10
    s.close()


def test_uart_echo_cli_uart_in():
    elf = BUILD / "uart_echo.elf"
    if not elf.exists():
        pytest.skip("not built")
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--chip", "stm32f405zg", str(elf), "--for", "3ms",
                        "--uart-in", "USART1:xy"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    assert 'USART1 RX: "x"' in r.stdout and "USART1 TX:" in r.stdout


def test_exti_irq_counts_rising_edges(desc):
    chip, ev = boot(desc, "exti_irq")
    chip.run_for(0.1e6)
    assert chip.sleeping
    for k in range(3):
        chip.pins.set_external("PA0", "H")
        chip.run_for(0.2e6)
        chip.pins.set_external("PA0", "L")
        chip.run_for(0.2e6)
    assert chip.bus.read(data_symbol(BUILD / "exti_irq.elf", "presses"), 4) == 3
    assert [e.new for e in ev] == ["L", "H", "L", "H"]             # initial L, then one toggle per rising edge
    assert chip.peripherals["EXTI"].pr_count[0] == 3
    assert chip.bus.read(0x40013C14, 4) == 0                       # handler cleared PR


def test_tim_irq_pll_168mhz_1ms_period(desc):
    chip, ev = boot(desc, "tim_irq")
    chip.run_for(10.5e6)
    rcc = chip.peripherals["RCC"]
    assert rcc.core_hz == 168e6 and chip.clock.hz == 168e6
    assert (rcc.apb1_hz, rcc.apb2_hz, rcc.timer_hz(1)) == (42e6, 84e6, 84e6)
    assert chip.peripherals["FLASH"].latency == 5
    t = [e.t_ns for e in ev[1:]]                                   # toggles from the TIM2 handler
    assert len(t) == 10
    periods = [b - a for a, b in zip(t, t[1:])]
    assert all(p == pytest.approx(1e6, rel=1e-3) for p in periods)
    assert chip.peripherals["TIM2"].updates == 10
    assert chip.bus.read(data_symbol(BUILD / "tim_irq.elf", "tim_ticks"), 4) == 10


def test_samples_need_no_fallback_stubs(desc):
    for name in ("blinky", "uart_echo", "exti_irq", "tim_irq"):
        chip, _ = boot(desc, name)
        chip.run_for(1e6)
        assert not [w for w in warns(chip) if "clock is disabled" in w or "unmodelled" in w], name
        assert not chip.bus.auto_stubs, name


@pytest.mark.skipif(not (BUILD / "uart_link_echo.elf").exists() or not K22_SENDER.exists(),
                    reason="samples not built")
def test_k22_stm32_board_link():
    s = Session.from_board(BOARD)
    s.run_for(7e6)
    out_a, out_b = s.uart_output("A.UART0"), s.uart_output("B.USART1")
    assert out_a.startswith(b"ping 1\r\nping 2\r\nping 3\r\n")
    assert out_b.startswith(b"PING 1\r\nPING 2\r\nPING 3\r\n")
    rx_a = bytes(e.byte for e in s.events(kinds={"uart"}, chip="A") if e.dir == "rx")
    assert rx_a.startswith(b"PING 1\r\n")
    b_led = [e for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PB0" and e.field == "level"]
    assert len(b_led) >= 24                                       # one toggle per echoed byte
    a_led = [e for e in s.events(kinds={"pin"}, chip="A") if e.pin == "PTC5" and e.field == "level"]
    assert len(a_led) >= 3
    assert not [e for e in s.events(kinds={"warn"}, chip="board")]   # baud within tolerance, same frame
    st = s.state()
    assert st["board"]["links"][0]["a_to_b"] == len(out_a)
    assert s.chips["B"].clock.hz == 16e6 and s.chips["A"].clock.hz == 120e6
    s.close()


@pytest.mark.skipif(not (BUILD / "uart_link_echo.elf").exists() or not K22_SENDER.exists(),
                    reason="samples not built")
def test_k22_stm32_board_cli():
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--board", str(BOARD), "--for", "20ms"],
                       capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "A| === summary chip A (MK22FN1M0VLL12)" in out and "B| === summary chip B (STM32F405ZG)" in out
    assert "A.UART0 TX:" in out and 'B.USART1 TX: "P"' in out
    assert "uart A.UART0 <-> B.USART1" in out


def test_pin_report_uses_stm32_names(desc):
    from chipem.pinreport import format_pin_report, report_dict
    chip, _ = boot(desc, "button_mirror")
    chip.run_for(0.5e6)
    chip.pins.set_external("PA0", "H")
    chip.run_for(0.5e6)
    txt = format_pin_report(chip, static=False)
    rows = {r["pin"]: r for r in report_dict(chip, static=False)["pins"]}
    assert set(rows) == {"PA0", "PB0"}                             # reset-state pins are not reported
    assert rows["PA0"]["role"] == "GPIO in (pull-down)" and rows["PA0"]["reads"] > 100
    assert rows["PB0"]["role"] == "GPIO out" and rows["PB0"]["changes"] == 1
    assert "PB0: 1 level change" in txt and "PA0:" in txt
    chip2, _ = boot(desc, "uart_echo")
    chip2.run_for(0.5e6)
    rows2 = {r["pin"]: r for r in report_dict(chip2, static=False)["pins"]}
    assert rows2["PA9"]["role"] == "USART1_TX" and "USART1: PA9 TX, PA10 RX" in format_pin_report(chip2, static=False)


def test_tim_pwm_sample_1khz_25_percent(desc):
    """tim_pwm: TIM3 CH1 PWM mode 1 on PA6 at the reset clock (16 MHz), PSC 15, ARR 999, CCR1 250."""
    chip, ev = boot(desc, "tim_pwm", pin="PA6")
    chip.run_for(20.5e6)
    pin = chip.pins.get("PA6")
    assert pin.owner == "TIM3_CH1" and pin.direction == "out"
    edges = [(e.t_ns, e.new) for e in ev if e.new in ("H", "L")]
    rises = [t for t, v in edges if v == "H"]
    periods = [b - a for a, b in zip(rises, rises[1:])]
    highs = [t2 - t for (t, v), (t2, v2) in zip(edges, edges[1:]) if v == "H" and v2 == "L"]
    assert len(periods) >= 19, edges[:6]
    assert all(p == pytest.approx(1e6, abs=1) for p in periods[1:]), periods[:5]     # steady state: exact
    assert periods[0] == pytest.approx(1e6, abs=500), periods[:2]   # first rise at CC1E, 3 cycles before CEN
    assert all(h == pytest.approx(250e3, abs=1) for h in highs[1:]), highs[:5]
    assert highs[0] == pytest.approx(250e3, abs=500), highs[:2]
    assert not chip.bus.auto_stubs


def test_tim_pwm_sample_first_period_exact(desc):
    """The counter is exact from CEN: first fall 250 us and second rise 1 ms after the CEN write.  The first rise
    comes earlier, at the CCER.CC1E write (3 instructions before CEN): PWM1 with the stopped counter at 0 < CCR1
    already has OC1REF = 1 (CCR1 was written before OC1PE was set), so enabling the output drives the pin high, as
    on silicon."""
    chip, ev = boot(desc, "tim_pwm", pin="PA6")
    chip.run_for(2.5e6)
    t_cen = next(e.t_ns for e in chip.log if e.kind == "info" and str(e.fields.get("msg", "")).startswith(
        "TIM3 started"))
    rises = [e.t_ns for e in ev if e.new == "H"]
    falls = [e.t_ns for e in ev if e.new == "L"]
    assert rises[0] < t_cen and t_cen - rises[0] == pytest.approx(3 / 16e6 * 1e9, abs=1)
    assert falls[0] - t_cen == pytest.approx(250e3, abs=1)
    assert rises[1] - t_cen == pytest.approx(1e6, abs=1)
    assert rises[2] - rises[1] == pytest.approx(1e6, abs=1)


def test_tim_capture_sample_measures_injected_period():
    """tim_capture: TIM3 CH2 (PA7) input capture at 1 MHz; the ISR reads CCR2 and the main loop prints the period
    between rising edges.  Edges come from Session stimulus ("TIME:PA7=LEVEL", the `--at` syntax): five rising
    edges 2000 us apart, then one 2500 us later, then 1750 us later (each line takes ~1.4 ms to print)."""
    elf = BUILD / "tim_capture.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session(YAML, elf)
    rises = [1000 + 2000 * k for k in range(5)] + [11500, 13250]
    for t in rises:
        s.apply_stimulus(f"{t}us:PA7=1")
        s.apply_stimulus(f"{t + 300}us:PA7=0")
    s.run_for(16e6)
    out = s.uart_output("USART1").decode()
    assert out == "capture ready\r\n" + "period 2000 us\r\n" * 4 + "period 2500 us\r\nperiod 1750 us\r\n", out
    sym = lambda n: s.chip.bus.read(data_symbol(elf, n), 4)                     # noqa: E731
    assert sym("captures") == 7 and sym("overcaptures") == 0
    pa7 = s.chip.pins.get("PA7")
    assert (pa7.owner, pa7.direction, pa7.pull) == ("TIM3_CH2", "in", "down")
    s.close()


def test_uart_dma_sample_tx_and_rx_by_dma(desc):
    """uart_dma: DMA2 Stream7 ch4 (USART1_TX) sends the banner and the echo, DMA2 Stream5 ch4 (USART1_RX) receives
    8-byte blocks into RAM (RM0090 Table 43)."""
    elf = BUILD / "uart_dma.elf"
    if not elf.exists():
        pytest.skip("not built")
    s = Session(YAML, elf)
    s.schedule_uart(0, "USART1", b"abcdefgh")      # paced at the baud rate from t=0, RX DMA is armed before the banner
    s.schedule_uart(2e6, "USART1", b"12345678")
    s.run_for(5e6)
    assert s.uart_output("USART1") == b"DMA ready\r\nabcdefgh\r\n12345678\r\n"
    chip = s.chip
    assert chip.bus.read_bytes(data_symbol(elf, "rx_buf"), 8) == b"12345678"
    assert chip.bus.read(data_symbol(elf, "rx_blocks"), 4) == 2
    assert chip.bus.read(data_symbol(elf, "tx_done"), 4) == 3          # DMA2_Stream7 TC interrupts
    assert chip.peripherals["USART1"].rx_dropped == 0
    dma2 = chip.peripherals["DMA2"]
    assert dma2.items == 11 + 8 + 10 + 8 + 10
    assert dma2.stream_request(7) == ("USART1_TX",) and dma2.stream_request(5) == ("USART1_RX",)
    # TX bytes leave back to back at the character time (the DMA refills DR on every TXE)
    tx = [e.t_ns for e in s.events(kinds={"uart"}) if e.fields.get("dir") == "tx"]
    char_ns = 10 * 139 / 16e6 * 1e9
    assert all(b - a == pytest.approx(char_ns, abs=150) for a, b in zip(tx[:10], tx[1:11]))
    assert not [w for w in warns(chip) if "DMA" in w]
    s.close()


def test_uart_dma_cli_uart_in():
    elf = BUILD / "uart_dma.elf"
    if not elf.exists():
        pytest.skip("not built")
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--chip", "stm32f405zg", str(elf), "--for", "3ms",
                        "--uart-in", "USART1:qwertyui"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    assert '"DMA ready\\r\\nqwertyui\\r\\n"' in r.stdout and "IRQ70 DMA2_Stream7" in r.stdout


def test_adc_tim_trigger_sample():
    """adc_tim_trigger: TIM3 TRGO (update, 1 kHz) triggers ADC1 IN0 (PA0); the EOC interrupt stores DR and the main
    loop prints it.  Each conversion ends 1 PCLK2 + 2 ADCCLK (latency) + (84 + 12) ADCCLK = 197 core cycles after the
    TIM3 update (16 MHz, ADCCLK = 8 MHz).  The input comes from Session.set_adc (`--adc PA0=1.65V`), then a raw code
    on the ADC1_IN0 name."""
    elf = BUILD / "adc_tim_trigger.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session(YAML, elf)
    s.set_adc("PA0", 1.65)
    adc, tim = s.chip.peripherals["ADC1"], s.chip.peripherals["TIM3"]
    ends: list[int] = []
    done = adc._done
    adc._done = lambda: (ends.append(s.chip.now_cycles()), done())
    s.run_for(3.5e6)
    started = [e.t_ns for e in s.events(kinds={"info"}) if "TIM3 started" in str(e.fields.get("msg"))]
    t0 = round(started[0] * 16e6 / 1e9)
    assert ends == [t0 + 16000 * k + 197 for k in (1, 2, 3)]
    s.set_adc("ADC1_IN0", 1000)
    s.run_for(2.4e6)
    out = s.uart_output("USART1").decode()
    assert out == "adc ready\r\n" + "adc 2048\r\n" * 3 + "adc 1000\r\n" * 2, out
    assert s.chip.bus.read(data_symbol(elf, "overruns"), 4) == 0 and tim.updates == 5
    pa0 = s.chip.pins.get("PA0")
    assert (pa0.owner, pa0.direction) == ("ADC123_IN0", "analog")
    assert not warns(s.chip)
    s.close()


def test_adc_tim_trigger_cli():
    elf = BUILD / "adc_tim_trigger.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--chip", "stm32f405zg", str(elf), "--for", "3ms",
                        "--adc", "ADC1_IN0=0.825V"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    assert '"adc ready\\r\\nadc 1024\\r\\nadc 1024\\r\\n"' in r.stdout, r.stdout


# ------------------------------------------------------------------------------------------------ SPI flash / RTC
SPI_FLASH_OUT = ("JEDEC 202015\r\nSR 00\r\nREAD 43 48 49 50 5F 45 4D" + " FF" * 9 + "\r\nTEXT CHIP_EM\r\ndone\r\n")


def test_spi_flash_sample_with_spi_flash_device():
    elf = BUILD / "spi_flash.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session(YAML, elf)
    dev = s.attach_spi_flash("SPI1:cs=PA4")
    sck = []
    s.chip.pins.subscribe(lambda e: sck.append(e.t_ns) if e.pin == "PA5" and e.field == "level" else None)
    s.run_for(20e6)
    assert s.uart_output("USART1").decode() == SPI_FLASH_OUT
    assert dev.ops == [("program", 0x100)] and dev.mem[0x100:0x107] == b"CHIP_EM"
    spi = s.chip.peripherals["SPI1"]
    assert spi.baud() == pytest.approx(2e6)                       # PCLK2 16 MHz / 8
    nframes = len(spi.frames)
    # X -> L when SPE turns the SCK output on, then 2 edges per bit; a half period is 250 ns
    assert len(sck) == 1 + 16 * nframes
    assert sck[2] - sck[1] == pytest.approx(250, abs=1)
    assert not [w for w in warns(s.chip) if "SPI1" in w]
    s.close()


def test_spi_flash_sample_without_device_reads_zeros():
    elf = BUILD / "spi_flash.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session(YAML, elf)
    s.run_for(20e6)
    assert s.uart_output("USART1").decode().startswith("JEDEC 000000\r\n")      # MISO floats: reads 0 like IDR
    s.close()


def test_spi_flash_sample_cli():
    elf = BUILD / "spi_flash.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--chip", "stm32f405zg", str(elf), "--for", "20ms",
                        "--spi-flash", "SPI1:cs=PA4,jedec=0xEF4016", "--summary-only"], capture_output=True, text=True,
                       cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "JEDEC EF4016" in r.stdout and "TEXT CHIP_EM" in r.stdout, r.stdout


def test_rtc_alarm_sample_every_second_and_warm_boot():
    elf = BUILD / "rtc_alarm.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session(YAML, elf)
    pb0 = []
    s.chip.pins.subscribe(lambda e: pb0.append(e.t_ns) if e.pin == "PB0" and e.field == "level" else None)
    s.run_for(3.5e9)
    out = s.uart_output("USART1").decode()
    assert out == ("cold boot\r\nRTC 2024-02-28 23:59:59\r\nRTC 2024-02-29 00:00:00\r\nRTC 2024-02-29 00:00:01\r\n"), out
    toggles = pb0[1:]                                             # first: L when PB0 becomes an output
    assert len(toggles) == 3
    assert [b - a for a, b in zip(toggles, toggles[1:])] == [pytest.approx(1e9, abs=2e3)] * 2
    s.chip.reset("software")                                      # system reset: the backup domain survives
    s.run_for(1.2e9)
    out2 = s.uart_output("USART1").decode()[len(out):]
    assert out2.startswith("warm boot\r\nRTC 2024-02-29 00:00:0"), out2
    s.close()


# ------------------------------------------------------------------ I2C (chipem/periph/stm32/i2c.py)
I2C_BOARD = ROOT / "boards" / "stm32_i2c_eeprom.yaml"


def test_i2c_eeprom_board_prints_read_back_string():
    if not (BUILD / "i2c_eeprom.elf").exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session.from_board(I2C_BOARD)
    scl = []
    s.chips["A"].pins.subscribe(lambda e: scl.append(e.t_ns) if e.pin == "PB6" and e.field == "level" else None)
    s.run_for(20e6)
    out = s.uart_output("A.USART1").decode()
    assert out.startswith("EEPROM: Hello I2C!\r\npolls: "), out
    polls = int(out.split("polls: ")[1][:2], 16)
    assert 10 < polls < 200                                        # two 5 ms write cycles, acknowledge polled
    ee = s.board.part("EE1")
    assert ee.dump(0x10, 10) == b"Hello I2C!" and ee.writes == 2
    # 100 kHz SM: SCL edges 5 us apart inside a byte
    gaps = [round(b - a, 1) for a, b in zip(scl, scl[1:])]
    assert gaps.count(5000.0) > 100
    assert not [e for e in s.events(kinds={"warn"})]
    s.close()


def test_i2c_slave_sample_answers_a_master_on_the_same_bus():
    """i2c_slave runs I2C1 as slave 0x42 (PB6/PB7); the test drives I2C2 (PB10/PB11, wired to the same nets) as
    master from Python: write pointer 4 + two bytes, then read three registers back."""
    elf = BUILD / "i2c_slave.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session(YAML, elf)
    chip, brd = s.chip, s.ensure_board()
    bus = chip.bus
    m = 0x40005800                                                 # I2C2
    s.run_for(2e6)                                                 # firmware configures I2C1, prints SLAVE READY
    bus.write(0x40023840, 4, bus.read(0x40023840, 4) | (1 << 22))  # RCC_APB1ENR.I2C2EN
    for p in (10, 11):
        bus.write(0x40020404, 4, bus.read(0x40020404, 4) | (1 << p))
        bus.write(0x40020400, 4, (bus.read(0x40020400, 4) & ~(3 << 2 * p)) | (2 << 2 * p))
        bus.write(0x40020424, 4, (bus.read(0x40020424, 4) & ~(0xF << 4 * (p - 8))) | (4 << 4 * (p - 8)))
    brd.add_net("SCL", [("A", "PB6"), ("A", "PB10")], open_drain=True)
    brd.add_net("SDA", [("A", "PB7"), ("A", "PB11")], open_drain=True)
    for off, v in ((0, 1 << 15), (0, 0), (4, 16), (0x1C, 80), (0x20, 17), (0, 1 | (1 << 10))):
        bus.write(m + off, 4, v)

    def wait(bits):
        for _ in range(5000):
            v = bus.read(m + 0x14, 4)
            if v & bits:
                return v
            brd.run_for(1000)
        raise AssertionError(f"timeout, SR1={bus.read(m + 0x14, 4):#x}")

    def start(a):
        bus.write(m, 4, bus.read(m, 4) | (1 << 8))
        wait(1)
        bus.write(m + 0x10, 4, a)
        assert wait(2 | (1 << 10)) & 2
        bus.read(m + 0x14, 4)
        bus.read(m + 0x18, 4)

    start(0x42 << 1)
    for b in (0x04, 0x4B, 0x32):
        wait(0x80)
        bus.write(m + 0x10, 4, b)
    wait(4)
    bus.write(m, 4, bus.read(m, 4) | (1 << 9))
    s.run_for(2e6)
    start(0x42 << 1)
    wait(0x80)
    bus.write(m + 0x10, 4, 0x04)
    wait(0x80)
    bus.write(m, 4, bus.read(m, 4) | (1 << 8))                     # repeated START (after the pointer byte)
    wait(1)
    bus.write(m + 0x10, 4, (0x42 << 1) | 1)
    wait(2)
    bus.read(m + 0x14, 4)
    bus.read(m + 0x18, 4)
    out = []
    wait(0x40)
    out.append(bus.read(m + 0x10, 4))
    wait(4)
    bus.write(m, 4, bus.read(m, 4) & ~(1 << 10))                   # NACK the next byte
    out.append(bus.read(m + 0x10, 4))
    wait(4)
    bus.write(m, 4, bus.read(m, 4) | (1 << 9))
    out += [bus.read(m + 0x10, 4), bus.read(m + 0x10, 4)]
    s.run_for(3e6)
    # bytes 1 and 2 arrive back to back (RxNE then BTF on byte 3), byte 4 is NACKed: registers 04..07
    assert bytes(out) == b"\x4b\x32\xa6\xa7"
    # the slave's ISR fills DR one byte ahead (TxE after each byte moves to the shift register): 4 sent + 1
    assert s.uart_output("USART1").decode() == "SLAVE READY\r\nSLAVE RX 04:4B32\r\nSLAVE TX 05\r\n"
    s.close()


def test_stop_rtc_wakeup_sample_stop_standby_and_sbf():
    """firmware/samples_stm32/stop_rtc_wakeup.c: three Stop entries woken by the RTC wakeup timer every 0.5 s (exit on
    HSI with the PLL off), then Standby, whose RTC wake-up resets the chip with PWR_CSR.SBF/WUF set."""
    elf = BUILD / "stop_rtc_wakeup.elf"
    if not elf.exists():
        pytest.skip("not built (firmware/samples_stm32/build.sh)")
    s = Session(YAML, elf)
    s.run_for(2.2e9)
    out = s.uart_output("USART1").decode()
    assert out == ("boot\r\n" + "".join(f"stop {n} woke on HSI, PLL off\r\n" for n in (1, 2, 3))
                   + "standby\r\nstandby wake-up (SBF WUF)\r\n"), out
    pwr = s.chip.peripherals["PWR"]
    assert pwr.entries["stop"] >= 4 and pwr.entries["standby"] == 1
    exits = [e.t_ns for e in s.chip.log if e.kind == "info" and "exit Stop" in e.fields.get("msg", "")]
    assert [b - a for a, b in zip(exits, exits[1:])] == [pytest.approx(0.5e9, abs=1e3)] * 2
    rs = [(e.get("reason"), e.t_ns) for e in s.chip.log if e.kind == "reset"]
    assert rs[-1][0] == "standby_wakeup" and rs[-1][1] == pytest.approx(exits[-1] + 0.5e9, abs=1e3)
    assert not [e for e in s.chip.log if e.kind in ("warn", "fault")]
    s.close()
