#!/usr/bin/env python3
"""ROV dashboard, LAPTOP BACKUP version: the same one as on the Pi, in a window, with the motors on an Arduino
Mega. Works at any size, with the mouse alone or the keyboard alone. Start it with run.sh.

It shows what rov.py is doing. No mouse click can arm the motors: that takes the controller (OPTIONS), or
holding Enter while a keyboard profile is in use. When this window closes, run.sh stops rov.py too.

  --fullscreen          full screen instead of in a window (F11 switches)
  --size 1200x720       size of the window
  --fps 60              limit the frame rate (0 = no limit, the default)
  --show-fps            show the frame rate (F12 switches)
  --margins 32,22,32,22 left, top, right, bottom: pixels the monitor hides (instead of SYSTEM > Edge margin)
  --page camera         page to start on
"""
import argparse
import importlib
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def numbers(text, separator):
    return tuple(int(n) for n in text.split(separator))


def repair():
    """After an update from the older (Tk) app: install pygame, which the dashboard needs. The app menu has no
    terminal to type a password in, so pkexec asks for it in a window. True if pygame is there."""
    install = ["pacman", "-S", "--needed", "--noconfirm", "python-pygame", "ttf-dejavu"]
    for command in (["sudo", "-n", *install], ["pkexec", *install]):
        if importlib.util.find_spec("pygame") is None:
            try:
                subprocess.run(command, check=False)
            except OSError:              # not installed
                pass
            importlib.invalidate_caches()
    return importlib.util.find_spec("pygame") is not None


def give_up(text):
    """Say why the dashboard can't open, on the desktop too: the app menu shows no terminal."""
    try:
        subprocess.run(["notify-send", "-u", "critical", "ROV Backup", text], check=False)
    except OSError:
        pass
    sys.exit(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--size", type=lambda s: numbers(s, "x"), default=None)
    parser.add_argument("--fps", type=int, default=0)
    parser.add_argument("--show-fps", action="store_true")
    parser.add_argument("--margins", type=lambda s: numbers(s, ","))
    parser.add_argument("--page", default="drive",
                        choices=("drive", "camera", "autopilot", "profiles", "motors", "system"))
    args, _ = parser.parse_known_args()
    if importlib.util.find_spec("pygame") is None:
        print("The dashboard needs pygame, installing it…", flush=True)
        if not repair():
            give_up("The dashboard needs pygame. Run the setup command from the README once.")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
    from dashboard.app import App
    App(args.fullscreen, args.size, args.margins, args.fps, args.show_fps, args.page).run()


if __name__ == "__main__":
    main()
