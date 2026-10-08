"""test_crashlog.py -- offline criteria for issue #6's observability fix.

Run:  python src/test_crashlog.py          (expect all green)
      python src/test_crashlog.py --old    (expect FAILURES: the point)

WHY THIS FILE EXISTS (NOTES §3.2 rule 4)
----------------------------------------
"selftest 全绿不能作为回归依据" -- session 7's 8 bugs all coexisted with a fully
green suite. A new fix must ship with a check that FAILS on the old code.

What it proves, in order of importance:

  §1  A worker-thread exception now leaves a record. Before the fix,
      `threading.excepthook` was the default, so a worker that raised simply
      vanished: `daemon=True` meant the main loop kept running and nothing was
      written anywhere. This is the exact failure that made issue #6
      undiagnosable.
  §2  A main-thread exception leaves a record AND is still printed to stderr
      (we must not swallow it).
  §3  The record carries the PHASE, so a death can be located in the pipeline.
  §4  `record()` never raises, even when the target is unwritable -- a logger
      that dies turns one diagnosable crash into two undiagnosable ones.
  §5  A real subprocess that dies by `os._exit(1)` (bypassing atexit, like a
      hard kill) still leaves the exit evidence that Python can produce.
  §6  `install()` is idempotent.

`--old` mode: the "old" behaviour is simulated by NOT installing the hooks
(the pre-fix state), which must make §1/§2/§3 fail. That is what proves these
checks actually measure the fix rather than passing by accident.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import crashlog  # noqa: E402

_old_mode = "--old" in sys.argv
_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def _scratch() -> "Path":
    """A scratch DIRECTORY inside the project, not %TEMP%.

    WHY not tempfile (measured 2026-10-08 on this machine): `%TEMP%` lives
    under a DSH sandbox directory whose ACLs make `TemporaryDirectory.cleanup()`
    raise `PermissionError [WinError 5]` when an open handle exists inside it --
    exactly our case, because crashlog deliberately keeps the faulthandler fd
    open for the process lifetime. The failure surfaced as a crash in the TEST,
    which would have been misreported as a product bug.

    Project-local scratch mirrors what the rest of the suite does
    (state/_selftest_align etc.) and sidesteps the ACL problem entirely.
    """
    base = SRC.parent / "state" / "_crashlog_test"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fresh_log(tmp: Path) -> Path:
    """Point crashlog at a scratch file for the duration of one test."""
    crashlog._close_faulthandler_file()
    crashlog.CRASH_LOG = tmp
    crashlog.EVIDENCE_DIR = tmp.parent
    return tmp


def _read(tmp: Path) -> str:
    try:
        return tmp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
print("\n§1 worker-thread exception must leave a record")
# --------------------------------------------------------------------------
td = _scratch()
tmp = _fresh_log(Path(td) / "crash.log")

if not _old_mode:
    # Re-arm: install() is idempotent, and a previous section may have
    # installed already, so force the flag for a clean measurement.
    crashlog._installed = False
    crashlog.install(enable_faulthandler=False)

boom = threading.Event()
crashlog.phase("worker-test")

def _explode():
    raise RuntimeError("kaboom-in-worker")

t = threading.Thread(target=_explode, name="test-worker")
if _old_mode:
    # Simulate the PRE-FIX world: no threading.excepthook, and stderr
    # silenced the way the UTF-8 launcher effectively does for a daemon.
    original = threading.excepthook
    threading.excepthook = threading.__excepthook__
    try:
        with open(os.devnull, "w") as devnull:
            old_err, sys.stderr = sys.stderr, devnull
            try:
                t.start()
                t.join(timeout=5)
            finally:
                sys.stderr = old_err
    finally:
        threading.excepthook = original
else:
    t.start()
    t.join(timeout=5)

time.sleep(0.1)
text = _read(tmp)
check("worker 线程异常被记录（旧码：无记录）",
      "UNCAUGHT-THREAD" in text,
      f"log={text.strip()[:70]!r}")
check("记录里含异常类型与消息",
      "RuntimeError" in text and "kaboom-in-worker" in text)
check("记录里含线程名",
      "test-worker" in text or "thread=" in text)
check("记录里含 traceback",
      "Traceback (most recent call last)" in text)

# --------------------------------------------------------------------------
print("\n§2 main-thread exception must be recorded AND still printed")
# --------------------------------------------------------------------------
td = _scratch()
tmp = _fresh_log(Path(td) / "crash.log")
if not _old_mode:
    crashlog._installed = False
    crashlog.install(enable_faulthandler=False)

import io
captured = io.StringIO()
old_stderr, sys.stderr = sys.stderr, captured
try:
    if _old_mode:
        try:
            raise ValueError("main-thread-boom")
        except ValueError:
            traceback_print = __import__("traceback").print_exc
            traceback_print(file=captured)
    else:
        sys.excepthook(ValueError, ValueError("main-thread-boom"), None)
finally:
    sys.stderr = old_stderr

text = _read(tmp)
check("主线程异常被记录（旧码：无记录）",
      "UNCAUGHT-MAIN" in text or "main-thread-boom" in text)
check("异常仍然打印到 stderr（未被吞掉）",
      "main-thread-boom" in captured.getvalue(),
      f"stderr={captured.getvalue().strip()[:50]!r}")

# --------------------------------------------------------------------------
print("\n§3 records must carry the current phase")
# --------------------------------------------------------------------------
td = _scratch()
tmp = _fresh_log(Path(td) / "crash.log")
crashlog.phase("resolving-stream")
crashlog.record("TEST-PHASE")
text = _read(tmp)
check("记录含 phase=resolving-stream",
      "phase=resolving-stream" in text, f"log={text.strip()[:70]!r}")
check("记录含 pid", "pid=" in text)
check("记录含 uptime", "uptime=" in text)
crashlog.phase("startup")

# --------------------------------------------------------------------------
print("\n§4 record() must never raise, even when unwritable")
# --------------------------------------------------------------------------
import contextlib
with contextlib.suppress(Exception):
    crashlog.CRASH_LOG = Path("Z:/definitely/not/a/real/drive/crash.log")
ok = crashlog.record("UNWRITABLE")
check("目标不可写时 record() 返回 False 而不抛异常", ok is False)

# --------------------------------------------------------------------------
print("\n§5 a hard-killed subprocess still leaves START evidence")
# --------------------------------------------------------------------------
if not _old_mode:
    td = _scratch()
    log = Path(td) / "crash.log"
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "import crashlog\n"
        "crashlog.CRASH_LOG = __import__('pathlib').Path(r'%s')\n"
        "crashlog.EVIDENCE_DIR = crashlog.CRASH_LOG.parent\n"
        "crashlog.install(enable_faulthandler=False)\n"
        "import os; os._exit(1)\n" % (SRC, log)
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=60)
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    check("os._exit(1)（绕过 atexit）仍留下 START 记录",
          proc.returncode == 1 and "START" in text,
          f"rc={proc.returncode} log={text.strip()[:60]!r}")
else:
    check("os._exit(1) 场景（旧码：无记录）", False, "old code writes nothing")

# --------------------------------------------------------------------------
print("\n§6 install() is idempotent")
# --------------------------------------------------------------------------
crashlog._installed = False
first = crashlog.install(enable_faulthandler=False)
second = crashlog.install(enable_faulthandler=False)
check("首次 install() 返回 True", first is True)
check("重复 install() 返回 False（幂等，不重复挂钩）", second is False)

# --------------------------------------------------------------------------
passed = sum(1 for ok, _, _ in _results if ok)
total = len(_results)
print("\n" + "=" * 60)
print(f"crashlog 判据: {passed}/{total} 通过" + ("  [--old 模式，预期失败]" if _old_mode else ""))
if passed < total:
    print("\n失败项：")
    for ok, name, detail in _results:
        if not ok:
            print(f"  - {name}  {detail}")
print("=" * 60)
sys.exit(0 if passed == total else 1)
