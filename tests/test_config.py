"""Config tests, including the guard against drifting away from the Rust side."""

import json

import pytest

from lotus_led.config import (
    DEFAULT_CONFIG,
    deep_merge,
    load_config,
    save_default,
    schedule_minutes,
)
from lotus_led.modes import MODE_REGISTRY
from lotus_led.protocol import hw_mode_from_name


def test_config_matches_rust_shape():
    """The two implementations share one config.json.

    If a section is added on one side only, swapping between them silently
    loses settings — so pin the top-level shape here.
    """
    assert set(DEFAULT_CONFIG) == {"device", "defaults", "modes", "scenes", "schedule"}
    assert set(DEFAULT_CONFIG["device"]) == {
        "mac", "scan_timeout_secs", "reconnect_attempts", "group", "pin_order",
    }
    assert set(DEFAULT_CONFIG["defaults"]) == {
        "brightness", "speed", "color", "mode", "power_off_on_exit",
    }
    assert set(DEFAULT_CONFIG["modes"]) == {
        "pulse", "rainbow", "wave", "fire", "meteor", "comet",
        "sunrise", "sunset", "sleep_timer", "cct", "alarm", "notification",
        "audio", "music", "ambient", "system", "hardware", "mic_hardware",
        "sequence", "appwatch", "game", "video",
    }


def test_every_mode_section_has_a_runner():
    """A config section nothing reads is dead weight; a mode with no section
    cannot be configured. Both are bugs."""
    sections = {section for section, _ in MODE_REGISTRY.values()}
    configured = set(DEFAULT_CONFIG["modes"]) | {"static", "schedule"}
    assert sections <= configured, f"modes with no config: {sections - configured}"
    unused = configured - sections - {"static", "schedule"}
    assert not unused, f"config sections nothing reads: {unused}"


def test_default_scenes_name_real_modes():
    for name, scene in DEFAULT_CONFIG["scenes"].items():
        mode = scene.get("mode", "static")
        assert mode in MODE_REGISTRY or hw_mode_from_name(mode), f"scene '{name}' -> '{mode}'"


def test_ramp_modes_have_their_own_sensible_durations():
    """They share a shape but not a purpose: a sunset as long as a sunrise is
    a chore, and a sleep timer needs to outlast both."""
    modes = DEFAULT_CONFIG["modes"]
    assert modes["sunrise"]["duration_secs"] == 1200
    assert modes["sunset"]["duration_secs"] == 600
    assert modes["sleep_timer"]["duration_secs"] == 1800


def test_partial_config_keeps_all_other_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"device": {"mac": "AA:BB:CC:DD:EE:FF"}}))
    cfg = load_config(path)
    assert cfg["device"]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert cfg["device"]["scan_timeout_secs"] == 6.0
    assert cfg["defaults"]["brightness"] == 80
    assert cfg["modes"]["pulse"]["period_secs"] == 3.0
    assert cfg["scenes"]


def test_malformed_config_is_reported_not_swallowed(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ this is not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_config(path)


def test_missing_config_yields_defaults(tmp_path):
    cfg = load_config(tmp_path / "absent.json")
    assert cfg["defaults"]["mode"] == "pulse"


def test_saved_default_reloads_identically(tmp_path):
    path = tmp_path / "config.json"
    save_default(path)
    assert load_config(path) == DEFAULT_CONFIG


def test_deep_merge_replaces_lists_wholesale():
    """A user who lists three appwatch rules means those three, not those
    three plus the defaults."""
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2, 3]}
    deep_merge(base, {"a": {"c": 9}, "list": [7]})
    assert base == {"a": {"b": 1, "c": 9}, "list": [7]}


@pytest.mark.parametrize(
    "text,expected",
    [("07:00", 420), ("23:59", 1439), ("00:00", 0), ("24:00", None), ("12:60", None), ("noon", None), ("", None)],
)
def test_schedule_times_parse_and_reject_nonsense(text, expected):
    assert schedule_minutes(text) == expected
