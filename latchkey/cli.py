"""Command line for latchkey.

  python3 -m latchkey login                     give latchkey a separate identity, once
  python3 -m latchkey login --status            is it signed in?
  python3 -m latchkey get <url>                 open a page, print what it is and its text
  python3 -m latchkey sites                     hosts in the cookie store
  python3 -m latchkey profiles                  Chrome profiles
  python3 -m latchkey show <host>               one site's cookies (masked)
  python3 -m latchkey shot <url> -o out.png     headless screenshot, logged in
  python3 -m latchkey probe <url>               is this site logged in?
  python3 -m latchkey run <url> --actions a.json
  python3 -m latchkey sync                      re-read the browser's cookies
  python3 -m latchkey wait-login <url>          wait for you to log in, then transfer
  python3 -m latchkey assist <url>              open a window to pass a captcha/block, carry it back
  python3 -m latchkey sessions                  saved sessions
  python3 -m latchkey serve                     MCP server on stdio (for agents)
  python3 -m latchkey watch                     viewer server: watch the screen and the

Add --headful to any browsing command to watch it; default is headless.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import cookies as ck
from . import google
from . import profile as profile_mod
from . import store
from .session import Browser, PageState, SessionSpec


def _profiles(args):
    """Profile spec from --profile (a name, or 'all')."""
    try:
        return ck.resolve_profiles(getattr(args, "profile", None))
    except (KeyError, FileNotFoundError) as exc:
        sys.exit(str(exc))


def _spec(args) -> SessionSpec:
    """The session every browsing command runs in."""
    return SessionSpec(mode=args.mode, profiles=_profiles(args),
                       account=getattr(args, "account", None),
                       label=getattr(args, "label", "") or "cli",
                       show_cursor=getattr(args, "cursor", False),
                       width=getattr(args, "width", 1280),
                       height=getattr(args, "height", 820))


def _host(args) -> str | None:
    return getattr(args, "host", None)


def _show(state: PageState) -> None:
    print(json.dumps(state.as_dict(), indent=2)[:4000])


# -- inspection ---------------------------------------------------------------

def cmd_accounts(args) -> int:
    """List latchkey's own logins: which exist, which are signed in, and as whom."""
    from . import login as login_mod

    rows = login_mod.list_accounts()
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0
    width = max((len(r["account"]) for r in rows), default=7)
    for row in rows:
        if row["google_signed_in"]:
            who = ", ".join(row.get("emails") or []) or "signed in"
            mark = f"signed in  {who}"
        elif row["exists"]:
            mark = "not signed in to Google"
        else:
            mark = "no profile yet"
        print(f"  {row['account']:<{width}}  {mark}")
    missing = [r["account"] for r in rows if not r["google_signed_in"]]
    if missing:
        print(f"\nSign one in once (an ordinary Chrome window, nothing typed in chat):"
              f"\n  latchkey login --account {missing[0]}")
    print("\nUse one with:  latchkey --account <name> get <url>")
    return 0


def cmd_sites(args) -> int:
    found, counts, errors = ck.load_many(_profiles(args), args.host)
    for name, n in counts.items():
        print(f"  profile {name}: {n} cookies")
    for name, err in errors.items():
        print(f"  profile {name}: ERROR {err}")
    hosts: dict[str, int] = {}
    for c in found:
        hosts[c.host] = hosts.get(c.host, 0) + 1
    print(f"\n{len(found)} cookies across {len(hosts)} hosts (merged)\n")
    for host, n in sorted(hosts.items(), key=lambda kv: -kv[1])[: args.limit]:
        print(f"  {n:>4}  {host}")
    return 0


