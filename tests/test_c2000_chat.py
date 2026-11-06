"""boards/c2000_stm32_chat.yaml: TMS320F28232 (uart_chat.asm) <-> STM32F405ZG (uart_chat.c) two-way UART chat.

Protocol: HELLO at boot, PING <n> every 5 ms from each side, PONG <n> replies; PONG toggles the LED pin (GPIO34 /
PB0) and counts, HELLO drives the peer pin (GPIO35 / PB1) and the first HELLO received is answered once with an own
HELLO.  The board is wired with nets only; the two UART links
must be created automatically.  One 40 ms run (~0.5 s) is shared by all tests.

UART input is not queued (hardware behaviour): the F28232's SCI-A listens only once its firmware has relocked the PLL
and configured the SCI (~6.6 ms), so the STM32's boot HELLO (t ~ 0) and PING 1 (5 ms) are lost on the wire. The
F28232's own HELLO (~6.6 ms) is answered once by the STM32, and that answer (~8.8 ms) drives GPIO35 high."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "boards" / "c2000_stm32_chat.yaml"
C2K_IMG = ROOT / "firmware" / "samples_c2000" / "build" / "uart_chat.c28img"
STM_ELF = ROOT / "firmware" / "samples_stm32" / "build" / "uart_chat.elf"

pytestmark = pytest.mark.skipif(not (C2K_IMG.exists() and STM_ELF.exists()), reason="samples not built")

PERIOD_MS = 5.0
SCI_LISTENS_MS = 6.6           # F28232: SCI-A enabled after the PLL relock; earlier STM32 bytes are dropped
TOL = 0.05                     # timing tolerance: 5 % of the 5 ms period
C2K_VARS = 0x0100              # PINGN PONGS PEER TICK RXHEAD RXTAIL LINELEN PINGSRX HELLOANS (uart_chat.asm)


def _lines(evs, cid):
    """[(t_ms of the first byte, text)] of the complete lines chip `cid` transmitted."""
    out, cur, t0 = [], bytearray(), None
    for e in evs:
        f = e.fields
        if e.kind != "uart" or f.get("chip") != cid or f.get("dir") != "tx":
            continue
        if t0 is None:
            t0 = e.t_ns / 1e6
        cur.append(f["byte"])
        if f["byte"] == 0x0A:
            out.append((t0, cur.decode("ascii").rstrip("\r\n")))
            cur, t0 = bytearray(), None
    return out


def _toggles(evs, cid, pin):
    return [(e.t_ns / 1e6, e.fields["new"]) for e in evs if e.kind == "pin" and e.fields.get("chip") == cid
            and e.fields.get("pin") == pin and e.fields.get("field") == "level"]


def _elf_symbols(path):
    from elftools.elf.elffile import ELFFile
    with open(path, "rb") as f:
        st = ELFFile(f).get_section_by_name(".symtab")
        return {s.name: s["st_value"] for s in st.iter_symbols() if s.name}


@pytest.fixture(scope="module")
def run():
    from chipem.session import Session
    s = Session.from_board(BOARD, quiet=True)
    try:
        s.run_for(40e6)
        evs = s.events(kinds={"uart", "pin"})
        a, b = s.chips["A"], s.chips["B"]
        words = a.core.read_mem(C2K_VARS, 9)
        c2k = [int.from_bytes(words[2 * i:2 * i + 2], "little") for i in range(9)]
        syms = _elf_symbols(STM_ELF)
        stm = {n: int.from_bytes(b.core.read_mem(syms[n], 4), "little")
               for n in ("pongs", "pings_answered", "peer_seen", "ping_n", "hello_answered")}
        yield {
            "A": _lines(evs, "A"), "B": _lines(evs, "B"), "evs": evs, "c2k": c2k, "stm": stm,
            "links": [(ln.describe(), getattr(ln, "auto", False)) for ln in s.board.links],
            "levels": {"GPIO34": a.pins.get("GPIO34").level(), "GPIO35": a.pins.get("GPIO35").level(),
                       "PB0": b.pins.get("PB0").level(), "PB1": b.pins.get("PB1").level()},
        }
    finally:
        s.close()


def test_nets_create_two_auto_links(run):
    assert sorted(run["links"]) == [("A.SCIA -> B.USART1", True), ("B.USART1 -> A.SCIA", True)]


def test_hello_first_and_answered_once(run):
    assert run["A"][0][1] == "HELLO C2000"
    assert run["B"][0][1] == "HELLO STM32"
    # boot HELLO + one answer to the first HELLO received (no HELLO ping-pong)
    assert [ln for _, ln in run["A"] if ln.startswith("HELLO")] == ["HELLO C2000"] * 2
    assert [ln for _, ln in run["B"] if ln.startswith("HELLO")] == ["HELLO STM32"] * 2
    assert run["B"][0][0] < 0.1                       # STM32 boots at once on the 16 MHz HSI
    assert 6.4 < run["A"][0][0] < 6.8                 # F28232 after the ~6.6 ms PLL relock


@pytest.mark.parametrize("cid", ["A", "B"])
def test_pings_every_5ms(run, cid):
    pings = [(t, ln) for t, ln in run[cid] if ln.startswith("PING ")]
    assert [ln for _, ln in pings] == [f"PING {n}" for n in range(1, len(pings) + 1)]
    assert len(pings) == {"A": 6, "B": 7}[cid]        # A starts its timer after the PLL lock (first PING ~11.6 ms)
    for (t0, _), (t1, _) in zip(pings, pings[1:]):
        assert abs((t1 - t0) - PERIOD_MS) <= TOL * PERIOD_MS
    if cid == "B":
        for n, (t, _) in enumerate(pings, 1):
            assert abs(t - n * PERIOD_MS) <= TOL * PERIOD_MS
    else:
        hello_t = run["A"][0][0]
        assert abs(pings[0][0] - hello_t - PERIOD_MS) <= TOL * PERIOD_MS


@pytest.mark.parametrize("pinger,ponger", [("A", "B"), ("B", "A")])
def test_every_ping_answered(run, pinger, ponger):
    pings = [(t, ln.split()[1]) for t, ln in run[pinger] if ln.startswith("PING ")]
    if ponger == "A":                                  # PINGs sent before SCI-A listened never reach the F28232
        pings = [(t, n) for t, n in pings if t > SCI_LISTENS_MS]
    pongs = [(t, ln.split()[1]) for t, ln in run[ponger] if ln.startswith("PONG ")]
    assert [n for _, n in pongs] == [n for _, n in pings][:len(pongs)]
    assert len(pongs) >= len(pings) - 1               # the last PING may still be in flight at 40 ms
    for (tp, _), (tq, _) in zip(pings, pongs):
        assert 0 < tq - tp < 1.5                       # reply within ~2 line times


def test_pong_counters_and_led_toggles(run):
    pongs_to_a = sum(ln.startswith("PONG ") for _, ln in run["B"])
    pongs_to_b = sum(ln.startswith("PONG ") for _, ln in run["A"])
    assert (pongs_to_a, pongs_to_b) == (6, 6)         # STM32 PING 1 (5 ms) was dropped: SCI-A not listening yet
    pingn, pongs, peer, _tick, _h, _t, _len, pingsrx, helloans = run["c2k"]
    assert (pingn, pongs, peer, pingsrx, helloans) == (6, pongs_to_a, 1, pongs_to_b, 1)
    assert run["stm"] == {"pongs": pongs_to_b, "pings_answered": pongs_to_a, "peer_seen": 1, "ping_n": 7,
                          "hello_answered": 1}
    assert len(_toggles(run["evs"], "A", "GPIO34")) == 1 + pongs_to_a     # first: configured as output (L)
    assert len(_toggles(run["evs"], "B", "PB0")) == 1 + pongs_to_b


def test_peer_pins_high(run):
    lv = run["levels"]
    assert str(lv["GPIO35"]).endswith("H") and str(lv["PB1"]).endswith("H")
    a_hi = [t for t, v in _toggles(run["evs"], "A", "GPIO35") if v == "H"]
    b_hi = [t for t, v in _toggles(run["evs"], "B", "PB1") if v == "H"]
    b_hello_answer = [t for t, ln in run["B"] if ln.startswith("HELLO")][1]
    assert len(a_hi) == 1 and b_hello_answer < a_hi[0] < 9.5   # the STM32's answer HELLO (its boot HELLO was lost)
    drops = [e for e in run["evs"] if e.kind == "uart" and e.fields.get("dir") == "drop"]
    assert drops and all(e.fields.get("chip") == "A" and e.t_ns < SCI_LISTENS_MS * 1e6 + 0.1e6 for e in drops)
    assert len(b_hi) == 1 and 7.0 < b_hi[0] < 8.0     # after the F28232's HELLO line (~6.6-7.7 ms)
