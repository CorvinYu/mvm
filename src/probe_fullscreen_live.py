"""Probe: does fullscreen REALLY survive the guard on a live mpv? (issue #2)

The unit criteria (test_window_guard.py) drive the guard with a fake controller,
which proves the DECISION LOGIC but not the wiring. This probe closes that gap
by exercising the real path end to end:

    real mpv  ->  mvm_control.lua writes lines 6-7  ->  read_window_mode()
              ->  _geometry_guard skips              ->  window stays fullscreen

Steps:
  1. start the project's isolated mpv through MpvController (starts the guard)
  2. send `set fullscreen yes` over the existing command-file channel
  3. assert the status file reports fullscreen=yes AND read_window_mode agrees
     (if only the first holds, the Lua/Python contract is broken)
  4. wait several guard ticks and assert the window is STILL fullscreen
     -- on the old code the guard yanked it back within ~0.5s
  5. `set fullscreen no` and assert the mode clears

Run:  python src/probe_fullscreen_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import player  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def status_line(n: int) -> str:
    try:
        lines = player.STATUS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "<no status file>"
    return lines[n - 1].strip() if len(lines) >= n else "<missing>"


def main() -> int:
    ctl = player.MpvController(mute=True)
    try:
        if not ctl.start():
            print("FAIL: mpv 未启动（前置条件不满足）")
            return 1
        print(f"  mpv pid={ctl.pid}, pinned={ctl._geometry}")

        # WAIT FOR A LAID-OUT WINDOW before taking the baseline.
        #
        # WHY (measured 2026-10-08): reading the rect the instant start() returns
        # can yield an initialising artefact -- observed (208,208,136,100). Using
        # that as "the normal window" made the exit-fullscreen assertion compare
        # against a bogus size and fail, and because it is a TIMING artefact the
        # probe passed or failed at random. The product itself has the same
        # guard for the same reason (`MIN_WINDOW_SIZE_PX = (320, 240)`), so the
        # probe uses that threshold too. The baseline is the controller's own
        # `_geometry` -- what it believes the pinned rect to be -- not a fresh
        # measurement.
        deadline = time.time() + 25
        normal = None
        while time.time() < deadline:
            r = ctl.get_window_rect()
            if r and r[2] >= 320 and r[3] >= 240:
                normal = r
                break
            time.sleep(0.5)
        if normal is None:
            check("窗口完成布局（拿到可信基线）", False,
                  f"rect={ctl.get_window_rect()}")
            return 1
        check("窗口完成布局（拿到可信基线）", True, f"normal={normal}")
        pinned_before = ctl._geometry
        print(f"  基线：实测 {normal}，控制器 _geometry {pinned_before}")

        # --- enter fullscreen over the real command channel -----------------
        print("\n§1 通过真实命令通道进入全屏")
        if not ctl.command("set fullscreen yes"):
            check("命令通道发送 `set fullscreen yes`", False, "command() 返回 False")
            return 1
        time.sleep(2.0)

        check("状态文件第 6 行报告 fullscreen=yes（lua 契约）",
              status_line(6).lower() == "yes", f"line6={status_line(6)!r}")
        fs, maxed = player.read_window_mode()
        check("read_window_mode() 读到 (True, False)（Python 契约）",
              (fs, maxed) == (True, False), f"{fs, maxed}")

        # --- the guard must NOT undo it ------------------------------------
        print("\n§2 等待守卫多个周期（旧码会在此拉回窗口）")
        rects = []
        for _ in range(8):                      # ~4s at the 0.5s cadence
            time.sleep(0.5)
            r = ctl.get_window_rect()
            if r:
                rects.append(r)
        wx, wy, ww, wh = player.work_area()
        last = rects[-1] if rects else None
        grew = last is not None and last[2] >= ww * 0.9 and last[3] >= wh * 0.85
        check("全屏后窗口仍覆盖屏幕（守卫没有拉回）", grew,
              f"last={last} work_area={ww}x{wh}")
        check("全屏矩形没有被写进 self._geometry",
              ctl._geometry == pinned_before,
              f"pinned={ctl._geometry} pinned_before={pinned_before}")

        # --- leaving fullscreen restores the pinned rect -------------------
        print("\n§3 退出全屏应恢复到全屏前的尺寸")
        ctl.command("set fullscreen no")
        time.sleep(2.0)
        fs2, maxed2 = player.read_window_mode()
        check("退出全屏后 read_window_mode() 回到 (False, False)",
              (fs2, maxed2) == (False, False), f"{fs2, maxed2}")
        rest = ctl.get_window_rect()
        check("窗口恢复到全屏前的尺寸附近",
              rest is not None and abs(rest[2] - pinned_before[2]) <= 40
              and abs(rest[3] - pinned_before[3]) <= 40,
              f"restored={rest} pinned_before={pinned_before}")

    finally:
        try:
            ctl.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"  (stop 失败: {exc})")

    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"全屏实测: {passed}/{len(results)} 通过")
    if passed < len(results):
        for ok, name, detail in results:
            if not ok:
                print(f"  FAIL: {name}  {detail}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
