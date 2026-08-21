"""Mode-resolution tests, plus the fake device used to check what gets sent."""

import asyncio

import pytest

from lotus_led.config import DEFAULT_CONFIG
from lotus_led.modes import (
    MODE_DESCRIPTIONS,
    MODE_NAMES,
    MODE_REGISTRY,
    Stop,
    _average_region,
    _grade,
    frame_delay,
    is_self_running,
    resolve_mode,
    run_mode,
    sleep_cancellable,
)
from lotus_led.protocol import MAX_PACKETS_PER_SEC, HWMode, hw_mode_from_name


class FakeDevice:
    """Records what a mode sends instead of talking to a strip."""

    def __init__(self):
        self.calls = []
        from lotus_led.device import ShadowState

        self.state = ShadowState()

    async def _record(self, name, *args):
        self.calls.append((name, args))

    async def power_on(self):
        await self._record("power_on")

    async def power_off(self):
        await self._record("power_off")

    async def set_color(self, r, g, b):
        await self._record("set_color", r, g, b)

    async def stream_color(self, r, g, b):
        await self._record("stream_color", r, g, b)

    async def set_brightness(self, level, channel=None):
        await self._record("set_brightness", level)

    async def set_speed(self, level):
        await self._record("set_speed", level)

    async def set_hw_mode(self, mode, speed=50):
        await self._record("set_hw_mode", mode, speed)

    async def set_mic(self, on):
        await self._record("set_mic", on)

    async def set_mic_sensitivity(self, s):
        await self._record("set_mic_sensitivity", s)

    async def set_mic_eq(self, eq):
        await self._record("set_mic_eq", eq)

    async def set_cct(self, warm, cold):
        await self._record("set_cct", warm, cold)

    async def send(self, packet):
        await self._record("send", bytes(packet))


@pytest.mark.parametrize("name", MODE_NAMES)
def test_every_advertised_name_resolves(name):
    """A name in the list that resolve_mode rejects is a runtime 'unknown
    mode' the user hits blind."""
    assert resolve_mode(name, DEFAULT_CONFIG)


@pytest.mark.parametrize("mode", list(HWMode))
def test_every_firmware_animation_resolves(mode):
    runner, settings, _ = resolve_mode(mode.mode_name, DEFAULT_CONFIG)
    assert settings["mode"] == mode.mode_name


def test_unknown_modes_are_rejected_with_suggestions():
    with pytest.raises(ValueError, match="unknown mode"):
        resolve_mode("definitely-not-a-mode", DEFAULT_CONFIG)


def test_every_advertised_name_has_a_description():
    for name in MODE_NAMES:
        assert MODE_DESCRIPTIONS.get(name), f"'{name}' has no description"


def test_config_values_actually_reach_the_mode():
    cfg = {**DEFAULT_CONFIG}
    cfg["modes"] = {**cfg["modes"], "pulse": {**cfg["modes"]["pulse"], "period_secs": 42.0}}
    _, settings, _ = resolve_mode("pulse", cfg)
    assert settings["period_secs"] == 42.0


def test_overrides_win_over_config():
    _, settings, _ = resolve_mode("rainbow", DEFAULT_CONFIG, {"cycle_secs": 2.5, "fps": 12})
    assert settings["cycle_secs"] == 2.5
    assert settings["fps"] == 12


def test_aliases_share_their_canonical_section():
    for alias, canonical in [("breathe", "pulse"), ("ambilight", "ambient"), ("cpu", "system")]:
        assert MODE_REGISTRY[alias][0] == MODE_REGISTRY[canonical][0]


def test_ram_alias_selects_the_ram_metric():
    _, settings, _ = resolve_mode("ram", DEFAULT_CONFIG)
    assert settings["metric"] == "ram"


def test_self_running_modes_are_the_ones_the_strip_owns():
    assert is_self_running("strobe_7_color")
    assert is_self_running("hardware")
    assert is_self_running("mic_hw")
    assert not is_self_running("rainbow")
    assert not is_self_running("music")


