"""Layout checks for the pages: dropdown, Settings page, Start feedback.

    python tests/layout_checks.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer                      # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

import roblox_rejoiner as rr                            # noqa: E402
import account_storage as storage                       # noqa: E402
import roblox_api as api                                # noqa: E402


def main():
    app = QApplication(sys.argv)
    for name in ("get_account_info", "universe_id_for_place",
                 "search_roblox_games", "fetch_game_icons",
                 "resolve_share_api", "probe_join_server",
                 "resolve_join_link", "presence_detail"):
        if hasattr(api, name):
            setattr(api, name, lambda *a, **k: None)

    checks = []

    def check(label, cond):
        checks.append((label, bool(cond)))

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.json"
        with mock.patch.object(rr, "CONFIG_PATH", cfg), \
                mock.patch.object(storage, "CONFIG_PATH", cfg), \
                mock.patch.object(rr, "IS_WINDOWS", False), \
                mock.patch.object(rr, "save_config", lambda c: None):
            win = rr.MainWindow("Segoe UI")
            win.resize(1100, 720)
            win.show()

            labels = [win.nav._selector.itemText(i)
                      for i in range(win.nav._selector.count())]
            check("nav has a Settings page", "Settings" in labels)
            check("nav no longer says Roblox Settings",
                  "Roblox Settings" not in labels)
            check("nav order unchanged",
                  labels == ["Rejoiner Setup", "Manage Accounts",
                             "Account Monitor", "Settings"])

            check("Setup join control is one row",
                  win.setup_page.join_dropdown.sizeHint().height() < 90)

            settings = win.settings_page
            check("Settings page has the webhook field",
                  hasattr(settings, "webhook_edit"))
            check("Settings page has the webhook test button",
                  hasattr(settings, "webhook_test_button"))
            check("Settings page has the update controls",
                  hasattr(settings, "update_check_btn")
                  and hasattr(settings, "update_restart_btn"))
            check("Settings page still has the Roblox install path",
                  hasattr(settings, "path_edit"))
            check("Setup page no longer has the webhook field",
                  not hasattr(win.setup_page, "webhook_edit"))
            check("Setup page no longer has update controls",
                  not hasattr(win.setup_page, "update_check_btn"))

            win.accounts = [
                {"user_id": 1, "username": "alice", "cookie": "c",
                 "alias": "A", "place": "", "note": "", "delay": "",
                 "join_on": True},
                {"user_id": 2, "username": "bob", "cookie": "c", "alias": "",
                 "place": "", "note": "", "delay": "", "join_on": False},
            ]
            win.select_all = True
            win.accounts_page.rebuild()
            dropdown = win.setup_page.join_dropdown
            dropdown.refresh()
            panel = dropdown.popup()
            dropdown.toggle_popup()
            check("dropdown popup opens", panel.isVisible())
            switches = panel.findChildren(rr.IOSSwitch)
            check("popup lists every account", len(switches) == 2)
            switches[1].toggled.emit(True)
            check("toggling in the popup updates the account",
                  win.accounts[1]["join_on"] is True)
            panel.close()
            check("popup closes", not panel.isVisible())
            check("summary follows the choice",
                  "2 of 2" in dropdown.summary_lbl.text())

            # Start must say why it cannot launch, not sit there silently.
            win.selected_ids = set()
            win.select_all = False
            win.toggle_monitoring()
            for _ in range(5):
                app.processEvents()
            log_text = win.monitor_page.log.toPlainText()
            check("Start explains why nothing launched",
                  "Nothing to launch" in log_text)
            check("no engine left running", win.engine is None)
            win.close()

    QTimer.singleShot(50, app.quit)
    app.exec()

    for label, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + label)
    bad = [label for label, ok in checks if not ok]
    print()
    if bad:
        print(f"{len(bad)} check(s) failed: {bad}")
        return 1
    print(f"All {len(checks)} layout checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
