"""Account configuration, encrypted cookies, and bot-bridge file storage.

On Windows, account cookies are encrypted with DPAPI before they are stored.
The bot bridge files contain account names, process IDs, and settings only.
This module also acquires the Roblox mutexes used for multi-client launching.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path

from rejoiner_constants import (CONFIG_PATH, SESSION_PATH, CMD_DIR, kernel32)

def _atomic_write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_remote_commands() -> list[dict]:
    """Pick up and delete pending command files dropped by the bot."""
    cmds = []
    try:
        files = sorted(CMD_DIR.glob("cmd_*.json"))
    except OSError:
        return cmds
    for f in files:
        try:
            cmds.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        try:
            f.unlink()
        except OSError:
            pass
    return cmds


def write_session_map(rt: dict) -> None:
    """Publish {username -> pid/place/state} so an external tool (the bot) can
    target a specific account's window. Never contains cookies or secrets."""
    data = []
    for st in rt.values():
        if not st.get("monitored", True):
            continue
        acc = st.get("acc", {})
        data.append({
            "username": acc.get("username"),
            "user_id": acc.get("user_id"),
            "pid": st.get("pid"),
            "place_id": acc.get("rplace"),
            "state": st.get("state"),
            "updated": time.time(),
        })
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


# Windows Data Protection API helpers for encrypting stored account cookies.
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_out_to_bytes(blob: "_DataBlob") -> bytes:
    size = int(blob.cbData)
    buf = ctypes.create_string_buffer(size)
    ctypes.memmove(buf, blob.pbData, size)
    kernel32.LocalFree(blob.pbData)
    return buf.raw


def dpapi_encrypt(text: str) -> str:
    raw = text.encode("utf-8")
    src = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DataBlob(len(raw), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return base64.b64encode(_blob_out_to_bytes(blob_out)).decode("ascii")


def dpapi_decrypt(b64: str) -> str:
    raw = base64.b64decode(b64)
    src = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DataBlob(len(raw), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out_to_bytes(blob_out).decode("utf-8")


# Hold Roblox's singleton mutexes while launching multiple client processes.


def acquire_multi_instance() -> list:
    if kernel32 is None:
        return []
    handles = []
    for name in ("ROBLOX_singletonEvent", "ROBLOX_singletonMutex"):
        try:
            h = kernel32.CreateMutexW(None, True, name)
            if h:
                handles.append(h)
        except Exception:
            pass
    return handles




def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    obsolete_keys = (
        "autokill_minutes", "autokill_on", "synckill_on", "tile_windows",
        "unlock_size", "schedule_on", "schedule_time", "schedule_daily",
        "log_clear_min", "cache_clear_min",
    )
    removed_obsolete_settings = False
    for key in obsolete_keys:
        if key in cfg:
            del cfg[key]
            removed_obsolete_settings = True
    accounts = cfg.get("accounts")
    if isinstance(accounts, list):
        for account in accounts:
            if isinstance(account, dict) and "killmin" in account:
                del account["killmin"]
                removed_obsolete_settings = True
    cfg.setdefault("accounts", [])
    cfg.setdefault("place", "")
    cfg.setdefault("delay", "60")
    cfg.setdefault("selected", "all")
    cfg.setdefault("discord", {})
    if removed_obsolete_settings:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass
