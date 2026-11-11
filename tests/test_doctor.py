"""`chipem doctor` and `chipem --version` (chipem/doctor.py): tool discovery is faked with monkeypatch."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from chipem import __version__, cli, doctor


@pytest.fixture
def no_tools(monkeypatch):
    """Nothing on PATH, no TI toolchain; subprocess must not be called for missing tools."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_find_cl2000", lambda root, env=None: "")
    monkeypatch.delenv("CHIPEM_UI_BROWSER", raising=False)

    def boom(*a, **k):
        raise AssertionError("no tool should be run")
    monkeypatch.setattr(doctor.subprocess, "run", boom)


def _by_name(checks):
    return {c.name: c for c in checks}


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"chipem {__version__}"


def test_required_present_optional_missing(no_tools, capsys):
    c = _by_name(doctor.collect())
    for n in ("python", "unicorn", "capstone", "pyelftools", "PyYAML", "chips/"):
        assert c[n].ok and c[n].required, n
    for n in ("gdb", "clang", "ld.lld", "sdcc", "pdftotext", "node", "firefox", "cl2000 (TI C2000 CGT)"):
        assert not c[n].ok and not c[n].required, n
    assert cli.main(["doctor"]) == 0                       # optional tools missing: still exit 0
    out = capsys.readouterr().out
    assert "required: all present" in out and "no  rebuild ARM samples" in out


def test_calibration_reported(no_tools):
    c = _by_name(doctor.collect())["unicorn counter calibration"]
    from chipem.core.unicorn_cm import UnicornCortexM
    assert c.ok == UnicornCortexM().exact_counts


def test_tools_found_and_sdcc_fedora_names(monkeypatch):
    paths = {"clang": "/usr/bin/clang", "ld.lld": "/usr/bin/ld.lld", "sdcc-sdcc": "/usr/bin/sdcc-sdcc",
             "sdcc-sdas8051": "/usr/bin/sdcc-sdas8051", "sdcc-s51": "/usr/bin/sdcc-s51", "gdb": "/usr/bin/gdb"}
    monkeypatch.setattr(doctor.shutil, "which", paths.get)
    monkeypatch.setattr(doctor, "_find_cl2000",
                        lambda root, env=None: "/x/toolchains/ti-cgt-c2000_25.11.1.LTS/bin/cl2000")
    outs = {"/usr/bin/clang": "clang version 22.1.8 (Fedora)", "/usr/bin/ld.lld": "LLD 22.1.8 (compatible)",
            "/usr/bin/sdcc-sdcc": "SDCC : mcs51/z80 TD- 4.5.0 #15242 (Linux)",
            "/usr/bin/gdb": "GNU gdb (Fedora Linux) 17.2-4.fc44"}
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda argv, **k: SimpleNamespace(stdout=outs.get(argv[0], ""), stderr="", returncode=0))
    c = _by_name(doctor.collect())
    assert c["clang"].version == "22.1.8" and c["ld.lld"].version == "22.1.8"
    assert c["sdcc"].ok and c["sdcc"].version == "4.5.0"
    assert c["sdcc"].extra["names"] == {"cc": "sdcc-sdcc", "as": "sdcc-sdas8051", "sim": "sdcc-s51"}
    assert c["gdb"].version == "17.2-4.fc44"
    assert c["cl2000 (TI C2000 CGT)"].version == "25.11.1.LTS"
    f = dict(doctor.features(list(c.values())))
    assert f["rebuild ARM samples"] and f["rebuild C28x C samples"] and f["8051 C (SDCC)"]


def test_tool_timeout_is_harmless(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda n: "/usr/bin/" + n if n == "node" else None)
    monkeypatch.setattr(doctor, "_find_cl2000", lambda root, env=None: "")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 5)
    monkeypatch.setattr(doctor.subprocess, "run", slow)
    c = _by_name(doctor.collect())["node"]
    assert c.ok and c.version == ""


def test_missing_required_exits_nonzero(no_tools, monkeypatch, capsys):
    real = doctor._importable
    monkeypatch.setattr(doctor, "_importable", lambda mod: False if mod == "elftools" else real(mod))
    assert cli.main(["doctor"]) == 1
    assert "required missing: pyelftools" in capsys.readouterr().out


def test_missing_chips_dir(no_tools, tmp_path):
    c = _by_name(doctor.collect(root=tmp_path))["chips/"]
    assert not c.ok and c.required and "pip install -e" in c.detail


def test_cl2000_lookup(tmp_path):
    assert doctor._find_cl2000(tmp_path, env={}) == ""
    cl = tmp_path / "toolchains" / "ti-cgt-c2000" / "ti-cgt-c2000_25.11.1.LTS" / "bin" / "cl2000"
    cl.parent.mkdir(parents=True)
    cl.write_text("#!/bin/sh\n")
    cl.chmod(0o755)
    assert doctor._find_cl2000(tmp_path, env={}) == str(cl)
    other = tmp_path / "ti" / "bin" / "cl2000"
    other.parent.mkdir(parents=True)
    other.write_text("")
    other.chmod(0o755)
    assert doctor._find_cl2000(tmp_path, env={"TI_CGT": str(tmp_path / "ti")}) == str(other)   # TI_CGT wins


def test_json(no_tools, capsys):
    assert cli.main(["doctor", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["version"] == __version__ and d["required_ok"] is True
    assert {"name", "ok", "required", "version", "detail"} <= set(d["checks"][0])
    assert d["features"]["run / info / shell / serve"] is True
    assert os.path.isabs(d["root"])
