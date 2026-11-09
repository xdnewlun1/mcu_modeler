from pathlib import Path

import pytest

from chipem.chipdesc import load_chip
from chipem.pins import Pin, PinBoard, PinEvent

ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now_ns(self):
        return self.t


@pytest.fixture
def board():
    clk = FakeClock()
    b = PinBoard.from_desc(load_chip(ROOT / "chips" / "mk22fn1m0vll12.yaml"), clk)
    b.clk = clk
    return b


def test_from_desc(board):
    assert len(board) == 100
    p = board.get("PTC5")
    assert p is board.get(77) is board.get("PTC5/LLWU_P9") is board.get("llwu_p9") is board.get("77")
    assert p.port == "C" and p.bit == 5 and p.id == "PTC5"
    assert p.owner == "disabled" and p.direction == "none" and p.level() == "X"
    assert board.get("PTE0").owner == "ADC1_SE4a"
    assert board.get(14).owner == "analog" and board.get(14).direction == "analog"
    assert board.get("VDD").level() == "H" and board.get(9).level() == "L" and board.get("VSSA").level() == "L"
    assert board.gpio("C", 6).num == 78
    with pytest.raises(KeyError):
        board.get("PTZ9")


def test_resolution_rules():
    p = Pin(num=1, name="PTA0", port="A", bit=0)
    assert p.level() == "X"                       # floating
    p.pull = "up"
    assert p.level() == "H"
    p.pull = "down"
    assert p.level() == "L"
    p.external = "H"
    assert p.level() == "H"                       # external beats pull
    p.direction, p.drive = "out", "L"
    assert p.level() == "L"                       # output drive beats external
    p.drive = "H"
    assert p.level() == "H"
    p.drive = None
    assert p.level() == "X"                       # output without a drive value
    p.direction = "in"
    p.external = None
    assert p.level() == "L"                       # back to the pull


def test_events_and_listeners(board):
    events: list[PinEvent] = []
    board.subscribe(events.append)
    heard = []
    board.input_listeners["PTC6/LLWU_P10"].append(lambda pin: heard.append((pin.id, pin.level())))

    board.clk.t = 1000.0
    board.mcu_update("PTC6", owner="GPIO", direction="in", pull="up")
    assert [(e.field, e.old, e.new) for e in events] == [
        ("owner", "CMP0_IN0", "GPIO"), ("direction", "none", "in"), ("pull", None, "up"), ("level", "X", "H")]
    assert all(e.pin == "PTC6" and e.t_ns == 1000.0 for e in events)
    assert heard == [("PTC6", "H")]

    events.clear()
    board.clk.t = 2000.0
    board.set_external("PTC6", "L")
    assert events == [PinEvent(2000.0, "PTC6", "level", "H", "L")]
    assert heard[-1] == ("PTC6", "L")

    events.clear()
    board.set_external("PTC6", 0)                  # same level: no event
    assert events == []
    board.set_external("PTC6", None)               # disconnect -> pull-up
    assert events[-1].new == "H"
    board.mcu_update("PTC6", pull=None)            # explicit None clears the pull
    assert board.get("PTC6").pull is None and events[-1].new == "X"
    board.mcu_update("PTC6", direction=None)       # None owner/direction = unchanged
    assert board.get("PTC6").direction == "in"


def test_output_drive_and_reset(board):
    events = []
    unsub = board.subscribe(events.append)
    board.mcu_update("PTC5", owner="GPIO", direction="out", drive="L")
    board.mcu_update("PTC5", drive="H")
    board.mcu_update("PTC5", drive=1)              # unchanged level
    levels = [(e.old, e.new) for e in events if e.field == "level"]
    assert levels == [("X", "L"), ("L", "H")]
    board.set_external("PTC5", "L")                # contention: output wins
    assert board.get("PTC5").level() == "H"
    board.reset()
    p = board.get("PTC5")
    assert (p.owner, p.direction, p.drive, p.pull, p.external) == ("disabled", "none", None, None, "L")
    assert p.level() == "L"
    unsub()
    n = len(events)
    board.set_external("PTC5", "H")
    assert len(events) == n


def test_bad_values(board):
    with pytest.raises(ValueError):
        board.set_external("PTC5", "maybe")
    with pytest.raises(ValueError):
        board.mcu_update("PTC5", direction="sideways")
    with pytest.raises(ValueError):
        board.mcu_update("PTC5", pull="left")


def test_no_clock():
    b = PinBoard([Pin(num=1, name="PTA0", port="A", bit=0)])
    ev = []
    b.subscribe(ev.append)
    b.set_external(1, "high")
    assert ev[0].t_ns == 0.0 and ev[0].new == "H"


