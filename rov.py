#!/usr/bin/env python3
"""Underwater ROV controller - LAPTOP BACKUP version.

PS5 DualSense / PS4 DualShock 4 (or any Linux gamepad) on a Linux laptop (EndeavourOS)
-> USB serial -> Arduino Mega 2560 (rov_mega/rov_mega.ino) -> 4x BTS7960 motor drivers.
Same controls, profiles and dashboard as the Raspberry Pi version.

Controls (default):
  OPTIONS            ARM motors (sticks must be centred)
  CIRCLE or CREATE   DISARM / emergency stop
  Left stick         forward/back + turn
  Right stick        up/down (Y) + sideways (X)
  R2 / L2            up / down (adds to right stick)
  R1 / L1            speed level up / down
  SQUARE             camera view on/off (on the case screen)
  TRIANGLE           next drive profile (only while disarmed). What the sticks, triggers
                     and D-pad do is set per profile on the case screen: SETUP > PROFILES.
                     Built-in: Tank (stick per side) and Standard (drive+turn / up-down).

Keyboard drive (switch it on on the case screen: SETUP > EXTRAS):
  W/S forward/back, A/D turn, Space up, Shift down, R/F tilt, Enter ARM, Esc STOP.
  The dashboard sends the keys; if it stops sending for 0.5 s the keys count as released
  (and with no controller connected the motors disarm).

Autopilot (SETUP > AUTOPILOT, mouse only, off at every start-up): trim, cruise control, line following,
colour-target following, camera heading hold, and pre-programmed missions. See class Autopilot.

Arm sound: when armed, the motors play a tune (quiet audio-frequency PWM, like a 3D printer's
stepper "music"): an FPV-drone style arming sound (5 s), or any RTTTL ringtone you paste in.
Touching any control stops the tune at once so you can drive. On/off, volume, tune and TEST are
on SETUP > EXTRAS.

Safety: starts disarmed; disarms on controller loss, heartbeat loss, panel switch off,
or program exit. The BTS7960 enable line is only HIGH while armed.

Usage:
  python3 rov.py                  run (normally started by systemd)
  python3 rov.py --test           spin each motor in turn to identify it
  python3 rov.py --dry-run        no GPIO, print motor output (for testing a controller)
  python3 rov.py --list           list input devices the Pi can see
"""
import argparse
import json
import logging
import os
import select
import signal
import sys
import time
import tomllib

log = logging.getLogger("rov")
HERE = os.path.dirname(os.path.abspath(__file__))
AXES = ("surge", "yaw", "heave", "sway", "pitch")
TICK = 0.02  # 50 Hz control loop
STATUS_PATH = "/dev/shm/rov-status.json"   # read by rov_gui.py
CMD_PATH = "/dev/shm/rov-cmd"              # written by rov_gui.py
KEYS_PATH = "/dev/shm/rov-keys.json"       # keyboard drive: keys held down, written by rov_gui.py

# Keyboard drive: key -> (motion axis, amount)
KEY_AXES = {"w": ("surge", 1.0), "s": ("surge", -1.0), "d": ("yaw", 1.0), "a": ("yaw", -1.0),
            "space": ("heave", 1.0), "shift": ("heave", -1.0), "r": ("pitch", 0.6), "f": ("pitch", -0.6)}

VISION_PATH = "/dev/shm/rov-vision.json"  # camera autopilot results, written by rov_gui.py

# Motor "music": (frequency Hz, seconds); 0 Hz = silence.
TUNES = {"beeps": [(1047, 0.10), (0, 0.03), (1319, 0.10), (0, 0.03), (1568, 0.20)]}
TUNE_MAX = 5.0           # seconds: longer tunes are cut off here
# Default arm sound: FPV-drone style (like a quad's ESCs + flight controller), 5 s:
TUNES["fpv"] = [
    (1319, 0.25), (0, 0.05), (1480, 0.25), (0, 0.05), (1661, 0.25),   # ESC power-up: 3 rising tones
    (0, 1.00),                                                          # waiting for the signal
    (988, 0.35), (0, 0.10), (1976, 0.55),                               # signal OK: low - HIGH
    (0, 0.65),
    (1976, 1.50),                                                       # ARMED: long beep
]
NOTE_SEMI = {"c": -9, "d": -7, "e": -5, "f": -4, "g": -2, "a": 0, "b": 2, "h": 2}


def parse_rtttl(text, max_s=TUNE_MAX):
    """RTTTL ringtone ('name:d=4,o=5,b=120:8e6,8d#6,...') -> (name, [(Hz, seconds), ...]).
    Each note is followed by a short gap so repeated notes are heard separately. Raises ValueError."""
    parts = " ".join(text.split()).split(":")
    if len(parts) != 3:
        raise ValueError("not a ringtone - needs name:settings:notes")
    name, settings, notes = parts[0].strip()[:24] or "Tune", parts[1], parts[2]
    d, o, b = 4, 6, 63
    for kv in settings.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k in ("d", "o", "b") and v.isdigit():
                d, o, b = (int(v), o, b) if k == "d" else (d, int(v), b) if k == "o" else (d, o, int(v))
    if not (1 <= d <= 64 and 3 <= o <= 8 and 10 <= b <= 900):
        raise ValueError("bad d=, o= or b= setting")
    whole = 240.0 / b
    out, total = [], 0.0
    for tok in notes.lower().replace(" ", "").split(","):
        if not tok:
            continue
        i = 0
        while i < len(tok) and tok[i].isdigit():
            i += 1
        dur = int(tok[:i]) if i else d
        rest = tok[i:]
        if not rest or rest[0] not in NOTE_SEMI and rest[0] != "p":
            raise ValueError(f"bad note '{tok}'")
        note, rest = rest[0], rest[1:]
        sharp = rest.startswith("#")
        rest = rest[1:] if sharp else rest
        dotted = "." in rest
        rest = rest.replace(".", "")
        octave = int(rest) if rest.isdigit() else o
        if dur not in (1, 2, 4, 8, 16, 32, 64) or not 3 <= octave <= 8:
            raise ValueError(f"bad note '{tok}'")
        sec = whole / dur * (1.5 if dotted else 1.0)
        if note == "p":
            out.append((0, sec))
        else:
            hz = 440.0 * 2 ** ((NOTE_SEMI[note] + sharp + 12 * (octave - 4)) / 12)
            hz = max(40, min(10000, round(hz)))
            on = sec * (0.9 if sec > 0.25 else 0.6)        # short notes staccato, like muted strums
            out += [(hz, on), (0, sec - on)]
        total += sec
        if total >= max_s:
            break
    if not any(f for f, _ in out):
        raise ValueError("no notes")
    # cut at max_s
    cut, t = [], 0.0
    for f, sec in out:
        if t >= max_s:
            break
        cut.append((f, min(sec, max_s - t)))
        t += sec
    return name, cut

# ---- drive profiles (edited on the case screen: SETUP > PROFILES; stored in profiles.json) ----
# Controls, read so that stick up / stick right / trigger pressed / D-pad up / D-pad right = positive.
INPUTS = ["ly", "lx", "ry", "rx", "l2", "r2", "dy", "dx"]
INPUT_LABELS = {"ly": "Left stick ↕", "lx": "Left stick ↔", "ry": "Right stick ↕", "rx": "Right stick ↔",
                "l2": "L2 trigger", "r2": "R2 trigger", "dy": "D-pad ↕", "dx": "D-pad ↔"}
