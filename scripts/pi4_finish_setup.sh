#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/juara-wildlife-station}"
CONFIG_PATH="${CONFIG_PATH:-/etc/juara-station.toml}"
SERVICE_USER="${SERVICE_USER:-juara2026pi4}"
HOSTNAME="${HOSTNAME:-juara2026pi4}"

set_swap_size() {
  local size_mb="$1"
  if [[ -f /etc/dphys-swapfile ]]; then
    sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=$size_mb/" /etc/dphys-swapfile
    if ! grep -q "^CONF_SWAPSIZE=" /etc/dphys-swapfile; then
      printf 'CONF_SWAPSIZE=%s\n' "$size_mb" >> /etc/dphys-swapfile
    fi
    systemctl restart dphys-swapfile.service 2>/dev/null || true
  fi
}

set_toml_key_in_section() {
  local file="$1"
  local section="$2"
  local key="$3"
  local value="$4"
  python3 - "$file" "$section" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
section = sys.argv[2]
key = sys.argv[3]
value = sys.argv[4]
lines = path.read_text().splitlines()
out = []
in_section = False
changed = False
inserted = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_section and not changed:
            out.append(f"{key} = {value}")
            changed = True
            inserted = True
        in_section = stripped == f"[{section}]"
    if in_section and stripped.startswith(f"{key}"):
        out.append(f"{key} = {value}")
        changed = True
        continue
    out.append(line)
if in_section and not changed:
    out.append(f"{key} = {value}")
    changed = True
if not changed:
    if out and out[-1].strip():
        out.append("")
    out.append(f"[{section}]")
    out.append(f"{key} = {value}")
path.write_text("\n".join(out) + "\n")
PY
}

append_once() {
  local file="$1"
  local line="$2"
  grep -qxF "$line" "$file" || printf '%s\n' "$line" >> "$file"
}

echo "=== ensure apt packages ==="
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  alsa-utils \
  dphys-swapfile \
  ffmpeg \
  i2c-tools \
  pigpio \
  python3-dev \
  python3-pigpio \
  python3-pip \
  python3-venv \
  python3-smbus \
  rpicam-apps-core \
  rclone \
  rsync

hostnamectl set-hostname "$HOSTNAME" || true
set_swap_size 1024

echo "=== rebuild venv ==="
cd "$APP_DIR"
python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR"
"$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR[pi]"
"$APP_DIR/.venv/bin/python" -m pip install birdnet-analyzer==2.4.0 pms5003 pigpio
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/src"
mkdir -p "$APP_DIR/.venv/lib/python3.11/site-packages/birdnet_analyzer/checkpoints"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv/lib/python3.11/site-packages/birdnet_analyzer/checkpoints"

echo "=== configure serial PMS5003 ==="
usermod -aG audio,video,i2c,gpio,plugdev,dialout "$SERVICE_USER" || true
raspi-config nonint do_i2c 0 || true
raspi-config nonint do_serial_hw 0 || true
raspi-config nonint do_serial_cons 1 || true
append_once /boot/firmware/config.txt "dtparam=i2c_arm=on"
append_once /boot/firmware/config.txt "dtoverlay=i2c-rtc,ds3231"
append_once /boot/firmware/config.txt "enable_uart=1"
append_once /boot/firmware/config.txt "dtoverlay=disable-bt"
if [[ -f /boot/firmware/cmdline.txt ]]; then
  python3 - <<'PY'
from pathlib import Path
path = Path("/boot/firmware/cmdline.txt")
tokens = path.read_text().strip().split()
tokens = [token for token in tokens if token not in ("console=serial0,115200", "console=ttyS0,115200", "console=ttyAMA0,115200")]
path.write_text(" ".join(tokens) + "\n")
PY
fi
systemctl disable --now serial-getty@serial0.service serial-getty@ttyS0.service serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl mask serial-getty@serial0.service serial-getty@ttyS0.service serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl enable --now pigpiod.service 2>/dev/null || systemctl enable --now pigpiod 2>/dev/null || true

echo "=== write station config ==="
install -o root -g root -m 0644 "$APP_DIR/configs/station.pi4.example.toml" "$CONFIG_PATH"
set_toml_key_in_section "$CONFIG_PATH" audio device '"plughw:CARD=Device,DEV=0"'
set_toml_key_in_section "$CONFIG_PATH" audio enabled "true"
set_toml_key_in_section "$CONFIG_PATH" birdnet enabled "true"
set_toml_key_in_section "$CONFIG_PATH" birdnet fast_tflite "true"
set_toml_key_in_section "$CONFIG_PATH" birdnet batch_max_files "1"
set_toml_key_in_section "$CONFIG_PATH" birdnet batch_size "1"
set_toml_key_in_section "$CONFIG_PATH" birdnet run_in_station_service "false"
set_toml_key_in_section "$CONFIG_PATH" speciesnet enabled "false"

