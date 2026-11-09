"""Independent account monitoring and relaunch engine.

The engine runs in a QThread and reports status through Qt signals. It
serializes and staggers launches so process-to-account matching stays
reliable, and it detects essentially every way an account can be removed
from an instance, then rejoins each affected account independently:

- client process exited (crash, forced close, Roblox update restart)
- native kick/disconnect dialog open on the client ("Disconnected",
  "Error Code: 267", moderation messages - the code is read from the
  dialog and reported)
- client window hung (frozen, not pumping messages)
- client open but Roblox presence says the account left the game
  (kick / disconnect / server shutdown with the dialog dismissed)
- presence unreachable for a sustained period (connection loss)
- client never reaches the game (stuck on the loading screen)
- account landed in a different experience than configured
- account landed in a different server than the private server or
  specific server its link points at

Private servers are resolved per account: each account may carry its own
share / private-server link, and only the accounts the user chose to send
there actually use one. A link that Roblox refuses to resolve holds the
account back with a clear reason instead of quietly dropping it into public
matchmaking.

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
                      AUTH_FAIL_LIMIT, LONG_BACKOFF, JOIN_RESOLVE_ATTEMPTS,
                      JOIN_RETRY_SECS, JOIN_RESOLVE_GIVEUP,
                      SERVER_REPROBE_SECS)
from roblox_windows import (roblox_pids, kill_pid, kill_all_roblox,
                            scan_client_states)
from account_storage import write_session_map, release_multi_instance
from roblox_api import (get_auth_ticket, build_launch_uri, presence_detail,
                        resolve_join_link, probe_join_server,
                        universe_id_for_place,
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
                 roblox_exe: str = "", app_icon: str = "", parent=None):
        super().__init__(parent)
        self.accounts = accounts          # resolved: rplace/rdelay present
        self.flags = flags                # live: kick detection and launch behavior
        self.webhook = webhook or ""
        self.join_url = (join_url or "").strip()
        self.roblox_exe = (roblox_exe or "").strip()   # "" -> use protocol handler
        self.app_icon = (app_icon or "").strip()       # the app's own badge
        self._icon_bytes: bytes | None = None
        self._icon_loaded = False
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
        # Private-server / specific-server targets, resolved per link text so
        # two accounts sharing one link only hit Roblox once. Accounts whose
        # link is not resolved yet wait in `pending` instead of launching.
        self._targets: dict[int, dict] = {}
        self._links: dict[str, dict] = {}
        self._link_gids: dict[str, dict[str, int]] = {}
        self._pending: dict[int, dict] = {}
        self._rt_counts_cache: dict = {}

    # Presence and universe-id helpers.
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
        """Live-add a (resolved) account while running - it launches at once."""
        self._add_q.put(acc)

    def drop_account(self, uid: int, kill: bool = False):
        """Live-remove an account while running - stop watching it."""
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
        return "  -  ".join(parts)

    # ------------------------------------------------------------------
    # Discord notifications - clean embeds in the Weeby style: plain title,
    # bold "Label: value" lines, game icon thumbnail, no emojis.
    # ------------------------------------------------------------------
    def _counts(self, rt: dict) -> tuple[int, int]:
        total = len(rt)
        up = sum(1 for s in rt.values() if s["state"] == "running")
        return up, total

    def _load_icon(self) -> None:
        """Load the app's own icon once, so every embed is branded with the
        tool it came from rather than a game thumbnail."""
        if self._icon_loaded:
            return
        self._icon_loaded = True
        data = b""
        try:
            if self.app_icon and os.path.exists(self.app_icon):
                with open(self.app_icon, "rb") as f:
                    data = f.read()
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

    # ------------------------------------------------------------------
    # Private-server / specific-server targets.
    #
    # Each account may carry its own link (`rjoin`), which is either a
    # per-account override or the link the Setup page sends this way. A link
    # only becomes a target once Roblox resolves it; a link that keeps
    # failing holds the account in "waiting" with the reason shown, so a bad
    # link can never silently dump an account into public matchmaking.
    # ------------------------------------------------------------------
    def _resolve_join(self, acc: dict, now: float, force: bool = False
                      ) -> dict | None:
        """Resolve (with retries) this account's link. None = still unknown."""
        link = str(acc.get("rjoin") or "").strip()
        uid = acc.get("user_id")
        if not (self.flags.get("join_server") and link):
            self._targets.pop(uid, None)
            return None
        state = self._links.get(link)
        if state is not None:
            target = state.get("target")
            if target is not None:
                self._targets[uid] = state
                return state
            if not force and (target is None and state.get("tries")
                              >= JOIN_RESOLVE_GIVEUP):
                return None                # hopeless link - leave it stopped
        if state is None:
            state = {"link": link, "target": None, "tries": 0,
                     "next_at": now, "error": "", "gave_up": False,
                     "ref": "", "ref_at": now}
            self._links[link] = state
        if not force and now < state.get("next_at", 0.0):
            return None
        cookie = acc.get("cookie")
        if not cookie:
            state["error"] = "no account cookie to resolve the link with"
            state["tries"] = state.get("tries", 0) + 1
            state["next_at"] = now + JOIN_RETRY_SECS
            return None
        target = resolve_join_link(cookie, link, attempts=JOIN_RESOLVE_ATTEMPTS,
                                   stop=self.stop_event.is_set)
        state["tries"] = state.get("tries", 0) + 1
        if target.ok:
            state["target"] = target
            state["error"] = ""
            state["gave_up"] = False
            state["ref"] = ""
            # Ask Roblox which server this link points at, so a client that
            # silently lands in public matchmaking can be caught and rejoin
            # instead of being declared correct.
            if target.kind == "private":
                job, _status, _err = probe_join_server(
                    cookie, target.place, link_code=target.link_code)
                if job:
                    state["ref"] = job
                    target.job_id = job
            elif target.kind == "job" and target.job_id:
                state["ref"] = target.job_id
            state["ref_at"] = now
            self._apply_target(acc, target)
            self._targets[uid] = state
            self._pending.pop(uid, None)
            self.log.emit(f"Join link: {acc.get('username', '?')} -> "
                          f"{target.describe()}.")
            return state
        state["error"] = target.error or "couldn't resolve that link"
        state["gave_up"] = state["tries"] >= JOIN_RESOLVE_GIVEUP
        state["next_at"] = now + JOIN_RETRY_SECS
        return None

    def _join_target_summary(self) -> str:
        """One phrase describing where the resolved targets send accounts.

        Used by the startup notification, so it says "private server links"
        rather than listing every account."""
        kinds = {s["target"].kind for s in self._targets.values()
                 if s.get("target") is not None}
        if not kinds:
            return ""
        if kinds == {"private"}:
            return "private server links"
        if kinds == {"job"}:
            return "specific server links"
        if kinds == {"open"}:
            return "one shared game"
        return "a mix of server links"

    @staticmethod
    def _current_place(places: set, root_place: str | None) -> str:
        """The place an account is really in.

        Presence reports both the current place and the experience's root
        place; when they differ, the account has been moved to another place
        inside the same experience."""
        root = str(root_place or "")
        others = {str(p) for p in places if str(p) != root}
        if len(others) == 1:
            return others.pop()
        return root or (str(next(iter(places))) if places else "")

    def _in_target_place(self, acc: dict, target, places: set,
                         root_place: str | None) -> bool:
        """True when the account is in the place its link points at.

        Anywhere else inside the same experience (a lobby handing players to
        the map, a reserved server the game opened for them) is a teleport,
        not a wrong join, so the job id is simply not comparable."""
        if target is None:
            return False
        where = self._current_place(places, root_place)
        if not where:
            return False
        return where == str(target.place) or where == str(acc.get("rplace") or "")

    def _server_moved(self, acc: dict, target, key: str, gid: str,
                      ref: str, backers: int, now: float) -> bool:
        """True when the account is in the private server after all.

        A private server gets a fresh job id whenever Roblox restarts it, and
        a single account cannot out-vote a stale lookup, so the link is asked
        again (at most once a minute) before the client is closed."""
        state = self._links.get(key) or {}
        if backers >= 2:
            # Several accounts agree on where they are: believe them.
            return False
        if str(state.get("ref") or "") != str(ref):
            return False                     # the vote produced this reference
        if now - float(state.get("ref_at", 0.0)) < SERVER_REPROBE_SECS:
            return False
        state["ref_at"] = now
        try:
            job, _status, _err = probe_join_server(
                acc.get("cookie"), target.place, link_code=target.link_code,
                job_id=target.job_id)
        except Exception:
            job = None
        if job and job == gid:
            # The private server just has a new job id: adopt it.
            state["ref"] = job
            self.log.emit(
                f"Server check: {acc.get('username', '?')} is in the private "
                f"server (it restarted with a new job) - keeping it.")
            return True
        if job:
            state["ref"] = job
        return False

    def _wrong_server_tick(self, acc: dict, st: dict, target, now: float
                          ) -> bool:
        """One wrong-server poll. Returns True once checking should stop.

        A mismatch must persist before the client is closed, and only a few
        attempts are made per launch, so a game that shuffles servers can
        never be rejoined forever."""
        if st.get("wrong_server_since") is None:
            st["wrong_server_since"] = now
            return False
        if now - st["wrong_server_since"] < JOIN_VERIFY_GRACE:
            return False
        if st.get("wrong_server_tries", 0) >= JOIN_VERIFY_MAX:
            st["server_check_done"] = True
            st["wrong_server_since"] = None
            self.log.emit(
                f"Server check: {acc.get('username', '?')} stopped verifying "
                f"{target.label()} after {st['wrong_server_tries']} tries - "
                f"leaving it where Roblox put it.")
            return True
        st["wrong_server_tries"] = st.get("wrong_server_tries", 0) + 1
        self._remove_from_game(
            st, now,
            f"landed in a different server than {target.label()} "
            f"(try {st['wrong_server_tries']}/{JOIN_VERIFY_MAX})")
        self.log.emit(
            f"Wrong server: {acc.get('username', '?')} - landed in a "
            f"different server than {target.label()} "
            f"(try {st['wrong_server_tries']}/{JOIN_VERIFY_MAX}); rejoining "
            f"once it leaves.")
        self._notify(
            "warn", acc.get("username", "?"), event="Wrong server",
            details=self._account_details(st) + [
                ("Reason", f"not in the private server ({target.label()})"),
                ("Attempt", f"{st['wrong_server_tries']}/{JOIN_VERIFY_MAX}")],
            footer="Rejoining the private server")
        return False

    def _apply_target(self, acc: dict, target) -> None:
        """Point this account at a resolved join target. Its place identity
        comes from the link so wrong-game detection follows the server the
        account is really being sent to."""
        acc["rplace"] = target.place
        if target.universe:
            acc["runiverse"] = target.universe
        else:
            acc["runiverse"] = self._universe_for_place(target.place) or ""
        acc["rjoin_target"] = target.label()

    def _retry_join(self, acc: dict, st: dict, now: float) -> bool:
        """Retry a pending link. True when the account may now launch."""
        state = self._resolve_join(acc, now)
        if state is None:
            info = self._links.get(str(acc.get("rjoin") or "").strip()) or {}
            if info.get("gave_up"):
                reason = f"giving up on the join link ({info.get('error')})"
                st["state"] = "stopped"
            else:
                reason = (info.get("error")
                          or "join link not resolved yet - retrying")
                st["state"] = "waiting"
                st["rejoin_at"] = now + JOIN_RETRY_SECS
            st["next_join_try"] = now + JOIN_RETRY_SECS
            self._set_reason(st, reason)
            if reason.startswith("giving up"):
                self.log.emit(f"Join link: {acc.get('username', '?')} - "
                             f"{reason}. Fix the link, then re-tick the account.")
            return False
        st["state"] = "launching"
        self._enqueue(acc.get("user_id"), acc)
        return True

    def _note_server(self, key: str, uid, gid, now: float) -> None:
        """Record which server one account is in, per link.

        One entry per *account*, not per poll: a vote is only meaningful when
        different accounts agree, otherwise a single account in the wrong
        server would count its own repeated answers as a majority."""
        if not key or not gid or gid == "None" or uid is None:
            return
        seen = self._link_gids.setdefault(key, {})
        slot = seen.get(uid)
        if slot is None or slot.get("gid") != str(gid):
            seen[uid] = {"gid": str(gid), "first": now}

    def _server_reference(self, key: str) -> tuple[str | None, int]:
        """The server an account is expected to be in, and how sure we are.

        Two sources of truth: what Roblox said the link points at when it was
        resolved, and where the accounts themselves ended up. A server that
        two or more accounts agree on wins, because a private server's job id
        changes whenever Roblox restarts it - otherwise a stale lookup would
        rejoin a working client forever. The count is of *accounts*, so one
        account's own reports can never out-vote the link."""
        if not key:
            return None, 0
        state = self._links.get(key) or {}
        probe = str(state.get("ref") or "")
        per_account = self._link_gids.get(key) or {}
        tally: dict[str, dict] = {}
        for info in per_account.values():
            gid = str(info.get("gid") or "")
            if not gid:
                continue
            slot = tally.setdefault(gid, {"count": 0, "first": info.get("first", 0.0)})
            slot["count"] += 1
            slot["first"] = min(slot["first"], info.get("first", 0.0))
        vote = None
        vote_n = 0
        if tally:
            (vote, info) = max(tally.items(),
                               key=lambda kv: (kv[1]["count"], -kv[1]["first"]))
            vote_n = int(info["count"])
        if vote_n >= 2 and vote != probe:
            return vote, vote_n
        if probe:
            return probe, 1
        if vote:
            return vote, vote_n
        return None, 0

    def _clear_reference(self, key: str) -> None:
        self._link_gids.pop(key, None)

    # Launch queue processing and client startup.
    def _launch_one(self, acc: dict):
        ticket = get_auth_ticket(acc["cookie"])
        if not ticket:
            return None, "auth failed (cookie may be expired)"
        btid = acc.get("_btid")
        if not btid:
            btid = random.randint(100_000_000_000, 999_999_999_999)
            acc["_btid"] = btid
        state = self._targets.get(acc.get("user_id"))
        target = state.get("target") if state else None
        if target is not None:
            place = target.place
            inst = target.job_id
            link_code = target.link_code
        else:
            place, inst, link_code = str(acc["rplace"]), None, None
        if not place:
            return None, "no place to launch (set one on Rejoiner Setup)"
        uri = build_launch_uri(ticket, place, inst, link_code,
                               browser_tracker_id=btid)
        if not uri:
            return None, ("invalid launch target (place id or link code has "
                          "unsupported characters)")
        before = roblox_pids()
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
                "next_join_try": 0.0,
                "server_check_done": False,
                "server_verified": False,
                "following": False,
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
        st["server_check_done"] = False
        st["server_verified"] = False
        st["following"] = False
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
                "join_target": acc.get("rjoin_target") or "",
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
        cookie can't hang the account forever - and an absolute safety cap
        prevents a stale 'in-game' report from blocking a rejoin for good."""
        if st.get("pid") and st["pid"] in pids:
            return False                          # client process still alive
        started = st.get("leave_started") or now
        ck = st["acc"].get("cookie")
        if not ck:
            return True                           # can't verify - don't block
        if now < st.get("next_leave_check", 0.0):
            return False                          # throttle presence polling
        st["next_leave_check"] = now + LEAVE_POLL
        try:
            state, _, _, _ = presence_detail(st["acc"]["user_id"], ck)
        except Exception:
            # Repeated lookup errors: give up waiting after the cap.
            return now - started >= LEAVE_MAX_WAIT
        if state != "ingame":
            return True                           # confirmed left -> safe to rejoin
        # Presence still says in-game long after the process died - treat the
        # report as stale rather than blocking the rejoin indefinitely.
        return now - started >= LEAVE_MAX_WAIT * 3

    def _set_reason(self, st: dict, reason: str):
        st["last_reason"] = reason

    def _remove_from_game(self, st: dict, now: float, reason: str,
                          count_disconnect: bool = True,
                          delay=None) -> None:
        """Kill the client (if open) and start the leave->rejoin cycle for an
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
        # Everyone else follows, so a group stays in the same server.
        self._rejoin_company(getattr(self, "_rt", None)
                             or self._rt_counts_cache, st, now)

    def _check_collisions(self, rt: dict, now: float) -> None:
        """Different-servers mode: Roblox can silently matchmake two accounts
        into the SAME server. Ask presence which server (gameId) each running
        account is actually in; if any server holds 2+ accounts, keep the
        first and relaunch the rest to re-spread them.

        Accounts sent to a specific server are exempt - they are *supposed*
        to share one, and their link target is checked instead."""
        if not self.flags.get("diff_server"):
            return
        if not self.flags.get("auto_relaunch", True):
            return
        if now - self._last_collision_check < PRESENCE_INTERVAL:
            return
        running = [(uid, st) for uid, st in rt.items()
                   if st["state"] == "running" and st.get("pid")
                   and st.get("open_since")
                   and now - st["open_since"] >= PRESENCE_SETTLE
                   and st["acc"].get("cookie")
                   and uid not in self._targets]
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
                reason = f"shared server with {rt[keep]['acc']['username']} - moving it"
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
        # A crash in here must say why: a silent QThread death leaves the app
        # looking like it did nothing, which is the worst possible feedback.
        try:
            self._run_inner()
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.log.emit(f"Monitor crashed: {type(e).__name__}: {e} - "
                              "stopping. Press Start to try again.")
                self.status.emit(f"Stopped ({type(e).__name__})", False)
            except Exception:
                pass
            try:
                release_multi_instance(self._mutex_handles)
                self._mutex_handles = []
            except Exception:
                pass
            return

    def _run_inner(self):
        from account_storage import acquire_multi_instance
        try:
            self._mutex_handles = acquire_multi_instance()
        except Exception:
            self._mutex_handles = []
        if self._mutex_handles:
            self.log.emit("Multi-instance: holding Roblox singleton mutex "
                          "and event - parallel clients can launch.")

        self._started_at = time.time()
        self._rt_counts_cache = {}
        launcher = threading.Thread(target=self._launcher_loop, daemon=True)
        launcher.start()

        rt: dict[int, dict] = {}
        self._rt = rt            # live table, for rejoin-together
            # Resolve every account's join link first, one link at a time, so a
        # transient API failure never decides an account's destination.
        now0 = time.time()
        for acc in self.accounts:
            uid = acc.get("user_id")
            st = self._new_state(acc)
            rt[uid] = st
            if self.flags.get("join_server") and str(acc.get("rjoin") or "").strip():
                state = self._resolve_join(acc, now0, force=True)
                if state is None:
                    st["state"] = "waiting"
                    st["next_join_try"] = now0 + JOIN_RETRY_SECS
                    st["rejoin_at"] = st["next_join_try"]
                    link = str(acc.get("rjoin") or "").strip()
                    info = self._links.get(link) or {}
                    self._set_reason(st, info.get("error")
                                     or "join link not resolved yet - retrying")
                    self._pending[uid] = st
                    self.log.emit(f"Join link: {acc.get('username', '?')} -> "
                                  f"{st['last_reason']}.")
                    continue
            # _resolve_join has already pointed the account at the target.
            # Only accounts without a pending link launch; adopted clients fill in
        # below so the pid order still matches the account order.
        if self.flags.get("detect_open"):
            adopt_pids = sorted(roblox_pids())
            if adopt_pids:
                self.log.emit(f"Detect open clients: found {len(adopt_pids)} "
                              f"open Roblox client(s) - adopting them.")
            else:
                self.log.emit("Detect open clients: none open - launching "
                              "normally.")
        else:
            adopt_pids = []
        self._load_icon()
        for idx, acc in enumerate(self.accounts):
            uid = acc.get("user_id")
            st = rt[uid]
            if uid in self._pending:
                continue
            if self.flags.get("detect_open") and idx < len(adopt_pids):
                st["state"] = "running"
                st["pid"] = adopt_pids[idx]
                st["open_since"] = now0
                st["has_connected"] = True
                self.log.emit(f"Watching {acc['username']} on existing client "
                              f"(pid {adopt_pids[idx]}).")
            else:
                st["state"] = "launching"
                self.log.emit(f"{acc['username']} - launching...")
                self._enqueue(uid, acc)

        self.status.emit(self._status_text(rt), True)
        self._last_status = self._status_text(rt)
        _entries = []
        for s in rt.values():
            acc = s["acc"]
            _entries.append((acc.get("username", "?"),
                             acc.get("rjoin_target")
                             or f"place {acc.get('rplace', '?')}"))
        _mode = ("watching" if self.flags.get("auto_relaunch", True)
                 else "launch-only (no auto-relaunch)")
        where = self._join_target_summary() or "their own games"
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
                    st = self._new_state(acc)
                    if (self.flags.get("join_server")
                            and str(acc.get("rjoin") or "").strip()):
                        state = self._resolve_join(acc, now, force=True)
                        if state is None:
                            self._pending[auid] = st
                            st["state"] = "waiting"
                            st["next_join_try"] = now + JOIN_RETRY_SECS
                            st["rejoin_at"] = st["next_join_try"]
                            link = str(acc.get("rjoin") or "").strip()
                            info = self._links.get(link) or {}
                            self._set_reason(
                                st, info.get("error")
                                or "join link not resolved yet - retrying")
                            rt[auid] = st
                            self.log.emit(
                                f"Join link: {acc['username']} -> "
                                f"{st['last_reason']}.")
                            continue
                    # _resolve_join already applied this account's target.
                    rt[auid] = st
                    self.log.emit(f"{acc['username']} toggled on - launching...")
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
                        self._targets.pop(duid, None)
                        self._pending.pop(duid, None)
                        self.log.emit(f"{st['acc']['username']} toggled off - "
                                      f"no longer watching it.")
            except queue.Empty:
                pass

                # Retry links that would not resolve, so a Roblox API hiccup
                # (CSRF refresh, rate limit) delays the launch instead of
            # dropping the account into a public server.
            for uid in list(self._pending):
                st = self._pending.get(uid)
                if st is None or uid not in rt:
                    self._pending.pop(uid, None)
                    continue
                if now >= st.get("next_join_try", 0.0):
                    self._pending.pop(uid, None)
                    self._retry_join(st["acc"], st, now)

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
                                      f"- killed manually; {_tail}")
                        self._notify(
                            "warn", st['acc'].get('username', '?'),
                            event="Manual restart",
                            details=self._account_details(st) + [
                                ("Action", "killed manually - rejoining"
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
                            "good", st['acc'].get('username', '?'),
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
            win_states, orphan_dialogs = (scan_client_states(running_pids, True)
                                          if running_pids else ({}, []))
            self._adopt_orphan_dialogs(rt, win_states, orphan_dialogs)

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
                        self.log.emit(f"{acc['username']} left the game - "
                                      f"rejoining in "
                                      f"{int(st['rejoin_at'] - now)}s.")

                elif st["state"] == "waiting" and now >= st["rejoin_at"]:
                    if uid in self._pending:
                            # Still waiting on the join link: the retry loop above
                            # owns this account, and it must never relaunch into
                        # its own game as a fallback.
                        continue
                    st["state"] = "launching"
                    st["pid"] = None
                    st["open_since"] = None
                    self.log.emit(f"{acc['username']} - relaunching...")
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
            detail = (f"in game - up "
                      f"{int(_now - s['open_since']) // 60}m"
                      if state == "running" and s.get("open_since")
                      else state)
            dis = int(s.get("disconnects", 0) or 0)
            if dis:
                detail += f" - {dis} disconnect" + ("s" if dis != 1 else "")
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

    def _adopt_orphan_dialogs(self, rt: dict, win_states: dict,
                              orphans: list) -> None:
        """Give an unattributed error dialog to the client it must belong to.

        Roblox sometimes shows "Join Error", "Disconnected" and similar
        dialogs from a helper process, so the dialog is not owned by the
        client's pid and no account would otherwise notice it. When exactly
        one client is still waiting to connect, the dialog is clearly its."""
        if not orphans:
            return
        waiting = [(uid, st) for uid, st in rt.items()
                   if st["state"] == "running" and st.get("pid")
                   and not st.get("was_ingame")
                   and not win_states.get(st["pid"], {}).get("dialog")]
        if len(waiting) != 1:
            for orphan in orphans:
                self.log.emit(
                    f"Found a Roblox error dialog "
                    f"({orphan.get('title') or 'dialog'}: "
                    f"{orphan.get('reason')}) that doesn't belong to a "
                    f"tracked client.")
            return
        _uid, st = waiting[0]
        orphan = orphans[0]
        slot = win_states.setdefault(st["pid"],
                                     {"dialog": None, "dialog_code": None,
                                      "hung": False})
        slot["dialog"] = orphan.get("reason")
        slot["dialog_code"] = orphan.get("code")
        self.log.emit(f"Error dialog found for "
                      f"{st['acc'].get('username', '?')} "
                      f"({orphan.get('reason')}).")

    def _rejoin_company(self, rt: dict, trigger_st: dict, now: float) -> int:
        """Send every other running account back to the same server too.

        "Rejoin together" keeps a group in one server: when any account is
        removed from the game, the rest follow it out and rejoin, so nobody is
        left behind in a different server. Only accounts that are actually in
        the game are touched, and each keeps its own delay and target."""
        if not self.flags.get("rejoin_all"):
            return 0
        trigger = trigger_st["acc"].get("username", "?")
        moved = 0
        for uid, st in rt.items():
            if st is trigger_st:
                continue
            if st["state"] != "running" or st.get("intentional"):
                continue
            if st.get("following"):
                continue
            st["following"] = True
            reason = f"rejoining together with {trigger}"
            self._remove_from_game(st, now, reason, count_disconnect=False,
                                   delay=2)
            self.log.emit(f"Together: {st['acc']['username']} is rejoining with "
                          f"{trigger}.")
            self._notify("info", st["acc"].get("username", "?"),
                         event="Rejoining with the group",
                         details=self._account_details(st) + [
                             ("Reason", f"{trigger} left the game")],
                         footer="Rejoin together")
            moved += 1
        return moved

    def _handle_launch_failure(self, st: dict, err, now: float,
                               relaunch: bool) -> bool:
        """Classify a failed launch. Returns True when the account was STOPPED
        (cookie dead - relaunching cannot succeed, needs user action)."""
        acc = st["acc"]
        errstr = str(err or "")
        if errstr.startswith("auth failed"):
            st["auth_fails"] = st.get("auth_fails", 0) + 1
            if relaunch and st["auth_fails"] >= AUTH_FAIL_LIMIT:
                # The cookie is dead: retrying would loop forever.
                reason = "cookie expired or invalid - re-add this account"
                st["state"] = "stopped"
                self._set_reason(st, reason)
                self.log.emit(f"Auth: {acc['username']} - {st['auth_fails']} "
                              f"failed logins in a row; stopping auto-rejoin.")
                self._notify(
                    "bad", acc.get("username", "?"),
                    event="Account needs attention",
                    details=[("Reason", reason),
                             ("Failed logins", st["auth_fails"])],
                    footer="Auto-rejoin stopped - update the cookie")
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
    #   1. process gone                    -> crash
    #   2. kick/disconnect dialog open     -> real reason + error code
    #   3. window hung                     -> frozen client
    #   4. presence checks                 -> kick/disconnect/wrong game/server
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
                # No dialog was seen - ask the client's log why it died.
                try:
                    hint = latest_client_error()
                except Exception:
                    hint = None
                if hint:
                    reason += f" (log: {hint})"
                self._set_reason(st, reason)
                if relaunch:
                    self.log.emit(f"Crash: {acc['username']} - {reason} after "
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

        # 2. A native kick/disconnect/error dialog is open on the client -
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
                    self.log.emit(f"Kick: {acc['username']} - {dialog} "
                                  f"(auto-relaunch off).")
                    self._notify("warn", acc.get("username", "?"),
                                 event="Kicked from the game",
                                 details=self._account_details(st) + [
                                     ("Reason", dialog)],
                                 footer="Auto-relaunch is off")
                return
            if error_codes.is_stop_category(category):
                # Rejoining can never fix this (no permission, platform or
                # country locked) - stop the account with a clear reason.
                if st.get("pid"):
                    kill_pid(st["pid"])
                st["disconnects"] += 1
                st["intentional"] = True
                st["pid"] = None
                st["open_since"] = None
                st["state"] = "stopped"
                self.log.emit(f"Needs attention: {acc['username']} - "
                              f"{dialog}; auto-rejoin stopped.")
                self._notify("bad", acc.get("username", "?"),
                             event="Account needs attention",
                             details=self._account_details(st) + [
                                 ("Reason", dialog)],
                             footer="Auto-rejoin stopped - fix the cause, "
                             "then re-tick the account")
                return
            if category == error_codes.CATEGORY_RETRY:
                # The game can't accept joins right now (updating, shut down,
                # full) - back off longer instead of hammering it.
                delay = max(LONG_BACKOFF, int(acc.get("rdelay", 0) or 0))
                self._remove_from_game(st, now, dialog, delay=delay)
                self.log.emit(f"Game unavailable: {acc['username']} - "
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
            self.log.emit(f"Kick: {acc['username']} - {dialog}; "
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
                    self.log.emit(f"Frozen: {acc['username']} - {reason}; "
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
                    self.log.emit(f"Frozen: {acc['username']} - {reason} "
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
            # Repeated failures about connection loss (or a dead cookie); require
            # both a streak and elapsed time so rate limits never trigger it.
            st["pres_fails"] += 1
            if st.get("pres_fail_since") is None:
                st["pres_fail_since"] = now
            if (st["pres_fails"] >= PRESENCE_FAIL_LIMIT
                    and now - st["pres_fail_since"] >= PRESENCE_FAIL_TIME):
                if st.get("was_ingame"):
                    reason = "presence unreachable - connection lost"
                elif up >= STUCK_CONNECT_GRACE:
                    reason = "never connected (stuck on the loading screen)"
        else:
            st["pres_fails"] = 0
            st["pres_fail_since"] = None

        if reason is None and pstate == "ingame":
            if not self._matches_experience(acc, places, root_place):
                reason = "in a different game than configured"
            else:
                key = str(acc.get("rjoin") or "").strip()
                target = self._targets.get(uid, {}).get("target")
                in_place = self._in_target_place(acc, target, places,
                                                 root_place)
                # Only servers in the place the link points at are compared:
                # a game that moves players between its own places, or into a
                # reserved server for them, legitimately changes the job id,
                # and that must never look like a public server.
                if gid and key and in_place:
                    self._note_server(key, uid, gid, now)
                if (target is not None and target.specific and gid and in_place
                        and not st.get("server_verified")
                        and not self.flags.get("diff_server")):
                    ref, backers = self._server_reference(key)
                    if not ref or gid == ref:
                        st["server_verified"] = True
                        st["wrong_server_since"] = None
                    else:
                        moved = self._server_moved(acc, target, key, gid, ref,
                                                   backers, now)
                        if moved:
                            st["server_verified"] = True
                            st["wrong_server_since"] = None
                        else:
                            st["server_verified"] = self._wrong_server_tick(
                                acc, st, target, now)
                st["was_ingame"] = True
                st["kick_since"] = None
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
        self.log.emit(f"{event}: {acc['username']} - {reason}; "
                      f"rejoining once it leaves.")
        self._notify(
            "warn", acc.get("username", "?"),
            event=event,
            details=self._account_details(st) + [
                ("Reason", reason),
                ("Disconnect #", st['disconnects'])],
            footer="Detected via Roblox presence")
