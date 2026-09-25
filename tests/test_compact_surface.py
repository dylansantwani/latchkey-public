"""The surface a local model carries, and what it gets back.

The owner's everyday model is a local 35B with a 128k window. Measured before this change:
the one advertised tool was 8,757 characters of JSON (7,316 of them description), `core`
18,311 and `all` 29,699; batch results averaged 3.7K characters and peaked at 37.6K, with an
`eval` at 24.5K, a cookie list at 23.6K and a host list at 22.6K. And the argument mistakes it
made were not random: `mode: "plain"` for a snapshot, `{"type": "click", "ref": "e4"}` with
no verb key, `session` passed to a tool that has none.

These hold the sizes down, check that the small budget really is small and that nothing it
cuts is cut silently, and read those mistakes the way they were meant.

    python3 -m unittest tests.test_compact_surface -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import a11y, detect, mcp_server  # noqa: E402
from latchkey import cookies as ck  # noqa: E402
from latchkey.automation import normalise_actions  # noqa: E402
from latchkey.driver import normalise_until  # noqa: E402
from latchkey.sessions import SessionRegistry  # noqa: E402


def size(tools) -> int:
    """Measured the way the before-numbers were: the tools list, serialised."""
    return len(json.dumps(tools))


class TheSurfaceIsSmall(unittest.TestCase):

    # These ceilings are a guard against surface creep, not a law: every byte here is
    # loaded into an agent's context on every call. They were set flush against the size
    # at the time, so a genuinely new tool does not fit without moving them - which is
    # meant to be a deliberate act with a number attached, and this is that act. The
    # accounts surface (latchkey_accounts, plus `account` on login/login_status/
    # session_open) cost +125 on the batch description and +573 across the full catalog.
    def test_the_one_advertised_tool(self):
        batch = mcp_server.advertised("batch")
        self.assertLessEqual(len(batch[0]["description"]), 2250, "was 2,093 before accounts")
        self.assertLess(size(batch), 3500, "was 8,757")

    def test_core_and_all(self):
        self.assertLess(size(mcp_server.advertised("core")), 9000, "was 18,311")
        self.assertLess(size(mcp_server.advertised("all")), 17_700,
                        "was 29,699; 15,985 before accounts; +~950 for latchkey_pilot")

    def test_no_single_schema_is_a_page_of_prose(self):
        for spec in mcp_server.CATALOG:
            self.assertLess(len(json.dumps(spec)), 1000, spec["name"])

    def test_the_instructions_fit_where_lattice_shows_them(self):
        self.assertLessEqual(len(mcp_server.INSTRUCTIONS), 500)
        reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {}})
        self.assertEqual(reply["result"]["instructions"], mcp_server.INSTRUCTIONS)

    def test_the_prose_that_left_the_schemas_is_still_one_call_away(self):
        for name in ("latchkey_act", "latchkey_snapshot", "latchkey_wait",
                     "latchkey_session_open", "latchkey_open", "latchkey_login"):
            self.assertIn(name, mcp_server.HELP_DETAIL, name)
        row = mcp_server.tool_help(name="latchkey_act")
        self.assertIn("force: true", row["description"])
        self.assertIn("wait_for", row["description"])
        self.assertIn("frame", row["description"])
        self.assertIn("click_at", row["description"])


class HelpByTopic(unittest.TestCase):

    def test_every_topic_answers(self):
        for topic in mcp_server.HELP_TOPICS:
            self.assertTrue(mcp_server.tool_help(topic=topic)["help"], topic)

    def test_a_topic_passed_as_a_name_and_the_usual_synonyms(self):
        self.assertEqual(mcp_server.tool_help(name="google")["topic"], "google")
        self.assertEqual(mcp_server.tool_help(topic="login")["topic"], "google")
        self.assertEqual(mcp_server.tool_help(name="mcp__latchkey__act")["name"],
                         "latchkey_act")

    def test_an_unknown_topic_lists_the_real_ones_in_a_line(self):
        with self.assertRaises(ValueError) as caught:
            mcp_server.tool_help(topic="teleport")
        self.assertIn("google", str(caught.exception))
        self.assertNotIn("\n", str(caught.exception))

    def test_the_google_topic_is_the_whole_sign_in_in_order(self):
        # The Google path IS a one-time latchkey sign-in on the dedicated profile;
        # the topic must walk it and warn off the clone that signs the user out.
        text = mcp_server.HELP_TOPICS["google"]
        for step in ("dedicated", "signed in once", "latchkey_login",
                     "latchkey_login_status", "Never ask",
                     "Do NOT switch Google to mode clone"):
            self.assertIn(step, text)


class TheMistakesAModelActuallyMade(unittest.TestCase):

    def test_snapshot_modes_by_the_names_models_use(self):
        for given, meant in (("plain", "text"), ("changes", "diff"), ("all", "full"),
                             ("structure", "outline"), ("", "interactive"),
                             (None, "interactive"), ("INTERACTIVE", "interactive")):
            self.assertEqual(a11y.normalise_mode(given), meant, given)

    def test_a_mode_that_means_nothing_gets_one_line_with_the_real_ones(self):
        with self.assertRaises(ValueError) as caught:
            a11y.normalise_mode("sideways")
        message = str(caught.exception)
        self.assertIn("interactive, full, text, outline, diff", message)
        self.assertNotIn("\n", message)

    def test_an_action_with_no_verb_is_read_from_its_fields(self):
        cases = [
            ({"type": "click", "ref": "e4"}, "click"),          # what Qwen sent
            ({"ref": "e4"}, "click"),
            ({"ref": "[ref=e4]", "value": "hello"}, "fill"),
            ({"url": "https://example.com"}, "goto"),
            ({"selector": "#q", "key": "Enter"}, "press"),
            ({"ms": 500}, "wait"),
            ({"js": "1 + 1"}, "eval"),
            ({"click": "e7"}, "click"),
            ({"navigate": "https://example.com"}, "goto"),
            ({"type": "#q", "text": "widgets"}, "fill"),
        ]
        for given, verb in cases:
            action = normalise_actions([given])[0]
            self.assertEqual(action["do"], verb, given)
        self.assertEqual(normalise_actions([{"ref": "[ref=e4]"}])[0]["ref"], "e4")

    def test_an_elements_own_type_is_not_read_as_type_into_it(self):
        """Found in review: {"selector": "#e", "type": "email"} became a fill with no value,
        which would have wiped the field."""
        action = normalise_actions([{"selector": "#e", "type": "email"}])[0]
        self.assertNotEqual(action["do"], "fill")
        self.assertEqual(normalise_actions([{"selector": "#q", "type": "text",
                                             "value": "hi"}])[0]["do"], "fill")
        with self.assertRaises(ValueError):
            normalise_actions([{"selector": "#x", "type": "banana"}])
        self.assertEqual(normalise_actions([{"selector": "#s", "select": "Two"}])[0],
                         {"do": "select", "selector": "#s", "value": "Two"})

    def test_every_list_alias_of_actions_is_unwrapped_not_nested(self):
        for alias in ("action", "steps", "step", "commands"):
            out = mcp_server.normalise_args("latchkey_act",
                                            {alias: [{"do": "click", "ref": "e7"}]})
            self.assertEqual(out, {"actions": [{"do": "click", "ref": "e7"}]}, alias)

    def test_a_ref_that_names_a_button_is_clicked_not_typed_into(self):
        refs = a11y.Refs()
        refs.note("https://example.com/", [{"ref": "e3", "role": "button", "name": "Next"},
                                           {"ref": "e4", "role": "textbox", "name": "Email"}])
        self.assertEqual(normalise_actions([{"ref": "e3", "text": "Next"}], refs)[0]["do"],
                         "click")
        self.assertEqual(normalise_actions([{"ref": "e4", "text": "a@b"}], refs)[0]["do"],
                         "fill")

    def test_an_action_that_says_nothing_is_told_what_to_say_in_one_line(self):
        with self.assertRaises(ValueError) as caught:
            normalise_actions([{"text": "Sign in"}])
        message = str(caught.exception)
        self.assertIn('"do"', message)
        self.assertIn("click", message)
        self.assertNotIn("None", message, "the old error was 'unknown action None'")

    def test_a_verb_where_the_tool_goes(self):
        calls = mcp_server.calls_of([
            {"tool": "click", "args": {"ref": "e7", "session": "g"}},
            {"do": "type", "ref": "e4", "value": "hi"},
            {"tool": "navigate", "url": "https://example.com"},
            {"tool": "back"},
            {"action": "goto", "url": "https://github.com", "session": "gh"},
        ])
        self.assertEqual(calls[0], ("latchkey_act",
                                    {"actions": [{"do": "click", "ref": "e7"}], "session": "g"}))
        self.assertEqual(calls[1], ("latchkey_act",
                                    {"actions": [{"do": "fill", "ref": "e4", "value": "hi"}]}))
        self.assertEqual(calls[2], ("latchkey_open", {"url": "https://example.com"}))
        self.assertEqual(calls[3], ("latchkey_history", {"url": "back"}))
        self.assertEqual(calls[4], ("latchkey_open", {"url": "https://github.com",
                                                      "session": "gh"}))

    def test_arguments_by_other_names(self):
        n = mcp_server.normalise_args
        self.assertEqual(n("latchkey_sites", {"session": "x", "limit": 5}), {"limit": 5})
        self.assertEqual(n("latchkey_session_open", {"session": "gmail"}), {"name": "gmail"})
        self.assertEqual(n("latchkey_text", {"max_chars": 900}), {"limit": 900})
        self.assertEqual(n("latchkey_snapshot", {"limit": 900}), {"max_chars": 900})
        self.assertEqual(n("latchkey_eval", {"script": "1"}), {"js": "1"})
        self.assertEqual(n("latchkey_find", {"text": "sign in"}), {"query": "sign in"})
        self.assertEqual(n("latchkey_open", {"href": "x.com"}), {"url": "x.com"})
        self.assertEqual(n("latchkey_wait", {"text": "Welcome"}),
                         {"until": "text", "value": "Welcome"})
        self.assertEqual(n("latchkey_wait", {"ms": 500}), {"until": "time", "value": "500"})
        self.assertEqual(n("latchkey_act", {"do": "click", "ref": "e1", "session": "s"}),
                         {"actions": [{"do": "click", "ref": "e1"}], "session": "s"})
        self.assertEqual(n("latchkey_act", {"action": "click", "ref": "e1"}),
                         {"actions": [{"action": "click", "ref": "e1"}]})
        once = n("latchkey_text", {"max_chars": 900})
        self.assertEqual(n("latchkey_text", once), once, "idempotent")

    def test_a_real_argument_is_never_renamed(self):
        self.assertEqual(mcp_server.normalise_args("latchkey_show_cookies", {"host": "a"}),
                         {"host": "a"})
        self.assertEqual(mcp_server.normalise_args("latchkey_text", {"limit": 3, "url": "u"}),
                         {"limit": 3, "url": "u"})

    def test_the_lane_follows_the_session_a_renamed_argument_names(self):
        """Ordering is per session: `session_open(session="gmail")` must queue on gmail's
        lane, or the call that uses the session can overtake the one that opens it."""
        self.assertEqual(mcp_server.lane_of_call("latchkey_session_open",
                                                 {"session": "gmail"}), "gmail")

    def test_waits_by_the_names_models_use(self):
        self.assertEqual(normalise_until("appear", "Hi"), ("text", "Hi"))
        self.assertEqual(normalise_until("network_idle"), ("networkidle", None))
        self.assertEqual(normalise_until("#inbox"), ("selector", "#inbox"))
        self.assertEqual(normalise_until("https://mail.google.com/"),
                         ("url", "https://mail.google.com/"))
        self.assertEqual(normalise_until("sleep", "300"), ("time", "300"))


class AWaitInsideABatchEndsWithTheBatch(unittest.TestCase):
    """A call that waits for a person is 20-25 s on its own; as the third call of a batch that
    has already spent ten, it would run past the client's thirty."""

    def test_a_wait_is_capped_by_the_batch_deadline(self):
        import time
        mcp_server._CALL.deadline = time.monotonic() + 5.0
        try:
            self.assertLessEqual(mcp_server.time_left(25.0), 4.1)
            with mock.patch.object(mcp_server.login_mod, "wait",
                                   return_value={"signed_in": False}) as wait:
                mcp_server.tool_login_status(wait_s=20)
            self.assertLessEqual(wait.call_args[1]["timeout_s"], 4.1)
        finally:
            mcp_server._CALL.deadline = None
        self.assertEqual(mcp_server.time_left(25.0), 25.0, "outside a batch: its own ceiling")

    def test_the_in_process_batch_hands_its_deadline_to_its_calls(self):
        seen = []
        real = mcp_server.DISPATCH["latchkey_login_status"]
        mcp_server.DISPATCH["latchkey_login_status"] = \
            lambda **kw: seen.append(mcp_server.time_left(99.0)) or {}
        try:
            mcp_server.tool_batch(calls=[{"tool": "latchkey_login_status"}], timeout_ms=3000)
        finally:
            mcp_server.DISPATCH["latchkey_login_status"] = real
        self.assertLessEqual(seen[0], 3.0)
        self.assertIsNone(getattr(mcp_server._CALL, "deadline", None))


