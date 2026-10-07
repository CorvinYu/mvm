"""SMTC (System Media Transport Controls) reader.

Reads "now playing" information from any Windows media session -- the same
mechanism Wallpaper Engine uses. This is how we implement "follow whatever the
user's other player is playing".

Why PowerShell under the hood:
    pip install winsdk / winrt is blocked in this environment, but
    Windows PowerShell 5.1 can call WinRT directly with zero dependencies.
    We shell out to src/smtc_reader.ps1 and parse its JSON output.

Key limitation (documented, important):
    SMTC exposes METADATA ONLY -- title/artist/duration/position.
    It does NOT give access to the audio stream. Any precise A/V alignment
    therefore needs a separate audio capture path (see notes in CLAUDE.md).

Whitelist (fail-closed, config/follow_whitelist.json):
    SMTC lists EVERY app that registers a media session -- browsers, short-video
    players, even sessions that are merely Paused. "Follow anything" therefore
    wakes the video window for software that has nothing to do with music.

    Blacklist vs whitelist failure modes are asymmetric (measured in
    `体检报告/02` §4.0): a blacklist that is missing an entry causes *wrong*
    follows (silent, dangerous), while a whitelist that is missing an entry
    causes *no* follow (loud, the user notices and fixes the config).
    So the decision is admission-based: only apps listed in the whitelist may
    be followed, and everything unknown is denied.

    Two independent gates (both required by the user's bug report):
      1. app_id must pass the whitelist (deny[] first, then apps[]);
      2. `require_playing` -- a Paused session must NOT wake the video
         (the old fallback branch only checked `is_usable`).

    Verdict order (short-circuits, see `evaluate_session`):
      broken file -> deny everything with an explicit error (never silently
      fail open) -> enabled==false -> skip whitelist (logged warning) ->
      deny[] hit -> reject -> apps[] hit -> admit -> unknown -> reject ->
      require_playing -> is_usable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
READER = SRC_DIR / "smtc_reader.ps1"
DEFAULT_WHITELIST_PATH = SRC_DIR.parent / "config" / "follow_whitelist.json"

# Windows PowerShell (not pwsh) -- WinRT projection is most reliable here.
POWERSHELL = "powershell"

# Legacy blacklist, kept only as a secondary net. It is NOT the main filter any
# more: it can only ever block app_ids that literally contain "mvm", and it has
# never matched our own window in practice (app_id != window title). The
# whitelist is what actually decides.
_IGNORED_APP_SUBSTRINGS = ("mvm",)

# Used when config/follow_whitelist.json is missing (on_missing_file) or when the
# file is broken and on_broken_file says to fall back. Only entries that were
# MEASURED on this machine are included -- no guessed app_ids (see 02 §5.4).
_BUILTIN_WHITELIST: dict = {
    "version": 1,
    "enabled": True,
    "require_playing": True,
    "on_unknown_app": "deny",
    "on_missing_file": "builtin_default",
    "on_broken_file": "deny_all_with_error",
    "apps": [
        {"match": "exact", "pattern": "汽水音乐", "label": "汽水音乐"},
    ],
    "deny": [
        {"match": "regex", "pattern": r"^mpvnet\.exe$", "label": "用户日常 mpv.net（铁律1禁碰）"},
        {"match": "regex", "pattern": r"^msedgewebview2\.exe$", "label": "WebView2 宿主（实测：红果短剧）"},
        {"match": "regex", "pattern": r"^(chrome|msedge|firefox)\.exe$", "label": "浏览器"},
    ],
}


@dataclass(frozen=True)
class NowPlaying:
    """A snapshot of one media session."""

    app_id: str
    title: str
    artist: str
    album: str
    status: str
    position_sec: float
    duration_sec: float

    @property
    def is_playing(self) -> bool:
        return self.status.lower() == "playing"

    @property
    def is_usable(self) -> bool:
        """True when there is enough info to attempt a PV lookup."""
        return bool(self.title.strip()) and self.duration_sec > 0

    def summary(self) -> str:
        return (
            f"[{self.app_id}] {self.title} - {self.artist} "
            f"({self.status}, {self.position_sec:.1f}s/{self.duration_sec:.1f}s)"
        )


# --------------------------------------------------------------------------
# Whitelist: rules, loading, verdicts
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Case-insensitive, Unicode-normalised comparison key.

    NFC first: the same CJK app_id can arrive decomposed from different
    sources, which would make an `exact` rule miss for no good reason.
    """
    return unicodedata.normalize("NFC", text).casefold()


