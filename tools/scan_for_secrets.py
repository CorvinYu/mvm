"""Lead pre-publish scan: look for secrets and machine-specific paths.

NOTES §3.3 is explicit that a public push must be preceded by a VALUE scan, not
just a .gitignore check: a filename rule cannot stop a credential that is pasted
inside a source file. This walks the files that are about to be published and
reports anything that looks like a credential, a local absolute path, or a
machine/user name.

Run:  python tools/scan_for_secrets.py <dir> [<dir> ...]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Patterns that must never appear in a published file.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("local-path-D", re.compile(r"[Dd]:\\software")),
    ("local-path-E", re.compile(r"[Ee]:\\claude")),
    ("user-profile", re.compile(r"C:\\Users\\", re.I)),
    ("username", re.compile(r"XHQSh", re.I)),
    ("bili-session", re.compile(r"SESSDATA|bili_jct|DedeUserID|sessdata", re.I)),
    ("cookie-jar", re.compile(r"cookies\.txt", re.I)),
    ("bearer", re.compile(r"(?:Bearer|token|api[_-]?key|secret)\s*[:=]\s*\S{8,}", re.I)),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# Files/extensions that are never published regardless of content.
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "bin", "vendor",
             "logs", "state", "_evidence"}
SKIP_SUFFIX = {".pyc", ".pyo", ".exe", ".dll", ".pyd", ".wav", ".mp4", ".mp3",
               ".log", ".ico", ".png", ".jpg"}


def scan(root: Path) -> int:
    hits = 0
    files = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  ! unreadable {path}: {exc}")
            continue
        for name, pat in PATTERNS:
            for i, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    hits += 1
                    rel = path.relative_to(root)
                    print(f"  [{name}] {rel}:{i}: {line.strip()[:110]}")
    print(f"\n扫描 {files} 个文件，命中 {hits} 处")
    return hits


def main() -> int:
    roots = [Path(a) for a in sys.argv[1:]] or [Path.cwd()]
    total = 0
    for r in roots:
        print(f"=== {r} ===")
        total += scan(r)
    if total:
        print("\n⚠ 存在命中项，推公开仓库前必须逐条确认或清理")
        return 1
    print("\n✓ 未发现凭证/本机路径特征")
    return 0


if __name__ == "__main__":
    sys.exit(main())
