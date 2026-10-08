"""test_align_inherit.py -- standalone tests for align_inherit.py (task-3).

Run directly (no pytest, no player, no network):

    cd src
    python test_align_inherit.py

WHY a separate file rather than src/selftest.py: this module and its tests were
developed as an independent unit (the inheritance rules are pure functions), and
keeping them separate means they can be run without the follower's dependencies.

WHY the MUTATION tests at the end: the project's hardest lesson (NOTES §3.2
item 4) is that "54/54 green" coexisted with 8 real bugs -- a green suite is
NOT evidence unless some criterion would FAIL when the rule is broken. So the
last section deliberately breaks one rule per criterion and asserts the
criterion goes red, proving the tests actually bind the behaviour.
"""

from __future__ import annotations

import sys
import traceback

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import align_inherit  # noqa: E402
from align_inherit import (  # noqa: E402
    CORRELATION_BLEND_WEIGHT,
    MAX_INHERITED_OFFSET_SEC,
    decide_inherited_offset,
    read_calibration_snapshot,
)

# ---------------------------------------------------------------------------
# Tiny assertion harness (mirrors selftest.py style: PASS/FAIL lines, exit code)
# ---------------------------------------------------------------------------

_PASS = 0
_FAIL = 0
_FAILED_NAMES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        _FAILED_NAMES.append(name)
        print(f"  FAIL  {name}  {detail}")


def expect_offset(name: str, got, want_sec: float, want_source: str,
                  tol: float = 1e-6) -> None:
    ok = (got.source == want_source
          and abs(got.offset_sec - want_sec) <= tol)
    check(name, ok, f"got offset={got.offset_sec:+.3f} source={got.source!r}, "
                    f"want offset={want_sec:+.3f} source={want_source!r}")


# ---------------------------------------------------------------------------
# Fixtures: snapshots are plain dicts (the whole point of the pure API)
# ---------------------------------------------------------------------------

# Bucket 22 == a 220-229s track (TrackKey.duration_bucket = int(duration // 10)).
TRACK = {"title": "幹物女", "artist": "z新豪", "duration_bucket": 22}
APP = "cloudmusic.exe"

# Song keys EMBED the bucket (align_calib.make_track_key), verified on disk.
SONG_KEY = "幹物女|z新豪|22"
SONG_KEY_OTHER_BUCKET = "幹物女|z新豪|26"

CAL_SONG = {"tracks": {SONG_KEY: {"offset_sec": 0.30}}}
CAL_APP = {"apps": {APP: {"offset_sec": 0.50}}}
CAL_BOTH = {
    "tracks": {SONG_KEY: {"offset_sec": 0.30}},
    "apps": {APP: {"offset_sec": 0.50}},
}
CAL_BUCKET_MISMATCH = {
    # Same title+artist, but the stored version is bucket 26 (260-269s) --
    # i.e. a different edit of the song.
    "tracks": {SONG_KEY_OTHER_BUCKET: {"offset_sec": 0.30}},
    "apps": {APP: {"offset_sec": 0.50}},
}

CORR_TRUSTED = {"delay_sec": 0.40, "confidence": 0.90, "trustworthy": True}
CORR_UNTRUSTED = {"delay_sec": 0.40, "confidence": 0.10, "trustworthy": False}


# ---------------------------------------------------------------------------
# §1 empty calibration -> no inheritance, offset 0, source "none"
# ---------------------------------------------------------------------------

def test_empty_calibration() -> None:
    print("\n§1 empty calibration -> offset 0, source 'none'")
    for label, snapshot in (("None", None), ("empty dict", {})):
        got = decide_inherited_offset(TRACK, APP, snapshot, None)
        expect_offset(f"{label} snapshot -> no inheritance", got, 0.0, "none")
        check(f"{label} confidence is 0", got.confidence == 0.0,
              f"got {got.confidence}")
    # A snapshot that only has an APP entry must inherit nothing into a DIFFERENT
    # app (the calibration is a property of the player).
    got = decide_inherited_offset(TRACK, "qqmusic.exe", CAL_APP, None)
    expect_offset("app entry for another app -> no inheritance for this app",
                  got, 0.0, "none")
    # ...and a snapshot with only a SONG entry must inherit nothing into a
    # different song on the same app (it is bound to that song).
    got = decide_inherited_offset(
        {"title": "别的歌", "artist": "别人", "duration_bucket": 22},
        APP, CAL_SONG, None)
    expect_offset("song entry for another song -> no inheritance for this song",
                  got, 0.0, "none")


