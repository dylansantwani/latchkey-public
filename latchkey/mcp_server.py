"""MCP (Model Context Protocol) server over stdio, for agents.

Agent-facing surface over the session registry: each named session is its own
Chrome process on its own thread, so two agents can drive two sites at once
without touching each other's cookies or tabs. Every browsing tool takes an
optional `session` argument and defaults to `"default"`.

The login handoff, which is the whole point: if a site is logged out, the agent
asks you to sign in in your own browser, then calls `latchkey_wait_for_login`,
which blocks until your fresh cookies appear and transfers them into the running
headless session.

`tools/call` is dispatched on a worker pool: a call that blocks for minutes (a
login wait, a slow page) does not stop the handshake or another session's work.
Commands aimed at the *same* session are serialised in arrival order, so an agent
can still rely on act-then-read seeing its own effect.

One tool is advertised - `latchkey_batch` - and it carries the rest: every other tool
is a name one of its calls can use, so a whole task arrives as one request and the
surface an agent carries on every turn is one schema instead of thirty-four.
`latchkey_help` describes any of those names in full. The individual names remain
direct `tools/call` targets, which is what an allowlist or a script written against
the older surface is holding.

Run:  python3 -m latchkey serve
"""
from __future__ import annotations

import difflib
import json
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import urlparse

from . import a11y
from . import automation
from . import cookies as ck
from . import credentials, store
from . import google
from . import intervene
from . import jev
from . import login as login_mod
from . import pilot as pilot_mod
from . import profile as profile_mod
from .detect import BODY_LIMIT, short_url
from .automation import apply_actions
from .a11y import RefError
from .automation import ActionFailed
from .chrome import ProfileBusy
from .login import LoginPending
from .policy import NavigationRefused, ReadOnlyError
from .session import (GOOGLE_OWN_LOGIN_HINT, GOOGLE_WRONG_MODE, RealModeUnavailable,
                      SessionSpec)
from .sessions import DEFAULT_NAME, SessionError, registry

PROTOCOL_VERSION = "2024-11-05"
# One place for the version: pyproject and the handshake used to disagree, and the
# handshake is the one a client writes down.
from . import __version__

SERVER_INFO = {"name": "latchkey", "version": __version__}

# Generous: a call may legitimately sit in wait_for_login for its whole timeout.
TOOL_TIMEOUT_S = 600.0

# `registry` is the one in sessions.py, not a second copy: the viewer serves the
# sessions an agent opened, so both have to be looking at the same object.


IDLE_LANE_S = 60.0        # a session's worker retires after this long with nothing to do


class Lane:
    """A session's command queue: one worker, strictly first in, first out.

    This exists for ordering. The pool gives parallelism *between* sessions, but two
    commands aimed at the same session have to run in the order they arrived,
    otherwise `latchkey_session_open` can be overtaken by the very call that needs
    the session it opens (found by driving the real server over stdio), and an act
    can be overtaken by the read that follows it. Queueing from the reader loop - one
    thread, in arrival order - is what actually guarantees the order; a lock only
    guarantees exclusion, which is a different thing.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, task: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=f"latchkey-{self.name}",
                                                daemon=True)
                self._thread.start()
        self._queue.put(task)

    def _run(self) -> None:
        while True:
            try:
                task = self._queue.get(timeout=IDLE_LANE_S)
            except queue.Empty:
                # Retire, but only under the lock and only if nothing is waiting:
                # otherwise a submit that saw a live thread would leave its task
                # in a queue with nobody to run it.
                with self._lock:
                    if self._queue.empty():
                        self._thread = None
                        return
                continue
            try:
                task()
            finally:
                self._queue.task_done()

    def drain(self) -> None:
        self._queue.join()


class Lanes:
    """One lane per session name. Different sessions never wait on each other."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._lanes: dict[str, Lane] = {}

    def for_name(self, name: str) -> Lane:
        with self._guard:
            return self._lanes.setdefault(name, Lane(name))

    def drain(self) -> None:
        with self._guard:
            lanes = list(self._lanes.values())
        for lane in lanes:
            lane.drain()


# The cancellation flag for the call running on this thread: set by `Server._serve`, read
# by the tools that can stop early. A wait for a human is the one call here that can last
# minutes, and it is the one that has to notice that the client stopped waiting.
_CALL = threading.local()

# Closing a session has to work while that session is busy with a call that is waiting on a
# human. These two tools cancel what is in flight on the lane first instead of queueing
# politely behind a call that may sit there for another twenty seconds - which is how a
# session that is waiting on somebody becomes a session nobody can close.
LANE_RECLAIMERS = ("latchkey_session_close", "latchkey_forget_session")


# -- how much a reply may carry ----------------------------------------------
#
# The owner's everyday model is a local 35B with a 128k window, and the batch results it was
# handed averaged 3.7K characters and peaked at 37.6K - one `eval` at 24.5K, one cookie list
# at 23.6K. A big model shrugs that off; a small one loses the task in it. So there are two
# budgets. `normal` is what the surface always did. `compact` roughly halves every default,
# and the replies that had no ceiling at all (cookies, sites, eval) summarise instead of
# listing everything. Chosen by the human (LATCHKEY_COMPACT=1) or per request
# (`budget: "compact"` on a batch or any call). A cut always says how to get the rest.
COMPACT_ENV = "LATCHKEY_COMPACT"
BUDGETS: dict[str, dict[str, int]] = {
    "normal": {"snapshot": 6000, "text": 4000, "batch": 40_000, "eval": 8000,
               "cookies": 40, "sites": 40, "links": 50, "frame_text": 4000, "find": 8},
    "compact": {"snapshot": 3000, "text": 2500, "batch": 12_000, "eval": 2000,
                "cookies": 15, "sites": 15, "links": 25, "frame_text": 2500, "find": 5},
}
BUDGET_NAMES = {"compact": "compact", "small": "compact", "low": "compact", "tight": "compact",
                "local": "compact", "lean": "compact", "minimal": "compact",
                "normal": "normal", "default": "normal", "full": "normal", "large": "normal"}
EVAL_MAX_CHARS_LIMIT = 50_000


def time_left(ceiling: float) -> float:
    """How long a waiting call may wait: its own ceiling, or less if its batch ends sooner.

    A call that waits for a person (login_status, wait_for_login) is 20-25 seconds on its own,
    which is fine as a call and too long as the third call of a batch that has already spent
    ten: the batch's promise is that nothing *starts* after its budget, and a wait that starts
    in time and then runs past the client's thirty seconds breaks it from the inside.
    """
    deadline = getattr(_CALL, "deadline", None)
    if deadline is None:
        return ceiling
    return max(0.0, min(ceiling, deadline - time.monotonic() - 1.0))


def budget_name(explicit: Any = None) -> str:
    """The budget in force: the call's own, the request's, then the server's environment."""
    for candidate in (explicit, getattr(_CALL, "budget", None)):
        if candidate:
            return BUDGET_NAMES.get(str(candidate).strip().lower(), "normal")
    compact = (__import__("os").environ.get(COMPACT_ENV) or "").strip().lower()
    return "compact" if compact in ("1", "true", "yes", "on", "compact") else "normal"


def default_for(kind: str, budget: Any = None) -> int:
    """A default size for one kind of reply, under the budget in force."""
    return BUDGETS[budget_name(budget)][kind]


def _int(value: Any, fallback: int) -> int:
    """A number a model sent, which may be a string, a float or nothing at all."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def _on_session(session: str, fn: Callable[[Any], Any],
                timeout: float = TOOL_TIMEOUT_S) -> Any:
    """Run `fn(browser)` inside `session`.

    Ordering is the server's job, not this function's: the callers that arrive here
    all run on their session's lane.

    The *default* session opens itself. A named one still does not - a typo in a name
    should be an error rather than a second silent browser - but "you must call
    latchkey_session_open first" as the answer to the very first latchkey_open is a
    step that exists only to be performed, and the models least able to recover from a
    refusal are exactly the ones that skip it.
    """
    if session == DEFAULT_NAME and registry.get_existing(session) is None:
        registry.get(session, spec=SessionSpec(label=session))
    return registry.require(session).submit(fn, timeout=timeout)


def _as_dict(value: Any) -> Any:
    return value.as_dict() if hasattr(value, "as_dict") else value


def _dialogs(browser: Any) -> list[dict]:
    """Anything this browser answered for us. A browser double may have none to report."""
    return list(getattr(browser, "dialogs", None) or [])


def _notes(browser: Any) -> list[str]:
    """One-line notes the browser has for the next reply (a mode it moved to), taken once."""
    take = getattr(browser, "take_notes", None)
    try:
        return list(take()) if callable(take) else []
    except Exception:  # noqa: BLE001
        return []


def _page_reply(state: Any, dialogs: Any = None, notes: Any = None) -> dict:
    """A page as an action's reply: small, and with what the action needs to be verified.

    Actions answer with the page's shape and not its text (see `PageState.as_dict`), because
    a reply that carries four kilobytes of prose is a reply an agent cannot afford to make
    often. Anything a dialog did to the page rides along, since that is the usual reason a
    step does not do what it looked like it would. Under the compact budget the fields that
    say nothing (null, false, empty) are left out as well.
    """
    out = state.as_dict() if hasattr(state, "as_dict") else dict(state)
    if dialogs:
        out["dialogs"] = list(dialogs)[-3:]
    if notes:
        out["note"] = " ".join(notes)
    if out.get("verdict") in ("challenged", "blocked") and intervene.enabled():
        # A wall the agent cannot pass headless: point at the one tool that can, which asks
        # the user first. Kept short; the how is in latchkey_assist's own description.
        out.setdefault("assist", "a person may be able to clear this: ask the user, then "
                                 "latchkey_assist(confirm=true).")
    if budget_name() == "compact":
        out = {key: value for key, value in out.items()
               if key in ("url", "title", "verdict") or value not in (None, False, {}, [], "")}
    return out


# -- sessions ----------------------------------------------------------------

SESSION_MODES = ("auto", "inject", "dedicated", "clone", "real")
MODE_ALIASES = {"": "auto", "default": "auto", "none": "auto", "copy": "inject",
                "cookies": "inject", "clone-profile": "clone", "google": "dedicated",
                "own": "dedicated", "profile": "dedicated", "separate": "dedicated",
                "attach": "real", "mine": "real"}


def _session_mode(mode: Any) -> str:
    word = str(mode if mode is not None else "").strip().lower()
    word = MODE_ALIASES.get(word, word)
    if word not in SESSION_MODES:
        raise ValueError(f"mode {mode!r} is not one of: {', '.join(SESSION_MODES)} "
                         f"(auto picks dedicated for Google-backed sites and inject elsewhere)")
    return word


def tool_session_open(name: str = DEFAULT_NAME, mode: str | None = None,
                      profiles: list[str] | str | None = None,
                      host: str | None = None, label: str = "",
                      cursor: bool = False, recreate: bool = False,
                      read_only: bool = False, account: str | None = None) -> dict:
    """Open a named browser session, or return the running one.

    A session is an isolated Chrome: its own cookies, its own tabs, its own crashes. Use
    one per site or per agent. `mode` is 'auto' when not given: 'dedicated' for Google sites
    and known Google-SSO portals, 'inject' for the rest, decided per navigation.

      inject     decrypt the user's Chrome cookies into a fresh browser (~1s). Never carries
                 a Google account session - it cannot renew a device-bound one, and copying
                 it is what signs the real Chrome out.
      dedicated  latchkey's own long-lived profile for `account` (~1s after the first run).
                 Signed in once by the user with latchkey_login; after that it owns, renews
                 and keeps its own session. This is the Google route.
      clone      a copy-on-write clone of the user's real profile (~4s). Carries everything,
                 including a Google login it may not be able to keep - available on request,
                 no longer the default for Google.
      real       attach to a Chrome with a debugging endpoint (LATCHKEY_CDP; Chrome 136+
                 refuses one for the everyday profile).

    `account` picks which of latchkey's own logins a dedicated session uses - one profile per
    account, so a second Google account is `account="school"`, not a second login inside the
    first one's. latchkey_accounts lists them and says which are signed in.

    With `read_only=True` nothing can be sent as the user from this session.
    LATCHKEY_GOOGLE_MODE overrides the Google mode.
    """
    mode = _session_mode(mode)
    # Resolve here, not in the session: a profile name that does not exist should come
    # back as a tool error naming the alternatives, not as a browser that fails to start
    # twenty seconds later in another thread.
    try:
        resolved = ck.resolve_profiles(profiles) if profiles is not None else None
    except KeyError as exc:
        raise ValueError(str(exc)) from None
    try:
        account = profile_mod.normalize_account(account) if account else None
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    spec = SessionSpec(mode=mode, profiles=resolved, host=host, account=account,
                       label=label or name, show_cursor=bool(cursor),
                       read_only=bool(read_only))
    session = registry.get(name, spec=spec, recreate=recreate)
    info, notes = _on_session(name, lambda browser: (browser.describe(), _notes(browser)))
    out = {"session": name, "started": session.alive, **info}
    running = registry.spec_of(name) or spec
    if notes:
        out["note"] = " ".join(notes)
    withheld = ((info.get("report") or {}).get("withheld_live_session") or 0) \
        if isinstance(info, dict) else 0
    if withheld and getattr(running, "mode", "") == "inject":
        out["google"] = (f"{withheld} Google account cookies were held back in inject mode "
                         f"(it cannot renew a device-bound session, and copying one signs the "
                         f"user's Chrome out). Open Google with no mode: it goes to latchkey's "
                         f"own signed-in profile instead.")
    which = getattr(running, "account", None)
    if which:
        # Say it even before a Google navigation moves the session: an agent that asked for
        # a named account should see that it got it, not wait to find out on a page.
        out["account"] = which
    if getattr(running, "mode", "") == "dedicated":
        state = login_mod.status(getattr(running, "profile_dir", None), account=which)
        out["account"] = which or profile_mod.DEFAULT_ACCOUNT
        out["google_signed_in"] = state["signed_in"]
        if state.get("accounts"):
            out["emails"] = state["accounts"]
        if not state["signed_in"]:
            out["next_step"] = state["next_step"]
    return out


def tool_session_list() -> list[dict]:
    """List open sessions: name, label, host, mode, how long it has been running."""
    return registry.describe()


def tool_session_close(name: str = DEFAULT_NAME) -> dict:
    """Close one session and its Chrome process."""
    return {"session": name, "closed": registry.close(name)}


# -- browsing ----------------------------------------------------------------

def _profile_recommendation(url: str, using: list[str],
                            session: str = DEFAULT_NAME) -> dict | None:
    """A stronger Chrome profile for this site, expressed as an executable next step.

    A profile may hold analytics, language or device cookies without holding the login.
    Counting only whether *any* cookie exists made that common case look conclusive and
    suppressed the useful profile hint. Compare the session-looking signal instead: suggest
    an explicit switch only when another profile has a stronger authentication-shaped jar.

    The switch stays explicit because Chrome profiles can be different people. The structured
    action makes the safe choice easy for an agent to execute without parsing prose.
    """
    try:
        host = urlparse(url).netloc.split(":")[0]
        if not host:
            return None
        counts = ck.host_counts(host)
    except Exception:  # noqa: BLE001
        return None
    current = [row for name, row in counts.items() if name in using]
    current_auth = sum(int(row.get("auth_like") or 0) for row in current)
    current_cookies = sum(int(row.get("cookies") or 0) for row in current)
    better = {name: row for name, row in counts.items()
              if int(row.get("auth_like") or 0) > current_auth and name not in using}
    if not better:
        return None
    name, row = max(better.items(),
                    key=lambda kv: (kv[1].get("auth_like", 0), kv[1]["cookies"]))
    auth_like = int(row.get("auth_like") or 0)
    cookies = int(row.get("cookies") or 0)
    mine = "+".join(using) or "no Chrome profile"
    action = {"tool": "latchkey_use_profiles",
              "args": {"profiles": [name], "session": session}}
    reopen = {"tool": "latchkey_open", "args": {"url": url, "session": session}}
    next_step = (f"Try {name} before asking the user to sign in; run the two calls in "
                 f"profile_recommendation.calls. {mine}: {current_cookies} cookies, "
                 f"{current_auth} session-looking; {name}: {cookies}, {auth_like} "
                 "session-looking.")
    return {"profile": name,
            "evidence": {"current": {"profiles": list(using),
                                      "cookies": current_cookies,
                                      "auth_like": current_auth},
                         "candidate": {"cookies": cookies,
                                       "auth_like": auth_like}},
            "calls": [action, reopen], "next_step": next_step}


def _mode_of(browser: Any) -> str:
    return str(getattr(getattr(browser, "spec", None), "mode", "") or "")


def logged_out_next_step(url: str, mode: str) -> str:
    """What to do about a page that is not signed in, for this site in this mode.

    The old answer was the same for every site - "ask the user to sign in in their own
    Chrome, then call latchkey_wait_for_login" - and for Google it was a loop with no exit:
    the sign-in landed in the user's Chrome, the account session was held back from the copy
    again, and the wait watched the wrong store forever. Google gets its own answer now:
    the user signs in in their own Chrome and the session is reopened, never a latchkey
    sign-in. Every answer says the one thing a model must never do about a sign-in page.
    """
    never = " Never ask the user to type a password or code into chat."
    if google.is_google_host(url):
        if mode == "inject":
            return GOOGLE_WRONG_MODE
        if mode in ("clone", "real"):
            # These carry the user's own Chrome Google login. A signed-out verdict here means
            # the user's Chrome itself is signed out - the fix is in their normal browser, not
            # a second sign-in.
            return ("This reads as signed out because the user's own Chrome is not signed in to "
                    "Google. The user signs in in their normal Chrome window; a clone of that "
                    "profile carries the login and renews it on this machine (real mode drives "
                    "their Chrome directly). Then reopen this session "
                    "(latchkey_session_close, then latchkey_session_open) so it is cloned again "
                    "from the profile they just signed in to, and open this url again. No "
                    "separate latchkey sign-in is needed." + never)
        state = login_mod.status()
        if state["window_open"] or state["signed_in"]:
            return state["next_step"]
        return ("Google is not signed in on latchkey's own profile (mode dedicated) yet. Call "
                "latchkey_login "
                "(it opens a Chrome window and returns at once), ask the user to sign in in "
                "that window, then call latchkey_login_status until signed_in is true and "
                "open this url again." + never)
    if mode == "dedicated":
        return ("Not signed in on latchkey's own profile. Call latchkey_login with this url; "
                "the user signs in in that window and closes it; then open this url again."
                + never)
    return ("This site is not signed in. Ask the user to sign in to it in their own Chrome "
            "window, then call latchkey_wait_for_login with this url." + never)


def google_account_chooser_next_step(url: str, *, snapshot_ready: bool = False) -> str | None:
    """Turn a signed-in Google account chooser into a choice, not a false login failure."""
    if google.signin_step(url) != "Google account chooser":
        return None
    inspect = "Use the account refs in this snapshot" if snapshot_ready else \
        "Call latchkey_snapshot to see the account refs"
    return (f"{inspect}, then latchkey_act to click the intended signed-in account. "
            "Choosing an identity is explicit when more than one account is shown; do not ask "
            "the user for a password or code in chat.")


def _google_sso_recommendation(session: str) -> dict | None:
    """Find the known portal's Google SSO control and return the click as a tool call."""
    try:
        found = _on_session(
            session, lambda browser: a11y.find(browser, "Google Single Sign-On", 4))
    except Exception:  # noqa: BLE001 - extra guidance must not make open fail
        return None
    matches = found.get("matches") if isinstance(found, dict) else None
    for match in matches or []:
        name = str(match.get("name") or "")
        ref = match.get("ref")
        low = name.lower()
        if ref and "google" in low and ("single sign-on" in low or "sso" in low):
            call = {"tool": "latchkey_act", "args": {
                "actions": [{"do": "click", "ref": ref}], "session": session}}
            return {"label": name, "call": call,
                    "next_step": ("Continue with Google SSO by running sso_recommendation.call. "
                                  "If Google shows more than one signed-in account, inspect the "
                                  "account chooser and click the intended identity explicitly.")}
    return None


