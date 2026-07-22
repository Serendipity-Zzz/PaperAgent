[CmdletBinding()]
param(
    [ValidateSet('Preserve', 'Export', 'Delete')]
    [string]$DataAction = 'Preserve',
    [string]$ExportPath = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'PaperAgent-data.zip'),
    [string]$Target = (Join-Path $env:LOCALAPPDATA 'Programs\PaperAgent'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'PaperAgent\data'),
    [switch]$ConfirmDataDeletion
)

$ErrorActionPreference = 'Stop'
$Target = [IO.Path]::GetFullPath($Target)
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$ProgramsRoot = [IO.Path]::GetFullPath((Split-Path -Parent $Target)).TrimEnd('\') + '\'
if (-not $Target.StartsWith($ProgramsRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw '卸载目录超出允许范围。'
}
if ($DataAction -eq 'Delete' -and -not $ConfirmDataDeletion) {
    throw '删除用户数据必须同时传入 -ConfirmDataDeletion。此操作不可撤销。'
}

if (Test-Path -LiteralPath (Join-Path $Target 'PaperAgent.exe')) {
    Start-Process -FilePath (Join-Path $Target 'PaperAgent.exe') -ArgumentList '--stop' `
        -WindowStyle Hidden -Wait | Out-Null
}
if ($DataAction -eq 'Export' -and (Test-Path -LiteralPath $DataRoot)) {
    $ExportPath = [IO.Path]::GetFullPath($ExportPath)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ExportPath) | Out-Null
    if (Test-Path -LiteralPath $ExportPath) { Remove-Item -LiteralPath $ExportPath -Force }
    Compress-Archive -Path (Join-Path $DataRoot '*') -DestinationPath $ExportPath -CompressionLevel Optimal
}
if ($DataAction -eq 'Delete' -and (Test-Path -LiteralPath $DataRoot)) {
    $AllowedDataParent = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'PaperAgent')).TrimEnd('\') + '\'
    if (-not $DataRoot.StartsWith($AllowedDataParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw '拒绝删除不在 PaperAgent 用户数据根目录中的路径。'
    }
    Remove-Item -LiteralPath $DataRoot -Recurse -Force
}

$Desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'PaperAgent.lnk'
$MenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\PaperAgent'
Remove-Item -LiteralPath $Desktop -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $MenuDir -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
Write-Host "程序已卸载；数据策略：$DataAction。" -ForegroundColor Green
