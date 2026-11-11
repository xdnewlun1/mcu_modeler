"""Differential testing of the MCS-51 core against ucsim (`s51`, shipped with SDCC): the same code runs in both and
the architectural state (PC, IRAM, ACC/B/PSW/SP/DPTR/P1/P2, touched XDATA bytes) and the oscillator-clock count are
compared.

ucsim is an independent 8051 implementation (Daniel Drotos, GPL).  Distributions name its binary `s51`
(upstream), `sdcc-s51` (Fedora) or `ucsim_51`; `find_s51()` looks for all of them.  One s51 process runs a whole
batch of cases through its command console: every case sets memory, sets PC, steps N instructions and dumps the
state; echo markers (`@@...`) split the output.  ucsim echoes the command lines it reads, possibly ahead of their
output, so only marker lines (`@@x`, never equal to a command line) and hex dump lines are parsed.

The model side mirrors every memory write on one persistent bare `MCS51Core` (ROM, IRAM, XDATA, the core SFRs and
P1/P2 as plain registers), so both simulators see the same history.

Used by tests/test_mcs51_ucsim.py (fast subset) and tools/mcs51_ucsim_diff.py (exhaustive / randomised runs).
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from chipem.core.mcs51 import MCS51Core
from chipem.core.mcs51.isa import OPCODES, OPERAND_BYTES

S51_NAMES = ("s51", "sdcc-s51", "ucsim_51", "sdcc-ucsim_51")
ACC, B, PSW, SP, DPL, DPH, P1, P2 = 0xE0, 0xF0, 0xD0, 0x81, 0x82, 0x83, 0x90, 0xA0
CORE_SFRS = (ACC, B, PSW, SP, DPL, DPH, P1, P2)
SFR_NAMES = {ACC: "ACC", B: "B", PSW: "PSW", SP: "SP", DPL: "DPL", DPH: "DPH", P1: "P1", P2: "P2"}
# direct addresses used for operands: internal RAM and the SFRs that are plain registers in both simulators
DIRECTS = list(range(0x80)) + list(CORE_SFRS)
BITS = list(range(0x80)) + [s + i for s in (ACC, B, PSW, P1, P2) for i in range(8)]
EDGE = (0x00, 0x01, 0x0F, 0x10, 0x7F, 0x80, 0x99, 0xF0, 0xFE, 0xFF)


def find_s51() -> str | None:
    for n in S51_NAMES:
        p = shutil.which(n)
        if p:
            return p
    return None


@dataclass
class Case:
    pc: int
    code: dict[int, bytes]                              # address -> bytes written to ROM before the run
    iram: bytes                                         # all 256 bytes
    sfr: dict[int, int]                                 # core SFR values
    xdata: dict[int, int] = field(default_factory=dict)  # XDATA bytes set (and compared) for MOVX
    steps: int = 1
    note: str = ""


@dataclass
class State:
    pc: int
    iram: bytes
    sfr: dict[int, int]
    xdata: dict[int, int]
    ticks: int

    def diff(self, other: "State", *, parity: bool = True) -> list[str]:
        out = []
        if self.pc != other.pc:
            out.append(f"PC {self.pc:04X} != {other.pc:04X}")
        for s in CORE_SFRS:
            a, b = self.sfr.get(s), other.sfr.get(s)
            if s == PSW and not parity and a is not None and b is not None:
                a, b = a & 0xFE, b & 0xFE
            if a != b:
                out.append(f"{SFR_NAMES[s]} {a:02X} != {b:02X}")
        for i in range(256):
            if self.iram[i] != other.iram[i]:
                out.append(f"IRAM[{i:02X}] {self.iram[i]:02X} != {other.iram[i]:02X}")
        for a in sorted(set(self.xdata) | set(other.xdata)):
            if self.xdata.get(a) != other.xdata.get(a):
                out.append(f"XDATA[{a:04X}] {self.xdata.get(a)} != {other.xdata.get(a)}")
        if self.ticks != other.ticks:
            out.append(f"ticks {self.ticks} != {other.ticks}")
        return out


def _hexbytes(bs) -> str:
    return " ".join(f"0x{b:02x}" for b in bs)


class S51Batch:
    """Run a list of cases in one s51 process."""

    def __init__(self, exe: str | None = None, cpu: str = "8052"):
        self.exe = exe or find_s51()
        if self.exe is None:
            raise FileNotFoundError("ucsim s51 not found (" + ", ".join(S51_NAMES) + ")")
        self.cpu = cpu

    def case_commands(self, c: Case) -> list[str]:
        out = ["fill rom 0 0xffff 0xff", "fill xram 0 0xffff 0"]          # every case starts from erased memory
        for k in range(0, 256, 64):
            out.append(f"set memory iram 0x{k:02x} {_hexbytes(c.iram[k:k + 64])}")
        for s, v in c.sfr.items():
            if s != PSW:
                out.append(f"set memory sfr 0x{s:02x} 0x{v:02x}")
        if PSW in c.sfr:
            # ucsim updates P only when ACC is written: give it the parity of the ACC set here
            psw = (c.sfr[PSW] & 0xFE) | (bin(c.sfr.get(ACC, 0)).count("1") & 1)
            out.append(f"set memory sfr 0x{PSW:02x} 0x{psw:02x}")
        for a, v in c.xdata.items():
            out.append(f"set memory xram 0x{a:04x} 0x{v:02x}")
        for a, bs in c.code.items():
            for k in range(0, len(bs), 64):
                out.append(f"set memory rom 0x{a + k:04x} {_hexbytes(bs[k:k + 64])}")
        out.append(f"pc 0x{c.pc:04x}")
        return out

    def run(self, cases: list[Case], timeout: float = 600) -> list[State]:
        """ucsim's console answers within microseconds for its first second or two and then settles into ~0.1 s
        per command (an idle poll interval), so the session is restarted every `RESTART_S` seconds; the model side
        mirrors that (ModelBatch only depends on the memory the cases set, see Case)."""
        import time
        out: list[State] = []
        ses = None
        t0 = 0.0
        try:
            for c in cases:
                if ses is None or time.monotonic() - t0 > self.RESTART_S:
                    if ses is not None:
                        ses.close()
                    ses = S51Session(self.exe, self.cpu, timeout=timeout)
                    t0 = time.monotonic()
                ses.cmds(self.case_commands(c))
                out.append(ses.step_state(c.steps, list(c.xdata)))
        finally:
            if ses is not None:
                ses.close()
        return out

    RESTART_S = 1.0


class S51Session:
    """An interactive s51 on a pseudo-terminal with a NUL prompt (`-P`): one command, one complete answer."""

    _dump = re.compile(r"^0x([0-9a-f]+)\s+((?:[0-9a-f]{2}(?: |$))+)")
    _ticks = re.compile(r"stepped (\d+) ticks")
    _pc = re.compile(r"^0x([0-9a-f]{4,6})\s+\S?\?")

    def __init__(self, exe: str | None = None, cpu: str = "8052", *, timeout: float = 600, args: tuple = ()):
        import pty
        import tty
        self.exe = exe or find_s51()
        if self.exe is None:
            raise FileNotFoundError("ucsim s51 not found (" + ", ".join(S51_NAMES) + ")")
        m, s = pty.openpty()
        tty.setraw(s)
        self.proc = subprocess.Popen([self.exe, "-q", "-P", "-t", cpu, *args], stdin=s, stdout=s, stderr=s,
                                     close_fds=True, env={**os.environ, "TERM": "dumb"})
        os.close(s)
        self.fd = m
        self.timeout = timeout
        self._read(1)
        # an SP wrap (PUSH at SP = FFh) is an ucsim "stack_overflow" error that stops the simulation; the 8051's SP
        # is an 8-bit register that just wraps, so the error class is switched off
        self.cmd("set error stack off")

    def _read(self, n: int = 1) -> list[str]:
        """Read the answers of the next `n` commands (each ends with the NUL prompt)."""
        import select
        buf = bytearray()
        while buf.count(0) < n:
            r, _w, _x = select.select([self.fd], [], [], self.timeout)
            if not r:
                raise TimeoutError("s51 did not answer")
            try:
                c = os.read(self.fd, 1 << 16)
            except OSError:
                c = b""
            if not c:
                raise EOFError("s51 exited")
            buf += c
        return [x.decode("latin-1") for x in bytes(buf).split(b"\0")[:n]]

    def cmd(self, line: str) -> str:
        return self.cmds([line])[0]

    def cmds(self, lines: list[str]) -> list[str]:
        """Send several commands at once (ucsim answers them in order; pipelining avoids its console latency) and
        return their answers.  Written in chunks so neither side's pty buffer fills up."""
        out: list[str] = []
        for k in range(0, len(lines), 16):
            part = lines[k:k + 16]
            os.write(self.fd, "".join(x + "\n" for x in part).encode())
            out += self._read(len(part))
        return out

    def dump(self, space: str, lo: int, hi: int) -> bytes:
        return self._parse_dump(self.cmd(f"dump /h {space} 0x{lo:x} 0x{hi:x} 16"), lo, hi)

    def _parse_dump(self, text: str, lo: int, hi: int) -> bytes:
        out = bytearray()
        for line in text.splitlines():
            m = self._dump.match(line.rstrip())
            if m and int(m.group(1), 16) == lo + len(out):
                out += bytes(int(x, 16) for x in m.group(2).split()[:16])
        if len(out) < hi - lo + 1:
            raise ValueError(f"short s51 dump of 0x{lo:x}..0x{hi:x}: {text!r}")
        return bytes(out[:hi - lo + 1])

    def pc(self) -> int:
        for line in self.cmd("pc").splitlines():
            m = self._pc.match(line)
            if m:
                return int(m.group(1), 16)
        raise ValueError("no PC in s51 answer")

    def step_state(self, steps: int, xaddrs=()) -> State:
        xaddrs = list(xaddrs)
        ans = self.cmds([f"step {steps}", "pc", "dump /h iram 0x0 0xff 16", "dump /h sfr 0x80 0xff 16"]
                        + [f"dump /h xram 0x{a:x} 0x{a:x} 16" for a in xaddrs])
        ticks = sum(int(m.group(1)) for m in self._ticks.finditer(ans[0]))
        pc = next(int(m.group(1), 16) for m in map(self._pc.match, ans[1].splitlines()) if m)
        iram = self._parse_dump(ans[2], 0, 0xFF)
        sfrs = self._parse_dump(ans[3], 0x80, 0xFF)
        sfr = {s: sfrs[s - 0x80] for s in CORE_SFRS}
        xd = {a: self._parse_dump(t, a, a)[0] for a, t in zip(xaddrs, ans[4:])}
        return State(pc, iram, sfr, xd, ticks)

    def close(self) -> None:
        try:
            os.write(self.fd, b"kill\n")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ModelBatch:
    """The same cases on a bare MCS51Core: ROM erased (FFh), XDATA zero, as the s51 side sets them per case."""

    @staticmethod
    def fresh() -> MCS51Core:
        c = MCS51Core()
        c.map_memory(0x00000, 0x10000, writable=True)
        c.map_memory(0x20000, 0x100)
        c.map_memory(0x10000, 0x10000)                   # 8052: MOVX reaches 64 KB external RAM
        c.spin_skip = False
        c.reset(0)
        return c

    def run(self, cases: list[Case]) -> list[State]:
        out = []
        for cs in cases:
            c = self.fresh()
            c.iram[:] = cs.iram
            for s, v in cs.sfr.items():
                c.sfr[s] = v & (0xFE if s == PSW else 0xFF)
            for a, v in cs.xdata.items():
                c.xdata[a] = v
            for a, bs in cs.code.items():
                c.write_mem(a, bs)
            c.pc = cs.pc
            ticks = 0
            for _ in range(cs.steps):
                _r, n = c.run(1)
                ticks += n
            sfr = {s: (c.psw() if s == PSW else c.sfr[s]) for s in CORE_SFRS}
            out.append(State(c.pc, bytes(c.iram), sfr, {a: c.xdata[a] for a in cs.xdata}, ticks))
        return out


