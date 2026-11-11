"""Kinetis FlexTimer (FTM0-FTM3 on the K22F; K22P100M120SF5V2RM chapter 38, "FlexTimer Module (FTM)").

Counter (16 bit, FTM clock = SC.CLKS: 01 system (bus) clock, 10 fixed frequency clock MCGFFCLK, 11 external clock:
rising edges of FTM_CLKIN0/1 as selected by SIM_SOPT4.FTMxCLKSEL, divided by 2^SC.PS). Counter values wrap at 16 bits
(38.4.3.1):
* up counting (SC.CPWMS=0): CNTIN .. MOD, then CNTIN again: (MOD - CNTIN + 1) mod 2^16 ticks, so signed counting
  (CNTIN = 0xFFFC, MOD = 4: -4 .. 4, 9 ticks) and CNTIN > MOD (through 0xFFFF -> 0x0000, Fig. 38-211) work; TOF when
  the counter goes MOD -> CNTIN. MOD = CNTIN: TOF at every tick (one `warn`).
* free running (38.4.3.3): FTMEN=0 and MOD = 0x0000 or 0xFFFF: 0x0000 .. 0xFFFF, TOF at 0xFFFF -> 0x0000 (every
  65536 ticks). FTMEN=1 and MOD = CNTIN = 0: the counter is stopped (no TOF, no matches); MOD/CNTIN/CnV writes then
  take effect at once.
* up-down counting (CPWMS=1): CNTIN up to MOD and down to CNTIN, 2 x (MOD - CNTIN) ticks; TOF when it goes MOD ->
  MOD-1. MOD = CNTIN stops it.
* CONF.NUMTOF (38.4.3.5): TOF is set at the first overflow and then not for the next NUMTOF overflows; a CNT write
  restarts that count.
* CNT reads are live; any CNT write resets the counter to CNTIN.
* clock changes (SC.CLKS/PS, the bus clock through SIM CLKDIV1 / MCG, MCGFFCLK) keep the counter value and the elapsed
  fraction of the current tick; a counter stopped with CLKS=00 continues from CNT when restarted (no repeated match).

Channels (n = 0..7; pairs m = n/2 for COMBINE/DEADTIME):
* output compare (MSnB:MSnA=01): on CnV match toggle / clear / set (ELSnB:ELSnA 01/10/11); 00 = software compare
  (flag only).
* edge-aligned PWM (MSnB=1, 38.4.6): high-true (ELSnB:ELSnA=10) high from CNTIN while CNT < CnV, low-true (X1) the
  inverse. CnV = CNTIN (CnV = 0 with CNTIN = 0) gives 0 %, CnV > MOD or CnV < CNTIN 100 %; neither sets CHnF.
* center-aligned PWM (CPWMS=1, 38.4.7): high-true is cleared at the match counting up and set at the match counting
  down (high while CNT < CnV up, CNT <= CnV down). CnV = 0 or CnV[15] = 1: 0 %; CnV >= MOD (MOD != 0): 100 %; neither
  sets CHnF.
* combine (COMBINEm): channel 2m is set at C(2m)V and cleared at C(2m+1)V (high-true), low at the start of the period;
  channel 2m+1 is the same, or its complement with COMPm. Dead time (DTENm): each rising edge of the pair is
  delayed by DEADTIME (DTVAL x 1/4/16 system-clock cycles), a pulse shorter than that disappears.
* CHnF at every match (also of channels without an output) and the FTM IRQ for TOF&TOIE | CHnF&CHnIE&!DMA |
  FAULTF&FAULTIE.
  TOF / CHnF / STATUS clear by a read with the flag set followed by a write of 0; a flag set again between the read
  and the write stays set.
* output path: mode -> dead time -> software output control (SWOCTRL) -> POL (inverts) -> OUTMASK (forces the
  inactive state, POLn) -> the pin owned by FTMx_CHn (PinBoard.drive_signal). MODE.INIT loads the channel outputs
  from OUTINIT. ELSnB:ELSnA = 00 (any mode, combine too; RM "pin not used for FTM") makes
  the muxed FTMx_CHn pin an input (PORT `_pin_dir` hook), as in the capture modes: it is never driven, also not
  between the PORT mux write and the channel setup; SWOCTRL forcing a channel drives it. A PWM / combine channel whose
  FTMx_CHn is not muxed to a pin (and without CHnIE/DMA) schedules no edges; it starts when the PORT mux selects it.
* register updates (38.4.10): with the counter clock off (or the counter stopped) MOD/CNTIN/CnV take effect at once.
  CNTIN: at once unless FTMEN=1 and SYNCONF.CNTINC=1. FTMEN=0: CnV of output compare at the next counter tick; MOD and
  PWM CnV at the next counter overflow (EPWM: MOD -> CNTIN, free running 0xFFFF -> 0; CPWM: MOD -> MOD-1). FTMEN=1:
  output compare CnV at the next tick when COMBINE.SYNCENm=0; otherwise from the write buffers at the loading point
  after a software sync (SYNC.SWSYNC; CNTMAX: counter = MOD, CNTMIN: counter = CNTIN, neither: the end of the period;
  SYNC.REINIT also restarts the counter) or at every loading point while PWMLOAD.LDOK is set.
* write protection (38.3.10/38.3.18): FMS.WPEN = 1 (set by writing 1; cleared by reading WPEN = 1 and then writing 1
  to MODE.WPDIS, which reads as its complement) keeps the fields the RM marks "write protected": SC.CPWMS/CLKS/PS,
  CnSC.MSnB/MSnA/ELSnB/ELSnA, MODE.FAULTM/CAPTEST/FTMEN, COMBINE.COMBINEm/COMPm/DECAPENm/DTENm/FAULTENm, DEADTIME,
  POL, FLTCTRL fault/filter enables, QDCTRL.QUADEN, FLTPOL. MOD/CNTIN/CnV/SWOCTRL/OUTMASK/SYNCONF/EXTTRIG are not.
* CNTIN written above the running counter (FTMEN=0, or SYNCONF.CNTINC=0): the counter goes on counting up to MOD and
  an output compare / software compare CnV in that stretch (below CNTIN) still matches; then it reloads CNTIN.

Inputs (pin edges through PinBoard input listeners, attached only while an input feature is on; an edge reaches the
logic sync_clocks = 3 system clocks later, the synchronizer of 38.4.4 / 38.4.16; the digital filters FILTER.CHnFVAL,
FLTCTRL.FFVAL/FFLTRnEN and QDCTRL.PHxFLTREN are ignored, i.e. as if disabled):
* input capture (38.4.4; DECAPEN=COMBINE=CPWMS=0, MSnB:MSnA=00, ELSnB:ELSnA 01 rising / 10 falling / 11 both): CnV =
  the counter 3 system clocks after the FTMx_CHn edge, CHnF, the interrupt with CHnIE. The pin is an input then (the
  PORT asks `_pin_dir`: the FTM does not drive it) and CnV writes are ignored. FTM1/FTM2 channel 0 follow the pin only
  while SIM_SOPT4.FTMxCH0SRC = 00 (CMP / USB SOF sources: no edges). Capture with the counter stopped latches CNT.
* dual edge capture (38.4.24; COMBINE.DECAPENm): only channel 2m's input is used; while DECAPm = 1, the edge selected
  by ELS(2m) goes to channel 2m's capture buffer (CH(2m)F), the next edge selected by ELS(2m+1) moves that buffer to
  C(2m)V and captures into channel 2m+1's buffer (CH(2m+1)F), which reaches C(2m+1)V when C(2m)V is read (read
  coherency, 38.4.24.5). MS(2m)A = 0 one-shot (DECAPm cleared after the second edge), 1 continuous.
* fault control (38.4.16): FTMx_FLTj pins (K22: FTM0 FLT0-3, FTM1/2/3 FLT0; SIM_SOPT4 CMP sources read 0), FLTjPOL,
  FLTCTRL.FAULTjEN, MODE.FAULTM. A rising edge of an enabled input sets FAULTFj and FAULTF and forces the outputs of
  the pairs with COMBINE.FAULTENm (FAULTM = 01: even channels only) to their safe value POLn (after POL, like
  OUTMASK); they are released at the start of a PWM cycle (counter reloaded with CNTIN) once FAULTF is clear (01/10,
  manual) or FAULTIN is 0 (11, automatic). FAULTF/FAULTFj clear by a read of 1 and a write of 0 while the input shows
  no fault; FMS.FAULTIN is live; MODE.FAULTIE adds FAULTF to the FTM interrupt.
* quadrature decoder (38.4.25; QDCTRL.QUADEN with CLKS != 00): FTMx_QD_PHA/PHB pins (K22: FTM1 and FTM2), PHAPOL/
  PHBPOL, QUADMODE 0 phase A/B (x4) or 1 count/direction (phase A rising, B = direction). The counter is pin driven
  between CNTIN and MOD with TOF and TOFDIR on wrap either way, QUADIR; matches of compare channels set CHnF.
* external clock (CLKS=11): each 2^PS-th rising edge of the selected FTM_CLKINx pin is one counter clock, evaluated
  at that instant with the counting / loading / match rules above. The "at most system clock / 4" limit is not
  checked (edges are not synchronized to the system clock).

Outputs to other modules:
* trigger output (38.4.20/21, EXTTRIG): a match of channel j with CHjTRIG (j = 0..5; TRIGF) and, with INITTRIGEN,
  the counter being loaded with CNTIN (period start, CNT write, SYNC REINIT, CLKS started with CNT = CNTIN) call
  `trigger_listeners`: PDB0 (TRGSEL 8..11) and, through SIM_SOPT7 ADCnALTTRGEN=1 / ADCnTRGSEL 8..11, the ADC0/ADC1
  alternate hardware trigger (kinetis.adc). Channels with CHjTRIG schedule their matches even without a pin.
* DMA (38.4.23): CnSC.DMA = CHnIE = 1: CHnF raises DMAMUX source DMA_BASE + n (FTM0 20-27, FTM1 28/29, FTM2 30/31,
  FTM3 32-39) instead of the channel interrupt; the DMA's acknowledge after the minor loop clears CHnF.

Not modelled: INVCTRL, the hardware synchronization triggers (TRIG0-2, SYNC.TRIGn; SIM_SOPT4 FTMxTRGySRC), the CMP
outputs as fault / CH0 capture sources (SIM_SOPT4: read as no fault / no edges), capture test mode (MODE.CAPTEST), the input filters,
global time base, the half-cycle reload, debug mode; the counter is one 16-bit counter of the FTM clock.
"""
from __future__ import annotations

