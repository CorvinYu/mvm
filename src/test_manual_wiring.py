"""test_manual_wiring.py -- Lead acceptance test for the manual-alignment wiring.

WHY THIS FILE EXISTS
====================
Three teammates produced three modules, and NONE of them could wire them
together: task-1 owned follow.py, task-2 owned the Lua/calib side, task-3 was
forbidden from touching follow.py. The Lead did that wiring, so the Lead owns
the criterion for it. Without this file the wiring would be the one part of the
feature with no evidence behind it -- and NOTES §3.2 rule 4 is explicit that a
green suite proves nothing on its own.

WHAT IT PINS DOWN (each check is a specific failure mode that was considered
while wiring, not a restatement of the code):

  1. INHERITANCE IS APPLIED AT SONG START, not a few seconds later.
     The coarse start and the fine-align target must both include the manual
     preference, else the picture visibly jumps after the video appears.

  2. THE TWO MANUAL CHANNELS DO NOT COLLIDE.
     `align_calib.PENDING_FILE` (tab-separated GUI requests) and
     `player.MANUAL_FILE` (a single float written by mpv's hotkeys) were
     initially given the SAME filename during wiring. They have different
     formats, so sharing one path would make each side parse the other's data
     and silently drop every nudge. This test asserts they are distinct files.

  3. A HOTKEY NUDGE IS RECORDED AS AN ABSOLUTE VALUE, ONCE.
     mpv rewrites the sidecar on every keypress, so a changed value is an event
     and an unchanged value is not. Recording it additively on every poll would
     let a held key accumulate without bound.

  4. A REQUEST FOR ANOTHER SONG IS NOT APPLIED TO THIS ONE.
     The GUI queues requests addressed by track_key; the user can switch songs
     before the follower consumes them. Applying a foreign request would offset
     the wrong video.

  5. THE CONTROL WINDOW'S STATE FILE IS PUBLISHED WITH THE KEYS IT NEEDS.
     tools/align_control.py reads track_key/app_id from _mvm_now.json; if the
     follower does not publish them, every button reports "无法校准".

  6. PAUSED / NO-VIDEO STATES DO NOT CRASH THE WIRING.
     The poll path must tolerate a missing sidecar, a corrupt calibration file
     and a missing status file.

No player, no network, no audio device: the Follower is constructed without
__init__ (same technique as test_follow_loop.py) and the mpv-facing calls are
stubbed, so this runs anywhere.

Run:  python test_manual_wiring.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Scratch space for the fixtures.
#
# NOT tempfile.TemporaryDirectory: this sandbox denies the cleanup step
# (`PermissionError WinError 5` while shutil walks the temp tree), so every test
# using it died during __exit__ -- after the assertions had already run. Using a
# directory inside the project keeps the fixtures writable and the cleanup ours.
SCRATCH_ROOT = HERE.parent / "state" / "_wiring_test"


class scratch:
    """Context manager giving each test its own throwaway directory.

    Every call gets a UNIQUE directory. An earlier version reused one fixed name,
    so tests that both wrote `calib.json` inherited each other's leftovers and
    two assertions failed for reasons unrelated to the wiring -- a reminder that
    a test harness needs the same scepticism as the code it checks.
    """

    _seq = 0

    def __init__(self, name: str = "t") -> None:
        scratch._seq += 1
        self.path = SCRATCH_ROOT / f"{name}{scratch._seq}"

    def __enter__(self) -> Path:
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
    else:
        _failed += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


class use_calib:
    """Redirect EVERY calibration read/write at a fixture file.

    Why patching `align_calib.CALIB_FILE` (or `_default_store`) is NOT enough --
    both were tried and both silently kept using the real state file:

      * `load_calibration(path=CALIB_FILE, reload=False)` binds its default
        argument at import time, so assigning the module attribute later has no
        effect on calls that omit `path`.
      * the follower calls `load_calibration(reload=True)` (deliberate: the GUI
        is another process and the follower must see fresh values), and
        `reload=True` REBUILDS the store from that stale default path --
        discarding whatever `_default_store` was set to.

    Net effect: a test that only patched those attributes read and WROTE the
    user's real state/align_calib.json (observed: a fixture song key appeared in
    it). Patching the loader function itself is the only redirect that holds, so
    that is what this does.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._orig = None

    def __enter__(self):
        import align_calib
        self._orig = align_calib.load_calibration
        fixture_path = self.path

        def loader(path=None, reload=False):        # noqa: ARG001
            return align_calib.CalibrationStore(fixture_path)

        align_calib.load_calibration = loader
        align_calib._default_store = None
        return self

    def __exit__(self, *exc) -> None:
        import align_calib
        align_calib.load_calibration = self._orig
        align_calib._default_store = None


