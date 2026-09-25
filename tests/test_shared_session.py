"""Offline tests for the one login a browser must not hand to another browser.

No browser, no network, no Keychain. These check three claims that are cheap to
state and expensive to get wrong:

  - a site's cookies travel; a live Google *account session* does not, and the
    session report says so instead of quietly logging the browser out,
  - cloning a signed-in profile names the accounts it inherited, because that is
    the one path that can end a session on every device at once,
  - `mode="dedicated"` gets a profile of its own and is refused a profile that is
    inside the real one.

    python3 -m unittest tests.test_shared_session -v
"""
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import latchkey.cookies as ck  # noqa: E402
import latchkey.mcp_server as mcp_server  # noqa: E402
import latchkey.profile as profile_mod  # noqa: E402
from latchkey.session import Browser, SessionSpec  # noqa: E402


def cookie(host: str, name: str, value: str = "v") -> ck.Cookie:
    return ck.Cookie(host=host, name=name, value=value, path="/", secure=True,
                     http_only=False, samesite=1, expires=-1.0, partitioned=False)


def jar():
    """One Google account session, one Google preference, one ordinary site login."""
    return [
        cookie(".google.com", "SID"),
        cookie(".google.com", "__Secure-1PSID"),
        cookie(".google.com", "__Secure-1PSIDTS"),
        cookie(".google.com", "__Secure-1PSIDRTS"),
        cookie(".google.com", "SIDCC"),
        cookie(".google.com", "LSID"),
        cookie(".google.com", "NID"),                 # preferences, not a login
        cookie(".youtube.com", "SAPISID"),
        cookie("gemini.google.com", "__Secure-1PSIDTS"),
        cookie(".github.com", "user_session"),
        cookie(".notgoogle.com", "SID"),              # a different site's SID
    ]


