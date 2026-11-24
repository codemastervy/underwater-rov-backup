"""Tests for the dashboard (no screen, controller or camera needed): it runs against the real motor program with
a fake Mega. Everything must work with the mouse alone and with the keyboard alone, at any resolution."""
import os
import shutil
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock

os.environ["SDL_VIDEODRIVER"] = "dummy"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import rov  # noqa: E402

try:
    import pygame
except ImportError:
    raise unittest.SkipTest("pygame is not installed")
from dashboard import app as dashboard  # noqa: E402
from dashboard import camera  # noqa: E402

SIZES = [(800, 480), (736, 436), (1024, 614), (1366, 768), (1920, 1080), (3840, 2160), (3440, 1440), (1024, 768),
         (480, 800), (900, 1000), (640, 400)]
CAMERAS = [("/dev/video0", "Integrated Webcam"), ("/dev/video2", "USB Video Capture")]
ARROWS = {"up": pygame.K_UP, "down": pygame.K_DOWN, "left": pygame.K_LEFT, "right": pygame.K_RIGHT}


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name in ("config.toml", "profiles.json", "camera.json"):
            shutil.copy(os.path.join(ROOT, name), self.dir)
        paths = {f"{name}_PATH": os.path.join(self.dir, name) for name in ("STATUS", "CMD", "KEYS", "VISION")}
        patches = [mock.patch.multiple(rov, HERE=self.dir, **paths), mock.patch.object(dashboard, "updater", None),
                   mock.patch.object(camera.Camera, "source", lambda self: None),       # never a real camera
                   mock.patch.object(camera, "list_cameras", lambda: list(CAMERAS))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.dir)
        config = os.path.join(self.dir, "config.toml")
        with open(config, "rb") as f:
            self.rov = rov.ROV(tomllib.load(f), rov.FakeBackend(), config)
        self.start((800, 480))

    def start(self, size):
        self.app = dashboard.App(fullscreen=False, size=size)
        pygame.event.clear()
        self.addCleanup(self.app.camera.stop)
        self.step()

    def step(self, frames=3):
        for _ in range(frames):
            self.rov.tick()
            self.rov.write_status_file(rov.STATUS_PATH)
            self.app.frame()

    def press(self, key, release=True, mod=0):
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=key, mod=mod, unicode=""))
        self.step()
        if release:
            self.release(key)

    def release(self, key):
        pygame.event.post(pygame.event.Event(pygame.KEYUP, key=key, mod=0))
        self.step()

    def click(self, wid):
        for dialog in self.app.dialogs:
            dialog.opened = 0                # long enough ago
        where = dict(self.app.ui.widgets)[wid].center
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=where, button=1))
        self.step(1)
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=where, button=1))
        self.step()

    def click_part(self, wid, share):
        rect = dict(self.app.ui.widgets)[wid]
        where = rect.x + round(rect.w * share), rect.centery
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=where, button=1))
        self.step(1)
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=where, button=1))
        self.step()

    def focus(self, wid):
        for _ in range(80):
            if self.app.ui.ring and self.app.ui.focus == wid:
                return
            self.press(pygame.K_TAB)
        self.fail(f"Tab never gets to {wid}")

    def ids(self):
        return [wid for wid, _ in self.app.ui.widgets]


