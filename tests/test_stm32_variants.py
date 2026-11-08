"""STM32F405xx/STM32F407xx package variants from DS8626 Rev 12 (chips/stm32f405{rg,og,oe,vg}.yaml,
chips/stm32f407{vg,ve,zg,ze,ig,ie,igh,ieh}.yaml): chip descriptions, pin spot checks, the existing STM32 samples on
every variant, the per-package port walk, and the two-chip UART board boards/stm32f405rg_f407ig_uart.yaml."""
from __future__ import annotations

import functools
import io
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
STM32_BUILD = ROOT / "firmware" / "samples_stm32" / "build"
BUILD = ROOT / "firmware" / "samples_stm32_variants" / "build"
BOARD = ROOT / "boards" / "stm32f405rg_f407ig_uart.yaml"


def tool(name: str):
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)

# file, name, package, pin count, GPIO count (DS8626 Table 2), flash bytes, F407 (Ethernet + camera)
PARTS = [
    ("stm32f405rg", "STM32F405RG", "LQFP64", 64, 51, 0x100000, False),
    ("stm32f405og", "STM32F405OG", "WLCSP90", 90, 72, 0x100000, False),
    ("stm32f405oe", "STM32F405OE", "WLCSP90", 90, 72, 0x80000, False),
    ("stm32f405vg", "STM32F405VG", "LQFP100", 100, 82, 0x100000, False),
    ("stm32f405zg", "STM32F405ZG", "LQFP144", 144, 114, 0x100000, False),
    ("stm32f407vg", "STM32F407VG", "LQFP100", 100, 82, 0x100000, True),
    ("stm32f407ve", "STM32F407VE", "LQFP100", 100, 82, 0x80000, True),
    ("stm32f407zg", "STM32F407ZG", "LQFP144", 144, 114, 0x100000, True),
    ("stm32f407ze", "STM32F407ZE", "LQFP144", 144, 114, 0x80000, True),
    ("stm32f407ig", "STM32F407IG", "LQFP176", 176, 140, 0x100000, True),
    ("stm32f407ie", "STM32F407IE", "LQFP176", 176, 140, 0x80000, True),
    ("stm32f407igh", "STM32F407IGH", "UFBGA176+25", 201, 140, 0x100000, True),
    ("stm32f407ieh", "STM32F407IEH", "UFBGA176+25", 201, 140, 0x80000, True),
]
IDS = [p[0] for p in PARTS]
_DESC: dict = {}


def desc_of(fn: str):
    if fn not in _DESC:
        _DESC[fn] = load_chip(CHIPS / f"{fn}.yaml")
    return _DESC[fn]


def gpio_set(d) -> set[str]:
    return {p.gpio_name for p in d.pins if p.kind == "gpio"}


# --------------------------------------------------------------------------------------------- chip descriptions
@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash,f407", PARTS, ids=IDS)
def test_variant_description(fn, name, package, npins, ngpio, flash, f407):
    d = desc_of(fn)
    assert (d.name, d.package, d.family) == (name, package, "stm32")
    assert len(d.pins) == npins and sorted(p.num for p in d.pins) == list(range(1, npins + 1))
    assert len(gpio_set(d)) == ngpio
    mem = {m.name: (m.base, m.size) for m in d.memory}
    assert mem["FLASH"] == (0x08000000, flash)
    assert mem["SRAM"] == (0x20000000, 0x20000) and mem["CCMRAM"] == (0x10000000, 0x10000)
    assert mem["BKPSRAM"] == (0x40024000, 0x1000)
    # same die as the STM32F405ZG: core, vector table base, IRQs and peripheral map come from the include
    zg = desc_of("stm32f405zg")
    assert d.core == zg.core and d.extra["vtor"] == 0x08000000 and d.extra["systick_ext_div"] == 8
    assert d.irqs == zg.irqs and [(p.name, p.base) for p in d.peripherals] == [(p.name, p.base) for p in zg.peripherals]
    chip = Chip(d)
    assert not [e for e in chip.log if e.kind == "warn"]
    assert chip.desc.peripheral("USART6").model == "stm32.usart"


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash,f407", PARTS, ids=IDS)
def test_variant_pin_table_check(fn, name, package, npins, ngpio, flash, f407):
    rep = tool("pin_table_check").check_file(CHIPS / f"{fn}.yaml", expect=npins)
    assert rep.ok and not rep.warnings, rep.text()


