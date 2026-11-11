"""Boards: several chips on one virtual PCB, connected by nets (pin level) and links (peripheral byte level).

Board description (`boards/<name>.yaml`, paths relative to the board file)::

    name: two_k22_uart
    quantum: 10us                      # optional lockstep quantum (CLI --quantum overrides)
    auto_quantum: true                 # optional: sleeping chips do not hold the others back (chipem/perf.py)
    performance: fast                  # optional speed/fidelity preset: accurate | default | fast; max_slice: N
    chips:
      - {id: A, chip: mk22fn1m0vll12, firmware: ../firmware/samples/build/uart_link_sender.elf, clock_hz: 120e6}
      - {id: B, chip: mk22fn1m0vll12, firmware: ../firmware/samples/build/uart_link_echo.elf}
    nets:                              # electrical connections, any number of pins per net
      - {name: BTN, pins: [A.PTC6, B.PTC6], pull: up}      # optional net pull (up|down) / fixed driver (drive: H|L)
      - {name: SDA, pins: [A.PTB3, B.PB7], open_drain: true} # wired-AND bus (I2C): H drivers count as released, pull-up
    links:                             # byte-level peripheral links (fast path)
      - {type: uart, from: A.UART0, to: B.UART0}           # bidirectional unless dir: tx
      - {type: can, name: CANBUS, nodes: [A.CAN0, B.CAN0]}   # CAN bus (any number of nodes; short: {can: [..]})
      - {type: eth, name: LAN, nodes: [A.ETH], phy: {model: lan8742, addr: 0, aneg: 2ms},   # Ethernet segment
         host: {ip: 192.168.0.1, ping: 192.168.0.10, count: 3}, pcap: lan.pcap}            # (chipem/ethernet.py)
    devices:
      - {type: spi_flash, chip: A, spi: SPI0, cs: PTC4}     # same options as --spi-flash
      - {type: sram, chip: A, bus: XINTF6, size: 256K, width: 16}   # parallel SRAM on an external bus (chipem/devices)
      - {type: sd_card, chip: A, host: SDIO, size: 64M, image: card.img}   # SD card; same options as --sd-card
      - {type: sdram, chip: A, bus: FMC, bank: 2, size: 8M, width: 16}   # SDR SDRAM on an SDRAM controller (chipem/devices/sdram.py)
    parts:                             # board parts (chipem/parts): port -> net name, or CHIP.PIN (anonymous net)
      - {id: SW1, type: switch, level: H, ports: {out: BTN}}
      - {id: LED1, type: led, active: L, ports: {in: A.PTC5}}
    stimulus: ["A.PTC6=1", "10ms:B.PTC6=0", "5ms:SW1=L"]
    host_serial: [{uart: A.UART0, pty: /tmp/ttyK22}]   # PC software on a UART (chipem/hostserial.py; tcp: / port:)
    realtime: true                     # lock virtual time to the wall clock (default: on with a host_serial entry)

Runtime (`Board`):

* **Lockstep.** Every chip keeps its own Clock/Scheduler/EventLog. `Board.run_for(ns)` repeatedly picks the chip
  with the smallest `now_ns()` and runs it up to `min(target, other chips' earliest now + quantum)`, so no chip
  is ever more than one quantum ahead of another and cross-chip effects arrive with at most one quantum of latency.
* **Nets** join pins of several chips. Whenever a member pin's direction, drive or pull changes (PinBoard
  events) the net level is recomputed: driven outputs / the net's fixed driver / user stimulus (strong) win over
  the net pull, which wins over the member pins' own pulls; else X. Two strong drivers at different levels are a
  conflict: the net resolves to X and the board logs one `warn` per conflict episode. A pin pulled low by an
  open-drain peripheral output (`Pin.od_low`, I2C SDA/SCL) is a strong L driver; a released open-drain pin drives
  nothing, so I2C buses are wired-AND on any net. An `open_drain: true` net is wired-AND for every driver (an H
  drive counts as released: any L wins, else the pull) and is pulled up unless it says `pull: down`. A part port
  may carry a pull resistor (`Part.pull(port)`, e.g. the pull-ups of an I2C EEPROM breakout): weaker than the net's
  own `pull`, stronger than the member pins' pulls. The resolved level is
  written into every member pin's `external` (an output pin keeps showing its own drive). A chip that is behind
  the source chip in time gets the change scheduled at the source's timestamp; one that is ahead sees it at once.
* **UART links** hook the UART model's `tx_listeners`: a byte written to the sender's D is delivered to the
  peer's `receive()` one character time (sender's baud/framing) later, i.e. at its stop bit, scheduled on the
  peer's scheduler. Each byte arrives on its own; the receiver has no input queue: it takes the byte into its
  holding register/FIFO, or loses it (not listening, RX pin not muxed, overrun) exactly like silicon
  (chipem/periph/uartwire.py). A receiver whose chip has no pin offering its RX function gets nothing (no_pin).
  Bytes travel as frames with their format (baud, data bits, parity bit, stop bits, breaks); the receiver decodes
  them at its own settings by sample point (framing / parity / noise errors, garbage at a wrong baud, breaks for LIN
  break / BRKDT detection). A baud (> 3 %) or frame-length mismatch is also reported once per link as a `warn`.
* **CAN links** put the listed CAN controllers (`Chip.can_blocks()`: models with the `can_attach` hook) on one
  `chipem.canbus.CanBus` (`CanLink`): frames take their real bus time at the transmitter's bit rate, the lowest ID
  wins arbitration, a frame nobody acknowledges is an ACK error and is retried (see chipem/canbus.py). `host: true`
  adds an external node that acknowledges every frame (a PC CAN adapter); `CanLink.bus.inject(frame, t)` sends from
  it. Each completed frame is one `can` event in the transmitter's chip log.
* **Auto UART links from nets.** Peripheral functions do not drive pin levels in this emulator, so a net cannot
  carry UART bits. Instead, whenever a net joins a pin owned by a UART TX signal (`UART0_TX`, `USART1_TX`,
  `LPUART0_TX`, `SCITXDA`, ...) and a pin owned by a UART RX signal (`UART0_RX`, `USART1_RX`, `SCIRXDA`, ...),
  the board creates a one-way byte link TX block -> RX block (`UartLink.auto`, `UartLink.net`) and logs
  `info` "net X: auto UART link A.UART0 → B.USART1". Re-evaluated whenever a member pin's owner changes
  (firmware muxes pins at runtime) and when nets or explicit links change; a direction that an explicit
  `links:` entry already covers gets no auto link (no double delivery).
* **Runtime wiring.** `add_net` / `connect` / `remove_net` / `rename_net` / `add_link` / `remove_link` work at any
  time (web UI wire mode); `wiring` counts changes; `to_yaml()` exports the current board as a board YAML.
* **Parts** (switches, buttons, clocks, LEDs, probes, counters, gates, flip-flops; chipem/parts) sit on nets
  through named ports (`Net.ports`). A part output that drives a level is a strong driver like a push-pull
  pin; input ports see the resolved net level (`Part.on_input`). Combinational parts propagate with zero
  delay: one pin change or timer event resolves the affected nets in waves until nothing changes; a net
  re-resolved more than 16 times in one event is a combinational loop (one `warn`, propagation stops).
  Timed parts (pulse, pattern) use the board clock domain: the first chip's scheduler in wall-clock time
  (`sched_at_ns`), cancelled on `Board.reset`/`reset_parts`. Part state changes are `part` events in `board.log`.
* **Merged log.** `board.log` receives a copy of every chip event with a `chip` field, plus board-level events
  (`chip: "board"`). CLI/web/shell read it through the Session.
"""
from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .core import StopReason
from .log import EventLog

__all__ = ["BoardDesc", "BoardChipSpec", "NetSpec", "LinkSpec", "DeviceSpec", "PartSpec", "load_board", "Board",
           "Net", "UartLink", "CanLink", "split_qualified", "uart_signal", "parse_part_spec", "DEFAULT_QUANTUM_NS",
           "MAX_PROPAGATION"]

DEFAULT_QUANTUM_NS = 10_000.0           # 10 us
BAUD_TOLERANCE = 0.03                   # relative baud difference tolerated by a real UART receiver
MAX_PROPAGATION = 16                    # re-resolutions of one net per event before a combinational loop warn


# ------------------------------------------------------------------------------------------------ description
@dataclass
class BoardChipSpec:
    id: str
    chip: str                           # chip name or (resolved) path to a chip YAML
    firmware: str | None = None         # resolved path
    load_addr: int | None = None
    clock_hz: float | None = None
    vtor: int | None = None
    format: str | None = None


@dataclass
class NetSpec:
    name: str
    pins: list[tuple[str, str]]         # [(chip id, pin name)]
    pull: str | None = None             # "up" | "down" | None
    drive: str | None = None            # fixed driver "H" | "L" | None
    open_drain: bool = False            # wired-AND: H drivers count as released; pulled up unless pull: down


@dataclass
class LinkSpec:
    type: str                           # "uart" | "can"
    a: tuple[str, str]                  # (chip id, peripheral) of `from` (can: the first node)
    b: tuple[str, str]                  # (chip id, peripheral) of `to` (can: the second node, or the first again)
    bidir: bool = True
    nodes: list[tuple[str, str]] = field(default_factory=list)   # can: every node on the bus
    name: str | None = None             # can: bus name
    host: bool = False                  # can: add an acknowledging host node
    options: dict = field(default_factory=dict)   # eth: per-node PHY options, host, pcap, caps, inject


@dataclass
class DeviceSpec:
    type: str                           # "spi_flash" | "sram" | "sd_card" | "sdram"
    chip: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class PartSpec:
    id: str | None
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    ports: dict[str, str | None] = field(default_factory=dict)    # port -> net name | CHIP.PIN | None


