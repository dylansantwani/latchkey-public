"""Signing latchkey's own profile in, once, as a person - a login of its own.

This *is* the Google path, and it is the whole reason the module exists. Google could be
reached by copying the user's own profile, and for a while it was; a copy goes stale, and
two holders of one account session is what Google reads as a replay, which ends the
account's session and signs the user out of their own Chrome. A profile latchkey signs into
itself has neither problem: it registers its own device-bound key and renews its own
session, in its own directory, the way a second phone on the same account does.

`mode="dedicated"` runs on one of those profiles - `~/.latchkey/profile` for the `default`
account, `~/.latchkey/accounts/<name>` for any other, which is how a second Google account
works. Each has to be signed in by the human, once, and this module is how:

  1. `start()` opens an ordinary Chrome window on that profile - no automation flags, no
     debugging port (`chrome.launch_visible`) - at accounts.google.com, and returns at once.
  2. The person signs in there: password, 2-step, passkey, whatever their account asks.
     Nothing they type goes near latchkey or the agent.
  3. `status()` / `wait()` read the profile's own cookie store on disk, by cookie *name*,
     until an account session is there. Chrome commits cookies in batches, so that can
     trail the sign-in by up to half a minute.
  4. The next dedicated session that needs the profile closes the signed-in window itself
     (`release_for_automation`), gracefully, and starts headless on the same directory.

It returns at once because the agent's client waits thirty seconds for a tool call and a
sign-in takes minutes. The CLI (`latchkey login`) is the same four steps with a person at a
terminal, so it waits.

Nothing here reads the user's real Chrome profile, and nothing is ever copied into this
one. That is the point: there is no path from this module to a sign-out in their Chrome.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from . import chrome as chrome_mod
from . import google, paths, policy
from . import profile as profile_mod

DEFAULT_URL = "https://accounts.google.com/"
STATE_FILE = paths.path("login.json")
COMMIT_LAG_S = 30          # Chrome writes cookies to disk in batches about this far apart


class LoginPending(RuntimeError):
    """The sign-in window holds the profile, and the person is not finished yet."""


def profile_path(explicit: str | None = None, account: str | None = None) -> str:
    return profile_mod.dedicated_path(explicit, account)


def cookie_db(path: str) -> str:
    return os.path.join(path, "Default", "Cookies")


def _read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # Created private, not created and then made private: nothing in it is a secret, but a
    # file under ~/.latchkey is not one to leave readable for a moment either.
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def _clear_state() -> None:
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def window_pid(path: str) -> int | None:
    """The pid of the sign-in window latchkey opened on this profile, while it is open."""
    owner = chrome_mod.lock_owner(path)
    state = _read_state()
    if owner and state.get("pid") == owner and state.get("profile") == path:
        return owner
    if owner and not state.get("pid") and state.get("profile") == path \
            and time.time() - float(state.get("started") or 0) < 120:
        # The window was slow to take the profile, so `start` wrote no pid. A lock that
        # appears within two minutes of the launch is that window - and is written down, so
        # a headless session holding the lock later is never mistaken for it.
        _write_state({**state, "pid": owner})
        return owner
    return None


def accounts(path: str) -> list[str]:
    """Emails the profile knows it is signed in with. Best effort, never a token."""
    found: list[str] = []
    try:
        with open(os.path.join(path, "Default", "Preferences"), encoding="utf-8",
                  errors="replace") as fh:
            prefs = json.load(fh)
        for row in prefs.get("account_info") or []:
            email = isinstance(row, dict) and row.get("email")
            if email and email not in found:
                found.append(str(email))
    except (OSError, ValueError, AttributeError):
        pass
    for email in profile_mod.signed_in_accounts(path):
        if email not in found:
            found.append(email)
    return found


def site_cookies(path: str, host: str) -> dict:
    """How many cookies, and session-looking ones, the profile holds for a non-Google site."""
    from . import cookies as ck
    counts = ck.host_counts(host, available={"dedicated": cookie_db(path)}) \
        if os.path.isfile(cookie_db(path)) else {}
    row = counts.get("dedicated") or {}
    return {"host": host, "cookies": row.get("cookies", 0), "auth_like": row.get("auth_like", 0)}


def _foreign_window(pid: int | None) -> dict | None:
    """The owner note for a live window on the profile that is *not* this server's, or None.

    The sign-in window is a real Chrome window a person may be typing into, and the state
    file that names it is one file per machine: whichever server opened a window last is
    written there. Reading that shared note and closing the pid in it is how one model's
    sign-in window disappeared under another model's. Ownership is the note
    `chrome.record_owner` leaves at launch; this is the question, asked once.
    """
    held = chrome_mod.live_owner(pid)
    if held and held.get("server_pid") not in (None, os.getpid()):
        return held
    return None


def status(profile_dir: str | None = None, host: str | None = None,
           account: str | None = None) -> dict:
    """Is latchkey's own profile signed in, and is the sign-in window open? One disk read."""
    path = profile_path(profile_dir, account)
    owner = chrome_mod.lock_owner(path)
    ours = window_pid(path)
    names = google.session_cookie_names(cookie_db(path))
    session_here = bool(owner and not ours and chrome_mod.launched_here(owner))
    out: dict[str, Any] = {"profile": path, "signed_in": bool(names),
                           "window_open": bool(ours),
                           "profile_in_use": bool(owner and not ours and not session_here)}
    if session_here:
        out["session_open"] = True        # this server's own dedicated session holds it
    if ours:
        # Which site the open window was opened for. Without it a model asking about Gmail read
        # an eBay sign-in window (opened by another thread) as "sign in to Google there".
        opened = _read_state().get("url")
        if opened:
            out["window_url"] = opened
        held = _foreign_window(ours)
        if held:
            out["window_owner"] = {"server_pid": held.get("server_pid"),
                                   "session": held.get("session")}
    which = profile_mod.account_of_path(path) or account
    if which:
        out["account"] = which
    emails = accounts(path) if names else []
    if emails:
        out["accounts"] = emails
    if host and not google.is_google_host(host):
        out["site"] = site_cookies(path, google.host_of(host))
    out["next_step"] = next_step(out)
    return out


