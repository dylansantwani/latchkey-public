"""Offline tests for wall attribution: who stopped us, and on what evidence.

The point of these is the *hard* cases, not the easy ones. Naming Cloudflare on a
"Just a moment..." page is trivial; the tests that matter are the ones that keep a
`server: cloudflare` header on a healthy page from being read as a bot wall, and a
permission-denied 403 from being read as a refusal to serve a robot.

    cd ~/tools/latchkey
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import walls  # noqa: E402


class TestChallengeShelves(unittest.TestCase):
    """A challenge is not a refusal: one clears itself, the other never will."""

    def test_cloudflare_managed_challenge(self):
        wall = walls.attribute(
            title="Just a moment...", body="Just a moment...",
            selectors=["#challenge-form", "script[src*='/cdn-cgi/challenge-platform/']"],
            status=403, headers={"server": "cloudflare", "cf-ray": "8f2c1d0e9b1a"},
            cookie_names=["cf_clearance", "__cf_bm"])
        self.assertEqual(wall.vendor, "cloudflare")
        self.assertEqual(wall.kind, "challenge")
        self.assertEqual(wall.confidence, "high")
        self.assertTrue(wall.strong)
        self.assertIn("cookie cf_clearance", wall.reasons)

    def test_turnstile_widget_with_no_words_at_all(self):
        # The checkbox case: the widget is an iframe with no readable text, and the
        # page around it says nothing a text match would find.
        wall = walls.attribute(
            selectors=["iframe[src*='challenges.cloudflare.com']"],
            urls=["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x"],
            title="", body="", status=200)
        self.assertEqual(wall.vendor, "cloudflare")
        self.assertEqual(wall.kind, "challenge")

    def test_recaptcha_is_named_from_its_script_host(self):
        wall = walls.attribute(
            urls=["https://www.google.com/recaptcha/api.js"],
            body="This site is protected by reCAPTCHA and the Google Privacy Policy apply.",
            title="Sign in", status=200)
        self.assertEqual(wall.vendor, "recaptcha")
        self.assertEqual(wall.kind, "challenge")

    def test_human_press_and_hold(self):
        wall = walls.attribute(selectors=["#px-captcha"], status=403,
                               cookie_names=["_pxhd", "pxvid"])
        self.assertEqual(wall.vendor, "human")
        self.assertEqual(wall.kind, "challenge")

    def test_queue_is_its_own_kind(self):
        wall = walls.attribute(body="You are now in line. Your estimated wait is 4 minutes.")
        self.assertEqual(wall.vendor, "queue-it")
        self.assertEqual(wall.kind, "queue")


class TestHardBlocks(unittest.TestCase):
    def test_cloudflare_1020_block_outranks_a_widget(self):
        # A page that says "you have been blocked" is a block even when challenge
        # markup is somewhere in it: the words are the site's own, and they are final.
        wall = walls.attribute(
            title="Attention Required! | Cloudflare",
            body="Sorry, you have been blocked. You are unable to access this site.",
            selectors=["#challenge-form"], status=403,
            headers={"cf-ray": "8f2c", "cf-mitigated": "challenge"})
        self.assertEqual(wall.vendor, "cloudflare")
        self.assertEqual(wall.kind, "block")
        self.assertEqual(wall.confidence, "high")

    def test_datadome_block_without_text_is_still_a_block(self):
        wall = walls.attribute(cookie_names=["datadome"], status=403, body="",
                               headers={"x-datadome": "protected"})
        self.assertEqual(wall.vendor, "datadome")
        self.assertEqual(wall.kind, "block")

    def test_akamai_reference_number(self):
        wall = walls.attribute(body="Access Denied. Reference #18.1a2b3c4d.1749",
                               cookie_names=["_abck", "bm_sz"], status=403,
                               headers={"server": "AkamaiGHost"})
        self.assertEqual(wall.vendor, "akamai")
        self.assertEqual(wall.kind, "block")

    def test_imperva_incident_id(self):
        wall = walls.attribute(body="Request unsuccessful. Incapsula incident ID: 123-456-789",
                               status=403, headers={"x-iinfo": "9-12345-12346 NNNN"})
        self.assertEqual(wall.vendor, "imperva")
        self.assertEqual(wall.kind, "block")

    def test_aws_waf_challenge(self):
        wall = walls.attribute(selectors=["#challenge-container"],
                               urls=["https://x.example/aaws/challenge.js"],
                               headers={"x-amzn-waf-action": "challenge"}, status=202)
        self.assertEqual(wall.vendor, "aws-waf")
        self.assertEqual(wall.kind, "challenge")


class TestFalsePositives(unittest.TestCase):
    """The expensive direction. A wall called on a healthy page breaks a session."""

    def test_a_healthy_page_behind_cloudflare_is_not_a_wall(self):
        wall = walls.attribute(title="Example Domain",
                               body="This domain is for use in illustrative examples.",
                               status=200, headers={"server": "cloudflare",
                                                    "cf-ray": "8f2c1d0e9b1a", "cf-cache-status": "HIT"})
        self.assertEqual(wall.kind, "")          # named at most, never a wall
        self.assertFalse(wall.strong)
        self.assertEqual(wall.confidence, "low")

    def test_a_permission_denial_with_content_is_not_a_bot_wall(self):
        # 403 from a normal app behind Cloudflare: a real page with real words on it.
        wall = walls.attribute(status=403, headers={"server": "cloudflare", "cf-ray": "8f2c"},
                               title="403 Forbidden",
                               body=("You do not have permission to view this document. "
                                     "Contact the owner of this repository for access. " * 6))
        self.assertEqual(wall.kind, "")
        self.assertFalse(wall.strong)

    def test_a_bare_denial_is_called_a_wall_even_without_a_vendor_cookie(self):
        wall = walls.attribute(status=403, body="", headers={"server": "cloudflare",
                                                             "cf-ray": "8f2c"})
        self.assertEqual(wall.vendor, "cloudflare")
        self.assertEqual(wall.kind, "block")
        self.assertEqual(wall.confidence, "low")

    def test_nothing_at_all_returns_an_empty_wall(self):
        wall = walls.attribute(title="Fine", body="Everything is fine", status=200)
        self.assertFalse(wall.known)
        self.assertEqual(wall.sentence(), "")
        self.assertEqual(wall.as_dict()["kind"], "")


class TestSentences(unittest.TestCase):
    def test_a_sentence_reads_like_a_sentence(self):
        wall = walls.attribute(cookie_names=["cf_clearance"], status=403,
                               title="Just a moment...", body="Just a moment...")
        self.assertEqual(wall.sentence(),
                         "Cloudflare challenge HTTP 403 high - cookie cf_clearance")

    def test_reasons_are_deduplicated(self):
        wall = walls.attribute(body="Just a moment... just a moment... just a moment...",
                               selectors=["#challenge-form", "#challenge-form"])
        self.assertEqual(len(wall.reasons), len(set(wall.reasons)))

    def test_from_page_uses_the_probe_shape(self):
        wall = walls.from_page({"title": "Just a moment...", "body": "Just a moment...",
                                "urls": [], "selectors": ["#challenge-form"]},
                               headers={"cf-ray": "x"}, status=403)
        self.assertEqual(wall.vendor, "cloudflare")
        self.assertEqual(wall.kind, "challenge")

    def test_every_marker_selector_names_a_vendor(self):
        # A marker with no vendor mapping is a sweep that costs time and says nothing:
        # asked about on every page read, and unable to tell you who asked.
        for selector in walls.MARKERS:
            wall = walls.attribute(selectors=[selector])
            self.assertTrue(wall.vendor, f"no vendor for marker {selector!r}")


if __name__ == "__main__":
    unittest.main()