@pytest.mark.parametrize("fn,svd", [("stm32f405rg", "svd/dl-stm32f405-Ij9M/STM32F405.svd"),
                                    ("stm32f407igh", "svd/dl-stm32f407/STM32F407.svd")])
def test_variant_svd_check(fn, svd):
    """Bases/IRQs against the vendor SVDs (every variant has the STM32F405ZG's peripheral map and IRQs, asserted in
    test_variant_description, so one F405 and one F407 part stand for all)."""
    if not (ROOT / svd).exists():
        pytest.skip(f"{svd} not downloaded")
    out = io.StringIO()
    assert tool("svd2chip").check(str(ROOT / svd), str(CHIPS / f"{fn}.yaml"), out=out) == 0, out.getvalue()


@pytest.mark.parametrize("fn,name,package,npins,ngpio,flash,f407", PARTS, ids=IDS)
def test_variant_functions_match_lqfp144(fn, name, package, npins, ngpio, flash, f407):
    """Every GPIO shared with the STM32F405ZG has its name and functions; the F407 adds only AF11 ETH_* and AF13
    DCMI_*; the F405 has neither."""
    zg = {p.gpio_name: p for p in desc_of("stm32f405zg").pins if p.kind == "gpio"}
    eth = dcmi = 0
    for p in desc_of(fn).pins:
        if p.kind != "gpio":
            continue
        extra = {k: v for k, v in p.functions.items() if "ETH_" in v or "DCMI_" in v}
        assert set(extra) <= {"AF11", "AF13"}, (p.name, extra)
        eth += "AF11" in extra
        dcmi += "AF13" in extra
        if p.gpio_name in zg:
            base = {k: v for k, v in p.functions.items() if k not in extra}
            assert (p.name, base, p.default) == (zg[p.gpio_name].name, zg[p.gpio_name].functions,
                                                 zg[p.gpio_name].default)
    assert (eth > 0, dcmi > 0) == (f407, f407)


# -------------------------------------------------------------- spot checks, read by eye from DS8626 Table 7 / figures
def by_name(d) -> dict[str, list]:
    out: dict[str, list] = {}
    for p in d.pins:
        out.setdefault(p.name, []).append(p)
    return out


@functools.lru_cache(maxsize=None)
def _balls(path: Path) -> dict[str, int]:
    return {p["ball"]: p["num"] for p in yaml.safe_load(path.read_text())["pins"] if "ball" in p}


def _ball_num(d, b: str) -> int:
    """`num` of the BGA ball / WLCSP bump `b` (the `ball:` key of the chip YAML)."""
    return _balls(Path(d.source_path))[b]


def test_lqfp64_spot_checks():
    d = desc_of("stm32f405rg")
    nums = {1: "VBAT", 2: "PC13", 5: "PH0/OSC_IN", 7: "NRST", 8: "PC0", 12: "VSSA", 13: "VDDA", 14: "PA0/WKUP",
            16: "PA2", 17: "PA3", 28: "PB2/BOOT1", 31: "VCAP_1", 42: "PA9", 43: "PA10", 46: "PA13", 47: "VCAP_2",
            54: "PD2", 60: "BOOT0", 63: "VSS", 64: "VDD"}
    for n, name in nums.items():
        assert d.pin(n).name == name, n
    assert d.pin("PA2").functions["AF7"] == "USART2_TX" and d.pin("PA3").functions["AF7"] == "USART2_RX"
    assert d.pin("PD2").functions["AF8"] == "UART5_RX"
    assert {p.port for p in d.pins if p.kind == "gpio"} == {"A", "B", "C", "D", "H"}
    assert "VREF+" not in by_name(d) and "PDR_ON" not in by_name(d)   # VREF+ bonded to VDDA, PDR_ON internal
    with pytest.raises(KeyError):
        d.pin("PE2")


