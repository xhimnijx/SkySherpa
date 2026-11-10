#Requires -Version 5.1
<#
.SYNOPSIS
    Install SkySherpa on Windows as a scheduled task that runs everything at logon.

.DESCRIPTION
    Windows has no systemd, so the three long-lived processes are supervised by
    `skysherpa up` and kept alive by Task Scheduler: start at logon, restart for a day if
    the supervisor itself dies. Everything runs as the current user; no admin rights and no
    Windows service.

.EXAMPLE
    .\ops\install-windows.ps1 -DryRun
    .\ops\install-windows.ps1

.EXAMPLE
    .\ops\install-windows.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Uninstall,
    [int]$Port = 8099,
    [int]$PublishInterval = 900,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $repo ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$taskName = "SkySherpa"

function Say([string]$message) { Write-Host $message }

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Say "scheduled task '$taskName' removed"
    Say "left in place: $repo\data (history), $repo\dist-real (bundle), $repo\logs, $venv"
    exit 0
}

# The repository path must not contain a space: it goes into a scheduled-task argument.
if ($repo -match ' ') {
    Say "error: the repository path contains a space: $repo"
    Say "       move it somewhere else, for example C:\skysherpa"
    exit 1
}

$exe = Get-Command $Python -ErrorAction SilentlyContinue
if (-not $exe) {
    Say "error: '$Python' not found on PATH"
    Say "       install Python 3.11+ (https://www.python.org/downloads/windows/)"
    exit 1
}
$version = & $Python -c "import sys; print(sys.version_info[0] * 100 + sys.version_info[1])"
if ([int]$version -lt 311) {
    Say "error: Python 3.11+ required, found $(& $Python -V 2>&1)"
    exit 1
}

if ($DryRun) {
    Say ""
    Say "dry run: nothing created, nothing registered"
    Say "  python    $(& $Python -V 2>&1)"
    Say "  repo      $repo"
    Say "  venv      $venv"
    Say "  task      $taskName  (at logon, restart every minute for a day)"
    Say "  command   $venvPython -m skysherpa up --port $Port --publish-interval $PublishInterval"
    Say "  logs      $repo\logs"
    exit 0
}

if (-not (Test-Path $venvPython)) {
    Say "creating $venv"
    & $Python -m venv $venv
}
& $venvPython -m pip install --quiet --upgrade pip
Say "installing skysherpa into the venv"
& $venvPython -m pip install --quiet -e $repo
& $venvPython -m skysherpa version | Out-Null
if ($LASTEXITCODE -ne 0) {
    Say "error: the installed package does not run; check the pip output above"
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute $venvPython `
    -Argument "-m skysherpa up --port $Port --publish-interval $PublishInterval" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "SkySherpa: collector, publisher and site" `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Say ""
Say "installed. The supervisor starts at logon and runs until you stop it."
Say ""
Say "  site      http://127.0.0.1:$Port/  (appears once the first publish completes)"
Say "  status    $venvPython -m skysherpa status"
Say "  logs      $repo\logs\{collector,publisher,site}.log"
Say "  task      Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo"
Say "  stop      Stop-ScheduledTask -TaskName $taskName"
Say "  remove    .\ops\install-windows.ps1 -Uninstall"
exit 0
