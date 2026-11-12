#!/bin/bash
# Start the ROV backup: the motor program (rov.py) + the dashboard window.
# Closing the dashboard stops the motor program; the Mega then switches all motors off within 0.3 s.
# Options are passed to the dashboard, e.g.  bash run.sh --fullscreen   or   bash run.sh --size 1600x960
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
python3 rov.py --config "$DIR/config.toml" &
ROV=$!
trap 'kill $ROV 2>/dev/null; wait $ROV 2>/dev/null' EXIT INT TERM
sleep 1
python3 rov_gui.py "$@"
