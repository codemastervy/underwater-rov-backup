"""Windows on top of a page. Each one draws itself and calls app.close_dialog() when done."""
import ctypes
import time

import pygame

import rov as core

from .keynames import KEYBOARD, key_label
from .layout import centred, columns, inset, rows
from .theme import ACCENT, BACKGROUND, BODY, CARD, GAP, LABEL, LINE, MUTED, TEXT, TITLE

IGNORE_CLICKS = 0.25       # seconds: so a double click doesn't also press something in the dialog


def clipboard_text():
    """The clipboard, from the SDL library pygame has loaded."""
    try:
        with open("/proc/self/maps") as maps:
            path = next(line.split(None, 5)[5].strip() for line in maps if "libSDL2-2" in line)
        sdl = ctypes.CDLL(path)
        sdl.SDL_GetClipboardText.restype = ctypes.c_void_p
        sdl.SDL_free.argtypes = [ctypes.c_void_p]
        text = sdl.SDL_GetClipboardText()
        result = ctypes.string_at(text).decode(errors="replace") if text else ""
        sdl.SDL_free(text)
        return result
    except (OSError, StopIteration, IndexError, AttributeError):
        return ""


class Dialog:
    size = (460, 220)

    def __init__(self, app, title):
        self.app, self.ui, self.title = app, app.ui, title
        self.focus_before = app.ui.focus
        self.opened = time.monotonic()

    def key(self, key, event, repeat):
        """True if the dialog used the key."""
        return False

    def type(self, text):
        pass

    def close(self):
        self.app.close_dialog(self)

    def draw(self, screen):
        ui = self.ui
        if time.monotonic() - self.opened < IGNORE_CLICKS:
            ui.click = None
        box = centred(inset(screen, ui.px(6)), ui.px(self.size[0]), ui.px(self.size[1]))
        ui.box(inset(box, -ui.px(1)), LINE, 13)
        ui.box(box, CARD, 12)
        inside = inset(box, ui.px(16), ui.px(12))
        title, body = rows(inside, ui.px(30), 1, gap=ui.px(6))
        title, stop = columns(title, 1, ui.px(90), gap=ui.px(GAP))
        ui.text(self.title, title, TITLE, TEXT, True)
        self.draw_body(body)
        if ui.stop_button(stop):
            self.close()
            self.app.stop()


class ConfirmDialog(Dialog):
    def __init__(self, app, title, text, action, on_yes, style="danger"):
        super().__init__(app, title)
        self.text, self.action, self.on_yes, self.style = text, action, on_yes, style

    def draw_body(self, body):
        ui = self.ui
        text, buttons = rows(body, 1, ui.px(52), gap=ui.px(GAP))
        ui.paragraph(self.text, text, LABEL)
        no, yes = columns(buttons, 1, 1, gap=ui.px(GAP))
        if ui.button(no, "CANCEL"):              # first, so it has the focus
            self.close()
        if ui.button(yes, self.action, style=self.style):
            self.close()
            self.on_yes()


