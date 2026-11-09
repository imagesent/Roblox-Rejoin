"""Roblox Rejoiner desktop interface and application window.

Pages in this module edit shared settings and control the multi-account engine.
Run this file to launch the app from source.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QHBoxLayout, QScrollArea,
                               QPlainTextEdit, QSizePolicy, QDialog, QComboBox,
                               QListWidget, QListWidgetItem)

from rejoiner_constants import (IS_WINDOWS, APP_VERSION, UPDATE_CHECK_SECS)
from rejoiner_constants import CONFIG_PATH
from rejoiner_colors import (fmt_dur, ACCENT, ACCENT_SOFT, GOOD, WARN, BAD, MUTED, BG,
                      TEXT, CARD2)
from account_storage import load_config, save_config, dpapi_encrypt, dpapi_decrypt
from roblox_account_profiles import ProfileFetcher, PresenceFetcher
from roblox_api import (get_account_info, parse_place_id, parse_join_link,
                       resolve_join_link, JoinTarget, search_roblox_games,
                       fetch_game_icons, GameSearchResult,
                       fetch_private_servers, generate_private_server_link)
from discord_notifications import (_is_discord_webhook, discord_post,
                                   make_embed, COLOR_GOOD)

import account_command_bridge as qt_bridge
import updater

from rejoiner_theme import pick_fonts, build_stylesheet, THEMES, set_theme as set_palette, BG
from rejoiner_widgets import (IOSSwitch, TopNavBar, AnimatedStack, Avatar,
                         StatTile, StatusDot, make_card, hsep,
                         DropdownCard, ClickableRow)
from multi_account_monitor_engine import MonitorEngine


# Shared layout helpers for settings rows and install-path detection.
def _row(title: str, sub: str, control: QWidget) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(18, 12, 18, 12)
    h.setSpacing(16)
    col = QVBoxLayout()
    col.setSpacing(3)
    t = QLabel(title)
    t.setObjectName("RowTitle")
    col.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setObjectName("RowSub")
        col.addWidget(s)
    h.addLayout(col)
    h.addStretch(1)
    h.addWidget(control, 0, Qt.AlignVCenter)
    return w


def scan_roblox_install() -> str:
    """Best-effort: folder holding the newest RobloxPlayerBeta.exe, or ''.

    Checks the standard per-user and Program Files install roots and returns the
    most recently modified version folder. Used only to *suggest* a path — the
    user can override it.
    """
    import glob
    roots = []
    for env in ("LOCALAPPDATA", "PROGRAMFILES(X86)", "PROGRAMFILES",
                "PROGRAMW6432"):
        base = os.environ.get(env)
        if base:
            roots.append(os.path.join(base, "Roblox", "Versions"))
    found = []
    for root in roots:
        try:
            for exe in glob.glob(os.path.join(root, "*", "RobloxPlayerBeta.exe")):
                try:
                    found.append((os.path.getmtime(exe), os.path.dirname(exe)))
                except OSError:
                    pass
        except Exception:
            pass
    if not found:
        return ""
    found.sort(reverse=True)
    return found[0][1]


def _fmt_hms(secs) -> str:
    secs = int(secs or 0)
    h, m = secs // 3600, (secs % 3600) // 60
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{secs}s"


class CookieHelpDialog(QDialog):
    """A friendly, safety-first guide to obtaining the .ROBLOSECURITY cookie."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("How to get your .ROBLOSECURITY cookie")
        self.setMinimumSize(540, 560)
        self.setStyleSheet(
            f"QDialog {{ background:{BG}; }} QLabel {{ color:{TEXT}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 20)
        lay.setSpacing(14)

        title = QLabel("Add an account with its cookie")
        title.setObjectName("H1")
        lay.addWidget(title)

        html = (
            "<p style='font-size:13px;color:%(m)s;'>Your cookie is like a "
            "password for one account. Roblox Rejoiner stores it <b>encrypted on this "
            "PC only</b> and never sends it anywhere except Roblox's own servers "
            "to launch your game.</p>"
            "<p style='font-size:14px;color:%(t)s;'><b>Get the cookie "
            "(Chrome / Edge)</b></p>"
            "<ol style='font-size:13px;color:%(t)s;line-height:152%%;'>"
            "<li>Open <b>roblox.com</b> and log in to the account you want.</li>"
            "<li>Press <b>F12</b> to open Developer Tools.</li>"
            "<li>Open the <b>Application</b> tab (in Firefox: <b>Storage</b>).</li>"
            "<li>In the left panel expand <b>Cookies</b> &rarr; click "
            "<b>https://www.roblox.com</b>.</li>"
            "<li>Click the cookie named <b>.ROBLOSECURITY</b>.</li>"
            "<li>Double-click its <b>Value</b>, select all (Ctrl+A), copy "
            "(Ctrl+C).</li>"
            "<li>Paste it into the <b>Add account</b> box and press "
            "<b>Add</b>.</li></ol>"
            "<p style='font-size:14px;color:%(t)s;'><b>Stay safe</b></p>"
            "<ul style='font-size:13px;color:%(t)s;line-height:152%%;'>"
            "<li><b>Never</b> share this value or paste it on any website — "
            "anyone with it can take over your account.</li>"
            "<li>The value begins with "
            "<span style='color:%(m)s;'>_|WARNING:-DO-NOT-SHARE-THIS…</span> — "
            "that warning is normal, copy the whole thing.</li>"
            "<li>Only add accounts you own; it stays on your machine "
            "(DPAPI-encrypted).</li>"
            "<li>If it ever leaked, use <b>Log out of all sessions</b> on "
            "roblox.com to reset it.</li></ul>"
        ) % {"t": TEXT, "m": MUTED}
        body = QLabel(html)
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setAlignment(Qt.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("Got it")
        ok.setObjectName("Primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)


class AddRobloxAccountDialog(QDialog):
    """Collect user-approved account metadata and a manually supplied cookie."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Roblox account")
        self.setMinimumWidth(480)
        self.setMinimumHeight(370)
        self.setStyleSheet(f"QDialog {{ background:{BG}; }}")
        self.cookie = ""
        self.alias = ""
        self.note = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        title = QLabel("Add Roblox account")
        title.setObjectName("H2")
        subtitle = QLabel("Add an account using its Roblox session cookie.")
        subtitle.setObjectName("Muted")
        root.addWidget(title)
        root.addWidget(subtitle)

        cookie_card = make_card()
        cookie_layout = QVBoxLayout(cookie_card)
        cookie_layout.setContentsMargins(14, 14, 14, 14)
        cookie_layout.setSpacing(10)
        cookie_help = QLabel(
            "Paste the .ROBLOSECURITY value for the account you want to add. "
            "It is a sensitive session credential; never share it."
        )
        cookie_help.setObjectName("Muted")
        cookie_help.setWordWrap(True)
        cookie_layout.addWidget(cookie_help)
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setPlaceholderText("Paste .ROBLOSECURITY session cookie")
        self.cookie_edit.setEchoMode(QLineEdit.Password)
        cookie_layout.addWidget(self.cookie_edit)
        cookie_actions = QHBoxLayout()
        paste_btn = QPushButton("Paste from clipboard")
        paste_btn.setObjectName("Ghost")
        paste_btn.clicked.connect(self._paste_cookie)
        help_btn = QPushButton("How to find it")
        help_btn.setObjectName("Link")
        help_btn.clicked.connect(lambda: CookieHelpDialog(self).exec())
        cookie_actions.addWidget(paste_btn)
        cookie_actions.addWidget(help_btn)
        cookie_actions.addStretch(1)
        cookie_layout.addLayout(cookie_actions)
        root.addWidget(cookie_card)

        metadata = QHBoxLayout()
        self.alias_edit = QLineEdit()
        self.alias_edit.setPlaceholderText("Alias (optional)")
        metadata.addWidget(self.alias_edit)
        root.addLayout(metadata)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Note (optional)")
        root.addWidget(self.note_edit)
        self.error = QLabel("")
        self.error.setObjectName("RowSub")
        self.error.setStyleSheet(f"color:{BAD};")
        root.addWidget(self.error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        self.add_btn = QPushButton("Add account")
        self.add_btn.setObjectName("Primary")
        self.add_btn.clicked.connect(self._accept_account)
        buttons.addWidget(cancel)
        buttons.addWidget(self.add_btn)
        root.addLayout(buttons)

    def _paste_cookie(self):
        cookie = QApplication.clipboard().text().strip()
        if not cookie:
            self.error.setText("Clipboard is empty. Copy the session cookie first.")
            return
        self.cookie_edit.setText(cookie)
        self.cookie_edit.setFocus()
        self.error.clear()

    def _accept_account(self):
        cookie = self.cookie_edit.text().strip()
        if not cookie:
            self.error.setText("Paste the account's session cookie to continue.")
            return
        self.cookie = cookie
        self.alias = self.alias_edit.text().strip()
        self.note = self.note_edit.text().strip()
        self.accept()


class JoinAccountsDropdown(DropdownCard):
    """Compact control for choosing which accounts join the private server.

    The face of the control is one row ("Send these accounts to the private
    server - 1 of 3 selected"); the list itself opens as a popup, so the Setup
    page stays short no matter how many accounts are saved. The same switches
    live on each account's own row, and both write to one setting."""

    def __init__(self, win: "MainWindow", parent=None):
        super().__init__("Send these accounts to the private server",
                         "", parent)
        self.win = win
        self._panel = QFrame(self, Qt.Popup)
        self._panel.setObjectName("DropdownPopup")
        self._rows: dict[int, QLabel] = {}
        self._build_panel()
        # Rebuild from the accounts every time the menu opens, so it can never
        # show stale switches.
        self.opened.connect(self.refresh)

    # -- popup contents ---------------------------------------------------
    def _build_panel(self):
        lay = QVBoxLayout(self._panel)
        lay.setContentsMargins(0, 6, 0, 8)
        lay.setSpacing(0)

        header = QWidget()
        hh = QHBoxLayout(header)
        hh.setContentsMargins(16, 10, 16, 6)
        hh.setSpacing(8)
        cap = QLabel("Accounts sent to the private server")
        cap.setObjectName("RowTitle")
        hh.addWidget(cap)
        hh.addStretch(1)
        for label, on in (("All", True), ("None", False)):
            btn = QPushButton(label)
            btn.setObjectName("Link")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, v=on: self._set_all(v))
            hh.addWidget(btn)
        lay.addWidget(header)
        lay.addWidget(hsep())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self._list_lay = QVBoxLayout(host)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(0)
        scroll.setWidget(host)
        lay.addWidget(scroll)
        self._panel.setMinimumWidth(320)
        self._panel.setMinimumHeight(120)
        self._panel.setMaximumHeight(420)
        self._empty = QLabel("No accounts yet — add one on Manage "
                             "Accounts to choose who joins the private server.")
        self._empty.setObjectName("Muted")
        self._empty.setWordWrap(True)
        self._empty.hide()
        self._list_lay.addWidget(self._empty)

    def popup(self) -> QFrame | None:
        return self._panel

    def popup_height(self) -> int | None:
        """Menu-sized to the account list it is about to show."""
        n = len(self._rows)
        if not n:
            return None
        return min(420, 104 + 48 * n)

    def refresh(self):
        """Rebuild the list and the face of the control from the accounts."""
        self.set_summary(self._summary())
        lay = self._list_lay
        for i in reversed(range(lay.count())):
            w = lay.itemAt(i).widget()
            if w is not None and w is not self._empty:
                w.setParent(None)
        self._rows.clear()
        accounts = [a for a in self.win.accounts if a.get("cookie")]
        if not accounts:
            self._empty.show()
            return
        self._empty.hide()
        # One row per account, capped so a long list still opens as a menu.
        self._panel.setMaximumHeight(min(420, 150 + 46 * len(accounts)))
        # Accounts the app will actually launch come first: an account that is
        # not monitored can never join the private server, so it should not
        # sit above the ones that can.
        accounts.sort(key=lambda a: (not self.win.is_monitored(a["user_id"]),
                                     (a.get("alias") or a.get("username")
                                      or "").lower()))
        for acc in accounts:
            lay.addWidget(self._account_row(acc))
        lay.addStretch(1)
        self._panel.setMinimumHeight(self.popup_height() or 0)

    def _account_row(self, acc: dict) -> QWidget:
        """One account line: click anywhere to toggle, switch inside it too."""
        uid = acc["user_id"]
        monitored = self.win.is_monitored(uid)
        row = ClickableRow()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(16, 8, 16, 8)
        rl.setSpacing(12)
        avatar = Avatar(acc.get("alias") or acc.get("username", "?"), 34)
        avatar.set_presence("online" if monitored else None)
        rl.addWidget(avatar, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(acc.get("alias") or acc.get("username", "account"))
        name.setObjectName("RowTitle")
        col.addWidget(name)
        sub = QLabel(self._where(acc, monitored))
        sub.setObjectName("RowSub")
        col.addWidget(sub)
        rl.addLayout(col)
        rl.addStretch(1)
        switch = IOSSwitch(bool(acc.get("join_on", True)))
        # Clicking anywhere on the line does the same as the switch, so the
        # whole row is one comfortable target.
        row.clicked.connect(
            lambda s=switch: (s.setChecked(not s.isChecked()),
                              s.toggled.emit(s.isChecked())))
        switch.toggled.connect(
            lambda v, u=uid: self.win.set_account_join(u, v))
        rl.addWidget(switch, 0, Qt.AlignVCenter)
        self._rows[uid] = sub
        return row

    @staticmethod
    def _where(acc: dict, monitored: bool = True) -> str:
        if acc.get("join_on", True):
            what = "joins the private server"
        else:
            place = str(acc.get("place") or "").strip()
            what = f"uses game {place}" if place else "uses the default game"
        if not monitored:
            what += " (not monitored, so it stays off)"
        return what

    def _set_all(self, on: bool):
        for acc in self.win.accounts:
            if acc.get("cookie"):
                self.win.set_account_join(acc["user_id"], on)

    def _summary(self) -> str:
        accounts = [a for a in self.win.accounts if a.get("cookie")]
        if not accounts:
            return "No accounts added yet"
        chosen = [a for a in accounts if a.get("join_on", True) is not False]
        unmonitored = [a for a in accounts
                       if not self.win.is_monitored(a["user_id"])]
        base = (f"{len(chosen)} of {len(accounts)} account"
                + ("" if len(accounts) == 1 else "s")
                + " sent to the private server")
        if unmonitored:
            base += (f" \u2014 {len(unmonitored)} not monitored on Manage "
                     f"Accounts")
        return base

class PrivateServerDialog(QDialog):
    """Pick one of the account's own private servers and use its join link.

    Search Roblox by game name (or load every game the account has private
    servers in), then pick the server. Roblox hands back a join link for each
    one, so nobody has to copy links out of a browser by hand. Every request
    runs on a worker thread while the dialog stays responsive."""

    _loaded = Signal(object)

    def __init__(self, win: "MainWindow", parent=None):
        super().__init__(parent or win)
        self.win = win
        self.setWindowTitle("Use one of your private servers")
        self.setMinimumSize(720, 620)
        self.setStyleSheet(f"QDialog {{ background:{BG}; }}")
        self._rows: list[dict] = []
        self._place_id = ""
        self._searching = False
        self._loaded.connect(self._on_loaded)
        self._all_rows: list[dict] = []      # everything, before the filter

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(10)

        title = QLabel("Use one of your private servers")
        title.setObjectName("H2")
        lay.addWidget(title)
        sub = QLabel("Pick the game, then pick the server. The search looks "
                     "up every Roblox experience by name.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        picker = QHBoxLayout()
        picker.setSpacing(8)
        self.account_combo = QComboBox()
        for a in win.accounts:
            if a.get("cookie"):
                self.account_combo.addItem(
                    a.get("alias") or a.get("username") or "account",
                    a["user_id"])
        picker.addWidget(QLabel("Account:"), 0, Qt.AlignVCenter)
        picker.addWidget(self.account_combo, 1)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search games by name")
        self.search_edit.returnPressed.connect(self.search_games)
        picker.addWidget(self.search_edit, 2)
        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("Ghost")
        self.search_btn.setCursor(Qt.PointingHandCursor)
        self.search_btn.clicked.connect(self.search_games)
        picker.addWidget(self.search_btn)
        lay.addLayout(picker)

        any_row = QHBoxLayout()
        any_row.setSpacing(8)
        self.all_games_btn = QPushButton("All my private servers")
        self.all_games_btn.setObjectName("Link")
        self.all_games_btn.setCursor(Qt.PointingHandCursor)
        self.all_games_btn.setToolTip("List every private server this account "
                                      "owns, whatever the game")
        self.all_games_btn.clicked.connect(self.use_all_games)
        any_row.addWidget(self.all_games_btn)
        any_row.addStretch(1)
        self.reload_btn = QPushButton("Load servers")
        self.reload_btn.setObjectName("Primary")
        self.reload_btn.setCursor(Qt.PointingHandCursor)
        self.reload_btn.clicked.connect(self.load)
        any_row.addWidget(self.reload_btn)
        lay.addLayout(any_row)

        self.search_status = QLabel("")
        self.search_status.setObjectName("RowSub")
        self.search_status.setWordWrap(True)
        lay.addWidget(self.search_status)

        self.games = QListWidget()
        self.games.setObjectName("GameResults")
        self.games.setIconSize(QSize(40, 40))
        self.games.setFixedHeight(3 * 56 + 4)
        self.games.setVisible(False)
        self.games.currentItemChanged.connect(self._show_game_details)
        self.games.itemClicked.connect(self._pick_game)
        lay.addWidget(self.games)

        self.status = QLabel("Search for the game, or list every private "
                             "server this account owns.")
        self.status.setObjectName("RowSub")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.list = QListWidget()
        self.list.setObjectName("GameResults")
        self.list.setIconSize(QSize(40, 40))
        self.list.itemDoubleClicked.connect(self._use_item)
        lay.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.link_btn = QPushButton("Use this server")
        self.link_btn.setObjectName("Primary")
        self.link_btn.setCursor(Qt.PointingHandCursor)
        self.link_btn.setEnabled(False)
        self.link_btn.clicked.connect(self.use_selected)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("Ghost")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.link_btn)
        buttons.addWidget(close_btn)
        lay.addLayout(buttons)

    def _account(self) -> dict | None:
        uid = self.account_combo.currentData()
        return next((a for a in self.win.accounts if a["user_id"] == uid), None)

    # --- game search (same behaviour as the Setup page's) ------------------
    def search_games(self):
        if self._searching:
            return
        query = self.search_edit.text().strip()
        if not query:
            return
        self._searching = True
        self.search_btn.setText("Searching...")
        self.search_btn.setEnabled(False)
        self.games.clear()
        self.games.setVisible(False)
        self.search_status.setText(f"Searching Roblox for \"{query}\"...")
        self.search_status.setVisible(True)

        def work():
            try:
                results = search_roblox_games(query)
                error = ""
            except Exception as e:
                results, error = [], str(e)
            try:
                icons = fetch_game_icons(
                    [g["universe_id"] for g in results])
            except Exception:
                icons = {}
            for game in results:
                game["icon_data"] = icons.get(game["universe_id"], b"")
            self._loaded.emit({"search": True, "results": results,
                               "error": error})

        threading.Thread(target=work, daemon=True).start()

    def _on_search_done(self, payload: dict):
        self._searching = False
        self.search_btn.setText("Search")
        self.search_btn.setEnabled(True)
        results = payload.get("results") or []
        self.games.clear()
        if payload.get("error"):
            self.search_status.setText(f"Game search failed: "
                                       f"{payload['error']}")
            return
        if not results:
            self.search_status.setText(
                f"No games found for \"{self.search_edit.text().strip()}\".")
            return
        for game in results:
            pixmap = QPixmap()
            if game.get("icon_data"):
                pixmap.loadFromData(game["icon_data"], "PNG")
            icon = QIcon(pixmap) if not pixmap.isNull() else QIcon()
            item = QListWidgetItem(icon,
                                   f"{game['name']} \u2014 {game['creator']}")
            item.setData(Qt.UserRole, game)
            item.setSizeHint(QSize(0, 56))
            self.games.addItem(item)
        self.games.setVisible(True)
        self.games.setCurrentRow(0)
        self.search_status.setText(f"{len(results)} game"
                                   + ("s" if len(results) != 1 else "")
                                   + " found - click one to list its private "
                                     "servers.")

    def _show_game_details(self, current=None, previous=None):
        game = current.data(Qt.UserRole) if current else None
        if isinstance(game, dict):
            self.search_status.setText(
                f"{game['name']} \u2014 Creator: {game['creator']}")

    def _pick_game(self, item=None):
        item = item or self.games.currentItem()
        game = item.data(Qt.UserRole) if item else None
        if not isinstance(game, dict):
            return
        self._place_id = str(game["root_place_id"])
        self.games.setVisible(False)
        self.search_status.setText(
            f"Game: {game['name']} \u2014 Place ID {self._place_id}. Loading "
            f"its private servers...")
        self.search_status.setStyleSheet(f"color:{ACCENT_SOFT};")
        self.load()

    def use_all_games(self):
        self._place_id = ""
        self.games.setVisible(False)
        self.search_status.setText("Loading every private server this account "
                                   "owns...")
        self.search_status.setStyleSheet(f"color:{ACCENT_SOFT};")
        self.load()

    def load(self):
        acc = self._account()
        if not acc or not acc.get("cookie"):
            self.status.setText("Add an account with its cookie first.")
            return
        place = self._place_id
        self.status.setText("Asking Roblox for this account's private servers...")
        self.reload_btn.setEnabled(False)
        self.list.clear()
        self.link_btn.setEnabled(False)
        self._rows = []

        def work():
            try:
                rows, err = fetch_private_servers(acc["cookie"], acc["user_id"],
                                                  place_id=place)
            except Exception as e:
                rows, err = [], f"load failed ({e})"
            self._loaded.emit({"rows": rows, "error": err})

        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, payload: dict):
        if payload.get("search"):
            self._on_search_done(payload)
            return
        self.reload_btn.setEnabled(True)
        # Roblox falls back to listing *every* server when the account has
        # none in the game that was asked for, which reads as "my server is
        # not here". Filter by the game that was actually picked.
        self._all_rows = payload.get("rows") or []
        self._rows = self._filtered()
        self._render()

    def _filtered(self) -> list[dict]:
        """Only the servers of the game that was actually chosen.

        Roblox's own endpoints return *every* server an account owns when the
        requested game has none, which reads as "my server is not here". So
        the list is narrowed to the game the search picked, and the empty case
        is reported clearly."""
        place = str(self._place_id or "").strip()
        if not place:
            return list(self._all_rows)
        return [r for r in self._all_rows
                if str(r.get("place_id", "")) == place]

    def _render(self):
        """Show the filtered servers, or a clear message when there are none."""
        rows = self._rows
        self.list.clear()
        self.link_btn.setEnabled(bool(rows))
        if self._all_rows and not rows:
            self.status.setText(
                "This account has no private server in the game you picked "
                "(only its own private servers are listed). Choose All my "
                "private servers to see every game.")
            return
        if not self._all_rows:
            self.status.setText(
                "This account owns no private servers"
                + (" in that game" if self._place_id else "")
                + " - Roblox only lists servers this account owns.")
            return
        self.status.setText(f"{len(rows)} private server"
                            + ("s" if len(rows) != 1 else "")
                            + " - select one to use its join link.")
        for row in rows:
            text = (f"{row['name']}  -  {row['game']}"
                    + ("" if row.get("active") else "  (inactive)"))
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, row)
            if row.get("link"):
                item.setToolTip(row["link"])
            else:
                item.setText(text + "  (no link - use Generate link)")
            item.setSizeHint(QSize(0, 48))
            self.list.addItem(item)
        if rows:
            self.list.setCurrentRow(0)

    def use_selected(self):
        item = self.list.currentItem()
        if item:
            self._use_item(item)

    def _use_item(self, item):
        row = item.data(Qt.UserRole) if item else None
        if not row:
            return
        link = row.get("link") or ""
        if not link:
            self._generate_link(row)
            return
        self.win.apply_join_link(link, f"{row['name']} ({row['game']})")

    def _generate_link(self, row: dict):
        acc = self._account()
        if not acc or not acc.get("cookie"):
            return
        self.status.setText("Asking Roblox for a fresh join link...")
        self.reload_btn.setEnabled(False)

        def work():
            try:
                link = generate_private_server_link(
                    acc["cookie"], row.get("id", ""), row.get("place_id", ""))
            except Exception:
                link = ""
            fresh = dict(row, link=link)
            # Keep the other servers, with the new link in place of the old.
            merged = [fresh if r.get("id") == row.get("id") else dict(r)
                      for r in (self._all_rows or [row])]
            self._loaded.emit({"rows": merged,
                               "error": "" if link else
                               "Roblox would not generate a link"})

        threading.Thread(target=work, daemon=True).start()


def _section(title: str) -> QLabel:
    lab = QLabel(title.upper())
    lab.setObjectName("Section")
    f = lab.font()
    f.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
    lab.setFont(f)
    return lab


def _card_of(rows: list[QWidget]) -> QFrame:
    card = make_card()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(0, 4, 0, 4)
    lay.setSpacing(0)
    for i, r in enumerate(rows):
        if i:
            sep = hsep()
            sep.setContentsMargins(16, 0, 0, 0)
            lay.addWidget(sep)
        lay.addWidget(r)
    return card


def _scroll(inner: QWidget) -> QScrollArea:
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sc.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    sc.setWidget(inner)
    sc.setFrameShape(QFrame.NoFrame)
    return sc


def _page_scaffold(title: str, subtitle: str):
    """Returns (page_widget, body_layout) with a standard header."""
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    header = QWidget()
    hl = QVBoxLayout(header)
    hl.setContentsMargins(30, 24, 30, 10)
    hl.setSpacing(4)
    h1 = QLabel(title)
    h1.setObjectName("H1")
    hl.addWidget(h1)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("Muted")
        hl.addWidget(sub)
    outer.addWidget(header)

    hairline = QFrame()
    hairline.setObjectName("HeaderRule")
    hairline.setFixedHeight(1)
    outer.addWidget(hairline)

    body_host = QWidget()
    body = QVBoxLayout(body_host)
    body.setContentsMargins(30, 16, 30, 26)
    body.setSpacing(14)
    outer.addWidget(_scroll(body_host), 1)
    return page, body


# Settings page for appearance, game selection, and relaunch behavior.
class SetupPage(QWidget):
    _game_search_complete = Signal(object)

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self._game_search_in_progress = False
        page, body = _page_scaffold("Rejoiner Setup",
                                    "Defaults and behavior for every session.")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page)

        cfg = win.cfg
        self.body = body
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(THEMES)
        self.theme_combo.setCurrentText(win.theme_name)
        self.theme_combo.currentTextChanged.connect(win.set_theme)
        body.addWidget(_section("Appearance"))
        body.addWidget(_card_of([
            _row("Color theme", "Choose a color palette",
                 self.theme_combo),
        ]))

        # Choose the default Roblox experience for accounts without an override.
        self.place_edit = QLineEdit(cfg.get("place", ""))
        self.place_edit.setPlaceholderText("Place ID or game link")
        self.place_edit.setFixedWidth(260)
        self.place_edit.editingFinished.connect(self._default_place_edited)
        body.addWidget(_section("Game"))
        body.addWidget(_card_of([
            _row("Default place", "Used when an account has no place of its own",
                 self.place_edit),
        ]))

        selected_card = make_card()
        selected_layout = QHBoxLayout(selected_card)
        selected_layout.setContentsMargins(14, 12, 14, 12)
        selected_layout.setSpacing(14)
        self.selected_game_icon = QLabel("No icon")
        self.selected_game_icon.setObjectName("Inset")
        self.selected_game_icon.setAlignment(Qt.AlignCenter)
        self.selected_game_icon.setFixedSize(72, 72)
        selected_layout.addWidget(self.selected_game_icon)
        self.selected_game_summary = QLabel()
        self.selected_game_summary.setObjectName("RowSub")
        self.selected_game_summary.setWordWrap(True)
        selected_layout.addWidget(self.selected_game_summary, 1)
        self._show_saved_game()
        body.addWidget(_section("Selected game"))
        body.addWidget(selected_card)

        self._game_search_complete.connect(self._on_game_search_complete)
        search_card = make_card()
        search_layout = QVBoxLayout(search_card)
        search_layout.setContentsMargins(16, 12, 16, 12)
        search_layout.setSpacing(8)
        search_row = QHBoxLayout()
        self.game_search_edit = QLineEdit()
        self.game_search_edit.setPlaceholderText("Search Roblox games by name")
        self.game_search_edit.returnPressed.connect(self.search_games)
        search_row.addWidget(self.game_search_edit, 1)
        self.game_search_button = QPushButton("Search")
        self.game_search_button.setObjectName("Ghost")
        self.game_search_button.setCursor(Qt.PointingHandCursor)
        self.game_search_button.clicked.connect(self.search_games)
        search_row.addWidget(self.game_search_button)
        search_layout.addLayout(search_row)

        self.game_results = QListWidget()
        self.game_results.setObjectName("GameResults")
        self.game_results.setIconSize(QSize(40, 40))
        self.game_results.setFixedHeight(5 * 56 + 2)
        self.game_results.setEnabled(False)
        self.game_results.setVisible(False)
        self.game_results.currentItemChanged.connect(self._show_game_details)
        self.game_results.itemClicked.connect(self.use_selected_game)
        self.game_results_panel = QWidget()
        results_layout = QVBoxLayout(self.game_results_panel)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.addWidget(self.game_results)
        self.game_results_panel.setVisible(False)
        search_layout.addWidget(self.game_results_panel)
        self.game_search_status = QLabel("")
        self.game_search_status.setObjectName("RowSub")
        self.game_search_status.setWordWrap(True)
        self.game_search_status.setVisible(False)
        current_universe = str(cfg.get("game_universe_id", "") or "")
        if current_universe:
            self.game_search_status.setText(
                f"Selected experience Universe ID: {current_universe}")
        search_layout.addWidget(self.game_search_status)
        body.addWidget(_section("Find a game"))
        body.addWidget(search_card)

        # Share / private-server link used to send monitored accounts to one
        # specific server. The link and the number of accounts it applies to
        # are both editable, and each account can override the link.
        self.join_edit = QLineEdit(cfg.get("joinserver_url", ""))
        self.join_edit.setPlaceholderText("Roblox share / private-server link")
        self.join_edit.editingFinished.connect(self._join_link_edited)
        self.sw_join = self._switch("joinserver_on")
        check_btn = QPushButton("Check link")
        check_btn.setObjectName("Ghost")
        check_btn.setCursor(Qt.PointingHandCursor)
        check_btn.clicked.connect(
            lambda: win.check_join_link(self.join_edit.text().strip()))
        pick_btn = QPushButton("Pick a server...")
        pick_btn.setObjectName("Ghost")
        pick_btn.setCursor(Qt.PointingHandCursor)
        pick_btn.clicked.connect(self.win.open_private_server_picker)
        link_row = QWidget()
        lr = QHBoxLayout(link_row)
        lr.setContentsMargins(16, 11, 16, 11)
        lr.setSpacing(10)
        lr.addWidget(self.join_edit, 1)
        lr.addWidget(check_btn)
        lr.addWidget(pick_btn)
        body.addWidget(_section("Join a specific server"))
        body.addWidget(_card_of([
            _row("Join specific server", "Send the accounts chosen below to "
                 "the private-server link; everyone else uses their own game",
                 self.sw_join),
            link_row,
        ]))
        # Which accounts the link applies to. The list lives in a dropdown so
        # the page stays compact: ticked accounts join the private server, the
        # rest keep the game they were set up with on their own row.
        self.join_dropdown = JoinAccountsDropdown(self.win)
        body.addWidget(self.join_dropdown)
        self.join_status = QLabel("")
        self.join_status.setObjectName("RowSub")
        self.join_status.setWordWrap(True)
        self.join_status.setContentsMargins(18, 0, 0, 0)
        body.addWidget(self.join_status)
        self.refresh_join_accounts()
        self.refresh_join_status()

        # Global delay applied before retrying a failed client.
        self.delay_edit = QLineEdit(str(cfg.get("delay", "60")))
        self.delay_edit.setFixedWidth(90)
        self.delay_edit.editingFinished.connect(
            lambda: win.set_setting("delay", self.delay_edit.text().strip()))
        body.addWidget(_section("Timing"))
        body.addWidget(_card_of([
            _row("Rejoin delay", "Seconds to wait before rejoining after a crash/kick",
                 self.delay_edit),
        ]))

        # Configure relaunch, kick-detection, and server-separation behavior.
        self.sw_relaunch = self._switch("auto_relaunch", default=True)
        self.sw_kick = self._switch("kickdetect_on")
        self.sw_diff = self._switch("diffserver_on")
        self.sw_detect = self._switch("detect_open")
        body.addWidget(_section("Behavior"))
        body.addWidget(_card_of([
            _row("Auto relaunch", "Relaunch accounts after crashes/kicks — turn off "
                 "to just launch them once", self.sw_relaunch),
            _row("Kick detection", "Rejoin when a client is kicked but still open",
                 self.sw_kick),
            _row("Different servers", "Put each account in its own server "
                 "(auto-splits collisions)", self.sw_diff),
            _row("Detect open clients", "Adopt already-open Roblox clients on "
                 "start instead of launching", self.sw_detect),
        ]))

        body.addStretch(1)

    def _join_link_edited(self):
        self.win.set_setting("joinserver_url", self.join_edit.text().strip())
        self.refresh_join_status()

    def refresh_join_accounts(self):
        """Fill the dropdown's popup with the accounts that can be sent."""
        dropdown = getattr(self, "join_dropdown", None)
        if dropdown is None:
            return
        dropdown.refresh()

    def refresh_join_row(self, uid):
        """Update one account's line after its private-server choice changed."""
        dropdown = getattr(self, "join_dropdown", None)
        if dropdown is not None:
            dropdown.refresh()

    def refresh_join_status(self, verified: str = ""):
        """Show what the link and the chosen accounts will actually do."""
        dropdown = getattr(self, "join_dropdown", None)
        if dropdown is not None:
            dropdown.refresh()
        """Show what the link and the chosen accounts will actually do."""
        if not self.sw_join.isChecked():
            self.join_status.setText(
                "Off — every monitored account uses its own game.")
            self.join_status.setStyleSheet(f"color:{MUTED};")
            return
        link = (self.join_edit.text().strip()
                or str(self.win.cfg.get("joinserver_url", "") or "").strip())
        if not link:
            self.join_status.setText(
                "On, but no link is set — paste a share / "
                "private-server link above, or leave this off.")
            self.join_status.setStyleSheet(f"color:{WARN};")
            return
        parsed = parse_join_link(link)
        if parsed is None:
            self.join_status.setText(
                "That doesn't look like a Roblox share / private-server link.")
            self.join_status.setStyleSheet(f"color:{BAD};")
            return
        kinds = {"private": "a private server",
                 "job": "one specific server",
                 "open": "that game (Roblox picks any server)"}
        what = kinds.get(parsed.kind,
                         "a share link (it resolves to the private server)")
        chosen = [a for a in self.win.accounts
                  if a.get("cookie") and a.get("join_on", True) is not False]
        if not chosen:
            self.join_status.setText(
                f"The link points to {what}, but no account is chosen "
                f"below — tick at least one.")
            self.join_status.setStyleSheet(f"color:{WARN};")
            return
        names = ", ".join(a.get("alias") or a.get("username", "?")
                          for a in chosen[:6])
        more = "" if len(chosen) <= 6 else f" (+{len(chosen) - 6} more)"
        self.join_status.setText(
            f"Sending {names}{more} to {what}. Everyone else uses their own "
            f"game."
            + (f"\nLast check: {verified}" if verified else ""))
        self.join_status.setStyleSheet(f"color:{ACCENT_SOFT};")

    def _default_place_edited(self):
        self.win.set_default_place(self.place_edit.text().strip())
        self._show_saved_game()

    def search_games(self):
        if self._game_search_in_progress:
            return
        query = self.game_search_edit.text().strip()
        if not query:
            self.game_search_status.setText("Enter a game name to search.")
            self.game_search_status.setVisible(True)
            return
        self._game_search_in_progress = True
        self.game_search_button.setText("Searching…")
        self.game_results.clear()
        self.game_results.setEnabled(False)
        self.game_results_panel.setVisible(False)
        self.game_search_status.setText("Searching Roblox games…")
        self.game_search_status.setVisible(True)

        def work():
            try:
                results = search_roblox_games(query)
                icon_error = ""
                try:
                    icons = fetch_game_icons(
                        [game["universe_id"] for game in results])
                except Exception as e:
                    icons = {}
                    icon_error = str(e)
                for game in results:
                    game["icon_data"] = icons.get(game["universe_id"], b"")
                self._game_search_complete.emit(
                    {"query": query, "results": results, "error": "",
                     "icon_error": icon_error})
            except Exception as e:
                self._game_search_complete.emit(
                    {"query": query, "results": [], "error": str(e),
                     "icon_error": ""})

        threading.Thread(target=work, daemon=True).start()

    def _on_game_search_complete(self, result: dict):
        self._game_search_in_progress = False
        self.game_search_button.setText("Search")
        self.game_results.clear()
        results = result["results"]
        if result["error"]:
            self.game_search_status.setText(result["error"])
            self.game_search_status.setVisible(True)
            self.game_results.setEnabled(False)
            self.game_results_panel.setVisible(False)
            return
        if not results:
            self.game_search_status.setText(
                f"No games found for “{result['query']}”.")
            self.game_search_status.setVisible(True)
            self.game_results.setEnabled(False)
            self.game_results_panel.setVisible(False)
            return
        for game in results:
            pixmap = QPixmap()
            if game.get("icon_data"):
                pixmap.loadFromData(game["icon_data"], "PNG")
            icon = QIcon(pixmap) if not pixmap.isNull() else QIcon()
            item = QListWidgetItem(
                icon, f"{game['name']} — {game['creator']}")
            item.setData(Qt.UserRole, game)
            item.setSizeHint(QSize(0, 56))
            self.game_results.addItem(item)
        self.game_results.setEnabled(True)
        self.game_results.setVisible(True)
        self.game_results_panel.setVisible(True)
        self.game_results.setCurrentRow(0)
        self._show_game_details(self.game_results.currentItem())
        icon_count = sum(bool(game.get("icon_data")) for game in results)
        if result.get("icon_error"):
            self.game_search_status.setText(
                f"Found {len(results)} games, but their icons could not be "
                f"loaded: {result['icon_error']}")
        elif not icon_count:
            self.game_search_status.setText(
                "Found games, but their icons could not be loaded. Search again later.")
        elif icon_count < len(results):
            self.game_search_status.setText(
                f"{icon_count} of {len(results)} game icons loaded. Select a result "
                "with an icon to continue.")

    def _show_game_details(self, current=None, previous=None):
        game = current.data(Qt.UserRole) if current else None
        if not isinstance(game, dict):
            return
        pixmap = QPixmap()
        icon_data = game.get("icon_data", b"")
        has_icon = bool(icon_data) and pixmap.loadFromData(icon_data, "PNG")
        self.game_search_status.setText(
            f"Universe ID: {game['universe_id']}  ·  "
            f"Starting Place ID: {game['root_place_id']}"
            + ("" if has_icon else "  ·  Icon unavailable"))

    def use_selected_game(self, item=None):
        item = item or self.game_results.currentItem()
        game = item.data(Qt.UserRole) if item else None
        if not isinstance(game, dict):
            return
        icon = QPixmap()
        if not game.get("icon_data") or not icon.loadFromData(
                game["icon_data"], "PNG"):
            self.game_search_status.setText(
                "This game’s icon is unavailable, so it can’t be selected yet.")
            return
        try:
            self.win.select_game(game)
        except (OSError, ValueError, RuntimeError) as e:
            self.game_search_status.setText(
                f"Couldn't save the selected game icon: {e}")
            return
        self.place_edit.setText(game["root_place_id"])
        self._show_saved_game(game.get("icon_data", b""))
        self.game_search_edit.setFocus(Qt.OtherFocusReason)
        self.game_results_panel.setVisible(False)
        self.game_search_status.clear()
        self.game_search_status.setVisible(False)

    def _show_saved_game(self, icon_data: bytes | None = None):
        name = str(self.win.cfg.get("game_name") or "")
        universe_id = str(self.win.cfg.get("game_universe_id") or "")
        place_id = str(self.win.cfg.get("place") or "")
        if not name or not universe_id:
            self.selected_game_icon.clear()
            self.selected_game_icon.setText("No icon")
            self.selected_game_summary.setText(
                "No searched game selected. Choose a game below to keep its icon here.")
            return
        if icon_data is None:
            try:
                icon_data = self.win.game_icon_path.read_bytes()
            except FileNotFoundError:
                icon_data = b""
            except OSError as e:
                icon_data = b""
                self.selected_game_summary.setText(
                    f"{name} · Universe ID: {universe_id} · "
                    f"Starting Place ID: {place_id}\n"
                    f"Couldn't read the saved game icon: {e}")
                self.selected_game_icon.setText("Icon unavailable")
                return
        pixmap = QPixmap()
        if icon_data and pixmap.loadFromData(icon_data, "PNG"):
            self.selected_game_icon.setPixmap(
                pixmap.scaled(64, 64, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation))
        else:
            self.selected_game_icon.clear()
            self.selected_game_icon.setText("Icon unavailable")
        self.selected_game_summary.setText(
            f"{name}\nUniverse ID: {universe_id} · Starting Place ID: {place_id}")

    def _switch(self, cfg_key: str, default: bool = False) -> IOSSwitch:
        flag = {"kickdetect_on": "kick_detect", "diffserver_on": "diff_server",
                "detect_open": "detect_open", "auto_relaunch": "auto_relaunch",
                "joinserver_on": "join_server"}[cfg_key]
        sw = IOSSwitch(bool(self.win.cfg.get(cfg_key, default)))
        sw.toggled.connect(lambda v, k=cfg_key, f=flag: self.win.set_flag(k, f, v))
        return sw


# Roblox installation settings page.
class RobloxSettingsPage(QWidget):
    _webhook_test_complete = Signal(object)

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        cfg = win.cfg
        page, body = _page_scaffold(
            "Settings",
            "Roblox installation, Discord notifications and app updates.")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page)

        # Select or detect the folder containing the Roblox client executable.
        self._detected = ""
        self.path_edit = QLineEdit(str(cfg.get("roblox_dir", "") or ""))
        self.path_edit.setPlaceholderText(
            r"e.g. C:\Users\You\AppData\Local\Roblox\Versions\version-abc123")
        self.path_edit.setMinimumWidth(360)
        self.path_edit.editingFinished.connect(self._path_typed)
        self._webhook_test_complete.connect(self._on_webhook_test_complete)
        browse = QPushButton("Browse…")
        browse.setObjectName("Ghost")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse)

        card = make_card()
        pc = QVBoxLayout(card)
        pc.setContentsMargins(16, 14, 16, 14)
        pc.setSpacing(10)
        cap = QLabel("Folder containing RobloxPlayerBeta.exe")
        cap.setObjectName("RowTitle")
        pc.addWidget(cap)
        note = QLabel("Auto-detected below. Leave empty to use the normal Roblox "
                      "launcher — set this only to force a specific install, e.g. "
                      "a portable copy or a particular version.")
        note.setObjectName("RowSub")
        note.setWordWrap(True)
        pc.addWidget(note)
        prow = QHBoxLayout()
        prow.setSpacing(10)
        prow.addWidget(self.path_edit, 1)
        prow.addWidget(browse)
        pc.addLayout(prow)
        self.detected_lbl = QLabel("Scanning for Roblox…")
        self.detected_lbl.setObjectName("RowSub")
        self.detected_lbl.setWordWrap(True)
        pc.addWidget(self.detected_lbl)
        brow = QHBoxLayout()
        self.use_btn = QPushButton("Use detected install")
        self.use_btn.setObjectName("Link")
        self.use_btn.setCursor(Qt.PointingHandCursor)
        self.use_btn.clicked.connect(self._use_detected)
        rescan = QPushButton("Rescan")
        rescan.setObjectName("Link")
        rescan.setCursor(Qt.PointingHandCursor)
        rescan.clicked.connect(self._rescan)
        brow.addWidget(self.use_btn)
        brow.addWidget(rescan)
        brow.addStretch(1)
        pc.addLayout(brow)

        body.addWidget(_section("Roblox installation"))
        body.addWidget(card)

        self.webhook_edit = QLineEdit(win.webhook_url)
        self.webhook_edit.setPlaceholderText("https://discord.com/api/webhooks/…")
        self.webhook_edit.setMinimumWidth(300)
        self.webhook_edit.editingFinished.connect(
            lambda: win.set_webhook(self.webhook_edit.text().strip()))
        self.webhook_test_button = QPushButton("Send test message")
        self.webhook_test_button.setObjectName("Ghost")
        self.webhook_test_button.setCursor(Qt.PointingHandCursor)
        self.webhook_test_button.clicked.connect(self.test_webhook)
        self._webhook_test_complete.connect(self._on_webhook_test_complete)
        webhook_row = QWidget()
        webhook_layout = QHBoxLayout(webhook_row)
        webhook_layout.setContentsMargins(0, 0, 0, 0)
        webhook_layout.setSpacing(8)
        webhook_layout.addWidget(self.webhook_edit, 1)
        webhook_layout.addWidget(self.webhook_test_button)

        # Automatic self-update from GitHub releases (standalone exe only).
        self.update_check_btn = QPushButton("Check now")
        self.update_check_btn.setObjectName("Ghost")
        self.update_check_btn.setCursor(Qt.PointingHandCursor)
        self.update_check_btn.clicked.connect(
            lambda: self.win.check_updates(manual=True))
        self.update_restart_btn = QPushButton("Restart & update")
        self.update_restart_btn.setObjectName("Primary")
        self.update_restart_btn.setCursor(Qt.PointingHandCursor)
        self.update_restart_btn.clicked.connect(self.win.restart_for_update)
        self.update_restart_btn.setVisible(False)
        update_btns = QWidget()
        ub = QHBoxLayout(update_btns)
        ub.setContentsMargins(0, 0, 0, 0)
        ub.setSpacing(8)
        ub.addWidget(self.update_check_btn)
        ub.addWidget(self.update_restart_btn)
        self.update_status = QLabel(f"Version {APP_VERSION}")
        self.update_status.setObjectName("RowSub")
        self.update_status.setWordWrap(True)
        self.update_status.setContentsMargins(18, 0, 0, 0)
        body.addWidget(_section("Notifications"))
        body.addWidget(_card_of([
            _row("Discord webhook", "Color-coded embeds for launches, crashes, "
                 "kicks, disconnects and stops — one clean card per event",
                 webhook_row),
        ]))
        self.webhook_test_status = QLabel("")
        self.webhook_test_status.setObjectName("RowSub")
        self.webhook_test_status.setWordWrap(True)
        self.webhook_test_status.setContentsMargins(18, 0, 0, 0)
        body.addWidget(self.webhook_test_status)

        body.addWidget(_section("App updates"))
        body.addWidget(_card_of([
            _row("Standalone app updates", "Checks GitHub releases "
                 "automatically and installs new versions", update_btns),
        ]))
        body.addWidget(self.update_status)
        body.addStretch(1)
        QTimer.singleShot(150, self._rescan)

    def set_update_status(self, info: dict):
        """Reflect the updater state under the App updates card."""
        state = info.get("state", "")
        if state == "source":
            text = ("Running from source — auto-update only applies to the "
                    "standalone exe.")
        elif state == "checking":
            text = "Checking GitHub for a newer release…"
        elif state == "uptodate":
            text = (f"Version {APP_VERSION} — up to date "
                    f"(latest release {info.get('tag', '?')}).")
        elif state == "available":
            text = f"{info.get('tag', '?')} available — downloading…"
        elif state == "downloading":
            text = (f"Downloading {info.get('tag', '?')}… "
                    f"{info.get('progress', 0)}%")
        elif state == "ready":
            text = f"{info.get('tag', '?')} downloaded — restart to apply."
        elif state == "error":
            text = f"Update check failed: {info.get('error', '?')}"
        else:
            text = f"Version {APP_VERSION}"
        self.update_status.setText(text)
        self.update_restart_btn.setVisible(state == "ready")

    def test_webhook(self):
        """Send a sample embed, driven by this page's own fields."""
        panel = self
        url = panel.webhook_edit.text().strip()
        if not _is_discord_webhook(url):
            panel.webhook_test_status.setText(
                "Enter a valid Discord webhook URL before testing.")
            panel.webhook_test_status.setVisible(True)
            return

        self.win.set_webhook(url)
        dc = self.win.cfg.get("discord", {})
        username = dc.get("username", "Roblox Rejoiner")
        avatar = dc.get("avatar", "")
        panel.webhook_test_button.setEnabled(False)
        panel.webhook_test_button.setText("Sending…")
        panel.webhook_test_status.setText("Sending test message to Discord…")
        panel.webhook_test_status.setVisible(True)

        def send_test():
            try:
                emb = make_embed(
                    title="ExampleAccount",
                    description="This is how notifications arrive: one clean "
                                "embed per event, with the account, place "
                                "and server spelled out.",
                    color=COLOR_GOOD,
                    fields=[
                        ("Event", "In game", False),
                        ("Details", "**Place:** 8304191830\n"
                         "**Server:** a1b2c3d4e5f6\n"
                         "**Session:** up 5m 12s\n"
                         "**Accounts online:** 2/3", False),
                    ],
                    footer="Roblox Rejoiner \u00b7 test message")
                result = discord_post(url, username, avatar, embeds=[emb])
            except Exception as exc:
                result = (False, f"Send failed: {exc}")
            self._webhook_test_complete.emit(result)

        threading.Thread(target=send_test, daemon=True).start()

    def _on_webhook_test_complete(self, result: tuple[bool, str]):
        self.webhook_test_button.setEnabled(True)
        self.webhook_test_button.setText("Test webhook")
        ok, error = result
        self.webhook_test_status.setText(
            "Test message sent successfully." if ok
            else f"Webhook test failed: {error}")

    def _rescan(self):
        self.detected_lbl.setText("Scanning for Roblox…")
        self._detected = scan_roblox_install()
        if self._detected:
            self.detected_lbl.setText(f"Detected: {self._detected}")
            self.use_btn.setEnabled(True)
        else:
            self.detected_lbl.setText(
                "No Roblox install auto-detected — paste the folder above if you "
                "want to force one.")
            self.use_btn.setEnabled(False)

    def _use_detected(self):
        if self._detected:
            self.path_edit.setText(self._detected)
            self.win.set_setting("roblox_dir", self._detected)

    def _path_typed(self):
        self.win.set_setting("roblox_dir", self.path_edit.text().strip())

    def _browse(self):
        from PySide6.QtWidgets import QFileDialog
        start = (self.path_edit.text().strip() or self._detected
                 or os.environ.get("LOCALAPPDATA", "") or "")
        d = QFileDialog.getExistingDirectory(
            self, "Select the folder containing RobloxPlayerBeta.exe", start)
        if d:
            self.path_edit.setText(d)
            self.win.set_setting("roblox_dir", d)


