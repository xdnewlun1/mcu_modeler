"""Pin discovery report (chipem.pinreport): PC attribution of PCR/PDDR/PSOR/PDIR accesses, counters, and the report
over the sample firmware and the Withings image."""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.pinreport import format_pin_report, pin_history, report_dict
from engine_util import build_image

ROOT = Path(__file__).resolve().parent.parent
K22 = ROOT / "chips" / "mk22fn1m0vll12.yaml"
FX512 = ROOT / "chips" / "mk22fx512vll12.yaml"
BUILD = ROOT / "firmware" / "samples" / "build"
WITHINGS = ROOT / "s1_off000084_503080.bin"

SNIPPET = """
  ldr r0, =0x40048038
  ldr r1, =0x3F80
  str r1, [r0]
  ldr r0, =0x4004B000
  ldr r1, =0x100
  str r1, [r0, #20]
  ldr r1, =0x103
  str r1, [r0, #24]
  ldr r1, =0x100
  str r1, [r0, #28]
  ldr r2, =0x400FF080
  movs r3, #0x20
  str r3, [r2, #20]
  str r3, [r2, #4]
  ldr r4, [r2, #16]
  ldr r4, [r2, #16]
  ldr r4, [r2, #16]
  str r3, [r2, #8]
spin:
  b spin
"""


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


def _sites(img, start: int, n: int = 0x60) -> dict[str, list[int]]:
    """'mnemonic op_str' -> addresses of the instructions in the assembled snippet."""
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    data = img.read(start, n)
    out: dict[str, list[int]] = {}
    for ins in md.disasm(data, start):
        out.setdefault(f"{ins.mnemonic} {ins.op_str}", []).append(ins.address)
        if ins.mnemonic == "b":
            break
    return out


@pytest.mark.parametrize("precise", [False, True])
def test_snippet_counts_and_pc_attribution(desc, precise):
    chip = Chip(desc, precise=precise)
    img, addrs = build_image({"reset": SNIPPET})
    chip.load_firmware(img)
    chip.reset()
    chip.run_cycles(60)
    sites = _sites(img, addrs["reset"])
    pcr5, pcr6, pcr7 = (sites[f"str r1, [r0, #{o}]"][0] for o in ("0x14", "0x18", "0x1c"))
    pddr, psor, pcor = (sites[f"str r3, [r2, #{o}]"][0] for o in ("0x14", "4", "8"))
    pdir = sites["ldr r4, [r2, #0x10]"]
    assert len(pdir) == 3

    rep = chip.pin_report
    # core.pc inside the MMIO callback is the accessing instruction itself (not the next one)
    h5 = rep.recs["PTC5"].history
    assert [(pc, w) for _, pc, w in h5] == [(pcr5, "mux ALT1 (GPIO)"), (pddr, "dir out (drive L)")]
    assert [(pc, w) for _, pc, w in rep.recs["PTC6"].history] == [(pcr6, "mux ALT1 (GPIO), pull up")]
    assert rep.recs["PTC5"].data_pcs == [psor, pcor]
    assert rep.recs["PTC6"].data_first_pc == pdir[0]
    assert rep.recs["PTC7"].data_first_pc == pdir[0]
    assert h5[0][0] < h5[1][0]                                     # timestamps are instruction-precise

    st = rep.stats()
    assert st["PTC6"]["reads"] == 3 and st["PTC7"]["reads"] == 3 and st["PTC5"].get("reads", 0) == 0
    assert st["PTC5"]["writes"] == 2 and st["PTC5"]["changes"] == 2
    assert st["PTC7"]["floating"] and not st["PTC6"].get("floating")

    # interrupt arming and ISF counting
    chip.bus.write(0x4004B000 + 24, 4, 0x000A0103)                 # PTC6: IRQ falling edge
    chip.pins.set_external("PTC6", "L")
    chip.pins.set_external("PTC6", "H")
    chip.pins.set_external("PTC6", "L")
    st = rep.stats()
    assert st["PTC6"]["irq_mode"] == "falling edge" and st["PTC6"]["irqs"] == 2
    assert rep.recs["PTC6"].history[-1][2] == "irq falling edge"

    text = format_pin_report(chip, static=False)
    assert "PTC7" in text and "FLOATING when read" in text and "--set PTC7=1" in text
    assert "GPIO in (pull-up, IRQ falling)" in text


