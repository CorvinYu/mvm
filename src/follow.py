"""follow.py -- the main daemon: follow whatever the user is playing.

Loop:
    SMTC (now playing)  ->  Matcher (find PV)  ->  isolated mpv (silent video)

Modes:
    * follow  (default) -- watch SMTC; when the song changes, switch the video
    * once              -- resolve and play a single song, then exit (testing)
    * probe             -- just print what SMTC sees and what we would match

Design decisions worth knowing:

  * We only switch when the *track identity* changes (title+artist+duration),
    not on every poll. Otherwise tiny metadata jitter would restart the video.

  * Duration agreement is the main signal for picking the right PV version.
    Measured: SMTC reported 247.6s, the correct VocaDB entry said 248s.

  * We deliberately do NOT auto-seek into the video by default. Different edits
    (MV vs album version) have different intros, so seeking to the source
    position can visibly desync. `--seek` enables it for experimentation.

  * Everything is isolated from the user's mpv.net (see player.py).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from matcher import (                                          # noqa: E402
    MIN_ACCEPTABLE_SCORE,
    Candidate,
    Matcher,
    MatchResult,
)
from player import MpvController, ResolveError, find_cookies, resolve_stream_url  # noqa: E402
from smtc import NowPlaying, pick_session, read_sessions      # noqa: E402
from delay_history import DelayHistory                        # noqa: E402

# Imported lazily inside methods to keep startup fast, but the type is needed at
# class-definition time for the annotation below.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from align import AlignmentResult

STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# How often to poll SMTC. 2s is responsive without being wasteful.
POLL_INTERVAL_SEC = 2.0

# How many consecutive polls with NO followable session before we tear the
# window down. Measured: during a real song change there is a brief gap where
# neither the old nor the new track is listed in SMTC; closing the window on
# the first such poll made the user see "close + reopen on every song change".
# 3 polls * 2s = 6s of grace, long enough for the switch, short enough that a
# closed player still gets cleaned up within a few seconds.
NO_SESSION_GRACE_POLLS = 3

# Fine-alignment tuning. Longer captures are more reliable but add latency to
# each song switch, so keep it modest.
ALIGN_CAPTURE_SEC = 10.0
# Slice the PV starting this far before the current position so the real lag
# falls inside the correlation window. Edits can differ by 10s+ in intro length.
ALIGN_PREROLL_SEC = 8.0
# Upper bound on scan windows when the player does not report its position, so a
# long video cannot stall a song switch indefinitely.
ALIGN_SCAN_MAX_TRIES = 12

# Download window for fine alignment, relative to the coarse estimate.
#
# The coarse estimate (real-time t2-t1) is good to a few seconds, so we do NOT
# need a wide slice. Measured 2026-10-07: the old +/-60s (120s total) download
# took 39-75s per song and alignment finished after the song had already ended
# ("精对齐太慢了，歌都播完了还没完成"). Download size is the dominant cost, so
# this is deliberately narrow: enough to cover the 10s capture plus a margin
# for intro/edit differences (a PV may open with a longer cold intro than the
# streaming master), but ~4x less data than before.
ALIGN_WINDOW_BEFORE = 15.0   # minimum reach-back from the coarse position
ALIGN_WINDOW_AFTER = 15.0    # how much after (also the correlation lag limit)
# Upper bound on the reach-back. The coarse estimate is a LOWER BOUND on the
# true position (see _fine_align), so the window must extend backwards far
# enough to contain the truth -- but correlation cost grows with the window, so
# this caps how far we are willing to look. 180s of audio is ~6s of correlation
# on this machine, still far better than the 39-75s the old path took.
MAX_ALIGN_WINDOW_BEFORE = 180.0
# Correlation lag search limit. MUST cover the whole downloaded window: the
# true offset can sit anywhere in it (the coarse estimate can be far too small,
# and the PV may differ in intro length). Keeping this below the window length
# would silently make the far end of the window unreachable.
ALIGN_MAX_LAG_SEC = 200.0


@dataclass(frozen=True)
class TrackKey:
    """Identity of a track -- used to detect song changes."""

    title: str
    artist: str
    duration_bucket: int

    @classmethod
    def from_session(cls, s: NowPlaying) -> "TrackKey":
        # Bucket duration to 10s so trivial jitter does not look like a new song.
        #
        # Measured 2026-10-07: a 5s bucket was too tight. 汽水音乐 reports the
        # duration with jitter while a track plays (183.5s, then 184.9s for the
        # same song), and a bucket boundary at 185s turned ONE song into TWO
        # different keys -- the follower started a second worker for the same
        # song, which produced two "已开始播放" lines, two alignment runs and
        # TWO mpv windows. 10s gives enough slack for that jitter.
        return cls(
            title=_normalise_title(s.title),
            artist=s.artist.strip().lower(),
            duration_bucket=int(s.duration_sec // 10) if s.duration_sec else 0,
        )


def _normalise_title(title: str) -> str:
    """Normalise a player-reported title for song-identity comparison.

    Players decorate titles inconsistently between polls: the same track can be
    reported as "勾指起誓" and "勾指起誓 - 洛天依" (artist appended), or gain
    " (Live)" / "【官方】" decorations mid-playback. Treating those as different
    songs made the follower restart the SAME song -- two windows for one track.
    """
    t = (title or "").strip().lower()
    # Drop a trailing " - <artist>" / " – <artist>" decoration.
    for dash in (" - ", " – ", " — ", "-"):
        if dash in t:
            head = t.split(dash)[0].strip()
            if head:
                t = head
                break
    # Drop bracketed decorations anywhere (【官方】, (Live), [MV], ...).
    for opener, closer in (("【", "】"), ("(", ")"), ("（", "）"), ("[", "]")):
        while opener in t and closer in t:
            start = t.find(opener)
            end = t.find(closer, start)
            if start == -1 or end == -1:
                break
            t = (t[:start] + t[end + 1:]).strip()
    return t


class Follower:
    """Watches SMTC and keeps an isolated mpv window in sync."""

    def __init__(
        self,
        matcher: Matcher,
        prefer_app: str = "",
        auto_seek: bool = True,
        verbose: bool = True,
        prefer_search: bool = False,
        align: bool = False,
    ) -> None:
        self.matcher = matcher
        self.prefer_app = prefer_app
        self.auto_seek = auto_seek
        self.verbose = verbose
        self.prefer_search = prefer_search
        self.align = align
        self.player = MpvController(mute=True)
        self.current: TrackKey | None = None
        self.current_candidate: Candidate | None = None
        self.last_result: MatchResult | None = None
        self.last_alignment: "AlignmentResult | None" = None
        # A5: rolling average of how long a song switch takes, per
        # (player, platform). Used as the initial seek target when SMTC's
        # position is unreliable, so the video does not start at 0:00.
        self.history = DelayHistory(STATE_DIR / "delay_history.json")
        # True while we are deliberately holding the video frozen because the
        # music was paused (decision D2). Also doubles as the "already logged"
        # flag so the pause message is printed once per pause, not every poll.
        self._video_paused = False
        # Match/resolve work in a worker thread so a slow lookup (VocaDB plus a
        # search fallback can take 10s+) never delays detecting the NEXT song
        # change. Measured: doing this inline made the loop miss subsequent
        # track switches entirely.
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending: TrackKey | None = None
        # Monotonic timestamp of when the current song was detected (A5).
        self._detected_at: float | None = None
        # Consecutive polls with no followable session (grace counter, see
        # NO_SESSION_GRACE_POLLS).
        self._no_session_streak = 0
        # The track key we last actually started playing. Prevents a second
        # worker from starting the SAME song again (two windows) when SMTC
        # briefly drops the player during a track change.
        self._started_key: TrackKey | None = None
        # When SMTC's position was last sampled (monotonic), so a sampled
        # position can be aged forward to "now".
        self._position_sampled_at: float = 0.0
        # (monotonic_time, music_position_sec) captured at playback start. The
        # absolute reference that fine alignment uses instead of trusting the
        # coarse guess.
        self._music_anchor: tuple[float, float] | None = None

    def log(self, msg: str) -> None:
        if self.verbose:
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] {msg}", flush=True)

    # ---------------- one step ----------------

    def step(self) -> None:
        """One poll iteration. Must return quickly -- never blocks on network."""
        sessions = read_sessions()
        session = pick_session(sessions, prefer_app=self.prefer_app)

        if session is None:
            # Decision D2 (user): pausing the music must KEEP the video window,
            # while the player disappearing should still close it. Those are two
            # different situations that both surface as "no session to follow":
            #
            #   (a) paused  -- the same track is still listed, just not Playing
            #                  (the whitelist's require_playing rejects it, so
            #                  pick_session returns None). Keep the window.
            #   (b) gone    -- the track is no longer in any session (player
            #                  closed, or the user switched to another app).
            #                  Tear the window down.
            #
            # Collapsing both into "None -> stop()" is what used to close the
            # window the moment the user hit pause -- AND, measured on the real
            # chain, the moment a song CHANGES: during the switch there is a
            # brief gap where the old track has left SMTC but the new one has
            # not been reported yet. `_current_still_listed` then returns False
            # for one or two polls and the old code closed the window, which the
            # user saw as "closing the window and reopening it on every song
            # change". So a disappearing session only closes the window after a
            # confirmation period of several polls (NO_SESSION_GRACE_POLLS).
            if self.current is not None and self._current_still_listed(sessions):
                if not self._video_paused:
                    self.log("音乐已暂停 -> 保留窗口（冻结画面）")
                    self._video_paused = True
                    # Freeze the picture too. Without this the video keeps
                    # advancing while the audio is paused, so resuming would
                    # leave the picture ahead by the whole paused duration.
                    self.player.set_property("pause", True)
                self._no_session_streak = 0
                return

            if self.current is not None:
                self._no_session_streak += 1
                if self._no_session_streak < NO_SESSION_GRACE_POLLS:
                    # Probably the brief gap between songs -- wait a few more
                    # polls before tearing the window down.
                    if self._no_session_streak == 1:
                        self.log("（暂时没有可跟随会话，先保留窗口确认中…）")
                    return
                self.log("没有白名单内的可跟随会话 -> 停止视频")
                self.player.stop()
                with self._lock:
                    self.current = None
                    self.current_candidate = None
                self._video_paused = False
                self._no_session_streak = 0
            return

        key = TrackKey.from_session(session)
        with self._lock:
            same = key == self.current
        if same:
            # Same song. If we had frozen the video for a pause, undo it: the
            # track is Playing again (otherwise pick_session would have
            # rejected it), so the picture must move again.
            if self._video_paused:
                self.log("音乐已恢复播放 -> 恢复画面")
                self._video_paused = False
                self.player.set_property("pause", False)
            return  # same song, nothing else to do

        self.log(f"检测到切歌: {session.title} - {session.artist} "
                 f"({session.duration_sec:.0f}s) [{session.app_id}]")
        # A5: remember when we detected this song, so _switch_to can measure
        # how long the whole lookup+resolve+start takes and compensate for it.
        self._detected_at = time.monotonic()
        # A new song must never appear frozen. mpv keeps `pause` across
        # `loadfile`, so if the previous track was pause-frozen we have to lift
        # it explicitly or the next video would sit on a still frame.
        if self._video_paused:
            self._video_paused = False
            self.player.set_property("pause", False)
        with self._lock:
            self.current = key
            self._pending = key

        # Hand the slow work (lookup + resolve + spawn) to a worker.
        if self._worker and self._worker.is_alive():
            # A previous lookup is still running; it will see it is stale and
            # discard its result, so we simply start the new one.
            pass
        self._worker = threading.Thread(
            target=self._switch_to, args=(session, key), daemon=True
        )
        self._worker.start()

    def _switch_to(self, session: NowPlaying, key: TrackKey) -> None:
        """Find a PV for this session and start playing it (runs in a thread)."""
        try:
            result = self.matcher.match(
                title=session.title,
                artist=session.artist,
                duration_sec=session.duration_sec,
                prefer_search=self.prefer_search,
            )
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            self.log(f"  ✗ 匹配异常: {exc}")
            return

        # If the song changed again while we were looking, abandon this result.
        with self._lock:
            if self._pending != key:
                self.log("  (匹配完成时已切歌，丢弃该结果)")
                return
        self.last_result = result

        # `best` is None when nothing scored well enough -- playing a random
        # unrelated video is worse than playing nothing.
        best = result.best
        if best is None:
            self.log(f"  ✗ 未找到匹配的 PV（{result.rejected_reason}）")
            self.player.stop()
            self.current_candidate = None
            return

        self.log(f"  → 匹配到 {len(result.candidates)} 个候选，选中:")
        self.log(f"     {best.describe()}")

        stream = None
        # Try every candidate, not just the top few. Measured: VocaDB sometimes
        # links a PV that no longer resolves ("Unable to extract initial state"
        # for a deleted/restricted video), and stopping there would fail a song
        # that has perfectly good alternatives further down the list.
        for alt in result.candidates:
            # Do not fall back to candidates that are themselves poor matches;
            # the point of trying more is to survive a dead link, not to lower
            # the quality bar.
            if alt.score < MIN_ACCEPTABLE_SCORE:
                continue
            try:
                stream = resolve_stream_url(alt.url, want="video")
                best = alt
                break
            except ResolveError as exc:
                self.log(f"     取流失败({alt.platform}): {str(exc)[:90]}")

        if stream is None:
            self.log(f"  ✗ {len(result.candidates)} 个候选均取流失败")
            self.player.stop()
            self.current_candidate = None
            return

        # Enforce the one-window invariant before showing a new video, but ONLY
        # when we can name the process to protect. If our mpv is still starting
        # (pid is None), a kill here would destroy the launch in progress -- that
        # is exactly what produced the "big window, then a small window" the
        # user reported, so we skip it in that case.
        own_pid = self.player.pid
        if own_pid:
            strays = self.player.kill_stray_windows(keep_pid=own_pid)
            if strays:
                self.log(f"  · 清理了 {strays} 个多余视频窗口")

        with self._lock:
            if self._pending != key:
                self.log("  (取流完成时已切歌，丢弃)")
                return
            # Guard against the SAME song being started twice by two workers.
            # Measured 2026-10-07: when SMTC briefly dropped the player (汽水
            # disappearing for one poll during a track change) the follower saw
            # the same song as a "new" switch a second time and started a second
            # worker -- producing two "已开始播放" lines, two parallel alignments
            # and TWO mpv windows for one song.
            if self._started_key == key and self.player.running:
                self.log("  (这首歌已在播放，忽略重复启动)")
                return
            self._started_key = key

        # --- alignment ---------------------------------------------------
        # Two offsets matter:
        #   coarse: the audio is already partway through, so the video must
        #           start there instead of at 0:00.
        #   fine:   different edits have different intros.
        #
        # IMPORTANT (measured): SMTC's `position_sec` is NOT reliable for every
        # player. 汽水音乐 reported a constant 0.2s across 16 seconds of
        # sampling while a 200s track played. So the coarse start is chosen as:
        #
        #   1. (user-suggested, preferred) t2 - t1: we know exactly when the
        #      song switch was DETECTED (t1 = _detected_at, set in step()) and
        #      we are about to start the video (t2 = now). The music has been
        #      playing for roughly (t2 - t1) seconds while we looked it up and
        #      resolved the stream. If the PV and the song have the same
        #      duration (the common case), starting the video at (t2 - t1) is
        #      correct without any history or SMTC position.
        #   2. position is reliable -> use SMTC position + elapsed switch.
        #   3. otherwise -> A5 rolling average of past switch delays.
        #
        # Either way the video starts mid-track instead of at 0:00, and fine
        # alignment corrects the remainder afterwards.

        # Coarse start, in order of trustworthiness.
        #
        # ORDER CHANGED 2026-10-07 (user: "粗对齐生效了，但时间不对。已经播放到
        # 1分多，粗对齐才到20秒左右"):
        #   The t2-t1 timer used to be FIRST. It can only ever measure how long
        #   OUR lookup took, never how long the user had already been listening
        #   before we noticed the song -- so it is systematically TOO SMALL. On a
        #   track the user had been playing for ~105s it produced 17.7s.
        #
        #   An ABSOLUTE position from the player is strictly better when the
        #   player reports one honestly, so that is tried first now. The timer
        #   remains the fallback for players whose position is unusable
        #   (measured: 汽水音乐 sometimes freezes at one value for 15s).
        position_reliable = False
        music_pos_now = 0.0
        if not self.auto_seek:
            rough = 0.0
            rough_source = "未开启 auto-seek"
        else:
            position_reliable, music_pos_now, rate = self._probe_music_position()
            # How long since the song was detected; used to age a sampled
            # position up to "now" and to describe the fallback.
            t2_t1 = self._switch_elapsed()
            # Age the SMTC sample forward: it was taken up to `probe_age`
            # seconds ago, and it advances at `rate` per second.
            probe_age = max(0.0, time.monotonic() - self._position_sampled_at)
            aged = music_pos_now + rate * probe_age

            if position_reliable and aged >= 1.0:
                # Absolute anchor: this is where the music really is.
                rough = aged
                rough_source = (f"SMTC 绝对位置 {music_pos_now:.1f}s"
                                f"（推进速率 {rate:.2f}x，+{probe_age:.1f}s 采样延迟）")
            elif t2_t1 > 0:
                # No usable absolute position: fall back to our own timing. This
                # is a LOWER BOUND (it misses pre-detection listening time), which
                # is exactly why it is no longer the first choice.
                rough = t2_t1
                rough_source = (f"实时 t2-t1（SMTC 位置不可用：速率 {rate:.2f}x）"
                                f" 耗时 {t2_t1:.1f}s")
            else:
                estimate = self.history.estimate_any(session.app_id)
                rough = estimate if estimate is not None else 0.0
                rough_source = (f"历史平均切换延迟 {estimate:.1f}s" if estimate is not None
                                else "无历史数据，从 0:00 起播")
            # Remember the anchor so fine alignment can convert "position inside
            # the PV" into an absolute target without trusting our own guess.
            self._music_anchor = (time.monotonic(), rough)
            self.log(f"  · 粗对齐依据: {rough_source}")

        # FINAL staleness check, immediately before we touch the player.
        #
        # Measured 2026-10-07: resolving + window cleanup can take 20-30s, and
        # the user may switch songs during it. The earlier check (after resolve)
        # was not enough -- the old song's worker still started playing AFTER
        # the new song had been detected, so the user saw the wrong video appear
        # first (and two mpv windows). Re-check here, where it actually counts.
        with self._lock:
            if self._pending != key:
                self.log("  (起播前已切歌，丢弃该结果)")
                return

        if not self.player.play_url(stream, start_sec=rough, mute=True):
            self.log("  ✗ mpv 启动失败")
            return

        # If a newer song arrived while we were starting, stop here: the newer
        # worker owns the window now. Without this the stale worker would ALSO
        # run fine alignment and seek the window the new song just took over.
        with self._lock:
            stale_now = self._pending != key
        if stale_now:
            self.log("  (起播瞬间已切歌，交由新任务接管)")
            return

        self.current_candidate = best
        self.log(f"  ✓ 已开始播放（起点 {rough:.1f}s，依据: {rough_source}）")

        # A5: record how long this switch took, so the next song can start
        # closer. `platform` comes from the selected candidate.
        if self._detected_at is not None:
            actual_delay = time.monotonic() - self._detected_at
            self.history.record(session.app_id, best.platform, actual_delay)
            next_est = self.history.estimate_any(session.app_id)
            if next_est is not None:
                self.log(f"  · 本次切歌耗时 {actual_delay:.1f}s（已记入历史，"
                         f"下次起播预跳 {next_est:.1f}s）")
            else:
                self.log(f"  · 本次切歌耗时 {actual_delay:.1f}s（已记入历史）")

        if not self.align:
            return

        # Fine alignment: find where in the PV the currently-playing audio is,
        # then SEEK the running video there.
        #
        # NOTE: an earlier version passed this value to mpv as `audio-delay`.
        # That was wrong and invisible in the logs: the video's own audio track
        # is muted, so shifting it does nothing to the picture. The screenshot
        # showed music at 00:19 while the video sat at 00:00:11 -- the computed
        # offset had never been applied to the video timeline at all.
        corrected = self._fine_align(session, best, position_reliable, rough)
        if corrected is None:
            return

        with self._lock:
            if self._pending != key:
                return
        if corrected > 0.5:
            self.player.seek(corrected)
            self.log(f"  ↻ 画面已校正到 {corrected:.1f}s")

    def _switch_elapsed(self) -> float:
        """Seconds since this song was detected (0 if we have no timestamp)."""
        if self._detected_at is None:
            return 0.0
        return time.monotonic() - self._detected_at

    def _pending_changed_since(self, since: float) -> bool:
        """Whether a NEW song was detected after the given monotonic time.

        Used to explain a rejected alignment: if the song changed while we were
        capturing, the capture and the PV slice belong to different songs and
        the mismatch is expected rather than a matching bug.
        """
        with self._lock:
            detected = self._detected_at
        return detected is not None and detected > since

    def _current_still_listed(self, sessions: list[NowPlaying]) -> bool:
        """Whether the track we are showing is still present in any session.

        Used to tell a PAUSE (the track is still listed, just not Playing, so
        pick_session rejected it) from the player being GONE (the track vanished
        entirely). D2 keeps the window for the former and closes it for the
        latter, so this distinction is load-bearing.

        `sessions` is the full list step() already fetched -- re-reading it here
        would cost another ~1s PowerShell launch on every poll.
        """
        if self.current is None:
            return False
        return any(
            TrackKey.from_session(s) == self.current for s in sessions
        )

    def _position_is_advancing(self, samples: int = 3, gap: float = 1.5) -> bool:
        """Whether SMTC's playback position actually moves for this player.

        Measured: some players (汽水音乐) keep reporting a constant position
        even while audio plays, so callers must not seek based on that value.
        """
        return self._probe_music_position(samples=samples, gap=gap)[0]

    def _probe_music_position(self, samples: int = 3, gap: float = 1.5
                              ) -> tuple[bool, float, float]:
        """Sample SMTC's position and decide whether it can be trusted.

        Returns (reliable, position_now, advance_rate).

        WHY the rate matters (measured 2026-10-07):
            "Reliable" is not a boolean property of a player -- the SAME player
            behaved differently at different times. 汽水音乐 reported a stuck
            105.0s for 15s and then jumped to 158.3s, yet for another track it
            reported a perfectly sane 108.2s. So we measure, per song:
              * does the value move at all, and
              * does it move at roughly real-time (rate ~1.0)?
            A stuck value has rate 0; a jumping value has an absurd rate. Only a
            value advancing at ~1x is usable as an absolute anchor.

        `position_now` is the LAST sample (best estimate of "where the music is
        right now"), and its timestamp is ~0s old, which is what makes it a
        usable anchor.
        """
        seen: list[tuple[float, float]] = []
        for _ in range(samples):
            s = pick_session(read_sessions(), prefer_app=self.prefer_app)
            if s is None:
                break
            seen.append((time.monotonic(), s.position_sec))
            if len(seen) < samples:
                time.sleep(gap)
        if not seen:
            self._position_sampled_at = time.monotonic()
            return (False, 0.0, 0.0)
        # Timestamp the last sample so callers can age it forward to "now".
        self._position_sampled_at = seen[-1][0]
        if len(seen) < 2:
            return (False, seen[-1][1], 0.0)

        first_t, first_p = seen[0]
        last_t, last_p = seen[-1]
        dt = last_t - first_t
        if dt <= 0:
            return (False, last_p, 0.0)
        rate = (last_p - first_p) / dt
        # Accept only a position that advances at roughly wall-clock speed.
        # 0.5..1.5 tolerates seek jitter and clock granularity while rejecting
        # both a frozen value (0.0) and a wild jump (e.g. 106s in 15s = 7x).
        reliable = 0.5 <= rate <= 1.5
        return (reliable, last_p, rate)

    def _fine_align(self, session: NowPlaying, candidate: Candidate,
                    position_reliable: bool, rough_sec: float) -> float | None:
        """Find where in the PV the currently-playing audio is.

        Returns the position in seconds to seek the video to, or None when we
        cannot establish it confidently. Refusing is deliberate: a wrong seek
        is worse than none, and unrelated audio still produces a sharp-looking
        correlation peak (measured), so a peak-margin check is enforced.

        SPEED (user requirement, 2026-10-07: "精对齐太慢了，歌都播完了还没完成"):
            The previous version downloaded PV[rough-60, rough+60] = 120s of
            audio and took 39-75s per song, so alignment routinely finished
            after the song had ended. Two changes fix that:

            1. NARROW WINDOW. We already know roughly where the music is
               (rough_sec, from the real-time t2-t1 coarse estimate, which is
               accurate to a few seconds). So we only need PV audio covering
               the capture plus a margin for intro/edit differences:
               [rough - ALIGN_WINDOW_BEFORE, + ALIGN_WINDOW_LEN]. Downloading
               ~30s instead of 120s cuts the dominant cost ~4x.
            2. PARALLEL capture + download. They are independent (one records
               the sound card, the other fetches from the network), so running
               them concurrently means we pay max(capture, download) instead of
               the sum. Measured: the download was the bottleneck, not capture.

        WHY a local window rather than scanning the whole track (measured):
            The old path downloaded the PV's ENTIRE audio and ran
            locate_in_track() over it. On a real song the same audio pair that
            estimate_delay() judged trustworthy (margin 1.45, offset +10.30s)
            was REJECTED by locate_in_track() (margin 1.16): with a long track
            the runner-up peak comes from the WHOLE curve, so repeats keep the
            margin under the 1.35 threshold and follow never fine-aligns.
            estimate_delay() only looks at a +/-max_lag window around the
            expected position, where the runner-up is genuinely comparable.

        TIMING (important): capture plus download take seconds, during which the
        music keeps playing. The correlated position therefore refers to where
        the track was when the CAPTURE STARTED, not where it is now. We measure
        the elapsed wall time and add it back, so the seek lands on the music's
        present position instead of being stale.
        """
        import threading

        from align import estimate_delay, extract_audio_track
        from capture import record

        scratch = STATE_DIR / "_align_work"
        scratch.mkdir(parents=True, exist_ok=True)

        # Window placement, and WHY it starts at (or near) 0.
        #
        # The coarse estimate can be WRONG BY AN UNKNOWN AMOUNT IN ONE
        # DIRECTION. Our timer (t2-t1) only measures how long the lookup took,
        # so it never exceeds the true position -- it is a LOWER BOUND, and the
        # gap is however long the user had already been listening before we
        # noticed. Measured: coarse said 17.7s while the music was ~105s in
        # (gap 87s). No heuristic can guess that gap, and a narrow window around
        # the anchor then EXCLUDES the truth -- the correlation latches onto a
        # wrong local peak and the seek lands somewhere worse, which is the
        # user's report "精对齐反而加大了偏差".
        #
        # When the anchor is a RELIABLE absolute position (SMTC advanced at ~1x)
        # we can trust it and keep the window tight, which is fast. When it is
        # only the timer lower bound, the window must reach back far enough to
        # contain the truth, so it starts at 0. Correlation over ~200s costs a
        # few seconds, which is an acceptable price for actually being right.
        anchor = max(0.0, rough_sec)
        if position_reliable:
            # Trusted absolute position: a tight window is both correct and fast.
            win_start = max(0.0, anchor - ALIGN_WINDOW_BEFORE)
            win_len = min(ALIGN_WINDOW_BEFORE + ALIGN_WINDOW_AFTER, 600.0)
        else:
            # Lower-bound anchor (our own timer, or nothing). The truth is at or
            # AFTER the anchor and cannot exceed the TRACK LENGTH, so search the
            # whole track head. Bounding by the session duration (rather than by
            # the anchor) is what makes the true position reachable -- anchoring
            # the window on the anchor is what excluded it and made fine
            # alignment move the video FURTHER from the music.
            track_len = max(0.0, float(getattr(session, "duration_sec", 0.0) or 0.0))
            upper = track_len if track_len > 0 else max(anchor + ALIGN_WINDOW_AFTER,
                                                        ALIGN_WINDOW_BEFORE)
            win_start = 0.0
            win_len = min(upper + ALIGN_WINDOW_AFTER, MAX_ALIGN_WINDOW_BEFORE, 600.0)

        live_path = scratch / "live.wav"
        track_path = scratch / "track.wav"
        captured: dict[str, object] = {}
        downloaded: dict[str, object] = {}

        def do_capture() -> None:
            try:
                captured["path"] = record(live_path, seconds=ALIGN_CAPTURE_SEC)
                # Timestamp the END of the capture: the absolute alignment
                # formula needs to know how long ago the captured audio played.
                captured["t_end"] = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                captured["error"] = exc

        def do_download() -> None:
            # Retry: bilibili's CDN intermittently fails a ranged request
            # (measured 2026-10-07: an extraction that succeeds in 2.6s on one
            # attempt returned nothing on another, for the SAME url and window).
            # A single transient failure used to cost the whole song its
            # alignment, which is the feature the user cares about most.
            last_err = ""
            for attempt in range(3):
                try:
                    if attempt == 0:
                        self.log(f"  · 下载 PV 音轨片段 [{win_start:.0f}s, +{win_len:.0f}s]…")
                    else:
                        self.log(f"    · 片段下载重试 {attempt}/2…")
                    stream = resolve_stream_url(candidate.url, want="audio")
                    got = extract_audio_track(
                        stream, track_path,
                        start=win_start, duration=win_len,
                        timeout=120,
                    )
                    if got:
                        downloaded["path"] = got
                        return
                    last_err = "提取返回空"
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{type(exc).__name__}: {exc}"
                time.sleep(1.5 * (attempt + 1))
            downloaded["error"] = last_err or "unknown"

        try:
            t0 = time.monotonic()
            self.log(f"  · 采集 + 下载并行进行（窗口 {win_len:.0f}s）…")
            th_cap = threading.Thread(target=do_capture, daemon=True)
            th_dl = threading.Thread(target=do_download, daemon=True)
            th_cap.start()
            th_dl.start()
            th_dl.join(timeout=150)
            th_cap.join(timeout=60)

            live = captured.get("path")
            track = downloaded.get("path")
            if not live:
                self.log("    · 采集不可用，跳过校正")
                return None
            if not track:
                # Report WHY instead of a bare "failed": the failure mode was
                # previously invisible, which made transient CDN errors look
                # like a broken alignment implementation.
                why = downloaded.get("error") or "未知原因"
                self.log(f"    · PV 音轨片段提取失败（{why}），跳过校正")
                return None

            # The capture started at t0; the music has advanced by however long
            # the SLOWER of the two jobs took (they ran concurrently), because
            # that is when we actually finish and are about to seek.
            elapsed = time.monotonic() - t0
            t_capture_end = float(captured.get("t_end") or (t0 + ALIGN_CAPTURE_SEC))
            self.log("  · 互相关定位（局部窗口）…")
            result = estimate_delay(live, track, max_lag_sec=ALIGN_MAX_LAG_SEC)
            self.last_alignment = result
            if not result.trustworthy:
                # A rejected correlation has two very different causes, and the
                # message must not hide which one happened:
                #   (a) the PV really is a different edit/recording, or
                #   (b) the user switched songs while we were capturing, so the
                #       capture holds song A while the track slice is song B.
                # Measured 2026-10-07: same-source loopback capture correlates
                # at margin 2.7-3.0 EVEN at 1/3 the source level, so a low
                # capture level is NOT itself a reason to reject -- meaning a
                # margin near 1.0 really does indicate different audio.
                same_song = (self.current is not None
                             and not self._pending_changed_since(t0))
                if not same_song:
                    self.log(f"    · {result.note}")
                    self.log("    · （采集期间已切歌，本次对齐作废）")
                else:
                    self.log(f"    · {result.note}")
                return None

            # delay_sec is where the capture sits INSIDE the downloaded window,
            # i.e. the PV position whose audio equals what the capture heard.
            position_in_pv = win_start + result.delay_sec

            # ABSOLUTE TARGET (rewritten 2026-10-07; user: "精对齐反而加大了偏差").
            #
            # Old formula:  target = position_in_pv + elapsed
            # It treats `position_in_pv` as if it already were the music's
            # position, then only adds our own processing time. That is only
            # valid when the coarse start was right -- and when it was wrong by
            # ~87s the "correction" merely nudged a wrong number, making the
            # error LOOK worse. It also silently mixed two different things:
            # "where in the PV this audio is" and "where the video should go".
            #
            # The correlation gives an absolute measurement of the audio, so no
            # guess is needed. We know:
            #   * when the capture ended (t_capture_end),
            #   * that the capture's own audio sat at PV position_in_pv,
            #   * that the capture lasted ALIGN_CAPTURE_SEC and finished
            #     `elapsed_after_capture` seconds ago.
            # Therefore the music is NOW at:
            #   position_in_pv + ALIGN_CAPTURE_SEC + elapsed_after_capture
            elapsed_after_capture = max(0.0, time.monotonic() - t_capture_end)
            target = position_in_pv + ALIGN_CAPTURE_SEC + elapsed_after_capture

            self.log(f"    · {result.note}")
            self.log(f"    · 片段起点 {win_start:.1f}s + 偏移 {result.delay_sec:+.1f}s"
                     f" = 采集音频在 PV {position_in_pv:.1f}s")
            self.log(f"    · 采集 {ALIGN_CAPTURE_SEC:.0f}s 后再过 "
                     f"{elapsed_after_capture:.1f}s => 音乐现在位于 {target:.1f}s")
            # Sanity check against the coarse anchor. The tolerance depends on
            # how much the anchor can be trusted:
            #   * reliable (absolute) anchor -> the two should agree closely;
            #     a big gap means the correlation latched onto a repeat.
            #   * lower-bound anchor -> the truth may legitimately be far ahead
            #     of the anchor (measured: anchor 17.7s, truth 126s), so only a
            #     result BEHIND the anchor is suspect.
            if position_reliable:
                tolerance = max(30.0, ALIGN_WINDOW_BEFORE + ALIGN_WINDOW_AFTER)
                if abs(target - anchor) > tolerance:
                    self.log(f"    · 与粗对齐 {anchor:.1f}s 相差过大"
                             f"（{target - anchor:+.1f}s），可能匹配到重复段落，放弃校正")
                    return None
            elif target < anchor - ALIGN_WINDOW_BEFORE:
                # The anchor is a lower bound, so a result well BEHIND it means
                # the correlation found the wrong thing.
                self.log(f"    · 结果 {target:.1f}s 早于下界锚点 {anchor:.1f}s 过多，"
                         f"可能匹配到重复段落，放弃校正")
                return None
            return target
        except Exception as exc:  # noqa: BLE001 - alignment must never kill the loop
            self.log(f"    · 对齐失败（{exc}），跳过校正")
            return None

    # ---------------- loop ----------------

    def run(self, duration_sec: float = 0.0) -> None:
        """Main loop. duration_sec=0 means run until interrupted."""
        self.log("跟随中… (Ctrl+C 退出)")
        if not find_cookies():
            self.log("⚠ 未找到 cookies.txt —— B站等需要登录的源会取流失败")
        # Clear any mpv left over from a previous run. Safe to do here because
        # we own no mpv yet, so anything present is genuinely stale.
        killed = self.player.kill_stray_windows(allow_when_unknown=True)
        if killed:
            self.log(f"· 清理了 {killed} 个残留视频窗口")
        started = time.time()
        try:
            while True:
                self.step()
                if duration_sec and (time.time() - started) >= duration_sec:
                    break
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            self.log("收到中断")
        finally:
            if self._worker and self._worker.is_alive():
                self._worker.join(timeout=15)
            self.player.stop()
            self.log("已停止")


# ---------------- CLI ----------------

def cmd_probe(args: argparse.Namespace) -> int:
    """Show what SMTC sees and what we would match -- no playback."""
    sessions = read_sessions()
    if not sessions:
        print("(没有 SMTC 会话 —— 先让某个播放器放首歌)")
        return 1
    print("当前 SMTC 会话:")
    for s in sessions:
        print(f"  - {s.summary()}")
    chosen = pick_session(sessions, prefer_app=args.app)
    if not chosen:
        print("\n→ 没有可跟随的会话")
        return 1
    print(f"\n→ 将要跟随: {chosen.summary()}")

    m = Matcher()
    r = m.match(chosen.title, chosen.artist, chosen.duration_sec,
                prefer_search=args.prefer_search)
    print(f"\n匹配结果（{r.note or 'VocaDB'}）:")
    if not r.candidates:
        print("  (无候选)")
        return 1
    for i, c in enumerate(r.candidates[:5], 1):
        print(f"  {i}. {c.describe()}")
        print(f"     {c.url}")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    """Resolve and play one song (by SMTC or by explicit title)."""
    if args.title:
        title, artist, dur = args.title, args.artist, args.duration
    else:
        sessions = read_sessions()
        chosen = pick_session(sessions, prefer_app=args.app)
        if not chosen:
            print("没有可跟随的 SMTC 会话，且未指定 --title")
            return 1
        title, artist, dur = chosen.title, chosen.artist, chosen.duration_sec
        print(f"SMTC: {chosen.summary()}")

    m = Matcher()
    r = m.match(title, artist, dur, prefer_search=args.prefer_search)
    print(f"匹配: {len(r.candidates)} 个候选 ({r.note or 'VocaDB'})")
    if not r.candidates:
        return 1
    best = r.best
    assert best is not None
    print(f"选中: {best.describe()}\n      {best.url}")

    try:
        stream = resolve_stream_url(best.url, want="video")
    except ResolveError as exc:
        print(f"取流失败: {exc}")
        return 1
    print(f"直链: {stream[:100]}...")

    ctl = MpvController(mute=True)
    if not ctl.play_url(stream):
        print("mpv 启动失败")
        return 1
    print(f"播放 {args.seconds}s (pid={ctl.proc.pid})")
    time.sleep(args.seconds)
    ctl.stop()
    print("完成")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="follow.py",
        description="跟随本机播放器正在播放的音乐，静音播放对应的 MV/PV 画面",
    )
    sub = p.add_subparsers(dest="cmd")

    pf = sub.add_parser("follow", help="持续跟随（默认）")
    pf.add_argument("--app", default="", help="只跟随名称含此字串的播放器")
    pf.add_argument("--duration", type=float, default=0.0, help="运行秒数，0=直到中断")
    pf.add_argument("--auto-seek", dest="auto_seek", action="store_true", default=True,
                    help="按 SMTC 位置 + 历史延迟预跳转，修正「视频从头播但音乐已在中间」的大偏移（默认开）")
    pf.add_argument("--no-auto-seek", dest="auto_seek", action="store_false",
                    help="关闭自动预跳转（从 0:00 起播）")
    pf.add_argument("--align", dest="align", action="store_true", default=False,
                    help="额外做互相关微调（需采集系统输出，切歌会慢约 10s）")
    pf.add_argument("--no-align", dest="align", action="store_false",
                    help="关闭互相关微调")
    pf.add_argument("--prefer-search", action="store_true",
                    help="优先用搜索而非 VocaDB（适合非 V曲）")
    pf.set_defaults(func=lambda a: _run_follow(a))

    pp = sub.add_parser("probe", help="只查看 SMTC + 匹配结果，不播放")
    pp.add_argument("--app", default="")
    pp.add_argument("--prefer-search", action="store_true")
    pp.set_defaults(func=cmd_probe)

    po = sub.add_parser("once", help="播一首就退出")
    po.add_argument("--title", default="", help="不指定则用 SMTC 当前曲目")
    po.add_argument("--artist", default="")
    po.add_argument("--duration", type=float, default=0.0)
    po.add_argument("--app", default="")
    po.add_argument("--seconds", type=float, default=8.0)
    po.add_argument("--prefer-search", action="store_true")
    po.set_defaults(func=cmd_once)

    return p


def _run_follow(args: argparse.Namespace) -> int:
    # Single-instance guard. Two followers means two mpv windows fighting over
    # one command file, which the user experienced as "every new song opens 3
    # windows and the stale ones never close".
    from single_instance import AlreadyRunning, SingleInstance

    lock = SingleInstance()
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"✗ 已经有一个跟随进程在运行（PID {exc.pid}）。")
        print("  同时运行多个会导致多个视频窗口互抢。")
        print("  如确认它已卡死，请结束该进程，或删除 state\\.follow.lock 后重试。")
        return 1

    try:
        f = Follower(
            matcher=Matcher(),
            prefer_app=args.app,
            auto_seek=args.auto_seek,
            prefer_search=args.prefer_search,
            align=args.align,
        )
        if not args.auto_seek:
            f.log("提示: 已关闭自动预跳转（--no-auto-seek），视频会从 0:00 开始；"
                  "若音乐已播到中间，画面会明显滞后。")
        if args.align:
            f.log("提示: 已开启 --align 互相关微调，每次切歌会慢约 10s+")
        f.run(duration_sec=args.duration)
    finally:
        lock.release()
    return 0


def main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    if not argv:
        argv = ["follow"]
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
