"""Audio capture and spectral analysis.

Two pieces: getting samples off the machine, and turning them into a level.

Capture
-------
On Windows, "react to what I am listening to" means capturing the *render*
endpoint. ``sounddevice``/PortAudio cannot do that — it only opens capture
endpoints, so asking it for loopback lands on a microphone unless the user has
enabled the legacy "Stereo Mix" device, which is off by default on Windows 11.
``soundcard`` opens the render endpoint through WASAPI loopback directly, which
is what this module prefers. Nothing is *routed* anywhere, only observed, so
this cannot hijack an output the way the vendor phone app does.

Analysis
--------
A windowed real FFT via numpy. Band levels are normalised against a decaying
per-band peak, so the effect looks the same whether the system volume is at 10%
or 90% — a fixed scale factor is the usual reason these modes "stop working"
after a volume change.

Kept in step with ``lotus-led/src/modes/audio.rs``.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Tuple

log = logging.getLogger("lotus.audio")

FFT_SIZE = 2048

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:  # pragma: no cover - exercised only on bare installs
    np = None  # type: ignore[assignment]
    HAS_NUMPY = False


class AudioUnavailable(RuntimeError):
    """Raised when no usable capture path exists, with what to do about it."""


class AudioStream:
    """A running capture feeding the most recent samples into a shared buffer.

    Capture lives on its own thread: the backends block, and on Windows they
    want their own COM apartment — importing ``sounddevice`` on the asyncio
    thread flips it to STA and breaks ``bleak``'s WinRT calls, which is a real
    bug this codebase has hit before.
    """

    def __init__(self, source: str = "loopback", sample_rate: int = 48000):
        if not HAS_NUMPY:
            raise AudioUnavailable(
                "Audio modes need numpy. Install it with: pip install numpy"
            )
        self.sample_rate = sample_rate
        self.source_name = "?"
        self._buffer = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[str] = None

        self._thread = threading.Thread(
            target=self._run, args=(source,), name="lotus-audio", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=8.0)
        if self._error:
            raise AudioUnavailable(self._error)
        if not self._ready.is_set():
            raise AudioUnavailable("timed out opening the audio input")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> "AudioStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def samples(self):
        """Most recent mono samples, oldest first."""
        with self._lock:
            return self._buffer.copy()

    def _push(self, chunk) -> None:
        if chunk.ndim > 1:
            # Downmix: a stereo image tells us nothing a single strip can show.
            chunk = chunk.mean(axis=1)
        chunk = chunk.astype(np.float32, copy=False)
        with self._lock:
            merged = np.concatenate([self._buffer, chunk])
            if merged.size > FFT_SIZE * 2:
                merged = merged[-FFT_SIZE:]
            self._buffer = merged

    # ── Backends ─────────────────────────────────────────────────────────────

    def _run(self, source: str) -> None:
        if source == "loopback":
            if self._try_soundcard_loopback():
                return
            log.warning("no WASAPI loopback; falling back to an input device")
        self._try_sounddevice(source)

    def _try_soundcard_loopback(self) -> bool:
        """Genuine system-audio capture. Windows only in practice."""
        try:
            import soundcard  # noqa: PLC0415 - deliberately lazy, see class docs
        except ImportError:
            self._error = None
            return False

        try:
            speaker = soundcard.default_speaker()
            mic = soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
            self.source_name = f"WASAPI loopback: {speaker.name}"
            with mic.recorder(samplerate=self.sample_rate, channels=2, blocksize=512) as rec:
                self._ready.set()
                while not self._stop.is_set():
                    self._push(rec.record(numframes=512))
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means try the fallback
            log.debug("soundcard loopback unavailable: %s", exc)
            if self._ready.is_set():
                # It worked and then died; do not silently switch backends.
                self._error = f"loopback capture stopped: {exc}"
                return True
            return False

    def _try_sounddevice(self, source: str) -> None:
        try:
            import sounddevice as sd  # noqa: PLC0415 - lazy: Pa_Initialize sets COM state
        except ImportError:
            self._error = (
                "No audio backend. Install one with: pip install soundcard sounddevice"
            )
            self._ready.set()
            return

        device = None
        label = "default input"
        try:
            if source == "loopback":
                hints = ("monitor", "loopback", "stereo mix", "what u hear", "wave out mix")
                for index, info in enumerate(sd.query_devices()):
                    if info["max_input_channels"] < 1:
                        continue
                    name = info["name"].lower()
                    if any(h in name for h in hints):
                        device, label = index, f"monitor input: {info['name']}"
                        break
                if device is None:
                    label = "microphone (fallback)"
                    log.warning(
                        "no system-audio monitor found — listening to the microphone, "
                        "which hears the room, not the PC. Install 'soundcard' for real "
                        "loopback on Windows."
                    )
            else:
                label = "microphone"

            self.source_name = label

            def callback(indata, frames, time_info, status):  # noqa: ARG001
                self._push(indata)

            kwargs = dict(
                samplerate=self.sample_rate, channels=1, blocksize=512, callback=callback
            )
            if device is not None:
                kwargs["device"] = device
            with sd.InputStream(**kwargs):
                self._ready.set()
                self._stop.wait()
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            self._error = f"cannot open audio input: {exc}"
            self._ready.set()


# ── Analysis ─────────────────────────────────────────────────────────────────

class Spectrum:
    """Windowed real FFT with reusable buffers."""

    def __init__(self, sample_rate: float):
        self.sample_rate = float(sample_rate)
        # Hann window: without one, a tone that does not fit the frame smears
        # energy across every band and the colours turn to mud.
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        self._magnitudes = np.zeros(FFT_SIZE // 2 + 1, dtype=np.float32)
        self._bin_hz = self.sample_rate / FFT_SIZE

    def analyze(self, samples) -> bool:
        """Recompute magnitudes from the tail of ``samples``.

        Returns False when there is not enough audio yet.
        """
        if samples is None or samples.size < FFT_SIZE // 2:
            return False
        tail = samples[-FFT_SIZE:]
        frame = np.zeros(FFT_SIZE, dtype=np.float32)
        frame[: tail.size] = tail * self._window[: tail.size]
        self._magnitudes = np.abs(np.fft.rfft(frame)).astype(np.float32) * (2.0 / FFT_SIZE)
        return True

    def band(self, lo_hz: float, hi_hz: float) -> float:
        """Mean magnitude across a frequency band."""
        lo = max(1, int(lo_hz / self._bin_hz))
        hi = min(self._magnitudes.size - 1, int(np.ceil(hi_hz / self._bin_hz)))
        if lo >= hi:
            return 0.0
        return float(self._magnitudes[lo:hi].mean())


class PeakFollower:
    """Normalise a band against its own decaying peak.

    Makes the visual response independent of system volume, and lets quiet
    passages after a loud one still show detail.
    """

    def __init__(self, decay: float = 0.995):
        self.peak = 1e-4
        self.decay = decay

    def normalize(self, value: float) -> float:
        self.peak = max(self.peak * self.decay, value, 1e-5)
        return min(max(value / self.peak, 0.0), 1.0)


def smooth(previous: float, target: float, amount: float) -> float:
    """One-pole smoothing; ``amount`` 0.0 (instant) to ~0.95 (very slow)."""
    a = min(max(amount, 0.0), 0.95)
    return previous * a + target * (1.0 - a)


class BeatDetector:
    """Flag bass transients that stand out from the recent running average."""

    def __init__(self, threshold: float = 1.35, min_gap_s: float = 0.12, history: int = 43):
        self.threshold = threshold
        self.min_gap_s = min_gap_s
        self._history: list[float] = []
        self._capacity = history
        self._last_beat = 0.0

    def feed(self, energy: float, now: float) -> bool:
        self._history.append(energy)
        if len(self._history) > self._capacity:
            self._history.pop(0)
        if len(self._history) < 8:
            return False

        # Compare against the mean of the *previous* frames; including this one
        # would let a loud beat raise its own bar.
        prior = self._history[:-1]
        mean = sum(prior) / len(prior)
        if energy > mean * self.threshold and energy > 1e-4 and now - self._last_beat >= self.min_gap_s:
            self._last_beat = now
            return True
        return False
