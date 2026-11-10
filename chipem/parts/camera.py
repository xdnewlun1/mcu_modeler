"""Parallel (DVP) camera sensor part: frames of a test pattern or an image file, with real HSYNC/VSYNC timing.

Generic glue (no vendor knowledge).  Ports: `pixclk`, `hsync`, `vsync` (outputs) and `d0`..`d<bits-1>` (outputs).

Params: `width` x `height` pixels, `bits` per sample (8..14), `format` (`gray`/`raw`: one sample per pixel of `bits`
bits; `rgb565`/`yuv422`: two 8-bit samples per pixel, high byte first / Y,U,Y,V), `pixclk` (Hz), `hblank` (pixel
clocks between lines), `vblank` (lines of vertical blanking), `fps` (0 = free running; otherwise the frame period,
vblank stretched to fit), `pattern` (`bars`, `gradient`, `counter` (changes every frame), `checker`) or `image` (a
binary PGM `P5` file, which sets width/height, or a raw file of width x height x bytes-per-pixel samples; relative
paths are taken from the current directory, then from the board file's directory), `sync` (`hardware` or `embedded`:
ITU-656 style 0xFF 0x00 0x00 XY codes `fs`, `ls`, `le`, `fe` around every line, the frame start code replacing the
first line start and the frame end code the last line end; pixel values are clamped to 1..254 so they never look like
a code), `hsync_blank` / `vsync_blank` (the level of HSYNC / VSYNC during blanking; defaults L / H like an OV7670 with
HREF), `start` (delay before the first frame).

Timing (pixel clock period P): a frame starts with VSYNC leaving its blanking level; line i drives HSYNC active
from (i x line + hblank) x P for width x samples-per-pixel (+8 code samples when embedded) clocks; VSYNC returns to
blanking half a pixel clock after the last line ends and stays there for `vblank` lines.  HSYNC/VSYNC are real net
levels (a VCD shows them; the event log summarises them as dense pins).  PIXCLK is driven low and NOT toggled per
edge: one stored `pin` event with `field="wave"` (freq_hz = pixclk, duty 0.5, edges) is logged per chip pin on the pixclk net per >= 100 ms of
running clock and when the part stops/resets (like chipem/periph/sdbus.py ClockWave).  Data pins stay low.

Receivers (DVP hook, duck-typed): at the start of every active line the part looks at the chip pins on its `pixclk`
net; every peripheral of those chips that has `dvp_accepts(pin) -> bool` and accepts the pin (it checks its own pin
muxing) gets `dvp_line(camera, t0_ns, period_ns, samples, ports)`: `samples[k]` (int) is clocked in at
t0 + (k + 1/2) x P, `ports` = {port name: set of pin ids of that chip on the port's net} so the receiver can tell
which data lines are really wired.  HSYNC is already active when `dvp_line` is called and goes back to blanking after
the last sample; in embedded mode the samples include the codes.  One `part` event per frame (`frame`, size).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Part, norm_level
from .registry import part
from .std import _fmt_level, _time

__all__ = ["Camera", "read_pgm"]

FORMATS = {"gray": 1, "raw": 1, "rgb565": 2, "yuv422": 2}
PATTERNS = ("bars", "gradient", "counter", "checker")
WAVE_NS = 100e6
_BARS565 = (0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000)


def read_pgm(data: bytes) -> tuple[int, int, int, list[int]]:
    """Binary PGM (P5): (width, height, maxval, samples row-major)."""
    if data[:2] != b"P5":
        raise ValueError("not a binary PGM (P5) file")
    fields: list[int] = []
    i = 2
    while len(fields) < 3:
        while i < len(data) and data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while i < len(data) and data[i:i + 1] not in (b"\n", b"\r"):
                i += 1
            continue
        j = i
        while j < len(data) and data[j:j + 1].isdigit():
            j += 1
        if j == i:
            raise ValueError("malformed PGM header")
        fields.append(int(data[i:j]))
        i = j
    i += 1                                              # one whitespace byte after maxval
    w, h, mx = fields
    if mx < 256:
        px = list(data[i:i + w * h])
    else:
        px = [int.from_bytes(data[i + 2 * k:i + 2 * k + 2], "big") for k in range(w * h)]
    if len(px) < w * h:
        raise ValueError("PGM file too short")
    return w, h, mx, px


@part("camera")
class Camera(Part):
    """Parallel (DVP) camera: test pattern or PGM/raw image frames with HSYNC/VSYNC timing; lines go to DVP
    receivers (camera interfaces) found on the pixclk net."""
    PARAMS = {"width": 160, "height": 120, "bits": 8, "format": "gray", "pixclk": 6_000_000, "hblank": 40,
              "vblank": 10, "fps": 0, "pattern": "bars", "image": "", "sync": "hardware", "hsync_blank": "L",
              "vsync_blank": "H", "fs": 0xAB, "ls": 0x80, "le": 0x9D, "fe": 0xB6, "start": "0ns"}
    ID_PREFIX = "CAM"

    @classmethod
    def port_descs(cls, params: dict) -> dict:
        from .base import PortDesc
        bits = int(params.get("bits", 8))
        d = {"pixclk": PortDesc("out"), "hsync": PortDesc("out"), "vsync": PortDesc("out")}
        for k in range(max(8, min(14, bits))):
            d[f"d{k}"] = PortDesc("out")
        return d

    def _setup(self) -> None:
        p = self.params
        self.bits = int(p["bits"])
        if not 8 <= self.bits <= 14:
            raise ValueError(f"part {self.id}: bits must be 8..14")
        fmt = str(p["format"]).strip().lower()
        if fmt not in FORMATS:
            raise ValueError(f"part {self.id}: format must be one of {', '.join(FORMATS)}")
        self.fmt = p["format"] = fmt
        self.bpp = FORMATS[fmt]
        if self.bpp == 2 and self.bits != 8:
            raise ValueError(f"part {self.id}: {fmt} needs bits: 8")
        pat = str(p["pattern"]).strip().lower()
        if pat not in PATTERNS:
            raise ValueError(f"part {self.id}: pattern must be one of {', '.join(PATTERNS)}")
        self.pattern = p["pattern"] = pat
        sync = str(p["sync"]).strip().lower()
        if sync not in ("hardware", "embedded"):
            raise ValueError(f"part {self.id}: sync must be hardware or embedded")
        self.sync = p["sync"] = sync
        if sync == "embedded" and self.bits != 8:
            raise ValueError(f"part {self.id}: embedded sync needs bits: 8")
        self.width, self.height = int(p["width"]), int(p["height"])
        self.pixclk = float(p["pixclk"])
        self.hblank, self.vblank = int(p["hblank"]), int(p["vblank"])
        self.fps = float(p["fps"])
        if self.pixclk <= 0 or self.hblank < 0 or self.vblank < 1:
            raise ValueError(f"part {self.id}: pixclk must be > 0, hblank >= 0, vblank >= 1")
        self.hs_blank = norm_level(p["hsync_blank"], f"{self.id}.hsync_blank")
        self.vs_blank = norm_level(p["vsync_blank"], f"{self.id}.vsync_blank")
        if self.hs_blank not in ("H", "L") or self.vs_blank not in ("H", "L"):
            raise ValueError(f"part {self.id}: hsync_blank / vsync_blank must be H or L")
        p["hsync_blank"], p["vsync_blank"] = self.hs_blank, self.vs_blank
        self.codes = {k: int(p[k]) & 0xFF for k in ("fs", "ls", "le", "fe")}
        self.start_ns = _time(p["start"], f"{self.id}.start")
        self.image: list[int] | None = None
        self._image_loaded = False
        if str(p["image"] or ""):
            self._load_image(required=False)
        self._geometry()

    def _geometry(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError(f"part {self.id}: width and height must be >= 1")
        self.period_ns = 1e9 / self.pixclk
        self.active = self.width * self.bpp + (8 if self.sync == "embedded" else 0)
        self.line_clks = self.active + self.hblank
        frame_clks = (self.height + self.vblank) * self.line_clks
        self.frame_ns = frame_clks * self.period_ns
        if self.fps > 0:
            want = 1e9 / self.fps
            if want < (self.height + 1) * self.line_clks * self.period_ns:
                raise ValueError(f"part {self.id}: fps {self.fps:g} too high for {self.width}x{self.height} at "
                                 f"{self.pixclk:g} Hz")
            self.frame_ns = want

    # ---------------------------------------------------------------- image / pattern
    def _image_path(self) -> Path:
        img = Path(str(self.params["image"]))
        if img.is_absolute() or img.exists():
            return img
        desc = getattr(self.board, "desc", None)
        base = getattr(desc, "path", None)
        if base is not None:
            cand = Path(base).parent / img
            if cand.exists():
                return cand
        return img

    def _load_image(self, required: bool = True) -> None:
        path = self._image_path()
        if not path.exists():
            if required:
                raise ValueError(f"part {self.id}: image {path} not found")
            return
        data = path.read_bytes()
        if data[:2] == b"P5":
            w, h, mx, px = read_pgm(data)
            if self.bpp != 1:
                raise ValueError(f"part {self.id}: a PGM image needs format gray/raw")
            shift = self.bits - (mx.bit_length())
            self.width, self.height = w, h
            self.params["width"], self.params["height"] = w, h
            self.image = [(v << shift) if shift >= 0 else (v >> -shift) for v in px]
        else:
            n = self.width * self.height * self.bpp
            if self.bits > 8:
                self.image = [int.from_bytes(data[2 * k:2 * k + 2], "little") for k in range(n)]
            else:
                self.image = list(data[:n])
            if len(self.image) < n:
                raise ValueError(f"part {self.id}: raw image {path} has fewer than {n} samples")
        self._image_loaded = True
        self._geometry()

    def _pixel(self, x: int, y: int, frame: int) -> int:
        """Pixel value: `bits` wide (gray/raw) or RGB565 (16 bits)."""
        mx = (1 << self.bits) - 1
        w, h = self.width, self.height
        if self.bpp == 2:
            if self.pattern == "bars":
                return _BARS565[min(7, x * 8 // w)]
            g = self._pixel_gray(x, y, frame, 255)
            return ((g >> 3) << 11) | ((g >> 2) << 5) | (g >> 3)
        return self._pixel_gray(x, y, frame, mx)

    def _pixel_gray(self, x: int, y: int, frame: int, mx: int) -> int:
        w, h = self.width, self.height
        if self.pattern == "bars":
            return (mx * (7 - min(7, x * 8 // w))) // 7
        if self.pattern == "gradient":
            return (mx * (x + y)) // max(1, w + h - 2)
        if self.pattern == "checker":
            return mx if ((x // 8) + (y // 8)) % 2 else 0
        return (x + y * w + frame) & mx                    # counter

    def line_samples(self, y: int, frame: int = 0) -> list[int]:
        """Active samples of line y (without sync codes)."""
        w = self.width
        if self.image is not None:
            row = self.image[y * w * self.bpp:(y + 1) * w * self.bpp]
            if self.bpp == 1:
                out = row
            else:
                out = row
        elif self.bpp == 1:
            out = [self._pixel(x, y, frame) for x in range(w)]
        elif self.fmt == "rgb565":
            out = []
            for x in range(w):
                v = self._pixel(x, y, frame)
                out += (v >> 8, v & 0xFF)
        else:                                              # yuv422: Y U Y V
            out = []
            for x in range(w):
                out += (self._pixel_gray(x, y, frame, 255), 128)
        if self.sync == "embedded":
            out = [min(254, max(1, v)) for v in out]
        return out

    # ---------------------------------------------------------------- engine
    def _reset_state(self) -> None:
        super()._reset_state()
        self.frames = 0
        self.lines = 0
        self.line_no = -1
        self.in_frame = False
        self._wave_since: float | None = None
        self._wave_edges = 0.0
        self._wave_t = 0.0
        self._out["pixclk"] = "L"
        for k in range(len(self.ports) - 3):
            self._out[f"d{k}"] = "L"
        self._out["hsync"] = self.hs_blank
        self._out["vsync"] = self.vs_blank
        if self.board is not None:
            if self.params.get("image") and not self._image_loaded:
                self._load_image(required=True)
            self._t_frame = self.now() + self.start_ns + self.vblank * self.line_clks * self.period_ns
            self._at(self._t_frame, self._frame_begin)

    def _receivers(self) -> list[tuple[Any, Any, dict]]:
        b = self.board
        net = b.part_net(self.id, "pixclk") if b is not None else None
        if net is None:
            return []
        out = []
        for _cid, chip, pin in net.members:
            for blk in getattr(chip, "peripherals", {}).values():
                acc = getattr(blk, "dvp_accepts", None)
                if acc is not None and acc(pin):
                    ports = {}
                    for port in self.ports:
                        n = b.part_net(self.id, port)
                        ports[port] = {p.id for c, ch, p in n.members if ch is chip} if n is not None else set()
                    out.append((chip, blk, ports))
        return out

    def _frame_begin(self) -> None:
        t = self._t_frame
        self.in_frame = True
        self.line_no = -1
        self._rx = self._receivers()
        if self._wave_since is None:
            self._wave_since = t
        self._drive("vsync", "L" if self.vs_blank == "H" else "H", quiet=True)
        for y in range(self.height):
            t0 = t + (y * self.line_clks + self.hblank) * self.period_ns
            self._at(t0, self._line_begin, y, t0)
        self._at(t + (self.height * self.line_clks + 0.5) * self.period_ns, self._frame_done)   # after the last HSYNC
        self._t_frame = t + self.frame_ns
        self._at(self._t_frame, self._frame_begin)

    def _line_begin(self, y: int, t0: float) -> None:
        self.line_no = y
        self._drive("hsync", "L" if self.hs_blank == "H" else "H", quiet=True)
        s = self.line_samples(y, self.frames)
        if self.sync == "embedded":
            c = self.codes
            sav = c["fs"] if y == 0 else c["ls"]
            eav = c["fe"] if y == self.height - 1 else c["le"]
            s = [0xFF, 0, 0, sav] + s + [0xFF, 0, 0, eav]
        for _chip, blk, ports in self._rx:
            blk.dvp_line(self, t0, self.period_ns, s, ports)
        self._at(t0 + self.active * self.period_ns, self._line_end)

    def _line_end(self) -> None:
        self.lines += 1
        self._drive("hsync", self.hs_blank, quiet=True)

    def _frame_done(self) -> None:
        self.in_frame = False
        self.frames += 1
        self._drive("vsync", self.vs_blank, quiet=True)
        t = self.now()
        self._wave_edges += 2 * self.frame_ns / self.period_ns
        if self._wave_since is not None and t - self._wave_since >= WAVE_NS:
            self._flush_wave(t)
        self.emit(frame=self.frames, width=self.width, height=self.height, receivers=len(self._rx),
                  action="frame")

    def _flush_wave(self, t: float | None = None) -> None:
        b = self.board
        if b is None or self._wave_since is None or not self._wave_edges:
            return
        net = b.part_net(self.id, "pixclk")
        for cid, _chip, pin in (net.members if net is not None else ()):
            b.log.event("pin", self._wave_t if t is None else t, chip=cid, pin=pin.id, field="wave", old=None,
                        new="L", edges=int(self._wave_edges), since_ns=self._wave_since, freq_hz=self.pixclk,
                        duty=0.5)
        self._wave_since = t
        self._wave_edges = 0.0

    def reset(self) -> None:
        if self.board is not None:
            self._flush_wave(self.now())
        super().reset()

    def detach(self) -> None:
        self._flush_wave(self.now() if self.board is not None else None)
        super().detach()

    def state(self) -> dict:
        return {"frames": self.frames, "lines": self.lines, "line": self.line_no, "in_frame": self.in_frame,
                "size": f"{self.width}x{self.height}", "format": self.fmt, "sync": self.sync,
                "pixclk_hz": self.pixclk, "fps": round(1e9 / self.frame_ns, 3),
                "vsync": _fmt_level(self._out.get("vsync")), "hsync": _fmt_level(self._out.get("hsync"))}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k not in ("pattern",):
                raise ValueError(f"part {self.id} (camera): cannot set {k!r} (use pattern)")
            self.params[k] = v
        pat = str(self.params["pattern"]).strip().lower()
        if pat not in PATTERNS:
            raise ValueError(f"part {self.id}: pattern must be one of {', '.join(PATTERNS)}")
        self.pattern = self.params["pattern"] = pat
        return self.state()

    def stimulus(self, value: str) -> dict:
        return self.set(pattern=value)
