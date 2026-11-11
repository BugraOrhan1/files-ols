param(
    [Parameter(Mandatory = $true)]
    [string]$DatabaseBackup,
    [Parameter(Mandatory = $true)]
    [string]$DatabaseManifest,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "OlsHashWorker"),
    [string]$ExpectedServerAddress = "100.117.16.25",
    [switch]$RegisterTask,
    [switch]$StartWorker
)

$ErrorActionPreference = "Stop"
$LocalAddress = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
    Where-Object { $_.IPAddress -eq $ExpectedServerAddress } |
    Select-Object -First 1
if ($null -eq $LocalAddress) {
    throw "This computer does not own the expected Dell address $ExpectedServerAddress. Run the installer on the OLS host or pass its verified local address."
}

$PackageRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
foreach ($Required in @(
    (Join-Path $PackageRoot "ols_platform"),
    (Join-Path $PackageRoot "tests"),
    (Join-Path $PackageRoot "deployment\configure_dell_database.py")
)) {
    if (-not (Test-Path -LiteralPath $Required)) {
        throw "Deployment package is incomplete: $Required"
    }
}
if (Test-Path -LiteralPath $InstallRoot) {
    throw "Install destination already exists; refusing to overwrite files or data: $InstallRoot"
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $PythonCommand) {
    throw "Python 3.11 or newer was not found on PATH. Install it for the intended Windows user first."
}
$PythonPath = $PythonCommand.Source
$VersionOutput = (& $PythonPath --version 2>&1 | Out-String).Trim()
if ($VersionOutput -notmatch "Python\s+([0-9]+)\.([0-9]+)") {
    throw "Could not determine the installed Python version: $VersionOutput"
}
$PythonMajor = [int]$Matches[1]
$PythonMinor = [int]$Matches[2]
if (($PythonMajor -lt 3) -or (($PythonMajor -eq 3) -and ($PythonMinor -lt 11))) {
    throw "Python 3.11 or newer is required; found $VersionOutput"
}

$Share = Get-SmbShare -Name "d" -ErrorAction Stop
if ([string]::IsNullOrWhiteSpace([string]$Share.Path)) {
    throw "Windows share 'd' did not expose a local filesystem path."
}
$ShareRoot = [System.IO.Path]::GetFullPath([string]$Share.Path)
$RootNames = @("Database 1","Database 2","Database 3","Database 4","default")
$LocalRoots = [ordered]@{}
foreach ($Name in $RootNames) {
    $LocalPath = [System.IO.Path]::GetFullPath((Join-Path $ShareRoot $Name))
    if (-not (Test-Path -LiteralPath $LocalPath -PathType Container)) {
        throw "Expected local directory from share configuration is missing: $LocalPath"
    }
    Get-ChildItem -LiteralPath $LocalPath -Force -ErrorAction Stop |
        Select-Object -First 1 | Out-Null
    $NormalizedRoot = $LocalPath.TrimEnd("\")
    $RootPrefix = $NormalizedRoot + "\"
    if ($InstallRoot.Equals(
            $NormalizedRoot,[System.StringComparison]::OrdinalIgnoreCase
        ) -or $InstallRoot.StartsWith(
            $RootPrefix,[System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "InstallRoot must not be inside any OLS source root: $InstallRoot"
    }
    $LocalRoots[$Name] = $LocalPath
}
if (@($LocalRoots.Values | ForEach-Object { $_.ToLowerInvariant() } | Select-Object -Unique).Count -ne 5) {
    throw "The five local root paths did not resolve to five distinct directories."
}

$DatabaseBackup = (Resolve-Path -LiteralPath $DatabaseBackup).Path
$DatabaseManifest = (Resolve-Path -LiteralPath $DatabaseManifest).Path
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $PackageRoot "ols_platform") -Destination $InstallRoot -Recurse
Copy-Item -LiteralPath (Join-Path $PackageRoot "tests") -Destination $InstallRoot -Recurse
Copy-Item -LiteralPath (Join-Path $PackageRoot "deployment") -Destination $InstallRoot -Recurse
Copy-Item -LiteralPath (Join-Path $PackageRoot "README.md") -Destination $InstallRoot

$DataRoot = Join-Path $InstallRoot "data"
$BackupRoot = Join-Path $DataRoot "backups"
$ConfigRoot = Join-Path $InstallRoot "config"
New-Item -ItemType Directory -Force -Path $BackupRoot,$ConfigRoot | Out-Null
$LocalBackup = Join-Path $BackupRoot "ols-index-source-backup.sqlite3"
$LocalManifest = Join-Path $BackupRoot "database-manifest.json"
Copy-Item -LiteralPath $DatabaseBackup -Destination $LocalBackup
Copy-Item -LiteralPath $DatabaseManifest -Destination $LocalManifest

$PreviousLocation = Get-Location
try {
    Set-Location $InstallRoot
    & $PythonPath -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Dell deployment test suite failed with exit code $LASTEXITCODE."
    }
}
finally {
    Set-Location $PreviousLocation
}

$RootsConfig = Join-Path $ConfigRoot "local-source-roots.json"
$LocalRoots | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $RootsConfig -Encoding UTF8
$DatabasePath = Join-Path $DataRoot "ols-index.sqlite3"
& $PythonPath (Join-Path $InstallRoot "deployment\configure_dell_database.py") `
    --database-backup $LocalBackup --database-path $DatabasePath `
    --manifest $LocalManifest --root-config $RootsConfig
if ($LASTEXITCODE -ne 0) {
    throw "SQLite restore, migration, or local-root verification failed."
}

$Config = [ordered]@{
    database_path = $DatabasePath
    sources = @(
        foreach ($Name in $RootNames) {
            [ordered]@{ name = $Name; path = $LocalRoots[$Name] }
        }
    )
    workers = 2
    batch_size = 10
    chunk_size = 1048576
    batch_delay_seconds = 1
    retry_limit = 5
    retry_delay_seconds = 60
    expected_server_address = $ExpectedServerAddress
}
$Config | ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath (Join-Path $ConfigRoot "hash-worker.json") -Encoding UTF8
$PythonPath | Set-Content -LiteralPath (Join-Path $InstallRoot "python-path.txt") -Encoding UTF8

Push-Location $InstallRoot
try {
    & $PythonPath -m ols_platform.cli --database $DatabasePath hashing plan
    if ($LASTEXITCODE -ne 0) {
        throw "The local five-root queue check failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

Write-Host "Local Dell deployment validated at $InstallRoot."
Write-Host "Database: $DatabasePath"
Write-Host "Share root resolved from Get-SmbShare -Name d: $ShareRoot"
Write-Host "No organization power policy was changed. The worker prevents system sleep while active."
if ($RegisterTask) {
    & (Join-Path $InstallRoot "deployment\register_hash_worker_task.ps1") `
        -InstallRoot $InstallRoot -ExpectedServerAddress $ExpectedServerAddress
}
if ($StartWorker) {
    & (Join-Path $InstallRoot "deployment\start_hash_worker.ps1") `
        -Action Start -InstallRoot $InstallRoot `
        -ExpectedServerAddress $ExpectedServerAddress
}
else {
    Write-Host "Start manually: & '$InstallRoot\deployment\start_hash_worker.ps1' -Action Start"
}
