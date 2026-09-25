"""The snapshot's two halves, tested apart.

The walker is injected JavaScript, so it needs a page and is checked against a real one
(`examples/verify_snapshot.py`) rather than here. Everything its answer goes through is
testable without a browser: the rendering, the character budget, the suffix that says what
changed, the ref table, and the rule that decides which selector a ref turns into.
"""
import unittest

from latchkey import a11y, automation, driver


def node(ref="e1", role="button", name="Sign in", depth=0, **extra):
    return {"ref": ref, "role": role, "name": name, "depth": depth, **extra}


class TestWhatASnapshotLooksLike(unittest.TestCase):
    def test_the_reading_names_things_and_hands_back_refs(self):
        text = a11y.render([
            node("e1", "heading", "Sign in", level=1),
            node("e2", "textbox", "Email", 1, placeholder="you@example.com"),
            node("e3", "checkbox", "Remember me", 1, checked=True),
            node("e4", "button", "Continue", 1, disabled=True),
        ], url="https://example.com/login")
        self.assertIn('heading1 "Sign in" [ref=e1]', text)
        self.assertIn('textbox "Email" placeholder="you@example.com" [ref=e2]', text)
        self.assertIn('checkbox "Remember me" checked [ref=e3]', text)
        self.assertIn('button "Continue" disabled [ref=e4]', text)
        self.assertIn('  - textbox "Email" placeholder="you@example.com" [ref=e2]', text,
                      "a control inside a form is indented under it")
        self.assertIn("(4 refs)", text)
        self.assertTrue(text.startswith("https://example.com/login"))

    def test_the_outline_is_the_shape_of_the_page_rather_than_its_contents(self):
        text = a11y.render([
            node("e1", "navigation", "Main", 0, sel="nav.main"),
            node("e2", "link", "One", 1, parent="e1"),
            node("e3", "link", "Two", 1, parent="e1"),
            node("e4", "form", "Sign in form", 0, sel="form >> nth=0"),
            node("e5", "textbox", "Email", 1, parent="e4"),
        ], mode="outline")
        self.assertIn('navigation "Main" · 2 controls · selector:"nav.main"', text)
        self.assertIn('form "Sign in form" · 1 controls', text)
        self.assertNotIn("ref=e5", text)

    def test_text_mode_is_the_text_and_nothing_else(self):
        text = a11y.render([
            node("e1", "heading", "Sign in", level=1),
            {"ref": None, "role": "text", "name": "Cookies are used here.", "depth": 0},
        ], mode="text")
        self.assertIn("Cookies are used here.", text)
        self.assertNotIn("ref=", text)

    def test_a_diff_shows_only_what_moved_since_the_last_one(self):
        before = [node("e1", "textbox", "Email"), node("e2", "button", "Continue")]
        previous = {n["ref"]: a11y.signature(n) for n in before}
        filled = [node("e1", "textbox", "Email", value="me@example.com"),
                  node("e2", "button", "Continue")]
        text = a11y.render(filled, mode="diff", previous=previous)
        self.assertIn("1 changed", text)
        self.assertIn('+ textbox "Email" value="me@example.com" [ref=e1]', text)
        self.assertNotIn("[ref=e2]", text, "a button that did not move is not worth a line")

    def test_a_snapshot_that_does_not_fit_ends_in_the_outline_not_mid_word(self):
        nodes = [node(f"e{i}", "link", "a link with a fairly long name", 0) for i in range(200)]
        nodes.append(node("e999", "main", "Body", 0, sel="main"))
        text = a11y.render(nodes, max_chars=600)
        self.assertIn("[cut at 600 characters", text)
        self.assertIn('main "Body"', text)
        self.assertLessEqual(len(text), 900)

    def test_frames_are_pointed_at_rather_than_silently_skipped(self):
        text = a11y.render([node("e1", "button", "Go")], frames=["https://example.com/frame"])
        self.assertIn("[frame 1] https://example.com/frame", text)
        self.assertIn("latchkey_frame_text", text)

    def test_an_empty_page_says_so_instead_of_returning_nothing(self):
        self.assertIn("nothing to name", a11y.render([]))

    def test_a_mode_that_does_not_exist_is_an_error_not_a_silent_default(self):
        with self.assertRaises(ValueError):
            a11y.render([], mode="summary")


