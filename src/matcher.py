"""PV matcher -- decide WHICH video to play for the currently playing song.

The hard part of this project is not "find a video", it is "find the video that
matches the version the user is actually listening to". The same song has many
PVs (original / cover / different PV authors / different platforms), and they
can differ in length by tens of seconds.

Strategy (ordered, cheapest and most reliable first):

  1. VocaDB search by title      -> authoritative, structured multi-PV data.
  2. Filter out disabled PVs     -> VocaDB marks dead/removed videos.
  3. Score by DURATION proximity -> the strongest signal we have. Measured in
     practice: SMTC reported 247.6s for a track whose VocaDB entry said 248s.
     A 0.4s delta correctly identified the version.
  4. Prefer platform order       -> configurable; Bilibili first for CN users
     because it is reachable without a JP proxy.
  5. Fall back to yt-dlp search  -> for songs VocaDB does not cover (the
     "not limited to V曲" requirement). Duration is still used to rank.

Everything returns a ranked list so the caller (or the user) can switch versions.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from vocadb import PV, Song, VocaDBSource

ROOT = Path(__file__).resolve().parent.parent
YTDLP = ROOT / "bin" / "yt-dlp.exe"

# Cookie files, most specific first. bilibili returns HTTP 412 without one.
# NEVER commit a real cookies.txt: it holds your account session. Point
# MVM_COOKIES at an existing file instead of adding machine-specific paths here.
COOKIE_CANDIDATES = [
    ROOT / "state" / "cookies.txt",
    ROOT / "config" / "cookies.txt",
]

_extra_cookies = os.environ.get("MVM_COOKIES", "").strip().strip('"')
if _extra_cookies:
    COOKIE_CANDIDATES.append(Path(_extra_cookies))


def _find_cookies() -> Path | None:
    """First non-empty cookies file, or None.

    Returns a THROWAWAY COPY, never the real file: yt-dlp rewrites whatever it
    is given via --cookies when the session ends, which was measured to wipe
    our captured login cookies (SESSDATA etc.) down to two anonymous ones.
    """
    for path in COOKIE_CANDIDATES:
        try:
            if not (path.exists() and path.stat().st_size > 0):
                continue
            workdir = ROOT / "state" / ".cookie-jar"
            workdir.mkdir(parents=True, exist_ok=True)
            dest = workdir / path.name
            data = path.read_bytes()
            if not dest.exists() or dest.read_bytes() != data:
                dest.write_bytes(data)
            return dest
        except OSError:
            continue
    return None

# Songs whose length differs by more than this are treated as a different edit.
DURATION_TOLERANCE_SEC = 20

# Minimum score for a candidate to be played at all. The duration term is worth
# up to 100 and the platform term up to 30, so anything below this means the
# duration did not agree and only platform/type bonuses were earned -- i.e. we
# are looking at an unrelated video.
#
# Measured: a genuine match scores 100+ (duration agrees); the unrelated
# fallback results scored exactly 5.0 (the floor). 40 sits well clear of both.
MIN_ACCEPTABLE_SCORE = 40.0

# bilibili search is flaky: identical queries returned 0/0/3/0/3 results in a
# row, so we retry with a small backoff before concluding "no results".
SEARCH_RETRIES = 3
SEARCH_RETRY_DELAY = 1.5

# Default platform preference.
#
# Bilibili first: measured to work from CN without a JP proxy, and it is the
# only platform that reliably streams here. YouTube is deliberately pushed down
# -- it is not merely "less preferred", it is currently unusable: every attempt
# failed with "Sign in to confirm you're not a bot" unless cookies are supplied.
# Ranking it first meant songs failed outright while a perfectly good bilibili
# PV sat further down the candidate list.
#
# Niconico sits low too: yt-dlp resolves its HLS URL, but the CDN connection is
# reset in this environment.
DEFAULT_PLATFORM_ORDER = [
    "Bilibili",
    "NicoNicoDouga",
    "Piapro",
    "SoundCloud",
    "Vimeo",
    "Youtube",
]

# Platforms that resolve metadata but cannot actually stream in this
# environment. Ranked last AND penalised, so a marginally better duration match
# on one of them cannot outrank a working source.
#   * Youtube  -- "Sign in to confirm you're not a bot" on every attempt.
#   * NicoNicoDouga -- HLS URL resolves, but the CDN connection is reset.
UNREACHABLE_PLATFORMS = {"Youtube", "NicoNicoDouga"}

# Case-insensitive view of the above. VocaDB stores the service as "Youtube"
# but PV.platform_label() renders it "YouTube" -- comparing the label against
# the raw set (as an earlier version did) never matched, so the hard exclusion
# silently did nothing and a duration-perfect YouTube PV still got selected.
_UNREACHABLE_LOWER = {p.lower() for p in UNREACHABLE_PLATFORMS}


def _is_unreachable(platform_or_service: str) -> bool:
    """True for platforms that cannot stream in this environment."""
    return (platform_or_service or "").strip().lower() in _UNREACHABLE_LOWER


@dataclass
class Candidate:
    """A ranked video candidate for a song."""

    url: str
    platform: str
    pv_type: str
    author: str
    score: float
    duration_sec: float
    source: str          # "vocadb" | "search"
    title: str = ""
    song_id: int | None = None
    reason: str = ""

    def describe(self) -> str:
        bits = [f"{self.platform}"]
        if self.pv_type:
            bits.append(self.pv_type)
        if self.author:
            bits.append(self.author[:28])
        if self.duration_sec:
            bits.append(f"{self.duration_sec:.0f}s")
        bits.append(f"score={self.score:.1f}")
        return " | ".join(bits)


@dataclass
class MatchResult:
    """Outcome of a match attempt."""

    query: str
    reference_duration: float
    candidates: list[Candidate]
    song: Song | None = None
    note: str = ""

    @property
    def best(self) -> Candidate | None:
        """Highest-scoring candidate, or None if nothing is good enough.

        Returning None matters: measured case "转一圈，画个圆" (130s) where the
        search fallback produced five unrelated videos, the closest still 94s
        off, all scoring the 5.0 floor. Playing any of them shows a random
        video, which is worse than showing nothing -- so a weak best match is
        treated as no match.
        """
        if not self.candidates:
            return None
        top = self.candidates[0]
        if top.score < MIN_ACCEPTABLE_SCORE:
            return None
        return top

    @property
    def rejected_reason(self) -> str:
        """Why `best` is None, for logging."""
        if not self.candidates:
            return "无候选"
        top = self.candidates[0]
        return (f"最佳候选仅 {top.score:.1f} 分（阈值 {MIN_ACCEPTABLE_SCORE}），"
                f"时长差 {abs(top.duration_sec - self.reference_duration):.0f}s")


class Matcher:
    """Finds and ranks PV candidates for a song."""

    def __init__(
        self,
        platform_order: list[str] | None = None,
        tolerance: int = DURATION_TOLERANCE_SEC,
        vocadb: VocaDBSource | None = None,
    ) -> None:
        self.platform_order = platform_order or DEFAULT_PLATFORM_ORDER
        self.tolerance = tolerance
        self.vocadb = vocadb or VocaDBSource()

    # ---------------- Scoring ----------------

    def _duration_score(self, candidate_len: float, reference_len: float) -> float:
        """Higher is better. Exact match scores 100, beyond tolerance scores 0."""
        if candidate_len <= 0 or reference_len <= 0:
            return 25.0  # unknown length: neutral, neither reward nor punish
        delta = abs(candidate_len - reference_len)
        if delta <= 2:
            return 100.0
        if delta >= self.tolerance:
            return 0.0
        # Linear falloff between 2s and tolerance.
        return 100.0 * (1.0 - (delta - 2) / (self.tolerance - 2))

    def _platform_score(self, service: str) -> float:
        try:
            idx = self.platform_order.index(service)
        except ValueError:
            return 5.0
        # First choice 30, then 24, 18, ... never below 6.
        score = max(30.0 - idx * 6.0, 6.0)
        # Explicit penalty for platforms known to be unreachable here. Without
        # it, a slightly better duration match on YouTube (100 vs 97 duration
        # points) outweighed the platform gap and ranked YouTube first -- then
        # every attempt burned ~3s failing the bot gate before falling through.
        # Measured: "完美劲敌" ranked YouTube 176 over Bilibili 173 this way.
        if _is_unreachable(service):
            score -= 40.0
        return score

    def _pv_type_score(self, pv_type: str) -> float:
        # "Original" here means the video upload is the canonical PV, NOT that
        # the song is the original. Song originality is handled separately by
        # _song_type_score -- confusing the two is exactly how a fan cover got
        # picked over the official PV.
        return {"Original": 10.0, "Reprint": 4.0, "Other": 2.0}.get(pv_type, 3.0)

    def _song_type_score(self, song_type: str) -> float:
        """Prefer the ORIGINAL version of a song over covers/remixes.

        Measured failure this fixes: searching 九九八十一 returned 7 candidates
        and the top pick was a fan cover (VocaDB songType="Cover"), while the
        official entry ("Original", song_id 115466) sat at #3 -- only 3 points
        behind. Every cover had pvType="Original", so the PV-level score could
        not distinguish them; only songType can.

        VocaDB songType values: Original / Cover / Remix / Remaster / Other /
        Instrumental / Mashup / MusicPV / DramaPV / Arrangement / Unspecified.
        """
        normalized = (song_type or "").strip().lower()
        if normalized == "original":
            return 60.0          # decisive: beats the platform preference gap
        if normalized in ("remaster", "reprint"):
            return 10.0
        if normalized in ("cover", "arrangement"):
            return -35.0         # push fan covers well below the original
        if normalized in ("remix", "mashup", "instrumental"):
            return -20.0
        if normalized in ("musicpv", "dramapv", "other", "unspecified", ""):
            return 0.0
        return 0.0

    # ---------------- VocaDB path ----------------

    def match_vocadb(self, title: str, artist: str, duration_sec: float) -> MatchResult:
        """Search VocaDB and rank its PVs.

        Note: measured behaviour -- appending the artist to the query can turn a
        hit into a miss ("保持距离" hit, "保持距离 洛天依" did not). So we search
        by title first and use the artist only to re-rank.
        """
        songs = self.vocadb.search_songs(title, max_results=8)

        if not songs and artist:
            # Retry with a cleaned title (streaming services often append tags).
            songs = self.vocadb.search_songs(_clean_title(title), max_results=8)

        # Rank songs: duration agreement first, then artist agreement.
        artist_key = (artist or "").split(",")[0].strip().lower()

        def song_score(s: Song) -> tuple[float, float]:
            dur = self._duration_score(s.length_sec, duration_sec)
            art = 1.0 if artist_key and artist_key in s.artist.lower() else 0.0
            return (dur, art)

        songs.sort(key=song_score, reverse=True)

        candidates: list[Candidate] = []
        for song in songs:
            # Only consider songs that are plausible length-wise; otherwise we
            # would happily play a 90-second radio edit for a 4-minute track.
            if duration_sec > 0 and song.length_sec > 0:
                if abs(song.length_sec - duration_sec) > self.tolerance:
                    continue

            for pv in song.usable_pvs:
                # Hard-exclude platforms that resolve metadata but CANNOT stream
                # here (YouTube/Nico: measured -- YouTube CDN connection fails /
                # bot-gated, Nico CDN reset). A duration-perfect YouTube match
                # still scored 130 and got selected, then mpv failed to open
                # it ("finished playback, loading failed") and the user saw a
                # black window. Penalising is not enough when it is the only
                # candidate -- it must not be selectable at all.
                if _is_unreachable(pv.platform_label()) or _is_unreachable(pv.service):
                    continue
                score = (
                    self._duration_score(pv.length_sec or song.length_sec, duration_sec)
                    + self._platform_score(pv.service)
                    + self._pv_type_score(pv.pv_type)
                    + self._song_type_score(song.song_type)
                )
                candidates.append(
                    Candidate(
                        url=pv.url,
                        platform=pv.platform_label(),
                        pv_type=pv.pv_type,
                        author=pv.author,
                        score=score,
                        duration_sec=float(pv.length_sec or song.length_sec),
                        source="vocadb",
                        title=song.name,
                        song_id=song.song_id,
                        reason=f"{song.name} ({song.song_type})",
                    )
                )

        candidates.sort(key=lambda c: c.score, reverse=True)

        best_song = songs[0] if songs else None
        note = "" if candidates else "VocaDB 无长度匹配的条目"
        return MatchResult(
            query=title,
            reference_duration=duration_sec,
            candidates=candidates,
            song=best_song,
            note=note,
        )

    # ---------------- yt-dlp search fallback ----------------

    def search_ytdlp(self, query: str, duration_sec: float, platform: str = "ytsearch", limit: int = 5) -> list[Candidate]:
        """Fallback for songs VocaDB does not cover.

        Uses yt-dlp's search pseudo-URLs. Measured working: `ytsearchN:` and
        `bilisearchN:`. The latter also side-steps bilibili's raw search API
        WBI-signature requirement.

        IMPORTANT (measured): `--flat-playlist` must NOT be used here. With it,
        yt-dlp returns entries without durations (and bilibili search then
        throws HTTP 412), which destroys the duration ranking -- the single
        most useful signal for picking the right version. Without it we get
        real durations, e.g. 165.0s and 156.9s for a track SMTC reported as
        158.0s, letting us correctly pick the 156.9s entry.
        """
        if not YTDLP.exists():
            return []

        target = f"{platform}{limit}:{query}"
        cmd = [str(YTDLP), "--no-warnings"]
        cookies = _find_cookies()
        if cookies:
            # NOTE: must be appended in a position-independent way. An earlier
            # version spliced this at a fixed index and silently corrupted argv.
            cmd += ["--cookies", str(cookies)]
        cmd += [
            "--playlist-end", str(limit),
            "--print", "%(id)s\t%(duration)s\t%(title)s\t%(webpage_url)s",
            target,
        ]

        # bilibili's search endpoint is flaky under rapid/successive queries:
        # measured 5 identical calls returning 0, 0, 3, 0, 3 results. Empty
        # results came back in ~1.5s (fast rejection) while successes took 4s+.
        # Retrying turns "no results" into a usable answer most of the time.
        for attempt in range(SEARCH_RETRIES):
            out = self._run_search(cmd, platform)
            if out:
                return out
            if attempt < SEARCH_RETRIES - 1:
                time.sleep(SEARCH_RETRY_DELAY * (attempt + 1))
        return []

    @staticmethod
    def _run_search(cmd: list[str], platform: str) -> list[Candidate]:
        """Run one search invocation and parse its output."""
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=150, encoding="utf-8", errors="replace"
            )
        except (subprocess.TimeoutExpired, OSError):
            return []

        if proc.returncode != 0:
            return []

        out: list[Candidate] = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            vid, dur_raw, title = parts[0], parts[1], parts[2]
            url = parts[3] if len(parts) > 3 and parts[3].startswith("http") else ""
            if not url:
                # Some extractors print NA for webpage_url; rebuild it.
                url = (
                    f"https://www.bilibili.com/video/{vid}"
                    if platform.startswith("bili")
                    else f"https://www.youtube.com/watch?v={vid}"
                )
            try:
                dur = float(dur_raw)
            except (TypeError, ValueError):
                dur = 0.0
            out.append(
                Candidate(
                    url=url,
                    platform="Bilibili" if platform.startswith("bili") else "YouTube",
                    pv_type="",
                    author="",
                    score=0.0,   # scored by the caller (needs the reference length)
                    duration_sec=dur,
                    source="search",
                    title=title,
                    reason="搜索兜底",
                )
            )
        return out

    # ---------------- Combined ----------------

    def match(
        self,
        title: str,
        artist: str = "",
        duration_sec: float = 0.0,
        allow_search_fallback: bool = True,
        prefer_search: bool = False,
    ) -> MatchResult:
        """Full match: VocaDB first, then search fallback.

        `prefer_search`: run the yt-dlp search FIRST and merge VocaDB after.

        This matters more than it looks. VocaDB is a Vocaloid/Utaite database;
        measured example: searching "晴天" (a Jay Chou pop song) returned six
        VocaDB entries that are all *Vocaloid covers* of it, none the original.
        So for non-Vocaloid repertoire the authoritative source is wrong, and
        the search results (ranked by duration) are the better answer.
        """
        clean_title = _clean_title(title)

        if prefer_search and allow_search_fallback:
            result = self._search_first(clean_title, artist, duration_sec)
            if result.candidates:
                return result

        result = self.match_vocadb(clean_title, artist, duration_sec)
        if result.candidates:
            return result

        if not allow_search_fallback:
            return result

        return self._search_first(clean_title, artist, duration_sec, base=result)

    def _search_first(
        self, clean_title: str, artist: str, duration_sec: float, base: MatchResult | None = None
    ) -> MatchResult:
        """Rank search results, optionally merging into an existing result.

        Search order: bilibili first, then YouTube.
        Measured: `bilisearch` works reliably without login, while `ytsearch`
        is blocked by YouTube's bot gate ("Sign in to confirm you're not a
        bot") unless cookies are supplied. So bilibili is the practical default.
        """
        q = f"{clean_title} {artist.split(',')[0].strip()}".strip() if artist else clean_title
        cands = self.search_ytdlp(q, duration_sec, platform="bilisearch")
        # NOTE: deliberately no ytsearch fallback -- YouTube cannot stream in
        # this environment (CDN unreachable / bot-gated, measured), so a
        # YouTube result would always fail to open and waste ~3s per attempt.
        # bilibili search is the only usable fallback source.

        # Score here (not in search_ytdlp) because ranking needs the reference
        # duration, which the search helper does not know about.
        for c in cands:
            c.score = self._duration_score(c.duration_sec, duration_sec) + 5.0
        cands.sort(key=lambda c: c.score, reverse=True)

        result = base or MatchResult(
            query=clean_title, reference_duration=duration_sec, candidates=[]
        )
        if cands:
            result.candidates = cands
            result.note = "搜索兜底（按时长排序）"
        elif base is not None and not result.note:
            result.note = "VocaDB 与搜索均无结果"
        return result


def _clean_title(title: str) -> str:
    """Strip streaming-service decorations that break database lookups.

    e.g. "保持距离 (Live)" / "Song【官方】" / "Song feat. X - Topic"
    """
    t = title.strip()
    for opener, closer in (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】")):
        while opener in t and closer in t:
            start = t.find(opener)
            end = t.find(closer, start)
            if start == -1 or end == -1:
                break
            t = (t[:start] + t[end + 1:]).strip()
    for junk in (" - Topic", "官方", "MV", "PV", "完整版", "高音质"):
        t = t.replace(junk, "")
    return t.strip()


if __name__ == "__main__":
    import sys

    title = sys.argv[1] if len(sys.argv) > 1 else "保持距离"
    artist = sys.argv[2] if len(sys.argv) > 2 else ""
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    m = Matcher()
    r = m.match(title, artist, dur)
    print(f"query={title!r} artist={artist!r} ref_duration={dur}s")
    print(f"note: {r.note or '-'}")
    print(f"candidates: {len(r.candidates)}\n")
    for i, c in enumerate(r.candidates[:6], 1):
        print(f"{i}. {c.describe()}")
        print(f"   {c.url}")
