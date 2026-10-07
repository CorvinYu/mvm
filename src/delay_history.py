"""delay_history.py -- remember how long a song switch takes, per player/platform.

Why this exists (user requirement A5, "按历史预估启动时差，先粗略对齐"):
    Matching + stream resolution take a variable amount of time (VocaDB plus a
    search fallback can be 10-13s). During that time the music keeps playing,
    so when the video finally starts, the picture is behind by roughly that
    delay. SMTC's position is NOT reliable for every player (汽水音乐 reported
    a constant 0.2s), so we cannot always read where the music is.

    The fix: remember the measured "detected -> playing" delay per
    (player, platform) and use the rolling average as the initial seek target
    for the NEXT song. After fine alignment we correct the exact position and
    feed the correction back, so the history converges on reality.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# How many past samples each (player, platform) bucket keeps (rolling window).
MAX_SAMPLES_PER_BUCKET = 8
MIN_SAMPLES_FOR_ESTIMATE = 2


class DelayHistory:
    """Rolling averages of song-switch latency, persisted to a JSON file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, list[float]] = {}
        self._load()

    # ---------------- persistence ----------------

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                self._data = {
                    k: [float(v) for v in vals if isinstance(v, (int, float))]
                    for k, vals in data.items()
                    if isinstance(vals, list)
                }
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)  # atomic, so a crash cannot corrupt it
        except OSError:
            pass  # persistence is best-effort; losing it only costs accuracy

    # ---------------- record / estimate ----------------

    def record(self, app_id: str, platform: str, delay_sec: float) -> None:
        """Add one measured switch latency to the (app, platform) bucket."""
        if delay_sec <= 0:
            return
        key = self._key(app_id, platform)
        bucket = self._data.setdefault(key, [])
        bucket.append(round(delay_sec, 1))
        del bucket[:-MAX_SAMPLES_PER_BUCKET]  # keep the newest N
        self._save()

    def estimate(self, app_id: str, platform: str) -> float | None:
        """Rolling average for this (app, platform), or None when not enough data."""
        bucket = self._data.get(self._key(app_id, platform)) or []
        if len(bucket) < MIN_SAMPLES_FOR_ESTIMATE:
            return None
        return sum(bucket) / len(bucket)

    def estimate_any(self, app_id: str) -> float | None:
        """Average across every bucket for this app (fallback when platform unknown)."""
        buckets = [
            v for k, v in self._data.items() if k.startswith(app_id + "|")
        ]
        flat = [x for b in buckets for x in b]
        if len(flat) < MIN_SAMPLES_FOR_ESTIMATE:
            return None
        return sum(flat) / len(flat)

    @staticmethod
    def _key(app_id: str, platform: str) -> str:
        return f"{app_id}|{platform}"