def _autocomplete_google_sso(session: str):
    """Click a portal's Google SSO control and follow the SAML round-trip to a logged-in page.

    The dedicated profile already holds the Google session, so the click needs no password -
    this is the same click the recommendation would hand back, done for the agent so opening
    the portal just lands signed in. Returns the resulting PageState when it lands logged-in,
    or None (no SSO control, a slow/absent flow, or a multi-account chooser that must be chosen
    explicitly), in which case the caller falls back to surfacing the click as a recommendation.
    """
    def run(browser):
        found = a11y.find(browser, "Google Single Sign-On", 4)
        ref = None
        for match in (found.get("matches") or []):
            low = str(match.get("name") or "").lower()
            if match.get("ref") and "google" in low and ("single sign-on" in low or "sso" in low):
                ref = match["ref"]
                break
        if not ref:
            return None
        try:
            apply_actions(browser, [{"do": "click", "ref": ref}])
        except (ActionFailed, Exception):  # noqa: BLE001 - a failed click is just "no auto-login"
            return None
        # accounts.google.com and back. A signed-in profile with one account passes straight
        # through; give it a few settles and stop the moment it lands or a chooser appears.
        for _ in range(4):
            browser.settle(3000)
            st = browser.state()
            if getattr(st, "verdict", None) == "logged-in":
                return st
            if google.signin_step(getattr(st, "url", "") or "") == "Google account chooser":
                return None
        return None
    try:
        return _on_session(session, run)
    except Exception:  # noqa: BLE001 - auto-complete is best effort, never fails the open
        return None


def tool_open(url: str, session: str = DEFAULT_NAME) -> dict:
    """Navigate to a URL as the logged-in user."""
    state, dialogs, notes, mode = _on_session(
        session, lambda browser: (browser.goto(url), _dialogs(browser), _notes(browser),
                                  _mode_of(browser)))
    if not mode:
        mode = getattr(registry.spec_of(session), "mode", "inject") or "inject"
    out = _page_reply(state, dialogs, notes)
    out["session"] = session
    # Usually a PageState; but anything that has been through the batch surface (or a
    # session stub) hands back the serialised dict, and both shapes have to read the
    # same here.
    verdict = state.get("verdict") if isinstance(state, dict) else state.verdict
    recommendation = None
    sso_recommendation = None
    if verdict != "logged-in" and mode == "inject" \
            and not google.is_google_host(url):
        # Worth a look before anyone concludes "signed out": a weak current jar can be
        # analytics/device cookies while another profile holds the actual login.
        using = _on_session(session, lambda browser: list(browser.profiles or []))
        recommendation = _profile_recommendation(url, using, session)
        if recommendation:
            out["profile_recommendation"] = recommendation
            out["other_profiles"] = recommendation["next_step"]
    if verdict == "logged-out" and google.is_google_sso_clone_host(url):
        # The portal's own page is signed out, but its login is a Google SSO click and the
        # profile latchkey is on already holds that Google session - so the way in is the SSO
        # click, not a fresh sign-in. Do it for the agent: a signed-in profile completes the
        # SAML round-trip without a password, so opening the portal just lands signed in. Only
        # when that does not land (profile not signed in, or a multi-account chooser) do we
        # fall back to handing the click back as a recommendation.
        completed = _autocomplete_google_sso(session)
        if completed is not None:
            state = completed
            verdict = "logged-in"
            out = _page_reply(state, dialogs, notes)
            out["session"] = session
            existing = out.get("note")
            out["note"] = (f"{existing} " if existing else "") + \
                "signed in automatically via Google SSO on latchkey's own profile."
        else:
            sso_recommendation = _google_sso_recommendation(session)
            if sso_recommendation:
                out["sso_recommendation"] = sso_recommendation
    if verdict == "logged-out":
        # Put the executable profile switch in the primary field agents already follow.
        # Asking the human to log in is only correct after the stronger profile was tried.
        current_url = (state.get("url") if isinstance(state, dict) else state.url) or url
        chooser = google_account_chooser_next_step(current_url)
        out["next_step"] = recommendation["next_step"] if recommendation else \
            sso_recommendation["next_step"] if sso_recommendation else chooser or \
            logged_out_next_step(current_url, mode)
    return out


def tool_snapshot(mode: str = "interactive", selector: str | None = None,
                  viewport_only: bool = False, max_chars: int | None = None,
                  url: str | None = None, session: str = DEFAULT_NAME) -> dict:
    """Read the page as the things on it, each with a ref an action can name.

    One round trip: the walker runs in the page, the refs are remembered for this session,
    and the text is what an agent reads instead of the DOM. A `url` opens that page first -
    what a model that writes `snapshot(url=...)` meant.
    """
    mode = a11y.normalise_mode(mode)
    budget = _int(max_chars, default_for("snapshot")) if max_chars else default_for("snapshot")

    def read(browser):
        if url:
            browser.goto(url)
        text = a11y.read(browser, mode=mode, selector=selector,
                         viewport_only=bool(viewport_only), max_chars=budget)
        state = browser.state()
        out = {"url": short_url(state.url), "title": state.title, "verdict": state.verdict,
               "mode": mode, "snapshot": text}
        notes = _notes(browser)
        if notes:
            out["note"] = " ".join(notes)
        if state.verdict == "logged-out":
            out["next_step"] = google_account_chooser_next_step(
                state.url, snapshot_ready=True) or \
                logged_out_next_step(state.url, _mode_of(browser))
        return out

    return _on_session(session, read)


def tool_text(limit: int | None = None, offset: int = 0, url: str | None = None,
              session: str = DEFAULT_NAME) -> dict:
    """Return the current page's URL, title and visible text, a window of it at a time.

    `offset` reads on from where a cut left off, and a cut says so: `more` names the offset
    to ask for next. The text used to stop at `limit` without a word, which a model reads as
    "that is the whole page".
    """
    size = max(1, _int(limit, default_for("text"))) if limit else default_for("text")
    start = max(0, _int(offset, 0))

    def read(browser):
        if url:
            browser.goto(url)
        state = browser.state(max(BODY_LIMIT, start + size))
        out = state.as_dict(detail=True)
        full = str(out.get("text") or "")
        total = len(full)
        chunk = full[start:start + size]
        if total >= BODY_LIMIT and start + size > total:
            # The probe carries the first 20,000 characters; past that, ask the page itself.
            try:
                total, chunk = browser.run_js(
                    "([o, l]) => { const t = document.body ? document.body.innerText : '';"
                    " return [t.length, t.slice(o, o + l)]; }", [start, size])
            except Exception:  # noqa: BLE001
                pass
        out["text"] = chunk
        out["text_chars"] = total
        if start:
            out["offset"] = start
        left = total - start - len(chunk)
        if left > 0:
            out["more"] = (f"{left} more characters: latchkey_text with offset="
                           f"{start + len(chunk)}")
        return _page_reply(out, None, _notes(browser))

    return _on_session(session, read)


def tool_sync(session: str = DEFAULT_NAME) -> dict:
    """Re-read the real browser's cookies and inject anything new.

    Call this after the user logs in somewhere in their own browser.
    """
    return _on_session(session, lambda browser: browser.refresh())


def tool_wait_for_login(url: str, timeout_s: int = 300,
                        session: str = DEFAULT_NAME) -> dict:
    """Ask the user to log in, then wait for their session to arrive.

    Blocks while the user signs in to the site in their real browser. Polls the browser's
    cookie database, transfers any new cookies into the running headless session, and
    reports the verdict once signed in. Only this session waits: other sessions keep
    working.

    And it never blocks longer than the client waits for an answer (LOGIN_WAIT_S): a wait
    that outlives its caller is a tool that reports "timed out" while it is still working.
    The wait is handed back instead - the answer says the login has not landed yet, and
    calling it again carries on watching the same login. `timeout_s` stays the ceiling
    across those calls, so ask for the minutes you actually need.

    If the client gives up on this call, the wait gives up too, within a quarter second,
    and says `status: abandoned`. A wait nobody is waiting for is the one call here that
    can hold a session for minutes, so it is the one that must let go the moment the
    reason for it is gone.
    """
    timeout_s = _int(timeout_s, 300)
    wait_s = max(1.0, time_left(float(min(timeout_s, int(LOGIN_WAIT_S)))))
    # Read the flag on this thread, then hand the object across: the session thread's own
    # thread-local is a different one, and would say "nothing is cancelled" forever.
    cancel = getattr(_CALL, "cancel", None)
    spec = registry.spec_of(session)
    mode = getattr(spec, "mode", "inject") if spec is not None else "inject"
    auto = getattr(spec, "auto", True) if spec is not None else True
    if google.is_google_host(url):
        # A Google session runs on latchkey's own profile, so the sign-in to watch for *is*
        # in a store of ours - that is the dedicated branch below. A session pinned to some
        # other mode has no such store, and is told which mode Google needs instead.
        wanted = google.mode_for(url) if auto else mode
        if wanted == "dedicated":
            # `wanted`, not `mode`: a session that chose nothing is still reading "inject"
            # until its next Google navigation moves it. The sign-in to wait for is on
            # latchkey's own profile either way, and watching the store the session happens
            # to hold *right now* is how this used to answer wrong-mode to a session that
            # was about to be in exactly the right mode.
            return _wait_for_own_profile_login(url, session, wanted, wait_s, cancel)
        return {"status": "wrong-mode", "url": url, "mode": wanted,
                "next_step": GOOGLE_WRONG_MODE if wanted == "inject" else GOOGLE_OWN_LOGIN_HINT}
    if mode == "dedicated":
        return _wait_for_own_profile_login(url, session, mode, wait_s, cancel)
    result = _on_session(session,
                         lambda browser: browser.wait_for_login(
                             url, timeout_s=int(wait_s), cancel=cancel),
                         timeout=wait_s + 30.0)
    # A profile recommendation is evidence, not authority: stale cookies or another person's
    # profile must never prevent a login the user intentionally completed in the current one.
    # Only offer the alternative after the real wait failed, and never for clone (whose Chrome
    # profile selection is not changed by latchkey_use_profiles).
    more_wait_requested = timeout_s > wait_s
    timed_out = isinstance(result, dict) and (
        result.get("status") == "timeout"
        # Preserve compatibility with older/fake browser implementations that returned only
        # the verdict. A logged-out result after wait_for_login means its wait elapsed.
        or ("status" not in result and result.get("verdict") == "logged-out")
    )
    if timed_out \
            and not more_wait_requested and mode == "inject" \
            and (spec is None or getattr(spec, "db_path", None) is None):
        try:
            using = ck.resolve_profiles(getattr(spec, "profiles", None))
        except Exception:  # noqa: BLE001 - profile discovery is advice, never a hard failure
            using = []
        recommendation = _profile_recommendation(url, using, session)
        if recommendation:
            result = {**result, "profile_recommendation": recommendation,
                      "next_step": recommendation["next_step"]}
    if timed_out and more_wait_requested:
        remaining_s = max(1, int(round(timeout_s - wait_s)))
        continuation_call = {"tool": "latchkey_wait_for_login",
                             "args": {"url": url, "timeout_s": remaining_s,
                                      "session": session}}
        continuation = ("Run continuation_call to wait again; the requested login window has not "
                        "elapsed yet.")
        result = {**result, "note": (
            f"waited {wait_s:.0f}s of the {timeout_s}s asked for: the client stops waiting "
            f"for a single call at {CLIENT_WAIT_S:.0f}s. Run continuation_call to wait again "
            f"with the remaining {remaining_s}s."),
                  "remaining_s": remaining_s, "continuation_call": continuation_call,
                  "next_step": continuation}
    return result


def _wait_for_own_profile_login(url: str, session: str, mode: str,
                                wait_s: float, cancel: Any) -> dict:
    """wait_for_login for a dedicated session, whose sign-in is on latchkey's own profile.

    It watches that profile's cookie store - the store the sign-in actually lands in - and
    never the user's Chrome. This is the Google path too: a Google session runs on latchkey's
    own profile, so its one sign-in lands here. The session is not touched while the window is
    open: Chrome allows one browser per profile, and the window is that browser until it is
    closed.
    """
    if mode != "dedicated":
        return {"status": "wrong-mode", "url": url, "mode": mode,
                "next_step": GOOGLE_OWN_LOGIN_HINT}
    account = getattr(registry.spec_of(session), "account", None)
    state = login_mod.wait(timeout_s=wait_s, cancel=cancel, host=url, account=account)
    if cancel is not None and cancel.is_set():
        return {"status": "abandoned", "hint": "the client stopped waiting; call "
                                               "latchkey_login_status to carry on."}
    done = state["signed_in"] if google.is_google_host(url) else not state["window_open"]
    if not done:
        status = "waiting" if state["window_open"] else "login-required"
        return {"status": status, "signed_in": state["signed_in"],
                "window_open": state["window_open"], "next_step": state["next_step"]}
    opened = tool_open(url, session)
    verdict = opened.get("verdict", "unclear")
    out = {"status": {"logged-in": "logged-in", "logged-out": "login-required"}.get(verdict,
                                                                                  verdict),
           "state": opened}
    if opened.get("next_step"):
        out["next_step"] = opened["next_step"]
    return out


def tool_login(url: str = login_mod.DEFAULT_URL, again: bool = False,
               account: str | None = None) -> dict:
    """Open a Chrome window on latchkey's own profile for the user to sign in to, once.

    This *is* the Google path. Google, Gmail and YouTube run on latchkey's own profile, which
    signs in once and then owns and renews that session itself - so the user's real Chrome is
    never copied and never signed out. One sign-in per account, ever.

    `account` names which login: leave it out for 'default', or pass a name like 'school' for
    a second Google account, which gets a profile of its own. latchkey_accounts lists them.

    Returns at once - a sign-in takes minutes and a tool call has seconds - so follow it with
    latchkey_login_status. The window is an ordinary Chrome (no automation flags, no debugging
    port), which is what lets Google's sign-in accept it at all. Sessions in this server using
    that profile are closed first, because Chrome allows one browser per profile.

    Never ask the user for a password or a code in chat: they type it in that window.
    """
    try:
        account = profile_mod.normalize_account(account) if account else None
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    closed = []
    target = login_mod.profile_path(None, account)
    for name in registry.names():
        spec = registry.spec_of(name)
        if getattr(spec, "mode", "") == "dedicated" and \
                login_mod.profile_path(getattr(spec, "profile_dir", None),
                                       getattr(spec, "account", None)) == target:
            registry.close(name)
            closed.append(name)
    out = login_mod.start(url or login_mod.DEFAULT_URL, again=bool(again), account=account)
    out["account"] = account or profile_mod.DEFAULT_ACCOUNT
    if closed:
        out["closed_sessions"] = closed
    return out


def tool_accounts() -> dict:
    """Which logins latchkey holds, and which are signed in to Google.

    Ask this first when a task needs a particular account, or when a Google page reads as
    signed out. Each account is a profile of latchkey's own: signed in once by the user
    (latchkey_login), then renewed in that profile alone - the user's Chrome is not involved,
    so nothing here can sign them out of it.
    """
    rows = login_mod.list_accounts()
    out = {"accounts": rows,
           "signed_in": [r["account"] for r in rows if r["google_signed_in"]],
           "default": profile_mod.DEFAULT_ACCOUNT}
    if not out["signed_in"]:
        out["next_step"] = (
            "No account is signed in to Google yet. Call latchkey_login (optionally with "
            "account='<name>'): it opens one ordinary Chrome window for the user to sign in "
            "once, and returns at once. Never ask the user for a password or code in chat.")
    else:
        out["usage"] = ("Pass account='<name>' to latchkey_session_open to browse as that "
                        "login; a session with no account uses 'default'.")
    return out


