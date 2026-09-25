"""Offline tests for a device bound session, and which modes may carry it.

Chrome registers a *device bound session* for some origins (Google among them): the
session cookies are refreshed by signing a challenge with a per-profile private key in the
OS keystore, which is not in the profile directory.

Two copies fail differently, and only one fails:

  * `inject` sets decrypted cookies into a fresh profile with no registration and a mock
    keychain - it holds neither the binding nor the key, so it can present the cookies once
    and never renew them, and its first rotation retires the value the real Chrome holds.
    That copy is refused.
  * `clone` copies the whole profile (registration included) and runs on the *real*
    keychain, so on the same machine it resolves the same key and rotates the session
    itself. That copy is not refused; it is the mode these hosts use. (Measured: a clone
    re-minted its bound cookies five hours after it was made, the real Chrome unaffected.)

These check that latchkey can *see* the registration, refuses only the inject copy, and
lets clone / real / dedicated through.

    python3 -m unittest tests.test_device_bound -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import latchkey.profile as profile_mod  # noqa: E402
from latchkey.session import Browser, SessionSpec  # noqa: E402


def make_bound_profile(root: str, profile: str = "Default", keys=("https://google.com",)) -> str:
    """A Chrome profile directory holding a device bound session registration."""
    directory = os.path.join(root, profile)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, profile_mod.DBSC_FILE)
    con = sqlite3.connect(path)
    con.execute("create table dbsc_session_tbl (key text, proto blob, primary key(key))")
    con.executemany("insert into dbsc_session_tbl (key, proto) values (?, ?)",
                    [(key, b"\x0a\x02hi") for key in keys])
    con.commit()
    con.close()
    return path


class ABoundSessionIsVisible(unittest.TestCase):

    def test_a_registration_is_read_per_profile(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_profile(root, "Default", ["https://google.com"])
            make_bound_profile(root, "Profile 2", ["https://example.com"])
            found = profile_mod.bound_sessions(root)
            self.assertEqual(found["Default"], ["https://google.com"])
            self.assertEqual(found["Profile 2"], ["https://example.com"])
            self.assertEqual(profile_mod.bound_hosts(root), {"google.com", "example.com"})

    def test_a_profile_without_the_store_is_not_evidence_of_anything(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "Default"))
            self.assertEqual(profile_mod.bound_sessions(root), {})
            self.assertEqual(profile_mod.bound_hosts(root), set())

    def test_a_locked_or_broken_store_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            path = make_bound_profile(root)
            with open(path, "wb") as handle:
                handle.write(b"not a database")
            self.assertEqual(profile_mod.bound_hosts(root), set())


class OnlyTheInjectCopyIsRefused(unittest.TestCase):

    def _browser(self, host: str, mode: str = "inject"):
        return Browser(None, spec=SessionSpec(mode=mode, host=host))

    def test_an_inject_bound_host_is_refused_in_words_that_offer_clone_first(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_profile(root)
            with mock.patch.object(profile_mod, "bound_hosts",
                                   lambda *a, **k: {"google.com"}):
                refusal = self._browser("mail.google.com")._device_bound_refusal()
        self.assertIn("device bound", refusal)
        self.assertIn("inject", refusal)
        self.assertIn("mode='clone'", refusal)      # the fix that works, offered first
        self.assertIn("mode='real'", refusal)
        self.assertIn("mode='dedicated'", refusal)
        self.assertIn("LATCHKEY_ALLOW_BOUND_COPY", refusal)

    def test_a_clone_is_never_refused_a_bound_host(self):
        # clone carries the registration and runs on the real keychain: it rotates the
        # session itself, so there is nothing to refuse.
        with mock.patch.object(profile_mod, "bound_hosts", lambda *a, **k: {"google.com"}):
            self.assertEqual(self._browser("mail.google.com", mode="clone")
                             ._device_bound_refusal(), "")
            self.assertEqual(self._browser("google.com", mode="clone")
                             ._device_bound_refusal(), "")

    def test_an_unbound_host_is_not_refused(self):
        with mock.patch.object(profile_mod, "bound_hosts", lambda *a, **k: {"google.com"}):
            self.assertEqual(self._browser("amazon.com")._device_bound_refusal(), "")

    def test_the_overlap_is_on_host_boundaries_not_substrings(self):
        with mock.patch.object(profile_mod, "bound_hosts", lambda *a, **k: {"google.com"}):
            self.assertEqual(self._browser("notgoogle.com")._device_bound_refusal(), "")
            self.assertNotEqual(self._browser("google.com")._device_bound_refusal(), "")
            self.assertNotEqual(self._browser("accounts.google.com")._device_bound_refusal(), "")

    def test_no_host_pinned_means_nothing_to_check_yet(self):
        with mock.patch.object(profile_mod, "bound_hosts", lambda *a, **k: {"google.com"}):
            self.assertEqual(self._browser("")._device_bound_refusal(), "")

    def test_the_escape_hatch_lets_inject_copy_anyway(self):
        with mock.patch.object(profile_mod, "bound_hosts", lambda *a, **k: {"google.com"}):
            with mock.patch.dict(os.environ, {"LATCHKEY_ALLOW_BOUND_COPY": "1"}):
                self.assertEqual(self._browser("google.com")._device_bound_refusal(), "")


class TheCheckHappensAtTheNavigationNotOnlyAtStartup(unittest.TestCase):
    """A session that pinned no host pinned nothing to check against, so an inject session
    opened with no host then navigated to a bound origin used to walk past the start-up
    refusal. The check is asked again at the navigation."""

    def browser(self, root, mode="inject", host=None):
        browser = Browser.__new__(Browser)
        browser.spec = SessionSpec(mode=mode, host=host)
        browser.host = host
        return browser

    def test_an_inject_session_with_no_host_still_refuses_a_bound_origin_at_the_url(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_profile(root, "Default", ["https://google.com"])
            with mock.patch.object(profile_mod, "CHROME_ROOT", root):
                browser = self.browser(root)
                refusal = browser.navigation_refusal("https://mail.google.com/mail/u/0/")
                self.assertIn("mail.google.com is device bound", refusal)
                self.assertIn("mode='clone'", refusal)

    def test_an_unbound_origin_goes_through(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_profile(root, "Default", ["https://google.com"])
            with mock.patch.object(profile_mod, "CHROME_ROOT", root):
                browser = self.browser(root)
                self.assertEqual(browser.navigation_refusal("https://github.com/"), "")

    def test_the_modes_that_keep_the_binding_or_own_a_key_are_never_refused(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_profile(root, "Default", ["https://google.com"])
            with mock.patch.object(profile_mod, "CHROME_ROOT", root):
                for mode in ("clone", "real", "dedicated"):
                    browser = self.browser(root, mode=mode)
                    self.assertEqual(
                        browser.navigation_refusal("https://mail.google.com/"), "",
                        f"{mode} keeps the binding or owns a key, so there is nothing to refuse")

    def test_a_port_or_userinfo_in_the_url_does_not_hide_the_host(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_profile(root, "Default", ["https://google.com"])
            with mock.patch.object(profile_mod, "CHROME_ROOT", root):
                browser = self.browser(root)
                for url in ("https://mail.google.com:443/x",
                            "https://someone@mail.google.com/x"):
                    self.assertIn("device bound", browser.navigation_refusal(url), url)


class TheRegistrationIsReadEvenWhenChromeHasNotCheckpointedIt(unittest.TestCase):
    def test_rows_that_live_only_in_the_sidecar_are_still_seen(self):
        with tempfile.TemporaryDirectory() as root:
            path = make_bound_profile(root, "Default", ["https://google.com"])
            con = sqlite3.connect(path)
            con.execute("pragma journal_mode=wal")
            con.execute("insert into dbsc_session_tbl (key, proto) values (?, ?)",
                        ("https://accounts.example.org", b"\x0a\x02hi"))
            con.commit()          # in the -wal, not yet checkpointed into the main file
            try:
                self.assertEqual(profile_mod.bound_hosts(root),
                                 {"google.com", "accounts.example.org"})
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
