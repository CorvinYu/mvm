"""Minimal repro: does capture_output subprocess work in this host environment?

WHY: `selftest.py` test 14 (`test_single_instance`) hangs at
`sp.run([sys.executable, tmp], capture_output=True, text=True, timeout=60)` --
the script it spawns never returns and no traceback reaches the log. The log
file's mtime stops right after "第一个实例获取锁" PASSes.

This isolates that primitive so the hang can be attributed to the ENVIRONMENT
rather than to product code. It is a diagnostic, not a criterion.

Run (background, output to a file):  python -u src\\repro_subproc_capture.py
"""

from __future__ import annotations

import subprocess
import sys
import time

print("== repro: subprocess capture_output on this host ==", flush=True)


def step(name: str, fn) -> None:
    t0 = time.monotonic()
    print(f"\n[{name}] 开始", flush=True)
    try:
        out = fn()
        print(f"[{name}] 返回 {out!r}  耗时 {time.monotonic() - t0:.2f}s", flush=True)
    except Exception as exc:  # noqa: BLE001 - diagnostic, report everything
        print(f"[{name}] 异常 {type(exc).__name__}: {exc}  "
              f"耗时 {time.monotonic() - t0:.2f}s", flush=True)


def cap_python() -> str:
    r = subprocess.run([sys.executable, "-c", "print('hello-from-child')"],
                       capture_output=True, text=True, timeout=20)
    return (r.stdout or "").strip()


def cap_powershell() -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Write-Output 'hello-from-ps'"],
                       capture_output=True, text=True, timeout=20)
    return (r.stdout or "").strip()


step("capture python child stdout", cap_python)
step("capture powershell child stdout", cap_powershell)
print("\n== repro 结束（若上面两项都能返回，则捕获本身可用）==", flush=True)
