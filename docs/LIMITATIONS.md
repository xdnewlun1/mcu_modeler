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
  or with FAULTMASK set, is a lockup: the run stops with exit status 3, except on chips whose YAML sets
  `lockup_reset: true` (the K22: RM 6.2.2.9), where it resets the chip with reason `lockup` (RCM_SRS1.LOCKUP) unless
  `--stop-on-fault` is given.
- **No MPU enforcement.** The ARMv7-M MPU registers at 0xE000ED90 are storage only. The Kinetis system MPU
  (`kinetis.mpu`, 0x4000D000) has its real reset state (RGD0 = whole map, enabled), VLD clearing on descriptor
  writes, the RGDAAC aliases and the RGD0 core write restrictions, but access checks are not made (that needs a
  hook on every memory access): when the enabled regions restrict the core, one `warn` and an `info` listing the
  regions are logged; no bus fault is raised and EARn/EDRn/SPERR are never set.
- **No debug blocks.** DWT, ITM, FPB and TPIU are RAM-like auto-stubs. `DWT_CYCCNT` does not count, so
  delays built on it never finish (or finish at once, depending on the code). ITM stimulus writes are not
  captured. There is no GDB stub and no semihosting. `bkpt` stops the run with `BREAKPOINT`.
- **Sleep.** WFI and WFE both sleep until an enabled exception could preempt (Unicorn raises WFE as an invalid
  instruction; the core turns that into the same halt, `stop_info = "wfe"`). The generic core has no event register
  and SEV runs as a NOP; the STM32 PWR model keeps a WFE event register of its own (see the STM32 PWR bullet), and
  other families treat WFE like WFI.
  `SCR.SLEEPONEXIT` sends the core back to sleep on the return to thread mode. `SCR.SLEEPDEEP` is read by the family
  power controller (Kinetis SMC, STM32 PWR), which picks the deep mode, stops clocks and decides the wake-up sources
  (`chip.power_hooks`, see CONTRACTS.md "Low-power modes"). Virtual time keeps advancing while asleep (jumps to the
  next scheduler event); wake-up latency (regulator, oscillator and PLL start-up) costs no time unless a model says
  otherwise.
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
- No flash cache or prefetch buffer. `kinetis.fmc` has the RM register resets (PFAPR, PFB0CR/PFB1CR with
  read-only RWSC/MW fields), CINV_WAY clears the selected ways' tag/data registers, but the cache is never filled
  (tags and data only change when software writes them) and wait states cost no time.
- **Bus errors from register accesses** cannot be raised by a peripheral model: AXBS PRS writes with duplicate
  priorities or with CRSn.RO set, MCM ETBRL values that are not 0 mod 4, MCM_CR SRAM write protection, FlexBus
  accesses that match no (or several) chip selects or hit a write-protected window, and AIPS PACR/MPRA protection
  are ignored and logged once (`warn`) instead (like the SIM clock gates).
- Unmapped addresses outside the two catch-all windows (0x40000000–0x400FFFFF, 0xE0000000–0xE00FFFFF)
  bus-fault. Inside them, the first access creates a RAM-like 4 KB stub.

## Peripherals

