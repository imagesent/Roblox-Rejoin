"""Offline smoke test: build the whole UI with a stubbed Roblox API.

No network, no Roblox. Confirms the app constructs, the new join-server
controls wire up, and the resolution helpers behave for every link shape.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

SRC = Path(r"C:\Users\yuzi\Downloads\Roblox-Rejoin-1.2.0\Roblox-Rejoin-1.2.0\src")
sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtCore import QTimer                    # noqa: E402
from PySide6.QtWidgets import QSizePolicy             # noqa: E402

import roblox_rejoiner as rr                         # noqa: E402
import account_storage as storage                    # noqa: E402
import roblox_api as api                             # noqa: E402


def stub_no_network():
    """Every outbound Roblox call fails, so nothing can hit the network."""
    for name in ("get_account_info", "get_csrf", "get_auth_ticket",
                 "universe_id_for_place", "search_roblox_games",
                 "fetch_game_icons", "presence_detail", "fetch_presence",
                 "presence_state", "resolve_share_api", "fetch_public_servers",
                 "detect_last_place", "latest_client_error",
                 "fetch_private_servers", "generate_private_server_link",
                 "probe_join_server", "resolve_join_link"):
        if hasattr(api, name):
            setattr(api, name, lambda *a, **k: None)


def main():
    app = QApplication(sys.argv)
    stub_no_network()

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.json"
        # Isolate both modules that read the config path, so the real
        # settings on this machine never leak into the test.
        with mock.patch.object(rr, "CONFIG_PATH", cfg_path), \
                mock.patch.object(storage, "CONFIG_PATH", cfg_path), \
                mock.patch.object(rr, "IS_WINDOWS", False):
            win = rr.MainWindow("Segoe UI")

            checks = []
            checks.append(("window created", win is not None))
            checks.append(("join link control", win.setup_page.join_edit is not None))
            checks.append(("join account dropdown exists",
                           win.setup_page.join_dropdown is not None))
            checks.append(("join switch exists",
                           win.setup_page.sw_join is not None))

            # Accounts: one ticked for the private server, the rest have
            # their own games to fall back to.
            win.cfg["place"] = "555"
            win.accounts = [
                {"user_id": 1, "username": "alice", "cookie": "c1",
                 "alias": "Alice", "place": "", "note": "", "delay": "",
                 "join_on": True},
                {"user_id": 2, "username": "bob", "cookie": "c2",
                 "alias": "", "place": "777", "note": "", "delay": "",
                 "join_on": True},
                {"user_id": 3, "username": "carol", "cookie": "c3",
                 "alias": "", "place": "", "note": "", "delay": "",
                 "join_on": True},
            ]
            win.select_all = True
            win.accounts_page.rebuild()

            # Turning the feature on and naming the accounts must not raise.
            win.setup_page.sw_join.setChecked(True, animate=False)
            win.set_flag("joinserver_on", "join_server", True)
            win.set_setting(
                "joinserver_url",
                "https://www.roblox.com/games/123/x?privateServerLinkCode=abc")
            win.setup_page.refresh_join_status()
            checks.append(("join status text",
                           "private server" in win.setup_page.join_status.text()
                           and "own game" in win.setup_page.join_status.text()))

            # Only the ticked accounts are sent to the private server; the
            # rest fall through to the game they were set up with.
            win.accounts[1]["join_on"] = False
            win.accounts[2]["join_on"] = False
            win.accounts_page.rebuild()
            win.setup_page.refresh_join_accounts()
            dropdown = win.setup_page.join_dropdown
            checks.append(("dropdown summary counts accounts",
                           "1 of 3" in dropdown.summary_lbl.text()))

            with mock.patch.object(rr, "save_config", lambda cfg: None), \
                    mock.patch.object(rr, "dpapi_encrypt", lambda t: "x"):
                resolved = win._resolve()
            by_user = {r["username"]: r["rjoin"] for r in resolved}
            checks.append(("ticked account uses the private server",
                           by_user.get("alice", "").startswith(
                               "https://www.roblox.com/games")))
            checks.append(("unticked accounts use their own game",
                           by_user.get("bob", "") == ""
                           and by_user.get("carol", "") == ""))
            checked = {r["username"]: r for r in resolved}
            checks.append(("unticked accounts keep their own place",
                           checked["bob"]["rplace"] == "777"
                           and checked["carol"]["rplace"] == "555"))

            # Ticking everyone sends them all.
            win.bridge_set_join_accounts(["alice", "bob", "carol"], True)
            checks.append(("bridge ticks every account",
                           all(a["join_on"] for a in win.accounts)))
            with mock.patch.object(rr, "save_config", lambda cfg: None), \
                    mock.patch.object(rr, "dpapi_encrypt", lambda t: "x"):
                resolved_all = win._resolve()
            checks.append(("every ticked account gets the link",
                           all(r["rjoin"] for r in resolved_all)))
            win.bridge_set_join_accounts(["alice", "bob", "carol"], False)

            # Engine construction with resolved accounts.
            eng = rr.MonitorEngine(resolved_all, dict(win.flags),
                                   join_url=win.cfg.get("joinserver_url", ""))
            checks.append(("engine accepts per-account links",
                           eng.flags.get("join_server") is True))

            # Account-row subtext reflects the per-account choice.
            row2 = win.accounts_page._rows.get(2)
            checks.append(("row shows its own-game choice",
                           "own game only" in row2.sub_lbl.text()))
            checks.append(("account row has no link field",
                           not hasattr(row2, "join_url")))

            # A per-account switch flip must persist through the window.
            win.accounts_page._rows.get(2).sw_join.toggled.emit(False)
            checks.append(("account switch saved",
                           win.accounts[1]["join_on"] is False))

            # The dropdown popup holds the per-account switches, so the page
            # itself stays a single row.
            panel = dropdown.popup()
            checks.append(("dropdown has a popup panel",
                           panel is not None and not panel.isVisible()))
            panel.show()
            switches = panel.findChildren(rr.IOSSwitch)
            checks.append(("popup lists every account",
                           len(switches) == 3))
            panel.close()
            checks.append(("no account row list on the page itself",
                           not hasattr(win.setup_page, "join_accounts_host")))

            # The private-server picker searches games by name.
            dialog = rr.PrivateServerDialog(win)
            checks.append(("picker has a game search",
                           dialog.search_edit is not None
                           and dialog.search_btn.text() == "Search"))
            checks.append(("picker has no place-id box",
                           not hasattr(dialog, "place_edit")))
            # sizeHint is layout-independent, so it is honest before the
            # window is ever shown.
            checks.append(("dropdown is one row on the page",
                           win.setup_page.join_dropdown.sizeHint().height()
                           < 90
                           and win.setup_page.join_dropdown.sizePolicy()
                           .verticalPolicy() == QSizePolicy.Fixed))
            checks.append(("picker lists all servers",
                           dialog.all_games_btn is not None))
            win._picker = dialog

            # Bot bridge: every join command must be handled.
            good_link = ("https://www.roblox.com/share?code=abc&type=Server")
            cmds = [
                {"action": "join_server", "value": "all"},
                {"action": "join_server", "value": "all"},
                {"action": "join_server", "value": "none"},
                {"action": "join_server", "value": "off"},
                {"action": "join_server", "value": good_link},
                {"action": "account_join", "user": "bob", "value": "on"},
                {"action": "account_join", "user": "carol", "value": "off"},
                {"action": "account_join", "value": "alice,bob"},
            ]
            for cmd in cmds:
                ok, msg = rr.qt_bridge.apply_command(win, cmd)
                checks.append((f"bridge {cmd['action']}"
                               f"[{str(cmd.get('value', cmd.get('user')))[:24]}]",
                               ok and bool(msg)))
            bad, _msg = rr.qt_bridge.apply_command(
                win, {"action": "join_server", "value": "not a link"})
            checks.append(("bridge rejects a bad link", not bad))

        # Shut the window down cleanly (timers, threads).
        QTimer.singleShot(50, app.quit)
        app.exec()

    bad = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + name)
    print()
    if bad:
        print(f"{len(bad)} check(s) failed: {bad}")
        return 1
    print(f"All {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