# What a control can do, as a mix of motion axes.
ACTIONS = {
    "none": {},
    "forward": {"surge": 1.0},
    "turn": {"yaw": 1.0},
    "updown": {"heave": 1.0},
    "tilt": {"pitch": 1.0},
    "sideways": {"sway": 1.0},
    "left_side": {"surge": 0.5, "yaw": 0.5},     # tank style: drives only the left motor
    "right_side": {"surge": 0.5, "yaw": -0.5},   # tank style: drives only the right motor
}
ACTION_ORDER = ["none", "forward", "turn", "updown", "tilt", "sideways", "left_side", "right_side"]
DEFAULT_PROFILES = [
    {"name": "Tank", "map": {
        "ly": {"action": "left_side"}, "ry": {"action": "right_side"},
        "r2": {"action": "updown"}, "l2": {"action": "updown", "invert": True},
        "dy": {"action": "tilt", "power": 0.6}}},
    {"name": "Standard", "map": {
        "ly": {"action": "forward"}, "lx": {"action": "turn"}, "ry": {"action": "updown"},
        "r2": {"action": "updown"}, "l2": {"action": "updown", "invert": True},
        "dy": {"action": "tilt", "power": 0.6}}},
]


def profiles_path(config_path=None):
    return os.path.join(os.path.dirname(os.path.abspath(config_path or os.path.join(HERE, "config.toml"))),
                        "profiles.json")


def clean_profiles(data):
    """Validate profiles loaded from JSON; drop anything malformed."""
    out = []
    for p in (data or {}).get("profiles", []):
        if not isinstance(p, dict) or not str(p.get("name", "")).strip():
            continue
        m = {}
        for k, v in (p.get("map") or {}).items():
            if k in INPUTS and isinstance(v, dict) and v.get("action") in ACTIONS:
                m[k] = {"action": v["action"], "invert": bool(v.get("invert", False)),
                        "power": max(0.0, min(1.0, float(v.get("power", 1.0))))}
        out.append({"name": str(p["name"]).strip()[:24], "map": m})
    return out


# --------------------------------------------------------------------------- GPIO

class LgpioBackend:
    """Real GPIO via lgpio (works on Pi OS Bookworm/Trixie, Pi 4 and Pi 5)."""

    def __init__(self, chip, freq):
        # lgpio creates a notify pipe in its working dir; the default (cwd) may be
        # read-only (e.g. "/" under systemd), so point it somewhere writable.
        if not os.access(os.environ.get("LG_WD", os.getcwd()), os.W_OK):
            os.environ["LG_WD"] = HERE if os.access(HERE, os.W_OK) else "/tmp"
        import lgpio
        self.lg = lgpio
        self.freq = freq
        self.h = lgpio.gpiochip_open(chip)
        self.claimed = []

    def output(self, pin):
        self.lg.gpio_claim_output(self.h, pin, 0)
        self.claimed.append(pin)

    def input_pullup(self, pin):
        self.lg.gpio_claim_input(self.h, pin, self.lg.SET_PULL_UP)

    def read(self, pin):
        return self.lg.gpio_read(self.h, pin)

    def write(self, pin, value):
        self.lg.gpio_write(self.h, pin, 1 if value else 0)

    def pwm(self, pin, duty):
        if duty <= 0:
            # 0% duty at a valid frequency (lgpio rejects frequency 0 with 'bad PWM micros')
            self.lg.tx_pwm(self.h, pin, self.freq, 0)
            self.lg.gpio_write(self.h, pin, 0)
        else:
            self.lg.tx_pwm(self.h, pin, self.freq, min(100, duty))

    def tone(self, pin, freq, duty):
        """PWM at an audio frequency (arm sound). pwm() puts the pin back to the normal frequency."""
        if duty <= 0 or freq <= 0:
            self.pwm(pin, 0)
        else:
            self.lg.tx_pwm(self.h, pin, max(20, min(10000, freq)), min(100, duty))

    def close(self):
        for pin in self.claimed:
            try:
                self.pwm(pin, 0)
            except Exception:
                pass
        self.lg.gpiochip_close(self.h)


class MegaBackend:
    """Motor output through an Arduino Mega on USB serial (see rov_mega/rov_mega.ino).

    Same calls as LgpioBackend; flush() sends the whole pin state as one frame each control tick.
    The Mega switches everything off by itself if frames stop for 300 ms.
    """
    KNOWN_USB = {0x2341: "Arduino", 0x2A03: "Arduino", 0x1A86: "CH340", 0x0403: "FTDI", 0x10C4: "CP210x"}

    def __init__(self, port="auto", baud=115200):
        import serial
        import serial.tools.list_ports
        self.serial = serial
        self.list_ports = serial.tools.list_ports
        self.want_port, self.baud = port, baud
        self.ser = None
        self.port = None
        self.duty = {}            # pwm pin -> duty 0..100
        self.en_pin, self.en_val = 22, 0
        self.tone_freq = 0        # >0: arm sound playing, motor pins run at this frequency
        self.last_rx = 0.0
        self.next_try = 0.0
        self.rx = b""
        self.desc = "Arduino Mega not found - plug in its USB cable"

    # -- same interface as the GPIO backends --
    def output(self, pin):
        self.duty.setdefault(pin, 0)   # every motor pin is in every frame (enable pin removed in write())

    def input_pullup(self, pin):
        pass

    def read(self, pin):
        return 0

    def write(self, pin, value):
        self.duty.pop(pin, None)
        self.en_pin, self.en_val = pin, 1 if value else 0

    def pwm(self, pin, duty):
        if duty > 0:
            self.tone_freq = 0    # normal driving again: Mega goes back to its normal PWM
        self.duty[pin] = max(0, min(100, duty))

    def tone(self, pin, freq, duty):
        """Arm sound: run the motor pins at an audio frequency (Mega 'T' frame)."""
        if freq > 0:
            self.tone_freq = int(max(40, min(10000, freq)))
        self.duty[pin] = max(0, min(100, duty)) if freq > 0 else 0

    # -- serial link --
    def find_port(self):
        if self.want_port and self.want_port != "auto":
            return self.want_port
        ports = sorted(self.list_ports.comports(), key=lambda p: p.device)
        for p in ports:
            if p.vid in self.KNOWN_USB:
                return p.device
        for p in ports:
            if "ttyACM" in p.device or "ttyUSB" in p.device:
                return p.device
        return None

    def connect(self):
        now = time.monotonic()
        if now < self.next_try:
            return
        self.next_try = now + 1.0
        port = self.find_port()
        if not port:
            self.desc = "Arduino Mega not found - plug in its USB cable"
            return
        try:
            self.ser = self.serial.Serial(port, self.baud, timeout=0, write_timeout=0.05)
            self.port = port
            self.opened_at = time.monotonic()
            self.last_rx = 0.0
            self.rx = b""
            self.desc = f"Mega on {port}: starting up..."
            log.info(f"Opened {port}")
        except (OSError, self.serial.SerialException) as ex:
            self.ser = None
            self.desc = f"Can't open {port}: {ex}"
            if "ermission" in str(ex):
                self.desc = f"No permission for {port} - run install.sh, then log out and back in"

    def drop(self, why):
        log.warning(f"Mega link lost: {why}")
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.desc = f"Mega disconnected ({why})"

    def flush(self):
        if self.ser is None:
            self.connect()
            if self.ser is None:
                return
        head = f"T {self.en_pin} {self.en_val if self.ready() else 0} {self.tone_freq} " if self.tone_freq \
            else f"F {self.en_pin} {self.en_val if self.ready() else 0} "
        frame = head + " ".join(
            f"{p}:{round(d * 2.55)}" for p, d in sorted(self.duty.items())) + "\n"
        try:
            data = self.ser.read(256)
            # Only talk once the Mega has said hello: data sent while its bootloader runs
            # (it resets when the port opens) can delay start-up.
            if self.last_rx:
                self.ser.write(frame.encode())
        except self.serial.SerialTimeoutException:
            data = b""                            # USB busy: skip this frame, the next one follows in 20 ms
        except (OSError, self.serial.SerialException) as ex:
            self.drop(str(ex))
            return
        if data:
            self.rx += data
            *lines, self.rx = self.rx.split(b"\n")
            for line in lines:
                text = line.decode(errors="replace").strip()
                if text in ("OK", "ROV-MEGA READY", "PONG", "TIMEOUT"):
                    self.last_rx = time.monotonic()
                    self.desc = f"Mega OK on {self.port}"
                elif text.startswith("ERR"):
                    log.warning(f"Mega: {text}")
        if self.last_rx and time.monotonic() - self.last_rx > 1.5:
            self.drop("no reply for 1.5 s")
        elif not self.last_rx and time.monotonic() - self.opened_at > 6.0:
            self.drop("no answer - is rov_mega.ino uploaded to the Mega?")
            self.desc = f"No answer from {self.port} - upload rov_mega.ino to the Mega"

    def ready(self):
        return self.ser is not None and self.last_rx and time.monotonic() - self.last_rx < 1.5

    def describe(self):
        return self.desc

    def close(self):
        if self.ser:
            try:
                off = f"F {self.en_pin} 0 " + " ".join(f"{p}:0" for p in sorted(self.duty)) + "\n"
                self.ser.write(off.encode())
                self.ser.flush()
                self.ser.close()
            except Exception:
                pass


class FakeBackend:
    """No hardware: used for --dry-run and tests."""

    def __init__(self, *_):
        self.state = {}

    def output(self, pin):
        self.state[pin] = 0

    def input_pullup(self, pin):
        self.state[pin] = 0  # "switch on"

    def read(self, pin):
        return self.state.get(pin, 0)

    def write(self, pin, value):
        self.state[pin] = 100 if value else 0

    def pwm(self, pin, duty):
        self.state[pin] = duty

    def tone(self, pin, freq, duty):
        self.state[pin] = duty if freq > 0 else 0

    def close(self):
        pass


# --------------------------------------------------------------------------- motors

class Thruster:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.rpwm = int(cfg["rpwm"])
        self.lpwm = int(cfg["lpwm"])
        self.coef = {a: float(cfg.get(a, 0.0)) for a in AXES}
        self.invert = bool(cfg.get("invert", False))
        self.value = 0.0          # current (ramped) output, -1..1
        self._sent = (0, 0)       # last (rpwm duty, lpwm duty) written

    def job(self):
        jobs = [("forward/back", "surge"), ("turn", "yaw"), ("up/down", "heave"), ("sideways", "sway"),
                ("tilt", "pitch")]
        return " + ".join(label for label, a in jobs if self.coef[a]) or "unused"

    def target(self, cmd):
        return sum(self.coef[a] * cmd[a] for a in AXES)

    def apply(self, gpio, value, min_duty):
        self.value = value
        v = -value if self.invert else value
        mag = abs(v)
        duty = 0 if mag < 0.01 else round(min_duty + (100 - min_duty) * mag)
        want = (duty, 0) if v > 0 else (0, duty)
        if want == self._sent:
            return
        # turn the old direction off before driving the new one (never both on)
        if want[0] == 0 and self._sent[0]:
            gpio.pwm(self.rpwm, 0)
        if want[1] == 0 and self._sent[1]:
            gpio.pwm(self.lpwm, 0)
        if want[0]:
            gpio.pwm(self.rpwm, want[0])
        if want[1]:
            gpio.pwm(self.lpwm, want[1])
        self._sent = want

    def stop(self, gpio):
        gpio.pwm(self.rpwm, 0)
        gpio.pwm(self.lpwm, 0)
        self.value = 0.0
        self._sent = (0, 0)


def shape(x, deadzone, expo):
    """Deadzone + expo curve, keeps sign, output -1..1."""
    if abs(x) < deadzone:
        return 0.0
    s = 1 if x > 0 else -1
    x = (abs(x) - deadzone) / (1 - deadzone)
    x = min(1.0, x)
    return s * ((1 - expo) * x + expo * x ** 3)


def mix(thrusters, cmd, limit):
    """Per-motor targets, scaled so no motor exceeds `limit` and ratios are kept."""
    raw = [t.target(cmd) for t in thrusters]
    peak = max([1.0] + [abs(r) for r in raw])
    return [r / peak * limit for r in raw]


def ramp(current, target, max_step):
    return current + max(-max_step, min(max_step, target - current))


# --------------------------------------------------------------------------- controller

class Gamepad:
    """Wraps an evdev gamepad (+ its motion-sensor node, used as a heartbeat)."""

    def __init__(self, dev, motion):
        from evdev import ecodes as e
        self.e = e
        self.dev = dev
        self.motion = motion
        self.name = dev.name
        self.last_motion = time.monotonic()
        self.axes = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "l2": 0.0, "r2": 0.0, "dy": 0.0, "dx": 0.0}
        caps = dict(dev.capabilities().get(e.EV_ABS, []))
        self.absinfo = caps
        self.code_map = {e.ABS_X: "lx", e.ABS_Y: "ly", e.ABS_RX: "rx", e.ABS_RY: "ry",
                         e.ABS_Z: "l2", e.ABS_RZ: "r2", e.ABS_HAT0Y: "dy", e.ABS_HAT0X: "dx"}
        # Some pads (generic/Xbox via other drivers) report triggers as ABS_GAS/ABS_BRAKE
        if e.ABS_GAS in caps:
            self.code_map[e.ABS_GAS] = "r2"
        if e.ABS_BRAKE in caps:
            self.code_map[e.ABS_BRAKE] = "l2"

    def fds(self):
        return [self.dev.fd] + ([self.motion.fd] if self.motion else [])

    def norm(self, code, value):
        info = self.absinfo.get(code)
        if info is None or info.max == info.min:
            return 0.0
        name = self.code_map[code]
        if name in ("l2", "r2"):
            return max(0.0, min(1.0, (value - info.min) / (info.max - info.min)))
        centre = (info.max + info.min) / 2
        half = (info.max - info.min) / 2
        return max(-1.0, min(1.0, (value - centre) / half))

    def read(self, fd):
        """Returns list of pressed button codes. Raises OSError when disconnected."""
        presses = []
        if self.motion and fd == self.motion.fd:
            for _ in self.motion.read():
                pass
            self.last_motion = time.monotonic()
            return presses
        for ev in self.dev.read():
            if ev.type == self.e.EV_ABS and ev.code in self.code_map:
                self.axes[self.code_map[ev.code]] = self.norm(ev.code, ev.value)
            elif ev.type == self.e.EV_KEY and ev.value == 1:
                presses.append(ev.code)
        return presses

    def close(self):
        for d in (self.dev, self.motion):
            if d:
                try:
                    d.close()
                except Exception:
                    pass


def find_gamepad(preferred):
    """Pick the best connected gamepad. Returns Gamepad or None."""
    from evdev import InputDevice, list_devices, ecodes as e
    devices = []
    for path in list_devices():
        try:
            devices.append(InputDevice(path))
        except OSError:
            pass
    pads, motions = [], []
    for d in devices:
        caps = d.capabilities()
        keys = caps.get(e.EV_KEY, [])
        abs_codes = [c for c, _ in caps.get(e.EV_ABS, [])]
        if "Motion Sensors" in d.name:
            motions.append(d)
        elif e.BTN_SOUTH in keys and e.ABS_X in abs_codes and e.ABS_Y in abs_codes:
            pads.append(d)

    def rank(d):
        for i, frag in enumerate(preferred):
            if frag.lower() in d.name.lower():
                return i
        return len(preferred)

    pads.sort(key=rank)
    chosen = pads[0] if pads else None
    motion = None
    if chosen and chosen.uniq:
        motion = next((m for m in motions if m.uniq == chosen.uniq), None)
    for d in devices:
        if d is not chosen and d is not motion:
            d.close()
    return Gamepad(chosen, motion) if chosen else None


