"""monitor_window.py -- sample the MVM window geometry over time.

Used to diagnose a real report: "a big window appears, closes after ~10s, then
a small window appears". Guessing was not productive, so this records the
actual sequence of window rectangles (and process/PID changes) so the
transition can be read off directly.

Run:  python monitor_window.py [seconds]
"""

from __future__ import annotations

import subprocess
import sys
import time

PS = (
    "Add-Type @\"\n"
    "using System;using System.Runtime.InteropServices;\n"
    "public class W{"
    "[DllImport(\"user32.dll\")]public static extern bool GetWindowRect(IntPtr h,out R r);"
    "public struct R{public int L,T,Rr,B;}}\n"
    "\"@;\n"
    "Get-Process mpv -ErrorAction SilentlyContinue | ForEach-Object {"
    "  $r=New-Object W+R;"
    "  [W]::GetWindowRect($_.MainWindowHandle,[ref]$r)|Out-Null;"
    "  if($_.MainWindowHandle -ne 0){"
    "    \"$($_.Id)|$($r.Rr-$r.L)x$($r.B-$r.T)|$($r.L),$($r.T)\""
    "  } else { \"$($_.Id)|no-window|\" }"
    "}"
)


def sample() -> list[str]:
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                             capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]


def main() -> int:
    total = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    start = time.time()
    prev: list[str] = []
    print(f"{'t':>6}  windows (pid|size|pos)")
    print("-" * 60)
    while time.time() - start < total:
        cur = sample()
        if cur != prev:
            t = time.time() - start
            if not cur:
                print(f"{t:6.1f}  (none)")
            else:
                for i, line in enumerate(cur):
                    prefix = f"{t:6.1f}  " if i == 0 else " " * 8
                    print(f"{prefix}{line}")
            prev = cur
        time.sleep(0.25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
