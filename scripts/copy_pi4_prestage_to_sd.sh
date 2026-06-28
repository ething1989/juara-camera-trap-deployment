#!/usr/bin/env bash
set -euo pipefail

STAGE_DIR="${STAGE_DIR:-/tmp/juara_pi4_sd_prestage}"
BOOT_VOLUME="${BOOT_VOLUME:-}"

if [[ -z "$BOOT_VOLUME" ]]; then
  for candidate in /Volumes/bootfs /Volumes/boot /Volumes/BOOT /Volumes/RASPIFIRM; do
    if [[ -f "$candidate/config.txt" || -f "$candidate/cmdline.txt" ]]; then
      BOOT_VOLUME="$candidate"
      break
    fi
  done
fi

if [[ -z "$BOOT_VOLUME" || ! -d "$BOOT_VOLUME" ]]; then
  echo "ERROR: could not find the Raspberry Pi boot partition. Set BOOT_VOLUME=/Volumes/<name>."
  exit 1
fi

for file in juara_pi4_code_update_clean.tgz pi4_bootstrap_from_boot.sh README_JUARA_PI4_BOOT.txt; do
  if [[ ! -f "$STAGE_DIR/$file" ]]; then
    echo "ERROR: missing $STAGE_DIR/$file"
    exit 1
  fi
done

cp "$STAGE_DIR/juara_pi4_code_update_clean.tgz" "$BOOT_VOLUME/juara_pi4_code_update_clean.tgz"
cp "$STAGE_DIR/pi4_bootstrap_from_boot.sh" "$BOOT_VOLUME/pi4_bootstrap_from_boot.sh"
cp "$STAGE_DIR/README_JUARA_PI4_BOOT.txt" "$BOOT_VOLUME/README_JUARA_PI4_BOOT.txt"
sync

echo "Copied Juara Pi 4 staging kit to $BOOT_VOLUME"
echo "After the Pi boots and SSH works, run:"
echo "  sudo bash /boot/firmware/pi4_bootstrap_from_boot.sh"
