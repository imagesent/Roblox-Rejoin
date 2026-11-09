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

from rejoiner_constants import (AUTH_TICKET_URL, USERS_AUTH_URL, SHARE_RESOLVE_URL,
                      PRIVATE_SERVERS_API, VIP_SERVERS_API, PLACE_LAUNCHER_URL, UA,
                      DEFAULT_SERVER_CAP, LOG_DIR, JOIN_RE, JOIN_RESOLVE_ATTEMPTS,
                      JOIN_RESOLVE_WAIT, JOIN_PRIVATE_PAGES, JOIN_PRIVATE_PAGE_SIZE)
import roblox_error_codes as error_codes


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
                     link_code: str | None = None,
                     browser_tracker_id: int | None = None) -> str | None:
    """Build the roblox-player launch URI, matching the exact format used by
    Roblox Account Manager (the widely-working multi-account tool).

    Three ways to target a server:
      * open matchmaking    -> request=RequestGame&placeId=…&isPlayTogetherGame=false
      * one specific server -> request=RequestGameJob&placeId=…&gameId=<jobId>
      * private server      -> request=RequestGameJob&placeId=…&linkCode=<link code>
    A private server is joined through its VIP link code, exactly like the
    Roblox website does. That code is only honoured in that one spot: sending
    it as gameId, or leaving it out, quietly drops the client into public
    matchmaking instead of the private server — which is the bug this keeps
    users out of. The three cases therefore stay strictly separate.

    Returns None when the place id or codes contain characters Roblox would
    reject, which would otherwise corrupt the whole URI.
    """
    place_id = str(place_id or "").strip()
    link_code = str(link_code or "").strip()
    instance_id = str(instance_id or "").strip()
    if not PLACE_ID_RE.fullmatch(place_id):
        return None
    if link_code and not LINK_CODE_RE.fullmatch(link_code):
        return None
    if instance_id and not JOB_ID_RE.fullmatch(instance_id):
        return None
    params = {"placeId": place_id, "isPlayTogetherGame": "false"}
    if link_code:
        # Private server: the VIP link code selects the server instance.
        params["request"] = "RequestGameJob"
        params["linkCode"] = link_code
    elif instance_id:
        # One specific running (public) server instance.
        params["request"] = "RequestGameJob"
        params["gameId"] = instance_id
    else:
        # Let Roblox matchmaking choose an available server.
        params["request"] = "RequestGame"
    pl = PLACE_LAUNCHER_URL + "?" + urllib.parse.urlencode(params)
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


PLACE_ID_RE = re.compile(r"[0-9]{1,20}")
JOB_ID_RE = re.compile(r"[0-9A-Za-z\-]{1,64}")
LINK_CODE_RE = re.compile(r"[0-9A-Za-z]{1,64}")


# Every shape Roblox uses to hand out a link to a specific server. Only the
# direct /games/…?privateServerLinkCode= form carries both the place and the
# server; share links (/share?code=…) must be resolved through Roblox's own
# API, which returns the place id together with the private-server link code.
_LINK_PATTERNS = (
    ("private", re.compile(
        r"roblox\.com/games/(?P<place>[0-9]{1,20})/[^?#]*\?[^#]*"
        r"privateServerLinkCode=(?P<code>[0-9A-Za-z]{1,64})", re.I)),
    ("private", re.compile(
        r"placelauncherurl:[^+]*placeId=(?P<place>[0-9]{1,20})[^+]*"
        r"linkCode=(?P<code>[0-9A-Za-z]{1,64})", re.I)),
    ("private", re.compile(
        r"placeId[=:](?P<place>[0-9]{1,20})[^#]*linkCode=(?P<code>[0-9A-Za-z]{1,64})",
        re.I)),
    ("share", re.compile(
        r"roblox\.com/share[^?#]*[?&]code=(?P<code>[0-9A-Za-z]{1,64})", re.I)),
    ("job", re.compile(
        r"/games/(?P<place>[0-9]{1,20})[^?#]*\?[^#]*"
        r"(?:gameInstanceId|gameId)=(?P<job>[0-9A-Za-z\-]{1,64})", re.I)),
)
_SHARE_TYPE_RE = re.compile(r"[?&]type=([A-Za-z]+)", re.I)
_BARE_CODE_RE = re.compile(r"[0-9A-Za-z]{10,64}")
# Response keys that hold a private-server link / access code, or a job id.
_LINK_CODE_KEYS = ("linkCode", "linkcode", "privateServerLinkCode",
                   "privateServerLinkcode", "accessCode")
