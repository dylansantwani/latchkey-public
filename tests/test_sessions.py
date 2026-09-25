"""Offline tests for the parts an agent runs on: sessions, events, the probe.

No browser, no network, no Keychain - the page is a fake that records what was
asked of it, which is how the cheap-path promises (one page read per state, cursor
coordinates only while someone is watching, no secret in an event) get checked.

    cd ~/tools/latchkey
    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import time
import unittest
import inspect

import latchkey.automation as automation
import latchkey.driver as driver
import latchkey.mcp_server as mcp_server
import latchkey.navigation as navigation
import latchkey.viewer as viewer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import detect, events  # noqa: E402
from latchkey import cookies as ck  # noqa: E402
from latchkey.driver import Driver  # noqa: E402
from latchkey.events import bus  # noqa: E402
from latchkey.frames import FramesMixin  # noqa: E402
from latchkey.navigation import NavigationMixin  # noqa: E402
from latchkey.policy import WRITE_VERBS, ReadOnlyError  # noqa: E402
from latchkey.automation import ActionFailed, AutomationMixin, apply_actions  # noqa: E402
from latchkey.session import Browser, SessionSpec  # noqa: E402
from latchkey.sessions import Session, SessionError, SessionRegistry  # noqa: E402
from latchkey.tabs import TabsMixin  # noqa: E402

BOX = {"x": 100.0, "y": 200.0, "width": 40.0, "height": 20.0}


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def bounding_box(self, timeout=None):
        self.page.boxed.append(self.selector)
        return self.page.boxes.get(self.selector, dict(BOX))

    def _record(self, verb, **extra):
        self.page.actions.append((verb, self.selector, extra))

    def click(self, timeout=None, force=False):
        self._record("click", force=force)

    def hover(self, timeout=None, force=False):
        self._record("hover", force=force)

    def fill(self, value, timeout=None):
        self._record("fill", value=value)

    def press(self, key, timeout=None):
        self._record("press", key=key)

    def select_option(self, value, timeout=None):
        self._record("select", value=value)

    def set_checked(self, on, timeout=None):
        self._record("check", on=on)

    def set_input_files(self, path, timeout=None):
        self._record("upload", path=path)

    def scroll_into_view_if_needed(self, timeout=None):
        self._record("scroll_into_view")

    def wait_for(self, timeout=None):
        self._record("wait_for")

    def screenshot(self, path=None, full_page=False, timeout=None):
        self.page.shots.append((path, full_page))


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def wheel(self, dx, dy):
        self.page.actions.append(("wheel", None, {"dy": dy}))

    def move(self, x, y, steps=1):
        self.page.actions.append(("move", None, {"x": x, "y": y, "steps": steps}))

    def click(self, x, y):
        self.page.actions.append(("click_at", None, {"x": x, "y": y}))


class FakePage:
    """Records every call, and answers the probe like a plain signed-in page."""

    def __init__(self, url="https://example.com/", probe=None, readable=True):
        self.url = url
        self.name = ""
        self.mouse = FakeMouse(self)
        self.probe = probe or {"title": "Example", "body": "hello there",
                               "login_prompt": None, "logged_in_marker": "avatar",
                               "password_field": False, "blocked": None}
        self.readable = readable
        self.probes = 0
        self.evaluates = []
        self.actions = []
        self.boxed = []
        self.boxes = {}
        self.shots = []
        self.waited = []
        self.closed = False
        self.on_close = None
        self.main_frame = None

    # playwright-ish surface -------------------------------------------------
    def evaluate(self, js, arg=None):
        if isinstance(arg, dict) and "markers" in arg:
            self.probes += 1
            if not self.readable:
                raise RuntimeError("Execution context was destroyed")
            self.cfg = arg
            return dict(self.probe)
        self.evaluates.append(js)
        return None

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_timeout(self, ms):
        self.waited.append(ms)

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def go_back(self, **kwargs):
        self.url = "https://example.com/back"

    def go_forward(self, **kwargs):
        self.url = "https://example.com/forward"

    def reload(self, **kwargs):
        pass

    def title(self):
        return self.probe["title"]

    def close(self):
        self.closed = True
        if self.on_close:
            self.on_close()

    def screenshot(self, path=None, full_page=False, timeout=None):
        self.shots.append((path, full_page))


class FakeContext:
    def __init__(self):
        self.scripts = []
        self.pages = []

    def add_init_script(self, script=None, path=None):
        self.scripts.append(script)

    def new_page(self):
        page = FakePage()
        page.main_frame = page
        page.on_close = lambda: self.pages.remove(page) if page in self.pages else None
        self.pages.append(page)
        return page


class FakeFrame:
    def __init__(self, url, text=""):
        self.url = url
        self.name = ""
        self._text = text
        self.actions = []
        self.boxed = []
        self.boxes = {}
        self.shots = []

    def title(self):
        return "frame title"

    def evaluate(self, js, arg=None):
        return {"body": self._text} if "document.body" in js else self._text

    def locator(self, selector):
        return FakeLocator(self, selector)


class Harness(Driver, NavigationMixin, AutomationMixin, FramesMixin, TabsMixin):
    """A Browser's verbs with a fake page - no Chrome, no launch cost."""

    def __init__(self, page=None, label="test"):
        super().__init__()
        self._page = page or FakePage()
        self._ctx = FakeContext()
        self.label = label


class Recording:
    """Collects what a session publishes, and unsubscribes itself afterwards."""

    def __init__(self):
        self.events = []
        self._off = bus.subscribe(self.events.append)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._off()

    def kinds(self):
        return [e.kind for e in self.events]

    def one(self, kind):
        matches = [e for e in self.events if e.kind == kind]
        assert len(matches) == 1, f"expected one {kind} event, got {len(matches)}"
        return matches[0]


