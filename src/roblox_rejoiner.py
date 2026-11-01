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

from rejoiner_constants import IS_WINDOWS
from rejoiner_constants import CONFIG_PATH
from rejoiner_colors import (fmt_dur, ACCENT, ACCENT_SOFT, GOOD, WARN, BAD, MUTED, BG,
                      TEXT, CARD2)
from account_storage import load_config, save_config, dpapi_encrypt, dpapi_decrypt
from roblox_account_profiles import ProfileFetcher, PresenceFetcher
from roblox_api import (get_account_info, parse_place_id, parse_share_link,
                       resolve_share_link, search_roblox_games,
                       fetch_game_icons, GameSearchResult)
from discord_notifications import _is_discord_webhook, discord_send

import account_command_bridge as qt_bridge

from rejoiner_theme import pick_fonts, build_stylesheet, THEMES, set_theme as set_palette, BG
from rejoiner_widgets import (IOSSwitch, TopNavBar, AnimatedStack, Avatar,
                        StatTile, StatusDot, make_card, hsep)
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
    _webhook_test_complete = Signal(object)

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

        # Optional share link used to send monitored accounts to one server.
        self.join_edit = QLineEdit(cfg.get("joinserver_url", ""))
        self.join_edit.setPlaceholderText("Roblox share / private-server link")
        self.join_edit.editingFinished.connect(
            lambda: win.set_setting("joinserver_url", self.join_edit.text().strip()))
        self.sw_join = self._switch("joinserver_on")
        check_btn = QPushButton("Check link")
        check_btn.setObjectName("Ghost")
        check_btn.setCursor(Qt.PointingHandCursor)
        check_btn.clicked.connect(
            lambda: win.check_join_link(self.join_edit.text().strip()))
        link_row = QWidget()
        lr = QHBoxLayout(link_row)
        lr.setContentsMargins(16, 11, 16, 11)
        lr.setSpacing(10)
        lr.addWidget(self.join_edit, 1)
        lr.addWidget(check_btn)
        body.addWidget(_section("Join a specific server"))
        body.addWidget(_card_of([
            _row("Join specific server", "Send every account to the same server "
                 "from the link below", self.sw_join),
            link_row,
        ]))
        self.join_status = QLabel("")
        self.join_status.setObjectName("RowSub")
        self.join_status.setContentsMargins(18, 0, 0, 0)
        body.addWidget(self.join_status)

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

        self.webhook_edit = QLineEdit(win.webhook_url)
        self.webhook_edit.setPlaceholderText("https://discord.com/api/webhooks/…")
        self.webhook_edit.setMinimumWidth(300)
        self.webhook_edit.editingFinished.connect(
            lambda: win.set_webhook(self.webhook_edit.text().strip()))
        self.webhook_test_button = QPushButton("Test webhook")
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
        body.addWidget(_section("Notifications"))
        body.addWidget(_card_of([
            _row("Discord webhook", "Posts launches, crashes, kicks and kills",
                 webhook_row),
        ]))
        self.webhook_test_status = QLabel("")
        self.webhook_test_status.setObjectName("RowSub")
        self.webhook_test_status.setWordWrap(True)
        self.webhook_test_status.setContentsMargins(18, 0, 0, 0)
        body.addWidget(self.webhook_test_status)

        body.addStretch(1)

    def test_webhook(self):
        url = self.webhook_edit.text().strip()
        if not _is_discord_webhook(url):
            self.webhook_test_status.setText(
                "Enter a valid Discord webhook URL before testing.")
            self.webhook_test_status.setVisible(True)
            return

        self.win.set_webhook(url)
        dc = self.win.cfg.get("discord", {})
        username = dc.get("username", "Roblox Rejoiner")
        avatar = dc.get("avatar", "")
        self.webhook_test_button.setEnabled(False)
        self.webhook_test_button.setText("Sending…")
        self.webhook_test_status.setText("Sending test message to Discord…")
        self.webhook_test_status.setVisible(True)

        def send_test():
            try:
                result = discord_send(
                    url, username, avatar,
                    "Test notification from Roblox Rejoiner.")
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
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        cfg = win.cfg
        page, body = _page_scaffold(
            "Roblox Settings",
            "Choose which Roblox installation to use when launching clients.")
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
        body.addStretch(1)
        QTimer.singleShot(150, self._rescan)

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

    def set_monitored_silent(self, v: bool):
        self.sw.setChecked(v, animate=True)


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
        self.dot.set_color(col)
        label = {"running": "running", "launching": "launching",
                 "leaving": "leaving", "waiting": "rejoining",
                 "stopped": "stopped"}.get(s, s)
        cr = st.get("crashes", 0)
        self.state.setText(f"{label}   ·   {cr} crash" + ("es" if cr != 1 else ""))
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

        # Session totals for active clients, successful rejoins, and uptime.
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tile_running = StatTile("RUNNING", "0")
        self.tile_rejoins = StatTile("SUCCESSFULLY REJOINED", "0")
        self.tile_uptime = StatTile("SESSION", "0:00")
        tiles.addWidget(self.tile_running)
        tiles.addWidget(self.tile_rejoins)
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
        for uid in list(self._rows):
            if uid not in seen:
                w = self._rows.pop(uid)
                w.setParent(None)
        self.tile_running.set_value(str(running))
        self.tile_rejoins.set_value(str(successful_rejoins))

    @staticmethod
    def _badge_for(st):
        if st.get("state") == "waiting" and st.get("custom_delay"):
            return "\u21bb " + fmt_dur(st.get("rejoin_in", 0))
        return ""

    def gray_out(self):
        for r in self._rows.values():
            r.set_idle()
        self.tile_running.set_value("0")

    def on_status(self, text: str, running: bool):
        self.status_lbl.setText(text)
        self.status_dot.set_color(GOOD if running else MUTED)
        self.start_btn.setText("Stop" if running else "Start")

    def set_uptime(self, secs: float):
        self.tile_uptime.set_value(fmt_dur(secs))


