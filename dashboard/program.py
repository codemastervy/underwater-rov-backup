"""The motor program (rov.py): the app starts it, starts it again if it stops, and stops it when the app closes.

It is a process of its own so that a slow screen can't hold up the motors; nothing else has to be started or
installed for it. What it prints goes to rov.log."""
import os
import subprocess
import sys
import threading

import rov as core

AGAIN = 1.0              # seconds before it is started again
PATIENCE = 4.0           # seconds it may take to switch the motors off and stop
LOG_LIMIT = 1_000_000    # bytes: a longer log is put aside (rov.log.old) when the app opens


def log_path():
    return os.path.join(core.HERE, "rov.log")


def open_log():
    """The log to add to. Without a place to write it, the messages are dropped."""
    try:
        if os.path.exists(log_path()) and os.path.getsize(log_path()) > LOG_LIMIT:
            os.replace(log_path(), log_path() + ".old")
        return open(log_path(), "ab")
    except OSError:
        return open(os.devnull, "ab")


def last_line(since):
    """The last thing in the log after position `since`: why the motor program stopped."""
    try:
        with open(log_path(), "rb") as f:
            f.seek(since)
            lines = f.read().decode(errors="replace").split("\n")
    except OSError:
        return ""
    return next((line.strip() for line in reversed(lines) if line.strip()), "")


class MotorProgram(threading.Thread):
    def __init__(self, dry_run=False, blocked=""):
        """blocked: why it must not be started at all, or ""."""
        super().__init__(daemon=True)
        self.command = [sys.executable, os.path.join(core.HERE, "rov.py"), "--app", str(os.getpid()),
                        "--config", os.path.join(core.HERE, "config.toml"), *(["--dry-run"] if dry_run else [])]
        self.blocked = blocked
        self.problem = blocked             # why it isn't running, for the screen
        self.stops = 0                     # how often it stopped by itself
        self._process = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    def run(self):
        if self.blocked:
            return
        with open_log() as log:
            while not self._stopping.is_set():
                since = log.seek(0, os.SEEK_END)
                with self._lock:           # so that stop() either sees the process or prevents it
                    self._process = None
                    if self._stopping.is_set():
                        return
                    try:
                        self._process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=log,
                                                         stderr=subprocess.STDOUT)
                    except OSError as ex:
                        self.problem = f"Can't start the motor program: {ex}"
                if self._process:
                    self._process.wait()
                    if self._stopping.is_set():
                        return
                    self.problem = f"Motor program stopped: {last_line(since) or 'no message'}"
                    self.stops += 1
                self._stopping.wait(AGAIN)

    def stop(self):
        """Ends the motor program; it switches the motors off first."""
        self._stopping.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(PATIENCE)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
