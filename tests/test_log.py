from chipem.log import EventLog, format_event


def test_event_subscribe_filter():
    log = EventLog()
    seen, pins_only = [], []
    log.subscribe(seen.append)
    unsub = log.subscribe(pins_only.append, kinds="pin")
    log.event("pin", 12_345_000, pin="PTC5", field="level", old="L", new="H")
    log.event("uart", 13_000_000, name="UART0", data=b"hi\r\n")
    log.event("info", 14_000_000, msg="hello")
    assert len(log) == 3 and len(seen) == 3 and len(pins_only) == 1
    assert [e.kind for e in log] == ["pin", "uart", "info"]
    assert log.filter("pin")[0].pin == "PTC5"
    assert log.filter(["uart", "info"])[1].msg == "hello"
    assert log.filter(since_ns=13e6, until_ns=13e6)[0].kind == "uart"
    assert log.filter("pin", pin="PTC6") == []
    assert log.of_kind("info")[0].get("msg") == "hello"
    unsub()
    log.event("pin", 15e6, pin="PTC5", field="level", old="H", new="L")
    assert len(pins_only) == 1
    assert log[-1].seq == 4 and log[-1].to_dict()["new"] == "L"
    log.clear()
    assert len(log) == 0


def test_format():
    log = EventLog()
    ev = log.event("pin", 12_345_000, pin="PTC5", field="level", old="L", new="H")
    assert format_event(ev) == "[    12.345 ms] pin PTC5 level L->H"
    assert format_event(log.event("uart", 0, name="UART0", data=b"a\n")) == "[     0.000 ms] uart UART0 tx 'a\\n'"
    s = format_event(log.event("mmio", 1e6, op="w", addr=0x4004B014, value=0x100))
    assert s == "[     1.000 ms] mmio op=w addr=0x4004B014 value=0x100"
    assert format_event(log.warn(2e6, "unmapped", pc=0x1A2C)) == "[     2.000 ms] warn unmapped pc=0x1A2C"


def test_maxlen():
    log = EventLog(maxlen=2)
    for i in range(5):
        log.event("info", i, msg=str(i))
    assert [e.msg for e in log] == ["3", "4"]


def _pwm(log, pin, t0, t1, period, duty=0.25, **kw):
    t, n = t0, 0
    while t < t1:
        log.event("pin", t, pin=pin, field="level", old="L", new="H", **kw)
        log.event("pin", t + duty * period, pin=pin, field="level", old="H", new="L", **kw)
        t += period
        n += 2
    return n


def test_pwm_edges_are_bounded_in_stored_history():
    """A 20 kHz PWM pin: subscribers get every edge; the stored history keeps the first `pin_burst` dense edges, then
    one `wave` event per 100 ms window, so other events are not evicted. Slow pins keep every edge."""
    log = EventLog(maxlen=5000)
    seen = []
    log.subscribe(seen.append, kinds="pin")
    log.info(0, "boot")
    n = _pwm(log, "PA6", 1e3, 1e9, 50e3)                     # 1 s at 20 kHz = 40000 edges
    slow = _pwm(log, "PB0", 0, 1e9, 100e6, duty=0.5)          # 10 Hz: never dense
    log.flush_waves()
    assert len(seen) == n + slow
    stored = [e for e in log if e.kind == "pin" and e.pin == "PA6"]
    edges = [e for e in stored if e.field == "level"]
    waves = [e for e in stored if e.field == "wave"]
    assert len(edges) == 1000 + log.pin_dense_edges - 1      # the edges before it became dense + the burst
    assert sum(w.edges for w in waves) + len(edges) == n
    assert 9 <= len(waves) <= 11
    w = waves[1]
    assert abs(w.freq_hz - 20e3) < 1 and abs(w.duty - 25.0) < 0.01 and w.since_ns < w.t_ns
    assert log.filter("info")[0].msg == "boot"               # not evicted
    assert len([e for e in log if e.kind == "pin" and e.pin == "PB0"]) == slow
    assert "wave" in format_event(w) and "Hz" in format_event(w)


def test_pwm_pause_starts_a_new_burst_and_flushes_window():
    log = EventLog(pin_burst=10)
    n1 = _pwm(log, "PA6", 0, 5e6, 50e3)                       # 200 edges
    log.flush_waves(5e6)                                     # still running: window stays open
    assert not [e for e in log if e.get("field") == "wave"]
    log.flush_waves(30e6)                                    # stopped 25 ms ago: stored
    waves = [e for e in log if e.get("field") == "wave"]
    assert len(waves) == 1
    n2 = _pwm(log, "PA6", 1e9, 1.005e9, 50e3)                # resumes: a new burst of stored edges
    log.flush_waves()
    edges = [e for e in log if e.get("field") == "level"]
    waves = [e for e in log if e.get("field") == "wave"]
    assert sum(w.edges for w in waves) + len(edges) == n1 + n2
    assert len([e for e in edges if e.t_ns >= 1e9]) == 10 + log.pin_dense_edges - 1


def test_pin_burst_none_stores_everything():
    log = EventLog(pin_burst=None)
    n = _pwm(log, "PA6", 0, 100e6, 50e3)
    assert len(log) == n
