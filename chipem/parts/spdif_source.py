"""S/PDIF (IEC 60958) transmitter part: a stereo PCM stream on one output port, summarised instead of edge by edge.

Generic glue (no vendor knowledge).  Port: `out` (output; connect it to a chip pin carrying an S/PDIF receiver input,
e.g. an STM32F446 SPDIFRX_INx pin).

Params: `rate` (sample rate in Hz, 32000..192000; the frame rate), `signal` (`sine`, `ramp`, `counter`, `silence`;
`counter` puts the frame number in the left channel and its complement in the right, which makes lost or swapped
samples easy to see), `freq` (Hz, sine), `amplitude` (0..1 of full scale), `bits` (16..24 significant bits, MSB
aligned in the 24-bit audio field), `nonaudio` (channel status bit 1: IEC 61937 data), `validity` (V bit),
`user` (user data bit pattern, one bit per sub-frame, repeating 32-bit integer), `parity_error_every` (N > 0: every
N-th sub-frame is sent with a wrong parity bit), `corrupt_at` (sub-frame index >= 0: that sub-frame is sent without a
valid preamble, a biphase coding error), `start` (delay before the stream starts) and `stop` (stream length; empty =
forever), `chunk` (frames per delivery, default 8).

IEC 60958-3 consumer channel status (192 bits per block, bit n carried in frame n): byte 0 bit 1 = non-audio, bit 2
= copy permitted; byte 3 bits 0-3 = sampling frequency (44.1 kHz 0000, 48 kHz 0010, 32 kHz 0011, 88.2 kHz 1000,
96 kHz 1010, 176.4 kHz 1100, 192 kHz 1110); byte 2 bits 4-7 = channel number (1 = left / A, 2 = right / B).  Frame 0
of every 192-frame block starts with preamble B, the other left sub-frames with M, every right sub-frame with W.

Line summary (no per-edge events): the `out` net is held L; the biphase-mark stream reaches receivers through a hook.
Every `chunk` frames the part looks at the chip pins on its net; each peripheral of those chips with
`spdif_accepts(pin) -> bool` that accepts the pin (it checks its own input selection) gets
`spdif_subframes(source, t0_ns, sub_ns, subframes)`: sub-frame k occupies [t0 + k x sub_ns, t0 + (k+1) x sub_ns) with
sub_ns = 1 / (2 x rate); `subframes[k]` = (preamble "B"/"M"/"W" or None, 24-bit audio, V, U, C, parity_error).  The
receiver decodes and times them itself (an S/PDIF receiver resynchronises on the stream anyway).  One `pin` event with
`field="wave"` (biphase transitions counted per sub-frame: 28 bit-cell boundaries + one per 1 bit + 4 in the preamble)
per >= 100 ms of stream and when the stream stops, like the camera's pixel clock.
"""
from __future__ import annotations

import math
from typing import Any

from .base import Part
from .registry import part
from .std import _time

__all__ = ["SpdifSource", "channel_status_block", "FS_CODES"]

FS_CODES = {44100: 0b0000, 48000: 0b0010, 32000: 0b0011, 88200: 0b1000, 96000: 0b1010, 176400: 0b1100,
            192000: 0b1110}
SIGNALS = ("sine", "ramp", "counter", "silence")
WAVE_NS = 100e6


