"""Independent account monitoring and relaunch engine.

The engine runs in a QThread and reports status through Qt signals. It
serializes and staggers launches so process-to-account matching stays reliable,
detects crashes and kicks, waits for Roblox presence to confirm a client left,
and handles each selected account independently.

The UI can update the shared behavior flags while monitoring. Changing which
accounts are selected restarts the engine in the UI.
"""

from __future__ import annotations

import queue
import random
import threading
import time

from PySide6.QtCore import QThread, Signal

from rejoiner_constants import (LAUNCH_STAGGER, PRESENCE_SETTLE, KICK_GRACE, KICK_POLL,
                      LEAVE_POLL, LEAVE_MAX_WAIT, PRESENCE_INTERVAL)
from roblox_windows import roblox_pids, kill_pid, kill_all_roblox
from account_storage import write_session_map
from roblox_api import (get_auth_ticket, build_launch_uri, presence_detail,
                       parse_share_link, resolve_share_link,
                       universe_id_for_place)
from discord_notifications import discord_notify

try:
    import os
except Exception:                       # pragma: no cover
    os = None


class MonitorEngine(QThread):
    log = Signal(str)
    state = Signal(list)                 # [{username, state, crashes, uptime, pid}]
    status = Signal(str, bool)           # (text, running)

    def __init__(self, accounts: list[dict], flags: dict,
                 webhook: str = "", join_url: str = "",
                 roblox_exe: str = "", parent=None):
        super().__init__(parent)
        self.accounts = accounts          # resolved: rplace/rdelay present
        self.flags = flags                # live: kick detection and launch behavior
        self.webhook = webhook or ""
        self.join_url = (join_url or "").strip()
        self.roblox_exe = (roblox_exe or "").strip()   # "" → use protocol handler
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

    def _notify(self, msg: str):
        """Fire-and-forget Discord webhook post (no-op if unset)."""
        if self.webhook:
            try:
                discord_notify(self.webhook, "Roblox Rejoiner", "", msg)
            except Exception:
                pass

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
                "open_since": None, "crashes": 0, "successful_rejoins": 0,
                "has_connected": False, "rejoin_at": 0.0,
                "intentional": False, "was_ingame": False,
                "kick_since": None, "leave_started": None,
                "leave_then_delay": 0, "next_leave_check": 0.0,
                "next_presence": 0.0}

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
                "successful_rejoins": st["successful_rejoins"],
                "uptime": up,
                "pid": st.get("pid"),
                "rdelay": int(acc.get("rdelay", 0) or 0),
                "custom_delay": bool(acc.get("custom_delay")),
                "rejoin_in": rejoin_in,
            })
        self.state.emit(rows)

    def _begin_leave(self, st: dict, now: float, delay=None):
        """Enter the 'leaving' state: keep the PID so we can watch the client
        actually close, and wait (via presence) until the account has truly left
        the game before the rejoin delay even starts."""
        st["state"] = "leaving"
        st["leave_started"] = now
        st["next_leave_check"] = 0.0
        st["leave_then_delay"] = (int(delay) if delay is not None
                                  else int(st["acc"].get("rdelay", 0) or 0))

    def _has_left(self, st: dict, pids: set, now: float) -> bool:
        """True only once Roblox presence (via the account's own cookie) confirms
        it is no longer in-game. Roblox keeps a just-closed session 'in-game' for
        a while; relaunching before that clears causes error 264. We wait as long
        as presence keeps reporting in-game — a time cap applies ONLY to repeated
        lookup failures so a dead cookie can't hang the account forever."""
        if st.get("pid") and st["pid"] in pids:
            return False                          # client process still alive
        ck = st["acc"].get("cookie")
        if not ck:
            return True                           # can't verify — don't block
        if now < st.get("next_leave_check", 0.0):
            return False                          # throttle presence polling
        st["next_leave_check"] = now + LEAVE_POLL
        try:
            state, _, _, _ = presence_detail(st["acc"]["user_id"], ck)
        except Exception:
            started = st.get("leave_started") or now
            return now - started >= 90            # only give up on repeated errors
        return state != "ingame"                  # confirmed left → safe to rejoin

    def _check_collisions(self, rt: dict, now: float) -> None:
        """Different-servers mode: Roblox can silently matchmake two accounts into
        the SAME server. Ask presence which server (gameId) each running account
        is actually in; if any server holds 2+ accounts, keep the alphabetically
        first and relaunch the rest to re-spread them."""
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
                if st.get("pid"):
                    kill_pid(st["pid"])
                st["intentional"] = True
                self._begin_leave(st, now, delay=3)   # re-spread fast once left
                self.log.emit(f"\U0001f500 {st['acc']['username']} shared a server "
                              f"with {rt[keep]['acc']['username']} — moving it.")
                self._notify(f"\U0001f500 {st['acc']['username']} shared a server "
                             f"with {rt[keep]['acc']['username']} — moving it.")

    # Poll account processes and handle queued UI actions.
    def run(self):
        from account_storage import acquire_multi_instance
        try:
            self._mutex_handles = acquire_multi_instance()
        except Exception:
            self._mutex_handles = []

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
                place, inst = resolve_share_link(cookie, code, ltype)
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
                    self.log.emit("Join server: couldn't resolve that link — "
                                  "launching into each account's own game.")
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
        _names = ", ".join(s["acc"]["username"] for s in rt.values()) or "—"
        _where = ("a specific server" if self._join_instance
                  else f"place {self._join_place}" if self._join_place
                  else "their own games")
        _mode = ("watching" if self.flags.get("auto_relaunch", True)
                 else "launch-only (no auto-relaunch)")
        self._notify(f"\u25b6\ufe0f Roblox Rejoiner {_mode} — {len(rt)} account(s) in "
                     f"{_where}.\nAccounts: {_names}")
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
                        self.log.emit(f"\U0001f52a {st['acc']['username']} — "
                                      f"killed manually; {_tail}")
                        self._notify(f"\U0001f52a {st['acc']['username']} — "
                                     f"killed manually; {_tail}")
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
                        self.log.emit(f"{st['acc']['username']} is in (pid {pid}). \u2713")
                        self._notify(f"\u2705 {st['acc']['username']} is in "
                                     f"(place {st['acc'].get('rplace', '?')}). "
                                     f"({self._running + 1}/{self._total} up)")
                    else:
                        st["state"] = "waiting"
                        st["rejoin_at"] = now + max(8, int(st["acc"]["rdelay"]))
                        self.log.emit(f"{st['acc']['username']} launch failed "
                                      f"({err}); retrying.")
            except queue.Empty:
                pass

            kick_detect = bool(self.flags.get("kick_detect")) and relaunch

            for uid, st in rt.items():
                if self.stop_event.is_set():
                    break
                acc = st["acc"]
                if st["state"] == "running":
                    # The tracked process exited unexpectedly.
                    if st["pid"] not in pids:
                        if not st["intentional"]:
                            st["crashes"] += 1
                            if relaunch:
                                self.log.emit(f"\U0001f4a5 {acc['username']} crashed "
                                              f"— closing out, then rejoining "
                                              f"(crash #{st['crashes']}).")
                                self._notify(
                                    f"\U0001f4a5 {acc['username']} crashed in place "
                                    f"{acc.get('rplace', '?')} — rejoining once it "
                                    f"leaves. ({self._running}/{self._total} up)")
                            else:
                                self.log.emit(f"\U0001f4a5 {acc['username']} closed "
                                              f"(auto-relaunch off).")
                                self._notify(f"\U0001f4a5 {acc['username']} closed "
                                             f"— auto-relaunch is off.")
                        st["intentional"] = False
                        if relaunch:
                            self._begin_leave(st, now)
                        else:
                            st["pid"] = None
                            st["open_since"] = None
                            st["state"] = "stopped"
                        continue
                    # Use Roblox presence to identify kicks or wrong experiences.
                    if (kick_detect and acc.get("cookie")
                            and st.get("open_since")
                            and now - st["open_since"] >= PRESENCE_SETTLE
                            and now >= st.get("next_presence", 0.0)):
                        st["next_presence"] = now + KICK_POLL
                        try:
                            pstate, places, _, root_place = presence_detail(
                                uid, acc["cookie"])
                        except Exception:
                            pstate, places, root_place = "unknown", set(), None
                        reason = None
                        if pstate == "ingame":
                            if not self._matches_experience(
                                    acc, places, root_place):
                                reason = "in a different game than configured"
                            else:
                                st["was_ingame"] = True
                                st["kick_since"] = None
                        elif pstate == "out":
                            if st.get("was_ingame"):
                                reason = "kicked/disconnected (client open, not in-game)"
                        if reason:
                            if st.get("kick_since") is None:
                                st["kick_since"] = now
                            elif now - st["kick_since"] >= KICK_GRACE:
                                kill_pid(st["pid"])
                                st["intentional"] = True
                                st["kick_since"] = None
                                st["was_ingame"] = False
                                self._begin_leave(st, now)
                                self.log.emit(f"\U0001f45f {acc['username']} — "
                                              f"{reason}; rejoining once it leaves.")
                                self._notify(f"\U0001f45f {acc['username']} — {reason} "
                                             f"(place {acc.get('rplace', '?')}); "
                                             f"rejoining. "
                                             f"({self._running}/{self._total} up)")
                        else:
                            st["kick_since"] = None

                elif st["state"] == "leaving":
                    if self._has_left(st, pids, now):
                        st["pid"] = None
                        st["open_since"] = None
                        st["state"] = "waiting"
                        st["rejoin_at"] = now + int(
                            st.pop("leave_then_delay", acc.get("rdelay", 0) or 0))
                        self.log.emit(f"{acc['username']} left the game — "
                                      f"rejoining in {int(st['rejoin_at'] - now)}s.")

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

        self._notify("\u23f9\ufe0f Roblox Rejoiner stopped.")
        self.status.emit("Stopped", False)
