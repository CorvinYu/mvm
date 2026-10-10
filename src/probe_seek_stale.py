"""Live probe: coarse seek against a REAL mpv, including the stale-value case.

WHY THIS EXISTS (measured 2026-10-10, user report
"第一次切歌后直接从 0 开始播放，粗对齐没有生效。第二次切歌后生效"):

    `loadfile` does NOT reset the status file, so for a moment after a song
    switch it still reports the PREVIOUS media's playhead. The offline criterion
    (src\\test_coarse_align.py) proves the confirmation logic no longer trusts
    that value; THIS probe proves the same thing end-to-end against a real mpv,
    over the real command-file channel, using the real status file.

THE SCENARIO, staged deliberately:
    1. Play the demo media and seek to ~90s. The status file now reports a high
       playhead -- this is the "stale value" a song switch would leave behind.
    2. Immediately request play_url(start_sec=5.0) on the SAME media (the URL is
       unchanged, so only the playhead RESET can betray that a new load began --
       the trickiest variant for the detector).
    3. Assert that play_url's verification reports the truth AND that the real
       playhead ends up near the requested 5s.

    Before the fix, step 2's verification was satisfied instantly by the stale
    90s value (`90 >= 5 - 2`), so `play_url` claimed success while the picture
    was still at the start of the file.

Runs the ISOLATED mpv copy (bin/mpv-iso) through the production MpvController --
never mpv.net (铁律 1). Requires state/_demo_pv.mp4 (120s).

Run:  python src\\probe_seek_stale.py
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


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def main() -> int:
    if not DEMO.exists():
        print(f"✗ 缺少测试媒体: {DEMO}")
        return 1
    print(f"媒体: {DEMO}（120s）")
    print("=" * 72)

    ctrl = P.MpvController(log=True)
    try:
        # ---- step 1: park the playhead high, creating the stale value --------
        print("\n§1 起播并 seek 到 90s（制造『陈旧高位值』）")
        if not ctrl.play_url(str(DEMO), start_sec=90.0):
            print("✗ play_url 失败")
            return 1
        time.sleep(2.5)
        pos, mtime, path = ctrl.read_status_full()
        print(f"  状态文件: pos={pos} path={P.status_path_basename(path)} "
              f"age={time.time() - mtime:.2f}s")
        check("状态文件停在高位（陈旧值已建立）",
              pos is not None and pos > 60.0, f"pos={pos}")
        check("play_url 确认了这次 seek", ctrl.last_seek_verified is True,
              f"last_seek_verified={ctrl.last_seek_verified}")

        # ---- step 2: request a LOW target on the SAME url --------------------
        print("\n§2 同一 URL 请求低目标 5.0s（陈旧值 90s 远大于 5s）")
        print(f"  旧判据会把 {pos:.1f}s 当作『已到达 5.0s』的证据并立即返回成功")
        t0 = time.monotonic()
        ok = ctrl.play_url(str(DEMO), start_sec=5.0)
        dt = time.monotonic() - t0
        verdict = ctrl.last_seek_verified
        print(f"  play_url 返回 {ok}（耗时 {dt:.2f}s）"
              f" last_seek_verified={verdict}")
        check("verify 判为成功（真实生效）", verdict is True,
              f"last_seek_verified={verdict}")

        # ---- step 3: the real playhead must actually be near 5s --------------
        print("\n§3 真机落点（play_url 返回后立刻读，避免把自然播放算成偏差）")
        pos2, _mt2, _p2 = ctrl.read_status_full()
        print(f"  状态文件: pos={pos2}")
        if pos2 is None:
            check("能读到落点", False, "pos=None")
        else:
            drift = pos2 - 5.0
            print(f"  落点 {pos2:.2f}s vs 请求 5.0s → 偏差 {drift:+.2f}s")
            check("真实落点在请求目标附近（±2.5s）", abs(drift) <= 2.5,
                  f"偏差 {drift:+.2f}s")
            # The decisive contrast with the old behaviour: if the stale 90s
            # value had been accepted as proof, the playhead would NOT be near 5.
            check("落点远离陈旧值 90s（证明不是被陈旧值糊弄过去）",
                  abs(pos2 - 90.0) > 60.0, f"pos={pos2:.1f}s")
    finally:
        ctrl.stop()

    passed = sum(1 for _n, ok_, _d in _results if ok_)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(_results)} 通过")
    failed = [n for n, ok_, _d in _results if not ok_]
    for n in failed:
        print(f"  失败: {n}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
