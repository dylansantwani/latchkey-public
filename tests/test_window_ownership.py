"""Two models must never want the same window, and must never take each other's.

The bug these cover: every clone directory was one machine-wide fixed path
(`/tmp/latchkey-profile-clone`, `~/.latchkey/google-clone`), so a second latchkey server
opening the same site wanted the same Chrome profile - and Chrome allows one browser per
profile only by killing whatever holds the lock. Two things let it do that: the orphan test
read "the server that opened this is gone" from a parent pid that is *always* 1, because
launches here are detached (`start_new_session=True`), and `_clear_clone_holder` ended the
holder on the assumption that the directory was latchkey's alone. Two models therefore took
turns terminating each other's Chrome and re-seeding the profile under it, which from the
outside looks like models using each other's windows.

    python3 -m unittest tests.test_window_ownership -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from latchkey import chrome, google, login, paths, profile, session, viewer
from tests.test_google_login import hold_lock, make_cookie_db

LIVE = 38001          # a Chrome this test pretends a *live* server opened
DEAD = 38002          # one whose server has exited


class TmpHome(unittest.TestCase):
    """Everything latchkey writes goes to a temporary directory, never the real one."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        home = mock.patch.dict(os.environ, {"LATCHKEY_HOME": self.home})
        home.start()
        self.addCleanup(home.stop)
        # A pinned path would defeat the very thing under test.
        for name in ("LATCHKEY_CLONE_DIR", "LATCHKEY_GOOGLE_CLONE_DIR"):
            os.environ.pop(name, None)
            self.addCleanup(lambda n=name: os.environ.pop(n, None))
        self.owners = os.path.join(self.home, "owners")
        patched = mock.patch.object(paths, "owners_dir", return_value=self.owners)
        patched.start()
        self.addCleanup(patched.stop)


class ADirectoryPerServerNotPerMachine(TmpHome):

    def test_the_google_clone_is_not_a_path_two_servers_share(self):
        with mock.patch.object(paths, "owner_id", return_value="111-1"):
            first = google.google_clone_dir()
        with mock.patch.object(paths, "owner_id", return_value="222-2"):
            second = google.google_clone_dir()
        self.assertNotEqual(first, second, "two servers would fight over one Chrome profile")
        self.assertIn(os.path.join("clones", "111-1"), first)
        self.assertTrue(first.endswith("google-clone"))

    def test_a_pinned_google_clone_dir_still_wins(self):
        with mock.patch.dict(os.environ, {"LATCHKEY_GOOGLE_CLONE_DIR": "/tmp/gc-pinned"}):
            self.assertEqual(google.google_clone_dir(), "/tmp/gc-pinned")

    def test_the_throwaway_clone_is_not_a_shared_tmp_path(self):
        self.assertIn(os.path.join("clones", paths.owner_id()), profile.CLONE_DIR)
        self.assertNotIn("/tmp/latchkey-profile-clone", profile.CLONE_DIR)


