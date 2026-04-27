# lotus-led-python

Python controller for **BLEDOM / ELK-BLEDOM / Lotus Lantern** LED strips over Bluetooth LE.  
Runs on Windows using a venv (bleak + sounddevice + mss).

## Features

- All BLEDOM hardware animation modes
- Software modes: rainbow, pulse, wave, fire, meteor, comet, sunrise, sunset
- **Music sync** — beat detection via WASAPI system audio loopback
- **Audio reactive** — FFT spectrum → RGB color mapping
- **Ambilight** — screen edge color ambient light
- System monitor — CPU/RAM load → color heatmap
- Game / video / app auto-detection (switches mode automatically)
- Windows notification flash
- Scene presets: movie, party, romance, relax, focus, gaming, chill
- Time-based schedule
- Interactive TUI and full CLI
- Config file (`config.json`) — auto-discovered device, mode tuning

## Quick start

```bat
:: Windows — run from the project folder
install.bat          :: first-time setup (creates venv, installs deps)

venv\Scripts\python lotus_controller.py scan
venv\Scripts\python lotus_controller.py on
venv\Scripts\python lotus_controller.py color 255 128 0
venv\Scripts\python lotus_controller.py mode rainbow
venv\Scripts\python lotus_controller.py mode music
venv\Scripts\python lotus_controller.py scene party
```

## Installation

```bat
git clone https://github.com/AlexMelanFromRingo/lotus-led-python
cd lotus-led-python
install.bat
```

`config.json` is created on first run — set your device MAC there (or leave empty for auto-discovery).

## Requirements

- Python 3.10+
- Windows 10 / 11 with Bluetooth
- `bleak`, `numpy`, `sounddevice`, `mss`, `psutil`, `rich`, `pywin32`

## Music sync

Captures system audio via WASAPI loopback (no extra virtual cable needed).  
Falls back to microphone if no loopback device is found.

```bat
venv\Scripts\python lotus_controller.py mode music
```

Press **Ctrl-C** to stop — the strip turns off automatically.

## Protocol

BLEDOM 9-byte BLE packets over service `FFF0`, write `FFF3`, notify `FFF4`.

## License

MIT
