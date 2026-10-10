"""Offline criterion: coarse alignment must not confirm a seek on stale data.

THE DEFECT THIS PINS DOWN (measured 2026-10-10, user report
"第一次切歌后直接从 0 开始播放，粗对齐没有生效。第二次切歌后生效"):

    `mvm_control.lua` republishes the status file every 0.5s, and `loadfile`
    does NOT reset it. So right after a song switch the file still reports the
    OUTGOING song's playhead. The old `_seek_after_load` (player.py):

        * proved "loaded" with `pos is not None` -- satisfied instantly by the
          previous song's position, so it never actually waited for the new
          media to open, and the seek it then sent was dropped by mpv (race 1);
        * confirmed the seek with `pos >= target_sec - 2.0` -- satisfied by ANY
          earlier position at or beyond the target. With the old song at 90s and
          the new target at 11.8s the confirmation passed in 0.25s on the OLD
          video's data.

    Result: the follower logged "✓ 已开始播放（起点 11.8s）" while the picture
    started at 0:00, and nothing was ever sought. The bug only showed on SOME
    switches -- only when the outgoing song was further along than the new
    target (which is exactly why the user saw the first switch fail and a later
    one work).

WHAT THE FIX REQUIRES (both phases now demand evidence about the NEW media):
    * phase 1 waits for a status snapshot that can only describe the media we
      asked for: matching path, a changed path, an observed playhead reset, and
      a rewrite newer than the loadfile we issued;
    * phase 2 accepts only a snapshot written AFTER the seek was issued (an
      older rewrite cannot report its effect) and requires the playhead to
      actually BE at the target rather than merely beyond it.

HOW TO RUN
    python src\\test_coarse_align.py        # expects every check to pass
    python src\\test_coarse_align.py --old  # expects the OLD logic to FAIL --
                                            # proof the criterion can fail

The `--old` mode matters: this project has been burned by criteria that passed
because they could not distinguish the bug from correct behaviour (see NOTES
§3.2 item 4: eight bugs coexisted with a fully green selftest).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import player as P  # noqa: E402

OLD_PATH = "https://upos.example/old/111111-1-30080.m4s?e=aaa&deadline=1"
NEW_PATH = "https://upos.example/new/222222-1-30080.m4s?e=bbb&deadline=2"
SAME_PATH = "https://upos.example/same/333333-1-30080.m4s?e=ccc&deadline=3"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line, flush=True)
    return ok


class StubPlayer:
    """Feeds `_seek_after_load` a scripted status-file timeline.

    A "stage" is (pos, path, age_seconds, reads): the status file reports `pos`
    for `path` as if it had been written `age_seconds` ago, for `reads` reads.
    `age` is what makes staleness testable -- a large age means the snapshot
    predates the loadfile/seek we issued, which is exactly the situation the
    real file produces between the switch and the Lua republishing it.

    Reads taken BEFORE the seek command is issued come from `pre`, afterwards
    from `post`, mirroring how a real seek changes what the file reports.
    """

    def __init__(self, pre, post):
        self.pre = list(pre)
        self.post = list(post)
        self.seek_issued = False
        self.commands: list[str] = []
        self.seek_issued_at: float | None = None
        self._pi = 0
        self._pc = 0
        self._si = 0
        self._sc = 0

    def _next(self, stages, idx_name, count_name):
        i = getattr(self, idx_name)
        c = getattr(self, count_name)
        if i >= len(stages):
            i = len(stages) - 1
            c = 0                       # repeat the last stage forever
        pos, path, age, _reads = stages[i]
        c += 1
        if c >= stages[i][3]:
            i += 1
            c = 0
        setattr(self, idx_name, i)
        setattr(self, count_name, c)
        return (pos, time.time() - age, path)

    def read_status_full(self):
        if self.seek_issued:
            return self._next(self.post, "_pi", "_pc")
        return self._next(self.pre, "_si", "_sc")

    def command(self, line: str) -> bool:
        self.commands.append(line)
        self.seek_issued = True
        self.seek_issued_at = time.time()
        self._pi = 0
        self._pc = 0
        return True


def run_new(pre, post, target, expect_path):
    """Run the CURRENT implementation; returns (result, commands)."""
    stub = StubPlayer(pre, post)
    ok = P.MpvController._seek_after_load(
        stub, target, expect_path=expect_path,
        load_timeout=1.2, seek_timeout=1.2)
    return ok, stub.commands


def run_old(pre, post, target):
    """Replicate the PRE-FIX logic so the criterion can be shown to fail.

    Kept here verbatim from the old implementation: "loaded" = any readable
    position, confirmation = `pos >= target - 2.0`.
    """
    stub = StubPlayer(pre, post)
    deadline = time.monotonic() + 1.2
    loaded = False
    while time.monotonic() < deadline:
        pos = stub.read_status_full()[0]
        if pos is not None:
            loaded = True
            break
        time.sleep(0.3)
    if not loaded:
        stub.command(f"seek {target:.2f} absolute+exact")
        return False
    stub.command(f"seek {target:.2f} absolute+exact")
    seek_deadline = time.monotonic() + 1.2
    while time.monotonic() < seek_deadline:
        time.sleep(0.05)
        pos = stub.read_status_full()[0]
        if pos is None:
            continue
        if pos >= target - 2.0:
            return True
    return False


def main() -> int:
    old_mode = "--old" in sys.argv
    print("=" * 72)
    if old_mode:
        print("模式: --old （复刻修复前的判据；期望它【失败】）")
    else:
        print("模式: 当前实现（期望全部通过）")
    print("=" * 72)

    # ------------------------------------------------------------------
    # 路径判据（纯函数）
    # ------------------------------------------------------------------
    print("\n§1 路径身份判据")
    if not old_mode:
        check("basename 忽略签名查询串",
              P.status_path_basename(OLD_PATH) == "111111-1-30080.m4s",
              P.status_path_basename(OLD_PATH))
        check("同一媒体：带/不带查询串都算命中",
              P.paths_refer_to_same_media(
                  "https://x/a/99999999999-1-30080.m4s?e=1&upsig=zz",
                  "https://x/a/99999999999-1-30080.m4s"))
        check("不同媒体：不得命中",
              not P.paths_refer_to_same_media(OLD_PATH, NEW_PATH))
        check("空 path 不得当作命中（fail closed）",
              not P.paths_refer_to_same_media(OLD_PATH, ""))
        check("反斜杠路径也能归一化",
              P.paths_refer_to_same_media(
                  "E:/media/song.m4s", "E:\\media\\song.m4s"))

    # ------------------------------------------------------------------
    # CASE A -- 用户报告的场景：旧值 90s，新目标 11.8s，新媒体始终没加载
    # ------------------------------------------------------------------
    print("\n§2 CASE A 旧值远大于目标（用户报告：日志说成功，画面在 0:00）")
    pre_a = [(90.0, OLD_PATH, 30.0, 999)]     # stale forever
    post_a = [(90.0, OLD_PATH, 30.0, 999)]
    if old_mode:
        # The old logic MUST reproduce the false positive; if it does not, this
        # criterion cannot distinguish the bug and is worthless.
        got = run_old(pre_a, post_a, 11.8)
        check("旧判据在陈旧值上误报成功（证明判据能失败）", got is True,
              f"返回 {got}，期望 True（=缺陷再现）")
    else:
        got, cmds = run_new(pre_a, post_a, 11.8, NEW_PATH)
        check("新媒体未加载时不得报告 seek 成功", got is False,
              f"返回 {got}")
        check("如实记录了『未确认生效』所需的依据（返回 False）",
              got is False and len(cmds) == 1,
              f"命令 {cmds}")

    # ------------------------------------------------------------------
    # CASE B -- 正常：新曲目加载后 seek 落到目标
    # ------------------------------------------------------------------
    print("\n§3 CASE B 正常路径：新媒体加载后落到 12.1s（目标 11.8s）")
    pre_b = [(90.0, OLD_PATH, 30.0, 2), (0.4, NEW_PATH, 0.05, 999)]
    post_b = [(12.1, NEW_PATH, 0.05, 999)]
    if old_mode:
        got = run_old(pre_b, post_b, 11.8)
        check("旧判据也接受正常成功（对照组）", got is True, f"返回 {got}")
    else:
        got, cmds = run_new(pre_b, post_b, 11.8, NEW_PATH)
        check("真实生效的 seek 必须判为成功", got is True, f"返回 {got}")
        check("确实发出了 seek 命令", cmds == ["seek 11.80 absolute+exact"],
              f"{cmds}")

    # ------------------------------------------------------------------
    # CASE C -- 新媒体加载了，但 seek 被丢弃（画面从 0 播）
    # ------------------------------------------------------------------
    print("\n§4 CASE C 新媒体已加载但 seek 被丢弃（画面从 0:00 播）")
    pre_c = [(90.0, OLD_PATH, 30.0, 2), (0.5, NEW_PATH, 0.05, 999)]
    post_c = [(0.8, NEW_PATH, 0.05, 1), (1.1, NEW_PATH, 0.05, 1),
              (1.4, NEW_PATH, 0.05, 999)]
    if old_mode:
        # Control case, NOT a demonstration of the defect: once the new media
        # really has loaded and the seek is dropped, the old `pos >= target-2`
        # test also rejects it (the playhead is small). The old code only
        # mis-reported when STALE data was still readable -- see CASE A and E.
        got = run_old(pre_c, post_c, 11.8)
        check("旧判据在此场景同样拒绝（对照组，非缺陷点）", got is False,
              f"返回 {got}，期望 False")
    else:
        got, _ = run_new(pre_c, post_c, 11.8, NEW_PATH)
        check("画面仍在片头时必须判为未确认", got is False, f"返回 {got}")

    # ------------------------------------------------------------------
    # CASE D -- 同一首歌重播（URL 相同）：只能靠『位置归零』识别
    # ------------------------------------------------------------------
    print("\n§5 CASE D 同一 URL 重播：路径不变，靠播放头归零识别")
    # Reported path does NOT match expect_path, and equals pre_path, so the only
    # available signal is the reset from 30s to 0.3s.
    pre_d = [(30.0, SAME_PATH, 30.0, 2), (0.3, SAME_PATH, 0.05, 999)]
    post_d = [(12.0, SAME_PATH, 0.05, 999)]
    if old_mode:
        got = run_old(pre_d, post_d, 11.8)
        check("旧判据在重播场景（对照组）", got is True, f"返回 {got}")
    else:
        got, _ = run_new(pre_d, post_d, 11.8,
                         "https://upos.example/unrelated/999.m4s?e=z")
        check("靠播放头归零识别重播并确认 seek", got is True, f"返回 {got}")

    # ------------------------------------------------------------------
    # CASE E -- 陈旧但 mtime 更新的读数不得用于确认（先于 seek 的重写）
    # ------------------------------------------------------------------
    print("\n§6 CASE E 先于 seek 的重写（即使位置很大）不得用于确认")
    pre_e = [(0.5, NEW_PATH, 0.05, 999)]
    post_e = [(95.0, NEW_PATH, 30.0, 3), (12.2, NEW_PATH, 0.05, 999)]
    if old_mode:
        got = run_old(pre_e, post_e, 11.8)
        check("旧判据会被 95s 的陈旧读数骗过（证明判据能失败）", got is True,
              f"返回 {got}，期望 True（=缺陷再现）")
    else:
        got, _ = run_new(pre_e, post_e, 11.8, NEW_PATH)
        check("跳过陈旧重写，用真实落点确认", got is True, f"返回 {got}")

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    passed = sum(1 for _n, ok, _d in _results if ok)
    total = len(_results)
    print("\n" + "=" * 72)
    print(f"{passed}/{total} 通过")
    if old_mode:
        print("--old 模式：上面的『证明判据能失败』各项即为预期结果。")
    print("=" * 72)
    failed = [n for n, ok, _d in _results if not ok]
    if failed:
        print("失败项:")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
