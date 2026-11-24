"""MOTORS: which way each motor turns, and the trim for the frame that is fitted."""
import rov as core

from ..layout import columns, inset, rows
from ..theme import AMBER, BODY, GAP, LABEL, MUTED, RAISED, SMALL, TEXT
from . import Page

TRIMS = {"pitch_mix": ("TILT POWER", "how hard tilt pushes"), "heave_front": ("FRONT UP MOTOR", "share of up/down"),
         "heave_rear": ("REAR UP MOTOR", "share of up/down"),
         "surge_pitch": ("LEVEL WHEN DRIVING", "tilt added with forward")}
STEP = 0.05


class MotorsPage(Page):
    name, title = "motors", "MOTORS"

    def draw(self, area):
        ui = self.ui
        unlocked = self.status.online and not self.status.armed
        split = rows if ui.portrait else columns
        direction, frame = split(area, 1, 1.15, gap=ui.px(GAP))
        self._draw_directions(ui.card(direction, "DIRECTION   pushes the wrong way? FLIP it"), unlocked)
        self._draw_frame(ui.card(frame, "FRAME   pick the one you built"), unlocked)

    def _draw_directions(self, area, unlocked):
        ui, motors = self.ui, self.status.motors
        for i, (line, motor) in enumerate(zip(rows(area, *[1] * max(1, len(motors)), gap=ui.px(6)), motors)):
            ui.box(line, RAISED, 8)
            about, state, flip = columns(inset(line, ui.px(10), ui.px(8)), 1, ui.px(76), ui.px(88), gap=ui.px(6))
            name, job = rows(about, 1, 1)
            ui.text(motor["name"].replace("_", " "), name, LABEL, TEXT, True)
            ui.text(motor.get("job", ""), job, SMALL, MUTED)
            flipped = motor.get("invert", False)
            ui.text("FLIPPED" if flipped else "normal", state, BODY, AMBER if flipped else MUTED, flipped, "center")
            if ui.button(flip, "FLIP", ("flip", i), enabled=unlocked):
                self.link.send(f"invert:{i}")

    def _draw_frame(self, area, unlocked):
        ui, status = self.ui, self.status
        current = next((d for d in status.designs if d["id"] == status.design), None)
        if not current:
            return
        picks, note, *lines, reset = rows(area, ui.px(40), ui.px(34), *[1] * len(TRIMS), ui.px(38), gap=ui.px(6))
        for cell, design in zip(columns(picks, *[1] * len(status.designs), gap=ui.px(6)), status.designs):
            if ui.button(cell, design.get("short", design["name"]).upper(), ("design", design["id"]),
                         "selected" if design is current else "normal", enabled=unlocked, size=BODY):
                self.link.send(f"design:{design['id']}")
        ui.paragraph(current.get("note", ""), note, SMALL)
        for line, (key, (label, about)) in zip(lines, TRIMS.items()):
            text, control = columns(line, 1, 1.1, gap=ui.px(GAP))
            name, sub = rows(text, 1, 1)
            ui.text(label, name, BODY, TEXT, True)
            ui.text(about, sub, SMALL, MUTED)
            low, high = core.DESIGN_LIMITS[key]
            new = ui.number(control, current[key], low, high, STEP, lambda v: f"{v:.2f}", ("trim", current["id"], key),
                            unlocked)
            if new is not None:
                self.link.set(f"design_set:{key}", new)
        if ui.button(reset, "DEFAULT TRIM", enabled=unlocked, size=BODY):
            self.link.send("design_reset")
