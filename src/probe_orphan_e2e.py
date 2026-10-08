"""Probe: kill the daemon and prove no orphan mpv survives (issue #6's core).

WHAT THIS PROVES
----------------
The user's actual complaint was an mpv window outliving its daemon and being
mistaken for "the whitelist woke it up". This probe reproduces that setup for
real:

  1. start a daemon process that starts an mpv with MVM_PARENT_PID set;
  2. hard-kill the daemon (Stop-Process -Force equivalent: SIGKILL/TerminateProcess)
     so no Python cleanup, no atexit, no `finally` can run;
  3. assert the mpv exits on its own within a few seconds.

`finally`-based cleanup can never cover case 2 -- that is exactly why the guard
lives inside mpv (Lua timer + parent liveness), not in Python.

Run:  python src/probe_orphan_e2e.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# A child that starts one mpv through the REAL player module (so it gets the
# same MVM_PARENT_PID wiring production uses), then sleeps forever waiting to
# be killed.
CHILD = r'''
import sys, time
sys.path.insert(0, r"{src}")
import player
p = player.MpvController(mute=True)
ok = p.start()
print("START_OK" if ok else "START_FAIL", flush=True)
print("MPV_PID", p.pid, flush=True)
time.sleep(600)
'''.format(src=SRC)


def mpv_pids() -> set[int]:
    """PIDs of mpv processes, via the project's own Windows-friendly method."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process mpv -ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.Id }"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(x) for x in out.split() if x.strip().isdigit()}


def main() -> int:
    before = mpv_pids()
    print(f"mpv before: {sorted(before) or '(none)'}")

    child = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=str(ROOT),
    )

    # Wait for the child to report its mpv pid.
    mpv_pid = None
    deadline = time.time() + 90
    lines: list[str] = []
    while time.time() < deadline:
        line = child.stdout.readline() if child.stdout else ""
        if not line:
            if child.poll() is not None:
                break
            time.sleep(0.2)
            continue
        lines.append(line.rstrip())
        print("  child:", line.rstrip())
        if line.startswith("MPV_PID"):
            mpv_pid = int(line.split()[1])
            break

    if mpv_pid is None:
        print("FAIL: child never reported an mpv pid; output was:")
        for l in lines:
            print("   ", l)
        child.kill()
        return 1

    after_start = mpv_pids()
    if mpv_pid not in after_start:
        print(f"FAIL: mpv {mpv_pid} not present after start ({sorted(after_start)})")
        child.kill()
        return 1
    print(f"  mpv {mpv_pid} is running; now HARD-KILLING the daemon (no cleanup)")

    # HARD KILL: TerminateProcess == no atexit, no finally, no signal handler.
    child.kill()
    child.wait(timeout=30)
    print("  daemon killed")

    # The Lua guard polls once a second; allow generous slack for process exit.
    gone = False
    waited = 0.0
    while waited < 20:
        if mpv_pid not in mpv_pids():
            gone = True
            break
        time.sleep(0.5)
        waited += 0.5

    if gone:
        print(f"PASS: orphan mpv {mpv_pid} exited by itself {waited:.1f}s "
              f"after its daemon was hard-killed")
        return 0

    print(f"FAIL: mpv {mpv_pid} still alive {waited:.1f}s after daemon death "
          f"-- ORPHAN WINDOW (issue #6 not fixed)")
    # Clean up so the probe does not leave exactly the mess it detects.
    for pid in mpv_pids() - before:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Stop-Process -Id {pid} -Force"],
                       capture_output=True, timeout=30)
    return 1


if __name__ == "__main__":
    sys.exit(main())