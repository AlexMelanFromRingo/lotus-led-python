"""Modes: what the strip is doing right now.

A mode is an async loop that watches a stop flag and writes colours. Modes
never own the connection and never decide when to stop — the caller does both,
which is what lets the CLI, the TUI and the scheduler drive the same code.

Because a strip shows exactly one colour at a time (it is not addressable),
every "animation" here is a colour sequence over time, not over space. Do not
port per-pixel effects from addressable projects; they cannot work.

Mirrors ``lotus-led/src/modes/``; the two implementations offer the same mode
names, the same config keys and the same behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .device import BLEDOMDevice
from .protocol import (
    MAX_PACKETS_PER_SEC,
    HWMode,
    MicEq,
    Pkt,
    cct_to_rgb,
    hsv_to_rgb,
    hw_mode_from_name,
    lerp_color,
    parse_color,
    rgb_to_hsv,
    scale_color,
)

log = logging.getLogger("lotus.modes")


class Stop:
    """Cooperative cancellation shared with whatever started the mode."""

    def __init__(self) -> None:
        self._flag = True

    def __bool__(self) -> bool:
        return self._flag

    def stop(self) -> None:
        self._flag = False

    @property
    def running(self) -> bool:
        return self._flag


def frame_delay(fps: int) -> float:
    """Frame interval, clamped to what the BLE link can actually sustain."""
    return 1.0 / max(1, min(int(fps), MAX_PACKETS_PER_SEC))


async def sleep_cancellable(seconds: float, stop: Stop) -> bool:
    """Sleep, waking early if stopped. Returns False if it was cut short.

    A mode that sleeps 30 s in one call takes 30 s to notice Ctrl-C; polling
    keeps stop latency at 50 ms without busy-waiting.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while stop.running:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(remaining, 0.05))
    return False


# ── Software animations ──────────────────────────────────────────────────────

