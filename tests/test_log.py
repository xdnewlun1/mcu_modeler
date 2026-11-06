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