_JOB_KEYS = ("gameInstanceId", "instanceId", "gameId", "placeJobId", "jobId")


class ParsedLink:
    """A link split into the pieces a launch needs (before any API call)."""

    __slots__ = ("raw", "kind", "place", "code", "link_type", "job_id")

    def __init__(self, raw: str, kind: str, place: str = "", code: str = "",
                 link_type: str = "", job_id: str = ""):
        self.raw = raw
        self.kind = kind                  # "private" | "share" | "job" | "open"
        self.place = place                # place id when the link carries one
        self.code = code                  # share link code (needs resolving)
        self.link_type = link_type        # Server / ExperienceInvite / …
        self.job_id = job_id              # one specific running server


class JoinTarget:
    """Where one client should be launched.

    kind "private" targets a private (VIP) server through its link code,
    kind "job" targets one specific running public server, and kind "open"
    lets matchmaking pick any server of the place. `job_id` is filled in once
    Roblox confirms which server the target really is, so the engine can
    prove a join landed in the right place instead of trusting the request.
    """

    __slots__ = ("place", "kind", "link_code", "access_code", "job_id",
                 "universe", "name", "source", "error", "notes")

    def __init__(self, place: str = "", kind: str = "open",
                 link_code: str = "", access_code: str = "", job_id: str = "",
                 universe: str = "", name: str = "", source: str = "",
                 error: str = "", notes: str = ""):
        self.place = str(place or "")
        self.kind = kind
        self.link_code = str(link_code or "")
        self.access_code = str(access_code or "")
        self.job_id = str(job_id or "")
        self.universe = str(universe or "")
        self.name = str(name or "")
        self.source = str(source or "")
        self.error = str(error or "")
        self.notes = str(notes or "")

    @property
    def ok(self) -> bool:
        return bool(self.place) and not self.error

    @property
    def specific(self) -> bool:
        """True when the target is one exact server, not any of them."""
        return self.kind in ("private", "job")

    def key(self) -> str:
        """Cache key: one resolution per distinct place + server target."""
        return f"{self.place}|{self.kind}|{self.link_code}|{self.job_id}"

    def label(self) -> str:
        if self.kind == "private":
            return f"private server {self.link_code[:12]}…"
        if self.kind == "job":
            return f"server {self.job_id[:12]}…"
        return f"any server of place {self.place}"

    def describe(self) -> str:
        """One-line summary for logs, the UI and Discord embeds."""
        if not self.ok:
            return self.error or "unresolved server"
        if self.kind == "private":
            base = "private server"
        elif self.kind == "job":
            base = "specific server"
        else:
            base = "place"
        tail = f" · server {self.job_id[:12]}…" if self.job_id else ""
        return f"{base} {self.place}{tail}"


def parse_join_link(raw: str) -> ParsedLink | None:
    """Split any accepted link (or bare code) into what a launch needs.

    Accepts the direct private-server URL, the /share?code=… form (with or
    without &type=), the roblox://experiences/start deep link, a share link
    packed into a roblox-player launch URI, a place URL carrying a job id, a
    bare share code, or a bare place id. Returns None when nothing matches.
    """
    text = (raw or "").strip()
    if not text:
        return None
    for kind, pattern in _LINK_PATTERNS:
        m = pattern.search(text)
        if m:
            fields = m.groupdict()
            if kind == "private":
                return ParsedLink(text, "private",
                                  place=fields.get("place", ""),
                                  code=fields.get("code", ""),
                                  link_type="Server")
            if kind == "share":
                tm = _SHARE_TYPE_RE.search(text)
                return ParsedLink(text, "share", code=fields.get("code", ""),
                                  link_type=(tm.group(1) if tm else "Server"))
            return ParsedLink(text, "job", place=fields.get("place", ""),
                              job_id=fields.get("job", ""))
    # A bare code can only mean a share link (Roblox's own land page hands
    # these out as plain hex strings).
    if text.isdigit():
        if PLACE_ID_RE.fullmatch(text):
            return ParsedLink(text, "open", place=text)
        return ParsedLink(text, "share", code=text, link_type="Server")
    if _BARE_CODE_RE.fullmatch(text):
        return ParsedLink(text, "share", code=text, link_type="Server")
    # Last resort: a place id anywhere in the text (e.g. a game permalink
    # with tracking junk the patterns above refuse).
    pm = re.search(r"/games/([0-9]{1,20})", text)
    if pm:
        return ParsedLink(text, "open", place=pm.group(1))
    return None