@dataclass(frozen=True)
class WhitelistRule:
    """One `apps[]` / `deny[]` entry.

    `match` semantics (case-insensitive throughout):
      exact  -> app_id.casefold() == pattern.casefold()
      regex  -> re.fullmatch(pattern, app_id, re.IGNORECASE)   (NEVER search)
      substr -> pattern.casefold() in app_id.casefold()
    """

    match: str
    pattern: str
    label: str = ""

    def matches(self, app_id: str) -> bool:
        kind = (self.match or "exact").strip().lower()
        if kind == "exact":
            return _norm(app_id) == _norm(self.pattern)
        if kind == "regex":
            try:
                # fullmatch is mandatory: `search` is just a substring test and
                # would re-introduce the measured `--app mpv` -> `mpvnet.exe`
                # false positive (02 §3.5 / §4.3).
                return re.fullmatch(self.pattern, app_id, re.IGNORECASE) is not None
            except re.error:
                return False
        if kind == "substr":
            return _norm(self.pattern) in _norm(app_id)
        return False

    @property
    def name(self) -> str:
        return self.label or self.pattern


@dataclass
class Whitelist:
    """A loaded whitelist plus the policies from the JSON file."""

    path: Path | None = None
    # "file" | "builtin_default" | "builtin_default_after_broken" | "broken"
    source: str = "file"
    enabled: bool = True
    require_playing: bool = True
    on_unknown_app: str = "deny"
    on_missing_file: str = "builtin_default"
    apps: list[WhitelistRule] = field(default_factory=list)
    deny: list[WhitelistRule] = field(default_factory=list)
    broken: bool = False
    error: str = ""

    @property
    def allow_labels(self) -> str:
        return ", ".join(r.name for r in self.apps) or "（空）"

    def describe(self) -> str:
        if self.broken:
            return f"❌ 损坏 → 拒绝一切（{self.error}）"
        if not self.enabled:
            return "⚠ 已禁用（enabled=false）→ 退回旧行为（任何会话都可能被跟随）"
        return (
            f"{self.source}｜require_playing={str(self.require_playing).lower()}"
            f"｜on_unknown_app={self.on_unknown_app}"
            f"｜apps=[{self.allow_labels}]｜deny={len(self.deny)} 条"
        )


@dataclass(frozen=True)
class Verdict:
    """Why a session may (not) be followed."""

    allowed: bool
    reason: str


_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Print a warning at most once per process (the follow loop polls every 2s)."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(message, file=sys.stderr, flush=True)


def _rules_from(raw, where: str) -> list[WhitelistRule]:
    rules: list[WhitelistRule] = []
    if raw is None:
        return rules
    if not isinstance(raw, list):
        raise ValueError(f"{where} 必须是数组")
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{where}[{i}] 必须是对象")
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{where}[{i}].pattern 必须是非空字符串")
        match = str(item.get("match", "exact")).strip().lower()
        if match not in ("exact", "regex", "substr"):
            raise ValueError(f"{where}[{i}].match 非法: {match!r}")
        rules.append(
            WhitelistRule(
                match=match,
                pattern=pattern,
                label=str(item.get("label", "") or ""),
            )
        )
    return rules


def _parse_whitelist(data, path: Path | None) -> Whitelist:
    if not isinstance(data, dict):
        raise ValueError("顶层必须是 JSON 对象")
    wl = Whitelist(
        path=path,
        source="file",
        enabled=bool(data.get("enabled", True)),
        require_playing=bool(data.get("require_playing", True)),
        on_unknown_app=str(data.get("on_unknown_app", "deny")).strip().lower(),
        on_missing_file=str(data.get("on_missing_file", "builtin_default")).strip().lower(),
        apps=_rules_from(data.get("apps"), "apps"),
        deny=_rules_from(data.get("deny"), "deny"),
    )
    # Any value other than an explicit allow is treated as deny (fail-closed).
    if wl.on_unknown_app != "allow":
        wl.on_unknown_app = "deny"
    return wl


def _builtin_whitelist(path: Path | None, source: str) -> Whitelist:
    wl = _parse_whitelist(_BUILTIN_WHITELIST, path)
    wl.source = source
    return wl


