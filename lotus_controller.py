#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lotus LED Controller v2.0
Full-featured PC controller for BLEDOM / ELK-BLEDOM / Lotus Lantern LED strips.

Features:
  - All hardware BLE modes (fade, strobe, jump, etc.)
  - Software modes: rainbow, pulse/breathe, wave, fire, meteor, comet, sunrise, sunset
  - Audio reactive (PC mic FFT + system audio loopback)
  - Beat detection / music sync
  - Ambilight / screen ambient
  - System monitor (CPU -> color heatmap)
  - Game / video / app detection (auto mode switch)
  - Windows notification flash
  - Alarm, wake-up, sleep timer
  - Scene presets (movie, party, relax, focus, gaming)
  - Schedule (time-based mode switching)
  - Full config file (config.json)
  - Auto-discovery of device by name pattern

Usage:
  python lotus_controller.py                  # interactive TUI
  python lotus_controller.py scan             # find devices
  python lotus_controller.py on               # power on
  python lotus_controller.py off              # power off
  python lotus_controller.py color 255 0 128  # set color
  python lotus_controller.py brightness 70    # set brightness
  python lotus_controller.py mode rainbow     # start mode
  python lotus_controller.py mode audio       # audio reactive
  python lotus_controller.py mode ambient     # ambilight
  python lotus_controller.py scene movie      # apply scene preset
  python lotus_controller.py status           # read device status
