"""Verify the liveness primitive used by the Lua orphan guard.

The Lua guard cannot be unit-tested from Python, but the WINDOWS CALL it relies
on can. This mirrors exactly what `parent_is_alive()` does, so if the primitive
is wrong the guard is wrong.

Measured facts this asserts (2026-10-08):
  * OpenProcess ALONE is insufficient: it keeps succeeding after
    TerminateProcess while a handle (e.g. Python's Popen object) is open.
  * GetExitCodeProcess distinguishes: dead -> exit code, alive -> 259.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time

k32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def openprocess_ok(pid: int) -> bool:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if h:
        k32.CloseHandle(h)
        return True
    return False


def exit_code_alive(pid: int) -> bool:
    """Exactly the Lua guard's logic: alive iff exit code == STILL_ACTIVE."""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not h:
        return False
    code = ctypes.c_ulong(0)
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    if not ok:
        return True
    return code.value == STILL_ACTIVE


def main() -> int:
    print("\n§1 a live process reads as alive under both primitives")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    pid = child.pid
    time.sleep(0.3)
    check("OpenProcess(live) = True", openprocess_ok(pid) is True)
    check("GetExitCodeProcess(live) = STILL_ACTIVE", exit_code_alive(pid) is True)

    print("\n§2 after TerminateProcess, GetExitCodeProcess must report DEAD")
    child.kill()
    child.wait(timeout=30)
    time.sleep(0.3)

    op = openprocess_ok(pid)
    code_alive = exit_code_alive(pid)
    print(f"    (OpenProcess still succeeds: {op} -- this is why it is useless alone)")
    check("GetExitCodeProcess(dead) = NOT alive  <-- the fix",
          code_alive is False, f"alive={code_alive}")

    print("\n§3 a pid that never existed reads as dead")
    never = 2**26 + 777
    check("OpenProcess(never existed) = False", openprocess_ok(never) is False)
    check("GetExitCodeProcess(never existed) = not alive",
          exit_code_alive(never) is False)

    print("\n§4 (informational) does the pid object linger after releasing Popen?")
    del child
    import gc
    gc.collect()
    time.sleep(0.5)
    # NOT a pass/fail criterion: whether OpenProcess starts failing depends on
    # who else holds a handle (the OS, WMI, a debugger). The point of §2 is that
    # the guard does not DEPEND on this happening -- hence the exit-code check.
    still = openprocess_ok(pid)
    print(f"    OpenProcess still succeeds: {still}"
          f"  ({'handle still held somewhere' if still else 'pid now fully released'})")
    print("    -> irrelevant to the fix; the guard reads the exit code, not openness")

    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"liveness 原语判据: {passed}/{len(results)} 通过")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())