# Main application window and page coordinator.
class MainWindow(QWidget):
    _join_checked = Signal(bool, str, bool)

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
                 ("monitor", "Account Monitor"), ("roblox", "Roblox Settings")]
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
        self.roblox_page = RobloxSettingsPage(self)
        for p in (self.setup_page, self.accounts_page, self.monitor_page,
                  self.roblox_page):
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
            "delay": "",
        })
        self.persist()
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

    def set_setting(self, key: str, value: str):
        self.cfg[key] = value
        save_config(self.cfg)

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

    # Start, stop, and update the multi-account monitoring engine.
    def _selected_accounts(self) -> list[dict]:
        if self.select_all:
            return [a for a in self.accounts if a.get("cookie")]
        return [a for a in self.accounts
                if a["user_id"] in self.selected_ids and a.get("cookie")]

    def _resolve_one(self, a: dict):
        default_place = parse_place_id(self.cfg.get("place", "") or "")
        join_on = bool(self.flags.get("join_server"))
        pid = parse_place_id(a.get("place")) or default_place
        if not pid and not join_on:
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
        self.engine = MonitorEngine(resolved, self.flags, webhook=self.webhook_url,
                                    join_url=self.cfg.get("joinserver_url", ""),
                                    roblox_exe=self._roblox_exe())
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
            row = self.accounts_page._rows.get(int(uid))
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
    def check_join_link(self, url: str):
        url = (url or "").strip()
        st = self.setup_page.join_status
        code, ltype = parse_share_link(url)
        if not code:
            st.setText("Paste a valid share / private-server link.")
            st.setStyleSheet(f"color:{WARN};")
            return
        cookie = next((a["cookie"] for a in self.accounts if a.get("cookie")), None)
        if not cookie:
            st.setText("Add an account first (its cookie resolves the link).")
            st.setStyleSheet(f"color:{WARN};")
            return
        self.set_setting("joinserver_url", url)
        st.setText("Checking…")
        st.setStyleSheet(f"color:{ACCENT_SOFT};")

        def work():
            try:
                place, inst = resolve_share_link(cookie, code, ltype)
            except Exception:
                place, inst = None, None
            self._join_checked.emit(bool(place), place or "", bool(inst))

        threading.Thread(target=work, daemon=True).start()

    def _on_join_checked(self, ok: bool, place: str, specific: bool):
        st = self.setup_page.join_status
        if ok:
            st.setText(f"\u2713 place {place}"
                       + (" \u00b7 specific server" if specific
                          else " \u00b7 open server"))
            st.setStyleSheet(f"color:{GOOD};")
        else:
            st.setText("Couldn't resolve that link.")
            st.setStyleSheet(f"color:{BAD};")

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

    # Exchange status and commands with the optional Discord bot.
    def _bridge_tick(self):
        try:
            qt_bridge.publish_state(self)
            qt_bridge.process_commands(self)
        except Exception:
            pass

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
