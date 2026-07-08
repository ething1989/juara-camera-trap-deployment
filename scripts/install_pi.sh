#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/juara-wildlife-station}"
CONFIG_PATH="${CONFIG_PATH:-/etc/juara-station.toml}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
INSTALL_AI="${INSTALL_AI:-1}"
INSTALL_BIRDNET="${INSTALL_BIRDNET:-$INSTALL_AI}"
INSTALL_SPECIESNET="${INSTALL_SPECIESNET:-0}"
INSTALL_CAMERA="${INSTALL_CAMERA:-1}"
DISABLE_BLUETOOTH_UART="${DISABLE_BLUETOOTH_UART:-0}"
CONFIG_TEMPLATE="${CONFIG_TEMPLATE:-configs/station.june2026trap.example.toml}"
APPLY_RUNTIME_OVERRIDES="${APPLY_RUNTIME_OVERRIDES:-}"
RESET_CONFIG="${RESET_CONFIG:-0}"
AUDIO_DEVICE="${AUDIO_DEVICE:-}"
TIMEZONE="${TIMEZONE:-America/Cuiaba}"
USB_LABEL="${USB_LABEL:-JUARA-CAM-1}"
USB_MOUNT="${USB_MOUNT:-/mnt/juara_usb}"
GPS_DEVICE="${GPS_DEVICE:-/dev/serial0}"
SPECIESNET_MODEL="${SPECIESNET_MODEL:-kaggle:google/speciesnet/pyTorch/v4.0.2a/1}"
SPECIESNET_MODEL_DIR="${SPECIESNET_MODEL_DIR:-/home/$SERVICE_USER/.cache/kagglehub/models/google/speciesnet/pyTorch/v4.0.2a/1}"
SPECIESNET_TARGET_SPECIES_PATH="${SPECIESNET_TARGET_SPECIES_PATH:-/etc/juara-speciesnet-target-species.txt}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo $0"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$APP_DIR/.venv/bin/python"

append_once() {
  local file="$1"
  local line="$2"
  grep -qxF "$line" "$file" || printf '\n%s\n' "$line" >> "$file"
}

set_key_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
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
header = f"[{section}]"
lines = path.read_text().splitlines()
out = []
in_section = False
seen_section = False
written = False

for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_section and not written:
            out.append(f"{key} = {value}")
            written = True
        in_section = stripped == header
        seen_section = seen_section or in_section
    if in_section and (stripped.startswith(f"{key} ") or stripped.startswith(f"{key}=")):
        if not written:
            out.append(f"{key} = {value}")
            written = True
        continue
    out.append(line)

if not seen_section:
    if out and out[-1] != "":
        out.append("")
    out.append(header)
    out.append(f"{key} = {value}")
elif in_section and not written:
    out.append(f"{key} = {value}")

path.write_text("\n".join(out) + "\n")
PY
}

install_extra() {
  local extra="$1"
  local label="$2"
  if "$VENV_PYTHON" -m pip install -e "$APP_DIR[$extra]"; then
    echo "Installed $label dependencies."
  else
    echo "WARNING: $label dependencies failed to install; the station will keep logging and retry/skip that AI work."
  fi
}

prestage_speciesnet_model() {
  if runuser -u "$SERVICE_USER" -- env SPECIESNET_MODEL="$SPECIESNET_MODEL" "$VENV_PYTHON" - <<'PY'
from pathlib import Path
import json
import os
from speciesnet.utils import ModelInfo

info = ModelInfo(os.environ["SPECIESNET_MODEL"])
base_dir = Path(info.classifier).parent
required = [
    info.classifier,
    info.classifier_labels,
    info.detector,
    info.taxonomy,
    info.geofence,
]
missing = [path for path in required if not Path(path).exists() or Path(path).stat().st_size == 0]
print(f"SpeciesNet model dir: {Path(info.classifier).parent}")
for path in required:
    print(f"{Path(path).name}: {Path(path).stat().st_size if Path(path).exists() else 0}")
if missing:
    raise SystemExit("Missing SpeciesNet files: " + ", ".join(str(path) for path in missing))
info_path = base_dir / "info.json"
payload = json.loads(info_path.read_text())
detector = payload.get("detector", "")
if detector.startswith(("http://", "https://")):
    local_detector = detector.split("?", 1)[0].replace(":", "_").replace("/", "_")
    payload["detector"] = local_detector
    info_path.write_text(json.dumps(payload, indent=4) + "\n")
    print(f"Pinned SpeciesNet detector to local file: {local_detector}")
PY
  then
    echo "Pre-staged SpeciesNet model files for $SERVICE_USER."
  else
    echo "WARNING: SpeciesNet model pre-stage failed; image AI may be disabled or try to fetch files if model_path is not set correctly."
  fi
}

