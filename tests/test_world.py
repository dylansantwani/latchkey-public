"""latchkey's own world: JavaScript runs where the page cannot see it."""
import unittest

from latchkey import world as world_mod
from latchkey.world import EvaluateError, World, as_function


class FakeCdp:
    """Answers the three CDP calls the world makes, and records them."""

    def __init__(self):
        self.sent = []
        self.contexts = 0
        self.gone_once = False
        self.frames = {"frame": {"id": "MAIN", "url": "https://example.com/"},
                       "childFrames": [
                           {"frame": {"id": "KID", "url": "https://kid.example/", "name": "kid"}},
                           {"frame": {"id": "TWIN1", "url": "https://twin.example/", "name": "a"}},
                           {"frame": {"id": "TWIN2", "url": "https://twin.example/", "name": "b"}}]}
        self.results = {}

    def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": self.frames}
        if method == "Page.createIsolatedWorld":
            self.contexts += 1
            return {"executionContextId": self.contexts}
        if method == "Runtime.callFunctionOn":
            if self.gone_once:
                self.gone_once = False
                raise RuntimeError("Cannot find context with specified id")
            key = params["functionDeclaration"]
            if key in self.results:
                return self.results[key]
            arg = (params.get("arguments") or [{}])[0].get("value")
            return {"result": {"type": "object", "value": {"ctx": params["executionContextId"],
                                                          "arg": arg}}}
        raise AssertionError(method)


class Frame:
    def __init__(self, url, name=""):
        self.url, self.name = url, name
        self.evaluated = []

    def evaluate(self, js, arg=None):
        self.evaluated.append((js, arg))
        return "main-world"


class Page:
    main_frame = None

    def __init__(self):
        self.evaluated = []

    def evaluate(self, js, arg=None):
        self.evaluated.append((js, arg))
        return "main-world"


class TestAsFunction(unittest.TestCase):
    def test_a_function_is_left_alone_and_an_expression_is_wrapped(self):
        for js in ("() => 1", "async () => 1", "(a) => a", "x => x", "function f() {}",
                   "async function f() {}"):
            self.assertEqual(as_function(js), js)
        self.assertEqual(as_function("document.title"), "() => (document.title\n)")
        self.assertEqual(as_function("1 + 1"), "() => (1 + 1\n)")


class TestWorld(unittest.TestCase):
    def setUp(self):
        self.cdp = FakeCdp()
        self.page = Page()
        self.world = World(lambda page: self.cdp)

    def methods(self):
        return [m for m, _ in self.cdp.sent]

    def test_a_call_makes_one_isolated_world_and_runs_the_function_in_it(self):
        got = self.world.evaluate(self.page, "(a) => a", {"k": 1})
        self.assertEqual(got, {"ctx": 1, "arg": {"k": 1}})
        self.assertEqual(self.methods(), ["Page.getFrameTree", "Page.createIsolatedWorld",
                                          "Runtime.callFunctionOn"])
        _, made = self.cdp.sent[1]
        self.assertEqual(made["frameId"], "MAIN")
        self.assertEqual(made["worldName"], world_mod.WORLD_NAME)
        _, call = self.cdp.sent[2]
        self.assertTrue(call["returnByValue"] and call["awaitPromise"])
        self.assertEqual(call["arguments"], [{"value": {"k": 1}}])
        self.assertEqual(self.page.evaluated, [])          # never the main world

    def test_the_world_is_reused_across_calls_on_the_same_page(self):
        self.world.evaluate(self.page, "() => 1")
        self.world.evaluate(self.page, "() => 2")
        self.assertEqual(self.methods().count("Page.createIsolatedWorld"), 1)

    def test_no_argument_means_no_arguments_key(self):
        self.world.evaluate(self.page, "() => 1")
        _, call = self.cdp.sent[-1]
        self.assertNotIn("arguments", call)

    def test_a_context_lost_to_a_navigation_is_made_again_once(self):
        self.world.evaluate(self.page, "() => 1")
        self.cdp.gone_once = True
        got = self.world.evaluate(self.page, "() => 2")
        self.assertEqual(got["ctx"], 2)                      # a fresh world, second try
        self.assertEqual(self.methods().count("Page.createIsolatedWorld"), 2)

    def test_the_pages_own_exception_is_raised_as_one_of_ours(self):
        self.cdp.results["() => boom()"] = {
            "result": {"type": "undefined"},
            "exceptionDetails": {"text": "Uncaught", "exception": {
                "description": "ReferenceError: boom is not defined\n    at <anonymous>:1:7"}}}
        with self.assertRaises(EvaluateError) as caught:
            self.world.evaluate(self.page, "() => boom()")
        self.assertIn("boom is not defined", str(caught.exception))
        self.assertNotIn("at <anonymous>", str(caught.exception))

    def test_a_value_that_cannot_come_back_by_value_is_none(self):
        self.cdp.results["() => document.body"] = {"result": {"type": "object",
                                                              "subtype": "node"}}
        self.assertIsNone(self.world.evaluate(self.page, "() => document.body"))

    def test_a_frame_is_found_by_its_url_and_name(self):
        self.world.evaluate(self.page, "() => 1", frame=Frame("https://kid.example/", "kid"))
        _, made = self.cdp.sent[1]
        self.assertEqual(made["frameId"], "KID")
        self.world.evaluate(self.page, "() => 1", frame=Frame("https://twin.example/", "b"))
        self.assertEqual(self.cdp.sent[-2][1]["frameId"], "TWIN2")

    def test_a_frame_this_page_cannot_see_is_read_through_playwright(self):
        # An out-of-process iframe is another target; Playwright reaches it, this cannot.
        frame = Frame("https://elsewhere.example/", "")
        got = self.world.evaluate(self.page, "() => 1", frame=frame)
        self.assertEqual(got, "main-world")
        self.assertEqual(frame.evaluated, [("() => 1", None)])
        self.assertNotIn("Page.createIsolatedWorld", self.methods())

    def test_two_frames_with_the_same_url_and_no_name_are_not_guessed_between(self):
        self.cdp.frames["childFrames"][1]["frame"]["name"] = ""
        self.cdp.frames["childFrames"][2]["frame"]["name"] = ""
        frame = Frame("https://twin.example/", "")
        self.assertEqual(self.world.evaluate(self.page, "() => 1", frame=frame), "main-world")

    def test_forgetting_a_page_drops_its_worlds(self):
        self.world.evaluate(self.page, "() => 1")
        self.world.forget(self.page)
        self.world.evaluate(self.page, "() => 1")
        self.assertEqual(self.methods().count("Page.createIsolatedWorld"), 2)