def tool_login_status(wait_s: int = 20, url: str | None = None,
                      account: str | None = None) -> dict:
    """Is latchkey's own profile signed in yet? Waits up to `wait_s` (max 25) for it to be.

    This is how latchkey_login finishes: it returns at once, this says when the sign-in has
    landed. `account` names which login to ask about (default: 'default'). Google included -
    a Google session runs on this profile, so this is the question to ask about Gmail too.
    """
    try:
        account = profile_mod.normalize_account(account) if account else None
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    cancel = getattr(_CALL, "cancel", None)
    seconds = time_left(max(0.0, min(float(_int(wait_s, 20)), LOGIN_WAIT_S)))
    out = login_mod.wait(timeout_s=seconds, cancel=cancel, host=url, account=account)
    out["account"] = account or profile_mod.DEFAULT_ACCOUNT
    return out


def tool_assist(url: str | None = None, session: str = DEFAULT_NAME, confirm: bool = False,
                cancel: bool = False, reason: str | None = None,
                timeout_s: int | None = None) -> dict:
    """Hand the user a real window to pass a wall the agent cannot - with their say-so first.

    For a page that stops the agent and needs a person: a captcha or "press & hold", an
    anti-bot block, a sign-in the agent must not do. Two steps on purpose, so a browser
    carrying the user's cookies is never opened without them agreeing:

      1. Call it with no confirm. latchkey reads the page and returns `ask_user`: what to
         tell the user (which site, which wall) and that a window will open. Nothing opens.
      2. On the user's yes, call it with confirm=true. latchkey opens a headed Chrome
         carrying this site's cookies at this session's own identity, the user solves the
         check in it, and the cookies it earns are carried back into this session, which is
         re-read so you learn whether the wall is gone.

    The window can outlast one call: if the user is still working when this returns, the
    status is `waiting` and calling confirm=true again keeps waiting (it does not open a
    second window). cancel=true closes the window. Never ask the user for a password or a
    code in chat - the window is where those happen.
    """
    if cancel:
        stopped = intervene.manager.cancel(session)
        handoff = intervene.manager.get(session)
        return {"status": "cancelled" if stopped else "nothing-to-cancel",
                **({"assist": handoff.as_dict()} if handoff else {})}
    if not intervene.enabled():
        return {"status": "disabled",
                "hint": "human intervention is switched off for this server "
                        "(LATCHKEY_ASSIST=off); only the person running it can turn it on."}
    total = _int(timeout_s, int(intervene.DEFAULT_TIMEOUT_S)) if timeout_s \
        else int(intervene.DEFAULT_TIMEOUT_S)
    if not confirm:
        snapshot = _on_session(session, lambda browser: browser.assist_snapshot(url))
        handoff = intervene.manager.request(
            session, snapshot.get("url") or url or "", verdict=snapshot.get("verdict") or "",
            wall=snapshot.get("wall"), reason=reason)
        return _assist_consent(handoff, snapshot)
    # Reading the page (which can navigate the session) is only needed to open the window; a
    # later confirm that is just keeping the wait alive attaches to the window already open.
    handoff = intervene.manager.get(session)
    if handoff is None or handoff.state != intervene.OPEN:
        snapshot = _on_session(session, lambda browser: browser.assist_snapshot(url))
        handoff = intervene.manager.start(session, snapshot=snapshot,
                                          url=snapshot.get("url") or url, timeout_s=total)
    cancel_flag = getattr(_CALL, "cancel", None)
    ceiling = max(1.0, time_left(min(float(total), ASSIST_WAIT_S)))
    _wait_assist(handoff, ceiling, cancel_flag)
    if cancel_flag is not None and cancel_flag.is_set():
        return {"status": "abandoned", "assist": handoff.as_dict(),
                "hint": "the client stopped waiting; the window is still open. Call "
                        "latchkey_assist(confirm=true) to keep waiting, or "
                        "latchkey_assist(cancel=true) to close it."}
    if handoff.running:
        return {"status": "waiting", "assist": handoff.as_dict(),
                "next_step": "the window is open and the user is working in it. Call "
                             "latchkey_assist(confirm=true) again to keep waiting; it does "
                             "not open a second window."}
    return _assist_finish(handoff, session)


def _assist_consent(handoff: intervene.Intervention, snapshot: dict) -> dict:
    """The reply that asks the user, before any window is opened."""
    site = urlparse(snapshot.get("url") or "").netloc or short_url(handoff.url)
    wall = snapshot.get("wall") or {}
    who = wall.get("sentence") or handoff.label or ""
    what = intervene.REASONS.get(handoff.reason, "a step that needs you")
    ask = (f"{site} is showing {what}" + (f" ({who})" if who else "") + ". latchkey can open "
           "a real Chrome window carrying this site's cookies, at this session's own "
           "identity, for you to solve it in - then carry the cookies it earns back into "
           "this session. Is that okay? (The window is where any password or code goes, "
           "never chat.)")
    out = {"status": "consent-required", "site": site, "verdict": snapshot.get("verdict"),
           "reason": handoff.reason, "ask_user": ask, "assist": handoff.as_dict(),
           "next_step": "on the user's yes, call latchkey_assist(confirm=true)"}
    if who:
        out["wall"] = who
    return out


def _wait_assist(handoff: intervene.Intervention, seconds: float, cancel: Any) -> bool:
    """Wait for the window to finish, in slices, letting go the moment the client gives up."""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            return False
        if handoff.wait(0.25):
            return True
    return not handoff.running


def _assist_finish(handoff: intervene.Intervention, session: str) -> dict:
    """The window is done: carry any earned cookies into the session and report the verdict."""
    out: dict = {"status": handoff.state, "assist": handoff.as_dict()}
    if handoff.state == intervene.DECLINED:
        return out
    if handoff.state == intervene.CANCELLED:
        out["hint"] = "the intervention was cancelled; the window is closed."
        return out
    if handoff.state == intervene.FAILED:
        out["hint"] = handoff.error or ("the window closed without clearing the wall. You can "
                                        "try latchkey_assist again, or read the page as it is.")
        return out
    # Solved: apply the earned cookies once, then re-read; a repeat call just re-reads.
    if handoff.harvested and not handoff.applied:
        applied = _on_session(session,
                              lambda browser: browser.assist_apply(handoff.harvested, handoff.url))
        # Marked applied only after the apply returns: if it raises (the session was closed
        # under us, a CDP hiccup), the error reaches the agent and the flag stays False, so a
        # retried confirm re-applies rather than silently reporting the clearance as gone.
        handoff.applied = True
        out["applied"] = {k: applied[k] for k in ("accepted", "offered") if k in applied}
        out["state"] = applied.get("state")
    else:
        state = _on_session(session, lambda browser: browser.state().as_dict())
        out["state"] = state
        if not handoff.harvested:
            out["note"] = "the wall cleared with no new cookies to carry; the page is re-read."
    out["verdict"] = (out.get("state") or {}).get("verdict")
    return out


def tool_assist_status(session: str | None = None) -> dict:
    """What human interventions are open or recently done: which site, which wall, what state.

    Read-only. With a session name, just that session's; without, every one this server knows.
    """
    intervene.manager._reap_expired()
    if session:
        handoff = intervene.manager.get(session)
        return handoff.as_dict() if handoff else {"session": session, "state": "none"}
    items = [handoff.as_dict() for handoff in intervene.manager.list()]
    return {"count": len(items), "interventions": items}


def tool_wait(until: str, value: str | None = None, timeout_ms: int = 10_000,
              session: str = DEFAULT_NAME) -> dict:
    """Wait for one thing to become true, instead of polling for it.

    One call replaces the eval-in-a-loop an agent does without it, and either answer is
    actionable: `ok` with how long it took, or where the page actually is instead.
    """
    budget = int(max(1, min(_int(timeout_ms, 10_000), CLIENT_WAIT_S * 1000 - 3000,
                            time_left(CLIENT_WAIT_S) * 1000)))
    return _on_session(session, lambda browser: browser.wait_until(until, value, budget))


def tool_find(query: str, limit: int | None = None, selector: str | None = None,
              session: str = DEFAULT_NAME) -> dict:
    """Find what is on the page that matches a description, best first, each with a ref.

    For when you know what you are looking for and not where it is: the cheap way to skip
    reading the page, and every match can be acted on by ref.
    """
    count = max(1, _int(limit, default_for("find"))) if limit else default_for("find")
    return _on_session(session,
                       lambda browser: a11y.find(browser, query, count, selector=selector))


def tool_act(actions: list[dict], session: str = DEFAULT_NAME) -> dict:
    """Run actions on the current page. See the tool description for the verbs."""
    def run(browser):
        apply_actions(browser, actions)
        state = browser.state()
        out = _page_reply(state, _dialogs(browser), _notes(browser))
        if state.verdict == "logged-out":
            chooser = google_account_chooser_next_step(state.url)
            if chooser:
                out["next_step"] = chooser
        return out
    return _on_session(session, run)


def tool_screenshot(path: str = "/tmp/latchkey.png", full_page: bool = False,
                    session: str = DEFAULT_NAME) -> dict:
    """Screenshot the current page."""
    return {"path": _on_session(session, lambda browser: browser.screenshot(path, full_page))}


def _as_function(js: str) -> str:
    """Script written as a function body (`return x`) wrapped into a function, once."""
    return f"() => {{ {js}\n}}"


def tool_eval(js: str, max_chars: int | None = None, session: str = DEFAULT_NAME) -> Any:
    """Evaluate JavaScript in the current page and return the result as JSON.

    The result has a ceiling: an `eval` that returned 24.5K characters was the largest single
    thing a local model was handed in a week of use. Past it, the reply says how big the
    result was and how to ask for less, instead of carrying it.
    """
    cap = max(200, min(_int(max_chars, default_for("eval")), EVAL_MAX_CHARS_LIMIT)) \
        if max_chars else default_for("eval")

    def run(browser):
        try:
            return browser.evaluate(js)
        except Exception as exc:  # noqa: BLE001
            # `return document.title` is a function body, not an expression; say it once more
            # as a function rather than hand back "Illegal return statement".
            if "Illegal return statement" in str(exc) and "=>" not in js[:40] \
                    and not js.lstrip().startswith(("function", "async")):
                return browser.evaluate(_as_function(js))
            raise

    result = _on_session(session, run)
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    if len(text) <= cap:
        return result
    return {"result_cut": text[:cap], "total_chars": len(text),
            "more": (f"the result was {len(text)} characters and {cap} are shown. Return less "
                     f"from the script (filter it, or .slice(0, n)), or pass max_chars (up to "
                     f"{EVAL_MAX_CHARS_LIMIT}).")}


def tool_links(limit: int | None = None, session: str = DEFAULT_NAME) -> list[dict]:
    """List links on the current page."""
    count = max(1, _int(limit, default_for("links"))) if limit else default_for("links")
    return _on_session(session, lambda browser: browser.links(count))


def tool_frames(session: str = DEFAULT_NAME) -> list[dict]:
    """List every frame on the current page, iframes included.

    state/text/eval only see the main frame, so content embedded in an iframe
    (a Google Slides deck inside Canvas, a payment form) is invisible to them.
    """
    return _on_session(session, lambda browser: browser.frames())


def tool_frame_text(index: int, limit: int | None = None,
                    session: str = DEFAULT_NAME) -> dict:
    """Read the visible text inside one frame, by index from latchkey_frames."""
    count = max(1, _int(limit, default_for("frame_text"))) if limit else \
        default_for("frame_text")
    return {"index": index,
            "text": _on_session(session, lambda browser: browser.frame_text(_int(index, 0),
                                                                            count))}


def tool_pages(session: str = DEFAULT_NAME) -> list[dict]:
    """List open tabs in this session."""
    return _on_session(session, lambda browser: browser.pages())


def tool_new_page(url: str | None = None, session: str = DEFAULT_NAME) -> dict:
    """Open a new tab, optionally at a URL."""
    return _on_session(session, lambda browser: _page_reply(browser.new_page(url),
                                                            _dialogs(browser)))


def tool_switch(target: int | str, session: str = DEFAULT_NAME) -> dict:
    """Drive a different tab: pass the tab id from latchkey_pages ('t2') or its index."""
    return _on_session(session, lambda browser: _page_reply(browser.switch(target),
                                                            _dialogs(browser)))


def tool_close_page(target: int | str | None = None,
                    session: str = DEFAULT_NAME) -> dict:
    """Close a tab, by id from latchkey_pages or index (default: the last one)."""
    return _on_session(session, lambda browser: browser.close_page(target))


def tool_go(url: str = "", session: str = DEFAULT_NAME) -> dict:
    """Navigate back or forward in history, or reload."""
    if url not in ("back", "forward", "reload"):
        raise ValueError("url must be 'back', 'forward' or 'reload'")
    return _on_session(session, lambda browser: _page_reply(getattr(browser, url)(),
                                                            _dialogs(browser)))


def tool_save_session(site: str | None = None, session: str = DEFAULT_NAME) -> dict:
    """Persist this session so it survives restarts."""
    return {"path": _on_session(session, lambda browser: browser.save_session(site))}


def tool_injected(session: str = DEFAULT_NAME) -> dict:
    """Which cookies were injected: totals, partitioned, host-only, failures."""
    return _on_session(session, lambda browser: browser.report.as_dict())


# -- cookie and profile discovery (no browser needed) ------------------------

def tool_sessions() -> list[dict]:
    """List saved sessions (names and counts, never values)."""
    return store.describe()


def tool_forget_session(site: str) -> dict:
    """Delete a saved session."""
    return {"removed": store.clear(site)}