make_module_writable() {
  local module="$1"
  local label="$2"
  local module_dir
  module_dir="$("$VENV_PYTHON" - "$module" <<'PY'
from pathlib import Path
import importlib.util
import sys

spec = importlib.util.find_spec(sys.argv[1])
if spec is None:
    raise SystemExit(1)
locations = spec.submodule_search_locations
if locations:
    print(locations[0])
elif spec.origin:
    print(Path(spec.origin).parent)
else:
    raise SystemExit(1)
PY
)" || {
    echo "WARNING: $label module is not importable yet; skipping writable package setup."
    return
  }
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$module_dir/checkpoints" 2>/dev/null || true
  chown -R "$SERVICE_USER:$SERVICE_USER" "$module_dir" 2>/dev/null || true
}

patch_birdnet_tflite_checker() {
  if "$VENV_PYTHON" - <<'PY'
from pathlib import Path
import birdnet_analyzer

utils_path = Path(birdnet_analyzer.__file__).parent / "utils.py"
text = utils_path.read_text()
start = text.index("def check_birdnet_files():")
end = text.index("\ndef ensure_model_exists", start)
replacement = '''def check_birdnet_files():
    checkpoint_dir = os.path.join(SCRIPT_DIR, "checkpoints", "V2.4")
    required_files = [
        "BirdNET_GLOBAL_6K_V2.4_Labels.txt",
        "BirdNET_GLOBAL_6K_V2.4_MData_Model_V2_FP16.tflite",
        "BirdNET_GLOBAL_6K_V2.4_Model_FP32.tflite",
    ]
    return all(os.path.exists(os.path.join(checkpoint_dir, file)) for file in required_files)
'''
if text[start:end] != replacement:
    backup = utils_path.with_suffix(".py.juara-backup")
    if not backup.exists():
        backup.write_text(text)
    utils_path.write_text(text[:start] + replacement + text[end:])
print(utils_path)
PY
  then
    echo "Patched BirdNET to accept a pre-staged TFLite model bundle."
  else
    echo "WARNING: BirdNET TFLite checker patch failed; analyzer may try to download the full model archive."
  fi
}

echo "Installing Juara station on $(uname -m) with $(python3 --version 2>&1)"

apt-get update
apt_packages=(
  alsa-utils
  ffmpeg
  gpsd
  gpsd-clients
  i2c-tools
  pigpio
  python3-dev
  python3-pigpio
  python3-pip
  python3-venv
  python3-smbus
  rsync
  util-linux-extra
)
if [[ "$INSTALL_CAMERA" == "1" ]]; then
  apt_packages+=(python3-picamera2 rpicam-apps)
fi
apt-get install -y "${apt_packages[@]}"

if command -v timedatectl >/dev/null 2>&1; then
  timedatectl set-timezone "$TIMEZONE" || true
fi

# Field stations usually run offline from solar power. Disable Debian's
# unattended apt timers so they do not burn CPU/network looking for updates.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service 2>/dev/null || true

if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_i2c 0 || true
fi

append_once /boot/firmware/config.txt "dtparam=i2c_arm=on"
append_once /boot/firmware/config.txt "dtoverlay=i2c-rtc,ds3231"
append_once /boot/firmware/config.txt "enable_uart=1"
if [[ "$CONFIG_TEMPLATE" == *station.june2026trap.example.toml* ]]; then
  append_once /boot/firmware/config.txt "dtoverlay=imx219"
fi
if [[ "$DISABLE_BLUETOOTH_UART" == "1" ]]; then
  append_once /boot/firmware/config.txt "dtoverlay=disable-bt"
