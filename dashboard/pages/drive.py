"""DRIVE: what the ROV is doing, and the controls for it."""
import pygame

import rov as core

from ..keynames import key_label
from ..layout import columns, grid, inset, rows
from ..theme import (ACCENT, ACCENT_DARK, AMBER, BODY, FAINT, GAP, GREEN, HUGE, LABEL, LARGE, LINE, MUTED, RAISED,
                     SMALL, TEXT, TRACK, WHITE)
from . import Page

WORDS = {"none": "", "forward": "fwd/back", "turn": "turn", "updown": "up/down", "tilt": "tilt",
         "sideways": "sideways", "left_side": "left side", "right_side": "right side"}
ONE_WAY = {"forward": ("forward", "back"), "turn": ("turn R", "turn L"), "updown": ("up", "down"),
           "tilt": ("nose up", "nose down"), "sideways": ("right", "left"), "left_side": ("left fwd", "left back"),
           "right_side": ("right fwd", "right back")}


def stick_label(profile, *inputs):
    words = [WORDS[profile.get(k, {}).get("action", "none")] for k in inputs]
    return " + ".join(w for w in words if w) or "—"


def push_label(profile, control, positive=True):
    """What pressing a trigger or one side of the D-pad does."""
    entry = profile.get(control, {})
    words = ONE_WAY.get(entry.get("action"))
    return words[positive == bool(entry.get("invert"))] if words else "—"


def pad_name(name):
    return "PS5 DualSense" if "DualSense" in name else "PS4 controller" if "Wireless Controller" in name else name


