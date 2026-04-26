#!/usr/bin/env bash
# Lotus LED Controller - WSL2 Setup Helper
# Run from WSL2 to install the Windows Python venv and set up the `led` CLI alias.

set -e

WIN_DIR="C:\\Users\\Alex_Melan\\Desktop\\Tests\\lotus_led"
WSL_DIR="/mnt/c/Users/Alex_Melan/Desktop/Tests/lotus_led"
INSTALL_BAT="${WIN_DIR}\\install.bat"

echo "======================================================"
echo "  Lotus LED Controller - WSL2 Setup"
echo "======================================================"

# --- Check PowerShell bridge ---
if ! command -v powershell.exe &>/dev/null; then
    echo "[ERROR] powershell.exe not found in PATH from WSL2."
    echo "        Make sure you are running WSL2 (not WSL1)."
    exit 1
fi
echo "[OK] PowerShell bridge detected."

# --- Check Windows project dir ---
if [ ! -f "${WSL_DIR}/lotus_controller.py" ]; then
    echo "[ERROR] lotus_controller.py not found at ${WSL_DIR}"
    echo "        Make sure files are in C:\\Users\\Alex_Melan\\Desktop\\Tests\\lotus_led\\"
    exit 1
fi
echo "[OK] Python controller found."

# --- Run install.bat on Windows to set up venv ---
echo ""
echo "Running install.bat on Windows (creates venv, installs packages)..."
powershell.exe -ExecutionPolicy Bypass -Command \
    "Start-Process cmd.exe -ArgumentList '/c ${INSTALL_BAT}' -Verb RunAs -Wait" 2>/dev/null || \
powershell.exe -ExecutionPolicy Bypass -Command \
    "cmd.exe /c '${INSTALL_BAT}'"

# --- Build Rust WSL2 launcher (if cargo is available) ---
RUST_PROJECT="$(dirname "$(readlink -f "$0")" 2>/dev/null)" || RUST_PROJECT="."
LED_RUST_DIR="$HOME/Rust/led_control_tool"

if command -v cargo &>/dev/null && [ -f "${LED_RUST_DIR}/Cargo.toml" ]; then
    echo ""
    echo "Building Rust WSL2 launcher..."
    (cd "${LED_RUST_DIR}" && cargo build --release 2>&1) && \
        echo "[OK] Built: ${LED_RUST_DIR}/target/release/led" || \
        echo "[WARN] Rust build failed. You can still use run.bat directly."

    # Add alias if not already in .bashrc / .zshrc
    ALIAS_LINE="alias led='${LED_RUST_DIR}/target/release/led'"
    for RC in ~/.bashrc ~/.zshrc; do
        if [ -f "$RC" ] && ! grep -q "alias led=" "$RC"; then
            echo "" >> "$RC"
            echo "# Lotus LED Controller" >> "$RC"
            echo "${ALIAS_LINE}" >> "$RC"
            echo "[OK] Added 'led' alias to $RC"
        fi
    done
else
    echo "[SKIP] cargo not found or Rust project missing — skipping Rust build."
fi

echo ""
echo "======================================================"
echo "  Setup complete!"
echo ""
echo "  From WSL2 you can now run:"
echo "    led on"
echo "    led off"
echo "    led color 255 0 128"
echo "    led mode rainbow"
echo "    led mode audio"
echo "    led mode ambient"
echo "    led scene movie"
echo "    led scan"
echo ""
echo "  Or run directly on Windows:"
echo "    C:\\Users\\Alex_Melan\\Desktop\\Tests\\lotus_led\\run.bat [args]"
echo "======================================================"
