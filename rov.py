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
        self.duty[pin] = max(0, min(100, duty))

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
        frame = f"F {self.en_pin} {self.en_val if self.ready() else 0} " + " ".join(
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

    def sticks_centred(self):
        a = self.pad.axes
        return (all(abs(a[k]) < self.deadzone * 2 for k in ("lx", "ly", "rx", "ry"))
                and a["l2"] < 0.1 and a["r2"] < 0.1 and abs(a.get("dy", 0)) < 0.5 and abs(a.get("dx", 0)) < 0.5)

    def output_ready(self):
        return getattr(self.gpio, "ready", lambda: True)()

    def arm(self):
        if not self.output_ready():
            self.say("Can't arm: " + getattr(self.gpio, "describe", lambda: "motor output not ready")())
        elif not self.switch_on():
            self.say("Panel ARM switch is OFF - can't arm")
        elif not self.sticks_centred():
            self.say("Centre the sticks and release triggers to arm")
        else:
            self.armed = True
            self.gpio.write(self.enable_pin, 1)
            if self.led_pin >= 0:
                self.gpio.write(self.led_pin, 1)
            self.say("ARMED")

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
        """Turn controller positions into motion (surge/yaw/heave/sway/pitch) using the active profile."""
        cmd = dict.fromkeys(AXES, 0.0)
        for key, m in self.active_profile()["map"].items():
            vec = ACTIONS.get(m["action"], {})
            if not vec:
                continue
            v = self.input_value(key) * (-1.0 if m["invert"] else 1.0) * m["power"]
            for axis, w in vec.items():
                cmd[axis] += v * w
        return {ax: self.inv[ax] * max(-1.0, min(1.0, cmd[ax])) for ax in AXES}

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
        if not self.armed or not self.pad:
            return
        targets = mix(self.thrusters, self.command(), self.levels[self.level])
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
            "msg": self.status_msg,
        }
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(st, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass

    def check_screen_commands(self):
        """Commands from the GUI: 'stop', 'speed+', 'speed-'. (Arming is controller-only.)"""
        try:
            with open(CMD_PATH) as f:
                cmd = f.read().strip()
            os.remove(CMD_PATH)
        except OSError:
            return
        if cmd == "stop":
            self.disarm("screen STOP button")
            self.say("STOPPED from screen - press OPTIONS on controller to re-arm")
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
