"""Several logins, one per account, each on a profile of its own.

The thing this makes possible is a second Google account. It could not be done before,
and the reason is worth keeping: latchkey had exactly one profile of its own
(`~/.latchkey/profile`, reachable only through an environment variable), and Google did
not use it anyway - a Google session was a *copy* of the user's real profile. That copy is
the bug this whole area exists to be rid of. It goes stale, because the account session's
freshness token is re-issued to whichever client presented it last; and when Google reads
two holders of one session as a replay, what it ends is the account's session, which is a
sign-out in the user's own Chrome.

So: Google runs on a profile latchkey owns and signs into once, and an account name picks
which one. Nothing here copies a login, and nothing here can sign the user out of Chrome.

    python3 -m unittest tests.test_accounts -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import latchkey.login as login_mod  # noqa: E402
import latchkey.mcp_server as mcp_server  # noqa: E402
import latchkey.profile as profile_mod  # noqa: E402
from latchkey import google  # noqa: E402
from latchkey.session import Browser, SessionSpec  # noqa: E402

CHROME_EPOCH = 11_644_473_600


def sign_in(path: str, *names: str) -> None:
    """Give a profile directory the cookie names a signed-in Google account leaves."""
    db = os.path.join(path, "Default", "Cookies")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table if not exists cookies (host_key text, name text, "
                "encrypted_value blob, expires_utc integer, is_secure integer, "
                "is_httponly integer, path text, samesite integer, has_expires integer, "
                "is_persistent integer, top_frame_site_key text)")
    for name in (names or ("SID", "__Secure-1PSID")):
        con.execute("insert into cookies values (?,?,?,?,1,1,'/',0,1,1,'')",
                    (".google.com", name, b"x", 0))
    con.commit()
    con.close()


class Isolated(unittest.TestCase):
    """Its own LATCHKEY_HOME, so both the default profile and accounts/ land in a tmpdir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        patch = mock.patch.dict(os.environ, {"LATCHKEY_HOME": self.home,
                                             google.GOOGLE_SSO_ACCOUNTS_ENV:
                                                 '{"portal.example.edu": "school"}'},
                                clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        # LATCHKEY_PROFILE_DIR would win over a name; these tests are about names.
        os.environ.pop("LATCHKEY_PROFILE_DIR", None)
        self.addCleanup(self._tmp.cleanup)


class AnAccountNameIsCheckedNotTrusted(unittest.TestCase):

    def test_the_shapes_that_are_allowed(self):
        for name in ("school", "work-gmail", "a", "acct_2", "SCHOOL"):
            self.assertEqual(profile_mod.normalize_account(name), name.lower())

    def test_nothing_is_a_route_out_of_latchkeys_directory(self):
        for name in ("../evil", "..", ".", "a/b", "a\\b", "/etc/passwd", "-lead", "x" * 65,
                     "a b", "acct.2", "acct:2"):
            with self.assertRaises(ValueError, msg=name):
                profile_mod.normalize_account(name)

    def test_nothing_means_the_default_account(self):
        for empty in (None, "", "   "):
            self.assertEqual(profile_mod.normalize_account(empty), profile_mod.DEFAULT_ACCOUNT)


class WhereEachAccountLives(Isolated):

    def test_the_default_account_keeps_the_directory_latchkey_has_always_used(self):
        """Not `accounts/default`: that directory may hold a sign-in from months ago, and a
        tidier layout is not worth losing it to."""
        self.assertEqual(profile_mod.account_path("default"),
                         os.path.join(self.home, "profile"))

    def test_every_other_account_is_a_directory_of_its_own(self):
        self.assertEqual(profile_mod.account_path("school"),
                         os.path.join(self.home, "accounts", "school"))
        self.assertNotEqual(profile_mod.account_path("school"),
                            profile_mod.account_path("personal"))

    def test_a_name_never_resolves_inside_the_real_chrome_profile(self):
        with self.assertRaises(ValueError):
            profile_mod.dedicated_path(os.path.join(profile_mod.CHROME_ROOT, "Default"))

    def test_they_are_listed_default_first(self):
        self.assertEqual(profile_mod.account_names(), [])
        profile_mod.dedicated_dir(None, "school")
        profile_mod.dedicated_dir(None, "default")
        self.assertEqual(profile_mod.account_names(), ["default", "school"])

    def test_a_directory_that_is_not_a_usable_name_is_not_reported_as_an_account(self):
        os.makedirs(os.path.join(profile_mod.accounts_root(), "not a name"), exist_ok=True)
        self.assertNotIn("not a name", profile_mod.account_names())

    def test_a_path_can_be_turned_back_into_its_account(self):
        path = profile_mod.dedicated_dir(None, "school")
        self.assertEqual(profile_mod.account_of_path(path), "school")
        self.assertIsNone(profile_mod.account_of_path("/tmp/somewhere-else"))

    def test_an_explicit_directory_still_wins_and_a_name_beats_the_environment(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_PROFILE_DIR": "/tmp/from-env"}):
            self.assertEqual(profile_mod.dedicated_path("/tmp/explicit"), "/tmp/explicit")
            self.assertEqual(profile_mod.dedicated_path(None, "school"),
                             profile_mod.account_path("school"))
            self.assertEqual(profile_mod.dedicated_path(), "/tmp/from-env")


class WhatEachAccountReportsAboutItself(Isolated):

    def test_an_account_with_no_profile_yet_says_how_to_make_one(self):
        rows = login_mod.list_accounts()
        self.assertEqual([r["account"] for r in rows], ["default"])
        self.assertFalse(rows[0]["exists"])
        self.assertFalse(rows[0]["google_signed_in"])
        self.assertIn("latchkey login", rows[0]["next_step"])

    def test_a_signed_in_account_is_read_from_its_own_store_by_cookie_name(self):
        sign_in(profile_mod.dedicated_dir(None, "school"))
        rows = {r["account"]: r for r in login_mod.list_accounts()}
        self.assertTrue(rows["school"]["google_signed_in"])
        self.assertFalse(rows["default"]["google_signed_in"])
        self.assertNotIn("next_step", rows["school"])

    def test_accounts_do_not_read_each_others_stores(self):
        sign_in(profile_mod.dedicated_dir(None, "school"))
        profile_mod.dedicated_dir(None, "personal")
        rows = {r["account"]: r for r in login_mod.list_accounts()}
        self.assertTrue(rows["school"]["google_signed_in"])
        self.assertFalse(rows["personal"]["google_signed_in"])

    def test_nothing_it_reads_is_the_users_own_chrome(self):
        with mock.patch.object(profile_mod, "CHROME_ROOT", "/nonexistent-on-purpose"):
            rows = login_mod.list_accounts()
        self.assertTrue(rows, "listing accounts must not depend on the user's Chrome")


class ASessionCarriesTheAccountItWasOpenedFor(Isolated):

    def test_the_spec_resolves_its_account_to_that_accounts_directory(self):
        spec = SessionSpec(mode="dedicated", account="school")
        self.assertEqual(profile_mod.dedicated_path(spec.profile_dir, spec.account),
                         profile_mod.account_path("school"))

    def test_the_account_travels_with_the_spec(self):
        self.assertEqual(SessionSpec(mode="dedicated", account="school").as_dict()["account"],
                         "school")

    def test_login_status_answers_about_the_account_it_is_asked_about(self):
        sign_in(profile_mod.dedicated_dir(None, "school"))
        self.assertTrue(login_mod.status(account="school")["signed_in"])
        self.assertFalse(login_mod.status(account="personal")["signed_in"])


class TheAgentSurfaceExposesTheAccounts(Isolated):

    def test_the_accounts_tool_says_which_logins_exist_and_which_are_signed_in(self):
        sign_in(profile_mod.dedicated_dir(None, "school"))
        out = mcp_server.tool_accounts()
        self.assertEqual(out["signed_in"], ["school"])
        self.assertEqual(out["default"], "default")
        self.assertIn("usage", out)

    def test_with_nothing_signed_in_it_names_the_one_sign_in_to_do(self):
        out = mcp_server.tool_accounts()
        self.assertEqual(out["signed_in"], [])
        self.assertIn("latchkey_login", out["next_step"])
        self.assertIn("never ask the user for a password", out["next_step"].lower())

    def test_it_is_dispatchable_and_advertised(self):
        self.assertIn("latchkey_accounts", mcp_server.DISPATCH)
        self.assertIn("latchkey_accounts", mcp_server.SPEC_OF)

    def test_a_bad_account_name_is_a_tool_error_naming_the_rule(self):
        for tool, kwargs in ((mcp_server.tool_session_open, {"name": "s"}),
                             (mcp_server.tool_login, {}),
                             (mcp_server.tool_login_status, {"wait_s": 0})):
            with self.assertRaises(ValueError) as caught:
                tool(account="../etc", **kwargs)
            self.assertIn("account name", str(caught.exception))

    def test_the_words_a_model_reaches_for_are_accepted_as_the_account(self):
        for tool in ("latchkey_session_open", "latchkey_login", "latchkey_login_status"):
            self.assertEqual(mcp_server.ARG_ALIASES[tool].get("profile"), "account")

    def test_the_surface_tells_a_model_that_google_needs_this_sign_in(self):
        self.assertIn("latchkey_accounts", mcp_server.INSTRUCTIONS)
        self.assertIn("latchkey_login", mcp_server.VERDICT_RULES)


class TheAdviceNamesTheAccountItIsAbout(Isolated):
    """Advice that names the wrong profile is worse than none: it is followed, and fails."""

    def test_a_named_account_is_told_to_sign_that_account_in(self):
        step = login_mod.status(account="school")["next_step"]
        self.assertIn("account='school'", step)

    def test_the_default_account_is_not_cluttered_with_a_name(self):
        step = login_mod.status()["next_step"]
        self.assertIn("latchkey_login", step)
        self.assertNotIn("account=", step)

    def test_a_signed_in_named_account_says_how_to_open_a_session_on_it(self):
        sign_in(profile_mod.dedicated_dir(None, "school"))
        step = login_mod.status(account="school")["next_step"]
        self.assertIn("latchkey_session_open", step)
        self.assertIn("account='school'", step)

    def test_the_status_says_which_account_it_answered_about(self):
        self.assertEqual(login_mod.status(account="school")["account"], "school")
        self.assertEqual(login_mod.status()["account"], "default")

    def test_the_cli_advice_names_the_account_too(self):
        from latchkey import cli
        self.assertIn("--account school",
                      cli._next_step("https://mail.google.com/", "dedicated", "school"))
        self.assertNotIn("--account",
                         cli._next_step("https://mail.google.com/", "dedicated", "default"))


class TheAccountSurvivesTheMoveToGoogle(Isolated):
    """A session that named an account keeps it when the site changes its mode.

    Verified against a real Chrome as well: a session opened with account='school' starts
    as inject, navigates to Google, moves itself to dedicated, and the profile it opens is
    ~/.latchkey/accounts/school. This is that path without a browser.
    """

    class Routing(Browser):
        def __init__(self, spec):
            super().__init__(None, spec=spec)
            self.lifecycle = []
            self._pw = object()

        def close(self):
            self.lifecycle.append(("close", self.spec.mode, self.spec.account))

        def start(self):
            self.lifecycle.append(("start", self.spec.mode, self.spec.account))
            self._pw = object()
            return self

    def test_the_account_is_still_named_after_the_mode_changes(self):
        browser = self.Routing(SessionSpec(account="school"))
        browser.route_for("https://mail.google.com/")
        self.assertEqual(browser.spec.mode, "dedicated")
        self.assertEqual(browser.spec.account, "school")
        self.assertEqual(browser.lifecycle,
                         [("close", "inject", "school"), ("start", "dedicated", "school")])

    def test_the_profile_that_move_will_open_is_that_accounts(self):
        spec = SessionSpec(account="school")
        spec.mode = "dedicated"
        self.assertEqual(profile_mod.dedicated_path(spec.profile_dir, spec.account),
                         profile_mod.account_path("school"))
        self.assertNotEqual(profile_mod.dedicated_path(spec.profile_dir, spec.account),
                            profile_mod.account_path("default"))

    def test_a_session_with_no_account_still_goes_to_the_default_one(self):
        browser = self.Routing(SessionSpec())
        browser.route_for("https://mail.google.com/")
        self.assertEqual(browser.spec.mode, "dedicated")
        self.assertIsNone(browser.spec.account)
        self.assertEqual(profile_mod.dedicated_path(None, browser.spec.account),
                         profile_mod.account_path(profile_mod.DEFAULT_ACCOUNT))

    def test_a_known_sso_portal_picks_its_own_account_on_a_no_account_session(self):
        # The configured portal maps to a named account, so a no-account session
        # moves to dedicated mode and selects that profile.
        browser = self.Routing(SessionSpec())
        browser.route_for("https://portal.example.edu/campus/portal")
        self.assertEqual(browser.spec.mode, "dedicated")
        self.assertEqual(browser.spec.account, "school")
        self.assertEqual(browser.lifecycle,
                         [("close", "inject", None), ("start", "dedicated", "school")])
        self.assertEqual(profile_mod.dedicated_path(None, browser.spec.account),
                         profile_mod.account_path("school"))

    def test_an_explicit_account_is_not_overridden_by_a_portal_mapping(self):
        # The caller named 'default' on purpose; a portal mapping must not steal it away.
        browser = self.Routing(SessionSpec(account="default"))
        browser.route_for("https://portal.example.edu/campus/portal")
        self.assertEqual(browser.spec.account, "default")

    def test_a_lookalike_host_does_not_inherit_the_portal_account(self):
        browser = self.Routing(SessionSpec())
        browser.route_for("https://portal.example.edu.attacker.test/")
        self.assertIsNone(browser.spec.account)


class GoogleNoLongerBorrowsTheUsersLogin(unittest.TestCase):

    def test_the_default_route_for_google_is_latchkeys_own_profile(self):
        self.assertEqual(google.google_mode(), "dedicated")
        self.assertEqual(google.mode_for("https://mail.google.com/"), "dedicated")

    def test_a_copy_is_still_available_to_anyone_who_asks_for_it(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_GOOGLE_MODE": "clone"}):
            self.assertEqual(google.mode_for("https://mail.google.com/"), "clone")


if __name__ == "__main__":
    unittest.main()
