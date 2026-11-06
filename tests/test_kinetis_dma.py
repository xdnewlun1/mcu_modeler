"""eDMA + DMAMUX model (memory-to-memory software start, UART RX-triggered transfers), DSPI and SPI flash."""
from __future__ import annotations

from chipem.chip import Chip
from chipem.chipdesc import ChipDescription, CoreDesc, MemoryDesc, PeripheralDesc

from engine_util import DATA, FLASH, RAM, build_image, word

DMA, DMAMUX, UART2, SPI0 = 0x40008000, 0x40021000, 0x4006C000, 0x4002C000
TCD0 = DMA + 0x1000
SRC, DST, RXBUF = DATA + 0x200, DATA + 0x300, DATA + 0x400
COUNTER = DATA + 0x80


def dchip(code, vectors=None):
    desc = ChipDescription(
        name="DTEST", core=CoreDesc(arch="armv7e-m", cpu="cortex-m4", fpu=True, clock_hz=1e6, num_irqs=48),
        family="kinetis",
        memory=[MemoryDesc("FLASH", FLASH, 0x10000, "flash"), MemoryDesc("RAM", RAM, 0x10000, "ram")],
        peripherals=[PeripheralDesc("DMA", "kinetis.dma", DMA, 0x2000),
                     PeripheralDesc("DMAMUX", "kinetis.dmamux", DMAMUX),
                     PeripheralDesc("UART2", "kinetis.uart", UART2, params={"index": 2}),
                     PeripheralDesc("SPI0", "kinetis.dspi", SPI0, params={"index": 0})],
        irqs={0: "DMA0", 1: "DMA1", 16: "DMA_Error", 26: "SPI0", 35: "UART2_RX_TX"}, pins=[])
    chip = Chip(desc)
    img, addrs = build_image(code, vectors=vectors)
    chip.load_firmware(img)
    chip.reset()
    return chip


def tcd(saddr, soff, attr, nbytes, slast, daddr, doff, citer, dlast, csr):
    """Assembly storing a TCD for channel 0 (CSR last, as drivers do)."""
    return f"""
  ldr r0, ={TCD0:#x}
  ldr r1, ={saddr:#x}
  str r1, [r0, #0]
  ldr r1, ={soff & 0xFFFF:#x}
  strh r1, [r0, #4]
  ldr r1, ={attr:#x}
  strh r1, [r0, #6]
  ldr r1, ={nbytes:#x}
  str r1, [r0, #8]
  ldr r1, ={slast & 0xFFFFFFFF:#x}
  str r1, [r0, #0xc]
  ldr r1, ={daddr:#x}
  str r1, [r0, #0x10]
  ldr r1, ={doff & 0xFFFF:#x}
  strh r1, [r0, #0x14]
  ldr r1, ={citer:#x}
  strh r1, [r0, #0x16]
  strh r1, [r0, #0x1e]
  ldr r1, ={dlast & 0xFFFFFFFF:#x}
  str r1, [r0, #0x18]
  ldr r1, ={csr:#x}
  strh r1, [r0, #0x1c]
"""


HANDLER = f"""
  ldr r0, ={DMA:#x}
  movs r1, #0
  strb r1, [r0, #0x1f]
  ldr r0, ={COUNTER:#x}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  bx lr
"""


def test_memory_to_memory_software_start():
    # 16 bytes in one minor loop, 32-bit reads -> 8-bit writes, INTMAJOR; started with SSRT
    code = {"reset": f"""
  ldr r0, =0xE000E100
  movs r1, #1
  str r1, [r0]
{tcd(SRC, 4, 0x0200, 16, -16, DST, 1, 1, -16, 0x2)}
  ldr r0, ={DMA:#x}
  movs r1, #0
  strb r1, [r0, #0x1d]
spin:
  b spin
""", "isr": HANDLER}
    chip = dchip(code, {16: "isr"})
    chip.bus.write_bytes(SRC, bytes(range(1, 17)))
    chip.run_cycles(2000)
    assert chip.bus.read_bytes(DST, 16) == bytes(range(1, 17))
    assert word(chip, COUNTER) == 1                      # DMA0 IRQ once, handler cleared INT via CINT
    dma = chip.peripherals["DMA"]
    t = dma.tcd(0)
    assert t[0] == SRC and t[5] == DST                   # SLAST/DLAST restored the start addresses
    assert t[9] & 0x80 and t[7] == 1                     # DONE, CITER reloaded from BITER
    assert dma.get("INT") == 0 and 16 not in chip.arch.asserted


