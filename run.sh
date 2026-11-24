#!/bin/bash
# Start the ROV Backup app. Options are passed on, e.g.  bash run.sh --fullscreen   (all: --help)
cd "$(dirname "$0")" && exec python3 rov_gui.py "$@"