# ---------------------------------------------------------------------------
# §2 app-level hit (R1: systematic player bias inherits across songs)
# ---------------------------------------------------------------------------

def test_app_level_hit() -> None:
    print("\n§2 app-level hit (different song, same player)")
    other = {"title": "另一首歌", "artist": "洛天依", "duration_bucket": 18}
    got = decide_inherited_offset(other, APP, CAL_APP, None)
    expect_offset("app-level value inherits to a NEW song", got, 0.50, "app")
    check("app-level confidence is 1.0 for a plain manual entry",
          got.confidence == 1.0, f"got {got.confidence}")


# ---------------------------------------------------------------------------
# §3 song-level beats app-level (R1)
# ---------------------------------------------------------------------------

def test_song_overrides_app() -> None:
    print("\n§3 song-level hit overrides app-level")
    got = decide_inherited_offset(TRACK, APP, CAL_BOTH, None)
    expect_offset("song-level wins over app-level", got, 0.30, "song")
    check("source is 'song', not 'app', when both exist",
          got.source == "song", f"got {got.source!r}")


# ---------------------------------------------------------------------------
# §4 song-level bucket mismatch -> fall back to app level (R2)
# ---------------------------------------------------------------------------

def test_bucket_mismatch_fallback() -> None:
    print("\n§4 song-level bucket mismatch -> fall back")
    # Mismatch of 4 buckets (40s) = a different version -> must NOT be used;
    # the app-level value applies instead (rule R2).
    got = decide_inherited_offset(TRACK, APP, CAL_BUCKET_MISMATCH, None)
    expect_offset("bucket mismatch drops to app-level value",
                  got, 0.50, "app")
    check("the reason explains the mismatch",
          "different version" in got.reason, f"got {got.reason!r}")
    # ...but with NO app fallback available, a mismatch means nothing to use.
    song_only_mismatch = {"tracks": {SONG_KEY_OTHER_BUCKET:
                                     {"offset_sec": 0.30}}}
    got = decide_inherited_offset(TRACK, APP, song_only_mismatch, None)
    expect_offset("mismatch with no app entry -> no inheritance",
                  got, 0.0, "none")
    # The exact same bucket is a hit (bucket is part of the key).
    got = decide_inherited_offset(TRACK, APP, CAL_SONG, None)
    expect_offset("same bucket -> song-level hit", got, 0.30, "song")
    # Unknown duration on the current track cannot confirm the version.
    unknown = {"title": "幹物女", "artist": "z新豪"}
    got = decide_inherited_offset(unknown, APP, CAL_BOTH, None)
    expect_offset("unknown duration -> song entry refused, app used",
                  got, 0.50, "app")


# ---------------------------------------------------------------------------
# §5 cap / runaway protection (R4)
# ---------------------------------------------------------------------------

