param(
    [string]$InstallRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "OlsHashWorker",
    [string]$ExpectedServerAddress = "100.117.16.25"
)

$ErrorActionPreference = "Stop"
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$LocalAddress = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
    Where-Object { $_.IPAddress -eq $ExpectedServerAddress } |
    Select-Object -First 1
if ($null -eq $LocalAddress) {
    throw "This computer does not own the expected OLS host address $ExpectedServerAddress."
}
$StartScript = Join-Path $InstallRoot "deployment\start_hash_worker.ps1"
if (-not (Test-Path -LiteralPath $StartScript -PathType Leaf)) {
    throw "Worker start script not found: $StartScript"
}
$PowerShell = (Get-Process -Id $PID).Path
$Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' +
    $StartScript + '" -Action Start -InstallRoot "' + $InstallRoot +
    '" -ExpectedServerAddress "' + $ExpectedServerAddress + '"'
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User (
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -WakeToRun `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal -Force | Out-Null
Write-Host "Registered '$TaskName' to start at logon for $User."
Write-Host "No password was embedded; the task runs in this user's logon session."
Write-Host "Task Scheduler will restart it after a non-zero worker exit."
Write-Host "The process keeps the system awake while hashing; display sleep remains allowed."