def _load_whitelist_uncached(path: Path) -> Whitelist:
    """Read one whitelist file. Never fails open: a broken file denies all."""
    try:
        # utf-8-sig: Windows editors (Notepad, PowerShell Set-Content) often add a
        # UTF-8 BOM, which plain json.loads rejects. A BOM is not a corrupt file,
        # so it must not be treated as one -- that would deny everything for a
        # config the user believes is fine.
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        # File missing/unreadable -> on_missing_file governs. Its default is
        # builtin_default: "no config yet" must not mean "follow nothing", or a
        # first run would look broken. The policy can only come from the builtin
        # defaults here -- the file that would otherwise carry it is the absent one.
        policy = str(_BUILTIN_WHITELIST.get("on_missing_file", "builtin_default")).lower()
        if policy == "deny_all_with_error":
            return _broken_whitelist(path, "白名单文件不存在（on_missing_file=deny_all_with_error）")
        _warn_once(
            f"missing:{path}",
            f"· 未找到白名单文件 {path} —— 使用内置默认白名单"
            f"（apps: {', '.join(r.name for r in _builtin_whitelist(None, 'builtin_default').apps)}）",
        )
        return _builtin_whitelist(path, "builtin_default")

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return _broken_whitelist(path, f"JSON 解析失败: {exc}")

    # on_broken_file only governs a file that EXISTS but is malformed; a missing
    # file is handled above via on_missing_file.
    on_broken = "deny_all_with_error"
    if isinstance(data, dict):
        on_broken = str(data.get("on_broken_file", on_broken)).strip().lower()

    try:
        return _parse_whitelist(data, path)
    except ValueError as exc:
        if on_broken == "builtin_default":
            _warn_once(
                f"broken-builtin:{path}",
                f"⚠ 白名单文件损坏（{exc}）—— on_broken_file=builtin_default，"
                f"改用内置默认白名单",
            )
            return _builtin_whitelist(path, "builtin_default_after_broken")
        return _broken_whitelist(path, str(exc))


def _broken_whitelist(path: Path, error: str) -> Whitelist:
    """Fail-closed on a broken config: deny everything, say so loudly.

    Silently falling back to "follow anything" is the project's most expensive
    past mistake (`问题清单与尝试记录.md` §三), so this path never opens up.
    """
    _warn_once(
        f"broken:{path}",
        f"❌ 白名单文件损坏，拒绝一切跟随：{path}\n   原因：{error}\n"
        f"   修复该文件（或删除它以回退到内置默认白名单）后重试。",
    )
    return Whitelist(
        path=path,
        source="broken",
        enabled=True,
        require_playing=True,
        on_unknown_app="deny",
        apps=[],
        deny=[],
        broken=True,
        error=error,
    )


# path -> (mtime_ns | None, Whitelist)
_WL_CACHE: dict[str, tuple[int | None, Whitelist]] = {}


def load_whitelist(path: str | Path | None = None, *, force: bool = False) -> Whitelist:
    """Load (and cache) the follow whitelist.

    Cached by mtime so editing the JSON takes effect without a restart, while
    the 2-second poll loop does not re-read the file on every iteration.
    A broken file is deliberately NOT cached, so fixing it is picked up at once.
    """
    target = Path(path).expanduser() if path else DEFAULT_WHITELIST_PATH
    try:
        target = target.resolve()
    except OSError:
        pass
    key = str(target)

    try:
        mtime = target.stat().st_mtime_ns
    except OSError:
        mtime = None

    cached = _WL_CACHE.get(key)
    if (
        not force
        and cached is not None
        and cached[0] == mtime
        and cached[1].source != "broken"
    ):
        return cached[1]

    wl = _load_whitelist_uncached(target)
    _WL_CACHE[key] = (mtime, wl)
    return wl


def _app_admission(session: NowPlaying, whitelist: Whitelist) -> Verdict:
    """Steps 1-4 of the verdict order: whitelist admission only.

    Kept separate from the playing/usability gate so callers can tell "this app
    is not allowed at all" apart from "this app is allowed but not playing" --
    the difference is what makes a rejection diagnosable (02 §5.5 坑 5).
    """
    if whitelist.broken:
        return Verdict(False, f"✗ 白名单损坏 → 拒绝一切（{whitelist.error}）")

    if not whitelist.enabled:
        _warn_once(
            "whitelist-disabled",
            "⚠ config/follow_whitelist.json 中 enabled=false —— 白名单已跳过，"
            "退回旧行为（任何注册 SMTC 的软件都可能唤醒视频）。仅调试用。",
        )
        return Verdict(True, "✓ 白名单已禁用")

    # deny wins over apps (an explicit block must never be overridden).
    for rule in whitelist.deny:
        if rule.matches(session.app_id):
            return Verdict(False, f"✗ 不在白名单（命中 deny：{rule.name}）")
    for rule in whitelist.apps:
        if rule.matches(session.app_id):
            return Verdict(True, "✓ 白名单内")
    if whitelist.on_unknown_app == "allow":
        return Verdict(True, "✓ 白名单外但 on_unknown_app=allow")
    return Verdict(False, f"✗ 不在白名单（app_id={session.app_id!r}）")


