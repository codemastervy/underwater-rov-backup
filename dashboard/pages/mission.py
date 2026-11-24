"""MISSION: the steps the autopilot's MISSION mode runs. Saved as you change them."""
import json
import os

import rov as core

from ..dialogs import ConfirmDialog
from ..layout import columns, inset, rows
from ..link import write_file
from ..theme import BODY, GAP, MUTED, RED, SMALL, TEXT, TITLE
from . import Page

MOST = core.MISSION_MOST
ACTIONS = [(name, name.replace("_", " ").upper()) for name in core.MISSION_ORDER]


class MissionPage(Page):
    name, title = "mission", "MISSION"

    def __init__(self, app):
        super().__init__(app)
        self.steps = []

    @property
    def path(self):
        return os.path.join(core.HERE, "mission.json")

    def opened(self):
        try:
            with open(self.path) as f:
                self.steps = core.clean_mission(json.load(f))
        except (OSError, ValueError):
            self.steps = [dict(step) for step in core.DEFAULT_MISSION]

    def back(self):
        self.app.show("autopilot")
        return True

    def _save(self):
        if not write_file(self.path, json.dumps({"steps": self.steps}, indent=1)):
            self.app.notice("Could not save the mission", RED)

    def _reset(self):
        self.steps = [dict(step) for step in core.DEFAULT_MISSION]
        self._save()

    def draw(self, area):
        ui = self.ui
        head, names, table, buttons = rows(area, ui.px(36), ui.px(14), 1, ui.px(40), gap=ui.px(4))
        self._draw_head(head)
        for cell, name in zip(self._cells(names), ("", "DO", "POWER", "TIME", "")):
            ui.text(name, cell, SMALL, MUTED, True, "center")
        lines = rows(table, *[1] * MOST, gap=ui.px(3))
        deleted = [i for i, line in enumerate(lines[:len(self.steps)]) if self._draw_step(line, i)]
        for i in deleted:
            del self.steps[i]
            self._save()
        self._draw_buttons(buttons)

    def _cells(self, line):
        ui = self.ui
        return columns(inset(line, ui.px(8), 0), ui.px(24), 1.5, 1, 1, ui.px(44), gap=ui.px(GAP))

    def _draw_head(self, rect):
        ui = self.ui
        back, title, total = columns(rect, ui.px(130), 1, ui.px(170), gap=ui.px(12))
        if ui.button(back, "AUTOPILOT", "back", icon="back", size=BODY):
            self.back()
        ui.text("MISSION", title, TITLE, TEXT, True)
        ui.text(f"{len(self.steps)} steps · {sum(s['secs'] for s in self.steps):.1f} s", total, BODY, MUTED,
                align="right")

    def _draw_step(self, line, i):
        """True if the step is to be deleted."""
        ui, step = self.ui, self.steps[i]
        ui.card(line)
        number, do, power, secs, delete = (inset(cell, 0, ui.px(1)) for cell in self._cells(line))
        ui.text(str(i + 1), number, BODY, MUTED, True)
        changed = {"do": ui.choice(do, step["do"], ACTIONS, ("do", i)),
                   "power": ui.number(power, step["power"], 0.0, 1.0, 0.05, lambda v: f"{v * 100:.0f}%", ("power", i),
                                      step["do"] != "hover"),
                   "secs": ui.number(secs, step["secs"], 0.5, 60.0, 0.5, lambda v: f"{v:.1f} s", ("secs", i))}
        changed = {key: value for key, value in changed.items() if value is not None}
        if changed:
            step.update(changed)
            self._save()
        return ui.button(delete, icon="cross", wid=("delete", i), size=15)

    def _draw_buttons(self, rect):
        ui = self.ui
        add, reset = columns(rect, 1, 1, gap=ui.px(GAP))
        if ui.button(add, "ADD STEP", icon="plus", enabled=len(self.steps) < MOST):
            self.steps.append({"do": "forward", "power": 0.4, "secs": 2.0})
            self._save()
        if ui.button(reset, "RESET TO EXAMPLE"):
            self.app.open_dialog(ConfirmDialog(self.app, "Reset the mission?",
                                               "Your steps are replaced by the example.", "RESET", self._reset))
