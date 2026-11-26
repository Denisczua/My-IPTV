param([int]$MaxStreams=40,[int]$BudgetSeconds=1500,[string]$FfmpegBin='')
$ErrorActionPreference='Stop'
if ($FfmpegBin) { $env:PATH = $FfmpegBin + ';' + $env:PATH }
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    python scripts/update_playlist.py
    if ($LASTEXITCODE -ne 0) { throw 'Source refresh failed. Previous main playlist retained.' }
    python scripts/audit_personal_cz.py --max-streams $MaxStreams --budget-seconds $BudgetSeconds
    if ($LASTEXITCODE -ne 0) { throw 'CZ batch not accepted. Previous main playlist retained.' }
    python scripts/build_playlist_site.py
    if ($LASTEXITCODE -ne 0) { throw 'Personal publication validation failed. Previous published playlist retained.' }
} finally { Pop-Location }