@dataclass
class BoardDesc:
    name: str
    path: Path | None
    chips: list[BoardChipSpec]
    nets: list[NetSpec] = field(default_factory=list)
    links: list[LinkSpec] = field(default_factory=list)
    devices: list[DeviceSpec] = field(default_factory=list)
    stimulus: list[str] = field(default_factory=list)
    quantum_ns: float | None = None
    parts: list[PartSpec] = field(default_factory=list)
    host_serial: list[dict] = field(default_factory=list)   # [{uart, pty|port|tcp, ...}] (chipem/hostserial.py)
    realtime: Any = None                # None (auto: on with a host bridge) | bool | speed factor
    # speed / fidelity (chipem/perf.py): {"preset": ..., "auto_quantum": bool, "max_slice": int} from the YAML keys
    # `performance:`, `auto_quantum:`, `max_slice:` (CLI options override them)
    perf: dict = field(default_factory=dict)

    def chip_ids(self) -> list[str]:
        return [c.id for c in self.chips]


def split_qualified(name: str, ids, default: str | None = None) -> tuple[str | None, str]:
    """'B.PTC6' -> ('B', 'PTC6') when B is a chip id (case-insensitive); otherwise (default, name)."""
    s = str(name).strip()
    if "." in s:
        head, rest = s.split(".", 1)
        for cid in ids:
            if cid.upper() == head.strip().upper():
                return cid, rest.strip()
    return default, s


_SCI_SIG = re.compile(r"SCI(TX|RX)D?([A-Z])")                     # TI C2000: SCITXDA, SCIRXDB
_UART_SIG = re.compile(r"([A-Z0-9]*?(?:US?ART|SCI)[A-Z0-9]*?)_?(TX|RX)D?")   # UART0_TX, USART1_RX, LPUART0_TX


def uart_signal(owner: Any) -> tuple[str, str] | None:
    """Pin owner string -> (UART peripheral name as named by the signal, "tx" | "rx"), or None.

    'UART0_TX' -> ('UART0', 'tx'); 'USART1_RX' -> ('USART1', 'rx'); 'SCITXDA' -> ('SCIA', 'tx'); "PTA2/UART0_TX"
    works too (each "/" part is tried). Only TX/RX data signals count (not RTS/CTS)."""
    if not owner:
        return None
    for part in str(owner).upper().split("/"):
        part = part.strip()
        m = _SCI_SIG.fullmatch(part)
        if m:
            return f"SCI{m.group(2)}", m.group(1).lower()
        m = _UART_SIG.fullmatch(part)
        if m:
            return m.group(1), m.group(2).lower()
    return None


def _num(v: Any, what: str) -> int | float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    s = str(v).strip()
    try:
        return int(s, 0)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            raise ValueError(f"board: bad {what} {v!r}") from None


def _resolve_path(p: str | None, base: Path | None) -> str | None:
    """Relative to the board file; falls back to the current directory if only that exists."""
    if p is None or p == "":
        return None
    path = Path(str(p)).expanduser()
    if path.is_absolute() or base is None:
        return str(path)
    cand = Path(os.path.normpath(base / path))
    if cand.exists() or not path.exists():
        return str(cand)
    return str(path)


def _chip_ref(name: str, base: Path | None) -> str:
    s = str(name)
    if s.endswith((".yaml", ".yml")):
        return _resolve_path(s, base) or s
    return s


def _qual(spec: Any, ids: list[str], what: str) -> tuple[str, str]:
    cid, rest = split_qualified(str(spec), ids)
    if cid is None or not rest:
        raise ValueError(f"board: {what} {spec!r} must be qualified with a chip id (one of {', '.join(ids)}), "
                         f"e.g. {ids[0]}.PTC6")
    return cid, rest


def load_board(path: str | Path) -> BoardDesc:
    """Parse a board YAML (see module docstring)."""
    import yaml
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"board description not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    return parse_board(data, base=path.parent, path=path)


def parse_board(data: dict, *, base: Path | None = None, path: Path | None = None) -> BoardDesc:
    if not isinstance(data, dict):
        raise ValueError("board: top level must be a mapping")
    from .session import parse_level, parse_time
    raw_chips = data.get("chips") or []
    if not raw_chips:
        raise ValueError("board: no chips")
    chips: list[BoardChipSpec] = []
    for i, c in enumerate(raw_chips):
        if not isinstance(c, dict):
            raise ValueError(f"board: chip entry {i} must be a mapping")
        cid = str(c.get("id") or chr(ord("A") + i))
        if "." in cid or not cid.strip():
            raise ValueError(f"board: bad chip id {cid!r} (no dots)")
        if any(x.id.upper() == cid.upper() for x in chips):
            raise ValueError(f"board: duplicate chip id {cid!r}")
        hz = _num(c.get("clock_hz"), "clock_hz")
        chips.append(BoardChipSpec(id=cid, chip=_chip_ref(c.get("chip") or "mk22fn1m0vll12", base),
                                   firmware=_resolve_path(c.get("firmware"), base),
                                   load_addr=_num(c.get("load_addr"), "load_addr"),
                                   clock_hz=float(hz) if hz else None, vtor=_num(c.get("vtor"), "vtor"),
                                   format=c.get("format")))
    ids = [c.id for c in chips]

    nets: list[NetSpec] = []
    seen: dict[tuple[str, str], str] = {}
    for i, n in enumerate(data.get("nets") or []):
        if not isinstance(n, dict) or n.get("pins") is None or not isinstance(n.get("pins"), list) or \
                (not n["pins"] and n.get("pull") is None and n.get("drive") is None):
            raise ValueError(f"board: net {i} needs a `pins` list")
        name = str(n.get("name") or f"NET{i}")
        pins = [_qual(p, ids, f"net {name} pin") for p in n["pins"]]
        for cp in pins:
            key = (cp[0], cp[1].upper())
            if key in seen:
                raise ValueError(f"board: pin {cp[0]}.{cp[1]} is in nets {seen[key]} and {name}")
            seen[key] = name
        pull = n.get("pull")
        if pull is not None:
            pull = str(pull).lower()
            if pull not in ("up", "down"):
                raise ValueError(f"board: net {name}: pull must be up|down")
        drive = n.get("drive")
        drive = parse_level(drive) if drive is not None else None
        od = n.get("open_drain", False)
        if not isinstance(od, bool):
            od = str(od).strip().lower() in ("1", "true", "yes", "on")
        nets.append(NetSpec(name=name, pins=pins, pull=pull, drive=drive, open_drain=od))

    links: list[LinkSpec] = []
    for i, ln in enumerate(data.get("links") or []):
        if not isinstance(ln, dict):
            raise ValueError(f"board: link {i} must be a mapping")
        typ = str(ln.get("type", "can" if "can" in ln else "uart")).lower()
        if typ == "can":
            eps = ln.get("nodes", ln.get("can"))
            if eps is None and ln.get("from") is not None:
                eps = [ln.get("from"), ln.get("to")]
            if not isinstance(eps, list) or not eps or any(e is None for e in eps):
                raise ValueError(f"board: link {i}: a can link needs `nodes: [CHIP.CANx, ...]`")
            nodes = [_qual(e, ids, "can node") for e in eps]
            nodes = [(c, p.upper()) for c, p in nodes]
            links.append(LinkSpec(type="can", a=nodes[0], b=nodes[-1], nodes=nodes,
                                  name=str(ln["name"]) if ln.get("name") else None, host=bool(ln.get("host", False))))
            continue
        if typ in ("eth", "ethernet"):
            from .ethernet import phy_options
            eps = ln.get("nodes", ln.get("eth"))
            if not isinstance(eps, list) or not eps:
                raise ValueError(f"board: link {i}: an eth link needs `nodes: [CHIP.ETH, ...]`")
            default_phy = phy_options(ln.get("phy"), f"board: link {i}")
            enodes, phys = [], []
            for e in eps:
                opts = dict(default_phy)
                if isinstance(e, dict):
                    opts.update(phy_options(e.get("phy"), f"board: link {i}"))
                    e = e.get("node")
                c, p = _qual(e, ids, "eth node")
                enodes.append((c, p))
                phys.append(opts)
            links.append(LinkSpec(type="eth", a=enodes[0], b=enodes[-1], nodes=enodes,
                                  name=str(ln["name"]) if ln.get("name") else None,
                                  options={"phy": phys, "host": ln.get("host"),
                                           "pcap": str(Path(base or ".") / ln["pcap"]) if ln.get("pcap") else None,
                                           "caps": ln.get("caps"), "inject": ln.get("inject")}))
            continue
        if typ == "usb":                                         # chipem/usbbus.py: device port -> host
            from .usbbus import host_options
            dev = ln.get("device", ln.get("usb"))
            if dev is None:
                raise ValueError(f"board: link {i}: a usb link needs `device: CHIP.PORT` (e.g. A.OTG_FS)")
            c, p = _qual(dev, ids, "usb device")
            hopts = ln.get("host")
            links.append(LinkSpec(type="usb", a=(c, p.upper()), b=(c, p.upper()), nodes=[(c, p.upper())],
                                  name=str(ln["name"]) if ln.get("name") else None,
                                  options=host_options(hopts if isinstance(hopts, dict) else {}, f"board: link {i}")))
            continue
        if typ != "uart":
            raise ValueError(f"board: link {i}: unsupported type {typ!r} (supported: uart, can, eth, usb)")
        a = _qual(ln.get("from"), ids, "link endpoint")
        b = _qual(ln.get("to"), ids, "link endpoint")
        d = str(ln.get("dir", "both")).lower()
        if d not in ("both", "tx", "bidir"):
            raise ValueError(f"board: link {i}: dir must be both|tx")
        links.append(LinkSpec(type=typ, a=(a[0], a[1].upper()), b=(b[0], b[1].upper()), bidir=d != "tx"))

    devices: list[DeviceSpec] = []
    for i, dv in enumerate(data.get("devices") or []):
        if not isinstance(dv, dict):
            raise ValueError(f"board: device {i} must be a mapping")
        typ = str(dv.get("type", "")).lower()
        if typ not in ("spi_flash", "sram", "sd_card", "sdram"):
            raise ValueError(f"board: device {i}: unsupported type {typ!r} (supported: spi_flash, sram, sd_card, "
                             "sdram)")
        cid, _ = split_qualified(f"{dv.get('chip', ids[0])}.x", ids)
        if cid is None:
            raise ValueError(f"board: device {i}: unknown chip {dv.get('chip')!r}")
        opts = {k: v for k, v in dv.items() if k not in ("type", "chip")}
        if opts.get("file"):
            opts["file"] = _resolve_path(opts["file"], base)
        for k in ("image", "save"):                 # sd_card image / write-back file
            if opts.get(k):
                opts[k] = _resolve_path(opts[k], base)
        devices.append(DeviceSpec(type=typ, chip=cid, options=opts))

    parts: list[PartSpec] = []
    from .parts import registry
    for i, pd in enumerate(data.get("parts") or []):
        if not isinstance(pd, dict):
            raise ValueError(f"board: part {i} must be a mapping")
        typ = str(pd.get("type") or "").strip().lower()
        if not registry.is_type(typ):
            raise ValueError(f"board: part {i}: unknown type {pd.get('type')!r} (known: {', '.join(registry.types())})")
        pid = pd.get("id")
        pid = str(pid).strip() if pid is not None else None
        if pid is not None and (not pid or "." in pid or "=" in pid or ":" in pid):
            raise ValueError(f"board: part {i}: bad id {pid!r} (no dots, colons or '=')")
        if pid is not None and any(x.id and x.id.upper() == pid.upper() for x in parts):
            raise ValueError(f"board: duplicate part id {pid!r}")
        ports = pd.get("ports") or {}
        if not isinstance(ports, dict):
            raise ValueError(f"board: part {pid or i}: `ports` must be a mapping port -> net or CHIP.PIN")
        params = {k: v for k, v in pd.items() if k not in ("id", "type", "ports", "state")}
        pcls = registry.get(typ)
        for k in getattr(pcls, "PATH_PARAMS", ()):     # file parameters (script parts): relative to the board file
            if params.get(k):
                params[k] = str(Path(_resolve_path(params[k], base)).resolve())
        if getattr(pcls, "PATH_PARAMS", ()) and base is not None:
            params["_board_dir"] = str(Path(base).resolve())
        parts.append(PartSpec(id=pid, type=typ, params=params,
                              ports={str(k): (str(v) if v is not None else None) for k, v in ports.items()}))

    stim = data.get("stimulus") or []
    if isinstance(stim, str):
        stim = [stim]
    q = data.get("quantum")
    quantum = parse_time(q) if q is not None else None
    perf: dict[str, Any] = {}
    if data.get("performance") is not None:
        pname = str(data["performance"]).strip().lower()
        if pname not in ("accurate", "default", "fast"):
            raise ValueError(f"board: performance must be accurate, default or fast, not {data['performance']!r}")
        perf["preset"] = pname
    if data.get("auto_quantum") is not None:
        perf["auto_quantum"] = bool(data["auto_quantum"])
    if data.get("max_slice") is not None:
        perf["max_slice"] = int(data["max_slice"])
    hs = data.get("host_serial") or []
    if isinstance(hs, dict):
        hs = [hs]
    if not isinstance(hs, list) or not all(isinstance(h, dict) and h.get("uart") for h in hs):
        raise ValueError("board: host_serial entries must be mappings with `uart` and one of pty/port/tcp")
    return BoardDesc(name=str(data.get("name") or (path.stem if path else "board")), path=path, chips=chips,
                     nets=nets, links=links, devices=devices, stimulus=[str(s) for s in stim], quantum_ns=quantum,
                     parts=parts, host_serial=[dict(h) for h in hs], realtime=data.get("realtime"), perf=perf)


