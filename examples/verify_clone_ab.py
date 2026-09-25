"""Which is it: the fingerprint, or the cookie that fingerprint was issued to?

    python3 examples/verify_clone_ab.py
    python3 examples/verify_clone_ab.py --url https://www.reddit.com/
    python3 examples/verify_clone_ab.py --variants baseline,old-config,no-clearance

Clone mode meets walls that inject mode does not, and there are two live explanations:

1. the clone presents a *different device* than the one the profile's `cf_clearance`
   and `__cf_bm` were issued to - a 1280x820 viewport at dpr 1 where the real Chrome is
   1200x765 at dpr 2 on a 1512x982 screen;
2. the clone arrives *carrying* a clearance that was issued to somebody else, and a
   stale pass is worse than no pass.

The variants separate them:

    baseline      the current build: display-matched, human-shaped input
    old-config    what it looked like before - fixed viewport, dpr 1, no screen
    no-clearance  baseline with Cloudflare's cookies deleted before the first request

If `no-clearance` clears the wall and `baseline` does not, it is the stale cookie (2).
If `baseline` clears it and `old-config` does not, it is the fingerprint (1). If all
three are walled, it is neither - the egress IP or the profile's reputation, which
nothing inside the browser can fix.

It only navigates and reads. Nothing is submitted anywhere.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import fingerprint as fp                     # noqa: E402
from latchkey.session import Browser, SessionSpec          # noqa: E402

VENDOR_COOKIES = ("cf_clearance", "__cf_bm")


def variant_spec(variant: str) -> SessionSpec:
    if variant == "old-config":
        # The launch config this project shipped before: the viewport reported as the
        # screen, dpr 1, no client-hint correction, machine-shaped input.
        return SessionSpec(mode="clone", width=1280, height=820, native_device=False,
                           humanize=False)
    return SessionSpec(mode="clone")


def strip_vendor_cookies(browser: Browser) -> list:
    """Delete only Cloudflare's cookies, so the rest of the session survives."""
    gone = []
    for name in VENDOR_COOKIES:
        try:
            browser._ctx.clear_cookies(name=name)
            gone.append(name)
        except Exception as exc:  # noqa: BLE001
            gone.append(f"{name} ({type(exc).__name__})")
    return gone


def run_variant(variant: str, url: str) -> dict:
    browser = Browser(spec=variant_spec(variant))
    row = {"variant": variant, "fingerprint": "", "verdict": "", "wall": "", "title": ""}
    try:
        browser.start()
        if variant == "no-clearance":
            row["stripped"] = ", ".join(strip_vendor_cookies(browser))
        browser.goto(url, settle_ms=4000)
        seen = fp.read(browser.page)
        plan = browser.fingerprint
        row["fingerprint"] = (f"{seen.get('geom', '?').split('x')[0]}x"
                              f"{seen.get('geom', '?').split('x')[1]}"
                              f"@{seen.get('dpr')}x screen "
                              f"{'x'.join(str(seen.get('screen', '?')).split('x')[:2])}")
        state = browser.state()
        row["verdict"] = state.verdict
        row["wall"] = (state.wall or {}).get("sentence", "")
        row["title"] = (state.title or "")[:44]
        row["plan"] = plan.summary()
    except Exception as exc:  # noqa: BLE001
        row["verdict"] = f"error: {type(exc).__name__}: {str(exc)[:70]}"
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://www.reddit.com/")
    parser.add_argument("--variants", default="baseline,old-config,no-clearance")
    args = parser.parse_args()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]

    print(f"{args.url}  ({fp.display().as_dict() if fp.display() else 'no display reading'})")
    print()
    rows = [run_variant(variant, args.url) for variant in variants]

    width = max(len(r["variant"]) for r in rows) + 2
    print(f"{'variant':<{width}}{'verdict':<14}{'fingerprint':<30}wall")
    print("-" * 110)
    for row in rows:
        print(f"{row['variant']:<{width}}{row['verdict']:<14}{row['fingerprint'][:28]:<30}"
              f"{row['wall'][:46]}")
    print()
    for row in rows:
        print(f"  {row['variant']:<13}{row['title']}")
        if row.get("stripped"):
            print(f"  {'':<13}cookies deleted first: {row['stripped']}")
    print()
    print("Reading it: no-clearance clearing while baseline does not means the profile's")
    print("stale Cloudflare pass was the problem. baseline clearing while old-config does")
    print("not means the device it presented was. All three walled means the egress IP or")
    print("the profile's reputation - nothing inside the browser will fix that one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
