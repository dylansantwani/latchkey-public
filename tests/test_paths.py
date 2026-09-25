"""latchkey's own directory, and the move from the name it used to have.

A rename that quietly abandons `~/.abrowser` abandons a signed-in Chrome profile with
it - the dedicated mode's whole point is that you sign into it once - so anything left
under the old name is carried across the first time the package is imported.

    python3 -m unittest tests.test_paths -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import paths  # noqa: E402


class MovingIn(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.old = os.path.join(self.root, "old")
        self.new = os.path.join(self.root, "new")

    def make(self, where, *names):
        for name in names:
            target = os.path.join(where, name)
            os.makedirs(os.path.dirname(target) or target, exist_ok=True)
            if name.endswith(".json"):
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("{}")
            else:
                os.makedirs(target, exist_ok=True)

    def test_what_the_old_name_held_is_carried_across(self):
        self.make(self.old, "sessions", "profile", "credentials.json")
        moved = paths.migrate(self.old, self.new)
        self.assertEqual(sorted(moved), ["credentials.json", "profile", "sessions"])
        for name in moved:
            self.assertTrue(os.path.exists(os.path.join(self.new, name)), name)
            self.assertFalse(os.path.exists(os.path.join(self.old, name)), name)

    def test_nothing_is_ever_moved_over_the_top_of_something(self):
        self.make(self.old, "sessions")
        self.make(self.new, "sessions")
        self.assertEqual(paths.migrate(self.old, self.new), [])
        self.assertTrue(os.path.isdir(os.path.join(self.old, "sessions")),
                        "and the old one is left where it is rather than deleted")

    def test_anything_the_package_does_not_own_is_left_alone(self):
        self.make(self.old, "sessions", "notes.txt")
        paths.migrate(self.old, self.new)
        self.assertTrue(os.path.exists(os.path.join(self.old, "notes.txt")))

    def test_no_old_directory_is_not_an_error(self):
        self.assertEqual(paths.migrate(os.path.join(self.root, "nope"), self.new), [])

    def test_migrating_a_directory_onto_itself_does_nothing(self):
        self.make(self.old, "sessions")
        self.assertEqual(paths.migrate(self.old, self.old), [])


class EverythingWritesUnderOneRoot(unittest.TestCase):
    def test_every_path_the_package_writes_is_declared_through_one_root(self):
        """Source-level on purpose: the tests patch some of these constants at runtime,
        and what matters is that no module spells the home directory out for itself."""
        import inspect
        from latchkey import credentials, detect, profile, store, viewer
        wanted = {store: "SESSION_DIR", credentials: "VAULT", detect: "HINTS_FILE",
                  profile: "DEDICATED_DIR", viewer: "VIEWERS_FILE"}
        for module, name in wanted.items():
            source = inspect.getsource(module)
            self.assertIn(f"{name} = paths.path(", source, module.__name__)
            self.assertNotIn('expanduser("~/.latchkey', source, module.__name__)
            self.assertNotIn(".abrowser", source, module.__name__)

    def test_the_root_can_be_moved(self):
        self.assertTrue(paths.path("sessions").endswith("sessions"))
        self.assertEqual(paths.path(), paths.HOME)


if __name__ == "__main__":
    unittest.main()