**Stubs.** Blocks with `model: stub` have no
behaviour. They read back what was written and start at 0, not at the SVD reset value. Firmware that waits for a
status bit set by hardware loops forever. The chip YAML's `model:` fields are the authoritative
list. FTM0–3 (`kinetis.ftm`), I2C0–2 (`kinetis.i2c`), CAN0 (`kinetis.flexcan`), USB0 (`kinetis.usbfs`), USBDCD
(`kinetis.usbdcd`), CRC, DAC0, CMP0–2, VREF and EWM are modelled (below; USB: the USB section).

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
| MCG/OSC | No lock/settle delays; IRC trim registers stored only. Clock monitors: `MCG.set_external_clock(False)` / `set_rtc_clock(False)` stop the crystal / RTC oscillator; CME0/CME1 + LOCRE0/LOCRE1 give a LOC reset or the MCG interrupt (SC.LOCS0, C8.LOCS1), the PLL loses lock (LOLS0, LOLRE reset / LOLIE0 interrupt), OSCINIT0 reads 0. The loss is seen at once (no floc detection time). A clock mode running from the stopped oscillator halts the core until it runs again (no automatic fallback, as on silicon). Exit from VLPS/LLS (and STOP with PLLSTEN0=0) in PEE goes to PBE; the PLL relocks at once. |
| PORT | Digital filter, slew rate and drive strength do not affect pin levels. ODE matters only for I2C pins (without it an enabled I2C module drives the released line H, push-pull); a GPIO output with ODE driving H still shows H. IRQC DMA-request modes only set ISF. |
| I2C0–2 | Pin-level bus (SDA/SCL toggle at the F register's rate, Table 48-54, 50 % duty; START/STOP/ACK, clock stretching and arbitration on board nets). Not modelled: 10-bit slave addresses (C2.ADEXT; a 10-bit master just sends the bytes), SMBus (SMB timeouts SLTF/SHTF, FACK = 1 behaves as FACK = 0, ALERTEN, PEC), A2 matching only with SIICAEN, the glitch filter (FLT.FLT stored), high drive (HDRS), SBRC, wake-up (WUEN) and stop holdoff (SHEN). Edges are instantaneous (no rise time); a slave answers at the SCL edge. |
| GPIO | No electrical contention: an output always wins over external stimulus. Floating inputs read 0 (one `warn`). |
| WDOG | The unlock window lasts at least 4096 core cycles (silicon: 256 bus clocks). Reset events of RM 24.6: time-out, a wrong unlock / refresh value, a refresh before the window; with IRQRSTEN they set INTFLG, raise WDOG_EWM and reset 256 bus clocks later. Not checked: the 20-bus-clock gap inside an unlock / refresh sequence and "no configuration within WCT after unlocking". Writes to locked registers are ignored with one `warn`. Test mode is not modelled. The counter holds in STOP/VLPS without STOPEN, WAIT/VLPW without WAITEN and in LLS. |
| PIT | The K22 PIT has no lifetime timer (no LTMR64H/L in its memory map). Channels hold in STOP/VLPS/LLS (bus clock off). Each channel timeout is a trigger for PDB0 (TRGSEL 4–7) and the ADC alternate trigger; DMAMUX periodic triggering is not modelled. |
| LPTMR | MCGIRCLK is fixed at 32.768 kHz and OSCERCLK at 8 MHz. Pulse-counter mode counts edges of CMP0's output or the pins muxed to LPTMR0_ALT1/ALT2 (TPP polarity); the glitch filter (PBYP=0) passes a level held for 2^PRESCALE prescaler clocks, without the sampling phase; PRESCALE=0 with the filter is unsupported on silicon and taken unfiltered (one `warn`). Keeps running in every low-power mode (also VLLS0, where silicon has no LPO/OSC). |
| UART | The TX pin does not toggle: it shows its idle level (output, H) and RX is an input (level from the pull or the net, else `X`). RX behaves like silicon: no input queue; a byte arriving with C2.RE clear, the SIM gate off or no pin muxed to UARTn_RX is dropped, and one arriving while RDRF/OR is set is lost with S1.OR (OR & C3.ORIE drives UARTn_ERR). No parity, framing or noise errors, no break, LIN, IrDA, ISO-7816 or 9-bit data (board links carry bytes). Hardware flow control is modelled on the UARTn_RTS_b / UARTn_CTS_b pins (MODEM TXCTSE, RXRTSE, TXRTSE/TXRTSPOL; see `kinetis/uart.py`): the TXRTSE one-bit lead before the start bit is not inserted, and an unmuxed CTS_b reads asserted. In STOP/VLPS/LLS the UART is static: an arriving character is lost (`drop` reason `stop_mode`) but sets S2.RXEDGIF (BDH.RXEDGIE interrupt wakes STOP/VLPS). TX/RX buffers follow RM ch. 49: UART0/1 have 8-entry FIFOs (`fifo_depth: 8` in the chip YAML; PFIFO reports the size), UART2–4 one entry; the effective depth is 1 until PFIFO.TXFE/RXFE is set. TDRE/RDRF follow the TWFIFO/RWFIFO watermarks, a byte arriving with the buffer full is lost with OR and SFIFO.RXOF, a write to a full transmit buffer sets TXOF, reading D from an empty buffer sets RXUF (silicon's resulting FIFO-pointer misalignment is not modelled), CFIFO flushes and the RXUFE/TXOFE/RXOFE error interrupts work. The transmitter is double buffered (buffer + shift register) and setting C2.TE shifts out a one-character idle preamble first. DMA requests (C5.RDMAS/TDMAS) are supported. Baud pacing is modelled, so TDRE/TC/RDRF timing follows the configured baud rate; UART0/1 run from the core clock, UART2–5 from the bus clock. |
| ADC | Hardware triggers (SC2.ADTRG) come from the PDB pre-triggers (SIM_SOPT7 ADCnALTTRGEN=0) or from the alternate source in ADCnTRGSEL (ALTTRGEN=1; ADCnPRETRGSEL picks SC1A/SC1B): PDB0_EXTRG pin rising edge, CMP0–2 output rising edge, FTM0–3 trigger, PIT channel 0–3 timeout, RTC alarm / seconds, LPTMR0 compare (all sources of Table 3-51; the SIM logs one `info` only for reserved TRGSEL 15 or a source missing from the chip). A trigger during a conversion restarts it. With ADTRG=1 an SC1n write just selects the channel. Calibration completes instantly. Conversion time is fixed (`params.conv_ns`, default 4 µs), times 4–32 with hardware averaging (SC3.AVGE; the average of noise-free samples is the sample). The compare function (ACFE/ACFGT/ACREN, CV1/CV2) suppresses COCO and the result as on silicon. Conversions on the bus clock stop in STOP/VLPS/LLS; ADACK (ADICLK=3) conversions continue in STOP/VLPS. Values come from `--adc`/`set_channel`, not from pin levels; unset channels read 0. ADC0_SE23 (DAC0 output) and ADC1_SE18 (VREF_OUT) convert the source's voltage with VREFH = 3.3 V (`params.vrefh`) or, with SC2.REFSEL=01, VALT = VREF_OUT. The temperature sensor, bandgap (AD27) and VREFH/VREFL channels are not wired. |
| PDB | The counter starts on the software trigger (TRGSEL=15) or any Table 3-51 input: PDB0_EXTRG pin, CMP0–2 output, PIT channel 0–3, FTM0–3 trigger, RTC alarm / seconds, LPTMR0 (a source missing from the chip is logged once and never fires). Pre-triggers drive ADC0/ADC1 SC1A/SC1B (pre-triggers 2–7 ignored); back-to-back chaining follows ADC completion (ch0 pre-trigger 0 chains from ADC1 SC1B). LDMOD=1/2/3 load buffered values at the next wrap or trigger (no distinction). Sequence errors flag when the target COCO is still set, but the conversion still starts. DAC interval trigger 0 drives DAC0's hardware trigger (TOE: every DACINT0+1 PDB clocks from each counter start, up to MOD; EXT: on ADC0 SC1A completion); interval trigger 1 (DAC1, absent) and pulse-out (POEN/POnDLY, the CMP sample/window signal) are storage only. One-shot mode leaves CNT at 0. |
| DMA/DMAMUX | Transfers complete instantly (zero bus time); no channel arbitration, priorities or bandwidth control. DMAMUX periodic triggering (PIT TRIG) is not modelled (logged once; the channel behaves as untriggered). Only peripherals that call `dma_request` (e.g. UART, ADC, DSPI) can request DMA. |
| DSPI (SPI0–2) | Bit by bit on the pins (SCK/SOUT/PCSx driven, SIN sampled) at the CTAR baud rate with tCSC/tASC/tDT, CPOL/CPHA, LSBFE, PCSIS and continuous selection (CONT); slave mode clocked from the SCK/PCS0 pins (MSB first, CTAR0). Not modelled: continuous SCK (CONT_SCKE), the modified transfer format (MTFE: classic timing), the PCS strobe (PCSSE) — one `warn` each; DBR duty cycles are exact only for BR = 2 (50/50 otherwise); a slave with an empty TX FIFO sets TFUF and shifts zeros (what silicon sends is not specified); pin delays. Without a muxed SCK pin a frame is one event (same duration). A responder (`--spi-flash`, `responder(tx, pcs, bits)`) answers instead of the SIN pin; SIN not muxed reads `params.miso` (all ones). |
| SDHC | `kinetis.sdhc` with the SD card device. Data moves in watermark-sized chunks (RDWML/WRWML words per event, timed at 32 / bus width SDCLK per word); the buffer never over/underruns because SDCLK is gated (PRSSTAT.SDOFF), as on silicon. Internal DMA moves data in zero bus time; no SDMA boundary (the K22 SDHC has none); ADMA1 is not modelled (DMAE + one `warn`) although HTCAPBLT advertises ADMA; the K22 DMAMUX has no SDHC slot, so VENDOR.EXTDMAEN does nothing. EMODE little endian puts the first bus byte in bits 7:0 (the RM figure is ambiguous; the NXP SDK relies on this). SDSTB is always 1 (no clock settling), stop at block gap / continue / read wait, SDIO card interrupts, suspend/resume, MMC boot and the 8-bit bus are not modelled (one `warn`). PRSSTAT.CINS follows the attached card (or PROCTL.CDSS/CDTL); there is no card-detect pin or debounce. DCLK is not toggled per edge: `pin` wave events summarise it (`chipem/periph/sdbus.py`); CMD/DAT pins keep their idle roles. |
| I2S0 (SAI) | `kinetis.sai`: bit clock / frame sync / data on the pins (master and slave, RX synchronous to TX), FIFOs, masks, FEF/SEF/WSF, IRQs, DMA. Not modelled: BCS (bit clock swap), BCI, Stop/Debug modes, MCLK duty cycle and fractional-divider jitter (average frequency exact), pad delays. An MCLK above `params.mclk_edges_max_hz` (1 MHz) is not toggled on its pin (one `info` gives the frequency). An external frame sync is read at the drive edge (FSE=0: the frame starts at that bit). Without pins on a side the bits of a word are one event. The bit clock and MCLK frequencies are taken when the clock starts (a later bus/PLL change does not retime a running SAI). |
| CMT | `kinetis.cmt`: carrier and modulator timing, time/baseband/FSK/EXSPC, IROL/CMTPOL/IROPEN, EOCF/IRQ/DMA. The undefined reset values of the data registers read 0; Stop modes and SIM_SOPT2[PTD7PAD] are not modelled. |
| FlexBus | `kinetis.flexbus` + `kinetis.flexbus_space` (0x60000000–0xDFFFFFFF): chip-select decode (incl. the global FB_CS0 out of reset), write protect, port size, external memories (`devices: [{type: sram, bus: FB}]`). Transaction level: no bus timing (ASET/WS/hold cost no time), no FB_TA, no FB_AD/FB_CS/FB_OE pin waveforms or pin-mux checks, BLS/BEM ignored, no instruction fetch from FlexBus memory (the core cannot execute MMIO). |
| MCM | `kinetis.mcm`: the ISCR FPU flags mirror FPSCR and raise the MCM IRQ, re-checked every `params.fpu_poll_ns` (10 µs) while an enable is set (IRQ latency up to that). ETB counter: no trace packets, never expires. |
| RTC | Driven by virtual time. Keeps state across chip resets (VBAT domain) but not across a new Session. TCR compensation stored only. |
| FTFE | Commands complete synchronously (CCIF never reads 0, no erase/program time). Program-only-clears-bits is honoured. Security, backdoor key and protection registers are loaded from the flash configuration field, but protection is not enforced on writes. |
| SMC/PMC/LLWU | Power modes follow RM ch. 7/14/16 (`kinetis/power.py`): WAIT, STOP, VLPR (status only: no frequency limit enforced, a warning when the MCG is not in BLPI/BLPE), VLPW, VLPS, LLS, VLLS3-0; PMPROT write-once and STOPM/RUNM blocking, STOPA on an aborted entry, LPWUI exits, PMSTAT. In stop modes the scheduler events of clock-stopped blocks are held back and resumed with their remaining time (Table 7-2); blocks with their own `clock_stop` (PIT, WDOG, UART) keep counters exact, others (FTM, PDB, DSPI, I2C, SAI, CMT, DMA, SysTick) resume where their next event was, so a counter read just after wake-up may be off by the time slept. Not modelled: the stop-acknowledge handshake (SACKERR), stop-mode wake-ups of I2C address match / CAN / SDHC, ADC conversions on the bus clock that are in progress at entry, pin isolation in VLLS (pins return to their reset state at the wake-up reset instead of holding until ACKISO), the LPO-off and POR-option differences of VLLS0, the LLWU 5-LPO-cycle filter delay (FILT flags are immediate). VLLS SRAM loss reads 0. LLWU F3 bit 7 (RTC seconds, which has no flag of its own) clears on the first F3 read. LVD/LVW are storage only. |
| RCM | SRS0/SRS1 from the `reset` reason: POR, PIN (RESET_b falling edge, RPFC filter not modelled), WDOG, LOC/LOL (MCG monitors), WAKEUP (VLLS, 0x41 with the pin), SW (SYSRESETREQ), LOCKUP (a core lockup resets the K22, `lockup_reset: true` in its YAML). LVD resets, JTAG, EzPort, MDM-AP and SACKERR never occur. |
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
  with UE/RE clear, the RCC enable off, the RCC reset bit set, no pin in AF mode for USARTn_RX (USARTn_TX with
  CR3.HDSEL) or in Stop mode are dropped. CR3.DMAT/DMAR DMA requests are generated (TXE/RXNE). Hardware flow control
  (USART1/2/3/6): CTSE holds a character in TDR while the USARTn_CTS pin is H/X (an unmuxed CTS counts as asserted,
  one `info`; SR.CTS + CTSIE on CTS edges), RTSE drives USARTn_RTS L while RXNE = 0 and H while it is set. Parity: the
  parity bit replaces the MSB on transmit and is computed into DR's MSB on receive; PE, FE (except for a break) and NF
  are never set because links carry no line errors. 9-bit words lose bit 8 on links and host input (byte-wide).
  CR1.SBK sends a timed break (logged `uart` event `brk: 1`), but board links do not carry it; `receive_break()`
  (tests, future links) gives FE + RXNE 0x00 and SR.LBD with CR2.LINEN (LBDL 10/11-bit length is not distinguished).
  Half duplex (HDSEL) receives on the TX pin and echoes its own characters. Mute mode (RWU) wakes on an idle character
  time before an arriving character (WAKE = 0) or on an address mark matching CR2.ADD (WAKE = 1). Smartcard (SCEN),
  IrDA (IREN) and synchronous mode (CLKEN) warn once and run as plain asynchronous; GTPR is storage. The TX pin is not
  toggled per bit (see "Pins").
- **PWR low-power modes** (`stm32.pwr`, RM0090 5.3): Sleep, Stop (SLEEPDEEP, PDDS = 0) and Standby (PDDS = 1) on
  WFI/WFE. Stop entry clears HSEON/PLLON/PLLI2SON and selects HSI (the RM states this for the exit; firmware cannot
  tell the difference), so the firmware must restore its clocks; the entry is skipped while an EXTI pending bit is set.
  In Stop only EXTI lines (pins, RTC alarm 17 / wakeup 22, ...) end the sleep; IWDG, RTC and LSE/LSI keep running;
  USARTs freeze (`clock_stop`), but the other peripheral models (TIM, SPI, I2C, ADC, DMA, SysTick, ...) keep counting
  in Stop: their interrupts do not wake the core (only EXTI does), are taken after the wake-up, and a free-running
  timer has advanced. LPDS/FPDS and the regulator / HSI start-up latency cost no time. Standby resets every peripheral
  except RTC, IWDG, RCC and PWR, releases all pins (PA0 pulled down with EWUP), clears SRAM/CCM RAM to 0 (silicon:
  undefined) and the backup SRAM unless CSR.BRE = 1, and ends with a chip reset ("standby_wakeup") on a WKUP rising
  edge, an RTC alarm or wakeup event (with its interrupt enable in RTC_CR), NRST or IWDG; tamper/time-stamp events are
  not generated (the RTC does not model them). PWR_CSR (WUF, SBF, EWUP, BRE) survives system resets; whether RCC_CSR
  shows a flag after a Standby exit is not modelled (it keeps its previous flags). The WFE event register (exception
  return since the last WFE, `SEV` immediately before the WFE, EXTI event lines, SEVONPEND for EXTI interrupt lines)
  lives in the PWR model, so it exists only on the STM32; an `SEV` elsewhere in the code is not seen. The PVD (EXTI
  line 16, PVDO) is not modelled.
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
  with CCR.TSVREFE/VBATE. Injected group: JSQR (the last JL+1 of JSQ1–4, first one only without SCAN), JSWSTART
  and the Table 52 timer triggers (JEXTEN), latency 3 ADCCLK (+1 PCLK2 external), JOFRx signed results (SEXT),
  JEOC/JEOCIE, JAUTO, JDISCEN, JAWDEN; an injected trigger aborts the running regular conversion, which restarts
  after the injected sequence; a regular trigger during an injected sequence waits for it. Multi-ADC mode
  (CCR.MULTI): regular simultaneous, interleaved (5 + DELAY ADCCLK), injected simultaneous, alternate trigger and the
  combined modes, dual and triple; ADC1's triggers start the slaves (slave triggers are ignored with one `warn`);
  CCR.DMA modes 1–3 queue the results for CDR and the "ADC1" DMA request. Approximations: the RM's equal-conversion-
  time rule for simultaneous groups is not checked; the multi-mode overrun fires when a whole round of CDR items is
  still unread; JAUTO with JDISCEN (not allowed by the RM) ignores JDISCEN; reserved MULTI values warn once and the
  ADCs run independently; with JL < 3 and SCAN = 0 only the first channel is converted (the RM describes scan mode
  only). Not modelled: the EXTI11 / EXTI15 triggers (one `warn`), source impedance and sampling-capacitor effects.
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
  A slave with an empty TX buffer resends its previous frame. Not modelled: TI frame format (one `warn`, Motorola
  timing), slave BIDIMODE, slave CRC, pin speed. I2S mode: see the next item. The Kinetis DSPI is bit-level too; a K22
  DSPI master talks to an STM32 SPI slave over board nets (`boards/k22_stm32_spi.yaml`).
- **I2S** (I2S mode of SPI2/SPI3 and I2S2ext/I2S3ext, `chipem/periph/stm32/i2s.py`): written without the RM0090 PDF
  (from memory of RM0090 28.4 / 7.3.23, the SVD and ST's HAL). Assumptions: MSB/LSB-justified WS high = left; a
  master starts with two data-less lead-in bit clocks (so receivers see a WS edge before frame 0); a master
  transmitter sends zeros until the first DR write and starts with it at the next left slot (underrun is silent in
  master mode, UDR is a slave flag; a slave sends zeros and sets UDR); clearing I2SE stops a master at the end of the
  channel slot, a slave at once; MCK runs while MCKOE=1 in master mode even with I2SE=0 (toggled per edge up to
  1 MHz, a `wave` summary above). Not modelled: the ODD duty cycle / jitter (average rate exact), slave start
  conditions beyond WS synchronisation, SPI mode on I2SxEXT (one `warn`). An I2SxEXT always uses SPIx's frame format.
  Bit edges use `sched.at_ns` (PLLI2S does not follow HCLK); with no CK/WS/SD pin muxed one event per channel slot.
- **Ethernet** (`stm32.eth`, `chipem/ethernet.py`; F407/F417): written without the RM0090 PDF (fetching it was not
  possible): register/descriptor semantics from the SVD, ST's HAL ETH driver (which runs: `boards/stm32f407_eth_hal.yaml`)
  and the Synopsys MAC that RM0090 ch. 33 documents. Approximations: frames never collide (no CSMA/CD back-off,
  half-duplex deferral, late collisions or jabber; the segment is a repeater that delivers every frame to every
  other linked port at its end, whatever their speeds); the DMA moves a frame in zero bus time at the end of its
  reception / when it starts transmitting (store-and-forward always: TTC/RTC thresholds, early transmit/receive
  interrupts and the TX FIFO holding several frames are not modelled; a frame with DC=1 must carry its own FCS);
  the receive watchdog, jabber timer and remote wake-up frame filter (WFE, one `warn`) are not modelled; PTP is
  register storage (TSE: one `warn`, descriptor time stamps 0, no PPS output); VLAN tags are only flagged (RDES0
  VLAN), not filtered; MMC counts only the six counters of the F4 (good frames sent, good unicast received, CRC
  errors; alignment and collision counters stay 0). DMABMR.SR completes 32 HCLK cycles after the write (or after the
  PHY clock pin is muxed); DMABMR resets with SR = 1 (pending until the PHY clock is there). The MII/RMII selection is
  sampled when ETHMACEN is set and at every MAC reset (RCC or DMABMR.SR), which is what ST's HAL relies on. PHYs:
  register sets of LAN8720A/LAN8742A/DP83848/KSZ8081 limited to the basic and the speed-status registers; the PHY
  has its own power-on reset (MAC and chip resets do not reset it); auto-negotiation takes `aneg` (default 1.5 s)
  against the segment's advertisement, there is no parallel detection, next pages or interrupt pin. MDC is a `wave`
  summary, MDIO is not shifted bit by bit (a transaction just takes its 64 MDC clocks). Not modelled yet: a bridge
  to the host's real network (TAP device) — to do.
- **DCMI** (`stm32.dcmi`; F407/F417): written without the RM0090 PDF (from memory of ch. 15, the HAL and the CMSIS
  header). Data come from a board `camera` part line by line through the DVP hook and become 32-bit words at the
  instant their last sample is clocked in; PIXCLK and the data pins are not toggled. PCKPOL is stored only (no
  effect of a wrong sampling edge). The F42x/F446 fields BSM/OEBS/LSM/OELS do not exist (one `warn`). JPEG mode only
  pads the last word (no marker handling, no variable line lengths); a partial word at frame end is zero-padded. The
  FIFO is modelled as 8 words. Embedded sync follows the camera part's convention (frame start replaces the first
  line start, frame end the last line end). No setup/hold timing checks.
- **CRYP / HASH** (`stm32.cryp`, `stm32.hash`; F415/F417, `chips/stm32f415zg.yaml`, `chips/stm32f417zg.yaml`):
  written without the RM0090 PDF. Processing times per block as recalled from RM0090: AES 14/16/18 HCLK cycles
  (128/192/256-bit keys; key preparation the same), DES 16, TDES 48; SHA-1 66, MD5 50 (SHA-224/256 50 with
  `sha2: true`, F43x). A DIN write to a full CRYP IN FIFO is dropped with a `warn` (silicon stalls the bus). GCM/CCM
  (F43x) are not modelled (one `warn`). The HASH context-swap register layout (CSR0-53) is CHIP_EM's own (ST does not
  document it): contexts restore within CHIP_EM, but a context saved on silicon cannot be loaded. HASH automatic DCAL
  after DMA is detected from the DMA stream's NDTR rather than an end-of-transfer signal. RCC CRYPEN/HASHEN and
  CRYPRST/HASHRST are writable on the F405/F407 too (reserved bits there). The ciphers are pure Python
  (`chipem/crypto_algos.py`), checked against FIPS-197, SP 800-38A, SP 800-67 and RFC 2202/4231 vectors.
- **SDIO** (`stm32.sdio`, RM0090 ch. 31, SD memory cards only): command and response timing in SDIO_CK periods (48
  bits, NCR 2 / NID 5 clocks, 64-clock CTIMEOUT, NCC 8), data word by word (32 / bus width clocks per word) with block
  CRC16 + end bit and the card's CRC-status / busy for writes. SDIOCLK is PLL48CLK (0 while the PLL is off: commands
  wait). The FIFO raises one DMA request per word (silicon issues bursts); DMA2 PFCTRL (peripheral flow control) is not
  modelled by `stm32.dma`, which uses NDTR (one `warn`), so DMA transfers need NDTR = the word count, as ST's HAL
  programs it anyway. Block CRCs are not computed on the wire: a block is "bad" when the card and host disagree on
  bus width or block size, a needed DAT pin is not muxed, or the card injects an error (`SDCard.crc_errors`). Not
  modelled: SDIO-card interrupts and read wait (RWSTART/RWSTOP/SDIOEN), suspend, CE-ATA, stream mode (MMC; an SD card
  does not answer it), the NEGEDGE sampling edge, the 8-bit bus, CLKCR write-to-write delays. SDIO_CK is not toggled
  per edge (up to 48 MHz): while the clock runs, one `pin` event `field="wave"` (edges, frequency, duty 0.5) per 100 ms
  window and per clock change summarises it (`chipem/periph/sdbus.py`); CMD/D0-D7 keep their idle pin roles.
