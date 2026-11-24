"""The window, the main loop, and what all pages have in common."""
import os
import sys
import time
from collections import deque

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS", "0")
os.environ.setdefault("SDL_RENDER_VSYNC", "0")                  # frame rate not tied to the screen
os.environ.setdefault("SDL_VIDEO_X11_WMCLASS", "rov-backup")
os.environ.setdefault("SDL_VIDEO_WAYLAND_WMCLASS", "rov-backup")

import pygame

import rov as core

from . import screen
from .camera import Camera
from .dialogs import HelpDialog
from .keyboard import KeyboardDrive
from .keynames import key_name
from .layout import Px, columns, inset, rows
from .link import RovLink
from .pages.autopilot import MODES, AutopilotPage
from .pages.camera import CameraPage
from .pages.drive import DrivePage
from .pages.mission import MissionPage
from .pages.motors import MotorsPage
from .pages.profiles import ProfilesPage
from .pages.system import SystemPage
from .theme import (AMBER, BACKGROUND, BLACK, BODY, CARD, FAINT, GAP, GREEN, LABEL, MUTED, RED, RED_DARK, SMALL, TEXT,
                    TITLE, WHITE)
from .ui import ARROWS, UI
from .vision_worker import VisionWorker

try:
    import updater
except ImportError:
    updater = None

TABS = ("drive", "camera", "autopilot", "profiles", "motors", "system")          # F1 - F6
NICE = 5                 # let the motor program go first
SLOW = 0.5               # seconds between update checks
MOUSE_JITTER = 3         # pixels: less isn't the pilot moving the mouse
NEWS = 4.0               # seconds a message from the ROV program is shown before the hint how to arm


