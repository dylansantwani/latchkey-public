"""The verdict on Google's own sign-in pages, which used to read `logged-in`.

On 11 September two pages were reported `logged-in` through the MCP path and the tool text
told the model to proceed: Google's "Enter your password" page, and an account chooser that
listed both of the user's accounts as "Signed out". The marker that did it was the generic
`[aria-label*="account" i]` - Google's sign-in pages label a help link "Open Google Account
Help Center" and the account pill "...selected. Switch account". The model then asked the
user to type their password into the chat.

The pages below are those pages, as the probe read them (title, the start of the body, the
marker it found, whether a password field was visible), so the verdict is checked against
what actually arrived rather than against a page invented to pass.

    python3 -m unittest tests.test_detect_google -v
"""
from __future__ import annotations

import os
import sys
import json
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import detect, google, mcp_server  # noqa: E402
from latchkey.detect import PageProbe, PageState  # noqa: E402


class FakePage:
    def __init__(self, probe: dict):
        self.probe = probe

    def evaluate(self, script, cfg=None):
        return dict(self.probe)


def read(url: str, *, title: str, body: str, marker: str | None = None,
         password: bool = False, prompt: str | None = None, marker_site: bool = False,
         host_cookies: int | None = 2) -> PageState:
    """A page as `Driver.state` builds it: the probe's answer, then the session's cookies."""
    page = FakePage({"title": title, "body": body, "login_prompt": prompt,
                     "logged_in_marker": marker, "marker_site": marker_site,
                     "password_field": password, "blocked": None, "challenged": None,
                     "selectors": [], "urls": []})
    state = PageProbe().state(page, url, 1)
    state.host_cookies = host_cookies
    return state


PASSWORD_PAGE = dict(
    url="https://accounts.google.com/v3/signin/challenge/pwd?TL=ACv9tzHWjx&cid=2&continue="
        "https%3A%2F%2Fmail.google.com%2Fmail%2Fu%2F0%2F&flowName=GlifWebSignIn",
    title="Sign in - Google Accounts",
    body="Loading\nHi Test\nyou@gmail.com\nEnter your password\nShow password\n"
         "Next\nTry another way\nEnglish (United States)\nHelp\nPrivacy\nTerms\nHi Test",
    marker="you@gmail.com selected. Switch account", password=True)

CHOOSER_SIGNED_OUT = dict(
    url="https://accounts.google.com/v3/signin/accountchooser?continue=https://mail.google.com"
        "/mail/u/1/&emr=1&followup=https://mail.google.com/mail/u/1/&osid=1&passive=1209600"
        "&service=mail&flowName=GlifWebSignIn&flowEntry=ServiceLogin",
    title="Gmail",
    body="Loading\nChoose an account\nTest User\nyou@gmail.com\nSigned out\n"
         "Test User\nyou@school.example\nSigned out\nUse another account\n"
         "Remove an account\nEnglish (United States)\nHelp\nPrivacy\nTerms",
    marker="Open Google Account Help Center (external, opens in a new window)")

CHOOSER_HELD_BACK = dict(
    url="https://accounts.google.com/v3/signin/accountchooser?continue=https%3A%2F%2Fmail."
        "google.com%2Fmail%2Fu%2F0%2F&flowName=GlifWebSignIn&flowEntry=AccountChooser",
    title="Sign in - Google Accounts",
    body="Loading\nChoose an account\nTest User\nyou@gmail.com\nTest User\n"
         "you@school.example\nUse another account\nEnglish (United States)\n"
         "Help\nPrivacy\nTerms",
    marker="Open Google Account Help Center (external, opens in a new window)")

METHOD_CHOICE = dict(
    url="https://accounts.google.com/v3/signin/challenge/selection?TL=ACv9tzHWj",
    title="Sign in - Google Accounts",
    body="Loading\nHi Test\nyou@gmail.com\nChoose how you want to sign in:\n"
         "Enter your password\nUse your passkey\nTry another way\nEnglish (United States)\n"
         "Help\nPrivacy\nTerms\nHi Test",
    marker="you@gmail.com selected. Switch account")

PASSKEY_PROMPT = dict(
    url="https://accounts.google.com/v3/signin/challenge/pk?TL=ACv9t",
    title="Sign in - Google Accounts",
    body="Hi Test\nyou@gmail.com\nUse your passkey to confirm it's really you\n"
         "Your device will ask for your fingerprint, face, or screen lock\nContinue\n"
         "Try another way",
    marker="you@gmail.com selected. Switch account")