def parse_part_spec(spec: str) -> PartSpec:
    """CLI `--part` syntax: 'ID:TYPE[:KEY=VALUE,...]' or 'TYPE[:KEY=VALUE,...]' (id auto-assigned). KEYs that are
    port names of the type connect that port (to a net name or a pin, e.g. in=PTC5 or in=B.PTC5); other KEYs
    are parameters. Example: 'LED1:led:in=PTC5,active=L', 'CLK:pulse:out=PTC6,period=2ms', 'and:in0=A,in1=B,out=Y'."""
    from .parts import registry
    s = str(spec).strip()
    head = s.split(":", 2)
    if registry.is_type(head[0]) and not (len(head) > 1 and registry.is_type(head[1])):
        pid, typ, rest = None, head[0], s.split(":", 1)[1] if ":" in s else ""
    elif len(head) >= 2:
        pid, typ, rest = head[0].strip() or None, head[1], head[2] if len(head) > 2 else ""
    else:
        raise ValueError(f"bad --part {spec!r} (expected ID:TYPE[:KEY=VALUE,...], e.g. LED1:led:in=PTC5)")
    if not registry.is_type(typ):
        raise ValueError(f"bad --part {spec!r}: unknown type {typ!r} (known: {', '.join(registry.types())})")
    if pid is not None and ("." in pid or "=" in pid):
        raise ValueError(f"bad --part {spec!r}: bad id {pid!r}")
    cls = registry.get(typ)
    kv: dict[str, str] = {}
    for item in [x for x in rest.split(",") if x.strip()]:
        if "=" not in item:
            raise ValueError(f"bad --part {spec!r}: {item!r} is not KEY=VALUE")
        k, v = item.split("=", 1)
        kv[k.strip()] = v.strip()
    params = {k: v for k, v in kv.items()}
    port_names = cls.port_descs({**cls.default_params(), **{k: v for k, v in params.items()
                                                             if k in cls.default_params()}})
    ports = {k: (None if v.lower() in ("", "none", "-") else v) for k, v in kv.items() if k in port_names}
    params = {k: v for k, v in kv.items() if k not in port_names}
    return PartSpec(id=pid, type=typ.lower(), params=params, ports=ports)


def _ep(ep: tuple[str, str]) -> str:
    return f"{ep[0]}.{ep[1]}"


def spi_flash_spec(dev: DeviceSpec) -> str:
    """DeviceSpec -> the --spi-flash string understood by chipem.devices.spi_flash.attach_from_spec."""
    o = dict(dev.options)
    spi = str(o.pop("spi", "SPI0")).upper()
    parts = [f"{k}={v}" for k, v in o.items() if v is not None]
    return f"{spi}:{','.join(parts)}" if parts else spi


def device_spec(dev: DeviceSpec, key: str, default: str) -> str:
    """DeviceSpec -> "PERIPH:k=v,..." with PERIPH from option `key` (e.g. sram: bus=XINTF6)."""
    o = dict(dev.options)
    per = str(o.pop(key, default)).upper()
    parts = [f"{k}={v}" for k, v in o.items() if v is not None]
    return f"{per}:{','.join(parts)}" if parts else per


# ------------------------------------------------------------------------------------------------ nets
class Net:
    """Pins of several chips joined by a wire. See the module docstring for the resolution rules."""

    def __init__(self, board: "Board", name: str, members: list[tuple[str, Any, Any]], *, pull: str | None = None,
                 drive: str | None = None, open_drain: bool = False):
        self.board = board
        self.name = name
        self.members = members              # [(chip id, Chip, Pin)]
        self.pull = pull
        self.drive = drive                  # fixed driver on the net
        self.open_drain = bool(open_drain)  # wired-AND net (I2C): H drivers count as released
        self.stimulus: str | None = None    # user stimulus applied to any member pin (strong)
        self.ports: list[tuple[Any, str]] = []   # part endpoints [(Part, port name)]
        self.anon = False                   # created implicitly for a part port wired to a chip pin
        self.by_part = False                # created by connect_port (dropped again once unused)
        self.level: str = "X"
        self.conflict = False
        self.conflicts = 0                  # number of conflict episodes so far
        self._busy = False
        self._dirty = False
        # member index -> deferred deliveries [(t_ns, Event)] in time order: a chip behind the source receives
        # every level change at its source time, so a pulse shorter than the board quantum is not lost
        self._pending: dict[int, list[tuple[float, Any]]] = {}

    def label(self, i: int) -> str:
        cid, _chip, pin = self.members[i]
        return f"{cid}.{pin.id}"

    def drivers(self) -> list[tuple[str, str]]:
        """[(who, level)] of the strong drivers."""
        out = []
        for i, (_cid, _chip, p) in enumerate(self.members):
            if p.fixed is not None:
                out.append((self.label(i), p.fixed))
            elif p.od_low:                  # pulled low by an open-drain peripheral output (I2C)
                out.append((self.label(i), "L"))
            elif p.direction == "out" and p.drive is not None:
                # an output without a modelled drive (a peripheral function such as SCITXDA) does not drive
                out.append((self.label(i), p.drive))
        for part, port in self.ports:
            if part.ports[port].is_output:
                lvl = part.drive(port)
                if lvl is not None:
                    out.append((f"{part.id}.{port}", lvl))
        if self.drive is not None:
            out.append((f"net {self.name} driver", self.drive))
        if self.stimulus is not None:
            out.append(("stimulus", self.stimulus))
        return out

    def resolve(self) -> tuple[str, bool]:
        """(level, conflict)."""
        vals = {lvl for _who, lvl in self.drivers()}
        if self.open_drain and vals:
            if "L" in vals:
                return "L", False           # wired-AND: any low driver wins
            if "X" in vals:
                return "X", False
            vals = set()                    # H drivers are released outputs: the pull decides
        if vals:
            if len(vals) == 1:
                return next(iter(vals)), False
            return "X", ("H" in vals and "L" in vals)
        if self.pull is not None:
            return ("H" if self.pull == "up" else "L"), False
        if self.ports:                      # pull resistors on part ports (I2C device breakout pull-ups)
            pp = {pl for pl in (part.pull(port) for part, port in self.ports) if pl}
            if len(pp) == 1:
                return ("H" if pp.pop() == "up" else "L"), False
        if self.open_drain:
            return "H", False               # an open-drain bus is pulled up unless the net says pull: down
        pulls = {p.pull for _c, _ch, p in self.members if p.pull}
        if len(pulls) == 1:
            return ("H" if pulls.pop() == "up" else "L"), False
        return "X", False

    def update(self, src_chip: Any = None) -> None:
        """Re-resolve the level and deliver it (member pins, part input ports; see Board._propagate)."""
        self.board._propagate(self, src_chip)

    def _update_once(self, src_chip: Any) -> None:
        lvl, conflict = self.resolve()
        t = src_chip.now_ns() if src_chip is not None else None
        if conflict and not self.conflict:
            self.conflicts += 1
            who = ", ".join(f"{w}={v}" for w, v in self.drivers())
            self.board.warn(f"net {self.name}: drive conflict ({who}) -> X", t_ns=t, net=self.name)
        self.conflict = conflict
        self.level = lvl
        for i, (_cid, chip, pin) in enumerate(self.members):
            q = self._pending.get(i)
            if t is None or chip is src_chip or chip.now_ns() >= t:
                if q:
                    now = chip.now_ns()
                    while q and q[0][0] <= now:             # overdue: deliver in order
                        _tq, ev = q.pop(0)
                        chip.sched.cancel(ev)
                        if pin.external != ev.args[1]:
                            chip.pins.set_external(pin, ev.args[1])
                    if q:
                        # deliveries from a source further ahead are still queued: `lvl` reflects that source's
                        # later state, so it belongs at the last queued time, not at this chip's (earlier) now
                        tq, ev = q[-1]
                        if ev.args[1] != lvl:
                            chip.sched.cancel(ev)
                            q[-1] = (tq, chip.sched.at_ns(tq, self._apply, i, lvl))
                        continue
                    del self._pending[i]
                if pin.external != lvl:
                    chip.pins.set_external(pin, lvl)
            else:                           # this chip is behind the source: deliver at the source's time
                if q:                       # a resolution at t replaces what was queued for t or later only
                    while q and q[-1][0] >= t:
                        chip.sched.cancel(q.pop()[1])
                else:
                    q = self._pending[i] = []
                q.append((t, chip.sched.at_ns(t, self._apply, i, lvl)))
        if self.ports:
            tp = t if t is not None else self.board.part_now_ns()
            for part, port in list(self.ports):
                if part.ports[port].is_input and part.inputs.get(port) != lvl:
                    part.inputs[port] = lvl
                    part.on_input(port, lvl, tp)

    def cancel_pending(self, i: int) -> None:
        """Drop every deferred delivery to member i."""
        q = self._pending.pop(i, None)
        if q:
            chip = self.members[i][1]
            for _t, ev in q:
                chip.sched.cancel(ev)

    def _apply(self, i: int, lvl: str) -> None:
        q = self._pending.get(i)
        if q:
            q.pop(0)                        # deliveries fire in time order: this is the oldest
            if not q:
                del self._pending[i]
        _cid, chip, pin = self.members[i]
        if pin.external != lvl:
            chip.pins.set_external(pin, lvl)


