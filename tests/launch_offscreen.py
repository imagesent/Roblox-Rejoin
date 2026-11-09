"""Launch the real app window (offscreen) and let it tick a few seconds.

This exercises the app's own startup path - timers, fetchers, bridge polling -
against a fully stubbed Roblox, which is the closest an offline run gets to
opening it on the desktop.
"""
import os
import sys
from pathlib import Path
from unittest import mock

SRC = Path(r"C:\Users\yuzi\Downloads\Roblox-Rejoin-1.2.0\Roblox-Rejoin-1.2.0\src")
sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication      # noqa: E402
from PySide6.QtCore import QTimer                # noqa: E402

import roblox_rejoiner as rr                     # noqa: E402
import account_storage as storage                # noqa: E402
import roblox_api as api                         # noqa: E402
import roblox_account_profiles as profiles       # noqa: E402
import updater                                   # noqa: E402
import account_command_bridge as bridge          # noqa: E402
import roblox_windows as windows                 # noqa: E402
import discord_notifications as dn               # noqa: E402


def stub_everything():
    """Nothing leaves the machine."""
    for name in dir(api):
        obj = getattr(api, name)
        if callable(obj) and not name.startswith("_"):
            setattr(api, name, lambda *a, **k: None)
    for name in dir(profiles):
        obj = getattr(profiles, name)
        if callable(obj) and not name.startswith("_") and not hasattr(obj, "fetch"):
            setattr(profiles, name, lambda *a, **k: None)
    for name in dir(windows):
        obj = getattr(windows, name)
        if callable(obj) and not name.startswith("_"):
            setattr(windows, name, lambda *a, **k: set())
    for name in dir(dn):
        obj = getattr(dn, name)
        if callable(obj) and not name.startswith("_"):
            setattr(dn, name, lambda *a, **k: None)
    for name in dir(updater):
        obj = getattr(updater, name)
        if callable(obj) and not name.startswith("_"):
            setattr(updater, name, lambda *a, **k: None)


def main():
    app = QApplication(sys.argv)
    stub_everything()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.json"
        with mock.patch.object(rr, "CONFIG_PATH", cfg), \
                mock.patch.object(storage, "CONFIG_PATH", cfg), \
                mock.patch.object(rr, "IS_WINDOWS", False):
            win = rr.MainWindow("Segoe UI")
            win.show()

            # Give the timers a few seconds to fire against the stubs.
            QTimer.singleShot(4000, app.quit)
            app.exec()

            ok = win.setup_page.join_status.text() != ""
            print("join status line:", repr(win.setup_page.join_status.text()))
            print("accounts page rows:", len(win.accounts_page._rows))
            print("monitor page built:", win.monitor_page.log is not None)
            print("start button:", win.monitor_page.start_btn.text())
            win.close()
    print("APP START OK" if ok else "APP START FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
