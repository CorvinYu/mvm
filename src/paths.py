"""paths.py -- locate the external tools this project depends on.

WHY THIS MODULE EXISTS
    The first version of this project hard-coded absolute paths from the
    machine it was developed on, e.g.

        FFMPEG = Path(r"D:\\software\\ffmpeg-8.1.1-essentials_build\\bin\\ffmpeg.exe")

    That made the code unusable anywhere else (and leaked a local directory
    layout into a public repository). Everything machine-specific now resolves
    in this order:

      1. an explicit environment variable (MVM_FFMPEG, MVM_MPV, MVM_YTDLP)
      2. a copy bundled under ./bin (the layout this project uses itself)
      3. whatever is on PATH

    Nothing here raises at import time: a missing tool is reported where it is
    actually used, so the rest of the program (matching, SMTC reading, tests
    that do not need that tool) keeps working.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _from_env(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip().strip('"')
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def find_tool(
    env_var: str,
    bundled: list[Path],
    executables: list[str],
) -> Path | None:
    """Resolve one external tool. Returns None when it cannot be found."""
    explicit = _from_env(env_var)
    if explicit:
        return explicit
    for candidate in bundled:
        if candidate.exists():
            return candidate
    for exe in executables:
        found = shutil.which(exe)
        if found:
            return Path(found)
    return None


# A Path that never exists. Callers written as `if not TOOL.exists():` keep
# working unchanged when the tool is missing, instead of raising on None.
MISSING = Path(os.devnull + ".mvm-missing-tool")


def find_ffmpeg() -> Path:
    return find_tool(
        "MVM_FFMPEG",
        [ROOT / "bin" / "ffmpeg.exe", ROOT / "bin" / "ffmpeg" / "ffmpeg.exe"],
        ["ffmpeg"],
    ) or MISSING


def find_mpv() -> Path:
    return find_tool(
        "MVM_MPV",
        [
            ROOT / "bin" / "mpv-iso" / "mpv.exe",
            ROOT / "bin" / "mpv.exe",
        ],
        ["mpv", "mpv.exe"],
    ) or MISSING


def find_ffprobe() -> Path | None:
    return find_tool(
        "MVM_FFPROBE",
        [ROOT / "bin" / "ffprobe.exe"],
        ["ffprobe"],
    )
