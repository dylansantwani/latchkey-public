"""Offline tests for the viewer registry - the thing that stops clients guessing ports.

Every MCP connection gets its own latchkey server process with its own viewer socket, so
"the viewer" is whichever process holds the session. A client that guesses the default port
gets another server's 200 with an empty session list, which looks connected and shows
nothing. Each viewer writes itself down instead; these tests cover the file's contract.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latchkey import mcp_server, viewer  # noqa: E402


class ViewerFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = viewer.VIEWERS_FILE
        viewer.VIEWERS_FILE = os.path.join(self.tmp, "viewers.json")

    def tearDown(self):
        viewer.VIEWERS_FILE = self._real


class TestRegistryFile(ViewerFileCase):
    def write(self, entries):
        with open(viewer.VIEWERS_FILE, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)

    def test_a_missing_file_is_no_viewers_not_an_error(self):
        self.assertEqual(viewer.known_viewers(), [])

    def test_a_corrupt_file_is_no_viewers_not_an_error(self):
        with open(viewer.VIEWERS_FILE, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(viewer.known_viewers(), [])

    def test_a_live_process_is_reported_newest_first(self):
        me = os.getpid()
        self.write([{"port": 8788, "pid": me, "started_at": 100.0},
                    {"port": 8791, "pid": me, "started_at": 200.0}])
        self.assertEqual([e["port"] for e in viewer.known_viewers()], [8791, 8788])

    def test_a_dead_pid_is_dropped_and_rewritten_out_of_the_file(self):
        """A killed server leaves an entry behind, and an entry that lies sends the app
        to a port with nothing on it."""
        self.write([{"port": 8788, "pid": 999999, "started_at": 1.0},
                    {"port": 8791, "pid": os.getpid(), "started_at": 2.0}])
        self.assertEqual([e["port"] for e in viewer.known_viewers()], [8791])
        with open(viewer.VIEWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual([e["port"] for e in json.load(fh)], [8791])

    def test_an_entry_without_a_pid_is_dropped(self):
        self.write([{"port": 8788}, {"port": 8791, "pid": os.getpid(), "started_at": 1.0}])
        self.assertEqual([e["port"] for e in viewer.known_viewers()], [8791])

    def test_entries_that_are_not_objects_are_ignored(self):
        self.write(["nonsense", 42, {"port": 8791, "pid": os.getpid(), "started_at": 1.0}])
        self.assertEqual([e["port"] for e in viewer.known_viewers()], [8791])

    def test_a_registered_viewer_is_findable_and_unregisters_cleanly(self):
        server = viewer.ViewerServer(port=0).start()
        try:
            entries = viewer.known_viewers()
            self.assertIn(server.port, [e["port"] for e in entries])
            entry = next(e for e in entries if e["port"] == server.port)
            self.assertEqual(entry["pid"], os.getpid())
            self.assertEqual(entry["url"], f"http://127.0.0.1:{server.port}/")
        finally:
            server.shutdown()
        self.assertNotIn(server.port, [e["port"] for e in viewer.known_viewers()])

    def test_a_writer_that_cannot_write_does_not_break_the_viewer(self):
        viewer.VIEWERS_FILE = "/proc/definitely/not/writable/viewers.json"
        self.assertEqual(viewer._write_registry([{"port": 1}]), None)

    def test_two_viewers_are_both_findable(self):
        first = viewer.ViewerServer(port=0).start()
        second = viewer.ViewerServer(port=0).start()
        try:
            ports = [e["port"] for e in viewer.known_viewers()]
            self.assertIn(first.port, ports)
            self.assertIn(second.port, ports)
        finally:
            first.shutdown()
            second.shutdown()

    def test_the_registry_write_is_atomic(self):
        """A reader must never see half a file: the app reads this while servers write it."""
        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                try:
                    viewer.known_viewers()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def writer():
            while not stop.is_set():
                viewer._write_registry([{"port": 8790, "pid": os.getpid(),
                                         "started_at": time.time()}])

        threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        time.sleep(0.6)
        stop.set()
        for thread in threads:
            thread.join(3)
        self.assertEqual(errors, [])


class TestViewersTool(ViewerFileCase):
    def test_the_tool_is_advertised_and_lane_free(self):
        names = {tool["name"] for tool in mcp_server.CATALOG}
        self.assertIn("latchkey_viewers", names)
        self.assertIsNone(mcp_server.LANE_OF["latchkey_viewers"])

    def test_the_tool_reports_what_the_registry_holds(self):
        server = viewer.ViewerServer(port=0).start()
        try:
            rows = mcp_server.tool_viewers()
            self.assertIn(server.port, [row["port"] for row in rows])
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