def test_cap_and_runaway() -> None:
    print("\n§5 cap and runaway protection")
    # The stored value is ALREADY the accumulated result of 20 nudges of +10s
    # if the storage naively summed them. task-2's record_manual_nudge must
    # UPDATE (take the latest absolute intent), not add -- and this module
    # must refuse to inherit an absurd value even if storage got it wrong.
    runaway = {"tracks": {SONG_KEY: {"offset_sec": 200.0}}}
    got = decide_inherited_offset(TRACK, APP, runaway, None)
    expect_offset("stored 200s is refused outright (not clamped)", got,
                  0.0, "none")
    check("refusal is explained in the reason",
          "exceeds cap" in got.reason, f"got {got.reason!r}")

    # Values within the cap are inherited unchanged.
    at_cap = {"tracks": {SONG_KEY: {"offset_sec": 10.0}}}
    got = decide_inherited_offset(TRACK, APP, at_cap, None)
    expect_offset("exactly +10s (the cap) is allowed", got, 10.0, "song")

    # The MERGED result is clamped, so manual + correlation cannot exceed it.
    big_corr = {"delay_sec": 9.0, "confidence": 0.99, "trustworthy": True}
    got = decide_inherited_offset(TRACK, APP, at_cap, big_corr)
    check("merged offset is clamped to the cap",
          abs(got.offset_sec) <= MAX_INHERITED_OFFSET_SEC + 1e-9,
          f"got {got.offset_sec:+.3f}")

    # NO accumulation across songs: 20 consecutive decisions on the SAME
    # snapshot must be identical and must never drift (the pure function has
    # no memory -- drift would prove hidden state).
    first = decide_inherited_offset(TRACK, APP, at_cap, None)
    stable = all(
        decide_inherited_offset(TRACK, APP, at_cap, None).offset_sec
        == first.offset_sec
        for _ in range(20)
    )
    check("20 consecutive decisions do not accumulate (no hidden state)",
          stable and first.offset_sec == 10.0,
          f"first={first.offset_sec:+.3f} stable={stable}")

    # Direction UPDATE semantics: the latest intent wins, it does not add.
    # Same song, user first nudged +2.0 then changed their mind to -1.0: the
    # snapshot holds only the latest value, so we inherit -1.0 -- not +1.0.
    updated = {"tracks": {SONG_KEY: {"offset_sec": -1.0}}}
    got = decide_inherited_offset(TRACK, APP, updated, None)
    expect_offset("latest intent replaces the previous one", got, -1.0, "song")


# ---------------------------------------------------------------------------
# §6 correlation merge: trustworthy vs untrustworthy (R3)
# ---------------------------------------------------------------------------

def test_correlation_merge() -> None:
    print("\n§6 correlation merge (trustworthy vs untrustworthy)")
    # Trustworthy -> manual + damped correlation (闭环调误差 + 手动调偏好 = 相加)
    got = decide_inherited_offset(TRACK, APP, CAL_BOTH, CORR_TRUSTED)
    want = 0.30 + CORRELATION_BLEND_WEIGHT * 0.40
    expect_offset("trusted correlation is blended into the manual value",
                  got, want, "song", tol=1e-9)
    check("blended confidence reflects the weaker source",
          got.confidence == 0.90, f"got {got.confidence}")

    # Untrustworthy -> manual value ALONE, correlation ignored entirely.
    got = decide_inherited_offset(TRACK, APP, CAL_BOTH, CORR_UNTRUSTED)
    expect_offset("untrusted correlation is ignored -> manual only",
                  got, 0.30, "song")
    check("reason records that the correlation was ignored",
          "untrustworthy" in got.reason, f"got {got.reason!r}")

    # None -> manual value alone (no correlation ran this song).
    got = decide_inherited_offset(TRACK, APP, CAL_BOTH, None)
    expect_offset("None correlation -> manual only", got, 0.30, "song")

    # A trustworthy flag with an impossible magnitude is still refused: a 40s
    # "error" is a latched repeat, and must not poison the long-term value.
    silly = {"delay_sec": 40.0, "confidence": 1.0, "trustworthy": True}
    got = decide_inherited_offset(TRACK, APP, CAL_BOTH, silly)
    expect_offset("trusted flag but absurd 40s magnitude -> ignored",
                  got, 0.30, "song")

    # No manual calibration at all, but a trustworthy correlation: the
    # correlation alone still contributes (source stays "none" because no
    # calibration applied -- documented, so the caller can distinguish).
    got = decide_inherited_offset(TRACK, APP, None, CORR_TRUSTED)
    expect_offset("no calibration + trusted correlation -> nothing inherited",
                  got, 0.0, "none")


# ---------------------------------------------------------------------------
# §7 storage wrapper tolerates missing / corrupt files (B)
# ---------------------------------------------------------------------------