"""

# ── Standard library ──────────────────────────────────────────────────────────
import asyncio
import json
import sys
import os
import math
import time
import colorsys
import threading
import argparse
import logging
import re
import copy
from pathlib import Path

# Force UTF-8 output on Windows (handles Russian system locale / cp1251)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from enum import IntEnum
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple, Dict, Any, Callable
from abc import ABC, abstractmethod
from datetime import datetime, time as dtime

# ── BLE (required) ────────────────────────────────────────────────────────────
try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("[FATAL] bleak not installed. Run:  pip install bleak")

# ── Optional: numpy (needed for audio/screen modes) ──────────────────────────
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None

# ── Optional: audio capture ──────────────────────────────────────────────────
try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:
    HAS_SD = False
    sd = None

try:
    import soundcard as sc_lib
    HAS_SC = True
except ImportError:
    HAS_SC = False
    sc_lib = None

# ── Optional: screen capture ─────────────────────────────────────────────────
try:
    import mss as mss_lib
    HAS_MSS = True
except ImportError:
    HAS_MSS = False
    mss_lib = None

# ── Optional: system monitoring / process detection ──────────────────────────
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    psutil = None

# ── Optional: Windows API (for active window / notifications) ────────────────
try:
    import win32gui
    import win32process
    import win32api
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# ── Optional: Rich TUI ────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, IntPrompt, Confirm
    from rich.text import Text
    from rich.columns import Columns
    HAS_RICH = True
    console = Console(force_terminal=True, highlight=False)
except ImportError:
    HAS_RICH = False
    console = None

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger("lotus")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 ▸ PROTOCOL LAYER
# ══════════════════════════════════════════════════════════════════════════════

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID   = "0000fff3-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID  = "0000fff4-0000-1000-8000-00805f9b34fb"

# Device name fragments to match during scanning
DEVICE_NAME_PATTERNS = ["BLEDOM", "ELK-BLEDOM", "LEDBLE", "ELK_BLEDOM"]

# Known MAC OUI prefix for these controllers (informational hint for scanning)
KNOWN_MAC_PREFIX = "BE:60:65"

MAX_PACKETS_PER_SEC = 20


class HWMode(IntEnum):
    """Hardware animation mode IDs supported by the BLEDOM firmware."""
    JUMP_7_COLOR       = 0x81
    FADE_RED           = 0x82
    FADE_GREEN         = 0x83
    FADE_BLUE          = 0x84
    FADE_YELLOW        = 0x85
    FADE_CYAN          = 0x86
    FADE_PURPLE        = 0x87
    FADE_WHITE         = 0x88
    CROSS_RED_GREEN    = 0x89
    CROSS_RED_BLUE     = 0x8A
    CROSS_GREEN_BLUE   = 0x8B
    STROBE_7_COLOR     = 0x8C
    STROBE_RED         = 0x8D
    STROBE_GREEN       = 0x8E
    STROBE_BLUE        = 0x8F
    STROBE_YELLOW      = 0x90
    STROBE_CYAN        = 0x91
    STROBE_PURPLE      = 0x92
    STROBE_WHITE       = 0x93
    FADE_7_COLOR       = 0x94


class Pkt:
    """All BLEDOM 9-byte packet constructors."""

    @staticmethod
    def power_on() -> bytearray:
        return bytearray([0x7E, 0x04, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF])

    @staticmethod
    def power_off() -> bytearray:
        return bytearray([0x7E, 0x04, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])

    @staticmethod
    def color(r: int, g: int, b: int) -> bytearray:
        return bytearray([0x7E, 0x07, 0x05, 0x03, r & 0xFF, g & 0xFF, b & 0xFF, 0x10, 0xEF])

    @staticmethod
    def brightness(level: int) -> bytearray:
        lvl = max(0, min(100, level))
        return bytearray([0x7E, 0x04, 0x01, lvl, 0x01, 0xFF, 0xFF, 0x00, 0xEF])

    @staticmethod
    def speed(level: int) -> bytearray:
        lvl = max(0, min(100, level))
        return bytearray([0x7E, 0x04, 0x02, lvl, 0xFF, 0xFF, 0xFF, 0x00, 0xEF])

    @staticmethod
    def hw_mode(mode: HWMode, speed: int = 50) -> bytearray:
        spd = max(0, min(100, speed))
        return bytearray([0x7E, 0x05, 0x03, int(mode), spd, 0x00, 0x00, 0x00, 0xEF])

    @staticmethod
    def mic_sensitivity(sensitivity: int) -> bytearray:
        sens = max(0, min(255, sensitivity))
        return bytearray([0x7E, 0x04, 0x05, sens, 0x00, 0x00, 0x00, 0x00, 0xEF])

    @staticmethod
    def color_order(order_id: int) -> bytearray:
        oid = max(1, min(6, order_id))
        return bytearray([0x7E, 0x08, 0x05, 0x02, oid, 0x00, 0x00, 0x00, 0xEF])

    @staticmethod
    def parse_status(data: bytearray) -> Optional[dict]:
        """Parse FFF4 notification: 7E 08 [Power] [Mode] [Speed] [R] [G] [B] 00 EF"""
        if len(data) >= 9 and data[0] == 0x7E and data[-1] == 0xEF:
            return {
                "power": bool(data[2]),
                "mode": data[3],
                "speed": data[4],
                "r": data[5],
                "g": data[6],
                "b": data[7],
            }
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 ▸ BLE DRIVER
# ══════════════════════════════════════════════════════════════════════════════

class BLEDOMDevice:
    """Low-level BLE driver with auto-reconnect and rate limiting."""

    def __init__(self, mac: str):
        self.mac = mac
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._last_send = 0.0
        self._min_interval = 1.0 / MAX_PACKETS_PER_SEC
        self._status: dict = {}
        self._notify_cb: Optional[Callable] = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def on_status_update(self, callback: Callable):
        self._notify_cb = callback

    async def connect(self, timeout: float = 10.0, subscribe_notify: bool = True) -> bool:
        try:
            self._client = BleakClient(self.mac, timeout=timeout)
            await self._client.connect()
            if subscribe_notify:
                try:
                    await self._client.start_notify(NOTIFY_UUID, self._notification_handler)
                except Exception:
                    pass  # FFF4 optional on some firmware
            log.info(f"Connected to {self.mac}")
            return True
        except Exception as e:
            log.error(f"Connection failed: {e}")
            self._client = None
            return False

    async def disconnect(self):
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def send(self, packet: bytearray) -> bool:
        async with self._lock:
            now = time.monotonic()
            gap = self._min_interval - (now - self._last_send)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                await self._client.write_gatt_char(WRITE_UUID, packet, response=False)
                self._last_send = time.monotonic()
                return True
            except Exception as e:
                log.warning(f"Send failed: {e}")
                return False

    async def read_firmware(self) -> str:
        try:
            data = await self._client.read_gatt_char(WRITE_UUID)
            return data.decode("ascii", errors="replace").strip()
        except Exception:
            return "unknown"

    def _notification_handler(self, sender, data: bytearray):
        parsed = Pkt.parse_status(data)
        if parsed:
            self._status = parsed
            log.debug(f"Status: {parsed}")
            if self._notify_cb:
                self._notify_cb(parsed)

    @property
    def last_status(self) -> dict:
        return self._status.copy()


async def scan_for_device(timeout: float = 8.0) -> Optional[str]:
    """Scan BLE and return MAC of first BLEDOM-like device found."""
    _print("Scanning for BLEDOM devices...")
    found = []
    devices = await BleakScanner.discover(timeout=timeout)
    for d in devices:
        name = d.name or ""
        mac  = d.address
        match_name   = any(p in name.upper() for p in DEVICE_NAME_PATTERNS)
        match_prefix = mac.upper().startswith(KNOWN_MAC_PREFIX)
        if match_name or match_prefix:
            found.append((mac, name))
            _print(f"  [+] {name}  [{mac}]")
    if not found:
        _print("  No BLEDOM device found.")
        return None
    if len(found) == 1:
        _print(f"  Using: {found[0][1]}  [{found[0][0]}]")
        return found[0][0]
    # Multiple devices — pick first
    _print(f"  Multiple found, using first: {found[0][0]}")
    return found[0][0]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 ▸ CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "device": {
        "mac": "",
        "auto_discover": True,
        "scan_timeout": 8.0,
        "connection_timeout": 10.0,
        "reconnect_attempts": 3,
        "subscribe_notifications": True
    },
    "defaults": {
        "brightness": 80,
        "speed": 50,
        "color": [255, 100, 30],
        "mode": "pulse"
    },
    "modes": {
        "pulse": {
            "color": [255, 100, 30],
            "min_brightness": 5,
            "max_brightness": 100,
            "period_seconds": 3.0,
            "fps": 20
        },
        "rainbow": {
            "saturation": 1.0,
            "value": 1.0,
            "cycle_seconds": 10.0,
            "fps": 20
        },
        "wave": {
            "saturation": 1.0,
            "value": 1.0,
            "cycle_seconds": 5.0,
            "fps": 20
        },
        "fire": {
            "fps": 15,
            "intensity": 0.85
        },
        "meteor": {
            "color": [200, 150, 255],
            "tail_length": 6,
            "fps": 20
        },
        "comet": {
            "color": [100, 200, 255],
            "fps": 20
        },
        "sunrise": {
            "duration_minutes": 20,
            "fps": 2
        },
        "sunset": {
            "duration_minutes": 10,
            "fps": 2
        },
        "cct": {
            "temperature": 3500,
            "brightness": 80
        },
        "audio": {
            "source": "microphone",
            "sensitivity": 1.0,
            "mode": "spectrum",
            "low_color": [255, 0, 0],
            "mid_color": [0, 255, 0],
            "high_color": [0, 100, 255],
            "fps": 25
        },
        "music": {
            "source": "loopback",
            "sensitivity": 1.2,
            "beat_color": [255, 220, 0],
            "idle_color": [50, 0, 120],
            "fps": 30
        },
        "ambient": {
            "fps": 10,
            "region": "edges",
            "saturation_boost": 1.4,
            "value_boost": 1.0,
            "smoothing": 0.65
        },
        "system_monitor": {
            "metric": "cpu",
            "fps": 2,
            "low_color": [0, 200, 0],
            "high_color": [255, 0, 0]
        },
        "notification": {
            "flash_color": [255, 230, 0],
            "flash_count": 4,
            "flash_duration_ms": 200,
            "restore_after": True
        },
        "game": {
            "detect_mode": "process",
            "process_keywords": ["steam", "gameoverlayui", "epicgames", "riotclient",
                                  "leagueclient", "csgo", "valorant", "minecraft",
                                  "fortnite", "overwatch", "dota2", "battlefield",
                                  "cyberpunk2077", "witcher3", "gta5", "rdr2",
                                  "deadlock", "baldursgate3"],
            "mode": "rainbow",
            "rainbow_cycle_seconds": 3.0,
            "check_interval_seconds": 5.0
        },
        "video": {
            "player_processes": ["vlc", "mpv", "mpc-hc64", "mpc-hc", "wmplayer",
                                   "potplayermini64", "potplayermini", "movies"],
            "mode": "ambient",
            "check_interval_seconds": 5.0
        },
        "hardware": {
            "mode": "FADE_7_COLOR",
            "speed": 50
        },
        "mic_hardware": {
            "sensitivity": 200
        },
        "schedule": {
            "enabled": False,
            "entries": [
                {"time": "07:00", "action": "wake",  "args": {}},
                {"time": "09:00", "action": "scene", "args": {"scene": "focus"}},
                {"time": "18:00", "action": "scene", "args": {"scene": "relax"}},
                {"time": "23:00", "action": "sleep", "args": {"duration_minutes": 30}},
                {"time": "23:30", "action": "off",   "args": {}}
            ]
        },
        "alarm": {
            "enabled": False,
            "time": "07:00",
            "color": [255, 200, 50],
            "flash_count": 15,
            "flash_duration_ms": 300
        }
    },
    "scenes": {
        "movie":   {"brightness": 25, "color": [255, 130, 50], "mode": "static"},
        "party":   {"mode": "hw", "hw_mode": "STROBE_7_COLOR", "speed": 75},
        "romance": {"mode": "pulse", "color": [200, 20, 80], "brightness": 50},
        "relax":   {"mode": "hw", "hw_mode": "FADE_PURPLE", "speed": 25, "brightness": 55},
        "focus":   {"brightness": 100, "color": [210, 230, 255], "mode": "static"},
        "gaming":  {"mode": "rainbow", "cycle_seconds": 3.0},
        "chill":   {"mode": "pulse", "color": [30, 80, 200], "brightness": 60}
    }
}

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            _deep_merge(cfg, user)
        except Exception as e:
            log.warning(f"Config load error: {e}. Using defaults.")
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 ▸ COLOR UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def hsv_to_rgb(h: float, s: float, v: float) -> Tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def lerp_color(c1, c2, t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def cct_to_rgb(kelvin: int) -> Tuple[int, int, int]:
    """Convert color temperature (Kelvin) to RGB. Range 1000–40000 K."""
    temp = kelvin / 100.0
    if temp <= 66:
        r = 255
        g = int(99.4708025861 * math.log(temp) - 161.1195681661) if temp > 0 else 0
    else:
        r = int(329.698727446 * ((temp - 60) ** -0.1332047592))
        g = int(288.1221695283 * ((temp - 60) ** -0.0755148492))
    if temp >= 66:
        b = 255
    elif temp <= 19:
        b = 0
    else:
        b = int(138.5177312231 * math.log(temp - 10) - 305.0447927307)
    return max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))


def parse_color(val) -> Tuple[int, int, int]:
    """Parse color from various formats: [r,g,b], '#RRGGBB', 'r,g,b'."""
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        return int(val[0]), int(val[1]), int(val[2])
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("#"):
            hex_val = val.lstrip("#")
            return int(hex_val[0:2], 16), int(hex_val[2:4], 16), int(hex_val[4:6], 16)
        parts = re.split(r"[,\s]+", val)
        if len(parts) == 3:
            return int(parts[0]), int(parts[1]), int(parts[2])
    return 255, 255, 255


def _print(msg: str, style: str = ""):
    if HAS_RICH and console:
        console.print(msg if not style else f"[{style}]{msg}[/{style}]")
    else:
        print(msg)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 ▸ MODE BASE CLASS
# ══════════════════════════════════════════════════════════════════════════════

class BaseMode(ABC):
    """Abstract base for all controller modes."""

    def __init__(self, device: BLEDOMDevice, cfg: dict):
        self.device = device
        self.cfg = cfg
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @abstractmethod
    async def _run(self):
        ...

    async def _send(self, pkt: bytearray):
        await self.device.send(pkt)

    def _fps_sleep(self, fps: int) -> float:
        return 1.0 / max(1, fps)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 ▸ SOFTWARE ANIMATION MODES
# ══════════════════════════════════════════════════════════════════════════════

class StaticMode(BaseMode):
    """Static solid color."""

    async def _run(self):
        r, g, b = parse_color(self.cfg.get("color", [255, 255, 255]))
        await self._send(Pkt.color(r, g, b))
        brightness = self.cfg.get("brightness")
        if brightness is not None:
            await self._send(Pkt.brightness(brightness))


class PulseMode(BaseMode):
    """Smooth breathing / pulse effect."""

    async def _run(self):
        fps     = self.cfg.get("fps", 20)
        period  = self.cfg.get("period_seconds", 3.0)
        min_b   = self.cfg.get("min_brightness", 5)
        max_b   = self.cfg.get("max_brightness", 100)
        r, g, b = parse_color(self.cfg.get("color", [255, 100, 30]))

        await self._send(Pkt.color(r, g, b))
        dt = 1.0 / fps
        t  = 0.0
        while self._running:
            # Sine wave for smooth breathe
            phase = math.sin(math.pi * t / period) ** 2
            level = int(min_b + (max_b - min_b) * phase)
            await self._send(Pkt.brightness(level))
            await asyncio.sleep(dt)
            t = (t + dt) % (period * 2)


class RainbowMode(BaseMode):
    """Smooth full-spectrum rainbow cycle."""

    async def _run(self):
        fps    = self.cfg.get("fps", 20)
        cycle  = self.cfg.get("cycle_seconds", 10.0)
        sat    = self.cfg.get("saturation", 1.0)
        val    = self.cfg.get("value", 1.0)
        dt     = 1.0 / fps
        hue    = 0.0
        step   = dt / cycle
        while self._running:
            r, g, b = hsv_to_rgb(hue, sat, val)
            await self._send(Pkt.color(r, g, b))
            hue = (hue + step) % 1.0
            await asyncio.sleep(dt)


class WaveMode(BaseMode):
    """Hue wave — oscillates through the spectrum back and forth."""

    async def _run(self):
        fps    = self.cfg.get("fps", 20)
        cycle  = self.cfg.get("cycle_seconds", 5.0)
        sat    = self.cfg.get("saturation", 1.0)
        val    = self.cfg.get("value", 1.0)
        dt     = 1.0 / fps
        t      = 0.0
        while self._running:
            hue = 0.5 + 0.5 * math.sin(2 * math.pi * t / cycle)
            r, g, b = hsv_to_rgb(hue, sat, val)
            await self._send(Pkt.color(r, g, b))
            await asyncio.sleep(dt)
            t = (t + dt) % cycle


class FireMode(BaseMode):
    """Fire-like warm flickering effect."""

    async def _run(self):
        import random
        fps       = self.cfg.get("fps", 15)
        intensity = self.cfg.get("intensity", 0.85)
        dt = 1.0 / fps
        while self._running:
            # Flicker: hue stays in orange-red range, value varies
            hue = random.uniform(0.01, 0.08)
            sat = random.uniform(0.8, 1.0)
            val = random.uniform(intensity * 0.5, intensity)
            r, g, b = hsv_to_rgb(hue, sat, val)
            await self._send(Pkt.color(r, g, b))
            await asyncio.sleep(dt)


class MeteorMode(BaseMode):
    """Meteor shower: bursts of a chosen color with fade-off."""

    async def _run(self):
        import random
        fps    = self.cfg.get("fps", 20)
        color  = parse_color(self.cfg.get("color", [200, 150, 255]))
        dt     = 1.0 / fps
        phase  = 0.0
        while self._running:
            # Sawtooth: quick bright pulse, slow fade
            if phase < 0.15:
                t = phase / 0.15
                bright_val = t
            else:
                t = (phase - 0.15) / 0.85
                bright_val = 1.0 - t
            r = int(color[0] * bright_val)
            g = int(color[1] * bright_val)
            b = int(color[2] * bright_val)
            await self._send(Pkt.color(r, g, b))
            await asyncio.sleep(dt)
            # Random burst interval between 0.8–3 seconds
            phase = (phase + dt / random.uniform(0.8, 3.0)) % 1.0


class CometMode(BaseMode):
    """Comet: slow sweep across hues with sparkling variation."""

    async def _run(self):
        import random
        fps = self.cfg.get("fps", 20)
        dt  = 1.0 / fps
        hue = 0.0
        while self._running:
            sparkle = random.uniform(0.85, 1.0)
            r, g, b = hsv_to_rgb(hue, 0.9, sparkle)
            await self._send(Pkt.color(r, g, b))
            hue = (hue + dt * 0.05) % 1.0
            await asyncio.sleep(dt)


class SunriseMode(BaseMode):
    """Gradual warm sunrise: from deep red to bright warm white over time."""

    async def _run(self):
        fps      = self.cfg.get("fps", 2)
        duration = self.cfg.get("duration_minutes", 20) * 60.0
        dt       = 1.0 / fps
        steps    = int(duration * fps)
        # Start: dim red-orange (warm ember), end: bright warm daylight
        start_rgb = (80, 5, 0)
        end_cct   = cct_to_rgb(5500)
        for i in range(steps):
            if not self._running:
                break
            t   = i / steps
            cct = int(1800 + (5500 - 1800) * t)
            rgb = cct_to_rgb(cct)
            r, g, b = lerp_color(start_rgb, rgb, t)
            await self._send(Pkt.color(r, g, b))
            bright = int(5 + 95 * t)
            await self._send(Pkt.brightness(bright))
            await asyncio.sleep(dt)
        # Hold at full brightness
        while self._running:
            await asyncio.sleep(1.0)


class SunsetMode(BaseMode):
    """Gradual sunset: from warm white to orange-red to off."""

    async def _run(self):
        fps      = self.cfg.get("fps", 2)
        duration = self.cfg.get("duration_minutes", 10) * 60.0
        dt       = 1.0 / fps
        steps    = int(duration * fps)
        for i in range(steps):
            if not self._running:
                break
            t       = i / steps
            cct     = int(5500 - (5500 - 1800) * t)
            r, g, b = cct_to_rgb(cct)
            bright  = int(100 - 95 * t)
            await self._send(Pkt.color(r, g, b))
            await self._send(Pkt.brightness(bright))
            await asyncio.sleep(dt)
        await self._send(Pkt.power_off())


class CCTMode(BaseMode):
    """Static color temperature mode (warm/cool white)."""

    async def _run(self):
        kelvin     = self.cfg.get("temperature", 4000)
        brightness = self.cfg.get("brightness", 80)
        r, g, b    = cct_to_rgb(kelvin)
        await self._send(Pkt.color(r, g, b))
        await self._send(Pkt.brightness(brightness))


class SleepTimerMode(BaseMode):
    """Gradually dim and shut off over duration_minutes."""

    async def _run(self):
        duration = self.cfg.get("duration_minutes", 30) * 60.0
        fps      = self.cfg.get("fps", 1)
        dt       = 1.0 / fps
        steps    = int(duration * fps)
        for i in range(steps):
            if not self._running:
                break
            bright = int(100 * (1.0 - i / steps))
            await self._send(Pkt.brightness(max(0, bright)))
            await asyncio.sleep(dt)
        await self._send(Pkt.power_off())


class AlarmMode(BaseMode):
    """Flash a color N times as alarm."""

    async def _run(self):
        r, g, b     = parse_color(self.cfg.get("color", [255, 200, 50]))
        count       = self.cfg.get("flash_count", 15)
        duration_ms = self.cfg.get("flash_duration_ms", 300)
        for _ in range(count):
            if not self._running:
                break
            await self._send(Pkt.color(r, g, b))
            await self._send(Pkt.brightness(100))
            await asyncio.sleep(duration_ms / 1000)
            await self._send(Pkt.brightness(0))
            await asyncio.sleep(duration_ms / 1000)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 ▸ HARDWARE PASS-THROUGH MODE
# ══════════════════════════════════════════════════════════════════════════════

class HardwareMode(BaseMode):
    """Delegate animation to the device's built-in firmware modes."""

    async def _run(self):
        mode_name = self.cfg.get("mode", "FADE_7_COLOR").upper()
        speed     = int(self.cfg.get("speed", 50))
        brightness= self.cfg.get("brightness")
        try:
            hw_mode = HWMode[mode_name]
        except KeyError:
            _print(f"[red]Unknown hardware mode '{mode_name}'. Valid: {[m.name for m in HWMode]}")
            return
        if brightness is not None:
            await self._send(Pkt.brightness(int(brightness)))
        await self._send(Pkt.hw_mode(hw_mode, speed))