def list_accounts(include_empty: bool = True) -> list[dict]:
    """Every account latchkey holds a profile for, and whether each one is signed in.

    This is the question an agent asks first - "which logins do I have?" - so it is one
    disk read per account and no browser at all. `default` is always listed even when its
    directory does not exist yet, because "you have none, here is how to make one" is a
    more useful answer than an empty list.
    """
    names = profile_mod.account_names()
    if include_empty and profile_mod.DEFAULT_ACCOUNT not in names:
        names.insert(0, profile_mod.DEFAULT_ACCOUNT)
    out = []
    for name in names:
        path = profile_mod.dedicated_path(None, name)
        exists = os.path.isdir(os.path.join(path, "Default"))
        signed_in = bool(google.session_cookie_names(cookie_db(path))) if exists else False
        row = {"account": name, "profile": path, "exists": exists,
               "google_signed_in": signed_in}
        if signed_in:
            emails = accounts(path)
            if emails:
                row["emails"] = emails
        if not signed_in:
            row["next_step"] = (
                f"Not signed in to Google on the {name!r} account. Run "
                f"`latchkey login --account {name}` (or the latchkey_login tool with "
                f"account={name!r}): it opens one ordinary Chrome window for the user to "
                f"sign in once. Never ask the user for a password or code in chat.")
        out.append(row)
    return out


