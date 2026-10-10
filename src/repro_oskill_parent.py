"""Diagnostic: can a child process call os.kill(parent_pid, 0) on this host?

CONTEXT: `selftest.py` test 14 hangs at
`sp.run([sys.executable, tmp], capture_output=True, text=True, timeout=60)`,
frozen right after "第一个实例获取锁" PASSes (verified by running
`test_single_instance()` alone: src\\repro_selftest_test14.py).

The script that child runs contains the ONLY primitive in test 14 that touches
another process: `single_instance._pid_alive(owner)` -> `os.kill(owner, 0)`,
where `owner` is the PARENT (the process holding the lock). Every other step in
test 14 is plain file I/O, which cannot block.

So this tests exactly that call from a child against its own parent, with a
timeout, to see whether the host blocks it instead of returning.

Run (background):  python -u src\\repro_oskill_parent.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

pid = os.getpid()
child_code = (
    "import os;"
    "print('child: before os.kill', flush=True);"
    f"os.kill({pid}, 0);"
    "print('child: os.kill returned', flush=True)"
)
print(f"parent pid = {pid}", flush=True)
t0 = time.monotonic()
try:
    r = subprocess.run([sys.executable, "-c", child_code],
                       capture_output=True, text=True, timeout=15)
    print(f"child rc={r.returncode} 耗时 {time.monotonic() - t0:.2f}s", flush=True)
    print(f"  stdout={r.stdout!r}", flush=True)
    print(f"  stderr={r.stderr[:300]!r}", flush=True)
except subprocess.TimeoutExpired:
    print(f"✗ 超时：子进程对父进程 os.kill 被挂起 "
          f"（>{time.monotonic() - t0:.1f}s）—— 这就是 test 14 卡死的原因",
          flush=True)
except Exception as exc:  # noqa: BLE001 - diagnostic
    print(f"异常 {type(exc).__name__}: {exc}", flush=True)
print("== 诊断结束 ==", flush=True)