class OnlyTheServerThatOpenedAWindowOwnsIt(TmpHome):

    def info(self, pid):
        """A launch of ours, seen from another process: detached, so parent == 1."""
        return (1, f"--user-data-dir=/tmp/dir{pid} --remote-debugging-port=0 --headless")

    def test_a_live_servers_note_beats_the_parent_pid(self):
        chrome.record_owner(LIVE, f"/tmp/dir{LIVE}", session="gmail")
        self.assertIsNotNone(chrome.live_owner(LIVE))
        self.assertFalse(chrome.orphaned_automation(LIVE, f"/tmp/dir{LIVE}", info=self.info),
                         "another live server's Chrome was read as an orphan to kill")

    def test_a_note_whose_server_is_gone_leaves_a_real_orphan(self):
        os.makedirs(self.owners, exist_ok=True)
        with open(chrome._owner_file(DEAD), "w", encoding="utf-8") as fh:
            json.dump({"pid": DEAD, "profile_dir": f"/tmp/dir{DEAD}",
                       "server_pid": 999999999, "session": "gone"}, fh)
        self.assertIsNone(chrome.live_owner(DEAD), "a dead server's note still claimed it")
        self.assertTrue(chrome.orphaned_automation(DEAD, f"/tmp/dir{DEAD}", info=self.info))

    def test_a_note_about_a_chrome_that_has_exited_is_not_kept(self):
        os.makedirs(self.owners, exist_ok=True)
        with open(chrome._owner_file(DEAD), "w", encoding="utf-8") as fh:
            json.dump({"pid": DEAD, "profile_dir": "/tmp/dir",
                       "server_pid": os.getpid()}, fh)
        chrome.record_owner(LIVE, f"/tmp/dir{LIVE}")
        self.assertFalse(os.path.exists(chrome._owner_file(DEAD)),
                         "a note about a Chrome that has exited is not worth keeping")

    def test_a_note_about_a_profile_that_is_gone_is_not_kept_either(self):
        os.makedirs(self.owners, exist_ok=True)
        with open(chrome._owner_file(4242), "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "profile_dir": "/tmp/never-existed-latchkey",
                       "server_pid": os.getpid()}, fh)
        chrome.record_owner(LIVE, f"/tmp/dir{LIVE}")
        self.assertFalse(os.path.exists(chrome._owner_file(4242)),
                         "a note about a profile directory that is gone protects nothing")

    def test_another_process_cannot_observe_or_clean_a_note_mid_update(self):
        """The shared lock makes cleanup wait for a concurrent server's atomic update."""
        parent_profile = os.path.join(self.home, "parent-profile")
        child_profile = os.path.join(self.home, "child-profile")
        os.makedirs(parent_profile)
        os.makedirs(child_profile)
        chrome.record_owner(os.getpid(), parent_profile)
        code = ("import os, sys; from latchkey import chrome; "
                "chrome.record_owner(os.getpid(), sys.argv[1])")
        env = {**os.environ, "LATCHKEY_HOME": self.home}
        with chrome._owners_lock():
            child = subprocess.Popen([sys.executable, "-c", code, child_profile], env=env)
            try:
                time.sleep(0.15)
                self.assertFalse(os.path.exists(chrome._owner_file(child.pid)),
                                 "the competing server wrote while ownership cleanup held lock")
            finally:
                # Leaving the context lets the child publish its note; it is then still
                # a live claim when its cleanup scan runs.
                pass
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertIsNotNone(chrome.live_owner(os.getpid()))
        self.assertIsNotNone(chrome.owner_record(child.pid))

    def test_a_foreign_window_is_named_and_never_offered(self):
        with mock.patch.object(chrome, "owner_record",
                               return_value={"server_pid": 424242, "session": "gmail"}):
            message = str(chrome.busy_here(LIVE, f"/tmp/dir{LIVE}"))
        self.assertIn("424242", message)
        self.assertIn("gmail", message)
        self.assertNotIn("pass its name as `session`", message,
                         "another server's session name does not exist here, so a model told "
                         "to pass it along is reaching for a window that is not its own")

    def test_our_own_other_session_is_still_pointed_at(self):
        with mock.patch.object(chrome, "owner_record",
                               return_value={"server_pid": os.getpid(), "session": "inbox"}):
            message = str(chrome.busy_here(LIVE, f"/tmp/dir{LIVE}"))
        self.assertIn("pass its name as `session`", message)


class ClearingACloneNeverKillsAWorkingModel(TmpHome):

    def test_another_live_servers_chrome_is_left_standing(self):
        with mock.patch.object(session.chrome_mod, "lock_owner", return_value=LIVE), \
                mock.patch.object(session.chrome_mod, "live_owner",
                                  return_value={"server_pid": 424242, "session": "gmail"}), \
                mock.patch.object(session.chrome_mod, "terminate") as ended, \
                mock.patch.object(session.profile_mod, "_remove_singletons") as cleaned:
            with self.assertRaises(chrome.ProfileBusy) as caught:
                session.Browser._clear_clone_holder("/tmp/clone", timeout_s=0.0)
        self.assertIn("424242", str(caught.exception))
        ended.assert_not_called()
        cleaned.assert_not_called()

    def test_a_dead_servers_leftover_chrome_is_cleared(self):
        with mock.patch.object(session.chrome_mod, "lock_owner", return_value=LIVE), \
                mock.patch.object(session.chrome_mod, "live_owner", return_value=None), \
                mock.patch.object(session.chrome_mod, "terminate") as ended, \
                mock.patch.object(session.profile_mod, "_remove_singletons") as cleaned:
            session.Browser._clear_clone_holder("/tmp/clone", timeout_s=0.0)
        self.assertEqual(ended.call_args[0][0], LIVE)
        cleaned.assert_called_once()


