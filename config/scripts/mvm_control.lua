-- mvm_control.lua -- lets an external process drive mpv without IPC.
--
-- Why this exists:
--   On this machine mpv cannot be controlled the usual ways:
--     * --input-ipc-server is a Windows named pipe, and opening named pipes is
--       denied here (PermissionError errno 13).
--     * --input-terminal=yes does NOT read commands from a redirected stdin.
--       Verified from mpv's own verbose log: commands written to the child's
--       stdin never appeared as "Run command" entries, i.e. they were silently
--       discarded. (An earlier version of this project "verified" graceful
--       song switching by checking the PID stayed constant -- that was a false
--       positive: nothing was happening at all.)
--   So we poll a plain command file instead. It needs no pipes and no sockets.

local cmd_file = os.getenv("MVM_CMD_FILE") or "mvm_cmd.txt"
-- Who owns this mpv (issue #6): the Python follower. If it dies for ANY reason
-- (crash, hard kill, sandbox teardown), the Lua timer below notices and quits,
-- so an orphan window cannot outlive its daemon. Only active when the variable
-- is present and parses as a number; standalone/test mpv runs skip the check.
local parent_pid = tonumber(os.getenv("MVM_PARENT_PID") or "")

-- mp.log requires a level string first, then the message. Calling it with a
-- single argument raises "Invalid log level ..." and kills the script.
local function log(msg)
    mp.log("info", "mvm: " .. msg)
end

local function parent_is_alive()
    -- Liveness check that survives a killed-but-not-yet-reaped process.
    --
    -- WHY NOT OpenProcess alone (measured 2026-10-08): OpenProcess keeps
    -- SUCCEEDING after TerminateProcess while any handle to the process object
    -- remains open -- e.g. the parent Python still holds the Popen object. The
    -- pid is not reused and the process object lingers, so "can I open it?"
    -- answers "yes" for a process that is already dead. That made the orphan
    -- guard silently useless in the exact case it exists for (an e2e test that
    -- hard-killed the daemon left the mpv running for 20s+).
    --
    -- GetExitCodeProcess is the correct primitive: a terminated process
    -- reports its exit code instead of STILL_ACTIVE (259).
    local ok, ffi = pcall(require, "ffi")
    if not ok then return true end  -- no ffi: assume alive (never self-kill)
    ffi.cdef[[
        typedef void* HANDLE;
        HANDLE __stdcall OpenProcess(unsigned long, int, unsigned long);
        int __stdcall CloseHandle(HANDLE);
        int __stdcall GetExitCodeProcess(HANDLE, unsigned long*);
    ]]
    local PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    local STILL_ACTIVE = 259
    local h = ffi.C.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, parent_pid)
    if h == nil then
        return false           -- no such process at all
    end
    local code = ffi.new("unsigned long[1]", 0)
    local okq = ffi.C.GetExitCodeProcess(h, code)
    ffi.C.CloseHandle(h)
    if okq == 0 then
        -- Could not query (rights). Be conservative: treat as alive rather
        -- than killing a perfectly good window.
        return true
    end
    return code[0] == STILL_ACTIVE
end

