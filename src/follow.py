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
import json
import os
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
from player import (                                           # noqa: E402
    MANUAL_FILE,
    MANUAL_TMP,
    MpvController,
    ResolveError,
    find_cookies,
    resolve_stream_url,
)
from smtc import NowPlaying, pick_session, read_sessions      # noqa: E402
from delay_history import DelayHistory                        # noqa: E402
# Manual calibration + inheritance (tasks 2/3). Imported at module level so a
# broken import is an immediate, loud failure at startup rather than a silent
# "manual alignment does nothing" at runtime.
from align_calib import (                                      # noqa: E402
    clamp_offset,
    consume_pending_offsets,
    make_track_key,
    set_manual_offset as calib_set_manual_offset,
)
from align_inherit import decide_inherited_offset              # noqa: E402

# Imported lazily inside methods to keep startup fast, but the type is needed at
# class-definition time for the annotation below.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from align import AlignmentResult

STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# Where the follower publishes "what it is following right now", so the control
# window (tools/align_control.py) can show the song, the residual error and the
# TrackKey it must calibrate against. A separate file from the mpv status file
# because mpv knows nothing about track keys, matched candidates or residuals.
NOW_FILE = STATE_DIR / "_mvm_now.json"

# How long a manual-offset sidecar value may sit unchanged before we stop
# treating it as "the user just pressed a key". The hotkeys write the CUMULATIVE
# offset on every press; we poll it and persist new values into the calibration
# store. Without a freshness guard a stale sidecar from a previous song would be
# re-recorded forever.
MANUAL_SIDECAR_POLL_SEC = 1.0

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
#
# NOTE (session 8): this is now only the REQUESTED capture duration. The
# alignment formula no longer uses it as a fact -- `capture.record()` returns a
# `Recording` whose `covered_sec` is the audio length actually written
# (frames / rate). See `_fine_align` and capture.Recording for why the nominal
# value was wrong often enough to matter.
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

# ---------------- closed-loop drift correction (session 8, TODO.md B2) ------
#
# THE PROBLEM. Fine alignment runs exactly ONCE per song (see the single call in
# `_switch_to`). After that seek nothing ever re-checks the two playheads, so any
# residual bias in the fine-alignment target -- a slightly wrong capture length,
# a seek that landed a few tens of ms off, a rounding slip -- persists for the
# whole song. The user's report is specifically this: a PERSISTENT sub-second
# offset rather than a growing one.
#
# THE BASELINE THESE NUMBERS COME FROM (NOTES.md §1, "关键数字与基线"):
#     | 同步偏差 | ±1s 内 | 实测 0.2–0.6s。超过 3s 即异常 |
# The measured steady state on 网易云 is 0.2-0.6s of offset, and this session's
# target is to pull a >0.5s offset back under 0.25s. So:
#
#   * DEADBAND = 0.25s. Deliberately at the LOW end of the normal 0.2-0.6s
#     band: correcting anything at or below normal jitter would make the picture
#     visibly twitch (a seek is a visible jump), i.e. the "fix" would be worse
#     than the drift. 0.25s sits below the smallest offset a user notices here
#     (0.2s) plus measurement noise, while still being tight enough to hold the
#     offset inside the 0.2-0.6s band instead of letting it pile up.
#   * PERIOD = 6.0s. The readbacks (SMTC via a PowerShell launch ~0.5-1s per
#     sample, mpv via a 0.5s status file) are not free, and the drift we are
#     chasing is sub-second over minutes, not per second. 6s keeps the loop
#     cheap while still catching drift long before it becomes visible. Kept
#     above the 5s floor suggested in task-1 and well under its 10s ceiling.
#   * MIN_APPLY = 0.10s. Do not even attempt a seek for less than this: the
#     status file's own 0.5s granularity means we cannot reliably resolve it,
#     and mpv's reply to a tiny seek is indistinguishable from noise.
#   * COOLDOWN = 2 periods. After an actual correction, wait before measuring
#     again: the seek takes up to 0.2s to travel the command-file poll and the
#     playhead keeps moving, so an immediate re-measure would read the OLD
#     position and "correct" the same error twice, overshooting the other way.
ALIGN_LOOP_PERIOD_SEC = 6.0
ALIGN_LOOP_DEADBAND_SEC = 0.25
ALIGN_LOOP_MIN_APPLY_SEC = 0.10
ALIGN_LOOP_COOLDOWN_SEC = 2 * ALIGN_LOOP_PERIOD_SEC
# Consecutive unusable samples (SMTC unreliable, readback unavailable, or the
# residual sign flipping) before we give up for this song. Measured motivation:
# 汽水音乐 freezes its reported position, so a loop that trusted it would seek
# forever against a stale number. Three strikes is enough to conclude "no usable
# feedback for this player" without abandoning a song after one hiccup.
ALIGN_LOOP_MAX_STRIKES = 3
# Never correct more than this in one step. A single wild sample (e.g. SMTC
# jumping 50s) should be refused, not applied -- a 3s+ "offset" is classified as
# an anomaly rather than drift (NOTES §1), so corrections stay small by design
# and anything larger is reported instead of acted on.
ALIGN_LOOP_MAX_STEP_SEC = 2.0
# Recovering from a GENUINE desync (see closed_loop_step): a large residual is
# acted on only once a second sample agrees with it, which separates a real
# 30s desync (consistent reading) from a single bad sample (does not repeat).
#
# WHY THIS EXISTS (measured live 2026-10-08): the previous song's fine alignment
# finished AFTER the track had changed, leaving the picture 29.3s off. The old
# rule counted that as an anomaly three times and then DISABLED the loop for the
# whole song, so the user watched a half-minute-desynced picture with no
# recovery. Confirming the reading turns that dead end into an absolute resync.
ALIGN_LOOP_RESYNC_AGREE_SEC = 3.0      # how close two samples must be to confirm
# Hard ceiling on a confirmed resync. Beyond this the offset is more likely a
# WRONG VIDEO than a timing problem, and seeking would hide the real fault
# (铁律 15: 宁可不放，也不放错的). Half a minute covers any plausible drift or
# stale-landing scenario while staying far below "wrong track" territory.
ALIGN_LOOP_RESYNC_MAX_STEP_SEC = 30.0

