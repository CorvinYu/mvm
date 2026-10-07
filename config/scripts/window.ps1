# window.ps1 -- measure and set the MVM video window's screen rectangle.
#
# Why this exists:
#   mpv re-centres (and can resize) its window when a video finishes loading,
#   so a window that starts at the intended position jumps elsewhere a few
#   seconds later. Measured: 953,500 -> 1191,439 about 9s after loadfile, and in
#   another run it cascaded to 3181,1377 then grew to 1414x1102.
#
#   mpv's own --geometry only applies at startup and does not survive that
#   re-centring, and the Lua script cannot call Win32. So we set the rectangle
#   ourselves with SetWindowPos, which is authoritative.
#
# Usage:
#   window.ps1 get                 -> "PID|X|Y|W|H" for the MVM window, or "none"
#   window.ps1 set X Y W H         -> moves/resizes the MVM window
#
# This is ASCII-only on purpose (cmd/PowerShell 5.1 encoding issues).

param(
    [Parameter(Position=0)][string]$Action = "get",
    [Parameter(Position=1)][int]$X = 0,
    [Parameter(Position=2)][int]$Y = 0,
    [Parameter(Position=3)][int]$W = 0,
    [Parameter(Position=4)][int]$H = 0
)

$ErrorActionPreference = "Stop"

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinRect {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after,
        int X, int Y, int cx, int cy, uint flags);

    [DllImport("user32.dll")]
    public static extern bool MoveWindow(IntPtr hWnd, int X, int Y,
        int nWidth, int nHeight, bool repaint);
}
"@

# Find our own video window. Title is unique to this tool, and we additionally
# require the executable to be our isolated mpv copy so the user's own mpv is
# never touched.
$proc = Get-Process mpv -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -eq "MVM-Video" } |
    Select-Object -First 1

if (-not $proc) {
    Write-Output "none"
    exit 0
}

$hwnd = $proc.MainWindowHandle

if ($Action -eq "get") {
    $r = New-Object WinRect+RECT
    [void][WinRect]::GetWindowRect($hwnd, [ref]$r)
    Write-Output ("{0}|{1}|{2}|{3}|{4}" -f $proc.Id, $r.Left, $r.Top, ($r.Right - $r.Left), ($r.Bottom - $r.Top))
    exit 0
}

if ($Action -eq "set") {
    # SWP_NOZORDER(0x4) | SWP_NOACTIVATE(0x10)
    $flags = 0x0004 -bor 0x0010
    [void][WinRect]::SetWindowPos($hwnd, [IntPtr]::Zero, $X, $Y, $W, $H, $flags)
    $r = New-Object WinRect+RECT
    [void][WinRect]::GetWindowRect($hwnd, [ref]$r)
    Write-Output ("{0}|{1}|{2}|{3}|{4}" -f $proc.Id, $r.Left, $r.Top, ($r.Right - $r.Left), ($r.Bottom - $r.Top))
    exit 0
}

Write-Output "unknown action: $Action"
exit 1
