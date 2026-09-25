"""Offline tests for the display reading and the plan built from it.

The numbers here are this machine's, measured: a 1512x982 Retina panel whose
available area loses 86px to the menu bar and dock, Chrome reporting dpr 2,
colorDepth 30, `en-US,en` and an `arm` architecture hint. The tests exist because
each of those was wrong before - a 1280x820 viewport standing in for a screen, dpr 1,
`en-US` alone, and (the subtle one) an `x86` architecture hint on Apple Silicon, which
Playwright derived from the Intel user-agent string it was handed.

    cd ~/tools/latchkey
    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import fingerprint as fp  # noqa: E402

DISPLAY = fp.Display(width=1512, height=982, avail_width=1512, avail_height=896,
                     dpr=2.0, color_depth=30, source="NSScreen")


class TestDisplayReading(unittest.TestCase):
    def test_nsscreen_json_becomes_a_display(self):
        payload = json.dumps({"w": 1512, "h": 982, "aw": 1512, "ah": 896, "scale": 2})
        with mock.patch.object(fp, "_run", return_value=payload):
            display = fp._display_jxa()
        self.assertEqual(display.width, 1512)
        self.assertEqual(display.avail_height, 896)
        self.assertEqual(display.dpr, 2.0)
        self.assertEqual(display.color_depth, 30)

    def test_a_broken_screen_reading_is_none_not_an_exception(self):
        for payload in ("", "not json", "{}", json.dumps({"w": 0, "h": 0})):
            with mock.patch.object(fp, "_run", return_value=payload):
                self.assertIsNone(fp._display_jxa())

    def test_system_profiler_fallback_halves_a_retina_panel(self):
        payload = json.dumps({"SPDisplaysDataType": [
            {"spdisplays_resolution": "3024 x 1964 Retina",
             "spdisplays_main": "spdisplays_yes"}]})
        with mock.patch.object(fp, "_run", return_value=payload):
            display = fp._display_system_profiler()
        self.assertEqual((display.width, display.height), (1512, 982))
        self.assertEqual(display.dpr, 2)
        self.assertTrue(display.approximate)

    def test_a_non_retina_panel_is_not_halved(self):
        payload = json.dumps({"SPDisplaysDataType": [
            {"spdisplays_resolution": "1920 x 1080", "spdisplays_main": "spdisplays_yes"}]})
        with mock.patch.object(fp, "_run", return_value=payload):
            display = fp._display_system_profiler()
        self.assertEqual((display.width, display.height), (1920, 1080))
        self.assertEqual(display.dpr, 1)


class TestWindow(unittest.TestCase):
    def test_the_window_is_a_window_not_the_screen(self):
        # Chrome opened 1200x765 of viewport on this screen. The point of the helpers is
        # that screen and viewport stop being the same number.
        self.assertEqual(fp.default_window(DISPLAY), (1200, 765))

    def test_a_small_screen_gets_a_smaller_window(self):
        small = fp.Display(width=1280, height=800, avail_width=1280, avail_height=775)
        width, height = fp.default_window(small)
        self.assertEqual(width, 1160)
        self.assertLess(height, small.avail_height)

    def test_a_short_screen_clamps_the_height(self):
        short = fp.Display(width=1440, height=700, avail_width=1440, avail_height=620)
        width, height = fp.default_window(short)
        self.assertGreaterEqual(height, 600)
        self.assertLessEqual(height, 620)


class TestWorkArea(unittest.TestCase):
    def test_the_work_area_is_the_visible_frame(self):
        # NSScreen's visible frame: 896 tall on a 982-tall screen, starting 33px down
        # (origin.y 53 + height 896 leaves the menu bar's 33 above it).
        self.assertEqual(fp.work_area(DISPLAY), (0, 33, 1512, 896))

    def test_the_jxa_origin_becomes_the_top_of_the_work_area(self):
        payload = json.dumps({"w": 1512, "h": 982, "aw": 1512, "ah": 896, "ax": 0, "ay": 53,
                              "scale": 2})
        with mock.patch.object(fp, "_run", return_value=payload):
            display = fp._display_jxa()
        self.assertEqual(display.avail_top, 33)
        self.assertEqual(fp.work_area(display), (0, 33, 1512, 896))

    def test_a_reading_without_a_work_area_is_derived_never_left_at_the_screen(self):
        flat = fp.Display(width=1512, height=982, avail_width=1512, avail_height=982)
        width, height = fp.work_area(flat)[2:]
        self.assertEqual(fp.work_area(flat), (0, 33, 1512, 896))
        self.assertLess(height, flat.height)      # a menu bar and a Dock exist
        self.assertLessEqual(width, flat.width)

    def test_the_fallback_reading_still_has_a_work_area_below_the_screen(self):
        payload = json.dumps({"SPDisplaysDataType": [
            {"spdisplays_resolution": "3024 x 1964 Retina",
             "spdisplays_main": "spdisplays_yes"}]})
        with mock.patch.object(fp, "_run", return_value=payload):
            display = fp._display_system_profiler()
        self.assertEqual(fp.work_area(display)[1], fp.MENU_BAR_HEIGHT)
        self.assertLess(fp.work_area(display)[3], display.height)


class TestIdentity(unittest.TestCase):
    def test_the_headless_token_is_swapped_for_the_real_version(self):
        seen = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36")
        with mock.patch.object(fp, "chrome_version", return_value="153.0.8010.37"):
            cleaned = fp.clean_ua(seen)
        self.assertNotIn("HeadlessChrome", cleaned)
        self.assertIn("Chrome/153.0.8010.37", cleaned)
        self.assertTrue(cleaned.startswith("Mozilla/5.0 (Macintosh;"))

    def test_architecture_comes_from_the_machine_not_the_user_agent(self):
        with mock.patch.object(fp._platform, "machine", return_value="arm64"):
            self.assertEqual(fp.architecture(), "arm")
        with mock.patch.object(fp._platform, "machine", return_value="x86_64"):
            self.assertEqual(fp.architecture(), "x86")

    def test_languages_come_from_the_profile_not_from_a_default(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "Default"))
            with open(os.path.join(root, "Default", "Preferences"), "w") as handle:
                json.dump({"intl": {"accept_languages": "en-US,en,bg"}}, handle)
            self.assertEqual(fp.profile_languages([root]), "en-US,en,bg")

    def test_a_profile_without_preferences_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(fp.profile_languages([root]), "en-US,en")

    def test_a_corrupt_preferences_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "Default"))
            with open(os.path.join(root, "Default", "Preferences"), "w") as handle:
                handle.write("{not json")
            self.assertEqual(fp.profile_languages([root]), "en-US,en")


class TestPlan(unittest.TestCase):
    def test_native_off_keeps_the_old_fixed_frame(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(width=1280, height=820, native=False, accept_language="en-US,en")
        self.assertEqual((plan.width, plan.height), (1280, 820))
        self.assertEqual(plan.dpr, 1.0)
        self.assertIsNone(plan.screen_width)
        self.assertEqual(plan.residuals, [])

    def test_native_on_reports_this_machines_screen(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(accept_language="en-US,en")
        self.assertEqual((plan.width, plan.height), (1200, 765))
        self.assertEqual((plan.screen_width, plan.screen_height), (1512, 982))
        self.assertEqual(plan.dpr, 2.0)
        self.assertEqual(plan.color_scheme, "dark")
        self.assertTrue(plan.native)

    def test_what_cannot_be_matched_is_written_down(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(accept_language="en-US,en")
        # The screen, its work area and its colour depth are Chrome's own now (the
        # `--screen-info` switch), and the window has real bounds: nothing is left to
        # confess for a window that fits its work area.
        self.assertEqual(plan.residuals, [])

    def test_a_requested_size_still_wins_over_the_display(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(width=1000, height=700, accept_language="en-US,en")
        self.assertEqual((plan.width, plan.height), (1000, 700))
        self.assertEqual(plan.screen_width, 1512)      # the screen is still the screen

    def test_no_display_means_no_native_claims(self):
        with mock.patch.object(fp, "display", return_value=None), \
             mock.patch.object(fp, "appearance", return_value=None):
            plan = fp.plan(accept_language="en-US,en")
        self.assertFalse(plan.native)
        self.assertEqual((plan.width, plan.height), (1280, 820))
        self.assertIn("no display", " ".join(plan.residuals))

    def test_the_window_holds_the_viewport_and_its_own_chrome(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(accept_language="en-US,en")
        self.assertEqual(plan.outer_width, plan.width)        # no side borders on macOS
        self.assertEqual(plan.outer_height, 765 + fp.BROWSER_CHROME_HEIGHT)
        self.assertGreater(plan.outer_height, plan.height)    # a window, not a frame
        self.assertEqual((plan.window_x, plan.window_y), (0, 33))
        self.assertEqual((plan.avail_width, plan.avail_height), (1512, 896))
        self.assertLessEqual(plan.window_y + plan.outer_height, 33 + plan.avail_height)

    def test_a_hand_asked_window_larger_than_the_work_area_is_written_down(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(width=1400, height=950, accept_language="en-US,en")
        self.assertEqual((plan.width, plan.height), (1400, 950))   # the request still wins
        self.assertIn("work area", " ".join(plan.residuals))

    def test_native_off_plans_no_window_at_all(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(width=1280, height=820, native=False, accept_language="en-US,en")
        self.assertIsNone(plan.outer_height)
        self.assertIsNone(plan.avail_height)

    def test_the_summary_is_one_line(self):
        with mock.patch.object(fp, "display", return_value=DISPLAY), \
             mock.patch.object(fp, "appearance", return_value="dark"):
            plan = fp.plan(accept_language="en-US,en")
        line = plan.summary()
        self.assertIn("1200x765@2x", line)
        self.assertIn("screen 1512x982", line)
        self.assertNotIn("\n", line)


PLAN = fp.Plan(width=1200, height=765, screen_width=1512, screen_height=982,
               avail_width=1512, avail_height=896, outer_width=1200, outer_height=852,
               window_x=0, window_y=33, dpr=2.0, native=True)


class TestApply(unittest.TestCase):
    class FakeCdp:
        def __init__(self, fail=()):
            self.sent = []
            self.fail = set(fail)

        answers: dict = {}

        def send(self, method, params=None):
            if method in self.fail:
                raise RuntimeError(f"{method} refused")
            self.sent.append((method, params))
            return dict(self.answers.get(method, {}))

    def test_the_screen_switch_describes_this_display_in_physical_pixels(self):
        switch = fp.screen_info_switch(PLAN)
        self.assertTrue(switch.startswith("--screen-info={0,0 "))
        # A 1512x982 screen at 2x is 3024x1964 physical pixels, and Chrome divides back.
        self.assertIn("3024x1964", switch)
        self.assertIn("devicePixelRatio=2", switch)
        self.assertIn(f"colorDepth={PLAN.color_depth}", switch)
        # The work area is given as insets: the menu bar above, the Dock below, both 2x.
        self.assertIn(f"workAreaTop={PLAN.window_y * 2}", switch)
        bottom = (PLAN.screen_height - PLAN.window_y - PLAN.avail_height) * 2
        self.assertIn(f"workAreaBottom={bottom}", switch)
        self.assertNotIn("workAreaLeft", switch)       # a zero inset is not written
        self.assertNotIn("isInternal", switch)         # Chrome 153 rejects the token

    def test_without_a_native_plan_the_screen_only_has_to_fit_the_window(self):
        self.assertEqual(fp.screen_info_switch(fp.Plan(width=1200, height=765)),
                         "--screen-info={1200x852}")
        self.assertEqual(fp.screen_info_switch(fp.Plan(width=300, height=200)),
                         "--screen-info={800x600}")

    def test_the_window_bounds_are_the_plans_own_numbers(self):
        bounds = fp.window_bounds(PLAN)
        self.assertEqual(bounds["width"], PLAN.width)
        self.assertEqual(bounds["height"], PLAN.height + fp.BROWSER_CHROME_HEIGHT)
        self.assertEqual((bounds["left"], bounds["top"]), (0, 33))
        self.assertEqual(fp.window_bounds(fp.Plan(width=1000, height=600))["height"],
                         600 + fp.BROWSER_CHROME_HEIGHT)

    def test_the_window_is_given_bounds_through_the_browser_not_the_page(self):
        cdp = self.FakeCdp()
        cdp.answers = {"Browser.getWindowForTarget": {"windowId": 7, "bounds": {}}}
        self.assertTrue(fp.apply_window_bounds(cdp, PLAN))
        methods = [m for m, _ in cdp.sent]
        self.assertEqual(methods, ["Browser.getWindowForTarget", "Browser.setWindowBounds"])
        _, params = cdp.sent[1]
        self.assertEqual(params["windowId"], 7)
        self.assertEqual(params["bounds"], fp.window_bounds(PLAN))
        # Nothing emulated, nothing scripted: no metrics override, no init script.
        self.assertNotIn("Emulation.setDeviceMetricsOverride", methods)
        self.assertFalse(hasattr(fp, "window_script"))
        self.assertFalse(hasattr(fp, "WINDOW_JS"))

    def test_refused_bounds_are_reported_not_raised(self):
        cdp = self.FakeCdp(fail=["Browser.setWindowBounds"])
        cdp.answers = {"Browser.getWindowForTarget": {"windowId": 7}}
        self.assertFalse(fp.apply_window_bounds(cdp, PLAN))
        self.assertFalse(fp.apply_window_bounds(self.FakeCdp(fail=["Browser.getWindowForTarget"]),
                                                PLAN))

    def test_blank_high_entropy_hints_are_rebuilt_from_the_brands_and_the_binary(self):
        # `--user-agent` blanks everything but the brands; the untouched Chrome's full
        # versions are the binary's own, and the GREASE brand's is its major at .0.0.0.
        measured = {"brands": [{"brand": "Google Chrome", "version": "153"},
                               {"brand": "Not_A Brand", "version": "8"},
                               {"brand": "Chromium", "version": "153"}],
                    "fullVersionList": [], "uaFullVersion": "", "platform": "macOS",
                    "platformVersion": "", "bitness": "", "model": "", "mobile": False}
        with mock.patch.object(fp, "chrome_version", return_value="153.0.8010.37"), \
             mock.patch.object(fp, "os_version", return_value="26.6.2"), \
             mock.patch.object(fp, "architecture", return_value="arm"):
            meta = fp.user_agent_metadata(measured, fp.Plan())
        self.assertEqual(meta["fullVersion"], "153.0.8010.37")
        self.assertEqual(meta["fullVersionList"], [
            {"brand": "Google Chrome", "version": "153.0.8010.37"},
            {"brand": "Not_A Brand", "version": "8.0.0.0"},
            {"brand": "Chromium", "version": "153.0.8010.37"}])
        self.assertEqual(meta["bitness"], "64")
        self.assertEqual((meta["architecture"], meta["platformVersion"]), ("arm", "26.6.2"))

    def test_metadata_is_built_from_what_the_browser_reported(self):
        measured = {"brands": [{"brand": "Google Chrome", "version": "153"}],
                    "fullVersionList": [{"brand": "Google Chrome", "version": "153.0.8010.37"}],
                    "uaFullVersion": "153.0.8010.37", "platform": "macOS",
                    "platformVersion": "10_15_7", "bitness": "64", "model": "",
                    "mobile": False, "wow64": False}
        with mock.patch.object(fp, "architecture", return_value="arm"), \
             mock.patch.object(fp, "os_version", return_value=""):
            meta = fp.user_agent_metadata(measured, fp.Plan())
        self.assertEqual(meta["brands"], measured["brands"])          # never invented
        self.assertEqual(meta["platformVersion"], "10_15_7")         # the measurement,
        self.assertEqual(meta["architecture"], "arm")                 # when there is no
        self.assertEqual(meta["bitness"], "64")                       # OS answer

    def test_the_os_version_beats_the_one_measured_under_an_override(self):
        # Measured: an untouched Chrome reports the same string `sw_vers` prints (26.6.2),
        # while anything measured under Playwright's user agent says the frozen 10_15_7.
        measured = {"brands": [{"brand": "Google Chrome", "version": "153"}],
                    "platformVersion": "10_15_7", "platform": "macOS"}
        with mock.patch.object(fp, "architecture", return_value="arm"), \
             mock.patch.object(fp, "os_version", return_value="26.6.2"):
            meta = fp.user_agent_metadata(measured, fp.Plan())
        self.assertEqual(meta["platformVersion"], "26.6.2")

    def test_no_measurement_means_no_metadata(self):
        self.assertIsNone(fp.user_agent_metadata({}, fp.Plan()))
        self.assertIsNone(fp.user_agent_metadata({"brands": []}, fp.Plan()))

    def test_a_clean_user_agent_is_left_alone(self):
        class Page:
            def evaluate(self, _script):
                return "Mozilla/5.0 (Macintosh) Chrome/153.0.8010.37 Safari/537.36"
        cdp = self.FakeCdp()
        result = fp.ensure_ua(Page(), cdp, fp.Plan(ua="Mozilla/5.0 (Macintosh) Chrome/153"))
        self.assertFalse(result["patched"])
        self.assertFalse(result["headless"])
        self.assertEqual(cdp.sent, [])            # no round trip beyond the read

    def test_a_surviving_headless_token_is_corrected(self):
        class Page:
            def evaluate(self, _script):
                return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " \
                       "HeadlessChrome/153.0.0.0 Safari/537.36"
        cdp = self.FakeCdp()
        with mock.patch.object(fp, "chrome_version", return_value="153.0.8010.37"):
            result = fp.ensure_ua(Page(), cdp, fp.Plan(ua=""))
        self.assertTrue(result["patched"])
        self.assertEqual(cdp.sent[0][0], "Emulation.setUserAgentOverride")
        self.assertNotIn("HeadlessChrome", cdp.sent[0][1]["userAgent"])


class TestClientHintCache(unittest.TestCase):
    """The client-hint measurement is cached across opens, keyed by Chrome version.

    It is the same answer every open - the brand and version lists belong to the Chrome
    binary - so the ~500ms probe should run once per version and be read from disk after.
    """

    MEASURED = {"brands": [{"brand": "Google Chrome", "version": "153"}],
                "fullVersionList": [{"brand": "Google Chrome", "version": "153.0.8010.37"}],
                "uaFullVersion": "153.0.8010.37", "platform": "macOS", "bitness": "64"}

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._file = os.path.join(self._dir, "client-hints.json")
        self._patches = [
            mock.patch.object(fp, "_hints_cache_file", return_value=self._file),
            mock.patch.object(fp, "chrome_version", return_value="153.0.8010.37"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self._patches:
            p.start()
        os.environ.pop("LATCHKEY_HINTS_CACHE", None)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def test_a_miss_measures_then_writes_the_cache(self):
        calls = []
        with mock.patch.object(fp, "measure", side_effect=lambda page: (calls.append(page)
                                                                        or self.MEASURED)):
            got = fp.measured_hints("PAGE")
        self.assertEqual(got, self.MEASURED)
        self.assertEqual(calls, ["PAGE"])                 # measured exactly once
        self.assertTrue(os.path.exists(self._file))
        stored = json.load(open(self._file))
        self.assertEqual(stored["3|darwin|153.0.8010.37"], self.MEASURED)

    def test_a_hit_returns_the_cache_without_measuring(self):
        json.dump({"3|darwin|153.0.8010.37": self.MEASURED}, open(self._file, "w"))
        with mock.patch.object(fp, "measure",
                               side_effect=AssertionError("must not measure on a hit")):
            got = fp.measured_hints("PAGE")
        self.assertEqual(got, self.MEASURED)

    def test_a_new_chrome_version_is_a_miss(self):
        json.dump({"3|darwin|153.0.8010.37": self.MEASURED}, open(self._file, "w"))
        fresh = dict(self.MEASURED, uaFullVersion="154.0.0.0")
        with mock.patch.object(fp, "chrome_version", return_value="154.0.0.0"), \
             mock.patch.object(fp, "measure", return_value=fresh) as m:
            got = fp.measured_hints("PAGE")
        m.assert_called_once()
        self.assertEqual(got, fresh)
        stored = json.load(open(self._file))
        self.assertIn("3|darwin|154.0.0.0", stored)       # both versions kept

    def test_an_empty_measurement_is_not_cached(self):
        with mock.patch.object(fp, "measure", return_value={}):
            got = fp.measured_hints("PAGE")
        self.assertEqual(got, {})
        self.assertFalse(os.path.exists(self._file))      # never pin the residual

    def test_the_env_switch_forces_a_live_measure_every_time(self):
        json.dump({"3|darwin|153.0.8010.37": self.MEASURED}, open(self._file, "w"))
        os.environ["LATCHKEY_HINTS_CACHE"] = "0"
        with mock.patch.object(fp, "measure", return_value=self.MEASURED) as m:
            fp.measured_hints("PAGE")
            fp.measured_hints("PAGE")
        self.assertEqual(m.call_count, 2)                 # cache ignored both ways

    def test_a_corrupt_cache_file_is_a_miss_not_a_crash(self):
        open(self._file, "w").write("{not json")
        with mock.patch.object(fp, "measure", return_value=self.MEASURED) as m:
            got = fp.measured_hints("PAGE")
        m.assert_called_once()
        self.assertEqual(got, self.MEASURED)
        self.assertEqual(json.load(open(self._file))["3|darwin|153.0.8010.37"],
                         self.MEASURED)                   # rewritten cleanly


if __name__ == "__main__":
    unittest.main()
