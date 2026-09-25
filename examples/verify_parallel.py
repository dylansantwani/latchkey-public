"""Real-site proof that two sessions really do run at once, and stay separate.

Two agents, two Chromes, two cookie jars, the same page each. If the sessions
queued behind one another the wall clock would be the sum of the two loads; if
they overlap, it is the slower one. Read-only: navigation only.

    python3 examples/verify_parallel.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import time

from latchkey import SessionRegistry, SessionSpec

URL = "https://github.com/"

registry = SessionRegistry()
results: dict[str, dict] = {}


def worker(name: str) -> None:
    session = registry.get(name, spec=SessionSpec(host="github.com", label=name))
    started = time.time()
    state = session.submit(lambda browser: browser.goto(URL, settle_ms=2000))
    cookies = session.submit(lambda browser: len(browser._ctx.cookies()))
    results[name] = {"seconds": round(time.time() - started, 2), "verdict": state.verdict,
                     "title": state.title[:30], "marker": state.logged_in_marker,
                     "cookies": cookies,
                     "cookies_in_jar": browser_report(session)["accepted"]}


def browser_report(session) -> dict:
    return session.submit(lambda browser: browser.report.as_dict())


try:
    # Start both Chromes first so the measurement covers the navigation, not the launch.
    names = ("one", "two")
    for name in names:
        registry.get(name, spec=SessionSpec(host="github.com", label=name))

    started = time.time()
    threads = [threading.Thread(target=worker, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    wall = time.time() - started

    total = sum(r["seconds"] for r in results.values())
    print(f"wall clock for both: {wall:.1f}s   sum of the two calls: {total:.1f}s")
    print(f"overlap: {'yes' if wall < total * 0.85 else 'NO - they queued'}\n")
    for name, row in sorted(results.items()):
        print(f"  {name:5} {row['seconds']:>5}s  verdict={row['verdict']!r} "
              f"cookies={row['cookies']}/{row['cookies_in_jar']}  "
              f"marker={row['marker']!r}  title={row['title']!r}")
    print("\nsessions the registry knows about:")
    for row in registry.describe():
        print(f"  {row}")
finally:
    print(f"\nclosed: {registry.close_all()} sessions")
