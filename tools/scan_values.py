"""Lead pre-publish scan #2: look for actual credential VALUES.

The first scanner flags NAMES (the string "SESSDATA" appears legitimately as a
dict key in the cookie-handling code). This one looks for the shape a real
credential has: a long, opaque literal. Anything that is a URL, a path, a format
string or an identifier with dots/slashes is skipped, so what remains is worth a
human look.

Run:  python tools/scan_values.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git", "bin", "vendor", "logs", "state",
             "_publish", "config"}
LONG_LITERAL = re.compile(r"""['"]([A-Za-z0-9_\-%.]{28,})['"]""")


def main() -> int:
    hits = 0
    checked = 0
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith("test_") or path.name.startswith("scan_"):
            continue
        checked += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for m in LONG_LITERAL.finditer(line):
                tok = m.group(1)
                # URLs, paths, dotted identifiers and format specs are fine.
                if tok.startswith("http") or "/" in tok or "." in tok:
                    continue
                if "%" in tok or "\\" in tok:
                    continue
                hits += 1
                print(f"  {path.relative_to(ROOT)}:{i}: {tok[:60]}")
    print(f"\n检查 {checked} 个源文件，可疑长字面量 {hits} 处")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