# --------------------------------------------------------------------------- autopilot

AP_MODES = ["cruise", "line", "target", "heading", "mission"]
VISION_MODES = ("line", "target", "heading")
# settings: key -> (default, min, max, step)
AP_SETTINGS = {
    "heave_trim": (0.0, -0.5, 0.5, 0.02),    # constant up(+)/down(-) thrust so it hovers
    "pitch_trim": (0.0, -0.5, 0.5, 0.02),    # constant nose up(+)/down(-)
    "power": (0.5, 0.1, 1.0, 0.05),          # most the autopilot may use on any axis
    "speed": (0.3, 0.05, 0.8, 0.05),         # forward speed for line / target following
    "steer": (1.0, 0.2, 3.0, 0.1),           # how hard it steers (gain)
    "target_size": (0.08, 0.02, 0.4, 0.01),  # target follow: stop approaching at this share of the picture
}
AP_COLOURS = ["dark", "red", "orange", "yellow", "green", "blue", "pink", "white", "picked"]
AP_CHOICES = {"line_colour": "dark", "target_colour": "orange"}
MISSION_ACTIONS = {"forward": {"surge": 1.0}, "back": {"surge": -1.0}, "up": {"heave": 1.0},
                   "down": {"heave": -1.0}, "turn_left": {"yaw": -1.0}, "turn_right": {"yaw": 1.0},
                   "tilt_up": {"pitch": 1.0}, "tilt_down": {"pitch": -1.0}, "hover": {}}
MISSION_ORDER = list(MISSION_ACTIONS)
DEFAULT_MISSION = [{"do": "down", "power": 0.4, "secs": 2.0}, {"do": "forward", "power": 0.4, "secs": 4.0},
                   {"do": "turn_right", "power": 0.4, "secs": 1.5}, {"do": "forward", "power": 0.4, "secs": 3.0},
                   {"do": "hover", "power": 0.0, "secs": 2.0}]
LOST_TIMEOUT = 3.0        # camera modes: switch off after this long without seeing the line/target
PILOT_ACTIVE = 0.15       # pilot input above this = the pilot wants control


def clean_mission(data):
    steps = []
    for st in (data or {}).get("steps", [])[:12]:
        try:
            if st["do"] in MISSION_ACTIONS:
                steps.append({"do": st["do"], "power": max(0.0, min(1.0, float(st.get("power", 0.4)))),
                              "secs": max(0.5, min(60.0, float(st.get("secs", 1.0))))})
        except (KeyError, TypeError, ValueError):
            pass
    return steps


