# Known limitations

This document lists everything known to be approximate or missing, taken from the code and from
[PLAN.md](PLAN.md). The model is good enough to see what firmware does with its pins, clocks, interrupts
and UARTs, and to follow its control flow. It is not cycle-accurate, and most peripherals beyond
clocks/GPIO/timers/UART are stubs.

## Timing model

- **One cycle per instruction.** Virtual time is the instruction count divided by the core clock. Flash wait
  states, pipeline refills on branches, multi-cycle instructions (`ldm/stm`, `udiv/sdiv`, VFP `vdiv/vsqrt`,
  `push/pop` of many registers) and bus wait states are not modelled. Busy-wait delays and bit-banged
  protocols run at a different speed than on silicon. In fast mode blinky's half period is 5000
  instructions = 0.239 ms at 20.97 MHz. The cycle count on real hardware depends on the flash and cache
  configuration.
- **The `instructions` line of the `chipem run` summary is really virtual cycles.** It includes time skipped
  while sleeping in WFI, so idle firmware shows hundreds of "M insn/s".
- **Exception entry and return cost zero cycles.** There is no 12-cycle entry latency, no late-arrival or
  tail-chaining optimisation timing, and no lazy-stacking cost.
- **Exceptions are taken between slices.** A peripheral or MMIO write that pends a preempting exception asks
  the core to stop right after the current instruction. This only works when that instruction is a plain
  single-register `str/strb/strh` outside an IT block. Otherwise the exception is taken at the end of the
  slice, up to `max_slice` = 2000 instructions later (`chipem run --max-slice N` lowers this). Scheduler
  events (timers, stimulus, UART pacing) always end a slice at their exact cycle, so timer interrupts are
  not late.
- **Fast-mode instruction counts depend on a Unicorn internal.** Exact counts for early stops come from
  reading Unicorn's private `emu_counter` through ctypes, located by calibration at import
  (`core/unicorn_cm.py`). If a Unicorn upgrade breaks the calibration, early stops are charged half the
  slice and handler slices shrink to 256 instructions. Timing becomes visibly approximate. `--precise`
  avoids the dependency and runs about 10x slower.
- **Clock switching is instantaneous.** MCG status bits (IREFST, CLKST, PLLST/LOCK0, OSCINIT0) mirror
  control writes immediately, with no FLL settle or PLL lock time. Clocks are ideal: no IRC trim, no
  jitter, and the external crystal is 8 MHz unless the MCG block's `params.ext_hz` says otherwise.
- **SysTick** counts at the core clock with `CSR.CLKSOURCE=1` and at core / `systick_ext_div` with
  `CLKSOURCE=0` (chip YAML, default 8: the STM32's HCLK/8). The K22 has no external reference (K22P100M120SF5V2RM
  §3.2.1.2 "System Tick Timer": the clock is always FCLK, CLKSOURCE always reads 1, SYST_CALIB NOREF=1 and
  TENMS=0); its YAML sets `systick_ext_div: 0`, which models exactly that. For chips with a reference,
  `SYST_CALIB` (TENMS) is computed from the clock when the chip is built, not when it changes.
- **Wall-clock speed** varies with what the firmware does. Expect about 100–850 M virtual cycles/s while
  sleeping or in pure computation, about 3–10 M/s in loops that hammer modelled MMIO (each access is a
  Python call), and about 10 M/s in `--precise` mode. Code that polls a peripheral every few instructions
  is the slow case. Before the SPI model existed, the Withings image dropped from 140 M/s to 9 M/s once
  it started polling SPI0.

## Core and exception model

- **Eager FP stacking.** When CONTROL.FPCA is set, the 26-word frame (s0–s15, FPSCR) is written at
  exception entry. `FPCCR.LSPEN` lazy stacking and `FPCAR` are not modelled (FPCCR.ASPEN is honoured).
  Frames are architecturally identical, so software cannot tell the difference except through FPCAR.
- **All faults escalate to HardFault.** CFSR/HFSR/BFAR bits are set, but MemManage, BusFault and
  UsageFault handlers are never entered separately, whatever SHCSR enables. A fault in HardFault or NMI,
  or with FAULTMASK set, is a lockup: the run stops with exit status 3.
- **No MPU.** The MPU registers at 0xE000ED90 and the Kinetis system MPU (0x4000D000) are storage only.
- **No debug blocks.** DWT, ITM, FPB and TPIU are RAM-like auto-stubs. `DWT_CYCCNT` does not count, so
  delays built on it never finish (or finish at once, depending on the code). ITM stimulus writes are not
  captured. There is no GDB stub and no semihosting. `bkpt` stops the run with `BREAKPOINT`.
- **Sleep.** WFI and WFE both sleep until an enabled exception could preempt (WFE has no event register;
  SEV is a no-op). `SCR.SLEEPDEEP` and `SLEEPONEXIT` are stored but ignored.
- **NVIC priority bits** are fixed at 4 for ARMv7-M and 2 for ARMv6-M, regardless of the part.
- **NVIC inputs** are level lines with a latched pending bit, as on silicon: asserting a line pends the IRQ,
  deasserting it does not clear a pending state that was already latched (only taking the exception or an
  `ICPR` write does). A peripheral flag cleared before its IRQ was serviced therefore still causes one handler
  entry, so firmware that enables an IRQ late should clear its pending bit first (CMSIS
  `NVIC_ClearPendingIRQ`). While the line stays asserted the IRQ re-pends on handler return.
- `AIRCR.VECTCLRACTIVE` clears the active list. `VECTRESET` and `SYSRESETREQ` both perform a full chip
  reset.

## Memory

- **Flash is writable by ordinary stores.** Real silicon would bus-fault. The FTFE model also writes flash
  directly. Firmware that relies on a store to flash faulting will behave differently.
- FlexNVM and FlexRAM are plain memory. FlexRAM EEPROM emulation (EEE) is not modelled: `PGMPART` and
  `SETRAM` are acknowledged only.
- No flash cache, prefetch buffer or FMC behaviour (FMC is a stub).
- Unmapped addresses outside the two catch-all windows (0x40000000–0x400FFFFF, 0xE0000000–0xE00FFFFF)
  bus-fault. Inside them, the first access creates a RAM-like 4 KB stub.

## Peripherals

**Stubs.** USB0, USBDCD, I2S0, CMT, SDHC, FlexBus, AXBS, AIPS, MPU, FMC and MCM have no
behaviour. They read back what was written and start at 0, not at the SVD reset value. Firmware that waits for a
status bit set by hardware loops forever. The chip YAML's `model:` fields are the authoritative
list. FTM0–3 (`kinetis.ftm`), I2C0–2 (`kinetis.i2c`), CAN0 (`kinetis.flexcan`), CRC, DAC0, CMP0–2, VREF and EWM are modelled (below).

**Analog values.** There is no analog level on the pin board: dedicated analog pins only change owner (the DAC0_OUT
and VREF_OUT pins show owner `DAC0_OUT` / `VREF_OUT`, direction analog, while those outputs are on). Analog sources
are wired model to model: DAC0 → CMP1 IN3 and ADC0_SE23; VREF_OUT → DAC0 DACREF_1, the CMP 6-bit DAC Vin1,
CMP0/CMP1 IN5 and ADC1_SE18 (and the ADC VALT reference). External comparator inputs are set with
`--adc CMPx_INn=<millivolts>` (Session values are integers) or `CMP.set_input(n, volts)`, and are independent of
the ADC channel values set on the same pins (`ADC0_SE4b` and `CMP1_IN0` are the same pin: set both). ADC
`--adc` values are raw counts.