# ucsim 0.8.15 deviations from the Intel MCS-51 definition, found by this harness and avoided by the generators
# (each is checked against our core's Intel behaviour in tests/test_mcs51_ucsim.py):
#  * PSW.P: ucsim recomputes the parity flag only when ACC is written, so a write to PSW (MOV/POP/XCH/INC PSW ...)
#    stores the written P bit; on the 8051 P always shows the parity of ACC.  -> P is not compared after the
#    instruction (`parity=False`), random programs do not read or write PSW as a byte.
#  * Register-bank switch through bit instructions: SETB/CLR/CPL/MOV RS0/RS1 change PSW but ucsim keeps using the
#    old bank for R0-R7 until PSW is written as a byte.  -> random programs do not touch bits D3h/D4h.
#  * MOVC A,@A+PC / @A+DPTR whose sum passes FFFFh: ucsim reads 00h beyond the 64 KB ROM; the 8051's 16-bit address
#    adder wraps to 0000h.  -> generators keep the sum below 10000h.
#  * PUSH SP: ucsim pushes the old SP value; Intel defines PUSH as "(SP) <- (SP)+1; ((SP)) <- (direct)", so the
#    incremented SP is stored (our core).  Not verified on silicon.  -> generators do not PUSH SP.
#  * An SP wrap (PUSH at SP = FFh) is an ucsim "stack_overflow" error that stops the simulation (switched off).
UCSIM_DEVIATIONS = ("psw_parity_write", "bank_switch_by_bit", "movc_wrap", "push_sp", "stack_overflow_stop")


