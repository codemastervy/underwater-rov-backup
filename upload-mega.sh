#!/bin/bash
# Compile and upload rov_mega/rov_mega.ino to an Arduino Mega 2560 on USB (only needed once).
# Close the ROV Backup app first - the upload needs the serial port.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1)}"
if [ -z "$PORT" ]; then echo "No Arduino found on USB. Plug in the Mega and try again."; exit 1; fi
echo "==> Using $PORT"
arduino-cli core update-index
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:mega "$DIR/rov_mega"
arduino-cli upload --fqbn arduino:avr:mega -p "$PORT" "$DIR/rov_mega"
sha256sum "$DIR/rov_mega/rov_mega.ino" | cut -d' ' -f1 > "$DIR/.mega-firmware"   # the app checks this
echo "Uploaded. (Arduino IDE works too: open rov_mega/rov_mega.ino, board 'Arduino Mega or Mega 2560', Upload.)"
