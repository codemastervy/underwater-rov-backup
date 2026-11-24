"""The USB capture card (or another camera of the laptop). ffmpeg decodes it and applies FLIP and PICTURE; changes are sent to it while it runs."""
import ctypes
import fcntl
import glob
import json
import os
import signal
import subprocess
import threading
import time
from collections import namedtuple

import pygame

import rov as core

from .link import write_file

WIDTH, HEIGHT = 640, 480
RETRY = 3.0               # seconds before trying a camera that failed again
LOOK_EVERY = 2.0          # seconds between looking which cameras are plugged in
CAPTURE_CARDS = ("pc camera", "capture", "av to usb")
BUILT_IN = ("integrated", "webcam", "hd camera", "hd user facing", "facetime", "chicony", "ir camera")
STUCK = 4.0               # seconds without a new picture: the capture card hangs, start again
COMMAND_EVERY = 0.1       # ffmpeg reads one command in this time
PIPE_SIZE, F_SETPIPE_SZ = 1 << 20, 1031       # a whole frame fits, so reading it takes one or two calls
PR_SET_PDEATHSIG = 1

FLIPS = ["none", "rotate", "mirror"]
FLIP_TEXT = {"none": "", "rotate": "UPSIDE DOWN", "mirror": "MIRRORED"}

Adjustment = namedtuple("Adjustment", "key label low high step default show")
PICTURE = [
    Adjustment("gain", "ISO", 0.25, 4.0, 0.25, 1.0, lambda v: f"{v * 100:.0f}"),       # ISO 100 = normal
    Adjustment("brightness", "BRIGHTNESS", -0.5, 0.5, 0.05, 0.0, lambda v: f"{round(v * 20):+d}"),
    Adjustment("contrast", "CONTRAST", 0.5, 2.0, 0.1, 1.0, lambda v: f"x{v:.1f}"),
    Adjustment("saturation", "COLOUR", 0.0, 3.0, 0.1, 1.0, lambda v: f"x{v:.1f}"),
    Adjustment("red", "RED", 0.0, 2.0, 0.05, 1.0, lambda v: f"{v * 100:.0f}%"),
    Adjustment("green", "GREEN", 0.0, 2.0, 0.05, 1.0, lambda v: f"{v * 100:.0f}%"),
    Adjustment("blue", "BLUE", 0.0, 2.0, 0.05, 1.0, lambda v: f"{v * 100:.0f}%"),
]

Frame = namedtuple("Frame", "number time data image")      # image: surface in the size of Camera.view


def settings_path():
    return os.path.join(core.HERE, "camera.json")


def configured_camera():
    """[camera] device in config.toml, or None for "auto"."""
    try:
        device = str(core.load_config(os.path.join(core.HERE, "config.toml")).get("camera", {}).get("device", "auto"))
    except (OSError, ValueError, AttributeError, SystemExit):
        return None
    return None if device == "auto" else device


def list_cameras():
    """[(device, name)] of the cameras that are plugged in (one entry per camera)."""
    found = []
    for path in sorted(glob.glob("/sys/class/video4linux/video*"), key=lambda p: int(p.rsplit("video", 1)[1])):
        try:
            with open(os.path.join(path, "name")) as name, open(os.path.join(path, "index")) as index:
                if int(index.read()) == 0:
                    found.append(("/dev/" + os.path.basename(path), name.read().strip()))
        except (OSError, ValueError):
            continue
    return found


def pick_camera(cameras, configured, picked):
    """The camera to show: the one in config.toml, else the one picked on the CAMERA page, else the USB capture
    card (the laptop's own webcam only if there is nothing else). None without a camera."""
    if configured:
        return configured
    if picked in [device for device, _ in cameras]:
        return picked
    names = [(device, name.lower()) for device, name in cameras]
    cards = [device for device, name in names if any(word in name for word in CAPTURE_CARDS)]
    others = [device for device, name in names if not any(word in name for word in BUILT_IN)]
    return next(iter(cards + others + [device for device, _ in names]), None)