# ------------------------------------------------------------------------------------------------ case generators
def _operand_bytes(e, rnd: random.Random, val: int):
    """Encoded operand bytes (byte order) and the (kind, address) of the data operand that holds `val`."""
    by_idx: dict[int, list[int]] = {}
    target = None
    for i, k in enumerate(e.ops):
        if k == "dir":
            a = rnd.choice(DIRECTS)
            by_idx[i] = [a]
            if target is None:
                target = ("dir", a)
        elif k in ("bit", "/bit"):
            by_idx[i] = [rnd.choice(BITS)]
        elif k == "#imm":
            by_idx[i] = [val if target is None or rnd.random() < 0.5 else rnd.choice(EDGE)]
        elif k == "#imm16":
            by_idx[i] = [rnd.randrange(256), rnd.randrange(256)]
        elif k == "rel":
            by_idx[i] = [rnd.choice((0x00, 0x01, 0x7F, 0x80, 0xFE, 0xFF, rnd.randrange(256)))]
        elif k in ("a11", "a16"):
            by_idx[i] = [rnd.randrange(256) for _ in range(1 if k == "a11" else 2)]
    raw = []
    for i in e.order:
        raw += by_idx.get(i, [])
    return raw, target


def random_state(rnd: random.Random):
    iram = bytearray(rnd.randrange(256) for _ in range(256))
    sfr = {s: rnd.randrange(256) for s in CORE_SFRS}
    return iram, sfr


