"""Offline tests for the MCP surface: sessions, routing, and the stdio server.

A fake "browser" stands in for Chrome, so these exercise the whole path an agent
takes - JSON-RPC in, session thread, browser verb, JSON-RPC out - including the
two promises that matter for parallel agents: two sessions never share a browser,
and a slow call in one does not hold up another.

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import mcp_server  # noqa: E402
from latchkey.detect import PageState  # noqa: E402
from latchkey.session import SessionSpec  # noqa: E402
from latchkey.sessions import SessionRegistry  # noqa: E402


class FakeAgent:
    """Stands in for Browser: records what the MCP tools asked it to do."""

    def __init__(self, label="", delay=0.0):
        self.label = label
        self.delay = delay
        self.url = "about:blank"
        self.calls = []
        self.closed = False
        self.clone_info = {}

    @property
    def report(self):
        return type("Report", (), {"as_dict": lambda _self: {"loaded": 3}})()

    def goto(self, url, settle_ms=4000, text_limit=4000):
        self.calls.append(("goto", url))
        self.url = url
        return PageState(url=url, title="Example", text="hello",
                         logged_in_marker="avatar")

    def state(self, text_limit=4000):
        self.calls.append(("state",))
        if self.delay:
            time.sleep(self.delay)
        return PageState(url=self.url, title="Example", text="hi there")

    def describe(self):
        return {"label": self.label, "host": None, "cursor": False}

    @property
    def profiles(self):
        return ["Default"]

    def screenshot(self, path, full_page=False, selector=None):
        self.calls.append(("screenshot", path))
        return path

    def close(self):
        self.closed = True


class LoggedOutAgent(FakeAgent):
    """A session that reads signed out, carrying nothing for the host.

    The shape of the reported case: the site *is* signed in, in another Chrome profile,
    and the session has no way to know that from the page alone.
    """

    def __init__(self, label="", host_cookies=0):
        super().__init__(label=label)
        self.host_cookies = host_cookies

    def goto(self, url, settle_ms=4000, text_limit=4000):
        self.calls.append(("goto", url))
        self.url = url
        return PageState(url=url, title="Sign in", text="Please sign in",
                         login_prompt="Sign in", host_cookies=self.host_cookies)

    def wait_for_login(self, url, timeout_s=None, cancel=None):
        self.calls.append(("wait_for_login", url, timeout_s))
        return {"status": "timeout"}


def registry_with_agents(startup_delay: float = 0.0, agent_factory=None):
    made = []

    def factory(spec=None):
        if startup_delay:
            time.sleep(startup_delay)
        if agent_factory is not None:
            agent = agent_factory(spec)
        else:
            agent = FakeAgent(label=getattr(spec, "label", "") or "",
                              delay=getattr(spec, "delay", 0.0) or 0.0)
        made.append(agent)
        return agent

    return SessionRegistry(factory=factory), made


def call(tool, **args):
    """What an agent's tool call looks like, end to end, as a dict."""
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": tool, "arguments": args}})
    text = response["result"]["content"][0]["text"]
    return response["result"]["isError"], text


class TestToolSurface(unittest.TestCase):
    def test_every_tool_is_dispatchable_and_nothing_else_is(self):
        listed = {tool["name"] for tool in mcp_server.CATALOG}
        self.assertEqual(listed | {mcp_server.BATCH}, set(mcp_server.DISPATCH))

    def test_the_only_advertised_tool_is_the_batch(self):
        """One schema per turn is the whole reason a batch exists: the rest of the
        catalog is reachable through it, and through a direct call."""
        self.assertEqual([tool["name"] for tool in mcp_server.TOOLS], [mcp_server.BATCH])

    def test_tool_names_are_unique(self):
        names = [tool["name"] for tool in mcp_server.CATALOG]
        self.assertEqual(len(names), len(set(names)))

    def test_the_session_tools_are_exposed(self):
        listed = {tool["name"] for tool in mcp_server.CATALOG}
        for name in ("latchkey_session_open", "latchkey_session_list",
                     "latchkey_session_close"):
            self.assertIn(name, listed)

    def test_driving_tools_all_take_a_session(self):
        """Routing a call to a named session is the whole point, so the argument
        has to be advertised on every tool that drives a page."""
        driving = {tool["name"] for tool in mcp_server.CATALOG
                   if tool["name"].startswith(
                       ("latchkey_open", "latchkey_act", "latchkey_text",
                        "latchkey_shot", "latchkey_screenshot", "latchkey_eval",
                        "latchkey_links", "latchkey_frames", "latchkey_frame_text",
                        "latchkey_sync", "latchkey_wait", "latchkey_pages",
                        "latchkey_new_page", "latchkey_switch", "latchkey_close_page",
                        "latchkey_history", "latchkey_save_session",
                        "latchkey_injected", "latchkey_use_"))}
        self.assertTrue(driving)
        for tool in mcp_server.CATALOG:
            if tool["name"] in driving:
                self.assertIn("session", tool["inputSchema"]["properties"],
                              f"{tool['name']} cannot be pointed at a session")

    def test_local_tools_do_not_take_a_session(self):
        by_name = {tool["name"]: tool for tool in mcp_server.CATALOG}
        for name in ("latchkey_sites", "latchkey_profiles", "latchkey_show_cookies",
                     "latchkey_credential_sources", "latchkey_clone_status"):
            self.assertNotIn("session", by_name[name]["inputSchema"]["properties"])

    def test_an_unknown_tool_is_an_error_not_a_silent_success(self):
        failed, text = call("latchkey_teleport")
        self.assertTrue(failed)
        self.assertIn("teleport", text)

    def test_a_bad_argument_is_reported_as_such(self):
        failed, text = call("latchkey_open", nonsense=1)
        self.assertTrue(failed)
        self.assertIn("bad arguments", text)


