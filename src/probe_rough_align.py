"""Probe: how noisy is the SMTC position that coarse alignment samples?

WHY THIS EXISTS (measured 2026-10-10, while investigating the user report
"第一次切歌后直接从 0 开始播放，粗对齐没有生效。第二次切歌后生效"):

    The coarse-align ROOT CAUSE turned out to be elsewhere (a stale status file
    satisfying `_seek_after_load`'s confirmation -- see test_coarse_align.py).
    This probe measures a SEPARATE, real property of the same data path:

        NetEase Cloud Music (cloudmusic.exe) reports SMTC `position` quantised to
        ~1 second, and the follower samples it only 3 times at 1.5s intervals.
        Because the sampling phase interacts with that quantisation, adjacent
        samples imply local advance rates anywhere in ~0.76x..1.25x even though
        the track plays at 1.0x. Endpoint-to-endpoint rates therefore look sane
        (1.01x over 24s) while the individual samples are not a trustworthy
        "where is the music now" anchor at the second-or-better precision
        alignment needs.

    WHY IT MATTERS: `follow._probe_music_position()` decides reliability from the
    ENDPOINT rate over a 3-sample window (0.5..1.5 accepted). Over such a short
    window the endpoint rate cannot distinguish "playing normally" from
    "quantised/jumping", and the same position was observed being judged
    reliable for coarse alignment (1.12x) and then unreliable by the closed loop
    (-24.07x) within the same song.

    This is NOT the reported bug's cause, but it is a real weakness in the
    coarse-align anchor. Recorded so the next person does not re-derive it.

Read-only: it samples SMTC and never starts mpv or moves a window.

Run:  python src\\probe_rough_align.py [seconds]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smtc  # noqa: E402


def sample(seconds: float, gap: float = 1.5):
    """Collect (t, position) samples the way the follower does."""
    out = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        sessions = smtc.read_sessions()
        wl = smtc.load_whitelist()
        best = None
        for s in sessions:
            if smtc.evaluate_session(s, wl).allowed and s.status == "Playing":
                best = s
                break
        if best is None:
            for s in sessions:
                if smtc.evaluate_session(s, wl).allowed:
                    best = s
                    break
        out.append((time.monotonic(), None if best is None
                    else (best.position_sec, best.status, best.title)))
        time.sleep(gap)
    return out


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 21.0
    print(f"采样 {seconds:.0f}s，间隔 1.5s（与 _probe_music_position 一致）…\n")
    samples = sample(seconds)

    usable = [(t, v[0], v[1], v[2]) for t, v in samples if v is not None]
    if len(usable) < 2:
        print("✗ 没有可用的 SMTC 会话（先播放音乐，且 app 要在白名单里）")
        return 1

    print("逐样本（position / status / title）:")
    prev = None
    prev_t = None
    deltas = []
    for t, pos, status, title in usable:
        d = ""
        if prev is not None and (t - prev_t) > 0:
            d = f"   Δ={pos - prev:+.2f}s / {t - prev_t:.2f}s wall"
            deltas.append(((pos - prev) / (t - prev_t), pos - prev))
        print(f"  t={t - usable[0][0]:6.2f}s  pos={pos:8.2f}s  [{status}] {title}{d}")
        prev, prev_t = pos, t

    first_t, first_p = usable[0][0], usable[0][1]
    last_t, last_p = usable[-1][0], usable[-1][1]
    dt = last_t - first_t
    endpoint_rate = (last_p - first_p) / dt if dt > 0 else 0.0

    print("\n--- 判据 1：端点速率（follow 粗对齐现在用的算法）---")
    print(f"  ({last_p:.2f} - {first_p:.2f}) / {dt:.2f} = {endpoint_rate:.2f}x")
    gate = 0.5 <= endpoint_rate <= 1.5
    print(f"  0.5..1.5 门限 -> {'可靠（会被采纳为绝对锚点）' if gate else '不可靠（走回退）'}")

    print("\n--- 判据 2：逐样本局部速率（稳健性检查）---")
    uniform = True
    if deltas:
        rates = [r for r, _ in deltas]
        lo, hi = min(rates), max(rates)
        print("  局部速率: " + "  ".join(f"{r:.2f}x" for r in rates))
        print(f"  最小 {lo:.2f}x / 最大 {hi:.2f}x  → 波动 {hi - lo:.2f}x")
        uniform = all(0.85 <= r <= 1.15 for r in rates)
        print(f"  每个相邻对都 ≈1.0x ? {'是' if uniform else '否 —— 采样噪声/量化明显'}")

    print("\n--- 结论 ---")
    if gate and not uniform:
        print("  ⚠ 端点速率通过门限，但逐样本速率波动明显：")
        print("     该窗口的 position 精度不足以支撑秒级锚定。粗对齐据此算起点时，")
        print("     误差可达数秒（可由互相关纠正）。这不是本次『从 0 开始』的原因")
        print("     （那是陈旧状态文件的假阳性，见 test_coarse_align.py），")
        print("     而是采样窗口过短 + 播放器 position 量化的独立弱点。")
    elif gate:
        print("  端点速率与局部速率一致，本窗口内 position 质量良好。")
    else:
        print("  门限已正确拒绝该 position（会走 t2-t1 回退）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