def opcode_case(op: int, rnd: random.Random, a: int, v: int, cy: int, ac: int, pc: int | None = None) -> Case:
    """One instruction `op` with ACC = a, its data operand = v, CY/AC given, the rest random."""
    e = OPCODES[op]
    iram, sfr = random_state(rnd)
    raw, target = _operand_bytes(e, rnd, v)
    if op == 0xC0 and raw[0] == SP:                       # PUSH SP: ucsim deviation (see UCSIM_DEVIATIONS)
        raw[0] = rnd.randrange(0x80)
        target = ("dir", raw[0])
    if pc is None:
        pc = rnd.choice((0x0100, 0x07FD, 0x0FFE, 0x1234, 0x7FFE, 0xF000, 0xFFFE - e.size))
        if op == 0x83:                                     # MOVC @A+PC: no 16-bit wrap (ucsim deviation)
            pc = rnd.choice((0x0100, 0x07FD, 0x1234, 0x7FFE, 0xEF00))
    sfr[ACC] = a
    sfr[PSW] = (sfr[PSW] & 0x3E) | (cy << 7) | (ac << 6)
    bank = sfr[PSW] & 0x18
    xdata = {}
    for k in e.ops:
        if k in ("@R0", "@R1"):
            ptr = rnd.choice((rnd.randrange(0x30, 0x80), rnd.randrange(0x80, 0x100)))
            iram[bank | int(k[2])] = ptr
            if target is None:
                target = ("ind", ptr)
        elif len(k) == 2 and k[0] == "R" and target is None:
            target = ("ind", bank | int(k[1]))
    if target is not None:
        kind, addr = target
        if kind == "dir" and addr >= 0x80:
            if addr == PSW:
                sfr[PSW] = (v & 0xFE)
            elif addr != ACC:
                sfr[addr] = v
        else:
            iram[addr] = v
    if op == 0x93 and ((sfr[DPH] << 8) | sfr[DPL]) + sfr[ACC] > 0xFFFF:
        sfr[DPH] &= 0x7F
    if op in (0xE0, 0xF0):
        xdata[(sfr[DPH] << 8) | sfr[DPL]] = rnd.randrange(256)
    if op in (0xE2, 0xE3, 0xF2, 0xF3):
        xdata[(sfr[P2] << 8) | iram[bank | (op & 1)]] = rnd.randrange(256)
    code = {pc: bytes([op] + raw)}
    if op == 0x93:
        src = (sfr[ACC] + ((sfr[DPH] << 8) | sfr[DPL])) & 0xFFFF
        if not (pc <= src < pc + e.size):
            code[src] = bytes([rnd.randrange(256)])
    if op == 0x83:
        src = (sfr[ACC] + pc + e.size) & 0xFFFF
        if not (pc <= src < pc + e.size):
            code[src] = bytes([rnd.randrange(256)])
    return Case(pc, code, bytes(iram), sfr, xdata, 1, f"{op:02X} {e.mnem} {' '.join(e.ops)} A={a:02X} v={v:02X} "
                                                     f"CY={cy} AC={ac}")


