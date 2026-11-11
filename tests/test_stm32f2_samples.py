"""End-to-end runs of the STM32F2 samples (firmware/samples_stm32f2/build/*.elf: the STM32F405 bare-metal samples
rebuilt for the Cortex-M3, the 168 MHz ones regenerated with the F2 120 MHz clock tree, plus clock120_uart and
cryp_hash) on the STM32F205/F207/F215/F217 chip descriptions, and the F207 boards."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32f2" / "build"
CHIPS = ROOT / "chips"
_D: dict = {}


def desc(fn="stm32f207zg"):
    if fn not in _D:
        _D[fn] = load_chip(CHIPS / f"{fn}.yaml")
    return _D[fn]


def elf(name: str) -> Path:
    p = BUILD / f"{name}.elf"
    if not p.exists():
        pytest.skip(f"{p} not built (firmware/samples_stm32f2/build.sh)")
    return p


def boot(name: str, fn="stm32f207zg", pin="PB0") -> tuple[Chip, list]:
    chip = Chip(desc(fn))
    chip.load_firmware(load_firmware(elf(name)))
    chip.reset()
    ev: list = []
    chip.pins.subscribe(lambda e: ev.append(e) if e.pin == pin and e.field == "level" else None)
    return chip, ev


def session(name: str, fn="stm32f207zg", **kw) -> Session:
    return Session(CHIPS / f"{fn}.yaml", elf(name), **kw)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def test_images_are_cortex_m3_with_81_vectors():
    from elftools.elf.elffile import ELFFile
    with open(elf("blinky"), "rb") as f:
        e = ELFFile(f)
        attrs = e.get_section_by_name(".ARM.attributes").data()
        vt = e.get_section_by_name(".isr_vector")
        assert vt.data_size == 4 * (16 + 81)
    assert b"cortex-m3" in attrs.lower() or b"7-M" in attrs


@pytest.mark.parametrize("fn", ["stm32f205rg", "stm32f207zg", "stm32f217igh"])
def test_blinky(fn):
    chip, ev = boot("blinky", fn)
    chip.run_for(5e6)
    assert chip.clock.hz == 16e6 and len(ev) > 10
    rises = [e.t_ns for e in ev if e.new == "H"]
    periods = [b - a for a, b in zip(rises[1:], rises[2:])]
    assert max(periods) - min(periods) < 1e3
    assert not warns(chip)


def test_clock120_uart_and_tim2_1ms():
    s = session("clock120_uart")
    pb0 = []
    s.chip.pins.subscribe(lambda e: pb0.append(e.t_ns) if e.pin == "PB0" and e.field == "level" else None)
    s.run_for(10e6)
    r = s.chip.peripherals["RCC"]
    assert (r.core_hz, r.apb1_hz, r.apb2_hz, s.chip.clock.hz) == (120e6, 30e6, 60e6, 120e6)
    assert s.chip.peripherals["FLASH"].latency == 3
    out = s.uart_output("USART1").decode()
    # IDCODE 0x20036411; the STM32F4-only PWR_CR.VOS and RCC_AHB1ENR.CCMDATARAMEN read back 0
    assert out.startswith("F2 120MHz id=20036411 pwr=00000000 ahb1=00000003\r\n"), out
    assert s.chip.peripherals["USART1"].baud() == pytest.approx(60e6 / 521, rel=1e-3)
    t = pb0[1:]
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert len(gaps) >= 4 and all(g == pytest.approx(1e6, rel=1e-3) for g in gaps)
    s.schedule_uart(s.chip.now_ns() + 0.1e6, "USART1", b"ok")
    s.run_for(1e6)
    assert s.uart_output("USART1").decode().endswith("ok")
    assert not warns(s.chip)
    s.close()


def test_tim_irq_generated_120mhz():
    chip, ev = boot("tim_irq")
    chip.run_for(10.5e6)
    assert chip.peripherals["RCC"].core_hz == 120e6 and chip.peripherals["RCC"].timer_hz(1) == 60e6
    t = [e.t_ns for e in ev[1:]]
    assert len(t) == 10
    assert all(b - a == pytest.approx(1e6, rel=1e-3) for a, b in zip(t, t[1:]))
    assert not warns(chip)


def test_uart_echo():
    s = session("uart_echo", "stm32f205rg")
    s.schedule_uart(1e6, "USART1", b"abc")
    s.run_for(5e6)
    assert s.uart_output("USART1") == b"hello\r\nabc"
    s.close()


def test_exti_irq():
    chip, ev = boot("exti_irq", "stm32f205vg")
    chip.run_for(0.1e6)
    for _ in range(3):
        chip.pins.set_external("PA0", "H")
        chip.run_for(0.2e6)
        chip.pins.set_external("PA0", "L")
        chip.run_for(0.2e6)
    assert chip.peripherals["EXTI"].pr_count[0] == 3 and [e.new for e in ev] == ["L", "H", "L", "H"]


def test_tim_pwm_1khz_25_percent():
    chip, ev = boot("tim_pwm", pin="PA6")
    chip.run_for(5.2e6)
    rises = [e.t_ns for e in ev if e.new == "H"]
    falls = [e.t_ns for e in ev if e.new == "L"]
    per = [b - a for a, b in zip(rises[1:], rises[2:])]     # the first period includes the start-up offset
    assert per and all(p == pytest.approx(1e6, abs=100) for p in per)
    hi = [min(f for f in falls if f > r) - r for r in rises[1:-1]]
    assert all(h == pytest.approx(0.25e6, abs=100) for h in hi)


def test_adc_tim_trigger():
    s = session("adc_tim_trigger")
    s.set_adc("PA0", 1.65)
    s.run_for(3.5e6)
    assert s.uart_output("USART1").decode().startswith("adc ready\r\nadc 2048\r\nadc 2048\r\n")
    s.close()


def test_uart_dma():
    s = session("uart_dma")
    s.schedule_uart(1.5e6, "USART1", b"qwertyui")
    s.run_for(3e6)
    out = s.uart_output("USART1").decode()
    assert out.startswith("DMA ready\r\nqwertyui\r\n"), out
    assert s.irq_counts.get(16 + 70, 0) or any(k == 70 or k == 86 for k in s.irq_counts)
    s.close()


def test_spi_flash_device():
    s = session("spi_flash")
    dev = s.attach_spi_flash("SPI1:cs=PA4")
    s.run_for(20e6)
    assert "TEXT CHIP_EM" in s.uart_output("USART1").decode() and dev.mem[0x100:0x107] == b"CHIP_EM"
    s.close()


def test_rtc_alarm_every_second():
    s = session("rtc_alarm", "stm32f205rg")
    s.run_for(3.5e9)
    out = s.uart_output("USART1").decode()
    assert out == ("cold boot\r\nRTC 2024-02-28 23:59:59\r\nRTC 2024-02-29 00:00:00\r\nRTC 2024-02-29 00:00:01\r\n"), out
    s.close()


def test_stop_rtc_wakeup_120mhz():
    s = session("stop_rtc_wakeup")
    s.run_for(2.2e9)
    out = s.uart_output("USART1").decode()
    assert out == ("boot\r\n" + "".join(f"stop {n} woke on HSI, PLL off\r\n" for n in (1, 2, 3))
                   + "standby\r\nstandby wake-up (SBF WUF)\r\n"), out
    exits = [e.t_ns for e in s.chip.log if e.kind == "info" and "exit Stop" in e.fields.get("msg", "")]
    assert [b - a for a, b in zip(exits, exits[1:])] == [pytest.approx(0.5e9, abs=1e3)] * 2
    assert not [e for e in s.chip.log if e.kind in ("warn", "fault")]
    s.close()


def test_sdio_card():
    s = session("sdio_card", "stm32f205zg", quiet=True)
    card = s.attach_sd_card("SDIO:size=64M")
    s.run_for(40e6)
    out = s.uart_output("USART1").decode()
    assert out.startswith("SD v2 SDHC") and out.endswith("DMA ok\r\ndone\r\n"), out
    assert card.bus_width == 4
    assert not warns(s.chip)
    s.close()


def test_usb_cdc_120mhz():
    s = session("usb_cdc")
    s.attach_usb_host("OTG_FS:debounce=20ms,reset=1ms,reset_recovery=1ms")
    br = s.attach_usb_cdc("memory")
    br.port.host_send("ping\r\n")
    for _ in range(80):
        s.run_for(1e6)
        br.pump()
    got = br.port.host_recv(timeout=1.0)
    for _ in range(10):
        if b"echo: ping" in got:
            break
        got += br.port.host_recv(timeout=0.2)
    assert got.startswith(b"STM32 CDC ready\r\n") and b"echo: ping\r\n" in got
    assert s.chip.peripherals["RCC"].pll_q_hz() == 48e6
    s.close()


def test_cryp_hash_f217():
    s = session("cryp_hash", "stm32f217zg")
    s.run_for(15e6)
    assert s.uart_output("USART1").decode() == (
        "aes 69C4E0D86A7B0430D8CDB78070B4C55A\r\nalgo1 0\r\nsha1 A9993E364706816ABA3E25717850C26C9CD0D89D\r\n"
        "md5 900150983CD24FB0D6963F7D28E17F72\r\ndone\r\n")
    s.close()


def test_cryp_hash_absent_on_f207():
    s = session("cryp_hash", "stm32f207zg")
    s.run_for(3e6)
    assert s.uart_output("USART1").decode() == "aes "
    # the inactive stub logs the unmodelled CRYP accesses (CPU accesses do not go through the stub's `warn` hook)
    assert any(e.kind == "mmio" and e.fields.get("block") == "CRYP" for e in s.chip.log)
    s.close()


# ------------------------------------------------------------------------------------------------ boards
def board(name):
    for p in ("uart_link_echo", "can_echo", "eth_ping"):
        pass
    return Session.from_board(ROOT / "boards" / f"{name}.yaml", quiet=True)


def test_board_k22_f207_uart():
    elf("uart_link_echo")
    if not (ROOT / "firmware/samples/build/uart_link_sender.elf").exists():
        pytest.skip("K22 samples not built")
    s = board("k22_stm32f207_uart")
    s.run_for(10e6)
    a = s.chips["A"].uart_out("UART0").decode()
    b = s.chips["B"].uart_out("USART1").decode()
    assert b.startswith("PING 1\r\nPING 2\r\n") and a.startswith("ping 1\r\n")
    s.close()


def test_board_k22_f207_can():
    elf("can_echo")
    if not (ROOT / "firmware/samples/build/can_echo.elf").exists():
        pytest.skip("K22 samples not built")
    s = board("k22_stm32f207_can")
    s.run_for(25e6)
    stm = [e for e in s.events(kinds={"can"}) if e.fields["chip"] == "B"]
    assert [(e.fields["id"], e.fields["data"]) for e in stm] == [(0x202, f"{n:04X}4B32") for n in range(2)]
    assert "RX 202 [4] 00 01 4B 32\r\n" in s.chips["A"].uart_out("UART0").decode()
    assert s.board.can_links[0].bus.error_frames == 0
    s.close()


def test_board_f207_eth_ping():
    elf("eth_ping")
    s = board("stm32f207_eth_ping")
    s.run_for(20e6)
    out = s.chips["A"].uart_out("USART1").decode() if hasattr(s, "chips") and "A" in s.chips else \
        s.uart_output("USART1").decode()
    assert out == "eth_ping\r\nPHY 0007:C131\r\nlink 100FD\r\ngarp\r\nping 1\r\nping 2\r\nping 3\r\nping 4\r\n", out
    s.close()
