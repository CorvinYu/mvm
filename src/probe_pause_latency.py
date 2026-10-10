"""Probe: how long does the PLAYER take to report "Paused" over SMTC?

WHY THIS EXISTS (measured 2026-10-10, user report option **B**):
    "暂停后越 5-10 秒暂停" -- the user confirmed this means the PICTURE keeps
    moving for ~5-10 seconds after the music is paused, so the two are visibly
    out of sync for that stretch.

    `follow` freezes the picture only AFTER SMTC reports `Paused`
    (`follow.py`: "音乐已暂停 -> 保留窗口（冻结画面）"), and the daemon's poll adds
    its own interval on top. So the delay has two possible sources:

      (a) the PLAYER is slow to update its SMTC status -- nothing this project
          can fix by polling faster; it would need a different signal; or
      (b) our own poll/grace logic adds the delay.

    The live log cannot settle this: `follow_live.log` records when the daemon
    DETECTED the pause, not when the user pressed it, so no latency can be
    derived from it after the fact.

HOW THIS PROBE SEPARATES THEM
    It samples SMTC every 0.2s and timestamps two independent events:

      * when the reported POSITION stops advancing  (<= the real pause moment:
        the music cannot advance while paused), and
      * when the reported STATUS flips to Paused.

    The gap between those two is the PLAYER's own reporting lag, measured
    without needing to know exactly when the user pressed the key. The daemon's
    polling interval then sits ON TOP of that gap, so a large (a) means faster
    polling cannot help.

USAGE
    Start it, then press pause, wait ~10s, and press play again.
    Read the "延迟" lines at the end.

Run:  python src\\probe_pause_latency.py [seconds]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smtc  # noqa: E402


def sample_one():
    """(position, status, title) of the whitelisted session, or None."""
    sessions = smtc.read_sessions()
    wl = smtc.load_whitelist()
    best = None
    for s in sessions:
        if smtc.evaluate_session(s, wl).allowed:
            best = s
            break
    if best is None:
        return None
    return (best.position_sec, best.status, best.title)


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    print("=" * 74)
    print("持续采样 SMTC（每 0.2s）。**你随时按一次暂停即可**（等约 10 秒再播放）。")
    print("捕捉到一次完整的『Playing → Paused → Playing』后会自动结束；")
    print(f"否则最多等 {seconds:.0f}s。")
    print("=" * 74)

    t0 = time.monotonic()
    rows = []            # (elapsed, pos, status)
    last_pos = None
    last_pos_change_at = None
    last_status = None

    events = []          # human-readable transition log
    paused_at = None     # when SMTC first reported Paused
    pause_lag = None     # how long the position had been frozen before that
    while time.monotonic() - t0 < seconds:
        el = time.monotonic() - t0
        s = sample_one()
        if s is None:
            time.sleep(0.2)
            continue
        pos, status, title = s
        rows.append((el, pos, status))

        if last_pos is None or pos != last_pos:
            last_pos_change_at = el
            last_pos = pos

        if status != last_status:
            msg = (f"  [{el:6.2f}s] status: {last_status} -> {status}"
                   f"   (pos={pos:.2f})")
            events.append(msg)
            # Print IMMEDIATELY: the probe may run for minutes and the user
            # needs to see the transition land, otherwise there is no way to
            # tell whether the pause was even captured.
            print(msg, flush=True)
            low = status.lower()
            if low.startswith("paused"):
                paused_at = el
                # How long had the position already been frozen when SMTC
                # finally admitted the pause? That is the player's own lag.
                if last_pos_change_at is not None:
                    pause_lag = el - last_pos_change_at
                    detail = (f"           ↑ 位置已静止 {pause_lag:.2f}s 后才报 Paused"
                              f"  ← 播放器自身的报告延迟")
                    events.append(detail)
                    print(detail, flush=True)
            elif low.startswith("playing") and paused_at is not None:
                # Full cycle captured -> we have what we came for.
                done = (f"  [{el:6.2f}s] 已恢复播放；本曲暂停持续 "
                        f"{el - paused_at:.2f}s → 结束采样")
                events.append(done)
                print(done, flush=True)
                last_status = status
                break
            last_status = status

        time.sleep(0.2)

    print("\n--- 状态变化事件 ---")
    print("\n".join(events) if events else "  （没有捕捉到状态变化）")

    # Summarise every continuous "position frozen while status says Playing"
    # stretch -- that is the window in which the picture is stale.
    print("\n--- 位置停滞但状态仍为 Playing 的时段（画面与音乐已经不同步）---")
    stalls = []
    start = None
    for i in range(1, len(rows)):
        el, pos, status = rows[i]
        prev_el, prev_pos, prev_status = rows[i - 1]
        same = pos == prev_pos
        playing = str(status).lower().startswith("playing")
        if same and playing:
            if start is None:
                start = prev_el
        else:
            if start is not None:
                stalls.append((start, prev_el))
                start = None
    if start is not None and rows:
        stalls.append((start, rows[-1][0]))
    long_stalls = [(a, b) for a, b in stalls if (b - a) >= 1.0]
    for a, b in long_stalls:
        print(f"  {a:6.2f}s → {b:6.2f}s  共 {b - a:.2f}s（期间 SMTC 仍报 Playing）")
    if not long_stalls:
        print("  （未发现 ≥1s 的停滞）")

    print("\n--- 结论 ---")
    if pause_lag is None:
        print("  未捕捉到暂停事件（这轮你没按暂停，或播放器没更新状态）。")
        print("  请重跑，并在运行期间按一次暂停。")
    elif pause_lag >= 2.0:
        print(f"  ✗ 播放器自身的报告延迟为 **{pause_lag:.2f}s**（≥2s）。")
        print("  说明：音乐停了之后，播放器隔了这么久才把 SMTC 状态改成 Paused。")
        print("  → **加快我们的轮询无法解决**。可行的修法是换判据：")
        print("    把『位置连续 N 秒不推进』也视为暂停（position 停得比 status 早）。")
    else:
        print(f"  ✓ 播放器报告延迟仅 **{pause_lag:.2f}s**（<2s），不是瓶颈。")
        print("  说明：延迟出在我们自己的轮询/宽限逻辑上，可以在 follow.py 里修。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
