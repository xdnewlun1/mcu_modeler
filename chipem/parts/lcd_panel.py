"""Parallel RGB TFT panel part: receives the frames of an LCD-TFT controller and keeps the last one for tests and UIs.

Generic glue (no vendor knowledge; the mirror image of chipem/parts/camera.py).  Ports (inputs): `clk`, `hsync`, `vsync`,
`de` and the data lines `r0`..`r7`, `g0`..`g7`, `b0`..`b7` (connect only the ones the panel has, MSB-aligned like a
16/18-bit panel on LCD_R[7:3]/G[7:2]/B[7:3]).

Params: `width` x `height` (active pixels the panel expects), `hsync` / `vsync` (active level the panel expects: L or H),
`de` (active level H or L, or `ignore` for a panel in HV mode without DE), `pclk_edge` (`rising` or `falling`: the edge
the panel samples on), `bits` (8 or 6 bits per channel the panel resolves: the low bits are dropped).

Source hook (duck-typed, chip side): every peripheral of a chip pin on the `clk` net that has `lcd_attach(rx)` and
`lcd_accepts(pin)` (pin muxed to its pixel clock output) is a source.  The part registers itself when it is wired
(`part_added`) and at every board reset.  At the end of each active area the source calls `lcd_frame(source, frame)`;
`frame` has `width`, `height`, `number`, `t_ns` and `rgb()` (RGB888 bytes, composed lazily).  The part asks the source
which of its nets are really driven (`source.lcd_wiring(ports)`: clock, sync and data lines carried by pins muxed to the
controller, and the controller's polarities):
* no clock (pin not muxed) -> the frame is not seen;
* HSYNC/VSYNC (and DE unless `de: ignore`) not carried, or with an active level other than the panel's -> the panel loses
  sync: the frame is counted in `bad` with the reason, one `warn`, the image is not updated;
* data lines: only the bits whose net carries the matching controller output reach the panel (the others read 0); a
  panel with no data port connected takes all 24 bits (logged once as `info`);
* active area different from width x height: one `warn`; the image is cropped / padded with black.
The kept image (`image`: width x height RGB888 bytes, black before the first frame) is reachable with `frame_rgb()`,
`pixel(x, y)`, `frame_ppm()` (binary PPM P6) and summarised by `state()` (frames, bad, last frame time, measured fps,
CRC-32 and the centre pixel).  One `part` event per frame (action `frame`) unless `quiet`.
"""
from __future__ import annotations

import zlib
from typing import Any

from .base import Part, PortDesc
from .registry import part

__all__ = ["LcdPanel"]

DATA_PORTS = [f"{c}{k}" for c in "rgb" for k in range(8)]


