"""Diagnostic: run ONLY selftest's test 14, to locate where it hangs.

`selftest.py` stops emitting output right after test 14's first check PASSes,
with the log file's mtime frozen from that moment on, and exits 1 much later.
The subprocess-capture primitive itself is fine on this host
(src\\repro_subproc_capture.py returns in <1s), so the hang is inside test 14.

This calls the real function (no copy) so the finding is about the shipped code.

Run (background):  python -u src\\repro_selftest_test14.py
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import selftest as S  # noqa: E402

print("== 只运行 selftest 测试 14 ==", flush=True)
t0 = time.monotonic()
try:
    S.test_single_instance()
    print(f"== test_single_instance() 正常返回，耗时 {time.monotonic() - t0:.2f}s ==",
          flush=True)
except BaseException as exc:  # noqa: BLE001 - diagnostic
    print(f"== 抛出 {type(exc).__name__}: {exc}  耗时 "
          f"{time.monotonic() - t0:.2f}s ==", flush=True)
    traceback.print_exc()

print(f"== passed={S._passed} failed={S._failed} ==", flush=True)
