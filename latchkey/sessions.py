"""Named, isolated browser sessions for parallel agents.

Playwright's sync API belongs to the thread that created it - one Browser cannot
be driven from several threads, and a single shared browser also means shared
cookies, shared tabs and shared crashes. So parallel agents need real separation,
not a lock.

Each Session owns a thread, its own Chrome process and its own tab set. Work is
submitted to that thread and answered synchronously, which keeps the straightforward
sync API everywhere else in the codebase while giving genuine isolation.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .events import bus

DEFAULT_NAME = "default"


class SessionError(RuntimeError):
    """Raised for a session that failed to start, died, or timed out."""


class Session:
    """One Browser, pinned to its own thread."""

    def __init__(self, name: str, factory: Callable[[], Any],
                 startup_timeout: float = 180.0) -> None:
        self.name = name
        self._factory = factory
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        # Set when the caller has stopped waiting for this session to start. A browser
        # that finishes launching after that belongs to nobody, and a Chrome nobody owns
        # is a Chrome nobody closes - so the worker checks this and hangs up instead.
        self._abandoned = threading.Event()
        # Serialises a submit's (is-closed? then enqueue) against the worker marking itself
        # closed, so a call that is accepted is always either run or failed, never orphaned.
        self._state_lock = threading.Lock()
        self._startup_error: BaseException | None = None
        self._label = ""
        self._created = time.time()
        self._used = time.time()       # when work last ran on this session; drives idle reaping
        self._inflight = 0             # calls running right now (0 = the browser is sitting idle)
        self._timed_out = 0            # calls whose caller gave up while they ran
        self._thread = threading.Thread(target=self._run, name=f"latchkey-{name}",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(startup_timeout):
            self._abandoned.set()
            raise SessionError(
                f"session {name!r} did not finish starting within {startup_timeout:.0f}s; "
                f"the browser it was launching will be closed when it finishes starting.")
        if self._startup_error is not None:
            raise SessionError(f"session {name!r} failed to start: {self._startup_error}")
        bus.publish(name, "session", action="open", label=self._label)

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        browser = None
        try:
            browser = self._factory()
            # Events are keyed by the session's *name*: the label is a description a
            # caller chose ("Offer Scout read-only audit"), the name is what the viewer's
            # tabs, `latchkey_sessions` and the close endpoint all go by. Publishing under
            # the label put a labelled session's goto/click events under a key nothing
            # else knew, so its tab showed no activity and no pointer.
            try:
                browser.session_name = self.name
            except Exception:  # noqa: BLE001
                pass
        except BaseException as exc:  # noqa: BLE001
            self._startup_error = exc
            self._ready.set()
            return
        self._label = getattr(browser, "label", "") or ""
        self._ready.set()
        try:
            while True:
                if self._abandoned.is_set():
                    break            # nobody is holding this session; do not keep a Chrome
                item = self._queue.get()
                if item is None:
                    break
                fn, box = item
                self._inflight += 1
                try:
                    box["result"] = fn(browser)
                except BaseException as exc:  # noqa: BLE001
                    box["error"] = exc
                finally:
                    self._inflight -= 1
                    self._used = time.time()
                    box["done"].set()
        finally:
            with self._state_lock:
                self._closed.set()        # no submit can enqueue past this point
            # A call enqueued just before the close (a submit that raced the reaper, or an
            # explicit close) would otherwise wait out its whole timeout for a worker that has
            # gone. Fail those boxes now, so the caller gets a clear "closed" at once and can
            # re-open, instead of a misleading "did not answer" minutes later.
            self._drain_pending()
            try:
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass

    def _drain_pending(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            _fn, box = item
            box["error"] = SessionError(f"session {self.name!r} was closed before this call ran; "
                                        f"open it again with latchkey_session_open.")
            box["done"].set()

    # -- api ---------------------------------------------------------------

    def submit(self, fn: Callable[[Any], Any], timeout: float = 300.0) -> Any:
        """Run `fn(browser)` on this session's thread and return its result."""
        box: dict[str, Any] = {"done": threading.Event(), "result": None, "error": None}
        with self._state_lock:
            # Check-and-enqueue under the lock the worker takes to mark itself closed, so a
            # call is never accepted onto a queue nobody will drain.
            if self._closed.is_set():
                raise SessionError(f"session {self.name!r} is closed")
            self._used = time.time()   # queued now counts as used, so a slow call is not reaped
            self._queue.put((fn, box))
        if not box["done"].wait(timeout):
            self._timed_out += 1
            raise SessionError(
                f"session {self.name!r} did not answer within {timeout:.0f}s; it is still "
                f"working on that call, and everything sent to it now queues behind it. "
                f"Close it with latchkey_session_close (which takes it back even while it "
                f"is busy) and open it again."
                + (f" {self._timed_out} calls have timed out on this session."
                   if self._timed_out > 1 else ""))
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    def close(self, timeout: float = 30.0) -> bool:
        if self._closed.is_set():
            return True
        self._queue.put(None)
        finished = self._closed.wait(timeout)
        bus.publish(self.name, "session", action="close", clean=finished)
        return finished

    @property
    def alive(self) -> bool:
        return not self._closed.is_set() and self._thread.is_alive()

    @property
    def busy(self) -> bool:
        """Is a call running or queued? A busy session is never idle, whatever the clock says."""
        return self._inflight > 0 or not self._queue.empty()

    @property
    def idle_s(self) -> float:
        """Seconds since work last ran or was queued. 0 while busy."""
        return 0.0 if self.busy else round(time.time() - self._used, 1)

    def describe(self) -> dict:
        out = {"name": self.name, "alive": self.alive, "label": self._label,
               "age_s": round(time.time() - self._created, 1),
               "idle_s": self.idle_s, "busy": self.busy,
               "queued": self._queue.qsize()}
        if self._timed_out:
            out["timed_out_calls"] = self._timed_out
        return out


