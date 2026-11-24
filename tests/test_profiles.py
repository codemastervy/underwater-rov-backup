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
from unittest import mock

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
            json.dump({"ts": time.monotonic() - age, "keys": held}, f)

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

    # ---- full power at once ----
    def powers(self):
        return {t.name: round(t.value, 2) for t in self.rov.thrusters}

    def test_a_key_is_full_power_in_one_tick_and_off_in_one_tick(self):
        self.arm_keyboard()
        for held, want in ((["w"], (1, 1, 0)), (["s"], (-1, -1, 0)), ([], (0, 0, 0)), (["space"], (0, 0, 1)),
                           (["d"], (1, -1, 0)), ([], (0, 0, 0))):
            self.keys(*held)
            self.rov.tick()
            powers = self.powers()
            self.assertEqual((powers["left"], powers["right"], powers["front_up"]), want, held)

    def test_tilt_keys_are_full_power_too(self):
        self.arm_keyboard()
        self.keys("r")
        self.rov.tick()
        self.assertEqual((self.powers()["front_up"], self.powers()["rear_up"]), (1, -1))

    def test_sticks_are_still_smoothed(self):
        self.connect_pad()
        self.rov.arm("controller")
        self.rov.end_tune()
        self.rov.pad.axes["ly"] = -1.0
        self.rov.tick()
        self.assertEqual(self.powers()["left"], 0.06)

    def test_the_autopilot_is_still_smoothed_with_a_keyboard_profile(self):
        self.arm_keyboard()
        self.rov.ap.enabled, self.rov.ap.mode = True, "cruise"
        self.rov.ap.locked = {"surge": 0.5}
        self.keys()
        self.rov.tick()
        self.assertEqual(self.powers()["left"], 0.06)

    def test_keyboard_smoothing_can_be_switched_on(self):
        with open(self.cfg_path) as f:
            text = f.read().replace("keyboard_ramp_per_second = 0 ", "keyboard_ramp_per_second = 5 ")
        with open(self.cfg_path, "w") as f:
            f.write(text)
        self.rov = self.make()
        self.arm_keyboard()
        self.keys("w")
        self.rov.tick()
        self.assertEqual(self.powers()["left"], 0.1)

    def test_each_kind_of_profile_has_its_own_speed(self):
        self.assertEqual(self.rov.levels[self.rov.level], 0.5)
        self.screen("use_profile:Keys WASD")
        self.assertEqual(self.rov.levels[self.rov.level], 1.0)
        self.screen("speed-")
        self.screen("use_profile:Standard")
        self.assertEqual(self.rov.levels[self.rov.level], 0.5)
        self.screen("use_profile:Keys arrows")
        self.assertEqual(self.rov.levels[self.rov.level], 0.75)

    def test_an_older_config_gets_the_same(self):
        with open(self.cfg_path) as f:
            lines = [line for line in f if not line.startswith("keyboard_")]
        with open(self.cfg_path, "w") as f:
            f.writelines(lines)
        self.rov = self.make()
        self.arm_keyboard()
        self.keys("w")
        self.rov.tick()
        self.assertEqual(self.powers()["left"], 1)

    # ---- robustness ----
    def test_looking_for_a_controller_never_holds_up_the_motors(self):
        def slow(preferred):
            time.sleep(0.5)
        ticks = []

        def tick():
            ticks.append(time.monotonic())
            self.rov.running = len(ticks) < 50
        self.rov.tick = tick
        self.rov.write_status_file = lambda: None
        with mock.patch.object(rov, "find_gamepad", slow):
            self.rov.run()
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), 0.2)

    def test_the_controller_that_was_found_is_taken_over(self):
        pad = types.SimpleNamespace(name="pad", motion=None, last_motion=0, fds=lambda: [])
        self.rov.tick = lambda: setattr(self.rov, "running", self.rov.pad is None)
        self.rov.write_status_file = lambda: None
        with mock.patch.object(rov, "find_gamepad", lambda preferred: pad):
            self.rov.run()
        self.assertIs(self.rov.pad, pad)

    def test_an_error_in_the_loop_stops_the_motors_but_not_the_program(self):
        self.arm_keyboard()
        self.keys("w")
        self.rov.tick()
        with mock.patch.object(self.rov.ap, "apply", side_effect=KeyError("x")), self.assertLogs("rov", "ERROR"):
            self.rov.safe_tick()
        self.assertFalse(self.rov.armed)
        self.assertEqual(self.powers()["left"], 0)
        self.assertEqual(self.rov.gpio.state[self.rov.enable_pin], 0)
        self.rov.safe_tick()

    def test_broken_files_are_survived(self):
        for text in ("[]", "3", '{"profiles": 5}', '{"profiles": [3, {"name": "x", "map": 4}]}', "null",
                     '{"profiles": [{"name": "x", "map": {"ly": {"action": "turn", "power": null}}}]}'):
            with open(rov.profiles_path(self.cfg_path), "w") as f:
                f.write(text)
            self.make()
        for text in ("[]", "{}", "null", '{"ts": "x"}', '{"ts": [], "keys": 3}', "{"):
            with open(rov.KEYS_PATH, "w") as f:
                f.write(text)
            self.screen("use_profile:Keys WASD")
            self.rov.read_keys()
            self.assertFalse(self.rov.kbd_live)
        for data in ([], None, {"steps": 4}, {"steps": [1, {"do": []}, {"do": "up", "power": "x"}]}):
            self.assertLessEqual(len(rov.clean_mission(data)), 1)

    def test_a_section_with_a_comment_is_found_when_saving(self):
        with open(self.cfg_path) as f:
            lines = [line for line in f if not line.startswith("design")]
        with open(self.cfg_path, "w") as f:
            f.write("".join(lines).replace("[control]", "[control]   # mine"))
        self.make().save_setting("design", '"new"', section="[control]")
        with open(self.cfg_path, "rb") as f:
            self.assertEqual(tomllib.load(f)["control"]["design"], "new")

    def test_only_one_program_drives_the_motors(self):
        with mock.patch.object(rov, "run_dir", lambda: self.dir):
            first = rov.lock("motors")
            self.assertIsNotNone(first)
            self.assertIsNone(rov.lock("motors"))
            first.close()
            rov.lock("motors").close()

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