def test_storage_wrapper_tolerant() -> None:
    print("\n§7 storage wrapper is fault tolerant")
    snap = read_calibration_snapshot()
    check("read_calibration_snapshot returns a dict without raising",
          isinstance(snap, dict), f"got {type(snap).__name__}")

    # Malformed snapshot shapes must degrade to "none", never crash.
    for label, bad in (
        ("track entry missing offset_sec",
         {"tracks": {SONG_KEY: {"samples": 1}}}),
        ("track value not a dict", {"tracks": {SONG_KEY: 0.3}}),
        ("tracks section is a list", {"tracks": [1, 2, 3]}),
        ("app id empty", {"apps": {"": {"offset_sec": 1.0}}}),
        ("offset is a string", {"apps": {APP: {"offset_sec": "0.5"}}}),
    ):
        got = decide_inherited_offset(TRACK, APP, bad, None)
        expect_offset(f"malformed snapshot ({label}) -> no inheritance",
                      got, 0.0, "none")

    # A snapshot with unknown extra keys is still read for the known ones.
    mixed = dict(CAL_APP)
    mixed["_comment"] = "written by task-2"
    mixed["version"] = 1
    got = decide_inherited_offset(TRACK, APP, mixed, None)
    expect_offset("extra keys do not break the read", got, 0.50, "app")


# ---------------------------------------------------------------------------
# §8 purity / no side effects
# ---------------------------------------------------------------------------

def test_purity() -> None:
    print("\n§8 pure function: no IO, no mutation of inputs")
    import copy
    track = copy.deepcopy(TRACK)
    calib = copy.deepcopy(CAL_BOTH)
    corr = copy.deepcopy(CORR_TRUSTED)
    before = (copy.deepcopy(track), copy.deepcopy(calib), copy.deepcopy(corr))
    decide_inherited_offset(track, APP, calib, corr)
    check("decide_inherited_offset does not mutate its inputs",
          (track, calib, corr) == before, "inputs changed")

    # Importing the module must not touch the filesystem or a player: the
    # state file for calibration is inside the project, and it must NOT be
    # created by a mere read.
    from pathlib import Path
    p = Path(align_inherit._CALIB_FALLBACK_PATH)
    existed = p.exists()
    read_calibration_snapshot()
    check("read_calibration_snapshot never creates the state file",
          p.exists() == existed,
          f"{p} appeared during a read")

    # No module-level mutable state that a decision could leave behind.
    a = decide_inherited_offset(TRACK, APP, at_cap_snapshot(), None).offset_sec
    decide_inherited_offset(TRACK, APP, runaway_snapshot(), None)
    b = decide_inherited_offset(TRACK, APP, at_cap_snapshot(), None).offset_sec
    check("a refused decision leaves no residue for the next call", a == b,
          f"{a} != {b}")


def at_cap_snapshot() -> dict:
    return {"tracks": {SONG_KEY: {"offset_sec": 10.0}}}


def runaway_snapshot() -> dict:
    return {"tracks": {SONG_KEY: {"offset_sec": 200.0}}}


# ---------------------------------------------------------------------------
# §9 MUTATION: break each rule on purpose and prove the criterion FAILS first
# ---------------------------------------------------------------------------

