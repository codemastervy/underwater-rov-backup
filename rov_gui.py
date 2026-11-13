#!/usr/bin/env python3
"""ROV dashboard - LAPTOP BACKUP version (window on the laptop screen; motors via Arduino Mega).
Run with --fullscreen for full screen, --size 1200x720 for another window size.

Shows what rov.py (the systemd service) is doing and offers STOP / speed buttons.
No mouse click can arm the motors. Arming is on the controller (OPTIONS), or Enter on the
keyboard when KEYBOARD DRIVE is switched on (SETUP > EXTRAS). If this window closes, the ROV
keeps running normally (keyboard driving stops).

Keys: Esc = STOP, Space = STOP (keyboard drive off), +/- = speed, P = profile, C = camera, F11 = full screen.
Keyboard drive on: W/S forward/back, A/D turn, Space up, Shift down, R/F tilt, Enter = ARM.
"""
import colorsys
import glob
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk

import rov as core   # profile definitions (INPUTS, ACTIONS, DEFAULT_PROFILES) live in rov.py
try:
    import updater       # automatic updates from GitHub (SETUP > EXTRAS)
except ImportError:
    updater = None

STATUS = "/dev/shm/rov-status.json"
CMD = "/dev/shm/rov-cmd"
KEYS = "/dev/shm/rov-keys.json"
VISION = "/dev/shm/rov-vision.json"
KB_KEYS = {"w", "a", "s", "d", "r", "f", "space", "shift"}
W, H = 800, 480
# Safe area: this monitor crops the edges of the HDMI picture. Increase these if
# anything is still cut off (left, top, right, bottom, in pixels). Or run with
#   --margins 30,20,30,20
MARGINS = (32, 22, 32, 22)

BG, PANEL, TRACK = "#0d1117", "#1f2630", "#4a5361"
FG, DIM = "#ffffff", "#b4bcc6"
GREEN, RED, AMBER, BLUE = "#2ea043", "#da3633", "#d29922", "#388bfd"
F = "DejaVu Sans"


def send(cmd):
    try:
        with open(CMD + ".tmp", "w") as f:
            f.write(cmd)
        os.replace(CMD + ".tmp", CMD)
    except OSError:
        pass


def short(text, n=66):
    return text if len(text) <= n else text[: n - 1] + "…"


ACTION_TEXT = {"none": "—  nothing", "forward": "forward / back", "turn": "turn", "updown": "up / down",
               "tilt": "tilt (nose)", "sideways": "sideways", "left_side": "left side (tank)",
               "right_side": "right side (tank)"}
STICK_WORD = {"none": "", "forward": "fwd/back", "turn": "turn", "updown": "up/down", "tilt": "tilt",
              "sideways": "sideways", "left_side": "left side", "right_side": "right side"}
ONE_WAY = {"forward": ("forward", "back"), "turn": ("turn R", "turn L"), "updown": ("up", "down"),
           "tilt": ("nose up", "nose down"), "sideways": ("right", "left"), "left_side": ("left fwd", "left back"),
           "right_side": ("right fwd", "right back")}


def stick_label(pmap, ky, kx):
    words = [STICK_WORD[pmap.get(k, {}).get("action", "none")] for k in (ky, kx)]
    return " + ".join(w for w in words if w) or "—"


def push_label(pmap, k, positive=True):
    """What pressing a one-way control (trigger / one D-pad direction) does."""
    e = pmap.get(k, {})
    a = e.get("action", "none")
    if a == "none":
        return "—"
    pos, neg = ONE_WAY[a]
    return pos if positive != bool(e.get("invert")) else neg


def pad_name(name):
    if "DualSense" in name:
        return "PS5 DualSense"
    if "Wireless Controller" in name:
        return "PS4 controller"
    return short(name, 22)


def read_status():
    try:
        with open(STATUS) as f:
            st = json.load(f)
        return st if time.time() - st.get("ts", 0) < 2.0 else None
    except (OSError, ValueError):
        return None


SX = SY = SCALE = 1.0


CW, CH = W, H          # real canvas size in pixels
OX = OY = 0.0          # where the 800x480 design starts on the canvas


def set_view(cw, ch, m=(0, 0, 0, 0)):
    """Fit the 800x480 design into a cw x ch canvas (uniform scale, centred, inside margins m)."""
    global CW, CH, MARGINS, SX, SY, SCALE, OX, OY
    CW, CH, MARGINS = cw, ch, m
    ml, mt, mr, mb = m
    SCALE = SX = SY = min((cw - ml - mr) / W, (ch - mt - mb) / H)
    OX = ml + ((cw - ml - mr) - W * SCALE) / 2
    OY = mt + ((ch - mt - mb) - H * SCALE) / 2


def set_margins(m):
    set_view(W, H, m)


def X(x):
    return OX + x * SX


def Y(y):
    return OY + y * SY


def fnt(size, *style):
    return (F, max(7, round(size * SCALE)), *style)


def camera_setting():
    try:
        return str(core.load_config(os.path.join(core.HERE, "config.toml")).get("camera", {}).get("device", "auto"))
    except Exception:
        return "auto"


def find_camera():
    """The USB capture card. On a laptop, skip the built-in webcam unless it's the only camera.
    Set [camera] device = "/dev/videoN" in config.toml to force one."""
    setting = camera_setting()
    if setting != "auto":
        return setting
    found = []
    for path in sorted(glob.glob("/sys/class/video4linux/video*"), key=lambda p: int(p.rsplit("video", 1)[1])):
        try:
            name = open(os.path.join(path, "name")).read().strip()
            idx = int(open(os.path.join(path, "index")).read().strip())
        except (OSError, ValueError):
            continue
        if idx == 0 and not name.startswith(("bcm2835", "rpi-")):
            found.append(("/dev/" + os.path.basename(path), name.lower()))
    builtin = ("integrated", "webcam", "hd camera", "hd user facing", "facetime", "chicony", "ir camera")
    for dev, name in found:
        if "pc camera" in name or "capture" in name or "av to usb" in name:
            return dev
    for dev, name in found:
        if not any(b in name for b in builtin):
            return dev
    return found[0][0] if found else None


