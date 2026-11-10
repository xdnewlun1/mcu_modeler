# TI C2000 (C28x): status and roadmap

First chip: **TMS320F28232** (C2823x: fixed-point C28x, no FPU, 100 MHz, 64K-word flash, 26K-word SARAM, 176-pin
LQFP "PGF"). The CPU interpreter is `chipem/core/c28x` (see docs/C28X_CORE.md); everything around it is described
here. Sources: SPRS439Q data sheet (memory map, pin table, PIE table, peripheral frames) and, for register bit
fields, the TMS320x2833x/2823x System Control and Interrupts guide (SPRUFB0) and SCI guide (SPRUFZ5), reproduced
from knowledge: TI publishes no SVD for this family, so `chips/tms320f28232_regs.yaml` is hand-written in the
svd2chip schema. Check bit-level details against the TRMs before relying on them.

## Status

| piece | where | state |
|---|---|---|
| 16-bit address units | `periph/base.py`, `bus.py`, `chip.py`, `firmware.py` | done: `MMIOBlock`/`Reg` offsets and widths in units (`unit_bits` from the chip), `Bus(addr_unit_bits=16)`, `read_bytes`/`write_bytes` on the little-endian byte image, `FirmwareImage.unit_bytes`; ARM paths unchanged (same speed) |
| catch-all stub windows | `bus.py`, chip YAML `auto_stub_ranges` | done: per chip (inclusive bounds), Kinetis/ARM defaults when absent; stubs are clipped to their window |
| interrupt architecture | `arch/c28x/{arch,pie}.py` | done: PIE (PIECTRL/ENPIE/PIEVECT, PIEACK, PIEIER/IFR 1-12, vector RAM 0x0D00), INT13/14/NMI, CPU vector tables (0x3FFFC0 / 0x000000) when ENPIE=0, ITRAP → vector 19, reset (PC = image entry or 0x33FFF6, SP 0x400, ST1 0x0A0B), `vector_table()` |
| chip description | `chips/tms320f28232.yaml` | done: memory map, 176 pins with MUX1-3 functions, IRQ names by vector number, all peripheral frames |
| register table | `chips/tms320f28232_regs.yaml` | done (hand-written, 16-bit units) |
| models | `periph/ti_c2000/` | `sysctrl` (PLL/DIVSEL/HISPCP/LOSPCP, PCLKCR gates, watchdog), `gpio` (A/B/C mux, DIR, PUD, DAT/SET/CLEAR/TOGGLE, XINT select), `xint`, `cputimer`, `sci` (FIFO, interrupts, board links), `adc` (dual/cascaded sequencers, start/stop and continuous run, arbitration, software / ePWM / XINT2-pin starts, INT_MOD, simultaneous sampling, ADCCLK/ACQ_PS timing), `adcmirror`, `epwm` (ePWM1-6: TB incl. TBCTR > TBPRD and CTRMAX, CC, AQ, DB, chopper, TZ software and TZ1-6 pins, ET with INT and SOC, sync scheme with EPWMSYNCI/O pins and `sync_listeners` for eCAP1, EPWMxA/B pins), `ecap` (eCAP1-4: capture with prescaler, polarity, absolute/delta, continuous/one-shot, CTROVF, sync, APWM output, ECAPx_INT), `eqep` (eQEP1-2: quadrature / direction / up / down decoding with polarity, swap and index gating, position counter with PCRM modes, index markers, index / strobe latch and init, position compare with shadow, unit timer, watchdog, capture unit, EQEPx_INT), `spi` (minimal), `flash`, `csm`, `dma` (6 channels: SEQ1INT/SEQ2INT, XINT, TINT, ePWM SOCA/SOCB and software triggers, bursts / transfers / wrap with active and shadow pointers, 16/32-bit, ONESHOT, CONTINUOUS ping-pong, DINTCHn at transfer begin / end, overflow, ADCSYNC, round robin and CH1 high priority, HALT / SOFTRESET / HARDRESET, 1 + 4n cycle bursts, DMA bus access rules), `i2c` (I2C-A: pin-level master/slave on GPIO32/33, IPSC/ICCL/ICCH timing, I2CCNT/RM/STT/STP, XA/FDF/BC/DLB, NACKMOD, I2CISRC arbiter, 16-deep FIFOs, I2CINT1A/I2CINT2A), `ecan` (eCAN-A/B: SCC/eCAN modes, CCR/CCE handshake, CANBTC bit timing from SYSCLKOUT/2, 32 mailboxes with TPL priority, LAM/GAM acceptance masks, OPC/RML, DBO, remote frames with auto answer, TRR abort, STM self-test, power-down/WUBA, ABO, time stamps MOTS/TSC and time-outs MOTO/TOC/TOS, GIF0/GIF1 with MIL/GIL routing to ECAN0INTx/ECAN1INTx, CANES/CANTEC/CANREC, EALLOW-protected bits; on a `chipem.canbus` bus), `alias` (L0-L3 dual mapping), `storage` (HRPWM, McBSP, XINTF, device emulation) |
| loaders | `firmware.py` | `.c28img` (assembler JSON), TI COFF2 `.out` (sniffed against ELF), C2000 ELF (word addresses), raw `.bin` with `--load-addr` in words |
| analysis | `firmware.analyze` | boot-entry `LB` target, vectors present in the image, peripheral references from `MOVW/MOVZ DP` and `MOVL XARn,#22`, pin roles from direct `MOV/OR/AND @GPxMUX/GPxDIR,#imm` stores (ISA-table decode) |
| front ends | `cmd_run`, `cmd_shell`, `session` | run summary (SCI output, "PIE x.y" labels), shell `regs`, `--uart-in SCIA:...`, `--adc ADCINA0=...`, `--clock` freezes SYSCTRL |
| samples | `firmware/samples_c2000/`, `firmware/samples_c2000_c/` | assembler: `blinky`, `button_mirror`, `timer_irq`, `sci_echo`, `uart_link_echo`, `uart_link_sender`, `uart_chat`, `epwm`, `ecap` (ePWM1A measured by eCAP1, `boards/c2000_ecap_loopback.yaml`), `eqep` (a `quadrature_encoder` board part decoded by eQEP1 with index reset and a 100 Hz unit timer, `boards/c2000_eqep_encoder.yaml`); `build.sh` → `build/*.c28img`. TI-compiled C (cl2000 25.11.1, EABI and COFF): `blinky`, `uart_chat`, `selftest`, `epwm` (ePWM1 with EPWM1_INT and an ADC SOCA) |
| board | `boards/c2000_k22_uart.yaml`, `boards/c2000_stm32_uart.yaml` | K22 `uart_link_sender` ↔ F28232 `uart_link_echo` over UART0 ↔ SCI-A; F28232 `uart_link_sender` ↔ STM32 `uart_link_echo` over SCI-A ↔ USART1 (run, `--pin-report`, web UI) |
| tests | `tests/test_engine_units.py`, `tests/test_c2000_{chip,periph,samples,compiled}.py` | fake-core tests for the engine/arch/models; samples, COFF/ELF, TI-compiled C and the board on the real core |

