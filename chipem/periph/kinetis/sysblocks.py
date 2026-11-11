"""Kinetis K22 system blocks: MCM (ch. 17), FMC (ch. 28), AIPS-Lite (ch. 20), AXBS crossbar (ch. 18), MPU (ch. 19).

Register files from the SVD (offsets, read-only masks, resets; RM reset values where the SVD differs); behaviour
hand-written.  None of these blocks can raise a bus error from a register access in this emulator (an MMIO callback
cannot fault the core), so accesses that terminate with an error on silicon are ignored and logged once (`warn`),
like the SIM clock gates.

kinetis.mcm (0xE0080000)
  PLASC = 0x1F / PLAMC = 0x37 (crossbar slaves / masters present, read-only).  CR: SRAM_L/SRAM_U arbitration
  (SRAMLAP/SRAMUAP, stored) and write protection SRAMLWP/SRAMUWP: setting one logs one `warn` (writes are not
  blocked: protecting a RAM region would need a core-layer hook).  ISCR: FIOC..FIDC (bits 8-12, 15) are a live copy
  of the core's FPSCR cumulative flags IOC/DZC/OFC/UFC/IXC/IDC, FIOCE..FIDCE (bits 24-28, 31) enable the "MCM"
  interrupt (level: asserted while an enabled flag is set; the handler clears FPSCR).  The FPU sets FPSCR inside
  instructions, so while any enable is set the model re-reads FPSCR every `params.fpu_poll_ns` (default 10 us) and on
  every ISCR access: the IRQ follows the FP exception within that latency.  ETBCC/ETBRL/ETBCNT: stored; RLRQ copies
  ETBRL to ETBCNT and clears DHREQ/NMI/IRQ; no trace packets are counted (no ETB), so the counter never expires; an
  ETBRL value that is not 0 mod 4 is a bus error on silicon (`warn`, not stored).  PID stored.  (The K22 MCM has no
  PLACR / CPO: rev. 5 of the RM replaced PLACR by CR.)
kinetis.fmc (0x4001F000)
  PFAPR (master access / prefetch disable, stored), PFB0CR/PFB1CR with their read-only fields (RWSC wait states,
  memory width) at the RM reset values; CINV_WAY clears the tag, valid and data words of the selected ways and reads
  0, S_B_INV reads 0.  The 4-way x 4-set cache tag (tag[19:6], valid) and data registers are plain storage: the
  emulator does not cache flash, so they only change when software writes them or invalidates a way.
kinetis.aips (AIPS0 0x40000000, AIPS1 0x40080000)
  The K22 RM (ch. 20) documents no AIPS-Lite programming model; the SVD lists MPRA and PACRA-P (reset 0).  They are
  stored; a write that sets a protection bit (SPn supervisor protect, WPn write protect, TPn trusted protect, or
  MPRA master trust/privilege changes) logs one `warn`: peripheral access protection is not enforced.
kinetis.axbs (0x40004000)
  PRS0-4 (master priorities per slave port), CRS0-4, MGPCR0-5 with the SVD/RM resets.  A PRS write with two present
  masters (0, 1, 2, 4, 5: PLAMC) at the same level or a level outside 0-4, or any write to a slave port whose
  CRSn.RO is set, is a bus error on silicon: not stored, one `warn`.  RO is sticky until reset.  Arbitration itself is
  not modelled (one bus master at a time).
kinetis.mpu (0x4000D000)
  CESR (VLD enabled out of reset, NRGD = 12, NSP = 5, HRL = 1, SPERR w1c), EAR0-4/EDR0-4 (read-only), RGD0-11
  WORD0-3 and the RGDAAC0-11 aliases of WORD2.  Resets per RM Table 3-21 and 19.3: RGD0 = whole 4 GB, rwx for the
  core, debugger and DMA, valid; RGD1-11 WORD1 = 0x1F, WORD2/WORD3 = 0.  Writes to WORD0-2 clear the descriptor's
  VLD (19.3.4-6); RGDAAC writes do not.  RGD0 core write restrictions (3.3.7.5): WORD0, WORD1, WORD3 and the debugger
  fields M1SM/M1UM of WORD2/RGDAAC0 cannot be changed by the core (writes ignored).  Access checks are not enforced
  (they would need a hook on every memory access): when the MPU is enabled with a region set that restricts the core
  (master 0, supervisor) anywhere, one `warn` is logged and an `info` describes the regions; no bus fault is raised,
  EARn/EDRn and SPERR are never set by the emulator.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number


class _WarnOnce:
    _warned: set

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)


# ================================================================================================== MCM
ISCR_EN = 0x9F000000           # FIDCE, FIXCE..FIOCE
ISCR_ST = 0x00009F00           # FIDC, FIXC..FIOC
FPSCR_TO_ISCR = ((0, 8), (1, 9), (2, 10), (3, 11), (4, 12), (7, 15))   # IOC DZC OFC UFC IXC IDC


@register("kinetis.mcm")
class MCM(_WarnOnce, MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"CR": {"on_write": self._cr_write},
              "ISCR": {"w1c_mask": 0x6, "on_read": self._iscr_read, "on_write": self._iscr_write,
                       "on_peek": lambda b, r: self._iscr_value()},
              "ETBCC": {"on_write": self._etbcc_write},
              "ETBRL": {"on_write": self._etbrl_write},
              "ETBCNT": {"ro_mask": 0xFFFFFFFF},
              "PID": {"ro_mask": 0xFFFFFF00}}
        r = build_regs((name, "MCM"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.poll_ns = float(params.get("fpu_poll_ns", 10_000.0))
        self._warned: set[str] = set()
        self._irqn: int | None = None
        self._line = False
        self._ev = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "MCM", self.name)

    def reset(self) -> None:
        if self.chip is not None:
            self.chip.sched.cancel(getattr(self, "_ev", None))
            if getattr(self, "_line", False) and self._irqn is not None:
                self.irq(self._irqn, False)
        self._ev = None
        self._line = False
        self._warned = set()
        super().reset()

    def fpu_flags(self) -> int:
        """ISCR status bits from the core's FPSCR."""
        core = getattr(self.chip, "core", None)
        try:
            fpscr = core.read_reg("fpscr") if core is not None else 0
        except Exception:
            fpscr = 0
        v = 0
        for src, dst in FPSCR_TO_ISCR:
            if fpscr >> src & 1:
                v |= 1 << dst
        return v

    def _iscr_value(self) -> int:
        return (self.get("ISCR") & ~ISCR_ST) | self.fpu_flags()

    def _iscr_read(self, blk, reg):
        v = self._iscr_value()
        self._check(v)
        return v

    def _iscr_write(self, blk, reg, old, new):
        self._store(reg, new & ~ISCR_ST)
        self._check(self._iscr_value())
        return None

    def _check(self, v: int | None = None) -> None:
        """Drive the MCM IRQ from the FPU flags and keep polling while an enable is set."""
        if self.chip is None:
            return
        if v is None:
            v = self._iscr_value()
        en = (v & ISCR_EN) >> 16
        line = bool(v & ISCR_ST & en) or bool(v & 0x6)          # NMI/IRQ pending flags (ETB) never set here
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        if en and self._ev is None:
            self._ev = self.chip.sched.at_ns(self.chip.now_ns() + self.poll_ns, self._poll)

    def _poll(self) -> None:
        self._ev = None
        self._check()

    def _cr_write(self, blk, reg, old, new):
        for bit, which in ((30, "SRAM_L"), (26, "SRAM_U")):
            if new >> bit & 1 and not old >> bit & 1:
                self._warn_once(f"wp{bit}", f"MCM_CR: {which} write protection is not enforced (writes still "
                                f"succeed; silicon returns a bus error)")
        return None

    def _etbcc_write(self, blk, reg, old, new):
        if new & 8:                                            # RLRQ
            self.set("ETBCNT", self.get("ETBRL") & 0x7FF)
            self.set("ISCR", self.get("ISCR") & ~0xE)
            self._check()
        return new & ~8

    def _etbrl_write(self, blk, reg, old, new):
        if new & 3:
            self._warn_once("etbrl", "MCM_ETBRL: reload value not 0 mod 4 (bus error on silicon): ignored")
            return old
        return None


