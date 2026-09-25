"""Offline tests. No browser, no network, no Keychain.

    cd ~/tools/latchkey
    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import credentials, profile, store  # noqa: E402
from latchkey import cookies as ck  # noqa: E402
from latchkey.automation import apply_actions  # noqa: E402
from latchkey.detect import PageState, _site_hints  # noqa: E402
from latchkey.mcp_server import BATCH, CATALOG, DISPATCH, TOOLS, handle  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

KEY = b"0123456789abcdef"


def encrypt_cbc(key, iv, data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def cookie(name="c", value="v", host=".example.com", secure=True,
           http_only=False, partitioned=False, partition_site=None,
           expires=1893456000.0, samesite=1) -> ck.Cookie:
    return ck.Cookie(host=host, name=name, value=value, path="/", secure=secure,
                     http_only=http_only, samesite=samesite, expires=expires,
                     partitioned=partitioned, partition_site=partition_site)


class TestDecrypt(unittest.TestCase):
    def test_classic_layout(self):
        blob = b"v10" + encrypt_cbc(KEY, b" " * 16, b"hello world")
        self.assertEqual(ck.decrypt(blob, KEY), b"hello world")

    def test_extended_header_layout(self):
        """The layout this machine actually uses: 32-byte header before the ciphertext."""
        header = bytes(range(32))
        blob = b"v10" + header + encrypt_cbc(KEY, header[16:], b"hello world")
        self.assertEqual(ck.decrypt(blob, KEY), b"hello world")

    def test_v11_prefix(self):
        blob = b"v11" + encrypt_cbc(KEY, b" " * 16, b"abc")
        self.assertEqual(ck.decrypt(blob, KEY), b"abc")

    def test_plaintext_passthrough(self):
        self.assertEqual(ck.decrypt(b"not-encrypted", KEY), b"not-encrypted")

    def test_wrong_key_does_not_raise(self):
        blob = b"v10" + encrypt_cbc(KEY, b" " * 16, b"secret")
        ck.decrypt(blob, b"fedcba9876543210")      # must not throw


class TestToCdp(unittest.TestCase):
    def test_partition_key_includes_cross_site_ancestor(self):
        """CDP rejects {topLevelSite} alone; hasCrossSiteAncestor is required."""
        params, _risky, _ = ck.to_cdp([cookie(name="cf_clearance", partitioned=True,
                                              partition_site="https://example.com")])
        self.assertEqual(params[0]["partitionKey"],
                         {"topLevelSite": "https://example.com",
                          "hasCrossSiteAncestor": False})

    def test_partitioned_without_site_is_dropped(self):
        params, risky, dropped = ck.to_cdp([cookie(partitioned=True, partition_site=None)])
        self.assertEqual((params, risky), ([], []))
        self.assertEqual(dropped["unpartitionable"], 1)

    def test_a_non_ascii_value_is_set_apart_rather_than_thrown_away(self):
        """One odd byte used to lose the cookie before Chrome was ever asked. It fails
        a whole `setCookies` batch, so it is offered on its own instead."""
        params, risky, dropped = ck.to_cdp([cookie(value="caf\u00e9"), cookie(name="ok")])
        self.assertEqual([p["name"] for p in params], ["ok"])
        self.assertEqual([p["value"] for p in risky], ["caf\u00e9"])
        self.assertEqual(dropped["non_ascii"], 1)

    def test_host_only_cookie_uses_url_not_domain(self):
        params, _risky, _ = ck.to_cdp([cookie(name="__Host-next-auth.csrf-token",
                                              host="chatgpt.com")])
        self.assertIn("url", params[0])
        self.assertNotIn("domain", params[0])
        self.assertEqual(params[0]["url"], "https://chatgpt.com/")

    def test_session_cookie_omits_expires(self):
        params, _risky, _ = ck.to_cdp([cookie(expires=-1.0)])
        self.assertNotIn("expires", params[0])

    def test_samesite_unspecified_omitted(self):
        params, _risky, _ = ck.to_cdp([cookie(samesite=-1)])
        self.assertNotIn("sameSite", params[0])

    def test_samesite_mapped(self):
        for raw, expected in ((0, "None"), (1, "Lax"), (2, "Strict")):
            params, _risky, _ = ck.to_cdp([cookie(samesite=raw)])
            self.assertEqual(params[0]["sameSite"], expected)


class TestRedact(unittest.TestCase):
    def test_short_value_fully_masked(self):
        self.assertEqual(ck.redact("abc"), "***")

    def test_long_value_keeps_only_the_edges(self):
        out = ck.redact("abcdefghijklmnop", 4)
        self.assertIn("abcd", out)
        self.assertIn("mnop", out)
        self.assertNotIn("efgh", out)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        store.SESSION_DIR = self.tmp

    def test_round_trip(self):
        store.save("example.com", [{"name": "sid", "value": "abc"}],
                   {"https://example.com": {"k": "v"}})
        session = store.load("example.com")
        self.assertEqual(session.cookies[0]["name"], "sid")
        self.assertEqual(session.local_storage["https://example.com"], {"k": "v"})

    def test_file_is_private(self):
        path = store.save("example.com", [])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_describe_never_leaks_values(self):
        store.save("example.com", [{"name": "sid", "value": "TOPSECRET"}])
        blob = json.dumps(store.describe())
        self.assertNotIn("TOPSECRET", blob)

    def test_local_storage_script_targets_origin(self):
        session = store.Session("example.com", [], {"https://example.com": {"a": "b"}})
        script = store.local_storage_init_script(session)
        self.assertIn("location.origin ===", script)
        self.assertIn('"a": "b"', script)

    def test_missing_session_is_none(self):
        self.assertIsNone(store.load("nope.example"))


class TestVerdict(unittest.TestCase):
    def test_marker_wins_over_prompt(self):
        """YouTube shows a Sign in link while logged in; the avatar is the truth."""
        state = PageState("u", "t", login_prompt="Sign in", logged_in_marker="avatar")
        self.assertEqual(state.verdict, "logged-in")

    def test_prompt_means_logged_out(self):
        self.assertEqual(PageState("u", "t", login_prompt="Log in").verdict, "logged-out")

    def test_password_field_means_logged_out(self):
        self.assertEqual(PageState("u", "t", password_field=True).verdict, "logged-out")

    def test_nothing_known_is_unclear(self):
        self.assertEqual(PageState("u", "t").verdict, "unclear")

    def test_blocked_outranks_everything(self):
        """A Cloudflare/anti-bot wall is not a login problem. If an agent confuses
        the two it will ask the user to sign in forever."""
        state = PageState("u", "t", blocked="you've been blocked",
                          login_prompt="Log in", logged_in_marker="avatar")
        self.assertEqual(state.verdict, "blocked")


class TestBlockHints(unittest.TestCase):
    def test_reddit_block_page_is_recognised(self):
        from latchkey.session import BLOCK_HINTS
        page = "you've been blocked by network security. if you think you've been "
        self.assertTrue(any(h in page for h in BLOCK_HINTS))

    def test_cloudflare_challenge_is_recognised(self):
        from latchkey.session import BLOCK_HINTS
        self.assertTrue(any(h in "just a moment..." for h in BLOCK_HINTS))


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        credentials.VAULT = os.path.join(self.tmp, "credentials.json")

    def test_vault_write_then_resolve(self):
        cred = credentials.put_in_vault("example.com", "alice", "hunter2")
        self.assertEqual(os.stat(cred).st_mode & 0o777, 0o600)
        found = credentials.resolve("example.com")
        self.assertEqual(found.username, "alice")
        self.assertEqual(found.source, "vault")

    def test_repr_does_not_leak_password(self):
        cred = credentials.Credential("example.com", "alice", "hunter2", "vault")
        self.assertNotIn("hunter2", repr(cred))

    def test_explicit_beats_vault(self):
        credentials.put_in_vault("example.com", "alice", "hunter2")
        found = credentials.resolve("example.com", "bob", "other")
        self.assertEqual(found.source, "explicit")

    def test_missing_returns_none(self):
        self.assertIsNone(credentials.resolve("nothing.example.com"))


class TestMcpProtocol(unittest.TestCase):
    def test_initialize(self):
        resp = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "latchkey")

    def test_notification_gets_no_reply(self):
        self.assertIsNone(handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_is_well_formed(self):
        resp = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for tool in resp["result"]["tools"]:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_every_advertised_tool_is_dispatchable(self):
        advertised = {t["name"] for t in TOOLS}
        self.assertTrue(advertised <= set(DISPATCH),
                        "tools/list and DISPATCH have drifted apart")

    def test_the_catalog_matches_the_dispatch_table(self):
        catalog = {t["name"] for t in CATALOG} | {BATCH}
        self.assertEqual(catalog, set(DISPATCH),
                         "the catalog and DISPATCH have drifted apart")

    def test_unknown_tool_is_an_error_not_a_crash(self):
        resp = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})
        self.assertTrue(resp["result"]["isError"])

    def test_bad_arguments_are_an_error_not_a_crash(self):
        resp = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "latchkey_open",
                                  "arguments": {"wrong": 1}}})
        self.assertTrue(resp["result"]["isError"])

    def test_unknown_method(self):
        resp = handle({"jsonrpc": "2.0", "id": 5, "method": "does/not/exist"})
        self.assertIn("error", resp)


class TestSiteHints(unittest.TestCase):
    """Some sites need a known marker: ChatGPT builds its account control from divs."""

    def test_builtin_chatgpt_marker_is_offered(self):
        hints = _site_hints("https://chatgpt.com/")
        self.assertTrue(any("create-new-chat-button" in h for h in hints))

    def test_unknown_host_gets_no_hints(self):
        self.assertEqual(_site_hints("https://nothing.example.org/"), [])

    def test_user_hints_file_is_honoured(self):
        import latchkey.detect as session_module
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "hints.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"example.org": ["#account-menu"]}, fh)
        original = session_module.HINTS_FILE
        session_module.HINTS_FILE = path
        try:
            self.assertIn("#account-menu", _site_hints("https://example.org/x"))
            self.assertEqual(_site_hints("https://other.example.com/"), [])
        finally:
            session_module.HINTS_FILE = original

    def test_broken_hints_file_does_not_raise(self):
        import latchkey.detect as session_module
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "hints.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        original = session_module.HINTS_FILE
        session_module.HINTS_FILE = path
        try:
            _site_hints("https://example.org/")
        finally:
            session_module.HINTS_FILE = original


class TestMultiProfile(unittest.TestCase):
    """Every profile in one Chrome install shares a Keychain key, so reading all
    of them costs one decryption call and merges into one jar."""

    def setUp(self):
        self._real_profiles = ck.profiles
        ck.profiles = lambda: {"Default": "/tmp/p-a", "Profile 2": "/tmp/p-b"}

    def tearDown(self):
        ck.profiles = self._real_profiles

    def test_none_means_default_only(self):
        self.assertEqual(ck.resolve_profiles(None), ["Default"])

    def test_all_expands_to_every_profile(self):
        self.assertEqual(ck.resolve_profiles("all"), ["Default", "Profile 2"])

    def test_star_is_all(self):
        self.assertEqual(ck.resolve_profiles("*"), ["Default", "Profile 2"])

    def test_single_name_passes_through(self):
        self.assertEqual(ck.resolve_profiles("Profile 2"), ["Profile 2"])

    def test_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            ck.resolve_profiles("Profile 99")

    def _fake_load(self, host=None, db_path=None):
        value = "from-a" if db_path == "/tmp/p-a" else "from-b"
        return [ck.Cookie(host=".x.com", name="sid", value=value, path="/",
                          secure=True, http_only=False, samesite=1,
                          expires=1893456000.0, partitioned=False)]

    def test_later_profile_wins_on_conflict(self):
        original = ck.load
        ck.load = self._fake_load
        try:
            merged, counts, errors = ck.load_many(["Default", "Profile 2"])
        finally:
            ck.load = original
        self.assertEqual(len(merged), 1, "same host/name/path should collapse to one")
        self.assertEqual(merged[0].value, "from-b")
        self.assertEqual(counts, {"Default": 1, "Profile 2": 1})
        self.assertEqual(errors, {})

    def test_reversed_order_flips_the_winner(self):
        original = ck.load
        ck.load = self._fake_load
        try:
            merged, _, _ = ck.load_many(["Profile 2", "Default"])
        finally:
            ck.load = original
        self.assertEqual(merged[0].value, "from-a")

    def test_a_broken_profile_is_reported_not_fatal(self):
        def exploding_load(host=None, db_path=None):
            raise sqlite3.OperationalError("database is locked")

        original = ck.load
        ck.load = exploding_load
        try:
            merged, counts, errors = ck.load_many(["Default", "Profile 2"])
        finally:
            ck.load = original
        self.assertEqual(merged, [])
        self.assertEqual(counts, {})
        self.assertEqual(len(errors), 2)
        self.assertIn("OperationalError", errors["Default"])


class TestProfileClone(unittest.TestCase):
    """The clone route is what covers IndexedDB, so its failure modes matter.
    Tested against a synthetic Chrome root, so it needs no real profile."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.src = os.path.join(self.root, "Chrome")
        os.makedirs(os.path.join(self.src, "Default"))
        con = sqlite3.connect(os.path.join(self.src, "Default", "Cookies"))
        con.execute("create table cookies (host_key text)")
        con.executemany("insert into cookies values (?)", [("a.com",), ("b.com",)])
        con.commit()
        con.close()
        os.symlink("hostname-1234", os.path.join(self.src, "SingletonLock"))
        self.dest = os.path.join(self.root, "clone")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cookies_survive_the_clone(self):
        info = profile.clone(source=self.src, dest=self.dest)
        self.assertEqual(info["rows"], 2)
        self.assertFalse(info["reused"])

    def test_inherited_singleton_lock_is_removed(self):
        """Left in place, Chrome refuses to start: 'Failed to create a ProcessSingleton'."""
        profile.clone(source=self.src, dest=self.dest)
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "SingletonLock")))

    def test_a_fresh_clone_is_reused_not_remade(self):
        profile.clone(source=self.src, dest=self.dest)
        again = profile.clone(source=self.src, dest=self.dest)
        self.assertTrue(again["reused"])

    def test_force_reclones(self):
        profile.clone(source=self.src, dest=self.dest)
        forced = profile.clone(source=self.src, dest=self.dest, force=True)
        self.assertFalse(forced["reused"])

    def test_an_empty_clone_raises_rather_than_silently_logging_out(self):
        """This is the bug that cost hours: a clone with no Cookies looks fine and
        produces a browser where every site is signed out."""
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        with self.assertRaises(RuntimeError):
            profile.clone(source=empty, dest=os.path.join(self.root, "clone2"))

    def test_staleness_is_detected(self):
        profile.clone(source=self.src, dest=self.dest)
        self.assertFalse(profile.source_is_newer(self.dest, self.src))
        future = time.time() + 10
        os.utime(os.path.join(self.src, "Default", "Cookies"), (future, future))
        self.assertTrue(profile.source_is_newer(self.dest, self.src))

    def test_missing_clone_counts_as_stale(self):
        self.assertTrue(profile.source_is_newer(os.path.join(self.root, "nope"), self.src))

    def test_discard_removes_it(self):
        profile.clone(source=self.src, dest=self.dest)
        self.assertTrue(profile.discard(self.dest))
        self.assertFalse(os.path.isdir(self.dest))


