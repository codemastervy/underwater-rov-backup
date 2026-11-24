"""The dashboard's end of the files it shares with rov.py in /dev/shm."""
import json
import os
import time
from collections import deque

import rov as core

STALE = 2.0      # seconds without news: the ROV program isn't running


def write_file(path, text):
    """Replace a file in one go, so the reader never sees half of it."""
    try:
        with open(path + ".tmp", "w") as f:
            f.write(text)
        os.replace(path + ".tmp", path)
        return True
    except OSError:
        return False


class Status:
    """What rov.py reports. Every field has a default, so pages never have to check for missing ones."""
    DEFAULTS = {"ts": 0.0, "armed": False, "controller": None, "heartbeat": False, "speed": 0, "profile_name": "",
                "profile_type": "controller", "profile_map": {}, "key_map": {}, "profiles": [], "view_toggle": None,
                "motors": [], "axes": None, "switch_on": True, "keys": [], "arm_sound": True, "sound_volume": 5,
                "output_ok": True,
                "output": "", "playing": False, "tune_name": "", "tune_secs": 0, "tune_custom": False, "design": "", "designs": [],
                "msg": ""}

    def __init__(self, data=None):
        self.online = data is not None
        data = data or {}
        for field, default in self.DEFAULTS.items():
            setattr(self, field, data.get(field, default))
        autopilot = data.get("autopilot") or {}
        self.ap_enabled = bool(autopilot.get("enabled"))
        self.ap_mode = autopilot.get("mode")
        self.ap_info = autopilot.get("info", "")
        self.ap_epoch = autopilot.get("epoch", 0)
        self.ap_settings = autopilot.get("settings") or {}
        self.ap_alert = autopilot.get("alert", "") if self.ap_enabled else ""

    @property
    def keyboard(self):
        """Is a keyboard profile in use?"""
        return self.profile_type == "keyboard"


class RovLink:
    def __init__(self):
        self.status = Status()
        self._seen = None
        self._waiting = deque()

    def poll(self):
        """Read the newest status and hand rov.py the next command."""
        try:
            info = os.stat(core.STATUS_PATH)
            stamp = info.st_mtime_ns, info.st_ino
            if stamp != self._seen:
                with open(core.STATUS_PATH) as f:
                    self.status = Status(json.load(f))
                self._seen = stamp
        except (OSError, ValueError):
            pass
        if self.status.online and time.time() - self.status.ts > STALE:
            self.status = Status()
        if not self.status.online:
            self._waiting.clear()
        elif self._waiting and not os.path.exists(core.CMD_PATH):      # rov.py has taken the one before
            write_file(core.CMD_PATH, self._waiting.popleft())

    def send(self, command):
        self._waiting.append(command)

    def set(self, name, value):
        """Send a setting. It replaces an older value that is still waiting."""
        if self._waiting and self._waiting[-1].startswith(name + ":"):
            self._waiting.pop()
        self.send(f"{name}:{value}")

    def stop(self):
        """STOP goes first and drops everything that was waiting."""
        self._waiting.clear()
        write_file(core.CMD_PATH, "stop")

    @staticmethod
    def send_keys(keys):
        write_file(core.KEYS_PATH, json.dumps({"ts": time.time(), "keys": sorted(keys)}))

    @staticmethod
    def send_vision(result):
        write_file(core.VISION_PATH, json.dumps(result))
