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
    # ALSO repoint the module-level path. Writing the file alone is not enough:
    # `read_window_mode()` reads `player.STATUS_FILE`, so without this the guard
    # kept reading the real (stale, 5-line) status file and answered "not
    # fullscreen" -- which made §1 fail in FIXED mode and was indistinguishable
    # from a product bug. Same class of mistake as NOTES §3.2: a check that does
    # not exercise the real path proves nothing.
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
        _status({"fullscreen": mode[0], "maximized": mode[1]})

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
            elif (player.rect_on_screen(rect)
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
print(f"\n模式：{'--old（旧码，预期 §1/§2 失败）' if _old_mode else '正式（预期全绿）'}")
wx, wy, ww, wh = player.work_area()
print(f"work_area = {ww}x{wh}\n")

PINNED = (953, 500, 655, 397)                    # the normal small window
SCREEN_RECT = (0, 0, ww, wh)                     # what fullscreen looks like
MAX_RECT = (0, 0, ww, wh - 40)                   # maximized (title-bar-less area)
BIG_USER = (900, 300, 900, 700)                  # big, but < 70% of the work area
RUNAWAY = (3181, 1377, 655, 397)                 # measured off-screen jump
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
implausible = not player._is_plausible_user_resize(PINNED, BIG_USER)
check("前提：该尺寸确实超出保守的“像用户拖拽”判据", implausible,
      f"area={BIG_USER[2] * BIG_USER[3]} pinned_area={PINNED[2] * PINNED[3]}")
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
print("§4 模式不可用时的保守行为（旧 lua / 文件截断）")
# --------------------------------------------------------------------------
SCRATCH.mkdir(parents=True, exist_ok=True)
short = SCRATCH / "_mvm_status.txt"
short.write_text("12.5\nno\nC:/x.mp4\nhas-window\n0.000\n", encoding="utf-8")
player.STATUS_FILE = short
player.reset_window_mode_cache()
check("只有 5 行且无历史值时 read_window_mode 返回 (False, False)（守卫保持启用）",
      player.read_window_mode() == (False, False),
      f"{player.read_window_mode()}")
missing = SCRATCH / "_does_not_exist.txt"
player.STATUS_FILE = missing
check("状态文件缺失且无历史值时返回 (False, False)",
      player.read_window_mode() == (False, False))

# --------------------------------------------------------------------------
print("\n§5 截断写竞态：读到半截文件必须回退到上次已知值")
# --------------------------------------------------------------------------
# WHY THIS CASE EXISTS (measured 2026-10-08): mvm_control.lua rewrites the
# status file with a plain truncating write every 0.5s, so a reader can catch it
# EMPTY or PARTIAL. Running the live fullscreen probe three times, the third run
# had its window yanked OUT of fullscreen again: on one tick the guard read a
# truncated file, concluded "not fullscreen", and its ENFORCE branch (active for
# ~12s after a launch) snapped the window back. Caching the last good reading
# fixes it -- and this case must FAIL if that cache is removed.
good = _status({"fullscreen": "yes", "maximized": "no"})
player.reset_window_mode_cache()
check("完整文件被正确解析为 (True, False)", player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

good.write_text("", encoding="utf-8")          # caught mid-truncate
check("文件被截断为空时回退到上次已知值 (True, False)（否则全屏会被拉回）",
      player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

good.write_text("12.5\nno\n", encoding="utf-8")  # partial write
check("文件只写了一半时仍回退到上次已知值",
      player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

player.STATUS_FILE = SCRATCH / "_nope.txt"
check("文件消失时仍回退到上次已知值",
      player.read_window_mode() == (True, False),
      f"{player.read_window_mode()}")

player.reset_window_mode_cache()
check("reset_window_mode_cache() 后不再回退（新进程不继承旧模式）",
      player.read_window_mode() == (False, False),
      f"{player.read_window_mode()}")

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
