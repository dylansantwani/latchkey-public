"""Google, signed in once: routing, the sign-in window, and the wait that used to loop.

What went wrong, in the order it went wrong:

  - A copied Google session worked for about twenty minutes, then signed the user out of
    both browsers: the session rotates, and Chrome binds it to a key in the profile that
    registered it. So inject holds the account session back - and could then never sign in
    to Google, and did not say so.
  - latchkey_open told the model to have the user sign in in their own Chrome and call
    wait_for_login, which watched the user's Chrome jar, whose Google cookies were held back
    again. A loop with no exit.
  - Dedicated mode, the one that works, had no way to be signed in from an agent: every
    session was headless, and a Playwright window is exactly what Google's sign-in refuses.

  - Then the clone that was meant to fix all that became the default for Google, and
    brought back the first failure in a slower form: a copy that sits unused goes stale,
    and a copy that is used at the same time as the original is a replay, which signs the
    user out of their own Chrome. That is the failure this file now guards against.

So: a session that did not choose a mode moves itself to *latchkey's own profile* for Google
- never to a copy of the user's. That profile signs in once (latchkey_login, an ordinary
Chrome window with no automation in its command line), registers a device-bound key of its
own, and renews its own session afterwards. `account=` picks which profile, so a second
Google account is a second profile rather than a second login in the first one's. Its status
is read from that profile's own cookie store, and the headless launch that uses it afterwards
is the same Chrome with a short explicit flag list. None of it needs a browser to test.

    python3 -m unittest tests.test_google_login -v
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import latchkey.chrome as chrome_mod  # noqa: E402
import latchkey.cookies as ck  # noqa: E402
import latchkey.login as login_mod  # noqa: E402
import latchkey.mcp_server as mcp_server  # noqa: E402
import latchkey.profile as profile_mod  # noqa: E402
import latchkey.session as session_mod  # noqa: E402
from latchkey import google, store  # noqa: E402
from latchkey.detect import PageState  # noqa: E402
from latchkey.session import Browser, SessionSpec  # noqa: E402
from latchkey.sessions import SessionRegistry  # noqa: E402

CHROME_EPOCH = 11_644_473_600
GOOGLE_SSO_ACCOUNTS = '{"portal.example.edu": "school"}'


def make_cookie_db(profile: str, rows) -> str:
    """A profile's Default/Cookies with (host, name, expires_in_seconds or None) rows."""
    path = os.path.join(profile, "Default", "Cookies")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table cookies (host_key text, name text, encrypted_value blob, "
                "path text, is_secure int, is_httponly int, samesite int, has_expires int, "
                "expires_utc int, top_frame_site_key text)")
    for host, name, expires_in in rows:
        expires = int((time.time() + expires_in + CHROME_EPOCH) * 1_000_000) \
            if expires_in is not None else 0
        con.execute("insert into cookies values (?, ?, ?, '/', 1, 1, 0, ?, ?, '')",
                    (host, name, b"v10" + b"\0" * 32, 1 if expires_in is not None else 0,
                     expires))
    con.commit()
    con.close()
    return path


def hold_lock(profile: str, pid: int | None = None) -> None:
    """Make a profile look open in a live Chrome: SingletonLock -> host-<pid>."""
    os.makedirs(profile, exist_ok=True)
    link = os.path.join(profile, chrome_mod.SINGLETON_LOCK)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(f"{socket.gethostname()}-{pid or os.getpid()}", link)


