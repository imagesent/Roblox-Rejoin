"""Roblox web API helpers for authentication, game launch, and presence.

Everything that talks to Roblox over HTTP: authenticating a cookie, fetching a
one-time auth ticket and building the launch URI, resolving share links,
listing public servers, and reading presence (which game/server an account is
actually in — the signal behind kick/wrong-game detection and the
different-servers collision splitter).
"""

from __future__ import annotations

import functools
from concurrent.futures import ThreadPoolExecutor
import json
import random
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import TypedDict

from rejoiner_constants import (AUTH_TICKET_URL, USERS_AUTH_URL, SHARE_RESOLVE_URL, UA,
                      DEFAULT_SERVER_CAP, LOG_DIR, JOIN_RE)


class GameSearchResult(TypedDict):
    name: str
    universe_id: str
    root_place_id: str
    creator: str
    icon_data: bytes


def _post(url: str, cookie: str, csrf: str | None = None, body: bytes = b""):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    req.add_header("Referer", "https://www.roblox.com/")
    req.add_header("Origin", "https://www.roblox.com")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    if csrf:
        req.add_header("X-CSRF-TOKEN", csrf)
    return urllib.request.urlopen(req, timeout=15)


def get_account_info(cookie: str) -> tuple[int | None, str | None]:
    req = urllib.request.Request(USERS_AUTH_URL)
    req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
            return data.get("id"), data.get("name")
    except Exception:
        return None, None


def get_csrf(cookie: str) -> str | None:
    try:
        resp = _post(AUTH_TICKET_URL, cookie)
        return resp.headers.get("x-csrf-token")
    except urllib.error.HTTPError as e:
        return e.headers.get("x-csrf-token")
    except Exception:
        return None


def get_auth_ticket(cookie: str) -> str | None:
    csrf = get_csrf(cookie)
    if not csrf:
        return None
    try:
        resp = _post(AUTH_TICKET_URL, cookie, csrf)
        return resp.headers.get("rbx-authentication-ticket")
    except urllib.error.HTTPError as e:
        return e.headers.get("rbx-authentication-ticket")
    except Exception:
        return None


def build_launch_uri(ticket: str, place_id: str, instance_id: str | None = None,
                     browser_tracker_id: int | None = None) -> str:
    """Build the roblox-player launch URI, matching the exact format used by
    Roblox Account Manager (the widely-working multi-account tool). Joining a
    specific server uses request=RequestGameJob with &gameId=<jobId>; open
    matchmaking uses request=RequestGame. The trailing +channel: field and the
    isPlayTogetherGame flag are intentionally omitted to match RAM."""
    if instance_id:
        # Target a specific public or private server instance.
        pl = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx"
              f"?request=RequestGameJob&placeId={place_id}&gameId={instance_id}")
    else:
        # Let Roblox matchmaking choose an available server.
        pl = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx"
              f"?request=RequestGame&placeId={place_id}&isPlayTogetherGame=false")
    pl_enc = urllib.parse.quote(pl, safe="")
    launch_ms = int(time.time() * 1000)
    btid = browser_tracker_id or random.randint(100_000_000_000, 999_999_999_999)
    return (f"roblox-player:1+launchmode:play+gameinfo:{ticket}"
            f"+launchtime:{launch_ms}+browsertrackerid:{btid}"
            f"+placelauncherurl:{pl_enc}"
            f"+robloxLocale:en_us+gameLocale:en_us")


def parse_place_id(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"/games/(\d+)", raw)
    if m:
        return m.group(1)
    return raw if raw.isdigit() else None


