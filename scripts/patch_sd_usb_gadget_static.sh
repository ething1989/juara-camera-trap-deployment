#!/usr/bin/env bash
set -euo pipefail

BOOT="${1:-/Volumes/bootfs}"
PI_USER="${PI_USER:-june2026trap}"
PI_PASSWORD="${PI_PASSWORD:-raspberry}"
PI_HOSTNAME="${PI_HOSTNAME:-june2026trap}"
USB_PI_IP="${USB_PI_IP:-192.168.7.2}"
USB_MAC_IP="${USB_MAC_IP:-192.168.7.1}"
WIFI_SSID="${WIFI_SSID:-NetworkPi}"
WIFI_PASSWORD="${WIFI_PASSWORD:-raspberry314}"
WIFI_COUNTRY="${WIFI_COUNTRY:-US}"

if [[ ! -d "$BOOT" ]]; then
  echo "Boot partition not found at $BOOT"
  echo "Usage: $0 /Volumes/bootfs"
  exit 1
fi

for required in config.txt cmdline.txt; do
  if [[ ! -f "$BOOT/$required" ]]; then
    echo "Missing $BOOT/$required"
    exit 1
  fi
done

stamp="$(date +%Y%m%d%H%M%S)"
cp "$BOOT/config.txt" "$BOOT/config.txt.codex-usb-backup-$stamp"
cp "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.codex-usb-backup-$stamp"

append_once() {
  local file="$1"
  local line="$2"
  grep -qxF "$line" "$file" || printf '\n%s\n' "$line" >> "$file"
}

append_once "$BOOT/config.txt" "dtoverlay=dwc2"
append_once "$BOOT/config.txt" "dtparam=i2c_arm=on"
append_once "$BOOT/config.txt" "dtoverlay=i2c-rtc,ds3231"
append_once "$BOOT/config.txt" "enable_uart=1"

python3 - "$BOOT/cmdline.txt" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
tokens = path.read_text().strip().split()
remove_prefixes = (
    "systemd.run=",
    "systemd.run_success_action=",
    "systemd.unit=kernel-command-line.target",
)
tokens = [token for token in tokens if not any(token.startswith(prefix) for prefix in remove_prefixes)]
if "modules-load=dwc2,g_ether" not in tokens:
    for index, token in enumerate(tokens):
        if token.startswith("rootwait"):
            tokens.insert(index + 1, "modules-load=dwc2,g_ether")
            break
    else:
        tokens.append("modules-load=dwc2,g_ether")
tokens.extend(
    [
        "systemd.run=/boot/firmware/firstrun.sh",
        "systemd.run_success_action=reboot",
        "systemd.unit=kernel-command-line.target",
    ]
)
path.write_text(" ".join(tokens) + "\n")
PY

touch "$BOOT/ssh"

cat > "$BOOT/wpa_supplicant.conf" <<EOF
country=$WIFI_COUNTRY
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PASSWORD"
    key_mgmt=WPA-PSK
}
EOF

cat > "$BOOT/firstrun.sh" <<EOF
#!/bin/bash
set +e

LOG=/boot/firmware/juara-firstrun.log
exec > >(tee -a "\$LOG") 2>&1
echo "Juara firstrun started: \$(date -Is)"

mount -o remount,rw /boot/firmware 2>/dev/null || true

if [ -f /boot/firmware/cmdline.txt ]; then
  python3 - <<'PY'
from pathlib import Path
path = Path("/boot/firmware/cmdline.txt")
tokens = path.read_text().strip().split()
remove_prefixes = (
    "systemd.run=",
    "systemd.run_success_action=",
    "systemd.unit=kernel-command-line.target",
)
tokens = [token for token in tokens if not any(token.startswith(prefix) for prefix in remove_prefixes)]
path.write_text(" ".join(tokens) + "\\n")
PY
fi