def _pluck(body: str, keys: tuple, value_re: str) -> str | None:
    """First `"<key>": "<value>"` from a raw response body.

    The value is matched both quoted and bare: Roblox returns place ids as
    numbers and link codes as strings, so a single shape would miss half of
    them."""
    for k in keys:
        for pattern in (rf'"{k}"\s*:\s*"({value_re})"',
                        rf'"{k}"\s*:\s*({value_re})'):
            m = re.search(pattern, body, re.I)
            if m:
                return m.group(1)
    return None


def resolve_share_api(cookie: str, code: str, link_type: str,
                      log=None) -> JoinTarget | None:
    """Resolve a /share?code=… link against Roblox's share-link API.

    The response nests the useful fields differently per link type (a private
    server invite answers with privateServerInviteData.linkCode + placeId, an
    experience invite with experienceInviteData), so the raw body is searched
    by key name rather than by a fixed path. Returns None when Roblox does
    not answer, so the caller can distinguish retry-worthy failures.
    """
    payload = json.dumps({"linkId": code,
                          "linkType": link_type or "Server"}).encode()

    def _try(token):
        resp = _post(SHARE_RESOLVE_URL, cookie, token, payload)
        return resp.read().decode("utf-8", "ignore")

    body = ""
    try:
        body = _try(get_csrf(cookie))
    except urllib.error.HTTPError as e:
        new = e.headers.get("x-csrf-token")
        if new:
            try:
                body = _try(new)
            except urllib.error.HTTPError as e2:
                if log:
                    log(f"share-link resolve HTTP {e2.code}.")
            except Exception as e:
                if log:
                    log(f"share-link resolve failed: {type(e).__name__}.")
        elif log:
            log(f"share-link resolve HTTP {e.code}.")
    except Exception as e:
        if log:
            log(f"share-link resolve failed: {type(e).__name__}.")
    if not body:
        return None
    place = (_pluck(body, ("placeId", "rootPlaceId"), "[0-9]{1,20}") or "")
    link_code = (_pluck(body, _LINK_CODE_KEYS, "[0-9A-Za-z]{1,64}") or "")
    job = (_pluck(body, _JOB_KEYS, r"[0-9A-Za-z\-]{1,64}") or "")
    universe = (_pluck(body, ("universeId",), "[0-9]{1,20}") or "")
    if not place:
        return None
    if link_code:
        return JoinTarget(place=place, kind="private", link_code=link_code,
                          universe=universe, source=code)
    if job:
        return JoinTarget(place=place, kind="job", job_id=job,
                          universe=universe, source=code)
    return JoinTarget(place=place, kind="open", universe=universe, source=code)


def _sleep(seconds: float, stop=None) -> None:
    """Wait between retries, but cut the wait short when asked to stop.

    `stop` may be a threading.Event (the usual caller) or any callable, and a
    caller that gives neither simply blocks."""
    if stop is None:
        time.sleep(seconds)
        return
    if callable(getattr(stop, "wait", None)):
        try:
            stop.wait(seconds)
            return
        except Exception:
            pass
    try:
        if stop():
            return
    except Exception:
        pass
    time.sleep(seconds)


def resolve_join_link(cookie: str, raw: str, attempts: int = JOIN_RESOLVE_ATTEMPTS,
                      stop=None, log=None) -> JoinTarget:
    """Turn one link into a launchable target, retrying transient API errors.

    A link that cannot be resolved is a hard failure: silently falling back
    to public matchmaking is what dumped people into random servers before.
    """
    parsed = parse_join_link(raw)
    if parsed is None:
        return JoinTarget(error="that isn't a share / private-server link")
    # Direct links already carry everything needed — no API call required.
    if parsed.kind in ("private", "job") and parsed.place:
        universe = universe_id_for_place(parsed.place) or ""
        if parsed.kind == "private":
            target = JoinTarget(place=parsed.place, kind="private",
                                link_code=parsed.code, universe=universe,
                                source=parsed.raw)
        else:
            target = JoinTarget(place=parsed.place, kind="job",
                               job_id=parsed.job_id, universe=universe,
                                source=parsed.raw)
        if target.kind == "job" and not target.job_id:
            return JoinTarget(error="that link has no server id in it")
        return target
    if parsed.kind == "open":
        return JoinTarget(place=parsed.place, kind="open",
                          universe=universe_id_for_place(parsed.place) or "",
                          source=parsed.raw)
    if not cookie:
        return JoinTarget(error="no account cookie to resolve the link with")
    last = ""
    for attempt in range(max(1, int(attempts))):
        target = resolve_share_api(cookie, parsed.code, parsed.link_type, log=log)
        if target is not None:
            target.source = parsed.raw
            return target
        last = "Roblox would not resolve that share link"
        if not (stop is not None and stop()) and attempt < int(attempts) - 1:
            _sleep(JOIN_RESOLVE_WAIT, stop)
    return JoinTarget(error=last or "couldn't resolve that link")