def _base_gate(session: NowPlaying, whitelist: Whitelist) -> Verdict:
    """Steps 5-6 of the verdict order: playing state, then usability."""
    if whitelist.require_playing and not session.is_playing:
        # The old fallback returned Paused sessions here, which is exactly why a
        # stopped player still woke the video window (02 §2.2).
        return Verdict(False, f"✗ 非 Playing（{session.status or '未知'}）")
    if not session.is_usable:
        return Verdict(False, "✗ 缺 title 或 duration<=0")
    return Verdict(True, "✓ 通过")


def evaluate_session(
    session: NowPlaying,
    whitelist: Whitelist | None = None,
) -> Verdict:
    """Decide whether one session may be followed, with a human-readable reason."""
    wl = whitelist if whitelist is not None else load_whitelist()
    admission = _app_admission(session, wl)
    if not admission.allowed:
        return admission
    return _base_gate(session, wl)


# --------------------------------------------------------------------------
# Reading sessions
# --------------------------------------------------------------------------


def read_sessions(filter_app: str = "", timeout: int = 20) -> list[NowPlaying]:
    """Read all active SMTC sessions.

    Returns an empty list on any failure -- callers should treat "no sessions"
    and "reader failed" the same way (nothing to follow right now).
    """
    if not READER.exists():
        raise FileNotFoundError(f"SMTC reader not found: {READER}")

    cmd = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", str(READER),
    ]
    if filter_app:
        cmd += ["-FilterApp", filter_app]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            # PowerShell writes UTF-8 once OutputEncoding is set; decode leniently.
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    return _parse(proc.stdout)


