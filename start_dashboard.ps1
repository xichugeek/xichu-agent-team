param(
    [ValidateRange(1024, 65535)][int]$Port = 18765,
    [switch]$NoBrowser,
    [string]$StudioRoot = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$studioMetadataRoot = [IO.Path]::GetFullPath($StudioRoot)
$studioPython = Get-Command python -ErrorAction SilentlyContinue
if (-not $studioPython) {
    throw 'Python was not found. Install/use the existing Studio Python 3.12+ environment.'
}
$existingService = $null
try { $existingService = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 } catch { }
if ($existingService -and $existingService.service_id -eq 'xichugeek-studio-control-center-v1' -and
    [string]::Equals([string]$existingService.root, $studioMetadataRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "Control Center is already running at http://127.0.0.1:$Port"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
    exit 0
}
$studioArgs = @('-B', '-X', 'utf8', (Join-Path $PSScriptRoot 'dashboard.py'), '--port', [string]$Port, '--studio-root', $studioMetadataRoot)
if (-not $NoBrowser) { $studioArgs += '--open' }
& $studioPython.Source @studioArgs
exit $LASTEXITCODE