def resolve_share_link(cookie: str, code: str, link_type: str
                       ) -> tuple[str | None, str | None]:
    """Backwards-compatible helper: (place_id, instance_id) or (None, None)."""
    target = resolve_join_link(cookie, code, attempts=1)
    if not target.ok:
        return None, None
    return target.place, (target.job_id or target.link_code or None)


def probe_join_server(cookie: str, place_id: str, link_code: str = "",
                      job_id: str = "", timeout: int = 15
                      ) -> tuple[str | None, int | None, str]:
    """Ask PlaceLauncher which server a link actually points at.

    Returns (job_id, status, error). Roblox answers with the job id of the
    private server (or of the requested instance) plus a status code — the
    same lookup the "Check link" button uses, and the only way to know which
    server the accounts are supposed to compare against.
    """
    params = {"request": "RequestPrivateGame", "placeId": str(place_id)}
    if link_code:
        params["linkCode"] = link_code
    elif job_id:
        params["request"] = "RequestGameJob"
        params["gameId"] = job_id
    else:
        return None, None, "nothing to check"
    url = PLACE_LAUNCHER_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        if cookie:
            req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return None, None, f"PlaceLauncher HTTP {e.code}"
    except Exception as e:
        return None, None, f"PlaceLauncher unreachable ({type(e).__name__})"
    try:
        data = json.loads(body)
    except ValueError:
        return None, None, "PlaceLauncher returned nothing useful"
    if not isinstance(data, dict):
        return None, None, "PlaceLauncher returned nothing useful"
    job = data.get("jobId")
    status = data.get("status")
    err = data.get("message") or data.get("Error")
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return (str(job) if job else None, status,
            str(err) if err and status not in (2,) else "")


