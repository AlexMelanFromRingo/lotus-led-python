"""Command-line control for BLEDOM / ELK-BLEDOM strips.

Command names, flags and behaviour match the Rust ``led`` binary — the two are
interchangeable, and share one ``config.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any, Dict, List, Optional

from .config import DEFAULT_CONFIG, config_path, load_config, save_config, save_default
from .device import BLEDOMDevice, DeviceGroup, scan
from .modes import (
    MODE_DESCRIPTIONS,
    MODE_NAMES,
    Stop,
    is_self_running,
    run_mode,
    sleep_cancellable,
)
from .protocol import (
    HWMode,
    LightChannel,
    MicEq,
    Pkt,
    hex_frame,
    hw_mode_from_name,
    parse_color,
)

log = logging.getLogger("lotus")

# Windows consoles default to a legacy code page; without this, any non-ASCII
# output raises UnicodeEncodeError instead of printing.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - older Pythons lack reconfigure
        pass


# ── Argument parsing ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="led",
        description="Control BLEDOM / ELK-BLEDOM / Lotus Lantern LED strips",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  led scan
  led color 255 0 128           led color ff0080
  led brightness 70
  led mode rainbow --period 4
  led mode music                # beat sync from system audio
  led mode ambient              # match the screen
  led mode strobe_7_color       # firmware animation, keeps running after exit
  led scene party
  led probe                     # walk every firmware animation
""",
    )
    parser.add_argument("--mac", help="device MAC or name fragment (overrides config.json)")
    parser.add_argument("--config", type=str, help="path to config.json")
    parser.add_argument("--scan-timeout", type=float, help="seconds to scan when discovering")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")

    sub = parser.add_subparsers(dest="command")

    scan_p = sub.add_parser("scan", help="find strips in range")
    scan_p.add_argument("timeout", nargs="?", type=float, default=6.0)

    sub.add_parser("on", help="power on")
    sub.add_parser("off", help="power off")
    sub.add_parser("status", help="show connection details and last known state")
    sub.add_parser("config", help="print the active config")
    sub.add_parser("tui", help="interactive terminal control")

    modes_p = sub.add_parser("modes", help="list every mode")
    modes_p.add_argument("--firmware", action="store_true", help="only firmware animations")

    init_p = sub.add_parser("init-config", help="write a fresh config.json")
    init_p.add_argument("--force", action="store_true", help="overwrite an existing file")

    color_p = sub.add_parser("color", help="set a static colour")
    color_p.add_argument("value", nargs="+", help="R G B, or a hex value")

    bright_p = sub.add_parser("brightness", help="set brightness 0-100")
    bright_p.add_argument("level", type=int)
    bright_p.add_argument("--channel", help="all, rgb, white, cct, laser")

    speed_p = sub.add_parser("speed", help="set firmware-animation speed 0-100")
    speed_p.add_argument("level", type=int)

    white_p = sub.add_parser("white", help="warm/cold white mix, 0-100 each")
    white_p.add_argument("warm", type=int)
    white_p.add_argument("cold", type=int)

    pin_p = sub.add_parser("pin-order", help="fix swapped colours by remapping driver pins")
    pin_p.add_argument("r", type=int)
    pin_p.add_argument("g", type=int)
    pin_p.add_argument("b", type=int)

    mic_p = sub.add_parser("mic", help="control the strip's own microphone")
    mic_p.add_argument("state", nargs="?", default="on", help="on or off")
    mic_p.add_argument("--sensitivity", type=int, help="0-100")
    mic_p.add_argument("--eq", help="classic, soft, dynamic, disco")

    raw_p = sub.add_parser("raw", help="send a raw 9-byte frame")
    raw_p.add_argument("bytes", nargs=9, help="nine hex bytes, e.g. 7E 05 03 95 03 FF FF 00 EF")

    probe_p = sub.add_parser("probe", help="walk through firmware animations")
    probe_p.add_argument("--hold", type=float, default=4.0, help="seconds per animation")
    probe_p.add_argument("--from", dest="start", help="start at this hex opcode, e.g. 0x88")

    mode_p = sub.add_parser("mode", help="run a mode until Ctrl-C")
    mode_p.add_argument("name", help="mode name — see 'led modes'")
    mode_p.add_argument("--color", nargs="+", help="R G B, or a hex value")
    mode_p.add_argument("--brightness", type=int)
    mode_p.add_argument("--speed", type=int)
    mode_p.add_argument("--fps", type=int)
    mode_p.add_argument("--period", type=float, help="cycle or pulse period, seconds")
    mode_p.add_argument("--temp", type=int, help="colour temperature in Kelvin (cct)")
    mode_p.add_argument("--duration", type=int, help="seconds (sunrise, sunset, sleep_timer)")
    mode_p.add_argument("--sensitivity", type=float, help="audio sensitivity multiplier")
    mode_p.add_argument("--run", type=float, help="stop after N seconds")

    scene_p = sub.add_parser("scene", help="apply a named scene from config.json")
    scene_p.add_argument("name")

    return parser