class TestTheBatchSurface(unittest.TestCase):
    """One advertised tool, and every other tool reachable through it by name."""

    def setUp(self):
        self.registry, self.agents = registry_with_agents()
        self._real = mcp_server.registry
        mcp_server.registry = self.registry
        self._real_resolve = mcp_server.ck.resolve_profiles
        mcp_server.ck.resolve_profiles = lambda names: names if isinstance(names, list) \
            else [names or "Default"]

    def tearDown(self):
        self.registry.close_all()
        mcp_server.registry = self._real
        mcp_server.ck.resolve_profiles = self._real_resolve

    def batch(self, calls, **arguments):
        """A batch as an agent sends one: through the tool, like any other call."""
        failed, text = call(mcp_server.BATCH, calls=calls, **arguments)
        return failed, json.loads(text)

    def test_the_description_indexes_every_tool_the_catalog_holds(self):
        """A name the advertised tool does not mention is a name an agent never learns,
        and a tool nobody can find is not a tool."""
        for spec in mcp_server.CATALOG:
            self.assertIn(spec["name"], mcp_server.TOOLS[0]["description"])

    def test_a_batch_runs_the_calls_of_one_session_in_order(self):
        failed, out = self.batch([
            {"tool": "latchkey_session_open", "args": {"name": "a"}},
            {"tool": "latchkey_open", "args": {"url": "https://a.example/",
                                                  "session": "a"}},
            {"tool": "latchkey_text", "args": {"session": "a"}},
        ])
        self.assertFalse(failed, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["ran"], 3)
        self.assertEqual([row["tool"] for row in out["results"]],
                         ["latchkey_session_open", "latchkey_open", "latchkey_text"])
        self.assertEqual(self.agents[0].calls, [("goto", "https://a.example/"), ("state",)])

    def test_a_result_is_the_result_a_direct_call_hands_back(self):
        call("latchkey_session_open", name="a")
        direct = json.loads(call("latchkey_open", url="https://a.example/",
                                 session="a")[1])
        _, out = self.batch([{"tool": "latchkey_open",
                              "args": {"url": "https://a.example/", "session": "a"}}])
        self.assertEqual(out["results"][0]["result"], direct)

    def test_a_failed_call_stops_the_batch_and_the_rest_say_so(self):
        failed, out = self.batch([
            {"tool": "latchkey_text", "args": {"session": "nope"}},
            {"tool": "latchkey_session_open", "args": {"name": "a"}},
        ])
        self.assertTrue(failed)
        self.assertEqual((out["ok"], out["ran"], out["failed"], out["skipped"]),
                         (False, 1, 1, 1))
        self.assertIn("latchkey_text failed", out["results"][1]["skipped"])
        self.assertEqual(self.agents, [], "a call after a failure must not run")

    def test_continue_on_error_runs_what_does_not_depend_on_what_failed(self):
        failed, out = self.batch([
            {"tool": "latchkey_session_open", "args": {"name": "a"}},
            {"tool": "latchkey_text", "args": {"session": "nope"}},
            {"tool": "latchkey_open", "args": {"url": "https://a.example/",
                                                  "session": "a"}},
        ], continue_on_error=True)
        self.assertTrue(failed)
        self.assertEqual([row["ok"] for row in out["results"]], [True, False, True])
        self.assertEqual(out["skipped"], 0)
        self.assertEqual(len(self.agents), 1)

    def test_parallel_lets_two_sessions_overlap_but_not_one(self):
        call("latchkey_session_open", name="a")
        call("latchkey_session_open", name="b")
        for agent in self.agents:
            agent.delay = 0.5
        calls = [{"tool": "latchkey_text", "args": {"session": "a"}},
                 {"tool": "latchkey_text", "args": {"session": "b"}}]
        started = time.time()
        _, out = self.batch(calls, parallel=True)
        overlapped = time.time() - started
        self.assertTrue(out["ok"])
        started = time.time()
        _, out = self.batch(calls)
        in_order = time.time() - started
        self.assertTrue(out["ok"])
        # Compared with each other rather than with a stopwatch a busy machine can move:
        # two 0.5s reads take 0.5s overlapped and 1.0s one after the other.
        self.assertGreater(in_order, 0.9, "in order means one call after the other")
        self.assertLess(overlapped, in_order - 0.3,
                        "two sessions should not wait on each other")

    def test_a_batch_inside_a_batch_is_refused_rather_than_recursed(self):
        _, out = self.batch([{"tool": "latchkey_batch", "args": {"calls": []}}])
        self.assertIn("cannot hold another batch", out["results"][0]["error"])

    def test_a_malformed_call_is_reported_against_the_call_that_is_wrong(self):
        failed, text = call(mcp_server.BATCH, calls=[{"args": {}}])
        self.assertTrue(failed)
        self.assertIn("call 0 names no tool", text)

    def test_calls_that_cannot_be_read_as_calls_at_all_are_refused(self):
        for calls in (5, 5.5, True):
            failed, text = call(mcp_server.BATCH, calls=calls)
            self.assertTrue(failed, calls)
            self.assertIn("not a list of calls", text)

    def test_the_shapes_a_small_model_actually_sends_are_read_not_refused(self):
        """One tool whose argument is a list of nested calls is the cheapest surface to
        carry and the hardest to emit. These are all a model that knew what it wanted."""
        self.assertEqual(
            mcp_server.calls_of('[{"tool":"open","args":{"url":"x.com"}}]'),
            [("latchkey_open", {"url": "x.com"})], "the whole list, stringified")
        self.assertEqual(
            mcp_server.calls_of({"tool": "latchkey_text", "args": {"limit": 10}}),
            [("latchkey_text", {"limit": 10})], "one call where a list was asked for")
        self.assertEqual(
            mcp_server.calls_of([{"name": "mcp__latchkey__latchkey_text",
                                  "arguments": '{"limit": 10}'}]),
            [("latchkey_text", {"limit": 10})], "the client's own prefix, stringified args")
        self.assertEqual(mcp_server.calls_of(["latchkey_session_list"]),
                         [("latchkey_session_list", {})], "a bare name for a bare call")
        self.assertEqual(mcp_server.calls_of([{"tool": "latchkey_open", "url": "x.com"}]),
                         [("latchkey_open", {"url": "x.com"})], "arguments beside the name")

    def test_a_name_that_is_only_close_says_which_one_was_meant(self):
        with self.assertRaises(ValueError) as caught:
            mcp_server.calls_of([{"tool": "latchkey_opn", "args": {}}])
        message = str(caught.exception)
        self.assertIn("latchkey_open", message)
        self.assertIn("latchkey_help", message)

    def test_a_batch_with_no_calls_is_not_an_error(self):
        failed, out = self.batch([])
        self.assertFalse(failed)
        self.assertEqual((out["ok"], out["ran"], out["results"]), (True, 0, []))

    def test_the_reply_is_cut_to_fit_rather_than_cut_mid_value(self):
        call("latchkey_session_open", name="a")
        long_text = "x" * 30_000
        self.agents[0].state = lambda text_limit=4000: PageState(
            url="https://a.example/", title="Example", text=long_text)
        failed, text = call(mcp_server.BATCH,
                            calls=[{"tool": "latchkey_text",
                                    "args": {"session": "a", "limit": 30_000}}],
                            max_chars=4000)
        self.assertFalse(failed)
        self.assertLess(len(text), 6000, "the batch cuts its own reply, not the client")
        self.assertGreater(json.loads(text)["cut_chars"], 20_000)

    def test_help_lists_what_a_batch_can_call(self):
        _, text = call("latchkey_help")
        listed = json.loads(text)
        self.assertEqual(listed["count"], len(mcp_server.CATALOG))
        self.assertEqual([row["name"] for row in listed["tools"]],
                         [spec["name"] for spec in mcp_server.CATALOG])

    def test_help_describes_one_tool_in_full(self):
        _, text = call("latchkey_help", name="latchkey_act")
        row = json.loads(text)
        self.assertIn("actions", row["inputSchema"]["properties"])
        self.assertIn("click", row["description"])
        self.assertIn("actions", row["args"])

    def test_help_on_a_name_that_does_not_exist_says_so(self):
        failed, text = call("latchkey_help", name="latchkey_teleport")
        self.assertTrue(failed)
        self.assertIn("latchkey_teleport", text)


