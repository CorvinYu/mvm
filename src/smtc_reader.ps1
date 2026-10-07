# SMTC reader (PowerShell) - outputs JSON for Python caller.
#
# Mechanism: Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager
#            (the same API Wallpaper Engine uses to read "now playing" info)
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File smtc_reader.ps1 [-FilterApp name]
# Output: JSON array of playback sessions.
#
# NOTE: This file is intentionally pure ASCII. Windows PowerShell 5.1 mis-parses
#       UTF-8-without-BOM files containing non-ASCII characters, which breaks the script.

param(
    [string]$FilterApp = ""
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

    function Await($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        if (-not $netTask.Wait(5000)) { throw "WinRT call timeout" }
        $netTask.Result
    }

    $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
    $propsType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]

    $mgr = Await ($mgrType::RequestAsync()) $mgrType
    $out = @()

    foreach ($s in $mgr.GetSessions()) {
        try {
            $appId = $s.SourceAppUserModelId
            if ($FilterApp -and ($appId -notlike "*$FilterApp*")) { continue }

            $p  = Await ($s.TryGetMediaPropertiesAsync()) $propsType
            $pi = $s.GetPlaybackInfo()
            $tl = $s.GetTimelineProperties()

            $out += [ordered]@{
                app_id       = $appId
                title        = [string]$p.Title
                artist       = [string]$p.Artist
                album        = [string]$p.AlbumTitle
                status       = [string]$pi.PlaybackStatus
                position_sec = [math]::Round($tl.Position.TotalSeconds, 2)
                duration_sec = [math]::Round($tl.EndTime.TotalSeconds, 2)
            }
        } catch {
            continue
        }
    }

    ,@($out) | ConvertTo-Json -Depth 4 -Compress
}
catch {
    '[]'
}
