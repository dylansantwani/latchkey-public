"""Real-site check of the wiring: cookies, events, cursor, probe cost.

Read-only: it navigates, hovers and screenshots. It does not click anything, so
nothing on the site changes. Needs Chrome with a signed-in profile.

    python3 examples/verify_wiring.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

from latchkey import Browser, SessionSpec, bus

events = []
off = bus.subscribe(events.append)

browser = Browser("github.com", spec=SessionSpec(label="verify")).start()
try:
    print("report:", {k: v for k, v in browser.report.as_dict().items()
                      if k in ("loaded", "accepted", "rejected", "partitioned",
                               "host_only", "profiles")})

    # Count what actually reaches the page. Before the detection rewrite, one
    # state() cost four round trips (login hint, marker, password field, block
    # check) and every action calls state().
    reads = {"n": 0}
    real = browser.page.evaluate

    def counting(js, arg=None):
        if isinstance(arg, dict) and "markers" in arg:
            reads["n"] += 1
        return real(js, arg)

    browser.page.evaluate = counting

    t0 = time.time()
    state = browser.goto("https://github.com/", settle_ms=3000)
    after_goto = reads["n"]
    print(f"goto: {state.url} verdict={state.verdict!r} marker={state.logged_in_marker!r} "
          f"title={state.title[:40]!r} text={len(state.text)}ch in {time.time() - t0:.1f}s")
    for _ in range(3):
        browser.state()
    print(f"probe: goto + 3x state() -> {reads['n']} page reads "
          f"(was 4 per state, so 16 for this; the goto's is the only one needed). "
          f"After the first: {reads['n'] - after_goto}")

    browser.page.evaluate = real

    attached = browser.attach_cursor()
    installed = browser.page.evaluate("() => typeof window.__latchkeyCursor")
    print(f"cursor: attach={attached} window.__latchkeyCursor={installed!r} "
          f"(the node itself appears on the first move)")

    browser.hover("header a", settle_ms=200)
    moved = browser.page.evaluate(
        "() => { const d = document.getElementById('__latchkey_cursor');"
        " return d ? d.style.transform : null; }")
    print(f"cursor after a hover: {moved!r}")

    browser.detach_cursor()
    gone = browser.page.evaluate("() => !document.getElementById('__latchkey_cursor')")
    print(f"detach removes the node: {gone}")

    browser.screenshot("/tmp/latchkey-verify.png")
    print("screenshot: /tmp/latchkey-verify.png")

    events.clear()
    browser.hover("header a", settle_ms=200)
    print("\nscreenshot + hover events on the bus:")
    for event in events:
        shown = {k: v for k, v in event.detail.items()
                 if k in ("selector", "x", "y", "verdict", "url", "settle_ms")}
        print(f"  {event.session:8} {event.kind:8} {shown}")
finally:
    off()
    browser.close()
