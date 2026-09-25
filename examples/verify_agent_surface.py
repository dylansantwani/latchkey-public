"""What an agent gets back: small replies, one-call waits, and a page it can search.

    python3 examples/verify_agent_surface.py

Prints the reply shapes (and their sizes) for a real flow on a real page: read by snapshot,
find one thing, act on it by ref, wait for the result, and get told about a dialog that
answered itself. Sizes are the point - a reply that carries the page's text is a reply an
agent cannot afford to make twenty times.
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from latchkey import SessionSpec                                  # noqa: E402
from latchkey.sessions import registry                            # noqa: E402
import latchkey.mcp_server as m                                   # noqa: E402

PAGE = ("data:text/html,<body style='font:16px -apple-system,sans-serif;margin:0'>"
        "<h1>Sign in</h1>"
        "<form aria-label='Sign in form'>"
        "<label for=email>Email</label>"
        "<input id=email type=email placeholder='you@example.com'>"
        "<label for=pw>Password</label><input id=pw type=password>"
        "<button id=go type=button onclick=\"document.getElementById('out').textContent="
        "'welcome back'; alert('Signing you in')\">Continue</button>"
        "</form>"
        "<p id=out>nothing yet</p>"
        "<p>" + "filler text that an agent should never have to read. " * 40 + "</p>"
        "</body>")


def call(tool: str, **args):
    value, failed = m._call_tool(tool, args)
    if failed:
        raise SystemExit(f"{tool} failed: {value}")
    return value


def size(value) -> int:
    return len(json.dumps(value))


def main() -> None:
    registry.get("agent", spec=SessionSpec(host="example.com", label="agent"))
    try:
        opened = call("latchkey_open", url=PAGE, session="agent")
        print(f"open            {size(opened):5d} chars  {sorted(opened)}")
        assert "text" not in opened, "an action's reply must not carry the page's text"

        read = call("latchkey_snapshot", session="agent")
        print(f"snapshot        {size(read):5d} chars\n{read['snapshot']}\n")

        found = call("latchkey_find", query="continue", session="agent")
        print(f"find 'continue' {size(found):5d} chars  {json.dumps(found['matches'])}")
        ref = found["matches"][0]["ref"]

        email = call("latchkey_find", query="email", session="agent")["matches"][0]["ref"]
        acted = call("latchkey_act", session="agent",
                     actions=[{"do": "fill", "ref": email, "value": "me@example.com"},
                              {"do": "click", "ref": ref}])
        print(f"act by ref      {size(acted):5d} chars  {json.dumps(acted)}")
        print(f"  ...the same act with the page's text in it would be "
              f"{size({**acted, 'text': 'x' * acted.get('text_chars', 0)})} chars")

        waited = call("latchkey_wait", until="text", value="welcome back", timeout_ms=4000,
                      session="agent")
        print(f"wait for text   {size(waited):5d} chars  {json.dumps(waited)}")

        missed = call("latchkey_wait", until="text", value="never appears here",
                      timeout_ms=700, session="agent")
        print(f"wait (timeout)  {size(missed):5d} chars  {json.dumps(missed)}")

        text = call("latchkey_text", limit=200, session="agent")
        print(f"text (asked)    {size(text):5d} chars  text={text.get('text', '')[:60]!r}")
    finally:
        registry.close_all()


if __name__ == "__main__":
    main()
