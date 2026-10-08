"""Probe: does the manual-offset sidecar actually UPDATE after the first nudge?

HYPOTHESIS (found 2026-10-08 while fixing #2)
    `publish_manual_sidecar()` in mvm_control.lua writes the new cumulative
    offset to a temp file and then calls `os.rename(tmp, target)`. On Windows
    `rename()` FAILS when the destination already exists (verified from Python:
    FileExistsError). The target is only deleted before an mpv spawn, so:
        nudge #1 -> target absent -> rename succeeds -> sidecar = 0.1
        nudge #2 -> target EXISTS -> rename fails  -> sidecar STILL 0.1
    i.e. every nudge after the first is silently dropped, and the daemon keeps
    reading a frozen value. That would match the user's issue #3 report
    ("手动调整对齐似乎没有生效") far better than a key-binding problem, and it
    would also mean the manual channel of issue #4/#5 is half-broken.

WHAT THIS DOES
    Drives the REAL mpv + REAL Lua over the existing command-file seam
    (`script-message-to mvm_control mvm-nudge N`, the same function the hotkeys
    call -- see the test seam documented in the Lua file), and reads the sidecar
    after each nudge. Three nudges must accumulate to +0.2 then -0.3.

Run:  python src/probe_manual_sidecar.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import player  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def sidecar() -> str:
    try:
        return player.MANUAL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "<absent>"


def nudge(ctl, delta: str, label: str) -> str:
    ctl.command(f"script-message-to mvm_control mvm-nudge {delta}")
    time.sleep(1.2)                      # the status/hotkey path is not instant
    value = sidecar()
    print(f"    nudge {delta:>5}  ->  sidecar = {value}")
    return value


def main() -> int:
    ctl = player.MpvController(mute=True)
    try:
        if not ctl.start():
            print("FAIL: mpv 未启动")
            return 1

        # Make sure we start from "no sidecar", as after a fresh spawn.
        try:
            player.MANUAL_FILE.unlink()
        except OSError:
            pass
        time.sleep(0.6)
        check("起始时 sidecar 不存在（同 start() 之后的状态）", sidecar() == "<absent>",
              sidecar())

        v1 = nudge(ctl, "+0.1", "first")
        check("第 1 次微调写入 sidecar = 0.100", v1.startswith("0.1"), f"{v1!r}")

        v2 = nudge(ctl, "+0.1", "second")
        check("第 2 次微调后 sidecar = 0.200  ← 这一步在 Windows rename 下会失败",
              v2.startswith("0.2"), f"{v2!r}（卡在首次值说明 rename 未覆盖）")

        v3 = nudge(ctl, "-0.5", "third")
        check("第 3 次微调后 sidecar = -0.300", v3.startswith("-0.3"), f"{v3!r}")

    finally:
        try:
            ctl.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"  (stop 失败: {exc})")

    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"手动 sidecar 实测: {passed}/{len(results)} 通过")
    if passed < len(results):
        print("  → 若第 2/3 次失败：publish_manual_sidecar 缺少 os.remove（Windows rename 不覆盖）")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
