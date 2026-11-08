"""C28x Peripheral Interrupt Expansion (PIE) block: registers at 0x0CE0 and the vector table RAM at 0x0D00.

Register map (16-bit units, offsets from 0x0CE0; TMS320x2833x System Control and Interrupts guide, SPRUFB0, ch. 6):
  +0x00 PIECTRL  bit 0 ENPIE (rw); bits 15:1 PIEVECT (ro): address bits 15:1 of the last vector fetched from the PIE
  +0x01 PIEACK   bits 11:0 PIEACKx: set by the PIE when it forwards group x to the CPU (blocks further INTx);
                 writing 1 clears a bit (writing 0 has no effect)
  +0x02 + 2(x-1) PIEIERx  bits 7:0 enables of INTx.1..INTx.8
  +0x03 + 2(x-1) PIEIFRx  bits 7:0 flags of INTx.1..INTx.8 (latched on a rising edge of the peripheral line;
                 software may set/clear them)
  +0x20..+0x11F  PIE vector table: 128 vectors × 2 words (22-bit address, low word at the lower address); vector n
                 lives at 0x0D00 + 2n.  0-31 mirror the CPU vectors (RESET, INT1-14, DLOGINT, RTOSINT, EMUINT, NMI,
                 ILLEGAL, USER1-12), INTx.y is n = 32 + 8(x-1) + (y-1) (INT1.1 = 0x0D40).
                 EALLOW-protected (SPRUFB0 6.3.4): a CPU write with ST1.EALLOW clear is ignored (one `warn`); the
                 firmware loader and debugger writes (core not running) are not CPU writes and always land.

The block only stores state; the group/ACK/IFR propagation lives in `C28xArch` (arch.py), which this block calls
on every write that may change what the CPU sees.
"""
from __future__ import annotations

from typing import Any

from ...periph.base import MMIOBlock, Reg

PIE_BASE = 0x0CE0
PIE_SIZE = 0x120                 # registers (0x20) + vector table (0x100)
VEC_OFF = 0x20                   # offset of the vector table inside the block
ST1_EALLOW = 1 << 6


class PIEBlock(MMIOBlock):
    model = "ti_c2000.pie"
    unit_bits = 16

    def __init__(self, arch: Any, name: str = "PIE", base: int = PIE_BASE, size: int = PIE_SIZE):
        self.arch = arch
        regs = [Reg("PIECTRL", 0x00, 1, reset=0, ro_mask=0xFFFE, on_write=self._ctrl_write, unit_bits=16),
                Reg("PIEACK", 0x01, 1, reset=0, on_write=self._ack_write, unit_bits=16)]
        for g in range(1, 13):
            regs.append(Reg(f"PIEIER{g}", 2 * g, 1, on_write=self._ier_write, unit_bits=16))
            regs.append(Reg(f"PIEIFR{g}", 2 * g + 1, 1, on_write=self._ifr_write, unit_bits=16))
        super().__init__(name, base, size, regs, unit_bits=16)
        self.log_unknown = False
        # live state mirrored in plain ints (the arch reads these on every service)
        self.enpie = False
        self.ack = 0
        self.ier = [0] * 13          # index 1..12
        self.ifr = [0] * 13
        self.enforce_eallow = True
        self._eallow_warned = False

    def reset(self) -> None:
        vt = bytes(self.mem[VEC_OFF * 2:])           # the vector RAM is not initialised by a reset
        super().reset()
        self.mem[VEC_OFF * 2:] = vt
        self.enpie = False
        self.ack = 0
        self.ier = [0] * 13
        self.ifr = [0] * 13

    # ------------------------------------------------------------------ vector table (plain RAM, fast path)
    def read(self, offset: int, size: int) -> int:
        if offset >= VEC_OFF and not self.log_all:
            o = offset * 2
            return int.from_bytes(self.mem[o:o + 2 * size], "little")
        return MMIOBlock.read(self, offset, size)

    def _cpu_write_blocked(self) -> bool:
        """A CPU store into the vector table without EALLOW (ignored on silicon)."""
        core = self.arch.core
        if not self.enforce_eallow or not getattr(core, "running", False):
            return False
        try:
            if core.read_reg("st1") & ST1_EALLOW:
                return False
        except Exception:
            return False
        if not self._eallow_warned:
            self._eallow_warned = True
            self.emit("warn", block=self.name, msg=f"{self.name} vector table: write ignored, the table is "
                      "EALLOW-protected and EALLOW is not set")
        return True

    def write(self, offset: int, size: int, value: int) -> None:
        if offset >= VEC_OFF:
            if self._cpu_write_blocked():
                return
            if self.log_all:
                MMIOBlock.write(self, offset, size, value)
                return
            o = offset * 2
            self.mem[o:o + 2 * size] = (value & ((1 << (16 * size)) - 1)).to_bytes(2 * size, "little")
            return
        MMIOBlock.write(self, offset, size, value)

    def vector(self, n: int) -> int:
        """22-bit target address stored for vector n (0..127)."""
        o = (VEC_OFF + 2 * n) * 2
        return int.from_bytes(self.mem[o:o + 4], "little") & 0x3FFFFF

    def set_vector(self, n: int, addr: int) -> None:
        o = (VEC_OFF + 2 * n) * 2
        self.mem[o:o + 4] = (addr & 0x3FFFFF).to_bytes(4, "little")

    # ------------------------------------------------------------------ state helpers used by the arch
    def set_flag(self, group: int, channel: int) -> None:
        """Latch PIEIFRgroup bit (channel 1..8)."""
        self.ifr[group] |= 1 << (channel - 1)
        self._store(self.regs[f"PIEIFR{group}"], self.ifr[group])

    def clear_flag(self, group: int, channel: int) -> None:
        self.ifr[group] &= ~(1 << (channel - 1))
        self._store(self.regs[f"PIEIFR{group}"], self.ifr[group])

    def set_ack(self, group: int) -> None:
        self.ack |= 1 << (group - 1)
        self._store(self.regs["PIEACK"], self.ack)

    def clear_ack(self, group: int) -> None:
        self.ack &= ~(1 << (group - 1))
        self._store(self.regs["PIEACK"], self.ack)

    def set_pievect(self, vec_addr: int) -> None:
        r = self.regs["PIECTRL"]
        self._store(r, (vec_addr & 0xFFFE) | int(self.enpie))

    # ------------------------------------------------------------------ register hooks
    def _ctrl_write(self, blk, reg, old, new):
        self.enpie = bool(new & 1)
        return None

    def _ack_write(self, blk, reg, old, new):
        written = new & 0x0FFF
        # w1c: a 1 clears the acknowledge bit; zeros leave it unchanged
        self.ack = old & 0x0FFF & ~written
        for g in range(1, 13):
            if written >> (g - 1) & 1:
                self.arch.pie_group_changed(g)
        return self.ack

    def _ier_write(self, blk, reg, old, new):
        g = (reg.offset - 2) // 2 + 1
        self.ier[g] = new & 0xFF
        self.arch.pie_group_changed(g)
        return new & 0xFF

    def _ifr_write(self, blk, reg, old, new):
        g = (reg.offset - 3) // 2 + 1
        self.ifr[g] = new & 0xFF
        self.arch.pie_group_changed(g)
        return new & 0xFF
