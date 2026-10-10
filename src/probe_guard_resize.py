"""Live probe: which geometry-guard branch fights the user's window changes?

WHY THIS EXISTS (measured 2026-10-10, user report):
    ① "在主屏幕内无法移动，松手后立刻跳回原位"（间歇）
    ② "靠边自动改变窗口大小填充下半屏，依然会跳回原来的样子"
    Cross-monitor movement was fixed (state/window.json now records a secondary-
    monitor rect), so these are the remaining complaints.

WHAT IT SEPARATES
    The guard has an ENFORCE window (`GUARD_SETTLE_SECONDS = 12s` after every
    loadfile/start) in which ANY move is undone, because mpv re-centres itself
    then. A complaint inside that window is the designed behaviour, not a bug.
    Outside it, a move is meant to be adopted (or at worst left alone).

    So the probe tests the SAME two operations twice: during the enforce window
    and after it, and prints the guard's own decision inputs for each. If a move
    is undone OUTSIDE the window, that is a real defect and the printed inputs
    say which branch did it.

    It also hammers `all_monitor_rects()`/`_largest_monitor_area()` while the
    guard thread is live: `_largest_monitor_area()` returning 0 would make
    `0.7 * biggest` collapse to 0.7, so EVERY rectangle would look like a
    self-resize and be yanked back -- which would explain "cannot move at all"
    on any monitor. That failure mode must be ruled in or out by measurement.

Runs the ISOLATED mpv copy via the production controller (never mpv.net).
Requires state/_demo_pv.mp4.

Run:  python src\\probe_guard_resize.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import player as P  # noqa: E402

DEMO = ROOT / "state" / "_demo_pv.mp4"

_results: list[tuple[str, bool, str]] = []

# Every set_window_rect call, tagged with whether the GUARD made it. Lets the
# probe prove WHO moved the window instead of inferring it from the outcome.
YANK_CALLS: list[tuple] = []


def install_spy(ctl) -> None:
    """Wrap set_window_rect so guard-initiated moves are attributable.

    The guard calls `self.set_window_rect(...)` from its own thread, so an
    INSTANCE attribute shadows the method and the call is visible here. Calls
    made by the probe itself are recorded too, but the probe only calls
    set_window_rect outside `try_change`'s measurement window.
    """
    original = ctl.set_window_rect

    def spy(x, y, w, h):
        YANK_CALLS.append((x, y, w, h, time.time()))
        return original(x, y, w, h)

    ctl.set_window_rect = spy  # type: ignore[method-assign]


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def decision_inputs(ctl, target) -> str:
    """Reproduce exactly the inputs the guard's branches consult.

    NOTE: `target` must be the REQUESTED rect, not what the window ended up as.
    Passing the post-yank rect compares the pinned rect with itself and reports
    "plausible_resize=True" for everything -- which sent this probe's first
    version down the wrong path.
    """
    pinned = ctl._geometry
    enforce = time.time() < ctl._enforce_until
    return (
        f"enforce={enforce}(剩 {max(0.0, ctl._enforce_until - time.time()):.1f}s) "
        f"rect_on_screen={P.rect_on_screen(target)} "
        f"plausible_resize={P._is_plausible_user_resize(pinned, target) if pinned else 'n/a'} "
        f"mpv_self_resize={P._looks_like_mpv_self_resize(target)} "
        f"within_cum_growth={ctl._within_cumulative_growth(target)}"
    )


def try_change(ctl, target, label, settle: float = 2.5) -> tuple[bool, tuple | None]:
    """Apply a rect, wait, and report whether the guard let it stand."""
    print(f"\n  → {label}: 请求 {target}")
    print(f"    判据输入(按请求值): {decision_inputs(ctl, target)}")
    before = len(YANK_CALLS)
    ctl.set_window_rect(*target)
    time.sleep(settle)
    got = ctl.get_window_rect()
    # -1: the probe's own call above also goes through the spy.
    yanks = max(0, len(YANK_CALLS) - before - 1)
    kept = got is not None and abs(got[0] - target[0]) <= 12 and abs(got[1] - target[1]) <= 12 \
        and abs(got[2] - target[2]) <= 24 and abs(got[3] - target[3]) <= 24
    print(f"    实际 {got}  → {'保留' if kept else '被改回/拉回'}"
          f"（期间守卫调用 set_window_rect {yanks} 次）")
    if yanks:
        print(f"    守卫最后拉回到: {YANK_CALLS[-1][:4]}")
    return kept, got


def main() -> int:
    if not DEMO.exists():
        print(f"✗ 缺少测试媒体: {DEMO}")
        return 1
    print(f"媒体: {DEMO}")
    print("=" * 74)

    ctl = P.MpvController(log=True)
    try:
        if not ctl.play_url(str(DEMO), start_sec=2.0):
            print("✗ play_url 失败")
            return 1
        install_spy(ctl)

        # ---- §1 within the enforce window (designed to undo moves) ---------
        print("\n§1 ENFORCE 期内（起播后 12s 内）—— 按设计应拉回")
        time.sleep(1.0)
        base = ctl.get_window_rect()
        print(f"  基线 {base}")
        if base:
            moved = (base[0] + 150, base[1] + 90, base[2], base[3])
            kept, _ = try_change(ctl, moved, "仅在原屏内小幅移动")
            check("ENFORCE 期内移动被拉回（既有设计，用户现象①）", not kept,
                  "被拉回=符合设计" if not kept else "竟然保留了")

        # ---- §2 after the enforce window ----------------------------------
        wait = max(0.0, ctl._enforce_until - time.time()) + 1.5
        print(f"\n§2 等待 enforce 期结束（还需 {wait:.1f}s）…")
        time.sleep(wait)

        # Park the window on the PRIMARY monitor first, because the user's
        # complaint ② ("靠边改变大小填充下半屏") happens on the screen they are
        # working on, and the previous version of this probe measured from a
        # secondary-monitor position -- which produced a CROSS-MONITOR rect that
        # `rect_on_screen` rejects for an unrelated reason.
        print("\n  把窗口先放回主屏 (200, 200) 再测")
        ctl.set_window_rect(200, 200, 976, 579)
        time.sleep(2.0)
        time.sleep(0.0)          # keep the guard's own poll in view
        base = ctl.get_window_rect()
        print(f"  主屏基线 {base}  pinned={ctl._geometry}")

        if base:
            # 2a: pure move on the primary monitor -- must be adopted.
            moved = (base[0] + 150, base[1] + 90, base[2], base[3])
            kept, _ = try_change(ctl, moved, "§2a 主屏内仅移动（尺寸不变）")
            check("ENFORCE 期外：主屏内纯移动必须保留", kept)

            # 2b: Snap to half screen width (the left/right snap).
            base2 = ctl.get_window_rect() or moved
            half = (0, 0, 1920, 1044)
            kept, got = try_change(ctl, half, "§2b 贴左边 → 半屏宽 (0,0,1920,1044)", settle=3.0)
            check("ENFORCE 期外：主屏贴边半屏宽必须保留", kept, f"实际 {got}")

            # 2c: Snap to the BOTTOM half -- "填充下半部分整个屏幕", the exact
            # shape the user described.
            bottom = (0, 1044, 3840, 1044)
            kept, got = try_change(ctl, bottom, "§2c 贴边 → 下半屏 (0,1044,3840,1044)",
                                   settle=3.0)
            check("ENFORCE 期外：主屏贴边下半屏必须保留（用户现象②）", kept,
                  f"实际 {got}")

            # 2d: a window deliberately spanning two monitors. Windows allows
            # this and a user dragging across the seam produces it naturally.
            across = (-800, 300, 1600, 900)
            kept, got = try_change(ctl, across, "§2d 横跨两块屏 (-800,300,1600,900)",
                                   settle=3.0)
            check("ENFORCE 期外：横跨两屏的窗口应被容忍", kept, f"实际 {got}")

        # ---- §3 hammer the monitor helpers while the guard thread runs -----
        print("\n§3 压测 all_monitor_rects() / _largest_monitor_area()（guard 线程存活）")
        bad_rects = 0
        zero_areas = 0
        exceptions: list[str] = []
        for _ in range(400):
            try:
                rects = P.all_monitor_rects()
                if not rects or any(w <= 0 or h <= 0 for _x, _y, w, h in rects):
                    bad_rects += 1
                if P._largest_monitor_area() <= 0:
                    zero_areas += 1
            except Exception as exc:  # noqa: BLE001
                exceptions.append(f"{type(exc).__name__}: {exc}")
        print(f"  400 次调用：无效矩形 {bad_rects} 次，面积<=0 {zero_areas} 次，"
              f"异常 {len(exceptions)} 次")
        if exceptions:
            print(f"    首个异常: {exceptions[0]}")
        check("all_monitor_rects() 始终返回有效矩形", bad_rects == 0, f"{bad_rects}/400")
        check("_largest_monitor_area() 始终 > 0（0 会让所有矩形被判自我放大）",
              zero_areas == 0, f"{zero_areas}/400")
        check("调用不抛异常", not exceptions, f"{len(exceptions)}/400")

        # ---- §4 current perspective: is the window still where we left it? --
        print("\n§4 结束时窗口位置")
        final = ctl.get_window_rect()
        print(f"  {final}  pinned={ctl._geometry}")
    finally:
        ctl.stop()

    passed = sum(1 for _n, ok, _d in _results if ok)
    print("\n" + "=" * 74)
    print(f"{passed}/{len(_results)} 通过")
    for n, ok, _d in _results:
        if not ok:
            print(f"  未通过: {n}")
    print("=" * 74)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
