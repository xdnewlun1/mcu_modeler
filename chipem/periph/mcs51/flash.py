"""P89CV51 flash: IAP calls, boot block, boot vector / status bit, security bits, the FX2 (6-clock) bit
(data sheet sec. 6.3 pp.17-26, Tables 10-12; sec. 6.10 Table 40; sec. 6.13.1.1).

Boot block: 1 KB at FC00H-FFFFH that "logically overlays the program memory ... when it is enabled" by AUXR1.ENBOOT
(bit 5).  We have no copy of NXP's boot code: while ENBOOT = 1 the overlay holds an empty ROM image (00H bytes) and
the IAP entry PGM_MTP at FFF0H is emulated natively (`core.install_native`), so `LCALL 0FFF0H` runs the requested
function in Python and returns like RET.  The user's FC00H-FFFFH flash content is kept aside while the overlay is
mapped (MOVC sees the boot ROM, as on silicon).
IAP functions (Table 12, function code in R1; bit 7 of R1 set = also feed the watchdog):
  00 read ID (DPL 00 manufacturer, 01 / 02 device IDs, 03 6x/12x bit (bit 7 = 1: 6-clock), 80 boot code version)
  01 erase 8/16 KB block (DPH 00, 20, 40, 80, C0: 0-8K, 8-16K, 16-32K, 32-48K, 48-64K)
  02 program byte ACC at DPH:DPL (programming only clears bits: new = old AND ACC)
  03 read byte at DPH:DPL -> ACC
  04 erase status bit and boot vector (boot vector -> FCH, status bit -> 0)
  05 program security bit DPL = 0, 1, 2 (bits 1 write protect, 2 read protect, 3 external execution inhibit)
  06 program DPL = 00 status bit (-> 1), 01 boot vector (ACC), 02 6x/12x bit (FX2: 6-clock mode from now on)
  07 read DPL = 00 security bits (bit 0-2 = security bits 1-3, bit 7 = FX2: our reading of the garbled "ACC = 00
     SoftICE S/N-match 0 SB 0 DBL_CLK" line), 01 status bit, 02 boot vector (the table says "ACC = 07H": R1 is used
     like every other call)
  08 erase 128-byte page at DPH:DPL
  0C erase 4 KB block (DPH = block address high byte; R0 = oscillator MHz, ignored)
Return: ACC = 00H pass, non-zero fail (unknown function: 0FFH; program/erase with security bit 1 programmed: 01H;
address beyond the device's flash: 02H).  The ID bytes are not in the data sheet: `params.ids` (default manufacturer
15H, IDs C2H / 01H as placeholders).  Durations: `params.page_erase_ms` (default 0: the data sheet gives "30 ms page
erase, 150 ms block erase" as typical figures) and `iap_us_per_byte` are charged as CPU time of the call.
Reset (sec. 6.3.4-6.3.5): PC = 0000H, unless the status bit is 1 or PSEN is held low at reset: then PC = boot vector
<< 8 with ENBOOT = 1.  Starting at the default boot vector FCH means NXP's ISP bootloader, which is not emulated:
one `warn` and the core stops at FC00H (`params.isp: halt`, default).  Security bits only matter for ISP / parallel
programming (no effect on IAP, sec. 6.10) and are reported, not enforced (except write protect for IAP writes).
The non-volatile state (boot vector, status bit, security bits, FX2) lives for the session; params give the initial
values.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import SFR_BASE, find_model

ENBOOT = 0x20
BOOT_LO = 0xFC00
IAP_ENTRY = 0xFFF0
PAGE = 128


@register("mcs51.flash")
class Flash(MMIOBlock):
    model = "mcs51.flash"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, SFR_BASE, 0, (), ram_like=False, log=log, params=params)   # no SFRs of its own
        p = self.params
        self.boot_vector = int(p.get("boot_vector", 0xFC)) & 0xFF
        self.status_bit = int(p.get("status_bit", 0)) & 1
        self.fx2 = bool(p.get("fx2", False))
        self.security = int(p.get("security", 0)) & 7
        ids = p.get("ids") or {}
        self.ids = {0x00: 0x15, 0x01: 0xC2, 0x02: 0x01, 0x80: 0x01}
        self.ids.update({int(k): int(v) for k, v in ids.items()})
        self.page_erase_ms = float(p.get("page_erase_ms", 0))
        self.us_per_byte = float(p.get("iap_us_per_byte", 0))
        self.isp = str(p.get("isp", "halt"))
        self._saved: bytes | None = None          # user flash under the boot overlay
        self.calls: list[tuple[int, int, int]] = []    # (function, dptr, result)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        core = chip.core
        if hasattr(core, "on_auxr1"):
            core.on_auxr1 = self._auxr1_changed
        self.flash_size = 0
        for m in chip.desc.memory:
            if m.kind == "flash" and m.base == 0:
                self.flash_size = m.size

    def reset(self) -> None:
        super().reset()
        if self.chip is None:
            return
        self._overlay(False)
        arch = self.chip.arch
        psen_low = False
        try:
            psen_low = self.chip.pins.get("PSEN").level() == "L"
        except (KeyError, AttributeError):
            pass
        if self.status_bit or psen_low:
            arch.boot_pc = self.boot_vector << 8
            self._boot_enboot = True
        else:
            arch.boot_pc = 0
            self._boot_enboot = False
        sc = find_model(self.chip, "mcs51.sysctrl")
        if sc is not None:
            sc.fx2 = self.fx2

    def after_reset(self) -> None:
        """Called by the arch after the core reset (sets ENBOOT for a boot-vector start)."""
        if getattr(self, "_boot_enboot", False):
            core = self.chip.core
            a = getattr(core, "auxr1", None)
            if a is not None:
                core._wr_auxr1(core.sfr[a] | ENBOOT)
            pc = self.chip.arch.boot_pc
            if pc == BOOT_LO and self.isp == "halt":
                self.emit("warn", block=self.name, msg="reset into the ISP bootloader (status bit set or PSEN low): "
                          "NXP's boot code is not emulated; execution stops at FC00H")

    # ------------------------------------------------------------------ boot overlay
    def _auxr1_changed(self, old: int, new: int) -> None:
        if (old ^ new) & ENBOOT:
            self._overlay(bool(new & ENBOOT))

    def _overlay(self, on: bool) -> None:
        core = self.chip.core
        if not hasattr(core, "code_swap"):
            return
        if on and self._saved is None:
            rom = bytearray(0x400)
            if self.isp == "halt":
                rom[0:2] = b"\x80\xfe"                 # FC00H: SJMP $ (ISP bootloader not emulated)
            rom[IAP_ENTRY - BOOT_LO] = 0x22            # FFF0H: RET (the native IAP hook runs instead)
            self._saved = core.code_swap(BOOT_LO, bytes(rom))
            core.install_native(IAP_ENTRY, self._iap)
        elif not on and self._saved is not None:
            core.code_swap(BOOT_LO, self._saved)
            self._saved = None
            core.install_native(IAP_ENTRY, None)

    def _user_read(self, a: int) -> int:
        if self._saved is not None and a >= BOOT_LO:
            return self._saved[a - BOOT_LO]
        return self.chip.core.code[a]

    def _user_write(self, a: int, v: int) -> None:
        if self._saved is not None and a >= BOOT_LO:
            b = bytearray(self._saved)
            b[a - BOOT_LO] = v
            self._saved = bytes(b)
            return
        self.chip.core.write_mem(a, bytes([v]))

    def _erase(self, lo: int, n: int) -> None:
        for a in range(lo, min(lo + n, 0x10000)):
            if a < self.flash_size:
                self._user_write(a, 0xFF)

    # ------------------------------------------------------------------ IAP
    def _iap(self) -> int:
        core = self.chip.core
        r1 = core.read_reg("r1")
        fn = r1 & 0x7F
        dpl, dph = core.read_reg("dpl"), core.read_reg("dph")
        dptr = dph << 8 | dpl
        acc = core.read_reg("a")
        if r1 & 0x80:
            w = find_model(self.chip, "mcs51.wdt")
            if w is not None:
                w.feed()
        res = 0
        busy_us = 0.0
        wp = bool(self.security & 1)
        if fn == 0x00:
            res = self.fx2 << 7 if dpl == 0x03 else self.ids.get(dpl, 0xFF)
        elif fn == 0x03:
            res = self._user_read(dptr) if dptr < self.flash_size else 0xFF
        elif fn == 0x02:
            if wp:
                res = 0x01
            elif dptr >= self.flash_size:
                res = 0x02
            else:
                self._user_write(dptr, self._user_read(dptr) & acc)
                busy_us = self.us_per_byte
        elif fn in (0x08, 0x0C, 0x01):
            if fn == 0x08:
                lo, n = dptr & ~(PAGE - 1), PAGE
            elif fn == 0x0C:
                lo, n = dph << 8 & 0xF000, 0x1000
            else:
                blocks = {0x00: (0x0000, 0x2000), 0x20: (0x2000, 0x2000), 0x40: (0x4000, 0x4000),
                          0x80: (0x8000, 0x4000), 0xC0: (0xC000, 0x4000)}
                lo, n = blocks.get(dph, (None, 0))
            if wp:
                res = 0x01
            elif lo is None or lo >= self.flash_size:
                res = 0x02
            else:
                self._erase(lo, n)
                busy_us = self.page_erase_ms * 1000.0 * (n // PAGE)
        elif fn == 0x04:
            self.boot_vector, self.status_bit = 0xFC, 0
        elif fn == 0x05:
            if dpl <= 2:
                self.security |= 1 << dpl
            else:
                res = 0xFF
        elif fn == 0x06:
            if dpl == 0x00:
                self.status_bit = 1
            elif dpl == 0x01:
                self.boot_vector = acc
            elif dpl == 0x02:
                self.fx2 = True
                sc = find_model(self.chip, "mcs51.sysctrl")
                if sc is not None:
                    sc.set_fx2(True)
            else:
                res = 0xFF
        elif fn == 0x07:
            res = {0x00: self.security | (self.fx2 << 7), 0x01: self.status_bit, 0x02: self.boot_vector}.get(dpl, 0xFF)
        else:
            res = 0xFF
        core.write_reg("a", res & 0xFF)
        self.calls.append((fn, dptr, res & 0xFF))
        if busy_us:
            mc = int(busy_us * 1e-6 * self.chip.clock.hz / max(getattr(core, "clocks_per_mc", 12), 1))
            core.add_cycles(mc)
        if self.log_all:
            self.emit("info", block=self.name, msg=f"IAP fn {fn:02X} dptr {dptr:04X} -> {res & 0xFF:02X}")
        # return to the caller (as RET)
        sp = core.sfr[0x81]
        hi, lo = core.iram[sp], core.iram[(sp - 1) & 0xFF]
        core.sfr[0x81] = (sp - 2) & 0xFF
        return hi << 8 | lo