class TestSessionRouting(unittest.TestCase):
    def setUp(self):
        self.registry, self.agents = registry_with_agents()
        self._real = mcp_server.registry
        mcp_server.registry = self.registry
        self._real_resolve = mcp_server.ck.resolve_profiles
        mcp_server.ck.resolve_profiles = lambda names: names if isinstance(names, list) \
            else [names or "Default"]

    def tearDown(self):
        self.registry.close_all()
        mcp_server.registry = self._real
        mcp_server.ck.resolve_profiles = self._real_resolve

    def test_the_default_session_opens_itself_on_first_use(self):
        """"Call latchkey_session_open first" as the answer to the first latchkey_open is
        a step that exists only to be performed, and a model that skipped it is usually
        the one least able to recover from being refused."""
        failed, text = call("latchkey_open", url="https://example.com/")
        self.assertFalse(failed, text)
        self.assertIn("default", self.registry.names())

    def test_a_named_session_that_does_not_exist_is_still_an_error(self):
        """A typo in a name should be an error, not a second silent browser."""
        failed, text = call("latchkey_open", url="https://example.com/", session="canvs")
        self.assertTrue(failed)
        self.assertIn("latchkey_session_open", text)
        self.assertNotIn("canvs", self.registry.names())

    def test_open_then_use_a_named_session(self):
        failed, text = call("latchkey_session_open", name="canvas", mode="inject")
        self.assertFalse(failed, text)
        self.assertIn("canvas", text)

        failed, text = call("latchkey_open", url="https://canvas.example/", session="canvas")
        self.assertFalse(failed, text)
        self.assertIn("logged-in", text)
        self.assertEqual(self.agents[0].calls, [("goto", "https://canvas.example/")])

    def test_each_session_gets_its_own_browser(self):
        call("latchkey_session_open", name="a", label="a")
        call("latchkey_session_open", name="b", label="b")
        call("latchkey_open", url="https://a.example/", session="a")
        call("latchkey_open", url="https://b.example/", session="b")
        self.assertEqual(len(self.agents), 2)
        urls = [[c for c in agent.calls if c[0] == "goto"] for agent in self.agents]
        self.assertEqual(sorted(urls), [[("goto", "https://a.example/")],
                                        [("goto", "https://b.example/")]])

    def test_the_cursor_choice_travels_with_the_spec(self):
        mcp_server.tool_session_open(name="watched", cursor=True)
        self.assertTrue(self.registry.spec_of("watched").show_cursor)

    def test_the_read_only_choice_travels_with_the_spec(self):
        """A session opened to read must arrive at the browser read-only, and must say
        so in what the session list reports back."""
        failed, text = call("latchkey_session_open", name="reader", read_only=True)
        self.assertFalse(failed, text)
        spec = self.registry.spec_of("reader")
        self.assertTrue(spec.read_only)
        self.assertTrue(spec.as_dict()["read_only"])

    def test_a_typo_in_the_session_name_is_an_error_not_a_second_browser(self):
        call("latchkey_session_open", name="canvas")
        failed, text = call("latchkey_text", session="canvs")
        self.assertTrue(failed)
        self.assertIn("canvs", text)
        self.assertIn("canvas", text, "the error should list the sessions that do exist")
        self.assertEqual(len(self.agents), 1)

    def test_session_list_reports_the_open_sessions(self):
        call("latchkey_session_open", name="a", label="alpha")
        described = json.loads(call("latchkey_session_list")[1])
        self.assertEqual([row["name"] for row in described], ["a"])
        self.assertEqual(described[0]["label"], "alpha")

    def test_closing_a_session_closes_its_browser_and_frees_the_name(self):
        call("latchkey_session_open", name="canvas")
        failed, text = call("latchkey_session_close", name="canvas")
        self.assertFalse(failed, text)
        self.assertTrue(self.agents[0].closed)
        self.assertEqual(json.loads(call("latchkey_session_list")[1]), [])
        self.assertTrue(call("latchkey_text", session="canvas")[0])

    def test_close_closes_everything(self):
        call("latchkey_session_open", name="a")
        call("latchkey_session_open", name="b")
        failed, text = call("latchkey_close")
        self.assertFalse(failed, text)
        self.assertEqual(json.loads(text), {"closed": 2})
        self.assertTrue(all(agent.closed for agent in self.agents))

    def test_restarting_on_profiles_keeps_the_rest_of_the_session_shape(self):
        """use_profiles is a restart, not a reset: a session watching with a cursor
        and pinned to a host should still be that after it comes back."""
        call("latchkey_session_open", name="a", mode="clone", cursor=True, host="x.example")
        mcp_server.tool_use_profiles(["Work"], session="a")
        spec = self.registry.spec_of("a")
        self.assertEqual(spec.profiles, ["Work"])
        self.assertEqual(spec.mode, "clone")
        self.assertTrue(spec.show_cursor)
        self.assertEqual(spec.host, "x.example")

    def test_clone_restart_keeps_the_label_and_profiles(self):
        call("latchkey_session_open", name="a", profiles=["Work"], label="mail")
        mcp_server.tool_use_clone(session="a")
        spec = self.registry.spec_of("a")
        self.assertEqual(spec.mode, "clone")
        self.assertEqual(spec.profiles, ["Work"])
        self.assertEqual(spec.label, "mail")


