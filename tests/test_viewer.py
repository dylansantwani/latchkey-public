"""Offline tests for the viewer server: SSE, frames, and never bothering the agent.

The browser is fake here, so what these check is the shape of the contract the
Electron app and the web page both depend on: the handshake, the events, the frame
counter, and the promise that a viewer attaching or leaving cannot fail an agent.

    python3 -m unittest discover -s tests -v
"""
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import mcp_server, viewer  # noqa: E402
from latchkey.detect import PageState  # noqa: E402
from latchkey.events import bus  # noqa: E402
from latchkey.sessions import SessionRegistry  # noqa: E402
from latchkey.viewer import FrameStore, SessionView, ViewerServer  # noqa: E402

def free_port() -> int:
    """Ask the OS for a port nothing is using, so tests never collide with each other
    or with a viewer the user happens to be running."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeAgent:
    """A Browser that answers the few things the viewer asks of it."""

    def __init__(self, label="", frames=b"\xff\xd8\xffJPEG", cursor_ok=True):
        self.label = label
        self.show_cursor = False
        self.cursor_calls = []
        self.screencast = []
        self.closed = False
        self._frames = frames
        self.cursor_ok = cursor_ok
        self.url = "https://example.com/"
        self.pages = [self]
        self.page = self
        self._cdp = self

    # what the viewer calls ------------------------------------------------
    def attach_cursor(self):
        if not self.cursor_ok:
            raise RuntimeError("page went away")
        self.cursor_calls.append("attach")
        self.show_cursor = True
        return True

    def detach_cursor(self):
        self.cursor_calls.append("detach")
        self.show_cursor = False
        return True

    def cdp_for(self, page=None):
        return self

    def send(self, method, params=None):
        self.screencast.append(method)
        return {}

    def on(self, event, handler):
        self.handler = handler

    def screenshot(self, path=None, type=None, quality=None):
        return self._frames

    def report(self):
        return {}

    def close(self):
        self.closed = True


def registry_with_agent(agent, name="canvas"):
    registry = SessionRegistry(factory=lambda spec=None: agent)
    registry.get(name, spec=None)
    return registry


class TestFrameStore(unittest.TestCase):
    def test_a_new_frame_bumps_the_counter(self):
        store = FrameStore()
        self.assertEqual(store.counter, 0)
        self.assertEqual(store.put(b"one"), 1)
        self.assertEqual(store.put(b"two"), 2)
        body, counter = store.get()
        self.assertEqual((body, counter), (b"two", 2))

    def test_only_the_newest_frame_is_kept(self):
        """A viewer that falls behind should skip ahead, not replay old news."""
        store = FrameStore()
        for n in range(50):
            store.put(str(n).encode())
        self.assertEqual(store.get()[0], b"49")


# The viewer's timings are wall-clock waits for a browser that is not here: a stop that
# is never confirmed, a poll that cannot get a turn, an ack that never lands. Left at
# their real values they made this module 44 of the suite's 60 seconds, which is how a
# suite stops being run. They are module constants read at call time, so shortening them
# for the module changes what the tests wait for and nothing about what they test.
REAL_TIMINGS: dict = {}
SHORT_TIMINGS = {"POLL_TIMEOUT_S": 0.2, "POLL_BACKOFF_S": 0.3, "ACK_GIVE_UP_S": 0.3,
                 "STOP_ATTEMPT_S": 0.1, "STOP_DEADLINE_S": 0.4, "VIEW_TIMEOUT_S": 0.5,
                 "ADOPT_GRACE_S": 0.6, "HEARTBEAT_S": 1.0}


def setUpModule():
    for name, value in SHORT_TIMINGS.items():
        REAL_TIMINGS[name] = getattr(viewer, name)
        setattr(viewer, name, value)


def tearDownModule():
    for name, value in REAL_TIMINGS.items():
        setattr(viewer, name, value)


class ViewerCase(unittest.TestCase):
    """A viewer server on its own port, torn down after each test."""

    def setUp(self):
        self.registry = None
        self._real_registry = viewer.registry
        # Every viewer writes itself into ~/.latchkey/viewers.json as it starts, which
        # is the point of that file - but a test server has no business in the user's
        # copy of it, so the tests get their own.
        self._real_viewers_file = viewer.VIEWERS_FILE
        viewer.VIEWERS_FILE = os.path.join(tempfile.mkdtemp(), "viewers.json")
        self.servers = []
        bus.clear()          # each test's viewer should see its own stream, not the last
                                # test's events replayed as "history"

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
        viewer.registry = self._real_registry
        viewer.VIEWERS_FILE = self._real_viewers_file

    def start_viewer(self, agent=None, name="canvas"):
        if agent is not None:
            self.registry = registry_with_agent(agent, name)
            viewer.registry = self.registry
        server = ViewerServer(port=free_port()).start()
        self.port = server.port
        self.servers.append(server)
        time.sleep(0.05)
        return server

    def get(self, path, timeout=5.0):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def post(self, path, timeout=10.0):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        connection.request("POST", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def events(self, server, seconds=1.5, quiet=0.25, until=None):
        """Read the SSE stream raw until it goes quiet, and return the messages parsed.

        Raw sockets rather than http.client: the stream is endless, so a buffered
        readline would sit there until the next heartbeat instead of returning when
        the test's window closes.

        `seconds` is the ceiling. What normally ends the read is `quiet`: everything the
        server had to say arrives in one burst, so waiting out the rest of the window
        after it has stopped talking is time spent proving nothing. A test asserting that
        something did *not* arrive is unaffected - it would have come with the burst.

        A test that makes something happen *after* connecting passes `until`, since for
        that one the first burst is not the end of the story.
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=0.05)
        sock.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Accept: text/event-stream\r\n\r\n")
        messages, buffer = [], b""
        deadline = time.time() + seconds
        last = time.time()
        try:
            while time.time() < deadline:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    if until is None and messages and time.time() - last > quiet:
                        break
                    continue
                if not chunk:
                    break
                last = time.time()
                buffer += chunk
                while b"\n\n" in buffer:
                    block, buffer = buffer.split(b"\n\n", 1)
                    for line in block.splitlines():
                        if line.startswith(b"data: "):
                            messages.append(json.loads(line[6:]))
                if until is not None and until(messages):
                    break
        finally:
            sock.close()
        return messages


