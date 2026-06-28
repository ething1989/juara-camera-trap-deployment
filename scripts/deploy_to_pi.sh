#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-raspberrypi.local}"
PI_USER="${PI_USER:-juara2026pi1}"
REMOTE_SRC="${REMOTE_SRC:-/home/$PI_USER/juara-wildlife-station-src}"
INSTALL_AI="${INSTALL_AI:-1}"
INSTALL_BIRDNET="${INSTALL_BIRDNET:-$INSTALL_AI}"
INSTALL_SPECIESNET="${INSTALL_SPECIESNET:-0}"
INSTALL_CAMERA="${INSTALL_CAMERA:-1}"
DISABLE_BLUETOOTH_UART="${DISABLE_BLUETOOTH_UART:-0}"
CONFIG_TEMPLATE="${CONFIG_TEMPLATE:-configs/station.june2026trap.example.toml}"
AUDIO_DEVICE="${AUDIO_DEVICE:-}"
REBOOT_AFTER_INSTALL="${REBOOT_AFTER_INSTALL:-0}"
RESET_CONFIG="${RESET_CONFIG:-0}"
SSH_OPTS="${SSH_OPTS:-}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_TARGET="$PI_USER@$PI_HOST"
SSH_ARGS=()
if [[ -n "$SSH_OPTS" ]]; then
  # shellcheck disable=SC2206
  SSH_ARGS=($SSH_OPTS)
fi
RSYNC_SSH="${RSYNC_RSH:-ssh $SSH_OPTS}"

rsync -az --delete \
  -e "$RSYNC_SSH" \
  --exclude ".DS_Store" \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude ".local-tests" \
  --exclude ".local_run" \
  --exclude ".pytest_cache" \
  --exclude "__pycache__" \
  --exclude "*.egg-info" \
  --exclude "data/bird_playback_test" \
  "$REPO_DIR/" "$PI_TARGET:$REMOTE_SRC/"

ssh_command=(ssh)
if [[ ${#SSH_ARGS[@]} -gt 0 ]]; then
  ssh_command+=("${SSH_ARGS[@]}")
fi
ssh_command+=(-t "$PI_TARGET")
"${ssh_command[@]}" "cd '$REMOTE_SRC' && sudo env INSTALL_AI='$INSTALL_AI' INSTALL_BIRDNET='$INSTALL_BIRDNET' INSTALL_SPECIESNET='$INSTALL_SPECIESNET' INSTALL_CAMERA='$INSTALL_CAMERA' DISABLE_BLUETOOTH_UART='$DISABLE_BLUETOOTH_UART' CONFIG_TEMPLATE='$CONFIG_TEMPLATE' AUDIO_DEVICE='$AUDIO_DEVICE' SERVICE_USER='$PI_USER' RESET_CONFIG='$RESET_CONFIG' scripts/install_pi.sh"

echo "Deploy complete. Run the Pi preflight with:"
echo "ssh -t $PI_TARGET 'sudo $REMOTE_SRC/scripts/pi_preflight.sh'"

if [[ "$REBOOT_AFTER_INSTALL" == "1" ]]; then
  "${ssh_command[@]}" "sudo reboot"
else
  echo "A reboot is recommended after first install so the DS3231 RTC overlay is active."
fi
