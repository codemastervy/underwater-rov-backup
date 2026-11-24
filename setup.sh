#!/bin/bash
# ROV BACKUP (EndeavourOS / Arch laptop + Arduino Mega) - one command to INSTALL or UPDATE.
# Needs only an internet connection. Run it in a terminal as your normal user:
#
#   curl -fsSL https://raw.githubusercontent.com/codemastervy/underwater-rov-backup/main/setup.sh | bash
#
# - Downloads the latest version from GitHub into ~/underwater-rov-backup (no git needed).
# - Installs everything it needs (Python libraries, Bluetooth, ffmpeg, arduino-cli) and the app-menu entry.
# - Updating keeps your settings: config.toml (motor flips), profiles, camera, autopilot, arm tune.
# - After this, the app updates itself: UPDATE NOW on the SYSTEM page when it says UPDATE READY.
# - If the Arduino Mega is plugged in, it uploads the firmware (only when it's new or changed).
set -euo pipefail

REPO="codemastervy/underwater-rov-backup"
BRANCH="${ROV_BRANCH:-main}"
DIR="$HOME/underwater-rov-backup"
KEEP=(config.toml profiles.json camera.json autopilot.json mission.json tune.txt keymap.json)

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (not with sudo). It asks for your password when it needs it."
  exit 1
fi
command -v pacman >/dev/null || { echo "This is for EndeavourOS / Arch Linux (pacman not found)."; exit 1; }

if [ -f "$DIR/rov.py" ]; then MODE=update; else MODE=install; fi
echo "==> ROV backup setup: $MODE"

if pgrep -f "python3 rov.py --config" >/dev/null || pgrep -f "python3 rov_gui.py" >/dev/null; then
  echo "Close the ROV Backup app first, then run this again."
  exit 1
fi

echo "==> Downloading the latest version from GitHub"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SHA="$(curl -fsSL -H "Accept: application/vnd.github.sha" "https://api.github.com/repos/$REPO/commits/$BRANCH" || true)"
if [ ${#SHA} -ne 40 ]; then SHA="$BRANCH"; fi
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$SHA" | tar -xz -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
if [ "$SHA" != "$BRANCH" ]; then echo "$SHA" > "$SRC/VERSION"; fi   # lets the app spot newer versions
mkdir -p "$DIR"
for f in "${KEEP[@]}"; do                     # your settings win over the downloaded defaults
  if [ -f "$DIR/$f" ]; then rm -f "$SRC/$f"; fi
done
cp -a "$SRC/." "$DIR/"
chmod +x "$DIR"/*.sh

echo "==> Installing packages and permissions (asks for your password)"
ROV_FROM_SETUP=1 bash "$DIR/install.sh"

# Firmware: upload when a Mega is plugged in and it isn't running the current ROV firmware
# (asks the board itself: an Arduino without the ROV firmware doesn't answer PING)
cat > "$TMP/mega-ping.py" <<'PY'
import sys, time, serial
try:
    s = serial.Serial(sys.argv[1], 115200, timeout=0.2)
except Exception:
    sys.exit(1)
time.sleep(2.5)                       # the Mega restarts when the port opens
s.reset_input_buffer()
for _ in range(3):
    s.write(b"PING\n")
    t = time.time()
    while time.time() - t < 0.6:
        if b"PONG" in s.readline():
            sys.exit(0)
sys.exit(1)
PY
PORT="$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1 || true)"
SUM="$(sha256sum "$DIR/rov_mega/rov_mega.ino" | cut -d' ' -f1)"
if [ -n "$PORT" ]; then
  if [ "$(cat "$DIR/.mega-firmware" 2>/dev/null)" = "$SUM" ] && sg uucp -c "python3 '$TMP/mega-ping.py' '$PORT'" 2>/dev/null; then
    echo "==> Arduino Mega on $PORT already has the current ROV firmware"
  else
    echo "==> Uploading the ROV firmware to the Arduino Mega on $PORT (about 30 s)"
    # 'sg uucp' gives serial-port access now, before you've logged out and in
    if sg uucp -c "bash '$DIR/upload-mega.sh' '$PORT'"; then
      echo "$SUM" > "$DIR/.mega-firmware"
    else
      echo "!! Firmware upload failed. Try again with:  bash $DIR/upload-mega.sh"
    fi
  fi
else
  echo "==> No Arduino Mega plugged in: plug it in and run this command again to upload the firmware"
  echo "    (or: bash $DIR/upload-mega.sh)"
fi

echo
if [ "$MODE" = install ]; then
  echo "Installed. LOG OUT AND BACK IN (or reboot) once so the controller/USB permissions apply."
  echo "Then open 'ROV Backup' from the app menu (or: bash $DIR/run.sh)."
else
  echo "Updated to the latest version. Open 'ROV Backup' from the app menu."
fi
