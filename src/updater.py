"""Self-update from GitHub releases for the standalone Roblox Rejoiner.

Flow: query the repo's latest release → compare against the running version
→ download the new exe next to the running one → swap it in → restart.

The swap uses the Windows rename trick: a RUNNING exe cannot be overwritten,
but it CAN be renamed — so the current app keeps running from the renamed
file while the new exe takes its place, and the update applies on restart.

Only active in the PyInstaller build (sys.frozen); when running from source
there is nothing to update, so every entry point is a no-op.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

from rejoiner_constants import (APP_VERSION, GITHUB_REPO, RELEASE_ASSET)

_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
_CHUNK = 262144


def exe_path() -> str | None:
    """Path of the running standalone exe, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return os.path.abspath(sys.executable)


def _ver_tuple(version: str) -> tuple:
    """'v1.2.3' → (1, 2, 3); stops at the first non-numeric part."""
    version = str(version or "").strip().lstrip("vV")
    parts = []
    for chunk in re.split(r"[.\-+_]", version):
        m = re.match(r"\d+", chunk)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts) if parts else (0,)


def is_newer(tag: str, current: str = APP_VERSION) -> bool:
    """True when `tag` is a strictly newer release than the running version."""
    try:
        return _ver_tuple(tag) > _ver_tuple(current)
    except Exception:
        return False


def _asset_name_ok(name: str) -> bool:
    """Match the app exe asset regardless of space/dot variants
    ("Roblox Rejoiner.exe" vs "Roblox.Rejoiner.exe")."""
    return re.sub(r"[ .]", "", str(name or "")).lower() == "robloxrejoinerexe"


def fetch_latest(repo: str = GITHUB_REPO, timeout: int = 15) -> dict | None:
    """Return {"tag", "url", "published"} for the latest published release
    that carries the app exe as an asset, or None on any problem."""
    req = urllib.request.Request(_API_URL.format(repo=repo), method="GET")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "Roblox-Rejoiner")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    asset_url = ""
    for asset in data.get("assets") or []:
        if _asset_name_ok(asset.get("name")):
            asset_url = str(asset.get("browser_download_url") or "")
            break
    if not (tag and asset_url):
        return None
    return {"tag": tag, "url": asset_url,
            "published": str(data.get("published_at") or "")}


def cleanup_stale(target: str) -> None:
    """Delete leftovers from earlier updates (.old backup, partial .update)."""
    if not target:
        return
    try:
        for name in os.listdir(os.path.dirname(target) or "."):
            full = os.path.join(os.path.dirname(target), name)
            if name.startswith(os.path.basename(target)) and (
                    ".old" in name or ".update" in name):
                try:
                    os.remove(full)
                except OSError:
                    pass   # still locked (previous instance exiting) — next time
    except OSError:
        pass


def download(url: str, dest: str, progress=None) -> bool:
    """Stream the asset to `dest`. `progress` gets 0-100 percentages.
    A partial download is deleted; returns True only for a full one."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Roblox-Rejoiner")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, \
                open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -5
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress and total:
                    pct = int(done * 100 / total)
                    if pct >= last_pct + 5:
                        last_pct = pct
                        try:
                            progress(pct)
                        except Exception:
                            pass
        return os.path.exists(dest) and os.path.getsize(dest) > 1_000_000
    except Exception:
        try:
            os.remove(dest)
        except OSError:
            pass
        return False


def apply_update(downloaded: str, target: str) -> bool:
    """Swap the new exe in, backing up the running one with a unique name
    (the running process keeps executing from the renamed file)."""
    old = f"{target}.old{os.getpid()}"
    try:
        os.replace(target, old)
        os.replace(downloaded, target)
        return True
    except OSError:
        try:
            if not os.path.exists(target) and os.path.exists(old):
                os.replace(old, target)     # roll back
        except OSError:
            pass
        return False


def launch_new(target: str) -> None:
    """Start the freshly swapped-in exe."""
    try:
        subprocess.Popen([target], cwd=os.path.dirname(target) or None,
                         close_fds=True)
    except Exception:
        pass