def cmd_profiles(args) -> int:
    found = ck.profiles()
    if not found:
        print("no Chrome profiles found")
        return 1
    host = getattr(args, "host", None)
    counts = ck.host_counts(host) if host else {}
    rows = []
    for name, path in found.items():
        if host:
            row = counts.get(name, {})
            rows.append((row.get("cookies") or 0, row.get("auth_like") or 0, name, path, row))
            continue
        try:
            n = len(ck.load(None, path))
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:<12} unreadable: {str(exc)[:60]}")
            continue
        print(f"  {name:<12} {n:>5} cookies  {path}")
    if host:
        rows.sort(key=lambda r: (-r[1], -r[0]))
        print(f"cookies for {host} per profile (best first):")
        for cookies, auth_like, name, path, row in rows:
            error = f"  unreadable: {row['error'][:50]}" if row.get("error") else ""
            auth = f"{auth_like} session-looking" if auth_like else ""
            print(f"  {name:<12} {cookies:>5} cookies  {auth:<20} {path}{error}")
        best = next((r for r in rows if r[0]), None)
        if best:
            print(f"\n{best[2]} holds the most for {host}. Use it: "
                  f"--profile '{best[2]}' (or 'all').")
        else:
            print(f"\nno profile holds any cookie for {host} - the user is not signed in "
                  f"in Chrome at all")
        return 0
    print("\nPass '--profile all' to read every profile, merging them with the "
          "later ones winning.")
    print("Pass '--host example.com' to see which profile holds that site's cookies.")
    return 0


def cmd_show(args) -> int:
    found, _, _ = ck.load_many(_profiles(args), args.host)
    if not found:
        print(f"no cookies for {args.host!r}")
        return 1
    print(f"{len(found)} cookies for *{args.host}*  (values masked)\n")
    for c in found:
        marks = [m for m, on in (("AUTH", ck.authish(c.name)),
                                 ("partitioned", c.partitioned),
                                 ("session-only", c.is_session),
                                 ("httpOnly", c.http_only)) if on]
        flag = ("  [" + ", ".join(marks) + "]") if marks else ""
        print(f"  {c.host:<22} {c.name:<34} {ck.redact(c.value, 3)}{flag}")
    return 0


def cmd_sessions(args) -> int:
    rows = store.describe()
    if not rows:
        print(f"no saved sessions in {store.SESSION_DIR}")
        return 0
    for row in rows:
        print(f"  {row['site']:<24} {row['cookies']:>4} cookies "
              f"({row['partitioned']} partitioned), "
              f"localStorage origins={row['local_storage_origins']}, "
              f"saved {row['saved_at']}")
    return 0


# -- browsing -----------------------------------------------------------------

def cmd_probe(args) -> int:
    with Browser(_host(args), spec=_spec(args), headless=not args.headful) as browser:
        state = browser.goto(args.url, args.settle)
        r = browser.report
        print(f"url     : {state.url}")
        print(f"title   : {state.title}")
        print(f"cookies : {r.accepted}/{r.loaded} accepted, {r.partitioned} partitioned, "
              f"{r.rejected} rejected, {r.dropped_non_ascii} dropped")
        detail = ""
        if state.login_prompt:
            detail += f"  (sign-in CTA {state.login_prompt!r})"
        if state.logged_in_marker:
            detail += f"  (marker {state.logged_in_marker!r})"
        print(f"verdict : {state.verdict}{detail}")
        if state.verdict == "logged-out":
            print(f"next    : {_next_step(args.url, browser.spec.mode, browser.spec.account)}")
        print(f"text    : {state.text[:240]!r}")
    return 0


def _next_step(url: str, mode: str, account: str | None = None) -> str:
    """What a person at a terminal does about a page that is not signed in.

    The command it prints has to be the one that works *here* - on this account. A message
    that says `latchkey login` while the session is running on `--account school` sends the
    reader to sign the wrong profile in, and the page stays signed out afterwards.
    """
    which = f" --account {account}" if account and account != profile_mod.DEFAULT_ACCOUNT else ""
    if google.is_google_host(url):
        if mode == "inject":
            return ("An inject copy cannot carry a Google login. Open it without --mode: "
                    f"Google runs on latchkey's own profile, signed in once with "
                    f"`latchkey login{which}`.")
        if mode in ("clone", "real"):
            return ("This session borrows your Chrome's Google login, which latchkey cannot "
                    "renew for it - a borrowed session goes stale and can sign your Chrome "
                    f"out. Open it without --mode and sign latchkey in once: "
                    f"`latchkey login{which}`.")
        return f"sign latchkey's own profile in to Google once: latchkey login{which}"
    if mode == "dedicated":
        return f"sign in on latchkey's own profile: latchkey login{which} {url}"
    return f"sign in to it in your own Chrome, then run: latchkey wait-login {url}"