def test_hot_path_counters_need_no_report_object(desc):
    """Reads are attributed lazily: a direction change in the middle splits the count correctly."""
    chip = Chip(desc)
    chip.bus.write(0x40048038, 4, 0x3F80)
    chip.bus.write(0x4004B000 + 20, 4, 0x100)                      # PTC5 GPIO (input)
    for _ in range(5):
        chip.bus.read(0x400FF080 + 0x10, 4)
    chip.bus.write(0x400FF080 + 0x14, 4, 0x20)                     # PTC5 -> output
    for _ in range(7):
        chip.bus.read(0x400FF080 + 0x10, 4)
    assert chip.peripherals["GPIOC"].pin_stats()["PTC5"]["reads"] == 5


def _boot(desc, name, *stim):
    elf = BUILD / f"{name}.elf"
    if not elf.exists():
        pytest.skip(f"{elf} not built")
    chip = Chip(desc, clock_hz=120e6)
    chip.load_firmware(load_firmware(elf))
    chip.reset()
    for t, pin, lvl in stim:
        chip.sched.in_ns(t, lambda p=pin, v=lvl: chip.pins.set_external(p, v))
    chip.run_for(3e6)
    return chip


def test_button_mirror_report(desc):
    chip = _boot(desc, "button_mirror", (1e6, "PTC6", "L"), (2e6, "PTC6", "H"))
    d = report_dict(chip)
    pins = {p["pin"]: p for p in d["pins"]}
    assert set(pins) == {"PTC5", "PTC6"}
    assert pins["PTC5"]["role"] == "GPIO out" and pins["PTC6"]["role"] == "GPIO in (pull-up)"
    assert pins["PTC5"]["cfg_by"].startswith("main+") and pins["PTC6"]["cfg_by"].startswith("main+")
    assert pins["PTC6"]["reads"] > 10000 and pins["PTC5"]["writes"] > 10000 and pins["PTC5"]["changes"] == 2
    assert pins["PTC5"]["static"] == pins["PTC6"]["static"] == "confirmed"
    text = d["text"]
    assert "PTC6: " in text and "PDIR reads" in text and "main: 2 pins (PTC5, PTC6)" in text
    assert "confirmed static guess: 2 (PTC5, PTC6)" in text
    hist = pin_history(chip, "PTC6")
    assert "mux ALT1 (GPIO), pull up" in hist and "main+0x" in hist


def test_port_irq_report(desc):
    chip = _boot(desc, "port_irq", (1e6, "PTC6", "L"), (2e6, "PTC6", "H"))
    d = report_dict(chip)
    pins = {p["pin"]: p for p in d["pins"]}
    assert pins["PTC6"]["role"] == "GPIO in (pull-up, IRQ falling)" and pins["PTC6"]["irqs"] == 1
    assert pins["PTC5"]["changes"] == 1
    assert "IRQ falling edge, 1 interrupt flag set, cleared by PORTC_IRQHandler" in d["text"]
    assert "driven by PORTC_IRQHandler" in d["text"]


def test_withings_report():
    if not WITHINGS.exists():
        pytest.skip("Withings image not present")
    from chipem.session import Session
    s = Session(FX512, WITHINGS, quiet=True)
    s.attach_spi_flash("SPI0:cs=PTC4")
    s.apply_stimulus("PTD2=1")
    s.run_for(3e9)
    d = report_dict(s.chip)
    pins = {p["pin"]: p for p in d["pins"]}
    assert len(pins) >= 15
    assert pins["PTA13"]["role"] == "GPIO in (IRQ logic 0)" and "FLOATING when read" in pins["PTA13"]["notes"]
    assert pins["PTD2"]["role"].startswith("UART2_RX") and pins["PTD3"]["role"] == "UART2_TX"
    assert pins["PTC4"]["changes"] > 100                            # SPI flash chip select
    assert all(p["cfg_by"].startswith(("sub_", "0x")) for p in pins.values())
    assert sum(p["cfg_by"].split("+")[0] == pins["PTA5"]["cfg_by"].split("+")[0] for p in pins.values()) >= 10
    text = d["text"]
    assert "UART2: PTD2 RX, PTD3 TX" in text and "SPI0: PTC5 SCK" in text and "--set PTA13=1" in text
    assert "not predicted statically" in text


