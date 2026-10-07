"""Launch an isolated browser, let the user log in, then grab cookies via CDP.

Why CDP instead of reading the cookie database:
    Modern Chrome encrypts cookie values with "App-Bound Encryption". Trying to
    decrypt the profile DB from another process fails -- that is exactly why
    `yt-dlp --cookies-from-browser chrome` errors out on this machine. Asking
    the browser itself over the DevTools Protocol returns the *decrypted* values
    with no key juggling, and it works for current Chrome versions.

Isolation:
    We launch with a dedicated --user-data-dir, so the user's normal Chrome
    profile is never read or modified. A separate debugging port is used.

Usage:
    python browser_login.py start        # open browser, wait for login
    python browser_login.py grab         # write cookies.txt from the session
    python browser_login.py all          # start + wait for Enter + grab
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "state" / "browser-profile"
COOKIE_OUT = ROOT / "state" / "cookies.txt"
DEBUG_PORT = 9333

CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

LOGIN_URLS = [
    "https://www.bilibili.com/",
    "https://passport.bilibili.com/login",
]


def find_browser() -> Path | None:
    for p in CHROME_CANDIDATES:
        if p.exists():
            return p
    return None


def cdp_endpoint(port: int = DEBUG_PORT) -> str | None:
    """Return the DevTools websocket URL, or None if the browser isn't up."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            return json.loads(r.read().decode())["webSocketDebuggerUrl"]
    except (urllib.error.URLError, KeyError, ValueError, OSError):
        return None


def launch(port: int = DEBUG_PORT) -> subprocess.Popen | None:
    """Start an isolated browser with remote debugging enabled."""
    browser = find_browser()
    if browser is None:
        print("Chrome or Edge not found.")
        return None

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        str(browser),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        # A fresh, isolated profile: no relation to the user's daily Chrome.
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        "--new-window",
        *LOGIN_URLS,
    ]
    print(f"Launching: {browser.name}")
    print(f"Isolated profile: {PROFILE_DIR}")
    print("(your normal Chrome profile is not touched)\n")
    return subprocess.Popen(args)


def wait_for_browser(port: int = DEBUG_PORT, timeout: int = 45) -> bool:
    for _ in range(timeout * 2):
        if cdp_endpoint(port):
            return True
        time.sleep(0.5)
    return False


def list_page_targets(port: int = DEBUG_PORT) -> list[dict]:
    """Return CDP page targets (each has its own websocket URL)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
            targets = json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError, OSError):
        return []
    return [
        t for t in targets
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]


def grab_cookies(port: int = DEBUG_PORT, domain_hint: str = "bilibili") -> list[dict]:
    """Ask the browser for cookies via CDP.

    IMPORTANT: connect to a *page* target, not the browser-level endpoint.
    Measured: the browser endpoint replies to Network.getAllCookies with
    error -32601 "'Network.getAllCookies' wasn't found", because the Network
    domain is only exposed on page/tab sessions. A page target returns the full
    cookie jar (33 cookies here, 23 of them bilibili).
    """
    targets = list_page_targets(port)
    if not targets:
        raise RuntimeError(
            f"no reachable page on debug port {port}. "
            "Start login-browser.cmd first and keep the window open."
        )

    # Prefer a bilibili page; otherwise use whatever page is open.
    page = next(
        (t for t in targets if domain_hint in str(t.get("url", ""))),
        targets[0],
    )

    import websocket  # websocket-client

    # Chrome rejects WebSocket handshakes that carry an Origin header, but
    # websocket-client sends one by default. That is what produces:
    #   "Handshake status 403 Forbidden ... Rejected an incoming WebSocket
    #    connection from the http://127.0.0.1:9333 origin"
    # Suppressing Origin is narrower than launching Chrome with
    # --remote-allow-origins=* (which would force a relaunch and re-login).
    try:
        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=20, suppress_origin=True
        )
    except TypeError:
        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=20, header=["Origin: "]
        )

    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        deadline = time.time() + 20
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            if "error" in msg:
                raise RuntimeError(f"CDP error: {msg['error']}")
            cookies = msg.get("result", {}).get("cookies", [])
            if domain_hint:
                cookies = [
                    c for c in cookies
                    if domain_hint in (c.get("domain") or "")
                ]
            return cookies
    finally:
        ws.close()
    return []


def to_netscape(cookies: list[dict]) -> str:
    """Render CDP cookies as a Netscape cookies.txt for yt-dlp.

    The Netscape format has exactly SEVEN tab-separated fields:
        domain, include_subdomains, path, secure, expires, name, value

    Do NOT append httpOnly as an eighth field. Measured: adding it makes yt-dlp
    reject every single line with
        "skipping cookie file entry due to invalid length 8"
    which silently yields an empty cookie jar -- and bilibili then answers
    HTTP 412 on every request, looking exactly like "not logged in".
    """
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by music-video-matcher via CDP",
        "",
    ]
    for c in cookies:
        domain = c.get("domain", "")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires < 0:
            expires = 0
        lines.append("\t".join([
            domain, include_sub, c.get("path", "/"), secure,
            str(expires), c.get("name", ""), c.get("value", ""),
        ]))
    return "\n".join(lines) + "\n"


def cmd_start() -> int:
    if cdp_endpoint():
        print(f"Browser already running on debug port {DEBUG_PORT}.")
    else:
        proc = launch()
        if proc is None:
            return 1
        if not wait_for_browser():
            print("Timed out waiting for the browser.")
            return 1
        print("Browser is ready (isolated profile; your normal browser is untouched).")

    print("\n" + "=" * 58)
    print("Log in to bilibili in the window that just opened:")
    print("  1. Sign in at www.bilibili.com (QR code is easiest)")
    print("  2. Keep the window open")
    print("=" * 58)
    return 0


def cmd_grab() -> int:
    try:
        cookies = grab_cookies()
    except RuntimeError as exc:
        print(f"FAILED: {exc}")
        return 1

    if not cookies:
        print("No bilibili cookies found.")
        print("-> Make sure the browser is still open and you are logged in.")
        return 1

    COOKIE_OUT.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_OUT.write_text(to_netscape(cookies), encoding="utf-8")

    names = {c.get("name") for c in cookies}
    print(f"OK: wrote {len(cookies)} cookies -> {COOKIE_OUT}")
    key = {"SESSDATA", "bili_jct", "DedeUserID", "buvid3"}
    print(f"    key cookies: {', '.join(sorted(names & key)) or '(none)'}")
    if "SESSDATA" not in names:
        print("    WARNING: no SESSDATA -- you are probably not logged in yet.")
        return 1
    return 0


def cmd_all() -> int:
    rc = cmd_start()
    if rc != 0:
        return rc
    try:
        input("\nPress Enter after you have logged in... ")
    except (EOFError, KeyboardInterrupt):
        print()
    return cmd_grab()


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "start":
        return cmd_start()
    if cmd == "grab":
        return cmd_grab()
    if cmd == "all":
        return cmd_all()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
