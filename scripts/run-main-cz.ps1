param([int]$MaxStreams=40,[int]$BudgetSeconds=1500,[string]$FfmpegBin='')
$ErrorActionPreference='Stop'
if ($FfmpegBin) { $env:PATH = $FfmpegBin + ';' + $env:PATH }
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    python scripts/update_playlist.py
    if ($LASTEXITCODE -ne 0) { throw 'Source refresh failed. Previous main playlist retained.' }
    python scripts/check_main_movies.py --confirm-direct-cz --max-streams $MaxStreams --budget-seconds $BudgetSeconds
    if ($LASTEXITCODE -ne 0) { throw 'CZ batch not accepted. Previous main playlist retained.' }
    python scripts/main_catalogue.py
    if ($LASTEXITCODE -ne 0) { throw 'Publication validation failed. Previous main playlist retained.' }
} finally { Pop-Location }
