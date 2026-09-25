"""Real-browser check of the viewer: screencast frames, the SSE stream, the pointer.

This is the one that matters for the viewer, because everything about it is
timing and Chromium's CDP behaviour, which no fake page can stand in for. It
opens a real session, attaches a viewer, drives one action, and reports what the
viewer actually received.

    python3 examples/verify_viewer.py
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import SessionSpec, bus
from latchkey.sessions import registry
from latchkey.viewer import ViewerServer


def sse_messages(port: int, stop: threading.Event, into: list) -> None:
    """A viewer: read the event stream raw until told to stop."""
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
                        into.append(json.loads(line[6:]))
    finally:
        sock.close()


def http_get(port: int, path: str) -> tuple[int, bytes, dict]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    raw = b""
    while b"\r\n\r\n" not in raw:
        raw += sock.recv(65536)
    head, body = raw.split(b"\r\n\r\n", 1)
    status = int(head.split(b" ")[1])
    headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line)
    length = int(headers.get(b"Content-Length", b"0"))
    while len(body) < length:
        body += sock.recv(65536)
    sock.close()
    return status, body, headers


# The viewer serves the sessions in the shared registry, so a script that wants to be
# watched has to use that one and not a SessionRegistry of its own.
server = ViewerServer(port=0).start()
messages: list = []
stop = threading.Event()

try:
    print(f"viewer on {server.url()} (port {server.port})")
    status, body, _ = http_get(server.port, "/health")
    print(f"health: {status} {json.loads(body)}")

    viewer = threading.Thread(target=sse_messages, args=(server.port, stop, messages),
                              daemon=True)
    viewer.start()
    time.sleep(0.3)

    session = registry.get("github", spec=SessionSpec(host="github.com", label="github"))
    print("session opened; waiting for the viewer to attach and push a frame...")
    deadline = time.time() + 12
    while time.time() < deadline and server.frame("github") is None:
        time.sleep(0.2)
    frame = server.frame("github")
    is_jpeg = bool(frame) and frame[:2] == b"\xff\xd8"
    print(f"first frame: {len(frame) if frame else 0} bytes "
          f"({'jpeg' if is_jpeg else 'NOT a jpeg'})")
    print(f"feed mode: {server._views['github'].mode}  error: {server._views['github'].error}")
    print("cursor attached:",
          session.submit(lambda browser: browser.show_cursor))
    print("cursor node in the page:",
          session.submit(lambda b: b.page.evaluate("() => typeof window.__latchkeyCursor")))

    state = session.submit(lambda browser: browser.goto("https://github.com/"))
    print(f"goto: verdict={state.verdict!r} title={state.title[:20]!r}")
    session.submit(lambda browser: browser.hover("header a", settle_ms=200))

    time.sleep(1.5)
    status, body, headers = http_get(server.port, f"/frame/github?n={server._views['github'].frames.counter}")
    print(f"frame endpoint: {status} {headers.get(b'Content-Type')} {len(body)} bytes")

    frames = [m["counter"] for m in messages if m["type"] == "frame"]
    print(f"frame announcements: {len(frames)} (counters {'...' + str(frames[-3:]) if frames else 'none'})")
    print("pointer messages:",
          [(round(m['x']), round(m['y']), m['click']) for m in messages if m["type"] == "cursor"])
    print("event kinds seen by the viewer:",
          sorted({m["event"]["kind"] for m in messages if m["type"] == "event"}))
    print("sessions messages:", sum(1 for m in messages if m["type"] == "sessions"))

    stop.set()
    viewer.join(2)
    print(f"\nbefore detach: cursor shown = {session.submit(lambda b: b.show_cursor)}")
    server.remove_viewer(next(iter(server._viewers), None))
    time.sleep(1.5)
    print(f"after last viewer left: cursor shown = {session.submit(lambda b: b.show_cursor)}, "
          f"viewers = {server.viewer_count()}")
finally:
    stop.set()
    registry.close_all()
    server.shutdown()