class DrivePage(Page):
    name, title = "drive", "DRIVE"
    drives = True

    @property
    def footer(self):
        return 210 if self.ui.portrait else 142

    def draw(self, area):
        ui = self.ui
        if ui.portrait:
            motors, controls = rows(area, 1, 1.1, gap=ui.px(GAP))
        else:
            motors, controls = columns(area, 1.15, 1, gap=ui.px(GAP))
        self._draw_motors(ui.card(motors, "THRUSTERS"))
        if self.status.keyboard:
            self._draw_keys(controls)
        else:
            self._draw_controller(controls)

    def _draw_motors(self, area):
        ui, status = self.ui, self.status
        if not status.motors:
            ui.text("No data from the ROV program", area, LABEL, FAINT, align="center")
            return
        for i, (line, motor) in enumerate(zip(rows(area, *[1] * len(status.motors), gap=ui.px(4)), status.motors)):
            value = ui.glide(("motor", i), motor["value"])
            label, bar = rows(line, 1.1, 1)
            ui.text(motor["name"].replace("_", " "), label, LABEL)
            ui.text(f"{motor['value'] * 100:+.0f}%", label, LABEL, TEXT if status.armed else MUTED, True, "right")
            bar = pygame.Rect(bar.x, bar.y, bar.w, min(bar.h - ui.px(4), ui.px(18)))
            ui.thrust(bar, value, (GREEN if value >= 0 else ACCENT) if status.armed else FAINT)

    # ------------------------------------------------------------ controller profile
    def _draw_controller(self, card):
        ui, status = self.ui, self.status
        area = ui.card(card, "CONTROLLER")
        state, color = "not connected", AMBER
        if status.controller:
            state, color = f"{pad_name(status.controller)} · " + ("link OK" if status.heartbeat else "connected"), MUTED
        self._card_note(card, state, color)
        sticks, triggers, dpad = rows(area, 1, ui.px(38), ui.px(18), gap=ui.px(4))
        axes = status.axes or {}
        for side, (name, x, y) in zip(columns(sticks, 1, 1, gap=ui.px(GAP)), (("L", "lx", "ly"), ("R", "rx", "ry"))):
            self._draw_stick(side, name, x, y, axes)
        for side, control in zip(columns(triggers, 1, 1, gap=ui.px(24)), ("l2", "r2")):
            bar, label = rows(inset(side, ui.px(GAP), 0), ui.px(13), 1, gap=ui.px(3))
            ui.meter(bar, ui.glide(control, axes.get(control, 0)), ACCENT)
            ui.text(f"{control.upper()} {push_label(status.profile_map, control)}", label, SMALL, MUTED, align="center")
        up, across = axes.get("dy", 0), axes.get("dx", 0)
        if abs(up) > 0.5 or abs(across) > 0.5:
            control, positive = ("dy", up < 0) if abs(up) > 0.5 else ("dx", across > 0)
            ui.text("D-pad: " + push_label(status.profile_map, control, positive), dpad, SMALL, AMBER, True, "center")
        else:
            ui.text("D-pad = " + stick_label(status.profile_map, "dy", "dx"), dpad, SMALL, MUTED, align="center")

    def _draw_stick(self, area, name, x, y, axes):
        ui = self.ui
        circle, label = rows(area, 1, ui.px(18))
        radius = max(8, min(circle.w, circle.h) // 2 - ui.px(4))
        cx, cy = circle.center
        ui.circle((cx, cy), radius, LINE, ui.px(2))
        ui.fill(pygame.Rect(cx - radius, cy, 2 * radius, 1), TRACK)
        ui.fill(pygame.Rect(cx, cy - radius, 1, 2 * radius), TRACK)
        reach = radius - ui.px(8)
        ui.dot((cx + ui.glide(x, axes.get(x, 0), 0.02) * reach, cy + ui.glide(y, axes.get(y, 0), 0.02) * reach),
               ui.px(9), ACCENT if axes else LINE)
        ui.text(f"{name}  {stick_label(self.status.profile_map, y, x)}", label, SMALL, MUTED, align="center")

    # ------------------------------------------------------------ keyboard profile
    def _draw_keys(self, card):
        ui, status = self.ui, self.status
        area = ui.card(card, "KEYBOARD")
        self._card_note(card, *((status.profile_name, MUTED) if self.app.focused
                                else ("click this window to use the keyboard", AMBER)))
        held = self.app.keyboard.held
        for cell, (action, label, *_) in zip(grid(area, 2, (len(core.KEY_ACTIONS) + 1) // 2, ui.px(5)),
                                             core.KEY_ACTIONS):
            key = status.key_map.get(action, "")
            cap, text = columns(cell, ui.px(58), 1, gap=ui.px(8))
            down = key in held
            ui.box(cap, ACCENT_DARK if down else RAISED, 6, ACCENT if down else LINE)
            ui.text(key_label(key), inset(cap, ui.px(4), 0), LABEL, WHITE, True, "center")
            ui.text(label, text, BODY, TEXT if down else MUTED)

    def _card_note(self, card, text, color):
        ui = self.ui
        line = pygame.Rect(card.x + ui.px(110), card.y + ui.px(9), card.w - ui.px(122), ui.line_height(SMALL))
        ui.text(text, line, SMALL, color, align="right")

    # ------------------------------------------------------------ footer
    def draw_footer(self, area):
        ui, status = self.ui, self.status
        strip, bar = rows(area, ui.px(36), 1, gap=ui.px(GAP))
        self.app.draw_message(strip)
        if ui.portrait:
            settings, stop = rows(bar, 1, 1.2, gap=ui.px(GAP))
            profile, speed = columns(settings, 1, 1.1, gap=ui.px(GAP))
        else:
            stop, profile, speed = columns(bar, 1.25, 1.1, 1.1, gap=ui.px(GAP))
        self._draw_profile(profile)
        ui.box(speed, RAISED, 8, LINE)
        steps = ui.stepper(inset(speed, ui.px(8)), f"{status.speed}%" if status.online else "--", "speed",
                           "SPEED   + −   L1 R1", HUGE, status.online)
        if steps:
            self.link.send("speed+" if steps > 0 else "speed-")
        if ui.stop_button(stop, "Esc · CIRCLE", HUGE):
            self.app.stop()

    def _draw_profile(self, rect):
        ui, status = self.ui, self.status
        key = "P" if status.keyboard else "△ · P = keyboard"
        if ui.button(rect, status.profile_name or "PROFILE", "profile", size=LARGE,
                     sub="locked while armed" if status.armed else f"profile · {key}",
                     enabled=status.online and not status.armed):
            options = [(p["name"], f"{p['name']}   ({'keys' if p['type'] == 'keyboard' else 'pad'})")
                       for p in status.profiles]
            names = [name for name, _ in options]
            if names:
                ui.open_menu("profile", rect, options,
                             names.index(status.profile_name) if status.profile_name in names else 0)
        name = ui.answer("profile")
        if name:
            self.link.send(f"use_profile:{name}")
            ui.ring = False           # back to driving
