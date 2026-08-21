"""Configuration (``config.json``).

Every mode's tunables live here, so behaviour changes without touching code or
passing a wall of CLI flags. Anything omitted from the file falls back to
:data:`DEFAULT_CONFIG`, so a partial config is always valid.

The shape is byte-for-byte the same as the Rust implementation's
``config.json``: one file drives both, and swapping between them keeps your
settings. ``DEFAULT_CONFIG`` below is generated from the Rust
``Config::default()`` output, and :func:`test_config_matches_rust_shape` in the
test suite guards the two against drifting apart.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

#: Canonical defaults, mirroring ``lotus-led/src/config.rs``.
DEFAULT_CONFIG: Dict[str, Any] = {   'device': {   'mac': '',
                  'scan_timeout_secs': 6.0,
                  'reconnect_attempts': 3,
                  'group': [],
                  'pin_order': None},
    'defaults': {   'brightness': 80,
                    'speed': 50,
                    'color': [255, 100, 30],
                    'mode': 'pulse',
                    'power_off_on_exit': True},
    'modes': {   'pulse': {   'color': [255, 100, 30],
                              'period_secs': 3.0,
                              'min_brightness': 5,
                              'max_brightness': 100,
                              'fps': 20},
                 'rainbow': {'cycle_secs': 10.0, 'saturation': 1.0, 'value': 1.0, 'fps': 20},
                 'wave': {   'cycle_secs': 5.0,
                             'saturation': 1.0,
                             'value': 1.0,
                             'fps': 20,
                             'hue_span': 0.15,
                             'hue_center': 0.6},
                 'fire': {   'fps': 15,
                             'intensity': 0.85,
                             'cool_color': [120, 20, 0],
                             'hot_color': [255, 170, 40]},
                 'meteor': {'color': [200, 150, 255], 'fps': 20, 'period_secs': 2.0},
                 'comet': {'color': [100, 200, 255], 'fps': 20, 'period_secs': 3.0},
                 'sunrise': {'duration_secs': 1200, 'fps': 2},
                 'sunset': {'duration_secs': 600, 'fps': 2},
                 'sleep_timer': {'duration_secs': 1800, 'fps': 1},
                 'cct': {'kelvin': 4000, 'brightness': 80},
                 'alarm': {   'color': [255, 200, 50],
                              'flash_count': 6,
                              'flash_ms': 250,
                              'restore': True},
                 'notification': {   'color': [255, 230, 0],
                                     'flash_count': 3,
                                     'flash_ms': 160,
                                     'restore': True},
                 'audio': {   'source': 'loopback',
                              'sensitivity': 1.0,
                              'fps': 20,
                              'low_color': [255, 0, 0],
                              'mid_color': [0, 255, 0],
                              'high_color': [0, 100, 255],
                              'bands_hz': [20.0, 250.0, 4000.0, 16000.0],
                              'smoothing': 0.45,
                              'noise_floor': 0.02},
                 'music': {   'source': 'loopback',
                              'sensitivity': 1.2,
                              'fps': 20,
                              'beat_color': [255, 220, 0],
                              'idle_color': [50, 0, 120],
                              'beat_threshold': 1.35,
                              'min_beat_gap_ms': 120,
                              'decay': 0.82,
                              'beat_band_hz': [40.0, 200.0],
                              'rainbow_beats': False},
                 'ambient': {   'fps': 15,
                                'region': 'edges',
                                'saturation_boost': 1.4,
                                'value_boost': 1.0,
                                'smoothing': 0.6,
                                'monitor': None,
                                'sample_step': 4,
                                'black_threshold': 8},
                 'system': {   'metric': 'cpu',
                               'fps': 2,
                               'low_color': [0, 200, 0],
                               'high_color': [255, 0, 0]},
                 'hardware': {'mode': 'cross_fade_7_color', 'speed': 50},
                 'mic_hardware': {'sensitivity': 70, 'eq': 'classic'},
                 'sequence': {   'loop_forever': True,
                                 'steps': [   {   'duration_secs': 1.0,
                                                  'color': [255, 0, 0],
                                                  'brightness': None,
                                                  'hw_mode': None,
                                                  'speed': None,
                                                  'raw': None,
                                                  'off': False},
                                              {   'duration_secs': 1.0,
                                                  'color': [0, 255, 0],
                                                  'brightness': None,
                                                  'hw_mode': None,
                                                  'speed': None,
                                                  'raw': None,
                                                  'off': False},
                                              {   'duration_secs': 1.0,
                                                  'color': [0, 0, 255],
                                                  'brightness': None,
                                                  'hw_mode': None,
                                                  'speed': None,
                                                  'raw': None,
                                                  'off': False}]},
                 'appwatch': {   'check_ms': 1000,
                                 'default_color': [80, 80, 80],
                                 'rules': [   {   'process': 'telegram',
                                                  'color': [0, 136, 212],
                                                  'brightness': None},
                                              {   'process': 'discord',
                                                  'color': [114, 137, 218],
                                                  'brightness': None},
                                              {   'process': 'spotify',
                                                  'color': [29, 185, 84],
                                                  'brightness': None},
                                              {   'process': 'chrome',
                                                  'color': [66, 133, 244],
                                                  'brightness': None},
                                              {   'process': 'firefox',
                                                  'color': [255, 100, 0],
                                                  'brightness': None},
                                              {   'process': 'code',
                                                  'color': [0, 188, 242],
                                                  'brightness': None},
                                              {   'process': 'steam',
                                                  'color': [60, 80, 110],
                                                  'brightness': None},
                                              {   'process': 'vlc',
                                                  'color': [255, 165, 0],
                                                  'brightness': None}]},
                 'game': {   'keywords': [   'csgo',
                                             'cs2',
                                             'valorant',
                                             'minecraft',
                                             'fortnite',
                                             'overwatch',
                                             'dota2',
                                             'cyberpunk2077',
                                             'witcher3',
                                             'gta5',
                                             'rdr2',
                                             'deadlock',
                                             'baldursgate3',
                                             'eldenring',
                                             'helldivers'],
                             'check_secs': 5.0,
                             'mode': 'rainbow',
                             'idle_mode': ''},
                 'video': {   'players': [   'vlc',
                                             'mpv',
                                             'mpc-hc64',
                                             'mpc-hc',
                                             'wmplayer',
                                             'potplayermini64',
                                             'potplayermini',
                                             'kodi',
                                             'plex',
                                             'netflix'],
                              'check_secs': 5.0,
                              'mode': 'ambient',
                              'idle_mode': ''}},
    'scenes': {   'gaming': {   'brightness': 100,
                                'color': None,
                                'mode': 'rainbow',
                                'speed': None,
                                'period_secs': 3.0},
                  'chill': {   'brightness': 60,
                               'color': [30, 80, 200],
                               'mode': 'pulse',
                               'speed': None,
                               'period_secs': 6.0},
                  'movie': {   'brightness': 25,
                               'color': [255, 130, 50],
                               'mode': 'static',
                               'speed': None,
                               'period_secs': None},
                  'focus': {   'brightness': 100,
                               'color': [210, 230, 255],
                               'mode': 'static',
                               'speed': None,
                               'period_secs': None},
                  'reading': {   'brightness': 90,
                                 'color': None,
                                 'mode': 'cct',
                                 'speed': None,
                                 'period_secs': None},
                  'party': {   'brightness': 100,
                               'color': None,
                               'mode': 'strobe_7_color',
                               'speed': 85,
                               'period_secs': None},
                  'relax': {   'brightness': 55,
                               'color': None,
                               'mode': 'fade_purple',
                               'speed': 20,
                               'period_secs': None},
                  'romance': {   'brightness': 45,
                                 'color': [200, 20, 80],
                                 'mode': 'pulse',
                                 'speed': None,
                                 'period_secs': 5.0}},
    'schedule': {'enabled': False, 'entries': []}}


def config_path() -> Path:
    """``config.json`` beside this package, or wherever ``LOTUS_CONFIG`` points."""
    override = os.environ.get("LOTUS_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "config.json"


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay ``override`` onto ``base`` in place, recursing into dicts.

    Lists are replaced wholesale rather than merged: a user who lists three
    appwatch rules means those three, not those three plus the defaults.
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path | None = None) -> Dict[str, Any]:
    """Load the config, falling back to defaults for anything absent.

    A malformed file raises rather than being silently ignored — quietly
    dropping a typo'd config is how people lose an afternoon.
    """
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not path.exists():
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(user, dict):
        raise ValueError(f"{path} should contain a JSON object")
    return deep_merge(cfg, user)


def save_config(cfg: Dict[str, Any], path: Path | None = None) -> Path:
    path = path or config_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    return path


def save_default(path: Path | None = None) -> Path:
    return save_config(copy.deepcopy(DEFAULT_CONFIG), path)


def schedule_minutes(time_text: str) -> int | None:
    """Parse ``"HH:MM"`` into minutes past midnight, or ``None`` if malformed."""
    try:
        hours, minutes = time_text.strip().split(":")
        h, m = int(hours), int(minutes)
    except (ValueError, AttributeError):
        return None
    if 0 <= h < 24 and 0 <= m < 60:
        return h * 60 + m
    return None
