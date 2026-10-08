"""Probe: does the Lua orphan guard actually work on this mpv build?

Runs a real mpv (the project's isolated copy) with the control script, a fake
parent pid that is ALREADY DEAD, and checks the mpv exits by itself within a
bounded time. If the guard is broken (ffi unavailable, timer not started, or
the liveness check wrong) the mpv stays up and this probe reports FAIL.

Also prints the first lua-related lines from mpv's own log so a failure can be
diagnosed instead of guessed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MPV = ROOT / "bin" / "mpv-iso" / "mpv.exe"
LUA = ROOT / "config" / "scripts" / "mvm_control.lua"
LOG = ROOT / "state" / "_guard_probe.log"

# A pid that cannot exist. (Windows PIDs reuse, so use one far above the usual
# range and re-verify it is absent before starting.)
DEAD_PID = 2**26 + 13


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    if not MPV.exists():
        print(f"FAIL: mpv not found at {MPV}")
        return 1
    if not LUA.exists():
        print(f"FAIL: lua not found at {LUA}")
        return 1
    if pid_exists(DEAD_PID):
        print(f"FAIL: DEAD_PID {DEAD_PID} unexpectedly exists -- pick another")
        return 1

    LOG.unlink(missing_ok=True)
    env = dict(os.environ)
    env["MVM_CMD_FILE"] = str(ROOT / "state" / "_mvm_cmd.txt")
    env["MVM_STATUS_FILE"] = str(ROOT / "state" / "_mvm_status.txt")
    env["MVM_PARENT_PID"] = str(DEAD_PID)

    args = [
        str(MPV), "--no-config", "--idle=yes", "--force-window=yes",
        "--title=MVM-Video", "--really-quiet",
        f"--log-file={LOG}", f"--script={LUA}",
    ]
    proc = subprocess.Popen(args, env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    deadline = time.time() + 12
    exited = False
    while time.time() < deadline:
        if proc.poll() is not None:
            exited = True
            break
        time.sleep(0.5)

    if not exited:
        print("FAIL: mpv did NOT exit within 12s (guard ineffective)")
        proc.terminate()
    else:
        print(f"PASS: mpv exited by itself (rc={proc.returncode}) "
              f"after ~{time.time() - (deadline - 12):.1f}s")

    # Show the lua lines from mpv's log for diagnosis.
    if LOG.exists():
        print("\n--- mvm/lua log lines ---")
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            low = line.lower()
            if ("mvm:" in low or "lua" in low or "ffi" in low
                    or "error" in low or "script" in low):
                print(" ", line[:160])
    return 0 if exited else 1


if __name__ == "__main__":
    sys.exit(main())
