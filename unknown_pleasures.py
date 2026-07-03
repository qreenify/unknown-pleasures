#!/usr/bin/env python
"""unknown-pleasures — voice-reactive OSD for voxtype dictation.

Two renderers, selected by STYLE below:

  "waves"     — voice-assistant look: full-width smooth lines resting as a
                single straight center line; when you speak they ripple up
                and down, each line driven by a different frequency band of
                your voice (real FFT over a private mic stream, opened ONLY
                while a recording is active). Silence stills them.
  "ridgeline" — Joy Division 'Unknown Pleasures' stacked ridgelines driven
                by the loudness envelope alone.

Visibility is driven by voxtype's audio-frame socket
($XDG_RUNTIME_DIR/voxtype/audio.sock, 16-byte frames at 100 Hz): frames only
flow while the daemon records, so frame flow doubles as the show signal. The
state file keeps the last shape frozen during transcription, then the window
hides on idle.

Replaces the stock voxtype OSD ([osd] enabled = false in voxtype's config).
"""

import math
import os
import random
import socket
import struct
import subprocess
import threading
import time
from collections import deque

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import GLib, Gtk, Gtk4LayerShell as LayerShell

# ---- general ----------------------------------------------------------------
STYLE = "siri"           # "siri", "waves" or "ridgeline"
WIDTH, HEIGHT = 400, 100
BOTTOM_MARGIN = 396      # px from screen bottom; the band the stock OSD used on 1440p
STATE_HZ = 30            # show/hide/freeze state machine rate; rendering itself
                         # runs on the GTK frame clock at the monitor's refresh
HIDE_AFTER = 0.5         # s without frames before hiding (unless transcribing)
CORNER_RADIUS = 10.0

RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCK_PATH = os.path.join(RUNTIME_DIR, "voxtype", "audio.sock")
STATE_PATH = os.path.join(RUNTIME_DIR, "voxtype", "state")
FRAME_FMT = "=Ifff"
FRAME_BYTES = struct.calcsize(FRAME_FMT)

# ---- siri renderer ------------------------------------------------------------
SIRI_HALF = 44.0         # px, half-height of the loudest swing
SIRI_BOOST = 1.9         # loudness boost so normal speech fills the panel
SIRI_DECAY_TAU = 0.11    # s, amplitude decay time constant — shorter than the
                         # gaps between syllables, so the wave breathes as you talk
SIRI_PERIODS = 3.0       # carrier periods across the width (fixed lobes)
SIRI_LAG_MAX = 0.55      # rad, how far echoes trail/lead the main wave
SIRI_SWAY = 0.3          # rad, how far the lobes sway around their anchor
SIRI_IDLE = 0.15         # amplitude floor while on screen — never totally
                         # still: a faint slow undulation says "listening"
SIRI_RIPPLE = 0.45       # depth of the ripple radiating out from the center
SIRI_RIPPLE_K = 2.2      # ripple wavelengths between center and edge
SIRI_SPECTRUM = 0.75     # how strongly the voice spectrum reshapes the wave:
                         # bass swells the center, sibilants light the flanks
SIRI_FOCUS = 0.24        # width of the central bell (fraction of panel) —
                         # smaller = action squeezed harder into the middle
# (scale, line width, alpha, band): bold carrier + thinner mirrored echoes.
# `band` picks which frequency band flexes that echo, so speech reshapes
# the wave instead of just resizing it.
SIRI_LINES = [
    (-1.00, 1.0, 0.55, 0),
    (0.72, 0.9, 0.45, 1), (-0.72, 0.9, 0.45, 2),
    (0.45, 0.8, 0.32, 2), (-0.45, 0.8, 0.32, 3),
    (0.22, 0.8, 0.22, 3), (-0.22, 0.8, 0.22, 4),
    (1.00, 2.6, 1.00, 0),   # main line drawn last, on top
]

# ---- waves renderer ----------------------------------------------------------
N_LINES = 5              # one per frequency band, low -> high
BAND_LO, BAND_HI = 85.0, 3800.0   # Hz range split log-evenly into N_LINES bands
AMP_MAX = 38.0           # px, tallest swing of the loudest band
WAVE_PERIODS = [1.3, 1.9, 2.7, 3.6, 4.6]   # spatial periods across the width
WAVE_SPEED = [1.6, 2.1, 2.7, 3.4, 4.2]     # phase speed (rad/s) per line when active
EDGE_FLOOR = 0.35        # lines keep 35% of their swing at the very edges
ATTACK, RELEASE = 1.0, 0.86   # band smoothing: instant up, ~exponential down
NOISE_GATE_DB = -56.0    # per-band gate (true dBFS amplitude) so room hiss doesn't wiggle the lines
GATE_RANGE_DB = 38.0     # gate..gate+range maps onto 0..1 swing
MIC_RATE = 16000
FFT_N = 1024             # 64 ms analysis window

