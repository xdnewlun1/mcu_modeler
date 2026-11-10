// Shared formatters for the web UI (app.js, board.js, parts.js, logic.js): one way to print a time, a frequency, a
// duty cycle, a byte count. The server twins are in chipem/web/evfmt.py (fmt_time, fmt_freq); tests/test_web.py checks
// that fmtTime and evfmt.fmt_time agree.

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const hex32 = (v) => "0x" + (v >>> 0).toString(16).toUpperCase().padStart(8, "0");

// virtual time: 812 ns, 1.250 µs, 3.000 ms, 1.000000 s (= evfmt.fmt_time)
export function fmtTime(ns) {
  if (ns == null || !isFinite(ns)) return "—";
  if (ns >= 1e9) return `${(ns / 1e9).toFixed(6)} s`;
  if (ns >= 1e6) return `${(ns / 1e6).toFixed(3)} ms`;
  if (ns >= 1e3) return `${(ns / 1e3).toFixed(3)} µs`;
  return `${Math.round(ns)} ns`;
}

// fixed-width time column (transcripts): right-aligned, unit chosen like fmtTime
export const fmtTs = (ns) => (ns == null ? "".padStart(13) : fmtTime(ns).padStart(13));

// frequency: 50.0 Hz, 20.000 kHz, 1.000 MHz (= evfmt.fmt_freq); short: 3 significant decimals trimmed
export function fmtFreq(hz, short = false) {
  if (hz == null || !isFinite(hz)) return "—";
  const [v, u] = hz >= 1e6 ? [hz / 1e6, "MHz"] : hz >= 1e3 ? [hz / 1e3, "kHz"] : [hz, "Hz"];
  if (short) return `${+v.toFixed(u === "Hz" && v < 100 ? 2 : 2)} ${u}`;
  return `${v.toFixed(u === "Hz" ? 1 : 3)} ${u}`;
}

// duty cycle in percent (the `wave` contract, chipem/log.py); parts take a fraction (pulse.duty): pass frac=true
export function fmtDuty(d, frac = false) {
  if (d == null || !isFinite(d)) return "—";
  return `${(frac ? d * 100 : d).toFixed(1)}%`;
}

export function fmtBytes(n) {
  if (n == null) return "—";
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MiB`;
  if (n >= 1 << 10) return `${(n / 1024).toFixed(1)} KiB`;
  return `${n} B`;
}

export function fmtRate(bps) {
  if (!bps) return "";
  return bps >= 1000 ? `${(bps / 1000).toFixed(bps >= 1e4 ? 0 : 1)} kB/s` : `${Math.round(bps)} B/s`;
}

// pin waveform summary: "20.000 kHz 25.0%" for a periodic pin, "" otherwise
export function fmtWave(w) {
  if (!w || !w.freq_hz) return "";
  return `${fmtFreq(w.freq_hz)} ${w.duty == null ? "?" : fmtDuty(w.duty)}`;
}

// "500us", "10ms", "1.5s" -> ms (null if unparsable)
export function parseDurationMs(s) {
  const m = /^\s*([0-9]*\.?[0-9]+(?:e[-+]?\d+)?)\s*(ns|us|µs|ms|s)?\s*$/i.exec(s);
  if (!m) return null;
  const mul = { ns: 1e-6, us: 1e-3, "µs": 1e-3, ms: 1, s: 1e3 }[(m[2] || "ms").toLowerCase()];
  return parseFloat(m[1]) * mul;
}

// per-chip colour class (c-A ... c-H; ids past H wrap)
export function chipCls(cid) {
  const s = String(cid ?? "");
  if (/^[A-H]$/.test(s)) return `c-${s}`;
  if (s === "board") return "c-board";
  let h = 0;
  for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `c-${"ABCDEFGH"[h % 8]}`;
}