class TestResolveProfiles(unittest.TestCase):
    """`['all']` is the documented shorthand and it used to raise KeyError.

    An agent hit exactly that: the tool description says pass `['all']` or `'all'`, the
    list form blew up with `unknown profile(s): all`, and the agent went off to read
    Chrome's cookie SQLite files and History to find the profile name that would have
    worked. Every spelling of "all" has to mean all.
    """

    AVAILABLE = {"Default": "/db/Default", "Profile 2": "/db/Profile 2",
                 "Profile 3": "/db/Profile 3"}

    def setUp(self):
        self._real = ck.profiles
        ck.profiles = lambda: dict(self.AVAILABLE)

    def tearDown(self):
        ck.profiles = self._real

    def test_the_bare_string_still_means_every_profile(self):
        self.assertEqual(ck.resolve_profiles("all"), list(self.AVAILABLE))

    def test_the_list_form_means_every_profile(self):
        self.assertEqual(ck.resolve_profiles(["all"]), list(self.AVAILABLE))

    def test_case_and_wildcard_spellings_work(self):
        for spec in ("ALL", ["All"], ["*"], ["*"], ["all"]):
            self.assertEqual(ck.resolve_profiles(spec), list(self.AVAILABLE), spec)

    def test_all_alongside_a_name_means_all(self):
        self.assertEqual(ck.resolve_profiles(["Default", "all"]), list(self.AVAILABLE))

    def test_none_is_default(self):
        self.assertEqual(ck.resolve_profiles(None), ["Default"])

    def test_one_named_profile_is_just_that_one(self):
        self.assertEqual(ck.resolve_profiles(["Profile 3"]), ["Profile 3"])
        self.assertEqual(ck.resolve_profiles("Profile 3"), ["Profile 3"])

    def test_whitespace_is_ignored(self):
        self.assertEqual(ck.resolve_profiles([" Profile 2 "]), ["Profile 2"])

    def test_an_empty_list_is_default_not_everything(self):
        self.assertEqual(ck.resolve_profiles([]), ["Default"])

    def test_a_real_typo_still_fails_loudly_and_says_what_exists(self):
        with self.assertRaises(KeyError) as caught:
            ck.resolve_profiles(["Profile 9"])
        message = str(caught.exception)
        self.assertIn("Profile 9", message)
        self.assertIn("Profile 3", message)
        self.assertIn("all", message, "the error should mention the shorthand that works")

    def test_no_profiles_at_all_is_a_clear_error(self):
        ck.profiles = lambda: {}
        with self.assertRaises(FileNotFoundError):
            ck.resolve_profiles("all")


