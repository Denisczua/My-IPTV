param(
    [int]$MaxChannels = 120,
    [int]$BudgetSeconds = 1500,
    [int]$Workers = 2
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    python --version
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12+ and add it to PATH.' }
    ffmpeg -version
    if ($LASTEXITCODE -ne 0) { throw 'Install FFmpeg and add its bin directory to PATH.' }
    ffprobe -version
    if ($LASTEXITCODE -ne 0) { throw 'Install FFprobe and add its bin directory to PATH.' }
    python scripts/check_ru_ua.py --confirm-direct-cz --workers $Workers --max-channels $MaxChannels --budget-seconds $BudgetSeconds
    if ($LASTEXITCODE -ne 0) { throw 'Check failed. Previous playlists have been retained.' }
} finally {
    Pop-Location
}
