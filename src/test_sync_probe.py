"""test_sync_probe.py -- verify the MEASUREMENT tool itself, offline.

Why this exists
---------------
`sync_probe.py` is the instrument used to judge whether the A/V offset fix
works. An unverified instrument is worse than no instrument: it would let a
broken fix "pass" (or a correct one "fail") and nobody would know. So the
statistics that the verdict depends on are tested here against synthetic
series whose answers are known by construction.

Specifically this covers the three cases the tool claims to distinguish:
    * constant offset  -> stable mean, small spread, ~0 drift
    * clock drift      -> non-zero drift_per_min
    * noise            -> large spread, stable mean, ~0 drift
plus the reliability gate (a frozen music clock must be rejected, not averaged).

Run:  python test_sync_probe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_probe  # noqa: E402

_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    mark = "PASS" if ok else "FAIL"
    if ok:
        _passed += 1
    else:
        _failed += 1
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def make_series(offsets: list[float], start_pos: float = 100.0,
                gap: float = 2.0) -> list[dict]:
    """Build a synthetic sample series.

    The music clock advances at exactly 1x (the honest case) so the rate gate
    passes; `offsets` is applied on TOP of that, which is what the video clock
    sees. This mirrors reality: both clocks advance, the difference is the
    error being measured.
    """
    out = []
    for i, off in enumerate(offsets):
        t = 1000.0 + i * gap
        music = start_pos + i * gap
        out.append({
            "music": music,
            "video": music + off,
            "video_raw": music + off,
            "offset": off,
            "gap": 0.05,
            "t": t,
            "music_t": t,
            "music_pos": music,
        })
    return out


def test_constant_offset() -> None:
    print("\n=== 1. 固定偏移：均值稳定、漂移≈0 ===")
    st = sync_probe.summarise(make_series([0.4] * 10))
    check("均值正确", abs(st["mean"] - 0.4) < 1e-6, f"{st['mean']:+.3f}s")
    check("标准差为 0", st["stdev"] < 1e-6, f"{st['stdev']:.4f}")
    check("漂移≈0", abs(st["drift_per_min"]) < 1e-6, f"{st['drift_per_min']:+.4f} s/min")
    check("速率判据通过", st["rate_ok"], f"{st['music_rate']:.3f}x")
    check("平均绝对偏差 0.4s", abs(st["abs_mean"] - 0.4) < 1e-6)


def test_drift() -> None:
    print("\n=== 2. 时钟漂移：漂移率能被检出 ===")
    # +0.05s of error added per sample, samples 2s apart
    # => 0.05/2 s per s = 0.025 s/s = 1.5 s/min
    offs = [0.05 * i for i in range(11)]
    st = sync_probe.summarise(make_series(offs, gap=2.0))
    check("漂移约 +1.5 s/min", abs(st["drift_per_min"] - 1.5) < 0.05,
          f"{st['drift_per_min']:+.3f} s/min")
    check("全程移动约 0.5s", abs(st["drift_total"] - 0.5) < 0.02,
          f"{st['drift_total']:+.3f}s（斜率 1.5 s/min × 20s 窗口 = 0.5s）")
    check("漂移/噪声比为无穷（完美线性）",
          st["drift_ratio"] == float("inf"), f"{st['drift_ratio']}")
    check("判定为真漂移", st["drift_ratio"] == float("inf")
          and abs(st["drift_total"]) >= 0.4,
          "会触发「必须闭环校正」提示")


def test_noise() -> None:
    print("\n=== 3. 噪声：均值稳定、标准差大、不得误报漂移 ===")
    # WHY THIS CASE MATTERS (a real bug found by this very test): an earlier
    # version judged drift on the raw slope, and this alternating series has a
    # chance slope of about -1.07 s/min -- well past the 0.6 threshold -- so a
    # purely jittery, stationary signal was reported as drifting. The verdict
    # now uses drift_ratio (trend vs residual noise), which must stay small.
    offs = [0.6, -0.4, 0.8, -0.7, 0.5, -0.6, 0.7, -0.5, 0.4, -0.3]
    st = sync_probe.summarise(make_series(offs, gap=2.0))
    check("均值接近 0", abs(st["mean"]) < 0.2, f"{st['mean']:+.3f}s")
    check("标准差较大 (>0.5)", st["stdev"] > 0.5, f"{st['stdev']:.3f}")
    check("裸斜率确实很大（说明单看斜率会误判）",
          abs(st["drift_per_min"]) > 0.6, f"{st['drift_per_min']:+.3f} s/min")
    check("但漂移/噪声比小 -> 不判为漂移", st["drift_ratio"] <= 1.0,
          f"ratio={st['drift_ratio']}")
    check("全程移动量小于噪声带",
          abs(st["drift_total"]) <= st["stdev"] * 1.5,
          f"total={st['drift_total']:+.3f}s vs stdev={st['stdev']:.3f}s")


def test_rate_gate() -> None:
    print("\n=== 4. 可靠性闸门：冻结的音乐时钟必须被拒绝 ===")
    # Music clock stuck at one value (measured on 汽水音乐: 105.0s for 15s).
    frozen = []
    for i in range(6):
        t = 1000.0 + i * 2.0
        frozen.append({
            "music": 105.0, "video": 105.4, "video_raw": 105.4,
            "offset": 0.4, "gap": 0.05, "t": t,
            "music_t": t, "music_pos": 105.0,
        })
    st = sync_probe.summarise(frozen)
    check("速率判为 0", abs(st["music_rate"]) < 1e-9, f"{st['music_rate']:.3f}x")
    check("闸门拒绝（rate_ok=False）", not st["rate_ok"],
          "不会把冻结值当有效同步精度")

    # A wild jump must be rejected too (7x measured on 汽水音乐).
    jumpy = []
    for i, pos in enumerate([10.0, 40.0, 44.0, 50.0, 55.0, 60.0]):
        t = 1000.0 + i * 2.0
        jumpy.append({
            "music": pos, "video": pos + 0.3, "video_raw": pos + 0.3,
            "offset": 0.3, "gap": 0.05, "t": t,
            "music_t": t, "music_pos": pos,
        })
    st2 = sync_probe.summarise(jumpy)
    check("跳变速率被拒", not st2["rate_ok"], f"{st2['music_rate']:.3f}x")


def test_aging_math() -> None:
    print("\n=== 5. 读取延迟 + 状态陈旧补偿（工具的核心测量逻辑）===")
    # take_sample must age the VIDEO reading forward by:
    #   (a) the staleness of the status file when we read it, and
    #   (b) the time that elapsed between the video read and the music read.
    # Verified with stubbed readers so the arithmetic is checked without a live
    # player. (b) alone was the first implementation and it BIASED the offset --
    # see the assertion at the end of this test.
    def fake_video():
        # status file value 50.0, written 0.4s ago, read at t=100.0
        return (50.0, 100.0, 0.4)

    def fake_music():
        # music read 0.8s after the video read; music at 50.5
        return (50.5, 100.8)

    orig_v, orig_m = sync_probe.read_video_position, sync_probe.read_music_position
    sync_probe.read_video_position = fake_video
    sync_probe.read_music_position = fake_music
    try:
        s = sync_probe.take_sample([])
    finally:
        sync_probe.read_video_position, sync_probe.read_music_position = orig_v, orig_m

    check("拿到样本", s is not None)
    if s:
        check("间隙被正确测量 (0.8s)", abs(s["gap"] - 0.8) < 1e-6, f"{s['gap']:.3f}s")
        check("陈旧量被正确测量 (0.4s)", abs(s["staleness"] - 0.4) < 1e-6,
              f"{s['staleness']:.3f}s")
        check("画面外推到当前时刻 (50.0+0.4+0.8=51.2)",
              abs(s["video"] - 51.2) < 1e-6, f"{s['video']:.3f}s")
        check("偏差 = 51.2 - 50.5 = +0.7", abs(s["offset"] - 0.7) < 1e-6,
              f"{s['offset']:+.3f}s")
        check("原始值被保留供排查", abs(s["video_raw"] - 50.0) < 1e-6)

    # THE BIAS THIS PINS: without the staleness term the same reads report
    # 50.8 - 50.5 = +0.3s instead of +0.7s -- a 0.4s error, the same order as
    # the effect being measured. A real measurement run showed this as an
    # offset that appeared to grow steadily negative across samples.
    no_staleness = (50.0 + 0.8) - 50.5
    check("漏掉陈旧项会低估 0.4s（旧实现的偏差）",
          abs(no_staleness - 0.3) < 1e-6,
          f"漏项得 {no_staleness:+.3f}s，修正后 {50.0 + 0.4 + 0.8 - 50.5:+.3f}s")


def test_missing_data() -> None:
    print("\n=== 6. 缺数据不能被当成 0 偏差 ===")
    orig_v, orig_m = sync_probe.read_video_position, sync_probe.read_music_position
    try:
        sync_probe.read_video_position = lambda: None
        sync_probe.read_music_position = lambda: (10.0, 100.0)
        check("视频缺失 -> None（而非 0）", sync_probe.take_sample([]) is None)

        sync_probe.read_video_position = lambda: (10.0, 100.0, 0.2)
        sync_probe.read_music_position = lambda: None
        check("音乐缺失 -> None（而非 0）", sync_probe.take_sample([]) is None)
    finally:
        sync_probe.read_video_position, sync_probe.read_music_position = orig_v, orig_m


def main() -> int:
    print("sync_probe.py 测量逻辑自检（离线，不需要播放器）")
    test_constant_offset()
    test_drift()
    test_noise()
    test_rate_gate()
    test_aging_math()
    test_missing_data()
    print(f"\n===== {_passed} passed, {_failed} failed =====")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
