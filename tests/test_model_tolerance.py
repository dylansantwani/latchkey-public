"""What a model actually sends, versus what the schema asks for.

One advertised tool whose argument is a list of nested calls is the cheapest surface
there is to carry, and it is the hardest shape for a small model to emit. The failures
are not random - a stringified JSON argument, one call where a list was wanted, `name`
for `tool`, the client's own prefix left on, a verb spelled `action` instead of `do` -
and every one of them is a model that knew exactly what it wanted. Reading them costs
nothing; refusing them costs the task.

What is still refused: a name nothing resolves to, and a verb that is not a verb. Those
get the nearest match by name, because running the wrong tool is worse than saying so.

    python3 -m unittest tests.test_model_tolerance -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import a11y, detect, mcp_server  # noqa: E402
from latchkey.automation import RUNNERS, normalise_actions  # noqa: E402
from latchkey.sessions import SessionRegistry  # noqa: E402


class FakeBrowser:
    """Enough of a browser for the tools to reach their own error handling."""
    label = "default"
    refs = a11y.Refs()
    page = type("Page", (), {"url": "https://example.com/", "evaluate": lambda *a: {}})()

    def state(self, limit=4000):
        return detect.PageState(url="https://example.com/", title="Example")

    def close(self):
        pass


class ToolNamesArriveMangledAndStillResolve(unittest.TestCase):
    def test_the_ways_one_name_arrives(self):
        for given in ("latchkey_open", "open", "mcp__latchkey__latchkey_open",
                      "latchkey-open", "latchkey.open", "  OPEN  ", '"open"'):
            self.assertEqual(mcp_server.resolve_tool(given), "latchkey_open", given)

    def test_a_name_that_is_nothing_resolves_to_nothing(self):
        for given in ("", None, "wander", "latchkey_"):
            self.assertIsNone(mcp_server.resolve_tool(given), given)

    def test_a_near_miss_is_named(self):
        self.assertIn("latchkey_open", mcp_server.nearest_tool("latchkey_opn"))
        self.assertIn("latchkey_snapshot", mcp_server.nearest_tool("snapshoot"))
        self.assertEqual(mcp_server.nearest_tool("zzzzzzzz"), "")


class ActionsArriveInEverySpellingAndStillRun(unittest.TestCase):
    def test_the_synonyms_a_model_reaches_for(self):
        cases = [
            ({"action": "type", "css": "#q", "text": "widgets"},
             {"do": "fill", "selector": "#q", "value": "widgets"}),
            ({"type": "navigate", "href": "example.com"},
             {"do": "goto", "url": "example.com"}),
            ({"verb": "shot", "path": "/tmp/a.png"},
             {"do": "screenshot", "path": "/tmp/a.png"}),
            ({"do": "tap", "ref": "e7"}, {"do": "click", "ref": "e7"}),
            ({"op": "refresh"}, {"do": "reload"}),
            ({"do": "click", "target": "#go"}, {"do": "click", "selector": "#go"}),
        ]
        for given, expected in cases:
            self.assertEqual(normalise_actions([given])[0], expected, given)

    def test_target_is_a_tab_for_the_tab_verbs_and_an_element_everywhere_else(self):
        """The one synonym that is genuinely two different arguments."""
        self.assertEqual(normalise_actions([{"op": "switch_tab", "target": "t2"}])[0],
                         {"do": "switch", "target": "t2"})
        self.assertEqual(normalise_actions([{"do": "close_tab", "target": 2}])[0],
                         {"do": "close_page", "target": 2})

    def test_a_list_that_is_not_a_list(self):
        one = {"do": "reload"}
        self.assertEqual(normalise_actions(one), [one])
        self.assertEqual(normalise_actions(json.dumps([one])), [one])
        self.assertEqual(normalise_actions("reload"), [one])

    def test_every_verb_the_runners_have_survives_normalising(self):
        for verb in RUNNERS:
            self.assertEqual(normalise_actions([{"do": verb}])[0]["do"], verb)

    def test_an_unknown_verb_names_the_nearest_and_lists_the_rest(self):
        with self.assertRaises(ValueError) as caught:
            normalise_actions([{"do": "clik", "ref": "e1"}])
        message = str(caught.exception)
        self.assertIn("click", message)
        self.assertIn("action 1 of 1", message)
        self.assertIn("screenshot", message, "the full list is there to choose from")

    def test_an_action_that_is_not_an_object_says_so(self):
        with self.assertRaises(ValueError) as caught:
            normalise_actions([5])
        self.assertIn("not an object", str(caught.exception))


class TheAdvertisedSurfaceIsTheHumanSChoice(unittest.TestCase):
    def test_one_tool_by_default_and_the_rest_still_dispatchable(self):
        tools = mcp_server.advertised("batch")
        self.assertEqual([tool["name"] for tool in tools], [mcp_server.BATCH])
        for name in mcp_server.CORE_TOOLS:
            self.assertIn(name, mcp_server.DISPATCH)

    def test_core_is_the_handful_a_task_uses_and_stays_far_smaller_than_all(self):
        core = mcp_server.advertised("core")
        names = [tool["name"] for tool in core]
        self.assertIn(mcp_server.BATCH, names)
        self.assertIn("latchkey_open", names)
        self.assertIn(mcp_server.HELP, names)
        self.assertLess(len(json.dumps(core)), len(json.dumps(mcp_server.advertised("all"))))

    def test_every_advertised_schema_names_a_tool_that_exists(self):
        for mode in ("batch", "core", "all"):
            for tool in mcp_server.advertised(mode):
                self.assertIn(tool["name"], mcp_server.DISPATCH, f"{mode}: {tool['name']}")

    def test_an_unrecognised_setting_falls_back_to_the_cheap_surface(self):
        self.assertEqual(len(mcp_server.advertised("nonsense")), 1)


class AnErrorSaysWhatToDoRatherThanWhereItCameFrom(unittest.TestCase):
    """No real browser here: the default session opens itself on first use, which is
    the point of that change and exactly what a test must not trigger."""

    def setUp(self):
        self._real = mcp_server.registry
        mcp_server.registry = SessionRegistry(lambda spec=None: FakeBrowser())

    def tearDown(self):
        mcp_server.registry.close_all()
        mcp_server.registry = self._real

    def test_a_spoken_error_arrives_without_a_traceback(self):
        out, failed = mcp_server._call_tool("latchkey_snapshot", {"mode": "sideways"})
        self.assertTrue(failed)
        self.assertNotIn("Traceback", out)
        self.assertIn("interactive", out, "it lists the modes that do exist")

    def test_the_default_session_opened_itself_to_answer(self):
        mcp_server._call_tool("latchkey_text", {})
        self.assertEqual(mcp_server.registry.names(), ["default"])

    def test_an_unknown_argument_is_ignored_and_said_so(self):
        # deepseek-v4-flash carried `mode` over from another browser tool; the call used to
        # fail outright for a word the tool would have ignored.
        out, failed = mcp_server._call_tool("latchkey_text", {"bogus": 1})
        self.assertFalse(failed)
        note = out["note"] if isinstance(out, dict) else out
        self.assertIn("ignored 'bogus'", note)
        self.assertIn("limit, offset, url, session", note, "it names the arguments that exist")

    def test_a_missing_required_argument_is_still_refused_with_the_signature(self):
        out, failed = mcp_server._call_tool("latchkey_frame_text", {"bogus": 1})
        self.assertTrue(failed)
        self.assertIn("It takes: index*", out)
        self.assertNotIn("\n", out, "one line, not a traceback")

    def test_an_unknown_tool_names_the_nearest(self):
        out, failed = mcp_server._call_tool("latchkey_opn", {})
        self.assertTrue(failed)
        self.assertIn("latchkey_open", out)
        self.assertIn("latchkey_help", out)


class DeepseekWeekOfSept12(unittest.TestCase):
    """The argument shapes deepseek-v4-flash sent latchkey in Lattice, 2026-09-12..19."""

    def test_a_wait_with_a_condition_reads_ms_as_its_timeout(self):
        got = mcp_server.normalise_args("latchkey_wait", {"until": "text", "value": "Done", "ms": 5000})
        self.assertEqual(got, {"until": "text", "value": "Done", "timeout_ms": 5000})
        got = mcp_server.normalise_args("latchkey_wait", {"until": "text", "value": "x", "seconds": 3})
        self.assertEqual(got["timeout_ms"], 3000)

    def test_a_bare_ms_is_still_a_sleep(self):
        self.assertEqual(mcp_server.normalise_args("latchkey_wait", {"ms": 800}),
                         {"until": "time", "value": "800"})

    def test_wait_for_login_takes_its_timeout_in_any_unit(self):
        for given, want in (({"timeout_ms": 15000}, 15), ({"wait_s": 60}, 60), ({"ms": 500}, 1)):
            got = mcp_server.normalise_args("latchkey_wait_for_login", {"url": "https://a.b", **given})
            self.assertEqual(got, {"url": "https://a.b", "timeout_s": want}, given)

    def test_the_tool_name_repeated_inside_its_args_is_dropped(self):
        got = mcp_server.normalise_args("latchkey_eval", {"tool": "latchkey_eval", "js": "1+1"})
        self.assertEqual(got, {"js": "1+1"})
        # a tool that really takes `tool` keeps it
        self.assertIn("name", mcp_server.normalise_args("latchkey_help", {"tool": "latchkey_open"}))

    def test_host_site_and_url_come_from_the_current_page(self):
        def show(host, limit=None):
            return {"host": host}
        real = mcp_server._current_url
        mcp_server._current_url = lambda session: "https://www.ebay.com/itm/1"
        try:
            args, ignored = mcp_server._drop_unknown("latchkey_show_cookies", show, {})
            self.assertEqual((args, ignored), ({"host": "www.ebay.com"}, []))
            def wait(url, timeout_s=300, session="default"):
                return url
            args, _ = mcp_server._drop_unknown("latchkey_wait_for_login", wait, {"timeout_s": 5})
            self.assertEqual(args["url"], "https://www.ebay.com/itm/1")
        finally:
            mcp_server._current_url = real

    def test_nothing_is_guessed_when_no_page_is_open(self):
        def show(host, limit=None):
            return {"host": host}
        real = mcp_server._current_url
        mcp_server._current_url = lambda session: None
        try:
            args, _ = mcp_server._drop_unknown("latchkey_show_cookies", show, {"mode": "x"})
            self.assertEqual(args, {"mode": "x"}, "required host missing: left for the call to refuse")
        finally:
            mcp_server._current_url = real


class TimeoutsSayWhy(unittest.TestCase):
    """Playwright's call log is the diagnosis; it used to be cut to its first line."""

    from latchkey.automation import why_it_timed_out as _why
    why = staticmethod(_why)

    def test_an_overlay_is_named(self):
        why, fix = self.why('Locator.click: Timeout 8000ms exceeded.\nCall log:\n  - waiting for locator("#go")\n'
                            '  - locator resolved to <button id="go">Go</button>\n  - attempting click action\n'
                            '    - <div class="reactour__mask"></div> intercepts pointer events\n')
        self.assertIn('<div class="reactour__mask"></div> is covering it', why)
        self.assertIn("force:true", fix)

    def test_hidden_disabled_missing_and_moving_each_get_their_own_fix(self):
        base = 'Timeout 8000ms exceeded.\nCall log:\n  - waiting for locator("#go")\n'
        resolved = base + '  - locator resolved to <button id="go">Go</button>\n'
        cases = {
            resolved + '    - element is not visible\n': ("hidden", "open whatever holds it"),
            resolved + '    - element is not enabled\n': ("disabled", "unfinished"),
            resolved + '    - element is not stable\n': ("moving", "settle"),
            base: ("nothing on the page matches", "fresh latchkey_snapshot"),
        }
        for log, (why_has, fix_has) in cases.items():
            why, fix = self.why(log)
            self.assertIn(why_has, why, log)
            self.assertIn(fix_has, fix, log)

    def test_a_plain_timeout_keeps_the_general_advice(self):
        self.assertEqual(self.why("Timeout 8000ms exceeded."), ("", ""))


