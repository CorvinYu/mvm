"""align_probe.py -- check whether fine alignment works for the CURRENT song.

Why this exists
---------------
`follow --align` refuses to apply an offset unless the correlation is clearly
trustworthy, which is correct but opaque: when it refuses you cannot tell
whether (a) the alignment code is broken, (b) the audio capture is wrong, or
(c) the matched PV genuinely is a different edit/version.

This tool answers that directly:
    1. capture what the system is playing right now
    2. pull the PV's audio from the same position
    3. report the correlation quality, plus plain audio statistics
    4. try every candidate, so you can see whether ANY of them matches

Usage:
    python align_probe.py            # use the current SMTC track
    python align_probe.py --all      # try all matched candidates
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from align import extract_audio_track, estimate_delay          # noqa: E402
from capture import record                                     # noqa: E402
from matcher import Matcher                                    # noqa: E402
from player import resolve_stream_url                          # noqa: E402
from smtc import pick_session, read_sessions                   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "state" / "_align_work"


def audio_stats(path: Path) -> dict:
    """Basic loudness stats, to distinguish silence from real music."""
    import array
    import wave

    import numpy as np

    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
            ch = w.getnchannels()
    except (wave.Error, OSError):
        return {}
    a = array.array("h")
    a.frombytes(frames)
    if ch > 1:
        a = array.array("h", a[::ch])
    x = np.asarray(a, dtype=float) / 32768.0
    if x.size == 0:
        return {}
    X = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freqs = np.fft.rfftfreq(x.size, 1 / rate)
    centroid = float((X * freqs).sum() / (X.sum() or 1))
    return {
        "seconds": round(x.size / rate, 1),
        "rms": round(float(np.sqrt((x ** 2).mean())), 4),
        "peak": round(float(abs(x).max()), 3),
        "centroid_hz": round(centroid),
    }


def main() -> int:
    use_all = "--all" in sys.argv

    sessions = read_sessions()
    session = pick_session(sessions)
    if session is None:
        print("No playable session found. Start some music first.")
        return 1

    print("Now playing:", session.summary())
    position = session.position_sec

    matcher = Matcher()
    result = matcher.match(session.title, session.artist, session.duration_sec)
    if not result.candidates:
        print("No PV candidates found.")
        return 1

    print(f"Candidates: {len(result.candidates)} ({result.note or 'VocaDB'})")

    WORK.mkdir(parents=True, exist_ok=True)

    print("\n1) Capturing system output (10s)...")
    live = record(WORK / "probe_live.wav", seconds=10.0)
    if not live:
        print("   FAILED: capture unavailable")
        return 1
    print("   live:", audio_stats(live))

    candidates = result.candidates if use_all else result.candidates[:1]
    for i, cand in enumerate(candidates, 1):
        print(f"\n--- candidate {i}: {cand.describe()}")
        print(f"    {cand.url}")
        try:
            stream = resolve_stream_url(cand.url, want="audio")
        except Exception as exc:  # noqa: BLE001
            print(f"    resolve failed: {str(exc)[:110]}")
            continue

        pv = extract_audio_track(
            stream, WORK / f"probe_pv{i}.wav",
            start=max(0.0, position - 8.0), duration=22.0,
        )
        if not pv:
            print("    audio extraction failed")
            continue

        pv_stats = audio_stats(pv)
        print("    pv:  ", pv_stats)

        res = estimate_delay(live, pv, max_lag_sec=12.0)
        print(f"    alignment: {res.note}")

        # A direct interpretation to save guesswork.
        if res.trustworthy:
            print("    => TRUSTWORTHY: this PV matches the playing audio.")
        else:
            lr, pr = audio_stats(live).get("rms", 0), pv_stats.get("rms", 0)
            if pr and lr and (pr / lr > 3 or lr / pr > 3):
                print("    => REJECTED: loudness differs a lot; probably a "
                      "different master/edit.")
            else:
                print("    => REJECTED: audio content differs; the matched PV "
                      "is likely a different version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