# Account management page.
class AccountRow(QFrame):
    def __init__(self, win: "MainWindow", acc: dict):
        super().__init__()
        self.win = win
        self.acc = acc
        self.uid = acc["user_id"]
        self.setObjectName("Inset")
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 12, 10)
        root.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.sw = IOSSwitch(win.is_monitored(acc["user_id"]))
        self.sw.toggled.connect(
            lambda v: win.set_monitored(acc["user_id"], v))
        top.addWidget(self.sw, 0, Qt.AlignVCenter)
        self.avatar = Avatar(acc.get("display") or acc.get("username", "?"), 40)
        top.addWidget(self.avatar, 0, Qt.AlignVCenter)
        self._load_cached_avatar()
        col = QVBoxLayout()
        col.setSpacing(1)
        self.name_lbl = QLabel(
            acc.get("alias") or acc.get("display") or acc.get("username", "account")
        )
        self.name_lbl.setObjectName("RowTitle")
        col.addWidget(self.name_lbl)
        self.sub_lbl = QLabel(self._sub_text())
        self.sub_lbl.setObjectName("RowSub")
        col.addWidget(self.sub_lbl)
        top.addLayout(col)
        top.addStretch(1)
        self.gear = QPushButton("Settings")
        self.gear.setObjectName("Ghost")
        self.gear.setCheckable(True)
        self.gear.setCursor(Qt.PointingHandCursor)
        self.gear.toggled.connect(self._toggle_settings)
        top.addWidget(self.gear, 0, Qt.AlignVCenter)
        rm = QPushButton("Remove")
        rm.setObjectName("Ghost")
        rm.setCursor(Qt.PointingHandCursor)
        rm.clicked.connect(lambda: win.remove_account(acc["user_id"]))
        top.addWidget(rm, 0, Qt.AlignVCenter)
        root.addLayout(top)

        # Optional game and delay overrides for this account.
        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(52, 6, 4, 2)
        pl.setSpacing(2)
        pl.addWidget(hsep())
        self.place = QLineEdit(acc.get("place", "") or "")
        self.place.setPlaceholderText("uses default game")
        self.place.setFixedWidth(230)
        self.place.editingFinished.connect(self._save_place)
        self.delay = QLineEdit(str(acc.get("delay", "") or ""))
        self.delay.setPlaceholderText("global")
        self.delay.setFixedWidth(110)
        self.delay.editingFinished.connect(self._save_delay)
        pl.addWidget(_row("Game / place",
                          "Place ID or link just for this account", self.place))
        pl.addWidget(_row("Rejoin delay",
                          "Seconds before rejoining after a crash (blank = global)",
                          self.delay))
        # Whether this account joins the private server picked on Rejoiner
        # Setup. Off means it always uses its own game.
        self.sw_join = self._switch("join_on")
        pl.addWidget(_row("Send to private server",
                          "Send this account to the private-server link chosen "
                          "on Rejoiner Setup (off = always uses its own game)",
                          self.sw_join))
        self.panel.setVisible(False)
        root.addWidget(self.panel)

    def _toggle_settings(self, on: bool):
        self.panel.setVisible(bool(on))

    def _sub_text(self) -> str:
        if not self.acc.get("cookie"):
            return "cookie missing"
        uname = self.acc.get("username", "")
        extras = []
        if self.acc.get("note"):
            extras.append(self.acc["note"])
        if self.acc.get("join_on", True) is False:
            extras.append("own game only")
        if str(self.acc.get("delay", "") or "").strip():
            extras.append(f"{self.acc['delay']}s delay")
        tail = (" · " + ", ".join(extras)) if extras else ""
        return f"@{uname} · {self.win.lifetime_str(self.uid)}{tail}"

    def _load_cached_avatar(self):
        try:
            import os
            from roblox_account_profiles import CACHE_DIR
            path = os.path.join(CACHE_DIR, f"{self.uid}.png")
            if os.path.exists(path):
                pix = QPixmap(path)
                if not pix.isNull():
                    self.avatar.set_pixmap(pix)
        except Exception:
            pass

    def set_profile(self, display: str, avatar_path: str):
        if display:
            self.name_lbl.setText(self.acc.get("alias") or display)
            self.avatar.set_name(self.acc.get("alias") or display)
        if avatar_path:
            pix = QPixmap(avatar_path)
            if not pix.isNull():
                self.avatar.set_pixmap(pix)

    def set_presence(self, state):
        self.avatar.set_presence(state)

    def refresh_sub(self):
        self.sub_lbl.setText(self._sub_text())

    def _save_place(self):
        self.acc["place"] = self.place.text().strip()
        self.win.persist()

    def _save_delay(self):
        self.acc["delay"] = self.delay.text().strip()
        self.win.persist()
        self.refresh_sub()

    def _switch(self, cfg_key: str, default: bool = False) -> IOSSwitch:
        """A per-account override switch, defaulting to the global setting."""
        sw = IOSSwitch(bool(self.acc.get(cfg_key, default)))
        sw.toggled.connect(lambda v, k=cfg_key: self._save_flag(k, v))
        return sw

    def _save_flag(self, key: str, value: bool):
        self.acc[key] = bool(value)
        self.win.persist()
        self.refresh_sub()

    def set_monitored_silent(self, v: bool):
        self.sw.setChecked(v, animate=True)

    def refresh_join(self):
        """Re-read the per-account private-server choice after a change."""
        self.sw_join.setChecked(bool(self.acc.get("join_on", True)),
                                animate=False)