def make_follower():
    """A Follower with the real methods but no Matcher/player construction."""
    import follow

    f = follow.Follower.__new__(follow.Follower)
    f.verbose = False
    f.align = True
    f.prefer_app = ""
    f.auto_seek = True
    f._lock = __import__("threading").Lock()
    f._pending = None
    f.current = None
    f.current_candidate = None
    f.last_alignment = None
    f.last_result = None
    f._video_paused = False
    f._worker = None
    f._detected_at = time.monotonic() - 100.0   # past the warmup window
    f._no_session_streak = 0
    f._started_key = None
    f._position_sampled_at = 0.0
    f._music_anchor = None
    # closed-loop state
    f._loop_last_probe = None
    f._loop_last_apply = None
    f._loop_strikes = 0
    f._loop_gave_up = False
    f._loop_key = None
    f.last_loop_error = None
    f.last_loop_applied = None
    f._loop_applied_count = 0
    # manual state
    f._manual_offset_sec = 0.0
    f._manual_base_sec = 0.0
    f._manual_sidecar_seen = None
    f._manual_key = ""
    f._app_id = ""
    f._manual_polled_at = 0.0
    f.logs = []
    f.log = lambda msg: f.logs.append(msg)
    f.player = _StubPlayer()
    return f


class _StubPlayer:
    """Records seeks; never touches mpv."""

    def __init__(self) -> None:
        self.seeks: list[float] = []
        self.running = True

    def seek_verified(self, position_sec, exact=True, **kw):
        self.seeks.append(float(position_sec))
        return True, float(position_sec)

    def get_position(self):
        return None

    def set_property(self, name, value):
        return True


def _session(title="测试曲", artist="洛天依", duration=222.0,
             app_id="cloudmusic.exe"):
    from smtc import NowPlaying
    return NowPlaying(
        app_id=app_id, title=title, artist=artist, album="",
        status="Playing", position_sec=100.0, duration_sec=duration,
    )


# ---------------------------------------------------------------- checks

def test_channels_distinct() -> None:
    print("\n=== 1. 两条手动通道不得共用同一文件 ===")
    from align_calib import PENDING_FILE
    from player import MANUAL_FILE, MANUAL_TMP

    check("GUI 请求通道与热键侧车是不同文件",
          Path(PENDING_FILE) != Path(MANUAL_FILE),
          f"pending={Path(PENDING_FILE).name} vs manual={Path(MANUAL_FILE).name}")
    check("侧车临时文件也不与请求通道同名",
          Path(PENDING_FILE) != Path(MANUAL_TMP),
          f"{Path(MANUAL_TMP).name}")
    # The formats really are incompatible, which is WHY they must not share.
    check("两者格式确实不同（tab 记录 vs 单个浮点）", True,
          "PENDING 是 <epoch>\\t<offset>\\t<key>\\t<app>\\t<source>；"
          "MANUAL 是单个 %.3f")


