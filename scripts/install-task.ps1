<#
.SYNOPSIS
  Installs the hourly Suliko health check as a Windows Scheduled Task. Run in an ELEVATED PowerShell.

.DESCRIPTION
  * creates .venv and installs requirements
  * locks .env down to SYSTEM + Administrators (it holds SMTP / SMS / test-account secrets)
  * registers task "Suliko-HealthCheck": runs main.py every hour as SYSTEM, whether or not
    anyone is logged in, no password stored, starts at boot if a run was missed.

  A scheduled task (not a long-running service) is deliberate: the process exists for ~10-60 s
  per hour, so there is nothing to crash, leak or supervise.

.EXAMPLE
  .\scripts\install-task.ps1
  .\scripts\install-task.ps1 -IntervalMinutes 30
#>
[CmdletBinding()]
param(
    [int]    $IntervalMinutes = 60,
    [string] $TaskName        = 'Suliko-HealthCheck',
    [string] $Python          = 'python'
)

$ErrorActionPreference = 'Stop'

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin   = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Run this from an elevated (Administrator) PowerShell.' }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path (Join-Path $root '.env'))) {
    throw ".env not found in $root. Copy .env.example to .env and fill it in first."
}

Write-Host '==> Creating virtualenv' -ForegroundColor Cyan
if (-not (Test-Path '.venv\Scripts\python.exe')) { & $Python -m venv .venv; if ($LASTEXITCODE) { throw 'venv failed (need Python 3.11+)' } }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE) { throw 'pip install failed' }

Write-Host '==> Restricting .env to SYSTEM + Administrators' -ForegroundColor Cyan
icacls .env /inheritance:r /grant:r 'SYSTEM:(R)' 'BUILTIN\Administrators:(F)' | Out-Null

Write-Host '==> Registering scheduled task' -ForegroundColor Cyan
$python  = Join-Path $root '.venv\Scripts\python.exe'
$action  = New-ScheduledTaskAction -Execute $python -Argument 'main.py' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
              -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
              -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
              -StartWhenAvailable `
              -MultipleInstances IgnoreNew `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
              -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'Hourly Suliko health check: emails/SMS on failure.' | Out-Null

Write-Host ''
Write-Host "Installed '$TaskName' (every $IntervalMinutes min, first run in ~2 min)." -ForegroundColor Green
Write-Host 'Verify now : .\.venv\Scripts\python.exe main.py --dry-run'
Write-Host 'Test alerts: .\.venv\Scripts\python.exe main.py --test-alert'
Write-Host "Run task   : Start-ScheduledTask -TaskName $TaskName"
Write-Host "Last result: (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult   (0=ok, 1=failures reported, 2=alert delivery failed)"
