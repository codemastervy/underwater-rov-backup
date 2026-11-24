"""PROFILES: what drives the ROV and how. Exactly one profile is in use; its type (controller or keyboard)
decides what drives. Changes are saved at once, and only while SAFE."""
import json

import rov as core

from ..dialogs import ConfirmDialog, KeyDialog, TextDialog
from ..keynames import key_label
from ..layout import columns, grid, inset, rows
from ..link import write_file
from ..theme import AMBER, BODY, GAP, LABEL, MUTED, RED, SMALL
from . import Page

ACTIONS = [("none", "— nothing"), ("forward", "forward / back"), ("turn", "turn"), ("updown", "up / down"),
           ("tilt", "tilt (nose)"), ("sideways", "sideways"), ("left_side", "left side (tank)"),
           ("right_side", "right side (tank)")]
NAME_LENGTH = 24


class ProfilesPage(Page):
    name, title = "profiles", "PROFILES"

    def __init__(self, app):
        super().__init__(app)
        self.profiles, self.index = [], 0

    def opened(self):
        try:
            with open(core.profiles_path()) as f:
                found = core.clean_profiles(json.load(f))
        except (OSError, ValueError):
            found = []
        self.profiles = core.complete_profiles(found)
        names = [p["name"] for p in self.profiles]
        self.index = names.index(self.status.profile_name) if self.status.profile_name in names else 0

    @property
    def profile(self):
        return self.profiles[self.index]

    def _save(self, use=""):
        """use: the profile to use from now on ("" = keep the one in use)."""
        if write_file(core.profiles_path(), json.dumps({"profiles": self.profiles}, indent=2)):
            self.link.send(f"reload_profiles:{use}")
        else:
            self.app.notice("Could not save the profiles", RED)

    def _free_name(self, base):
        names, name, n = {p["name"] for p in self.profiles}, base, 2
        while name in names:
            name, n = f"{base} {n}", n + 1
        return name

    def _add(self, profile):
        self.profiles.append(profile)
        self.index = len(self.profiles) - 1
        self._save()

    def _delete(self):
        del self.profiles[self.index]
        self.index = min(self.index, len(self.profiles) - 1)
        self._save()

    def _rename(self, name):
        name = name.strip()[:NAME_LENGTH]
        old = self.profile["name"]
        if not name or name == old:
            return
        if any(p["name"] == name for p in self.profiles):
            self.app.notice(f"There is already a profile called {name}", AMBER)
            return
        self.profile["name"] = name
        self._save(name if old == self.status.profile_name else "")

    def _set_key(self, action, key):
        keys = self.profile["keys"]
        for other, used in keys.items():
            if used == key and other != action:              # already used: the two swap
                keys[other] = keys.get(action, "")
                self.app.notice(f"{key_label(key)} was already used: swapped", AMBER)
        keys[action] = key
        self._save()

    # ------------------------------------------------------------ drawing
    def draw(self, area):
        ui = self.ui
        if not self.profiles:
            self.opened()
        if ui.portrait:
            side, editor = rows(area, ui.px(226), 1, gap=ui.px(GAP))
        else:
            side, editor = columns(area, ui.px(220), 1, gap=ui.px(GAP))
        self._draw_list(side)
        self._draw_editor(ui.card(editor), not self.status.armed)

    def _draw_list(self, area):
        ui, unlocked = self.ui, not self.status.armed
        box, new, change = rows(area, 1, ui.px(40), ui.px(40), gap=ui.px(6))
        ui.card(box)
        items = [(("● " if p["name"] == self.status.profile_name else "    ") + p["name"],
                  "KEYS" if p["type"] == "keyboard" else "PAD") for p in self.profiles]
        self.index, picked = ui.listbox(inset(box, ui.px(6)), items, self.index, "list", 30)
        pad, keys = columns(new, 1, 1, gap=ui.px(6))
        if ui.button(pad, "CONTROLLER", "new pad", icon="plus", size=SMALL, enabled=unlocked):
            standard = next(p for p in core.DEFAULT_PROFILES if p["name"] == "Standard")
            self._add(core.clean_profiles({"profiles": [{**standard, "name": self._free_name("New controller")}]})[0])
        if ui.button(keys, "KEYBOARD", "new keys", icon="plus", size=SMALL, enabled=unlocked):
            self._add({"name": self._free_name("New keys"), "type": "keyboard", "keys": dict(core.KEY_PRESETS["WASD"])})
        copy, delete = columns(change, 1, 1, gap=ui.px(6))
        if ui.button(copy, "COPY", size=BODY, enabled=unlocked):
            twin = json.loads(json.dumps(self.profile))
            self._add({**twin, "name": self._free_name(twin["name"][:18] + " copy")})
        last = sum(p["type"] == self.profile["type"] for p in self.profiles) == 1
        if ui.button(delete, "DELETE", size=BODY, enabled=unlocked and not last):
            self.app.open_dialog(ConfirmDialog(self.app, "Delete this profile?", self.profile["name"], "DELETE",
                                               self._delete))

    def _draw_editor(self, area, unlocked):
        ui, profile = self.ui, self.profile
        head, body = rows(area, ui.px(40), 1, gap=ui.px(7))
        name, use = columns(head, 1, ui.px(120), gap=ui.px(GAP))
        if ui.field(name, profile["name"], "name", unlocked):
            self.app.open_dialog(TextDialog(self.app, "PROFILE NAME", profile["name"], self._rename, NAME_LENGTH))
        in_use = profile["name"] == self.status.profile_name
        if ui.button(use, "IN USE" if in_use else "USE", "use", "good", "check" if in_use else None,
                     enabled=unlocked and not in_use):
            self.link.send(f"use_profile:{profile['name']}")
        if profile["type"] == "keyboard":
            self._draw_keys(body, unlocked)
        else:
            self._draw_controls(body, unlocked)

    def _draw_controls(self, area, unlocked):
        ui = self.ui
        head, *lines = rows(area, ui.px(15), *[1] * len(core.INPUTS), gap=ui.px(4))
        for cell, name in zip(self._cells(head), ("CONTROL", "DOES", "REVERSE", "POWER")):
            ui.text(name, cell, SMALL, MUTED, True, "left" if name == "CONTROL" else "center")
        for line, control in zip(lines, core.INPUTS):
            entry = {"action": "none", "invert": False, "power": 1.0, **self.profile["map"].get(control, {})}
            label, does, reverse, power = self._cells(line)
            ui.text(core.INPUT_LABELS[control], label, BODY)
            changed = {"action": ui.choice(does, entry["action"], ACTIONS, ("does", control), enabled=unlocked),
                       "power": ui.number(power, entry["power"], 0.05, 1.0, 0.05, lambda v: f"{v * 100:.0f}%",
                                          ("power", control), unlocked)}
            if ui.button(reverse, "REV" if entry["invert"] else "—", ("reverse", control),
                         "warn" if entry["invert"] else "normal", size=BODY, enabled=unlocked):
                changed["invert"] = not entry["invert"]
            changed = {key: value for key, value in changed.items() if value is not None}
            if changed:
                self.profile["map"][control] = {**entry, **changed}
                self._save()

    def _cells(self, line):
        ui = self.ui
        return columns(line, 1, 1.75, ui.px(62), 1.15, gap=ui.px(6))

    def _draw_keys(self, area, unlocked):
        ui = self.ui
        table, note = rows(area, 1, ui.px(18), gap=ui.px(6))
        keys = self.profile["keys"]
        cells = grid(table, 2, (len(core.KEY_ACTIONS) + 1) // 2, ui.px(6))
        for cell, (action, label, *_) in zip(cells, core.KEY_ACTIONS):
            name, key = columns(cell, 1, 1, gap=ui.px(6))
            ui.text(label, name, LABEL)
            if ui.button(key, key_label(keys.get(action)), ("key", action), enabled=unlocked):
                self.app.open_dialog(KeyDialog(self.app, label, set(keys.values()),
                                               lambda key, action=action: self._set_key(action, key)))
        ui.text("Hold Enter = ARM and Esc = STOP in every keyboard profile", note, SMALL, MUTED)