def opcode_cases(rnd: random.Random, *, full: bool = False, per_op: int = 8) -> list[Case]:
    """Every opcode (A5 excluded) with boundary operands.  full: A x operand x CY x AC over EDGE values (about 400
    per opcode); otherwise `per_op` random picks from that grid plus the all-zero / all-one corners."""
    out = []
    for op in range(256):
        if OPCODES[op] is None:
            continue
        grid = [(a, v, cy, ac) for a in EDGE for v in EDGE for cy in (0, 1) for ac in (0, 1)]
        picks = grid if full else [(0, 0, 0, 0), (0xFF, 0xFF, 1, 1)] + rnd.sample(grid, per_op)
        for a, v, cy, ac in picks:
            out.append(opcode_case(op, rnd, a, v, cy, ac))
    return out


_RANDOM_SKIP = {0x22, 0x32, 0x73, 0x93}          # RET/RETI/JMP @A+DPTR (data-dependent targets), MOVC @A+DPTR (wrap)


def random_program_case(rnd: random.Random, n: int = 40, pc: int = 0x0200) -> Case:
    """A random instruction sequence of `n` instructions run for `n` steps.  Conditional jumps go forward to a later
    instruction boundary of the block (taken or not, execution stays on generated instructions; the tail is NOPs);
    no absolute jumps/calls.  Operands avoid the known ucsim deviations (UCSIM_DEVIATIONS)."""
    insns: list[list[int]] = []
    while len(insns) < n:
        op = rnd.randrange(256)
        e = OPCODES[op]
        if e is None or op in _RANDOM_SKIP or e.mnem in ("AJMP", "LJMP", "ACALL", "LCALL"):
            continue
        raw, _t = _operand_bytes(e, rnd, rnd.choice(EDGE))
        pos = 0
        bad = False
        for i in e.order:
            k = e.ops[i]
            w = OPERAND_BYTES.get(k, 0)
            if k == "dir" and raw[pos] == PSW:
                bad = True
            if k in ("bit", "/bit") and raw[pos] in (PSW, PSW + 3, PSW + 4):
                bad = True
            if op == 0xC0 and raw[pos] == SP:
                bad = True
            pos += w
        if bad:
            continue
        insns.append([op] + raw)
    starts, a = [], pc
    for ins in insns:
        starts.append(a)
        a += len(ins)
    end = a
    for k, ins in enumerate(insns):
        e = OPCODES[ins[0]]
        if "rel" in e.ops:                                  # rel is always the last byte
            npc = starts[k] + len(ins)
            choices = [t for t in starts[k + 1:k + 6] + [end] if 0 <= t - npc <= 127]
            ins[-1] = (rnd.choice(choices) - npc) & 0xFF
    body = bytes(b for ins in insns for b in ins)
    iram, sfr = random_state(rnd)
    sfr[SP] = rnd.choice((0x07, 0x2F, 0x60, 0xF0, 0xFD))
    xdata = {(sfr[DPH] << 8) | sfr[DPL]: rnd.randrange(256)}
    return Case(pc, {pc: body + bytes(4 * n)}, bytes(iram), sfr, xdata, n, f"random program of {n} at {pc:04X}")


