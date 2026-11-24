"""CAMERA: the video, with everything needed to drive beside it."""
import colorsys
import time

import pygame

import rov as core

from ..camera import FLIP_TEXT, HEIGHT, PICTURE, WIDTH
from ..layout import columns, fit, grid, inset, rows
from ..theme import (ACCENT, AMBER, BLACK, BODY, CARD, FAINT, GAP, GREEN, LABEL, LARGE, MUTED, RED, SMALL, TEXT, TITLE,
                     WHITE)
from . import Page

MODES = [("cruise", "CRUISE"), ("line", "LINE"), ("target", "TARGET"), ("heading", "HEADING"), ("mission", "MISSION")]
CROSSHAIR_SPEED = 0.45        # picture widths a second
MESSAGE_SECONDS = 5.0


class CameraPage(Page):
    name, title = "camera", "CAMERA"
    drives = True
    chrome = False

    def __init__(self, app):
        super().__init__(app)
        self.full = self.picture_open = self.picking = False
        self.crosshair = [0.5, 0.5]
        self.message, self.message_until = "", 0.0

    @property
    def camera(self):
        return self.app.camera

    # ------------------------------------------------------------ modes
    def opened(self):
        self.full = self.picture_open = self.picking = False

    def back(self):
        for mode in ("picking", "picture_open", "full"):
            if getattr(self, mode):
                setattr(self, mode, False)
                return True
        return False

    def toggle_full(self):
        self.full = not self.full
        self.picture_open = False

    def start_picking(self):
        self.picking, self.picture_open, self.crosshair = True, False, [0.5, 0.5]

    def captures_keys(self):
        return self.picking

    def key(self, key, repeat):
        if not self.picking or key in ("tab", "backspace"):
            return False
        if key in ("return", "kp_enter") and not repeat:
            self._pick()
        return True

    def _move_crosshair(self):
        step = CROSSHAIR_SPEED * self.ui.dt
        down = self.app.down
        for axis, less, more in ((0, "left", "right"), (1, "up", "down")):
            move = (more in down) - (less in down)
            self.crosshair[axis] = max(0.0, min(0.999, self.crosshair[axis] + move * step))

    def _pick(self):
        """The colour under the crosshair (average of a 7x7 patch) becomes the autopilot's target."""
        self.picking = False
        frame = self.camera.frame
        if not frame:
            self.app.notice("No camera picture to pick from", AMBER)
            return
        cx, cy = int(self.crosshair[0] * WIDTH), int(self.crosshair[1] * HEIGHT)
        pixels = [(y * WIDTH + x) * 3 for y in range(max(0, cy - 3), min(HEIGHT, cy + 4))
                  for x in range(max(0, cx - 3), min(WIDTH, cx + 4))]
        red, green, blue = (sum(frame.data[i + k] for i in pixels) / len(pixels) / 255 for k in range(3))
        hue, saturation, brightness = colorsys.rgb_to_hsv(red, green, blue)
        self.link.send(f"ap_set:target_pick:{hue * 360:.0f},{saturation:.2f},{brightness:.2f}")
        self.app.notice("Target colour picked", GREEN)

    # ------------------------------------------------------------ drawing
    def draw(self, area):
        ui = self.ui
        if self.full:
            video = fit(ui.area, WIDTH, HEIGHT)
            self._draw_video(video)
            self._draw_full_controls(area)
        else:
            if ui.portrait:
                video, side = rows(area, 1, 1.05, gap=ui.px(GAP))
            else:
                side, video = columns(area, ui.px(184), 1, gap=ui.px(GAP))
            video = fit(video, WIDTH, HEIGHT)
            self._draw_video(video)
            self._draw_side(side)
            if self.picture_open:
                self._draw_picture_panel(video)
        if self.picking:
            self._draw_picking(video)

    def _draw_video(self, video):
        ui, camera = self.ui, self.camera
        camera.view = video.size
        ui.fill(video, BLACK)
        frame = camera.frame
        if frame and frame.image:
            image = frame.image
            if image.get_size() != video.size:                  # just resized: until the next frame comes
                image = pygame.transform.scale(image, video.size)
            ui.surface.blit(image, video)
        else:
            ui.text(camera.error or "Starting camera…", inset(video, ui.px(16)), TITLE, MUTED, True, "center")
        self._draw_tracking(video)
        corner = pygame.Rect(video.x + ui.px(10), video.y + ui.px(6), video.w - ui.px(20), ui.px(22))
        ui.text(FLIP_TEXT[camera.flip], corner, BODY, AMBER, True, "right")
        self._draw_banner(video)
        if self.status.ap_enabled and not self.picking:
            self._draw_modes(video)

    def _draw_tracking(self, video):
        """What the autopilot sees."""
        ui, status = self.ui, self.status
        if not (status.ap_enabled and status.ap_mode):
            return
        result = self.app.vision.result if status.ap_mode in core.VISION_MODES else {}
        found = result.get("found") and result.get("mode") == status.ap_mode
        line = ui.px(3)
        if found and status.ap_mode == "line":
            points = [(video.x + x * video.w, video.y + y * video.h) for x, y in result["points"]]
            pygame.draw.lines(ui.surface, AMBER, False, points, line)
            for point in points:
                pygame.draw.circle(ui.surface, AMBER, point, ui.px(8), line)
        elif found and status.ap_mode == "target":
            center = video.x + (result["x"] + 1) / 2 * video.w, video.y + (result["y"] + 1) / 2 * video.h
            pygame.draw.circle(ui.surface, GREEN, center, max(ui.px(10), result["area"] ** 0.5 * video.w / 2), line)
        sure = found or status.ap_mode not in core.VISION_MODES
        where = pygame.Rect(video.x + ui.px(10), video.y + ui.px(6), video.w // 2, ui.px(22))
        ui.text(f"AUTO {status.ap_mode.upper()}" + ("" if sure else " - NOT SURE"), where, LABEL,
                GREEN if sure else RED, True)

    def _draw_banner(self, video):
        """Autopilot status, or the newest message for a few seconds."""
        ui = self.ui
        text, color = self.app.autopilot_banner()
        if not text:
            message = self.app.message()[0]
            if message != self.message:
                self.message, self.message_until = message, time.monotonic() + MESSAGE_SECONDS
            text = self.message if time.monotonic() < self.message_until else ""
        if text:
            strip = pygame.Rect(video.x, video.bottom - ui.px(32), video.w, ui.px(32))
            if color:
                ui.fill(strip, color)
            else:
                ui.shade(strip, BLACK, 150)
            ui.text(text, inset(strip, ui.px(10), 0), LABEL, WHITE, bool(color))

    def _draw_modes(self, video):
        """Autopilot modes, while the autopilot is switched on."""
        ui, status = self.ui, self.status
        strip = pygame.Rect(video.x + ui.px(6), video.bottom - ui.px(72), video.w - ui.px(12), ui.px(34))
        for i, (cell, (mode, label)) in enumerate(zip(columns(strip, *[1] * len(MODES), gap=ui.px(5)), MODES)):
            if ui.button(cell, f"{i + 1} {label}", ("mode", mode), "good" if status.ap_mode == mode else "normal",
                         size=SMALL):
                self.app.autopilot(mode)

    def _draw_side(self, side):
        ui, status = self.ui, self.status
        ui.card(side)
        inside = inset(side, ui.px(8))
        count = max(1, len(status.motors))
        if ui.portrait:
            top, middle, stop = rows(inside, ui.px(64), 1, ui.px(84), gap=ui.px(GAP))
            pill, info = columns(top, 1, 1, gap=ui.px(GAP))
            motors, buttons = columns(middle, 1, 1, gap=ui.px(12))
        else:
            pill, info, motors, buttons, stop = rows(inside, ui.px(42), ui.px(42), 1, ui.px(152), ui.px(80),
                                                     gap=ui.px(7))
        self.app.draw_state(pill)
        speed, name = columns(inset(info, ui.px(4), 0), 1, 1.5, gap=ui.px(6))
        ui.text(f"{status.speed}%" if status.online else "--", speed, LARGE, TEXT, True)
        ui.text(status.profile_name, name, BODY, MUTED, align="right")
        for i, (line, motor) in enumerate(zip(rows(motors, *[1] * count, gap=ui.px(3)), status.motors)):
            label, bar = rows(inset(line, ui.px(4), 0), 1.2, 1)
            value = ui.glide(("motor", i), motor["value"])
            ui.text(f"{motor['name'].replace('_', ' ')}", label, SMALL)
            ui.text(f"{motor['value'] * 100:+.0f}%", label, SMALL, MUTED, align="right")
            ui.thrust(pygame.Rect(bar.x, bar.y, bar.w, min(bar.h - ui.px(3), ui.px(10))), value,
                      (GREEN if value >= 0 else ACCENT) if status.armed else FAINT)
        self._draw_buttons(buttons)
        if ui.stop_button(stop, "Esc", LARGE):
            self.app.stop()

    def _draw_buttons(self, area):
        ui = self.ui
        buttons, cameras = rows(area, 3, 1, gap=ui.px(6))
        back, full, picture, pick, flip, page = grid(buttons, 2, 3, ui.px(6))
        if ui.button(back, "DRIVE", icon="back", size=BODY):
            self.app.show("drive")
        if ui.button(full, "FULL", icon="expand", size=BODY):
            self.toggle_full()
        if ui.button(picture, "PICTURE", style="selected" if self.picture_open else "normal", size=BODY):
            self.picture_open = not self.picture_open
        if ui.button(pick, "PICK", style="selected" if self.picking else "normal", size=BODY):
            if self.picking:
                self.picking = False
            else:
                self.start_picking()
        if ui.button(flip, "FLIP", size=BODY):
            self.camera.next_flip()
        if ui.button(page, "AUTO", size=BODY):
            self.app.show("autopilot")
        self._draw_camera_choice(cameras)

    def _draw_camera_choice(self, rect):
        """Which of the laptop's cameras to show. config.toml can force one."""
        camera = self.camera
        options = camera.cameras
        if camera.configured or not options:
            options = [(camera.device, f"{camera.configured} (config)" if camera.configured else "no camera")]
        new = self.ui.choice(rect, camera.device, options, "which camera", enabled=len(options) > 1)
        if new is not None:
            camera.pick(new)

    def _draw_full_controls(self, area):
        """Corners of the full-screen video (in the black side bars when there are any)."""
        ui, status = self.ui, self.status
        width = max(ui.px(72), min(ui.px(150), (ui.area.w - fit(ui.area, WIDTH, HEIGHT).w) // 2 - ui.px(8)))
        left = pygame.Rect(area.x, area.y, width, area.h)
        right = pygame.Rect(area.right - width, area.y, width, area.h)
        leave, _, stop = rows(left, ui.px(60), 1, ui.px(120))
        pill, speed, _ = rows(right, ui.px(46), ui.px(34), 1)
        if ui.button(leave, "EXIT", icon="expand", size=BODY):
            self.full = False
        self.app.draw_state(pill)
        ui.text(f"{status.speed}%" if status.online else "--", speed, TITLE, WHITE, True, "center")
        if ui.stop_button(stop, "Esc", TITLE):
            self.app.stop()

    def _draw_picture_panel(self, video):
        ui = self.ui
        panel = pygame.Rect(0, 0, min(video.w - ui.px(12), ui.px(300)), min(video.h - ui.px(12), ui.px(330)))
        panel.topleft = video.x + ui.px(6), video.y + ui.px(6)
        ui.box(panel, CARD, 10, MUTED)
        *lines, bottom = rows(inset(panel, ui.px(10)), *[1] * len(PICTURE), 1.1, gap=ui.px(5))
        for line, a in zip(lines, PICTURE):
            label, control = columns(line, 1, 1.7, gap=ui.px(6))
            value = self.camera.picture[a.key]
            ui.text(a.label, label, SMALL, TEXT if value == a.default else AMBER, True)
            new = ui.number(control, value, a.low, a.high, a.step, a.show, ("picture", a.key))
            if new is not None:
                self.camera.adjust(a.key, new)
        reset, close = columns(bottom, 1, 1, gap=ui.px(6))
        if ui.button(reset, "RESET", size=BODY):
            self.camera.reset_picture()
        if ui.button(close, "CLOSE", size=BODY):
            self.picture_open = False

    def _draw_picking(self, video):
        """Crosshair: follows the mouse, or the arrow keys."""
        ui = self.ui
        self._move_crosshair()
        if video.collidepoint(ui.mouse):
            self.crosshair = [(ui.mouse[0] - video.x) / video.w, (ui.mouse[1] - video.y) / video.h]
        x, y = round(video.x + self.crosshair[0] * video.w), round(video.y + self.crosshair[1] * video.h)
        for width, color in ((ui.px(5), BLACK), (ui.px(2), AMBER)):
            pygame.draw.circle(ui.surface, color, (x, y), ui.px(16), width)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                pygame.draw.line(ui.surface, color, (x + dx * ui.px(6), y + dy * ui.px(6)),
                                 (x + dx * ui.px(28), y + dy * ui.px(28)), width)
        strip = pygame.Rect(video.x, video.y, video.w, ui.px(32))
        ui.shade(strip, BLACK, 170)
        ui.text("Click the object, or move with the arrow keys and press Enter · Backspace = cancel",
                inset(strip, ui.px(10), 0), LABEL, AMBER, True, "center")
        if ui.click and video.collidepoint(ui.click):
            self.crosshair = [(ui.click[0] - video.x) / video.w, (ui.click[1] - video.y) / video.h]
            ui.click = None
            self._pick()
