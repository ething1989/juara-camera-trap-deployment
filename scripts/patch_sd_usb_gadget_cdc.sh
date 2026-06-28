#!/usr/bin/env bash
set -euo pipefail

BOOT="${1:-/Volumes/bootfs}"

if [[ ! -d "$BOOT" || ! -f "$BOOT/cmdline.txt" || ! -f "$BOOT/config.txt" ]]; then
  echo "Boot partition not found at $BOOT"
  exit 1
fi

stamp="$(date +%Y%m%d%H%M%S)"
cp "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.codex-cdc-backup-$stamp"
cp "$BOOT/config.txt" "$BOOT/config.txt.codex-cdc-backup-$stamp"

grep -qxF "dtoverlay=dwc2" "$BOOT/config.txt" || printf '\ndtoverlay=dwc2\n' >> "$BOOT/config.txt"

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
]
for index, token in enumerate(tokens):
    if token.startswith("rootwait"):
        tokens.insert(index + 1, "modules-load=dwc2,g_cdc")
        break
else:
    tokens.append("modules-load=dwc2,g_cdc")
path.write_text(" ".join(tokens) + "\n")
PY

sync
echo "Switched USB gadget module to g_cdc for macOS CDC Ethernet."