import math
from functools import partial
from typing import Any, Callable

from ...pins import PinBoard
from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, find_model
from .clockutil import bus_hz, irq_number
from .dma import dma_request

TOF, TOIE, CPWMS = 1 << 7, 1 << 6, 1 << 5
CHF, CHIE, MSB, MSA, ELSB, ELSA, DMA = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 3, 1 << 2, 1 << 0
FTMEN, INIT, WPDIS = 1 << 0, 1 << 1, 1 << 2
FAULTIE, FAULTM_SHIFT = 1 << 7, 5                         # MODE
FAULTF, WPEN, FAULTIN = 1 << 7, 1 << 6, 1 << 5            # FMS (FAULTFj: bits 3..0)
TRIGF, INITTRIGEN = 1 << 7, 1 << 6                        # EXTTRIG
QUADEN, TOFDIR, QUADIR, QUADMODE, PHBPOL, PHAPOL = 1, 2, 4, 8, 0x10, 0x20     # QDCTRL
SYNC_CNTMIN, SYNC_CNTMAX, SYNC_REINIT, SYNC_SWSYNC = 1 << 0, 1 << 1, 1 << 2, 1 << 7
CNTINC = 1 << 2                  # SYNCONF
# COMBINE, per pair byte
P_COMBINE, P_COMP, P_DECAPEN, P_DECAP, P_DTEN, SYNCEN, P_FAULTEN = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40
LDOK = 1 << 9
DTPS_FACTOR = (1, 1, 4, 16)
EPS = 1e-6                       # in ticks
_INF = float("inf")
# EXTTRIG CHjTRIG bit of channel j (bit 0 = CH2TRIG ... bit 3 = CH5TRIG, bit 4 = CH0TRIG, bit 5 = CH1TRIG; 38.3.16)
_TRIG_BIT = {2: 0, 3: 1, 4: 2, 5: 3, 0: 4, 1: 5}
# write-protected fields (RM 38.3: "This field is write protected. It can be written only when MODE[WPDIS] = 1")
WP_SC = 0x3F                     # CPWMS, CLKS, PS
WP_CSC = MSB | MSA | ELSB | ELSA
WP_MODE = 0x71                   # FAULTM, CAPTEST, FTMEN
WP = {"COMBINE": 0x57575757, "DEADTIME": 0xFF, "POL": 0xFF, "FLTCTRL": 0xFF, "QDCTRL": QUADEN, "FLTPOL": 0x0F}
# DMAMUX request source of channel 0 (K22P100M120SF5V2RM Table 3-25: 20-27 FTM0, 28/29 FTM1, 30/31 FTM2, 32-39 FTM3)
DMA_BASE = {"FTM0": 20, "FTM1": 28, "FTM2": 30, "FTM3": 32}
# SIM_SOPT4 (12.2.4): FTMxCLKSEL bit (external clock pin FTM_CLKIN0/1), fault input source bits (1 = CMP output, not
# a pin) and the FTM1/FTM2 channel 0 input capture source fields (00 = FTMx_CH0 pin)
SOPT4_CLKSEL = {"FTM0": 24, "FTM1": 25, "FTM2": 26, "FTM3": 27}
SOPT4_FLTSEL = {("FTM0", 0): 0, ("FTM0", 1): 1, ("FTM0", 2): 2, ("FTM1", 0): 4, ("FTM2", 0): 8, ("FTM3", 0): 12}
SOPT4_CH0SRC = {"FTM1": 18, "FTM2": 20}
_CAPTURE = ("ic", "decap", "decap2")
_NOMATCH = ("off", "ic", "decap", "decap2")


class _Ch:
    __slots__ = ("n", "v", "kind", "lowtrue", "oc", "ie", "active", "ref", "target", "applied", "ev", "dt_ev",
                 "out", "lazy_k", "armed", "last_t", "dma", "buf", "stage")

    def __init__(self, n: int):
        self.n = n
        self.v = 0               # active CnV
        self.kind = "off"        # off | sw | swe | swc | oc | ic | decap | decap2 | epwm | cpwm | comb | comb2
        self.lowtrue = False
        self.oc = 0              # output compare action: 0 none, 1 toggle, 2 clear, 3 set
        self.ie = False          # CHIE or DMA: matches need their exact time
        self.dma = False         # DMA and CHIE: CHnF raises a DMA request instead of the interrupt (38.4.23)
        self.buf: int | None = None      # dual edge capture buffer (38.4.24.5)
        self.stage = 0           # dual edge capture (channel n): 0 waiting for the first edge, 1 for the second
        self.active = False      # needs match events (drives a muxed pin or interrupts)
        self.ref = False         # mode output (high-true sense, before dead time/POL)
        self.target = False      # after the mode's polarity, before dead time
        self.applied = False     # after dead time
        self.ev = None           # next match event
        self.dt_ev = None        # pending dead-time rise
        self.out: str | None = None      # level driven on the pin signal (None: not driving)
        self.lazy_k = -1         # quiet channel: last match position already reflected in CHnF
        self.armed = False       # CHnF read as 1 (a following write of 0 clears it)
        self.last_t = -_INF      # time of the last match (never two matches at one instant)