# ------------------------------------------------------------------------------------------------ links
class UartLink:
    """UART connection between two UART models. Models with `frame_listeners` / `receive_frame` exchange frames
    (chipem/periph/uartwire.py: the transmitter's baud, data bits, parity bit, stop bits, breaks; the receiver decodes
    them at its own settings); others fall back to bytes (tx_listeners / receive). Delivery is at the frame's stop-bit
    end and the receiver decides whether it is taken or dropped."""

    def __init__(self, board: "Board", a: tuple[str, str], b: tuple[str, str], *, bidir: bool = True):
        self.board = board
        self.a, self.b = a, b
        self.bidir = bidir
        self.blk_a = board.peripheral(*a)
        self.blk_b = board.peripheral(*b)
        for (cid, name), blk in ((a, self.blk_a), (b, self.blk_b)):
            if not hasattr(blk, "tx_listeners") or not hasattr(blk, "receive"):
                raise ValueError(f"board: {cid}.{name} is not a UART model with link hooks "
                                 f"(model {getattr(blk, 'model', '?')})")
        self.chip_a, self.chip_b = board.chips[a[0]], board.chips[b[0]]
        self.bytes_ab = 0
        self.bytes_ba = 0
        self.warned = False
        self.auto = False                   # created from a net (TX pin and RX pin on the same net)
        self.net: str | None = None         # that net's name
        self._last_rx: dict[int, float] = {}    # id(receiver block) -> arrival time of its last byte (ns)
        self._hooks = []
        for blk, cb in ((self.blk_a, self._a_tx), (self.blk_b, self._b_tx))[:2 if bidir else 1]:
            lst = getattr(blk, "frame_listeners", None)
            lst = lst if lst is not None else blk.tx_listeners
            lst.append(cb)
            self._hooks.append((lst, cb))

    def describe(self) -> str:
        arrow = "<->" if self.bidir else "->"
        return f"{self.a[0]}.{self.a[1]} {arrow} {self.b[0]}.{self.b[1]}"

    def covers(self, src: tuple[str, str], dst: tuple[str, str]) -> bool:
        """Does this link carry bytes from src to dst?"""
        return (self.a, self.b) == (src, dst) or (self.bidir and (self.b, self.a) == (src, dst))

    def to_dict(self) -> dict:
        return {"type": "uart", "from": f"{self.a[0]}.{self.a[1]}", "to": f"{self.b[0]}.{self.b[1]}",
                "bidir": self.bidir, "auto": self.auto, "net": self.net, "describe": self.describe(),
                "a_to_b": self.bytes_ab, "b_to_a": self.bytes_ba}

    def close(self) -> None:
        for lst, cb in self._hooks:
            if cb in lst:
                lst.remove(cb)
        self._hooks = []

    def _a_tx(self, blk, x) -> None:
        if isinstance(x, int) or not x.brk:
            self.bytes_ab += 1
        self._send(self.chip_a, blk, self.chip_b, self.blk_b, x)

    def _b_tx(self, blk, x) -> None:
        if isinstance(x, int) or not x.brk:
            self.bytes_ba += 1
        self._send(self.chip_b, blk, self.chip_a, self.blk_a, x)

    def _send(self, src_chip, src, dst_chip, dst, x) -> None:
        """`x`: a Frame (frame_listeners) or a byte (tx_listeners)."""
        self._check(src_chip, src, dst)
        fr = None if isinstance(x, int) else x
        if fr is not None and fr.baud > 0:
            char = fr.duration_ns()
        else:
            char = src.char_ns() if hasattr(src, "char_ns") else 0.0
        t = src_chip.now_ns() + char
        # a receiver ahead of the sender cannot get the byte at its stop bit any more; it gets it now, but never
        # sooner than one character after the previous byte (back-to-back bytes must not arrive together)
        last = self._last_rx.get(id(dst))
        if last is not None and t < last + char:
            t = last + char
        rf = getattr(dst, "receive_frame", None) if fr is not None else None
        if rf is not None:
            fn, args = rf, (fr, t)
        elif fr is not None and fr.brk:
            return                          # a byte-level receiver cannot see a break
        else:
            fn, args = dst.receive, (bytes([(fr.data if fr is not None else x) & 0xFF]),)
        now = dst_chip.now_ns()
        if now >= t:
            self._last_rx[id(dst)] = now
            fn(*args)
        else:
            self._last_rx[id(dst)] = t
            dst_chip.sched.at_ns(t, fn, *args)

    def _check(self, src_chip, src, dst) -> None:
        if self.warned:
            return
        try:
            bs, bd = float(src.baud()), float(dst.baud())
            fs, fd = src.char_bits(), dst.char_bits()
        except Exception:
            return
        if bs <= 0 or bd <= 0:
            return                      # one side has no baud generator yet: nothing to compare
        if abs(bs - bd) / max(bs, bd) > BAUD_TOLERANCE or fs != fd:
            self.warned = True
            what = f"baud mismatch {bs:.0f} vs {bd:.0f}" if fs == fd else f"frame mismatch {fs} vs {fd} bits"
            self.board.warn(f"uart link {self.describe()}: {what}; the receiver decodes at its own settings "
                            f"(framing / parity errors, garbage)", t_ns=src_chip.now_ns(), link=self.describe())



class CanLink:
    """A CAN bus (chipem.canbus.CanBus) joining CAN controllers of the board's chips (`Chip.can_blocks()`)."""

    def __init__(self, board: "Board", name: str, nodes: list[tuple[str, str]], *, host: bool = False):
        from .canbus import CanBus
        self.board = board
        self.name = name
        self.nodes = list(nodes)
        self.bus = CanBus(name)
        self.blocks = []
        for cid, pname in self.nodes:
            blk = board.peripheral(cid, pname)
            if not hasattr(blk, "can_attach"):
                raise ValueError(f"board: {cid}.{pname} is not a CAN controller model (model "
                                 f"{getattr(blk, 'model', '?')})")
            self.blocks.append(blk)
        for blk in self.blocks:
            blk.can_attach(self.bus)
        self.host = self.bus.add_host(chip=board.clock_chip) if host else None

    def describe(self) -> str:
        return f"{self.name}: " + ", ".join(f"{c}.{p}" for c, p in self.nodes) + (" + host" if self.host else "")

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"type": "can", "name": self.name, "nodes": [f"{c}.{p}" for c, p in self.nodes]}
        if self.host is not None:
            d["host"] = True
        return d

    def close(self) -> None:
        for blk in self.blocks:
            blk.can_detach()
        self.blocks = []


