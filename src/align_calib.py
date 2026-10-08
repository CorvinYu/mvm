"""align_calib.py -- persistent manual-alignment calibration (two levels).

Why this exists
---------------
Automatic alignment (coarse position + cross-correlation) removes the
*algorithmic* error but it can never know that the user simply prefers the
picture a bit later, nor can it fix a constant bias of a particular player.
The user asked for a manual nudge ("手动对齐按钮"), and for that nudge to be
remembered so it does not have to be repeated on every song.

The single most important design rule here is the SEMANTICS OF THE OFFSET:

    alignment_error  = what the closed loop (task-1) measures and corrects
    manual_offset    = what the USER wants, on top of a correct alignment
    effective_offset = alignment_error + manual_offset

These are ADDED, never merged into one number. If the manual value were
folded into the auto-alignment error, the loop would immediately "correct"
the user's preference away -- the nudge would visibly spring back within one
or two poll cycles. Keeping them in separate slots is what makes a manual
nudge survive the loop. `effective_offset()` below is the only place where
they are combined, and it deliberately exposes both parts separately too, so
logs and the control window can show "闭环 -0.3s + 手动 +1.0s = +0.7s".

Sign convention
---------------
Positive = the VIDEO is moved LATER (forward) relative to the music, i.e. the
picture lags more. This matches the project's existing "+offset" language in
follow.log ("互相关对齐：偏移 +18.34s") and mpv's own `seek +N` relative form.
The mpv hotkeys therefore do `seek <delta> relative` with delta=+0.1 for `]`.

Two levels of storage
---------------------
state/align_calib.json holds:

    {
      "version": 1,
      "tracks": { "<track_key>": {...} },   # song level   (title+artist+bucket)
      "apps":   { "<app_id>":    {...} }    # player level (cloudmusic.exe)
    }

* song level  -- a specific upload/edit of a specific song is offset by a
  fixed amount (the PV's intro differs from the streaming master).
* app level   -- a player/route has a constant bias for EVERY song (e.g. the
  SMTC power-of-two sample granularity that makes cloudmusic.exe read 0.3s
  ahead of reality).

Lookup order is song level first, then app level ("歌曲级优先，回退 app 级"),
which is exactly what task-3's inheritance module consumes.

Each entry carries `samples` and `updated_at` so the inheritance layer can
weigh a one-off nudge against a repeatedly confirmed one, and `nudges` (the
newest deltas) so it can detect the user reversing their mind.

Storage format notes
--------------------
* Written atomically (tmp + replace) -- the follower, the control window and
  the Lua hotkey path may all touch this file, and a torn write would lose
  the whole calibration. `save()` returns False when the write did not land
  (rather than failing silently) so a caller can log it.
* `record_manual_nudge` APPLIES a delta (adds). `set_manual_offset` SETS an
  absolute value. The difference matters: the hotkeys report deltas, the
  "reset" button sets 0, and a screen that shows "current = 1.0s" must not
  add 1.0s again when saving.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

CALIB_FILE = STATE_DIR / "align_calib.json"

# The file the GUI writes and the follower reads: "the user wants the manual
# offset for the current song to become X".
#
# Line format (whitespace separated, one record per line, UTF-8):
#     <epoch_seconds> <offset_seconds> <track_key> <app_id> <source>
# The follower consumes (and truncates) it, applies the delta to the running
# video and records it in the calibration store. A plain append-only text file
# is used rather than JSON so that a half-written line can simply be skipped
# instead of invalidating the whole file (the GUI and the follower are
# different processes, and the hotkey path inside mpv writes through Lua).
PENDING_FILE = STATE_DIR / "_mvm_manual_offset.txt"

# Guard rails. A manual nudge beyond a few seconds is almost certainly a
# mis-click or a stuck key, and letting it through would seek the video to a
# wildly wrong place.
#
# Measured baseline (NOTES §1): the normal residual sync error is 0.2-0.6s and
# anything past 3s is already abnormal. A preference offset is a fraction of
# that, so +/-5s is a generous ceiling while still catching runaway input.
MAX_ABS_OFFSET_SEC = 5.0
# Entries older than this are dropped from the store by `prune()`. Not applied
# automatically on load -- aging policy belongs to task-3's inheritance module,
# which may well decide app-level entries never expire.
DEFAULT_MAX_AGE_DAYS = 180.0

_VERSION = 1


# --------------------------------------------------------------------------
# track keys
# --------------------------------------------------------------------------

def normalise_title(title: str) -> str:
    """Normalise a player-reported title for song identity.

    Mirrors follow.py's `_normalise_title` on purpose. Players decorate the
    same track inconsistently between polls ("勾指起誓" vs "勾指起誓 - 洛天依",
    plus " (Live)" / "【官方】" decorations), and a calibration stored under one
    spelling would be missed under another.

    Kept as a local copy rather than importing follow.py: follow.py is task-1's
    write scope, and importing it here would drag the whole follower (SMTC,
    matcher, mpv) into every consumer of this module, including the GUI and the
    tests. The two implementations are pinned together by a selftest case that
    asserts they agree on a shared set of samples.
    """
    t = (title or "").strip().lower()
    for dash in (" - ", " – ", " — ", "-"):
        if dash in t:
            head = t.split(dash)[0].strip()
            if head:
                t = head
                break
    for opener, closer in (("【", "】"), ("(", ")"), ("（", "）"), ("[", "]")):
        while opener in t and closer in t:
            start = t.find(opener)
            end = t.find(closer, start)
            if start == -1 or end == -1:
                break
            t = (t[:start] + t[end + 1:]).strip()
    # Collapse whitespace so "song  name" and "song name" are one key.
    return re.sub(r"\s+", " ", t).strip()


def duration_bucket(duration_sec: float | None) -> int:
    """Bucket a duration to 10s, matching follow.py's TrackKey.

    A 5s bucket was measured to be too tight: 汽水音乐 reports the duration with
    jitter while a track plays (183.5s then 184.9s for the same song), and a
    boundary at 185s split ONE song into TWO track keys.
    """
    if not duration_sec:
        return 0
    return int(duration_sec // 10)


def make_track_key(title: str, artist: str, duration_sec: float | None) -> str:
    """Stable storage key for the song level: normalised title+artist+bucket.

    The bucket (not the raw duration) is what makes a calibration survive the
    player reporting 222.1s on one poll and 222.4s on the next.
    """
    return "|".join((
        normalise_title(title),
        (artist or "").strip().lower(),
        str(duration_bucket(duration_sec)),
    ))


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------

@dataclass
class CalibEntry:
    """One level's remembered manual offset."""

    offset_sec: float = 0.0
    samples: int = 0
    updated_at: float = 0.0
    # Newest deltas, kept so task-3 can spot "the user keeps pushing the OTHER
    # way" (a changed mind) rather than summing forever.
    nudges: list[float] = field(default_factory=list)
    note: str = ""

    def to_json(self) -> dict:
        return {
            "offset_sec": round(self.offset_sec, 4),
            "samples": self.samples,
            "updated_at": self.updated_at,
            "nudges": [round(n, 4) for n in self.nudges],
            "note": self.note,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "CalibEntry":
        nudges = raw.get("nudges") or []
        return cls(
            offset_sec=float(raw.get("offset_sec") or 0.0),
            samples=int(raw.get("samples") or 0),
            updated_at=float(raw.get("updated_at") or 0.0),
            nudges=[float(n) for n in nudges if isinstance(n, (int, float))],
            note=str(raw.get("note") or ""),
        )


def clamp_offset(value: float, limit: float = MAX_ABS_OFFSET_SEC) -> float:
    """Clamp a manual offset to +/-limit (see MAX_ABS_OFFSET_SEC)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(-limit, min(limit, v))


@dataclass
class EffectiveOffset:
    """The two offset slots, kept separate and also summed.

    `auto_error` is the closed loop's correction (task-1). `manual` is the
    user's preference. `total` is what actually has to be applied to the video
    timeline. They are reported separately because "the picture is 0.7s late"
    has two very different fixes depending on which part dominates.
    """

    auto_error: float = 0.0
    manual: float = 0.0
    source: str = "无"

    @property
    def total(self) -> float:
        return self.auto_error + self.manual

    def describe(self) -> str:
        return (f"闭环 {self.auto_error:+.2f}s + 手动 {self.manual:+.2f}s"
                f" = {self.total:+.2f}s（来源: {self.source}）")


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class CalibrationStore:
    """Two-level manual calibration, persisted to one JSON file."""

    def __init__(self, path: Path | str = CALIB_FILE) -> None:
        self.path = Path(path)
        self.tracks: dict[str, CalibEntry] = {}
        self.apps: dict[str, CalibEntry] = {}
        self.load()

    # ---------------- persistence ----------------

    def load(self) -> None:
        """Read the store. A corrupt file is IGNORED, not fatal.

        Losing calibration costs the user one re-nudge; refusing to start
        because of a truncated JSON would cost them the whole session. The bad
        file is not deleted either -- it is left for inspection.
        """
        self.tracks = {}
        self.apps = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for name, target in (("tracks", self.tracks), ("apps", self.apps)):
            section = raw.get(name)
            if not isinstance(section, dict):
                continue
            for key, val in section.items():
                if isinstance(val, dict):
                    target[str(key)] = CalibEntry.from_json(val)

    def save(self) -> bool:
        """Atomically persist. Returns False if the write did not happen.

        The return value exists because the first version swallowed OSError
        silently -- and when the store was pointed at a directory the process
        could not write (measured: a tempdir created by another security
        context answered `WinError 5 拒绝访问`), every nudge appeared to be
        recorded while nothing reached the disk. A calibration the user
        carefully dialled in must not vanish without a trace, so callers (and
        the selftest) can detect and log the failure.
        """
        data = {
            "version": _VERSION,
            "tracks": {k: v.to_json() for k, v in self.tracks.items()},
            "apps": {k: v.to_json() for k, v in self.apps.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
            return True
        except OSError:
            # Still best-effort for the caller's control flow (a failed save
            # must not break playback), but the caller can now TELL.
            return False

    # ---------------- writes ----------------

    def record_manual_nudge(
        self,
        track_key: str,
        app_id: str,
        delta: float,
        note: str = "",
    ) -> CalibEntry:
        """ADD `delta` to the song level (creating it from the app level).

        Applied to the SONG level only. Rationale: pressing `]` while listening
        to one song means "for THIS song, later". Silently spreading it to every
        other song of the player would be surprising; the user has a separate
        explicit action for that (`save_as_app_default`).

        When the song has no entry yet it is seeded from the app level, so the
        first nudge refines the inherited baseline instead of discarding it.
        """
        entry = self.tracks.get(track_key)
        if entry is None:
            inherited = self.apps.get(app_id)
            entry = CalibEntry(
                offset_sec=inherited.offset_sec if inherited else 0.0,
                note="由 app 级继承起算",
            )
        entry.offset_sec = clamp_offset(entry.offset_sec + float(delta))
        entry.samples += 1
        entry.updated_at = time.time()
        entry.nudges.append(round(float(delta), 4))
        del entry.nudges[:-16]  # keep the newest few; the file stays small
        if note:
            entry.note = note
        self.tracks[track_key] = entry
        self.save()
        return entry

    def set_manual_offset(
        self,
        track_key: str,
        app_id: str,
        value: float,
        note: str = "",
    ) -> CalibEntry:
        """SET the song level to an absolute value (used by "重置"/save)."""
        entry = self.tracks.get(track_key)
        if entry is None:
            inherited = self.apps.get(app_id)
            entry = CalibEntry(offset_sec=inherited.offset_sec if inherited else 0.0)
        entry.offset_sec = clamp_offset(value)
        entry.samples += 1
        entry.updated_at = time.time()
        entry.nudges.append(round(entry.offset_sec, 4))
        del entry.nudges[:-16]
        if note:
            entry.note = note
        self.tracks[track_key] = entry
        self.save()
        return entry

    def save_as_app_default(self, app_id: str, value: float,
                            note: str = "") -> CalibEntry:
        """Promote an offset to the app level -- it applies to EVERY song.

        This is the "保存为跨歌校准" button. It is what turns a repeated manual
        correction into a one-time fix: a player whose SMTC position is sampled
        at a coarse granularity is biased by the same amount on every track.
        """
        entry = self.apps.get(app_id) or CalibEntry()
        entry.offset_sec = clamp_offset(value)
        entry.samples += 1
        entry.updated_at = time.time()
        entry.nudges.append(round(entry.offset_sec, 4))
        del entry.nudges[:-16]
        entry.note = note or "app 级默认偏移"
        self.apps[app_id] = entry
        self.save()
        return entry

    def clear_track(self, track_key: str) -> None:
        """Drop the song level so it falls back to the app level."""
        if track_key in self.tracks:
            del self.tracks[track_key]
            self.save()

    # ---------------- reads ----------------

    def song_offset(self, track_key: str) -> float | None:
        entry = self.tracks.get(track_key)
        return entry.offset_sec if entry else None

    def app_offset(self, app_id: str) -> float | None:
        if not app_id:
            return None
        entry = self.apps.get(app_id)
        return entry.offset_sec if entry else None

    def manual_offset(self, track_key: str, app_id: str = "") -> float:
        """Effective manual offset: song level, else app level, else 0.

        This is the "两级回退" lookup rule. Song level wins because it is the
        more specific observation: the same player, the same route, but this
        particular edit of this particular song.
        """
        song = self.song_offset(track_key)
        if song is not None:
            return song
        app = self.app_offset(app_id)
        return app if app is not None else 0.0

    def inherited_from(self, track_key: str, app_id: str = "") -> str:
        """Where the manual value would come from: 歌曲级 / app级 / 无."""
        if track_key in self.tracks:
            return "歌曲级"
        if app_id and app_id in self.apps:
            return "app级"
        return "无"

    def get_effective_offset(
        self,
        track_key: str = "",
        app_id: str = "",
        auto_error: float = 0.0,
    ) -> EffectiveOffset:
        """Combine the closed loop's error with the user's preference.

        THE contract of this module: the two are ADDED. The closed loop must
        keep correcting only `auto_error` -- if it also tried to cancel
        `manual`, a manual nudge would be undone within a poll cycle and the
        feature would look broken.
        """
        manual = self.manual_offset(track_key, app_id) if track_key else \
            (self.app_offset(app_id) or 0.0)
        return EffectiveOffset(
            auto_error=float(auto_error),
            manual=manual,
            source=self.inherited_from(track_key, app_id) if track_key
            else ("app级" if app_id in self.apps else "无"),
        )

    def prune(self, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> int:
        """Drop stale entries. Returns how many were removed."""
        cutoff = time.time() - max_age_days * 86400.0
        removed = 0
        for section in (self.tracks, self.apps):
            for key in [k for k, v in section.items()
                        if v.updated_at and v.updated_at < cutoff]:
                del section[key]
                removed += 1
        if removed:
            self.save()
        return removed

    def snapshot(self) -> dict:
        """Plain-data copy for the pure-function inheritance layer (task-3)."""
        return {
            "version": _VERSION,
            "tracks": {k: v.to_json() for k, v in self.tracks.items()},
            "apps": {k: v.to_json() for k, v in self.apps.items()},
        }


# --------------------------------------------------------------------------
# module-level convenience API (the intended import surface)
# --------------------------------------------------------------------------

_default_store: CalibrationStore | None = None


def load_calibration(path: Path | str = CALIB_FILE,
                     reload: bool = False) -> CalibrationStore:
    """Return the process-wide store, loading it once.

    A cached instance is used because the follower polls this on every cycle;
    re-reading and re-parsing the JSON each time would be pointless IO. Pass
    `reload=True` (or construct CalibrationStore directly) when another process
    may have written the file -- e.g. the GUI wants to show what the follower
    just recorded.
    """
    global _default_store
    if _default_store is None or reload or Path(path) != _default_store.path:
        _default_store = CalibrationStore(path)
    return _default_store


def record_manual_nudge(track_key: str, app_id: str, delta: float,
                        note: str = "") -> CalibEntry:
    """Add a manual nudge (see CalibrationStore.record_manual_nudge)."""
    return load_calibration(reload=True).record_manual_nudge(
        track_key, app_id, delta, note)


def set_manual_offset(track_key: str, app_id: str, value: float,
                      note: str = "") -> CalibEntry:
    return load_calibration(reload=True).set_manual_offset(
        track_key, app_id, value, note)


def get_effective_offset(track_key: str = "", app_id: str = "",
                         auto_error: float = 0.0) -> EffectiveOffset:
    """Combine closed-loop error and manual preference (see EffectiveOffset)."""
    return load_calibration(reload=True).get_effective_offset(
        track_key, app_id, auto_error)


def manual_offset_for(track_key: str, app_id: str = "") -> float:
    """Effective manual offset for a track (song level, else app level)."""
    return load_calibration(reload=True).manual_offset(track_key, app_id)


# --------------------------------------------------------------------------
# pending-offset channel (GUI / hotkeys -> follower)
# --------------------------------------------------------------------------

def publish_pending_offset(
    offset_sec: float,
    track_key: str,
    app_id: str,
    source: str = "gui",
    path: Path | str = PENDING_FILE,
) -> bool:
    """Ask the follower to apply a manual offset for the running song.

    The GUI and the mpv hotkeys must NOT drive mpv directly: the follower owns
    the player, and a second writer would fight the closed loop (task-1) for
    the same playhead. So the requested offset is published here and the
    follower applies it on its next cycle.

    `source` records who asked ("gui" / "hotkey" / "reset"), which shows up in
    the follower's log and makes the two input paths distinguishable when the
    user reports "I pressed the button and nothing happened".
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f}\t{float(offset_sec):.4f}\t"
                     f"{track_key}\t{app_id}\t{source}\n")
        return True
    except OSError:
        return False


def read_pending_offsets(path: Path | str = PENDING_FILE) -> list[dict]:
    """Read (and keep) pending requests. Malformed lines are skipped."""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            out.append({
                "at": float(parts[0]),
                "offset_sec": float(parts[1]),
                "track_key": parts[2] if len(parts) > 2 else "",
                "app_id": parts[3] if len(parts) > 3 else "",
                "source": parts[4] if len(parts) > 4 else "",
            })
        except ValueError:
            continue
    return out


def consume_pending_offsets(path: Path | str = PENDING_FILE) -> list[dict]:
    """Read the pending requests and truncate the file.

    Truncation happens after a successful read, mirroring how
    mvm_control.lua handles the command file: a request must be applied
    exactly once, or a stuck button would keep re-seeking the video forever.
    """
    items = read_pending_offsets(path)
    p = Path(path)
    try:
        if p.exists():
            p.write_text("", encoding="utf-8")
    except OSError:
        pass
    return items


def clear_pending_offsets(path: Path | str = PENDING_FILE) -> None:
    try:
        p = Path(path)
        if p.exists():
            p.write_text("", encoding="utf-8")
    except OSError:
        pass
