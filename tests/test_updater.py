"""The self-update path: the swap, the leftover cleanup, and the checks the
app makes against a published release.

The reported symptom this guards against: after updating, a 60 MB
"Roblox Rejoiner.exe.old<pid>" file stayed next to the app forever, because
the cleanup ran once (while the old process still held the lock) and never
retried.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import updater                                       # noqa: E402
from rejoiner_constants import APP_VERSION           # noqa: E402


class VersionTests(unittest.TestCase):
    def test_newer_tags_are_detected(self):
        for tag in ("v1.2.1", "1.2.1", "v1.3.0", "v2.0.0", "v1.2.0.1"):
            with self.subTest(tag=tag):
                self.assertTrue(updater.is_newer(tag, "1.2.0"))

    def test_same_or_older_is_not_an_update(self):
        for tag in ("v1.2.0", "1.2.0", "v1.1.9", "v1.0.0", "", "garbage"):
            with self.subTest(tag=tag):
                self.assertFalse(updater.is_newer(tag, "1.2.0"))

    def test_the_shipped_version_offers_the_next_one(self):
        """The next patch release is always seen as an update, whichever
        version this build happens to be."""
        parts = [int(x) for x in APP_VERSION.split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        parts[-1] += 1
        nxt = "v" + ".".join(str(x) for x in parts)
        self.assertTrue(updater.is_newer(nxt, APP_VERSION))
        self.assertFalse(updater.is_newer(f"v{APP_VERSION}", APP_VERSION))

    def test_asset_names_are_matched_with_or_without_punctuation(self):
        for name in ("Roblox Rejoiner.exe", "Roblox.Rejoiner.exe",
                     "ROBLOX REJOINER.EXE", "robloxrejoiner.exe"):
            with self.subTest(name=name):
                self.assertTrue(updater._asset_name_ok(name))
        for name in ("Roblox.Rejoiner.zip", "Setup.exe", ""):
            with self.subTest(name=name):
                self.assertFalse(updater._asset_name_ok(name))


class SwapTests(unittest.TestCase):
    """apply_update renames the running exe (Windows cannot overwrite it)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.target = self.folder / "Roblox Rejoiner.exe"
        self.downloaded = self.folder / "Roblox Rejoiner.exe.update"
        self.target.write_bytes(b"old build")
        self.downloaded.write_bytes(b"new build")

    def tearDown(self):
        self._tmp.cleanup()

    def test_swap_keeps_a_backup_and_installs_the_new_file(self):
        self.assertTrue(updater.apply_update(str(self.downloaded),
                                             str(self.target)))
        self.assertEqual(self.target.read_bytes(), b"new build")
        backups = updater.stale_files(str(self.target))
        self.assertEqual(len(backups), 1)
        self.assertIn(f".old{os.getpid()}", backups[0])
        self.assertEqual(Path(backups[0]).read_bytes(), b"old build")

    def test_failed_swap_rolls_back(self):
        with mock.patch.object(updater.os, "replace",
                               side_effect=[None, OSError("locked")]):
            self.assertFalse(updater.apply_update(str(self.downloaded),
                                                  str(self.target)))
        self.assertEqual(self.target.read_bytes(), b"old build")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.target = self.folder / "Roblox Rejoiner.exe"
        self.target.write_bytes(b"current")

    def tearDown(self):
        self._tmp.cleanup()

    def _leftover(self, suffix=".old1234"):
        path = self.folder / f"Roblox Rejoiner.exe{suffix}"
        path.write_bytes(b"leftover")
        return path

    def test_leftovers_are_listed_and_removed(self):
        old = self._leftover()
        part = self._leftover(".update")
        keep = self.folder / "unrelated.txt"
        keep.write_bytes(b"keep me")
        found = updater.stale_files(str(self.target))
        self.assertCountEqual(found, [str(old), str(part)])
        self.assertTrue(updater.cleanup_stale(str(self.target)))
        self.assertFalse(old.exists())
        self.assertFalse(part.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(updater.stale_files(str(self.target)), [])

    def test_a_locked_leftover_reports_false_so_the_caller_retries(self):
        old = self._leftover()
        real_remove = os.remove

        def locked(path, *a, **k):
            if str(path) == str(old):
                raise PermissionError("in use")
            return real_remove(path, *a, **k)

        with mock.patch.object(updater.os, "remove", locked):
            self.assertFalse(updater.cleanup_stale(str(self.target)))
        self.assertTrue(old.exists())
        # Once the other process exits, the retry succeeds.
        self.assertTrue(updater.cleanup_stale(str(self.target)))
        self.assertFalse(old.exists())

    def test_nothing_to_do_is_success(self):
        self.assertTrue(updater.cleanup_stale(str(self.target)))
        self.assertTrue(updater.cleanup_stale(""))


class MainWindowRetryTests(unittest.TestCase):
    """The app keeps retrying for a while instead of only at startup."""

    def test_cleanup_is_retried_until_it_succeeds(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        import roblox_rejoiner as rr

        app = QApplication.instance() or QApplication(sys.argv)
        win = rr.MainWindow.__new__(rr.MainWindow)
        win.monitor_page = mock.MagicMock()

        calls = {"n": 0}

        def cleanup(_target):
            calls["n"] += 1
            return calls["n"] >= 3          # locked twice, then free

        with mock.patch.object(rr.updater, "cleanup_stale", cleanup), \
                mock.patch.object(rr.QTimer, "singleShot",
                                  lambda _ms, fn: fn()):
            win._clean_update_leftovers("C:/x/Roblox Rejoiner.exe")
        self.assertEqual(calls["n"], 3)

    def test_it_stops_retrying_and_says_so(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        import roblox_rejoiner as rr

        app = QApplication.instance() or QApplication(sys.argv)
        win = rr.MainWindow.__new__(rr.MainWindow)
        win.monitor_page = mock.MagicMock()
        calls = {"n": 0}

        def cleanup(_target):
            calls["n"] += 1
            return False

        with mock.patch.object(rr.updater, "cleanup_stale", cleanup), \
                mock.patch.object(rr.QTimer, "singleShot",
                                  lambda _ms, fn: fn()):
            win._clean_update_leftovers("C:/x/Roblox Rejoiner.exe")
        self.assertEqual(calls["n"], 7)     # the initial attempt + 6 retries
        self.assertTrue(win.monitor_page.on_log.called)


if __name__ == "__main__":
    unittest.main()