# ---- ridgeline renderer -------------------------------------------------------
N_ROWS = 12
N_PTS = 72
ROW_PERIOD = 0.22
PEAK_MAX = 34.0
GAIN = 9.0
PAD_X = 14.0
PAD_TOP, PAD_BOTTOM = 12.0, 14.0
FLAT_MARGIN = 0.12


class FrameReader(threading.Thread):
    """Reads envelope frames off the voxtype socket; reconnects forever."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.env = deque(maxlen=int(ROW_PERIOD * 100) + 5)
        self.last_frame = 0.0

    def run(self):
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(SOCK_PATH)
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= FRAME_BYTES:
                        _seq, mn, mx, _db = struct.unpack(FRAME_FMT, buf[:FRAME_BYTES])
                        buf = buf[FRAME_BYTES:]
                        with self.lock:
                            self.env.append(max(abs(mn), abs(mx)))
                            self.last_frame = time.monotonic()
            except OSError:
                pass
            time.sleep(1.0)


class MicCapture:
    """Private mic tap for spectral analysis, alive only while recording.

    PipeWire happily serves a second consumer alongside voxtype. Raw f32
    samples stream from pw-record into a ring buffer; stop() kills the
    process so the mic is released the moment recording ends.
    """

    def __init__(self):
        self.proc = None
        self.thread = None
        self.lock = threading.Lock()
        self.ring = deque(maxlen=FFT_N * 2)
        self.last_sample = 0.0

    def start(self):
        if self.proc is not None:
            return
        try:
            self.proc = subprocess.Popen(
                ["pw-record", "--raw", "--rate", str(MIC_RATE), "--channels", "1",
                 "--format", "f32", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.proc = None
            return
        self.thread = threading.Thread(target=self._read, args=(self.proc,), daemon=True)
        self.thread.start()

    def _read(self, proc):
        pending = b""
        while True:
            data = proc.stdout.read(2048)
            if not data:
                return
            pending += data
            usable = len(pending) - len(pending) % 4
            samples = np.frombuffer(pending[:usable], dtype=np.float32)
            pending = pending[usable:]  # carry partial sample to the next read
            with self.lock:
                self.ring.extend(samples.tolist())
                self.last_sample = time.monotonic()

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            self.proc = None
        with self.lock:
            self.ring.clear()

    def spectrum_bands(self):
        """RMS per log-spaced band over the newest FFT_N samples, or None."""
        with self.lock:
            if len(self.ring) < FFT_N or time.monotonic() - self.last_sample > 0.3:
                return None
            samples = np.array(list(self.ring)[-FFT_N:], dtype=np.float32)
        windowed = samples * np.hanning(FFT_N)
        power = np.abs(np.fft.rfft(windowed)) ** 2
        freqs = np.fft.rfftfreq(FFT_N, 1.0 / MIC_RATE)
        edges = np.geomspace(BAND_LO, BAND_HI, N_LINES + 1)
        bands = []
        for i in range(N_LINES):
            sel = (freqs >= edges[i]) & (freqs < edges[i + 1])
            # sum (not mean) of band power, calibrated so a full-scale sine
            # in the band reads ~1.0 regardless of band width: with a hann
            # window a sine of amplitude A leaves ~(A*N/4)^2 * 1.5 of summed
            # power in its main lobe, so amp ≈ sqrt(sum) * 3.3 / N
            amp = math.sqrt(float(power[sel].sum())) * 3.3 / FFT_N if sel.any() else 0.0
            db = 20 * math.log10(amp + 1e-12)
            if db < NOISE_GATE_DB:
                bands.append(0.0)
            else:
                bands.append(min(1.0, ((db - NOISE_GATE_DB) / GATE_RANGE_DB) ** 0.55))
        return bands


class Waves:
    """Voice-assistant renderer: full-width band-driven sinusoids."""

    def __init__(self):
        self.amps = [0.0] * N_LINES
        self.phases = [random.uniform(0, 2 * math.pi) for _ in range(N_LINES)]
        self.prev = time.monotonic()

    def reset(self):
        self.amps = [0.0] * N_LINES
        self.prev = time.monotonic()

    def tick(self, bands, fallback_amp):
        now = time.monotonic()
        dt = min(now - self.prev, 0.1)
        self.prev = now
        if bands is None:
            # mic tap unavailable: drive every line from the loudness envelope
            bands = [min(1.0, fallback_amp * GAIN) * m for m in (1.0, 0.8, 0.65, 0.5, 0.4)]
        for i in range(N_LINES):
            target = bands[i]
            if target >= self.amps[i]:
                self.amps[i] += (target - self.amps[i]) * ATTACK
            else:
                self.amps[i] *= math.exp(-dt / 0.35)  # time-based, fps-independent
            # lines drift while their band is audible, stand still in silence
            self.phases[i] += WAVE_SPEED[i] * dt * min(1.0, self.amps[i] * 6)

    def draw(self, cr, w, h):
        cy = h / 2
        steps = 100
        for i in range(N_LINES):
            amp = self.amps[i] * AMP_MAX
            cr.move_to(0, cy)
            for s in range(steps + 1):
                t = s / steps
                envelope = EDGE_FLOOR + (1 - EDGE_FLOOR) * math.sin(math.pi * t) ** 0.8
                y = cy + amp * envelope * math.sin(
                    2 * math.pi * WAVE_PERIODS[i] * t + self.phases[i]
                )
                cr.line_to(t * w, y)
            alpha = 0.95 - 0.14 * i          # bass brightest, treble faintest
            cr.set_source_rgba(1, 1, 1, alpha)
            cr.set_line_width(1.8 - 0.2 * i)
            cr.stroke()


class Siri:
    """Siri-style renderer: a bold amplitude-modulated carrier wave with
    thinner mirrored echoes, pinched at the edges, beating as it travels.

    Voice drives it three ways: overall loudness sets the swing, spectral
    balance sets how many oscillations fit and how fast they travel, and
    each echo flexes and lags with its own frequency band so the wave
    morphs with what you say rather than just resizing. All motion is
    time-based, so it renders identically at any frame rate.
    """

    def __init__(self):
        self.amp = 0.0
        self.voice = 0.0                             # recent voice presence
        self.centroid = 0.3
        self.bands = [0.0] * N_LINES
        self.phi1 = random.uniform(0, 2 * math.pi)
        self.phi2 = random.uniform(0, 2 * math.pi)
        self.phi3 = random.uniform(0, 2 * math.pi)   # echo lag oscillator
        self.prev = time.monotonic()
        self._t = np.linspace(0.0, 1.0, 140)
        # gaussian bell: the action lives in the middle and decays toward
        # both sides, like the original — edges barely whisper
        self._bell = np.exp(-((self._t - 0.5) ** 2) / (2 * SIRI_FOCUS**2))

    def reset(self):
        self.amp = 0.0
        self.bands = [0.0] * N_LINES
        self.prev = time.monotonic()

    def tick(self, bands, fallback_amp):
        now = time.monotonic()
        dt = min(now - self.prev, 0.1)
        self.prev = now
        if bands is None:
            loud = min(1.0, fallback_amp * GAIN)
            centroid = 0.3
            bands = [loud * m for m in (1.0, 0.8, 0.65, 0.5, 0.4)]
        else:
            total = sum(bands)
            loud = min(1.0, max(bands) + 0.35 * (total - max(bands)))
            centroid = (
                sum(i * b for i, b in enumerate(bands)) / (total * (N_LINES - 1))
                if total > 1e-6 else self.centroid
            )
        loud = min(1.0, (loud * SIRI_BOOST) ** 0.85)
        if loud >= self.amp:
            self.amp = loud                          # instant attack
        else:
            self.amp *= math.exp(-dt / SIRI_DECAY_TAU)
        self.amp = max(self.amp, SIRI_IDLE)          # never totally still
        self.voice = max(loud, self.voice * math.exp(-dt / 0.3))
        k = 1 - math.exp(-dt / 0.10)                 # time-based smoothing
        self.centroid += (centroid - self.centroid) * k
        for i in range(N_LINES):
            if bands[i] >= self.bands[i]:
                self.bands[i] = bands[i]
            else:
                self.bands[i] *= math.exp(-dt / SIRI_DECAY_TAU)
        # the wave doesn't travel: lobes stay anchored at the center and
        # pump with the voice. phi1 sways them, phi2 radiates ripples
        # outward from the middle, phi3 staggers the echoes. In silence
        # they slow to a faint idle drift — never totally still.
        alive = 0.25 + 0.75 * min(1.0, self.voice * 8)
        self.phi1 += (2.4 + 1.5 * self.centroid) * dt * alive
        self.phi2 += (4.0 + 5.0 * self.amp) * dt * alive   # ripple speeds up with voice
        self.phi3 += 1.9 * dt * alive

    def draw(self, cr, w, h):
        cy = h / 2
        t = self._t
        # ripple radiating outward: phase grows with distance from center,
        # so motion visually blooms from the middle toward both edges
        d = np.abs(t - 0.5)
        ripple = 1 - SIRI_RIPPLE * np.cos(2 * np.pi * SIRI_RIPPLE_K * d - self.phi2)
        # live spectrum profile across the panel: center follows the bass
        # bands, flanks follow the treble — consonants and vowels visibly
        # reshape the wave instead of just resizing it
        profile = np.interp(d * 2, [0.0, 0.25, 0.5, 0.75, 1.0], self.bands)
        spectrum = (1 - SIRI_SPECTRUM) + SIRI_SPECTRUM * (0.25 + 0.75 * profile)
        base = self.amp * SIRI_HALF * self._bell * ripple * spectrum
        sway = SIRI_SWAY * math.sin(self.phi1)
        xs = t * w
        for scale, width, alpha, band in SIRI_LINES:
            # echoes trail/lead the sway and flex with their own band
            lag = SIRI_LAG_MAX * (1 - abs(scale)) * math.sin(self.phi3 + band)
            flex = 0.65 + 0.55 * self.bands[band] if abs(scale) < 1.0 else 1.0
            # cos centered on t=0.5 puts a crest (antinode) dead-center,
            # so the biggest lobe lives in the middle like the OG
            ys = cy + scale * flex * base * np.cos(2 * np.pi * SIRI_PERIODS * (t - 0.5) + sway + lag)
            cr.move_to(xs[0], float(ys[0]))
            for x, y in zip(xs[1:], ys[1:]):
                cr.line_to(float(x), float(y))
            cr.set_source_rgba(1, 1, 1, alpha)
            cr.set_line_width(width)
            cr.stroke()


def smooth_noise(n, roughness=0.5):
    vals = [random.random() for _ in range(n)]
    out = list(vals)
    for _ in range(2):
        out = [
            (out[max(i - 1, 0)] + out[i] * 2 + out[min(i + 1, n - 1)]) / 4
            for i in range(n)
        ]
    return [(1 - roughness) * 0.5 + roughness * v for v in out]


class Ridgelines:
    """Joy Division renderer: stacked occluding ridgelines from the envelope."""

    def __init__(self):
        self.rows = deque(maxlen=N_ROWS - 1)
        self.live_noise = smooth_noise(N_PTS)
        self.next_commit = 0.0
        self.live = [0.0] * N_PTS

    def reset(self):
        self.rows.clear()
        self.live_noise = smooth_noise(N_PTS)
        self.next_commit = time.monotonic() + ROW_PERIOD

    @staticmethod
    def _shape(envelope, noise):
        n_env = len(envelope)
        pts = []
        for j in range(N_PTS):
            t = j / (N_PTS - 1)
            if t < FLAT_MARGIN or t > 1 - FLAT_MARGIN:
                window = 0.0
            else:
                u = (t - FLAT_MARGIN) / (1 - 2 * FLAT_MARGIN)
                window = math.sin(math.pi * u) ** 1.3
            if n_env:
                lo = int(t * (n_env - 1))
                amp = max(envelope[lo : min(lo + 2, n_env)])
            else:
                amp = 0.0
            v = min(1.0, (amp * GAIN) ** 0.8)
            pts.append(v * window * (0.55 + 0.45 * noise[j]) + 0.035 * noise[j] * window)
        return pts

    def tick(self, envelope, now):
        self.live = self._shape(envelope, self.live_noise)
        if now >= self.next_commit:
            self.rows.append(self.live)
            self.live_noise = smooth_noise(N_PTS)
            self.next_commit = now + ROW_PERIOD

    def draw(self, cr, w, h):
        rows = list(self.rows) + [self.live]
        n = len(rows)
        spacing = (h - PAD_TOP - PAD_BOTTOM) / (N_ROWS - 1)
        inner_w = w - 2 * PAD_X
        for i, row in enumerate(rows):
            base = PAD_TOP + (N_ROWS - n + i) * spacing
            cr.move_to(PAD_X, base)
            for j, v in enumerate(row):
                cr.line_to(PAD_X + inner_w * j / (N_PTS - 1), base - v * PEAK_MAX)
            cr.line_to(PAD_X + inner_w, h)
            cr.line_to(PAD_X, h)
            cr.close_path()
            cr.set_source_rgba(0, 0, 0, 1)
            cr.fill()
            cr.move_to(PAD_X, base)
            for j, v in enumerate(row):
                cr.line_to(PAD_X + inner_w * j / (N_PTS - 1), base - v * PEAK_MAX)
            cr.set_source_rgba(1, 1, 1, 0.55 + 0.45 * (i / max(n - 1, 1)))
            cr.set_line_width(1.1)
            cr.stroke()


class App:
    def __init__(self):
        self.reader = FrameReader()
        self.reader.start()
        self.mic = MicCapture()
        self.model = {"siri": Siri, "waves": Waves}.get(STYLE, Ridgelines)()
        self.visible = False
        self.frozen = False

        self.app = Gtk.Application(application_id="one.wonderland.unknown-pleasures")
        self.app.connect("activate", self.on_activate)

    def on_activate(self, app):
        self.win = Gtk.Window(application=app, default_width=WIDTH, default_height=HEIGHT)
        LayerShell.init_for_window(self.win)
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self.win, "unknown-pleasures")
        LayerShell.set_anchor(self.win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_margin(self.win, LayerShell.Edge.BOTTOM, BOTTOM_MARGIN)

        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            self.win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(WIDTH)
        self.area.set_content_height(HEIGHT)
        self.area.set_draw_func(self.draw)
        self.win.set_child(self.area)

        GLib.timeout_add(1000 // STATE_HZ, self.tick)
        # animation runs on the frame clock: fires at the monitor's refresh
        # rate while the window is mapped, pauses automatically when hidden
        self.area.add_tick_callback(self.on_frame)

    @staticmethod
    def read_state():
        try:
            with open(STATE_PATH) as f:
                return f.read().strip()
        except OSError:
            return "idle"

    def tick(self):
        """State machine only: show/hide/freeze and the mic tap lifecycle."""
        now = time.monotonic()
        with self.reader.lock:
            last = self.reader.last_frame
        recording = (now - last) < HIDE_AFTER

        if recording:
            if not self.visible:
                self.model.reset()
                self.frozen = False
                if STYLE in ("siri", "waves"):
                    self.mic.start()
                self.win.set_visible(True)  # maps on the focused monitor
                self.visible = True
        elif self.visible and self.read_state() == "transcribing":
            if not self.frozen:
                self.frozen = True
                self.mic.stop()   # release the mic the moment recording ends
        elif self.visible:
            self.mic.stop()
            self.win.set_visible(False)
            self.visible = False
            self.frozen = False
        return True

    def on_frame(self, widget, frame_clock):
        """Per-display-frame animation step (monitor refresh rate)."""
        if not self.visible:
            return GLib.SOURCE_CONTINUE
        if self.frozen:
            # mic is released while transcribing; the siri wave keeps a
            # gentle idle shimmer instead of freezing solid
            if STYLE == "siri":
                self.model.tick([0.0] * N_LINES, 0.0)
                self.area.queue_draw()
            return GLib.SOURCE_CONTINUE
        with self.reader.lock:
            env = list(self.reader.env)
        if STYLE in ("siri", "waves"):
            self.model.tick(self.mic.spectrum_bands(), max(env) if env else 0.0)
        else:
            self.model.tick(env, time.monotonic())
        self.area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def draw(self, area, cr, w, h):
        r = CORNER_RADIUS
        cr.new_path()
        cr.arc(r, r, r, math.pi, 1.5 * math.pi)
        cr.arc(w - r, r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(w - r, h - r, r, 0, 0.5 * math.pi)
        cr.arc(r, h - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()
        cr.set_source_rgba(0, 0, 0, 1)
        cr.fill_preserve()
        cr.clip()
        self.model.draw(cr, w, h)

    def run(self):
        self.app.run(None)


if __name__ == "__main__":
    App().run()
