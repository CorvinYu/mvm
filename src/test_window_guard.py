"""test_window_guard.py -- offline criteria for issue #2 (无法全屏 / 无法调整位置).

Run:  python src/test_window_guard.py          (expect all green)
      python src/test_window_guard.py --old    (expect the fullscreen cases to FAIL)

WHAT WAS WRONG (issue #2)
-------------------------
`_geometry_guard` polls the window rectangle every 0.5s and, on a change it
cannot attribute to the user, calls `set_window_rect(pinned)` to yank the window
back. Its "is this the user?" test refused near-fullscreen rectangles and
sudden growth -- so when the user pressed F (or maximized, or enlarged the
window a lot at once), the guard classified a DELIBERATE action as pollution and
undid it within half a second.

The fix has three parts, all asserted here:
  §1  mpv's own `fullscreen` / `window-maximized` state is honoured: the guard
      steps aside entirely, and does not persist the screen-sized rectangle.
  §2  An on-screen rectangle that is NOT mpv's self-enlargement signature is
      LEFT ALONE even if it fails the conservative "plausible drag" heuristics
      -- the guard merely declines to remember it.
  §3  The protections that this guard exists for are still intact: off-screen
      runaway geometry and mpv's near-fullscreen self-resize are still undone.

HOW THIS TESTS THE REAL CODE
----------------------------
The real `MpvController._geometry_guard` is invoked on a fake controller that
records every `set_window_rect` (a "snap back") and `_remember_geometry` (an
"adopt"). `read_window_mode()` reads the module-level STATUS_FILE, so the test
points `player.STATUS_FILE` at a scratch file to control the reported mode. No
mpv process is needed, so this runs anywhere and is fast enough for CI.

`--old` mode runs a faithful transcription of the PRE-FIX branch structure
(`_legacy_guard`) and must report a snap-back for the deliberate-fullscreen
cases. Without that, the criteria cannot claim to measure the fix.
"""
from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import player  # noqa: E402

_old_mode = "--old" in sys.argv
_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


SCRATCH = SRC.parent / "state" / "_window_guard_test"