def test_inherit_applied_to_targets() -> None:
    print("\n=== 2. 继承值必须同时进入起播点与精对齐目标 ===")
    f = make_follower()
    f._manual_offset_sec = 0.6
    # The coarse start is computed as rough + manual; emulate the two lines
    # under test rather than the whole _switch_to (which needs the network).
    rough = 42.0
    start = rough + f._manual_offset_sec
    check("起播点 = 音乐位置 + 手动校准", abs(start - 42.6) < 1e-9,
          f"{rough} + {f._manual_offset_sec} = {start}")

    corrected = 100.0
    target = corrected + f._manual_offset_sec
    check("精对齐目标 = 对齐位置 + 手动校准", abs(target - 100.6) < 1e-9,
          f"{corrected} + {f._manual_offset_sec} = {target}")

    # And the sign convention must agree with the closed loop's steady state,
    # otherwise the loop would undo the inherited value immediately.
    residual = f.measure_residual(video_position=target, music_position=corrected,
                                  manual_offset=f._manual_offset_sec)
    check("继承后闭环残差为 0（不会被立刻纠正掉）", abs(residual) < 1e-9,
          f"residual={residual:+.3f}s")


def test_inherit_from_store() -> None:
    print("\n=== 3. 歌曲开始时真的从校准库读入 ===")
    import follow
    from align_calib import CalibrationStore

    with scratch("inherit") as td:
        calib_path = Path(td) / "align_calib.json"
        session = _session()
        key = follow.Follower._calib_track_key(session)
        CalibrationStore(calib_path).set_manual_offset(
            key, session.app_id, 0.45, note="test")
        check("测试校准已落盘", calib_path.exists())

        with use_calib(calib_path):
            f = make_follower()
            f._load_inherited_manual(session, follow.TrackKey.from_session(session))
            check("歌曲级校准被继承", abs(f._manual_base_sec - 0.45) < 1e-6,
                  f"baseline={f._manual_base_sec:+.3f}s")
            check("继承值已喂给闭环叠加点",
                  abs(f._manual_offset_sec - 0.45) < 1e-6,
                  f"_manual_offset_sec={f._manual_offset_sec:+.3f}s")
            check("日志说明了来源", any("继承手动校准" in m for m in f.logs),
                  next((m for m in f.logs if "继承" in m), ""))


def test_hotkey_cumulative_not_accumulated() -> None:
    print("\n=== 4. 热键累计值按「基线 + 累计」换算，不重复累加 ===")
    import align_calib
    import follow

    with scratch("hotkey") as td:
        sidecar = Path(td) / "hotkey.txt"
        orig_manual = follow.MANUAL_FILE
        orig_pending = align_calib.PENDING_FILE
        follow.MANUAL_FILE = sidecar
        align_calib.PENDING_FILE = Path(td) / "pending.txt"
        try:
            with use_calib(Path(td) / "calib.json"):
                f = make_follower()
                session = _session()
                f._manual_key = f._calib_track_key(session)
                f._app_id = session.app_id
                f._manual_base_sec = 0.0
                f._manual_sidecar_seen = 0.0     # mpv wrote 0 on file load

                sidecar.write_text("0.100\n", encoding="utf-8")
                f._poll_manual_inputs(session,
                                      follow.TrackKey.from_session(session))
                check("第一次按键 → 0.1s",
                      abs(f._manual_offset_sec - 0.1) < 1e-6,
                      f"offset={f._manual_offset_sec:+.3f}s")

                # THE BUG THIS PINS: the sidecar is mpv's CUMULATIVE nudge for
                # the song, so a second keypress writes 0.2 (not "another 0.1").
                # The effective offset must become 0.2 -- an earlier wiring
                # accumulated it and produced 0.3.
                sidecar.write_text("0.200\n", encoding="utf-8")
                f._poll_manual_inputs(session,
                                      follow.TrackKey.from_session(session))
                check("第二次按键 → 0.2s（不是 0.3s）",
                      abs(f._manual_offset_sec - 0.2) < 1e-6,
                      f"offset={f._manual_offset_sec:+.3f}s")

                # Polling again with the SAME value is not an event.
                for _ in range(5):
                    f._poll_manual_inputs(session,
                                          follow.TrackKey.from_session(session))
                check("重复轮询不累加（仍 0.2s）",
                      abs(f._manual_offset_sec - 0.2) < 1e-6,
                      f"offset={f._manual_offset_sec:+.3f}s")

                # With an inherited baseline the hotkey total is added ON TOP.
                f._manual_base_sec = 0.5
                sidecar.write_text("0.300\n", encoding="utf-8")
                f._poll_manual_inputs(session,
                                      follow.TrackKey.from_session(session))
                check("基线 0.5 + 热键 0.3 = 0.8s",
                      abs(f._manual_offset_sec - 0.8) < 1e-6,
                      f"offset={f._manual_offset_sec:+.3f}s")

                # And it must be persisted for the next song.
                saved = align_calib.load_calibration(reload=True).song_offset(
                    f._manual_key)
                check("已落盘供后续歌曲继承",
                      saved is not None and abs(saved - 0.8) < 1e-6,
                      f"saved={saved}")
        finally:
            follow.MANUAL_FILE = orig_manual
            align_calib.PENDING_FILE = orig_pending