def next_step(state: dict) -> str:
    """What an agent should do next, in one or two sentences.

    Every call it names is spelled for the account this state is about. "Call latchkey_login"
    is wrong advice on the `school` account - it signs a different profile in, and the page
    that sent the agent here stays signed out afterwards.
    """
    which = state.get("account")
    named = "" if not which or which == profile_mod.DEFAULT_ACCOUNT else f" (account={which!r})"
    login = f"latchkey_login{named}"
    if state.get("signed_in"):
        if state.get("window_open"):
            if state.get("window_owner"):
                return ("Signed in on latchkey's own profile, but the open sign-in window "
                        f"belongs to another latchkey server (pid "
                        f"{state['window_owner'].get('server_pid')}): it closes that window "
                        "when it hands the profile over, and this server will not close or "
                        "adopt it. Ask that server to finish (latchkey_login_status there). "
                        "Never ask the user to type a password or code into chat.")
            return (f"Signed in on latchkey's own profile{named}. A session can use it now "
                    f"(latchkey_session_open mode='dedicated'"
                    + (f", account={which!r}" if named else "")
                    + "); latchkey closes the sign-in window itself.")
        return (f"Signed in on latchkey's own profile{named}. A session can use it now "
                f"(latchkey_session_open mode='dedicated'"
                + (f", account={which!r}" if named else "") + ").")
    if state.get("window_open"):
        if state.get("window_owner"):
            return ("The open sign-in window belongs to another latchkey server (pid "
                    f"{state['window_owner'].get('server_pid')}): the user finishes signing in "
                    "there and that server reports when it is done. Do not call "
                    "latchkey_login here - it would reach for a window that is not this "
                    "server's. Never ask the user to type a password or code into chat.")
        where = google.host_of(state["window_url"]) if state.get("window_url") else ""
        return (f"A sign-in window{' for ' + where if where else ''} is open and not signed in "
                "to Google yet. Ask the user to finish signing in there, then call "
                f"latchkey_login_status again; it can take up to {COMMIT_LAG_S}s after they "
                "finish to show here. Never ask the user for a password or code in chat.")
    if state.get("profile_in_use"):
        return ("latchkey's profile is open in another latchkey server's session, which this "
                "server cannot close. Ask that server to close it (latchkey_session_close "
                f"there), then call {login} here. Never ask the user for a password or "
                "code in chat.")
    if state.get("session_open"):
        return (f"Not signed in. Call {login}: it closes this server's session on latchkey's "
                f"profile, opens a Chrome window for the user to sign in to Google once, and "
                f"returns at once. Never ask the user for a password or code in chat.")
    return (f"Not signed in. Call {login}: it opens a Chrome window for the user to sign in "
            f"to Google once, and returns at once. Never ask the user for a password or code "
            f"in chat.")


def start(url: str = DEFAULT_URL, profile_dir: str | None = None, *, again: bool = False,
          account: str | None = None,
          launcher: Callable[..., Any] = chrome_mod.launch_visible) -> dict:
    """Open the sign-in window on latchkey's own profile, and return without waiting."""
    url = policy.check_url(url or DEFAULT_URL)
    path = profile_mod.dedicated_dir(profile_dir, account)
    current = status(path, host=url)
    if current["window_open"]:
        held = _foreign_window(window_pid(path))
        if held:
            # One window per profile, and this one is not ours. Saying so is the fix for two
            # models reaching for each other's windows: a second server must not adopt,
            # reopen or close a window another server opened for its own user.
            return {**current, "status": "window-already-open", "url": url,
                    "window_owner": {"server_pid": held.get("server_pid"),
                                     "session": held.get("session")},
                    "next_step": (
                        "A sign-in window is already open on latchkey's own profile, opened by "
                        f"another latchkey server (pid {held.get('server_pid')}). That window "
                        "is not this server's to use or close: the user finishes signing in "
                        "*there*, and that server's latchkey_login_status reports when it is "
                        "done. Do not open a second window - Chrome allows one browser per "
                        "profile and would hand it to that one. Never ask the user to type a "
                        "password or code into chat.")}
        return {**current, "status": "window-already-open", "url": url}
    if current["profile_in_use"] or current.get("session_open"):
        owner = chrome_mod.lock_owner(path) or 0
        if current.get("session_open"):
            raise chrome_mod.busy_here(owner, path)
        raise chrome_mod.ProfileBusy(owner, path)
    if current["signed_in"] and google.is_google_host(url) and not again:
        return {**current, "status": "already-signed-in", "url": url,
                "next_step": "Already signed in to Google on latchkey's profile. Open the "
                             "Google page (latchkey_open). Pass again=true only to add or "
                             "switch accounts."}
    pid = launcher(path, url)
    _write_state({"pid": pid, "profile": path, "url": url, "started": time.time(),
                  "server_pid": os.getpid(), "server": paths.owner_id()})
    return {"status": "window-open", "profile": path, "url": url, "pid": pid,
            "signed_in": current["signed_in"], "window_open": True,
            "next_step": ("Tell the user: a Chrome window opened for latchkey's own profile; "
                          "sign in there (password, 2-step and passkey prompts all happen in "
                          "that window). Then call latchkey_login_status until signed_in is "
                          "true. Never ask the user to type a password or code into chat.")}