- **SD card device** (`chipem/devices/sdcard.py`, `--sd-card`, board `{type: sd_card}`): SD physical layer v2.00
  native mode (no SPI mode), SDHC (CSD v2, block addressing) or SDSC (CSD v1, byte addressing; `version=1` for a v1.x
  card). Commands CMD0/2/3/4/6/7/8/9/10/12/13/15/16/17/18/23/24/25/32/33/38/55 and ACMD6/13/22/23/41/42/51; others
  (and CMD42 lock/unlock: class 7 is not in the CCC) are illegal, with ILLEGAL_COMMAND reported on the next command.
  Responses carry real CRC7 (CID/CSD with their internal CRC). Timing: power-up `init_time` (1 ms, real cards up to
  1 s), read access `read_time` (Nac, 50 us), programming `busy` (250 us per block), erase `erase_time` (1 ms);
  erased/unwritten blocks read 0x00. No write protection, no UHS-I (S18R ignored), no speed-class timing, no CMD
  CRC errors (the hosts always send valid CRC7). The card is not electrically on the pins (like `--spi-flash`): the
  host checks that its CK/CMD/DAT signals are muxed.
- **FSMC** (`stm32.fsmc`, RM0090 ch. 36): only the NOR/PSRAM/SRAM bank 1 windows (NE1-NE4) exist; NAND/PC Card banks
  2-4 are register storage (their address ranges bus-fault, no FSMC interrupt, ECC not computed). Accesses take the
  CPU's normal memory time: ADDSET/ADDHLD/DATAST/BUSTURN/CLKDIV/DATLAT and the extended mode (BWTR) are not applied,
  nor synchronous burst, NWAIT, address/data multiplexing (MUXEN: one `warn`) or NOR flash command sets (a NOR bank
  is plain memory). The address/data/NE/NOE/NWE/NBL pins are not toggled. Board SRAMs (`{type: sram, bus: FSMC}`)
  attach to the first free sub-bank; the first 16 mirrors of the 64 MB window are mapped directly into the core
  (fast, executable), further mirrors go through callbacks (not executable, and a WREN = 0 write there is ignored
  instead of faulting). An MWID different from the memory width only warns (bytes are kept as written). A sub-bank
  enabled without a memory reads 0.