async def run_static(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    r, g, b = parse_color(cfg.get("color", [255, 255, 255]))
    await dev.set_color(r, g, b)
    if cfg.get("brightness") is not None:
        await dev.set_brightness(int(cfg["brightness"]))


async def run_pulse(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Breathing brightness at a fixed hue.

    ``sin**2`` spends more time near the extremes than a triangle wave, which
    reads as a breath rather than a ramp.
    """
    import math

    r, g, b = parse_color(cfg.get("color", [255, 100, 30]))
    period = max(0.1, float(cfg.get("period_secs", 3.0)))
    lo = int(cfg.get("min_brightness", 5))
    hi = int(cfg.get("max_brightness", 100))
    lo, hi = min(lo, hi), max(lo, hi)
    dt = frame_delay(cfg.get("fps", 20))

    await dev.set_color(r, g, b)
    t = 0.0
    while stop.running:
        phase = math.sin(math.pi * t / period) ** 2
        await dev.set_brightness(round(lo + (hi - lo) * phase))
        await asyncio.sleep(dt)
        t = (t + dt) % (period * 2)


async def run_rainbow(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    cycle = max(0.1, float(cfg.get("cycle_secs", 10.0)))
    sat = float(cfg.get("saturation", 1.0))
    val = float(cfg.get("value", 1.0))
    dt = frame_delay(cfg.get("fps", 20))
    step = dt / cycle
    hue = 0.0
    while stop.running:
        await dev.stream_color(*hsv_to_rgb(hue, sat, val))
        hue = (hue + step) % 1.0
        await asyncio.sleep(dt)


async def run_wave(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Hue sweeping back and forth across a narrow span.

    A full rainbow is restless as ambient light; a wave around one colour is not.
    """
    import math

    cycle = max(0.1, float(cfg.get("cycle_secs", 5.0)))
    sat = float(cfg.get("saturation", 1.0))
    val = float(cfg.get("value", 1.0))
    span = min(max(float(cfg.get("hue_span", 0.15)), 0.0), 1.0)
    center = float(cfg.get("hue_center", 0.6))
    dt = frame_delay(cfg.get("fps", 20))
    t = 0.0
    while stop.running:
        phase = math.sin(2 * math.pi * t / cycle)
        await dev.stream_color(*hsv_to_rgb(center + phase * span * 0.5, sat, val))
        t = (t + dt) % cycle
        await asyncio.sleep(dt)


async def run_fire(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Flicker between an ember colour and a flame colour.

    Low-pass filtered so the flame gutters instead of strobing.
    """
    cool = parse_color(cfg.get("cool_color", [120, 20, 0]))
    hot = parse_color(cfg.get("hot_color", [255, 170, 40]))
    intensity = min(max(float(cfg.get("intensity", 0.85)), 0.0), 2.0)
    dt = frame_delay(cfg.get("fps", 15))
    level = 0.5
    while stop.running:
        level += (random.random() - level) * 0.35 * intensity
        await dev.stream_color(*lerp_color(cool, hot, min(max(level, 0.0), 1.0)))
        await asyncio.sleep(dt)


async def run_meteor(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """A sharp flash that decays exponentially, then repeats."""
    import math

    base = parse_color(cfg.get("color", [200, 150, 255]))
    period = max(0.2, float(cfg.get("period_secs", 2.0)))
    dt = frame_delay(cfg.get("fps", 20))
    t = 0.0
    while stop.running:
        level = math.exp(-4.0 * (t / period))
        await dev.stream_color(*scale_color(base, level * 100.0))
        t = (t + dt) % period
        await asyncio.sleep(dt)


async def run_comet(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Like meteor, with a softer tail and occasional sparkles."""
    base = parse_color(cfg.get("color", [100, 200, 255]))
    period = max(0.2, float(cfg.get("period_secs", 3.0)))
    dt = frame_delay(cfg.get("fps", 20))
    t = 0.0
    while stop.running:
        phase = t / period
        level = 0.25 + 0.75 * (1.0 - phase) ** 2
        if random.randrange(23) == 0:
            level = 1.0
        await dev.stream_color(*scale_color(base, min(max(level, 0.0), 1.0) * 100.0))
        t = (t + dt) % period
        await asyncio.sleep(dt)


async def _run_ramp(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop, reverse: bool) -> None:
    total = max(1, int(cfg.get("duration_secs", 1200)))
    dt = frame_delay(cfg.get("fps", 2))
    await dev.power_on()
    elapsed = 0.0
    while stop.running and elapsed <= total:
        p = min(max(elapsed / total, 0.0), 1.0)
        if reverse:
            p = 1.0 - p
        # 1200 K at the horizon through 5000 K at full daylight.
        await dev.set_color(*cct_to_rgb(1200 + int(p * 3800)))
        # Brightness ramps faster than colour early on, matching real dawn.
        await dev.set_brightness(round(5 + 95 * (p ** 0.7)))
        await asyncio.sleep(dt)
        elapsed += dt


async def run_sunrise(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    await _run_ramp(dev, cfg, stop, reverse=False)


async def run_sunset(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    await _run_ramp(dev, cfg, stop, reverse=True)
    if stop.running:
        await dev.power_off()


async def run_sleep_timer(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Hold the current colour and dim to nothing, then power off."""
    total = max(1, int(cfg.get("duration_secs", 1800)))
    dt = frame_delay(cfg.get("fps", 1))
    start = max(1, dev.state.brightness)
    elapsed = 0.0
    while stop.running and elapsed <= total:
        p = min(max(elapsed / total, 0.0), 1.0)
        await dev.set_brightness(round(start * (1.0 - p)))
        await asyncio.sleep(dt)
        elapsed += dt
    if stop.running:
        await dev.power_off()


async def run_cct(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    await dev.set_color(*cct_to_rgb(int(cfg.get("kelvin", 4000))))
    await dev.set_brightness(int(cfg.get("brightness", 80)))


async def run_flash(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Flash a colour, optionally putting the previous one back.

    Alarm and notification share this; they differ only in how insistent the
    numbers in the config are.
    """
    color = parse_color(cfg.get("color", [255, 200, 50]))
    count = int(cfg.get("flash_count", 6))
    gap = max(0.04, int(cfg.get("flash_ms", 250)) / 1000.0)
    restore = bool(cfg.get("restore", True))

    before = dev.state
    prior = (before.r, before.g, before.b, before.brightness, before.power, before.known)

    await dev.power_on()
    for _ in range(count):
        if not stop.running:
            break
        await dev.set_color(*color)
        await asyncio.sleep(gap)
        await dev.set_color(0, 0, 0)
        await asyncio.sleep(gap)

    if restore and prior[5]:
        await dev.set_color(prior[0], prior[1], prior[2])
        await dev.set_brightness(prior[3])
        if not prior[4]:
            await dev.power_off()


# ── Firmware-native ──────────────────────────────────────────────────────────

async def run_hardware(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Select a firmware animation and let the strip get on with it."""
    name = cfg.get("mode", "cross_fade_7_color")
    mode = hw_mode_from_name(str(name))
    if mode is None:
        raise ValueError(f"unknown firmware animation '{name}'")
    if cfg.get("brightness") is not None:
        await dev.set_brightness(int(cfg["brightness"]))
    await dev.set_hw_mode(mode, int(cfg.get("speed", 50)))


async def run_mic_hardware(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Hand control to the controller's own microphone.

    The strip reacts on its own — no audio flows through the PC, so it keeps
    working after the host disconnects.
    """
    eq_name = str(cfg.get("eq", "classic")).upper()
    try:
        eq = MicEq[eq_name]
    except KeyError:
        raise ValueError(
            f"unknown mic eq '{eq_name.lower()}' — try classic, soft, dynamic or disco"
        ) from None
    await dev.set_mic_eq(eq)
    await dev.set_mic_sensitivity(int(cfg.get("sensitivity", 70)))
    await dev.set_mic(True)


# ── Audio reactive ───────────────────────────────────────────────────────────

async def run_audio(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Blend three colours by which part of the spectrum dominates."""
    from .audio import AudioStream, PeakFollower, Spectrum, smooth

    low = parse_color(cfg.get("low_color", [255, 0, 0]))
    mid = parse_color(cfg.get("mid_color", [0, 255, 0]))
    high = parse_color(cfg.get("high_color", [0, 100, 255]))
    bands = cfg.get("bands_hz", [20.0, 250.0, 4000.0, 16000.0])
    sens = min(max(float(cfg.get("sensitivity", 1.0)), 0.1), 10.0)
    smoothing = float(cfg.get("smoothing", 0.45))
    floor = float(cfg.get("noise_floor", 0.02))
    dt = frame_delay(cfg.get("fps", 20))

    with AudioStream(str(cfg.get("source", "loopback"))) as stream:
        print(f"[audio] listening on {stream.source_name}")
        spectrum = Spectrum(stream.sample_rate)
        followers = [PeakFollower(), PeakFollower(), PeakFollower()]
        levels = [0.0, 0.0, 0.0]

        while stop.running:
            if spectrum.analyze(stream.samples()):
                raw = [
                    followers[0].normalize(spectrum.band(bands[0], bands[1])) * sens,
                    followers[1].normalize(spectrum.band(bands[1], bands[2])) * sens,
                    followers[2].normalize(spectrum.band(bands[2], bands[3])) * sens,
                ]
                levels = [smooth(prev, min(now, 1.0), smoothing) for prev, now in zip(levels, raw)]
                total = sum(levels)

                if total <= floor:
                    await dev.stream_color(0, 0, 0)
                else:
                    blended = tuple(
                        min(255, round(sum(c[i] * l for c, l in zip((low, mid, high), levels)) / total))
                        for i in range(3)
                    )
                    # Loudness drives brightness, with a floor so it never goes
                    # fully dark mid-track.
                    loudness = min(total / 3.0, 1.0)
                    await dev.stream_color(*scale_color(blended, 15 + 85 * loudness))
            await asyncio.sleep(dt)


async def run_music(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Flash on detected beats and decay back to an idle colour between them."""
    from .audio import AudioStream, BeatDetector, Spectrum

    beat_color = parse_color(cfg.get("beat_color", [255, 220, 0]))
    idle_color = parse_color(cfg.get("idle_color", [50, 0, 120]))
    band = cfg.get("beat_band_hz", [40.0, 200.0])
    sens = min(max(float(cfg.get("sensitivity", 1.2)), 0.1), 10.0)
    decay = min(max(float(cfg.get("decay", 0.82)), 0.1), 0.99)
    rainbow = bool(cfg.get("rainbow_beats", False))
    dt = frame_delay(cfg.get("fps", 20))

    detector = BeatDetector(
        threshold=float(cfg.get("beat_threshold", 1.35)),
        min_gap_s=int(cfg.get("min_beat_gap_ms", 120)) / 1000.0,
    )

    with AudioStream(str(cfg.get("source", "loopback"))) as stream:
        print(f"[audio] listening on {stream.source_name}")
        spectrum = Spectrum(stream.sample_rate)
        envelope = 0.0
        hue = 0.0

        while stop.running:
            if spectrum.analyze(stream.samples()):
                energy = spectrum.band(band[0], band[1]) * sens
                if detector.feed(energy, time.monotonic()):
                    envelope = 1.0
                    if rainbow:
                        hue = (hue + 0.13) % 1.0
                envelope *= decay
                flash = hsv_to_rgb(hue, 1.0, 1.0) if rainbow else beat_color
                await dev.stream_color(*lerp_color(idle_color, flash, envelope))
            await asyncio.sleep(dt)


# ── Screen ambient ───────────────────────────────────────────────────────────

def _average_region(frame, region: str, step: int):
    """Mean colour of a region of a captured frame (numpy array, BGR)."""
    import numpy as np

    height, width = frame.shape[:2]
    band_v = max(16, height // 8)
    band_h = max(16, width // 8)
    s = max(1, step)

    if region == "full":
        pixels = frame[::s, ::s]
    elif region == "center":
        pixels = frame[height // 4 : height * 3 // 4 : s, width // 4 : width * 3 // 4 : s]
    elif region == "top":
        pixels = frame[:band_v:s, ::s]
    elif region == "bottom":
        pixels = frame[-band_v::s, ::s]
    else:  # edges
        parts = [
            frame[:band_v:s, ::s].reshape(-1, 3),
            frame[-band_v::s, ::s].reshape(-1, 3),
            frame[band_v:-band_v:s, :band_h:s].reshape(-1, 3),
            frame[band_v:-band_v:s, -band_h::s].reshape(-1, 3),
        ]
        pixels = np.concatenate([p for p in parts if p.size])

    pixels = pixels.reshape(-1, 3)
    if pixels.size == 0:
        return None
    mean = pixels.mean(axis=0)
    # mss hands back BGRA, so the channels arrive reversed.
    return int(mean[2]), int(mean[1]), int(mean[0])


def _grade(rgb, saturation_boost: float, value_boost: float, black_threshold: int):
    """Push saturation and value so an averaged colour still reads on the wall.

    Near-black is clamped to black rather than amplifying sensor noise into a
    random hue.
    """
    r, g, b = rgb
    luma = (r * 299 + g * 587 + b * 114) // 1000
    if luma <= black_threshold:
        return 0, 0, 0
    h, s, v = rgb_to_hsv(r, g, b)
    return hsv_to_rgb(h, min(1.0, s * max(0.0, saturation_boost)), min(1.0, v * max(0.0, value_boost)))


async def run_ambient(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Average the screen and mirror it to the strip."""
    try:
        import mss
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Ambient mode needs mss and numpy. Install them with: pip install mss numpy"
        ) from exc

    region = str(cfg.get("region", "edges"))
    sat = float(cfg.get("saturation_boost", 1.4))
    val = float(cfg.get("value_boost", 1.0))
    smoothing = min(max(float(cfg.get("smoothing", 0.6)), 0.0), 0.95)
    step = int(cfg.get("sample_step", 4))
    black = int(cfg.get("black_threshold", 8))
    dt = frame_delay(cfg.get("fps", 15))
    monitor_index = cfg.get("monitor")

    with mss.mss() as sct:
        # monitors[0] is the union of all displays; [1] is the primary.
        index = 1 if monitor_index is None else int(monitor_index) + 1
        if index >= len(sct.monitors):
            raise RuntimeError(
                f"display {monitor_index} does not exist — {len(sct.monitors) - 1} available"
            )
        monitor = sct.monitors[index]
        print(f"[ambient] sampling display {monitor['width']}x{monitor['height']}")

        previous = None
        while stop.running:
            try:
                frame = np.asarray(sct.grab(monitor))[:, :, :3]
            except Exception as exc:  # noqa: BLE001 - transient during mode changes
                log.warning("capture failed: %s", exc)
                await asyncio.sleep(dt)
                continue

            raw = _average_region(frame, region, step)
            if raw is None:
                await asyncio.sleep(dt)
                continue
            target = _grade(raw, sat, val, black)

            # Smooth in float space; rounding every frame quantises slow fades
            # into visible steps.
            if previous is None:
                previous = tuple(float(c) for c in target)
            else:
                previous = tuple(
                    p * smoothing + t * (1.0 - smoothing) for p, t in zip(previous, target)
                )
            await dev.stream_color(*(round(c) for c in previous))
            await asyncio.sleep(dt)


# ── System monitor ───────────────────────────────────────────────────────────

async def run_system(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Machine load as colour: green when idle, red when pegged."""
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError(
            "System monitor needs psutil. Install it with: pip install psutil"
        ) from exc

    metric = str(cfg.get("metric", "cpu")).lower()
    low = parse_color(cfg.get("low_color", [0, 200, 0]))
    high = parse_color(cfg.get("high_color", [255, 0, 0]))
    dt = frame_delay(cfg.get("fps", 2))

    # The first CPU reading is meaningless — usage is measured between calls.
    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.2)

    while stop.running:
        if metric == "ram":
            load = psutil.virtual_memory().percent / 100.0
        else:
            load = psutil.cpu_percent(interval=None) / 100.0
        load = min(max(load, 0.0), 1.0)
        await dev.stream_color(*lerp_color(low, high, load))
        # Brighter under load, so it reads from the corner of your eye.
        await dev.set_brightness(round(40 + 60 * load))
        await asyncio.sleep(dt)


# ── Sequence ─────────────────────────────────────────────────────────────────

async def run_sequence(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    steps: List[Dict[str, Any]] = list(cfg.get("steps", []))
    if not steps:
        raise ValueError(
            "Sequence has no steps — add some under modes.sequence.steps in config.json"
        )
    loop_forever = bool(cfg.get("loop_forever", True))

    while stop.running:
        for step in steps:
            if not stop.running:
                return
            if step.get("off"):
                await dev.power_off()
            elif step.get("color") or step.get("hw_mode"):
                await dev.power_on()
            if step.get("brightness") is not None:
                await dev.set_brightness(int(step["brightness"]))
            if step.get("color"):
                await dev.set_color(*parse_color(step["color"]))
            if step.get("hw_mode"):
                mode = hw_mode_from_name(str(step["hw_mode"]))
                if mode is None:
                    raise ValueError(f"unknown firmware animation '{step['hw_mode']}'")
                await dev.set_hw_mode(mode, int(step.get("speed", 50)))
            elif step.get("speed") is not None:
                await dev.set_speed(int(step["speed"]))
            if step.get("raw"):
                await dev.send(Pkt.raw(step["raw"]))
            if not await sleep_cancellable(float(step.get("duration_secs", 1.0)), stop):
                return
        if not loop_forever:
            return


# ── Process watchers ─────────────────────────────────────────────────────────

def foreground_process() -> str:
    """Name of the process owning the foreground window, or empty if unknown."""
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return ""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 - the window can vanish between calls
        return ""


def any_process_matches(keywords: List[str]) -> bool:
    """Whether any running process name contains one of ``keywords``."""
    try:
        import psutil
    except ImportError:
        return False
    lowered = [k.lower() for k in keywords]
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if any(k in name for k in lowered):
            return True
    return False


async def run_appwatch(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop) -> None:
    """Colour follows whichever application is in front."""
    rules = list(cfg.get("rules", []))
    default = parse_color(cfg.get("default_color", [80, 80, 80]))
    interval = max(0.1, int(cfg.get("check_ms", 1000)) / 1000.0)

    last = None
    while stop.running:
        name = foreground_process()
        if name != last:
            last = name
            lowered = name.lower()
            hit = next((r for r in rules if str(r.get("process", "")).lower() in lowered), None)
            if hit is not None and hit.get("brightness") is not None:
                await dev.set_brightness(int(hit["brightness"]))
            await dev.set_color(*(parse_color(hit["color"]) if hit else default))
        if not await sleep_cancellable(interval, stop):
            return


async def _run_watcher(
    dev: BLEDOMDevice,
    cfg: Dict[str, Any],
    stop: Stop,
    full_config: Dict[str, Any],
    keywords: List[str],
    foreground_only: bool,
) -> None:
    """Run one mode while a matching process exists, another when it does not.

    ``foreground_only`` distinguishes the two uses: a game counts if it is
    running at all, a video player only counts while you are looking at it.
    """
    active_name = str(cfg.get("mode", "rainbow"))
    idle_name = str(cfg.get("idle_mode", ""))
    interval = max(1.0, float(cfg.get("check_secs", 5.0)))

    child_task: Optional[asyncio.Task] = None
    child_stop: Optional[Stop] = None
    was_active: Optional[bool] = None

    async def cancel_child() -> None:
        nonlocal child_task, child_stop
        if child_stop is not None:
            child_stop.stop()
        if child_task is not None:
            child_task.cancel()
            try:
                await child_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        child_task, child_stop = None, None

    try:
        while stop.running:
            if foreground_only:
                fg = foreground_process().lower()
                active = bool(fg) and any(k.lower() in fg for k in keywords)
            else:
                active = any_process_matches(keywords)

            if active != was_active:
                was_active = active
                await cancel_child()
                name = active_name if active else idle_name
                if name:
                    child_stop = Stop()
                    child_task = asyncio.create_task(
                        run_mode(name, dev, full_config, child_stop)
                    )
            if not await sleep_cancellable(interval, stop):
                break
    finally:
        await cancel_child()


async def run_game(dev, cfg, stop, full_config) -> None:
    await _run_watcher(dev, cfg, stop, full_config, list(cfg.get("keywords", [])), False)


async def run_video(dev, cfg, stop, full_config) -> None:
    await _run_watcher(dev, cfg, stop, full_config, list(cfg.get("players", [])), True)


# ── Schedule ─────────────────────────────────────────────────────────────────

async def run_schedule(dev: BLEDOMDevice, cfg: Dict[str, Any], stop: Stop, full_config: Dict[str, Any]) -> None:
    """Fire time-of-day actions.

    Applies whichever entry is already in effect on startup, so the strip is
    correct immediately rather than at the next entry.
    """
    from .config import schedule_minutes

    entries = []
    for entry in full_config.get("schedule", {}).get("entries", []):
        minute = schedule_minutes(str(entry.get("time", "")))
        if minute is None:
            log.warning("skipping schedule entry with bad time %r", entry.get("time"))
            continue
        entries.append((minute, str(entry.get("action", ""))))
    if not entries:
        raise ValueError(
            "Schedule is empty — add entries under schedule.entries in config.json"
        )
    entries.sort()

    child_task: Optional[asyncio.Task] = None
    child_stop: Optional[Stop] = None

    async def apply(action: str) -> None:
        nonlocal child_task, child_stop
        if child_stop is not None:
            child_stop.stop()
        if child_task is not None:
            child_task.cancel()
            try:
                await child_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            child_task = None

        verb, _, arg = action.partition(":")
        verb, arg = verb.strip(), arg.strip()
        try:
            if verb == "on":
                await dev.power_on()
            elif verb == "off":
                await dev.power_off()
            elif verb == "brightness":
                await dev.set_brightness(int(arg))
            elif verb == "color":
                await dev.power_on()
                await dev.set_color(*parse_color(arg))
            elif verb in ("mode", "scene"):
                name = arg
                if verb == "scene":
                    scene = full_config.get("scenes", {}).get(arg)
                    if not scene:
                        log.warning("unknown scene %r in schedule", arg)
                        return
                    name = scene.get("mode", "static")
                await dev.power_on()
                child_stop = Stop()
                child_task = asyncio.create_task(run_mode(name, dev, full_config, child_stop))
            else:
                log.warning("unknown schedule action %r", action)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the clock
            log.warning("schedule action %r failed: %s", action, exc)

    def now_minute() -> int:
        now = datetime.now()
        return now.hour * 60 + now.minute

    start = now_minute()
    current = next((a for m, a in reversed(entries) if m <= start), entries[-1][1])
    await apply(current)

    last = start
    try:
        while stop.running:
            if not await sleep_cancellable(20.0, stop):
                break
            minute = now_minute()
            if minute == last:
                continue
            # Catch every entry in (last, minute], handling the midnight wrap.
            for m, action in entries:
                fired = (last < m <= minute) if last < minute else (m > last or m <= minute)
                if fired:
                    await apply(action)
            last = minute
    finally:
        if child_stop is not None:
            child_stop.stop()
        if child_task is not None:
            child_task.cancel()


# ── Registry ─────────────────────────────────────────────────────────────────

#: Mode name to (config section, runner). Aliases share a section.
MODE_REGISTRY: Dict[str, tuple] = {
    "static": ("static", run_static),
    "solid": ("static", run_static),
    "color": ("static", run_static),
    "pulse": ("pulse", run_pulse),
    "breathe": ("pulse", run_pulse),
    "rainbow": ("rainbow", run_rainbow),
    "wave": ("wave", run_wave),
    "fire": ("fire", run_fire),
    "meteor": ("meteor", run_meteor),
    "comet": ("comet", run_comet),
    "sunrise": ("sunrise", run_sunrise),
    "wake": ("sunrise", run_sunrise),
    "wakeup": ("sunrise", run_sunrise),
    "sunset": ("sunset", run_sunset),
    "sleep": ("sleep_timer", run_sleep_timer),
    "sleep_timer": ("sleep_timer", run_sleep_timer),
    "cct": ("cct", run_cct),
    "white": ("cct", run_cct),
    "daylight": ("cct", run_cct),
    "alarm": ("alarm", run_flash),
    "notify": ("notification", run_flash),
    "notification": ("notification", run_flash),
    "hw": ("hardware", run_hardware),
    "hardware": ("hardware", run_hardware),
    "mic_hw": ("mic_hardware", run_mic_hardware),
    "mic": ("mic_hardware", run_mic_hardware),
    "audio": ("audio", run_audio),
    "spectrum": ("audio", run_audio),
    "music": ("music", run_music),
    "beat": ("music", run_music),
    "ambient": ("ambient", run_ambient),
    "ambilight": ("ambient", run_ambient),
    "screen": ("ambient", run_ambient),
    "system": ("system", run_system),
    "cpu": ("system", run_system),
    "ram": ("system", run_system),
    "sequence": ("sequence", run_sequence),
    "appwatch": ("appwatch", run_appwatch),
    "app_watch": ("appwatch", run_appwatch),
    "game": ("game", run_game),
    "video": ("video", run_video),
    "movie": ("video", run_video),
    "schedule": ("schedule", run_schedule),
}

#: Canonical names, in display order. Aliases above resolve to these.
MODE_NAMES = [
    "static", "pulse", "rainbow", "wave", "fire", "meteor", "comet",
    "sunrise", "sunset", "sleep_timer", "cct", "alarm", "notify",
    "hardware", "mic_hw",
    "audio", "music", "ambient", "system",
    "sequence", "appwatch", "game", "video", "schedule",
]

MODE_DESCRIPTIONS = {
    "static": "Solid colour",
    "pulse": "Breathing brightness",
    "rainbow": "Full-spectrum hue cycle",
    "wave": "Gentle hue oscillation",
    "fire": "Warm flicker",
    "meteor": "Bright streak, slow decay",
    "comet": "Sparkling sweep",
    "sunrise": "Slow warm-up to daylight",
    "sunset": "Slow fade to dark",
    "sleep_timer": "Dim to off over a set time",
    "cct": "White at a colour temperature",
    "alarm": "Insistent flashing",
    "notify": "Brief flash, then restore",
    "hardware": "Firmware animation (runs without the PC)",
    "mic_hw": "Strip's own microphone (runs without the PC)",
    "audio": "Spectrum -> colour",
    "music": "Beat detection",
    "ambient": "Match the screen (Ambilight)",
    "system": "CPU/RAM load heatmap",
    "sequence": "Scripted colour steps",
    "appwatch": "Colour follows the foreground app",
    "game": "Auto-switch when a game runs",
    "video": "Auto-switch when a video player runs",
    "schedule": "Time-of-day actions",
}


def resolve_mode(name: str, config: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None):
    """Look up a mode by name and build its settings.

    Returns ``(runner, settings, needs_full_config)``. Raises
    :class:`ValueError` naming the alternatives when the name is unknown.
    """
    key = name.strip().lower()

    hw = hw_mode_from_name(key)
    if hw is not None and key not in MODE_REGISTRY:
        settings = dict(config["modes"].get("hardware", {}))
        settings["mode"] = hw.mode_name
        settings.update(overrides or {})
        return run_hardware, settings, False

    if key not in MODE_REGISTRY:
        raise ValueError(
            f"unknown mode '{name}'. Try one of: {', '.join(MODE_NAMES)}, "
            "or a firmware animation such as strobe_7_color."
        )

    section, runner = MODE_REGISTRY[key]
    settings = dict(config["modes"].get(section, {}))
    if key == "static":
        settings.setdefault("color", config["defaults"]["color"])
    if key == "ram":
        settings["metric"] = "ram"
    settings.update(overrides or {})
    needs_full = runner in (run_game, run_video, run_schedule)
    return runner, settings, needs_full


async def run_mode(
    name: str,
    dev: BLEDOMDevice,
    config: Dict[str, Any],
    stop: Stop,
    overrides: Optional[Dict[str, Any]] = None,
) -> None:
    """Resolve and run a mode until ``stop`` is cleared."""
    runner, settings, needs_full = resolve_mode(name, config, overrides)
    if needs_full:
        await runner(dev, settings, stop, config)
    else:
        await runner(dev, settings, stop)


def is_self_running(name: str) -> bool:
    """Whether the strip keeps going once told, with no PC involvement.

    Firmware animations and the on-board mic are set-and-forget: there is
    nothing to hold open and powering off on exit would defeat the point.
    """
    key = name.strip().lower()
    if hw_mode_from_name(key) is not None and key not in MODE_REGISTRY:
        return True
    section = MODE_REGISTRY.get(key, (None, None))[0]
    return section in ("hardware", "mic_hardware")
