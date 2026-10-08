"""crashlog.py -- make daemon deaths leave evidence.

WHY THIS EXISTS (issue #6, measured 2026-10-08)
-----------------------------------------------
The follower died mid-song and left NO trace. `state/_evidence/follow_live.log`
ended on an ordinary business line:

    [11:31:26]   · 闭环校验：偏差 -64.5s 超出漂移量级（>2.0s），先确认再处理（第 1/3 次）

...and then nothing. No traceback, no exit reason, no "已停止". An mpv window
(PID 50640) stayed behind as an orphan, which the user then reported as
"白名单失效" because the empty window appeared while they were playing
something else. Diagnosing it required reverse-engineering from process tables.

Why nothing was logged:

  * `Follower.run()`'s `finally` calls `player.stop()`, but that only runs on a
    clean exit or KeyboardInterrupt. A hard kill never runs it at all.
  * The worker thread is created with `daemon=True`. When a thread raises, the
    default `threading.excepthook` prints to stderr -- which the recommended
    launcher (`tools/run_follow_utf8.py`) may not capture -- and, critically,
    the MAIN thread's `try/finally` never sees it, so no cleanup happens.
  * Nothing recorded WHY the process ended.

This module fixes the observability half. It is deliberately tiny and
dependency-free so it can be imported by follow.py, tests, and tools alike.

Design notes
------------
* **Append-only.** Crash records go to a dedicated file
  (`state/_evidence/crash.log`) rather than the main log, so a crash cannot be
  lost by log rotation/truncation and is trivially greppable.
* **Never raises.** A logging facility that itself throws would turn a
  diagnosable crash into an undiagnosable one. Every entry point swallows its
  own errors and reports failure via the return value.
* **Records the PHASE.** Knowing the daemon died is much less useful than
  knowing it died *while resolving a stream*. `phase()` sets a human-readable
  label that every subsequent record carries.
* **Idempotent install.** `install()` may be called repeatedly (e.g. tests).
"""
from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import time
import traceback
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
EVIDENCE_DIR = STATE_DIR / "_evidence"
CRASH_LOG = EVIDENCE_DIR / "crash.log"

# Set by phase(); included in every record so a crash can be located in the
# song-switch pipeline without reading the whole main log.
_current_phase: str = "startup"
_started_at: float = time.time()
_installed: bool = False
_lock = threading.Lock()


def phase(name: str) -> None:
    """Record what the daemon is currently doing (cheap, thread-safe)."""
    global _current_phase
    with _lock:
        _current_phase = str(name)


def current_phase() -> str:
    return _current_phase


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _uptime() -> str:
    return f"{time.time() - _started_at:.1f}s"


def record(kind: str, detail: str = "") -> bool:
    """Append one crash/exit record. NEVER raises; returns True on success.

    The file is opened in append mode and flushed immediately: if the process
    is about to die, buffered data would be lost exactly when it is needed.
    """
    line = (f"[{_timestamp()}] {kind} | phase={_current_phase} "
            f"| pid={os.getpid()} | uptime={_uptime()}")
    if detail:
        line += f"\n{detail}"
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line.rstrip("\n") + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception:  # noqa: BLE001 - a logger must never kill the caller
        return False


def _format_exc(exc_type, exc, tb, thread_name: str) -> str:
    body = "".join(traceback.format_exception(exc_type, exc, tb))
    return (f"  thread={thread_name}\n"
            f"  exception={exc_type.__name__}: {exc}\n"
            f"  traceback:\n{body.rstrip()}")


def install(*, enable_faulthandler: bool = True) -> bool:
    """Install hooks so every abnormal death writes a record.

    Covers:
      * uncaught exception in the MAIN thread   -> sys.excepthook
      * uncaught exception in ANY other thread  -> threading.excepthook
        (this is the one that was silently eating worker deaths)
      * normal interpreter shutdown             -> atexit (records "exit")
      * hard faults (segfault / stack overflow) -> faulthandler, which writes
        the C-level traceback to our file even when Python cannot run
    """
    global _installed
    if _installed:
        return False
    _installed = True

    def _sys_hook(exc_type, exc, tb):
        record("UNCAUGHT-MAIN", _format_exc(exc_type, exc, tb, "MainThread"))
        # Preserve the default behaviour (print to stderr) so an interactive
        # run still shows the error.
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        record("UNCAUGHT-THREAD",
               _format_exc(args.exc_type, args.exc_value, args.exc_traceback,
                           getattr(args.thread, "name", "?")))

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook

    @atexit.register
    def _on_exit() -> None:  # pragma: no cover - exercised via subprocess
        record("exit", "  (interpreter shutdown; graceful path)")

    if enable_faulthandler:
        try:
            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            # Keep the handle open for the process lifetime: faulthandler needs
            # a live fd to write into when Python itself is unusable.
            _fh = open(CRASH_LOG, "a", encoding="utf-8", errors="replace")
            faulthandler.enable(file=_fh, all_threads=True)
            globals()["_faulthandler_file"] = _fh
        except Exception:  # noqa: BLE001 - optional hardening
            pass

    record("START", f"  argv={' '.join(sys.argv[1:]) or '(none)'}")
    return True


def _close_faulthandler_file() -> None:
    """Close the faulthandler fd (used by tests before removing the dir).

    Windows will not delete a directory while a handle inside it is open, so
    `TemporaryDirectory.cleanup()` raises PermissionError unless this runs
    first. Harmless to call when nothing was opened.
    """
    fh = globals().pop("_faulthandler_file", None)
    if fh is not None:
        try:
            faulthandler.disable()
        except Exception:  # noqa: BLE001
            pass
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass


def read_records(limit: int = 50) -> list[str]:
    """Return the tail of the crash log (for tests/tools)."""
    try:
        text = CRASH_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-limit:]
