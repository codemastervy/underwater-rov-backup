#!/bin/bash
# ROV BACKUP - set up an EndeavourOS / Arch laptop.   Run:  bash install.sh
set -euo pipefail
if [ "$EUID" -eq 0 ]; then echo "Run as your normal user (not sudo) - it asks for your password when needed."; exit 1; fi
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing packages"
sudo pacman -S --needed --noconfirm python python-evdev python-pyserial python-numpy tk ffmpeg bluez bluez-utils arduino-cli

echo "==> Giving $USER access to game controllers (input) and the Arduino (uucp)"
sudo usermod -aG input,uucp "$USER"

echo "==> Turning on Bluetooth"
sudo systemctl enable --now bluetooth

chmod +x "$DIR/run.sh" "$DIR/upload-mega.sh"

echo "==> Adding 'ROV Backup' to the app menu"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/rov-backup.desktop <<EOF
[Desktop Entry]
Type=Application
Name=ROV Backup
Comment=Drive the ROV from this laptop through the Arduino Mega
Exec=$DIR/run.sh
Icon=input-gaming
Terminal=false
Categories=Utility;
EOF

[ -n "${ROV_FROM_SETUP:-}" ] && exit 0   # setup.sh prints its own summary
cat <<EOF

Done. LOG OUT AND BACK IN (or reboot) so the new permissions apply. Then:
  1. Upload the firmware once:   bash $DIR/upload-mega.sh      (Mega plugged in by USB)
  2. Pair the PS5 controller in the laptop's Bluetooth settings (hold CREATE + PS),
     or just plug it in with a USB cable.
  3. Start it: "ROV Backup" in the app menu, or   bash $DIR/run.sh
EOF