class TestEvents(unittest.TestCase):
    """Every action an agent takes is visible on the bus, because that is what a
    viewer renders and what a future live stream carries."""

    def test_goto_publishes_the_url_and_verdict(self):
        page = FakePage()
        with Recording() as rec:
            Harness(page).goto("https://example.com/x", settle_ms=0)
        event = rec.one("goto")
        self.assertEqual(event.session, "test")
        self.assertEqual(event.detail["url"], "https://example.com/x")
        self.assertEqual(event.detail["verdict"], "logged-in")

    def test_click_publishes_selector_and_force(self):
        with Recording() as rec:
            Harness().click("#go", settle_ms=0, force=True)
        event = rec.one("click")
        self.assertEqual(event.detail["selector"], "#go")
        self.assertTrue(event.detail["force"])

    def test_fill_reports_the_value(self):
        with Recording() as rec:
            Harness().fill("#search", "widgets")
        self.assertEqual(rec.one("fill").detail["value"], "widgets")

    def test_a_credential_field_never_reaches_the_event_log(self):
        """Events feed a viewer and, later, an SSE stream. A password does not
        belong on either."""
        with Recording() as rec:
            Harness().fill("input[type=password]", "hunter2")
        self.assertEqual(rec.one("fill").detail["value"], "(hidden)")

    def test_every_verb_publishes_something(self):
        page = FakePage()
        harness = Harness(page)
        with Recording() as rec:
            harness.hover("#a")
            harness.press("#a", "Enter")
            harness.select("#a", "x")
            harness.check("#a", True)
            harness.scroll(300)
            harness.upload("#a", "/tmp/f.pdf")
            harness.screenshot("/tmp/s.png")
            harness.back()
            harness.forward()
            harness.reload()
            harness.wait_for("#a")
            harness.evaluate("() => 1")
        self.assertEqual(rec.kinds(),
                         ["hover", "press", "select", "check", "scroll", "upload",
                          "screenshot", "back", "forward", "reload", "wait_for",
                          "eval"])

    def test_a_broken_subscriber_cannot_break_the_agent(self):
        def explode(event):
            raise RuntimeError("viewer blew up")

        off = bus.subscribe(explode)
        try:
            state = Harness().click("#go", settle_ms=0)
        finally:
            off()
        self.assertEqual(state.verdict, "logged-in")

    def test_events_are_isolated_per_label(self):
        """Two sessions publishing at once must not be confused for each other."""
        with Recording() as rec:
            Harness(label="canvas").click("#a", settle_ms=0)
            Harness(label="mail").click("#b", settle_ms=0)
        sessions = {e.session for e in rec.events}
        self.assertEqual(sessions, {"canvas", "mail"})

    def test_events_go_by_the_sessions_name_not_its_label(self):
        """A session named `offerscout` with the label "Offer Scout read-only audit" used to
        publish its goto/click events under the label, so the viewer's `offerscout` tab
        (which goes by name, as does every endpoint) showed no activity and no pointer."""
        harness = Harness(label="Offer Scout read-only audit - never submit")
        harness.session_name = "offerscout"           # what the registry sets
        with Recording() as rec:
            harness.click("#a", settle_ms=0)
        self.assertEqual({e.session for e in rec.events}, {"offerscout"})
        # ...and the registry does set it, on the browser it builds.
        import latchkey.sessions as sessions_mod
        made = []
        session = sessions_mod.Session("named", lambda: made.append(Harness()) or made[-1])
        try:
            self.assertEqual(made[0].session_name, "named")
        finally:
            session.close()


class TestCursor(unittest.TestCase):
    """The pointer is a real DOM node, so it is opt-in. Where it goes, though, is not
    opt-in: the real pointer walks to every element whether or not a viewer is attached,
    because the page is watching even when nobody else is."""

    def test_nobody_watching_means_no_coordinates_and_no_cursor_node(self):
        page = FakePage()
        state = Harness(page).click("#go", settle_ms=0)
        # The pointer moved (the page saw it) but nothing was drawn and no coordinates
        # were published: those exist for a viewer, and there is not one.
        self.assertEqual(page.boxed, ["#go"])
        self.assertEqual([js for js in page.evaluates if "__latchkeyCursor" in js], [])
        self.assertEqual(state.verdict, "logged-in")

    def test_machine_shaped_input_skips_the_pointer_walk_entirely(self):
        page = FakePage()
        harness = Harness(page)
        harness.spec = SessionSpec(humanize=False)
        harness.click("#go", settle_ms=0)
        self.assertEqual(page.boxed, [])
        self.assertEqual(page.evaluates, [])

    def test_a_subscriber_gets_coordinates(self):
        page = FakePage()
        with Recording() as rec:
            Harness(page).click("#go", settle_ms=0)
        self.assertEqual(page.boxed, ["#go"])
        event = rec.one("click")
        x, y = event.detail["x"], event.detail["y"]
        # Inside the element, not on its exact centre: a pointer that lands dead centre
        # of every element is one of the published behavioural tells.
        self.assertTrue(2 <= x <= 238 and 2 <= y <= 418, (x, y))

    def test_the_cursor_moves_on_a_click_when_it_is_switched_on(self):
        page = FakePage()
        harness = Harness(page)
        harness.attach_cursor()
        harness.click("#go", settle_ms=0)
        moves = [js for js in page.evaluates if "__latchkey_cursor" in js]
        self.assertTrue(any("(p) =>" in js for js in moves), page.evaluates)
        self.assertIn(events.CURSOR_INIT_SCRIPT, page.evaluates)
        # DOM only: nothing the cursor does leaves a global the page could find.
        for js in moves:
            self.assertNotIn("window.", js)

    def test_attach_installs_it_for_future_navigations_too(self):
        page = FakePage()
        harness = Harness(page)
        self.assertTrue(harness.attach_cursor())
        self.assertIn(events.CURSOR_INIT_SCRIPT, harness._ctx.scripts)

    def test_detach_removes_the_node(self):
        page = FakePage()
        harness = Harness(page)
        harness.attach_cursor()
        self.assertTrue(harness.detach_cursor())
        self.assertFalse(harness.watching)
        self.assertTrue(any("remove()" in js for js in page.evaluates))

    def test_a_page_that_refuses_the_injection_is_not_fatal(self):
        page = FakePage()

        def explode(js, arg=None):
            raise RuntimeError("no document")

        page.evaluate = explode
        self.assertFalse(Harness(page).attach_cursor())


class TestProbe(unittest.TestCase):
    """state() runs after every action, so it has to be cheap and it must not
    serve a stale answer."""

    def test_one_page_read_per_state(self):
        page = FakePage()
        harness = Harness(page)
        harness.state()
        self.assertEqual(page.probes, 1)

    def test_reading_an_unchanged_page_twice_costs_nothing(self):
        page = FakePage()
        harness = Harness(page)
        harness.state()
        harness.state()
        harness.state()
        self.assertEqual(page.probes, 1)

    def test_an_action_invalidates_the_cached_read(self):
        page = FakePage()
        harness = Harness(page)
        harness.state()
        harness.click("#go", settle_ms=0)
        self.assertEqual(page.probes, 2)

    def test_a_navigation_invalidates_the_cached_read(self):
        page = FakePage()
        harness = Harness(page)
        harness.state()
        harness.goto("https://example.com/other", settle_ms=0)
        self.assertEqual(page.probes, 2)

    def test_an_unreadable_page_is_reported_not_raised_and_not_cached(self):
        page = FakePage(readable=False)
        harness = Harness(page)
        state = harness.state()
        self.assertEqual(state.verdict, "unclear")
        page.readable = True
        self.assertEqual(harness.state().verdict, "logged-in")

    def test_the_probe_receives_the_hints_for_the_site(self):
        page = FakePage(url="https://chatgpt.com/")
        Harness(page).state()
        self.assertIn("[data-testid='create-new-chat-button']", page.cfg["markers"])
        self.assertIn("sign in", page.cfg["login"])
        self.assertIn("just a moment", page.cfg["block"])

    def test_an_unknown_site_gets_only_the_generic_markers(self):
        page = FakePage(url="https://nothing.example.org/")
        Harness(page).state()
        for marker in page.cfg["markers"]:
            self.assertIn(marker, detect.GENERIC_MARKERS)

    def test_text_limit_is_applied_without_another_read(self):
        page = FakePage()
        page.probe["body"] = "x" * 500
        harness = Harness(page)
        self.assertEqual(len(harness.state(text_limit=10).text), 10)
        self.assertEqual(page.probes, 1)

    def test_the_verdict_still_reads_plainly(self):
        """A challenge and a refusal are two answers, and both read plainly.

        "Just a moment..." is Cloudflare asking a question - passable, worth waiting out.
        A page that says you have been blocked is not. They used to share one word
        (`blocked`), which is how an agent ends up waiting on a refusal, or writing off a
        page that was about to let it in.
        """
        challenge = FakePage(probe={"title": "Just a moment...", "body": "",
                                   "login_prompt": None, "logged_in_marker": None,
                                   "password_field": False, "blocked": "just a moment"})
        self.assertEqual(Harness(challenge).state().verdict, "challenged")
        refusal = FakePage(probe={"title": "Access denied", "body": "",
                                 "login_prompt": None, "logged_in_marker": None,
                                 "password_field": False,
                                 "blocked": "you have been blocked"})
        self.assertEqual(Harness(refusal).state().verdict, "blocked")