def test_frame_delay_respects_the_packet_rate_ceiling():
    floor = 1.0 / MAX_PACKETS_PER_SEC
    assert frame_delay(255) >= floor
    assert frame_delay(60) >= floor
    assert frame_delay(10) == pytest.approx(0.1)
    assert frame_delay(0) > 0  # must not divide by zero


def test_sleep_cancellable_returns_early_when_stopped():
    async def scenario():
        stop = Stop()
        assert await sleep_cancellable(0.05, stop) is True

        stop = Stop()
        stop.stop()
        import time

        t0 = time.monotonic()
        assert await sleep_cancellable(30.0, stop) is False
        assert time.monotonic() - t0 < 1.0

    asyncio.run(scenario())


def test_static_mode_sends_the_configured_colour():
    async def scenario():
        dev = FakeDevice()
        stop = Stop()
        await run_mode("static", dev, DEFAULT_CONFIG, stop, {"color": [10, 20, 30]})
        assert ("set_color", (10, 20, 30)) in dev.calls

    asyncio.run(scenario())


def test_firmware_mode_sends_speed_then_the_animation():
    async def scenario():
        dev = FakeDevice()
        await run_mode("strobe_7_color", dev, DEFAULT_CONFIG, Stop(), {"speed": 70})
        assert ("set_hw_mode", (HWMode.STROBE_7_COLOR, 70)) in dev.calls

    asyncio.run(scenario())


def test_a_running_mode_stops_promptly_when_asked():
    async def scenario():
        dev = FakeDevice()
        stop = Stop()
        task = asyncio.create_task(run_mode("rainbow", dev, DEFAULT_CONFIG, stop))
        await asyncio.sleep(0.25)
        stop.stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert any(c[0] == "stream_color" for c in dev.calls), "rainbow should have sent colours"

    asyncio.run(scenario())


def test_sequence_without_steps_says_what_to_do():
    async def scenario():
        with pytest.raises(ValueError, match="config.json"):
            await run_mode("sequence", FakeDevice(), DEFAULT_CONFIG, Stop(), {"steps": []})

    asyncio.run(scenario())


# ── Ambient sampling ─────────────────────────────────────────────────────────

def _image(width, height, fn):
    """Build an HxWx3 BGR frame, the layout mss hands back."""
    import numpy as np

    frame = np.zeros((height, width, 3), dtype=np.uint8)
    for row in range(height):
        for col in range(width):
            r, g, b = fn(row, col)
            frame[row, col] = (b, g, r)
    return frame


def test_a_uniform_screen_averages_to_that_colour():
    frame = _image(64, 64, lambda r, c: (10, 200, 30))
    for region in ("full", "edges", "center", "top", "bottom"):
        assert _average_region(frame, region, 1) == (10, 200, 30), region


def test_edge_region_ignores_the_centre():
    """The border matches the band the sampler uses, so edge and centre do
    not overlap."""
    n, band = 256, 32
    frame = _image(n, n, lambda r, c: (255, 0, 0) if (r < band or r >= n - band or c < band or c >= n - band) else (0, 255, 0))
    r, g, _ = _average_region(frame, "edges", 1)
    assert r > 200 and g < 55, f"edges read ({r}, {g})"
    r, g, _ = _average_region(frame, "center", 1)
    assert g > 200 and r < 55, f"centre read ({r}, {g})"


def test_near_black_is_clamped_to_black():
    """A dark screen must go dark, not pick a hue out of the noise."""
    assert _grade((3, 2, 4), 2.0, 1.0, 8) == (0, 0, 0)
    assert _grade((3, 2, 4), 2.0, 1.0, 0) != (0, 0, 0)


def test_grading_a_grey_leaves_it_grey():
    r, g, b = _grade((128, 128, 128), 2.0, 1.0, 8)
    assert r == g == b
