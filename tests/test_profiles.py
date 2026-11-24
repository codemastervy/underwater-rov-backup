"""Tests for profiles: exactly one is in use, and its type decides whether the controller or the keyboard drives."""
import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import rov  # noqa: E402


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.dir, "config.toml")
        for name in ("config.toml", "profiles.json"):
            shutil.copy(os.path.join(ROOT, name), self.dir)
        self.paths = rov.CMD_PATH, rov.KEYS_PATH
        rov.CMD_PATH, rov.KEYS_PATH = os.path.join(self.dir, "cmd"), os.path.join(self.dir, "keys")
        self.rov = self.make()

    def tearDown(self):
        rov.CMD_PATH, rov.KEYS_PATH = self.paths
        shutil.rmtree(self.dir)

    def make(self):
        with open(self.cfg_path, "rb") as f:
            return rov.ROV(tomllib.load(f), rov.FakeBackend(), self.cfg_path)

    def screen(self, command):
        with open(rov.CMD_PATH, "w") as f:
            f.write(command)
        self.rov.check_screen_commands()

    def keys(self, *held, age=0.0):
        with open(rov.KEYS_PATH, "w") as f:
            json.dump({"ts": time.time() - age, "keys": held}, f)

    def connect_pad(self):
        axes = dict.fromkeys(("lx", "ly", "rx", "ry", "l2", "r2", "dy", "dx"), 0.0)
        self.rov.pad = types.SimpleNamespace(name="pad", axes=axes, motion=None, close=lambda: None)

    def arm_keyboard(self):
        self.screen("use_profile:Keys WASD")
        self.keys()
        self.screen("kbd_arm")
        self.assertTrue(self.rov.armed)
        self.rov.end_tune()

    # ---- which device drives ----
    def test_keyboard_drives_with_a_keyboard_profile(self):
        self.arm_keyboard()
        self.keys("w")
        self.rov.read_keys()
        self.assertEqual(self.rov.command()["surge"], 1.0)

    def test_sticks_do_nothing_with_a_keyboard_profile(self):
        self.connect_pad()
        self.screen("use_profile:Keys WASD")
        self.rov.pad.axes["ly"] = -1.0
        self.assertFalse(any(self.rov.command().values()))

    def test_keys_do_nothing_with_a_controller_profile(self):
        self.connect_pad()
        self.keys("w")
        self.rov.read_keys()
        self.assertFalse(any(self.rov.command().values()))

    def test_only_the_device_that_drives_can_arm(self):
        self.connect_pad()
        self.keys()
        self.screen("kbd_arm")
        self.assertFalse(self.rov.armed)
        self.screen("use_profile:Keys WASD")
        self.rov.arm("controller")
        self.assertFalse(self.rov.armed)

    def test_cannot_arm_without_the_keyboard_link_or_with_keys_held(self):
        self.screen("use_profile:Keys WASD")
        self.keys(age=2)
        self.screen("kbd_arm")
        self.assertFalse(self.rov.armed)
        self.keys("w")
        self.screen("kbd_arm")
        self.assertFalse(self.rov.armed)

    def test_losing_the_keyboard_disarms_even_with_a_controller(self):
        self.arm_keyboard()
        self.connect_pad()
        self.keys(age=2)
        self.rov.tick()
        self.assertFalse(self.rov.armed)
        self.assertIn("keyboard link lost", self.rov.status_msg)

    def test_every_stop_works_with_a_keyboard_profile(self):
        codes = types.SimpleNamespace(**{f"BTN_{n}": i for i, n in enumerate(
            ("START", "EAST", "SELECT", "MODE", "NORTH", "WEST", "TR", "TL"))})
        for button in (codes.BTN_EAST, codes.BTN_SELECT, codes.BTN_MODE):
            self.arm_keyboard()
            self.connect_pad()
            self.rov.pad.e = codes
            self.rov.handle_buttons([button])
            self.assertFalse(self.rov.armed)
        self.arm_keyboard()
        self.screen("stop")
        self.assertFalse(self.rov.armed)

    # ---- picking profiles ----
    def test_each_device_picks_its_own_kind(self):
        seen = []
        for kind in ("keyboard", "keyboard", "controller", "keyboard"):
            self.screen(f"next_profile:{kind}")
            seen.append(self.rov.profile()["name"])
        self.assertEqual(seen, ["Keys WASD", "Keys arrows", "Standard", "Keys arrows"])

    def test_no_profile_change_while_armed(self):
        self.arm_keyboard()
        for command in ("use_profile:Standard", "next_profile:controller", "next_profile:keyboard"):
            self.screen(command)
            self.assertEqual(self.rov.profile()["name"], "Keys WASD")

    def test_the_choice_survives_a_restart(self):
        self.screen("use_profile:Keys arrows")
        self.assertEqual(self.make().profile()["name"], "Keys arrows")

    def test_renaming_the_profile_in_use_keeps_it_in_use(self):
        profiles = [{**p, "name": 'Mine "é"'} if p["name"] == "Standard" else p for p in self.rov.profiles]
        with open(rov.profiles_path(self.cfg_path), "w") as f:
            json.dump({"profiles": profiles}, f)
        self.screen('reload_profiles:Mine "é"')
        self.assertEqual(self.rov.profile()["name"], 'Mine "é"')
        self.assertEqual(self.make().profile()["name"], 'Mine "é"')

    def test_keys_that_work_the_screen_cannot_be_bound(self):
        keys = {"forward": "tab", "back": "f1", "left": "escape", "up": "space"}
        cleaned = rov.clean_profiles({"profiles": [{"name": "x", "type": "keyboard", "keys": keys}]})[0]["keys"]
        self.assertEqual([cleaned[a] for a in ("forward", "back", "left", "up")], ["", "", "", "space"])

    # ---- older configs ----
    def old_config(self, *lines):
        with open(self.cfg_path) as f:
            text = f.read().replace("[control]", "\n".join(("[control]", *lines)))
        with open(self.cfg_path, "w") as f:
            f.write(text)

    def test_the_old_keyboard_switch_becomes_a_keyboard_profile(self):
        self.old_config("keyboard_drive = true", 'keyboard_profile = "Keys arrows"')
        self.assertEqual(self.make().profile()["name"], "Keys arrows")
        with open(self.cfg_path) as f:
            text = f.read()
        self.assertNotIn("keyboard_drive", text)
        self.assertNotIn("keyboard_profile", text)
        self.assertEqual(self.make().profile()["name"], "Keys arrows")

    def test_the_old_switch_left_off_changes_nothing(self):
        self.old_config("keyboard_drive = false", 'keyboard_profile = "Keys arrows"')
        self.assertEqual(self.make().profile()["name"], "Standard")

    # ---- commands ----
    def test_bad_commands_are_ignored(self):
        for command in ("invert:x", "volume:", "use_profile:nope", "next_profile:zzz", "rubbish", ""):
            self.screen(command)
        self.assertEqual(self.rov.profile()["name"], "Standard")


if __name__ == "__main__":
    unittest.main()