def cmd_get(args) -> int:
    """Open a page as you and print what it is: the verdict, why, and the first of its text."""
    with Browser(_host(args), spec=_spec(args), headless=not args.headful) as browser:
        state = browser.goto(args.url, args.settle)
        out = state.as_dict()
        out["mode"] = browser.spec.mode
        notes = browser.take_notes()
        if notes:
            out["note"] = " ".join(notes)
        if state.verdict == "logged-out":
            out["next_step"] = _next_step(state.url or args.url, browser.spec.mode,
                                           browser.spec.account)
        text = state.text or ""
        out["text"] = text[:args.chars]
        if len(text) > args.chars:
            out["more"] = f"{len(text) - args.chars} more characters (--chars to see more)"
        print(json.dumps(out, indent=2))
    return 0


def cmd_login(args) -> int:
    """Sign latchkey's own Chrome profile in, once, in an ordinary window you use yourself."""
    import os
    import time

    from . import login as login_mod
    from .chrome import ProfileBusy
    from .policy import NavigationRefused

    # A person is sitting at this terminal waiting to sign in, so this window should come to
    # the front - unlike an agent-opened window, which now stays in the background by default.
    os.environ.setdefault("LATCHKEY_WINDOW_FOREGROUND", "1")

    if args.status:
        print(json.dumps(login_mod.status(args.profile_dir, host=args.url,
                                          account=args.account), indent=2))
        return 0
    wants_google = google.is_google_host(args.url)
    try:
        started = login_mod.start(args.url, args.profile_dir, again=args.again,
                                  account=args.account)
    except (ProfileBusy, NavigationRefused, ValueError) as exc:
        print(f"latchkey login: {exc}")
        return 1
    path = started["profile"]
    if started["status"] == "already-signed-in":
        who = ", ".join(started.get("accounts") or [])
        print(f"latchkey's profile is already signed in to Google{f' as {who}' if who else ''}."
              f"\n  ({path}; pass --again to add or switch accounts)")
        return _verify(args, path) if args.verify else 0
    if started["status"] == "window-already-open":
        print(f"The sign-in window is already open on {path}. Sign in there; waiting for it.")
    else:
        print(f"A Chrome window opened on latchkey's own profile ({path}).\n"
              f"Sign in there - password, 2-step verification and passkeys all happen in that "
              f"window, and nothing you type reaches latchkey.\n"
              f"Waiting up to {args.timeout}s. Chrome writes cookies to disk about every "
              f"{login_mod.COMMIT_LAG_S}s, so a finished sign-in can take that long to show.",
              flush=True)
    deadline = time.monotonic() + args.timeout
    state = login_mod.status(path, host=args.url)
    try:
        while time.monotonic() < deadline:
            state = login_mod.wait(path, timeout_s=min(5.0, max(0.0, deadline - time.monotonic())),
                                   host=args.url)
            if (wants_google and state["signed_in"]) or not state["window_open"]:
                break
    except KeyboardInterrupt:
        print("\nStopped waiting. The window is still open: finish there, then run "
              "`latchkey login --status`.")
        return 1
    if not state["window_open"]:
        state = login_mod.status(path, host=args.url)     # Chrome flushed its cookies on the way out
    if not wants_google:
        site = state.get("site") or {}
        print(f"The window {'is still open' if state['window_open'] else 'was closed'}. "
              f"latchkey's profile holds {site.get('cookies', 0)} cookies for "
              f"{site.get('host', args.url)} ({site.get('auth_like', 0)} session-looking).")
        return 0
    if not state["signed_in"]:
        print("Not signed in yet. " + ("The window is still open: finish there, then run "
                                       "`latchkey login --status`." if state["window_open"]
                                       else "The window was closed before a Google session "
                                            "was saved; run `latchkey login` again."))
        return 1
    who = ", ".join(state.get("accounts") or [])
    print(f"Signed in to Google{f' as {who}' if who else ''}, on latchkey's own profile.")
    if state["window_open"] and not args.keep_open:
        from .chrome import ProfileBusy
        try:
            closed = login_mod.close_window(path)
        except ProfileBusy as busy:
            print(str(busy))
            return 1
        print("Closed the sign-in window." if closed else
              "The sign-in window did not close when asked; quit it yourself (Cmd-Q).")
    elif state["window_open"]:
        print("Leaving the window open (--keep-open). Close it before an agent uses the "
              "profile, or latchkey closes it then.")
        return 0
    name = args.account or profile_mod.DEFAULT_ACCOUNT
    print(f"Done. Every session on the {name!r} account now uses this login - Google, Gmail "
          f"and YouTube included - and renews it in this profile alone. Your own Chrome is "
          f"never copied and never signed out."
          + (f"\n  Use it with: latchkey --account {name} get <url>"
             if name != profile_mod.DEFAULT_ACCOUNT else ""))
    return _verify(args, path) if args.verify else 0


