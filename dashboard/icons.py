"""Icons, drawn large (240 x 240) and shrunk by Painter.icon() so they are smooth at any size."""
import math

import pygame

SIZE = 240
PEN = 26


def _line(canvas, color, *points):
    for a, b in zip(points, points[1:]):
        pygame.draw.line(canvas, color, a, b, PEN)
    for p in points:
        pygame.draw.circle(canvas, color, p, PEN // 2)


def _shape(*points):
    return lambda canvas, color: pygame.draw.polygon(canvas, color, points)


def _strokes(*strokes):
    def draw(canvas, color):
        for points in strokes:
            _line(canvas, color, *points)
    return draw


def _power(canvas, color):
    pygame.draw.arc(canvas, color, (36, 46, 168, 168), math.radians(125), math.radians(415), PEN)
    _line(canvas, color, (120, 34), (120, 116))


def _restart(canvas, color):
    pygame.draw.arc(canvas, color, (40, 40, 160, 160), math.radians(70), math.radians(380), PEN)
    pygame.draw.polygon(canvas, color, [(128, 12), (128, 108), (206, 60)])


def _square(canvas, color):
    pygame.draw.rect(canvas, color, (56, 56, 128, 128), border_radius=14)


DRAW = {
    "left": _shape((160, 46), (160, 194), (62, 120)),
    "right": _shape((80, 46), (80, 194), (178, 120)),
    "play": _shape((72, 40), (72, 200), (196, 120)),
    "minus": _strokes([(56, 120), (184, 120)]),
    "plus": _strokes([(56, 120), (184, 120)], [(120, 56), (120, 184)]),
    "cross": _strokes([(66, 66), (174, 174)], [(174, 66), (66, 174)]),
    "check": _strokes([(52, 126), (102, 176), (190, 72)]),
    "back": _strokes([(140, 56), (76, 120), (140, 184)]),
    "expand": _strokes([(98, 44), (44, 44), (44, 98)], [(142, 44), (196, 44), (196, 98)],
                       [(98, 196), (44, 196), (44, 142)], [(142, 196), (196, 196), (196, 142)]),
    "erase": _strokes([(92, 56), (28, 120), (92, 184), (206, 184), (206, 56), (92, 56)],
                      [(118, 94), (170, 146)], [(170, 94), (118, 146)]),
    "shift": _strokes([(120, 40), (36, 124), (84, 124), (84, 198), (156, 198), (156, 124), (204, 124), (120, 40)]),
    "power": _power,
    "restart": _restart,
    "square": _square,
}
