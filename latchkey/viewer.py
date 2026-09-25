"""Watching an agent work: the HTTP + SSE server behind the viewer app.

This is the human's window, not an agent's tool. It serves the event stream, the
list of sessions, and the screen. Three things it deliberately does not do:

* it never drives the browser. Every call it makes is `attach_cursor`,
  `start_screencast` or a screenshot, and every one is guarded, so a viewer that
  connects, disconnects or wedges itself can never fail an agent's action;
* it keeps no history of its own. `EventBus.recent()` already holds the stream
  the agent's own retry/undo path depends on, and a second copy would be a second
  thing to keep consistent;
* it never sits between the agent and a page. Frames come from Chromium's
  screencast pushed over CDP, and the pointer rides along in the events the agent
  already publishes, so nothing is added to the critical path;
* it sends no CORS headers, on purpose. A `file://` renderer cannot read a
  cross-origin stream without `Access-Control-Allow-Origin`, so the Electron app adds
  the header to its *own* requests (`webRequest.onHeadersReceived` in `viewer/main.js`).
  Adding it here instead would be one line, and would also mean any web page the user
  happens to have open could read their browsing off `127.0.0.1:8788/events`.

Where does it run? Sessions live in a process. The MCP server is usually that
process, so the viewer listens inside it: set `LATCHKEY_VIEWER_PORT` in the MCP
server's environment, call the `latchkey_viewer_start` tool, or run
`python3 -m latchkey watch` for a session you are driving from your own script.

It serves the sessions in the shared `latchkey.sessions.registry`. A script that
wants to be watched therefore has to open its session through that registry
(`from latchkey import registry; registry.get("canvas")`), not through a
`SessionRegistry` of its own - a separate registry is a separate process's worth
of sessions as far as the viewer is concerned.

    python3 -m latchkey watch --port 8788
    open http://127.0.0.1:8788/
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import http.client
import json
import os
import queue
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import paths
from .events import bus
from .sessions import SessionError, registry

DEFAULT_PORT = 8788
# How far `start_viewer` may walk to find a free port. Every MCP connection is handed the
# same `LATCHKEY_VIEWER_PORT`, so several server processes ask for 8788 and only the first
# can have it - and the one that loses is often the one the agent is actually driving. A
# taken port therefore rolls forward, inside the range every client scans (8788..8798), so
# that whoever ends up holding the sessions also holds a viewer.
VIEWER_PORT_SPAN = 11
# How often an empty viewer asks the other viewers whether they have anything, in seconds.
ELSEWHERE_TTL_S = 5.0

# Where a viewer writes down that it exists. A client cannot guess which port a viewer is
# on - every MCP connection gets its own server process, and the socket is inside that
# process, so "the viewer" is whichever process happens to hold the session. Without this
# file a client has to guess a port, and guessing wrong looks exactly like "no sessions".
VIEWERS_FILE = paths.path("viewers.json")
HISTORY = 200                 # events replayed to a viewer as it connects
RETRY_ATTACH_S = 20           # a screencast that failed on a busy session is retried after this
FRAME_QUALITY = 55            # jpeg quality for the screencast
FRAME_WIDTH = 1100            # scaled-down frames: the viewer is a window, not a monitor
FALLBACK_INTERVAL_S = 0.7     # screenshot cadence when screencast is unavailable
POLL_TIMEOUT_S = 2.0          # a screenshot that cannot get a turn stands down
POLL_BACKOFF_S = 3.0          # ...and does not come back for this long
ACK_GIVE_UP_S = 3.0           # a queued ack is assumed lost after this
HEARTBEAT_S = 15.0            # keep-alive, and how a dead viewer is noticed
VIEWER_QUEUE = 400            # a stalled viewer gets dropped frames, never the agent
VIEW_TIMEOUT_S = 5.0          # never park a viewer thread behind a busy session
STOP_ATTEMPT_S = 1.0          # the screencast stop is retried, so one attempt is short
STOP_DEADLINE_S = 5.0         # ...and it is only given up on after this long
ADOPT_GRACE_S = 6.0           # an adopted screencast gets this long to prove it pushes
SHUTDOWN_POLL_S = 0.05        # how soon serve_forever notices it has been asked to stop

VIEWER_PAGE_PATH = Path(__file__).with_name("viewer_page.html")

_page_lock = threading.Lock()
_page_cache: tuple[int, str] | None = None

_MISSING_PAGE = """<!doctype html><meta charset="utf-8"><title>latchkey</title>
<body style="font:14px -apple-system,sans-serif;background:#0f1114;color:#e6e8eb;padding:24px">
<p>The viewer page is missing: <code>%s</code></p>
<p>It ships next to <code>viewer.py</code>; restore it and reload this page.</p>
""" % (VIEWER_PAGE_PATH,)


def viewer_page() -> str:
    """The viewer page, as it is on disk right now.

    Deliberately not a string compiled into this module at import time. A viewer
    server is long-lived - the one behind a person's menu bar panel had been up for
    eighteen hours - and it serves the page from its own memory, so a page that is
    fixed in this file is a page nobody sees until every server is restarted. Read
    per request, with the mtime as the cache key, a fix to the page lands on the
    next reload instead of the next restart.
    """
    global _page_cache
    path = Path(VIEWER_PAGE_PATH)   # a str here is a test pointing this at a tmpfile
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return _MISSING_PAGE
    with _page_lock:
        if _page_cache is None or _page_cache[0] != stamp:
            _page_cache = (stamp, path.read_text(encoding="utf-8"))
        return _page_cache[1]




def cursor_message(event) -> dict | None:
    """The pointer position an event implies, or None if it does not name one.

    The pointer is not a separate feed: it rides in the events the agent already
    publishes, so the viewer can animate it between frames without asking the page
    anything. Used for live events and for the replay a viewer gets as it connects.
    """
    detail = event.detail
    if "x" not in detail or "y" not in detail:
        return None
    return {"type": "cursor", "session": event.session, "x": detail["x"],
            "y": detail["y"], "click": event.kind == "click", "kind": event.kind,
            # The page's own size, when the agent's side knew it: a frame is a picture of the
            # viewport at the screencast's own width (1100 by default) while a page is 1280
            # of them, so a viewer without this has to guess the units and draws the pointer
            # where the mouse is not. Rebuilding the message from x and y alone is exactly
            # what left the panel's pointer a sixth of the way out.
            **{key: detail[key] for key in ("vw", "vh") if key in detail}}


_ALREADY_STREAMING = ("already active", "already started", "already running")


def _already_streaming(exc: BaseException) -> bool:
    """Is this failure Chromium saying a screencast for this page is already on?

    The wording is Chromium's, not ours, so it is matched loosely - and only for the
    readings that mean there really is a stream. Anything else is a screencast that
    could not start, and that one still wants the poll fallback.
    """
    text = str(exc).lower()
    return any(phrase in text for phrase in _ALREADY_STREAMING)


class FrameStore:
    """The newest frame for one session, and a counter a viewer can compare.

    Only the newest frame is kept: a viewer that misses one should skip ahead, not
    replay history it would then race to catch up with.
    """

    def __init__(self) -> None:
        self.jpeg: bytes | None = None
        self.counter = 0
        self.lock = threading.Lock()

    def put(self, data: bytes) -> int:
        with self.lock:
            self.jpeg, self.counter = data, self.counter + 1
            return self.counter

    def get(self) -> tuple[bytes | None, int]:
        with self.lock:
            return self.jpeg, self.counter


class SessionView:
    """One session's screen feed.

    Screencast first: Chromium pushes frames over CDP, so the viewer sees the page
    change without anyone polling it. If that is unavailable (a page that is mid-
    navigation, an older Chromium), a screenshot poller takes over at a lower rate,
    because a slow picture beats no picture.
    """

    def __init__(self, server: "ViewerServer", name: str) -> None:
        self.server = server
        self.name = name
        self.frames = FrameStore()
        self.mode = "none"                 # screencast | poll | none
        self.error: str | None = None
        self._retry_at = 0.0            # when to try the screencast again after a busy attach
        self._pending: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._pump: threading.Thread | None = None
        self._cdp_token: Any = None
        self._cursor_attached = False
        self._last_poll = 0.0
        self._adopted = False              # a screencast this page was already running
        self._adopted_at = 0.0
        self._screencast_frames = 0        # frames the screencast has actually pushed
        self._ack_out = False              # an ack task may still be queued behind the agent
        self._ack_out_at = 0.0

    # -- lifecycle ---------------------------------------------------------

    @property
    def session(self):
        return registry.require(self.name)

    def _send(self, fn: Any, timeout: float | None = None) -> Any:
        """Ask the session for something, briefly.

        The short timeout is the important part: a session may be minutes into a
        login wait, and a viewer thread must never queue behind that. Whatever it
        could not do this time it tries again on the next tick.

        Callers with no next tick pass their own budget, which is why the timeout is a
        parameter at all - see `_stop_screencast`.
        """
        return self.session.submit(fn, timeout=VIEW_TIMEOUT_S if timeout is None else timeout)

    def start(self) -> None:
        """Attach the cursor and start feeding frames. Safe to call again."""
        if self._pump and self._pump.is_alive():
            return
        self._stop.clear()
        try:
            wanted = self._send(lambda browser: not browser.show_cursor)
            if wanted:
                # The flag goes up *before* the call, because this runs on the monitor
                # thread while a viewer can already be leaving: a stop() that landed in
                # between would have found it False, never put the cursor back, and left a
                # red dot on the page for good. Detaching a cursor that is not there is
                # harmless, so erring this way costs nothing.
                self._cursor_attached = True
                try:
                    self._send(lambda browser: browser.attach_cursor())
                except Exception:  # noqa: BLE001
                    self._cursor_attached = False
                    raise
            self._start_screencast()
        except Exception as exc:  # noqa: BLE001
            # A viewer must never be the reason an agent fails: fall back to polling. A
            # session that was merely busy when the viewer arrived (a SessionError timeout)
            # is asked again later, from the poll loop, rather than left on the slow feed
            # with a red mark for the rest of its life.
            self.error = f"{type(exc).__name__}: {exc}"
            self.mode = "poll"
            # A queue timeout can clear by itself.  A protocol/browser error cannot;
            # repeatedly retrying the latter costs the agent a CDP task forever.
            if isinstance(exc, SessionError):
                self._retry_at = time.time() + RETRY_ATTACH_S
        if self.mode == "none":
            self.mode = "poll"
        self._seed_first_frame()
        self._seed_cursor()
        self._pump = threading.Thread(target=self._run, name=f"view-{self.name}", daemon=True)
        self._pump.start()

    def _seed_first_frame(self) -> None:
        """One screenshot so the viewer is never blank.

        Chromium's screencast sends its first frame when the page next paints, and on
        a page that is just sitting there that can be a long time. One screenshot at
        attach time means the window shows the page immediately, and the screencast
        takes over from there.
        """
        try:
            data = self._send(lambda browser: browser.page.screenshot(
                type="jpeg", quality=FRAME_QUALITY))
        except Exception:  # noqa: BLE001
            return
        if data:
            self.server.frame_ready(self, data)

    def stop(self) -> None:
        self._stop.set()
        self._pending.put(None)
        # Let the pump finish what it is doing before tearing anything down: it may be
        # mid-screenshot, and a detach queued behind that is a detach that arrives late or
        # not at all if the session is busy.
        if self._pump and self._pump.is_alive() and self._pump is not threading.current_thread():
            self._pump.join(timeout=VIEW_TIMEOUT_S)
        try:
            self._stop_screencast()
            if self._cursor_attached:
                self._send(lambda browser: browser.detach_cursor())
        except Exception:  # noqa: BLE001
            pass
        self._cursor_attached = False
        self.mode = "none"

    def refresh(self) -> None:
        """Re-point the feed at the page this session is driving now.

        A screencast belongs to one page, so after a tab switch the old feed would
        keep showing the tab nobody is looking at.
        """
        try:
            token = self._send(lambda browser: (id(browser.page), browser.cdp_for(browser.page)))
        except Exception:  # noqa: BLE001
            return
        if token[0] != (self._cdp_token[0] if self._cdp_token else None):
            self._stop_screencast()
            self._start_screencast()

    # -- screencast --------------------------------------------------------

    def _start_screencast(self) -> bool:
        """Turn on Chromium's frame push for the page this session is driving.

        What is really running is decided *in here*, on the session's thread, because
        this side can only ask and be told: when the queue is busy the answer comes back
        late (VIEW_TIMEOUT_S) even though the work happened. Reading that as "no
        screencast" is what left a feed running for a window nobody had open - a viewer
        that believes there is no screencast never ends one.
        """
        def begin(browser):
            if self._stop.is_set():
                # The last viewer left while this was queued. `stop()` has already looked
                # for a screencast to end and found none, so starting one now would leave
                # a feed running with no pump behind it and nobody watching.
                return False
            cdp = browser.cdp_for(browser.page)
            # The token goes up *before* the send, the same discipline as the cursor flag:
            # a viewer can already be leaving, and a stop that lands in between has to find
            # something to stop. Stopping a screencast that never started is harmless,
            # and the failure path below takes the token back down.
            self._cdp_token = (id(browser.page), cdp)
            self._adopted = False
            self._screencast_frames = 0
            try:
                cdp.on("Page.screencastFrame", self._on_frame)
                cdp.send("Page.startScreencast", {
                    "format": "jpeg", "quality": FRAME_QUALITY, "maxWidth": FRAME_WIDTH,
                    "maxHeight": FRAME_WIDTH, "everyNthFrame": 1})
            except Exception as exc:  # noqa: BLE001
                if not _already_streaming(exc):
                    self._cdp_token = None
                    self.error = (f"screencast unavailable ({type(exc).__name__}: {exc}); "
                                  f"polling")
                    self.mode = "poll"
                    return False
                # "Screencast is already active": this page's own connection is already
                # pushing frames, left behind by a viewer that went away before the
                # stream could be stopped. The listener above receives those frames
                # either way, so adopt the stream instead of falling back - polling
                # takes a full-size screenshot on the session's own thread twice a
                # second, which is both the slow, jerky feed this feed exists to avoid
                # and work queued in front of the agent.
                self._adopted = True
                self._adopted_at = time.time()
            self.mode = "screencast"
            self.error = None
            return True

        try:
            return bool(self._send(begin))
        except Exception as exc:  # noqa: BLE001
            # Late, or never: `begin` reports for itself when the queue gets to it, and
            # this is the honest reading until then.
            if self._cdp_token is None:
                self.error = (f"screencast start unconfirmed ({type(exc).__name__}: {exc}); "
                              f"polling")
                self.mode = "poll"
            # Only a session queue timeout is transient.  Protocol failures (unsupported
            # screencast, a closed CDP target) must remain on the low-rate poller instead
            # of creating an endless stream of failed start calls.
            if isinstance(exc, SessionError):
                self._retry_at = time.time() + RETRY_ATTACH_S
            return False

    def _stop_screencast(self) -> None:
        """Turn Chromium's frame push off, and mean it.

        Decided on the session's thread like the start, and retried from here, because
        this is the one viewer call with no next tick to fall back on: the pump is already
        on its way out, so a stop that quietly failed to get through would leave Chromium
        encoding frames for a viewer that has gone, for the life of the session.
        """
        if self._cdp_token is None:
            return

        def end(browser):
            browser.cdp_for(browser.page).send("Page.stopScreencast")
            self._cdp_token = None      # the send happened; this side can believe it
            return True

        deadline = time.time() + STOP_DEADLINE_S
        while self._cdp_token is not None:
            try:
                self._send(end, timeout=STOP_ATTEMPT_S)
            except Exception:  # noqa: BLE001
                pass
            if self._cdp_token is None:
                return
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        self.error = f"the screencast would not stop within {STOP_DEADLINE_S:.0f}s"
        self._cdp_token = None

    def _on_frame(self, params: dict) -> None:
        """Called by Playwright's dispatcher. Enqueue only, never touch the page.

        Doing any Playwright work here would run it on the dispatcher thread, which
        is not the thread that owns the browser, and block frame delivery while it
        waits. The pump acks and stores instead.
        """
        if self._stop.is_set():
            return          # a stream that outlived its view must not queue up behind it
        self._screencast_frames += 1
        self._pending.put(params)

    # -- the pump ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.mode == "screencast":
                self._pump_screencast()
            else:
                self._poll()

    def _pump_screencast(self) -> None:
        try:
            params = self._pending.get(timeout=1.0)
        except queue.Empty:
            self._drop_adopted_screencast()
            return
        if params is None:
            return
        # Everything Chromium pushed while the last one was being dealt with, in one go.
        # An ack is a round trip through the session's own lane - the agent's lane, where
        # the calls of a batch are queued - so one ack per frame is the viewer being paid
        # for out of the agent's latency. Only the newest frame is worth keeping; the
        # older ones need their ack and nothing else, and one task can carry all of them.
        frames = []
        while params is not None:
            frames.append(params)
            try:
                params = self._pending.get_nowait()
            except queue.Empty:
                break
        frames = [frame for frame in frames if frame]
        if not frames:
            return
        ids = [frame["sessionId"] for frame in frames if frame.get("sessionId") is not None]
        try:
            data = base64.b64decode(frames[-1].get("data", ""))
        except Exception:  # noqa: BLE001
            data = b""
        if data:
            self.server.frame_ready(self, data)
        if not ids:
            return
        # One ack at a time: while one is still queued behind the agent's work, more
        # frames would only queue more of them behind it, and Chromium waits for the ack
        # before pushing again, so nothing is lost by waiting here. An ack that is slow
        # is *not* a broken screencast - reading it as one used to drop the view into the
        # poll fallback, whose full-size screenshots queue on that same lane, in front of
        # the agent's own calls. That is how watching a page made a batch time out.
        if self._ack_out and time.time() - self._ack_out_at < ACK_GIVE_UP_S:
            return
        self._ack_out = True
        self._ack_out_at = time.time()
        try:
            # The ack has to come from the session's own thread: Chromium sends no
            # further frames until the last one is acked.
            self._send(lambda browser, ids=tuple(ids): [
                browser.cdp_for(browser.page).send("Page.screencastFrameAck", {"sessionId": sid})
                for sid in ids])
            self._ack_out = False
        except Exception:  # noqa: BLE001
            # Probably still queued: the next ack carries everything that piled up
            # behind it, and ACK_GIVE_UP_S is the way out if it never lands.
            pass

    def _seed_cursor(self) -> None:
        """Put the pointer where the mouse already is, for a viewer that arrived late.

        Nobody moves the mouse because someone started watching, so a viewer that attaches
        mid-session would otherwise show no pointer at all until the agent's next click -
        and "where is it right now" is the question a viewer is usually opened to answer.
        """
        try:
            where = self._send(lambda browser: (
                {"x": browser._cursor[0], "y": browser._cursor[1], **browser.viewport}
                if getattr(browser, "_cursor", None) else None))
        except Exception:  # noqa: BLE001
            return          # a viewer must never be the reason an agent fails
        if where:
            self.server.publish_pointer({"type": "cursor", "session": self.name,
                                         "click": False, "kind": "seed", **where})

    def _drop_adopted_screencast(self) -> None:
        """Give up on an adopted stream that never pushed anything.

        Adopting is the right reading of "already active" for one of these, but it is
        still an assumption, and the one thing the viewer must not do is sit on a
        frozen picture. A few seconds without a frame says the assumption was wrong.
        """
        if not self._adopted or self._screencast_frames:
            return
        if time.time() - self._adopted_at < ADOPT_GRACE_S:
            return
        self._adopted = False
        self.mode = "poll"
        self.error = "the screencast already running here pushed no frames; polling"

    def _poll(self) -> None:
        """Screenshot at a low rate. The fallback, and the reason the viewer always
        shows something even when the screencast cannot start."""
        deadline = self._last_poll + FALLBACK_INTERVAL_S - time.time()
        if deadline > 0:
            self._stop.wait(min(deadline, 0.5))
            return
        self._last_poll = time.time()
        try:
            data = self._send(
                lambda browser: browser.page.screenshot(type="jpeg", quality=FRAME_QUALITY),
                timeout=POLL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            # A screenshot that could not get a turn is not a reason to fire another one
            # immediately: the task may still be queued behind the agent's work, and that
            # is how the poller ended up holding the lane the agent needs. Stand down
            # instead - and leave `error` alone if it already says why this view is not on
            # the screencast, because that is the more useful of the two answers.
            if not self.error:
                self.error = f"polling is waiting on the session's lane: {exc}"
            self._last_poll = time.time() + POLL_BACKOFF_S
            self._stop.wait(0.5)
            return
        if data:
            self.server.frame_ready(self, data)
            # A frame arrived, so whatever was said about the lane being busy is over.
            if self.error and self.error.startswith("polling is waiting"):
                self.error = None
            # ...and a screencast that could not start because the session was busy at the
            # time gets another chance, now that the session has just answered.
            if self._retry_at and time.time() >= self._retry_at and not self._stop.is_set():
                self._retry_at = 0.0
                try:
                    if self._start_screencast():
                        self.error = None
                except Exception as exc:  # noqa: BLE001
                    self.error = f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, SessionError):
                        self._retry_at = time.time() + RETRY_ATTACH_S


class ViewerServer(ThreadingHTTPServer):
    """The viewer's endpoints, and the viewers currently connected.

    Threading, because an event stream is a long-lived request and an agent's
    session list must still be answerable while one is open.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
        super().__init__((host, port), ViewerHandler)
        # port 0 means "any free port"; the bound one is the one to report, since that
        # is what the human will type and what the Electron app will connect to.
        self.host, self.port = host, self.server_address[1]
        self.started_at = time.time()
        self.last_error: str | None = None
        self._viewers: set[queue.Queue] = set()
        self._views: dict[str, SessionView] = {}
        self._last_cursor: dict[str, dict] = {}   # where each watched mouse was last seen
        self._guard = threading.Lock()
        self._elsewhere: list[dict] = []       # other viewers with sessions, and when we
        self._elsewhere_at = 0.0               # last looked - see elsewhere()
        self._subscriber = lambda: None
        self._monitor = threading.Thread(target=self._watch_sessions, name="view-sessions",
                                        daemon=True)

    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    # -- viewers -----------------------------------------------------------

    def add_viewer(self) -> queue.Queue:
        """Register a viewer and start watching. Returns its inbox.

        This returns immediately and does the browser work in the monitor thread: a
        session may be in the middle of a five-minute login wait, and a viewer that
        blocks on the handshake would look like a broken app. Attaching only when
        someone is watching is also what keeps an unattended session cheap - no
        cursor node in the page, no screencast, no frames.
        """
        inbox: queue.Queue = queue.Queue(maxsize=VIEWER_QUEUE)
        with self._guard:
            self._viewers.add(inbox)
            first = len(self._viewers) == 1
        if first and not self._monitor.is_alive():
            self._monitor = threading.Thread(target=self._watch_sessions,
                                            name="view-sessions", daemon=True)
            self._monitor.start()
        return inbox

    def remove_viewer(self, inbox: queue.Queue) -> None:
        """Last viewer leaving puts every session back to unattended."""
        with self._guard:
            self._viewers.discard(inbox)
            last = not self._viewers
            views = list(self._views.values()) if last else []
            if last:
                self._last_cursor.clear()      # a position nobody is watching goes stale
                self._views.clear()
        for view in views:
            view.stop()

    def viewer_count(self) -> int:
        with self._guard:
            return len(self._viewers)

    def elsewhere(self) -> list[dict]:
        """Other viewers on this machine that *do* have sessions, newest first.

        A viewer only ever serves the sessions of its own process, so the window a human
        has open can be sitting on a server with nothing to show while the agent works in
        a different process - the one whose viewer could not have the port, or the one
        nobody gave `LATCHKEY_VIEWER_PORT` to. From the human's side that is
        indistinguishable from "latchkey is not working", so an empty viewer answers with
        where the sessions actually are and the page links them.

        Costs one loopback request per other viewer, at most every `ELSEWHERE_TTL_S`, and
        only while this viewer has no sessions of its own.
        """
        if self.session_names():
            return []                 # this one is the one; nothing to explain
        now = time.time()
        with self._guard:
            cached = list(self._elsewhere)
            if now - self._elsewhere_at < ELSEWHERE_TTL_S:
                return cached
            self._elsewhere_at = now
        found = []
        for entry in known_viewers():
            port = entry.get("port")
            host = entry.get("host") or "127.0.0.1"
            if not isinstance(port, int) or port == self.port:
                continue
            report = _health_of(host, port)
            names = (report or {}).get("sessions")
            if names:
                found.append({"port": port, "host": host,
                              "url": entry.get("url") or f"http://{host}:{port}/",
                              "sessions": names})
        with self._guard:
            self._elsewhere = found
        return found

    def _start_view(self, name: str) -> None:
        with self._guard:
            view = self._views.get(name)
            if view is None:
                view = SessionView(self, name)
                self._views[name] = view
        view.start()

    # -- sessions and frames ----------------------------------------------

    def session_names(self) -> list[str]:
        return [row["name"] for row in registry.describe() if row.get("alive")]

    def has_session(self, name: str) -> bool:
        return any(row["name"] == name for row in registry.describe())

    def sessions(self) -> list[dict]:
        """What the viewer's session picker shows."""
        out = []
        for row in registry.describe():
            with self._guard:
                view = self._views.get(row["name"])
            out.append({**row, "mode": row.get("mode", ""),
                        "connected": bool(view and view.mode != "none"),
                        "feed": view.mode if view else "off",
                        "frame": view.frames.counter if view else 0,
                        "error": view.error if view else None})
        return out

    def frame_ready(self, view: SessionView, data: bytes) -> None:
        counter = view.frames.put(data)
        self.broadcast({"type": "frame", "session": view.name, "counter": counter})

    def frame(self, name: str) -> bytes | None:
        with self._guard:
            view = self._views.get(name)
        return view.frames.get()[0] if view else None

    def close_session(self, name: str) -> dict:
        """Stop and delete a session on the viewer's say-so.

        This is the one thing the viewer can do that changes the agent's world, so it is
        explicit (a POST to that session's own path), it is never a side effect of looking
        at something, and it reports back what is left. It is also the escape hatch for a
        session whose thread is wedged: the HTTP API stays answerable when the session's
        own lane does not, which is what made it possible to clear the stuck `default`
        session earlier tonight.
        """
        live = self.session_names()
        if name not in live:
            return {"ok": False, "session": name, "running": live,
                    "error": f"no session named {name!r} is running here"}
        with self._guard:
            view = self._views.pop(name, None)
        if view is not None:
            view.stop()
        closed = registry.close(name)
        remaining = self.session_names()
        self.broadcast({"type": "sessions", "sessions": self.sessions()})
        self.broadcast({"type": "event", "event": {"seq": 0, "run": bus.run_id,
                                                   "session": name, "kind": "closed",
                                                   "ts": time.time(),
                                                   "detail": {"by": "viewer",
                                                              "running": remaining}}})
        return {"ok": True, "session": name, "closed": closed, "running": remaining}

    def _watch_sessions(self) -> None:
        """Follow the registry while anyone is watching.

        Sessions appear and disappear under the viewer's feet, and a tab switch moves
        what a screencast is pointing at, so this re-checks both every second and
        pushes the session list with every tick. The list is small, and a viewer that
        has to poll for it would be a viewer that is wrong for a moment.
        """
        while self.viewer_count():
            try:
                for name in self.session_names():
                    with self._guard:
                        known = name in self._views
                    if not known:
                        self._start_view(name)
                for event in bus.recent()[-20:]:
                    if event.kind == "tab" and event.detail.get("action") in ("switch", "new"):
                        with self._guard:
                            view = self._views.get(event.session)
                        if view:
                            view.refresh()
                self.broadcast({"type": "sessions", "sessions": self.sessions()})
            except Exception as exc:  # noqa: BLE001
                # A viewer thread that dies takes the whole picture with it, so it
                # reports and keeps going instead of raising.
                self.last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1.0)

    # -- broadcasting ------------------------------------------------------

    def broadcast(self, message: dict) -> None:
        """Send to every viewer; a viewer that is too slow loses messages, not the server."""
        line = dict(message)
        with self._guard:
            viewers = list(self._viewers)
        for inbox in viewers:
            try:
                inbox.put_nowait(line)
            except queue.Full:
                pass

    def remember_pointer(self, message: dict) -> None:
        """Note where a session's pointer is, without telling anyone yet."""
        with self._guard:
            self._last_cursor[message["session"]] = message

    def publish_pointer(self, message: dict) -> None:
        """Remember a pointer, and tell every viewer about it."""
        self.remember_pointer(message)
        self.broadcast(message)

    def pointers(self) -> list[dict]:
        """Where every watched session's mouse is, for a viewer about to be handed it.

        Nobody moves the mouse because someone started watching, so a viewer that attaches
        after the last move - and after it has fallen out of the replay buffer - would
        otherwise show no pointer at all until the agent's next click. That is precisely the
        question a viewer is usually opened to answer, and it is why the answer is kept here
        rather than left to the event history.
        """
        with self._guard:
            return list(self._last_cursor.values())

    def on_event(self, event) -> None:
        """Mirror one agent event, plus a pointer move if it says where the pointer went."""
        self.broadcast({"type": "event", "event": event.as_dict()})
        pointer = cursor_message(event)
        if pointer:
            self.publish_pointer(pointer)

    def start(self) -> "ViewerServer":
        self._subscriber = bus.subscribe(self.on_event)
        threading.Thread(target=self._keepalive, name="view-keepalive", daemon=True).start()
        # socketserver's default poll interval is half a second, and `shutdown()` waits
        # for the loop to come round - so stopping a viewer took half a second of doing
        # nothing, every time, for no reason anybody chose.
        threading.Thread(target=self.serve_forever, args=(SHUTDOWN_POLL_S,),
                         name="view-http", daemon=True).start()
        _register(self)
        return self

    def _keepalive(self) -> None:
        """A heartbeat every few seconds, so a silently dropped viewer is noticed."""
        while True:
            time.sleep(HEARTBEAT_S)
            self.broadcast({"type": "heartbeat", "ts": time.time()})

    def shutdown(self) -> None:
        for view in list(self._views.values()):
            view.stop()
        _unregister(self.port)
        try:
            self._subscriber()
        except Exception:  # noqa: BLE001
            pass
        super().shutdown()


