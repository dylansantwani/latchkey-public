"""One wedged session must cost one session, and a browser must always have an owner.

Two failures with the same shape. The registry held its lock across `close()` and
`Session(...)` - seconds to half a minute of real work - so a session stuck on a page
blocked `require()` for every *other* session, which is a registry-wide stall caused by
one name. And a session that blew its startup timeout raised, leaving its worker thread
still launching a Chrome that nothing then held a reference to.

    python3 -m unittest tests.test_registry_lifecycle -v
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey.sessions import Session, SessionError, SessionRegistry  # noqa: E402


class FakeBrowser:
    def __init__(self, label="", closing=0.0):
        self.label = label
        self.closing = closing
        self.closed = threading.Event()

    def close(self):
        time.sleep(self.closing)
        self.closed.set()


class ClosingOneSessionDoesNotStallTheOthers(unittest.TestCase):
    def test_a_slow_close_on_one_name_does_not_hold_up_another(self):
        made = {}

        def factory(spec=None):
            browser = FakeBrowser(closing=0.6)
            made.setdefault(spec, []).append(browser)
            return browser

        registry = SessionRegistry(factory)
        registry.get("slow", spec="slow")
        other = registry.get("quick", spec="quick")

        done = threading.Event()

        def recreate():
            registry.get("slow", spec="slow", recreate=True)
            done.set()

        threading.Thread(target=recreate, daemon=True).start()
        time.sleep(0.1)                      # the close is now in flight
        started = time.perf_counter()
        self.assertIs(registry.get("quick"), other)
        self.assertEqual(registry.names().count("quick"), 1)
        spent = time.perf_counter() - started
        self.assertLess(spent, 0.25, f"the other session waited {spent * 1000:.0f} ms")
        self.assertTrue(done.wait(5), "and the recreate still finished")
        registry.close_all()

    def test_two_callers_racing_for_one_name_get_one_browser(self):
        made = []

        def factory(spec=None):
            time.sleep(0.2)
            browser = FakeBrowser()
            made.append(browser)
            return browser

        registry = SessionRegistry(factory)
        got = []
        threads = [threading.Thread(target=lambda: got.append(registry.get("one")))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(made), 1, "the name is serialised against itself")
        self.assertEqual(len({id(session) for session in got}), 1)
        registry.close_all()


class AStartupNobodyWaitedForIsNotLeftRunning(unittest.TestCase):
    def test_a_browser_that_finishes_starting_too_late_is_closed(self):
        browser = FakeBrowser()
        release = threading.Event()

        def factory():
            release.wait(5)
            return browser

        with self.assertRaises(SessionError) as caught:
            Session("late", factory, startup_timeout=0.2)
        self.assertIn("did not finish starting", str(caught.exception))
        self.assertIn("will be closed", str(caught.exception))
        self.assertFalse(browser.closed.is_set(), "it has not even started yet")
        release.set()
        self.assertTrue(browser.closed.wait(5),
                        "the browser nobody is holding is closed rather than orphaned")

    def test_a_factory_that_fails_says_why(self):
        def factory():
            raise RuntimeError("no Chrome at that path")

        with self.assertRaises(SessionError) as caught:
            Session("broken", factory, startup_timeout=5)
        self.assertIn("no Chrome at that path", str(caught.exception))


class AWedgedSessionSaysSoRatherThanJustTimingOut(unittest.TestCase):
    def test_the_second_timeout_says_how_many_there_have_been(self):
        block = threading.Event()
        session = Session("stuck", lambda: FakeBrowser(), startup_timeout=5)
        try:
            for expected in (None, "2 calls have timed out"):
                with self.assertRaises(SessionError) as caught:
                    session.submit(lambda _b: block.wait(10), timeout=0.15)
                if expected:
                    self.assertIn(expected, str(caught.exception))
                self.assertIn("queues behind it", str(caught.exception))
            self.assertEqual(session.describe()["timed_out_calls"], 2)
        finally:
            block.set()
            session.close(timeout=2)


if __name__ == "__main__":
    unittest.main()