def channel_status_block(rate: int, nonaudio: bool = False, channel: int = 1) -> list[int]:
    """The 192 channel status bits (bit n = frame n) of an IEC 60958-3 consumer block."""
    cs = bytearray(24)
    cs[0] = (0x02 if nonaudio else 0) | 0x04                  # consumer, copy permitted
    cs[2] = (channel & 0xF) << 4
    cs[3] = FS_CODES.get(int(rate), 0b0001)                   # 0001 = not indicated
    return [(cs[n // 8] >> (n % 8)) & 1 for n in range(192)]


@part("spdif_source")
class SpdifSource(Part):
    """IEC 60958 stereo stream source (S/PDIF transmitter); see the module docstring."""
    PARAMS = {"rate": 48000, "signal": "sine", "freq": 1000.0, "amplitude": 0.5, "bits": 24, "nonaudio": False,
              "validity": 0, "user": 0, "parity_error_every": 0, "corrupt_at": -1, "start": "0ns", "stop": "",
              "chunk": 8}
    PORTS = {"out": "out"}
    ID_PREFIX = "SPDIF"

    def _setup(self) -> None:
        p = self.params
        self.rate = int(p["rate"])
        if not 8000 <= self.rate <= 192000:
            raise ValueError(f"part {self.id}: rate must be 8000..192000 Hz")
        sig = str(p["signal"]).strip().lower()
        if sig not in SIGNALS:
            raise ValueError(f"part {self.id}: signal must be one of {', '.join(SIGNALS)}")
        self.signal = p["signal"] = sig
        self.bits = int(p["bits"])
        if not 16 <= self.bits <= 24:
            raise ValueError(f"part {self.id}: bits must be 16..24")
        self.freq = float(p["freq"])
        self.amp = max(0.0, min(1.0, float(p["amplitude"])))
        self.nonaudio = str(p["nonaudio"]).strip().lower() in ("1", "true", "yes", "on")
        self.validity = 1 if int(p["validity"]) else 0
        self.user = int(p["user"]) & 0xFFFFFFFF
        self.perr_every = max(0, int(p["parity_error_every"]))
        self.corrupt_at = int(p["corrupt_at"])
        self.chunk = max(1, int(p["chunk"]))
        self.start_ns = _time(p["start"], f"{self.id}.start")
        self.stop_ns = _time(p["stop"], f"{self.id}.stop") if str(p["stop"] or "").strip() else None
        self.frame_ns = 1e9 / self.rate
        self.cs = (channel_status_block(self.rate, self.nonaudio, 1), channel_status_block(self.rate, self.nonaudio, 2))

    def _reset_state(self) -> None:
        super()._reset_state()
        self._out["out"] = "L"
        self.frames = 0
        self.running = False
        self._wave_since: float | None = None
        self._wave_edges = 0
        if self.board is not None:
            self._t_next = self.now() + self.start_ns
            self._t_start = self._t_next
            self._at(self._t_next, self._chunk)

    # ------------------------------------------------------------------ stream
    def sample(self, frame: int, ch: int) -> int:
        """24-bit two's complement audio of channel ch (0 = A/left, 1 = B/right) in frame `frame`."""
        full = (1 << 23) - 1
        if self.signal == "silence":
            v = 0
        elif self.signal == "counter":
            v = frame & 0xFFFFFF if ch == 0 else (~frame) & 0xFFFFFF
            return v
        elif self.signal == "ramp":
            v = int(((frame % 256) / 128.0 - 1.0) * self.amp * full)
        else:
            ph = 2 * math.pi * self.freq * frame / self.rate + (math.pi / 2 if ch else 0.0)
            v = int(round(math.sin(ph) * self.amp * full))
        drop = 24 - self.bits
        if drop:
            v = (v >> drop) << drop
        return v & 0xFFFFFF

    def subframe(self, n: int) -> tuple:
        """Sub-frame n of the stream (2 per frame): (preamble, audio24, V, U, C, parity_error)."""
        frame, ch = divmod(n, 2)
        pre = ("B" if frame % 192 == 0 else "M") if ch == 0 else "W"
        if n == self.corrupt_at:
            pre = None
        u = (self.user >> (n % 32)) & 1
        c = self.cs[ch][frame % 192]
        perr = bool(self.perr_every) and (n + 1) % self.perr_every == 0
        return (pre, self.sample(frame, ch), self.validity, u, c, perr)

    @staticmethod
    def _edges(sf: tuple) -> int:
        _pre, data, v, u, c, _pe = sf
        ones = bin(data).count("1") + v + u + c
        par = ones & 1
        return 28 + ones + par + 4

    def _receivers(self) -> list:
        b = self.board
        net = b.part_net(self.id, "out") if b is not None else None
        if net is None:
            return []
        out = []
        for _cid, chip, pin in net.members:
            for blk in getattr(chip, "peripherals", {}).values():
                acc = getattr(blk, "spdif_accepts", None)
                if acc is not None and acc(pin):
                    out.append(blk)
        return out

    def _chunk(self) -> None:
        t0 = self._t_next
        if self.stop_ns is not None and t0 >= self._t_start + self.stop_ns:
            self._finish(t0)
            return
        if not self.running:
            self.running = True
            self.emit(action="start", rate=self.rate)
        n0 = 2 * self.frames
        subs = [self.subframe(n0 + k) for k in range(2 * self.chunk)]
        sub_ns = self.frame_ns / 2
        for blk in self._receivers():
            blk.spdif_subframes(self, t0, sub_ns, subs)
        if self._wave_since is None:
            self._wave_since = t0
        self._wave_edges += sum(self._edges(s) for s in subs)
        self.frames += self.chunk
        self._t_next = t0 + self.chunk * self.frame_ns
        if self._t_next - self._wave_since >= WAVE_NS:
            self._flush_wave(self._t_next)
        self._at(self._t_next, self._chunk)

    def _finish(self, t: float) -> None:
        if self.running:
            self.running = False
            self._flush_wave(t)
            self.emit(action="stop", frames=self.frames)

    def _flush_wave(self, t: float) -> None:
        b = self.board
        if b is None or self._wave_since is None or not self._wave_edges:
            return
        net = b.part_net(self.id, "out")
        span = max(t - self._wave_since, 1.0)
        for cid, _chip, pin in (net.members if net is not None else ()):
            b.log.event("pin", t, chip=cid, pin=pin.id, field="wave", old=None, new="L", edges=int(self._wave_edges),
                        since_ns=self._wave_since, freq_hz=self._wave_edges / 2 / span * 1e9, duty=50.0)
        self._wave_since = t
        self._wave_edges = 0

    def reset(self) -> None:
        if self.board is not None and self.running:
            self._flush_wave(self.now())
        super().reset()

    def detach(self) -> None:
        if self.board is not None and self.running:
            self._flush_wave(self.now())
        super().detach()

    def state(self) -> dict:
        return {"rate": self.rate, "signal": self.signal, "frames": self.frames, "running": self.running}
