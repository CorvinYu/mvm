"""sync_probe.py -- measure the LIVE A/V offset, repeatedly, as a time series.

Why this exists
---------------
`align_probe.py` answers "does fine alignment work for this song" -- a single
shot, and it re-downloads audio to do it. That is the wrong tool for the
question this project kept failing to answer honestly:

    "the video stays 0.2-0.6s off; is that a constant offset, a drift, or
     noise -- and does my fix actually reduce it?"

Those three look identical in a single sample. Only a TIME SERIES distinguishes
them, so this tool samples the two clocks repeatedly and reports the statistics
that separate the cases:

  * constant offset -> mean is stable, spread is small
  * clock drift      -> mean MOVES monotonically across samples
  * noise            -> spread is large but mean is stable

It reads the same two sources the alignment code trusts:
  * music position: Windows SMTC (what the user's player reports)
  * video position: state/_mvm_status.txt, written by mvm_control.lua

WHY IT MEASURES LIKE THIS (the measurement is the hard part):

  1. SMTC's position is NOT reliably "now". Reading it costs a PowerShell
     launch (~1s, measured), and different players update it at different
     rates. So a sample is only usable if we pair it with mpv's position read
     at (as close as possible to) the SAME instant. We read mpv FIRST and SMTC
     SECOND, then correct for the SMTC read latency by advancing mpv's value
     with the wall time that elapsed between the two reads. Reading order
     matters: mvm_status.txt is refreshed by the Lua timer every 0.5s, so
     mpv's number is up to 0.5s STALE and must be aged forward too.

  2. A player whose position freezes (measured: 汽水音乐 held 105.0s for 15s
     then jumped to 158.3s) would make every sample meaningless. We therefore
     compute the advance rate across samples and REFUSE to report statistics
     for a run whose rate is outside 0.5..1.5x -- the same criterion the
     follower uses (follow.py `_probe_music_position`).

  3. mpv's time-pos is the position of the VIDEO timeline. A positive
     (video - music) means the picture is AHEAD of the sound.

Usage
-----
    python sync_probe.py                    # 10 samples, 2s apart
    python sync_probe.py --samples 15 --gap 2
    python sync_probe.py --json out.json    # also write the raw series

Exit code is 0 when the offset stayed within --tolerance, 1 otherwise, so a
regression test or a shell loop can act on it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from player import STATUS_FILE                                   # noqa: E402
from smtc import pick_session, read_sessions                     # noqa: E402

# The follower's own reliability window (follow.py): a player reporting a
# position that advances at ~1x wall-clock is usable as an absolute anchor.
# Reused rather than re-derived so this probe cannot disagree with the code it
# is measuring.
RATE_LO, RATE_HI = 0.5, 1.5

# Baseline from NOTES.md §1: the end-to-end sync error was measured at
# 0.2-0.6s, and "anything over 3s is abnormal". A fix is judged on whether it
# holds the offset near the LOW end of that band, so 1.0s is the default bar.
DEFAULT_TOLERANCE_SEC = 1.0


def read_video_position() -> tuple[float, float, float] | None:
    """(position_sec, monotonic_time_of_read, staleness_sec) for the video.

    STALENESS IS NOT OPTIONAL -- omitting it biased every earlier measurement.
    mvm_control.lua rewrites the status file every 0.5s, so the value we read
    describes the playhead as of the last rewrite, NOT as of the read. An
    earlier version aged the value by the time between our two reads (video,
    then music) and ignored this 0-0.5s, which systematically UNDER-estimated
    the video position and therefore reported an offset biased toward "video
    lags" by up to half a second. Since the numbers this tool prints are used to
    judge whether the sync fix works, that bias had to go.

    The file's mtime is when the Lua timer wrote it, so
    `now - mtime` is exactly the age of the value.
    """
    t = time.monotonic()
    try:
        stat = STATUS_FILE.stat()
        raw = STATUS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not raw or not raw[0].strip():
        return None
    try:
        position = float(raw[0].strip())
    except ValueError:
        return None
    staleness = max(0.0, time.time() - stat.st_mtime)
    return (position, t, staleness)


def read_music_position() -> tuple[float, float] | None:
    """(position_sec, monotonic_time_of_read) for the music, or None."""
    sessions = read_sessions()
    t = time.monotonic()
    s = pick_session(sessions)
    if s is None:
        return None
    return (float(s.position_sec), t)


def take_sample(already_seen: list[tuple[float, float]]) -> dict | None:
    """One (music, video) pair, corrected for BOTH read latency and staleness.

    ORDER IS LOAD-BEARING -- video first, then music:

      * The video value comes from a file the Lua timer refreshes every 0.5s,
        so it is BETWEEN 0 and 0.5s old. That age is measured from the file's
        mtime and added back (the video advanced by exactly that much since the
        value was written).
      * The music value from SMTC is "now-ish" but costs ~1s to obtain. Any
        wall time that passes between the two reads advances BOTH timelines; we
        apply that advance to the video too, because the music position is the
        reference taken at face value.

    Both corrections move the video FORWARD, and both are needed: skipping the
    mtime term (as an earlier version did) biased every reported offset toward
    "video lags" by up to 0.5s, which is the same order as the effect being
    measured. This is the same absolute-position reasoning the follower uses
    (NOTES §2, 铁律 23): measure each clock, then carry the known elapsed time
    forward rather than assuming the two reads were simultaneous.
    """
    video = read_video_position()
    if video is None:
        return None
    music = read_music_position()
    if music is None:
        return None

    video_pos, video_t, staleness = video
    music_pos, music_t = music

    # Time from the video read to the music read.
    gap = music_t - video_t
    # The video value was already `staleness` old when we read it, and has
    # advanced by `gap` more since.
    video_now = video_pos + staleness + gap
    offset = video_now - music_pos

    return {
        "music": round(music_pos, 3),
        "video": round(video_now, 3),
        "video_raw": round(video_pos, 3),
        "staleness": round(staleness, 3),
        "offset": round(offset, 3),
        "gap": round(gap, 3),
        "t": round(music_t, 3),
        "music_t": music_t,
        "music_pos": music_pos,
    }


def summarise(samples: list[dict]) -> dict:
    """Statistics that distinguish a constant offset from a drift."""
    offs = [s["offset"] for s in samples]
    mean = statistics.fmean(offs)
    out = {
        "n": len(offs),
        "mean": round(mean, 3),
        "min": round(min(offs), 3),
        "max": round(max(offs), 3),
        # stdev needs >=2 points; a single sample is reported as 0 spread.
        "stdev": round(statistics.stdev(offs), 3) if len(offs) > 1 else 0.0,
        "abs_mean": round(abs(mean), 3),
        "abs_max": round(max(abs(o) for o in offs), 3),
    }

    # Advance rate of the MUSIC clock across the run. This is the honesty check:
    # with a frozen SMTC position every offset above is meaningless, and it is
    # better to say so than to print a confident-looking number.
    if len(samples) > 1:
        dt = samples[-1]["music_t"] - samples[0]["music_t"]
        dp = samples[-1]["music_pos"] - samples[0]["music_pos"]
        out["music_rate"] = round(dp / dt, 3) if dt > 0 else 0.0
    else:
        out["music_rate"] = 0.0
    out["rate_ok"] = RATE_LO <= out["music_rate"] <= RATE_HI

    # Drift: the slope of offset vs time, in seconds per minute. A large value
    # means the two clocks diverge and no one-shot seek can fix it -- the case
    # that motivated closed-loop correction (TODO.md B2).
    #
    # WHY THE SLOPE ALONE IS NOT ENOUGH (measured while testing this tool):
    # alternating noise around a stable mean produced a slope of -1.07 s/min --
    # larger than the 0.6 threshold -- purely by chance. Judging "do we need
    # closed loop?" on the raw slope would therefore cry drift on a jittery but
    # stationary signal. What actually matters is whether the trend is LARGE
    # COMPARED WITH THE NOISE, so the decision is based on how far the fitted
    # line travels across the run relative to the residual spread
    # (`drift_ratio`), and the raw slope is only reported for information.
    out["drift_per_min"] = 0.0
    out["drift_total"] = 0.0
    out["drift_ratio"] = 0.0
    if len(samples) > 2:
        xs = [s["t"] - samples[0]["t"] for s in samples]
        ys = [s["offset"] for s in samples]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom > 0:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
            intercept = my - slope * mx
            # Residual spread around the fitted line: how noisy the signal is
            # once the trend is removed.
            resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
            resid_sd = statistics.stdev(resid) if len(resid) > 1 else 0.0
            # Total movement of the fitted line over the observation window.
            span = max(xs) - min(xs)
            out["drift_per_min"] = round(slope * 60.0, 3)
            out["drift_total"] = round(slope * span, 3)
            # Ratio > 1 means the trend moved the offset by more than the noise
            # band did -- i.e. a real drift rather than jitter. 0 residual
            # (perfectly linear) counts as definitely drifting.
            if resid_sd > 1e-6:
                out["drift_ratio"] = round(abs(out["drift_total"]) / resid_sd, 2)
            else:
                out["drift_ratio"] = float("inf") if abs(slope) > 1e-9 else 0.0

    return out


def main() -> int:
    p = argparse.ArgumentParser(
        prog="sync_probe.py",
        description="连续采样音画偏差，判断是固定偏移、漂移，还是噪声",
    )
    p.add_argument("--samples", type=int, default=10, help="采样次数（默认 10）")
    p.add_argument("--gap", type=float, default=2.0, help="采样间隔秒（默认 2）")
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_SEC,
                   help=f"判定合格的平均绝对偏差（默认 {DEFAULT_TOLERANCE_SEC}s）")
    p.add_argument("--json", default="", help="把原始序列写到该 json 文件")
    args = p.parse_args()

    print(f"采样 {args.samples} 次，间隔 {args.gap}s …")
    print("（偏差 = 画面 − 音乐；正值=画面超前）\n")

    samples: list[dict] = []
    for i in range(args.samples):
        s = take_sample(samples)
        if s is None:
            # Report WHY rather than silently producing fewer samples. The two
            # causes are completely different problems:
            #   * no video position -> the video never loaded. An empty first
            #     line in _mvm_status.txt is THE signature of the comma-mangled
            #     HTTP header bug (NOTES §3.2 判据陷阱 1) -- it must not be
            #     reported as "not aligned".
            #   * no music position -> nothing followable is playing (paused, or
            #     the player is outside the whitelist).
            # Determining which requires probing both, and each SMTC read costs
            # ~1s, so this only runs on the failure path.
            video_ok = read_video_position() is not None
            music_ok = read_music_position() is not None
            if not video_ok and not music_ok:
                print(f"  {i+1:2d}. 无数据：既没有视频位置，也没有可跟随的 SMTC 会话")
            elif not video_ok:
                print(f"  {i+1:2d}. 无数据：视频未加载"
                      f"（state/_mvm_status.txt 第一行为空 → 先查 header 逗号问题）")
            else:
                print(f"  {i+1:2d}. 无数据：没有可跟随的 SMTC 会话"
                      f"（音乐暂停了，或播放器不在白名单内）")
        else:
            samples.append(s)
            flag = "超前" if s["offset"] > 0 else "滞后"
            print(f"  {i+1:2d}. 音乐 {s['music']:8.3f}s  画面 {s['video']:8.3f}s  "
                  f"偏差 {s['offset']:+6.3f}s ({flag})  "
                  f"[读取 {s['gap']:+.2f}s, 状态陈旧 {s['staleness']:.2f}s]")
        if i < args.samples - 1:
            time.sleep(max(0.0, args.gap - 0.0))

    if not samples:
        print("\n没有任何有效样本，无法给出结论。")
        return 1

    st = summarise(samples)
    print("\n—— 统计 ——")
    print(f"  样本数      {st['n']}")
    print(f"  平均偏差    {st['mean']:+.3f}s")
    print(f"  偏差范围    {st['min']:+.3f} .. {st['max']:+.3f}s")
    print(f"  标准差      {st['stdev']:.3f}s")
    print(f"  最大绝对偏差 {st['abs_max']:.3f}s")
    print(f"  音乐位置推进速率 {st['music_rate']:.3f}x "
          f"({'可靠' if st['rate_ok'] else '不可靠 → 上面的数字不可信'})")
    print(f"  漂移        {st['drift_per_min']:+.3f} s/min"
          f"（全程 {st['drift_total']:+.3f}s，"
          f"漂移/噪声比 {st['drift_ratio']}）")

    if not st["rate_ok"]:
        print("\n⚠ SMTC 位置推进速率不在 0.5–1.5x 内，本次采样不可作为同步精度依据。")
        print("  （汽水音乐实测会把 position 冻结在单一值 15s 以上，见 NOTES §3.2）")
        if args.json:
            Path(args.json).write_text(
                json.dumps({"samples": samples, "summary": st},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    verdict_ok = st["abs_mean"] <= args.tolerance
    print(f"\n结论：平均绝对偏差 {st['abs_mean']:.3f}s "
          f"{'在' if verdict_ok else '超出'}容差 {args.tolerance}s → "
          f"{'合格' if verdict_ok else '不合格'}")

    # Interpretation hints, so the numbers are actionable rather than just a
    # pass/fail. These are the three cases the tool exists to separate.
    #
    # The drift test uses drift_ratio (trend size vs residual noise), NOT the
    # raw slope: a jittery-but-stationary signal can produce a big slope by
    # chance (measured -1.07 s/min from alternating noise), and calling that
    # "drift" would send the user chasing a problem that is really jitter.
    drifting = (st["drift_ratio"] == float("inf") or st["drift_ratio"] > 1.0)
    # 0.4s of total movement over the run: below the ~0.2-0.6s band the project
    # treats as normal, so a trend that small is not worth chasing even if it
    # is statistically clean.
    if drifting and abs(st["drift_total"]) >= 0.4:
        print(f"  · 存在明显漂移（全程移动 {st['drift_total']:+.3f}s，"
              f"大于噪声带）→ 一次性 seek 无法解决，必须闭环持续校正")
    elif st["stdev"] > 0.5:
        print("  · 偏差波动大而均值稳定 → 更可能是测量噪声/位置上报抖动")
    else:
        print("  · 偏差基本恒定 → 属于固定偏移，校准后可继承到其他歌曲")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"samples": samples, "summary": st},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  原始序列已写入 {args.json}")

    return 0 if verdict_ok else 1


if __name__ == "__main__":
    sys.exit(main())
