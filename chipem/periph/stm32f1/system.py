"""STM32F1 flash interface (FPEC), option bytes, system memory (device electronic signature) and DBGMCU.

Sources: RM0008 Rev 21 3.3.3 (Flash memory: organisation Tables 4-8, "Reading the Flash memory", FLASH_ACR), 7.1.1
(low-power management reset), 30 (device electronic signature), 31.6.1 / 31.16 (DBGMCU).  RM0008 refers everything
else about the FPEC (KEYR/OPTKEYR keys, SR, CR, AR, OBR, WRPR, option byte layout and programming) to the flash
programming manual PM0075 (PM0068 for XL density), which was not available: those parts follow ST's drivers (SPL/HAL
FLASH code), the SVD layout and DS15060 (Table 28 flash timings) and are not verified.

FLASH (`stm32f1.flash`, 0x40022000):
* ACR (RM0008 3.3.3, verified): reset 0x30; LATENCY (0..2), HLFCYA, PRFTBE (reset 1), PRFTBS (read-only, follows
  PRFTBE).  Wait states have no timing effect, but one `warn` each when: LATENCY is too low for SYSCLK (0 up to 24 MHz,
  1 up to 48 MHz, 2 up to 72 MHz: silicon would fetch wrong instructions); HLFCYA is set with SYSCLK above 8 MHz, an
  AHB prescaler or the PLL; PRFTBE is 0 with an AHB prescaler; PRFTBE is switched with SYSCLK above 24 MHz or an AHB
  prescaler.  A program or erase with the HSI off logs one `warn` (3.3.3: the HSI must be on).
* (PM0075, not verified) KEYR 0x45670123 then 0xCDEF89AB clears CR.LOCK; any wrong key locks the FPEC until the next
  reset (one `warn`; the silicon also returns a bus error).  OPTKEYR with the same keys sets CR.OPTWRE.  CR.LOCK can
  only be set by software.  CR writes while LOCK = 1 are ignored.
* (PM0075) Page erase (PER, AR, STRT; 1 KB pages on low/medium density, RM0008 Table 4) and mass erase (MER, STRT; the
  information block is not affected, 3.3.3): BSY = 1 for the erase time (params `erase_ns`, default 30 ms: DS15060
  Table 28 tERASE / tME 20-40 ms), then the page / the whole main flash reads 0xFF, EOP = 1 (FLASH vector with EOPIE).
  A write-protected page (WRPR bit clear, 4 KB = 4 pages per bit) is not erased and sets WRPRTERR.
* (PM0075) Programming (PG = 1, half-words, 3.3.3): CHIP_EM maps the flash as memory the CPU writes directly (no write
  hook in the core), so the FPEC checks each programmed half-word after the fact, at the next FLASH register access
  (firmware polls SR.BSY after every half-word): a half-word that was not erased (not 0xFFFF; writing 0x0000 is
  allowed) is restored and sets PGERR, one in a write-protected page is restored and sets WRPRTERR, a good one sets BSY
  for tPROG (params `prog_ns`, default 52.5 us, Table 28) and then EOP.  Flash changes made while PG = 0 are restored
  with one `warn` (silicon ignores such writes / bus-faults).
* (PM0075) SR: BSY read-only; PGERR, WRPRTERR, EOP write-1-to-clear.  FLASH vector = (EOP & EOPIE) | (errors & ERRIE).
* (PM0075) OBR / WRPR: read-only images of the option bytes loaded at reset: OBR = OPTERR | RDPRT << 1 | USER << 2 |
  Data0 << 10 | Data1 << 18 (RDPRT = RDP != 0xA5), WRPR = WRP3..WRP0.
* (PM0075) Option bytes (`stm32f1.optbytes`, 0x1FFFF800-0x1FFFF80F, RM0008 Table 4; eight half-words: RDP, USER,
  Data0, Data1, WRP0-3, each byte followed by its complement): OPTER + STRT erases them (to 0xFFFF; needs OPTWRE),
  OPTPG + a half-word write programs the low byte (the complement is written by hardware).  They take effect at the
  next reset.  Factory state: RDP 0xA5 (no read protection), USER/Data/WRP 0xFF.  USER bit 0 WDG_SW = 0 starts the
  IWDG at every reset (hardware watchdog, RM0008 19.3.1); bits 1/2 nRST_STOP / nRST_STDBY = 0 make Stop / Standby entry
  reset the chip (RM0008 7.1.1 low-power management reset; stm32f1.pwr asks `rst_on(mode)`).  Read protection only
  shows in OBR (no debugger access to block).
System memory (`stm32f1.sysmem`, 0x1FFFF000, 2 KB, RM0008 Table 4): the ST bootloader is not available, so it reads 0xFF
except the device electronic signature (RM0008 ch. 30, verified): flash size register F_SIZE at 0x1FFFF7E0 (16-bit,
KB; params `flash_kb`, default from the FLASH memory region) and the 96-bit unique ID at 0x1FFFF7E8 (params `uid`,
three 32-bit words; byte/half-word/word reads).  Writes are ignored.
DBGMCU (`stm32f1.dbgmcu`, 0xE0042000, RM0008 31.6.1 / 31.16.3, verified): IDCODE = REV_ID << 16 | DEV_ID (params
`density`: 0x412 low, 0x410 medium, 0x414 high, 0x430 XL, 0x418 connectivity line; default low = 0x10006412, revision
A; `rev_id` / `idcode` override; the reserved bits 15:12 read 0x6 as on silicon).  CR survives system resets (power-on
reset only); TRACE_IOEN / TRACE_MODE assign the trace pins (stm32f1.gpio); DBG_SLEEP/STOP/STANDBY and the DBG_xxx_STOP freeze bits act only while a debugger halts the
core, which CHIP_EM does not model: storage.  The errata "IDCODE reads 0 without a debugger" is not in RM0008 (31.6.1
says the code is readable by user software): `idcode_readable: false` models it.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock, pure_read
from ..registry import register
from ._util import build_regs, find_model, irq_number, rcc

KEYS = (0x45670123, 0xCDEF89AB)
PG, PER, MER, OPTPG, OPTER, STRT, LOCK, OPTWRE, ERRIE, EOPIE = (1 << 0, 1 << 1, 1 << 2, 1 << 4, 1 << 5, 1 << 6,
                                                              1 << 7, 1 << 9, 1 << 10, 1 << 12)
BSY, PGERR, WRPRTERR, EOP = 1 << 0, 1 << 2, 1 << 4, 1 << 5
PAGE = 1024
OB_FACTORY = [0x5AA5, 0x00FF, 0x00FF, 0x00FF, 0x00FF, 0x00FF, 0x00FF, 0x00FF]   # RDP USER D0 D1 WRP0-3


@register("stm32f1.flash")
class FLASH(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"ACR": {"on_write": self._acr_write, "ro_mask": 0xFFFFFFE0},
              "KEYR": {"on_write": self._keyr_write, "on_read": pure_read(lambda b, r: 0)},
              "OPTKEYR": {"on_write": self._optkeyr_write, "on_read": pure_read(lambda b, r: 0)},
              "SR": {"w1c_mask": PGERR | WRPRTERR | EOP, "ro_mask": 0xFFFFFFCB, "on_read": self._sr_read, "on_peek": self._sr_peek,
                     "on_write": self._sr_write},
              "CR": {"on_write": self._cr_write, "ro_mask": 0xFFFFE908},
              "AR": {"on_read": pure_read(lambda b, r: 0)},
              "OBR": {"ro_mask": 0xFFFFFFFF}, "WRPR": {"ro_mask": 0xFFFFFFFF}}
        r = build_regs((name, "FLASH"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        # page size: 1 KB (low/medium density, RM0008 3.3.3 Tables 4/5) or 2 KB (high density, Table 6; params
        # `page_size: 2048`).  Write protection (PM0075 FLASH_WRPR): 4 KB per WRPR bit; on 2 KB-page parts bits
        # 0-30 cover 2 pages each and bit 31 the rest of the flash (pages 62-255)
        self.page = int(params.get("page_size", PAGE))
        self.erase_ns = float(params.get("erase_ns", 30e6))
        self.prog_ns = float(params.get("prog_ns", 52.5e3))
        self.ob = list(params.get("option_bytes") or OB_FACTORY)       # programmed option bytes (survive resets)
        self._key = 0                     # KEYR sequence position
        self._optkey = 0
        self._dead = False                # wrong key: locked until reset
        self._busy_until = -1.0
        self._done_ev = None
        self._pending_sr = 0              # SR flags set when the running operation completes
        self._snap: bytes | None = None   # flash content when PG was set / last checked
        self._fbase = 0x08000000
        self._fsize = 0
        self._irqn: int | None = None
        self._line = False
        self._warned: set[str] = set()
        self.programmed = 0               # half-words programmed (statistics)
        self.erased_pages = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for m in getattr(chip.desc, "memory", []) or []:
            if m.kind == "flash":
                self._fbase, self._fsize = m.base, m.size
                break
        self._irqn = irq_number(chip, "FLASH")
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._check_latency)

    def reset(self) -> None:
        if self._done_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._done_ev)
        self._done_ev = None
        super().reset()
        self._key = self._optkey = 0
        self._dead = False
        self._busy_until = -1.0
        self._pending_sr = 0
        self._snap = None
        self._warned.clear()
        if self._line and self.chip is not None:
            self.irq(self._irqn, False)
        self._line = False
        self._load_option_bytes()
        if self.chip is not None:
            self._check_latency()
            self._hw_watchdog()

    # ------------------------------------------------------------ option bytes
    def _load_option_bytes(self) -> None:
        ob = self.ob
        rdp = ob[0] & 0xFF
        user, d0, d1 = ob[1] & 0xFF, ob[2] & 0xFF, ob[3] & 0xFF
        bad = any(((v >> 8) ^ v) & 0xFF != 0xFF for v in ob if v != 0xFFFF)
        self.set("OBR", (1 if bad else 0) | ((rdp != 0xA5) << 1) | (user << 2) | (d0 << 10) | (d1 << 18))
        self.set("WRPR", sum((ob[4 + k] & 0xFF) << (8 * k) for k in range(4)))

    def user_bit(self, bit: int) -> bool:
        return bool((self.get("OBR") >> (2 + bit)) & 1)

    def rst_on(self, mode: str) -> bool:
        """Option byte nRST_STOP / nRST_STDBY = 0: entering that mode resets the chip."""
        return not self.user_bit(1 if mode == "stop" else 2)

    def page_protected(self, page: int) -> bool:
        bit = page * self.page // 4096
        return not (self.get("WRPR") >> min(bit, 31)) & 1

    def _hw_watchdog(self) -> None:
        """USER.WDG_SW = 0 (hardware watchdog): the IWDG is started by every reset (the chip YAML lists IWDG before
        FLASH, so the IWDG has already been reset when this runs)."""
        if self.chip is None or self.user_bit(0):
            return
        iw = find_model(self.chip, "stm32.iwdg", "IWDG")
        if iw is not None and not getattr(iw, "running", False):
            iw.write(iw.regs["KR"].offset, 4, 0xCCCC)

    # ------------------------------------------------------------ helpers
    def _now(self) -> float:
        return self.chip.now_ns() if self.chip is not None else 0.0

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _check_latency(self) -> None:
        """RM0008 3.3.3 "Reading the Flash memory": wait states follow SYSCLK (0 up to 24 MHz, 1 up to 48 MHz, 2 up
        to 72 MHz); half-cycle access only with SYSCLK <= 8 MHz from HSI/HSE (not the PLL) and no AHB prescaler; the
        prefetch buffer must be on with an AHB prescaler other than 1."""
        r = rcc(self.chip) if self.chip is not None else None
        hz = getattr(r, "sysclk_hz", None) or self.chip.clock.hz
        need = 0 if hz <= 24e6 * 1.001 else (1 if hz <= 48e6 * 1.001 else 2)
        acr = self.get("ACR")
        lat = acr & 7
        if lat < need:
            self._warn_once(f"lat{need}", f"FLASH_ACR.LATENCY = {lat} is too low for SYSCLK {hz / 1e6:.3f} MHz (needs "
                            f"{need} wait state{'s' if need > 1 else ''}): silicon would fetch wrong instructions")
        hpre = getattr(r, "hpre", 1) if r is not None else 1
        if acr & 0x8 and (hz > 8e6 * 1.001 or hpre != 1 or getattr(r, "source", "HSI") == "PLL"):
            self._warn_once("hlfcya", "FLASH_ACR.HLFCYA (half-cycle access) is only allowed with SYSCLK <= 8 MHz from "
                            "HSI/HSE and no AHB prescaler (RM0008 3.3.3)")
        if hpre != 1 and not acr & 0x10:
            self._warn_once("prft", "FLASH_ACR.PRFTBE = 0 with an AHB prescaler: the prefetch buffer must be kept on "
                            "(RM0008 3.3.3)")

    def _update_irq(self) -> None:
        sr, cr = self.get("SR"), self.get("CR")
        lvl = bool((sr & EOP and cr & EOPIE) or (sr & (PGERR | WRPRTERR) and cr & ERRIE))
        if lvl != self._line and self.chip is not None and self._irqn is not None:
            self._line = lvl
            self.irq(self._irqn, lvl)

    def _flash_bytes(self) -> bytes:
        return bytes(self.chip.core.read_mem(self._fbase, self._fsize))

    def _busy(self) -> bool:
        return self._now() < self._busy_until

    def _start_op(self, ns: float, flags: int) -> None:
        self._busy_until = self._now() + ns
        self._pending_sr |= flags
        self.set("SR", self.get("SR") | BSY)
        if self._done_ev is not None:
            self.chip.sched.cancel(self._done_ev)
        self._done_ev = self.chip.sched.at_ns(self._busy_until, self._op_done)

    def _op_done(self) -> None:
        self._done_ev = None
        self.set("SR", (self.get("SR") & ~BSY) | self._pending_sr)
        self._pending_sr = 0
        self._update_irq()

    def _check_program(self) -> None:
        """Validate the half-words the CPU wrote into the flash region since the last check (see docstring)."""
        if self.chip is None or not self._fsize:
            return
        cur = self._flash_bytes()
        snap = self._snap
        if snap is None:
            self._snap = cur
            return
        if cur == snap:
            return
        cr = self.get("CR")
        ok = 0
        err = 0
        fix: list[tuple[int, bytes]] = []
        n = len(cur)
        i = 0
        while i < n:
            # skip equal 256-byte chunks quickly
            if cur[i:i + 256] == snap[i:i + 256]:
                i += 256
                continue
            for j in range(i, min(i + 256, n), 2):
                old = int.from_bytes(snap[j:j + 2], "little")
                new = int.from_bytes(cur[j:j + 2], "little")
                if old == new:
                    continue
                if not cr & PG or cr & LOCK:
                    fix.append((j, snap[j:j + 2]))
                    self._warn_once("nopg", f"flash write at 0x{self._fbase + j:08x} without FLASH_CR.PG (or "
                                    f"locked): ignored, as on silicon")
                elif self.page_protected(j // self.page):
                    fix.append((j, snap[j:j + 2]))
                    err |= WRPRTERR
                elif old != 0xFFFF and new != 0:
                    fix.append((j, snap[j:j + 2]))
                    err |= PGERR
                    self._warn_once("pgerr", f"flash half-word 0x{self._fbase + j:08x} programmed while not "
                                    f"erased (0x{old:04x} -> 0x{new:04x}): PGERR, not written")
                else:
                    ok += 1
            i += 256
        for j, b in fix:
            self.chip.core.write_mem(self._fbase + j, b)
        if fix:
            self.chip.core.flush_code_cache()
        self._snap = self._flash_bytes() if fix else cur
        if err:
            self.set("SR", self.get("SR") | err)
            self._update_irq()
        if ok:
            self._hsi_check()
            self.programmed += ok
            self._start_op(self.prog_ns * ok, EOP)

    # ------------------------------------------------------------ register hooks
    def _acr_write(self, blk, reg, old, new):
        if (old ^ new) & 0x10 and self.chip is not None:
            r = rcc(self.chip)
            if r is not None and (r.sysclk_hz > 24e6 * 1.001 or r.hpre != 1):
                self._warn_once("prft_sw", "FLASH_ACR.PRFTBE changed with SYSCLK above 24 MHz or an AHB prescaler: "
                                "the prefetch buffer may only be switched with SYSCLK < 24 MHz and HCLK = SYSCLK "
                                "(RM0008 3.3.3)")
        v = (new & ~0x20) | (0x20 if new & 0x10 else 0)      # PRFTBS follows PRFTBE
        self._store(reg, v)
        self._check_latency()
        return v

    def _keyr_write(self, blk, reg, old, new):
        if self._dead or not self.get("CR") & LOCK:
            return 0
        if new == KEYS[self._key]:
            self._key += 1
            if self._key == 2:
                self._key = 0
                self.set("CR", self.get("CR") & ~LOCK)
        else:
            self._dead = True
            self._warn_once("key", "FLASH_KEYR: wrong unlock sequence: the FPEC stays locked until the next reset "
                            "(silicon also raises a bus error)")
        return 0

    def _optkeyr_write(self, blk, reg, old, new):
        if self.get("CR") & LOCK:
            return 0
        if new == KEYS[self._optkey]:
            self._optkey += 1
            if self._optkey == 2:
                self._optkey = 0
                self.set("CR", self.get("CR") | OPTWRE)
        else:
            self._optkey = 0
        return 0

    def _sr_peek(self, blk, reg):
        if self.get("CR") & PG and not self._busy():
            # the read validates the half-words written into flash since (and may start a program operation):
            # its result is not known without doing it -> raise = not peekable now (MMIOBlock.peek_reg)
            raise LookupError("FLASH_SR with PG set: read checks pending flash writes")
        return None

    def _sr_read(self, blk, reg):
        if self.get("CR") & PG and not self._busy():
            self._check_program()
        return None

    def _sr_write(self, blk, reg, old, new):
        self._update_irq()
        return None

    def _cr_write(self, blk, reg, old, new):
        if old & LOCK:
            self._warn_once("crlock", "FLASH_CR write while LOCK = 1: ignored (unlock with FLASH_KEYR first)")
            return old
        if self._busy() and (new ^ old) & (PG | PER | MER | OPTPG | OPTER | STRT):
            self._warn_once("busy", "FLASH_CR changed while SR.BSY = 1")
        if old & PG:
            self._check_program()
        if new & PG and not old & PG:
            self._snap = self._flash_bytes() if self.chip is not None else None
        if not new & PG and old & PG:
            self._snap = None
        if new & OPTWRE:                    # OPTWRE: set only by the OPTKEYR sequence, cleared by writing 0
            new = (new & ~OPTWRE) | (old & OPTWRE)
        if new & STRT and not old & STRT:
            new = self._start(new)
        if new & LOCK:
            new &= ~OPTWRE
        self._store(reg, new)
        self._update_irq()
        return new

    def _hsi_check(self) -> None:
        """RM0008 3.3.3: the HSI must be on for Flash write and erase operations (the FPEC is clocked by it)."""
        r = rcc(self.chip) if self.chip is not None else None
        if r is not None and not r.get("CR") & 2:
            self._warn_once("hsi", "Flash program/erase with the HSI off: the HSI must be ON for write and erase "
                            "operations (RM0008 3.3.3)")

    def _start(self, cr: int) -> int:
        chip = self.chip
        if chip is None:
            return cr & ~STRT
        if cr & (PER | MER | OPTER):
            self._hsi_check()
        if cr & PER:
            addr = self.get("AR")
            off = addr - self._fbase
            if not 0 <= off < self._fsize:
                self._warn_once("ar", f"FLASH_AR = 0x{addr:08x} is outside the main flash: page erase ignored")
                return cr & ~STRT
            page = off // self.page
            if self.page_protected(page):
                self.set("SR", self.get("SR") | WRPRTERR)
                return cr & ~STRT
            chip.core.write_mem(self._fbase + page * self.page, b"\xff" * self.page)
            chip.core.flush_code_cache()
            self.erased_pages += 1
            self.emit("info", block=self.name, msg=f"flash page {page} (0x{self._fbase + page * self.page:08x}) erased")
            self._start_op(self.erase_ns, EOP)
        elif cr & MER:
            if any(self.page_protected(p) for p in range(self._fsize // self.page)):
                self.set("SR", self.get("SR") | WRPRTERR)
                return cr & ~STRT
            chip.core.write_mem(self._fbase, b"\xff" * self._fsize)
            chip.core.flush_code_cache()
            self.emit("info", block=self.name, msg="flash mass erase")
            self._start_op(self.erase_ns, EOP)
        elif cr & OPTER:
            if not cr & OPTWRE:
                self._warn_once("optwre", "option byte erase without CR.OPTWRE (OPTKEYR sequence): ignored")
                return cr & ~STRT
            self.ob = [0xFFFF] * 8
            self.emit("info", block=self.name, msg="option bytes erased (effective after reset)")
            self._start_op(self.erase_ns, EOP)
        if self._snap is not None:
            self._snap = self._flash_bytes()
        return cr & ~STRT

    def program_option_byte(self, index: int, value: int) -> None:
        """OB block: a half-word write to option byte `index` (0..7) with OPTPG."""
        cr = self.get("CR")
        if not cr & OPTPG or not cr & OPTWRE:
            self._warn_once("optpg", "option byte write without CR.OPTPG / OPTWRE: ignored")
            return
        if self.ob[index] != 0xFFFF:
            self.set("SR", self.get("SR") | PGERR)
            self._update_irq()
            return
        lo = value & 0xFF
        self.ob[index] = lo | ((~lo & 0xFF) << 8)
        self._start_op(self.prog_ns, EOP)


@register("stm32f1.optbytes")
class OptionBytes(MMIOBlock):
    """Option bytes at 0x1FFFF800 (8 half-words); reads the programmed values, writes go through the FPEC."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, (), ram_like=False, log=log, params=params)
        self._flash: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._flash = find_model(chip, "stm32f1.flash", "FLASH")

    def _ob(self) -> list[int]:
        return self._flash.ob if self._flash is not None else OB_FACTORY

    def read(self, offset: int, size: int) -> int:
        ob = self._ob()
        v = 0
        for k in range(size):
            o = offset + k
            b = (ob[o // 2] >> (8 * (o & 1))) & 0xFF if o < 16 else 0xFF
            v |= b << (8 * k)
        return v

    def write(self, offset: int, size: int, value: int) -> None:
        if self._flash is None or offset >= 16 or offset & 1 or size != 2:
            self.emit("warn", block=self.name, msg=f"option bytes: {size}-byte write at +0x{offset:x} ignored "
                      f"(half-word writes with FLASH_CR.OPTPG only)")
            return
        self._flash.program_option_byte(offset // 2, value)


@register("stm32f1.sysmem")
class SystemMemory(MMIOBlock):
    """System memory 0x1FFFF000: bootloader (not available: reads 0xFF), F_SIZE at +0x7E0, unique ID at +0x7E8.
    `params.fsize_offset` / `uid_offset` move the two signatures for other layouts (STM32F401/F411: block at 0x1FFF7800
    = OTP area, erased 0xFF, with UID at +0x210 = 0x1FFF7A10 and F_SIZE at +0x222 = 0x1FFF7A22, RM0368/RM0383 24.1/24.2).
    """

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, (), ram_like=False, log=log, params=params)
        p = self.params
        self.flash_kb = p.get("flash_kb")
        self.uid = [int(x) for x in (p.get("uid") or (0x0034FF6D, 0x34375131, 0x43142357))]
        self.fsize_offset = int(p.get("fsize_offset", 0x7E0))
        self.uid_offset = int(p.get("uid_offset", 0x7E8))

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self.flash_kb is None:
            fl = next((m for m in getattr(chip.desc, "memory", []) if m.kind == "flash"), None)
            self.flash_kb = fl.size // 1024 if fl is not None else 32
        data = bytearray(b"\xff" * self.size)
        fo, uo = self.fsize_offset, self.uid_offset
        data[fo:fo + 2] = int(self.flash_kb).to_bytes(2, "little")
        for k, w in enumerate(self.uid):
            data[uo + 4 * k:uo + 4 + 4 * k] = (w & 0xFFFFFFFF).to_bytes(4, "little")
        self.mem[:] = data

    def reset(self) -> None:
        pass                                   # read-only content set in attach

    def read(self, offset: int, size: int) -> int:
        return int.from_bytes(self.mem[offset:offset + size], "little")

    def write(self, offset: int, size: int, value: int) -> None:
        self.emit("warn", block=self.name, msg=f"write to system memory 0x{self.base + offset:08x} ignored (ROM)")


# density -> (DEV_ID, default REV_ID) (RM0008 31.6.1)
DEV_IDS = {"low": (0x412, 0x1000), "medium": (0x410, 0x2003), "high": (0x414, 0x1003), "xl": (0x430, 0x1000),
           "cl": (0x418, 0x1001)}


@register("stm32f1.dbgmcu")
class DBGMCU(MMIOBlock):
    """DBGMCU_IDCODE (31.6.1) and DBGMCU_CR (31.16.3).  `params`: `density` (low / medium / high / xl / cl: DEV_ID
    0x412 / 0x410 / 0x414 / 0x430 / 0x418 with the latest REV_ID of that table), `rev_id` (16-bit), or `idcode` (the
    whole register); `idcode_readable` (default true: RM0008 31.6.1 says the code is readable by user software; set
    false to model the errata of early medium/high-density revisions, where the CPU reads 0 without a debugger)."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        density = str(params.get("density") or "low").lower()
        dev, rev = DEV_IDS.get(density, DEV_IDS["low"])
        rev = int(params.get("rev_id", rev))
        # bits 15:12 are reserved in 31.6.1 but read 0x6 on silicon (debug probes report 0x10006412, 0x20036410,
        # 0x10036414 ...): kept
        self.idcode = int(params.get("idcode", (rev << 16) | 0x6000 | dev))
        self.idcode_readable = bool(params.get("idcode_readable", True))
        # CR: bits 25-30 (TIM9-14, XL density) and 21 (CAN2, connectivity line) are reserved elsewhere
        cr_rw = 0x0001FFE7 | (1 << 17 | 1 << 18 | 1 << 19 | 1 << 20)
        if density == "xl":
            cr_rw |= 0x7E000000
        if density == "cl":
            cr_rw |= 1 << 21
        ov = {"IDCODE": {"reset": self.idcode, "ro_mask": 0xFFFFFFFF, "on_read": self._idcode_read},
              "CR": {"ro_mask": ~cr_rw & 0xFFFFFFFF, "on_write": self._cr_write}}
        r = build_regs((name, "DBG", "DBGMCU"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._powered = False

    pure_reads = frozenset({"IDCODE"})

    def _idcode_read(self, blk, reg):
        return self.idcode if self.idcode_readable else 0

    def reset(self) -> None:
        """DBGMCU_CR is reset by a power-on reset only, not by a system reset (31.16.3)."""
        keep = self.get("CR") if self._powered else 0
        super().reset()
        self.set("CR", keep)
        self._powered = True

    def _cr_write(self, blk, reg, old, new):
        if (old ^ new) & 0xE0 and self.chip is not None:       # TRACE_IOEN / TRACE_MODE: trace pin assignment
            self._store(reg, new)
            from ._util import gpio_ports
            for p in gpio_ports(self.chip):
                p.resync_all()
        return None


__all__ = ["FLASH", "OptionBytes", "SystemMemory", "DBGMCU"]