@part("lcd_panel")
class LcdPanel(Part):
    """RGB TFT panel: takes frames from LCD-TFT controllers on its clk net, keeps the last image."""
    PARAMS = {"width": 240, "height": 320, "hsync": "L", "vsync": "L", "de": "H", "pclk_edge": "rising", "bits": 8}
    ID_PREFIX = "LCD"

    @classmethod
    def port_descs(cls, params: dict) -> dict:
        d = {"clk": PortDesc("in"), "hsync": PortDesc("in"), "vsync": PortDesc("in"), "de": PortDesc("in")}
        for n in DATA_PORTS:
            d[n] = PortDesc("in")
        return d

    def _setup(self) -> None:
        p = self.params
        self.width, self.height = int(p["width"]), int(p["height"])
        if self.width < 1 or self.height < 1:
            raise ValueError(f"part {self.id}: width and height must be >= 1")
        for k in ("hsync", "vsync"):
            v = str(p[k]).strip().upper()
            if v not in ("H", "L"):
                raise ValueError(f"part {self.id}: {k} must be H or L (active level)")
            p[k] = v
        de = str(p["de"]).strip()
        de = de.lower() if de.lower() == "ignore" else de.upper()
        if de not in ("H", "L", "ignore"):
            raise ValueError(f"part {self.id}: de must be H, L or ignore")
        p["de"] = de
        edge = str(p["pclk_edge"]).strip().lower()
        if edge not in ("rising", "falling"):
            raise ValueError(f"part {self.id}: pclk_edge must be rising or falling")
        p["pclk_edge"] = edge
        self.bits = int(p["bits"])
        if self.bits not in (6, 8):
            raise ValueError(f"part {self.id}: bits must be 6 or 8")
        self._sources: list[Any] = []

    def _reset_state(self) -> None:
        super()._reset_state()
        self.frames = 0
        self.bad = 0
        self.bad_reason = ""
        self.last_t_ns: float | None = None
        self.prev_t_ns: float | None = None
        self.image = bytes(3 * self.width * self.height)
        self.source = ""
        self._noted: set[str] = set()
        if self.board is not None:
            self._register()

    # ---------------------------------------------------------------- wiring
    def part_added(self, other: Any) -> None:
        if other is self:
            self._register()

    def _register(self) -> None:
        b = self.board
        net = b.part_net(self.id, "clk") if b is not None else None
        for blk in self._sources:
            blk.lcd_detach(self)
        self._sources = []
        if net is None:
            return
        for _cid, chip, _pin in net.members:
            for blk in getattr(chip, "peripherals", {}).values():
                if hasattr(blk, "lcd_attach") and blk not in self._sources:
                    blk.lcd_attach(self)
                    self._sources.append(blk)

    def detach(self) -> None:
        for blk in self._sources:
            blk.lcd_detach(self)
        self._sources = []
        super().detach()

    def _note(self, key: str, kind: str, msg: str) -> None:
        if key in self._noted or self.board is None:
            return
        self._noted.add(key)
        self.board.log.event(kind, self.now(), chip="board", part=self.id, msg=f"{self.id}: {msg}")

    # ---------------------------------------------------------------- frames
    def lcd_frame(self, src: Any, frame: Any) -> None:
        b = self.board
        if b is None:
            return
        chip = getattr(src, "chip", None)
        net = b.part_net(self.id, "clk")
        if net is None or not any(ch is chip and src.lcd_accepts(pin) for _c, ch, pin in net.members):
            return                                              # the clock does not reach the panel
        ports: dict[str, set] = {}
        for port in self.ports:
            n = b.part_net(self.id, port)
            ports[port] = {p.id for _c, ch, p in n.members if ch is chip} if n is not None else set()
        wire = src.lcd_wiring(ports)
        pol = wire["polarity"]
        p = self.params
        why = ""
        if not wire["hsync"] or not wire["vsync"]:
            why = "HSYNC/VSYNC not driven by the controller (net not connected or pin not muxed)"
        elif p["de"] != "ignore" and not wire["de"]:
            why = "DE not driven by the controller (net not connected or pin not muxed)"
        elif pol["hsync"] != p["hsync"] or pol["vsync"] != p["vsync"]:
            why = (f"sync polarity mismatch (controller HSYNC/VSYNC active {pol['hsync']}/{pol['vsync']}, panel "
                   f"expects {p['hsync']}/{p['vsync']})")
        elif p["de"] != "ignore" and pol["de"] != p["de"]:
            why = f"DE polarity mismatch (controller active {pol['de']}, panel expects {p['de']})"
        elif pol["pclk"] != p["pclk_edge"]:
            why = f"pixel clock edge mismatch (controller {pol['pclk']}, panel samples on {p['pclk_edge']})"
        if why:
            self.bad += 1
            self.bad_reason = why
            self._note("bad:" + why, "warn", f"frame not displayed: {why}")
            return
        if (frame.width, frame.height) != (self.width, self.height):
            self._note("size", "warn", f"controller active area {frame.width}x{frame.height} differs from the panel's "
                       f"{self.width}x{self.height}: image cropped / padded")
        rgb = frame.rgb()
        if wire["data_wired"]:
            masks = wire["mask"]
        else:
            masks = (0xFF, 0xFF, 0xFF)
            self._note("nodata", "info", "no data line connected: all 24 RGB bits taken from the controller")
        if self.bits == 6:
            masks = tuple(m & 0xFC for m in masks)
        self.image = self._fit(rgb, frame.width, frame.height, masks)
        self.frames += 1
        self.prev_t_ns, self.last_t_ns = self.last_t_ns, frame.t_ns
        self.source = f"{getattr(chip, 'desc', None) and chip.desc.name}.{getattr(src, 'name', '?')}"
        if not self.quiet:
            self.emit(action="frame", frame=self.frames, width=self.width, height=self.height,
                      crc=f"{zlib.crc32(self.image):08X}")

    def _fit(self, rgb: bytes, fw: int, fh: int, masks: tuple[int, int, int]) -> bytes:
        w, h = self.width, self.height
        out = bytearray(3 * w * h)
        cw = min(w, fw)
        for y in range(min(h, fh)):
            out[3 * w * y:3 * (w * y + cw)] = rgb[3 * fw * y:3 * (fw * y + cw)]
        if masks != (0xFF, 0xFF, 0xFF):
            for c, m in enumerate(masks):
                if m != 0xFF:
                    out[c::3] = bytes(v & m for v in out[c::3])
        return bytes(out)

    # ---------------------------------------------------------------- API
    def frame_rgb(self) -> bytes:
        """Last displayed image: width x height x 3 bytes (RGB888, row-major)."""
        return self.image

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        k = 3 * (y * self.width + x)
        return self.image[k], self.image[k + 1], self.image[k + 2]

    def frame_info(self) -> dict:
        """Size and sequence number of the displayed image (UI preview; refetched when `seq` changes)."""
        return {"w": self.width, "h": self.height, "seq": self.frames, "format": "rgb"}

    def frame_view(self, max_w: int = 320) -> dict:
        """The displayed image as RGB888, downscaled (nearest pixel) to <= max_w pixels wide. No side effects."""
        w, h, img = self.width, self.height, self.image
        step = max(1, -(-w // max(1, max_w)))
        if step == 1:
            data = bytes(img)
        else:
            out = bytearray()
            for y in range(0, h, step):
                row = 3 * y * w
                for x in range(0, w, step):
                    k = row + 3 * x
                    out += img[k:k + 3]
            data = bytes(out)
        return {"w": len(range(0, w, step)), "h": len(range(0, h, step)), "src_w": w, "src_h": h,
                "format": "rgb", "seq": self.frames, "data": data}

    def frame_ppm(self) -> bytes:
        return f"P6\n{self.width} {self.height}\n255\n".encode() + self.image

    def state(self) -> dict:
        fps = None
        if self.last_t_ns is not None and self.prev_t_ns is not None and self.last_t_ns > self.prev_t_ns:
            fps = round(1e9 / (self.last_t_ns - self.prev_t_ns), 3)
        cx, cy = self.width // 2, self.height // 2
        return {"frames": self.frames, "bad": self.bad, "bad_reason": self.bad_reason,
                "size": f"{self.width}x{self.height}", "last_frame_ns": self.last_t_ns, "fps": fps,
                "crc": f"{zlib.crc32(self.image):08X}", "center": "#%02X%02X%02X" % self.pixel(cx, cy),
                "source": self.source}
