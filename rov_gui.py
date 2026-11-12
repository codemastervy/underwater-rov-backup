#!/usr/bin/env python3
"""ROV dashboard - LAPTOP BACKUP version (window on the laptop screen; motors via Arduino Mega).
Run with --fullscreen for full screen, --size 1200x720 for another window size.

Shows what rov.py (the systemd service) is doing and offers STOP / speed buttons.
It never arms the motors: arming is controller-only (OPTIONS), so a stray click
can't start the props. If this window closes, the ROV keeps running normally.

Keys: Space = STOP, +/- = speed, P = profile, C = camera, Esc = leave full screen, F11 = full screen.
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk

import rov as core   # profile definitions (INPUTS, ACTIONS, DEFAULT_PROFILES) live in rov.py

STATUS = "/dev/shm/rov-status.json"
CMD = "/dev/shm/rov-cmd"
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


def camera_flip_path():
    return os.path.join(core.HERE, "camera.json")


def load_camera_flip():
    try:
        with open(camera_flip_path()) as f:
            v = json.load(f).get("flip", "none")
        return v if v in FLIP_ORDER else "none"
    except (OSError, ValueError, AttributeError):
        return "none"


def save_camera_flip(v):
    try:
        with open(camera_flip_path() + ".tmp", "w") as f:
            json.dump({"flip": v}, f)
        os.replace(camera_flip_path() + ".tmp", camera_flip_path())
    except OSError:
        pass


class CameraFeed:
    """ffmpeg reads the capture card (MJPEG) and pipes raw PPM frames; Tk shows the newest one."""

    def __init__(self):
        self.proc = None
        self.frame = None
        self.frame_no = 0
        self.error = ""

    def start(self, w, h, flip="none"):
        self.stop()
        dev = find_camera()
        if not dev:
            self.error = "No camera / capture card found"
            return
        self.error = ""
        self.w, self.h = w, h
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "640x480", "-framerate", "30",
               "-i", dev, "-vf", ",".join(FLIP_FILTERS[flip] + [f"scale={w}:{h}"]), "-f", "image2pipe", "-vcodec", "ppm", "-"]
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
                self.frame_no += 1
        except (EOFError, OSError, ValueError):
            pass
        if self.proc is proc:
            err = proc.stderr.read().decode(errors="replace").strip().splitlines()
            self.error = "Camera stopped" + (f": {err[-1][:60]}" if err else "")

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
        root.bind("<Escape>", lambda e: root.attributes("-fullscreen", False))
        root.bind("<F11>", lambda e: root.attributes("-fullscreen", True))
        key = self.key
        root.bind("<space>", key(lambda: self.press("stop")))
        root.bind("<plus>", key(lambda: self.press("speed+")))
        root.bind("<equal>", key(lambda: self.press("speed+")))
        root.bind("<minus>", key(lambda: self.press("speed-")))
        root.bind("<p>", key(lambda: self.press("profile")))
        root.bind("<c>", key(lambda: self.toggle_camera()))
        self.page = "main"
        self.last_view_toggle = None
        self.cam = CameraFeed()
        self.cam_flip = load_camera_flip()

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
        c.create_rectangle(20, 300, 780, 342, fill=PANEL, width=0)
        self.msg = c.create_text(34, 321, anchor="w", text="", font=fnt(13), fill=FG)

        # --- buttons
        self.buttons = {}
        self.button(20, 356, 330, 466, "stop", "STOP", "disarm motors  (Space)", RED)
        self.profile_lbl = self.button(345, 356, 555, 466, "profile", "PROFILE", "△ next", PANEL)
        self.button(570, 356, 670, 466, "speed-", "SPEED −", "(L1)", PANEL)
        self.button(680, 356, 780, 466, "speed+", "SPEED +", "(R1)", PANEL)

        # thin frame on the edge of the safe area: if you can't see all 4 sides, increase MARGINS
        c.create_rectangle(1, 1, W - 1, H - 1, outline=DIM, width=1)

        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)

        self.build_setup()
        self.build_profiles()
        self.build_camera()
        self.update()
        self.refresh_video()

    # ------------------------------------------------------------ motor setup page
    def small_button(self, c, x0, y0, x1, y1, label, action, color=None, tag=None):
        # canvas tags must not contain spaces (Tk would split them into several tags)
        tag = tag or "sb_" + "".join(ch for ch in label if ch.isalnum())
        c.create_rectangle(x0, y0, x1, y1, fill=color or BG, outline=DIM, width=2, tags=tag)
        c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label, font=fnt(12, "bold"), fill=FG, tags=tag)
        c.tag_bind(tag, "<Button-1>", lambda e: action())
        return tag

    def key(self, fn):
        """Keyboard shortcut handler that does nothing while typing in a text box."""
        def handler(event):
            if isinstance(self.root.focus_get(), tk.Entry):
                return
            fn()
        return handler

    def setup_tabs(self, c, active):
        self.small_button(c, 12, 12, 140, 54, "◀  BACK", self.show_main, tag=f"back_{active}")
        for x0, x1, name, label, action in ((156, 300, "motors", "MOTORS", self.show_setup),
                                            (310, 454, "profiles", "PROFILES", self.show_profiles)):
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
        if page != "camera":
            self.cam.stop()
        if page == "profiles" and not self.ed_dirty:
            self.ed_load()
        self.root.focus_set()
        for name, canvas in (("main", self.c), ("setup", self.c2), ("camera", self.c3), ("profiles", self.c4)):
            if name == page:
                canvas.pack(expand=True)
            else:
                canvas.pack_forget()
        self.page = page
        if page == "camera":
            vx0, vy0, vx1, vy1 = self.c3.coords(self.video_box)
            w, h = int(vx1 - vx0) // 2 * 2, int(vy1 - vy0) // 2 * 2
            self.cam.start(w, h, self.cam_flip)

    def show_setup(self):
        self.show_page("setup")

    def show_main(self):
        self.show_page("main")

    def show_profiles(self):
        self.show_page("profiles")

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
        self.small_button(c, 620, 300, 702, 344, "◀ BACK", self.show_main, tag="cam_back")
        self.small_button(c, 710, 300, 792, 344, "FLIP ↻", self.flip_camera, tag="cam_flip")
        self.flip_label = c.create_text(10, 14, anchor="w", text="", font=fnt(11, "bold"), fill=AMBER)
        c.create_rectangle(620, 356, 792, 470, fill=RED, outline=TRACK, width=2, tags="cam_stop")
        c.create_text(706, 404, text="STOP", font=fnt(24, "bold"), fill="white", tags="cam_stop")
        c.create_text(706, 448, text="(Space)", font=fnt(10), fill="#f0f0f0", tags="cam_stop")
        c.tag_bind("cam_stop", "<Button-1>", lambda e: send("stop"))
        self.cmsg = c.create_text(8, 465, anchor="w", text="", font=fnt(11), fill=FG)
        c.scale("all", 0, 0, SX, SY)
        c.move("all", OX, OY)
        vx0, vy0, _, _ = c.coords(self.video_box)
        self.video_img = tk.PhotoImage()
        self.video_item = c.create_image(vx0, vy0, anchor="nw", image=self.video_img)
        c.tag_raise(self.no_signal)
        c.tag_raise(self.flip_label)
        c.itemconfig(self.flip_label, text=FLIP_TEXT[self.cam_flip])
        self.shown_frame = 0

    def flip_camera(self):
        """FLIP button: normal -> upside down -> mirrored -> normal. Saved in camera.json."""
        self.cam_flip = FLIP_ORDER[(FLIP_ORDER.index(self.cam_flip) + 1) % len(FLIP_ORDER)]
        save_camera_flip(self.cam_flip)
        self.c3.itemconfig(self.flip_label, text=FLIP_TEXT[self.cam_flip])
        if self.page == "camera":
            self.show_page("camera")   # restart the video with the new setting

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
        self.root.after(30, self.refresh_video)

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
            armed = st["armed"]
            c.itemconfig(self.badge, fill=GREEN if armed else RED)
            c.itemconfig(self.badge_txt, text="ARMED" if armed else "SAFE")
            if st["controller"]:
                c.itemconfig(self.pad_txt, text=pad_name(st["controller"]))
                c.itemconfig(self.pad_sub, text="connected · link OK" if st["heartbeat"]
                             else "connected · no link monitor")
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
            elif not armed and st["controller"] and "STOP" not in msg and "Centre" not in msg:
                msg = "Press OPTIONS on the controller to arm"
            c.itemconfig(self.msg, text=short(msg))

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