def _die_with_parent():
    """Runs in ffmpeg before it starts: kill ffmpeg if the dashboard dies (Linux prctl PDEATHSIG),
    so an orphaned ffmpeg can never keep the camera busy."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)   # 1 = PR_SET_PDEATHSIG
    except Exception:
        pass


FLIP_ORDER = ["none", "rotate", "mirror"]
FLIP_FILTERS = {"none": [], "rotate": ["hflip", "vflip"], "mirror": ["hflip"]}
FLIP_TEXT = {"none": "", "rotate": "UPSIDE DOWN", "mirror": "MIRRORED"}

# Picture adjustments (done in ffmpeg, so they work with any capture card):
# key, label, min, max, step, default, how to show the value
PICTURE = [
    ("gain", "GAIN (ISO)", 0.25, 4.0, 0.25, 1.0, lambda v: f"x{v:.2f}"),
    ("brightness", "BRIGHTNESS", -0.5, 0.5, 0.05, 0.0, lambda v: f"{round(v * 20):+d}"),
    ("contrast", "CONTRAST", 0.5, 2.0, 0.1, 1.0, lambda v: f"x{v:.1f}"),
    ("saturation", "COLOUR", 0.0, 3.0, 0.1, 1.0, lambda v: f"x{v:.1f}"),
    ("warmth", "WARMTH", -1.0, 1.0, 0.1, 0.0, lambda v: f"{round(v * 10):+d}"),
]
PICTURE_DEFAULT = {k: d for k, _, _, _, _, d, _ in PICTURE}


def picture_filters(adj):
    """ffmpeg filters for the picture settings (none when everything is at default)."""
    a = dict(PICTURE_DEFAULT, **adj)
    out = []
    g, w = a["gain"], a["warmth"]
    if abs(g - 1) > 1e-6 or abs(w) > 1e-6:
        # gain = brighter sensor (like ISO); warmth = more red / less blue (water makes things blue-green)
        out.append(f"colorchannelmixer=rr={g * (1 + 0.35 * w):.3f}:gg={g:.3f}:bb={g * (1 - 0.35 * w):.3f}")
    if abs(a["brightness"]) > 1e-6 or abs(a["contrast"] - 1) > 1e-6 or abs(a["saturation"] - 1) > 1e-6:
        out.append(f"eq=brightness={a['brightness']:.2f}:contrast={a['contrast']:.2f}"
                   f":saturation={a['saturation']:.2f}")
    return out


def camera_flip_path():
    return os.path.join(core.HERE, "camera.json")


def load_camera_settings():
    """camera.json -> (flip, picture adjustments)."""
    try:
        with open(camera_flip_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    flip = data.get("flip", "none")
    adj = {}
    for k, _, lo, hi, _, d, _ in PICTURE:
        try:
            adj[k] = max(lo, min(hi, float((data.get("picture") or {}).get(k, d))))
        except (TypeError, ValueError, AttributeError):
            adj[k] = d
    return (flip if flip in FLIP_ORDER else "none"), adj


def save_camera_settings(flip, adj):
    try:
        with open(camera_flip_path() + ".tmp", "w") as f:
            json.dump({"flip": flip, "picture": adj}, f)
        os.replace(camera_flip_path() + ".tmp", camera_flip_path())
    except OSError:
        pass


class CameraFeed:
    """ffmpeg reads the capture card (MJPEG) and pipes raw PPM frames; Tk shows the newest one."""

    def __init__(self):
        self.proc = None
        self.frame = None
        self.frame_no = 0
        self.frame_time = 0.0
        self.error = ""

    def start(self, w, h, flip="none", picture=None):
        self.stop()
        dev = find_camera()
        if not dev:
            self.error = "No camera / capture card found"
            return
        self.error = ""
        self.w, self.h = w, h
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "640x480", "-framerate", "30",
               "-i", dev, "-vf", ",".join(FLIP_FILTERS[flip] + [f"scale={w}:{h}"] + picture_filters(picture or {})), "-f", "image2pipe", "-vcodec", "ppm", "-"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                         preexec_fn=_die_with_parent)
        except OSError as ex:
            self.error = f"ffmpeg failed: {ex}"
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        out = proc.stdout
        size = self.w * self.h * 3
        try:
            while True:
                header = out.readline() + out.readline() + out.readline()   # "P6\nW H\n255\n"
                if not header.startswith(b"P6"):
                    break
                data = bytearray()
                while len(data) < size:
                    chunk = out.read(size - len(data))
                    if not chunk:
                        raise EOFError
                    data += chunk
                self.frame = header + bytes(data)
                self.frame_time = time.monotonic()
                self.frame_no += 1
        except (EOFError, OSError, ValueError):
            pass
        if self.proc is proc:
            err = proc.stderr.read().decode(errors="replace").strip().splitlines()
            self.error = "Camera stopped" + (f": {err[-1][:60]}" if err else "")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc:
            p, self.proc = self.proc, None
            p.kill()
            p.wait()
        self.frame = None


class Dashboard:
    def __init__(self, root):
        self.root = root
        root.configure(bg=BG)
        if "--fullscreen" in sys.argv:
            root.attributes("-fullscreen", True)
        else:
            root.geometry(f"{CW}x{CH}")
            root.resizable(False, False)
        # one handler for all keys (Tk runs only the most specific binding per widget, so
        # separate <space>/<w>/... bindings would hide keys from the keyboard-drive handler)
        root.bind("<KeyPress>", self.on_key)
        root.bind("<KeyRelease>", self.on_key_release)
        root.bind("<FocusOut>", lambda e: self.kb_release_all())
        self.kb_on = False          # KEYBOARD DRIVE switch (comes from rov.py status)
        self.kb_down = set()
        self.kb_jobs = {}
        self.page = "main"
        self.last_view_toggle = None
        self.cam = CameraFeed()
        self.cam_flip, self.cam_adj = load_camera_settings()
        self.adj_job = None
        self.cam_retry_at = 0.0
        # autopilot state (from rov.py status)
        self.ap_enabled, self.ap_mode, self.ap_epoch, self.ap_alert, self.ap_info_text = False, None, 0, "", ""
        self.ap_settings, self.ap_local_until = {}, 0.0
        self.vision_result, self.vision_error = {}, ""
        self.upd = updater.Updater() if updater else None
        self.armed = False

        c = self.c = tk.Canvas(root, width=CW, height=CH, bg=BG, highlightthickness=0)
        c.pack(expand=True)
        # everything below is drawn in 800x480 "design" coordinates, then shrunk into the safe area

        # --- top bar
        c.create_rectangle(0, 0, W, 66, fill=PANEL, width=0)
        self.badge = c.create_rectangle(12, 10, 200, 56, fill=AMBER, width=0)
        self.badge_txt = c.create_text(106, 33, text="…", font=fnt(22, "bold"), fill="white")
        self.pad_txt = c.create_text(218, 22, anchor="w", text="", font=fnt(14, "bold"), fill=FG)
        self.pad_sub = c.create_text(218, 46, anchor="w", text="", font=fnt(11), fill=DIM)
        c.create_text(785, 18, anchor="e", text="SPEED", font=fnt(10), fill=DIM)
        self.speed_txt = c.create_text(785, 44, anchor="e", text="--", font=fnt(24, "bold"), fill=FG)
        self.small_button(c, 420, 14, 525, 52, "CAMERA", self.show_camera)
        self.small_button(c, 540, 14, 690, 52, "SETUP", self.show_setup)
        self.kb_badge = c.create_text(470, 62, text="", font=fnt(9, "bold"), fill=AMBER)
        self.ap_badge = c.create_text(615, 62, text="", font=fnt(9, "bold"), fill=GREEN)
        self.upd_badge = c.create_text(300, 62, text="", font=fnt(9, "bold"), fill=BLUE, tags="upd_badge")
        c.tag_bind("upd_badge", "<Button-1>", lambda e: self.show_extras())

        # --- thrusters
        c.create_text(20, 84, anchor="w", text="THRUSTERS", font=fnt(10, "bold"), fill=DIM)
        self.rows = []
        for i in range(4):
            y = 100 + i * 50
            name = c.create_text(20, y + 8, anchor="w", text="", font=fnt(12), fill=FG)
            pct = c.create_text(500, y + 8, anchor="e", text="", font=fnt(12, "bold"), fill=FG)
            c.create_rectangle(20, y + 22, 500, y + 40, fill=TRACK, width=0)
            bar = c.create_rectangle(260, y + 22, 260, y + 40, fill=GREEN, width=0)
            c.create_line(260, y + 18, 260, y + 44, fill=DIM)
            self.rows.append((name, pct, bar))

        # --- sticks / triggers
        c.create_text(530, 84, anchor="w", text="CONTROLLER", font=fnt(10, "bold"), fill=DIM)
        self.tilt_txt = c.create_text(780, 84, anchor="e", text="", font=fnt(10, "bold"), fill=DIM)
        self.sticks = []
        self.stick_labels = []
        for cx, label in ((595, "L  drive"), (725, "R  up/down")):
            cy, r = 160, 48
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=TRACK, width=3)
            c.create_line(cx - r, cy, cx + r, cy, fill=TRACK)
            c.create_line(cx, cy - r, cx, cy + r, fill=TRACK)
            dot = c.create_oval(cx - 9, cy - 9, cx + 9, cy + 9, fill=BLUE, width=0)
            lbl = c.create_text(cx, cy + r + 14, text=label, font=fnt(10), fill=DIM)
            self.sticks.append((dot, cx, cy, r))
            self.stick_labels.append(lbl)
        self.triggers = []
        for x0, label in ((547, "L2 down"), (677, "R2 up")):
            c.create_rectangle(x0, 248, x0 + 96, 262, fill=TRACK, width=0)
            fill = c.create_rectangle(x0, 248, x0, 262, fill=BLUE, width=0)
            lbl = c.create_text(x0 + 48, 276, text=label, font=fnt(9), fill=DIM)
            self.triggers.append((fill, x0, lbl))

        # --- message line
        self.msg_rect = c.create_rectangle(20, 300, 780, 342, fill=PANEL, width=0)
        self.msg = c.create_text(34, 321, anchor="w", text="", font=fnt(13), fill=FG)

        # --- buttons
        self.buttons = {}
        self.stop_lbl = self.button(20, 356, 330, 466, "stop", "STOP", "disarm motors  (Esc)", RED)
        self.profile_lbl = self.button(345, 356, 555, 466, "profile", "PROFILE", "△ next", PANEL)
        self.button(570, 356, 670, 466, "speed-", "SPEED −", "(L1)", PANEL)
        self.button(680, 356, 780, 466, "speed+", "SPEED +", "(R1)", PANEL)

        # thin frame on the edge of the safe area: if you can't see all 4 sides, increase MARGINS
        c.create_rectangle(1, 1, W - 1, H - 1, outline=DIM, width=1)

        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)

        self.build_setup()
        self.build_profiles()
        self.build_extras()
        self.build_autopilot()
        self.build_mission()
        self.build_camera()
        self.update()
        self.refresh_video()
        self.send_keys()
        threading.Thread(target=self.vision_loop, daemon=True).start()

    # ------------------------------------------------------------ motor setup page
    def small_button(self, c, x0, y0, x1, y1, label, action, color=None, tag=None):
        # canvas tags must not contain spaces (Tk would split them into several tags)
        tag = tag or "sb_" + "".join(ch for ch in label if ch.isalnum())
        c.create_rectangle(x0, y0, x1, y1, fill=color or BG, outline=DIM, width=2, tags=tag)
        c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label, font=fnt(12, "bold"), fill=FG, tags=tag)
        c.tag_bind(tag, "<Button-1>", lambda e: action())
        return tag

    # ------------------------------------------------------------ keyboard
    @staticmethod
    def kb_name(event):
        k = event.keysym.lower()
        return "shift" if k in ("shift_l", "shift_r") else k

    def typing(self):
        try:
            return isinstance(self.root.focus_get(), tk.Entry)
        except (KeyError, tk.TclError):
            return False

    def on_key(self, event):
        k = self.kb_name(event)
        if k == "escape":          # STOP works always, even while typing in a text box
            self.press("stop")
            if self.typing():
                self.root.focus_set()
            return
        if self.typing():
            return
        if k == "f11":
            self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))
        elif self.kb_on and k in KB_KEYS:
            job = self.kb_jobs.pop(k, None)
            if job:
                self.root.after_cancel(job)
            self.kb_down.add(k)
        elif self.kb_on and k in ("return", "kp_enter"):
            send("kbd_arm")
        elif k == "space":
            self.press("stop")
        elif k in ("plus", "equal", "kp_add"):
            self.press("speed+")
        elif k in ("minus", "kp_subtract"):
            self.press("speed-")
        elif k == "p":
            self.press("profile")
        elif k == "c":
            self.toggle_camera()

    def on_key_release(self, event):
        k = self.kb_name(event)
        if k in self.kb_down:
            # held keys auto-repeat as release+press pairs: only count a release that isn't followed
            # by a press within 60 ms
            old = self.kb_jobs.pop(k, None)
            if old:
                self.root.after_cancel(old)
            self.kb_jobs[k] = self.root.after(60, lambda: (self.kb_down.discard(k), self.kb_jobs.pop(k, None)))

    def kb_release_all(self):
        for job in self.kb_jobs.values():
            self.root.after_cancel(job)
        self.kb_jobs.clear()
        self.kb_down.clear()

    def send_keys(self):
        """Keyboard drive: tell rov.py which keys are held, 20x a second (this is also its heartbeat)."""
        if self.kb_on:
            try:
                with open(KEYS + ".tmp", "w") as f:
                    json.dump({"ts": time.time(), "keys": sorted(self.kb_down)}, f)
                os.replace(KEYS + ".tmp", KEYS)
            except OSError:
                pass
        else:
            self.kb_release_all()
        self.root.after(50, self.send_keys)

    def setup_tabs(self, c, active):
        self.small_button(c, 12, 12, 112, 54, "◀ BACK", self.show_main, tag=f"back_{active}")
        for x0, x1, name, label, action in ((122, 242, "motors", "MOTORS", self.show_setup),
                                            (252, 382, "profiles", "PROFILES", self.show_profiles),
                                            (392, 502, "extras", "EXTRAS", self.show_extras),
                                            (512, 662, "autopilot", "AUTOPILOT", self.show_autopilot)):
            self.small_button(c, x0, 12, x1, 54, label, action, color=BLUE if name == active else BG,
                              tag=f"tab_{name}_{active}")

    def build_setup(self):
        c = self.c2 = tk.Canvas(self.root, width=CW, height=CH, bg=BG, highlightthickness=0)
        c.create_rectangle(0, 0, W, 66, fill=PANEL, width=0)
        self.setup_tabs(c, "motors")
        self.setup_state = c.create_text(785, 33, anchor="e", text="", font=fnt(14, "bold"), fill=FG)
        c.create_text(30, 88, anchor="w", font=fnt(11), fill=DIM,
                      text="If a motor pushes the wrong way, press FLIP. Saved automatically. Only works while SAFE.")
        self.flip_rows = []
        for i in range(4):
            y = 106 + i * 72
            c.create_rectangle(20, y, 780, y + 62, fill=PANEL, width=0)
            name = c.create_text(36, y + 20, anchor="w", text="", font=fnt(15, "bold"), fill=FG)
            job = c.create_text(36, y + 44, anchor="w", text="", font=fnt(10), fill=DIM)
            state = c.create_text(470, y + 31, anchor="e", text="", font=fnt(14, "bold"), fill=FG)
            tag = f"flip_{i}"
            rect = c.create_rectangle(560, y + 8, 768, y + 54, fill=BLUE, outline="", tags=tag)
            c.create_text(664, y + 31, text="FLIP", font=fnt(16, "bold"), fill="white", tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, i=i: self.flip(i))
            self.flip_rows.append((name, job, state, rect))
        c.create_rectangle(20, 402, 780, 446, fill=PANEL, width=0)
        self.setup_msg = c.create_text(34, 424, anchor="w", text="", font=fnt(13), fill=FG)
        c.create_rectangle(1, 1, W - 1, H - 1, outline=DIM, width=1)
        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)
        self.armed = False

    def flip(self, i):
        if self.armed:
            self.c2.itemconfig(self.setup_msg, text="Press STOP / CIRCLE first - can't flip while ARMED")
            return
        send(f"invert:{i}")
        rect = self.flip_rows[i][3]
        self.c2.itemconfig(rect, fill="#ffffff")
        self.root.after(150, lambda: self.c2.itemconfig(rect, fill=BLUE))

    def show_page(self, page):
        if page == "profiles" and not self.ed_dirty:
            self.ed_load()
        self.root.focus_set()
        for name, canvas in (("main", self.c), ("setup", self.c2), ("camera", self.c3), ("profiles", self.c4),
                             ("extras", self.c5), ("autopilot", self.c6), ("mission", self.c7)):
            if name == page:
                canvas.pack(expand=True)
            else:
                canvas.pack_forget()
        self.page = page
        self.camera_policy()

    def camera_policy(self, restart=False):
        """The camera runs while the camera page is open, or while a camera autopilot mode needs it."""
        want = self.page == "camera" or self.ap_mode in core.VISION_MODES
        if want and (restart or not self.cam.alive()):
            if not restart and time.monotonic() < self.cam_retry_at:
                return
            self.cam_retry_at = time.monotonic() + 3.0
            vx0, vy0, vx1, vy1 = self.c3.coords(self.video_box)
            w, h = int(vx1 - vx0) // 2 * 2, int(vy1 - vy0) // 2 * 2
            self.cam.start(w, h, self.cam_flip, self.cam_adj)
        elif not want and self.cam.proc:
            self.cam.stop()

    def show_setup(self):
        self.show_page("setup")

    def show_main(self):
        self.show_page("main")

    def show_profiles(self):
        self.show_page("profiles")

    def show_extras(self):
        self.show_page("extras")

    # ------------------------------------------------------------ extras page (keyboard drive, arm sound)
    def toggle(self, c, x0, y0, tag, action):
        rect = c.create_rectangle(x0, y0, x0 + 150, y0 + 46, fill=BG, outline=DIM, width=2, tags=tag)
        txt = c.create_text(x0 + 75, y0 + 23, text="", font=fnt(15, "bold"), fill=FG, tags=tag)
        c.tag_bind(tag, "<Button-1>", lambda e: action())
        return rect, txt

    def build_extras(self):
        c = self.c5 = tk.Canvas(self.root, width=CW, height=CH, bg=BG, highlightthickness=0)
        c.create_rectangle(0, 0, W, 66, fill=PANEL, width=0)
        self.setup_tabs(c, "extras")
        self.ex_state = c.create_text(785, 33, anchor="e", text="", font=fnt(14, "bold"), fill=FG)

        c.create_rectangle(20, 74, 780, 184, fill=PANEL, width=0)
        c.create_text(36, 94, anchor="w", text="KEYBOARD DRIVE", font=fnt(15, "bold"), fill=FG)
        c.create_text(36, 116, anchor="w", font=fnt(10), fill=DIM,
                      text="Drive with this laptop's keyboard. Works with or without the controller.")
        for i, (keys, what) in enumerate((("W / S", "forward/back"), ("A / D", "turn left/right"),
                                          ("Space / Shift", "up/down"), ("R / F", "tilt nose up/down"),
                                          ("Enter", "ARM"), ("Esc", "STOP"))):
            x, y = 36 + (i // 2) * 250, 144 + (i % 2) * 24
            c.create_text(x, y, anchor="w", text=keys, font=fnt(11, "bold"),
                          fill=RED if keys == "Esc" else GREEN if keys == "Enter" else FG)
            c.create_text(x + 112, y, anchor="w", text=what, font=fnt(10), fill=DIM)
        self.kb_toggle = self.toggle(c, 614, 82, "ex_kb", lambda: send(f"keyboard:{0 if self.kb_on else 1}"))

        c.create_rectangle(20, 192, 780, 318, fill=PANEL, width=0)
        c.create_text(36, 212, anchor="w", text="ARM SOUND", font=fnt(15, "bold"), fill=FG)
        self.ex_tune = c.create_text(36, 238, anchor="w", font=fnt(10), fill=DIM, text="")
        c.create_text(36, 286, anchor="w", text="VOLUME", font=fnt(11, "bold"), fill=FG)
        self.small_button(c, 108, 266, 150, 306, "−", lambda: self.ex_volume(-1), tag="ex_vdn")
        self.ex_vol = c.create_text(176, 286, text="", font=fnt(15, "bold"), fill=FG)
        self.small_button(c, 202, 266, 244, 306, "+", lambda: self.ex_volume(1), tag="ex_vup")
        self.small_button(c, 262, 266, 412, 306, "SET TUNE", self.tune_edit, tag="ex_tset")
        self.small_button(c, 422, 266, 592, 306, "DEFAULT TUNE", lambda: send("tune:"), tag="ex_tdef")
        self.small_button(c, 614, 258, 764, 310, "▶ TEST", self.tune_test, tag="ex_test")
        self.snd_toggle = self.toggle(c, 614, 200, "ex_snd", lambda: send(f"sound:{0 if self.snd_on else 1}"))

        c.create_rectangle(20, 326, 780, 406, fill=PANEL, width=0)
        c.create_text(36, 348, anchor="w", text="SOFTWARE UPDATE", font=fnt(15, "bold"), fill=FG)
        self.ex_upd = c.create_text(36, 380, anchor="w", font=fnt(10), fill=DIM, text="")
        self.small_button(c, 614, 340, 764, 392, "CHECK NOW", self.update_click, tag="ex_upd")

        c.create_rectangle(20, 414, 780, 458, fill=PANEL, width=0)
        self.ex_msg = c.create_text(34, 436, anchor="w", text="", font=fnt(13), fill=FG)
        c.create_rectangle(1, 1, W - 1, H - 1, outline=DIM, width=1)
        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)
        self.snd_on, self.snd_vol, self.snd_playing = True, 5, False
        # text box for pasting a ringtone (shown by SET TUNE)
        self.tune_var = tk.StringVar()
        self.tune_entry = tk.Entry(self.root, textvariable=self.tune_var, font=fnt(11), bg=BG, fg=FG,
                                   insertbackground=FG, relief="flat", highlightthickness=1,
                                   highlightbackground=DIM, highlightcolor=BLUE)
        self.tune_entry.bind("<Return>", lambda e: self.tune_edit())
        self.tune_win = c.create_window(X(36), Y(226), anchor="nw", window=self.tune_entry,
                                        width=int(556 * SX), height=int(26 * SY), state="hidden")
        self.tune_editing = False
        self.ex_note, self.ex_note_until = "", 0.0

    def button_text(self, c, tag, text):
        for item in c.find_withtag(tag):
            if c.type(item) == "text":
                c.itemconfig(item, text=text)

    def ex_say(self, text, secs=6):
        self.ex_note, self.ex_note_until = text, time.monotonic() + secs

    def tune_edit(self):
        """SET TUNE opens a text box: paste (Ctrl+V) or type an RTTTL ringtone, then SAVE (or Enter)."""
        c = self.c5
        if not self.tune_editing:
            self.tune_editing = True
            c.itemconfig(self.tune_win, state="normal")
            c.itemconfig(self.ex_tune, state="hidden")
            self.button_text(c, "ex_tset", "SAVE")
            self.tune_entry.focus_set()
            self.ex_say("Paste an RTTTL ringtone (Ctrl+V), then SAVE  ·  Esc = STOP", 30)
        else:
            text = self.tune_var.get().strip()
            if text:
                send("tune:" + text)
                self.ex_say("")
            self.tune_close()

    def tune_close(self):
        c = self.c5
        self.tune_editing = False
        c.itemconfig(self.tune_win, state="hidden")
        c.itemconfig(self.ex_tune, state="normal")
        self.button_text(c, "ex_tset", "SET TUNE")
        self.root.focus_set()

    def tune_test(self):
        send("sound_stop" if self.snd_playing else "sound_test")

    def update_click(self):
        u = self.upd
        if not u:
            self.ex_say("Auto-update isn't installed - run the setup command once")
        elif u.ready:
            if self.armed:
                self.ex_say("Press STOP first: updates only install while SAFE")
            else:
                u.install()
        elif u.state not in ("installing", "done"):
            u.check()

    def ex_volume(self, d):
        send(f"volume:{max(1, min(15, self.snd_vol + d))}")

    def update_extras(self, st):
        c = self.c5
        u = self.upd
        if u:
            u.tick()
            col = {"ready": BLUE, "failed": RED, "offline": AMBER, "current": GREEN, "done": GREEN}.get(u.state, DIM)
            c.itemconfig(self.ex_upd, text=short(u.text, 80), fill=col)
            self.button_text(c, "ex_upd", "UPDATE NOW" if u.ready else "…" if u.busy else "CHECK NOW")
            for item in c.find_withtag("ex_upd"):
                if c.type(item) == "rectangle":
                    c.itemconfig(item, fill=BLUE if u.ready else PANEL)
                    break
            self.c.itemconfig(self.upd_badge, text="⬆ UPDATE READY" if u.ready else
                              "UPDATING…" if u.state in ("installing", "done") else "")
            if u.state == "done" and not getattr(self, "restarting", False):
                self.restarting = True
                self.root.after(8000 if u.notes else 1500, self.restart_new_version)
        else:
            c.itemconfig(self.ex_upd, text="Auto-update isn't installed - run the setup command once")
        if st is None:
            c.itemconfig(self.ex_state, text="OFFLINE", fill=AMBER)
            return
        c.itemconfig(self.ex_state, text="ARMED" if st["armed"] else "SAFE", fill=GREEN if st["armed"] else RED)
        self.snd_on, self.snd_vol = st.get("arm_sound", True), st.get("sound_volume", 5)
        self.snd_playing = bool(st.get("playing"))
        for (rect, txt), on in ((self.kb_toggle, self.kb_on), (self.snd_toggle, self.snd_on)):
            c.itemconfig(rect, fill=GREEN if on else BG)
            c.itemconfig(txt, text="ON" if on else "OFF")
        c.itemconfig(self.ex_vol, text=str(self.snd_vol))
        self.button_text(c, "ex_test", "■ STOP SOUND" if self.snd_playing else "▶ TEST")
        name = st.get("tune_name")
        c.itemconfig(self.ex_tune, text=(f"Tune: {name} ({st.get('tune_secs', 0):.0f} s"
                                         f"{', yours' if st.get('tune_custom') else ''}) · motors play it when armed"
                                         " · touch any control to stop it") if name else
                     "Motors play a short tune when armed (no speaker). Props may twitch.")
        note = self.ex_note if time.monotonic() < self.ex_note_until else ""
        c.itemconfig(self.ex_msg, text=short(note or st.get("msg", "")))

    def restart_new_version(self):
        self.cam.stop()
        try:
            updater.restart_after_update(sys.argv)
        except Exception as ex:
            self.upd.state, self.upd.text = "failed", f"Installed, but restart failed ({ex}) - close and reopen ROV Backup"
            self.restarting = False

    # ------------------------------------------------------------ profile editor page (mouse/keyboard only)
    def build_profiles(self):
        c = self.c4 = tk.Canvas(self.root, width=CW, height=CH, bg=BG, highlightthickness=0)
        c.bind("<Button-1>", lambda e: self.root.focus_set(), add="+")
        self.ed_profiles, self.ed_sel, self.ed_scroll, self.ed_dirty = [], 0, 0, False
        self.active_name = None
        c.create_rectangle(0, 0, W, 66, fill=PANEL, width=0)
        self.setup_tabs(c, "profiles")
        self.prof_state = c.create_text(785, 33, anchor="e", text="", font=fnt(14, "bold"), fill=FG)

        # left: list of profiles
        c.create_text(20, 76, anchor="w", text="PROFILES", font=fnt(10, "bold"), fill=DIM)
        self.list_rows = []
        for i in range(7):
            y = 88 + i * 34
            tag = f"prow_{i}"
            rect = c.create_rectangle(20, y, 245, y + 30, fill=PANEL, outline="", tags=tag)
            txt = c.create_text(30, y + 15, anchor="w", text="", font=fnt(12, "bold"), fill=FG, tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, i=i: self.ed_select(self.ed_scroll + i))
            self.list_rows.append((rect, txt))
        self.small_button(c, 20, 330, 128, 358, "▲", lambda: self.ed_scroll_by(-1), tag="p_up")
        self.small_button(c, 137, 330, 245, 358, "▼", lambda: self.ed_scroll_by(1), tag="p_dn")
        self.small_button(c, 20, 366, 128, 406, "+ NEW", self.ed_new, tag="p_new")
        self.small_button(c, 137, 366, 245, 406, "COPY", self.ed_copy, tag="p_copy")
        self.small_button(c, 20, 414, 245, 454, "DELETE", self.ed_delete, tag="p_del")

        # right: name + one row per control
        c.create_text(262, 86, anchor="w", text="Name", font=fnt(11, "bold"), fill=DIM)
        self.ed_msg = c.create_text(262, 112, anchor="w", text="", font=fnt(10), fill=AMBER)
        for x, t in ((262, "CONTROL"), (530, "DOES"), (695, "REV"), (754, "POWER")):
            c.create_text(x, 130, anchor="w" if x == 262 else "center", text=t, font=fnt(9, "bold"), fill=DIM)
        self.ctrl_rows = []
        for i, k in enumerate(core.INPUTS):
            y = 140 + i * 32
            c.create_text(262, y + 14, anchor="w", text=core.INPUT_LABELS[k], font=fnt(11), fill=FG)
            self.small_button(c, 400, y, 428, y + 28, "◀", lambda k=k: self.ed_action(k, -1), tag=f"aprev_{k}")
            box = c.create_rectangle(432, y, 628, y + 28, fill=PANEL, outline="", tags=f"anext_{k}")
            act = c.create_text(530, y + 14, text="", font=fnt(11, "bold"), fill=FG, tags=f"anext_{k}")
            c.tag_bind(f"anext_{k}", "<Button-1>", lambda e, k=k: self.ed_action(k, 1))
            self.small_button(c, 632, y, 660, y + 28, "▶", lambda k=k: self.ed_action(k, 1), tag=f"anx2_{k}")
            rev = c.create_rectangle(670, y, 720, y + 28, fill=BG, outline=DIM, width=2, tags=f"rev_{k}")
            revt = c.create_text(695, y + 14, text="", font=fnt(10, "bold"), fill=FG, tags=f"rev_{k}")
            c.tag_bind(f"rev_{k}", "<Button-1>", lambda e, k=k: self.ed_toggle_rev(k))
            pw = c.create_rectangle(728, y, 780, y + 28, fill=BG, outline=DIM, width=2, tags=f"pw_{k}")
            pwt = c.create_text(754, y + 14, text="", font=fnt(10, "bold"), fill=FG, tags=f"pw_{k}")
            c.tag_bind(f"pw_{k}", "<Button-1>", lambda e, k=k: self.ed_power(k))
            self.ctrl_rows.append((k, act, rev, revt, pwt))
        self.small_button(c, 262, 404, 500, 454, "SAVE", lambda: self.ed_save(False), color=PANEL, tag="p_save")
        self.small_button(c, 512, 404, 780, 454, "SAVE + USE", lambda: self.ed_save(True), color=GREEN,
                          tag="p_use")
        c.create_rectangle(1, 1, W - 1, H - 1, outline=DIM, width=1)
        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)
        # text box for the profile name (placed in real screen coordinates)
        self.name_var = tk.StringVar()
        self.name_entry = tk.Entry(self.root, textvariable=self.name_var, font=fnt(13, "bold"), bg=PANEL, fg=FG,
                                   insertbackground=FG, relief="flat", highlightthickness=1,
                                   highlightbackground=DIM, highlightcolor=BLUE)
        c.create_window(X(310), Y(73), anchor="nw", window=self.name_entry,
                        width=int(470 * SX), height=int(28 * SY))
        self.name_var.trace_add("write", lambda *a: self.ed_rename())

    def ed_load(self):
        try:
            with open(core.profiles_path()) as f:
                profs = core.clean_profiles(json.load(f))
        except (OSError, ValueError):
            profs = []
        self.ed_profiles = profs or core.clean_profiles({"profiles": core.DEFAULT_PROFILES})
        names = [p["name"] for p in self.ed_profiles]
        self.ed_sel = names.index(self.active_name) if self.active_name in names else 0
        self.ed_dirty = False
        self.ed_note("")
        self.ed_render(update_name=True)

    def ed_note(self, text, color=AMBER):
        self.c4.itemconfig(self.ed_msg, text=text, fill=color)

    def ed_changed(self):
        self.ed_dirty = True
        self.ed_note("Unsaved changes - press SAVE or SAVE + USE")
        self.ed_render()

    def ed_cur(self):
        return self.ed_profiles[self.ed_sel]

    def ed_select(self, idx):
        if 0 <= idx < len(self.ed_profiles):
            self.ed_sel = idx
            self.root.focus_set()
            self.ed_render(update_name=True)

    def ed_scroll_by(self, d):
        self.ed_scroll = max(0, min(self.ed_scroll + d, max(0, len(self.ed_profiles) - 7)))
        self.ed_render()

    def ed_unique(self, base):
        names = {p["name"] for p in self.ed_profiles}
        if base not in names:
            return base
        n = 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"

    def ed_add(self, prof):
        self.ed_profiles.append(prof)
        self.ed_sel = len(self.ed_profiles) - 1
        self.ed_scroll = max(0, self.ed_sel - 6)
        self.ed_changed()
        self.ed_render(update_name=True)

    def ed_new(self):
        base = json.loads(json.dumps(core.DEFAULT_PROFILES[1]["map"]))   # start from Standard
        self.ed_add({"name": self.ed_unique("New profile"), "map": base})

    def ed_copy(self):
        cur = json.loads(json.dumps(self.ed_cur()))
        cur["name"] = self.ed_unique(cur["name"][:18] + " copy")
        self.ed_add(cur)

    def ed_delete(self):
        if len(self.ed_profiles) <= 1:
            self.ed_note("You need at least one profile")
            return
        del self.ed_profiles[self.ed_sel]
        self.ed_sel = min(self.ed_sel, len(self.ed_profiles) - 1)
        self.ed_scroll = min(self.ed_scroll, max(0, len(self.ed_profiles) - 7))
        self.ed_changed()
        self.ed_render(update_name=True)

    def ed_rename(self):
        if self._setting_name:
            return
        self.ed_cur()["name"] = self.name_var.get()[:24]
        self.ed_changed()

    def ed_entry(self, k):
        return self.ed_cur()["map"].setdefault(k, {"action": "none", "invert": False, "power": 1.0})

    def ed_action(self, k, d):
        m = self.ed_entry(k)
        order = core.ACTION_ORDER
        m["action"] = order[(order.index(m.get("action", "none")) + d) % len(order)]
        self.ed_changed()

    def ed_toggle_rev(self, k):
        m = self.ed_entry(k)
        m["invert"] = not m.get("invert", False)
        self.ed_changed()

    def ed_power(self, k):
        m = self.ed_entry(k)
        steps = [0.25, 0.5, 0.75, 1.0]
        cur = min(steps, key=lambda s: abs(s - m.get("power", 1.0)))
        m["power"] = steps[(steps.index(cur) + 1) % len(steps)]
        self.ed_changed()

    def ed_save(self, use):
        if self.armed:
            self.ed_note("Press STOP first - can't change profiles while ARMED", RED)
            return
        names = [p["name"].strip() for p in self.ed_profiles]
        if any(not n for n in names):
            self.ed_note("Every profile needs a name", RED)
            return
        if len(set(names)) != len(names):
            self.ed_note("Two profiles have the same name", RED)
            return
        for p in self.ed_profiles:
            p["name"] = p["name"].strip()
        path = core.profiles_path()
        try:
            with open(path + ".tmp", "w") as f:
                json.dump({"profiles": self.ed_profiles}, f, indent=2)
            os.replace(path + ".tmp", path)
        except OSError as ex:
            self.ed_note(f"Could not save: {ex}", RED)
            return
        send(f"reload_profiles:{self.ed_sel if use else -1}")
        self.ed_dirty = False
        self.ed_note(f"Saved. Now driving with '{self.ed_cur()['name']}'" if use else "Saved", GREEN)
        self.ed_render()

    _setting_name = False

    def ed_render(self, update_name=False):
        c = self.c4
        if not self.ed_profiles:
            return
        for i, (rect, txt) in enumerate(self.list_rows):
            j = self.ed_scroll + i
            if j < len(self.ed_profiles):
                p = self.ed_profiles[j]
                mark = "● " if p["name"] == self.active_name and not self.ed_dirty else "   "
                c.itemconfig(txt, text=short(mark + p["name"], 22))
                c.itemconfig(rect, fill=BLUE if j == self.ed_sel else PANEL)
            else:
                c.itemconfig(txt, text="")
                c.itemconfig(rect, fill=BG)
        if update_name:
            self._setting_name = True
            self.name_var.set(self.ed_cur()["name"])
            self._setting_name = False
        m = self.ed_cur()["map"]
        for k, act, rev, revt, pwt in self.ctrl_rows:
            e = m.get(k, {})
            a = e.get("action", "none")
            c.itemconfig(act, text=ACTION_TEXT[a], fill=FG if a != "none" else DIM)
            inv = e.get("invert", False)
            c.itemconfig(rev, fill=AMBER if inv else BG)
            c.itemconfig(revt, text="REV" if inv else "—")
            c.itemconfig(pwt, text=f"{int(round(e.get('power', 1.0) * 100))}%")

    def update_profiles_page(self, st):
        if st is None:
            self.c4.itemconfig(self.prof_state, text="OFFLINE", fill=AMBER)
            return
        self.c4.itemconfig(self.prof_state, text="ARMED" if st["armed"] else "SAFE",
                           fill=GREEN if st["armed"] else RED)
        if st.get("profile_name") != self.active_name:
            self.active_name = st.get("profile_name")
            if self.page == "profiles":
                self.ed_render()

    # ------------------------------------------------------------ autopilot page (mouse only)
    def show_autopilot(self):
        self.show_page("autopilot")

    def show_mission(self):
        self.mi_load()
        self.show_page("mission")

    AP_MODE_BUTTONS = [("cruise", "CRUISE", "hold thrust"), ("line", "LINE", "follow a line"),
                       ("target", "TARGET", "follow a colour"), ("heading", "HEADING", "hold still, facing"),
                       ("mission", "MISSION", "run the steps")]
    AP_ROWS = [  # (key, label, kind, column, row)
        ("heave_trim", "HOVER TRIM", "num", 0, 0), ("pitch_trim", "NOSE TRIM", "num", 0, 1),
        ("power", "AUTO POWER", "num", 0, 2), ("speed", "AUTO SPEED", "num", 0, 3),
        ("line_colour", "LINE COLOUR", "colour", 1, 0), ("target_colour", "TARGET COLOUR", "colour", 1, 1),
        ("target_size", "TARGET DISTANCE", "num", 1, 2), ("steer", "STEERING", "num", 1, 3)]

    @staticmethod
    def ap_format(key, v):
        if key in ("line_colour", "target_colour"):
            return str(v).upper()
        if key == "steer":
            return f"x{v:.1f}"
        if key == "target_size":
            return f"{v * 100:.0f}% of view"
        if key.endswith("trim"):
            return f"{v * 100:+.0f}%"
        return f"{v * 100:.0f}%"

    def build_autopilot(self):
        c = self.c6 = tk.Canvas(self.root, width=CW, height=CH, bg=BG, highlightthickness=0)
        c.create_rectangle(0, 0, W, 66, fill=PANEL, width=0)
        self.setup_tabs(c, "autopilot")
        self.ap_state = c.create_text(785, 33, anchor="e", text="", font=fnt(14, "bold"), fill=FG)

        c.create_rectangle(20, 74, 780, 124, fill=PANEL, width=0)
        c.create_text(36, 92, anchor="w", text="AUTOPILOT", font=fnt(15, "bold"), fill=FG)
        c.create_text(36, 112, anchor="w", font=fnt(9), fill=DIM,
                      text="Off at every start-up. Mouse only. Any stick/key movement or STOP takes over.")
        self.ap_master = self.toggle(c, 614, 76, "ap_master", lambda: send(f"ap_enable:{0 if self.ap_enabled else 1}"))

        self.ap_mode_btns = {}
        for i, (m, label, sub) in enumerate(self.AP_MODE_BUTTONS):
            x0, tag = 20 + i * 153, f"apm_{m}"
            rect = c.create_rectangle(x0, 132, x0 + 146, 182, fill=BG, outline=DIM, width=2, tags=tag)
            c.create_text(x0 + 73, 150, text=label, font=fnt(13, "bold"), fill=FG, tags=tag)
            c.create_text(x0 + 73, 170, text=sub, font=fnt(8), fill=DIM, tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, m=m: self.ap_click(m))
            self.ap_mode_btns[m] = rect

        self.ap_info_rect = c.create_rectangle(20, 190, 616, 232, fill=PANEL, width=0)
        self.ap_info = c.create_text(30, 211, anchor="w", text="", font=fnt(11, "bold"), fill=FG)
        c.create_rectangle(628, 190, 780, 232, fill=RED, outline="", tags="ap_off")
        c.create_text(704, 211, text="AUTO OFF", font=fnt(13, "bold"), fill="white", tags="ap_off")
        c.tag_bind("ap_off", "<Button-1>", lambda e: send("ap_off"))

        self.ap_vals = {}
        for key, label, kind, col, row in self.AP_ROWS:
            x0, y = 20 + col * 384, 240 + row * 40
            c.create_rectangle(x0, y, x0 + 376, y + 34, fill=PANEL, width=0)
            c.create_text(x0 + 12, y + 17, anchor="w", text=label, font=fnt(10, "bold"), fill=FG)
            minus, plus = ("◀", "▶") if kind == "colour" else ("−", "+")
            self.small_button(c, x0 + 150, y + 3, x0 + 186, y + 31, minus, lambda k=key: self.ap_step(k, -1),
                              tag=f"aps_{key}_dn")
            self.ap_vals[key] = c.create_text(x0 + 263, y + 17, text="", font=fnt(11, "bold"), fill=FG)
            self.small_button(c, x0 + 334, y + 3, x0 + 370, y + 31, plus, lambda k=key: self.ap_step(k, 1),
                              tag=f"aps_{key}_up")
        self.small_button(c, 20, 404, 250, 448, "EDIT MISSION", self.show_mission, tag="ap_mission")
        self.small_button(c, 260, 404, 490, 448, "CAMERA VIEW", self.show_camera, tag="ap_cam")
        self.ap_msg = c.create_text(502, 426, anchor="w", text="", font=fnt(9), fill=DIM, width=270)
        c.create_rectangle(1, 1, W - 1, H - 1, outline=DIM, width=1)
        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)
        c.itemconfig(self.ap_msg, width=int(270 * SX))

    def ap_click(self, mode):
        send("ap_off" if self.ap_mode == mode else f"ap:{mode}")

    def ap_step(self, key, d):
        s = self.ap_settings
        if key not in s:
            return
        if key in core.AP_CHOICES:
            cols = core.AP_COLOURS
            s[key] = cols[(cols.index(s[key]) + d) % len(cols)] if s[key] in cols else cols[0]
        else:
            _, lo, hi, step = core.AP_SETTINGS[key]
            s[key] = round(max(lo, min(hi, s[key] + d * step)), 3)
        self.ap_local_until = time.monotonic() + 1.5     # show our value until rov.py has caught up
        send(f"ap_set:{key}:{s[key]}")
        self.update_autopilot(read_status(), {})

    def update_autopilot(self, st, ap):
        c = self.c6
        if st is None:
            c.itemconfig(self.ap_state, text="OFFLINE", fill=AMBER)
        else:
            c.itemconfig(self.ap_state, text="ARMED" if st["armed"] else "SAFE",
                         fill=GREEN if st["armed"] else RED)
        rect, txt = self.ap_master
        c.itemconfig(rect, fill=GREEN if self.ap_enabled else BG)
        c.itemconfig(txt, text="ON" if self.ap_enabled else "OFF")
        for m, r in self.ap_mode_btns.items():
            c.itemconfig(r, fill=GREEN if self.ap_mode == m else BG if self.ap_enabled else PANEL)
        for key, item in self.ap_vals.items():
            if key in self.ap_settings:
                c.itemconfig(item, text=self.ap_format(key, self.ap_settings[key]), fill=FG)
        pick = self.ap_settings.get("target_pick")
        if self.ap_settings.get("target_colour") == "picked":
            if pick:
                rgb = colorsys.hsv_to_rgb(pick[0] / 360, pick[1], max(0.35, pick[2]))
                c.itemconfig(self.ap_vals["target_colour"], text="■ PICKED",
                             fill="#%02x%02x%02x" % tuple(int(v * 255) for v in rgb))
            else:
                c.itemconfig(self.ap_vals["target_colour"], text="PICK ON CAMERA", fill=AMBER)
        if self.ap_alert:
            info, color = "⚠ " + self.ap_alert, "white"
        elif self.ap_mode:
            info, color = f"{self.ap_mode.upper()}: {ap.get('info', '')}", GREEN
        elif self.ap_enabled:
            info, color = "ON - trim active. Arm, then click a mode.", FG
        else:
            info, color = "OFF - switch on, then arm and click a mode", DIM
        c.itemconfig(self.ap_info, text=short(info, 60), fill=color)
        if st is not None:
            c.itemconfig(self.ap_msg, text=self.vision_error or st.get("msg", ""))

    def update_alerts(self):
        """Bottom status bar on the main and camera pages, only while the autopilot is switched ON:
        green = a mode is engaged and working; flashing red = unsure, TAKE CONTROL NOW."""
        a = self.ap_alert if self.ap_enabled else ""
        good = bool(self.ap_enabled and self.ap_mode and not a)
        flash = int(time.monotonic() * 3) % 2 == 0
        if a:
            fill, text = (RED if flash else "#7a1512"), "⚠ " + a
        elif good:
            fill, text = GREEN, f"✓ AUTOPILOT {self.ap_mode.upper()}: {self.ap_info_text}"
        else:
            fill, text = None, ""
        if fill:
            self.c.itemconfig(self.msg_rect, fill=fill)
            self.c.itemconfig(self.msg, text=short(text, 60), fill="white", font=fnt(13, "bold"))
        else:
            self.c.itemconfig(self.msg_rect, fill=PANEL)
            self.c.itemconfig(self.msg, font=fnt(13))
        self.c3.itemconfig(self.cam_alert_rect, state="normal" if fill else "hidden", fill=fill or RED)
        self.c3.itemconfig(self.cam_alert, state="normal" if fill else "hidden", text=short(text, 52))
        self.c3.tag_raise(self.cam_alert_rect)
        self.c3.tag_raise(self.cam_alert)
        self.c6.itemconfig(self.ap_info_rect, fill=fill if a else PANEL)

    # ------------------------------------------------------------ vision (runs in a thread)
    def vision_loop(self):
        try:
            import vision as V
        except Exception as ex:   # numpy missing
            self.vision_error = f"Camera autopilot unavailable: {ex}"
            return
        tracker, last = V.HeadingTracker(), -1
        while True:
            time.sleep(0.07)
            mode = self.ap_mode
            if mode not in core.VISION_MODES:
                continue
            cam = self.cam
            fresh = cam.frame is not None and time.monotonic() - cam.frame_time < 0.5
            if fresh and cam.frame_no == last:
                continue                     # no new frame yet
            res = {"found": False}
            if fresh:
                last = cam.frame_no
                try:
                    img = V.to_array(cam.frame)
                    s = self.ap_settings
                    if mode == "line":
                        res = V.find_line(img, s.get("line_colour", "dark"))
                    elif mode == "target":
                        res = V.find_target(img, s.get("target_colour", "orange"), s.get("target_pick"))
                    else:
                        res = tracker.update(img, self.ap_epoch)
                except Exception as ex:
                    res = {"found": False, "error": str(ex)}
            res.update(ts=time.time(), mode=mode, epoch=self.ap_epoch)
            self.vision_result = res
            try:
                with open(VISION + ".tmp", "w") as f:
                    json.dump(res, f)
                os.replace(VISION + ".tmp", VISION)
            except OSError:
                pass

    # ------------------------------------------------------------ mission editor (mouse only)
    def mission_path(self):
        return os.path.join(core.HERE, "mission.json")

    def build_mission(self):
        c = self.c7 = tk.Canvas(self.root, width=CW, height=CH, bg=BG, highlightthickness=0)
        c.create_rectangle(0, 0, W, 66, fill=PANEL, width=0)
        self.small_button(c, 12, 12, 170, 54, "◀ AUTOPILOT", self.show_autopilot, tag="mi_back")
        c.create_text(190, 33, anchor="w", text="MISSION", font=fnt(16, "bold"), fill=FG)
        self.mi_state = c.create_text(785, 33, anchor="e", text="", font=fnt(14, "bold"), fill=FG)
        c.create_text(30, 80, anchor="w", font=fnt(10), fill=DIM,
                      text="Runs top to bottom when you click MISSION on the AUTOPILOT page (while armed). Saved automatically.")
        for x, t in ((30, "STEP"), (186, "DO"), (405, "POWER"), (570, "TIME")):
            c.create_text(x, 100, anchor="w" if x == 30 else "center", text=t, font=fnt(9, "bold"), fill=DIM)
        self.mi_rows = []
        for i in range(8):
            y, row = 110 + i * 36, f"mrow_{i}"
            c.create_rectangle(20, y, 780, y + 32, fill=PANEL, width=0, tags=row)
            c.create_text(36, y + 16, anchor="w", text=f"{i + 1}", font=fnt(12, "bold"), fill=FG, tags=row)
            act = c.create_text(186, y + 16, text="", font=fnt(11, "bold"), fill=FG, tags=row)
            pw = c.create_text(405, y + 16, text="", font=fnt(11, "bold"), fill=FG, tags=row)
            secs = c.create_text(570, y + 16, text="", font=fnt(11, "bold"), fill=FG, tags=row)
            for x0, x1, sym, fn in ((64, 96, "◀", lambda i=i: self.mi_action(i, -1)),
                                    (276, 308, "▶", lambda i=i: self.mi_action(i, 1)),
                                    (340, 372, "−", lambda i=i: self.mi_num(i, "power", -0.05)),
                                    (438, 470, "+", lambda i=i: self.mi_num(i, "power", 0.05)),
                                    (505, 537, "−", lambda i=i: self.mi_num(i, "secs", -0.5)),
                                    (603, 635, "+", lambda i=i: self.mi_num(i, "secs", 0.5)),
                                    (700, 768, "✕", lambda i=i: self.mi_delete(i))):
                t = f"mi_{i}_{x0}"
                c.create_rectangle(x0, y + 3, x1, y + 29, fill=BG, outline=DIM, width=2, tags=(row, t))
                c.create_text((x0 + x1) / 2, y + 16, text=sym, font=fnt(11, "bold"), fill=FG, tags=(row, t))
                c.tag_bind(t, "<Button-1>", lambda e, fn=fn: fn())
            self.mi_rows.append((row, act, pw, secs))
        self.small_button(c, 20, 404, 250, 448, "+ ADD STEP", self.mi_add, tag="mi_add")
        self.small_button(c, 260, 404, 490, 448, "RESET TO EXAMPLE", self.mi_reset, tag="mi_reset")
        self.mi_msg = c.create_text(502, 426, anchor="w", text="", font=fnt(10), fill=DIM)
        c.create_rectangle(1, 1, W - 1, H - 1, outline=DIM, width=1)
        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)
        self.mi_steps = []

    def mi_load(self):
        try:
            with open(self.mission_path()) as f:
                self.mi_steps = core.clean_mission(json.load(f))
        except (OSError, ValueError):
            self.mi_steps = [dict(st) for st in core.DEFAULT_MISSION]
        self.mi_render()

    def mi_save(self):
        try:
            with open(self.mission_path() + ".tmp", "w") as f:
                json.dump({"steps": self.mi_steps}, f, indent=1)
            os.replace(self.mission_path() + ".tmp", self.mission_path())
            total = sum(st["secs"] for st in self.mi_steps)
            self.c7.itemconfig(self.mi_msg, text=f"Saved - {len(self.mi_steps)} steps, {total:.1f} s", fill=GREEN)
        except OSError as ex:
            self.c7.itemconfig(self.mi_msg, text=f"Could not save: {ex}", fill=RED)
        self.mi_render()

    def mi_action(self, i, d):
        if i < len(self.mi_steps):
            order = core.MISSION_ORDER
            st = self.mi_steps[i]
            st["do"] = order[(order.index(st["do"]) + d) % len(order)]
            self.mi_save()

    def mi_num(self, i, key, d):
        if i < len(self.mi_steps):
            lo, hi = (0.0, 1.0) if key == "power" else (0.5, 60.0)
            st = self.mi_steps[i]
            st[key] = round(max(lo, min(hi, st[key] + d)), 2)
            self.mi_save()

    def mi_delete(self, i):
        if i < len(self.mi_steps):
            del self.mi_steps[i]
            self.mi_save()

    def mi_add(self):
        if len(self.mi_steps) >= 8:
            self.c7.itemconfig(self.mi_msg, text="8 steps is the most that fits", fill=AMBER)
            return
        self.mi_steps.append({"do": "forward", "power": 0.4, "secs": 2.0})
        self.mi_save()

    def mi_reset(self):
        self.mi_steps = [dict(st) for st in core.DEFAULT_MISSION]
        self.mi_save()

    def mi_render(self):
        c = self.c7
        for i, (row, act, pw, secs) in enumerate(self.mi_rows):
            if i < len(self.mi_steps):
                st = self.mi_steps[i]
                c.itemconfig(row, state="normal")
                c.itemconfig(act, text=st["do"].replace("_", " ").upper())
                c.itemconfig(pw, text="—" if st["do"] == "hover" else f"{st['power'] * 100:.0f}%")
                c.itemconfig(secs, text=f"{st['secs']:.1f} s")
            else:
                c.itemconfig(row, state="hidden")
        st = read_status()
        if st is not None:
            c.itemconfig(self.mi_state, text="ARMED" if st["armed"] else "SAFE", fill=GREEN if st["armed"] else RED)

    def show_camera(self):
        self.show_page("camera")

    def toggle_camera(self):
        self.show_page("main" if self.page == "camera" else "camera")

    # ------------------------------------------------------------ camera page
    def build_camera(self):
        c = self.c3 = tk.Canvas(self.root, width=CW, height=CH, bg=BG, highlightthickness=0)
        # video 4:3 on the left, status sidebar on the right
        self.video_box = c.create_rectangle(0, 0, 600, 450, fill="#000000", width=0)
        self.no_signal = c.create_text(300, 225, text="Starting camera…", font=fnt(16, "bold"), fill=DIM)
        c.create_rectangle(612, 0, 800, 480, fill=PANEL, width=0)
        self.cbadge = c.create_rectangle(620, 8, 792, 52, fill=RED, width=0)
        self.cbadge_txt = c.create_text(706, 30, text="SAFE", font=fnt(20, "bold"), fill="white")
        self.cspeed = c.create_text(624, 72, anchor="w", text="", font=fnt(13, "bold"), fill=FG)
        self.cprofile = c.create_text(624, 96, anchor="w", text="", font=fnt(11), fill=DIM)
        self.cbars = []
        for i in range(4):
            y = 118 + i * 44
            name = c.create_text(624, y + 8, anchor="w", text="", font=fnt(10), fill=FG)
            c.create_rectangle(624, y + 20, 788, y + 34, fill=TRACK, width=0)
            bar = c.create_rectangle(706, y + 20, 706, y + 34, fill=GREEN, width=0)
            c.create_line(706, y + 17, 706, y + 37, fill=DIM)
            self.cbars.append((name, bar))
        self.small_button(c, 620, 294, 702, 334, "◀ BACK", self.show_main, tag="cam_back")
        self.small_button(c, 710, 294, 792, 334, "FLIP ↻", self.flip_camera, tag="cam_flip")
        self.small_button(c, 620, 340, 702, 378, "PICTURE", self.toggle_picture, tag="cam_pic")
        self.pick_btn = self.small_button(c, 710, 340, 792, 378, "PICK", self.toggle_pick, tag="cam_pick")
        c.bind("<Button-1>", self.cam_click, add="+")
        self.flip_label = c.create_text(10, 14, anchor="w", text="", font=fnt(11, "bold"), fill=AMBER)
        c.create_rectangle(620, 386, 792, 470, fill=RED, outline=TRACK, width=2, tags="cam_stop")
        c.create_text(706, 420, text="STOP", font=fnt(24, "bold"), fill="white", tags="cam_stop")
        c.create_text(706, 454, text="(Esc)", font=fnt(10), fill="#f0f0f0", tags="cam_stop")
        c.tag_bind("cam_stop", "<Button-1>", lambda e: send("stop"))
        self.cmsg = c.create_text(8, 465, anchor="w", text="", font=fnt(11), fill=FG)
        self.build_picture_panel(c)
        self.picking = False
        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)
        vx0, vy0, _, _ = c.coords(self.video_box)
        self.video_img = tk.PhotoImage()
        self.video_item = c.create_image(vx0, vy0, anchor="nw", image=self.video_img)
        # autopilot: what the camera is tracking, and the take-control banner
        self.ov_pts = [c.create_oval(0, 0, 0, 0, outline=AMBER, width=3, state="hidden") for _ in range(3)]
        self.ov_line = c.create_line(0, 0, 0, 0, fill=AMBER, width=3, state="hidden")
        self.ov_target = c.create_oval(0, 0, 0, 0, outline=GREEN, width=3, state="hidden")
        self.ov_text = c.create_text(vx0 + 8 * SX, vy0 + 40 * SY, anchor="w", text="", font=fnt(11, "bold"),
                                     fill=GREEN)
        self.cam_alert_rect = c.create_rectangle(X(0), Y(410), X(600), Y(450), fill=RED, width=0, state="hidden")
        self.cam_alert = c.create_text(X(300), Y(430), text="", font=fnt(14, "bold"), fill="white", state="hidden")
        self.pick_msg = c.create_text(X(300), Y(390), text="", font=fnt(15, "bold"), fill=AMBER)
        c.tag_raise(self.no_signal)
        c.tag_raise(self.flip_label)
        c.tag_raise("pic")
        c.itemconfig("pic", state="hidden")
        c.itemconfig(self.flip_label, text=FLIP_TEXT[self.cam_flip])
        self.shown_frame = 0

    # picture adjustments panel (over the left of the video)
    def build_picture_panel(self, c):
        c.create_rectangle(6, 30, 306, 314, fill=PANEL, outline=DIM, width=2, tags="pic")
        self.pic_vals = {}
        for i, (k, label, *_rest) in enumerate(PICTURE):
            y = 40 + i * 44
            c.create_text(16, y + 18, anchor="w", text=label, font=fnt(11, "bold"), fill=FG, tags="pic")
            for x0, sym, d in ((150, "−", -1), (258, "+", 1)):
                t = f"pic_{k}_{'up' if d > 0 else 'dn'}"
                c.create_rectangle(x0, y, x0 + 40, y + 36, fill=BG, outline=DIM, width=2, tags=("pic", t))
                c.create_text(x0 + 20, y + 18, text=sym, font=fnt(15, "bold"), fill=FG, tags=("pic", t))
                c.tag_bind(t, "<Button-1>", lambda e, k=k, d=d: self.adjust_picture(k, d))
            self.pic_vals[k] = c.create_text(224, y + 18, text="", font=fnt(12, "bold"), fill=FG, tags="pic")
        for x0, x1, t, label, fn in ((14, 152, "pic_reset", "RESET", self.reset_picture),
                                     (160, 298, "pic_close", "CLOSE", self.toggle_picture)):
            c.create_rectangle(x0, 266, x1, 304, fill=BG, outline=DIM, width=2, tags=("pic", t))
            c.create_text((x0 + x1) / 2, 285, text=label, font=fnt(12, "bold"), fill=FG, tags=("pic", t))
            c.tag_bind(t, "<Button-1>", lambda e, fn=fn: fn())
        self.pic_open = False
        self.show_picture_values(c)

    def show_picture_values(self, c=None):
        c = c or self.c3
        for k, _, _, _, _, d, fmt in PICTURE:
            v = self.cam_adj[k]
            c.itemconfig(self.pic_vals[k], text=fmt(v), fill=FG if abs(v - d) < 1e-6 else AMBER)

    # click an object on the video to make its colour the autopilot TARGET colour
    def toggle_pick(self):
        self.picking = not self.picking
        if self.picking and self.pic_open:
            self.toggle_picture()
        self.c3.itemconfig(self.c3.find_withtag(self.pick_btn)[0], fill=AMBER if self.picking else BG)
        self.c3.itemconfig(self.pick_msg, text="CLICK THE OBJECT TO USE ITS COLOUR" if self.picking else "")

    def cam_click(self, event):
        if not self.picking:
            return
        vx0, vy0, vx1, vy1 = self.c3.coords(self.video_box)
        if not (vx0 <= event.x < vx1 and vy0 <= event.y < vy1):
            return
        frame = self.cam.frame
        if not frame:
            self.c3.itemconfig(self.pick_msg, text="NO CAMERA PICTURE TO PICK FROM")
            return
        _, size, _, data = frame.split(b"\n", 3)
        w, h = (int(v) for v in size.split())
        px, py = int(event.x - vx0), int(event.y - vy0)
        tot, n = [0, 0, 0], 0
        for y in range(max(0, py - 3), min(h, py + 4)):           # average a 7x7 patch
            for x in range(max(0, px - 3), min(w, px + 4)):
                i = (y * w + x) * 3
                for k in range(3):
                    tot[k] += data[i + k]
                n += 1
        if not n:
            return
        r, g, b = (t / n / 255 for t in tot)
        hh, ss, vv = colorsys.rgb_to_hsv(r, g, b)
        pick = [round(hh * 360), round(ss, 2), round(vv, 2)]
        send(f"ap_set:target_pick:{pick[0]},{pick[1]},{pick[2]}")
        self.ap_settings.update(target_pick=pick, target_colour="picked")
        self.ap_local_until = time.monotonic() + 1.5
        self.picking = False
        self.c3.itemconfig(self.c3.find_withtag(self.pick_btn)[0], fill=BG)
        rgb = "#%02x%02x%02x" % tuple(int(t / n) for t in tot)
        self.c3.itemconfig(self.pick_msg, text="■ TARGET COLOUR PICKED", fill=rgb if pick[2] > 0.3 else AMBER)
        self.root.after(4000, lambda: self.picking or self.c3.itemconfig(self.pick_msg, text="", fill=AMBER))

    def toggle_picture(self):
        if not self.pic_open and getattr(self, "picking", False):
            self.toggle_pick()
        self.pic_open = not self.pic_open
        self.c3.itemconfig("pic", state="normal" if self.pic_open else "hidden")

    def adjust_picture(self, k, d):
        _, _, lo, hi, step, _, _ = next(p for p in PICTURE if p[0] == k)
        self.cam_adj[k] = round(max(lo, min(hi, self.cam_adj[k] + d * step)), 3)
        self.picture_changed()

    def reset_picture(self):
        self.cam_adj = dict(PICTURE_DEFAULT)
        self.picture_changed()

    def picture_changed(self):
        save_camera_settings(self.cam_flip, self.cam_adj)
        self.show_picture_values()
        # restart the video once the clicking stops (restarting takes ~1 s)
        if self.adj_job:
            self.root.after_cancel(self.adj_job)
        self.adj_job = self.root.after(400, self.restart_video)

    def restart_video(self):
        self.adj_job = None
        if self.cam.alive():
            self.camera_policy(restart=True)

    def flip_camera(self):
        """FLIP button: normal -> upside down -> mirrored -> normal. Saved in camera.json."""
        self.cam_flip = FLIP_ORDER[(FLIP_ORDER.index(self.cam_flip) + 1) % len(FLIP_ORDER)]
        save_camera_settings(self.cam_flip, self.cam_adj)
        self.c3.itemconfig(self.flip_label, text=FLIP_TEXT[self.cam_flip])
        if self.cam.alive():
            self.camera_policy(restart=True)   # restart the video with the new setting

    def refresh_video(self):
        if self.page == "camera":
            cam = self.cam
            if cam.frame and cam.frame_no != self.shown_frame:
                self.shown_frame = cam.frame_no
                try:
                    self.video_img.configure(data=cam.frame, format="ppm")
                    self.c3.itemconfig(self.no_signal, text="")
                except tk.TclError:
                    pass
            elif cam.error:
                self.c3.itemconfig(self.no_signal, text=cam.error)
            self.draw_overlay()
        self.root.after(30, self.refresh_video)

    def draw_overlay(self):
        c = self.c3
        vx0, vy0, vx1, vy1 = c.coords(self.video_box)
        vw, vh = vx1 - vx0, vy1 - vy0
        r = self.vision_result if self.ap_mode in core.VISION_MODES else {}
        found = r.get("found")
        pts = r.get("points", []) if found and self.ap_mode == "line" else []
        for i, item in enumerate(self.ov_pts):
            if i < len(pts):
                x, y = vx0 + pts[i][0] * vw, vy0 + pts[i][1] * vh
                c.coords(item, x - 8, y - 8, x + 8, y + 8)
            c.itemconfig(item, state="normal" if i < len(pts) else "hidden")
        if len(pts) >= 2:
            c.coords(self.ov_line, *[v for x, y in pts for v in (vx0 + x * vw, vy0 + y * vh)])
        c.itemconfig(self.ov_line, state="normal" if len(pts) >= 2 else "hidden")
        if found and self.ap_mode == "target":
            x, y = vx0 + (r["x"] + 1) / 2 * vw, vy0 + (r["y"] + 1) / 2 * vh
            rad = max(10, (r["area"] ** 0.5) * vw / 2)
            c.coords(self.ov_target, x - rad, y - rad, x + rad, y + rad)
        c.itemconfig(self.ov_target, state="normal" if found and self.ap_mode == "target" else "hidden")
        if self.ap_mode:
            word = {"line": "LINE", "target": "TARGET", "heading": "HEADING HOLD", "cruise": "CRUISE",
                    "mission": "MISSION"}[self.ap_mode]
            ok = found or self.ap_mode not in core.VISION_MODES
            c.itemconfig(self.ov_text, text=f"AUTO {word}" + ("" if ok else "  - NOT SURE") if self.ap_enabled else "",
                         fill=GREEN if ok else RED)
        else:
            c.itemconfig(self.ov_text, text="")

    def update_camera(self, st):
        c = self.c3
        if st is None:
            c.itemconfig(self.cbadge, fill=AMBER)
            c.itemconfig(self.cbadge_txt, text="OFFLINE")
            return
        armed = st["armed"]
        c.itemconfig(self.cbadge, fill=GREEN if armed else RED)
        c.itemconfig(self.cbadge_txt, text="ARMED" if armed else "SAFE")
        c.itemconfig(self.cspeed, text=f"Speed {st['speed']}%")
        c.itemconfig(self.cprofile, text=short(f"Profile: {st.get('profile_name', '')}", 24))
        for (name, bar), m in zip(self.cbars, st["motors"]):
            v = m["value"]
            c.itemconfig(name, text=f"{m['name'].replace('_', ' ')}  {v * 100:+.0f}%")
            _, y0, _, y1 = c.coords(bar)
            mid = X(706)
            x = X(706 + v * 82)
            c.coords(bar, min(mid, x), y0, max(mid, x), y1)
            c.itemconfig(bar, fill=(GREEN if v >= 0 else BLUE) if armed else DIM)
        c.itemconfig(self.cmsg, text=short(st.get("msg", ""), 80))

    def update_setup(self, st):
        c = self.c2
        if st is None:
            c.itemconfig(self.setup_state, text="OFFLINE", fill=AMBER)
            return
        self.armed = st["armed"]
        c.itemconfig(self.setup_state, text="ARMED" if self.armed else "SAFE", fill=GREEN if self.armed else RED)
        for (name, job, state, rect), m in zip(self.flip_rows, st["motors"]):
            c.itemconfig(name, text=m["name"].replace("_", " "))
            c.itemconfig(job, text=m.get("job", ""))
            inv = m.get("invert", False)
            c.itemconfig(state, text="FLIPPED" if inv else "NORMAL", fill=AMBER if inv else FG)
            c.itemconfig(rect, fill=TRACK if self.armed else BLUE)
        c.itemconfig(self.setup_msg, text=short(st.get("msg", "")))

    def button(self, x0, y0, x1, y1, cmd, label, sub, color):
        tag = f"btn_{cmd}"
        c = self.c
        rect = c.create_rectangle(x0, y0, x1, y1, fill=color, outline=TRACK, width=2, tags=tag)
        size = {"stop": 26, "profile": 18}.get(cmd, 13)
        main = c.create_text((x0 + x1) / 2, (y0 + y1) / 2 - 10, text=label, font=fnt(size, "bold"),
                             fill="white", tags=tag)
        subt = c.create_text((x0 + x1) / 2, y1 - 18, text=sub, font=fnt(10), fill="#f0f0f0", tags=tag)
        c.tag_bind(tag, "<Button-1>", lambda e: self.press(cmd))
        self.buttons[cmd] = (rect, color)
        return main, subt

    def press(self, cmd):
        send(cmd)
        rect, color = self.buttons[cmd]
        self.c.itemconfig(rect, fill="#ffffff" if cmd != "stop" else "#ff7b72")
        self.root.after(150, lambda: self.c.itemconfig(rect, fill=color))

    def update(self):
        c = self.c
        st = read_status()
        self.kb_on = bool(st and st.get("keyboard_drive"))
        ap = (st or {}).get("autopilot") or {}
        self.ap_enabled, self.ap_mode = bool(ap.get("enabled")), ap.get("mode")
        self.ap_epoch, self.ap_alert = ap.get("epoch", 0), ap.get("alert", "") if ap.get("enabled") else ""
        self.ap_info_text = ap.get("info", "")
        if time.monotonic() > self.ap_local_until and ap.get("settings"):
            self.ap_settings = dict(ap["settings"])
        self.camera_policy()
        self.update_autopilot(st, ap)
        self.update_extras(st)
        self.update_setup(st)
        self.update_camera(st)
        self.update_profiles_page(st)
        if st is not None:
            vt = st.get("view_toggle", 0)
            if self.last_view_toggle is not None and vt != self.last_view_toggle:
                self.toggle_camera()
            self.last_view_toggle = vt
        if st is None:
            c.itemconfig(self.badge, fill=AMBER)
            c.itemconfig(self.badge_txt, text="OFFLINE")
            c.itemconfig(self.pad_txt, text="ROV program not running")
            c.itemconfig(self.pad_sub, text="check:  journalctl -u rov")
            c.itemconfig(self.speed_txt, text="--")
            c.itemconfig(self.msg, text="Waiting for the ROV service…")
            for name, pct, bar in self.rows:
                c.itemconfig(name, text="")
                c.itemconfig(pct, text="")
                _, y0, _, y1 = c.coords(bar)
                c.coords(bar, X(260), y0, X(260), y1)
        else:
            armed = self.armed = st["armed"]
            c.itemconfig(self.badge, fill=GREEN if armed else RED)
            c.itemconfig(self.badge_txt, text="ARMED" if armed else "SAFE")
            if st["controller"]:
                c.itemconfig(self.pad_txt, text=pad_name(st["controller"]))
                c.itemconfig(self.pad_sub, text="connected · link OK" if st["heartbeat"]
                             else "connected · no link monitor")
            elif self.kb_on:
                c.itemconfig(self.pad_txt, text="Keyboard")
                c.itemconfig(self.pad_sub, text="Enter = arm · Esc = stop")
            else:
                c.itemconfig(self.pad_txt, text="No controller")
                c.itemconfig(self.pad_sub, text="press PS · CREATE+PS pairs")
            c.itemconfig(self.speed_txt, text=f"{st['speed']}%")
            pmap = st.get("profile_map", {})
            c.itemconfig(self.profile_lbl[0], text=short(st.get("profile_name", "").upper(), 13))
            c.itemconfig(self.profile_lbl[1],
                         text=f"profile {st.get('profile', 1)} of {st.get('profile_count', 1)} · △ next")
            c.itemconfig(self.stick_labels[0], text="L  " + short(stick_label(pmap, "ly", "lx"), 18))
            c.itemconfig(self.stick_labels[1], text="R  " + short(stick_label(pmap, "ry", "rx"), 18))
            for (_, _, lbl), k in zip(self.triggers, ("l2", "r2")):
                c.itemconfig(lbl, text=f"{k.upper()} {push_label(pmap, k)}")

            msg = st.get("msg", "")
            if not st.get("output_ok", True):
                msg = "⚠ " + st.get("output", "Arduino Mega not connected")
                c.itemconfig(self.msg, fill=AMBER)
            else:
                c.itemconfig(self.msg, fill=FG)
            if not st.get("output_ok", True):
                pass
            elif not armed and st["controller"] and not st.get("switch_on", True):
                msg = "Panel ARM switch is OFF"
            elif (not armed and self.kb_on and "STOP" not in msg and "Let go" not in msg
                  and "Centre" not in msg and "Panel" not in msg):
                msg = ("Press OPTIONS or Enter to arm" if st["controller"] else "Press Enter to arm") + \
                      "  ·  WASD drive, Space/Shift up/down"
            elif not armed and st["controller"] and "STOP" not in msg and "Centre" not in msg:
                msg = "Press OPTIONS on the controller to arm"
            c.itemconfig(self.msg, text=short(msg))
            c.itemconfig(self.ap_badge, text=f"AUTO: {self.ap_mode.upper()}" if self.ap_mode
                         else "AUTOPILOT ON" if self.ap_enabled else "")
            keys = st.get("keys") or []
            c.itemconfig(self.kb_badge, text=("KEYS: " + " ".join(k.upper() for k in keys)) if keys
                         else "KEYBOARD DRIVE ON" if self.kb_on else "")

            motors = st["motors"]
            for i, (name, pct, bar) in enumerate(self.rows):
                if i >= len(motors):
                    continue
                v = motors[i]["value"]
                c.itemconfig(name, text=motors[i]["name"].replace("_", " "))
                c.itemconfig(pct, text=f"{v * 100:+.0f}%")
                y0, y1 = c.coords(bar)[1], c.coords(bar)[3]
                x = X(260 + v * 240)
                c.coords(bar, min(X(260), x), y0, max(X(260), x), y1)
                c.itemconfig(bar, fill=(GREEN if v >= 0 else BLUE) if armed else DIM)

            ax = st.get("axes") or {}
            for (dot, cx, cy, r), (kx, ky) in zip(self.sticks, (("lx", "ly"), ("rx", "ry"))):
                x, y = X(cx + ax.get(kx, 0) * (r - 6)), Y(cy + ax.get(ky, 0) * (r - 6))
                d = 9 * SCALE
                c.coords(dot, x - d, y - d, x + d, y + d)
                c.itemconfig(dot, fill=BLUE if ax else TRACK)
            dy, dx = ax.get("dy", 0), ax.get("dx", 0)
            if abs(dy) > 0.5 or abs(dx) > 0.5:
                k, positive = ("dy", dy < 0) if abs(dy) > 0.5 else ("dx", dx > 0)
                c.itemconfig(self.tilt_txt, text="D-pad: " + push_label(pmap, k, positive).upper(), fill=AMBER)
            else:
                words = [STICK_WORD[pmap.get(k, {}).get("action", "none")] for k in ("dy", "dx")]
                c.itemconfig(self.tilt_txt, text="D-pad = " + (" + ".join(w for w in words if w) or "—"), fill=DIM)
            for (fill, x0, _), k in zip(self.triggers, ("l2", "r2")):
                c.coords(fill, X(x0), Y(248), X(x0 + 96 * ax.get(k, 0)), Y(262))
        self.update_alerts()      # last, so the TAKE CONTROL banner wins over the normal message
        self.root.after(100, self.update)


def main():
    root = tk.Tk()
    if "--fullscreen" in sys.argv:
        set_view(root.winfo_screenwidth(), root.winfo_screenheight(), (0, 0, 0, 0))
    else:
        cw, ch = 1200, 720
        if "--size" in sys.argv:
            cw, ch = (int(v) for v in sys.argv[sys.argv.index("--size") + 1].lower().split("x"))
        set_view(cw, ch, (0, 0, 0, 0))
    root.title("ROV backup (Arduino Mega)")
    d = Dashboard(root)
    if "--setup" in sys.argv:
        d.show_setup()
    if "--camera" in sys.argv:
        d.show_camera()
    if "--profiles" in sys.argv:
        d.show_profiles()
    root.mainloop()


if __name__ == "__main__":
    main()
