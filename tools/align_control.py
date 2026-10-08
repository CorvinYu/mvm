"""align_control.py -- a small always-available control window for manual alignment.

Why this exists
---------------
The mpv hotkeys (see config/scripts/mvm_control.lua) are the fast path, but
they only work while the mpv window has keyboard focus -- and this project's
window is deliberately NOT always-on-top (user decision, NOTES §4/§6), so it
is often hidden behind the music player. The user asked for "另外有按钮": a
separate little window that can be clicked at any time.

It must therefore be launchable WHILE the follower is already running, which
rules out doing the work here directly:

  * the follower owns the mpv instance and the closed loop (task-1) is
    continuously correcting the playhead. A second writer that also seeked the
    video would fight the loop and produce visible oscillation;
  * the follower is a long-lived process holding state (current TrackKey, the
    music anchor, whether the position is reliable).

So this window is a pure REQUESTER: every button publishes a pending manual
offset to state/_mvm_manual_offset.txt (align_calib.publish_pending_offset)
and the follower applies it on its next cycle. That also means the window can
be started, closed and restarted freely without disturbing playback.

What it shows
-------------
  * the current song as the follower sees it (state/_mvm_now.json)
  * the residual sync error, when the follower publishes one
  * the accumulated MANUAL offset, split into its three sources:
        current session (what the hotkeys/buttons have accumulated)
        song level     (remembered for this track in align_calib.json)
        app level      (remembered for this player, applies to every song)
    Keeping these visible is what makes the "闭环误差 + 手动偏好" semantics
    (task-2 §D) legible instead of a magic number.

Single instance
---------------
A second window would show stale numbers and the user would not know which one
is authoritative. state/.align_control.lock is used, via the project's own
SingleInstance (the same primitive the follower uses -- reused, not
reimplemented, so the stale-lock handling stays in one place).

Run:
    python tools/align_control.py            (starts; refuses if one is up)
    python tools/align_control.py --reset-lock   (clear a stale lock first)

Encoding note: this file is UTF-8 and is launched by Python, not by cmd.exe,
so Chinese UI text is safe here (铁律 9 applies to .cmd files only).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
STATE = ROOT / "state"
sys.path.insert(0, str(SRC))

from align_calib import (  # noqa: E402
    CALIB_FILE,
    MAX_ABS_OFFSET_SEC,
    CalibrationStore,
    clear_pending_offsets,
    publish_pending_offset,
)

# Where the follower publishes "what is playing right now" for external tools.
# Kept separate from the mpv status file: the mpv file has no notion of a
# matched candidate, a residual error, or which TrackKey the song maps to.
NOW_FILE = STATE / "_mvm_now.json"

LOCK_FILE = STATE / ".align_control.lock"

# Button steps. 0.1s is the fine adjustment the user asked for; 1.0s exists
# because a wrong cut can be a whole second out, and pressing 0.1s ten times
# while the video plays is unusable.
NUDGE_STEPS = (0.1, 1.0)


def _read_now() -> dict:
    """Read the follower's published state; tolerant of a missing file."""
    try:
        data = json.loads(NOW_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fmt(value, suffix: str = "s", width: int = 6) -> str:
    try:
        return f"{float(value):+.{width - 2}f}{suffix}"
    except (TypeError, ValueError):
        return "   --  "


class AlignControlWindow:
    """Tk window: shows state, publishes manual-offset requests."""

    def __init__(self, refresh_ms: int = 500) -> None:
        import tkinter as tk

        self.tk = tk
        self.store = CalibrationStore(CALIB_FILE)
        # What the user has accumulated in THIS window since the last time the
        # follower's on-disk value caught up:
        #   _base    -- the follower's last confirmed value (from the disk);
        #   _pending -- deltas clicked since then, shown as base+pending.
        # Keeping the two apart matters because the follower applies requests
        # asynchronously (one 0.2s poll plus a seek). If refresh() simply
        # overwrote the display with the disk value, a fast second click would
        # visibly snap back before the follower recorded it -- a measured
        # defect in the first version. The disk value is adopted only when it
        # actually differs from _base (the follower applied something, or a
        # hotkey nudged from the mpv side).
        self._base: float | None = None
        self._pending: float = 0.0
        self.session_offset: float = 0.0
        self._last_pending_flush = 0.0

        self.root = tk.Tk()
        self.root.title("MVM 手动对齐")
        self.root.attributes("-topmost", True)   # a control surface: must stay reachable
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self.refresh()
        self.root.after(refresh_ms, self._tick)

    # ---------------- layout ----------------

    def _build(self) -> None:
        tk = self.tk
        pad = {"padx": 6, "pady": 3}

        self.var_song = tk.StringVar(value="(等待 follow 守护)")
        self.var_error = tk.StringVar(value="--")
        self.var_manual = tk.StringVar(value="0.00s")
        self.var_song_level = tk.StringVar(value="--")
        self.var_app_level = tk.StringVar(value="--")
        self.var_status = tk.StringVar(value="")

        box = tk.Frame(self.root)
        box.pack(fill="x", **pad)

        rows = (
            ("当前歌曲", self.var_song),
            ("闭环误差", self.var_error),
            ("手动偏移（本次）", self.var_manual),
            ("歌曲级校准", self.var_song_level),
            ("app 级校准", self.var_app_level),
        )
        for i, (label, var) in enumerate(rows):
            tk.Label(box, text=label, anchor="e", width=14).grid(
                row=i, column=0, sticky="e")
            tk.Label(box, textvariable=var, anchor="w", width=30).grid(
                row=i, column=1, sticky="w")

        # --- nudge buttons -------------------------------------------------
        grid = tk.Frame(self.root)
        grid.pack(fill="x", **pad)
        tk.Label(grid, text="微调（画面相对音乐）", anchor="w").grid(
            row=0, column=0, columnspan=4, sticky="w")

        specs = (
            ("-1s", -1.0), ("-0.1s", -0.1),
            ("+0.1s", 0.1), ("+1s", 1.0),
        )
        for i, (text, delta) in enumerate(specs):
            tk.Button(grid, text=text, width=7,
                      command=lambda d=delta: self.nudge(d)).grid(
                row=1, column=i, padx=3, pady=2)

        actions = tk.Frame(self.root)
        actions.pack(fill="x", **pad)
        tk.Button(actions, text="重置本歌", width=10,
                  command=self.reset_current).grid(row=0, column=0, padx=3)
        tk.Button(actions, text="保存为跨歌校准", width=16,
                  command=self.save_as_app_default).grid(row=0, column=1, padx=3)
        tk.Button(actions, text="清除本歌校准", width=13,
                  command=self.clear_song).grid(row=0, column=2, padx=3)

        tk.Label(self.root, textvariable=self.var_status, anchor="w",
                 fg="#555").pack(fill="x", padx=8)
        tk.Label(self.root,
                 text="mpv 窗口内热键: [ ] = ∓0.1s   { } = ∓1s   0 = 重置",
                 anchor="w", fg="#555").pack(fill="x", padx=8, pady=(0, 6))

    # ---------------- state refresh ----------------

    def _tick(self) -> None:
        self.refresh()
        self.root.after(500, self._tick)

    def refresh(self) -> None:
        """Re-read the calibration file and the follower's published state.

        The calibration file is re-read every tick (not cached) because the
        FOLLOWER is the one that records nudges -- if this window cached its
        own copy it would show 0.0s while the follower had already saved the
        user's keypress.
        """
        self.store = CalibrationStore(CALIB_FILE)
        now = _read_now()

        title = (now.get("title") or "").strip()
        artist = (now.get("artist") or "").strip()
        if title:
            self.var_song.set(f"{title}" + (f" - {artist}" if artist else ""))
        else:
            self.var_song.set("(未在跟随 / follow 未发布状态)")

        err = now.get("auto_error")
        self.var_error.set(_fmt(err) if err is not None else "--")

        # What the user has accumulated in this window; once the follower
        # records it the song-level value takes over and matches it.
        track_key = now.get("track_key") or ""
        app_id = now.get("app_id") or ""
        song_level = self.store.song_offset(track_key) if track_key else None
        app_level = self.store.app_offset(app_id) if app_id else None
        on_disk = (song_level if song_level is not None
                   else (app_level if app_level is not None else 0.0))

        # Adopt the disk value only when it moved on its own: either the
        # follower applied our pending request, or a hotkey nudged from the
        # mpv side. Otherwise keep showing base+pending so rapid clicks do not
        # appear to be lost while the follower is still catching up.
        if self._base is None or abs(on_disk - self._base) > 1e-6:
            self._base = on_disk
            self._pending = 0.0
        self.session_offset = self._base + self._pending

        self.var_manual.set(f"{self.session_offset:+.2f}s")
        self.var_song_level.set(f"{song_level:+.2f}s" if song_level is not None
                                else "（无）")
        self.var_app_level.set(f"{app_level:+.2f}s" if app_level is not None
                               else "（无）")

    # ---------------- actions ----------------

    def _publish(self, offset: float, source: str = "gui") -> None:
        now = _read_now()
        track_key = now.get("track_key") or ""
        app_id = now.get("app_id") or ""
        if not track_key:
            self.var_status.set("⚠ follow 守护未发布当前歌曲，无法校准（先启动 follow）")
            return
        if publish_pending_offset(offset, track_key, app_id, source=source):
            self.var_status.set(
                f"已请求手动偏移 {offset:+.2f}s（{source}），等待守护施加…")
        else:
            self.var_status.set("⚠ 写入待施加偏移失败（检查 state 目录权限）")

    def nudge(self, delta: float) -> None:
        total = max(-MAX_ABS_OFFSET_SEC, min(MAX_ABS_OFFSET_SEC,
                                            self.session_offset + delta))
        # The follower applies the ABSOLUTE value (not a delta) so that a lost
        # or duplicated request cannot accumulate twice -- publishing deltas
        # over a file channel has no delivery guarantee.
        self._pending = total - (self._base or 0.0)
        self.session_offset = total
        self.var_manual.set(f"{self.session_offset:+.2f}s")
        self._publish(total, "gui")

    def reset_current(self) -> None:
        self._pending = -(self._base or 0.0)
        self.session_offset = 0.0
        self.var_manual.set(f"{self.session_offset:+.2f}s")
        self._publish(0.0, "reset")

    def save_as_app_default(self) -> None:
        """Promote the current offset to the app level (all songs of a player)."""
        now = _read_now()
        app_id = now.get("app_id") or ""
        if not app_id:
            self.var_status.set("⚠ 未知 app_id，无法保存为跨歌校准")
            return
        ok = self.store.save_as_app_default(
            app_id, self.session_offset,
            note="用户从控制窗口保存为跨歌默认").offset_sec
        self.var_status.set(
            f"已保存 app 级校准 {ok:+.2f}s（{app_id}，对所有歌生效）")
        # Re-read so the displayed app level reflects what was just written and
        # the local pending delta is folded into the new baseline.
        self._base = None
        self.refresh()

    def clear_song(self) -> None:
        now = _read_now()
        track_key = now.get("track_key") or ""
        if not track_key:
            self.var_status.set("⚠ 未知当前歌曲，无法清除")
            return
        self.store.clear_track(track_key)
        self.var_status.set("已清除本歌校准（回退到 app 级）")
        self._base = None   # force adoption of the new on-disk value
        self.refresh()

    def _on_close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MVM 手动对齐控制窗口")
    ap.add_argument("--reset-lock", action="store_true",
                    help="先清除 .align_control.lock（仅在确认没有窗口在跑时用）")
    ap.add_argument("--clear-pending", action="store_true",
                    help="启动前清空待施加偏移队列")
    args = ap.parse_args(argv)

    from single_instance import AlreadyRunning, SingleInstance

    if args.reset_lock:
        try:
            LOCK_FILE.unlink()
            print(f"已清除锁 {LOCK_FILE}")
        except OSError:
            pass

    if args.clear_pending:
        clear_pending_offsets()
        print("已清空待施加偏移队列")

    # Reuse the project's SingleInstance: it already handles the stale-lock
    # races that were measured in this environment (体检报告/01 §1.4), and
    # reimplementing that logic would just reintroduce the bug.
    lock = SingleInstance(LOCK_FILE)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"已有控制窗口在运行（pid={exc.pid}）—— 不重复启动。", file=sys.stderr)
        return 2

    print(f"控制窗口启动中… 校准文件: {CALIB_FILE}")
    print("提示: 需要 follow 守护在运行才能施加偏移（本窗口只负责请求）。")
    try:
        AlignControlWindow().run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