# Speed-based correction (session 9). See closed_loop_step for the full WHY.
#
# GAIN: `speed = 1 + residual * GAIN`. With GAIN = 0.05 the picture gains back
# a 1.0s lag at 5% speed-up, i.e. over ~20s -- about 3 probe periods -- which
# is smooth enough to be imperceptible while fast enough to keep the lag from
# growing. Higher gain corrects faster but makes the speed change noticeable.
ALIGN_LOOP_SPEED_GAIN = 0.05
# Clamp the speed change to +-5%. Beyond that the picture is visibly sped up
# or slowed down, which looks worse than a small residual. A 5% bound still
# corrects a 1s lag over ~20s.
ALIGN_LOOP_SPEED_DELTA_MAX = 0.05
# Below this speed change the correction is meaningless: a 0.1% speed tweak
# cannot be perceived nor measured. Report instead of acting.
ALIGN_LOOP_MIN_SPEED_STEP = 0.005
# Minimum song age before the loop will act. Fine alignment can take 15-25s
# (NOTES §1) and runs on the worker thread; correcting during it would fight the
# alignment seek. This is a floor in addition to the explicit _pending/_lock
# ownership checks.
ALIGN_LOOP_WARMUP_SEC = 20.0
# How much of the mpv status file's staleness we tolerate when aging its
# position forward. mvm_control.lua rewrites it every 0.5s, so a sample older
# than a few seconds means the timer stalled and the value is not trustworthy.
ALIGN_LOOP_VIDEO_STALE_SEC = 3.0


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
        # ---------------- closed-loop drift correction state ----------------
        # When the loop last MEASURED a residual (monotonic). None until the
        # first sample of the current song.
        self._loop_last_probe: float | None = None
        # When the loop last APPLIED a correction. Enforces the cooldown so a
        # single error is not corrected twice (see ALIGN_LOOP_COOLDOWN_SEC).
        self._loop_last_apply: float | None = None
        # Consecutive samples we could not use (unreliable position, no
        # readback, sign flip). Reaching ALIGN_LOOP_MAX_STRIKES disables the
        # loop for the rest of the song.
        self._loop_strikes = 0
        # Separate budget for "the player gives me no usable position at all"
        # (frozen SMTC / unreadable status file). Kept apart from
        # `_loop_strikes`, which counts "the offset I computed looks absurd":
        # the two failures need different handling and must not mask each other.
        self._loop_unusable_streak = 0
        # Set once we have given up on this song, so the explanation is logged
        # once instead of every period.
        self._loop_gave_up = False
        # Previous LARGE residual, used to confirm a genuine desync before
        # acting on it (see closed_loop_step). None when the last reading was
        # normal-sized.
        self._loop_anomaly_prev: float | None = None
        # The playback speed currently applied by the drift corrector (e.g.
        # 1.02), or None when running at normal 1.0x. Reset to 1.0 when the
        # residual returns to the deadband; cleared when the song changes.
        self._loop_speed_applied: float | None = None
        # Which TrackKey the loop is currently serving. Any change resets all of
        # the above, so a new song can never inherit the old song's cooldown or
        # strike count.
        self._loop_key: TrackKey | None = None
        # Last residual we measured and last one we applied, exposed for the
        # control window / manual-align integration (task-2) and for tests.
        self.last_loop_error: float | None = None
        self.last_loop_applied: float | None = None
        # Set by the manual-alignment path (task-2) when the user pressed a
        # nudge key: a user-intent offset that must NOT be cancelled by the
        # corrector. Semantics agreed with task-2/task-3:
        #     closed loop corrects ALIGNMENT ERROR, manual keys express USER
        #     PREFERENCE, and the two are ADDED. The corrector therefore
        #     measures against (music position + manual offset), else it would
        #     immediately undo every nudge the user makes.
        self._manual_offset_sec = 0.0
        # ---------------- manual alignment wiring (session 8) ----------------
        # The manual offset INHERITED from the calibration store when this song
        # started (song level, else app level), or the absolute value a control
        # window button last requested. This is the BASELINE the mpv hotkey
        # sidecar is added to; see _poll_manual_inputs for the full model.
        self._manual_base_sec = 0.0
        # Last value read from the hotkey sidecar, so an unchanged file is not
        # re-recorded as a fresh nudge on every poll. The sidecar holds mpv's
        # per-song CUMULATIVE nudge.
        self._manual_sidecar_seen: float | None = None
        # TrackKey/app_id of the song the manual state belongs to, used to build
        # the calibration keys and to publish _mvm_now.json.
        self._manual_key: str = ""
        self._app_id: str = ""
        # When the sidecar was last polled, so we do not stat/read it on every
        # single step (POLL_INTERVAL_SEC is 2s; this is a cheap guard anyway).
        self._manual_polled_at: float = 0.0
        # Count of corrections applied this song -- reported at song end so the
        # user can see whether the loop actually did anything.
        self._loop_applied_count = 0

    # ---------------- closed loop: drift correction ----------------

    def set_manual_offset(self, offset_sec: float) -> None:
        """Record the user's manual alignment preference (task-2 integration).

        Kept on the Follower rather than read from disk on every probe so the
        corrector has no IO in its hot path, and so a test can set it directly.
        """
        self._manual_offset_sec = float(offset_sec)

    def _reset_loop_state(self, key: TrackKey) -> None:
        """Clear per-song loop state. Called when (and only when) the song changes."""
        self._loop_key = key
        self._loop_last_probe = None
        self._loop_last_apply = None
        self._loop_strikes = 0
        self._loop_unusable_streak = 0
        self._loop_gave_up = False
        self._loop_anomaly_prev = None
        self._loop_speed_applied = None
        self.last_loop_error = None
        self.last_loop_applied = None
        self._loop_applied_count = 0

    def measure_residual(self, video_position: float, music_position: float,
                         manual_offset: float = 0.0) -> float:
        """Signed misalignment: how far the PICTURE is ahead of the MUSIC.

        SIGN CONVENTION (fixed here, and relied on by the log and the tests):
            residual = video_position - (music_position + manual_offset)
            residual > 0  -> the picture is AHEAD of the music, so the video
                             must be seeked BACKWARDS (negative correction).
            residual < 0  -> the picture LAGS; seek forwards.

        `manual_offset` is the user's preference (task-2). Adding it to the music
        side means a manual nudge does NOT register as an error the loop would
        then "fix" away: after the user nudges the video +0.3s, the intended
        steady state is residual == 0 with the offset included.
        """
        return video_position - (music_position + manual_offset)

    def closed_loop_step(self, key: TrackKey, music_position: float,
                         video_position: float, usable: bool,
                         now: float | None = None) -> tuple[str, float | None]:
        """One iteration of the drift loop -- PURE w.r.t. the player.

        Returns (action, correction) where action is one of:
            "skip-deadband"  residual inside the deadband, nothing to do
            "skip-cooldown"  corrected recently, let the seek settle first
            "skip-warmup"    song too young (fine alignment may still be running)
            "skip-unreliable" the position feed is not trustworthy
            "skip-anomaly"   residual too large to be drift -> reported, not acted on
            "speed"          target PLAYBACK SPEED (e.g. 1.02) for smooth drift
                             correction -- the caller sets mpv's speed property
            "resync"         confirmed genuine desync -> caller must seek
            "correct"        legacy absolute seek correction (kept for callers
                             that have not migrated to speed)
            "gave-up"        too many unusable samples; loop disabled for this song

        WHY THIS IS A SEPARATE METHOD: the decision rules are the part worth
        testing, and they must be testable WITHOUT a real player or a real
        status file. Everything here is arithmetic plus the instance counters, so
        a test can feed a synthetic position sequence through it and assert the
        convergence -- which is exactly the "must fail on the old code" evidence
        this task requires (the old code has no loop to drive at all).

        The caller owns the seek and the logging, because only the caller knows
        which mpv/status file is live.
        """
        now = time.monotonic() if now is None else now
        if self._loop_key != key:
            self._reset_loop_state(key)

        if self._loop_gave_up:
            return ("gave-up", None)

        if not usable:
            # HARD REQUIREMENT (task-1 C): a player whose SMTC position freezes
            # must be SKIPPED, never "corrected" against a stale number. 汽水音乐
            # was measured holding one value for 15s (NOTES §1), which would
            # otherwise look like a huge, stable offset and produce a seek storm.
            return ("skip-unreliable", None)

        detected = self._detected_at
        if detected is not None and (now - detected) < ALIGN_LOOP_WARMUP_SEC:
            return ("skip-warmup", None)
        if self._loop_last_apply is not None and \
                (now - self._loop_last_apply) < ALIGN_LOOP_COOLDOWN_SEC:
            return ("skip-cooldown", None)

        residual = self.measure_residual(video_position, music_position,
                                         self._manual_offset_sec)
        self.last_loop_error = residual

        if abs(residual) <= ALIGN_LOOP_DEADBAND_SEC:
            # Inside the deadband: this is the normal, healthy case (NOTES §1
            # baseline 0.2-0.6s). Do NOTHING -- a seek here is a visible jump
            # that would make the picture worse than the residual it removed.
            self._loop_strikes = 0
            return ("skip-deadband", None)

        if abs(residual) > ALIGN_LOOP_MAX_STEP_SEC:
            # Too large to be ordinary drift. Two very different situations land
            # here, and treating them the same was a REAL bug found by live
            # testing on 2026-10-08:
            #
            #   (a) a single wild sample (SMTC jumping, a stale readback) --
            #       acting on it would seek the picture somewhere random, so it
            #       must be ignored;
            #   (b) a GENUINE desync, e.g. the previous song's fine alignment
            #       landing after the track had already changed. Measured live:
            #       residual -29.3s then -38.8s, i.e. the picture was half a
            #       minute off -- and the old rule counted strikes and then
            #       DISABLED the loop for the rest of the song, so the user
            #       watched a permanently desynced picture with no recovery.
            #
            # The discriminator is CONSISTENCY: a real desync reads roughly the
            # same on the next sample, while noise does not. So confirm the
            # large residual across consecutive samples and only then resync.
            # A resync is just a big correction; the caller's absolute formula
            # (`target = video_position + correction`) turns it into an absolute
            # seek, which is exactly what recovering from a 30s offset needs.
            if self._loop_anomaly_prev is not None and \
                    abs(residual - self._loop_anomaly_prev) <= ALIGN_LOOP_RESYNC_AGREE_SEC:
                self._loop_anomaly_prev = residual
                if abs(residual) > ALIGN_LOOP_RESYNC_MAX_STEP_SEC:
                    # Even a confirmed desync beyond this is more likely a wrong
                    # video than a timing problem: report, do not seek.
                    self._loop_strikes += 1
                    if self._loop_strikes >= ALIGN_LOOP_MAX_STRIKES:
                        self._loop_gave_up = True
                        return ("gave-up", None)
                    return ("skip-anomaly", residual)
                self._loop_last_apply = now
                self.last_loop_applied = -residual
                self._loop_applied_count += 1
                self._loop_strikes = 0
                return ("resync", -residual)

            # First large sample (or it disagreed with the previous one): wait
            # for confirmation instead of acting on what may be one bad read.
            self._loop_anomaly_prev = residual
            self._loop_strikes += 1
            if self._loop_strikes >= ALIGN_LOOP_MAX_STRIKES:
                # Repeatedly inconsistent large residuals: no stable reading to
                # act on, so stop trying for this song rather than seek blindly.
                self._loop_gave_up = True
                return ("gave-up", None)
            return ("skip-anomaly", residual)

        # A normal-sized residual clears the anomaly memory: the next large
        # reading must again be confirmed before it triggers a resync.
        self._loop_anomaly_prev = None

        # ---------------- speed-based correction (session 9) ----------------
        #
        # WHY SPEED, NOT SEEK, FOR ORDINARY DRIFT (user report 2026-10-08:
        # "本来比较对齐，调整后不对齐了"): every closed-loop correction used to
        # be a `seek`, and a seek is a VISIBLE JUMP. Worse, the seek landing was
        # only verified within +-1.0s (SEEK_VERIFY_TOLERANCE_SEC) while the
        # deadband is +-0.25s, so a "successful" correction routinely landed
        # 0.4-0.8s off -- the picture jumped AND the jump landed wrong. The
        # measured live correction sequence was
        #     -1.1, -0.7, -1.1, -0.9, -0.5, +0.3, -0.3, +0.1 ...
        # with 9 of 27 adjacent corrections in OPPOSITE directions: the loop was
        # sawtoothing, which is exactly what the user saw as "调整后不对齐".
        #
        # For sub-second drift the right response is to nudge the PLAYBACK SPEED
        # instead: mpv's `speed` property runs the picture at 1.0 * ratio, so a
        # speed of 1.01 makes the video advance 1% faster than the music and the
        # lag shrinks smoothly, with no visible jump. The speed is returned to
        # 1.0 once the residual is back inside the deadband.
        #
        # The gain is deliberately SMALL: the loop samples every 6s, and a
        # residual of r needs the picture to gain r seconds at a rate of
        # (speed - 1) seconds per second, i.e. roughly `speed = 1 + r / (3s)`
        # if we want to recover over ~3 probe periods. Clamped to a range that
        # keeps the motion imperceptible (ALIGN_LOOP_SPEED_MAX).
        #
        # SIGN (easy to get wrong -- an earlier draft did): `residual > 0`
        # means the picture is AHEAD of the music (measure_residual), so the
        # picture must SLOW DOWN, i.e. speed < 1.0. Hence `speed = 1 - r*GAIN`.
        #
        # PURE P-CONTROL NEVER FULLY CONVERGES (measured in the convergence
        # test): as the residual shrinks, the proportional speed difference
        # shrinks with it, so the catch-up rate goes to zero and the picture
        # hovers just OUTSIDE the deadband forever. The fix is a MINIMUM
        # correction floor: once we have decided to correct, the speed must
        # differ by at least ALIGN_LOOP_MIN_SPEED_STEP so the residual keeps
        # shrinking instead of asymptotically stalling. This is standard
        # proportional-with-floor control, and it is what lets the test (and a
        # real song) actually get back INSIDE the deadband.
        proportional = max(-ALIGN_LOOP_SPEED_DELTA_MAX,
                           min(ALIGN_LOOP_SPEED_DELTA_MAX,
                               -residual * ALIGN_LOOP_SPEED_GAIN))
        # A floor that preserves the SIGN of the correction: picture ahead ->
        # at least MIN_SPEED_STEP slower; picture behind -> at least MIN_SPEED_STEP
        # faster. Only applies when the proportional term is too small to matter.
        if abs(proportional) < ALIGN_LOOP_MIN_SPEED_STEP:
            proportional = (ALIGN_LOOP_MIN_SPEED_STEP
                            if proportional >= 0 else
                            -ALIGN_LOOP_MIN_SPEED_STEP)
        speed = 1.0 + proportional
        if abs(speed - 1.0) < ALIGN_LOOP_MIN_SPEED_STEP:
            # The drift is real but too small to compensate without an
            # imperceptible-yet-meaningless speed tweak. Report and do nothing;
            # the next probe re-evaluates.
            self._loop_strikes = 0
            return ("skip-deadband", None)

        # Enforce a cooldown between SPEED changes too: changing speed every 6s
        # is fine, but rapid oscillation between 1.01 and 0.99 would be visible
        # as pulsing. One cooldown period is enough because the speed change
        # itself is smooth.
        if self._loop_last_apply is not None and \
                (now - self._loop_last_apply) < ALIGN_LOOP_COOLDOWN_SEC:
            return ("skip-cooldown", None)

        self._loop_last_apply = now
        self.last_loop_applied = speed
        self._loop_applied_count += 1
        self._loop_strikes = 0
        return ("speed", speed)

    def _age_video_position(self, raw_position: float | None,
                            read_at: float, now: float,
                            staleness: float = 0.0) -> float | None:
        """Age a status-file position forward to `now`, or None if too stale.

        TWO ages must be added, and omitting either biases the measurement:

          1. `staleness` -- how old the VALUE already was when we read it. The
             Lua timer rewrites the file every 0.5s, so the number describes the
             playhead as of the last rewrite, not as of the read. This is
             measured from the file's mtime.
          2. `now - read_at` -- the time that has passed since our read.

        WHY `staleness` IS NOT OPTIONAL (found in live testing 2026-10-08):
            An earlier version added only (2). That systematically UNDER-
            estimated the video position by up to 0.5s, so the loop kept
            believing the picture lagged and pushed it forward again -- the
            measured correction sequence was
                -0.9, -1.3, -1.7, +0.3, -1.3, +0.3, -0.4, -0.6, -0.5, -1.0
            i.e. a nearly constant negative bias around -0.7s, while the true
            drift was only 0.4 s/min (0.13s over 19s). The loop was chasing its
            own measurement error and would have made the picture visibly twitch.
            The same bug was independently found and fixed in sync_probe.py.

        If the sample is older than ALIGN_LOOP_VIDEO_STALE_SEC the Lua timer has
        stalled and the value is worthless; refusing is better than correcting
        against it.
        """
        if raw_position is None:
            return None
        age = max(0.0, staleness) + max(0.0, now - read_at)
        if age > ALIGN_LOOP_VIDEO_STALE_SEC:
            return None
        return raw_position + age

    def _maybe_correct_drift(self, session: NowPlaying, key: TrackKey) -> None:
        """The periodic closed-loop probe. Safe to call on every poll.

        COOPERATION RULES (task-1 C -- "must not fight the switch / fine align"):
          * `self._pending != key` means a NEWER song has been detected: this
            song's video is about to be replaced, so correcting it is pointless
            and could land a seek on the next song's window during the handover.
          * `self._video_paused` means the picture is deliberately frozen while
            the music is paused. Seeking a paused player and comparing positions
            across a pause is meaningless (the music clock is stopped), so skip.
          * the caller only invokes this when `self.current == key` under the
            lock, i.e. the track is still the one being shown.
          * ALIGN_LOOP_WARMUP_SEC covers the fine-alignment window, which runs
            on the worker thread and issues its own seek.
        """
        now = time.monotonic()
        if self._loop_gave_up:
            return
        if self._video_paused:
            return
        with self._lock:
            if self._pending != key or self.current != key:
                return

        # Adopt the song here as well as in closed_loop_step: the "unusable"
        # path below returns without ever calling it, and carrying the previous
        # song's strike count over would silence the loop on a track that has
        # nothing wrong with it.
        if self._loop_key != key:
            self._reset_loop_state(key)

        if self._loop_last_probe is not None and \
                (now - self._loop_last_probe) < ALIGN_LOOP_PERIOD_SEC:
            return
        self._loop_last_probe = now

        if not self.player.running:
            return

        # --- music side: SMTC position, with the rate probe as the gate ---
        reliable, music_pos, rate = self._probe_music_position()
        if reliable:
            # `_probe_music_position` samples over `samples*gap` seconds, so age
            # the last sample forward to now; otherwise every probe would look
            # like a fixed lag equal to the sampling time.
            age = max(0.0, time.monotonic() - self._position_sampled_at)
            music_pos = music_pos + rate * age

        # --- video side: mpv time-pos from the status file ---
        #
        # The STALENESS of the file matters as much as the read delay: the Lua
        # timer rewrites it every 0.5s, so the number we get describes the
        # playhead as of the last rewrite. Reading the mtime BEFORE the position
        # keeps the two consistent (and can only over-state staleness, which is
        # the safe direction: it makes the loop less likely to act).
        #
        # `getattr` because a substituted player (tests, an external mpv) may
        # not implement status_file_age(); missing it degrades to the old
        # behaviour (0.0) instead of raising inside the poll loop.
        read_at = time.monotonic()
        age_reader = getattr(self.player, "status_file_age", None)
        staleness = age_reader() if callable(age_reader) else 0.0
        raw_video = self.player.get_position()
        video_pos = self._age_video_position(raw_video, read_at, time.monotonic(),
                                            staleness=staleness)

        usable = reliable and video_pos is not None
        if not usable:
            # HARD REQUIREMENT (task-1 C): a player whose reported position
            # freezes (汽水音乐 held one value for 15s, NOTES §1) must be SKIPPED.
            # Its stale number would look like a large, rock-steady offset and a
            # loop that trusted it would seek continuously against a fiction.
            #
            # Counted in its OWN budget (`_loop_unusable_streak`), separate from
            # the anomaly budget: "this player gives me no usable position" and
            # "the offset I computed looks absurd" are different faults and
            # sharing one counter would let one mask the other.
            self._loop_unusable_streak += 1
            if self._loop_unusable_streak == 1:
                reason = ("SMTC 位置不可靠" if not reliable else "读不到画面位置")
                self.log(f"  · 闭环校验跳过：{reason}"
                         f"（速率 {rate:.2f}x，不做任何调整）")
            if self._loop_unusable_streak >= ALIGN_LOOP_MAX_STRIKES:
                self._loop_gave_up = True
                self.log("  · 闭环校验本曲停用（连续无可用位置，避免误调）")
            return
        # A good sample clears the unusable budget.
        self._loop_unusable_streak = 0

        assert video_pos is not None
        action, correction = self.closed_loop_step(
            key, music_position=music_pos, video_position=video_pos,
            usable=True, now=now,
        )

        if action == "skip-warmup":
            # Not an error: alignment is likely still running. Roll the probe
            # back so the first real sample happens promptly once warm.
            self._loop_last_probe = None
            return
        if action == "skip-cooldown":
            return
        if action == "skip-deadband":
            residual = self.last_loop_error or 0.0
            self.log(f"  · 闭环校验：音乐 {music_pos:.1f}s vs 画面 {video_pos:.1f}s "
                     f"→ 偏差 {residual:+.1f}s，在死区（±{ALIGN_LOOP_DEADBAND_SEC:.2f}s）内，不调整")
            # Restore normal speed once the picture is back inside the deadband.
            # Without this, a speed that was set to chase a previous drift stays
            # in place and the picture OVERSHOOTS into the other direction --
            # the sawtooth pattern the user reported ("调整后不对齐").
            if self._loop_speed_applied is not None:
                self.player.set_property("speed", "1.0")
                self._loop_speed_applied = None
                self.log("    · 已恢复 1.0x 速度（偏差回到死区）")
            return
        if action == "skip-anomaly":
            # `correction` is the RESIDUAL here (see closed_loop_step), not a
            # proposed seek, so label it as the observed deviation. The message
            # says the reading is being CONFIRMED, because that is what happens
            # next: one more agreeing sample turns this into a resync instead of
            # an ever-growing strike count.
            self.log(f"  · 闭环校验：偏差 {correction:+.1f}s 超出漂移量级"
                     f"（>{ALIGN_LOOP_MAX_STEP_SEC:.1f}s），先确认再处理"
                     f"（第 {self._loop_strikes}/{ALIGN_LOOP_MAX_STRIKES} 次）")
            return
        if action == "gave-up":
            self.log("  · 闭环校验本曲停用（偏差反复异常且不一致，避免误调）")
            return
        if action == "resync":
            # A CONFIRMED large desync: resynchronise with one absolute seek.
            # Measured live 2026-10-08: the previous song's fine alignment
            # landed after a track change and left the picture 29s off; the old
            # code disabled the loop instead, so the user watched that for the
            # rest of the song.
            with self._lock:
                if self._pending != key or self.current != key:
                    return
            target = video_pos + correction
            landed, observed = self.player.seek_verified(target)
            self.log(f"  · 闭环校验：偏差 {self.last_loop_error:+.1f}s 已被连续确认，"
                     f"判定为真实失步 → 重新同步到 {target:.1f}s")
            if not landed:
                self.log(f"    · ⚠ 重同步未确认落点（读到 "
                         f"{'无' if observed is None else f'{observed:.1f}s'}）"
                         f"—— 下个周期会重新测量")
            return
        if action == "speed":
            # Smooth drift correction: nudge the playback speed instead of
            # seeking. `correction` here is the target SPEED (e.g. 1.02).
            with self._lock:
                if self._pending != key or self.current != key:
                    return
            speed = float(correction)
            self.player.set_property("speed", f"{speed:.4f}")
            self._loop_speed_applied = speed
            self.log(f"  · 闭环校验：偏差 {self.last_loop_error:+.1f}s"
                     f" → 播放速度 {speed:.2f}x（平滑追平，无跳变）")
            return
        if action != "correct" or correction is None:
            return

        # Re-check ownership immediately before touching the player: the probe
        # above took up to ~3.5s (three SMTC samples), and the user can change
        # songs inside that window.
        with self._lock:
            if self._pending != key or self.current != key:
                return
        target = video_pos + correction
        landed, observed = self.player.seek_verified(target)
        # Log the user-visible story: what the deviation was, which way the
        # picture moved, and where it landed. `correction` is negative when the
        # picture was AHEAD (it must move back), so report the applied delta as
        # `correction` on the video clock -- not a naked double negative.
        self.log(f"  · 闭环校验：音乐 {music_pos:.1f}s vs 画面 {video_pos:.1f}s "
                 f"→ 偏差 {self.last_loop_error:+.1f}s，"
                 f"已微调 {correction:+.1f}s 到 {target:.1f}s")
        if not landed:
            self.log(f"    · ⚠ 微调未确认落点（读到 "
                     f"{'无' if observed is None else f'{observed:.1f}s'}）"
                     f"—— 下个周期会重新测量")

    def log_loop_summary(self, key: TrackKey) -> None:
        """Say what the loop did for a song that just ended.

        Without this the user cannot tell "the loop works and had nothing to do"
        from "the loop never ran" -- both look like silence in the log.
        """
        if self._loop_key != key:
            return
        if self._loop_applied_count:
            self.log(f"  · 闭环微调本曲共 {self._loop_applied_count} 次"
                     f"（最后一次 {self.last_loop_applied:+.2f}s）"
                     if self.last_loop_applied is not None else
                     f"  · 闭环微调本曲共 {self._loop_applied_count} 次")
        else:
            self.log("  · 闭环微调本曲 0 次（偏差始终在死区内或位置不可用）")

    def log(self, msg: str) -> None:
        if self.verbose:
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] {msg}", flush=True)

    # ---------------- manual alignment wiring (session 8) ----------------
    #
    # Three channels have to meet here, and they carry DIFFERENT things:
    #
    #   * calibration store (state/align_calib.json) -- the remembered user
    #     preference, two levels (song, then app). Read ONCE per song.
    #   * hotkey sidecar (player.MANUAL_FILE) -- what the user nudged DURING
    #     this song. mpv resets it to 0 on every file load, so it is a per-song
    #     delta, not an absolute preference.
    #   * pending channel (align_calib.PENDING_FILE) -- absolute SET requests
    #     published by the control window's buttons.
    #
    # The effective manual preference the corrector must respect is
    # `inherited + hotkey delta`, and whatever the user does is written back to
    # the store so the next song inherits it. Keeping the channels separate is
    # what stops a stale value from one being mistaken for a fresh action in
    # another.

    @staticmethod
    def _calib_track_key(session: NowPlaying) -> str:
        """Storage key for the song level (title+artist+10s bucket)."""
        return make_track_key(session.title, session.artist,
                              session.duration_sec)

    def _load_inherited_manual(self, session: NowPlaying, key: TrackKey) -> None:
        """Adopt the remembered manual preference for a newly started song.

        Called at song start, before playback, so the very first seek already
        reflects the user's preference instead of being corrected a few seconds
        later (which the user would see as the picture jumping).

        INHERITANCE comes from task-3's `decide_inherited_offset`, which owns
        the rules (song level beats app level; a bucket mismatch falls back to
        app level; out-of-range values are refused rather than clamped).

        `corr=None` IS DELIBERATE. The inheritance module can blend a
        cross-correlation error with the manual preference, but this follower's
        closed loop already measures and corrects the alignment error
        continuously. Passing the correlation error here as well would count it
        TWICE -- once as a permanent preference and once as a live correction --
        which is the double-counting failure mode task-3 flagged for wiring.
        """
        self._manual_key = self._calib_track_key(session)
        self._app_id = session.app_id or ""
        self._manual_sidecar_seen = None
        self._manual_base_sec = 0.0

        track = {
            "title": _normalise_title(session.title),
            "artist": (session.artist or "").strip().lower(),
            "duration_bucket": int((session.duration_sec or 0) // 10),
        }
        try:
            from align_inherit import read_calibration_snapshot
            decision = decide_inherited_offset(
                track=track, app_id=self._app_id,
                calib=read_calibration_snapshot(), corr=None,
            )
        except Exception as exc:  # noqa: BLE001 - never block a song switch
            self.log(f"  · 手动校准读取失败（{type(exc).__name__}: {exc}），本次不继承")
            decision = None

        if decision is not None and decision.offset_sec:
            self._manual_base_sec = float(decision.offset_sec)
            self.log(f"  · 继承手动校准 {self._manual_base_sec:+.2f}s"
                     f"（来源: {decision.source}，{decision.reason}）")
        self.set_manual_offset(self._manual_base_sec)

    def _poll_manual_inputs(self, session: NowPlaying, key: TrackKey) -> None:
        """Apply GUI requests and hotkey nudges for the song being shown.

        Runs on every poll while the song is current. Never raises: a broken
        calibration file must not stop playback.

        THE OFFSET MODEL (worth stating precisely -- an earlier version got it
        wrong and double-counted the second keypress):

            effective = clamp(baseline + hotkey_cumulative)

          * `hotkey_cumulative` is what mpv's sidecar reports. mpv writes 0.0
            every time a file loads and then ACCUMULATES each keypress, so the
            file is the total nudge for THIS song, not a delta since last read.
            It must therefore be ADDED TO A FIXED BASELINE, never accumulated
            into a running total -- doing the latter made `]` `]` produce
            0.1 + 0.2 = 0.3 instead of 0.2.
          * `baseline` is the preference inherited at song start, or the
            absolute value a control-window button asked for.
          * A GUI request is ABSOLUTE ("make the offset X"), and the value it
            sends already includes whatever the hotkeys contributed (the window
            reads the store, which we keep up to date). So the baseline is
            re-derived as `X - hotkey_cumulative` to make `effective == X`
            exactly, rather than adding X on top of the hotkey total.
        """
        if self._manual_key != self._calib_track_key(session):
            # A different song is current than the manual state belongs to; the
            # song-start path owns (re)initialisation.
            return

        # --- 1. hotkey sidecar (read FIRST: the GUI step below reconciles
        #        against it) ---------------------------------------------------
        sidecar: float | None
        try:
            raw = MANUAL_FILE.read_text(encoding="utf-8").strip()
            sidecar = float(raw) if raw else 0.0
        except (OSError, ValueError):
            sidecar = None          # absent/unreadable: keep the last known

        hotkey_changed = False
        if sidecar is not None and sidecar != self._manual_sidecar_seen:
            first_read = self._manual_sidecar_seen is None
            self._manual_sidecar_seen = sidecar
            # On the very first read a non-zero value means the user pressed a
            # key before we polled; treat that as a real nudge. A zero is just
            # mpv's initial write on file load and is not an event.
            hotkey_changed = (not first_read) or bool(sidecar)
            if hotkey_changed and not first_read:
                self.log(f"  · 手动对齐（热键）: 本曲累计 {sidecar:+.2f}s")

        # --- 2. control-window buttons: ABSOLUTE set requests ---------------
        gui_changed = False
        try:
            for req in consume_pending_offsets():
                if req.get("track_key") and req["track_key"] != self._manual_key:
                    # A request for another song (the user switched while it was
                    # queued): applying it here would offset the wrong video.
                    self.log(f"  · 忽略一条针对其他歌曲的手动请求"
                             f"（{req.get('track_key')}）")
                    continue
                value = clamp_offset(float(req.get("offset_sec") or 0.0))
                # Absorb the hotkey contribution so the request's absolute value
                # is honoured exactly (see the model above).
                self._manual_base_sec = clamp_offset(
                    value - (self._manual_sidecar_seen or 0.0))
                gui_changed = True
                self.log(f"  · 手动对齐（{req.get('source') or 'gui'}）: "
                         f"设为 {value:+.2f}s")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  · 读取待施加手动偏移失败（{type(exc).__name__}）")

        if not (hotkey_changed or gui_changed):
            return

        effective = clamp_offset(self._manual_base_sec
                                 + (self._manual_sidecar_seen or 0.0))
        self.set_manual_offset(effective)
        self.log(f"  · 手动偏移生效 {effective:+.2f}s"
                 f"（基线 {self._manual_base_sec:+.2f}s"
                 f" + 热键 {self._manual_sidecar_seen or 0.0:+.2f}s）")
        # Persist so later songs inherit it (song level; the window's explicit
        # "save as app default" button is what promotes it to the player level).
        try:
            calib_set_manual_offset(self._manual_key, self._app_id,
                                    effective, note="follow 施加")
        except Exception as exc:  # noqa: BLE001
            self.log(f"  · 手动校准落盘失败（{type(exc).__name__}: {exc}）")

    def _publish_now(self, session: NowPlaying, key: TrackKey) -> None:
        """Publish what is being followed, for tools/align_control.py.

        The control window needs the TrackKey and app_id to address its
        requests, plus the current residual so the user can see what the closed
        loop is doing. mpv's own status file cannot carry any of that, so this
        is a separate, best-effort file.
        """
        payload = {
            "title": session.title,
            "artist": session.artist,
            "app_id": self._app_id,
            "track_key": self._manual_key,
            "manual_offset": round(self._manual_offset_sec, 3),
            "auto_error": (round(self.last_loop_error, 3)
                           if self.last_loop_error is not None else None),
            "updated": time.time(),
        }
        try:
            tmp = NOW_FILE.with_name(NOW_FILE.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, NOW_FILE)
        except OSError:
            pass        # best effort: the window just shows "no state"

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
            # Manual alignment (task-2): pick up control-window requests and
            # hotkey nudges, then republish state for the control window. Done
            # BEFORE the closed-loop check so a fresh manual preference is
            # already reflected in the residual the loop measures -- otherwise
            # the loop would spend one cycle "correcting" a nudge the user just
            # made (the two channels are added, see measure_residual).
            self._poll_manual_inputs(session, key)
            self._publish_now(session, key)
            # TODO.md B2: fine alignment corrected the picture ONCE, seconds
            # after the song started. Everything after that -- residual bias in
            # the alignment target, a sub-second seek landing, both players'
            # clocks -- is uncorrected, which is the user's "持续 <1s 偏移".
            # This is the periodic re-check that closes that loop. It is
            # self-throttling (ALIGN_LOOP_PERIOD_SEC), never blocks, and is a
            # no-op when alignment is off.
            if self.align and self.current is not None:
                self._maybe_correct_drift(session, key)
            return  # same song, nothing else to do

        if self.current is not None:
            # Report what the loop achieved for the song that is ending, then
            # reset per-song state so the new song starts with a clean slate
            # (cooldown/strikes must not carry over).
            self.log_loop_summary(self.current)

        self.log(f"检测到切歌: {session.title} - {session.artist} "
                 f"({session.duration_sec:.0f}s) [{session.app_id}]")
        self._reset_loop_state(key)
        # Adopt the remembered manual preference BEFORE the video starts, so the
        # first seek already lands where the user wants it. Loading it later
        # would show as a visible jump once the corrector reacted.
        self._load_inherited_manual(session, key)
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

        # Start the video at the music position PLUS the user's manual
        # preference, so a song with an inherited calibration is already where
        # the user wants it from the first frame. `rough` is a MUSIC position;
        # the steady state the closed loop maintains is `video = music +
        # manual`, so the initial guess must use the same relation or the loop
        # would immediately shift the picture (a visible jump right after the
        # video appears).
        start_sec = rough + self._manual_offset_sec
        if self._manual_offset_sec:
            self.log(f"  · 起播点含手动校准 {self._manual_offset_sec:+.2f}s "
                     f"→ {start_sec:.1f}s")
        if not self.player.play_url(stream, start_sec=start_sec, mute=True):
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
        self.log(f"  ✓ 已开始播放（起点 {start_sec:.1f}s，依据: {rough_source}）")

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

        # Add the user's manual preference to the alignment target.
        #
        # WHY (this is the wiring half of task-2's semantics): the closed loop
        # drives the system to `video == music + manual` (see measure_residual).
        # Fine alignment computes where the MUSIC is, so the video target must
        # be that position PLUS the manual preference -- otherwise a song that
        # starts with an inherited calibration would be seeked to the aligned
        # position and the corrector would then push it to the right place a few
        # seconds later, which the user sees as the picture jumping on every
        # song change.
        manual = self._manual_offset_sec
        target = corrected + manual
        if manual:
            self.log(f"  · 手动校准叠加 {manual:+.2f}s → 目标 {target:.1f}s")

        with self._lock:
            if self._pending != key:
                return
        if target > 0.5:
            # VERIFY the landing instead of announcing the computed number.
            # The old code logged "画面已校正到 83.5s" purely from `corrected`,
            # which is what it COMPUTED -- if mpv dropped the command or clamped
            # the target past the end of the PV, the log still claimed success
            # and the user saw a desync with no explanation in the log.
            landed, observed = self.player.seek_verified(target)
            if landed:
                self.log(f"  ↻ 画面已校正到 {target:.1f}s"
                         f"（实测落点 {observed:.1f}s）")
            else:
                self.log(f"  ↻ 画面已发出校正 {target:.1f}s，"
                         f"但未确认落点（读到 "
                         f"{'无' if observed is None else f'{observed:.1f}s'}）"
                         f"—— 后续闭环微调会继续纠偏")

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
                # `record()` returns a Recording carrying the interval the audio
                # REALLY covers. The old code stored only a path and then used
                # the nominal ALIGN_CAPTURE_SEC in the alignment formula, which
                # was wrong twice over:
                #   (a) the wall-clock loop can stop early or run long, so the
                #       captured audio is not exactly `seconds` long; and
                #   (b) the decode/downmix/resample/WAV-write tail inside
                #       record() produces NO audio, yet the old `t_end =
                #       time.monotonic()` was stamped only AFTER record()
                #       RETURNED, so that tail was counted as audio time.
                # Both errors push the computed target too far ahead, i.e. a
                # persistent sub-second offset -- exactly the bug being fixed.
                rec = record(live_path, seconds=ALIGN_CAPTURE_SEC)
                captured["rec"] = rec
                captured["path"] = rec.path if rec else None
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

            # NOTE: the old code also computed `elapsed = now - t0` here. It was
            # never added to the target (the absolute formula uses
            # `elapsed_after_capture` instead), i.e. it was already dead. It is
            # deleted rather than carried along, so nobody "fixes" the formula
            # later by adding it back and double-counting the same interval.
            rec = captured.get("rec")
            # (A) REAL capture length, measured from the file the recorder
            # produced -- never the nominal constant. See capture.Recording.
            capture_sec = float(getattr(rec, "covered_sec", 0.0) or 0.0)
            if capture_sec <= 0.0:
                # Defensive: no measured length means the formula would fall
                # back to a guess, which is the whole bug. Refuse instead.
                self.log("    · 采集时长不可测，跳过校正")
                return None
            # (B) The instant the captured AUDIO stopped, excluding record()'s
            # decode/resample tail (which is what the old t_end wrongly
            # included). Falls back to t0 + capture_sec if the recorder did not
            # report it, which is at least consistent with capture_sec.
            t_audio_end = float(getattr(rec, "t_audio_end", 0.0) or 0.0)
            if t_audio_end <= 0.0:
                t_audio_end = t0 + capture_sec
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
            #   * when the captured audio ENDED (t_audio_end -- the last sample,
            #     NOT when record() returned),
            #   * that the capture's own audio sat at PV position_in_pv,
            #   * that the capture lasted capture_sec (measured from the file).
            # Therefore the music is NOW at:
            #   position_in_pv + capture_sec + (time since the audio ended)
            #
            # Why `t_audio_end` rather than "record() returned": record() spends
            # real time AFTER the last sample decoding, downmixing, resampling
            # and writing the WAV. That tail has no audio in it, so counting it
            # as audio time pushed the target ahead by however long the tail
            # took -- a silent, systematically positive error.
            elapsed_after_capture = max(0.0, time.monotonic() - t_audio_end)
            target = position_in_pv + capture_sec + elapsed_after_capture

            self.log(f"    · {result.note}")
            self.log(f"    · 片段起点 {win_start:.1f}s + 偏移 {result.delay_sec:+.1f}s"
                     f" = 采集音频在 PV {position_in_pv:.1f}s")
            self.log(f"    · 采集实测 {capture_sec:.2f}s（名义 {ALIGN_CAPTURE_SEC:.0f}s，"
                     f"{getattr(rec, 'frames', 0)} 帧）"
                     f"后再过 {elapsed_after_capture:.1f}s => 音乐现在位于 {target:.1f}s")
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