class AccountsPage(QWidget):
    _resolved = Signal(object)

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self._rows: dict[int, AccountRow] = {}
        page, self.body = _page_scaffold(
            "Accounts",
            "Add your Roblox accounts and choose which ones to monitor.")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page)

        # Add accounts through the dialog that accepts a user-supplied cookie.
        head = QWidget()
        hh = QHBoxLayout(head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(8)
        hh.addWidget(_section("Add account"))
        hh.addStretch(1)
        add_btn = QPushButton("Add Roblox account")
        add_btn.setObjectName("Primary")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_clicked)
        self.add_btn = add_btn
        hh.addWidget(add_btn)
        self.body.addWidget(head)
        self.err = QLabel("")
        self.err.setObjectName("RowSub")
        self.err.setStyleSheet(f"color:{BAD};")
        self.body.addWidget(self.err)

        # Choose whether monitoring applies to every account or a subset.
        self.sw_all = IOSSwitch(win.select_all)
        self.sw_all.toggled.connect(win.set_select_all)
        self.body.addWidget(_section("Monitoring"))
        self.body.addWidget(_card_of([
            _row("Select all", "Monitor every account with a valid cookie",
                 self.sw_all),
        ]))

        # Show the configured accounts and their individual settings.
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(8)
        self.body.addWidget(_section("Your accounts"))
        self.body.addWidget(self.list_host)
        self.empty = QLabel("No accounts yet — select Add Roblox account to get started.")
        self.empty.setObjectName("Muted")
        self.body.addWidget(self.empty)
        self.body.addStretch(1)

        self._resolved.connect(self._on_resolved)
        self.rebuild()

    def rebuild(self):
        for i in reversed(range(self.list_lay.count())):
            w = self.list_lay.itemAt(i).widget()
            if w:
                w.setParent(None)
        self._rows.clear()
        for acc in self.win.accounts:
            row = AccountRow(self.win, acc)
            self._rows[acc["user_id"]] = row
            self.list_lay.addWidget(row)
        self.empty.setVisible(not self.win.accounts)
        self.sw_all.setChecked(self.win.select_all, animate=False)

    def refresh_switches(self):
        self.sw_all.setChecked(self.win.select_all, animate=False)
        for uid, row in self._rows.items():
            row.set_monitored_silent(self.win.is_monitored(uid))

    def refresh_stats(self):
        for row in self._rows.values():
            row.refresh_sub()

    def _add_clicked(self):
        dialog = AddRobloxAccountDialog(self.win)
        if dialog.exec() != QDialog.Accepted:
            return
        self.err.setText("")
        self.add_btn.setEnabled(False)
        self.add_btn.setText("Adding…")
        details = {
            "cookie": dialog.cookie,
            "alias": dialog.alias,
            "note": dialog.note,
        }

        def work():
            try:
                uid, uname = get_account_info(details["cookie"])
                if uid:
                    self._resolved.emit((True, uid, uname or f"user {uid}",
                                         details, None))
                else:
                    self._resolved.emit((False, None, None, details,
                                         "couldn't read account (cookie invalid?)"))
            except Exception as e:
                self._resolved.emit((False, None, None, details, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_resolved(self, res):
        ok, uid, uname, details, err = res
        self.add_btn.setEnabled(True)
        self.add_btn.setText("Add Roblox account")
        if not ok:
            self.err.setText(err or "failed")
            return
        if self.win.add_account(
            uid,
            uname,
            details["cookie"],
            alias=details["alias"],
            note=details["note"],
        ):
            self.rebuild()
        else:
            self.err.setText("that account is already added")


# Multi-account monitoring page.
class StatusRow(QFrame):
    def __init__(self, username: str, uid, on_kill):
        super().__init__()
        self.uid = uid
        self.setObjectName("Inset")
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 8, 10, 8)
        h.setSpacing(10)
        self.dot = StatusDot(MUTED)
        h.addWidget(self.dot)
        self.name = QLabel(username)
        self.name.setObjectName("RowTitle")
        h.addWidget(self.name)
        h.addStretch(1)
        self.state = QLabel("—")
        self.state.setObjectName("RowSub")
        h.addWidget(self.state)
        self.badge = QLabel("")
        self.badge.setObjectName("TimerBadge")
        self.badge.setVisible(False)
        h.addWidget(self.badge)
        self.up = QLabel("")
        self.up.setObjectName("RowSub")
        self.up.setFixedWidth(56)
        self.up.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self.up)
        self.kill_btn = QPushButton("Kill")
        self.kill_btn.setObjectName("KillBtn")
        self.kill_btn.setCursor(Qt.PointingHandCursor)
        self.kill_btn.clicked.connect(lambda: on_kill(self.uid))
        h.addWidget(self.kill_btn)

    def update_row(self, st: dict, badge: str = ""):
        s = st.get("state", "—")
        colors = {"running": GOOD, "launching": ACCENT, "waiting": WARN,
                  "leaving": WARN, "stopped": MUTED}
        col = colors.get(s, MUTED)
        # Spells out which server this account is being sent to, so a
        # private-server link is never a silent decision.
        target = str(st.get("join_target") or "")
        if target:
            self.name.setToolTip(f"{self.name.text()} - {target}")
        self.dot.set_color(col)
        label = {"running": "running", "launching": "launching",
                 "leaving": "leaving", "waiting": "rejoining",
                 "stopped": "stopped"}.get(s, s)
        cr = int(st.get("disconnects", st.get("crashes", 0)) or 0)
        text = f"{label}   ·   {cr} disconnect" + ("s" if cr != 1 else "")
        # Surface the latest removal reason (kick code, crash, launch error).
        reason = (st.get("reason") or "").strip()
        if reason and s != "running":
            text += f"\n{reason}"
        elif reason and s == "running":
            self.state.setToolTip(reason)
        self.state.setText(text)
        self.state.setToolTip(reason)
        up = st.get("uptime", 0)
        self.up.setText(fmt_dur(up) if up else "")
        self.badge.setText(badge)
        self.badge.setVisible(bool(badge))

    def set_idle(self):
        self.dot.set_color(MUTED)
        self.state.setText("stopped")
        self.up.setText("")
        self.badge.setVisible(False)


class MonitorPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self._rows: dict[str, StatusRow] = {}
        page, body = _page_scaffold(
            "Account Monitor",
            "Monitors selected accounts independently and rejoins each affected account.",
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page)

        # Start monitoring or close all running Roblox clients.
        ctl = make_card()
        ch = QHBoxLayout(ctl)
        ch.setContentsMargins(16, 14, 16, 14)
        ch.setSpacing(12)
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(win.toggle_monitoring)
        self.kill_btn = QPushButton("Stop all clients")
        self.kill_btn.setObjectName("Danger")
        self.kill_btn.setCursor(Qt.PointingHandCursor)
        self.kill_btn.setToolTip(
            "Close every running Roblox client, including clients outside this monitor.")
        self.kill_btn.clicked.connect(win.kill_all)
        ch.addWidget(self.start_btn)
        ch.addWidget(self.kill_btn)
        ch.addStretch(1)
        self.status_dot = StatusDot(MUTED)
        self.status_lbl = QLabel("Idle")
        self.status_lbl.setObjectName("RowTitle")
        ch.addWidget(self.status_dot)
        ch.addWidget(self.status_lbl)
        body.addWidget(ctl)

        # Session totals for active clients, rejoins, disconnects, and uptime.
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tile_running = StatTile("RUNNING", "0")
        self.tile_rejoins = StatTile("SUCCESSFULLY REJOINED", "0")
        self.tile_disconnects = StatTile("DISCONNECTS", "0")
        self.tile_uptime = StatTile("SESSION", "0:00")
        tiles.addWidget(self.tile_running)
        tiles.addWidget(self.tile_rejoins)
        tiles.addWidget(self.tile_disconnects)
        tiles.addWidget(self.tile_uptime)
        body.addLayout(tiles)

        # Live state for each account selected for monitoring.
        body.addWidget(_section("Accounts"))
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(8)
        body.addWidget(self.list_host)

        # Chronological engine messages for launches and reconnects.
        body.addWidget(_section("Log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        self.log.setObjectName("ActivityLog")
        installed_fonts = {
            family.casefold(): family for family in QFontDatabase.families()
        }
        log_family = next(
            (installed_fonts[name.casefold()]
             for name in ("Cascadia Mono", "Consolas", "Courier New")
             if name.casefold() in installed_fonts),
            "monospace",
        )
        self.log.setFont(QFont(log_family, 10))
        body.addWidget(self.log)
        body.addStretch(1)

    # Receive engine logs and account-state updates.
    def on_log(self, msg: str):
        self.log.appendPlainText(msg)
        # Keep the activity log bounded during long monitoring sessions.
        if self.log.blockCount() > 250:
            cur = self.log.textCursor()
            cur.movePosition(cur.MoveOperation.Start)
            cur.select(cur.SelectionType.LineUnderCursor)
            cur.removeSelectedText()
            cur.deleteChar()

    def on_state(self, rows: list):
        seen = set()
        running = 0
        successful_rejoins = 0
        disconnects = 0
        for st in rows:
            uid = st.get("user_id")
            uname = st["username"]
            seen.add(uid)
            if uid not in self._rows:
                r = StatusRow(uname, uid, self.win.kill_account)
                self._rows[uid] = r
                self.list_lay.addWidget(r)
            badge = self._badge_for(st)
            self._rows[uid].update_row(st, badge)
            if st["state"] == "running":
                running += 1
            successful_rejoins += st.get("successful_rejoins", 0)
            disconnects += int(st.get("disconnects", 0) or 0)
        for uid in list(self._rows):
            if uid not in seen:
                w = self._rows.pop(uid)
                w.setParent(None)
        self.tile_running.set_value(str(running))
        self.tile_rejoins.set_value(str(successful_rejoins))
        self.tile_disconnects.set_value(str(disconnects))

    @staticmethod
    def _badge_for(st):
        if st.get("state") == "waiting" and st.get("custom_delay"):
            return "\u21bb " + fmt_dur(st.get("rejoin_in", 0))
        return ""

    def gray_out(self):
        for r in self._rows.values():
            r.set_idle()
        self.tile_running.set_value("0")
        self.tile_disconnects.set_value("0")

    def on_status(self, text: str, running: bool):
        self.status_lbl.setText(text)
        self.status_dot.set_color(GOOD if running else MUTED)
        self.start_btn.setText("Stop" if running else "Start")

    def set_uptime(self, secs: float):
        self.tile_uptime.set_value(fmt_dur(secs))


# Main application window and page coordinator.
class MainWindow(QWidget):
    _join_checked = Signal(str, str, str, str, str)
    _update_checked = Signal(dict)

    def __init__(self, display_family: str):
        super().__init__()
        self.cfg = load_config()
        self.game_icon_path = CONFIG_PATH.parent / "game_icon.png"
        self.theme_name = set_palette(str(self.cfg.get("ui_theme", "Graphite")))
        theme_was_removed = self.cfg.get("ui_theme") != self.theme_name
        self.cfg["ui_theme"] = self.theme_name
        if theme_was_removed:
            save_config(self.cfg)
        app = QApplication.instance()
        app.setStyleSheet(build_stylesheet(app.font().family()))
        self.accounts = self._load_accounts()
        sel = self.cfg.get("selected", "all")
        self.select_all = (sel == "all")
        self.selected_ids = set() if self.select_all else set(sel)
        self.flags = {
            "kick_detect": bool(self.cfg.get("kickdetect_on", False)),
            "diff_server": bool(self.cfg.get("diffserver_on", False)),
            "detect_open": bool(self.cfg.get("detect_open", False)),
            "auto_relaunch": bool(self.cfg.get("auto_relaunch", True)),
            "join_server": bool(self.cfg.get("joinserver_on", False)),
        }
        self.engine: MonitorEngine | None = None
        self._session_start = None
        dc = self.cfg.get("discord", {}) or {}
        self.webhook_url = ""
        if isinstance(dc, dict) and dc.get("url_enc") and IS_WINDOWS:
            try:
                self.webhook_url = dpapi_decrypt(dc["url_enc"])
            except Exception:
                self.webhook_url = ""
        self._join_checked.connect(self._on_join_checked)
        self._verified_join = ("", "", "", "")
        self._picker = None
        self._update_checked.connect(self._on_update_state)
        self._updating = False
        self._pending_restart = False

        self.setWindowTitle("Roblox Rejoiner")
        self.setWindowFlags(
            Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint | Qt.WindowCloseButtonHint
        )
        self.setFixedSize(1100, 720)

        root = QFrame(self)
        root.setObjectName("Root")
        self._root = root
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)

        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        items = [("setup", "Rejoiner Setup"), ("accounts", "Manage Accounts"),
                 ("monitor", "Account Monitor"), ("roblox", "Settings")]
        self.nav = TopNavBar(display_family, items)
        self.nav.navigated.connect(self._navigate)
        root_layout.addWidget(self.nav)
        top_rule = QFrame(root)
        top_rule.setObjectName("HeaderRule")
        top_rule.setFixedHeight(1)
        root_layout.addWidget(top_rule)

        content = QWidget()
        clay = QVBoxLayout(content)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(0)
        self.stack = AnimatedStack()
        self.setup_page = SetupPage(self)
        self.accounts_page = AccountsPage(self)
        self.monitor_page = MonitorPage(self)
        self.settings_page = RobloxSettingsPage(self)
        for p in (self.setup_page, self.accounts_page, self.monitor_page,
                  self.settings_page):
            self.stack.addPage(p)
        clay.addWidget(self.stack, 1)
        root_layout.addWidget(content, 1)

        self.nav.setCurrent(0, animate=False)
        self.nav.set_status("Idle", False)

        # Cached state exchanged with the optional Discord bot.
        self._cmd_results: dict = {}
        self._recent_log: list = []
        self._last_rows: list = []

        # Persisted totals displayed across monitoring sessions.
        self._prev_rows: dict = {}
        self._stats_flush = 0

        # Background lookups for account names, avatars, and presence.
        self._profiles = ProfileFetcher(self)
        self._profiles.fetched.connect(self._on_profile)
        self._presence = PresenceFetcher(self)
        self._presence.fetched.connect(self._on_presence)

        # Refresh the session timer and account uptime once per second.
        self._uptime_timer = QTimer(self)
        self._uptime_timer.setInterval(1000)
        self._uptime_timer.timeout.connect(self._tick_uptime)
        self._uptime_timer.start()

        # Publish bot state and process queued commands once per second.
        self._bridge_timer = QTimer(self)
        self._bridge_timer.setInterval(1000)
        self._bridge_timer.timeout.connect(self._bridge_tick)
        self._bridge_timer.start()

        # Refresh all account presence in one request each minute.
        self._presence_timer = QTimer(self)
        self._presence_timer.setInterval(60000)
        self._presence_timer.timeout.connect(self._fetch_presence)
        self._presence_timer.start()
        QTimer.singleShot(400, self._fetch_all_profiles)
        QTimer.singleShot(1200, self._fetch_presence)

        # Self-update: clean leftovers, check shortly after launch and every
        # UPDATE_CHECK_SECS. Download + swap are automatic; the restart waits
        # for the app to be idle (or the user clicks "Restart & update").
        target = updater.exe_path()
        if target:
            try:
                updater.cleanup_stale(target)
            except Exception:
                pass
            QTimer.singleShot(10000, lambda: self.check_updates(manual=False))
            self._update_timer = QTimer(self)
            self._update_timer.setInterval(UPDATE_CHECK_SECS * 1000)
            self._update_timer.timeout.connect(
                lambda: self.check_updates(manual=False))
            self._update_timer.start()
        else:
            self.settings_page.set_update_status({"state": "source"})

    # Load, save, and update account configuration.
    def _load_accounts(self) -> list[dict]:
        out = []
        for a in self.cfg.get("accounts", []):
            try:
                cookie = (dpapi_decrypt(a["cookie_enc"])
                          if (IS_WINDOWS and a.get("cookie_enc")) else "")
            except Exception:
                cookie = ""
            out.append({
                "user_id": a.get("user_id"),
                "username": a.get("username", "unknown"),
                "cookie": cookie,
                "alias": a.get("alias", ""),
                "place": a.get("place", ""),
                "note": a.get("note", ""),
                "delay": a.get("delay", ""),
                "join_on": a.get("join_on", True),
            })
        return out

    def persist(self):
        accs = []
        for a in self.accounts:
            enc = ""
            try:
                if a.get("cookie") and IS_WINDOWS:
                    enc = dpapi_encrypt(a["cookie"])
            except Exception:
                enc = ""
            accs.append({
                "user_id": a["user_id"], "username": a["username"],
                "cookie_enc": enc, "alias": a.get("alias", ""),
                "place": a.get("place", ""), "note": a.get("note", ""),
                "delay": a.get("delay", ""),
                "join_on": a.get("join_on", True),
            })
        self.cfg["accounts"] = accs
        self.cfg["selected"] = "all" if self.select_all else list(self.selected_ids)
        save_config(self.cfg)

    def is_monitored(self, uid) -> bool:
        return self.select_all or uid in self.selected_ids

    def _all_ids(self) -> set:
        return {a["user_id"] for a in self.accounts}

    def set_monitored(self, uid, v: bool):
        if self.select_all:
            self.select_all = False
            self.selected_ids = self._all_ids()
        if v:
            self.selected_ids.add(uid)
        else:
            self.selected_ids.discard(uid)
        if self.selected_ids == self._all_ids() and self.accounts:
            self.select_all = True
        self.persist()
        self.accounts_page.refresh_switches()
        self._sync_join_page()
        # Apply account-selection changes without restarting the engine.
        if self.engine and self.engine.isRunning():
            if v:
                acc = next((a for a in self.accounts
                            if a["user_id"] == uid), None)
                if acc and acc.get("cookie"):
                    resolved = self._resolve_one(acc)
                    if resolved:
                        self.engine.add_account(resolved)
            else:
                self.engine.drop_account(uid)

    def set_select_all(self, v: bool):
        self.select_all = bool(v)
        self.selected_ids = set() if v else set()
        self.persist()
        self.accounts_page.refresh_switches()
        self._sync_join_page()
        if self.engine and self.engine.isRunning():
            if v:
                for a in self.accounts:
                    if a.get("cookie"):
                        r = self._resolve_one(a)
                        if r:
                            self.engine.add_account(r)
            else:
                for a in self.accounts:
                    self.engine.drop_account(a["user_id"])

    def add_account(self, uid, uname, cookie, alias="", note="") -> bool:
        if any(a["user_id"] == uid for a in self.accounts):
            return False
        self.accounts.append({
            "user_id": uid, "username": uname, "cookie": cookie,
            "alias": alias, "place": "", "note": note,
            "delay": "", "join_on": True,
        })
        self.persist()
        self._sync_join_page()
        try:
            self._profiles.fetch(uid)
            self._fetch_presence()
        except Exception:
            pass
        return True

    def remove_account(self, uid):
        self.accounts = [a for a in self.accounts if a["user_id"] != uid]
        self.selected_ids.discard(uid)
        self.persist()
        self.accounts_page.rebuild()
        self._sync_join_page()

    def set_setting(self, key: str, value: str):
        self.cfg[key] = value
        save_config(self.cfg)
        if key in ("joinserver_url", "joinserver_on"):
            try:
                self.setup_page.refresh_join_status()
            except (AttributeError, RuntimeError):
                pass

    def set_default_place(self, value: str):
        value = (parse_place_id(value) or value.strip())
        if value != str(self.cfg.get("place", "") or ""):
            self.cfg.pop("game_universe_id", None)
            self.cfg.pop("game_name", None)
        self.cfg["place"] = value
        save_config(self.cfg)

    def select_game(self, game: GameSearchResult):
        icon_data = game.get("icon_data")
        if not isinstance(icon_data, bytes) or not icon_data:
            raise ValueError("Roblox did not provide an icon for this game.")
        pixmap = QPixmap()
        if not pixmap.loadFromData(icon_data, "PNG"):
            raise ValueError("Roblox returned an invalid game icon.")
        self.game_icon_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.game_icon_path.with_suffix(".tmp")
        try:
            temp_path.write_bytes(icon_data)
            os.replace(temp_path, self.game_icon_path)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.cfg["place"] = game["root_place_id"]
        self.cfg["game_universe_id"] = game["universe_id"]
        self.cfg["game_name"] = game["name"]
        save_config(self.cfg)

    def set_theme(self, name: str):
        self.theme_name = set_palette(name)
        self.cfg["ui_theme"] = self.theme_name
        save_config(self.cfg)
        app = QApplication.instance()
        app.setStyleSheet(build_stylesheet(app.font().family()))
        self.nav.apply_theme()
        for widget in self.findChildren(QWidget):
            widget.update()

    def set_webhook(self, url: str):
        url = (url or "").strip()
        self.webhook_url = url if _is_discord_webhook(url) else ""
        dc = self.cfg.get("discord", {})
        if not isinstance(dc, dict):
            dc = {}
        enc = ""
        if self.webhook_url and IS_WINDOWS:
            try:
                enc = dpapi_encrypt(self.webhook_url)
            except Exception:
                enc = ""
        dc["url_enc"] = enc
        dc.setdefault("username", "Roblox Rejoiner")
        dc.setdefault("avatar", "")
        self.cfg["discord"] = dc
        if self.engine:
            self.engine.webhook = self.webhook_url
        save_config(self.cfg)

    def set_flag(self, cfg_key: str, flag: str, value: bool):
        self.cfg[cfg_key] = bool(value)
        self.flags[flag] = bool(value)        # shared dict — engine sees it live
        save_config(self.cfg)
        if flag == "join_server":
            try:
                self.setup_page.refresh_join_status()
            except (AttributeError, RuntimeError):
                pass
            if self.engine and self.engine.isRunning():
                # The link budget is decided when an account is resolved, so a
                # flip here affects what starts next, not what is running.
                self.monitor_page.on_log(
                    "Join specific server turned "
                    + ("on - newly ticked accounts use the link "
                       "(Stop and Start to re-apply it to running ones)."
                       if value else "off - accounts use their own games."))

    # Start, stop, and update the multi-account monitoring engine.
    def _selected_accounts(self) -> list[dict]:
        if self.select_all:
            return [a for a in self.accounts if a.get("cookie")]
        return [a for a in self.accounts
                if a["user_id"] in self.selected_ids and a.get("cookie")]

    def _join_link_for(self, a: dict) -> str:
        """This account's private-server link, or "" when it doesn't use one.

        An account joins the private server when it is ticked on the Rejoiner
        Setup list or on its own row; everyone else keeps their own game. That
        is an explicit choice, never a silent fallback from a failed link."""
        if not self.flags.get("join_server"):
            return ""
        if a.get("join_on", True) is False:
            return ""
        return str(self.cfg.get("joinserver_url", "") or "").strip()

    def _resolve_one(self, a: dict):
        """One account resolved for the engine.

        The account is sent to the private server only when it was ticked for
        it; everyone else launches into the game it was set up with."""
        default_place = parse_place_id(self.cfg.get("place", "") or "")
        rjoin = self._join_link_for(a)
        pid = parse_place_id(a.get("place")) or default_place
        if not pid and not rjoin:
            self.monitor_page.on_log(
                f"{a['username']} has no place and no default — skipping.")
            return None
        try:
            d = max(0, min(3600, int(a.get("delay") or "")))
        except (ValueError, TypeError):
            d = int(self.cfg.get("delay") or 60)
        runiverse = (str(self.cfg.get("game_universe_id") or "")
                     if not str(a.get("place") or "").strip() else "")
        return {**a, "rplace": pid or "", "rdelay": d,
                "runiverse": runiverse,
                "rjoin": rjoin,
                "custom_delay": bool(str(a.get("delay") or "").strip())}

    def _resolve(self) -> list[dict]:
        out = []
        for a in self._selected_accounts():
            r = self._resolve_one(a)
            if r:
                out.append(r)
        return out

    def _roblox_exe(self) -> str:
        """Resolved RobloxPlayerBeta.exe override, or '' to use the default
        Windows launcher (protocol handler)."""
        d = str(self.cfg.get("roblox_dir", "") or "").strip()
        if d:
            exe = os.path.join(d, "RobloxPlayerBeta.exe")
            if os.path.exists(exe):
                return exe
        return ""

    def toggle_monitoring(self):
        if self.engine and self.engine.isRunning():
            self.monitor_page.on_log("Stopping…")
            eng = self.engine
            self.engine = None            # guards drop any late signals at once
            try:
                eng.log.disconnect(self._log_sink)
                eng.state.disconnect(self._state_sink)
                eng.status.disconnect(self._status_sink)
            except (RuntimeError, TypeError):
                pass
            eng.stop()
            eng.wait(4000)
            self._session_start = None
            self._last_rows = []
            self._prev_rows = {}
            self._status_sink("Idle", False)
            self.monitor_page.gray_out()
            save_config(self.cfg)
            return
        resolved = self._resolve()
        if not resolved:
            self.monitor_page.on_log(
                "Nothing to launch — add an account on Manage Accounts, tick it, "
                "and set a place on Rejoiner Setup.")
            self._navigate(1)
            return
        icon_path = ""
        try:
            if self.game_icon_path.exists():
                icon_path = str(self.game_icon_path)
        except OSError:
            icon_path = ""
        self.engine = MonitorEngine(resolved, self.flags, webhook=self.webhook_url,
                                    join_url=self.cfg.get("joinserver_url", ""),
                                    roblox_exe=self._roblox_exe(),
                                    app_icon=_app_icon_path())
        self.engine.log.connect(self._log_sink)
        self.engine.state.connect(self._state_sink)
        self.engine.status.connect(self._status_sink)
        import time
        self._session_start = time.time()
        self.engine.start()

    def kill_all(self):
        if self.engine:
            self.engine.kill_all()
        else:
            from roblox_windows import kill_all_roblox
            kill_all_roblox()
        self.monitor_page.on_log("Closed all Roblox clients.")

    def kill_account(self, uid):
        if self.engine and self.engine.isRunning():
            self.engine.request_kill(uid)

    # Apply account profile and presence results to the UI.
    def _fetch_all_profiles(self):
        try:
            uids = [a["user_id"] for a in self.accounts]
            if uids:
                self._profiles.fetch_batch(uids)
        except Exception:
            pass

    def _on_profile(self, uid: int, display: str, path: str):
        for a in self.accounts:
            if a.get("user_id") == uid and display:
                a["display"] = display
                break
        row = self.accounts_page._rows.get(uid)
        if row:
            row.set_profile(display, path)

    def _fetch_presence(self):
        cookie = next((a.get("cookie") for a in self.accounts
                       if a.get("cookie")), None)
        uids = [a["user_id"] for a in self.accounts]
        if cookie and uids:
            self._presence.fetch(uids, cookie)

    def _on_presence(self, mapping: dict):
        for uid, state in mapping.items():
            try:
                key = int(uid)
            except (TypeError, ValueError):
                continue
            row = self.accounts_page._rows.get(key)
            if row:
                row.set_presence(state)

    # Read and persist lifetime successful-rejoin totals.
    def _stats(self) -> dict:
        st = self.cfg.get("stats")
        if not isinstance(st, dict):
            st = {}
            self.cfg["stats"] = st
        return st

    def lifetime(self, uid) -> dict:
        d = self._stats().get(str(uid)) or {}
        return {"runtime": int(d.get("runtime", 0) or 0),
                "launches": int(d.get("launches", 0) or 0),
                "crashes": int(d.get("crashes", 0) or 0)}

    def _bump(self, uid, key, n: int = 1):
        if uid is None:
            return
        d = self._stats().setdefault(
            str(uid), {"runtime": 0, "launches": 0, "crashes": 0})
        d[key] = int(d.get(key, 0) or 0) + n

    def lifetime_str(self, uid) -> str:
        d = self.lifetime(uid)
        if not (d["runtime"] or d["launches"] or d["crashes"]):
            return "not farmed yet"
        return f"{_fmt_hms(d['runtime'])} farmed · {d['launches']} launches"

    # Validate a Roblox share or private-server link.
    # Validate a Roblox share or private-server link.
    def check_join_link(self, url: str):
        """Resolve the link and report exactly where it would send accounts."""
        url = (url or "").strip()
        st = self.setup_page.join_status
        parsed = parse_join_link(url)
        if not parsed:
            st.setText(
                "Paste a valid share / private-server link: the link Roblox "
                "gives you on your private server, "
                "https://www.roblox.com/share?code=...&type=Server, or "
                "https://www.roblox.com/games/<id>/<name>"
                "?privateServerLinkCode=...")
            st.setStyleSheet(f"color:{WARN};")
            return
        cookie = next((a["cookie"] for a in self.accounts if a.get("cookie")),
                      None)
        if not cookie:
            st.setText("Add an account first (its cookie resolves the link).")
            st.setStyleSheet(f"color:{WARN};")
            return
        self.set_setting("joinserver_url", url)
        st.setText("Checking the link...")
        st.setStyleSheet(f"color:{ACCENT_SOFT};")

        def work():
            try:
                target = resolve_join_link(cookie, url)
            except Exception as e:
                target = JoinTarget(error=f"resolve failed ({e})")
            self._join_checked.emit(
                target.kind, target.place or "", target.link_code or "",
                target.job_id or "", target.error or "")

        threading.Thread(target=work, daemon=True).start()

    # Private-server picker: choose a server the account already owns.
    def open_private_server_picker(self):
        """Open the private-server browser for a chosen account."""
        if not any(a.get("cookie") for a in self.accounts):
            self.setup_page.join_status.setText(
                "Add an account first - the private servers it owns are listed.")
            self.setup_page.join_status.setStyleSheet(f"color:{WARN};")
            return
        if self._picker is None:
            self._picker = PrivateServerDialog(self)
        self._picker.show()
        self._picker.raise_()
        self._picker.load()

    def apply_join_link(self, link: str, label: str = ""):
        """Fill the join link in from the private-server picker."""
        link = (link or "").strip()
        if not link:
            return
        self.set_setting("joinserver_url", link)
        self.setup_page.join_edit.setText(link)
        self._navigate(0)
        st = self.setup_page.join_status
        where = f" ({label})" if label else ""
        st.setText(f"Using {where or 'that private server'}: {link} - press "
                   f"Check link to confirm it resolves.")
        st.setStyleSheet(f"color:{GOOD};")

    def _on_join_checked(self, kind: str, place: str, link_code: str,
                         job: str, error: str):
        st = self.setup_page.join_status
        if error:
            st.setText(f"That link did not resolve: {error}. Accounts wait "
                       f"instead of being sent to a public server.")
            st.setStyleSheet(f"color:{BAD};")
            return
        if kind == "private":
            what = ("private server - place " + place
                    + (f" - server {job[:12]}..." if job else ""))
        elif kind == "job":
            what = f"one specific server - place {place} - server {job[:12]}..."
        else:
            what = f"that game - place {place} (Roblox picks any server)"
        chosen = [a for a in self.accounts
                  if a.get("cookie") and a.get("join_on", True) is not False]
        names = ", ".join(a.get("alias") or a.get("username", "?")
                          for a in chosen[:6])
        more = "" if len(chosen) <= 6 else f" (+{len(chosen) - 6} more)"
        st.setText(f"Send {names or 'no account'}{more} to {what}.")
        st.setStyleSheet(f"color:{GOOD};")
        self._verified_join = (kind, place, link_code, job)

    # Forward engine updates to the UI and optional bot bridge.
    def _log_sink(self, msg: str):
        self._recent_log.append(msg)
        if len(self._recent_log) > 60:
            self._recent_log = self._recent_log[-60:]
        self.monitor_page.on_log(msg)

    def _state_sink(self, rows: list):
        if self.engine is None:           # ignore late emissions after stop
            return
        self._last_rows = rows
        for r in rows:
            uid = r.get("user_id")
            st = r.get("state")
            cr = int(r.get("crashes", 0) or 0)
            prev = self._prev_rows.get(uid)
            if prev is None:
                if st == "running":
                    self._bump(uid, "launches", 1)   # adopted/already in
            else:
                if prev[0] != "running" and st == "running":
                    self._bump(uid, "launches", 1)
                if cr > prev[1]:
                    self._bump(uid, "crashes", cr - prev[1])
            self._prev_rows[uid] = (st, cr)
        self.monitor_page.on_state(rows)

    def _status_sink(self, text: str, running: bool):
        if self.engine is None and running:   # ignore late "running" after stop
            return
        self.monitor_page.on_status(text, running)
        self.nav.set_status(text, running)
        # A downloaded update waiting for the app to go idle.
        if (not running and self._pending_restart
                and not (self.engine and self.engine.isRunning())):
            self._pending_restart = False
            self.monitor_page.on_log("Update ready — restarting in 2s.")
            QTimer.singleShot(2000, self.restart_for_update)

    # Exchange status and commands with the optional Discord bot.
    def _bridge_tick(self):
        try:
            qt_bridge.publish_state(self)
            qt_bridge.process_commands(self)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Self-update from GitHub releases (standalone exe only).
    # ------------------------------------------------------------------
    def check_updates(self, manual: bool = False):
        """Check GitHub for a newer release; download and swap it in when
        found. Emits state through _update_checked for the Setup page."""
        target = updater.exe_path()
        if not target:
            self.settings_page.set_update_status({"state": "source"})
            return
        if self._updating:
            return
        self._updating = True
        if manual:
            self.settings_page.set_update_status({"state": "checking"})

        def work():
            final: dict = {}
            try:
                rel = updater.fetch_latest()
                if not rel:
                    final = {"state": "error",
                             "error": "couldn't reach GitHub releases "
                             "(no published exe asset found)"}
                elif not updater.is_newer(rel["tag"]):
                    final = {"state": "uptodate", "tag": rel["tag"]}
                else:
                    self._update_checked.emit({"state": "available",
                                               "tag": rel["tag"]})
                    dest = target + ".update"

                    def progress(pct):
                        self._update_checked.emit({"state": "downloading",
                                                   "tag": rel["tag"],
                                                   "progress": pct})

                    if updater.download(rel["url"], dest, progress):
                        if updater.apply_update(dest, target):
                            final = {"state": "ready", "tag": rel["tag"]}
                        else:
                            final = {"state": "error",
                                     "error": "couldn't replace the exe — "
                                     "is the app running as administrator?"}
                    else:
                        final = {"state": "error", "error": "download failed"}
            except Exception as e:
                final = {"state": "error", "error": str(e)}
            self._updating = False
            self._update_checked.emit(final)

        threading.Thread(target=work, daemon=True).start()

    def _on_update_state(self, info: dict):
        self.settings_page.set_update_status(info)
        if info.get("state") != "ready":
            return
        # Apply automatically: restart now when idle, otherwise right after
        # monitoring stops so an active watch is never interrupted.
        if self.engine and self.engine.isRunning():
            self._pending_restart = True
        else:
            QTimer.singleShot(3000, self.restart_for_update)

    def restart_for_update(self):
        """Launch the freshly swapped exe and exit this instance."""
        target = updater.exe_path()
        if not target:
            return
        self._pending_restart = False
        if self.engine and self.engine.isRunning():
            self.monitor_page.on_log("Updating — stopping the watcher…")
            self.toggle_monitoring()
        updater.launch_new(target)
        QApplication.quit()

    def _crash_for(self, uid) -> int:
        for r in self._last_rows:
            if r.get("user_id") == uid:
                return int(r.get("crashes", 0) or 0)
        return 0

    def account_by_name(self, name):
        n = (name or "").strip().lower()
        for a in self.accounts:
            if (a.get("username") or "").lower() == n:
                return a
        return None

    def bridge_set_flag(self, flag: str, on: bool) -> bool:
        m = {"kick_detect": ("kickdetect_on", self.setup_page, "sw_kick"),
             "diff_server": ("diffserver_on", self.setup_page, "sw_diff"),
             "detect_open": ("detect_open", self.setup_page, "sw_detect"),
             "auto_relaunch": ("auto_relaunch", self.setup_page, "sw_relaunch"),
             "join_server": ("joinserver_on", self.setup_page, "sw_join")}
        if flag not in m:
            return False
        cfg_key, pageobj, sw_attr = m[flag]
        self.cfg[cfg_key] = bool(on)
        self.flags[flag] = bool(on)
        try:
            getattr(pageobj, sw_attr).setChecked(bool(on))
        except (AttributeError, RuntimeError):
            pass
        save_config(self.cfg)
        return True

    def bridge_set_global_delay(self, v: int):
        self.cfg["delay"] = str(v)
        self.setup_page.delay_edit.setText(str(v))
        for a in self.accounts:
            a["delay"] = ""
        self.accounts_page.rebuild()
        save_config(self.cfg)

    def bridge_set_global_place(self, pid: str):
        self.set_default_place(str(pid))
        self.setup_page.place_edit.setText(str(pid))
        self.setup_page._show_saved_game()
        for a in self.accounts:
            a["place"] = ""
        self.accounts_page.rebuild()

    def bridge_set_account_delay(self, uid, v: int):
        a = next((x for x in self.accounts if x["user_id"] == uid), None)
        if a:
            a["delay"] = str(v)
            self.accounts_page.rebuild()
            self.persist()

    def bridge_set_account_place(self, uid, pid: str):
        a = next((x for x in self.accounts if x["user_id"] == uid), None)
        if a:
            a["place"] = str(pid)
            self.accounts_page.rebuild()
            self.persist()

    def bridge_set_join_url(self, url: str):
        self.cfg["joinserver_url"] = url
        self.setup_page.join_edit.setText(url)
        save_config(self.cfg)
        try:
            self.setup_page.refresh_join_status()
        except (AttributeError, RuntimeError):
            pass

    def _sync_join_page(self):
        """Refresh the Setup page's private-server list and summary.

        Safe to call even before the page finishes building, which is what
        makes it usable from account add/remove/toggle handlers."""
        try:
            self.setup_page.refresh_join_accounts()
            self.setup_page.refresh_join_status()
        except (AttributeError, RuntimeError):
            pass

    def set_account_join(self, uid, on: bool):
        """Choose whether one account joins the private server.

        The Setup page list and the account's own row both read this, so they
        can never disagree about where an account is being sent."""
        a = next((x for x in self.accounts if x["user_id"] == uid), None)
        if not a:
            return
        a["join_on"] = bool(on)
        self.persist()
        row = self.accounts_page._rows.get(uid)
        if row is not None:
            row.sw_join.setChecked(bool(on), animate=False)
        self.accounts_page.refresh_stats()
        try:
            self.setup_page.refresh_join_row(uid)
        except (AttributeError, RuntimeError):
            pass
        self._sync_join_page()

    def bridge_set_account_join(self, uid, on: bool):
        """Set or clear the private-server choice for one account."""
        self.set_account_join(uid, on)

    def bridge_set_join_accounts(self, names: list[str], on: bool,
                                 clear_others: bool = False) -> int:
        """Apply one private-server choice to several accounts at once.

        Naming accounts turns them on (or off) and leaves the rest alone;
        `clear_others` is for "everyone" / "the first N", where the accounts
        not named must be switched the other way too."""
        wanted = {n.strip().lower() for n in names if n.strip()}
        if not wanted:
            return 0
        changed = 0
        for a in self.accounts:
            hit = (a.get("username") or "").lower() in wanted
            if on:
                value = True if hit else (False if clear_others else None)
            else:
                value = False if hit else None
            if value is None:
                continue
            if bool(a.get("join_on", True)) != value:
                a["join_on"] = value
                changed += 1
        self.persist()
        self.accounts_page.rebuild()
        self._sync_join_page()
        return changed

    def bridge_watch(self, on: bool):
        running = bool(self.engine and self.engine.isRunning())
        if on and not running:
            self.toggle_monitoring()
        elif (not on) and running:
            self.toggle_monitoring()

    def _tick_uptime(self):
        if self._session_start:
            import time
            self.monitor_page.set_uptime(time.time() - self._session_start)
        if self.engine and self.engine.isRunning():
            for r in self._last_rows:
                if r.get("state") == "running":
                    self._bump(r.get("user_id"), "runtime", 1)
        self._stats_flush += 1
        if self._stats_flush % 20 == 0:
            save_config(self.cfg)
        if self._stats_flush % 3 == 0:
            self.accounts_page.refresh_stats()

    # Handle page navigation and native window behavior.
    def _navigate(self, index: int):
        self.nav.setCurrent(index)
        self.stack.setCurrent(index)

    def closeEvent(self, ev):
        try:
            self._bridge_timer.stop()
        except Exception:
            pass
        try:
            if self.engine and self.engine.isRunning():
                self.engine.stop()
                self.engine.wait(3000)
        except Exception:
            pass
        self.persist()
        super().closeEvent(ev)


def _app_icon_path() -> str:
    """Path to the app's own icon - the badge embeds are branded with."""
    path = _resource("rejoiner_logo.png")
    return path if os.path.exists(path) else ""


def _resource(name: str) -> str:
    base = getattr(
        sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(base, name)
    if not os.path.exists(path) and not hasattr(sys, "_MEIPASS"):
        path = os.path.join(base, "assets", name)
    return path


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Roblox Rejoiner")
    icon_path = _resource("rejoiner_logo.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    display, text = pick_fonts()
    app.setFont(QFont(text, 10))
    app.setStyleSheet(build_stylesheet(text))
    win = MainWindow(display)
    if os.path.exists(icon_path):
        win.setWindowIcon(QIcon(icon_path))
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
