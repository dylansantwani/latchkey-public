"""The parts every verb needs: the page handle, the verdict probe, the events.

`Browser` is assembled from mixins - navigation, automation, frames, tabs - and
each of them needs the same three things: the page it is driving, a way to say
what just happened, and a memoised read of what the page now is. Those live here,
so no mixin has to know how the browser was launched or where events go.

This is also where the on-page cursor lives. It is opt-in - injecting a node into
a page being driven is a real side effect - so it has to be attached at runtime,
not fixed at construction. Coordinates cost a round trip per action, so they are
only asked for when something is actually watching: a viewer, or the cursor.
"""
from __future__ import annotations

import os
import re
import time

from typing import Any

from . import human
from .a11y import RefError, Refs
from .detect import PageProbe, short_url, secretish, PageState
from .events import CURSOR_INIT_SCRIPT, REMOVE_CURSOR_JS, Event, bus, move_cursor
from .world import MISSING, World
from . import policy
from .policy import WRITE_VERBS, refusal
from .sessions import DEFAULT_NAME

from urllib.parse import urlparse

# How `settle` decides a page has stopped changing. Playwright's `networkidle` waits for
# *zero* connections for 500ms, which a page with a video, a websocket or a long-poll
# channel never reaches - so every action against a live app burned the whole ceiling for
# a page that was interactive at once. This watches requests in flight instead, but not all
# of them count:
#
#   * a long-lived transport (websocket, server-sent events, streaming media, a fire-and-
#     forget beacon) is background by nature and never a thing an action is waiting on, so
#     it is ignored by resource type;
#   * a request still open after SETTLE_LONGPOLL_MS is a held-open channel, not a response
#     to what we just did, so it is aged out of the count;
#   * everything else - a document, its subresources, an XHR or fetch a click kicked off -
#     is what "the page is still working" means, and settling waits for all of it to finish.
#
# The page is settled once that filtered count has stayed at or below SETTLE_INFLIGHT_MAX
# (zero, by default) for SETTLE_QUIET_MS without interruption. The quiet window is also the
# beat that catches changes which never touch the network - a panel opening, a class
# flipping - so there is no separate tail. Every threshold here is tunable by env.
SETTLE_QUIET_MS = 350         # how long the network must stay quiet before we call it settled
SETTLE_INFLIGHT_MAX = 0       # genuine requests that may be outstanding and still count as quiet
SETTLE_LONGPOLL_MS = 1500     # a request open longer than this is a channel, not an action's reply
SETTLE_POLL_MS = 25           # how often the quiet check samples while it waits
SETTLE_PRUNE_MS = 30_000      # forget a tracked request after this, in case its end was missed

# Transports that stay open by design and are never what an action is waiting on.
SETTLE_IGNORED_TYPES = frozenset({"websocket", "eventsource", "media", "ping"})

# How models spell what they are waiting for. Each maps to one of the waits that exist.
UNTIL_ALIASES = {
    "appear": "text", "appears": "text", "contains": "text", "text_present": "text",
    "text_visible": "text", "phrase": "text",
    "disappear": "gone", "disappears": "gone", "text_gone": "gone", "absent": "gone",
    "not_text": "gone",
    "visible": "selector", "element": "selector", "css": "selector", "exists": "selector",
    "present": "selector",
    "selector_gone": "hidden", "detached": "hidden", "invisible": "hidden",
    "element_gone": "hidden",
    "navigation": "url", "navigate": "url", "url_contains": "url", "redirect": "url",
    "title_contains": "title",
    "loaded": "load", "ready": "load", "domcontentloaded": "load", "page_load": "load",
    "idle": "networkidle", "network_idle": "networkidle", "network": "networkidle",
    "sleep": "time", "ms": "time", "delay": "time", "timeout": "time", "pause": "time",
}


def normalise_until(until: Any, value: Any = None) -> tuple[str, Any]:
    """`until` as one of the waits there are, read the way it arrives.

    Besides the synonyms, two shapes carry their meaning in the value itself: a CSS-looking
    `until` ("#inbox") is a selector to wait for, and a URL-looking one is a URL.
    """
    word = str(until or "").strip()
    low = word.lower().replace("-", "_").replace(" ", "_")
    if low in ("text", "gone", "selector", "hidden", "url", "title", "load", "networkidle",
               "time"):
        return low, value
    if low in UNTIL_ALIASES:
        return UNTIL_ALIASES[low], value
    if word and value is None:
        if word.startswith(("http://", "https://", "/")):
            return "url", word
        if word[0] in "#.[" or re.match(r"^[a-z]+[#.\[]", word):
            return "selector", word
    return low, value

REMOVE_CURSOR_JS = ("() => { if (window.__latchkeyCursor) "
                    "window.__latchkeyCursor.remove(); }")


class Driver:
    """Shared machinery for the verb mixins. `Browser` supplies the lifecycle."""

    def __init__(self) -> None:
        self._page: Any | None = None
        self._ctx: Any | None = None
        self._cdp: Any | None = None
        self.label: str = DEFAULT_NAME
        self.session_name: str | None = None      # set by the registry; events go by it
        self.show_cursor: bool = False
        self._probe = PageProbe()
        self._epoch = 0
        self._cursor: tuple[float, float] | None = None
        self._tabs: dict[int, str] = {}      # id(page) -> stable tab id
        self._tab_seq = 0
        self._cdp_sessions: dict[int, Any] = {}   # id(page) -> that tab's CDP session
        self._cookie_hosts: set[str] | None = None   # None = this session does not know
        self._jar_counts: dict[str, int] = {}     # host -> what the browser said it holds
        self._cookie_names: dict[str, list[str]] = {}  # host -> the names in that jar
        self._last_response: dict = {}            # the last document response we saw
        self._hrng: Any | None = None             # the seeded source of human timing
        self.read_only: bool = False              # refused writes; see policy.py
        self.refs = Refs()                        # what this session's snapshot refs point at
        self.dialogs: list[dict] = []             # dialogs answered for us, newest last
        # id(page) -> {request: (started_monotonic, resource_type)} for requests in flight.
        # Filtered by type and age at settle time; drives when a page is called quiet.
        self._net_reqs: dict[int, dict[Any, tuple[float, str]]] = {}
        self._now = time.monotonic                # the clock settle reads; tests replace it

    # -- the page ----------------------------------------------------------

    @property
    def page(self) -> Any:
        if not self._page:
            raise RuntimeError("browser not started; call start() first")
        return self._page

    @property
    def watching(self) -> bool:
        """Is anyone watching this session?

        Falls back to the bus: a viewer that subscribes gets coordinates for free
        without having to ask the session to turn anything on.
        """
        return bool(self.show_cursor or bus.subscriber_count)

    def _touch(self) -> int:
        """Record that the page may have changed, and return the new epoch."""
        self._epoch += 1
        self._probe.invalidate()
        return self._epoch

    def _publish(self, kind: str, **detail: Any) -> Event:
        return bus.publish(self.session_name or self.label or DEFAULT_NAME, kind, **detail)


    def ref_selector(self, ref: str) -> str:
        """The selector to act on for a snapshot ref: the live tag, else the path it was taken at.

        The tag is minted on the element itself, so it survives a re-render that keeps the
        element and disappears with the element it named. One cheap round trip decides which,
        and that is the difference between clicking the button the agent *read* and clicking
        whatever now happens to sit where it was.
        """
        ref = str(ref).strip()
        live = self.refs.selector(ref)
        if self.refs.known(ref) is None:
            return live     # not one of ours: let Playwright fail in its own words
        if self._matches(live):
            return live
        path = self.refs.fallback(ref)
        if path and self._matches(path):
            return path
        raise RefError(
            f"{self.refs.describe(ref)}, and it is not on the page now - the page has moved "
            f"on since that snapshot. Read it again (latchkey_snapshot) and act on the ref "
            f"that is there now.")

    def _matches(self, selector: str) -> bool:
        try:
            return self.page.locator(selector).count() > 0
        except Exception:  # noqa: BLE001
            return False

    def wait_until(self, until: str, value: str | None = None,
                   timeout_ms: int = 10_000) -> dict:
        """Wait for one thing about the page, in one call, and say whether it happened.

        Without this an agent polls: `eval` in a loop, a round trip and a paragraph of tokens
        each time, and it reads the page anyway. This is openbrowser's `browser_wait` - the
        cheapest tool in the box, because one call replaces a habit.

        The answer is small and true either way: what was waited for, how long it took, and on
        a timeout where the page actually is, so the next call can be a decision instead of a
        guess.
        """
        started = time.monotonic()
        page = self.page
        wanted, value = normalise_until(until, value)
        text = str(value if value is not None else "")
        body = "document.body ? document.body.innerText : ''"
        try:
            if wanted in ("text", "appear"):
                self._poll(f"t => ({body}).includes(t)", text, timeout_ms)
            elif wanted in ("gone", "text-gone", "disappear"):
                self._poll(f"t => !({body}).includes(t)", text, timeout_ms)
            elif wanted in ("selector", "visible", "element"):
                page.wait_for_selector(text, state="visible", timeout=timeout_ms)
            elif wanted in ("hidden", "selector-gone", "detached"):
                page.wait_for_selector(text, state="detached", timeout=timeout_ms)
            elif wanted == "url":
                page.wait_for_url(re.compile(re.escape(text)), timeout=timeout_ms)
            elif wanted == "title":
                self._poll("t => document.title.includes(t)", text, timeout_ms)
            elif wanted == "load":
                page.wait_for_load_state("load", timeout=timeout_ms)
            elif wanted == "networkidle":
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            elif wanted == "time":
                page.wait_for_timeout(max(0, min(int(float(text or timeout_ms)), timeout_ms)))
            else:
                return {"ok": False, "until": until, "error": (
                    "until must be one of: text (a phrase appears), gone (it disappears), "
                    "selector (an element is visible), hidden, url, title, load, networkidle, "
                    "time (sleep value ms)")}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "until": wanted, "value": text or None,
                    "waited_ms": round((time.monotonic() - started) * 1000),
                    "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
                    "now": self.state().summary()}
        return {"ok": True, "until": wanted, "value": text or None,
                "waited_ms": round((time.monotonic() - started) * 1000),
                "url": short_url(page.url)}

    def _poll(self, js: str, arg: Any, timeout_ms: float, every_s: float = 0.1) -> None:
        """Wait until `js(arg)` is truthy, from latchkey's own world.

        Playwright's `wait_for_function` polls in the page's main world, where a site can
        watch it. This asks the same question from the isolated world, and a page that is
        mid-navigation (the context gone between two polls) is simply asked again.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_ms)) / 1000.0
        while True:
            try:
                if self.run_js(js, arg):
                    return
            except Exception:  # noqa: BLE001
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timeout {int(timeout_ms)}ms exceeded waiting for "
                                   f"{js.strip()[:60]}")
            time.sleep(every_s)

    # -- settling ----------------------------------------------------------

    def track_network(self, page: Any) -> None:
        """Record a page's requests in flight, so `settle` can tell busy from quiet.

        Attached once per page (`Browser` calls it as pages appear). Playwright promises a
        `requestfinished` or `requestfailed` for every `request` - including the ones a
        navigation cancels, which is what lets the set heal itself on every page load rather
        than growing over a long session. The handlers must never raise into Playwright's
        event dispatch, hence the blanket guards.
        """
        key = id(page)
        self._net_reqs.setdefault(key, {})

        def started(req: Any) -> None:
            try:
                rtype = req.resource_type
            except Exception:  # noqa: BLE001
                rtype = ""
            self._net_reqs.setdefault(key, {})[req] = (self._now(), rtype)

        def done(req: Any) -> None:
            reqs = self._net_reqs.get(key)
            if reqs is not None:
                reqs.pop(req, None)

        for event, handler in (("request", started), ("requestfinished", done),
                               ("requestfailed", done)):
            try:
                page.on(event, handler)
            except Exception:  # noqa: BLE001
                pass
        try:
            page.on("close", lambda _p=page: self._net_reqs.pop(key, None))
        except Exception:  # noqa: BLE001
            pass

    def reset_network(self, page: Any | None = None) -> None:
        """Forget a page's in-flight requests. A navigation is a clean slate for settling."""
        reqs = self._net_reqs.get(id(page if page is not None else self._page))
        if reqs is not None:
            reqs.clear()

    def _pending_requests(self, key: int, now: float, longpoll_ms: int) -> int:
        """How many requests this page is genuinely waiting on right now.

        Ignores the transports that stay open by design (see SETTLE_IGNORED_TYPES) and the
        ones held open past `longpoll_ms` (a channel, not a reply), and prunes anything so
        old its end event was clearly lost, so the set cannot grow without bound.
        """
        reqs = self._net_reqs.get(key)
        if not reqs:
            return 0
        pending = 0
        for req, (started_at, rtype) in list(reqs.items()):
            age_ms = (now - started_at) * 1000
            if age_ms >= SETTLE_PRUNE_MS:
                reqs.pop(req, None)
                continue
            if rtype in SETTLE_IGNORED_TYPES or age_ms >= longpoll_ms:
                continue
            pending += 1
        return pending

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        try:
            return max(0, int(float(raw)))
        except ValueError:
            return default

    def settle(self, cap_ms: int) -> float:
        """Let the page go quiet, up to `cap_ms`, and return what it actually cost.

        A ceiling, not a duration. Sleeping the whole of it on every action is what
        makes a batch slow: a click that opens a panel in 40 ms still paid a second and
        a half, two and a half seconds went by after every navigation, and a handful of
        real calls walked straight through the thirty seconds the MCP client waits for an
        answer - which is exactly what a client timeout looks like from here.

        The signal is requests in flight, watched by `track_network`. The page is settled
        once no more than `SETTLE_INFLIGHT_MAX` of them have been outstanding, without
        interruption, for `SETTLE_QUIET_MS` - so a navigation's burst of resources holds
        it while it drains, and the steady one or two connections a live app keeps open
        (a websocket, a heartbeat, a poll) do not. That last part is the whole point:
        Playwright's `networkidle` demands *zero* connections for 500 ms, which those apps
        never reach, so every action against them used to burn the entire ceiling for a
        page that was interactive at once. A page that is genuinely still working - a real
        flood of requests that will not drain - still gets the whole ceiling, which is what
        it got before, and a caller who knows a page is slow passes a bigger `settle_ms`.

        The quiet window also covers the changes that never touch the network - a panel
        opening, a class flipping, an aria-expanded turning over - which is why there is no
        separate tail: the beat is already built into waiting for the quiet to hold.
        """
        if cap_ms <= 0:
            return 0.0
        started = self._now()
        page = self.page
        key = id(page)
        quiet_ms = self._env_int("LATCHKEY_SETTLE_QUIET_MS", SETTLE_QUIET_MS)
        threshold = self._env_int("LATCHKEY_SETTLE_INFLIGHT", SETTLE_INFLIGHT_MAX)
        longpoll_ms = self._env_int("LATCHKEY_SETTLE_LONGPOLL_MS", SETTLE_LONGPOLL_MS)
        deadline = started + cap_ms / 1000
        quiet_since: float | None = None
        while True:
            now = self._now()
            remaining = deadline - now
            if remaining <= 0:
                break
            if self._pending_requests(key, now, longpoll_ms) <= threshold:
                if quiet_since is None:
                    quiet_since = now
                elif (now - quiet_since) * 1000 >= quiet_ms:
                    break
            else:
                quiet_since = None
            # A short beat that also pumps Playwright's event loop, so the request/response
            # events that move the counter are delivered while we wait on it.
            try:
                page.wait_for_timeout(min(SETTLE_POLL_MS, max(1.0, remaining * 1000)))
            except Exception:  # noqa: BLE001
                break
        return round((self._now() - started) * 1000, 1)

    def state(self, text_limit: int = 4000) -> PageState:
        """What the page is now: url, title, text and the verdict signals.

        One round trip, and a second call without an action in between answers
        from memory. "The page" always means the tab this session is driving - the
        one the last `switch()` or `new_page()` selected - and the answer says which
        tab that was.
        """
        page = self.page
        url = page.url
        state = self._probe.state(page, url, self._epoch, text_limit,
                                  response=self._last_response,
                                  cookie_names=lambda: self._jar_names(url),
                                  run=self.run_js)
        state.tab = self.tab_id(page)
        state.tabs = len(self._ctx.pages) if self._ctx is not None else 1
        state.host_cookies = self.host_cookies(url)
        return state

    def note_response(self, response: Any) -> None:
        """Remember the last document response, so a wall can be attributed.

        A wall is often only legible in the response - `cf-mitigated`, `x-datadome`, a
        403 with no body worth reading - and headers are the half of the evidence a page
        cannot fake. One dict per navigation, and it is the difference between "blocked"
        and "blocked by DataDome, no challenge to wait out".
        """
        try:
            if response.request.resource_type != "document":
                return
            headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
            self._last_response = {"url": response.url, "status": int(response.status),
                                   "headers": headers}
        except Exception:  # noqa: BLE001
            return

    def _jar_names(self, url: str) -> list[str]:
        """The *names* of this session's cookies for a host: the cheapest vendor tell.

        Only asked for when a page already looks walled - a vendor cookie says the vendor
        has already spoken to this client, whatever page it is showing now - and cached
        per host, because a wall does not hand out new cookies. The happy path pays
        nothing for this.
        """
        host = urlparse(url).netloc.split(":")[0].lower()
        if not host:
            return []
        if host in self._cookie_names:
            return self._cookie_names[host]
        names: list[str] = []
        if self._ctx is not None:
            try:
                names = [str(cookie.get("name") or "") for cookie in self._ctx.cookies([url])]
            except Exception:  # noqa: BLE001
                names = []
        self._cookie_names[host] = names
        return names

    def host_cookies(self, url: str) -> int | None:
        """How many of this session's cookies apply to the host in front of us.

        inject mode answers from the domains collected while injecting, which is free -
        it is a set, not a question put to the browser - and that is why the verdict can
        weigh it on every read. A cloned profile never passes through that injection, so
        its jar is only knowable by asking Chrome: one `context.cookies([url])` per
        navigation, cached until the next one, because a host's cookie count is the kind
        of thing only navigation (a different host, a fresh login) can change. `None`
        means genuinely unknown, and an unknown never moves a verdict.
        """
        host = urlparse(url).netloc.split(":")[0].lower()
        if not host:
            return None
        hosts = getattr(self, "_cookie_hosts", None)
        if hosts is not None:
            return sum(1 for cookie_host in hosts
                       if host == cookie_host or host.endswith("." + cookie_host))
        return self._jar_count(url, host)

    def _jar_count(self, url: str, host: str) -> int | None:
        """Ask the browser what it holds for this url. Once per page, not per read."""
        if host in self._jar_counts:
            return self._jar_counts[host]
        if self._ctx is None:
            return None
        try:
            cookies = self._ctx.cookies([url])
        except Exception:  # noqa: BLE001
            return None           # mid-navigation, or a context that cannot answer
        if not isinstance(cookies, (list, tuple)):
            return None
        self._jar_counts[host] = len(cookies)
        return self._jar_counts[host]

    def forget_jar(self) -> None:
        """The jar may have changed, so the next host question is worth asking again.

        Called by every verb that lands on a new page, and by `refresh()`.
        """
        self._jar_counts.clear()
        self._cookie_names.clear()

    # -- CDP ---------------------------------------------------------------

    def cdp_for(self, page: Any | None = None) -> Any:
        """A CDP session for a page, created on demand.

        CDP sessions are per page. The one made at start-up belongs to the first tab,
        which is fine for injecting cookies but wrong for anything that has to follow
        the page the agent is driving - a screencast on a stale page shows a tab
        nobody is looking at.
        """
        page = page if page is not None else self.page
        key = id(page)
        session = self._cdp_sessions.get(key)
        if session is None:
            if self._ctx is None:
                raise RuntimeError("browser not started")
            session = self._ctx.new_cdp_session(page)
            self._cdp_sessions[key] = session
        return session

    def _forget_cdp(self, page: Any) -> None:
        """Drop the CDP session for a page that is gone (CDP sessions die with it)."""
        self._cdp_sessions.pop(id(page), None)
        world = getattr(self, "_world", None)
        if world is not None:
            world.forget(page)

    # -- JavaScript --------------------------------------------------------

    @property
    def world(self) -> World:
        """The isolated world latchkey's own JavaScript runs in (see `world.py`)."""
        world = getattr(self, "_world", None)
        if world is None:
            world = self._world = World(self.cdp_for)
        return world

    def run_js(self, js: str, arg: Any = MISSING, *, page: Any | None = None,
               frame: Any | None = None) -> Any:
        """Run JavaScript on a page from the isolated world - every internal read goes
        through here, never `page.evaluate`, which runs in the page's main world through
        a wrapper the page can see. `arg` is passed as the function's one argument.

        A page with no CDP behind it (a stand-in in a test, a browser not started) is
        asked through Playwright instead, which is the only time the main world is used.
        """
        page = page if page is not None else self.page
        try:
            self.cdp_for(page)
        except Exception:  # noqa: BLE001
            scope = frame if frame is not None else page
            return scope.evaluate(js) if arg is MISSING else scope.evaluate(js, arg)
        return self.world.evaluate(page, js, arg, frame=frame)

    # -- tabs --------------------------------------------------------------

    def tab_id(self, page: Any | None = None) -> str:
        """A tab's stable id (`t1`, `t2`, ...).

        Indices are positional and shift when a tab closes, which makes them a poor
        thing for an agent to hold on to across calls. An id is handed out the first
        time a page is seen and never changes.

        Numbering happens for every page of the context at once, in the order
        Playwright reports them, so ids follow the order tabs were opened rather than
        the order the agent happened to ask about them.
        """
        page = page if page is not None else self._page
        if id(page) not in self._tabs:
            self._assign_ids()
        return self._tabs[id(page)]

    def _assign_ids(self) -> None:
        pages = list(self._ctx.pages) if self._ctx is not None else []
        if self._page is not None:
            pages.append(self._page)
        for page in pages:
            if id(page) not in self._tabs:
                self._tab_seq += 1
                self._tabs[id(page)] = f"t{self._tab_seq}"

    def page_for(self, target: Any) -> Any:
        """Resolve a tab id ('t2') or an index (1) to the page itself."""
        if not self._ctx:
            raise RuntimeError("browser not started")
        pages = list(self._ctx.pages)
        if isinstance(target, bool) or not isinstance(target, (int, str)):
            raise ValueError(f"tab target must be an id like 't2' or an index, not "
                             f"{target!r}")
        if isinstance(target, str) and not target.isdigit():
            wanted = target.lower().lstrip("t") if target.lower().startswith("t") else target
            for page in pages:
                if self.tab_id(page) == f"t{wanted}":
                    return page
            raise KeyError(f"no tab {target!r}; this session has "
                           f"{[self.tab_id(p) for p in pages]}")
        index = int(target)
        if not -len(pages) <= index < len(pages):
            raise IndexError(f"no tab at index {index}; this session has {len(pages)}")
        return pages[index]

    def evaluate(self, js: str, frame: int | None = None) -> Any:
        """Run caller-supplied JS in the page's main world.

        It counts as a write for policy purposes - `fetch(url, {method: 'POST'})` is one
        line - so a read-only session refuses it rather than pretend it is a reader.  Unlike
        `run_js`, this is public page/frame evaluation: preserve Playwright's documented
        main-world semantics for callers.  Latchkey's own probes remain isolated by using
        `run_js` directly.
        """
        self.guard_write("eval")
        self._touch()
        scope = self.page if frame is None else self.frame_at(frame)
        result = scope.evaluate(js)
        self._publish("eval", js=js, frame=frame, **self.state().summary())
        return result

    # -- policy ------------------------------------------------------------

    def secret_target(self, selector: str) -> bool:
        """Would reporting what was typed into this target leak a credential?

        Two ways to know, because there are two ways to name a field. A CSS selector says
        so in its own text (`input[type=password]`), and `detect.secretish` reads that. A
        snapshot `ref` says nothing at all - `[data-3f2a="e5"]` is opaque - so the answer
        has to come from what the walker saw on the element, which is what `Refs` kept.
        Acting by ref is the flow the tools recommend, so this is the path that matters.
        """
        ref = self.refs.ref_of(selector)
        if ref is not None:
            return self.refs.is_secret(ref)
        return secretish(selector)

    def guard_navigation(self, url: str) -> str:
        """The url this session may go to, normalised - or a refusal saying why not.

        Two questions, and they are asked in one place because every way of navigating
        (goto, an action list's {do:'goto'}, a new tab with a url, the login handoff)
        funnels through `NavigationMixin.goto`. Checking at the tool would have left the
        other three open.
        """
        url = policy.check_url(url)
        self.route_for(url)
        refused = self.navigation_refusal(url)
        if refused:
            raise policy.NavigationRefused(refused)
        return url

    def navigation_refusal(self, url: str) -> str:
        """Why this session must not open this url, or "" - `Browser` knows, `Driver` does not."""
        return ""

    def route_for(self, url: str) -> None:
        """Move to the mode this url needs, if the session left that open - see `Browser`."""
        return None

    def guard_write(self, verb: str) -> None:
        """Refuse a verb that can change something, in a read-only session.

        Enforced at the verb rather than at the action-list layer, because the verb is
        the only thing that knows what it does: `browser.click()` from a script and
        `{do: 'click'}` through `latchkey_act` are the same act on the same account, and
        they get the same answer. The refusal goes on the bus as well, so a human
        watching sees *why* nothing happened.
        """
        if verb not in WRITE_VERBS:
            raise AssertionError(
                f"{verb} calls guard_write, so it can send something, but it is not in "
                f"policy.WRITE_VERBS - add it there in the same change")
        if not self.read_only:
            return
        self._publish("read_only", verb=verb, refused=True)
        raise refusal(self.label or DEFAULT_NAME, verb)

    # -- the cursor --------------------------------------------------------

    @property
    def viewport(self) -> dict:
        """The page's size in CSS pixels - the units a viewer has to draw the pointer in.

        A frame is a *picture* of the viewport, taken at the screencast's own width (1100 by
        default), so a viewer that scales its pointer by the picture's pixels puts it a fifth
        of the way out: a click at the middle of a 1280 px page is 640 CSS px in, which is 550
        px into the picture, and the picture is not the page. Sending the page's own size with
        the pointer is what makes the two agree - and it is here, not in the viewer, because
        this side is the one that knows.
        """
        try:
            size = self.page.viewport_size
        except Exception:  # noqa: BLE001
            size = None
        if not size:
            # A browser attached over CDP (dedicated, real) has no Playwright viewport; the
            # page is still the size the fingerprint set it to.
            plan = getattr(self, "fingerprint", None)
            if plan is not None and getattr(plan, "width", None):
                return {"vw": int(plan.width), "vh": int(plan.height)}
            return {}
        return {"vw": int(size["width"]), "vh": int(size["height"])}

    @property
    def humanize(self) -> bool:
        """Is this session moving and typing the way a person does?

        On by default. It costs a few hundred milliseconds an action and it is the one
        layer of detection a fingerprint cannot buy off: a fingerprint-perfect profile
        that clicks the exact centre of every element in the same instant is still the
        easiest client on the page to pick out. `LATCHKEY_HUMANIZE=0`, or
        `humanize=False` in the spec, turns it off for a session paying for speed.
        """
        spec = getattr(self, "spec", None)
        return human.enabled(getattr(spec, "humanize", None))

    @property
    def human_rand(self) -> Any:
        """The session's own source of human timing, seeded when the spec says so.

        Seeded means a session can be reproduced exactly - which is what makes a flaky
        *site* distinguishable from a flaky pointer.
        """
        if self._hrng is None:
            self._hrng = human.rand(getattr(getattr(self, "spec", None), "seed", None))
        return self._hrng

    def _target_point(self, selector: str,
                      frame: int | None = None) -> tuple[float, float] | None:
        """A point inside an element, in viewport coordinates, or None.

        Not the centre: `_position` answers "where is this element" for a viewer, this
        answers "where would a hand land on it". Landing dead centre of every element is
        a published behavioural tell, so the point is inset from the edges and picked
        fresh each time.
        """
        try:
            box = self.locator_in(selector, frame).bounding_box(timeout=5_000)
        except Exception:  # noqa: BLE001
            return None
        if not box:
            return None
        return human.click_point(box, self.human_rand)

    def _viewport_centre(self) -> tuple[float, float] | None:
        """The middle of the page, for a scroll with no element to aim at."""
        vp = self.viewport or {}
        if not vp:
            return None
        return vp.get("vw", 0) / 2, vp.get("vh", 0) / 2

    def _move_pointer(self, x: float, y: float, click: bool = False) -> None:
        """Take the real pointer to a point, the long way round.

        The drawn cursor and the pointer the page can see used to disagree: the cursor
        glided while the real pointer teleported, so every click arrived as a mousemove-
        free jump to the element's exact centre. Both now follow the same path.

        A path is only walked from a position we know. After a navigation the last
        position is stale, and a pointer that visibly crosses the page from somewhere it
        never was is worse than no path at all.
        """
        rng = self.human_rand
        start = getattr(self, "_cursor", None)
        points: list[tuple[float, float]] = [(x, y)]
        if start and self.humanize and (start[0], start[1]) != (x, y):
            vp = self.viewport or {}
            inside = (not vp or (0 <= start[0] <= vp.get("vw", 0)
                                 and 0 <= start[1] <= vp.get("vh", 0)))
            if inside:
                points = human.waypoints(start[0], start[1], x, y, rng)
        for index, (px, py) in enumerate(points):
            last = index == len(points) - 1
            # The pointer and the drawing are two different audiences - the page and the
            # viewer - so a failure in one is not allowed to cancel the other.
            try:
                self.page.mouse.move(px, py, steps=2 if len(points) > 1 else 1)
            except Exception:  # noqa: BLE001
                pass
            if self.watching:
                try:
                    move_cursor(self.run_js, round(px, 1), round(py, 1), click and last)
                except Exception:  # noqa: BLE001
                    pass
            if not last:
                try:
                    self.page.wait_for_timeout(human.step_pause_ms(rng))
                except Exception:  # noqa: BLE001
                    break
        self._cursor = (float(x), float(y))

    def _approach(self, selector: str | None = None, click: bool = False,
                  frame: int | None = None) -> dict:
        """Bring the pointer onto an element; returns {x, y} for the event detail.

        The acting verbs' replacement for `_cursor_to`: same return shape, but the real
        pointer moves too, it lands inside the element rather than on its centre, and the
        walk happens even when nobody is watching - because the page is watching.

        An unattended session that has also asked for machine-shaped input pays nothing,
        exactly as before.
        """
        if not (self.humanize or self.watching):
            return {}
        point = self._target_point(selector, frame) if selector else self._viewport_centre()
        if point is None:
            return {}
        x, y = round(point[0], 1), round(point[1], 1)
        self._move_pointer(x, y, click=click)
        return {"x": x, "y": y, **self.viewport}

    def _type_like(self, locator: Any, value: str, point: dict | None = None) -> bool:
        """Type into a field one key at a time. False means "fall back to fill()".

        `fill()` sets a value in one go: no keydown, no keyup, no keypress. A page that
        counts keystrokes - and plenty do, it is a published tell - sees a field that
        filled itself. Real key events are the fix, and also the fragile half of this
        module: masked inputs, autocomplete widgets and contenteditable all answer
        differently, so the value is read back and anything unexpected is handed to
        `fill()`, which is slower to fool but never fails.
        """
        rng = self.human_rand
        try:
            if point:
                # Focus it where the hand landed, not by asking the locator to click the
                # centre - that would be the same tell in miniature.
                self.page.mouse.click(point["x"], point["y"])
            else:
                locator.click(timeout=8_000)
        except Exception:  # noqa: BLE001
            return False
        _, delays = human.type_timing(value, rng)
        try:
            for char, delay in zip(value, delays):
                self.page.keyboard.type(char, delay=0)
                self.page.wait_for_timeout(delay)
        except Exception:  # noqa: BLE001
            return False
        try:
            got = locator.input_value(timeout=3_000)
        except Exception:  # noqa: BLE001
            return True          # nothing to read back; take the typing at its word
        return got == value

    def _position(self, selector: str) -> tuple[float, float] | None:
        """Centre of an element in viewport coordinates, or None if unknowable."""
        if not self.watching:
            return None
        try:
            box = self.page.locator(selector).first.bounding_box(timeout=5_000)
        except Exception:  # noqa: BLE001
            return None
        if not box:
            return None
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def _cursor_to(self, selector: str, click: bool = False) -> dict:
        """Move the pointer onto an element; returns {x, y} for the event detail.

        Empty when nobody is watching, so an unattended session pays nothing.
        """
        point = self._position(selector)
        if point is None:
            return {}
        x, y = round(point[0], 1), round(point[1], 1)
        self._cursor = (x, y)
        move_cursor(self.run_js, x, y, click)
        return {"x": x, "y": y, **self.viewport}

    def attach_cursor(self) -> bool:
        """Start showing the agent's pointer, from now on and after navigations."""
        self.show_cursor = True
        if self._ctx is not None:
            try:
                self._ctx.add_init_script(script=CURSOR_INIT_SCRIPT)
            except Exception:  # noqa: BLE001
                pass
        return self._inject_cursor()

    def detach_cursor(self) -> bool:
        """Stop showing it, and take the node back off the current page."""
        self.show_cursor = False
        try:
            self.run_js(REMOVE_CURSOR_JS)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _inject_cursor(self) -> bool:
        """Install the cursor in the page that is on screen right now.

        An init script only covers *future* navigations, so a viewer attaching to
        a session that is already sitting on a page needs this too.
        """
        if self._ctx is not None:
            try:
                self._ctx.add_init_script(script=CURSOR_INIT_SCRIPT)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.run_js(CURSOR_INIT_SCRIPT)
            return True
        except Exception:  # noqa: BLE001
            return False
