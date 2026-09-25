"""Multi-site check through the MCP path - the surface agents actually use.

For each site: open a session pinned to that host, navigate, read the verdict, and
close it. Read-only: nothing is clicked and nothing on the site changes. One
session per site proves the isolation too, since each has its own cookie jar.

    python3 examples/verify_mcp_path.py            # the default list
    python3 examples/verify_mcp_path.py github.com amazon.com
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from latchkey import mcp_server

SITES = {
    "github.com": "https://github.com/",
    "chatgpt.com": "https://chatgpt.com/",
    "claude.ai": "https://claude.ai/",
    "youtube.com": "https://www.youtube.com/",
    "amazon.com": "https://www.amazon.com/",
    "canvas.uw.edu": "https://canvas.uw.edu/",
}


def tool(tool_name: str, **args):
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": tool_name, "arguments": args}})
    result = response["result"]
    text = result["content"][0]["text"]
    if result["isError"]:
        raise RuntimeError(text.splitlines()[0])
    return json.loads(text) if text[:1] in "[{" else text


def main(argv: list[str]) -> int:
    hosts = argv[1:] or list(SITES)
    print(f"{'host':16} {'verdict':11} {'cookies':>8}  title / text")
    print("-" * 78)
    failures = 0
    for host in hosts:
        url = SITES.get(host, f"https://{host}/")
        try:
            tool("latchkey_session_open", name=host, host=host, label=host)
            state = tool("latchkey_open", url=url, session=host)
            report = tool("latchkey_injected", session=host)
            text = " ".join(state["text"].split())[:46]
            print(f"{host:16} {state['verdict']:11} "
                  f"{report['accepted']:>4}/{report['loaded']:<3}  "
                  f"{state['title'][:26]!r} | {text!r}")
            if state["verdict"] != "logged-in":
                failures += 1
                print(f"{'':16} -> marker={state['logged_in_marker']!r} "
                      f"prompt={state['login_prompt']!r} blocked={state['blocked']!r}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{host:16} FAILED      {type(exc).__name__}: {exc}")
        finally:
            tool("latchkey_session_close", name=host)

    print("\nsessions after cleanup:", tool("latchkey_session_list"))
    print(f"not signed in on {failures} of {len(hosts)} sites")
    return 0 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
