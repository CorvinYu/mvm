"""Probe: does acquire() really self-heal a stale lock (dead owner pid)?

issue #6 claimed "stale lock not self-healed". Reading single_instance.py, the
code DOES claim a stale lock when the recorded PID is dead (acquire(), the
`owner and not _pid_alive(owner)` branch). So the claim needs verification
rather than repetition -- this probe exercises the real code path.

Cases:
  1. lock records a DEAD pid            -> must be taken over (self-heal)
  2. lock records a LIVE pid            -> must raise AlreadyRunning
  3. lock is EMPTY and fresh            -> must NOT be stolen (fail closed)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import single_instance as si  # noqa: E402

scratch = SRC.parent / "state" / "_lock_probe"
scratch.mkdir(parents=True, exist_ok=True)
results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def dead_pid() -> int:
    """Find a pid that is definitely not running."""
    for candidate in range(2**26 + 101, 2**26 + 400):
        if not si._pid_alive(candidate):
            return candidate
    raise SystemExit("could not find a dead pid")


print("\n§1 stale lock (dead owner pid) must be taken over")
lock = scratch / "case1.lock"
lock.unlink(missing_ok=True)
d = dead_pid()
lock.write_text(str(d), encoding="utf-8")
os.utime(lock, (time.time() - 3600, time.time() - 3600))
inst = si.SingleInstance(lock)
try:
    inst.acquire()
    check("记录死 PID 的陈旧锁被接管", inst.acquired, f"dead pid={d}")
    check("接管后锁内容是本进程 PID",
          lock.read_text(encoding="utf-8").strip() == str(os.getpid()))
    inst.release()
    check("release() 后锁文件被删除", not lock.exists())
except si.AlreadyRunning as exc:
    check("记录死 PID 的陈旧锁被接管", False, f"仍被拒绝: {exc}")

print("\n§2 live owner must still be refused")
lock2 = scratch / "case2.lock"
lock2.unlink(missing_ok=True)
# A real, live process: spawn a sleeper and use ITS pid.
sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
try:
    time.sleep(0.5)
    lock2.write_text(str(sleeper.pid), encoding="utf-8")
    inst2 = si.SingleInstance(lock2)
    try:
        inst2.acquire()
        check("活着的持有者必须被拒绝", False, "竟然拿到了锁！")
        inst2.release()
    except si.AlreadyRunning as exc:
        check("活着的持有者必须被拒绝", True, f"pid={exc.pid}")
finally:
    sleeper.terminate()
    try:
        sleeper.wait(timeout=5)
    except subprocess.TimeoutExpired:
        sleeper.kill()

print("\n§3 a FRESH empty lock must NOT be stolen (fail closed)")
lock3 = scratch / "case3.lock"
lock3.unlink(missing_ok=True)
lock3.write_text("", encoding="utf-8")
inst3 = si.SingleInstance(lock3)
t0 = time.time()
try:
    inst3.acquire()
    check("新鲜的空白锁不得被抢", False,
          f"竟然拿到了（耗时 {time.time() - t0:.1f}s）")
    inst3.release()
except si.AlreadyRunning as exc:
    check("新鲜的空白锁不得被抢（fail closed）", True,
          f"拒绝，耗时 {time.time() - t0:.1f}s")

passed = sum(1 for ok, _, _ in results if ok)
print("\n" + "=" * 60)
print(f"陈旧锁判据: {passed}/{len(results)} 通过")
print("=" * 60)
sys.exit(0 if passed == len(results) else 1)