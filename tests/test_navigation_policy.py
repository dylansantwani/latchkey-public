"""Where a session may go.

`read_only` refuses the verbs that send something, which is the right cut for acting as
the user on a website - and it says nothing about *where the browser goes*. A browser is
a perfectly good local file reader, so a read-only session could open
`file:///Users/you/.ssh/id_rsa` (not a write, so nothing objected) and read it back with
`latchkey_text` like any other page. These pin the schemes down.

    python3 -m unittest tests.test_navigation_policy -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import driver, policy  # noqa: E402


class TheWebIsTheDefaultScope(unittest.TestCase):
    def test_http_and_https_go_through_untouched(self):
        for url in ("https://github.com/", "http://localhost:8000/x?y=1#z"):
            self.assertEqual(policy.check_url(url), url)

    def test_a_bare_host_is_read_as_https_the_way_an_address_bar_does(self):
        self.assertEqual(policy.check_url("example.com"), "https://example.com")
        self.assertEqual(policy.check_url("  example.com/a/b  "), "https://example.com/a/b")

    def test_about_blank_is_where_a_session_starts_and_is_allowed(self):
        self.assertEqual(policy.check_url("about:blank"), "about:blank")

    def test_a_file_url_is_refused_and_says_what_to_use_instead(self):
        with self.assertRaises(policy.NavigationRefused) as caught:
            policy.check_url("file:///Users/someone/.ssh/id_rsa")
        message = str(caught.exception)
        self.assertIn("will not open file:", message)
        self.assertIn("LATCHKEY_ALLOW_SCHEMES", message)

    def test_the_other_schemes_a_browser_answers_are_refused_too(self):
        for url in ("chrome://settings", "javascript:fetch('/x')",
                    "data:text/html,<b>hi", "devtools://devtools/x", "view-source:https://x.com"):
            with self.assertRaises(policy.NavigationRefused, msg=url):
                policy.check_url(url)

    def test_the_human_can_open_one_and_only_the_human(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_ALLOW_SCHEMES": "file"}):
            self.assertEqual(policy.check_url("file:///tmp/x"), "file:///tmp/x")
            with self.assertRaises(policy.NavigationRefused):
                policy.check_url("chrome://settings")
        with self.assertRaises(policy.NavigationRefused):
            policy.check_url("file:///tmp/x")


class EveryRouteToANewPageIsGuarded(unittest.TestCase):
    """goto is the single funnel: the tool, an action list, a new tab, the login handoff."""

    class Harness(driver.Driver):
        def __init__(self, refusal=""):
            super().__init__()
            self.refusal = refusal
            self.went = []

        def navigation_refusal(self, url):
            return self.refusal

    def test_the_guard_normalises_and_returns_the_url(self):
        self.assertEqual(self.Harness().guard_navigation("example.com"),
                         "https://example.com")

    def test_a_refused_scheme_never_reaches_the_page(self):
        with self.assertRaises(policy.NavigationRefused):
            self.Harness().guard_navigation("file:///etc/passwd")

    def test_a_session_level_refusal_is_raised_as_one(self):
        harness = self.Harness(refusal="google.com is device bound")
        with self.assertRaises(policy.NavigationRefused) as caught:
            harness.guard_navigation("https://mail.google.com/")
        self.assertIn("device bound", str(caught.exception))

    def test_a_plain_driver_refuses_nothing_of_its_own(self):
        self.assertEqual(driver.Driver().navigation_refusal("https://x.com"), "")


if __name__ == "__main__":
    unittest.main()