- **CRC** (`stm32.crc`): 32-bit words fed MSB first with polynomial 0x04C11DB7 (CRC-32/MPEG-2 over the word
  stream; 0x12345678 → 0xDF8A8A2B), CR.RESET, IDR. Instantaneous. 8/16-bit DR writes are fed zero-extended with one
  `warn` (the F4 unit has no byte enables).
- **RNG** (`stm32.rng`): a new value 40 PLL48CLK periods after RNGEN and after each DR read (PLL48CLK = the main PLL's
  Q output, 0 while PLLON=0); clock error (CECS/CEIS) when it is stopped or below HCLK/16. Values are deterministic
  (seeded PRNG, `params.seed`) and never repeat consecutively; seed errors only through `inject_seed_error()`. The
  HASH_RNG vector is shared with HASH (`_util.shared_irq`; HASH is a stub except on F415/F417).
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
- SDIO and FSMC are RAM-like stubs; USB OTG_FS/OTG_HS and bxCAN: see the USB and CAN sections.
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
  silicon). Blocks of other die-family members are gated by `params.only` in the base YAML and behave as stubs (one
  `warn` on the first access): Ethernet and DCMI on the F405/F415 files, CRYP/HASH on the F405/F407 files. Only the
  LQFP144 F415/F417 parts have chip files (`stm32f415zg`, `stm32f417zg`); other F415/F417 packages would be copies of
  the F405/F407 files with another name.
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
- **McBSP** (`ti_c2000.mcbsp`, McBSP-A on the F28232, SPRUI07 ch. 12) is bit-level on the pins: the sample rate
  generator (LSPCLK or an MCLKR/MCLKX pin, CLKGDV with the odd-divide duty, FWID/FPER, CLKG/FSG low with GRST = 0),
  internal or pin clocks / frame syncs per PCR and polarities, FSGM = 0 frame syncs per DXR copy (they need FRST = 1,
  as the manual says), one- and two-phase frames of 8-32-bit words, 0-2-bit data delay (delay 0 drives bit 0 as the
  frame sync becomes active), RJUST, mu-/A-law companding (G.711) and LSB-first 8-bit words, the DRR/RBR/RSR pipeline
  (RRDY, RFULL after the third unread word, later words overwrite RSR), XRDY/XEMPTY double buffering and underflow
  (the old DXR is resent; zeros right after XRST), unexpected frame syncs (RSYNCERR/XSYNCERR unless RFIG/XFIG),
  digital loopback, clock-stop (SPI) master and slave in the four CLKSTP/CLKXP schemes, MFFINT-gated MRINTA/MXINTA
  for RINTM/XINTM 0, 2 and 3, the DMA events MXEVTA/MREVTA and the PCLKCR0 gate (the port stops while it is off).
  The SPI NOR flash device answers a clock-stop master packet byte by byte (`spi: MCBSPA`); with MFSX as its chip
  select a command is one packet (up to 32 bits), longer commands need a GPIO chip select. A free-running MCLKX/MCLKR
  output is one scheduler event per clock edge (several million per simulated second at a few MHz); with the clock
  pins not muxed and FSGM = 0 the generator costs nothing while idle. Not modelled (one `warn`): multichannel
  selection (MCR RMCM/XMCM: all channels enabled; RINTM/XINTM = 1 never interrupts), GSYNC, DXENA, reserved word
  lengths / data delay 3, FREE/SOFT. Between frames MDX keeps the last bit (silicon: high impedance); MDX is high
  impedance only while the transmitter is in reset. An edge that fires late because the core ran an atomic
  `RPT` shows on the pin at the time it is processed (several edges can then share one timestamp).