class SessionRegistry:
    """Name -> Session. Thread-safe; the MCP server calls into it concurrently."""

    def __init__(self, factory: Callable[..., Any] | None = None) -> None:
        self._factory = factory or default_factory
        self._sessions: dict[str, Session] = {}
        self._specs: dict[str, Any] = {}
        self._gates: dict[str, threading.Lock] = {}   # one per name; see `get`
        self._lock = threading.RLock()

    def get(self, name: str = DEFAULT_NAME, *, spec: Any = None,
            recreate: bool = False, startup_timeout: float = 180.0) -> Session:
        """The named session, starting it if it is not running.

        `spec` is a SessionSpec, so the registry can start exactly the browser
        that was asked for - mode, profiles, cursor and all. With
        `recreate=True` and no spec, the session's own spec is reused, which is
        how "restart on a profile clone" keeps the rest of the session's shape.

        The registry lock covers the dictionaries and nothing else. Closing a browser
        takes up to thirty seconds and starting one takes several, and holding the lock
        across either meant one wedged session blocked `require()` for every *other*
        session - a registry-wide stall caused by one name. Names are serialised against
        themselves by a gate of their own instead.
        """
        with self._lock:
            existing = self._sessions.get(name)
            if existing is not None and existing.alive and not recreate:
                return existing
            gate = self._gates.setdefault(name, threading.Lock())
        with gate:
            with self._lock:
                existing = self._sessions.get(name)
                if existing is not None and existing.alive and not recreate:
                    return existing        # another caller started it while we queued
                if spec is None:
                    spec = self._specs.get(name)
                self._sessions.pop(name, None)
            if existing is not None:
                existing.close()           # slow, and none of the other sessions' business
            session = Session(name, lambda: self._factory(spec),
                              startup_timeout=startup_timeout)
            with self._lock:
                self._sessions[name] = session
                self._specs[name] = spec
            return session

    def spec_of(self, name: str) -> Any:
        """The spec a session was started with, so it can be restarted in shape."""
        with self._lock:
            return self._specs.get(name)

    def get_existing(self, name: str = DEFAULT_NAME) -> Session | None:
        with self._lock:
            session = self._sessions.get(name)
            return session if session and session.alive else None

    def require(self, name: str = DEFAULT_NAME) -> Session:
        """For tools: use the named session, or say clearly that it does not exist.

        Deliberately does not auto-create. A typo in a session name should be an
        error, not a second silent browser.
        """
        session = self.get_existing(name)
        if session is None:
            known = ", ".join(self.names()) or "none"
            raise SessionError(
                f"no open session named {name!r} (open: {known}). Call "
                f"latchkey_session_open first.")
        return session

    def names(self) -> list[str]:
        with self._lock:
            return [n for n, s in self._sessions.items() if s.alive]

    def close(self, name: str) -> bool:
        with self._lock:
            session = self._sessions.pop(name, None)
            self._specs.pop(name, None)
        return session.close() if session else False

    def close_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._specs.clear()
        return sum(1 for s in sessions if s.close())

    def describe(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.describe() for s in sessions]

    def idle_names(self, ttl_s: float, protected: set[str] | None = None) -> list[str]:
        """Sessions that have sat idle longer than `ttl_s`, safe to close.

        A session that is busy - a call running or queued - is never idle whatever the clock
        says, and a `protected` name (one in the middle of a human intervention, say) is left
        alone even when it looks idle, because the work holding it lives off this thread.
        """
        if ttl_s <= 0:
            return []
        guard = protected or set()
        with self._lock:
            sessions = list(self._sessions.items())
        return [name for name, session in sessions
                if name not in guard and session.alive
                and not session.busy and session.idle_s >= ttl_s]

    def reap_idle(self, ttl_s: float, protected: set[str] | None = None) -> list[str]:
        """Close every idle session and its Chrome. Returns the names closed.

        This is how a browser that nobody is using stops holding memory: closing the session
        closes the Chrome process it started. Ordering with `get` is the registry's own gates'
        job - a name that a caller re-opens the instant it is reaped simply starts again.
        """
        closed = []
        for name in self.idle_names(ttl_s, protected):
            # Re-check under close: a call could have arrived since the list was taken.
            session = self.get_existing(name)
            if session is None or session.busy:
                continue
            if self.close(name):
                closed.append(name)
        return closed


def default_factory(spec: Any = None):
    """Create and start a Browser. Imported lazily to avoid a cycle."""
    from .session import Browser
    return Browser(None, spec=spec).start()


registry = SessionRegistry()
