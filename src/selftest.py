"""Self-test -- verify the whole chain without needing network or cookies.

This exists because the real-world blockers (bilibili needs a cookie, niconico
CDN is unreachable here) would otherwise hide whether OUR logic is correct.
Each test isolates one link of the chain and asserts on it.

Run:  python selftest.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

# Resolved from MVM_FFMPEG, ./bin, or PATH -- never hard-coded to one machine.
from paths import find_ffmpeg  # noqa: E402

FFMPEG = find_ffmpeg()

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


# ---------------- link 1: SMTC ----------------

def _mk_sm_session(app_id="汽水音乐", title="釉中月", artist="洛天依",
                   duration=229.0, status="Playing") -> "NowPlaying":
    from smtc import NowPlaying
    return NowPlaying(
        app_id=app_id, title=title, artist=artist, album="",
        status=status, position_sec=10.0, duration_sec=duration,
    )


def test_smtc() -> None:
    print("\n=== 1. SMTC 读取播放器状态 ===")
    try:
        from smtc import pick_session, read_sessions
    except ImportError as exc:
        check("import smtc", False, str(exc))
        return

    sessions = read_sessions()
    check("read_sessions 不抛异常", True, f"读到 {len(sessions)} 个会话")
    for s in sessions:
        print(f"      - {s.summary()}")

    # Deterministic assertions on the whitelist gate (fail-closed). The old
    # "pick_session can pick a session" check was direction-inverted: it
    # PASSED when the follower would latch onto whatever happened to be
    # playing (the very bug the whitelist fixes) and went RED when the
    # whitelist was doing its job (nothing in whitelist + playing). It also
    # depended on whether some player happened to be playing right now.
    # These four cover the actual requirement (T7).
    wl_playing = _mk_sm_session(status="Playing")
    wl_paused = _mk_sm_session(status="Paused")
    foreign_playing = _mk_sm_session(app_id="msedgewebview2.exe", title="短剧",
                                     artist="", duration=55.3, status="Playing")

    check("白名单内 + Playing -> 可跟随",
          pick_session([wl_playing, foreign_playing]) is wl_playing)
    check("白名单内 + Paused -> 拒绝（require_playing）",
          pick_session([wl_paused]) is None)
    check("白名单外 + Playing -> 拒绝（未知 app）",
          pick_session([foreign_playing]) is None)
    check("白名单外 + 白名单内 Paused -> 不跟随",
          pick_session([foreign_playing, wl_paused]) is None)


# ---------------- link 2: duration scoring ----------------

def test_scoring() -> None:
    print("\n=== 2. 时长打分（选对版本的核心）===")
    from matcher import Matcher

    m = Matcher()
    exact = m._duration_score(248, 247.6)
    near = m._duration_score(210, 198)
    far = m._duration_score(100, 198)
    unknown = m._duration_score(0, 198)

    check("精确匹配得满分", exact >= 99, f"{exact:.1f}")
    check("接近匹配得分较高", near > 30, f"{near:.1f}")
    check("差距过大得 0 分", far == 0, f"{far:.1f}")
    check("未知时长给中性分", 0 < unknown < 50, f"{unknown:.1f}")
    check("排序关系正确", exact > near > far, f"{exact:.0f} > {near:.0f} > {far:.0f}")


# ---------------- link 3: title cleaning ----------------

def test_clean_title() -> None:
    print("\n=== 3. 标题清洗 ===")
    from matcher import _clean_title

    cases = [
        ("保持距离", "保持距离"),
        ("Song (Live)", "Song"),
        ("Song【官方】", "Song"),
        ("Song - Topic", "Song"),
        ("Song（完整版）", "Song"),
    ]
    for raw, want in cases:
        got = _clean_title(raw)
        check(f"{raw!r} -> {want!r}", got == want, f"实际 {got!r}")


# ---------------- link 4: VocaDB (network) ----------------

def test_vocadb() -> None:
    print("\n=== 4. VocaDB 数据源（需要网络）===")
    from vocadb import VocaDBSource

    src = VocaDBSource(use_cache=True)
    songs = src.search_songs("保持距离", max_results=3)
    if not songs:
        print("      (VocaDB 不可达或无结果 -- 跳过)")
        return

    check("VocaDB 返回条目", len(songs) > 0, f"{len(songs)} 条")
    with_pv = [s for s in songs if s.usable_pvs]
    check("至少一条含可用 PV", len(with_pv) > 0, f"{len(with_pv)} 条含 PV")
    for s in songs[:3]:
        print(f"      [{s.song_id}] {s.name} | {s.length_sec}s | PV={len(s.usable_pvs)}")
        for pv in s.usable_pvs[:2]:
            print(f"          {pv.platform_label()} {pv.pv_type} {pv.url}")


# ---------------- link 5: matching ----------------

def test_match() -> None:
    print("\n=== 5. 匹配（VocaDB + 排序）===")
    from matcher import Matcher

    m = Matcher()
    r = m.match("保持距离", "洛天依, PYH208", 247.6, allow_search_fallback=False)
    if not r.candidates:
        print("      (无候选 -- 可能 VocaDB 不可达或时长不匹配)")
        return
    check("找到候选", len(r.candidates) > 0, f"{len(r.candidates)} 个")
    best = r.best
    assert best is not None
    check("首选时长接近参考", abs(best.duration_sec - 247.6) <= 20,
          f"候选 {best.duration_sec}s vs 参考 247.6s")
    print(f"      选中: {best.describe()}")
    print(f"            {best.url}")


# ---------------- link 6: search fallback (network) ----------------

def test_search() -> None:
    print("\n=== 6. 搜索兜底（bilisearch，需要网络）===")
    from matcher import Matcher

    m = Matcher()
    cands = m.search_ytdlp("祈愿 洛天依", 198.0, platform="bilisearch", limit=3)
    if not cands:
        print("      (搜索无结果 -- 可能网络受限)")
        return
    check("搜索返回候选", len(cands) > 0, f"{len(cands)} 个")
    scored = [c for c in cands if c.duration_sec > 0]
    check("候选带有真实时长", len(scored) > 0, f"{len(scored)}/{len(cands)} 有时长")
    for c in cands[:3]:
        print(f"      {c.score:6.1f} | {c.duration_sec:6.1f}s | {c.url}")


# ---------------- link 7: isolated mpv ----------------

def test_player_local() -> None:
    print("\n=== 7. 隔离 mpv 播放本地视频 ===")
    from player import MpvController

    STATE.mkdir(parents=True, exist_ok=True)
    sample = STATE / "_selftest.mp4"

    # Regenerate if missing OR too short: an earlier version made a 12s clip,
    # which can finish before the CPU-growth check and look like a failure.
    needs_build = True
    if sample.exists() and sample.stat().st_size > 0:
        needs_build = False
        try:
            probe = subprocess.run(
                [str(FFMPEG), "-hide_banner", "-i", str(sample)],
                capture_output=True, text=True, timeout=30,
            )
            if "Duration: 00:00:2" in (probe.stderr or "") or "Duration: 00:00:1" in (probe.stderr or ""):
                needs_build = True
        except (subprocess.TimeoutExpired, OSError):
            pass

    if needs_build and FFMPEG.exists():
        subprocess.run(
            [str(FFMPEG), "-y", "-f", "lavfi",
             "-i", "testsrc=size=640x360:rate=25:duration=60",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(sample)],
            capture_output=True, timeout=120,
        )

    check("测试视频存在", sample.exists() and sample.stat().st_size > 0)
    if not sample.exists():
        return

    ctl = MpvController(mute=True, log=False)
    # Stop any leftover instance first: play_url reuses a running process, so a
    # stale idle mpv from an earlier test would shadow this one.
    ctl.stop()

    ok = ctl.play_url(str(sample))
    check("mpv 启动播放", ok)
    if not ok:
        return

    time.sleep(3)
    cpu1 = _mpv_cpu()
    time.sleep(3)
    cpu2 = _mpv_cpu()
    running = ctl.running

    check("播放中进程存活", running)
    # NOTE: a long-lived idle mpv can sit at a constant CPU value once the clip
    # finishes, so treat "CPU grew OR the window is up and the process is
    # healthy" as success rather than assuming continuous decode load.
    grew = cpu1 is not None and cpu2 is not None and cpu2 > cpu1
    title = _mpv_window_title()
    check("确实在解码或已渲染窗口", grew or title == "MVM-Video",
          f"CPU {cpu1} -> {cpu2}, 窗口={title!r}")
    check("视频窗口已创建", title == "MVM-Video", f"标题={title!r}")
    ctl.stop()


def _mpv_cpu() -> float | None:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process mpv -EA SilentlyContinue | Measure-Object CPU -Sum).Sum"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(r.stdout.strip())
    except (TypeError, ValueError):
        return None


def _mpv_window_title() -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process mpv -EA SilentlyContinue | Select-Object -First 1).MainWindowTitle"],
        capture_output=True, text=True, timeout=30,
    )
    return r.stdout.strip()


# ---------------- link 8: isolation guarantee ----------------

def test_isolation() -> None:
    print("\n=== 8. 与用户 mpv.net 的隔离保证 ===")
    from player import MPV, CONFIG_DIR

    check("使用独立 mpv 副本", "mpv-iso" in str(MPV), str(MPV))
    check("副本存在", MPV.exists())
    check("使用独立配置目录", "music-video-matcher" in str(CONFIG_DIR), str(CONFIG_DIR))

    # The user's config must not have been touched by us.
    user_conf = Path.home() / "AppData" / "Roaming" / "mpv.net" / "mpv.conf"
    if user_conf.exists():
        age_h = (time.time() - user_conf.stat().st_mtime) / 3600
        check("用户 mpv.net 配置未被近期修改", age_h > 1, f"{age_h:.1f} 小时前")


def test_alignment_math() -> None:
    """Verify cross-correlation on synthetic audio with a KNOWN offset.

    This is the only way to prove the alignment math is right without relying on
    real music: we generate a non-periodic signal, shift it by a known amount,
    and check the recovered lag.

    Note: a pure sine wave is NOT a valid test here -- it is periodic, so
    correlation locks onto the wrong (equally good) peak. Measured: a 440 Hz
    tone gave +0.00s instead of the true 2.5s. Real music behaves like the
    noise-based signal used below.
    """
    print("\n=== 9. 对齐算法（已知偏移的合成信号）===")
    from align import estimate_delay
    from pathlib import Path as _P

    work = STATE / "_selftest_align"
    work.mkdir(parents=True, exist_ok=True)
    if not FFMPEG.exists():
        print("      (缺少 ffmpeg，跳过)")
        return

    src = work / "src.wav"
    if not src.exists():
        subprocess.run(
            [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anoisesrc=d=60:c=pink:a=0.5",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
             "-filter_complex", "[1]volume=0.3[s];[0][s]amix=inputs=2",
             "-ac", "1", "-ar", "16000", str(src)],
            capture_output=True, timeout=180,
        )
    if not src.exists():
        print("      (无法生成测试音频，跳过)")
        return

    for shift in (1.0, 5.0):
        ref = work / f"ref{shift}.wav"
        pv = work / f"pv{shift}.wav"
        for path, start in ((ref, 10.0), (pv, 10.0 + shift)):
            subprocess.run(
                [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", str(start), "-i", str(src), "-t", "15",
                 "-ac", "1", "-ar", "16000", str(path)],
                capture_output=True, timeout=120,
            )
        res = estimate_delay(ref, pv, max_lag_sec=10.0)
        err = abs(abs(res.delay_sec) - shift)
        check(f"恢复 {shift:.1f}s 偏移（误差 {err:.2f}s）",
              res.method == "correlation" and err < 0.5,
              f"测得 {res.delay_sec:+.3f}s, conf={res.confidence}")

    # And the guard must reject unrelated audio.
    other = work / "other.wav"
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anoisesrc=d=20:c=white:a=0.5",
         "-ac", "1", "-ar", "16000", str(other)],
        capture_output=True, timeout=120,
    )
    if other.exists():
        ref = work / "ref1.0.wav"
        res = estimate_delay(ref, other, max_lag_sec=10.0)
        check("无关音频被拒绝（不产生伪偏移）", not res.trustworthy,
              f"method={res.method}")


def test_capture_available() -> None:
    print("\n=== 10. 系统音频采集（WASAPI loopback）===")
    try:
        from capture import LoopbackRecorder
    except ImportError as exc:
        check("导入 capture", False, str(exc))
        return

    rec = LoopbackRecorder()
    if not rec.open():
        check("打开默认输出设备", False, "WASAPI 不可用")
        return
    info = rec.format_info()
    check("打开默认输出设备", True, f"{info.get('rate')}Hz "
          f"{info.get('channels')}ch {info.get('bits')}bit")
    started = rec.start()
    check("以 loopback 模式启动采集", started)
    if started:
        time.sleep(1.0)
        chunk = rec.read_chunk()
        check("能读取到音频数据", chunk is not None and len(chunk) > 0,
              f"{len(chunk) if chunk else 0} 字节")
    rec.close()


def test_control_channel() -> None:
    """Verify mpv actually EXECUTES the commands we send.

    This test exists because of a real false positive: an earlier version sent
    commands over `--input-terminal` stdin and "verified" success by checking
    the PID stayed constant across song switches. But mpv never executed any of
    them -- the PID was stable precisely because nothing happened. Only mpv's
    own log revealed it ("Run command" entries never appeared).

    So: assert on the observed playhead position, not on process identity.
    """
    print("\n=== 11. mpv 控制通道（命令真的被执行）===")
    from player import MpvController

    sample = STATE / "_selftest.mp4"
    if not sample.exists():
        print("      (缺少测试视频，跳过)")
        return

    ctl = MpvController(mute=True, log=False)
    ctl.stop()
    if not ctl.start():
        check("启动常驻 mpv", False)
        return
    check("启动常驻 mpv", True, f"pid={ctl.proc.pid if ctl.proc else '?'}")

    try:
        if not ctl.play_url(str(sample)):
            check("loadfile 被接受", False)
            return
        time.sleep(3.5)
        before = ctl.get_position()
        check("loadfile 真的生效（有播放位置）", before is not None,
              f"position={before}")

        # The decisive assertion: seek must move the playhead.
        ctl.seek(30.0)
        time.sleep(3.0)
        after = ctl.get_position()
        check("seek 真的生效（播放位置跳到 30s 附近）",
              after is not None and after > 25.0,
              f"{before} -> {after}")

        # And the process must be reused, not restarted.
        pid_before = ctl.proc.pid if ctl.proc else None
        ctl.play_url(str(STATE / "_demo_pv.mp4")) if (STATE / "_demo_pv.mp4").exists() else None
        time.sleep(2)
        check("切换文件复用同一进程", ctl.running and ctl.proc and ctl.proc.pid == pid_before,
              f"pid={ctl.proc.pid if ctl.proc else None}")
    finally:
        ctl.stop()


def test_official_pv_preferred() -> None:
    """The ORIGINAL song must outrank fan covers/remixes.

    Regression test for a real user-visible bug: searching 九九八十一 returned
    the official entry at #3 and a fan cover at #1, because scoring only looked
    at PV-level fields (pvType="Original" for every upload) and ignored the
    song-level songType.
    """
    print("\n=== 12. 优先官方 PV（而非二创翻唱）===")
    from matcher import Matcher

    m = Matcher()
    # Pure scoring check -- no network needed.
    orig = m._song_type_score("Original")
    cover = m._song_type_score("Cover")
    remix = m._song_type_score("Remix")
    check("Original 得分高于 Cover", orig > cover, f"{orig} vs {cover}")
    check("Original 得分高于 Remix", orig > remix, f"{orig} vs {remix}")
    check("Cover 得分为负（明确降权）", cover < 0, f"{cover}")

    # End-to-end check when the network is available.
    r = m.match("九九八十一", "洛天依", 286.0)
    if not r.candidates:
        print("      (VocaDB 不可达，跳过端到端检查)")
        return
    best = r.best
    assert best is not None
    check("首选是 Original 版本",
          "original" in (best.reason or "").lower(),
          f"选中: {best.reason} | {best.url}")


def test_window_stays_put() -> None:
    """The video window must not move or resize when songs change.

    Regression test: every song change used to reposition/resize the window,
    because mpv resizes to each new video's aspect ratio by default.
    """
    print("\n=== 13. 窗口位置在切歌时保持不变 ===")
    from player import MpvController

    s1 = STATE / "_selftest.mp4"
    s2 = STATE / "_demo_pv.mp4"
    if not s1.exists():
        print("      (缺少测试视频，跳过)")
        return

    def rect() -> str:
        ps = (
            "$p=Get-Process mpv -EA SilentlyContinue | Select-Object -First 1;"
            "if($p){Add-Type @\"\nusing System;using System.Runtime.InteropServices;"
            "public class W{[DllImport(\"user32.dll\")]public static extern bool "
            "GetWindowRect(IntPtr h,out R r);public struct R{public int L,T,Rr,B;}}\n\"@;"
            "$r=New-Object W+R;[W]::GetWindowRect($p.MainWindowHandle,[ref]$r)|Out-Null;"
            "\"$($r.L),$($r.T)\"}else{'none'}"
        )
        try:
            res = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=30)
            return res.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return "?"

    ctl = MpvController(mute=True, log=False)
    ctl.stop()
    try:
        ctl.start()
        ctl.play_url(str(s1))
        time.sleep(4)
        first = rect()
        if s2.exists():
            ctl.play_url(str(s2))
            time.sleep(4)
            second = rect()
            check("切歌后窗口位置不变", first == second and first != "none",
                  f"{first} -> {second}")
        else:
            print("      (缺少第二个视频，跳过切换检查)")
        check("已启用 auto-window-resize=no",
              "--auto-window-resize=no" in ctl._base_args())
    finally:
        ctl.stop()


def test_single_instance() -> None:
    """A second follower must be refused while one holds the lock.

    Regression test: two followers were left running, each with its own mpv,
    sharing one command file -- the user saw 3 windows per song and stale ones
    never closed.
    """
    print("\n=== 14. 单实例锁（防止多窗口互抢）===")
    import subprocess as sp

    from single_instance import AlreadyRunning, SingleInstance

    lock_file = STATE / ".selftest.lock"
    try:
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass

    holder = SingleInstance(lock_file)
    holder.acquire()
    check("第一个实例获取锁", holder.acquired)

    # A second process (not a second object in this process) must be refused.
    script = (
        "import sys;"
        f"sys.path.insert(0, r'{Path(__file__).resolve().parent}');"
        "from single_instance import SingleInstance, AlreadyRunning;"
        f"l = SingleInstance(r'{lock_file}');"
        "\ntry:\n"
        "    l.acquire(); print('ACQUIRED')\n"
        "except AlreadyRunning as e:\n"
        "    print('REFUSED', e.pid)\n"
    )
    tmp = STATE / "_selftest_lock.py"
    tmp.write_text(script, encoding="utf-8")
    try:
        res = sp.run([sys.executable, str(tmp)], capture_output=True,
                     text=True, timeout=60)
        out = (res.stdout or "").strip()
        check("第二个进程被拒绝", out.startswith("REFUSED"), out or res.stderr[:80])
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
        holder.release()
        try:
            lock_file.unlink()
        except OSError:
            pass


def test_quality_gate() -> None:
    """A weak best match must be rejected rather than played.

    Regression test: for a song with no matching video, the search fallback
    returned five unrelated videos (closest still 94s off, all scoring the 5.0
    floor) and the follower happily played one of them.
    """
    print("\n=== 15. 匹配质量门限（宁可不放，也不放错的）===")
    from matcher import MIN_ACCEPTABLE_SCORE, Candidate, MatchResult

    weak = MatchResult(
        query="x", reference_duration=130.0,
        candidates=[Candidate(url="u", platform="Bilibili", pv_type="",
                              author="", score=5.0, duration_sec=246.0,
                              source="search")],
    )
    check("弱匹配返回 None", weak.best is None, weak.rejected_reason)

    strong = MatchResult(
        query="x", reference_duration=286.0,
        candidates=[Candidate(url="u", platform="Bilibili", pv_type="Original",
                              author="a", score=177.8, duration_sec=292.0,
                              source="vocadb")],
    )
    check("强匹配正常返回", strong.best is not None)
    check("阈值设置合理", 5.0 < MIN_ACCEPTABLE_SCORE < 100.0,
          f"阈值={MIN_ACCEPTABLE_SCORE}")


def test_single_window_invariant() -> None:
    """Only ONE MVM window may survive cleanup, and ours must be spared.

    Regression test for the user-visible bug: several video windows appeared
    and stale ones never closed. Rather than chase every spawn path, the code
    now enforces the invariant directly before each song change.
    """
    print("\n=== 16. 单窗口不变量（清理多余窗口，保留自己）===")
    import subprocess as sp

    from player import MPV, MpvController

    def mpv_pids() -> list[int]:
        r = sp.run(["powershell", "-NoProfile", "-Command",
                    "@(Get-Process mpv -EA SilentlyContinue | ForEach-Object { $_.Id }) -join ','"],
                   capture_output=True, text=True, timeout=30)
        return [int(x) for x in (r.stdout or "").strip().split(",") if x.strip().isdigit()]

    ctl = MpvController(mute=True, log=False)
    ctl.stop()
    if not ctl.start():
        check("启动我们的 mpv", False)
        return
    own = ctl.pid

    # Spawn two impostors.
    strays = [
        sp.Popen([str(MPV), "--no-config", "--title=MVM-Video",
                  "--idle=yes", "--force-window=yes"],
                 stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        for _ in range(2)
    ]
    time.sleep(3)
    before = len(mpv_pids())
    killed = ctl.kill_stray_windows(keep_pid=own)
    time.sleep(2)
    after = mpv_pids()

    check("清理前确实有多个窗口", before >= 3, f"{before} 个")
    check("清理后只剩我们一个", after == [own], f"剩 {after}，期望 [{own}]")
    check("我们的进程未被误杀", ctl.running)
    ctl.stop()


def main() -> int:
    print("music-video-matcher 自检")
    print("=" * 60)
    test_smtc()
    test_scoring()
    test_clean_title()
    test_vocadb()
    test_match()
    test_search()
    test_player_local()
    test_isolation()
    test_alignment_math()
    test_capture_available()
    test_control_channel()
    test_official_pv_preferred()
    test_window_stays_put()
    test_single_instance()
    test_quality_gate()
    test_single_window_invariant()

    print("\n" + "=" * 60)
    total = _passed + _failed
    print(f"结果: {_passed}/{total} 通过" + (f", {_failed} 失败" if _failed else ""))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
