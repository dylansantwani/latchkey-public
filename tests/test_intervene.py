"""The human-intervention handoff, driven without a browser.

A fake window stands in for the headed Chrome, so the state machine that opens it, seeds
it, waits for the human, harvests what they earned and closes it can be tested end to end -
including the three ways it ends (the wall clears, the human closes the window, the clock
runs out) and the one rule that makes it safe: a browser carrying the user's cookies is
never opened without a consented `start`, and only the site's cookies go into it.

    python3 -m unittest tests.test_intervene -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import intervene  # noqa: E402


SITE = "https://shop.example.com/checkout"


def cookie(name, value, domain, path="/"):
    return {"name": name, "value": value, "domain": domain, "path": path}


class FakeWindow:
    """A scripted headed window: solves after `solve_after` polls, or is closed by hand.

    `cookie_timeline` is the cookie list it reports at each poll (last value repeats), so a
    test can make a clearance cookie appear at the moment the wall clears.
    """

    def __init__(self, *, solve_after=None, close_after=None, cookie_timeline=None,
                 fail_on_open=False):
        self.pid = 4321
        self.solve_after = solve_after
        self.close_after = close_after
        self.cookie_timeline = cookie_timeline or []
        self.fail_on_open = fail_on_open
        self.seeded = None
        self.opened = None
        self.closed = False
        self._polls = 0

    def seed(self, cookies):
        self.seeded = list(cookies)
        return len(cookies)

    def open_url(self, url):
        if self.fail_on_open:
            raise RuntimeError("window would not open")
        self.opened = url

    def alive(self):
        if self.close_after is not None and self._polls >= self.close_after:
            return False
        return not self.closed

    def cookies(self):
        if not self.cookie_timeline:
            return []
        idx = min(self._polls, len(self.cookie_timeline) - 1)
        return list(self.cookie_timeline[idx])

    def solved(self):
        self._polls += 1
        if self.solve_after is None:
            return False
        return self._polls > self.solve_after

    def close(self):
        self.closed = True


def opener_for(window):
    def _open(profile_dir, url, *, user_agent, width, height):
        window.profile_dir = profile_dir
        window.ua = user_agent
        window.size = (width, height)
        return window
    return _open


def snapshot(cookies, url=SITE, verdict="challenged", wall=None):
    return {"url": url, "verdict": verdict, "wall": wall or {"vendor": "cloudflare",
                                                             "label": "Cloudflare",
                                                             "kind": "challenge",
                                                             "sentence": "Cloudflare challenge"},
            "cookies": cookies, "user_agent": "UA/1", "width": 1400, "height": 900}


class Scoping(unittest.TestCase):
    def test_only_the_sites_own_cookies_are_carried(self):
        cookies = [
            cookie("sess", "a", ".shop.example.com"),        # the exact host: kept
            cookie("root", "b", ".example.com"),             # parent domain: sent here, kept
            cookie("other", "c", ".unrelated.org"),          # a different site: dropped
            cookie("cf_clearance", "d", ".cdn-vendor.net"),  # a vendor's own domain: dropped
        ]
        kept = {c["name"] for c in intervene.scope_cookies(cookies, SITE)}
        self.assertEqual(kept, {"sess", "root"})

    def test_a_public_suffix_neighbour_is_not_treated_as_the_same_site(self):
        # A wall on foo.github.io must not pull in bar.github.io's cookies just because they
        # share the github.io public suffix. Matching each cookie by its own domain (never a
        # guessed registrable parent) means a neighbour's host simply does not match, and a
        # cookie on the bare suffix .github.io cannot occur (Chrome refuses to set one).
        cookies = [cookie("mine", "1", "foo.github.io"),
                   cookie("theirs", "2", "bar.github.io")]
        kept = {c["name"] for c in intervene.scope_cookies(cookies, "https://foo.github.io/x")}
        self.assertEqual(kept, {"mine"})

    def test_earned_is_only_what_the_window_gained_or_changed_for_the_site(self):
        seeded = [cookie("sess", "1", ".shop.example.com")]
        final = [
            cookie("sess", "1", ".shop.example.com"),          # unchanged: not earned
            cookie("cf_clearance", "new", ".shop.example.com"),  # new: earned
            cookie("noise", "x", ".unrelated.org"),            # off-site: never
        ]
        earned = intervene.InterventionManager._earned(seeded, final, SITE)
        self.assertEqual([c["name"] for c in earned], ["cf_clearance"])


class TheAsk(unittest.TestCase):
    def setUp(self):
        self.mgr = intervene.InterventionManager()

    def test_request_records_a_pending_intervention_and_opens_nothing(self):
        handoff = self.mgr.request("default", SITE, verdict="challenged",
                                   wall={"vendor": "cloudflare", "label": "Cloudflare",
                                         "kind": "challenge"})
        self.assertEqual(handoff.state, intervene.PENDING)
        self.assertEqual(handoff.reason, "challenge")
        self.assertTrue(handoff.running)
        self.assertIsNone(handoff.window_pid)

    def test_a_second_ask_while_one_is_pending_returns_the_same_one(self):
        first = self.mgr.request("default", SITE, verdict="challenged")
        second = self.mgr.request("default", SITE, verdict="challenged")
        self.assertIs(first, second)

    def test_reason_follows_the_verdict(self):
        self.assertEqual(intervene.reason_for("blocked", None), "block")
        self.assertEqual(intervene.reason_for("logged-out", None), "login")
        self.assertEqual(intervene.reason_for("challenged", None), "challenge")


class TheWindow(unittest.TestCase):
    def setUp(self):
        self.mgr = intervene.InterventionManager()

    def drive(self, window, snap, **kw):
        handoff = self.mgr.start("default", snapshot=snap, opener=opener_for(window),
                                 sleep=lambda _s: None, clock=self._clock(), poll_s=0, **kw)
        self.assertTrue(handoff.wait(5.0), "the window thread did not finish")
        return handoff

    def _clock(self):
        # A clock that advances a tick each call, so a timeout is deterministic and instant.
        state = {"t": 0.0}

        def clock():
            state["t"] += 0.05
            return state["t"]
        return clock

    def test_a_solved_wall_carries_the_earned_clearance_back(self):
        after = [cookie("sess", "1", ".shop.example.com"),
                 cookie("cf_clearance", "PASS", ".shop.example.com")]
        window = FakeWindow(solve_after=1, cookie_timeline=[[], after, after])
        handoff = self.drive(window, snapshot([cookie("sess", "1", ".shop.example.com")]))
        self.assertEqual(handoff.state, intervene.SOLVED)
        self.assertEqual([c["name"] for c in handoff.harvested], ["cf_clearance"])
        self.assertTrue(window.closed)
        self.assertEqual(window.ua, "UA/1")
        self.assertEqual(window.size, (1400, 900))
        # Only the site's cookies were seeded into the window.
        self.assertEqual({c["name"] for c in window.seeded}, {"sess"})

    def test_the_human_closing_the_window_counts_as_done(self):
        earned = [cookie("cf_clearance", "PASS", ".shop.example.com")]
        window = FakeWindow(close_after=2, solve_after=None,
                            cookie_timeline=[[], earned, earned])
        handoff = self.drive(window, snapshot([]))
        self.assertEqual(handoff.state, intervene.SOLVED)
        self.assertEqual([c["name"] for c in handoff.harvested], ["cf_clearance"])
        self.assertTrue(window.closed)

    def test_a_window_that_never_clears_times_out_and_closes(self):
        window = FakeWindow(solve_after=None, cookie_timeline=[[]])
        handoff = self.drive(window, snapshot([]), timeout_s=0.2)
        self.assertEqual(handoff.state, intervene.FAILED)
        self.assertTrue(window.closed)
        self.assertIn("time limit", handoff.error)

    def test_cancel_stops_a_running_window(self):
        started = threading.Event()

        class Blocking(FakeWindow):
            def solved(self_inner):
                started.set()
                return super().solved()

        window = Blocking(solve_after=None, cookie_timeline=[[]])
        handoff = self.mgr.start("default", snapshot=snapshot([]),
                                 opener=opener_for(window), sleep=lambda _s: time.sleep(0.01),
                                 poll_s=0.01, timeout_s=30)
        self.assertTrue(started.wait(2.0))
        self.assertTrue(self.mgr.cancel("default"))
        self.assertTrue(handoff.wait(3.0))
        self.assertEqual(handoff.state, intervene.CANCELLED)
        self.assertTrue(window.closed)

    def test_a_window_that_will_not_open_is_a_failure_not_a_crash(self):
        window = FakeWindow(fail_on_open=True)
        handoff = self.drive(window, snapshot([]))
        self.assertEqual(handoff.state, intervene.FAILED)
        self.assertTrue(handoff.error)

    def test_one_window_per_session_at_a_time(self):
        window = FakeWindow(solve_after=None, cookie_timeline=[[]])
        first = self.mgr.start("default", snapshot=snapshot([]),
                               opener=opener_for(window), sleep=lambda _s: time.sleep(0.01),
                               poll_s=0.01, timeout_s=30)
        second = self.mgr.start("default", snapshot=snapshot([]),
                                opener=opener_for(FakeWindow()), poll_s=0.01, timeout_s=30)
        self.assertIs(first, second)         # the open one is reused; no second window
        self.mgr.cancel("default")
        first.wait(3.0)

    def test_a_live_intervention_is_protected_from_reaping(self):
        window = FakeWindow(solve_after=None, cookie_timeline=[[]])
        handoff = self.mgr.start("canvas", snapshot=snapshot([]),
                                 opener=opener_for(window), sleep=lambda _s: time.sleep(0.01),
                                 poll_s=0.01, timeout_s=30)
        self.assertIn("canvas", self.mgr.protected_sessions())
        self.mgr.cancel("canvas")
        handoff.wait(3.0)
        self.assertNotIn("canvas", self.mgr.protected_sessions())


class Orphans(unittest.TestCase):
    def _root(self, name="iv1"):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        root = os.path.join(tmp, "handoff")
        os.makedirs(os.path.join(root, name))
        open(os.path.join(root, name, "Cookies"), "w").close()
        return root

    def _patch(self, **attrs):
        for name, value in attrs.items():
            real = getattr(intervene.chrome_mod, name, None) if name != "HANDOFF_ROOT" \
                else intervene.HANDOFF_ROOT
            target = intervene if name == "HANDOFF_ROOT" else intervene.chrome_mod
            setattr(target, name, value)
            self.addCleanup(lambda t=target, n=name, r=real: setattr(t, n, r))

    def test_sweep_removes_a_profile_no_chrome_holds(self):
        root = self._root()
        self._patch(HANDOFF_ROOT=root, lock_owner=lambda _path: None)
        self.assertEqual(intervene.sweep_orphans(), 1)
        self.assertFalse(os.path.isdir(os.path.join(root, "iv1")))

    def test_sweep_kills_an_orphaned_window_and_reclaims_its_profile(self):
        root = self._root()
        state = {"owner": 5150}
        self._patch(
            HANDOFF_ROOT=root,
            lock_owner=lambda _path: state["owner"],
            process_info=lambda pid: (1, f"Google Chrome --user-data-dir="
                                         f"{os.path.join(root, 'iv1')} --remote-debugging-port=0"),
            terminate=lambda pid, *_a, **_k: state.__setitem__("owner", None) or True)
        self.assertEqual(intervene.sweep_orphans(), 1)
        self.assertIsNone(state["owner"])          # the orphan was asked to quit
        self.assertFalse(os.path.isdir(os.path.join(root, "iv1")))

    def test_sweep_leaves_a_window_a_live_server_still_owns(self):
        root = self._root()
        self._patch(
            HANDOFF_ROOT=root,
            lock_owner=lambda _path: 5151,
            process_info=lambda pid: (4321, "Google Chrome --user-data-dir=/somewhere/iv1"),
            terminate=lambda *a, **k: self.fail("must not kill a live server's window"))
        self.assertEqual(intervene.sweep_orphans(), 0)
        self.assertTrue(os.path.isdir(os.path.join(root, "iv1")))


class Toggle(unittest.TestCase):
    def test_off_by_environment(self):
        real = os.environ.get("LATCHKEY_ASSIST")
        try:
            os.environ["LATCHKEY_ASSIST"] = "off"
            self.assertFalse(intervene.enabled())
            os.environ["LATCHKEY_ASSIST"] = "1"
            self.assertTrue(intervene.enabled())
            del os.environ["LATCHKEY_ASSIST"]
            self.assertTrue(intervene.enabled())     # on by default
        finally:
            if real is None:
                os.environ.pop("LATCHKEY_ASSIST", None)
            else:
                os.environ["LATCHKEY_ASSIST"] = real


if __name__ == "__main__":
    unittest.main()