# ------------------------------------------------------------------------------------------------ runtime
class Board:
    """N chips in lockstep plus nets and links. `chips` maps chip id -> Chip (order = board order)."""

    def __init__(self, chips: dict[str, Any], *, quantum_ns: float | None = None, name: str = "board",
                 log: EventLog | None = None):
        if not chips:
            raise ValueError("board: no chips")
        self.name = name
        self.chips: dict[str, Any] = dict(chips)
        self.quantum_ns = float(quantum_ns) if quantum_ns else DEFAULT_QUANTUM_NS
        self.auto_quantum = False                   # set_auto_quantum(): sleeping chips do not hold the others back
        self.chip_wall: dict[str, float] = {}       # wall seconds spent in each chip's run_for (speed meter)
        self.log = log if log is not None else EventLog()
        self.nets: list[Net] = []
        self.links: list[UartLink] = []
        self.can_links: list[CanLink] = []      # CAN buses (chipem.canbus)
        self.eth_links: list = []               # Ethernet segments (chipem.ethernet.EthLink)
        self._net_of: dict[tuple[str, str], Net] = {}
        self._auto: dict[str, dict[tuple[tuple[str, str], tuple[str, str]], UartLink]] = {}   # net -> links
        self.wiring = 0                     # bumped on every net/link change (UIs redraw the wiring)
        self.desc: BoardDesc | None = None  # set by Session.from_board: chip specs/devices/stimulus for to_yaml
        self._stop = False
        self._current: Any = None
        self.last_stop_chip: str | None = None
        self._unsubs: list[Callable[[], None]] = []
        # parts (chipem/parts): board clock domain = the first chip's scheduler (wall-clock ns)
        self.parts: dict[str, Any] = {}
        self._port_net: dict[tuple[str, str], Net] = {}
        self.clock_chip: Any = next(iter(self.chips.values()))
        self._timed: set = set()                    # live board-clock events (timed parts, scheduled stimulus):
                                                    # run_for stops every chip at the next one (sync point)
        self._wave: list[Net] | None = None         # nets queued for the next propagation wave
        self._src: Any = None                       # chip whose event is being propagated
        self._loop_warned: set = set()
        # merged log: copy what the chips logged so far (time order), then mirror live
        backlog = []
        for cid, chip in self.chips.items():
            for ev in list(getattr(chip.log, "_events", []) or []):
                backlog.append((ev.t_ns, ev.seq, cid, ev))
        # (chip id, seq in the chip's log, seq in the merged log) of each copied event: a front end that already
        # showed the chip's log up to seq N continues the merged log after the copy of N (web: first part added)
        self.backlog_seqs: list[tuple[str, int, int]] = []
        for _t, _s, cid, ev in sorted(backlog, key=lambda x: (x[0], x[1])):
            self._mirror(cid, ev)
            self.backlog_seqs.append((cid, ev.seq, getattr(self.log, "_seq", 0)))
        for cid, chip in self.chips.items():
            if hasattr(chip.log, "subscribe"):
                self._unsubs.append(chip.log.subscribe(lambda ev, _c=cid: self._mirror(_c, ev)))
            if chip.pins is not None:
                self._unsubs.append(chip.pins.subscribe(lambda ev, _c=cid, _chip=chip: self._on_pin(_c, _chip, ev)))

    # ---------------------------------------------------------------- log
    def _mirror(self, cid: str, ev: Any) -> None:
        fields = {k: v for k, v in ev.fields.items() if k not in ("kind", "t_ns", "chip")}
        self.log.event(ev.kind, ev.t_ns, chip=cid, **fields)

    def warn(self, msg: str, *, t_ns: float | None = None, **fields: Any) -> None:
        self.log.event("warn", self.now_ns() if t_ns is None else t_ns, chip="board", msg=msg, **fields)

    def info(self, msg: str, *, t_ns: float | None = None, **fields: Any) -> None:
        self.log.event("info", self.now_ns() if t_ns is None else t_ns, chip="board", msg=msg, **fields)

    # ---------------------------------------------------------------- lookup
    def ids(self) -> list[str]:
        return list(self.chips)

    def chip_id(self, name: str) -> str:
        for cid in self.chips:
            if cid.upper() == str(name).strip().upper():
                return cid
        raise ValueError(f"unknown chip id {name!r} (board chips: {', '.join(self.chips)})")

    def peripheral(self, cid: str, name: str) -> Any:
        chip = self.chips[self.chip_id(cid)]
        blk = chip.peripherals.get(str(name).upper())
        if blk is None:
            raise ValueError(f"board: chip {cid} has no peripheral {name!r}")
        return blk

    def net_of(self, cid: str, pin: Any) -> Net | None:
        chip = self.chips[cid]
        try:
            p = chip.pins.get(pin)
        except KeyError:
            return None
        return self._net_of.get((cid, p.id))

    # ---------------------------------------------------------------- construction / runtime wiring
    def net(self, name: str) -> Net:
        for n in self.nets:
            if n.name == name:
                return n
        for n in self.nets:
            if n.name.upper() == str(name).upper():
                return n
        raise ValueError(f"board: no net {name!r}")

    def _net_name(self) -> str:
        names = {n.name for n in self.nets}
        i = len(self.nets)
        while f"NET{i}" in names:
            i += 1
        return f"NET{i}"

    def _members(self, name: str, pins: list[tuple[str, str]], *, allow: Net | None = None) -> list:
        members = []
        for cid, pname in pins:
            cid = self.chip_id(cid)
            chip = self.chips[cid]
            try:
                pin = chip.pins.get(pname)
            except KeyError:
                raise ValueError(f"board: net {name}: chip {cid} has no pin {pname!r}") from None
            other = self._net_of.get((cid, pin.id))
            if other is not None and other is not allow:
                raise ValueError(f"board: pin {cid}.{pin.id} is already in net {other.name}")
            if other is None and all(m[2] is not pin or m[0] != cid for m in members):
                members.append((cid, chip, pin))
        return members

    def add_net(self, name: str | None, pins: list[tuple[str, str]], *, pull: str | None = None,
                drive: str | None = None, extend: bool = False, open_drain: bool | None = None) -> Net:
        """Join pins with a wire (same rules for YAML nets and nets created at runtime). `name=None` picks a free
        NETn name. An existing name is an error unless `extend` (then the pins are added to that net)."""
        if pull is not None and pull not in ("up", "down"):
            raise ValueError("board: net pull must be up|down")
        if name is not None and any(n.name == name for n in self.nets):
            if not extend:
                raise ValueError(f"board: net {name} already exists")
            net = self.net(name)
            members = self._members(name, pins, allow=net)
            for m in members:
                net.members.append(m)
                self._net_of[(m[0], m[2].id)] = net
            if members:                      # the user wired more pins onto it: no longer a part's implicit net
                net.anon = net.by_part = False
            if pull is not None:
                net.pull = pull
            if drive is not None:
                net.drive = drive
            if open_drain is not None:
                net.open_drain = bool(open_drain)
            self._changed()
            net.update()
            self._refresh_auto(net)
            return net
        name = name or self._net_name()
        if not str(name).strip():
            raise ValueError("board: empty net name")
        members = self._members(name, pins)
        net = Net(self, name, members, pull=pull, drive=drive, open_drain=bool(open_drain))
        for cid, _chip, pin in members:
            self._net_of[(cid, pin.id)] = net
        self.nets.append(net)
        self._changed()
        net.update()
        self._refresh_auto(net)
        return net

    def connect(self, pins: list[tuple[str, str]], *, name: str | None = None, pull: str | None = None) -> Net:
        """Interactive wiring: join `pins`. No member in a net -> a new net (`name` or NETn); members of one net ->
        the other pins are added to it; members of several nets -> those nets are merged into the first."""
        found: list[Net] = []
        for cid, pname in pins:
            n = self.net_of(self.chip_id(cid), pname)
            if n is not None and n not in found:
                found.append(n)
        if not found:
            return self.add_net(name, pins, pull=pull)
        keep = found[0]
        extra = [(cid, p.id) for other in found[1:] for cid, _c, p in other.members]
        for other in found[1:]:
            if keep.pull is None and other.pull is not None:
                keep.pull = other.pull
            if keep.drive is None and other.drive is not None:
                keep.drive = other.drive
            keep.open_drain = keep.open_drain or other.open_drain
            if keep.stimulus is None and other.stimulus is not None:
                keep.stimulus = other.stimulus
            for part, port in other.ports:          # part endpoints move to the merged net
                keep.ports.append((part, port))
                self._port_net[(part.id, port)] = keep
            other.ports = []
            self.remove_net(other.name, float_pins=False)   # its pins join `keep` at once: no glitch to open
        if len(found) > 1:
            keep.anon = keep.by_part = False
        net = self.add_net(keep.name, list(pins) + extra, pull=pull, extend=True)
        if name and name != net.name:
            self.rename_net(net.name, name)
        return net

    def remove_net(self, name: str, *, float_pins: bool = True) -> None:
        """Remove a net: its pins float again (external = open) and its auto UART links go away. `float_pins=False`
        (merging into another net) leaves the pins' levels for the caller to re-resolve."""
        net = self.net(name)
        for i, (cid, chip, pin) in enumerate(net.members):
            net.cancel_pending(i)
            self._net_of.pop((cid, pin.id), None)
        ports, net.ports = net.ports, []
        for part, port in ports:
            self._port_net.pop((part.id, port), None)
            self._port_floating(part, port)
        self.nets.remove(net)
        self._drop_auto(net.name)
        self._changed()
        if float_pins:
            for _cid, chip, pin in net.members:
                if pin.external is not None:
                    chip.pins.set_external(pin, None)
        self._refresh_auto()                # an auto link this net suppressed? (none today; cheap)

    def rename_net(self, old: str, new: str) -> Net:
        new = str(new).strip()
        if not new:
            raise ValueError("board: empty net name")
        net = self.net(old)
        if new != net.name and any(n.name == new for n in self.nets):
            raise ValueError(f"board: net {new} already exists")
        links = self._auto.pop(net.name, {})
        for ln in links.values():
            ln.net = new
        if links:
            self._auto[new] = links
        net.name = new
        self._changed()
        return net

    def _uart_ep(self, ep: tuple[str, str]) -> tuple[str, str]:
        cid = self.chip_id(ep[0])
        find = getattr(self.chips[cid], "find_uart", None)
        return cid, ((find(ep[1]) if find is not None else None) or ep[1].upper())

    def add_uart_link(self, a: tuple[str, str], b: tuple[str, str], *, bidir: bool = True) -> UartLink:
        a, b = self._uart_ep(a), self._uart_ep(b)
        if a == b:
            raise ValueError(f"board: a uart link needs two different endpoints (got {a[0]}.{a[1]} twice)")
        for ln in self.links:
            if not ln.auto and (ln.covers(a, b) or (bidir and ln.covers(b, a))):
                raise ValueError(f"board: uart link {ln.describe()} already connects these endpoints")
        link = UartLink(self, a, b, bidir=bidir)
        self.links.append(link)
        self._changed()
        self._refresh_auto()                # drop auto links this explicit link now covers
        return link

    add_link = add_uart_link

    def _can_ep(self, ep: tuple[str, str]) -> tuple[str, str]:
        cid = self.chip_id(ep[0])
        find = getattr(self.chips[cid], "find_can", None)
        name = find(ep[1]) if find is not None else None
        return cid, name or ep[1]

    def add_can_link(self, nodes: list[tuple[str, str]], *, name: str | None = None, host: bool = False) -> CanLink:
        """Put CAN controllers [(chip id, CAN0 / ECANA / ...), ...] on one CAN bus."""
        nodes = [self._can_ep(n) for n in nodes]
        if len(set(nodes)) != len(nodes):
            raise ValueError("board: a can link lists the same controller twice")
        for ln in self.can_links:
            both = set(nodes) & set(ln.nodes)
            if both:
                raise ValueError(f"board: {', '.join(f'{c}.{p}' for c, p in both)} already on CAN bus {ln.name}")
        name = name or ("CANBUS" if not self.can_links else f"CANBUS{len(self.can_links)}")
        link = CanLink(self, name, nodes, host=host)
        self.can_links.append(link)
        self._changed()
        return link

    def add_eth_link(self, nodes: list, *, name: str | None = None, host: dict | None = None,
                     pcap: str | None = None, caps=None, inject: list | None = None):
        """Put Ethernet MACs on one segment (chipem.ethernet.EthLink): nodes = [(chip id, MAC name[, PHY options])]."""
        from .ethernet import EthLink
        full = [(self.chip_id(n[0]), n[1], dict(n[2]) if len(n) > 2 and n[2] else {}) for n in nodes]
        for ln in self.eth_links:
            both = {(c, p.upper()) for c, p, _ in full} & {(c, p.upper()) for c, p in ln.nodes}
            if both:
                raise ValueError(f"board: {', '.join(f'{c}.{p}' for c, p in both)} already on Ethernet segment "
                                 f"{ln.name}")
        name = name or ("LAN" if not self.eth_links else f"LAN{len(self.eth_links)}")
        link = EthLink(self, name, full, host=host, pcap=pcap, caps=caps, inject=inject)
        self.eth_links.append(link)
        self._changed()
        return link

    def eth_segment(self, name: str | None = None):
        """The EthSegment named `name` (default: the first one)."""
        for ln in self.eth_links:
            if name is None or ln.name.upper() == str(name).upper():
                return ln.segment
        raise ValueError(f"board: no Ethernet segment {name!r}")

    def add_usb_link(self, device: tuple[str, str], *, name: str | None = None, options: dict | None = None):
        """Plug a chip's USB device port (stm32.otg_fs, kinetis.usbfs, ...) into a host (chipem.usbbus.UsbLink)."""
        from .usbbus import UsbLink
        cid = self.chip_id(device[0])
        chip = self.chips[cid]
        port = chip.find_usb(device[1]) if hasattr(chip, "find_usb") else None
        if port is None:
            raise ValueError(f"board: {cid}.{device[1]} is not a USB device port (ports: "
                             f"{', '.join(getattr(chip, 'usb_blocks', dict)()) or 'none'})")
        links = self.__dict__.setdefault("usb_links", [])
        if any(ln.chip_id == cid and ln.port == port for ln in links):
            raise ValueError(f"board: {cid}.{port} already has a usb link")
        link = UsbLink(name or ("USB" if not links else f"USB{len(links)}"), cid, port, chip.peripherals[port],
                       options)
        links.append(link)
        self._changed()
        return link

    def usb_host(self, name: str | None = None):
        """The UsbHost of the usb link named `name` (or of the chip port `name`; default: the first link)."""
        for ln in getattr(self, "usb_links", []):
            if name is None or str(name).upper() in (ln.name.upper(), ln.port.upper(), f"{ln.chip_id}.{ln.port}".upper()):
                return ln.host
        raise ValueError(f"board: no usb link {name!r}")

    def can_bus(self, name: str | None = None):
        """The CanBus named `name` (default: the first one)."""
        for ln in self.can_links:
            if name is None or ln.name.upper() == str(name).upper():
                return ln.bus
        raise ValueError(f"board: no CAN bus {name!r}")

    def find_link(self, a: tuple[str, str], b: tuple[str, str]) -> UartLink:
        """The explicit link between endpoints a and b (either orientation)."""
        a, b = self._uart_ep(a), self._uart_ep(b)
        for ln in self.links:
            if not ln.auto and {ln.a, ln.b} == {a, b}:
                return ln
        auto = [ln for ln in self.links if ln.auto and {ln.a, ln.b} == {a, b}]
        if auto:
            raise ValueError(f"board: {auto[0].describe()} is an automatic link from net {auto[0].net}; "
                             f"remove or rewire that net instead")
        raise ValueError(f"board: no uart link between {a[0]}.{a[1]} and {b[0]}.{b[1]}")

    def remove_link(self, link: "UartLink | int | tuple") -> None:
        """Remove an explicit link (the UartLink, its index in `links`, or an (a, b) endpoint pair)."""
        if isinstance(link, int):
            link = self.links[link]
        elif isinstance(link, tuple):
            link = self.find_link(*link)
        if link.auto:
            raise ValueError(f"board: {link.describe()} is an automatic link from net {link.net}; "
                             f"remove or rewire that net instead")
        link.close()
        self.links.remove(link)
        self._changed()
        self._refresh_auto()                # an auto link may now be needed for that direction

    def _changed(self) -> None:
        self.wiring += 1

    # ---------------------------------------------------------------- auto UART links
    def _uart_of(self, cid: str, chip: Any, owner: str) -> tuple[tuple[str, str], str] | None:
        sig = uart_signal(owner)
        if sig is None:
            return None
        blocks = chip.uart_blocks() if hasattr(chip, "uart_blocks") else {
            n: b for n, b in chip.peripherals.items() if hasattr(b, "tx_listeners")}
        name = sig[0] if sig[0] in blocks else None
        if name is None and hasattr(chip, "find_uart"):
            name = chip.find_uart(sig[0])
        if name is None or name not in blocks:
            return None
        return (cid, name), sig[1]

    def _wanted_auto(self, net: Net) -> list[tuple[tuple[str, str], tuple[str, str]]]:
        txs: dict = {}
        rxs: dict = {}
        for cid, chip, pin in net.members:
            r = self._uart_of(cid, chip, pin.owner)
            if r is not None:
                (txs if r[1] == "tx" else rxs)[r[0]] = True
        explicit = [ln for ln in self.links if not ln.auto]
        return [(a, b) for a in txs for b in rxs if not any(ln.covers(a, b) for ln in explicit)]

    def _refresh_auto(self, net: Net | None = None, src_chip: Any = None) -> None:
        """(Re)create the auto UART links of one net (or of every net) from the member pins' current owners."""
        for n in ([net] if net is not None else list(self.nets)):
            have = self._auto.setdefault(n.name, {})
            want = self._wanted_auto(n)
            t = src_chip.now_ns() if src_chip is not None else None
            for key in [k for k in have if k not in want]:
                ln = have.pop(key)
                ln.close()
                if ln in self.links:
                    self.links.remove(ln)
                self._changed()
                self.info(f"net {n.name}: auto UART link {_ep(key[0])} → {_ep(key[1])} removed", t_ns=t,
                          net=n.name)
            for key in want:
                if key in have:
                    continue
                try:
                    ln = UartLink(self, key[0], key[1], bidir=False)
                except ValueError as e:
                    self.warn(f"net {n.name}: no auto UART link {_ep(key[0])} → {_ep(key[1])}: {e}", t_ns=t)
                    continue
                ln.auto, ln.net = True, n.name
                have[key] = ln
                self.links.append(ln)
                self._changed()
                self.info(f"net {n.name}: auto UART link {_ep(key[0])} → {_ep(key[1])}", t_ns=t, net=n.name,
                          link=ln.describe())
            if not have:
                self._auto.pop(n.name, None)

    def _drop_auto(self, name: str) -> None:
        for key, ln in self._auto.pop(name, {}).items():
            ln.close()
            if ln in self.links:
                self.links.remove(ln)
            self.info(f"net {name}: auto UART link {_ep(key[0])} → {_ep(key[1])} removed", net=name)

    def _on_pin(self, cid: str, chip: Any, ev: Any) -> None:
        net = self._net_of.get((cid, ev.pin))
        if net is None:
            return
        if ev.field == "owner":
            # firmware (re)muxed a member pin: a UART TX/RX function may have appeared or gone
            if uart_signal(ev.old) is not None or uart_signal(ev.new) is not None:
                self._refresh_auto(net, chip)
            return
        if ev.field == "level":
            # an input's level changes only through `external` (the net itself) or its pull (own event):
            # only an output's level change is a new contribution
            if chip.pins.get(ev.pin).direction != "out":
                return
        net.update(chip)

    # ---------------------------------------------------------------- export
    def to_dict(self, base: str | Path | None = None) -> dict:
        """The current board as a board-YAML mapping (explicit links only: auto links follow from the nets).
        Paths are absolute, or relative to `base` (the directory the YAML will live in)."""
        def path(p: str | None) -> str | None:
            if not p:
                return p
            if base is None:
                return str(Path(p).resolve())
            return os.path.relpath(Path(p).resolve(), Path(base).resolve())

        specs = {c.id: c for c in self.desc.chips} if self.desc is not None else {}
        chips = []
        for cid, chip in self.chips.items():
            sp = specs.get(cid)
            d: dict[str, Any] = {"id": cid}
            ref = sp.chip if sp is not None else getattr(getattr(chip, "desc", None), "name", "?")
            d["chip"] = path(ref) if str(ref).endswith((".yaml", ".yml")) else ref
            if sp is not None:
                if sp.firmware:
                    d["firmware"] = path(sp.firmware)
                if sp.load_addr is not None:
                    d["load_addr"] = hex(sp.load_addr)
                if sp.clock_hz:
                    d["clock_hz"] = int(sp.clock_hz) if float(sp.clock_hz).is_integer() else sp.clock_hz
                if sp.vtor is not None:
                    d["vtor"] = hex(sp.vtor)
                if sp.format:
                    d["format"] = sp.format
            chips.append(d)
        nets = []
        for n in self.nets:
            if not n.members and not n.pull and not n.drive and not n.open_drain:
                continue                    # part-only net: implied by the parts' ports
            d = {"name": n.name, "pins": [n.label(i) for i in range(len(n.members))]}
            if n.pull:
                d["pull"] = n.pull
            if n.drive:
                d["drive"] = n.drive
            if n.open_drain:
                d["open_drain"] = True
            nets.append(d)
        links = []
        for ln in self.links:
            if ln.auto:
                continue
            d = {"type": "uart", "from": _ep(ln.a), "to": _ep(ln.b)}
            if not ln.bidir:
                d["dir"] = "tx"
            links.append(d)
        links.extend(ln.to_dict() for ln in self.can_links)
        links.extend(ln.to_dict() for ln in getattr(self, "eth_links", []))
        links.extend(ln.to_dict() for ln in getattr(self, "usb_links", []))
        out: dict[str, Any] = {"name": self.name, "quantum": f"{self.quantum_ns / 1e3:g}us", "chips": chips}
        if nets:
            out["nets"] = nets
        if links:
            out["links"] = links
        if self.desc is not None and self.desc.devices:
            devs = []
            for dv in self.desc.devices:
                o = dict(dv.options)
                for k in ("file", "image", "save"):
                    if o.get(k):
                        o[k] = path(o[k])
                devs.append({"type": dv.type, "chip": dv.chip, **o})
            out["devices"] = devs
        if self.parts:
            out["parts"] = [p.to_dict() for p in self.parts.values()]
            for pd, p in zip(out["parts"], self.parts.values()):
                for k in getattr(p, "PATH_PARAMS", ()):  # script files: relative to the exported YAML
                    if pd.get(k):
                        pd[k] = path(pd[k])
        if self.desc is not None and getattr(self.desc, "host_serial", None):
            out["host_serial"] = [dict(h) for h in self.desc.host_serial]
        if self.desc is not None and getattr(self.desc, "realtime", None) is not None:
            out["realtime"] = self.desc.realtime
        bp = dict(getattr(self.desc, "perf", None) or {}) if self.desc is not None else {}
        if "preset" in bp:
            out["performance"] = bp["preset"]
        if self.auto_quantum or "auto_quantum" in bp:
            out["auto_quantum"] = bool(self.auto_quantum)
        if "max_slice" in bp:
            out["max_slice"] = int(bp["max_slice"])
        if self.desc is not None and self.desc.stimulus:
            out["stimulus"] = list(self.desc.stimulus)
        return out

    def to_yaml(self, base: str | Path | None = None) -> str:
        """The current board (chips with firmware, nets, explicit links, devices, stimulus) as board YAML that
        `load_board` reads back into an equivalent board. One flow-style line per chip/net/link."""
        import yaml

        def flow(v: Any) -> str:
            out = yaml.safe_dump(v, default_flow_style=True, sort_keys=False, width=10_000).strip()
            return out[:-3].strip() if out.endswith("\n...") else out      # scalars end with a document marker

        d = self.to_dict(base)
        for p in d.get("parts", []):
            p.pop("state", None)            # runtime state is not configuration
            p["ports"] = {k: v for k, v in p["ports"].items() if v is not None}
        lines = [f"# board {self.name}: exported by chipem", f"name: {flow(d['name'])}", f"quantum: {d['quantum']}"]
        auto = [ln for ln in self.links if ln.auto]
        for key in ("chips", "nets", "links", "devices", "parts", "host_serial"):
            if key in d:
                lines.append(f"{key}:")
                lines.extend(f"  - {flow(item)}" for item in d[key])
        if auto:
            lines.append("# automatic UART links (derived from the nets and the pins' current owners; not stored):")
            lines.extend(f"#   net {ln.net}: {ln.describe()}" for ln in auto)
        if "realtime" in d:
            lines.append(f"realtime: {flow(d['realtime'])}")
        for key in ("performance", "auto_quantum", "max_slice"):
            if key in d:
                lines.append(f"{key}: {flow(d[key])}")
        if "stimulus" in d:
            lines.append(f"stimulus: {flow(d['stimulus'])}")
        return "\n".join(lines) + "\n"

    # ---------------------------------------------------------------- stimulus
    def set_pin(self, cid: str, pin: Any, level: str | None) -> None:
        """User stimulus: a pin in a net drives the whole net (strong); other pins get `external` directly."""
        cid = self.chip_id(cid)
        net = self.net_of(cid, pin)
        if net is None:
            self.chips[cid].pins.set_external(pin, level)
            return
        net.stimulus = level
        net.update(self.chips[cid])

    # ---------------------------------------------------------------- time
    def now_ns(self) -> float:
        return min(c.now_ns() for c in self.chips.values())

    def skew_ns(self) -> float:
        ts = [c.clock.now_ns() for c in self.chips.values()]
        return max(ts) - min(ts)

    def stop(self) -> None:
        """Thread-safe: end the current run_for early (StopReason.USER)."""
        self._stop = True
        cur = self._current
        if cur is not None:
            cur.stop()

    def run_for(self, ns: float) -> StopReason:
        chips = list(self.chips.items())
        wall = self.chip_wall
        clk = time.perf_counter
        if len(chips) == 1:
            cid0, chip0 = chips[0]
            self._current = chip0
            w0 = clk()
            try:
                return chip0.run_for(ns)
            finally:
                self._current = None
                wall[cid0] = wall.get(cid0, 0.0) + (clk() - w0)
        target = min(c.clock.now_ns() for _i, c in chips) + float(ns)
        self._stop = False
        self.last_stop_chip = None
        eps = 1e-6
        auto = self.auto_quantum
        while True:
            if self._stop:
                self._stop = False
                return StopReason.USER
            q = self.quantum_ns                   # (may be changed between iterations: web/shell `quantum`)
            # chip furthest behind
            cid, chip, t = None, None, math.inf
            for i, c in chips:
                tc = c.clock.now_ns()
                if tc < t:
                    cid, chip, t = i, c, tc
            if t >= target - eps:
                return StopReason.COUNT
            if auto:
                # auto quantum: a sleeping chip cannot act before its next event, so it holds the others back up to
                # that event (exactly: its event-driven outputs are never late); an awake chip by its now + q.
                # Something sent to a sleeping chip meanwhile ends the sender's run (_idle_poke) and re-plans.
                limit, blocker = target, None
                for i, c in chips:
                    if i != cid:
                        b = self._bound_ns(c, q)
                        if b < limit:
                            limit, blocker = b, c
                others = limit
            else:
                others = min(c.clock.now_ns() for i, c in chips if i != cid)
                limit = min(target, others + q)
            # board-clock events (pulse/pattern parts, part stimulus) are known in advance: no chip runs past the
            # next one, so every chip is at or behind it when it fires and receives the edge at its exact time
            if self._timed:
                nxt = self.next_timed_ns()
                if nxt is not None and t + eps < nxt <= limit:
                    limit = nxt
                    others_now = min(c.clock.now_ns() for i, c in chips if i != cid)
                    if chip is self.clock_chip and others_now < nxt - eps:
                        # the event fires on this chip's scheduler: stop just short of it and let the others
                        # catch up first, so they see the drivers as they were before the edge
                        short = nxt - 2e9 / chip.clock.hz
                        if short > t + eps:
                            limit = short
            dt = limit - t
            if dt <= eps:
                if auto and blocker is not None and blocker.sleeping and blocker.clock.now_ns() <= t + eps:
                    # tie with a sleeper whose next event is due now: let it fire that event first
                    chip, cid = blocker, next(i for i, c in chips if c is blocker)
                    dt = 2e9 / chip.clock.hz
                else:
                    dt = min(q, target - t)
            self._current = chip
            w0 = clk()
            try:
                r = chip.run_for(dt)
            finally:
                self._current = None
                wall[cid] = wall.get(cid, 0.0) + (clk() - w0)
            if r is StopReason.COUNT:
                continue
            if r is StopReason.USER:
                if self._stop:
                    self._stop = False
                    return StopReason.USER
                continue
            self.last_stop_chip = cid
            return r

    @staticmethod
    def _bound_ns(c: Any, q: float) -> float:
        """Auto quantum: how far the other chips may run ahead of chip `c`. A chip that sleeps (WFI/IDLE, no wake-up
        pending) or is parked (its last slice ended in a branch-to-itself or a skipped polling loop: its state repeats
        until an event) does nothing before its next scheduled event: up to that event (a polling loop: at most
        poll_skip_ns); an awake chip: its now + q."""
        now = c.clock.now_ns()
        parked = c.parked and not c.sleeping
        if not (c.sleeping or parked) or c._wake_req:
            return now + q
        if c.arch.wakeup_pending() and (c.wake_gate is None or c.wake_gate() or parked):
            return now + q
        nd = c.sched.next_due()
        b = math.inf if nd is None else max(now, c.clock.cycles_to_ns(nd))
        if parked and c.poll_skip_ns > 0 and c.core.spin_pc is None:
            b = min(b, now + c.poll_skip_ns)        # a skipped polling loop is re-read at least every poll_skip_ns
        return b

    def set_auto_quantum(self, on: bool) -> None:
        """Auto quantum on/off (see run_for). While on, every chip reports events scheduled on it from outside
        (Chip.idle_watch) so a sleeping chip that receives something re-plans the lockstep at once."""
        self.auto_quantum = bool(on)
        for c in self.chips.values():
            c.idle_watch = self._idle_poke if self.auto_quantum else None

    def _idle_poke(self, chip: Any) -> None:
        """An event was scheduled on `chip` while another chip runs: if `chip` sleeps (it may lag far behind under
        auto quantum), end the running chip's run_for so the next iteration limits it by `chip`'s new event."""
        cur = self._current
        if cur is not None and cur is not chip and (chip.sleeping or chip.parked):
            cur.yield_run()

    def reset(self) -> None:
        for chip in self.chips.values():
            chip.reset()
        self.reset_parts()
        for net in self.nets:
            net.update()

    # ---------------------------------------------------------------- parts
    def reset_parts(self) -> None:
        """Power-on state of every part (timers cancelled and restarted). The caller re-resolves the nets."""
        for p in self.parts.values():
            p.reset()

    def part_now_ns(self) -> float:
        """Time of the event being propagated (the source chip's instruction-precise now), else the board clock."""
        src = self._src if self._src is not None else self.clock_chip
        return src.now_ns()

    def sched_at_ns(self, t_ns: float, cb: Callable, *args: Any):
        """Board clock domain (wall-clock ns on the first chip's scheduler). Returns the Event (cancel with
        sched_cancel). These events are sync points of the lockstep loop (run_for)."""
        box: list = []
        ev = self.clock_chip.sched.at_ns(float(t_ns), self._fire_timed, box, cb, args)
        box.append(ev)
        self._timed.add(ev)
        return ev

    def _fire_timed(self, box: list, cb: Callable, args: tuple) -> None:
        self._timed.discard(box[0])
        cb(*args)

    def next_timed_ns(self) -> float | None:
        """Time of the earliest pending board-clock event, or None."""
        live = [ev for ev in self._timed if not ev.cancelled]
        if len(live) != len(self._timed):
            self._timed = set(live)
        return min(ev.t_ns for ev in live) if live else None

    def sched_in_ns(self, dt_ns: float, cb: Callable, *args: Any):
        return self.sched_at_ns(self.part_now_ns() + float(dt_ns), cb, *args)

    def sched_cancel(self, ev: Any) -> None:
        if ev is not None:
            self.clock_chip.sched.cancel(ev)
            self._timed.discard(ev)

    def part(self, part_id: str) -> Any:
        p = self.parts.get(part_id)
        if p is not None:
            return p
        for k, v in self.parts.items():
            if k.upper() == str(part_id).strip().upper():
                return v
        raise ValueError(f"board: no part {part_id!r}" + (f" (parts: {', '.join(self.parts)})" if self.parts else ""))

    def find_part(self, name: str) -> Any:
        """The part called `name` (case-insensitive) or None."""
        try:
            return self.part(name)
        except ValueError:
            return None

    def _part_id(self, cls: Any) -> str:
        prefix = getattr(cls, "ID_PREFIX", "U")
        used = {k.upper() for k in self.parts}
        i = 1
        while f"{prefix}{i}".upper() in used:
            i += 1
        return f"{prefix}{i}"

    def add_part(self, part_id: str | None, type_name: str, params: dict | None = None,
                 ports: dict[str, str | None] | None = None) -> Any:
        """Create a part (chipem.parts.registry type), reset it and wire `ports` {port: net name | CHIP.PIN}."""
        from .parts import registry
        cls = registry.get(type_name)
        pid = str(part_id).strip() if part_id not in (None, "") else self._part_id(cls)
        if not pid or any(c in pid for c in ".:=,") or " " in pid:
            raise ValueError(f"board: bad part id {pid!r} (letters, digits, _ and -)")
        if self.find_part(pid) is not None:
            raise ValueError(f"board: part {pid} already exists")
        if any(cid.upper() == pid.upper() for cid in self.chips):
            raise ValueError(f"board: part id {pid} clashes with a chip id")
        part = cls(pid, params or {})
        for port in (ports or {}):
            if port not in part.ports:
                raise ValueError(f"board: part {pid} ({part.type}) has no port {port!r} "
                                 f"(ports: {', '.join(part.ports)})")
        part.attach(self)
        self.parts[pid] = part
        part.reset()
        try:
            for port, to in (ports or {}).items():
                if to not in (None, ""):
                    self.connect_port(pid, port, to)
            # parts that wait for another part (a cable plugged into a connector; chipem/parts/serial.py)
            for other in list(self.parts.values()):
                hook = getattr(other, "part_added", None)
                if hook is not None:
                    hook(part)
        except Exception:
            self.remove_part(pid)
            raise
        self._changed()
        self.part_event(part, action="add", **part.state())
        return part

    def remove_part(self, part_id: str) -> None:
        part = self.part(part_id)
        for port in list(part.ports):
            if (part.id, port) in self._port_net:
                self.disconnect_port(part.id, port)
        part.detach()
        self.parts.pop(part.id, None)
        self._changed()
        self.log.event("part", self.part_now_ns(), chip="board", part=part.id, action="remove")

    def set_part(self, part_id: str, **values: Any) -> dict:
        """User action on a part (switch level, button pressed, pulse period, ...). Returns the new state()."""
        part = self.part(part_id)
        st = part.set(**values)
        self.part_event(part, action="set", **{k: v for k, v in st.items() if not isinstance(v, (dict, list))})
        return st

    def part_net(self, part_id: str, port: str) -> Net | None:
        part = self.find_part(part_id)
        return self._port_net.get((part.id, port)) if part is not None else None

    def _resolve_endpoint(self, part: Any, port: str, to: str) -> Net:
        """Net name -> that net (created, pin-less, if new); CHIP.PIN (or PIN on a one-chip board) -> the pin's
        net, or a new anonymous net `<part>_<port>` holding the pin."""
        to = str(to).strip()
        if not to:
            raise ValueError("board: empty connection target")
        for n in self.nets:
            if n.name == to:
                return n
        cid, rest = split_qualified(to, list(self.chips))
        if cid is None and len(self.chips) == 1:
            only = next(iter(self.chips))
            try:
                self.chips[only].pins.get(to)
                cid, rest = only, to
            except (KeyError, AttributeError):
                pass
        if cid is not None:
            try:
                pin = self.chips[cid].pins.get(rest)
            except KeyError:
                raise ValueError(f"board: chip {cid} has no pin {rest!r}") from None
            net = self._net_of.get((cid, pin.id))
            if net is not None:
                return net
            name = f"{part.id}_{port}"
            names = {n.name.upper() for n in self.nets}
            k = 2
            while name.upper() in names:
                name = f"{part.id}_{port}_{k}"
                k += 1
            net = self.add_net(name, [(cid, pin.id)])
            net.anon = net.by_part = True
            return net
        for n in self.nets:
            if n.name.upper() == to.upper():
                return n
        if "." in to:
            raise ValueError(f"board: {to!r} is neither a net nor a chip pin (chips: {', '.join(self.chips)})")
        net = self.add_net(to, [])
        net.by_part = True
        return net

    def connect_port(self, part_id: str, port: str, to: str) -> Net:
        """Wire a part port to a net (by name; created if absent) or a chip pin ("A.PTC5"). Replaces an existing
        connection of that port."""
        part = self.part(part_id)
        if port not in part.ports:
            raise ValueError(f"board: part {part.id} ({part.type}) has no port {port!r} "
                             f"(ports: {', '.join(part.ports)})")
        if (part.id, port) in self._port_net:
            self.disconnect_port(part.id, port)
        net = self._resolve_endpoint(part, port, to)
        net.ports.append((part, port))
        self._port_net[(part.id, port)] = net
        self._changed()
        net.update(self.clock_chip)
        return net

    def disconnect_port(self, part_id: str, port: str) -> None:
        part = self.part(part_id)
        net = self._port_net.pop((part.id, port), None)
        if net is None:
            return
        net.ports = [(p, n) for p, n in net.ports if not (p is part and n == port)]
        self._changed()
        if net.by_part and not net.ports and (net.anon or not net.members) and not net.pull and not net.drive:
            self.remove_net(net.name)       # implicit net no longer used by any part
        else:
            net.update(self.clock_chip)
        self._port_floating(part, port)

    def _port_floating(self, part: Any, port: str) -> None:
        """A disconnected input port reads X (and the part reacts)."""
        if part.ports[port].is_input and part.inputs.get(port) != "X" and part.board is self:
            part.inputs[port] = "X"
            part.on_input(port, "X", self.part_now_ns())

    def part_drive_changed(self, part: Any, port: str) -> None:
        """A part output changed: re-resolve its net (zero-delay; joins the running propagation if any)."""
        net = self._port_net.get((part.id, port))
        if net is not None:
            net.update(self._src if self._src is not None else self.clock_chip)

    def part_event(self, part: Any, **fields: Any) -> None:
        self.log.event("part", self.part_now_ns(), chip="board", part=part.id, **fields)

    def _propagate(self, net: Net, src: Any) -> None:
        """Resolve `net` and everything its part ports drive, in waves (zero delay). A net resolved more than
        MAX_PROPAGATION times in one event means a combinational loop: one warn, propagation stops."""
        if self._wave is not None:
            if all(n is not net for n in self._wave):
                self._wave.append(net)
            return
        self._wave = []
        prev_src = self._src
        if src is not None:
            self._src = src
        counts: dict[int, int] = {}
        try:
            cur = [net]
            while cur:
                for n in cur:
                    k = counts.get(id(n), 0) + 1
                    counts[id(n)] = k
                    if k > MAX_PROPAGATION:
                        names = sorted({x.name for x in cur})
                        key = tuple(names)
                        if key not in self._loop_warned:
                            self._loop_warned.add(key)
                            self.warn(f"combinational loop: net(s) {', '.join(names)} still changing after "
                                      f"{MAX_PROPAGATION} iterations; propagation stopped",
                                      t_ns=self.part_now_ns(), net=names[0])
                        return
                    n._update_once(self._src)
                cur, self._wave = self._wave, []
        finally:
            self._wave = None
            self._src = prev_src

    def close(self) -> None:
        for p in self.parts.values():
            p._cancel_all()
        for u in self._unsubs:
            u()
        self._unsubs = []
        for link in self.links:
            link.close()
        self._auto.clear()
        for cl in self.can_links:
            cl.close()
        for el in getattr(self, "eth_links", []):
            el.close()
        for ul in getattr(self, "usb_links", []):
            ul.close()
        sr = getattr(self, "_serial_router", None)     # serial cable chains (chipem/parts/serial.py)
        if sr is not None:
            sr.close()
