"""A real browser: read a page by snapshot, act on it by ref, and show the ref failing safely.

    python3 examples/verify_snapshot.py

Prints what an agent would see. The walker is injected JavaScript, so this is the only
place it is really exercised - the test suite covers what its answer goes through.
"""
import re
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from latchkey import SessionSpec                                    # noqa: E402
from latchkey.sessions import registry                              # noqa: E402
import latchkey.mcp_server as m                                     # noqa: E402

FORM = ("data:text/html,<html><body><main><h1>Sign in</h1>"
        "<form aria-label='Sign in form'>"
        "<label for='e'>Email</label><input id='e' type='email' placeholder='you@example.com'>"
        "<label for='p'>Password</label><input id='p' type='password' required>"
        "<input id='r' type='checkbox'><label for='r'>Remember me</label>"
        "<button id='go' type='button'>Continue</button>"
        "<a href='https://example.com/help'>Need help?</a>"
        "</form>"
        "<nav><a href='#a'>One</a><a href='#b'>Two</a></nav>"
        "<p>Hidden things: <span style='display:none'>not me</span></p>"
        "<iframe src='about:blank'></iframe>"
        "</main></body></html>")


def call(tool: str, **args):
    value, failed = m._call_tool(tool, args)
    if failed:
        raise SystemExit(f"{tool} failed: {value}")
    return value


def ref_in(text: str, role: str, name: str) -> str:
    """The ref of a thing by what it says - which is the whole point of a snapshot."""
    found = re.search(rf'{role} "{re.escape(name)}"[^\n]*\[ref=(\w+)\]', text)
    if not found:
        raise SystemExit(f"no {role} {name!r} in:\n{text}")
    return found.group(1)


def main() -> None:
    registry.get("snap", spec=SessionSpec(host="example.com", label="snap"))
    try:
        call("latchkey_open", url=FORM, session="snap")
        reading = call("latchkey_snapshot", session="snap")
        print(f"--- interactive\n{reading['snapshot']}\n")

        outline = call("latchkey_snapshot", mode="outline", session="snap")
        print(f"--- outline\n{outline['snapshot']}\n")

        form = call("latchkey_snapshot", mode="interactive", selector="form", session="snap")
        print(f"--- scoped to the form\n{form['snapshot']}\n")

        email = ref_in(reading["snapshot"], "textbox", "Email")
        remember = ref_in(reading["snapshot"], "checkbox", "Remember me")
        call("latchkey_act", session="snap", actions=[
            {"do": "fill", "ref": email, "value": "someone@example.com"},
            {"do": "check", "ref": remember, "state": True}])
        print(f"filled {email} and checked {remember}\n")

        diff = call("latchkey_snapshot", mode="diff", session="snap")
        print(f"--- diff (the cheap read after an action)\n{diff['snapshot']}\n")

        registry.get("snap").submit(lambda browser: browser.goto("https://example.com/"))
        time.sleep(0.5)
        stale, failed = m._call_tool("latchkey_act", {"session": "snap",
                                                     "actions": [{"do": "click", "ref": email}]})
        print(f"--- the same ref on another page\nfailed={failed}\n{stale}\n")

        tiny = call("latchkey_snapshot", mode="full", max_chars=400, session="snap")
        print(f"--- a 400 character budget\n{tiny['snapshot']}")
    finally:
        registry.close_all()


if __name__ == "__main__":
    main()