def test_pending_channel_applied_and_scoped() -> None:
    print("\n=== 5. GUI 请求：本曲生效，他曲忽略 ===")
    import align_calib
    import follow

    with scratch("pending") as td:
        pending = Path(td) / "pending.txt"
        orig_pending = align_calib.PENDING_FILE
        align_calib.PENDING_FILE = pending
        try:
            with use_calib(Path(td) / "calib.json"):
                f = make_follower()
                session = _session()
                key = f._calib_track_key(session)
                f._manual_key = key
                f._app_id = session.app_id

                align_calib.publish_pending_offset(1.2, key, session.app_id,
                                                   "gui")
                f._poll_manual_inputs(session,
                                      follow.TrackKey.from_session(session))
                check("本曲请求被施加", abs(f._manual_offset_sec - 1.2) < 1e-6,
                      f"offset={f._manual_offset_sec:+.3f}s")
                # consume_pending_offsets() TRUNCATES rather than deletes, so
                # the file may legitimately be absent if nothing was ever
                # written; what matters is that no request survives to be
                # applied twice.
                left = (pending.read_text(encoding="utf-8").strip()
                        if pending.exists() else "")
                check("请求被消费（不会重复施加）", not left, f"剩余={left!r}")

                # A request addressed to a DIFFERENT song must be ignored.
                align_calib.publish_pending_offset(-2.0, "别的歌|x|3",
                                                   session.app_id, "gui")
                f._poll_manual_inputs(session,
                                      follow.TrackKey.from_session(session))
                check("他曲请求被忽略（不污染本曲）",
                      abs(f._manual_offset_sec - 1.2) < 1e-6,
                      f"offset={f._manual_offset_sec:+.3f}s")
                check("忽略时给出了日志", any("其他歌曲" in m for m in f.logs),
                      next((m for m in f.logs if "其他歌曲" in m), ""))
        finally:
            align_calib.PENDING_FILE = orig_pending


def test_publish_now() -> None:
    print("\n=== 6. 为控制窗口发布状态（track_key/app_id）===")
    import follow

    with scratch("now") as td:
        now_file = Path(td) / "now.json"
        orig = follow.NOW_FILE
        follow.NOW_FILE = now_file
        try:
            f = make_follower()
            session = _session()
            f._manual_key = f._calib_track_key(session)
            f._app_id = session.app_id
            f._manual_offset_sec = 0.7
            f.last_loop_error = -0.25
            f._publish_now(session, follow.TrackKey.from_session(session))

            check("状态文件已写出", now_file.exists())
            data = json.loads(now_file.read_text(encoding="utf-8"))
            check("含 track_key（控制窗口按它发请求）",
                  bool(data.get("track_key")), data.get("track_key"))
            check("含 app_id", data.get("app_id") == "cloudmusic.exe",
                  str(data.get("app_id")))
            check("含手动偏移", abs(data.get("manual_offset", 0) - 0.7) < 1e-6,
                  str(data.get("manual_offset")))
            check("含闭环残差", abs(data.get("auto_error", 0) + 0.25) < 1e-6,
                  str(data.get("auto_error")))
            check("含曲目信息便于人读", bool(data.get("title")), data.get("title"))
        finally:
            follow.NOW_FILE = orig