class TestStdioServer(unittest.TestCase):
    """The transport: one slow tool call must not stall the connection."""

    def setUp(self):
        self.registry, self.agents = registry_with_agents()
        self._real = mcp_server.registry
        mcp_server.registry = self.registry

    def tearDown(self):
        self.registry.close_all()
        mcp_server.registry = self._real

    def run_server(self, lines, workers=8):
        stdin = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        stdout = io.StringIO()
        server = mcp_server.StdioServer(stdin=stdin, stdout=stdout, workers=workers)
        started = time.time()
        server.serve_forever()
        return [json.loads(line) for line in stdout.getvalue().splitlines()], \
            time.time() - started

    def test_the_handshake_is_answered_without_a_browser(self):
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": mcp_server.PROTOCOL_VERSION}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual([r["id"] for r in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "latchkey")
        self.assertTrue(responses[1]["result"]["tools"])

    def test_a_slow_tool_call_does_not_block_the_handshake(self):
        call("latchkey_session_open", name="a")
        self.agents[0].delay = 0.5
        responses, elapsed = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "latchkey_text", "arguments": {"session": "a"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ])
        self.assertEqual([r["id"] for r in responses], [1, 3, 2],
                         "the blocking call must not hold up the ones behind it")
        self.assertGreaterEqual(elapsed, 0.5, "the slow call still had to finish")

    def test_two_sessions_work_at_the_same_time(self):
        """Two agents, two sessions, one blocked page each: the wall clock should
        be one wait, not two."""
        call("latchkey_session_open", name="a")
        call("latchkey_session_open", name="b")
        for agent in self.agents:
            agent.delay = 0.5
        _, elapsed = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_text", "arguments": {"session": "a"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "latchkey_text", "arguments": {"session": "b"}}},
        ])
        self.assertLess(elapsed, 0.9, f"two sessions took {elapsed:.2f}s; they should "
                                     f"overlap, not queue")

    def test_the_call_that_opens_a_session_is_not_overtaken_by_the_one_that_uses_it(self):
        """Regression, found by driving the real server over stdio: session_open and
        the call that needs that session arrive together, and without lanes the second
        one ran first and failed with "no open session named ...". A slow startup here
        makes that race certain rather than occasional."""
        slow, _ = registry_with_agents(startup_delay=0.3)
        mcp_server.registry = slow
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_session_open", "arguments": {"name": "a"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "latchkey_open",
                        "arguments": {"url": "https://a.example/", "session": "a"}}},
        ])
        self.assertEqual([r["id"] for r in responses], [1, 2],
                         "the open overtook the session it needed")
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertFalse(responses[1]["result"]["isError"],
                         responses[1]["result"]["content"][0]["text"][:200])

    def test_calls_to_one_session_stay_in_order(self):
        """act-then-read has to see its own effect, so commands for the same
        session are queued in arrival order even though dispatch is concurrent."""
        call("latchkey_session_open", name="a")
        order = []
        agent = self.agents[0]
        real_state = agent.state

        def slow_state(text_limit=4000):
            order.append("first")
            time.sleep(0.2)
            return real_state(text_limit)

        agent.state = slow_state
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_text", "arguments": {"session": "a"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "latchkey_text", "arguments": {"session": "a"}}},
        ])
        self.assertEqual([r["id"] for r in responses], [1, 2],
                         "responses came back out of order for one session")
        self.assertEqual(order, ["first", "first"])

    def test_a_tool_that_raises_comes_back_as_an_error_not_a_dead_server(self):
        call("latchkey_session_open", name="a")

        def explode(*args, **kwargs):
            raise ValueError("page blew up")

        self.agents[0].screenshot = explode
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_screenshot", "arguments": {"session": "a"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ])
        by_id = {r["id"]: r for r in responses}
        self.assertTrue(by_id[1]["result"]["isError"])
        self.assertIn("page blew up", by_id[1]["result"]["content"][0]["text"])
        self.assertIn("result", by_id[2], "the server must still answer afterwards")

    def test_garbage_and_unknown_methods_do_not_kill_the_server(self):
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "no/such"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ])
        by_id = {r["id"]: r for r in responses}
        self.assertEqual(by_id[1]["error"]["code"], -32601)
        self.assertIn("result", by_id[2])

    def test_broken_json_is_skipped(self):
        stdin = io.StringIO("{oops\n" + json.dumps({"jsonrpc": "2.0", "id": 1,
                                                    "method": "ping"}) + "\n")
        stdout = io.StringIO()
        server = mcp_server.StdioServer(stdin=stdin, stdout=stdout)
        self.assertEqual(server.serve_forever(), 0)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)

    def test_a_client_that_hangs_up_does_not_produce_a_traceback(self):
        class Rude(io.StringIO):
            def write(self, _text):
                raise BrokenPipeError("gone")

        server = mcp_server.StdioServer(
            stdin=io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1,
                                          "method": "ping"}) + "\n"),
            stdout=Rude())
        self.assertEqual(server.serve_forever(), 0)


    def test_a_batch_keeps_one_session_in_order_against_a_direct_call(self):
        """A batch is nobody's single request, so its calls take their own place in the
        lane - and the call sent after the batch waits for the batch."""
        seen = []

        class WatchedAgent(FakeAgent):
            def state(self, text_limit=4000):
                seen.append("enter")
                time.sleep(0.15)
                seen.append("leave")
                return super().state(text_limit)

        watched, agents = registry_with_agents(agent_factory=lambda spec: WatchedAgent())
        mcp_server.registry = watched
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_session_open", "arguments": {"name": "a"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "latchkey_batch",
                        "arguments": {"calls": [
                            {"tool": "latchkey_text", "args": {"session": "a"}},
                            {"tool": "latchkey_text", "args": {"session": "a"}}]}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "latchkey_text", "arguments": {"session": "a"}}},
        ])
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(seen, ["enter", "leave"] * 3,
                         "a batch's calls must not interleave with what follows it")
        self.assertEqual(len(agents), 1)

    def test_a_batch_that_opens_a_session_then_drives_it_keeps_that_order(self):
        """The regression the lanes exist for, now inside a single request."""
        slow, agents = registry_with_agents(startup_delay=0.3)
        mcp_server.registry = slow
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_batch",
                        "arguments": {"calls": [
                            {"tool": "latchkey_session_open", "args": {"name": "a"}},
                            {"tool": "latchkey_open",
                             "args": {"url": "https://a.example/", "session": "a"}}]}}},
        ])
        out = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual([row["ok"] for row in out["results"]], [True, True], out)
        self.assertEqual(len(agents), 1, "the session must be opened once")

    def test_a_batch_can_mix_in_calls_that_name_no_session(self):
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_batch",
                        "arguments": {"calls": [
                            {"tool": "latchkey_session_open", "args": {"name": "a"}},
                            {"tool": "latchkey_session_list", "args": {}},
                            {"tool": "latchkey_text", "args": {"session": "a"}},
                            {"tool": "latchkey_close", "args": {}}]}}},
        ])
        out = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual([row["ok"] for row in out["results"]], [True] * 4, out)
        self.assertEqual([row["name"] for row in out["results"][1]["result"]], ["a"],
                         "the list must see the session the call before it opened")

    def test_a_batch_of_two_sessions_overlaps_only_when_asked(self):
        slow, agents = registry_with_agents(agent_factory=lambda spec: FakeAgent(delay=0.4))
        mcp_server.registry = slow
        opens = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "latchkey_session_open",
                             "arguments": {"name": "a"}}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "latchkey_session_open",
                             "arguments": {"name": "b"}}}]
        batch = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "latchkey_batch",
                            "arguments": {
                                "parallel": True,
                                "calls": [
                                    {"tool": "latchkey_text",
                                     "args": {"session": "a"}},
                                    {"tool": "latchkey_text",
                                     "args": {"session": "b"}}]}}}
        responses, overlapped = self.run_server(opens + [batch])
        # Ids 1 and 2 name different sessions, so they are on different lanes and may
        # answer in either order - opening two browsers is two browsers starting at
        # once, not one after the other. JSON-RPC does not promise response order, and
        # the batch (3) is what has to wait for both.
        self.assertEqual(sorted(response["id"] for response in responses), [1, 2, 3])
        responses = sorted(responses, key=lambda response: response["id"])
        out = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertTrue(out["ok"], out)
        batch["params"]["arguments"]["parallel"] = False
        responses, in_order = self.run_server(opens + [batch])
        responses = sorted(responses, key=lambda response: response["id"])
        out = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertTrue(out["ok"], out)
        # Compared with each other rather than with a stopwatch a busy machine can move.
        self.assertGreater(in_order, 0.6, "in order means one call after the other")
        self.assertLess(overlapped, in_order - 0.2,
                        "two sessions should not wait on each other")
        self.assertEqual(len(agents), 4, "one browser per session, per run")

    def test_a_malformed_batch_is_answered_rather_than_left_hanging(self):
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_batch", "arguments": {"calls": [{"tool": 7}]}}},
        ])
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("call 0 names no tool", responses[0]["result"]["content"][0]["text"])

    def test_arguments_that_are_not_an_object_do_not_take_the_server_down(self):
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_batch", "arguments": "nope"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "latchkey_text", "arguments": 7}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ])
        by_id = {response["id"]: response for response in responses}
        self.assertEqual(sorted(by_id), [1, 2, 3], "the loop must keep reading")
        self.assertTrue(by_id[1]["result"]["isError"])
        self.assertTrue(by_id[2]["result"]["isError"])

    def test_a_queued_call_is_not_dropped_when_the_client_stops_talking(self):
        """The loop ends with stdin, but the work already handed to the pool must still
        answer: one worker, two calls, both replies."""
        real_load = mcp_server.ck.load

        def slow_load(domain=None):
            time.sleep(0.2)
            return []

        mcp_server.ck.load = slow_load
        try:
            responses, _ = self.run_server([
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "latchkey_sites", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "latchkey_sites", "arguments": {}}},
            ], workers=1)
        finally:
            mcp_server.ck.load = real_load
        self.assertEqual([response["id"] for response in responses], [1, 2])

    def test_an_empty_batch_comes_back_empty(self):
        responses, _ = self.run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "latchkey_batch", "arguments": {"calls": []}}},
        ])
        out = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["ran"], 0)