class TestHostCookieCrossCheck(unittest.TestCase):
    """A page that merely *looks* signed in is not a signed-in session.

    Regression from the site sweep: instagram.com, steamcommunity.com and
    stackoverflow.com all read `logged-in` with zero cookies loaded, because public
    pages are full of profile-shaped markup. A marker with no cookie behind it is
    `unclear`, which is the honest answer and stops an agent assuming it is in.
    """

    def test_a_marker_without_a_cookie_behind_it_is_not_logged_in(self):
        page = FakePage(url="https://instagram.com/")
        harness = Harness(page)
        harness._cookie_hosts = {"github.com"}
        state = harness.state()
        self.assertEqual(state.host_cookies, 0)
        self.assertEqual(state.verdict, "unclear")
        self.assertEqual(state.logged_in_marker, "avatar")

    def test_a_marker_without_a_cookie_but_with_a_sign_in_button_is_logged_out(self):
        """webassign.net's landing page: a "SIGN IN" button *and* an image whose alt text
        reads like a user avatar. `unclear` there would hide the fact that the page is
        plainly asking to be signed in to."""
        page = FakePage(url="https://www.webassign.net/")
        page.probe["login_prompt"] = "SIGN IN"
        page.probe["logged_in_marker"] = "Two females and one male profile picture"
        harness = Harness(page)
        harness._cookie_hosts = set()
        self.assertEqual(harness.state().verdict, "logged-out")

    def test_the_same_page_with_cookies_is_logged_in(self):
        page = FakePage(url="https://www.webassign.net/")
        page.probe["login_prompt"] = "SIGN IN"
        page.probe["logged_in_marker"] = "profile picture"
        harness = Harness(page)
        harness._cookie_hosts = {"webassign.net"}
        state = harness.state()
        self.assertEqual(state.host_cookies, 1)
        self.assertEqual(state.verdict, "logged-in")

    def test_a_marker_with_a_cookie_behind_it_is_logged_in(self):
        page = FakePage(url="https://www.instagram.com/")
        harness = Harness(page)
        harness._cookie_hosts = {"instagram.com"}
        self.assertEqual(harness.state().host_cookies, 1)
        self.assertEqual(harness.state().verdict, "logged-in")

    def test_a_parent_domain_cookie_counts_for_a_subdomain(self):
        page = FakePage(url="https://api.example.com/")
        harness = Harness(page)
        harness._cookie_hosts = {"example.com"}
        self.assertEqual(harness.state().host_cookies, 1)

    def test_a_sibling_domain_cookie_does_not_count(self):
        page = FakePage(url="https://evil-example.com/")
        harness = Harness(page)
        harness._cookie_hosts = {"example.com"}
        self.assertEqual(harness.state().host_cookies, 0)

    def test_an_unknown_jar_leaves_the_verdict_alone(self):
        """When the jar cannot be read at all - no context to ask, or a browser that
        will not answer - the verdict has to stand on the page alone rather than call a
        signed-in site signed out. A cloned profile that *can* be asked is asked: see
        TestCloneJarCrossCheck."""
        page = FakePage(url="https://instagram.com/")
        harness = Harness(page)
        self.assertIsNone(harness.state().host_cookies)
        self.assertEqual(harness.state().verdict, "logged-in")

    def test_the_count_is_reported_to_the_agent(self):
        page = FakePage(url="https://www.instagram.com/")
        harness = Harness(page)
        harness._cookie_hosts = {"instagram.com"}
        self.assertEqual(harness.state().as_dict()["host_cookies"], 1)


class JarFakeContext(FakeContext):
    """A context that can be asked what it holds, like a cloned profile's.

    `cookies([url])` is the one question Chrome will answer about a profile latchkey
    never injected into, and counting the calls is how the tests prove it is asked once
    per navigation rather than once per read.
    """

    def __init__(self, held=0, refuses=False):
        super().__init__()
        self.held = held
        self.refuses = refuses
        self.asked = []

    def cookies(self, urls=None):
        self.asked.append(tuple(urls or ()))
        if self.refuses:
            raise RuntimeError("Target page, context or browser has been closed")
        return [{"name": f"cookie-{i}"} for i in range(self.held)]


class TestCloneJarCrossCheck(unittest.TestCase):
    """A clone's jar is knowable, and that is what closes the site-sweep hole.

    instagram.com, steamcommunity.com and stackoverflow.com all read `logged-in` on a
    cloned, signed-out session, because a clone never passes through our injection and
    the verdict fell back to the page alone. The fix written down for it - ask Chrome
    once per navigation and cache until the next one - is what these hold in place. The
    question is not free, so it must not be asked per read, and must not be asked at all
    when injection already answered it.
    """

    def setUp(self):
        self.page = FakePage(url="https://www.instagram.com/")
        self.harness = Harness(self.page)
        self.ctx = JarFakeContext(held=0)
        self.harness._ctx = self.ctx

    def test_a_clone_that_holds_nothing_for_the_site_is_not_logged_in(self):
        state = self.harness.state()
        self.assertEqual(state.host_cookies, 0)
        self.assertEqual(state.verdict, "unclear")

    def test_a_clone_that_holds_cookies_reads_logged_in(self):
        self.ctx.held = 4
        state = self.harness.state()
        self.assertEqual(state.host_cookies, 4)
        self.assertEqual(state.verdict, "logged-in")

    def test_the_jar_is_asked_once_per_navigation_not_once_per_read(self):
        for _ in range(5):
            self.harness.state()
        self.assertEqual(len(self.ctx.asked), 1,
                         "reading a page again must not ask the browser again")
        self.harness.goto("https://www.instagram.com/explore/")
        self.assertEqual(len(self.ctx.asked), 2, "a navigation is worth asking again")
        self.harness.state()
        self.assertEqual(len(self.ctx.asked), 2)

    def test_going_somewhere_again_is_worth_asking_about_again(self):
        """A login handoff is exactly this: the page reloads and the jar has changed."""
        self.harness.state()
        self.ctx.held = 3
        self.harness.reload()
        self.assertEqual(self.harness.state().host_cookies, 3)

    def test_a_jar_that_cannot_be_read_leaves_the_verdict_alone(self):
        self.ctx.refuses = True
        state = self.harness.state()
        self.assertIsNone(state.host_cookies)
        self.assertEqual(state.verdict, "logged-in")

    def test_a_page_with_no_host_is_never_counted_against_the_page(self):
        harness = Harness(FakePage(url="about:blank"))
        harness._ctx = self.ctx
        self.assertIsNone(harness.state().host_cookies)
        self.assertEqual(self.ctx.asked, [], "there is no host to ask about")

    def test_an_injected_session_is_still_never_asked_of_the_browser(self):
        """Injection already knows the domains, so the free path stays free."""
        self.harness._cookie_hosts = {"instagram.com"}
        self.assertEqual(self.harness.state().host_cookies, 1)
        self.assertEqual(self.ctx.asked, [])