def test_wlcsp90_spot_checks():
    d = desc_of("stm32f405og")
    balls = {"A1": "VDD", "A7": "BOOT0", "A8": "PDR_ON", "A9": "PC13", "A10": "VBAT", "B2": "PI1", "C3": "PI0",
             "D2": "PA9", "D3": "PA10", "D9": "BYPASS_REG", "G9": "VDDA", "G10": "NRST", "H10": "VSSA", "J10": "PA2"}
    for b, name in balls.items():
        assert d.pin(_ball_num(d, b)).name == name, b
    assert _ball_num(d, "A1") == 1 and _ball_num(d, "J10") == 90            # row-major A1..J10
    assert d.pin("BYPASS_REG").kind == "special"
    assert d.pin("PI1").functions == {"AF5": "SPI2_SCK/I2S2_CK", "AF15": "EVENTOUT"}   # F405: no DCMI_D8


def test_lqfp100_spot_checks():
    for fn in ("stm32f405vg", "stm32f407vg"):
        d = desc_of(fn)
        nums = {1: "PE2", 6: "VBAT", 14: "NRST", 20: "VSSA", 21: "VREF+", 23: "PA0/WKUP", 25: "PA2", 49: "VCAP_1",
                68: "PA9", 73: "VCAP_2", 94: "BOOT0", 99: "VSS", 100: "VDD"}
        for n, name in nums.items():
            assert d.pin(n).name == name, (fn, n)
    assert desc_of("stm32f407vg").pin("PE2").functions["AF11"] == "ETH_MII_TXD3"
    assert "AF11" not in desc_of("stm32f405vg").pin("PE2").functions


def test_lqfp176_spot_checks():
    d = desc_of("stm32f407ig")
    nums = {1: "PE2", 7: "PI8", 11: "PI9", 14: "VSS", 38: "VREF+", 43: "PH2", 48: "BYPASS_REG", 120: "PA9",
            132: "PI1", 152: "PG9", 157: "PG14", 171: "PDR_ON", 176: "PI7"}
    for n, name in nums.items():
        assert d.pin(n).name == name, n
    assert d.pin("PG14").functions["AF8"] == "USART6_TX" and d.pin("PG9").functions["AF8"] == "USART6_RX"
    assert d.pin("PG13").functions["AF8"] == "USART6_CTS"                    # DS8626 typo "UART6_CTS" corrected
    assert d.pin("PI8").functions == {"additional": "RTC_TAMP1/RTC_TAMP2/RTC_TS"}
    assert d.pin("PH6").functions == {"AF4": "I2C2_SMBA", "AF9": "TIM12_CH1", "AF11": "ETH_MII_RXD2", "AF15": "EVENTOUT"}
    assert d.pin("PA1").functions["AF11"] == "ETH_MII_RX_CLK/ETH_RMII_REF_CLK"
    assert d.pin("PA7").functions["AF11"] == "ETH_MII_RX_DV/ETH_RMII_CRS_DV"
    assert d.pin("PI5").functions["AF13"] == "DCMI_VSYNC"


def test_ufbga176_spot_checks():
    d = desc_of("stm32f407igh")
    balls = {"A1": "PE3", "A2": "PE2", "A7": "PG14", "C1": "VBAT", "C10": "PG9", "D2": "PI8", "D14": "PI1",
             "E15": "PA9", "J1": "NRST", "L4": "BYPASS_REG", "N1": "VREF-", "P1": "VREF+", "R1": "VDDA",
             "R15": "PB15", "G8": "VSS"}
    for b, name in balls.items():
        assert d.pin(_ball_num(d, b)).name == name, b
    assert _ball_num(d, "A1") == 1 and _ball_num(d, "R15") == 201
    power = [p for p in d.pins if p.kind == "power"]
    assert sum(p.name == "VSS" for p in power) == 36 and sum(p.name == "VDD" for p in power) == 14  # incl. 25 thermal
    assert len({p.gpio_name for p in d.pins if p.kind == "gpio"}) == 140


