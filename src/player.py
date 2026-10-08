"""Video player -- drives an ISOLATED mpv instance.

HARD CONSTRAINT (user requirement): never disturb the user's mpv.net setup.
Therefore:
  * We run our own mpv copy at bin/mpv-iso/mpv.exe
  * We pass --config-dir=<our dir> and --no-config so mpv NEVER reads
    %APPDATA%\\mpv or %APPDATA%\\mpv.net
  * We manage the process ourselves; no global hotkeys, no user scripts

Architecture note (measured in this environment):
    mpv's built-in ytdl_hook fails here with "Subprocess failed: init" -- mpv
    cannot spawn yt-dlp as a child process. Python CAN spawn subprocesses
    normally (verified). So we do NOT rely on mpv's ytdl_hook at all:
    Python resolves the direct stream URL via yt-dlp, then hands mpv a plain
    URL. This is also more controllable and avoids mpv/yt-dlp version drift.

Default mode is silent (`mute=yes`): the video draws only the picture while the
user's own player (e.g. 汽水音乐) keeps producing the sound.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# External tools. Resolution order: MVM_MPV / MVM_YTDLP, then ./bin, then PATH.
# Hard-coding one machine's install path made the project unrunnable elsewhere.
from paths import find_mpv  # noqa: E402

MPV = find_mpv()
YTDLP = ROOT / "bin" / "yt-dlp.exe"
CONFIG_DIR = ROOT / "config" / "mpvdir"    # --config-dir target (kept minimal)
SCRIPTS_DIR = ROOT / "config" / "scripts"
CONTROL_LUA = SCRIPTS_DIR / "mvm_control.lua"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"

# Command/status files used to drive the long-lived mpv.
#
# We cannot use mpv's normal control channels here:
#   * --input-ipc-server is a named pipe, and opening named pipes is denied
#     (PermissionError errno 13).
#   * --input-terminal=yes does NOT read from a redirected stdin -- verified
#     from mpv's verbose log, where our written commands never appeared as
#     "Run command" entries.
# So config/scripts/mvm_control.lua polls CMD_FILE and publishes state to
# STATUS_FILE. Both are plain files, which need no pipes or sockets.
CMD_FILE = STATE_DIR / "_mvm_cmd.txt"
STATUS_FILE = STATE_DIR / "_mvm_status.txt"

# Manual-alignment sidecar (task-2 / session 8).
#
# The mpv hotkeys ([ ] { } 0 \) move the playhead by a relative amount and write
# the CUMULATIVE manual offset to MANUAL_FILE, atomically via MANUAL_TMP. The
# follower polls MANUAL_FILE and persists new values through align_calib, so a
# user's nudge survives the song and can be inherited by later songs.
#
# WHY A SIDECAR INSTEAD OF JUST STATUS_FILE LINE 5: the status file is rewritten
# every 0.5s and also carries time-pos; a reader cannot tell "the manual offset
# changed" from "the position advanced". The sidecar only ever changes when the
# USER acts, which makes it a reliable trigger. Both are exposed because the Lua
# side writes them together.
#
# NAME MATTERS (a real collision found during wiring): this must NOT be
# `_mvm_manual_offset.txt`, which is align_calib.PENDING_FILE -- a completely
# different channel. PENDING_FILE is a tab-separated REQUEST log written by the
# GUI ("please set the offset to X") and consumed by the follower, whereas this
# sidecar is a single float written by mpv's Lua ("the user has nudged by Y so
# far"). Pointing them at one file would make each side parse the other's format
# and silently lose every nudge. Distinct names keep the two channels honest.
#
# These paths are handed to mpv via MVM_MANUAL_FILE / MVM_MANUAL_TMP (see
# start()), which is how the Lua script learns them -- it cannot import Python.
MANUAL_FILE = STATE_DIR / "_mvm_hotkey_offset.txt"
MANUAL_TMP = STATE_DIR / "_mvm_hotkey_offset.tmp"

# How long to wait for a freshly spawned mpv to become usable.
#
# Measured 2026-10-08: a small window (~982x596) is ready in ~1.5s, but a large
# one (1942x1136) spends ~16s in GPU/libplacebo initialisation first. The old
# fixed 1.5s sleep therefore declared "mpv 启动失败" for launches that were
# merely slow.
#
# TWO budgets, because they answer different questions:
#   * START_READY_TIMEOUT_SEC -- how long we BLOCK inside start() waiting for
#     evidence that THIS process is up. start() holds `_lifecycle_lock`, which
#     the follower's poll loop needs for stop() during a song change, so this
#     must stay short: 6s covers the small-window case (~1.5s) with room, and a
#     slow large-window launch simply proceeds without us (the geometry guard
#     and the status file catch up on the next poll).
#   * START_TIMEOUT_SEC -- the absolute ceiling after which a launch is
#     considered failed by callers that want to wait longer.
START_READY_TIMEOUT_SEC = 6.0
START_TIMEOUT_SEC = 25.0

# ---------------- seek verification tuning (session 8) ----------------
#
# Context. mvm_control.lua rewrites STATUS_FILE's time-pos every 0.5s and polls
# CMD_FILE every 0.2s, so any readback we do carries up to ~0.7s of latency.
# These constants are sized so the verification catches GROSS failures (a clamped
# or dropped seek) without ever rejecting a normal, healthy landing.
#
# Baseline for scale (NOTES.md §1): the measured steady-state A/V offset on this
# machine is 0.2-0.6s, and >3s is treated as a real fault. The verification
# tolerance therefore sits at 1.0s late / 1.5s early -- comfortably wider than
# normal jitter (so it does not cry wolf) and comfortably narrower than the 3s
# fault threshold (so it does catch the failures worth catching).
SEEK_VERIFY_TOLERANCE_SEC = 1.0        # how far AHEAD of target we still accept
SEEK_VERIFY_LATE_TOLERANCE_SEC = 1.5   # how far BEHIND target we still accept
SEEK_VERIFY_SETTLE_SEC = 0.8           # wait before reading back (0.2s poll + 0.5s writer + mpv latency)
SEEK_VERIFY_ATTEMPTS = 2               # one retry: a single re-issue fixes a dropped command

# Optional cookie file. bilibili REQUIRES a logged-in cookie or it returns 412.
# NEVER commit a real cookies.txt: it holds your account session. Set MVM_COOKIES
# to reuse a file kept elsewhere rather than editing machine paths into the code.
COOKIE_CANDIDATES = [
    STATE_DIR / "cookies.txt",
    ROOT / "config" / "cookies.txt",
]

_extra_cookies = os.environ.get("MVM_COOKIES", "").strip().strip('"')
if _extra_cookies:
    COOKIE_CANDIDATES.append(Path(_extra_cookies))

# Scratch area holding throwaway cookie copies handed to yt-dlp (see
# find_cookies). Kept separate so it is obvious these are disposable.
_COOKIE_WORKDIR = STATE_DIR / ".cookie-jar"

# Browser-ish headers for bilibili's CDN.
#
# bilibili needs Referer AND a browser User-Agent TOGETHER -- with only one of
# them the CDN answers 403, and with neither, 403 as well (measured).
#
# CRITICAL: mpv's `http-header-fields` is a COMMA-SEPARATED LIST, so a value
# containing a comma SPLITS THE HEADER IN TWO. The natural browser UA
#     Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/... 
# contains "Windows NT 10.0, Win64" and was therefore torn apart at that comma,
# producing a malformed request. Measured effects:
#     --http-header-fields="Referer: ...,User-Agent: Mozilla/5.0 (Windows NT 10.0, Win64)..."
#         -> header split mid-UA -> HTTP 400 Bad Request -> video never loaded
#     (with UA placed first the fragments happened to re-form a valid sequence,
#      which is why it "sometimes worked" and made this look intermittent)
#
# The fix: NO COMMAS ANYWHERE in the header values. The UA below spells the
# platform comment without a comma; it is accepted by the CDN (verified: the
# stream loads and reports LOADED with the UA-only variant).
#
# Also note the separator is a literal comma (mpv's list syntax), NOT "\r\n":
# a \r\n-joined string is truncated to its first line by mpv's option parser,
# which silently dropped the User-Agent and caused 403s.
HTTP_HEADERS = (
    "Referer: https://www.bilibili.com/,"
    "Origin: https://www.bilibili.com,"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0 Win64) "
    "AppleWebKit/537.36 (KHTML like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ---------------- window geometry: units, persistence, defaults ------------
#
# WHY THIS EXISTS: the window position is remembered ACROSS PROCESS RESTARTS in
# state/window.json. Previously `_geometry` was a plain in-memory attribute, so
# every new follower started with None, passed NO --geometry to mpv, and mpv
# opened at its own default -- the CENTRE of the work area (measured 953,500 =
# the exact centre of the 2560x1440 virtualised work area). That is the
# user-reported bug "每次启动窗口都在主屏幕中间".
#
# UNITS: everything below is PHYSICAL pixels. mpv's --geometry and Win32
# GetWindowRect/SetWindowPos all speak physical pixels, and we pin each calling
# thread to PER_MONITOR_AWARE_V2 so the numbers agree.
#
# WHY NOT config/scripts/window.ps1 ANY MORE: it runs under DPI-UNAWARE
# PowerShell 5.1, which reports VIRTUALISED pixels -- on this machine the
# physical screen is 3840x2160 but PowerShell sees 2560x1440 (1.5x smaller).
# Those numbers are not interchangeable with --geometry, which is exactly the
# 1.5x mismatch traced in 体检报告/01 §5.3. window.ps1 also cost ~930ms per
# call, which stretched the geometry guard's real period to ~1.4s instead of
# 0.5s (01 §2.3). ctypes costs ~1.4us. The .ps1 file is left on disk (NOTES.md
# documents it for manual use) but player.py no longer shells out to it.
WINDOW_STATE_FILE = STATE_DIR / "window.json"
WINDOW_TITLE = "MVM-Video"
# Physical size used on the very first run. Matches what the window already
# measured (655x397 virtualised x 1.5 = 982x596 physical).
DEFAULT_WINDOW_W = 982
DEFAULT_WINDOW_H = 596
# Gap kept between the window and the work-area edge on first run.
WINDOW_MARGIN = 24
# How long after a start/loadfile the guard ENFORCES the remembered rectangle
# instead of accepting a moved one. mpv re-centres itself shortly after a video
# loads (measured at t~9s and t~20.8s in 01 §2.1), so that period must be
# covered; after it, a window that stays put is the user's doing.
GUARD_SETTLE_SECONDS = 12.0
# DWM frame offset (geometry - outer rect) used on the very FIRST run, before we
# have had a chance to measure it. Measured on this machine:
#     --geometry=800x500+300+200  ->  outer (291,200) 822x556
# i.e. (9, 0, -22, -56). Without it the first window would overhang the work
# area by the title bar and borders (observed 1868+652 = 2520 > 2488). It is
# only a starting estimate: the real value is measured from the live window and
# stored in window.json, so a different DWM theme self-corrects after one launch.
DEFAULT_FRAME = (9, 0, -22, -56)

_WIN = os.name == "nt"

if _WIN:                                    # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as _wt

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    # A PRIVATE WinDLL instance, deliberately not ctypes.windll.user32.
    # windll caches one shared function table per process, so setting .argtypes
    # on it mutates state every other module (and any test script) sees: doing
    # that from a probe script made GetWindowRect here raise
    # "expected LP_RECT instance instead of pointer to _RECT". WinDLL() returns
    # a fresh instance, so our prototypes stay ours.
    _user32 = ctypes.WinDLL("user32")
    _user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    _user32.SystemParametersInfoW.argtypes = [_wt.UINT, _wt.UINT,
                                              ctypes.c_void_p, _wt.UINT]
    _user32.GetWindowRect.argtypes = [_wt.HWND, ctypes.POINTER(_RECT)]
    _user32.GetWindowRect.restype = _wt.BOOL
    _user32.SetWindowPos.argtypes = [_wt.HWND, _wt.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     _wt.UINT]
    _user32.SetWindowPos.restype = _wt.BOOL
    _user32.IsWindowVisible.argtypes = [_wt.HWND]
    _user32.GetWindowTextW.argtypes = [_wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [_wt.HWND,
                                                 ctypes.POINTER(ctypes.c_ulong)]
    _ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)
    _user32.EnumWindows.argtypes = [_ENUM_PROC, _wt.LPARAM]

    _DPI_AWARE_V2 = -4          # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    _SPI_GETWORKAREA = 0x0030
    _SWP_NOZORDER = 0x0004
    _SWP_NOACTIVATE = 0x0010


def _dpi_aware() -> None:
    """Pin the CALLING THREAD to physical pixels (idempotent, ~1us).

    Python is DPI-unaware by default, so without this every Win32 call reports
    virtualised coordinates -- the exact mismatch this task had to remove.
    Measured: after the switch GetSystemMetrics goes 2560x1440 -> 3840x2160.
    """
    if not _WIN:
        return
    try:
        _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(_DPI_AWARE_V2))
    except (AttributeError, OSError):
        pass        # pre-1703 Windows: keep virtualised coordinates


def work_area() -> tuple[int, int, int, int]:
    """Primary monitor's work area (taskbar excluded) in PHYSICAL pixels."""
    _dpi_aware()
    if _WIN:
        rect = _RECT()
        try:
            if _user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0,
                                             ctypes.byref(rect), 0):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 0 and h > 0:
                    return (rect.left, rect.top, w, h)
        except OSError:
            pass
        return (0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))
    return (0, 0, 1920, 1080)


