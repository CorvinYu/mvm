"""test_user_closed_window.py -- offline criteria for issue #5.

Run:  python src/test_user_closed_window.py          (expect all green)
      python src/test_user_closed_window.py --old    (expect the reopen cases to FAIL)

THE BUG (user report: "暂停歌曲后，手动关闭窗口后会被反复唤起")
    `Follower.step()` never consulted `self.player.running`, and
    `_current_still_listed()` only reads the SMTC session list -- which says
    nothing about our own window. So after the user closed the window mid-pause
    the daemon still believed a picture was showing, and reopened a NEW window
    the moment the music resumed or the song changed.

    That is the mirror image of issue #6: #6 = the daemon died and the window
    stayed; #5 = the window was closed and the daemon kept restarting it. Both
    come from the mpv window and the daemon having no shared lifecycle.

WHAT THIS TESTS
    The REAL `Follower.step()` is driven with fake session/player objects, so the
    decision chain under test is the shipped one -- not a copy. The scenario is
    the user's actual sequence: play -> pause -> close window -> resume.

`--old` MODE IS FAITHFUL BY CONSTRUCTION
    Every change made for #5 is conditioned on state that ONLY
    `_notice_user_closed_window()` sets (`_user_closed_key`, `_window_was_up`).
    Replacing that one method with a no-op therefore reproduces the old
    behaviour exactly for all the other edits, without transcribing old code
    that could drift from it. That is what makes the failing run meaningful.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import crashlog  # noqa: E402
import follow  # noqa: E402
from smtc import NowPlaying  # noqa: E402

_old_mode = "--old" in sys.argv
_results: list[tuple[bool, str, str]] = []

SCRATCH = SRC.parent / "state" / "_user_closed_test"


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


SONG_A = NowPlaying(app_id="cloudmusic.exe", title="歌A", artist="艺A", album="",
                    status="Playing", position_sec=10.0, duration_sec=200.0)
SONG_B = replace(SONG_A, title="歌B")


class FakePlayer:
    """Stand-in for MpvController: tracks liveness and start/stop calls."""

    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0
        self.props: list[tuple[str, object]] = []

    def start(self) -> bool:
        self.starts += 1
        self.running = True
        return True

    def stop(self) -> None:
        self.stops += 1
        self.running = False

    def set_property(self, key: str, value) -> bool:
        self.props.append((key, value))
        return True

    def kill_stray_windows(self, **kw) -> int:
        return 0

    @property
    def pid(self):
        return 4242 if self.running else None


class Harness:
    """Drives the real Follower.step() with controlled SMTC + player."""

    def __init__(self) -> None:
        self.sessions: list[NowPlaying] = []
        self.picked: NowPlaying | None = None
        self.switches: list[str] = []
        self.f = follow.Follower(matcher=object(), align=False, verbose=False)
        self.f.player = FakePlayer()
        # Side effects that touch files/PowerShell are irrelevant to the decision
        # under test; stub them so the test stays offline and deterministic.
        self.f._poll_manual_inputs = lambda s, k: None      # type: ignore[assignment]
        self.f._publish_now = lambda s, k: None             # type: ignore[assignment]
        if _old_mode:
            # Faithful old behaviour -- see the module docstring.
            self.f._notice_user_closed_window = lambda: None  # type: ignore[assignment]

    def step(self, sessions, picked) -> None:
        self.sessions = sessions
        self.picked = picked
        orig_read, orig_pick = follow.read_sessions, follow.pick_session
        follow.read_sessions = lambda: self.sessions           # type: ignore[assignment]
        follow.pick_session = lambda s, prefer_app=None: self.picked  # type: ignore[assignment]
        try:
            # _switch_to is what would open a window; record instead of spawning
            # real work, then let step()'s worker thread invoke it.
            self.f._switch_to = lambda session, key: self.switches.append(session.title)  # type: ignore[assignment]
            self.f.step()
            if self.f._worker is not None:
                self.f._worker.join(timeout=3)
        finally:
            follow.read_sessions, follow.pick_session = orig_read, orig_pick


def crashed_records() -> str:
    try:
        return crashlog.CRASH_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def reset_scratch() -> None:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    crashlog.CRASH_FILE_BACKUP = None  # type: ignore[attr-defined]
    crashlog.CRASH_LOG = SCRATCH / "crash.log"
    crashlog.EVIDENCE_DIR = SCRATCH


# --------------------------------------------------------------------------
print(f"\n模式：{'--old（旧码，预期 §1 失败）' if _old_mode else '正式（预期全绿）'}")
reset_scratch()
h = Harness()
paused_a = replace(SONG_A, status="Paused")

print("\n§0 前置：歌A 起播 → 窗口起来 → 暂停")
h.step([SONG_A], SONG_A)
check("检测到切歌并请求开窗", h.switches == ["歌A"], f"switches={h.switches}")
h.f.player.running = True                     # the window came up
h.step([SONG_A], SONG_A)
check("窗口在场被记录（_window_was_up）", h.f._window_was_up is True)
h.step([paused_a], None)                       # user paused
check("暂停时保留窗口（D2 行为不变）", h.f._video_paused is True,
      f"_video_paused={h.f._video_paused}")
check("暂停时未请求开新窗", h.switches == ["歌A"], f"switches={h.switches}")

print("\n§1 用户手动关闭窗口")
h.f.player.running = False                     # <-- the user closed the window
h.step([paused_a], None)
check("察觉到窗口被关闭", getattr(h.f, "_user_closed_key", None) == h.f.current,
      f"_user_closed_key={getattr(h.f, '_user_closed_key', None)}")
check("窗口关闭后清除“已冻结画面”状态（无处可冻）",
      h.f._video_paused is False, f"_video_paused={h.f._video_paused}")

print("\n§1b 暂停中该曲从 SMTC 列表消失（播放器常见行为）→ 守护拆掉状态")
for _ in range(follow.NO_SESSION_GRACE_POLLS + 1):
    h.step([], None)
check("状态已按既有逻辑拆解（self.current 被清空）", h.f.current is None,
      f"current={h.f.current}")

print("\n§1c 该曲重新出现 → 不得重开窗口（这就是“反复唤起”）")
before = len(h.switches)
for _ in range(3):
    h.step([SONG_A], SONG_A)
check("曲目重现后没有重开窗口（issue #5 的核心）",
      len(h.switches) == before, f"switches={h.switches}")
check("没有调用 player.start()", h.f.player.starts == 0,
      f"starts={h.f.player.starts}")
check("没有开窗 = player 仍未运行", h.f.player.running is False)

print("\n§1d 关闭事件必须留痕")
rec = crashed_records()
check("crash.log 记录了 USER-CLOSED-WINDOW", "USER-CLOSED-WINDOW" in rec,
      f"{rec.strip()[:80]!r}")

print("\n§2 切到真正的另一首歌 → 必须重新给窗口（修复不能等于“永远不再出画面”）")
h.step([SONG_B], SONG_B)
check("新歌重新请求开窗", h.switches[-1] == "歌B", f"switches={h.switches}")
check("新歌清除了“用户已关闭”标记",
      h.f._user_closed_key is None, f"{h.f._user_closed_key}")

print("\n§3 用户重新打开窗口 → 恢复正常跟随")
h.f.player.running = True
h.step([SONG_B], SONG_B)
check("窗口重新在场后标记为空", h.f._user_closed_key is None,
      f"{h.f._user_closed_key}")
check("窗口在场时 _window_was_up 为真", h.f._window_was_up is True)

# --------------------------------------------------------------------------
passed = sum(1 for ok, _, _ in _results if ok)
total = len(_results)
print("\n" + "=" * 60)
print(f"关窗判据: {passed}/{total} 通过"
      + ("  [--old 模式，§1 预期失败]" if _old_mode else ""))
if passed < total:
    print("\n失败项：")
    for ok, name, detail in _results:
        if not ok:
            print(f"  - {name}  {detail}")
print("=" * 60)
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(0 if passed == total else 1)