# ------------------------------------------------------------------- existing STM32 samples on every package variant
def boot(fn: str, elf: Path, **kw) -> Chip:
    if not elf.exists():
        pytest.skip(f"{elf} not built")
    chip = Chip(desc_of(fn), **kw)
    chip.load_firmware(load_firmware(elf))
    chip.reset()
    return chip


@pytest.mark.parametrize("fn", IDS)
def test_uart_echo_on_every_variant(fn):
    """firmware/samples_stm32/uart_echo (USART1 on PA9/PA10: bonded in every package) runs unchanged."""
    elf = STM32_BUILD / "uart_echo.elf"
    if not elf.exists():
        pytest.skip("firmware/samples_stm32 not built")
    s = Session(CHIPS / f"{fn}.yaml", elf)
    s.schedule_uart(1e6, "USART1", b"ok")
    s.run_for(2.5e6)
    assert s.uart_output("USART1") == b"hello\r\nok"
    d = s.chip.desc
    assert s.chip.pins.get("PA9").owner == "USART1_TX"
    num = {"LQFP64": 42, "LQFP100": 68, "LQFP144": 101, "LQFP176": 120}.get(d.package)
    if num is None:                                                    # ball D2 (WLCSP90) / E15 (UFBGA176)
        num = _ball_num(d, "D2" if d.package == "WLCSP90" else "E15")
    assert d.pin("PA9").num == num
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()


@pytest.mark.parametrize("fn", ["stm32f405rg", "stm32f405oe", "stm32f407igh"])
def test_tim_irq_pll_on_variants(fn):
    """HSE 8 MHz -> PLL 168 MHz, TIM2 1 ms period, as on the STM32F405ZG."""
    chip = boot(fn, STM32_BUILD / "tim_irq.elf")
    ev: list = []
    chip.pins.subscribe(lambda e: ev.append(e.t_ns) if e.pin == "PB0" and e.field == "level" else None)
    chip.run_for(5.5e6)
    assert chip.clock.hz == 168e6
    periods = [b - a for a, b in zip(ev[1:], ev[2:])]
    assert len(periods) == 4 and all(p == pytest.approx(1e6, rel=1e-3) for p in periods)


# --------------------------------------------------------------------------------- port walk: the bonded GPIO set
@pytest.mark.parametrize("fn", IDS)
def test_port_walk_toggles_exactly_the_package_gpios(fn):
    chip = boot(fn, BUILD / "port_walk.elf")
    edges: dict[str, int] = {}
    chip.pins.subscribe(lambda e: edges.__setitem__(e.pin, edges.get(e.pin, 0) + 1)
                        if e.field == "level" else None)
    chip.run_for(0.3e6)
    toggled = {p for p, n in edges.items() if n >= 4}
    assert toggled == gpio_set(desc_of(fn))
    assert all(chip.pins.get(g).owner == "GPIO" for g in toggled)
    assert not [e for e in chip.log if e.kind in ("warn", "fault")]


# ------------------------------------------------------------------------ two variants with connected UART lines
def board_session() -> Session:
    for name in ("link_requester", "link_responder"):
        if not (BUILD / f"{name}.elf").exists():
            pytest.skip("firmware/samples_stm32_variants not built (build.sh)")
    return Session.from_board(BOARD, quiet=True)


