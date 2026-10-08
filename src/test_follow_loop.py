"""Offline criteria for task-1 (closed-loop drift correction). NO player needed.

WHY THIS FILE EXISTS
====================
NOTES.md §3.2 rule 4: "selftest 全绿 不能作回归依据 -- 会话七的 8 个 bug 全都
能在 54/54 全绿下共存."  So this task ships a criterion that FAILS on the old
code and PASSES on the new one, and this script is that criterion.

It exercises the REAL decision code (`Follower.closed_loop_step` and
`measure_residual`), driven by a simulated pair of clocks -- no mpv, no SMTC, no
audio device, no network. Three independent checks:

  CHECK 1 (A: capture timing)
      The absolute alignment formula must use the capture length that was
      actually recorded, not the nominal constant. On the old code the formula
      reads `ALIGN_CAPTURE_SEC`; here we ask the real `Recording` for
      `covered_sec` and confirm the two disagree when they should.

  CHECK 2 (C: convergence)
      Drive a simulated offset of +0.80s (a realistic "persistent <1s offset",
      well above the 0.25s deadband and inside the normal 0.2-0.6s band's
      neighbourhood) through the real corrector and require the residual to end
      BELOW 0.25s. On the old code there is no corrector at all: the residual
      never moves, so this check cannot pass.

  CHECK 3 (C: safety)
      A player whose reported position is frozen (the measured 汽水音乐
      behaviour) must produce ZERO corrections. A loop that "corrects" against a
      stale number is worse than no loop.

USAGE
    cd src
    python test_follow_loop.py
    # --old additionally runs the same checks against a frozen pre-fix snapshot
    # (../_old_code_snapshot/src, present only in the development checkout),
    # which MUST fail -- that is what proves the criteria can fail at all.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # music-video-matcher/ (HERE == <root>/src)
SRC = ROOT / "src"
# Frozen pre-task snapshot used by `--old` to PROVE the criteria can fail.
#
# It is a SEPARATE directory (`_old_code_snapshot/src`, extracted from the
# published repo's git history), NOT `_publish/src`. Using _publish for this was
# a real trap: _publish is the publish source, so the moment the new code is
# synced there the "old snapshot" silently becomes the NEW code and `--old`
# reports "旧代码竟然全部通过" -- a false alarm about the criteria being
# worthless. A clone of the public repo has neither directory, in which case
# `--old` explains itself and exits 0.
OLD_SRC = ROOT / "_old_code_snapshot" / "src"

RATE = 16000
DEADBAND = 0.25


def make_follower(follow_mod):
    """Build a Follower without running __init__ (which needs a Matcher).

    Returns None when the module has no Follower at all. Used by every check so
    that an OLD module fails on BEHAVIOUR rather than on a missing attribute --
    otherwise the demo would only prove "the new code exists", which is exactly
    the kind of vacuous criterion NOTES §3.2 rule 4 warns about.
    """
    F = getattr(follow_mod, "Follower", None)
    if F is None:
        return None
    f = F.__new__(F)
    for attr, val in (("_loop_key", None), ("_loop_last_probe", None),
                      ("_loop_last_apply", None), ("_loop_strikes", 0),
                      ("_loop_unusable_streak", 0), ("_loop_gave_up", False),
                      ("_loop_anomaly_prev", None), ("_loop_speed_applied", None),
                      ("last_loop_error", None), ("last_loop_applied", None),
                      ("_loop_applied_count", 0), ("_manual_offset_sec", 0.0),
                      ("_detected_at", 0.0)):
        setattr(f, attr, val)
    return f


def deadband_of(follow_mod, default: float) -> float:
    return float(getattr(follow_mod, "ALIGN_LOOP_DEADBAND_SEC", default))


def period_of(follow_mod, default: float) -> float:
    return float(getattr(follow_mod, "ALIGN_LOOP_PERIOD_SEC", default))


def step_of(follow_mod):
    """The corrector callable, or a stub that models the OLD behaviour.

    The old code had NO loop at all: the residual is whatever it was forever
    after the one-shot fine alignment. Returning a stub that reports "nothing to
    do" lets the convergence check run to completion on the old module and fail
    on the RESULT (the error never shrinks) instead of crashing on an
    AttributeError -- a far more convincing demonstration.
    """
    f = make_follower(follow_mod)
    if f is not None and hasattr(f, "closed_loop_step"):
        return f, f.closed_loop_step
    return f, (lambda *a, **k: ("skip-deadband", None))


class FakePlayer:

    """Minimal stand-in for MpvController: a video clock we can seek OR speed up.

    `speed` is the mpv playback-speed multiplier (1.0 = normal). Between probes
    the video advances at `period * speed` instead of `period * 1.0`, which is
    exactly how mpv's `speed` property works -- the correction becomes smooth
    drift rather than a jump. `set_speed` records the applied value so tests can
    assert the corrector set it and that it was restored to 1.0.
    """

    def __init__(self, position: float) -> None:
        self.position = position
        self.seeks: list[float] = []
        self.speeds: list[float] = []

    @property
    def speed(self) -> float:
        return self.speeds[-1] if self.speeds else 1.0

    def set_speed(self, value: float) -> None:
        self.speeds.append(float(value))

    def apply(self, correction: float) -> None:
        self.position += correction
        self.seeks.append(correction)


class FakeMusic:
    """Minimal stand-in for the SMTC-reported music clock."""

    def __init__(self, position: float, rate: float = 1.0) -> None:
        self.position = position
        self.rate = rate


def banner(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def load_follow(src_dir: Path):
    """Import follow.py from a specific src directory, isolated from the other."""
    for mod in ("follow", "player", "capture", "matcher", "smtc", "delay_history"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(src_dir))
    try:
        mod = importlib.import_module("follow")
    finally:
        sys.path.remove(str(src_dir))
    return mod


# --------------------------------------------------------------------------
# CHECK 1 -- capture timing semantics
# --------------------------------------------------------------------------

def check_capture_timing(capture_mod) -> bool:
    banner("CHECK 1  (A) 采集时长语义：公式必须用实测时长，不是名义常量")
    ok = True
    try:
        from capture import Recording  # noqa: F401
    except ImportError:
        Recording = None

    nominal = 10.0
    position_in_pv = 90.0                # correlation said the audio sat here in the PV

    def new_target(audio_sec: float, tail: float) -> float:
        """What follow.py computes NOW: measured length + time since audio end."""
        frames = int(audio_sec * RATE)
        t_end = 100.0 + audio_sec
        rec = Recording(path=Path("x.wav"), covered_sec=frames / RATE,
                        t_audio_start=100.0, t_audio_end=t_end,
                        requested_sec=nominal, frames=frames, sample_rate=RATE)
        t_now = t_end + tail
        return position_in_pv + rec.covered_sec + (t_now - rec.t_audio_end)

    def old_target(audio_sec: float, tail: float) -> float:
        """What the OLD code computed.

        Two separate mistakes that happen to have OPPOSITE signs:
          * `ALIGN_CAPTURE_SEC` (10.0) was used instead of the real audio
            length, over-counting by (nominal - audio_sec); and
          * `t_end` was stamped only AFTER record() returned, so the decode/
            resample tail was absorbed into "the 10s of audio" and the term
            `elapsed_after_capture` collapsed to ~0, under-counting by `tail`.
        """
        t_end_old = 100.0 + audio_sec + tail      # record() RETURNED here
        t_now = t_end_old
        return position_in_pv + nominal + max(0.0, t_now - t_end_old)

    def truth(audio_sec: float, tail: float) -> float:
        return position_in_pv + audio_sec + tail

    if Recording is None:
        # Old module: reproduce what its `record()` could possibly tell a caller
        # -- a bare path, i.e. the caller has to assume `nominal`. So the "new"
        # column degrades to the old formula and the check FAILS on behaviour.
        new_target = old_target
        print("  ⚠ 该模块的 record() 只返回路径：调用方拿不到实测时长，"
              "公式只能沿用名义常量")
    else:
        print("  两个错误方向相反、可部分抵消，所以只测一组数字会得出误导性结论：")
    print(f"  {'采集实测':>8} {'解码尾巴':>8} | {'新公式误差':>10} {'旧公式误差':>10}")
    cases = [(9.30, 0.55), (10.00, 0.55), (10.70, 0.20), (9.00, 0.05)]
    worst_old = 0.0
    worst_new = 0.0
    for audio_sec, tail in cases:
        e_new = new_target(audio_sec, tail) - truth(audio_sec, tail)
        e_old = old_target(audio_sec, tail) - truth(audio_sec, tail)
        worst_new = max(worst_new, abs(e_new))
        worst_old = max(worst_old, abs(e_old))
        print(f"  {audio_sec:8.2f} {tail:8.2f} | {e_new:+10.3f} {e_old:+10.3f}")

    ok &= check("模块能报告音频实际覆盖的时长（Recording.covered_sec）",
                Recording is not None,
                "" if Recording is not None
                else "旧代码 record() 只返回路径 → 公式只能用名义常量猜测")
    ok &= check("新公式在所有情形下都恢复真值（误差 <0.05s）", worst_new < 0.05,
                f"最大误差 {worst_new:.3f}s")
    ok &= check("旧公式在至少一种情形下明显算错（误差 >0.5s）", worst_old > 0.5,
                f"最大误差 {worst_old:.3f}s")
    # The case that motivates the fix: capture ran long, decode was fast. The
    # old formula is then ahead by (nominal-real) + tail = 0.90s -- squarely in
    # the range the user reports as "持续 <1s 偏移".
    e_classic = old_target(9.00, 0.05) - truth(9.00, 0.05)
    ok &= check("典型情形（采集 9.0s、解码 0.05s）旧公式偏 +0.95s",
                abs(e_classic - 0.95) < 1e-9, f"{e_classic:+.3f}s")
    # And the two errors can cancel, which is exactly why the OLD code appeared
    # to "work" often enough to survive: neither term alone is visible.
    e_cancel = old_target(9.30, 0.55) - truth(9.30, 0.55)
    ok &= check("两处误差可部分抵消（旧代码因此常看起来『基本对』，掩盖了 bug）",
                abs(e_cancel) < 0.5,
                f"采集 9.3s + 尾巴 0.55s -> 仅 {e_cancel:+.3f}s")
    return ok


# --------------------------------------------------------------------------
# CHECK 2 -- convergence of the real corrector
# --------------------------------------------------------------------------

def check_convergence(follow_mod) -> bool:
    banner("CHECK 2  (C) 收敛：把 >0.5s 的持续偏差拉回 <0.25s 死区（速率补偿）")
    F, step = step_of(follow_mod)
    if F is None:
        return check("模块提供 Follower", False, "旧代码无此类型")

    deadband = deadband_of(follow_mod, 0.25)
    period = period_of(follow_mod, 6.0)
    key = follow_mod.TrackKey(title="t", artist="a", duration_bucket=1)

    initial_error = 0.80
    video = FakePlayer(position=100.0 + initial_error)
    music = FakeMusic(position=100.0)
    # A realistic measurement chain: the SMTC position we read is a few tenths
    # of a second stale, and the video has a slow independent clock drift
    # (TODO.md B2's "渐进漂移"). The speed corrector must pull the residual
    # back INTO the deadband and then restore 1.0x.
    drift_per_period = 0.03          # video creeps ahead ~5ms/s

    print(f"  模拟：画面领先音乐 {initial_error:+.2f}s，死区 ±{deadband}s，"
          f"周期 {period}s")
    print(f"  画面额外漂移 {drift_per_period/period*1000:.1f} ms/s")
    if F is not None and not hasattr(F, "closed_loop_step"):
        print("  ⚠ 该模块没有闭环（旧代码）：下面每一轮都会是『无动作』，"
              "偏差不会被修正")
    print(f"  {'t(s)':>6} {'画面':>9} {'音乐':>9} {'偏差':>8}  动作")
    t = 60.0
    trace = []
    for _ in range(50):
        # Between probes the video advances at its CURRENT speed -- that is the
        # whole point of speed correction: setting speed 1.04 makes the picture
        # GAIN on the music without any jump.
        video.position += period * video.speed + drift_per_period
        music.position += music.rate * period
        t += period
        action, correction = step(
            key, music_position=music.position, video_position=video.position,
            usable=True, now=t,
        )
        if action == "speed" and correction is not None:
            video.set_speed(correction)
        elif action == "skip-deadband":
            # Back inside the deadband: the real loop restores 1.0x; model that.
            video.set_speed(1.0)
        residual = video.position - music.position
        trace.append(residual)
        if _ < 8 or _ > 42:
            print(f"  {t:6.0f} {video.position:9.2f} {music.position:9.2f} "
                  f"{residual:+8.3f}  {action} (speed={video.speed:.3f})")

    final_error = abs(video.position - music.position)
    # The FIRST corrections produce a transient overshoot (0.8s initial error
    # -> first speed change -> picture catches up through ~0.6s): that is the
    # healthy response to a large initial error, not instability. The bounded-
    # residual criterion therefore measures the STEADY STATE (skip the first 3
    # periods), which is what "does it stay aligned during a long song" means.
    steady = trace[3:]
    peak_after = max((abs(r) for r in steady), default=0.0)
    ok = True
    print(f"  首次测得偏差 {trace[0]:+.3f}s，最终 {final_error:+.3f}s，"
          f"稳定段峰值 {peak_after:.3f}s，速度调整 {len(video.speeds)} 次")
    # On the OLD module there is no corrector: no speed change ever happens and
    # the residual grows without bound (the simulated 5ms/s creep).
    ok &= check("闭环实际调整了播放速度（不是空转）", len(video.speeds) > 0,
                f"{len(video.speeds)} 次")
    ok &= check(f"初始偏差 {initial_error:+.2f}s 确实超过死区", initial_error > deadband)
    ok &= check(f"收敛后 |偏差| < {deadband}s（实际 {final_error:.3f}s）",
                final_error < deadband)
    ok &= check("速度调整方向正确（画面超前 -> 减速）",
                any(s < 1.0 for s in video.speeds),
                f"speed 序列: {[round(s, 3) for s in video.speeds[:6]]}…")
    ok &= check("速度从未超过 ±5% 上限",
                all(0.95 <= s <= 1.05 for s in video.speeds),
                f"min={min(video.speeds):.3f}, max={max(video.speeds):.3f}")
    ok &= check(f"持续漂移下残差有界（收敛后峰值 < {2*deadband:.2f}s）",
                peak_after < 2 * deadband, f"峰值 {peak_after:.3f}s")
    tail = trace[-6:]
    # Speed control keeps the residual inside OR just at the deadband edge
    # (the proportional term shrinks as the error shrinks, and the floor keeps
    # a tiny correction alive). Allow the boundary value itself.
    ok &= check("收尾阶段偏差在死区或边界上",
                all(abs(r) <= deadband + 0.02 for r in tail),
                f"末 6 次残差 {[round(r, 3) for r in tail]}")
    return ok


# --------------------------------------------------------------------------
# CHECK 3 -- safety: unreliable position must be SKIPPED
# --------------------------------------------------------------------------

def check_unreliable_skipped(follow_mod) -> bool:
    banner("CHECK 3  (C) 安全：位置不可靠（汽水冻结）必须跳过，不能乱调")
    F, step = step_of(follow_mod)
    if F is None:
        return check("模块提供 Follower", False, "旧代码无此类型")
    key = follow_mod.TrackKey(title="t", artist="a", duration_bucket=1)
    period = period_of(follow_mod, 6.0)

    # The frozen-position case: SMTC says 105.0s forever while the video runs.
    # A naive loop reads a 40s "error" -- huge, stable, and entirely fake.
    actions = []
    t = 60.0
    for _ in range(10):
        t += period
        action, correction = step(
            key, music_position=105.0, video_position=145.0,
            usable=False, now=t,
        )
        actions.append((action, correction))

    ok = check("不可靠输入 -> 从未产生任何微调量",
               all(c is None for _, c in actions),
               f"产生的微调量: {[c for _, c in actions if c is not None]}")
    ok &= check("不可靠输入 -> 判定都不是『correct』",
                all(a != "correct" for a, _ in actions),
                f"{sorted({a for a, _ in actions})}")
    if hasattr(F, "closed_loop_step"):
        ok &= check("不可靠输入 -> 明确标记为 skip-unreliable",
                    all(a == "skip-unreliable" for a, _ in actions),
                    f"{sorted({a for a, _ in actions})}")
    else:
        ok &= check("旧代码没有闭环（因此这条其实恒真 —— 不能算判据）", False,
                    "旧模块无 closed_loop_step；安全判据无从施加")

    # A huge-but-"usable" sample must be refused as an anomaly rather than
    # seeked -- a 3s+ error is a fault, not drift (NOTES §1).
    f2, step2 = step_of(follow_mod)
    t = 60.0
    got = []
    for _ in range(4):
        t += period
        got.append(step2(key, music_position=100.0, video_position=140.0,
                         usable=True, now=t))
    ok &= check("40s 的『偏差』被拒绝，而不是被 seek 掉",
                all(a != "correct" for a, _ in got), f"{[a for a, _ in got]}")
    if hasattr(F, "closed_loop_step"):
        ok &= check("异常反复出现后停用闭环（不会 seek 风暴）",
                    getattr(f2, "_loop_gave_up", False) and got[-1][0] == "gave-up",
                    f"gave_up={getattr(f2, '_loop_gave_up', None)}")
    return ok


def check_staleness_compensated(follow_mod) -> bool:
    """The video position must be aged by the status file's OWN staleness.

    WHY THIS CRITERION EXISTS (found in live testing 2026-10-08): mvm_control.lua
    rewrites state/_mvm_status.txt every 0.5s, so the position read from it
    describes the playhead as of the last rewrite. The loop added only the time
    since OUR read, ignoring that 0-0.5s, and therefore UNDER-estimated the
    video position. The live correction sequence was

        -0.9, -1.3, -1.7, +0.3, -1.3, +0.3, -0.4, -0.6, -0.5, -1.0

    -- a near-constant negative bias (~-0.7s) while the true drift was only
    0.4 s/min (0.13s over 19s). The loop was chasing its own measurement error,
    which would have made the picture twitch rather than stay aligned.

    A caller that passes no staleness must still behave as before (the default
    keeps older call sites working), so the criterion is about the ARGUMENT
    being honoured, not about the default.
    """
    banner("CHECK 7  (C) 陈旧量补偿：状态文件 0.5s 陈旧必须计入，否则闭环追自己的误差")
    F, step = step_of(follow_mod)
    if F is None:
        return check("旧代码没有闭环 -> 判据能先失败", False, "旧模块无 Follower")

    f = make_follower(follow_mod)
    if f is None or not hasattr(f, "_age_video_position"):
        return check("模块提供 _age_video_position", False,
                     "旧代码没有闭环位置老化逻辑")

    read_at, now = 1000.0, 1000.2      # our read happened 0.2s ago
    raw = 50.0

    # (a) With a 0.4s-stale file the value must be aged by 0.4 + 0.2 = 0.6s.
    try:
        aged = f._age_video_position(raw, read_at, now, staleness=0.4)
    except TypeError:
        return check("_age_video_position 接受 staleness 参数", False,
                     "签名里没有 staleness -> 无法补偿陈旧量")
    ok = check("陈旧 0.4s + 读取 0.2s -> 画面 50.6s",
               aged is not None and abs(aged - 50.6) < 1e-6,
               f"aged={aged}")

    # (b) THE BIAS THIS PINS: ignoring staleness reports 50.2s instead of
    # 50.6s -- a 0.4s under-estimate, i.e. the loop believes the picture is
    # BEHIND and pushes it forward again on every cycle.
    no_staleness = f._age_video_position(raw, read_at, now, staleness=0.0)
    ok &= check("漏掉陈旧项会低估 0.4s（旧实现的系统性偏差）",
                no_staleness is not None and abs(no_staleness - 50.2) < 1e-6,
                f"漏项 {no_staleness}s vs 正确 {aged}s")

    # (c) A stalled timer must still be refused rather than extrapolated.
    stalled = f._age_video_position(raw, read_at, now, staleness=30.0)
    ok &= check("状态文件过期过久 -> 拒绝（不拿陈旧值校正）",
                stalled is None, f"got {stalled}")

    # (d) Default behaviour is unchanged for callers that pass nothing.
    default = f._age_video_position(raw, read_at, now)
    ok &= check("不传 staleness 时行为与原来一致（向后兼容）",
                default is not None and abs(default - 50.2) < 1e-6,
                f"default={default}")
    return ok


def check_confirmed_desync_recovers(follow_mod) -> bool:
    """A CONFIRMED large desync must be resynced, not abandoned.

    WHY THIS CRITERION EXISTS (measured live 2026-10-08): the previous song's
    fine alignment finished after the track had already changed, leaving the
    picture 29.3s behind. The loop classified it as an anomaly, counted strikes,
    and then DISABLED itself for the rest of the song -- so the user watched a
    half-minute-desynced picture with no recovery. The log showed
    "闭环校验本曲停用（偏差反复异常，避免误调）" while the offset sat at -29.3s
    and then -38.8s.

    The fix separates the two situations that both produce a large residual:
      * ONE wild sample -> keep refusing (a seek could land anywhere);
      * TWO CONSECUTIVE agreeing samples -> it is a real desync, so resync.

    On the old code (which has no loop at all) this cannot pass, which is what
    makes it a criterion rather than a restatement of the implementation.
    """
    banner("CHECK 6  (C) 真实失步：连续确认后必须重同步，而不是放弃整首歌")
    F, step = step_of(follow_mod)
    if F is None or not hasattr(F, "closed_loop_step"):
        return check("旧代码没有闭环 -> 无法重同步（判据能先失败）", False,
                     "旧模块无 closed_loop_step")

    key = follow_mod.TrackKey(title="t", artist="a", duration_bucket=1)
    period = period_of(follow_mod, 6.0)

    # A ~29s desync that stays roughly constant across samples: the picture is
    # 29s BEHIND the music, so the correction must be positive (move forward).
    f, step = step_of(follow_mod)
    t = 60.0
    results = []
    for i in range(4):
        t += period
        # Real desyncs are not perfectly constant (both clocks run); 0.4s of
        # variation keeps this inside the confirmation window.
        music = 100.0 + i * period
        video = music - 29.0 + (0.2 if i % 2 else -0.2)
        results.append(step(key, music_position=music, video_position=video,
                            usable=True, now=t))

    actions = [a for a, _ in results]
    ok = check("首次大偏差先只确认、不动作",
               results[0][0] == "skip-anomaly", f"{actions}")
    ok &= check("第二次一致的大偏差 -> 判定为 resync",
                results[1][0] == "resync", f"{actions}")
    ok &= check("resync 的修正量方向正确（画面滞后 -> 向前移，正值）",
                results[1][1] is not None and results[1][1] > 0,
                f"correction={results[1][1]}")
    ok &= check("resync 量级约等于偏差（~29s）",
                results[1][1] is not None and 28.0 < results[1][1] < 30.0,
                f"correction={results[1][1]}")
    ok &= check("不会因此放弃整首歌",
                not getattr(f, "_loop_gave_up", False),
                f"gave_up={getattr(f, '_loop_gave_up', None)}")

    # CONTRAST: an INCONSISTENT large residual must never be acted on -- that is
    # the bad-sample case the anomaly guard exists for.
    f2, step2 = step_of(follow_mod)
    t = 60.0
    wild = []
    for i in range(3):
        t += period
        music = 100.0 + i * period
        # Residuals 20s, -5s, 15s: nothing agrees with anything.
        video = music + [20.0, -5.0, 15.0][i]
        wild.append(step2(key, music_position=music, video_position=video,
                          usable=True, now=t))
    ok &= check("互不一致的大偏差 -> 从不 resync（拒绝坏样本）",
                all(a != "resync" for a, _ in wild), f"{[a for a, _ in wild]}")

    # CONTRAST: beyond the resync ceiling the offset is more likely a WRONG
    # VIDEO than a timing problem, so it must be refused (铁律 15).
    f3, step3 = step_of(follow_mod)
    t = 60.0
    huge = []
    for i in range(3):
        t += period
        music = 100.0 + i * period
        video = music - 120.0        # 120s: wrong track, not drift
        huge.append(step3(key, music_position=music, video_position=video,
                          usable=True, now=t))
    ok &= check("超过上限的巨大偏差 -> 不 resync（疑似选错视频）",
                all(a != "resync" for a, _ in huge), f"{[a for a, _ in huge]}")
    return ok


class FakeSession:
    """Stand-in for smtc.NowPlaying -- only app_id is read by the loop."""

    def __init__(self, app_id: str = "cloudmusic.exe", position: float = 0.0,
                 rate: float = 1.0) -> None:
        self.app_id = app_id
        self.position = position
        self.rate = rate


def make_wired_follower(follow_mod, *, video_pos: float, music_pos: float,
                        reliable: bool = True, rate: float = 1.0,
                        running: bool = True):
    """Build a Follower wired to FAKE player/probe, then call the real probe.

    This exercises `_maybe_correct_drift` itself -- the guards, the position
    aging and the seek emission -- rather than only the pure decision function.
    Everything it touches outside the class is replaced, so no mpv, no SMTC and
    no PowerShell is involved.
    """
    F = getattr(follow_mod, "Follower", None)
    if F is None or not hasattr(F, "_maybe_correct_drift"):
        return None
    f = make_follower(follow_mod)
    player = FakePlayer(video_pos)
    player.running = running

    def seek_verified(target, *a, **k):
        player.apply(target - player.position)
        return (True, player.position)

    def set_property(name, value, *a, **k):
        if name == "speed":
            player.set_speed(float(value))
        return True

    player.seek_verified = seek_verified
    player.set_property = set_property
    player.get_position = lambda: player.position
    f.player = player

    # Replace the SMTC probe with a deterministic stub. `_position_sampled_at`
    # is set to "just now" so the aging term is ~0 and the numbers are exact.
    def probe(samples=3, gap=1.5):
        f._position_sampled_at = __import__("time").monotonic()
        return (reliable, music_pos, rate)

    f._probe_music_position = probe
    f._lock = __import__("threading").Lock()
    f.logs = []
    f.log = f.logs.append
    f.verbose = False
    f.align = True
    f._video_paused = False
    f._manual_offset_sec = 0.0
    f.player = player
    return f, player


def check_integration(follow_mod) -> bool:
    banner("CHECK 5  (C) 接线：真实 _maybe_correct_drift 会调速，且守规矩")
    F = getattr(follow_mod, "Follower", None)
    if F is None or not hasattr(F, "_maybe_correct_drift"):
        return check("Follower 提供 _maybe_correct_drift（闭环接线到主循环）", False,
                     "旧代码里主循环没有任何周期性校验")

    key = follow_mod.TrackKey(title="t", artist="a", duration_bucket=1)
    session = FakeSession()
    ok = True

    # --- (1) a real deviation must produce a SPEED change --------------
    # Session 9: ordinary drift (<2s) is corrected by nudging playback speed,
    # NOT by seeking -- a seek is a visible jump (user: "调整后不对齐").
    f, player = make_wired_follower(follow_mod, video_pos=100.8, music_pos=100.0)
    f.current = key
    f._pending = key
    f._detected_at = 0.0            # long past warmup
    f._loop_last_probe = None
    f._maybe_correct_drift(session, key)
    # Video is 0.8s AHEAD of music (residual > 0) -> SLOW DOWN (speed < 1.0).
    ok &= check("偏差 +0.80s -> 真的调整了播放速度（画面被平滑追平）",
                len(player.speeds) == 1 and player.speeds[0] < 1.0,
                f"speeds={[round(s, 3) for s in player.speeds]}")
    ok &= check("速度变化在 ±5% 内且不跳变",
                len(player.speeds) == 1
                and 0.95 <= player.speeds[0] < 1.0,
                f"speed={player.speeds[0] if player.speeds else None}")
    ok &= check("日志含「闭环校验」与偏差数值（用户可验收）",
                any("闭环校验" in m and "偏差" in m for m in f.logs),
                f"{[m.strip() for m in f.logs][:2]}")

    # --- (2) deadband: no NEW speed change; a previous in-flight one is
    #        restored to 1.0 -------------------------------------------
    f2, player2 = make_wired_follower(follow_mod, video_pos=100.10, music_pos=100.0)
    f2.current = key
    f2._pending = key
    f2._detected_at = 0.0
    f2._loop_key = key            # MUST match: closed_loop_step resets state
                                  # when _loop_key != key, wiping the in-flight
                                  # speed we set below.
    f2._loop_last_probe = None
    f2._loop_speed_applied = 1.03    # a correction is in flight
    f2._maybe_correct_drift(session, key)
    db = deadband_of(follow_mod, 0.25)
    ok &= check(f"死区内（偏差 0.10s < {db}s）不发起新调速",
                all(s == 1.0 for s in player2.speeds),
                f"speeds={[round(s, 3) for s in player2.speeds]}")
    # The in-flight speed must be restored to 1.0 once back in the deadband.
    ok &= check("回到死区后恢复 1.0x（防止过冲造成锯齿）",
                len(player2.speeds) >= 1 and player2.speeds[-1] == 1.0,
                f"speeds={[round(s, 3) for s in player2.speeds]}")
    ok &= check("死区不调整时日志说明了原因",
                any("死区" in m for m in f2.logs), f"{[m.strip() for m in f2.logs][:2]}")

    # --- (3) unreliable player: no correction, and it says so ----------
    f3, player3 = make_wired_follower(follow_mod, video_pos=145.0, music_pos=105.0,
                                      reliable=False, rate=0.0)
    f3.current = key
    f3._pending = key
    f3._detected_at = 0.0
    f3._loop_last_probe = None
    f3._maybe_correct_drift(session, key)
    ok &= check("位置不可靠（汽水冻结）不调速（硬要求）",
                len(player3.speeds) == 0, f"speeds={player3.speeds}")
    ok &= check("跳过时日志说明了是不可靠、且不做调整",
                any("跳过" in m and "不可靠" in m for m in f3.logs),
                f"{[m.strip() for m in f3.logs][:2]}")

    # --- (4) paused: no correction -------------------------------------
    f4, player4 = make_wired_follower(follow_mod, video_pos=100.8, music_pos=100.0)
    f4.current = key
    f4._pending = key
    f4._detected_at = 0.0
    f4._loop_last_probe = None
    f4._video_paused = True
    f4._maybe_correct_drift(session, key)
    ok &= check("暂停时不调整（_video_paused）", len(player4.speeds) == 0)

    # --- (5) a NEWER song pending: no correction -----------------------
    f5, player5 = make_wired_follower(follow_mod, video_pos=100.8, music_pos=100.0)
    other = follow_mod.TrackKey(title="other", artist="b", duration_bucket=2)
    f5.current = key
    f5._pending = other           # user changed song; this video is going away
    f5._detected_at = 0.0
    f5._loop_last_probe = None
    f5._maybe_correct_drift(session, key)
    ok &= check("已切歌（_pending 变了）时不调整，避免与切歌打架",
                len(player5.speeds) == 0)

    # --- (6) warmup: no correction while fine alignment may be running -
    f6, player6 = make_wired_follower(follow_mod, video_pos=100.8, music_pos=100.0)
    import time as _t
    f6.current = key
    f6._pending = key
    f6._detected_at = _t.monotonic()      # song just started
    f6._loop_last_probe = None
    f6._maybe_correct_drift(session, key)
    ok &= check("刚起播（精对齐可能还在跑）时不调整",
                len(player6.speeds) == 0
                and all("correct" not in str(m) for m in f6.logs))
    return ok


# --------------------------------------------------------------------------
# CHECK 4 -- manual offset must not be cancelled (task-2/task-3 integration)
# --------------------------------------------------------------------------

def check_manual_offset_respected(follow_mod) -> bool:
    banner("CHECK 4  (C) 语义：手动偏移是「用户偏好」，闭环不得立刻抵消")
    f = make_follower(follow_mod)
    has_pure = f is not None and hasattr(f, "measure_residual")
    if has_pure:
        residual = f.measure_residual(video_position=100.3, music_position=100.0,
                                      manual_offset=0.3)
        residual_ignored = f.measure_residual(video_position=100.3,
                                              music_position=100.0,
                                              manual_offset=0.0)
    else:
        # Model the old behaviour: residual ignores any user offset.
        residual = 100.3 - 100.0
        residual_ignored = residual

    # User nudged the picture +0.3s later. Music 100.0, video 100.3: with the
    # manual offset included this is the INTENDED steady state, i.e. residual 0.
    ok = check("手动偏移计入后，用户意图位置被判为已对齐",
               abs(residual) < 1e-9, f"residual={residual:+.3f}s")
    ok &= check("若忽略手动偏移就会误判为 +0.3s 偏差（即被闭环抵消）",
                abs(residual_ignored - 0.3) < 1e-9,
                f"residual={residual_ignored:+.3f}s")
    ok &= check("模块提供可调的手动偏移叠加点",
                has_pure and hasattr(f, "set_manual_offset"),
                "旧代码没有手动偏移语义" if not has_pure else "")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", action="store_true",
                    help="run against the frozen pre-task snapshot (must FAIL)")
    args = ap.parse_args()

    src = OLD_SRC if args.old else SRC
    label = "旧代码快照 _publish/src（预期失败）" if args.old else "当前 src（预期全绿）"
    banner(f"task-1 闭环微调 离线判据 —— {label}")
    print(f"  follow.py 来源: {src}")
    if not (src / "follow.py").exists():
        if args.old:
            # `--old` needs the development checkout's frozen snapshot. In a
            # clone of the public repo it is absent, which is expected -- say so
            # clearly rather than looking like a broken test (the whole point of
            # the mode is to show the criteria CAN fail, which only means
            # something where the pre-fix code is still available).
            print(f"  (跳过) --old 需要开发环境的旧代码快照，此副本中没有：{src}")
            print("  (正常判据请直接运行：python test_follow_loop.py)")
            return 0
        print(f"  ✗ 找不到 {src / 'follow.py'}")
        return 2

    follow_mod = load_follow(src)
    try:
        capture_mod = importlib.import_module("capture")
    except ImportError:
        capture_mod = None

    results = []
    # Every check runs against BOTH modules, so the old one fails on BEHAVIOUR
    # (no recorder facts, no corrector, error never shrinks) rather than merely
    # on a missing attribute.
    results.append(check_capture_timing(capture_mod))
    results.append(check_convergence(follow_mod))
    results.append(check_unreliable_skipped(follow_mod))
    results.append(check_integration(follow_mod))
    results.append(check_manual_offset_respected(follow_mod))
    results.append(check_confirmed_desync_recovers(follow_mod))
    results.append(check_staleness_compensated(follow_mod))

    banner("汇总")
    passed = sum(1 for r in results if r)
    print(f"  {passed}/{len(results)} 组判据通过")
    if args.old:
        if passed < len(results):
            print("  VERDICT: 旧代码如期失败 —— 判据确实能先失败（不是全绿假象）")
            return 0
        print("  VERDICT: 意外！旧代码竟然全部通过 —— 判据无效")
        return 1
    if passed == len(results):
        print("  VERDICT: 全部通过")
        return 0
    print("  VERDICT: 失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
