"""The `pin` `wave` event contract (chipem/log.py): `duty` is a percentage (0..100) for every producer: EventLog's own
summaries of dense pins, the synthesized clocks of chipem/periph/sdbus.py ClockWave (SD host clocks, Ethernet REF_CLK /
MDC, I2S MCK, C2000 XCLKOUT) and the camera part's PIXCLK."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _waves(events):
    return [e for e in events if e.kind == "pin" and e.fields.get("field") == "wave"]


def test_eventlog_summary_is_percent():
    from chipem.log import EventLog
    log = EventLog(pin_burst=10, pin_dense_edges=5, pin_dense_ns=1e6, pin_wave_ns=1e5)
    t = 0.0
    for _ in range(400):                           # 1 MHz, 25 % high
        log.event("pin", t, pin="PA0", field="level", old="L", new="H")
        log.event("pin", t + 250, pin="PA0", field="level", old="H", new="L")
        t += 1000
    log.flush_waves()
    w = _waves(log)
    assert w and all(e.fields["duty"] is None or 0 < e.fields["duty"] <= 100 for e in w)
    assert any(abs(e.fields["duty"] - 25.0) < 0.5 for e in w if e.fields["duty"] is not None)


def _board_waves(board: str, ms: float):
    from chipem.session import Session
    p = ROOT / "boards" / board
    if not p.exists():
        pytest.skip(f"{board} missing")
    try:
        s = Session.from_board(p)
    except FileNotFoundError as e:
        pytest.skip(str(e))
    try:
        s.run_for(ms * 1e6)
        s._flush_waves(stopped_only=False)
        for part in (s.board.parts.values() if s.board is not None else ()):
            if hasattr(part, "_flush_wave"):         # camera PIXCLK window
                part._flush_wave(s.board.now_ns())
        for c in s.ctxs.values():                  # flush the open ClockWave windows
            for blk in c.chip.peripherals.values():
                for w in [getattr(blk, "wave", None), *getattr(blk, "_waves", {}).values()]:
                    if w is not None and hasattr(w, "flush"):
                        w.flush()
        return _waves(s.log)
    finally:
        s.close()


def test_clockwave_sd_clock_is_percent():
    w = _board_waves("k22_sdcard.yaml", 60)
    assert w, "SD clock summaries expected"
    assert all(e.fields["duty"] == 50.0 for e in w if e.fields.get("pin", "").startswith("PTE"))
    assert all(0 < e.fields["duty"] <= 100 for e in w if e.fields["duty"] is not None)


def test_camera_pixclk_is_percent():
    from chipem.parts.camera import Camera                                     # noqa: F401
    w = _board_waves("stm32f407_dcmi_camera.yaml", 30)
    cam = [e for e in w if e.fields.get("freq_hz") and e.fields.get("edges") and e.fields.get("duty") == 50.0]
    bad = [e for e in w if e.fields.get("duty") is not None and not 0 < e.fields["duty"] <= 100]
    assert not bad
    assert cam, "camera PIXCLK summary expected"


def test_camera_flush_wave_duty_unit():
    """Direct: the camera part's PIXCLK summary carries duty 50.0 (percent)."""
    import inspect
    from chipem.parts import camera
    src = inspect.getsource(camera)
    assert "duty=50.0" in src and "duty=0.5" not in src
    from chipem.periph import sdbus
    assert "duty=50.0" in inspect.getsource(sdbus) and "duty=0.5" not in inspect.getsource(sdbus)