def sym(name: str, symbol: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(BUILD / f"{name}.elf", "rb") as f:
        return ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(symbol)[0]["st_value"]


def test_board_rg_ig_request_response():
    s = board_session()
    a, b = s.chips["A"], s.chips["B"]
    assert (a.desc.name, a.desc.package, b.desc.name, b.desc.package) == ("STM32F405RG", "LQFP64", "STM32F407IG",
                                                                          "LQFP176")
    s.run_for(21e6)
    # A: PLL 168 MHz, USART2 on APB1 = 42 MHz; B: HSI 16 MHz, USART6 on APB2
    assert a.clock.hz == 168e6 and b.clock.hz == 16e6
    ua, ub = a.peripherals[a.find_uart("USART2")], b.peripherals[b.find_uart("USART6")]
    assert ua.baud() == pytest.approx(42e6 / 182) and ub.baud() == pytest.approx(16e6 / 69)
    out_a, out_b = s.uart_output("A.USART2"), s.uart_output("B.USART6")
    assert out_a.startswith(b"REQ 1\r\nREQ 2\r\nREQ 3\r\n")
    assert out_b.startswith(b"ACK 1 1\r\nACK 2 4\r\nACK 3 9\r\n")
    assert b"ACK 10 100\r\n" in out_b
    rd = a.bus.read
    assert rd(sym("link_requester", "req_n"), 4) == 10                     # one request per 2 ms TIM2 period
    assert rd(sym("link_requester", "acks_ok"), 4) == 10
    assert rd(sym("link_requester", "ack_errors"), 4) == 0
    assert b.bus.read(sym("link_responder", "requests"), 4) == 10
    assert b.bus.read(sym("link_responder", "bad_lines"), 4) == 0
    # pins: muxed USART pins on both sides; LEDs toggled per request / per ACK; LINK_UP high
    assert a.pins.get("PA2").owner == "USART2_TX" and a.pins.get("PA3").owner == "USART2_RX"
    assert b.pins.get("PG14").owner == "USART6_TX" and b.pins.get("PG9").owner == "USART6_RX"
    led_b = [e.t_ns for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PI1" and e.field == "level"]
    led_a = [e for e in s.events(kinds={"pin"}, chip="A") if e.pin == "PB0" and e.field == "level"]
    assert len(led_b) == 1 + 10 and len(led_a) == 1 + 10                 # initial level + one toggle per request
    # A sends a request every 2 ms (TIM2 at 84 MHz / 84 / 2000), the first 2 ms after TIM2 starts
    req = [e.t_ns for e in s.events(kinds={"uart"}, chip="A") if e.fields.get("dir") == "tx" and e.byte == ord("R")]
    assert len(req) == 10 and all(b - a == pytest.approx(2e6, rel=1e-3) for a, b in zip(req, req[1:]))
    # B toggles PI1 right after handing the last reply byte to USART6: the 7-byte request arrives (stop bit of
    # '\n'), then TXE paces the 9-byte reply, one character per write, so the 9th write is 8 characters later
    t = led_b[1] - req[0]
    char_a, char_b = 10 / ua.baud() * 1e9, 10 / ub.baud() * 1e9
    lo = 7 * char_a + 8 * char_b
    assert lo < t < lo + char_b                                            # + B's ISR/parse time (a few us)
    assert a.pins.get("PC13").level() == "H" and a.pins.get("PB1").level() == "L"
    assert not [e for e in s.events(kinds={"warn", "fault"})]              # bauds within tolerance, no faults
    s.close()


def test_board_rg_ig_cli():
    board_session().close()
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--board", str(BOARD), "--for", "9ms"],
                       capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "A| === summary chip A (STM32F405RG)" in out and "B| === summary chip B (STM32F407IG)" in out
    # nets with a TX and an RX pin create one automatic one-way link per direction
    assert "uart A.USART2 -> B.USART6: 28 bytes A->B" in out and "uart B.USART6 -> A.USART2: 37 bytes B->A" in out
    assert 'A|   USART2 (28 bytes): "REQ 1\\r\\nREQ 2' in out
