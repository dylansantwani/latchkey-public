"""The assist tool over the MCP surface: ask first, then act, without a browser.

A fake session stands in for Chrome. What these pin down is the promise the feature is
built on - a window carrying the user's cookies is never opened until the user has said yes
- plus the confirm path handing the wait back when the human is still working, and the
earned cookies being carried into the session once the window is done.

    python3 -m unittest tests.test_assist_surface -v
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import intervene, mcp_server  # noqa: E402
from latchkey.detect import PageState  # noqa: E402
from latchkey.sessions import SessionRegistry  # noqa: E402

SITE = "https://shop.example.com/checkout"


class FakeAgent:
    def __init__(self, label="", verdict="challenged"):
        self.label = label
        self.verdict = verdict
        self.url = SITE
        self.applied = None

    def assist_snapshot(self, url=None):
        return {"url": self.url, "verdict": self.verdict,
                "wall": {"vendor": "cloudflare", "label": "Cloudflare", "kind": "challenge",
                         "sentence": "Cloudflare challenge"},
                "title": "Just a moment...",
                "cookies": [{"name": "sess", "value": "1", "domain": ".shop.example.com",
                             "path": "/"}],
                "user_agent": "UA/1", "width": 1400, "height": 900}

    def assist_apply(self, cookies, url=None, settle_ms=3000):
        self.applied = list(cookies)
        return {"accepted": len(cookies), "offered": len(cookies),
                "state": PageState(url=self.url, title="Checkout", text="signed in",
                                   logged_in_marker="avatar").as_dict()}

    def state(self, text_limit=4000):
        return PageState(url=self.url, title="Checkout", text="ok")

    def close(self):
        pass


def registry_of(agent):
    return SessionRegistry(factory=lambda spec=None: agent)


class NeverWindow:
    pid = 999

    def seed(self, cookies):
        return len(cookies)

    def open_url(self, url):
        pass

    def alive(self):
        return True

    def cookies(self):
        return []

    def solved(self):
        return False

    def close(self):
        self.closed = True


def call(tool, **args):
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": tool, "arguments": args}})
    text = resp["result"]["content"][0]["text"]
    try:
        return resp["result"]["isError"], json.loads(text)
    except json.JSONDecodeError:
        return resp["result"]["isError"], text


class Base(unittest.TestCase):
    def setUp(self):
        self.agent = FakeAgent()
        self._real_registry = mcp_server.registry
        self._real_manager = intervene.manager
        mcp_server.registry = registry_of(self.agent)
        intervene.manager = intervene.InterventionManager()

    def tearDown(self):
        mcp_server.registry.close_all()
        intervene.manager.close_all()
        mcp_server.registry = self._real_registry
        intervene.manager = self._real_manager


class ConsentComesFirst(Base):
    def test_no_confirm_asks_the_user_and_opens_nothing(self):
        failed, out = call("latchkey_assist")
        self.assertFalse(failed, out)
        self.assertEqual(out["status"], "consent-required")
        self.assertIn("ask_user", out)
        self.assertIn("shop.example.com", out["ask_user"])
        # A pending intervention was recorded, but no window was opened.
        handoff = intervene.manager.get("default")
        self.assertEqual(handoff.state, intervene.PENDING)
        self.assertIsNone(handoff.window_pid)

    def test_off_by_environment_refuses(self):
        os.environ["LATCHKEY_ASSIST"] = "off"
        try:
            failed, out = call("latchkey_assist", confirm=True)
            self.assertEqual(out["status"], "disabled")
        finally:
            os.environ.pop("LATCHKEY_ASSIST", None)


class ConfirmOpensAndCanBeHandedBack(Base):
    def test_confirm_opens_a_window_and_hands_the_wait_back_while_the_human_works(self):
        real_open, real_wait = intervene.open_window, mcp_server.ASSIST_WAIT_S
        intervene.open_window = lambda *a, **k: NeverWindow()
        mcp_server.ASSIST_WAIT_S = 0.3
        try:
            failed, out = call("latchkey_assist", confirm=True)
            self.assertFalse(failed, out)
            self.assertEqual(out["status"], "waiting")
            self.assertEqual(out["assist"]["state"], intervene.OPEN)
            # And it can be cancelled, which closes the window.
            failed, out = call("latchkey_assist", cancel=True)
            self.assertEqual(out["status"], "cancelled")
            deadline = time.time() + 3
            while intervene.manager.get("default").running and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(intervene.manager.get("default").running)
        finally:
            intervene.open_window = real_open
            mcp_server.ASSIST_WAIT_S = real_wait


class FinishCarriesCookiesBack(Base):
    def test_a_solved_window_applies_its_earned_cookies_to_the_session(self):
        handoff = intervene.Intervention(id="iv9", session="default", url=SITE,
                                         reason="challenge", state=intervene.SOLVED)
        handoff.harvested = [{"name": "cf_clearance", "value": "PASS",
                              "domain": ".shop.example.com", "path": "/"}]
        out = mcp_server._assist_finish(handoff, "default")
        self.assertEqual(out["verdict"], "logged-in")
        self.assertEqual([c["name"] for c in self.agent.applied], ["cf_clearance"])
        self.assertTrue(handoff.applied)

    def test_a_failed_window_does_not_touch_the_session(self):
        handoff = intervene.Intervention(id="iv8", session="default", url=SITE,
                                         state=intervene.FAILED, error="timed out")
        out = mcp_server._assist_finish(handoff, "default")
        self.assertEqual(out["status"], intervene.FAILED)
        self.assertIn("timed out", out["hint"])
        self.assertIsNone(self.agent.applied)


class StatusIsReadOnly(Base):
    def test_status_lists_what_is_known(self):
        call("latchkey_assist")            # a pending consent
        failed, out = call("latchkey_assist_status")
        self.assertFalse(failed, out)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["interventions"][0]["site"], "shop.example.com")


if __name__ == "__main__":
    unittest.main()
