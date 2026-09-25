"""A credential must not leave the page, whichever way the field was named.

`detect.secretish` reads the *selector*, which works for `input[type=password]` and says
nothing at all about `[data-3f2a="e5"]` - and acting by ref is the flow every tool
description recommends. So the answer has to be decided on the element, by the walker,
and carried on the ref. These are the three places a value escaped through: the event the
fill publishes, the snapshot an agent reads back, and `find`'s matches.
"""
import unittest

from latchkey import a11y, automation, detect, driver, events


PASSWORD = "correct-horse-battery-staple"


def secret_node(ref="e5", **extra):
    return {"ref": ref, "role": "textbox", "name": "Password", "depth": 1,
            "secret": True, **extra}


class Harness(driver.Driver, automation.AutomationMixin):
    """Just enough browser: a locator that records, and a page that answers."""

    def __init__(self, secret_ref="e5"):
        super().__init__()
        self.refs = a11y.Refs()
        self.refs.note("https://example.com/login", [secret_node(secret_ref)])
        self.published: list[events.Event] = []
        self.filled: list[tuple[str, str]] = []
        self.spec = None

    # -- the parts the verb touches
    class _Locator:
        def __init__(self, outer, selector):
            self.outer, self.selector = outer, selector

        @property
        def first(self):
            return self

        def fill(self, value, timeout=None):
            self.outer.filled.append((self.selector, value))

    class _Page:
        url = "https://example.com/login"

        def locator(self, selector):
            raise AssertionError("unused")

        def wait_for_load_state(self, *a, **k):
            pass

        def wait_for_timeout(self, *a, **k):
            pass

    def locator(self, selector):
        return self._Locator(self, selector)

    def locator_in(self, selector, frame=None):
        return self._Locator(self, selector)

    @property
    def page(self):
        page = self._Page()
        page.locator = self.locator
        return page

    @property
    def humanize(self):
        return False

    def _approach(self, selector=None, click=False, frame=None):
        return {}

    def settle(self, cap_ms):
        return 0.0

    def state(self, text_limit=4000):
        return detect.PageState(url=self.page.url, title="Sign in")

    def _publish(self, kind, **detail):
        event = events.Event(1, "test", kind, 0.0, dict(detail))
        self.published.append(event)
        return event


class TestFillDoesNotPublishACredential(unittest.TestCase):
    def test_a_css_selector_that_says_password_is_redacted(self):
        harness = Harness()
        harness.fill("input[type=password]", PASSWORD)
        self.assertEqual(harness.published[-1].detail["value"], "(hidden)")

    def test_a_ref_to_a_password_field_is_redacted_too(self):
        """The regression: a ref selector carries no type, so the old check let it past."""
        harness = Harness()
        selector = harness.refs.selector("e5")
        self.assertFalse(detect.secretish(selector),
                         "the selector itself cannot say this - that is the whole point")
        harness.fill(selector, PASSWORD)
        self.assertEqual(harness.published[-1].detail["value"], "(hidden)")
        self.assertNotIn(PASSWORD, str(harness.published[-1].as_dict()))
        self.assertEqual(harness.filled, [(selector, PASSWORD)],
                         "the field is still filled; only the report is redacted")

    def test_an_ordinary_field_is_still_reported(self):
        harness = Harness()
        harness.refs.note("https://example.com/", [
            {"ref": "e9", "role": "textbox", "name": "Search"}])
        harness.fill(harness.refs.selector("e9"), "widgets")
        self.assertEqual(harness.published[-1].detail["value"], "widgets")

    def test_a_ref_from_another_session_is_not_treated_as_ours(self):
        harness = Harness()
        other = a11y.Refs()
        self.assertTrue(detect.secretish(harness.secret_target("#password") and "#password"))
        self.assertFalse(harness.secret_target(other.selector("e5")))


class TestTheSnapshotNeverCarriesTheValue(unittest.TestCase):
    def test_a_filled_password_renders_as_filled_not_as_itself(self):
        text = a11y.render([secret_node("e5", filled=len(PASSWORD), required=True)],
                           url="https://example.com/login")
        self.assertNotIn(PASSWORD, text)
        self.assertIn("filled (28 chars, hidden)", text)
        self.assertIn("[ref=e5]", text)

    def test_an_empty_password_field_still_reads_as_one(self):
        text = a11y.render([secret_node("e5")], url="https://example.com/login")
        self.assertIn("secret empty", text)

    def test_diff_mode_notices_a_password_being_typed_without_showing_it(self):
        before = {"e5": a11y.signature(secret_node("e5"))}
        after = [secret_node("e5", filled=len(PASSWORD))]
        text = a11y.render(after, mode="diff", previous=before)
        self.assertIn("1 changed", text)
        self.assertNotIn(PASSWORD, text)


class TestFindDoesNotCarryItEither(unittest.TestCase):
    class Browser:
        def __init__(self, nodes):
            self.refs = a11y.Refs()
            self._nodes = nodes

        class _Page:
            url = "https://example.com/login"

            def evaluate(self, *_a, **_k):
                raise AssertionError("patched out")

        page = _Page()

    def test_a_match_on_a_password_field_reports_it_as_secret_only(self):
        browser = self.Browser(None)
        found = {"nodes": [secret_node("e5", value=PASSWORD)], "frames": []}
        original = a11y.nodes
        a11y.nodes = lambda *a, **k: found
        try:
            out = a11y.find(browser, "password")
        finally:
            a11y.nodes = original
        self.assertEqual(out["matches"][0]["ref"], "e5")
        self.assertTrue(out["matches"][0]["secret"])
        self.assertNotIn("value", out["matches"][0])
        self.assertNotIn(PASSWORD, str(out))


class TestRefsForgetTheOldestRatherThanGrowForever(unittest.TestCase):
    def test_a_long_session_does_not_keep_every_ref_it_ever_minted(self):
        refs = a11y.Refs()
        refs.KEEP = 10
        for batch in range(5):
            refs.note("https://example.com/", [
                {"ref": f"e{batch * 10 + i}", "role": "button", "name": "x"}
                for i in range(10)])
        self.assertEqual(len(refs.by_ref), 10)
        self.assertIsNotNone(refs.known("e49"), "the newest are kept")
        self.assertIsNone(refs.known("e0"), "the oldest are forgotten")


if __name__ == "__main__":
    unittest.main()