def _status(values: dict) -> Path:
    """Write a STATUS_FILE with the given mode flags (issue #2 lines 6-7)."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / "_mvm_status.txt"
    fullscreen = values.get("fullscreen", "no")
    maximized = values.get("maximized", "no")
    path.write_text(
        "12.500\n"          # 1 time-pos
        "no\n"              # 2 pause
        "C:/x.mp4\n"        # 3 path
        "has-window\n"      # 4 geometry marker
        "0.000\n"           # 5 manual_offset
        f"{fullscreen}\n"   # 6 fullscreen
        f"{maximized}\n",   # 7 window-maximized
        encoding="utf-8",
    )
    # ALSO repoint the module-level paths. Writing the file alone is not enough:
    # `read_window_mode()` reads `player.MODE_FILE` / `player.STATUS_FILE`, so
    # without this the guard kept reading the real (stale, 5-line) status file
    # and answered "not fullscreen" -- which made §1 fail in FIXED mode and was
    # indistinguishable from a product bug. Same class of mistake as NOTES §3.2:
    # a check that does not exercise the real path proves nothing.
    #
    # The ATOMIC mode file is the primary source, so the helper must produce it
    # too -- otherwise the tests would silently exercise only the legacy
    # fallback and never cover the path production actually uses.
    mode_path = SCRATCH / "_mvm_window_mode.txt"
    mode_path.write_text(f"{fullscreen}\n{maximized}\n", encoding="utf-8")
    player.MODE_FILE = mode_path
    player.MODE_TMP = SCRATCH / "_mvm_window_mode.tmp"
    player.STATUS_FILE = path
    return path


class FakeCtl:
    """Minimal stand-in for MpvController that records guard decisions."""

    def __init__(self, rects, pinned, *, enforce_until=0.0, launch_area=None,
                 mode=("no", "no")):
        self._rects = list(rects)
        self._geometry = pinned
        self._enforce_until = enforce_until
        self._launch_area = launch_area
        self._geometry_lock = threading.Lock()
        self.running = True
        self.snaps: list[tuple[int, int, int, int]] = []
        self.adopted: list[tuple[int, int, int, int]] = []
        self.logs: list[str] = []
        self._last_drag_adopt_rect = None
        _status({"fullscreen": mode[0], "maximized": mode[1]})

    def _log_line(self, msg: str) -> None:
        self.logs.append(msg)

    def get_window_rect(self):
        if not self._rects:
            self.running = False       # ends the guard's `while True`
            return None
        return self._rects.pop(0)

    def set_window_rect(self, x, y, w, h):     # the "yank back" action
        self.snaps.append((x, y, w, h))
        return True

    def _remember_geometry(self, rect, measurable=True):   # the "adopt" action
        self.adopted.append(rect)
        self._geometry = rect

    def _within_cumulative_growth(self, rect):
        return player.MpvController._within_cumulative_growth(self, rect)


def _pre_fix_rect_on_screen(rect):
    """The rule this task replaced: the rect must fit WHOLLY inside one monitor.

    Used ONLY by --old mode. Without it the legacy guard would keep calling the
    FIXED `player.rect_on_screen` and §6c would pass in BOTH modes -- a check
    that cannot fail, which is exactly the trap NOTES §3.2 warns about.
    """
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return False
    for mx, my, mw, mh in player.all_monitor_rects():
        if mw <= 0 or mh <= 0:
            continue
        if x >= mx and y >= my and x + w <= mx + mw and y + h <= my + mh:
            return True
    return False


def _legacy_guard(ctl, interval=0.0):
    """Faithful transcription of the PRE-FIX decision chain (issue #2)."""
    while True:
        if not ctl.running:
            return
        rect = ctl.get_window_rect()
        if rect is None:
            return
        with ctl._geometry_lock:
            enforce = False  # test drives post-settle behaviour
            pinned = ctl._geometry
        if pinned is None:
            ctl._remember_geometry(rect)
        elif rect != pinned:
            if enforce:
                ctl.set_window_rect(*pinned)
            elif (_pre_fix_rect_on_screen(rect)
                  and player._is_plausible_user_resize(pinned, rect)
                  and ctl._within_cumulative_growth(rect)):
                ctl._remember_geometry(rect, measurable=False)
            else:
                ctl.set_window_rect(*pinned)      # <-- the bug: always yanks back
        if interval:
            import time
            time.sleep(interval)


def run_guard(ctl):
    """Invoke the REAL guard (or the legacy transcription in --old mode)."""
    if _old_mode:
        _legacy_guard(ctl)
    else:
        player.MpvController._geometry_guard(ctl, interval=0.0)


# --------------------------------------------------------------------------
print(f"\n模式：{'--old（旧码，预期 §1/§1b/§2/§6c 失败）' if _old_mode else '正式（预期全绿）'}")
wx, wy, ww, wh = player.work_area()
print(f"work_area = {ww}x{wh}\n")

PINNED = (953, 500, 655, 397)                    # the normal small window
SCREEN_RECT = (wx, wy, ww, wh)                   # what fullscreen looks like
MAX_RECT = (wx, wy, ww, wh - 40)                 # maximized (title-bar-less area)
BIG_USER = (wx + 40, wy + 40, 1000, 700)         # big, deliberate, but not near-fullscreen
# DERIVED, not hard-coded. WHY (measured 2026-10-08): this used to be the literal
# (3181,1377,655,397) recorded on the machine where the bug was found. When
# `work_area()` later reported 3840x2088 instead of 1920x1032 (display
# scaling/resolution changed), that rectangle became genuinely ON-screen, so the
# guard correctly adopted it and the "runaway geometry is undone" criterion
# failed -- a TEST failure that looked exactly like a product regression. The
# coordinates must be expressed relative to whatever the current work area is.
RUNAWAY = (wx + ww + 300, wy + wh + 300, 655, 397)     # fully off-screen
SELF_RESIZE = (wx, wy, int(ww * 0.9), int(wh * 0.9))   # mpv's own near-fullscreen

# --------------------------------------------------------------------------
print("§1 用户按 F 全屏 → 守卫必须让开（不得拉回、不得持久化）")
# --------------------------------------------------------------------------
ctl = FakeCtl([SCREEN_RECT], PINNED, launch_area=655 * 397,
              mode=("yes", "no"))
run_guard(ctl)
check("全屏时没有被拉回（issue #2 的核心）", not ctl.snaps,
      f"snaps={ctl.snaps}")
check("全屏矩形没有被持久化为窗口尺寸（否则下次启动就全屏）",
      not ctl.adopted, f"adopted={ctl.adopted}")
check("pinned 保持为全屏前的小窗", ctl._geometry == PINNED,
      f"{ctl._geometry}")

print("\n§1b 用户最大化 → 同样让开")
ctl = FakeCtl([MAX_RECT], PINNED, launch_area=655 * 397,
              mode=("no", "yes"))
run_guard(ctl)
check("最大化时没有被拉回", not ctl.snaps, f"snaps={ctl.snaps}")
check("最大化矩形没有被持久化", not ctl.adopted, f"adopted={ctl.adopted}")

# --------------------------------------------------------------------------
print("§2 用户把窗口调大（非全屏、未越界）→ 留在原处，只是不记住")
# --------------------------------------------------------------------------
ctl = FakeCtl([BIG_USER], PINNED, launch_area=655 * 397,
              mode=("no", "no"))
run_guard(ctl)
# Assert ALL the preconditions this branch depends on. If any stops holding (a
# different resolution, changed constants) the case would silently become
# vacuous -- it would "pass" without exercising the branch it exists for.
implausible = not player._is_plausible_user_resize(PINNED, BIG_USER)
not_self_resize = not player._looks_like_mpv_self_resize(BIG_USER)
on_screen = player.rect_on_screen(BIG_USER)
beyond_growth = not ctl._within_cumulative_growth(BIG_USER)
check("前提1：超出“像用户拖拽”判据（否则会走采纳分支）", implausible,
      f"area={BIG_USER[2] * BIG_USER[3]} > 1.5*pinned={1.5 * PINNED[2] * PINNED[3]:.0f}")
check("前提2：不是 mpv 自放大签名（否则会被拉回）", not_self_resize,
      f"area={BIG_USER[2] * BIG_USER[3]} <= 0.7*work={0.7 * ww * wh:.0f}")
check("前提3：超出累计增长上限（否则会被采纳）", beyond_growth,
      f"area={BIG_USER[2] * BIG_USER[3]} > 2*launch={2 * 655 * 397}")
check("前提4：仍在屏幕内（否则会被当越界拉回）", on_screen, f"rect={BIG_USER}")
check("用户调大的窗口没有被拉回（修复“无法调整位置”）", not ctl.snaps,
      f"snaps={ctl.snaps}")
check("未把不确定的尺寸写进 window.json", not ctl.adopted, f"adopted={ctl.adopted}")

# --------------------------------------------------------------------------
print("§3 原有保护必须仍在（否则就是拿掉防护换来的“能全屏”）")
# --------------------------------------------------------------------------
ctl = FakeCtl([RUNAWAY], PINNED, launch_area=655 * 397,
              mode=("no", "no"))
run_guard(ctl)
check("越界到屏幕外的窗口仍被拉回", ctl.snaps == [PINNED], f"snaps={ctl.snaps}")

ctl = FakeCtl([SELF_RESIZE], PINNED, launch_area=655 * 397,
              mode=("no", "no"))
run_guard(ctl)
check("mpv 自己放大到近全屏（fullscreen=no）仍被拉回",
      ctl.snaps == [PINNED], f"snaps={ctl.snaps}")

print("\n§3b 正常的用户移动仍应被采纳并持久化")
MOVED = (400, 300, 655, 397)      # same size, different position
ctl = FakeCtl([MOVED], PINNED, launch_area=655 * 397, mode=("no", "no"))
run_guard(ctl)
check("同尺寸移动被采纳（未误伤）", ctl.adopted == [MOVED] and not ctl.snaps,
      f"adopted={ctl.adopted} snaps={ctl.snaps}")

# --------------------------------------------------------------------------
print("§4 两个来源都不可用时的保守行为（守卫必须保持启用）")
# --------------------------------------------------------------------------
SCRATCH.mkdir(parents=True, exist_ok=True)
short = SCRATCH / "_mvm_status.txt"
short.write_text("12.5\nno\nC:/x.mp4\nhas-window\n0.000\n", encoding="utf-8")
player.STATUS_FILE = short
player.MODE_FILE = SCRATCH / "_no_mode_file.txt"      # the atomic file is absent
player.reset_window_mode_cache()
check("模式文件缺失 + 状态文件只有 5 行 + 无历史值 -> (False, False)",
      player.read_window_mode() == (False, False),
      f"{player.read_window_mode()}")
player.STATUS_FILE = SCRATCH / "_does_not_exist.txt"
check("两个文件都不存在且无历史值时返回 (False, False)（守卫保持启用）",
      player.read_window_mode() == (False, False))

# --------------------------------------------------------------------------
print("\n§5 ★ 状态文件被截断时，答案必须不变（这就是把模式挪进原子文件的理由）")
# --------------------------------------------------------------------------
# WHY THIS CASE IS THE POINT OF THE FIX (measured 2026-10-08, twice):
#   mvm_control.lua rewrites the status file with a plain truncating write every
#   0.5s. Two live-probe runs lost a fullscreen to it: the guard caught a torn
#   read, concluded "not fullscreen", and its ENFORCE branch (active ~12s after a
#   launch) snapped the window back. Caching the last good value only NARROWED
#   the window -- right after entering fullscreen the cache still held the older
#   "no", so a torn read returned the wrong answer anyway.
#   The mode now lives in its own file published via os.rename, which is atomic,
#   so the status file's races simply cannot affect this decision.
good = _status({"fullscreen": "yes", "maximized": "no"})
player.reset_window_mode_cache()
check("原子模式文件被解析为 (True, False)", player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

good.write_text("", encoding="utf-8")            # status file caught mid-truncate
check("状态文件被截断为空时答案仍为 (True, False)  ★ 核心",
      player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

good.write_text("12.5\nno\n", encoding="utf-8")  # status file half written
check("状态文件只写了一半时答案仍为 (True, False)",
      player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

# Now the atomic file also disappears (e.g. mpv just exited): the legacy status
# source + the cache must keep the answer sane rather than invent "not fullscreen".
player.MODE_FILE = SCRATCH / "_gone_mode.txt"
check("模式文件消失时回退到上次已知值 (True, False)",
      player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

player.reset_window_mode_cache()
check("reset_window_mode_cache() 后不再回退（新进程不继承旧模式）",
      player.read_window_mode() == (False, False),
      f"{player.read_window_mode()}")

# --------------------------------------------------------------------------
print("\n§6 ★ 2026-10-10：按住左键拖动时守卫让开 + 跨屏/贴边窗口必须被容忍")
# --------------------------------------------------------------------------
# WHY THESE CASES EXIST (live probe src/probe_guard_resize.py reproduced both):
#   ① 用户："在主屏幕内无法移动，松手后立刻跳回原位" —— the ENFORCE regime
#      (12s after every loadfile) undoes ANY move, so grabbing the window in that
#      window of time loses it on release. The guard now stands down while the
#      left button is held.
#   ② 用户："靠边自动改变窗口大小填充下半屏，依然会跳回原来的样子" —— the guard
#      required a rect to fit WHOLLY inside one monitor, so a window spanning two
#      (which is what snapping on a narrower secondary produces) was treated as
#      runaway geometry. It now requires >= 50% of the window to be on screen.
import time as _time

DRAGGED = (400, 300, 655, 397)

if not _old_mode:
    # 6a: enforce period + left button held -> must NOT be yanked.
    ctl = FakeCtl([DRAGGED], PINNED, enforce_until=_time.time() + 100.0,
                  launch_area=655 * 397, mode=("no", "no"))
    _real_drag = player.user_is_dragging
    player.user_is_dragging = lambda: True
    try:
        run_guard(ctl)
    finally:
        player.user_is_dragging = _real_drag
    check("ENFORCE 期内按住左键不拉回（用户现象①）", not ctl.snaps,
          f"snaps={ctl.snaps}")

# 6b: same scene WITHOUT the button -> still yanked, i.e. 6a did not disable the
# guard (a check that cannot fail would be worthless).
if not _old_mode:
    ctl = FakeCtl([DRAGGED], PINNED, enforce_until=_time.time() + 100.0,
                  launch_area=655 * 397, mode=("no", "no"))
    run_guard(ctl)
    check("同一场景未按左键仍拉回（对照组，证明 §6a 不是把守卫关掉）",
          ctl.snaps == [PINNED], f"snaps={ctl.snaps}")

# 6c: a rect poking off one monitor's edge. Derived from the REAL monitor layout
# (never hard-coded -- NOTES §5 records a hard-coded coordinate that turned into
# a phantom regression), so this holds on a single-monitor machine too.
_mons = player.all_monitor_rects()
_ax, _ay, _aw, _ah = _mons[0]
# 400 wide starting 300px left of the monitor's right edge: 300px (75%) stays on
# screen and 100px pokes past the edge, so it fits inside NO single monitor while
# remaining mostly visible -- the shape a Snap produces on a narrower secondary.
SPANNING = (_ax + _aw - 300, _ay + 100, 400, 300)
_frac = player.rect_overlap_fraction(SPANNING)
check(f"越出屏边的窗口重叠比例在 (0.5,1.0)：{_frac:.2f}",
      0.5 < _frac <= 1.0, f"rect={SPANNING}")
check("越出屏边的窗口判为『在屏内』", player.rect_on_screen(SPANNING) is True,
      f"重叠={_frac:.2f}")

ctl = FakeCtl([SPANNING], PINNED, launch_area=655 * 397, mode=("no", "no"))
run_guard(ctl)
check("越出屏边的窗口不被拉回（用户现象②）", not ctl.snaps, f"snaps={ctl.snaps}")

# 6d: the runaway geometry this guard exists for must STILL be rejected --
# proving the loosened rule kept its protection.
RUNAWAY = (_ax + _aw + 2000, _ay + 2000, 982, 596)
check("完全在屏外的失控矩形仍判屏外（保护未丢）",
      player.rect_on_screen(RUNAWAY) is False,
      f"重叠={player.rect_overlap_fraction(RUNAWAY):.2f}")
ctl = FakeCtl([RUNAWAY], PINNED, launch_area=655 * 397, mode=("no", "no"))
run_guard(ctl)
check("完全在屏外的失控矩形仍被拉回", ctl.snaps == [PINNED], f"snaps={ctl.snaps}")

# 6e/6f: the SECOND user report (2026-10-10). Standing down only WHILE the button
# is held was not enough: on release the still-active enforce period yanked the
# window back, so the user still saw "无法移动". And because a user-enlarged
# window was never recorded, every song switch restored the old small rect
# ("切歌后窗口从原本的大变成默认的小"). Both are fixed by adopting the dragged
# rectangle and ending the enforce period.
if not _old_mode:
    DRAGGED_BIG = (100, 100, 1400, 900)      # what a user's drag produces
    ctl = FakeCtl([DRAGGED_BIG], PINNED, enforce_until=_time.time() + 100.0,
                  launch_area=655 * 397, mode=("no", "no"))
    player.user_is_dragging = lambda: True
    try:
        run_guard(ctl)
    finally:
        player.user_is_dragging = _real_drag
    check("§6e 拖动期间被采纳、不拉回", not ctl.snaps, f"snaps={ctl.snaps}")
    check("§6e 拖动后 pinned = 用户拖出的矩形", ctl._geometry == DRAGGED_BIG,
          f"pinned={ctl._geometry}")
    check("§6e 拖动后强制期被结束（松手也不会再拉回）",
          ctl._enforce_until == 0.0, f"enforce_until={ctl._enforce_until}")
    check("§6e 采纳时留下了可验证的日志",
          any("采纳" in m for m in ctl.logs), f"logs={ctl.logs[:2]}")

    # 6f: the NEXT song switch re-arms the enforce period; the window must stay
    # where the user put it, because that is now the pinned rectangle.
    ctl = FakeCtl([DRAGGED_BIG, DRAGGED_BIG], PINNED,
                  enforce_until=_time.time() + 100.0,
                  launch_area=655 * 397, mode=("no", "no"))
    player.user_is_dragging = lambda: True
    try:
        run_guard(ctl)
    finally:
        player.user_is_dragging = _real_drag
    check("§6f 采纳后再次进入强制期（切歌）不拉回旧小窗",
          not ctl.snaps, f"snaps={ctl.snaps}")

    # 6g: dragged almost entirely off the desktop -> must NOT be adopted, or the
    # window would be remembered somewhere the user cannot reach it. This is the
    # DRAG_MIN_ON_SCREEN_FRACTION guard, and it is the one drag case where the
    # guard must still fight the mouse.
    ctl = FakeCtl([RUNAWAY], PINNED, enforce_until=_time.time() + 100.0,
                  launch_area=655 * 397, mode=("no", "no"))
    player.user_is_dragging = lambda: True
    try:
        run_guard(ctl)
    finally:
        player.user_is_dragging = _real_drag
    check("§6g 拖到几乎完全出屏时不采纳（防窗口永久丢失）",
          not ctl.adopted and ctl.snaps == [PINNED],
          f"adopted={ctl.adopted} snaps={ctl.snaps}")

# --------------------------------------------------------------------------
passed = sum(1 for ok, _, _ in _results if ok)
total = len(_results)
print("\n" + "=" * 60)
print(f"窗口守卫判据: {passed}/{total} 通过"
      + ("  [--old 模式，§1/§1b/§2 预期失败]" if _old_mode else ""))
if passed < total:
    print("\n失败项：")
    for ok, name, detail in _results:
        if not ok:
            print(f"  - {name}  {detail}")
print("=" * 60)
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(0 if passed == total else 1)
