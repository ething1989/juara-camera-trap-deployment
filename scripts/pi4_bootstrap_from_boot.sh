#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="${SERVICE_USER:-juara2026pi4}"
APP_DIR="${APP_DIR:-/opt/juara-wildlife-station}"
REMOTE_SRC="${REMOTE_SRC:-/home/$SERVICE_USER/juara-wildlife-station-src}"
BOOT_DIR="${BOOT_DIR:-}"
BUNDLE_NAME="${BUNDLE_NAME:-juara_pi4_code_update_clean.tgz}"
LOG_PATH="${LOG_PATH:-/var/log/juara-pi4-bootstrap.log}"
RUN_FULL_SETUP="${RUN_FULL_SETUP:-1}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo $0"
  exit 1
fi

if [[ -z "$BOOT_DIR" ]]; then
  if [[ -d /boot/firmware ]]; then
    BOOT_DIR=/boot/firmware
  else
    BOOT_DIR=/boot
  fi
fi

exec > >(tee -a "$LOG_PATH") 2>&1

echo "=== Juara Pi 4 bootstrap $(date -Is) ==="
echo "boot_dir=$BOOT_DIR"
echo "bundle=$BOOT_DIR/$BUNDLE_NAME"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "ERROR: user $SERVICE_USER does not exist yet."
  exit 1
fi

if [[ ! -f "$BOOT_DIR/$BUNDLE_NAME" ]]; then
  echo "ERROR: missing bundle: $BOOT_DIR/$BUNDLE_NAME"
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y rsync python3 python3-venv

workdir="$(mktemp -d /tmp/juara-pi4-bootstrap.XXXXXX)"
cleanup() {
  rm -rf "$workdir"
}
trap cleanup EXIT

tar -xzf "$BOOT_DIR/$BUNDLE_NAME" -C "$workdir"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR" "$REMOTE_SRC"
rsync -a --delete "$workdir/" "$APP_DIR/"
rsync -a --delete "$workdir/" "$REMOTE_SRC/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$REMOTE_SRC"

if [[ "$RUN_FULL_SETUP" == "1" ]]; then
  bash "$APP_DIR/scripts/pi4_finish_setup.sh"
else
  echo "RUN_FULL_SETUP=0, staged code only."
fi

echo "=== Juara Pi 4 bootstrap finished $(date -Is) ==="