def mode_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Turn CLI flags into per-mode settings.

    A flag is mapped onto whichever field name the target mode uses, so
    ``--period`` works for both ``pulse`` (``period_secs``) and ``rainbow``
    (``cycle_secs``).
    """
    out: Dict[str, Any] = {}
    if args.color:
        out["color"] = list(parse_color(args.color if len(args.color) > 1 else args.color[0]))
    if args.brightness is not None:
        out["brightness"] = args.brightness
        out["max_brightness"] = args.brightness
    if args.speed is not None:
        out["speed"] = args.speed
    if args.fps is not None:
        out["fps"] = args.fps
    if args.period is not None:
        out["period_secs"] = args.period
        out["cycle_secs"] = args.period
    if args.temp is not None:
        out["kelvin"] = args.temp
    if args.duration is not None:
        out["duration_secs"] = args.duration
    if args.sensitivity is not None:
        out["sensitivity"] = args.sensitivity
    return out


# ── Output ───────────────────────────────────────────────────────────────────

def print_modes(firmware_only: bool) -> None:
    if not firmware_only:
        print("Software modes — the PC computes each frame:")
        for name in MODE_NAMES:
            print(f"  {name:<14} {MODE_DESCRIPTIONS.get(name, '')}")
        print()
    print("Firmware animations — run on the strip, survive disconnection:")
    for mode in HWMode:
        print(f"  {mode.mode_name:<20} {mode.code:#04x}  {mode.label}")
    if not firmware_only:
        print(f"\nScenes: {' '.join(sorted(DEFAULT_CONFIG['scenes']))}")


# ── Connection ───────────────────────────────────────────────────────────────

async def connect_all(cfg: Dict[str, Any]) -> DeviceGroup:
    """Connect to the primary strip, plus any extras listed in device.group."""
    device_cfg = cfg["device"]
    timeout = float(device_cfg.get("scan_timeout_secs", 6.0))
    label = device_cfg.get("mac") or "auto-discover"
    print(f"Connecting ({label})…")

    primary = await BLEDOMDevice.connect(
        device_cfg.get("mac", ""), timeout, int(device_cfg.get("reconnect_attempts", 3))
    )
    print(f"Connected: {primary.name or 'strip'} [{primary.address}]")

    devices = [primary]
    for extra in device_cfg.get("group", []):
        try:
            member = await BLEDOMDevice.connect(extra, timeout, 1)
            print(f"  + group member {member.name or 'strip'} [{member.address}]")
            devices.append(member)
        except Exception as exc:  # noqa: BLE001 - one unreachable strip must not stop the rest
            print(f"  ! group member '{extra}' unavailable: {exc}", file=sys.stderr)

    pins = device_cfg.get("pin_order")
    if pins:
        await primary.set_pin_order(*pins)

    return DeviceGroup(devices)


# ── Interactive ──────────────────────────────────────────────────────────────

async def interactive(group: DeviceGroup, cfg: Dict[str, Any], stop: Stop) -> None:
    """A small REPL, for when you want to try several things in a row."""
    dev = group.primary
    print("\nType a command, or 'help'. Blank line or 'quit' exits.")
    current: Optional[asyncio.Task] = None
    current_stop: Optional[Stop] = None

    async def halt() -> None:
        nonlocal current, current_stop
        if current_stop is not None:
            current_stop.stop()
        if current is not None:
            current.cancel()
            try:
                await current
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        current, current_stop = None, None

    while stop.running:
        try:
            line = (await asyncio.get_running_loop().run_in_executor(None, input, "led> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line in ("quit", "exit", "q"):
            break

        parts = line.split()
        cmd, rest = parts[0].lower(), parts[1:]
        try:
            if cmd == "help":
                print("  on | off | color <rgb|hex> | brightness N | speed N")
                print("  mode <name> | scene <name> | stop | modes | status | quit")
            elif cmd == "on":
                await group.power_on()
            elif cmd == "off":
                await halt()
                await group.power_off()
            elif cmd == "stop":
                await halt()
                print("stopped")
            elif cmd == "color" and rest:
                await halt()
                await group.set_color(*parse_color(rest if len(rest) > 1 else rest[0]))
            elif cmd == "brightness" and rest:
                await group.set_brightness(int(rest[0]))
            elif cmd == "speed" and rest:
                await group.set_speed(int(rest[0]))
            elif cmd == "modes":
                print_modes(False)
            elif cmd == "status":
                print(dev.state)
            elif cmd in ("mode", "scene") and rest:
                await halt()
                name = rest[0]
                if cmd == "scene":
                    scene = cfg["scenes"].get(name)
                    if not scene:
                        print(f"unknown scene '{name}' — have: {', '.join(sorted(cfg['scenes']))}")
                        continue
                    name = scene.get("mode", "static")
                await group.power_on()
                current_stop = Stop()
                current = asyncio.create_task(run_mode(name, dev, cfg, current_stop))
                print(f"running {name}")
            else:
                print(f"don't know '{cmd}' — try 'help'")
        except Exception as exc:  # noqa: BLE001 - a bad command must not end the session
            print(f"error: {exc}")

    await halt()


# ── Command dispatch ─────────────────────────────────────────────────────────

async def run_command(args: argparse.Namespace, cfg: Dict[str, Any], path) -> int:
    command = args.command

    # -- no connection needed ------------------------------------------------
    if command == "scan":
        print(f"Scanning for {args.timeout}s…")
        found = await scan(args.timeout)
        if not found:
            print("No strips found. Make sure the strip is powered and not already")
            print("connected to a phone — it accepts one connection at a time.")
        else:
            for device in found:
                print(f"  {device}")
            print(f"\nTo use the first one every time, put this in {path}:")
            print(f'  "device": {{ "mac": "{found[0].address}" }}')
        return 0

    if command == "modes":
        print_modes(args.firmware)
        return 0

    if command == "config":
        print(f"Config: {path}")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0

    if command == "init-config":
        if path.exists() and not args.force:
            print(f"{path} already exists — pass --force to overwrite it", file=sys.stderr)
            return 1
        save_default(path)
        print(f"Wrote {path}")
        return 0

    # -- everything below talks to the strip ---------------------------------
    group = await connect_all(cfg)
    dev = group.primary
    stop = Stop()
    ran_a_mode = False

    def request_stop(*_args) -> None:
        stop.stop()
        print("\nStopping…")

    try:
        signal.signal(signal.SIGINT, request_stop)
    except ValueError:  # pragma: no cover - not the main thread
        pass

    try:
        if command == "on":
            await group.power_on()

        elif command == "off":
            await group.power_off()

        elif command == "color":
            rgb = parse_color(args.value if len(args.value) > 1 else args.value[0])
            await group.power_on()
            await group.set_color(*rgb)
            print(f"Colour set to {rgb}")

        elif command == "brightness":
            channel = LightChannel.DEFAULT
            if args.channel:
                try:
                    channel = LightChannel[args.channel.upper()]
                except KeyError:
                    print(
                        f"unknown channel '{args.channel}' — try all, rgb, white, cct, laser",
                        file=sys.stderr,
                    )
                    return 1
            for device in group.devices:
                await device.set_brightness(args.level, channel)

        elif command == "speed":
            await group.set_speed(args.level)

        elif command == "white":
            await group.power_on()
            await dev.set_cct(min(args.warm, 100), min(args.cold, 100))

        elif command == "pin-order":
            await dev.set_pin_order(args.r, args.g, args.b)
            print(f"Pin order set to R={args.r} G={args.g} B={args.b}.")
            print(f'Add "pin_order": [{args.r}, {args.g}, {args.b}] under "device" to keep it.')

        elif command == "mic":
            if args.state.lower() in ("on", "true", "1", "enable"):
                if args.eq:
                    try:
                        await dev.set_mic_eq(MicEq[args.eq.upper()])
                    except KeyError:
                        print(
                            f"unknown eq '{args.eq}' — try classic, soft, dynamic, disco",
                            file=sys.stderr,
                        )
                        return 1
                if args.sensitivity is not None:
                    await dev.set_mic_sensitivity(args.sensitivity)
                await dev.set_mic(True)
                print("On-board microphone on — this keeps running after we disconnect.")
            else:
                await dev.set_mic(False)
                print("On-board microphone off.")

        elif command == "raw":
            try:
                frame = [int(b, 16) for b in args.bytes]
            except ValueError:
                print("every byte must be hex, e.g. 7E 05 03 95 03 FF FF 00 EF", file=sys.stderr)
                return 1
            await dev.send(Pkt.raw(frame))
            print(f"Sent {hex_frame(frame)}")

        elif command == "status":
            firmware = await dev.read_firmware()
            print(f"Device    : {dev.name or 'strip'} [{dev.address}]")
            print(f"Firmware  : {firmware}")
            print(f"Connected : {dev.is_connected}")
            print(f"State     : {dev.state}")
            print()
            print("The strip does not report its own state — the line above is what we")
            print("have told it to do since connecting.")

        elif command == "probe":
            start = int(args.start, 16) if args.start else 0x80
            print(f"Walking firmware animations from {start:#04x}, {args.hold}s each.")
            print("Watch the strip and note which ones do something. Ctrl-C to stop.\n")
            await dev.power_on()
            await dev.set_brightness(100)
            for mode in HWMode:
                if not stop.running:
                    break
                if mode.code < start:
                    continue
                print(f"  {mode.code:#04x}  {mode.mode_name:<20} {mode.label}")
                await dev.set_hw_mode(mode, 50)
                if not await sleep_cancellable(args.hold, stop):
                    break
            await dev.set_color(255, 255, 255)
            print("\nDone. Start one directly with:  led mode <name>")

        elif command == "mode":
            overrides = mode_overrides(args)
            await group.power_on()
            if args.brightness is not None:
                await group.set_brightness(args.brightness)

            if is_self_running(args.name):
                await run_mode(args.name, dev, cfg, stop, overrides)
                print(f"'{args.name}' is running on the strip and will continue after we disconnect.")
            else:
                print(f"Running '{args.name}'. Ctrl-C to stop.")
                ran_a_mode = True
                task = asyncio.create_task(run_mode(args.name, dev, cfg, stop, overrides))
                if args.run:
                    await sleep_cancellable(args.run, stop)
                    stop.stop()
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        elif command == "scene":
            scene = cfg["scenes"].get(args.name)
            if not scene:
                print(
                    f"unknown scene '{args.name}' — available: {', '.join(sorted(cfg['scenes']))}",
                    file=sys.stderr,
                )
                return 1

            await group.power_on()
            if scene.get("brightness") is not None:
                await group.set_brightness(int(scene["brightness"]))
            if scene.get("speed") is not None:
                await group.set_speed(int(scene["speed"]))

            # A scene names its mode directly, firmware animations included, and
            # layers colour/period on top — "pulse, but pink and slow".
            mode_name = scene.get("mode", "static")
            overrides = {}
            if scene.get("color"):
                overrides["color"] = scene["color"]
            if scene.get("period_secs") is not None:
                overrides["period_secs"] = scene["period_secs"]
                overrides["cycle_secs"] = scene["period_secs"]
            if scene.get("speed") is not None:
                overrides["speed"] = scene["speed"]

            if is_self_running(mode_name):
                await run_mode(mode_name, dev, cfg, stop, overrides)
                print(f"Scene '{args.name}' → {mode_name}, running on the strip itself.")
            else:
                print(f"Scene '{args.name}' → {mode_name}. Ctrl-C to stop.")
                ran_a_mode = True
                await run_mode(mode_name, dev, cfg, stop, overrides)

        elif command in (None, "tui"):
            await interactive(group, cfg, stop)

    finally:
        # Only a stopped mode powers the strip off; one-shot commands like
        # `led color` are meant to leave the strip lit.
        if ran_a_mode and cfg["defaults"].get("power_off_on_exit", True) and not stop.running:
            try:
                await group.power_off()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        await group.disconnect_all()

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    path = config_path() if not args.config else __import__("pathlib").Path(args.config)
    try:
        cfg = load_config(path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.mac:
        cfg["device"]["mac"] = args.mac
    if args.scan_timeout is not None:
        cfg["device"]["scan_timeout_secs"] = args.scan_timeout

    try:
        return asyncio.run(run_command(args, cfg, path))
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
