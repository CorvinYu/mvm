"""align.py -- estimate the A/V offset between the playing audio and a PV.

The problem
-----------
"Play the PV for the song that is playing" needs the video's timeline to match
the audio's timeline. Two independent offsets break that:

  1. **Start offset (coarse).** The user is already 90s into the track; the PV
     starts at 0. Fixed by seeking the video to the audio position (SMTC gives
     us `position_sec`), which is a large, obvious shift.

  2. **Edit offset (fine).** Even when both start at 0, a PV and the streaming
     track are different edits. An MV may open with 8s of spoken intro, or the
     streaming version may trim a cold open. Measured in this project: matched
     candidates differed from the reference duration by 1-20s, so intro lengths
     genuinely differ.

This module handles both, and is honest about what each can and cannot do:

  * `coarse_offset()`  -> trust SMTC's position. Always available, no audio
    capture needed. This alone removes the "video is 90 seconds behind" symptom.

  * `estimate_delay()` -> cross-correlate the PV's own audio against a captured
    sample of what the user is actually hearing. This is the only way to get a
    true fine alignment, and it requires an audio capture path (see
    capture.py). Without one, we cannot compute it, and we say so instead of
    guessing.

  * `apply_delay()`    -> feed the result to mpv as `--audio-delay`. Note the
    sign: we keep the video's own audio muted, so the delay is used to shift the
    VIDEO relative to the reference audio timeline; a positive delay means the
    video should start later.
"""

from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

# Resolved from MVM_FFMPEG, ./bin, or PATH -- never hard-coded to one machine.
from paths import find_ffmpeg  # noqa: E402

FFMPEG = find_ffmpeg()

# Cross-correlation needs a decent amount of signal to be trustworthy.
MIN_CORRELATION_SECONDS = 8.0

# If the best correlation peak is weaker than this, refuse to trust it.
# A wrong auto-alignment is worse than none: the user sees the video drift.
MIN_CONFIDENCE = 0.25


@dataclass
class AlignmentResult:
    """Outcome of an alignment attempt."""

    delay_sec: float          # positive => video should start later
    confidence: float         # 0..1
    method: str               # "coarse" | "correlation" | "none"
    note: str = ""

    @property
    def trustworthy(self) -> bool:
        return self.method == "correlation" and self.confidence >= MIN_CONFIDENCE


def coarse_offset(audio_position_sec: float) -> AlignmentResult:
    """The obvious part: match the video's start to the audio's position.

    SMTC reports where the user's player is in the track, so seeking the video
    there synchronises the two timelines assuming identical edits.
    """
    return AlignmentResult(
        delay_sec=0.0,
        confidence=1.0,
        method="coarse",
        note=f"视频定位到 {audio_position_sec:.1f}s（假定与音源剪辑一致）",
    )


# bilibili CDN rejects requests without a Referer: measured "400 Bad Request"
# when the header was missing/malformed. ffmpeg wants CRLF-terminated lines.
STREAM_HEADERS = (
    "Referer: https://www.bilibili.com/\r\n"
    "Origin: https://www.bilibili.com\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36\r\n"
)


def extract_audio_track(media: str, out_wav: Path, start: float = 0.0,
                        duration: float = 30.0, timeout: int = 180) -> Path | None:
    """Decode a slice of a media file/URL to mono 16 kHz WAV for analysis."""
    if not FFMPEG.exists():
        return None
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += [
        # Required for bilibili CDN; harmless for local files.
        "-headers", STREAM_HEADERS,
        "-i", media,
        "-t", f"{duration:.3f}",
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_wav),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not out_wav.exists():
        return None
    return out_wav


def _read_wav(path: Path) -> tuple[list[float], int]:
    """Read a mono 16-bit WAV into floats in [-1, 1]."""
    import array
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    samples = array.array("h")
    samples.frombytes(raw)
    if ch > 1:
        samples = array.array("h", samples[::ch])
    scale = 1.0 / 32768.0
    return [s * scale for s in samples], rate


