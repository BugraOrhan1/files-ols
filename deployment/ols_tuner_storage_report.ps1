<#
.SYNOPSIS
  Read-only report of what takes up space in the OLS Tuner data folder.
  Deletes nothing.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File ols_tuner_storage_report.ps1 -Store "E:\Users\bobda\AppData\Local\OLS Tuner"
#>
param(
    [string]$Store = (Join-Path $env:LOCALAPPDATA "OLS Tuner"),
    [int]$Top = 30
)

if (-not (Test-Path -LiteralPath $Store)) { Write-Error "Map niet gevonden: $Store"; exit 1 }

function Fmt([double]$b) {
    if ($b -ge 1GB) { "{0:N2} GB" -f ($b / 1GB) } elseif ($b -ge 1MB) { "{0:N1} MB" -f ($b / 1MB) } else { "{0:N0} KB" -f ($b / 1KB) }
}

$drive = (Get-Item -LiteralPath $Store).PSDrive
Write-Host "`n=== Schijf $($drive.Name): vrij $(Fmt $drive.Free) van $(Fmt ($drive.Free + $drive.Used))"

$files = Get-ChildItem -LiteralPath $Store -Recurse -File -Force -ErrorAction SilentlyContinue
$total = ($files | Measure-Object Length -Sum).Sum
Write-Host "=== $Store : $(Fmt $total) in $($files.Count) bestanden`n"

Write-Host "--- Per submap (bovenste niveau)"
$files | Group-Object { $rel = $_.FullName.Substring($Store.Length).TrimStart('\'); if ($rel -match '\\') { $rel.Split('\')[0] + '\' } else { '(los bestand)' } } |
    ForEach-Object { [pscustomobject]@{ Map = $_.Name; Bestanden = $_.Count; Bytes = ($_.Group | Measure-Object Length -Sum).Sum } } |
    Sort-Object Bytes -Descending | Format-Table Map, Bestanden, @{n='Grootte';e={Fmt $_.Bytes}} -AutoSize

Write-Host "--- Per extensie"
$files | Group-Object Extension |
    ForEach-Object { [pscustomobject]@{ Ext = $_.Name; Bestanden = $_.Count; Bytes = ($_.Group | Measure-Object Length -Sum).Sum } } |
    Sort-Object Bytes -Descending | Select-Object -First 15 |
    Format-Table Ext, Bestanden, @{n='Grootte';e={Fmt $_.Bytes}} -AutoSize

Write-Host "--- Grootste $Top bestanden"
$files | Sort-Object Length -Descending | Select-Object -First $Top |
    Format-Table @{n='Grootte';e={Fmt $_.Length}}, LastWriteTime, @{n='Pad';e={$_.FullName.Substring($Store.Length)}} -AutoSize

Write-Host "--- Modelpakket-versies (active.json)"
$active = Get-ChildItem -LiteralPath $Store -Recurse -Filter active.json -ErrorAction SilentlyContinue | Select-Object -First 1
if ($active) {
    $a = Get-Content -LiteralPath $active.FullName -Raw | ConvertFrom-Json
    Write-Host "Actief: $($a.active); oude versies in history: $($a.history.Count)"
    $ids = @($a.history) + @($a.active)
    $pkgBytes = 0
    foreach ($id in $ids) {
        $hit = $files | Where-Object { $_.FullName -like "*$id*" }
        $pkgBytes += ($hit | Measure-Object Length -Sum).Sum
    }
    Write-Host "Bestanden/mappen met een pakket-id in de naam: $(Fmt $pkgBytes)"
} else { Write-Host "active.json niet gevonden." }

Write-Host "`nKlaar. Er is niets verwijderd. Stuur deze uitvoer mee om gericht op te ruimen."
