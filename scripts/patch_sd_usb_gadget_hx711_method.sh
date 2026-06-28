#!/usr/bin/env bash
set -euo pipefail

BOOT="${1:-/Volumes/bootfs}"
USB_PI_IP="${USB_PI_IP:-192.168.7.2}"

if [[ ! -d "$BOOT" || ! -f "$BOOT/cmdline.txt" || ! -f "$BOOT/config.txt" ]]; then
  echo "Boot partition not found at $BOOT"
  exit 1
fi

stamp="$(date +%Y%m%d%H%M%S)"
cp "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.codex-hx711usb-backup-$stamp"
cp "$BOOT/config.txt" "$BOOT/config.txt.codex-hx711usb-backup-$stamp"

grep -qxF "dtoverlay=dwc2" "$BOOT/config.txt" || printf '\ndtoverlay=dwc2\n' >> "$BOOT/config.txt"
touch "$BOOT/ssh"

cat > "$BOOT/usb0-static-setup.sh" <<EOF
#!/bin/sh

set -eu

cat >/etc/systemd/system/usb0-static.service <<'SERVICE'
[Unit]
Description=Static IP for Raspberry Pi USB gadget
After=sys-subsystem-net-devices-usb0.device
Wants=sys-subsystem-net-devices-usb0.device
Before=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for i in \$(seq 1 30); do [ -d /sys/class/net/usb0 ] && break; sleep 1; done; ip link set usb0 up || true; ip addr replace $USB_PI_IP/24 dev usb0'
ExecStop=/bin/sh -c 'ip addr del $USB_PI_IP/24 dev usb0 || true'

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable usb0-static.service
systemctl start usb0-static.service || true

touch /boot/firmware/ssh
rm -f /boot/firmware/usb0-static-setup.sh
sed -i 's| systemd\\.[^ ]*||g' /boot/firmware/cmdline.txt

exit 0
EOF
chmod +x "$BOOT/usb0-static-setup.sh"

python3 - "$BOOT/cmdline.txt" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
tokens = path.read_text().strip().split()
tokens = [
    token
    for token in tokens
    if not token.startswith("modules-load=dwc2,")
    and token != "modules-load=dwc2"
    and not token.startswith("systemd.run=")
    and not token.startswith("systemd.run_success_action=")
    and token != "systemd.unit=kernel-command-line.target"
]
for index, token in enumerate(tokens):
    if token.startswith("rootwait"):
        tokens.insert(index + 1, "modules-load=dwc2,g_ether")
        break
else:
    tokens.append("modules-load=dwc2,g_ether")
tokens.extend(
    [
        "systemd.run=/boot/firmware/usb0-static-setup.sh",
        "systemd.run_success_action=reboot",
        "systemd.unit=kernel-command-line.target",
    ]
)
path.write_text(" ".join(tokens) + "\n")
PY

sync
echo "Applied HX711-style USB gadget static IP setup for $USB_PI_IP."