class TestEndpoints(ViewerCase):
    def test_health_and_sessions_answer_without_a_browser(self):
        self.start_viewer()
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, body = self.get("/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sessions"], [])

    def test_the_page_is_served_without_any_dependency(self):
        self.start_viewer()
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"EventSource", body)
        self.assertNotIn(b"http://cdn", body)

    def test_an_unknown_path_is_a_clean_404(self):
        self.start_viewer()
        status, body = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body)["error"])

    def test_an_unknown_session_frame_is_a_404_not_a_crash(self):
        self.start_viewer()
        status, _ = self.get("/frame/typo")
        self.assertEqual(status, 404)
        self.assertEqual(self.get("/health")[0], 200)

    def test_a_session_without_a_frame_yet_answers_204(self):
        self.start_viewer(FakeAgent())
        status, body = self.get("/frame/canvas")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")


class TestOnlyLoopbackMayAsk(ViewerCase):
    """DNS rebinding, which is what sending no CORS header does *not* stop.

    A page on `evil.example` whose name resolves to 127.0.0.1 is same-origin with this
    server as far as the browser is concerned, so the same-origin policy that was doing
    the work is gone and the page can read /events - the user's URLs, titles, typed
    values and screen frames. The header it cannot forge is Host.
    """

    def request(self, method, path, host):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_a_request_addressed_to_a_domain_name_is_refused(self):
        self.start_viewer()
        for path in ("/", "/health", "/sessions", "/events", "/frame/canvas"):
            status, body = self.request("GET", path, "evil.example:%d" % self.port)
            self.assertEqual(status, 403, path)
            self.assertIn(b"loopback only", body)

    def test_closing_a_session_from_a_foreign_name_is_refused_too(self):
        self.start_viewer(FakeAgent())
        status, _ = self.request("POST", "/session/canvas/close", "evil.example")
        self.assertEqual(status, 403)
        self.assertIn("canvas", self.servers[0].session_names(),
                      "and the session is still running")

    def test_the_names_loopback_actually_answers_to_all_work(self):
        self.start_viewer()
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port,
                     "[::1]:%d" % self.port, "LocalHost"):
            status, _ = self.request("GET", "/health", host)
            self.assertEqual(status, 200, host)


class TestStream(ViewerCase):
    def test_a_viewer_gets_the_handshake_the_sessions_and_the_history(self):
        server = self.start_viewer(FakeAgent())
        bus.publish("canvas", "click", selector="#go", x=10.0, y=20.0, verdict="logged-in")
        messages = self.events(server, seconds=1.0)
        kinds = [m["type"] for m in messages]
        self.assertEqual(kinds[0], "hello")
        self.assertEqual(messages[0]["run"], bus.run_id)
        self.assertIn("sessions", kinds)
        self.assertIn("event", kinds)
        self.assertIn("cursor", kinds, "a replayed event with coordinates should move the "
                                       "pointer before anything new happens")
        clicks = [m for m in messages if m["type"] == "event"
                  and m["event"]["kind"] == "click"]
        self.assertEqual(len(clicks), 1)

    def test_the_pointer_message_carries_the_coordinates(self):
        server = self.start_viewer(FakeAgent())
        bus.publish("canvas", "click", selector="#go", x=120.0, y=210.0, verdict="logged-in")
        messages = self.events(server, seconds=0.8)
        cursor = [m for m in messages if m["type"] == "cursor"]
        self.assertEqual((cursor[0]["x"], cursor[0]["y"], cursor[0]["click"]),
                         (120.0, 210.0, True))

    def test_an_event_without_coordinates_does_not_move_the_pointer(self):
        server = self.start_viewer(FakeAgent())
        bus.publish("canvas", "goto", url="https://example.com/", verdict="logged-in")
        messages = self.events(server, seconds=0.6)
        self.assertEqual([m for m in messages if m["type"] == "cursor"], [])

    def test_a_viewer_sees_what_happened_before_it_connected(self):
        """The viewer is normally opened after the agent has already been working, so
        a blank screen at connect would be the normal case."""
        server = self.start_viewer(FakeAgent())
        for n in range(5):
            bus.publish("canvas", "click", selector=f"#{n}", x=1.0, y=2.0)
        messages = self.events(server, seconds=1.0)
        replayed = [m["event"]["detail"]["selector"] for m in messages
                    if m["type"] == "event" and m["event"]["kind"] == "click"]
        self.assertEqual(replayed, ["#0", "#1", "#2", "#3", "#4"])


