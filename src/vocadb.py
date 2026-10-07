"""VocaDB client -- the primary PV data source.

Why VocaDB (verified by real API calls during research):
    A single song returns MULTIPLE PVs across Nico / YouTube / Bilibili, each
    carrying:
      - service  (platform -> decides which stream adapter to use)
      - pvType   (Original / Reprint / Other -> official vs reupload)
      - author   (PV author -> distinguishes different PV versions)
      - pvId     (platform-native id, directly feedable to yt-dlp)
      - url      (full URL -- VocaDB provides it, we do NOT build it ourselves)
    Plus songType + originalVersionId give the original<->cover version chain.

Environment gotcha (measured):
    PowerShell's Invoke-RestMethod and curl CANNOT reach vocadb.net from this
    machine (TLS handshake failure), but Python's urllib works fine.
    => Always use urllib here, never shell out to curl/pwsh for VocaDB.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API_BASE = "https://vocadb.net/api"

# Identify ourselves politely; VocaDB is a community-run service.
USER_AGENT = "music-video-matcher/0.1 (personal tool)"

# Requests time out quickly -- a slow lookup must not stall playback.
DEFAULT_TIMEOUT = 20

# Cache directory (keyed by query) to avoid re-querying on every song change.
_CACHE_DIR = Path(__file__).resolve().parent.parent / "state" / "vocadb_cache"


@dataclass
class PV:
    """One promotional video attached to a song."""

    service: str      # NicoNicoDouga / Youtube / Bilibili / ...
    pv_id: str
    url: str          # full URL as provided by VocaDB
    pv_type: str      # Original / Reprint / Other
    author: str
    disabled: bool = False
    length_sec: int = 0
    thumb_url: str = ""

    @property
    def is_usable(self) -> bool:
        return bool(self.url) and not self.disabled

    def platform_label(self) -> str:
        return {
            "NicoNicoDouga": "NicoNico",
            "Youtube": "YouTube",
            "Bilibili": "Bilibili",
            "SoundCloud": "SoundCloud",
            "Piapro": "Piapro",
            "Vimeo": "Vimeo",
            "Bandcamp": "Bandcamp",
        }.get(self.service, self.service)


@dataclass
class Song:
    """A VocaDB song entry with its PVs."""

    song_id: int
    name: str
    artist: str
    length_sec: int
    song_type: str
    original_version_id: int | None
    pvs: list[PV] = field(default_factory=list)
    publish_date: str = ""

    @property
    def usable_pvs(self) -> list[PV]:
        return [p for p in self.pvs if p.is_usable]

    def duration_delta(self, duration_sec: float) -> float:
        """Absolute difference between this entry's length and a reference."""
        if not self.length_sec:
            return float("inf")
        return abs(self.length_sec - duration_sec)


class VocaDBSource:
    """Thin VocaDB API client with a small on-disk cache."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, use_cache: bool = True) -> None:
        self.timeout = timeout
        self.use_cache = use_cache
        # Verified necessary in this environment: plain urllib + default context
        # is what actually connected. Fall back to unverified only if needed.
        self._ctx = ssl.create_default_context()
        if use_cache:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------- HTTP ----------------

    def _get(self, path: str, params: dict, cache_key: str | None = None) -> dict | None:
        if self.use_cache and cache_key:
            cached = _CACHE_DIR / f"{cache_key}.json"
            if cached.exists():
                try:
                    return json.loads(cached.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass  # corrupt cache -> just refetch

        url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

        if self.use_cache and cache_key and data:
            try:
                (_CACHE_DIR / f"{cache_key}.json").write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                pass
        return data

    # ---------------- Public API ----------------

    def search_songs(self, query: str, max_results: int = 8, get_pvs: bool = True) -> list[Song]:
        """Search songs by free text (title, optionally with artist)."""
        params = {
            "query": query,
            "maxResults": max_results,
            "lang": "Default",
            "fields": "PVs,Artists" if get_pvs else "Artists",
        }
        key = _safe_key(f"search_{query}_{max_results}")
        data = self._get("songs", params, cache_key=key)
        if not data:
            return []
        # NOTE: `totalCount` is unreliable on this API (observed 0 while items
        # were non-empty), so we always judge by the items array itself.
        return [self._to_song(it) for it in (data.get("items") or [])]

    def get_song(self, song_id: int) -> Song | None:
        """Fetch a single song with full PV data."""
        data = self._get(
            f"songs/{song_id}",
            {"fields": "PVs,Artists,Names", "lang": "Default"},
            cache_key=_safe_key(f"song_{song_id}"),
        )
        return self._to_song(data) if data else None

    # ---------------- Mapping ----------------

    @staticmethod
    def _to_song(item: dict) -> Song:
        return Song(
            song_id=int(item.get("id") or 0),
            name=str(item.get("name") or ""),
            artist=str(item.get("artistString") or ""),
            length_sec=int(item.get("lengthSeconds") or 0),
            song_type=str(item.get("songType") or ""),
            original_version_id=item.get("originalVersionId"),
            publish_date=str(item.get("publishDate") or ""),
            pvs=[VocaDBSource._to_pv(p) for p in (item.get("pvs") or [])],
        )

    @staticmethod
    def _to_pv(raw: dict) -> PV:
        return PV(
            service=str(raw.get("service") or ""),
            pv_id=str(raw.get("pvId") or ""),
            url=str(raw.get("url") or ""),
            pv_type=str(raw.get("pvType") or ""),
            author=str(raw.get("author") or ""),
            disabled=bool(raw.get("disabled")),
            length_sec=int(raw.get("length") or 0),
            thumb_url=str(raw.get("thumbUrl") or ""),
        )


def _safe_key(text: str) -> str:
    """Filesystem-safe cache key."""
    keep = "-_"
    return "".join(c if (c.isalnum() or c in keep) else "_" for c in text)[:120]


if __name__ == "__main__":
    # Quick manual check: python vocadb.py "song name"
    import sys

    q = " ".join(sys.argv[1:]) or "Senbonzakura"
    src = VocaDBSource()
    songs = src.search_songs(q)
    print(f"query={q!r} -> {len(songs)} song(s)\n")
    for s in songs:
        print(f"[{s.song_id}] {s.name} | {s.artist} | {s.length_sec}s | {s.song_type}")
        if s.original_version_id:
            print(f"      original version id = {s.original_version_id}")
        for pv in s.usable_pvs:
            print(f"      - {pv.platform_label():<10} {pv.pv_type:<10} {pv.author!r}")
            print(f"        {pv.url}")
