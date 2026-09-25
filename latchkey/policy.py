"""What a session is allowed to do to the accounts it is signed in to.

latchkey hands an agent a browser that is already logged in as the user. That is the
whole point of it, and it is also the whole risk: the same session that can read an
inbox can send a message from it. So "read" is the default posture and the verbs that
*send* something are listed here, in one place, rather than judged in eight.

A session is what an agent opens per site, so a read-only session is the per-host knob
in practice: `latchkey_session_open(name="canvas-read", read_only=True)`.

Two ways a session becomes read-only, deliberately not symmetric:

  - `read_only=True` on the session - a choice made when the session is opened.
  - `LATCHKEY_READ_ONLY=1` in the server's environment - a floor set by the human. An
    agent cannot lift it from inside, because a policy an agent can switch off is not one.

`hover` and `scroll` are not writes: they move the pointer and send nothing. The click
that follows a hover is refused, which is where it matters.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

# Verbs that can change something on a server, on the user's account, or on this machine.
# click/fill/press/select/check/upload all end in something being submitted somewhere, and
# eval is a raw escape hatch - `fetch(url, {method: 'POST'})` is one line - so a read-only
# session refuses it and points at the read tools instead.
WRITE_VERBS = frozenset({"click", "click_at", "fill", "press", "select", "check",
                         "upload", "eval"})

TRUTHY = ("1", "true", "yes", "on")


def read_only_floor() -> bool:
    """Has the human's environment forced every session to be read-only?"""
    return (os.environ.get("LATCHKEY_READ_ONLY") or "").strip().lower() in TRUTHY


class ReadOnlyError(RuntimeError):
    """A read-only session was asked to change something."""


def refusal(session: str, verb: str) -> ReadOnlyError:
    """The error a read-only session raises, worded so an agent can act on it."""
    return ReadOnlyError(
        f"session {session!r} is read-only, and {verb} can send something as the user. "
        f"Read with latchkey_text, latchkey_links, latchkey_screenshot, latchkey_frames "
        f"or latchkey_frame_text instead. To act, open a writable session: "
        f"latchkey_session_open(name='<a new name>'). If LATCHKEY_READ_ONLY is set in the "
        f"server's environment that is a floor nothing inside the server can lift - only "
        f"the human running it can.")


# -- where a session may go ---------------------------------------------------
#
# `read_only` refuses the verbs that *send* something, which is the right cut for acting
# as the user on a website. It says nothing about where the browser goes, and a browser
# is a perfectly good local file reader: `goto("file:///Users/you/.ssh/id_rsa")` is not a
# write, so nothing above objected, and the page's text came back through `latchkey_text`
# like any other page. The tool is meant to hand an agent the user's *web* session, so
# the schemes it will navigate are the web's.
#
# `about:blank` is allowed because it is where a session starts and what a closed tab
# becomes. `LATCHKEY_ALLOW_SCHEMES=file,chrome` opens specific others for someone who
# genuinely wants them - the point is that it is a decision, not a default.
WEB_SCHEMES = frozenset({"http", "https"})
BLANK = frozenset({"about:blank", "about:srcdoc", ""})


class NavigationRefused(RuntimeError):
    """A session was asked to go somewhere it is not for."""


def extra_schemes() -> frozenset[str]:
    """Schemes the human has opened up, beyond the web's own."""
    raw = (os.environ.get("LATCHKEY_ALLOW_SCHEMES") or "").replace(",", " ").split()
    return frozenset(part.strip().lower().rstrip(":") for part in raw if part.strip())


def check_url(url: str) -> str:
    """The url a session may navigate to, or a refusal that says what to do instead."""
    text = (url or "").strip()
    if text.lower() in BLANK:
        return text
    scheme = urlparse(text).scheme.lower()
    if not scheme:
        # A bare "example.com" is what a person types and what a small model emits;
        # guessing https for it is the same thing every address bar does.
        return f"https://{text}"
    allowed = WEB_SCHEMES | extra_schemes()
    if scheme in allowed:
        return text
    if scheme == "file":
        raise NavigationRefused(
            f"latchkey will not open {scheme}: URLs. This browser carries the user's "
            f"logins, not their filesystem, and a local file read is not something a "
            f"read-only session should be able to do either. Read files with the "
            f"file tools your client already has. LATCHKEY_ALLOW_SCHEMES=file in the "
            f"server's environment opens it, and only the human can set that.")
    raise NavigationRefused(
        f"latchkey navigates http and https; {scheme!r} is not one of them. "
        f"LATCHKEY_ALLOW_SCHEMES={scheme} in the server's environment opens it.")