**FTM (`kinetis.ftm`)** models the counter, output compare, EPWM/CPWM, combine with dead time and the output path
onto FTMx_CHn pins; input capture and dual edge capture (one-shot and continuous, with the C(n)V/C(n+1)V read
coherency) from FTMx_CHn pin edges (`--at`, board nets, other outputs), fault control (FTMx_FLTn pins, FLTPOL,
manual and automatic clearing, safe values), the quadrature decoder (FTM1/FTM2 QD_PHA/PHB pins, both encodings), the
external clock (FTM_CLKIN0/1 via SIM_SOPT4), write protection (WPEN), the EXTTRIG channel and initialization
triggers into PDB0 (TRGSEL 8–11) and channel DMA requests (DMAMUX sources 20–39, CHnF cleared by the DMA). Input
edges take effect 3 system clocks after the pin edge (the synchronizer). Approximations and gaps: the input filters
(FILTER, FLTCTRL.FFVAL, QDCTRL.PHxFLTREN) are ignored as if disabled; the external clock and quadrature inputs are
not synchronized and the "at most system clock / 4" input rate is not checked; CMP / USB SOF sources selected in
SIM_SOPT4 (fault inputs, FTM1/FTM2 channel 0) give no edges. The trigger output also reaches ADC0/ADC1 through
SIM_SOPT7 (ADCnALTTRGEN=1, ADCnTRGSEL 8–11). Not modelled: INVCTRL, the hardware sync triggers (TRIG0–2) and most
SYNCONF options (software sync and LDOK work), capture test mode, the global time base and debug mode.

Notes on the modelled peripherals:

