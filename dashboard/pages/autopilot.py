"""AUTOPILOT: modes and settings."""
import colorsys

import rov as core

from ..layout import columns, inset, rows
from ..theme import AMBER, BODY, GAP, GREEN, MUTED, TEXT, TITLE
from . import Page

MODES = [("cruise", "CRUISE", "hold thrust"), ("line", "LINE", "follow a line"),
         ("target", "TARGET", "follow a colour"), ("heading", "HEADING", "keep facing"), ("mission", "MISSION", "run the steps")]
LEFT = [("heave_trim", "HOVER TRIM"), ("pitch_trim", "NOSE TRIM"), ("power", "AUTO POWER"), ("speed", "AUTO SPEED")]
RIGHT = [("line_colour", "LINE COLOUR"), ("target_colour", "TARGET COLOUR"), ("target_size", "TARGET DISTANCE"),
         ("steer", "STEERING")]
COLOURS = [(c, c.upper()) for c in core.AP_COLOURS]


def show(key):
    """How a setting is written."""
    if key == "steer":
        return lambda v: f"x{v:.1f}"
    if key == "target_size":
        return lambda v: f"{v * 100:.0f}% of view"
    return (lambda v: f"{v * 100:+.0f}%") if key.endswith("trim") else (lambda v: f"{v * 100:.0f}%")


class AutopilotPage(Page):
    name, title = "autopilot", "AUTOPILOT"

    def draw(self, area):
        ui = self.ui
        head, modes, settings, buttons = rows(area, ui.px(58), ui.px(54), 1, ui.px(44), gap=ui.px(GAP))
        self._draw_head(head)
        self._draw_modes(modes)
        if ui.portrait:
            sides = rows(settings, 1, 1, gap=ui.px(6))
        else:
            sides = columns(settings, 1, 1, gap=ui.px(GAP))
        for side, entries in zip(sides, (LEFT, RIGHT)):
            for line, (key, label) in zip(rows(side, *[1] * len(entries), gap=ui.px(6)), entries):
                self._draw_setting(line, key, label)
        self._draw_buttons(buttons)

    def _draw_head(self, rect):
        ui, status = self.ui, self.status
        ui.card(rect)
        text, stop, switch = columns(inset(rect, ui.px(10), ui.px(8)), 1, ui.px(120), ui.px(100), gap=ui.px(GAP))
        title, info = rows(text, 1, 1)
        ui.text("AUTOPILOT", title, TITLE, TEXT, True)
        if status.ap_mode:
            ui.text(f"{status.ap_mode.upper()}: {status.ap_info}", info, BODY, GREEN, True)
        else:
            ui.text("On: trim is active. Arm, then pick a mode." if status.ap_enabled else
                    "Off at every start. Any stick or drive key, or STOP, takes over.", info, BODY, MUTED)
        if ui.button(stop, "MODE OFF", style="danger", enabled=bool(status.ap_mode)):
            self.link.send("ap_off")
        if ui.toggle(switch, status.ap_enabled, "master", status.online):
            self.link.send(f"ap_enable:{int(not status.ap_enabled)}")

    def _draw_modes(self, rect):
        ui, status = self.ui, self.status
        for i, (cell, (mode, label, sub)) in enumerate(zip(columns(rect, *[1] * len(MODES), gap=ui.px(6)), MODES)):
            if ui.button(cell, f"{i + 1}  {label}", ("mode", mode), "good" if status.ap_mode == mode else "normal",
                         sub=sub, size=BODY, enabled=status.ap_enabled):
                self.app.autopilot(mode)

    def _draw_setting(self, line, key, label):
        ui, settings = self.ui, self.status.ap_settings
        ui.card(line)
        name, control = columns(inset(line, ui.px(10), ui.px(4)), 1, 1.35, gap=ui.px(GAP))
        ui.text(label, name, BODY, TEXT, True)
        if key not in settings:
            ui.text("--", control, BODY, MUTED, align="center")
            return
        if key in core.AP_CHOICES:
            new = ui.choice(control, settings[key], self._colours(key), ("ap", key), self._colour_shown(key))
        else:
            _, low, high, step = core.AP_SETTINGS[key]
            new = ui.number(control, settings[key], low, high, step, show(key), ("ap", key))
        if new is not None:
            self.link.set(f"ap_set:{key}", new)

    def _colours(self, key):
        picked = self.status.ap_settings.get("target_pick")
        if key == "target_colour":
            return COLOURS[:-1] + [("picked", "■ PICKED" if picked else "PICK ON CAMERA")]
        return COLOURS[:-1]              # only the target colour can be picked on the camera

    def _colour_shown(self, key):
        settings = self.status.ap_settings
        if key != "target_colour" or settings.get(key) != "picked":
            return TEXT
        if not settings.get("target_pick"):
            return AMBER
        hue, saturation, brightness = settings["target_pick"]
        return "#%02x%02x%02x" % tuple(int(v * 255) for v in
                                       colorsys.hsv_to_rgb(hue / 360, saturation, max(0.35, brightness)))

    def _draw_buttons(self, rect):
        ui = self.ui
        mission, pick, camera = columns(rect, 1, 1.2, 1, gap=ui.px(GAP))
        if ui.button(mission, "EDIT MISSION"):
            self.app.show("mission")
        if ui.button(pick, "PICK TARGET COLOUR"):
            self.app.show("camera")
            self.app.page.start_picking()
        if ui.button(camera, "CAMERA VIEW"):
            self.app.show("camera")
