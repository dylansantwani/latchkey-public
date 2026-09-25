"""The agent-facing surface, without a browser: what a reply carries, and what a wait does."""
import unittest

from latchkey import a11y, detect, mcp_server
from latchkey import driver


def node(ref="e1", role="button", name="Continue", depth=0, **extra):
    return {"ref": ref, "role": role, "name": name, "depth": depth, **extra}


class TestAReplyCarriesTheShapeAndNotTheProse(unittest.TestCase):
    """An action's reply is a receipt, not a transcript: four kilobytes of page text next to
    "clicked Continue" is context an agent spends and gets nothing for."""

    class State:
        text = "x" * 4000

        def as_dict(self, detail=False):
            out = {"url": "https://example.com/", "title": "Sign in"}
            if detail:
                out["text"] = self.text
            elif self.text:
                out["text_chars"] = len(self.text)
            return out

    class Browser:
        dialogs = [{"type": "alert", "message": "Signing you in", "answered": "dismissed"}]

    def test_the_text_is_left_out_and_sized_instead(self):
        out = mcp_server._page_reply(self.State())
        self.assertNotIn("text", out)
        self.assertEqual(out["text_chars"], 4000)

    def test_a_dialog_that_answered_itself_is_worth_the_lines(self):
        out = mcp_server._page_reply(self.State(), self.Browser.dialogs)
        self.assertEqual(out["dialogs"][0]["type"], "alert")
        self.assertIn("dismissed", out["dialogs"][0]["answered"])

    def test_a_quiet_session_says_nothing_extra(self):
        self.assertNotIn("dialogs", mcp_server._page_reply(self.State()))

    def test_asking_for_the_text_still_gets_it(self):
        state = self.State()
        self.assertEqual(len(state.as_dict(detail=True)["text"]), 4000)


class TestWaitIsOneCallInsteadOfALoop(unittest.TestCase):
    class Page:
        def __init__(self):
            self.calls = []
            self.url = "https://example.com/next"

        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append(("selector", selector, state, timeout))

        def wait_for_url(self, pattern, timeout=None):
            self.calls.append(("url", pattern.pattern, timeout))

        def wait_for_load_state(self, state, timeout=None):
            self.calls.append(("load", state, timeout))

    class Browser:
        _poll = driver.Driver._poll               # the real poll, asking a fake page

        def __init__(self, page):
            self.page = page

        def state(self):
            class Now:
                def summary(self):
                    return {"url": "https://example.com/next"}
            return Now()

        def run_js(self, js, arg=None):
            # The poll runs in latchkey's own world, not through Playwright's
            # `wait_for_function` (which polls in the page's main world, in view).
            self.page.calls.append(("function", js, arg))
            return True

    def wait(self, until, value=None, timeout_ms=5000):
        page = self.Page()
        result = driver.Driver.wait_until(self.Browser(page), until, value, timeout_ms)
        return result, page.calls

    def test_waiting_for_text_asks_from_latchkeys_own_world(self):
        result, calls = self.wait("text", "welcome back")
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], "function")
        self.assertIn("includes(t)", calls[0][1])
        self.assertEqual(calls[0][2], "welcome back")
        self.assertEqual(len(calls), 1)                     # true at once: asked once
        self.assertIn("waited_ms", result)

    def test_a_poll_that_never_comes_true_times_out_and_says_so(self):
        class Never(self.Browser):
            def run_js(self, js, arg=None):
                self.page.calls.append(("function", js, arg))
                return False
        page = self.Page()
        result = driver.Driver.wait_until(Never(page), "text", "never", 150)
        self.assertFalse(result["ok"])
        self.assertIn("TimeoutError", result["error"])
        self.assertGreaterEqual(len(page.calls), 1)
        self.assertGreaterEqual(result["waited_ms"], 100)

    def test_every_documented_until_reaches_the_right_primitive(self):
        self.assertEqual(self.wait("gone", "Loading")[1][0][0], "function")
        self.assertEqual(self.wait("selector", "#done")[1][0][:3], ("selector", "#done", "visible"))
        self.assertEqual(self.wait("hidden", "#done")[1][0][:2], ("selector", "#done"))
        self.assertEqual(self.wait("url", "/account")[1][0][0], "url")
        self.assertEqual(self.wait("load")[1][0], ("load", "load", 5000))
        self.assertEqual(self.wait("networkidle")[1][0], ("load", "networkidle", 5000))

    def test_an_until_it_does_not_know_says_what_it_does_know(self):
        result, calls = self.wait("explode", "x")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        self.assertIn("text", result["error"])
        self.assertIn("networkidle", result["error"])


class TestFindIsHowAnAgentSkipsReading(unittest.TestCase):
    def setUp(self):
        self.nodes = [
            node("e1", "heading", "Sign in", 0),
            node("e2", "textbox", "Email", 1),
            node("e3", "button", "Continue", 1),
            node("e4", "link", "Continue as guest", 1),
            node("e5", "button", "Sign in with Google", 1),
        ]

    def find(self, query, limit=8):
        real = a11y.nodes
        a11y.nodes = lambda browser, **kwargs: {"nodes": self.nodes, "frames": []}

        class Browser:
            class page:
                url = "https://example.com/login"

            refs = a11y.Refs()

        try:
            return a11y.find(Browser(), query, limit)
        finally:
            a11y.nodes = real

    def test_an_exact_name_beats_a_longer_one_that_contains_it(self):
        found = self.find("continue")
        self.assertEqual(found["matches"][0]["ref"], "e3")

    def test_a_partial_query_still_finds_the_thing_with_a_ref_to_act_on(self):
        found = self.find("google")
        self.assertEqual([match["name"] for match in found["matches"]], ["Sign in with Google"])
        self.assertEqual(found["matches"][0]["ref"], "e5")

    def test_multi_word_queries_rank_by_how_much_of_the_query_matches(self):
        found = self.find("sign in")
        self.assertEqual(found["matches"][0]["ref"], "e5")

    def test_nothing_matching_says_what_to_do_instead(self):
        found = self.find("purchase a yacht")
        self.assertEqual(found["found"], 0)
        self.assertIn("latchkey_snapshot", found["next_step"])

    def test_the_limit_is_honoured(self):
        self.assertEqual(len(self.find("continue", limit=1)["matches"]), 1)


class TestAReplyNeverCarriesAPageInItsUrl(unittest.TestCase):
    """A data: URL *is* the page: pasting it into a reply costs more than the text these
    replies exist to leave out, and it was 2.5 kB of a 2.9 kB reply."""

    def test_a_data_url_is_reported_by_its_size_instead(self):
        url = "data:text/html," + "x" * 4000
        self.assertEqual(detect.short_url(url),
                         f"data: page ({len(url)} characters of markup)")

    def test_a_real_url_is_left_exactly_alone(self):
        url = "https://example.com/a/b?c=1#frag"
        self.assertEqual(detect.short_url(url), url)

    def test_an_absurd_url_is_cut_and_the_cut_is_visible(self):
        short = detect.short_url("https://example.com/" + "y" * 500, limit=60)
        self.assertEqual(len(short), 60)
        self.assertTrue(short.endswith("…"))