- **SPI-A** (`ti_c2000.spi`) is bit-level on the pins (master and slave, four clock schemes, FIFOs, SPISTE gating).
  The master drives SPISTEA low from the first bit of a character until the last of a back-to-back run (FIFO with
  TXDLY = 0, or SPITXBUF refilled in time), high otherwise. A slave whose SPISTEA is not muxed to a pin counts as
  selected. On RX FIFO overflow the oldest word is lost. Not modelled: FREE/SOFT emulation suspend, the one-SPICLK
  delay before SPICLK returns to its idle level after SPISWRESET = 1, slave clock speed limits (LSPCLK/4); SPICCR/
  SPICTL changes while a character is shifting take effect at once.
- **Synchronous links between chips** (McBSP, SPI on board nets) need a board `quantum` below half a bit period: a
  slave answers a clock edge in its own time slice, and the master samples before it sees the answer otherwise.
- **HRPWM** (`ti_c2000.epwm`, ePWM1-4 with `params.hrpwm`; SPRUI07 ch. 4): TBPHSHR / CMPAHR (MEP steps in bits
  15:8; CMPAHR shadowed like CMPA, loaded per HRCNFG.HRLOAD), HRCNFG EDGMODE / CTLMODE, MEP delay of the selected
  output-A edges by steps x `mep_step_ps` (chip YAML, default 150 ps), the 3-cycle MEP-inactive zone at the start of
  the period. Pin edges are timestamped in core cycles (10 ns at 100 MHz), so a refined edge is shown at the nearest
  SYSCLKOUT edge; the exact edge times, high time and duty (fractional ns) are in `EPWMx.hr_state()` and one `info`
  event per HRPWM configuration / step change. Phase control (CTLMODE = 1) delays the edges at all times, not only
  after a TBPHS load. Not modelled: the MEP calibration / diagnostics logic used by TI's SFO library
  (`SFO_MepDis`/`SFO_MepEn` never return a scale factor; firmware that waits for one hangs), MEP step variation with
  temperature and voltage. The F28232 has no HRPWM on ePWM5/6 and no TBPRDHR.
- **XINTF** (`ti_c2000.xintf`, `ti_c2000.xintf_zone`, SPRUI07 ch. 14) works at transaction level. Zones 0/6/7 reach
  an external memory declared on the board (`devices: {type: sram, bus: XINTF6}`) only when PCLKCR3.XINTFENCLK = 1
  (otherwise the access is not performed, with one `warn`) and when the zone chip select, XWE0/XWE1, XA and XD lines
  are muxed to their XINTF functions. Unmuxed address lines reach the device as 0; unmuxed data lines, and zones with
  no device, read the pin pull (0xFFFF after reset). Lead/active/trail (X2TIMING, XTIMCLK), XBANK cycles and the 1-3
  deep write buffer are charged to the CPU; DMA, debugger and loader accesses are not timed. Not modelled: XREADY
  (always ready; one `warn` while USEREADY = 1), XHOLD/XHOLDA, instruction-fetch wait states from XINTF, the
  write-followed-by-read pipeline order, XA/XD/strobe pin waveforms. XCLKOUT (pin 138) runs at SYSCLKOUT / XTIMCLK /
  CLKMODE divider from reset (SPRUI07 1.3.2.3: active during reset, not gated by XINTFENCLK; CLKOFF stops it; it stops
  in STANDBY/HALT); it is not toggled but summarised as `pin` events `field="wave"` (`chipem/periph/sdbus.py`), one
  per 100 ms and per frequency change. Register decisions:
  EALLOW-protected per ch. 14 (Table 1-86 says otherwise); XTIMINGx reset 0x0043FFFF; XINTCNF2 bit map and reset
  0x00010C04 taken from TI's DSP2833x_Xintf.h (SPRUI07 has no XINTCNF2 figure); XBANK 0x003F; XRESET at 0x0B3D
  (header) with an alias at SPRUI07's 0x083D; PCLKCR3 resets to 0x2700 (XINTF clock off).
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
- **DMA** (`ti_c2000.dma`, SPRUI07 ch. 8) models the six channels with every PERINTSEL trigger (SEQ1INT /
  SEQ2INT, XINT1-7 / XINT13, TINT0-2, McBSP-A MXEVTA / MREVTA, ePWM1-6 SOCA/SOCB, PERINTFRC), PERINTFLG latching and overflow (OVRFLG,
  OVRINTE), burst / transfer / wrap address generation with signed steps and active / shadow pointers, 16/32-bit
  items, ONESHOT, CONTINUOUS (ping-pong from the shadows), DINTCHn at the beginning or end of a transfer, the ADCSYNC
  re-synchronisation (SYNCE / SYNCSEL / SYNCERR, triggers ignored until the first sync after RUN), round-robin and
  CH1 high-priority arbitration (preemption after the word in progress, PRIORITYSTAT), HALT / SOFTRESET / HARDRESET /
  PRIORITYRESET and the DMAENCLK gate. Timing is SPRUI07 8.3: a burst of n items takes 1 + 4n SYSCLKOUT (5n for a
  McBSP source) from the trigger; its data is written at the burst's last cycle (one event per burst), and CPU/DMA
  access conflicts or XINTF stalls cost nothing. The DMA reaches only its bus (L4-L7, XINTF zones 0/6/7, the ADC
  results at 0x0B00, McBSP data registers, ePWM registers remapped to PF3 with MAPCNF.MAPEPWM, where the CPU view is
  not remapped); any other address is skipped with one `warn` per channel (no defined silicon behaviour). McBSP-B
  events (PERINTSEL 16/17) never trigger on the F28232 (one `warn`), DEBUGCTRL.FREE: see "Emulation suspend".
