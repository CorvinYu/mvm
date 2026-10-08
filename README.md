<div align="center">

<img src="assets/icon-256.png" width="160" alt="mvm icon — a girl peeking over a play button labelled mvm">

# mvm — music video matcher

**Find the matching music video for whatever song is playing, and show it in
sync — without touching your audio.**

[Quick start](QUICKSTART.md) · [How it works](#how-it-works) · [Limitations](#honest-limitations)

</div>

---

You keep listening in your normal player (NetEase Cloud Music, 汽水音乐, …).
`mvm` reads what is playing from Windows' system media controls, finds the
corresponding MV/PV, and plays **just the picture** in its own window, seeking it
to the right position so the video lines up with the audio you are hearing.

```
┌─────────────────────┐        ┌──────────────────────────┐
│ your music player   │        │  mvm (muted mpv window)  │
│  audio  ────────────┼───────▶│  video, seeked to match  │
└─────────────────────┘  SMTC  └──────────────────────────┘
          │                              ▲
          └──── WASAPI loopback capture ─┘
                 (cross-correlated to align)
```

## Why it exists

Songs have many videos: the official PV, reuploads, covers, different edits with
different intros. Finding *the right one* — and then lining it up — is the whole
problem this project solves.

## Features

- **Follow mode** — tracks whatever your player is playing, shows only the video
- **Automatic alignment** — claims an absolute position by cross-correlating a
  short capture of your system audio against the video's own audio track
- **Stays aligned** — a closed loop keeps re-checking the two clocks and nudges
  the **playback speed** (±5%) to absorb drift, so the picture follows smoothly
  instead of jumping. A genuine desync (e.g. a stale seek) is confirmed over two
  samples and then resynchronised with one absolute seek
- **Manual alignment** — nudge the picture with hotkeys in the video window
  (`[` `]` = ∓0.1s, `{` `}` = ∓1s, `0` = reset) or with the small control window
  (`python tools/align_control.py`). Your preference is kept **separate** from
  the automatic error, so the loop never "corrects" your nudge away
- **Remembers your preference across songs** — a calibration is stored at two
  levels (this song → this player) and re-applied to later songs
- **Refuses rather than guesses** — a wrong seek is worse than none, so weak or
  ambiguous correlations are rejected instead of applied
- **Player whitelist** — only apps you list can drive it; anything else is
  denied (so a random video in a browser tab will not hijack your screen)
- **Isolated playback** — its own mpv copy and config; your mpv/mpv.net setup is
  never read or modified
- **Single window invariant** — songs switch inside one long-lived process, and a
  leftover window is cleared before a new one is created
- **Window position memory** — reopens where you left it, without stealing focus
- **Self-test** — `python selftest.py` covers the maths, the capture path, the
  control channel and the isolation guarantees

## Measuring sync quality

Do not judge alignment from the log alone — measure it:

```powershell
python sync_probe.py --samples 12 --gap 3
```

It samples the music clock (SMTC) and the video clock repeatedly and reports the
mean offset, the spread, and whether the residual is a constant bias, a real
drift, or just noise. The reliability gate refuses to report numbers when the
player's position is frozen or jumping.

## Quick start

```powershell
pip install numpy          # required for alignment

# optional but recommended: a logged-in bilibili cookie
#   export Netscape-format cookies for bilibili.com to state/cookies.txt

cd src
$env:PYTHONIOENCODING='utf-8'
python follow.py follow --align
```

See **[QUICKSTART.md](QUICKSTART.md)** for requirements, cookie setup, all
commands, and how the alignment works.

## Requirements

- Windows 10/11 (SMTC, WASAPI loopback, Win32 window APIs)
- Python 3.10+ with `numpy`
- `mpv`, `ffmpeg`, and `yt-dlp` — located via environment variables
  (`MVM_MPV`, `MVM_FFMPEG`), a local `./bin`, or `PATH`

## Configuration

`config/follow_whitelist.json` decides which players may drive mvm. Add the
app id that Windows reports, for example:

```json
"apps": [
  { "match": "exact", "pattern": "cloudmusic.exe", "label": "NetEase Cloud Music" },
  { "match": "exact", "pattern": "汽水音乐",        "label": "Soda Music" }
]
```

Matching is case-insensitive and anchored (`re.fullmatch`), so a substring like
`mpv` cannot accidentally match `mpvnet.exe`. Unknown apps are denied by
default, a broken config denies everything, and paused players are ignored —
these are deliberate fail-closed choices.

## Honest limitations

- **A video must exist.** If the exact version you are listening to is not on
  the reachable platforms, there is no correct offset to find. Alignment will
  reject the mismatched candidates rather than fake a sync.
- **Position reporting varies by player.** Some report a frozen or jumping
  playback position; the code probes for it and falls back when it is unusable.
- **Platform reachability is network-dependent.** YouTube and NicoNico are often
  unusable from some networks and are excluded rather than selected and failed.
- **This is a personal-scale tool.** It is not a media library manager,
  downloader, or streaming service client.

## Notes on scope

`mvm` streams video from third-party platforms for personal playback. It does not
download, redistribute or circumvent access controls; where a platform requires
a logged-in session, you supply your own cookie, and what you may do with that
access is governed by that platform's terms.

## License

MIT — see [LICENSE](LICENSE).
