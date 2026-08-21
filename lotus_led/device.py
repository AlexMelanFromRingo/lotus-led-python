"""Async BLE driver for BLEDOM / ELK-BLEDOM controllers.

Scanning, connecting, rate-limited writes, auto-reconnect, and a shadow copy of
everything we have told the strip to do.

Why there is no "read the current state" call
---------------------------------------------
This firmware is write-only in practice. ``FFF4`` advertises the notify
property but never fires, and the vendor Android app does not even subscribe to
it. Reading ``FFF3`` returns a fixed firmware identity string, not live state.

So the driver tracks a :class:`ShadowState` instead: every packet we send
updates it. It is accurate as long as nothing else is talking to the strip, and
it is the only state a UI can meaningfully display.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from bleak import BleakClient, BleakScanner

from .protocol import (
    DEVICE_NAME_PATTERNS,
    KNOWN_MAC_PREFIX,
    MAX_PACKETS_PER_SEC,
    WRITE_UUID,
    HWMode,
    LightChannel,
    MicEq,
    Pkt,
)

log = logging.getLogger("lotus.device")

_MIN_INTERVAL = 1.0 / MAX_PACKETS_PER_SEC

#: Wait this long between reconnect attempts. Without it, a 20 fps mode turns an
#: unplugged strip into twenty connection attempts a second.
_RECONNECT_INTERVAL = 1.5

#: Give up after this many attempts in a row. Retrying forever leaves a UI
#: claiming to be connected to a strip that is switched off.
_RECONNECT_ATTEMPTS = 4

#: What the user is told when the link is gone.
#:
#: Deliberately free of platform error codes — they name nothing the user can
#: act on, and the remedy is the same whatever the code. The underlying error
#: still goes to the log.
LINK_LOST = (
    "The strip stopped responding. These controllers drop the link at range — "
    "move closer, then connect again."
)


@dataclass
class FoundDevice:
    name: str
    address: str
    rssi: Optional[int] = None

    def __str__(self) -> str:
        name = self.name or "(unnamed)"
        if self.rssi is None:
            return f"{name:22} [{self.address}]"
        return f"{name:22} [{self.address}]  {self.rssi} dBm"


@dataclass
class ShadowState:
    """What we believe the strip is doing, derived from what we sent it."""

    #: False until we have sent the strip something. A freshly connected strip
    #: keeps whatever it was doing before and will not tell us what that is, so
    #: the other fields are placeholders until this turns true.
    known: bool = False
    power: bool = False
    r: int = 255
    g: int = 255
    b: int = 255
    brightness: int = 100
    speed: int = 50
    hw_mode: Optional[HWMode] = None

    def as_dict(self) -> dict:
        return {
            "known": self.known,
            "power": self.power,
            "r": self.r,
            "g": self.g,
            "b": self.b,
            "brightness": self.brightness,
            "speed": self.speed,
            "hw_mode": self.hw_mode.mode_name if self.hw_mode else None,
        }

    def __str__(self) -> str:
        if not self.known:
            return "unknown — nothing sent to the strip yet this session"
        mode = self.hw_mode.mode_name if self.hw_mode else "-"
        return (
            f"Power: {'ON' if self.power else 'OFF'}  "
            f"RGB: ({self.r}, {self.g}, {self.b})  "
            f"Brightness: {self.brightness}  Speed: {self.speed}  Mode: {mode}"
        )


def looks_like_bledom(name: str, address: str) -> bool:
    upper = (name or "").upper()
    return any(p in upper for p in DEVICE_NAME_PATTERNS) or address.upper().startswith(
        KNOWN_MAC_PREFIX
    )


async def scan(timeout: float = 6.0) -> List[FoundDevice]:
    """Find BLEDOM-like strips, strongest signal first."""
    found: List[FoundDevice] = []
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in discovered.items():
        name = device.name or adv.local_name or ""
        if looks_like_bledom(name, address):
            found.append(FoundDevice(name=name or "(unnamed)", address=address, rssi=adv.rssi))
    found.sort(key=lambda d: d.rssi if d.rssi is not None else -999, reverse=True)
    return found


class BLEDOMDevice:
    """One connected strip."""

    def __init__(self, address: str, name: str = ""):
        self.address = address
        self.name = name if name and name != address else ""
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._last_send = 0.0
        self._last_reconnect = 0.0
        self._reconnect_failures = 0
        self._state = ShadowState()
        self._listeners: List[Callable[[ShadowState], None]] = []

    # ── Discovery & connection ───────────────────────────────────────────────

    @classmethod
    async def connect(
        cls,
        identifier: str = "",
        scan_timeout: float = 6.0,
        attempts: int = 3,
    ) -> "BLEDOMDevice":
        """Connect by MAC, by name fragment, or to the nearest strip.

        Raises :class:`RuntimeError` with a message that says what to try next,
        because "connection failed" on its own is useless when the usual cause
        is a phone still holding the single available connection.
        """
        wanted = identifier.strip().upper()
        target: Optional[FoundDevice] = None

        candidates = await scan(scan_timeout)
        if wanted:
            for d in candidates:
                if d.address.upper() == wanted or wanted in d.name.upper():
                    target = d
                    break
            if target is None:
                # A MAC we were given directly may not be advertising right
                # now — it is often busy with the connection we just closed.
                # Connect anyway, but do not pretend the MAC is a name.
                target = FoundDevice(name="", address=identifier)
        elif candidates:
            target = candidates[0]

        if target is None:
            raise RuntimeError(
                "No strip found. Check it is powered, and that a phone is not "
                "already connected — these controllers accept one connection at a time."
            )

        device = cls(target.address, target.name)
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                client = BleakClient(target.address, timeout=15.0)
                await client.connect()
                device._client = client
                log.info("connected to %s", target.address)
                return device
            except Exception as exc:  # noqa: BLE001 - surfaced to the user below
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(1.0)

        raise RuntimeError(f"Could not connect to {target.address}: {last_error}")

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
            self._client = None

    async def read_firmware(self) -> str:
        """The firmware identity string, e.g. ``ELKP10Y60H_OLSMN_V02``.

        The only thing the controller will tell us about itself.
        """
        if self._client is None:
            return "unknown"
        try:
            data = await self._client.read_gatt_char(WRITE_UUID)
            return data.decode("ascii", errors="replace").strip() or "unknown"
        except Exception:  # noqa: BLE001 - not every unit allows the read
            return "unknown"

    # ── State ────────────────────────────────────────────────────────────────

    @property
    def state(self) -> ShadowState:
        return self._state

    def on_state_change(self, callback: Callable[[ShadowState], None]) -> None:
        self._listeners.append(callback)

    def _touch(self, **changes) -> None:
        self._state.known = True
        for key, value in changes.items():
            setattr(self._state, key, value)
        for listener in self._listeners:
            try:
                listener(self._state)
            except Exception:  # noqa: BLE001 - a bad listener must not break the strip
                log.exception("state listener failed")

    # ── Sending ──────────────────────────────────────────────────────────────

    async def send(self, packet: bytearray) -> None:
        """Send one frame, rate-limited, reconnecting if the link has dropped.

        The lock serialises callers, so only one recovery runs at a time. These
        controllers drop the link on their own at the edge of range, and a mode
        that has been running for hours should recover rather than die.
        """
        async with self._lock:
            gap = _MIN_INTERVAL - (time.monotonic() - self._last_send)
            if gap > 0:
                await asyncio.sleep(gap)

            try:
                await self._write(packet)
                self._last_send = time.monotonic()
                self._reconnect_failures = 0
                return
            except Exception as first:  # noqa: BLE001
                self._last_send = time.monotonic()

            await self._recover(first)
            try:
                await self._write(packet)
                self._reconnect_failures = 0
            except Exception as exc:  # noqa: BLE001
                log.warning("write failed again after reconnecting: %s", exc)
                raise RuntimeError(LINK_LOST) from exc
            finally:
                self._last_send = time.monotonic()

    async def _write(self, packet: bytearray) -> None:
        if self._client is None:
            raise RuntimeError("not connected")
        await self._client.write_gatt_char(WRITE_UUID, packet, response=False)

    async def _recover(self, cause: Exception) -> None:
        """Try to get the link back, pacing attempts and giving up eventually.

        ``cause`` is logged rather than shown: platform BLE errors are opaque
        codes, and every one of them means the same thing here.
        """
        if self._reconnect_failures >= _RECONNECT_ATTEMPTS:
            raise RuntimeError(LINK_LOST) from cause

        since = time.monotonic() - self._last_reconnect
        if since < _RECONNECT_INTERVAL:
            await asyncio.sleep(_RECONNECT_INTERVAL - since)
        self._last_reconnect = time.monotonic()

        log.warning(
            "link to %s dropped (%s) — reconnecting, attempt %d/%d",
            self.address, cause, self._reconnect_failures + 1, _RECONNECT_ATTEMPTS,
        )

        try:
            await self.disconnect()
            client = BleakClient(self.address, timeout=15.0)
            await client.connect()
            self._client = client
            await self._restore_state()
        except Exception as exc:  # noqa: BLE001
            self._reconnect_failures += 1
            log.warning("reconnect failed: %s", exc)
            raise RuntimeError(LINK_LOST) from exc

        self._reconnect_failures = 0
        log.info("reconnected")

    async def _restore_state(self) -> None:
        """Push our shadow state back, since the strip forgets on a power cycle."""
        if not self._state.known:
            return
        try:
            await self._write(Pkt.power(self._state.power))
            await self._write(Pkt.brightness(self._state.brightness))
            if self._state.hw_mode is not None:
                await self._write(Pkt.speed(self._state.speed))
                await self._write(Pkt.hw_mode(self._state.hw_mode))
            else:
                await self._write(Pkt.color(self._state.r, self._state.g, self._state.b))
        except Exception:  # noqa: BLE001 - restoring is best-effort
            log.debug("could not restore state after reconnect", exc_info=True)

    # ── Commands ─────────────────────────────────────────────────────────────

    async def power_on(self) -> None:
        await self.send(Pkt.power_on())
        self._touch(power=True)

    async def power_off(self) -> None:
        await self.send(Pkt.power_off())
        self._touch(power=False)

    async def set_color(self, r: int, g: int, b: int) -> None:
        await self.send(Pkt.color(r, g, b))
        # Writing a static colour stops whatever animation was running.
        self._touch(r=r, g=g, b=b, hw_mode=None)

    async def stream_color(self, r: int, g: int, b: int) -> None:
        """Colour write tagged as part of a rapid stream (audio, ambilight).

        Skips the state broadcast: at 20 fps the notifications would swamp any
        listener, and a UI does not want to chase them.
        """
        await self.send(Pkt.color_streaming(r, g, b))
        self._state.known = True
        self._state.r, self._state.g, self._state.b = r, g, b
        self._state.hw_mode = None

    async def set_brightness(self, level: int, channel: LightChannel = LightChannel.DEFAULT) -> None:
        await self.send(Pkt.brightness(level, channel))
        self._touch(brightness=max(0, min(100, int(level))))

    async def set_speed(self, level: int) -> None:
        await self.send(Pkt.speed(level))
        self._touch(speed=max(0, min(100, int(level))))

    async def set_hw_mode(self, mode: HWMode, speed: int = 50) -> None:
        """Start a firmware animation.

        Speed is a separate frame, sent first — the firmware applies it to
        whichever animation runs next.
        """
        await self.send(Pkt.speed(speed))
        await self.send(Pkt.hw_mode(mode))
        self._touch(speed=max(0, min(100, int(speed))), hw_mode=mode)

    async def set_cct(self, warm: int, cold: int) -> None:
        await self.send(Pkt.cct(warm, cold))

    async def set_mic(self, on: bool) -> None:
        await self.send(Pkt.mic_enabled(on))

    async def set_mic_sensitivity(self, sensitivity: int) -> None:
        await self.send(Pkt.mic_sensitivity(sensitivity))

    async def set_mic_eq(self, eq: MicEq) -> None:
        await self.send(Pkt.mic_eq(eq))

    async def set_pin_order(self, r: int, g: int, b: int) -> None:
        await self.send(Pkt.pin_order(r, g, b))


class DeviceGroup:
    """Drive several strips as one.

    Each device keeps its own rate limiter, so a broadcast goes out in parallel
    without any of them exceeding the packet ceiling. One strip failing does not
    abort the others — a half-applied group looks worse than a fully-applied one
    with a warning.
    """

    def __init__(self, devices: Sequence[BLEDOMDevice]):
        self.devices: List[BLEDOMDevice] = list(devices)

    def __len__(self) -> int:
        return len(self.devices)

    def __getitem__(self, index: int) -> BLEDOMDevice:
        return self.devices[index]

    @property
    def primary(self) -> BLEDOMDevice:
        return self.devices[0]

    async def _fan_out(self, method: str, *args) -> List[Optional[Exception]]:
        results = await asyncio.gather(
            *(getattr(d, method)(*args) for d in self.devices),
            return_exceptions=True,
        )
        errors: List[Optional[Exception]] = []
        for device, result in zip(self.devices, results):
            if isinstance(result, Exception):
                log.warning("%s failed on %s: %s", method, device.address, result)
                errors.append(result)
            else:
                errors.append(None)
        return errors

    async def power_on(self):
        return await self._fan_out("power_on")

    async def power_off(self):
        return await self._fan_out("power_off")

    async def set_color(self, r: int, g: int, b: int):
        return await self._fan_out("set_color", r, g, b)

    async def set_brightness(self, level: int):
        return await self._fan_out("set_brightness", level)

    async def set_speed(self, level: int):
        return await self._fan_out("set_speed", level)

    async def set_hw_mode(self, mode: HWMode, speed: int = 50):
        return await self._fan_out("set_hw_mode", mode, speed)

    async def disconnect_all(self) -> None:
        await asyncio.gather(*(d.disconnect() for d in self.devices), return_exceptions=True)