class LayoutTests(DashboardTest):
    def variants(self):
        app = self.app
        for name in app.pages:
            app.show(name)
            self.step(1)
            yield name
        app.show("profiles")
        for i, profile in enumerate(app.page.profiles):
            app.page.index = i
            self.step(1)
            yield "profile " + profile["name"]
        self.click("name")
        yield "text dialog"
        app.dialogs[-1].close()
        app.page.index = next(i for i, p in enumerate(app.page.profiles) if p["type"] == "keyboard")
        self.step(1)
        self.click(("key", "forward"))
        yield "key dialog"
        app.dialogs[-1].close()
        self.press(pygame.K_F10)
        yield "help"
        app.dialogs[-1].close()
        app.show("camera")
        app.page.picture_open = True
        self.step(1)
        yield "picture"
        app.page.toggle_full()
        self.step(1)
        yield "full video"

    def test_everything_fits_at_every_resolution(self):
        for size in SIZES:
            self.start(size)
            screen = pygame.Rect((0, 0), size)
            for variant in self.variants():
                where = f"{size} {variant}"
                self.app.ui.written = []
                self.step(1)
                for text, rect in self.app.ui.written:
                    self.assertTrue(screen.contains(rect), f"{where}: {text!r} is off the screen")
                widgets = self.app.ui.widgets
                self.assertEqual(len(set(self.ids())), len(widgets), f"{where}: two widgets with one id")
                for i, (wid, rect) in enumerate(widgets):
                    self.assertTrue(screen.contains(rect), f"{where}: {wid} is off the screen")
                    self.assertGreaterEqual(min(rect.size), 14, f"{where}: {wid} is too small to click")
                    if "video" in variant or "picture" in variant or variant == "camera":
                        continue                       # controls lie on top of the video there
                    for other, b in widgets[i + 1:]:
                        self.assertFalse(rect.inflate(-2, -2).colliderect(b), f"{where}: {wid} overlaps {other}")
                self.app.ui.written = None

    def test_everything_stays_inside_the_edges(self):
        for size in ((800, 480), (1366, 768), (640, 400), (480, 800)):
            self.start(size)
            self.app.fullscreen = True
            for edge in (None, 12):
                self.app.saved_edge = edge
                for variant in self.variants():
                    self.app.ui.written = []
                    self.step(1)
                    where = f"{size} {edge} {variant}"
                    for what, rect in self.app.ui.written + self.app.ui.widgets:
                        self.assertTrue(self.app.ui.area.contains(rect), f"{where}: {what!r} is in the hidden edge")
                    self.app.ui.written = None

    def test_menu_stays_inside_the_edges(self):
        self.app.fullscreen = True
        self.app.saved_edge = 12
        self.app.show("system")
        self.step()
        self.click("edge")
        self.assertTrue(self.app.ui.area.contains(self.app.ui.menu.box))

    def test_edge_is_picked_and_kept(self):
        self.app.fullscreen = True
        self.app.show("system")
        self.step()
        self.assertEqual(self.app.edge, 5)              # the case monitor
        self.focus("edge")
        self.press(pygame.K_RIGHT)
        self.assertEqual(self.app.edge, 6)
        self.assertEqual(self.app.ui.area, pygame.Rect(48, 29, 704, 422))
        self.assertEqual(dashboard.screen.load_edge(), 6)
        self.app.fullscreen = False
        self.assertEqual(self.app.edge, 0)

    def test_bad_saved_edge_is_ignored(self):
        for text in ("", "[]", '{"edge": 99}', '{"edge": "5"}', '{"edge": true}'):
            with open(dashboard.screen.settings_path(), "w") as f:
                f.write(text)
            self.assertIsNone(dashboard.screen.load_edge())

    def test_window_fits_a_small_desktop(self):
        with mock.patch.object(pygame.display, "get_desktop_sizes", lambda: [(800, 480)]):
            self.assertEqual(dashboard.screen.window_size(), (720, 432))
        with mock.patch.object(pygame.display, "get_desktop_sizes", lambda: [(1920, 1080)]):
            self.assertEqual(dashboard.screen.window_size(), (1200, 720))

    def test_window_can_be_resized(self):
        for size in ((1366, 768), (500, 900), (800, 480)):
            pygame.display.set_mode(size, pygame.RESIZABLE)
            self.step()
            self.assertEqual(self.app.ui.size, size)