def test_uart_rx_dma_ring_with_major_loop_irq():
    # UART2 RX -> DMAMUX source 6 -> channel 0: 1 byte per request into a 4-byte buffer, wraps via DLAST=-4
    code = {"reset": f"""
  ldr r0, =0xE000E100
  movs r1, #1
  str r1, [r0]
{tcd(UART2 + 7, 0, 0x0000, 1, 0, RXBUF, 1, 4, -4, 0x2)}
  ldr r0, ={DMAMUX:#x}
  movs r1, #0x86
  strb r1, [r0]
  ldr r0, ={DMA:#x}
  movs r1, #0
  strb r1, [r0, #0x1b]
  ldr r0, ={UART2:#x}
  movs r1, #0x20
  strb r1, [r0, #0xb]
  movs r1, #0x2c
  strb r1, [r0, #3]
spin:
  b spin
""", "isr": HANDLER}
    chip = dchip(code, {16: "isr"})
    chip.run_cycles(200)
    chip.peripherals["UART2"].feed(b"abcdef")
    chip.run_for(20e6)                                       # 6 characters at ~7.8 kBd
    assert chip.bus.read_bytes(RXBUF, 4) == b"efcd"          # 6 bytes into a 4-byte ring
    assert word(chip, COUNTER) == 1                          # one major loop completed
    assert chip.peripherals["UART2"].rx_pending() == 0
    assert 16 + 35 not in chip.arch.pending                  # RDMAS: no CPU RX interrupt
    assert chip.peripherals["DMA"].transfers == 6


def test_dma_config_error_sets_err():
    code = {"reset": f"""
{tcd(SRC + 1, 4, 0x0202, 8, 0, DST, 4, 1, 0, 0)}
  ldr r0, ={DMA:#x}
  movs r1, #0
  strb r1, [r0, #0x1d]
spin:
  b spin
"""}
    chip = dchip(code)
    chip.run_cycles(500)
    dma = chip.peripherals["DMA"]
    assert dma.get("ERR") & 1 and dma.get("ES") & (1 << 31)


def test_dspi_with_spi_flash():
    from chipem.devices.spi_flash import SpiNorFlash
    code = {"reset": "spin:\n  b spin\n"}
    chip = dchip(code)
    dev = SpiNorFlash(0x202015).attach(chip, "SPI0")       # CS from the PUSHR CONT bit
    dev.mem[0x100:0x104] = b"WXYZ"
    bus = chip.bus
    bus.write(SPI0, 4, 0x80000000)                          # MSTR, MDIS=0, HALT=0

    def xfer(data: bytes) -> bytes:
        out = []
        for i, b in enumerate(data):
            last = i == len(data) - 1
            bus.write(SPI0 + 0x34, 4, (0 if last else 1 << 31) | b)
            out.append(bus.read(SPI0 + 0x38, 4) & 0xFF)
        return bytes(out)

    assert xfer(b"\x9f\0\0\0")[1:] == bytes([0x20, 0x20, 0x15])
    assert xfer(b"\x03\x00\x01\x00\0\0\0\0")[4:] == b"WXYZ"
    xfer(b"\x06")
    xfer(b"\x02\x00\x02\x00\x12\x34")
    assert dev.mem[0x200:0x202] == b"\x12\x34" and dev.ops[-1] == ("program", 0x200)
    sr = bus.read(SPI0 + 0x2C, 4)
    assert sr & (1 << 30) and sr & (1 << 31)                # TXRXS, TCF
    bus.write(SPI0 + 0x34, 4, (1 << 27) | 0x05)             # EOQ stops the module
    assert not bus.read(SPI0 + 0x2C, 4) & (1 << 30)
    bus.write(SPI0 + 0x2C, 4, 1 << 28)                      # clear EOQF -> running again
    assert bus.read(SPI0 + 0x2C, 4) & (1 << 30)
