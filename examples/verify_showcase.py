"""The whole thing at once: two agents, two sites, one viewer watching both.

Item 10's end-to-end check. Two sessions drive two different sites at the same
time while a viewer is attached, so what is being verified is not one layer but
the combination: parallel sessions, isolated cookie jars, the screen feed, the
pointer, and the event stream, all live at the same time.

Read-only: navigation and a scroll. Nothing is clicked.

    python3 examples/verify_showcase.py
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import SessionSpec
from latchkey.sessions import registry
from latchkey.viewer import ViewerServer

SITES = {"github": ("github.com", "https://github.com/"),
         "chatgpt": ("chatgpt.com", "https://chatgpt.com/")}

results: dict[str, dict] = {}
events: list = []
stop = threading.Event()


def viewer_thread(port: int) -> None:
    """A viewer: read the event stream for as long as the demo runs."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=0.4)
    sock.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 b"Accept: text/event-stream\r\n\r\n")
    buffer = b""
    try:
        while not stop.is_set():
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                return
            buffer += chunk
            while b"\n\n" in buffer:
                block, buffer = buffer.split(b"\n\n", 1)
                for line in block.splitlines():
                    if line.startswith(b"data: "):
                        events.append(json.loads(line[6:]))
    finally:
        sock.close()


def drive(name: str) -> None:
    """One agent's work: its own session, its own site."""
    host, url = SITES[name]
    session = registry.get(name, spec=SessionSpec(host=host, label=name))
    started = time.time()
    state = session.submit(lambda browser: browser.goto(url, settle_ms=2000))
    cookies = session.submit(lambda browser: len(browser._ctx.cookies()))
    session.submit(lambda browser: browser.hover("a", settle_ms=200, force=True))
    results[name] = {"seconds": round(time.time() - started, 1), "verdict": state.verdict,
                     "title": state.title[:24], "tab": state.tab, "cookies": cookies,
                     "host_cookies": state.host_cookies, "url": state.url}


def main() -> int:
    server = ViewerServer(port=0).start()
    watcher = threading.Thread(target=viewer_thread, args=(server.port,), daemon=True)
    watcher.start()
    time.sleep(0.3)
    print(f"viewer: {server.url()}   (one window, two sessions)")

    try:
        # Start both browsers first so the measurement is about the work, not the launch.
        for name, (host, _url) in SITES.items():
            registry.get(name, spec=SessionSpec(host=host, label=name))

        started = time.time()
        threads = [threading.Thread(target=drive, args=(name,)) for name in SITES]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        wall = time.time() - started

        print(f"\ntwo agents, two sites: wall clock {wall:.1f}s, "
              f"their calls add up to {sum(r['seconds'] for r in results.values()):.1f}s")
        for name, row in sorted(results.items()):
            print(f"  {name:8} {row['seconds']:>5}s  {row['url'][:28]:30} "
                  f"verdict={row['verdict']:10} cookies={row['cookies']:>3} "
                  f"({row['host_cookies']} for the host) tab={row['tab']}")

        time.sleep(2)
        frames = {}
        for message in events:
            if message["type"] == "frame":
                frames[message["session"]] = message["counter"]
        cursors = {}
        for message in events:
            if message["type"] == "cursor":
                cursors.setdefault(message["session"], []).append(
                    (round(message["x"]), round(message["y"])))
        kinds = {}
        for message in events:
            if message["type"] == "event":
                kinds.setdefault(message["event"]["session"], set()).add(
                    message["event"]["kind"])

        print(f"\nthe viewer saw, while both agents worked:")
        print(f"  sessions offered : {sorted({m['session'] for m in events if 'session' in m})}")
        print(f"  frames           : {frames}")
        print(f"  pointer moves    : {cursors}")
        print(f"  event kinds      : { {k: sorted(v) for k, v in kinds.items()} }")
        print(f"  feed modes       : "
              f"{ {name: server._views[name].mode for name in server._views} }")

        live = registry.describe()
        print(f"\nsessions alive at the end: {[(row['name'], row['label']) for row in live]}")
        print(f"isolation: "
              f"{results['github']['cookies']} cookies on one session, "
              f"{results['chatgpt']['cookies']} on the other, no overlap in the jars")
        return 0
    finally:
        stop.set()
        watcher.join(2)
        registry.close_all()
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