echo "=== google drive uploader ==="
install -o root -g root -m 0755 "$APP_DIR/scripts/juara_gdrive_sync" /usr/local/bin/juara_gdrive_sync
install -o root -g root -m 0755 "$APP_DIR/scripts/juara_gdrive_auth_helper" /usr/local/bin/juara_gdrive_auth_helper
install -o root -g root -m 0755 "$APP_DIR/scripts/juara_uart_co2_check" /usr/local/bin/juara_uart_co2_check
cat >/etc/default/juara-gdrive-sync <<'ENV'
JUARA_LOCAL_ROOT=/var/lib/juara-station/local
JUARA_GDRIVE_REMOTE=juara-gdrive
JUARA_GDRIVE_DIR="Juara Sensor/pi4"
ENV
cat >/etc/systemd/system/juara-gdrive-sync.service <<SERVICE
[Unit]
Description=Juara Google Drive uploader
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=-/etc/default/juara-gdrive-sync
ExecStart=/usr/local/bin/juara_gdrive_sync
SERVICE
cat >/etc/systemd/system/juara-gdrive-sync.timer <<'TIMER'
[Unit]
Description=Run Juara Google Drive uploader every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true
Unit=juara-gdrive-sync.service

[Install]
WantedBy=timers.target
TIMER
touch /var/log/juara-gdrive-sync.log
chown "$SERVICE_USER:$SERVICE_USER" /var/log/juara-gdrive-sync.log
sudo -u "$SERVICE_USER" mkdir -p "/home/$SERVICE_USER/.config/rclone"
if ! sudo -u "$SERVICE_USER" rclone listremotes 2>/dev/null | grep -qx 'juara-gdrive:'; then
  cat >"/home/$SERVICE_USER/.config/rclone/rclone.conf" <<'RCLONE'
[juara-gdrive]
type = drive
scope = drive
RCLONE
  chown "$SERVICE_USER:$SERVICE_USER" "/home/$SERVICE_USER/.config/rclone/rclone.conf"
  chmod 0600 "/home/$SERVICE_USER/.config/rclone/rclone.conf"
fi

echo "=== services ==="
sed "s#__USER__#$SERVICE_USER#g; s#__APP_DIR__#$APP_DIR#g; s#__CONFIG_PATH__#$CONFIG_PATH#g" \
  "$APP_DIR/systemd/juara-station.service.in" > /etc/systemd/system/juara-station.service
sed "s#__USER__#$SERVICE_USER#g; s#__APP_DIR__#$APP_DIR#g; s#__CONFIG_PATH__#$CONFIG_PATH#g" \
  "$APP_DIR/systemd/juara-ai-worker.service.in" > /etc/systemd/system/juara-ai-worker.service
install -m 0644 "$APP_DIR/systemd/juara-daily-reboot.service" /etc/systemd/system/juara-daily-reboot.service
install -m 0644 "$APP_DIR/systemd/juara-daily-reboot.timer" /etc/systemd/system/juara-daily-reboot.timer
systemctl daemon-reload
systemctl enable juara-station.service juara-ai-worker.service juara-gdrive-sync.timer
systemctl enable --now juara-daily-reboot.timer 2>/dev/null || true
systemctl restart juara-gdrive-sync.timer

echo "=== module check ==="
"$APP_DIR/.venv/bin/python" - <<'PY'
import importlib.util
for module in ("juara_station", "birdnet_analyzer", "pms5003", "pigpio", "board", "busio"):
    print(f"{module}={bool(importlib.util.find_spec(module))}")
PY
command -v rpicam-still || command -v libcamera-still || true

echo "=== BirdNET model warmup ==="
"$APP_DIR/.venv/bin/python" - <<'PY'
from pathlib import Path
import wave

path = Path("/tmp/juara_birdnet_warmup_silence.wav")
with wave.open(str(path), "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(48000)
    wav.writeframes(b"\x00\x00" * 48000 * 3)
PY
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -m birdnet_analyzer.analyze \
  /tmp/juara_birdnet_warmup_silence.wav \
  -o /tmp/juara_birdnet_warmup_out \
  --lat -1 \
  --lon -1 \
  --week 20 \
  --rtype csv \
  --min_conf 0.1 \
  --threads 1 \
  --batch_size 1 >/tmp/juara_birdnet_warmup.log 2>&1 || cat /tmp/juara_birdnet_warmup.log

echo "Pi 4 setup finished; reboot required for dtoverlay=disable-bt to move UART to /dev/ttyAMA0."