def estimate_delay(
    reference_audio: Path,
    pv_audio: Path,
    max_lag_sec: float = 12.0,
) -> AlignmentResult:
    """Cross-correlate two audio clips to find the offset between them.

    `reference_audio` is what the user is actually hearing (short capture);
    `pv_audio` is the PV's own audio, taken from the same nominal position.

    Returns the lag that best aligns them. Uses numpy when available; without
    it, reports "none" rather than falling back to something unreliable.
    """
    try:
        import numpy as np
    except ImportError:
        return AlignmentResult(
            0.0, 0.0, "none",
            "需要 numpy 才能做互相关对齐（当前环境未安装）",
        )

    if not reference_audio.exists() or not pv_audio.exists():
        return AlignmentResult(0.0, 0.0, "none", "缺少音频样本")

    try:
        ref, rate_a = _read_wav(reference_audio)
        pv, rate_b = _read_wav(pv_audio)
    except (wave.Error, OSError) as exc:
        return AlignmentResult(0.0, 0.0, "none", f"读取音频失败: {exc}")

    if rate_a != rate_b:
        return AlignmentResult(0.0, 0.0, "none", "采样率不一致")

    ref_np = np.asarray(ref, dtype=np.float64)
    pv_np = np.asarray(pv, dtype=np.float64)
    if ref_np.size < rate_a * MIN_CORRELATION_SECONDS or pv_np.size < rate_a * MIN_CORRELATION_SECONDS:
        return AlignmentResult(
            0.0, 0.0, "none",
            f"样本太短（需 ≥{MIN_CORRELATION_SECONDS:.0f}s）",
        )

    # Remove DC and normalise so loudness differences do not skew the peak.
    ref_np = ref_np - ref_np.mean()
    pv_np = pv_np - pv_np.mean()
    ref_norm = np.linalg.norm(ref_np)
    pv_norm = np.linalg.norm(pv_np)
    if ref_norm == 0 or pv_norm == 0:
        return AlignmentResult(0.0, 0.0, "none", "音频为静音")

    corr = np.correlate(pv_np, ref_np, mode="full")
    corr /= (ref_norm * pv_norm)

    max_lag = int(max_lag_sec * rate_a)
    mid = len(ref_np) - 1
    lo = max(0, mid - max_lag)
    hi = min(len(corr), mid + max_lag + 1)
    window = corr[lo:hi]
    if window.size == 0:
        return AlignmentResult(0.0, 0.0, "none", "相关窗口为空")

    best_idx = int(np.argmax(np.abs(window)))
    peak = float(window[best_idx])
    lag_samples = (lo + best_idx) - mid
    lag_sec = lag_samples / rate_a

    # Confidence = peak height relative to the typical correlation level.
    typical = float(np.median(np.abs(window))) or 1e-9
    confidence = min(1.0, abs(peak) / (typical * 8.0))

    # A high peak alone does NOT mean the two clips are the same recording.
    # Measured: correlating a clip of "MAO!" against the PV audio of an
    # unrelated song still returned a sharp peak (confidence 1.00) and a
    # plausible-looking +1.45s offset -- pure coincidence.
    #
    # So we also check how much better the best lag is than the *rest* of the
    # correlation curve. For genuinely identical audio the peak stands far
    # above every other lag; for unrelated audio the curve is noisy and the
    # second-best peak is comparable.
    peak_abs = abs(peak)
    masked = np.abs(window).copy()
    guard = int(0.5 * rate_a)      # exclude lags within 0.5s of the peak
    lo_g = max(0, best_idx - guard)
    hi_g = min(masked.size, best_idx + guard + 1)
    masked[lo_g:hi_g] = 0.0
    runner_up = float(masked.max()) if masked.size else 0.0
    margin = peak_abs / (runner_up + 1e-9)

    MIN_MARGIN = 1.35
    if margin < MIN_MARGIN:
        return AlignmentResult(
            0.0, round(confidence, 3), "none",
            f"相关曲线无明显主峰（主/次峰比 {margin:.2f}）——"
            f"两个音频很可能不是同一段录音，拒绝自动对齐",
        )

    if confidence < MIN_CONFIDENCE:
        return AlignmentResult(
            0.0, confidence, "none",
            f"相关峰太弱（{confidence:.2f}），不可靠，已放弃自动对齐",
        )

    return AlignmentResult(
        delay_sec=round(lag_sec, 3),
        confidence=round(confidence, 3),
        method="correlation",
        note=f"互相关对齐：偏移 {lag_sec:+.2f}s（置信度 {confidence:.2f}，主次峰比 {margin:.2f}）",
    )