class TestFrames(ViewerCase):
    def test_a_frame_ends_up_on_the_frame_endpoint(self):
        agent = FakeAgent(frames=b"\xff\xd8\xffNOTREALLYJPEG")
        server = self.start_viewer(agent)
        server._start_view("canvas")
        view = server._views["canvas"]
        view.start()
        self.assertEqual(view.mode, "screencast")
        view.server.frame_ready(view, b"\xff\xd8\xffFRAME")
        status, body = self.get("/frame/canvas")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\xff\xd8\xffFRAME")
        view.stop()

    def test_a_screencast_frame_is_forwarded_and_acked(self):
        """The attach-time screenshot seeds the window, and the screencast takes over
        from there: the pushed frame has to become the one the viewer fetches."""
        agent = FakeAgent()
        server = self.start_viewer(agent)
        server._start_view("canvas")
        view = server._views["canvas"]
        self.assertIn("Page.startScreencast", agent.screencast)
        encoded = __import__("base64").b64encode(b"\xff\xd8\xffPUSHED").decode()
        view._on_frame({"data": encoded, "sessionId": 7})
        deadline = time.time() + 2
        while time.time() < deadline and server.frame("canvas") != b"\xff\xd8\xffPUSHED":
            time.sleep(0.05)
        self.assertEqual(server.frame("canvas"), b"\xff\xd8\xffPUSHED")
        self.assertIn("Page.screencastFrameAck", agent.screencast)
        view.stop()
        self.assertIn("Page.stopScreencast", agent.screencast)

    def test_attaching_seeds_a_frame_immediately(self):
        """Chromium's screencast sends nothing until the page next paints, and a page
        that is just sitting there may not paint for a long time. A viewer that opened
        onto a blank window would look broken."""
        agent = FakeAgent(frames=b"\xff\xd8\xffSEEDED")
        server = self.start_viewer(agent)
        server._start_view("canvas")
        view = server._views["canvas"]
        deadline = time.time() + 3
        while time.time() < deadline and server.frame("canvas") is None:
            time.sleep(0.05)
        self.assertEqual(server.frame("canvas"), b"\xff\xd8\xffSEEDED")
        self.assertEqual(view.frames.counter, 1)
        view.stop()

    def test_a_frame_announcement_is_pushed_to_viewers(self):
        server = self.start_viewer(FakeAgent())
        server._start_view("canvas")
        view = server._views["canvas"]
        seen = []
        started = threading.Event()

        def watch():
            started.set()
            seen.extend(m for m in self.events(
                server, seconds=2.0,
                until=lambda msgs: any(m["type"] == "frame" for m in msgs))
                if m["type"] == "frame")

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        started.wait(1)
        time.sleep(0.4)
        view.server.frame_ready(view, b"\xff\xd8\xffX")
        thread.join(5)
        view.stop()
        self.assertTrue(seen, "no frame message reached the viewer")
        self.assertGreaterEqual(seen[0]["counter"], 1)
        self.assertEqual(seen[0]["session"], "canvas")


class TestTheAgentIsNeverHarmed(ViewerCase):
    """The viewer is an observer. Everything it does has to be survivable."""

    def test_a_cursor_that_cannot_be_attached_falls_back_to_polling(self):
        agent = FakeAgent(cursor_ok=False)
        server = self.start_viewer(agent)
        server._start_view("canvas")
        view = server._views["canvas"]
        self.assertEqual(view.mode, "poll")
        self.assertIn("page went away", view.error)
        deadline = time.time() + 4
        while time.time() < deadline and server.frame("canvas") is None:
            time.sleep(0.1)
        self.assertIsNotNone(server.frame("canvas"),
                             "the poll fallback never produced a frame")
        view.stop()

    def test_a_screencast_that_cannot_start_falls_back_to_polling(self):
        agent = FakeAgent()

        def refuse(method, params=None):
            raise RuntimeError("no screencast here")

        agent.send = refuse
        server = self.start_viewer(agent)
        server._start_view("canvas")
        view = server._views["canvas"]
        self.assertEqual(view.mode, "poll")
        view.stop()

    def test_a_permanent_screencast_error_is_not_scheduled_forever(self):
        server = self.start_viewer(FakeAgent())
        view = SessionView(server, "canvas")
        view.mode = "poll"
        view._retry_at = time.time() - 1
        view._send = lambda *_args, **_kwargs: b"jpeg"

        def unsupported():
            raise RuntimeError("screencast unsupported")

        view._start_screencast = unsupported
        view._poll()
        self.assertEqual(view._retry_at, 0.0)
        self.assertIn("screencast unsupported", view.error)
        view.stop()

    def test_a_session_that_is_busy_is_skipped_not_waited_on(self):
        """A viewer that sat behind a five-minute login wait would look like a frozen
        app, so every call the viewer makes has a short timeout."""
        registry = SessionRegistry(factory=lambda spec=None: FakeAgent())
        session = registry.get("slow", spec=None)
        # Long enough to still be running when the viewer asks, and no longer: the
        # teardown has to wait this out.
        threading.Thread(target=lambda: session.submit(lambda b: time.sleep(0.8)),
                         daemon=True).start()
        time.sleep(0.1)
        viewer.registry = registry
        real_timeout = viewer.VIEW_TIMEOUT_S
        viewer.VIEW_TIMEOUT_S = 0.3
        try:
            view = SessionView(self.start_viewer(), "slow")
            started = time.time()
            with self.assertRaises(Exception):
                view._send(lambda browser: None)
            self.assertLess(time.time() - started, 0.7,
                            "the viewer waited on a busy session instead of skipping it")
        finally:
            viewer.VIEW_TIMEOUT_S = real_timeout
            registry.close_all()

    def test_closing_every_session_does_not_break_the_viewer(self):
        server = self.start_viewer(FakeAgent())
        self.registry.close_all()
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sessions"], [])
        messages = self.events(server, seconds=0.6)
        self.assertEqual(messages[0]["type"], "hello")


