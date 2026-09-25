"""Decisive test of the same-machine idea: can a copy-on-write clone of your real Chrome
profile, launched headless with your REAL macOS keychain, sign a DBSC rotation challenge
with the key in your Secure Enclave — and therefore keep Gmail signed in by itself,
without signing your real Chrome out?

It does not read or print any cookie value. It:
  1. clones your Default profile (copy-on-write, instant),
  2. opens Gmail headless with the real keychain, and reports signed-in / signed-out,
  3. DELETES the bound __Secure-*PSIDTS cookies from the clone, then reloads — which forces
     Chrome to run the DBSC refresh (sign a challenge with the per-profile key) or fail,
  4. reports whether fresh PSIDTS cookies came back (proof the clone holds the key) and
     whether Gmail is still signed in,
  5. checks — by cookie NAME and expiry only, nothing decrypted — that your real Default
     profile is still signed in to Google afterwards (no sign-out).

Run it yourself:
    python3 ~/tools/latchkey/examples/probe_gmail_dbsc.py

Green result => the clone can be latchkey's Google mode: your real login, your profile,
no second sign-in, and your Chrome stays signed in.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LATCHKEY_ALLOW_BOUND_COPY"] = "1"   # this probe is the thing that tests the guard

from latchkey import Browser, SessionSpec, google, profile as profile_mod

CLONE_DIR = "/tmp/latchkey-gmail-probe"
DEFAULT_DB = os.path.join(profile_mod.CHROME_ROOT, "Default", "Cookies")
PSIDTS = ("__Secure-1PSIDTS", "__Secure-3PSIDTS", "__Secure-1PSIDRTS", "__Secure-3PSIDRTS")


def real_chrome_google_names():
    """Google account cookie names live in your real Default profile — names+expiry only."""
    return sorted(google.session_cookie_names(DEFAULT_DB))


def cdp_cookies(browser):
    got = browser._cdp.send("Network.getAllCookies")["cookies"]
    return {c["name"] for c in got if c["name"] in PSIDTS
            and c.get("domain", "").endswith("google.com")}


def main():
    before_real = real_chrome_google_names()
    print(f"[0] your real Chrome, Google session cookies present: {before_real or 'NONE'}")
    if not before_real:
        print("    Your real Chrome is not signed in to Google right now — sign in there first.")
        return 2

    spec = SessionSpec(mode="clone", clone_dir=CLONE_DIR, fresh_clone=True,
                       label="gmail-probe", read_only=True)
    b = Browser("mail.google.com", spec=spec).start()
    try:
        st = b.goto("https://mail.google.com/mail/u/0/", settle_ms=8000)
        print(f"[1] clone opened Gmail: verdict={st.verdict!r}  url={st.url}")
        print(f"    title={st.title[:60]!r}")
        if st.verdict != "logged-in":
            print("    -> clone is NOT signed in to Gmail. The copied cookies alone were not "
                  "accepted; see text below.")
            print("   ", (st.text or "")[:200].replace("\n", " "))

        have = cdp_cookies(b)
        print(f"[2] bound PSIDTS cookies in the clone before: {sorted(have) or 'NONE'}")

        # Force a DBSC refresh: remove the bound cookies, then make an authenticated request.
        for name in PSIDTS:
            for dom in (".google.com",):
                try:
                    b._cdp.send("Network.deleteCookies", {"name": name, "domain": dom})
                except Exception:
                    pass
        print("[3] deleted the bound PSIDTS cookies from the clone; reloading Gmail to force "
              "Chrome to re-mint them by signing a DBSC challenge ...")
        b.goto("https://mail.google.com/mail/u/0/", settle_ms=9000)
        time.sleep(3)
        back = cdp_cookies(b)
        st2 = b.state()
        print(f"[4] bound PSIDTS cookies after forced refresh: {sorted(back) or 'NONE'}")
        print(f"    Gmail verdict after refresh: {st2.verdict!r}")

        after_real = real_chrome_google_names()
        print(f"[5] your real Chrome, Google session cookies still present: {after_real or 'NONE'}")

        rotated = bool(back)
        still_in = st2.verdict == "logged-in"
        real_ok = bool(after_real)
        print("\n=== RESULT ===")
        print(f"clone re-minted bound cookies (holds the key): {'YES' if rotated else 'NO'}")
        print(f"clone still signed in to Gmail after refresh:  {'YES' if still_in else 'NO'}")
        print(f"your real Chrome still signed in to Google:    {'YES' if real_ok else 'NO'}")
        if rotated and still_in and real_ok:
            print("VERDICT: PASS — same-machine clone can be latchkey's Google mode.")
            return 0
        print("VERDICT: needs a look — details above.")
        return 1
    finally:
        b.close()


if __name__ == "__main__":
    raise SystemExit(main())
