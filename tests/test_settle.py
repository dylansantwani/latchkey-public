"""Offline tests for the network-aware settle.

`settle` used to lean on Playwright's `networkidle` - zero connections for 500ms - which a
page with a video, a websocket or a long-poll channel never reaches, so every action against
a live app burned the whole ceiling for a page that was interactive at once. It now watches
the requests genuinely in flight: a long-lived transport is ignored by type, a held-open
channel is aged out, and a real subresource or XHR is waited on until it finishes. These
tests pin that logic without a browser, driving a fake page and a fake clock.

    cd ~/tools/latchkey
    python3 -m unittest tests.test_settle -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import driver as drv  # noqa: E402


class FakeReq:
    """Stands in for a Playwright Request: identity plus a resource type."""

    def __init__(self, resource_type="fetch"):
        self.resource_type = resource_type


class FakePage:
    """A page whose events can be fired by hand and whose clock the test controls."""

    def __init__(self, clock):
        self._clock = clock
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, arg=None):
        for handler in self.handlers.get(event, []):
            handler(arg)

    def wait_for_timeout(self, ms):
        # settle's only blocking call: advance the fake clock instead of sleeping.
        self._clock.advance(ms / 1000)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def advance(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


def make_driver():
    d = drv.Driver()
    d._clock = Clock()
    d._now = d._clock                     # settle and tracking read the same fake clock
    d._page = FakePage(d._clock)
    d.track_network(d._page)
    return d


class TestTracking(unittest.TestCase):
    def test_a_request_is_counted_then_forgotten_when_it_finishes(self):
        d = make_driver()
        page, key = d._page, id(d._page)
        req = FakeReq("fetch")
        page.fire("request", req)
        self.assertEqual(d._pending_requests(key, d._clock(), 1500), 1)
        page.fire("requestfinished", req)
        self.assertEqual(d._pending_requests(key, d._clock(), 1500), 0)

    def test_a_failed_request_is_also_forgotten(self):
        d = make_driver()
        page, key = d._page, id(d._page)
        req = FakeReq("document")
        page.fire("request", req)
        page.fire("requestfailed", req)
        self.assertEqual(d._pending_requests(key, d._clock(), 1500), 0)

    def test_long_lived_transports_never_count(self):
        d = make_driver()
        page, key = d._page, id(d._page)
        for rtype in ("websocket", "eventsource", "media", "ping"):
            page.fire("request", FakeReq(rtype))
        self.assertEqual(d._pending_requests(key, d._clock(), 1500), 0)

    def test_a_held_open_channel_is_aged_out(self):
        d = make_driver()
        page, key = d._page, id(d._page)
        page.fire("request", FakeReq("xhr"))
        now = d._clock()
        self.assertEqual(d._pending_requests(key, now, 1500), 1)          # fresh: waited on
        self.assertEqual(d._pending_requests(key, now + 2.0, 1500), 0)    # >1.5s: a channel

    def test_a_lost_end_event_is_pruned_so_the_set_cannot_grow(self):
        d = make_driver()
        page, key = d._page, id(d._page)
        page.fire("request", FakeReq("fetch"))
        d._pending_requests(key, d._clock() + 31, 1500)      # past the 30s prune horizon
        self.assertEqual(len(d._net_reqs[key]), 0)

    def test_reset_forgets_the_pages_requests(self):
        d = make_driver()
        page, key = d._page, id(d._page)
        page.fire("request", FakeReq("fetch"))
        d.reset_network()
        self.assertEqual(d._pending_requests(key, d._clock(), 1500), 0)

    def test_close_drops_the_pages_entry(self):
        d = make_driver()
        page, key = d._page, id(d._page)
        page.fire("close")
        self.assertNotIn(key, d._net_reqs)


class TestSettleTiming(unittest.TestCase):
    """Drive settle with the fake clock; wait_for_timeout advances it, no real waiting."""

    def settle(self, d, cap_ms):
        return d.settle(cap_ms)           # d._now is the fake clock; no real waiting

    def test_a_quiet_page_settles_after_the_quiet_window(self):
        d = make_driver()
        cost = self.settle(d, 5000)
        # nothing in flight: it returns after ~one quiet window, not the ceiling
        self.assertGreaterEqual(cost, drv.SETTLE_QUIET_MS)
        self.assertLess(cost, drv.SETTLE_QUIET_MS + 200)

    def test_a_page_that_never_drains_hits_the_ceiling(self):
        d = make_driver()
        d._page.fire("request", FakeReq("fetch"))
        # a real flood that never finishes and never ages out within the cap: whole ceiling
        cost = self.settle(d, 1000)
        self.assertGreaterEqual(cost, 1000)
        self.assertLess(cost, 1200)

    def test_the_quiet_window_restarts_when_a_request_arrives(self):
        d = make_driver()
        page = d._page
        # A request that is in flight partway through must reset the quiet window, so a page
        # that goes briefly quiet and then fetches again is waited out, not settled in the gap.
        original = page.wait_for_timeout
        req = FakeReq("fetch")
        state = {"opened": False, "closed": False}

        def wait(ms):
            original(ms)
            elapsed = d._clock() - 1000
            if not state["opened"] and elapsed >= 0.2:
                page.fire("request", req)           # a fetch starts 200ms in...
                state["opened"] = True
            elif state["opened"] and not state["closed"] and elapsed >= 0.4:
                page.fire("requestfinished", req)   # ...and finishes at 400ms
                state["closed"] = True

        page.wait_for_timeout = wait
        cost = self.settle(d, 5000)
        # in flight until 400ms, so the quiet window can only complete after that
        self.assertGreaterEqual(cost, 400 + drv.SETTLE_QUIET_MS)
        self.assertLess(cost, 400 + drv.SETTLE_QUIET_MS + 200)

    def test_env_can_shorten_the_quiet_window(self):
        d = make_driver()
        with mock.patch.dict(os.environ, {"LATCHKEY_SETTLE_QUIET_MS": "100"}):
            cost = self.settle(d, 5000)
        self.assertGreaterEqual(cost, 100)
        self.assertLess(cost, 300)

    def test_env_can_raise_the_inflight_tolerance(self):
        d = make_driver()
        d._page.fire("request", FakeReq("fetch"))    # one genuine request pending
        with mock.patch.dict(os.environ, {"LATCHKEY_SETTLE_INFLIGHT": "1"}):
            cost = self.settle(d, 5000)              # tolerated, so it settles anyway
        self.assertLess(cost, drv.SETTLE_QUIET_MS + 300)

    def test_a_zero_ceiling_returns_at_once(self):
        d = make_driver()
        self.assertEqual(self.settle(d, 0), 0.0)


if __name__ == "__main__":
    unittest.main()