class TestClosingASession(ViewerCase):
    """The one thing the viewer can do that changes the agent's world.

    It is a POST to the session's own path, it reports what is left, and it is the way to
    clear a session whose own thread has stopped answering - the HTTP API stays answerable
    when the session does not, which is how the stuck `default` session got cleared.
    """

    def test_closing_a_session_stops_it_and_reports_what_is_left(self):
        agent = FakeAgent()
        self.start_viewer(agent, name="canvas")
        self.assertEqual([s["name"] for s in json.loads(self.get("/sessions")[1])["sessions"]],
                         ["canvas"])

        status, body = self.post("/session/canvas/close")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["session"], "canvas")
        self.assertEqual(payload["running"], [])
        self.assertTrue(agent.closed, "the browser behind the session was not closed")
        self.assertEqual(json.loads(self.get("/sessions")[1])["sessions"], [])

    def test_closing_one_session_leaves_the_others_alone(self):
        first, second = FakeAgent(), FakeAgent()
        made = []

        def factory(spec=None):
            made.append(spec)
            return first if len(made) == 1 else second

        from latchkey.sessions import SessionRegistry
        self.registry = SessionRegistry(factory=factory)
        viewer.registry = self.registry
        self.start_viewer(None)
        self.registry.get("one", spec=None)
        self.registry.get("two", spec=None)

        status, body = self.post("/session/one/close")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["running"], ["two"])
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertEqual([s["name"] for s in json.loads(self.get("/sessions")[1])["sessions"]],
                         ["two"])

    def test_closing_something_that_is_not_there_says_what_is(self):
        self.start_viewer(FakeAgent(), name="canvas")
        status, body = self.post("/session/typo/close")
        self.assertEqual(status, 404)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("typo", payload["error"])
        self.assertEqual(payload["running"], ["canvas"])
        self.assertEqual([s["name"] for s in json.loads(self.get("/sessions")[1])["sessions"]],
                         ["canvas"], "a failed close must not touch what is running")

    def test_the_closed_session_is_announced_to_the_other_viewers(self):
        self.start_viewer(FakeAgent(), name="canvas")
        server = self.servers[-1]
        seen = []
        thread = threading.Thread(target=lambda: seen.extend(self.events(
            server, seconds=2.5,
            until=lambda msgs: any(m["type"] == "sessions" and not m["sessions"]
                                   for m in msgs))), daemon=True)
        thread.start()
        time.sleep(0.3)
        self.post("/session/canvas/close")
        thread.join(4)
        lists = [m["sessions"] for m in seen if m["type"] == "sessions"]
        self.assertTrue(lists, "no session list was broadcast after the close")
        self.assertEqual(lists[-1], [], "the last broadcast still listed the closed session")

    def test_a_post_to_an_unknown_path_is_a_clean_404(self):
        self.start_viewer()
        status, body = self.post("/session/canvas/delete")
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body)["error"])
        self.assertEqual(self.get("/health")[0], 200)

    def test_a_frame_for_an_unknown_session_says_which_and_what_is_running(self):
        self.start_viewer(FakeAgent(), name="canvas")
        status, body = self.get("/frame/typo")
        self.assertEqual(status, 404)
        payload = json.loads(body)
        self.assertIn("typo", payload["error"])
        self.assertEqual(payload["running"], ["canvas"])
        self.assertIn("closed", payload["hint"])


