#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo from the repo root:"
  echo "  sudo scripts/install_june2026trap.sh"
  exit 1
fi

SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-june2026trap}}"

cd "$REPO_DIR"
SERVICE_USER="$SERVICE_USER" \
CONFIG_TEMPLATE="${CONFIG_TEMPLATE:-configs/station.june2026trap.example.toml}" \
INSTALL_AI="${INSTALL_AI:-1}" \
INSTALL_BIRDNET="${INSTALL_BIRDNET:-1}" \
INSTALL_SPECIESNET="${INSTALL_SPECIESNET:-0}" \
INSTALL_CAMERA="${INSTALL_CAMERA:-1}" \
RESET_CONFIG="${RESET_CONFIG:-1}" \
scripts/install_pi.sh
