#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PI_HOST="${PI_HOST:-juara2026pi4.local}" \
PI_USER="${PI_USER:-juara2026pi4}" \
CONFIG_TEMPLATE="${CONFIG_TEMPLATE:-configs/station.pi4.example.toml}" \
INSTALL_AI="${INSTALL_AI:-1}" \
INSTALL_BIRDNET="${INSTALL_BIRDNET:-1}" \
INSTALL_SPECIESNET="${INSTALL_SPECIESNET:-0}" \
DISABLE_BLUETOOTH_UART="${DISABLE_BLUETOOTH_UART:-1}" \
"$SCRIPT_DIR/deploy_to_pi.sh"
