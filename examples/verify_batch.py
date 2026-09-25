"""The batch surface, over real stdio, against real sites.

Nothing here uses the in-process path: it speaks JSON-RPC to `python3 -m latchkey serve`
exactly as a client does, so the advertised surface, the lanes a batch's calls take, and
the ordering they keep are all exercised the way an agent exercises them. Read-only -
nothing is clicked and nothing on the site changes.

    python3 examples/verify_batch.py                  # github.com
    python3 examples/verify_batch.py github.com amazon.com
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SITES = {
    "github.com": "https://github.com/",
    "chatgpt.com": "https://chatgpt.com/",
    "claude.ai": "https://claude.ai/",
    "youtube.com": "https://www.youtube.com/",
    "amazon.com": "https://www.amazon.com/",
}


class Client:
    """JSON-RPC over the real stdio transport, reading replies as they come."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "latchkey", "serve"], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.next_id = 1
        self.replies: dict[int, dict] = {}
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for line in self.process.stdout:
            line = line.strip()
            if line and '"id"' in line:
                message = json.loads(line)
                if isinstance(message.get("id"), int):
                    self.replies[message["id"]] = message

    def call(self, method: str, params: dict | None = None, timeout: float = 300.0) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self.process.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method,
             "params": params or {}}) + "\n")
        self.process.stdin.flush()
        deadline = time.time() + timeout
        while request_id not in self.replies:
            if time.time() > deadline:
                raise TimeoutError(f"{method} did not answer within {timeout:.0f}s")
            time.sleep(0.02)
        return self.replies[request_id]

    def tools_call(self, name: str, arguments: dict) -> dict:
        """One `tools/call` result: content, and whether the call itself failed."""
        return self.call("tools/call", {"name": name, "arguments": arguments})["result"]

    def tool(self, name: str, **args):
        result = self.tools_call(name, args)
        text = result["content"][0]["text"]
        if result["isError"]:
            raise RuntimeError(text.splitlines()[0])
        return json.loads(text)

    def batch(self, calls: list[dict], **options) -> dict:
        """The batch's own answer, which a failed call inside it does not replace."""
        result = self.tools_call("latchkey_batch", {"calls": calls, **options})
        return json.loads(result["content"][0]["text"])

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=60)


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"{'ok  ' if ok else 'FAIL'}  {label}{' - ' + detail if detail else ''}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    hosts = argv[1:] or ["github.com"]
    client = Client()
    failures = 0
    try:
        advertised = client.call("tools/list")["result"]["tools"]
        size = len(json.dumps({"tools": advertised}))
        failures += check("one tool advertised", [t["name"] for t in advertised]
                          == ["latchkey_batch"], f"{size} bytes of schema per turn")
        index = advertised[0]["description"]
        failures += check("its description indexes the catalog",
                          all(f"latchkey_{name}" in index for name in
                              ("open", "act", "text", "session_open", "wait_for_login")))

        for host in hosts:
            url = SITES.get(host, f"https://{host}/")
            out = client.batch([
                {"tool": "latchkey_session_open",
                 "args": {"name": host, "host": host, "label": host}},
                {"tool": "latchkey_open", "args": {"url": url, "session": host}},
                {"tool": "latchkey_injected", "args": {"session": host}},
                {"tool": "latchkey_text", "args": {"session": host}},
            ])
            ran = [row["ok"] for row in out["results"]]
            state = out["results"][1].get("result", {})
            report = out["results"][2].get("result", {})
            failures += check(f"{host}: four calls in one request, in order",
                              ran == [True, True, True, True] and out["ok"],
                              f"verdict={state.get('verdict')} "
                              f"cookies={report.get('accepted')}/{report.get('loaded')} "
                              f"title={state.get('title', '')[:32]!r}")
            if state.get("verdict") != "logged-in":
                failures += check(f"{host}: signed in", False,
                                  f"marker={state.get('logged_in_marker')!r} "
                                  f"prompt={state.get('login_prompt')!r}")
            client.batch([{"tool": "latchkey_session_close", "args": {"name": host}}])

        # The claim worth checking on real hardware: a session opened inside a batch, and
        # driven by the next call of that same batch, is open by the time it is used.
        out = client.batch([
            {"tool": "latchkey_session_open",
             "args": {"name": "batch-open", "host": "example.com", "label": "batch"}},
            {"tool": "latchkey_open",
             "args": {"url": "https://example.com/", "session": "batch-open"}},
            {"tool": "latchkey_session_close", "args": {"name": "batch-open"}},
        ])
        failures += check("open a session and use it, in one batch",
                          out["ok"] and all(row["ok"] for row in out["results"]),
                          str([row["tool"] for row in out["results"]]))

        # A failure has to be visible, and has to stop what came after it.
        failed_call = client.tools_call("latchkey_batch", {"calls": [
            {"tool": "latchkey_text", "args": {"session": "no-such-session"}},
            {"tool": "latchkey_session_list", "args": {}}]})
        out = json.loads(failed_call["content"][0]["text"])
        failures += check("a failed call stops the batch and says so",
                          failed_call["isError"] and out["failed"] == 1
                          and out["skipped"] == 1
                          and "not run" in out["results"][1]["skipped"],
                          f"isError={failed_call['isError']} failed={out['failed']} "
                          f"skipped={out['skipped']}")

        out = client.batch([{"tool": "latchkey_session_list", "args": {}}])
        failures += check("sessions after cleanup", out["ok"],
                          str([row.get("name") for row in out["results"][0]["result"]]))
    finally:
        client.close()

    print(f"\n{len(hosts)} site(s) checked, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