class TestWhatARefPointsAt(unittest.TestCase):
    def setUp(self):
        self.refs = a11y.Refs()
        self.refs.note("https://example.com/login", [
            {"ref": "e3", "role": "button", "name": "Sign in", "sel": "form > button"}])

    class StubPage:
        def __init__(self, present):
            self.present = set(present)
            self.asked = []

        def locator(self, selector):
            self.asked.append(selector)
            return self

        def count(self):
            return 1 if self.asked[-1] in self.present else 0

    class Stub:
        """The real rule, on a page that only knows which selectors are on it."""

        ref_selector = driver.Driver.ref_selector
        _matches = driver.Driver._matches

        def __init__(self, page, refs):
            self.page = page
            self.refs = refs

    def resolve(self, present, ref="e3"):
        page = self.StubPage(present)
        return driver.Driver.ref_selector(self.Stub(page, self.refs), ref), page

    def test_the_live_tag_on_the_element_is_what_gets_used(self):
        live = self.refs.selector("e3")
        selector, page = self.resolve({live, "form > button"})
        self.assertEqual(selector, live)
        self.assertEqual(len(page.asked), 1, "no second look when the first one matched")

    def test_a_rebuilt_element_falls_back_to_the_path_it_was_read_at(self):
        selector, page = self.resolve({"form > button"})
        self.assertEqual(selector, "form > button")

    def test_a_ref_that_is_gone_fails_saying_what_it_was(self):
        with self.assertRaises(driver.RefError) as caught:
            self.resolve(set())
        message = str(caught.exception)
        self.assertIn("ref e3", message)
        self.assertIn("button", message)
        self.assertIn("Sign in", message)
        self.assertIn("latchkey_snapshot", message)

    def test_a_ref_from_nowhere_is_left_to_playwright_to_reject(self):
        selector, _page = self.resolve(set(), ref="e99")
        self.assertEqual(selector, self.refs.selector("e99"))

    def test_the_ref_attribute_is_this_session_s_own_and_not_a_brand(self):
        """A fixed attribute on live elements is a one-selector tell for automation."""
        mine, theirs = a11y.Refs(), a11y.Refs()
        self.assertNotEqual(mine.attr, theirs.attr)
        self.assertNotIn("latchkey", mine.attr)
        self.assertNotIn("latchkey", mine.seq_key)
        self.assertEqual(mine.ref_of(mine.selector("e3")), "e3")
        self.assertIsNone(mine.ref_of(theirs.selector("e3")))
        self.assertIsNone(mine.ref_of("#login"))

    def test_what_a_ref_was_is_remembered_per_session(self):
        self.assertEqual(self.refs.describe("e3"), "ref e3 was button Sign in")
        self.assertIn("not from a snapshot", self.refs.describe("e42"))
        self.assertEqual(self.refs.fallback("e3"), "form > button")


class TestActionsCanNameWhatTheSnapshotRead(unittest.TestCase):
    class Browser:
        def __init__(self, refs):
            self.refs = refs
            self.asked = []

        def ref_selector(self, ref):
            self.asked.append(ref)
            return f'[data-latchkey-ref="{ref}"]'

    def test_a_ref_is_resolved_and_a_selector_is_left_alone(self):
        browser = self.Browser(a11y.Refs())
        self.assertEqual(automation._target(browser, {"ref": "e7"}), '[data-latchkey-ref="e7"]')
        self.assertEqual(browser.asked, ["e7"])
        self.assertEqual(automation._target(browser, {"selector": "#go"}), "#go")

    def test_every_verb_that_takes_a_selector_can_take_a_ref(self):
        """The verbs are the agent's hands: a ref that only half of them accept is worse
        than no refs at all, so no runner may read `selector` for itself."""
        source = (__import__("pathlib").Path(__file__).resolve().parent.parent
                  / "latchkey/automation.py").read_text(encoding="utf-8")
        self.assertNotIn('a["selector"]', source)
        for verb in ("click", "hover", "fill", "press", "select", "check", "upload",
                     "wait_for", "screenshot"):
            self.assertIn(f"_run_{verb}(browser, a)", source)
        self.assertEqual(source.count("_target(browser, a)"), 10,
                         "every anchor: seven required, two optional, one on the scroll")


class TestFindSaysNothingRatherThanSayingTheWrongThing(unittest.TestCase):
    """Measured on a real page: searching a signed-in github.com for "sign in" used to
    return eight matches, all of them wrong, because the role went into the haystack as
    a substring - "in" is inside link, main, banner and contentinfo - and because a
    fraction of the query counted as a hit. A model handed a plausible ref clicks it."""

    def find(self, nodes, query, limit=8):
        class Browser:
            refs = a11y.Refs()
            page = type("Page", (), {"url": "https://example.com/"})()
        real = a11y.nodes
        a11y.nodes = lambda *a, **k: {"nodes": nodes, "frames": []}
        try:
            return a11y.find(Browser(), query, limit)
        finally:
            a11y.nodes = real

    PAGE = [
        node("e1", "link", "Skip to content"),
        node("e2", "banner", ""),
        node("e3", "main", ""),
        node("e4", "contentinfo", ""),
        node("e5", "link", "tests : drop SYCL special-casing in test-backend-ops.cpp #28688"),
        node("e6", "button", "Pull requests"),
        node("e7", "button", "Open quick search dialog, type / to search"),
    ]

    def test_a_word_inside_another_word_is_not_a_match(self):
        out = self.find(self.PAGE, "sign in")
        self.assertEqual(out["found"], 0, out["matches"])
        self.assertIn("not a ranking problem", out["next_step"])

    def test_what_is_actually_there_is_still_found_and_ranked(self):
        out = self.find(self.PAGE, "pull requests")
        self.assertEqual(out["matches"][0]["ref"], "e6")
        out = self.find(self.PAGE, "search")
        self.assertEqual(out["matches"][0]["ref"], "e7")

    def test_the_actionable_thing_beats_the_heading_that_says_the_same(self):
        out = self.find([node("e1", "heading", "Sign in"),
                         node("e2", "button", "Sign in with Google")], "sign in")
        self.assertEqual(out["matches"][0]["ref"], "e2")

    def test_one_word_of_a_query_buried_in_a_paragraph_is_not_a_match(self):
        page = [node("e1", "text", "This domain is for use in documentation examples "
                                   "without needing permission to ask anybody first.")]
        self.assertEqual(self.find(page, "sign in")["found"], 0)

    def test_a_query_that_is_only_stopwords_still_searches_for_them(self):
        out = self.find([node("e1", "link", "In")], "in")
        self.assertEqual(out["found"], 1)

    def test_a_query_with_nothing_in_it_asks_for_something_to_match_on(self):
        out = self.find(self.PAGE, "  ?  ")
        self.assertEqual(out["found"], 0)
        self.assertIn("give find something to match on", out["next_step"])
