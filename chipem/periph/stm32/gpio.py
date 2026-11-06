"""STM32F4 GPIO port (RM0090 ch. 8): MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, BSRR, LCKR, AFRL/AFRH.

This model is the only STM32 code that turns pin registers into PinBoard state (`mcu_update`) and pin levels into
IDR values (there is no separate mux block on STM32).

Per pin (MODER field):
* 00 input:  owner "GPIO", direction "in", pull from PUPDR (01 up, 10 down).
* 01 output: owner "GPIO". Push-pull: direction "out", drive = ODR bit. Open-drain (OTYPER=1): ODR=0 drives L,
  ODR=1 releases the pin (direction "in"; the level comes from the pull / external stimulus / net).
* 10 alternate function: owner = the AFn function of the pin table (`functions["AF7"]` = "USART1_TX"), or
  "disabled" with one `warn` when the pin has no such function; direction and idle drive from the STM32 signal
  table (`signals.py`: USART TX -> output idling H, RX -> input, I2C -> open-drain, unknown -> "none"); pull from
  PUPDR. Traffic does not toggle the pin.
* 11 analog: owner = the pin's "ADC" (or "analog") function, else "analog"; direction "analog"; no pull.
Reset values come from the SVD (GPIOA: PA13/PA14/PA15 AF0 = JTMS-SWDIO/JTCK-SWCLK/JTDI with pulls; GPIOB: PB3/PB4 AF0
= JTDO/TRACESWO and NJTRST), so the debug pins start owned by SWD/JTAG.

IDR = resolved levels of all pins of the port (H -> 1, L/X -> 0) except analog-mode pins (read 0). Reading a
floating (X) input pin whose configuration the firmware changed (MODER/PUPDR/OTYPER/AFR field written with a new
value) logs one `warn` per pin until reset; untouched pins (every pin is a floating input after reset) read 0
silently. The value is cached and invalidated by PinBoard input
listeners and configuration changes, so polling loops cost one attribute read.
ODR writes and BSRR (set bits 0-15, reset bits 16-31; set wins) update the outputs; BSRR reads 0.
LCKR: the LCKK write sequence (1, 0, 1 with the same LCKy mask, then a read) freezes MODER/OTYPER/OSPEEDR/PUPDR/
AFR bits of the locked pins until reset (writes to locked fields are ignored with one `warn`).

Pin discovery (chipem.pinreport): configuration changes are reported with the PC of the writing instruction
("mode output", "mux AF7 (USART1_TX)", "pull up", "open-drain"); IDR reads are counted and attributed lazily to
the pins that were inputs at the time; BSRR/ODR writes are counted per written value. `pin_stats()` hands the
counters to the report.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs
from .signals import TABLE as SIGNALS

MODE_NAMES = ("input", "output", "alternate", "analog")
CFG_REGS = ("MODER", "OTYPER", "OSPEEDR", "PUPDR", "AFRL", "AFRH")
LCKK = 1 << 16


def _zero(blk, reg):
    return 0


@register("stm32.gpio")
class GPIO(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.port = str(params.get("port") or name[-1]).upper()
        self.gate = f"GPIO{self.port}"
        ov: dict[str, dict] = {n: {"on_write": self._cfg_write} for n in CFG_REGS}
        ov["IDR"] = {"on_read": self._idr_read, "ro_mask": 0xFFFFFFFF}
        ov["ODR"] = {"on_write": self._odr_write, "ro_mask": 0xFFFF0000}
        ov["BSRR"] = {"on_write": self._bsrr_write, "on_read": _zero}
        ov["LCKR"] = {"on_write": self._lckr_write, "on_read": self._lckr_read}
        r = build_regs((name, f"GPIO{self.port}"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.pins: list[Any] = [None] * 16
        self.odr = 0
        self._idr: int | None = None
        self._warned_x: set[int] = set()
        self._warned_af: set[tuple[int, int]] = set()
        self._lock_seq = 0
        self._lock_val = 0
        self._locked = 0
        self._warned_lock = False
        self._out_mask = 0
        self.touched = 0                     # pins whose configuration firmware changed (since reset)
        self._odr_off = self.regs["ODR"].offset
        self._idr_off = self.regs["IDR"].offset
        self._bsrr_off = self.regs["BSRR"].offset
        self._state: list[tuple | None] = [None] * 16      # last (owner, direction, drive, pull) pushed per pin
        # pin discovery counters (kept across resets so a report covers the whole run)
        self._rep: Any = None
        self.n_idr = 0
        self._rd_acc = [0] * 16
        self._rd_mark = 0
        self._rd_mask = 0
        self._w_bsrr: dict[int, int] = {}
        self._w_fold = [0] * 16
        self.n_odr_w = 0
        self.changes = [0] * 16
        self.ever_out = 0
        self.x_read_mask = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        board = getattr(chip, "pins", None)
        if board is not None:
            for i in range(16):
                pin = board.gpio(self.port, i)
                self.pins[i] = pin
                if pin is not None:
                    board.input_listeners[pin.id].append(self._invalidate)
            from ...pinreport import ensure
            self._rep = ensure(chip)
            if self._rep is not None:
                self._rep.hooked = True

    def reset(self) -> None:
        super().reset()
        self.odr = 0
        self._idr = None
        self._warned_x.clear()
        self._warned_af.clear()
        self._lock_seq = 0
        self._locked = 0
        self._warned_lock = False
        self._out_mask = self._calc_out_mask()
        self.touched = 0
        self._state = [None] * 16
        if self.chip is not None:
            self._flush_reads()
            for i in range(16):
                self._sync_pin(i, quiet=True)

    # ------------------------------------------------------------ field helpers
    def mode(self, i: int) -> int:
        return (self.get("MODER") >> (2 * i)) & 3

    def otype(self, i: int) -> int:
        return (self.get("OTYPER") >> i) & 1

    def pupd(self, i: int) -> int:
        return (self.get("PUPDR") >> (2 * i)) & 3

    def af(self, i: int) -> int:
        reg = "AFRL" if i < 8 else "AFRH"
        return (self.get(reg) >> (4 * (i & 7))) & 0xF

    def _calc_out_mask(self) -> int:
        m = self.get("MODER")
        return sum(1 << i for i in range(16) if (m >> (2 * i)) & 3 == 1)

    @property
    def output_mask(self) -> int:
        return self._out_mask

    @property
    def input_mask(self) -> int:
        m = self.get("MODER")
        return sum(1 << i for i in range(16) if (m >> (2 * i)) & 3 == 0)

    def _owner_af(self, pin: Any, i: int, af: int) -> str:
        f = pin.functions.get(f"AF{af}")
        if f is None:
            if (i, af) not in self._warned_af:
                self._warned_af.add((i, af))
                self.emit("warn", block=self.name, pin=pin.id, msg=f"{pin.id}: AF{af} selects no function on this pin")
            return "disabled"
        return f

    def pin_config(self, i: int) -> tuple[str, str, str | None, str | None]:
        """(owner, direction, drive, pull) of pin i from the current registers."""
        pin = self.pins[i]
        mode = self.mode(i)
        pud = self.pupd(i)
        pull = {1: "up", 2: "down"}.get(pud)
        if mode == 3:
            fn = (pin.functions.get("ADC") or pin.functions.get("analog")) if pin is not None else None
            return (fn or "analog"), "analog", None, None
        if mode == 2:
            owner = self._owner_af(pin, i, self.af(i)) if pin is not None else "?"
            d, drv = SIGNALS.pin_state(owner if owner not in ("disabled", "?") else None)
            return owner, d, drv, pull
        if mode == 1:
            high = bool(self.odr >> i & 1)
            if self.otype(i) and high:
                return "GPIO", "in", None, pull         # open-drain released
            return "GPIO", "out", ("H" if high else "L"), pull
        return "GPIO", "in", None, pull

    def _sync_pin(self, i: int, *, quiet: bool = False, why: str = "") -> None:
        pin = self.pins[i]
        board = self.chip.pins if self.chip is not None else None
        if pin is None or board is None:
            return
        st = self.pin_config(i)
        if st == self._state[i]:
            return
        prev = self._state[i]
        self._state[i] = st
        owner, direction, drive, pull = st
        board.mcu_update(pin, owner=owner, direction=direction, drive=drive, pull=pull)
        self._idr = None
        if not quiet and (prev is None or prev[0] != owner or prev[1] != direction or prev[3] != pull):
            if owner == "GPIO":
                what = "output" if direction == "out" else ("open-drain output (released)" if self.mode(i) == 1
                                                           else "input")
                msg = f"{pin.id} → GPIO {what}" + (f" ({drive})" if drive else "")
            else:
                msg = f"{pin.id} → {owner}"
            if pull:
                msg += f" (pull-{pull})"
            self.emit("info", block=self.name, pin=pin.id, msg=msg)

    # ------------------------------------------------------------ fast paths
    def read(self, offset: int, size: int) -> int:
        if offset == 0x10 and size == 4 and not self.log_all:
            self.n_idr += 1
            v = self._idr
            return v if v is not None else self._idr_compute()
        return MMIOBlock.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if offset == 0x18 and size == 4 and not self.log_all:
            d = self._w_bsrr
            c = d.get(value)
            d[value] = 1 if c is None else c + 1
            if c is None:
                self._new_write_value(value)
            self._set_odr((self.odr & ~(value >> 16)) | (value & 0xFFFF))
            return
        if self._gate_loc is not None and offset != 0x14:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------ pin discovery (chipem.pinreport)
    def _new_write_value(self, value: int) -> None:
        d = self._w_bsrr
        if len(d) > 256:
            fold = self._w_fold
            for v, c in d.items():
                m = (v | v >> 16) & 0xFFFF
                while m:
                    low = m & -m
                    fold[low.bit_length() - 1] += c
                    m ^= low
            d.clear()
        rep = self._rep
        if rep is None:
            return
        pc = rep.pc()
        m = (value | value >> 16) & 0xFFFF
        while m:
            low = m & -m
            pin = self.pins[low.bit_length() - 1]
            if pin is not None:
                rep.touch_data(pin, pc)
            m ^= low

    def _flush_reads(self) -> None:
        d = self.n_idr - self._rd_mark
        if d:
            m = self._rd_mask
            acc = self._rd_acc
            while m:
                low = m & -m
                acc[low.bit_length() - 1] += d
                m ^= low
        self._rd_mark = self.n_idr
        self._rd_mask = self.input_mask & self.touched & sum(1 << i for i, x in enumerate(self.pins) if x is not None)

    def pin_stats(self) -> dict[str, dict]:
        self._flush_reads()
        out: dict[str, dict] = {}
        for i, pin in enumerate(self.pins):
            if pin is None:
                continue
            bit = 1 << i
            w = self._w_fold[i] + sum(c for v, c in self._w_bsrr.items() if (v | v >> 16) & bit)
            if self.ever_out & bit:
                w += self.n_odr_w
            reads = self._rd_acc[i]
            fl = bool(self.x_read_mask & bit)
            if w or reads or self.changes[i] or fl:
                out[pin.id] = {"reads": reads, "writes": w, "changes": self.changes[i], "floating": fl}
        return out

    def _invalidate(self, pin: Any = None) -> None:
        self._idr = None

    # ------------------------------------------------------------ output
    def _set_odr(self, new: int) -> None:
        new &= 0xFFFF
        old = self.odr
        self.odr = new
        o = self._odr_off
        self.mem[o:o + 4] = new.to_bytes(4, "little")
        ch = (old ^ new) & self.output_mask
        if ch:
            rep = self._rep
            pc = rep.pc() if rep is not None else None
            while ch:
                low = ch & -ch
                i = low.bit_length() - 1
                ch ^= low
                pin = self.pins[i]
                if pin is not None:
                    self.changes[i] += 1
                    if rep is not None:
                        rep.touch_data(pin, pc)
                    self._sync_pin(i, quiet=True)

    def _odr_write(self, blk, reg, old, new):
        self.n_odr_w += 1
        if self.n_odr_w == 1 and self._rep is not None:
            self._new_write_value(self.output_mask)
        self._set_odr(new)
        return None

    def _bsrr_write(self, blk, reg, old, new):
        self._w_bsrr[new] = self._w_bsrr.get(new, 0) + 1
        self._set_odr((self.odr & ~(new >> 16)) | (new & 0xFFFF))
        return 0

    # ------------------------------------------------------------ configuration
    def _field_bits(self, regname: str) -> int:
        return {"MODER": 2, "OSPEEDR": 2, "PUPDR": 2, "OTYPER": 1, "AFRL": 4, "AFRH": 4}[regname]

    def _lock_mask(self, regname: str) -> int:
        """Register bits belonging to locked pins."""
        if not self._locked:
            return 0
        w = self._field_bits(regname)
        first = 8 if regname == "AFRH" else 0
        n = 8 if regname in ("AFRL", "AFRH") else 16
        m = 0
        for k in range(n):
            if self._locked >> (first + k) & 1:
                m |= ((1 << w) - 1) << (w * k)
        return m

    def _describe(self, i: int, before: tuple, after: tuple) -> list[str]:
        mode0, ot0, pud0, af0 = before
        mode1, ot1, pud1, af1 = after
        pin = self.pins[i]
        parts = []
        if mode1 != mode0 or (mode1 == 2 and af1 != af0):
            if mode1 == 2:
                parts.append(f"mux AF{af1} ({pin.functions.get(f'AF{af1}', '?')})")
            elif mode1 == 3:
                parts.append(f"mode analog ({pin.functions.get('ADC') or pin.functions.get('analog') or 'analog'})")
            else:
                parts.append(f"mode {MODE_NAMES[mode1]}")
        if pud1 != pud0:
            parts.append({0: "pull off", 1: "pull up", 2: "pull down"}.get(pud1, "pull reserved"))
        if ot1 != ot0:
            parts.append("open-drain" if ot1 else "push-pull")
        return parts

    def _cfg_write(self, blk, reg, old, new):
        lm = self._lock_mask(reg.name)
        if lm and (old ^ new) & lm:
            new = (new & ~lm) | (old & lm)
            if not self._warned_lock:
                self._warned_lock = True
                self.emit("warn", block=self.name, msg=f"{self.name}_{reg.name}: write to a locked pin (LCKR) ignored")
            self._store(reg, new)
        if old == new:
            return None
        self._store(reg, new)
        w = self._field_bits(reg.name)
        first = 8 if reg.name == "AFRH" else 0
        n = 8 if reg.name in ("AFRL", "AFRH") else 16
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        changed = []
        for k in range(n):
            mask = ((1 << w) - 1) << (w * k)
            if (old ^ new) & mask:
                changed.append(first + k)
                self.touched |= 1 << (first + k)
        self._flush_reads()                 # attribute IDR reads so far to the previous input set
        if reg.name == "MODER":
            self._out_mask = self._calc_out_mask()
            for i in changed:
                if (new >> (2 * i)) & 3 == 1:
                    self.ever_out |= 1 << i
        for i in changed:
            pin = self.pins[i]
            if pin is None:
                continue
            if rep is not None and reg.name != "OSPEEDR":
                before = self._snapshot(i, reg, old)
                after = self._snapshot(i, reg, new)
                parts = self._describe(i, before, after)
                if parts:
                    rep.config(pin, ", ".join(parts), pc)
                else:
                    rep.touch_cfg(pin, pc)
            self._sync_pin(i)
        return None

    def _snapshot(self, i: int, reg: Any, value: int) -> tuple[int, int, int, int]:
        """(mode, otype, pupd, af) of pin i with register `reg` holding `value`."""
        def fld(name: str, lsb: int, width: int) -> int:
            v = value if reg.name == name else self.get(name)
            return (v >> lsb) & ((1 << width) - 1)
        afreg = "AFRL" if i < 8 else "AFRH"
        return (fld("MODER", 2 * i, 2), fld("OTYPER", i, 1), fld("PUPDR", 2 * i, 2), fld(afreg, 4 * (i & 7), 4))

    def _lckr_write(self, blk, reg, old, new):
        if self._locked:
            return (self._locked & 0xFFFF) | LCKK
        mask = new & 0xFFFF
        k = new & LCKK
        seq = self._lock_seq
        if seq == 0 and k:
            self._lock_seq, self._lock_val = 1, mask
        elif seq == 1 and not k and mask == self._lock_val:
            self._lock_seq = 2
        elif seq == 2 and k and mask == self._lock_val:
            self._lock_seq = 3
        else:
            self._lock_seq = 1 if k else 0
            self._lock_val = mask
        return mask

    def _lckr_read(self, blk, reg):
        if self._lock_seq == 3:
            self._lock_seq = 0
            self._locked = self._lock_val
            self.emit("info", block=self.name, msg=f"{self.name}: pins 0x{self._locked:04x} locked (LCKR)")
        if self._locked:
            return self._locked | LCKK
        return None

    # ------------------------------------------------------------ input
    def _idr_read(self, blk, reg):
        self.n_idr += 1
        v = self._idr
        return v if v is not None else self._idr_compute()

    def _idr_compute(self) -> int:
        v = 0
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        moder = self.get("MODER")
        for i, pin in enumerate(self.pins):
            if pin is None:
                continue
            mode = (moder >> (2 * i)) & 3
            if mode == 3:
                continue
            lvl = pin.level()
            if rep is not None and mode == 0 and self.touched >> i & 1:
                rep.touch_data(pin, pc)
            if lvl == "H":
                v |= 1 << i
            elif lvl == "X" and mode == 0 and self.touched >> i & 1:
                self.x_read_mask |= 1 << i
                if i not in self._warned_x:
                    self._warned_x.add(i)
                    self.emit("warn", block=self.name, pin=pin.id,
                              msg=f"reading floating pin {pin.id} (no pull, nothing connected): reads 0")
        self._idr = v
        o = self._idr_off
        self.mem[o:o + 4] = v.to_bytes(4, "little")
        return v
