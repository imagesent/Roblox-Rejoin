"""File-based command bridge between Roblox Rejoiner and its optional bot.

The app publishes settings and session snapshots, reads queued bot commands,
and writes command results. The bridge contains account metadata and process
state only; session cookies are never written to these files.

MainWindow polls this module once per second. The monitoring engine writes the
live session map because it owns the process and account state.
"""

from __future__ import annotations

import json
import time

from rejoiner_constants import STATE_PATH, RESULTS_PATH
from account_storage import _atomic_write, read_remote_commands
from roblox_api import parse_place_id, parse_share_link


def publish_state(win) -> None:
    now = time.time()
    running = bool(win.engine and win.engine.isRunning())
    accounts = []
    for a in win.accounts:
        if not a.get("cookie"):
            continue
        uid = a["user_id"]
        lt = win.lifetime(uid)
        row = next((r for r in win._last_rows
                    if r.get("user_id") == uid), {})
        accounts.append({
            "user_id": uid,
            "username": a["username"],
            "display": a.get("display", "") or "",
            "monitored": bool(win.is_monitored(uid)),
            "rejoin_delay": a.get("delay", "") or "",
            "place": a.get("place", "") or "",
            "crashes": win._crash_for(uid),
            "state": (row.get("state") if running else "idle") or "idle",
            "uptime": int(row.get("uptime", 0) or 0),
            "lifetime_runtime": lt["runtime"],
            "lifetime_launches": lt["launches"],
            "lifetime_crashes": lt["crashes"],
        })
    running = bool(win.engine and win.engine.isRunning())
    state = {
        "updated": now,
        "running": running,
        "kick_detect": bool(win.flags.get("kick_detect")),
        "detect_open": bool(win.flags.get("detect_open")),
        "join_server": bool(win.flags.get("join_server")),
        "diff_server": bool(win.flags.get("diff_server")),
        "join_server_url": str(win.cfg.get("joinserver_url", "") or ""),
        "auto_relaunch": bool(win.flags.get("auto_relaunch", True)),
        "global_rejoin_delay": str(win.cfg.get("delay", "60") or "60"),
        "default_place": str(win.cfg.get("place", "") or ""),
        "accounts": accounts,
        "recent_log": win._recent_log[-15:],
    }
    try:
        _atomic_write(STATE_PATH, json.dumps(state))
    except Exception:
        pass


def process_commands(win) -> None:
    cmds = read_remote_commands()
    if not cmds:
        return
    changed = False
    for cmd in cmds:
        ok, msg = apply_command(win, cmd)
        cid = cmd.get("id")
        if cid:
            win._cmd_results[cid] = {"ok": ok, "message": msg, "ts": time.time()}
        win._log_sink(f"[bot] {cmd.get('action', '?')}: {msg}")
        changed = True
    if changed:
        cutoff = time.time() - 120
        win._cmd_results = {k: v for k, v in win._cmd_results.items()
                            if v.get("ts", 0) > cutoff}
        try:
            _atomic_write(RESULTS_PATH, json.dumps(win._cmd_results))
        except Exception:
            pass


def _truthy(v) -> bool:
    return str(v).lower() in ("on", "true", "1", "yes", "start")


def apply_command(win, cmd) -> tuple[bool, str]:
    action = cmd.get("action")
    user = cmd.get("username")
    value = cmd.get("value")

    flag_actions = {"kick_detect": "kick_detect", "diff_server": "diff_server",
                    "detect_open": "detect_open", "auto_relaunch": "auto_relaunch"}
    if action in flag_actions:
        flag = flag_actions[action]
        want = _truthy(value)
        if bool(win.flags.get(flag)) == want:
            return True, f"already {'on' if want else 'off'}"
        win.bridge_set_flag(flag, want)
        return True, f"turned {'on' if want else 'off'}"

    if action == "join_server":
        v = str(value or "").strip()
        if v.lower() in ("off", "false", "0", "no", ""):
            win.bridge_set_flag("join_server", False)
            return True, "join-server off"
        if v.lower() in ("on", "true", "1", "yes"):
            win.bridge_set_flag("join_server", True)
            return True, "join-server on (using saved link)"
        code, _ = parse_share_link(v)
        if not code:
            return False, "that isn't a valid share / private-server link"
        win.bridge_set_join_url(v)
        win.bridge_set_flag("join_server", True)
        return True, "join-server set to that link"

    if action == "account":
        a = win.account_by_name(user)
        if not a:
            return False, f"no account '{user}'"
        want = _truthy(value)
        win.set_monitored(a["user_id"], want)
        return True, f"{a['username']} turned {'on' if want else 'off'}"

    if action == "set_rejoin_delay":
        try:
            v = max(0, min(3600, int(value)))
        except (TypeError, ValueError):
            return False, f"'{value}' isn't a valid number of seconds"
        if user:
            a = win.account_by_name(user)
            if not a:
                return False, f"no account '{user}'"
            win.bridge_set_account_delay(a["user_id"], v)
            return True, f"{a['username']} rejoin delay = {v}s"
        win.bridge_set_global_delay(v)
        return True, f"global rejoin delay = {v}s (overrides cleared)"

    if action == "set_place":
        pid = parse_place_id(str(value))
        if not pid:
            return False, f"'{value}' isn't a valid place ID or game link"
        if user:
            a = win.account_by_name(user)
            if not a:
                return False, f"no account '{user}'"
            win.bridge_set_account_place(a["user_id"], pid)
            return True, f"{a['username']} game = {pid} (applies on next rejoin)"
        win.bridge_set_global_place(pid)
        return True, f"global game = {pid} (overrides cleared)"

    if action == "watch":
        want = _truthy(value)
        running = bool(win.engine and win.engine.isRunning())
        if want and not running:
            win.bridge_watch(True)
            ok = bool(win.engine and win.engine.isRunning())
            return ok, ("started watching" if ok else
                        "couldn't start — check accounts are ticked and a place is set")
        if (not want) and running:
            win.bridge_watch(False)
            return True, "stopped watching"
        return True, f"already {'running' if running else 'stopped'}"

    if action == "rejoin":
        a = win.account_by_name(user)
        if not a:
            return False, f"no account '{user}'"
        if not (win.engine and win.engine.isRunning()):
            return False, "not watching — start the watcher first"
        win.kill_account(a["user_id"])
        return True, f"restarting {a['username']}"

    return False, f"unknown action '{action}'"