class KeyboardOnlyTests(DashboardTest):
    def test_every_widget_can_be_reached_with_tab_and_with_arrows(self):
        for name in self.app.pages:
            self.app.show(name)
            self.step()
            everything = set(self.ids())
            self.press(pygame.K_TAB)
            seen = set()
            for _ in range(len(everything) + 1):
                seen.add(self.app.ui.focus)
                self.press(pygame.K_TAB)
            self.assertEqual(seen, everything, f"{name}: Tab")
            ui = self.app.ui
            reached, waiting = set(), [self.ids()[0]]
            while waiting:
                wid = waiting.pop()
                if wid in reached:
                    continue
                reached.add(wid)
                for move in ARROWS:
                    ui.focus, ui.move = wid, move
                    waiting.append(ui._neighbour(self.ids()))
            ui.move = None
            self.assertEqual(reached, everything, f"{name}: arrows")

    def test_function_keys_open_the_pages(self):
        for i, name in enumerate(dashboard.TABS):
            self.press(getattr(pygame, f"K_F{i + 1}"))
            self.assertEqual(self.app.page.name, name)

    def test_enter_left_and_right_use_widgets(self):
        self.press(pygame.K_F3)
        self.focus("master")
        self.press(pygame.K_RETURN)
        self.assertTrue(self.rov.ap.enabled)
        self.focus(("ap", "power"))
        for _ in range(3):
            self.press(pygame.K_RIGHT)
        self.step(6)
        self.assertEqual(self.rov.ap.s["power"], 0.65)
        self.focus(("ap", "line_colour"))
        self.press(pygame.K_RETURN)              # the list
        self.press(pygame.K_DOWN)
        self.press(pygame.K_RETURN)
        self.step(6)
        self.assertEqual(self.rov.ap.s["line_colour"], rov.AP_COLOURS[1])

    def test_a_profile_is_made_renamed_bound_and_used(self):
        self.press(pygame.K_F4)
        self.focus("new keys")
        self.press(pygame.K_RETURN)
        self.focus("name")
        self.press(pygame.K_RETURN)
        self.press(pygame.K_u, mod=pygame.KMOD_CTRL)
        pygame.event.post(pygame.event.Event(pygame.TEXTINPUT, text="Mine"))
        self.step()
        self.press(pygame.K_RETURN)
        self.focus(("key", "forward"))
        self.press(pygame.K_RETURN)
        self.press(pygame.K_i)
        self.focus("use")
        self.press(pygame.K_RETURN)
        self.step(6)
        self.assertEqual(self.rov.profile()["name"], "Mine")
        self.assertEqual(self.rov.profile()["keys"]["forward"], "i")

    def test_a_colour_is_picked_with_the_arrow_keys(self):
        self.press(pygame.K_F2)
        self.focus("PICK")
        self.press(pygame.K_RETURN)
        self.assertTrue(self.app.page.picking)
        data = bytes((255, 0, 0)) * (camera.WIDTH * camera.HEIGHT)
        before = self.app.page.crosshair[0]
        self.press(pygame.K_RIGHT, release=False)
        time.sleep(0.05)
        self.step()
        self.release(pygame.K_RIGHT)
        self.assertGreater(self.app.page.crosshair[0], before)
        self.app.camera.frame = camera.Frame(1, time.monotonic(), data, None)
        with mock.patch.object(camera.Camera, "update", lambda *a: None):
            self.press(pygame.K_RETURN)
            self.step(6)
        self.assertEqual(self.rov.ap.s["target_pick"], [0.0, 1.0, 1.0])

    def test_confirm_dialogs_start_on_cancel(self):
        self.press(pygame.K_F6)
        self.focus("CLOSE DASHBOARD")
        self.press(pygame.K_RETURN)
        self.assertTrue(self.app.dialogs)
        self.press(pygame.K_RETURN)
        self.step(6)
        self.assertFalse(self.app.dialogs)
        self.assertTrue(self.app.running)

    def test_escape_stops_first_and_goes_back_when_safe(self):
        self.press(pygame.K_F5)
        self.rov.pad = mock.Mock(axes=dict.fromkeys(("lx", "ly", "rx", "ry", "l2", "r2"), 0.0), motion=None)
        self.rov.pad.name = "pad"
        self.rov.arm("controller")
        self.step()
        self.press(pygame.K_ESCAPE)
        self.assertFalse(self.rov.armed)
        self.assertEqual(self.app.page.name, "motors")
        self.press(pygame.K_ESCAPE)
        self.assertEqual(self.app.page.name, "drive")