# The names a loopback server may legitimately be called by. Anything else in the Host
# header means the request was addressed to a *name*, and a name is what DNS rebinding
# controls - see `ViewerHandler.addressed_to_us`.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D102 - silence per-request logging
        pass

    # -- who is allowed to ask ---------------------------------------------

    def addressed_to_us(self) -> bool:
        """Was this request addressed to loopback, or to somebody's domain name?

        Sending no CORS header stops an ordinary cross-origin read, and the module
        docstring is right about why. It does not stop **DNS rebinding**: an attacker's
        page on `evil.example` whose name resolves to 127.0.0.1 is *same-origin* with
        this server as far as the browser is concerned, so the same-origin policy that
        was doing the work is gone and the page can read /events - the user's URLs,
        titles, typed values and screen frames - straight off the stream.

        The header the browser cannot forge in that attack is `Host`: it carries the
        name the page was loaded from, not the address it resolved to. So a request that
        names anything but loopback is not for us.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True                    # HTTP/1.0, or a client that sends none
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else \
            host[:host.find("]") + 1]
        return name in LOOPBACK_NAMES

    def _refuse_foreign_host(self) -> None:
        self._json({"error": "this server answers on loopback only",
                    "host": (self.headers.get("Host") or ""),
                    "hint": "open it as http://127.0.0.1:%d/ - a request addressed to a "
                            "domain name that resolves here is DNS rebinding, not you"
                            % self.server.port}, 403)

    # -- helpers -----------------------------------------------------------

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by http.server
        path, _, query = self.path.partition("?")
        try:
            if not self.addressed_to_us():
                self._refuse_foreign_host()
            elif path in ("/", "/index.html"):
                self._bytes(viewer_page().encode(), "text/html; charset=utf-8")
            elif path == "/sessions":
                self._json({"sessions": self.server.sessions()})
            elif path == "/elsewhere":
                self._json({"elsewhere": self.server.elsewhere()})
            elif path == "/events":
                self._stream()
            elif path.startswith("/frame/"):
                self._frame(path.rsplit("/", 1)[-1])
            elif path == "/health":
                self._json({"ok": True, "uptime_s": round(time.time() - self.server.started_at, 1),
                            "viewers": self.server.viewer_count(),
                            "sessions": self.server.session_names()})
            else:
                self._json({"error": "not found", "path": path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass                     # the viewer went away mid-answer; nothing to report

    def _frame(self, name: str) -> None:
        body = self.server.frame(name)
        if body is None:
            if not self.server.has_session(name):
                # Verbose on purpose: a bare 404 leaves the reader guessing whether the
                # session was closed, mistyped, or lives in another server process.
                self._json({"error": f"no session named {name!r} is running here",
                            "running": self.server.session_names(),
                            "hint": "it may have been closed, or the agent may be driving "
                                    "another latchkey server"}, 404)
                return
            self.send_response(204)   # a session exists, its first frame does not yet
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self._bytes(body, "image/jpeg")

    def do_POST(self) -> None:  # noqa: N802 - the name is fixed by http.server
        path, _, _query = self.path.partition("?")
        try:
            if not self.addressed_to_us():
                self._refuse_foreign_host()
            elif path.startswith("/session/") and path.endswith("/close"):
                name = unquote(path[len("/session/"):-len("/close")].strip("/"))
                result = self.server.close_session(name)
                self._json(result, 200 if result.get("ok") else 404)
            else:
                self._json({"error": "not found", "path": path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream(self) -> None:
        """One SSE stream per viewer: the handshake, the history, then live."""
        inbox = self.server.add_viewer()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._send({"type": "hello", "viewer": self.server.viewer_count(),
                        "run": bus.run_id})
            self._send({"type": "sessions", "sessions": self.server.sessions()})
            for event in bus.recent()[-HISTORY:]:
                self._send({"type": "event", "event": event.as_dict()})
                pointer = cursor_message(event)
                if pointer:
                    self.server.remember_pointer(pointer)
                    self._send(pointer)   # so the pointer starts where the agent left it
            # ...and when that move is older than the buffer, the remembered position is
            # still where the mouse is. Nobody moves the mouse because someone started
            # watching, so a panel opened mid-session would otherwise show no pointer at all
            # until the next click - the one thing a viewer is opened to find out.
            for pointer in self.server.pointers():
                self._send(pointer)
            while True:
                try:
                    message = inbox.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # also detects a dead socket
                    self.wfile.flush()
                    continue
                self._send(message)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            self.server.remove_viewer(inbox)

    def _send(self, message: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(message, default=str).encode() + b"\n\n")
        self.wfile.flush()


_servers: dict[int, ViewerServer] = {}
_servers_lock = threading.Lock()
# The in-process half of the viewer registry's write lock; the other half is the file lock in
# `_one_writer_at_a_time`. Two threads of one server must not interleave a read-modify-write
# of that file any more than two servers may.
_registry_lock = threading.Lock()


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process, but it exists
    except OSError:
        return False
    return True


def _read_registry() -> list[dict]:
    try:
        with open(VIEWERS_FILE, encoding="utf-8") as fh:
            entries = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _write_registry(entries: list[dict]) -> None:
    """Write the registry atomically: a reader must never see half a file."""
    try:
        os.makedirs(os.path.dirname(VIEWERS_FILE), exist_ok=True)
        tmp = f"{VIEWERS_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        os.replace(tmp, VIEWERS_FILE)
    except OSError:
        pass            # a viewer that cannot write this still works; clients can scan


def _health_of(host: str, port: int, timeout: float = 0.4) -> dict | None:
    """`GET /health` on another viewer, or None if nothing usable answers there.

    Short timeout on purpose: this runs while a human waits for a page to say something,
    and a viewer that is wedged, or a port that is something else entirely, must read as
    "no" rather than as a hang.
    """
    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request("GET", "/health")
            body = connection.getresponse().read()
        finally:
            connection.close()
        report = json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return report if isinstance(report, dict) and report.get("ok") else None


def _live_entries() -> list[dict]:
    """The registry, pruned of dead viewers, newest first. The caller holds the lock."""
    raw = _read_registry()
    entries = [e for e in raw if _pid_alive(e.get("pid"))]
    if len(entries) != len(raw):
        _write_registry(entries)
    return sorted(entries, key=lambda e: -float(e.get("started_at") or 0))


@contextlib.contextmanager
def _one_writer_at_a_time():
    """Serialise the read-modify-write of the viewer registry, across processes.

    Every viewer start and stop reads this one file, changes it and writes it back. Two
    servers coming up together - which is exactly what two models do - interleave that, and
    the loser's window vanishes from the registry, so a client looking for its window finds
    the *other* connection's, or nothing. The lock is a sibling file: readers of the registry
    itself are undisturbed.
    """
    with _registry_lock:
        try:
            os.makedirs(os.path.dirname(VIEWERS_FILE), exist_ok=True)
            handle = open(f"{VIEWERS_FILE}.lock", "a+")
        except OSError:
            yield                   # no lock file: the old unlocked behaviour, no worse
            return
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def known_viewers() -> list[dict]:
    """Every viewer still running, dead pids pruned. Newest first.

    This is how a client finds the live viewer instead of guessing: each viewer writes
    itself down as it starts and is pruned once its process is gone. Pruning is a write, so
    it happens under the same lock as registering - a read that quietly rewrote the file is
    how another server's live window got dropped from the registry.
    """
    with _one_writer_at_a_time():
        return _live_entries()


def _register(server: "ViewerServer") -> None:
    """Add this viewer, without disturbing any other server's entry.

    The port is dropped only for *this* process. Another server's viewer on the same port
    (the same LATCHKEY_VIEWER_PORT is handed to every connection, and its bind rolls forward)
    is a window a model may be looking at, and it is not this process's to unlist.
    """
    with _one_writer_at_a_time():
        entries = [e for e in _live_entries()
                   if not (e.get("port") == server.port and e.get("pid") == os.getpid())]
        entries.append({"port": server.port, "pid": os.getpid(), "host": server.host,
                        "url": server.url(), "owner": paths.owner_id(),
                        "started_at": time.time()})
        _write_registry(entries)


def _unregister(port: int) -> None:
    with _one_writer_at_a_time():
        _write_registry([e for e in _live_entries()
                         if not (e.get("port") == port and e.get("pid") == os.getpid())])


def start_viewer(port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                 span: int = VIEWER_PORT_SPAN) -> ViewerServer:
    """Start the viewer server, or hand back the one already on that port.

    One per port per process: the MCP server may be asked to start it twice (a tool
    call and an environment variable, say), and two servers on one port is not a
    thing a user can debug.

    The port is a preference, not a requirement. Every MCP connection is started with
    the same `LATCHKEY_VIEWER_PORT`, so the *second* server process to come up asks for
    a port the first one already has - and the first one is rarely the one holding the
    sessions. Failing there is not a visible failure, which is the problem: that process
    then serves no viewer at all, while the viewer that does listen belongs to some other
    connection and answers every client with `[]`. The human gets a window that is
    connected, live, and permanently empty next to an agent that is plainly working.
    So a port that is taken rolls forward within `span` ports - 8788..8798 by default,
    the range every client scans - and if it moves, the caller is told which port it
    actually got by looking at `server.port`.
    """
    with _servers_lock:
        existing = _servers.get(port)
        if existing is not None:
            return existing
        failure: OSError | None = None
        for candidate in range(port, port + max(1, span)):
            existing = _servers.get(candidate)
            if existing is not None:      # this process already listens there
                return existing
            try:
                server = ViewerServer(port=candidate, host=host).start()
            except OSError as exc:        # someone else has it; the next one may be free
                failure = exc
                continue
            # Registered under the port that was *asked* for, so a second call for 8788
            # gets this server back and `stop_viewer(8788)` stops it, wherever it landed.
            _servers[port] = server
            return server
        raise failure if failure is not None else OSError(
            f"no free port in {port}..{port + max(1, span) - 1}")


def stop_viewer(port: int = DEFAULT_PORT) -> None:
    with _servers_lock:
        server = _servers.pop(port, None)
        if server is None:
            # The requested port may have been taken and the viewer rolled forward; the
            # call still means "stop the viewer this process started", whichever port it
            # ended up on.
            for asked, candidate in list(_servers.items()):
                if candidate.port == port:
                    server = _servers.pop(asked)
                    break
    if server is not None:
        server.shutdown()


def main(argv: list[str] | None = None) -> int:
    """`python3 -m latchkey watch`: serve the sessions that live in *this* process."""
    import argparse

    parser = argparse.ArgumentParser(description="Watch latchkey sessions in a browser.")
    parser.add_argument("--port", type=int, default=int(
        __import__("os").environ.get("LATCHKEY_VIEWER_PORT", DEFAULT_PORT)))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    server = start_viewer(args.port, host=args.host)
    print(f"latchkey viewer on {server.url()}  (ctrl-c to stop)")
    if server.port != args.port:
        print(f"port {args.port} was already taken, so this viewer moved to {server.port}")
    print("sessions are discovered as they open; nothing is shown until one does")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