def tool_sites(limit: int | None = None, host: str | None = None) -> dict:
    """Hosts present in the cookie store, with counts, largest first. Never returns values.

    A summary, not the whole store: the totals, then the first `limit` hosts, and a line
    saying how many more there are and how to see them. `host` filters by substring.
    """
    count = max(1, min(_int(limit, default_for("sites")), 1000)) if limit else \
        default_for("sites")
    counts: dict[str, int] = {}
    for c in ck.load(None):
        if host and host.lower() not in c.host.lower():
            continue
        counts[c.host] = counts.get(c.host, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    out: dict[str, Any] = {"hosts": len(ranked), "cookies": sum(counts.values()),
                           "top": [{"host": h, "cookies": n} for h, n in ranked[:count]]}
    if len(ranked) > count:
        out["more"] = (f"{len(ranked) - count} more hosts: pass limit (up to 1000), or host "
                       f"to filter")
    return out


def tool_profiles(host: str | None = None) -> list[dict]:
    """Chrome profiles on this machine, and with `host`, which of them hold its cookies.

    `host='webassign.net'` answers "which profile am I signed in to this site in?" - each
    row counts that site's cookies in that profile and how many look like a session, best
    first. No values, no decryption beyond the count, no SQLite by hand.
    """
    counts = ck.host_counts(host) if host else {}
    out = []
    for name, path in ck.profiles().items():
        row: dict = {"profile": name, "path": path}
        if host:
            row.update(counts.get(name, {"cookies": None, "auth_like": None}))
        else:
            try:
                row["cookies"] = len(ck.load(None, path))
            except Exception:  # noqa: BLE001
                row["cookies"] = -1
        out.append(row)
    if host:
        out.sort(key=lambda row: (-(row.get("auth_like") or 0), -(row.get("cookies") or 0)))
    return out


def tool_use_profiles(profiles: list[str] | str = "all",
                      session: str = DEFAULT_NAME) -> dict:
    """Restart this session reading cookies from these profiles ('all' for every one).

    Every profile in a Chrome install shares one key, so this is cheap. Later
    profiles win when two profiles hold the same cookie.
    """
    names = ck.resolve_profiles(profiles)
    base = registry.spec_of(session)
    spec = replace(base, profiles=names) if base is not None \
        else SessionSpec(profiles=names)
    registry.get(session, spec=spec, recreate=True)
    report = _on_session(session, lambda browser: browser.report.as_dict())
    return {"session": session, "profiles": names, "report": report}


def tool_use_clone(session: str = DEFAULT_NAME, force: bool = False) -> dict:
    """Restart this session on a copy-on-write clone of your real profile.

    Covers what cookie injection cannot: IndexedDB, service workers, session
    storage, and every Chrome profile at once, all correctly scoped because
    Chrome opened the real directory. Costs about 4 seconds and almost no disk.

    Refuses on a Google host unless forced: two browsers holding one device-bound
    Google session is exactly what signs the user out of their real Chrome, and
    dedicated (latchkey's own signed-in-once profile) already carries Google
    safely. Set force=true or LATCHKEY_GOOGLE_MODE=clone to override deliberately.
    """
    if not force and google.google_mode() != "clone":
        try:
            here = _on_session(session, lambda browser: browser.state().url)
        except Exception:
            here = ""
        if google.uses_google_session(here):
            return {"session": session, "refused": True, "mode": "unchanged",
                    "note": ("Not switching a Google host to clone: two browsers on one "
                             "device-bound Google session is what signs the user out of their "
                             "real Chrome. Google, Gmail and YouTube use mode dedicated - open "
                             "with no mode, or latchkey_login to sign latchkey's own profile in "
                             "(see latchkey_help topic google). Pass force=true only if the user "
                             "explicitly wants the clone route.")}
    base = registry.spec_of(session)
    spec = replace(base, mode="clone", auto=False) if base is not None \
        else SessionSpec(mode="clone")
    registry.get(session, spec=spec, recreate=True)
    out = _on_session(session, lambda browser: {
        "clone": browser.clone_info, "report": browser.report.as_dict()})
    return {"session": session, "mode": "clone", **out}


def tool_clone_status() -> dict:
    """What the current profile clone holds: per-profile cookie counts, IndexedDB
    origins, localStorage files, and whether it is stale."""
    from . import profile as profile_mod
    return profile_mod.describe()


def tool_show_cookies(host: str, limit: int | None = None) -> dict:
    """Cookie names for a site, values masked: the counts, then the likeliest-to-matter first.

    A summary rather than every row - a busy site's jar ran to 23.6K characters in one
    reply - with the session-looking cookies leading, since those are the ones anyone asks
    this about.
    """
    count = max(1, min(_int(limit, default_for("cookies")), 1000)) if limit else \
        default_for("cookies")
    found = ck.load(host)
    rows = sorted(found, key=lambda c: (not ck.authish(c.name), c.host, c.name))
    hosts: dict[str, int] = {}
    for c in found:
        hosts[c.host] = hosts.get(c.host, 0) + 1
    out: dict[str, Any] = {
        "host": host, "total": len(found),
        "auth_like": sum(1 for c in found if ck.authish(c.name)),
        "partitioned": sum(1 for c in found if c.partitioned),
        "session_only": sum(1 for c in found if c.is_session),
        "by_host": dict(sorted(hosts.items(), key=lambda kv: -kv[1])[:10]),
        "cookies": [{"host": c.host, "name": c.name, "masked": ck.redact(c.value, 3),
                     "flags": ",".join(flag for flag, on in (
                         ("auth", ck.authish(c.name)), ("httpOnly", c.http_only),
                         ("partitioned", c.partitioned), ("session", c.is_session)) if on)}
                    for c in rows[:count]]}
    if google.is_google_host(host):
        out["google"] = ("A Google account session is device-bound, so an inject copy of "
                         "these cookies reads as signed out - and copying one is what signs "
                         "the real Chrome out. Google sites open on latchkey's own profile "
                         "instead (the default), which holds a sign-in of its own.")
    if len(rows) > count:
        out["more"] = f"{len(rows) - count} more cookies: pass limit (up to 1000)"
    return out


def tool_credential_sources(site: str) -> dict:
    """Which credential sources could serve a site, for diagnostics. No secrets."""
    return {"site": site, "sources": credentials.sources_available(site)}


def tool_close() -> dict:
    """Close every session and its Chrome process."""
    return {"closed": registry.close_all()}


# -- the batch surface -------------------------------------------------------
#
# One tool is advertised, `latchkey_batch`, and every other tool is a name one of its
# calls can carry. Thirty-four schemas of instructions on every turn is the largest
# thing this server asks an agent to pay for, and a surface nobody wants to pay for is a
# surface nobody uses. The names are not gone: any of them can be called inside a batch,
# `latchkey_help` hands out any of them in full, and a direct `tools/call` still works
# for an allowlist or a script written against the older surface.

BATCH = "latchkey_batch"
HELP = "latchkey_help"

CALL_SHAPE = "{'tool': name, 'args': {...}}"

# A batch carries several calls at once, and the client caps the reply text wherever its
# cap happens to land. Cut here, in whole strings and counted, rather than mid-value
# there. The default is the budget's (BUDGETS: 40,000, or 12,000 compact), and the most
# anyone may ask for stays under the client's own 60,000-character cap on purpose.
BATCH_MAX_CHARS_LIMIT = 50_000
PARALLEL_WORKERS = 8

# What the client waits for one tool call, and what a batch gives itself.
#
# The client is Lattice, and it aborts a call after `MCP_CALL_TIMEOUT_MS` (30000, in
# src/main/mcp/manager.ts). Nothing this server does after that is ever read: the caller
# has already been told the call timed out, and the work left behind still occupies the
# session's lane, so the *next* call is slow too. That is the shape of "it keeps timing
# out and I do not know why". So the lane's own guard (600s) is not the binding deadline -
# these are: a batch stops handing out work while there is still time to answer, and says
# per call what was left.
CLIENT_WAIT_S = 30.0
BATCH_BUDGET_S = 24.0
BATCH_BUDGET_LIMIT_S = 28.0    # the most a caller may ask for; the client stops at 30
LOGIN_WAIT_S = 25.0            # the longest a single blocking call may wait
ASSIST_WAIT_S = 90.0          # the longest one assist call blocks before handing the wait back


# -- reading what a model actually sent ---------------------------------------
#
# One advertised tool whose argument is a list of nested calls is the cheapest surface
# there is to carry, and it is also the hardest shape for a small model to emit. The
# failures are not random: a stringified JSON argument, one call where a list was asked
# for, `name` for `tool`, a bare tool name with the server prefix still attached, a verb
# spelled `action` instead of `do`. Every one of those is a model that knew exactly what
# it wanted. Refusing them buys nothing, so they are read rather than rejected - and a
# name that is merely close gets told which one was meant.

NAME_KEYS = ("tool", "name", "tool_name", "toolName", "function", "method")
ARG_KEYS = ("args", "arguments", "params", "parameters", "input", "kwargs")


def _loads(value: Any) -> Any:
    """A JSON string read back, or the value as it came. Models stringify nested JSON."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def resolve_tool(name: str) -> str | None:
    """The tool a name means, allowing for the ways a name arrives mangled.

    `latchkey_open`, `open`, `mcp__latchkey__latchkey_open` and `latchkey.open` are all
    one tool. A client that namespaces its tools, a model that drops the prefix because
    the description's index prints the short form, and a model that keeps the client's
    own prefix are three different mistakes with the same right answer.
    """
    raw = str(name or "").strip().strip("\"'")
    if not raw:
        return None
    if raw in DISPATCH:
        return raw
    short = raw.rsplit("__", 1)[-1].replace("-", "_").replace(".", "_").lower()
    for candidate in (short, f"latchkey_{short}", short.replace("latchkey_", "")):
        if candidate in DISPATCH:
            return candidate
        if f"latchkey_{candidate}" in DISPATCH:
            return f"latchkey_{candidate}"
    bare = short[len("latchkey_"):] if short.startswith("latchkey_") else short
    target = _SYNONYM_INDEX.get(bare)
    if target and f"latchkey_{target}" in DISPATCH:
        return f"latchkey_{target}"
    return None


# Names models reach for when they guess a tool instead of reading the index, mapped to the tool
# that does that job. Every one was emitted by Qwen3.6-35B-A3B driving latchkey through Lattice on
# 2026-09-12 (`latchkey_read_page`, `latchkey_get_text`, `latchkey_get_text_content`,
# `latchkey_text_content`, `latchkey_page_text`, `latchkey_evaluate`), and each wrong guess cost a
# whole model round to be told "no tool called …". Consulted only after exact and prefix resolution
# fail, so a real tool name always wins.
TOOL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "text": ("page_text", "get_text", "get_page_text", "text_content", "get_text_content",
             "read_page", "read_text", "page_content", "get_content", "inner_text", "innertext",
             "extract_text", "visible_text", "body_text", "get_page_content"),
    "eval": ("evaluate", "execute_script", "execute_js", "run_js", "run_script", "javascript",
             "js", "script", "exec_js"),
    "open": ("navigate", "goto", "go_to", "visit", "open_url", "load_url", "browse", "navigate_to"),
    "snapshot": ("accessibility_tree", "a11y", "get_snapshot", "page_snapshot", "read_dom", "dom"),
    "pilot": ("errand", "autopilot", "auto", "do_errand", "run_errand", "do_task", "complete_task"),
    "screenshot": ("take_screenshot", "capture_screenshot", "screen_shot"),
    "links": ("get_links", "list_links"),
    "pages": ("tabs", "list_tabs", "list_pages", "url", "get_url", "current_url", "title",
              "get_title", "page_info"),
}

# Names that have no tool on purpose, with what to do instead. A raw-HTML dump is the single most
# expensive thing a small model can pull into its window, so there is no `html` tool - but "no tool
# called latchkey_html" alone sent Qwen round the loop again instead of to the one call that works.
MISSING_TOOL_HINTS: dict[str, str] = {
    **dict.fromkeys(("html", "get_html", "page_html", "source", "page_source", "outer_html",
                     "get_source", "dom_html"),
                    " There is no raw-HTML tool (it floods the context). Read one attribute or "
                    "element with latchkey_eval, e.g. {\"tool\": \"latchkey_eval\", \"args\": "
                    "{\"js\": \"document.querySelector('relative-time')?.getAttribute('datetime')\"}}, "
                    "or the structure with latchkey_snapshot."),
    **dict.fromkeys(("get_attribute", "attribute", "get_attr", "attr", "read_attribute",
                     "element_attribute"),
                    " Attributes are read with latchkey_eval: {\"tool\": \"latchkey_eval\", "
                    "\"args\": {\"js\": \"document.querySelector('SELECTOR')?.getAttribute('NAME')\"}}."),
}
_SYNONYM_INDEX = {alias: target for target, aliases in TOOL_SYNONYMS.items() for alias in aliases}


def nearest_tool(name: str) -> str:
    """"did you mean" for a name nothing resolves to, as a sentence or an empty string."""
    short = str(name or "").rsplit("__", 1)[-1].lower()
    short = short[len("latchkey_"):] if short.startswith("latchkey_") else short
    # Matched on the bare names: every tool here shares the `latchkey_` prefix, and
    # comparing with it in makes every name look like every other one - which is how
    # "zzzzzzzz" came back as "did you mean latchkey_act".
    bare = {full[len("latchkey_"):]: full for full in DISPATCH if full.startswith("latchkey_")}
    close = difflib.get_close_matches(short, list(bare), n=2, cutoff=0.6)
    if not close:
        return ""
    return " Did you mean " + " or ".join(bare[one] for one in close) + "?"


def _call_of(raw: Any, index: int) -> tuple[str, dict]:
    """One entry of `calls`, read as forgivingly as it can be read without guessing.

    A name it cannot resolve is still an error - running the wrong tool is worse than
    saying so - but the error names the nearest one and the list to look at.
    """
    raw = _loads(raw)
    if isinstance(raw, str):
        raw = {"tool": raw}           # a bare name, for a call that takes no arguments
    if not isinstance(raw, dict):
        raise ValueError(f"call {index} is {type(raw).__name__}, not an object; each "
                         f"call is {CALL_SHAPE}")
    name = next((raw[key] for key in NAME_KEYS
                 if isinstance(raw.get(key), str) and raw[key].strip()), None)
    if name is None or resolve_tool(name) is None:
        as_action = _action_call(raw, name)
        if as_action is not None:
            return as_action
    if name is None:
        raise ValueError(f"call {index} names no tool; each call is {CALL_SHAPE}. "
                         f"latchkey_help lists the names.")
    resolved = resolve_tool(name)
    if resolved is None:
        bare = name.strip().rsplit("__", 1)[-1].lower()
        bare = bare[len("latchkey_"):] if bare.startswith("latchkey_") else bare
        raise ValueError(f"call {index}: no tool called {name.strip()!r}."
                         f"{MISSING_TOOL_HINTS.get(bare) or nearest_tool(name)} latchkey_help "
                         f"lists all {len(CATALOG)} of them.")
    args = {}
    for key in ARG_KEYS:
        found = _loads(raw.get(key))
        if isinstance(found, dict):
            args = found
            break
        if found not in (None, "", {}):
            raise ValueError(f"call {index} ({resolved}): {key!r} is "
                             f"{type(found).__name__}, not an object of arguments")
    if not args:
        # Some models put the arguments beside the name instead of under it. That is
        # unambiguous as long as what is left is not itself a name or an args key.
        loose = {k: v for k, v in raw.items() if k not in NAME_KEYS + ARG_KEYS}
        if loose:
            args = loose
    return resolved, normalise_args(resolved, args)


# Tools whose name is also a page verb: `{"tool": "screenshot"}` is the tool, not the verb.
_VERB_TOOLS = {"goto": "latchkey_open", "new_page": "latchkey_new_page",
               "back": "latchkey_history", "forward": "latchkey_history",
               "reload": "latchkey_history"}


def _action_call(raw: dict, name: Any) -> tuple[str, dict] | None:
    """A batch call that is really a page action - `{"tool": "click", "ref": "e7"}`.

    A model that has just read "act on refs" often writes the verb where the tool goes, or
    writes the action itself as the call with no tool at all. Both say exactly what they
    want, so they become the call that does it: a navigation is latchkey_open, history is
    latchkey_history, anything else is one latchkey_act.
    """
    fields = {}
    for key in ARG_KEYS:
        found = _loads(raw.get(key))
        if isinstance(found, dict):
            fields.update(found)
    fields.update({k: v for k, v in raw.items() if k not in NAME_KEYS + ARG_KEYS})
    bare = str(name or "").rsplit("__", 1)[-1].lower()
    bare = bare[len("latchkey_"):] if bare.startswith("latchkey_") else bare
    verb = automation.verb_of(bare) if isinstance(name, str) else ""
    if not verb and name is None:
        spelled = next((fields.get(key) for key in automation.VERB_KEYS
                        if isinstance(fields.get(key), str)), None)
        verb = automation.verb_of(spelled) if spelled else ""
        if verb:
            for key in automation.VERB_KEYS:
                if automation.verb_of(fields.get(key)) == verb:
                    fields.pop(key)
                    break
    if not verb:
        return None
    session = fields.pop("session", None)
    extra = {"session": session} if session else {}
    if verb in _VERB_TOOLS:
        tool = _VERB_TOOLS[verb]
        if tool == "latchkey_history":
            return tool, {"url": verb, **extra}
        url = next((fields.get(key) for key in automation.URL_KEYS if fields.get(key)), None)
        if tool == "latchkey_open" and url:
            return tool, {"url": url, **extra}
        if tool == "latchkey_new_page":
            return tool, {**({"url": url} if url else {}), **extra}
    return "latchkey_act", {"actions": [{"do": verb, **fields}], **extra}


# Other names models give arguments. Only applied when the tool does not take the name given
# and does take the one it maps to, so nothing a tool really accepts is ever renamed.
ARG_ALIASES: dict[str, dict[str, str]] = {
    "*": {"href": "url", "link": "url", "uri": "url", "address": "url", "page": "url"},
    "latchkey_text": {"max_chars": "limit", "chars": "limit", "length": "limit",
                      "max_length": "limit", "start": "offset", "from": "offset"},
    "latchkey_frame_text": {"max_chars": "limit", "frame": "index", "i": "index"},
    "latchkey_snapshot": {"limit": "max_chars", "type": "mode", "format": "mode",
                          "view": "mode", "scope": "selector", "css": "selector",
                          "within": "selector"},
    "latchkey_find": {"text": "query", "q": "query", "search": "query", "name": "query",
                      "description": "query", "label": "query", "max": "limit"},
    "latchkey_eval": {"script": "js", "code": "js", "expression": "js",
                      "javascript": "js", "function": "js", "limit": "max_chars"},
    "latchkey_act": {"action": "actions", "steps": "actions", "step": "actions",
                     "commands": "actions"},
    "latchkey_pilot": {"task": "goal", "objective": "goal", "instruction": "goal",
                       "maxSteps": "max_steps", "max": "max_steps", "budget": "max_steps",
                       "minConfidence": "min_confidence", "confidence": "min_confidence",
                       "values": "type", "inputs": "type", "text": "type",
                       "shortcuts": "keys", "stop": "until", "off_site": "offsite",
                       "cross_site": "offsite", "allow_offsite": "offsite"},
    "latchkey_wait": {"condition": "until", "for": "until", "type": "until",
                      "timeout": "timeout_ms"},
    "latchkey_wait_for_login": {"timeout": "timeout_s"},
    "latchkey_session_open": {"session": "name", "session_name": "name",
                              "profile": "account", "identity": "account",
                              "login": "account", "as": "account"},
    "latchkey_session_close": {"session": "name"},
    "latchkey_history": {"direction": "url", "action": "url", "do": "url", "to": "url"},
    "latchkey_screenshot": {"file": "path", "filename": "path", "out": "path",
                            "fullPage": "full_page"},
    "latchkey_switch": {"tab": "target", "id": "target", "index": "target"},
    "latchkey_close_page": {"tab": "target", "id": "target", "index": "target"},
    "latchkey_help": {"tool": "name", "about": "topic"},
    "latchkey_login": {"site": "url", "profile": "account", "identity": "account",
                       "name": "account", "as": "account"},
    "latchkey_login_status": {"timeout": "wait_s", "wait": "wait_s", "timeout_s": "wait_s",
                              "profile": "account", "name": "account"},
    "latchkey_show_cookies": {"site": "host", "domain": "host", "url": "host"},
    "latchkey_profiles": {"site": "host", "domain": "host", "url": "host"},
    "latchkey_sites": {"filter": "host", "domain": "host"},
}
_WAIT_SHORTHANDS = ("text", "gone", "selector", "hidden", "url", "title")


def _accepts(name: str) -> set[str]:
    """Every argument a tool's function takes - the schema can advertise fewer."""
    import inspect
    fn = DISPATCH.get(name)
    try:
        return set(inspect.signature(fn).parameters) if fn else set()
    except (TypeError, ValueError):
        return set()


def normalise_args(name: str, args: Any) -> Any:
    """A tool's arguments, with the other names models give them read as the real ones.

    Idempotent, and it never renames an argument the tool takes. What it does do: `session`
    passed to a tool that has no session is dropped (a model that passes it everywhere is
    being careful, not wrong), `{"text": "Welcome"}` to latchkey_wait is `until: text`, and
    a single action handed to latchkey_act on its own is a list of one.
    """
    if not isinstance(args, dict) or name not in DISPATCH:
        return args
    takes = _accepts(name)
    out = dict(args)
    if name == "latchkey_act" and "actions" not in out and not any(
            isinstance(_loads(out.get(alias)), (list, dict))
            for alias in ARG_ALIASES["latchkey_act"]):
        loose = {k: v for k, v in out.items() if k != "session"}
        if loose:
            out = {"actions": [loose], **({"session": out["session"]} if "session" in out
                                          else {})}
    aliases = {**ARG_ALIASES["*"], **ARG_ALIASES.get(name, {})}
    for alias, real in aliases.items():
        if alias in out and alias not in takes and real in takes and real not in out:
            out[real] = out.pop(alias)
    if "session" in out and "session" not in takes and name != BATCH:
        out.pop("session")
    for key in ("tab", "tab_id", "tabId"):
        # the tab id a model saw in an earlier result, repeated on a tool that reads the current tab
        if key in out and key not in takes and "target" not in takes and name != BATCH:
            out.pop(key)
    # The call's own tool name repeated inside its args ({"tool": "latchkey_eval", "js": ...}
    # as the args of a latchkey_eval call): the model restating what it is calling.
    if "tool" in out and "tool" not in takes and resolve_tool(str(out.get("tool") or "")):
        out.pop("tool")
    if name == "latchkey_wait" and out.get("until"):
        # A condition AND a number: the number is how long to wait for it, not a sleep.
        for key, scale in (("ms", 1), ("timeout_s", 1000), ("seconds", 1000), ("wait_ms", 1)):
            if key in out and "timeout_ms" not in out:
                out["timeout_ms"] = _int(out.pop(key), 0) * scale
    if name == "latchkey_wait_for_login":
        for key, scale in (("timeout_ms", 0.001), ("ms", 0.001), ("wait_s", 1), ("wait", 1),
                           ("seconds", 1)):
            if key in out and "timeout_s" not in out:
                out["timeout_s"] = max(1, int(round(_int(out.pop(key), 0) * scale)))
    if name == "latchkey_wait" and not out.get("until"):
        for key in _WAIT_SHORTHANDS:
            if key in out:
                out["until"] = key
                out.setdefault("value", out.pop(key))
                break
        else:
            for key in ("ms", "seconds", "sleep"):
                if key in out:
                    amount = out.pop(key)
                    ms = _int(amount, 0) * (1000 if key == "seconds" else 1)
                    out.update({"until": "time", "value": str(ms)})
                    break
    return out


def calls_of(calls: Any) -> list[tuple[str, dict]]:
    """Every call in a batch, checked, as `(tool, args)` in the order given."""
    calls = _loads(calls)
    if calls is None:
        raise ValueError(f"a batch needs 'calls': a list of {CALL_SHAPE}")
    if isinstance(calls, dict):
        calls = [calls]               # one call where a list was asked for
    if isinstance(calls, str):
        calls = [calls]
    if not isinstance(calls, list):
        raise ValueError(f"'calls' is {type(calls).__name__}, not a list of calls")
    return [_call_of(raw, index) for index, raw in enumerate(calls)]


def _skipped_because(stopped: str) -> str:
    return (f"not run: {stopped} failed and a failed call stops the batch. Pass "
            f"continue_on_error=true to run this one anyway")


def _one_call(make: Callable[[str, dict], tuple[Any, bool]], name: str, args: dict) -> dict:
    """Run one call of a batch and report it the way a batch always reports a call."""
    if name == BATCH:
        return {"tool": name, "ok": False,
                "error": "a batch cannot hold another batch: put its calls in this one "
                         "list, in the order you want them"}
    value, failed = make(name, args)
    if failed:
        return {"tool": name, "ok": False, "error": value}
    return {"tool": name, "ok": True, "result": value}


def _out_of_time(budget_s: float) -> str:
    """Why a call in a batch did not run, in the terms the caller can act on."""
    return (f"not run: the batch's {budget_s:.0f}s budget was spent before this call "
            f"started, and the client waits only {CLIENT_WAIT_S:.0f}s for the whole "
            f"request. Send what did not run in another batch.")


def batch_budget(timeout_ms: int | None) -> float:
    """How long a batch may take. The client's own patience is the ceiling on this."""
    if not timeout_ms:
        return BATCH_BUDGET_S
    return max(1.0, min(float(timeout_ms) / 1000.0, BATCH_BUDGET_LIMIT_S))


def run_calls(calls: list[dict], continue_on_error: bool = False,
              parallel: bool = False, budget_s: float = BATCH_BUDGET_S) -> list[dict]:
    """Make the calls in order, and return one entry per call.

    Handing out work stops once the budget is spent, and every call that did not get to
    run says so in its own entry. The budget is the whole point: the client waits
    CLIENT_WAIT_S for the request and then reports a timeout, so a batch still running at
    that point is a batch whose answer nobody ever reads. Four calls done and one plainly
    not run beats a batch that vanishes.
    """
    deadline = time.monotonic() + budget_s
    plain = _call_tool

    def make(name: str, args: dict) -> tuple[Any, bool]:
        if time.monotonic() >= deadline:
            return _out_of_time(budget_s), True
        previous = getattr(_CALL, "deadline", None)
        _CALL.deadline = deadline          # so a wait inside the batch ends with the batch
        try:
            return plain(name, args)
        finally:
            _CALL.deadline = previous
    parsed = calls_of(calls)
    if parallel:
        return _run_parallel(parsed, make)
    entries: list[dict] = []
    stopped: str | None = None
    out_of_time = False
    for name, args in parsed:
        if stopped is not None:
            # A batch that ran out of time should not blame the call it stopped on: the
            # calls after it did not run because there was no time left, and saying
            # otherwise sends the caller looking for a failure that never happened.
            entries.append({"tool": name, "ok": False,
                            "skipped": (_out_of_time(budget_s) if out_of_time
                                        else _skipped_because(stopped))})
            continue
        entry = _one_call(make, name, args)
        if not entry["ok"] and str(entry.get("error", "")).startswith("not run:"):
            # Out of time is not a failure of the call: it is a call that never happened,
            # and the report counts those as skipped.
            out_of_time = True
            entry = {"tool": name, "ok": False, "skipped": entry["error"]}
        entries.append(entry)
        if not entry["ok"] and not continue_on_error:
            stopped = name
    return entries


def _run_parallel(parsed: list[tuple[str, dict]], make) -> list[dict]:
    """Overlap the calls that cannot affect each other: different sessions.

    Calls for *one* session share a worker, because the order a session's own calls run
    in is the thing the lanes exist to keep. Nothing is skipped here either: by the time
    a failure comes back, the other calls are already in flight.
    """
    groups: dict[str | None, list[int]] = {}
    for index, (name, args) in enumerate(parsed):
        groups.setdefault(lane_of_call(name, args), []).append(index)
    entries: list[dict] = [{} for _ in parsed]
    budget = getattr(_CALL, "budget", None)

    def run_group(indexes: list[int]) -> None:
        _CALL.budget = budget
        for index in indexes:
            name, args = parsed[index]
            entries[index] = _one_call(make, name, args)

    with ThreadPoolExecutor(max_workers=max(1, min(len(groups), PARALLEL_WORKERS))) as pool:
        list(pool.map(run_group, groups.values()))
    return entries


def _assemble(entries: list[dict], max_chars: int) -> dict:
    """The batch's answer: what ran, what failed, what was skipped, and the results."""
    failed = sum(1 for entry in entries if not entry.get("ok") and "skipped" not in entry)
    skipped = sum(1 for entry in entries if "skipped" in entry)
    return _fit({"ok": not failed and not skipped, "ran": len(entries) - skipped,
                 "failed": failed, "skipped": skipped, "results": entries}, max_chars)


def _char_budget(max_chars: Any) -> int:
    """A reply budget the client can actually deliver, whatever was asked for."""
    return max(1_000, min(int(max_chars), BATCH_MAX_CHARS_LIMIT))


def _longest_string(node: Any, holder: Any = None, key: Any = None
                    ) -> tuple[Any, Any, str] | None:
    """The longest string anywhere in `node`, as (what holds it, its key, the string)."""
    if isinstance(node, str):
        return (holder, key, node)
    best: tuple[Any, Any, str] | None = None
    children = list(node.items()) if isinstance(node, dict) else \
        list(enumerate(node)) if isinstance(node, list) else []
    for child_key, value in children:
        found = _longest_string(value, node, child_key)
        if found is not None and (best is None or len(found[2]) > len(best[2])):
            best = found
    return best


def _fit(out: dict, max_chars: int) -> dict:
    """Cut the longest strings in the results until the reply fits `max_chars`.

    In whole strings, and said out loud. A batch carrying four page reads is otherwise
    cut by the client at a character that lands in the middle of one of them, which
    leaves text nobody can parse and no way to tell how much went missing.
    """
    cut = 0
    for _ in range(500):
        size = len(json.dumps(out, indent=2, default=str))
        if size <= max_chars:
            break
        found = _longest_string(out)
        if found is None or len(found[2]) < 200:
            break                 # nothing left that is worth cutting
        holder, key, value = found
        keep = max(0, len(value) - (size - max_chars) - 500)
        if keep >= len(value):
            break
        holder[key] = value[:keep] + f"...[+{len(value) - keep} chars cut to fit]"
        cut += len(value) - keep
    if cut:
        out["cut_chars"] = cut
        out["note"] = (f"{cut} characters of the longest results were cut to fit "
                       f"max_chars={max_chars}. Get the rest with latchkey_text's `offset`, a "
                       f"snapshot `selector`, or fewer calls per batch.")
    return out


def _signature(spec: dict, session: bool = True, most: int | None = None) -> str:
    """A tool's arguments as they read in an index line: `url*, session`."""
    schema = spec.get("inputSchema") or {}
    required = set(schema.get("required") or [])
    properties = schema.get("properties") or {}
    names = [f"{name}*" if name in required else name for name in properties
             if session or name != "session"]
    return ", ".join(names[:most] if most else names)


def _summary(spec: dict, limit: int = 130) -> str:
    """The first thing a tool's description says, cut to fit one index line."""
    first = (spec.get("description") or "").strip().split("\n\n")[0].replace("\n", " ")
    sentence = first.split(". ")[0].strip()
    if sentence and not sentence.endswith((".", "!", "?")):
        sentence += "."
    if len(sentence) <= limit:
        return sentence
    return sentence[:limit - 1].rstrip() + "\u2026"


def _batch_spec(catalog: list[dict]) -> dict:
    """The advertised tool: one schema, and the index of the other names in it.

    The index is generated from the catalog, so a tool added without a line here fails a
    test rather than quietly becoming a name no agent ever learns about. The tools a task
    actually uses are listed with their arguments; the rest by name, because a name is all it
    takes to ask `latchkey_help` for the rest.
    """
    by_name = {spec["name"]: spec for spec in catalog}
    core = " ".join(f"{name}({_signature(by_name[name], session=False, most=4)})"
                    for name in INDEX_TOOLS if name in by_name)
    rest = ", ".join(spec["name"] for spec in catalog if spec["name"] not in INDEX_TOOLS)
    return {
        "name": BATCH,
        "description": f"{BATCH_PROSE}\nTools (* = required): {core}\nAlso: {rest}.\n\n"
                       f"{VERDICT_RULES}",
        "inputSchema": {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "description": "In order. Each: {\"tool\": name, \"args\": {...}}.",
                    "items": {"type": "object",
                              "properties": {"tool": {"type": "string"},
                                             "args": {"type": "object"}},
                              "required": ["tool"]}},
                "continue_on_error": {"type": "boolean",
                                      "description": "run the calls after a failure"},
                "parallel": {"type": "boolean",
                             "description": "let different sessions' calls overlap"},
                "timeout_ms": {"type": "integer",
                               "description": f"default {int(BATCH_BUDGET_S * 1000)}, max "
                                              f"{int(BATCH_BUDGET_LIMIT_S * 1000)}"},
                "max_chars": {"type": "integer",
                              "description": "cut the reply to this size (default 40000; "
                                             "12000 compact)"},
                "budget": {"type": "string", "enum": ["normal", "compact"],
                           "description": "compact: smaller pages and lists"},
            },
            "required": ["calls"],
        },
    }


