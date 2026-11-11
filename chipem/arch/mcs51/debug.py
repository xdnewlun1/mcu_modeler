"""Debugger support for MCS-51 chips (used by chipem/debug.py through `arch.debug_support()`): register list,
instruction decode (call / return / target), call-stack walk over the 8-bit stack, return address for `finish`."""
from __future__ import annotations

from typing import Any

from ...core.mcs51.asm import disasm_one
from ...core.mcs51.isa import OPCODES, operands

CALLS = {0x12} | {p << 5 | 0x11 for p in range(8)}         # LCALL, ACALL
RETS = {0x22, 0x32}                                          # RET, RETI


class MCS51DebugSupport:
    code_mask = 0xFFFF
    stack_grows_up = True                # PUSH/LCALL pre-increment SP

    def __init__(self, arch: Any):
        self.arch = arch

    @property
    def core(self):
        return self.arch.chip.core

    def reg_names(self) -> list[str]:
        return ["pc", "a", "b", "psw", "sp", "dptr", "dptr0", "dptr1", "dps", "r0", "r1", "r2", "r3", "r4", "r5",
                "r6", "r7", "cy", "ac", "ov", "rs", "f0"]

    def decode(self, addr: int) -> dict | None:
        code = self.core.code
        a = addr & 0xFFFF
        size, text = disasm_one(lambda x: code[x & 0xFFFF], a, self.arch.names())
        op = code[a]
        e = OPCODES[op]
        target = None
        if e is not None and op in CALLS:
            raw = [code[(a + i) & 0xFFFF] for i in range(e.size)]
            target = operands(e, a, raw)[0]
        return {"addr": a, "size": size, "bytes": " ".join(f"{code[(a + i) & 0xFFFF]:02x}" for i in range(size)),
                "text": text, "call": op in CALLS, "target": target, "ret": op in RETS}

    def backtrace(self, max_frames: int = 32) -> list[tuple[int, int, str]]:
        """[(pc, sp, how)]: the current PC, then every return address found on the stack (pushed 16-bit values just
        after an LCALL/ACALL, or an interrupted PC when an interrupt level is active)."""
        core = self.core
        sp = core.sfr[0x81]
        out = [(core.pc, sp, "pc")]
        for s, ret in self.arch.backtrace(max_frames - 1):
            out.append((ret, s, "scan"))
        return out[:max_frames]

    def return_address(self) -> int | None:
        bt = self.arch.backtrace(1)
        return bt[0][1] if bt else None
