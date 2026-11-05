"""Roblox client error-code classification for Roblox Rejoiner.

Maps the error codes the Roblox client shows (or writes to its logs) to a
category that decides how the monitor reacts:

- kick        removed by the experience/moderation — rejoin right away
- disconnect  network/server drop — rejoin right away
- session     the account's old session is still clearing (264/265) — wait
              for presence to confirm it left, then rejoin
- retry       the experience itself is temporarily unavailable (updating,
              shut down, full, Roblox server error) — rejoin with a longer
              backoff so we don't hammer a game that can't accept joins
- restricted  joining cannot succeed until the user changes something
              (no permission, platform/country locked) — stop auto-rejoining
              for that account and say why
- account     the account itself is the problem (expired cookie, ban) —
              stop auto-rejoining and ask for a re-add

Unknown codes fall back to the plain disconnect behavior (rejoin), which is
always safe for a rejoiner: at worst it retries a join that fails again.
"""

from __future__ import annotations

import re

CATEGORY_KICK = "kick"
CATEGORY_DISCONNECT = "disconnect"
CATEGORY_SESSION = "session"
CATEGORY_RETRY = "retry"
CATEGORY_RESTRICTED = "restricted"
CATEGORY_ACCOUNT = "account"

# Categories that can never be fixed by relaunching the client.
_STOP_CATEGORIES = (CATEGORY_RESTRICTED, CATEGORY_ACCOUNT)

ERROR_CODES: dict[int, tuple[str, str]] = {
    # -- client / join lifecycle --
    6:   (CATEGORY_RETRY, "client failed to start"),
    100: (CATEGORY_DISCONNECT, "connection problem"),
    142: (CATEGORY_RETRY, "experience has been shut down"),
    # -- join restrictions --
    146: (CATEGORY_RESTRICTED, "no permission to join this experience"),
    272: (CATEGORY_RESTRICTED, "experience not available on this platform"),
    292: (CATEGORY_RESTRICTED, "experience not available in your country"),
    # -- session overlap (old session still registered) --
    264: (CATEGORY_SESSION, "already in this experience — old session still clearing"),
    265: (CATEGORY_SESSION, "account already connected from another session"),
    # -- moderation / script kicks --
    267: (CATEGORY_KICK, "kicked by the experience or its moderators"),
    268: (CATEGORY_KICK, "kicked for unexpected client behavior"),
    269: (CATEGORY_KICK, "kicked by the game"),
    270: (CATEGORY_KICK, "kicked by the game"),
    # -- server shutdown / availability --
    256: (CATEGORY_DISCONNECT, "server shut down"),
    260: (CATEGORY_DISCONNECT, "server shut down"),
    273: (CATEGORY_RETRY, "experience temporarily unavailable"),
    274: (CATEGORY_RETRY, "joining temporarily disabled by the creator"),
    275: (CATEGORY_RETRY, "experience temporarily unavailable"),
    293: (CATEGORY_RETRY, "experience temporarily unavailable"),
    294: (CATEGORY_RETRY, "experience reached its player limit"),
    # -- network drops --
    277: (CATEGORY_DISCONNECT, "lost connection to the game"),
    279: (CATEGORY_RETRY, "failed to connect to the game"),
    280: (CATEGORY_DISCONNECT, "disconnected from the server"),
    282: (CATEGORY_DISCONNECT, "teleport failed"),
    286: (CATEGORY_DISCONNECT, "connection to the server timed out"),
    288: (CATEGORY_DISCONNECT, "disconnected while teleporting"),
    290: (CATEGORY_RETRY, "graphics or driver issue"),
    # -- Roblox platform errors (join API) --
    500: (CATEGORY_RETRY, "Roblox server error"),
    502: (CATEGORY_RETRY, "Roblox server error"),
    503: (CATEGORY_RETRY, "Roblox server error"),
    504: (CATEGORY_RETRY, "Roblox server error"),
    507: (CATEGORY_RETRY, "Roblox server error"),
}

DEFAULT_CATEGORY = CATEGORY_DISCONNECT
DEFAULT_LABEL = "removed from the game"

_ERROR_CODE_RE = re.compile(r"error code\s*:?\s*(\d{1,4})", re.IGNORECASE)

# Phrases a log line can contain that identify why a client died, used when
# the client dies without leaving a dialog open.
_LOG_HINT_RE = re.compile(
    r"Error Code:?\s*\d{1,4}|kicked by|Lost connection|"
    r"Disconnected from|join failed|Failed to join",
    re.IGNORECASE)


def classify(code: int | None) -> tuple[str, str]:
    """Return (category, friendly label) for a client error code."""
    if code is None:
        return DEFAULT_CATEGORY, DEFAULT_LABEL
    return ERROR_CODES.get(int(code), (DEFAULT_CATEGORY, DEFAULT_LABEL))


def is_stop_category(category: str) -> bool:
    """True when relaunching cannot fix this category (needs the user)."""
    return category in _STOP_CATEGORIES


def summarize_dialog_text(blob: str, limit: int = 160) -> str:
    """Condense dialog text to the informative part: drop window titles and
    button labels, keep the message sentences."""
    pieces = [p.strip() for p in blob.split("|") if p.strip()]
    pieces = [p for p in pieces if len(p) > 3]
    if not pieces:
        return DEFAULT_LABEL
    msg = max(pieces, key=len)
    msg = " ".join(msg.split())
    if len(msg) > limit:
        msg = msg[: limit - 1] + "\u2026"
    return msg


def reason_from_dialog(blob: str, limit: int = 160) -> tuple[str, int | None]:
    """Turn raw dialog text into a human reason + the error code, if any.

    Known codes produce "<friendly label> (Error Code: N)"; kick dialogs
    keep their custom moderation message appended. Unknown codes keep the
    dialog's own message so nothing is lost.
    """
    m = _ERROR_CODE_RE.search(blob)
    code = int(m.group(1)) if m else None
    msg = summarize_dialog_text(blob, limit)
    if code is None:
        return msg, None
    category, label = classify(code)
    if category == CATEGORY_KICK:
        reason = f"{label} (Error Code: {code})"
        if msg and msg.lower() != label.lower():
            reason += f" \u2014 {msg}"
        return reason, code
    if int(code) in ERROR_CODES:
        return f"{label} (Error Code: {code})", code
    return f"{msg} (Error Code: {code})", code


def log_hint(text: str) -> str | None:
    """Extract the last error marker from a chunk of client log text."""
    last = None
    for m in _LOG_HINT_RE.finditer(text):
        last = m.group(0)
    return last.strip() if last else None