if __name__ == "__main__":
    unittest.main()


class GuessedToolNames(unittest.TestCase):
    """The exact names Qwen3.6-35B-A3B invented on 2026-09-12 resolve to the tool it meant."""

    def test_text_guesses(self):
        from latchkey import mcp_server as m
        for guess in ("latchkey_read_page", "latchkey_get_text", "latchkey_get_text_content",
                      "latchkey_text_content", "latchkey_page_text", "mcp__latchkey__get_page_text"):
            self.assertEqual(m.resolve_tool(guess), "latchkey_text", guess)

    def test_eval_open_and_friends(self):
        from latchkey import mcp_server as m
        self.assertEqual(m.resolve_tool("latchkey_evaluate"), "latchkey_eval")
        self.assertEqual(m.resolve_tool("navigate"), "latchkey_open")
        self.assertEqual(m.resolve_tool("latchkey_take_screenshot"), "latchkey_screenshot")
        self.assertEqual(m.resolve_tool("list_tabs"), "latchkey_pages")

    def test_real_names_still_win_and_nonsense_still_fails(self):
        from latchkey import mcp_server as m
        for real in m.DISPATCH:
            self.assertEqual(m.resolve_tool(real), real)
        self.assertIsNone(m.resolve_tool("latchkey_zzzzzz"))

    def test_synonyms_point_at_real_tools(self):
        from latchkey import mcp_server as m
        for target in m.TOOL_SYNONYMS:
            self.assertIn(f"latchkey_{target}", m.DISPATCH)

    def test_eval_action_without_js_says_what_is_missing(self):
        from latchkey import automation
        with self.assertRaises(ValueError) as ctx:
            automation._run_eval(object(), {})
        self.assertIn("js", str(ctx.exception))


