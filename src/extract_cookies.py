"""Extract bilibili cookies from a Chromium profile into Netscape cookies.txt.

Why this exists:
    yt-dlp's --cookies-from-browser fails on this machine (Chrome's DB is locked
    while running, and Edge's cookies fail DPAPI decryption). So we read the
    profile DB ourselves and decrypt with Windows DPAPI.

How Chromium encrypts cookies (modern versions):
    * The AES key is stored in Local State -> os_crypt.encrypted_key,
      base64-encoded, prefixed with "DPAPI", and itself DPAPI-protected.
    * Each cookie value is "v10"/"v11" + 12-byte nonce + ciphertext + 16-byte
      GCM tag, encrypted with AES-256-GCM.
    * Older entries may be raw DPAPI blobs (no v10 prefix).

Requires: pywin32 (present) + cryptography (installed on demand below).
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import shutil
import sys
import tempfile
from pathlib import Path

# Domain filter: bilibili sets cookies on several subdomains.
BILIBILI_DOMAIN_HINT = "bilibili"

# Chrome epoch offset (1601-01-01 -> 1970-01-01) in microseconds.
_CHROME_EPOCH_OFFSET_US = 11644473600 * 1_000_000


def _load_cryptography():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except ImportError:
        return None


def _dpapi_unprotect(data: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def get_master_key(profile_dir: Path) -> bytes | None:
    """Read and decrypt the AES key from the profile's Local State."""
    for candidate in (profile_dir / "Local State", profile_dir.parent / "Local State"):
        if not candidate.exists():
            continue
        try:
            state = json.loads(candidate.read_text(encoding="utf-8"))
            enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
        except (KeyError, ValueError, OSError):
            continue
        if enc[:5] == b"DPAPI":
            enc = enc[5:]
        try:
            return _dpapi_unprotect(enc)
        except Exception:  # noqa: BLE001 - fall through to next candidate
            continue
    return None


def decrypt_value(blob: bytes, key: bytes | None) -> str:
    """Decrypt one cookie value; returns '' when undecryptable."""
    if not blob:
        return ""
    # Legacy: raw DPAPI blob.
    if not blob.startswith((b"v10", b"v11")):
        try:
            return _dpapi_unprotect(blob).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""

    AESGCM = _load_cryptography()
    if AESGCM is None or key is None:
        return ""
    try:
        nonce, payload = blob[3:15], blob[15:]
        return AESGCM(key).decrypt(nonce, payload, None).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def find_cookie_db(profile_dir: Path) -> Path | None:
    """Locate the Cookies SQLite file inside a Chromium profile dir."""
    for rel in ("Default/Network/Cookies", "Network/Cookies", "Default/Cookies", "Cookies"):
        p = profile_dir / rel
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def read_cookies(profile_dir: Path, domain_hint: str = BILIBILI_DOMAIN_HINT) -> list[dict]:
    """Read cookies from the profile, optionally filtered by domain."""
    db = find_cookie_db(profile_dir)
    if db is None:
        raise FileNotFoundError(f"未找到 Cookies 数据库，检查配置目录: {profile_dir}")

    # Chrome locks the DB while running -> copy it (and WAL) before reading.
    tmpdir = Path(tempfile.mkdtemp(prefix="mvm_cookies_"))
    local_db = tmpdir / "Cookies"
    shutil.copy2(db, local_db)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(local_db) + suffix))

    key = get_master_key(profile_dir)
    rows: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{local_db}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT host_key, name, value, encrypted_value, path, "
                "expires_utc, is_secure, is_httponly FROM cookies"
            )
            for host, name, value, enc, path, expires, secure, httponly in cur:
                if domain_hint and domain_hint not in (host or ""):
                    continue
                if not value and enc:
                    value = decrypt_value(bytes(enc), key)
                if not value:
                    continue
                rows.append({
                    "host": host or "",
                    "name": name or "",
                    "value": value,
                    "path": path or "/",
                    "expires_utc": expires or 0,
                    "secure": bool(secure),
                    "httponly": bool(httponly),
                })
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return rows


def to_netscape(rows: list[dict]) -> str:
    """Render cookies in Netscape format (what yt-dlp expects)."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by music-video-matcher (extract_cookies.py)",
        "",
    ]
    for r in rows:
        host = r["host"]
        # Netscape convention: a leading dot means "include subdomains".
        domain_field = host if host.startswith(".") else host
        include_sub = "TRUE" if host.startswith(".") else "FALSE"
        secure = "TRUE" if r["secure"] else "FALSE"
        if r["expires_utc"]:
            expires = int(r["expires_utc"] / 1_000_000 - 11644473600)
            if expires <= 0:
                expires = 0
        else:
            expires = 0
        httponly = "TRUE" if r["httponly"] else "FALSE"
        lines.append("\t".join([
            domain_field, include_sub, r["path"], secure,
            str(expires), r["name"], r["value"], httponly,
        ]))
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python extract_cookies.py <profile_dir> [output.txt] [domain_hint]")
        return 2
    profile_dir = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("cookies.txt")
    hint = sys.argv[3] if len(sys.argv) > 3 else BILIBILI_DOMAIN_HINT

    if not profile_dir.exists():
        print(f"配置目录不存在: {profile_dir}")
        return 1

    try:
        rows = read_cookies(profile_dir, domain_hint=hint)
    except FileNotFoundError as exc:
        print(f"失败: {exc}")
        return 1

    if not rows:
        print(f"该配置目录下没有匹配 {hint!r} 的 cookie。")
        print("→ 请确认已在打开的浏览器里登录对应网站。")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_netscape(rows), encoding="utf-8")

    print(f"✅ 已提取 {len(rows)} 条 cookie -> {out}")
    important = {"SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4", "b_nut"}
    got = sorted({r["name"] for r in rows} & important)
    print(f"   关键 cookie: {', '.join(got) if got else '(无 — 可能未登录)'}")
    if "SESSDATA" not in {r["name"] for r in rows}:
        print("   ⚠️ 未发现 SESSDATA —— B站取流需要它，请确认已登录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