class MicHardwareMode(BaseMode):
    """Activate the LED strip's on-board microphone (if present)."""

    async def _run(self):
        sensitivity = int(self.cfg.get("sensitivity", 200))
        await self._send(Pkt.mic_sensitivity(sensitivity))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 ▸ AUDIO REACTIVE MODES
# ══════════════════════════════════════════════════════════════════════════════

class AudioMode(BaseMode):
    """PC microphone or loopback FFT → RGB color mapping."""

    def __init__(self, device, cfg):
        super().__init__(device, cfg)
        self._audio_buf = None
        self._buf_lock  = threading.Lock()

    async def _run(self):
        if not HAS_NUMPY or not HAS_SD:
            _print("[yellow]Audio mode requires numpy + sounddevice. Install them.")
            return

        source      = self.cfg.get("source", "microphone")
        fps         = self.cfg.get("fps", 25)
        sensitivity = float(self.cfg.get("sensitivity", 1.0))
        low_color   = parse_color(self.cfg.get("low_color",  [255, 0, 0]))
        mid_color   = parse_color(self.cfg.get("mid_color",  [0, 255, 0]))
        high_color  = parse_color(self.cfg.get("high_color", [0, 100, 255]))

        sample_rate = 44100
        chunk_size  = 2048
        device_id   = None

        if source == "loopback":
            device_id = self._find_loopback_device()

        loop = asyncio.get_event_loop()

        def audio_callback(indata, frames, time_info, status):
            with self._buf_lock:
                self._audio_buf = indata[:, 0].copy()

        stream_kwargs = dict(
            samplerate=sample_rate,
            channels=1,
            blocksize=chunk_size,
            callback=audio_callback,
        )
        if device_id is not None:
            stream_kwargs["device"] = device_id

        try:
            with sd.InputStream(**stream_kwargs):
                dt = 1.0 / fps
                while self._running:
                    with self._buf_lock:
                        buf = self._audio_buf
                    if buf is not None and len(buf) >= chunk_size:
                        r, g, b = self._analyze(buf, sample_rate, sensitivity,
                                                  low_color, mid_color, high_color)
                        await self._send(Pkt.color(r, g, b))
                    await asyncio.sleep(dt)
        except Exception as e:
            _print(f"[red]Audio error: {e}")

    def _find_loopback_device(self) -> Optional[int]:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            name = d["name"].lower()
            if "loopback" in name or "stereo mix" in name or "что слышит" in name:
                return i
        return None

    def _analyze(self, buf, sr, sensitivity, low_col, mid_col, high_col):
        window = np.hanning(len(buf))
        fft    = np.abs(np.fft.rfft(buf * window))
        freqs  = np.fft.rfftfreq(len(buf), 1.0 / sr)

        def band_energy(lo, hi):
            mask = (freqs >= lo) & (freqs < hi)
            return float(fft[mask].mean()) if mask.any() else 0.0

        bass    = min(1.0, band_energy(20,   250)  * sensitivity * 0.003)
        mid     = min(1.0, band_energy(250,  4000) * sensitivity * 0.002)
        treble  = min(1.0, band_energy(4000, 20000)* sensitivity * 0.002)

        # Blend colors based on which band dominates
        total = bass + mid + treble + 1e-9
        r = int((low_col[0] * bass + mid_col[0] * mid + high_col[0] * treble) / total)
        g = int((low_col[1] * bass + mid_col[1] * mid + high_col[1] * treble) / total)
        b = int((low_col[2] * bass + mid_col[2] * mid + high_col[2] * treble) / total)

        # Brightness follows overall volume
        volume = min(1.0, (bass + mid + treble) / 3.0)
        r = int(r * volume)
        g = int(g * volume)
        b = int(b * volume)
        return r, g, b


