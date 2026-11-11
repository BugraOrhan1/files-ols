param(
    [ValidateSet("Start","Status","Pause","Resume","Stop","Retry")]
    [string]$Action = "Start",
    [string]$InstallRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ExpectedServerAddress = "100.117.16.25"
)

$ErrorActionPreference = "Stop"
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$ConfigPath = Join-Path $InstallRoot "config\hash-worker.json"
$PythonPath = Join-Path $InstallRoot "python-path.txt"

$LocalAddress = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
    Where-Object { $_.IPAddress -eq $ExpectedServerAddress } |
    Select-Object -First 1
if ($null -eq $LocalAddress) {
    throw "This computer does not own the expected OLS host address $ExpectedServerAddress."
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Worker configuration not found: $ConfigPath. Run install_hash_worker.ps1 first."
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python path configuration is missing: $PythonPath."
}
$Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ([string]$Config.expected_server_address -ne $ExpectedServerAddress) {
    throw "The requested host address does not match the installed configuration."
}
$Python = (Get-Content -LiteralPath $PythonPath -Raw).Trim()
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Configured Python executable is unavailable: $Python"
}
$DatabasePath = [System.IO.Path]::GetFullPath([string]$Config.database_path)
if ($DatabasePath.StartsWith("\\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "SQLite must be local to Dell. UNC database path rejected: $DatabasePath"
}
if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
    throw "Local SQLite database is missing: $DatabasePath"
}
foreach ($Source in $Config.sources) {
    $SourcePath = [System.IO.Path]::GetFullPath([string]$Source.path)
    if ($SourcePath.StartsWith("\\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Source is not local; refusing SMB hashing: $SourcePath"
    }
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Container)) {
        throw "Configured local source is unavailable: $SourcePath"
    }
    Get-ChildItem -LiteralPath $SourcePath -Force -ErrorAction Stop |
        Select-Object -First 1 | Out-Null
}

$Cli = @("-m", "ols_platform.cli", "--database", $DatabasePath)
switch ($Action) {
    "Status" { & $Python @Cli hashing status; exit $LASTEXITCODE }
    "Pause"  { & $Python @Cli hashing pause; exit $LASTEXITCODE }
    "Resume" { & $Python @Cli hashing resume; exit $LASTEXITCODE }
    "Stop"   { & $Python @Cli hashing stop; exit $LASTEXITCODE }
    "Retry"  { & $Python @Cli hashing retry; exit $LASTEXITCODE }
}

$LockPath = Join-Path $InstallRoot "data\hash-worker.lock"
$LogDirectory = Join-Path $InstallRoot "data\logs"
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$LockStream = $null
try {
    try {
        $LockStream = [System.IO.File]::Open(
            $LockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        throw "Another start_hash_worker.ps1 process holds the local single-instance lock."
    }
    $LogPath = Join-Path $LogDirectory (
        "hash-worker-" + (Get-Date -Format "yyyyMMdd") + ".log"
    )
    "Starting continuous hash worker; log: $LogPath" |
        Tee-Object -FilePath $LogPath -Append
    & $Python -u -m ols_platform.cli --database $DatabasePath hash-worker `
        --continuous --workers ([int]$Config.workers) `
        --batch-size ([int]$Config.batch_size) `
        --chunk-size ([int]$Config.chunk_size) `
        --batch-delay ([double]$Config.batch_delay_seconds) `
        --retry-limit ([int]$Config.retry_limit) `
        --retry-delay ([double]$Config.retry_delay_seconds) `
        --report (Join-Path $LogDirectory "last-worker-report.json") *>&1 |
        Tee-Object -FilePath $LogPath -Append
    $WorkerExitCode = $LASTEXITCODE
    "Worker exited with code $WorkerExitCode at $(Get-Date -Format o)" |
        Tee-Object -FilePath $LogPath -Append
    if ($WorkerExitCode -ne 0) {
        throw "Continuous worker exited with code $WorkerExitCode. Review $LogPath."
    }
}
finally {
    if ($null -ne $LockStream) {
        $LockStream.Dispose()
    }
    if (Test-Path -LiteralPath $LockPath -PathType Leaf) {
        Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
    }
}