class TestSessionsSurviveTheBus(unittest.TestCase):
    def test_opening_and_closing_publish_on_the_bus(self):
        from latchkey.events import bus

        seen = []
        off = bus.subscribe(seen.append)
        registry, _ = registry_with_agents()
        try:
            registry.get("canvas", spec=SessionSpec(label="canvas"))
            registry.close("canvas")
        finally:
            off()
        actions = [e.detail.get("action") for e in seen if e.kind == "session"]
        self.assertEqual(actions, ["open", "close"])
        self.assertEqual(seen[0].session, "canvas")


class TestProfileHints(unittest.TestCase):
    """The reported bug, end to end: `['all']` resolved, and the hint that saves an agent
    from finding the right profile by hand.

    The report: `use_profiles(['all'])` raised `KeyError: unknown profile(s): all`, and no
    tool would say which profile held the site's session - so the agent read Chrome's
    cookie SQLite files and History to discover `profiles=['Profile 3']` by hand.
    """

    def setUp(self):
        self.host_cookies = 0
        self.registry, self.agents = registry_with_agents(
            agent_factory=lambda spec=None: LoggedOutAgent(host_cookies=self.host_cookies))
        self._real_registry = mcp_server.registry
        self._real_counts = mcp_server.ck.host_counts
        self._real_resolve = mcp_server.ck.resolve_profiles
        mcp_server.registry = self.registry
        mcp_server.ck.host_counts = lambda host: {
            "Default": {"cookies": 0, "auth_like": 0},
            "Profile 3": {"cookies": 42, "auth_like": 3},
        }
        self._old_sso_accounts = os.environ.get(mcp_server.google.GOOGLE_SSO_ACCOUNTS_ENV)
        os.environ[mcp_server.google.GOOGLE_SSO_ACCOUNTS_ENV] = '{"portal.example.edu": "school"}'

    def tearDown(self):
        self.registry.close_all()
        mcp_server.registry = self._real_registry
        mcp_server.ck.host_counts = self._real_counts
        mcp_server.ck.resolve_profiles = self._real_resolve
        if self._old_sso_accounts is None:
            os.environ.pop(mcp_server.google.GOOGLE_SSO_ACCOUNTS_ENV, None)
        else:
            os.environ[mcp_server.google.GOOGLE_SSO_ACCOUNTS_ENV] = self._old_sso_accounts

    def test_the_shorthand_resolves_through_the_tool(self):
        mcp_server.ck.resolve_profiles = lambda spec: ["Default", "Profile 2",
                                                       "Profile 3"]
        result = mcp_server.tool_use_profiles(["all"], session="canvas")
        self.assertEqual(result["profiles"], ["Default", "Profile 2", "Profile 3"])

    def test_a_typo_in_a_profile_name_is_reported_to_the_agent(self):
        def refuse(spec):
            raise KeyError("unknown profile(s): Profile 9; have: Default, Profile 3")

        mcp_server.ck.resolve_profiles = refuse
        with self.assertRaises(ValueError) as caught:
            mcp_server.tool_session_open(name="a", profiles=["Profile 9"])
        self.assertIn("Profile 9", str(caught.exception))

    def test_a_logged_out_site_points_at_the_profile_that_has_the_session(self):
        self.registry.get("a", spec=None)
        out = mcp_server.tool_open("https://www.webassign.net/", session="a")
        self.assertEqual(out["verdict"], "logged-out")
        hint = out["other_profiles"]
        self.assertIn("Profile 3", hint)
        self.assertIn("42", hint)
        self.assertIn("profile_recommendation.calls", hint)
        self.assertNotIn("Default holds", hint, "the session's own profile is not a lead")

    def test_a_junk_current_cookie_prefers_the_authenticated_profile_before_login_wait(self):
        """A non-session Campus cookie in Default must not hide Profile 3's login.

        The primary next step is deliberately asserted here: agents naturally obey
        `next_step` before supplemental fields such as `other_profiles`.
        """
        self.host_cookies = 1
        mcp_server.ck.host_counts = lambda host: {
            "Default": {"cookies": 1, "auth_like": 0},
            "Profile 3": {"cookies": 18, "auth_like": 2},
        }
        self.registry.get("a", spec=None)

        out = mcp_server.tool_open("https://campus.example.edu/campus/portal/students", session="a")

        self.assertEqual(out["verdict"], "logged-out")
        self.assertIn("Profile 3", out["other_profiles"])
        self.assertIn("18", out["other_profiles"])
        self.assertNotIn("holds no cookies", out["other_profiles"])
        recommendation = out["profile_recommendation"]
        self.assertEqual(recommendation["profile"], "Profile 3")
        self.assertEqual(recommendation["calls"][0], {
            "tool": "latchkey_use_profiles",
            "args": {"profiles": ["Profile 3"], "session": "a"},
        })
        self.assertEqual(recommendation["calls"][1], {
            "tool": "latchkey_open",
            "args": {"url": "https://campus.example.edu/campus/portal/students",
                     "session": "a"},
        })
        self.assertIn("profile_recommendation.calls", out["next_step"])
        self.assertNotIn("latchkey_wait_for_login", out["next_step"])
        self.assertLess(len(json.dumps(out)), 1500)

    def test_a_configured_portal_points_directly_at_its_google_sso_control(self):
        self.registry.get("campus", spec=SessionSpec(mode="clone"))
        real_find = mcp_server.a11y.find
        mcp_server.a11y.find = lambda browser, query, limit: {
            "query": query, "found": 1, "matches": [{
                "ref": "e6", "role": "link",
                "name": "Google Single Sign-On (SSO)",
            }]}
        try:
            out = mcp_server.tool_open(
                "https://portal.example.edu/campus/portal/students/",
                session="campus")
        finally:
            mcp_server.a11y.find = real_find

        self.assertEqual(out["sso_recommendation"]["call"], {
            "tool": "latchkey_act",
            "args": {"actions": [{"do": "click", "ref": "e6"}],
                     "session": "campus"},
        })
        self.assertIn("sso_recommendation.call", out["next_step"])
        self.assertNotIn("wait_for_login", out["next_step"])

    def test_a_google_account_chooser_is_an_identity_choice_not_a_login_failure(self):
        url = "https://accounts.google.com/v3/signin/accountchooser?continue=campus"
        next_step = mcp_server.google_account_chooser_next_step(url)
        self.assertIn("latchkey_snapshot", next_step)
        self.assertIn("intended signed-in account", next_step)
        self.assertNotIn("sign in in their normal Chrome", next_step)

    def test_a_current_profile_with_an_equally_strong_session_is_not_recommended_away(self):
        self.host_cookies = 2
        mcp_server.ck.host_counts = lambda host: {
            "Default": {"cookies": 18, "auth_like": 2},
            "Profile 3": {"cookies": 1, "auth_like": 0},
        }
        self.registry.get("a", spec=None)

        out = mcp_server.tool_open("https://campus.example.edu/campus/portal/students", session="a")

        self.assertNotIn("other_profiles", out)
        self.assertIn("latchkey_wait_for_login", out["next_step"])

    def test_wait_for_login_honours_the_current_profile_then_advises_after_timeout(self):
        self.host_cookies = 1
        mcp_server.ck.host_counts = lambda host: {
            "Default": {"cookies": 1, "auth_like": 0},
            "Profile 3": {"cookies": 18, "auth_like": 2},
        }
        self.registry.get("a", spec=None)

        out = mcp_server.tool_wait_for_login(
            "https://campus.example.edu/campus/portal/students", timeout_s=1, session="a")

        self.assertEqual(out["status"], "timeout")
        self.assertEqual(out["profile_recommendation"]["profile"], "Profile 3")
        self.assertEqual(out["profile_recommendation"]["calls"][0], {
            "tool": "latchkey_use_profiles",
            "args": {"profiles": ["Profile 3"], "session": "a"},
        })
        self.assertIn("profile_recommendation.calls", out["next_step"])
        self.assertTrue(any(call[0] == "wait_for_login" for call in self.agents[0].calls))

    def test_a_chunked_login_wait_keeps_waiting_before_suggesting_another_identity(self):
        mcp_server.ck.host_counts = lambda host: {
            "Default": {"cookies": 1, "auth_like": 0},
            "Profile 3": {"cookies": 18, "auth_like": 2},
        }
        self.registry.get("a", spec=None)

        out = mcp_server.tool_wait_for_login(
            "https://campus.example.edu/campus/portal/students", timeout_s=300, session="a")

        self.assertEqual(out["status"], "timeout")
        self.assertNotIn("profile_recommendation", out)
        self.assertIn("again", out["next_step"])
        self.assertEqual(out["remaining_s"], 275)
        self.assertEqual(out["continuation_call"], {
            "tool": "latchkey_wait_for_login",
            "args": {"url": "https://campus.example.edu/campus/portal/students",
                     "timeout_s": 275, "session": "a"},
        })

    def test_following_continuation_calls_reaches_the_final_profile_advice(self):
        mcp_server.ck.host_counts = lambda host: {
            "Default": {"cookies": 1, "auth_like": 0},
            "Profile 3": {"cookies": 18, "auth_like": 2},
        }
        self.registry.get("a", spec=None)
        args = {"url": "https://campus.example.edu/campus/portal/students",
                "timeout_s": 75, "session": "a"}

        for _ in range(4):
            out = mcp_server.tool_wait_for_login(**args)
            if "continuation_call" not in out:
                break
            args = out["continuation_call"]["args"]

        self.assertEqual(out["status"], "timeout")
        self.assertNotIn("continuation_call", out)
        self.assertEqual(out["profile_recommendation"]["profile"], "Profile 3")

    def test_a_long_wait_never_continues_after_success_or_abandonment(self):
        self.registry.get("a", spec=None)
        agent = self.agents[0]

        for status in ("logged-in", "already-logged-in", "abandoned"):
            with self.subTest(status=status):
                agent.wait_for_login = lambda *args, _status=status, **kwargs: {
                    "status": _status}
                out = mcp_server.tool_wait_for_login(
                    "https://campus.example.edu/campus/portal/students",
                    timeout_s=300, session="a")
                self.assertEqual(out["status"], status)
                self.assertNotIn("continuation_call", out)
                self.assertNotIn("next_step", out)

    def test_clone_mode_never_recommends_a_profile_switch_it_cannot_apply(self):
        self.host_cookies = 1
        mcp_server.ck.host_counts = lambda host: {
            "Default": {"cookies": 1, "auth_like": 0},
            "Profile 3": {"cookies": 18, "auth_like": 2},
        }
        self.registry.get("a", spec=SessionSpec(mode="clone"))

        out = mcp_server.tool_open(
            "https://campus.example.edu/campus/portal/students", session="a")

        self.assertNotIn("profile_recommendation", out)
        self.assertIn("latchkey_wait_for_login", out["next_step"])

    def test_no_hint_when_the_session_already_reads_the_only_profile_with_cookies(self):
        mcp_server.ck.host_counts = lambda host: {"Default": {"cookies": 9,
                                                              "auth_like": 2}}
        self.registry.get("a", spec=None)
        out = mcp_server.tool_open("https://example.com/", session="a")
        self.assertNotIn("other_profiles", out)

    def test_no_hint_when_no_profile_holds_anything(self):
        mcp_server.ck.host_counts = lambda host: {"Default": {"cookies": 0,
                                                              "auth_like": 0}}
        self.registry.get("a", spec=None)
        out = mcp_server.tool_open("https://example.com/", session="a")
        self.assertNotIn("other_profiles", out)

    def test_a_cookie_read_that_fails_does_not_break_open(self):
        def explode(host):
            raise RuntimeError("no keychain")

        mcp_server.ck.host_counts = explode
        self.registry.get("a", spec=None)
        out = mcp_server.tool_open("https://example.com/", session="a")
        self.assertEqual(out["verdict"], "logged-out")

    def test_the_profiles_tool_answers_per_host_and_leads_with_the_best(self):
        rows = mcp_server.tool_profiles(host="webassign.net")
        self.assertEqual(rows[0]["profile"], "Profile 3")
        self.assertEqual(rows[0]["cookies"], 42)
        self.assertEqual(rows[0]["auth_like"], 3)