class TextDialog(Dialog):
    """Type with the keyboard, or click the letters with the mouse."""
    size = (780, 470)
    LETTERS = ("1234567890", "qwertyuiop", "asdfghjkl#", "zxcvbnm,.:", "=-_+()/&'!")

    def __init__(self, app, title, text, on_ok, limit):
        super().__init__(app, title)
        self.text, self.on_ok, self.limit = text, on_ok, limit
        self.caret = len(text)
        self.capital = False

    def type(self, text):
        text = "".join(ch for ch in text.replace("\n", " ") if ch.isprintable())[:self.limit - len(self.text)]
        self.text = self.text[:self.caret] + text + self.text[self.caret:]
        self.caret += len(text)

    def erase(self):
        if self.caret:
            self.text = self.text[:self.caret - 1] + self.text[self.caret:]
            self.caret -= 1

    def accept(self):
        self.close()
        self.on_ok(self.text.strip())

    def key(self, key, event, repeat):
        if event.mod & pygame.KMOD_CTRL:
            if key == "v":
                self.type(clipboard_text())
            elif key in ("u", "backspace"):
                self.text, self.caret = self.text[self.caret:], 0
        elif key == "backspace":
            self.erase()
        elif key == "delete":
            self.text = self.text[:self.caret] + self.text[self.caret + 1:]
        elif key in ("left", "right"):
            self.caret = max(0, min(len(self.text), self.caret + (1 if key == "right" else -1)))
        elif key in ("home", "end"):
            self.caret = 0 if key == "home" else len(self.text)
        elif key in ("return", "kp_enter") and not repeat:
            self.accept()
        return True

    def draw_body(self, body):
        ui = self.ui
        field, *lines, bottom = rows(body, ui.px(44), 1, 1, 1, 1, 1, ui.px(48), gap=ui.px(5))
        self._draw_field(field)
        for line, letters in zip(lines, self.LETTERS):
            for key, letter in zip(columns(line, *[1] * len(letters), gap=ui.px(5)), letters):
                letter = letter.upper() if self.capital else letter
                if ui.button(key, letter, ("letter", letter)):
                    self.type(letter)
        no, shift, space, paste, erase, yes = columns(bottom, 1.4, 0.8, 2, 1.1, 0.8, 1.4, gap=ui.px(5))
        if ui.button(no, "CANCEL"):
            self.close()
        if ui.button(shift, icon="shift", wid="shift", style="selected" if self.capital else "normal", size=18):
            self.capital = not self.capital
        if ui.button(space, "SPACE"):
            self.type(" ")
        if ui.button(paste, "PASTE"):
            self.type(clipboard_text())
        if ui.button(erase, icon="erase", wid="erase", size=20):
            self.erase()
        if ui.button(yes, "OK", style="good"):
            self.accept()

    def _draw_field(self, rect):
        ui = self.ui
        ui.box(rect, BACKGROUND, 6, ACCENT)
        inside = inset(rect, ui.px(10), ui.px(4))
        font = ui.font(LABEL, True)
        before = font.size(self.text[:self.caret])[0]
        shift = max(0, before - inside.w + ui.px(3))         # keep the caret in sight
        clip = ui.surface.get_clip()
        ui.surface.set_clip(inside)
        picture = font.render(self.text, True, TEXT)
        ui.surface.blit(picture, picture.get_rect(left=inside.x - shift, centery=inside.centery))
        if int(ui.now * 2) % 2 == 0:
            ui.fill(inset(pygame.Rect(inside.x - shift + before, inside.y, ui.px(2), inside.h), 0, ui.px(4)), ACCENT)
        ui.surface.set_clip(clip)


class KeyDialog(Dialog):
    """Pick a key for an action: press it, or click it."""
    size = (790, 330)

    def __init__(self, app, action, used, on_key):
        super().__init__(app, f"{action.upper()}: press a key, or click one")
        self.used, self.on_key = used, on_key

    def key(self, key, event, repeat):
        if not repeat and key not in core.FIXED_KEYS:
            self.pick(key)
        return True

    def pick(self, key):
        self.close()
        self.on_key(key)

    def draw_body(self, body):
        ui = self.ui
        *lines, bottom = rows(body, *[1] * len(KEYBOARD), ui.px(40), gap=ui.px(4))
        for line, keys in zip(lines, KEYBOARD):
            for rect, (key, _) in zip(columns(line, *[width for _, width in keys], gap=ui.px(4)), keys):
                if ui.button(rect, key_label(key), ("key", key), "selected" if key in self.used else "normal",
                             size=BODY, enabled=key not in core.FIXED_KEYS):
                    self.pick(key)
        note, no = columns(bottom, 1, ui.px(120), gap=ui.px(GAP))
        ui.text("Blue keys are in use: picking one swaps the two.", note, BODY, MUTED)
        if ui.button(no, "CANCEL"):
            self.close()


class HelpDialog(Dialog):
    size = (780, 450)
    LEFT = (("Esc", "STOP (when safe: also back)"), ("F1 – F6", "open a page"), ("Arrows, Tab", "move around"),
            ("Enter", "select"), ("+  −", "change a value"), ("Backspace", "back"), ("F10", "this list"),
            ("F11", "full screen / window"), ("F12", "show frame rate"))
    RIGHT = (("Hold Enter", "arm (keyboard profile)"), ("P", "next keyboard profile"), ("+  −", "speed (or the value in the ring)"),
             ("C", "camera on / off"), ("F2 again", "full-screen video"), ("1 – 5", "autopilot modes"),
             ("0", "autopilot mode off"), ("Space", "STOP (if the profile doesn't use it)"),
             ("Tab", "work the screen instead of driving"))

    def __init__(self, app):
        super().__init__(app, "KEYS")

    def draw_body(self, body):
        ui = self.ui
        lists, bottom = rows(body, 1, ui.px(44), gap=ui.px(GAP))
        for side, title, keys in zip(columns(lists, 1, 1, gap=ui.px(16)), ("EVERYWHERE", "DRIVE AND CAMERA"),
                                     (self.LEFT, self.RIGHT)):
            head, *lines = rows(side, ui.px(20), *[1] * len(keys))
            ui.text(title, head, BODY, MUTED, True)
            for line, (key, what) in zip(lines, keys):
                name, text = columns(line, 1, 2, gap=ui.px(6))
                ui.text(key, name, BODY, ACCENT, True)
                ui.text(what, text, BODY)
        if ui.button(bottom, "CLOSE"):
            self.close()