local function read_and_clear(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local content = f:read("*a")
    f:close()
    -- Truncate so each command runs exactly once.
    local w = io.open(path, "w")
    if w then
        w:write("")
        w:close()
    end
    content = content:gsub("%s+$", "")
    if content == "" then return nil end
    return content
end

mp.add_periodic_timer(0.2, function()
    local blob = read_and_clear(cmd_file)
    if not blob then return end
    for single in blob:gmatch("[^\r\n]+") do
        local ok, err = pcall(mp.command, single)
        if ok then
            log("ran " .. single)
        else
            log("FAILED " .. single .. " -- " .. tostring(err))
        end
    end
end)

-- Orphan-window guard (issue #6): when our owner process is gone, quit.
-- Every 1s is plenty: the failure mode is "daemon died", not "daemon dying",
-- and a sub-second delay in noticing costs nothing.
if parent_pid then
    mp.add_periodic_timer(1.0, function()
        if not parent_is_alive() then
            log("parent pid " .. parent_pid .. " gone -- quitting")
            mp.commandv("quit")
        end
    end)
    log("orphan guard armed: parent pid " .. parent_pid)
end

-- Publish playback state so the Python side can read it back. We have no
-- request/response channel (no IPC), so state is pushed to a file instead.
--
-- Fields, one per line:
--   1. time-pos
--   2. pause
--   3. path
--   4. geometry  (WxH+X+Y)
--   5. manual_offset  (task-2: cumulative user-preference offset, seconds)
--   6. fullscreen      (yes/no)   -- issue #2: user asked for fullscreen
--   7. window-maximized (yes/no)  -- issue #2: user maximized
--
-- WHY 6 and 7 EXIST (issue #2, measured 2026-10-08)
--   The geometry guard could not tell "the user pressed F / maximized" from
--   "mpv enlarged the window by itself": both look like one big rectangle. So
--   it treated a deliberate fullscreen as pollution and yanked the window back
--   -- the user's "无法全屏" report. mpv KNOWS which it is, and these two
--   properties are that answer. Appended (not inserted) so existing
--   index-based readers of lines 1-5 keep working with an older file.
--
-- IMPORTANT about the geometry: `width`/`height` are the VIDEO's dimensions
-- (e.g. 1920x1078), NOT the window's. Reporting those and feeding them back
-- via --geometry produced a window roughly twice the intended size -- the
-- "big window" the user reported. The real window rectangle must come from
-- Win32 GetWindowRect, which the Python side does.
local status_file = os.getenv("MVM_STATUS_FILE")

-- ------------------------------------------------------------------------
-- Manual alignment hotkeys (task-2).
--
-- Semantics: the closed loop (follow.py, task-1) corrects ALIGNMENT ERROR;
-- these hotkeys express USER PREFERENCE, and the two are ADDED (see
-- align_calib.py). The hotkeys NEVER write to the command file and never
-- touch alignment targets -- they only move the playhead by a RELATIVE amount,
-- so the follower sees "the user moved the picture" and must not treat it as
-- an error to cancel.
--
-- Sign: positive delta = video moved LATER (picture lags more), matching
-- follow.log's "+offset" language and mpv's `seek +N relative`.
--
-- Key layout (mirrored in tools/align_control.py and the task-2 report):
--     [  /  ]   nudge -0.1s / +0.1s
--     {  /  }   nudge -1.0s / +1.0s
--     0  or  \  reset the current song's manual offset to 0
--   `[`/`]`/`0`/`\` carry no default binding under --no-config, so there is
--   nothing to conflict with. mp.add_forced_key_binding is used (not
--   add_key_binding) so a stray user input.conf -- which must not exist here,
--   but might in a hand-made test copy -- cannot shadow the binding.
-- ------------------------------------------------------------------------
local manual_offset = 0.0       -- cumulative preference offset, THIS mpv run
local manual_track  = ""        -- path of the media it applies to
local manual_file   = os.getenv("MVM_MANUAL_FILE")
local manual_tmp    = os.getenv("MVM_MANUAL_TMP")
local manual_dirty  = false     -- force a status write right after a nudge

-- Sidecar file holding just the cumulative offset, so the FOLLOW DAEMON can
-- persist it through align_calib.py without Lua needing to know what a
-- "track key" is (title/artist/duration are Python's business).
--
-- Written via rename() to be atomic: mpv and the daemon are different
-- processes and a torn read would make the daemon record a half value.
local function publish_manual_sidecar()
    if not (manual_file and manual_tmp) then return end
    local w = io.open(manual_tmp, "w")
    if not w then return end
    w:write(string.format("%.3f\n", manual_offset))
    w:close()
    local ok = os.rename(manual_tmp, manual_file)
    if not ok then
        -- On Windows the rename can lose a race with a reader holding the
        -- target open. Retry once: the value is the user's keypress, so it is
        -- worth one retry rather than dropping it silently.
        ok = os.rename(manual_tmp, manual_file)
        if not ok then
            log("manual sidecar rename failed (value kept in status file)")
        end
    end
end

local function write_status()
    if not status_file then return end
    local pos = mp.get_property_number("time-pos")
    local paused = mp.get_property("pause")
    local path = mp.get_property("path")

    -- Only report whether a window exists; the rectangle is measured
    -- externally. Keep the field for compatibility.
    local geom = ""
    if mp.get_property_native("window-minimized") ~= nil then
        geom = "has-window"
    end

    local f = io.open(status_file, "w")
    if f then
        -- Lines 6-7 let the geometry guard honour a DELIBERATE fullscreen /
        -- maximize instead of undoing it (issue #2).
        local fullscreen = mp.get_property("fullscreen") or "no"
        local maximized = mp.get_property("window-maximized") or "no"
        f:write(string.format("%s\n%s\n%s\n%s\n%.3f\n%s\n%s\n",
            pos and string.format("%.3f", pos) or "",
            paused or "",
            path or "",
            geom,
            manual_offset,
            fullscreen,
            maximized))
        f:close()
    end
end

local function nudge(delta)
    local path = mp.get_property("path") or ""
    if manual_track ~= path then
        -- New media loaded (song changed): a preference offset belongs to ONE
        -- track and must not leak into the next song. Remembering values
        -- across songs is align_calib.py's job (driven by the follower); this
        -- Lua value is only the in-flight accumulation for the current file.
        manual_offset = 0.0
        manual_track = path
    end
    manual_offset = manual_offset + delta
    -- Relative seek, not absolute+exact: the video keeps playing between the
    -- keypress and the command landing, so only a relative move means
    -- "exactly delta from where the picture is now".
    mp.commandv("seek", string.format("%+.3f", delta), "relative+exact")
    -- Observable side effect in mpv's OWN log (铁律 12: prove the action
    -- happened -- process liveness proves nothing).
    log(string.format("manual nudge %+.3fs -> cumulative %+.3fs", delta,
                      manual_offset))
    manual_dirty = true
    publish_manual_sidecar()
    write_status()
end

local function reset_manual()
    if manual_track == "" then
        manual_track = mp.get_property("path") or ""
    end
    local was = manual_offset
    manual_offset = 0.0
    log(string.format("manual reset (was %+.3fs)", was))
    manual_dirty = true
    publish_manual_sidecar()
    write_status()
end

if status_file then
    -- 0.5s: exactly the status-publish cadence that existed before task-2, so
    -- Python's MpvController.get_position() sees no timing change.
    mp.add_periodic_timer(0.5, write_status)
end

-- Publish the manual offset whenever a NEW file loads (wiring fix, session 8).
--
-- WHY: the sidecar was only ever written on a nudge/reset, so after a song
-- change it still held the PREVIOUS song's cumulative value. The follower polls
-- this file to answer "has the user nudged, and by how much?", and a leftover
-- value is indistinguishable from a fresh keypress -- so the old song's offset
-- would be recorded against the new song, and the control window would show a
-- manual offset the user never set for this track.
--
-- Writing 0.0 on load makes the file mean "cumulative offset for the file that
-- is loaded RIGHT NOW", which is exactly what the follower needs in order to
-- compute a correct delta. `nudge()` also re-syncs `manual_track` on its first
-- press, so this handler and that guard agree.
mp.register_event("file-loaded", function()
    manual_track = mp.get_property("path") or ""
    manual_offset = 0.0
    publish_manual_sidecar()
    write_status()
end)

-- Manual-alignment hotkeys.
--
-- 铁律 2 (never touch the user's mpv.net) is satisfied by the fact that this
-- script is only ever loaded with an explicit `--script=<this file>` from
-- player.py's isolated copy; the user's mpv.net never loads it and therefore
-- never sees these bindings.
--
-- An earlier version additionally gated on `options/no-config`, which was a
-- real bug: with `--config-dir` also present mpv reports that option as
-- something other than "yes"/true, so the guard evaluated false and the
-- hotkeys were never bound at all. Symptom: the keys did nothing (measured:
-- playhead did not move, delta 0.0) while the log claimed the bindings were
-- deliberately off.
-- A feature that is off in its own test configuration is worse than no guard,
-- so the check is gone; the isolation guarantee comes from how mpv is invoked
-- (an explicit --script=<this file> that the user's mpv.net never passes).
--
-- mp.add_forced_key_binding (rather than add_key_binding) is deliberate: it
-- takes precedence over user/config bindings, so `[`/`]` cannot be silently
-- stolen by an input.conf. mpv's defaults DO bind these keys to playback speed
-- and volume (mpv manual, "Keyboard Control": [ ] speed -/+10%, { } speed
-- halve/double, 0 volume +). Overriding them is harmless here because the MVM
-- window is a muted, picture-only output: speed and volume are not user-facing
-- controls in follow mode. This is documented in the task-2 report.
mp.add_forced_key_binding("[", "mvm_nudge_m01", function() nudge(-0.1) end)
mp.add_forced_key_binding("]", "mvm_nudge_p01", function() nudge(0.1) end)
mp.add_forced_key_binding("{", "mvm_nudge_m1", function() nudge(-1.0) end)
mp.add_forced_key_binding("}", "mvm_nudge_p1", function() nudge(1.0) end)
mp.add_forced_key_binding("0", "mvm_nudge_reset0", reset_manual)
mp.add_forced_key_binding("\\", "mvm_nudge_reset", reset_manual)
log("manual hotkeys: [ ] = -0.1/+0.1s, { } = -1/+1s, 0 or \\ = reset")

-- Test seam (and integration bridge): the same nudge/reset functions can be
-- triggered over the command file, e.g. `script-message-to mvm_control mvm-nudge 0.1`.
-- Why: this sandbox refuses synthetic keyboard input (SendInput delivers 0
-- events even with the window foreground; measured), so an automated check of
-- the KEY path is impossible HERE -- but the command path reaches the exact
-- same function the key binding calls. tools/verify_hotkeys.ps1 uses this to
-- verify the nudge side effects (playhead moves, manual offset updates,
-- sidecar written); the key->binding dispatch itself is proven by mpv's own
-- define-section log (visible in the mpv logfile) and must be pressed by a
-- human during the final field test.
mp.register_script_message("mvm-nudge", function(delta_str)
    local d = tonumber(delta_str)
    if not d then
        log("mvm-nudge: bad delta " .. tostring(delta_str))
        return
    end
    nudge(d)
end)
mp.register_script_message("mvm-reset", function()
    reset_manual()
end)
log("mvm-nudge/mvm-reset script-message handlers registered")

log("control loaded, watching " .. cmd_file)
