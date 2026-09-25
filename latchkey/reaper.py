"""Close browsers nobody is using, so an idle session stops holding memory.

A latchkey session is a real Chrome process. It is cheap to keep one around for the next
call, and not free: each idle browser is a few hundred megabytes sitting there for a task
that finished ten minutes ago. An agent opens a session per site and rarely closes them, so
without this they accumulate until the machine feels it.

So a background thread watches the clock. A session that has run nothing and had nothing
queued for `LATCHKEY_IDLE_TTL` seconds is closed - which closes its Chrome, and the memory
goes with it. Nothing is lost that a re-open would not restore: a session's cookies come
from the profile, and a reaped name simply starts again the next time it is used.

Two things are never reaped. A **busy** session - a call running or waiting - is not idle
whatever the clock says (a `wait_for_login` can sit for minutes with the browser genuinely
in use). And a session in the middle of a **human intervention** is protected explicitly,
because the window driving it lives on its own thread and the session itself can look quiet
while a person is signing in through it.

The same tick also sweeps the throwaway profiles a closed intervention window leaves behind,
reclaims the headless Chrome of any latchkey server that died without closing it, and removes
the clone directories left by servers that have exited - each of those is a whole browser
profile, and every server has one of its own now (so that two models never want one window).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from . import chrome as chrome_mod
from . import intervene
from . import paths
from .events import bus
from .sessions import registry as default_registry

# Off with 0 / off; otherwise seconds a session may sit idle before it is closed. Fifteen
# minutes by default: long enough that a user coming back to a task does not pay a re-open,
# short enough that a forgotten session is not still resident an hour later.
TTL_ENV = "LATCHKEY_IDLE_TTL"
DEFAULT_TTL_S = 900.0
FALSEY = ("0", "off", "no", "false", "never", "disabled")


def ttl_s() -> float:
    """The idle time-to-live in force, from the environment. 0 disables reaping."""
    raw = (os.environ.get(TTL_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_TTL_S
    if raw in FALSEY:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TTL_S


class IdleReaper:
    """A daemon that closes idle sessions on a timer and tidies up after interventions."""

    def __init__(self, registry=default_registry, manager: "intervene.InterventionManager" = None,
                 *, ttl: Callable[[], float] = ttl_s, interval_s: float | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._registry = registry
        self._manager = manager or intervene.manager
        self._ttl = ttl
        self._sleep = sleep
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def interval(self) -> float:
        """How often to look. A quarter of the TTL, bounded, so the check is cheap and timely."""
        if self._interval is not None:
            return self._interval
        ttl = self._ttl()
        if ttl <= 0:
            return 60.0
        return max(15.0, min(120.0, ttl / 4))

    def tick(self) -> dict:
        """One pass: close idle sessions, sweep orphan profiles. Returns what it did."""
        result: dict = {}
        ttl = self._ttl()
        if ttl > 0:
            try:
                protected = self._manager.protected_sessions()
                reaped = self._registry.reap_idle(ttl, protected)
            except Exception:  # noqa: BLE001 - a reaper that throws must not take the server down
                reaped = []
            if reaped:
                result["reaped"] = reaped
                for name in reaped:
                    bus.publish(name, "session", action="reaped", idle_ttl_s=ttl)
        try:
            swept = intervene.sweep_orphans()
        except Exception:  # noqa: BLE001
            swept = 0
        if swept:
            result["swept_profiles"] = swept
        try:
            pruned = paths.prune_clones()
        except Exception:  # noqa: BLE001
            pruned = 0
        if pruned:
            result["pruned_clones"] = pruned
        return result

    def _run(self) -> None:
        # Sweep once at start: a previous server may have left windows' profiles and clones
        # behind - they are the same size as a browser profile, so they do not wait for the
        # first interval.
        try:
            intervene.sweep_orphans()
            paths.prune_clones()
        except Exception:  # noqa: BLE001
            pass
        while not self._stop.is_set():
            waited = 0.0
            step = min(1.0, self.interval())
            while waited < self.interval() and not self._stop.is_set():
                self._sleep(step)
                waited += step
            if self._stop.is_set():
                break
            self.tick()

    def start(self) -> "IdleReaper":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="latchkey-reaper", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
