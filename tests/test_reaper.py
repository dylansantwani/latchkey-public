"""Idle sessions are closed so their Chrome stops holding memory - but never a busy one.

The policy lives on the registry (which names are idle, and closing them) so it can be
tested without threads or a clock. A busy session is never idle whatever the clock says,
and a session in the middle of a human intervention is protected by name even when it looks
quiet, because the work holding it lives off the session's own thread.

    python3 -m unittest tests.test_reaper -v
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import reaper as reaper_mod  # noqa: E402
from latchkey.sessions import SessionRegistry  # noqa: E402


class FakeBrowser:
    def __init__(self, label=""):
        self.label = label
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


def a_registry():
    made = {}

    def factory(spec=None):
        browser = FakeBrowser(label=getattr(spec, "label", "") or "")
        made[id(browser)] = browser
        return browser

    return SessionRegistry(factory=factory)


class TTL(unittest.TestCase):
    def _with_env(self, value):
        real = os.environ.get("LATCHKEY_IDLE_TTL")
        if value is None:
            os.environ.pop("LATCHKEY_IDLE_TTL", None)
        else:
            os.environ["LATCHKEY_IDLE_TTL"] = value
        self.addCleanup(lambda: os.environ.__setitem__("LATCHKEY_IDLE_TTL", real)
                        if real is not None else os.environ.pop("LATCHKEY_IDLE_TTL", None))

    def test_default_when_unset(self):
        self._with_env(None)
        self.assertEqual(reaper_mod.ttl_s(), reaper_mod.DEFAULT_TTL_S)

    def test_off_disables(self):
        self._with_env("off")
        self.assertEqual(reaper_mod.ttl_s(), 0.0)

    def test_a_number_is_honoured(self):
        self._with_env("120")
        self.assertEqual(reaper_mod.ttl_s(), 120.0)


class ReapPolicy(unittest.TestCase):
    def setUp(self):
        self.registry = a_registry()
        self.addCleanup(self.registry.close_all)

    def _idle_session(self, name, idle_s):
        session = self.registry.get(name)
        session._used = time.time() - idle_s
        return session

    def test_an_idle_session_is_closed(self):
        session = self._idle_session("old", 1000)
        reaped = self.registry.reap_idle(ttl_s=60)
        self.assertEqual(reaped, ["old"])
        self.assertIsNone(self.registry.get_existing("old"))

    def test_a_fresh_session_is_left_alone(self):
        self._idle_session("fresh", 5)
        self.assertEqual(self.registry.reap_idle(ttl_s=60), [])
        self.assertIsNotNone(self.registry.get_existing("fresh"))

    def test_a_busy_session_is_never_idle(self):
        session = self._idle_session("busy", 1000)
        session._inflight = 1                       # a call is running right now
        self.assertEqual(self.registry.reap_idle(ttl_s=60), [])
        self.assertIsNotNone(self.registry.get_existing("busy"))

    def test_a_protected_session_is_skipped(self):
        self._idle_session("mid-assist", 1000)
        self.assertEqual(self.registry.reap_idle(ttl_s=60, protected={"mid-assist"}), [])
        self.assertIsNotNone(self.registry.get_existing("mid-assist"))

    def test_ttl_of_zero_reaps_nothing(self):
        self._idle_session("old", 1000)
        self.assertEqual(self.registry.reap_idle(ttl_s=0), [])

    def test_a_call_left_in_the_queue_at_close_is_failed_not_orphaned(self):
        """A submit that raced the reaper's close must get a clear error, not hang until its
        own timeout waiting for a worker that has gone."""
        session = self.registry.get("racing")
        self.assertTrue(session.close())          # worker exits; no consumer left
        box = {"done": threading.Event(), "result": None, "error": None}
        session._queue.put((lambda browser: None, box))
        session._drain_pending()
        self.assertTrue(box["done"].is_set())
        self.assertIsNotNone(box["error"])
        self.assertIn("closed", str(box["error"]))


class FakeManager:
    def __init__(self, protected=None):
        self._protected = set(protected or ())

    def protected_sessions(self):
        return set(self._protected)


class ReaperThread(unittest.TestCase):
    def setUp(self):
        self.registry = a_registry()
        self.addCleanup(self.registry.close_all)

    def test_tick_reaps_idle_and_reports(self):
        session = self.registry.get("old")
        session._used = time.time() - 1000
        reaper = reaper_mod.IdleReaper(self.registry, FakeManager(), ttl=lambda: 60,
                                       interval_s=0.01)
        result = reaper.tick()
        self.assertEqual(result.get("reaped"), ["old"])

    def test_tick_respects_the_managers_protected_set(self):
        session = self.registry.get("mid-assist")
        session._used = time.time() - 1000
        reaper = reaper_mod.IdleReaper(self.registry, FakeManager({"mid-assist"}),
                                       ttl=lambda: 60, interval_s=0.01)
        self.assertEqual(reaper.tick().get("reaped"), None)
        self.assertIsNotNone(self.registry.get_existing("mid-assist"))

    def test_tick_sweeps_orphan_profiles(self):
        real = reaper_mod.intervene.sweep_orphans
        reaper_mod.intervene.sweep_orphans = lambda: 3
        try:
            reaper = reaper_mod.IdleReaper(self.registry, FakeManager(), ttl=lambda: 0)
            self.assertEqual(reaper.tick().get("swept_profiles"), 3)
        finally:
            reaper_mod.intervene.sweep_orphans = real

    def test_interval_is_bounded_by_the_ttl(self):
        self.assertEqual(reaper_mod.IdleReaper(self.registry, FakeManager(),
                                               ttl=lambda: 900).interval(), 120.0)
        self.assertEqual(reaper_mod.IdleReaper(self.registry, FakeManager(),
                                               ttl=lambda: 40).interval(), 15.0)


if __name__ == "__main__":
    unittest.main()