def search_roblox_games(query: str, limit: int = 20) -> list[GameSearchResult]:
    """Search public Roblox experiences and return their IDs and display data."""
    query = (query or "").strip()
    if not query:
        raise ValueError("Enter a game name to search.")
    params = urllib.parse.urlencode({
        "searchQuery": query,
        "pageType": "games",
        "sessionId": str(uuid.uuid4()),
    })
    url = f"https://apis.roblox.com/search-api/omni-search?{params}"
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://www.roblox.com/"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Roblox game search failed (HTTP {e.code}).") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError("Roblox game search could not connect.") from e
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError("Roblox returned an invalid game search response.") from e

    if not isinstance(data, dict):
        raise RuntimeError("Roblox returned an invalid game search response.")
    results = []
    for group in data.get("searchResults", []):
        if not isinstance(group, dict) or group.get("contentGroupType") != "Game":
            continue
        for game in group.get("contents", []):
            if not isinstance(game, dict):
                continue
            universe_id = game.get("universeId")
            root_place_id = game.get("rootPlaceId")
            name = game.get("name")
            if not (universe_id and root_place_id and name):
                continue
            results.append({
                "name": str(name),
                "universe_id": str(universe_id),
                "root_place_id": str(root_place_id),
                "creator": str(game.get("creatorName") or "Unknown creator"),
                "icon_data": b"",
            })
            if len(results) >= max(1, int(limit)):
                return results
    return results