class TestTheViewerPage(unittest.TestCase):
    """The built-in page has to match the Electron window: same endpoints, same controls."""

    def test_the_page_has_no_action_list(self):
        self.assertNotIn("actions", viewer.viewer_page())
        self.assertNotIn("<ul", viewer.viewer_page())

    def test_the_page_offers_the_close_control(self):
        self.assertIn("#tabs", viewer.viewer_page())
        self.assertIn("/close", viewer.viewer_page())
        self.assertIn("arming", viewer.viewer_page())

    def test_the_page_explains_a_session_that_is_not_there(self):
        page = viewer.viewer_page()
        self.assertIn("does not exist on this viewer", page)
        self.assertIn("Running:", page)
        self.assertIn("another server", page)

    def test_the_page_still_speaks_the_same_api(self):
        page = viewer.viewer_page()
        # The page takes its session list from the event stream, so /sessions is not in it.
        for endpoint in ("/events", "/frame/", "/session/"):
            self.assertIn(endpoint, page)

    def test_the_page_says_where_the_sessions_are_when_it_has_none(self):
        """An empty window is not the same as an idle agent, and must not be read that way."""
        page = viewer.viewer_page()
        self.assertIn("/elsewhere", page)
        self.assertIn("on another latchkey viewer", page)


class SlowAgent(FakeAgent):
    """A session that does the work, but whose answer comes back late.

    This is the shape of a failure seen under full-suite runs: `Session.submit` times out
    on the *wait* while the CDP call it asked for has already gone through. The viewer
    then wrote down "no screencast" about a screencast Chromium was streaming, so leaving
    never turned it off, and it went on feeding a window nobody had open.
    """

    def send(self, method, params=None):
        self.screencast.append(method)
        time.sleep(0.2)
        return {}


class AlreadyStreamingAgent(FakeAgent):
    """A page with a screencast already on it, as Chromium reports it.

    This is the shape the live viewer was found in: `startScreencast` comes back
    "Screencast is already active" because an earlier viewer walked away without
    stopping the stream. Reading that as "there is no screencast here" is what put a
    session - the agent's own thread, screenshots included - into the poll fallback.
    """

    def send(self, method, params=None):
        self.screencast.append(method)
        if method == "Page.startScreencast":
            raise RuntimeError("Protocol error (Page.startScreencast): "
                               "Screencast is already active")
        return {}


class TestAScreencastThatWasAlreadyRunning(ViewerCase):
    """A stream that is still pushing is the feed, not the absence of one."""

    def start_attached(self):
        agent = AlreadyStreamingAgent()
        server = self.start_viewer(agent)
        server._start_view("canvas")
        return agent, server, server._views["canvas"]

    def test_it_is_adopted_rather_than_polled(self):
        _agent, _server, view = self.start_attached()
        self.assertEqual(view.mode, "screencast")
        self.assertIsNone(view.error)
        view.stop()

    def test_its_frames_still_reach_the_stage(self):
        _agent, server, view = self.start_attached()
        encoded = __import__("base64").b64encode(b"\xff\xd8\xffADOPTED").decode()
        view._on_frame({"data": encoded, "sessionId": 3})
        deadline = time.time() + 2
        while time.time() < deadline and server.frame("canvas") != b"\xff\xd8\xffADOPTED":
            time.sleep(0.05)
        self.assertEqual(server.frame("canvas"), b"\xff\xd8\xffADOPTED")
        view.stop()

    def test_leaving_turns_the_adopted_stream_off(self):
        agent, _server, view = self.start_attached()
        view.stop()
        self.assertIn("Page.stopScreencast", agent.screencast)

    def test_one_that_pushes_nothing_falls_back_to_polling(self):
        """Adopting is an assumption, and a frozen picture is the one thing the viewer
        must not sit on while it guesses."""
        _agent, _server, view = self.start_attached()
        view._adopted_at = time.time() - viewer.ADOPT_GRACE_S - 1
        view._drop_adopted_screencast()
        self.assertEqual(view.mode, "poll")
        self.assertIn("polling", view.error)
        view.stop()

    def test_only_a_real_stream_is_read_that_way(self):
        self.assertTrue(viewer._already_streaming(RuntimeError(
            "Protocol error (Page.startScreencast): Screencast is already active")))
        self.assertFalse(viewer._already_streaming(RuntimeError("Target page closed")))


class TestThePageIsReadFromDisk(ViewerCase):
    """The page a server hands out has to be the page on disk now.

    A viewer server lives as long as the agent that started it, and the menu bar panel
    points at whichever one owns the sessions, so a page compiled into the process is a
    UI as old as that process. Eighteen hours old, in the case that turned this up: a
    tab strip, a frame counter, and no picture, served by a server that had started the
    night before the page was fixed.
    """

    def setUp(self):
        super().setUp()
        self.path = os.path.join(tempfile.mkdtemp(), "viewer_page.html")
        self._real_path = viewer.VIEWER_PAGE_PATH
        self._real_cache = viewer._page_cache
        viewer.VIEWER_PAGE_PATH = self.path
        viewer._page_cache = None
        self.write("<!doctype html><title>ONE</title>")

    def tearDown(self):
        viewer.VIEWER_PAGE_PATH = self._real_path
        viewer._page_cache = self._real_cache
        super().tearDown()

    def write(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)
        os.utime(self.path, ns=(time.time_ns(), time.time_ns()))

    def test_an_edited_page_is_served_without_a_restart(self):
        self.start_viewer()
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<!doctype html><title>ONE</title>")
        self.write("<!doctype html><title>TWO</title>")
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<!doctype html><title>TWO</title>")

    def test_a_missing_page_says_so_instead_of_breaking_the_viewer(self):
        os.remove(self.path)
        self.start_viewer()
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"viewer_page.html", body)