fi
if [[ -f /boot/firmware/cmdline.txt ]]; then
  python3 - <<'PY'
from pathlib import Path

path = Path("/boot/firmware/cmdline.txt")
tokens = path.read_text().strip().split()
tokens = [token for token in tokens if token not in ("console=serial0,115200", "console=ttyS0,115200")]
path.write_text(" ".join(tokens) + "\n")
PY
fi
systemctl disable --now serial-getty@serial0.service serial-getty@ttyS0.service 2>/dev/null || true
systemctl mask serial-getty@serial0.service serial-getty@ttyS0.service 2>/dev/null || true
systemctl enable --now pigpiod.service 2>/dev/null || systemctl enable --now pigpiod 2>/dev/null || true
usermod -aG dialout gpsd 2>/dev/null || true

if [[ -f /etc/default/gpsd ]]; then
  set_key_value /etc/default/gpsd DEVICES "\"$GPS_DEVICE\""
  set_key_value /etc/default/gpsd GPSD_OPTIONS "\"-n\""
  set_key_value /etc/default/gpsd USBAUTO "\"true\""
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR"
rsync -a --delete \
  --exclude ".DS_Store" \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude ".local-tests" \
  --exclude ".local_run" \
  --exclude ".pytest_cache" \
  --exclude "__pycache__" \
  --exclude "*.egg-info" \
  --exclude "data/bird_playback_test" \
  "$REPO_DIR/" "$APP_DIR/"

python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e "$APP_DIR"
install_extra pi "Pi hardware"

