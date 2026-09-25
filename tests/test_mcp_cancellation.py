"""Offline tests for the two ways a client can stop work it no longer wants.

The server's lanes keep each session's calls in order, which is right up until the
client gives up: the call that timed out keeps running, and every call queued behind
it runs too, so a session stays unresponsive long after the agent stopped listening.
These check the three parts of not doing that:

  - a cancelled request is never started (it was queued, not running),
  - a running call is told it has been abandoned, so it can let go early,
  - a session can be reclaimed from a call that is waiting on a human.

No browser and no network: `wait_for_login`'s loop is exercised directly, and the
server is driven with a double for the browser.

    python3 -m unittest tests.test_mcp_cancellation -v
"""
import io
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import mcp_server  # noqa: E402
from latchkey.session import Browser  # noqa: E402


class ServerDouble(mcp_server.StdioServer):
    """A Server with no stdio: the replies it would have written, in a list."""

    def __init__(self) -> None:
        self.written: list = []
        super().__init__(stdin=io.StringIO(), stdout=io.StringIO())

    def send(self, response: dict) -> None:
        self.written.append(response)


def tool_request(name: str, request_id: int = 1, **arguments) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


class ACancelledCallIsNeverStarted(unittest.TestCase):

    def test_a_queued_call_the_client_abandoned_does_not_run(self):
        server = ServerDouble()
        request = tool_request("latchkey_pages")
        task = server._watched(request, "default")
        self.assertTrue(server.cancel({"params": {"requestId": 1}}),
                        "cancelling a request in flight must be acknowledged")
        task()                                   # the lane gets to it a moment too late
        self.assertEqual(server.written, [], "it should not have run at all")

    def test_a_cancelled_call_that_had_started_reports_that_it_stopped(self):
        server = ServerDouble()
        request = tool_request("latchkey_text")
        event = threading.Event()

        def handle_stub(_request):
            event.set()                          # the client gives up mid-call
            return {"jsonrpc": "2.0", "id": _request["id"], "result": {"content": []}}
        with mock.patch.object(mcp_server, "handle", handle_stub):
            server._serve(request, event)
        self.assertEqual(server.written[0]["error"]["code"], -32800)
        self.assertIn("cancelled", server.written[0]["error"]["message"])

    def test_cancelling_something_that_is_not_in_flight_is_harmless(self):
        server = ServerDouble()
        self.assertFalse(server.cancel({"params": {"requestId": 99}}))
        self.assertFalse(server.cancel({}))

    def test_a_call_that_is_not_cancelled_still_runs(self):
        server = ServerDouble()
        with mock.patch.object(mcp_server, "handle",
                               lambda request: {"jsonrpc": "2.0", "id": request["id"],
                                                "result": {"content": []}}):
            server._watched(tool_request("latchkey_pages"), "default")()
        self.assertEqual(len(server.written), 1)
        self.assertIn("result", server.written[0])


class ASessionCanBeReclaimedWhileItWaits(unittest.TestCase):

    def test_reclaiming_a_lane_cancels_what_is_in_flight_on_it(self):
        server = ServerDouble()
        mine = server._watched(tool_request("latchkey_wait_for_login", 1), "default")
        else_where = server._watched(tool_request("latchkey_text", 2), "other")
        self.assertEqual(server.reclaim("default"), 1)
        self.assertTrue(server._inflight[1][0].is_set())
        self.assertFalse(server._inflight[2][0].is_set(), "another session is not our business")
        self.assertTrue(callable(mine) and callable(else_where))

    def test_the_tools_that_reclaim_a_lane_are_the_ones_that_close_a_session(self):
        self.assertEqual(mcp_server.LANE_RECLAIMERS,
                         ("latchkey_session_close", "latchkey_forget_session"))


class TheWaitLetsGoWhenTheClientDoes(unittest.TestCase):
    """`wait_for_login` is the one call that can hold a session for minutes."""

    class State:
        verdict = "logged-out"

        def as_dict(self):
            return {"url": "https://example.com", "verdict": self.verdict}

        def summary(self):
            return {}

    def _browser(self):
        browser = Browser.__new__(Browser)       # no Chrome: just the loop under test
        browser.goto = lambda url, settle_ms=0: self.State()
        browser.cookie_signature = lambda: "unchanged"
        browser._publish = lambda *args, **kwargs: None
        browser.state = lambda: self.State()
        return browser

    def test_an_abandoned_wait_returns_at_once_instead_of_at_its_deadline(self):
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        out = self._browser().wait_for_login("https://example.com", timeout_s=300,
                                             poll_s=5.0, cancel=cancel)
        elapsed = time.monotonic() - started
        self.assertEqual(out["status"], "abandoned")
        self.assertLess(elapsed, 1.0,
                        "a wait nobody wants must not hold the session for its timeout")

    def test_a_wait_that_is_never_cancelled_still_times_out_as_before(self):
        out = self._browser().wait_for_login("https://example.com", timeout_s=1, poll_s=0.25)
        self.assertEqual(out["status"], "timeout")
        self.assertIn("hint", out)


if __name__ == "__main__":
    unittest.main()