class TestNothingDeadlocks(unittest.TestCase):
    """Regression: the per-session lock used to be a plain Lock, so a tool that
    restarts a session and then asks it something - session_open, use_profiles,
    use_clone - self-deadlocked until the startup timeout. A hung suite is a
    miserable way to learn that, so these run on a watch thread."""

    def setUp(self):
        self.registry, self.agents = registry_with_agents()
        self._real = mcp_server.registry
        mcp_server.registry = self.registry
        self._real_resolve = mcp_server.ck.resolve_profiles
        mcp_server.ck.resolve_profiles = lambda names: ["Default"]

    def tearDown(self):
        self.registry.close_all()
        mcp_server.registry = self._real
        mcp_server.ck.resolve_profiles = self._real_resolve

    def in_time(self, fn, seconds=5.0):
        thread = threading.Thread(target=fn, daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), f"{fn} never returned (deadlock?)")

    def test_session_open_returns(self):
        self.in_time(lambda: call("latchkey_session_open", name="a"))

    def test_session_open_on_an_existing_name_returns(self):
        call("latchkey_session_open", name="a")
        self.in_time(lambda: call("latchkey_session_open", name="a"))

    def test_use_profiles_returns(self):
        call("latchkey_session_open", name="a")
        self.in_time(lambda: mcp_server.tool_use_profiles(["Default"], session="a"))

    def test_use_clone_returns(self):
        call("latchkey_session_open", name="a")
        self.in_time(lambda: mcp_server.tool_use_clone(session="a"))

    def test_recreate_returns(self):
        call("latchkey_session_open", name="a")
        self.in_time(lambda: call("latchkey_session_open", name="a", recreate=True))