def fetch_game_icons(universe_ids: list[str]) -> dict[str, bytes]:
    """Fetch available 150px Roblox experience icons keyed by universe ID."""
    ids = list(dict.fromkeys(str(uid) for uid in universe_ids if str(uid).isdigit()))
    if not ids:
        return {}
    params = urllib.parse.urlencode({
        "universeIds": ",".join(ids),
        "size": "150x150",
        "format": "Png",
        "isCircular": "false",
    })
    url = f"https://thumbnails.roblox.com/v1/games/icons?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Roblox game icons failed (HTTP {e.code}).") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError("Roblox game icons could not connect.") from e
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError("Roblox returned an invalid game icon response.") from e

    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RuntimeError("Roblox returned an invalid game icon response.")
    icon_urls = {
        str(item["targetId"]): item["imageUrl"]
        for item in data["data"]
        if isinstance(item, dict)
        and str(item.get("targetId", "")).isdigit()
        and item.get("state") == "Completed"
        and isinstance(item.get("imageUrl"), str)
        and urllib.parse.urlparse(item["imageUrl"]).hostname == "tr.rbxcdn.com"
    }

    def download(item: tuple[str, str]) -> tuple[str, bytes | None]:
        uid, image_url = item
        image_req = urllib.request.Request(image_url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(image_req, timeout=10) as resp:
                content_type = resp.headers.get_content_type()
                if content_type != "image/png":
                    return uid, None
                image = resp.read(1_048_577)
                if len(image) > 1_048_576:
                    return uid, None
                return uid, image
        except (urllib.error.URLError, TimeoutError, OSError):
            return uid, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {uid: image for uid, image in pool.map(download, icon_urls.items())
                if image is not None}


@functools.lru_cache(maxsize=256)
def universe_id_for_place(place_id: str) -> str | None:
    """Look up and cache the Roblox experience ID that owns a place."""
    place_id = str(place_id or "").strip()
    if not place_id.isdigit():
        return None
    url = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    universe_id = data.get("universeId") if isinstance(data, dict) else None
    return str(universe_id) if universe_id else None


def parse_share_link(raw: str) -> tuple[str | None, str | None]:
    """Pull (code, link_type) out of a Roblox share URL (or a bare code).
    e.g. https://www.roblox.com/share?code=ABC&type=ExperienceInvite."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        u = urllib.parse.urlparse(raw)
        if u.query:
            qs = urllib.parse.parse_qs(u.query)
            code = (qs.get("code") or [None])[0]
            ltype = (qs.get("type") or ["ExperienceInvite"])[0]
            if code:
                return code, ltype
    except Exception:
        pass
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", raw):   # looks like a bare code
        return raw, "ExperienceInvite"
    return None, None


def _find_first(blob, keys: tuple):
    """Depth-first search a nested dict/list for the first present key."""
    stack = [blob]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k in keys:
                v = cur.get(k)
                if v not in (None, "", 0):
                    return v
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def resolve_share_link(cookie: str, code: str, link_type: str
                       ) -> tuple[str | None, str | None]:
    """Resolve a share link to (place_id, instance_id). instance_id is the
    specific server when the invite points at one, else None (open server).
    Returns (None, None) if it can't be resolved."""
    payload = json.dumps({"linkId": code,
                          "linkType": link_type or "ExperienceInvite"}).encode()

    def _try(token):
        resp = _post(SHARE_RESOLVE_URL, cookie, token, payload)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    csrf = get_csrf(cookie)
    try:
        data = _try(csrf)
    except urllib.error.HTTPError as e:
        new = e.headers.get("x-csrf-token")
        if not new:
            return None, None
        try:
            data = _try(new)
        except Exception:
            return None, None
    except Exception:
        return None, None
    place = _find_first(data, ("placeId", "rootPlaceId"))
    inst = _find_first(data, ("gameInstanceId", "instanceId", "gameId",
                              "placeJobId", "jobId"))
    if place is None:
        return None, None
    return str(place), (str(inst) if inst else None)


def fetch_public_servers(place_id: str, cookie: str | None = None,
                         want: int = 50, min_free: int = 4, log=None) -> list[str]:
    """Return a list of job IDs (server instance IDs) for public running
    servers of a place, ordered lowest-ping first (with room).

    Only servers with room are kept. Note: Roblox's listed ping is sometimes
    inaccurate, so the ordering is best-effort. Returns [] on any error.
    """
    base = (f"https://games.roblox.com/v1/games/{place_id}/servers/Public"
            f"?sortOrder=Desc&limit=100")
    # Keep ping, available slots, and server ID for each joinable server.
    rated: list[tuple[float, int, str]] = []   # has a real ping value
    unrated: list[tuple[int, str]] = []        # no ping data → order by room
    cursor = ""
    pages = 0
    total_seen = 0
    rej_full = 0
    try:
        while pages < 6 and (len(rated) + len(unrated)) < want:
            url = base + (f"&cursor={cursor}" if cursor else "")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            if cookie:
                req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            rows = data.get("data", [])
            total_seen += len(rows)
            for s in rows:
                jid = s.get("id")
                if not jid:
                    continue
                # Prefer the player-token sample for occupancy; on this endpoint,
                # "playing" often mirrors maxPlayers instead of the live count.
                mx = (s.get("maxPlayers") or s.get("maxPlayerCount")
                      or s.get("capacity") or 0)
                mx = int(mx or 0)
                if mx <= 0:
                    mx = DEFAULT_SERVER_CAP
                toks = s.get("playerTokens")
                if isinstance(toks, list):
                    pl = len(toks)
                else:
                    pl = s.get("playing")
                    if pl is None:
                        pl = s.get("playerCount") or 0
                pl = int(pl or 0)
                free = mx - pl
                # Token counts are sampled, so they cannot prove a server is
                # empty. A count at capacity does prove it is full; reject full
                # and nearly full servers to reduce matchmaking collisions.
                if free < max(2, min_free):
                    rej_full += 1
                    continue
                ping = s.get("ping")
                # Ignore implausible ping values rather than ranking on bad data.
                if isinstance(ping, (int, float)) and 0 < ping < 400:
                    rated.append((float(ping), free, str(jid)))
                else:
                    unrated.append((free, str(jid)))
            cursor = data.get("nextPageCursor") or ""
            pages += 1
            if not cursor:
                break
    except urllib.error.HTTPError as e:
        if log:
            log(f"server list HTTP {e.code} (the game may hide its server list, "
                f"or the cookie was rejected).")
    except Exception as e:
        if log:
            log(f"server list fetch failed: {type(e).__name__}.")

    # Prefer the lowest valid ping. Use free capacity as a tie-breaker, and
    # prefer servers with the requested spare capacity when enough are available.
    rated.sort(key=lambda t: (t[0], -t[1]))    # ping asc, then most-empty
    roomy_rated = [t for t in rated if t[1] >= min_free]
    use = roomy_rated if len(roomy_rated) >= want else rated
    unrated.sort(key=lambda t: -t[0])          # most-empty first (no ping info)
    result = [jid for _, _, jid in use] + [jid for _, jid in unrated]

    if log:
        if use:
            best = int(use[0][0]) if isinstance(use[0], tuple) and len(use[0]) == 3 else None
            worst = int(use[-1][0]) if isinstance(use[-1], tuple) and len(use[-1]) == 3 else None
            png = (f", ping {best}–{worst} ms" if best is not None else "")
        else:
            png = ""
        log(f"server list: saw {total_seen}, {len(result)} joinable "
            f"({len(roomy_rated)} roomy+rated, {rej_full} full){png}.")
    return result


def _interruptible_sleep(seconds: float, stop=None) -> bool:
    """Sleep in small slices, checking the stop callable between them. Returns
    True if a stop was requested (so callers can abort), False if it slept the
    full duration. Keeps network-probe pauses from blocking a Stop request."""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        try:
            if stop and stop():
                return True
        except Exception:
            pass
        time.sleep(min(0.1, end - time.time()) if end > time.time() else 0)
    return bool(stop and stop()) if stop else False


def fetch_presence(user_ids: list[int], cookie: str) -> dict[int, str]:
    """Return {userId: gameId} for the accounts that are currently in-game.

    The presence API reports the ACTUAL server (gameId / jobId) each user is in
    — this is ground truth, unlike the server we *requested* at launch (Roblox
    can silently reroute a join into matchmaking). Used to detect when two
    accounts ended up in the same server. Best-effort: returns {} on failure.
    """
    out: dict[int, str] = {}
    ids = [int(u) for u in user_ids if u]
    if not ids:
        return out
    url = "https://presence.roblox.com/v1/presence/users"
    body = json.dumps({"userIds": ids}).encode()

    def _try(token):
        resp = _post(url, cookie, token, body)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    data = None
    try:
        data = _try(None)
    except urllib.error.HTTPError as e:
        tok = e.headers.get("x-csrf-token")
        if tok:
            try:
                data = _try(tok)
            except Exception:
                return out
        else:
            return out
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for p in data.get("userPresences", []):
        # Presence type 2 means in-game; gameId identifies the server instance.
        if p.get("userPresenceType") == 2:
            uid = p.get("userId")
            gid = p.get("gameId")
            if uid and gid:
                out[int(uid)] = str(gid)
    return out


def presence_detail(user_id: int, cookie: str
                    ) -> tuple[str, set[str], str | None, str | None]:
    """One-account presence with a definite state, which place it's in, AND the
    server (gameId/jobId) and experience root place:
        ("ingame", {placeId, rootPlaceId}, gameId, rootPlaceId)
        ("out", set(), None, None) – online/away/offline
        ("unknown", set(), None, None) – lookup failed; callers must NOT treat
                    this as 'left'/'wrong game'/'collision'.
    The place set holds both placeId and rootPlaceId (as strings) so callers can
    match a configured place whether it's a root or a sub-place. gameId is the
    server instance, used to detect two accounts sharing a server. The fourth
    value is rootPlaceId, used to resolve the current experience's universe."""
    ids = [int(user_id)] if user_id else []
    if not ids:
        return "unknown", set(), None, None
    url = "https://presence.roblox.com/v1/presence/users"
    body = json.dumps({"userIds": ids}).encode()

    def _try(token):
        resp = _post(url, cookie, token, body)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    data = None
    try:
        data = _try(None)
    except urllib.error.HTTPError as e:
        tok = e.headers.get("x-csrf-token")
        if not tok:
            return "unknown", set(), None, None
        try:
            data = _try(tok)
        except Exception:
            return "unknown", set(), None, None
    except Exception:
        return "unknown", set(), None, None
    if not isinstance(data, dict):
        return "unknown", set(), None, None
    for p in data.get("userPresences", []):
        if p.get("userId") and int(p["userId"]) == int(user_id):
            # Only presence type 2 is in-game; other Roblox presence states are out.
            if p.get("userPresenceType") != 2:
                return "out", set(), None, None
            places = {str(p[k]) for k in ("placeId", "rootPlaceId")
                      if p.get(k)}
            gid = p.get("gameId")
            root_place = p.get("rootPlaceId")
            return ("ingame", places, (str(gid) if gid else None),
                    str(root_place) if root_place else None)
    return "out", set(), None, None


def presence_state(user_id: int, cookie: str) -> str:
    """Return only the presence state, ignoring place and server details."""
    return presence_detail(user_id, cookie)[0]


def detect_last_place() -> str | None:
    try:
        files = sorted(LOG_DIR.glob("*.log"), key=lambda f: -f.stat().st_mtime)
    except OSError:
        return None
    for f in files[:12]:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = JOIN_RE.findall(text)
        if hits:
            return hits[-1][1]
    return None
