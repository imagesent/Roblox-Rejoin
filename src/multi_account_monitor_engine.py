"""Independent account monitoring and relaunch engine.

The engine runs in a QThread and reports status through Qt signals. It
serializes and staggers launches so process-to-account matching stays
reliable, and it detects essentially every way an account can be removed
from an instance, then rejoins each affected account independently:

- client process exited (crash, forced close, Roblox update restart)
- native kick/disconnect dialog open on the client ("Disconnected",
  "Error Code: 267", moderation messages — the code is read from the
  dialog and reported)
- client window hung (frozen, not pumping messages)
- client open but Roblox presence says the account left the game
  (kick / disconnect / server shutdown with the dialog dismissed)
- presence unreachable for a sustained period (connection loss)
- client never reaches the game (stuck on the loading screen)
- account landed in a different experience than configured
- account landed in a different server than the requested private
  server (join-link verification)

The UI can update the shared behavior flags while monitoring. Changing
which accounts are selected restarts the engine in the UI.
"""

from __future__ import annotations

import queue
import random
import threading
import time

from PySide6.QtCore import QThread, Signal

from rejoiner_constants import (LAUNCH_STAGGER, PRESENCE_SETTLE, KICK_GRACE,
                      KICK_POLL, LEAVE_POLL, LEAVE_MAX_WAIT, PRESENCE_INTERVAL,
                      PRESENCE_FAIL_LIMIT, PRESENCE_FAIL_TIME, HANG_GRACE,
                      STUCK_CONNECT_GRACE, JOIN_VERIFY_GRACE, JOIN_VERIFY_MAX,
                      AUTH_FAIL_LIMIT, LONG_BACKOFF)
from roblox_windows import (roblox_pids, kill_pid, kill_all_roblox,
                            scan_client_states)
from account_storage import write_session_map, release_multi_instance
from roblox_api import (get_auth_ticket, build_launch_uri, presence_detail,
                        parse_share_link, resolve_share_link,
                        universe_id_for_place, fetch_game_icons,
                        latest_client_error)
import roblox_error_codes as error_codes
import discord_notifications as dn

try:
    import os
except Exception:                       # pragma: no cover
    os = None


