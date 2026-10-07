"""single_instance.py -- prevent two followers from running at once.

Why this exists (a real, user-visible bug):
    Two `follow.py` processes were left running. Each owns its own mpv window,
    and they share the same command file, so a song change produced a burst of
    windows: one would eventually align, the others stayed open forever. The
    user saw "every new song closes the current mpv and opens 3 new windows".

    A lock file holding the owner's PID fixes it: the second instance refuses
    to start (or takes over if the recorded process is dead).

Implementation notes:
    * Windows has no flock, and this environment denies named pipes, so the
      only usable mutex primitive is an atomic, exclusive file operation.
    * A stale lock (previous process crashed) must not block forever, so we
      verify the recorded PID is actually alive.
    * Taking over a stale lock is the hard part. It used to be a plain
      read-owner -> check-alive -> write-pid sequence, which is NOT atomic:
      with 8 processes racing one dead-PID lock, 2 of them "won" in 5/5 trials
      (体检报告 01 §1.4). That race is the real mechanism behind the
      "several windows per song" report.

How the lock is decided now (all measured on this machine, see below):
    1. Ownership is ONLY ever granted by an atomic *exclusive publish*: write
       our PID to a private `lock.tmp.<pid>` file, then publish it with
       `os.link` (exclusive on NTFS and POSIX), falling back to
       `O_CREAT|O_EXCL` where hard links are unavailable. Exactly one process
       can succeed; the loser sees FileExistsError and knows it lost.
    2. A stale lock is never overwritten in place. We first *move it aside*
       with `os.rename` -- also atomic and, on Windows, exclusive (it raises
       FileExistsError when the target exists). Only the process that wins
       that rename may then publish, so the final decision again goes through
       step 1. This keeps a single decision point for ownership.
    3. A lock whose content is empty or unparseable is NOT treated as stale.
       An empty lock is exactly what a peer looks like in the instant between
       creating the file and writing its PID, and claiming it there lets the
       thief in alongside the real owner (measured: an empty lock was stolen
       immediately, producing ACQUIRED=1 alongside the creator). Since age
       cannot distinguish "being written right now" from "abandoned by a
       crash", such a lock is never stolen: we wait for the writer, and if it
       stays empty past `_EMPTY_LOCK_TIMEOUT_SECONDS` we fail with an explicit
       message instead of gambling on it.

Measured primitive semantics (16-process barrier races, 5/5 trials each):
    * `os.rename(tmp, dst)` with dst existing -> FileExistsError; WON=1/16
      -> atomic, exclusive, carries content. Used for both publish and claim.
    * `os.replace(tmp, dst)` -> last-writer-wins, WON=4..5/16, and many racers
      got WinError 5 (sharing violation). It cannot arbitrate a lock, and
      "verify by reading back afterwards" does not fix it: A replace, A read
      (ok), B replace, B read (ok) leaves two owners. Deliberately NOT used.
    * `os.link(tmp, dst)` with dst existing -> FileExistsError; WON=1/16.

Known limitation:
    `_pid_alive()` can report a long-gone process as alive when somebody still
    holds its handle (体检报告 05 §4.3). That errs toward refusing a start --
    never toward two windows -- which is the safe direction for this bug.
"""

from __future__ import annotations

import atexit
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "state" / ".follow.lock"

# Bounded retry budget: 1000 * 10ms = 10s worst case, which only happens after
# a crash inside the critical section. On exhaustion we fail CLOSED (raise
# AlreadyRunning) -- refusing one start beats two windows.
_ACQUIRE_TIMEOUT_SECONDS = 10.0
_RETRY_SLEEP_SECONDS = 0.01

# How long an EMPTY lock may exist before we give up waiting for its writer and
# fail with a clear error. An empty lock is treated as "being written" and is
# never stolen (stealing it is the second half of the original bug); this is
# the backstop for a writer that crashed between create and write. Must stay
# below _ACQUIRE_TIMEOUT_SECONDS so the explanatory error wins the race with
# the generic deadline.
_EMPTY_LOCK_TIMEOUT_SECONDS = 5.0


class AlreadyRunning(RuntimeError):
    """Raised when another live follower holds the lock."""

    def __init__(self, pid: int, message: str | None = None) -> None:
        super().__init__(
            message or f"another follower is already running (pid {pid})")
        self.pid = pid