def _get_json(url: str, cookie: str | None = None, timeout: int = 15):
    """GET one JSON document. Returns (data_or_None, error_string)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if cookie:
        req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}"
    try:
        return json.loads(raw), ""
    except ValueError:
        return None, "unreadable response"


def _patch_json(url: str, cookie: str, body: dict,
                timeout: int = 15) -> tuple[dict | None, str]:
    """PATCH one JSON document with a CSRF token. (data_or_None, error)."""
    csrf = get_csrf(cookie)
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method="PATCH")
    req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Referer", "https://www.roblox.com/")
    req.add_header("Origin", "https://www.roblox.com")
    req.add_header("User-Agent", UA)
    if csrf:
        req.add_header("X-CSRF-TOKEN", csrf)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}"
    if not raw:
        return {}, ""
    try:
        data = json.loads(raw)
        return (data if isinstance(data, dict) else {}), ""
    except ValueError:
        return None, "unreadable response"


def _vip_server_link(detail: dict, entry: dict, place_id: str = "") -> str:
    """Build a usable private-server link from Roblox's server fields."""
    def _link_value(data: dict) -> str:
        for key in ("link", "privateServerLink", "shareLink"):
            value = data.get(key) if isinstance(data, dict) else None
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if value.startswith("/"):
                value = "https://www.roblox.com" + value
            elif value.startswith(("roblox.com/", "www.roblox.com/")):
                value = "https://" + value
            parsed = urllib.parse.urlparse(value)
            if (parsed.scheme == "https"
                    and parsed.hostname in ("roblox.com", "www.roblox.com")):
                return value
        return ""

    for data in (detail or {}, entry or {}):
        link = _link_value(data)
        if link:
            return link
    place = ""
    for data in (detail or {}, entry or {}):
        game = data.get("game") or {}
        root = game.get("rootPlace") or {}
        place = str(root.get("id") or data.get("placeId") or "")
        if place.isdigit():
            break
    if not place:
        place = str(place_id or "")
    code = ""
    for data in (detail or {}, entry or {}):
        for key in ("joinCode", "linkCode", "privateServerLinkCode"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                code = value.strip()
                break
        if code:
            break
    if place.isdigit() and code:
        return (f"https://www.roblox.com/games/{place}?"
                + urllib.parse.urlencode({"privateServerLinkCode": code}))
    return ""


def fetch_private_servers(cookie: str, user_id="", place_id: str = "",
                          limit: int = JOIN_PRIVATE_PAGE_SIZE
                          ) -> tuple[list[dict], str]:
    """List private servers an account can use: (rows, error).

    With a place id, the list is scoped to that game and filtered to servers
    the account owns; without one, every server the account owns is listed.
    Each row carries a join link so the UI can apply it directly.
    """
    if not cookie:
        return [], "this account has no saved cookie"
    place_id = str(place_id or "").strip()
    user_id = str(user_id or "").strip()
    if place_id and not place_id.isdigit():
        return [], "enter a numeric Place ID or leave it blank"
    if place_id and not user_id.isdigit():
        return [], "refresh this account before searching by place"
    params = {"limit": max(1, min(10, limit))}
    if place_id:
        url = PRIVATE_SERVERS_API + f"/v1/games/{place_id}/private-servers"
    else:
        params = {"itemsPerPage": max(1, min(10, limit)),
                  "privateServersTab": "MyPrivateServers"}
        url = PRIVATE_SERVERS_API + "/v1/private-servers/my-private-servers"
    rows: list[dict] = []
    seen: set[str] = set()
    cursor = ""
    for _ in range(JOIN_PRIVATE_PAGES):
        if cursor:
            params["cursor"] = cursor
        data, err = _get_json(url + "?" + urllib.parse.urlencode(params), cookie)
        if err or not isinstance(data, dict):
            return rows, (err or "unreadable server list")
        entries = data.get("data")
        if not isinstance(entries, list):
            return rows, "unreadable server list"
        for entry in entries:
            if len(rows) >= limit:
                return rows, ""
            if not isinstance(entry, dict):
                continue
            if place_id and str((entry.get("owner") or {}).get("id")) != user_id:
                continue
            # Listing ids and VIP server ids are different numbers.
            server_id = str(entry.get("vipServerId") if place_id
                            else entry.get("privateServerId") or "")
            if not server_id.isdigit() or server_id in seen:
                continue
            seen.add(server_id)
            detail, derr = _get_json(
                VIP_SERVERS_API + f"/v1/vip-servers/{server_id}", cookie)
            if derr or not isinstance(detail, dict):
                # Private servers disabled for this game are simply skipped.
                continue
            game = detail.get("game") or {}
            root = game.get("rootPlace") or {}
            rows.append({
                "id": server_id,
                "place_id": str(root.get("id") or entry.get("placeId")
                                or place_id or ""),
                "game": str(game.get("name") or root.get("name")
                            or "Unknown game"),
                "name": str(detail.get("name") or entry.get("name")
                            or f"server {server_id}"),
                "active": bool(detail.get("active")),
                "link": _vip_server_link(detail, entry, place_id),
            })
            if len(rows) >= limit:
                return rows, ""
        cursor = data.get("nextPageCursor")
        if not cursor or not isinstance(cursor, str):
            break
    return rows, ""


def generate_private_server_link(cookie: str, server_id: str,
                                place_id: str = "") -> str:
    """Ask Roblox for a fresh join link for a private server.

    Roblox only hands out a link when one is requested (PATCH newJoinCode);
    returns "" when it refuses.
    """
    server_id = str(server_id or "").strip()
    if not server_id.isdigit():
        return ""
    detail, err = _patch_json(VIP_SERVERS_API + f"/v1/vip-servers/{server_id}",
                              cookie, {"newJoinCode": True})
    if err or not isinstance(detail, dict) or not detail:
        detail, derr = _get_json(
            VIP_SERVERS_API + f"/v1/vip-servers/{server_id}", cookie)
        if derr or not isinstance(detail, dict):
            return ""
    return _vip_server_link(detail, {}, place_id)

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


def latest_client_error(window: float = 120.0) -> str | None:
    """Crash hint: the last error marker written by a client that just died.

    A RUNNING client keeps writing its log, so logs modified within the last
    ~20s belong to live clients. This looks at logs that stopped updating
    recently (modified within `window`, but not in the last 20s) — those
    belong to a client that just exited — and returns its final error marker
    ("Error Code: 279", "kicked by", …), or None.
    """
    try:
        files = list(LOG_DIR.glob("*.log"))
    except OSError:
        return None
    now = time.time()
    candidates = []
    for f in files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if now - window <= mtime <= now - 20:
            candidates.append((mtime, f))
    candidates.sort(reverse=True)
    for _mtime, f in candidates[:3]:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)                      # end of file
                size = fh.tell()
                fh.seek(max(0, size - 65536))      # tail only
                text = fh.read(65536).decode("utf-8", "ignore")
        except OSError:
            continue
        hint = error_codes.log_hint(text)
        if hint:
            return hint
    return None