def default_window_rect() -> tuple[int, int, int, int]:
    """First-run position: BOTTOM-RIGHT of the work area, never the centre.

    mpv's built-in default is to centre the window, which is precisely what the
    user complained about. With nothing remembered yet we pick a spot that stays
    out of the way: bottom-right corner, small margin from the edges.

    The returned rect is the OUTER rect we want the window to end up at.
    __init__ pairs it with DEFAULT_FRAME so the --geometry numbers handed to mpv
    account for the DWM border; the visible window then lands exactly here.
    """
    wx, wy, ww, wh = work_area()
    w = min(DEFAULT_WINDOW_W, max(320, ww - 2 * WINDOW_MARGIN))
    h = min(DEFAULT_WINDOW_H, max(240, wh - 2 * WINDOW_MARGIN))
    return (wx + ww - w - WINDOW_MARGIN,
            wy + wh - h - WINDOW_MARGIN, w, h)


# Fraction of the work area above which an on-screen rectangle is treated as
# mpv self-enlarging rather than a user drag. Shared by
# `_is_plausible_user_resize` (which refuses to PERSIST such a rect) and
# `_looks_like_mpv_self_resize` (which decides whether to UNDO it) so the two
# can never drift apart (issue #2).
NEAR_FULLSCREEN_WORK_AREA_FRACTION = 0.70


def read_window_mode() -> tuple[bool, bool]:
    """mpv's own (fullscreen, maximized) state, from STATUS_FILE lines 6-7.

    WHY THIS EXISTS (issue #2, measured 2026-10-08)
        The geometry guard cannot distinguish "the user pressed F" from "mpv
        enlarged itself": both are one big rectangle. It therefore undid a
        deliberate fullscreen -- the user's "无法全屏" report. mpv knows the
        difference; `mvm_control.lua` now publishes it and this reads it.

    THE TRUNCATE-WRITE RACE, AND WHY THE LAST KNOWN VALUE IS CACHED ★
        Lua rewrites this file every 0.5s with a plain truncating write, so a
        reader can observe it EMPTY or PARTIAL (documented in NOTES §3.2 #2 for
        the same file). Measured 2026-10-08 while running the fullscreen probe
        three times: the third run's window was yanked out of fullscreen again,
        because on one tick this function saw a truncated file, reported "not
        fullscreen", and the guard's ENFORCE branch (still active for ~12s after
        a launch) snapped the window back.

        A truncation is momentary, so the most recent GOOD reading is the best
        estimate of the truth. Falling back to it makes the guard's fullscreen
        bypass robust; with no previous reading at all we return (False, False),
        which keeps the guard ACTIVE -- the conservative direction, since a
        missing flag must not silently disable window management.
    """
    global _last_window_mode
    try:
        lines = STATUS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return _last_window_mode or (False, False)

    # Lines 1-7 must all be present; a short file means we caught the writer
    # mid-truncate, not that the mode is false.
    if len(lines) < 7:
        return _last_window_mode or (False, False)

    mode = (lines[5].strip().lower() == "yes", lines[6].strip().lower() == "yes")
    _last_window_mode = mode
    return mode


# Most recent successfully parsed (fullscreen, maximized). See read_window_mode.
_last_window_mode: tuple[bool, bool] | None = None


def reset_window_mode_cache() -> None:
    """Forget the cached mode. Called when a new mpv is spawned.

    Without this, the first ticks of a NEW mpv (before it writes its own status
    file) would inherit the PREVIOUS process's mode -- e.g. a fresh window would
    be treated as fullscreen because the last one quit in fullscreen.
    """
    global _last_window_mode
    _last_window_mode = None


def _looks_like_mpv_self_resize(rect: tuple[int, int, int, int]) -> bool:
    """Whether an on-screen change carries mpv's self-enlargement signature.

    Used only AFTER mpv has told us it is neither fullscreen nor maximized (see
    `read_window_mode`): at that point a near-fullscreen rectangle is mpv having
    grown itself, which is worth undoing (the "窗口变得很大/挡住屏幕" bug).
    """
    _x, _y, w, h = rect
    wx, wy, ww, wh = work_area()
    if w <= 0 or h <= 0 or ww <= 0 or wh <= 0:
        return False
    return float(w * h) > NEAR_FULLSCREEN_WORK_AREA_FRACTION * float(ww * wh)


