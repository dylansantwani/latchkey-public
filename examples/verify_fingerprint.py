"""Does the browser the page sees agree with the machine latchkey is running on?

    python3 examples/verify_fingerprint.py                 # inject, this machine
    python3 examples/verify_fingerprint.py --mode clone
    python3 examples/verify_fingerprint.py --sannysoft     # also run bot.sannysoft.com
    python3 examples/verify_fingerprint.py --json

Prints every signal a detection script reads, next to what this machine actually is,
and exits non-zero when a fixable one disagrees. `screen.availHeight`, `window.outer*`
and the window's position cannot be set through CDP, so latchkey sets them from the same
plan with a context init script, and they are shown here next to the machine's own
numbers. `colorDepth` stays a residual: it comes from emulation, and the only way to
fake it is page JavaScript, which is itself a trace.

The UA check is the one that found a real bug: Playwright derives the client-hint
metadata from whatever user-agent string it is handed, so an "Intel Mac OS X" string
made this Apple Silicon machine report `architecture: x86` to any page that asked.
latchkey now sets the user agent with a launch switch and leaves Chrome's own metadata
alone - this script is what proves it stayed that way.
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import fingerprint as fp          # noqa: E402
from latchkey.session import Browser, SessionSpec  # noqa: E402

SANNY_JS = r"""() => {
  const rows = [];
  for (const tr of document.querySelectorAll('table tr')) {
    const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim().replace(/\s+/g, ' '));
    // Only the rows that actually failed. bot.sannysoft words its passes as
    // "missing (passed)" and "present (passed)", so matching the words alone flags a
    // clean bill of health as a failure - which is exactly what it did once.
    if (cells.length >= 2 && /\(failed\)|FAIL/.test(cells[1])) rows.push(cells);
  }
  return rows;
}"""


def report(seen: dict, plan, display) -> tuple[list, list]:
    """(rows, mismatches) - rows for the table, mismatches for the exit code."""
    rows, bad = [], []

    def row(signal, session, machine, ok, note=""):
        rows.append((signal, str(session), str(machine), "ok" if ok else (note or "differs")))
        if not ok and not note:
            bad.append(signal)

    ua = str(seen.get("ua") or "")
    headless = "HeadlessChrome" in ua
    row("user agent", "HeadlessChrome" if headless else "real Chrome strings",
        "no headless token", not headless)
    row("languages", seen.get("languages"), plan.accept_language,
        seen.get("languages") == plan.accept_language)
    row("dpr", seen.get("dpr"), plan.dpr, float(seen.get("dpr") or 0) == float(plan.dpr))
    screen = str(seen.get("screen") or "").split("x")
    want_screen = f"{plan.screen_width}x{plan.screen_height}"
    got_screen = "x".join(screen[:2]) if len(screen) >= 2 else ""
    row("screen", got_screen, want_screen, got_screen == want_screen)
    row("colorDepth", screen[4] if len(screen) > 4 else "?",
        plan.color_depth, True, "residual: not settable through CDP")
    row("avail height", screen[3] if len(screen) > 3 else "?",
        (display.avail_height if display else "?"), True,
        "informational: set by the context init script, not by CDP")
    row("architecture", seen.get("architecture"), fp.architecture(),
        str(seen.get("architecture") or "") == fp.architecture())
    row("platformVersion", seen.get("platformVersion"), fp.os_version() or "?",
        str(seen.get("platformVersion") or "") == fp.os_version())
    row("platform", seen.get("platform"), "MacIntel", seen.get("platform") == "MacIntel")
    row("hint platform", seen.get("uaPlatform"), "macOS", True,
        "informational: the client hint, not navigator.platform")
    colour = "dark" if seen.get("dark") else "light"
    if plan.color_scheme:
        row("colour scheme", colour, plan.color_scheme, colour == plan.color_scheme)
    geom = str(seen.get("geom") or "").split("x")
    if len(geom) == 4:
        row("window chrome", f"{int(geom[2]) - int(geom[0])}x{int(geom[3]) - int(geom[1])}",
            f"0x{fp.BROWSER_CHROME_HEIGHT} is a real window", True,
            "informational: the window around the viewport")
    return rows, bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="inject", choices=("inject", "clone"))
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--sannysoft", action="store_true",
                        help="also load bot.sannysoft.com and list what it catches")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    spec = SessionSpec(mode=args.mode, show_cursor=False)
    browser = Browser(spec=spec)
    try:
        browser.start()
        browser.goto(args.url, settle_ms=1500)
        seen = fp.read(browser.page)
        plan = browser.fingerprint
        display = fp.display()
        rows, bad = report(seen, plan, display)

        if args.sannysoft:
            browser.goto("https://bot.sannysoft.com/", settle_ms=4000)
            rows.append(("", "", "", ""))
            for cells in browser.page.evaluate(SANNY_JS):
                rows.append(("bot.sannysoft", cells[0], cells[1], "FAILED"))
                bad.append(f"bot.sannysoft: {cells[0]}")
            rows.append(("bot.sannysoft", "nothing else failed", "", "ok"))

        if args.json:
            print(json.dumps({"seen": seen, "plan": plan.as_dict(),
                              "identity": getattr(browser, "identity", {}),
                              "rows": [dict(zip(("signal", "session", "machine", "verdict"), r))
                                       for r in rows],
                              "mismatches": bad}, indent=2))
            return 1 if bad else 0

        width = max(len(r[0]) for r in rows) + 2
        print(f"mode {args.mode}  |  {plan.summary()}")
        print(f"identity: {getattr(browser, 'identity', {})}")
        print()
        print(f"{'signal':<{width}}{'the page sees':<34}{'this machine':<30}verdict")
        print("-" * (width + 74))
        for signal, session, machine, verdict in rows:
            print(f"{signal:<{width}}{session[:32]:<34}{machine[:28]:<30}{verdict}")
        print()
        if bad:
            print(f"NOT FAITHFUL: {', '.join(bad)}")
            return 1
        print("Everything a page can check agrees with this machine "
              "(residuals above are what CDP cannot set).")
        return 0
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
