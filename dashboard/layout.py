"""Cutting a rectangle into rows, columns and grids.

A size is either Px (fixed, in pixels) or a plain number (a share of whatever is left over)."""
import pygame


class Px(int):
    """A fixed size in pixels."""


def _cut(start, length, sizes, gap):
    room = max(0, length - gap * (len(sizes) - 1))
    fixed = sum(size for size in sizes if isinstance(size, Px))
    shares = sum(size for size in sizes if not isinstance(size, Px))
    squeeze = min(1, room / fixed) if fixed else 1       # not enough room: the fixed parts shrink together
    left = max(0, room - fixed)
    for size in sizes:
        size = size * squeeze if isinstance(size, Px) else left * size / shares
        yield round(start), round(start + size) - round(start)
        start += size + gap


def columns(rect, *sizes, gap=0):
    return [pygame.Rect(x, rect.y, w, rect.h) for x, w in _cut(rect.x, rect.w, sizes, gap)]


def rows(rect, *sizes, gap=0):
    return [pygame.Rect(rect.x, y, rect.w, h) for y, h in _cut(rect.y, rect.h, sizes, gap)]


def grid(rect, across, down, gap=0):
    """Equal cells, left to right and top to bottom."""
    return [cell for row in rows(rect, *[1] * down, gap=gap) for cell in columns(row, *[1] * across, gap=gap)]


def inset(rect, x, y=None):
    """rect with a margin taken off every side."""
    return rect.inflate(-2 * x, -2 * (x if y is None else y))


def centred(rect, width, height):
    """A box of this size in the middle of rect (no larger than rect)."""
    box = pygame.Rect(0, 0, min(width, rect.w), min(height, rect.h))
    box.center = rect.center
    return box


def fit(rect, width, height):
    """The largest box with this shape in the middle of rect."""
    scale = min(rect.w / width, rect.h / height)
    return centred(rect, round(width * scale), round(height * scale))