# ---- peripheral output signals (drive_signal / release_signal / signal_levels / signal_pins / signal_matches) ----
def _sig_board():
    clk = FakeClock()
    pins = [Pin(1, "PA6", functions={"AF2": "TIM3_CH1", "AF1": "TIM1_BKIN"}, port="A", bit=6, owner="GPIO",
                direction="in", gpio_name_format="P{port}{bit}"),
            Pin(2, "PA0", functions={"AF1": "TIM2_CH1_ETR"}, port="A", bit=0, owner="GPIO", direction="in",
                gpio_name_format="P{port}{bit}"),
            Pin(3, "PB1", functions={"AF1": "TIM1_CH1/TIM1_CH1N"}, port="B", bit=1, owner="GPIO", direction="in",
                gpio_name_format="P{port}{bit}"),
            Pin(4, "PB2", functions={"AF1": "TIM1_CH10"}, port="B", bit=2, owner="GPIO", direction="in",
                gpio_name_format="P{port}{bit}")]
    b = PinBoard(pins, clk)
    b.clk = clk
    ev = []
    b.subscribe(ev.append)
    return b, ev


def test_signal_matches_rules():
    m = PinBoard.signal_matches
    assert m("TIM3_CH1", "TIM3_CH1")
    assert m("TIM2_CH1_ETR", "TIM2_CH1")                  # bundled with a letters-only qualifier
    assert m("JTAG_TDO/TIM1_CH1", "TIM1_CH1")             # a '/'-part
    assert not m("TIM1_CH10", "TIM1_CH1")                 # different channel
    assert not m("TIM1_CH1", "TIM1")                      # module name is not a channel
    assert not m("TIM1_CH1N", "TIM1_CH1")
    assert not m("", "TIM1_CH1") and not m(None, "TIM1_CH1") and not m("TIM1_CH1", "")


def test_signal_pins_static_from_functions():
    b, _ = _sig_board()
    assert [p.id for p in b.signal_pins("TIM3_CH1")] == ["PA6"]
    assert [p.id for p in b.signal_pins("TIM2_CH1")] == ["PA0"]
    assert [p.id for p in b.signal_pins("TIM1_CH1")] == ["PB1"]      # not PB2 (TIM1_CH10)
    assert b.signal_pins("TIM9_CH1") == []


def test_drive_signal_follows_only_owned_output_pins():
    b, ev = _sig_board()
    b.drive_signal("TIM3_CH1", "H")                       # pin still GPIO: nothing changes
    assert b.signal_levels == {"TIM3_CH1": "H"} and b.get("PA6").level() == "X" and not ev
    b.mcu_update("PA6", owner="TIM3_CH1", direction="out", drive="L")   # mux: idle L, but the signal is H now
    assert b.get("PA6").level() == "H"
    b.clk.t = 100.0
    ev.clear()
    b.drive_signal("TIM3_CH1", "L")
    b.drive_signal("TIM3_CH1", "L")                       # unchanged: no event
    assert [(e.t_ns, e.pin, e.old, e.new) for e in ev] == [(100.0, "PA6", "H", "L")]
    b.drive_signal("TIM3_CH1", 1)
    assert b.get("PA6").level() == "H"
    b.mcu_update("PA6", direction="in")                   # input: drive ignored
    ev.clear()
    b.drive_signal("TIM3_CH1", "L")
    assert not [e for e in ev if e.field == "level"]


def test_release_signal_restores_mux_drive_or_idle():
    b, ev = _sig_board()
    b.mcu_update("PA6", owner="TIM3_CH1", direction="out", drive="L")
    b.drive_signal("TIM3_CH1", "H")
    assert b.get("PA6").level() == "H"
    b.release_signal("TIM3_CH1")                          # back to the drive the mux model asked for (L)
    assert b.get("PA6").level() == "L" and "TIM3_CH1" not in b.signal_levels
    b.drive_signal("TIM3_CH1", "L")
    b.release_signal("TIM3_CH1", "H")                     # explicit idle level
    assert b.get("PA6").level() == "H"
    ev.clear()
    b.release_signal("TIM3_CH1", "L")                     # not driven any more: no-op
    assert not ev and b.get("PA6").level() == "H"


def test_signal_level_prefers_exact_match_deterministically():
    b, _ = _sig_board()
    b.drive_signal("TIM1_CH1N", "L")
    b.drive_signal("TIM1_CH1", "H")
    b.mcu_update("PB1", owner="TIM1_CH1N", direction="out", drive="L")
    assert b.get("PB1").level() == "L"                    # TIM1_CH1N exactly, not TIM1_CH1 (driven later)
    b.mcu_update("PA0", owner="TIM2_CH1_ETR", direction="out", drive="L")
    b.drive_signal("TIM2_CH1", "H")
    assert b.get("PA0").level() == "H"                    # bundled match
    b.drive_signal("TIM2_CH1_ETR", "L")
    assert b.get("PA0").level() == "L"                    # the exact signal beats the bundled one
    b.drive_signal("TIM2_CH1", "H")
    assert b.get("PA0").level() == "L"                    # ... whatever the order of updates
    b.release_signal("TIM2_CH1_ETR")
    assert b.get("PA0").level() == "H"                    # still carries the driven TIM2_CH1


def test_reset_clears_signal_levels():
    b, _ = _sig_board()
    b.mcu_update("PA6", owner="TIM3_CH1", direction="out", drive="L")
    b.drive_signal("TIM3_CH1", "H")
    b.reset()
    assert b.signal_levels == {} and b.get("PA6").owner == "GPIO" and b.get("PA6").direction == "in"
