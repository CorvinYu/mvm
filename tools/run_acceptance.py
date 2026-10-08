"""run_acceptance.py -- one command to run every automated acceptance check.

WHY THIS EXISTS
    The fixes for issues #2/#5/#6 each shipped with (a) offline criteria that must
    pass, (b) the SAME criteria under `--old` that must FAIL in specific places,
    and (c) live probes against a real mpv. Verifying that by hand is 10 separate
    commands and easy to do partially -- and a partial run is exactly how the
    status-file truncate race survived its first "verification" (see NOTES
    §1b). This runs all of it and prints one summary.

WHAT IT CHECKS
    1. selftest                      -- the broad smoke suite (slowest)
    2. offline criteria, normal mode -- must be fully green
    3. offline criteria, --old mode  -- must FAIL (proves they measure the fix)
    4. live probes (real mpv)        -- orphan guard + fullscreen

Exit code 0 only when everything is as expected, so it can gate a merge.

Usage:
    python tools/run_acceptance.py
    python tools/run_acceptance.py --fast      # skip selftest and live probes
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# (script, mode, expected) -- mode is "" (normal) or "--old".
# For "--old" the expectation is FAILURE; the point is that the criteria are
# falsifiable, not that the old code is good.
SUITES: list[tuple[str, str, str]] = [
    ("test_crashlog.py",            "",     "13/13"),
    ("test_crashlog.py",            "--old", "FAIL"),
    ("test_window_guard.py",        "",     "21/21"),
    ("test_window_guard.py",        "--old", "FAIL"),
    ("test_user_closed_window.py",  "",     "15/15"),
    ("test_user_closed_window.py",  "--old", "FAIL"),
    ("test_follow_loop.py",         "",     "7/7"),
    ("test_manual_wiring.py",       "",     "38"),
    ("test_align_inherit.py",       "",     "51/51"),
    ("test_sync_probe.py",          "",     "26"),
]

LIVE: list[tuple[str, str, str]] = [
    ("probe_orphan_e2e.py",         "",     "PASS"),
    ("probe_fullscreen_live.py",    "",     "7/7"),
    # Found while fixing #2: the manual-offset sidecar was frozen at its first
    # value because Windows rename() refuses an existing target. Directly
    # relevant to issue #3 ("手动调整似乎没有生效").
    ("probe_manual_sidecar.py",     "",     "4/4"),
]


# selftest checks that legitimately fail on an idle machine. They must be listed
# EXPLICITLY so a real regression cannot hide behind "it was already failing".
ENV_KNOWN_FAILURES = {
    "能读取到音频数据",       # WASAPI loopback returns 0 bytes when nothing plays
}


def selftest_verdict(lines: list[str]) -> tuple[bool, str]:
    """Pass unless a failure OTHER than a known-environment one is present."""
    failed = [ln.split("]", 1)[1].strip() for ln in lines if "[FAIL]" in ln]
    failed = [f.split("--")[0].strip() for f in failed]
    unknown = [f for f in failed if not any(k in f for k in ENV_KNOWN_FAILURES)]
    summary = pick(lines, "结果:")
    if unknown:
        return False, f"{summary}  ← 非环境性失败: {unknown}"
    if failed:
        return True, f"{summary}  (仅环境性项: {failed})"
    return True, summary


def kill_stray_mpv() -> None:
    """Clear leftover mpv between steps.

    WHY (measured 2026-10-08): running this battery in one go, the live probes
    left an mpv behind and the NEXT suite that also drives mpv (selftest) then
    failed several checks -- reported as "116/119, 3 failed" with nothing wrong
    in the code. Each suite must start from a clean process table.
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process mpv -ErrorAction SilentlyContinue | "
             "Stop-Process -Force -ErrorAction SilentlyContinue"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    time.sleep(0.6)

# Pattern matching the "N/M 通过" or "N passed" summary a suite prints.
RESULT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*(?:组判据)?通过|(\d+)\s+passed")


def run(script: str, mode: str, timeout: int) -> tuple[int, str]:
    args = [sys.executable, str(SRC / script)]
    if mode:
        args.append(mode)
    kill_stray_mpv()
    started = time.time()
    # encoding="utf-8" IS REQUIRED, not cosmetic: the suites print Chinese and
    # this host's default codec is GBK, so `text=True` alone dies with
    # UnicodeDecodeError and every result looks like an empty "BAD" (measured
    # 2026-10-08 on the first run of this very script -- same failure mode as the
    # PowerShell-redirection trap in NOTES §3.1 #1).
    proc = subprocess.run(args, capture_output=True, timeout=timeout,
                          cwd=str(ROOT), encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    elapsed = time.time() - started
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    return proc.returncode, lines, elapsed


def pick(lines: list[str], pattern: str) -> str:
    """The most informative line: one matching `pattern`, else the last line."""
    if pattern:
        for ln in reversed(lines):
            if pattern in ln:
                return ln
    return lines[-1] if lines else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true",
                    help="skip selftest and the live probes")
    args = ap.parse_args()

    rows: list[tuple[str, str, bool, str]] = []

    if not args.fast:
        print("=" * 70)
        print("1/3  selftest（较慢，含真实 mpv 链路）")
        print("=" * 70)
        rc, lines, el = run("selftest.py", "", timeout=1800)
        # Criterion is NOT the exit code alone and NOT a hard-coded total.
        # Measured 2026-10-08: the total moved between 121/121 and 119/119 in one
        # session (checks SKIP themselves when a precondition is missing), and on
        # an idle machine the loopback-capture check fails through no fault of
        # the code. So: pass, unless something OUTSIDE the known-environment list
        # failed -- which is the only signal that means "we broke something".
        ok, tail = selftest_verdict(lines)
        rows.append(("selftest.py", "normal", ok, f"{tail}  [{el:.1f}s]"))

    print("\n" + "=" * 70)
    print("2/3  离线判据（正常模式必须全绿；--old 必须失败）")
    print("=" * 70)
    for script, mode, expect in SUITES:
        rc, lines, el = run(script, mode, timeout=600)
        if expect == "FAIL":
            ok = rc != 0                      # it MUST fail on the old path
            label = f"{mode} (期望失败)"
            tail = pick(lines, "判据:")
        else:
            tail = pick(lines, expect)
            ok = rc == 0 and any(expect in ln for ln in lines)
            label = mode or "normal"
        rows.append((script, label, ok, f"{tail}  [{el:.1f}s]"))
        print(f"  [{'OK ' if ok else 'BAD'}] {script} {label}  ->  {tail}  [{el:.1f}s]")

    if not args.fast:
        print("\n" + "=" * 70)
        print("3/3  真机探针（会短暂开 mpv 窗口）")
        print("=" * 70)
        for script, mode, expect in LIVE:
            rc, lines, el = run(script, mode, timeout=600)
            tail = pick(lines, expect)
            ok = rc == 0 and any(expect in ln for ln in lines)
            rows.append((script, mode or "normal", ok, f"{tail}  [{el:.1f}s]"))
            print(f"  [{'OK ' if ok else 'BAD'}] {script}  ->  {tail}  [{el:.1f}s]")

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    bad = 0
    for script, label, ok, tail in rows:
        if not ok:
            bad += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {script:<28} {label:<16} {tail[:60]}")
    print()
    if bad:
        print(f"✗ {bad} 项不符合预期 —— 不要合并，先看上面的输出。")
    else:
        print("✓ 全部符合预期。")
        print("  注意：自动化只能证明机制正确；「按 F 能全屏」「关窗不再回来」")
        print("  这两件事仍需你在真机上按一次（见 待测试列表.md 的 T5/T6/T7）。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
