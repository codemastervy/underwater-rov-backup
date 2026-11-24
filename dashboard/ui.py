"""Widgets. They are drawn again every frame and return what the pilot did with them, so a page is just code
that runs from top to bottom. Each one works with the mouse alone and with the keyboard alone:

  mouse     click, hold, drag, wheel
  keyboard  arrows and Tab move the focus ring, Enter uses the widget in it, Left/Right change a value
"""
import time

import pygame

from .layout import Px, columns, inset
from .painter import Painter
from .theme import (ACCENT, ACCENT_DARK, AMBER, BACKGROUND, BLACK, BODY, CARD, FAINT, GREEN, HOVER, LABEL, LINE, MUTED,
                    RAISED, RED, SMALL, TEXT, TRACK, WHITE)

# style -> fill, fill under the mouse, border, text
STYLES = {
    "normal": (RAISED, HOVER, LINE, TEXT),
    "tab": (CARD, RAISED, None, MUTED),
    "selected": (ACCENT_DARK, ACCENT_DARK, ACCENT, WHITE),
    "good": (GREEN, "#2bb069", None, WHITE),
    "danger": (RED, "#e65050", None, WHITE),
    "warn": (AMBER, "#f2bb52", None, BLACK),
    "disabled": (CARD, CARD, TRACK, FAINT),
}
FLASH = 0.12                 # seconds a pressed button stays lit
REPEAT_AFTER, REPEAT_EVERY = 0.4, 0.09
REPORT_BACK = 0.6            # seconds a chosen value is shown before rov.py's own value counts again
ARROWS = ("up", "down", "left", "right")


class Menu:
    """The list a choice opens."""

    def __init__(self, wid, anchor, options, index, focus):
        self.wid, self.anchor, self.options, self.index, self.focus = wid, anchor, options, index, focus
        self.box = pygame.Rect(0, 0, 0, 0)       # where it was drawn


