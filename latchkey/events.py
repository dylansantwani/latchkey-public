"""Activity events and the on-page cursor.

A viewer needs two things from a running session: what just happened, and where
the pointer is. Both live here.

Events are published by Browser as it acts. Subscribers are plain callables, so
the viewer server is just one subscriber and a test can be another. A bounded
history means a viewer connecting late sees recent activity rather than a blank
screen, which matters because the viewer is usually started after the agent.
"""
from __future__ import annotations

import itertools
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

MAX_STRING = 400


@dataclass(frozen=True)
class Event:
    seq: int
    session: str
    # goto | back | forward | reload | wait_for | click | hover | fill | press |
    # select | check | scroll | upload | screenshot | eval | tab | sync |
    # save_session | login | session
    kind: str
    ts: float
    # A verb that points at an element also reports the pointer's x/y, so a viewer
    # knows where the agent is even between frames. Nothing secret is ever put in
    # here: a value typed into a credential field is reported as "(hidden)".
    detail: dict
    # Sequence numbers restart with each server process.  Carry the run namespace so a
    # viewer that reconnects to a restarted server cannot discard its new event as old.
    run: str = ""

    def as_dict(self) -> dict:
        return {"seq": self.seq, "session": self.session, "kind": self.kind,
                "ts": self.ts, "detail": self.detail, "run": self.run}


def _trim(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_STRING]


class EventBus:
    """Thread-safe fan-out with a bounded replay buffer."""

    def __init__(self, history: int = 300) -> None:
        self.run_id = uuid.uuid4().hex
        self._subs: list[Callable[[Event], None]] = []
        self._history: deque[Event] = deque(maxlen=history)
        self._lock = threading.RLock()
        self._seq = itertools.count(1)

    def subscribe(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)
        return unsubscribe

    def publish(self, session: str, kind: str, **detail: Any) -> Event:
        # The viewer turns a page URL into a link.  Trimming it creates a convincing but
        # broken destination, so preserve the actual address while still bounding every
        # other arbitrary detail field.
        safe_detail = {k: (v if k == "url" and isinstance(v, str) else _trim(v))
                       for k, v in detail.items()}
        event = Event(next(self._seq), session, kind, time.time(),
                      safe_detail, self.run_id)
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subs)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                pass          # a broken viewer must never break the agent
        return event

    def recent(self, limit: int = 200, session: str | None = None) -> list[Event]:
        with self._lock:
            items = list(self._history)
        if session:
            items = [e for e in items if e.session == session]
        return items[-limit:]

    def clear(self) -> int:
        """Forget the replay buffer, and say how much was forgotten.

        The buffer is what a viewer sees the moment it connects, so a test that wants
        a stream of its own needs a bus the test before it has not already used.
        """
        with self._lock:
            count = len(self._history)
            self._history.clear()
            self._seq = itertools.count(1)
            return count

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


bus = EventBus()


# -- on-page cursor ----------------------------------------------------------
#
# Playwright's mouse is invisible: a click happens with no pointer drawn. To show
# where an agent is, install a real element in the page and move it. Injected via
# an init script so it survives navigation, and only when a viewer is attached -
# adding nodes to every page by default would be an unwanted side effect on the
# sites being driven.

CURSOR_INIT_SCRIPT = r"""
(() => {
  // DOM only, on purpose: no global, no closure the page could find. The dot is an
  // element with an id, and every later move finds it by that id from latchkey's own
  // isolated world - so nothing here has to be reachable from the main world.
  if (document.getElementById('__latchkey_cursor')) return;
  const dot = document.createElement('div');
  dot.id = '__latchkey_cursor';
  dot.style.cssText = 'position:fixed;left:0;top:0;width:16px;height:16px;margin:-8px 0 0 -8px;'
    + 'border-radius:50%;background:rgba(239,68,68,.9);border:2px solid #fff;'
    + 'box-shadow:0 2px 10px rgba(0,0,0,.5);pointer-events:none;z-index:2147483647;'
    + 'transition:transform .12s ease-out;will-change:transform';
  (document.body || document.documentElement).appendChild(dot);
})();
"""

# Moves the dot (creating it if a navigation took it away) and draws a click ripple.
# Runs from the isolated world: only the DOM is shared, and only the DOM is used.
MOVE_CURSOR_JS = r"""(p) => {
  const [x, y, click] = p;
  let dot = document.getElementById('__latchkey_cursor');
  if (!dot) {
    dot = document.createElement('div');
    dot.id = '__latchkey_cursor';
    dot.style.cssText = 'position:fixed;left:0;top:0;width:16px;height:16px;margin:-8px 0 0 -8px;'
      + 'border-radius:50%;background:rgba(239,68,68,.9);border:2px solid #fff;'
      + 'box-shadow:0 2px 10px rgba(0,0,0,.5);pointer-events:none;z-index:2147483647;'
      + 'transition:transform .12s ease-out;will-change:transform';
    (document.body || document.documentElement).appendChild(dot);
  }
  dot.style.transform = 'translate(' + x + 'px,' + y + 'px)';
  if (click) {
    const r = document.createElement('div');
    r.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483646;border-radius:50%;'
      + 'border:2px solid rgba(239,68,68,.9);width:12px;height:12px;'
      + 'transform:translate(' + (x - 6) + 'px,' + (y - 6) + 'px);'
      + 'transition:all .45s ease-out;opacity:.9';
    (document.body || document.documentElement).appendChild(r);
    requestAnimationFrame(() => {
      r.style.width = '56px'; r.style.height = '56px'; r.style.opacity = '0';
      r.style.transform = 'translate(' + (x - 28) + 'px,' + (y - 28) + 'px)';
    });
    setTimeout(() => r.remove(), 500);
  }
}"""

REMOVE_CURSOR_JS = """() => { const d = document.getElementById('__latchkey_cursor');
  if (d) d.remove(); }"""


def move_cursor(run: Any, x: float, y: float, click: bool = False) -> bool:
    """Position the on-page cursor. Never raises - cosmetics must not break a task.

    `run` is `Driver.run_js` (or a page, whose `evaluate` is used): the move is made from
    latchkey's own world, since the script only touches the DOM.
    """
    call = run if callable(run) else getattr(run, "evaluate", None)
    if call is None:
        return False
    try:
        call(MOVE_CURSOR_JS, [float(x), float(y), bool(click)])
        return True
    except Exception:  # noqa: BLE001
        return False