- **ePWM** (`ti_c2000.epwm`) models time base (including TBCTR above TBPRD counting to 0xFFFF with TBSTS.CTRMAX),
  compare, action qualifier, dead band, PWM chopper (every carrier edge is an event: slow, one `warn`), trip zone
  (software and TZ1–TZ6 pins, one-shot and cycle-by-cycle), event trigger (INT, SOCA/SOCB), the F2833x sync scheme
  (ePWM1 → 2 → 3, ePWM1 → 4 → 5 → 6, ePWM1 → eCAP1; 2-SYSCLKOUT / next-TBCLK load latency), the EPWMSYNCI /
  EPWMSYNCO pins and the EPWMxA/B pins. A high-impedance trip releases the pin: it becomes an input whose level
  comes from the pull-up or a net, else X. HRPWM: see above. Not modelled: compare events while TBCTR is above TBPRD.
- **eCAP** (`ti_c2000.ecap`, eCAP1-4, SPRUFG4A) models the 32-bit TSCTR at SYSCLKOUT, capture from the ECAPx pin
  (event prescaler, CAPxPOL, CTRRSTx, CAPLDEN, Mod4 sequencer, continuous / one-shot with STOP_WRAP and REARM),
  CTROVF, CTRPHS loads by SWSYNC / SYNCI (eCAP1 from ePWM1 SYNCO when the ePWM model offers `sync_listeners`, then
  eCAP1 → 2 → 3 → 4), APWM mode (CAP1/CAP2 active, CAP3/CAP4 shadows loaded at CTR=PRD, APWMPOL, output on the pin)
  and the ECFLG / ECEINT / ECCLR / ECFRC interrupt logic (ECAPx_INT, PIE 4.x). None of its registers is
  EALLOW-protected. Not modelled: the one-SYSCLKOUT synchronization delay (an edge is captured on the cycle it passes
  the GPIO input qualification), CTR=PRD sync-out / comparators in
  capture mode; the prescaler state is cleared on a PRESCALE change and on REARM (our choice: the guide does not
  say), and a TSCTR load (write or sync) produces no CTR=0 / compare event at the load instant.
- **eQEP** (`ti_c2000.eqep`, eQEP1/2, Type-0 eQEP of SPRUI07 ch. 6 = SPRUG05A) decodes the EQEPxA/B/I/S pins
  (QSRC quadrature x4 / direction-count / up / down, XCR 2x/1x, SWAP, QAP/QBP/QIP/QSP, IGATE; an A and a B edge in
  the same cycle is a phase error, PHE, not counted), the position counter with QPOSMAX rollover (PCO/PCU), PCRM
  index / max / first index / unit time reset with the index marker logic (FIMF, FIDF, QDLF, PCEF/PCE, QPOSILAT),
  IEI/IEL and SEI/SEL index and strobe init / latch, SWI, QDF/QDC, position compare with shadow load (PCM, PCR), the
  unit timer (QUTMR/QUPRD, UTO, QPOSLAT / QCTMRLAT / QCPRDLAT latch with QCLM), the watchdog (SYSCLKOUT/64, WTO),
  the capture unit (CCPS, UPPS, QCTMR/QCPRD, UPEVNT, COEF, CDEF, latch on a QPOSCNT read with QCLM = 0) and the
  QFLG / QEINT / QCLR / QFRC interrupt logic (EQEPx_INT, PIE 5.x) and the position-compare sync output (SOEN / SPSEL
  turn the EQEPxI or EQEPxS pin into an output; PCSOUT is active for 4 x (PCSPW + 1) SYSCLKOUT per match, retriggered
  by the pulse stretcher, PCPOL inverts it). No register is EALLOW-protected. Not modelled: the synchronization
  delay, the "index within one SYSCLKOUT of a QCLK" boundary case. Our choices where the guide is silent: the pin
  carrying PCSOUT is not also an index / strobe input of the module; QPEN = 0 clears QEPSTS, the latch registers and the index-marker
  state but keeps QPOSCNT and QFLG; the capture timer wraps past 0xFFFF (COEF) instead of saturating; QFRC reads 0;
  a polarity / gating change or a mux change does not produce an edge; QDF is 0 after reset, so the first forward
  edge sets QDC.
- **GPIO input qualification** (GPxQSEL 3/6 samples, GPxCTRL.QUALPRD; SPRUI07 1.4.4) delays and de-glitches what
  GPxDAT, XINT1-7/XNMI and the peripheral inputs on the pin see (eCAP, eQEP, ePWM TZ/SYNCI, SPI, McBSP, the ADC
  external SOC: they subscribe through `_util.listen_input`), first sampling edge after the change + 2/5 sampling
  periods. A model reading a second pin's level inside its callback (eQEP A/B) reads that pin raw. I2C-A (shared
  `i2cwire` bus engine) sees the raw pin; SCI bytes travel over links, not pin edges. The one-SYSCLKOUT
  synchronisation of synchronous inputs is not modelled.
- **Emulation suspend**: there is no debugger halt, so every FREE/SOFT-type bit (ePWM TBCTL.FREE_SOFT, eCAP
  ECCTL1.FREE_SOFT, eQEP QEPCTL.FREE_SOFT, CPU timer TCR.FREE/SOFT, SPI/SCI/McBSP/I2C FREE, ADC SUSMOD, eCAN SUSP,
  DMA DEBUGCTRL.FREE) is stored and has no effect, silently.
- **Low-power modes** (`ti_c2000.sysctrl`, SPRUI07 1.3.3): IDLE (LPMCR0.LPM = 00; any enabled interrupt wakes),
  STANDBY (01) and HALT (1x) are entered by the IDLE instruction. STANDBY/HALT stop SYSCLKOUT to every peripheral:
  their scheduler events are parked and their time-derived counters (CPU timers, ePWM, eCAP, eQEP, XINT counters, SPI)
  frozen, pin inputs are ignored (edges in between are lost) and SCI bytes are dropped (`reason=clock_stopped`). The
  watchdog keeps counting in STANDBY (and stops in HALT, with the oscillator). Wake-up: a GPIOLPMSEL port A pin held
  low for QUALSTDBY + 2 OSCCLK (STANDBY; a shorter pulse restarts the qualification), the watchdog interrupt with
  LPMCR0.WDINTE + SCSR.WDENINT (STANDBY), a selected pin low through the oscillator start-up
  (`SYSCTRL.params.osc_startup_ns`, default 0) and a PLL relock of 131072 OSCCLK unless PLLOFF (HALT), or a reset. On
  exit WAKEINT is latched; the CPU leaves IDLE only if WAKEINT is enabled (PIEIER1.8 + IER.INT1), otherwise it stays in
  IDLE with the clocks running (HALT: one `warn`, the TRM requires the enable). Other interrupts do not end
  STANDBY/HALT. Approximations: the datasheet wake-up latencies beyond the qualification / PLL lock are not added; a
  watchdog interrupt in STANDBY without WDINTE is still latched in the PIE (no wake-up); models whose state is not
  covered by the generic freeze (ADC conversions, DMA bursts, I2C, McBSP closures) simply resume their pending events
  after the stop; HALT entered in limp mode becomes STANDBY (one `warn`; SPRUI07: unpredictable).
- **Watchdog** (SPRUI07 1.3.4): WDKEY 0x55/0xAA sequence (any other value disarms), WDKEY reads WDCR, WDCHK != 101
  resets (or fires WDINT with WDENINT = 1) even when disabled, WDOVERRIDE write-1-to-clear locking WDDIS, WDINT low for
  512 OSCCLK (WDINTS = 0, one WAKEINT per falling edge), clearing WDENINT while WDINT is low resets, WDFLAG set by a
  watchdog reset and cleared by writing 1 or by XRS (`chip.reset()`). Not modelled: the 512-OSCCLK reset pulse and
  the 8192-cycle WDFLAG delay, the "WDINT count halts while disabled" detail, WDINTS lagging WDINT by 2 SYSCLKOUT.
