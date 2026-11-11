"""End-to-end runs of the STM32F105/F107 connectivity-line samples (firmware/samples_stm32f1_cl/build/*.elf): the
PLL2 reference clock tree with USART1, Ethernet ping on the STM32F107VC (RMII, PHY clocked from MCO = PLL3), USB CDC on
the STM32F105RC OTG_FS, CAN1 <-> CAN2 on one chip and DMA2 memory-to-memory.  Skipped when the samples are not built
(firmware/samples_stm32f1_cl/build.sh)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.session import Session
from chipem.usbbus import UsbBus

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32f1_cl" / "build"
FAST = dict(debounce_ns=1e6, reset_ns=1e6, reset_recovery_ns=1e6, set_address_recovery_ns=0.5e6)


def elf(name: str) -> Path:
    p = BUILD / f"{name}.elf"
    if not p.exists():
        pytest.skip(f"{p} not built (firmware/samples_stm32f1_cl/build.sh)")
    return p


def sym(sample: str, name: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(elf(sample), "rb") as f:
        return ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(name)[0]["st_value"]


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def test_uart_echo_pll2_clock_tree():
    """RM0008 8.2: HSE 25 MHz -> PLL2 40 MHz -> PREDIV1 /5 -> PLL x9 = 72 MHz; DEV_ID 0x418 (31.6.1)."""
    s = Session(ROOT / "chips" / "stm32f105rc.yaml", str(elf("uart_echo")), quiet=True)
    s.run_for(7e6)
    assert s.chip.clock.hz == 72e6
    s.uart_input(b"ok", "USART1")
    s.run_for(1e6)
    out = s.uart_output("USART1")
    assert out.startswith(b"CL DEV 418 CR 0F030083 CFGR 001D840A CFGR2 00018644\r\n72 MHz\r\n"), out
    assert out.endswith(b"OK")
    assert not warns(s.chip)


def test_eth_ping_board():
    """F107 RMII, DP83848 clocked from MCO (PLL3 50 MHz): gratuitous ARP, ARP reply, 4 ICMP echo replies."""
    elf("eth_ping")
    s = Session.from_board(ROOT / "boards" / "stm32f107vc_eth_ping.yaml", quiet=True)
    s.run_for(16e6)
    out = s.uart_output("A.USART1")
    assert out == (b"eth_ping F107\r\nPHY 2000:5C90\r\nlink 100FD\r\ngarp\r\nping 1\r\nping 2\r\nping 3\r\nping 4\r\n"), out
    chip = s.chips["A"]
    eth = chip.peripherals["ETH"]
    st = eth.eth_stats()
    assert st["iface"] == "rmii" and st["tx"] == 5 and st["rx"] == 4
    assert chip.peripherals["RCC"].mco_hz() == 50e6
    assert chip.pins.get("PA1").owner == "ETH_RMII_REF_CLK" and chip.pins.get("PB11").owner == "ETH_RMII_TX_EN"


def test_usb_cdc_f105():
    """OTG FS on the F105 (VBUS sensing through PA9, OTGFSCLK = VCO / 3): enumeration, DTR banner, echo."""
    s = Session(ROOT / "chips" / "stm32f105rc.yaml", str(elf("usb_cdc")), quiet=True)
    bus = UsbBus("USB1")
    bus.attach(s.chip.peripherals["OTG_FS"])
    host = bus.add_host(**dict(FAST, debounce_ns=5e6))
    s.run_for(60e6)
    d = host.device
    assert d is not None and d.configured and d.product == "F105 bare-metal CDC"
    assert bytes(host.cdc_rx) == b"F105 CDC ready\r\n"
    host.cdc_write("hi\r\n")
    s.run_for(3e6)
    assert bytes(host.cdc_rx).endswith(b"echo: hi\r\n")
    assert s.chip.pins.get("PA11").owner == "OTG_FS_DM" and s.chip.pins.get("PA9").external == "H"   # VBUS from the host


def test_usb_cdc_needs_vbus():
    """28.6.2: no NOVBUSSENS on the F105: without VBUS on PA9 the B-session is never valid (no pull-up)."""
    s = Session(ROOT / "chips" / "stm32f105rc.yaml", str(elf("usb_cdc")), quiet=True)
    s.chip.pins.set_external("PA9", "L")
    otg = s.chip.peripherals["OTG_FS"]
    s.run_for(40e6)
    assert otg.usb_pullup(None) is None


def test_dual_can_board():
    """24.3.1 / 24.9.5: CAN1 (banks 0-13) and CAN2 (banks 14-27, CAN2SB = 14) on one bus."""
    elf("can_dual")
    s = Session.from_board(ROOT / "boards" / "stm32f105rc_dual_can.yaml", quiet=True)
    s.run_for(9e6)
    chip = s.chips["A"]
    out = s.uart_output("A.USART1")
    assert out.startswith(b"dual CAN, CAN2SB 14\r\nTX 1\r\nRX2 1\r\nRX1 1\r\nTX 2\r\n"), out
    rd = lambda n: chip.bus.read(sym("can_dual", n), 4)
    assert rd("rx1") >= 3 and rd("rx2") >= 3 and rd("bad") == 0


def test_dma2_m2m():
    """13.3 / 13.4 on DMA2 (5 channels, vectors 56/57): memory-to-memory copies with TC interrupts."""
    s = Session(ROOT / "chips" / "stm32f107vc.yaml", str(elf("dma2_m2m")), quiet=True)
    s.run_for(3e6)
    assert s.uart_output("USART1") == b"DMA2 ok 256\r\n"
    assert not warns(s.chip)