## Design notes

**Address units.** All addresses, offsets and sizes are words. A width-2 register is 32 bits with its low word at
the lower address. `MMIOBlock` keeps a `bytearray` of `unit_bytes` per unit; `Reg` precomputes its byte offset/width
(`boff`/`bw`) so the hot path costs the same as before. Models get `unit_bits` from `params["unit_bits"]` (the Chip
injects the chip's `addr_unit_bits`) or the class attribute (`C2000Block.unit_bits = 16`).

**PIE flow.** A peripheral line's rising edge latches PIEIFRx.y. When PIEIFRx & PIEIERx is non-zero and PIEACKx is
clear, the PIE sets PIEACKx and the CPU IFR bit x−1. Between slices `service()` takes the highest-priority
IFR & IER interrupt if INTM is clear: it clears the IFR bit, and with ENPIE (and VMAP=1) picks the
highest-priority pending channel of the group, clears its flag, sets PIEVECT and fetches vector
n = 32 + 8(x−1) + (y−1) from 0x0D00 + 2n; then `core.take_interrupt(target, x)` saves the context. The ISR must
write 1 to PIEACK bit x−1, which re-raises INTx if another enabled flag is pending. Peripherals pulse lines that
have no flag of their own (CPU timers pulse on every expiry, as TI examples never clear TIF). A vector that holds
0 or 0x3FFFFF (or points outside memory) is reported as a `fault` event (`type: bad_vector`) and not taken.
The core calls `itrap_handler(pc)` on an illegal opcode and `vector_fetch(n)` for TRAP/INTR; both resolve through
the active table.

**IRQ names.** `irqs:` in the chip YAML are keyed by **vector number** (INTx.y = 32 + 8(x−1) + (y−1); 13/14 = INT13/14;
18 NMI; 19 ILLEGAL), because the chip loader keys IRQs by integer. `set_irq` accepts the YAML name, "x.y",
(x, y), "INT13"/"INT14"/"NMI" or the vector number. `irq_summary` counts by vector number.

**Boot.** There is no boot ROM. Reset jumps to the image entry (COFF/c28img) if any, else to `boot.entry`
(0x33FFF6, the boot-to-flash location that holds `LB _c_int00`), with the registers the boot ROM leaves:
SP = 0x400, ST1 = 0x0A0B (OBJMODE, M0M1MAP, VMAP, DBGM, INTM). `SYSCTRL` applies the boot ROM's DIVSEL = /2
(`params.boot_divsel`). `boot.rom_words` seeds erased OTP: `ADC_cal` at 0x380080 is a bare `LRETR` (so TI's
`InitSysCtrl` can call it) and PARTID at 0x380090. The BROM reset vector (0x3FFFC0) is seeded with the boot entry.

**Clocks.** OSCCLK is `SYSCTRL.params.ext_hz` (20 MHz in the F28232 YAML: TI's 100 MHz setup PLLCR = 10, DIVSEL = /2).
After reset SYSCLKOUT = 10 MHz. A PLLCR write relocks for 131072 OSCCLK cycles (6.55 ms) in bypass. The core runs
1 cycle per instruction, so CPU-timer and SCI timing follow SYSCLKOUT; the watchdog and PLL lock use OSCCLK
(wall-clock scheduler domain).

**EALLOW.** `C2000Block.eallow_regs` lists protected registers; a write without ST1.EALLOW is ignored and logged
once per block (`params.enforce_eallow: false` turns it off). Protected: all of SYSCTRL and the flash registers,
GPIO control and interrupt-select registers. Not enforced on the PIE vector table.

**Pins.** `GPIO{num}` ids. Port C: mux 1 is still GPIO, 2/3 the XINTF signal. SCI TXD outputs idle high when
muxed; other peripheral functions get direction `none`. GPIO0-11 reset without pull-ups (GPAPUD = 0x0FFF), so a
whole-port GPADAT read reports them floating (one `warn` per read that finds new ones).

## Limitations (F28232 support)

- Not modelled: input qualification (GPxCTRL/QSEL), low-power modes beyond IDLE, XINTF timing (zones are plain
  RAM), HRPWM, McBSP (register storage only), eCAN SUSP/TCOF (docs/LIMITATIONS.md "CAN"), I2C-A START byte / slave 10-bit / free data format, SPI devices (MISO reads 0xFFFF), flash programming (flash is writable by the CPU), CSM (always
  unsecured), missing-clock detection / limp mode.
- SCI: no framing/parity errors or break detection; FFTXDLY/autobaud are ignored. Bytes that arrive while the
  receiver is disabled, in reset, gated off or not muxed are dropped, and overruns set OE/RXERROR (or RXFFOVF), as on
  silicon (no input queue; see docs/LIMITATIONS.md).
- Peripheral clock gates only `warn` (silicon ignores accesses to an unclocked peripheral).
- SCI-C, McBSP-B, eCAP5/6 do not exist on the F28232 and are not instantiated, although the shared pin table lists
  their mux functions (GPIO62/63, GPIO48/49, ...).
- Python core speed: about 2-6 M instructions/s, i.e. 1/20 to 1/50 of real time at 100 MHz.

## Remaining work

1. ePWM remainder: HRPWM MEP; compare events while TBCTR is above TBPRD. FREE_SOFT (ePWM, eCAP, eQEP) and ADC
   SUSMOD need an emulation-suspend concept, which CHIP_EM does not have.
   eQEP remainder: the position-compare sync output pulse on EQEPxI/EQEPxS (SOEN/SPSEL/PCSPW/PCPOL).
2. Input qualification and GPIO low-power wake-up; XNMI routing details.
   DMA remainder: McBSP event triggers (needs a McBSP model), CPU/DMA access-conflict stalls and XINTF wait states,
   DEBUGCTRL.FREE, the CPU-side ePWM remap to Peripheral Frame 3 (MAPCNF.MAPEPWM).
3. Other parts: F28335 (FPU32, SCI-C, 34K SARAM) is a YAML away (same family package); F2837xD needs new
   sysctl/GPIO models (different register maps, GPxGMUX, input X-BAR) and a grid package renderer for BGA.
4. TI-tagged / Intel hex from `hex2000`.
5. Validation against real hardware traces (see docs/C28X_CORE.md for the interpreter's own validation status).
