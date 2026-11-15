param([ValidateRange(1024, 65535)][int]$Port = 18765, [string]$StudioRoot = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$serviceInfo = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
if ($serviceInfo.service_id -ne 'xichugeek-studio-control-center-v1' -or
    -not [string]::Equals([string]$serviceInfo.root, [IO.Path]::GetFullPath($StudioRoot), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The listener is not this workspace Control Center. No process was stopped.'
}
$targetProcessId = [int]$serviceInfo.pid
$targetProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $targetProcessId"
$expectedScript = Join-Path $PSScriptRoot 'dashboard.py'
$matchesScript = $targetProcess -and ($targetProcess.CommandLine -match [regex]::Escape($expectedScript) -or
    $targetProcess.CommandLine -match '(?:^|\s|")dashboard\.py(?:\s|$|")')
if (-not $targetProcess -or $targetProcess.Name -notmatch '^python(w)?\.exe$' -or
    -not $matchesScript) {
    throw 'The process identity does not match this dashboard. No process was stopped.'
}
Stop-Process -Id $targetProcessId
Write-Host "XichuGeek Studio Control Center stopped (port $Port)."
