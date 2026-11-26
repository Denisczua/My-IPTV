param(
    [Parameter(Mandatory=$true)][string]$FfmpegBin,
    [string]$RunnerRoot = (Join-Path $env:LOCALAPPDATA 'MyIPTV-CZ-runner')
)
# Run only on the dedicated direct-CZ Windows computer. Credentials stay outside OneDrive/Git.
$ErrorActionPreference = 'Stop'
$repo = 'Denisczua/My-IPTV'
$runnerName = 'iptv-cz-' + $env:USERNAME
$taskName = 'My-IPTV Czech runner'
$oldTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($oldTask -and -not (($oldTask.Actions.Arguments -join ' ').Contains($RunnerRoot))) {
    throw 'A differently configured task with this name already exists; it has not been changed.'
}
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$gitPath = (Get-Command git -ErrorAction Stop).Source
$null = Get-Command gh -ErrorAction Stop
foreach ($exe in @('ffmpeg.exe','ffprobe.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $FfmpegBin $exe))) { throw "Missing $exe in FfmpegBin" }
}
New-Item -ItemType Directory -Force -Path $RunnerRoot | Out-Null
$tools = Join-Path $RunnerRoot 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
foreach ($exe in @('ffmpeg.exe','ffprobe.exe')) {
    Copy-Item -LiteralPath (Join-Path $FfmpegBin $exe) -Destination (Join-Path $tools $exe) -Force
}
if (-not (Test-Path -LiteralPath (Join-Path $RunnerRoot '.runner'))) {
    $releaseText = gh api repos/actions/runner/releases/latest
    if ($LASTEXITCODE -ne 0) { throw 'Cannot obtain official GitHub runner release' }
    $asset = ($releaseText | ConvertFrom-Json).assets | Where-Object { $_.name -like 'actions-runner-win-x64-*.zip' } | Select-Object -First 1
    if (-not $asset) { throw 'Official Windows x64 runner asset not found' }
    $archive = Join-Path $RunnerRoot 'runner.zip'
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive
    if ($asset.digest -and $asset.digest.StartsWith('sha256:')) {
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.digest.Substring(7)) { throw 'Runner checksum mismatch' }
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $RunnerRoot -Force
    $registrationToken = gh api --method POST "repos/$repo/actions/runners/registration-token" --jq .token
    if ($LASTEXITCODE -ne 0 -or -not $registrationToken) { throw 'Runner registration requires repository administration permission' }
    Push-Location $RunnerRoot
    try {
        # Do not print the token or write it to repository files.
        & .\config.cmd --unattended --url "https://github.com/$repo" --token $registrationToken --name $runnerName --labels iptv-cz --work _work
        if ($LASTEXITCODE -ne 0) { throw 'GitHub runner configuration failed' }
    } finally {
        $registrationToken = $null
        Pop-Location
    }
}
$pythonFolder = Split-Path -Parent $pythonPath
$gitFolder = Split-Path -Parent $gitPath
$launch = Join-Path $RunnerRoot 'launch.ps1'
$launchText = @"
`$env:PATH = '$tools;$pythonFolder;$gitFolder;' + `$env:PATH
Set-Location -LiteralPath '$RunnerRoot'
& '.\run.cmd'
exit `$LASTEXITCODE
"@
Set-Content -LiteralPath $launch -Value $launchText -Encoding UTF8
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launch`"" -WorkingDirectory $RunnerRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "Registered $runnerName with label iptv-cz; startup task: $taskName."
Write-Output 'Keep Windows signed in, awake and connected directly in CZ. Enable CZ_CHECKS_ENABLED only after GitHub reports this runner online.'
