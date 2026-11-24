"""Tests for frame designs (per-frame motor trim, MOTORS page) and the safety rules around it.

Run on a laptop or the Pi (no GPIO, no controller needed):  python3 -m unittest discover tests
"""
import json
import os
import shutil
import sys
import tempfile
import tomllib
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import rov  # noqa: E402

# What the hull simulation (aquabots-rov-hull, main: analysis/) uses for the same design:
#   front = heave*heave_front + p,  rear = heave*heave_rear - p,  p = pitch*pitch_mix + surge*surge_pitch
SIM = {
    "v13": (1.0, 1.0, 1.0, 0.0),
    "new": (0.25, 0.75, 1.0, 0.3),
}


def sim_mix(design, cmd):
    pm, hf, hr, sp = SIM[design]
    s, yw, hv, pt = (cmd.get(k, 0.0) for k in ("surge", "yaw", "heave", "pitch"))
    p = pt * pm + s * sp
    out = {"left": s + yw, "right": s - yw, "front_up": hv * hf + p, "rear_up": hv * hr - p}
    k = max([1.0] + [abs(v) for v in out.values()])
    return {n: v / k for n, v in out.items()}


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.dir, "config.toml")
        shutil.copy(os.path.join(ROOT, "config.toml"), self.cfg_path)
        self.paths = (rov.CMD_PATH, rov.STATUS_PATH)
        rov.CMD_PATH = os.path.join(self.dir, "cmd")
        rov.STATUS_PATH = os.path.join(self.dir, "status.json")
        self.rov = self.make()

    def tearDown(self):
        rov.CMD_PATH, rov.STATUS_PATH = self.paths
        shutil.rmtree(self.dir)

    def make(self):
        with open(self.cfg_path, "rb") as f:
            return rov.ROV(tomllib.load(f), rov.FakeBackend(), self.cfg_path)

    def screen(self, cmd, r=None):
        with open(rov.CMD_PATH, "w") as f:
            f.write(cmd)
        (r or self.rov).check_screen_commands()

    def motors(self, cmd, r=None):
        r = r or self.rov
        c = dict.fromkeys(rov.AXES, 0.0)
        c.update(cmd)
        return {t.name: v for t, v in zip(r.thrusters, rov.mix(r.thrusters, c, 1.0))}

    # ---- the trims ----
    def test_default_is_v13_and_matches_config_exactly(self):
        self.assertEqual(self.rov.design_id, "v13")
        for t in self.rov.thrusters:
            self.assertEqual(t.coef, t.base)
            self.assertEqual(t.surge_pitch, 0.0)

    def test_every_design_mixes_like_its_simulation(self):
        cmds = [{"surge": 1}, {"surge": -1}, {"heave": 1}, {"heave": -1}, {"pitch": 1}, {"pitch": -0.6},
                {"yaw": 1}, {"surge": 1, "yaw": 0.5}, {"surge": 0.7, "heave": 0.5, "pitch": 0.3}]
        for d in SIM:
            self.screen("design:" + d)
            self.assertEqual(self.rov.design_id, d)
            for cmd in cmds:
                got, want = self.motors(cmd), sim_mix(d, cmd)
                for name in want:
                    self.assertAlmostEqual(got[name], want[name], places=6, msg=f"{d} {cmd} {name}")

    def test_builtin_values_match_the_simulations(self):
        for d in rov.DEFAULT_DESIGNS:
            self.assertEqual(tuple(d[k] for k in rov.DESIGN_KEYS), SIM[d["id"]], d["id"])

    # ---- saving ----
    def test_choice_and_tweaks_survive_a_restart(self):
        self.screen("design:new")
        self.screen("design_set:heave_rear:0.95")
        with open(self.cfg_path) as f:
            self.assertIn('design = "new"', f.read())
        r2 = self.make()
        self.assertEqual(r2.design_id, "new")
        self.assertEqual(r2.design()["heave_rear"], 0.95)
        self.screen("design_reset", r2)
        self.assertEqual(self.make().design()["heave_rear"], 1.0)

    def test_config_toml_stays_valid_after_saving(self):
        self.screen("design:new")
        with open(self.cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        self.assertEqual(cfg["control"]["design"], "new")
        self.assertEqual(len(cfg["motor"]), 4)

    def test_old_config_without_design_line_still_works(self):
        with open(self.cfg_path) as f:
            text = "\n".join(l for l in f.read().split("\n") if not l.startswith("design ="))
        with open(self.cfg_path, "w") as f:
            f.write(text)
        r = self.make()
        self.assertEqual(r.design_id, "v13")
        self.screen("design:new", r)
        with open(self.cfg_path, "rb") as f:
            self.assertEqual(tomllib.load(f)["control"]["design"], "new")

    def test_broken_designs_json_is_ignored(self):
        with open(rov.designs_path(self.cfg_path), "w") as f:
            f.write("{not json")
        self.assertEqual(self.make().designs[1]["heave_front"], 0.75)
        with open(rov.designs_path(self.cfg_path), "w") as f:
            json.dump({"new": {"heave_front": "x", "pitch_mix": 9}}, f)
        d = self.make().designs[1]
        self.assertEqual(d["heave_front"], 0.75)
        self.assertEqual(d["pitch_mix"], 1.0)          # clamped to its limit

    def test_unknown_design_in_config_falls_back_to_v13(self):
        with open(self.cfg_path) as f:
            text = f.read().replace('design = "v13"', 'design = "v99"')
        with open(self.cfg_path, "w") as f:
            f.write(text)
        self.assertEqual(self.make().design_id, "v13")

    # ---- bad input and limits ----
    def test_bad_screen_commands_change_nothing(self):
        self.screen("design:new")
        before = dict(self.rov.design())
        for cmd in ("design:zzz", "design_set:bogus:0.5", "design_set:pitch_mix:abc", "design_set:pitch_mix",
                    "design_set:", "design:"):
            self.screen(cmd)
        self.assertEqual(self.rov.design_id, "new")
        self.assertEqual(self.rov.design(), before)

    def test_values_are_clamped(self):
        self.screen("design_set:heave_front:0.0")
        self.assertEqual(self.rov.design()["heave_front"], 0.3)
        self.screen("design_set:surge_pitch:5")
        self.assertEqual(self.rov.design()["surge_pitch"], 0.5)

    # ---- safety ----
    def test_nothing_changes_while_armed(self):
        self.screen("design:new")
        self.rov.armed = True
        before = [dict(t.coef) for t in self.rov.thrusters]
        self.screen("design:v13")
        self.screen("design_set:pitch_mix:1.0")
        self.screen("design_reset")
        self.assertEqual(self.rov.design_id, "new")
        self.assertEqual([t.coef for t in self.rov.thrusters], before)
        self.assertIn("STOP", self.rov.status_msg)

    def test_no_motor_ever_exceeds_the_speed_limit(self):
        for d in SIM:
            self.screen("design:" + d)
            for s in (-1, 1):
                for h in (-1, 1):
                    for p in (-1, 1):
                        cmd = dict.fromkeys(rov.AXES, 0.0)
                        cmd.update(surge=s, heave=h, pitch=p, yaw=s)
                        self.assertTrue(all(abs(v) <= 0.5 + 1e-9 for v in rov.mix(self.rov.thrusters, cmd, 0.5)))

    def test_controller_buttons_cannot_switch_on_autopilot_or_change_design(self):
        codes = [f"BTN_{n}" for n in ("START", "EAST", "SELECT", "MODE", "NORTH", "WEST", "SOUTH", "TR", "TL",
                                      "TR2", "TL2", "THUMBL", "THUMBR")]
        e = types.SimpleNamespace(**{c: i for i, c in enumerate(codes)})
        self.rov.pad = types.SimpleNamespace(e=e, axes={}, name="test pad", close=lambda: None)
        self.rov.sticks_centred = lambda: True
        self.screen("design:new")
        for i in range(len(codes)):
            self.rov.handle_buttons([i])
            self.assertFalse(self.rov.ap.enabled, codes[i])
            self.assertEqual(self.rov.design_id, "new")

    def test_status_file_carries_designs_for_the_screen(self):
        self.screen("design:new")
        self.rov.write_status_file(rov.STATUS_PATH)
        with open(rov.STATUS_PATH) as f:
            st = json.load(f)
        self.assertEqual(st["design"], "new")
        self.assertEqual([d["id"] for d in st["designs"]], list(SIM))

    def test_updates_keep_the_settings(self):
        import updater
        with open(os.path.join(self.dir, "designs.json"), "w") as f:
            f.write("mine")
        with open(self.cfg_path) as f:
            before = f.read()
        updater.copy_files(ROOT, self.dir)
        with open(os.path.join(self.dir, "designs.json")) as f:
            self.assertEqual(f.read(), "mine")
        with open(self.cfg_path) as f:
            self.assertEqual(f.read(), before)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "dashboard", "pages", "drive.py")))


if __name__ == "__main__":
    unittest.main()