class TestHostCounts(unittest.TestCase):

    AVAILABLE = {"Default": "/db/Default", "Profile 2": "/db/Profile 2",
                 "Profile 3": "/db/Profile 3"}

    def setUp(self):
        self._real_rows = ck._rows
        self.jars = {
            "/db/Default": [_row("theme")],
            "/db/Profile 3": [_row("session-token"), _row("sid"), _row("theme")],
            "/db/Profile 2": [],
        }
        # `host_counts` counts names; it never decrypts and never touches the Keychain,
        # so the seam it is tested at is the raw row read, one copy per profile.
        ck._rows = lambda db, hosts=None: list(self.jars.get(db, []))

    def tearDown(self):
        ck._rows = self._real_rows

    def test_a_profile_is_read_once_however_many_host_candidates_there_are(self):
        """`www.example.com` asks about two hosts; that used to be two copies of the file."""
        seen = []
        ck._rows = lambda db, hosts=None: seen.append((db, tuple(hosts or ()))) or []
        ck.host_counts("www.example.com", available={"Default": "/db/Default"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], ("www.example.com", "example.com"))

    def test_a_www_page_finds_cookies_kept_on_the_parent_domain(self):
        """The reported case, exactly: the page is www.webassign.net and the session is on
        .webassign.net, so asking about the exact host found nothing and the answer read
        as "no profile has it" - the wrong answer in the commonest case of all."""
        self.jars["/db/Profile 3"] = [
            _row("session-token", ".webassign.net"),
            _row("sid", ".webassign.net"),
        ]
        # Only the parent domain matches, as in Chrome.
        ck._rows = lambda db, hosts=None: list(self.jars.get(db, [])) \
            if any(h == "webassign.net" for h in (hosts or [])) else []
        counts = ck.host_counts("www.webassign.net", available=self.AVAILABLE)
        self.assertEqual(counts["Profile 3"]["cookies"], 2)
        self.assertEqual(counts["Profile 3"]["auth_like"], 2)

    def test_a_cookie_matching_both_candidates_is_counted_once(self):
        counts = ck.host_counts("www.example.com", available=self.AVAILABLE)
        self.assertEqual(counts["Profile 3"]["cookies"], 3)

    def test_similar_and_sibling_hosts_do_not_count_for_the_requested_site(self):
        self.jars["/db/Profile 3"] = [
            _row("session-token", "notexample.com"),
            _row("session-token", "example.com.evil.test"),
            _row("session-token", "mail.example.com"),
            _row("sid", ".example.com"),
        ]

        counts = ck.host_counts("campus.example.com", available=self.AVAILABLE)

        self.assertEqual(counts["Profile 3"], {"cookies": 1, "auth_like": 1})

    def test_every_profile_is_counted_for_the_host(self):
        counts = ck.host_counts("example.com", available=self.AVAILABLE)
        self.assertEqual(counts["Profile 3"]["cookies"], 3)
        self.assertEqual(counts["Default"]["cookies"], 1)
        self.assertEqual(counts["Profile 2"]["cookies"], 0)

    def test_auth_shaped_cookies_are_called_out(self):
        """This is what separates "has cookies" from "has the session": the profile with
        the auth-shaped names is the one worth restarting a session onto."""
        counts = ck.host_counts("example.com", available=self.AVAILABLE)
        self.assertEqual(counts["Profile 3"]["auth_like"], 2)
        self.assertEqual(counts["Default"]["auth_like"], 0)

    def test_expired_cookie_names_are_not_evidence_of_a_current_login(self):
        expired = list(_row("session-token"))
        expired[7] = 1
        expired[8] = 1
        self.jars["/db/Profile 3"] = [tuple(expired), _row("theme")]

        counts = ck.host_counts("example.com", available=self.AVAILABLE)

        self.assertEqual(counts["Profile 3"], {"cookies": 1, "auth_like": 0})

    def test_the_names_argument_narrows_the_search(self):
        counts = ck.host_counts("example.com", names=["Profile 3"],
                                available=self.AVAILABLE)
        self.assertEqual(list(counts), ["Profile 3"])

    def test_an_unreadable_jar_is_reported_not_raised(self):
        def explode(db, hosts=None):
            raise RuntimeError("database is locked")

        ck._rows = explode
        counts = ck.host_counts("example.com", available=self.AVAILABLE)
        self.assertEqual(counts["Default"]["cookies"], 0)
        self.assertIn("database is locked", counts["Default"]["error"])


