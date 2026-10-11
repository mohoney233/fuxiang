# fuxiang_bridge_install.ps1 (ASCII-only to avoid PS encoding issues)
# Install the FuXiang editor bridge WITHOUT the scheduled task (retired 2026-10-03).
#
# Why the task was retired: schtasks defaults gave it DisallowStartIfOnBatteries=True,
# so on battery power a trigger silently queued forever ("never worked"), and
# MultipleInstances=IgnoreNew then wedged it permanently. Elevation itself cannot be
# avoided: builds/_medium_test.log shows OpenProcess fails with error 5 at Medium
# integrity. So this script does the elevation VISIBLY instead:
#   1) try direct install (works as-is from an elevated terminal, no prompt at all)
#   2) only if OpenProcess is denied (injector exit code 5) -> ask UAC once and rerun
#   3) verdict = bridge_state.json pid equals live game pid AND heartbeat is fresh
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\fuxiang\fuxiang_bridge_install.ps1
#   # from the console (WinUI) it also passes -ToolRoot, see below

# Tool root: -ToolRoot wins (the WinUI console spawns this with -File and there
# $PSScriptRoot came back EMPTY on this box, which made every Join-Path fail);
# then $PSScriptRoot (script sits in the tool root); then this file's own path.
param([string]$ToolRoot = '')

$root = if ($ToolRoot) { $ToolRoot }
        elseif ($PSScriptRoot) { $PSScriptRoot }
        else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $root) { Write-Host "[FATAL] cannot determine tool root (pass -ToolRoot)"; exit 1 }
$inj       = Join-Path $root 'injector\bin\Debug\net8.0-windows\fuxiang-inject.exe'
$elevLog   = Join-Path $root 'ipc\_install_elevated.log'
$stateFile = Join-Path $root 'ipc\bridge_state.json'

# Read the override BEFORE installing: the injector deletes it on success.
# It tells us which game pid and which channel ipc this run targets (multi-bridge).
$overrideFile = Join-Path $root 'ipc\install_override.json'
$ov = $null
if (Test-Path $overrideFile) {
    try { $ov = Get-Content $overrideFile -Raw | ConvertFrom-Json } catch { $ov = $null }
}
$targetState = $stateFile
if ($ov -and $ov.ipc) { $targetState = Join-Path $ov.ipc 'bridge_state.json' }
$wantPid = $null
if ($ov -and $ov.pid) { $wantPid = [string]$ov.pid }

# 1) direct attempt
Write-Host "=== direct install ==="
$out = & $inj install 2>&1
$code = $LASTEXITCODE
$out | Out-Host

# 2) elevation fallback, only for OpenProcess denial (exit 5)
if ($code -eq 5) {
    Write-Host "[ELEVATE] OpenProcess denied (the game must be accessed elevated). Asking UAC once..."
    try {
        Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
            "& '$inj' install *>&1 | Out-File -FilePath '$elevLog' -Encoding utf8")
    } catch {
        Write-Host "[ELEVATE] UAC declined or failed:"
        Write-Host $_.Exception.Message
        exit 1
    }
    Write-Host "=== elevated install output ==="
    Get-Content $elevLog -Encoding UTF8 -ErrorAction SilentlyContinue | Out-Host
} elseif ($code -eq 9) {
    Write-Host "[GATE] export-table gate refused injection (engine updated?)."
    Write-Host "       Regenerate research/tools_extract/neox_exports.txt first (see INJECTOR_USAGE.md section 4)."
    exit 1
}

# 3) verdict from the state file: pid must equal the live game pid, heartbeat fresh
#    With an override (multi-bridge) the verdict must use THAT channel's state file and pid,
#    otherwise the main channel's fresh heartbeat makes a failed injection look OK.
$g = Get-Process -Name dwrg -ErrorAction SilentlyContinue | Select-Object -First 1
$gamePid = if ($g) { [string]$g.Id } else { "none" }
if ($wantPid) { $gamePid = $wantPid }
try {
    $s = Get-Content $targetState -Raw | ConvertFrom-Json
    $age = [Math]::Round([DateTimeOffset]::Now.ToUnixTimeSeconds() - [double]$s.now, 1)
    Write-Host "[STATE] target pid=$gamePid | bridge pid=$($s.pid) | heartbeat age=${age}s | state=$targetState"
    if ($gamePid -eq [string]$s.pid -and $age -le 5) {
        Write-Host "bridge ready OK"
        exit 0
    }
    Write-Host "bridge NOT ready -- check ipc/bridge.log"
    exit 1
} catch {
    Write-Host "[STATE] bridge_state.json not readable (target pid=$gamePid, state=$targetState). Did that game window reach the editor?"
    exit 1
}