def _parse(raw: str) -> list[NowPlaying]:
    """Parse the reader's JSON. Handles PowerShell's single-object-vs-array quirk."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    # ConvertTo-Json wraps arrays in {"value": [...], "Count": n} when the
    # pipeline emits a collection; a single item may arrive unwrapped.
    if isinstance(data, dict):
        items = data.get("value", [])
    else:
        items = data
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []

    sessions: list[NowPlaying] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        app_id = str(item.get("app_id", ""))
        if any(s in app_id.lower() for s in _IGNORED_APP_SUBSTRINGS):
            continue
        try:
            sessions.append(
                NowPlaying(
                    app_id=app_id,
                    title=str(item.get("title", "")),
                    artist=str(item.get("artist", "")),
                    album=str(item.get("album", "")),
                    status=str(item.get("status", "")),
                    position_sec=float(item.get("position_sec") or 0.0),
                    duration_sec=float(item.get("duration_sec") or 0.0),
                )
            )
        except (TypeError, ValueError):
            continue
    return sessions


# --------------------------------------------------------------------------
# Choosing which session to follow
# --------------------------------------------------------------------------


def pick_session(
    sessions: list[NowPlaying],
    prefer_app: str = "",
    whitelist: Whitelist | None = None,
    any_app: bool = False,
) -> NowPlaying | None:
    """Choose which session to follow. Fail-closed by default.

    Admission first, then selection:
      1. Every candidate must pass `evaluate_session` (whitelist + playing +
         usable). `--app` can NOT bypass the whitelist -- otherwise asking for
         `--app 汽水` would silently re-open the bug.
      2. `prefer_app` given  -> strict substring filter among admitted sessions
         (case-insensitive), keeping the old rule that the session must be
         Playing. Not present -> None, with an explicit message on stderr.
      3. no `prefer_app`     -> deterministic pick among admitted sessions:
         longest track first, then app_id (stable across polls).
      4. `any_app=True`      -> explicit escape hatch: skip the app admission
         (still requires Playing / usable, still logged). Debug/interactive use
         only; the default must never be lenient.
    """
    wl = whitelist if whitelist is not None else load_whitelist()

    if any_app:
        _warn_once(
            "any-app",
            "⚠ --any-app 逃生舱已启用：白名单准入被显式绕过（仍要求 Playing 且可用的会话）。",
        )

    def verdict_for(session: NowPlaying) -> Verdict:
        if any_app:
            # Even the escape hatch stays closed when the config is broken --
            # otherwise a corrupt file would look like a working setup.
            if wl.broken:
                return Verdict(False, f"✗ 白名单损坏 → 拒绝一切（{wl.error}）")
            return _base_gate(session, wl)
        return evaluate_session(session, wl)

    if prefer_app:
        needle = _norm(prefer_app)
        matching = [s for s in sessions if needle in _norm(s.app_id)]
        if not matching:
            seen = ", ".join(describe_choices(sessions)) or "无"
            print(
                f'✗ --app "{prefer_app}" 未命中任何会话（当前会话：{seen}）',
                file=sys.stderr,
                flush=True,
            )
            return None

        # --app narrows the choice; it never widens it (02 §4.4). If the
        # preferred app is not admissible, say exactly why instead of silently
        # returning None -- that is what makes "no reaction" diagnosable.
        admitted: list[NowPlaying] = []
        not_admitted: list[tuple[NowPlaying, str]] = []
        for session in matching:
            verdict = verdict_for(session)
            if verdict.allowed:
                admitted.append(session)
            else:
                not_admitted.append((session, verdict.reason))

        if not admitted:
            for session, reason in not_admitted:
                print(f"  {session.summary()}  {reason}", file=sys.stderr, flush=True)
            if not any_app and all(
                _app_admission(s, wl).allowed for s, _ in not_admitted
            ):
                # Whitelisted, but nothing to follow right now (usually Paused).
                print(
                    f'✗ --app "{prefer_app}" 在白名单内，但没有正在播放的会话'
                    f"（require_playing={str(wl.require_playing).lower()}）",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f'✗ --app "{prefer_app}" 未命中白名单'
                    f"（当前白名单：{wl.allow_labels}）；"
                    f"如确需跟随请加入 config/follow_whitelist.json，或用 --any-app 显式绕过",
                    file=sys.stderr,
                    flush=True,
                )
            return None

        # Preserved strict semantics: the preferred app must be playing.
        for session in admitted:
            if session.is_playing:
                return session
        return None

    # No preference: every candidate must clear the whitelist first, so the
    # "longest track wins" sort can only ever choose among admitted apps.
    allowed = [s for s in sessions if verdict_for(s).allowed]

    playing = [s for s in allowed if s.is_playing]
    if playing:
        playing.sort(key=lambda s: (-s.duration_sec, s.app_id.lower()))
        return playing[0]

    if not wl.require_playing:
        # Only when the user explicitly turned require_playing off: fall back to
        # "usable" sessions -- but still whitelist-only, never anything else.
        usable = [s for s in allowed if s.is_usable]
        if usable:
            usable.sort(key=lambda s: (-s.duration_sec, s.app_id.lower()))
            return usable[0]

    return None


def describe_choices(sessions: list[NowPlaying]) -> list[str]:
    """Human-readable list of followable app ids (for prompts/CLI)."""
    seen: list[str] = []
    for s in sessions:
        if s.app_id and s.app_id not in seen:
            seen.append(s.app_id)
    return seen


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    """CLI: print current now-playing state plus the whitelist verdict."""
    parser = argparse.ArgumentParser(
        description="查看 SMTC 会话与白名单裁决（fail-closed）",
    )
    parser.add_argument("--app", default="", help="只跟随 app_id 含该子串的会话（仍受白名单约束）")
    parser.add_argument(
        "--any-app",
        action="store_true",
        help="逃生舱：显式绕过白名单准入（日志警告）",
    )
    parser.add_argument("--whitelist", default="", help="用指定的白名单文件替代 config/follow_whitelist.json")
    args = parser.parse_args(argv)

    whitelist = load_whitelist(args.whitelist or None)
    print(f"白名单：{whitelist.describe()}")

    sessions = read_sessions()
    if not sessions:
        print("(no SMTC sessions)")
        print("\n-> 不跟随（nothing usable to follow）")
        return 1

    for s in sessions:
        verdict = evaluate_session(s, whitelist)
        mark = "✓ 白名单内" if verdict.allowed else verdict.reason
        print(f"  {s.summary()}  {mark}")

    chosen = pick_session(
        sessions,
        prefer_app=args.app,
        whitelist=whitelist,
        any_app=args.any_app,
    )
    if chosen:
        print(f"\n-> would follow: {chosen.summary()}")
        return 0

    print("\n-> 不跟随（nothing usable to follow）")
    return 1


if __name__ == "__main__":
    sys.exit(_main())