def _cookie(name: str, host: str = ".example.com") -> "ck.Cookie":
    return ck.Cookie(host=host, name=name, value="x", path="/",
                     secure=True, http_only=True, samesite=2, expires=-1,
                     partitioned=False)


def _row(name: str, host: str = ".example.com", path: str = "/") -> tuple:
    """One raw row, in the column order `COOKIE_COLUMNS` selects."""
    return (host, name, b"", path, 1, 1, 2, 0, 0, None)


class TestLoginHintsDefined(unittest.TestCase):
    """Regression: an edit once dropped LOGIN_HINTS while leaving it referenced.
    login_prompt() swallowed the NameError, so it silently always returned None
    and 'logged-out' detection was dead for several runs without anything failing.
    The hints live in `detect` now, and the check is that they reach the script."""

    def test_login_hints_exists_and_is_populated(self):
        from latchkey import detect
        self.assertTrue(hasattr(detect, "LOGIN_HINTS"))
        self.assertIn("sign in", detect.LOGIN_HINTS)

    def test_block_hints_are_wired_into_the_probe(self):
        """A hint that never reaches the script is a hint that does nothing."""
        from latchkey import detect
        self.assertIn("cfg.block", detect.PROBE_JS)
        self.assertIn("cfg.login", detect.PROBE_JS)
        self.assertIn("cfg.markers", detect.PROBE_JS)
        self.assertTrue(detect.BLOCK_HINTS)
        self.assertTrue(detect.GENERIC_MARKERS)


