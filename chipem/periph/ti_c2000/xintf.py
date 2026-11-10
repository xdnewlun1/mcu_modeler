"""C2833x/C2823x external interface XINTF (SPRUI07 ch. 14; register addresses SPRUI07 Table 1-99 / SPRS439 Table 8-28).

Two models:

* `ti_c2000.xintf` (0x0B20, 32 words): XTIMING0/6/7 (32-bit, +0x00/+0x0C/+0x0E), XINTCNF2 (32-bit, +0x14), XBANK (+0x18),
  XREVISION (+0x1A, read-only) and XRESET (+0x1D).  Every register is EALLOW protected (SPRUI07 14.1.1 "The XINTF
  registers are now EALLOW protected", the Table 1-99 footnote and each register description; Table 1-86 lists the
  whole 0x0B20 frame as "Not EALLOW protected", which contradicts the chapter: we follow the chapter).  Reset values:
  XTIMINGx 0x0043FFFF (X2TIMING = 1, XSIZE = 3 (16-bit), READYMODE = USEREADY = 1, lead/active/trail at their maximum:
  the defaults the field tables 14-12..14-17 and 14.2.4 describe; the "41D2A5h" printed in the 14.6.2-4 headings is
  every field = 1 and contradicts them), XINTCNF2 0x00010C04 (XTIMCLK = 1: SYSCLKOUT/2, CLKMODE = 1: XCLKOUT =
  XTIMCLK/2 (SPRS439 power-on reset timing, note A: "both the XTIMCLK and CLKMODE bits ... come up with a reset
  state of 1"), WRBUFF = 0, HOLD = 0; HOLDS / HOLDAS read the inactive (high)
  XHOLD / XHOLDA levels), XBANK 0x003F (BCYC = 7, BANK = 7: Table 14-18).  XRESET: TI's DSP2833x_Xintf.h puts it at
  0x0B3D inside this frame; SPRUI07 Tables 1-99 / 14-7 print 0x083D, so the chip YAML also maps a one-word alias
  there.  Writing XHARDRESET = 1 returns XTIMINGx/XBANK/XINTCNF2 to reset and drops the write buffer; it reads 0.
  XINTCNF2 bit map (DSP2833x_Xintf.h; SPRUI07 has no XINTCNF2 figure): WRBUFF 1:0, CLKMODE 2, CLKOFF 3, WLEVEL 7:6
  (read-only: writes still in the buffer), HOLD 9, HOLDS 10, HOLDAS 11, XTIMCLK 18:16 (0 = SYSCLKOUT, 1 = /2, other
  values reserved: one `warn`, /2 used).
* `ti_c2000.xintf_zone` (zone 0 at 0x4000 + 4K, zone 6 at 0x100000 + 1M, zone 7 at 0x200000 + 1M words, `params.zone`):
  the address window of one chip select.  An external memory is attached by the board (`devices: [{type: sram, chip:
  A, bus: XINTF6, size: 256K, width: 16}]`, chipem/devices/sram.py, or `zone.attach_memory(dev)`).

Data path (SPRUI07 14.1.3, 14.2.7).  A zone access needs PCLKCR3.XINTFENCLK = 1 ("When the XINTF clock is enabled
in PCLKCR3, all zones are enabled"; what silicon does on an access with XTIMCLK stopped is not documented): with the
clock off the access is not performed (writes are lost, reads return 0) and one `warn` is logged.  The external
address is the offset in the zone (zone 0 drives XA11:0, upper lines undefined -> 0).  XSIZE = 3 (16-bit bus): the
device sees XA19:0 and XD15:0; a 32-bit CPU access is two bus cycles.  XSIZE = 1 (32-bit bus): the device sees
XA19:1, XWE0 writes XD15:0 (even word), XA0/XWE1 writes XD31:16 (odd word), a 32-bit access is one bus cycle; a
16-bit device on a 32-bit zone only gets the even words (odd ones float / are lost), a 32-bit device on a 16-bit zone
only its low half (one `warn`).  XSIZE = 0 or 2 are "reserved: incorrect behaviour": treated as 16-bit with a `warn`.
The board wiring follows the GPIO mux: the zone's chip select XZCSn (GPIO36 / GPIO28 / GPIO37), XWE0 (GPIO38), XA0/XWE1
(GPIO40), the address lines (XA1-7 GPIO41-47, XA8-15 GPIO80-87, XA16 GPIO39, XA17-19 GPIO31/30/29) and data lines
(XD15:0 GPIO79:64, XD31:16 GPIO63:48) must be muxed to their XINTF function (XRD is a dedicated pin).  Without XZCSn
the device is never selected; an address line not muxed reaches the device as 0 (aliasing); a data line not muxed
reads its pin's pull (pull-up enabled -> 1, else 0) and is not written; without XWE0 / XWE1 the write strobe never
reaches the device.  With no device on the zone (or not selected) a read returns the undriven bus: each data line
reads 1 if its pin's pull-up is enabled (GPxPUD = 0, the reset state of GPIO12-87), else 0 (one `warn` per zone).
Each kind of problem is reported once per zone.  The wiring is re-evaluated when a pin's owner or pull changes.

Timing (Table 14-3, 14-5).  One bus cycle lasts r x (m x LEAD + (m x ACTIVE + 1) + m x TRAIL) SYSCLKOUT cycles,
r = 2 if XTIMCLK = SYSCLKOUT/2 else 1, m = 2 if X2TIMING else 1, read or write fields.  XBANK adds r x BCYC cycles
before an access that enters or leaves zone BANK from another zone.  The XINTF serves one bus cycle at a time: a CPU
read (and a write with WRBUFF = 0) stalls the CPU until its bus cycle(s) complete, i.e. the instruction costs the
bus time instead of 1 cycle (`core._extra`, like RPT); with WRBUFF = n (1-3) up to n writes are buffered without a
stall and drain in order at the bus rate (WLEVEL), a read waits for buffered writes first.  Only accesses made while
the CPU runs are timed (DMA bursts, debugger and loader accesses are not; DMA XINTF stalls are not modelled, see
dma.py).  Not modelled: instruction fetch from XINTF (the decode cache reads code once; fetch wait states are not
charged), XREADY (USEREADY = 1 behaves as XREADY always high, WS = 0; one `warn` per zone on the first access),
XHOLD/XHOLDA (HOLD mode is reported once if GPIO14 is muxed to TZ3/XHOLD while XINTCNF2.HOLD = 0 and the pin is low;
HOLDAS always reads 1), the 28x write-followed-by-read pipeline order, the XA/XD/XZCS/XRD/XWE pin waveforms (the bus
is modelled at transaction level) and the XCLKOUT pin (`xclkout_hz()` gives its frequency; CLKOFF is stored).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model

ZONES = (0, 6, 7)
XTIMING_RESET = 0x0043FFFF
XINTCNF2_RESET = 0x00010C04
XBANK_RESET = 0x003F
_XT_RW = 0x0043FFFF                       # X2TIMING, XSIZE, READYMODE, USEREADY, lead/active/trail fields
_CNF_RW = 0x0007020F                      # XTIMCLK, HOLD, CLKOFF, CLKMODE, WRBUFF
HOLDS, HOLDAS = 1 << 10, 1 << 11
_CS = {0: "XZCS0", 6: "XZCS6", 7: "XZCS7"}


def _zone_timing(xt: int, r: int) -> tuple[int, int, bool, bool, int]:
    """(read cycles, write cycles, 32-bit bus, USEREADY, XSIZE) of one bus cycle for XTIMINGx value `xt`."""
    m = 2 if xt >> 22 & 1 else 1
    rd = r * (m * (xt >> 12 & 3) + m * (xt >> 9 & 7) + 1 + m * (xt >> 7 & 3))
    wr = r * (m * (xt >> 5 & 3) + m * (xt >> 2 & 7) + 1 + m * (xt & 3))
    xsize = xt >> 16 & 3
    return rd, wr, xsize == 1, bool(xt >> 14 & 1), xsize


@register("ti_c2000.xintf")
class Xintf(C2000Block):
    eallow_regs = "*"
    gate = None                                        # zone accesses check XINTFENCLK; the registers are always on

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ro = 0xFFFFFFFF & ~_XT_RW
        ov = {f"XTIMING{z}": {"ro_mask": ro, "on_write": self._cfg_write} for z in ZONES}
        ov["XINTCNF2"] = {"ro_mask": 0xFFFFFFFF & ~_CNF_RW, "on_write": self._cfg_write, "on_read": self._cnf_read}
        ov["XBANK"] = {"ro_mask": 0xFFC0, "on_write": self._cfg_write}
        ov["XREVISION"] = {"ro_mask": 0xFFFF, "reset": int(params.get("revision", 0)) & 0xFFFF}
        ov["XRESET"] = {"ro_mask": 0xFFFE, "on_write": self._xreset_write, "on_read": lambda b, r: 0}
        regs = build_regs((name, "XINTF"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.zones: dict[int, "XintfZone"] = {}
        self._timing: dict[int, tuple] = {}
        self._warned: set[str] = set()
        self._busy = 0                                 # cycle at which the bus finishes its last queued bus cycle
        self._wbuf: deque[int] = deque()               # completion cycles of buffered writes
        self._last_zone: int | None = None
        self.bus_cycles = 0                            # bus cycles performed (CPU and DMA)
        self.stall_cycles = 0                          # CPU cycles charged beyond 1 per instruction

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._recompute()

    def reset(self) -> None:
        super().reset()
        self._soft()
        self._warned.clear()

    def _soft(self) -> None:
        self._busy = 0
        self._wbuf.clear()
        self._last_zone = None
        self._recompute()

    # ------------------------------------------------------------------ registers
    def _cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._recompute()
        if reg.name == "XINTCNF2" and (new >> 16 & 7) > 1:
            self._warn("xtimclk", f"{self.name}: XINTCNF2.XTIMCLK = {new >> 16 & 7} is reserved; XTIMCLK = "
                       "SYSCLKOUT/2 used")
        if reg.name.startswith("XTIMING") and (new >> 16 & 3) in (0, 2):
            self._warn(f"xsize{reg.name}", f"{self.name}: {reg.name}.XSIZE = {new >> 16 & 3} is reserved "
                       "(incorrect XINTF behaviour); the zone is treated as 16-bit")
        return None

    def _cnf_read(self, blk, reg):
        v = self.get("XINTCNF2") & ~(0xC0 | HOLDS | HOLDAS)
        self._drain()
        v |= min(len(self._wbuf), 3) << 6
        v |= HOLDAS
        if not self._xhold_low():
            v |= HOLDS
        return v

    def _xreset_write(self, blk, reg, old, new):
        if new & 1:
            for z in ZONES:
                self.set(f"XTIMING{z}", XTIMING_RESET)
            self.set("XINTCNF2", XINTCNF2_RESET)
            self.set("XBANK", XBANK_RESET)
            self._soft()
            self.emit("info", block=self.name, msg=f"{self.name}: XHARDRESET (timing registers reset, write buffer "
                      "dropped)")
        return 0

    def _warn(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _recompute(self) -> None:
        cnf = self.get("XINTCNF2")
        r = 1 if (cnf >> 16 & 7) == 0 else 2
        self._r = r
        for z in ZONES:
            self._timing[z] = _zone_timing(self.get(f"XTIMING{z}"), r)
        xb = self.get("XBANK")
        self._bank, self._bcyc = xb & 7, r * (xb >> 3 & 7)
        self._wrbuff = cnf & 3

    # ------------------------------------------------------------------ queries
    def xtimclk_hz(self) -> float:
        hz = float(self.chip.clock.hz) if self.chip is not None else 0.0
        return hz / self._r

    def xclkout_hz(self) -> float:
        """XCLKOUT frequency (0 when CLKOFF = 1)."""
        cnf = self.get("XINTCNF2")
        if cnf & 8:
            return 0.0
        return self.xtimclk_hz() / (2 if cnf & 4 else 1)

    def access_cycles(self, zone: int, write: bool = False) -> int:
        """SYSCLKOUT cycles of one bus cycle on `zone`."""
        t = self._timing[zone]
        return t[1] if write else t[0]

    def clock_on(self) -> bool:
        sc = self._sysctrl
        return sc is None or sc.clock_enabled("XINTFENCLK")

    def _xhold_low(self) -> bool:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return False
        for p in board.signal_pins("XHOLD"):
            if board.signal_matches(p.owner, "XHOLD") and p.level() == "L":
                return True
        return False

    # ------------------------------------------------------------------ timing
    def _drain(self) -> None:
        if not self._wbuf or self.chip is None:
            return
        now = self.chip.now_cycles()
        q = self._wbuf
        while q and q[0] <= now:
            q.popleft()

    def _charge(self, zone: int, write: bool, n: int) -> None:
        """Account `n` bus cycles of a CPU access to `zone`: stall the running instruction as needed."""
        chip = self.chip
        core = chip.core
        if not getattr(core, "running", False):
            return
        t = self._timing[zone]
        cost = n * (t[1] if write else t[0])
        lz = self._last_zone
        if lz is not None and lz != zone and self._bank in (lz, zone):
            cost += self._bcyc
        self._last_zone = zone
        now = chip.now_cycles()
        start = max(now, self._busy)
        stall = 0
        if write and self._wrbuff:
            q = self._wbuf
            while q and q[0] <= now:
                q.popleft()
            if len(q) >= self._wrbuff:                 # buffer full: wait for the oldest write to finish
                stall = q[0] - now
                q.popleft()
            end = start + cost
            q.append(end)
        else:
            end = start + cost
            stall = max(end - now - 1, 0)              # the instruction's own cycle is already counted
            if write:
                self._wbuf.clear()
        self._busy = end
        self.bus_cycles += n
        if stall > 0:
            core._extra += stall
            self.stall_cycles += stall

    # ------------------------------------------------------------------ zone access (called by XintfZone)
    def zone_access(self, zb: "XintfZone", off: int, size: int, value: int | None) -> int:
        z = zb.zone
        if not self.clock_on():
            self._warn(f"clk{z}", f"{zb.name}: zone {z} accessed with the XINTF clock off (PCLKCR3.XINTFENCLK = 0): "
                       "the access is not performed")
            return 0
        t = self._timing[z]
        if t[3]:
            self._warn(f"rdy{z}", f"{zb.name}: XTIMING{z}.USEREADY = 1 but XREADY is not modelled: treated as always "
                       "ready (no extra wait states)")
        if self.get("XINTCNF2") >> 9 & 1 == 0 and self._xhold_low():
            self._warn("hold", f"{self.name}: XHOLD is low with XINTCNF2.HOLD = 0, but bus hold (XHOLD/XHOLDA) is "
                       "not modelled: the access proceeds")
        x32 = t[2]
        write = value is not None
        self._charge(z, write, 1 if (size == 1 or x32) else 2)
        w = zb.wiring()
        out = 0
        for i in range(size):
            o = off + i
            if write:
                zb.bus_write(o, (value >> (16 * i)) & 0xFFFF, x32, w)
            else:
                out |= zb.bus_read(o, x32, w) << (16 * i)
        return out


@register("ti_c2000.xintf_zone")
class XintfZone(C2000Block):
    """One XINTF zone window (`params.zone`: 0, 6 or 7); see the module doc."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        super().__init__(name, base, 0, (), params=params, log=log)
        self.size = size                               # no backing store: the data lives in the external device
        self.zone = int(params.get("zone", 0))
        self.log_unknown = False
        self.device: Any = None
        self.ctl: Xintf | None = None
        self._w: tuple | None = None                   # cached wiring (see wiring())
        self._unsub = None
        self._checked: tuple | None = None
        self._warned: set[str] = set()

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self.ctl = find_model(chip, "ti_c2000.xintf")
        if self.ctl is not None:
            self.ctl.zones[self.zone] = self
        board = getattr(chip, "pins", None)
        if board is not None and self._unsub is None:
            self._unsub = board.subscribe(self._pin_event)

    def reset(self) -> None:
        self._warned.clear()
        self._w = self._checked = None

    def attach_memory(self, dev: Any) -> None:
        """Connect an external memory (`read(loc)`, `write(loc, value, mask)`, `size`, `width`) to this zone."""
        self.device = dev
        self._w = self._checked = None
        self._warned.clear()

    def _pin_event(self, ev: Any) -> None:
        if ev.field in ("owner", "pull"):
            self._w = None

    def _warn(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ wiring
    def wiring(self) -> tuple:
        """(selected, write-enable mask (bit 0 XWE0, bit 1 XWE1), XA line mask, XD line mask, floating XD value)."""
        w = self._w
        if w is None:
            w = self._w = self._compute_wiring()
        return w

    def _compute_wiring(self) -> tuple:
        board = getattr(self.chip, "pins", None)
        if board is None:                               # no pin model: everything connected
            return True, 3, 0xFFFFF, 0xFFFFFFFF, 0

        def pins(sig):
            return [p for p in board.signal_pins(sig) if board.signal_matches(p.owner, sig)]

        cs = bool(pins(_CS[self.zone]))
        we = (1 if pins("XWE0") else 0) | (2 if pins("XWE1") else 0)
        amask = 0
        for n in range(20):
            if pins(f"XA{n}"):
                amask |= 1 << n
        dmask = flt = 0
        for n in range(32):
            if pins(f"XD{n}"):
                dmask |= 1 << n
            cand = board.signal_pins(f"XD{n}")       # an undriven data line reads its pin's pull (no pin: 0)
            if cand and cand[0].pull == "up":
                flt |= 1 << n
        return cs, we, amask, dmask, flt

    def _check(self, w: tuple, x32: bool) -> bool:
        """Report wiring problems once; True if the device is selected."""
        dev = self.device
        if dev is None:
            self._warn("nodev", f"{self.name}: no external device on XINTF zone {self.zone}: reads return the undriven "
                       "data bus (pull-ups), writes are lost")
            return False
        cs, we, amask, dmask, _ = w
        if not cs:
            self._warn("cs", f"{self.name}: {_CS[self.zone]} is not muxed to its pin: the device on zone {self.zone} is "
                       "never selected (reads float, writes are lost)")
            return False
        if self._checked != (w, x32, dev):                # once per wiring / bus width / device change
            self._checked = (w, x32, dev)
            nb = max((dev.size - 1).bit_length(), 0)
            need = ((1 << nb) - 1) << (1 if x32 else 0)
            if self.zone == 0:
                need &= 0xFFF
            miss_a = [f"XA{n}" for n in range(20) if need >> n & 1 and not amask >> n & 1]
            dw = 32 if x32 and dev.width >= 32 else 16
            miss_d = [f"XD{n}" for n in range(dw) if not dmask >> n & 1]
            key = "lines:" + ",".join(miss_a + miss_d)
            if (miss_a or miss_d) and key not in self._warned:
                self._warned.add(key)
                self.emit("warn", block=self.name, msg=f"{self.name}: XINTF lines not muxed to their pins: "
                          f"{', '.join(miss_a + miss_d)} (address bits read as 0 by the device, data bits float)")
        return True

    def _width_check(self, x32: bool) -> None:
        dev = self.device
        if not x32 and dev.width > 16:
            self._warn("w32on16", f"{self.name}: {dev.width}-bit device on a 16-bit zone (XSIZE = 3): only its low "
                       "16 bits are connected")

    def bus_read(self, o: int, x32: bool, w: tuple) -> int:
        cs, we, amask, dmask, flt = w
        if not self._check(w, x32):
            return flt & 0xFFFF
        dev = self.device
        self._width_check(x32)
        a = o & amask
        if x32:
            loc, half = a >> 1, o & 1
            if half and dev.width <= 16:
                return (flt >> 16) & 0xFFFF
            v = dev.read(loc)
            sh = 16 * half
            dm = (dmask >> sh) & 0xFFFF
            return ((v >> sh) & dm | (flt >> sh) & ~dm) & 0xFFFF
        if dev.width > 16:
            a >>= 1
        v = dev.read(a) & 0xFFFF
        dm = dmask & 0xFFFF & dev.mask                 # an 8-bit device leaves XD15:8 undriven
        return (v & dm) | (flt & ~dm & 0xFFFF)

    def bus_write(self, o: int, v: int, x32: bool, w: tuple) -> None:
        cs, we, amask, dmask, flt = w
        if not self._check(w, x32):
            return
        dev = self.device
        self._width_check(x32)
        a = o & amask
        if x32:
            loc, half = a >> 1, o & 1
            strobe = 2 if half else 1                  # odd word: XWE1, even: XWE0 (Table 14-2)
            if not we & strobe:
                self._warn(f"we{strobe}", f"{self.name}: {'XWE1' if half else 'XWE0'} is not muxed to its pin: "
                           "writes on that half of the bus are lost")
                return
            if half and dev.width <= 16:
                return
            sh = 16 * half
            dm = (dmask >> sh) & 0xFFFF
            data = (v & dm) | (flt >> sh) & ~dm & 0xFFFF
            dev.write(loc, data << sh, 0xFFFF << sh)
            return
        if not we & 1:
            self._warn("we1", f"{self.name}: XWE0 is not muxed to its pin: writes to zone {self.zone} are lost")
            return
        if dev.width > 16:
            a >>= 1
        dm = dmask & 0xFFFF
        dev.write(a, (v & dm) | (flt & ~dm & 0xFFFF), 0xFFFF)

    # ------------------------------------------------------------------ bus interface
    def read(self, offset: int, size: int) -> int:
        ctl = self.ctl
        if ctl is None:
            return 0
        return ctl.zone_access(self, offset, size, None)

    def write(self, offset: int, size: int, value: int) -> None:
        ctl = self.ctl
        if ctl is not None:
            ctl.zone_access(self, offset, size, value)