class TestWhenTheAnswerComesBackLate(unittest.TestCase):
    """What the viewer believes has to be decided by the work, not by the reply to it."""

    def setUp(self):
        self.agent = SlowAgent()
        self._real = viewer.registry
        self._real_timeout = viewer.VIEW_TIMEOUT_S
        viewer.VIEW_TIMEOUT_S = 0.05     # any answer slower than this is a late one
        viewer.registry = registry_with_agent(self.agent)
        bus.clear()
        self.server = ViewerServer(port=free_port()).start()
        time.sleep(0.05)

    def tearDown(self):
        self.server.shutdown()
        viewer.VIEW_TIMEOUT_S = self._real_timeout
        viewer.registry = self._real

    def test_a_late_start_is_still_a_screencast_that_has_to_be_stopped(self):
        inbox = self.server.add_viewer()
        deadline = time.time() + 8
        while time.time() < deadline and "Page.startScreencast" not in self.agent.screencast:
            time.sleep(0.1)
        self.assertIn("Page.startScreencast", self.agent.screencast)
        self.server.remove_viewer(inbox)
        deadline = time.time() + 8
        while time.time() < deadline and "Page.stopScreencast" not in self.agent.screencast:
            time.sleep(0.1)
        self.assertIn("Page.stopScreencast", self.agent.screencast,
                      "the feed was running, so the last viewer out has to turn it off")


class TestAttachOnlyWhileWatched(unittest.TestCase):
    """No viewer, no cursor node, no screencast - an unattended session stays cheap."""

    def setUp(self):
        self.agent = FakeAgent()
        self._real = viewer.registry
        viewer.registry = registry_with_agent(self.agent)
        bus.clear()
        self.server = ViewerServer(port=free_port()).start()
        time.sleep(0.05)

    def tearDown(self):
        self.server.shutdown()
        viewer.registry = self._real

    def test_the_monitor_starts_feeding_when_a_viewer_arrives(self):
        self.assertEqual(self.agent.cursor_calls, [])
        self.server.add_viewer()
        deadline = time.time() + 8
        while time.time() < deadline and "attach" not in self.agent.cursor_calls:
            time.sleep(0.1)
        self.assertIn("attach", self.agent.cursor_calls)
        self.assertEqual(self.server.viewer_count(), 1)
        # The feed comes up on a tick of its own, and a start that does not take is
        # retried on the next one, so wait for the fact rather than for the cursor.
        deadline = time.time() + 8
        while time.time() < deadline and "Page.startScreencast" not in self.agent.screencast:
            time.sleep(0.1)
        self.assertIn("Page.startScreencast", self.agent.screencast,
                      "a viewer arrived, so the feed has to come up")

    def test_the_cursor_is_put_back_when_the_last_viewer_leaves(self):
        inbox = self.server.add_viewer()
        deadline = time.time() + 8
        while time.time() < deadline and "attach" not in self.agent.cursor_calls:
            time.sleep(0.1)
        self.assertIn("attach", self.agent.cursor_calls)
        # Wait for the feed to be up before leaving. A viewer that goes before the start
        # reached the session leaves nothing to stop - on purpose: `begin` refuses to start
        # a screencast whose viewer has already gone - and this test is about the stop.
        deadline = time.time() + 8
        while time.time() < deadline and "Page.startScreencast" not in self.agent.screencast:
            time.sleep(0.1)
        self.assertIn("Page.startScreencast", self.agent.screencast)
        self.server.remove_viewer(inbox)
        # Generous: under a full-suite run the detach goes through the session's queue behind
        # whatever else is running, and a 3s window made this fail about one run in ten.
        deadline = time.time() + 8
        while time.time() < deadline and "detach" not in self.agent.cursor_calls:
            time.sleep(0.1)
        self.assertIn("detach", self.agent.cursor_calls)
        # Stopping the screencast is a second round trip on the same session queue, so it
        # gets waited for the same way. Asserting straight after the detach raced it and
        # failed a run in five; the behaviour under test - the last viewer out stops the
        # feed - was never in doubt.
        deadline = time.time() + 8
        while time.time() < deadline and "Page.stopScreencast" not in self.agent.screencast:
            time.sleep(0.1)
        self.assertIn("Page.stopScreencast", self.agent.screencast)

    def test_a_cursor_the_user_asked_for_is_left_alone(self):
        """`--cursor` means the user wants it; the viewer leaving must not switch it off."""
        self.agent.show_cursor = True
        inbox = self.server.add_viewer()
        time.sleep(0.5)
        self.server.remove_viewer(inbox)
        time.sleep(0.3)
        self.assertEqual(self.agent.cursor_calls, [])
        self.assertTrue(self.agent.show_cursor)