@register("kinetis.ftm")
class FTM(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", name)
        ov: dict[str, dict] = {
            # TOF / CHnF are read-only in the SVD; they clear by "read 1, then write 0", done in the hooks
            "SC": {"on_read": self._sc_read, "on_write": self._sc_write, "ro_mask": 0xFFFFFF00},
            "CNT": {"on_read": self._cnt_read, "on_write": self._cnt_write},
            "MOD": {"on_write": self._buf_write}, "CNTIN": {"on_write": self._buf_write},
            "STATUS": {"on_read": self._status_read, "on_write": self._status_write},
            "MODE": {"on_write": self._mode_write},
            "FMS": {"on_read": self._fms_read, "on_write": self._fms_write, "ro_mask": 0},
            "SYNC": {"on_write": self._sync_write},
            "COMBINE": {"on_write": self._cfg_write}, "POL": {"on_write": self._cfg_write},
            "OUTMASK": {"on_write": self._cfg_write}, "SWOCTRL": {"on_write": self._cfg_write},
            "DEADTIME": {"on_write": self._cfg_write}, "QDCTRL": {"on_write": self._qd_write},
            "PWMLOAD": {"on_write": self._cfg_write},
            "FLTCTRL": {"on_write": self._cfg_write}, "FLTPOL": {"on_write": self._cfg_write},
            "EXTTRIG": {"on_read": self._exttrig_read, "on_write": self._exttrig_write, "ro_mask": 0xFFFFFF00},
        }
        for n in range(8):
            ov[f"C{n}SC"] = {"on_read": self._csc_read(n), "on_write": self._csc_write(n), "ro_mask": 0xFFFFFF02}
            ov[f"C{n}V"] = {"on_write": self._buf_write, "on_read": self._cv_read(n)}
        regs_ = build_regs((name, "FTM0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs_, log=log, params=params)
        self.ch = [_Ch(n) for n in range(8) if f"C{n}SC" in self.regs]
        self._fms_off = self.regs["FMS"].offset
        # input synchronizer: a pin edge reaches the capture / fault logic 3 system clocks later (38.4.4, 38.4.16)
        self.sync_clocks = int(params.get("sync_clocks", 3))
        self._sync_evs: list = []        # pending synchronized input edges (cancelled on reset)
        self._listening = False          # input listeners on the PinBoard (only while an input feature is on)
        self._in_pins: list = []
        self._in_last: dict[str, int] = {}
        self._routes: dict[str, Any] = {}
        self._in_mask = -1               # channels whose FTMx_CHn pin is an input (capture modes, ELS = 00)
        self._trig_mask = 0              # channels with EXTTRIG CHjTRIG
        self._fmask = 0                  # channels whose outputs fault control forces to the safe value
        self._fault = False              # a fault condition holds the outputs at their safe values
        self._fault_irq = False          # FMS.FAULTF and MODE.FAULTIE
        self._fms_armed = 0              # FAULTF / FAULTFj read as 1 (a following write of 0 clears)
        self._trigf_armed = False
        self._init_trig = False          # EXTTRIG.INITTRIGEN
        self._ext_div = 0                # external clock: edges counted towards the prescaler
        self._qd = [0, 0]                # quadrature decoder: last phase A / B pin levels
        self._dma_q: list[_Ch] = []
        self.trigger_listeners: list[Callable[[Any], None]] = []    # FTM trigger output (EXTTRIG) users: PDB
        self.triggers = 0
        self.captures = 0
        self._ev = None              # next boundary event (mid: TOF of up-down counting; end of the period)
        self._irqn: int | None = None
        self._line = False
        self.running = False
        self.mod = self.cntin = 0    # active values (write buffers: the MOD/CNTIN registers)
        self._c0 = 0                 # counter value at position 0 (CNTIN; 0 when free running)
        self._mm = 0                 # MOD - CNTIN (mod 2^16; 0xFFFF free running)
        self._halt = False           # counter stopped by its configuration (FTMEN=1, MOD=CNTIN=0)
        self._start_ns = 0.0         # anchor time
        self._p0: float = 0          # position in the period (ticks since CNTIN counting up) at the anchor
        self._tick_ns = 0.0
        self._fresh = True           # counter just (re)loaded: a match at its position is still to come
        self._down = False           # counting down when stopped (up-down counting)
        self._tofcnt = 0             # NUMTOF counter
        self._pending = False        # write buffers differ from the active values
        self._sync = False           # software sync requested (SWSYNC), loads at the next loading point
        self._tof_armed = False
        self._status_armed = 0
        self._wpen_armed = False
        self._quiet: list[_Ch] = []
        self._warned: set[str] = set()
        self._prefix = f"{name}_CH"
        self.overflows = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, str(self.params.get("irq") or self.name))
        mcg = find_model(chip, "kinetis.mcg", "MCG")
        if mcg is not None and hasattr(mcg, "listeners"):
            mcg.listeners.append(self._clock_changed)
        pins = getattr(chip, "pins", None)
        if pins is not None:
            pins.subscribe(self._pin_event)
            seen: set[str] = set()
            for sig in self._input_signals():
                for p in pins.signal_pins(sig):
                    if p.id not in seen:
                        seen.add(p.id)
                        self._in_pins.append(p)
        mux = find_model(chip, "kinetis.dmamux")
        base = self._dma_base()
        if mux is not None and base is not None:
            for c in self.ch:
                mux.register_source(base + c.n, partial(self._dma_active, c))
                if hasattr(mux, "register_ack"):
                    mux.register_ack(base + c.n, partial(self._dma_done, c))
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.port" or type(b).__name__ == "PORT":
                hooks = getattr(b, "signal_dir_hooks", None)
                if hooks is not None and self._pin_dir not in hooks:
                    hooks.append(self._pin_dir)

    def _input_signals(self) -> list[str]:
        n = self.name
        sigs = [f"{n}_CH{c.n}" for c in self.ch] + [f"{n}_FLT{j}" for j in range(4)]
        return sigs + [f"{n}_QD_PHA", f"{n}_QD_PHB", "FTM_CLKIN0", "FTM_CLKIN1"]

    def _dma_base(self) -> int | None:
        b = self.params.get("dma_source", DMA_BASE.get(self.name))
        return None if b is None else int(b)

    def reset(self) -> None:
        if getattr(self, "ch", None) is not None:
            self._stop()
            for ev in self._sync_evs:
                if self.chip is not None:
                    self.chip.sched.cancel(ev)
            self._sync_evs = []
            for c in self.ch:
                self._cancel_dt(c)
                c.ref = c.target = c.applied = False
                c.v, c.lazy_k, c.armed, c.last_t = 0, -1, False, -_INF
                c.buf, c.stage, c.dma = None, 0, False
                if c.out is not None and self.chip is not None and self.chip.pins is not None:
                    self.chip.pins.release_signal(self._sig(c), "L")
                c.out = None
            if self._line and self.chip is not None and self._irqn is not None:
                self.irq(self._irqn, False)
        self._line = False
        super().reset()
        if getattr(self, "ch", None) is None:
            return
        self.mod = self.cntin = 0
        self._p0 = 0
        self._pending = self._sync = self._tof_armed = self._wpen_armed = self._down = False
        self._fault = self._fault_irq = self._trigf_armed = self._init_trig = False
        self._fresh = True
        self._status_armed = self._tofcnt = self._fms_armed = self._ext_div = 0
        self._dma_q = []
        self._geom()
        self._chan_cfg()

    def _sig(self, c: _Ch) -> str:
        return f"{self.name}_CH{c.n}"

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    # ------------------------------------------------------------ clock and counting geometry
    def counter_hz(self) -> float:
        """Frequency of the time-driven counter; 0 when it is stopped (CLKS=00) or driven by pin edges (CLKS=11
        external clock, QDCTRL.QUADEN quadrature decoder: see _tick_step / _qd_count)."""
        sc = self.get("SC")
        clks = (sc >> 3) & 3
        if clks == 1:
            hz = bus_hz(self.chip)
        elif clks == 2:
            mcg = find_model(self.chip, "kinetis.mcg", "MCG")
            hz = mcg.ffclk_hz() if mcg is not None and hasattr(mcg, "ffclk_hz") else 32768.0
        else:
            return 0.0
        if self.get("QDCTRL") & QUADEN:
            return 0.0
        return hz / (1 << (sc & 7))

    def _cpwm(self) -> bool:
        return bool(self.mem[0] & CPWMS)             # SC is at offset 0

    def _geom(self) -> None:
        """Counting range from the active MOD/CNTIN, FTMEN and CPWMS (38.4.3)."""
        cp = self._cpwm()
        ftmen = self.get("MODE") & FTMEN
        if not ftmen and not cp and self.mod in (0, 0xFFFF):     # free running counter
            self._c0, self._mm, self._halt = 0, 0xFFFF, False
            return
        self._c0 = self.cntin
        self._mm = (self.mod - self.cntin) & 0xFFFF
        self._halt = (cp and self._mm == 0) or (bool(ftmen) and not cp and self.mod == 0 and self.cntin == 0)

    def _period(self) -> int:
        m = self._mm
        return max(2 * m, 1) if self._cpwm() else m + 1

    def _counter(self, p: int) -> tuple[int, bool]:
        """(counter value, counting down) at position p of the period."""
        m = self._mm
        if p > m and self._cpwm():
            return (self._c0 + 2 * m - p) & 0xFFFF, True
        return (self._c0 + p) & 0xFFFF, False

    def _cnt_at(self, ip: int) -> tuple[int, bool]:
        per = self._period()
        if ip >= per:                                # the end of the period, its event not handled yet
            ip -= per
        return self._counter(ip)

    def _pos_from_cnt(self, cnt: int, down: bool) -> int:
        """Position in the period of counter value cnt (negative: below CNTIN, counting up to MOD)."""
        m, c0 = self._mm, self._c0
        d = (cnt - c0) & 0xFFFF
        if self._cpwm():
            if d > m:
                return m + 1 if down and m > 0 else 0
            return 2 * m - d if down and 0 < d < m else d
        if d <= m:
            return d
        return m - ((((c0 + m) & 0xFFFF) - cnt) & 0xFFFF)

    def _boundaries(self) -> list[tuple[int, str]]:
        """Positions of the period's boundary events: (M+1, 'mid') when up-down counting (TOF, MOD -> MOD-1) and
        (period, 'end') (up counting: TOF, MOD -> CNTIN; up-down: back at CNTIN)."""
        p = self._period()
        if self._cpwm() and self._mm > 0:
            return [(self._mm + 1, "mid"), (p, "end")]
        return [(p, "end")]

    def _pos_now(self) -> float:
        if not self.running or self._tick_ns <= 0:
            return float(self._p0)
        now = self.lp_t0_ns if self.lp_frozen else self.chip.now_ns()
        return self._p0 + (now - self._start_ns) / self._tick_ns

    def _t(self, pos: float) -> float:
        return self._start_ns + (pos - self._p0) * self._tick_ns

    # ------------------------------------------------------------ start / stop / anchor
    def _cancel(self) -> None:
        self._quiet = []
        sched = self.chip.sched if self.chip is not None else None
        if self._ev is not None and sched is not None:
            sched.cancel(self._ev)
        self._ev = None
        for c in getattr(self, "ch", ()):
            if c.ev is not None and sched is not None:
                sched.cancel(c.ev)
            c.ev = None

    def _stop(self) -> tuple[int, bool, float]:
        """Stop counting: flags of quiet channels brought up to date, events cancelled, CNT holds the counter.
        Returns (counter, counting down, elapsed fraction of the current tick)."""
        if self.running:
            pos = self._pos_now()
            if self._quiet:
                self._lazy_flags(pos)
            ip = math.floor(pos + EPS)
            cnt, down = self._cnt_at(ip)
            self.set("CNT", cnt)
            self._down = down
            self._fresh = False
            st = (cnt, down, max(pos - ip, 0.0))
        else:
            st = (self.get("CNT") & 0xFFFF, self._down, 0.0)
        self._cancel()
        self.running = False
        return st

    def _start(self, p0: float, t_ns: float | None = None, fresh: bool = False) -> None:
        """Anchor the counter at period position p0 at time t_ns (default now) and schedule what follows. fresh: the
        counter has just been loaded with this value (a match at p0 happens now); otherwise matches at p0 are past."""
        self._cancel()
        hz = self.counter_hz() if self.chip is not None else 0.0
        if hz <= 0 or self._halt:
            self.running = False
            self._resync(0)
            return
        if self._mm == 0 and not self._cpwm():
            self._warn_once("modeq", "MOD = CNTIN: the counter overflows (TOF) at every FTM clock")
        self.running = True
        self._tick_ns = 1e9 / hz
        self._start_ns = self.chip.now_ns() if t_ns is None else t_ns
        self._p0 = p0
        self._schedule_boundary(p0)
        self._resync(math.ceil(p0 - EPS) if fresh else math.floor(p0 + EPS) + 1)

    def _resume(self, st: tuple[int, bool, float], fresh: bool = False) -> None:
        """Continue from a _stop() state in the (possibly changed) geometry and clock."""
        cnt, down, frac = st
        self._start(self._pos_from_cnt(cnt, down) + (0.0 if fresh else frac), fresh=fresh)

    def _schedule_boundary(self, after: float) -> None:
        for pos, kind in self._boundaries():
            if pos > after + EPS:
                self._ev = self.chip.sched.at_ns(self._t(pos), self._boundary, kind, pos)
                return

    lp_frozen = False                                   # set by the SMC freezer while the FTM clocks are stopped

    def time_shift(self, dns: float, dcycles: int) -> None:
        """Stop modes (38.1.3: "all FTM input clocks are stopped"): the SMC held this FTM's events for the time
        slept; move the counter anchor too, so CNT continues from the value it had at the stop."""
        self._start_ns += dns
        for c in self.ch:
            if c.last_t != -_INF:
                c.last_t += dns

    def _clock_changed(self) -> None:
        if not self.running or self.chip is None:
            return
        hz = self.counter_hz()
        if hz > 0 and abs(hz * self._tick_ns - 1e9) < 1e-3:
            return                                      # this FTM's clock did not change
        self._resume(self._stop())

    # ------------------------------------------------------------ boundaries, loading, TOF
    def _boundary(self, kind: str, pos: int) -> None:
        self._ev = None
        if self._quiet:
            self._lazy_flags(pos)
        t = self._t(pos)
        cpwm = self._cpwm()
        if kind == "mid" or not cpwm:                    # TOF: MOD -> MOD-1 (up-down) / MOD -> CNTIN (up)
            self.overflows += 1
            self._overflow()
        load = self._load_due(kind)
        if kind == "end":
            if load:
                self._load()
            self._start(0, t, fresh=True)
            self._period_start()
            return
        if load:                                         # mid-period load (up-down): keep counting down from here
            cnt = self._counter(pos)[0]
            self._load()
            self._start(self._pos_from_cnt(cnt, True), t, fresh=True)
            return
        per = self._period()
        self._ev = self.chip.sched.at_ns(self._t(per), self._boundary, "end", per)

    def _load_due(self, kind: str) -> bool:
        """Do the write buffers load at this boundary ('mid': MOD -> MOD-1 up-down; 'end': the counter reloads)?"""
        if not (self._pending or self._sync):
            return False
        cpwm = self._cpwm()
        if not self.get("MODE") & FTMEN:
            return (kind == "mid") if cpwm else True
        if self._sync or self.get("PWMLOAD") & LDOK:
            sync = self.get("SYNC")
            pts = sync & (SYNC_CNTMIN | SYNC_CNTMAX)
            if not pts:
                return kind == "end"
            if kind == "mid":
                return bool(sync & SYNC_CNTMAX)
            return bool(sync & SYNC_CNTMIN) or (not cpwm and bool(sync & SYNC_CNTMAX))
        return False

    def _period_start(self) -> None:
        """The counter was loaded with CNTIN by the counting mode (a new PWM cycle): automatic / manual fault
        recovery (38.4.16.1/2) and the initialization trigger (38.4.21)."""
        if self._fault:
            fm = (self.get("MODE") >> FAULTM_SHIFT) & 3
            if fm == 0 or (fm == 3 and not self._faultin()) or (fm != 3 and not self.get("FMS") & FAULTF):
                self._fault = False
                for c in self.ch:
                    self._drive(c)
        if self._init_trig:
            self._fire_trigger()

    def _overflow(self) -> None:
        """A counter overflow: TOF unless CONF.NUMTOF skips it."""
        if self._tofcnt == 0:
            self.set("SC", self.get("SC") | TOF)
            self._tof_armed = False                      # set again: a pending read-then-write-0 no longer clears
            self._update_line()
        self._tofcnt = self._tofcnt + 1 if self._tofcnt < (self.get("CONF") & 0x1F) else 0

    def _load(self) -> None:
        self.mod = self.get("MOD") & 0xFFFF
        self.cntin = self.get("CNTIN") & 0xFFFF
        for c in self.ch:
            c.v = self.get(f"C{c.n}V") & 0xFFFF
        self._pending = False
        if self._sync:
            self._sync = False
            self.set("SYNC", self.get("SYNC") & ~SYNC_SWSYNC)
        self._geom()

    # ------------------------------------------------------------ channel configuration
    def _chan_cfg(self) -> None:
        comb = self.get("COMBINE")
        cpwm = self._cpwm()
        for c in self.ch:
            sc = self.get(f"C{c.n}SC")
            els = (sc >> 2) & 3
            ms = (sc >> 4) & 3
            m = c.n // 2
            pair = (comb >> (8 * m)) & 0xFF
            c.ie = bool(sc & (CHIE | DMA))
            c.dma = bool(sc & DMA and sc & CHIE)
            c.oc = 0
            if pair & P_DECAPEN:                                         # dual edge capture (38.4.24)
                c.kind = "decap" if c.n % 2 == 0 else "decap2"
            elif pair & 0x01 and not cpwm:                               # COMBINEm
                if c.n % 2 == 0:
                    c.kind, c.lowtrue = "comb", bool(els & 1)
                else:
                    c.kind, c.lowtrue = "comb2", False
            elif cpwm:
                c.kind, c.lowtrue = ("cpwm" if els else "swc"), bool(els & 1)
            elif ms == 0:
                c.kind = "ic" if els else "off"
            elif ms == 1:
                c.kind, c.oc = ("oc" if els else "sw"), els
            else:
                c.kind, c.lowtrue = ("epwm" if els else "swe"), bool(els & 1)
            if c.kind not in ("decap", "decap2"):
                c.stage, c.buf = 0, None
        ext = self.get("EXTTRIG")
        self._trig_mask = sum(1 << j for j, b in _TRIG_BIT.items() if ext >> b & 1)
        self._init_trig = bool(ext & INITTRIGEN)
        fm = (self.get("MODE") >> FAULTM_SHIFT) & 3
        fmask = 0
        if fm:
            for c in self.ch:
                if (comb >> (8 * (c.n // 2))) & P_FAULTEN and (fm != 1 or c.n % 2 == 0):
                    fmask |= 1 << c.n
        self._fmask = fmask
        in_mask = 0
        for c in self.ch:
            k = c.kind
            if k in _NOMATCH:
                c.active = False
                if k in _CAPTURE:
                    in_mask |= 1 << c.n
            elif k == "oc":
                c.active = True                          # the output state (toggle) depends on every match
            elif k in ("epwm", "cpwm"):
                c.active = c.ie or self._visible(c)      # the output follows the counter: no pin, no events
            elif k in ("comb", "comb2"):
                b = c.n & ~1
                vis = any(self._drives(x) and self._visible(x) for x in self.ch[b:b + 2])
                c.active = c.ie or vis
            else:
                c.active = c.ie
            if self._trig_mask >> c.n & 1 and k not in _NOMATCH:
                c.active = True                          # channel trigger: the match time matters
        cap_mask = in_mask
        sw = self.get("SWOCTRL")
        for c in self.ch:                                # ELSnB:ELSnA = 00 (any mode): the pin is not used by the FTM
            if not self._drives(c) and not (sw >> c.n) & 1:
                in_mask |= 1 << c.n
        if in_mask != self._in_mask:
            self._in_mask = in_mask
            self._refresh_ports()
        qd = self.get("QDCTRL") & QUADEN
        need = bool(cap_mask or (fm and self.get("FLTCTRL") & 0xF) or qd or (self.get("SC") >> 3) & 3 == 3)
        if need != self._listening:
            self._listen(need)

    def _visible(self, c: _Ch) -> bool:
        """Is FTMx_CHn muxed to a pin as an output?"""
        pins = self.chip.pins if self.chip is not None else None
        if pins is None:
            return False
        sig = self._sig(c)
        return any(p.direction == "out" and pins.signal_matches(p.owner, sig) for p in pins.signal_pins(sig))

    def _pin_event(self, ev: Any) -> None:
        f = ev.field
        if f != "owner" and f != "direction":
            return
        last = self._in_last
        if ev.pin in last:                       # a mux / direction change is not an input edge
            last[ev.pin] = 1 if self.chip.pins.get(ev.pin).level() == "H" else 0
        pre = self._prefix
        if f == "owner":
            hit = pre in str(ev.new) or pre in str(ev.old)
        else:
            hit = pre in str(self.chip.pins.get(ev.pin).owner)
        if hit and getattr(self, "ch", None):
            self._config_changed()

    def _drives(self, c: _Ch) -> bool:
        if c.kind in ("oc", "epwm", "cpwm"):
            return True
        if c.kind == "comb" or c.kind == "comb2":
            return bool((self.get(f"C{c.n}SC") >> 2) & 3)
        return False

    def _wp(self, old: int, new: int, mask: int) -> int:
        """Write protection (FMS.WPEN = 1): the protected fields keep their value."""
        if self.mem[self._fms_off] & WPEN:
            return (new & ~mask) | (old & mask)
        return new

    def _cfg_write(self, blk, reg, old, new):
        m = WP.get(reg.name)
        if m:
            new = self._wp(old, new, m)
        self._store(reg, new)
        if reg.name == "COMBINE":
            for c in self.ch[::2]:                          # DECAP set: a new dual edge measurement starts
                sh = 8 * (c.n // 2)
                if (new >> sh) & P_DECAP and not (old >> sh) & P_DECAP:
                    c.stage = 0
        self._config_changed()
        if reg.name in ("FLTCTRL", "MODE"):
            self._fault_flags_changed()
        return new

    def _qd_write(self, blk, reg, old, new):
        new = self._wp(old, new, QUADEN)
        if (old ^ new) & QUADEN:                            # the counter becomes pin driven / time driven again
            self._store(reg, old)
            st = self._stop()
            self._store(reg, new)
            self._chan_cfg()
            self._resume(st, fresh=self._fresh)
        else:
            self._store(reg, new)
            self._config_changed()
        return new

    def _exttrig_read(self, blk, reg):
        if self.get("EXTTRIG") & TRIGF:
            self._trigf_armed = True
        return None

    def _exttrig_write(self, blk, reg, old, new):
        trigf = old & TRIGF
        if not new & TRIGF and self._trigf_armed:
            trigf = 0                                       # read-1-then-write-0 clears TRIGF
        self._trigf_armed = False
        new = (new & 0x7F) | trigf
        self._store(reg, new)
        if (old ^ new) & 0x7F:
            self._config_changed()
        return new

    def _config_changed(self) -> None:
        if self._quiet and self.running:
            self._lazy_flags(self._pos_now())
        self._chan_cfg()
        self._resync()

    # ------------------------------------------------------------ input pins (capture, faults, QD, external clock)
    def _refresh_ports(self) -> None:
        """A channel switched between an input (capture) and an output mode: let the PORT models re-derive the
        direction of pins muxed to FTMx_CHn (`_pin_dir`)."""
        if self.chip is None:
            return
        pre = self._prefix
        for b in self.chip.peripherals.values():
            f = getattr(b, "refresh_function", None)
            if f is not None and getattr(b, "model", None) == "kinetis.port":
                f(lambda owner: pre in str(owner))

    def _pin_dir(self, owner: str) -> tuple[str, Any] | None:
        """PORT hook: (direction, drive) of a pin muxed to `owner` when it is one of this FTM's channels that does not
        drive its pin (input capture, dual edge capture, ELSnB:ELSnA = 00 "pin not used for FTM"), else None (signal
        table: an output)."""
        if not self._in_mask or self._prefix not in owner:
            return None
        for c in self.ch:
            if self._in_mask >> c.n & 1 and PinBoard.signal_matches(owner, self._sig(c)):
                return "in", None
        return None

    def _listen(self, on: bool) -> None:
        """Attach / detach the PinBoard input listeners of the FTM input pins (none while no input feature is on:
        the PWM output pins then cost nothing extra)."""
        if self.chip is None or self.chip.pins is None:
            return
        self._listening = on
        lst = self.chip.pins.input_listeners
        cb = self._on_input
        for p in self._in_pins:
            cbs = lst[p.id]
            if on:
                self._in_last[p.id] = 1 if p.level() == "H" else 0
                cbs.append(cb)
            elif cb in cbs:
                cbs.remove(cb)
        if on:
            self._qd = [self._sig_level(f"{self.name}_QD_PHA"), self._sig_level(f"{self.name}_QD_PHB")]

    def _sig_level(self, sig: str) -> int | None:
        """Level (1/0) of the input pin currently muxed to `sig`; None if no pin carries it as an input."""
        pins = self.chip.pins if self.chip is not None else None
        if pins is None:
            return None
        for p in pins.signal_pins(sig):
            if p.direction == "in" and PinBoard.signal_matches(p.owner, sig):
                return 1 if p.level() == "H" else 0
        return None

    def _route(self, owner: str) -> tuple[str, int] | None:
        r = self._routes.get(owner, False)
        if r is not False:
            return r
        r = None
        n = self.name
        for c in self.ch:
            if PinBoard.signal_matches(owner, f"{n}_CH{c.n}"):
                r = ("ch", c.n)
        for j in range(4):
            if PinBoard.signal_matches(owner, f"{n}_FLT{j}"):
                r = ("flt", j)
        for j, ph in enumerate(("PHA", "PHB")):
            if PinBoard.signal_matches(owner, f"{n}_QD_{ph}"):
                r = ("qd", j)
        for j in range(2):
            if PinBoard.signal_matches(owner, f"FTM_CLKIN{j}"):
                r = ("clk", j)
        self._routes[owner] = r
        return r

    def _sopt4(self) -> int:
        sim = self._sim
        return sim.get("SOPT4") if sim is not None and "SOPT4" in sim.regs else 0

    def _on_input(self, pin: Any) -> None:
        """PinBoard input listener: a level change on a pin that can carry one of this FTM's input signals."""
        if pin.direction != "in":
            return
        pid = pin.id
        lvl = 1 if pin.level() == "H" else 0
        if self._in_last.get(pid) == lvl:
            return
        self._in_last[pid] = lvl
        if self.lp_frozen:
            return                                      # Stop modes: FTM clocks stopped (38.1.3), the edge is lost
        r = self._route(pin.owner)
        if r is None:
            return
        kind, j = r
        if kind == "ch":
            c = self.ch[j]
            if c.kind not in ("ic", "decap"):
                return
            src = SOPT4_CH0SRC.get(self.name)
            if j == 0 and src is not None and (self._sopt4() >> src) & 3:
                return                                  # channel 0 input is a CMP output / USB SOF, not the pin
            self._synced(self._capture, j, bool(lvl))
        elif kind == "flt":
            sel = SOPT4_FLTSEL.get((self.name, j))
            if sel is not None and (self._sopt4() >> sel) & 1:
                return                                  # fault input j is a CMP output
            if lvl ^ ((self.get("FLTPOL") >> j) & 1):   # rising edge of the (polarity adjusted) fault input
                self._synced(self._fault_edge, j)
        elif kind == "qd":
            self._qd_edge(j, lvl)
        elif kind == "clk":
            sc = self.get("SC")
            if lvl and (sc >> 3) & 3 == 3 and (self._sopt4() >> SOPT4_CLKSEL.get(self.name, 24)) & 1 == j:
                self._ext_edge()

    def _synced(self, fn: Callable, *args: Any) -> None:
        """Run fn after the input synchronizer delay (sync_clocks system clocks)."""
        hz = bus_hz(self.chip)
        if self.sync_clocks <= 0 or hz <= 0:
            fn(*args)
            return
        holder: list = []
        ev = self.chip.sched.at_ns(self.chip.now_ns() + self.sync_clocks * 1e9 / hz, self._sync_fire, holder, fn,
                                   args)
        holder.append(ev)
        self._sync_evs.append(ev)

    def _sync_fire(self, holder: list, fn: Callable, args: tuple) -> None:
        if holder and holder[0] in self._sync_evs:
            self._sync_evs.remove(holder[0])
        fn(*args)

    def _cnt_now(self) -> int:
        """The FTM counter now (live while the counter runs, else the CNT register)."""
        if self.running:
            return self._cnt_at(math.floor(self._pos_now() + EPS))[0]
        return self.get("CNT") & 0xFFFF

    # ------------------------------------------------------------ input capture (38.4.4) and dual edge capture
    def _capture(self, n: int, rising: bool) -> None:
        c = self.ch[n]
        sc = self.get(f"C{n}SC")
        els = (sc >> 2) & 3
        if c.kind == "ic":
            if not (els & 1 if rising else els & 2):
                return
            v = self._cnt_now()
            self.set(f"C{n}V", v)
            c.v = v
            self.captures += 1
            self._flag(c)
        elif c.kind == "decap" and n + 1 < len(self.ch):
            self._decap_edge(c, self.ch[n + 1], rising)
        else:
            return
        self._update_line()
        self._flush_dma()

    def _decap_edge(self, c0: _Ch, c1: _Ch, rising: bool) -> None:
        """Dual edge capture (38.4.24): channel n captures the edge selected by ELSn into its buffer, channel n+1 the
        next edge selected by ELS(n+1); at the second edge C(n)V gets channel n's buffer and C(n+1)V gets its buffer
        when C(n)V is read (read coherency, 38.4.24.5). One-shot (MS(n)A=0) clears DECAP after the second edge."""
        m = c0.n // 2
        comb = self.get("COMBINE")
        if not (comb >> (8 * m)) & P_DECAP:
            return
        sel0 = (self.get(f"C{c0.n}SC") >> 2) & 3
        sel1 = (self.get(f"C{c1.n}SC") >> 2) & 3
        if c0.stage == 1 and not self.get(f"C{c0.n}SC") & CHF:
            c0.stage = 0                                    # CH(n)F cleared in between: start again
        v = self._cnt_now()
        if c0.stage == 0:
            if sel0 & 1 if rising else sel0 & 2:
                c0.buf = v
                c0.stage = 1
                self.captures += 1
                self._flag(c0)
            return
        if not (sel1 & 1 if rising else sel1 & 2):
            return
        c0.v = c0.buf if c0.buf is not None else 0
        self.set(f"C{c0.n}V", c0.v)
        c1.buf = v
        c0.stage = 0
        self.captures += 1
        self._flag(c1)
        if not self.get(f"C{c0.n}SC") & MSA:               # one-shot: DECAP cleared once both edges are captured
            self.set("COMBINE", comb & ~(P_DECAP << (8 * m)))

    def _cv_read(self, n: int):
        def rd(blk, reg):
            if n < len(self.ch) and self.ch[n].kind == "decap" and n + 1 < len(self.ch):
                c1 = self.ch[n + 1]
                if c1.buf is not None:                      # C(n)V read: C(n+1)V gets its capture buffer
                    c1.v = c1.buf
                    self.set(f"C{c1.n}V", c1.buf)
                    c1.buf = None
            return None
        return rd

    # ------------------------------------------------------------ fault control (38.4.16)
    def _fault_value(self, j: int) -> int:
        """Fault input j after FLTjPOL (1 = fault); a CMP-sourced or unmuxed input reads 0."""
        sel = SOPT4_FLTSEL.get((self.name, j))
        if sel is None or (self._sopt4() >> sel) & 1:
            return 0
        lvl = self._sig_level(f"{self.name}_FLT{j}")
        if lvl is None:
            return 0
        return lvl ^ ((self.get("FLTPOL") >> j) & 1)

    def _faultin(self) -> bool:
        """FMS.FAULTIN: the OR of the enabled fault inputs while fault control is enabled."""
        if not (self.get("MODE") >> FAULTM_SHIFT) & 3:
            return False
        en = self.get("FLTCTRL") & 0xF
        return any(en >> j & 1 and self._fault_value(j) for j in range(4))

    def _fault_edge(self, j: int) -> None:
        if not (self.get("MODE") >> FAULTM_SHIFT) & 3 or not (self.get("FLTCTRL") >> j) & 1:
            return
        self.set("FMS", self.get("FMS") | FAULTF | (1 << j))
        self._fms_armed = 0                                 # a new fault resets a pending clearing sequence
        self.emit("info", block=self.name, msg=f"{self.name}: fault input {j} detected; outputs at their safe value")
        self._fault = True
        for c in self.ch:
            self._drive(c)
        self._fault_flags_changed()

    def _fault_flags_changed(self) -> None:
        self._fault_irq = bool(self.get("FMS") & FAULTF and self.get("MODE") & FAULTIE)
        self._update_line()

    # ------------------------------------------------------------ triggers (38.4.20/21) and DMA (38.4.23)
    def _fire_trigger(self) -> None:
        self.triggers += 1
        for cb in self.trigger_listeners:
            cb(self)

    def _channel_trigger(self) -> None:
        self.set("EXTTRIG", self.get("EXTTRIG") | TRIGF)
        self._trigf_armed = False
        self._fire_trigger()

    def _dma_active(self, c: _Ch) -> bool:
        sc = self.get(f"C{c.n}SC")
        return bool(sc & CHF and sc & DMA and sc & CHIE)

    def _dma_done(self, c: _Ch) -> None:
        """The DMA serviced the channel's request: CHnF clears (Table 38-312)."""
        sc = self.get(f"C{c.n}SC")
        if sc & CHF and sc & DMA:
            self.set(f"C{c.n}SC", sc & ~CHF)
            c.armed = False
            self._update_line()

    def _flush_dma(self) -> None:
        q = self._dma_q
        if not q:
            return
        self._dma_q = []
        base = self._dma_base()
        if base is None or self.chip is None:
            return
        for c in q:
            if self._dma_active(c):
                dma_request(self.chip, base + c.n)

    # ------------------------------------------------------------ pin-clocked counting: external clock, QD
    def _ext_edge(self) -> None:
        """A rising edge of the selected FTM_CLKINx pin (CLKS=11): one FTM clock every 2^PS edges."""
        self._ext_div += 1
        if self._ext_div >= 1 << (self.get("SC") & 7):
            self._ext_div = 0
            if not self.get("QDCTRL") & QUADEN:
                self._tick_step()

    def _tick_step(self) -> None:
        """One counter clock of a counter that is not time driven (external clock): the same counting, boundary,
        loading and match rules as the scheduled counter, evaluated at this instant."""
        if self._halt or self.chip is None:
            return
        cnt, down = self.get("CNT") & 0xFFFF, self._down
        q = self._pos_from_cnt(cnt, down) + 1
        now = self.chip.now_ns()
        self._start_ns, self._p0, self._tick_ns = now, q, 0.0     # _t(k) == now for the match bookkeeping
        kind = None
        for pos, k in self._boundaries():
            if pos == q:
                kind = k
        period_start = False
        if kind is not None:
            if kind == "mid" or not self._cpwm():
                self.overflows += 1
                self._overflow()
            load = self._load_due(kind)
            if kind == "end":
                if load:
                    self._load()
                q = 0
                period_start = True
            elif load:
                c_ = self._counter(q)[0]
                self._load()
                q = self._pos_from_cnt(c_, True)
            self._p0 = q
        cnt2, down2 = self._counter(q)
        self.set("CNT", cnt2)
        self._down = down2
        self._step_matches(q)
        self._resync()
        if period_start:
            self._period_start()
        self._flush_dma()

    def _step_matches(self, q: int) -> None:
        for c in self.ch:
            if c.kind not in _NOMATCH and q in self._match_positions(c):
                self._match(c, q, False)

    def _qd_edge(self, which: int, lvl: int) -> None:
        """Quadrature decoder (38.4.25): a phase A (0) / B (1) pin edge."""
        self._qd[which] = lvl
        qdc = self.get("QDCTRL")
        if not qdc & QUADEN or not (self.get("SC") >> 3) & 3:
            return
        a, b = self._qd
        a = (a or 0) ^ bool(qdc & PHAPOL)
        b = (b or 0) ^ bool(qdc & PHBPOL)
        if qdc & QUADMODE:                                  # count and direction: count at phase A rising edges
            if which != 0 or not a:
                return
            up = bool(b)
        elif which == 0:                                    # phase A and B encoding
            up = (not b) if a else bool(b)
        else:
            up = bool(a) if b else (not a)
        self._qd_count(up)

    def _qd_count(self, up: bool) -> None:
        if self._halt or self.chip is None:
            return
        lo, hi = self._c0, (self._c0 + self._mm) & 0xFFFF
        cnt = self.get("CNT") & 0xFFFF
        qdc = self.get("QDCTRL") & ~(QUADIR | TOFDIR)
        wrap = False
        if up:
            qdc |= QUADIR
            if cnt == hi:
                cnt, wrap = lo, True
                qdc |= TOFDIR
            else:
                cnt = (cnt + 1) & 0xFFFF
        else:
            if cnt == lo:
                cnt, wrap = hi, True
            else:
                cnt = (cnt - 1) & 0xFFFF
        self.set("QDCTRL", qdc)
        self.set("CNT", cnt)
        self._down = False
        q = (cnt - self._c0) & 0xFFFF
        self._start_ns, self._p0, self._tick_ns = self.chip.now_ns(), q, 0.0
        if wrap:
            self.overflows += 1
            self._overflow()
            if self._load_due("end"):
                self._load()
        self._step_matches(q)
        self._resync()
        if wrap:
            self._period_start()
        self._flush_dma()

    # ------------------------------------------------------------ matches and outputs
    def _match_positions(self, c: _Ch) -> tuple[int, ...]:
        """_matches, plus the match of an output compare channel whose CnV lies below CNTIN while the counter still
        counts up there (CNTIN written above the counter: it goes on up to MOD, 38.4.10.1)."""
        ms = self._matches(c)
        if self._p0 < 0 and (c.kind == "oc" or c.kind == "sw") and not self._cpwm():
            m, c0 = self._mm, self._c0
            neg = m - ((((c0 + m) & 0xFFFF) - c.v) & 0xFFFF)
            if self._p0 - 1 <= neg < 0:
                return (neg,) + ms
        return ms

    def _matches(self, c: _Ch) -> tuple[int, ...]:
        """Positions in the period where channel c matches (CHnF; 38.4.5-38.4.8 incl. the CnV boundary rules)."""
        v = c.v
        m = self._mm
        d = (v - self._c0) & 0xFFFF
        k = c.kind
        if k == "cpwm" or k == "swc":
            if v == 0 or v & 0x8000 or (self.mod and v >= self.mod) or d > m:
                return ()                                # 0 % / 100 %: no CHnF
            return (d,) if d == 0 else (d, 2 * m - d)
        if d > m:
            return ()
        if d == 0 and (k == "epwm" or k == "swe"):       # CnV = CNTIN: 0 %, no CHnF
            return ()
        return (d,)

    def _mode_ref(self, c: _Ch, cnt: int, down: bool) -> bool | None:
        """Mode output (high-true sense) derived from the counter, or None for modes that only change at matches."""
        k = c.kind
        if k == "epwm":
            c0 = self._c0
            d = (c.v - c0) & 0xFFFF
            if d > self._mm:
                return True                              # CnV > MOD or CnV < CNTIN: 100 %
            return ((cnt - c0) & 0xFFFF) < d
        if k == "cpwm":
            v = c.v
            if v == 0 or v & 0x8000:
                return False
            if self.mod and v >= self.mod:
                return True
            c0 = self._c0
            d = (v - c0) & 0xFFFF
            if d > self._mm:
                return False
            p = (cnt - c0) & 0xFFFF
            return p <= d if down else p < d
        if k == "comb":
            c0 = self._c0
            v1 = self.ch[c.n + 1].v if c.n + 1 < len(self.ch) else 0
            p = (cnt - c0) & 0xFFFF
            return (c.v - c0) & 0xFFFF <= p < (v1 - c0) & 0xFFFF
        return None

    def _resync(self, kmin: int | None = None) -> None:
        """Re-evaluate the PWM/combine outputs from the counter now and (re)schedule the next match (position >= kmin;
        default: after the current tick) of every active channel; quiet channels get their CHnF when read."""
        running = self.running
        if running:
            pos = self._pos_now()
            if self._quiet:
                self._lazy_flags(pos)
            ip = math.floor(pos + EPS)
            cnt, down = self._cnt_at(ip)
            if kmin is None:
                kmin = ip + 1                           # a match at the current tick has already been handled
        else:
            cnt, down = self.get("CNT") & 0xFFFF, self._down
            kmin = 0
        sched = self.chip.sched if self.chip is not None else None
        for c in self.ch:
            if c.ev is not None and sched is not None:
                sched.cancel(c.ev)
            c.ev = None
            r = self._mode_ref(c, cnt, down)
            if r is not None:
                c.ref = r
        quiet = []
        for c in self.ch:
            self._apply(c)
            if not running or c.kind in _NOMATCH:
                continue
            if c.active:
                self._schedule_match(c, kmin)
            else:
                quiet.append(c)
                c.lazy_k = kmin - 1
        self._quiet = quiet
        if self._dma_q:
            self._flush_dma()

    def _schedule_match(self, c: _Ch, kmin: int) -> None:
        tol = self._tick_ns * 1e-3
        for k in self._match_positions(c):
            if k < kmin:
                continue
            t = self._t(k)
            if t <= c.last_t + tol:                      # this match already happened (re-anchored at that instant)
                continue
            if k == self._p0 and k == kmin and c.kind in ("epwm", "comb", "comb2"):
                if k == 0:
                    self._match(c, k, False)            # C(n)V = CNTIN: match at the start of the period
                continue
            c.ev = self.chip.sched.at_ns(t, self._match, c, k, True)
            return

    def _flag(self, c: _Ch) -> None:
        self.set(f"C{c.n}SC", self.get(f"C{c.n}SC") | CHF)
        c.armed = False                                  # set again: a pending read-then-write-0 no longer clears
        self._status_armed &= ~(1 << c.n)
        if c.dma:
            self._dma_q.append(c)                        # requested once the caller is done (_flush_dma)

    def _match(self, c: _Ch, k: int, resched: bool) -> None:
        c.ev = None
        c.last_t = self._t(k)
        self._flag(c)
        self._update_line()
        cnt, down = self._counter(k)
        if c.kind == "oc":
            c.ref = {1: not c.ref, 2: False, 3: True}.get(c.oc, c.ref)
        elif c.kind in ("epwm", "cpwm"):
            c.ref = bool(self._mode_ref(c, cnt, down))
        elif c.kind == "comb":
            c.ref = True                                     # set at C(2m)V
        elif c.kind == "comb2" and c.n > 0:
            self.ch[c.n - 1].ref = False                     # cleared at C(2m+1)V
            self._apply(self.ch[c.n - 1])
        self._apply(c)
        if resched and self.running:
            self._schedule_match(c, k + 1)
        if self._trig_mask >> c.n & 1:
            self._channel_trigger()
        if resched and self._dma_q:                          # (from _resync: flushed at its end)
            self._flush_dma()

    def _deadtime_ns(self) -> float:
        dt = self.get("DEADTIME")
        n = (dt & 0x3F) * DTPS_FACTOR[(dt >> 6) & 3]
        hz = bus_hz(self.chip)
        return n * 1e9 / hz if n and hz > 0 else 0.0

    def _cancel_dt(self, c: _Ch) -> None:
        if c.dt_ev is not None and self.chip is not None:
            self.chip.sched.cancel(c.dt_ev)
        c.dt_ev = None

    def _apply(self, c: _Ch) -> None:
        """Mode output -> (pair) -> dead time -> SWOCTRL -> POL -> OUTMASK -> pin."""
        if self.chip is None or self.chip.pins is None:
            return
        if c.kind == "comb2" and c.n > 0:
            lead = self.ch[c.n - 1]
            comp = (self.get("COMBINE") >> (8 * (c.n // 2))) & 0x02
            tgt = (not lead.target) if comp else lead.target
        else:
            tgt = c.ref ^ c.lowtrue
        c.target = tgt
        dten = (self.get("COMBINE") >> (8 * (c.n // 2))) & 0x10
        if dten and tgt and not c.applied:
            dt = self._deadtime_ns()
            if dt > 0:
                if c.dt_ev is None:
                    c.dt_ev = self.chip.sched.at_ns(self.chip.now_ns() + dt, self._dt_rise, c)
                self._apply_pair_follower(c)
                return
        self._cancel_dt(c)
        c.applied = tgt
        self._drive(c)
        self._apply_pair_follower(c)

    def _apply_pair_follower(self, c: _Ch) -> None:
        if c.kind == "comb" and c.n + 1 < len(self.ch) and self.ch[c.n + 1].kind == "comb2":
            self._apply(self.ch[c.n + 1])

    def _dt_rise(self, c: _Ch) -> None:
        c.dt_ev = None
        if c.target:
            c.applied = True
            self._drive(c)

    def _drive(self, c: _Ch) -> None:
        pins = self.chip.pins
        if not self._drives(c) and not (self.get("SWOCTRL") >> c.n) & 1:
            if c.out is not None:
                pins.release_signal(self._sig(c), "L")
                c.out = None
            return
        lvl = c.applied
        sw = self.get("SWOCTRL")
        if (sw >> c.n) & 1:
            lvl = bool((sw >> (8 + c.n)) & 1)
        pol = bool((self.get("POL") >> c.n) & 1)
        lvl = lvl ^ pol
        if (self.get("OUTMASK") >> c.n) & 1 or (self._fault and self._fmask >> c.n & 1):
            lvl = pol                                       # inactive state (output mask) / safe value (fault)
        v = "H" if lvl else "L"
        if c.out != v:
            c.out = v
            pins.drive_signal(self._sig(c), v)

    # ------------------------------------------------------------ flags and IRQ
    def _lazy_flags(self, upto: float | None) -> None:
        changed = False
        tol = self._tick_ns * 1e-3
        for c in self._quiet:
            for k in self._match_positions(c):
                if k > c.lazy_k and (upto is None or k <= upto + EPS):
                    c.lazy_k = k
                    t = self._t(k)
                    if t <= c.last_t + tol:
                        continue
                    c.last_t = t
                    self._flag(c)
                    changed = True
        if changed:
            self._update_line()

    def _update_line(self) -> None:
        lvl = bool(self.get("SC") & TOF and self.get("SC") & TOIE) or self._fault_irq
        if not lvl:
            for c in self.ch:
                sc = self.get(f"C{c.n}SC")
                if sc & CHF and sc & CHIE and not sc & DMA:   # DMA=1, CHIE=1: a DMA request instead (38.4.23)
                    lvl = True
                    break
        if lvl != self._line:
            self._line = lvl
            if self._irqn is not None and self.chip is not None:
                self.irq(self._irqn, lvl)

    # ------------------------------------------------------------ register hooks
    def _sc_read(self, blk, reg):
        if self.get("SC") & TOF:
            self._tof_armed = True
        return None

    def _sc_write(self, blk, reg, old, new):
        tof = old & TOF
        if not new & TOF and self._tof_armed:
            tof = 0                                         # read-1-then-write-0 clears TOF
        self._tof_armed = False
        new = self._wp(old, (new & ~TOF) | tof, WP_SC)
        if (old ^ new) & (0x1F | CPWMS):                    # CLKS / PS / CPWMS
            self._store(reg, old)                           # the counter so far, in the old mode and clock
            was_running = self.running
            st = self._stop()
            self._store(reg, new)
            if not was_running:
                self._load()                                # clock off: the buffers are the values
            self._geom()
            self._chan_cfg()
            self._ext_div = 0
            self._resume(st, fresh=self._fresh)
            if not (old >> 3) & 3 and (new >> 3) & 3 and st[0] == self.cntin and self._init_trig:
                self._fire_trigger()                        # clock started with CNT = CNTIN (38.4.21)
        else:
            self._store(reg, new)
            self._chan_cfg()
            self._resync()
        self._update_line()
        return new

    def _cnt_read(self, blk, reg):
        if not self.running:
            return None
        return self._cnt_at(math.floor(self._pos_now() + EPS))[0]

    def _cnt_write(self, blk, reg, old, new):
        self._stop()
        self._tofcnt = 0
        self.set("CNT", self.cntin)
        self._down = False
        self._fresh = True
        self._start(self._pos_from_cnt(self.cntin, False), fresh=True)
        if self._init_trig:
            self._fire_trigger()                            # the counter was loaded with CNTIN (38.4.21)
        return self.cntin

    def _buf_write(self, blk, reg, old, new):
        name = reg.name
        if name[0] == "C" and name[1] != "N":
            n = int(name[1])
            if n < len(self.ch) and self.ch[n].kind in _CAPTURE:
                return old                                  # capture modes ignore CnV writes (38.3.7)
        if not self.running:                                # counter clock off / counter stopped: at once
            st = self._stop()
            self._load()
            self._resume(st, fresh=self._fresh)
            return None
        ftmen = self.get("MODE") & FTMEN
        if name == "CNTIN":
            if not ftmen or not self.get("SYNCONF") & CNTINC:   # Table 38-303: at the next system clock
                st = self._stop()
                self.cntin = new & 0xFFFF
                self._geom()
                self._resume(st)
                return None
        elif name != "MOD":
            n = int(name[1])
            c = self.ch[n] if n < len(self.ch) else None
            if c is not None and c.kind in ("oc", "sw", "off", "ic") and \
                    (not ftmen or not (self.get("COMBINE") >> (8 * (n // 2))) & SYNCEN):
                if self._quiet:
                    self._lazy_flags(self._pos_now())
                c.v = new & 0xFFFF                          # output compare: at the next counter tick (38-305)
                self._resync()
                return None
        self._pending = True
        return None

    def _csc_read(self, n: int):
        def rd(blk, reg):
            if self._quiet and self.running:
                self._lazy_flags(self._pos_now())
            v = self.get(f"C{n}SC")
            if v & CHF and n < len(self.ch):
                self.ch[n].armed = True
            return v                                    # (storage may have changed: lazy CHnF)
        return rd

    def _csc_write(self, n: int):
        def wr(blk, reg, old, new):
            new = self._wp(old, new, WP_CSC)
            self._store(reg, old)
            if self._quiet and self.running:
                self._lazy_flags(self._pos_now())          # a match up to now may set CHnF again
            cur = self.get(f"C{n}SC")
            chf = cur & CHF
            c = self.ch[n] if n < len(self.ch) else None
            if c is not None:
                if not new & CHF and c.armed:
                    chf = 0
                c.armed = False
            new = (new & ~CHF) | chf
            self._store(reg, new)
            if (cur ^ new) & ~CHF & 0xFF:
                self._config_changed()
                if c is not None and c.dma and chf and (cur ^ new) & (DMA | CHIE):
                    self._dma_q.append(c)                   # DMA enabled with CHnF already set: request now
                    self._flush_dma()
            self._update_line()
            return new
        return wr

    def _status_read(self, blk, reg):
        if self._quiet and self.running:
            self._lazy_flags(self._pos_now())
        v = 0
        for c in self.ch:
            if self.get(f"C{c.n}SC") & CHF:
                v |= 1 << c.n
        self._status_armed = v
        self._store(reg, v)
        return v

    def _status_write(self, blk, reg, old, new):
        if self._quiet and self.running:
            self._lazy_flags(self._pos_now())
        clear = self._status_armed & ~new
        self._status_armed = 0
        for c in self.ch:
            if clear >> c.n & 1:
                self.set(f"C{c.n}SC", self.get(f"C{c.n}SC") & ~CHF)
        self._update_line()
        v = 0
        for c in self.ch:
            if self.get(f"C{c.n}SC") & CHF:
                v |= 1 << c.n
        return v

    def _fms_read(self, blk, reg):
        v = self.get("FMS")
        if v & WPEN:
            self._wpen_armed = True
        self._fms_armed = v & (FAULTF | 0x0F)
        v = (v & ~FAULTIN) | (FAULTIN if self._faultin() else 0)
        self.set("FMS", v)
        return v

    def _fms_write(self, blk, reg, old, new):
        wpen = (old | new) & WPEN                           # writing 1 sets WPEN, writing 0 has no effect
        self._wpen_armed = False
        if wpen:
            self.set("MODE", self.get("MODE") & ~WPDIS)
        # FAULTF / FAULTFj: cleared by reading them as 1 and then writing 0 while there is no fault condition at the
        # (enabled) input; clearing FAULTF clears every FAULTFj, clearing the last FAULTFj clears FAULTF
        flags = old & (FAULTF | 0x0F)
        armed, self._fms_armed = self._fms_armed, 0
        en = self.get("FLTCTRL") & 0xF if (self.get("MODE") >> FAULTM_SHIFT) & 3 else 0
        for j in range(4):
            b = 1 << j
            if armed & b and not new & b and not (en >> j & 1 and self._fault_value(j)):
                flags &= ~b
        if armed & FAULTF and not new & FAULTF and not self._faultin():
            flags = 0
        elif flags & FAULTF and not flags & 0x0F:
            flags = 0
        v = (old & ~(WPEN | FAULTF | 0x0F | FAULTIN)) | wpen | flags | (FAULTIN if self._faultin() else 0)
        self._store(reg, v)
        self._fault_flags_changed()
        return v

    def _mode_write(self, blk, reg, old, new):
        init = new & INIT
        new = self._wp(old, new, WP_MODE)
        wpen = self.get("FMS") & WPEN
        if wpen and new & WPDIS and self._wpen_armed:       # WPEN read as 1, then 1 written to WPDIS
            wpen = 0
            self.set("FMS", self.get("FMS") & ~WPEN)
        self._wpen_armed = False
        new = (new & ~(INIT | WPDIS)) | (0 if wpen else WPDIS)
        if (old ^ new) & FTMEN:                             # free running / stopped counter rules depend on FTMEN
            self._store(reg, old)
            st = self._stop()
            self._store(reg, new)
            self._geom()
            self._resume(st, fresh=self._fresh)
        if init:                                            # initialize the channel outputs from OUTINIT
            oi = self.get("OUTINIT")
            for c in self.ch:
                self._cancel_dt(c)
                c.ref = bool(oi >> c.n & 1) ^ c.lowtrue
                c.applied = c.ref ^ c.lowtrue
            for c in self.ch:
                self._apply(c)
        if (old ^ new) & (3 << FAULTM_SHIFT | FAULTIE):
            self._store(reg, new)
            if not (new >> FAULTM_SHIFT) & 3 and self._fault:
                self._fault = False                         # fault control disabled: outputs released
                for c in self.ch:
                    self._drive(c)
            self._config_changed()
            self._fault_flags_changed()
        return new

    def _sync_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new & SYNC_SWSYNC:
            self._sync = True
            if new & SYNC_REINIT and self.running:          # software trigger with REINIT: restart at CNTIN now
                self._stop()
                self._load()
                self.set("CNT", self.cntin)
                self._down = False
                self._fresh = True
                self._start(self._pos_from_cnt(self.cntin, False), fresh=True)
                if self._init_trig:
                    self._fire_trigger()
            elif not self.running:
                st = self._stop()
                self._load()
                self._resume(st, fresh=self._fresh)
        return None


@register("kinetis.ftm_alias")
class FTMAlias(MMIOBlock):
    """A second address window of an FTM (K22: FTM2 also at 0x4003A000): every access goes to `params.target`."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, ram_like=True, log=log, params=params)
        self._target: MMIOBlock | None = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._target = chip.peripherals.get(str(self.params.get("target", "")))

    def read(self, offset: int, size: int) -> int:
        t = self._target
        return t.read(offset, size) if t is not None else MMIOBlock.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        t = self._target
        if t is not None:
            t.write(offset, size, value)
        else:
            MMIOBlock.write(self, offset, size, value)
