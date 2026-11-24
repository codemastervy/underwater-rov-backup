#!/usr/bin/env python3
"""The ROV Backup app (laptop + Arduino Mega). This one program is everything: it opens the window and runs the
motor program (rov.py). Closing it stops the motors. Start it from the app menu, or with run.sh.

No mouse click can arm the motors: that takes the controller (OPTIONS), or holding Enter while a keyboard profile
is in use. It works at any size, with the mouse alone or the keyboard alone.

  --fullscreen          full screen instead of in a window (F11 switches)
  --size 1200x720       size of the window
  --fps 60              limit the frame rate (0 = no limit, the default)
  --show-fps            show the frame rate (F12 switches)
  --margins 32,22,32,22 left, top, right, bottom: pixels the monitor hides (instead of SYSTEM > Edge margin)
  --page camera         page to start on
  --dry-run             try it without a Mega: nothing is sent to motors
"""
import argparse
import importlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OLD_STATUS = "/dev/shm/rov-status.json"       # written all the time by motor programs from before the one-app version
CLOSING = 5.0                                 # seconds a program that is just closing gets
RAN_WELL = 10.0                               # seconds: an app that ran this long is opened again after a crash


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
    """Say why the app can't open, on the desktop too: the app menu shows no terminal."""
    try:
        subprocess.run(["notify-send", "-u", "critical", "ROV Backup", text], check=False)
    except OSError:
        pass
    sys.exit(text)


def old_version_runs():
    """Does a motor program from before the one-app version run? It would fight this one over the motors."""
    try:
        with open(OLD_STATUS) as f:
            return abs(time.time() - json.load(f)["ts"]) < 2.0
    except (OSError, ValueError, KeyError, TypeError):
        return False


def wait_for(get):
    """get(), tried for a few seconds until it gives something."""
    end = time.monotonic() + CLOSING
    while not (result := get()) and time.monotonic() < end:
        time.sleep(0.2)
    return result


def write_crash():
    from dashboard.program import open_log
    traceback.print_exc()
    with open_log() as log:
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} the app crashed\n{traceback.format_exc()}".encode())


def start_again():
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])


def restarter():
    """A function that opens the app again. A thread that is there before the window lowers its priority does it:
    the new app, and so the motor program, must have the normal one."""
    go = threading.Event()

    def wait():
        go.wait()
        start_again()

    def open_again():
        go.set()
        time.sleep(10)                        # start_again() ends this program long before that

    threading.Thread(target=wait, daemon=True).start()
    return open_again


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--size", type=lambda s: numbers(s, "x"), default=None)
    parser.add_argument("--fps", type=int, default=0)
    parser.add_argument("--show-fps", action="store_true")
    parser.add_argument("--margins", type=lambda s: numbers(s, ","))
    parser.add_argument("--page", default="drive",
                        choices=("drive", "camera", "autopilot", "profiles", "motors", "system"))
    parser.add_argument("--dry-run", action="store_true")
    args, _ = parser.parse_known_args()
    if importlib.util.find_spec("pygame") is None:
        print("The dashboard needs pygame, installing it…", flush=True)
        if not repair():
            give_up("The dashboard needs pygame. Run the setup command from the README once.")
        start_again()

    blocked, folder = "", None
    if args.dry_run:                          # its own files, so it can't reach the motors of an app that is open
        folder = os.environ["ROV_RUN_DIR"] = tempfile.mkdtemp(prefix="rov-dry-")
    import rov
    held = args.dry_run or wait_for(lambda: rov.lock("app"))           # kept until the app ends
    if not held:
        give_up("ROV Backup is already open.")
    if not args.dry_run and old_version_runs() and not wait_for(lambda: not old_version_runs()):
        blocked = "An older ROV Backup is still running: close it, then open this one again"
    from dashboard.app import App
    from dashboard.program import MotorProgram
    app = App(args.fullscreen and not args.windowed, args.size, args.margins, args.fps, args.show_fps, args.page,
              MotorProgram(args.dry_run, blocked))
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: app.quit())
    open_again = restarter()
    started, crashed = time.monotonic(), False
    try:
        app.run()                             # stops the motors whatever happens
    except Exception:
        crashed = True
        write_crash()
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    if app.restart or crashed and time.monotonic() - started > RAN_WELL:
        open_again()
    sys.exit(int(crashed))


if __name__ == "__main__":
    main()