class Page:
    def __init__(self, text):
        self.text = text
        self.url = "https://example.com/"

    def evaluate(self, script, args=None):
        offset, limit = args
        return [len(self.text), self.text[offset:offset + limit]]


class TextBrowser:
    label = "default"

    def __init__(self, text):
        self.page = Page(text)
        self._text = text

    def run_js(self, js, arg=None):
        return self.page.evaluate(js, arg)

    def state(self, text_limit=4000):
        return detect.PageState(url="https://example.com/", title="Example",
                                text=self._text[:min(text_limit, detect.BODY_LIMIT)])

    def close(self):
        pass


class TheSmallBudget(unittest.TestCase):

    def setUp(self):
        self.text = "".join(f"line {i:05d} of a long page\n" for i in range(3000))  # ~75K
        self._real = mcp_server.registry
        mcp_server.registry = SessionRegistry(lambda spec=None: TextBrowser(self.text))
        self.addCleanup(self.restore)

    def restore(self):
        mcp_server.registry.close_all()
        mcp_server.registry = self._real

    def test_the_environment_and_the_request_choose_it(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_COMPACT": "1"}):
            self.assertEqual(mcp_server.budget_name(), "compact")
            self.assertEqual(mcp_server.default_for("snapshot"), 3000)
        with mock.patch.dict(os.environ, {"LATCHKEY_COMPACT": ""}):
            self.assertEqual(mcp_server.budget_name(), "normal")
            self.assertEqual(mcp_server.budget_name("small"), "compact")
            self.assertEqual(mcp_server.default_for("text"), 4000)

    def test_text_says_where_the_rest_is_and_the_offset_reads_it(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_COMPACT": "1"}):
            first = mcp_server.tool_text()
        self.assertEqual(len(first["text"]), 2500)
        self.assertIn("offset=2500", first["more"])
        self.assertEqual(first["text_chars"], len(self.text[:detect.BODY_LIMIT].strip()))
        second = mcp_server.tool_text(limit=2500, offset=2500)
        self.assertTrue(second["text"].startswith(self.text[2500:2520]))
        self.assertEqual(second["offset"], 2500)

    def test_past_what_the_probe_carries_the_page_is_asked(self):
        out = mcp_server.tool_text(limit=1000, offset=60_000)
        self.assertEqual(out["text"], self.text[60_000:61_000])
        self.assertEqual(out["text_chars"], len(self.text))

    def test_a_compact_batch_is_cut_to_its_own_smaller_ceiling_and_says_so(self):
        out = mcp_server.tool_batch(calls=[{"tool": "latchkey_text",
                                            "args": {"limit": 30_000}}], budget="compact")
        self.assertLess(len(json.dumps(out, indent=2)), 13_000)
        self.assertIn("offset", out["note"])

    def test_a_budget_on_one_call_is_that_calls(self):
        out, failed = mcp_server._call_tool("latchkey_text", {"budget": "compact"})
        self.assertFalse(failed, out)
        self.assertEqual(len(out["text"]), 2500)
        self.assertIsNone(getattr(mcp_server._CALL, "budget", None), "and does not leak")

    def test_a_compact_page_reply_leaves_out_what_says_nothing(self):
        state = detect.PageState(url="https://example.com/", title="Example")
        with mock.patch.dict(os.environ, {"LATCHKEY_COMPACT": "1"}):
            out = mcp_server._page_reply(state)
        self.assertEqual(set(out) - {"tab", "tabs"}, {"url", "title", "verdict"})


class ListsSummariseInsteadOfPouring(unittest.TestCase):

    def cookie(self, host, name, value="v" * 40):
        return ck.Cookie(host=host, name=name, value=value, path="/", secure=True,
                         http_only=True, samesite=1, expires=-1.0, partitioned=False)

    def test_show_cookies_counts_then_leads_with_the_ones_that_matter(self):
        jar = [self.cookie(".example.com", f"pref_{i}") for i in range(60)]
        jar.append(self.cookie(".example.com", "session_token"))
        with mock.patch.object(ck, "load", return_value=jar):
            out = mcp_server.tool_show_cookies("example.com", limit=10)
        self.assertEqual(out["total"], 61)
        self.assertEqual(out["auth_like"], 1)
        self.assertEqual(out["cookies"][0]["name"], "session_token")
        self.assertEqual(len(out["cookies"]), 10)
        self.assertIn("51 more", out["more"])
        self.assertNotIn("v" * 40, json.dumps(out), "values stay masked")
        self.assertLess(len(json.dumps(out)), 2000)

    def test_sites_is_a_summary_with_a_way_to_more(self):
        jar = [self.cookie(f"host{i}.example", "a") for i in range(300)]
        with mock.patch.object(ck, "load", return_value=jar), \
                mock.patch.dict(os.environ, {"LATCHKEY_COMPACT": "1"}):
            out = mcp_server.tool_sites()
        self.assertEqual((out["hosts"], out["cookies"], len(out["top"])), (300, 300, 15))
        self.assertIn("285 more", out["more"])

    def test_eval_reports_a_large_result_instead_of_carrying_it(self):
        class Evaluator:
            label = "default"

            def evaluate(self, js):
                return ["x" * 100] * 300

            def close(self):
                pass

        real = mcp_server.registry
        mcp_server.registry = SessionRegistry(lambda spec=None: Evaluator())
        try:
            out = mcp_server.tool_eval("big()")
            self.assertEqual(len(out["result_cut"]), 8000)
            self.assertGreater(out["total_chars"], 30_000)
            self.assertIn(".slice", out["more"])
            self.assertEqual(len(mcp_server.tool_eval("big()", max_chars=40_000)), 300)
        finally:
            mcp_server.registry.close_all()
            mcp_server.registry = real

    def test_a_function_body_with_return_is_wrapped_rather_than_refused(self):
        class Evaluator:
            label = "default"
            seen = []

            def evaluate(self, js):
                self.seen.append(js)
                if js.startswith("return"):
                    raise RuntimeError("Page.evaluate: SyntaxError: Illegal return statement")
                return "Inbox"

            def close(self):
                pass

        real = mcp_server.registry
        mcp_server.registry = SessionRegistry(lambda spec=None: Evaluator())
        try:
            self.assertEqual(mcp_server.tool_eval("return document.title"), "Inbox")
        finally:
            mcp_server.registry.close_all()
            mcp_server.registry = real


if __name__ == "__main__":
    unittest.main()