def wait(profile_dir: str | None = None, timeout_s: float = 20.0, poll_s: float = 2.0,
         cancel: threading.Event | None = None, host: str | None = None,
         sleep: Callable[[float], None] = time.sleep, account: str | None = None) -> dict:
    """`status()`, repeated until the profile is signed in, the window closes, or time is up."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    started = time.monotonic()
    # For Google the store says when it is done. For another site only the person does, by
    # closing the window - a Google session already on the profile says nothing about it.
    google_target = not host or google.is_google_host(host)
    while True:
        state = status(profile_dir, host=host, account=account)
        done = (google_target and state["signed_in"]) or not state["window_open"]
        if done or time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
            state["waited_s"] = round(time.monotonic() - started, 1)
            return state
        slept = 0.0
        while slept < poll_s and time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                break
            sleep(0.25)
            slept += 0.25


def close_window(profile_dir: str | None = None, timeout_s: float = 15.0,
                 terminate: Callable[..., bool] = chrome_mod.terminate,
                 account: str | None = None) -> bool:
    """Quit the sign-in window gracefully, so the cookies it earned are flushed to disk."""
    path = profile_path(profile_dir, account)
    pid = window_pid(path)
    if not pid:
        return True
    held = _foreign_window(pid)
    if held:
        raise chrome_mod.ProfileBusy(pid, path, (
            f"the sign-in window (pid {pid}) on latchkey's own profile is another latchkey "
            f"server's (pid {held.get('server_pid')}), and closing it would end a sign-in in "
            f"a window that is not this server's to touch. Its own server closes it when it "
            f"hands the profile over; until then the user signs in there."))
    closed = terminate(pid, timeout_s)
    if closed:
        _clear_state()
    return closed


def release_for_automation(profile_dir: str | None = None,
                           terminate: Callable[..., bool] = chrome_mod.terminate,
                           account: str | None = None) -> dict | None:
    """Free the profile for a headless session - or say exactly why it cannot be freed yet.

    A signed-in sign-in window is closed (that is the handover: the human is done). One that
    is not signed in yet is the human still working, and is left alone. Any other Chrome on
    the profile is not ours to close.
    """
    path = profile_path(profile_dir, account)
    owner = chrome_mod.lock_owner(path)
    if owner and window_pid(path) != owner and not chrome_mod.launched_here(owner) \
            and chrome_mod.orphaned_automation(owner, path):
        # The headless Chrome of a server that died without closing it: nobody's session.
        if terminate(owner, 10.0):
            return {"reclaimed_orphan": owner}
    if not owner:
        return None
    if window_pid(path) != owner:
        if chrome_mod.launched_here(owner):
            raise chrome_mod.busy_here(owner, path)
        raise chrome_mod.ProfileBusy(owner, path)
    state = _read_state()
    login_host = google.host_of(state.get("url") or DEFAULT_URL)
    held = _foreign_window(owner)
    if held:
        # The window holding the profile is another server's, and a shared state file naming
        # it does not make it ours. That server closes it when it hands the profile over.
        raise chrome_mod.ProfileBusy(owner, path, (
            f"latchkey's profile is held by the sign-in window of another latchkey server "
            f"(pid {held.get('server_pid')}). That server closes it when it is done. This one "
            f"does not close another model's window, and cannot use the profile while it is "
            f"open."))
    if google.is_google_host(login_host) and google.signed_in(cookie_db(path)):
        if not close_window(path, terminate=terminate):
            raise chrome_mod.ProfileBusy(
                owner, path, f"the sign-in window (pid {owner}) is signed in but did not close "
                             f"when asked. Ask the user to quit that Chrome window (Cmd-Q), "
                             f"then try again.")
        return {"closed_login_window": owner}
    if google.is_google_host(login_host):
        raise LoginPending(
            "The Google sign-in window is still open and not signed in yet, and it holds "
            "latchkey's profile. Ask the user to finish signing in there, then call "
            "latchkey_login_status (it can take up to 30s after they finish to show). Never "
            "ask the user for a password or code in chat.")
    raise LoginPending(
        f"The sign-in window for {login_host} is still open and holds latchkey's profile. Ask "
        f"the user to close that window when they have signed in, then try again.")
