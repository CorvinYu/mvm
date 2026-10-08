"""Lead launcher: run the follower with a UTF-8 log file.

WHY NOT POWERSHELL REDIRECTION: `python follow.py ... *> file` on this host
writes the log as UTF-16 with mojibake (PowerShell re-encodes the child's
stdout), which made every log line unreadable for both me and the user. Running
the follower in-process with its stdout wrapped in a UTF-8 file keeps the log
plain UTF-8, so the diagnostics stay usable.

This is a THIN launcher: it changes nothing about the follower's behaviour, it
only redirects the console streams. Used for manual/live verification runs.

Usage:  python tools/run_follow_utf8.py [--align] [extra args...]
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
LOG = ROOT / "state" / "_evidence" / "follow_live.log"

sys.path.insert(0, str(SRC))


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:] or ["follow", "--align"]

    # Wrap BOTH streams in the same UTF-8 file so ordering is preserved and
    # nothing is lost to the host console encoding.
    fh = open(LOG, "w", encoding="utf-8", buffering=1)
    sys.stdout = fh
    sys.stderr = fh

    import follow as F

    # follow.main() reads sys.argv itself, so set it rather than passing args.
    sys.argv = ["follow.py", *args]
    print(f"=== launcher: args={args} log={LOG} ===", flush=True)
    return F.main()


if __name__ == "__main__":
    sys.exit(main())