def load_settings():
    """camera.json -> (flip, picture, picked camera or None)."""
    try:
        with open(settings_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    saved = data.get("picture") if isinstance(data.get("picture"), dict) else {}
    if "warmth" in saved and "red" not in saved:            # older setting
        try:
            warmth = float(saved["warmth"])
            saved = {**saved, "red": 1 + 0.35 * warmth, "blue": 1 - 0.35 * warmth}
        except (TypeError, ValueError):
            pass
    picture = {}
    for a in PICTURE:
        try:
            picture[a.key] = max(a.low, min(a.high, float(saved.get(a.key, a.default))))
        except (TypeError, ValueError):
            picture[a.key] = a.default
    device = data.get("device")
    return (data.get("flip") if data.get("flip") in FLIPS else "none", picture,
            device if isinstance(device, str) and device else None)


def _die_with_parent():
    """Runs in ffmpeg before it starts: if the dashboard dies, so does ffmpeg (it would keep the camera busy)."""
    try:
        ctypes.CDLL("libc.so.6").prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


class Camera:
    def __init__(self):
        self.flip, self.picture, self.picked = load_settings()
        self.configured = configured_camera()
        self.cameras = list_cameras()
        self.frame = None
        self.error = ""
        self.view = None              # size the page shows the picture in
        self._process = None
        self._started = 0.0
        self._applied = {}
        self._retry_at = self._command_at = self._look_at = 0.0

    # ------------------------------------------------------------ settings
    def next_flip(self):
        self.flip = FLIPS[(FLIPS.index(self.flip) + 1) % len(FLIPS)]
        self._save()

    def adjust(self, key, value):
        self.picture[key] = value
        self._save()

    def reset_picture(self):
        self.picture = {a.key: a.default for a in PICTURE}
        self._save()

    @property
    def device(self):
        return pick_camera(self.cameras, self.configured, self.picked)

    def pick(self, device):
        self.picked = device
        self._save()
        self.stop()
        self._retry_at = 0.0

    def _save(self):
        settings = {"flip": self.flip, "picture": self.picture}
        if self.picked:
            settings["device"] = self.picked
        write_file(settings_path(), json.dumps(settings))

    def _filters(self):
        """ffmpeg's filter options for the current settings: {(filter, option): value}."""
        p = self.picture
        options = {("hflip", "enable"): str(int(self.flip != "none")),
                   ("vflip", "enable"): str(int(self.flip == "rotate"))}
        options.update({("eq", k): f"{p[k]:.3f}" for k in ("brightness", "contrast", "saturation")})
        options.update({("lutrgb", k[0]): f"val*{p['gain'] * p[k]:.4f}" for k in ("red", "green", "blue")})
        return options

    def _filter_chain(self):
        filters = {}
        for (name, option), value in self._applied.items():
            filters.setdefault(name, []).append(f"{option}={value}")
        chain = {name: f"{name}=" + ":".join(options) for name, options in filters.items()}
        return f"{chain['hflip']},{chain['vflip']},{chain['eq']},format=rgb24,{chain['lutrgb']}," \
               f"scale={WIDTH}:{HEIGHT}"

    # ------------------------------------------------------------ running ffmpeg
    @property
    def running(self):
        return self._process is not None and self._process.poll() is None

    @property
    def fresh(self):
        return self.frame is not None and time.monotonic() - self.frame.time < 0.5

    def source(self):
        """ffmpeg's input options, or None without a camera."""
        device = self.device
        return device and ["-f", "v4l2", "-input_format", "mjpeg", "-video_size", f"{WIDTH}x{HEIGHT}",
                           "-framerate", "30", "-i", device]

    def update(self, now, wanted):
        if not wanted:
            self.stop()
            return
        if now >= self._look_at:
            self._look_at = now + LOOK_EVERY
            self.cameras = list_cameras()
        if not self.running:
            if now >= self._retry_at:
                self._retry_at = now + RETRY
                self._start()
        elif now - max(self._started, self.frame.time if self.frame else 0.0) > STUCK:
            self.stop()                   # never leave an old picture on the screen as if it were live
            self.error, self._retry_at = "No picture from the camera, trying again…", now + 1.0
        elif now >= self._command_at:
            self._send_change(now)

    def _start(self):
        self.stop()
        source = self.source()
        if not source:
            self.error = "No camera / capture card found"
            return
        self.error = ""
        self._started = time.monotonic()
        self._applied = self._filters()
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-stats_period", "0.05",
                   "-fflags", "nobuffer", "-flags", "low_delay", *source,
                   "-vf", self._filter_chain(), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, bufsize=0, preexec_fn=_die_with_parent)
        except OSError as ex:
            self.error = f"ffmpeg failed: {ex}"
            return
        for job in (self._read_frames, self._read_messages):
            threading.Thread(target=job, args=(self._process,), daemon=True).start()

    def stop(self):
        process, self._process, self.frame = self._process, None, None
        if process:
            process.kill()
            process.wait()

    def _send_change(self, now):
        for option, value in self._filters().items():
            if self._applied[option] != value:
                self._applied[option] = value
                self._command_at = now + COMMAND_EVERY
                try:
                    self._process.stdin.write(f"c{option[0]} -1 {option[1]} {value}\n".encode())
                except OSError:
                    pass
                return

    def _read_frames(self, process):
        pipe = process.stdout
        try:
            fcntl.fcntl(pipe, F_SETPIPE_SZ, PIPE_SIZE)
        except OSError:
            pass
        buffer = bytearray(WIDTH * HEIGHT * 3)
        number = 0
        while True:
            filled = 0
            while filled < len(buffer):
                count = pipe.readinto(memoryview(buffer)[filled:])
                if not count:
                    return
                filled += count
            number += 1
            data = bytes(buffer)
            frame = Frame(number, time.monotonic(), data, self._image(data))
            if self._process is not process:
                return
            self.frame = frame

    def _image(self, data):
        if not self.view:
            return None
        image = pygame.Surface((WIDTH, HEIGHT), 0, 32)
        image.blit(pygame.image.frombuffer(data, (WIDTH, HEIGHT), "RGB"), (0, 0))
        return pygame.transform.smoothscale(image, self.view)

    def _read_messages(self, process):
        """What ffmpeg says: answers to our commands, and why it stopped."""
        last = ""
        for line in process.stderr:
            text = line.decode(errors="replace").strip()
            if text.startswith("Command reply"):
                if "ret:-" in text and self._process is process:
                    process.kill()             # this ffmpeg can't change it live: start again with the new value
                    self._retry_at = 0.0
            elif text and not text.startswith("Enter command"):
                last = text
        if self._process is process:
            self.error = "Camera stopped" + (f": {last[:60]}" if last else "")