class UI(Painter):
    def __init__(self):
        super().__init__()
        self.now = 0.0
        self.mouse, self.click, self.held, self.wheel = (-1, -1), None, False, 0
        self.move = None             # up, down, left, right, next, prev, more or less: a key this frame
        self.enter = False
        self.ring = False            # the focus ring is showing: the keyboard works the screen
        self.focus = None
        self.live = True             # False while drawing what is behind a dialog
        self.first = 0               # the widget that gets the focus first
        self.hint = ""
        self.widgets = []            # (id, rect) of everything that can be used this frame
        self.menu = None
        self._answer = None          # (id, value) picked in a menu
        self._grab = None            # (id, part) held by the mouse
        self._repeat_at = 0.0
        self._flash = {}
        self._chosen = {}            # id -> (value, until): picked here, not reported back yet
        self._scroll = {}

    # ------------------------------------------------------------ frame
    def begin(self, surface, area, dt):
        self.surface, self.area, self.dt, self.now = surface, area, dt, time.monotonic()
        self.resize(area.size)
        self.widgets = []
        self.hint = ""
        self.live = self.menu is None
        if not self.held:
            self._grab = None

    def layer(self):
        """Start a dialog: only what is drawn from here on can be used."""
        self.widgets, self.first = [], 0
        self.live = self.menu is None

    def end(self):
        if self.menu:
            self._draw_menu()
        ids = [wid for wid, _ in self.widgets]
        if self.ring and ids:
            if self.focus not in ids:
                self.focus = ids[min(self.first, len(ids) - 1)]
            elif self.move:
                self.focus = self._neighbour(ids)
            self._draw_ring()
        self.click, self.move, self.enter, self.wheel = None, None, False, 0

    def key(self, key, shift, repeat):
        """A key that works the screen. True if it was one."""
        move = {"tab": "prev" if shift else "next", "plus": "more", "equal": "more", "kp_add": "more",
                "minus": "less", "kp_subtract": "less"}.get(key, key if key in ARROWS else None)
        if not move and key not in ("return", "kp_enter", "space"):
            return False
        if not self.ring:
            self.ring = True                 # the first press only shows where the focus is
        elif move:
            self.move = move
        elif not repeat:
            self.enter = True
        return True

    def flash(self, wid):
        self._flash[wid] = self.now + FLASH

    # ------------------------------------------------------------ what every widget does
    def _widget(self, wid, rect, hint="Enter select"):
        """Adds a widget. Returns (has the focus ring, click position or None, mouse over it)."""
        if not self.live:
            return False, None, False
        self.widgets.append((wid, rect.copy()))
        click = self.click if self.click and rect.collidepoint(self.click) else None
        if click:
            self.click, self.focus = None, wid
        focused = self.ring and self.focus == wid
        if focused:
            self.hint = hint
        return focused, click, rect.collidepoint(self.mouse)

    def _entered(self, focused):
        if focused and self.enter:
            self.enter = False
            return True
        return False

    def _moved(self, focused, *moves):
        if focused and self.move in moves:
            self.move = None
            return True
        return False

    def _held_again(self):
        """Hold a button down: is it time for the next step?"""
        if self.now < self._repeat_at:
            return False
        self._repeat_at = self.now + REPEAT_EVERY
        return True

    def _latest(self, wid, value):
        """The value to show: what was just picked here, until rov.py reports it back."""
        chosen, until = self._chosen.get(wid, (None, 0))
        if self.now < until and chosen != value:
            return chosen
        self._chosen.pop(wid, None)
        return value

    def _choose(self, wid, value):
        self._chosen[wid] = (value, self.now + REPORT_BACK)
        return value

    def _neighbour(self, ids):
        """The widget the arrow or Tab key leads to."""
        here = ids.index(self.focus)
        if self.move in ("next", "prev"):
            return ids[(here + (1 if self.move == "next" else -1)) % len(ids)]
        a, best, best_cost = self.widgets[here][1], self.focus, None
        for wid, b in self.widgets:
            ahead, side = {
                "right": (b.left - a.right, max(a.top - b.bottom, b.top - a.bottom)),
                "left": (a.left - b.right, max(a.top - b.bottom, b.top - a.bottom)),
                "down": (b.top - a.bottom, max(a.left - b.right, b.left - a.right)),
                "up": (a.top - b.bottom, max(a.left - b.right, b.left - a.right)),
            }.get(self.move, (-1, 0))
            if ahead < -1:
                continue
            offset = abs(a.centerx - b.centerx) if self.move in ("up", "down") else abs(a.centery - b.centery)
            cost = max(0, ahead) + 3 * max(0, side) + 0.1 * offset
            if wid != self.focus and (best_cost is None or cost < best_cost):
                best, best_cost = wid, cost
        return best

    def _draw_ring(self):
        rect = dict(self.widgets).get(self.focus)
        if not rect:
            return
        x, y, w, h = (self.glide(("ring", i), v, 0.03) for i, v in enumerate(rect))
        ring = inset(pygame.Rect(round(x), round(y), round(w), round(h)), -self.px(3))
        pygame.draw.rect(self.surface, ACCENT, ring, self.px(2.5), border_radius=self.px(10))

    # ------------------------------------------------------------ buttons
    def _paint_button(self, rect, label, icon, sub, size, fill, border, ink):
        self.box(rect, fill, 8, border)
        inside = inset(rect, self.px(5), self.px(2))
        if icon and not label:
            self.icon(icon, rect, ink, size)
            return
        if icon:
            gap, mark = self.px(6), min(self.px(size + 1), inside.h)
            width = min(self.text_width(label, size, True), inside.w - mark - gap)
            left = inside.centerx - (mark + gap + width) // 2
            self.icon(icon, pygame.Rect(left, inside.y, mark, inside.h), ink, size + 1)
            self.text(label, pygame.Rect(left + mark + gap, inside.y, width, inside.h), size, ink, True)
            return
        height, below = self.line_height(size, True), self.line_height(SMALL)
        if sub and height + below <= inside.h:
            top = inside.centery - (height + below) // 2
            self.text(label, pygame.Rect(inside.x, top, inside.w, height), size, ink, True, "center")
            self.text(sub, pygame.Rect(inside.x, top + height, inside.w, below), SMALL,
                      MUTED if ink == TEXT else ink, align="center")
        else:
            self.text(label, inside, size, ink, True, "center")

    def face(self, rect, style, label="", icon=None, sub="", size=LABEL, hover=False, lit=False):
        """What a button looks like, without being one."""
        fill, over, border, ink = STYLES[style]
        fill = LINE if lit and style in ("normal", "tab") else over if hover or lit else fill
        self.sprite(rect, ("button", label, icon, sub, size, fill, border, ink),
                    lambda r: self._paint_button(r, label, icon, sub, size, fill, border, ink))

    def button(self, rect, label="", wid=None, style="normal", icon=None, sub="", size=LABEL, enabled=True):
        """True when it is pressed."""
        wid = wid or label
        focused, click, hover = self._widget(wid, rect) if enabled else (False, None, False)
        pressed = bool(click) or self._entered(focused)
        if pressed:
            self.flash(wid)
        self.face(rect, style if enabled else "disabled", label, icon, sub, size, hover,
                  self._flash.get(wid, 0) > self.now)
        return pressed

    def stop_button(self, rect, sub="", size=LABEL):
        """STOP: also works while a menu is open."""
        pressed = self.button(rect, "STOP", "stop", "danger", sub=sub, size=size)
        if self.menu and self.click and rect.collidepoint(self.click) and not self.menu.box.collidepoint(self.click):
            self.click = None
            self.close_menu()
            return True
        return pressed

    def toggle(self, rect, on, wid, enabled=True):
        return self.button(rect, "ON" if on else "OFF", wid, "good" if on else "normal", enabled=enabled)

    def field(self, rect, text, wid, enabled=True):
        """Text that opens an editor. True when it is pressed."""
        focused, click, hover = self._widget(wid, rect, "Enter edit") if enabled else (False, None, False)
        self.box(rect, BACKGROUND, 6, ACCENT if hover else LINE)
        self.text(text, inset(rect, self.px(10), 0), LABEL, TEXT if enabled else MUTED, True)
        return bool(click) or self._entered(focused)

    # ------------------------------------------------------------ values
    def _ends(self, rect, names, enabled, active):
        """The two small buttons around a value."""
        end = Px(min(max(self.px(26), round(rect.h * 1.05)), rect.w // 4))
        low, middle, high = columns(rect, end, 1, end, gap=self.px(3))
        for part, icon in ((low, names[0]), (high, names[1])):
            hover = enabled and self.live and part.collidepoint(self.mouse)
            self.face(part, "normal" if enabled else "disabled", icon=icon, size=15, hover=hover, lit=active == icon)
        return low, middle, high

    def _step(self, rect, wid, names, enabled, hint):
        """Shared by stepper, number and choice: the buttons, the keys and the wheel.
        Returns (steps, middle, focused, click in the middle)."""
        focused, click, hover = self._widget(wid, rect, hint) if enabled else (False, None, False)
        active = self._grab[1] if self._grab and self._grab[0] == wid else None
        low, middle, high = self._ends(rect, names, enabled, active)
        steps = 0
        if click:
            for part, name, step in ((low, names[0], -1), (high, names[1], 1)):
                if part.collidepoint(click):
                    self._grab, self._repeat_at, steps = (wid, name), self.now + REPEAT_AFTER, step
        elif active in names and self._held_again():
            steps = -1 if active == names[0] else 1
        steps += self._moved(focused, "right", "more") - self._moved(focused, "left", "less")
        if hover and self.wheel:
            steps, self.wheel = steps + self.wheel, 0
        return steps, middle, focused, click if click and middle.collidepoint(click) else None

    def stepper(self, rect, text, wid, sub="", size=LABEL, enabled=True):
        """[-] text [+]. Returns the steps: -1, 0 or 1."""
        steps, middle, _, _ = self._step(rect, wid, ("minus", "plus"), enabled, "← → change")
        ink = TEXT if enabled else FAINT
        if sub:
            height, below = self.line_height(size, True), self.line_height(SMALL)
            top = middle.centery - (height + below) // 2
            self.text(text, pygame.Rect(middle.x, top, middle.w, height), size, ink, True, "center")
            self.text(sub, pygame.Rect(middle.x, top + height, middle.w, below), SMALL, MUTED, align="center")
        else:
            self.text(text, middle, size, ink, True, "center")
        return steps

    def number(self, rect, value, low, high, step, show, wid, enabled=True):
        """[-] value [+] that can also be dragged. Returns the new value, or None."""
        value = self._latest(wid, value)
        steps, middle, _, click = self._step(rect, wid, ("minus", "plus"), enabled, "← → change")
        new = value + steps * step
        if click:
            self._grab = (wid, "drag")
        if self._grab == (wid, "drag"):
            share = (self.mouse[0] - middle.x) / max(1, middle.w)
            new = low + round(share * (high - low) / step) * step
        new = round(max(low, min(high, new)), 4)
        if new == round(value, 4):
            new = None
        else:
            value = self._choose(wid, new)
        self.box(middle, BACKGROUND, 5, LINE if enabled else TRACK)
        thin = self.px(4 if middle.h >= self.px(26) else 2)
        groove = pygame.Rect(middle.x + self.px(3), middle.bottom - thin - self.px(2), middle.w - 2 * self.px(3), thin)
        zero = -low / (high - low) if low < 0 < high else 0.0
        self.meter(groove, (value - low) / (high - low), ACCENT if enabled else LINE, zero)
        self.text(show(value), inset(middle, self.px(4), 0).move(0, -thin // 2), BODY, TEXT if enabled else FAINT,
                  True, "center")
        return new

    def choice(self, rect, value, options, wid, color=TEXT, enabled=True):
        """< value >, or click it for the whole list. options: [(value, label)]. Returns the new value, or None."""
        value = self._latest(wid, value)
        steps, middle, focused, click = self._step(rect, wid, ("left", "right"), enabled, "← → change · Enter list")
        values = [v for v, _ in options]
        index = values.index(value) if value in values else 0
        new = values[(index + steps) % len(values)] if steps else None
        if click or self._entered(focused):
            self.open_menu(wid, rect, options, index)
        if new is None:
            new = self.answer(wid)
        if new is not None:
            value = self._choose(wid, new)
        self.box(middle, BACKGROUND, 5, LINE if enabled else TRACK)
        self.text(dict(options).get(value, str(value)), inset(middle, self.px(5), 0), BODY,
                  color if enabled else FAINT, True, "center")
        return new

    # ------------------------------------------------------------ lists and menus
    def open_menu(self, wid, anchor, options, index):
        self.menu = Menu(wid, anchor.copy(), options, index, self.focus)

    def answer(self, wid):
        """What was picked in the menu opened for this widget (once), or None."""
        if self.live and self._answer and self._answer[0] == wid:
            value, self._answer = self._answer[1], None
            return value
        return None

    def close_menu(self, value=None):
        menu, self.menu = self.menu, None
        self.focus = menu.focus
        if value is not None:
            self._answer = (menu.wid, value)

    def _draw_menu(self):
        menu, screen = self.menu, inset(self.area, self.px(6))
        outside = self.click
        self.layer()
        self.live = True
        row, pad = self.px(32), self.px(5)
        width = min(screen.w, max(menu.anchor.w, self.px(210)))
        box = pygame.Rect(0, 0, width, min(screen.h, row * len(menu.options) + 2 * pad))
        box.midtop = menu.anchor.midbottom
        if box.bottom > screen.bottom:
            box.bottom = menu.anchor.top
        box.clamp_ip(screen)
        menu.box = box
        self.box(inset(box, -self.px(1)), ACCENT, 9)
        self.box(box, CARD, 8)
        self.focus = ("menu", menu.wid)
        menu.index, picked = self.listbox(inset(box, pad), [(label, "") for _, label in menu.options], menu.index,
                                          ("menu", menu.wid))
        if picked:
            self.close_menu(menu.options[menu.index][0])
        elif outside and not box.collidepoint(outside):
            self.click = None
            self.close_menu()

    def listbox(self, rect, items, index, wid, row=32):
        """A list of (label, tag). Returns (selected index, picked by click or Enter)."""
        focused, click, hover = self._widget(wid, rect, "↑ ↓ choose · Enter select")
        height = self.px(row)
        shown = max(1, rect.h // height)
        last = max(0, len(items) - shown)
        before = index
        if self._moved(focused and index < len(items) - 1, "down"):
            index += 1
        elif self._moved(focused and index > 0, "up"):
            index -= 1
        top = self._scroll.get(wid, (0, None))[0]
        if hover and self.wheel:
            top, self.wheel = top - self.wheel, 0
        if index != before or self._scroll.get(wid, (0, None))[1] != (index, len(items), shown):
            top = max(min(top, index), index - shown + 1)        # keep the selected one in sight
        rail = pygame.Rect(rect.right - self.px(10), rect.y, self.px(10), rect.h)
        if click and last and rail.collidepoint(click):
            self._grab, click = (wid, "rail"), None
        if self._grab == (wid, "rail"):
            top = round((self.mouse[1] - rect.y) / max(1, rect.h) * last)
        top = max(0, min(last, top))
        picked = self._entered(focused)
        width = rect.w - (rail.w + self.px(2) if last else 0)
        for i in range(top, min(len(items), top + shown)):
            line = pygame.Rect(rect.x, rect.y + (i - top) * height, width, height - self.px(2))
            if click and line.collidepoint(click):
                index, picked = i, True
            over = self.live and line.collidepoint(self.mouse)
            if i == index or over:
                self.box(line, ACCENT_DARK if i == index else RAISED, 6)
            label, tag = items[i]
            inside = inset(line, self.px(9), 0)
            tag_width = self.text_width(tag, SMALL, True) if tag else Px(0)
            self.text(label, pygame.Rect(inside.x, inside.y, inside.w - tag_width - self.px(4), inside.h), BODY,
                      TEXT, True)
            self.text(tag, inside, SMALL, WHITE if i == index else MUTED, True, "right")
        if last:
            size = max(self.px(20), rail.h * shown // len(items))
            self.box(inset(rail, self.px(2), 0), TRACK, 4)
            self.box(pygame.Rect(rail.x + self.px(2), rail.y + (rail.h - size) * top // last, rail.w - 2 * self.px(2),
                                 size), MUTED, 4)
        self._scroll[wid] = (top, (index, len(items), shown))
        return index, picked

    # ------------------------------------------------------------ furniture
    def card(self, rect, title=""):
        """A panel. Returns the room inside, below the title."""
        self.box(rect, CARD, 10)
        inside = inset(rect, self.px(12), self.px(9))
        if title:
            height = self.line_height(SMALL, True)
            self.text(title, pygame.Rect(inside.x, inside.y, inside.w, height), SMALL, MUTED, True)
            inside = pygame.Rect(inside.x, inside.y + height + self.px(5), inside.w,
                                 inside.h - height - self.px(5))
        return inside