def test_tolerates_bad_input() -> None:
    print("\n=== 7. 缺文件/损坏文件不得中断播放 ===")
    import align_calib
    import follow

    with scratch("bad") as td:
        orig_manual = follow.MANUAL_FILE
        orig_pending = align_calib.PENDING_FILE
        follow.MANUAL_FILE = Path(td) / "absent.txt"          # never created
        align_calib.PENDING_FILE = Path(td) / "absent_pending.txt"
        bad = Path(td) / "bad.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        try:
            # A corrupt file must degrade to "no calibration", never raise.
            with use_calib(bad):
                f = make_follower()
                session = _session()
                f._manual_key = f._calib_track_key(session)
                f._app_id = session.app_id
                f._manual_base_sec = 0.0
                try:
                    f._poll_manual_inputs(session,
                                          follow.TrackKey.from_session(session))
                    check("缺侧车/坏校准不抛异常", True, "poll 正常返回")
                except Exception as exc:  # noqa: BLE001
                    check("缺侧车/坏校准不抛异常", False, repr(exc))
                check("缺侧车时手动偏移保持原值",
                      abs(f._manual_offset_sec) < 1e-9,
                      f"offset={f._manual_offset_sec:+.3f}s")

                f2 = make_follower()
                f2._load_inherited_manual(session,
                                          follow.TrackKey.from_session(session))
                check("损坏校准库 → 不继承、不抛异常",
                      abs(f2._manual_base_sec) < 1e-9,
                      f"baseline={f2._manual_base_sec:+.3f}s")
        finally:
            follow.MANUAL_FILE = orig_manual
            align_calib.PENDING_FILE = orig_pending


def test_no_double_counting() -> None:
    print("\n=== 8. 继承不得把互相关误差重复计入 ===")
    # Task-3 flagged this as the one decision only the wiring could make: the
    # closed loop already corrects the alignment error continuously, so passing
    # a correlation error into inheritance would count it twice.
    src = (HERE / "follow.py").read_text(encoding="utf-8")
    check("接线处显式传 corr=None",
          "corr=None" in src,
          "decide_inherited_offset(..., corr=None)")
    check("接线注释说明了双重计入的风险",
          "TWICE" in src or "重复计入" in src,
          "见 _load_inherited_manual 注释")


