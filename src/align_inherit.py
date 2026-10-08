"""align_inherit.py -- inherit manual alignment across songs (pure decision logic).

WHY this module exists (user requirement, 2026-10-07):
    "考虑如何使用这个按钮在不同歌曲之间继承对齐效果" -- the manual align
    button should not be wasted on the song you just heard; the calibration
    should carry over to the NEXT song so repeated manual fixing of the same
    systematic bias becomes unnecessary.

Background (Lead's survey, task-3 description):
    A sync offset has two very different causes:
      ① app-level systematic bias -- the player/link path itself (e.g. 网易云
         SMTC reports positions at a coarse granularity, biasing every song
         played through that app by roughly the same amount). This is a
         PROPERTY OF THE PLAYER, so it should inherit across songs.
      ② song-level bias -- one particular song's PV edit differs from its audio
         source (different intro, different cut). This is a property of THE
         SONG, so it must be bound to that song (title + artist + duration
         bucket) and its candidate version.

Scope contract (task-3):
    This file is PURE DECISION LOGIC only -- no follow.py wiring (the Lead
    wires it in after task-1/task-2 land). Every decision function is a pure
    function: input snapshot dict -> output decision. No IO, no player, no
    network. Storage access lives in read_calibration_snapshot(), which is a
    thin, fault-tolerant wrapper.

Sign convention (MUST match align.py / follow.py):
    align.py's AlignmentResult.delay_sec: positive => the video lags the music
    => we must seek the VIDEO FORWARD by that amount. follow.py then seeks the
    video to (position + delay). Everything in this module uses the SAME
    convention: the returned offset is "how many seconds to seek the VIDEO
    forward to catch up with the music" (video-lag offset). A manual nudge that
    says "push the picture later" therefore INCREASES this offset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants -- every cap and threshold, with the WHY in comments (task-3 A).
# ---------------------------------------------------------------------------

# Absolute cap on ANY inherited offset. WHY: an offset beyond ~10s means the
# "same song" is almost certainly NOT the same edit/version (an intro that
# differs by >10s, a wrong version, or the audio source is a different take).
# Inheriting a 30s "preference" would then permanently corrupt every future
# song that shares the app/title -- far worse than inheriting nothing. So the
# decision function CLAMPS to this bound; a value that exceeds it is treated
# as "user changed source/version" and inheriting a stale bias is refused
# (宁可不放，也不放错的 -- project iron rule 15, applied to calibration).
# Baseline for the judgement: normal sync bias measured 0.2-0.6s (NOTES §1);
# a 10s bound is two orders of magnitude above that, so it never interferes
# with legitimate corrections.
MAX_INHERITED_OFFSET_SEC = 10.0

# Song-level hits are trusted only when the duration buckets agree. WHY:
# duration_bucket (int(duration // 10)) is part of the TrackKey identity --
# same title+artist at a very different length is a different version (an
# extended cut, a different recording), whose intro/edit bias is NOT
# transferable. But two versions of the same song legitimately differ by a few
# seconds, so a one-bucket (10s) mismatch still counts as "the same song" with
# a slightly degraded confidence (see compute()).
MAX_BUCKET_MISMATCH = 1

# Cross-correlation (本次对齐误差, instantaneous) is blended into the manual
# preference (长期有效) when the correlation is trustworthy. Weight chosen:
# a single correlation can be fooled by repeated sections (measured
# 2026-10-07: a wrong repeat produced a sharp, high-margin peak), while the
# manual value is the user's deliberate, stable preference -- so the
# correlation may only move the result a fraction of the way.
CORRELATION_BLEND_WEIGHT = 0.5

# A correlation offset larger than this is treated as untrustworthy on its own
# (same reasoning as MAX_INHERITED_OFFSET_SEC: it implies a different version
# or a latched repeat, not a small sync error to blend in).
MAX_TRUSTED_CORR_SEC = 10.0

# ---------------------------------------------------------------------------
# Public decision API (all pure)
# ---------------------------------------------------------------------------


class InheritedOffset:
    """Decision output: how to adjust the video for the CURRENT song.

    Attributes:
        offset_sec: seconds to seek the VIDEO FORWARD (video-lag convention,
            matches align.py delay_sec). Clamped to [-MAX_INHERITED_OFFSET_SEC,
            +MAX_INHERITED_OFFSET_SEC]. 0.0 with source "none" means "no
            calibration applies -- behave as before".
        source: "song" | "app" | "none". The calibration level that produced
            the offset. "song" overrides "app" (rule R1).
        confidence: 0..1. How much we trust the offset (see rule table).
        reason: human-readable decision rationale (for follow.py logs).
    """

    __slots__ = ("offset_sec", "source", "confidence", "reason")

    def __init__(
        self,
        offset_sec: float,
        source: str,
        confidence: float,
        reason: str,
    ) -> None:
        self.offset_sec = float(offset_sec)
        self.source = source
        self.confidence = float(confidence)
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"InheritedOffset(offset_sec={self.offset_sec:+.3f}, "
            f"source={self.source!r}, confidence={self.confidence:.2f}, "
            f"reason={self.reason!r})"
        )


def decide_inherited_offset(
    track: dict[str, Any],
    app_id: str,
    calib: dict[str, Any] | None,
    corr: dict[str, Any] | None,
) -> InheritedOffset:
    """Combine manual calibration + cross-correlation into one inherited offset.

    Pure function -- no IO. All inputs are plain dicts so the module is
    trivially testable and can be driven straight from follow.py without a
    storage layer.

    Args:
        track: the current TrackKey as a dict. Expected keys:
            "title" (normalized), "artist" (normalized), "duration_bucket"
            (int, TrackKey.duration_bucket). Anything missing degrades to a
            "none" decision instead of crashing.
        app_id: e.g. "cloudmusic.exe" (the SMTC session's app id).
        calib: snapshot from read_calibration_snapshot() (or a hand-built
            dict for tests). This is exactly task-2's align_calib snapshot
            format (CalibrationStore.snapshot()), read tolerantly:
              {
                "version": 1,
                "tracks": { "<title>|<artist>|<bucket>": {"offset_sec": ...,
                                                          "samples": ...,
                                                          "updated_at": ...,
                                                          "nudges": [...]}, ...},
                "apps":   { "<app_id>": {"offset_sec": ..., ...}, ...},
              }
            The song key embeds the duration bucket (align_calib.make_track_key),
            and every stored offset follows the video-lag sign convention.
        corr: the latest cross-correlation result, or None. Expected keys:
            "delay_sec" (video-lag offset, see align.py), "confidence" (0..1),
            "trustworthy" (bool, from AlignmentResult.trustworthy).

    Returns:
        InheritedOffset -- final offset + source + confidence + reason.
    """
    # --- step 0: nothing calibrated? ---
    if not calib:
        return InheritedOffset(0.0, "none", 0.0,
                               "no calibration data (empty snapshot)")
    app_cal = _app_entry(calib, app_id)
    song_cal = _song_entry(calib, track)

    # --- step 1: pick the calibration level (rule R1) ---
    # A song-level hit wins over the app-level value whenever it exists. WHY:
    # the song-level value is strictly more specific -- it was measured on THIS
    # song (this exact duration bucket), so it contains the song-specific edit
    # bias ON TOP of the app bias. The app-level value cannot know about this
    # song's intro, so using it while a song value exists would throw away the
    # most relevant information.
    #
    # The bucket is part of the song key, so a key hit already implies a
    # matching version (see _song_entry). A key MISS caused by a bucket
    # difference is reported explicitly by _bucket_gate_reason below.
    if song_cal is not None:
        level = "song"
        cal = song_cal
        # No mismatch to explain: a key hit is the same title+artist+bucket.
        prefix_note = ""
    elif app_cal is not None:
        level = "app"
        cal = app_cal
        # If a song-level entry exists at a DIFFERENT bucket, say so: silently
        # substituting the app value would hide the strongest available hint
        # that the matched video may be the wrong version of the song.
        note = _bucket_gate_reason(calib, track)
        if "different version" in note:
            prefix_note = f"({note}) "
        else:
            prefix_note = ""
    else:
        return InheritedOffset(
            0.0, "none", 0.0,
            f"{_bucket_gate_reason(calib, track)} and no app-level entry")

    base = float(cal["offset_sec"])
    # --- step 2: clamp (rule R4) ---
    if abs(base) > MAX_INHERITED_OFFSET_SEC:
        # A stored offset beyond the bound is refused outright (not clamped):
        # see MAX_INHERITED_OFFSET_SEC for the reasoning -- it almost always
        # means the source/version changed and this "calibration" is stale
        # garbage. Better inherit nothing than inherit nonsense.
        return InheritedOffset(
            0.0, "none", 0.0,
            f"{prefix_note}{level}-level offset {base:+.2f}s exceeds cap "
            f"{MAX_INHERITED_OFFSET_SEC:+.1f}s; refusing to inherit")

    # --- step 3: merge the correlation (rule R3) ---
    corr_offset, corr_conf, corr_trusted = _corr_parts(corr)
    if corr_trusted:
        # 闭环调「误差」+ 手动调「偏好」=> 相加 (task-2 semantics). The
        # correlation says "the CURRENT alignment is off by corr_offset" --
        # that is an ERROR term, transient, re-measured every song. The manual
        # calibration is a PREFERENCE, long-lived. They are different physical
        # quantities, so they ADD. But because a single correlation is
        # fallible (repeats!), the error term is damped by
        # CORRELATION_BLEND_WEIGHT instead of applied 1:1.
        offset = base + CORRELATION_BLEND_WEIGHT * corr_offset
        # --- step 4: clamp AFTER merging too (rule R4) ---
        offset = max(-MAX_INHERITED_OFFSET_SEC,
                     min(MAX_INHERITED_OFFSET_SEC, offset))
        return InheritedOffset(
            offset, level,
            confidence=_blend_confidence(cal, corr_conf),
            reason=(f"{prefix_note}{level}-level manual {base:+.2f}s + "
                    f"{CORRELATION_BLEND_WEIGHT}×correlation "
                    f"{corr_offset:+.2f}s (conf {corr_conf:.2f})"))

    # correlation untrustworthy / absent -> manual value alone (rule R2b)
    return InheritedOffset(
        base, level,
        confidence=_cal_confidence(cal),
        reason=prefix_note + f"{level}-level manual offset {base:+.2f}s"
               + ("" if corr is None
                  else " (correlation untrustworthy, ignored)"))


# ---------------------------------------------------------------------------
# Lookup helpers (pure; tolerant of malformed snapshots -- task-3 B)
# ---------------------------------------------------------------------------


def track_key(title: str, artist: str, bucket: int) -> str:
    """Song-level key, byte-identical to align_calib.make_track_key().

    VERIFIED 2026-10-07 against the landed task-2 module:
        make_track_key(title, artist, duration_sec) -> "title|artist|bucket"
    The bucket is EMBEDDED IN THE KEY string, not stored as a separate field
    inside the entry. Reproduced here rather than imported because this is the
    identity contract between the two modules and must be visible/assertable;
    test_align_inherit.py pins the two implementations against each other so a
    drift in either one fails a test.
    """
    return f"{str(title).strip().lower()}|{str(artist).strip().lower()}|{int(bucket)}"


def _song_entry(calib: dict[str, Any],
                track: dict[str, Any]) -> dict[str, Any] | None:
    """Return the song-level calibration entry, or None when absent.

    Looks up the ONE bucket-precise key first. A miss is NOT backfilled by
    scanning neighbouring buckets: the bucket is part of the identity
    (follow.py TrackKey), and two different buckets are two different
    versions. The caller then falls back to the app level, which is the
    documented, deliberate回退行为 (see _bucket_gate_reason).
    """
    tracks = calib.get("tracks") or {}
    if not isinstance(tracks, dict):
        return None
    title = track.get("title")
    artist = track.get("artist")
    bucket = track.get("duration_bucket")
    if title is None or artist is None or bucket is None:
        return None  # cannot form a song key without full identity
    entry = tracks.get(track_key(str(title), str(artist), int(bucket)))
    if not isinstance(entry, dict):
        return None
    if not isinstance(entry.get("offset_sec"), (int, float)):
        return None  # malformed entry: treat as absent, do not crash
    return entry


def _app_entry(calib: dict[str, Any], app_id: str) -> dict[str, Any] | None:
    """Return the app-level calibration entry, or None when absent."""
    apps = calib.get("apps") or {}
    if not isinstance(apps, dict) or not app_id:
        return None
    entry = apps.get(str(app_id))
    if not isinstance(entry, dict):
        return None
    if not isinstance(entry.get("offset_sec"), (int, float)):
        return None
    return entry


# Tolerance for a DIAGNOSTIC-ONLY search: how many 10s buckets away a song
# entry may be before it stops being worth mentioning in a log line. Wider
# than MAX_BUCKET_MISMATCH on purpose -- a stored bucket 4 away is clearly a
# different version and must NOT be inherited, but it IS worth telling the
# user "this song was calibrated, at a different length", which is a strong
# hint that the matched video is the wrong version.
_EXPLAIN_BUCKET_WINDOW = 6


def near_bucket_song_entry(calib: dict[str, Any],
                           track: dict[str, Any]) -> dict[str, Any] | None:
    """Find a song-level entry with the same title+artist at a NEARBY bucket.

    Exists to SERVICE THE BUCKET-MISMATCH DECISION, not to silently use it: a
    hit here means "the user calibrated this song, but the current track's
    duration says it is a different version". compute() deliberately does NOT
    inherit such a value -- it only uses the hit to explain WHY in the log
    ("this song was calibrated, but at a different length → falling back"),
    which is far more debuggable than an unexplained app-level value.

    The window is _EXPLAIN_BUCKET_WINDOW (diagnostic only) and is deliberately
    WIDER than MAX_BUCKET_MISMATCH (the inheritance gate). Do not conflate the
    two: one decides what we may USE, the other only what we may SAY.
    """
    tracks = calib.get("tracks") or {}
    if not isinstance(tracks, dict):
        return None
    title = track.get("title")
    artist = track.get("artist")
    bucket = track.get("duration_bucket")
    if title is None or artist is None or bucket is None:
        return None
    prefix = f"{str(title).strip().lower()}|{str(artist).strip().lower()}|"
    best: dict[str, Any] | None = None
    best_gap = _EXPLAIN_BUCKET_WINDOW + 1
    for key, entry in tracks.items():
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        if not isinstance(entry, dict):
            continue
        try:
            other = int(key[len(prefix):])
        except ValueError:
            continue
        gap = abs(other - int(bucket))
        if 0 < gap <= _EXPLAIN_BUCKET_WINDOW and gap < best_gap:
            best, best_gap = entry, gap
    return best


def _bucket_gate_reason(calib: dict[str, Any],
                        track: dict[str, Any]) -> str:
    """Explain a song-level miss: was it absent, or a different version?

    WHY this exists: "song entry absent" and "song entry exists but for a
    different-length version" call for completely different user-facing
    messages. The first is normal (never calibrated this song); the second
    means the calibration IS there but the durations disagree, which is the
    signal that the matched video may be the wrong version.
    """
    near = near_bucket_song_entry(calib, track)
    if near is not None:
        return ("song calibrated at a nearby duration but not this bucket "
                "(likely a different version) -> app-level fallback")
    return "no song-level entry for this track"


def _corr_parts(corr: dict[str, Any] | None) -> tuple[float, float, bool]:
    """Extract (offset_sec, confidence, trusted) from a correlation dict.

    trusted requires ALL of: a trustworthy flag (mirrors align.py
    AlignmentResult.trustworthy), a real numeric offset, and |offset| within
    MAX_TRUSTED_CORR_SEC. WHY the extra bound: follow.py already rejects
    correlations whose target is implausible, but this module must be safe
    even when fed a raw result -- a 40s "correlation error" on a 3min song is
    a latched repeat, not a sync error, and must NOT be blended into the
    inherited preference (it would corrupt the user's long-term calibration).
    """
    if not isinstance(corr, dict):
        return (0.0, 0.0, False)
    if not bool(corr.get("trustworthy", False)):
        return (0.0, 0.0, False)
    off = corr.get("delay_sec")
    conf = corr.get("confidence", 0.0)
    if not isinstance(off, (int, float)) or not isinstance(conf, (int, float)):
        return (0.0, 0.0, False)
    if abs(float(off)) > MAX_TRUSTED_CORR_SEC:
        return (0.0, 0.0, False)
    return (float(off), float(conf), True)


def _cal_confidence(entry: dict[str, Any]) -> float:
    """Confidence of a manual calibration entry. Stored confidence wins when
    present (task-2 may record it); otherwise a 1-sample manual value is
    taken at face value (1.0) -- it is the user's deliberate choice."""
    c = entry.get("confidence")
    if isinstance(c, (int, float)) and 0.0 <= float(c) <= 1.0:
        return float(c)
    return 1.0


def _blend_confidence(cal_entry: dict[str, Any], corr_conf: float) -> float:
    """Combine the two confidences when both sources contribute.

    The manual entry and the correlation are independent measurements; the
    blended result is only as strong as the weaker one (AND semantics -- both
    must agree for high confidence, since either could be fooled on its own).
    """
    return round(_cal_confidence(cal_entry) * min(1.0, corr_conf), 3)


# ---------------------------------------------------------------------------
# Storage access (thin wrapper -- task-3 B)
# ---------------------------------------------------------------------------

# task-2 owns the storage format (src/align_calib.py, LANDED 2026-10-07). We
# DUCK-TYPE against its module-level surface rather than importing it at module
# scope, so this file stays importable with zero side effects and zero
# dependency on the storage layer existing:
#
#     align_calib.load_calibration(reload: bool = False) -> CalibrationStore
#         .snapshot() -> {"version", "tracks": {...}, "apps": {...}}
#
# We never WRITE calibration data ourselves -- recording manual nudges is
# task-2's job (record_manual_nudge / set_manual_offset). This module is
# read-only w.r.t. the store, which is what keeps it a pure decision layer.
_CALIB_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "state" / "align_calib.json"


def read_calibration_snapshot(reload: bool = True) -> dict[str, Any]:
    """Load the current calibration snapshot, tolerantly.

    Returns {} (empty snapshot) when the module/file is missing, corrupt, or
    the schema is unrecognized -- NEVER raises. WHY: calibration is an
    enhancement; a broken or absent state file must not take down the
    follower. An empty snapshot makes decide_inherited_offset() return source
    "none", offset 0, which is exactly the pre-feature behaviour.

    `reload=True` by default because the follower and the GUI are separate
    processes: the GUI writes a nudge, the follower must see it on the next
    cycle rather than a value cached at import time.
    """
    try:
        import align_calib  # task-2's module
        loader = getattr(align_calib, "load_calibration", None)
        if callable(loader):
            store = loader(reload=reload)
            snapshot = getattr(store, "snapshot", None)
            if callable(snapshot):
                data = snapshot()
                if isinstance(data, dict):
                    return data
            # Tolerate a loader that already returns plain data.
            if isinstance(store, dict):
                return store
    except Exception:  # noqa: BLE001 - import or loader failure: fall back
        pass
    return _read_calib_json()


def _read_calib_json() -> dict[str, Any]:
    """Direct tolerant JSON read (fallback when align_calib.py is unusable).

    Reads the SAME file with the SAME schema -- this is a degradation path,
    not a second storage implementation. We never write through it.
    """
    try:
        raw = _CALIB_FALLBACK_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            # Only accept the shape we understand -- anything else degrades to
            # an empty snapshot instead of guessing (fail closed, 铁律 15).
            if any(k in data for k in ("tracks", "apps")):
                return data
        return {}
    except (OSError, ValueError):
        return {}
