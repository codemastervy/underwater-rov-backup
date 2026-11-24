"""Driving with a keyboard profile."""
import rov as core

BEAT = 0.05            # seconds between reports of the held keys (rov.py disarms after 0.5 s without one)
HOLD_TO_ARM = 0.5      # seconds Enter must be held: a tap also picks things on the screen


class KeyboardDrive:
    def __init__(self, link):
        self.link = link
        self.held = set()
        self._enter_since = None
        self._next_beat = 0.0

    @property
    def in_use(self):
        return self.link.status.keyboard

    def uses(self, key):
        """Is this key in the profile?"""
        return key in self.link.status.key_map.values()

    def press(self, key):
        keymap = self.link.status.key_map
        if key == keymap.get("speed_up"):
            self.link.send("speed+")
        elif key == keymap.get("speed_down"):
            self.link.send("speed-")
        elif key in core.key_axes(keymap):
            self.held.add(key)
            self._next_beat = 0.0           # report it at once

    def release(self, key):
        if key in self.held:
            self.held.discard(key)
            self._next_beat = 0.0

    def release_all(self):
        self._enter_since = None
        if self.held:
            self.held.clear()
            self._next_beat = 0.0

    def hold_enter(self, now):
        self._enter_since = now

    def release_enter(self):
        """True if it was let go too early to arm."""
        early, self._enter_since = self._enter_since is not None, None
        return early

    def arming(self, now):
        """How far Enter has been held, 0..1."""
        if self._enter_since is None or self.link.status.armed:
            return 0.0
        return min(1.0, (now - self._enter_since) / HOLD_TO_ARM)

    def update(self, now, focused, driving):
        """focused: this window has the keyboard. driving: the keys drive rather than work the screen."""
        if not (self.in_use and focused and driving):
            self.release_all()
        if not (self.in_use and focused):
            return                            # no reports: rov.py disarms
        if now >= self._next_beat:
            self._next_beat = now + BEAT
            self.link.send_keys(self.held)
        if self._enter_since is not None and self.arming(now) >= 1:
            self._enter_since = None
            self.link.send("kbd_arm")
