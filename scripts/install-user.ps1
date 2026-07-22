[CmdletBinding()]
param(
    [string]$Target = (Join-Path $env:LOCALAPPDATA 'Programs\PaperAgent'),
    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA 'PaperAgent\data'),
    [switch]$NoShortcuts,
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
$Source = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$Target = [IO.Path]::GetFullPath($Target)
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$ProgramsRoot = [IO.Path]::GetFullPath((Split-Path -Parent $Target))
$StateRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'PaperAgent\install'))
$RollbackRoot = Join-Path $StateRoot 'rollback'
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Rollback = Join-Path $RollbackRoot $Timestamp
$Stage = Join-Path $ProgramsRoot ".PaperAgent-stage-$([guid]::NewGuid().ToString('N'))"

function Assert-ChildPath {
    param([string]$Path, [string]$Parent, [string]$Label)
    $NormalizedParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $NormalizedPath = [IO.Path]::GetFullPath($Path)
    if (-not $NormalizedPath.StartsWith($NormalizedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label 超出允许目录：$NormalizedPath"
    }
}

Assert-ChildPath $Target $ProgramsRoot '安装目录'
Assert-ChildPath $Stage $ProgramsRoot '暂存目录'
Assert-ChildPath $Rollback $RollbackRoot '回滚目录'
if ($Source -eq $Target) { throw '不能从正在安装的目标目录执行升级。请从新的便携包运行安装脚本。' }
if (-not (Test-Path -LiteralPath (Join-Path $Source 'PaperAgent.exe'))) {
    throw '发布包不完整：缺少 PaperAgent.exe。'
}

New-Item -ItemType Directory -Force -Path $ProgramsRoot, $RollbackRoot | Out-Null
if (Test-Path -LiteralPath (Join-Path $Target 'PaperAgent.exe')) {
    Start-Process -FilePath (Join-Path $Target 'PaperAgent.exe') -ArgumentList '--stop' `
        -WindowStyle Hidden -Wait | Out-Null
}

$HadPrevious = Test-Path -LiteralPath $Target
if ($HadPrevious) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Rollback 'app') | Out-Null
    Copy-Item -Path (Join-Path $Target '*') -Destination (Join-Path $Rollback 'app') -Recurse -Force
}
if (Test-Path -LiteralPath $DataRoot) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Rollback 'data') | Out-Null
    Copy-Item -Path (Join-Path $DataRoot '*') -Destination (Join-Path $Rollback 'data') -Recurse -Force
}

try {
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null
    Copy-Item -Path (Join-Path $Source '*') -Destination $Stage -Recurse -Force
    if (Test-Path -LiteralPath $Target) {
        Assert-ChildPath $Target $ProgramsRoot '旧安装目录'
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
    Move-Item -LiteralPath $Stage -Destination $Target
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    if (-not $SkipSmoke) {
        $PreviousData = $env:PAPERAGENT_DATA_DIR
        $env:PAPERAGENT_DATA_DIR = $DataRoot
        try {
            $Smoke = Start-Process -FilePath (Join-Path $Target 'PaperAgent.exe') `
                -ArgumentList @('--smoke-test', '--no-browser', '--no-tray') `
                -WindowStyle Hidden -Wait -PassThru
            if ($Smoke.ExitCode -ne 0) { throw "新版本 smoke 失败：exit $($Smoke.ExitCode)" }
        }
        finally { $env:PAPERAGENT_DATA_DIR = $PreviousData }
    }
}
catch {
    if (Test-Path -LiteralPath $Stage) {
        Assert-ChildPath $Stage $ProgramsRoot '失败暂存目录'
        Remove-Item -LiteralPath $Stage -Recurse -Force
    }
    if (Test-Path -LiteralPath $Target) {
        Assert-ChildPath $Target $ProgramsRoot '失败安装目录'
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
    if ($HadPrevious -and (Test-Path -LiteralPath (Join-Path $Rollback 'app'))) {
        New-Item -ItemType Directory -Force -Path $Target | Out-Null
        Copy-Item -Path (Join-Path $Rollback 'app\*') -Destination $Target -Recurse -Force
    }
    if (Test-Path -LiteralPath (Join-Path $Rollback 'data')) {
        if (Test-Path -LiteralPath $DataRoot) { Remove-Item -LiteralPath $DataRoot -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
        Copy-Item -Path (Join-Path $Rollback 'data\*') -Destination $DataRoot -Recurse -Force
    }
    throw
}

if (-not $NoShortcuts) {
    $Shell = New-Object -ComObject WScript.Shell
    $Desktop = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'PaperAgent.lnk'))
    $Desktop.TargetPath = Join-Path $Target 'PaperAgent.exe'
    $Desktop.WorkingDirectory = $Target
    $Desktop.Save()
    $MenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\PaperAgent'
    New-Item -ItemType Directory -Force -Path $MenuDir | Out-Null
    $Menu = $Shell.CreateShortcut((Join-Path $MenuDir 'PaperAgent.lnk'))
    $Menu.TargetPath = Join-Path $Target 'PaperAgent.exe'
    $Menu.WorkingDirectory = $Target
    $Menu.Save()
}

$ReleaseManifest = Join-Path $Target 'RELEASE.json'
$State = [ordered]@{
    schema_version = 1
    installed_at = (Get-Date).ToUniversalTime().ToString('o')
    target = $Target
    data_root = $DataRoot
    rollback = if ($HadPrevious) { $Rollback } else { $null }
    release = if (Test-Path -LiteralPath $ReleaseManifest) {
        Get-Content -LiteralPath $ReleaseManifest -Raw | ConvertFrom-Json
    } else { $null }
}
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
$State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $StateRoot 'installed.json') -Encoding utf8
Write-Host "PaperAgent 已安装到 $Target；数据目录为 $DataRoot。" -ForegroundColor Green
if ($HadPrevious) { Write-Host "升级前快照：$Rollback" }