class Autopilot:
    """Adds automatic thrust on top of the pilot's command. Only ever active while ARMED, only switched on
    from the screen (never the controller), and the master switch is OFF at every start-up.

    Modes (one at a time):
      cruise   holds the thrust you were using when you clicked it
      line     follows a line seen by the camera
      target   steers towards a coloured object and stops at a set distance
      heading  holds the direction using camera motion
      mission  runs timed steps (mission.json, edited on the screen)
    ANY pilot input (stick, trigger, D-pad, keyboard drive key) switches the mode off immediately.
    Cruise: the input held while clicking sets the speed; once let go, the next movement takes over.
    Trim (hover / nose) is added whenever the master switch is ON and the ROV is armed.
    """

    def __init__(self, rov, settings_path):
        self.rov = rov
        self.path = settings_path
        self.s = {k: v[0] for k, v in AP_SETTINGS.items()}
        self.s.update(AP_CHOICES)
        self.s["target_pick"] = None   # [hue 0-360, saturation 0-1, brightness 0-1] clicked on the camera page
        try:
            with open(settings_path) as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in AP_SETTINGS:
                    _, lo, hi, _ = AP_SETTINGS[k]
                    self.s[k] = max(lo, min(hi, float(v)))
                elif k in AP_CHOICES and v in AP_COLOURS:
                    self.s[k] = v
                elif k == "target_pick":
                    self.s[k] = self.clean_pick(v)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.enabled = False
        self.mode = None
        self.info = ""
        self.locked = {}
        self.lost_since = None
        self.epoch = 0           # bumped to reset the camera heading reference
        self.steps, self.step_i, self.step_t0 = [], 0, 0.0
        self.alert = ""          # shown big on the screen: autopilot is unsure, pilot must take over
        self.alert_until = 0.0
        self.released = True     # cruise: pilot has let go since engaging

    def save(self):
        try:
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.s, f, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError as ex:
            log.error(f"saving autopilot settings failed: {ex}")

    @staticmethod
    def clean_pick(v):
        try:
            h, s, b = (float(x) for x in (v.split(",") if isinstance(v, str) else v))
            return [h % 360, max(0.0, min(1.0, s)), max(0.0, min(1.0, b))]
        except (TypeError, ValueError, AttributeError):
            return None

    def set(self, key, value):
        if key == "target_pick":                  # picked by clicking the object on the camera page
            pick = self.clean_pick(value)
            if pick:
                self.s["target_pick"], self.s["target_colour"] = pick, "picked"
                self.save()
                self.rov.say("Target colour picked from the camera")
            return
        if key in AP_SETTINGS:
            _, lo, hi, _ = AP_SETTINGS[key]
            self.s[key] = round(max(lo, min(hi, float(value))), 3)
        elif key in AP_CHOICES and value in AP_COLOURS:
            self.s[key] = value
        else:
            return
        self.save()

    def set_enabled(self, on):
        self.enabled = on
        if not on:
            self.off(None)
            self.alert, self.alert_until = "", 0.0
        self.rov.say("Autopilot switched " + ("ON (trim active, pick a mode)" if on else "OFF"))

    def off(self, why, warn=False):
        was = self.mode
        self.mode = None
        self.locked = {}
        self.lost_since = None
        self.info = ""
        self.alert = ""
        if was and why:
            self.rov.say(f"Autopilot {was.upper()} off ({why})")
        if was and warn:
            # it gave up by itself while the ROV is still armed: tell the pilot loudly
            self.alert = f"AUTOPILOT OFF ({why}) - YOU HAVE CONTROL"
            self.alert_until = time.monotonic() + 6.0

    def engage(self, mode, pilot):
        """Screen button. pilot = the pilot's current command."""
        if mode not in AP_MODES:
            return
        if not self.enabled:
            self.rov.say("Switch the autopilot ON first (SETUP > AUTOPILOT)")
        elif not self.rov.armed:
            self.rov.say("Arm first, then pick an autopilot mode")
        elif mode == "cruise" and not any(abs(v) > PILOT_ACTIVE for v in pilot.values()):
            self.rov.say("Drive at the speed you want, then click CRUISE")
        elif mode != "cruise" and self.rov.pilot_input():
            self.rov.say(f"Let go of the sticks, then click {mode.upper()}")
        else:
            if mode == "mission":
                self.steps = self.load_mission()
                if not self.steps:
                    self.rov.say("The mission has no steps (SETUP > AUTOPILOT > EDIT MISSION)")
                    return
                self.step_i, self.step_t0 = 0, time.monotonic()
            self.mode = mode
            self.locked = {a: v for a, v in pilot.items() if abs(v) > PILOT_ACTIVE} if mode == "cruise" else {}
            self.released = mode != "cruise"
            self.alert, self.alert_until = "", 0.0
            self.lost_since = None
            self.epoch += 1
            self.rov.say(f"Autopilot {mode.upper()} engaged - " +
                         ("let go of the sticks; then any movement takes over" if mode == "cruise"
                          else "any stick/key movement or STOP takes over"))

    def mission_path(self):
        return os.path.join(os.path.dirname(self.path), "mission.json")

    def load_mission(self):
        try:
            with open(self.mission_path()) as f:
                return clean_mission(json.load(f))
        except FileNotFoundError:
            return [dict(st) for st in DEFAULT_MISSION]
        except (OSError, ValueError):
            return []

    def read_vision(self):
        try:
            with open(VISION_PATH) as f:
                v = json.load(f)
        except (OSError, ValueError):
            return None
        if v.get("mode") != self.mode or abs(time.time() - float(v.get("ts", 0))) > 0.5:
            return None
        if v.get("epoch", self.epoch) != self.epoch and self.mode == "heading":
            return None
        return v

    def lost(self, what):
        now = time.monotonic()
        if self.lost_since is None:
            self.lost_since = now
        left = LOST_TIMEOUT - (now - self.lost_since)
        self.info = f"lost the {what} - hovering, switching off in {max(0.0, left):.0f} s"
        self.alert = f"AUTOPILOT UNSURE: lost the {what} - TAKE CONTROL NOW"
        if left <= 0:
            self.off(f"lost the {what}", warn=self.rov.armed)

    def unsure(self, why):
        self.alert = f"AUTOPILOT UNSURE: {why} - TAKE CONTROL NOW"

    def apply(self, pilot):
        """pilot: the pilot's command (surge/yaw/heave/sway/pitch). Returns the command to drive."""
        out = dict(pilot)
        if not self.enabled or not self.rov.armed:
            if self.mode:
                self.off("disarmed")
            return out
        out["heave"] += self.s["heave_trim"]
        out["pitch"] += self.s["pitch_trim"]
        mode = self.mode
        touching = self.rov.pilot_input()
        if touching:
            self.alert_until = 0.0              # the pilot has control: clear any "you have control" banner
        if not mode:
            return out
        if mode == "cruise" and not self.released:
            if touching:                        # still holding the input used to set the cruise speed
                for a in AXES:
                    out[a] -= pilot[a]
            else:
                self.released = True
        elif touching:
            self.off("pilot took over")
            return out
        P, steer, speed = self.s["power"], self.s["steer"], self.s["speed"]
        auto = {}
        if mode == "cruise":
            auto = dict(self.locked)
            self.alert = ""
            self.info = "holding " + ", ".join(f"{a} {v * 100:+.0f}%" for a, v in self.locked.items())
        elif mode == "mission":
            now = time.monotonic()
            while self.step_i < len(self.steps) and now - self.step_t0 >= self.steps[self.step_i]["secs"]:
                self.step_t0 += self.steps[self.step_i]["secs"]
                self.step_i += 1
            if self.step_i >= len(self.steps):
                self.off("mission complete")
                return out
            self.alert = ""
            st = self.steps[self.step_i]
            auto = {a: w * st["power"] for a, w in MISSION_ACTIONS[st["do"]].items()}
            self.info = (f"step {self.step_i + 1}/{len(self.steps)}: {st['do'].replace('_', ' ')} "
                         f"{st['power'] * 100:.0f}% ({st['secs'] - (now - self.step_t0):.1f} s left)")
        elif mode == "line":
            v = self.read_vision()
            if not v or not v.get("found"):
                self.lost("line")
            else:
                self.lost_since = None
                self.alert = ""
                off, ang = float(v["offset"]), float(v["angle"])
                auto = {"yaw": steer * (0.8 * off + 0.6 * ang), "surge": speed * (1 - min(0.7, abs(ang)))}
                self.info = f"following the line (offset {off:+.2f}, angle {ang:+.2f})"
        elif mode == "target":
            v = self.read_vision()
            if not v or not v.get("found"):
                self.lost("target")
            else:
                self.lost_since = None
                self.alert = ""
                x, y, area = float(v["x"]), float(v["y"]), float(v["area"])
                size = self.s["target_size"]
                approach = max(-0.5, min(1.0, (size - area) / size * 1.5))
                centred = max(0.0, 1 - abs(x) * 1.5)          # only drive forward when it's roughly ahead
                auto = {"yaw": steer * 0.9 * x, "heave": -steer * 0.7 * y, "surge": speed * approach * centred}
                self.info = f"target at x {x:+.2f}, y {y:+.2f}, size {area * 100:.1f}%"
        elif mode == "heading":
            v = self.read_vision()
            if not v or not v.get("found"):
                self.info = "can't see enough detail to hold heading (not correcting)"
                self.unsure("can't see enough to hold heading" if v else "no camera picture")
            else:
                self.alert = ""
                err = float(v["heading"])
                auto = {"yaw": max(-1.0, min(1.0, -steer * 2.5 * err))}
                self.info = f"holding heading (drift {err * 100:+.1f}%)"
        for a, val in auto.items():
            out[a] += max(-P, min(P, val))
        return out

    def status(self):
        # warnings only exist while the autopilot is switched ON
        alert = self.alert if self.enabled and (self.mode or time.monotonic() < self.alert_until) else ""
        return {"enabled": self.enabled, "mode": self.mode, "info": self.info, "epoch": self.epoch,
                "settings": self.s, "alert": alert}


# --------------------------------------------------------------------------- main app