def _verify(args, path: str) -> int:
    """Open the signed-in profile headless, the way an agent will, and say what Google says."""
    url = "https://myaccount.google.com/"
    print(f"Checking it headless: {url} ...", flush=True)
    spec = SessionSpec(mode="dedicated", profile_dir=path, label="login-check")
    try:
        with Browser(None, spec=spec) as browser:
            state = browser.goto(url, 4000)
            print(f"  verdict {state.verdict}: {state.title!r}"
                  + (f" ({state.signin})" if state.signin else ""))
            return 0 if state.verdict in ("logged-in", "unclear") and not state.signin else 1
    except Exception as exc:  # noqa: BLE001 - a check that fails should say how, not trace
        print(f"  could not check: {type(exc).__name__}: {str(exc)[:300]}")
        return 1


def cmd_shot(args) -> int:
    with Browser(_host(args), spec=_spec(args), headless=not args.headful) as browser:
        browser.goto(args.url, args.settle)
        browser.screenshot(args.out, args.full_page)
        print(json.dumps(browser.report.as_dict(), indent=2))
        print(f"screenshot -> {args.out}")
    return 0


def cmd_run(args) -> int:
    actions = json.loads(open(args.actions).read()) if args.actions else []
    with Browser(_host(args), spec=_spec(args), headless=not args.headful) as browser:
        _show(browser.run(args.url, actions, args.settle))
    return 0


def cmd_sync(args) -> int:
    with Browser(None, spec=_spec(args), headless=not args.headful) as browser:
        print(json.dumps(browser.refresh(), indent=2))
    return 0


def cmd_wait_login(args) -> int:
    with Browser(_host(args), spec=_spec(args), headless=not args.headful) as browser:
        print(f"Open {args.url} in your real browser and sign in. "
              f"Waiting up to {args.timeout}s...", flush=True)
        result = browser.wait_for_login(args.url, timeout_s=args.timeout)
        print(json.dumps(result, indent=2)[:3000])
        return 0 if result.get("status") in ("logged-in", "already-logged-in") else 1