class MouseOnlyTests(DashboardTest):
    def test_tabs_buttons_numbers_and_lists(self):
        self.click(("tab", "autopilot"))
        self.click("master")
        self.assertTrue(self.rov.ap.enabled)
        self.click_part(("ap", "power"), 0.97)
        self.step(6)
        self.assertEqual(self.rov.ap.s["power"], 0.55)
        self.click_part(("ap", "line_colour"), 0.5)
        menu = self.app.ui.menu
        self.assertIsNotNone(menu)
        row = menu.box.x + 30, menu.box.y + round(self.app.ui.px(32) * 3.4)
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=row, button=1))
        self.step(8)
        self.assertEqual(self.rov.ap.s["line_colour"], rov.AP_COLOURS[3])

    def test_dragging_a_number(self):
        self.click(("tab", "system"))
        rect = dict(self.app.ui.widgets)["volume"]
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=rect.center, button=1))
        self.step(8)
        self.assertEqual(self.rov.sound_volume, 8)
        pygame.event.post(pygame.event.Event(pygame.MOUSEMOTION, pos=(rect.right - 5, rect.centery), rel=(50, 0),
                                             buttons=(1, 0, 0)))
        self.step(8)
        self.assertEqual(self.rov.sound_volume, 15)

    def test_a_profile_is_renamed_and_bound_without_a_keyboard(self):
        self.click(("tab", "profiles"))
        self.click("new keys")
        self.click("name")
        for letter in ("z", "7"):
            self.click(("letter", letter))
        self.click("OK")
        self.click(("key", "forward"))
        self.click(("key", "o"))
        self.click("use")
        self.step(6)
        self.assertEqual(self.rov.profile()["name"], "New keysz7")
        self.assertEqual(self.rov.profile()["keys"]["forward"], "o")

    def test_binding_a_used_key_swaps(self):
        self.click(("tab", "profiles"))
        self.click("new keys")
        self.click(("key", "forward"))
        self.click(("key", "s"))
        keys = self.app.page.profile["keys"]
        self.assertEqual((keys["forward"], keys["back"]), ("s", "w"))

    def test_the_drive_page_picks_a_profile(self):
        self.click("profile")
        menu = self.app.ui.menu
        row = menu.box.x + 30, menu.box.y + round(self.app.ui.px(32) * 0.5) + 4
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=row, button=1))
        self.step(8)
        self.assertEqual(self.rov.profile()["name"], "Tank")

    def test_designs(self):
        self.click(("tab", "motors"))
        self.click(("design", "new"))
        self.step(6)
        self.assertEqual(self.rov.design_id, "new")
        self.click_part(("trim", "new", "heave_front"), 0.03)
        self.step(6)
        self.assertEqual(self.rov.design()["heave_front"], 0.7)

    def test_nothing_clicked_ever_arms(self):
        self.rov.use_profile("Keys WASD")
        for name in self.app.pages:
            self.app.show(name)
            self.step()
            for wid in self.ids():
                if wid in self.ids() and wid not in ("CLOSE DASHBOARD", "fullscreen"):
                    self.click(wid)
                    self.assertFalse(self.rov.armed, f"{name}: {wid}")
                while self.app.dialogs:
                    self.app.dialogs[-1].close()
                if self.app.ui.menu:
                    self.app.ui.close_menu()
                self.app.show(name)
                self.step(1)