if [[ "$INSTALL_BIRDNET" == "1" ]]; then
  install_extra birdnet "BirdNET audio AI"
  make_module_writable birdnet_analyzer "BirdNET audio AI"
  patch_birdnet_tflite_checker
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/juara-station/state
  if [[ "$CONFIG_TEMPLATE" == *station.june2026trap.example.toml* && -f "$APP_DIR/data/birdnet/june2026trap_active_species_list.txt" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 \
      "$APP_DIR/data/birdnet/june2026trap_active_species_list.txt" \
      /var/lib/juara-station/state/juara-birdnet-species-list.txt
  elif [[ -f "$APP_DIR/data/birdnet/juara_region_species_list.txt" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 \
      "$APP_DIR/data/birdnet/juara_region_species_list.txt" \
      /var/lib/juara-station/state/juara-birdnet-species-list.txt
  fi
fi

if [[ "$INSTALL_SPECIESNET" == "1" ]]; then
  install_extra speciesnet "SpeciesNet image AI"
  if [[ -f "$APP_DIR/data/speciesnet/juara_region_target_species.txt" ]]; then
    install -m 0644 "$APP_DIR/data/speciesnet/juara_region_target_species.txt" "$SPECIESNET_TARGET_SPECIES_PATH"
  fi
  prestage_speciesnet_model
fi

if [[ -z "$APPLY_RUNTIME_OVERRIDES" ]]; then
  case "$CONFIG_TEMPLATE" in
    *station.june2026trap.example.toml*) APPLY_RUNTIME_OVERRIDES="0" ;;
    *) APPLY_RUNTIME_OVERRIDES="1" ;;
  esac
fi

if [[ "$RESET_CONFIG" == "1" || ! -f "$CONFIG_PATH" ]]; then
  install -m 0644 "$APP_DIR/$CONFIG_TEMPLATE" "$CONFIG_PATH"
fi
if [[ "$APPLY_RUNTIME_OVERRIDES" == "1" ]]; then
  set_toml_key_in_section "$CONFIG_PATH" schedule image_ai_defer_enabled "false"
  set_toml_key_in_section "$CONFIG_PATH" schedule audio_recording_disabled_start_hour "1"
  set_toml_key_in_section "$CONFIG_PATH" schedule audio_recording_disabled_end_hour "3"
  set_toml_key_in_section "$CONFIG_PATH" schedule audio_backlog_purge_hour "3"
  set_toml_key_in_section "$CONFIG_PATH" schedule post_audio_reboot_hour "3"
  set_toml_key_in_section "$CONFIG_PATH" schedule photo_capture_disabled_start_hour "19"
  set_toml_key_in_section "$CONFIG_PATH" schedule photo_capture_disabled_end_hour "6"
  set_toml_key_in_section "$CONFIG_PATH" schedule photo_processing_deadline_hour "6"
  set_toml_key_in_section "$CONFIG_PATH" camera warm_restart_interval_seconds "300"
fi
if [[ -n "$AUDIO_DEVICE" ]]; then
  set_toml_key_in_section "$CONFIG_PATH" audio device "\"$AUDIO_DEVICE\""
fi
if [[ "$INSTALL_SPECIESNET" == "1" ]]; then
  set_toml_key_in_section "$CONFIG_PATH" speciesnet enabled "true"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet model_path "\"$SPECIESNET_MODEL_DIR\""
  set_toml_key_in_section "$CONFIG_PATH" speciesnet target_species_txt "\"$SPECIESNET_TARGET_SPECIES_PATH\""
  set_toml_key_in_section "$CONFIG_PATH" speciesnet run_in_station_service "false"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet classifier_only "true"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet direct_classifier "true"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet isolated_process "true"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet subprocess_timeout_seconds "240"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet subprocess_threads "1"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet subprocess_nice "15"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet subprocess_memory_limit_mb "384"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet keep_classifier_loaded "false"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet batch_size "1"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet input_size "224"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet fast_input_size "0"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet fast_accept_min_confidence "0.90"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet blank_precheck_enabled "true"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet blank_precheck_skip_classifier "true"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet min_classifier_available_memory_mb "650"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet max_photos_per_run "1"
else
  set_toml_key_in_section "$CONFIG_PATH" speciesnet enabled "false"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet run_in_station_service "false"
  set_toml_key_in_section "$CONFIG_PATH" speciesnet delete_blanks "false"
fi
if [[ "$INSTALL_BIRDNET" == "1" ]]; then
  set_toml_key_in_section "$CONFIG_PATH" birdnet run_in_station_service "false"
  set_toml_key_in_section "$CONFIG_PATH" birdnet batch_max_files "1"
  set_toml_key_in_section "$CONFIG_PATH" birdnet batch_size "1"
fi
if [[ "$INSTALL_BIRDNET" == "1" && "$CONFIG_TEMPLATE" == *station.june2026trap.example.toml* && -d "$APP_DIR/data/BirdNET_Global_Species_Packs/cells" ]]; then
  "$VENV_PYTHON" -m juara_station.cli --config "$CONFIG_PATH" select-species || {
    echo "WARNING: Dynamic BirdNET species-pack selection failed; keeping existing active species list."
  }
fi

usermod -aG audio,video,i2c,gpio,plugdev,dialout "$SERVICE_USER" || true
sudoers_file="/etc/sudoers.d/juara-station-hwclock"
printf '%s ALL=(root) NOPASSWD: /usr/sbin/hwclock *\n' "$SERVICE_USER" > "$sudoers_file"
chmod 0440 "$sudoers_file"
visudo -cf "$sudoers_file" >/dev/null
reboot_sudoers_file="/etc/sudoers.d/juara-station-reboot"
printf '%s ALL=(root) NOPASSWD: /usr/sbin/reboot\n' "$SERVICE_USER" > "$reboot_sudoers_file"
chmod 0440 "$reboot_sudoers_file"
visudo -cf "$reboot_sudoers_file" >/dev/null
systemctl_sudoers_file="/etc/sudoers.d/juara-station-systemctl"
{
  printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl stop juara-ai-worker.service\n' "$SERVICE_USER"
  printf '%s ALL=(root) NOPASSWD: /bin/systemctl stop juara-ai-worker.service\n' "$SERVICE_USER"
} > "$systemctl_sudoers_file"
chmod 0440 "$systemctl_sudoers_file"
visudo -cf "$systemctl_sudoers_file" >/dev/null
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$USB_MOUNT" /var/lib/juara-station

usb_device="$(blkid -L "$USB_LABEL" 2>/dev/null || true)"
if [[ -z "$usb_device" ]]; then
  usb_device="$(lsblk -rpno NAME,TYPE,FSTYPE | awk '$1 ~ "^/dev/sd" && $2 == "part" && $3 != "" { print $1; exit }')"
fi
if [[ -n "$usb_device" ]]; then
  usb_uuid="$(blkid -s UUID -o value "$usb_device")"
  usb_fstype="$(blkid -s TYPE -o value "$usb_device")"
  if [[ -n "$usb_uuid" && -n "$usb_fstype" ]]; then
    user_uid="$(id -u "$SERVICE_USER")"
    user_gid="$(id -g "$SERVICE_USER")"
    tmp_fstab="$(mktemp)"
    awk -v mountpoint="$USB_MOUNT" '$2 != mountpoint { print }' /etc/fstab > "$tmp_fstab"
    cat "$tmp_fstab" > /etc/fstab
    rm -f "$tmp_fstab"
    if [[ "$usb_fstype" == "vfat" || "$usb_fstype" == "exfat" ]]; then
      mount_options="defaults,nofail,x-systemd.automount,uid=$user_uid,gid=$user_gid,umask=0022"
    else
      mount_options="defaults,nofail,x-systemd.automount"
    fi
    printf 'UUID=%s %s %s %s 0 0\n' "$usb_uuid" "$USB_MOUNT" "$usb_fstype" "$mount_options" >> /etc/fstab
    systemctl daemon-reload
    mount "$USB_MOUNT" || true
  else
    echo "WARNING: USB partition $usb_device has no UUID or filesystem type; station will use fallback storage until USB is mounted."
  fi
else
  user_uid="$(id -u "$SERVICE_USER")"
  user_gid="$(id -g "$SERVICE_USER")"
  tmp_fstab="$(mktemp)"
  awk -v mountpoint="$USB_MOUNT" '$2 != mountpoint { print }' /etc/fstab > "$tmp_fstab"
  cat "$tmp_fstab" > /etc/fstab
  rm -f "$tmp_fstab"
  printf 'LABEL=%s %s auto defaults,nofail,x-systemd.automount,x-systemd.device-timeout=10s,uid=%s,gid=%s,umask=0022 0 0\n' \
    "$USB_LABEL" "$USB_MOUNT" "$user_uid" "$user_gid" >> /etc/fstab
  systemctl daemon-reload
  echo "WARNING: USB drive label $USB_LABEL not found and no USB partition was detected; installed a label-based automount entry and station will use fallback storage until USB is mounted."
fi

mkdir -p \
  /var/lib/juara-station/local \
  /var/lib/juara-station/state \
  /var/lib/juara-station/audio_recordings \
  /tmp/juara-ai-work \
  /tmp/juara-audio
if [[ "$CONFIG_TEMPLATE" == *station.june2026trap.example.toml* ]]; then
  mkdir -p "$USB_MOUNT/Photos"
  rm -rf "$USB_MOUNT/audio" "$USB_MOUNT/media/audio"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$USB_MOUNT/Photos" /var/lib/juara-station /tmp/juara-ai-work /tmp/juara-audio 2>/dev/null || true
else
  mkdir -p \
    "$USB_MOUNT/juara/photos" \
    "$USB_MOUNT/juara/logs" \
    "$USB_MOUNT/juara/media/photos"
  rm -rf "$USB_MOUNT/juara/audio" "$USB_MOUNT/juara/media/audio"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$USB_MOUNT/juara" /var/lib/juara-station /tmp/juara-ai-work /tmp/juara-audio 2>/dev/null || true
fi

cat > /usr/local/bin/juara-planned-reboot <<EOF
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$APP_DIR"
CONFIG_PATH="$CONFIG_PATH"
VENV_PYTHON="\$APP_DIR/.venv/bin/python"
TIMEOUT_SECONDS="\${JUARA_PLANNED_REBOOT_TIMEOUT_SECONDS:-180}"
export APP_DIR CONFIG_PATH VENV_PYTHON

cleanup() {
  systemctl stop juara-station.service juara-ai-worker.service 2>/dev/null || true
  "\$VENV_PYTHON" -m juara_station.cli --config "\$CONFIG_PATH" planned-reboot-cleanup || true
}

if ! timeout --kill-after=15s "\${TIMEOUT_SECONDS}s" bash -c "\$(declare -f cleanup); cleanup"; then
  echo "WARNING: Juara planned reboot cleanup timed out after \${TIMEOUT_SECONDS}s; rebooting anyway." >&2
fi

systemctl reboot
EOF
chmod 0755 /usr/local/bin/juara-planned-reboot

if [[ -f "$APP_DIR/scripts/juara_motion_trips.py" ]]; then
  install -m 0755 "$APP_DIR/scripts/juara_motion_trips.py" /usr/local/bin/juara-motion-trips
fi
install -m 0755 "$APP_DIR/scripts/juara_wifi_reconnect" /usr/local/bin/juara_wifi_reconnect
install -m 0755 "$APP_DIR/scripts/juara_networkpi_maintenance" /usr/local/bin/juara_networkpi_maintenance
install -m 0755 "$APP_DIR/scripts/juara_git_update" /usr/local/bin/juara_git_update
install -m 0755 "$APP_DIR/scripts/juara_gdrive_sync" /usr/local/bin/juara_gdrive_sync
install -m 0755 "$APP_DIR/scripts/juara_health_report" /usr/local/bin/juara_health_report

sed "s#__USER__#$SERVICE_USER#g; s#__APP_DIR__#$APP_DIR#g; s#__CONFIG_PATH__#$CONFIG_PATH#g" \
  "$APP_DIR/systemd/juara-station.service.in" > /etc/systemd/system/juara-station.service
sed "s#__USER__#$SERVICE_USER#g; s#__APP_DIR__#$APP_DIR#g; s#__CONFIG_PATH__#$CONFIG_PATH#g" \
  "$APP_DIR/systemd/juara-ai-worker.service.in" > /etc/systemd/system/juara-ai-worker.service
install -m 0644 "$APP_DIR/systemd/juara-daily-reboot.service" /etc/systemd/system/juara-daily-reboot.service
install -m 0644 "$APP_DIR/systemd/juara-daily-reboot.timer" /etc/systemd/system/juara-daily-reboot.timer
install -m 0644 "$APP_DIR/systemd/juara-wifi-reconnect.service" /etc/systemd/system/juara-wifi-reconnect.service
install -m 0644 "$APP_DIR/systemd/juara-wifi-reconnect.timer" /etc/systemd/system/juara-wifi-reconnect.timer
install -m 0644 "$APP_DIR/systemd/juara-networkpi-maintenance.service" /etc/systemd/system/juara-networkpi-maintenance.service
install -m 0644 "$APP_DIR/systemd/juara-networkpi-maintenance.timer" /etc/systemd/system/juara-networkpi-maintenance.timer
install -m 0644 "$APP_DIR/systemd/juara-git-update.service" /etc/systemd/system/juara-git-update.service
install -m 0644 "$APP_DIR/systemd/juara-git-update.timer" /etc/systemd/system/juara-git-update.timer
install -m 0644 "$APP_DIR/systemd/juara-health-report.service" /etc/systemd/system/juara-health-report.service
install -m 0644 "$APP_DIR/systemd/juara-health-report.timer" /etc/systemd/system/juara-health-report.timer

systemctl daemon-reload
systemctl enable gpsd.socket gpsd.service || true
systemctl restart gpsd.socket gpsd.service || true
systemctl enable juara-station.service
systemctl enable juara-ai-worker.service
systemctl enable --now juara-daily-reboot.timer
systemctl enable --now juara-wifi-reconnect.timer
systemctl enable --now juara-networkpi-maintenance.timer
systemctl enable --now juara-git-update.timer
systemctl enable --now juara-health-report.timer
umount "$USB_MOUNT" 2>/dev/null || true

"$VENV_PYTHON" - <<'PY' || true
import importlib.util
for module in ("juara_station", "birdnet_analyzer", "speciesnet", "picamera2", "board", "busio", "adafruit_scd4x", "pms5003", "pigpio"):
    print(f"{module}={bool(importlib.util.find_spec(module))}")
PY

echo "Installed Juara station for user $SERVICE_USER"
echo "Config: $CONFIG_PATH"
echo "USB mount: $USB_MOUNT"
echo "Start service: sudo systemctl start juara-station"
echo "Start AI worker: sudo systemctl start juara-ai-worker"
echo "Watch logs: sudo journalctl -u juara-station -f"
