"""Offline tests for the arithmetic behind human-shaped input.

Pure functions, so they can be checked without a browser: where a pointer goes, how
long it waits, and the two promises that matter - the path *ends* where it was aimed
(a jittered click that lands 3px off is a miss), and nothing is constant (a fixed
inter-key delay is the easiest behavioural signal there is to measure).

    cd ~/tools/latchkey
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import human  # noqa: E402

BOX = {"x": 100.0, "y": 200.0, "width": 40.0, "height": 20.0}


class TestWaypoints(unittest.TestCase):
    def test_the_path_ends_exactly_on_the_target(self):
        rng = human.rand(1)
        points = human.waypoints(10, 10, 400, 250, rng)
        self.assertEqual(points[-1], (400, 250))

    def test_a_path_has_several_legs_and_bows_off_the_straight_line(self):
        rng = human.rand(2)
        points = human.waypoints(0, 0, 300, 0, rng)
        self.assertGreaterEqual(len(points), 3)
        # The straight line from (0,0) to (300,0) has y == 0 everywhere; a hand does not.
        self.assertTrue(any(abs(y) > 0.5 for _x, y in points[:-1]))

    def test_the_same_seed_gives_the_same_path(self):
        first = human.waypoints(5, 5, 200, 90, human.rand(7))
        second = human.waypoints(5, 5, 200, 90, human.rand(7))
        self.assertEqual(first, second)

    def test_different_seeds_do_not(self):
        first = human.waypoints(5, 5, 200, 90, human.rand(7))
        second = human.waypoints(5, 5, 200, 90, human.rand(8))
        self.assertNotEqual(first, second)

    def test_a_move_too_short_to_walk_is_a_single_point(self):
        self.assertEqual(human.waypoints(50, 50, 50.5, 50, human.rand(3)), [(50.5, 50)])

    def test_the_bow_stays_near_the_line(self):
        rng = human.rand(4)
        for _ in range(20):
            points = human.waypoints(0, 0, 500, 300, rng)
            for x, y in points:
                self.assertLess(abs(y - x * 0.6), 60)


class TestClickPoint(unittest.TestCase):
    def test_the_point_lands_inside_the_element_with_room_to_spare(self):
        rng = human.rand(11)
        for _ in range(50):
            x, y = human.click_point(BOX, rng)
            self.assertGreaterEqual(x, BOX["x"] + 2)
            self.assertLessEqual(x, BOX["x"] + BOX["width"] - 2)
            self.assertGreaterEqual(y, BOX["y"] + 2)
            self.assertLessEqual(y, BOX["y"] + BOX["height"] - 2)

    def test_it_is_not_always_the_centre(self):
        rng = human.rand(12)
        centre = (BOX["x"] + BOX["width"] / 2, BOX["y"] + BOX["height"] / 2)
        points = [human.click_point(BOX, rng) for _ in range(30)]
        self.assertTrue(any(abs(x - centre[0]) > 1 or abs(y - centre[1]) > 1
                            for x, y in points))

    def test_a_tiny_element_falls_back_to_its_centre(self):
        tiny = {"x": 10.0, "y": 10.0, "width": 3.0, "height": 3.0}
        self.assertEqual(human.click_point(tiny, human.rand(13)), (11.5, 11.5))


class TestTiming(unittest.TestCase):
    def test_typing_has_one_delay_per_character_and_none_of_them_are_equal(self):
        rng = human.rand(21)
        text = "hello world, this is a reasonably long line"
        delays = human.type_delays(text, rng)
        self.assertEqual(len(delays), len(text))
        self.assertTrue(all(d > 0 for d in delays))
        self.assertGreater(len(set(delays)), 5)

    def test_the_budget_sizes_the_typing_without_bounding_it_exactly(self):
        rng = human.rand(22)
        text = "x" * 40
        delays = human.type_delays(text, rng, budget_ms=1400)
        self.assertGreater(sum(delays), 400)
        self.assertLess(sum(delays), 4 * 1400 + 500)

    def test_an_empty_field_has_nothing_to_type(self):
        self.assertEqual(human.type_delays("", human.rand(23)), [])

    def test_type_timing_reports_a_usable_nominal_delay(self):
        nominal, delays = human.type_timing("hello", human.rand(24))
        self.assertGreater(nominal, 0)
        self.assertEqual(len(delays), 5)


class TestScrolling(unittest.TestCase):
    def test_chunks_add_up_exactly(self):
        for pixels in (1, 90, 500, 1234):
            chunks = human.scroll_chunks(pixels, human.rand(31))
            self.assertEqual(sum(chunks), pixels)

    def test_scrolling_up_adds_up_too(self):
        chunks = human.scroll_chunks(-500, human.rand(32))
        self.assertEqual(sum(chunks), -500)
        self.assertTrue(all(c < 0 for c in chunks[:-1]))

    def test_no_scroll_no_chunks(self):
        self.assertEqual(human.scroll_chunks(0, human.rand(33)), [])

    def test_a_long_scroll_arrives_in_several_notches(self):
        chunks = human.scroll_chunks(900, human.rand(34))
        self.assertGreater(len(chunks), 3)
        for chunk in chunks[:-1]:
            self.assertGreaterEqual(chunk, 90)
            self.assertLessEqual(chunk, 240)


class TestEnablement(unittest.TestCase):
    def test_on_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(human.enabled())
            self.assertTrue(human.enabled(None))

    def test_off_when_the_environment_says_so(self):
        with mock.patch.dict(os.environ, {human.ENV: "0"}, clear=True):
            self.assertFalse(human.enabled())
            self.assertTrue(human.enabled(True))     # the session wins over the floor

    def test_a_wordy_no_still_counts_as_no(self):
        for value in ("false", "no", "off", "FALSE"):
            with mock.patch.dict(os.environ, {human.ENV: value}, clear=True):
                self.assertFalse(human.enabled())


if __name__ == "__main__":
    unittest.main()