# ================================================================================================== FMC
@register("kinetis.fmc")
class FMC(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {
            # RWSC (31:28) and BxMW (18:17) read-only; CINV_WAY (23:20) / S_B_INV (19) write-only, read 0
            "PFB0CR": {"ro_mask": 0xF0070000 | 0xFF00, "on_write": self._pfb0_write},
            "PFB1CR": {"ro_mask": 0xFFFFFFE0 & ~0x1F},
        }
        for w in range(4):
            for s in range(4):
                ov[f"TAGVDW{w}S{s}"] = {"ro_mask": 0xFFFFFFFF & ~(0x000FFFC0 | 1)}
        r = build_regs((name, "FMC"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)

    def _pfb0_write(self, blk, reg, old, new):
        ways = (new >> 20) & 0xF
        for w in range(4):
            if ways >> w & 1:
                for s in range(4):
                    self.set(f"TAGVDW{w}S{s}", 0)
                    for k in ("UM", "MU", "ML", "LM"):
                        n = f"DATAW{w}S{s}{k}"
                        if n in self.regs:
                            self.set(n, 0)
        return new & ~(0xF << 20) & ~(1 << 19)


# ================================================================================================== AIPS
@register("kinetis.aips")
class AIPS(_WarnOnce, MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        r = build_regs((name, "AIPS0"), size, None, params.get("regs_file"))
        for reg in r:
            reg.on_write = self._write_hook
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned: set[str] = set()

    def reset(self) -> None:
        self._warned = set()
        super().reset()

    def _write_hook(self, blk, reg, old, new):
        if reg.name.startswith("PACR"):
            prot = new & 0x77777777                    # SP/WP/TP of the 8 slots
            if prot:
                self._warn_once(reg.name, f"{self.name}_{reg.name} = 0x{new:08X}: peripheral access protection "
                                f"(SP/WP/TP) is not enforced; the K22 RM documents no AIPS-Lite programming model")
        elif new != old:
            self._warn_once(reg.name, f"{self.name}_{reg.name} = 0x{new:08X}: master trust/privilege settings are "
                            f"stored but not enforced")
        return None


# ================================================================================================== AXBS
AXBS_MASTERS = (0, 1, 2, 4, 5)         # PLAMC = 0x37


@register("kinetis.axbs")
class AXBS(_WarnOnce, MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {}
        for n in range(8):
            ov[f"PRS{n}"] = {"on_write": self._prs_write(n)}
            ov[f"CRS{n}"] = {"on_write": self._crs_write(n)}
        r = build_regs((name, "AXBS"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned: set[str] = set()

    def reset(self) -> None:
        self._warned = set()
        super().reset()

    def _locked(self, n: int) -> bool:
        return bool(self.get(f"CRS{n}") >> 31 & 1) if f"CRS{n}" in self.regs else False

    def _prs_write(self, n: int):
        def wr(blk, reg, old, new):
            if self._locked(n):
                self._warn_once(f"ro{n}", f"AXBS_PRS{n} written while CRS{n}.RO is set (bus error on silicon): "
                                f"ignored")
                return old
            levels = [(new >> (4 * m)) & 7 for m in AXBS_MASTERS]
            if len(set(levels)) != len(levels) or max(levels) >= len(AXBS_MASTERS):
                self._warn_once(f"prs{n}", f"AXBS_PRS{n} = 0x{new:08X}: duplicate or invalid master priority "
                                f"(bus error on silicon): not updated")
                return old
            return None
        return wr

    def _crs_write(self, n: int):
        def wr(blk, reg, old, new):
            if old >> 31 & 1:
                self._warn_once(f"ro{n}", f"AXBS_CRS{n} written while RO is set (bus error on silicon): ignored")
                return old
            return None
        return wr


# ================================================================================================== MPU
NRGD = 12
M1_FIELDS = 0x7 << 6 | 0x3 << 9              # M1UM (8:6), M1SM (10:9)


@register("kinetis.mpu")
class MPU(_WarnOnce, GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "MPU"                         # SIM_SCGC7[MPU] (set out of reset)
        ov: dict[str, dict] = {"CESR": {"w1c_mask": 0xF8000000, "on_write": self._cesr_write}}
        for n in range(NRGD):
            rst1 = 0xFFFFFFFF if n == 0 else 0x1F
            rst2 = 0x0061F7DF if n == 0 else 0
            rst3 = 1 if n == 0 else 0
            ov[f"RGD{n}_WORD0"] = {"ro_mask": 0x1F, "on_write": self._word_write(n, 0)}
            ov[f"RGD{n}_WORD1"] = {"reset": rst1, "ro_mask": 0x1F, "on_write": self._word_write(n, 1)}
            ov[f"RGD{n}_WORD2"] = {"reset": rst2, "on_write": self._word_write(n, 2)}
            ov[f"RGD{n}_WORD3"] = {"reset": rst3, "ro_mask": 0x0000FFFE, "on_write": self._word_write(n, 3)}
            ov[f"RGDAAC{n}"] = {"reset": rst2, "on_read": self._aac_read(n), "on_write": self._aac_write(n),
                               "on_peek": self._aac_read(n)}
        r = build_regs((name, "MPU"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned: set[str] = set()

    def reset(self) -> None:
        self._warned = set()
        super().reset()

    # -------------------------------------------------------------- descriptors
    def region(self, n: int) -> dict:
        w = [self.get(f"RGD{n}_WORD{k}") for k in range(4)]
        return {"start": w[0] & ~0x1F, "end": w[1] | 0x1F, "word2": w[2], "valid": bool(w[3] & 1)}

    @staticmethod
    def _m0_sup(word2: int) -> str:
        """Core (master 0) supervisor rights 'rwx' from M0SM (4:3) and M0UM (2:0) (19.3.6: SM 00 = r/w/x,
        01 = r-x, 10 = rw-, 11 = same as user)."""
        sm, um = (word2 >> 3) & 3, word2 & 7
        if sm == 0:
            return "rwx"
        if sm == 1:
            return "r-x"
        if sm == 2:
            return "rw-"
        return ("r" if um & 4 else "-") + ("w" if um & 2 else "-") + ("x" if um & 1 else "-")

    def _restricts_core(self) -> bool:
        """Does the enabled region set deny the core (supervisor) anything anywhere?"""
        if not self.get("CESR") & 1:
            return False
        full = [self.region(n) for n in range(NRGD)]
        for rg in full:
            if rg["valid"] and rg["start"] == 0 and rg["end"] == 0xFFFFFFFF and self._m0_sup(rg["word2"]) == "rwx":
                return False
        return True

    def _recheck(self) -> None:
        if self._restricts_core():
            regs = ", ".join(f"RGD{n} 0x{rg['start']:08X}-0x{rg['end']:08X} core {self._m0_sup(rg['word2'])}"
                             for n, rg in ((n, self.region(n)) for n in range(NRGD)) if rg["valid"])
            if "restrict" not in self._warned:
                self._warn_once("restrict", "MPU: regions now restrict the core, but access checks are not "
                                "enforced (no bus fault, EARn/EDRn/SPERR not set)")
                self.emit("info", block=self.name, msg=f"MPU regions: {regs or 'none valid'}")

    # -------------------------------------------------------------- hooks
    def _cesr_write(self, blk, reg, old, new):
        self._recheck()
        return None

    def _word_write(self, n: int, k: int):
        def wr(blk, reg, old, new):
            if n == 0 and k in (0, 1, 3):
                return old                                # RGD0 start/end/valid: not writable by the core
            if n == 0 and k == 2:
                new = (new & ~M1_FIELDS) | (old & M1_FIELDS)
            if k in (0, 1, 2):
                self.set(f"RGD{n}_WORD3", self.get(f"RGD{n}_WORD3") & ~1)   # writes clear VLD
            self._store(reg, new)
            self._recheck()
            return new
        return wr

    def _aac_read(self, n: int):
        def rd(blk, reg):
            return self.get(f"RGD{n}_WORD2")
        return rd

    def _aac_write(self, n: int):
        def wr(blk, reg, old, new):
            w2 = self.get(f"RGD{n}_WORD2")
            if n == 0:
                new = (new & ~M1_FIELDS) | (w2 & M1_FIELDS)
            self.set(f"RGD{n}_WORD2", new)
            self._recheck()
            return new
        return wr