class MusicSyncMode(BaseMode):
    """Beat detection with rhythm-based color flashing."""

    def __init__(self, device, cfg):
        super().__init__(device, cfg)
        self._audio_buf = None
        self._buf_lock  = threading.Lock()
        self._beat_history: List[float] = []

    async def _run(self):
        if not HAS_NUMPY or not HAS_SD:
            _print("[yellow]Music sync requires numpy + sounddevice.")
            return

        source      = self.cfg.get("source", "loopback")
        fps         = self.cfg.get("fps", 30)
        sensitivity = float(self.cfg.get("sensitivity", 1.2))
        beat_color  = parse_color(self.cfg.get("beat_color",  [255, 220, 0]))
        idle_color  = parse_color(self.cfg.get("idle_color",  [50, 0, 120]))

        sample_rate = 44100
        chunk_size  = 1024
        device_id   = self._find_loopback_device() if source == "loopback" else None

        def audio_cb(indata, frames, time_info, status):
            with self._buf_lock:
                self._audio_buf = indata[:, 0].copy()

        try:
            stream_kwargs = dict(samplerate=sample_rate, channels=1,
                                  blocksize=chunk_size, callback=audio_cb)
            if device_id is not None:
                stream_kwargs["device"] = device_id
            with sd.InputStream(**stream_kwargs):
                dt = 1.0 / fps
                while self._running:
                    with self._buf_lock:
                        buf = self._audio_buf
                    if buf is not None:
                        is_beat, energy = self._detect_beat(buf, sample_rate, sensitivity)
                        if is_beat:
                            r, g, b = beat_color
                        else:
                            t       = min(1.0, energy * 2.0)
                            r, g, b = lerp_color(idle_color, beat_color, t * 0.4)
                        await self._send(Pkt.color(r, g, b))
                    await asyncio.sleep(dt)
        except Exception as e:
            _print(f"[red]Music sync error: {e}")

    def _find_loopback_device(self) -> Optional[int]:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if "loopback" in d["name"].lower() or "stereo mix" in d["name"].lower():
                return i
        return None

    def _detect_beat(self, buf, sr, sensitivity) -> Tuple[bool, float]:
        fft   = np.abs(np.fft.rfft(buf))
        freqs = np.fft.rfftfreq(len(buf), 1.0 / sr)
        bass_mask = (freqs >= 40) & (freqs <= 200)
        energy = float(fft[bass_mask].mean()) if bass_mask.any() else 0.0
        energy *= sensitivity

        self._beat_history.append(energy)
        if len(self._beat_history) > 43:
            self._beat_history.pop(0)

        if len(self._beat_history) < 10:
            return False, energy

        avg = sum(self._beat_history[:-1]) / len(self._beat_history[:-1])
        is_beat = energy > avg * 1.5 and energy > 0.01
        return is_beat, min(1.0, energy / (avg + 1e-9) / 2.0)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 ▸ SCREEN AMBIENT (AMBILIGHT)