class TestDriverRunsThroughTheWorld(unittest.TestCase):
    """`Driver.run_js` is the one door: CDP behind the page means the isolated world,
    a stand-in page (no CDP) means Playwright's own evaluate."""

    def test_a_page_without_cdp_is_asked_directly(self):
        from latchkey.driver import Driver

        class Bare(Driver):
            def __init__(self):
                super().__init__()
                self._page = Page()
                self._ctx = None                 # nothing to open a CDP session on

        bare = Bare()
        self.assertEqual(bare.run_js("() => 1"), "main-world")
        self.assertEqual(bare.run_js("(a) => a", 5), "main-world")
        self.assertEqual(bare._page.evaluated, [("() => 1", None), ("(a) => a", 5)])

    def test_a_page_with_cdp_is_asked_from_the_isolated_world(self):
        from latchkey.driver import Driver
        cdp = FakeCdp()

        class Ctx:
            pages = []

            def new_cdp_session(self, page):
                return cdp

        class Wired(Driver):
            def __init__(self):
                super().__init__()
                self._page = Page()
                self._ctx = Ctx()

        wired = Wired()
        got = wired.run_js("(a) => a", {"x": 1})
        self.assertEqual(got, {"ctx": 1, "arg": {"x": 1}})
        self.assertEqual(wired._page.evaluated, [])
        self.assertIn("Page.createIsolatedWorld", [m for m, _ in cdp.sent])
        wired._forget_cdp(wired._page)                        # a closed page forgets its world
        wired.run_js("() => 1")
        self.assertEqual([m for m, _ in cdp.sent].count("Page.createIsolatedWorld"), 2)

    def test_public_eval_keeps_playwright_main_world_semantics(self):
        from latchkey.driver import Driver

        class Ctx:
            pages = []

            def new_cdp_session(self, page):
                return cdp

        class Wired(Driver):
            def __init__(self):
                super().__init__()
                self._page = Page()
                self._ctx = Ctx()

            def state(self):
                return type("State", (), {"summary": lambda _self: {}})()

        cdp = FakeCdp()
        wired = Wired()
        self.assertEqual(wired.evaluate("() => window.location.href"), "main-world")
        self.assertEqual(wired._page.evaluated, [("() => window.location.href", None)])
        self.assertEqual(cdp.sent, [], "public eval must not enter latchkey's isolated world")

    def test_public_frame_eval_keeps_main_world_semantics(self):
        from latchkey.frames import FramesMixin

        frame = Frame("https://kid.example/", "kid")

        class Frames(FramesMixin):
            def frame_at(self, index):
                self.assertEqual(index, 2)
                return frame

            def assertEqual(self, *args):
                unittest.TestCase().assertEqual(*args)

        self.assertEqual(Frames().frame_eval("() => document.title", 2), "main-world")
        self.assertEqual(frame.evaluated, [("() => document.title", None)])


if __name__ == "__main__":
    unittest.main()
