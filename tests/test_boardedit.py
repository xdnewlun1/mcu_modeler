"""Board setup editing (chipem/boardedit.py): catalog, structural edits on the board mapping, save/open."""
from pathlib import Path

import pytest

from chipem import boardedit as be
from chipem.board import parse_board

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware/samples/build/blinky.elf"


def _two():
    d = be.new_board("t", "mk22fn1m0vll12", firmware=str(FW))
    d, _ = be.add_chip(d, "mk22fn1m0vll12")
    d.update({"nets": [{"name": "N", "pins": ["A.PTC5", "B.PTC6"]}, {"name": "ONLY_B", "pins": ["B.PTC7"]}],
              "links": [{"type": "uart", "from": "A.UART0", "to": "B.UART0"}],
              "parts": [{"id": "LED1", "type": "led", "ports": {"in": "B.PTC5"}},
                        {"id": "SW1", "type": "switch", "ports": {"out": "N"}}],
              "stimulus": ["5ms:B.PTC6=0", "A.PTC6=1"]})
    return d


def test_catalog_lists_chips_firmware_and_boards():
    cat = be.catalog()
    ids = {c["id"] for c in cat["chips"]}
    assert {"mk22fn1m0vll12", "stm32f405zg", "tms320f28232"} <= ids
    assert not any(i.endswith("_regs") for i in ids)
    assert any(f["path"] == str(FW.resolve()) for f in cat["firmware"])
    assert any(b["name"] == "two_k22_uart" for b in cat["boards"])


def test_add_chip_ids_and_validation():
    d = be.new_board("t", "mk22fn1m0vll12")
    d, cid = be.add_chip(d, "stm32f405zg")
    assert cid == "B"
    with pytest.raises(ValueError, match="already used"):
        be.add_chip(d, "stm32f405zg", cid="b")
    with pytest.raises(ValueError, match="bad chip id"):
        be.add_chip(d, "stm32f405zg", cid="9x")
    with pytest.raises(FileNotFoundError):
        be.add_chip(d, "no_such_chip")
    with pytest.raises(ValueError, match="firmware not found"):
        be.add_chip(d, "stm32f405zg", firmware="/nope.elf")
    parse_board(d, base=ROOT)                                 # a valid board description


def test_remove_chip_drops_its_wiring_only():
    d = be.remove_chip(_two(), "b")
    assert [c["id"] for c in d["chips"]] == ["A"]
    assert d["nets"] == [{"name": "N", "pins": ["A.PTC5"]}]   # ONLY_B is gone, N keeps A's pin
    assert "links" not in d
    led = next(p for p in d["parts"] if p["id"] == "LED1")
    assert led["ports"] == {"in": None} and next(p for p in d["parts"] if p["id"] == "SW1")["ports"] == {"out": "N"}
    assert d["stimulus"] == ["A.PTC6=1"]
    parse_board(d, base=ROOT)
    with pytest.raises(ValueError, match="at least one MCU"):
        be.remove_chip(d, "A")


def test_set_chip_firmware_keeps_wiring_but_a_new_chip_type_drops_it():
    d = be.set_chip(_two(), "B", firmware=None)
    assert "firmware" not in d["chips"][1] and len(d["nets"]) == 2
    d = be.set_chip(_two(), "B", chip="stm32f405zg")
    assert d["chips"][1]["chip"].endswith("stm32f405zg.yaml")
    assert d["nets"] == [{"name": "N", "pins": ["A.PTC5"]}] and "links" not in d


def test_save_and_open_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "BOARDS_DIR", tmp_path)
    p = be.save_board(_two(), "rt")
    text = p.read_text()
    assert "chip: mk22fn1m0vll12" in text
    fw = text.split("firmware: ")[1].split("}")[0]
    assert not fw.startswith("/") and (tmp_path / fw).resolve() == FW.resolve()   # relative to the board file
    with pytest.raises(FileExistsError):
        be.save_board(_two(), "rt")
    be.save_board(_two(), "rt", overwrite=True)
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            be.save_board(_two(), bad)
    d = be.open_board(str(p))
    assert [c["id"] for c in d["chips"]] == ["A", "B"] and Path(d["chips"][0]["firmware"]) == FW.resolve()
    bd = parse_board(d, base=ROOT)
    assert [n.name for n in bd.nets] == ["N", "ONLY_B"] and bd.links and bd.stimulus == ["5ms:B.PTC6=0", "A.PTC6=1"]