def _pid_alive(pid: int) -> bool:
    """Whether a process with this PID currently exists.

    Measured Windows semantics (体检报告 05 §4.3, re-confirmed on this machine):
        * live process             -> os.kill(pid, 0) returns, no exception
        * PID that never existed   -> OSError winerror 87 (ERROR_INVALID_PARAMETER)
        * process we cannot signal -> OSError winerror 5 (ERROR_ACCESS_DENIED)

    "Access denied" means the process is real and merely not ours, so we count
    it as ALIVE (conservative). Anything else is treated as gone, which keeps a
    crashed owner's lock takeable instead of deadlocking startup forever.

    Also verified non-destructive: os.kill(pid, 0) leaves a live process
    running (poll() still None afterwards).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # POSIX EPERM: the process exists, we just may not signal it.
        return True
    except ProcessLookupError:
        # POSIX ESRCH: no such process.
        return False
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 5:
            return True  # ERROR_ACCESS_DENIED -- exists but is not ours
        # WinError 87 (ERROR_INVALID_PARAMETER) == the PID does not exist.
        return False


class SingleInstance:
    """Context manager holding an exclusive lock for this process."""

    def __init__(self, lock_file: Path | str = LOCK_FILE) -> None:
        # Accept str too: callers (and tests) naturally pass a string path, and
        # silently requiring a Path produced a confusing AttributeError.
        self.lock_file = Path(lock_file)
        self.acquired = False
        self._owner_pid = os.getpid()
        self._registered_atexit = False

    # -- paths -----------------------------------------------------------

    def _tmp_path(self) -> Path:
        """Private staging file; unique per process, so never contended."""
        return self.lock_file.with_name(f"{self.lock_file.name}.tmp.{self._owner_pid}")

    def _stale_path(self) -> Path:
        """Where a claimed stale lock is moved before being discarded."""
        return self.lock_file.with_name(f"{self.lock_file.name}.stale.{self._owner_pid}")

    # -- primitives ------------------------------------------------------

    def _read_owner(self) -> int:
        """PID recorded in the lock file, or 0 if empty/unreadable/garbage."""
        try:
            return int(self.lock_file.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def _lock_age(self) -> float:
        """Seconds since the lock file was last modified (inf if unreadable)."""
        try:
            return time.time() - self.lock_file.stat().st_mtime
        except OSError:
            return float("inf")

    def _publish_pid(self, me: int) -> bool:
        """Atomically publish our PID as the lock owner.

        True  -> we created the lock and therefore own it.
        False -> the lock already exists; we lost the race.

        Raises nothing for the "lost" case; returns False so the caller can
        inspect the incumbent. Unrecoverable OSErrors are reported and also
        yield False (the caller then fails closed after its retry budget).
        """
        tmp = self._tmp_path()
        try:
            # Stage the full content first: the lock file itself must never be
            # observable while empty.
            tmp.write_text(str(me), encoding="utf-8")
        except OSError as exc:
            print(f"[warn] 无法写入锁暂存文件（{exc}）", file=sys.stderr)
            return False

        try:
            # Preferred primitive: atomic + exclusive + carries the content.
            try:
                os.link(str(tmp), str(self.lock_file))
                return True
            except FileExistsError:
                return False
            except OSError:
                # Hard links unsupported (FAT/network share) -> fall back to an
                # exclusive create. This leaves a sub-millisecond empty window,
                # which the acquire loop's "empty lock" handling protects
                # readers from (an empty lock is never stolen, only waited on).
                pass

            try:
                fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False
            except OSError as exc:
                print(f"[warn] 无法创建锁文件（{exc}）", file=sys.stderr)
                return False
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(str(me))
            except OSError as exc:
                print(f"[warn] 无法写入锁文件（{exc}）", file=sys.stderr)
                # Never leave a half-written lock behind: an empty lock would
                # look claimable to everyone and invite the race we are fixing.
                try:
                    self.lock_file.unlink()
                except OSError:
                    pass
                return False
            return True
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _claim_stale(self) -> bool:
        """Move a stale lock aside so its path becomes free again.

        `os.rename` is atomic and -- on Windows -- refuses to overwrite an
        existing target, so with N processes trying this only one succeeds.
        The winner discards the claimed file; everyone else gets
        FileNotFoundError and simply re-reads the world.

        Returns True when we were the one who moved it.
        """
        stale = self._stale_path()
        try:
            os.rename(str(self.lock_file), str(stale))
        except (FileNotFoundError, FileExistsError):
            return False
        except OSError as exc:
            # Cannot claim it (permissions?). Report once per attempt; the
            # caller's retry budget bounds the noise.
            print(f"[warn] 无法接管陈旧锁文件（{exc}）", file=sys.stderr)
            return False
        try:
            stale.unlink()
        except OSError:
            pass
        return True

    def _mark_acquired(self) -> None:
        self.acquired = True
        if not self._registered_atexit:
            atexit.register(self.release)
            self._registered_atexit = True

    # -- public API ------------------------------------------------------

    def acquire(self) -> None:
        """Take the lock, or raise AlreadyRunning if a live peer holds it."""
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        me = os.getpid()
        deadline = time.monotonic() + _ACQUIRE_TIMEOUT_SECONDS
        last_owner = 0

        while True:
            # 1) The only way to become the owner: win the exclusive publish.
            if self._publish_pid(me):
                self._mark_acquired()
                return

            # 2) The lock exists. Decide whether we may take it over.
            owner = self._read_owner()
            if owner == me:
                # Re-acquiring in the same process: already ours.
                self._mark_acquired()
                return
            if owner and _pid_alive(owner):
                raise AlreadyRunning(owner)

            if owner:
                # Recorded PID is dead -> stale lock, claim it and retry.
                last_owner = owner
                self._claim_stale()
            else:
                # The lock is empty or unparseable. This is NOT proof of a
                # stale lock: it is exactly what a peer looks like for the
                # instant between creating the file and writing its PID, and
                # stealing it there is the second half of the original
                # multi-window bug (measured: an empty lock was claimed
                # instantly, letting a racer in alongside the real creator).
                # Age cannot separate "being written" from "abandoned by a
                # crash", so never steal it -- wait for the owner to finish
                # writing, and fail closed if it never does.
                if self._lock_age() >= _EMPTY_LOCK_TIMEOUT_SECONDS:
                    raise AlreadyRunning(
                        0,
                        f"锁文件 {self.lock_file} 内容为空且已存在 "
                        f"{self._lock_age():.1f}s（疑为崩溃残留）。"
                        f"确认没有 follow 进程在运行后删除该文件再重试。",
                    )

            if time.monotonic() >= deadline:
                # Could not get a definitive answer. Fail closed: refusing to
                # start beats risking two windows.
                raise AlreadyRunning(last_owner)
            time.sleep(_RETRY_SLEEP_SECONDS)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            # Only remove it if we still own it.
            if self._read_owner() == self._owner_pid:
                self.lock_file.unlink()
        except OSError:
            pass
        # Drop our staging file if a publish was interrupted halfway.
        try:
            self._tmp_path().unlink()
        except OSError:
            pass
        self.acquired = False

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