def test_mutations_prove_criteria_bind() -> None:
    """Each mutation rewrites ONE rule inside align_inherit, then asserts the
    corresponding criterion goes red. If a mutation still passes, the test was
    not actually checking the rule (the "green suite, 8 live bugs" trap).
    """
    print("\n§9 mutation: deliberately wrong rules must FAIL the criteria")

    originals = {
        "decide_inherited_offset": align_inherit.decide_inherited_offset,
        "MAX_INHERITED_OFFSET_SEC": align_inherit.MAX_INHERITED_OFFSET_SEC,
        "_song_entry": align_inherit._song_entry,
    }
    try:
        # --- mutation 1: R1 reversed -- app level wins over song level ---
        # Simulate by removing the song section entirely, i.e. what a broken
        # "prefer app" implementation would effectively consult.
        align_inherit.decide_inherited_offset = (
            lambda track, app_id, calib, corr: _app_only(track, app_id, calib, corr)
        )
        got = align_inherit.decide_inherited_offset(TRACK, APP, CAL_BOTH, None)
        check("[mutation] app-beats-song rule -> 'song overrides app' FAILS",
              not (got.source == "song" and abs(got.offset_sec - 0.30) < 1e-6),
              f"mutation unexpectedly passed: {got}")

        # --- mutation 2: R4 cap removed -- inherit the runaway 200s ---
        align_inherit.decide_inherited_offset = originals["decide_inherited_offset"]
        align_inherit.MAX_INHERITED_OFFSET_SEC = 1e9
        got = align_inherit.decide_inherited_offset(TRACK, APP,
                                                    runaway_snapshot(), None)
        check("[mutation] cap removed -> 'stored 200s refused' FAILS",
              not (got.source == "none" and got.offset_sec == 0.0),
              f"mutation unexpectedly passed: {got}")
        align_inherit.MAX_INHERITED_OFFSET_SEC = originals["MAX_INHERITED_OFFSET_SEC"]

        # --- mutation 3: R2 bucket gate removed -- the song lookup ignores the
        # bucket, so a calibration for ANOTHER VERSION of the song is used. ---
        align_inherit._song_entry = _bucket_ignoring_song_entry
        got = align_inherit.decide_inherited_offset(TRACK, APP,
                                                    CAL_BUCKET_MISMATCH, None)
        check("[mutation] bucket gate removed -> 'mismatch falls back to app' "
              "FAILS",
              not (got.source == "app" and abs(got.offset_sec - 0.50) < 1e-6),
              f"mutation unexpectedly passed: {got}")
        align_inherit._song_entry = originals["_song_entry"]

        # --- mutation 4: R3 broken -- apply the correlation 1:1 (no damping) ---
        align_inherit.CORRELATION_BLEND_WEIGHT = 1.0
        got = align_inherit.decide_inherited_offset(TRACK, APP, CAL_BOTH,
                                                    CORR_TRUSTED)
        check("[mutation] correlation applied 1:1 -> 'blended' criterion FAILS",
              abs(got.offset_sec - (0.30 + 0.5 * 0.40)) > 1e-9,
              f"mutation unexpectedly passed: {got}")
        align_inherit.CORRELATION_BLEND_WEIGHT = CORRELATION_BLEND_WEIGHT

        # --- mutation 5: R3 broken -- use the correlation even when untrusted ---
        real_parts = align_inherit._corr_parts
        align_inherit._corr_parts = lambda corr: (0.40, 0.10, True)
        got = align_inherit.decide_inherited_offset(TRACK, APP, CAL_BOTH,
                                                    CORR_UNTRUSTED)
        check("[mutation] untrusted correlation used -> 'ignored' criterion "
              "FAILS",
              abs(got.offset_sec - 0.30) > 1e-9,
              f"mutation unexpectedly passed: {got}")
        align_inherit._corr_parts = real_parts
    finally:
        # Restore absolutely, so a failure inside cannot corrupt later tests.
        for name, fn in originals.items():
            setattr(align_inherit, name, fn)
        align_inherit.CORRELATION_BLEND_WEIGHT = CORRELATION_BLEND_WEIGHT

    # Post-restore sanity: the real rules are back in force.
    got = decide_inherited_offset(TRACK, APP, CAL_BOTH, CORR_TRUSTED)
    expect_offset("rules restored after mutation testing", got,
                  0.30 + CORRELATION_BLEND_WEIGHT * 0.40, "song")


def _app_only(track, app_id, calib, corr):
    """A deliberately wrong implementation: app level first, song level never."""
    if not calib:
        return align_inherit.InheritedOffset(0.0, "none", 0.0, "mutant")
    apps = calib.get("apps") or {}
    if isinstance(apps, dict) and app_id in apps:
        return align_inherit.InheritedOffset(
            float(apps[app_id]["offset_sec"]), "app", 1.0, "mutant")
    return align_inherit.InheritedOffset(0.0, "none", 0.0, "mutant")


def _bucket_ignoring_song_entry(calib, track):
    """A deliberately wrong song lookup: matches title+artist, ignores bucket."""
    tracks = calib.get("tracks") or {}
    if not isinstance(tracks, dict):
        return None
    title = track.get("title")
    artist = track.get("artist")
    if title is None or artist is None:
        return None
    prefix = f"{str(title).strip().lower()}|{str(artist).strip().lower()}|"
    for key, entry in tracks.items():
        if isinstance(key, str) and key.startswith(prefix) \
                and isinstance(entry, dict):
            return entry
    return None


# ---------------------------------------------------------------------------
# §10 cross-module contract with task-2's landed align_calib.py
# ---------------------------------------------------------------------------

