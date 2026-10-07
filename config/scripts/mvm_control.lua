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

-- mp.log requires a level string first, then the message. Calling it with a
-- single argument raises "Invalid log level ..." and kills the script.
local function log(msg)
    mp.log("info", "mvm: " .. msg)
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

-- Publish playback state so the Python side can read it back. We have no
-- request/response channel (no IPC), so state is pushed to a file instead.
--
-- Fields, one per line:
--   1. time-pos
--   2. pause
--   3. path
--   4. geometry  (WxH+X+Y)
--
-- IMPORTANT about the geometry: `width`/`height` are the VIDEO's dimensions
-- (e.g. 1920x1078), NOT the window's. Reporting those and feeding them back
-- via --geometry produced a window roughly twice the intended size -- the
-- "big window" the user reported. The real window rectangle must come from
-- Win32 GetWindowRect, which the Python side does.
local status_file = os.getenv("MVM_STATUS_FILE")
if status_file then
    mp.add_periodic_timer(0.5, function()
        local pos = mp.get_property_number("time-pos")
        local paused = mp.get_property("pause")
        local path = mp.get_property("path")

        -- Only report whether a window exists; the rectangle is measured
        -- externally. Keep the field for compatibility.
        local has_window = mp.get_property_native("window-minimized")
        local geom = ""
        if has_window ~= nil then
            geom = "has-window"
        end

        local f = io.open(status_file, "w")
        if f then
            f:write(string.format("%s\n%s\n%s\n%s\n",
                pos and string.format("%.3f", pos) or "",
                paused or "",
                path or "",
                geom))
            f:close()
        end
    end)
end

log("control loaded, watching " .. cmd_file)