class App:
    def __init__(self, fullscreen=True, size=None, margins=None, fps=0, show_fps=False, page="drive"):
        pygame.display.init()
        pygame.font.init()
        pygame.display.set_caption("ROV backup (Arduino Mega)")
        pygame.key.set_repeat(350, 45)
        self.window_size, self.margins, self.fps, self.show_fps = size or screen.window_size(), margins, fps, show_fps
        self.saved_edge = screen.load_edge()
        self.set_fullscreen(fullscreen)
        self.clock = pygame.time.Clock()
        self.ui = UI()
        self.link = RovLink()
        self.keyboard = KeyboardDrive(self.link)
        self.camera = Camera()
        self.vision = VisionWorker(self.link, self.camera)
        self.updater = updater.Updater() if updater else None
        self.pages = {p.name: p(self) for p in (DrivePage, CameraPage, AutopilotPage, ProfilesPage, MotorsPage,
                                                SystemPage, MissionPage)}
        self.page = self.pages["drive"]
        self.dialogs = []
        self.events = deque()
        self.down = set()                  # keys held down
        self.running, self.focused = True, True
        self._notice, self._notice_until = ("", None), 0.0
        self._news, self._news_until = "", 0.0
        self._view_toggle = None
        self._slow_at = self._restart_at = 0.0
        self.show(page)

    # ------------------------------------------------------------ window
    def set_fullscreen(self, on):
        self.fullscreen = on
        if on:
            pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            pygame.display.set_mode(self.window_size, pygame.RESIZABLE)

    @property
    def edge(self):
        """Percent of the screen that stays empty on each side."""
        if not self.fullscreen:
            return 0
        size = pygame.display.get_surface().get_size()
        return screen.default_edge(size) if self.saved_edge is None else self.saved_edge

    def set_edge(self, edge):
        self.saved_edge = edge
        screen.save_edge(edge)

    def _safe_area(self, surface):
        size = width, height = surface.get_size()
        if not self.margins:
            return screen.visible(size, self.edge)
        left, top, right, bottom = self.margins
        return pygame.Rect(left, top, width - left - right, height - top - bottom)

    def run(self):
        os.setpriority(os.PRIO_PROCESS, 0, max(NICE, os.getpriority(os.PRIO_PROCESS, 0)))
        sys.setswitchinterval(0.001)       # the camera and autopilot threads get their turn sooner
        self.vision.start()
        try:
            while self.running:
                self.frame()
                pygame.display.flip()
                if not pygame.display.get_active():
                    time.sleep(0.02)       # minimised: nobody is looking
        finally:
            self.camera.stop()
            pygame.quit()

    def quit(self):
        self.running = False

    def frame(self):
        dt = self.clock.tick(self.fps) / 1000        # 0 = as fast as it can
        self._handle_events()
        self._update(time.monotonic())
        self._draw(min(dt, 0.1))

    # ------------------------------------------------------------ state
    @property
    def status(self):
        return self.link.status

    def show(self, name):
        self.page = self.pages[name]
        self.ui.focus = None
        if self.page.drives:
            self.ui.ring = False           # ready to drive
        self.page.opened()

    def open_dialog(self, dialog):
        self.dialogs.append(dialog)
        self.ui.focus = None               # the dialog's first button, never one that looks like the one just used
        self.keyboard.release_all()
        pygame.key.start_text_input()

    def close_dialog(self, dialog):
        if dialog in self.dialogs:
            self.dialogs.remove(dialog)
            self.ui.focus = dialog.focus_before
        if not self.dialogs:
            pygame.key.stop_text_input()

    def notice(self, text, color=None, seconds=5):
        self._notice, self._notice_until = (text, color), time.monotonic() + seconds

    def stop(self):
        self.link.stop()
        self.keyboard.release_all()
        self.ui.flash("stop")

    def autopilot(self, mode):
        self.link.send("ap_off" if self.status.ap_mode == mode else f"ap:{mode}")

    def keys_drive(self):
        """Do the keys drive right now, or do they work the screen?"""
        return (self.page.drives and not self.ui.ring and not self.dialogs and not self.ui.menu
                and not self.page.captures_keys())

    def _update(self, now):
        self.link.poll()
        self.keyboard.update(now, self.focused, self.keys_drive())
        if self.status.msg != self._news:
            self._news, self._news_until = self.status.msg, now + NEWS
        toggle = self.status.view_toggle                 # SQUARE on the controller
        if toggle is not None:
            if self._view_toggle not in (None, toggle) and not self.dialogs:
                self.toggle_camera()
            self._view_toggle = toggle
        self.camera.update(now, self.page.name == "camera" or self.status.ap_mode in core.VISION_MODES)
        if now >= self._slow_at:
            self._slow_at = now + SLOW
            self._check_updater(now)

    def _check_updater(self, now):
        if not self.updater:
            return
        self.updater.tick()
        if self.updater.state == "done":
            if not self._restart_at:
                self._restart_at = now + (8 if self.updater.notes else 1.5)
            elif now >= self._restart_at:
                self.camera.stop()
                pygame.quit()
                updater.restart_after_update(sys.argv)

    def toggle_camera(self):
        self.show("drive" if self.page.name == "camera" else "camera")

    # ------------------------------------------------------------ events
    def _handle_events(self):
        """Stop at each click or key the pages must react to, so every one is seen against a fresh screen."""
        self.events.extend(pygame.event.get())
        while self.events:
            if self._handle(self.events.popleft()):
                break

    def _handle(self, event):
        ui = self.ui
        if event.type == pygame.QUIT:
            self.quit()
        elif event.type == pygame.WINDOWFOCUSLOST:
            self.focused = False
            self.down.clear()
        elif event.type == pygame.WINDOWFOCUSGAINED:
            self.focused = True
        elif event.type == pygame.MOUSEMOTION:
            if ui.mouse != (-1, -1) or abs(event.rel[0]) + abs(event.rel[1]) >= MOUSE_JITTER:
                self._use_mouse(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._use_mouse(event.pos)
            ui.click, ui.held = event.pos, True
            return True
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            ui.held = False
        elif event.type == pygame.MOUSEWHEEL:
            ui.wheel = event.y
            return True
        elif event.type == pygame.TEXTINPUT:
            if self.dialogs:
                self.dialogs[-1].type(event.text)
        elif event.type == pygame.KEYDOWN:
            self.focused = True
            key = key_name(event.key)
            repeat = key in self.down
            self.down.add(key)
            self._key(key, event, repeat)
            return True
        elif event.type == pygame.KEYUP:
            self._key_up(key_name(event.key))
        return False

    def _use_mouse(self, position):
        self.ui.mouse = position
        self.ui.ring = False
        pygame.mouse.set_visible(True)

    def _key_up(self, key):
        self.down.discard(key)
        self.keyboard.release(key)
        if key in ("return", "kp_enter") and self.keyboard.release_enter() and not self.status.armed:
            self.notice("Hold Enter to arm", AMBER, 2)

    def _key(self, key, event, repeat):
        if key == "escape":
            if not repeat:
                self._escape()
        elif key in ("f11", "f12"):
            if not repeat:
                self._window_key(key)
        elif self.dialogs and self.dialogs[-1].key(key, event, repeat):
            pass
        elif key == "backspace" and not (self.keys_drive() and self.keyboard.in_use and self.keyboard.uses(key)):
            if not repeat:
                self._back()
        elif self.dialogs or self.ui.menu:
            self._work_screen(key, event, repeat)
        elif key == "f10":
            if not repeat:
                self.open_dialog(HelpDialog(self))
        elif key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= len(TABS):
            if not repeat:
                self._page_key(TABS[int(key[1:]) - 1])
        elif self.page.key(key, repeat):
            pass
        elif self.page.drives:
            self._drive_key(key, event, repeat)
        else:
            self._work_screen(key, event, repeat)

    def _window_key(self, key):
        if key == "f11":
            self.set_fullscreen(not self.fullscreen)
        else:
            self.show_fps = not self.show_fps

    def _page_key(self, name):
        if name == "camera" and self.page.name == "camera":
            self.page.toggle_full()          # F2 again
            return
        self.show(name)
        self.ui.ring = not self.page.drives
        self._hide_mouse()

    def _drive_key(self, key, event, repeat):
        """A key on DRIVE or CAMERA. What the keyboard profile uses goes first; arrows, Tab and Enter work the
        screen while the focus ring shows."""
        ui, keyboard = self.ui, self.keyboard
        works_screen = ui.ring and key in (*ARROWS, "tab", "return", "kp_enter")
        if keyboard.in_use and keyboard.uses(key) and not works_screen:
            if not repeat:
                ui.ring = False
                keyboard.press(key)
        elif keyboard.in_use and key in ("return", "kp_enter") and not ui.ring:
            if not repeat:
                keyboard.hold_enter(time.monotonic())
        elif key in (*ARROWS, "tab", "return", "kp_enter"):
            self._work_screen(key, event, repeat)
        elif not repeat:
            self._shortcut(key)

    def _shortcut(self, key):
        if key == "space":
            self.stop()
        elif key == "c":
            self.toggle_camera()
        elif key == "p":
            self.link.send("next_profile:keyboard")
        elif key in ("plus", "equal", "kp_add"):
            self.link.send("speed+")
        elif key in ("minus", "kp_subtract"):
            self.link.send("speed-")
        elif key == "0":
            self.link.send("ap_off")
        elif key.isdigit() and 1 <= int(key) <= len(MODES):
            self.autopilot(MODES[int(key) - 1][0])

    def _work_screen(self, key, event, repeat):
        if self.ui.key(key, bool(event.mod & pygame.KMOD_SHIFT), repeat):
            self._hide_mouse()

    def _hide_mouse(self):
        self.ui.mouse = (-1, -1)
        pygame.mouse.set_visible(False)

    def _escape(self):
        """STOP, always. If the ROV was already safe it also goes back."""
        safe = not self.status.armed
        self.stop()
        if safe or self.dialogs or self.ui.menu:
            self._back()

    def _back(self):
        ui = self.ui
        if ui.menu:
            ui.close_menu()
        elif self.dialogs:
            self.dialogs[-1].close()
        elif self.page.back():
            pass
        elif ui.ring and self.page.drives:
            ui.ring = False
        elif self.page.name != "drive":
            self.show("drive")

    # ------------------------------------------------------------ drawing
    def _draw(self, dt):
        ui = self.ui
        surface = pygame.display.get_surface()
        area = self._safe_area(surface)
        ui.begin(surface, area, dt)
        ui.live = ui.live and not self.dialogs
        surface.fill(BLACK if self.page.name == "camera" and self.page.full else BACKGROUND)
        self._draw_page(inset(area, ui.px(8)))
        if area.size != surface.get_size():
            pygame.draw.rect(surface, FAINT, area, 1)       # the edge of what the monitor should show
        if self.dialogs:
            ui.shade(surface.get_rect(), BLACK, 150)
            ui.layer()
            self.dialogs[-1].draw(area)
        ui.end()
        if self.show_fps:
            self._draw_fps(area)

    def _draw_page(self, area):
        ui, page = self.ui, self.page
        if not page.chrome:
            page.draw(area)
            return
        top, body, footer = rows(area, ui.px(92 if ui.portrait else 46), 1, ui.px(page.footer), gap=ui.px(GAP))
        self._draw_tabs(top)
        ui.first = len(ui.widgets)
        page.draw(body)
        page.draw_footer(footer)

    def _draw_tabs(self, top):
        ui = self.ui
        if ui.portrait:
            pill, tabs = rows(top, 1, 1, gap=ui.px(6))
        else:
            pill, tabs = columns(top, ui.px(128), 1, gap=ui.px(GAP))
        self.draw_state(pill)
        ui.box(tabs, CARD, 10)
        here = "autopilot" if self.page.name == "mission" else self.page.name
        for i, (cell, name) in enumerate(zip(columns(inset(tabs, ui.px(4)), *[1] * len(TABS), gap=ui.px(3)), TABS)):
            if ui.button(cell, self.pages[name].title, ("tab", name), "selected" if name == here else "tab",
                         sub=f"F{i + 1}", size=BODY):
                self.show(name)

    def draw_state(self, rect):
        ui, status = self.ui, self.status
        word, style = ("OFFLINE", "warn") if not status.online else ("ARMED", "good") if status.armed else \
            ("SAFE", "normal")
        ui.face(rect, style, word, size=TITLE)
        arming = self.keyboard.arming(ui.now)
        if arming:
            ui.fill(pygame.Rect(rect.x, rect.bottom - ui.px(7), round(rect.w * arming), ui.px(7)), GREEN)

    def draw_footer(self, area):
        ui = self.ui
        stop, strip = columns(area, ui.px(150), 1, gap=ui.px(GAP))
        self.draw_message(strip)
        if ui.stop_button(stop, size=TITLE):
            self.stop()

    def draw_message(self, rect):
        ui = self.ui
        banner, fill = self.autopilot_banner()
        ui.box(rect, fill or CARD, 8)
        inside = inset(rect, ui.px(12), 0)
        if ui.ring and ui.hint and not banner:
            hint = f"{ui.hint} · Backspace back"
            width = min(ui.text_width(hint, SMALL), inside.w // 2)
            inside, right = columns(inside, 1, Px(width), gap=ui.px(10))
            ui.text(hint, right, SMALL, MUTED, align="right")
        elif self.updater and self.updater.ready and not banner:
            inside, chip = columns(inside, 1, ui.px(130), gap=ui.px(10))
            if ui.button(inset(chip, 0, ui.px(4)), "UPDATE READY", "chip", "selected", size=SMALL):
                self.show("system")
        text, color = (banner, WHITE) if banner else self.message()
        ui.text(text, inside, LABEL, color or TEXT, bool(banner))

    def autopilot_banner(self):
        """(text, colour) while the autopilot is on: green = working, flashing red = take control."""
        status = self.status
        if status.ap_alert:
            return "⚠ " + status.ap_alert, RED if int(time.monotonic() * 3) % 2 == 0 else RED_DARK
        if status.ap_enabled and status.ap_mode:
            return f"✓ AUTOPILOT {status.ap_mode.upper()}: {status.ap_info}", GREEN
        return "", None

    def message(self):
        """(text, colour): what the pilot should know now."""
        status = self.status
        if time.monotonic() < self._notice_until:
            return self._notice
        if not status.online:
            return "Waiting for the ROV program…  (start the app with run.sh)", AMBER
        if not status.output_ok:
            return "⚠ " + status.output, AMBER
        if status.armed or time.monotonic() < self._news_until:
            return status.msg, None
        if not status.switch_on:
            return "Panel ARM switch is OFF", AMBER
        if status.keyboard:
            return ("Backspace, then hold Enter to arm" if self.ui.ring and self.page.drives else
                    "Hold Enter to arm" if self.page.drives else "To arm: F1 (DRIVE), then hold Enter"), None
        if status.controller:
            return "Press OPTIONS on the controller to arm", None
        return "No controller: press its PS button, or P to drive with the keyboard", AMBER

    def _draw_fps(self, area):
        ui = self.ui
        box = pygame.Rect(0, 0, ui.px(74), ui.px(20))
        box.topright = area.topright
        ui.fill(box, BLACK)
        ui.text(f"{self.clock.get_fps():.0f} fps", box, SMALL, GREEN, True, "center")
