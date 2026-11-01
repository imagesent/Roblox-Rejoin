"""Roblox Rejoiner — Windows process & window management, plus client screenshots.

Finding/killing Roblox processes, locating and focusing their windows, and
grabbing a PNG of a single client for the dashboard. Win32 work lives here.
"""

from __future__ import annotations

import base64
import ctypes
import subprocess
import time
from ctypes import wintypes

from rejoiner_constants import (CREATE_NO_WINDOW, ROBLOX_EXE, IS_WINDOWS,
                      user32, gdi32)

def roblox_pids() -> set[int]:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {ROBLOX_EXE}", "/FO", "CSV", "/NH"],
            creationflags=CREATE_NO_WINDOW, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="ignore")
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        if line.lower().startswith(f'"{ROBLOX_EXE.lower()}"'):
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(int(parts[1]))
    return pids


# Optional status-game data for recognizing known executor window titles.
# Matching is anchored to a process or window name to avoid unrelated matches.
EXECUTOR_RATINGS: dict[str, tuple[str, int]] = {
    "volt":         ("Volt", 10),
    "wave":         ("Wave", "RAT"),
    "seliware":     ("Seliware", "6"),
    "potassium":    ("Potassium", "Tuff"),
    "synapse":      ("Synapse", 7),
    "synapsez":     ("Synapse Z", 7),
    "scriptware":   ("Script-Ware", 7),
    "matcha":       ("Matcha", 3),
    "velocity":     ("Velocity", 6),
    "madium":       ("Madium", 7),
    "bytebreaker":  ("ByteBreaker", 5),
    "swift":        ("Swift", 5),
    "codex":        ("Codex", 5),
    "fluxus":       ("Fluxus", 4),
    "krnl":         ("Krnl", 4),
    "valex":        ("Valex", 3),
    "evon":         ("Evon", "SAKRAT"),
    "luna":         ("Luna", 3),
    "solara":       ("Solara", "ASS"),
    "xeno":         ("Xeno", "TRASH"),
    "nihon":        ("Nihon", 2),
    "comet":        ("Comet", 1),
    "jjsploit":     ("JJSploit", 1),
}


def _matches_executor(text: str) -> tuple[str, int] | None:
    """Return a configured executor label and rating for a matching name.

    Matching is anchored to the start of the cleaned text, where product names
    such as "Wave 4.2" and "Xeno v1.0.0" are expected to appear.
    """
    if not text:
        return None
    low = text.lower().strip()
    # Normalize common separators so equivalent product names match.
    norm = (low.replace("-", "").replace("_", "").replace("|", " ")
               .replace(":", " "))
    norm_nospace = norm.replace(" ", "")
    first_word = norm.split()[0] if norm.split() else ""
    # Check longer names first so a specific product is not mistaken for a prefix.
    for key in sorted(EXECUTOR_RATINGS, key=len, reverse=True):
        val = EXECUTOR_RATINGS[key]
        # Accept an exact name or a leading product name followed by a version.
        if (norm_nospace == key
                or first_word == key
                or norm_nospace.startswith(key) and (
                    len(norm_nospace) == len(key)
                    or not norm_nospace[len(key)].isalpha())):
            return val
    return None


def _enum_window_titles() -> list[str]:
    """Titles of every visible, titled top-level window on screen."""
    if user32 is None:
        return []
    titles: list[str] = []
    try:
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                          ctypes.c_int]
    except Exception:
        pass
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(h, _l):
        try:
            if user32.IsWindowVisible(h):
                n = user32.GetWindowTextLengthW(h)
                if n and n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(h, buf, n + 1)
                    t = buf.value.strip()
                    if t:
                        titles.append(t)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(proto(_cb), 0)
    except Exception:
        pass
    return titles


def detect_executor() -> tuple[str, int] | None:
    """Return the highest-rated configured executor found on Windows.

    Window titles are checked first, followed by process image names. Returns
    None on other platforms or when no configured name matches.
    """
    if not IS_WINDOWS:
        return None
    best: tuple[str, int] | None = None

    # Prefer branded window titles when available.
    for title in _enum_window_titles():
        hit = _matches_executor(title)
        if hit and (best is None or hit[1] > best[1]):
            best = hit

    # Fall back to process names for windows without a recognizable title.
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=CREATE_NO_WINDOW, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="ignore")
    except Exception:
        out = ""
    for line in out.splitlines():
        first = line.split('","', 1)[0].strip().strip('"')
        if first.lower().endswith(".exe"):
            hit = _matches_executor(first[:-4])
            if hit and (best is None or hit[1] > best[1]):
                best = hit
    return best


