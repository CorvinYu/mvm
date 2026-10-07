"""demo_follow.py -- prove the follow loop works, without network/cookies.

Real blockers (bilibili needs a cookie, niconico CDN unreachable here) would
otherwise make it impossible to show that the *follow logic* is correct. So this
script monkey-patches only the "resolve stream URL" step to hand back a local
video file, and leaves SMTC + matching + the isolated mpv untouched.

What it demonstrates end to end:
    real SMTC reading  ->  real matching  ->  real isolated mpv window

Run:  python demo_follow.py [seconds]
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

# Resolved from MVM_FFMPEG, ./bin, or PATH -- never hard-coded to one machine.
from paths import find_ffmpeg  # noqa: E402

FFMPEG = find_ffmpeg()

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def make_fake_pv(path: Path, label: str = "PV") -> None:
    """Render a local stand-in 'PV' video so the window shows something."""
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"drawtext=text='{label}':fontsize=64:fontcolor=white:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.5:boxborderw=20"
    )
    subprocess.run(
        [str(FFMPEG), "-y", "-f", "lavfi",
         "-i", "testsrc=size=854x480:rate=25:duration=120",
         "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, timeout=180,
    )


def main() -> int:
    print("=" * 62)
    print("演示：跟随本机播放器 -> 匹配 PV -> 隔离 mpv 静音播放画面")
    print("=" * 62)

    # --- patch the resolve step to use a local file ---
    import player

    fake = STATE / "_demo_pv.mp4"
    make_fake_pv(fake, "MVM DEMO PV")
    if not fake.exists():
        print("无法生成演示视频（缺 ffmpeg）")
        return 1

    original_resolve = player.resolve_stream_url
    resolved_calls: list[str] = []

    def fake_resolve(url: str, want: str = "video", timeout: int = 90) -> str:
        resolved_calls.append(url)
        print(f"   [演示] 本应取流: {url}")
        print(f"   [演示] 改用本地视频: {fake.name}")
        return str(fake)

    # follow.py imported resolve_stream_url by name, so patch it there too.
    import follow
    follow.resolve_stream_url = fake_resolve
    player.resolve_stream_url = fake_resolve

    try:
        f = follow.Follower(matcher=follow.Matcher(), verbose=True)
        print(f"\n开始跟随 {DURATION} 秒（切歌会自动切换画面）…\n")
        f.run(duration_sec=DURATION)
    finally:
        player.resolve_stream_url = original_resolve

    print(f"\n共尝试解析 {len(resolved_calls)} 个候选 URL")
    for u in resolved_calls:
        print(f"  - {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
