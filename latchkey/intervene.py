"""Hand the human a real window when an agent cannot pass a wall by itself.

Some pages an agent cannot clear from inside a headless browser: a sign-in that wants a
password the agent must never see, a "press & hold", a Turnstile checkbox, a reCAPTCHA
image grid. `detect.py` already tells the agent *that* a page is `challenged` or `blocked`;
this module is what happens next. It is deliberately in two halves, because the whole point
is that a browser carrying the user's cookies is never opened without the user's say-so:

  1. **Ask.** `request()` records a pending intervention and returns what the agent should
     tell the user - which site, which wall, what a window will do. Nothing is opened.
  2. **Act.** Once the user says yes (the agent calls back with consent), `start()` opens a
     *headed* Chrome the user can see and touch, seeded over CDP with the session's own
     cookies for that site, at the session's own user agent and window size. The user solves
     the check in a real browser; latchkey harvests the cookies it earned and hands them
     back to the headless session, which carries on.

Why a separate window and not the session's own browser: Chrome allows one process per
profile, and the agent's session is already driving one. A throwaway profile, seeded with
the site's cookies and shredded afterwards, lets the session stay alive and paused rather
than be torn down and rebuilt - and it presents the *same* client (same user agent, same
size, same machine, so the same IP) the site issued its cookies to, which is exactly what
keeps a `cf_clearance` the human earns valid when it comes back to the headless session.

Why not just drive the challenge for them: a bot that solves an anti-bot wall is the thing
the wall exists to stop, and a sign-in is a password latchkey is built never to touch. The
human does the part that is theirs; latchkey does the cookie plumbing around it.

The window runs on a thread of its own with its own Playwright, so nothing here ever touches
the session's browser from the wrong thread. The two moments it does need the session - read
its cookies to seed the window, write the earned cookies back - happen on the session's own
thread, and only plain data (cookie dicts, a URL) crosses between them.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from . import chrome as chrome_mod
from . import paths
from .detect import PageProbe, short_url
from .events import bus

# Where the throwaway profiles a human window runs on live. One directory per
# intervention, seeded with the site's cookies and removed when the window closes -
# it holds a copy of the user's cookies for that site, so it is never left behind.
HANDOFF_ROOT = paths.path("handoff")

# How long a pending consent stays open before it is forgotten. The agent asks the user,
# the user answers; if that has not happened in a few minutes the request is stale, and a
# stale pending intervention should not be sitting around holding a plan to open a window.
CONSENT_TTL_S = 300

# How long a window waits for the human, unless the caller asks for more or less. A person
# solving a captcha or signing in takes a minute or two; five is generous without being a
# window left open all afternoon because nobody closed it.
DEFAULT_TIMEOUT_S = 300

# The states an intervention moves through, most also carried back to the agent as `status`.
PENDING = "pending"       # asked for, waiting on the user's yes
OPEN = "open"             # the window is up and the human is working
SOLVED = "solved"         # the wall cleared (or the human finished) and cookies came back
FAILED = "failed"         # the window closed unsolved, timed out, or errored
DECLINED = "declined"     # the user said no
CANCELLED = "cancelled"   # the intervention was cancelled (session closed, explicit cancel)

TERMINAL = frozenset({SOLVED, FAILED, DECLINED, CANCELLED})

TRUTHY = ("1", "true", "yes", "on")


def enabled() -> bool:
    """Is the assist subsystem on? On by default; LATCHKEY_ASSIST=off turns it off.

    A human running the server without anyone at the screen - a cron job, a container -
    can switch off the whole idea of "open a window for a person", so the tool refuses
    cleanly instead of opening a Chrome nobody will ever look at.
    """
    raw = (os.environ.get("LATCHKEY_ASSIST") or "").strip().lower()
    return raw not in ("0", "false", "no", "off", "disabled")


# -- what stopped us, in a sentence for the user -----------------------------

REASONS = {
    "challenge": "a human-verification check (a captcha or \"press & hold\")",
    "block": "an anti-bot block",
    "login": "a sign-in the agent cannot complete for you",
    "manual": "a step that needs you",
}


def reason_for(verdict: str, wall: dict | None) -> str:
    """The kind of intervention a page needs, from its verdict and who walls it."""
    kind = str((wall or {}).get("kind") or "")
    if verdict == "challenged" or kind in ("challenge", "queue"):
        return "challenge"
    if verdict == "blocked" or kind in ("block", "rate_limit"):
        return "block"
    if verdict == "logged-out":
        return "login"
    return "manual"


def _host(url: str) -> str:
    return urlparse(url or "").netloc.split(":")[0].lower()


def _cookie_domain(cookie: dict) -> str:
    """The host a cookie is scoped to, from its `domain` or the URL it was set with."""
    domain = str(cookie.get("domain") or "").strip().lstrip(".").lower()
    return domain or _host(str(cookie.get("url") or ""))


def _applies_to(cookie: dict, host: str) -> bool:
    """Would this cookie be sent to `host`? The standard cookie-domain match, nothing more.

    A cookie on `.example.com` (or `example.com`) applies to `example.com` and every
    subdomain; a cookie on `foo.example.com` does not apply to `bar.example.com`. This is
    exactly the rule Chrome uses to decide whether to send a cookie, which is what makes it
    the right, and leak-free, test for "does this belong to the site being walled": it can
    never match an unrelated site that merely shares a public suffix (`bar.github.io` for a
    wall on `foo.github.io`), because Chrome refuses to set a cookie on a public suffix in
    the first place, so no `.github.io` cookie exists in the jar to over-match.
    """
    domain = _cookie_domain(cookie)
    return bool(domain) and (host == domain or host.endswith("." + domain))


def scope_cookies(cookies: list[dict], url: str) -> list[dict]:
    """The cookies worth handing a window for a wall on `url`: exactly the site's own.

    Not the user's whole jar. The window is a real browser carrying the user's cookies, so
    it gets exactly the cookies the site in front of it would be sent - the ones that make
    it recognise the client, `cf_clearance` and the rest of a first-party clearance included
    - and nothing from any other site they are signed in to.
    """
    host = _host(url)
    if not host:
        return list(cookies)
    return [c for c in cookies if _applies_to(c, host)]


# -- the window itself (the part that talks to a real browser) ---------------
#
# Kept behind a tiny interface so the state machine can be tested without a browser: a fake
# window drives the same `_run` loop a real one does. The real window is a headed Chrome on
# a throwaway profile, attached to over CDP - the same attach `session.py` uses for its own
# dedicated and clone modes.

class Window:
    """A headed Chrome a human is using, seeded with a site's cookies and read back over CDP."""

    def __init__(self, launched: "chrome_mod.Launched", pw: Any, browser: Any,
                 page: Any, cdp: Any, profile_dir: str) -> None:
        self._launched = launched
        self._pw = pw
        self._browser = browser
        self._page = page
        self._cdp = cdp
        self._profile_dir = profile_dir
        self._probe = PageProbe()
        self._epoch = 0

    @property
    def pid(self) -> int:
        return self._launched.pid

    def seed(self, cookies: list[dict]) -> int:
        """Put the site's cookies into the window before the human's first request."""
        if not cookies:
            return 0
        try:
            self._cdp.send("Network.setCookies", {"cookies": cookies})
            return len(cookies)
        except Exception:  # noqa: BLE001 - one bad cookie must not sink the seed
            done = 0
            for cookie in cookies:
                try:
                    self._cdp.send("Network.setCookie", cookie)
                    done += 1
                except Exception:  # noqa: BLE001
                    continue
            return done

    def open_url(self, url: str) -> None:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:  # noqa: BLE001 - a challenge page can be slow or odd; the human still sees it
            pass
        try:
            self._page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

    def alive(self) -> bool:
        try:
            return bool(self._browser.is_connected()) and len(self._browser.contexts) > 0
        except Exception:  # noqa: BLE001
            return False

    def cookies(self) -> list[dict] | None:
        """Every cookie the window holds now, in setCookies shape, or None if it is gone."""
        try:
            raw = self._cdp.send("Network.getAllCookies").get("cookies", [])
        except Exception:  # noqa: BLE001
            return None
        out = []
        for c in raw:
            entry = {k: c[k] for k in ("name", "value", "domain", "path", "secure",
                                       "httpOnly", "expires") if k in c and c[k] is not None}
            if c.get("sameSite"):
                entry["sameSite"] = c["sameSite"]
            if c.get("partitionKey"):
                entry["partitionKey"] = c["partitionKey"]
            out.append(entry)
        return out

    def solved(self) -> bool | None:
        """Has the wall cleared? True/False, or None when the page cannot be read yet."""
        self._epoch += 1
        try:
            probe = self._probe.read(self._page, self._page.url, self._epoch)
        except Exception:  # noqa: BLE001
            return None
        if probe is None:
            return None
        return not (probe.blocked or probe.challenged or probe.markers)

    def close(self) -> None:
        for step in (self._browser.close if self._browser else None,
                     self._pw.stop if self._pw else None,
                     lambda: self._launched.close(timeout_s=6.0)):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        try:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def open_window(profile_dir: str, url: str, *, user_agent: str, width: int, height: int,
                launcher: Callable[..., Any] = chrome_mod.launch_for_automation) -> Window:
    """Launch a headed Chrome on a throwaway profile and attach to it over CDP.

    Headed (`headless=False`), so the human can see and drive it. At the session's own user
    agent and window size, so it is the same client the site's cookies were issued to.
    `launch_for_automation` starts Chrome with a short, explicit flag list - no
    `--enable-automation`, no `AutomationControlled` switch - so the browser reads as a
    normal one to a challenge, which is what lets the human's solve stick.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(profile_dir, mode=0o700, exist_ok=True)
    args = [f"--user-agent={user_agent}", f"--window-size={int(width)},{int(height)}"]
    launched = launcher(profile_dir, headless=False, args=args)
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(launched.endpoint)
        if not browser.contexts:
            raise RuntimeError("the window opened with no browser context")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages
                     if not p.url.startswith(("devtools://", "chrome-extension://"))), None)
        page = page or ctx.new_page()
        cdp = ctx.new_cdp_session(page)
    except Exception:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            launched.close(timeout_s=2.0)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    return Window(launched, pw, browser, page, cdp, profile_dir)


# -- one intervention --------------------------------------------------------

@dataclass
class Intervention:
    """One "hand the human a window" episode, from the ask to the cookies coming back."""
    id: str
    session: str
    url: str
    reason: str = "manual"
    vendor: str = ""
    label: str = ""
    state: str = PENDING
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    seeded: int = 0
    harvested: list[dict] = field(default_factory=list, repr=False)
    applied: bool = False
    error: str = ""
    window_pid: int | None = None
    # Snapshot of what the session needs to seed the window: cookies, user agent, size.
    _seed_cookies: list[dict] = field(default_factory=list, repr=False)
    _ua: str = ""
    _w: int = 1280
    _h: int = 820
    # Coordination between the window's own thread and the caller waiting on it.
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: Any = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.state in (PENDING, OPEN)

    @property
    def age_s(self) -> float:
        return round(time.time() - self.created, 1)

    def wait(self, timeout: float) -> bool:
        """Block until the window is done, or `timeout`. True if it finished."""
        return self._done.wait(timeout)

    def as_dict(self) -> dict:
        out = {"id": self.id, "session": self.session, "site": _host(self.url),
               "url": short_url(self.url), "reason": self.reason, "state": self.state,
               "age_s": self.age_s}
        if self.vendor:
            out["vendor"] = self.label or self.vendor
        if self.window_pid:
            out["window_pid"] = self.window_pid
        if self.seeded:
            out["cookies_seeded"] = self.seeded
        if self.state in TERMINAL:
            out["cookies_earned"] = len(self.harvested)
        if self.error:
            out["error"] = self.error
        return out


class InterventionManager:
    """Every intervention this server has going, and the machinery to run one.

    One active intervention per session at a time: a session hitting the same wall twice
    should re-use the window it already opened, not stack a second on top of it.
    """

    def __init__(self) -> None:
        self._items: dict[str, Intervention] = {}
        self._lock = threading.Lock()
        self._seq = 0

    # -- the ask -----------------------------------------------------------

    def request(self, session: str, url: str, *, verdict: str = "", wall: dict | None = None,
                reason: str | None = None) -> Intervention:
        """Record a pending intervention for a session. Opens nothing."""
        self._reap_expired()
        wall = wall or {}
        with self._lock:
            existing = self._items.get(session)
            if existing is not None and existing.running:
                return existing
            self._seq += 1
            handoff = Intervention(
                id=f"iv{self._seq}", session=session, url=url,
                reason=reason or reason_for(verdict, wall),
                vendor=str(wall.get("vendor") or ""), label=str(wall.get("label") or ""))
            self._items[session] = handoff
        bus.publish(session, "assist", action="requested", url=short_url(url),
                    reason=handoff.reason, vendor=handoff.label or handoff.vendor)
        return handoff

    def get(self, session: str) -> Intervention | None:
        with self._lock:
            return self._items.get(session)

    def by_id(self, handoff_id: str) -> Intervention | None:
        with self._lock:
            for item in self._items.values():
                if item.id == handoff_id:
                    return item
        return None

    def list(self) -> list[Intervention]:
        with self._lock:
            return list(self._items.values())

    def protected_sessions(self) -> set[str]:
        """Sessions the reaper must not close: a window is up, or its earned cookies are not
        carried back yet.

        The second half matters because the window finishes on its own thread (state goes
        terminal there) a moment before the earned cookies are applied to the session on the
        MCP lane. Closing the session in that gap would throw away the pass the human just
        earned, so a solved-but-not-yet-applied intervention keeps its session protected
        until the cookies land (or the intervention is reaped by age).
        """
        with self._lock:
            return {s for s, h in self._items.items()
                    if h.running or (h.state == SOLVED and h.harvested and not h.applied)}

    def decline(self, session: str) -> Intervention | None:
        handoff = self.get(session)
        if handoff and handoff.state == PENDING:
            handoff.state = DECLINED
            handoff.finished = time.time()
            handoff._done.set()
            bus.publish(session, "assist", action="declined", url=short_url(handoff.url))
        return handoff

    def cancel(self, session: str) -> bool:
        """Stop a session's intervention and close its window if one is open."""
        handoff = self.get(session)
        if handoff is None or handoff.state in TERMINAL:
            return False
        handoff._cancel.set()
        if handoff.state == PENDING:
            handoff.state = CANCELLED
            handoff.finished = time.time()
            handoff._done.set()
            bus.publish(session, "assist", action="cancelled", url=short_url(handoff.url))
        # An open window is closed by its own thread noticing the cancel flag.
        return True

    # -- the act -----------------------------------------------------------

    def start(self, session: str, *, snapshot: dict, url: str | None = None,
              timeout_s: float = DEFAULT_TIMEOUT_S,
              opener: Callable[..., Window] | None = None,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic,
              poll_s: float = 1.25) -> Intervention:
        """Open the window for a consented intervention, on a thread of its own.

        Idempotent per session: if a window is already running for it, that one is returned
        and no second window is opened. The snapshot is the session's cookies, user agent
        and size, read on the session's thread and handed here as plain data.
        """
        with self._lock:
            handoff = self._items.get(session)
            if handoff is not None and handoff.state == OPEN:
                return handoff            # already up; do not open a second
            if handoff is None or handoff.state in TERMINAL:
                self._seq += 1
                handoff = Intervention(id=f"iv{self._seq}", session=session,
                                       url=url or snapshot.get("url") or "")
                self._items[session] = handoff
            handoff.url = url or handoff.url or snapshot.get("url") or ""
            # A handoff that came from request() carries the reason the ask decided; a fresh
            # one (confirm without a prior ask) still has the default, so read it off the page.
            if handoff.reason in ("", "manual"):
                handoff.reason = reason_for(str(snapshot.get("verdict") or ""),
                                            snapshot.get("wall"))
            wall = snapshot.get("wall") or {}
            handoff.vendor = handoff.vendor or str(wall.get("vendor") or "")
            handoff.label = handoff.label or str(wall.get("label") or "")
            handoff._seed_cookies = scope_cookies(list(snapshot.get("cookies") or []),
                                                  handoff.url)
            handoff._ua = str(snapshot.get("user_agent") or "")
            handoff._w = int(snapshot.get("width") or 1280)
            handoff._h = int(snapshot.get("height") or 820)
            handoff.state = OPEN
            handoff.started = time.time()
            handoff._done.clear()
            handoff._cancel.clear()
            # Resolved here, not bound at def time, so a test can swap `open_window`.
            handoff._thread = threading.Thread(
                target=self._run, name=f"latchkey-assist-{session}",
                args=(handoff, timeout_s, opener or open_window, sleep, clock, poll_s),
                daemon=True)
        handoff._thread.start()
        return handoff

    def _run(self, handoff: Intervention, timeout_s: float,
             opener: Callable[..., Window], sleep: Callable[[float], None],
             clock: Callable[[], float], poll_s: float) -> None:
        """Drive one window: open, seed, wait for the human, harvest, close. Never raises."""
        profile_dir = os.path.join(HANDOFF_ROOT, handoff.id)
        window: Window | None = None
        try:
            window = opener(profile_dir, handoff.url, user_agent=handoff._ua,
                            width=handoff._w, height=handoff._h)
            handoff.window_pid = window.pid
            handoff.seeded = window.seed(handoff._seed_cookies)
            window.open_url(handoff.url)
            bus.publish(handoff.session, "assist", action="open", url=short_url(handoff.url),
                        pid=window.pid, seeded=handoff.seeded)
            latest = handoff._seed_cookies
            deadline = clock() + max(0.1, timeout_s)
            solved_streak = 0
            while True:
                if handoff._cancel.is_set():
                    self._finish(handoff, CANCELLED, window, latest, solved=False)
                    return
                if clock() >= deadline:
                    self._finish(handoff, FAILED, window, latest, solved=False,
                                 error="the window was open past its time limit")
                    return
                if not window.alive():
                    # The human closed it. Their close is a "done" - carry back whatever the
                    # window last held, which is where any earned clearance is.
                    self._finish(handoff, SOLVED, window, latest, solved=False, closed=True)
                    return
                snap = window.cookies()
                if snap is not None:
                    latest = snap
                verdict = window.solved()
                if verdict is True:
                    solved_streak += 1
                    if solved_streak >= 2:      # confirmed, not a flash between redirects
                        self._finish(handoff, SOLVED, window, latest, solved=True)
                        return
                else:
                    solved_streak = 0
                sleep(poll_s)
        except Exception as exc:  # noqa: BLE001 - a broken window is a failed assist, not a crash
            handoff.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._finish(handoff, FAILED, window, handoff._seed_cookies, solved=False)

    def _finish(self, handoff: Intervention, state: str, window: Window | None,
                latest: list[dict], *, solved: bool, closed: bool = False,
                error: str = "") -> None:
        """Close the window, keep the cookies it earned, and mark the intervention done."""
        earned = self._earned(handoff._seed_cookies, latest or [], handoff.url)
        handoff.harvested = earned
        if error and not handoff.error:
            handoff.error = error
        handoff.state = state
        handoff.finished = time.time()
        if window is not None:
            window.close()
        handoff.window_pid = None
        handoff._done.set()
        bus.publish(handoff.session, "assist",
                    action="solved" if state == SOLVED else state,
                    url=short_url(handoff.url), earned=len(earned),
                    closed_by_user=closed)

    @staticmethod
    def _earned(seeded: list[dict], final: list[dict], url: str) -> list[dict]:
        """The cookies the window gained or changed for the site: the clearance to carry back.

        Only the site's family (and vendor clearance), and only the ones that are new or
        whose value moved - so what comes back is the pass the human earned, not the whole
        jar churning back into the session.
        """
        before = {(str(c.get("domain") or c.get("url") or "").lstrip("."),
                   c.get("name"), c.get("path") or "/"): c.get("value")
                  for c in seeded}
        out = []
        for cookie in scope_cookies(final, url):
            key = (str(cookie.get("domain") or cookie.get("url") or "").lstrip("."),
                   cookie.get("name"), cookie.get("path") or "/")
            if before.get(key) != cookie.get("value"):
                out.append(cookie)
        return out

    # -- housekeeping ------------------------------------------------------

    def _reap_expired(self) -> None:
        """Drop pending consents nobody acted on, and terminal ones that are old."""
        now = time.time()
        with self._lock:
            for session, handoff in list(self._items.items()):
                if handoff.state == PENDING and now - handoff.created > CONSENT_TTL_S:
                    handoff.state = CANCELLED
                    handoff._done.set()
                    self._items.pop(session, None)
                elif handoff.state in TERMINAL and now - handoff.finished > CONSENT_TTL_S:
                    self._items.pop(session, None)

    def close_all(self) -> int:
        """Cancel every live intervention and close its window. For server shutdown."""
        closed = 0
        for handoff in self.list():
            if handoff.running:
                self.cancel(handoff.session)
                handoff.wait(8.0)
                closed += 1
        sweep_orphans()
        return closed