- **Missing-clock detection / limp mode** (SPRUI07 1.3.2.5): `SYSCTRL.stop_oscillator()` / `start_oscillator()`
  (tests, board scripts) or PLLSTS.OSCOFF = 1 remove OSCCLK. With MCLKOFF = 0 the detector fires after
  `params.mclk_detect_ns` (default 128 limp-clock periods): MCLKSTS = 1 and a missing-clock reset (`reset` reason
  `missing_clock`; PLLCR/PLLSTS and WDFLAG survive), then the CPU runs at the limp frequency `params.limp_hz` (default
  4 MHz, not from a data sheet: set it for your part) / 2 with the PLL enabled, / DIVSEL divider in bypass (our reading
  of the TRM's "limp or one-half"). MCLKCLR clears MCLKSTS and re-arms detection (another reset if OSCCLK is still
  missing); XRS clears it. The watchdog stops without OSCCLK. With MCLKOFF = 1 (or a PLLCR write in limp mode) the CPU
  has no clock: it stops (time advances) until OSCCLK returns. Limp mode is assumed to work from power-up.
- **Code security module** (`ti_c2000.csm`, SPRUI07 1.2): secure after reset; the password match flow (dummy read of
  PWL 0x33FFF8-0x33FFFF, then KEY0-7 = PWL) unsecures, PWL all ones needs only the dummy read, PWL all zeros never
  unsecures, FORCESEC clears KEY (to 0, our reading of "clears") and secures. The boot ROM's dummy read is emulated at
  every reset (`params.boot_dummy_read`), so an erased password leaves the part unsecure. While secure, CPU data reads
  of L0-L3 (and their 0x3F8000 mirror), flash, OTP and the flash registers return 0 unless the executing instruction is
  in secure memory (fetches are never blocked); those pages then leave the interpreter's fast path (slower). Not
  modelled: blocking writes from unsecure code, DMA/JTAG views, the ECSL (no emulator; KEY0-3 are storage), the
  16-wait-state PWL reads.
- **Flash registers** (`ti_c2000.flash`): FPWR/FSTATUS power state machine with the FSTDBYWAIT/FACTIVEWAIT delays and
  STDBYWAITS/ACTIVEWAITS; FOPT.ENPIPE, FBANKWAIT and FOTPWAIT are stored only (flash/OTP accesses cost one cycle like
  everything else); a flash access does not wake a sleeping bank and nothing stalls. The TI Flash API (RAM-resident
  erase/program code driving undocumented pump registers) is not supported: flash is written by ordinary CPU stores.