# The one description every turn carries. It has one job - let a small model build a correct
# batch on the first try - and it used to spend 7,316 characters doing it: a paragraph per
# argument, the index with a sentence per tool, and the verdict rules twice over. Everything
# that is not needed to make the first call now lives in `latchkey_help`, by topic.
BATCH_PROSE = (
    "Drive a Chrome that is already signed in as the user. One call runs several latchkey "
    "tools in order; each result comes back as {tool, ok, result|error|skipped}.\n"
    'Example: {"calls": [{"tool": "latchkey_open", "args": {"url": "https://github.com"}}, '
    '{"tool": "latchkey_snapshot", "args": {}}]}\n'
    "Loop: latchkey_open -> latchkey_snapshot (refs like e7) or latchkey_find -> latchkey_act "
    '{"actions": [{"do": "click", "ref": "e7"}]} -> latchkey_snapshot mode "diff".\n'
    "A failure skips later calls unless continue_on_error; unstarted calls come back skipped. "
    "Page tools take session (default \"default\")."
)

VERDICT_RULES = (
    "Google, Gmail and YouTube run on latchkey's own profile: signed in once, renewed by "
    "latchkey, the user's Chrome never copied or signed out. Signed out there means it needs "
    "that one sign-in - latchkey_accounts, then latchkey_login (account='<name>' for a second "
    "account). Other sites use the user's Chrome cookies.\n"
    "verdict is a hint; trust title and text. logged-out: follow next_step. blocked: an "
    "anti-bot wall, not a login problem. unclear: carry on.\n"
    "NEVER ask the user to type a password or 2FA code into chat.\n"
    "latchkey_help(topic) explains act, snapshot, wait, google, verdicts, batch, budget, modes."
)

# What the server tells a client about itself at `initialize`. Lattice shows the first 500
# characters of this as the server's entry in its tool picker, so it is written to be whole at
# that length.
INSTRUCTIONS = (
    "latchkey: a headless Chrome already signed in as the user. Call latchkey_batch with "
    "calls like {\"tool\": \"latchkey_open\", \"args\": {\"url\": \"...\"}}. Google, Gmail and "
    "YouTube run on latchkey's own profile: signed in once, renewed there, their Chrome "
    "never copied or signed out. Signed out there means it needs that sign-in - "
    "latchkey_accounts, then latchkey_login (account='<name>' for a second account). Other "
    "sites use their Chrome cookies. Never ask the user to type a password or code into chat."
)


def tool_batch(calls: list[dict] | None = None, continue_on_error: bool = False,
               parallel: bool = False, timeout_ms: int | None = None,
               max_chars: int | None = None, budget: str | None = None) -> dict:
    """Run several tool calls in one request and report each one's own result.

    The whole advertised surface of this server: `calls` is a list of calls, each naming
    any tool `latchkey_help` lists. The stdio server does not come through here for a
    well-formed batch - it sequences the calls across their sessions' lanes itself - but
    this is the same batch, made in process, which is what a test or a script gets.
    """
    previous = getattr(_CALL, "budget", None)
    if budget:
        _CALL.budget = budget
    try:
        entries = run_calls(calls, continue_on_error=bool(continue_on_error),
                            parallel=bool(parallel), budget_s=batch_budget(timeout_ms))
        size = _int(max_chars, default_for("batch")) if max_chars else default_for("batch")
        return _assemble(entries, _char_budget(size))
    finally:
        _CALL.budget = previous


def tool_help(name: str | None = None, topic: str | None = None) -> dict:
    """A topic, one tool in full, or every tool a batch can call with its arguments.

    `tools/list` carries one short description, so this is where everything else is: how
    to act, how to read, the Google sign-in, what a verdict means - read when it is needed
    instead of on every turn. A name that is a topic is taken as the topic.
    """
    wanted = str(topic or "").strip().lower()
    if not wanted and name and str(name).strip().lower() in HELP_TOPICS:
        wanted = str(name).strip().lower()
    if wanted:
        wanted = TOPIC_ALIASES.get(wanted, wanted)
        if wanted not in HELP_TOPICS:
            raise ValueError(f"no help topic {topic!r}; topics: {', '.join(HELP_TOPICS)}, or "
                             f"name= one tool")
        return {"topic": wanted, "help": HELP_TOPICS[wanted]}
    if name:
        resolved = resolve_tool(name) or str(name)
        spec = SPEC_OF.get(resolved)
        if spec is None:
            raise ValueError(f"unknown tool {name!r}.{nearest_tool(str(name))} Call "
                             f"latchkey_help with no name for the {len(CATALOG)} that exist, "
                             f"or topic= one of {', '.join(HELP_TOPICS)}")
        detail = HELP_DETAIL.get(spec["name"], "")
        return {"name": spec["name"], "args": _signature(spec), "summary": _summary(spec),
                "description": spec["description"] + (f"\n\n{detail}" if detail else ""),
                "inputSchema": spec["inputSchema"]}
    return {"count": len(CATALOG), "topics": list(HELP_TOPICS),
            "tools": [{"name": spec["name"], "args": _signature(spec),
                       "summary": _summary(spec)} for spec in CATALOG]}


# -- the viewer (the human's window, not an agent's tool) --------------------

def tool_viewer_start(port: int = 8788) -> dict:
    """Start the local viewer server so a human can watch, and show the URL.

    Serves the event stream, the screen and the pointer on 127.0.0.1 only. Point the
    Electron app (or a browser) at the returned url. Safe to call twice: the same
    port returns the server already running.
    """
    from .viewer import start_viewer
    server = start_viewer(port)
    return {"url": server.url(), "port": server.port, "viewers": server.viewer_count()}


def tool_viewers() -> list[dict]:
    """Every latchkey viewer currently listening, newest first, with the live ones only.

    Call this when the viewer app shows an empty window: it names the port whose server
    actually holds the sessions, so the app can be pointed at it instead of at a stale
    server that happened to take the default port first.
    """
    from .viewer import known_viewers
    return known_viewers()


def tool_viewer_stop(port: int = 8788) -> dict:
    """Stop the viewer server. Sessions keep running; only the window closes."""
    from .viewer import stop_viewer
    stop_viewer(port)
    return {"stopped": port}