class ROV:
    def __init__(self, cfg, gpio, config_path=None):
        self.cfg = cfg
        self.config_path = config_path
        c = cfg.get("control", {})
        g = cfg.get("gpio", {})
        self.gpio = gpio
        self.deadzone = c.get("deadzone", 0.08)
        self.expo = c.get("expo", 0.35)
        self.ramp_rate = c.get("ramp_per_second", 3.0)
        self.levels = c.get("speed_levels", [0.25, 0.5, 0.75, 1.0])
        self.level = min(c.get("start_speed_level", 1), len(self.levels) - 1)
        self.min_duty = c.get("min_duty", 0)
        self.load_profiles(c.get("drive_profile", "Standard"))
        self.view_toggle = 0  # bumped by SQUARE; the GUI flips between camera and dashboard
        self.hb_timeout = c.get("heartbeat_timeout", 0.5)
        self.inv = {a: -1.0 if c.get(f"invert_{a}", False) else 1.0 for a in AXES}
        self.preferred = cfg.get("controllers", {}).get("preferred", ["DualSense", "Wireless Controller"])
        self.enable_pin = g.get("enable_pin", 17)
        self.led_pin = g.get("status_led_pin", -1)
        self.switch_pin = g.get("arm_switch_pin", -1)
        self.thrusters = [Thruster(m) for m in cfg["motor"]]
        self.armed = False
        self.pad = None
        self.running = True
        self.status_msg = ""
        self.kbd_enabled = bool(c.get("keyboard_drive", False))
        self.kbd_keys = set()
        self.kbd_live = False
        self.sound_on = bool(c.get("arm_sound", True))
        self.sound_volume = max(1, min(15, int(c.get("sound_volume", 5))))
        self.tune = None
        self.tune_path = os.path.join(os.path.dirname(os.path.abspath(config_path or os.path.join(HERE, "config.toml"))),
                                      "tune.txt")
        self.load_tune()
        self.ap = Autopilot(self, os.path.join(os.path.dirname(os.path.abspath(config_path or
                                                                               os.path.join(HERE, "config.toml"))),
                                               "autopilot.json"))
        self.last_pilot = dict.fromkeys(AXES, 0.0)
        try:
            os.remove(CMD_PATH)  # ignore any stale screen command from before startup
        except OSError:
            pass

        gpio.output(self.enable_pin)
        gpio.write(self.enable_pin, 0)
        if self.led_pin >= 0:
            gpio.output(self.led_pin)
        if self.switch_pin >= 0:
            gpio.input_pullup(self.switch_pin)
        for t in self.thrusters:
            gpio.output(t.rpwm)
            gpio.output(t.lpwm)

    # ---- safety ----
    def switch_on(self):
        return self.switch_pin < 0 or self.gpio.read(self.switch_pin) == 0

    def pilot_input(self):
        """Is the pilot touching anything that drives (sticks, triggers, D-pad, keyboard drive keys)?"""
        return not self.sticks_centred() or bool(self.kbd_live and self.kbd_keys)

    def sticks_centred(self):
        if not self.pad:
            return True
        a = self.pad.axes
        return (all(abs(a[k]) < self.deadzone * 2 for k in ("lx", "ly", "rx", "ry"))
                and a["l2"] < 0.1 and a["r2"] < 0.1 and abs(a.get("dy", 0)) < 0.5 and abs(a.get("dx", 0)) < 0.5)

    def output_ready(self):
        return getattr(self.gpio, "ready", lambda: True)()

    def arm(self, source="controller"):
        if self.armed:
            return
        if source == "keyboard" and not (self.kbd_enabled and self.kbd_live):
            self.say("Keyboard drive is off (SETUP > EXTRAS)")
        elif not self.output_ready():
            self.say("Can't arm: " + getattr(self.gpio, "describe", lambda: "motor output not ready")())
        elif not self.switch_on():
            self.say("Panel ARM switch is OFF - can't arm")
        elif not self.sticks_centred():
            self.say("Centre the sticks and release triggers to arm")
        elif self.kbd_live and self.kbd_keys:
            self.say("Let go of the keys to arm")
        else:
            self.end_tune()
            self.armed = True
            self.gpio.write(self.enable_pin, 1)
            if self.led_pin >= 0:
                self.gpio.write(self.led_pin, 1)
            self.say("ARMED" + (" (keyboard)" if source == "keyboard" else ""))
            if self.sound_on:
                self.start_tune("arm")

    def disarm(self, why):
        # Cut the drivers' enable line FIRST so the motors are off even if a PWM write fails.
        try:
            self.gpio.write(self.enable_pin, 0)
        except Exception as ex:
            log.error(f"failed to drop enable pin: {ex}")
        for t in self.thrusters:
            try:
                t.stop(self.gpio)
            except Exception as ex:
                log.error(f"failed to stop motor {t.name}: {ex}")
        if self.led_pin >= 0:
            self.gpio.write(self.led_pin, 0)
        if self.armed:
            self.say(f"DISARMED ({why})")
        self.armed = False
        self.tune = None
        self.ap.off(None)        # autopilot mode ends with every disarm (the DISARMED message says why)

    def say(self, msg):
        self.status_msg = msg
        log.info(msg)

    # ---- input ----
    def handle_buttons(self, presses):
        e = self.pad.e
        for code in presses:
            if code in (e.BTN_START,):
                self.arm()
            elif code in (e.BTN_EAST, e.BTN_SELECT, e.BTN_MODE):
                self.disarm("button")
            elif code == e.BTN_NORTH:
                self.switch_profile()
            elif code == e.BTN_WEST:
                self.view_toggle += 1
            elif code == e.BTN_TR:
                self.change_speed(1)
            elif code == e.BTN_TL:
                self.change_speed(-1)

    # ---- profiles ----
    def load_profiles(self, active=None):
        """Load profiles.json (creating it with the defaults if missing). `active` = name or 1-based number."""
        path = profiles_path(self.config_path)
        profs = None
        try:
            with open(path) as f:
                profs = clean_profiles(json.load(f))
        except FileNotFoundError:
            try:
                with open(path, "w") as f:
                    json.dump({"profiles": DEFAULT_PROFILES}, f, indent=2)
            except OSError:
                pass
        except (OSError, ValueError) as ex:
            log.error(f"profiles.json unreadable ({ex}) - using built-in profiles")
        self.profiles = profs or clean_profiles({"profiles": DEFAULT_PROFILES})
        names = [p["name"] for p in self.profiles]
        if isinstance(active, int) and 1 <= active <= len(names):
            self.profile_idx = active - 1
        elif active in names:
            self.profile_idx = names.index(active)
        else:
            self.profile_idx = names.index("Standard") if "Standard" in names else 0

    def active_profile(self):
        return self.profiles[self.profile_idx]

    def set_profile(self, idx):
        self.profile_idx = idx % len(self.profiles)
        name = self.active_profile()["name"]
        saved = self.save_setting("drive_profile", json.dumps(name), section="[control]")
        self.say(f"Drive profile {self.profile_idx + 1}/{len(self.profiles)}: {name}"
                 + ("" if saved else " (could not save!)"))

    def switch_profile(self):
        """TRIANGLE / screen button: next profile (only while SAFE)."""
        if self.armed:
            self.say("Disarm first (STOP) to change drive profile")
            return
        self.set_profile(self.profile_idx + 1)

    def reload_profiles(self, use_idx):
        """The screen editor saved profiles.json. use_idx >= 0 also makes that profile active."""
        if self.armed:
            self.disarm("profiles changed")
        current = self.active_profile()["name"]
        self.load_profiles(current)
        if 0 <= use_idx < len(self.profiles):
            self.set_profile(use_idx)
        else:
            self.say(f"Profiles saved ({len(self.profiles)}). Active: {self.active_profile()['name']}")

    def save_setting(self, key, value, section):
        """Set `key = value` in config.toml (first match, else add under `section`)."""
        if not self.config_path:
            return False
        try:
            with open(self.config_path) as f:
                lines = f.read().split("\n")
            for k, line in enumerate(lines):
                if line.split("=")[0].strip() == key:
                    lines[k] = f"{key} = {value}"
                    break
            else:
                if section not in lines:
                    lines += ["", section]
                lines.insert(lines.index(section) + 1, f"{key} = {value}")
            tmp = self.config_path + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(lines))
            os.replace(tmp, self.config_path)
            return True
        except (OSError, ValueError) as ex:
            log.error(f"saving config failed: {ex}")
            return False

    def change_speed(self, d):
        self.level = max(0, min(self.level + d, len(self.levels) - 1))
        self.say(f"Speed {int(self.levels[self.level] * 100)}%")

    def lose_pad(self, why):
        self.disarm(why)
        if self.pad:
            self.say(f"Controller lost: {self.pad.name} ({why})")
            self.pad.close()
        self.pad = None

    def input_value(self, key):
        """Control position, -1..1 (triggers 0..1). Up / right / pressed = positive."""
        a = self.pad.axes
        if key in ("ly", "ry"):
            return shape(-a[key], self.deadzone, self.expo)
        if key in ("lx", "rx"):
            return shape(a[key], self.deadzone, self.expo)
        if key in ("l2", "r2"):
            return a[key]
        if key == "dy":
            return -a.get("dy", 0.0)
        return a.get("dx", 0.0)

    def command(self):
        """Turn controller positions (active profile) + held keyboard keys into motion
        (surge/yaw/heave/sway/pitch)."""
        cmd = dict.fromkeys(AXES, 0.0)
        for key, m in (self.active_profile()["map"].items() if self.pad else ()):
            vec = ACTIONS.get(m["action"], {})
            if not vec:
                continue
            v = self.input_value(key) * (-1.0 if m["invert"] else 1.0) * m["power"]
            for axis, w in vec.items():
                cmd[axis] += v * w
        if self.kbd_live:
            for k in self.kbd_keys:
                axis, v = KEY_AXES[k]
                cmd[axis] += v
        return {ax: self.inv[ax] * max(-1.0, min(1.0, cmd[ax])) for ax in AXES}

    # ---- keyboard drive ----
    def read_keys(self):
        """Keys held on the case keyboard (sent by the dashboard ~20x a second)."""
        self.kbd_live = False
        self.kbd_keys = set()
        if not self.kbd_enabled:
            return
        try:
            with open(KEYS_PATH) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return
        if abs(time.time() - float(st.get("ts", 0))) > self.hb_timeout:
            return
        self.kbd_live = True
        self.kbd_keys = {k for k in st.get("keys", []) if k in KEY_AXES}

    def set_keyboard(self, on):
        if self.armed:
            self.disarm("keyboard drive switched")
        self.kbd_enabled = on
        saved = self.save_setting("keyboard_drive", "true" if on else "false", section="[control]")
        self.say(("Keyboard drive ON: Enter = arm, Esc = stop" if on else "Keyboard drive OFF")
                 + ("" if saved else " (could not save!)"))

    # ---- arm sound (motor music) ----
    def set_sound(self, on=None, volume=None):
        if on is not None:
            self.sound_on = on
            self.save_setting("arm_sound", "true" if on else "false", section="[control]")
        if volume is not None:
            self.sound_volume = max(1, min(15, volume))
            self.save_setting("sound_volume", str(self.sound_volume), section="[control]")
        self.say(f"Arm sound {'ON' if self.sound_on else 'OFF'}, volume {self.sound_volume}")

    def test_sound(self):
        """Play the arm tune while SAFE (drivers enabled just for the tune)."""
        if self.armed:
            self.say("Sound test only while SAFE")
        elif not self.output_ready():
            self.say("Can't play: " + getattr(self.gpio, "describe", lambda: "motor output not ready")())
        elif not self.switch_on():
            self.say("Panel ARM switch is OFF")
        else:
            self.gpio.write(self.enable_pin, 1)
            self.start_tune("arm", test=True)
            self.say("Playing arm sound...")

    def load_tune(self):
        """Arm tune: tune.txt (your ringtone) if it's there and valid, else the built-in one."""
        text = ""
        try:
            with open(self.tune_path) as f:
                text = f.read().strip()
        except OSError:
            pass
        self.tune_name, notes, self.tune_custom = "FPV arm", TUNES["fpv"], False
        if text:
            try:
                self.tune_name, notes = parse_rtttl(text)
                self.tune_custom = True
            except ValueError as ex:
                log.warning(f"tune.txt ignored: {ex}")
        TUNES["arm"] = notes
        self.tune_secs = sum(d for _, d in notes)

    def set_tune(self, text):
        """New arm tune from the screen ('' = back to the built-in one), saved in tune.txt."""
        text = " ".join(text.split())
        if text:
            try:
                name, notes = parse_rtttl(text)
            except ValueError as ex:
                self.say(f"Tune not saved: {ex}")
                return
        try:
            if text:
                with open(self.tune_path, "w") as f:
                    f.write(text + "\n")
            elif os.path.exists(self.tune_path):
                os.remove(self.tune_path)
        except OSError as ex:
            self.say(f"Tune not saved: {ex.strerror or ex}")
            return
        self.load_tune()
        self.say(f"Arm tune: {self.tune_name} ({self.tune_secs:.1f} s)")

    def start_tune(self, name, test=False):
        self.tune = {"notes": TUNES[name], "t0": time.monotonic(), "cur": None, "test": test}

    def end_tune(self):
        if not self.tune:
            return
        test, self.tune = self.tune["test"], None
        for t in self.thrusters:
            t.stop(self.gpio)
        if test and not self.armed:
            self.gpio.write(self.enable_pin, 0)

    def play_tune(self):
        """Advance the tune. Each note drives every motor at the note's frequency with a tiny duty,
        alternating direction note by note so the props don't creep."""
        elapsed = time.monotonic() - self.tune["t0"]
        notes = self.tune["notes"]
        i, end = 0, 0.0
        for i, (_, dur) in enumerate(notes):
            end += dur
            if elapsed < end:
                break
        else:
            self.end_tune()
            return
        if i == self.tune["cur"]:
            return
        self.tune["cur"] = i
        freq = notes[i][0]
        for t in self.thrusters:
            on, off = (t.rpwm, t.lpwm) if i % 4 < 2 else (t.lpwm, t.rpwm)
            self.gpio.pwm(off, 0)
            if freq:
                self.gpio.tone(on, freq, self.sound_volume)
            else:
                self.gpio.pwm(on, 0)
            t._sent = (-1, -1)   # force a fresh write when driving resumes

    # ---- loop ----
    def run(self):
        next_scan = 0.0
        next_tick = time.monotonic()
        next_print = 0.0
        tty = sys.stdout.isatty()
        self.say("Waiting for controller (press PS button to connect)...")
        while self.running:
            now = time.monotonic()
            if self.pad is None and now >= next_scan:
                next_scan = now + 1.0
                self.pad = find_gamepad(self.preferred)
                if self.pad:
                    hb = "heartbeat ok" if self.pad.motion else "no heartbeat available"
                    self.say(f"Controller connected: {self.pad.name} ({hb}). Press OPTIONS to arm.")

            timeout = max(0.0, next_tick - time.monotonic())
            if self.pad:
                try:
                    ready, _, _ = select.select(self.pad.fds(), [], [], timeout)
                    for fd in ready:
                        self.handle_buttons(self.pad.read(fd))
                except OSError as ex:
                    self.lose_pad(f"disconnected: {ex.strerror or ex}")
                    continue
            else:
                time.sleep(timeout)

            if time.monotonic() < next_tick:
                continue
            next_tick += TICK
            if next_tick < time.monotonic():  # fell behind, don't try to catch up
                next_tick = time.monotonic() + TICK
            self.tick()
            if time.monotonic() >= next_print:
                next_print = time.monotonic() + 0.2
                self.write_status_file()
                if tty:
                    self.print_status()

    def tick(self):
        try:
            self._tick()
        finally:
            if hasattr(self.gpio, "flush"):
                self.gpio.flush()   # Mega: send the pin state every tick (also acts as its heartbeat)

    def _tick(self):
        self.check_screen_commands()
        if self.armed and not self.output_ready():
            self.disarm("Mega connection lost")
        if self.pad and self.pad.motion and time.monotonic() - self.pad.last_motion > self.hb_timeout:
            self.lose_pad("heartbeat timeout")
        if self.armed and not self.switch_on():
            self.disarm("panel switch off")
        self.read_keys()
        if self.armed and not self.pad and not self.kbd_live:
            self.disarm("keyboard link lost" if self.kbd_enabled else "no controller")
        if self.tune and (self.pilot_input() or self.ap.mode):
            self.end_tune()          # the pilot wants to drive: stop the music at once
            if self.armed:
                self.say("ARMED")
        if self.tune:
            self.play_tune()
            return
        if not self.armed:
            return
        self.last_pilot = self.command()
        cmd = self.ap.apply(self.last_pilot)
        targets = mix(self.thrusters, {a: max(-1.0, min(1.0, v)) for a, v in cmd.items()}, self.levels[self.level])
        step = self.ramp_rate * TICK
        for t, target in zip(self.thrusters, targets):
            t.apply(self.gpio, ramp(t.value, target, step), self.min_duty)

    def write_status_file(self, path=STATUS_PATH):
        """JSON status for the on-screen GUI (rov_gui.py)."""
        st = {
            "ts": time.time(),
            "armed": self.armed,
            "controller": self.pad.name if self.pad else None,
            "heartbeat": bool(self.pad and self.pad.motion),
            "speed": int(self.levels[self.level] * 100),
            "profile": self.profile_idx + 1,
            "profile_count": len(self.profiles),
            "profile_name": self.active_profile()["name"],
            "profile_map": self.active_profile()["map"],
            "view_toggle": self.view_toggle,
            "motors": [{"name": t.name, "value": round(t.value, 3), "invert": t.invert, "job": t.job()}
                       for t in self.thrusters],
            "axes": dict(self.pad.axes) if self.pad else None,
            "switch_on": self.switch_on(),
            "output_ok": bool(self.output_ready()),
            "output": getattr(self.gpio, "describe", lambda: "Raspberry Pi GPIO")(),
            "keyboard_drive": self.kbd_enabled,
            "keyboard_live": self.kbd_live,
            "keys": sorted(self.kbd_keys),
            "arm_sound": self.sound_on,
            "sound_volume": self.sound_volume,
            "playing": bool(self.tune),
            "tune_name": self.tune_name,
            "tune_secs": round(self.tune_secs, 1),
            "tune_custom": self.tune_custom,
            "autopilot": self.ap.status(),
            "msg": self.status_msg,
        }
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(st, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass

    def check_screen_commands(self):
        """Commands from the GUI. Arming from the screen is only possible with keyboard drive ON (Enter)."""
        try:
            with open(CMD_PATH) as f:
                cmd = f.read().strip()
            os.remove(CMD_PATH)
        except OSError:
            return
        if cmd == "stop":
            self.disarm("screen STOP button")
            self.say("STOPPED - press OPTIONS" + (" or Enter" if self.kbd_enabled else "") + " to re-arm")
        elif cmd == "speed+":
            self.change_speed(1)
        elif cmd == "speed-":
            self.change_speed(-1)
        elif cmd == "profile":
            self.switch_profile()
        elif cmd.startswith("reload_profiles:"):
            self.reload_profiles(int(cmd.split(":")[1]))
        elif cmd.startswith("invert:"):
            self.flip_motor(int(cmd.split(":")[1]))
        elif cmd == "kbd_arm":
            self.read_keys()
            self.arm(source="keyboard")
        elif cmd.startswith("keyboard:"):
            self.set_keyboard(cmd.endswith(":1"))
        elif cmd.startswith("sound:"):
            self.set_sound(on=cmd.endswith(":1"))
        elif cmd.startswith("volume:"):
            self.set_sound(volume=int(cmd.split(":")[1]))
        elif cmd == "sound_test":
            self.test_sound()
        elif cmd == "sound_stop":
            self.end_tune()
        elif cmd.startswith("tune:"):
            self.set_tune(cmd[5:])
        elif cmd.startswith("ap_enable:"):
            self.ap.set_enabled(cmd.endswith(":1"))
        elif cmd == "ap_off":
            self.ap.off("switched off on screen")
        elif cmd.startswith("ap:"):
            self.ap.engage(cmd[3:], self.last_pilot if self.armed else self.command())
        elif cmd.startswith("ap_set:"):
            _, key, value = cmd.split(":", 2)
            self.ap.set(key, value)

    def flip_motor(self, idx):
        """Reverse one motor's direction (only while SAFE) and save it to config.toml."""
        if not 0 <= idx < len(self.thrusters):
            return
        if self.armed:
            self.say("Disarm first (STOP) before flipping a motor")
            return
        t = self.thrusters[idx]
        t.stop(self.gpio)
        t.invert = not t.invert
        saved = self.save_invert(idx, t.invert)
        state = "FLIPPED" if t.invert else "NORMAL"
        self.say(f"{t.name}: {state}" + ("" if saved else " (could not save to config!)"))

    def save_invert(self, idx, value):
        """Rewrite just the `invert =` line of the idx-th [[motor]] block, keeping comments."""
        if not self.config_path:
            return False
        try:
            with open(self.config_path) as f:
                lines = f.read().split("\n")
            n = -1
            for k, line in enumerate(lines):
                if line.strip() == "[[motor]]":
                    n += 1
                elif n == idx and line.strip().startswith("invert"):
                    lines[k] = f"invert = {'true' if value else 'false'}"
                    break
            else:
                return False
            tmp = self.config_path + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(lines))
            os.replace(tmp, self.config_path)
            return True
        except OSError as ex:
            log.error(f"saving config failed: {ex}")
            return False

    def print_status(self):
        state = "\033[42;30m ARMED \033[0m" if self.armed else "\033[41;37m SAFE  \033[0m"
        pad = self.pad.name[:28] if self.pad else "no controller"
        bars = "  ".join(f"{t.name[:10]:>10} {t.value * 100:+4.0f}%" for t in self.thrusters)
        line = f"\r{state} spd {int(self.levels[self.level] * 100):3d}% | {pad:<28} | {bars} | {self.status_msg[:40]:<40}"
        sys.stdout.write(line)
        sys.stdout.flush()

    def shutdown(self):
        self.disarm("shutdown")
        if self.pad:
            self.pad.close()
        self.gpio.close()