INBOX = dict(
    url="https://mail.google.com/mail/u/0/#inbox",
    title="Inbox (3) - you@gmail.com - Gmail",
    body="Compose\nInbox 3\nStarred\nSnoozed\nSent\nDrafts\n" + "A message subject - preview "
         "text of an email the user received this week\n" * 80,
    marker="Google Account: Test User (you@gmail.com)")


class GooglesSignInPagesAreSignedOut(unittest.TestCase):

    def test_the_password_page_is_not_signed_in(self):
        state = read(**PASSWORD_PAGE)
        self.assertEqual(state.verdict, "logged-out")
        self.assertEqual(state.signin, "Google password page")
        self.assertEqual(state.as_dict()["signin"], "Google password page")

    def test_an_account_chooser_whose_accounts_are_signed_out(self):
        state = read(**CHOOSER_SIGNED_OUT)
        self.assertEqual(state.verdict, "logged-out")
        self.assertEqual(state.signin, "Google account chooser")

    def test_the_chooser_a_copied_session_lands_on(self):
        """Inject mode held the account session back, so Gmail bounced to the chooser - no
        "Signed out" on it, but it is a sign-in step all the same."""
        self.assertEqual(read(**CHOOSER_HELD_BACK).verdict, "logged-out")

    def test_the_choose_how_to_sign_in_page(self):
        state = read(**METHOD_CHOICE)
        self.assertEqual(state.verdict, "logged-out")
        self.assertEqual(state.signin, "Google verification step")

    def test_a_passkey_prompt(self):
        state = read(**PASSKEY_PROMPT)
        self.assertEqual(state.verdict, "logged-out")
        self.assertEqual(state.signin, "Google passkey prompt")

    def test_the_signed_in_inbox_is_still_signed_in(self):
        state = read(**INBOX, host_cookies=14)
        self.assertEqual(state.verdict, "logged-in")
        self.assertIsNone(state.signin)
        self.assertNotIn("signin", state.as_dict())

    def test_a_verdict_computed_from_the_url_alone(self):
        """A PageState built without the probe (a double, a restored reply) still knows."""
        self.assertEqual(PageState(PASSWORD_PAGE["url"], "t", logged_in_marker="x",
                                   host_cookies=3).verdict, "logged-out")


class TheSameWordsElsewhere(unittest.TestCase):

    def test_a_signed_out_chooser_on_another_host_is_read_from_its_words(self):
        state = read(**{**CHOOSER_SIGNED_OUT, "url": "https://sso.example.edu/choose"})
        self.assertEqual(state.verdict, "logged-out")
        self.assertEqual(state.signin, "account chooser, accounts signed out")

    def test_a_passkey_sign_in_on_another_site(self):
        state = read("https://www.amazon.com/ap/signin", title="Amazon Sign-In",
                     body="Sign in\nSwitch accounts\nTest User\nPassword\nForgot "
                          "password?\nSign in\nKeep me signed in.\nor\nSign in with a passkey",
                     marker="Switch accounts", password=True)
        self.assertEqual(state.verdict, "logged-out")

    def test_a_long_signed_in_page_that_quotes_the_words_is_not_a_sign_in_step(self):
        """The phrases are only read on a short page: an article about passwords, or an email
        that says "enter your password", is not a password prompt."""
        body = "How to stay safe: never enter your password on a page you did not open.\n" * 60
        state = read("https://blog.example.com/security", title="Security tips", body=body,
                     marker="avatar", host_cookies=5)
        self.assertIsNone(state.signin)
        self.assertEqual(state.verdict, "logged-in")

    def test_the_oauth_account_picker_of_a_signed_in_user_is_not_a_sign_in_step(self):
        self.assertIsNone(google.signin_step(
            "https://accounts.google.com/signin/oauth/id?authuser=0&client_id=x"))

    def test_a_password_field_outranks_a_generic_marker(self):
        state = read("https://example.com/login", title="Log in", body="Email\nPassword\nLog in",
                     marker="avatar", password=True, host_cookies=4)
        self.assertEqual(state.verdict, "logged-out")

    def test_a_sites_own_marker_survives_a_password_field(self):
        """A hint written for a site is evidence a generic label is not: a signed-in settings
        page can have a password field on it."""
        state = read("https://chatgpt.com/#settings/Security", title="ChatGPT",
                     body="Settings\nSecurity\nChange password\nCurrent password",
                     marker="create-new-chat-button", marker_site=True, password=True,
                     host_cookies=9)
        self.assertEqual(state.verdict, "logged-in")