class TestLanesKeepOrder(unittest.TestCase):
    """Ordering for one session, parallelism between sessions. A session's lane is
    its own FIFO worker, fed by the single reader thread, so arrival order is what
    decides execution order."""

    def test_every_tool_declares_which_argument_names_its_session(self):
        self.assertEqual(set(mcp_server.LANE_OF), set(mcp_server.DISPATCH))

    def test_one_lane_runs_its_tasks_in_order(self):
        lane = mcp_server.Lane("t")
        seen = []
        for i in range(25):
            lane.submit(lambda i=i: (time.sleep(0.001), seen.append(i)))
        lane.drain()
        self.assertEqual(seen, list(range(25)))

    def test_one_lane_does_not_lose_a_task_the_worker_is_holding(self):
        real = mcp_server.IDLE_LANE_S
        mcp_server.IDLE_LANE_S = 0.05
        try:
            lane = mcp_server.Lane("t")
            seen = []
            lane.submit(lambda: seen.append(1))
            lane.drain()
            time.sleep(0.3)                   # the worker retires here
            lane.submit(lambda: seen.append(2))   # and must wake up again
            lane.drain()
            self.assertEqual(seen, [1, 2])
        finally:
            mcp_server.IDLE_LANE_S = real

    def test_different_lanes_do_not_wait_on_each_other(self):
        lanes = mcp_server.Lanes()
        done = []
        lanes.for_name("slow").submit(lambda: (time.sleep(0.4), done.append("slow")))
        lanes.for_name("fast").submit(lambda: done.append("fast"))
        lanes.for_name("fast").drain()
        self.assertIn("fast", done)
        self.assertNotIn("slow", done, "the fast lane waited for the slow one")
        lanes.drain()

    def test_the_same_name_gets_the_same_lane(self):
        lanes = mcp_server.Lanes()
        self.assertIs(lanes.for_name("a"), lanes.for_name("a"))
        self.assertIsNot(lanes.for_name("a"), lanes.for_name("b"))

    def test_lane_lookup_is_thread_safe(self):
        lanes = mcp_server.Lanes()

        def grab():
            for _ in range(50):
                lanes.for_name("a")

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertIs(lanes.for_name("a"), lanes.for_name("a"))

    def test_a_lane_name_comes_from_the_arguments(self):
        server = mcp_server.StdioServer(stdin=io.StringIO(), stdout=io.StringIO())
        self.assertEqual(
            server._lane_name({"method": "tools/call",
                               "params": {"name": "latchkey_text",
                                          "arguments": {"session": "mail"}}}), "mail")
        self.assertEqual(
            server._lane_name({"method": "tools/call",
                               "params": {"name": "latchkey_text", "arguments": {}}}),
            mcp_server.DEFAULT_NAME)
        self.assertEqual(
            server._lane_name({"method": "tools/call",
                               "params": {"name": "latchkey_session_close",
                                          "arguments": {"name": "canvas"}}}), "canvas")
        self.assertIsNone(
            server._lane_name({"method": "tools/call",
                               "params": {"name": "latchkey_sites", "arguments": {}}}))
        self.assertIsNone(server._lane_name({"method": "tools/list"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