def cmd_assist(args) -> int:
    """Open a window carrying a site's cookies so you can pass a wall the headless run cannot."""
    import time

    from . import intervene

    with Browser(_host(args), spec=_spec(args), headless=not args.headful) as browser:
        snap = browser.assist_snapshot(args.url)
        verdict = snap["verdict"]
        if verdict not in ("challenged", "blocked", "logged-out") and not args.force:
            print(f"{snap['url']} reads {verdict!r}; nothing to solve. "
                  f"Pass --force to open a window anyway.")
            return 0
        wall = (snap.get("wall") or {}).get("sentence") or ""
        print(f"{snap['url']} : {verdict}{f' ({wall})' if wall else ''}.\n"
              f"Opening a Chrome window carrying this site's cookies at this run's own "
              f"identity - solve the check there, then close the window (or it closes itself "
              f"when the wall clears). Waiting up to {args.timeout}s.", flush=True)
        handoff = intervene.manager.start("cli", snapshot=snap, url=snap["url"],
                                          timeout_s=args.timeout)
        try:
            while handoff.running and not handoff.wait(1.0):
                pass
        except KeyboardInterrupt:
            intervene.manager.cancel("cli")
            print("\nStopped; closing the window.")
            return 1
        if handoff.state != intervene.SOLVED:
            detail = f" ({handoff.error})" if handoff.error else ""
            print(f"The window closed without clearing the wall: {handoff.state}{detail}.")
            return 1
        applied = browser.assist_apply(handoff.harvested, handoff.url)
        state = applied["state"]
        print(f"Carried {applied['accepted']} of {applied['offered']} earned cookies back. "
              f"The page now reads {state['verdict']!r}.")
        return 0 if state["verdict"] not in ("challenged", "blocked") else 1


def cmd_clone(args) -> int:
    from . import profile as profile_mod
    if args.discard:
        print(json.dumps({"discarded": profile_mod.discard()}, indent=2))
    elif args.status:
        print(json.dumps(profile_mod.describe(), indent=2))
    else:
        print(json.dumps(profile_mod.clone(force=args.force), indent=2))
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import main as serve
    return serve()


def cmd_watch(args) -> int:
    from .viewer import main as watch
    return watch(["--port", str(args.port)])


