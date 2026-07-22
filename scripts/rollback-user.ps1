[CmdletBinding()]
param(
    [string]$Target = (Join-Path $env:LOCALAPPDATA 'Programs\PaperAgent'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'PaperAgent\data'),
    [string]$Snapshot = '',
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
$Target = [IO.Path]::GetFullPath($Target)
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$RollbackRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'PaperAgent\install\rollback'))
if (-not $Snapshot) {
    $Snapshot = Get-ChildItem -LiteralPath $RollbackRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $Snapshot -or -not (Test-Path -LiteralPath (Join-Path $Snapshot 'app\PaperAgent.exe'))) {
    throw '没有可用的回滚快照。'
}
$Snapshot = [IO.Path]::GetFullPath($Snapshot)
if (-not $Snapshot.StartsWith($RollbackRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw '回滚快照超出允许目录。'
}

if (Test-Path -LiteralPath (Join-Path $Target 'PaperAgent.exe')) {
    Start-Process -FilePath (Join-Path $Target 'PaperAgent.exe') -ArgumentList '--stop' `
        -WindowStyle Hidden -Wait | Out-Null
}
$Safety = Join-Path $RollbackRoot ("rollback-safety-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $Safety | Out-Null
if (Test-Path -LiteralPath $Target) { Copy-Item -Path (Join-Path $Target '*') -Destination $Safety -Recurse -Force }
if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Target | Out-Null
Copy-Item -Path (Join-Path $Snapshot 'app\*') -Destination $Target -Recurse -Force
if (Test-Path -LiteralPath (Join-Path $Snapshot 'data')) {
    if (Test-Path -LiteralPath $DataRoot) { Remove-Item -LiteralPath $DataRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    Copy-Item -Path (Join-Path $Snapshot 'data\*') -Destination $DataRoot -Recurse -Force
}
if (-not $SkipSmoke) {
    $PreviousData = $env:PAPERAGENT_DATA_DIR
    $env:PAPERAGENT_DATA_DIR = $DataRoot
    try {
        $Smoke = Start-Process -FilePath (Join-Path $Target 'PaperAgent.exe') `
            -ArgumentList @('--smoke-test', '--no-browser', '--no-tray') `
            -WindowStyle Hidden -Wait -PassThru
        if ($Smoke.ExitCode -ne 0) { throw "回滚版本 smoke 失败：exit $($Smoke.ExitCode)" }
    }
    finally { $env:PAPERAGENT_DATA_DIR = $PreviousData }
}
Write-Host "已回滚到快照：$Snapshot" -ForegroundColor Green