def _is_plausible_user_resize(pinned: tuple[int, int, int, int],
                              rect: tuple[int, int, int, int]) -> bool:
    """Whether a changed rectangle is plausibly the USER's doing.

    WHY this exists (measured 2026-10-07, user report "窗口变得很大/挡住屏幕"):
        The guard's rule was "any on-screen change must be the user moving the
        window, so adopt and persist it". But mpv also resizes the window by
        itself, and a self-resize that still fits on screen was therefore saved
        as if the user had asked for it. state/window.json ended up holding
        1942x1136 @949,480 -- and because it persists, EVERY later launch
        reopened that giant window, which is what the user saw as "no video"
        (it covered the screen / looked wrong) even though playback was fine.

    User drags are modest and preserve the window's AREA roughly; mpv's
    self-resizes jump to a much larger area (typically filling the screen).
    So we accept a change only if the area grows by less than 50% and the
    window still covers less than 70% of the work area.

    WHY A PER-CHANGE TEST IS STILL NOT ENOUGH (measured 2026-10-08):
        mpv's growth is INCREMENTAL. A window at 982x596 was observed being
        adopted at 1618x1136 and then at 1942x1136 -- each step within 1.5x of
        the previous value, so the per-change rule accepted every one of them
        and the "polluted geometry" the docstring above warns about came back
        through a series of individually-plausible steps. A user's drag is a
        one-off event, so the comparison must also be made against the window's
        ORIGINAL size for this launch, which `pinned` no longer represents once
        a step has been adopted.
    """
    px, py, pw, ph = pinned
    x, y, w, h = rect
    if pw <= 0 or ph <= 0 or w <= 0 or h <= 0:
        return False
    wx, wy, ww, wh = work_area()
    work_area_px = float(ww * wh) or 1.0
    area = float(w * h)
    if area > NEAR_FULLSCREEN_WORK_AREA_FRACTION * work_area_px:
        return False                      # near-fullscreen: mpv's own doing
    if area > 1.5 * float(pw * ph):
        return False                      # sudden big growth: not a drag
    return True


# Ceiling on how much the window may grow through a SEQUENCE of adopted
# changes, relative to the size it had when this mpv was launched. Chosen at
# 2.0x: a user who deliberately enlarges the window does it once (and that one
# step is judged by _is_plausible_user_resize above), whereas mpv's runaway
# growth is cumulative and crosses 2x within a couple of steps -- measured
# 982x596 -> 1618x1136 (3.1x area) -> 1942x1136 (3.8x area).
MAX_CUMULATIVE_GROWTH = 2.0

# Smallest window rectangle worth remembering (physical px). Below this the
# measurement is a transient artefact of a window that has not been laid out
# yet, not a size the user chose -- measured 202x100 while mpv was initialising.
# DEFAULT_WINDOW_W/H (982x596) is the intended first-run size, so this is a
# floor against nonsense rather than a constraint on legitimate resizing.
MIN_WINDOW_SIZE_PX = (320, 240)


def rect_on_screen(rect: tuple[int, int, int, int]) -> bool:
    """True when `rect` fits entirely inside the primary work area.

    Used to refuse remembering a runaway position: mpv has been measured
    jumping to 3181,1377 / 3285,1428, which hangs off this screen's edge.
    (Multi-monitor is out of scope -- only the primary work area is considered.)
    """
    x, y, w, h = rect
    wx, wy, ww, wh = work_area()
    return (w > 0 and h > 0 and x >= wx and y >= wy
            and x + w <= wx + ww and y + h <= wy + wh)


def load_window_state() -> tuple[tuple[int, int, int, int] | None,
                                 tuple[int, int, int, int] | None]:
    """Read state/window.json -> (outer rect, frame offsets).

    Returns (None, None) when the file is missing or unusable, so the caller
    falls back to default_window_rect(). Both values are PHYSICAL pixels;
    `frame` records how mpv's --geometry numbers map onto the resulting window
    rectangle (see geometry_arg). Never raises: a corrupt file must not stop
    playback.
    """
    try:
        raw = json.loads(WINDOW_STATE_FILE.read_text(encoding="utf-8"))
        rect = (int(raw["x"]), int(raw["y"]), int(raw["w"]), int(raw["h"]))
        if rect[2] <= 0 or rect[3] <= 0:
            return (None, None)
        # Reject an implausible SAVED size. A polluted file (a near-fullscreen
        # rectangle that a previous version recorded when mpv resized itself)
        # would otherwise be restored on every launch, which is how the user
        # ended up with a giant window that looked like "no video playing".
        wx, wy, ww, wh = work_area()
        if ww > 0 and wh > 0 and (rect[2] * rect[3]) > 0.70 * (ww * wh):
            return (None, None)
        frame = None
        stored = raw.get("frame")
        if isinstance(stored, dict):
            frame = (int(stored["dx"]), int(stored["dy"]),
                     int(stored["dw"]), int(stored["dh"]))
        return (rect, frame)
    except (OSError, ValueError, KeyError, TypeError):
        return (None, None)


def save_window_state(rect: tuple[int, int, int, int],
                      frame: tuple[int, int, int, int] | None) -> bool:
    """Persist the window rectangle (physical px) so a restart reuses it."""
    wx, wy, ww, wh = work_area()
    payload = {
        "version": 1,
        "units": "physical_px",
        "x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3],
        "screen": {"w": ww, "h": wh},
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if frame:
        payload["frame"] = {"dx": frame[0], "dy": frame[1],
                            "dw": frame[2], "dh": frame[3]}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: the guard thread rewrites this while the main
        # thread may read it, and a torn file would silently lose the position.
        tmp = WINDOW_STATE_FILE.parent / (WINDOW_STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, WINDOW_STATE_FILE)
        return True
    except OSError:
        return False


def geometry_numbers(rect: tuple[int, int, int, int],
                     frame: tuple[int, int, int, int] | None,
                     ) -> tuple[int, int, int, int]:
    """The (x, y, w, h) to hand mpv so that `rect` becomes the OUTER rect.

    `frame` is always stored as (requested -- outer), so reproducing a given
    outer rectangle is simply outer + frame. Sign convention matters: an earlier
    draft mixed `G-A` for position with `A-G` for size, which made the window
    GROW by the border width on every relaunch.
    """
    x, y, w, h = rect
    if frame:
        dx, dy, dw, dh = frame
        # Negative results would flip mpv's parser into the "distance from the
        # right/bottom edge" form (`-X-Y`); the guard fixes any residual.
        return (max(0, x + dx), max(0, y + dy),
                max(320, w + dw), max(240, h + dh))
    return (x, y, w, h)


def geometry_arg(rect: tuple[int, int, int, int],
                 frame: tuple[int, int, int, int] | None) -> str:
    """Build the mpv --geometry value that reproduces `rect` as the OUTER rect.

    mpv's --geometry is in PHYSICAL pixels but is NOT the window rectangle:
    measured, `--geometry=800x500+300+200` produced an outer rect of 822x556 at
    (291,200). The 9/22/56 px differences are the DWM resize borders and title
    bar (stable for a given window style), not a unit conversion -- so we LEARN
    them from the live window and invert them here, which makes a relaunch land
    pixel-identically instead of 9px left and 22x56px too large.

    (01 §5.3 read those same deltas as a 1.5x unit mismatch. The 1.5x error
    actually came from window.ps1 reporting virtualised pixels -- see the module
    comment above. --geometry itself was always physical.)
    """
    x, y, w, h = geometry_numbers(rect, frame)
    return f"{w}x{h}+{x}+{y}"


def find_cookies(for_ytdlp: bool = False) -> Path | None:
    """Return the first usable cookies.txt (must be non-empty).

    `for_ytdlp=True` returns a throwaway copy instead of the real file.

    WHY: yt-dlp REWRITES the file passed to --cookies when the session ends (it
    saves its cookie jar back). Measured: that wiped our 23 captured cookies
    down to 2 anonymous ones (b_nut + buvid3), destroying SESSDATA and breaking
    every bilibili request with HTTP 412. yt-dlp may therefore never be handed
    the real credential file.
    """
    for path in COOKIE_CANDIDATES:
        try:
            if path.exists() and path.stat().st_size > 0:
                return _cookie_work_copy(path) if for_ytdlp else path
        except OSError:
            continue
    return None


def _cookie_work_copy(source: Path) -> Path:
    """Copy the cookie file to a scratch path for yt-dlp to clobber freely."""
    try:
        _COOKIE_WORKDIR.mkdir(parents=True, exist_ok=True)
        dest = _COOKIE_WORKDIR / source.name
        data = source.read_bytes()
        if not dest.exists() or dest.read_bytes() != data:
            dest.write_bytes(data)
        return dest
    except OSError:
        # If copying fails we must NOT fall back to the real file.
        return source


class ResolveError(RuntimeError):
    """Raised when yt-dlp cannot produce a playable URL."""


def _ytdlp_backend() -> tuple[list[str], str]:
    """Pick how to invoke yt-dlp.

    Preference: the vendored Python module (vendor/ytdlp), because the bundled
    .exe is pinned at 2026.03.17 while upstream is 2026.08.19 -- five months of
    extractor fixes matter a lot for bilibili, which changes often.

    Why not just replace the .exe: the newer release is a PyInstaller *onefile*
    build that must unpack itself into a temp directory at startup, and this
    sandbox denies it that ("Failed to create parent directory structure").
    Running it as a Python module avoids the unpack step entirely.

    Falls back to the .exe when the module is absent.
    """
    vendored = ROOT / "vendor" / "ytdlp"
    if (vendored / "yt_dlp").is_dir():
        return (
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, r'{vendored}'); "
             "from yt_dlp import main; sys.exit(main())"],
            "module",
        )
    return [str(YTDLP)], "exe"