# -- tool schemas ------------------------------------------------------------
#
# Short on purpose. A schema is read on every turn by every model that has the tool in view,
# and the five largest (session_open, act, snapshot, open, wait) were 1.3-1.7K characters
# each, mostly prose a model needs once. Each description now says what the tool is for in a
# sentence or two; the rest is in HELP_DETAIL, which `latchkey_help(name=...)` hands out.

SESSION_PROP = {"type": "string", "description": "session name (default 'default')"}
VERBS = ("click", "click_at", "fill", "press", "select", "check", "hover", "scroll", "upload", "goto",
         "back", "forward", "reload", "wait", "wait_for", "eval", "screenshot", "new_page",
         "switch", "close_page", "sync", "save_session")
UNTILS = ("text", "gone", "selector", "hidden", "url", "title", "load", "networkidle", "time")


def _schema(props: dict | None = None, required: list[str] | None = None,
            session: bool = True) -> dict:
    properties = dict(props or {})
    if session:
        properties["session"] = SESSION_PROP
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _s(description: str = "") -> dict:
    return {"type": "string", "description": description} if description else {"type": "string"}


def _i(description: str = "") -> dict:
    return {"type": "integer", "description": description} if description else \
        {"type": "integer"}


HELP_SPEC = {
    "name": HELP,
    "description": "Explain a topic, or one tool in full. No arguments lists every tool.",
    "inputSchema": _schema({"topic": {"type": "string",
                                      "enum": ["act", "snapshot", "wait", "google",
                                               "verdicts", "batch", "budget", "modes"]},
                            "name": _s("a tool name")}, session=False),
}

def tool_pilot(goal: str, url: str | None = None, type: list | None = None,
               keys: dict | None = None, until: dict | None = None,
               max_steps: int = pilot_mod.DEFAULT_STEPS, avoid: list | None = None,
               offsite: bool = False, min_confidence: float = jev.DEFAULT_MIN_CONFIDENCE,
               page: bool = True, session: str = DEFAULT_NAME) -> dict:
    """Hand a UI errand to the Jev decision model.

    It reads the page, takes one action per step (click, choose, or type a value
    YOU supplied in `type`) and stops when the goal is met, it is stuck, or a step
    is yours. It never writes text of its own, never presses anything irreversible,
    and stays on the start host unless `offsite`. A hand-back names the obstacle and
    the best candidates: do that one step, then call again with no `url` to resume.
    Needs a Jev endpoint (see latchkey_help topic 'pilot').
    """
    transport = jev.transport()
    if transport is None:
        return {"ok": False, "outcome": "unconfigured", "note": jev.NO_KEY}

    def go(browser):
        usage = jev.new_usage()
        ask = jev.asker(transport, usage)
        # Leave headroom under the client's call ceiling so a long errand hands back
        # a partial result instead of timing the whole call out with nothing.
        deadline = min(pilot_mod.DEFAULT_DEADLINE_S, max(6.0, time_left(CLIENT_WAIT_S) - 3.0))
        return pilot_mod.run_errand(
            browser, goal, jev_ask=ask, usage=usage, url=url, type_values=type or [],
            keys=keys or {}, until=until, max_steps=max_steps, avoid=avoid or [],
            offsite=offsite, min_confidence=min_confidence, page=page, deadline_s=deadline)

    return _on_session(session, go)


CATALOG: list[dict] = [
    {"name": "latchkey_open",
     "description": "Open a URL as the signed-in user. Returns url, title, verdict (a hint), "
                    "and next_step when the site is not signed in.",
     "inputSchema": _schema({"url": _s()}, ["url"])},
    {"name": "latchkey_pilot",
     "description": "Hand a UI errand to a fast decision model (Jev): it reads the page and takes "
                    "one action per step (click, choose, type a value from `type`), stopping at "
                    "the goal, when stuck, or when a step is yours. Never types its own text or "
                    "presses anything irreversible; on-host unless offsite. latchkey_help pilot.",
     "inputSchema": _schema({
         "goal": _s("The destination or end state, literally."),
         "url": _s("Start here; omit to stay on the tab."),
         "type": {"type": "array", "items": {"type": ["string", "object"]}},
         "keys": {"type": "object"}, "until": {"type": "object"},
         "max_steps": _i(), "avoid": {"type": "array", "items": _s()},
         "offsite": {"type": "boolean"}, "min_confidence": {"type": "number"}},
         ["goal"])},
    {"name": "latchkey_act",
     "description": "Do things on the page, in order, and return the page after. Act on refs "
                    "from latchkey_snapshot or latchkey_find.",
     "inputSchema": _schema({"actions": {
         "type": "array",
         "description": 'e.g. [{"do": "click", "ref": "e7"}, {"do": "fill", "ref": "e4", '
                        '"value": "hi"}]',
         "items": {"type": "object",
                   "properties": {"do": _s("|".join(VERBS)), "ref": _s(), "selector": _s(),
                                  "frame": _i(), "x": {"type": "number"},
                                  "y": {"type": "number"},
                                  "value": {"type": ["string", "number", "boolean"]},
                                  "url": _s(), "key": _s()}}}},
         ["actions"])},
    {"name": "latchkey_snapshot",
     "description": "The page as its controls, each with a ref to act on: "
                    "'button \"Sign in\" [ref=e7]'.",
     "inputSchema": _schema({"mode": {"type": "string", "enum": list(a11y.MODES),
                                      "description": "interactive (default): controls; "
                                                     "text: words; diff: what changed"},
                             "selector": _s("CSS: read one part"),
                             "max_chars": _i(), "url": _s("open this first")})},
    {"name": "latchkey_wait",
     "description": "Wait for one thing, in one call: a phrase, an element, a URL, a load.",
     "inputSchema": _schema({"until": {"type": "string", "enum": list(UNTILS)},
                             "value": _s("the phrase, CSS selector, URL part or ms"),
                             "timeout_ms": _i("default 10000")}, ["until"])},
    {"name": "latchkey_find",
     "description": "Find what matches a description ('sign in', 'accept cookies'), best "
                    "first, each with a ref to act on.",
     "inputSchema": _schema({"query": _s(), "limit": _i()}, ["query"])},
    {"name": "latchkey_text",
     "description": "The page's visible words, a window at a time; `more` says the offset "
                    "to read next.",
     "inputSchema": _schema({"limit": _i("characters (default 4000; 2500 compact)"),
                             "offset": _i(), "url": _s("open this first")})},
    {"name": "latchkey_screenshot",
     "description": "Screenshot the current page to a file path.",
     "inputSchema": _schema({"path": _s(), "full_page": {"type": "boolean"}})},
    {"name": "latchkey_eval",
     "description": "Run JavaScript in the page and return JSON (capped; counts as a write).",
     "inputSchema": _schema({"js": _s(), "max_chars": _i()}, ["js"])},
    {"name": "latchkey_links",
     "description": "List links on the current page.",
     "inputSchema": _schema({"limit": _i()})},
    {"name": "latchkey_frames",
     "description": "List the page's iframes, for content the text tools cannot see.",
     "inputSchema": _schema()},
    {"name": "latchkey_frame_text",
     "description": "Read the text inside one iframe, by index from latchkey_frames.",
     "inputSchema": _schema({"index": _i(), "limit": _i()}, ["index"])},
    {"name": "latchkey_sync",
     "description": "Re-read the user's Chrome cookies into this session.",
     "inputSchema": _schema()},
    {"name": "latchkey_wait_for_login",
     "description": "Wait for sign-in; after timeout, suggests a stronger Chrome profile.",
     "inputSchema": _schema({"url": _s(), "timeout_s": _i()}, ["url"])},
    {"name": "latchkey_login",
     "description": "Sign latchkey's own profile in, once - Google included. Opens a Chrome "
                    "window for the user, returns at once; then latchkey_login_status.",
     "inputSchema": _schema({"url": _s("default https://accounts.google.com/"),
                             "account": _s("which login; a new name gets its own profile"),
                             "again": {"type": "boolean",
                                       "description": "add or switch accounts"}},
                            session=False)},
    {"name": "latchkey_login_status",
     "description": "Is latchkey's own profile signed in yet? Waits up to wait_s (max 25).",
     "inputSchema": _schema({"wait_s": _i("default 20"), "account": _s("which login")},
                            session=False)},
    {"name": "latchkey_accounts",
     "description": "Which logins latchkey holds, and which are signed in to Google. Ask it "
                    "when a task names an account, or Google reads as signed out.",
     "inputSchema": _schema(session=False)},
    {"name": "latchkey_assist",
     "description": "Hand the user a real window to pass a wall the agent cannot (a captcha, a "
                    "block, a sign-in). Call with no confirm to get what to ask the user; "
                    "on their yes, confirm=true opens it and carries the earned cookies back.",
     "inputSchema": _schema({"url": _s("the walled page (default: the current one)"),
                             "confirm": {"type": "boolean",
                                         "description": "the user said yes: open the window"},
                             "cancel": {"type": "boolean",
                                        "description": "close an open assist window"},
                             "timeout_s": _i("how long to keep the window (default 300)")})},
    {"name": "latchkey_assist_status",
     "description": "What human-intervention windows are open or recently done, and their state.",
     "inputSchema": _schema(session=False)},
    {"name": "latchkey_save_session",
     "description": "Save this session's cookies and localStorage across restarts.",
     "inputSchema": _schema({"site": _s()})},
    {"name": "latchkey_sessions",
     "description": "List saved sessions (names and counts, never values).",
     "inputSchema": _schema(session=False)},
    {"name": "latchkey_forget_session",
     "description": "Delete a saved session.",
     "inputSchema": _schema({"site": _s()}, ["site"], session=False)},
    {"name": "latchkey_injected",
     "description": "How many cookies this session loaded, and what was held back.",
     "inputSchema": _schema()},
    {"name": "latchkey_pages",
     "description": "List this session's tabs, each with a stable id.",
     "inputSchema": _schema()},
    {"name": "latchkey_new_page",
     "description": "Open a new tab, optionally at a URL, and drive it.",
     "inputSchema": _schema({"url": _s()})},
    {"name": "latchkey_switch",
     "description": "Drive another tab, by id ('t2') or index.",
     "inputSchema": _schema({"target": {"type": ["integer", "string"]}}, ["target"])},
    {"name": "latchkey_close_page",
     "description": "Close a tab by id or index (default: the last one).",
     "inputSchema": _schema({"target": {"type": ["integer", "string"]}})},
    {"name": "latchkey_history",
     "description": "Go back, forward or reload: url is 'back', 'forward' or 'reload'.",
     "inputSchema": _schema({"url": {"type": "string", "enum": ["back", "forward", "reload"]}},
                            ["url"])},
    {"name": "latchkey_sites",
     "description": "Hosts in the user's cookie store with counts, largest first (no values).",
     "inputSchema": _schema({"limit": _i(), "host": _s("substring filter")}, session=False)},
    {"name": "latchkey_profiles",
     "description": "Chrome profiles; with host, which profile holds that site's cookies.",
     "inputSchema": _schema({"host": _s()}, session=False)},
    {"name": "latchkey_use_profiles",
     "description": "Restart on named Chrome profiles; use profile_recommendation when present "
                    "('all' reads every profile).",
     "inputSchema": _schema({"profiles": {"type": ["array", "string"],
                                          "items": {"type": "string"}}})},
    {"name": "latchkey_show_cookies",
     "description": "A site's cookie names, values masked: counts, then session-looking first.",
     "inputSchema": _schema({"host": _s(), "limit": _i()}, ["host"], session=False)},
    {"name": "latchkey_credential_sources",
     "description": "Which credential sources could serve a site. No secrets.",
     "inputSchema": _schema({"site": _s()}, ["site"], session=False)},
    {"name": "latchkey_use_clone",
     "description": "Restart this session on a copy-on-write clone of the user's Chrome "
                    "profile (brings IndexedDB).",
     "inputSchema": _schema()},
    {"name": "latchkey_clone_status",
     "description": "What this server's profile clone holds, and whether it is stale. Each "
                    "server has its own clone: two models never share a window.",
     "inputSchema": _schema(session=False)},
    {"name": "latchkey_session_open",
     "description": "Open a named, isolated browser session; pass its name as `session` to "
                    "other tools. Leave mode out unless you need one.",
     "inputSchema": _schema({"name": _s(),
                             "mode": {"type": "string", "enum": ["auto", "inject", "dedicated",
                                                                 "clone", "real"],
                                      "description": "auto: dedicated (latchkey's own "
                                                     "signed-in profile) for Google/SSO "
                                                     "portals; inject otherwise"},
                             "account": _s("which latchkey login to browse as, e.g. 'school'"),
                             "host": _s("load only this host's cookies"),
                             "read_only": {"type": "boolean",
                                           "description": "refuse anything that sends"},
                             "profiles": {"type": ["array", "string"],
                                          "items": {"type": "string"}},
                             "label": _s(), "cursor": {"type": "boolean"},
                             "recreate": {"type": "boolean"}},
                            session=False)},
    {"name": "latchkey_session_list",
     "description": "List open sessions: name, label, mode, age.",
     "inputSchema": _schema(session=False)},
    {"name": "latchkey_session_close",
     "description": "Close one session and its Chrome.",
     "inputSchema": _schema({"name": _s()}, session=False)},
    {"name": "latchkey_viewer_start",
     "description": "Start the local viewer so a human can watch; returns its URL.",
     "inputSchema": _schema({"port": _i("default 8788")}, session=False)},
    {"name": "latchkey_viewer_stop",
     "description": "Stop the local viewer. Sessions keep running.",
     "inputSchema": _schema({"port": _i()}, session=False)},
    {"name": "latchkey_viewers",
     "description": "Every viewer listening now, newest first, with which holds sessions.",
     "inputSchema": _schema(session=False)},
    {"name": "latchkey_close",
     "description": "Close every session and its Chrome.",
     "inputSchema": _schema(session=False)},
    HELP_SPEC,
]