class Isolated(unittest.TestCase):
    """Every test here gets its own profile directory and its own login state file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.profile = os.path.join(self.root, "profile")
        patches = [mock.patch.dict(os.environ, {"LATCHKEY_PROFILE_DIR": self.profile}),
                   mock.patch.object(login_mod, "STATE_FILE",
                                     os.path.join(self.root, "login.json"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)


# -- which hosts are Google's ---------------------------------------------------

@mock.patch.dict(os.environ, {google.GOOGLE_SSO_ACCOUNTS_ENV: GOOGLE_SSO_ACCOUNTS})
class GoogleIsAHostQuestionAnsweredPrecisely(unittest.TestCase):

    def test_the_google_family(self):
        for host in ("google.com", "accounts.google.com", "mail.google.com", "docs.google.com",
                     "gemini.google.com", "gmail.com", "www.youtube.com", "youtu.be",
                     "music.youtube.com", "lh3.googleusercontent.com", "www.google.co.uk",
                     "google.de", "google.com.au", "https://mail.google.com/mail/u/0/",
                     "MAIL.GOOGLE.COM:443", "someone@accounts.google.com"):
            self.assertTrue(google.is_google_host(host), host)

    def test_not_google(self):
        for host in ("notgoogle.com", "google.com.evil.net", "github.com", "googleapis.com",
                     "fonts.gstatic.com", "grow.withgoogle.com", "example.com", "", None,
                     "mygoogle.de", "https://example.com/?next=https://mail.google.com"):
            self.assertFalse(google.is_google_host(host), host)

    def test_the_mode_a_site_needs(self):
        # Google routes to latchkey's own signed-in profile. A copy of the real profile is
        # what gets the *user* signed out - two holders of one account session read as a
        # replay - so the account session latchkey uses has to be one it owns. Everything
        # else is inject, which never touches a Google login at all.
        self.assertEqual(google.mode_for("https://mail.google.com/"), "dedicated")
        self.assertEqual(google.mode_for(
            "https://portal.example.edu/campus/portal/students/"),
            "dedicated")
        self.assertEqual(google.mode_for("https://github.com/"), "inject")

    def test_a_google_sso_portal_is_not_mistaken_for_google_itself(self):
        campus = "https://portal.example.edu/campus/portal/students/"
        self.assertTrue(google.is_google_sso_clone_host(campus))
        self.assertTrue(google.uses_google_session(campus))
        self.assertFalse(google.is_google_host(campus))
        for host in ("portal.example.edu.attacker.test", "notportal.example.edu",
                     "other.example.edu"):
            self.assertFalse(google.is_google_sso_clone_host(host), host)

    def test_the_google_mode_is_overridable(self):
        self.assertEqual(google.google_mode(), "dedicated")
        for choice in ("clone", "inject", "real"):
            with mock.patch.dict(os.environ, {"LATCHKEY_GOOGLE_MODE": choice}):
                self.assertEqual(google.google_mode(), choice)
                self.assertEqual(google.mode_for("https://mail.google.com/"), choice)
        with mock.patch.dict(os.environ, {"LATCHKEY_GOOGLE_MODE": "nonsense"}):
            self.assertEqual(google.google_mode(), "dedicated")

    def test_an_invalid_portal_mapping_does_not_route_an_unexpected_host(self):
        with mock.patch.dict(os.environ, {google.GOOGLE_SSO_ACCOUNTS_ENV: "not json"}):
            self.assertFalse(google.is_google_sso_clone_host("portal.example.edu"))
            self.assertEqual(google.mode_for("portal.example.edu"), "inject")

    def test_the_country_sites_account_session_is_held_back_too(self):
        """`.google.co.uk` carries the same session as `.google.com`; the old rule copied it."""
        self.assertTrue(ck.is_live_cookie(".google.co.uk", "__Secure-1PSID"))
        self.assertTrue(ck.is_live_cookie(".youtube.com", "SAPISID"))
        self.assertFalse(ck.is_live_cookie(".google.co.uk", "NID"))


@mock.patch.dict(os.environ, {google.GOOGLE_SSO_ACCOUNTS_ENV: GOOGLE_SSO_ACCOUNTS})
class TheGoogleCloneDirectory(unittest.TestCase):
    """Google routes to a clone in its own directory (so it never collides with an ordinary
    clone session), re-seeded from the real profile whenever that profile is newer - which
    keeps its device-bound token current instead of drifting into a revoked one."""

    def test_the_clone_dir_is_its_own_and_overridable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LATCHKEY_GOOGLE_CLONE_DIR", None)
            self.assertTrue(google.google_clone_dir().endswith("google-clone"))
        with mock.patch.dict(os.environ, {"LATCHKEY_GOOGLE_CLONE_DIR": "/tmp/gc"}):
            self.assertEqual(google.google_clone_dir(), "/tmp/gc")

    def test_reuse_ok_can_keep_a_clone_even_when_the_source_is_newer(self):
        import latchkey.profile as profile_mod
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dest:
            for root in (src, dest):
                os.makedirs(os.path.join(root, "Default"), exist_ok=True)
            make_cookie_db(src, [(".google.com", "SID", 3600)])
            make_cookie_db(dest, [(".google.com", "SID", 3600)])
            os.utime(os.path.join(src, "Default", "Cookies"), (2 ** 31, 2 ** 31))  # far newer
            report = profile_mod.clone(source=src, dest=dest, reuse_ok=lambda d: True)
            self.assertTrue(report["reused"], "reuse_ok=True keeps the clone despite the mtime")

    def test_a_google_pinned_clone_session_is_flagged(self):
        self.assertTrue(Browser("mail.google.com",
                                spec=SessionSpec(mode="clone", host="mail.google.com"))._google_clone)
        self.assertTrue(Browser("portal.example.edu", spec=SessionSpec(
            mode="clone", host="portal.example.edu"))._google_clone)
        self.assertFalse(Browser("github.com",
                                 spec=SessionSpec(mode="clone", host="github.com"))._google_clone)

    def test_strip_rotating_session_drops_the_timestamps_and_keeps_the_anchors(self):
        # The clone mints its own fresh rotating token, so the copied (possibly stale) one is
        # removed while the anchor session and other cookies are left in place.
        with tempfile.TemporaryDirectory() as clone:
            make_cookie_db(clone, [
                (".google.com", "SID", 3600),
                (".google.com", "__Secure-1PSID", 3600),
                (".google.com", "__Secure-1PSIDTS", 3600),
                (".google.com", "__Secure-3PSIDTS", 3600),
                (".google.com", "__Secure-1PSIDRTS", 600),
                (".github.com", "user_session", 3600),
            ])
            removed = google.strip_rotating_session(clone)
            self.assertEqual(removed, 3)
            con = sqlite3.connect(os.path.join(clone, "Default", "Cookies"))
            left = {name for (name,) in con.execute("select name from cookies")}
            con.close()
            self.assertEqual(left & set(google.ROTATING_SESSION_COOKIES), set())
            self.assertIn("SID", left)
            self.assertIn("__Secure-1PSID", left)   # anchors kept
            self.assertIn("user_session", left)      # a non-Google cookie is untouched
            self.assertTrue(google.signed_in(os.path.join(clone, "Default", "Cookies")))

    def test_strip_rotating_session_on_a_missing_store_is_zero(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(google.strip_rotating_session(empty), 0)
        self.assertFalse(ck.is_live_cookie(".notgoogle.com", "SID"))


class SignInStepsByUrl(unittest.TestCase):

    def test_the_steps_are_named(self):
        cases = {
            "https://accounts.google.com/v3/signin/challenge/pwd?TL=x": "Google password page",
            "https://accounts.google.com/v3/signin/challenge/pk?TL=x": "Google passkey prompt",
            "https://accounts.google.com/v3/signin/accountchooser?x=1": "Google account chooser",
            "https://accounts.google.com/v3/signin/identifier?flowName=x": "Google email page",
            "https://accounts.google.com/v3/signin/challenge/totp?x": "Google verification step",
            "https://accounts.google.com/ServiceLogin?service=mail": "Google sign-in page",
            "https://accounts.google.com/InteractiveLogin?continue=x": "Google sign-in page",
        }
        for url, name in cases.items():
            self.assertEqual(google.signin_step(url), name, url)

    def test_pages_that_are_not_steps(self):
        for url in ("https://mail.google.com/mail/u/0/#inbox", "https://myaccount.google.com/",
                    "https://accounts.google.com/signin/oauth/id?client_id=x",
                    "https://accounts.google.com/CheckCookie?continue=x",
                    "https://example.com/v3/signin/challenge/pwd", ""):
            self.assertIsNone(google.signin_step(url), url)


# -- the dedicated profile's own cookie store -------------------------------------

class SignedInIsReadFromTheProfilesOwnStoreByName(Isolated):

    def test_an_account_session_is_seen(self):
        db = make_cookie_db(self.profile, [(".google.com", "SID", 3600),
                                           (".google.com", "__Secure-1PSID", 3600),
                                           (".google.com", "NID", 3600)])
        self.assertEqual(google.session_cookie_names(db), {"SID", "__Secure-1PSID"})
        self.assertTrue(google.signed_in(db))

    def test_anonymous_google_cookies_are_not_a_sign_in(self):
        db = make_cookie_db(self.profile, [(".google.com", "NID", 3600),
                                           (".google.com", "AEC", 3600),
                                           ("accounts.google.com", "__Host-GAPS", 3600)])
        self.assertFalse(google.signed_in(db))

    def test_an_expired_session_is_not_a_sign_in(self):
        db = make_cookie_db(self.profile, [(".google.com", "SID", -60)])
        self.assertFalse(google.signed_in(db))

    def test_another_hosts_sid_is_not_googles(self):
        db = make_cookie_db(self.profile, [(".notgoogle.com", "SID", 3600)])
        self.assertFalse(google.signed_in(db))

    def test_no_store_or_a_broken_one_is_simply_not_signed_in(self):
        self.assertFalse(google.signed_in(os.path.join(self.profile, "nope")))
        path = os.path.join(self.profile, "Default", "Cookies")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"not a database")
        self.assertFalse(google.signed_in(path))

    def test_nothing_is_decrypted_to_answer(self):
        make_cookie_db(self.profile, [(".google.com", "SID", 3600)])
        with mock.patch.object(ck, "key", side_effect=AssertionError("no Keychain here")):
            self.assertTrue(login_mod.status()["signed_in"])


# -- the window a person signs in to ----------------------------------------------

class TheSignInWindowHasNoAutomationInIt(unittest.TestCase):

    def test_the_command_on_macos_opens_in_the_background_by_default(self):
        # An agent-opened window must not steal focus: -g opens the app without bringing it
        # to the front. That is the fix for "agents front a window without asking".
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with mock.patch.object(chrome_mod.shutil, "which", return_value="/usr/bin/open"):
            command = chrome_mod.visible_command("/p/profile", "https://accounts.google.com/",
                                                 chrome, platform="darwin")
        self.assertEqual(command[:6], ["open", "-g", "-n", "-a",
                                       "/Applications/Google Chrome.app", "--args"])
        self.assertIn("--user-data-dir=/p/profile", command)
        self.assertEqual(command[-1], "https://accounts.google.com/")

    def test_a_waiting_person_can_ask_for_the_front(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with mock.patch.object(chrome_mod.shutil, "which", return_value="/usr/bin/open"):
            command = chrome_mod.visible_command("/p/profile", "https://accounts.google.com/",
                                                 chrome, platform="darwin", foreground=True)
        self.assertEqual(command[:5], ["open", "-n", "-a",
                                       "/Applications/Google Chrome.app", "--args"])
        self.assertNotIn("-g", command)

    def test_no_flag_google_can_read_as_automation(self):
        for platform in ("darwin", "linux"):
            command = chrome_mod.visible_command("/p/profile", "https://accounts.google.com/",
                                                 "/usr/bin/google-chrome", platform=platform)
            for flag in chrome_mod.AUTOMATION_FLAGS:
                self.assertFalse(any(part.startswith(flag) for part in command),
                                 f"{flag} in {command}")

    def test_the_launch_returns_as_soon_as_chrome_holds_the_profile(self):
        with tempfile.TemporaryDirectory() as profile:
            launched = []

            def popen(command, **kwargs):
                launched.append((command, kwargs))
                hold_lock(profile)
                return mock.Mock(pid=os.getpid())

            pid = chrome_mod.launch_visible(profile, "https://accounts.google.com/",
                                            chrome="/usr/bin/google-chrome", popen=popen,
                                            sleep=lambda s: None)
            self.assertEqual(pid, os.getpid())
            self.assertTrue(launched[0][1]["start_new_session"],
                            "the window must outlive the latchkey that opened it")

    def test_a_profile_already_open_is_refused_rather_than_handed_a_window(self):
        with tempfile.TemporaryDirectory() as profile:
            hold_lock(profile)
            with self.assertRaises(chrome_mod.ProfileBusy):
                chrome_mod.launch_visible(profile, "https://x/", chrome="/bin/false",
                                          popen=mock.Mock(side_effect=AssertionError))


class LockOwners(unittest.TestCase):

    def test_a_live_pid_owns_it_and_a_dead_one_does_not(self):
        with tempfile.TemporaryDirectory() as profile:
            self.assertIsNone(chrome_mod.lock_owner(profile))
            hold_lock(profile)
            self.assertEqual(chrome_mod.lock_owner(profile), os.getpid())
            hold_lock(profile, pid=2 ** 22 + 12345)       # no such process
            self.assertIsNone(chrome_mod.lock_owner(profile))

    def test_a_lock_that_is_not_hostname_pid_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as profile:
            os.symlink("garbage", os.path.join(profile, chrome_mod.SINGLETON_LOCK))
            self.assertIsNone(chrome_mod.lock_owner(profile))

    def test_the_major_version(self):
        self.assertEqual(chrome_mod.major_version("153.0.8010.37"), 153)
        self.assertIsNone(chrome_mod.major_version(""))


class StartingTheSignIn(Isolated):

    def test_it_opens_the_window_writes_down_which_it_is_and_returns(self):
        calls = []

        def launcher(path, url):
            calls.append((path, url))
            hold_lock(path)
            return os.getpid()

        out = login_mod.start(launcher=launcher)
        self.assertEqual(out["status"], "window-open")
        self.assertEqual(calls, [(self.profile, "https://accounts.google.com/")])
        self.assertIn("Never ask the user to type a password", out["next_step"])
        self.assertIn("latchkey_login_status", out["next_step"])
        with open(login_mod.STATE_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pid"], os.getpid())
        self.assertEqual(os.stat(login_mod.STATE_FILE).st_mode & 0o777, 0o600)
        state = login_mod.status()
        self.assertTrue(state["window_open"])
        self.assertFalse(state["signed_in"])

    def test_a_second_start_while_the_window_is_open_does_not_open_another(self):
        login_mod.start(launcher=lambda path, url: (hold_lock(path), os.getpid())[1])
        again = login_mod.start(launcher=mock.Mock(side_effect=AssertionError("second window")))
        self.assertEqual(again["status"], "window-already-open")

    def test_already_signed_in_opens_nothing_unless_asked_again(self):
        make_cookie_db(self.profile, [(".google.com", "SID", 3600)])
        out = login_mod.start(launcher=mock.Mock(side_effect=AssertionError("no window")))
        self.assertEqual(out["status"], "already-signed-in")
        launcher = mock.Mock(return_value=None)
        self.assertEqual(login_mod.start(again=True, launcher=launcher)["status"], "window-open")
        launcher.assert_called_once()

    def test_another_chrome_on_the_profile_is_not_ours_to_open_over(self):
        hold_lock(self.profile)                   # a lock with no login state behind it
        with self.assertRaises(chrome_mod.ProfileBusy):
            login_mod.start(launcher=mock.Mock(side_effect=AssertionError))

    def test_only_web_urls_open(self):
        with self.assertRaises(Exception):
            login_mod.start("file:///etc/passwd", launcher=mock.Mock())

    def test_it_never_reads_the_real_chrome_profile(self):
        with mock.patch.object(ck, "load", side_effect=AssertionError("real jar")), \
                mock.patch.object(ck, "load_many", side_effect=AssertionError("real jar")):
            login_mod.start(launcher=lambda path, url: None)
            login_mod.status()


class WaitingForThePerson(Isolated):

    def test_it_returns_the_moment_the_profile_is_signed_in(self):
        make_cookie_db(self.profile, [(".google.com", "__Secure-1PSID", 3600)])
        started = time.monotonic()
        state = login_mod.wait(timeout_s=10)
        self.assertTrue(state["signed_in"])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("latchkey_session_open", state["next_step"])
        self.assertIn("mode='dedicated'", state["next_step"])

    def test_a_closed_window_ends_the_wait(self):
        state = login_mod.wait(timeout_s=10)
        self.assertFalse(state["window_open"])
        self.assertIn("latchkey_login", state["next_step"])

    def test_an_open_window_is_waited_on_until_the_time_is_up(self):
        login_mod._write_state({"pid": os.getpid(), "profile": self.profile, "started": 0})
        hold_lock(self.profile)
        state = login_mod.wait(timeout_s=0.6, poll_s=0.25)
        self.assertTrue(state["window_open"])
        self.assertGreaterEqual(state["waited_s"], 0.5)
        self.assertIn("30s", state["next_step"])

    def test_a_cancelled_wait_lets_go(self):
        login_mod._write_state({"pid": os.getpid(), "profile": self.profile, "started": 0})
        hold_lock(self.profile)
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        login_mod.wait(timeout_s=20, cancel=cancel)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_for_another_site_only_the_closed_window_says_it_is_done(self):
        make_cookie_db(self.profile, [(".google.com", "SID", 3600)])
        login_mod._write_state({"pid": os.getpid(), "profile": self.profile, "started": 0})
        hold_lock(self.profile)
        state = login_mod.wait(timeout_s=0.5, poll_s=0.25, host="https://canvas.example.edu/")
        self.assertGreaterEqual(state["waited_s"], 0.4, "a Google session says nothing about it")


class HandingTheProfileToAHeadlessSession(Isolated):

    def own_window(self):
        login_mod._write_state({"pid": os.getpid(), "profile": self.profile,
                                "url": "https://accounts.google.com/", "started": 0})
        hold_lock(self.profile)

    def test_a_signed_in_window_is_closed_gracefully(self):
        self.own_window()
        make_cookie_db(self.profile, [(".google.com", "SID", 3600)])
        terminate = mock.Mock(return_value=True)
        out = login_mod.release_for_automation(terminate=terminate)
        self.assertEqual(out, {"closed_login_window": os.getpid()})
        terminate.assert_called_once()
        self.assertFalse(os.path.exists(login_mod.STATE_FILE))

    def test_a_window_mid_sign_in_is_left_alone_and_said_so(self):
        self.own_window()
        terminate = mock.Mock()
        with self.assertRaises(login_mod.LoginPending) as caught:
            login_mod.release_for_automation(terminate=terminate)
        terminate.assert_not_called()
        self.assertIn("latchkey_login_status", str(caught.exception))
        self.assertIn("Never ask", str(caught.exception))

    def test_somebody_elses_chrome_is_never_closed(self):
        hold_lock(self.profile)
        terminate = mock.Mock()
        with self.assertRaises(chrome_mod.ProfileBusy):
            login_mod.release_for_automation(terminate=terminate)
        terminate.assert_not_called()

    def test_this_servers_own_session_is_named_as_such_not_as_another_chrome(self):
        """Found by the stdio smoke test: login_status on a profile held by the server's own
        dedicated session said "another latchkey server's session"."""
        hold_lock(self.profile)
        with mock.patch.object(chrome_mod, "_LAUNCHED", {os.getpid()}):
            state = login_mod.status()
            self.assertFalse(state["profile_in_use"])
            self.assertTrue(state["session_open"])
            self.assertIn("closes this server's session", state["next_step"])
            with self.assertRaises(chrome_mod.ProfileBusy) as caught:
                login_mod.release_for_automation()
            self.assertIn("this latchkey server", str(caught.exception))

    def test_a_free_profile_needs_nothing(self):
        self.assertIsNone(login_mod.release_for_automation())

    def test_a_window_that_will_not_close_is_reported(self):
        self.own_window()
        make_cookie_db(self.profile, [(".google.com", "SID", 3600)])
        with self.assertRaises(chrome_mod.ProfileBusy) as caught:
            login_mod.release_for_automation(terminate=mock.Mock(return_value=False))
        self.assertIn("Cmd-Q", str(caught.exception))

    def test_a_slow_window_with_no_pid_is_identified_once_and_written_down(self):
        login_mod._write_state({"pid": None, "profile": self.profile, "started": time.time()})
        hold_lock(self.profile)
        self.assertEqual(login_mod.window_pid(self.profile), os.getpid())
        with open(login_mod.STATE_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pid"], os.getpid())

    def test_an_old_pidless_state_does_not_claim_a_headless_sessions_lock(self):
        login_mod._write_state({"pid": None, "profile": self.profile, "started": 1.0})
        hold_lock(self.profile)
        self.assertIsNone(login_mod.window_pid(self.profile))


# -- the headless launch on the same profile ---------------------------------------

class TheAutomatedLaunchIsTheSameChromeWithAShortList(unittest.TestCase):

    def setUp(self):
        # A fake launch registers a fake pid; it must not outlive the test, or the exit
        # handler would be asked about a number some real process may be using.
        patch = mock.patch.object(chrome_mod, "_LAUNCHED", set())
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_waits_for_the_port_chrome_chose_and_hands_back_the_endpoint(self):
        with tempfile.TemporaryDirectory() as profile:
            seen = {}

            class Proc:
                pid = 4242

                def poll(self):
                    return None

            def popen(command, **kwargs):
                seen["command"] = command
                seen["kwargs"] = kwargs
                with open(os.path.join(profile, chrome_mod.DEVTOOLS_ACTIVE_PORT), "w") as fh:
                    fh.write("61234\n/devtools/browser/abc\n")
                return Proc()

            launched = chrome_mod.launch_for_automation(
                profile, args=["--accept-lang=en-US", "--window-size=1200,765"],
                chrome="/usr/bin/google-chrome", popen=popen, sleep=lambda s: None)
            self.assertEqual(launched.endpoint, "ws://127.0.0.1:61234/devtools/browser/abc")
            command = seen["command"]
            self.assertIn(f"--user-data-dir={profile}", command)
            self.assertIn("--remote-debugging-port=0", command)
            self.assertIn("--headless=new", command)
            for flag in ("--use-mock-keychain", "--enable-automation", "--password-store=basic",
                         "--disable-sync", "--disable-field-trial-config",
                         "--remote-allow-origins"):
                self.assertFalse(any(part.startswith(flag) for part in command), flag)
            self.assertTrue(seen["kwargs"]["start_new_session"])

    def test_a_stale_port_file_is_not_mistaken_for_this_launch(self):
        with tempfile.TemporaryDirectory() as profile:
            with open(os.path.join(profile, chrome_mod.DEVTOOLS_ACTIVE_PORT), "w") as fh:
                fh.write("1\n/devtools/browser/old\n")

            def popen(command, **kwargs):
                self.assertFalse(os.path.exists(
                    os.path.join(profile, chrome_mod.DEVTOOLS_ACTIVE_PORT)))
                with open(os.path.join(profile, chrome_mod.DEVTOOLS_ACTIVE_PORT), "w") as fh:
                    fh.write("5555\n/devtools/browser/new\n")
                return mock.Mock(pid=1, poll=lambda: None)

            launched = chrome_mod.launch_for_automation(profile, chrome="/x", popen=popen,
                                                        sleep=lambda s: None)
            self.assertIn("5555", launched.endpoint)

    def test_a_chrome_that_exits_at_once_says_what_it_said(self):
        with tempfile.TemporaryDirectory() as profile:
            def popen(command, stdout=None, **kwargs):
                stdout.write("Opening in existing browser session.\n")
                stdout.flush()
                return mock.Mock(pid=1, poll=lambda: 0)

            with self.assertRaises(RuntimeError) as caught:
                chrome_mod.launch_for_automation(profile, chrome="/x", popen=popen,
                                                 sleep=lambda s: None)
            self.assertIn("existing browser session", str(caught.exception))

    def test_a_locked_profile_is_refused_before_anything_starts(self):
        with tempfile.TemporaryDirectory() as profile:
            hold_lock(profile)
            with self.assertRaises(chrome_mod.ProfileBusy):
                chrome_mod.launch_for_automation(profile, chrome="/x",
                                                 popen=mock.Mock(side_effect=AssertionError))


class FakeCDP:
    def __init__(self):
        self.sent = []

    def send(self, method, params=None):
        self.sent.append(method)
        return {}


class FakePage:
    url = "about:blank"
    viewport_size = None

    def on(self, *args):
        pass

    def evaluate(self, *args):
        raise RuntimeError("no page here")


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]
        self.cdp = FakeCDP()

    def new_cdp_session(self, page):
        return self.cdp

    def cookies(self, urls=None):
        return []

    def on(self, *args):
        pass

    def add_init_script(self, **kwargs):
        pass


class ADedicatedSessionCopiesNothing(Isolated):

    def test_it_attaches_to_its_own_chrome_and_injects_nothing(self):
        context = FakeContext()
        browser_obj = mock.Mock(contexts=[context])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj
        launched = mock.Mock(endpoint="ws://127.0.0.1:1/devtools/browser/x", pid=1)
        browser = Browser(None, spec=SessionSpec(mode="dedicated", label="t"))
        browser._pw = pw
        browser.fingerprint = session_mod.fp.plan(width=1200, height=765, native=False,
                                                  ua="UA", color_scheme=None)
        with mock.patch.object(session_mod.chrome_mod, "launch_for_automation",
                               return_value=launched) as launch, \
                mock.patch.object(Browser, "_jar", side_effect=AssertionError("copied a jar")), \
                mock.patch.object(ck, "load_many", side_effect=AssertionError("real jar")), \
                mock.patch.object(session_mod.fp, "apply_identity", return_value={}):
            browser._start_dedicated(list(session_mod.STEALTH_ARGS))
        self.assertEqual(launch.call_args[0][0], self.profile)
        self.assertIn("--window-size=1200,765", launch.call_args[1]["args"])
        pw.chromium.connect_over_cdp.assert_called_once_with(launched.endpoint)
        self.assertNotIn("Network.setCookies", context.cdp.sent)
        self.assertNotIn("Network.setCookie", context.cdp.sent)
        self.assertEqual(browser.report.loaded, 0)
        self.assertIsNone(browser._cookie_hosts, "the verdict asks Chrome, not an injected set")

    def test_closing_it_is_graceful_and_certain(self):
        browser = Browser(None, spec=SessionSpec(mode="dedicated"))
        cdp = FakeCDP()
        browser._browser = mock.Mock()
        browser._browser.new_browser_cdp_session.return_value = cdp
        browser._pw = mock.Mock()
        browser._chrome = mock.Mock()
        browser.close()
        self.assertEqual(cdp.sent, ["Browser.close"])
        self.assertIsNone(browser._browser)

    def test_a_saved_google_login_is_never_loaded_into_a_copy(self):
        saved = [{"name": "SID", "value": "x", "domain": ".google.com"},
                 {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                 {"name": "user_session", "value": "x", "domain": ".github.com"},
                 {"name": "SAPISID", "value": "x", "url": "https://www.youtube.com/"}]
        kept = store.without_live_session(saved)
        self.assertEqual([c["name"] for c in kept], ["user_session"])
        with mock.patch.dict(os.environ, {"LATCHKEY_SHARE_LIVE_SESSION": "1"}):
            self.assertEqual(len(store.without_live_session(saved)), 4)


# -- the session that did not choose a mode ------------------------------------------

class RoutingBrowser(Browser):
    """A Browser whose lifecycle is a log, so a mode change can be watched without Chrome."""

    def __init__(self, spec):
        super().__init__(None, spec=spec)
        self.lifecycle = []
        self._pw = object()

    def close(self):
        self.lifecycle.append(("close", self.spec.mode))

    def start(self):
        self.lifecycle.append(("start", self.spec.mode))
        self._pw = object()
        return self


class ASessionWithNoModeGoesWhereTheSiteNeeds(Isolated):

    def test_no_mode_means_auto_and_starts_as_inject(self):
        spec = SessionSpec()
        self.assertEqual((spec.mode, spec.auto), ("inject", True))
        self.assertFalse(SessionSpec(mode="inject").auto)
        self.assertEqual(SessionSpec(mode="auto").as_dict()["auto"], True)

    def test_google_moves_it_to_its_own_profile_and_says_so(self):
        browser = RoutingBrowser(SessionSpec())
        browser.route_for("https://mail.google.com/mail/u/0/")
        self.assertEqual(browser.spec.mode, "dedicated")
        self.assertEqual(browser.lifecycle, [("close", "inject"), ("start", "dedicated")])
        notes = browser.take_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("dedicated", notes[0])
        self.assertEqual(browser.take_notes(), [], "a note is said once")

    def test_the_move_to_google_never_clones_the_real_profile_by_default(self):
        """The regression this default exists to prevent: a copy of the user's own login."""
        browser = RoutingBrowser(SessionSpec())
        browser.route_for("https://mail.google.com/")
        self.assertNotEqual(browser.spec.mode, "clone")
        self.assertFalse(browser._google_clone,
                         "nothing may ask for a copy of the real Google session")

    def test_a_configured_portal_starts_google_capable_before_its_click_driven_redirect(self):
        # Its login redirects straight into Google, and a click-driven redirect cannot change
        # browser modes midway - so it has to *start* in whatever mode Google uses.
        browser = RoutingBrowser(SessionSpec())
        with mock.patch.dict(os.environ, {google.GOOGLE_SSO_ACCOUNTS_ENV: GOOGLE_SSO_ACCOUNTS}):
            browser.route_for("https://portal.example.edu/campus/portal/students/")
        self.assertEqual(browser.spec.mode, "dedicated")
        self.assertEqual(browser.lifecycle, [("close", "inject"), ("start", "dedicated")])
        self.assertFalse(browser._google_clone)

    def test_an_explicit_inject_choice_is_still_respected_for_a_configured_portal(self):
        browser = RoutingBrowser(SessionSpec(mode="inject"))
        with mock.patch.dict(os.environ, {google.GOOGLE_SSO_ACCOUNTS_ENV: GOOGLE_SSO_ACCOUNTS}):
            browser.route_for("https://portal.example.edu/campus/portal/students/")
        self.assertEqual((browser.spec.mode, browser.lifecycle), ("inject", []))

    def test_google_moves_it_to_dedicated_when_that_mode_is_chosen(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_GOOGLE_MODE": "dedicated"}):
            browser = RoutingBrowser(SessionSpec())
            browser.route_for("https://mail.google.com/mail/u/0/")
            self.assertEqual(browser.spec.mode, "dedicated")
            self.assertIn("dedicated", browser.take_notes()[0])

    def test_and_back_to_inject_for_everything_else(self):
        browser = RoutingBrowser(SessionSpec())
        browser.route_for("https://mail.google.com/")
        browser.route_for("https://github.com/")
        self.assertEqual(browser.spec.mode, "inject")

    def test_the_same_mode_is_not_a_restart(self):
        browser = RoutingBrowser(SessionSpec())
        browser.route_for("https://github.com/")
        browser.route_for("https://example.com/")
        self.assertEqual(browser.lifecycle, [])

    def test_a_mode_somebody_chose_is_kept(self):
        for mode in ("inject", "clone", "real", "dedicated"):
            browser = RoutingBrowser(SessionSpec(mode=mode))
            browser.route_for("https://mail.google.com/")
            browser.route_for("https://github.com/")
            self.assertEqual((browser.spec.mode, browser.lifecycle), (mode, []), mode)

    def test_a_dedicated_sign_in_still_in_progress_leaves_the_working_browser_alone(self):
        # Only the dedicated target consults the sign-in lock; force that mode to cover it.
        with mock.patch.dict(os.environ, {"LATCHKEY_GOOGLE_MODE": "dedicated"}):
            login_mod._write_state({"pid": os.getpid(), "profile": self.profile,
                                    "url": "https://accounts.google.com/", "started": 0})
            hold_lock(self.profile)
            browser = RoutingBrowser(SessionSpec())
            with self.assertRaises(login_mod.LoginPending):
                browser.route_for("https://mail.google.com/")
            self.assertEqual((browser.spec.mode, browser.lifecycle), ("inject", []))

    def test_a_pinned_google_host_starts_in_latchkeys_own_profile(self):
        browser = Browser("mail.google.com", spec=SessionSpec(host="mail.google.com"))
        with mock.patch("latchkey.session.sync_playwright", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                browser.start()
        self.assertEqual(browser.spec.mode, "dedicated")


class WaitForLoginWatchesTheRightStore(Isolated):

    class State:
        def __init__(self, verdict):
            self.verdict = verdict

        def as_dict(self):
            return {"verdict": self.verdict}

    def test_an_inject_copy_asked_to_wait_for_google_says_why_not_at_once(self):
        browser = Browser(None, spec=SessionSpec(mode="inject"))
        browser.goto = mock.Mock(side_effect=AssertionError("no navigation, no loop"))
        out = browser.wait_for_login("https://mail.google.com/", timeout_s=300)
        self.assertEqual(out["status"], "wrong-mode")
        self.assertIn("dedicated", out["hint"])
        self.assertIn("device-bound", out["hint"])

    def test_a_dedicated_page_that_is_signed_out_says_login_required_at_once(self):
        browser = Browser(None, spec=SessionSpec(mode="dedicated"))
        browser.goto = lambda url, settle_ms=0: self.State("logged-out")
        browser.jar_stamp = mock.Mock(side_effect=AssertionError("watched the real jar"))
        started = time.monotonic()
        out = browser.wait_for_login("https://mail.google.com/", timeout_s=300)
        self.assertEqual(out["status"], "login-required")
        self.assertIn("latchkey login", out["hint"])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_an_ordinary_site_still_waits_on_the_users_chrome(self):
        browser = Browser(None, spec=SessionSpec())
        browser.goto = lambda url, settle_ms=0: self.State("logged-out")
        browser.jar_stamp = lambda: ("same",)
        browser.cookie_signature = lambda: "same"
        browser._publish = lambda *a, **k: None
        browser.state = lambda: self.State("logged-out")
        out = browser.wait_for_login("https://github.com/", timeout_s=1, poll_s=0.25)
        self.assertEqual(out["status"], "timeout")


# -- real mode on a Chrome that refuses it --------------------------------------------

class RealModeFailsFastAndSaysWhy(unittest.TestCase):

    def browser(self):
        return Browser(None, spec=SessionSpec(mode="real"))

    def test_chrome_136_and_later_are_not_launched_at_all(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(session_mod.chrome_mod, "devtools_active_port",
                                  return_value=None), \
                mock.patch.object(session_mod.chrome_mod, "major_version", return_value=153), \
                mock.patch.object(session_mod.chrome_mod, "lock_owner", return_value=17055), \
                mock.patch.object(session_mod.subprocess, "Popen",
                                  side_effect=AssertionError("launched Chrome")):
            os.environ.pop("LATCHKEY_CDP", None)
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as caught:
                self.browser()._real_endpoint()
            self.assertLess(time.monotonic() - started, 1.0)
        message = str(caught.exception)
        self.assertIn("Chrome 153", message)
        self.assertIn("136", message)
        self.assertIn("mode='clone'", message)
        self.assertNotIn("latchkey login", message,
                         "real mode refused Chrome: the answer is a clone, not a latchkey "
                         "sign-in on its own profile")
        self.assertIn("chrome://inspect/#remote-debugging", message)
        self.assertIn("17055", message)

    def test_an_older_chrome_that_is_running_is_not_handed_a_second_launch(self):
        with mock.patch.object(session_mod.chrome_mod, "devtools_active_port",
                               return_value=None), \
                mock.patch.object(session_mod.chrome_mod, "major_version", return_value=120), \
                mock.patch.object(session_mod.chrome_mod, "lock_owner", return_value=999), \
                mock.patch.object(session_mod.subprocess, "Popen",
                                  side_effect=AssertionError("launched Chrome")):
            os.environ.pop("LATCHKEY_CDP", None)
            with self.assertRaises(RuntimeError) as caught:
                self.browser()._real_endpoint()
        self.assertIn("already running", str(caught.exception))

    def test_a_chrome_that_allowed_debugging_is_attached_to(self):
        with mock.patch.object(session_mod.chrome_mod, "devtools_active_port",
                               return_value=(9229, "/devtools/browser/abc")):
            os.environ.pop("LATCHKEY_CDP", None)
            browser = self.browser()
            self.assertEqual(browser._real_endpoint(), "ws://127.0.0.1:9229/devtools/browser/abc")
            self.assertFalse(browser._real_launched)

    def test_a_port_file_nobody_answers_is_not_an_endpoint(self):
        with tempfile.TemporaryDirectory() as profile:
            with open(os.path.join(profile, chrome_mod.DEVTOOLS_ACTIVE_PORT), "w") as fh:
                fh.write("9\n/devtools/browser/gone\n")
            self.assertIsNone(chrome_mod.devtools_active_port(profile))
            self.assertEqual(chrome_mod.devtools_active_port(profile, probe=False),
                             (9, "/devtools/browser/gone"))


class ACrashedServerDoesNotLockTheProfileForever(Isolated):
    """Found in review: a Playwright launch died with its driver, a detached Chrome does not.
    One SIGKILL of the server used to mean ProfileBusy for every Google session after it."""

    def orphan(self, parent=1, flags="--remote-debugging-port=0 --headless=new"):
        return lambda pid: (parent, f"/Applications/Google Chrome.app/Contents/MacOS/Google "
                                    f"Chrome --user-data-dir={self.profile} {flags} about:blank")

    def test_the_orphan_is_recognised_exactly(self):
        self.assertTrue(chrome_mod.orphaned_automation(1, self.profile, info=self.orphan()))
        self.assertFalse(chrome_mod.orphaned_automation(1, self.profile,
                                                        info=self.orphan(parent=4321)),
                         "a live server's Chrome is that server's")
        self.assertFalse(chrome_mod.orphaned_automation(1, self.profile,
                                                        info=self.orphan(flags="")),
                         "the sign-in window is not an automation Chrome")
        self.assertFalse(chrome_mod.orphaned_automation(1, "/other/profile",
                                                        info=self.orphan()))
        self.assertFalse(chrome_mod.orphaned_automation(1, self.profile, info=lambda pid: None))

    def test_an_orphan_is_reclaimed_before_a_session_starts(self):
        hold_lock(self.profile)
        with mock.patch.object(chrome_mod, "orphaned_automation", return_value=True):
            terminate = mock.Mock(return_value=True)
            self.assertEqual(login_mod.release_for_automation(terminate=terminate),
                             {"reclaimed_orphan": os.getpid()})
        terminate.assert_called_once()

    def test_a_session_that_cannot_go_back_either_says_the_session_is_gone(self):
        class Broken(RoutingBrowser):
            def start(self):
                raise RuntimeError(f"no Chrome for {self.spec.mode}")

        browser = Broken(SessionSpec())
        with self.assertRaises(RuntimeError) as caught:
            browser.route_for("https://mail.google.com/")
        self.assertIn("latchkey_session_close", str(caught.exception))
        self.assertIn("inject", str(caught.exception))

    def test_the_exit_handler_only_stops_a_chrome_that_still_looks_like_ours(self):
        with mock.patch.object(chrome_mod, "_LAUNCHED", {424242}), \
                mock.patch.object(chrome_mod, "process_info",
                                  return_value=(1, "/usr/sbin/somethingelse")), \
                mock.patch.object(chrome_mod.os, "kill") as kill:
            chrome_mod._stop_launched()
        kill.assert_not_called()

    def test_the_state_file_is_created_private(self):
        login_mod._write_state({"pid": 1})
        self.assertEqual(os.stat(login_mod.STATE_FILE).st_mode & 0o777, 0o600)


class OnlyInjectIsRefusedABoundHost(unittest.TestCase):
    """Clone carries the registration and runs on the real keychain, so it is the mode that
    works for a bound host - it is never refused, whatever the bindings say. Only inject is."""

    @staticmethod
    def bind(root: str) -> None:
        directory = os.path.join(root, "Default")
        os.makedirs(directory, exist_ok=True)
        con = sqlite3.connect(os.path.join(directory, profile_mod.DBSC_FILE))
        con.execute("create table dbsc_session_tbl (key text, proto blob, primary key(key))")
        con.execute("insert into dbsc_session_tbl values ('https://google.com', x'0a')")
        con.commit()
        con.close()

    def test_a_clone_is_allowed_even_with_a_binding_present(self):
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as clone:
            self.bind(real)
            with mock.patch.object(profile_mod, "CHROME_ROOT", real), \
                    mock.patch.object(profile_mod, "CLONE_DIR", clone):
                browser = Browser(None, spec=SessionSpec(mode="clone", host="mail.google.com"))
                self.assertEqual(browser._device_bound_refusal(), "")

    def test_inject_is_refused_when_the_binding_is_present(self):
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as clone:
            self.bind(real)
            with mock.patch.object(profile_mod, "CHROME_ROOT", real), \
                    mock.patch.object(profile_mod, "CLONE_DIR", clone):
                browser = Browser(None, spec=SessionSpec(mode="inject", host="mail.google.com"))
                self.assertIn("device bound", browser._device_bound_refusal())

    def test_inject_without_a_binding_anywhere_is_not_refused(self):
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as clone:
            with mock.patch.object(profile_mod, "CHROME_ROOT", real), \
                    mock.patch.object(profile_mod, "CLONE_DIR", clone):
                browser = Browser(None, spec=SessionSpec(mode="inject", host="mail.google.com"))
                self.assertEqual(browser._device_bound_refusal(), "")


# -- the MCP surface -------------------------------------------------------------------

class Agent:
    """A browser double for the MCP tools, carrying a spec like the real one."""

    def __init__(self, spec, state):
        self.spec = spec or SessionSpec()
        self.label = "a"
        self.calls = []
        self._state = state
        self.notes = []

    def goto(self, url, settle_ms=2500, text_limit=4000):
        self.calls.append(("goto", url))
        return self._state(url)

    def take_notes(self):
        notes, self.notes = self.notes, []
        return notes

    def describe(self):
        return {"label": self.label, "spec": self.spec.as_dict(),
                "report": {"withheld_live_session": 8 if self.spec.mode == "inject" else 0}}

    @property
    def profiles(self):
        return ["Default"]

    def close(self):
        pass


class TheToolsSendGoogleToTheOneTimeSignIn(Isolated):

    def setUp(self):
        super().setUp()
        self.agents = []

        def factory(spec=None):
            agent = Agent(spec, lambda url: PageState(
                url="https://accounts.google.com/v3/signin/identifier?x=1", title="Sign in",
                host_cookies=0))
            self.agents.append(agent)
            return agent

        self.registry = SessionRegistry(factory)
        real = mcp_server.registry
        mcp_server.registry = self.registry
        self.addCleanup(lambda: setattr(mcp_server, "registry", real))
        self.addCleanup(self.registry.close_all)

    def test_a_signed_out_google_page_in_the_dedicated_profile_points_at_latchkey_login(self):
        mcp_server.tool_session_open(name="g", mode="dedicated")
        out = mcp_server.tool_open("https://mail.google.com/", session="g")
        self.assertEqual(out["verdict"], "logged-out")
        self.assertIn("latchkey_login", out["next_step"])
        self.assertIn("Never ask the user to type a password", out["next_step"])
        self.assertNotIn("wait_for_login", out["next_step"])
        self.assertNotIn("other_profiles", out, "the user's Chrome is not where this lives")

    def test_an_inject_copy_asked_for_by_name_is_sent_to_latchkeys_own_profile(self):
        mcp_server.tool_session_open(name="c", mode="inject")
        out = mcp_server.tool_open("https://mail.google.com/", session="c")
        self.assertIn("device-bound", out["next_step"])
        self.assertIn("'dedicated'", out["next_step"])
        self.assertNotIn("'clone'", out["next_step"],
                         "a copy of the user's login is what this default exists to avoid")

    def test_an_ordinary_site_keeps_the_old_answer_and_gains_the_rule(self):
        mcp_server.tool_session_open(name="o")
        next_step = mcp_server.logged_out_next_step("https://github.com/login", "inject")
        self.assertIn("latchkey_wait_for_login", next_step)
        self.assertIn("Never ask the user to type a password", next_step)

    def test_session_open_with_no_mode_is_auto_and_a_copy_says_what_it_held_back(self):
        out = mcp_server.tool_session_open(name="plain")
        spec = self.registry.spec_of("plain")
        self.assertTrue(spec.auto)
        self.assertIn("held back", out["google"])
        self.assertIn("latchkey's own signed-in profile", out["google"])

    def test_session_open_accepts_the_names_models_use_for_modes(self):
        # "google" now maps to the dedicated profile - the mode Google actually uses -
        # not the clone that used to sign the user out.
        mcp_server.tool_session_open(name="x", mode="google")
        self.assertEqual(self.registry.spec_of("x").mode, "dedicated")
        with self.assertRaises(ValueError) as caught:
            mcp_server.tool_session_open(name="y", mode="sideways")
        self.assertIn("auto, inject, dedicated, clone, real", str(caught.exception))

    def test_a_dedicated_session_reports_whether_the_profile_is_signed_in(self):
        out = mcp_server.tool_session_open(name="d", mode="dedicated")
        self.assertFalse(out["google_signed_in"])
        self.assertIn("latchkey_login", out["next_step"])

    def test_wait_for_login_on_google_watches_latchkeys_own_profile(self):
        """A session that chose no mode is still reading 'inject' until it navigates.

        Google goes to latchkey's own profile, so that is the store this sign-in lands in -
        and answering wrong-mode to a session that was about to be in exactly the right mode
        was a dead end whose advice ("open it with no mode") was what the caller had done.
        """
        mcp_server.tool_session_open(name="w")
        self.assertEqual(self.registry.spec_of("w").mode, "inject", "auto starts as inject")
        with mock.patch.object(login_mod, "wait", wraps=login_mod.wait) as wait:
            out = mcp_server.tool_wait_for_login("https://mail.google.com/", timeout_s=1,
                                                 session="w")
        self.assertNotEqual(out.get("status"), "wrong-mode")
        wait.assert_called()
        self.assertEqual(self.agents[0].calls, [], "the session was not driven into a loop")

    def test_a_session_pinned_away_from_google_is_told_which_mode_it_needs(self):
        mcp_server.tool_session_open(name="w2", mode="clone")
        make_cookie_db(self.profile, [(".google.com", "SID", 3600)])
        with mock.patch.object(login_mod, "wait", wraps=login_mod.wait) as wait:
            out = mcp_server.tool_wait_for_login("https://mail.google.com/", session="w2")
        self.assertEqual(out["status"], "wrong-mode")
        self.assertIn("dedicated", out["next_step"])
        self.assertIn("latchkey_login", out["next_step"])
        wait.assert_not_called()
        self.assertEqual(self.agents[0].calls, [])

    def test_a_dedicated_wait_still_watches_the_own_profile(self):
        mcp_server.tool_session_open(name="d", mode="dedicated")
        with mock.patch.object(login_mod, "wait", wraps=login_mod.wait) as wait:
            out = mcp_server.tool_wait_for_login("https://mail.google.com/", session="d")
        self.assertEqual(out["status"], "login-required")
        self.assertIn("latchkey_login", out["next_step"])
        wait.assert_called_once()
        self.assertEqual(self.agents[0].calls, [], "the session was not driven into a loop")

    def test_an_explicit_copy_waiting_for_google_is_wrong_mode(self):
        mcp_server.tool_session_open(name="i", mode="inject")
        out = mcp_server.tool_wait_for_login("https://mail.google.com/", session="i")
        self.assertEqual(out["status"], "wrong-mode")

    def test_login_closes_this_servers_dedicated_sessions_and_returns_at_once(self):
        mcp_server.tool_session_open(name="d1", mode="dedicated")
        mcp_server.tool_session_open(name="other", mode="inject")
        with mock.patch.object(login_mod, "start",
                               return_value={"status": "window-open"}) as start:
            out = mcp_server.tool_login()
        self.assertEqual(out["closed_sessions"], ["d1"])
        self.assertEqual(self.registry.names(), ["other"])
        start.assert_called_once()

    def test_login_status_is_capped_under_the_clients_patience(self):
        with mock.patch.object(login_mod, "wait", return_value={"signed_in": False}) as wait:
            mcp_server.tool_login_status(wait_s=600)
        self.assertLessEqual(wait.call_args[1]["timeout_s"], mcp_server.LOGIN_WAIT_S)

    def test_the_login_tools_touch_no_session_lane(self):
        self.assertIsNone(mcp_server.lane_of_call("latchkey_login", {}))
        self.assertIsNone(mcp_server.lane_of_call("latchkey_login_status", {}))

    def test_a_pending_sign_in_is_spoken_not_traced(self):
        self.assertIn(login_mod.LoginPending, mcp_server.SPOKEN_ERRORS)
        self.assertIn(chrome_mod.ProfileBusy, mcp_server.SPOKEN_ERRORS)


if __name__ == "__main__":
    unittest.main()


class WindowSiteIsReported(unittest.TestCase):
    def test_next_step_names_the_site_the_window_was_opened_for(self):
        from latchkey import login
        text = login.next_step({"signed_in": False, "window_open": True,
                                "window_url": "https://www.ebay.com/sh/lst/active"})
        self.assertIn("www.ebay.com", text)
        self.assertIn("Never ask the user for a password", text)
        plain = login.next_step({"signed_in": False, "window_open": True})
        self.assertIn("A sign-in window is open", plain)
