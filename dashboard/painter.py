"""Drawing text, boxes, icons and bars at any screen size."""
import math
import os

import pygame

from . import icons
from .layout import Px
from .theme import ACCENT_DARK, BODY, MUTED, TEXT, TRACK

FONT_FILES = {False: "DejaVuSans.ttf", True: "DejaVuSans-Bold.ttf"}
FONT_FOLDERS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF", "/usr/share/fonts/dejavu-sans-fonts")
SMALLEST_FONT = 9        # pixels
CACHE_LIMIT = 4000


def font_file(bold):
    for folder in FONT_FOLDERS:
        path = os.path.join(folder, FONT_FILES[bold])
        if os.path.exists(path):
            return path
    return pygame.font.match_font("dejavusans,notosans,freesans,arial", bold=bold)


class Painter:
    """Sizes are in design units: 1 unit is 1 pixel on an 800x480 screen (480x800 when the window is taller
    than wide) and grows or shrinks with the window. Anything slow to draw (text, icons, smooth corners,
    whole buttons) is drawn once and re-used until the window changes size."""

    def __init__(self):
        self.surface = None
        self.area = pygame.Rect(0, 0, 0, 0)          # the part of the surface that can be seen
        self.size = (0, 0)
        self.scale = 1.0
        self.portrait = False
        self.dt = 0.0
        self.written = None          # tests: set to a list to collect (text, rect) of everything written
        self._font_files = {bold: font_file(bold) for bold in FONT_FILES}
        self._cache = {}
        self._gliding = {}

    def resize(self, size):
        """Work out the scale for this much room. Large screens grow the interface a bit slower than the
        screen, so they also get more room instead of only bigger letters."""
        if size == self.size:
            return
        self.size = size
        width, height = size
        self.portrait = height > width
        scale = min(width / 480, height / 800) if self.portrait else min(width / 800, height / 480)
        self.scale = scale if scale <= 1 else scale ** 0.8
        self._cache.clear()

    def px(self, units):
        return Px(max(1, round(units * self.scale)))

    def glide(self, key, target, seconds=0.045):
        """A value that moves smoothly to its target, at the same speed whatever the frame rate."""
        value = self._gliding.get(key, target)
        value += (target - value) * (1 - math.exp(-self.dt / seconds))
        if abs(target - value) < 0.002:
            value = target
        self._gliding[key] = value
        return value

    def _once(self, key, make):
        try:
            return self._cache[key]
        except KeyError:
            if len(self._cache) > CACHE_LIMIT:
                self._cache.clear()
            made = self._cache[key] = make()
            return made

    # ------------------------------------------------------------ text
    def font(self, size, bold=False):
        pixels = max(SMALLEST_FONT, round(size * self.scale))
        return self._once(("font", pixels, bold), lambda: pygame.font.Font(self._font_files[bold], pixels))

    def line_height(self, size, bold=False):
        return Px(self.font(size, bold).get_linesize())

    def _write(self, string, size, color, bold, width):
        """The text as a picture no wider than width: smaller letters first, then cut short."""
        for shrink in (1, 0.93, 0.86, 0.79, 0.72):
            font = self.font(size * shrink, bold)
            if font.size(string)[0] <= width:
                break
        else:
            while string and font.size(string + "…")[0] > width:
                string = string[:-1]
            string = string.rstrip() + "…" if string else ""
        return string, font.render(string, True, color).convert_alpha()

    def text(self, string, rect, size=BODY, color=TEXT, bold=False, align="left"):
        """One line of text, centred vertically in rect and never wider than it."""
        if not string or rect.w <= 0:
            return
        shown, image = self._once(("text", string, size, color, bold, rect.w),
                                  lambda: self._write(string, size, color, bold, rect.w))
        where = image.get_rect(centery=rect.centery)
        if align == "center":
            where.centerx = rect.centerx
        else:
            setattr(where, align, getattr(rect, align))
        self.surface.blit(image, where)
        if self.written is not None:
            self.written.append((shown, where))

    def text_width(self, string, size=BODY, bold=False):
        return Px(self.font(size, bold).size(string)[0] + 1)

    def paragraph(self, string, rect, size=BODY, color=MUTED, align="left"):
        """Text over as many lines as fit, from the top of rect."""
        height = self.line_height(size)
        count = max(1, rect.h // height)
        for i, line in enumerate(self._once(("lines", string, size, rect.w, count),
                                            lambda: self._wrap(string, size, rect.w, count))):
            self.text(line, pygame.Rect(rect.x, rect.y + i * height, rect.w, height), size, color, align=align)

    def _wrap(self, string, size, width, count):
        font, lines, words = self.font(size), [], string.split()
        while words:
            take = 1
            while take < len(words) and font.size(" ".join(words[:take + 1]))[0] <= width:
                take += 1
            if len(lines) == count - 1:
                take = len(words)            # last line: text() shortens whatever doesn't fit
            lines.append(" ".join(words[:take]))
            words = words[take:]
        return lines

    # ------------------------------------------------------------ shapes
    def fill(self, rect, color):
        self.surface.fill(color, rect)

    def _disc(self, radius, color):
        def draw():
            shade = pygame.Color(color)
            big = pygame.Surface((radius * 8, radius * 8), pygame.SRCALPHA)
            big.fill((shade.r, shade.g, shade.b, 0))
            pygame.draw.circle(big, shade, (radius * 4, radius * 4), radius * 4)
            return pygame.transform.smoothscale(big, (radius * 2, radius * 2)).convert_alpha()
        return self._once(("disc", radius, color), draw)

    def dot(self, center, radius, color):
        """A filled circle (sizes in pixels)."""
        self.surface.blit(self._disc(radius, color), (round(center[0]) - radius, round(center[1]) - radius))

    def circle(self, center, radius, color, width):
        """A circle (sizes in pixels)."""
        def draw():
            shade = pygame.Color(color)
            big = pygame.Surface((radius * 8, radius * 8), pygame.SRCALPHA)
            big.fill((shade.r, shade.g, shade.b, 0))
            pygame.draw.circle(big, shade, (radius * 4, radius * 4), radius * 4, width * 4)
            return pygame.transform.smoothscale(big, (radius * 2, radius * 2)).convert_alpha()
        image = self._once(("circle", radius, color, width), draw)
        self.surface.blit(image, image.get_rect(center=center))

    def box(self, rect, color, radius=8, border=None, thickness=1.2):
        """A rounded rectangle with smooth corners."""
        if border:
            self.box(rect, border, radius)
            edge = self.px(thickness)
            rect, radius = rect.inflate(-2 * edge, -2 * edge), radius - edge / self.scale
        if rect.w <= 0 or rect.h <= 0:
            return
        r = min(round(radius * self.scale), rect.w // 2, rect.h // 2)
        if r < 2:
            self.surface.fill(color, rect)
            return
        x, y, w, h = rect
        self.surface.fill(color, (x + r, y, w - 2 * r, h))
        self.surface.fill(color, (x, y + r, r, h - 2 * r))
        self.surface.fill(color, (x + w - r, y + r, r, h - 2 * r))
        disc = self._disc(r, color)
        for cx, cy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            self.surface.blit(disc, (x + cx * (w - r), y + cy * (h - r)), (cx * r, cy * r, r, r))

    def sprite(self, rect, key, paint):
        """Draw something with paint(rect) once and re-use the picture for the same key and size."""
        if self.written is not None:
            paint(rect)
            return

        def draw():
            image = pygame.Surface(rect.size, pygame.SRCALPHA)
            target, self.surface = self.surface, image
            paint(image.get_rect())
            self.surface = target
            return image
        self.surface.blit(self._once((key, rect.size), draw), rect)

    def shade(self, rect, color, alpha):
        """A see-through layer of colour."""
        def draw():
            layer = pygame.Surface(rect.size)
            layer.fill(color)
            layer.set_alpha(alpha)
            return layer
        self.surface.blit(self._once(("shade", rect.size, color, alpha), draw), rect)

    def icon(self, name, rect, color=TEXT, size=16):
        pixels = min(self.px(size), rect.w, rect.h)

        def draw():
            shade = pygame.Color(color)
            canvas = pygame.Surface((icons.SIZE, icons.SIZE), pygame.SRCALPHA)
            canvas.fill((shade.r, shade.g, shade.b, 0))
            icons.DRAW[name](canvas, shade)
            return pygame.transform.smoothscale(canvas, (pixels, pixels)).convert_alpha()
        if pixels > 0:
            image = self._once(("icon", name, color, pixels), draw)
            self.surface.blit(image, image.get_rect(center=rect.center))

    # ------------------------------------------------------------ gauges
    def meter(self, rect, share, color=ACCENT_DARK, start=0.0):
        """A bar filled from start to share (both 0..1)."""
        self.box(rect, TRACK, 4)
        a, b = (rect.x + round(rect.w * max(0.0, min(1.0, v))) for v in (start, share))
        if a != b:
            self.fill(pygame.Rect(min(a, b), rect.y, abs(b - a), rect.h), color)

    def thrust(self, rect, value, color):
        """A bar from the middle: value -1..1."""
        self.meter(rect, 0.5 + value / 2, color, start=0.5)
        line = self.px(1.5)
        self.fill(pygame.Rect(rect.centerx - line // 2, rect.y - self.px(3), line, rect.h + 2 * self.px(3)), MUTED)
