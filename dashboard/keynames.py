"""Keyboard keys by the names stored in keyboard profiles ("w", "space", "prior"...)."""
import pygame

NAMES = {
    pygame.K_SPACE: "space", pygame.K_RETURN: "return", pygame.K_KP_ENTER: "kp_enter", pygame.K_ESCAPE: "escape",
    pygame.K_TAB: "tab", pygame.K_BACKSPACE: "backspace", pygame.K_CAPSLOCK: "caps_lock",
    pygame.K_LSHIFT: "shift", pygame.K_RSHIFT: "shift", pygame.K_LCTRL: "ctrl", pygame.K_RCTRL: "ctrl",
    pygame.K_LALT: "alt", pygame.K_RALT: "alt", pygame.K_LSUPER: "super", pygame.K_RSUPER: "super",
    pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left", pygame.K_RIGHT: "right",
    pygame.K_PAGEUP: "prior", pygame.K_PAGEDOWN: "next", pygame.K_HOME: "home", pygame.K_END: "end",
    pygame.K_INSERT: "insert", pygame.K_DELETE: "delete",
    pygame.K_EQUALS: "equal", pygame.K_MINUS: "minus", pygame.K_PLUS: "plus", pygame.K_COMMA: "comma",
    pygame.K_PERIOD: "period", pygame.K_SLASH: "slash", pygame.K_SEMICOLON: "semicolon",
    pygame.K_QUOTE: "apostrophe", pygame.K_LEFTBRACKET: "bracketleft", pygame.K_RIGHTBRACKET: "bracketright",
    pygame.K_BACKSLASH: "backslash", pygame.K_BACKQUOTE: "grave",
    pygame.K_KP_PLUS: "kp_add", pygame.K_KP_MINUS: "kp_subtract", pygame.K_KP_MULTIPLY: "kp_multiply",
    pygame.K_KP_DIVIDE: "kp_divide", pygame.K_KP_PERIOD: "kp_decimal",
}
NAMES.update({getattr(pygame, f"K_KP{n}"): f"kp_{n}" for n in range(10)})

LABELS = {"space": "Space", "shift": "Shift", "ctrl": "Ctrl", "alt": "Alt", "super": "Win", "up": "↑", "down": "↓",
          "left": "←", "right": "→", "prior": "PgUp", "next": "PgDn", "home": "Home", "end": "End", "insert": "Ins",
          "delete": "Del", "tab": "Tab", "backspace": "Bksp", "caps_lock": "Caps", "return": "Enter",
          "kp_enter": "Num Enter", "escape": "Esc", "equal": "=", "minus": "−", "plus": "+", "comma": ",",
          "period": ".", "slash": "/", "semicolon": ";", "apostrophe": "'", "bracketleft": "[", "bracketright": "]",
          "backslash": "\\", "grave": "`", "kp_add": "Num +", "kp_subtract": "Num −", "kp_multiply": "Num ×",
          "kp_divide": "Num ÷", "kp_decimal": "Num ."}

# the keyboard shown for picking a key with the mouse: (key, width in keys)
KEYBOARD = [
    [(k, 1) for k in ["grave", *"1234567890", "minus", "equal"]] + [("backspace", 2), ("insert", 1.3), ("home", 1.3)],
    [("tab", 1.5), *((k, 1) for k in [*"qwertyuiop", "bracketleft", "bracketright"]), ("backslash", 1.5),
     ("delete", 1.3), ("end", 1.3)],
    [("caps_lock", 1.8), *((k, 1) for k in [*"asdfghjkl", "semicolon", "apostrophe"]), ("return", 2.2),
     ("prior", 1.3), ("next", 1.3)],
    [("shift", 2.4), *((k, 1) for k in [*"zxcvbnm", "comma", "period", "slash"]), ("ctrl", 2.6), ("up", 2.6)],
    [("alt", 2.4), ("super", 2), ("space", 8), ("left", 1.7), ("down", 1.7), ("right", 1.8)],
]


def key_name(code):
    return NAMES.get(code) or pygame.key.name(code).lower().replace(" ", "_")


def key_label(name):
    """What is printed on the key."""
    if not name:
        return "—"
    if name in LABELS:
        return LABELS[name]
    return name.upper() if len(name) <= 2 else name.replace("kp_", "Num ").title()
