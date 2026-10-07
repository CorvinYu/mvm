# Quick start

## Requirements

| Component | Notes |
|---|---|
| **Python 3.10+** | `numpy` is required for alignment; everything else is stdlib |
| **mpv** | Any recent build. Resolved via `MVM_MPV`, `./bin/mpv-iso/mpv.exe`, or `PATH` |
| **ffmpeg** | Resolved via `MVM_FFMPEG`, `./bin/ffmpeg.exe`, or `PATH` |
| **yt-dlp** | Resolved via `./vendor/ytdlp` (as a Python module) or `./bin/yt-dlp.exe` |
| **Windows 10/11** | SMTC, WASAPI loopback and the Win32 window APIs are Windows-only |

Nothing is hard-coded to a particular machine. If a tool lives somewhere
unusual, point at it explicitly:

```powershell
$env:MVM_MPV    = "C:\tools\mpv\mpv.exe"
$env:MVM_FFMPEG = "C:\tools\ffmpeg\bin\ffmpeg.exe"
```

This project **never touches your own mpv/mpv.net installation**: it always
launches its own copy with `--no-config` and a private `--config-dir`.

## Cookies (needed for bilibili)

bilibili returns **HTTP 412 without a logged-in cookie**, and it is the main
source of PVs for Chinese/Vocaloid songs.

1. Log in to bilibili in your browser.
2. Export cookies in **Netscape format** (e.g. the "Get cookies.txt LOCALLY"
   extension) with only `bilibili.com` cookies.
3. Save as `state/cookies.txt`.

The file must be non-empty and start with `# Netscape HTTP Cookie File`.
Search order: `state/cookies.txt`, then `config/cookies.txt`, then whatever
`MVM_COOKIES` points at.

> **Never commit this file.** It carries your account session. `.gitignore`
> already excludes it — keep it that way.

> Known limitation: `yt-dlp --cookies-from-browser chrome/edge` tends to fail
> (browser database locks / DPAPI), so exporting a file by hand is the reliable
> route. The bundled `login-browser.cmd` + `grab-cookies.cmd` automate it via
> CDP if you prefer.

## Usage

```powershell
cd src
$env:PYTHONIOENCODING='utf-8'

# What can we see, and what would we match? (no playback)
python follow.py probe

# Play one song, then exit
python follow.py once --seconds 8

# Non-Vocaloid repertoire (pop etc.) -- prefer search over VocaDB
python follow.py probe --prefer-search

# Recommended: follow whatever is playing, with fine alignment
python follow.py follow --align

# Restrict to one player (substring match on the app id)
python follow.py follow --app cloudmusic

# Run for a fixed time (debugging)
python follow.py follow --duration 60
```

The follower only shows **video**; your original player keeps playing the audio.

### Alignment, in two stages

| Stage | What it fixes |
|---|---|
| **Coarse** | The music is already partway through, so the video must start there instead of at 0:00 |
| **Fine** (`--align`) | The PV may be a different edit (longer intro etc.), so the exact offset is measured |

Coarse alignment prefers the player's own reported position, but only after
verifying it advances at roughly real time (some players report a frozen or
jumping value). When it is unusable, the follower falls back to how long its own
lookup took — a **lower bound**, since it cannot know how long you had already
been listening.

Fine alignment captures 10 s of system output (WASAPI loopback), downloads the
matching slice of the PV's audio, and cross-correlates them. The correlation
gives an **absolute** position, so the result does not depend on the coarse
guess being right:

```
✓ 已开始播放（起点 58.1s，依据: SMTC 绝对位置 58.1s（推进速率 0.85x））
· 采集 + 下载并行进行（窗口 30s）…
· 互相关对齐：偏移 +18.34s（置信度 1.00，主次峰比 2.77）
· 采集音频在 PV 61.4s；采集 10s 后再过 12.0s => 音乐现在位于 83.5s
↻ 画面已校正到 83.5s
```

A wrong seek is worse than none, so a result is only applied when the
correlation peak is clearly dominant (main/secondary peak ratio ≥ 1.35). When
the PV is a genuinely different recording, no offset exists and alignment
correctly refuses.

### Diagnosing alignment

```powershell
python align_probe.py          # why did alignment succeed/fail for this track?
python align_probe.py --all    # try every candidate; finds the matching version
```

```
--- candidate 1: Bilibili | Original | 199s
    alignment: 互相关对齐：偏移 +4.22s（置信度 1.00，主次峰比 4.24）
    => TRUSTWORTHY: this PV matches the playing audio.
```

### Testing individual modules

```powershell
python smtc.py                        # what SMTC reports right now
python vocadb.py "Senbonzakura"       # query VocaDB
python matcher.py "歌名" "艺术家" 198  # inspect candidate ranking
python capture.py 5                   # record 5s of system audio
python align.py ref.wav pv.wav        # cross-correlate two files
python player.py --resolve <url>      # resolve a stream URL only
python selftest.py                    # full self-test (opens an mpv window)
```

## How it works

```
SMTC (any player's now-playing state)
   ↓  title / artist / duration / position (position may be unreliable)
Matcher
   ├─ 1. VocaDB API       → multi-platform, multi-version PVs
   └─ 2. yt-dlp search    → bilisearch fallback
   ↓  ranked by duration proximity, song type (original vs cover), reachability
Alignment (optional)
   ├─ WASAPI loopback capture of what is actually playing
   ├─ slice of the PV's audio, cross-correlated (FFT)
   └─ accepted only when the peak margin is convincing
Player
   ├─ one long-lived mpv, songs switched via loadfile (no window restart)
   ├─ control channel = a Lua script polling a command file
   └─ muted video; your player keeps the audio
```

**Why duration is a strong signal**: SMTC reported 247.6 s and the correct
VocaDB entry said 248 s — a 0.4 s match identified the version while other
candidates for the same title were 7-50 s off.

**Why the window is not restarted per song**: mpv stays alive and each switch is
a single `loadfile ... replace`, so the process id and the window are stable.

**Why a command file instead of IPC**: `--input-ipc-server` needs a Windows
named pipe (unavailable in some sandboxes) and `--input-terminal` does not read a
redirected stdin — commands written there were silently dropped. A Lua timer
polling a plain file needs neither pipes nor sockets.

## Environment notes

| Item | Behaviour |
|---|---|
| **Player position** | Not every player reports it honestly. Some freeze the value mid-track, which is why it is probed for an advancing rate rather than trusted blindly |
| **bilibili** | Works with cookies. Needs `Referer` **and** a browser `User-Agent`, supplied together — the CDN returns 403/400 otherwise |
| **YouTube** | Usually bot-gated here; treated as unreachable and excluded rather than selected and failed |
| **NicoNico** | yt-dlp resolves the HLS URL, but the CDN connection is reset in some networks |
| **Multiple players** | SMTC reports the same app id for two instances, so use `--app` to disambiguate |

> The most important lesson from building this: **an API being reachable does
> not mean its media stream is reachable.** `yt-dlp --list-formats` succeeding
> says nothing about whether playback will work.

## Self-test

```powershell
python selftest.py
```

Covers SMTC reading, scoring, VocaDB, search fallback, isolated mpv playback,
alignment maths on synthetic signals, WASAPI capture, the command channel,
single-instance locking, and the one-window invariant.

> It opens a real mpv window and records system audio, so run it when that is
> acceptable. Some checks need network and are skipped when it is unavailable.
