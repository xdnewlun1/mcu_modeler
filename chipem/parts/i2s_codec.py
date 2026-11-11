"""I2S audio codec part (DAC + ADC side of a stereo codec, e.g. an SGTL5000 / WM8731 in slave mode).

Generic glue (no vendor knowledge): it watches the bit clock, word select and serial data nets and drives one serial
data net.  See docs/CONTRACTS.md "Board parts".

Ports: `bclk` (in), `ws` (in, word select / frame sync), `sdin` (in, data from the MCU: DAC input), `sdout` (out,
data to the MCU: ADC output; high impedance until the first frame).  The codec is a bus slave: the MCU generates BCLK
and WS.  Data is sampled on the `edge` (default rising) BCLK edge and `sdout` changes on the other one.
Formats: `i2s` (Philips: MSB one bit clock after the WS change, WS low = left channel) and `left_justified` (MSB
with the WS change, WS high = left).  `bits` per sample (default 16; longer slots are padded / ignored, a shorter
slot truncates).
Recording: every received stereo frame is appended to `frames` as (left, right) signed integers (bounded by
`record_max`, default 100000); `state()` reports the count and the last frame.
Playback: `pattern` = comma separated samples, interleaved left/right ("100,-100,2000,-2000"), repeated; or `wav` =
path of a 16-bit PCM WAV file (mono files feed both channels); or `echo: true` = what was received is sent back one
frame later (loopback).  Nothing configured: zeros.
Per-sample changes are not logged (a VCD still sees `sdout`); `set reset_record=true` clears the recording.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Part
from .registry import part
from .std import _bool, _fmt_level

__all__ = ["I2SCodec"]


def _signed(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def _parse_pattern(v: Any) -> list[int]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v]
    return [int(x, 0) for x in str(v).replace(";", ",").split(",") if x.strip()]


def _read_wav(path: str) -> list[int]:
    import wave
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit PCM WAV files are supported")
        n, ch = w.getnframes(), w.getnchannels()
        raw = w.readframes(n)
    s = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
    if ch == 1:
        return [x for v in s for x in (v, v)]
    return [x for k in range(0, len(s), ch) for x in s[k:k + 2]]


@part("i2s_codec")
class I2SCodec(Part):
    PARAMS = {"bits": 16, "format": "i2s", "edge": "rising", "pattern": "", "wav": "", "echo": False,
              "record_max": 100000}
    PORTS = {"bclk": "in", "ws": "in", "sdin": "in", "sdout": "out"}
    ID_PREFIX = "CODEC"

    def _setup(self) -> None:
        self.bits = int(self.params["bits"])
        if not 8 <= self.bits <= 32:
            raise ValueError(f"part {self.id}: bits must be 8..32")
        fmt = str(self.params["format"]).strip().lower().replace("-", "_")
        if fmt in ("lj", "left"):
            fmt = "left_justified"
        if fmt not in ("i2s", "left_justified"):
            raise ValueError(f"part {self.id}: format must be i2s or left_justified")
        self.params["format"] = self.fmt = fmt
        e = str(self.params["edge"]).strip().lower()
        if e not in ("rising", "falling"):
            raise ValueError(f"part {self.id}: edge must be rising or falling")
        self.sample_rising = e == "rising"
        self.echo = _bool(self.params["echo"], f"{self.id}.echo")
        self.params["echo"] = self.echo
        self.play = _parse_pattern(self.params["pattern"])
        wav = str(self.params["wav"] or "")
        if wav:
            base = getattr(self, "_base_dir", None)
            p = Path(wav) if Path(wav).is_absolute() or base is None else Path(base) / wav
            self.play = _read_wav(str(p))
        self.record_max = int(self.params["record_max"])

    def _reset_state(self) -> None:
        super()._reset_state()
        self.frames: list[tuple[int, int]] = []
        self._acc = 0                   # bits shifted in since the slot started
        self._nbits = 0
        self._slot_left = True          # the slot being received is the left channel
        self._cur: list[int | None] = [None, None]
        self._ws_s = None               # WS at the previous sampling edge
        self._synced = False            # a WS edge has been seen (recording starts with the next slot)
        self._ws_d = None               # WS at the previous drive edge
        self._out_word = 0
        self._out_bit = 0
        self._out_pending = None        # I2S: the next drive edge starts a word for this channel
        self._play_i = 0
        self._last_rx: list[int] = [0, 0]
        self.received = 0
        self.sent = 0

    def _is_left(self, ws: str | None) -> bool:
        return (ws == "L") if self.fmt == "i2s" else (ws == "H")

    # ---------------------------------------------------------------- receive (sdin)
    def _finish_slot(self) -> None:
        if self._nbits == 0:
            return
        n = self._nbits
        v = self._acc >> (n - self.bits) if n >= self.bits else self._acc << (self.bits - n)
        ch = 0 if self._slot_left else 1
        self._cur[ch] = _signed(v, self.bits)
        if ch == 1 and self._cur[0] is not None:
            fr = (self._cur[0], self._cur[1])
            self._last_rx = [fr[0], fr[1]]
            self.received += 1
            if len(self.frames) < self.record_max:
                self.frames.append(fr)
            self._cur = [None, None]
        self._acc = 0
        self._nbits = 0

    def _sample(self) -> None:
        ws = self.inputs.get("ws")
        changed = self._ws_s is not None and ws != self._ws_s
        self._ws_s = ws
        bit = 1 if self.inputs.get("sdin") == "H" else 0
        if changed and not self._synced:                 # first WS edge: frame alignment only
            self._synced = True
            self._acc = 0 if self.fmt == "i2s" else bit
            self._nbits = 0 if self.fmt == "i2s" else 1
            self._slot_left = self._is_left(ws)
            return
        if not self._synced:
            return
        if changed and self.fmt == "left_justified":
            self._finish_slot()
            self._slot_left = self._is_left(ws)
        self._acc = (self._acc << 1) | bit
        self._nbits += 1
        if changed and self.fmt == "i2s":
            self._finish_slot()                      # that was the LSB of the previous slot
            self._slot_left = self._is_left(ws)

    # ---------------------------------------------------------------- transmit (sdout)
    def _next_sample(self, ch: int) -> int:
        if self.echo:
            return self._last_rx[ch]
        if not self.play:
            return 0
        v = self.play[self._play_i % len(self.play)]
        self._play_i += 1
        return v

    def _start_word(self, left: bool) -> None:
        self._out_word = self._next_sample(0 if left else 1) & ((1 << self.bits) - 1)
        self._out_bit = 0
        self.sent += 1

    def _drive_bit(self) -> None:
        if self._out_bit < self.bits:
            b = self._out_word >> (self.bits - 1 - self._out_bit) & 1
            self._out_bit += 1
            self._drive("sdout", "H" if b else "L", quiet=True)
        else:
            self._drive("sdout", "L", quiet=True)

    def _drive_edge(self) -> None:
        ws = self.inputs.get("ws")
        changed = self._ws_d is not None and ws != self._ws_d
        self._ws_d = ws
        if self._out_pending is not None:            # I2S: one bit clock after the WS change
            self._start_word(self._out_pending)
            self._out_pending = None
        if changed:
            if self.fmt == "i2s":
                self._out_pending = self._is_left(ws)
            else:
                self._start_word(self._is_left(ws))
        if self._out.get("sdout") is None and not (changed or self.sent):
            return                                   # not framed yet: high impedance
        self._drive_bit()

    # ---------------------------------------------------------------- board interface
    def on_input(self, port: str, level: str, t_ns: float) -> None:
        if port != "bclk" or level not in ("H", "L"):
            return
        if (level == "H") == self.sample_rising:
            self._sample()
        else:
            self._drive_edge()

    # ---------------------------------------------------------------- UI preview (side-effect free)
    def sample_info(self) -> dict:
        """Counters of the recorded stereo frames (the UI refetches the waveform when `seq` changes)."""
        return {"seq": self.received, "kept": len(self.frames), "bits": self.bits, "channels": 2}

    def sample_view(self, n: int = 512) -> dict:
        """The last `n` recorded (received) stereo frames as [[left, right], ...] signed integers. When the recording
        is full (`record_max`), these are the last ones recorded, not the newest received (`received` > `kept`)."""
        n = max(1, min(int(n), 4096))
        return {"bits": self.bits, "channels": 2, "received": self.received, "kept": len(self.frames),
                "full": len(self.frames) >= self.record_max, "samples": [list(f) for f in self.frames[-n:]]}

    def state(self) -> dict:
        last = self.frames[-1] if self.frames else None
        return {"received": self.received, "sent": self.sent, "last": list(last) if last else None,
                "format": self.fmt, "bits": self.bits, "sdout": _fmt_level(self._out.get("sdout"))}

    def set(self, **values: Any) -> dict:
        for k, v in values.items():
            if k == "reset_record":
                if _bool(v, f"{self.id}.reset_record"):
                    self.frames = []
                    self.received = 0
                continue
            if k not in ("pattern", "echo"):
                raise ValueError(f"part {self.id} (i2s_codec): cannot set {k!r} (use pattern, echo, reset_record)")
            self.params[k] = v
        self._setup()
        return self.state()