class DriveTests(DashboardTest):
    def keyboard_profile(self, name="Keys WASD"):
        self.rov.use_profile(name)
        self.step()

    def arm(self):
        self.press(pygame.K_RETURN, release=False)
        time.sleep(0.55)
        self.step()
        self.release(pygame.K_RETURN)
        self.assertTrue(self.rov.armed)
        self.rov.end_tune()

    def test_hold_enter_arms_and_a_tap_does_not(self):
        self.keyboard_profile()
        self.press(pygame.K_RETURN)
        self.assertFalse(self.rov.armed)
        self.arm()

    def test_enter_never_arms_on_other_pages_or_with_the_ring(self):
        self.keyboard_profile()
        for key in (pygame.K_F4, pygame.K_TAB):
            self.press(pygame.K_F1)
            self.press(key)
            self.press(pygame.K_RETURN, release=False)
            time.sleep(0.55)
            self.step()
            self.release(pygame.K_RETURN)
            self.assertFalse(self.rov.armed)
            while self.app.dialogs:
                self.app.dialogs[-1].close()
            if self.app.ui.menu:
                self.app.ui.close_menu()

    def test_enter_never_arms_with_a_controller_profile(self):
        self.press(pygame.K_RETURN, release=False)
        time.sleep(0.55)
        self.step()
        self.assertFalse(self.rov.armed)

    def test_keys_drive_and_let_go(self):
        self.keyboard_profile()
        self.arm()
        self.press(pygame.K_w, release=False)
        self.step(20)
        self.assertGreater(self.rov.last_pilot["surge"], 0.9)
        self.release(pygame.K_w)
        self.step(20)
        self.assertEqual(self.rov.last_pilot["surge"], 0)

    def test_menu_keys_never_drive(self):
        self.keyboard_profile("Keys arrows")
        self.arm()
        self.press(pygame.K_UP, release=False)
        self.step(4)
        self.assertTrue(self.rov.kbd_keys)
        self.press(pygame.K_TAB)                        # now the keys work the screen
        self.step(4)
        self.assertFalse(self.rov.kbd_keys)
        self.press(pygame.K_UP, release=False)          # still held: a repeat
        self.press(pygame.K_LEFT, release=False)
        self.assertFalse(self.rov.kbd_keys)
        self.assertTrue(self.rov.armed)
        self.press(pygame.K_BACKSPACE)                  # back to driving
        self.release(pygame.K_LEFT)
        self.press(pygame.K_LEFT, release=False)
        self.step(4)
        self.assertEqual(self.rov.kbd_keys, {"left"})

    def test_opening_another_page_lets_go_of_the_keys(self):
        self.keyboard_profile()
        self.arm()
        self.press(pygame.K_w, release=False)
        self.press(pygame.K_F3)
        self.step(4)
        self.assertFalse(self.rov.kbd_keys)
        self.assertTrue(self.rov.armed)

    def test_losing_the_window_focus_disarms(self):
        self.keyboard_profile()
        self.arm()
        pygame.event.post(pygame.event.Event(pygame.WINDOWFOCUSLOST))
        self.step()
        time.sleep(0.6)
        self.step()
        self.assertFalse(self.rov.armed)

    def test_cruise_with_the_keyboard_alone(self):
        self.keyboard_profile()
        self.press(pygame.K_F3)
        self.focus("master")
        self.press(pygame.K_RETURN)
        self.press(pygame.K_F1)
        self.arm()
        self.press(pygame.K_w, release=False)
        self.step(10)
        self.press(pygame.K_1)
        self.step(10)
        self.assertEqual(self.rov.ap.mode, "cruise")
        self.release(pygame.K_w)
        self.step(10)
        self.assertEqual(self.rov.ap.mode, "cruise")

    def test_stop_works_everywhere(self):
        self.keyboard_profile()
        for how in ("escape", "dialog", "text", "button"):
            self.press(pygame.K_F1)
            self.arm()
            if how == "dialog":
                self.press(pygame.K_F10)
                self.click("stop")
            elif how == "text":
                self.press(pygame.K_F6)
                self.click("SET TUNE")
                self.press(pygame.K_ESCAPE)
            elif how == "button":
                self.press(pygame.K_F5)
                self.click("stop")
            else:
                self.press(pygame.K_ESCAPE)
            self.step(4)
            self.assertFalse(self.rov.armed, how)

    def test_stop_goes_ahead_of_waiting_commands(self):
        for value in range(20):
            self.app.link.send(f"volume:{value % 15 + 1}")
        self.app.stop()
        with open(rov.CMD_PATH) as f:
            self.assertEqual(f.read(), "stop")
        self.assertFalse(self.app.link._waiting)

    def test_p_takes_the_keyboard(self):
        self.press(pygame.K_p)
        self.step(6)
        self.assertEqual(self.rov.profile()["name"], "Keys WASD")

    def test_nothing_is_offered_while_armed(self):
        self.keyboard_profile()
        self.arm()
        for page, locked in (("profiles", ("new keys", "name", "use", "DELETE")), ("motors", (("flip", 0),)),
                             ("system", ("test tune",))):
            self.app.show(page)
            self.step()
            for wid in locked:
                self.assertNotIn(wid, self.ids())

    def test_offline(self):
        os.remove(rov.STATUS_PATH)
        self.app.link.status.ts = 0
        for name in self.app.pages:
            self.app.show(name)
            self.app.frame()
        self.assertFalse(self.app.status.online)