class TestApplyActions(unittest.TestCase):
    """The action layer is what agents drive, so its plumbing gets tested directly."""

    class Fake:
        def __init__(self):
            self.calls = []
            self.page = type("P", (), {"wait_for_timeout": lambda *a: None})()

        def click(self, selector, settle_ms=2000, force=False):
            self.calls.append(("click", selector, force))

        def fill(self, selector, value, settle_ms=300):
            self.calls.append(("fill", selector, value))

        def refresh(self):
            self.calls.append(("refresh",))

    def test_force_is_passed_through_to_click(self):
        """Canvas's welcome-tour overlay makes ordinary clicks time out; force is
        the escape hatch, so it must survive the action layer."""
        fake = self.Fake()
        apply_actions(fake, [{"do": "click", "selector": "#a", "force": True}])
        self.assertEqual(fake.calls, [("click", "#a", True)])

    def test_force_defaults_to_false(self):
        fake = self.Fake()
        apply_actions(fake, [{"do": "click", "selector": "#a"}])
        self.assertEqual(fake.calls, [("click", "#a", False)])

    def test_fill_and_sync_dispatch(self):
        fake = self.Fake()
        apply_actions(fake, [{"do": "fill", "selector": "#q", "value": "x"},
                             {"do": "sync"}])
        self.assertEqual(fake.calls, [("fill", "#q", "x"), ("refresh",)])

    def test_unknown_verb_raises_rather_than_silently_doing_nothing(self):
        with self.assertRaises(ValueError):
            apply_actions(self.Fake(), [{"do": "teleport"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