class SecondQwenRun(unittest.TestCase):
    """From the second Qwen3.6 pass over a GitHub releases page (2026-09-12)."""

    def test_tab_id_on_a_tool_without_tabs_is_dropped(self):
        from latchkey import mcp_server as m
        self.assertEqual(m.normalise_args("latchkey_text", {"tab": "t1", "limit": 500}), {"limit": 500})
        # a tool that really targets tabs keeps it (read as its own argument name)
        self.assertIn("target", m.normalise_args("latchkey_switch", {"tab": "t2"}))

    def test_url_and_title_guesses_go_to_pages(self):
        from latchkey import mcp_server as m
        for guess in ("latchkey_url", "latchkey_get_url", "latchkey_current_url", "latchkey_title"):
            self.assertEqual(m.resolve_tool(guess), "latchkey_pages", guess)

    def test_html_guess_gets_a_way_forward_not_a_dead_end(self):
        from latchkey import mcp_server as m
        with self.assertRaises(ValueError) as ctx:
            m._call_of({"tool": "latchkey_html", "args": {"limit": 15000}}, 0)
        self.assertIn("latchkey_eval", str(ctx.exception))
        self.assertIn("getAttribute", str(ctx.exception))


class AttributeGuess(unittest.TestCase):
    def test_get_attribute_points_at_eval(self):
        from latchkey import mcp_server as m
        with self.assertRaises(ValueError) as ctx:
            m._call_of({"tool": "latchkey_get_attribute", "args": {"selector": "time", "attribute": "datetime"}}, 1)
        self.assertIn("getAttribute", str(ctx.exception))