class LiveSessionIsNotStorage(unittest.TestCase):

    def test_google_login_is_held_back_and_site_cookies_travel(self):
        kept, held = ck.split_live_session(jar(), share=False)
        kept_names = {c.name for c in kept}
        held_names = {c.name for c in held}
        self.assertIn("user_session", kept_names)
        self.assertIn("NID", kept_names, "a preference cookie is not a login")
        kept_pairs = {(c.host, c.name) for c in kept}
        held_pairs = {(c.host, c.name) for c in held}
        self.assertNotIn((".google.com", "SID"), kept_pairs)
        self.assertIn((".google.com", "SID"), held_pairs)
        self.assertIn((".notgoogle.com", "SID"), kept_pairs,
                      "another site's SID is just a cookie")
        for name in ("__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-1PSIDRTS",
                     "SIDCC", "LSID", "SAPISID"):
            self.assertIn(name, held_names, name)
        self.assertNotIn("__Secure-1PSIDTS", kept_names)

    def test_a_sid_that_is_not_googles_is_kept(self):
        kept, held = ck.split_live_session([cookie(".notgoogle.com", "SID")], share=False)
        self.assertEqual([c.host for c in kept], [".notgoogle.com"])
        self.assertEqual(held, [])

    def test_subdomains_of_google_count_as_google(self):
        for host in (".youtube.com", "gemini.google.com", ".googleusercontent.com",
                     ".withgoogle.com"):
            kept, held = ck.split_live_session([cookie(host, "SIDCC")], share=False)
            self.assertEqual(kept, [], host)
            self.assertEqual(len(held), 1, host)

    def test_the_human_can_override_it_out_loud(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_SHARE_LIVE_SESSION": "1"}):
            kept, held = ck.split_live_session(jar())
        self.assertEqual(held, [])
        self.assertEqual(len(kept), len(jar()))
        with mock.patch.dict(os.environ, {"LATCHKEY_SHARE_LIVE_SESSION": "0"}):
            self.assertFalse(ck.share_live_session())

    def test_the_session_names_what_it_did_not_carry(self):
        browser = Browser(None, spec=SessionSpec())
        with mock.patch.object(ck, "load_many", return_value=(jar(), {"Default": 11}, {})):
            kept = browser._jar()
        self.assertEqual(len(browser.withheld_live_session), 8)
        self.assertIn(("SID", "google.com"), browser.withheld_live_session)
        self.assertEqual(len(kept), 3)
        browser.report.withheld_live_session = len(browser.withheld_live_session)
        self.assertEqual(browser.report.as_dict()["withheld_live_session"], 8)
        self.assertNotIn("value", json.dumps(browser.report.as_dict()))


class SignedInProfilesAreNamed(unittest.TestCase):

    def _profile(self, tmp, accounts):
        state = {"profile": {"info_cache": {
            name: ({"user_name": email, "gaia_id": gaia} if email else {})
            for name, (email, gaia) in accounts.items()}}}
        with open(os.path.join(tmp, "Local State"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        return tmp

    def test_accounts_are_read_without_their_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._profile(tmp, {"Default": ("dylan@example.com", "1234"),
                                "Profile 2": ("other@example.com", "5678"),
                                "Profile 3": ("", "")})
            self.assertEqual(profile_mod.signed_in_accounts(tmp),
                             ["dylan@example.com", "other@example.com"])

    def test_a_profile_with_no_accounts_reports_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(profile_mod.signed_in_accounts(tmp), [])

    def test_a_missing_or_broken_local_state_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Local State"), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(profile_mod.signed_in_accounts(tmp), [])


class DedicatedProfile(unittest.TestCase):

    def test_it_refuses_to_live_inside_the_real_profile(self):
        with self.assertRaises(ValueError) as caught:
            profile_mod.dedicated_dir(ck.CHROME_ROOT)
        self.assertIn("inside your real Chrome profile", str(caught.exception))
        with self.assertRaises(ValueError):
            profile_mod.dedicated_dir(os.path.join(ck.CHROME_ROOT, "Default"))

    def test_it_creates_its_own_directory_private_to_the_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "profile")
            self.assertEqual(profile_mod.dedicated_dir(path), path)
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)

    def test_the_environment_can_move_it_and_the_spec_carries_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LATCHKEY_PROFILE_DIR": tmp}):
                self.assertEqual(profile_mod.dedicated_dir(), tmp)
            spec = SessionSpec(mode="dedicated", profile_dir=tmp)
            self.assertEqual(spec.mode, "dedicated")
            self.assertEqual(spec.as_dict()["profile_dir"], tmp)


class TheModeIsReachable(unittest.TestCase):

    def test_the_tool_schema_offers_dedicated(self):
        tools = [tool for value in vars(mcp_server).values() if isinstance(value, list)
                 for tool in value
                 if isinstance(tool, dict) and tool.get("name") == "latchkey_session_open"]
        self.assertTrue(tools, "latchkey_session_open is not in the tool list")
        mode = tools[0]["inputSchema"]["properties"]["mode"]
        self.assertIn("dedicated", mode["enum"])

    def test_the_other_modes_still_validate(self):
        for mode in ("inject", "clone", "dedicated"):
            self.assertEqual(SessionSpec(mode=mode).mode, mode)


class RealModeAttachesInsteadOfCopying(unittest.TestCase):
    """Real mode drives the browser the user is in, so it must never close it."""

    def test_the_mode_is_reachable(self):
        self.assertEqual(SessionSpec(mode="real").mode, "real")
        tools = [tool for value in vars(mcp_server).values() if isinstance(value, list)
                 for tool in value
                 if isinstance(tool, dict) and tool.get("name") == "latchkey_session_open"]
        self.assertIn("real", tools[0]["inputSchema"]["properties"]["mode"]["enum"])

    def test_hanging_up_does_not_close_the_browser_or_the_users_tabs(self):
        calls = []

        class Stub:
            def __init__(self, name):
                self.name = name

            def close(self):
                calls.append(self.name)

            def stop(self):
                calls.append(self.name)

        browser = Browser(None, spec=SessionSpec(mode="real"))
        browser._ctx, browser._browser, browser._pw = Stub("ctx"), Stub("browser"), Stub("pw")
        browser.close()
        self.assertEqual(calls, ["pw"], "only the connection is dropped")
        self.assertIsNone(browser._ctx)
        self.assertIsNone(browser._browser)

    def test_a_missing_endpoint_says_how_to_get_one(self):
        browser = Browser(None, spec=SessionSpec(mode="real"))
        message = browser._real_unreachable("http://127.0.0.1:9999", "chrome said: nope")
        self.assertIn("LATCHKEY_CDP", message)
        self.assertIn("quit Chrome", message)
        self.assertIn("chrome said: nope", message, "Chrome's own words are the evidence")

    def test_a_dead_endpoint_is_not_mistaken_for_a_browser(self):
        self.assertFalse(Browser._wait_for_endpoint("http://127.0.0.1:9", timeout_s=0.3))

    def test_the_port_offered_to_chrome_is_actually_free(self):
        port = Browser._free_port()
        self.assertGreater(port, 1024)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))

    def test_the_endpoint_can_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_CDP": "9222"}):
            browser = Browser(None, spec=SessionSpec(mode="real"))
            self.assertEqual(browser._real_endpoint(), "http://127.0.0.1:9222")
            self.assertFalse(browser._real_launched, "an endpoint was given; nothing was started")
        with mock.patch.dict(os.environ, {"LATCHKEY_CDP": "http://127.0.0.1:9223"}):
            self.assertEqual(Browser(None, spec=SessionSpec(mode="real"))._real_endpoint(),
                             "http://127.0.0.1:9223")


if __name__ == "__main__":
    unittest.main()