def resolve_stream_url(url: str, want: str = "video", timeout: int = 90) -> str:
    """Resolve a page URL to a direct media stream URL via yt-dlp.

    `want`: "video" -> video-only track (for pairing with external audio),
            "audio" -> audio-only track,
            "both"  -> a single muxed stream (simplest, always playable).

    Retries transient failures. Measured (2026-10-07): a bilibili page that
    returned "HTTP Error 502: Bad Gateway" during a follow run resolved fine
    seconds later -- B站 occasionally 502s and then works on retry. Without a
    retry the whole song was abandoned ("5 个候选均取流失败") and the user saw
    "no video, no alignment". We only retry when the error looks transient
    (HTTP 5xx / timeout / connection reset), never on real extractor errors.
    """
    backend, kind = _ytdlp_backend()
    if kind == "exe" and not YTDLP.exists():
        raise ResolveError(f"yt-dlp not found: {YTDLP}")

    fmt = {
        # "both" prefers a muxed stream, but many sources (bilibili DASH,
        # niconico) ONLY provide separate audio/video tracks. The trailing
        # fallbacks let yt-dlp pick a video track then merge, so playback still
        # works instead of failing with "Requested format is not available".
        "video": "bestvideo[height<=1080]/bestvideo/best",
        "audio": "bestaudio/best",
        "both": (
            "best[height<=1080]/best/"
            "bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio"
        ),
    }.get(want, "best")

    cmd = backend + [
        "--no-warnings",
        "--no-playlist",
        "-f", fmt,
        "--get-url",
    ]
    cookies = find_cookies(for_ytdlp=True)
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)

    import re as _re
    transient = _re.compile(
        r"HTTP Error 5\d\d|timed out|timeout|connection (?:reset|aborted)|"
        r"Remote end closed connection|502|503|504", _re.IGNORECASE
    )

    last_err: str | None = None
    for attempt in range(3):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            last_err = f"yt-dlp timeout after {timeout}s"
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise ResolveError(last_err) from exc
        except OSError as exc:
            raise ResolveError(f"cannot run yt-dlp: {exc}") from exc

        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            msg = err[-1] if err else f"exit {proc.returncode}"
            if transient.search(msg) and attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise ResolveError(msg)

        urls = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip().startswith("http")]
        if not urls:
            last_err = "yt-dlp returned no stream URL"
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise ResolveError(last_err)
        return urls[0]

    raise ResolveError(last_err or "resolve failed")