class TestFrames(unittest.TestCase):
    def test_frames_are_listed_with_their_index(self):
        page = FakePage()
        page.main_frame = page
        page.frames = [page, FakeFrame("https://docs.google.com/x")]
        harness = Harness(page)
        listed = harness.frames()
        self.assertEqual([f["index"] for f in listed], [0, 1])
        self.assertTrue(listed[0]["is_main"])

    def test_reading_a_missing_frame_says_which(self):
        page = FakePage()
        page.main_frame = page
        page.frames = [page]
        with self.assertRaises(IndexError):
            Harness(page).frame_text(3)

    def test_click_targets_a_cross_origin_frame_by_listed_index(self):
        page = FakePage()
        page.main_frame = page
        frame = FakeFrame("https://challenges.cloudflare.com/widget")
        page.frames = [page, frame]
        harness = Harness(page)
        harness.click("#checkbox", settle_ms=0, frame=1)
        self.assertEqual(frame.actions, [("click", "#checkbox", {"force": False})])
        self.assertFalse(any(action[1] == "#checkbox" for action in page.actions))

    def test_a_frame_index_string_is_tolerated_but_a_missing_frame_is_clear(self):
        page = FakePage()
        page.main_frame = page
        frame = FakeFrame("https://example.net/embedded")
        page.frames = [page, frame]
        self.assertIs(Harness(page).frame_at("1"), frame)
        with self.assertRaisesRegex(IndexError, "no frame 2; page has 2"):
            Harness(page).frame_at(2)