class TheSignInWindowBelongsToWhoeverOpenedIt(TmpHome):
    """Seen in the wild: one machine, one login state file, two models.

    `release_for_automation` read the pid out of that shared file, matched it against the
    profile lock, and quit the window - which for the human meant the Chrome window they were
    signing in in vanished, because a *second* model had a dedicated session to start. The
    window is now owned like every other window: noted at launch, and never closed by a
    server that did not open it.
    """

    def setUp(self):
        super().setUp()
        self.profile = os.path.join(self.home, "profile")
        os.makedirs(self.profile, exist_ok=True)
        state = mock.patch.object(login, "STATE_FILE", os.path.join(self.home, "login.json"))
        state.start()
        self.addCleanup(state.stop)
        make_cookie_db(self.profile, [(".google.com", "SID", 3600)])

    def a_window(self, server_pid):
        """A sign-in window, recording who opened it.

        `hold_lock` is what makes it a real window to the rest of the module: the profile is
        locked by a live pid, which is what `window_pid` reads.
        """
        hold_lock(self.profile)
        login._write_state({"pid": os.getpid(), "profile": self.profile,
                            "url": "https://accounts.google.com/", "started": 0,
                            "server_pid": server_pid, "server": f"{server_pid}-1"})

    def test_closing_refuses_a_window_another_server_opened(self):
        self.a_window(424242)
        terminate = mock.Mock(return_value=True)
        with mock.patch.object(login.chrome_mod, "live_owner",
                               return_value={"server_pid": 424242, "session": "login-window"}):
            with self.assertRaises(chrome.ProfileBusy) as caught:
                login.close_window(self.profile, terminate=terminate)
        terminate.assert_not_called()
        self.assertIn("424242", str(caught.exception))

    def test_handing_the_profile_over_refuses_a_foreign_window(self):
        self.a_window(424242)
        with mock.patch.object(login.chrome_mod, "lock_owner", return_value=os.getpid()), \
                mock.patch.object(login.chrome_mod, "live_owner",
                                  return_value={"server_pid": 424242, "session": "login-window"}), \
                mock.patch.object(login.chrome_mod, "terminate") as ended:
            with self.assertRaises(chrome.ProfileBusy) as caught:
                login.release_for_automation(self.profile)
        ended.assert_not_called()
        self.assertIn("424242", str(caught.exception))

    def test_start_does_not_adopt_or_reopen_another_servers_window(self):
        self.a_window(424242)
        launcher = mock.Mock(side_effect=AssertionError("opened a second window"))
        with mock.patch.object(login.chrome_mod, "live_owner",
                               return_value={"server_pid": 424242}):
            out = login.start(profile_dir=self.profile, launcher=launcher)
        self.assertEqual(out["status"], "window-already-open")
        self.assertEqual(out["window_owner"]["server_pid"], 424242)
        self.assertIn("not this server's", out["next_step"])
        self.assertEqual(launcher.call_count, 0)

    def test_status_names_the_server_that_owns_the_open_window(self):
        self.a_window(424242)
        with mock.patch.object(login.chrome_mod, "live_owner",
                               return_value={"server_pid": 424242, "session": "login-window"}):
            state = login.status(self.profile)
        self.assertEqual(state["window_owner"]["server_pid"], 424242)
        self.assertIn("another latchkey server", state["next_step"])

    def test_a_window_this_server_opened_is_still_ours_to_close(self):
        self.a_window(os.getpid())
        terminate = mock.Mock(return_value=True)
        with mock.patch.object(login.chrome_mod, "live_owner",
                               return_value={"server_pid": os.getpid()}), \
                mock.patch.object(login.chrome_mod, "lock_owner", return_value=os.getpid()):
            out = login.release_for_automation(self.profile, terminate=terminate)
        self.assertEqual(out, {"closed_login_window": os.getpid()})
        terminate.assert_called_once()


class TheWindowRegistryKeepsEveryServersWindows(TmpHome):
    """One file lists every viewer window on the machine, for every server and model.

    Each start and stop read it, changed it and wrote it back with no lock, so two servers
    coming up together could drop each other's entry - and a client that finds windows in
    there then opens a viewer belonging to another connection. The entry also carries the
    owner now, so a window can be traced back to the server that opened it.
    """

    def setUp(self):
        super().setUp()
        self.registry = os.path.join(self.home, "viewers.json")
        patched = mock.patch.object(viewer, "VIEWERS_FILE", self.registry)
        patched.start()
        self.addCleanup(patched.stop)

    def a_foreign_viewer(self):
        # pid 1 is a live process that is not this one: another server, in effect.
        viewer._write_registry([{"port": 8788, "pid": 1, "host": "127.0.0.1",
                                 "url": "http://127.0.0.1:8788/", "started_at": 1.0}])

    def test_registering_keeps_another_servers_window(self):
        self.a_foreign_viewer()
        server = mock.Mock(port=8788, host="127.0.0.1")
        server.url.return_value = "http://127.0.0.1:8789/"
        viewer._register(server)
        entries = viewer._read_registry()
        self.assertEqual(sorted(e["pid"] for e in entries), [1, os.getpid()],
                         "one server's window was dropped from the registry by another")
        ours = [e for e in entries if e["pid"] == os.getpid()][0]
        self.assertEqual(ours["owner"], paths.owner_id(),
                         "a window must be traceable to the server that opened it")

    def test_unregistering_only_removes_our_own_window(self):
        self.a_foreign_viewer()
        viewer._unregister(8788)
        self.assertEqual([e["pid"] for e in viewer._read_registry()], [1])


class CloneDirectoriesDoNotPileUp(TmpHome):

    def test_a_dead_servers_clone_is_cleaned_up_and_a_live_ones_is_left(self):
        root = os.path.join(self.home, "clones")
        dead = os.path.join(root, "999999999-1")
        ours = os.path.join(root, paths.owner_id())
        os.makedirs(os.path.join(dead, "Default"))
        os.makedirs(os.path.join(ours, "Default"))
        self.assertEqual(paths.prune_clones(), 1)
        self.assertFalse(os.path.isdir(dead))
        self.assertTrue(os.path.isdir(ours), "a live server's clone is somebody's browser")


if __name__ == "__main__":
    unittest.main()