class TestViewerTools(unittest.TestCase):
    def setUp(self):
        bus.clear()

    def test_the_viewer_tools_are_advertised_and_lane_free(self):
        names = {tool["name"] for tool in mcp_server.CATALOG}
        self.assertIn("latchkey_viewer_start", names)
        self.assertIn("latchkey_viewer_stop", names)
        self.assertIsNone(mcp_server.LANE_OF["latchkey_viewer_start"])

    def test_starting_twice_returns_the_same_server(self):
        port = free_port()
        try:
            first = viewer.start_viewer(port)
            second = viewer.start_viewer(port)
            self.assertIs(first, second)
            self.assertEqual(first.url(), f"http://127.0.0.1:{port}/")
        finally:
            viewer.stop_viewer(port)

    def test_the_tool_returns_a_url_a_human_can_open(self):
        port = free_port()
        try:
            result = mcp_server.tool_viewer_start(port)
            self.assertEqual(result["url"], f"http://127.0.0.1:{port}/")
            self.assertEqual(result["viewers"], 0)
        finally:
            mcp_server.tool_viewer_stop(port)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAFrameBurstDoesNotQueueBehindTheAgent(ViewerCase):
    """Acking is a trip through the session's own lane - the lane an agent's calls are
    queued on - so a page that keeps moving must not cost one trip per frame, and an ack
    that has to wait must never be read as a broken screencast. Reading it that way used
    to drop the view into the poll fallback, whose full-size screenshots queue on that
    same lane, in front of the agent's next call."""

    def scene(self):
        agent = FakeAgent()
        server = self.start_viewer(agent)
        server._start_view("canvas")
        view = server._views["canvas"]
        agent.screencast.clear()
        trips = []
        real = view._send

        def counted(fn, timeout=None):
            trips.append(1)
            return real(fn, timeout)

        view._send = counted
        return agent, server, view, trips

    @staticmethod
    def frame(index):
        encoded = __import__("base64").b64encode(b"\xff\xd8\xff" + bytes([index])).decode()
        return {"data": encoded, "sessionId": index}

    def test_three_frames_cost_one_trip_and_keep_the_newest(self):
        agent, server, view, trips = self.scene()
        for index in (1, 2, 3):
            view._on_frame(self.frame(index))
        view._pump_screencast()
        self.assertEqual(len(trips), 1, "one trip through the lane, not three")
        self.assertEqual(agent.screencast.count("Page.screencastFrameAck"), 3,
                         "every frame still gets its ack")
        self.assertEqual(server.frame("canvas"), b"\xff\xd8\xff\x03")

    def test_an_ack_that_cannot_get_a_turn_is_not_a_broken_screencast(self):
        _agent, _server, view, _trips = self.scene()

        def busy(fn, timeout=None):
            raise TimeoutError("the session's lane is busy with the agent")

        view._send = busy
        view._on_frame(self.frame(1))
        view._pump_screencast()
        self.assertEqual(view.mode, "screencast")
        view.stop()

    def test_a_poll_that_cannot_get_a_turn_stands_down_instead_of_queueing_up(self):
        _agent, _server, view, _trips = self.scene()
        view.mode = "poll"
        view._last_poll = 0.0

        def busy(fn, timeout=None):
            raise TimeoutError("the session's lane is busy with the agent")

        view._send = busy
        view._poll()
        self.assertGreater(view._last_poll, time.time() - 1 + viewer.POLL_BACKOFF_S - 1)
        self.assertIn("lane", view.error)
        view.stop()

    def test_the_poll_waits_less_than_the_viewer_used_to_before_giving_up_on_a_turn(self):
        self.assertLess(viewer.POLL_TIMEOUT_S, viewer.VIEW_TIMEOUT_S)


class TestThePointerIsPlacedInThePagesPixels(unittest.TestCase):
    """The frame is a picture of the page, and the two are different sizes."""

    def test_the_served_page_scales_the_pointer_by_the_viewport(self):
        page = viewer.viewer_page()
        self.assertIn("data.vw", page, "the page size travels with the event")
        self.assertIn("box.width / vw", page)
        self.assertNotIn("box.width / img.naturalWidth", page,
                         "scaling by the picture is the bug this fixed")

    def test_the_marker_is_a_ring_with_a_crosshair(self):
        page = viewer.viewer_page()
        self.assertIn("radial-gradient(circle, rgba(239,68,68,.95) 0 3px", page)
        self.assertIn("border-radius:50%; border:2px solid #ef4444", page)

    def test_a_server_restart_resets_client_event_dedupe(self):
        page = viewer.viewer_page()
        self.assertIn("resetForRun(data.run)", page)
        self.assertIn("e.run === event.run && e.seq === event.seq", page)
        app = (pathlib.Path(__file__).resolve().parent.parent
               / "viewer/renderer/app.js").read_text(encoding="utf-8")
        self.assertIn("resetRun(message.run)", app)
        self.assertIn("for (const rec of state.sessions.values()) rec.lastSeq = 0", app)

    def test_the_electron_window_scales_it_the_same_way(self):
        app = (pathlib.Path(__file__).resolve().parent.parent
               / "viewer/renderer/app.js").read_text(encoding="utf-8")
        self.assertIn("box.width / vw", app)
        self.assertIn("rec.viewport", app, "the page size is remembered per session")


def _get_json(port: int, path: str) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        return json.loads(connection.getresponse().read())
    finally:
        connection.close()


