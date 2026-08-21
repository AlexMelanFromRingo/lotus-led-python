"""Control BLEDOM / ELK-BLEDOM / "Lotus Lantern" LED strips over Bluetooth LE.

The package is layered so each piece is usable on its own:

* :mod:`lotus_led.protocol` — pure packet construction, no I/O.
* :mod:`lotus_led.device`   — BLE transport, rate limiting, shadow state.
* :mod:`lotus_led.modes`    — animations and reactive modes.
* :mod:`lotus_led.config`   — the on-disk ``config.json``.
* :mod:`lotus_led.cli`      — the ``led`` command.

Quick start::

    import asyncio
    from lotus_led import BLEDOMDevice, HWMode

    async def main():
        strip = await BLEDOMDevice.connect()      # nearest strip
        await strip.power_on()
        await strip.set_color(255, 128, 0)
        await strip.set_hw_mode(HWMode.CROSS_FADE_7_COLOR, speed=60)
        await strip.disconnect()

    asyncio.run(main())

This is the Python half of a pair; ``lotus-led`` is the same controller in Rust,
with the same protocol, mode names and ``config.json``.
"""

from .config import DEFAULT_CONFIG, load_config, save_config
from .device import BLEDOMDevice, DeviceGroup, FoundDevice, ShadowState, scan
from .modes import MODE_NAMES, Stop, run_mode
from .protocol import (
    HWMode,
    LightChannel,
    MicEq,
    Pkt,
    cct_to_rgb,
    hsv_to_rgb,
    hw_mode_from_name,
    lerp_color,
    parse_color,
    rgb_to_hsv,
)

__version__ = "3.0.0"

__all__ = [
    "BLEDOMDevice", "DeviceGroup", "FoundDevice", "ShadowState", "scan",
    "HWMode", "LightChannel", "MicEq", "Pkt",
    "hsv_to_rgb", "rgb_to_hsv", "lerp_color", "cct_to_rgb", "parse_color",
    "hw_mode_from_name",
    "DEFAULT_CONFIG", "load_config", "save_config",
    "MODE_NAMES", "Stop", "run_mode",
    "__version__",
]