grep -qxF "dtoverlay=dwc2" /boot/firmware/config.txt || printf "\\ndtoverlay=dwc2\\n" >> /boot/firmware/config.txt
grep -qxF "dtparam=i2c_arm=on" /boot/firmware/config.txt || printf "\\ndtparam=i2c_arm=on\\n" >> /boot/firmware/config.txt
grep -qxF "dtoverlay=i2c-rtc,ds3231" /boot/firmware/config.txt || printf "\\ndtoverlay=i2c-rtc,ds3231\\n" >> /boot/firmware/config.txt
grep -qxF "enable_uart=1" /boot/firmware/config.txt || printf "\\nenable_uart=1\\n" >> /boot/firmware/config.txt

hostnamectl set-hostname "$PI_HOSTNAME" || true
echo "$PI_HOSTNAME" > /etc/hostname
grep -q "127.0.1.1" /etc/hosts && sed -i "s/^127\\.0\\.1\\.1.*/127.0.1.1\\t$PI_HOSTNAME/" /etc/hosts || echo "127.0.1.1	$PI_HOSTNAME" >> /etc/hosts

if ! id "$PI_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$PI_USER"
fi
echo "$PI_USER:$PI_PASSWORD" | chpasswd
usermod -aG sudo,adm,dialout,audio,video,gpio,i2c,spi,plugdev,netdev "$PI_USER" || true

raspi-config nonint do_ssh 0 || true
systemctl enable ssh || systemctl enable ssh.service || true
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-juara-password-login.conf <<'SSHCONF'
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PermitRootLogin no
SSHCONF

mkdir -p /etc/NetworkManager/system-connections
cat > /etc/NetworkManager/system-connections/usb0-static.nmconnection <<'NMUSB'
[connection]
id=usb0-static
type=ethernet
interface-name=usb0
autoconnect=true

[ipv4]
method=manual
address1=192.168.7.2/24

[ipv6]
method=link-local
NMUSB
chmod 600 /etc/NetworkManager/system-connections/usb0-static.nmconnection

cat > /etc/NetworkManager/system-connections/NetworkPi.nmconnection <<'NMWIFI'
[connection]
id=NetworkPi
type=wifi
interface-name=wlan0
autoconnect=true

[wifi]
mode=infrastructure
ssid=NetworkPi

[wifi-security]
key-mgmt=wpa-psk
psk=raspberry314

[ipv4]
method=auto

[ipv6]
method=auto
NMWIFI
chmod 600 /etc/NetworkManager/system-connections/NetworkPi.nmconnection

if [ -f /etc/dhcpcd.conf ] && ! grep -q "Juara USB gadget static IP" /etc/dhcpcd.conf; then
  cat >> /etc/dhcpcd.conf <<'DHCPCD'

# Juara USB gadget static IP
interface usb0
static ip_address=192.168.7.2/24
DHCPCD
fi

mkdir -p /etc/systemd/network
cat > /etc/systemd/network/10-juara-usb0.network <<'NETWORKD'
[Match]
Name=usb0

[Network]
Address=192.168.7.2/24
LinkLocalAddressing=ipv6
NETWORKD

rfkill unblock wifi || true
systemctl restart NetworkManager 2>/dev/null || true
nmcli connection reload 2>/dev/null || true
nmcli connection up usb0-static 2>/dev/null || true
systemctl restart ssh 2>/dev/null || true

ip link set usb0 up 2>/dev/null || true
ip addr add 192.168.7.2/24 dev usb0 2>/dev/null || true

sync
echo "Juara firstrun finished: \$(date -Is)"
rm -f /boot/firmware/firstrun.sh
exit 0
EOF

chmod +x "$BOOT/firstrun.sh"
sync

echo "Patched $BOOT for USB gadget SSH."
echo "Mac side should be $USB_MAC_IP/24; Pi side will be $USB_PI_IP."
echo "After boot, connect with: ssh $PI_USER@$USB_PI_IP"