class MpvController:
    """Controls a dedicated, isolated mpv process.

    Keeps ONE long-lived mpv alive in --idle mode and drives it through a
    command file watched by config/scripts/mvm_control.lua, so switching songs
    does not tear down and recreate the window.

    Why a command file (and not the obvious options):
        * `--input-ipc-server` uses a Windows named pipe, and opening named
          pipes is denied in this environment (PermissionError errno 13).
        * `--input-terminal=yes` does NOT read commands from a redirected
          stdin. Verified against mpv's own verbose log: written commands never
          appeared as "Run command" entries, i.e. they were silently dropped.
          An earlier version of this file claimed stdin worked because the PID
          stayed constant across switches -- a false positive, since nothing
          was being executed at all.
        * A plain file needs no pipes and no sockets, and is confirmed working:
          mpv logs "mvm: ran loadfile ..." / "mvm: ran seek ...".
    """

    def __init__(self, mute: bool = True, log: bool = True,
                 ontop: bool = False) -> None:
        self.mute = mute
        self.log = log
        # Window stacking. DEFAULT: NOT on top (user decision D3).
        #
        # WHY: _base_args() used to hard-code `--ontop`, which sets
        # WS_EX_TOPMOST on our mpv window and kept the video above EVERY other
        # application -- reported as "显示在最前面，很影响我其他操作". mpv's own
        # default is already not-on-top, so passing nothing is the correct
        # behaviour; --ontop is now opt-in only (ontop=True), never the default.
        self.ontop = ontop
        self.proc: subprocess.Popen | None = None
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if log:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Geometry carried across songs so the window never jumps around.
        # A (x, y, w, h) tuple in PHYSICAL pixels, or None.
        #
        # Unlike before, this is loaded from state/window.json at construction,
        # so a brand-new process already knows where the window belongs and can
        # pass --geometry to mpv. Without that, mpv opens centred on the primary
        # work area -- the user's "每次启动都在主屏幕中间" bug.
        self._geometry, self._frame = load_window_state()
        if self._geometry is not None and not rect_on_screen(self._geometry):
            # Saved while a second monitor / different resolution was attached,
            # or a runaway position got persisted. Reusing it would put the
            # window off-screen where the user cannot reach it.
            self._geometry, self._frame = None, None
        if self._geometry is None:
            # First run (or unusable state): pick bottom-right instead of
            # letting mpv centre the window. Pair it with the measured frame
            # estimate so the window does not overhang the screen on the very
            # first launch; once the real frame is measured it replaces this.
            self._geometry = default_window_rect()
            self._frame = DEFAULT_FRAME
        self._geometry_lock = threading.Lock()
        # The size the window had when this mpv was launched. Used to reject
        # CUMULATIVE growth: mpv's self-resizes arrive as a series of steps that
        # each look plausible against the previous value (see
        # _is_plausible_user_resize), so the running value cannot be the only
        # baseline. Set in start(); None until then.
        self._launch_area: float | None = None
        self._geometry_thread: threading.Thread | None = None
        # When the guard must ENFORCE the rectangle rather than adopt a new one.
        self._enforce_until = 0.0
        # Serialises process lifecycle. The follower calls stop() from its poll
        # loop while a worker thread is inside play_url(); without this lock the
        # poll loop could kill the mpv the worker had just launched, which
        # surfaced as a spurious "mpv 启动失败".
        self._lifecycle_lock = threading.RLock()

    # ---------------- window geometry ----------------

    def _find_window(self) -> int | None:
        """HWND of OUR mpv window, or None.

        Matched on the process we spawned (`self.pid`) rather than on a window
        title alone -- 铁律 16: a title is not a reliable identity, and several
        stale MVM-Video windows can coexist. Falls back to the title match only
        when we have no live pid (e.g. a probe of an externally started mpv).
        """
        if not _WIN:
            return None
        _dpi_aware()
        mine = self.pid
        found: list[int] = []

        def cb(hwnd, _lparam):
            pid = ctypes.c_ulong()
            _user32.GetWindowThreadProcessId(_wt.HWND(hwnd), ctypes.byref(pid))
            if mine is not None and pid.value != mine:
                return True
            buf = ctypes.create_unicode_buffer(256)
            _user32.GetWindowTextW(_wt.HWND(hwnd), buf, 256)
            if buf.value == WINDOW_TITLE or (mine is not None and buf.value):
                found.append(hwnd)
                return False        # first match wins
            return True

        try:
            _user32.EnumWindows(_ENUM_PROC(cb), 0)
        except OSError:
            return None
        if found:
            return found[0]
        if mine is None:
            return None
        # We own a process but no titled window is visible yet.
        return None

    def get_window_rect(self) -> tuple[int, int, int, int] | None:
        """Return the MVM window's (x, y, w, h) in PHYSICAL pixels, or None.

        Measured from Win32 GetWindowRect, not from mpv: mpv's `width`/`height`
        are the VIDEO's dimensions (1920x1078), which is why feeding them back
        as --geometry produced a window twice the intended size.
        """
        hwnd = self._find_window()
        if not hwnd:
            return None
        rect = _RECT()
        try:
            if not _user32.GetWindowRect(_wt.HWND(hwnd), ctypes.byref(rect)):
                return None
        except OSError:
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        return (rect.left, rect.top, w, h)

    def set_window_rect(self, x: int, y: int, w: int, h: int) -> bool:
        """Move/resize the MVM window authoritatively via SetWindowPos.

        Needed because mpv re-centres its window a few seconds after a video
        loads (measured 953,500 -> 1191,439), so mpv's own --geometry does not
        always keep it in place.

        All values are PHYSICAL pixels; the calling thread is pinned DPI-aware
        first so they are not silently scaled by 1.5.
        """
        if not _WIN:
            return False
        hwnd = self._find_window()
        if not hwnd:
            return False
        _dpi_aware()
        try:
            return bool(_user32.SetWindowPos(
                _wt.HWND(hwnd), None, int(x), int(y), int(w), int(h),
                _SWP_NOZORDER | _SWP_NOACTIVATE,
            ))
        except OSError:
            return False

    def _measure_frame(self) -> tuple[int, int, int, int] | None:
        """Measure the REAL (requested -- outer) offsets of the live window.

        `--geometry=WxH+X+Y` does NOT produce a WxH window at (X,Y): the DWM
        frame adds borders and a title bar. Measured here:
            800x500+300+200 -> 822x556 @ (291,200)   => frame (9, 0, -22, -56)
        Recording it lets the next launch reproduce the SAME rectangle.

        The "requested" numbers are geometry_numbers(self._geometry,
        self._frame), NOT self._geometry itself -- that field holds the OUTER
        target we want the window to land on. MUST be called while self._geometry
        still describes the current launch.
        """
        rect = self.get_window_rect()
        if not rect or not self._geometry:
            return self._frame
        req = geometry_numbers(self._geometry, self._frame)
        return (req[0] - rect[0], req[1] - rect[1],
                req[2] - rect[2], req[3] - rect[3])

    def _remember_geometry(self, rect: tuple[int, int, int, int],
                           measurable: bool = True) -> None:
        """Adopt `rect` as the pinned geometry and persist it to disk.

        `measurable=False` when `rect` was NOT requested through --geometry
        (a window the user dragged): the frame offsets cannot be re-derived from
        it, so the previously learned ones are carried over unchanged.

        REFUSES to persist an implausible rectangle. WHY (measured 2026-10-08):
        while mpv was still initialising, GetWindowRect returned a degenerate
        202x100 at (38,38) with a 1718x980 "frame" -- and because this value is
        PERSISTED, the next launch would have opened a pin-hole window and then
        reproduced that nonsense border offset. A rectangle below
        MIN_WINDOW_SIZE_PX is a transient artefact of the window not being laid
        out yet, not something the user asked for, so it is dropped rather than
        remembered. The previously good value stays in place.
        """
        if rect[2] < MIN_WINDOW_SIZE_PX[0] or rect[3] < MIN_WINDOW_SIZE_PX[1]:
            return
        frame = self._measure_frame() if measurable else self._frame
        self._geometry = rect
        save_window_state(rect, frame)

    def remember_geometry(self) -> tuple[int, int, int, int] | None:
        """Cache the window rectangle so later songs reuse it (and persist it).

        Refuses to record anything while mpv is fullscreen or maximized
        (issue #2): that rectangle is the SCREEN, not a size the user chose for
        the window, and persisting it would make every subsequent launch open
        fullscreen with no way for the guard to tell "user wants this" from
        "we saved it by accident". The previously pinned rect is kept instead,
        which is also exactly what mpv restores on leaving fullscreen.
        """
        fullscreen, maximized = read_window_mode()
        if fullscreen or maximized:
            return self._geometry
        rect = self.get_window_rect()
        if rect:
            self._remember_geometry(rect)
        return self._geometry

    def restore_geometry(self) -> bool:
        """Re-apply the cached rectangle (used after mpv re-centres)."""
        if not self._geometry:
            return False
        x, y, w, h = self._geometry
        return self.set_window_rect(x, y, w, h)

    def kill_stray_windows(self, keep_pid: int | None = None,
                           allow_when_unknown: bool = False) -> int:
        """Terminate MVM video windows that are not the one we own.

        Measured problem: the user saw several video windows at once, and stale
        ones never closed. Rather than depend on pinpointing every path that can
        spawn an extra mpv, this enforces the invariant: at most ONE MVM window
        may exist.

        DANGER -- why `keep_pid` must be honoured exactly:
            An earlier version passed keep_pid=self.player.pid, but `pid` is
            None while a process is still starting. The cleanup then killed the
            mpv that was mid-launch, so a second one was spawned and the user saw
            "a big window appears, closes after ~10s, then a small window
            appears". Verified from a geometry trace:
                23684|735x757 -> (none) -> 24652|655x397

        `allow_when_unknown` exists for the one safe case: process startup,
        where we own no mpv yet, so anything present is genuinely left over from
        a previous run. Mid-run (during a song change) a launch may be in
        progress, and we refuse rather than risk killing it.

        Implementation notes (all verified on this machine):
          * `taskkill /FI WINDOWTITLE ...` -> "Access denied".
          * `Get-CimInstance`/WMI -> returns nothing in this sandbox.
          * Python's `os.kill(pid, 9)` -> WinError 5 (Access denied).
          * `Get-Process` + PowerShell `Stop-Process` -> works.
        The path must also be read from an env var: interpolating it into the
        command mangles backslashes, and passing it as a trailing argument makes
        PowerShell bind it to the last pipeline block and error out.
        """
        if os.name != "nt":
            return 0
        if not keep_pid and not allow_when_unknown:
            # A launch may be in progress; killing now could destroy it.
            return 0

        env = dict(os.environ)
        env["MVM_KILL_TARGET"] = str(MPV).lower()
        # Must be a numeric string: str(None) is "None", and PowerShell's
        # [int]"None" throws, which silently turned the whole cleanup into a
        # no-op. 0 never matches a real PID, so nothing is protected.
        env["MVM_KEEP_PID"] = str(keep_pid) if keep_pid else "0"
        script = (
            "$killed = 0; "
            "Get-Process mpv -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Path -and $_.Path.ToLower() -eq $env:MVM_KILL_TARGET } | "
            "Where-Object { $_.Id -ne [int]$env:MVM_KEEP_PID } | "
            "ForEach-Object { "
            "  try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $killed++ } catch {} "
            "}; "
            "Write-Output $killed"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=30, env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0
        try:
            return int((res.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    def _base_args(self) -> list[str]:
        args = [
            str(MPV),
            "--no-config",
            f"--config-dir={CONFIG_DIR}",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=no",
            "--title=MVM-Video",
            "--no-terminal",
            "--really-quiet",
            # Topmost is OPT-IN (ontop=True), never the default: mpv's own
            # default leaves the window in the normal Z-order, and the old
            # hard-coded `--ontop` kept it above every other application
            # (WS_EX_TOPMOST=True -- the "显示在最前面" bug, user decision D3).
            # Omitting the flag entirely == --no-ontop, which is what we want.
            *(["--ontop"] if self.ontop else []),
            # ALWAYS start at the remembered rectangle (state/window.json, or
            # bottom-right on the very first run). This is what stops mpv from
            # opening in the centre of the work area. `frame` inverts the DWM
            # border/title-bar offsets so the OUTER rect matches what was saved.
            f"--geometry={geometry_arg(self._geometry, self._frame)}",
            # Do NOT resize the window when a new video with a different
            # resolution loads. Without this, every song change snapped the
            # window to the new video's aspect ratio, which the user sees as
            # "the window moves every time".
            "--auto-window-resize=no",
            # Control channel: a Lua timer polls CMD_FILE (see class docstring).
            f"--script={CONTROL_LUA}",
            # Keep our own video window OUT of the SMTC session list. Without
            # this, mpv publishes its own "now playing" entry and the follower
            # would detect its own video as a song and start chasing itself.
            # NOTE: the option is `--media-controls` (plural). Writing
            # `--no-media-control` is NOT silently ignored -- mpv refuses to
            # start at all, so the typo showed up as "playback failed".
            "--no-media-controls",
            f"--http-header-fields={HTTP_HEADERS}",
        ]
        if self.mute:
            args += ["--mute=yes", "--volume=0"]
        if self.log:
            args.append(f"--log-file={LOG_DIR / 'mpv-iso.log'}")
        return args

    # ---------------- lifecycle ----------------

    def start(self) -> bool:
        """Launch the long-lived idle mpv (safe to call repeatedly)."""
        with self._lifecycle_lock:
            if self.running:
                return True
            # A new process must not inherit the previous one's window mode from
            # the cache (e.g. a window opening as "fullscreen" because the last
            # mpv quit while fullscreen).
            reset_window_mode_cache()

            # Make sure the PREVIOUS process is really gone before spawning a
            # new one. WHY (measured 2026-10-08: the user saw TWO windows while
            # one daemon ran):
            #
            #   * `stop()` sets `self.proc = None` UNCONDITIONALLY, even when
            #     terminate() did not actually kill the process. A surviving mpv
            #     therefore becomes an ORPHAN: nobody references it, and the
            #     follower's one-window cleanup only runs on song changes with a
            #     live pid to protect -- so the next start() spawned a SECOND
            #     window next to it.
            #   * `self.proc` can also still point at a live child (a start()
            #     that never got reaped), so that one is terminated too.
            #
            # Both cases are handled here, at the single place where a new mpv
            # comes into existence, because that is the only point where the
            # invariant "at most one MVM window" can be enforced without
            # guessing which pid to protect.
            self._terminate_proc()
            if self.pid is None:
                # We own nothing right now: anything with our binary path is a
                # leftover, so it is safe to clear (allow_when_unknown=True).
                strays = self.kill_stray_windows(allow_when_unknown=True)
                if strays:
                    self._log_line(f"  · 启动前清理了 {strays} 个残留视频窗口")

            # Fresh command/status files so a stale command is not replayed.
            try:
                CMD_FILE.write_text("", encoding="utf-8")
                if STATUS_FILE.exists():
                    STATUS_FILE.unlink()
                # The manual-offset sidecar belongs to a previous mpv run: a new
                # run starts with no manual offset, so leaving the old value
                # would make the follower record a nudge the user never made in
                # THIS session.
                if MANUAL_FILE.exists():
                    MANUAL_FILE.unlink()
            except OSError:
                pass

            env = dict(os.environ)
            env["MVM_CMD_FILE"] = str(CMD_FILE)
            env["MVM_STATUS_FILE"] = str(STATUS_FILE)
            # Hand the manual-offset sidecar paths to the Lua hotkeys (task-2).
            env["MVM_MANUAL_FILE"] = str(MANUAL_FILE)
            env["MVM_MANUAL_TMP"] = str(MANUAL_TMP)
            # Tell mpv which Python process owns it, so the Lua side can quit
            # when that process dies (issue #6). Compares launcher PIDs rather
            # than recording the child's parent pid: the launcher may itself be
            # wrapped (tools/run_follow_utf8.py), and the pid we embed here is
            # the one this very process controls.
            env["MVM_PARENT_PID"] = str(os.getpid())

            try:
                self.proc = subprocess.Popen(
                    self._base_args(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
            except OSError:
                return False

            # Wait for the window and the Lua timer to come up.
            #
            # WAS a fixed `time.sleep(1.5)` and that is not enough in general:
            # measured 2026-10-08, when state/window.json holds a large
            # rectangle mpv spends ~16s in GPU/libplacebo initialisation before
            # it is usable, so the 1.5s check saw `running == False` and the
            # caller reported "mpv 启动失败" for a process that was in fact
            # starting normally. The geometry can legitimately be large (the
            # user may drag the window big), so the wait must follow the
            # process rather than assume a fixed cost.
            #
            # THE WAIT IS BOUNDED AND PROCESS-BOUND. An earlier version of this
            # loop broke as soon as STATUS_FILE existed -- but that file is
            # written by whichever mpv is running, so a leftover file (or a
            # dying previous instance) satisfied it immediately and `start()`
            # returned while the NEW mpv was still initialising. The loop now
            # waits for evidence that THIS process is up: either its window
            # exists, or the status file has been rewritten after our spawn.
            # START_READY_TIMEOUT_SEC is deliberately much shorter than the
            # absolute START_TIMEOUT_SEC, because this method holds
            # `_lifecycle_lock` and the follower's poll loop needs `stop()` to
            # stay responsive during a song change.
            spawn_time = time.time()
            deadline = time.monotonic() + START_READY_TIMEOUT_SEC
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    # mpv exited on its own: a real failure (bad option, missing
                    # binary), not slowness. Report it now instead of waiting.
                    return False
                if self._find_window():
                    break
                try:
                    if STATUS_FILE.stat().st_mtime >= spawn_time:
                        break
                except OSError:
                    pass
                time.sleep(0.1)

            if self.running:
                # Remember the launch size, so the guard can reject cumulative
                # growth rather than judging each step in isolation.
                if self._geometry:
                    self._launch_area = float(self._geometry[2]
                                              * self._geometry[3])
                # Enforce the remembered rectangle for a while after launch:
                # mpv re-centres itself a few seconds after a video loads
                # (measured t~9s and t~20.8s), so a moved window in this period
                # is mpv's doing, not the user's.
                self._enforce_until = time.time() + GUARD_SETTLE_SECONDS
                if self._frame == DEFAULT_FRAME:
                    # First run: we only guessed bottom-right and the DWM frame
                    # is an estimate. Measure what mpv actually produced (and
                    # the real frame offsets) so a second launch is exact --
                    # and so the position is persisted at all.
                    self.remember_geometry()
                # Start pinning immediately so the window cannot drift even
                # before the first video loads.
                self._start_geometry_guard()
            return self.running

    def stop(self) -> None:
        """Tear the process down (used on shutdown, not between songs)."""
        with self._lifecycle_lock:
            # Last chance to save where the user left the window: the guard
            # samples every 0.5s, so a move followed immediately by a shutdown
            # would otherwise be lost. Skipped while fullscreen/maximized
            # (issue #2) -- quitting in fullscreen must not persist the screen
            # rectangle as the window's remembered size.
            try:
                fullscreen, maximized = read_window_mode()
                if not (fullscreen or maximized):
                    rect = self.get_window_rect()
                    if rect and rect != self._geometry and rect_on_screen(rect):
                        self._remember_geometry(rect, measurable=False)
            except OSError:
                pass
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
            self.proc = None

    def _terminate_proc(self) -> None:
        """Terminate `self.proc` if it is still alive, and forget it.

        Used by start() before spawning a replacement. WHY it must be explicit
        (measured 2026-10-08, user saw TWO windows from ONE daemon): the
        previous process can still be running while `self.running` reports False
        (it exited just far enough for poll() to return, or was never reaped),
        and spawning a new mpv then leaves both windows on screen. `stop()`
        cannot be reused here because it also persists window geometry and
        starts a guard -- this is purely "make sure the old child is dead".

        Never raises: a failure to kill is reported by `running` being False
        anyway, and the next cleanup pass will retry.
        """
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
        except OSError:
            pass
        self.proc = None

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def pid(self) -> int | None:
        """PID of the mpv we own, or None."""
        return self.proc.pid if (self.proc and self.proc.poll() is None) else None

    # ---------------- commands ----------------

    def command(self, line: str) -> bool:
        """Send one mpv command line through the watched command file.

        MUST append, never overwrite. The Lua side polls CMD_FILE every 0.2s
        (mvm_control.lua), so with `write_text` (truncate mode) a second command
        written in the same poll window replaced the first one before it was
        ever read: measured 5 back-to-back commands -> only 1 executed. Append
        mode keeps every line; the Lua timer reads the whole blob and runs each
        line in turn (mvm_control.lua: `for single in blob:gmatch(...)`).

        The truncate happens on the Lua side (it clears the file right after
        reading) and in start(), which empties the file so a stale command from
        a previous run is not replayed.
        """
        if not self.running:
            return False
        try:
            with open(CMD_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return True
        except OSError:
            return False

    def _seek_after_load(self, target_sec: float,
                         load_timeout: float = 25.0,
                         seek_timeout: float = 8.0) -> bool:
        """Seek to `target_sec` once the file has really loaded, then verify.

        Two races are handled here, both measured on this machine:

        1. mpv needs time to OPEN the media (a bilibili DASH stream over the
           network took 2-5s). A `seek` sent before that is silently dropped,
           and the video then plays from 0:00 -- the reason coarse alignment
           was never observed to work.
        2. Even after loading, the seek has to travel through the 0.2s Lua
           command-file poll before mpv acts on it.

        So we wait for a readable position (proof the file is loaded), issue the
        seek, then CONFIRM the position actually moved. Returns True only when
        the seek verifiably took effect -- callers can log that honestly instead
        of assuming success.
        """
        deadline = time.monotonic() + load_timeout
        loaded = False
        while time.monotonic() < deadline:
            pos = self.get_position()
            if pos is not None:
                loaded = True
                break
            time.sleep(0.3)
        if not loaded:
            # No position yet: still try the seek (some sources report position
            # late) but report failure honestly.
            self.command(f"seek {target_sec:.2f} absolute+exact")
            return False

        self.command(f"seek {target_sec:.2f} absolute+exact")
        # Confirm: the 0.2s poll plus mpv's own seek latency.
        seek_deadline = time.monotonic() + seek_timeout
        while time.monotonic() < seek_deadline:
            time.sleep(0.25)
            pos = self.get_position()
            if pos is None:
                continue
            # Accept anything within a few seconds of the target: the video
            # keeps playing while we poll, so pos is target + small delta.
            if pos >= target_sec - 2.0:
                return True
        return False

    def set_property(self, name: str, value) -> bool:
        if isinstance(value, bool):
            value = "yes" if value else "no"
        # NOTE: the mpv command is `set`, not `set_property`. Measured against
        # mpv's own log: `set_property mute yes` produced
        #     [e][input] Command 'set_property' not found.
        # so muting/pausing through this helper silently did nothing.
        return self.command(f"set {name} {value}")

    def get_position(self) -> float | None:
        """Read the video's current position from the status file.

        There is no request/response channel (no IPC), so the Lua script pushes
        state to a file and we read it back. Returns None when unavailable.

        NOTE: this value describes the playhead as of the LAST REWRITE, not as
        of this read -- see `status_file_age()`. Any caller comparing it against
        another clock must add that age, or it will systematically under-
        estimate where the video is (measured effect: a closed loop that pushed
        the picture forward every cycle, chasing its own 0.5s error).
        """
        try:
            lines = STATUS_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        if not lines or not lines[0].strip():
            return None
        try:
            return float(lines[0].strip())
        except ValueError:
            return None

    def status_file_age(self) -> float:
        """Seconds since the status file was last written (0.0 if unknown).

        mvm_control.lua rewrites STATUS_FILE every 0.5s, so a position read from
        it is between 0 and 0.5s old. The mtime is exactly when the value was
        produced, which makes it the correct amount to add when converting a
        stored position into "where the video is now".
        """
        try:
            return max(0.0, time.time() - STATUS_FILE.stat().st_mtime)
        except OSError:
            return 0.0

    def seek(self, position_sec: float, exact: bool = True) -> bool:
        """Jump the running video to an absolute position.

        This is how alignment is applied. `audio-delay` CANNOT do it: the
        video's own audio track is muted, so shifting it leaves the picture
        untouched. Measured: logs reported a computed offset while the video
        stayed at 00:00:11 against music at 00:19.

        NOTE (2026-10-07, session 8): a bare `seek` only appends a line to the
        command file -- it proves nothing about where the playhead went. The
        Lua side polls that file every 0.2s and mpv may CLAMP the request (past
        the end of the file) or land the keyframe slightly off for a non-exact
        seek. Fine alignment therefore uses `seek_verified()` below; this
        primitive stays for callers that genuinely do not need the readback
        (e.g. the manual-nudge path, where the user sees the result directly).
        """
        mode = "absolute+exact" if exact else "absolute"
        return self.command(f"seek {position_sec:.3f} {mode}")

    def seek_verified(self, position_sec: float, exact: bool = True,
                      tolerance: float = SEEK_VERIFY_TOLERANCE_SEC,
                      settle: float = SEEK_VERIFY_SETTLE_SEC,
                      attempts: int = SEEK_VERIFY_ATTEMPTS,
                      ) -> tuple[bool, float | None]:
        """Seek, then READ BACK the playhead and confirm the seek landed.

        Returns (landed, observed_position). `observed` is the position that was
        compared (already aged forward by the polling delay), or None when the
        status file gave us nothing.

        WHY THIS EXISTS (measured 2026-10-07, session 8)
        -----------------------------------------------
        `_seek_after_load` has verified its seek since session 7, but that
        verification only ever ran on the LOAD path. The fine-alignment seek --
        the one that decides whether the user sees a synced picture -- was a
        fire-and-forget `player.seek(target)`: follow.py logged
        "↻ 画面已校正到 83.5s" purely from the number it had COMPUTED.

        Two concrete ways that number can be wrong while the log still claims
        success:
          * the 0.2s command-file poll means the seek lands up to ~0.2s later
            than the music position we computed, and the video keeps playing
            from there, and
          * mpv CLAMPS an out-of-range target (a PV shorter than the music's
            position) instead of failing, so the playhead ends up somewhere
            else entirely with no error anywhere.

        The tolerance is asymmetric on purpose. A seek that lands slightly
        BEHIND the target is the normal case (the poll + keyframe snapping);
        landing measurably AHEAD is worse, because the picture then spoils the
        music. So `tolerance` is applied going forward and a tighter
        SEEK_VERIFY_LATE_TOLERANCE_SEC going backward. Both are well inside the
        deadband of the closed loop (ALIGN_LOOP_DEADBAND_SEC), so this check
        never fights the corrector -- it only catches gross failures.
        """
        mode = "absolute+exact" if exact else "absolute"
        for attempt in range(1, max(1, attempts) + 1):
            if not self.command(f"seek {position_sec:.3f} {mode}"):
                return (False, None)
            # Wait for the poll + mpv's own seek latency. A `time.sleep(settle)`
            # is deliberate here rather than a busy poll: the status file is
            # rewritten every 0.5s (mvm_control.lua), so reading faster than
            # that just returns the same sample again.
            time.sleep(settle)
            observed = self.get_position()
            if observed is None:
                # No readable position == no evidence. Report failure honestly
                # instead of assuming the seek worked (铁律 12).
                continue
            # The video kept playing while we waited, so the playhead we read
            # has advanced past the instant of the seek. Add that back before
            # comparing, otherwise every seek looks "late" by the settle time.
            expected = position_sec + settle
            error = observed - expected
            if -SEEK_VERIFY_LATE_TOLERANCE_SEC <= error <= tolerance:
                return (True, observed)
            if attempt < attempts:
                self._log_line(
                    f"  · seek 落点 {observed:.2f}s 偏离目标 {position_sec:.2f}s"
                    f"（{error:+.2f}s），重试 {attempt}/{attempts - 1}…"
                )
        return (False, self.get_position())

    def _log_line(self, msg: str) -> None:
        """Print a player-side diagnostic when logging is enabled.

        Kept on the controller (not the follower) because it reports a fact
        only the player can observe -- where the playhead ACTUALLY landed.
        """
        if self.log:
            print(msg, flush=True)

    # ---------------- playback ----------------

    def play_url(
        self,
        stream_url: str,
        start_sec: float = 0.0,
        mute: bool | None = None,
        audio_delay: float = 0.0,
    ) -> bool:
        """Show a video, reusing the existing window when possible.

        `start_sec` seeks into the video so it lines up with the audio that is
        already playing elsewhere. Without it the video would start at 0:00
        while the music is mid-track -- which is exactly the "huge offset"
        symptom this fixes.
        """
        effective_mute = self.mute if mute is None else mute

        # Held across the whole operation so the follower's poll loop cannot
        # stop() the process between start() and loadfile().
        with self._lifecycle_lock:
            # Reuse the running instance: no window flash, no re-init cost.
            if not self.running and not self.start():
                return False

            self.set_property("mute", effective_mute)
            if audio_delay:
                self.set_property("audio-delay", f"{audio_delay:.3f}")

            # Escape backslashes/quotes so Windows paths survive mpv's parser.
            # Measured (2026-10-07): a BACKSLASH windows path fails to load
            # (`Command loadfile: error in argument 1`, status file stays
            # EMPTY), while forward slashes work. Normalise to forward slashes.
            safe = stream_url.replace("\\", "/").replace('"', '\\"')
            if not self.command(f'loadfile "{safe}" replace'):
                # Control channel failed -> fall back to a fresh process.
                #
                # THE OLD PROCESS MUST BE GONE BEFORE THE NEW ONE STARTS
                # (measured 2026-10-08: the user saw TWO windows from ONE
                # daemon). The chain that produced it:
                #   * `command()` returns False when `running` is False, which
                #     happens as soon as the child's poll() returns -- even if
                #     the OS process (and its window) is still alive;
                #   * `stop()` then clears `self.proc` UNCONDITIONALLY, so a
                #     process that survived terminate() becomes an orphan that
                #     nothing references any more;
                #   * `_spawn_with_file()` started a SECOND mpv beside it.
                # So the fallback now clears stray windows explicitly, with no
                # pid to protect -- we own nothing at this point by definition.
                self.stop()
                strays = self.kill_stray_windows(allow_when_unknown=True)
                if strays:
                    self._log_line(f"  · 回退启动前清理了 {strays} 个残留视频窗口")
                return self._spawn_with_file(stream_url, start_sec, effective_mute)

            # Coarse alignment: jump the video to where the music already is.
            #
            # WHY NOT `loadfile ... start=N`: measured against mpv's own log,
            # `start=` is NOT a loadfile flag --
            #     [f][input] Invalid flag for option loadfile: start=40.00
            #     [e][input] Command loadfile: argument 2 can't be parsed
            # so the whole command was rejected and NOTHING loaded (the window
            # stayed empty and the status file never reported a path). That is
            # exactly why coarse alignment was never seen to work. `--start`
            # only exists as a command-line option, which cannot help us here
            # because the process is long-lived and reused across songs.
            #
            # So we seek, but instead of a blind `sleep(1.5)` (which raced the
            # network open and silently did nothing) we CONFIRM that the file
            # actually loaded and that the seek took effect. The user never saw
            # coarse alignment work; this loop is what makes it verifiable.
            #
            # THE RESULT IS NOW REPORTED, NOT DISCARDED (measured live
            # 2026-10-08). The follower logs "已开始播放（起点 Ns）" from the
            # value it REQUESTED, but a failed coarse seek leaves the picture at
            # 0:00 -- so the log claimed success while the video sat ~29s behind
            # the music (which the closed loop then classified as an anomaly).
            # A silent failure here is indistinguishable from success in the
            # log, so the caller must be able to see it (铁律 12).
            if start_sec and start_sec > 0.5:
                if not self._seek_after_load(start_sec):
                    self._log_line(
                        f"  · ⚠ 粗对齐 seek 未确认生效（请求 {start_sec:.1f}s）；"
                        f"画面可能停在片头，闭环会纠正")

        # Pin the window rectangle. mpv re-centres (and sometimes resizes) the
        # window a few seconds AFTER the video starts loading, so a one-shot
        # placement is not enough -- measured 953,500 -> 1191,439 about 9s in,
        # and in another run a cascade to 3181,1377 growing to 1414x1102.
        with self._geometry_lock:
            self._enforce_until = time.time() + GUARD_SETTLE_SECONDS
        self._start_geometry_guard()
        return True

    def _start_geometry_guard(self) -> None:
        """Keep the window at its pinned rectangle for the process lifetime.

        Measured: mpv re-centres its window not only shortly after loadfile
        (953,500 -> 1191,439 at ~9s) but also much later -- a trace showed it
        jumping to 3285,1428 about a minute in, right around an alignment seek.
        A guard that only ran for 20s after loadfile therefore missed it, so the
        guard now runs continuously until the process exits.

        Cost: one ctypes GetWindowRect per interval (~1.4us, measured). It used
        to shell out to window.ps1 at ~930ms per sample, which stretched the real
        period to ~1.4s and is why this guard previously lagged behind mpv.
        """
        with self._geometry_lock:
            if self._geometry_thread and self._geometry_thread.is_alive():
                return
            self._geometry_thread = threading.Thread(
                target=self._geometry_guard, daemon=True
            )
            self._geometry_thread.start()

    def _geometry_guard(self, interval: float = 0.5) -> None:
        """Runs until the mpv process exits, keeping the window where it belongs.

        Two regimes:
          * Within GUARD_SETTLE_SECONDS of a launch/loadfile -> ENFORCE the
            remembered rectangle. mpv re-centres itself in that window of time,
            so a moved window is mpv's doing and must be undone.
          * After it -> ADOPT a new rectangle instead of fighting it. The user
            drags the window where they want it; the old guard yanked it back
            within ~2.4s (01 §2.2), which is the "窗口放不住" complaint. The new
            position is persisted so it also survives a restart.
        """
        while True:
            if not self.running:
                return
            rect = self.get_window_rect()
            if rect is None:
                time.sleep(interval)
                continue
            # A DELIBERATE fullscreen / maximize (issue #2): leave the window
            # exactly as the user asked for it. We do NOT enforce, and we do NOT
            # persist this rectangle -- it describes a MODE, not the window size
            # the user chose, so saving it would make every later launch open
            # fullscreen (the same class of pollution as the giant-window bug).
            # mpv restores the previous rectangle when the mode ends, after which
            # this guard resumes on the pinned rect.
            fullscreen, maximized = read_window_mode()
            if fullscreen or maximized:
                time.sleep(interval)
                continue
            with self._geometry_lock:
                enforce = time.time() < self._enforce_until
                pinned = self._geometry
            if pinned is None:
                # First sighting defines the pinned rectangle.
                self._remember_geometry(rect)
            elif rect != pinned:
                if enforce:
                    # Just after a launch/loadfile mpv re-centres itself, so a
                    # moved window here is mpv's doing and must be undone.
                    x, y, w, h = pinned
                    self.set_window_rect(x, y, w, h)
                elif not rect_on_screen(rect):
                    # Runaway geometry, off-screen (measured: 3181,1377 /
                    # 3285,1428). It would be lost to the user, so undo it.
                    x, y, w, h = pinned
                    self.set_window_rect(x, y, w, h)
                elif ( _is_plausible_user_resize(pinned, rect)
                       and self._within_cumulative_growth(rect)):
                    # User moved/resized it somewhere sane: adopt + persist.
                    self._remember_geometry(rect, measurable=False)
                elif _looks_like_mpv_self_resize(rect):
                    # mpv grew itself to fill the screen WITHOUT fullscreen being
                    # on, which is the pollution this guard exists for. Undo it.
                    x, y, w, h = pinned
                    self.set_window_rect(x, y, w, h)
                else:
                    # On-screen, not mpv's self-enlargement signature, but past
                    # our conservative "plausible drag" heuristics -- e.g. the
                    # user deliberately made the window much bigger at once.
                    # LEAVE THE WINDOW ALONE (issue #2: the old code yanked it
                    # back, which the user experienced as "无法调整位置") and
                    # merely decline to remember it, so nothing gets persisted
                    # from a resize we are not confident about.
                    pass
            time.sleep(interval)

    def _within_cumulative_growth(self, rect: tuple[int, int, int, int]) -> bool:
        """Whether `rect` is still within MAX_CUMULATIVE_GROWTH of the launch size.

        Guards against mpv growing the window in steps that each look like a
        plausible drag (see _is_plausible_user_resize). `self._launch_area` is
        the size at start(); if it is unknown (e.g. a probe of an externally
        started mpv) the check is skipped rather than guessed.
        """
        if not self._launch_area:
            return True
        area = float(rect[2] * rect[3])
        return area <= MAX_CUMULATIVE_GROWTH * self._launch_area

    def _spawn_with_file(self, stream_url: str, start_sec: float, mute: bool) -> bool:
        """Fallback: start a new process with the file on the command line.

        Terminates any existing child first: this path is reached when the
        command channel failed, and the caller has already called stop() -- but
        stop() only clears `self.proc` when it could actually reap the process,
        so a stubborn instance could otherwise stay on screen alongside the new
        one (the "two windows" report, 2026-10-08).
        """
        self._terminate_proc()
        args = self._base_args()
        args = [a for a in args if not a.startswith("--mute") and not a.startswith("--volume")]
        args.append("--mute=yes" if mute else "--mute=no")
        if start_sec and start_sec > 0.5:
            args.append(f"--start={start_sec:.2f}")
        args.append(stream_url)
        try:
            self.proc = subprocess.Popen(
                args, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        return True

    def play_resolved(self, page_url: str, start_sec: float = 0.0, mute: bool | None = None) -> bool:
        """Resolve a page URL then play it. Raises ResolveError on failure."""
        stream = resolve_stream_url(page_url, want="both")
        return self.play_url(stream, start_sec=start_sec, mute=mute)

    def probe(self, seconds: float = 3.0) -> dict:
        """Diagnostic helper: return process state as a dict."""
        info = {
            "running": self.running,
            "pid": self.proc.pid if self.proc else None,
            "returncode": self.proc.poll() if self.proc else None,
        }
        if seconds > 0 and self.running:
            time.sleep(seconds)
            info["still_running_after"] = self.running
        return info


def _main() -> int:
    """Smoke test.

    python player.py <page-url> [--mode both|video|audio] [--seconds N]
    python player.py --resolve <page-url>       # just print the stream URL
    """
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2

    if argv[0] == "--resolve":
        url = argv[1]
        try:
            print(resolve_stream_url(url))
            return 0
        except ResolveError as exc:
            print(f"resolve failed: {exc}")
            return 1

    url = argv[0]
    mode = "both"
    secs = 8.0
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]
    if "--seconds" in argv:
        secs = float(argv[argv.index("--seconds") + 1])

    cookies = find_cookies()
    print(f"cookies: {cookies or '(none)'}")

    try:
        stream = resolve_stream_url(url, want=mode)
        print(f"resolved ({mode}): {stream[:110]}...")
    except ResolveError as exc:
        print(f"resolve failed: {exc}")
        return 1

    ctl = MpvController(mute=True)
    if not ctl.play_url(stream):
        print("failed to start isolated mpv")
        return 1
    print(f"mpv started (pid={ctl.proc.pid}), playing {secs}s...")
    time.sleep(secs)
    print("state:", json.dumps(ctl.probe(seconds=0)))
    ctl.stop()
    print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