- **Clock gates only warn** (PCLKCR); silicon ignores accesses to an unclocked peripheral.
- SCI: overrun is modelled (OE/RXERROR without FIFO: the unread character is overwritten; RXFFOVF with the FIFO:
  the 17th character is not stored, our reading of the guide's ambiguous wording); bytes arriving in SW RESET, with
  RXENA clear, the PCLKCR0 gate off or GPIO not muxed to SCIRXDx are dropped. No framing/parity errors, break
  detection, FFTXDLY or autobaud. No boot ROM (reset jumps to the image entry or 0x33FFF6; its CSM dummy read and
  DIVSEL = /2 are emulated), flash writable by the CPU.
- The interpreter runs about 2–5 M instructions/s, 1/20 to 1/50 of real time at 100 MHz.

More detail in [C2000_ROADMAP.md](C2000_ROADMAP.md).

## Pins

- Levels are digital (H/L/X). There are no pin voltages: ADC inputs come from `--adc` / `set_channel` (on the STM32
  a channel without a value converts its pin's digital level, H = VREF+, L/X = 0 V).
- UARTs do not toggle pin levels with their traffic; I2C does (pin-level bus,
  `chipem/periph/i2cwire.py`), the STM32 SPI and Kinetis DSPI do (bit by
  bit, `stm32.spi`, `kinetis.dspi`), and so do the Kinetis SAI (BCLK/FS/TXD) and CMT (IRO carrier). Timer/PWM channels (STM32 TIM,
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

### CAN (chipem/canbus.py, `kinetis.flexcan`, `ti_c2000.ecan`, `stm32.can`)

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
- **STM32 bxCAN** (`stm32.can`, CAN1 master / CAN2 slave): INRQ takes precedence over SLEEP (both the HAL and the SPL
  init sequences work); mode changes wait for the end of the current bus activity, leaving Init/Sleep waits for 11
  recessive bits (needs CANx_RX muxed, else INAK/SLAK stay set with one `warn`, like a missing transceiver). A sleeping
  node notices bus activity at the ACK slot of a frame (WKUI, AWUM), not at its SOF. LBKM runs on a private
  self-acknowledging bus: the frames do not also appear on the board bus (silicon drives CANTX unless SILM). SILM
  never starts a transmission (requests stay pending). TTCM: TGT puts the timer value of the arbitration decision
  into bytes 6–7, at most one frame before the real SOF. The FMI numbering of CAN2 restarts at CAN2SB. CAN2's own
  filter address range reads 0 / ignores writes (one `warn`). Arbitration loss (ALST) is detected when another
  node's frame completes while ours had been requested before its SOF. Not modelled: SJW/resynchronisation, MSR.SAMP/RX
  (read 1), DBF debug freeze, FULL/FOVR clearing other than by software.
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
- The idle level of a serial signal follows the model: the STM32 SPI and the Kinetis DSPI drive SCK at CPOL (the DSPI
  master also drives PCSn at PCSIS). PWM channels show their live level
  (100 % duty, FTM ELSnA / TIM CCxP / ePWM inversion included) while the timer drives them; a released channel
  (output disabled, module off) goes back to the drive the mux model set, the table's idle level. The idle state is
  applied whenever the owner changes and at reset (JTAG/SWD pins on Kinetis and STM32).
- Output volume: a 20 kHz PWM pin produces 40,000 edges per second. The VCD gets every edge; the live printer,
  shell `trace` and the stored event log summarise periodic pins (see USER_GUIDE "Fast signals").

### USB (chipem/usbbus.py, `stm32.otg_fs` / `stm32.otg_hs`, `kinetis.usbfs`, `kinetis.usbdcd`)
- **Transaction level, full and low speed only.** Packets are not bit-level signals on the DP/DM pins: the cable is
  a `chipem.usbbus.UsbBus` between one device controller and one emulated host (no hubs, one device per port). Packet
  durations are exact (SYNC, PID, CRC5/CRC16 computed, bit stuffing counted on the real bit stream, EOP), with a
  fixed 4-bit-time inter-packet delay, an 18-bit-time response timeout and 2 bit times between host transactions.
  Packets are never corrupted (no CRC/bit-stuff errors, no babble, no PID check failures).
- **High speed is not modelled.** OTG_HS runs only with its embedded full-speed PHY (`GUSBCFG.PHYSEL = 1`);
  selecting the ULPI PHY logs one `warn` and the core never connects (no external high-speed PHY, no chirp,
  no HS ENUMSPD). Isochronous transfers are not modelled on either side (no IISOIXFR / INCOMPISOOUT / EOPF, the host
  has no iso scheduling); LPM and test modes neither.
- **Host scheduling** follows a simple controller: periodic (interrupt) transfers at each SOF when their `bInterval`
  is due, then control and bulk round robin while a maximum-size transaction still fits before EOF. A NAKed control
  transaction is retried after 20 us; a NAKed bulk endpoint is polled again in the next frame (real OHCI/UHCI hosts
  retry as often as bus time allows, EHCI throttles with its NAK counter): an idle CDC port costs one transaction per
  ms. A control transfer still NAKed after 5 s fails (Linux's control timeout); 3 consecutive timeouts fail a
  transfer. Enumeration follows the Linux order (GET_DESCRIPTOR(DEVICE, 64) at address 0, second reset, SET_ADDRESS,
  device / configuration / string descriptors, SET_CONFIGURATION) and is retried twice before "device not recognised".
  Spec times are defaults (attach debounce 100 ms, reset 10 ms — a root port would use 50 ms in several pieces —
  recovery 10 ms, SET_ADDRESS recovery 2 ms, resume 20 ms + 10 ms) and can be shortened per link.
- **Class drivers:** CDC-ACM (SET_LINE_CODING, SET_CONTROL_LINE_STATE, bulk IN reads of 512 bytes, bulk OUT
  writes without zero-length packets, SERIAL_STATE notifications), HID (SET_IDLE, report descriptor, interrupt IN
  polling; no report parsing, no output reports), MSC bulk-only (GET_MAX_LUN, INQUIRY, READ CAPACITY, READ(10); no
  error recovery / reset recovery). One driver instance per matching interface.
- **CDC bridge to PC software** (`--usb-cdc`, board `cdc_pty`/`cdc_tcp`/`cdc_serial`): bytes from the PC are written
  at USB speed (no baud pacing); bytes the device sends while no PC program has the port open are lost
  (`dropped_out`, as the host serial bridge). The PC's DTR/RTS and line speed are forwarded only when the transport
  reports them (PTY, pyserial).
- **STM32 OTG cores:** the register file is hand-dispatched (FIFO windows, per-endpoint arrays sized by
  `params.endpoints`); reset values match the SVD (tests). Approximations: CSRST and the FIFO flushes complete at
  once; EPDIS disables an enabled endpoint immediately (no wait for NAK effective); a TX FIFO push into a full FIFO is
  dropped with a `warn` (silicon corrupts the FIFO); the DCFG.DAD change during a control transfer is applied after
  that transfer's zero-length status IN (how the ST stack and the DWC2 core behave together; the databook wording is
  "after the status stage"); after a SETUP the core NAKs EP0 IN/OUT (NAKSTS) until the firmware clears NAK (DWC2
  behaviour; RM0090 does not spell it out); the "SETUP stage done" entry is pushed at the next IN/OUT token on EP0
  (core 2.81a). SOF output pulses are 1 us wide. TRDT/TOCAL are not checked against HCLK. DP/DM must be in the OTG
  alternate function for the pull-up to reach the bus (RM0090 is not explicit for OTG_FS; the HAL MSP always muxes
  them). VBUS sensing reads the VBUS pin's external level (the cable drives PA9/PB13 when the USB link powers
  VBUS); NOVBUSSENS forces a valid B-session. Host mode: registers are storage, one `warn` when host mode is forced.
- **Kinetis USB0 (`kinetis.usbfs`, K22 RM ch. 43):** USBCTRL.SUSP = 1 (its reset value) keeps the D+ pull-up (USB
  suspend requires it; the NXP stack sets SUSP while connected) but the SIE ignores tokens, one `warn`. A USB reset
  clears ADDR but not the BDT ODD bits (43.4.14 names only ODDRST and USBENSOFEN; the NXP driver sets ODDRST in its
  reset handler) and does not flush the STAT FIFO. EPSTALL stalls SETUP too ("any access", 43.4.23; not USB 2.0
  compliant, what the NXP stack expects: it clears EPSTALL in its STALL interrupt). While CTL.TXSUSPENDTOKENBUSY is set
  every token is NAKed, SETUP included (the RM does not say NAK or no response). SETUP to a BD with OWN = 0 is NAKed;
  SETUP skips the DTS toggle check (always DATA0). A DTS toggle mismatch is ACKed and ignored (BD kept, no TOKDNE;
  behaviour of the earlier JM-family USB block, not in the K22 RM). With the STAT FIFO full (STAT + 3) tokens are NAKed
  (unspecified). KEEP = 1 still flips the ODD bit. OTGSTAT and the OTGISTAT session/ID bits are storage (the K22 has
  no USB0 VBUS/ID pins; VBUS feeds VREGIN); SIM_SOPT1CFG.URWE is not enforced for USBREGEN; SYNC_DET is set with
  USB_RESUME_INT when the SIE is clocked (semantics guessed). Not modelled: host mode (HOSTMODEEN / TOKEN / LSEN warn,
  registers are storage), OTG, isochronous endpoints (EPHSHK = 0 warns, handshakes kept), low speed, CRC / bit-stuff /
  PID errors, DP/DM pin activity in the VCD.
- **Kinetis USBDCD (`kinetis.usbdcd`, ch. 44):** only a standard downstream port is detected (SEQ_RES = 01); charging
  and dedicated charging ports are not modelled. Data-pin contact = the USB0 cable has VBUS (no plug-insertion delay).
  ACTIVE drops to 0 once the standard-host result is reported (unspecified). Timeout and SR as the RM; intervals scale
  with CLOCK_SPEED vs the real bus clock (one `info`). The sample firmware does not use it (register-level tests only).

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

## Host serial bridge and serial cable parts

- **Framing mismatch is drop + warn, not garbage.** A host at another baud rate / frame length than the UART would
  make the receiver decode wrong characters with framing/noise flags; chipem drops the byte (`reason=framing`) and
  logs one `warn` per direction instead. Likewise an inverted line (TTL on an RS-232 port) is modelled as "no valid
  characters" (`polarity`), not as the break/garbage stream a real receiver would produce.
- **PTY line settings.** Linux keeps the speed of a PTY but drops CSIZE and PARENB, so only the baud the PC program
  set is checked (frame: the adapter's `format`, default 8N1). A PTY has no modem lines: DTR/RTS read asserted while
  a program has it open, CTS/DSR/DCD/RI towards the host are recorded but not visible to it. macOS PTYs are expected
  to work the same way but were not tested. No PTYs on Windows: use com0com (`--uart-port`) or `--uart-tcp`.
- **TCP is raw** (no RFC 2217: no baud or modem-line negotiation; telnet negotiation bytes would reach the UART). A
  `--uart-port` URL like `rfc2217://host:port` goes through pyserial's own client.
- **pyserial ports** use null-modem wiring for the modem lines (host RTS → our CTS, host DTR → our DSR); RI towards
  the host cannot be driven. The line settings checked are the ones given on the command line (the other end of a
  null-modem pair is not visible).
- **Real time** follows the wall clock to within one 2 ms slice; when every chip sleeps, host bytes are taken within a
  few ms. Firmware that busy-polls cannot run at real time at 120 MHz (the emulator reaches 0.05-0.2x): the run falls
  behind, one `warn`, then the anchor moves every 200 ms of lag. Pausing (web) and starting a new `run` re-anchor.
- **Byte-level chains.** Bytes cross the cable chain by path resolution at the stop bit; the RS-232 nets show idle
  levels and modem lines but no data bit edges (the UART models do not toggle their TX pins). Path verdicts are
  static per wiring: a modem line or level that changes mid-byte does not corrupt that byte. Voltages are shown in
  part states only (nets are logic levels; on RS-232 nets H = positive voltage).
- **Electrical model.** Ground is checked per segment (driver and receiver must share a conductive ground
  component); a chip's ground is a net with a `gnd` part, `drive: L` or one of its VSS pins, and when the board has
  none, a transceiver's `gnd` port on any grounded net counts. Power pins (VCC, charge-pump capacitors) of the
  transceiver are not modelled; its `shdn_n`/`en_n` follow the MAX3222 (floating = active). RS-485 and TTL-5 V
  adapters are not modelled (`levels: ttl` is 3.3 V).
- **Cable presets**: only DE-9 straight / null-modem; DB9-RJ50 pinouts are vendor specific, give `map:`.

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
- It references DWT, ITM, FPB, TPIU and CRC (stubs), USB0 (modelled; no host is plugged in unless the run adds
  `--usb-host USB0`) and I2C0/1 (modelled, but its sensors are not on any
  board: add `i2c_register_device` parts to answer them). FTM0–2 are modelled (`kinetis.ftm`); its output with and without the FTM models was
  identical in the PWM review run.
- The 0x0–0x3FFF bootloader is not in the file. That region is erased flash (0xFF), so anything that calls
  into the bootloader would fault.