def test_shell_commands():
    pytest.importorskip("chipem.session")
    fw = BUILD / "button_mirror.elf"
    if not fw.exists():
        pytest.skip("sample firmware not built")
    from chipem import cmd_shell
    from chipem.cli import build_parser
    args = build_parser().parse_args(["shell", str(fw), "--exec", "run 1ms; pinreport nostatic; pin PTC6"])
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        rc = cmd_shell.main(args)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert rc == 0, out
    assert "Discovery summary" in out and "GPIO in (pull-up)" in out
    assert "firmware activity:" in out and "mux ALT1 (GPIO), pull up" in out


def test_web_pinreport_endpoint():
    pytest.importorskip("chipem.session")
    fw = BUILD / "button_mirror.elf"
    if not fw.exists():
        pytest.skip("sample firmware not built")
    from chipem.session import Session
    from chipem.web.server import WebApp
    s = Session("mk22fn1m0vll12", fw)
    app = WebApp(s, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{app.port}"
    try:
        req = urllib.request.Request(f"{base}/api/run", data=b'{"ms": 1}', method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()

        def get(path):
            with urllib.request.urlopen(base + path, timeout=30) as r:
                return json.loads(r.read())
        end = time.monotonic() + 10
        st = get("/api/state")
        while time.monotonic() < end and (st.get("running") or st.get("t_ns", 0) < 1e6):
            time.sleep(0.02)
            st = get("/api/state")
        row = next(p for p in st["pins"] if p["short"] == "PTC6")
        assert row["cfg_by"].startswith("main+") and row["cfg_t"] > 0
        d = get("/api/pinreport?static=0")
        assert {p["pin"] for p in d["pins"]} == {"PTC5", "PTC6"} and "Discovery summary" in d["text"]
    finally:
        app.stop()
        s.close()


def test_shell_board_uart_prefix_and_pinreport_per_chip():
    board = ROOT / "boards" / "two_k22_uart.yaml"
    if not board.exists():
        pytest.skip("board example not present")
    from chipem import cmd_shell
    from chipem.cli import build_parser
    try:
        args = build_parser().parse_args(["shell", "--board", str(board), "--exec",
                                          "run 5ms; uart B.UART0 x; run 1ms; uartout B.UART0; pinreport nostatic"])
    except SystemExit:
        pytest.skip("--board not available")
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        rc = cmd_shell.main(args)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert rc == 0, out
    assert "B.UART0 <- b'x'" in out and "--- B.UART0" in out
    assert "=== chip A ===" in out and "=== chip B ===" in out


EPWM_IMG = ROOT / "firmware" / "samples_c2000" / "build" / "epwm.c28img"


@pytest.mark.skipif(not EPWM_IMG.exists(), reason="C2000 samples not built")
def test_pwm_pins_count_changes_and_show_frequency_duty():
    """PWM outputs are driven through PinBoard.drive_signal, not the GPIO model: their `chg` column comes from the
    PinBoard edges and the notes carry the measured frequency / duty."""
    from chipem.pinreport import pin_waves, report_dict
    from chipem.session import Session
    s = Session("tms320f28232", EPWM_IMG, quiet=True)
    try:
        s.run_for(20e6)
        rows = {r["pin"]: r for r in report_dict(s.chip, static=False)["pins"]}
        waves = pin_waves(s.chip)
        text = format_pin_report(s.chip, static=False)
    finally:
        s.close()
    a = rows["GPIO0"]
    assert a["changes"] > 400
    assert any("periodic 20 kHz, duty 29.0 %" in n for n in a["notes"]), a["notes"]
    assert abs(waves["GPIO0"]["freq_hz"] - 20e3) < 1 and waves["GPIO0"]["duty"] == 29.0
    assert waves["GPIO0"]["edges"] == a["changes"]
    assert "Outputs driven by peripherals" in text


def test_wave_of_glitch_then_clean_1khz():
    """A start-up glitch edge must not skew the reported frequency (median period); the edge count stays exact."""
    from chipem.pinreport import PinRecord, fmt_wave, wave_of
    rec = PinRecord(pin=None)
    edges = [(30e3, "H"), (31e3, "L")]
    t = 100e3
    for _ in range(20):
        edges += [(t, "H"), (t + 250e3, "L")]
        t += 1e6
    for tt, v in edges:
        rec.level_events += 1
        rec.edge_times.append(tt)
        rec.edge_levels.append(v)
    w = wave_of(rec, now=t)
    assert w["edges"] == 42 and w["freq_hz"] == 1000.0 and w["duty"] == 25.0
    assert fmt_wave(w) == "periodic 1 kHz, duty 25.0 %"