# ══════════════════════════════════════════════════════════════════════════════

class AmbientMode(BaseMode):
    """Capture screen color and mirror it to the LED strip."""

    async def _run(self):
        if not HAS_MSS or not HAS_NUMPY:
            _print("[yellow]Ambient mode requires mss + numpy.")
            return

        fps        = self.cfg.get("fps", 10)
        region     = self.cfg.get("region", "edges")
        sat_boost  = float(self.cfg.get("saturation_boost", 1.4))
        val_boost  = float(self.cfg.get("value_boost", 1.0))
        smoothing  = float(self.cfg.get("smoothing", 0.65))
        dt         = 1.0 / fps

        prev_r, prev_g, prev_b = 0, 0, 0

        try:
            with mss_lib.mss() as sct:
                monitor = sct.monitors[1]
                while self._running:
                    img   = sct.grab(monitor)
                    frame = np.array(img)[:, :, :3]  # BGR

                    if region == "edges":
                        ew = max(50, frame.shape[0] // 8)
                        eh = max(50, frame.shape[1] // 8)
                        top    = frame[:ew, :, :]
                        bottom = frame[-ew:, :, :]
                        left   = frame[:, :eh, :]
                        right  = frame[:, -eh:, :]
                        pixels = np.vstack([
                            top.reshape(-1, 3), bottom.reshape(-1, 3),
                            left.reshape(-1, 3), right.reshape(-1, 3)
                        ])
                    elif region == "center":
                        h, w  = frame.shape[:2]
                        cy, cx = h // 2, w // 2
                        pixels = frame[cy-100:cy+100, cx-100:cx+100].reshape(-1, 3)
                    else:
                        pixels = frame.reshape(-1, 3)

                    avg  = pixels.mean(axis=0)
                    b_raw, g_raw, r_raw = avg[0], avg[1], avg[2]

                    # Saturation / value boost in HSV space
                    h, s, v = colorsys.rgb_to_hsv(r_raw/255, g_raw/255, b_raw/255)
                    s = min(1.0, s * sat_boost)
                    v = min(1.0, v * val_boost)
                    r_f, g_f, b_f = colorsys.hsv_to_rgb(h, s, v)
                    r_n = int(r_f * 255)
                    g_n = int(g_f * 255)
                    b_n = int(b_f * 255)

                    # Temporal smoothing
                    r = int(prev_r * smoothing + r_n * (1 - smoothing))
                    g = int(prev_g * smoothing + g_n * (1 - smoothing))
                    b = int(prev_b * smoothing + b_n * (1 - smoothing))

                    await self._send(Pkt.color(r, g, b))
                    prev_r, prev_g, prev_b = r, g, b
                    await asyncio.sleep(dt)
        except Exception as e:
            _print(f"[red]Ambient error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 ▸ SYSTEM MONITOR MODE
# ══════════════════════════════════════════════════════════════════════════════

class SystemMonitorMode(BaseMode):
    """CPU/RAM/GPU load → color heatmap (green = low, red = high)."""

    async def _run(self):
        if not HAS_PSUTIL:
            _print("[yellow]System monitor requires psutil.")
            return

        fps       = self.cfg.get("fps", 2)
        metric    = self.cfg.get("metric", "cpu")
        low_color = parse_color(self.cfg.get("low_color",  [0, 200, 0]))
        high_color= parse_color(self.cfg.get("high_color", [255, 0, 0]))
        dt        = 1.0 / fps

        while self._running:
            if metric == "cpu":
                load = psutil.cpu_percent(interval=None) / 100.0
            elif metric == "ram":
                load = psutil.virtual_memory().percent / 100.0
            elif metric == "disk":
                load = psutil.disk_usage("/").percent / 100.0
            else:
                load = psutil.cpu_percent(interval=None) / 100.0

            r, g, b = lerp_color(low_color, high_color, load)
            await self._send(Pkt.color(r, g, b))
            bright = int(50 + 50 * load)
            await self._send(Pkt.brightness(bright))
            await asyncio.sleep(dt)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 ▸ NOTIFICATION FLASH MODE
# ══════════════════════════════════════════════════════════════════════════════

class NotificationMode(BaseMode):
    """Flash LEDs when Windows toast notifications appear."""

    async def _run(self):
        flash_color  = parse_color(self.cfg.get("flash_color", [255, 230, 0]))
        flash_count  = int(self.cfg.get("flash_count", 4))
        duration_ms  = int(self.cfg.get("flash_duration_ms", 200))
        restore      = bool(self.cfg.get("restore_after", True))

        # Save current state
        saved_status = self.device.last_status.copy()

        # Trigger a flash immediately, then poll
        await self._flash(flash_color, flash_count, duration_ms)

        if restore and saved_status:
            r = saved_status.get("r", 255)
            g = saved_status.get("g", 255)
            b = saved_status.get("b", 255)
            await self._send(Pkt.color(r, g, b))

        # Keep running — future triggers call flash externally
        while self._running:
            await asyncio.sleep(1.0)

    async def _flash(self, color, count, duration_ms):
        r, g, b = color
        for _ in range(count):
            await self._send(Pkt.color(r, g, b))
            await self._send(Pkt.brightness(100))
            await asyncio.sleep(duration_ms / 1000)
            await self._send(Pkt.brightness(0))
            await asyncio.sleep(duration_ms / 1000)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 ▸ CONTEXT-AWARE MODES (GAME / VIDEO)
# ══════════════════════════════════════════════════════════════════════════════

class GameMode(BaseMode):
    """Detect gaming apps and apply intense rainbow lighting."""

    async def _run(self):
        if not HAS_PSUTIL:
            _print("[yellow]Game detection requires psutil.")
            await HardwareMode(self.device, {"mode": "FADE_7_COLOR", "speed": 70})._run()
            return

        keywords  = [k.lower() for k in self.cfg.get("process_keywords", [])]
        check_int = float(self.cfg.get("check_interval_seconds", 5.0))
        fps       = 20
        cycle_sec = float(self.cfg.get("rainbow_cycle_seconds", 3.0))

        sub_cfg = {"fps": fps, "cycle_seconds": cycle_sec, "saturation": 1.0, "value": 1.0}
        sub     = RainbowMode(self.device, sub_cfg)
        is_running_sub = False

        while self._running:
            is_game = self._detect_game(keywords)
            if is_game and not is_running_sub:
                _print("[green]Game detected — activating gaming mode.")
                await sub.start()
                is_running_sub = True
            elif not is_game and is_running_sub:
                await sub.stop()
                is_running_sub = False
                _print("[dim]No game detected — gaming mode stopped.")
            await asyncio.sleep(check_int)

        if is_running_sub:
            await sub.stop()

    def _detect_game(self, keywords) -> bool:
        if not HAS_PSUTIL:
            return False
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if any(k in name for k in keywords):
                return True
        return False


class VideoMode(BaseMode):
    """Detect video players and switch to Ambilight."""

    async def _run(self):
        players   = [p.lower() for p in self.cfg.get("player_processes", [])]
        check_int = float(self.cfg.get("check_interval_seconds", 5.0))
        sub_cfg   = {"fps": 8, "region": "edges", "saturation_boost": 1.3, "smoothing": 0.7}
        sub       = AmbientMode(self.device, sub_cfg)
        is_running_sub = False

        while self._running:
            is_video = self._detect_video(players)
            if is_video and not is_running_sub:
                if HAS_MSS and HAS_NUMPY:
                    _print("[green]Video player detected — activating Ambilight.")
                    await sub.start()
                    is_running_sub = True
                else:
                    _print("[yellow]Video detected but mss/numpy missing for Ambilight.")
            elif not is_video and is_running_sub:
                await sub.stop()
                is_running_sub = False
                _print("[dim]Video player closed — Ambilight stopped.")
            await asyncio.sleep(check_int)

        if is_running_sub:
            await sub.stop()

    def _detect_video(self, players) -> bool:
        if not HAS_PSUTIL:
            return False
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if any(p in name for p in players):
                return True
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13 ▸ SCHEDULE MODE
# ══════════════════════════════════════════════════════════════════════════════

class ScheduleMode(BaseMode):
    """Time-based action scheduler."""

    def __init__(self, device, cfg, controller):
        super().__init__(device, cfg)
        self.controller = controller

    async def _run(self):
        entries = self.cfg.get("entries", [])
        while self._running:
            now_str = datetime.now().strftime("%H:%M")
            for entry in entries:
                if entry.get("time") == now_str:
                    action = entry.get("action", "")
                    args   = entry.get("args", {})
                    await self._execute(action, args)
            await asyncio.sleep(60)

    async def _execute(self, action: str, args: dict):
        _print(f"[cyan]Schedule trigger: {action} {args}")
        if action == "on":
            await self.device.send(Pkt.power_on())
        elif action == "off":
            await self.device.send(Pkt.power_off())
        elif action == "wake":
            await self.controller.set_mode("sunrise", args)
        elif action == "sleep":
            await self.controller.set_mode("sleep_timer", args)
        elif action == "scene":
            scene = args.get("scene", "")
            await self.controller.apply_scene(scene)
        elif action == "mode":
            mode_name = args.get("mode", "")
            await self.controller.set_mode(mode_name, args)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 14 ▸ CONTROLLER ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

MODE_REGISTRY: Dict[str, type] = {
    "static":       StaticMode,
    "pulse":        PulseMode,
    "breathe":      PulseMode,
    "rainbow":      RainbowMode,
    "wave":         WaveMode,
    "fire":         FireMode,
    "meteor":       MeteorMode,
    "comet":        CometMode,
    "sunrise":      SunriseMode,
    "sunset":       SunsetMode,
    "cct":          CCTMode,
    "sleep_timer":  SleepTimerMode,
    "alarm":        AlarmMode,
    "audio":        AudioMode,
    "music":        MusicSyncMode,
    "ambient":      AmbientMode,
    "ambilight":    AmbientMode,
    "system":       SystemMonitorMode,
    "cpu":          SystemMonitorMode,
    "game":         GameMode,
    "video":        VideoMode,
    "hw":           HardwareMode,
    "hardware":     HardwareMode,
    "mic_hw":       MicHardwareMode,
    "notification": NotificationMode,
    # Hardware mode aliases
    "jump7":        None,  # resolved below
    "fade7":        None,
    "strobe7":      None,
}

# Add hardware mode aliases
for _m in HWMode:
    MODE_REGISTRY[_m.name.lower()] = None  # resolved to HardwareMode with hw_mode param


class LotusController:
    """High-level controller: manages connection, modes, config."""

    def __init__(self, cfg: dict):
        self.cfg        = cfg
        self.device_cfg = cfg["device"]
        self.device: Optional[BLEDOMDevice] = None
        self._active_mode: Optional[BaseMode] = None
        self._schedule_mode: Optional[ScheduleMode] = None

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        mac = self.device_cfg.get("mac", "").strip()
        if not mac and self.device_cfg.get("auto_discover", True):
            mac = await scan_for_device(float(self.device_cfg.get("scan_timeout", 8.0)))
        if not mac:
            _print("[red]No device MAC found. Set 'mac' in config.json or enable auto_discover.")
            return False

        self.device = BLEDOMDevice(mac)
        timeout     = float(self.device_cfg.get("connection_timeout", 10.0))
        subscribe   = bool(self.device_cfg.get("subscribe_notifications", True))
        attempts    = int(self.device_cfg.get("reconnect_attempts", 3))

        for attempt in range(1, attempts + 1):
            _print(f"Connecting to [{mac}] (attempt {attempt}/{attempts})...")
            ok = await self.device.connect(timeout, subscribe)
            if ok:
                fw = await self.device.read_firmware()
                _print(f"[green]Connected! Firmware: {fw}")
                return True
            await asyncio.sleep(1.0)

        _print("[red]Failed to connect.")
        return False

    async def disconnect(self):
        await self.stop_mode()
        if self.device:
            await self.device.disconnect()

    # ── Mode management ───────────────────────────────────────────────────────

    async def stop_mode(self):
        if self._active_mode:
            await self._active_mode.stop()
            self._active_mode = None

    async def set_mode(self, mode_name: str, extra_cfg: dict = None):
        await self.stop_mode()

        mode_name = mode_name.lower().strip()
        mode_cfg  = copy.deepcopy(self.cfg.get("modes", {}).get(mode_name, {}))
        if extra_cfg:
            mode_cfg.update(extra_cfg)

        # Resolve hardware aliases
        hw_name = None
        try:
            hw_mode_enum = HWMode[mode_name.upper()]
            hw_name = hw_mode_enum.name
        except KeyError:
            pass

        if hw_name:
            mode_cfg["mode"] = hw_name
            mode_cfg.setdefault("speed", self.cfg["defaults"].get("speed", 50))
            instance = HardwareMode(self.device, mode_cfg)
        elif mode_name in MODE_REGISTRY and MODE_REGISTRY[mode_name] is not None:
            klass = MODE_REGISTRY[mode_name]
            if klass == ScheduleMode:
                instance = ScheduleMode(self.device, mode_cfg, self)
            else:
                instance = klass(self.device, mode_cfg)
        else:
            _print(f"[red]Unknown mode: '{mode_name}'")
            _print(f"Available: {list(MODE_REGISTRY.keys())}")
            return

        self._active_mode = instance
        await instance.start()
        _print(f"[green]Mode started: {mode_name}")

    # ── Quick commands ────────────────────────────────────────────────────────

    async def power_on(self):
        await self.device.send(Pkt.power_on())

    async def power_off(self):
        await self.device.send(Pkt.power_off())

    async def set_color(self, r, g, b):
        await self.stop_mode()
        await self.device.send(Pkt.color(r, g, b))

    async def set_brightness(self, level: int):
        await self.device.send(Pkt.brightness(level))

    async def set_speed(self, level: int):
        await self.device.send(Pkt.speed(level))

    async def apply_scene(self, scene_name: str):
        scenes = self.cfg.get("scenes", {})
        scene  = scenes.get(scene_name)
        if not scene:
            _print(f"[red]Unknown scene: '{scene_name}'. Available: {list(scenes.keys())}")
            return

        _print(f"[cyan]Applying scene: {scene_name}")
        scene = copy.deepcopy(scene)

        if "brightness" in scene:
            await self.set_brightness(int(scene.pop("brightness")))

        mode = scene.pop("mode", None)
        if mode:
            await self.set_mode(mode, scene)
        elif "color" in scene:
            r, g, b = parse_color(scene["color"])
            await self.set_color(r, g, b)

    async def trigger_notification(self):
        """Flash LEDs for notification without changing persistent mode."""
        notif_cfg = self.cfg["modes"].get("notification", {})
        mode      = NotificationMode(self.device, notif_cfg)
        await mode._flash(
            parse_color(notif_cfg.get("flash_color", [255, 230, 0])),
            int(notif_cfg.get("flash_count", 4)),
            int(notif_cfg.get("flash_duration_ms", 200)),
        )

    async def get_status(self) -> dict:
        status = self.device.last_status
        if not status:
            _print("No status notification received yet (send a command first).")
        return status

    async def start_schedule(self):
        sched_cfg = self.cfg["modes"].get("schedule", {})
        if not sched_cfg.get("enabled", False):
            return
        self._schedule_mode = ScheduleMode(self.device, sched_cfg, self)
        await self._schedule_mode.start()

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def shutdown(self):
        if self._schedule_mode:
            await self._schedule_mode.stop()
        await self.stop_mode()
        await self.disconnect()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 15 ▸ INTERACTIVE TUI
# ══════════════════════════════════════════════════════════════════════════════

def _banner():
    lines = [
        "  Lotus LED Controller v2.0",
        "  BLEDOM / ELK-BLEDOM / Lotus Lantern",
    ]
    if HAS_RICH:
        console.print(Panel("\n".join(lines), style="bold cyan", expand=False))
    else:
        print("=" * 45)
        for l in lines:
            print(l)
        print("=" * 45)


def _mode_table():
    sw_modes = [
        ("static",      "Solid static color"),
        ("pulse",       "Breathing / pulse"),
        ("rainbow",     "Full spectrum cycle"),
        ("wave",        "Hue wave oscillation"),
        ("fire",        "Flickering fire"),
        ("meteor",      "Meteor burst"),
        ("comet",       "Sparkling comet"),
        ("sunrise",     "Gradual warm sunrise"),
        ("sunset",      "Warm fade to off"),
        ("cct",         "Color temperature (K)"),
        ("sleep_timer", "Dim then power off"),
        ("alarm",       "Flash alarm"),
    ]
    reactive = [
        ("audio",       "PC mic FFT -> colors"),
        ("music",       "Beat detection sync"),
        ("ambient",     "Ambilight screen capture"),
        ("system",      "CPU/RAM load heatmap"),
        ("game",        "Auto-detect games"),
        ("video",       "Auto-detect video players"),
        ("notification","Windows notification flash"),
    ]
    hw_modes = [
        ("hw",          "Device firmware mode"),
        ("jump7",       "7-color jump"),
        ("fade7",       "7-color smooth fade"),
        ("strobe7",     "7-color strobe"),
        ("fade_red/green/blue/...", "Single-color fade"),
        ("strobe_red/green/...",    "Single-color strobe"),
        ("cross_red_green/...",     "Cross-fade between 2"),
        ("mic_hw",                  "On-board mic reactive"),
    ]
    if HAS_RICH:
        t = Table(title="Available Modes", show_header=True, header_style="bold magenta")
        t.add_column("Mode", style="cyan", no_wrap=True)
        t.add_column("Description")
        for row in sw_modes + reactive + hw_modes:
            t.add_row(*row)
        console.print(t)
    else:
        print("\nAvailable Modes:")
        for name, desc in sw_modes + reactive + hw_modes:
            print(f"  {name:<28} {desc}")


async def interactive_tui(ctrl: LotusController):
    """Simple interactive menu loop."""
    _banner()

    MENU = {
        "1": ("Power ON",        lambda: ctrl.power_on()),
        "2": ("Power OFF",       lambda: ctrl.power_off()),
        "3": ("Set Color",       None),
        "4": ("Set Brightness",  None),
        "5": ("Set Speed",       None),
        "6": ("Set Mode",        None),
        "7": ("Apply Scene",     None),
        "8": ("Device Status",   lambda: show_status(ctrl)),
        "9": ("Show Modes",      lambda: asyncio.coroutine(_mode_table)()),
        "0": ("Exit",            None),
    }

    async def show_status(c):
        s = await c.get_status()
        if s:
            _print(f"Power: {'ON' if s['power'] else 'OFF'} | "
                   f"Mode: {s['mode']:#x} | Speed: {s['speed']} | "
                   f"RGB: ({s['r']},{s['g']},{s['b']})")

    while True:
        _print("\n[bold]─ Main Menu ─")
        for k, (label, _) in MENU.items():
            _print(f"  [{k}] {label}")

        choice = (input("Choice: ") if not HAS_RICH
                  else Prompt.ask("Choice", choices=list(MENU.keys()), default="0"))

        if choice == "0":
            break
        elif choice == "1":
            await ctrl.power_on(); _print("[green]ON")
        elif choice == "2":
            await ctrl.power_off(); _print("[yellow]OFF")
        elif choice == "3":
            r = int(input("R (0-255): ") if not HAS_RICH else IntPrompt.ask("R", default=255))
            g = int(input("G (0-255): ") if not HAS_RICH else IntPrompt.ask("G", default=255))
            b = int(input("B (0-255): ") if not HAS_RICH else IntPrompt.ask("B", default=255))
            await ctrl.set_color(r, g, b)
        elif choice == "4":
            lvl = int(input("Brightness 0-100: ") if not HAS_RICH
                      else IntPrompt.ask("Brightness (0-100)", default=80))
            await ctrl.set_brightness(lvl)
        elif choice == "5":
            lvl = int(input("Speed 0-100: ") if not HAS_RICH
                      else IntPrompt.ask("Speed (0-100)", default=50))
            await ctrl.set_speed(lvl)
        elif choice == "6":
            _mode_table()
            mode = (input("Mode name: ") if not HAS_RICH
                    else Prompt.ask("Mode name"))
            await ctrl.set_mode(mode)
        elif choice == "7":
            scenes = list(ctrl.cfg.get("scenes", {}).keys())
            _print(f"Scenes: {scenes}")
            scene = (input("Scene: ") if not HAS_RICH else Prompt.ask("Scene name"))
            await ctrl.apply_scene(scene)
        elif choice == "8":
            await show_status(ctrl)
        elif choice == "9":
            _mode_table()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 16 ▸ CLI + MAIN
# ══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lotus_controller",
        description="Lotus / BLEDOM LED Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s                          # interactive TUI
  %(prog)s scan                     # find devices
  %(prog)s on                       # power on
  %(prog)s off                      # power off
  %(prog)s color 255 0 128          # set RGB color
  %(prog)s color '#FF00AA'          # set hex color
  %(prog)s brightness 70            # 0-100
  %(prog)s speed 60                 # 0-100
  %(prog)s mode rainbow             # start rainbow
  %(prog)s mode audio               # audio reactive
  %(prog)s mode music               # music beat sync
  %(prog)s mode ambient             # Ambilight
  %(prog)s mode game                # gaming auto mode
  %(prog)s mode video               # video auto ambient
  %(prog)s mode sunrise             # gradual sunrise
  %(prog)s mode sleep_timer         # dim to off
  %(prog)s mode fire                # fire effect
  %(prog)s mode cct --temp 4000     # color temperature
  %(prog)s scene movie              # apply preset scene
  %(prog)s status                   # device status
  %(prog)s modes                    # list all modes
  %(prog)s config                   # open config.json
""")
    p.add_argument("--mac",     help="Override device MAC address")
    p.add_argument("--verbose", action="store_true", help="Debug logging")

    sub = p.add_subparsers(dest="command")

    sub.add_parser("scan",   help="Scan for BLEDOM devices")
    sub.add_parser("on",     help="Power on")
    sub.add_parser("off",    help="Power off")
    sub.add_parser("status", help="Show device status")
    sub.add_parser("modes",  help="List available modes")
    sub.add_parser("tui",    help="Interactive terminal UI")
    sub.add_parser("config", help="Open config.json in editor")

    col = sub.add_parser("color", help="Set static color")
    col.add_argument("value", nargs="+", help="R G B or #RRGGBB")

    brt = sub.add_parser("brightness", help="Set brightness 0-100")
    brt.add_argument("level", type=int)

    spd = sub.add_parser("speed", help="Set speed 0-100")
    spd.add_argument("level", type=int)

    md = sub.add_parser("mode", help="Start a mode")
    md.add_argument("name", help="Mode name")
    md.add_argument("--speed",  type=int,   default=None)
    md.add_argument("--fps",    type=int,   default=None)
    md.add_argument("--period", type=float, default=None, help="Period seconds (pulse/wave)")
    md.add_argument("--temp",   type=int,   default=None, help="Color temperature K (cct)")
    md.add_argument("--duration", type=int, default=None, help="Duration minutes")
    md.add_argument("--sensitivity", type=float, default=None, help="Audio sensitivity")
    md.add_argument("--color",  nargs="+",  default=None, help="R G B")
    md.add_argument("--run",    type=float, default=None, help="Run for N seconds then stop")

    sc_p = sub.add_parser("scene", help="Apply a scene preset")
    sc_p.add_argument("name", help="Scene name (movie, party, relax, focus, gaming, romance)")

    notif = sub.add_parser("notify", help="Trigger notification flash")

    return p


async def cli_main(args: argparse.Namespace, cfg: dict):
    """Execute a single CLI command and exit."""

    if args.command == "scan":
        mac = await scan_for_device()
        if mac:
            _print(f"\nFound: [{mac}]")
            _print(f"Set this in config.json: \"mac\": \"{mac}\"")
        return

    if args.command == "modes":
        _mode_table()
        return

    if args.command == "config":
        editor = os.environ.get("EDITOR", "notepad" if os.name == "nt" else "nano")
        os.system(f'{editor} "{CONFIG_PATH}"')
        return

    ctrl = LotusController(cfg)
    if not await ctrl.connect():
        return

    try:
        await ctrl.power_on()

        if args.command == "on":
            pass  # already powered on

        elif args.command == "off":
            await ctrl.power_off()

        elif args.command == "status":
            await asyncio.sleep(0.5)
            status = await ctrl.get_status()
            if status:
                _print(f"Power:      {'ON' if status['power'] else 'OFF'}")
                _print(f"Mode ID:    {status['mode']:#04x}")
                _print(f"Speed:      {status['speed']}")
                _print(f"Color RGB:  ({status['r']}, {status['g']}, {status['b']})")

        elif args.command == "color":
            raw = " ".join(args.value) if isinstance(args.value, list) else args.value
            r, g, b = parse_color(raw if len(args.value) == 1 else [int(x) for x in args.value])
            await ctrl.set_color(r, g, b)

        elif args.command == "brightness":
            await ctrl.set_brightness(args.level)

        elif args.command == "speed":
            await ctrl.set_speed(args.level)

        elif args.command == "mode":
            extra = {}
            if args.speed is not None:     extra["speed"]     = args.speed
            if args.fps is not None:       extra["fps"]       = args.fps
            if args.period is not None:    extra["period_seconds"] = args.period
            if args.temp is not None:      extra["temperature"]    = args.temp
            if args.duration is not None:  extra["duration_minutes"] = args.duration
            if args.sensitivity is not None: extra["sensitivity"]  = args.sensitivity
            if args.color is not None:     extra["color"] = [int(x) for x in args.color]

            await ctrl.set_mode(args.name, extra)

            run_time = args.run
            if run_time:
                _print(f"Running for {run_time}s...")
                await asyncio.sleep(run_time)
            else:
                _print("Press Ctrl+C to stop.")
                try:
                    while True:
                        await asyncio.sleep(1.0)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    pass

        elif args.command == "scene":
            await ctrl.apply_scene(args.name)
            await asyncio.sleep(2.0)

        elif args.command == "notify":
            await ctrl.trigger_notification()

        elif args.command in (None, "tui"):
            await interactive_tui(ctrl)

    except (KeyboardInterrupt, asyncio.CancelledError):
        _print("\n[yellow]Interrupted.")
    finally:
        await ctrl.shutdown()


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config()

    if args.mac:
        cfg["device"]["mac"] = args.mac

    # First-run: save default config
    if not CONFIG_PATH.exists():
        save_config(cfg)
        _print(f"[cyan]Created default config: {CONFIG_PATH}")

    try:
        asyncio.run(cli_main(args, cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
