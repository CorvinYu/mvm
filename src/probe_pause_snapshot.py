"""Probe: what does SMTC ACTUALLY report while the music is paused?

WHY THIS EXISTS (measured 2026-10-10, user report option **B**):
    "暂停后越 5-10 秒暂停" -- the user confirmed this means the PICTURE keeps
    moving for ~5-10 seconds after the music is paused, so the two are visibly
    out of sync for that stretch. `follow` freezes the picture only after SMTC
    reports the pause (`follow.py`: "音乐已暂停 -> 保留窗口（冻结画面）").

WHY THE FIRST VERSION OF THIS PROBE FAILED (and what it taught us)
    It only printed on a STATUS CHANGE, and it captured nothing while the user
    reliably pressed pause. Two explanations were possible, and the difference
    matters:
      * the player REMOVES the session from SMTC on pause (T7's root-cause
        analysis found exactly this: "播放器暂停时常把曲目从 SMTC 列表摘掉"), so
        there is no Playing->Paused transition to observe at all; or
      * the session stays but its status text is something we do not match.
    A change-only probe cannot tell these apart, because "the session vanished"
    is not a change in any field it watched.

WHAT THIS VERSION DOES
    Prints ONE snapshot per second, unconditionally:
        [  12.4s] pos=  37.21 st=Playing  allowed=1 all=3
    plus a marker when the position stops advancing. So the raw behaviour around
    the pause is fully visible afterwards, and the question becomes answerable
    by reading the file instead of by guessing.

    It also reports how many SMTC sessions are visible in total, so a
    disappearing session shows up as `allowed=0` (or a jump in `all=`).

USAGE
    Start it, press pause once (wait ~10s), press play. Read the snapshots
    around the stall.

Run:  python src\\probe_pause_snapshot.py [seconds]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smtc  # noqa: E402


def snapshot():
    """(pos, status, title, n_allowed, n_all) for the best whitelisted session."""
    sessions = smtc.read_sessions()
    wl = smtc.load_whitelist()
    allowed = [s for s in sessions if smtc.evaluate_session(s, wl).allowed]
    playing = [s for s in allowed if s.status == "Playing"]
    pick = playing[0] if playing else (allowed[0] if allowed else None)
    if pick is None:
        return (None, None, None, len(allowed), len(sessions))
    return (pick.position_sec, pick.status, pick.title,
            len(allowed), len(sessions))


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    print("=" * 78)
    print("每秒一行快照。**请按一次暂停**（等约 10 秒再播放）。")
    print("`<<< 位置停滞` 标记出现时即为暂停生效点。")
    print(f"最多运行 {seconds:.0f}s。")
    print("=" * 78)
    print(f"{'时间':>8}  {'位置':>9}  {'状态':<9} {'白名单':>6} {'全部':>5}  备注")
    print("-" * 78, flush=True)

    t0 = time.monotonic()
    last_pos = None
    stall_start = None
    while time.monotonic() - t0 < seconds:
        el = time.monotonic() - t0
        pos, status, title, n_ok, n_all = snapshot()

        marks = []
        if pos is not None and last_pos is not None and pos == last_pos:
            if stall_start is None:
                stall_start = el
                marks.append("<<< 位置停滞开始")
            else:
                marks.append(f"<<< 位置停滞 {el - stall_start:.1f}s")
        elif stall_start is not None:
            marks.append(f">>> 位置恢复（停滞了 {el - stall_start:.1f}s）")
            stall_start = None
        if pos is not None:
            last_pos = pos

        pos_s = f"{pos:9.2f}" if pos is not None else "      n/a"
        st_s = status if status else "n/a"
        print(f"{el:7.1f}s  {pos_s}  {st_s:<9} {n_ok:>6} {n_all:>5}  "
              + " ".join(marks), flush=True)
        time.sleep(1.0)

    print("-" * 78)
    print("若『位置停滞』期间的『白名单』计数掉到 0，说明播放器把曲目从 SMTC")
    print("摘掉了（而不是报告 Paused）—— 那么靠轮询 status 永远会有延迟，")
    print("必须改用『位置连续 N 秒不推进』作为暂停判据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