class MonitorEngine(QThread):
    log = Signal(str)
    state = Signal(list)                 # [{username, state, crashes, ...}]
    status = Signal(str, bool)           # (text, running)

    def __init__(self, accounts: list[dict], flags: dict,
                 webhook: str = "", join_url: str = "",
                 roblox_exe: str = "", icon_path: str = "", parent=None):
        super().__init__(parent)
        self.accounts = accounts          # resolved: rplace/rdelay present
        self.flags = flags                # live: kick detection and launch behavior
        self.webhook = webhook or ""
        self.join_url = (join_url or "").strip()
        self.roblox_exe = (roblox_exe or "").strip()   # "" → use protocol handler
        self.icon_path = (icon_path or "").strip()     # game icon for embeds
        self._icon_bytes: bytes | None = None
        self._icon_loaded = False
        self._join_place = None
        self._join_instance = None
        self._running = 0
        self._total = 0
        self._last_status = ""
        self.stop_event = threading.Event()
        self._lq: queue.Queue = queue.Queue()      # launch requests
        self._rq: queue.Queue = queue.Queue()      # launch results
        self._launching: set[int] = set()
        self._kill_q: queue.Queue = queue.Queue()   # manual per-account kills
        self._add_q: queue.Queue = queue.Queue()    # live account add (toggle on)
        self._drop_q: queue.Queue = queue.Queue()   # live account drop (toggle off)
        self._universe_cache: dict[str, str | None] = {}
        self._last_collision_check = 0.0
        self._mutex_handles = []

    def _universe_for_place(self, place_id: str | None) -> str | None:
        if not place_id:
            return None
        place_id = str(place_id)
        if place_id not in self._universe_cache:
            self._universe_cache[place_id] = universe_id_for_place(place_id)
        return self._universe_cache[place_id]

    def _matches_experience(self, acc: dict, places: set[str],
                            root_place: str | None) -> bool:
        target = str(acc.get("rplace") or "")
        if not target:
            return True
        expected = (str(acc.get("runiverse") or "")
                    or self._universe_for_place(target))
        current = self._universe_for_place(root_place)
        if expected and current:
            return expected == current
        return target in places

    # Controls called by the UI while monitoring is active.
    def stop(self):
        self.stop_event.set()

    def kill_all(self):
        try:
            kill_all_roblox()
        except Exception:
            pass

    def request_kill(self, uid: int):
        """Kill one account's client now and rejoin it (from the UI)."""
        self._kill_q.put(uid)

    def add_account(self, acc: dict):
        """Live-add a (resolved) account while running — it launches at once."""
        self._add_q.put(acc)

    def drop_account(self, uid: int, kill: bool = False):
        """Live-remove an account while running — stop watching it."""
        self._drop_q.put((int(uid), bool(kill)))

    def _status_text(self, rt: dict) -> str:
        total = len(rt)
        run = sum(1 for s in rt.values() if s["state"] == "running")
        launching = sum(1 for s in rt.values() if s["state"] == "launching")
        rejoining = sum(1 for s in rt.values()
                        if s["state"] in ("leaving", "waiting"))
        stopped = sum(1 for s in rt.values() if s["state"] == "stopped")
        if total == 0:
            return "No accounts being watched"
        parts = [f"{run}/{total} in-game"]
        if launching:
            parts.append(f"{launching} launching")
        if rejoining:
            parts.append(f"{rejoining} rejoining")
        if stopped:
            parts.append(f"{stopped} stopped")
        return "  ·  ".join(parts)

    # ------------------------------------------------------------------
    # Discord notifications — clean embeds in the Weeby style: plain title,
    # bold "Label: value" lines, game icon thumbnail, no emojis.
    # ------------------------------------------------------------------
    def _counts(self, rt: dict) -> tuple[int, int]:
        total = len(rt)
        up = sum(1 for s in rt.values() if s["state"] == "running")
        return up, total

    def _load_icon(self) -> None:
        """Load the selected game's icon once (local cache first, then the
        Roblox thumbnail API) so every embed can show it as a thumbnail."""
        if self._icon_loaded:
            return
        self._icon_loaded = True
        data = b""
        try:
            if self.icon_path and os.path.exists(self.icon_path):
                with open(self.icon_path, "rb") as f:
                    data = f.read()
            if not data:
                place = (self._join_place
                         or next((str(a.get("rplace") or "")
                                  for a in self.accounts
                                  if a.get("rplace")), ""))
                universe = self._universe_for_place(place) if place else None
                if universe:
                    icons = fetch_game_icons([universe])
                    data = icons.get(universe, b"")
        except Exception:
            data = b""
        self._icon_bytes = data or None

    def _notify(self, kind: str, title: str, event: str = "",
                details: list | None = None, description: str = "",
                online: bool = True, footer: str = "Roblox Rejoiner"):
        """Queue one embed: title, optional Event line, optional multi-account
        blocks, and a Details block of bold "Label: value" lines."""
        if not self.webhook:
            return
        try:
            fields: list[tuple] = []
            if description:
                fields.append(("Accounts", description, False))
            if event:
                fields.append(("Event", event, False))
            lines = [f"**{label}:** {value}" for label, value in (details or [])]
            if online and self._rt_counts_cache:
                up, total = self._counts(self._rt_counts_cache)
                lines.append(f"**Accounts online:** {up}/{total}")
            if lines:
                fields.append(("Details", "\n".join(lines), False))
            if not fields:
                return
            emb = dn.event_embed(kind, title, fields=fields, footer=footer)
            files = None
            if self._icon_bytes:
                dn.with_thumbnail(emb, "game_icon.png")
                files = [("game_icon.png", self._icon_bytes)]
            dn.discord_notify_embed(self.webhook, "Roblox Rejoiner", "",
                                    embeds=[emb], files=files)
        except Exception:
            pass

    def _account_details(self, st: dict) -> list[tuple[str, str]]:
        """Standard detail lines for one account: place and server (when the
        presence poll has seen it)."""
        acc = st["acc"]
        details = [("Place", str(acc.get("rplace", "?")))]
        if st.get("server"):
            details.append(("Server", str(st["server"])[:18]))
        return details

    # Launch queue processing and client startup.
    def _launch_one(self, acc: dict):
        ticket = get_auth_ticket(acc["cookie"])
        if not ticket:
            return None, "auth failed (cookie may be expired)"
        btid = acc.get("_btid")
        if not btid:
            btid = random.randint(100_000_000_000, 999_999_999_999)
            acc["_btid"] = btid
        if self.flags.get("join_server") and self._join_place:
            place, inst = self._join_place, self._join_instance
        else:
            place, inst = str(acc["rplace"]), None
        before = roblox_pids()
        uri = build_launch_uri(ticket, place, inst, browser_tracker_id=btid)
        try:
            if self.roblox_exe and os.path.exists(self.roblox_exe):
                import subprocess
                subprocess.Popen([self.roblox_exe, uri],
                                 close_fds=True)
            else:
                os.startfile(uri)
        except Exception as e:
            return None, f"launch error: {e}"
        deadline = time.time() + 60
        while time.time() < deadline:
            new = roblox_pids() - before
            if new:
                return max(new), None
            if self.stop_event.wait(1):
                return None, "stopped"
        return None, "Roblox window never started"

    def _launcher_loop(self):
        """One launch at a time, with a stagger between them."""
        while not self.stop_event.is_set():
            try:
                uid, acc = self._lq.get(timeout=0.3)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                return
            pid, err = self._launch_one(acc)
            self._rq.put((uid, pid, err))
            # Separate launch attempts so each new process can be identified.
            self.stop_event.wait(LAUNCH_STAGGER)

    def _enqueue(self, uid: int, acc: dict):
        if uid in self._launching:
            return
        self._launching.add(uid)
        self._lq.put((uid, acc))

    # Presence, account-state, and queue helpers.
    @staticmethod
    def _new_state(acc: dict) -> dict:
        return {"acc": acc, "pid": None, "state": "launching",
                "open_since": None, "crashes": 0, "disconnects": 0,
                "successful_rejoins": 0, "has_connected": False,
                "rejoin_at": 0.0,
                "intentional": False, "was_ingame": False,
                "kick_since": None, "leave_started": None,
                "leave_then_delay": 0, "next_leave_check": 0.0,
                "next_presence": 0.0,
                "server": None, "last_reason": "",
                "auth_fails": 0,
                "pres_fails": 0, "pres_fail_since": None,
                "hung_since": None,
                "dialog_reported": False, "hung_reported": False,
                "wrong_server_since": None, "wrong_server_tries": 0,
                "staged_reason": None}

    @staticmethod
    def _record_launch_success(st: dict, pid: int, connected_at: float) -> None:
        if st["has_connected"]:
            st["successful_rejoins"] += 1
        st["has_connected"] = True
        st["pid"] = pid
        st["state"] = "running"
        st["open_since"] = connected_at
        st["intentional"] = False
        st["was_ingame"] = False
        st["kick_since"] = None
        st["pres_fails"] = 0
        st["pres_fail_since"] = None
        st["hung_since"] = None
        st["wrong_server_since"] = None
        st["server"] = None
        st["dialog_reported"] = False
        st["hung_reported"] = False
        st["auth_fails"] = 0

    def _emit_state(self, rt: dict, now: float):
        rows = []
        for st in rt.values():
            up = 0.0
            if st["state"] == "running" and st.get("open_since"):
                up = now - st["open_since"]
            rejoin_in = 0.0
            if st["state"] == "waiting" and st.get("rejoin_at"):
                rejoin_in = max(0.0, st["rejoin_at"] - now)
            acc = st["acc"]
            rows.append({
                "user_id": acc.get("user_id"),
                "username": acc.get("username", "account"),
                "state": st["state"],
                "crashes": st["crashes"],
                "disconnects": st["disconnects"],
                "successful_rejoins": st["successful_rejoins"],
                "uptime": up,
                "pid": st.get("pid"),
                "rdelay": int(acc.get("rdelay", 0) or 0),
                "custom_delay": bool(acc.get("custom_delay")),
                "rejoin_in": rejoin_in,
                "reason": st.get("last_reason") or "",
            })
        self._rt_counts_cache = rt
        self.state.emit(rows)

    def _begin_leave(self, st: dict, now: float, delay=None,
                     reason: str = ""):
        """Enter the 'leaving' state: keep the PID so we can watch the client
        actually close, and wait (via presence) until the account has truly
        left the game before the rejoin delay even starts."""
        st["state"] = "leaving"
        st["leave_started"] = now
        st["next_leave_check"] = 0.0
        st["leave_then_delay"] = (int(delay) if delay is not None
                                  else int(st["acc"].get("rdelay", 0) or 0))
        if reason:
            st["last_reason"] = reason

    def _has_left(self, st: dict, pids: set, now: float) -> bool:
        """True only once Roblox presence (via the account's own cookie)
        confirms it is no longer in-game. Roblox keeps a just-closed session
        'in-game' for a while; relaunching before that clears causes error
        264. A time cap applies ONLY to repeated lookup failures so a dead
        cookie can't hang the account forever — and an absolute safety cap
        prevents a stale 'in-game' report from blocking a rejoin for good."""
        if st.get("pid") and st["pid"] in pids:
            return False                          # client process still alive
        started = st.get("leave_started") or now
        ck = st["acc"].get("cookie")
        if not ck:
            return True                           # can't verify — don't block
        if now < st.get("next_leave_check", 0.0):
            return False                          # throttle presence polling
        st["next_leave_check"] = now + LEAVE_POLL
        try:
            state, _, _, _ = presence_detail(st["acc"]["user_id"], ck)
        except Exception:
            # Repeated lookup errors: give up waiting after the cap.
            return now - started >= LEAVE_MAX_WAIT
        if state != "ingame":
            return True                           # confirmed left → safe to rejoin
        # Presence still says in-game long after the process died — treat the
        # report as stale rather than blocking the rejoin indefinitely.
        return now - started >= LEAVE_MAX_WAIT * 3

    def _set_reason(self, st: dict, reason: str):
        st["last_reason"] = reason

    def _remove_from_game(self, st: dict, now: float, reason: str,
                          count_disconnect: bool = True,
                          delay=None) -> None:
        """Kill the client (if open) and start the leave→rejoin cycle for an
        unplanned removal. Shared by crash/kick/hang/presence paths."""
        if count_disconnect:
            st["disconnects"] += 1
        if st.get("pid"):
            kill_pid(st["pid"])
        st["intentional"] = True
        st["kick_since"] = None
        st["hung_since"] = None
        st["dialog_reported"] = False
        st["hung_reported"] = False
        self._set_reason(st, reason)
        self._begin_leave(st, now, delay=delay, reason=reason)

    def _check_collisions(self, rt: dict, now: float) -> None:
        """Different-servers mode: Roblox can silently matchmake two accounts
        into the SAME server. Ask presence which server (gameId) each running
        account is actually in; if any server holds 2+ accounts, keep the
        alphabetically first and relaunch the rest to re-spread them."""
        if not self.flags.get("diff_server"):
            return
        if self.flags.get("join_server"):
            return
        if not self.flags.get("auto_relaunch", True):
            return
        if now - self._last_collision_check < PRESENCE_INTERVAL:
            return
        running = [(uid, st) for uid, st in rt.items()
                   if st["state"] == "running" and st.get("pid")
                   and st.get("open_since")
                   and now - st["open_since"] >= PRESENCE_SETTLE
                   and st["acc"].get("cookie")]
        if len(running) < 2:
            return
        self._last_collision_check = now
        servers: dict[int, str] = {}
        for uid, st in running:
            try:
                state, _, gid, _ = presence_detail(uid, st["acc"]["cookie"])
            except Exception:
                continue
            if state == "ingame" and gid:
                servers[uid] = gid
        by_server: dict[str, list[int]] = {}
        for uid, gid in servers.items():
            by_server.setdefault(gid, []).append(uid)
        for gid, uids in by_server.items():
            if len(uids) < 2:
                continue
            ordered = sorted(
                uids, key=lambda u: (rt[u]["acc"].get("username") or "").lower())
            keep = ordered[0]
            for uid in ordered[1:]:
                st = rt[uid]
                reason = f"shared server with {rt[keep]['acc']['username']} — moving it"
                self._remove_from_game(st, now, reason, count_disconnect=False,
                                       delay=3)
                self.log.emit(f"Server move: {st['acc']['username']} {reason}.")
                self._notify("warn", "Server collision",
                             event="Account moved to another server",
                             details=self._account_details(st) + [
                                 ("Reason", reason)],
                             footer="Different servers")

    # Poll account processes and handle queued UI actions.
    def run(self):
        from account_storage import acquire_multi_instance
        try:
            self._mutex_handles = acquire_multi_instance()
        except Exception:
            self._mutex_handles = []
        if self._mutex_handles:
            self.log.emit("Multi-instance: holding Roblox singleton mutex "
                          "and event — parallel clients can launch.")

        self._started_at = time.time()
        self._rt_counts_cache = {}
        launcher = threading.Thread(target=self._launcher_loop, daemon=True)
        launcher.start()

        rt: dict[int, dict] = {}
        self._join_place = None
        self._join_instance = None
        if self.flags.get("join_server") and self.join_url:
            code, ltype = parse_share_link(self.join_url)
            cookie = next((a.get("cookie") for a in self.accounts
                           if a.get("cookie")), None)
            if code and cookie:
                place = inst = None
                # Retry resolution: transient API/CSRF failures shouldn't
                # silently dump every account into open matchmaking.
                for attempt in range(3):
                    place, inst = resolve_share_link(cookie, code, ltype)
                    if place or self.stop_event.is_set():
                        break
                    self.stop_event.wait(3)
                if place:
                    self._join_place, self._join_instance = place, inst
                    join_universe = universe_id_for_place(place) or ""
                    for a in self.accounts:
                        a["rplace"] = place        # kick-detect targets this game
                        a["runiverse"] = join_universe
                    self.log.emit("Join server: resolved place " + place
                                  + (" (specific server)." if inst
                                     else " (open server)."))
                else:
                    self.log.emit("Join server: couldn't resolve that link "
                                  "after 3 attempts — launching into each "
                                  "account's own game.")
            else:
                self.log.emit("Join server: missing link or cookie — launching "
                              "normally.")
        adopt_pids: list[int] = []
        if self.flags.get("detect_open"):
            adopt_pids = sorted(roblox_pids())
            if adopt_pids:
                self.log.emit(f"Detect open clients: found {len(adopt_pids)} "
                              f"open Roblox client(s) — adopting them.")
            else:
                self.log.emit("Detect open clients: none open — launching "
                              "normally.")
        self._load_icon()
        now0 = time.time()
        for idx, acc in enumerate(self.accounts):
            uid = acc["user_id"]
            st = self._new_state(acc)
            if self.flags.get("detect_open") and idx < len(adopt_pids):
                st["state"] = "running"
                st["pid"] = adopt_pids[idx]
                st["open_since"] = now0
                st["has_connected"] = True
                rt[uid] = st
                self.log.emit(f"Watching {acc['username']} on existing client "
                              f"(pid {adopt_pids[idx]}).")
            else:
                rt[uid] = st
                self.log.emit(f"{acc['username']} — launching…")
                self._enqueue(uid, acc)

        self.status.emit(self._status_text(rt), True)
        self._last_status = self._status_text(rt)
        _entries = []
        for s in rt.values():
            acc = s["acc"]
            target = (f"server {self._join_instance[:12]}"
                      if self._join_instance
                      else f"place {acc.get('rplace', '?')}")
            _entries.append((acc.get("username", "?"), target))
        _mode = ("watching" if self.flags.get("auto_relaunch", True)
                 else "launch-only (no auto-relaunch)")
        where = ("a specific server" if self._join_instance
                 else f"place {self._join_place}" if self._join_place
                 else "their own games")
        self._notify("info", "Monitoring started",
                     details=[("Mode", _mode), ("Target", where)],
                     description=dn.account_lines(_entries))

        while not self.stop_event.is_set():
            now = time.time()
            pids = roblox_pids()
            self._total = len(rt)
            self._running = sum(1 for s in rt.values()
                                if s["state"] == "running")
            relaunch = bool(self.flags.get("auto_relaunch", True))
            txt = self._status_text(rt)
            if txt != self._last_status:
                self._last_status = txt
                self.status.emit(txt, True)

            # Apply account-selection changes without restarting the engine.
            try:
                while True:
                    acc = self._add_q.get_nowait()
                    auid = acc["user_id"]
                    if auid in rt:
                        continue
                    rt[auid] = self._new_state(acc)
                    self.log.emit(f"{acc['username']} toggled on — launching…")
                    self._enqueue(auid, acc)
            except queue.Empty:
                pass
            try:
                while True:
                    duid, kill = self._drop_q.get_nowait()
                    st = rt.pop(duid, None)
                    if st:
                        if kill and st.get("pid"):
                            kill_pid(st["pid"])
                        self._launching.discard(duid)
                        self.log.emit(f"{st['acc']['username']} toggled off — "
                                      f"no longer watching it.")
            except queue.Empty:
                pass

            # Process user-requested relaunches for individual accounts.
            try:
                while True:
                    kuid = self._kill_q.get_nowait()
                    st = rt.get(kuid)
                    if not st:
                        continue
                    if st.get("pid"):
                        kill_pid(st["pid"])
                        _tail = ("rejoining." if relaunch
                                 else "auto-relaunch is off.")
                        self.log.emit(f"Manual restart: {st['acc']['username']} "
                                      f"— killed manually; {_tail}")
                        self._notify(
                            "warn", st['acc'].get("username", "?"),
                            event="Manual restart",
                            details=self._account_details(st) + [
                                ("Action", "killed manually — rejoining"
                                 if relaunch else "killed manually")],
                            footer="Requested from the app")
                    st["intentional"] = True
                    if relaunch:
                        self._begin_leave(st, now)
                    else:
                        st["pid"] = None
                        st["open_since"] = None
                        st["state"] = "stopped"
            except queue.Empty:
                pass

            # Match completed launches to their account state.
            try:
                while True:
                    uid, pid, err = self._rq.get_nowait()
                    self._launching.discard(uid)
                    st = rt.get(uid)
                    if not st:
                        continue
                    if pid:
                        self._record_launch_success(st, pid, time.time())
                        self.log.emit(f"{st['acc']['username']} is in "
                                      f"(pid {pid}).")
                        self._notify(
                            "good", st['acc'].get("username", "?"),
                            event="Client launched",
                            details=self._account_details(st),
                            footer="Waiting for Roblox presence")
                    else:
                        self._handle_launch_failure(st, err, now, relaunch)
            except queue.Empty:
                pass

            kick_detect = bool(self.flags.get("kick_detect")) and relaunch

            # One window-enumeration pass per tick covers kick dialogs and
            # frozen windows for every running client at once.
            running_pids = {s["pid"] for s in rt.values()
                            if s["state"] == "running" and s.get("pid")}
            win_states = scan_client_states(running_pids) if running_pids else {}

            for uid, st in rt.items():
                if self.stop_event.is_set():
                    break
                acc = st["acc"]
                if st["state"] == "running":
                    self._watch_running(uid, st, pids,
                                        win_states.get(st["pid"], {}),
                                        now, kick_detect, relaunch)

                elif st["state"] == "leaving":
                    if self._has_left(st, pids, now):
                        st["pid"] = None
                        st["open_since"] = None
                        st["state"] = "waiting"
                        st["rejoin_at"] = now + int(
                            st.pop("leave_then_delay", acc.get("rdelay", 0) or 0))
                        self.log.emit(f"{acc['username']} left the game — "
                                      f"rejoining in "
                                      f"{int(st['rejoin_at'] - now)}s.")

                elif st["state"] == "waiting" and now >= st["rejoin_at"]:
                    st["state"] = "launching"
                    st["pid"] = None
                    st["open_since"] = None
                    self.log.emit(f"{acc['username']} — relaunching…")
                    self._enqueue(uid, acc)

            # Move accounts apart when Roblox places them in the same server.
            self._check_collisions(rt, now)

            self._emit_state(rt, now)
            try:
                write_session_map(rt)        # Discord-bot session bridge
            except Exception:
                pass
            self.stop_event.wait(1.0)

        # Stop summary: one embed, one spaced block per account.
        _entries = []
        _now = time.time()
        _session_up = ""
        for s in self._rt_counts_cache.values():
            acc = s["acc"]
            state = s["state"]
            detail = (f"in game \u00b7 up "
                      f"{int(_now - s['open_since']) // 60}m"
                      if state == "running" and s.get("open_since")
                      else state)
            dis = int(s.get("disconnects", 0) or 0)
            if dis:
                detail += f" \u00b7 {dis} disconnect" + ("s" if dis != 1 else "")
            _entries.append((acc.get("username", "?"), detail))
        self._notify("muted", "Monitoring stopped",
                     details=[("Session",
                               f"ran for {int((_now - self._started_at) // 60)}m")],
                     description=dn.account_lines(_entries))
        self.status.emit("Stopped", False)
        try:
            release_multi_instance(self._mutex_handles)
            self._mutex_handles = []
        except Exception:
            pass

    def _handle_launch_failure(self, st: dict, err, now: float,
                               relaunch: bool) -> bool:
        """Classify a failed launch. Returns True when the account was STOPPED
        (cookie dead — relaunching cannot succeed, needs user action)."""
        acc = st["acc"]
        errstr = str(err or "")
        if errstr.startswith("auth failed"):
            st["auth_fails"] = st.get("auth_fails", 0) + 1
            if relaunch and st["auth_fails"] >= AUTH_FAIL_LIMIT:
                # The cookie is dead: retrying would loop forever.
                reason = "cookie expired or invalid — re-add this account"
                st["state"] = "stopped"
                self._set_reason(st, reason)
                self.log.emit(f"Auth: {acc['username']} — {st['auth_fails']} "
                              f"failed logins in a row; stopping auto-rejoin.")
                self._notify(
                    "bad", acc.get("username", "?"),
                    event="Account needs attention",
                    details=[("Reason", reason),
                             ("Failed logins", st["auth_fails"])],
                    footer="Auto-rejoin stopped — update the cookie")
                return True
        else:
            st["auth_fails"] = 0
        st["state"] = "waiting"
        st["rejoin_at"] = now + max(8, int(acc.get("rdelay", 0) or 0))
        self._set_reason(st, f"launch failed ({err})")
        self.log.emit(f"Launch failed: {acc['username']} ({err}); retrying.")
        self._notify("bad", acc.get("username", "?"), event="Launch failed",
                     details=[("Reason", errstr)],
                     footer="Will retry automatically")
        return False

    # ------------------------------------------------------------------
    # Per-tick health checks for a RUNNING account. Ordered by confidence:
    #   1. process gone                    → crash
    #   2. kick/disconnect dialog open     → real reason + error code
    #   3. window hung                     → frozen client
    #   4. presence checks                 → kick/disconnect/wrong game/server
    # `client` is this pid's entry from scan_client_states (dialog + hung).
    # ------------------------------------------------------------------
    def _watch_running(self, uid: int, st: dict, pids: set, client: dict,
                       now: float, kick_detect: bool, relaunch: bool):
        acc = st["acc"]
        up = now - st["open_since"] if st.get("open_since") else 0

        # 1. The tracked process exited unexpectedly.
        if st["pid"] not in pids:
            if not st["intentional"]:
                st["crashes"] += 1
                st["disconnects"] += 1
                reason = "client crashed / closed unexpectedly"
                # No dialog was seen — ask the client's log why it died.
                try:
                    hint = latest_client_error()
                except Exception:
                    hint = None
                if hint:
                    reason += f" (log: {hint})"
                self._set_reason(st, reason)
                if relaunch:
                    self.log.emit(f"Crash: {acc['username']} — {reason} after "
                                  f"{int(up // 60)}m {int(up % 60)}s in game; "
                                  f"rejoining (crash #{st['crashes']}).")
                    self._notify(
                        "bad", acc.get("username", "?"),
                        event="Client crashed",
                        details=self._account_details(st) + [
                            ("Reason", reason),
                            ("In game for", f"{int(up // 60)}m {int(up % 60)}s"),
                            ("Crash #", st['crashes'])],
                        footer="Rejoining once presence clears")
                else:
                    self.log.emit(f"Crash: {acc['username']} closed "
                                  f"(auto-relaunch off).")
                    self._notify(
                        "bad", acc.get("username", "?"),
                        event="Client closed",
                        details=self._account_details(st) + [
                            ("Reason", reason)],
                        footer="Auto-relaunch is off")
            st["intentional"] = False
            if relaunch:
                self._begin_leave(st, now)
            else:
                st["pid"] = None
                st["open_since"] = None
                st["state"] = "stopped"
            return

        # 2. A native kick/disconnect/error dialog is open on the client —
        #    this is the definitive removal signal, with the real reason.
        dialog = client.get("dialog")
        if dialog:
            if st.get("intentional"):
                return                       # we closed it ourselves
            self._set_reason(st, dialog)
            category, _ = error_codes.classify(client.get("dialog_code"))
            if not relaunch:
                if not st.get("dialog_reported"):
                    # Launch-only mode: report it, leave the dialog for the user.
                    st["dialog_reported"] = True
                    st["disconnects"] += 1
                    self.log.emit(f"Kick: {acc['username']} — {dialog} "
                                  f"(auto-relaunch off).")
                    self._notify("warn", acc.get("username", "?"),
                                 event="Kicked from the game",
                                 details=self._account_details(st) + [
                                     ("Reason", dialog)],
                                 footer="Auto-relaunch is off")
                return
            if error_codes.is_stop_category(category):
                # Rejoining can never fix this (no permission, platform or
                # country locked) — stop the account with a clear reason.
                if st.get("pid"):
                    kill_pid(st["pid"])
                st["disconnects"] += 1
                st["intentional"] = True
                st["pid"] = None
                st["open_since"] = None
                st["state"] = "stopped"
                self.log.emit(f"Needs attention: {acc['username']} — "
                              f"{dialog}; auto-rejoin stopped.")
                self._notify("bad", acc.get("username", "?"),
                             event="Account needs attention",
                             details=self._account_details(st) + [
                                 ("Reason", dialog)],
                             footer="Auto-rejoin stopped — fix the cause, "
                             "then re-tick the account")
                return
            if category == error_codes.CATEGORY_RETRY:
                # The game can't accept joins right now (updating, shut down,
                # full) — back off longer instead of hammering it.
                delay = max(LONG_BACKOFF, int(acc.get("rdelay", 0) or 0))
                self._remove_from_game(st, now, dialog, delay=delay)
                self.log.emit(f"Game unavailable: {acc['username']} — "
                              f"{dialog}; rejoining in {int(delay)}s.")
                self._notify("warn", acc.get("username", "?"),
                             event="Game temporarily unavailable",
                             details=self._account_details(st) + [
                                 ("Reason", dialog),
                                 ("Next attempt", f"in {int(delay)}s")],
                             footer="Backing off while the game recovers")
                return
            # kick / disconnect / session overlap: rejoin (session overlap
            # additionally waits for presence to clear the old session).
            self._remove_from_game(st, now, dialog)
            self.log.emit(f"Kick: {acc['username']} — {dialog}; "
                          f"rejoining once it leaves.")
            self._notify("warn", acc.get("username", "?"),
                         event="Kicked from the game",
                         details=self._account_details(st) + [
                             ("Reason", dialog),
                             ("Disconnect #", st['disconnects'])],
                         footer="Read from the client's dialog")
            return

        # 3. Frozen client: window not responding for a sustained period.
        if client.get("hung"):
            if st.get("hung_since") is None:
                st["hung_since"] = now
            elif now - st["hung_since"] >= HANG_GRACE:
                reason = "client froze (not responding)"
                if relaunch:
                    self._remove_from_game(st, now, reason)
                    self.log.emit(f"Frozen: {acc['username']} — {reason}; "
                                  f"rejoining.")
                    self._notify("bad", acc.get("username", "?"),
                                 event="Client froze",
                                 details=self._account_details(st) + [
                                     ("Reason", reason)],
                                 footer="Client was not responding")
                elif not st.get("hung_reported"):
                    st["hung_reported"] = True
                    st["disconnects"] += 1
                    self._set_reason(st, reason)
                    self.log.emit(f"Frozen: {acc['username']} — {reason} "
                                  f"(auto-relaunch off).")
                    self._notify("bad", acc.get("username", "?"),
                                 event="Client froze",
                                 details=self._account_details(st) + [
                                     ("Reason", reason)],
                                 footer="Auto-relaunch is off")
                return
        else:
            st["hung_since"] = None

        # 4. Presence-based checks need the account's cookie; give the client
        #    a moment after connecting so presence reflects the real server.
        if not (kick_detect and acc.get("cookie") and st.get("open_since")
                and now - st["open_since"] >= PRESENCE_SETTLE
                and now >= st.get("next_presence", 0.0)):
            return
        st["next_presence"] = now + KICK_POLL
        try:
            pstate, places, gid, root_place = presence_detail(uid, acc["cookie"])
        except Exception:
            pstate, places, gid, root_place = "unknown", set(), None, None
        if gid and pstate == "ingame":
            st["server"] = gid

        reason = None
        if pstate == "unknown":
            # Repeated failures ≈ connection loss (or a dead cookie); require
            # both a streak and elapsed time so rate limits never trigger it.
            st["pres_fails"] += 1
            if st.get("pres_fail_since") is None:
                st["pres_fail_since"] = now
            if (st["pres_fails"] >= PRESENCE_FAIL_LIMIT
                    and now - st["pres_fail_since"] >= PRESENCE_FAIL_TIME):
                if st.get("was_ingame"):
                    reason = "presence unreachable — connection lost"
                elif up >= STUCK_CONNECT_GRACE:
                    reason = "never connected (stuck on the loading screen)"
        else:
            st["pres_fails"] = 0
            st["pres_fail_since"] = None

        if reason is None and pstate == "ingame":
            if not self._matches_experience(acc, places, root_place):
                reason = "in a different game than configured"
            elif (self._join_instance and st.get("was_ingame")
                  and gid and gid != self._join_instance
                  and not self.flags.get("diff_server")):
                # Landed in open matchmaking instead of the requested server.
                if st.get("wrong_server_since") is None:
                    st["wrong_server_since"] = now
                elif (now - st["wrong_server_since"] >= JOIN_VERIFY_GRACE
                      and st.get("wrong_server_tries", 0) < JOIN_VERIFY_MAX):
                    st["wrong_server_tries"] = \
                        st.get("wrong_server_tries", 0) + 1
                    reason = ("landed in a different server than requested "
                              f"(try {st['wrong_server_tries']}/"
                              f"{JOIN_VERIFY_MAX})")
            else:
                st["was_ingame"] = True
                st["kick_since"] = None
                st["wrong_server_since"] = None
        elif reason is None and pstate == "out":
            if st.get("was_ingame"):
                reason = "kicked/disconnected (client open, not in-game)"
            elif up >= STUCK_CONNECT_GRACE:
                reason = "never connected (stuck on the loading screen)"

        if reason:
            # Reasons that already carried their own longer grace (connection
            # loss, stuck loading, wrong-server verification) act at once;
            # everything else must persist KICK_GRACE before we close the
            # client, so a momentary presence blip never rejoins a good client.
            already_graced = ("connection lost" in reason
                              or "never connected" in reason
                              or "different server" in reason)
            if already_graced:
                self._act_on_reason(st, now, reason)
            else:
                if st.get("kick_since") is None:
                    st["kick_since"] = now
                elif now - st["kick_since"] >= KICK_GRACE:
                    self._act_on_reason(st, now, reason)
        else:
            st["kick_since"] = None

    def _act_on_reason(self, st: dict, now: float, reason: str):
        acc = st["acc"]
        self._remove_from_game(st, now, reason)
        low = reason.lower()
        if "crash" in low:
            event = "Client crashed"
        elif "kick" in low:
            event = "Kicked from the game"
        elif "froze" in low or "hung" in low:
            event = "Client froze"
        elif "connection" in low:
            event = "Disconnected"
        elif "wrong server" in low or "different server" in low:
            event = "Wrong server"
        else:
            event = "Removed from the game"
        self.log.emit(f"{event}: {acc['username']} — {reason}; "
                      f"rejoining once it leaves.")
        self._notify(
            "warn", acc.get("username", "?"),
            event=event,
            details=self._account_details(st) + [
                ("Reason", reason),
                ("Disconnect #", st['disconnects'])],
            footer="Detected via Roblox presence")
