"""Where the mouse is, in the units the viewer draws in.

    python3 examples/verify_pointer.py

The frame the viewer shows is a *picture* of the page: the screencast is pushed at 1100 px
wide by default while the page is 1280 CSS px of viewport. A viewer that scales the pointer
by the picture's pixels therefore draws it - and this is the whole bug behind "I can't see
where the mouse is" - somewhere else entirely. This measures the gap on a real frame, at the
point the agent actually clicked.
"""
import json
import sys
import threading
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from latchkey import SessionSpec                                  # noqa: E402
from latchkey.sessions import registry                            # noqa: E402
from latchkey.viewer import ViewerServer                          # noqa: E402
import latchkey.mcp_server as m                                   # noqa: E402

PORT = 0        # any free port: the machine may have ideas about 8899
TARGET = ("data:text/html,<body style='margin:0'>"
          "<div id=far style='position:absolute;left:820px;top:520px;width:300px;height:180px;"
          "background:rgb(34,170,102)'>far corner</div>"
          "<div id=near style='position:absolute;left:60px;top:40px;width:120px;height:60px;"
          "background:rgb(34,102,204)'>near corner</div></body>")


def jpeg_size(data: bytes) -> tuple[int, int]:
    """The frame's own pixel size, out of its header - no decoder needed for two numbers."""
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD,
                      0xCE, 0xCF):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
    return 0, 0


def main() -> None:
    server = ViewerServer(port=PORT).start()
    print(f"viewer on {server.url()}", flush=True)
    seen: list[dict] = []

    def watch() -> None:
        stream = urllib.request.urlopen(f"{server.url()}/events", timeout=120)
        while True:
            line = stream.readline()
            if not line:
                return
            if line.startswith(b"data: "):
                message = json.loads(line[6:])
                if message.get("type") == "cursor":
                    seen.append(message)

    registry.get("point", spec=SessionSpec(host="example.com", label="point", width=1280,
                                          height=820))
    try:
        threading.Thread(target=watch, daemon=True).start()
        deadline = time.time() + 5
        while time.time() < deadline and not server.viewer_count():
            time.sleep(0.1)
        m._call_tool("latchkey_open", {"url": TARGET, "session": "point"})
        time.sleep(1.5)                      # a frame to be looking at
        for which in ("near", "far"):
            m._call_tool("latchkey_act", {"session": "point",
                                          "actions": [{"do": "hover", "selector": f"#{which}"}]})
        time.sleep(1.0)

        frame = server.frame("point") or b""
        frame_w, frame_h = jpeg_size(frame)
        tip = seen[-1] if seen else {}
        print(f"\nframe as pushed: {frame_w}x{frame_h} · viewport (from the event): "
              f"{tip.get('vw')}x{tip.get('vh')}")
        for message in seen[-2:]:
            vw, vh = message.get("vw"), message.get("vh")
            print(f"\ncursor event: x={message['x']} y={message['y']} vw={vw} vh={vh}")
            if not (frame_w and vw):
                continue
            stage = 900.0                     # roughly what the panel's stage is
            correct = stage / vw              # the page's own pixels
            old = stage / frame_w             # the picture's pixels - the old sum
            print(f"  drawn at {message['x'] * correct:7.1f} px from the left (page units)")
            print(f"  old sum drew it {message['x'] * old:7.1f} px from the left (picture units)")
            print(f"  that is {abs(message['x'] * old - message['x'] * correct):.1f} px out at "
                  f"x={message['x']}, and {abs((vw - message['x']) * (old - correct)):.1f} px at "
                  f"the far edge")
    finally:
        registry.close_all()
        server.shutdown()


if __name__ == "__main__":
    main()
