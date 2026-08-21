# lotus-led-python

Control **BLEDOM / ELK-BLEDOM / "Lotus Lantern"** LED strips from a PC over
Bluetooth LE.

There is a [Rust twin](https://github.com/AlexMelanFromRingo/lotus-led) with the
same commands, the same mode names and the same `config.json` — plus a web UI
and a C ABI. Use whichever suits; they are interchangeable.

## What it does

| | |
|---|---|
| **Colour** | Any RGB value, hex or triplet. Brightness and animation speed. |
| **Firmware animations** | All 29 the controller has, by name. They keep running after the PC disconnects. |
| **Software animations** | pulse · rainbow · wave · fire · meteor · comet · sunrise · sunset · sleep timer · colour temperature · alarm |
| **Audio reactive** | Spectrum-to-colour and beat detection, from real system audio — see below. |
| **Ambilight** | Screen colour on the wall, with region and smoothing control. |
| **Automatic** | Switch modes when a game or a video player starts, colour by foreground app, time-of-day schedule, system load heatmap. |
| **Scenes** | Named presets in `config.json`. |
| **Multiple strips** | Driven in lockstep. |

The strip is **not addressable** — three PWM channels for the whole run. Every
"animation" here is a colour sequence over time, not over space. Effects that
travel along the strip are not possible on this hardware.

## Install

Windows, from the project folder:

```bat
install.bat
```

Anywhere:

```bash
pip install -e ".[full]"
```

Only `bleak` is required; the extras add audio, ambilight and system modes.

**WSL2 has no Bluetooth stack.** Run this on Windows itself — `install.bat` and
`run.bat` are there for exactly that. From WSL2 you can still call the Windows
copy through `powershell.exe`.

## Using it

```bash
led scan                     # or: run.bat scan
led on
led color ff8800             # or: led color 255 136 0
led brightness 70

led mode rainbow --period 6
led mode music               # beat sync from system audio
led mode ambient             # match the screen
led mode strobe_7_color      # firmware animation; keeps going after we exit

led scene party
led modes                    # everything available
led probe                    # walk all 29 firmware animations, watch the strip
led                          # interactive
```

`python -m lotus_led.cli`, `python lotus_controller.py` and `led` are the same
program.

First run uses built-in defaults; `led init-config` writes them out so you can
edit them:

```jsonc
{
  "device": { "mac": "BE:60:65:00:8E:F4" }
}
```

Every mode's settings live in that file — periods, colours, frame rates, audio
bands, appwatch rules, the schedule. `led config` prints the active one. It is
the same file the Rust version reads, so settings carry across.

## System audio, properly

Audio-reactive modes capture the **output** you are actually listening to.

On Windows that needs WASAPI loopback. `sounddevice`/PortAudio cannot do it —
it only opens capture endpoints, so "loopback" lands on a microphone unless you
have enabled the legacy "Stereo Mix" device, off by default on Windows 11. With
`soundcard` installed (part of `[full]`) this opens the render endpoint
directly:

```
[audio] listening on WASAPI loopback: Headset Earphone
```

If it says `microphone (fallback)` instead, it is hearing the room, not the PC.

Nothing is *routed* anywhere, only observed — this cannot hijack an output
device the way the vendor phone app does.

## As a library

```python
import asyncio
from lotus_led import BLEDOMDevice, HWMode

async def main():
    strip = await BLEDOMDevice.connect()          # nearest strip
    await strip.power_on()
    await strip.set_color(255, 128, 0)
    await strip.set_hw_mode(HWMode.CROSS_FADE_7_COLOR, speed=60)
    await strip.disconnect()

asyncio.run(main())
```

Modes are separate from the device, so you can run one and stop it whenever:

```python
from lotus_led import run_mode, Stop, load_config

stop = Stop()
task = asyncio.create_task(run_mode("music", strip, load_config(), stop))
await asyncio.sleep(30)
stop.stop()
await task
```

Layout mirrors the Rust version: `protocol` (packets, no I/O) · `device` (BLE,
rate limiting, shadow state) · `modes` · `config` · `cli`.

## Protocol

Nine-byte frames over service `FFF0`, write characteristic `FFF3`. Documented in
full at
[BLEDOM-Protocol-Reversing](https://github.com/AlexMelanFromRingo/BLEDOM-Protocol-Reversing),
recovered from the vendor Android app and verified on hardware.

Two things worth knowing before writing any client:

- **The strip reports nothing.** `FFF4` advertises notifications and never sends
  one. This package tracks what it has sent instead, and says so rather than
  inventing a reading.
- **Bad frames are ignored in silence.** Every version of this project before
  3.0 sent a malformed set-mode frame, so firmware animations appeared not to
  exist. The tests now pin every frame byte-for-byte against the vendor app.

## Tests

```bash
pytest
```

No hardware needed — protocol, config, mode resolution, spectral analysis and
screen sampling are all covered. For the parts only a strip can answer, use
`led probe`.

## Licence

MIT