def kill_pid(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def kill_all_roblox() -> None:
    try:
        subprocess.run(["taskkill", "/F", "/IM", ROBLOX_EXE],
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def find_window_for_pid(pid: int):
    if user32 is None or not pid:
        return None
    matches = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(h, _l):
        if user32.IsWindowVisible(h):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid and user32.GetWindowTextLengthW(h) > 0:
                matches.append(h)
        return True

    user32.EnumWindows(proto(_cb), 0)
    return matches[0] if matches else None


def find_all_windows_for_pid(pid: int) -> list:
    """All visible, titled, top-level windows owned by a pid (for dup detection)."""
    if user32 is None or not pid:
        return []
    matches = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(h, _l):
        if user32.IsWindowVisible(h):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid and user32.GetWindowTextLengthW(h) > 0:
                # Ignore small utility windows; retain full-size client windows.
                r = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                if (r.right - r.left) > 200 and (r.bottom - r.top) > 200:
                    matches.append(h)
        return True

    user32.EnumWindows(proto(_cb), 0)
    return matches


def focus_window(hwnd) -> bool:
    if user32 is None or not hwnd:
        return False
    try:
        user32.keybd_event(0x12, 0, 0, 0)        # Alt down (unlock focus steal)
        user32.ShowWindow(hwnd, 9)               # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.keybd_event(0x12, 0, 0x0002, 0)   # Alt up
        return True
    except Exception:
        return False


# Structures required by the Windows GDI window-capture APIs.
class _BMIHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BMInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIHeader), ("bmiColors", wintypes.DWORD * 3)]


def _grab_hwnd_image(hwnd, foreground: bool):
    """Capture a window to a PIL image. PrintWindow by default (no focus steal);
    foreground=True focuses it and grabs the screen region (reliable for DX)."""
    from PIL import Image  # lazy — only needed for the bot's screenshots

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    if foreground:
        focus_window(hwnd)
        time.sleep(0.45)
        src_dc = user32.GetDC(None)          # whole-screen DC
        sx, sy = rect.left, rect.top
        release_target = None
    else:
        src_dc = user32.GetWindowDC(hwnd)
        sx, sy = 0, 0
        release_target = hwnd
    if not src_dc:
        return None

    mem_dc = gdi32.CreateCompatibleDC(src_dc)
    bmp = gdi32.CreateCompatibleBitmap(src_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        if foreground:
            gdi32.BitBlt(mem_dc, 0, 0, w, h, src_dc, sx, sy, 0x00CC0020)  # SRCCOPY
        else:
            # Request full DWM rendering, then fall back to the basic capture flag.
            if not user32.PrintWindow(hwnd, mem_dc, 0x00000002):
                user32.PrintWindow(hwnd, mem_dc, 0)

        bmi = _BMInfo()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BMIHeader)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # negative -> top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB
        buf = (ctypes.c_char * (w * h * 4))()
        got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        if not got:
            return None
        return Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(release_target, src_dc)


def _is_mostly_black(img) -> bool:
    try:
        small = img.convert("L").resize((16, 16))
        px = list(small.getdata())
        return (sum(px) / len(px)) < 8
    except Exception:
        return False


def capture_account_png(pid: int, out_path: str, foreground: bool):
    """Returns (ok, error). error == 'BLACK' signals a DirectX black frame."""
    if not IS_WINDOWS or gdi32 is None:
        return False, "capture unavailable on this platform."
    hwnd = find_window_for_pid(pid)
    if not hwnd:
        return False, "no Roblox window found for that account."
    try:
        img = _grab_hwnd_image(hwnd, foreground)
    except ImportError:
        return False, "Pillow not installed (pip install pillow)."
    except Exception as e:
        return False, f"capture error: {e}"
    if img is None:
        return False, "capture failed (empty frame)."
    if not foreground and _is_mostly_black(img):
        return False, "BLACK"
    try:
        img.save(out_path, "PNG")
    except Exception as e:
        return False, f"could not save image: {e}"
    return True, ""
