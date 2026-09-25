"""Headless Chrome that is already logged in, driven step by step.

Cookies come from your real Chrome profile. The browser is headless throughout -
there is no window, not even an offscreen one.

The login handoff: if a site is logged out here, `wait_for_login()` blocks while
you sign in in your *real* browser, notices the cookie database change, injects
the fresh cookies into the running headless session, and returns. Nothing is
typed into the agent, and latchkey never holds your password.

This module owns the lifecycle and the cookies. The verbs it drives the page with
live in `navigation`, `automation`, `frames` and `tabs`, and they are mixed in
here; `detect` decides what a page is.
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from playwright.sync_api import Browser as PWBrowser
from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import chrome as chrome_mod
from . import cookies as ck
from . import fingerprint as fp
from . import google
from . import login as login_mod
from . import policy
from . import profile as profile_mod
from . import store
from .automation import AutomationMixin
from .automation import apply_actions  # noqa: F401  (façade: callers import it here)
from .cookies import Cookie, to_cdp, user_agent
from .detect import (BLOCK_HINTS, GENERIC_MARKERS, HINTS_FILE, LOGIN_HINTS,  # noqa: F401
                     PageState, _site_hints)
from .driver import Driver
from .events import CURSOR_INIT_SCRIPT, bus  # noqa: F401
from .frames import FramesMixin
from .navigation import NavigationMixin
from .tabs import TabsMixin

# Playwright advertises automation by default. Real Chrome headless is the real
# engine, and this removes the obvious tell (`navigator.webdriver`).
#
# The rest of a session's identity is added per session in `_launch()`, because it
# depends on the machine: `--user-agent` (Chrome's own string with the headless token
# swapped for the real version), `--screen-info` (this display, its work area, colour
# depth and scale, for new headless's virtual screen) and `--accept-lang` (the value
# Chrome's profile holds). The user agent is a launch switch and *not* Playwright's
# `user_agent=` option, for two measured reasons: Playwright's option is a per-target
# CDP override that never reaches a site's service worker - whose fetches then left
# with `HeadlessChrome` in their User-Agent header - and Playwright *derives* the
# client-hint metadata from the string it is handed, so an "Intel Mac OS X" user agent
# made this Apple Silicon machine report `architecture: x86`. The switch is browser-wide;
# the hints are put back per page from what the machine really is (`fingerprint`).
# No `viewport=` either: the window gets real bounds (`apply_window_bounds`) and the
# viewport follows from them, instead of `Emulation.setDeviceMetricsOverride`.
# Chrome opens a debugging endpoint only when it is told to at launch, and refuses
# to for a profile directory it was not told about (Chrome 136+). Real mode attaches
# to that endpoint instead of starting a browser of its own.
REAL_ENDPOINT_ENV = "LATCHKEY_CDP"

# A stand-in for "nobody is going to cancel this": the wait loop asks one question of one
# object, and "never set" is cheaper to read than a branch that says the same thing.
_NEVER = threading.Event()

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

# Real mode against the default profile stopped being possible here: Chrome 136 ignores
# --remote-debugging-port for the default user data directory, named or not.
REAL_DEFAULT_PROFILE_BLOCKED_FROM = 136

# What a session that did not choose its mode says when the site chose it instead.
MODE_NOTES = {
    "clone": ("mode: clone (a copy of your Chrome profile on the real keychain) - this site "
              "uses the profile-bound Google session, so the copy carries and rotates that "
              "login itself, same machine, same key. Your Chrome is unaffected."),
    "dedicated": ("mode: dedicated (latchkey's own Chrome profile) - this site uses a "
                  "Google account session, which latchkey holds on a profile of its own, "
                  "signed in once. Your Chrome is not copied and cannot be signed out."),
    "inject": "mode: inject (your Chrome's cookies) - this is not a Google site.",
}
class RealModeUnavailable(RuntimeError):
    """Real mode has no browser it can attach to, and the message says why and what works."""


GOOGLE_WRONG_MODE = (
    "An inject copy cannot carry a Google login: it holds neither the device-bound key nor the "
    "session's registration, so it reads as signed out and its first rotation would end the "
    "session in your Chrome. Open Google with no mode, or mode 'dedicated': latchkey's own "
    "profile holds a Google sign-in of its own and renews it there, so your Chrome is never "
    "copied and never signed out. Never ask the user to type a password or code into chat.")


# A session pinned to clone or real is holding a Google login latchkey does not own, and
# cannot renew for it. The way out is latchkey's own profile, which it can.
GOOGLE_OWN_LOGIN_HINT = (
    "This session is pinned to a mode that borrows the user's own Google login, which "
    "latchkey cannot renew for it - a borrowed account session goes stale, and can sign the "
    "user's Chrome out when Google reads two holders as a replay. Google runs on latchkey's "
    "own profile instead: open the session with no mode (or mode 'dedicated'), and if it is "
    "not signed in yet, latchkey_login signs it in once in an ordinary Chrome window. "
    "latchkey_accounts says which accounts exist and which are signed in. Never ask the user "
    "to type a password or code into chat.")


@dataclass
class SessionSpec:
    """Everything that shapes a session, in one object.

    These used to be loose keyword arguments on `Browser`. A session is decided
    as a unit - "the clone of every profile, labelled canvas, with the cursor on"
    is one decision - so it travels as one object, and the registry can be handed
    the same object to reproduce a session exactly.
    """
    mode: str = "auto"        # auto | inject | clone | dedicated | real
    profiles: list[str] | str | None = None  # None = Default, "all" = every profile
    db_path: str | None = None               # one explicit cookie DB, overrides profiles
    clone_dir: str | None = None             # where the copy-on-write clone lives
    profile_dir: str | None = None           # dedicated: latchkey's own profile, by path
    account: str | None = None               # dedicated: which of them, by name
    fresh_clone: bool = False                # re-clone even when one is fresh
    host: str | None = None                  # cookie host filter; None = every host
    label: str = ""                          # the name events are published under
    show_cursor: bool = False                # draw the agent's pointer on the page
    read_only: bool = False                  # refuse anything that sends; see policy.py
    width: int | None = None                 # None = the size of a real window on this screen
    height: int | None = None
    native_device: bool = True               # report this machine's screen, scale and dpr
    humanize: bool | None = None             # None = on, unless LATCHKEY_HUMANIZE=0
    color_scheme: str | None = None          # None = follow the OS appearance
    seed: int | None = None                  # reproduce a session's pointer and typing exactly
    auto: bool = False                       # nobody chose the mode: pick it per site

    def __post_init__(self) -> None:
        # "auto" is not a fifth way to run a browser, it is the absence of a choice - so it
        # becomes a real mode at once (inject, until a Google site says otherwise) and the
        # fact that nobody chose is kept beside it. Everything that asks `spec.mode` keeps
        # getting one of the four answers it knows.
        if self.mode in (None, "", "auto", "default"):
            self.mode = "inject"
            self.auto = True

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in
               ("mode", "auto", "db_path", "clone_dir", "profile_dir", "account",
                "fresh_clone", "host",
                "label",
                "show_cursor", "read_only", "width", "height", "native_device",
                "humanize", "color_scheme", "seed")}
        out["profiles"] = list(self.profiles) if isinstance(self.profiles, list) \
            else self.profiles
        return out


@dataclass
class InjectionReport:
    loaded: int = 0
    accepted: int = 0
    rejected: int = 0
    partitioned: int = 0
    host_only: int = 0
    dropped_non_ascii: int = 0
    non_ascii_accepted: int = 0             # odd values Chrome took when offered singly
    dropped_unpartitionable: int = 0
    withheld_live_session: int = 0          # a live login not carried; see cookies.py
    profiles: dict[str, int] = field(default_factory=dict)
    profile_errors: dict[str, str] = field(default_factory=dict)
    mode: str = "inject"
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"mode": self.mode, "loaded": self.loaded, "accepted": self.accepted,
                "rejected": self.rejected, "partitioned": self.partitioned,
                "host_only": self.host_only, "dropped_non_ascii": self.dropped_non_ascii,
                "dropped_unpartitionable": self.dropped_unpartitionable,
                "withheld_live_session": self.withheld_live_session,
                "profiles": self.profiles, "profile_errors": self.profile_errors,
                "failures": self.failures[:10]}


class Browser(Driver, NavigationMixin, AutomationMixin, FramesMixin, TabsMixin):
    """A headless Chrome carrying your cookies. Reuse one across many actions."""

    def __init__(self, host: str | None = None, *, spec: SessionSpec | None = None,
                 headless: bool = True, channel: str = "chrome",
                 use_sessions: bool = True) -> None:
        super().__init__()
        self.spec = spec or SessionSpec()
        self.host = host or self.spec.host
        if self.spec.host is None and host:
            self.spec.host = host
        self.headless = headless
        self.channel = channel
        self.use_sessions = use_sessions
        self.label = self.spec.label
        self.show_cursor = self.spec.show_cursor
        # The session's own choice, with the environment as a floor: a policy an agent
        # could switch off from inside would not be a policy.
        self.read_only = bool(self.spec.read_only or policy.read_only_floor())
        self.profiles = ck.resolve_profiles(self.spec.profiles)
        self.db_path = self.spec.db_path
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._sent: dict[tuple, str] = {}    # (domain, name, path) -> what we last injected
        self.withheld_live_session: list[tuple[str, str]] = []   # live logins left alone
        self.clone_info: dict = {}
        self._google_clone: bool = google.uses_google_session(self.host)
        self.profile_counts: dict[str, int] = {}
        self.profile_errors: dict[str, str] = {}
        self.report = InjectionReport(mode=self.spec.mode)
        self.notes: list[str] = []           # one-line things the next reply should say
        self._chrome: chrome_mod.Launched | None = None   # dedicated: the Chrome we started
        self._auto_resolved = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Browser":
        if self.spec.auto and not self._auto_resolved:
            # A session pinned to a Google host that nobody chose a mode for starts where
            # it will have to be anyway, instead of starting a copy and restarting.
            self._auto_resolved = True
            wanted = google.mode_for(self.host) if self.host else self.spec.mode
            if wanted != self.spec.mode and self.spec.mode in ("inject", "clone", "dedicated"):
                self.spec.mode = wanted
                self.notes.append(MODE_NOTES.get(wanted, f"mode: {wanted}"))
            # A host mapped to a specific login starts on that
            # profile, so the first launch is already the right one - but never override an
            # account the caller chose explicitly.
            if self.spec.mode == "dedicated" and self.host and self.spec.account is None:
                mapped = google.account_for(self.host)
                if mapped:
                    self.spec.account = mapped
                    self.notes.append(f"account: {mapped} (this site signs in with that login)")
            self._google_clone = self.spec.mode == "clone" \
                and google.uses_google_session(self.host)
            self.report.mode = self.spec.mode
        self._pw = sync_playwright().start()
        try:
            return self._launch()
        except BaseException:
            # A launch that fails part-way must not leave its Playwright driver behind: the
            # mode router starts again straight after, and a second driver on the thread of
            # a first one that was never stopped is a leak per failed attempt. Nor a Chrome
            # this session started itself, which the driver stopping would not take with it.
            if self._chrome is not None:
                try:
                    self._chrome.close(timeout_s=2.0)
                except Exception:  # noqa: BLE001
                    pass
                self._chrome = None
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            raise

    def _launch(self) -> "Browser":
        # Decide the window, the screen and the identity *before* Chrome is launched: the
        # scale factor and the language are context-time decisions, and anything that
        # arrives after the first request is something no site ever saw.
        self.fingerprint = fp.plan(width=self.spec.width, height=self.spec.height,
                                   native=self.spec.native_device, ua=user_agent(),
                                   color_scheme=self.spec.color_scheme)
        if self.spec.mode in ("inject", "clone"):
            refusal = self._device_bound_refusal()
            if refusal:
                raise RuntimeError(refusal)
        args = list(STEALTH_ARGS) + [f"--accept-lang={self.fingerprint.accept_language}",
                                     fp.screen_info_switch(self.fingerprint)]
        if self.fingerprint.ua:
            args.append(f"--user-agent={self.fingerprint.ua}")
        if self.spec.mode == "clone":
            return self._start_clone(args)
        if self.spec.mode == "dedicated":
            return self._start_dedicated(args)
        if self.spec.mode == "real":
            return self._start_real()
        self._browser = self._pw.chromium.launch(
            channel=self.channel, headless=self.headless, args=args)
        self._ctx = self._browser.new_context(
            no_viewport=True, color_scheme=self.fingerprint.color_scheme)
        if self.show_cursor:
            self._ctx.add_init_script(script=CURSOR_INIT_SCRIPT)

        if self.use_sessions:
            for script in self._storage_scripts():
                self._ctx.add_init_script(script=script)

        self._page = self._ctx.new_page()
        self._cdp = self._ctx.new_cdp_session(self._page)
        self._settle_identity()
        self._inject(self._jar())

        if self.use_sessions:
            for session in self._storage_sessions():
                cookies = store.without_live_session(session.cookies)
                if cookies:
                    self._set_cookies(cookies)
        return self

    def _settle_identity(self) -> None:
        """Make the page agree with the machine about the window and the client hints.

        The screen and the user agent are launch switches and need nothing here. The
        window is per target - `fingerprint.apply_window_bounds` gives it this plan's
        bounds, and the viewport follows - and so are the client hints, which
        `--user-agent` blanks and `fingerprint.set_user_agent` puts back from the
        machine's own numbers.

        Runs before any navigation, on the blank first page, so no site ever sees the
        pre-correction browser.
        """
        self.identity: dict = {}
        self.client_hints: dict | None = None
        spare = None
        try:
            # A restored tab in clone mode is not ours to overwrite, and the measurement
            # needs a page we can load a frame on: give it one of its own.
            if self._page.url not in ("", "about:blank", "chrome://newtab/"):
                try:
                    spare = self._ctx.new_page()
                except Exception:  # noqa: BLE001
                    spare = None
            self.identity = fp.apply_identity(self._page, self._cdp, self.fingerprint,
                                              measure_page=spare or self._page)
            self.client_hints = self.identity.get("userAgentMetadata") or None
        except Exception:  # noqa: BLE001
            pass
        finally:
            if spare is not None:
                try:
                    spare.close()
                except Exception:  # noqa: BLE001
                    pass
        self._watch_responses()
        self._watch_pages()

    def _watch_responses(self) -> None:
        """Record document responses, so a wall can be attributed from its headers.

        Attached to the context rather than the page: a tab opened by a click can be
        walled off too, and `note_response` ignores everything that is not a document.
        """
        for target in (self._ctx, self._page):
            try:
                target.on("response", self.note_response)
            except Exception:  # noqa: BLE001
                continue
        self.track_network(self._page)
        self._watch_dialogs(self._page)

    def _watch_dialogs(self, page: Any) -> None:
        """Answer dialogs and say so - a JS dialog is a step that hangs otherwise.

        Playwright blocks the page until a dialog is handled, so an `alert()` nobody answers
        is an agent that waits forever on a click it already made. Answering is therefore not
        a preference; the choice is only *which* answer, and it is: dismiss, except for
        `beforeunload`, which has to be accepted or the navigation never happens. What was
        answered is recorded and surfaces in the reply, because a confirm the agent did not
        read is exactly the thing it needs to know about when the page does not move.
        """
        def on_dialog(dialog: Any) -> None:
            kind = getattr(dialog, "type", "dialog")
            answer = "accepted" if kind == "beforeunload" else "dismissed"
            try:
                dialog.accept() if kind == "beforeunload" else dialog.dismiss()
            except Exception:  # noqa: BLE001
                answer = "could not be answered"
            try:
                self.dialogs.append({"type": kind, "message": str(dialog.message)[:200],
                                     "answered": answer})
                del self.dialogs[:-5]        # only the last few are ever worth reading
            except Exception:  # noqa: BLE001
                pass
        try:
            page.on("dialog", on_dialog)
        except Exception:  # noqa: BLE001
            pass

    def _watch_pages(self) -> None:
        """Give every new page the same treatment, and watch its responses.

        Window bounds and the user-agent override apply per target, so a page opened
        later does not inherit them - a second tab would be the one page in the session
        with no window around it and blank client hints.
        """
        def on_page(page: Any) -> None:
            try:
                page.on("response", self.note_response)
            except Exception:  # noqa: BLE001
                pass
            self.track_network(page)
            self._watch_dialogs(page)
            # One CDP session per page, from the cache, not two fresh ones per tab that
            # nothing then holds: a session that opens tabs for a while was otherwise
            # leaving a pair of attached sessions behind for each of them.
            try:
                cdp = self.cdp_for(page)
            except Exception:  # noqa: BLE001
                return
            try:
                fp.apply_window_bounds(cdp, self.fingerprint)
            except Exception:  # noqa: BLE001
                pass
            try:
                # The user agent override is per target as well, and it carries the hints
                # measured for the first page: a later tab without it would report a
                # different device than the tab next to it.
                if self.client_hints:
                    fp.set_user_agent(cdp, self.fingerprint, self.client_hints)
            except Exception:  # noqa: BLE001
                pass
            try:
                page.on("close", lambda _p=page: self._forget_cdp(_p))
            except Exception:  # noqa: BLE001
                pass
        try:
            self._ctx.on("page", on_page)
        except Exception:  # noqa: BLE001
            pass

    def _start_real(self) -> "Browser":
        """Drive the Chrome you are using, instead of a second browser beside it - no copy.

        Every other mode puts a browser next to yours and hands it a copy of something:
        your cookies, a cloned profile, a login. This one does not. latchkey attaches to
        your Chrome over CDP and drives your actual tabs, so there is nothing to sign
        into twice, nothing for a service to see replayed, and no second session to
        explain to Google. One client, one login - which is also why this mode cannot
        sign you out of anything: it *is* the browser that would have been signed out.

        It is also the only mode where every number a page reads about the window is
        genuine rather than emulated, because the window is genuine. So no identity is
        applied here on purpose: a real browser with an emulated window pinned onto it
        would be strictly worse than the real window it already has.

        Where the endpoint comes from. Chrome opens a debugging port only when told to at
        launch, and a Chrome that is *already running* silently ignores the flag, so:

            # quit Chrome first, then
            open -a "Google Chrome" --args \
                 --user-data-dir="$HOME/Library/Application Support/Google/Chrome" \
                 --remote-debugging-port=9222
            LATCHKEY_CDP=http://127.0.0.1:9222

        or leave LATCHKEY_CDP unset and this launches your Chrome with that port itself,
        naming the profile directory explicitly (Chrome 136 and later refuses remote
        debugging unless the directory is named, which is the whole reason the profile
        path is spelled out above rather than left to default).

        What it costs, plainly: while that port is open, anything on this machine that
        can reach it can drive your browser; the agent's clicks and typing land in the
        browser you are using; and it has your real tabs, not a fresh window. `read_only`
        still holds, and `close()` only disconnects - it never closes your browser.
        """
        endpoint = self._real_endpoint()
        self.clone_info = {"mode": "real", "endpoint": endpoint,
                           "launched": self._real_launched}
        try:
            self._browser = self._pw.chromium.connect_over_cdp(endpoint)
        except Exception as exc:  # noqa: BLE001 - the message is the point
            raise RuntimeError(self._real_unreachable(endpoint, str(exc))) from exc
        if not self._browser.contexts:
            raise RuntimeError(
                f"attached to {endpoint}, but it reported no browser context - that "
                f"endpoint is a page rather than a browser, or not a Chrome at all.")
        # contexts[0] is the real profile's default context: your tabs, your logins.
        self._ctx = self._browser.contexts[0]
        pages = [p for p in self._ctx.pages if not p.url.startswith("devtools://")]
        self._page = pages[-1] if pages else self._ctx.new_page()
        self._cdp = self._ctx.new_cdp_session(self._page)
        self.identity, self.client_hints = {}, None
        self.report = InjectionReport(mode="real", loaded=0, accepted=0)
        return self

    def _real_endpoint(self) -> str:
        """Attach to a Chrome that is already listening - or say at once why there is none.

        Three places an endpoint can come from, in order:

          * `LATCHKEY_CDP`, a Chrome somebody started with a debugging port;
          * the `DevToolsActivePort` your running Chrome writes into its own profile when
            remote debugging has been allowed for it (chrome://inspect/#remote-debugging,
            Chrome 144 and later; Chrome asks you to approve the connection);
          * starting your Chrome with a port - which only works before Chrome 136.

        The last one used to be tried unconditionally, and on this machine it cannot work
        twice over: Chrome 136 and later ignore `--remote-debugging-port` for the default
        profile directory whether it is named or not, and a Chrome that is already running
        hands a second launch its window and exits. So the launch would open a stray window
        in your browser and then wait 25 seconds for a port that was never going to open.
        Now it is not attempted on a Chrome that refuses it, and the error says why.
        """
        given = (os.environ.get(REAL_ENDPOINT_ENV) or "").strip()
        if given:
            self._real_launched = False
            return given if "://" in given else f"http://127.0.0.1:{given}"
        active = chrome_mod.devtools_active_port(profile_mod.CHROME_ROOT)
        if active:
            self._real_launched = False
            port, path = active
            return f"ws://127.0.0.1:{port}{path}" if path else f"http://127.0.0.1:{port}"
        version = chrome_mod.major_version()
        running = chrome_mod.lock_owner(profile_mod.CHROME_ROOT)
        if version is None or version >= REAL_DEFAULT_PROFILE_BLOCKED_FROM:
            raise RealModeUnavailable(self._real_refused(version, running))
        if running:
            raise RealModeUnavailable(self._real_unreachable(
                "(not started)", f"your Chrome is already running (pid {running}), and a second "
                                 f"launch on the same profile only hands it a window"))
        self._real_launched = True
        port = self._free_port()
        log = os.path.join(tempfile.gettempdir(), "latchkey-real-chrome.log")
        with open(log, "w", encoding="utf-8") as handle:
            subprocess.Popen([profile_mod.chrome_binary(),
                              f"--user-data-dir={profile_mod.CHROME_ROOT}",
                              f"--remote-debugging-port={port}",
                              "--no-first-run", "--no-default-browser-check"],
                             stdout=handle, stderr=subprocess.STDOUT)
        endpoint = f"http://127.0.0.1:{port}"
        if not self._wait_for_endpoint(endpoint):
            raise RuntimeError(self._real_unreachable(endpoint, self._tail(log)))
        return endpoint

    @staticmethod
    def _real_refused(version: int | None, running: int | None) -> str:
        """Why real mode cannot attach to the everyday Chrome, and what does work instead."""
        which = f"Chrome {version}" if version else "This Chrome"
        state = f" (it is running now, pid {running})" if running else ""
        return (
            f"real mode has no browser to attach to. {which}{state} ignores "
            f"--remote-debugging-port for your default profile directory - Chrome 136 and "
            f"later refuse it whether or not the directory is named - so latchkey will not "
            f"launch it that way: the launch would only open a window in your running Chrome.\n"
            f"What works instead:\n"
            f"  Google/Gmail/YouTube  mode='clone' (or no mode) opens a copy of your profile\n"
            f"  other sites           mode='inject' (or no mode) copies your Chrome's cookies\n"
            f"  your own window       allow remote debugging at chrome://inspect/#remote-debugging\n"
            f"                        (Chrome 144+, asks you to approve), or start a Chrome on a\n"
            f"                        non-default --user-data-dir with a port and set LATCHKEY_CDP")

    @staticmethod
    def _free_port() -> int:
        """A local port nothing is on, released again for Chrome to take."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def _wait_for_endpoint(endpoint: str, timeout_s: float = 25.0) -> bool:
        """Wait for a Chrome to answer on its debugging endpoint, as it starts. ~1s when it is up."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{endpoint}/json/version", timeout=2):
                    return True
            except Exception:  # noqa: BLE001 - not up yet is the normal case, not an error
                time.sleep(0.4)
        return False

    @staticmethod
    def _tail(path: str, lines: int = 8) -> str:
        """The last lines of a log, because Chrome's own words are better than ours."""
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return "".join(handle.readlines()[-lines:]).strip()
        except OSError:
            return ""

    def _real_unreachable(self, endpoint: str, detail: str) -> str:
        """Say why there is no endpoint, for a Chrome old enough to have offered one."""
        return (
            f"no debugging endpoint at {endpoint}, so there is no browser to drive.\n"
            f"Chrome only opens one when it is told to at launch, and a Chrome that is\n"
            f"already running ignores the flag - so quit Chrome (\u2318Q) and try again, or\n"
            f"start a Chrome with a port yourself and set LATCHKEY_CDP to it. From Chrome 136\n"
            f"the default profile directory refuses a port entirely; see mode='dedicated'.\n"
            f"Chrome said: {detail[-400:] or '(nothing)'}")

    def navigation_refusal(self, url: str) -> str:
        """Refuse to carry a copied session to an origin that binds one - at the url.

        The check used to run once, at start-up, against the host the session pinned. A
        session opened without a host pinned nothing, so `latchkey_session_open()` then
        `latchkey_open("https://mail.google.com")` walked straight past it in inject
        mode - which is the default, and the exact route that ends the session in the
        user's own Chrome. Asking at the navigation asks about the site being opened.
        """
        from urllib.parse import urlparse
        host = urlparse(url).netloc.split("@")[-1].split(":")[0]
        return self._device_bound_refusal(host)

    def _device_bound_refusal(self, host: str | None = None) -> str:
        """Why an *inject* session cannot carry a device-bound host, or "" when it is fine.

        Chrome registers a *device bound session* for origins that ask for one, and Google
        does: the account's cookies are afterwards refreshed by signing a challenge with a
        private key in the OS keystore. The key is one per profile, kept in the machine's
        keystore rather than the profile directory, and it cannot be exported.

        Two ways to hold a copy fail differently, and only one of them fails:

          * `inject` sets decrypted cookies over CDP into a *fresh* profile that carries no
            registration and runs on a mock keychain. It has neither the binding nor the key,
            so it can present the cookies once and never renew them - and the first time it
            triggers a rotation it retires the value the real Chrome still holds. That is the
            "Gmail signed me out" report, and it is real, so this refuses it.

          * `clone` copies the whole profile - the Device Bound Sessions registration with it -
            and runs on the *real* keychain, so it resolves the same per-profile key and signs
            the rotation itself. Same machine, same signed app, same key. Measured here: a
            clone rotated its own bound cookies five hours after it was made, while the real
            Chrome stayed signed in. So clone is not refused; it is the mode these hosts use.

        `real` and `dedicated` each hold a key of their own and are never a copy at all.
        """
        if (os.environ.get("LATCHKEY_ALLOW_BOUND_COPY") or "").strip():
            return ""
        if self.spec.mode != "inject":
            return ""       # clone carries the registration + real keychain; real/dedicated hold their own key
        wanted = (host if host is not None else (self.host or "")).lstrip(".").lower()
        if not wanted:
            return ""       # no host to check against yet
        bound = profile_mod.bound_hosts()
        if not any(wanted == one or wanted.endswith("." + one) for one in bound):
            return ""
        return (
            f"{wanted} is device bound in your Chrome profile, so an *inject* copy of its\n"
            f"session cannot work - and trying is not harmless. Chrome keeps a *device bound\n"
            f"session* for {', '.join(sorted(bound))}: the cookies are refreshed by signing a\n"
            f"challenge with a per-profile key in your keystore, and an inject session carries\n"
            f"neither that key nor the session's registration. It can present the cookies once\n"
            f"and never renew them; its first rotation retires the value the real Chrome holds,\n"
            f"which is why this reads as signed out here *and* signs you out of your Chrome.\n\n"
            f"Use a copy that keeps the binding, or a browser that owns its own key:\n"
            f"  mode='clone'      a copy of your profile on the real keychain - it rotates the\n"
            f"                    session itself, same machine, same key. This is the default for\n"
            f"                    Google when no mode is chosen.\n"
            f"  mode='real'       drive your own Chrome - the profile that has the key\n"
            f"  mode='dedicated'  sign in once on latchkey's own profile; it registers its own\n"
            f"LATCHKEY_ALLOW_BOUND_COPY=1 copies in inject anyway, for anyone who wants to watch it fail.")

    @staticmethod
    def _clear_clone_holder(dest: str, timeout_s: float = 10.0) -> None:
        """Make sure no Chrome is holding a persistent clone directory before it is relaunched.

        A lock owner here is normally a latchkey Chrome that has not finished exiting - the
        directory is this server's own, under its owner id, so a Chrome still on it belongs
        to this process or to a dead one. What it must never be is another *live* server's
        window: that would take a page out from under a model that is working, which is the
        bug this guards (a pinned LATCHKEY_CLONE_DIR or LATCHKEY_GOOGLE_CLONE_DIR is one
        directory for every server). So: wait a moment for an orderly exit, then end it only
        if nobody alive is claiming it - otherwise say whose it is and leave it alone.
        """
        owner = chrome_mod.lock_owner(dest)
        if not owner:
            return
        deadline = time.time() + min(timeout_s, 3.0)
        while time.time() < deadline and chrome_mod.lock_owner(dest) == owner:
            time.sleep(0.2)
        if chrome_mod.lock_owner(dest) == owner:
            held = chrome_mod.live_owner(owner)
            if held and held.get("server_pid") not in (None, os.getpid()):
                raise chrome_mod.ProfileBusy(owner, dest, (
                    f"{dest} is open in Chrome pid {owner}, which another latchkey server "
                    f"(pid {held.get('server_pid')}) opened"
                    + (f" for its session {held['session']!r}" if held.get("session") else "")
                    + ". Closing it would pull the page out from under that model, so this "
                      "server does not. Clone directories are per server now (under "
                      "~/.latchkey/clones), so seeing this means LATCHKEY_CLONE_DIR or "
                      "LATCHKEY_GOOGLE_CLONE_DIR is pinning one path for every server: "
                      "unset it, or give each server its own."))
            chrome_mod.terminate(owner, timeout_s)
        try:
            profile_mod._remove_singletons(dest)
        except Exception:  # noqa: BLE001
            pass

    def _start_clone(self, args: list[str]) -> "Browser":
        """Open Chrome on a copy-on-write clone of the real profile.

        Nothing is transferred: cookies (partitions intact), localStorage,
        IndexedDB, service workers and every profile are simply present, because
        Chrome opened the actual directory. Costs ~4s and almost no disk.

        The clone is the mode most likely to meet a wall, and the reason is worth
        keeping in view: the profile's `cf_clearance` and `__cf_bm` were issued to the
        *real* Chrome's screen, scale and colour depth, so a clone that presents a
        1280x820 viewport at dpr 1 is a client holding somebody else's pass. Matching
        the machine is what makes the clone look like the browser the cookie belongs to.

        A signed-in profile also brings its account bookkeeping - and its device-bound
        session registration. On the same machine the clone runs on the real keychain
        (`--use-mock-keychain` is dropped below), so it resolves the same per-profile key
        and rotates the bound Google session itself rather than presenting a token it can
        never renew. Measured here: a clone re-minted its bound `__Secure-1PSIDTS` five
        hours after it was made, while the real Chrome stayed signed in. So a clone of a
        Google-signed-in profile is a working second client of the same account, not the
        sign-out it once was; it still reports what it inherited, for visibility.
        """
        # Google gets its own clone directory - apart from the throwaway one every other clone
        # session shares, so the two never collide - and it is re-seeded from the real profile
        # on *every* open. That freshness is load-bearing and cannot be cached: a device-bound
        # session revokes a *superseded* token (its whole point is catching a second client on a
        # stale one), and a clone rotates its own copy the moment it is used - so a clone kept
        # from a previous open is a client holding a token the real Chrome has moved past, and
        # opening it gets it signed out. `source_is_newer` cannot see this (the clone's own
        # writes make it look newer than the source it drifted from), so Google does not rely on
        # it: it re-clones from the real Chrome's current cookies each time, ~a few seconds. An
        # explicit clone_dir always wins.
        google_clone = self._google_clone or google.uses_google_session(self.host)
        if self.spec.clone_dir:
            dest, force = self.spec.clone_dir, self.spec.fresh_clone
        elif google_clone:
            dest, force = google.google_clone_dir(), True
        else:
            dest, force = profile_mod.CLONE_DIR, self.spec.fresh_clone
        if google_clone and not self.spec.clone_dir:
            # The Google directory hosts one Chrome at a time. A previous session's Chrome may
            # still be finishing its orderly exit (it writes the bound-session state on the way
            # out), and Chrome allows one browser per profile - so a relaunch would block on the
            # single-instance lock, and the re-seed's rmtree would fail. Clear any prior holder
            # first; this directory is latchkey's alone, so whatever holds it is our Chrome.
            self._clear_clone_holder(dest)
        self.clone_info = profile_mod.clone(dest=dest, force=force)
        if google_clone:
            # Drop the copied rotating token so the clone mints its own fresh one on first use
            # (it holds the key), rather than presenting a copy the real Chrome may have moved
            # past - which a device-bound session revokes. This is what makes a Google clone
            # open signed in every time, not only when the copied token happened to be current.
            stripped = google.strip_rotating_session(self.clone_info["path"])
            if stripped:
                self.clone_info["stripped_rotating"] = stripped
        self._browser = None
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.clone_info["path"], channel=self.channel,
            headless=self.headless, no_viewport=True,
            color_scheme=self.fingerprint.color_scheme,
            ignore_default_args=["--use-mock-keychain", "--password-store=basic"],
            args=args)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        if self.show_cursor:
            self._inject_cursor()
        self._cdp = self._ctx.new_cdp_session(self._page)
        self._settle_identity()
        accounts = profile_mod.signed_in_accounts(self.clone_info["path"])
        bound = profile_mod.bound_hosts() | profile_mod.bound_hosts(self.clone_info["path"])
        if bound:
            self.clone_info["device_bound"] = sorted(bound)
            self.clone_info["note"] = (
                "this clone carries your device bound session for " + ", ".join(sorted(bound))
                + " and runs on the real keychain, so it rotates that session itself - it "
                "reads as signed in and does not sign your Chrome out. (An inject copy could "
                "do neither, which is why inject still refuses these hosts.)")
        if accounts:
            self.clone_info["signed_in_accounts"] = accounts
        self.report = InjectionReport(mode="clone",
                                      loaded=self.clone_info.get("rows", -1),
                                      accepted=len(self._ctx.cookies()))
        return self

    def _start_dedicated(self, args: list[str]) -> "Browser":
        """Open Chrome on latchkey's own long-lived profile, so its login is *its own*.

        This is the mode for a site you are signed in to with an account you would
        rather not disturb - Google above all. Two browsers cannot share one login: an
        account session carries a freshness token that the service re-issues to
        whichever client used it last, so a second browser holding a copy retires the
        value the first one is still holding, and the service reads the first one's
        next request as a replayed session and ends it. That is a sign-out in the real
        Chrome, on a profile nothing here ever wrote to.

        Nothing is copied in this mode. The directory is seeded by nothing, you sign
        into it once (`latchkey login`, an ordinary Chrome window), and after that the
        session lives and rotates inside it and nowhere else. Same account, second
        session - which is what a phone signed into that account already has.

        How it is launched matters as much as what is in it. The sign-in window wrote its
        cookies under the real Keychain, and Playwright's own persistent launch brings back
        its defaults - the mock keychain among them, which makes those cookies unreadable,
        and a flag list Google can recognise. So the same Chrome binary is started here
        with a short explicit list (`chrome.launch_for_automation`) and attached to over
        CDP, and closing it is a graceful `Browser.close` so a rotated cookie is written
        before the process goes.
        """
        path = profile_mod.dedicated_dir(self.spec.profile_dir, self.spec.account)
        released = login_mod.release_for_automation(path)
        self.clone_info = {"path": path, "mode": "dedicated",
                           "profile": profile_mod.account_of_path(path)
                                      or self.spec.account or profile_mod.DEFAULT_ACCOUNT,
                           "reused": os.path.isdir(os.path.join(path, "Default"))}
        if released:
            self.clone_info.update(released)
        launch_args = [flag for flag in args if flag not in ("--no-first-run",
                                                             "--no-default-browser-check")]
        launch_args.append(f"--window-size={self.fingerprint.width},{self.fingerprint.height}")
        self._chrome = chrome_mod.launch_for_automation(path, headless=self.headless,
                                                        args=launch_args)
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self._chrome.endpoint)
            if not self._browser.contexts:
                raise RuntimeError("the dedicated Chrome answered with no browser context")
            self._ctx = self._browser.contexts[0]
        except Exception:
            self._chrome.close(timeout_s=2.0)
            self._chrome = None
            raise
        pages = [page for page in self._ctx.pages
                 if not page.url.startswith(("devtools://", "chrome-extension://"))]
        self._page = pages[0] if pages else self._ctx.new_page()
        self._cookie_hosts = None        # nothing was injected: ask Chrome per navigation
        self._cdp_sessions.clear()
        if self.show_cursor:
            self._inject_cursor()
        self._cdp = self._ctx.new_cdp_session(self._page)
        self._settle_identity()
        self.clone_info["google_signed_in"] = google.signed_in(login_mod.cookie_db(path))
        self.report = InjectionReport(mode="dedicated", loaded=0,
                                      accepted=self._cookie_count())
        return self

    def _cookie_count(self) -> int:
        """How many cookies this context already holds, or -1 if the browser went away first.

        A persistent profile has a lock, and a Chrome that is still letting the directory go
        exits the new one on the spot. That is worth reporting as "nothing was loaded", not
        as an exception from the middle of a launch - the caller can see the browser is gone.
        """
        try:
            return len(self._ctx.cookies())
        except Exception:  # noqa: BLE001
            return -1

    def _storage_sessions(self) -> list[store.Session]:
        """Saved sessions that this browser's host filter lets through."""
        return [s for s in store.load_all()
                if not self.host or self.host in s.site or s.site in (self.host or "")]

    def _storage_scripts(self) -> list[str]:
        scripts = []
        for session in self._storage_sessions():
            script = store.local_storage_init_script(session)
            if script:
                scripts.append(script)
        return scripts

    def close(self) -> None:
        if getattr(self, "_chrome", None) is not None:
            self._close_launched()
            return
        if self.spec.mode == "real":
            # Attached, not owned. `_ctx.close()` here would close the profile context -
            # i.e. the user's own tabs - and `_browser.close()` talks to a browser we did
            # not start. Dropping the connection is the whole of hanging up.
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._ctx = self._browser = self._page = self._pw = self._cdp = None
            self._probe.invalidate()
            return
        for closer in (self._ctx.close if self._ctx else None,
                       self._browser.close if self._browser else None,
                       self._pw.stop if self._pw else None):
            if closer:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        self._ctx = self._browser = self._page = self._pw = self._cdp = None
        self._probe.invalidate()

    def _close_launched(self) -> None:
        """Put away a Chrome this session started itself: gracefully, then for certain.

        `connect_over_cdp` only ever disconnects, so closing the Playwright side alone would
        leave the browser running and the profile locked. `Browser.close` first, which is
        Chrome's own orderly exit - the cookie store and the bound-session state are written
        on the way out - and then the process is waited for, and ended if it will not go.
        """
        try:
            if self._browser is not None:
                self._browser.new_browser_cdp_session().send("Browser.close")
        except Exception:  # noqa: BLE001 - it may already be on its way out
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._chrome.close(timeout_s=10.0)
        finally:
            self._chrome = None
            self._ctx = self._browser = self._page = self._pw = self._cdp = None
            self._cdp_sessions.clear()
            self._probe.invalidate()

    def take_notes(self) -> list[str]:
        """The one-line notes waiting for the next reply, handed over once."""
        notes, self.notes = list(self.notes), []
        return notes

    def route_for(self, url: str) -> None:
        """Move a session that did not choose its mode to the one this site needs.

        Google in `clone` (a copy of your profile, so their login comes with it), everything
        else in `inject` (your Chrome's cookies). Only a session whose mode nobody chose is
        ever moved, and only between those: a caller who asked for clone, real or a specific
        mode keeps it. A move is a restart - about a second, and the tabs of the old browser
        go - and it is said in one
        line on the reply, because a session that changed under an agent without saying so
        is a session it cannot reason about.

        Before anything is closed, the profile is checked: if the sign-in window is still
        holding it, the answer is an error that says so, and the browser that was working
        is still there.
        """
        if not getattr(self.spec, "auto", False) or self._pw is None:
            return
        if self.spec.mode not in ("inject", "clone", "dedicated"):
            return
        host = google.host_of(url)
        if not host:
            return
        wanted = google.mode_for(host)
        # A configured Google-SSO portal can name one specific account.
        # Auto-pick it only when the caller named no account: an explicit account is the
        # session's identity and is never overridden, and a plain Google host maps to nothing
        # so a no-account session stays on the default profile. When the account has to change
        # even though the mode does not, a relaunch is still due - otherwise the session stays
        # on the wrong profile and the portal's account chooser stalls it.
        mapped = google.account_for(host) if wanted == "dedicated" else None
        wanted_account = mapped if (mapped is not None and self.spec.account is None) else None
        current_account = self.spec.account or profile_mod.DEFAULT_ACCOUNT
        account_change = wanted_account is not None and wanted_account != current_account
        if wanted == self.spec.mode and not account_change:
            return
        if wanted == "dedicated":
            # Raises while the profile cannot be had (the sign-in window is mid sign-in, or
            # another Chrome holds it) - before the working browser is closed, not after. The
            # account released is the one currently held; the target is acquired by start().
            login_mod.release_for_automation(self.spec.profile_dir,
                                             account=self.spec.account)
        # A move to clone for a Google host uses the persistent Google clone; a move away
        # from Google drops the flag so a later inject does not read it.
        self._google_clone = wanted == "clone" and google.uses_google_session(host)
        previous = self.spec.mode
        previous_account = self.spec.account
        self.close()
        self.spec.mode = wanted
        if wanted_account is not None:
            self.spec.account = wanted_account
        try:
            self.start()
        except Exception as first:
            self.spec.mode = previous
            self.spec.account = previous_account
            try:
                self.start()
            except Exception as second:  # noqa: BLE001
                # Both failed, so there is no browser behind this session any more - say that,
                # rather than re-raise the first error and let the next call trip over a None.
                raise RuntimeError(
                    f"could not open {host} in {wanted} mode ({type(first).__name__}: "
                    f"{str(first)[:300]}), and the session could not go back to {previous} "
                    f"mode either ({type(second).__name__}: {str(second)[:200]}). Close this "
                    f"session (latchkey_session_close) and open it again.") from first
            raise
        self.report.mode = self.spec.mode
        if account_change:
            self.notes.append(f"account: {wanted_account} (this site signs in with that login)")
        else:
            self.notes.append(MODE_NOTES.get(wanted, f"mode: {wanted}"))

    def __enter__(self) -> "Browser":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- cookies -----------------------------------------------------------

    def _jar(self) -> list[Cookie]:
        """Every requested profile's cookies, merged, minus any live login we must not carry.

        What is held back is recorded on the browser, so a session that comes back
        "logged out of Google" can say why in one line instead of looking like a bug.
        """
        if self.db_path:
            jar = ck.load(self.host, self.db_path)
        else:
            jar, counts, errors = ck.load_many(self.profiles, self.host)
            self.profile_counts = counts
            self.profile_errors = errors
        jar, held = ck.split_live_session(jar)
        self.withheld_live_session = [(c.name, c.host.lstrip(".")) for c in held]
        return jar

    @staticmethod
    def _cookie_key(param: dict) -> tuple:
        """What identifies a cookie for "is this the same one?" - and for deleting it."""
        domain = param.get("domain") or urlparse(param.get("url") or "").netloc
        return (str(domain).lower(), param["name"], param.get("path") or "/")

    def _set_cookies(self, params: list[dict]) -> int:
        """Set cookies over CDP, in one batch with a per-cookie fallback."""
        if not params:
            return 0
        try:
            self._cdp.send("Network.setCookies", {"cookies": params})
            return len(params)
        except Exception:  # noqa: BLE001
            return self._set_one_by_one(params)

    def _set_one_by_one(self, params: list[dict]) -> int:
        """Offer each cookie on its own, and count the ones Chrome took.

        For the batch's fallback, and for the values CDP may not accept at all: a cookie
        with a byte outside printable ASCII used to be dropped before it was ever
        offered, which is a login thrown away on a guess about what Chrome will take.
        """
        accepted = 0
        for param in params:
            try:
                self._cdp.send("Network.setCookie", param)
                accepted += 1
            except Exception as exc:  # noqa: BLE001
                if len(self.report.failures) < 10:
                    self.report.failures.append(f"{param.get('name')}: {str(exc)[:60]}")
        return accepted

    def _inject(self, jar: list[Cookie]) -> None:
        params, risky, dropped = to_cdp(jar)
        self._cookie_hosts = {p["domain"].lstrip(".").lower() for p in params + risky
                              if p.get("domain")}
        self.report = InjectionReport(
            loaded=len(jar), partitioned=sum(1 for c in jar if c.partitioned),
            withheld_live_session=len(self.withheld_live_session),
            host_only=sum(1 for c in jar if c.is_host_only),
            dropped_unpartitionable=dropped["unpartitionable"],
            profiles=dict(self.profile_counts),
            profile_errors=dict(self.profile_errors))
        accepted = self._set_cookies(params)
        odd_accepted = self._set_one_by_one(risky) if risky else 0
        self.report.accepted = accepted + odd_accepted
        self.report.rejected = len(params) + len(risky) - self.report.accepted
        self.report.dropped_non_ascii = len(risky) - odd_accepted
        self.report.non_ascii_accepted = odd_accepted
        for p in params + risky:
            self._sent[self._cookie_key(p)] = p["value"]

    def refresh(self) -> dict:
        """Pick up the real profile's current state.

        inject mode: re-read the cookie database and set only what changed.
        clone  mode: re-clone and restart. ~4s, but page state is lost.
        dedicated: restart on its own profile - its own login, untouched.
        """
        self._probe.invalidate()
        self.forget_jar()
        if self.spec.mode in ("clone", "dedicated"):
            self.close()
            self.start()
            return {"mode": self.spec.mode, "restarted": True, **self.clone_info}

        jar = self._jar()
        params, risky, dropped = to_cdp(jar)
        offered = params + risky
        changed = [p for p in offered if self._sent.get(self._cookie_key(p)) != p["value"]]
        # A sync used to be additive only, so signing out in the real Chrome never
        # reached the session: the cookie was gone from the database and still in the
        # browser. What the profile no longer has, this no longer holds.
        gone = set(self._sent) - {self._cookie_key(p) for p in offered}
        removed = self._delete_cookies(gone)

        before = self.report.accepted
        accepted = self._set_cookies(changed)
        for p in changed:
            self._sent[self._cookie_key(p)] = p["value"]
        return {"cookie_db_total": len(jar), "new_or_changed": len(changed),
                "accepted": accepted, "rejected": len(changed) - accepted,
                "removed": removed,
                "session_accepted_total": max(0, before + accepted - removed),
                "dropped_non_ascii": dropped["non_ascii"],
                "dropped_unpartitionable": dropped["unpartitionable"]}

    def _delete_cookies(self, keys) -> int:
        """Drop cookies the profile no longer has. Returns how many went."""
        removed = 0
        for domain, name, path in list(keys):
            try:
                self._cdp.send("Network.deleteCookies",
                               {"name": name, "domain": domain, "path": path})
                removed += 1
            except Exception:  # noqa: BLE001
                continue
            finally:
                self._sent.pop((domain, name, path), None)
        return removed

    def jar_stamp(self) -> tuple:
        """What the cookie databases look like from outside, without opening one.

        The login handoff asks "has anything changed?" every few seconds for up to five
        minutes. Answering it by reading the store means a copy, a query and several
        thousand decryptions each time - about a hundred round numbers of work to learn
        that nothing happened. Chrome cannot write a cookie without touching one of these
        files, so their mtimes answer the same question for nothing, and the real read
        happens only on the poll where something actually moved.
        """
        explicit = getattr(self, "db_path", None)
        wanted = getattr(self, "profiles", None) or []
        paths = [explicit] if explicit else \
            [path for name, path in ck.profiles().items() if name in wanted]
        return tuple(ck.db_stamp(path) for path in paths if path)

    def cookie_signature(self) -> str:
        """Fingerprint of the profile's cookies, used to confirm a fresh login.

        The expensive half of the poll, so it runs only when `jar_stamp` says a file
        moved: a write that changed nothing this session cares about still has to be
        told apart from a login, and only the contents can do that.
        """
        jar = self._jar()
        h = hashlib.sha256()
        for c in sorted(jar, key=lambda c: (c.host, c.name)):
            h.update(f"{c.host}|{c.name}|{c.value}|".encode("utf-8", "replace"))
        return h.hexdigest()

    def save_session(self, site: str | None = None) -> str:
        """Persist this session (cookies + localStorage) under ~/.latchkey/sessions."""
        site = site or self.host or _site_of(self.page.url)
        cookies = store.snapshot_cookies(self._cdp)
        storage: dict[str, dict[str, str]] = {}
        try:
            data = self.run_js(store.CAPTURE_LOCAL_STORAGE_JS) or {}
            if data:
                storage[self.run_js("() => location.origin")] = data
        except Exception:  # noqa: BLE001
            pass
        path = store.save(site, cookies, storage)
        self._publish("save_session", site=site, path=path, cookies=len(cookies))
        return path

    # -- human intervention (see intervene.py) -----------------------------

    def assist_snapshot(self, url: str | None = None) -> dict:
        """What a human-intervention window needs from this session, as plain data.

        Read on the session's own thread and handed to the window: the page's verdict and
        wall (so the agent can tell the user what stopped it), this session's cookies in
        setCookies shape (minus any live account session, which `snapshot_cookies` never
        carries), and the session's user agent and window size. The identity matters as much
        as the cookies - a clearance the human earns in the window is bound to the client the
        window presents, so it must be the client this session already is, or the pass does
        not fit when it comes back.
        """
        target = policy.check_url(url) if url else self.page.url
        nav_error = None
        if url and target not in ("", "about:blank") and target != self.page.url:
            # A navigation that fails outright - a connection reset, an anti-bot refusal at
            # the network layer - is itself a reason to hand off to a human, so it must not
            # sink the snapshot: the window can still be seeded from the cookie jar (which
            # `snapshot_cookies` reads without loading the page) and opened at the url, where
            # a real headed browser often gets through where the headless client did not.
            try:
                self.goto(target)
            except Exception as exc:  # noqa: BLE001
                nav_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
        try:
            state = self.state()
        except Exception:  # noqa: BLE001
            state = None
        cookies = store.snapshot_cookies(self._cdp)
        fp_obj = getattr(self, "fingerprint", None)
        out = {"url": target or self.page.url,
               "verdict": state.verdict if state is not None else "unclear",
               "wall": state.wall if state is not None else {},
               "title": state.title if state is not None else "",
               "cookies": cookies, "cookie_count": len(cookies),
               "user_agent": getattr(fp_obj, "ua", None) or user_agent(),
               "width": int(getattr(fp_obj, "width", 0) or 1280),
               "height": int(getattr(fp_obj, "height", 0) or 820)}
        if nav_error:
            out["nav_error"] = nav_error
        return out

    def assist_apply(self, cookies: list[dict], url: str | None = None,
                     settle_ms: int = 3000) -> dict:
        """Carry the cookies a human earned in the window back into this session, and re-read.

        The window's clearance is set over CDP the same way an inject is, the sent-cookie
        record is kept in step (so a later `refresh` does not undo it), and the page is read
        again so the caller learns whether the wall is actually gone. A page that now reads
        past the wall is saved, so the pass survives a restart.
        """
        applied = self._set_cookies(cookies) if cookies else 0
        for param in cookies or []:
            try:
                self._sent[self._cookie_key(param)] = param["value"]
            except Exception:  # noqa: BLE001
                continue
        self.forget_jar()
        self._probe.invalidate()
        target = policy.check_url(url) if url else self.page.url
        state = self.goto(target, settle_ms)
        self._publish("assist", action="applied", accepted=applied, offered=len(cookies or []),
                      **state.summary())
        if state.verdict not in ("challenged", "blocked"):
            try:
                self.save_session()
            except Exception:  # noqa: BLE001 - persistence is a nicety, not the result
                pass
        return {"accepted": applied, "offered": len(cookies or []), "state": state.as_dict()}

    def wait_for_login(self, url: str, *, timeout_s: int = 300, poll_s: float = 3.0,
                       settle_ms: int = 3000,
                       cancel: threading.Event | None = None) -> dict:
        """Block while the human logs in on their real browser, then transfer.

        Polls the cookie database for a change, re-injects, reloads the page and
        reports the verdict. Returns status: logged-in | timeout | already-logged-in |
        abandoned - or, without waiting at all, wrong-mode | login-required.

        Which store is watched depends on the mode, because watching the wrong one is a
        wait that can never end. A copy (inject, clone) watches your Chrome: you sign in
        there and the cookies are carried across. `dedicated` - which is where Google goes -
        has nothing to watch in your Chrome at all: its sign-in happens in its own window on
        latchkey's own profile (`latchkey login`), which this browser cannot share while it
        is running. So a dedicated page that is not signed in answers `login-required`
        straight away, naming the one sign-in to do, rather than looping on a store the
        sign-in never reaches.

        `cancel` is for when the caller is no longer wanted. The MCP server sets it when
        the client gives up on the call, or when somebody closes the session to reclaim
        it, and this gives up within a quarter second of that instead of at its own
        deadline. A wait nobody is waiting for must not keep the browser: it is the one
        call here that can last minutes, and every other call in the session is queued
        behind it.
        """
        cancel = cancel or _NEVER
        spec = getattr(self, "spec", None) or SessionSpec(mode="inject")
        if google.is_google_host(url) and spec.mode == "inject" and not spec.auto:
            return {"status": "wrong-mode", "url": url, "mode": spec.mode,
                    "hint": GOOGLE_WRONG_MODE}
        if google.is_google_host(url) and spec.mode in ("clone", "real") and not spec.auto:
            return {"status": "wrong-mode", "url": url, "mode": spec.mode,
                    "hint": GOOGLE_OWN_LOGIN_HINT}
        state = self.goto(url, settle_ms)
        if state.verdict == "logged-in":
            return {"status": "already-logged-in", "state": state.as_dict()}
        if spec.mode == "dedicated":
            return {"status": "login-required", "mode": "dedicated", "state": state.as_dict(),
                    "hint": ("this is latchkey's own profile, so the sign-in happens in its own "
                             "window: run `latchkey login " + url + "` (or the latchkey_login "
                             "tool), sign in there once, then open the page again. Never type "
                             "a password or code into an agent's chat.")}
        if google.is_google_host(url):
            hint = GOOGLE_OWN_LOGIN_HINT if spec.mode in ("clone", "real") \
                else GOOGLE_WRONG_MODE
            return {"status": "wrong-mode", "url": url, "mode": spec.mode,
                    "hint": hint, "state": state.as_dict()}

        stamp = self.jar_stamp()
        baseline = self.cookie_signature()
        deadline = time.time() + timeout_s
        polls = 0
        reads = 0
        self._publish("login", action="waiting", url=url, timeout_s=timeout_s)
        while time.time() < deadline:
            # Sleep in slices, so a cancellation is noticed in a fraction of a poll and
            # not at the end of one: the point of the flag is that it frees the browser now.
            slept = 0.0
            while slept < poll_s:
                if cancel.is_set():
                    self._publish("login", action="abandoned", polls=polls)
                    return {"status": "abandoned", "polls": polls,
                            "hint": "the client stopped waiting for this call; it can call "
                                    "latchkey_wait_for_login again to carry on watching the "
                                    "same login."}
                time.sleep(min(0.25, poll_s - slept))
                slept += 0.25
            polls += 1
            now = self.jar_stamp()
            if now == stamp:
                continue          # nothing wrote to the store; no need to open it
            stamp = now
            reads += 1
            signature = self.cookie_signature()
            if signature == baseline:
                continue          # a write, but not one that changed a cookie we hold
            baseline = signature
            changed = self.refresh()
            state = self.goto(url, settle_ms)
            if state.verdict == "logged-in":
                self.save_session()
                self._publish("login", action="logged-in", polls=polls,
                              **state.summary())
                return {"status": "logged-in", "polls": polls, "store_reads": reads,
                        "transferred": changed, "state": state.as_dict()}
        self._publish("login", action="timeout", polls=polls)
        return {"status": "timeout", "polls": polls, "store_reads": reads,
                "waited_s": timeout_s,
                "hint": "no new cookies appeared; is the site signed in on your browser?",
                "state": self.state().as_dict()}

    def describe(self) -> dict:
        """What this browser is, without a round trip to the page."""
        return {"label": self.label, "host": self.host, "profiles": self.profiles,
                "spec": self.spec.as_dict(), "cursor": self.show_cursor,
                "report": self.report.as_dict()}


def _site_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


@contextmanager
def logged_in(host: str | None = None, **kwargs: Any) -> Iterator[Browser]:
    """Context manager form: `with logged_in() as b: b.goto(...)`."""
    browser = Browser(host, **kwargs)
    try:
        yield browser.start()
    finally:
        browser.close()


def capture(url: str, out: str, *, host: str | None = None, settle_ms: int = 6000,
            full_page: bool = False, **kwargs: Any) -> InjectionReport:
    with logged_in(host, **kwargs) as browser:
        browser.goto(url, settle_ms)
        browser.screenshot(out, full_page)
        return browser.report
