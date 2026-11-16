[CmdletBinding()]
param([string] $TaskName = 'Suliko-HealthCheck')
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'. Files in D:\suliko-healthcheck are untouched."
