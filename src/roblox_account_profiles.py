"""Roblox account avatar, display-name, and presence lookups.

Fetches Roblox display names and avatar headshots (cached to disk) and batch
presence, entirely in the background so the UI never blocks. Everything is
batched into one request per kind to avoid Roblox rate-limits, and nothing here
ever raises into the UI — every failure degrades to a clean placeholder. Names
and avatars use public, unauthenticated endpoints; presence uses one stored
cookie (Roblox exposes presence for arbitrary user IDs).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from PySide6.QtCore import QObject, Signal

from rejoiner_constants import CONFIG_PATH, UA

CACHE_DIR = os.path.join(str(CONFIG_PATH.parent), "avatars")


def _open(req, timeout=12):
    return urllib.request.urlopen(req, timeout=timeout)


class ProfileFetcher(QObject):
    """Background, batched fetch of (display_name, avatar_path) per user id."""

    fetched = Signal(int, str, str)        # uid, display_name, avatar_path ("" if none)

    def fetch(self, uid: int):
        self.fetch_batch([int(uid)])

    def fetch_batch(self, uids):
        ids = [int(u) for u in uids if u]
        if ids:
            threading.Thread(target=self._work, args=(ids,), daemon=True).start()

    # Batched request and retry helpers.
    def _work(self, uids: list[int]):
        names = self._names(uids)
        thumbs = self._thumbs(uids)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
        except Exception:
            pass
        for uid in uids:
            path = os.path.join(CACHE_DIR, f"{uid}.png")
            url = thumbs.get(uid)
            if url and not os.path.exists(path):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": UA})
                    with _open(req) as r:
                        raw = r.read()
                    if raw:
                        with open(path, "wb") as f:
                            f.write(raw)
                except Exception:
                    pass
            self.fetched.emit(int(uid), names.get(uid, ""),
                              path if os.path.exists(path) else "")
            time.sleep(0.05)

    def _names(self, uids: list[int]) -> dict:
        out = {}
        try:
            body = json.dumps({"userIds": uids,
                               "excludeBannedUsers": False}).encode("utf-8")
            req = urllib.request.Request(
                "https://users.roblox.com/v1/users", data=body, method="POST",
                headers={"User-Agent": UA, "Content-Type": "application/json"})
            with _open(req, 10) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            for it in data.get("data", []):
                out[int(it.get("id"))] = (it.get("displayName")
                                          or it.get("name") or "")
        except Exception:
            pass
        return out

    def _thumbs(self, uids: list[int]) -> dict:
        """Batch headshot URLs. Retries once for any that come back 'Pending'."""
        out = {}
        pending = list(uids)
        for attempt in range(2):
            if not pending:
                break
            ids = ",".join(str(u) for u in pending)
            url = ("https://thumbnails.roblox.com/v1/users/avatar-headshot"
                   f"?userIds={ids}&size=150x150&format=Png&isCircular=false")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with _open(req, 10) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
            except Exception:
                break
            still = []
            for it in data.get("data", []):
                tid = int(it.get("targetId", 0) or 0)
                if it.get("state") == "Completed" and it.get("imageUrl"):
                    out[tid] = it["imageUrl"]
                elif tid:
                    still.append(tid)
            pending = still
            if pending and attempt == 0:
                time.sleep(1.5)
        return out


class PresenceFetcher(QObject):
    """Background batch presence for many user ids using one cookie."""

    fetched = Signal(dict)                  # {uid: "ingame"/"online"/"out"/...}

    def fetch(self, user_ids: list[int], cookie: str):
        if user_ids and cookie:
            threading.Thread(target=self._work,
                             args=(list(user_ids), cookie), daemon=True).start()

    def _work(self, user_ids: list[int], cookie: str):
        try:
            from roblox_api import fetch_presence
            result = fetch_presence(user_ids, cookie)
        except Exception:
            result = {}
        if result:
            self.fetched.emit(result)