| model | what is missing or approximate |
|---|---|
| SIM | Clock-gate violations log one `warn` per peripheral; silicon bus-faults. Only the gated models check (PORT, ADC, FTFE, FTM, PDB, CRC, DAC0, CMP, VREF, EWM and others built on `GatedBlock`). |
| MCG/OSC | No lock/settle delays; no loss-of-clock or loss-of-lock monitors; IRC trim registers stored only. |
| PORT | Digital filter, slew rate and drive strength do not affect pin levels. ODE matters only for I2C pins (without it an enabled I2C module drives the released line H, push-pull); a GPIO output with ODE driving H still shows H. IRQC DMA-request modes only set ISF. |
| I2C0–2 | Pin-level bus (SDA/SCL toggle at the F register's rate, Table 48-54, 50 % duty; START/STOP/ACK, clock stretching and arbitration on board nets). Not modelled: 10-bit slave addresses (C2.ADEXT; a 10-bit master just sends the bytes), SMBus (SMB timeouts SLTF/SHTF, FACK = 1 behaves as FACK = 0, ALERTEN, PEC), A2 matching only with SIICAEN, the glitch filter (FLT.FLT stored), high drive (HDRS), SBRC, wake-up (WUEN) and stop holdoff (SHEN). Edges are instantaneous (no rise time); a slave answers at the SCL edge. |
| GPIO | No electrical contention: an output always wins over external stimulus. Floating inputs read 0 (one `warn`). |
| WDOG | The unlock window lasts at least 4096 core cycles (silicon: 256 bus clocks). Window violations and writes outside the unlock window log a `warn` instead of resetting. Test mode is not modelled. |
| PIT | Lifetime timer (LTMR64) not modelled. Each channel timeout is a trigger for PDB0 (TRGSEL 4–7) and the ADC alternate trigger; DMAMUX periodic triggering is not modelled. |
| LPTMR | Pulse-counter mode (TMS=1) is not supported (logged once). MCGIRCLK is fixed at 32.768 kHz and OSCERCLK at 8 MHz. |
| UART | The TX pin does not toggle: it shows its idle level (output, H) and RX is an input (level from the pull or the net, else `X`). RX behaves like silicon: no input queue; a byte arriving with C2.RE clear, the SIM gate off or no pin muxed to UARTn_RX is dropped, and one arriving while RDRF/OR is set is lost with S1.OR (OR & C3.ORIE drives UARTn_ERR). No parity, framing or noise errors, no break, LIN, IrDA or ISO-7816. TX/RX buffers follow RM ch. 49: UART0/1 have 8-entry FIFOs (`fifo_depth: 8` in the chip YAML; PFIFO reports the size), UART2–4 one entry; the effective depth is 1 until PFIFO.TXFE/RXFE is set. TDRE/RDRF follow the TWFIFO/RWFIFO watermarks, a byte arriving with the buffer full is lost with OR and SFIFO.RXOF, a write to a full transmit buffer sets TXOF, reading D from an empty buffer sets RXUF (silicon's resulting FIFO-pointer misalignment is not modelled), CFIFO flushes and the RXUFE/TXOFE/RXOFE error interrupts work. The transmitter is double buffered (buffer + shift register) and setting C2.TE shifts out a one-character idle preamble first. DMA requests (C5.RDMAS/TDMAS) are supported. Baud pacing is modelled, so TDRE/TC/RDRF timing follows the configured baud rate; UART0/1 run from the core clock, UART2–5 from the bus clock. |
| ADC | Hardware triggers (SC2.ADTRG) come from the PDB pre-triggers (SIM_SOPT7 ADCnALTTRGEN=0) or from the alternate source in ADCnTRGSEL (ALTTRGEN=1; ADCnPRETRGSEL picks SC1A/SC1B): PDB0_EXTRG pin rising edge, CMP0–2 output rising edge, FTM0–3 trigger, PIT channel 0–3 timeout, RTC alarm / seconds, LPTMR0 compare (all sources of Table 3-51; the SIM logs one `info` only for reserved TRGSEL 15 or a source missing from the chip). A trigger during a conversion restarts it. With ADTRG=1 an SC1n write just selects the channel. Calibration completes instantly. Conversion time is fixed (`params.conv_ns`, default 4 µs). Values come from `--adc`/`set_channel`, not from pin levels; unset channels read 0. ADC0_SE23 (DAC0 output) and ADC1_SE18 (VREF_OUT) convert the source's voltage with VREFH = 3.3 V (`params.vrefh`) or, with SC2.REFSEL=01, VALT = VREF_OUT. The temperature sensor, bandgap (AD27) and VREFH/VREFL channels are not wired. |
| PDB | The counter starts on the software trigger (TRGSEL=15) or any Table 3-51 input: PDB0_EXTRG pin, CMP0–2 output, PIT channel 0–3, FTM0–3 trigger, RTC alarm / seconds, LPTMR0 (a source missing from the chip is logged once and never fires). Pre-triggers drive ADC0/ADC1 SC1A/SC1B (pre-triggers 2–7 ignored); back-to-back chaining follows ADC completion (ch0 pre-trigger 0 chains from ADC1 SC1B). LDMOD=1/2/3 load buffered values at the next wrap or trigger (no distinction). Sequence errors flag when the target COCO is still set, but the conversion still starts. DAC interval trigger 0 drives DAC0's hardware trigger (TOE: every DACINT0+1 PDB clocks from each counter start, up to MOD; EXT: on ADC0 SC1A completion); interval trigger 1 (DAC1, absent) and pulse-out (POEN/POnDLY, the CMP sample/window signal) are storage only. One-shot mode leaves CNT at 0. |
| DMA/DMAMUX | Transfers complete instantly (zero bus time); no channel arbitration, priorities or bandwidth control. DMAMUX periodic triggering (PIT TRIG) is not modelled (logged once; the channel behaves as untriggered). Only peripherals that call `dma_request` (e.g. UART, ADC, DSPI) can request DMA. |
| DSPI (SPI0–2) | Master mode only. Frames transfer instantly, with no baud rate or CS-to-SCK delays and no continuous SCK. MISO reads all ones unless a responder is attached: `--spi-flash` (SPI NOR flash device) or a `responder(tx, pcs, bits)` callback in Python. The SPI pins do not toggle: SCK/SOUT show output L (CPOL is not looked at), PCS output H, SIN an input. |
| RTC | Driven by virtual time. Keeps state across chip resets (VBAT domain) but not across a new Session. TCR compensation stored only. |
| FTFE | Commands complete synchronously (CCIF never reads 0, no erase/program time). Program-only-clears-bits is honoured. Security, backdoor key and protection registers are loaded from the flash configuration field, but protection is not enforced on writes. |
| SMC/PMC/LLWU | Power modes are not modelled: `SMC_PMCTRL` STOPM is logged and stored, PMSTAT stays RUN, WFI is plain sleep. LLS/VLLS entry, VLLS wake-up through reset, and LLWU wake-up flags (always 0) are missing. |
| RCM | Reports the last reset cause from the `reset` event reason (POR, watchdog, lockup, SYSRESETREQ). |
| VBAT | Plain RAM that survives chip reset. |
| CRC | Results are immediate (silicon: 2 clocks per byte). Data writes are transposed per TOT within the bytes the access covers and fed MSB first; seed writes (WAS=1) are not transposed (32.3.2.1 puts a 16-bit seed in DATA[LU:LL] whatever TOT is). GPOLY[31:16] ignores writes while TCRC=0, as 32.2.2 says: drivers that write GPOLY before setting TCRC=1 get a 16-bit polynomial. In 16-bit mode DATA[31:16] keeps its reset/32-bit-mode value (RM: indeterminate). |
| DAC0 | Output level is `voltage()` plus one `info` event per change (no analog pin level); settling time, LPEN and the output drive are not modelled. The hardware trigger (DACTRGSEL=0) comes from the PDB0 DAC interval trigger 0. RP written above DACBFUP is clamped to DACBFUP. 16/32-bit accesses to SR/C0–C2 (forbidden by the RM) act bytewise. Also mapped at 0x4003F000 (`DAC0_ALIAS`). |
| CMP0–2 | No propagation delay; PMODE stored only. Hysteresis 5/10/20/30 mV (HYSTCTR 0–3, data sheet typical). Filtered mode with the bus clock delays a change by FILTER_CNT × FILT_PER bus clocks (worst-case latency, no sample phase). Sampled (SE=1) and windowed (WE=1) modes need the PDB pulse-out, which is not generated: COUT holds (one `warn`). CMP outputs do not reach the FTM fault / capture inputs (SIM_SOPT4) or the UART RX inputs (SIM_SOPT5). Bandgap input IN6 is a fixed 1.0 V. MUXCR.PSTM is stored only. |
| VREF | VREFST after a fixed 35 µs (`params.startup_ns`); the buffer start-up after a MODE_LV change, the chop oscillator, REGEN and ICOMPEN are not modelled. Output 1.2 V + 0.5 mV per TRIM step from the reset trim. |
| EWM | Write-once violations (CTRL EWMEN/ASSIN/INEN, CMPL, CMPH) are ignored with one `warn` (silicon: bus transfer error). The LPO is an exact 1 kHz. Stop/Wait counter freeze is not modelled. A service exactly at counter == CMPL is legal. |

**K22 checks against the reference manual** (K22P100M120SF5V2RM Rev. 5, which covers the "A" mask parts
MK22FN1M0AVLL12/MK22FX512AVLL12; our YAMLs model the non-A parts from data sheet K22P100M120SF5 Rev 4):

- Confirmed: WDOG configuration time 256 bus clocks and unlock within 20 bus clocks (§24.3.1, §24.3.2);
  LPTMR reset only on POR/LVD (§40.4.1, §6.2.3.3); FEI reset clock 32.768 kHz × 640 = 20.97 MHz (§25.3.4 DCO
  range table, §25.5.2); UART0/1 on the system clock, UART2–4 on the bus clock (Table 5-2); boot only from internal
  flash, no ROM bootloader (§6.3.1).
- Corrected: DMAMUX sources 11 and 46 are Reserved (Table 3-25), so `kinetis.dma.SOURCES` no longer lists
  UART5/DAC1.
- Removed: UART5 and DAC1 are not on the 100-pin part (0x400EB000 is unassigned, Table 4-3 in §4.5.3; IRQs
  68/69/72 reserved in Table 3-5; no UART5 or DAC1 pins in the data sheet). The MK22F12 SVD has them because it
  also covers larger packages. The chip YAML lists them under `omitted:` (with the reason), names IRQs 68/69/72
  `Reserved84/85/88`, and `svd2chip --check` reports them as OMITTED. Accesses to 0x400CD000/0x400EB000 fall into
  the auto-stub window (silicon would bus-fault).

## STM32F405ZG

- **No flash alias at 0x00000000.** The boot alias is not mapped; `vtor: 0x08000000` makes the core fetch the
  vector table from flash. Code that reads or jumps through address 0 (or relies on the alias for a system
  memory/SRAM boot) bus-faults.
- **USART** is single-buffered like silicon: a byte arriving while RXNE is set sets ORE and is lost; bytes arriving
  with UE/RE clear, the RCC enable off, the RCC reset bit set or no pin in AF mode for USARTn_RX are dropped; no
  parity/framing/noise errors, no LIN, smartcard, IrDA or hardware flow control. CR3.DMAT/DMAR DMA requests are
  generated (TXE/RXNE).
- **DMA1/DMA2** (`stm32.dma`, RM0090 ch. 10): 8 streams with the Table 42/43 request mapping (CHSEL), P2M/M2P/M2M
  (M2M on DMA2 only; DMA1 M2M is a transfer error), PINC/MINC/PINCOS, PSIZE/MSIZE, direct and FIFO mode (packing and
  unpacking with the same data as silicon; memory written at the FTH threshold, at the end and on disable), circular,
  double buffer (CT/M1AR), PL arbitration, NDTR, EN protection and abort, TCIF/HTIF/TEIF/FEIF with LIFCR/HIFCR and the
  per-stream IRQs, TEIF on bus errors and CCM RAM accesses. Transfers take zero bus time (run right after the access
  that triggered them), so DMEIF and FIFO overrun/underrun FEIF never occur; burst beats, bus-matrix contention and
  AHB/APB latencies are not modelled; PFCTRL (peripheral flow control) is not modelled (one `warn`, NDTR is used).
  The USART, TIM (UP/CHx/TRIG), ADC, SPI (SPIx_TX/RX) and DAC (DAC1/DAC2) models raise DMA requests
  (`_util.dma_request`); so does I2C (I2Cx_TX/RX, LAST), SDIO is a stub whose requests stay silent. On silicon
  DMA1's peripheral port reaches APB1 only (it is not on the bus matrix); here every DMA1 port can address
  anything, and the AHB1ENR gate only warns on register access.
- **Timers** (TIM1–14, one `stm32.tim` model; TIM9–14 get 1–2 channels, up-counting and 16 bits from their SVD
  registers) model the time base, the update interrupt, output compare and PWM1/PWM2 driving TIMx_CHn / TIMx_CHnN
  pins (TIM1/8 complementary outputs, MOE/OSSI/OSSR idle states), input capture from pin edges (CCxS 01/10, edge
  polarity, ICxPSC, CCxOF, CCRx read clears CCxIF, CCxS=11 TRC), CR2.TI1S (CH1 xor CH2 xor CH3), the encoder
  interface (SMS 001–011), TIM1/8 dead time (BDTR.DTG with CR1.CKD) and break (BKIN pin with BKE/BKP, EGR.BG,
  BIF/BIE, AOE). Master/slave synchronisation: CR2.MMS drives TRGO (reset/UG, enable, update, compare pulse,
  OC1REF–OC4REF); the slave mode controller takes TRGI from ITR0–3 (RM0090 Tables 86/93/97, e.g. TIM2 ITR0–3 =
  TIM1/TIM8/TIM3/TIM4; TIM9/12 ITR2/3 = OC1REF of TIM10/11, TIM13/14), TI1F_ED, TI1FP1, TI2FP2 or ETRF and runs
  reset, gated, trigger or external clock mode 1 (with TIF/TIE); SMCR.ECE external clock mode 2 counts the TIMx_ETR pin
  (ETP, ETPS). DMA requests TIMx_UP / TIMx_CHn (CCDS) / TIMx_TRIG go to `stm32.dma`. Shared vectors (TIM1_UP_TIM10,
  ...) are ORed. Captures latch CNT at the instruction-exact edge time with no input resynchronisation delay; TRGO
  reaches its slaves (and the ADC) in the same core cycle (MSM has no effect); a TI, ETR or BKIN input with no pin
  muxed to it sees nothing (an unmuxed BKIN is inactive). Not modelled: the ICxF and ETF input filters (no-op),
  center-aligned counting with an external clock (counts edge-aligned), OCxCE/ETR clear, OCxFE (no-op), compare
  flags while in encoder mode, dead time on the break transition, the CCPC/COM preload, BDTR.LOCK write protection
  (stored only), the TIMx_OR input remaps (TIM2 ITR1 is always TIM8_TRGO) and the DMA burst registers DCR/DMAR.
- **ADC1–3** (`stm32.adc`, RM0090 ch. 13) model the regular group: ADON, SWSTART, SQR1.L/SQ1–16 with SCAN, CONT,
  DISCEN/DISCNUM, SMPx sample times, RES 12/10/8/6, ALIGN, EOC with EOCS (DR read clears it), OVR (only with DMA=1
  or EOCS=1, RM0090 13.8.1: stops conversions until cleared), the analog watchdog (AWDEN, AWDSGL/AWDCH, HTR/LTR),
  EOCIE/AWDIE/OVRIE on the shared `ADC` vector, the DMA request "ADCn" per conversion (DDS stored only: after the
  DMA's last transfer nobody reads DR and the next conversion overruns, as with DDS=0 on silicon) and the external
  triggers of Table 51 (EXTEN edges; TIMx_CCy = OCyREF, so the channel must be in a PWM/toggle mode, or a capture).
  Timing: ADCCLK = PCLK2 / ADCPRE; a conversion takes (SMPx + RES) ADCCLK, the first one after a trigger 2 ADCCLK
  more (DS8626 tlatr max) plus 1 PCLK2 cycle for an external trigger; ADCCLK phase, tSTAB and clock changes during
  a conversion are not modelled. Values: `--adc PA0=1.65V` / `ADC1_IN0=1.65` (volts; a plain integer is a raw 12-bit
  code), stored per pin (PA0 feeds IN0 of ADC1, 2 and 3); a channel with no value reads its pin's digital level
  (H = 3.3 V, L/X = 0 V); IN16 temperature (25 °C, `ADC1_TEMP=<degC>`), IN17 VREFINT 1.21 V, IN18 VBAT/2 (`VBAT=3.3`)
  with CCR.TSVREFE/VBATE. Not modelled: injected channels (JSQR/JOFRx/JDRx/JSWSTART/JEXTEN, one `warn`), multi-ADC
  modes (CCR.MULTI, one `warn`; CDR reads 0), the EXTI11 trigger (one `warn`), CCR.DELAY, source impedance and
  sampling-capacitor effects.
- **SPI1–3** (`stm32.spi`, RM0090 ch. 28, SPI mode) are bit-level on the pins: a master toggles SCK (CPOL idle,
  2^(BR+1) PCLK per bit, SPI1 on APB2, SPI2/3 on APB1), MOSI (CPHA edges, MSB/LSB first, 8/16 bits) and, with
  SSOE=1 and SSM=0, NSS (low while SPE=1), samples MISO from its pin (H = 1, L/X = 0) or from a responder
  (`--spi-flash SPI1:cs=PA4`, `responder(tx, pcs, bits)`); a slave (MSTR=0) shifts on the SCK pin edges while NSS
  (pin, or SSI with SSM=1) is low and drives MISO only while selected, so two STM32s talk over board nets
  (`boards/stm32_spi_pair.yaml`; keep the board quantum well below half an SCK period). Double buffering, TXE/RXNE/
  BSY, OVR (DR then SR read clears), MODF (NSS low in master, SSM=1/SSI=0), CRCEN/CRCNEXT/CRCERR, RXONLY and
  BIDIMODE master continuous clocking, IRQ and DMA requests are modelled. The TX buffer moves to the shift register
  at once (no PCLK synchronisation delay) and back-to-back frames have no gap. A master frame with SCK/MOSI not
  muxed still runs (flags/timing) but no device sees it (one `warn`). SCK/MOSI/NSS are released (float) while SPE=0.
  A slave with an empty TX buffer resends its previous frame. Not modelled: I2S mode (one `warn`, idle), TI frame
  format (one `warn`, Motorola timing), slave BIDIMODE, slave CRC, pin speed. The Kinetis DSPI stays frame-level.
- **CRC** (`stm32.crc`): 32-bit words fed MSB first with polynomial 0x04C11DB7 (CRC-32/MPEG-2 over the word
  stream; 0x12345678 → 0xDF8A8A2B), CR.RESET, IDR. Instantaneous. 8/16-bit DR writes are fed zero-extended with one
  `warn` (the F4 unit has no byte enables).
- **RNG** (`stm32.rng`): a new value 40 PLL48CLK periods after RNGEN and after each DR read (PLL48CLK = the main PLL's
  Q output, 0 while PLLON=0); clock error (CECS/CEIS) when it is stopped or below HCLK/16. Values are deterministic
  (seeded PRNG, `params.seed`) and never repeat consecutively; seed errors only through `inject_seed_error()`. The
  HASH_RNG vector is shared with the (stub) HASH.
- **DAC** (`stm32.dac`): two channels, DHR→DOR after 1 APB1 cycle (TEN=0) or 3 after a TIM6/8/7/5/2/4 TRGO trigger
  (1 after SWTRIG), DHR8/12 R/L and dual registers, noise/triangle waves (LFSR taps after the RM figure, not checked
  against silicon sequences), DMA requests with underrun (DMAUDR, the shared TIM6_DAC vector). The output is
  `voltage()` + an `info` per change; PA4/PA5 show owner DAC_OUT1/2 in analog mode and the voltage is the pin's
  analog value, so ADC channel 4/5 on that pin converts it (no internal DAC→ADC path exists on the F4). Not
  modelled: the EXTI9 trigger (one `warn`), the output buffer (BOFF stored), settling time.
- **WWDG** (`stm32.wwdg`): 7-bit counter at PCLK1/4096/2^WDGTB (prescaler phase starts at the last CR write),
  reset at 0x40→0x3F, on a refresh above W and on a CR write with T6=0 (RCC_CSR.WWDGRSTF), EWIF at 0x40 with the
  WWDG IRQ (EWI). Counts while the APB1 clock is on, also before WDGA (wrapping). DBGMCU freeze is not modelled.
- **RTC** (`stm32.rtc`): BCD calendar (TR/DR/SSR, leap years, weekday, 12 h format, ADD1H/SUB1H) from LSE/LSI/
  HSE÷RTCPRE with PRER prescalers, shadow registers (TR/SSR read locks DR; RSF set 2 RTCCLK after it is cleared;
  BYPSHAD), WPR key protection, PWR_CR.DBP enforcement, INIT mode (INITF at once, not after 2 RTCCLK), alarms A/B
  with MSK1–4 and WDSEL on EXTI line 17, the wakeup timer (WUCKSEL, WUTR) on EXTI line 22; the backup domain
  (calendar, registers, BKP0R–19R) survives system resets and is cleared by RCC_BDCR.BDRST. RCC_BDCR changes are
  seen at the next RTC access or event. Not modelled: sub-second alarm compare (MASKSS≠0: one `warn`), SHIFTR,
  calibration (CALIBR/CALR stored), timestamp, tamper, RTC_AF1 outputs, REFCKON. RCC_BDCR itself ignores DBP.
- RCC clock switches and PLL lock complete instantly; clock gates only `warn` on access to a gated-off block.
- CAN, SDIO, USB and FSMC are RAM-like stubs.
- **I2C1–3** (`stm32.i2c`) is a pin-level bus. Not modelled: 10-bit slave addressing (OAR1.ADDMODE = 1 never
  matches), SMBus (SMBUS/PEC/ALERT/ARP/TIMEOUT), BERR, analog/digital filters, rise time (TRISE stored only; edges are
  instantaneous). OTYPER is not checked: I2C pins are always open drain. CR2.FREQ is only checked (one `warn` if it
  differs from PCLK1); the timing comes from the real PCLK1. A START requested while another master holds the bus
  waits for its STOP (no ARLO); arbitration during a transfer is detected bit by bit (ARLO). DMA LAST uses the RX
  stream's NDTR when the ACK bit is driven.
- **What ST's HAL meets** (`firmware/samples_stm32_hal`, STM32CubeF4 HAL 1.8.5): HSE/PLL ready flags follow their
  enable bits at once, so `HSE_STARTUP_TIMEOUT`/PLL timeouts never fire and a missing crystal cannot be simulated;
  the clock security system and `RCC_CIR` ready interrupts are not modelled. FLASH `ACR` latency, prefetch and
  caches are stored but cost nothing (1 cycle per instruction). `PWR_CSR.VOSRDY` is always 1. `HAL_UART_Transmit_DMA`/`HAL_UART_Receive_DMA` complete
  (`firmware/samples_stm32_hal/uart_dma_hal.c`); `*_DMA` calls of peripherals that are still stubs (SDIO) never
  complete.
  The checkout has no libc/compiler-rt for Cortex-M, so HAL builds must bring `memset`/`memcpy`, `__aeabi_memclr*`
  and `__aeabi_uldivmod` themselves (`firmware/samples_stm32_hal/aeabi_rt.c`, `startup.c`).

## STM32F405xx / STM32F407xx package variants

Everything in the STM32F405ZG section applies to every `chips/stm32f405*.yaml` and `chips/stm32f407*.yaml` file (same
die, same peripheral list and models). In addition:

- **The peripheral map is the die's, not the package's.** Blocks that a smaller package cannot use are still
  mapped: the STM32F405RG (LQFP64) has no FSMC per DS8626 Table 2 but its FSMC registers are a RAM-like stub, and
  GPIOD..GPIOI registers exist on every package (writes reach no pin when the port is not bonded out, which matches
  silicon). The F405 files keep the Ethernet and DCMI stubs (F407-only blocks), the F407 files keep the CRYP/HASH
  stubs (F415/F417-only blocks).
- **Ethernet and DCMI are stubs** on the F407 parts: the `ETH_*`/`DCMI_*` alternate functions show as pin owners,
  but no MAC, PHY or camera data path is modelled.
- **No flash-size register.** The F405OE/F407xE files map 512 KB of flash, but the flash-size data register
  (0x1FFF7A22, system memory) and the unique ID are not mapped (bus fault), so firmware that sizes flash at run time
  faults. Flash beyond 512 KB on an "E" part is unmapped, not an alias.
- **BGA / WLCSP drawings**: the web UI draws every package as a quad package; `num` is a row-major ball index and the
  ball name is only in the YAML (`ball:`). On the UFBGA176 files `VREF-` (ball N1) is drawn at level H, because the
  generic power-pin rule ties only names with `VSS`/`VREFL` low.
- **UFBGA176+25 pin count**: the files list all 201 balls (176 + 25 thermal VSS balls F6..K10), so
  `tools/pin_table_check.py` needs `--expect 201` (its default takes the count from the package name).

## TMS320F28232 (C2000)

- **ISA checked against TI's tools**: every `isa.yaml` entry assembles to the same words as TI's cl2000 25.11.1
  assembler, and our disassembler agrees with `dis2000` on every C28x and FPU32 instruction of TI's run-time
  libraries (about 600,000 instructions). TI-compiled C runs: `firmware/samples_c2000_c` (EABI and COFF builds, RTS
  `_c_int00`, soft-float and 64-bit helpers) behaves like the assembler samples. Not covered: FPU64 and the fast
  integer division (FINTDIV) instructions of newer C28x devices, VCU/TMU/CLA, and `SXTB AX` (listed in SPRU430F
  without an opcode; it traps as an illegal instruction). See [C28X_CORE.md](C28X_CORE.md).
- **No pipeline timing**: one cycle per instruction, no pipeline stalls, wait states or flash timing. `RPT` is
  atomic, and instructions the manual marks "not repeatable" are still repeated (only flow control runs once).
  The F28232 has no FPU (`fpu: false`): FPU32 opcodes, including `RPTB`, trap as illegal instructions there, as on
  silicon; FPU32 and `RPTB` run on F2833x-class chips (`fpu: true`).
- **AMODE = 1** (C2xLP addressing) is executed but the assembler/disassembler only know the AMODE = 0 syntax.
- **Loaders**: TI COFF2 and C2000 ELF (EABI, the cl2000 v25 default) load sections/segments at their load address,
  checked with real cl2000 25.11.1 output. `--rom_model` (.cinit copied by `_c_int00` at run time) works for both;
  `--ram_model` load-time initialisation is applied for COFF only (EABI's compressed `.cinit` is not decoded).
  `.ebss`/`.bss` are not zeroed by the loader (RAM resets to 0 anyway).
- **EALLOW protection is enforced**: a write to a protected register without EALLOW is ignored (one `warn` per
  block), like silicon. Missing `EALLOW` in firmware shows up as configuration that does nothing.
- **Storage only**: HRPWM, McBSP and XINTF timing keep register values but have
  no behaviour; SPI MISO reads 0xFFFF.
- **I2C-A** (`ti_c2000.i2c`) is a pin-level bus on GPIO32/33 (SDAA/SCLA): module clock SYSCLKOUT/(IPSC+1) latched
  at IRS = 1 (one `warn` outside 7-12 MHz), SCL low/high = Tmod x (ICCL/ICCH + d). Not modelled: START byte mode
  (STB) and FREE (stored only); free data format and 10-bit addresses as a slave; I2CEMDR.BC stored only (slave-transmit
  XRDY timing unchanged); noise filters and rise time; a slave byte arriving with I2CDRR full is ACKed and SCL is
  then stretched (RSFULL) rather than NACKed; the SDA hold time is fixed at SCL low / 4 (SPRUG03 gives no value).
- **ADC** (`ti_c2000.adc`) models the dual / cascaded sequencers with start/stop and continuous run (SEQ_OVRD),
  SEQ1-before-SEQ2 arbitration of the one converter, pending / lost triggers, software, ePWM SOCA/SOCB and external
  (XINT2 pin rising edge) starts, INT_MOD (every other end of sequence), simultaneous sampling, and conversion timing
  from HSPCLK / ADCCLKPS / CPS / ACQ_PS (SPRS439 latencies). Channel values come from `--adc` / `set_channel`, not
  pin voltages. The SEQ1INT/SEQ2INT pulses (and the ADCSYNC of SEQ_OVRD + CONT_RUN) trigger the DMA, which clears
  INT_SEQx. Not modelled: SUSMOD, the few SYSCLKOUT between the last result
  and the interrupt flag, the ADCCLK phase. Every ePWM SOCA / SOCB also pulses a pin muxed to ADCSOCAO / ADCSOCBO
  low for 32 HSPCLK cycles.
- **DMA** (`ti_c2000.dma`, SPRUI07 ch. 8) models the six channels with every PERINTSEL trigger except McBSP (SEQ1INT /
  SEQ2INT, XINT1-7 / XINT13, TINT0-2, ePWM1-6 SOCA/SOCB, PERINTFRC), PERINTFLG latching and overflow (OVRFLG,
  OVRINTE), burst / transfer / wrap address generation with signed steps and active / shadow pointers, 16/32-bit
  items, ONESHOT, CONTINUOUS (ping-pong from the shadows), DINTCHn at the beginning or end of a transfer, the ADCSYNC
  re-synchronisation (SYNCE / SYNCSEL / SYNCERR, triggers ignored until the first sync after RUN), round-robin and
  CH1 high-priority arbitration (preemption after the word in progress, PRIORITYSTAT), HALT / SOFTRESET / HARDRESET /
  PRIORITYRESET and the DMAENCLK gate. Timing is SPRUI07 8.3: a burst of n items takes 1 + 4n SYSCLKOUT (5n for a
  McBSP source) from the trigger; its data is written at the burst's last cycle (one event per burst), and CPU/DMA
  access conflicts or XINTF stalls cost nothing. The DMA reaches only its bus (L4-L7, XINTF zones 0/6/7, the ADC
  results at 0x0B00, McBSP data registers, ePWM registers remapped to PF3 with MAPCNF.MAPEPWM, where the CPU view is
  not remapped); any other address is skipped with one `warn` per channel (no defined silicon behaviour). McBSP
  events never trigger (McBSP is storage), DEBUGCTRL.FREE (emulation suspend) has no effect.
- **ePWM** (`ti_c2000.epwm`) models time base (including TBCTR above TBPRD counting to 0xFFFF with TBSTS.CTRMAX),
  compare, action qualifier, dead band, PWM chopper (every carrier edge is an event: slow, one `warn`), trip zone
  (software and TZ1–TZ6 pins, one-shot and cycle-by-cycle), event trigger (INT, SOCA/SOCB), the F2833x sync scheme
  (ePWM1 → 2 → 3, ePWM1 → 4 → 5 → 6, ePWM1 → eCAP1; 2-SYSCLKOUT / next-TBCLK load latency), the EPWMSYNCI /
  EPWMSYNCO pins and the EPWMxA/B pins. A high-impedance trip releases the pin: it becomes an input whose level
  comes from the pull-up or a net, else X. Not modelled: HRPWM (MEP), emulation FREE_SOFT (no emulation-suspend
  state), GPIO input qualification of TZn / EPWMSYNCI, compare events while TBCTR is above TBPRD.
- **eCAP** (`ti_c2000.ecap`, eCAP1-4, SPRUFG4A) models the 32-bit TSCTR at SYSCLKOUT, capture from the ECAPx pin
  (event prescaler, CAPxPOL, CTRRSTx, CAPLDEN, Mod4 sequencer, continuous / one-shot with STOP_WRAP and REARM),
  CTROVF, CTRPHS loads by SWSYNC / SYNCI (eCAP1 from ePWM1 SYNCO when the ePWM model offers `sync_listeners`, then
  eCAP1 → 2 → 3 → 4), APWM mode (CAP1/CAP2 active, CAP3/CAP4 shadows loaded at CTR=PRD, APWMPOL, output on the pin)
  and the ECFLG / ECEINT / ECCLR / ECFRC interrupt logic (ECAPx_INT, PIE 4.x). None of its registers is
  EALLOW-protected. Not modelled: emulation FREE_SOFT (always free-running), GPIO input qualification and
  synchronization delay (an edge is captured on the cycle it reaches the pin), CTR=PRD sync-out / comparators in
  capture mode; the prescaler state is cleared on a PRESCALE change and on REARM (our choice: the guide does not
  say), and a TSCTR load (write or sync) produces no CTR=0 / compare event at the load instant.
- **eQEP** (`ti_c2000.eqep`, eQEP1/2, Type-0 eQEP of SPRUI07 ch. 6 = SPRUG05A) decodes the EQEPxA/B/I/S pins
  (QSRC quadrature x4 / direction-count / up / down, XCR 2x/1x, SWAP, QAP/QBP/QIP/QSP, IGATE; an A and a B edge in
  the same cycle is a phase error, PHE, not counted), the position counter with QPOSMAX rollover (PCO/PCU), PCRM
  index / max / first index / unit time reset with the index marker logic (FIMF, FIDF, QDLF, PCEF/PCE, QPOSILAT),
  IEI/IEL and SEI/SEL index and strobe init / latch, SWI, QDF/QDC, position compare with shadow load (PCM, PCR), the
  unit timer (QUTMR/QUPRD, UTO, QPOSLAT / QCTMRLAT / QCPRDLAT latch with QCLM), the watchdog (SYSCLKOUT/64, WTO),
  the capture unit (CCPS, UPPS, QCTMR/QCPRD, UPEVNT, COEF, CDEF, latch on a QPOSCNT read with QCLM = 0) and the
  QFLG / QEINT / QCLR / QFRC interrupt logic (EQEPx_INT, PIE 5.x). No register is EALLOW-protected. Not modelled:
  the position-compare sync output on EQEPxI/S (SOEN, SPSEL, PCSPW, PCPOL stored; the pins stay inputs), emulation
  FREE_SOFT, GPIO input qualification and synchronization delay, the "index within one SYSCLKOUT of a QCLK" boundary
  case. Our choices where the guide is silent: QPEN = 0 clears QEPSTS, the latch registers and the index-marker
  state but keeps QPOSCNT and QFLG; the capture timer wraps past 0xFFFF (COEF) instead of saturating; QFRC reads 0;
  a polarity / gating change or a mux change does not produce an edge; QDF is 0 after reset, so the first forward
  edge sets QDC.
- **Clock gates only warn** (PCLKCR); silicon ignores accesses to an unclocked peripheral.
- SCI: overrun is modelled (OE/RXERROR without FIFO: the unread character is overwritten; RXFFOVF with the FIFO:
  the 17th character is not stored, our reading of the guide's ambiguous wording); bytes arriving in SW RESET, with
  RXENA clear, the PCLKCR0 gate off or GPIO not muxed to SCIRXDx are dropped. No framing/parity errors, break
  detection, FFTXDLY or autobaud. No boot ROM (reset jumps to the
  image entry or 0x33FFF6), CSM always unsecured, flash writable by the CPU, no input qualification.
- The interpreter runs about 2–5 M instructions/s, 1/20 to 1/50 of real time at 100 MHz.

More detail in [C2000_ROADMAP.md](C2000_ROADMAP.md).

## Pins

- Levels are digital (H/L/X). There are no pin voltages: ADC inputs come from `--adc` / `set_channel` (on the STM32
  a channel without a value converts its pin's digital level, H = VREF+, L/X = 0 V).
- Serial peripherals (UART, Kinetis DSPI) do not toggle pin levels with their traffic; I2C does (pin-level bus,
  `chipem/periph/i2cwire.py`) and the STM32 SPI does (bit by
  bit, `stm32.spi`). Timer/PWM channels (STM32 TIM,
  Kinetis FTM, C2000 ePWM, eCAP in APWM mode) do: they drive their signal edge by edge through
  `PinBoard.drive_signal`, and the pin shows the live level. Timer inputs (input capture, eCAP, FTM faults and
  quadrature, TIM break, ePWM trip zones) are pin inputs and see every edge of their pin (`--at`, nets, parts). Otherwise a peripheral-owned pin shows the **idle** state of its signal from the family signal table (`chipem/periph/<family>/signals.py`, rule
  in `docs/CONTRACTS.md`): UART/SCI TX is an output at H, RX an input, SPI SCK/MOSI an output at L (CPOL is not
  looked at), chip selects an output at H, I2C pins open-drain (H only when pulled), timer/PWM channels and clock
  outputs an output at L, ADC/CMP/DAC analog, JTAG/SWD TCK/TMS/TDI inputs and TDO/SWO an output at H. Signals
  the table does not know keep direction `none` and level `X`. On a board (`--board`), a TX pin therefore drives
  its net H and the connected RX reads H, but bytes travel only over a `links:` entry (or the automatic UART link
  a TX→RX net creates).
- Board lockstep: cross-chip effects (net edges, UART link bytes) can arrive up to one quantum (default 10 µs)
  late when the receiving chip is ahead in time.

### CAN (chipem/canbus.py, `kinetis.flexcan`, `ti_c2000.ecan`)

- **Bus.** Frames take their exact stuffed length (SOF..EOF, from the real bit stream and CRC-15) at the
  transmitter's bit time plus 3 intermission bits; arbitration by the arbitration field (lowest wins, losers retry
  after the winner); no ACK from any other active node = ACK error, 14-bit error frame, automatic retry; error-passive
  senders wait 8 more bits. Arbitration and the ACK decision are instantaneous at fixed points (end of the previous
  frame / ACK slot), so the controllers' internal arbitration windows (FlexCAN TASD, eCAN's 80-cycle copy) and the
  move-in/move-out timing are not modelled; a decision taken on one chip is revised only while the winner's SOF bit
  has not passed (lockstep quantum must stay below the shortest frame, ~47 bit times).
- **Errors.** Frames on the emulated bus are never corrupted: no CRC, stuff, form or bit errors from noise, no
  overload frames. A bit-rate mismatch (> 1.5 %) only makes that receiver count a form error and not acknowledge (on
  a real bus its error flags would destroy the frame for everyone). A transmitter without its TX/RX pins muxed gets a
  bit error per attempt (bus off after 32). Two nodes sending the same arbitration field at once are not detected
  (the bus picks one). Bus-off recovery waits 128 x 11 bit times regardless of bus traffic.
- **Pins.** CAN pins are not driven bit by bit: a `can` link joins the controllers; the TX pin shows its recessive
  idle level. A chip description without a pin offering CANx_RX/CANRXx skips the pin check.
- **FlexCAN:** no supervisor-mode access checks (SUPV), low-power stop / doze / self wake-up (WAKINT), BUSY code
  bit, SMP/RJW effects, TASD, WRMFRZ; freeze and disable are acknowledged at once.
- **eCAN:** no emulation suspend (SUSP/SMA), TCOF (TSC MSB overflow), SA1 after leaving init only cleared by the
  first bus contact, no corruption on 16-bit accesses to the 32-bit-only registers.
- **STM32 bxCAN** (CAN1/CAN2) is still a stub; a future `stm32.can` model joins the same bus by implementing the
  node protocol (docs/CONTRACTS.md "CAN bus").
- UART input (links, `--uart-in`, `--at TIME:UARTn=TEXT`, shell, web) is never queued for the firmware: each byte
  arrives at its stop-bit time and is taken or dropped as on silicon (`uart` events with `dir=drop` and a
  `reason`; the run summary counts them). Host input is paced at the receiving UART's current baud (115200 8N1
  while it has none). `--uart-in` starts at t=0, so bytes sent before the firmware has enabled its receiver are
  lost; use `--at` to type later. A chip with no pin offering a UART's RX function receives host input without a
  pin check but nothing over board links.
- A net is a perfect wire: there is no propagation delay or drive strength. A peripheral
  output contributes its idle (or, for a PWM channel, its live) level as a strong driver; a peripheral input or a
  released open-drain pin contributes only its pull resistor; an open-drain pin pulled low (I2C) is a strong L.
  `open_drain: true` nets are wired-AND (H drivers count as released) with an implicit pull-up. There is no
  resistor value, so no RC rise time and no "too weak pull-up" failure.
- Pin identifiers come from the chip YAML's `gpio_name_format` (`PTC5`, `PA9`, `GPIO34`).
- The idle level of a serial signal is fixed: a CPOL=1 Kinetis DSPI clock still shows L (the STM32 SPI follows CPOL). PWM channels show their live level
  (100 % duty, FTM ELSnA / TIM CCxP / ePWM inversion included) while the timer drives them; a released channel
  (output disabled, module off) goes back to the drive the mux model set, the table's idle level. The idle state is
  applied whenever the owner changes and at reset (JTAG/SWD pins on Kinetis and STM32).
- Output volume: a 20 kHz PWM pin produces 40,000 edges per second. The VCD gets every edge; the live printer,
  shell `trace` and the stored event log summarise periodic pins (see USER_GUIDE "Fast signals").

## Board parts

- Parts are digital only (H/L/X/Z): no analog parts (potentiometers, RC filters, voltage dividers), and an LED has no
  current or brightness: it is lit while its net is at the active level.
- Logic is zero-delay combinational: no propagation delay, rise time, setup/hold checks or metastability; a
  flip-flop samples `d` as it is at the clock edge (an edge that changes `d` in the same event may see the old or
  new value depending on wiring order). Combinational loops are detected (16 re-resolutions of a net in one event),
  warned once and left at their last level, so ring oscillators do not oscillate.
- I2C parts (`i2c_eeprom`, `i2c_register_device`) decode the bus from net edges; they never stretch SCL, answer at
  the SCL edge (no output delay), support 7-bit addresses only, and the EEPROM has no write-protect pin, no
  hardware address pins (the address is a parameter) and no endurance model. No SPI/UART parts (SPI flash is a
  `devices:` entry, UART bytes travel over `links:`).
- Cross-chip I2C needs a board quantum below the SCL low time (`quantum: 1us`; the I2C boards set it): with the
  default 10 µs a slave on another chip may answer after the master sampled SDA.
- Timed parts (pulse, pattern) run on the first chip's scheduler; other chips see their edges with the usual board
  lockstep latency (up to one quantum). A pulse faster than the board quantum still works but costs a slice cut per
  edge.
- A `--vcd` file is complete only when the run / session ends: value changes are spooled to a temporary file next
  to it and the header (which must list every wire, including parts added later from the web or the shell) is
  written at close. The shell's own `vcd PATH` command traces pins only (no parts scope).

## Tooling

- Static analysis (`chipem info`) is heuristic. Peripheral references come from literal pools and
  `movw/movt` pairs (ARM) or `MOVW/MOVZ DP` and `MOVL XARn,#22` (C28x). Pin roles come from a bounded forward
  dataflow per function (no path enumeration, no symbolic values): register constants (literal, `movw/movt`,
  `add #imm`, register copies; C28x `MOVL XARn,#addr`, DP, `ADDB XARn`), stack-slot constants and read-modify-write
  known bits, resolved at base+displacement stores (ARM `[rN, #off]`; C28x `@off`, `*+XARn[k]`, `*XARn++`,
  `*--XARn`, `*(0:abs)`). Unknown bits are assumed to keep their reset value. Joins keep the facts all incoming
  paths agree on, loop heads drop what the body redefines, calls drop caller-saved registers. Values passed into a
  function as arguments are not followed (no interprocedural analysis), except the STM32 HAL GPIO calls
  (`HAL_GPIO_Init` with its `GPIO_InitTypeDef` on the stack, `HAL_GPIO_WritePin/TogglePin/DeInit`), recognised by
  ELF symbol name. Pins computed in a loop or taken from tables are missed: on the Withings image only one pin
  write is found (the rest go through a table-driven driver; the pin report shows 0 of 19 configured pins
  predicted). Raw images have no symbols, so function starts are taken from `bl` targets and the vector table, and
  data decoded as code can blur the result. The scan takes about 1 s on the 500 KB Withings image.
- Load-address auto-detection for `.bin` tries only 0x0, 0x1000, 0x2000, 0x4000, 0x8000 and 0x10000.
  Anything else needs `--load-addr` (and possibly `--vtor`).
- `chips/` and the web assets are located relative to the source tree. The package works from the checkout
  (editable install), not as a standalone wheel.
- `keystone-engine` is needed by the tests but not declared in `pyproject.toml`.
- The comment in `firmware/samples/uart_echo.c` says UART0 runs from the 60 MHz bus clock. The model
  clocks UART0/1 from the core clock, as silicon does, so the sample's baud rate is only correct when the
  core clock is 60 MHz (e.g. `--clock 60e6`); at `--clock 120e6` the line runs at about 230 kbaud.
  Byte-level capture works at any clock.

## The Withings image (`s1_off000084_503080.bin`)

What works (run with `--chip mk22fx512vll12`; the load address 0x4000 is auto-detected):

- Boots, and runs its first 2 s without faults:
  - watchdog unlock and reconfigure (5 s on the LPO), refreshed afterwards;
  - SIM gates and MCG FEI with DMX32 → 23.99 MHz;
  - GPIO setup of 13 pins;
  - VBAT register file used as battery-backed RAM;
  - LLWU/PMC/SMC/RTC init;
  - ADC0 calibration;
  - NVIC priorities.
- Starts its RTOS: SVC #0, PendSV task switches on PSP with FP context, SysTick at 1 kHz with BASEPRI
  masking.
- Prints on UART2 (PTD3): ` ------> ENTERING MAINTASK <-------`, `WDOG_RSTCNT=0` at about 17 ms, then
  `[BAT] Use default cal` and the battery reading `[BAT] N mv (P %) ...` at about 20 ms (see below).

What it does not do yet:

- **Without a SPI flash it resets itself about every 0.69 s.** At about 21.5 ms (right after the battery
  reading) it sends a
  JEDEC "read ID" (0x9F) on SPI0 to an external SPI NOR flash (chip select PTC4). With nothing attached MISO
  reads all ones, it reaches a deliberate `udf #0` at 0x3AE1C, its HardFault handler prints a register dump
  on UART2 and it requests SYSRESETREQ. Pass `--spi-flash SPI0:cs=PTC4` to attach the emulated flash
  (`chipem/devices/spi_flash.py`, default Micron M25P16 id 0x202015; the firmware accepts only Micron/ST,
  Atmel and SST ids) and the boot continues through the flash-backed database init.
- **The console needs PTD2 held high.** The firmware only starts its UART2 console (and enables IRQ 35) when
  the receive pin reads idle-high ("UART IS CONNECTED"). With `--set PTD2=1 --at '100ms:UART2=help\r\n'` and
  the flash attached it answers with its command list (the console is up at about 35 ms; bytes sent earlier, e.g.
  `--uart-in` from t=0, are dropped while the UART is gated off or overrun during boot); the `gpio set/get` commands are available there.
- **Battery measurement.** PDB0 (software trigger, continuous, MOD 3999, delay 1999) hardware-triggers ADC0
  channel SE18 (16-bit), DMA0 copies 5 results, and the firmware prints `[BAT] N mv` at about 20 ms. The
  mapping is mV = counts × 0.141 ("Use default cal"): `--adc ADC0_SE18=40000` gives
  `[BAT] 5640 mv (77 %) ... State <OK>`; about 31560–42550 counts cover the 4450–6000 mV range. Without `--adc` it
  reads 0 mV. The software-triggered temperature-sensor read (ADCH 26) also reads 0.
- **PTA13 floats.** It is an input with IRQC "logic 0", and with nothing connected it reads 0, so PORTA's
  ISF stays set. Drive it (e.g. `--set PTA13=1`) to mimic the board's idle level.
- It references DWT, ITM, FPB, TPIU, USB0 and CRC (stubs) and I2C0/1 (modelled, but its sensors are not on any
  board: add `i2c_register_device` parts to answer them). USB code paths will not behave. FTM0–2 are modelled (`kinetis.ftm`); its output with and without the FTM models was
  identical in the PWM review run.
- The 0x0–0x3FFF bootloader is not in the file. That region is erased flash (0xFF), so anything that calls
  into the bootloader would fault.
