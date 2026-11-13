"""Automatic updates from GitHub (used by rov_gui.py; needs only Python and an internet connection).

The dashboard checks GitHub at start-up and every 30 minutes. When a newer version is there it
shows "UPDATE READY"; one tap on SETUP > EXTRAS > UPDATE NOW (only while SAFE) installs it and
restarts the program. Your settings are never touched (motor flips, profiles, camera, autopilot,
mission, arm tune).

Laptop version: the files in this folder are replaced; then the app restarts by itself. If the Arduino
firmware changed, it says so: plug the Mega in and run  bash upload-mega.sh  (or the setup command).

VERSION (next to this file) holds the GitHub commit the installed copy came from.
"""
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request

REPO = "codemastervy/underwater-rov-backup"
BRANCH = "main"
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, "VERSION")
# your settings: never overwritten by an update
KEEP = {"config.toml", "profiles.json", "camera.json", "autopilot.json", "mission.json", "tune.txt",
        "VERSION", ".mega-firmware"}
CHECK_EVERY = 30 * 60
UA = {"User-Agent": "rov-updater"}


def local_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def latest_version(timeout=10):
    """Newest commit on GitHub (40-character id)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                                 headers={**UA, "Accept": "application/vnd.github.sha"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sha = r.read().decode().strip()
    if len(sha) != 40:
        raise ValueError("unexpected answer from GitHub")
    return sha


def download(sha, into):
    """Download and unpack that version; returns the folder with the files."""
    req = urllib.request.Request(f"https://codeload.github.com/{REPO}/tar.gz/{sha}", headers=UA)
    path = os.path.join(into, "src.tar.gz")
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if (m.name.startswith("/") or ".." in m.name.split("/")
                    or not (m.isfile() or m.isdir())):     # no links, devices or pipes
                raise ValueError(f"unsafe file in download: {m.name}")
        try:
            t.extractall(into, filter="data")    # Python 3.12+ (and patched 3.11): strict extraction
        except TypeError:
            t.extractall(into)
    dirs = [d for d in os.listdir(into) if os.path.isdir(os.path.join(into, d))]
    if len(dirs) != 1 or not os.path.isfile(os.path.join(into, dirs[0], "rov.py")):
        raise ValueError("download doesn't look like the ROV program")
    return os.path.join(into, dirs[0])


def copy_files(src, dest):
    """Copy the program files over the installed ones, keeping your settings."""
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in (".git", "build", "__pycache__")]
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for name in files:
            if rel == "." and name in KEEP and os.path.exists(os.path.join(dest, name)):
                continue
            shutil.copy2(os.path.join(root, name), os.path.join(dest, rel, name))


def startup_note():
    """Laptop: is the Mega's firmware older than rov_mega.ino? (.mega-firmware = what was last uploaded)"""
    import hashlib
    try:
        with open(os.path.join(HERE, "rov_mega", "rov_mega.ino"), "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(HERE, ".mega-firmware")) as f:
            have = f.read().strip()
    except OSError:
        return ""
    return "" if want == have else "Mega firmware is out of date: close the app, plug in the Mega, run  bash upload-mega.sh"


def file_changed(src, dest, name):
    try:
        with open(os.path.join(src, name), "rb") as a, open(os.path.join(dest, name), "rb") as b:
            return a.read() != b.read()
    except OSError:
        return True


class Updater:
    """Background checker/installer. Read .state / .text from the GUI; call check() / install()."""

    def __init__(self, on_done=None):
        self.current = local_version()
        self.latest = ""
        self.state = "idle"        # idle, checking, ready, current, offline, installing, done, failed
        self.text = "Not checked yet"
        self.next_check = 0.0
        self.busy = False
        self.notes = []            # extra things to tell the user after an update
        self.on_done = on_done

    @property
    def ready(self):
        return self.state == "ready"

    def short(self, sha):
        return sha[:7] if sha else "unknown"

    def tick(self):
        """Call often; starts a check every CHECK_EVERY seconds (in a thread)."""
        if not self.busy and time.monotonic() >= self.next_check and self.state not in ("done", "installing"):
            self.check()

    def check(self):
        if self.busy:
            return
        self.busy = True
        self.next_check = time.monotonic() + CHECK_EVERY
        if self.state != "ready":
            self.state, self.text = "checking", "Checking GitHub for updates…"
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            self.latest = latest_version()
            if self.latest == self.current:
                self.state, self.text = "current", f"Up to date (version {self.short(self.current)})"
                note = startup_note()
                if note:
                    self.state, self.text = "failed", note
            else:
                self.state, self.text = "ready", (f"UPDATE READY: {self.short(self.current)} → "
                                                  f"{self.short(self.latest)}. Tap UPDATE NOW (while SAFE)")
        except Exception:
            self.next_check = time.monotonic() + 5 * 60     # no internet (at the pool): try again in 5 min
            if self.state != "ready":
                self.state, self.text = "offline", "Can't reach GitHub (no internet?) - will try again"
        finally:
            self.busy = False

    def install(self):
        if self.busy or not self.latest:
            return
        self.busy = True
        self.state, self.text = "installing", "Downloading the update…"
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        tmp = tempfile.mkdtemp(prefix="rov-update-")
        try:
            src = download(self.latest, tmp)
            self.text = "Installing the update…"
            self.notes = install_files(src, HERE)
            with open(VERSION_FILE, "w") as f:
                f.write(self.latest + "\n")
            self.current = self.latest
            self.state, self.text = "done", "Updated! Restarting… " + " · ".join(self.notes)
            if self.on_done:
                self.on_done()
        except Exception as ex:
            self.state, self.text = "failed", f"Update failed: {ex}"[:90]
            self.next_check = time.monotonic() + 5 * 60
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            self.busy = False


def install_files(src, dest):
    """Laptop: copy the new files over this folder (your settings stay). Returns notes for the user."""
    notes = []
    if file_changed(src, dest, os.path.join("rov_mega", "rov_mega.ino")):
        notes.append("new Mega firmware: plug in the Mega and run  bash upload-mega.sh")
    if file_changed(src, dest, "install.sh"):
        notes.append("new packages may be needed: run the setup command once")
    copy_files(src, dest)
    for name in ("run.sh", "upload-mega.sh", "install.sh", "setup.sh"):
        p = os.path.join(dest, name)
        if os.path.exists(p):
            os.chmod(p, 0o755)
    return notes


def restart_after_update(argv):
    """Start the new version: launch run.sh again (after this window has closed and run.sh has stopped
    the old motor program), then close this window. The motors are off in between (SAFE)."""
    import sys
    args = " ".join("'" + a.replace("'", "'\\''") + "'" for a in argv[1:])
    subprocess.Popen(["bash", "-c", f"sleep 3; exec bash '{HERE}/run.sh' {args}"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(0)