class MegaTests(DashboardTest):
    def mega(self, ok, text):
        self.rov.gpio.ready, self.rov.gpio.describe = lambda: ok, lambda: text
        self.step(6)

    def test_a_missing_mega_is_shown_and_cannot_arm(self):
        self.rov.use_profile("Keys WASD")
        self.mega(False, "Arduino Mega not found")
        self.assertEqual(self.app.message()[0], "⚠ Arduino Mega not found")
        self.press(pygame.K_RETURN, release=False)
        time.sleep(0.55)
        self.step(6)
        self.release(pygame.K_RETURN)
        self.assertFalse(self.rov.armed)
        self.assertIn("Can't arm", self.rov.status_msg)
        self.press(pygame.K_F6)
        self.app.ui.written = []
        self.step(1)
        self.assertIn("Arduino Mega not found", [text for text, _ in self.app.ui.written])

    def test_losing_the_mega_disarms(self):
        self.rov.use_profile("Keys WASD")
        self.mega(True, "Mega OK")
        self.press(pygame.K_RETURN, release=False)
        time.sleep(0.55)
        self.step(6)
        self.release(pygame.K_RETURN)
        self.assertTrue(self.rov.armed)
        self.mega(False, "Mega disconnected")
        self.assertFalse(self.rov.armed)


class CameraTests(DashboardTest):
    def test_the_capture_card_goes_before_the_webcam(self):
        pick = camera.pick_camera
        self.assertEqual(pick(CAMERAS, None, None), "/dev/video2")
        self.assertEqual(pick(CAMERAS, None, "/dev/video0"), "/dev/video0")
        self.assertEqual(pick(CAMERAS, None, "/dev/video9"), "/dev/video2")         # unplugged
        self.assertEqual(pick(CAMERAS, "/dev/video7", "/dev/video0"), "/dev/video7")
        self.assertEqual(pick(CAMERAS[:1], None, None), "/dev/video0")
        self.assertIsNone(pick([], None, None))

    def test_a_camera_is_picked_and_kept(self):
        self.press(pygame.K_F2)
        self.focus("which camera")
        self.press(pygame.K_RIGHT)
        self.assertEqual(self.app.camera.device, "/dev/video0")
        self.app.camera.next_flip()                       # saving something else keeps it
        self.assertEqual(camera.load_settings()[2], "/dev/video0")

    def test_config_forces_a_camera(self):
        path = os.path.join(self.dir, "config.toml")
        with open(path) as f:
            text = f.read().replace('device = "auto"', 'device = "/dev/video5"')
        with open(path, "w") as f:
            f.write(text)
        self.start((800, 480))
        self.press(pygame.K_F2)
        self.assertEqual(self.app.camera.device, "/dev/video5")
        self.assertNotIn("which camera", self.ids())


class StartTests(unittest.TestCase):
    def test_opens_full_screen(self):
        with mock.patch.object(pygame.display, "set_mode", wraps=pygame.display.set_mode) as set_mode, \
                mock.patch.object(dashboard, "updater", None):
            dashboard.App()
        set_mode.assert_called_once_with((0, 0), pygame.FULLSCREEN)

    def test_frame_rate_is_not_limited(self):
        with mock.patch.object(dashboard, "updater", None):
            self.assertEqual(dashboard.App(fullscreen=False).fps, 0)


if __name__ == "__main__":
    unittest.main()