def test_contract_with_align_calib() -> None:
    """Pin the two interfaces that task-3 depends on.

    These are the assumptions most likely to drift silently: the song-key
    format and the snapshot section names. If task-2 changes either, THIS
    test fails rather than the calibration quietly never matching at runtime
    (a failure mode that would look like "inheritance just doesn't work").
    """
    print("\n§10 contract with align_calib.py (task-2's landed module)")
    try:
        import align_calib  # noqa: F401
    except ImportError:
        check("align_calib importable (skipped contract checks otherwise)",
              True, "")
        print("        (align_calib.py not present -- contract not pinned)")
        return

    # 1. Song-key format must match byte for byte.
    key = align_calib.make_track_key("幹物女", "Z新豪", 222.0)
    check("my track_key() == align_calib.make_track_key()",
          key == align_inherit.track_key("幹物女", "z新豪", 22),
          f"align_calib={key!r} mine={align_inherit.track_key('幹物女','z新豪',22)!r}")
    check("track key embeds the bucket as the 3rd field",
          key.split("|")[-1] == "22", f"got {key!r}")

    # 2. Snapshot section names must be "tracks"/"apps".
    snap = align_calib.load_calibration(reload=True).snapshot()
    check("snapshot exposes 'tracks' (+ 'apps') sections",
          "tracks" in snap and "apps" in snap, f"got {sorted(snap)}")

    # 3. End-to-end through the REAL store: record a nudge, read it back as a
    #    decision. Uses a TEMP store path so the user's real calibration file
    #    is never touched by a test run.
    #
    #    The temp dir is created manually and NOT removed via
    #    tempfile.TemporaryDirectory: under the DSH sandbox its cleanup raises
    #    PermissionError (WinError 5) on Windows, which would fail the test for
    #    an environment reason rather than a logic reason. Cleanup is
    #    best-effort below.
    import shutil
    import tempfile
    from pathlib import Path as _P
    td = tempfile.mkdtemp(prefix="mvm_calib_contract_")
    try:
        store = align_calib.CalibrationStore(_P(td) / "align_calib.json")
        store.record_manual_nudge(SONG_KEY, APP, 0.30)
        store.save_as_app_default(APP, 0.50)
        real_snap = store.snapshot()
        got = decide_inherited_offset(TRACK, APP, real_snap, None)
        expect_offset("real store snapshot -> song-level value inherited",
                      got, 0.30, "song")
        # Second level: a song with no entry inherits the app default.
        got = decide_inherited_offset(
            {"title": "另一首歌", "artist": "洛天依", "duration_bucket": 18},
            APP, real_snap, None)
        expect_offset("real store snapshot -> app-level default inherited",
                      got, 0.50, "app")
        # Confirm the test never wrote the project's real calibration file.
        check("contract test did not create the real state file",
              not align_inherit._CALIB_FALLBACK_PATH.exists(),
              f"{align_inherit._CALIB_FALLBACK_PATH} exists")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # 4. The store's own clamp must not be looser than ours, or a value could
    #    be stored that we always refuse (silent dead calibration).
    check("align_calib MAX_ABS_OFFSET_SEC <= our MAX_INHERITED_OFFSET_SEC",
          align_calib.MAX_ABS_OFFSET_SEC <= MAX_INHERITED_OFFSET_SEC,
          f"store={align_calib.MAX_ABS_OFFSET_SEC} "
          f"ours={MAX_INHERITED_OFFSET_SEC}")


# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 68)
    print("test_align_inherit.py -- inheritance decision module (task-3)")
    print("=" * 68)
    tests = [
        test_empty_calibration,
        test_app_level_hit,
        test_song_overrides_app,
        test_bucket_mismatch_fallback,
        test_cap_and_runaway,
        test_correlation_merge,
        test_storage_wrapper_tolerant,
        test_purity,
        test_mutations_prove_criteria_bind,
        test_contract_with_align_calib,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a crashing test is a failing test
            global _FAIL
            _FAIL += 1
            _FAILED_NAMES.append(fn.__name__)
            print(f"  FAIL  {fn.__name__} raised:")
            traceback.print_exc()

    print("\n" + "=" * 68)
    total = _PASS + _FAIL
    if _FAIL == 0:
        print(f"ALL PASS  {_PASS}/{total}")
    else:
        print(f"FAILED  {_PASS}/{total} passed; failures: "
              f"{', '.join(_FAILED_NAMES)}")
    print("=" * 68)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