class TestTabs(unittest.TestCase):
    def test_new_tab_is_published_and_active(self):
        page = FakePage()
        page.main_frame = page
        ctx = FakeContext()
        ctx.pages = [page]
        harness = Harness(page)
        harness._ctx = ctx
        with Recording() as rec:
            harness.new_page()
        self.assertEqual(rec.one("tab").detail["action"], "new")
        self.assertIs(harness.page, ctx.pages[-1])

    def test_pages_marks_the_active_tab(self):
        page = FakePage()
        page.main_frame = page
        ctx = FakeContext()
        ctx.pages = [page, FakePage(url="https://example.com/two")]
        harness = Harness(page)
        harness._ctx = ctx
        active = [p for p in harness.pages() if p["active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["index"], 0)

    def test_switching_pages_republishes_and_reactivates(self):
        page = FakePage()
        page.main_frame = page
        other = FakePage(url="https://example.com/two")
        other.main_frame = other
        ctx = FakeContext()
        ctx.pages = [page, other]
        harness = Harness(page)
        harness._ctx = ctx
        with Recording() as rec:
            state = harness.switch(1)
        self.assertEqual(state.url, "https://example.com/two")
        self.assertEqual(rec.one("tab").detail["target"], 1)

    def open_three(self):
        """A harness with three tabs, ids t1..t3, driving the last one."""
        ctx = FakeContext()
        first = ctx.new_page()
        second = ctx.new_page()
        third = ctx.new_page()
        for page in ctx.pages:
            page.url = f"https://example.com/{page is first and 'one' or page is second and 'two' or 'three'}"
        harness = Harness(first)
        harness._ctx = ctx
        harness._page = third
        return harness, ctx, [first, second, third]

    def test_tab_ids_are_handed_out_once_and_stay_put(self):
        harness, ctx, pages = self.open_three()
        self.assertEqual([harness.tab_id(p) for p in pages], ["t1", "t2", "t3"])
        self.assertEqual(harness.tab_id(pages[0]), "t1")     # asked again, same answer

    def test_an_id_survives_the_index_shifting(self):
        """The whole reason ids exist: closing the first tab moves every index, so
        an agent holding an index would silently start driving another tab."""
        harness, ctx, pages = self.open_three()
        harness.close_page("t1")
        listed = harness.pages()
        self.assertEqual([row["id"] for row in listed], ["t2", "t3"])
        self.assertEqual([row["index"] for row in listed], [0, 1])

    def test_switch_takes_an_id_or_an_index(self):
        harness, ctx, pages = self.open_three()
        self.assertEqual(harness.switch("t1").url, "https://example.com/one")
        self.assertEqual(harness.switch(2).url, "https://example.com/three")
        self.assertEqual(harness.switch("t2").url, "https://example.com/two")

    def test_a_tab_that_is_not_there_says_what_is(self):
        harness, ctx, pages = self.open_three()
        with self.assertRaises(KeyError) as caught:
            harness.switch("t9")
        self.assertIn("t1", str(caught.exception))
        with self.assertRaises(IndexError):
            harness.switch(7)
        with self.assertRaises(ValueError):
            harness.switch(None)

    def test_state_says_which_tab_it_read(self):
        """With several tabs open, "the page" has to be unambiguous."""
        harness, ctx, pages = self.open_three()
        harness.switch("t2")
        state = harness.state()
        self.assertEqual(state.tab, "t2")
        self.assertEqual(state.tabs, 3)
        self.assertEqual(state.as_dict()["tab"], "t2")

    def test_closing_the_active_tab_moves_to_another_one(self):
        harness, ctx, pages = self.open_three()
        with Recording() as rec:
            result = harness.close_page("t3")
        self.assertEqual(result["closed"], "t3")
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["tab"], "t2")
        self.assertIs(harness.page, pages[1])
        self.assertEqual(rec.one("tab").detail["action"], "close")

    def test_closing_the_last_tab_leaves_the_session_usable(self):
        ctx = FakeContext()
        only = ctx.new_page()
        harness = Harness(only)
        harness._ctx = ctx
        result = harness.close_page()
        self.assertEqual(result["pages"], 1)
        self.assertIsNot(harness.page, only)
        self.assertEqual(harness.state().verdict, "logged-in")


class TestActions(unittest.TestCase):
    """The action-list layer is what the MCP tool drives."""

    class Fake:
        def __init__(self):
            self.calls = []
            self.page = type("P", (), {"wait_for_timeout": lambda *a: None})()

        def hover(self, selector, settle_ms=500, force=False):
            self.calls.append(("hover", selector, force))

        def click(self, selector, settle_ms=2000, force=False):
            self.calls.append(("click", selector, force))

        def click_at(self, x, y, settle_ms=2000):
            self.calls.append(("click_at", x, y))

        def fill(self, selector, value, settle_ms=300):
            self.calls.append(("fill", selector, value))

        def refresh(self):
            self.calls.append(("refresh",))

        def screenshot(self, path, full_page=False, selector=None):
            self.calls.append(("screenshot", path, selector))

    def test_force_is_passed_through_to_click(self):
        """Canvas's welcome-tour overlay makes ordinary clicks time out; force is
        the escape hatch, so it must survive the action layer."""
        fake = self.Fake()
        apply_actions(fake, [{"do": "click", "selector": "#a", "force": True}])
        self.assertEqual(fake.calls, [("click", "#a", True)])

    def test_frame_is_passed_through_and_coordinates_have_their_own_verb(self):
        class FrameFake(self.Fake):
            def click(inner, selector, settle_ms=2000, force=False, frame=None):
                inner.calls.append(("click", selector, force, frame))

        fake = FrameFake()
        apply_actions(fake, [{"do": "click", "frame": 1, "selector": "#checkbox"},
                             {"do": "click_at", "x": 42, "y": 73}])
        self.assertEqual(fake.calls, [("click", "#checkbox", False, 1),
                                      ("click_at", 42, 73)])

    def test_a_main_page_ref_cannot_be_misapplied_inside_a_frame(self):
        with self.assertRaisesRegex(ActionFailed, "frame actions take a selector"):
            apply_actions(self.Fake(), [{"do": "click", "frame": 1, "ref": "e7"}])

    def test_force_is_passed_through_to_hover_too(self):
        """GitHub's fixed loading bar covers the page and swallows pointer events, so
        a hover without force waits 15s and then looks like a missing element."""
        fake = self.Fake()
        apply_actions(fake, [{"do": "hover", "selector": "#a", "force": True}])
        self.assertEqual(fake.calls, [("hover", "#a", True)])

    def test_fill_and_sync_dispatch(self):
        fake = self.Fake()
        apply_actions(fake, [{"do": "fill", "selector": "#q", "value": "x"},
                             {"do": "sync"}])
        self.assertEqual(fake.calls, [("fill", "#q", "x"), ("refresh",)])

    def test_screenshot_can_target_an_element(self):
        fake = self.Fake()
        apply_actions(fake, [{"do": "screenshot", "path": "/tmp/a.png",
                              "selector": "#doc"}])
        self.assertEqual(fake.calls, [("screenshot", "/tmp/a.png", "#doc")])

    def test_unknown_verb_raises_rather_than_silently_doing_nothing(self):
        with self.assertRaises(ValueError):
            apply_actions(self.Fake(), [{"do": "teleport"}])

    class Failing:
        """A click that times out, the way an overlay swallowing the pointer does."""

        def __init__(self):
            self.calls = []
            self.page = type("P", (), {
                "wait_for_timeout": lambda *a: None,
                "url": "https://canvas.example/courses/7"})()

        def fill(self, selector, value, settle_ms=300):
            self.calls.append(("fill", selector, value))

        def click(self, selector, settle_ms=2000, force=False):
            self.calls.append(("click", selector, force))
            raise TimeoutError("locator.click: Timeout 15000ms exceeded")

        def wait_for(self, selector, timeout_ms=15_000):
            self.calls.append(("wait_for", selector))

    def test_a_failed_action_says_which_action_it_was(self):
        """Four steps in, a bare TimeoutError out of apply_actions tells the agent
        nothing: not which step, not whether the ones before it ran, and the traceback
        points at the action layer rather than at what it was doing."""
        fake = self.Failing()
        with self.assertRaises(ActionFailed) as caught:
            apply_actions(fake, [{"do": "fill", "selector": "#q", "value": "x"},
                                 {"do": "click", "selector": "#sign-in"},
                                 {"do": "wait_for", "selector": "#done"}])
        message = str(caught.exception)
        self.assertIn("action 2 of 3", message)
        self.assertIn("click #sign-in", message)
        self.assertIn("https://canvas.example/courses/7", message)
        self.assertIn("actions 1..1 did run", message)
        self.assertEqual(fake.calls, [("fill", "#q", "x"), ("click", "#sign-in", False)],
                         "the third action must not have been attempted")

    def test_a_failed_action_keeps_the_error_that_caused_it(self):
        with self.assertRaises(ActionFailed) as caught:
            apply_actions(self.Failing(), [{"do": "click", "selector": "#a"}])
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        self.assertEqual((caught.exception.index, caught.exception.verb), (1, "click"))

    def test_a_failed_action_points_at_the_usual_cure(self):
        """A timeout on an element that is plainly on the page is nearly always an
        overlay - the force flag is in the README, and the error should say so."""
        with self.assertRaises(ActionFailed) as caught:
            apply_actions(self.Failing(), [{"do": "click", "selector": "#a"}])
        self.assertIn("force:true", str(caught.exception))

    def test_an_unknown_verb_in_a_list_says_where_it_was(self):
        with self.assertRaises(ValueError) as caught:
            apply_actions(self.Fake(), [{"do": "hover", "selector": "#a"},
                                        {"do": "teleport"}])
        self.assertIn("action 2 of 2", str(caught.exception))
        self.assertIn("teleport", str(caught.exception))


class TestReadOnly(unittest.TestCase):
    """Read the user's accounts; do not act as them unless the session says so.

    Enforced at the verb, so a direct `browser.click()` from a script and `{do: 'click'}`
    through latchkey_act cannot disagree about what a read-only session means.
    """

    def setUp(self):
        self.page = FakePage()
        self.harness = Harness(self.page)
        self.harness.read_only = True

    def test_every_verb_that_can_send_something_is_refused(self):
        """policy.WRITE_VERBS is the contract; this is what holds the code to it - a
        write verb added to the table without the guard fails here."""
        cases = {
            "click": lambda h: h.click("#go"),
            "click_at": lambda h: h.click_at(10, 20),
            "fill": lambda h: h.fill("#q", "x"),
            "press": lambda h: h.press("#q", "Enter"),
            "select": lambda h: h.select("#s", "v"),
            "check": lambda h: h.check("#c", True),
            "upload": lambda h: h.upload("#f", "/tmp/x.pdf"),
            "eval": lambda h: h.evaluate("fetch('/x', {method: 'POST'})"),
        }
        self.assertEqual(set(cases), set(WRITE_VERBS))
        for verb, act in cases.items():
            with self.subTest(verb=verb):
                with self.assertRaises(ReadOnlyError):
                    act(self.harness)

    def test_nothing_reaches_the_page(self):
        with self.assertRaises(ReadOnlyError):
            self.harness.click("#go")
        self.assertEqual(self.page.actions, [])

    def test_the_refusal_says_what_to_do_instead(self):
        with self.assertRaises(ReadOnlyError) as caught:
            self.harness.click("#go")
        message = str(caught.exception)
        self.assertIn("read-only", message)
        self.assertIn("click", message)
        self.assertIn("latchkey_session_open", message)

    def test_the_refusal_is_visible_to_a_human_watching(self):
        with Recording() as seen:
            with self.assertRaises(ReadOnlyError):
                self.harness.fill("#q", "x")
        event = seen.one("read_only")
        self.assertEqual(event.detail["verb"], "fill")
        self.assertTrue(event.detail["refused"])

    def test_reading_and_navigating_still_work(self):
        """What a read-only session is *for* has to keep working: pointer moves that
        send nothing, scrolling, waiting, screenshots, and reading the page."""
        self.assertEqual(self.harness.state().verdict, "logged-in")
        self.harness.hover("#menu")
        self.harness.scroll(400)
        self.harness.wait_for("#menu")
        self.harness.screenshot("/tmp/s.png")
        verbs = [a[0] for a in self.page.actions]
        self.assertIn("hover", verbs)
        self.assertIn("wheel", verbs)
        self.assertIn("wait_for", verbs)
        self.assertNotIn("click", verbs, "nothing that sends ran")

    def test_a_write_in_an_action_list_is_a_refusal_not_a_broken_action(self):
        """The refusal already says exactly what it is; dressing it up as a failed
        action would bury that under `action 1 of 1 (click #go) failed`."""
        with self.assertRaises(ReadOnlyError):
            apply_actions(self.harness, [{"do": "click", "selector": "#go"}])

    def test_a_read_only_session_is_declared_when_it_is_opened(self):
        browser = Browser(None, spec=SessionSpec(label="reader", read_only=True))
        self.assertTrue(browser.read_only)
        self.assertTrue(browser.spec.as_dict()["read_only"])

    def test_a_writable_session_stays_writable(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_READ_ONLY": ""}):
            self.assertFalse(Browser(None, spec=SessionSpec()).read_only)

    def test_the_environment_is_a_floor_an_agent_cannot_lift(self):
        """The human sets it, so a tool call must not be able to talk the server out
        of it: no session opens writable while it is set."""
        with mock.patch.dict(os.environ, {"LATCHKEY_READ_ONLY": "1"}):
            self.assertTrue(Browser(None, spec=SessionSpec()).read_only)
            self.assertTrue(Browser(None, spec=SessionSpec(read_only=False)).read_only)


class TestSessionSpec(unittest.TestCase):
    def setUp(self):
        self._real = ck.resolve_profiles
        ck.resolve_profiles = lambda spec: ["Default"]

    def tearDown(self):
        ck.resolve_profiles = self._real

    def test_the_spec_shapes_the_browser(self):
        browser = Browser(None, spec=SessionSpec(mode="inject", label="canvas",
                                                 show_cursor=True, width=900))
        self.assertEqual((browser.spec.mode, browser.label, browser.show_cursor),
                         ("inject", "canvas", True))
        self.assertEqual(browser.spec.width, 900)

    def test_no_spec_still_works(self):
        self.assertEqual(Browser(None).spec.mode, "inject")

    def test_the_spec_is_serialisable_without_secrets(self):
        blob = SessionSpec(profiles=["Default"], label="x").as_dict()
        self.assertEqual(blob["profiles"], ["Default"])
        self.assertEqual(blob["label"], "x")
        self.assertNotIn("cookie", str(blob).lower())


class FakeBrowser:
    """What the registry factories hand back: an object with a label and its own
    state, which is the whole point of one session per agent."""

    def __init__(self, index, label=""):
        self.id = index
        self.label = label
        self.thread = threading.get_ident()
        self.seen = []
        self.closed = False

    def close(self):
        self.closed = True


class TestRegistry(unittest.TestCase):
    """Sessions are how two agents drive two sites at once. These are the offline
    halves of that promise: one thread each, one browser each, no shared state."""

    def setUp(self):
        self.browsers = []
        self.lock = threading.Lock()

        def factory(spec=None):
            with self.lock:
                browser = FakeBrowser(len(self.browsers),
                                      getattr(spec, "label", "") or "")
                self.browsers.append(browser)
            return browser

        self.registry = SessionRegistry(factory=factory)

    def tearDown(self):
        self.registry.close_all()

    def test_each_name_gets_its_own_browser(self):
        a = self.registry.get("a")
        b = self.registry.get("b")
        self.assertIsNot(a.submit(lambda br: br), b.submit(lambda br: br))

    def test_calls_run_on_the_session_thread_not_the_callers(self):
        session = self.registry.get("a")
        mine = threading.get_ident()
        theirs = session.submit(lambda br: threading.get_ident())
        self.assertNotEqual(mine, theirs)

    def test_the_same_name_returns_the_same_session(self):
        self.assertIs(self.registry.get("a"), self.registry.get("a"))

    def test_spec_reaches_the_factory(self):
        session = self.registry.get("a", spec=SessionSpec(label="canvas"))
        self.assertEqual(session.submit(lambda br: br.label), "canvas")

    def test_require_refuses_an_unknown_name(self):
        self.registry.get("a")
        with self.assertRaises(SessionError) as caught:
            self.registry.require("typo")
        self.assertIn("a", str(caught.exception))

    def test_parallel_callers_do_not_share_a_browser(self):
        """Two agents, two sessions, at the same time: each browser sees only its
        own work."""
        a = self.registry.get("a")
        b = self.registry.get("b")
        seen = {}

        def work(session, tag, count):
            for i in range(count):
                session.submit(lambda br, i=i: br.seen.append(f"{tag}{i}") or time.sleep(0.01))
            seen[tag] = session.submit(lambda br: list(br.seen))

        threads = [threading.Thread(target=work, args=(a, "a", 5)),
                   threading.Thread(target=work, args=(b, "b", 5))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(seen["a"], ["a0", "a1", "a2", "a3", "a4"])
        self.assertEqual(seen["b"], ["b0", "b1", "b2", "b3", "b4"])

    def test_an_error_inside_a_session_comes_back_to_the_caller(self):
        session = self.registry.get("a")

        def explode(_browser):
            raise ValueError("page blew up")

        with self.assertRaises(ValueError):
            session.submit(explode)
        self.assertTrue(session.alive, "a failed call must not kill the session")

    def test_a_session_that_fails_to_start_says_so(self):
        def bad_factory(spec=None):
            raise RuntimeError("chrome is not installed")

        registry = SessionRegistry(factory=bad_factory)
        with self.assertRaises(SessionError):
            registry.get("a")

    def test_close_frees_the_name(self):
        self.registry.get("a")
        self.assertTrue(self.registry.close("a"))
        self.assertEqual(self.registry.names(), [])
        self.assertTrue(self.browsers[0].closed, "the browser was not closed")

    def test_a_closed_session_refuses_more_work(self):
        session = self.registry.get("a")
        session.close()
        with self.assertRaises(SessionError):
            session.submit(lambda br: br)

    def test_sessions_are_described_without_a_page_read(self):
        self.registry.get("a", spec=SessionSpec(label="canvas"))
        described = self.registry.describe()
        self.assertEqual(described[0]["name"], "a")
        self.assertEqual(described[0]["label"], "canvas")
        self.assertTrue(described[0]["alive"])


class TestSessionLifecycle(unittest.TestCase):
    """A Session owns a thread; closing it must actually stop that thread and
    close the browser behind it."""

    def test_close_runs_the_browser_close_and_joins_the_thread(self):
        closed = threading.Event()

        class Fake:
            label = "fake"

            def close(self):
                closed.set()

        session = Session("a", lambda spec=None: Fake())
        self.assertTrue(session.alive)
        self.assertTrue(session.close())
        self.assertTrue(closed.is_set())
        self.assertFalse(session.alive)

    def test_a_browser_that_never_answers_times_out_with_a_usable_message(self):
        session = Session("a", lambda spec=None: object())
        session.submit(lambda br: None)
        with self.assertRaises(SessionError) as caught:
            session.submit(lambda br: time.sleep(2), timeout=0.2)
        self.assertIn("latchkey_session_close", str(caught.exception))
        session.close(timeout=0.5)

    def test_bus_reports_session_open_and_close(self):
        with Recording() as rec:
            session = Session("a", lambda spec=None: object())
            session.close()
        self.assertIn("session", rec.kinds())
        actions = [e.detail.get("action") for e in rec.events if e.kind == "session"]
        self.assertEqual(actions, ["open", "close"])


class TestEventBus(unittest.TestCase):
    def test_history_is_bounded_but_keeps_the_newest(self):
        local = events.EventBus(history=3)
        for i in range(5):
            local.publish("s", "click", selector=f"#{i}")
        recent = local.recent()
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[-1].detail["selector"], "#4")

    def test_a_late_viewer_sees_recent_history(self):
        """The viewer is normally started after the agent, so a fresh subscriber
        must not stare at a blank screen."""
        local = events.EventBus()
        local.publish("s", "goto", url="https://example.com/")
        seen = []
        local.subscribe(seen.append)
        self.assertEqual(seen, [], "a subscriber should not replay synchronously")
        self.assertEqual(len(local.recent()), 1)

    def test_long_detail_is_trimmed(self):
        local = events.EventBus()
        event = local.publish("s", "fill", value="x" * 5000)
        self.assertLess(len(event.detail["value"]), 500)

    def test_events_are_serialisable(self):
        local = events.EventBus()
        blob = local.publish("s", "click", selector="#a", x=1, y=2).as_dict()
        self.assertEqual(blob["session"], "s")
        self.assertEqual(blob["detail"]["selector"], "#a")
        json.dumps(blob)          # a viewer has to be able to send this

    def test_event_urls_are_not_trimmed_into_broken_viewer_links(self):
        local = events.EventBus()
        url = "https://example.com/?q=" + "x" * 5000
        blob = local.publish("s", "goto", url=url).as_dict()
        self.assertEqual(blob["detail"]["url"], url)
        self.assertEqual(blob["run"], local.run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestASettleIsACeilingNotADuration(unittest.TestCase):
    """The page has to be quiet before the agent reads it. Getting there must not cost
    three seconds a click: the client waits 30s for a whole call, and a batch of six
    real actions used to spend all of it asleep. Quiet is now decided by the requests in
    flight (see `tests/test_settle.py` for that logic in detail); these pin the ceiling
    behaviour a batch depends on."""

    class Clock:
        def __init__(self):
            self.t = 1000.0

        def advance(self, seconds):
            self.t += seconds

        def __call__(self):
            return self.t

    class Page:
        def __init__(self, clock):
            self._clock = clock
            self.waited = []

        def wait_for_timeout(self, ms):
            self.waited.append(ms)
            self._clock.advance(ms / 1000)

    def make(self):
        d = driver.Driver()
        d._clock = self.Clock()
        d._now = d._clock
        d._page = self.Page(d._clock)
        d.track_network(d._page)
        return d

    def settle(self, driver_obj, cap):
        return driver_obj.settle(cap)

    def test_a_quiet_page_settles_in_a_beat_not_the_ceiling(self):
        d = self.make()
        spent = self.settle(d, 2500)
        self.assertLess(spent, 2500)                      # nowhere near the ceiling
        self.assertGreaterEqual(spent, driver.SETTLE_QUIET_MS)

    def test_a_page_that_never_drains_spends_the_ceiling(self):
        """A genuine flood that never finishes still gets the whole cap - as before."""
        d = self.make()
        req = type("R", (), {"resource_type": "fetch"})()   # a fresh request, never ended
        d._net_reqs[id(d._page)][req] = (d._now(), "fetch")
        spent = self.settle(d, 1200)
        self.assertGreaterEqual(spent, 1200)

    def test_a_long_lived_transport_does_not_hold_the_ceiling(self):
        """A websocket or a video never drains, but it is background - settle must not wait
        it out, which is the whole reason networkidle was wrong here."""
        d = self.make()
        req = type("R", (), {"resource_type": "websocket"})()
        d._net_reqs[id(d._page)][req] = (d._now(), "websocket")
        spent = self.settle(d, 2500)
        self.assertLess(spent, 2500)

    def test_no_settle_means_no_waits_at_all(self):
        d = self.make()
        self.assertEqual(self.settle(d, 0), 0.0)
        self.assertEqual(d._page.waited, [])


class TestTheBeatIsShortEnoughToMatter(unittest.TestCase):
    def test_the_quiet_window_is_under_half_a_second(self):
        self.assertLessEqual(driver.SETTLE_QUIET_MS, 500)

    def test_the_action_ceilings_are_bounded(self):
        """These are the worst cases now, not the costs. A click that opens a panel in
        40ms used to pay the whole 1200ms; now it pays about a quiet window."""
        self.assertLessEqual(inspect.signature(automation.AutomationMixin.click)
                             .parameters["settle_ms"].default, 1200)
        self.assertLessEqual(inspect.signature(navigation.NavigationMixin.goto)
                             .parameters["settle_ms"].default, 2500)

    def test_an_explicit_wait_is_still_a_real_wait(self):
        """`wait` is the caller asking for wall-clock time, not for quiet."""
        self.assertIn('page.wait_for_timeout(a.get("ms"', inspect.getsource(automation))


class TestABatchDoesNotOutliveItsClient(unittest.TestCase):
    """The client aborts at 30s and reports a timeout. Coming back at 24s with four
    calls done and one plainly not run is the difference between losing work and
    losing the answer."""

    def run_with(self, budget, sleeps, calls=4):
        real = mcp_server._call_tool

        def slow(name, args):
            time.sleep(sleeps)
            return "done", False

        mcp_server._call_tool = slow
        try:
            return mcp_server.run_calls(
                [{"tool": "latchkey_eval", "args": {"js": "1"}} for _ in range(calls)],
                budget_s=budget)
        finally:
            mcp_server._call_tool = real

    def test_calls_the_budget_does_not_reach_are_not_run_and_say_why(self):
        entries = self.run_with(budget=0.4, sleeps=0.2)
        self.assertEqual(len(entries), 4)
        self.assertEqual([entry["ok"] for entry in entries], [True, True, False, False])
        self.assertIn("budget", entries[2]["skipped"])
        self.assertIn("another batch", entries[2]["skipped"])
        # ...and the calls after it are not blamed on a failure that never happened.
        self.assertIn("budget", entries[3]["skipped"])
        self.assertNotIn("failed and a failed call stops the batch", entries[3]["skipped"])

    def test_a_batch_that_fits_is_left_alone(self):
        entries = self.run_with(budget=5.0, sleeps=0.01)
        self.assertEqual([entry["ok"] for entry in entries], [True] * 4)

    def test_a_call_that_waits_out_the_budget_is_skipped_rather_than_started(self):
        """The production path, and the hole that was in it.

        `run_calls` runs a batch in process; the stdio server does not - it hands each
        call to its session's lane and has them wait on each other (`BatchFlight`). That
        path checked the deadline when a call was *picked up*, then waited on the call
        before it with no bound, then ran. So a call could clear the check at t=0, wait
        twenty seconds for a lane it does not share, and start work the budget exists to
        prevent - which is the client-side timeout the whole design is avoiding.
        """
        sent = []

        class FakeServer:
            _inflight_lock = threading.Lock()
            _inflight = {}

            def dispatch(self, _lane, task):
                threading.Thread(target=task, daemon=True).start()

            def send(self, response):
                sent.append(response)

        real = mcp_server._call_tool
        started = []

        def slow(name, args):
            started.append(name)
            time.sleep(args.get("for", 0.0))
            return "done", False

        mcp_server._call_tool = slow
        try:
            # the budget floor is 1s, so the first call has to outlast that
            parsed = [("latchkey_open", {"session": "a", "for": 1.4}),
                      ("latchkey_text", {"session": "b", "for": 0.0})]
            flight = mcp_server.BatchFlight(FakeServer(), {"id": 1, "method": "tools/call"},
                                            parsed, {"timeout_ms": 200})
            flight.start()
            for _ in range(200):
                if sent:
                    break
                time.sleep(0.02)
        finally:
            mcp_server._call_tool = real

        self.assertTrue(sent, "the batch answered rather than hanging")
        out = json.loads(sent[0]["result"]["content"][0]["text"])
        self.assertTrue(out["results"][0]["ok"], "the first call ran")
        self.assertIn("skipped", out["results"][1])
        self.assertIn("budget", out["results"][1]["skipped"])
        self.assertEqual(started, ["latchkey_open"],
                         "the second call never started; it was out of time when its turn came")

    def test_the_client_s_patience_is_the_ceiling_on_what_a_caller_may_ask_for(self):
        self.assertEqual(mcp_server.batch_budget(None), mcp_server.BATCH_BUDGET_S)
        self.assertEqual(mcp_server.batch_budget(5000), 5.0)
        self.assertEqual(mcp_server.batch_budget(600_000), mcp_server.BATCH_BUDGET_LIMIT_S)

    def test_the_default_batch_budget_leaves_room_to_answer(self):
        self.assertLess(mcp_server.BATCH_BUDGET_S, mcp_server.CLIENT_WAIT_S)
        self.assertLess(mcp_server.BATCH_BUDGET_LIMIT_S, mcp_server.CLIENT_WAIT_S)
        self.assertLess(mcp_server.LOGIN_WAIT_S, mcp_server.CLIENT_WAIT_S)

    def test_a_login_wait_hands_the_wait_back_instead_of_outliving_its_caller(self):
        real = mcp_server._on_session
        asked = {}

        def fake(session, fn, timeout=None):
            asked["timeout"] = timeout

            class Browser:
                def wait_for_login(self, url, timeout_s=None, cancel=None):
                    asked["wait"] = timeout_s
                    return {"verdict": "logged-out"}

            return fn(Browser())

        mcp_server._on_session = fake
        try:
            result = mcp_server.tool_wait_for_login("https://example.com/login", timeout_s=300)
        finally:
            mcp_server._on_session = real
        self.assertLessEqual(asked["wait"], mcp_server.LOGIN_WAIT_S)
        self.assertIn("again", result["note"])


class TestWhereTheMouseIs(unittest.TestCase):
    """The page's size has to travel with the pointer.

    A frame is a *picture* of the page: the screencast is pushed at 1100 px wide while the
    viewport is 1280 CSS px, so a viewer that scaled the pointer by the picture's pixels drew
    it between a fifth and a quarter of the way off - a click at the middle of the page
    73 px from the mark, 146 px out at the far edge. That is what "I can't see where the mouse
    is" looks like.
    """

    class Page:
        def __init__(self, size=None):
            self.viewport_size = size

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        def bounding_box(self, timeout=None):
            return {"x": 900.0, "y": 640.0, "width": 200.0, "height": 120.0}

    class Browser:
        watching = True
        _position = driver.Driver._position      # the real ones, on a page that has a box
        viewport = driver.Driver.viewport

        def __init__(self, page):
            self.page = page

        def run_js(self, js, arg=None):           # the cursor move, on a page that is a stub
            return None

    def cursor_to(self, browser, selector="#far", click=True):
        real = driver.move_cursor
        driver.move_cursor = lambda *args, **kwargs: None      # the page is a stub
        try:
            return driver.Driver._cursor_to(browser, selector, click)
        finally:
            driver.move_cursor = real

    def test_the_event_carries_the_page_size_with_the_point(self):
        detail = self.cursor_to(self.Browser(self.Page({"width": 1280, "height": 820})))
        self.assertEqual(detail, {"x": 1000.0, "y": 700.0, "vw": 1280, "vh": 820})

    def test_a_page_that_will_not_say_its_size_still_reports_the_point(self):
        detail = self.cursor_to(self.Browser(self.Page(None)))
        self.assertEqual(detail, {"x": 1000.0, "y": 700.0},
                         "nothing the page cannot know, and no invented number")

    def test_nobody_watching_means_no_pointer_and_no_work(self):
        class Unwatched(self.Browser):
            watching = False

        detail = self.cursor_to(Unwatched(self.Page({"width": 1280, "height": 820})))
        self.assertEqual(detail, {}, "an unattended session pays nothing")

    def test_the_event_that_reaches_a_viewer_still_carries_the_page_size(self):
        """The last hop is its own chance to lose it: the pointer message is rebuilt from
        the event, and a rebuild that keeps only x and y puts the bug back."""
        class Event:
            session = "point"
            kind = "hover"
            detail = {"x": 970.0, "y": 610.0, "vw": 1280, "vh": 820}

        message = viewer.cursor_message(Event())
        self.assertEqual((message["x"], message["y"]), (970.0, 610.0))
        self.assertEqual((message["vw"], message["vh"]), (1280, 820))

    def test_a_page_that_never_said_its_size_does_not_get_one_invented_for_it(self):
        class Event:
            session = "point"
            kind = "click"
            detail = {"x": 10.0, "y": 20.0}

        message = viewer.cursor_message(Event())
        self.assertNotIn("vw", message)
