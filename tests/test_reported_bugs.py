"""Regression tests for the three reported bugs.

    python -m unittest tests.test_reported_bugs -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

import roblox_rejoiner as rr                            # noqa: E402
import account_storage as storage                       # noqa: E402
import roblox_api as api                                # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)


def stub_roblox(tmp_path=None):
    for name in ("get_account_info", "universe_id_for_place",
                 "search_roblox_games", "fetch_game_icons",
                 "resolve_share_api", "probe_join_server",
                 "resolve_join_link", "presence_detail"):
        if hasattr(api, name):
            setattr(api, name, lambda *a, **k: None)


SERVER_ROWS = [
    {"id": "1", "place_id": "1111111111", "game": "Anime Adventure",
     "name": "Anime Adventure Server", "active": True,
     "link": "https://www.roblox.com/games/1111111111?privateServerLinkCode=aaa"},
    {"id": "2", "place_id": "2222222222", "game": "Driving Empire",
     "name": "Driving Empire Server", "active": True,
     "link": "https://www.roblox.com/games/2222222222?privateServerLinkCode=bbb"},
    {"id": "3", "place_id": "3333333333", "game": "Dungeon Quest",
     "name": "Dungeon Quest Server", "active": False, "link": ""},
]


class PickerTests(unittest.TestCase):
    """Bug 1: the game search must narrow the server list to that game."""

    def setUp(self):
        stub_roblox()
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._patches = [
            mock.patch.object(rr, "CONFIG_PATH", cfg),
            mock.patch.object(storage, "CONFIG_PATH", cfg),
            mock.patch.object(rr, "IS_WINDOWS", False),
            mock.patch.object(rr, "save_config", lambda c: None),
        ]
        for p in self._patches:
            p.start()
        self.win = rr.MainWindow("Segoe UI")
        self.win.accounts = [{"user_id": 1, "username": "a", "cookie": "c",
                              "alias": "", "place": "", "note": "", "delay": "",
                              "join_on": True}]
        self.win.select_all = True
        self.win.accounts_page.rebuild()

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_search_result_renders_without_error(self):
        """The search rows used to raise NameError on a literal '{D}'."""
        dialog = rr.PrivateServerDialog(self.win)
        dialog._on_search_done({"search": True, "results": [], "error": ""})
        # A real result must produce a row with the creator in it.
        dialog._on_search_done({
            "search": True,
            "results": [{"name": "Anime Adventure", "creator": "Innovative",
                         "universe_id": "1", "root_place_id": "1111111111",
                         "icon_data": b""}],
            "error": ""})
        self.assertEqual(dialog.games.count(), 1)
        self.assertIn("Anime Adventure", dialog.games.item(0).text())
        self.assertIn("Innovative", dialog.games.item(0).text())

    def test_picking_a_game_filters_the_list(self):
        dialog = rr.PrivateServerDialog(self.win)
        dialog._on_loaded({"rows": SERVER_ROWS, "error": ""})
        self.assertEqual(len(dialog._rows), 3)      # no game chosen yet

        dialog._place_id = "1111111111"
        dialog._rows = dialog._filtered()
        dialog._render()
        self.assertEqual(len(dialog._rows), 1)
        self.assertEqual(dialog._rows[0]["game"], "Anime Adventure")
        self.assertIn("1 private server", dialog.status.text())

    def test_no_servers_in_the_picked_game_says_so(self):
        dialog = rr.PrivateServerDialog(self.win)
        dialog._on_loaded({"rows": SERVER_ROWS, "error": ""})
        dialog._place_id = "9999999999"
        dialog._rows = dialog._filtered()
        dialog._render()
        self.assertEqual(dialog._rows, [])
        self.assertIn("no private server in the game you picked",
                      dialog.status.text())
        self.assertFalse(dialog.link_btn.isEnabled())

    def test_all_games_still_lists_everything(self):
        dialog = rr.PrivateServerDialog(self.win)
        dialog._place_id = "1111111111"
        dialog._on_loaded({"rows": SERVER_ROWS, "error": ""})
        self.assertEqual(len(dialog._rows), 1)
        dialog.use_all_games()
        self.assertEqual(dialog._place_id, "")
        dialog._on_loaded({"rows": SERVER_ROWS, "error": ""})
        self.assertEqual(len(dialog._rows), 3)


class DropdownTests(unittest.TestCase):
    """Bug 2: the dropdown opens as a menu, not a giant detached panel."""

    def setUp(self):
        stub_roblox()
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._patches = [
            mock.patch.object(rr, "CONFIG_PATH", cfg),
            mock.patch.object(storage, "CONFIG_PATH", cfg),
            mock.patch.object(rr, "IS_WINDOWS", False),
            mock.patch.object(rr, "save_config", lambda c: None),
        ]
        for p in self._patches:
            p.start()
        self.win = rr.MainWindow("Segoe UI")
        self.win.accounts = [
            {"user_id": 1, "username": "alice", "cookie": "c", "alias": "A",
             "place": "", "note": "", "delay": "", "join_on": True},
            {"user_id": 2, "username": "bob", "cookie": "c", "alias": "",
             "place": "", "note": "", "delay": "", "join_on": False},
        ]
        self.win.select_all = True
        self.win.accounts_page.rebuild()

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_popup_is_sized_as_a_menu(self):
        dropdown = self.win.setup_page.join_dropdown
        dropdown.refresh()
        dropdown.toggle_popup()
        panel = dropdown.popup()
        # Width matches the control, and the height fits its rows only.
        self.assertLessEqual(panel.width(), self.win.width())
        self.assertGreater(panel.width(), 300)
        self.assertLess(panel.height(), 260)
        self.assertTrue(panel.isVisible())
        panel.close()
        self.assertFalse(panel.isVisible())

    def test_popup_refreshes_when_opened(self):
        dropdown = self.win.setup_page.join_dropdown
        self.win.accounts.append(
            {"user_id": 3, "username": "carol", "cookie": "c", "alias": "",
             "place": "", "note": "", "delay": "", "join_on": True})
        dropdown.toggle_popup()
        switches = dropdown.popup().findChildren(rr.IOSSwitch)
        dropdown.popup().close()
        self.assertEqual(len(switches), 3)

    def test_clicking_anywhere_on_a_row_toggles_it(self):
        """The whole line is a hit target, not just the little switch."""
        dropdown = self.win.setup_page.join_dropdown
        dropdown.toggle_popup()
        panel = dropdown.popup()
        rows = panel.findChildren(rr.ClickableRow)
        self.assertEqual(len(rows), 2)
        # bob is the second account and starts off.
        before = self.win.accounts[1]["join_on"]
        rows[1].clicked.emit()
        self.assertNotEqual(self.win.accounts[1]["join_on"], before)
        panel.close()

    def test_monitored_accounts_are_listed_first(self):
        dropdown = self.win.setup_page.join_dropdown
        self.win.select_all = False
        self.win.selected_ids = {2}          # only bob is monitored
        dropdown.refresh()
        panel = dropdown.popup()
        rows = panel.findChildren(rr.ClickableRow)
        labels = [w.text() for r in rows for w in r.findChildren(rr.QLabel)
                  if w.objectName() == "RowTitle"]
        self.assertEqual(labels[0], "bob")
        panel.close()

    def test_unmonitored_account_is_labelled(self):
        dropdown = self.win.setup_page.join_dropdown
        self.win.select_all = False
        self.win.selected_ids = {1}
        dropdown.refresh()
        panel = dropdown.popup()
        subs = [w.text() for w in panel.findChildren(rr.QLabel)
                if w.objectName() == "RowSub"]
        self.assertTrue(any("not monitored" in s for s in subs), subs)
        panel.close()

    def test_popup_has_the_menu_style(self):
        dropdown = self.win.setup_page.join_dropdown
        self.assertEqual(dropdown.popup().objectName(), "DropdownPopup")


class SettingsPageTests(unittest.TestCase):
    """Bug 3: the App updates card must appear once, on the Settings page."""

    def setUp(self):
        stub_roblox()
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._patches = [
            mock.patch.object(rr, "CONFIG_PATH", cfg),
            mock.patch.object(storage, "CONFIG_PATH", cfg),
            mock.patch.object(rr, "IS_WINDOWS", False),
            mock.patch.object(rr, "save_config", lambda c: None),
        ]
        for p in self._patches:
            p.start()
        self.win = rr.MainWindow("Segoe UI")

    def tearDown(self):
        self.win.close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _sections(self, page):
        return [w.text() for w in page.findChildren(type(self.win.setup_page.join_dropdown.summary_lbl))
                if w.objectName() == "Section"]

    def test_settings_page_has_each_section_once(self):
        from PySide6.QtWidgets import QLabel
        labels = [w.text() for w in self.win.settings_page.findChildren(QLabel)]
        self.assertEqual(labels.count("APP UPDATES"), 1)
        self.assertEqual(labels.count("NOTIFICATIONS"), 1)
        self.assertEqual(labels.count("ROBLOX INSTALLATION"), 1)
        self.assertIn("Standalone app updates", labels)
        self.assertIn("Discord webhook", labels)

    def test_setup_page_has_no_notifications_or_updates(self):
        from PySide6.QtWidgets import QLabel
        labels = [w.text() for w in self.win.setup_page.findChildren(QLabel)]
        self.assertNotIn("APP UPDATES", labels)
        self.assertNotIn("NOTIFICATIONS", labels)
        self.assertNotIn("Discord webhook", labels)
        self.assertNotIn("Standalone app updates", labels)

    def test_update_status_still_reports(self):
        page = self.win.settings_page
        page.set_update_status({"state": "uptodate", "tag": "v1.2.0"})
        self.assertIn("up to date", page.update_status.text())
        # The page itself is not the visible one, so ask Qt about it relative
        # to its parent rather than about the top-level window.
        self.assertTrue(page.update_restart_btn.isHidden())
        page.set_update_status({"state": "ready", "tag": "v1.3.0"})
        self.assertTrue(page.update_restart_btn.isVisibleTo(page))
        self.assertIn("restart to apply", page.update_status.text())
        page.set_update_status({"state": "source"})
        self.assertTrue(page.update_restart_btn.isHidden())


import unittest                                           # noqa: E402

if __name__ == "__main__":
    unittest.main()