# The long half of the descriptions above: what `latchkey_help(name=...)` adds.
HELP_DETAIL: dict[str, str] = {
    "latchkey_pilot": (
        "Hand a UI errand to Jev, a fast decision model, instead of looping snapshot -> act "
        "yourself: it reads the page, is offered the controls actually on it (never the "
        "irreversible ones), takes the one Jev picks, waits for the page to settle, and repeats "
        "until the goal is met, it is stuck, or a step is yours. ~10x faster and cheaper per "
        "step than driving it by hand.\n\n"
        "You stay the brain. Give a literal `goal` ('the Releases page', 'dark mode on'); the "
        "exact `type` values it may enter (it never invents text) as [\"query\"] or "
        "[{text, hint}]; `keys` it may press ({\"undo\":\"Mod+z\"}, Mod = Cmd/Ctrl); `until` "
        "when you know the destination ({url|title|text} substrings, or typed:true). It stays "
        "on the starting host unless offsite:true, and hands back rather than guess below "
        "min_confidence (default 0.2).\n\n"
        "A hand-back is a question, not a failure: it names the obstacle (needs text, a login "
        "step, a judgment call, an irreversible step, or nothing here leads to the goal) and "
        "its best candidate refs. Do that one step yourself, then call latchkey_pilot again "
        "with no url to resume on the same tab. Irreversible steps (submit, buy, send, delete) "
        "are always yours - Jev is never even offered them.\n\n"
        "Setup: it needs a Jev endpoint. It uses a local /v1/systemone server on "
        "http://127.0.0.1:8930 if one is up, and/or an sk-or- OpenRouter key in "
        "~/.openbrowser/jev.key for the cloud (LATCHKEY_JEV_BASE_URL and JEV_ESCALATE tune "
        "this). With neither, the tool says so and you drive with latchkey_snapshot + "
        "latchkey_act. Verify visual results yourself; the reply ends with the page outline."),
    "latchkey_open": (
        "Every route to a page goes through the session's navigation guard: http and https "
        "only, and a session whose mode nobody chose moves to the mode the site needs - "
        "dedicated (latchkey's own signed-in-once profile) for Google, Gmail, YouTube and the "
        "known Google-SSO portals, inject (the user's Chrome cookies) elsewhere - and says so "
        "in `note`. A Google page that reads signed out means latchkey's own profile is not "
        "signed in yet: call latchkey_login (see latchkey_help(topic=google)); the user's own "
        "Chrome is never touched. If another Chrome profile has a stronger authentication signal, "
        "`profile_recommendation` gives a structured latchkey_use_profiles call and makes "
        "that switch the primary `next_step`; try it before asking the user to sign in.\n\n"
        "verdict: see latchkey_help(topic="
        "'verdicts')."),
    "latchkey_act": (
        "Verbs: click, hover (ref|selector, force?), click_at (x, y), fill (ref|selector, value), press (ref|"
        "selector, key), select (ref|selector, value), check (ref|selector, state), scroll "
        "(pixels | ref|selector), upload (ref|selector, path), goto (url), back, forward, "
        "reload, wait (ms), wait_for (ref|selector), screenshot (path), eval (js), sync, "
        "save_session (site), new_page (url), switch (target), close_page (target). Selector "
        "actions can pass frame (the index from latchkey_frames) to act inside a cross-origin "
        "iframe; frame actions use selector, not a main-page ref.\n\n"
        "Forgiving on purpose: 'type', 'input' and 'set' mean fill; 'navigate', 'open' and "
        "'visit' mean goto; the verb can be the key ({\"click\": \"e7\"}); a missing verb is "
        "read from the fields (a url alone is goto, a ref with a value is fill, a ref alone "
        "is click); refs copied as '[ref=e7]' work.\n\nIf a click on something you can see "
        "keeps timing out, pass force: true - an overlay is swallowing it. A failure names "
        "the action, its index and where the page was. A read-only session refuses click, "
        "click_at, fill, press, select, check, upload and eval."),
    "latchkey_snapshot": (
        "Modes: interactive (default) controls, landmarks and headings; full adds the page's "
        "text; text is only the words; outline is the page's shape with a selector per part; "
        "diff is only what changed since the last snapshot - the cheap read after an action. "
        "'plain' means text, 'changes' means diff, and so on. selector reads one part; "
        "max_chars raises the budget (a cut snapshot ends in the outline and says so); "
        "viewport_only reads only what is on screen. Refs are never reused, so a stale ref "
        "fails saying what it was. Frames are listed, not entered: latchkey_frames."),
    "latchkey_wait": (
        "until: text (value appears), gone (value disappears), selector (CSS visible), hidden "
        "(CSS gone), url (URL contains value), title, load, networkidle (never true on a page "
        "that polls), time (sleep value ms). A timeout answers with where the page got to. "
        "Shortcuts work too: {\"text\": \"Welcome\"} or until '#inbox'."),
    "latchkey_text": (
        "The prose, not the controls: for something to act on, latchkey_snapshot or "
        "latchkey_find is smaller. limit is characters per read; offset continues a cut read."),
    "latchkey_eval": (
        "A function body with `return` is wrapped for you. The result is cut at max_chars "
        "(8000; 2000 compact) with its full size reported - return less rather than more. "
        "Counts as a write: a read-only session refuses it."),
    "latchkey_login": (
        "Gives latchkey a login of its own, on its own profile - and this IS the Google path: "
        "Google, Gmail and YouTube run on that profile so the user's Chrome is never copied "
        "and never signed out. One sign-in per account, ever. account='<name>' puts a second "
        "Google account on a profile of its own (~/.latchkey/accounts/<name>); no account "
        "means 'default' (~/.latchkey/profile). The window is an ordinary Chrome on that "
        "profile: no automation flags, no debugging port, which is what lets Google's sign-in "
        "accept it at all. The user signs in there - password, 2-step and passkeys all happen "
        "in that window, never in chat. Returns at once; then latchkey_login_status. Sessions "
        "of this server on that profile are closed first (one Chrome per profile), and the "
        "next session that needs it closes the signed-in window itself. url can be another "
        "site to sign in to on that profile."),
    "latchkey_login_status": (
        "Reads that profile's own cookie store, by name, no secrets. account='<name>' asks "
        "about one login in particular. Chrome writes cookies to disk in batches, so a "
        "finished sign-in can take up to 30s to show - which is why this waits rather than "
        "answering once. Google is read here too: a Google session runs on this profile."),
    "latchkey_accounts": (
        "The logins latchkey holds, one profile each, with whether each is signed in to "
        "Google and as which address. Ask it when a task names an account ('my school "
        "account'), or when a Google page reads as signed out - the answer says whether the "
        "fix is a sign-in and which account needs it. Nothing here touches the user's own "
        "Chrome, so nothing here can sign them out of it."),
    "latchkey_wait_for_login": (
        "For an ordinary site it watches the user's Chrome cookie store and carries the new "
        "login across. For Google it watches latchkey's own profile, where latchkey_login's "
        "sign-in lands - that is the same store a Google session runs on, so the wait and the "
        "session agree. A session pinned to some other mode gets wrong-mode, whose next_step "
        "names the mode Google needs. Waits at most 25s a call."),
    "latchkey_session_open": (
        "mode: auto (default) picks per navigation - dedicated for Google and configured "
        "Google-SSO portals (LATCHKEY_GOOGLE_SSO_ACCOUNTS), inject elsewhere. inject copies "
        "the user's Chrome cookies (~1s; Google's account "
        "session is held back, always). dedicated is latchkey's own profile, signed into once "
        "with latchkey_login - this IS what Google, Gmail and YouTube use, and it never touches "
        "the user's Chrome. clone is an opt-in (LATCHKEY_GOOGLE_MODE=clone) copy-on-write clone "
        "of the Chrome profile (~4s, "
        "carries IndexedDB, can trip bot walls, and can sign the user out - avoid for Google). "
        "real attaches to a Chrome with a debugging "
        "endpoint (LATCHKEY_CDP); Chrome 136+ refuses one for the everyday profile. host "
        "narrows cookies by substring and can drop a parent-domain login. read_only refuses "
        "every verb that sends something; LATCHKEY_READ_ONLY=1 is a floor no call can lift."),
    "latchkey_show_cookies": "limit rows (15 compact, 40 normal); session-looking ones first.",
    "latchkey_sites": "limit hosts (15 compact, 40 normal); host filters by substring.",
    "latchkey_assist": (
        "For a wall a headless browser cannot pass: a `challenged` page (a captcha, Turnstile, "
        "\"press & hold\"), a `blocked` page, or a sign-in the agent must not do. Two steps, so "
        "a browser carrying the user's cookies is never opened without their yes:\n"
        "1. latchkey_assist (no confirm) reads the page and returns `ask_user` - which site, "
        "which wall, and that a window will open. It opens nothing. Relay it to the user.\n"
        "2. On the user's yes, latchkey_assist(confirm=true) opens a headed Chrome on a "
        "throwaway profile, seeded with this site's cookies at this session's own user agent "
        "and window size (so a clearance the user earns is bound to the same client this "
        "session is). The user solves it there. latchkey harvests the cookies the site issued, "
        "carries them into this session, and re-reads the page so you learn if the wall is "
        "gone (`verdict`).\n"
        "The window can outlast one call: `waiting` means the user is still working - call "
        "confirm=true again to keep waiting (no second window opens). cancel=true closes it. "
        "The throwaway profile is shredded when the window closes. Never ask the user for a "
        "password or code in chat; the window is where those happen. Off if the server sets "
        "LATCHKEY_ASSIST=off."),
}

HELP_TOPICS: dict[str, str] = {
    "act": HELP_DETAIL["latchkey_act"],
    "snapshot": HELP_DETAIL["latchkey_snapshot"],
    "wait": HELP_DETAIL["latchkey_wait"],
    "pilot": HELP_DETAIL["latchkey_pilot"],
    "google": (
        "Google cannot be copied out of the user's Chrome as cookies: the account session is "
        "device bound, so a second browser holding a copy signs the user out of both - the "
        "root cause of the sign-outs. So Google, Gmail, YouTube and the known Google-SSO "
        "portals default to mode dedicated: latchkey's OWN long-lived profile, signed in once "
        "by the user in an ordinary Chrome window, which registers its own device key and "
        "renews its own session. The user's Chrome is never copied and there is no path from "
        "latchkey to a sign-out in it.\n"
        "1. Open the Google page. If it reads signed out, latchkey's own profile has not been "
        "signed in yet - the fix is latchkey_login, NOT the user's Chrome.\n"
        "2. Call latchkey_login (it opens a plain Chrome window and returns at once; "
        "latchkey_login (account='school') for a second, named account). Ask the user to sign "
        "in in that window. Never ask them to type a password or code into chat.\n"
        "3. latchkey_login_status confirms when the profile is signed in (up to ~30s after "
        "they finish); then open the page again.\n"
        "Do NOT switch Google to mode clone or inject to 'carry' the Chrome login - that is the "
        "two-browsers-one-session shape that signs the user out. Clone stays available only for "
        "someone who deliberately sets LATCHKEY_GOOGLE_MODE=clone."),
    "verdicts": (
        "verdict is a DOM heuristic, a hint and not a gate; judge by title and text.\n"
        "logged-in: something only a signed-in visitor sees, and cookies for the host. "
        "Proceed.\n"
        "logged-out: a sign-in step - a sign-in button, a password field, a Google sign-in "
        "page, an account chooser whose accounts are signed out, a passkey prompt (`signin` "
        "says which). Follow next_step: for Google the user signs in in their own Chrome and "
        "the session is reopened (never a latchkey sign-in); for other sites, the user signs "
        "in in their own Chrome and you call latchkey_wait_for_login.\n"
        "blocked: an anti-bot wall. Not a login problem: do not ask the user to sign in. If it "
        "will not clear on its own, latchkey_assist can hand the user a window to pass it "
        "(with their yes first).\n"
        "challenged: a wall asking something (Verify you are human); wait a few seconds and "
        "read again. If it stays, latchkey_assist opens a window for the user to solve it.\n"
        "unclear: no evidence either way; carry on and read the page.\n"
        "Never ask the user to type a password or 2FA code into chat."),
    "batch": (
        "calls run in order; each result is {tool, ok, result|error|skipped}. A failed call "
        "skips the rest unless continue_on_error. parallel lets calls for different sessions "
        "overlap (one session's calls keep their order). timeout_ms (default 24000, max "
        "28000): the client gives up at 30000, so calls not started in time come back "
        "skipped with the reason. max_chars (default 40000, 12000 compact) cuts the longest "
        "results in whole strings and counts the cut. Forgiving: a stringified calls list, "
        "one call instead of a list, name for tool, a short or prefixed tool name, args "
        "beside the name, and an act verb as the tool ({\"tool\": \"click\", \"ref\": "
        "\"e7\"}) all work."),
    "budget": (
        "budget 'compact' (per batch or call, or LATCHKEY_COMPACT=1 for the server) roughly "
        "halves every default: snapshot 3000 characters, text 2500, batch reply 12000, eval "
        "2000, 15 cookies or hosts, 25 links, 5 find matches, and replies drop empty fields. "
        "Nothing is cut silently: text says the next offset, a snapshot ends in its outline, "
        "cookie and host lists say how many more, eval reports the full size."),
    "modes": HELP_DETAIL["latchkey_session_open"],
}
TOPIC_ALIASES = {"login": "google", "gmail": "google", "sign-in": "google", "signin": "google",
                 "youtube": "google", "verdict": "verdicts", "actions": "act", "verbs": "act",
                 "read": "snapshot", "waiting": "wait", "compact": "budget", "size": "budget",
                 "session": "modes", "mode": "modes"}

# What `tools/list` advertises: one tool, whose description indexes every name above.
# Thirty-odd schemas of instructions per turn is the largest thing this server asks an
# agent to carry, and an agent that cannot afford the surface does not use the server.
# The individual names stay dispatchable - an allowlist, the viewer's own scripts and
# the stdio client all hold them - so the only thing given up is the per-turn cost.
#
# That trade is not the right one for every model. One tool whose argument is a list of
# nested calls is the cheapest surface to carry and the hardest to emit, and a smaller
# local model that can call a flat tool reliably may not be able to build the batch at
# all. So the human running the server can widen it:
#
#   LATCHKEY_ADVERTISE=batch   one tool (the default, and the cheapest)
#   LATCHKEY_ADVERTISE=core    the handful a task actually uses, flat, plus the batch
#   LATCHKEY_ADVERTISE=all     every name, for a client that would rather see them
#
# Nothing about dispatch changes; only what `tools/list` says exists.
CORE_TOOLS = ("latchkey_open", "latchkey_snapshot", "latchkey_find", "latchkey_act",
              "latchkey_text", "latchkey_wait", "latchkey_session_open", "latchkey_login",
              "latchkey_login_status")
# The tools the batch description lists with their arguments; the rest are listed by name.
INDEX_TOOLS = CORE_TOOLS + (HELP,)

BATCH_SPEC: dict = _batch_spec(CATALOG)
SPEC_OF: dict[str, dict] = {spec["name"]: spec for spec in CATALOG + [BATCH_SPEC]}


def advertised(mode: str | None = None) -> list[dict]:
    """The tool schemas `tools/list` hands out, for the chosen surface."""
    import os
    wanted = (mode or os.environ.get("LATCHKEY_ADVERTISE") or "batch").strip().lower()
    if wanted == "all":
        return [BATCH_SPEC] + list(CATALOG)
    if wanted == "core":
        return [BATCH_SPEC] + [SPEC_OF[name] for name in CORE_TOOLS if name in SPEC_OF] \
            + [HELP_SPEC]
    return [BATCH_SPEC]


TOOLS: list[dict] = advertised()

DISPATCH: dict[str, Callable[..., Any]] = {
    "latchkey_open": tool_open, "latchkey_text": tool_text, "latchkey_snapshot": tool_snapshot,
    "latchkey_wait": tool_wait, "latchkey_find": tool_find,
    "latchkey_act": tool_act, "latchkey_pilot": tool_pilot,
    "latchkey_screenshot": tool_screenshot, "latchkey_eval": tool_eval,
    "latchkey_links": tool_links, "latchkey_frames": tool_frames,
    "latchkey_frame_text": tool_frame_text, "latchkey_sync": tool_sync,
    "latchkey_wait_for_login": tool_wait_for_login,
    "latchkey_login": tool_login, "latchkey_login_status": tool_login_status,
    "latchkey_accounts": tool_accounts,
    "latchkey_assist": tool_assist, "latchkey_assist_status": tool_assist_status,
    "latchkey_save_session": tool_save_session, "latchkey_sessions": tool_sessions,
    "latchkey_forget_session": tool_forget_session, "latchkey_injected": tool_injected,
    "latchkey_pages": tool_pages, "latchkey_new_page": tool_new_page,
    "latchkey_switch": tool_switch, "latchkey_close_page": tool_close_page,
    "latchkey_history": tool_go, "latchkey_sites": tool_sites,
    "latchkey_profiles": tool_profiles, "latchkey_use_profiles": tool_use_profiles,
    "latchkey_use_clone": tool_use_clone, "latchkey_clone_status": tool_clone_status,
    "latchkey_show_cookies": tool_show_cookies,
    "latchkey_credential_sources": tool_credential_sources,
    "latchkey_session_open": tool_session_open,
    "latchkey_session_list": tool_session_list,
    "latchkey_session_close": tool_session_close,
    "latchkey_viewer_start": tool_viewer_start,
    "latchkey_viewer_stop": tool_viewer_stop,
    "latchkey_viewers": tool_viewers,
    "latchkey_close": tool_close,
    BATCH: tool_batch,
    HELP: tool_help,
}


# -- JSON-RPC plumbing -------------------------------------------------------

