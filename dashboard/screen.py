"""Which part of the screen can be seen. Some monitors hide the edges of the picture."""
import json
import os

import pygame

import rov as core

from .link import write_file

EDGES = range(13)                             # percent of the screen, on each side
CASE_MONITOR, CASE_EDGE = (800, 480), 5       # the case monitor hides about this much
WINDOW, WINDOW_SHARE = (1200, 720), 0.9       # window size, and the share of the desktop it may take up


def settings_path():
    return os.path.join(core.HERE, "screen.json")


def load_edge():
    """The edge picked on SYSTEM, or None if none was picked."""
    try:
        with open(settings_path()) as f:
            edge = json.load(f)["edge"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return edge if type(edge) is int and edge in EDGES else None


def save_edge(edge):
    write_file(settings_path(), json.dumps({"edge": edge}))


def default_edge(size):
    return CASE_EDGE if size == CASE_MONITOR else 0


def visible(size, edge):
    """The part of a screen of this size that is left without its edges."""
    x, y = (round(length * edge / 100) for length in size)
    return pygame.Rect(x, y, size[0] - 2 * x, size[1] - 2 * y)


def window_size():
    """The usual window, smaller if the desktop has no room for it."""
    width, height = pygame.display.get_desktop_sizes()[0]
    return min(WINDOW[0], round(width * WINDOW_SHARE)), min(WINDOW[1], round(height * WINDOW_SHARE))