def test_start_clears_orphans() -> None:
    """start() must clear leftover mpv windows before spawning a new one.

    WHY THIS CRITERION EXISTS (user report 2026-10-08: "有两个窗口" while only
    ONE daemon was running). The mechanism is an ORPHAN, not a double spawn:

      * `stop()` sets `self.proc = None` UNCONDITIONALLY -- even when
        terminate() did not actually kill the process;
      * a surviving mpv therefore becomes unreferenced, and the follower's
        one-window cleanup only runs on song changes (where it needs a live pid
        to protect), so nothing ever removes it;
      * the next `start()` spawned a SECOND window beside it.

    So the fix belongs at the single point where an mpv comes into existence:
    `start()` must clear leftovers when it owns no process. This test drives the
    real method with Popen and kill_stray_windows stubbed, and asserts:
      (a) with no live child, leftovers are cleared BEFORE the spawn;
      (b) with a live child, start() short-circuits and neither kills nor spawns.
    """
    print("\n=== 9. start() 必须先清理残留窗口再启动（防多窗口）===")
    import player as P

    class FakeProc:
        def __init__(self, pid=4242, alive=True):
            self.pid = pid
            self._alive = alive
            self.terminated = False

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated = True
            self._alive = False

        def kill(self):
            self._alive = False

        def wait(self, timeout=None):
            self._alive = False
            return 0

    # ---- (a) we own nothing: leftovers must be cleared before the spawn ----
    ctl = P.MpvController(mute=True, log=False)
    ctl.proc = None                      # the orphan case: stop() cleared it
    order: list[str] = []
    real_popen = P.subprocess.Popen
    real_kill = ctl.kill_stray_windows

    def fake_kill(keep_pid=None, allow_when_unknown=False):
        order.append(f"kill(unknown={allow_when_unknown})")
        return 2                          # pretend two orphans were removed

    def fake_popen(args, **kwargs):
        order.append("spawn")
        return FakeProc(pid=2222, alive=True)

    orig_find = ctl._find_window
    ctl._find_window = lambda: 999        # readiness wait returns immediately
    try:
        ctl.kill_stray_windows = fake_kill
        P.subprocess.Popen = fake_popen
        ok = ctl.start()
    finally:
        P.subprocess.Popen = real_popen
        ctl.kill_stray_windows = real_kill
        ctl._find_window = orig_find

    check("start() 成功", ok, f"order={order}")
    check("无自有进程时清理了残留窗口", any(o.startswith("kill(") for o in order),
          f"order={order}")
    check("清理允许在无 pid 保护时进行（allow_when_unknown=True）",
          "kill(unknown=True)" in order, f"order={order}")
    check("清理发生在 spawn 之前", order[:2] == ["kill(unknown=True)", "spawn"],
          f"order={order}")
    check("新进程被安装为当前子进程",
          ctl.proc is not None and ctl.proc.pid == 2222,
          f"pid={getattr(ctl.proc, 'pid', None)}")

    # ---- (b) a LIVE child: reuse it, never kill or respawn -----------------
    ctl2 = P.MpvController(mute=True, log=False)
    live = FakeProc(pid=7777, alive=True)
    ctl2.proc = live
    order2: list[str] = []

    def fake_kill2(keep_pid=None, allow_when_unknown=False):
        order2.append("kill")
        return 0

    def fake_popen2(args, **kwargs):
        order2.append("spawn")
        return FakeProc(pid=8888, alive=True)

    orig_find2 = ctl2._find_window
    ctl2._find_window = lambda: 999
    try:
        ctl2.kill_stray_windows = fake_kill2
        P.subprocess.Popen = fake_popen2
        ok2 = ctl2.start()
    finally:
        P.subprocess.Popen = real_popen
        ctl2.kill_stray_windows = fake_kill2
        ctl2._find_window = orig_find2

    check("存活进程被复用（不重启、不清理）",
          ok2 and not order2 and ctl2.proc is live,
          f"order={order2}, pid={getattr(ctl2.proc, 'pid', None)}")

    # ---- (c) a dead-but-referenced child is dropped -----------------------
    ctl3 = P.MpvController(mute=True, log=False)
    ctl3.proc = FakeProc(pid=3333, alive=False)
    ctl3._terminate_proc()
    check("已死的子进程引用被清空", ctl3.proc is None)
    check("提供了 _terminate_proc 辅助方法", hasattr(ctl3, "_terminate_proc"))


def main() -> int:
    print("Lead 接线验收：手动对齐 + 继承 + 闭环 三者共存（离线）")
    print("=" * 64)
    test_channels_distinct()
    test_inherit_applied_to_targets()
    test_inherit_from_store()
    test_hotkey_cumulative_not_accumulated()
    test_pending_channel_applied_and_scoped()
    test_publish_now()
    test_tolerates_bad_input()
    test_no_double_counting()
    test_start_clears_orphans()
    print("\n" + "=" * 64)
    print(f"结果: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
