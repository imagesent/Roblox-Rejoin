"""Discord webhook notifications for Roblox Rejoiner.

Sends event notifications (launches, crashes, kicks, disconnects, kills,
stops) to a user-configured webhook as color-coded embeds so multiple
accounts stay readable. Only usernames and events are ever sent — never
cookies.

Design notes:
- One background sender thread owns every outgoing message. Callers push
  onto a queue and never block the monitor loop, and messages keep their
  order instead of racing each other as separate threads.
- Discord's 429 rate-limit responses are honored (retry_after) with a
  small retry budget per message; hopeless messages are dropped rather
  than stalling the queue.
- Embed builders enforce Discord's limits (field value 1024, description
  4096, 25 fields, 6000 chars total) so long account lists are chunked
  instead of silently rejected.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid

from rejoiner_constants import DISCORD_HOSTS

# Discord hard limits (see discord developer docs, embed limits).
_FIELD_NAME_MAX = 256
_FIELD_VALUE_MAX = 1024
_DESC_MAX = 4096
_TITLE_MAX = 256
_FIELDS_MAX = 25
_TOTAL_MAX = 6000

# Event color palette (hex ints, Discord embed colors).
COLOR_GOOD = 0x2ECC71      # green — joined / recovered
COLOR_INFO = 0x3498DB      # blue — monitoring started / info
COLOR_WARN = 0xE67E22      # orange — kick, kill, server move, retry
COLOR_BAD = 0xE74C3C       # red — crash / disconnect
COLOR_MUTED = 0x95A5A6     # gray — stopped / neutral


def _is_discord_webhook(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    if p.scheme != "https" or p.netloc.lower() not in DISCORD_HOSTS:
        return False
    return "/api/webhooks/" in p.path


def _clamp(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Embed construction helpers.
# ---------------------------------------------------------------------------

def make_embed(title: str = "", description: str = "", color: int = COLOR_INFO,
               fields: list[tuple] | None = None,
               footer: str = "", timestamp: bool = True) -> dict:
    """Build a Discord embed dict, enforcing per-part limits.

    `fields` is a list of (name, value) or (name, value, inline) tuples.
    Long values are chunked across continuation fields so nothing is lost.
    """
    emb: dict = {"type": "rich"}
    if title:
        emb["title"] = _clamp(title, _TITLE_MAX)
    if description:
        emb["description"] = _clamp(description, _DESC_MAX)
    emb["color"] = int(color) & 0xFFFFFF
    clean: list[dict] = []
    for f in fields or []:
        name, value = f[0], f[1]
        inline = bool(f[2]) if len(f) > 2 else False
        name = _clamp(name or "\u200b", _FIELD_NAME_MAX)
        value = str(value if value not in (None, "") else "\u200b")
        # Chunk oversized values across sibling fields ("name", "name (cont)").
        chunk = max(1, _FIELD_VALUE_MAX - 1)
        part = 0
        while value and len(clean) < _FIELDS_MAX - 1:
            fname = name if part == 0 else f"{name} (cont.)"
            clean.append({"name": _clamp(fname, _FIELD_NAME_MAX),
                          "value": _clamp(value[:chunk], _FIELD_VALUE_MAX),
                          "inline": inline})
            value = value[chunk:]
            part += 1
        if value:
            clean.append({"name": _clamp(f"{name} (truncated)", _FIELD_NAME_MAX),
                          "value": _clamp(value, _FIELD_VALUE_MAX),
                          "inline": inline})
        if len(clean) >= _FIELDS_MAX:
            break
    if clean:
        emb["fields"] = clean[:_FIELDS_MAX]
    if footer:
        emb["footer"] = {"text": _clamp(footer, _TITLE_MAX)}
    if timestamp:
        emb["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                         time.gmtime())
    return emb


def account_lines(entries: list[tuple[str, str]]) -> str:
    """Format multi-account summaries with one line per account AND a blank
    line between entries, so several accounts never blur together.

    `entries` is a list of (title, detail); renders as:
        **name**
        detail

        **name2**
        detail2
    """
    parts = []
    for name, detail in entries:
        line = f"**{_clamp(name, 80)}**"
        if detail:
            line += f"\n{_clamp(detail, 400)}"
        parts.append(line)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Sending.
# ---------------------------------------------------------------------------

def with_thumbnail(embed: dict, filename: str) -> dict:
    """Point an embed's thumbnail at an attached file (attachment:// URL)."""
    embed["thumbnail"] = {"url": f"attachment://{filename}"}
    return embed


def _encode_multipart(payload: dict,
                      files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """Encode payload_json + binary attachments as multipart/form-data."""
    boundary = "RobloxRejoiner" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(b'Content-Disposition: form-data; name="payload_json"\r\n')
    buf.write(b"Content-Type: application/json\r\n\r\n")
    buf.write(json.dumps(payload).encode("utf-8"))
    buf.write(b"\r\n")
    for i, (filename, data) in enumerate(files):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="files[{i}]"; '
            f'filename="{filename}"\r\n'.encode())
        buf.write(b"Content-Type: image/png\r\n\r\n")
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), boundary


def _post_payload(url: str, payload: dict,
                  files: list[tuple[str, bytes]] | None = None
                  ) -> tuple[bool, str]:
    """POST once to the webhook. Returns (ok, error_message)."""
    if files:
        data, boundary = _encode_multipart(payload, files)
        content_type = f"multipart/form-data; boundary={boundary}"
    else:
        data = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("User-Agent", "Roblox-Rejoiner")
    try:
        urllib.request.urlopen(req, timeout=12).read()
        return True, ""
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                body = json.loads(e.read().decode("utf-8", "ignore"))
                retry = float(body.get("retry_after", 1.0))
            except Exception:
                retry = 1.0
            return False, f"__RATE_LIMIT__{min(retry, 30.0)}"
        if e.code in (400, 413):
            return False, f"Discord rejected the message (HTTP {e.code})."
        return False, f"Discord returned HTTP {e.code}."
    except Exception as e:
        return False, f"Send failed: {e}"