def compare(cases: list[Case], *, exe: str | None = None, parity: bool = False) -> list[tuple[Case, list[str]]]:
    ref = S51Batch(exe).run(cases)
    mod = ModelBatch().run(cases)
    bad = []
    for c, r, m in zip(cases, ref, mod):
        d = m.diff(r, parity=parity)
        if d:
            bad.append((c, d))
    return bad


# ------------------------------------------------------------------------------------------------ whole programs
@dataclass
class ProgramResult:
    iram: bytes
    xdata: bytes                 # XDATA 0000h..(xdata_len-1)
    ticks: int                   # oscillator clocks from reset to the stop address
    pc: int


def run_program_model(image, stop: int, xdata_len: int = 0x300, max_ticks: int = 2_000_000_000) -> ProgramResult:
    """Run an image on a bare core (8052-like: 256 B IRAM, 64 KB external XDATA RAM) from reset to `stop`."""
    from chipem.core.base import StopReason
    c = ModelBatch.fresh()
    for a, d in image.segments:
        c.write_mem(a, d)
    c.reset(0)
    c.set_breakpoints([stop])
    ticks = 0
    while ticks < max_ticks:
        r, n = c.run(100_000)
        ticks += n
        if r is StopReason.DEBUG:
            break
        if r is StopReason.FAULT:
            raise RuntimeError(f"fault {c.last_fault}")
    return ProgramResult(bytes(c.iram), bytes(c.xdata[:xdata_len]), ticks, c.pc)


_clks = re.compile(r"\((\d+) clks\)")


def run_program_s51(path, stop: int, xdata_len: int = 0x300, exe: str | None = None,
                    timeout: float = 600) -> ProgramResult:
    """The same in ucsim: IRAM and XDATA cleared (ucsim fills them with random bytes, the model with zeros; SDCC's
    startup clears what it uses anyway), breakpoint at `stop`, run, read the clock count from `state`."""
    with S51Session(exe, args=(str(path),), timeout=timeout) as ses:
        ses.cmds(["fill iram 0 0xff 0", "fill xram 0 0xffff 0", "pc 0", f"break 0x{stop:x}"])
        ses.cmd("run")
        st = ses.cmd("state")
        m = _clks.search(st)
        ticks = int(m.group(1)) if m else -1
        iram = ses.dump("iram", 0, 0xFF)
        xd = ses.dump("xram", 0, xdata_len - 1) if xdata_len else b""
        return ProgramResult(iram, xd, ticks, ses.pc())