class TheMarkersThemselves(unittest.TestCase):

    def test_the_generic_account_label_is_gone(self):
        self.assertNotIn('[aria-label*="account" i]', detect.GENERIC_MARKERS)

    def test_googles_signed_in_avatar_label_is_a_marker(self):
        self.assertIn('[aria-label^="Google Account:" i]', detect.GENERIC_MARKERS)

    def test_bare_avatar_image_selectors_are_scoped_to_page_chrome(self):
        # A content-area <img alt="Event host avatar"> on eBay's logged-out homepage matched an
        # unscoped `img[alt*="avatar"]` and read the page `logged-in`. The bare avatar/profile
        # <img> selectors must be scoped to the top chrome, where a real account avatar lives.
        for sel in detect.GENERIC_MARKERS:
            if 'img[alt*="avatar"' in sel or 'img[alt*="profile"' in sel or 'img[class*="avatar"' in sel:
                self.assertNotEqual(sel.strip(), sel.strip().split()[-1],
                                    f"selector {sel!r} is unscoped; scope it to header/nav/banner")
                self.assertTrue(sel.startswith(':is(') or sel.startswith('header '),
                                f"selector {sel!r} must be scoped to page chrome")

    def test_the_probe_says_whether_the_marker_was_a_site_hint(self):
        self.assertIn("marker_site", detect.PROBE_JS)
        self.assertIn("site_markers", detect.PROBE_JS)
        seen = {}

        class Capture:
            def evaluate(self, script, cfg=None):
                seen.update(cfg)
                return {"title": "", "body": ""}

        PageProbe().read(Capture(), "https://chatgpt.com/", 1)
        self.assertEqual(seen["site_markers"], len(detect.SITE_MARKERS["chatgpt.com"]))
        self.assertEqual(seen["markers"][:seen["site_markers"]],
                         detect.SITE_MARKERS["chatgpt.com"])


class WhatTheModelIsToldOnThosePages(unittest.TestCase):

    def test_the_surface_forbids_asking_for_a_password_in_chat(self):
        description = mcp_server.advertised("batch")[0]["description"]
        self.assertIn("NEVER ask the user to type a password", description)
        self.assertIn("never ask the user to type a password", mcp_server.INSTRUCTIONS.lower())
        self.assertIn("Never ask", mcp_server.HELP_TOPICS["verdicts"])

    def test_inject_is_no_longer_sold_as_usually_enough(self):
        for tool in mcp_server.advertised("all"):
            self.assertNotIn("usually enough", tool["description"])

    def test_the_rules_route_google_to_latchkeys_own_profile_and_to_its_one_sign_in(self):
        """The inverse of what this asserted before, and the reason is worth keeping here.

        Google used to route to a clone of the user's own profile, and the surface told
        models so - including "do not call latchkey_login for Google". Two browsers holding
        one account session is what Google reads as a replay, and what it ends is the
        *account's* session: a sign-out in the user's own Chrome. So the route is now
        latchkey's own profile, and the surface has to say the opposite of what it said.
        """
        rules = mcp_server.VERDICT_RULES
        self.assertIn("latchkey's own profile", rules)
        self.assertNotIn("clone of the user's own Chrome profile", rules)
        self.assertIn("latchkey_login", rules)
        self.assertNotIn("do not call latchkey_login for Google", mcp_server.INSTRUCTIONS)
        self.assertIn("latchkey_accounts", mcp_server.INSTRUCTIONS)
        description = mcp_server.advertised("batch")[0]["description"]
        self.assertIn("never copied or signed out", description)

    def test_no_part_of_the_surface_still_sells_cloning_the_users_google_login(self):
        """One sweep, so a stale sentence in any tool's prose is caught, not just the rules."""
        stale = ("clone of the user's own", "clone of their profile",
                 "do not call latchkey_login for Google")
        for tool in mcp_server.advertised("all"):
            blob = json.dumps(tool)
            for phrase in stale:
                self.assertNotIn(phrase, blob, f"{tool['name']} still says {phrase!r}")
        for topic, text in mcp_server.HELP_TOPICS.items():
            for phrase in stale:
                self.assertNotIn(phrase, text, f"help topic {topic!r} still says {phrase!r}")
        for name, text in mcp_server.HELP_DETAIL.items():
            for phrase in stale:
                self.assertNotIn(phrase, text, f"help for {name!r} still says {phrase!r}")


if __name__ == "__main__":
    unittest.main()