def _deliver(url: str, payload: dict,
             files: list[tuple[str, bytes]] | None = None
             ) -> tuple[bool, str]:
    """Send with a small retry budget; honors 429 retry_after."""
    for attempt in range(4):
        ok, err = _post_payload(url, payload, files)
        if ok:
            return True, ""
        if err.startswith("__RATE_LIMIT__"):
            time.sleep(min(float(err.split("__", 2)[-1] or 1.0), 30.0))
            continue
        if attempt < 3 and (err.startswith("Send failed")
                            or "HTTP 5" in err):
            time.sleep(1.5 * (attempt + 1))   # transient network / 5xx
            continue
        return False, err
    return False, "Discord kept rate-limiting this webhook; message dropped."


# Single background sender: ordered, non-blocking, 429-aware.
_q: "queue.Queue" = queue.Queue()          # (url, payload, files) or None
_q_thread_started = False
_q_lock = threading.Lock()


def _sender_loop() -> None:
    while True:
        item = _q.get()
        if item is None:
            return
        url, payload, files = item
        try:
            _deliver(url, payload, files)
        except Exception:
            pass


def _ensure_sender() -> None:
    global _q_thread_started
    with _q_lock:
        if not _q_thread_started:
            threading.Thread(target=_sender_loop, daemon=True,
                             name="discord-webhook-sender").start()
            _q_thread_started = True


def _build_payload(username: str, avatar: str,
                   content: str, embeds: list[dict]) -> dict:
    payload: dict = {"allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content[:1900]
    if embeds:
        payload["embeds"] = embeds
    if (username or "").strip():
        payload["username"] = username.strip()[:80]
    if (avatar or "").strip():
        payload["avatar_url"] = avatar.strip()
    return payload


def discord_post(url: str, username: str = "", avatar: str = "",
                 content: str = "", embeds: list[dict] | None = None,
                 files: list[tuple[str, bytes]] | None = None
                 ) -> tuple[bool, str]:
    """Synchronously POST content/embeds (+ optional PNG attachments) to a
    webhook. Returns (ok, error)."""
    url = (url or "").strip()
    if not _is_discord_webhook(url):
        return False, "Not a valid Discord webhook URL."
    if not content and not embeds:
        return False, "Nothing to send."
    return _deliver(url, _build_payload(username, avatar, content,
                                        embeds or []), files)


def discord_notify_embed(url: str, username: str = "", avatar: str = "",
                         content: str = "",
                         embeds: list[dict] | None = None,
                         files: list[tuple[str, bytes]] | None = None) -> None:
    """Fire-and-forget embed post through the shared sender thread."""
    url = (url or "").strip()
    if not url or not _is_discord_webhook(url):
        return
    if not content and not embeds:
        return
    _ensure_sender()
    _q.put((url, _build_payload(username, avatar, content, embeds or []),
            files))


# Back-compat plain-text helpers (still used by the webhook test button).
def discord_send(url: str, username: str, avatar: str,
                 content: str) -> tuple[bool, str]:
    """Synchronously POST a plain-text message to a Discord webhook."""
    return discord_post(url, username, avatar, content=content)


def discord_notify(url: str, username: str, avatar: str, content: str) -> None:
    """Fire-and-forget plain-text post (kept for compatibility)."""
    if not url:
        return
    threading.Thread(target=discord_send,
                     args=(url, username, avatar, content),
                     daemon=True).start()


# ---------------------------------------------------------------------------
# Rejoiner event embeds (used by the monitor engine).
# ---------------------------------------------------------------------------

def event_embed(kind: str, title: str, description: str = "",
                fields: list[tuple] | None = None, footer: str = "",
                color: int | None = None) -> dict:
    """Build a colored event embed. `kind` picks the color when no explicit
    color is given: good (joined), info (start), warn (kick/kill/moved),
    bad (crash/disconnect), muted (stopped)."""
    palette = {"good": COLOR_GOOD, "info": COLOR_INFO, "warn": COLOR_WARN,
               "bad": COLOR_BAD, "muted": COLOR_MUTED}
    return make_embed(title=title, description=description,
                      color=color if color is not None
                      else palette.get(kind, COLOR_INFO),
                      fields=fields, footer=footer)


def status_field(accounts: list[dict]) -> tuple[str, str]:
    """Build an 'Accounts' embed field from live state rows with readable
    spacing (blank line between each account) for multi-account sessions.

    Each row: bold name, state word, and detail (uptime, disconnects).
    """
    words = {"running": "in game", "launching": "launching",
             "leaving": "leaving", "waiting": "rejoining",
             "stopped": "stopped"}
    entries = []
    for st in accounts:
        state = st.get("state", "stopped")
        detail = words.get(state, state)
        up = st.get("uptime") or 0
        if state == "running" and up:
            m, s = divmod(int(up), 60)
            detail += f" \u00b7 up {m}m{s:02d}s" if m else f" \u00b7 up {s}s"
        crashes = int(st.get("crashes", 0) or 0)
        if crashes:
            detail += f" \u00b7 {crashes} disconnect" + ("s" if crashes != 1 else "")
        reason = st.get("reason")
        if reason and state != "running":
            detail += f" \u00b7 {reason}"
        entries.append((st.get("username", "?"), detail))
    return "Accounts", account_lines(entries)