def locate_in_track(
    live_wav: Path,
    track_wav: Path,
    min_position_sec: float = 0.0,
) -> AlignmentResult:
    """Find WHERE in a full track the captured audio sits.

    Returns `delay_sec` = the position (seconds from the start of `track_wav`)
    at which the captured audio begins.

    Why this exists as a separate function:
        `estimate_delay` compares two short clips and is fine when we already
        know roughly where we are. But when the player does not report its
        position (measured: 汽水音乐 always reports 0.2s), we do not know where
        we are at all, and scanning the remote stream window-by-window is slow
        (each window is a separate ffmpeg run against the network).

        Instead we download the PV's audio ONCE and correlate locally. A
        direct O(n*m) correlation would be far too slow for a 3-minute track
        (3.2M x 160k samples), so this uses FFT-based correlation, which is
        effectively instant.
    """
    try:
        import numpy as np
    except ImportError:
        return AlignmentResult(0.0, 0.0, "none", "需要 numpy")

    if not live_wav.exists() or not track_wav.exists():
        return AlignmentResult(0.0, 0.0, "none", "缺少音频样本")

    try:
        live, rate_a = _read_wav(live_wav)
        track, rate_b = _read_wav(track_wav)
    except (wave.Error, OSError) as exc:
        return AlignmentResult(0.0, 0.0, "none", f"读取音频失败: {exc}")

    if rate_a != rate_b:
        return AlignmentResult(0.0, 0.0, "none", "采样率不一致")

    a = np.asarray(live, dtype=np.float64)
    b = np.asarray(track, dtype=np.float64)
    if a.size < rate_a * MIN_CORRELATION_SECONDS or b.size < a.size:
        return AlignmentResult(0.0, 0.0, "none", "样本长度不足")

    a = a - a.mean()
    b = b - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return AlignmentResult(0.0, 0.0, "none", "音频为静音")

    # FFT cross-correlation.
    #
    # Index convention (easy to get wrong -- an earlier version was off by
    # exactly the clip length): the convolution of `track` with reversed `live`
    # satisfies
    #     corr[m] = sum_n track[n] * live[len(live)-1-m+n]
    # so corr is zero-lag at m = len(live)-1, NOT at m = 0. The position in the
    # track where the live clip starts is therefore m - (len(live)-1).
    n_fft = 1 << (len(a) + len(b) - 1).bit_length()
    fa = np.fft.rfft(b, n_fft)
    fb = np.fft.rfft(a[::-1], n_fft)
    corr = np.fft.irfft(fa * fb, n_fft)[: len(b) - len(a) + 1]
    corr = corr / (na * nb)

    zero_lag_index = len(a) - 1
    lo = max(0, int(min_position_sec * rate_a) + zero_lag_index)
    if lo >= corr.size:
        return AlignmentResult(0.0, 0.0, "none", "搜索区间为空")
    window = corr[lo:]

    best = int(np.argmax(np.abs(window))) + lo
    peak = float(corr[best])

    # Margin test: how far does the best peak stand above the next-best one?
    # Needed because unrelated audio also produces a sharp-looking peak.
    guard = int(0.5 * rate_a)
    masked = np.abs(corr).copy()
    masked[max(0, best - guard): best + guard + 1] = 0.0
    runner = float(masked.max()) if masked.size else 0.0
    margin = abs(peak) / (runner + 1e-9)

    typical = float(np.median(np.abs(corr))) or 1e-9
    confidence = min(1.0, abs(peak) / (typical * 8.0))

    MIN_MARGIN = 1.35
    if margin < MIN_MARGIN:
        return AlignmentResult(
            0.0, round(confidence, 3), "none",
            f"相关曲线无明显主峰（主/次峰比 {margin:.2f}）——"
            f"两个音频很可能不是同一段录音，拒绝自动对齐",
        )

    position = (best - zero_lag_index) / rate_a
    return AlignmentResult(
        delay_sec=round(position, 3),
        confidence=round(confidence, 3),
        method="correlation",
        note=f"在 PV 中定位到 {position:.2f}s"
             f"（置信度 {confidence:.2f}，主次峰比 {margin:.2f}）",
    )


def apply_delay(mpv_controller, delay_sec: float) -> bool:
    """Push the computed offset to mpv.

    mpv's `audio-delay` shifts audio relative to video. Since we mute the
    video's own audio anyway, we use it as a pure timeline shift for the
    picture relative to the reference track.
    """
    return mpv_controller.set_property("audio-delay", f"{delay_sec:.3f}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print(__doc__)
        print("用法: python align.py <参考音频.wav> <PV音频.wav>")
        raise SystemExit(2)
    res = estimate_delay(Path(sys.argv[1]), Path(sys.argv[2]))
    print(res)