def sweep_orphans() -> int:
    """Remove leftover handoff profiles - and any orphaned window - after an unclean exit.

    A server killed mid-intervention (SIGKILL, a pulled plug) leaves a throwaway profile on
    disk holding a copy of a site's cookies, and its headed Chrome reparented to launchd
    still running and still holding memory. Nothing else will clean either, so the reaper
    does, on a schedule and at boot - but never a window a *live* server still owns. That is
    the note `chrome.record_owner` leaves at launch, not the parent pid: a detached Chrome is
    reparented to launchd immediately, so its parent is 1 either way, and judging by that
    alone quit windows belonging to servers that were still working. Never a directory a live
    Chrome holds either. Returns how many profiles were removed.
    """
    if not os.path.isdir(HANDOFF_ROOT):
        return 0
    removed = 0
    for entry in os.listdir(HANDOFF_ROOT):
        path = os.path.join(HANDOFF_ROOT, entry)
        if not os.path.isdir(path):
            continue
        owner = chrome_mod.lock_owner(path)
        if owner is not None:
            info = chrome_mod.process_info(owner)
            # Parent 1 means the server that started it is gone and this window was
            # reparented to launchd: an orphan nobody is using. Ask it to quit, then
            # reclaim its profile. A window whose parent is alive belongs to a running
            # server and is left alone.
            if chrome_mod.live_owner(owner):
                continue          # a live server opened this window, and still wants it
            if not (info and info[0] == 1 and f"--user-data-dir={path}" in info[1]):
                continue
            chrome_mod.terminate(owner, 6.0)
            if chrome_mod.lock_owner(path):
                continue          # it would not go; leave the profile for the next pass
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


# The one manager the server and the reaper share.
manager = InterventionManager()