# --------------------------------------------------------------------------- modes

def motor_test(rov):
    print("\nMOTOR TEST - each motor runs at 30% forward, then reverse.")
    print("Keep hands and cables clear of the props. Ctrl+C to stop at any time.\n")
    rov.gpio.write(rov.enable_pin, 1)
    try:
        for t in rov.thrusters:
            input(f"Press Enter to test motor '{t.name}' (RPWM GPIO{t.rpwm}, LPWM GPIO{t.lpwm})...")
            for label, v in (("FORWARD (+)", 0.3), ("REVERSE (-)", -0.3)):
                print(f"   {label}")
                t.apply(rov.gpio, v, rov.min_duty)
                time.sleep(1.5)
                t.stop(rov.gpio)
                time.sleep(0.5)
        print("\nDone. If a motor spun the wrong way set invert = true for it in config.toml.")
        print("If the wrong motor spun, swap the names/coefficients in config.toml.")
    finally:
        rov.disarm("test finished")


def list_devices_cmd():
    from evdev import InputDevice, list_devices
    for p in list_devices():
        d = InputDevice(p)
        print(f"{p:<20} {d.name:<45} uniq={d.uniq}")


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if len(cfg.get("motor", [])) == 0:
        raise SystemExit("config has no [[motor]] entries")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Underwater ROV controller")
    ap.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    ap.add_argument("--test", action="store_true", help="spin each motor to identify it")
    ap.add_argument("--dry-run", action="store_true", help="no GPIO; show what motors would do")
    ap.add_argument("--list", action="store_true", help="list input devices")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        stream=sys.stderr if sys.stdout.isatty() else sys.stdout)
    if sys.stdout.isatty():
        logging.getLogger().setLevel(logging.WARNING)  # status line shows messages instead

    if args.list:
        return list_devices_cmd()

    cfg = load_config(args.config)
    g = cfg.get("gpio", {})
    out = cfg.get("output", {})
    if args.dry_run:
        gpio = FakeBackend()
    elif out.get("type", "mega") == "mega":
        gpio = MegaBackend(out.get("port", "auto"), out.get("baud", 115200))
    else:
        gpio = LgpioBackend(g.get("chip", 0), g.get("pwm_frequency", 1000))
    rov = ROV(cfg, gpio, args.config)

    def stop(*_):
        rov.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        if args.test:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            motor_test(rov)
        else:
            rov.run()
    except KeyboardInterrupt:
        pass
    finally:
        rov.shutdown()
        print("\nMotors stopped.")


if __name__ == "__main__":
    main()