def _reply(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_reply(out: Any, failed: bool) -> dict:
    """A tool result as MCP carries it: text, and whether the call itself failed."""
    text = out if isinstance(out, str) else json.dumps(out, indent=2, default=str)
    return {"content": [{"type": "text", "text": text[:60_000]}], "isError": failed}


# Errors that already say what happened and what to do about it. A Python traceback
# under one of these is noise an agent pays for and cannot act on; under anything else
# it is the only clue where the fault was.
SPOKEN_ERRORS = (SessionError, ValueError, KeyError, IndexError, RefError,
                 ActionFailed, ReadOnlyError, NavigationRefused, ProfileBusy, LoginPending,
                 RealModeUnavailable)


def _call_tool(name: str, args: dict) -> tuple[Any, bool]:
    resolved = resolve_tool(name) or name
    fn = DISPATCH.get(resolved)
    if fn is None:
        return (f"no tool called {name!r}.{nearest_tool(name)} "
                f"latchkey_help lists all {len(CATALOG)} of them."), True
    name = resolved
    args = normalise_args(name, args)
    if name != BATCH and isinstance(args, dict) and "budget" in args:
        previous = getattr(_CALL, "budget", None)
        _CALL.budget = args.pop("budget")
        try:
            return _call_tool(name, args)
        finally:
            _CALL.budget = previous
    if name == BATCH:
        # A batch that is malformed is the caller's own call shape, so it is told plainly
        # rather than through the traceback that wraps a tool's own failure.
        try:
            out = tool_batch(**args)
        except ValueError as exc:
            return str(exc), True
        except TypeError as exc:
            return f"bad arguments: {exc}", True
        return out, not out["ok"]
    ignored: list[str] = []
    if isinstance(args, dict):
        args, ignored = _drop_unknown(name, fn, args)
    try:
        out = fn(**args)
        if ignored:
            out = _with_note(out, f"ignored {', '.join(repr(k) for k in ignored)}: "
                                  f"{name} takes {_signature(SPEC_OF.get(name) or {}) or 'no arguments'}")
        return out, False
    except TypeError as exc:
        spec = SPEC_OF.get(name) or {}
        wanted = _signature(spec)
        detail = str(exc).split(") ", 1)[-1] if ") " in str(exc) else str(exc)
        return (f"bad arguments for {name}: {detail}."
                + (f" It takes: {wanted} (* is required)." if wanted else "")), True
    except SPOKEN_ERRORS as exc:
        # The message is the whole answer; a traceback under it is a paragraph of
        # tokens pointing at this file rather than at what the agent should do next.
        return f"{type(exc).__name__}: {exc}", True
    except Exception as exc:  # noqa: BLE001
        detail = traceback.format_exc(limit=3)
        return f"{type(exc).__name__}: {exc}\n{detail}", True


# A required argument a model left out that the page it is on already answers.
_FROM_CURRENT_PAGE = {"latchkey_show_cookies": "host", "latchkey_credential_sources": "site",
                      "latchkey_wait_for_login": "url"}


def _drop_unknown(name: str, fn: Callable[..., Any], args: dict) -> tuple[dict, list[str]]:
    """Arguments the tool does not take, dropped - when everything it needs is there.

    A model carrying `mode` from another browser tool over to latchkey_open, or `limit`
    to latchkey_history, knew what it wanted; failing the whole call for a word the tool
    would have ignored cost it a turn every time (deepseek-v4-flash, week of 2026-09-12:
    `mode`, `command`, `topic`, `url` on tools that take none of them). The call runs and
    the result says what was ignored, so the next call is right. A missing REQUIRED
    argument still fails - except the ones the current page answers (`host` for cookies,
    `site` for credential sources, `url` to wait for a login on).
    """
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return args, []
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args, []
    out = dict(args)
    missing_from_page = _FROM_CURRENT_PAGE.get(name)
    if missing_from_page and not out.get(missing_from_page):
        url = _current_url(out.get("session") or DEFAULT_NAME)
        if url:
            out[missing_from_page] = (urlparse(url).hostname or url) if missing_from_page != "url" else url
    required = {k for k, p in params.items()
                if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    unknown = [k for k in out if k not in params]
    if not unknown or not required.issubset(out):
        return out, []
    for key in unknown:
        out.pop(key)
    return out, unknown


def _current_url(session: str) -> str | None:
    """The page a session is on, if it is open - never opening one to find out."""
    try:
        if registry.get_existing(session) is None:
            return None
        return _on_session(session, lambda browser: browser.page.url, timeout=5)
    except Exception:  # noqa: BLE001
        return None


def _with_note(out: Any, note: str) -> Any:
    """A tool result with one more thing the caller should know."""
    if isinstance(out, dict):
        return {**out, "note": f"{out['note']}; {note}" if out.get("note") else note}
    if isinstance(out, str):
        return f"{out}\n({note})"
    if isinstance(out, list):
        return {"result": out, "note": note}
    return out


def handle(request: dict) -> dict | None:
    """Answer one JSON-RPC request in-process.

    Pure and synchronous: the stdio server calls this from a worker pool for a
    `tools/call` that is not a batch, and tests call it directly with a stubbed registry.
    A batch reaches here only when it is empty or malformed; a well-formed one is
    sequenced across the lanes by `BatchFlight`.
    """
    method = request.get("method")
    msg_id = request.get("id")

    if method == "initialize":
        return _reply(msg_id, {"protocolVersion": PROTOCOL_VERSION,
                               "capabilities": {"tools": {}},
                               "serverInfo": SERVER_INFO,
                               "instructions": INSTRUCTIONS})
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _reply(msg_id, {})
    if method == "tools/list":
        # Read per request, not per process: the human can widen the surface without
        # rebuilding anything, and a test can ask for one without a module reload.
        return _reply(msg_id, {"tools": advertised()})
    if method == "tools/call":
        params = request.get("params") or {}
        out, failed = _call_tool(params.get("name"), params.get("arguments") or {})
        return _reply(msg_id, _tool_reply(out, failed))
    if msg_id is None:
        return None
    return _error(msg_id, -32601, f"method not found: {method}")


# Which argument names the session a tool call acts on, so the call can be queued on
# that session's lane in arrival order. None = the call does not touch a page and may
# run straight on the pool. Every dispatchable tool must appear here; a test checks it.
LANE_OF: dict[str, str | None] = {name: "session" for name in DISPATCH}
LANE_OF.update({"latchkey_session_open": "name", "latchkey_session_close": "name"})
for _local in ("latchkey_session_list", "latchkey_close", "latchkey_sites",
               "latchkey_profiles", "latchkey_clone_status", "latchkey_show_cookies",
               "latchkey_credential_sources", "latchkey_sessions",
               "latchkey_forget_session", "latchkey_viewer_start",
               "latchkey_viewer_stop", "latchkey_viewers", "latchkey_login",
               "latchkey_login_status", "latchkey_assist_status", HELP, BATCH):
    LANE_OF[_local] = None
# A batch belongs to no single lane - no session of its own - and help touches nothing.


def lane_of_call(name: str, args: Any) -> str | None:
    """The lane a tool call belongs to: the session it names, or None for no session."""
    if not isinstance(args, dict):
        return None
    arg = LANE_OF.get(name)
    if arg is None:
        return None
    args = normalise_args(name, args)
    named = (args or {}).get(arg)
    return str(named) if named else DEFAULT_NAME


class BatchFlight:
    """One `latchkey_batch` request in flight, sequenced across its sessions' lanes.

    A batch names no session of its own, so it cannot sit on one lane - its calls can,
    and each one's place in its lane is claimed here, from the reader loop, at the
    moment the batch arrives. That is the same place the call would have had if the
    client had sent it on its own, which is what keeps arrival order meaning something
    for a batch too: the call that opens a session is not overtaken by the call that
    uses it, and a call that arrived earlier still runs first.

    Each call waits for the one before it, so the batch runs strictly in order and the
    failure that stops a batch is decided in that same order - a call after the failure
    is skipped, not already in flight. The last call to run sends the reply.
    """

    def __init__(self, server: "StdioServer", request: dict,
                 parsed: list[tuple[str, dict]], args: dict) -> None:
        self.server = server
        self.request = request
        self.parsed = parsed
        self.continue_on_error = bool(args.get("continue_on_error"))
        self.parallel = bool(args.get("parallel"))
        # A batch gives itself a deadline, and it is not the lane's: that guard is 600s
        # and the client is gone at 30s. See CLIENT_WAIT_S.
        self.budget_s = batch_budget(args.get("timeout_ms"))
        self.deadline = time.monotonic() + self.budget_s
        self.budget = args.get("budget") or None
        self.max_chars = _char_budget(_int(args.get("max_chars"), 0)
                                      or BUDGETS[budget_name(self.budget)]["batch"])
        self.entries: list[dict] = [{} for _ in parsed]
        self.gates = [threading.Event() for _ in parsed]
        self.stopped: str | None = None    # the call whose failure ended the batch
        self.done = 0                      # calls that have an entry, however they got one
        self._guard = threading.Lock()

    def start(self) -> None:
        """Queue the batch's calls, in the order the batch lists them."""
        if self.parallel:
            for lane, indexes in self._by_lane().items():
                self.server.dispatch(lane, self._group(indexes))
            return
        for index, (name, args) in enumerate(self.parsed):
            self.server.dispatch(lane_of_call(name, args), self._step(index))

    def _settle(self, count: int = 1) -> None:
        """Record that `count` more calls have an entry, and answer once they all do.

        The reply used to be sent by whichever task held the last *index*, which was the
        last to finish only as long as every call waited on the one before it without a
        bound. It does not any more - a call skipped for want of budget finishes at once,
        possibly while an earlier one is still on the page - so the batch answers when it
        is actually complete instead of when a particular task happens to end.
        """
        with self._guard:
            self.done += count
            last = self.done >= len(self.parsed)
        if last:
            self.finish()

    def _by_lane(self) -> dict[str | None, list[int]]:
        """Call indexes grouped by the lane they belong to, in first-seen order."""
        groups: dict[str | None, list[int]] = {}
        for index, (name, args) in enumerate(self.parsed):
            groups.setdefault(lane_of_call(name, args), []).append(index)
        return groups

    def _step(self, index: int) -> Callable[[], None]:
        """One call of the batch: wait for the call before it, then make this one."""
        def run() -> None:
            previous = (getattr(_CALL, "cancel", None), getattr(_CALL, "budget", None),
                        getattr(_CALL, "deadline", None))
            _CALL.cancel = self._flag()      # so a wait inside the batch can let go too
            _CALL.budget, _CALL.deadline = self.budget, self.deadline
            try:
                self.entries[index] = self._make(index)
            finally:
                _CALL.cancel, _CALL.budget, _CALL.deadline = previous
                self.gates[index].set()
                self._settle()
        return run

    def _group(self, indexes: list[int]) -> Callable[[], None]:
        """A lane's calls of a parallel batch: in order among themselves, overlapping
        the other lanes' groups."""
        def run() -> None:
            previous = (getattr(_CALL, "cancel", None), getattr(_CALL, "budget", None),
                        getattr(_CALL, "deadline", None))
            _CALL.cancel = self._flag()
            _CALL.budget, _CALL.deadline = self.budget, self.deadline
            try:
                for index in indexes:
                    self.entries[index] = self._make(index, ordered=False)
            finally:
                _CALL.cancel, _CALL.budget, _CALL.deadline = previous
                self._settle(len(indexes))
        return run

    def _make(self, index: int, ordered: bool = True) -> dict:
        name, args = self.parsed[index]
        if self._given_up():
            return {"tool": name, "ok": False,
                    "skipped": "the client cancelled this request before this call started"}
        if time.monotonic() >= self.deadline:
            # Out of time, and said per call - reported as skipped, because that is what
            # it is: the client's own timeout is what happens if this keeps working.
            return {"tool": name, "ok": False, "skipped": _out_of_time(self.budget_s)}
        try:
            if ordered and index:
                # Bounded by the batch's own deadline, not unbounded: a call that waits
                # out the budget here has to be skipped rather than started, or it runs
                # past the client's patience and the reply nobody reads.
                self.gates[index - 1].wait(max(0.0, self.deadline - time.monotonic()))
            if ordered and self.stopped is not None:
                return {"tool": name, "ok": False,
                        "skipped": _skipped_because(self.stopped)}
            # Asked again *after* the wait. The check above the wait only says the batch
            # had time when this call was picked up; what decides whether it may start is
            # whether there is time left now.
            if time.monotonic() >= self.deadline:
                return {"tool": name, "ok": False, "skipped": _out_of_time(self.budget_s)}
            if self._given_up():
                return {"tool": name, "ok": False,
                        "skipped": "the client cancelled this request before this call started"}
            entry = _one_call(_call_tool, name, args)
            if not entry["ok"] and not self.continue_on_error:
                self.stopped = name
            return entry
        except Exception as exc:  # noqa: BLE001
            return {"tool": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def finish(self) -> None:
        """The batch's answer, once its last call has run."""
        request_id = self.request.get("id")
        if request_id is not None:
            with self.server._inflight_lock:
                self.server._inflight.pop(request_id, None)
        out = _assemble(self.entries, self.max_chars)
        self.server.send(_reply(self.request.get("id"), _tool_reply(out, not out["ok"])))

    def _flag(self) -> threading.Event | None:
        """This batch's cancellation flag, if the client was given an id to cancel."""
        request_id = self.request.get("id")
        if request_id is None:
            return None
        with self.server._inflight_lock:
            found = self.server._inflight.get(request_id)
        return found[0] if found else None

    def _given_up(self) -> bool:
        """Has the client stopped waiting for this batch? (`notifications/cancelled`)

        Checked before every call, and handed to the calls themselves, so an abandoned
        batch stops at the next call instead of running the rest of itself into a reply
        nobody is reading - which is what keeps a session unresponsive after a timeout.
        """
        flag = self._flag()
        return bool(flag and flag.is_set())


class StdioServer:
    """Reads JSON-RPC lines and answers them, in order per session, concurrently
    across sessions.

    `initialize`, `ping` and `tools/list` are answered inline so the handshake is
    immediate and never queued behind a browser. A `tools/call` that names a session
    goes to that session's lane (first in, first out, so the call that opens a session
    is not overtaken by the call that uses it); anything else goes to the pool.

    A `latchkey_batch` call is not one session's, so it does not go to one lane: each of
    its calls is dispatched to that call's own lane at the moment the batch arrives
    (`BatchFlight`), which is what gives the batch the place in the queue those calls
    would have had on their own.
    """

    INLINE = ("initialize", "initialized", "notifications/initialized", "ping",
              "tools/list")

    def __init__(self, stdin: Any = None, stdout: Any = None, workers: int = 8) -> None:
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="latchkey-rpc")
        # One thread for a batch's calls that name no session: they are ordered among
        # themselves, and a thread of their own means a batch can never be starved of
        # the pool slots it would otherwise need to make progress.
        self._plain = ThreadPoolExecutor(max_workers=1, thread_name_prefix="latchkey-plain")
        self._lanes = Lanes()
        self.responses = 0
        # What the client is still waiting for: request id -> (its cancel flag, its lane).
        # A client that gives up sends notifications/cancelled, and this is what turns that
        # into "stop that call" rather than "keep working on a reply nobody will read".
        self._inflight: dict[Any, tuple[threading.Event, str | None]] = {}
        self._inflight_lock = threading.Lock()

    def _lane_name(self, request: dict) -> str | None:
        """The lane this request belongs to, decided in the reader loop's order."""
        if request.get("method") != "tools/call":
            return None
        params = request.get("params") or {}
        name = params.get("name") or ""
        return lane_of_call(resolve_tool(name) or name, params.get("arguments") or {})

    def _maybe_batch(self, request: dict) -> bool:
        """Take a batch off the ordinary path, if it is one that can be sequenced.

        A batch is fanned out to its calls' lanes here, in the reader's order. An empty
        or malformed one is left to `_serve`, so that whatever is wrong with it is said
        by the one place that validates batches.
        """
        if request.get("method") != "tools/call":
            return False
        params = request.get("params") or {}
        if params.get("name") != BATCH:
            return False
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return False          # `_serve` will say what is wrong with it
        # A batch leaves the reader's hands for as long as it takes, so it is registered
        # the same way a single call is: a client that gives up on this batch must be able
        # to stop the calls it has not started yet, and the one it is already on.
        request_id = request.get("id")
        if request_id is not None:
            with self._inflight_lock:
                self._inflight[request_id] = (threading.Event(), None)
        try:
            parsed = calls_of(args.get("calls"))
        except ValueError:
            parsed = []
        if not parsed:
            # `_serve` will say what is wrong with it, and it registers its own entry -
            # so drop the placeholder rather than leaving one behind for an id that is
            # about to be answered on the ordinary path.
            if request_id is not None:
                with self._inflight_lock:
                    self._inflight.pop(request_id, None)
            return False
        BatchFlight(self, request, parsed, args).start()
        return True

    def _watched(self, request: dict, lane: str | None) -> Callable[[], None]:
        """The call, with the client's ability to abandon it attached while it is in flight."""
        request_id = request.get("id")
        event: threading.Event | None = None
        if request_id is not None:
            event = threading.Event()
            with self._inflight_lock:
                self._inflight[request_id] = (event, lane)

        def run() -> None:
            try:
                if event is not None and event.is_set():
                    return       # the client gave up while this was queued: it must not run
                self._serve(request, event)
            finally:
                if request_id is not None:
                    with self._inflight_lock:
                        self._inflight.pop(request_id, None)
        return run

    def cancel(self, request: dict) -> bool:
        """`notifications/cancelled`: stop that call, and never start it if it has not begun.

        This is the difference between a client that gave up and a server that carries on:
        without it, a batch whose reply was abandoned still runs every call it named, one
        after another, holding the session's lane for as long as that takes - so the session
        stays unresponsive long after the agent stopped listening to it.
        """
        request_id = (request.get("params") or {}).get("requestId")
        with self._inflight_lock:
            found = self._inflight.get(request_id)
        if found is None:
            return False
        found[0].set()
        return True

    def reclaim(self, lane: str) -> int:
        """Cancel whatever is in flight on a lane, so a stuck session can be taken back."""
        with self._inflight_lock:
            events = [event for event, where in self._inflight.values() if where == lane]
        for event in events:
            event.set()
        return len(events)

    def dispatch(self, lane: str | None, task: Callable[[], None]) -> None:
        """Run one call of a batch where that call belongs: its session's lane, or the
        single thread that keeps the calls naming no session in order."""
        if lane is None:
            self._plain.submit(task)
        else:
            self._lanes.for_name(lane).submit(task)

    def send(self, response: dict) -> None:
        line = json.dumps(response) + "\n"
        with self._write_lock:
            self._out.write(line)
            self._out.flush()
            self.responses += 1

    def _serve(self, request: dict, cancel: threading.Event | None = None) -> None:
        _CALL.cancel = cancel
        try:
            response = handle(request)
        except Exception:  # noqa: BLE001
            response = _error(request.get("id"), -32603, "internal error")
        finally:
            _CALL.cancel = None
        if cancel is not None and cancel.is_set() and request.get("id") is not None:
            # It stopped because the client stopped waiting. Saying so is cheap, and it is
            # worth more than silence to a batch that is still being read.
            response = _error(request.get("id"), -32800, "request cancelled by the client")
        if response is not None:
            try:
                self.send(response)
            except (BrokenPipeError, ValueError):
                pass                  # the client hung up; nothing to report to

    def serve_forever(self) -> int:
        from . import reaper as reaper_mod
        reaper = reaper_mod.IdleReaper().start()
        try:
            for line in self._in:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if request.get("method") == "notifications/cancelled":
                    self.cancel(request)
                    continue
                if request.get("method") in self.INLINE:
                    self._serve(request)
                    continue
                if self._maybe_batch(request):
                    continue
                lane = self._lane_name(request)
                task = self._watched(request, lane)
                if lane is None:
                    self._pool.submit(task)
                else:
                    if (request.get("params") or {}).get("name") in LANE_RECLAIMERS:
                        self.reclaim(lane)
                    self._lanes.for_name(lane).submit(task)
        except (KeyboardInterrupt, BrokenPipeError):
            pass                      # a client hanging up is normal, not a crash
        finally:
            reaper.stop()
            # A window still open for a human is closed with the server, and its throwaway
            # profile (which holds a copy of a site's cookies) goes with it.
            intervene.manager.close_all()
            self._lanes.drain()
            # Drained, not cancelled: a client that stops talking is often a script
            # waiting for the replies it already asked for, and a cancelled future is a
            # reply that never comes. The lanes are drained for the same reason.
            self._plain.shutdown(wait=True)
            self._pool.shutdown(wait=True)
            registry.close_all()
        return 0


def main() -> int:
    """Serve MCP on stdio. Set LATCHKEY_VIEWER_PORT to have the viewer listening too.

    The viewer has to live in this process, because this process is the one holding
    the sessions. Off unless asked for: a local server that shows the user's screen is
    something they switch on deliberately.
    """
    import os

    port = (os.environ.get("LATCHKEY_VIEWER_PORT") or "").strip()
    if port and port.lower() not in ("0", "off", "no", "false"):
        try:
            from .viewer import start_viewer
            server = start_viewer(int(port))
            moved = "" if server.port == int(port) else f"  (port {port} was taken)"
            print(f"latchkey viewer on {server.url()}{moved}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"latchkey viewer did not start on port {port}: "
                  f"{type(exc).__name__}: {exc}\n"
                  f"  something is already on that port. Point LATCHKEY_VIEWER_PORT at a "
                  f"free one (or stop whatever is holding it) and restart.",
                  file=sys.stderr, flush=True)
    import signal

    def stop(signum, frame):
        # A client that ends the server with SIGTERM gets the same orderly exit as one that
        # closes stdin: sessions closed, and the Chrome processes they started with them.
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, stop)
    except ValueError:
        pass                      # not the main thread (an embedding); nothing to install
    return StdioServer().serve_forever()


if __name__ == "__main__":
    sys.exit(main())