class HeldPort:
    """Something already listening on a port, for the tests about a taken one.

    This is the shape of the real thing: every MCP connection is handed the same
    `LATCHKEY_VIEWER_PORT`, so the port a later server asks for is usually gone.
    """

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]

    def close(self):
        self.socket.close()


class TestATakenPortRollsForward(unittest.TestCase):
    """Two latchkey processes asking for one viewer port is the normal case, not an error.

    The viewer socket has to live in the process holding the sessions, and that is usually
    *not* the process that came up first: `LATCHKEY_VIEWER_PORT=8788` is handed to every
    MCP connection, so the first one takes 8788 and every later one used to fail to bind
    and end up with no viewer at all. What the human then saw was the first process's
    viewer - connected, live, and permanently empty, because the agent was working in the
    other process. Failing to bind is not a visible failure; rolling forward is.
    """

    def setUp(self):
        self._real_viewers_file = viewer.VIEWERS_FILE
        viewer.VIEWERS_FILE = os.path.join(tempfile.mkdtemp(), "viewers.json")

    def tearDown(self):
        for server in list(viewer._servers.values()):
            server.shutdown()
        viewer._servers.clear()
        viewer.VIEWERS_FILE = self._real_viewers_file

    def test_the_viewer_lands_on_the_next_free_port_and_is_findable_there(self):
        held = HeldPort()
        self.addCleanup(held.close)

        server = viewer.start_viewer(held.port)
        self.addCleanup(lambda: viewer.stop_viewer(held.port))

        self.assertEqual(server.port, held.port + 1)
        self.assertEqual(_get_json(server.port, "/health")["ok"], True)
        # It writes itself down where it actually listens, so a client's scan and the
        # registry agree with the socket.
        self.assertIn(server.port, [entry["port"] for entry in viewer.known_viewers()])

    def test_asking_twice_gives_back_the_same_server_not_a_third_port(self):
        held = HeldPort()
        self.addCleanup(held.close)

        first = viewer.start_viewer(held.port)
        self.addCleanup(lambda: viewer.stop_viewer(held.port))

        self.assertIs(viewer.start_viewer(held.port), first)
        self.assertEqual(viewer.start_viewer(held.port).port, held.port + 1)

    def test_stop_viewer_stops_it_wherever_it_landed(self):
        held = HeldPort()
        self.addCleanup(held.close)
        viewer.start_viewer(held.port)

        viewer.stop_viewer(held.port)

        self.assertEqual([entry for entry in viewer.known_viewers()
                          if entry["port"] == held.port + 1], [])
        with self.assertRaises(OSError):
            _get_json(held.port + 1, "/health")

    def test_a_range_with_nothing_free_in_it_says_so(self):
        held = HeldPort()
        self.addCleanup(held.close)
        with self.assertRaises(OSError):
            viewer.start_viewer(held.port, span=1)


def _stop(server) -> None:
    """Stop a stand-in viewer and release its socket, or unittest warns about the leak."""
    server.shutdown()
    server.server_close()


class FakeOtherViewer:
    """A viewer in another process, which is all `/health` has to come from.

    The real one serves its own process's sessions; the only thing this server needs from
    it is what it answers on `/health`, so that is all this answers.
    """

    def __init__(self, name="canvas", payload=None):
        body = json.dumps(payload if payload is not None else
                          {"ok": True, "uptime_s": 1.0, "viewers": 1,
                           "sessions": [name]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # As if it had registered itself on the way up, which is how a client finds it.
        viewer._write_registry(viewer.known_viewers() + [{
            "port": self.port, "pid": os.getpid(), "host": "127.0.0.1",
            "url": f"http://127.0.0.1:{self.port}/", "started_at": time.time()}])


class TestAnEmptyViewerSaysWhereTheSessionsAre(ViewerCase):
    """"No sessions here" is not "no sessions", and the window owes the human the differ-
    ence: a reader who sees an empty window while an agent is plainly working concludes
    latchkey is broken, and goes looking in the wrong place."""

    def setUp(self):
        super().setUp()
        viewer.registry = SessionRegistry()      # this process has no sessions of its own

    def test_the_route_names_the_viewer_that_does_have_sessions(self):
        other = FakeOtherViewer("canvas")
        self.addCleanup(_stop, other.server)
        self.start_viewer()

        status, body = self.get("/elsewhere")
        report = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual([viewer_row["port"] for viewer_row in report["elsewhere"]],
                         [other.port])
        self.assertEqual(report["elsewhere"][0]["sessions"], ["canvas"])
        self.assertEqual(report["elsewhere"][0]["url"], f"http://127.0.0.1:{other.port}/")

    def test_a_viewer_with_sessions_of_its_own_does_not_send_you_elsewhere(self):
        other = FakeOtherViewer("canvas")
        self.addCleanup(_stop, other.server)
        self.start_viewer(FakeAgent())

        status, body = self.get("/elsewhere")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["elsewhere"], [])

    def test_a_viewer_that_is_not_latchkey_is_not_offered_as_an_answer(self):
        """A port in the range that answers with something else is not a viewer."""
        impostor = FakeOtherViewer(payload={"hello": "world"})
        self.addCleanup(_stop, impostor.server)
        self.start_viewer()

        status, body = self.get("/elsewhere")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["elsewhere"], [])