# -- parser --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="latchkey", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=("auto", "inject", "clone", "dedicated", "real"),
                   default="auto",
                   help="auto (default): latchkey's own signed-in profile for Google, "
                        "Gmail and YouTube, inject for everything else. "
                        "inject: copy cookies into a fresh browser. "
                        "clone: open a copy-on-write clone of your real profile, which "
                        "also brings localStorage, IndexedDB, service workers and every "
                        "profile, because Chrome opens the actual directory. "
                        "dedicated: open latchkey's own long-lived profile for the "
                        "chosen --account, which signs in once (`latchkey login`) and then "
                        "owns its own session. This is what Google, Gmail and YouTube use, "
                        "so your own Chrome is never copied and never signed out. "
                        "real: attach to a Chrome that has a debugging endpoint "
                        "(LATCHKEY_CDP); Chrome 136 and later refuse one for your everyday "
                        "profile, so this fails fast there")
    p.add_argument("--profile", help="Chrome profile name, or 'all' for every profile"
                                      " (default: Default). This is one of *your* Chrome "
                                      "profiles, whose cookies inject mode reads.")
    p.add_argument("--account", help="which of latchkey's own logins to use (default: "
                                     "'default'). Each account is a profile of its own, "
                                     "signed in once with `latchkey login --account NAME`; "
                                     "this is how a second Google account works.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def browsing(name: str, help_text: str):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("url")
        s.add_argument("--host", help="cookie host filter (default: every cookie)")
        s.add_argument("--settle", type=int, default=6000, help="ms to wait after load")
        s.add_argument("--headful", action="store_true", help="show a real window")
        s.add_argument("--cursor", action="store_true",
                       help="draw the agent's pointer on the page")
        s.add_argument("--label", help="name this session in the event stream")
        return s

    s = sub.add_parser("sites", help="list hosts in the cookie store")
    s.add_argument("host", nargs="?", help="substring filter")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_sites)

    s = sub.add_parser("accounts", help="list latchkey's own logins and their sign-in state")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(func=cmd_accounts)

    s = sub.add_parser("profiles", help="list Chrome profiles")
    s.add_argument("--host", help="also show which profile holds that site's cookies")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("show", help="inspect one site's cookies (masked)")
    s.add_argument("host")
    s.set_defaults(func=cmd_show)

    sub.add_parser("sessions", help="list saved sessions").set_defaults(func=cmd_sessions)

    s = browsing("probe", "report whether a URL looks logged in")
    s.set_defaults(func=cmd_probe)

    s = browsing("get", "open a page and print its verdict, why, and its text")
    s.add_argument("--chars", type=int, default=2000, help="how much of the text to print")
    s.set_defaults(func=cmd_get)

    s = sub.add_parser("login", help="sign a separate identity in to Google (or a site), "
                                      "once, on latchkey's own profile")
    s.add_argument("url", nargs="?", default="https://accounts.google.com/",
                   help="where to sign in (default https://accounts.google.com/)")
    s.add_argument("--status", action="store_true", help="only say whether it is signed in")
    s.add_argument("--again", action="store_true",
                   help="open the window even if already signed in (add or switch accounts)")
    s.add_argument("--timeout", type=int, default=900, help="seconds to wait for you")
    s.add_argument("--keep-open", action="store_true",
                   help="leave the window open after the sign-in is seen")
    s.add_argument("--no-verify", dest="verify", action="store_false",
                   help="skip the headless check afterwards")
    s.add_argument("--profile-dir", help="latchkey's profile by path (default "
                                         "LATCHKEY_PROFILE_DIR or ~/.latchkey/profile); "
                                         "--account names one instead")
    # Also accept --account *after* the subcommand: every bit of guidance (this tool's own
    # help, and the agent-facing next_step prose) says `latchkey login --account NAME`, and
    # argparse otherwise only takes the global one before the subcommand. SUPPRESS means an
    # absent flag here leaves the global value untouched instead of clobbering it to None.
    s.add_argument("--account", default=argparse.SUPPRESS,
                   help="which of latchkey's own logins to sign in (same as the global "
                        "--account; accepted before or after `login`)")
    s.set_defaults(func=cmd_login)

    s = browsing("shot", "headless screenshot, logged in")
    s.add_argument("-o", "--out", default="/tmp/latchkey.png")
    s.add_argument("--full-page", action="store_true")
    s.add_argument("--width", type=int, default=1280)
    s.add_argument("--height", type=int, default=820)
    s.set_defaults(func=cmd_shot)

    s = browsing("run", "navigate then run actions")
    s.add_argument("--actions", help="JSON file holding a list of action objects")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("sync", help="re-read the browser's cookies and inject changes")
    s.add_argument("--headful", action="store_true")
    s.add_argument("--cursor", action="store_true")
    s.add_argument("--label")
    s.set_defaults(func=cmd_sync)

    s = browsing("wait-login", "wait for you to log in, then transfer the session")
    s.add_argument("--timeout", type=int, default=300, help="seconds to wait")
    s.set_defaults(func=cmd_wait_login)

    s = browsing("assist", "open a window to pass a captcha/block/sign-in, then carry the "
                           "earned cookies back")
    s.add_argument("--timeout", type=int, default=300, help="seconds to keep the window")
    s.add_argument("--force", action="store_true",
                   help="open the window even if the page does not look walled")
    s.set_defaults(func=cmd_assist)

    sub.add_parser("serve", help="MCP server on stdio, for agents").set_defaults(func=cmd_serve)
    s = sub.add_parser("watch", help="viewer server: watch sessions from this process")
    s.add_argument("--port", type=int, default=8788)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("clone", help="copy-on-write clone of your real profile")
    s.add_argument("--force", action="store_true", help="re-clone even if fresh")
    s.add_argument("--status", action="store_true", help="show what the clone holds")
    s.add_argument("--discard", action="store_true", help="delete the clone")
    s.set_defaults(func=cmd_clone)
    return p


def main(argv: list[str] | None = None) -> int:
    import os

    from .chrome import ProfileBusy
    from .login import LoginPending
    from .policy import NavigationRefused, ReadOnlyError
    from .session import RealModeUnavailable

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ProfileBusy, LoginPending, NavigationRefused, ReadOnlyError,
            RealModeUnavailable) as exc:
        # These already say what happened and what to do; a traceback under them is noise.
        if (os.environ.get("LATCHKEY_DEBUG") or "").strip():
            raise
        print(f"latchkey {args.cmd}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
